import pytest
import hmac
import hashlib
import json
import sqlite3
import subprocess
import concurrent.futures
import httpx
from unittest.mock import MagicMock
from fastapi.testclient import TestClient

from app.main import app, get_github_token, process_issue
from app.config import settings
from app.services.db_service import DBService
from app.services.audit_service import AuditService
from app.services.llm_service import LLMService
from app.services.sandbox_service import SandboxService
from app.services.config_service import ConfigService
from app.services.hook_service import HookService
from app.services.github_service import GitHubService
from app.schemas import WebhookPayload, ReproductionContext
from tests.e2e.conftest import MockContainer


def send_webhook(client: TestClient, payload: dict) -> httpx.Response:
    body = json.dumps(payload).encode("utf-8")
    signature = hmac.new(
        settings.WEBHOOK_SECRET.encode("utf-8"),
        msg=body,
        digestmod=hashlib.sha256
    ).hexdigest()
    
    headers = {
        "x-hub-signature-256": f"sha256={signature}",
        "Content-Type": "application/json"
    }
    return client.post("/webhook", content=body, headers=headers)


# =====================================================================
# F1 LLM Boundaries - 5 tests
# =====================================================================

@pytest.mark.asyncio
async def test_f1_1_llm_empty_and_long_inputs(monkeypatch):
    """F1.1: Title/body is empty or extremely long: does LLM service handle this?"""
    # Test empty inputs
    ctx_empty = await LLMService.extract_reproduction_context("", "")
    assert isinstance(ctx_empty, ReproductionContext)
    
    # Test extremely long inputs
    long_title = "A" * 50000
    long_body = "B" * 100000
    ctx_long = await LLMService.extract_reproduction_context(long_title, long_body)
    assert isinstance(ctx_long, ReproductionContext)

    # Force the OpenAI completions client to raise an exception for general error handling
    class MockErrorCompletions:
        async def create(self, *args, **kwargs):
            raise Exception("LLM Provider Timeout or API Error")
            
    class MockErrorChat:
        completions = MockErrorCompletions()
        
    class MockErrorClient:
        chat = MockErrorChat()
        
    monkeypatch.setattr("app.services.llm_service.client", MockErrorClient())
    
    # It should catch the Exception and return the fallback context
    fallback_ctx = await LLMService.extract_reproduction_context("title", "body")
    assert isinstance(fallback_ctx, ReproductionContext)
    assert fallback_ctx.reproduction_commands == ["echo 'LLM Parsing Failed'"]


@pytest.mark.asyncio
async def test_f1_2_llm_markdown_backticks_stripping(monkeypatch):
    """F1.2: LLM response has JSON code block markdown backticks around it (e.g. ```json ... ```): check if LLM service successfully strips them."""
    class MockBackticksCompletions:
        async def create(self, *args, **kwargs):
            content = """```json
            {
                "base_image": "python:3.11",
                "required_packages": ["pip"],
                "env_vars": {"TEST": "true"},
                "reproduction_commands": ["echo 'reproduced'"],
                "expected_error_keywords": ["reproduced"],
                "known_good_commit": "abcdef",
                "requires_network": false
            }
            ```"""
            class MockMessage:
                def __init__(self, content_str):
                    self.content = content_str
            class MockChoice:
                def __init__(self, content_str):
                    self.message = MockMessage(content_str)
            class MockResponse:
                def __init__(self, content_str):
                    self.choices = [MockChoice(content_str)]
            return MockResponse(content)
            
    class MockChat:
        completions = MockBackticksCompletions()
        
    class MockClient:
        chat = MockChat()
        
    monkeypatch.setattr("app.services.llm_service.client", MockClient())
    
    ctx = await LLMService.extract_reproduction_context("title", "body")
    assert ctx.base_image == "python:3.11"
    assert ctx.required_packages == ["pip"]


def test_f1_3_docker_empty_commands(mock_docker, monkeypatch):
    """F1.3: LLM returns reproduction commands that are empty: how does Docker exec run handle it?"""
    # Mock container.exec_run to return clean setup
    def mock_exec_run(self, cmd, workdir=None):
        return (0, b"Setup clean, banana success")
    monkeypatch.setattr(MockContainer, "exec_run", mock_exec_run)

    context = ReproductionContext(
        base_image="ubuntu:22.04",
        required_packages=[],
        env_vars={},
        reproduction_commands=[],
        expected_error_keywords=["error"]
    )
    sandbox = SandboxService()
    result = sandbox.run_reproduction(context, "owner/repo")
    assert result["expected_found"] is False
    assert "STAGE 2" in result["logs"]


