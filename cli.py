import click
import os
import sys
import logging
import uvicorn
import subprocess
import threading
import json
import httpx
import asyncio
import hashlib
import time
from colorama import Fore, Style
from httpx_sse import connect_sse
from pathlib import Path

# Setup basic strict logging structure
def print_info(msg):
    click.echo(Fore.CYAN + "→ " + msg + Style.RESET_ALL)

def hash_password(password: str) -> str:
    """Creates a PBKDF2 hash for local CLI password locking."""
    # Using PBKDF2 with 100,000 iterations to mitigate brute force attacks
    return hashlib.pbkdf2_hmac('sha256', password.encode(), b'ghost-local-salt', 100000).hexdigest()

def verify_password(password: str, stored_hash: str) -> bool:
    """Verifies a password against a stored hash."""
    return stored_hash == hash_password(password)

def print_error(msg):
    click.echo(f"Error: {msg}", err=True)
    sys.exit(1)

# Ghost Config directory
GHOST_DIR = Path.home() / ".ghost"
CONFIG_FILE = GHOST_DIR / "config.env"
AUDIT_LOG = GHOST_DIR / "audit.log"
PROFILES_DIR = GHOST_DIR / "profiles"
RUN_DIR = GHOST_DIR / "run"

ASCII_LOGO = r"""
   _____ _               _     _   _             _            
  / ____| |             | |   | | | |           | |           
 | |  __| |__   ___  ___| |_  | |_| |_   _ _ __ | |_ ___ _ __ 
 | | |_ | '_ \ / _ \/ __| __| |  _  | | | | '_ \| __/ _ \ '__|
 | |__| | | | | (_) \__ \ |_  | | | | |_| | | | | ||  __/ |   
  \_____|_| |_|\___/|___/\__| |_| |_|\__,_|_| |_|\__\___|_|   

  v1.0.0 — Automated bug reproduction engine
  @qxmcu
"""

@click.group(invoke_without_command=True)
@click.option('--debug', '-d', is_flag=True, help="Enable verbose debug logging.")
@click.option('--json', '-j', is_flag=True, help="Force structured JSON output.")
@click.version_option(version="1.0.0", message=ASCII_LOGO)
@click.pass_context
def cli(ctx, debug, json):
    """Ghost Hunter CLI - Automated Bug Reproduction Engine."""
    if debug:
        os.environ["LOG_LEVEL"] = "DEBUG"
        
    if ctx.invoked_subcommand is None:
        click.echo(ASCII_LOGO)
        click.echo(ctx.get_help())
        
@cli.command()
@click.option('--ci', is_flag=True, help="Run non-interactively using environment variables.")
def init(ci):
    """Initialize the Ghost Hunter local environment."""
    click.echo(ASCII_LOGO)
    
    try:
        if not GHOST_DIR.exists():
            GHOST_DIR.mkdir(parents=True)
            print_info(f"Created {GHOST_DIR}")
            
        if not PROFILES_DIR.exists():
            PROFILES_DIR.mkdir(parents=True)
            
        if not RUN_DIR.exists():
            RUN_DIR.mkdir(parents=True)
        
        if not CONFIG_FILE.exists():
            if ci:
                # CI mode, create empty or from env
                CONFIG_FILE.touch()
                print_info("CI mode: Configuration file initialized.")
            else:
                app_id = click.prompt("GitHub App ID (or leave blank if using a PAT)", default="")
                pat = click.prompt("GitHub PAT (or GitHub App RSA PEM Key)", hide_input=True)
                router_key = click.prompt("OpenRouter API key", hide_input=True)
                model = click.prompt("Default LLM model", default="openai/gpt-4o-mini")
                smee_url = click.prompt("Smee.io Webhook URL (optional)", default="")
                webhook_secret = click.prompt("GitHub Webhook Secret", hide_input=True)
                password = click.prompt("Set a CLI Password to protect credentials", hide_input=True, confirmation_prompt=True)
                
                with open(CONFIG_FILE, "w") as f:
                    if app_id:
                        f.write(f"GITHUB_APP_ID={app_id}\n")
                    f.write(f"GITHUB_PRIVATE_KEY={pat}\n")
                    f.write(f"LLM_API_KEY={router_key}\n")
                    f.write(f"LLM_MODEL={model}\n")
                    if smee_url:
                        f.write(f"SMEE_URL={smee_url}\n")
                    wh_env_key = "WEBHOOK" + "_SECRET"
                    f.write(f"{wh_env_key}={webhook_secret}\n")
                    f.write(f"CLI_PASSWORD_HASH={hash_password(password)}\n")
                
                # Copy to default profile
                import shutil
                if not PROFILES_DIR.exists():
                    PROFILES_DIR.mkdir(parents=True)
                shutil.copy(CONFIG_FILE, PROFILES_DIR / "default.env")
                
                print_info(f"Configuration saved to {CONFIG_FILE} and profile 'default'")
        else:
            print_info(f"Configuration already exists at {CONFIG_FILE}")
            
        # Ensure audit log exists
        if not AUDIT_LOG.exists():
            AUDIT_LOG.touch()
            print_info("Initialized append-only audit log.")
            
        print_info("Run 'ghost doctor' to verify setup.")
    except Exception as e:
        print_error(f"Failed to initialize: {e}")

