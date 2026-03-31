# MCP Proxy Server in AgentCore Runtime

A lightweight MCP proxy that runs on [Amazon Bedrock AgentCore Runtime](https://aws.amazon.com/bedrock/agentcore/) and exposes tools from an upstream MCP server as a standard MCP server. The upstream MCP server can be any MCP-compatible endpoint, including MCP servers running on AgentCore Runtime, self-hosted MCP servers, or third-party MCP services. This implementation showcases the pattern using an AgentCore Gateway as the upstream server example, but the proxy architecture applies to any MCP server that you want to add custom controls to.

## Architecture

```
User / Agent  →  MCP Proxy (AgentCore Runtime)  →  AgentCore Gateway  →  Tools
```

![Diagram](image/diagram.svg)

The proxy fetches all tools registered on the gateway at startup, registers them locally via FastMCP, and forwards every `tools/call` request to the gateway. Clients connect using the standard MCP streamable-http transport.

The proxy supports two authentication modes for the outbound gateway connection:
- **IAM (default)** — requests are signed with SigV4 using the runtime's IAM role
- **JWT** — an OAuth access token is obtained from Amazon Cognito (client credentials grant) and sent as a Bearer token

## Project structure

```
├── agentcore_deploy/          # Deployed to AgentCore Runtime
│   ├── mcp_proxy/
│   │   ├── __init__.py
│   │   └── main.py            # The proxy server
│   ├── Dockerfile
│   ├── requirements.txt
│   └── .dockerignore
├── setup_and_deploy.py        # Automated deployment script
├── deploy_config.json         # Deployment configuration template
├── test_agent.py              # Strands agent for testing the proxy
└── README.md
```

## Prerequisites

- Python 3.12+
- AWS CLI configured with credentials
- [AgentCore CLI](https://pypi.org/project/bedrock-agentcore-starter-toolkit/) (`pip install bedrock-agentcore-starter-toolkit`)
- Docker
- An AgentCore Gateway with tools registered

## Deploy

### 1. Configure

Edit `deploy_config.json` with your values:

```json
{
  "agent_name": "my-mcp-proxy",
  "gateway_endpoint": "https://<your-gateway>.gateway.bedrock-agentcore.<region>.amazonaws.com/mcp",
  "region": "us-east-1",
  "iam_role_name": "MCPProxyServerRole",
  "auth_mode": "iam"
}
```

For JWT authentication, set `auth_mode` to `"jwt"` and provide Cognito details:

```json
{
  "auth_mode": "jwt",
  "cognito_user_pool_id": "us-east-1_XXXXXXXXX",
  "cognito_client_id": "<app-client-id>",
  "cognito_client_secret": "<app-client-secret>",
  "cognito_domain": "<cognito-domain-prefix>"
}
```

### 2. Run the deployment script

```bash
python3 setup_and_deploy.py
```

The script will:
1. Create an IAM execution role (if it doesn't exist)
2. Run `agentcore configure` interactively
3. Deploy to AgentCore Runtime with the correct environment variables

You can also override config values via CLI flags:

```bash
python3 setup_and_deploy.py \
  --agent-name my_proxy \
  --gateway-endpoint https://<gateway-url>/mcp \
  --auth-mode jwt
```

### 3. Manual deployment (alternative)

```bash
cd agentcore_deploy
agentcore configure --name my_proxy --entrypoint mcp_proxy/main.py \
  --execution-role <role-arn> --protocol MCP --requirements-file requirements.txt
agentcore launch --agent my_proxy \
  --env GATEWAY_ENDPOINT=https://<gateway-url>/mcp \
  --env AUTH_MODE=iam
```

For JWT mode, add the Cognito environment variables:

```bash
agentcore launch --agent my_proxy \
  --env GATEWAY_ENDPOINT=https://<gateway-url>/mcp \
  --env AUTH_MODE=jwt \
  --env COGNITO_USER_POOL_ID=us-east-1_XXXXXXXXX \
  --env COGNITO_CLIENT_ID=<client-id> \
  --env COGNITO_CLIENT_SECRET=<client-secret> \
  --env COGNITO_DOMAIN=<domain-prefix>
```

## Test with a Strands agent

Update `MCP_PROXY_ARN` in `test_agent.py` with your deployed proxy's ARN, then:

```bash
pip install strands-agents strands-agents-tools boto3 httpx
python3 test_agent.py
```

The agent discovers tools from the proxy and lets you interact with them in a chat loop.

## How it works

`agentcore_deploy/mcp_proxy/main.py` does three things:

1. On startup, sends a `tools/list` JSON-RPC request to the gateway (authenticated via IAM or JWT).
2. For each tool returned, registers a FastMCP tool that forwards `tools/call` to the gateway.
3. Runs FastMCP with `stateless_http=True` and `transport="streamable-http"`.

AgentCore Runtime handles the HTTP ingress and client authorization. The proxy itself is stateless.

## Environment variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `GATEWAY_ENDPOINT` | Yes | — | AgentCore Gateway MCP endpoint URL |
| `AUTH_MODE` | No | `iam` | `iam` or `jwt` |
| `COGNITO_USER_POOL_ID` | When jwt | — | Cognito User Pool ID |
| `COGNITO_CLIENT_ID` | When jwt | — | OAuth App Client ID |
| `COGNITO_CLIENT_SECRET` | When jwt | — | OAuth App Client Secret |
| `COGNITO_DOMAIN` | When jwt | — | Cognito domain prefix for token endpoint |

## License

MIT
