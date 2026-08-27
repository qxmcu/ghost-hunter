import pytest
from app.config import settings
from app.main import get_github_token, app, PROCESSED_WEBHOOK_IDS, PROCESSED_WEBHOOK_IDS_FIFO
from fastapi.testclient import TestClient
import hmac
import hashlib
import json

def test_get_github_token(monkeypatch):
    # Test fallback to hardcoded PAT
    monkeypatch.setattr(settings, "GITHUB_PRIVATE_KEY", "")
    assert get_github_token().startswith("github_pat_")

    # Test returning settings GITHUB_PRIVATE_KEY
    monkeypatch.setattr(settings, "GITHUB_PRIVATE_KEY", "my_custom_key")
    assert get_github_token() == "my_custom_key"

def test_webhook_deduplication(monkeypatch):
    client = TestClient(app)
    
    # Reset deduplication sets
    PROCESSED_WEBHOOK_IDS.clear()
    PROCESSED_WEBHOOK_IDS_FIFO.clear()
    
    # Mock process_issue to avoid running full background logic
    processed_payloads = []
    async def mock_process_issue(payload):
        processed_payloads.append(payload)
    monkeypatch.setattr("app.main.process_issue", mock_process_issue)
    
    payload = {
        "action": "opened",
        "issue": {
            "title": "Deduplication bug report",
            "body": "This is a bug report with bot/reproduce keyword",
            "number": 125
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
        "x-github-delivery": "unique-delivery-id-12345",
        "Content-Type": "application/json"
    }
    
    # First webhook request
    response1 = client.post("/webhook", content=body, headers=headers)
    assert response1.status_code == 200
    assert response1.json()["status"] == "accepted"
    
    # Second webhook request with same X-GitHub-Delivery
    response2 = client.post("/webhook", content=body, headers=headers)
    assert response2.status_code == 200
    assert response2.json() == {"status": "ignored", "reason": "Duplicate webhook request"}
    
    # Verify that background task was only queued once
    assert len(processed_payloads) == 1
