import hmac
import hashlib
import json
from fastapi.testclient import TestClient
from app.main import app
from app.config import settings
from app.services.db_service import DBService
from app.services.audit_service import AuditService

def test_health():
    client = TestClient(app)
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "ghost-hunter"}

def test_webhook_accepted(mock_github_api):
    client = TestClient(app)
    
    payload = {
        "action": "opened",
        "issue": {
            "title": "A bug report",
            "body": "This is a bug report with bot/reproduce keyword",
            "number": 123
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
    
    # Verify that the GitHub comments were requested (respx captured GET and POST calls)
    post_calls = [
        call for call in mock_github_api.calls 
        if call.request.method == "POST" and "/comments" in str(call.request.url)
    ]
    assert len(post_calls) > 0, "Webhook did not trigger the background task or post comment"
    
    # Verify database logging
    db = DBService()
    history = db.get_history(123)
    assert len(history) > 0
    assert history[0]["success"] == 1
    assert "d3b07384d113edec49eaa6238ad5ff00" in history[0]["bisect_result"]
    
    # Verify audit chain integrity
    assert AuditService.verify_chain() is True
