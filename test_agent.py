"""Strands Agent that connects to the MCP Proxy on AgentCore Runtime.

Architecture:
  User → Strands Agent (local) → MCP Proxy (AgentCore) → Gateway → Tools

The agent discovers tools from the MCP proxy and uses them to answer questions.
The MCP proxy is a standard MCP server running on AgentCore that forwards
all requests to the AgentCore Gateway.

Usage:
  1. Update MCP_PROXY_ARN below with your deployed proxy's ARN
  2. Run: python3 test_agent.py
"""

import json
import uuid
import urllib.parse
import logging

import boto3
import httpx
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from strands import Agent, tool
from strands.models.bedrock import BedrockModel

logging.basicConfig(level=logging.WARNING)

# ── Configuration ──────────────────────────────────────────────────────────
# Replace with your deployed proxy's ARN (from `agentcore status` output)
MCP_PROXY_ARN = "arn:aws:bedrock-agentcore:us-east-1:<account-id>:runtime/<agent-id>"
REGION = "us-east-1"
MODEL_ID = "us.anthropic.claude-sonnet-4-20250514-v1:0"


def _build_endpoint(arn: str, region: str) -> str:
    encoded = urllib.parse.quote(arn, safe="")
    return f"https://bedrock-agentcore.{region}.amazonaws.com/runtimes/{encoded}/invocations?qualifier=DEFAULT"


def _send_mcp_request(endpoint: str, method: str, params: dict = None) -> dict:
    """Send a signed MCP JSON-RPC request and parse the response."""
    session = boto3.Session()
    creds = session.get_credentials()

    body = json.dumps({
        "jsonrpc": "2.0",
        "id": str(uuid.uuid4()),
        "method": method,
        "params": params or {},
    })

    aws_req = AWSRequest(
        method="POST", url=endpoint, data=body,
        headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream"},
    )
    SigV4Auth(creds, "bedrock-agentcore", REGION).add_auth(aws_req)

    resp = httpx.post(endpoint, content=body, headers=dict(aws_req.headers), timeout=120.0)
    resp.raise_for_status()

    # Try SSE format first (data: lines), then fall back to plain JSON
    for line in resp.text.split("\n"):
        if line.startswith("data:"):
            payload = json.loads(line[5:].strip())
            if "error" in payload:
                raise RuntimeError(f"MCP error: {payload['error']}")
            return payload.get("result", {})

    # Fall back to plain JSON response
    payload = json.loads(resp.text)
    if "error" in payload:
        raise RuntimeError(f"MCP error: {payload['error']}")
    return payload.get("result", {})


def _make_tool(name: str, description: str, endpoint: str):
    """Create a Strands-compatible tool function that calls the MCP proxy."""
    safe_name = name.replace("-", "_")

    @tool(name=safe_name, description=description)
    def tool_fn(**kwargs) -> str:
        result = _send_mcp_request(endpoint, "tools/call", {"name": name, "arguments": kwargs})
        content = result.get("content", [])
        if content and isinstance(content, list):
            return "\n".join(c.get("text", str(c)) for c in content if isinstance(c, dict))
        return json.dumps(result)

    return tool_fn


def main():
    endpoint = _build_endpoint(MCP_PROXY_ARN, REGION)

    print("Discovering tools from MCP proxy...")
    tools_result = _send_mcp_request(endpoint, "tools/list")
    tools_meta = tools_result.get("tools", [])
    print(f"Found {len(tools_meta)} tools:")
    for t in tools_meta:
        print(f"  - {t['name']}: {t.get('description', '')}")
    print()

    tools = [_make_tool(t["name"], t.get("description", ""), endpoint) for t in tools_meta]

    agent = Agent(
        model=BedrockModel(model_id=MODEL_ID, region=REGION),
        tools=tools,
        system_prompt="You are a helpful assistant. Use the available tools when needed.",
    )

    print("Agent ready. Type 'quit' to exit.\n")
    while True:
        user_input = input("You: ").strip()
        if not user_input:
            continue
        if user_input.lower() in ("quit", "exit", "q"):
            break
        try:
            response = agent(user_input)
            print(f"Agent: {response}\n")
        except Exception as e:
            print(f"Error: {e}\n")


if __name__ == "__main__":
    main()
