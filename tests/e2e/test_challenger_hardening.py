import pytest
import hmac
import hashlib
import json
import asyncio
import httpx
import docker
import concurrent.futures
from fastapi.testclient import TestClient
from openai import RateLimitError, AuthenticationError

from app.main import app
from app.config import settings
from app.services.llm_service import LLMService, FatalLLMException
from app.services.sandbox_service import SandboxService
from app.services.github_service import GitHubService
from app.schemas import ReproductionContext
from cli import run_smee_proxy

# Helper to send signature-verified webhook
def send_webhook(client: TestClient, payload: dict, delivery_id: str = None) -> httpx.Response:
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
    if delivery_id:
        headers["x-github-delivery"] = delivery_id
    return client.post("/webhook", content=body, headers=headers)

# -------------------------------------------------------------
# 1. LLM Failure Gracefulness Tests
# -------------------------------------------------------------

@pytest.mark.asyncio
async def test_llm_rate_limit_graceful(monkeypatch):
    """Verify rate limits result in FatalLLMException and graceful background abort."""
    dummy_req = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    dummy_resp = httpx.Response(429, request=dummy_req)
    rate_limit_err = RateLimitError("Rate limit exceeded", response=dummy_resp, body=None)

    class MockRateLimitCompletions:
        async def create(self, *args, **kwargs):
            raise rate_limit_err

    class MockChat:
        completions = MockRateLimitCompletions()

    class MockAsyncOpenAI:
        def __init__(self, *args, **kwargs):
            self.chat = MockChat()

    monkeypatch.setattr("app.services.llm_service.client", MockAsyncOpenAI())
    monkeypatch.setattr("openai.AsyncOpenAI", MockAsyncOpenAI)

    # Calling extract_reproduction_context should raise FatalLLMException
    with pytest.raises(FatalLLMException, match="Rate limit exceeded"):
        await LLMService.extract_reproduction_context("title", "body")

@pytest.mark.asyncio
async def test_llm_auth_failure_graceful(monkeypatch):
    """Verify auth failures result in FatalLLMException."""
    dummy_req = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    dummy_resp = httpx.Response(401, request=dummy_req)
    auth_err = AuthenticationError("Incorrect API key", response=dummy_resp, body=None)

    class MockAuthCompletions:
        async def create(self, *args, **kwargs):
            raise auth_err

    class MockChat:
        completions = MockAuthCompletions()

    class MockAsyncOpenAI:
        def __init__(self, *args, **kwargs):
            self.chat = MockChat()

    monkeypatch.setattr("app.services.llm_service.client", MockAsyncOpenAI())
    monkeypatch.setattr("openai.AsyncOpenAI", MockAsyncOpenAI)

    with pytest.raises(FatalLLMException, match="Incorrect API key"):
        await LLMService.extract_reproduction_context("title", "body")

@pytest.mark.asyncio
async def test_llm_missing_key_graceful(monkeypatch):
    """Verify missing LLM API key results in FatalLLMException."""
    monkeypatch.setattr("app.config.settings.LLM_API_KEY", "")
    # Clear client cache
    monkeypatch.setattr("app.services.llm_service.client", None)

    with pytest.raises(FatalLLMException, match="LLM API key is missing"):
        await LLMService.extract_reproduction_context("title", "body")

@pytest.mark.asyncio
async def test_llm_malformed_json_fallback(monkeypatch):
    """Verify malformed JSON from LLM is handled gracefully and falls back to default context."""
    class MockMalformedCompletions:
        async def create(self, *args, **kwargs):
            class MockMessage:
                content = "{ malformed_json_here "
            class MockChoice:
                message = MockMessage()
            class MockResponse:
                choices = [MockChoice()]
            return MockResponse()

    class MockChat:
        completions = MockMalformedCompletions()

    class MockAsyncOpenAI:
        def __init__(self, *args, **kwargs):
            self.chat = MockChat()

    monkeypatch.setattr("app.services.llm_service.client", MockAsyncOpenAI())
    monkeypatch.setattr("openai.AsyncOpenAI", MockAsyncOpenAI)

    ctx = await LLMService.extract_reproduction_context("title", "body")
    assert isinstance(ctx, ReproductionContext)
    assert ctx.reproduction_commands == ["echo 'LLM Parsing Failed'"]

# -------------------------------------------------------------
# 2. Docker Failure Gracefulness Tests
# -------------------------------------------------------------