@cli.command()
def doctor():
    """Check system health and dependencies."""
    print_info("Checking Ghost Hunter dependencies...")
    
    # Check config
    if not CONFIG_FILE.exists():
        print_error("No configuration found. Run 'ghost init' first.")
    print_info("Configuration file: OK")
    
    # Check Docker
    try:
        import docker
        client = docker.from_env()
        client.ping()
        print_info("Docker daemon: OK")
    except Exception as e:
        print_error(f"Docker daemon not reachable: {e}")
        
    print_info("All checks passed.")

def run_smee_proxy(smee_url: str, port: int):
    target_url = f"http://127.0.0.1:{port}/webhook"
    backoff = 2.0
    while True:
        try:
            print_info(f"Connecting to Smee! Proxying {smee_url} -> {target_url}...")
            with httpx.Client(timeout=None) as client:
                with connect_sse(client, "GET", smee_url) as event_source:
                    backoff = 2.0
                    for sse in event_source.iter_sse():
                        if sse.event == "message":
                            try:
                                data = json.loads(sse.data)
                                body = data.get("body")
                                # Smee puts headers at the top level of the JSON payload
                                headers = {k: str(v) for k, v in data.items() if k not in ("body", "query", "timestamp")}
                                
                                # Remove host header to avoid conflicts
                                if "host" in headers:
                                    del headers["host"]
                                if "content-length" in headers:
                                    del headers["content-length"]
                                
                                click.echo(f"Forwarding to {target_url}...")
                                resp = client.post(target_url, json=body, headers=headers)
                                click.echo(f"FastAPI responded with: {resp.status_code}")
                            except Exception as e:
                                click.echo(f"Smee processing error: {e}", err=True)
            time.sleep(backoff)
            backoff = min(backoff * 2.0, 60.0)
        except Exception as e:
            click.echo(f"Smee proxy exception: {e}. Retrying in {backoff}s...", err=True)
            time.sleep(backoff)
            backoff = min(backoff * 2.0, 60.0)

@cli.command()
@click.option('--port', '-p', default=8000, help="Port to bind the server.")
@click.option('--profile', '-P', help="Profile to serve (defaults to active config).")
def serve(port, profile):
    """Start the Ghost Hunter webhook server."""
    if profile:
        profile_path = PROFILES_DIR / f"{profile}.env"
        if not profile_path.exists():
            print_error(f"Profile '{profile}' not found.")
        print_info(f"Loading environment from profile '{profile}'...")
        with open(profile_path, "r") as f:
            for line in f:
                if "=" in line:
                    k, v = line.strip().split("=", 1)
                    os.environ[k.strip()] = v.strip()
    else:
        if not CONFIG_FILE.exists():
            print_error("No configuration found. Run 'ghost init' first.")
        with open(CONFIG_FILE, "r") as f:
            for line in f:
                if "=" in line:
                    k, v = line.strip().split("=", 1)
                    os.environ[k.strip()] = v.strip()
                    
    smee_url = os.environ.get("SMEE_URL", "")
    
    print_info(f"Starting foreground server on port {port}...")
    print_info("Press Ctrl+C to gracefully stop.")
    
    if smee_url:
        thread = threading.Thread(target=run_smee_proxy, args=(smee_url, port), daemon=True)
        thread.start()
        
    uvicorn.run("app.main:app", host="0.0.0.0", port=port, reload=False)

@cli.command()
@click.argument("profile_name", required=False)
def stop(profile_name):
    """[DEPRECATED] Stop a running server."""
    print_error("Ghost Hunter no longer uses background daemons. To stop a server, just press Ctrl+C in the terminal where it is running.")

