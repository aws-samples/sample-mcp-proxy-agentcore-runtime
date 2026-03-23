#!/usr/bin/env python3
"""
Automated setup and deployment script for MCP Proxy Server.

This script automates the complete deployment process:
1. Creates IAM role with appropriate permissions
2. Generates configuration file
3. Packages the application
4. Deploys to AgentCore Runtime
5. Verifies deployment success
"""

import argparse
import getpass
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional, Tuple


class Colors:
    """ANSI color codes for terminal output."""
    HEADER = '\033[95m'
    OKBLUE = '\033[94m'
    OKCYAN = '\033[96m'
    OKGREEN = '\033[92m'
    WARNING = '\033[93m'
    FAIL = '\033[91m'
    ENDC = '\033[0m'
    BOLD = '\033[1m'


def print_header(message: str) -> None:
    """Print a formatted header message."""
    print(f"\n{Colors.HEADER}{Colors.BOLD}{'=' * 70}{Colors.ENDC}")
    print(f"{Colors.HEADER}{Colors.BOLD}{message}{Colors.ENDC}")
    print(f"{Colors.HEADER}{Colors.BOLD}{'=' * 70}{Colors.ENDC}\n")


def print_success(message: str) -> None:
    """Print a success message."""
    print(f"{Colors.OKGREEN}✓ {message}{Colors.ENDC}")


def print_error(message: str) -> None:
    """Print an error message."""
    print(f"{Colors.FAIL}✗ {message}{Colors.ENDC}")


def print_warning(message: str) -> None:
    """Print a warning message."""
    print(f"{Colors.WARNING}⚠ {message}{Colors.ENDC}")


def print_info(message: str) -> None:
    """Print an info message."""
    print(f"{Colors.OKCYAN}ℹ {message}{Colors.ENDC}")


def run_command(
    command: list,
    capture_output: bool = True,
    check: bool = True,
    interactive: bool = False
) -> Tuple[int, str, str]:
    """
    Run a shell command and return the result.
    
    Args:
        command: Command and arguments as a list
        capture_output: Whether to capture stdout/stderr
        check: Whether to raise exception on non-zero exit
        interactive: Whether to allow interactive input (stdin, stdout, stderr not redirected)
        
    Returns:
        Tuple of (exit_code, stdout, stderr)
    """
    try:
        if interactive:
            # Run with full terminal interaction (no redirection)
            # This allows the command to prompt for user input
            result = subprocess.run( # nosemgrep: dangerous-subprocess-use-audit — list args, no shell; inputs from local config/CLI
                command,
                check=check
            )
            return result.returncode, "", ""
        else:
            result = subprocess.run( # nosemgrep: dangerous-subprocess-use-audit — list args, no shell; inputs from local config/CLI
                command,
                capture_output=capture_output,
                text=True,
                check=check
            )
            return result.returncode, result.stdout, result.stderr
    except subprocess.CalledProcessError as e:
        if interactive or not capture_output:
            return e.returncode, "", ""
        return e.returncode, e.stdout, e.stderr
    except FileNotFoundError:
        return 127, "", f"Command not found: {command[0]}"


def check_prerequisites() -> bool:
    """
    Check if all required tools are installed.
    
    Returns:
        True if all prerequisites are met, False otherwise
    """
    print_header("Checking Prerequisites")
    
    all_ok = True
    
    # Check AWS CLI
    exit_code, stdout, _ = run_command(["aws", "--version"], check=False)
    if exit_code == 0:
        print_success(f"AWS CLI installed: {stdout.strip()}")
    else:
        print_error("AWS CLI not found. Please install: https://aws.amazon.com/cli/")
        all_ok = False
    
    # Check AWS credentials
    exit_code, _, _ = run_command(["aws", "sts", "get-caller-identity"], check=False)
    if exit_code == 0:
        print_success("AWS credentials configured")
    else:
        print_error("AWS credentials not configured. Run: aws configure")
        all_ok = False
    
    # Check Python version
    if sys.version_info >= (3, 10):
        print_success(f"Python {sys.version_info.major}.{sys.version_info.minor} installed")
    else:
        print_error("Python 3.10 or higher required")
        all_ok = False
    
    # Check if source code exists
    if Path("agentcore_deploy/mcp_proxy/main.py").exists():
        print_success("Source code found")
    else:
        print_error("Source code not found at agentcore_deploy/mcp_proxy/main.py")
        all_ok = False
    
    # Check if requirements.txt exists
    if Path("agentcore_deploy/requirements.txt").exists():
        print_success("requirements.txt found")
    else:
        print_error("agentcore_deploy/requirements.txt not found")
        all_ok = False
    
    return all_ok


