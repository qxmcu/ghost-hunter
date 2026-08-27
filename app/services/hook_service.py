import os
import subprocess
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

HOOKS_DIR = Path.home() / ".ghost" / "hooks"

class HookService:
    @staticmethod
    def _execute_hook(hook_name: str, env_vars: dict):
        """
        Executes an executable hook script in the ~/.ghost/hooks directory if it exists.
        Passes the context via environment variables.
        """
        if not HOOKS_DIR.exists():
            HOOKS_DIR.mkdir(parents=True, exist_ok=True)
            return

        # On Windows, people might name scripts .bat, .cmd, or .ps1.
        # On Linux/Mac, they might not have an extension.
        # We will scan the directory for any file that starts with `hook_name` and is executable.
        
        candidates = []
        for file_path in HOOKS_DIR.iterdir():
            if file_path.is_file() and file_path.stem == hook_name:
                candidates.append(file_path)

        for hook_file in candidates:
            # Check if it's executable (on Linux/Mac) or a Windows script
            is_executable = os.access(hook_file, os.X_OK)
            is_windows_script = hook_file.suffix in [".bat", ".cmd", ".ps1", ".exe"]
            
            if is_executable or is_windows_script:
                logger.info(f"Executing hook: {hook_file.name}")
                
                # Merge current environment with our custom Ghost context
                merged_env = os.environ.copy()
                for k, v in env_vars.items():
                    merged_env[k] = str(v)
                    
                try:
                    cmd = [str(hook_file)]
                    if hook_file.suffix == ".ps1":
                        cmd = ["powershell", "-ExecutionPolicy", "Bypass", "-File", str(hook_file)]
                        
                    subprocess.Popen(
                        cmd,
                        env=merged_env,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        cwd=str(HOOKS_DIR)
                    )
                except Exception as e:
                    logger.error(f"Failed to execute hook {hook_file.name}: {e}")

    @staticmethod
    def trigger_pre_run(repo: str, issue_number: int):
        HookService._execute_hook("pre-run", {
            "GHOST_REPO": repo,
            "GHOST_ISSUE": issue_number,
            "GHOST_PHASE": "pre-run"
        })

    @staticmethod
    def trigger_post_run(repo: str, issue_number: int, success: bool, bisect_result: str):
        HookService._execute_hook("post-run", {
            "GHOST_REPO": repo,
            "GHOST_ISSUE": issue_number,
            "GHOST_SUCCESS": "true" if success else "false",
            "GHOST_BISECT": bisect_result,
            "GHOST_PHASE": "post-run"
        })
