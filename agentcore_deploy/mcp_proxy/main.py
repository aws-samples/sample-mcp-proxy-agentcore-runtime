"""MCP Proxy Server - Pure passthrough to AgentCore Gateway.

This is a simple MCP server that:
1. Connects to an AgentCore Gateway on startup
2. Fetches all tools registered on the gateway
3. Exposes them as its own tools via standard MCP protocol
4. Forwards all tool calls to the gateway and returns results

It runs on AgentCore Runtime using FastMCP's streamable-http transport.
"""

import os
import json
import logging
import base64
import time
import httpx
import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from mcp.server.fastmcp import FastMCP

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger("mcp_proxy")

# Configuration from environment
GATEWAY_ENDPOINT = os.environ.get("GATEWAY_ENDPOINT")
if not GATEWAY_ENDPOINT:
    raise RuntimeError("GATEWAY_ENDPOINT environment variable is required")

# Create the MCP server
mcp = FastMCP("MCP Proxy", host="0.0.0.0", stateless_http=True)

# Boto3 session for SigV4 signing
_boto_session = boto3.Session()
_request_counter = 0


class TokenProvider:
    """Manages JWT token lifecycle for gateway authentication."""

    def __init__(self, user_pool_id: str, client_id: str, client_secret: str, region: str,
                 cognito_domain: str = None):
        if cognito_domain:
            self._token_endpoint = (
                f"https://{cognito_domain}.auth.{region}.amazoncognito.com/oauth2/token"
            )
        else:
            self._token_endpoint = (
                f"https://{user_pool_id}.auth.{region}.amazoncognito.com/oauth2/token"
            )
        self._auth_header = "Basic " + base64.b64encode(
            f"{client_id}:{client_secret}".encode()
        ).decode()
        self._access_token: str | None = None
        self._token_expiry: float = 0.0

    def get_token(self) -> str:
        """Return a valid access token, refreshing if needed."""
        if not self._is_token_valid():
            self._refresh_token()
        return self._access_token

    def _is_token_valid(self) -> bool:
        """Check if cached token exists and has >60s until expiry."""
        return self._access_token is not None and (self._token_expiry - time.time()) > 60

    def _refresh_token(self) -> None:
        """Request a new token from the Cognito token endpoint."""
        response = httpx.post(
            self._token_endpoint,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Authorization": self._auth_header,
            },
            content="grant_type=client_credentials",
            timeout=10.0,
        )
        if response.status_code != 200:
            raise RuntimeError(
                f"Token request failed ({response.status_code}): {response.text}"
            )
        data = response.json()
        self._access_token = data["access_token"]
        self._token_expiry = time.time() + data["expires_in"]


# Auth configuration
AUTH_MODE = os.environ.get("AUTH_MODE", "iam").lower()
_token_provider: TokenProvider | None = None

# Validate auth configuration at startup
if AUTH_MODE == "jwt":
    _missing = [
        v for v in ("COGNITO_USER_POOL_ID", "COGNITO_CLIENT_ID", "COGNITO_CLIENT_SECRET")
        if not os.environ.get(v)
    ]
    if _missing:
        raise RuntimeError(f"AUTH_MODE is 'jwt' but missing required env vars: {', '.join(_missing)}")
    _token_provider = TokenProvider(
        user_pool_id=os.environ["COGNITO_USER_POOL_ID"],
        client_id=os.environ["COGNITO_CLIENT_ID"],
        client_secret=os.environ["COGNITO_CLIENT_SECRET"],
        region=_boto_session.region_name or "us-east-1",
        cognito_domain=os.environ.get("COGNITO_DOMAIN"),
    )
    logger.info("Auth mode: jwt (Cognito client credentials)")
elif AUTH_MODE == "iam":
    logger.info("Auth mode: iam (SigV4 signing)")
else:
    raise ValueError(f"Unknown AUTH_MODE: '{AUTH_MODE}'. Must be 'iam' or 'jwt'.")


def _send_gateway_request(method: str, params: dict = None) -> dict:
    """Send a JSON-RPC request to the gateway (synchronous)."""
    global _request_counter
    _request_counter += 1

    payload = {"jsonrpc": "2.0", "method": method, "id": _request_counter}
    if params:
        payload["params"] = params

    body = json.dumps(payload)

    if AUTH_MODE == "iam":
        # SigV4 sign the request
        credentials = _boto_session.get_credentials()
        aws_request = AWSRequest(
            method="POST",
            url=GATEWAY_ENDPOINT,
            data=body,
            headers={"Content-Type": "application/json"},
        )
        SigV4Auth(
            credentials, "bedrock-agentcore", _boto_session.region_name or "us-east-1"
        ).add_auth(aws_request)

        # Send it
        response = httpx.post(
            GATEWAY_ENDPOINT,
            content=body,
            headers=dict(aws_request.headers),
            timeout=30.0,
        )
    elif AUTH_MODE == "jwt":
        token = _token_provider.get_token()
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        }
        response = httpx.post(GATEWAY_ENDPOINT, content=body, headers=headers, timeout=30.0)
    else:
        raise ValueError(f"Unknown AUTH_MODE: {AUTH_MODE}")

    response.raise_for_status()
    result = response.json()

    if "error" in result:
        logger.warning(json.dumps({
            "event": "gateway_error",
            "method": method,
            "request_id": _request_counter,
            "error": str(result["error"]),
        }))
        raise RuntimeError(f"Gateway error: {result['error']}")

    return result.get("result", {})


def _make_tool_handler(tool_name: str):
    """Create a tool handler function that forwards calls to the gateway."""
    def handler(**kwargs) -> str:
        logger.info(json.dumps({
            "event": "tool_call",
            "tool": tool_name,
            "request_id": _request_counter + 1,
        }))
        result = _send_gateway_request("tools/call", {"name": tool_name, "arguments": kwargs})
        # Return the content from the gateway response
        content = result.get("content", [])
        if content and isinstance(content, list):
            texts = [c.get("text", str(c)) for c in content if isinstance(c, dict)]
            return "\n".join(texts) if texts else json.dumps(result)
        return json.dumps(result)
    return handler


def register_gateway_tools():
    """Fetch tools from gateway and register them on this MCP server."""
    logger.info(f"Fetching tools from gateway: {GATEWAY_ENDPOINT}")
    try:
        result = _send_gateway_request("tools/list")
        tools = result.get("tools", [])
        logger.info(f"Found {len(tools)} tools on gateway")

        for tool in tools:
            name = tool.get("name", "unknown")
            description = tool.get("description", "")

            handler = _make_tool_handler(name)
            handler.__name__ = name
            handler.__doc__ = description

            mcp.tool(name=name, description=description)(handler)
            logger.info(f"  Registered tool: {name}")

    except Exception as e:
        logger.warning(f"Failed to fetch tools from gateway: {e}")
        raise


# Register tools at import time (before server starts)
register_gateway_tools()


if __name__ == "__main__":
    logger.info("Starting MCP Proxy Server (streamable-http transport)")
    mcp.run(transport="streamable-http")
