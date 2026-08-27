import asyncio
import hashlib
import hmac
import json
import pytest
import httpx
import docker
from fastapi.testclient import TestClient
from app.main import app
from app.config import settings
from app.services.db_service import DBService
from app.services.audit_service import AuditService
import app.services.hook_service as hook_service_module


@pytest.mark.asyncio
async def test_workload_1_happy_path(tmp_path, mock_github_api, monkeypatch):
    """
    1. Standard happy path (Webhook -> get config -> LLM extraction ->
    Docker run -> Git Bisect -> conversational response -> post comment ->
    DB log -> Audit log -> Hooks).
    """
    test_hooks_dir = tmp_path / "hooks"
    test_hooks_dir.mkdir()
    monkeypatch.setattr(hook_service_module, "HOOKS_DIR", test_hooks_dir)

    # Write pre-run.bat and post-run.bat to verify hook execution on Windows
    pre_hook_file = test_hooks_dir / "pre-run.bat"
    pre_hook_file.write_text(
        "echo pre-run-triggered %GHOST_REPO% %GHOST_ISSUE% >> "
        + json.dumps(str(test_hooks_dir / "pre_hook_output.txt")).strip('"')
    )

    post_hook_file = test_hooks_dir / "post-run.bat"
    post_hook_file.write_text(
        "echo post-run-triggered %GHOST_REPO% %GHOST_ISSUE% %GHOST_SUCCESS% %GHOST_BISECT% >> "
        + json.dumps(str(test_hooks_dir / "post_hook_output.txt")).strip('"')
    )

    client = TestClient(app)
    issue_num = 201
    payload = {
        "action": "opened",
        "issue": {
            "title": "A bug report",
            "body": "This is a bug report with bot/reproduce keyword",
            "number": issue_num
        },
        "repository": {
            "full_name": "testowner/testrepo"
        }
    }

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

    response = client.post("/webhook", content=body, headers=headers)
    assert response.status_code == 200
    assert response.json() == {"status": "accepted", "message": "Reproduction started in background"}

    # Poll database until the background task finishes
    db = DBService()
    for _ in range(50):
        history = db.get_history(issue_num)
        if len(history) > 0:
            break
        await asyncio.sleep(0.1)

    history = db.get_history(issue_num)
    assert len(history) > 0
    assert history[0]["success"] == 1
    assert "d3b07384d113edec49eaa6238ad5ff00" in history[0]["bisect_result"]

    # Poll for pre-run hook output
    pre_output = test_hooks_dir / "pre_hook_output.txt"
    for _ in range(50):
        if pre_output.exists():
            break
        await asyncio.sleep(0.1)
    assert pre_output.exists()
    assert f"pre-run-triggered testowner/testrepo {issue_num}" in pre_output.read_text()

    # Poll for post-run hook output
    post_output = test_hooks_dir / "post_hook_output.txt"
    for _ in range(50):
        if post_output.exists():
            break
        await asyncio.sleep(0.1)
    assert post_output.exists()
    assert f"post-run-triggered testowner/testrepo {issue_num} true d3b07384d113edec49eaa6238ad5ff00" in post_output.read_text()

    # Verify GitHub comment posted
    post_calls = [
        call for call in mock_github_api.calls
        if call.request.method == "POST" and f"/issues/{issue_num}/comments" in str(call.request.url)
    ]
    assert len(post_calls) > 0

    # Verify Audit log integrity
    assert AuditService.verify_chain() is True