def test_f1_4_no_expected_keywords(mock_docker):
    """F1.4: LLM returns no expected error keywords: sandbox does not search for keywords, returns expected_found=False."""
    context = ReproductionContext(
        base_image="ubuntu:22.04",
        required_packages=[],
        env_vars={},
        reproduction_commands=["echo 'reproduced'"],
        expected_error_keywords=[]
    )
    sandbox = SandboxService()
    result = sandbox.run_reproduction(context, "owner/repo")
    assert result["expected_found"] is False


def test_f1_5_extremely_long_env_vars(mock_docker):
    """F1.5: LLM returns extremely long environment variables."""
    long_value = "X" * 100000
    context = ReproductionContext(
        base_image="ubuntu:22.04",
        required_packages=[],
        env_vars={"LONG_VAR": long_value},
        reproduction_commands=["echo $LONG_VAR"],
        expected_error_keywords=["reproduced"]
    )
    sandbox = SandboxService()
    sandbox.run_reproduction(context, "owner/repo")
    
    # Verify the env var was passed to containers.run
    assert len(mock_docker.containers.active_containers) > 0
    container = mock_docker.containers.active_containers[-1]
    assert container.environment["LONG_VAR"] == long_value


# =====================================================================
# F2 Docker Boundaries - 5 tests
# =====================================================================

def test_f2_1_timeout_kills_execution(mock_docker, monkeypatch):
    """F2.1: Timeout parameter in config is extremely small (e.g. 1 second): checks if timeout kills the execution and logs it."""
    # Mock container.exec_run to raise TimeoutError
    def mock_exec_run(self, cmd, workdir=None):
        raise concurrent.futures.TimeoutError()
        
    monkeypatch.setattr(MockContainer, "exec_run", mock_exec_run)
    
    context = ReproductionContext(
        base_image="ubuntu:22.04",
        required_packages=[],
        env_vars={},
        reproduction_commands=["sleep 100"],
        expected_error_keywords=[]
    )
    sandbox = SandboxService()
    result = sandbox.run_reproduction(context, "owner/repo", repo_config={"timeout": 1})
    
    assert "Host-side timeout" in result["logs"]
    assert result["expected_found"] is False


def test_f2_2_custom_memory_limit(mock_docker):
    """F2.2: Memory limit in config is custom (e.g. "512m"): verifies MemLimit passed to Docker containers run."""
    context = ReproductionContext(
        base_image="ubuntu:22.04",
        required_packages=[],
        env_vars={},
        reproduction_commands=[],
        expected_error_keywords=[]
    )
    sandbox = SandboxService()
    sandbox.run_reproduction(context, "owner/repo", repo_config={"resource_limits": {"memory": "512m"}})
    
    assert len(mock_docker.containers.active_containers) > 0
    container = mock_docker.containers.active_containers[-1]
    assert container.kwargs["mem_limit"] == "512m"


def test_f2_3_custom_cpu_shares(mock_docker):
    """F2.3: CPU limit in config is custom (e.g. 512 cpus): verifies cpu_shares passed."""
    context = ReproductionContext(
        base_image="ubuntu:22.04",
        required_packages=[],
        env_vars={},
        reproduction_commands=[],
        expected_error_keywords=[]
    )
    sandbox = SandboxService()
    sandbox.run_reproduction(context, "owner/repo", repo_config={"resource_limits": {"cpus": 512}})
    
    assert len(mock_docker.containers.active_containers) > 0
    container = mock_docker.containers.active_containers[-1]
    assert container.kwargs["cpu_shares"] == 512