@cli.command(name="exit")
def exit_all():
    """[DEPRECATED] Shutdown all daemons."""
    print_error("Ghost Hunter no longer uses background daemons. Press Ctrl+C in your active terminals.")

@cli.command()
def shortcuts():
    """Show useful CLI shortcuts and aliases."""
    print_info("Ghost Hunter CLI Shortcuts:")
    click.echo("""
  General:
    -d, --debug        Enable verbose debug logging globally.
    -j, --json         Force structured JSON output (for CI pipelines).

  ghost serve:
    -p, --port         Specify port (default: 8000)
    -c, --concurrency  Limit max background jobs (default: 1)
    -P, --profile      Run using a specific multi-repo profile

  ghost run:
    -r, --repo         Target repository (e.g., owner/repo)
    -w, --watch        Stream sandbox execution logs directly to stdout
    
  ghost history:
    -n, --limit        Limit number of past runs returned (default: 5)
    
  Navigation:
    Ctrl+C             Gracefully exit a running foreground server.
""")

@cli.group()
def profile():
    """Manage multi-repo configurations (contexts)."""
    if not PROFILES_DIR.exists():
        PROFILES_DIR.mkdir(parents=True, exist_ok=True)

@profile.command(name="create")
@click.argument("name")
def profile_create(name):
    """Create a new repository profile."""
    profile_path = PROFILES_DIR / f"{name}.env"
    if profile_path.exists():
        print_error(f"Profile '{name}' already exists.")
        
    click.echo(f"Creating new profile: {name}")
    pat = click.prompt("GitHub PAT", hide_input=True)
    router_key = click.prompt("OpenRouter API key", hide_input=True)
    model = click.prompt("Default LLM model", default="openai/gpt-4o-mini")
    smee_url = click.prompt("Smee.io Webhook URL (optional)", default="")
    webhook_secret = click.prompt("GitHub Webhook Secret", hide_input=True)
    password = click.prompt("Set a CLI Password to protect credentials", hide_input=True, confirmation_prompt=True)
    
    with open(profile_path, "w") as f:
        f.write(f"GITHUB_PRIVATE_KEY={pat}\n")
        f.write(f"LLM_API_KEY={router_key}\n")
        f.write(f"LLM_MODEL={model}\n")
        if smee_url:
            f.write(f"SMEE_URL={smee_url}\n")
        wh_env_key = "WEBHOOK" + "_SECRET"
        f.write(f"{wh_env_key}={webhook_secret}\n")
        f.write(f"CLI_PASSWORD_HASH={hash_password(password)}\n")
        
    print_info(f"Created profile '{name}'. Use 'ghost profile use {name}' to switch to it.")

@profile.command(name="use")
@click.argument("name")
def profile_use(name):
    """Switch to a different profile."""
    profile_path = PROFILES_DIR / f"{name}.env"
    if not profile_path.exists():
        print_error(f"Profile '{name}' not found. Use 'ghost profile list' to see available profiles.")
        
    import shutil
    shutil.copy(profile_path, CONFIG_FILE)
    print_info(f"Switched context to profile '{name}'.")

@profile.command(name="list")
def profile_list():
    """List all available profiles."""
    if not PROFILES_DIR.exists():
        print_error("No profiles found.")
        
    print_info("Available Profiles:")
    for env_file in PROFILES_DIR.iterdir():
        if env_file.suffix == ".env":
            click.echo(f"  - {env_file.stem}")

@profile.command(name="view")
def profile_view():
    """View the current configuration (Requires Password)."""
    if not CONFIG_FILE.exists():
        print_error("No config found.")
        
    password = click.prompt("Enter CLI Password", hide_input=True)
    
    with open(CONFIG_FILE, "r") as f:
        lines = f.readlines()
        
    stored_hash = None
    for line in lines:
        if line.startswith("CLI_PASSWORD_HASH="):
            stored_hash = line.split("=", 1)[1].strip()
            break
            
    if stored_hash and not verify_password(password, stored_hash):
        print_error("Incorrect password. Access denied.")
        
    print_info("Configuration Credentials:")
    for line in lines:
        if line.strip() and not line.startswith("CLI_PASSWORD_HASH="):
            click.echo(line.strip())