@pytest.mark.asyncio
async def test_workload_2_llm_retry(mock_github_api, monkeypatch):
    """
    2. LLM fails once (returns malformed), succeeds on second retry;
    Docker executes successfully.
    """
    class StatefulMockLLMClient:
        def __init__(self):
            self.call_count = 0

        async def create(self, *args, **kwargs):
            messages = kwargs.get("messages", [])
            system_prompt = next((m["content"] for m in messages if m["role"] == "system"), "")

            # Check if it's structured output extraction
            if "reproduction_commands" in system_prompt or kwargs.get("response_format", {}).get("type") == "json_object":
                self.call_count += 1
                if self.call_count == 1:
                    # Return malformed JSON
                    content = "{ malformed_json_response"
                else:
                    # Return valid JSON
                    content = """{
                        "base_image": "ubuntu:22.04",
                        "required_packages": ["curl"],
                        "env_vars": {"TEST_ENV": "1"},
                        "reproduction_commands": ["echo 'reproduced'"],
                        "expected_error_keywords": ["reproduced"],
                        "known_good_commit": "abcdef0",
                        "requires_network": false
                    }"""
            else:
                # Conversational response
                content = "Ghost: Finished successfully! <details><summary>Logs</summary>reproduced</details>"

            class MockMessage:
                def __init__(self, content):
                    self.content = content

            class MockChoice:
                def __init__(self, content):
                    self.message = MockMessage(content)

            class MockResponse:
                def __init__(self, content):
                    self.choices = [MockChoice(content)]

            return MockResponse(content)

    stateful_llm = StatefulMockLLMClient()

    class MockChat:
        def __init__(self):
            self.completions = stateful_llm

    class MockAsyncOpenAI:
        def __init__(self, *args, **kwargs):
            self.chat = MockChat()

    monkeypatch.setattr("app.services.llm_service.client", MockAsyncOpenAI())
    monkeypatch.setattr("openai.AsyncOpenAI", MockAsyncOpenAI)

    client = TestClient(app)
    issue_num = 202
    payload = {
        "action": "opened",
        "issue": {
            "title": "A bug report",
            "body": "This is a bug report with bot/reproduce keyword",
            "number": issue_num
        },
        "repository": {
            "full_name": "testowner/testrepo"
        }
    }

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

    response = client.post("/webhook", content=body, headers=headers)
    assert response.status_code == 200

    # Poll database until the background task finishes
    db = DBService()
    for _ in range(50):
        history = db.get_history(issue_num)
        if len(history) > 0:
            break
        await asyncio.sleep(0.1)

    history = db.get_history(issue_num)
    assert len(history) > 0
    assert history[0]["success"] == 1
    # Check that extraction was called twice
    assert stateful_llm.call_count == 2


@pytest.mark.asyncio
async def test_workload_3_github_500(mock_github_api, monkeypatch):
    """
    3. GitHub returns 500 repeatedly, triggers retries and eventually fails gracefully.
    """
    # Mock GET contents/ghost.yml to fail with 500
    mock_github_api.get(url__regex=r"https://api.github.com/repos/[^/]+/[^/]+/contents/ghost.yml").mock(
        return_value=httpx.Response(500, text="Internal Server Error")
    )

    # Avoid real delays during retries but keep polling loop sleep working
    real_sleep = asyncio.sleep

    async def smart_sleep(delay, *args, **kwargs):
        if delay >= 1.0:
            return
        await real_sleep(delay, *args, **kwargs)

    monkeypatch.setattr(asyncio, "sleep", smart_sleep)

    client = TestClient(app)
    issue_num = 203
    payload = {
        "action": "opened",
        "issue": {
            "title": "A bug report",
            "body": "This is a bug report with bot/reproduce keyword",
            "number": issue_num
        },
        "repository": {
            "full_name": "testowner/testrepo"
        }
    }

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

    response = client.post("/webhook", content=body, headers=headers)
    assert response.status_code == 200

    # Poll database until the background task finishes
    db = DBService()
    for _ in range(50):
        history = db.get_history(issue_num)
        if len(history) > 0:
            break
        await asyncio.sleep(0.1)

    history = db.get_history(issue_num)
    assert len(history) > 0

    # Verify configuration endpoint was retried 5 times
    ghost_yml_calls = [
        call for call in mock_github_api.calls
        if call.request.method == "GET" and "/contents/ghost.yml" in str(call.request.url)
    ]
    assert len(ghost_yml_calls) == 5

    # Chain remains valid
    assert AuditService.verify_chain() is True


@pytest.mark.asyncio
async def test_workload_4_duplicate_webhook(mock_github_api):
    """
    4. Webhook receives duplicate fire, server deduplicates / handles gracefully.
    """
    # Clear the deduplication cache
    from app.main import PROCESSED_WEBHOOK_IDS, PROCESSED_WEBHOOK_IDS_FIFO
    PROCESSED_WEBHOOK_IDS.clear()
    PROCESSED_WEBHOOK_IDS_FIFO.clear()

    client = TestClient(app)
    issue_num = 204
    payload = {
        "action": "opened",
        "issue": {
            "title": "A bug report",
            "body": "This is a bug report with bot/reproduce keyword",
            "number": issue_num
        },
        "repository": {
            "full_name": "testowner/testrepo"
        }
    }

    body = json.dumps(payload).encode("utf-8")
    signature = hmac.new(
        settings.WEBHOOK_SECRET.encode("utf-8"),
        msg=body,
        digestmod=hashlib.sha256
    ).hexdigest()

    headers = {
        "x-hub-signature-256": f"sha256={signature}",
        "Content-Type": "application/json",
        "x-github-delivery": "delivery-unique-204"
    }

    # First request - should be accepted
    response1 = client.post("/webhook", content=body, headers=headers)
    assert response1.status_code == 200
    assert response1.json() == {"status": "accepted", "message": "Reproduction started in background"}

    # Duplicate request - should be ignored
    response2 = client.post("/webhook", content=body, headers=headers)
    assert response2.status_code == 200
    assert response2.json() == {"status": "ignored", "reason": "Duplicate webhook request"}

    # Poll database for the execution to finish
    db = DBService()
    for _ in range(50):
        history = db.get_history(issue_num)
        if len(history) > 0:
            break
        await asyncio.sleep(0.1)

    history = db.get_history(issue_num)
    assert len(history) == 1  # Only one run was actually scheduled and logged


