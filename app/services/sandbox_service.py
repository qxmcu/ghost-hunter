import logging
import docker
import base64
import concurrent.futures
from docker.errors import DockerException
from app.schemas import ReproductionContext

logger = logging.getLogger(__name__)


class FatalSandboxException(Exception):
    """Raised when sandbox encounters a persistent setup/environment failure."""
    pass


class SandboxService:
    def __init__(self):
        try:
            self.client = docker.from_env()
        except DockerException as e:
            logger.critical(f"Failed to initialize Docker SDK: {e}")
            self.client = None
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=10)

    def __del__(self):
        if hasattr(self, "_executor"):
            self._executor.shutdown(wait=False)

    def _exec_run_with_timeout(self, container, cmd, workdir, timeout_sec):
        future = self._executor.submit(container.exec_run, cmd, workdir=workdir)
        try:
            return future.result(timeout=timeout_sec)
        except concurrent.futures.TimeoutError as e:
            logger.error(f"Host-side timeout reached during container.exec_run for command: {cmd}")
            raise TimeoutError(f"Host-side timeout of {timeout_sec}s reached during container execution.") from e

    def run_reproduction(self, context: ReproductionContext, repo_full_name: str, repo_config: dict = None) -> dict:
        """
        Executes a secure two-stage container run.
        Stage 1: Network enabled, pulls repo and installs dependencies.
        Stage 2: Network isolated (air-gapped), runs untrusted reproduction code.
        Stage 3: Automated Git Bisect if the bug is reproduced and a good commit is known.
        """
        if self.client is None:
            return {
                "logs": "Sandbox Exception: Docker daemon is unreachable or not running.",
                "expected_found": False,
                "bisect_result": ""
            }

        logger.info(f"Starting sandbox reproduction for {repo_full_name}")
        container = None
        logs = ""
        bisect_result = ""
        expected_found = False

        try:
            # Pre-pull image
            try:
                self.client.images.get(context.base_image)
            except docker.errors.ImageNotFound:
                self.client.images.pull(context.base_image)

            # Apply config constraints
            repo_config = repo_config or {}
            limits = repo_config.get("resource_limits", {"cpus": 1024, "memory": "2g"})
            timeout_sec = str(repo_config.get("timeout", 60))

            # Start container in sleep mode for Exec orchestration
            logger.info("Spinning up background sandbox container...")
            container = self.client.containers.run(
                image=context.base_image,
                command=["sleep", "infinity"],
                environment=context.env_vars,
                detach=True,
                mem_limit=limits.get("memory", "2g"),
                cpu_shares=int(limits.get("cpus", 1024)),
                network_mode="bridge",
                working_dir="/workspace",
                remove=False,
                cap_add=["NET_ADMIN"],
                dns=["8.8.8.8", "8.8.4.4"],
            )

            # --- STAGE 1: SETUP (Network Enabled) ---
            logger.info("Stage 1: Cloning repository and installing dependencies...")
            
            # Force non-interactive apt-get to prevent hanging on tzdata and others
            install_cmds = ["export DEBIAN_FRONTEND=noninteractive", "apt-get update && apt-get install -yq git iproute2 libcap2-bin"]
            
            # Prevent DNS Rebinding SSRF by blackholing cloud metadata and RFC1918 networks
            install_cmds.append("ip route add blackhole 169.254.169.254/32 || true")
            install_cmds.append("ip route add blackhole 10.0.0.0/8 || true")
            install_cmds.append("ip route add blackhole 172.16.0.0/12 || true")
            install_cmds.append("ip route add blackhole 192.168.0.0/16 || true")
            
            if context.required_packages:
                install_cmds.append(f"apt-get install -yq {' '.join(context.required_packages)}")
            
            # Auto-Clone feature. GIT_TERMINAL_PROMPT=0 prevents hanging if the repo asks for a password!
            install_cmds.append(f"GIT_TERMINAL_PROMPT=0 git clone https://github.com/{repo_full_name}.git .")
            
            setup_script = "set -e\n" + "\n".join(install_cmds)
            
            # Wrap Stage 1 in timeout to prevent infinite hangs during setup
            exit_code, output = self._exec_run_with_timeout(
                container, ["timeout", "300", "/bin/sh", "-c", setup_script], "/workspace", 320
            )
            output_str = output.decode('utf-8', errors='replace')
            logs += f"--- STAGE 1: SETUP ---\n{output_str}\n"

            if exit_code != 0:
                logger.error("Stage 1 Setup failed.")
                is_persistent = any(
                    err in output_str.lower()
                    for err in ["not found", "could not resolve host", "fatal: repository", "permission denied", "unable to access"]
                ) or exit_code == 128
                if is_persistent:
                    raise FatalSandboxException(f"Persistent environment/setup failure in Stage 1. Output:\n{output_str}")
                return {"logs": logs, "expected_found": False, "bisect_result": ""}

            # --- STAGE 2: EXECUTION (Air-Gapped Optional) ---
            network_ids = []
            if not context.requires_network:
                logger.info("Stage 2: Disconnecting network for secure air-gapped execution...")
                for net_name, network_info in container.attrs['NetworkSettings']['Networks'].items():
                    net_id = network_info['NetworkID']
                    network_ids.append(net_id)
                    self.client.networks.get(net_id).disconnect(container)
            
            logger.info("Stage 2: Running reproduction commands...")
            # We use set +e so we can capture the actual crash traceback without the shell exiting silently
            repro_script = "\n".join(context.reproduction_commands)
            # CRITICAL FIX: Wrap in 'timeout' to prevent malicious or accidental infinite loops from hanging
            # SECONDARY FIX: Drop NET_ADMIN via capsh so the payload cannot delete the blackhole routes
            host_timeout = float(timeout_sec) + 20.0
            
            # Use capsh if available, fallback to direct execution (alpine, etc) if not
            wrapper_cmd = ["sh", "-c", f"if command -v capsh >/dev/null; then capsh --drop=cap_net_admin -- -c 'timeout {timeout_sec} /bin/sh -c \"$1\"'; else timeout {timeout_sec} /bin/sh -c \"$1\"; fi", "--", repro_script]
            
            exit_code, output = self._exec_run_with_timeout(
                container, wrapper_cmd, "/workspace", host_timeout
            )
            repro_logs = output.decode("utf-8", errors="replace")
            logs += f"\n--- STAGE 2: EXECUTION ---\n{repro_logs}\n"

            # Check expected error keywords
            if context.expected_error_keywords and repro_logs:
                for kw in context.expected_error_keywords:
                    if kw.lower() in repro_logs.lower():
                        expected_found = True
                        break

            # --- STAGE 3: GIT BISECT ---
            if expected_found and context.known_good_commit:
                logger.info(f"Bug reproduced! Starting Git Bisect against {context.known_good_commit}...")
                
                # Re-enable network for bisect checking out packages
                if not context.requires_network:
                    for net_id in network_ids:
                        self.client.networks.get(net_id).connect(container)
                
                # CRITICAL FIX: Use base64 to write files to avoid Nested Heredoc Bash Injection!
                # If the LLM generated script contains an 'EOF', it would prematurely terminate our outer heredoc.
                # Base64 bypasses all shell parsing entirely.
                b64_repro = base64.b64encode(repro_script.encode()).decode()
                
                # Write keywords to a file using base64 and use grep -F (Fixed Strings) to avoid Regex Injection!
                patterns = "\n".join(context.expected_error_keywords)
                b64_patterns = base64.b64encode(patterns.encode()).decode()
                
                bisect_script = (
                    f"git bisect start HEAD {context.known_good_commit}\n"
                    f"echo {b64_repro} | base64 -d > bisect_test.sh\n"
                    f"echo {b64_patterns} | base64 -d > patterns.txt\n"
                    f"chmod +x bisect_test.sh\n"
                    # Exit 1 (BAD) if grep finds the error, Exit 0 (GOOD) if it doesn't
                    f"git bisect run sh -c 'timeout 30 ./bisect_test.sh 2>&1 | grep -i -F -f patterns.txt; if [ $? -eq 0 ]; then exit 1; else exit 0; fi'\n"
                )
                
                exit_code, output = self._exec_run_with_timeout(
                    container, ["timeout", "300", "/bin/sh", "-c", bisect_script], "/workspace", 320
                )
                bisect_logs = output.decode("utf-8", errors="replace")
                logs += f"\n--- STAGE 3: GIT BISECT ---\n{bisect_logs}\n"
                
                # Extract the culprit commit from logs
                for line in bisect_logs.split("\n"):
                    if "is the first bad commit" in line:
                        bisect_result = line.strip()
                        break

        except FatalSandboxException:
            raise
        except Exception as e:
            logger.error(f"Sandbox execution failed: {e}")
            logs += f"\nSandbox Exception: {str(e)}"
        finally:
            if container:
                try:
                    container.remove(force=True)
                except Exception as e:
                    pass

        logger.info(f"Sandbox run complete. Expected error found: {expected_found}")
        return {"logs": logs, "expected_found": expected_found, "bisect_result": bisect_result}