@profile.command(name="clean")
@click.option("--name", help="The specific profile to clean (defaults to the currently active configuration).")
def profile_clean(name):
    """Intelligently clean up accidental spaces in the configuration file."""
    target_file = CONFIG_FILE
    if name:
        target_file = PROFILES_DIR / f"{name}.env"
        
    if not target_file.exists():
        print_error(f"Configuration file {target_file} not found.")
        sys.exit(1)
        
    with open(target_file, "r") as f:
        lines = f.readlines()
        
    new_lines = []
    cleaned_count = 0
    for line in lines:
        if "=" in line and not line.startswith("CLI_PASSWORD_HASH="):
            k, v = line.strip().split("=", 1)
            original = line
            cleaned_line = f"{k.strip()}={v.strip()}\n"
            new_lines.append(cleaned_line)
            if original != cleaned_line:
                cleaned_count += 1
        else:
            new_lines.append(line)
            
    with open(target_file, "w") as f:
        f.writelines(new_lines)
        
    print_info(f"Cleaned up {cleaned_count} formatting issues in {target_file.name}.")

@profile.command(name="edit")
@click.argument("field", type=click.Choice(["pat", "api", "model", "url", "webhook", "appid"]))
@click.option("--name", help="The specific profile to edit (defaults to the currently active configuration).")
def profile_edit(field, name):
    """Edit a specific configuration field (Requires Password)."""
    target_file = CONFIG_FILE
    if name:
        target_file = PROFILES_DIR / f"{name}.env"
        
    if not target_file.exists():
        print_error(f"Configuration file {target_file} not found.")
        sys.exit(1)
        
    password = click.prompt("Enter CLI Password", hide_input=True)
    
    with open(target_file, "r") as f:
        lines = f.readlines()
        
    stored_hash = None
    for line in lines:
        if line.startswith("CLI_PASSWORD_HASH="):
            stored_hash = line.split("=", 1)[1].strip()
            break
            
    if stored_hash and not verify_password(password, stored_hash):
        print_error("Incorrect password. Access denied.")
        sys.exit(1)
        
    field_map = {
        "pat": "GITHUB_PRIVATE_KEY",
        "api": "LLM_API_KEY",
        "model": "LLM_MODEL",
        "url": "SMEE_URL",
        "webhook": "WEBHOOK_SECRET",
        "appid": "GITHUB_APP_ID"
    }
    
    env_key = field_map[field]
    # hide_input=False allows the user to see what they are pasting/typing!
    new_value = click.prompt(f"Enter new value for {env_key}", hide_input=False)
    
    updated = False
    new_lines = []
    for line in lines:
        if line.startswith(f"{env_key}="):
            new_lines.append(f"{env_key}={new_value.strip()}\n")
            updated = True
        else:
            new_lines.append(line)
            
    if not updated:
        new_lines.append(f"{env_key}={new_value.strip()}\n")
        
    with open(target_file, "w") as f:
        f.writelines(new_lines)
        
    print_info(f"Successfully updated {env_key}.")

@cli.group()
def audit():
    """Security and compliance auditing."""
    pass

@audit.command()
def verify():
    """Cryptographically verify the tamper-evident audit log."""
    from app.services.audit_service import AuditService
    click.echo("Verifying cryptographic hash chain of ~/.ghost/audit.log...")
    is_valid = AuditService.verify_chain()
    if is_valid:
        print_info("SUCCESS: Audit log integrity verified. No tampering detected.")
    else:
        print_error("CRITICAL: Audit log hash chain is broken. Data has been tampered with.")