def get_aws_account_id() -> Optional[str]:
    """
    Get the current AWS account ID.
    
    Returns:
        AWS account ID or None if failed
    """
    exit_code, stdout, stderr = run_command(
        ["aws", "sts", "get-caller-identity", "--query", "Account", "--output", "text"],
        check=False
    )
    
    if exit_code == 0:
        return stdout.strip()
    else:
        print_error(f"Failed to get AWS account ID: {stderr}")
        return None


def create_iam_role(
    role_name: str,
    gateway_api_id: Optional[str] = None,
    account_id: Optional[str] = None,
    region: str = "us-east-1"
) -> Tuple[bool, Optional[str]]:
    """
    Create IAM role with appropriate permissions for AgentCore.
    
    Args:
        role_name: Name of the IAM role to create
        gateway_api_id: Optional API Gateway ID for specific permissions
        account_id: AWS account ID (will be fetched if not provided)
        
    Returns:
        Tuple of (success, role_arn)
    """
    print_header("Creating IAM Role")
    
    # Get account ID if not provided
    if not account_id:
        account_id = get_aws_account_id()
        if not account_id:
            print_error("Failed to get AWS account ID")
            return False, None
    
    # Create trust policy for AgentCore Runtime
    trust_policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "AssumeRolePolicy",
                "Effect": "Allow",
                "Principal": {
                    "Service": "bedrock-agentcore.amazonaws.com"
                },
                "Action": "sts:AssumeRole",
                "Condition": {
                    "StringEquals": {
                        "aws:SourceAccount": account_id
                    },
                    "ArnLike": {
                        "aws:SourceArn": f"arn:aws:bedrock-agentcore:*:{account_id}:*"
                    }
                }
            }
        ]
    }
    
    # Check if role already exists
    exit_code, stdout, _ = run_command(
        ["aws", "iam", "get-role", "--role-name", role_name],
        check=False
    )
    
    if exit_code == 0:
        print_warning(f"IAM role '{role_name}' already exists")
        role_data = json.loads(stdout)
        role_arn = role_data["Role"]["Arn"]
        print_info(f"Using existing role: {role_arn}")
        return True, role_arn
    
    # Create the role
    print_info(f"Creating IAM role: {role_name}")
    
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
        json.dump(trust_policy, f, indent=2)
        f.flush()
        trust_policy_file = f.name
    
    try:
        exit_code, stdout, stderr = run_command(
            ["aws", "iam", "create-role",
             "--role-name", role_name,
             "--assume-role-policy-document", f"file://{trust_policy_file}"],
            check=False
        )
        
        if exit_code != 0:
            print_error(f"Failed to create IAM role: {stderr}")
            return False, None
        
        role_data = json.loads(stdout)
        role_arn = role_data["Role"]["Arn"]
        print_success(f"Created IAM role: {role_arn}")
        
    finally:
        os.unlink(trust_policy_file)
    
    # Create and attach policy
    print_info("Attaching permissions policy")
    
    # Build resource ARNs
    if gateway_api_id:
        api_resources = [
            f"arn:aws:execute-api:*:*:{gateway_api_id}/*/POST/tools/*",
            f"arn:aws:execute-api:*:*:{gateway_api_id}/*/GET/tools"
        ]
    else:
        # Wildcard if no specific API ID provided
        api_resources = [
            "arn:aws:execute-api:*:*:*/*/POST/tools/*",
            "arn:aws:execute-api:*:*:*/*/GET/tools"
        ]
    
    gateway_policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "GatewayAPIAccess",
                "Effect": "Allow",
                "Action": ["execute-api:Invoke"],
                "Resource": api_resources
            },
            {
                "Sid": "CloudWatchLogsAccess",
                "Effect": "Allow",
                "Action": [
                    "logs:CreateLogGroup",
                    "logs:CreateLogStream",
                    "logs:PutLogEvents",
                    "logs:DescribeLogStreams",
                    "logs:DescribeLogGroups"
                ],
                "Resource": [
                    f"arn:aws:logs:*:{account_id}:log-group:/aws/bedrock-agentcore/runtimes/*",
                    f"arn:aws:logs:*:{account_id}:log-group:*"
                ]
            },
            {
                "Sid": "ECRAuthToken",
                "Effect": "Allow",
                "Action": [
                    "ecr:GetAuthorizationToken"
                ],
                "Resource": "*"
            },
            {
                "Sid": "ECRImagePull",
                "Effect": "Allow",
                "Action": [
                    "ecr:BatchGetImage",
                    "ecr:GetDownloadUrlForLayer"
                ],
                "Resource": f"arn:aws:ecr:*:{account_id}:repository/*"
            },
            {
                "Sid": "XRayAccess",
                "Effect": "Allow",
                "Action": [
                    "xray:PutTraceSegments",
                    "xray:PutTelemetryRecords",
                    "xray:GetSamplingRules",
                    "xray:GetSamplingTargets"
                ],
                "Resource": "*"
            },
            {
                "Sid": "CloudWatchMetrics",
                "Effect": "Allow",
                "Action": "cloudwatch:PutMetricData",
                "Resource": "*",
                "Condition": {
                    "StringEquals": {
                        "cloudwatch:namespace": "bedrock-agentcore"
                    }
                }
            },
            {
                "Sid": "BedrockModelInvocation",
                "Effect": "Allow",
                "Action": [
                    "bedrock:InvokeModel",
                    "bedrock:InvokeModelWithResponseStream"
                ],
                "Resource": [
                    f"arn:aws:bedrock:{region}::foundation-model/*"
                ]
            }
        ]
    }
    
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
        json.dump(gateway_policy, f, indent=2)
        f.flush()
        policy_file = f.name
    
    try:
        exit_code, _, stderr = run_command(
            ["aws", "iam", "put-role-policy",
             "--role-name", role_name,
             "--policy-name", "GatewayAccessPolicy",
             "--policy-document", f"file://{policy_file}"],
            check=False
        )
        
        if exit_code != 0:
            print_error(f"Failed to attach policy: {stderr}")
            return False, role_arn
        
        print_success("Attached permissions policy")
        
    finally:
        os.unlink(policy_file)
    
    # Wait for role to be available
    print_info("Waiting for IAM role to propagate...")
    time.sleep(10) # nosemgrep: arbitrary-sleep — intentional wait for IAM eventual consistency (IAM role to propagate before it can be assumed)
    
    return True, role_arn