def test_f2_4_git_bisect_skipped(mock_docker, monkeypatch):
    """F2.4: Stage 3 git bisect with malformed known_good_commit (e.g., none or empty): verifies Git Bisect is skipped."""
    def mock_exec_run(self, cmd, workdir=None):
        return (0, b"AssertionError: expected error reproduced! key: reproduced")
    monkeypatch.setattr(MockContainer, "exec_run", mock_exec_run)
    
    # 1. Test when known_good_commit is empty string
    context_empty = ReproductionContext(
        base_image="ubuntu:22.04",
        required_packages=[],
        env_vars={},
        reproduction_commands=["echo 'reproduced'"],
        expected_error_keywords=["reproduced"],
        known_good_commit=""
    )
    sandbox = SandboxService()
    result_empty = sandbox.run_reproduction(context_empty, "owner/repo")
    assert result_empty["expected_found"] is True
    assert "STAGE 3: GIT BISECT" not in result_empty["logs"]
    assert result_empty["bisect_result"] == ""

    # 2. Test when known_good_commit is None
    context_none = ReproductionContext(
        base_image="ubuntu:22.04",
        required_packages=[],
        env_vars={},
        reproduction_commands=["echo 'reproduced'"],
        expected_error_keywords=["reproduced"],
        known_good_commit=None
    )
    result_none = sandbox.run_reproduction(context_none, "owner/repo")
    assert result_none["expected_found"] is True
    assert "STAGE 3: GIT BISECT" not in result_none["logs"]
    assert result_none["bisect_result"] == ""


@pytest.mark.asyncio
async def test_f2_5_large_logs_truncation(monkeypatch):
    """F2.5: Sandbox gets very large logs from Docker run: checks if previous_attempts truncates or handles large logs."""
    # Mock gh.get_repo_file to return max_retries = 2
    async def mock_get_repo_file(self, repo, path, token):
        return "ghost:\n  max_retries: 2\n"
    monkeypatch.setattr("app.services.github_service.GitHubService.get_repo_file", mock_get_repo_file)
    
    # Mock sandbox.run_reproduction to return large logs
    large_logs = "A" * 5000 + "END_OF_LARGE_LOGS"
    def mock_run_reproduction(self, context, repo, config=None):
        return {"logs": large_logs, "expected_found": False, "bisect_result": ""}
    monkeypatch.setattr("app.services.sandbox_service.SandboxService.run_reproduction", mock_run_reproduction)
    
    # Spy on LLMService.extract_reproduction_context
    calls = []
    async def mock_extract_context(title, body, comments, previous):
        calls.append(previous)
        return ReproductionContext(
            base_image="ubuntu:22.04",
            required_packages=[],
            env_vars={},
            reproduction_commands=[],
            expected_error_keywords=[]
        )
    monkeypatch.setattr("app.services.llm_service.LLMService.extract_reproduction_context", staticmethod(mock_extract_context))
    
    # Mock other services called inside process_issue
    async def mock_get_comments(self, repo, num, token):
        return ""
    monkeypatch.setattr("app.services.github_service.GitHubService.get_issue_comments", mock_get_comments)
    
    async def mock_post_comment(self, repo, num, comment, token):
        pass
    monkeypatch.setattr("app.services.github_service.GitHubService.post_issue_comment", mock_post_comment)
    
    # Construct a valid payload
    payload = WebhookPayload(
        action="opened",
        issue={"title": "test", "body": "test", "number": 123},
        repository={"full_name": "owner/repo"}
    )
    
    # Call process_issue
    await process_issue(payload)
    
    # We expect 2 calls to extract_reproduction_context
    assert len(calls) == 2
    # First call: previous_attempts is empty
    assert calls[0] == ""
    # Second call: previous_attempts contains the truncated logs
    second_prev = calls[1]
    assert "Attempt 1 Logs:" in second_prev
    assert "END_OF_LARGE_LOGS" in second_prev
    assert len(second_prev) < 2100


# =====================================================================
# F3 GitHub Boundaries - 5 tests
# =====================================================================

def test_f3_1_invalid_ghost_yml():
    """F3.1: ghost.yml config file has invalid YAML syntax: ConfigService should handle error and return default config."""
    invalid_yaml = "ghost: \n  - - invalid: ["
    config = ConfigService.parse_ghost_yml(invalid_yaml)
    
    # Check that it returns the default configuration
    assert config["trigger_keyword"] == "bot/reproduce"
    assert config["timeout"] == 60
    assert config["max_retries"] == 3
    assert config["resource_limits"]["memory"] == "2g"