def test_docker_daemon_offline_graceful(monkeypatch):
    """Verify starting SandboxService with offline daemon logs gracefully and does not crash."""
    def mock_from_env(*args, **kwargs):
        raise docker.errors.DockerException("Docker daemon offline")

    monkeypatch.setattr(docker, "from_env", mock_from_env)

    sandbox = SandboxService()
    assert sandbox.client is None

    context = ReproductionContext(
        base_image="ubuntu:22.04",
        required_packages=[],
        env_vars={},
        reproduction_commands=["echo 1"],
        expected_error_keywords=[]
    )
    res = sandbox.run_reproduction(context, "owner/repo")
    assert res["expected_found"] is False
    assert "Docker daemon is unreachable" in res["logs"]

def test_sandbox_timeout_enforced(mock_docker, monkeypatch):
    """Verify host-side container execution timeout is correctly enforced."""
    def mock_exec_run(self, cmd, workdir=None):
        raise concurrent.futures.TimeoutError("Host timeout")

    monkeypatch.setattr("tests.e2e.conftest.MockContainer.exec_run", mock_exec_run)

    context = ReproductionContext(
        base_image="ubuntu:22.04",
        required_packages=[],
        env_vars={},
        reproduction_commands=["sleep 100"],
        expected_error_keywords=[]
    )
    sandbox = SandboxService()
    res = sandbox.run_reproduction(context, "owner/repo", repo_config={"timeout": 1})
    assert "Host-side timeout" in res["logs"]
    assert res["expected_found"] is False

# -------------------------------------------------------------
# 3. GitHub Resiliency Tests
# -------------------------------------------------------------

@pytest.mark.asyncio
async def test_github_http_retry_backoff(mock_github_api, monkeypatch):
    """Verify HTTP network errors (500, 429) are retried with backoff."""
    # Mock GET comments to fail with 429, then 500, then succeed
    call_states = [429, 500, 200]
    calls = []

    def mock_handler(request):
        status = call_states[len(calls)]
        calls.append(status)
        if status == 200:
            return httpx.Response(200, json=[])
        return httpx.Response(status, text="Error")

    mock_github_api.clear()
    mock_github_api.get(url__regex=r"https://api.github.com/repos/[^/]+/[^/]+/issues/\d+/comments\?per_page=100").mock(
        side_effect=mock_handler
    )

    # Stub asyncio.sleep so the test runs instantly
    sleep_calls = []
    async def mock_sleep(delay):
        sleep_calls.append(delay)

    monkeypatch.setattr(asyncio, "sleep", mock_sleep)

    comments = await GitHubService.get_issue_comments("owner/repo", 123, "token")
    assert comments == ""
    assert calls == [429, 500, 200]
    assert sleep_calls == [1.0, 2.0]  # Exponential backoff 1.0, 2.0

@pytest.mark.asyncio
async def test_webhook_deduplication(mock_github_api):
    """Verify duplicate webhooks are discarded via X-GitHub-Delivery header."""
    from app.main import PROCESSED_WEBHOOK_IDS, PROCESSED_WEBHOOK_IDS_FIFO
    PROCESSED_WEBHOOK_IDS.clear()
    PROCESSED_WEBHOOK_IDS_FIFO.clear()

    client = TestClient(app)
    payload = {
        "action": "opened",
        "issue": {
            "title": "Bug description",
            "body": "bot/reproduce key",
            "number": 999
        },
        "repository": {
            "full_name": "owner/repo"
        }
    }

    # First webhook call with delivery-123
    resp1 = send_webhook(client, payload, delivery_id="delivery-123")
    assert resp1.status_code == 200
    assert resp1.json() == {"status": "accepted", "message": "Reproduction started in background"}

    # Duplicate webhook call with same delivery-123
    resp2 = send_webhook(client, payload, delivery_id="delivery-123")
    assert resp2.status_code == 200
    assert resp2.json() == {"status": "ignored", "reason": "Duplicate webhook request"}

# -------------------------------------------------------------
# 4. CLI Resiliency Tests
# -------------------------------------------------------------

class SmeeTestComplete(BaseException):
    pass

def test_smee_proxy_reconnect(monkeypatch):
    """Verify uvicorn doesn't crash when Smee proxy loses connection and reconnects."""
    calls = []
    def mock_connect_sse(client, method, url):
        calls.append(url)
        if len(calls) == 1:
            raise httpx.RequestError("SSE connection lost")
        else:
            raise SmeeTestComplete("Resiliency verified: loop retried")

    monkeypatch.setattr("cli.connect_sse", mock_connect_sse)
    monkeypatch.setattr("time.sleep", lambda x: None)
    monkeypatch.setattr("cli.print_info", lambda x: None)

    with pytest.raises(SmeeTestComplete, match="Resiliency verified: loop retried"):
        run_smee_proxy("https://smee.io/dummy", 8000)

    assert len(calls) == 2
