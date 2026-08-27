import pytest
import asyncio
import httpx
import respx
from unittest.mock import MagicMock, patch
from docker.errors import DockerException
from app.config import settings
from app.services.llm_service import _get_client, FatalLLMException
from app.services.github_service import GitHubService
from app.services.sandbox_service import SandboxService
from app.main import app, PROCESSED_WEBHOOK_IDS, PROCESSED_WEBHOOK_IDS_FIFO
from fastapi.testclient import TestClient

def test_dynamic_llm_client_retrieval(monkeypatch):
    """
    Verify that _get_client dynamically retrieves settings and instantiates AsyncOpenAI.
    """
    # Clear any cached client
    monkeypatch.setattr("app.services.llm_service.client", None)
    
    # 1. Missing api key raises FatalLLMException
    monkeypatch.setattr(settings, "LLM_API_KEY", "")
    with pytest.raises(FatalLLMException) as excinfo:
        _get_client()
    assert "missing, empty, or default dummy key" in str(excinfo.value)

    # 2. Dynamic instantiation works when API key is set
    monkeypatch.setattr(settings, "LLM_API_KEY", "api-key-A")
    client_a = _get_client()
    assert client_a.api_key == "api-key-A"

    # 3. Dynamic instantiation gets updated key
    monkeypatch.setattr(settings, "LLM_API_KEY", "api-key-B")
    client_b = _get_client()
    assert client_b.api_key == "api-key-B"


def test_webhook_deduplication_sliding_window(monkeypatch):
    """
    Verify that the webhook deduplication sliding window strictly limits the set to 1000 entries.
    Evicts the oldest IDs when size exceeds 1000.
    """
    monkeypatch.setattr("app.main.process_issue", lambda payload: None)
    PROCESSED_WEBHOOK_IDS.clear()
    PROCESSED_WEBHOOK_IDS_FIFO.clear()

    # Fill up the window to 1000
    for i in range(1000):
        delivery_id = f"id-{i}"
        PROCESSED_WEBHOOK_IDS.add(delivery_id)
        PROCESSED_WEBHOOK_IDS_FIFO.append(delivery_id)

    assert len(PROCESSED_WEBHOOK_IDS) == 1000
    assert len(PROCESSED_WEBHOOK_IDS_FIFO) == 1000

    # Add a new one to trigger eviction of "id-0"
    client = TestClient(app)
    # Mock verify_webhook_signature to return True
    with patch("app.services.github_service.GitHubService.verify_webhook_signature", return_value=True):
        # We need a valid payload structure
        payload = {
            "action": "opened",
            "issue": {"title": "Test", "body": "test bot/reproduce", "number": 1},
            "repository": {"full_name": "owner/repo"}
        }
        
        headers = {
            "x-hub-signature-256": "sha256=dummy",
            "x-github-delivery": "id-1000",
            "Content-Type": "application/json"
        }
        
        # This should trigger eviction of oldest "id-0"
        response = client.post("/webhook", json=payload, headers=headers)
        assert response.status_code == 200
        
        # Verify "id-0" is evicted and "id-1000" is added
        assert "id-0" not in PROCESSED_WEBHOOK_IDS
        assert "id-1000" in PROCESSED_WEBHOOK_IDS
        assert len(PROCESSED_WEBHOOK_IDS) == 1000
        assert len(PROCESSED_WEBHOOK_IDS_FIFO) == 1000
        
        # Sending "id-0" again should now be ACCEPTED (no longer duplicate)
        headers["x-github-delivery"] = "id-0"
        response2 = client.post("/webhook", json=payload, headers=headers)
        assert response2.status_code == 200
        assert response2.json()["status"] == "accepted"


@pytest.mark.asyncio
@respx.mock
async def test_github_service_retries_transient_errors(monkeypatch):
    """
    Verify GitHubService retries on 429, 500, 502, 503, 504 and fails on 400.
    """
    # Disable real sleep during retries
    async def mock_sleep(x):
        pass
    monkeypatch.setattr(asyncio, "sleep", mock_sleep)

    # 1. Test retry on 503 Service Unavailable
    route = respx.get("https://api.github.com/repos/owner/repo/contents/ghost.yml")
    # Make it fail 2 times and then succeed
    route.side_effect = [
        httpx.Response(503, text="Service Unavailable"),
        httpx.Response(503, text="Service Unavailable"),
        httpx.Response(200, text="success-config")
    ]

    res = await GitHubService.get_repo_file("owner/repo", "ghost.yml", "dummy-token")
    assert res == "success-config"
    assert route.call_count == 3

    # 2. Test retry limit reached (5 attempts)
    route.reset()
    route.side_effect = [httpx.Response(500, text="Internal Server Error")] * 6

    res = await GitHubService.get_repo_file("owner/repo", "ghost.yml", "dummy-token")
    assert res == ""  # Graceful failure returning empty string on unhandled exception/max retries
    assert route.call_count == 5

    # 3. Test non-retried error (400 Bad Request)
    route.reset()
    route.side_effect = [httpx.Response(400, text="Bad Request")]
    
    res = await GitHubService.get_repo_file("owner/repo", "ghost.yml", "dummy-token")
    assert res == ""
    assert route.call_count == 1


def test_sandbox_service_offline_docker_graceful(monkeypatch):
    """
    Verify SandboxService handles DockerException gracefully at init and returns safe error dict.
    """
    # Mock docker.from_env to raise DockerException
    def mock_from_env():
        raise DockerException("Failed to connect to Docker")

    monkeypatch.setattr("docker.from_env", mock_from_env)

    sandbox = SandboxService()
    assert sandbox.client is None

    # Call run_reproduction when Docker is offline
    result = sandbox.run_reproduction(MagicMock(), "owner/repo")
    assert "Docker daemon is unreachable or not running" in result["logs"]
    assert result["expected_found"] is False
    assert result["bisect_result"] == ""