@pytest.mark.asyncio
async def test_f3_2_huge_comment_history(mock_github_api):
    """F3.2: Issue has huge conversation history (hundreds of comments): checks formatting of comments."""
    # Set up respx mock for comments to return a list of 200 comments
    comments_payload = []
    for i in range(200):
        comments_payload.append({
            "user": {"login": f"user{i}"},
            "body": f"Comment text {i}"
        })
        
    # Override standard comments endpoint mock
    mock_github_api.get(url__regex=r"https://api.github.com/repos/[^/]+/[^/]+/issues/\d+/comments\?per_page=100").mock(
        return_value=httpx.Response(200, json=comments_payload)
    )
    
    history = await GitHubService.get_issue_comments("owner/repo", 123, "token")
    
    # Check that it formatted all comments
    for i in range(200):
        assert f"[user{i}]: Comment text {i}" in history


def test_f3_3_ignore_bot_comment():
    """F3.3: Issue has comments from the bot itself (prevent infinite loops): should ignore if payload action is "created" and "<!-- ghost-bot-signature -->" in comment."""
    client = TestClient(app)
    payload = {
        "action": "created",
        "comment": {
            "body": "This is a comment with bot/reproduce <!-- ghost-bot-signature -->"
        },
        "issue": {
            "title": "A bug",
            "body": "Bug description",
            "number": 123
        },
        "repository": {
            "full_name": "owner/repo"
        }
    }
    
    response = send_webhook(client, payload)
    assert response.status_code == 200
    assert response.json() == {"status": "ignored", "reason": "Ignoring bot's own comment"}


def test_f3_4_missing_payload_data():
    """F3.4: Webhook payload contains no issue or repository data: returns {"status": "ignored", "reason": "Payload does not contain issue or repository data"}."""
    client = TestClient(app)
    
    # Missing repository data
    payload_no_repo = {
        "action": "opened",
        "issue": {
            "title": "A bug",
            "body": "bot/reproduce"
        }
    }
    response = send_webhook(client, payload_no_repo)
    assert response.status_code == 200
    assert response.json() == {"status": "ignored", "reason": "Payload does not contain issue or repository data"}
    
    # Missing issue data
    payload_no_issue = {
        "action": "opened",
        "repository": {
            "full_name": "owner/repo"
        }
    }
    response = send_webhook(client, payload_no_issue)
    assert response.status_code == 200
    assert response.json() == {"status": "ignored", "reason": "Payload does not contain issue or repository data"}


def test_f3_5_unsupported_webhook_action():
    """F3.5: Webhook action is not opened/reopened/created (e.g. "deleted"): returns {"status": "ignored", "reason": "Not an opened issue or created comment"}."""
    client = TestClient(app)
    payload = {
        "action": "deleted",
        "issue": {
            "title": "A bug",
            "body": "bot/reproduce",
            "number": 123
        },
        "repository": {
            "full_name": "owner/repo"
        }
    }
    response = send_webhook(client, payload)
    assert response.status_code == 200
    assert response.json() == {"status": "ignored", "reason": "Not an opened issue or created comment"}


# =====================================================================
# F4 Network Boundaries - 5 tests
# =====================================================================

def test_f4_1_hook_service_execution(tmp_path, monkeypatch):
    """F4.1: HookService execution: checks hook trigger scripts (pre-run and post-run triggers) execution, especially on Windows or Unix."""
    # Set HOOKS_DIR to tmp_path
    monkeypatch.setattr("app.services.hook_service.HOOKS_DIR", tmp_path)
    
    # Create mock hook scripts (use .bat extension to ensure they are picked up as windows script candidates on any platform)
    pre_hook_file = tmp_path / "pre-run.bat"
    pre_hook_file.write_text("echo 'pre'")
    post_hook_file = tmp_path / "post-run.bat"
    post_hook_file.write_text("echo 'post'")
    
    # Record popen calls
    popen_calls = []
    def mock_popen(cmd, **kwargs):
        popen_calls.append((cmd, kwargs))
        return MagicMock()
        
    monkeypatch.setattr(subprocess, "Popen", mock_popen)
    
    # Trigger pre-run
    HookService.trigger_pre_run("owner/repo", 123)
    assert len(popen_calls) == 1
    cmd, kwargs = popen_calls[0]
    assert cmd == [str(pre_hook_file)]
    assert kwargs["env"]["GHOST_REPO"] == "owner/repo"
    assert kwargs["env"]["GHOST_ISSUE"] == "123"
    assert kwargs["env"]["GHOST_PHASE"] == "pre-run"
    
    # Trigger post-run
    HookService.trigger_post_run("owner/repo", 123, True, "culprit commit hash")
    assert len(popen_calls) == 2
    cmd, kwargs = popen_calls[1]
    assert cmd == [str(post_hook_file)]
    assert kwargs["env"]["GHOST_REPO"] == "owner/repo"
    assert kwargs["env"]["GHOST_ISSUE"] == "123"
    assert kwargs["env"]["GHOST_SUCCESS"] == "true"
    assert kwargs["env"]["GHOST_BISECT"] == "culprit commit hash"
    assert kwargs["env"]["GHOST_PHASE"] == "post-run"