def select_auth_mode() -> Tuple[str, Optional[str], Optional[str], Optional[str], Optional[str]]:
    """Prompt user to select auth mode and provide Cognito details if JWT.

    Returns:
        Tuple of (auth_mode, user_pool_id, client_id, client_secret, cognito_domain)
    """
    print("\nSelect authentication mode for gateway:")
    print("  [1] IAM (default) - SigV4 signing with IAM role")
    print("  [2] JWT - OAuth client credentials via Cognito")
    choice = input("\nEnter choice [1]: ").strip()

    if choice == "2":
        print()
        user_pool_id = input("Cognito User Pool ID: ").strip()
        client_id = input("Cognito App Client ID: ").strip()
        client_secret = getpass.getpass("Cognito App Client Secret: ").strip()
        cognito_domain = input("Cognito Domain Prefix: ").strip()
        return "jwt", user_pool_id, client_id, client_secret, cognito_domain

    return "iam", None, None, None, None


def deploy_to_agentcore(
    role_arn: str,
    agent_name: str,
    region: str,
    gateway_endpoint: str,
    auth_mode: str = "iam",
    cognito_user_pool_id: Optional[str] = None,
    cognito_client_id: Optional[str] = None,
    cognito_client_secret: Optional[str] = None,
    cognito_domain: Optional[str] = None,
) -> bool:
    """
    Deploy the proxy to AgentCore Runtime.
    
    Runs `agentcore configure` interactively (so the user can choose
    IAM vs OAuth, ECR repo, etc.) then `agentcore launch`.
    
    Args:
        role_arn: ARN of the IAM execution role
        agent_name: Name for the agent
        region: AWS region for deployment
        gateway_endpoint: Gateway endpoint URL for environment variable
        
    Returns:
        True if successful, False otherwise
    """
    print_header("Deploying to AgentCore Runtime")
    
    # Check if agentcore CLI is available
    exit_code, _, _ = run_command(["agentcore", "--help"], check=False)
    if exit_code != 0:
        print_error("AgentCore CLI not found. Please install it first.")
        print_info("Installation: pip install agentcore")
        return False
    
    print_success("AgentCore CLI found")
    
    # The proxy code lives in agentcore_deploy/ already
    deploy_dir = Path("agentcore_deploy")
    if not deploy_dir.exists():
        print_error("agentcore_deploy/ directory not found")
        return False
    
    print_success("Deployment directory ready")
    
    # Configure and launch from the deployment directory
    original_dir = os.getcwd()
    
    try:
        os.chdir(deploy_dir)
        
        print_info(f"Configuring agent '{agent_name}'...")
        configure_command = [
            "agentcore", "configure",
            "--name", agent_name,
            "--entrypoint", "mcp_proxy/main.py",
            "--execution-role", role_arn,
            "--region", region,
            "--protocol", "MCP",
            "--requirements-file", "requirements.txt",
            "--verbose"  # Add verbose flag to see what's happening
        ]
        
        print_info(f"Running: {' '.join(configure_command)}")
        print_info(f"Working directory: {os.getcwd()}")
        print_warning("This may take several minutes while dependencies are installed...")
        print_info("Please respond to the interactive prompts below.")
        print()
        
        # Run interactively so the user can choose ECR repo and IAM vs OAuth
        exit_code, _, _ = run_command(
            configure_command,
            interactive=True,
            check=False
        )
        
        print()
        
        if exit_code != 0:
            print_error(f"Configuration failed with exit code {exit_code}")
            return False
        
        print_success(f"Agent '{agent_name}' configured")
        
        # Step 2b: Launch (deploy) the agent
        # IMPORTANT: Must run in same directory as configure (where .bedrock_agentcore.yaml is)
        print_info(f"Launching agent '{agent_name}' to AgentCore Runtime...")
        
        # Environment variables for the proxy's outbound connection to the gateway
        env_vars = [
            f"GATEWAY_ENDPOINT={gateway_endpoint}",
            f"AUTH_MODE={auth_mode}",
        ]
        if auth_mode == "jwt":
            env_vars.extend([
                f"COGNITO_USER_POOL_ID={cognito_user_pool_id}",
                f"COGNITO_CLIENT_ID={cognito_client_id}",
                f"COGNITO_CLIENT_SECRET={cognito_client_secret}",
                f"COGNITO_DOMAIN={cognito_domain}",
            ])
        
        launch_command = [
            "agentcore", "launch",
            "--agent", agent_name
        ]
        
        # Add environment variables
        for env_var in env_vars:
            launch_command.extend(["--env", env_var])
        
        print_info(f"Running: {' '.join(launch_command)}")
        print_info(f"Working directory: {os.getcwd()}")
        print_info("If the command prompts for input, please respond in the terminal.")
        print()
        
        # Use interactive mode to allow user input if needed
        exit_code, stdout, stderr = run_command(
            launch_command,
            interactive=True,  # Allow interactive prompts
            check=False
        )
        
        print()  # Add blank line after interactive command
        
        if exit_code != 0:
            print_error(f"Launch failed with exit code {exit_code}")
            if stderr:
                print_error(f"Error: {stderr}")
            if stdout:
                print_info("Output: " + stdout)
            return False
        
        print_success(f"Agent '{agent_name}' launched successfully!")
        if stdout:
            print_info("Launch output:")
            print(stdout)
        
        return True
        
    finally:
        # Always return to original directory
        os.chdir(original_dir)


