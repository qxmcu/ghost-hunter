import pytest
import asyncio
import json
import httpx
import concurrent.futures
from openai import RateLimitError, AuthenticationError
from docker.errors import DockerException
from app.services.llm_service import LLMService, FatalLLMException
from app.services.sandbox_service import SandboxService, FatalSandboxException
from app.services.github_service import GitHubService
from app.main import PROCESSED_WEBHOOK_IDS, PROCESSED_WEBHOOK_IDS_FIFO
from fastapi.testclient import TestClient
import cli
import respx

# ==========================================
# 1. LLM Failure Gracefulness Tests
# ==========================================

class MockErrorCompletions:
    def __init__(self, exception_to_raise):
        self.exception_to_raise = exception_to_raise

    async def create(self, *args, **kwargs):
        raise self.exception_to_raise

class MockErrorChat:
    def __init__(self, exception_to_raise):
        self.completions = MockErrorCompletions(exception_to_raise)

class MockErrorAsyncOpenAI:
    def __init__(self, exception_to_raise):
        self.chat = MockErrorChat(exception_to_raise)

@pytest.mark.asyncio
async def test_llm_rate_limit_graceful(monkeypatch):
    """
    Verify that when OpenAI API raises a RateLimitError,
    extract_reproduction_context raises FatalLLMException.
    """
    req = httpx.Request("POST", "https://api.openai.com")
    resp = httpx.Response(429, request=req)
    rate_limit_err = RateLimitError("Rate limit exceeded", response=resp, body=None)

    monkeypatch.setattr("app.services.llm_service.client", MockErrorAsyncOpenAI(rate_limit_err))
    # Ensure api key check passes
    monkeypatch.setattr("app.config.settings.LLM_API_KEY", "real-key-format")

    with pytest.raises(FatalLLMException) as excinfo:
        await LLMService.extract_reproduction_context("test title", "test body")
    assert "Fatal OpenAI error occurred" in str(excinfo.value)

@pytest.mark.asyncio
async def test_llm_auth_failure_graceful(monkeypatch):
    """
    Verify that when OpenAI API raises an AuthenticationError,
    extract_reproduction_context raises FatalLLMException.
    """
    req = httpx.Request("POST", "https://api.openai.com")
    resp = httpx.Response(401, request=req)
    auth_err = AuthenticationError("Invalid API key", response=resp, body=None)

    monkeypatch.setattr("app.services.llm_service.client", MockErrorAsyncOpenAI(auth_err))
    monkeypatch.setattr("app.config.settings.LLM_API_KEY", "real-key-format")

    with pytest.raises(FatalLLMException) as excinfo:
        await LLMService.extract_reproduction_context("test title", "test body")
    assert "Fatal OpenAI error occurred" in str(excinfo.value)

@pytest.mark.asyncio
async def test_llm_missing_key_graceful(monkeypatch):
    """
    Verify that when settings.LLM_API_KEY is empty or default,
    extract_reproduction_context raises FatalLLMException.
    """
    # Test empty API key
    monkeypatch.setattr("app.config.settings.LLM_API_KEY", "")
    monkeypatch.setattr("app.services.llm_service.client", None) # reset cached client

    with pytest.raises(FatalLLMException) as excinfo:
        await LLMService.extract_reproduction_context("test title", "test body")
    assert "LLM API key is missing, empty, or default dummy key" in str(excinfo.value)

    # Test dummy API key
    monkeypatch.setattr("app.config.settings.LLM_API_KEY", "dummy-key")
    with pytest.raises(FatalLLMException) as excinfo:
        await LLMService.extract_reproduction_context("test title", "test body")
    assert "LLM API key is missing, empty, or default dummy key" in str(excinfo.value)

@pytest.mark.asyncio
async def test_llm_malformed_json_fallback(monkeypatch):
    """
    Verify that when the LLM returns malformed JSON,
    extract_reproduction_context falls back gracefully to a default context
    instead of throwing an unhandled exception or entering an infinite loop.
    """
    class MockMalformedCompletions:
        async def create(self, *args, **kwargs):
            class MockMessage:
                content = "{ malformed json: no closing brace"
            class MockChoice:
                message = MockMessage()
            class MockResponse:
                choices = [MockChoice()]
            return MockResponse()

    class MockMalformedChat:
        completions = MockMalformedCompletions()

    class MockMalformedOpenAI:
        chat = MockMalformedChat()

    monkeypatch.setattr("app.services.llm_service.client", MockMalformedOpenAI())
    monkeypatch.setattr("app.config.settings.LLM_API_KEY", "real-key-format")

    ctx = await LLMService.extract_reproduction_context("test title", "test body")
    assert ctx.base_image == "ubuntu:22.04"
    assert "LLM Parsing Failed" in ctx.reproduction_commands[0]


