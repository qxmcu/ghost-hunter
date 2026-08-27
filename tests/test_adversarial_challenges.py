import pytest
import asyncio
import time
import httpx
import respx
import concurrent.futures
from unittest.mock import MagicMock, patch
from docker.errors import DockerException
from app.config import settings
from app.services.llm_service import _get_client, FatalLLMException, LLMService
from app.services.sandbox_service import SandboxService, FatalSandboxException
from app.main import PROCESSED_WEBHOOK_IDS, PROCESSED_WEBHOOK_IDS_FIFO, process_issue, get_github_token
from fastapi.testclient import TestClient

def test_llm_client_cached_correctly(monkeypatch):
    """
    Verify that _get_client caches the OpenAI client.
    """
    monkeypatch.setattr("app.services.llm_service.client", None)
    monkeypatch.setattr("app.config.settings.LLM_API_KEY", "test-key-abc")

    c1 = _get_client()
    c2 = _get_client()
    # If caching is correct, they should be the same instance.
    assert c1 is c2, "LLM client is not cached correctly."


def test_sandbox_timeout_no_executor_hang():
    """
    Verify that the ThreadPoolExecutor inside _exec_run_with_timeout
    does not cause the host-side timeout to block the main execution thread.
    """
    sandbox = SandboxService()
    
    # Mock container and a slow exec_run (sleeps 1.5s)
    mock_container = MagicMock()
    def slow_exec_run(*args, **kwargs):
        time.sleep(1.5)
        return (0, b"success")
    mock_container.exec_run = slow_exec_run

    # Call _exec_run_with_timeout with a very short timeout (0.1s)
    start_time = time.time()
    with pytest.raises(TimeoutError):
        sandbox._exec_run_with_timeout(mock_container, "dummy cmd", "/workspace", 0.1)
    duration = time.time() - start_time
    
    # If ThreadPoolExecutor correctly shuts down asynchronously or doesn't block,
    # the function should raise TimeoutError in ~0.1s.
    assert duration < 0.5, f"Expected the function to raise TimeoutError quickly without blocking, but took {duration}s."


@pytest.mark.asyncio
async def test_docker_offline_during_reproduction_does_not_abort_loop(monkeypatch):
    """
    Verify that if Docker daemon goes offline (raising DockerException) during reproduction,
    the sandbox service catches it, logs it, but does NOT raise FatalSandboxException.
    This causes the main webhook processing loop to NOT abort, wasting LLM tokens and time
    on redundant retries.
    """
    # 1. Mock sandbox setup to succeed initially, but fail with DockerException on run
    sandbox = SandboxService()
    sandbox.client = MagicMock()
    
    def offline_images_get(*args, **kwargs):
        raise DockerException("Docker daemon connection lost")
    sandbox.client.images.get = offline_images_get

    # Create dummy ReproductionContext
    from app.schemas import ReproductionContext
    dummy_ctx = ReproductionContext(
        base_image="ubuntu:22.04",
        required_packages=[],
        env_vars={},
        reproduction_commands=["echo 'test'"],
        expected_error_keywords=[]
    )

    # Calling run_reproduction should not raise FatalSandboxException
    # but rather return a failure result dictionary.
    res = sandbox.run_reproduction(dummy_ctx, "owner/repo")
    assert res["expected_found"] is False
    assert "Docker daemon connection lost" in res["logs"] or "Sandbox execution failed" in res["logs"]
    
    # Verify it does not raise FatalSandboxException
    # (If it doesn't raise, the caller retry loop in main.py will continue to retry)


def test_webhook_deduplication_swallows_failed_validation():
    """
    Verify that a webhook request with duplicate delivery ID is rejected
    even if the first request failed validation (400 Bad Request) or failed parsing.
    This prevents legitimate retries of failed deliveries from ever being processed.
    """
    from app.main import app
    client = TestClient(app)
    PROCESSED_WEBHOOK_IDS.clear()
    PROCESSED_WEBHOOK_IDS_FIFO.clear()

    # Mock verify_webhook_signature to return True
    from app.services.github_service import GitHubService
    GitHubService.verify_webhook_signature = lambda body, signature: True

    headers = {
        "x-hub-signature-256": "sha256=dummy",
        "x-github-delivery": "delivery-id-9999",
        "Content-Type": "application/json"
    }

    # 1. Send a request with malformed JSON body
    # This will fail at JSON parsing in FastAPI/main.py but it will still consume the delivery ID!
    response1 = client.post("/webhook", content="{ malformed json", headers=headers)
    assert response1.status_code == 400

    # 2. Resend the request with correct JSON body and same delivery ID (a retry)
    payload = {
        "action": "opened",
        "issue": {"title": "Test Bug", "body": "bot/reproduce", "number": 1},
        "repository": {"full_name": "owner/repo"}
    }
    response2 = client.post("/webhook", json=payload, headers=headers)
    
    # It will be rejected as a duplicate, even though the first one was never successfully parsed or validated!
    assert response2.status_code == 200
    assert response2.json() == {"status": "ignored", "reason": "Duplicate webhook request"}


def test_github_token_escaped_newline_cleaned(monkeypatch):
    """
    Verify that GITHUB_PRIVATE_KEY from environment with escaped newlines (\n)
    is correctly cleaned/unescaped by get_github_token in app/main.py.
    """
    escaped_key = "---BEGIN---\\nKEY\\n---END---"
    monkeypatch.setattr(settings, "GITHUB_PRIVATE_KEY", escaped_key)
    
    token = get_github_token()
    # The loaded token should contain \n instead of \\n because we now call get_clean_private_key()
    assert token == "---BEGIN---\nKEY\n---END---"
    assert "\n" in token