@pytest.mark.asyncio
async def test_workload_5_cascading_failures(tmp_path, mock_github_api, monkeypatch):
    """
    5. Complex failing scenario: LLM returns malformed JSON, Docker is offline,
    GitHub comment posting fails; handles all.
    """
    # 1. LLM always returns malformed JSON
    class MalformedMockLLMClient:
        async def create(self, *args, **kwargs):
            class MockMessage:
                def __init__(self, content):
                    self.content = content
            class MockChoice:
                def __init__(self, content):
                    self.message = MockMessage(content)
            class MockResponse:
                def __init__(self, content):
                    self.choices = [MockChoice(content)]
            return MockResponse("{ malformed_json_response")

    malformed_llm = MalformedMockLLMClient()

    class MockChat:
        def __init__(self):
            self.completions = malformed_llm

    class MockAsyncOpenAI:
        def __init__(self, *args, **kwargs):
            self.chat = MockChat()

    monkeypatch.setattr("app.services.llm_service.client", MockAsyncOpenAI())
    monkeypatch.setattr("openai.AsyncOpenAI", MockAsyncOpenAI)

    # 2. Docker daemon is offline
    from docker.errors import DockerException
    def mock_from_env(*args, **kwargs):
        raise DockerException("Docker daemon is unreachable or not running.")
    monkeypatch.setattr(docker, "from_env", mock_from_env)

    # 3. GitHub comment posting fails
    mock_github_api.post(url__regex=r"https://api.github.com/repos/[^/]+/[^/]+/issues/\d+/comments").mock(
        return_value=httpx.Response(500, text="Internal Server Error")
    )

    # Avoid real delays during retries but keep polling loop sleep working
    real_sleep = asyncio.sleep

    async def smart_sleep(delay, *args, **kwargs):
        if delay >= 1.0:
            return
        await real_sleep(delay, *args, **kwargs)

    monkeypatch.setattr(asyncio, "sleep", smart_sleep)

    # 4. Hooks setup to check failure status propagation
    test_hooks_dir = tmp_path / "hooks"
    test_hooks_dir.mkdir()
    monkeypatch.setattr(hook_service_module, "HOOKS_DIR", test_hooks_dir)

    post_hook_file = test_hooks_dir / "post-run.bat"
    post_hook_file.write_text(
        "echo post-run-triggered %GHOST_REPO% %GHOST_ISSUE% %GHOST_SUCCESS% >> "
        + json.dumps(str(test_hooks_dir / "post_hook_output.txt")).strip('"')
    )

    client = TestClient(app)
    issue_num = 205
    payload = {
        "action": "opened",
        "issue": {
            "title": "A bug report",
            "body": "This is a bug report with bot/reproduce keyword",
            "number": issue_num
        },
        "repository": {
            "full_name": "testowner/testrepo"
        }
    }

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

    response = client.post("/webhook", content=body, headers=headers)
    assert response.status_code == 200

    # Poll database until the background task finishes
    db = DBService()
    for _ in range(50):
        history = db.get_history(issue_num)
        if len(history) > 0:
            break
        await asyncio.sleep(0.1)

    history = db.get_history(issue_num)
    assert len(history) > 0
    assert history[0]["success"] == 0  # Failed
    assert "Docker daemon is unreachable or not running" in history[0]["logs"]

    # Verify post-run hook triggered with GHOST_SUCCESS=false
    post_output = test_hooks_dir / "post_hook_output.txt"
    for _ in range(50):
        if post_output.exists():
            break
        await asyncio.sleep(0.1)

    assert post_output.exists()
    content = post_output.read_text()
    assert f"post-run-triggered testowner/testrepo {issue_num} false" in content

    # Verify Audit log chain is valid
    assert AuditService.verify_chain() is True