# ==========================================
# 2. Docker Failure Gracefulness Tests
# ==========================================

def test_docker_daemon_offline_graceful(monkeypatch):
    """
    Verify that initializing SandboxService when the Docker daemon is offline
    logs a critical error and sets client=None, and run_reproduction fails gracefully
    returning a dictionary indicating the error instead of crashing the server.
    """
    def mock_from_env(*args, **kwargs):
        raise DockerException("Connection refused")

    monkeypatch.setattr("docker.from_env", mock_from_env)

    sandbox = SandboxService()
    assert sandbox.client is None

    # Dummy ReproductionContext
    from app.schemas import ReproductionContext
    dummy_ctx = ReproductionContext(
        base_image="ubuntu:22.04",
        required_packages=[],
        env_vars={},
        reproduction_commands=["echo 'test'"],
        expected_error_keywords=[]
    )

    res = sandbox.run_reproduction(dummy_ctx, "testowner/testrepo")
    assert res["expected_found"] is False
    assert "Sandbox Exception: Docker daemon is unreachable or not running" in res["logs"]

def test_sandbox_timeout_enforced(monkeypatch):
    """
    Verify that sandbox timeouts are correctly enforced on host-side container execution.
    If the command runs indefinitely, it is timed out on the host side and handled gracefully.
    """
    # Mock future.result to raise TimeoutError immediately
    def mock_future_result(self, timeout=None):
        raise concurrent.futures.TimeoutError()

    monkeypatch.setattr(concurrent.futures.Future, "result", mock_future_result)

    # Use the mock docker client from conftest, but override run to return a dummy container
    # that supports our test
    class MockContainer:
        def __init__(self):
            self.attrs = {"NetworkSettings": {"Networks": {}}}
        def exec_run(self, cmd, workdir=None):
            return (0, b"")
        def remove(self, **kwargs):
            pass

    class MockContainers:
        def run(self, *args, **kwargs):
            return MockContainer()

    class MockImages:
        def get(self, name):
            return True
        def pull(self, name):
            return True

    class MockNetwork:
        def disconnect(self, container):
            pass
        def connect(self, container):
            pass

    class MockNetworks:
        def get(self, net_id):
            return MockNetwork()

    class MockClient:
        def __init__(self):
            self.containers = MockContainers()
            self.images = MockImages()
            self.networks = MockNetworks()

    sandbox = SandboxService()
    sandbox.client = MockClient()

    from app.schemas import ReproductionContext
    dummy_ctx = ReproductionContext(
        base_image="ubuntu:22.04",
        required_packages=[],
        env_vars={},
        reproduction_commands=["sleep 100"],
        expected_error_keywords=[]
    )

    res = sandbox.run_reproduction(dummy_ctx, "testowner/testrepo")
    assert res["expected_found"] is False
    assert "Host-side timeout" in res["logs"]


# ==========================================
# 3. GitHub Resiliency Tests
# ==========================================

@pytest.mark.asyncio
async def test_github_retry_exponential_backoff(monkeypatch):
    """
    Verify that HTTP network errors (500, 429) are correctly retried
    with exponential backoff, and eventually succeed if the server recovers.
    """
    sleep_durations = []
    async def mock_sleep(seconds):
        sleep_durations.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", mock_sleep)

    with respx.mock(assert_all_called=True) as respx_mock:
        # Mock 3 calls to contents/ghost.yml: 500, 429, then 200
        respx_mock.get("https://api.github.com/repos/testowner/testrepo/contents/ghost.yml").mock(
            side_effect=[
                httpx.Response(500),
                httpx.Response(429),
                httpx.Response(200, text="ghost:\n  max_retries: 2\n")
            ]
        )

        content = await GitHubService.get_repo_file("testowner/testrepo", "ghost.yml", "token")
        assert "max_retries: 2" in content
        # Backoff starts at 1.0, doubles to 2.0
        assert sleep_durations == [1.0, 2.0]