def verify_deployment(agent_name: str, region: str) -> bool:
    """
    Verify the deployment was successful.
    
    Args:
        agent_name: Name of the deployed agent
        region: AWS region where deployed
        
    Returns:
        True if verification successful, False otherwise
    """
    print_header("Verifying Deployment")
    
    # Check agent status
    print_info("Checking agent status...")
    exit_code, stdout, stderr = run_command(
        ["agentcore", "status", "--agent", agent_name],
        check=False
    )
    
    if exit_code != 0:
        print_error(f"Failed to get agent status: {stderr}")
        print_warning("Agent may still be deploying. Check status manually:")
        print(f"   agentcore status --agent {agent_name}")
        return False
    
    print_success("Agent status retrieved")
    print(stdout)
    
    print()
    print_info("To invoke the agent:")
    print(f"   agentcore invoke --agent {agent_name}")
    print()
    print_info("To view agent configuration:")
    print(f"   agentcore configure list")
    
    return True




def main() -> int:
    """Main entry point for the automated deployment script."""
    parser = argparse.ArgumentParser(
        description="Automated deployment of MCP Proxy Server to AgentCore Runtime",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Configuration is read from deploy_config.json. All values can be
overridden via CLI flags.

Examples:
  # Deploy using deploy_config.json defaults
  python3 setup_and_deploy.py

  # Deploy with a different config file
  python3 setup_and_deploy.py --config my_config.json

  # Override a single value
  python3 setup_and_deploy.py --agent-name my_proxy_v2
        """
    )
    
    parser.add_argument(
        "--config",
        default="deploy_config.json",
        help="Path to configuration file (default: deploy_config.json)"
    )
    parser.add_argument("--agent-name", help="Name for the AgentCore agent")
    parser.add_argument("--gateway-endpoint", help="URL of the AgentCore Gateway")
    parser.add_argument("--iam-role-name", help="Name for the IAM role")
    parser.add_argument("--region", help="AWS region for deployment")
    parser.add_argument("--gateway-api-id", help="Specific API Gateway ID for IAM permissions")
    parser.add_argument("--auth-mode", choices=["iam", "jwt"], help="Authentication mode for gateway (default: iam)")
    parser.add_argument("--cognito-user-pool-id", help="Cognito User Pool ID (required for jwt mode)")
    parser.add_argument("--cognito-client-id", help="Cognito App Client ID (required for jwt mode)")
    parser.add_argument("--cognito-client-secret", help="Cognito App Client Secret (required for jwt mode)")
    parser.add_argument("--cognito-domain", help="Cognito Domain Prefix (required for jwt mode)")
    
    args = parser.parse_args()
    
    # Load config file
    config_path = Path(args.config)
    if not config_path.exists():
        print_error(f"Config file not found: {config_path}")
        print_info("Create one from the template: cp deploy_config.json my_config.json")
        return 1
    
    with open(config_path, encoding="utf-8") as f:
        config = json.load(f)
    
    # CLI flags override config file values
    agent_name = args.agent_name or config.get("agent_name", "mcp-proxy-server")
    gateway_endpoint = args.gateway_endpoint or config.get("gateway_endpoint")
    iam_role_name = args.iam_role_name or config.get("iam_role_name", "MCPProxyServerRole")
    region = args.region or config.get("region", "us-east-1")
    gateway_api_id = args.gateway_api_id or config.get("gateway_api_id")

    # Auth mode: CLI flag > config file > interactive prompt
    auth_mode = args.auth_mode or config.get("auth_mode")
    cognito_user_pool_id = args.cognito_user_pool_id or config.get("cognito_user_pool_id")
    cognito_client_id = args.cognito_client_id or config.get("cognito_client_id")
    cognito_client_secret = args.cognito_client_secret or config.get("cognito_client_secret")
    cognito_domain = args.cognito_domain or config.get("cognito_domain")

    if not auth_mode:
        auth_mode, cognito_user_pool_id, cognito_client_id, cognito_client_secret, cognito_domain = select_auth_mode()
    elif auth_mode == "jwt" and not all([cognito_user_pool_id, cognito_client_id, cognito_client_secret]):
        # JWT selected via CLI/config but missing Cognito params — prompt for them
        if not cognito_user_pool_id:
            cognito_user_pool_id = input("Cognito User Pool ID: ").strip()
        if not cognito_client_id:
            cognito_client_id = input("Cognito App Client ID: ").strip()
        if not cognito_client_secret:
            cognito_client_secret = getpass.getpass("Cognito App Client Secret: ").strip()
        if not cognito_domain:
            cognito_domain = input("Cognito Domain Prefix: ").strip()

    # Persist auth config (never write secrets to disk)
    config["auth_mode"] = auth_mode
    if auth_mode == "jwt":
        config["cognito_user_pool_id"] = cognito_user_pool_id
        config["cognito_client_id"] = cognito_client_id
        config["cognito_domain"] = cognito_domain
        # cognito_client_secret is intentionally NOT persisted — pass it
        # via --cognito-client-secret or enter it interactively each time
    config.pop("cognito_client_secret", None)
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)

    if not gateway_endpoint:
        print_error("gateway_endpoint is required (set it in deploy_config.json or pass --gateway-endpoint)")
        return 1
    
    print_header("MCP Proxy Server - Automated Deployment to AgentCore")
    print_info(f"Config file: {config_path}")
    print_info(f"Agent Name: {agent_name}")
    print_info(f"Gateway Endpoint: {gateway_endpoint}")
    print_info(f"IAM Role Name: {iam_role_name}")
    print_info(f"Region: {region}")
    print_info(f"Auth Mode: {auth_mode}")
    
    try:
        # Step 1: Check prerequisites
        if not check_prerequisites():
            print_error("Prerequisites check failed")
            return 1
        
        # Step 2: Get AWS account ID
        account_id = get_aws_account_id()
        if not account_id:
            print_error("Failed to get AWS account ID")
            return 1
        print_success(f"AWS Account ID: {account_id}")
        
        # Step 3: Create IAM role (if it doesn't exist)
        success, role_arn = create_iam_role(iam_role_name, gateway_api_id, account_id, region)
        if not success or not role_arn:
            print_error("Failed to create IAM role")
            return 1
        
        # Step 4: Deploy to AgentCore Runtime (interactive configure + launch)
        if not deploy_to_agentcore(role_arn, agent_name, region, gateway_endpoint,
                                   auth_mode, cognito_user_pool_id, cognito_client_id,
                                   cognito_client_secret, cognito_domain):
            print_error("Deployment failed")
            return 1
        
        # Step 5: Verify deployment
        if not verify_deployment(agent_name, region):
            print_warning("Verification incomplete - please check manually")
        
        # Success!
        print_header("Deployment Complete!")
        print_success(f"MCP Proxy Server deployed to AgentCore as '{agent_name}'")
        print()
        print_info("Next steps:")
        print(f"1. Check agent status: agentcore status --agent {agent_name}")
        print(f"2. Update the ARN in test_agent.py")
        print(f"3. Run: python3 test_agent.py")
        
        return 0
        
    except KeyboardInterrupt:
        print()
        print_warning("Deployment interrupted by user")
        return 130
    except Exception as e:
        print_error(f"Unexpected error: {e}")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