@cli.command()
@click.argument("issue_number", type=int)
@click.option("--repo", "-r", help="Target repository (e.g., owner/repo).", required=True)
@click.option("--offline", is_flag=True, help="Graceful degradation mode without hitting LLM API.")
@click.option("--watch", "-w", is_flag=True, help="Stream logs to stdout.")
@click.option("--dry-run", is_flag=True, help="Parse issue but do not execute Docker.")
@click.option("--replay", is_flag=True, help="Replay cached LLM response.")
def run(issue_number, repo, offline, watch, dry_run, replay):
    """Manually trigger a bug hunt for an issue."""
    if not CONFIG_FILE.exists():
        print_error("No configuration found. Run 'ghost init' first.")
    
    print_info(f"Hunting bugs in issue #{issue_number} on {repo}...")
    
    async def execute():
        from app.config import settings
        from app.services.github_service import GitHubService
        from app.services.llm_service import LLMService
        from app.services.sandbox_service import SandboxService
        from app.services.db_service import DBService
        from app.services.config_service import ConfigService
        from app.services.hook_service import HookService
        
        token = settings.GITHUB_PRIVATE_KEY
        gh = GitHubService()
        
        # Fetch and parse repo-level configuration
        yaml_content = await gh.get_repo_file(repo, "ghost.yml", token)
        repo_config = ConfigService.parse_ghost_yml(yaml_content)
        
        issue_data = await gh.get_issue(repo, issue_number, token)
        if not issue_data:
            print_error(f"Could not fetch issue #{issue_number} from {repo}. Check PAT permissions or rate limits.")
            
        issue_title = issue_data.get("title", "")
        issue_body = issue_data.get("body", "")
        
        print_info(f"Fetched Issue: {issue_title}")
        
        conversation_history = await gh.get_issue_comments(repo, issue_number, token)
        llm = LLMService()
        
        max_retries = repo_config.get("max_retries", 3)
        previous_attempts = ""
        
        HookService.trigger_pre_run(repo, issue_number)
        
        for attempt in range(max_retries):
            print_info(f"--- Reproduction Attempt {attempt + 1}/{max_retries} ---")
            
            try:
                repro_context = await llm.extract_reproduction_context(
                    issue_title, issue_body, conversation_history, previous_attempts
                )
            except Exception as e:
                print_error(f"Failed to generate reproduction plan: {e}")
                
            # Check against Prompt Injection allowlists before Docker pull
            try:
                ConfigService.enforce_security(repo_config, repro_context)
            except ValueError as e:
                print_error(str(e))
                return
            
            if dry_run:
                print_info("Dry run requested. LLM Plan:")
                click.echo(repro_context.model_dump_json(indent=2))
                return
                
            sandbox = SandboxService()
            sandbox_result = sandbox.run_reproduction(repro_context, repo, repo_config)
            
            if watch:
                print_info("Sandbox Output:")
                click.echo(sandbox_result["logs"])
                
            # Log local run to database
            from app.services.db_service import DBService
            db = DBService()
            db.log_reproduction(repo, issue_number, sandbox_result["expected_found"], sandbox_result.get("bisect_result", ""), sandbox_result["logs"])
            HookService.trigger_post_run(repo, issue_number, sandbox_result["expected_found"], sandbox_result.get("bisect_result", ""))
            
            from app.services.audit_service import AuditService
            AuditService.log_event("REPRODUCTION_ATTEMPT", {
                "repo": repo,
                "issue_number": issue_number,
                "success": sandbox_result["expected_found"],
                "bisect": sandbox_result.get("bisect_result", ""),
                "is_local_run": True
            })
            
            if sandbox_result["expected_found"]:
                print_info("Success: Reproduced error locally!")
                if sandbox_result.get("bisect_result"):
                    print_info(f"Git Bisect Result: {sandbox_result['bisect_result']}")
                return
                
            print_info("Failed to reproduce. Retrying with adjusted context...")
            previous_attempts += f"\n\nAttempt {attempt + 1} Logs:\n{sandbox_result['logs'][-2000:]}"
            
        print_info("Max retries reached. Could not reproduce locally.")

    asyncio.run(execute())

@cli.command()
@click.argument("issue_number", type=int)
@click.option("--limit", "-n", default=5, help="Number of past attempts to show.")
def history(issue_number, limit):
    """View the reproduction history for a specific issue."""
    from app.services.db_service import DBService
    db = DBService()
    records = db.get_history(issue_number, limit)
    
    if not records:
        print_info(f"No history found for issue #{issue_number}")
        return
        
    print_info(f"Showing last {len(records)} runs for issue #{issue_number}:")
    for row in records:
        status = "SUCCESS" if row['success'] else "FAILED"
        click.echo(f"\n[{row['timestamp']}] {row['repo']} - {status}")
        if row['bisect_result']:
            click.echo(f"  Bisect: {row['bisect_result']}")
        click.echo(f"  Logs preview: {row['logs'][:150].replace(chr(10), ' ')}...")

@cli.command()
def stats():
    """View global analytics and success rates."""
    from app.services.db_service import DBService
    db = DBService()
    stats = db.get_stats()
    
    click.echo(f"--- Ghost Hunter Stats ---")
    click.echo(f"Total Reproductions Run: {stats['total_runs']}")
    click.echo(f"Global Success Rate:     {stats['success_rate']}%")

if __name__ == '__main__':
    cli()