def test_f4_2_db_init_failure(monkeypatch):
    """F4.2: DBService init failure handling: database file location is read-only or invalid, service catches error."""
    # Mock sqlite3.connect to raise OperationalError
    def mock_connect(*args, **kwargs):
        raise sqlite3.OperationalError("Mock DB Access Error: read-only or invalid location")
    monkeypatch.setattr(sqlite3, "connect", mock_connect)
    
    # Instantiation should not crash
    db = DBService()
    assert db.db_path is not None
    
    # log_reproduction should not crash
    db.log_reproduction("owner/repo", 123, True, "bisect", "logs")
    
    # get_history should not crash and return empty list
    history = db.get_history(123)
    assert history == []
    
    # get_stats should not crash and return empty/default stats
    stats = db.get_stats()
    assert stats == {"total_runs": 0, "success_rate": 0}


def test_f4_3_audit_tampered_log():
    """F4.3: Audit verification on tampered log: verify_chain returns False when a log entry is modified."""
    # Chain is initially valid (empty file is valid)
    assert AuditService.verify_chain() is True
    
    # Log some events
    AuditService.log_event("EVENT_A", {"key": "value_a"})
    AuditService.log_event("EVENT_B", {"key": "value_b"})
    
    # Chain must be valid now
    assert AuditService.verify_chain() is True
    
    # Tamper with the log file
    from app.services.audit_service import AUDIT_LOG_PATH
    with open(AUDIT_LOG_PATH, "r") as f:
        lines = f.readlines()
        
    assert len(lines) == 2
    
    # Tamper with the first line by changing the data value
    first_entry = json.loads(lines[0])
    first_entry["data"]["key"] = "tampered_value"
    lines[0] = json.dumps(first_entry) + "\n"
    
    with open(AUDIT_LOG_PATH, "w") as f:
        f.writelines(lines)
        
    # Chain must now be invalid/tampered!
    assert AuditService.verify_chain() is False


def test_f4_4_missing_github_token(monkeypatch):
    """F4.4: GitHub token env variable is missing or empty."""
    # Set GITHUB_PRIVATE_KEY to empty
    monkeypatch.setattr(settings, "GITHUB_PRIVATE_KEY", "")
    
    token = get_github_token()
    assert token.startswith("github_pat_")


def test_f4_5_mixed_case_trigger(monkeypatch):
    """F4.5: Webhook trigger keyword with case-insensitive / mixed-case triggers (e.g., "BoT/RePrOdUcE")."""
    client = TestClient(app)
    
    # Spy on process_issue to verify it would be triggered
    task_added = []
    class MockBackgroundTasks:
        def add_task(self, func, *args, **kwargs):
            task_added.append((func, args, kwargs))
            
    from fastapi import BackgroundTasks
    monkeypatch.setattr(BackgroundTasks, "add_task", MockBackgroundTasks.add_task)
    
    payload = {
        "action": "opened",
        "issue": {
            "title": "A bug",
            "body": "Please run BoT/RePrOdUcE on this issue",
            "number": 123
        },
        "repository": {
            "full_name": "owner/repo"
        }
    }
    
    response = send_webhook(client, payload)
    assert response.status_code == 200
    assert response.json() == {"status": "accepted", "message": "Reproduction started in background"}
    assert len(task_added) == 1
    assert task_added[0][0] == process_issue