def test_webhook_deduplication_delivery_header():
    """
    Verify that duplicate webhooks are discarded via the X-GitHub-Delivery header.
    """
    from app.main import app
    client = TestClient(app)

    # Reset deduplication state
    PROCESSED_WEBHOOK_IDS.clear()
    PROCESSED_WEBHOOK_IDS_FIFO.clear()

    payload = {
        "action": "opened",
        "issue": {
            "title": "A bug report",
            "body": "No trigger",
            "number": 999
        },
        "repository": {
            "full_name": "testowner/testrepo"
        }
    }

    from app.services.github_service import GitHubService
    GitHubService.verify_webhook_signature = lambda body, signature: True

    headers = {
        "x-github-delivery": "delivery-uuid-abc-123",
        "Content-Type": "application/json"
    }

    # First request
    response1 = client.post("/webhook", json=payload, headers=headers)
    # The payload doesn't contain "bot/reproduce", so it is ignored but with status code 200
    # and reason "No explicit trigger keyword..."
    assert response1.status_code == 200
    assert response1.json()["status"] == "ignored"
    assert "No explicit trigger keyword" in response1.json()["reason"]

    # Second request with identical delivery header
    response2 = client.post("/webhook", json=payload, headers=headers)
    assert response2.status_code == 200
    assert response2.json() == {"status": "ignored", "reason": "Duplicate webhook request"}


# ==========================================
# 4. CLI / Smee Proxy Resiliency Tests
# ==========================================

class StopLoopException(BaseException):
    pass

def test_smee_proxy_reconnection_backoff(monkeypatch):
    """
    Verify that Smee proxy reconnection logic handles network dropouts gracefully
    using exponential backoff and retries, without crashing uvicorn.
    """
    # Track the backoffs passed to time.sleep
    sleep_durations = []
    def mock_sleep(seconds):
        sleep_durations.append(seconds)
        if len(sleep_durations) >= 3:
            raise StopLoopException("Break infinite loop for verification")

    monkeypatch.setattr("time.sleep", mock_sleep)

    # Mock connect_sse to always raise connection error
    class MockConnectSSE:
        def __init__(self, *args, **kwargs):
            pass
        def __enter__(self):
            raise httpx.ConnectError("SSE connection dropped")
        def __exit__(self, exc_type, exc_val, exc_tb):
            pass

    monkeypatch.setattr("cli.connect_sse", lambda *args, **kwargs: MockConnectSSE())

    # We expect run_smee_proxy to raise StopLoopException when sleep_durations reaches 3
    with pytest.raises(StopLoopException):
        cli.run_smee_proxy("https://smee.io/dummy-channel", 8000)

    # Verify exponential backoff: starts at 2.0, doubles to 4.0, then 8.0 (raised before sleeping 8.0)
    assert sleep_durations == [2.0, 4.0, 8.0]

def test_smee_proxy_message_forwarding(monkeypatch):
    """
    Verify that messages received via Smee SSE proxy are correctly cleaned
    (host/content-length headers deleted) and forwarded to the local webhook server.
    """
    # Event data containing custom headers to verify they are stripped
    event_data = {
        "body": {"test": "payload"},
        "x-hub-signature-256": "sha256=abcdef",
        "host": "smee.io",
        "content-length": "42"
    }

    class MockSSEEvent:
        event = "message"
        data = json.dumps(event_data)

    class MockEventSource:
        def iter_sse(self):
            yield MockSSEEvent()
            raise StopLoopException()

    class MockConnectSSE:
        def __enter__(self):
            return MockEventSource()
        def __exit__(self, exc_type, exc_val, exc_tb):
            pass

    monkeypatch.setattr("cli.connect_sse", lambda *args, **kwargs: MockConnectSSE())

    posted_requests = []
    class MockHttpClient:
        def __init__(self, *args, **kwargs):
            pass
        def __enter__(self):
            return self
        def __exit__(self, exc_type, exc_val, exc_tb):
            pass
        def post(self, url, json=None, headers=None):
            posted_requests.append((url, json, headers))

    monkeypatch.setattr("httpx.Client", MockHttpClient)

    # We mock time.sleep to break the loop on first iteration
    def mock_sleep(seconds):
        raise StopLoopException()
    monkeypatch.setattr("time.sleep", mock_sleep)

    with pytest.raises(StopLoopException):
        cli.run_smee_proxy("https://smee.io/dummy-channel", 8000)

    assert len(posted_requests) == 1
    url, body, headers = posted_requests[0]
    assert url == "http://127.0.0.1:8000/webhook"
    assert body == {"test": "payload"}
    # Host and content-length should be stripped
    assert "host" not in headers
    assert "content-length" not in headers
    assert headers["x-hub-signature-256"] == "sha256=abcdef"
