import asyncio
import hashlib
import hmac
import json
import docker
import httpx
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.services.db_service import DBService
from app.services.llm_service import LLMService
from tests.e2e.conftest import MockContainer


def send_webhook(client, payload):
    body = json.dumps(payload).encode("utf-8")
    signature = hmac.new(
        settings.WEBHOOK_SECRET.encode("utf-8"), msg=body, digestmod=hashlib.sha256
    ).hexdigest()
    headers = {
        "x-hub-signature-256": f"sha256={signature}",
        "Content-Type": "application/json",
    }
    return client.post("/webhook", content=body, headers=headers)


def test_docker_down_and_security_block(mock_github_api, monkeypatch):
    """
    Test 1: Docker daemon is down + LLM security block.
    Checks that if Docker is down but a security block occurs first, the flow exits on the
    security block without checking Docker (i.e., we don't hit the sandbox check).
    """

    # Mock docker.from_env to raise DockerException
    def mock_from_env(*args, **kwargs):
        raise docker.errors.DockerException("Docker daemon is down")

    monkeypatch.setattr(docker, "from_env", mock_from_env)

    # Mock LLM conversational response to reflect failure
    async def mock_generate(
        issue_title,
        issue_body,
        conversation_history,
        sandbox_logs,
        success,
        bisect_result,
    ):
        return f"Ghost: Security check failed. Details: {sandbox_logs} <!-- ghost-bot-signature -->"

    monkeypatch.setattr(
        LLMService, "generate_conversational_response", staticmethod(mock_generate)
    )

    # Mock ghost.yml to restrict allowed base images
    for route in mock_github_api.routes:
        if "ghost.yml" in str(route.pattern):
            route.return_value = httpx.Response(
                200,
                text="ghost:\n  max_retries: 2\n  allowed_base_images:\n    - ubuntu:20.04\n",
            )

    client = TestClient(app)

    payload = {
        "action": "opened",
        "issue": {
            "title": "A bug report",
            "body": "This is a bug report with bot/reproduce keyword",
            "number": 101,
        },
        "repository": {"full_name": "testowner/testrepo"},
    }

    response = send_webhook(client, payload)
    assert response.status_code == 200
    assert response.json() == {
        "status": "accepted",
        "message": "Reproduction started in background",
    }

    # Verify database logging
    db = DBService()
    history = db.get_history(101)
    assert len(history) > 0
    assert history[0]["success"] == 0
    assert "SECURITY BLOCK" in history[0]["logs"]
    assert "Docker daemon is unreachable or not running" not in history[0]["logs"]

    # Verify posted comment
    post_calls = [
        call
        for call in mock_github_api.calls
        if call.request.method == "POST" and "/comments" in str(call.request.url)
    ]
    assert len(post_calls) > 0
    comment_posted = json.loads(post_calls[0].request.content.decode("utf-8"))["body"]
    assert "SECURITY BLOCK" in comment_posted
    assert "Docker daemon is unreachable" not in comment_posted


def test_github_500_llm_malformed_docker_pull_fails(
    mock_github_api, mock_docker, monkeypatch
):
    """
    Test 2: GitHub API 500 error on fetching comments + LLM returns malformed JSON + Docker fails on pull.
    Checks that the workflow handles all three failures gracefully, logs the error, and completes.
    """
    # 1. GitHub comments GET returns 500
    for route in mock_github_api.routes:
        if "comments" in str(route.pattern) and "per_page" in str(route.pattern):
            route.return_value = httpx.Response(500, text="Internal Server Error")

    # Mock asyncio.sleep to run instantly
    async def mock_sleep(delay):
        pass

    monkeypatch.setattr(asyncio, "sleep", mock_sleep)

    # 2. LLM returns malformed JSON
    class MalformedMockChatCompletions:
        async def create(self, *args, **kwargs):
            messages = kwargs.get("messages", [])
            system_prompt = next(
                (m["content"] for m in messages if m["role"] == "system"), ""
            )
            if (
                "reproduction_commands" in system_prompt
                or kwargs.get("response_format", {}).get("type") == "json_object"
            ):
                content = "{ malformed json: true "
            else:
                content = "Ghost: Fallback triggered due to parsing failure! <details><summary>Logs</summary>reproduced</details>"

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

    class MalformedMockChat:
        def __init__(self):
            self.completions = MalformedMockChatCompletions()

    class MalformedMockAsyncOpenAI:
        def __init__(self, *args, **kwargs):
            self.chat = MalformedMockChat()

    monkeypatch.setattr("app.services.llm_service.client", MalformedMockAsyncOpenAI())

    # 3. Docker pull fails
    def mock_get(name):
        raise docker.errors.ImageNotFound(f"Image {name} not found")

    def mock_pull(name):
        raise docker.errors.DockerException("Simulated pull failure")

    monkeypatch.setattr(mock_docker.images, "get", mock_get)
    monkeypatch.setattr(mock_docker.images, "pull", mock_pull)

    client = TestClient(app)

    payload = {
        "action": "opened",
        "issue": {
            "title": "A bug report",
            "body": "This is a bug report with bot/reproduce keyword",
            "number": 102,
        },
        "repository": {"full_name": "testowner/testrepo"},
    }

    response = send_webhook(client, payload)
    assert response.status_code == 200

    # Verify DB logging contains "Simulated pull failure" (since it fails on pull)
    db = DBService()
    history = db.get_history(102)
    assert len(history) > 0
    assert history[0]["success"] == 0
    assert "Simulated pull failure" in history[0]["logs"]

    # Verify that the GitHub comments post was attempted (shows post comment called)
    post_calls = [
        call
        for call in mock_github_api.calls
        if call.request.method == "POST" and "/comments" in str(call.request.url)
    ]
    assert len(post_calls) > 0
    comment_posted = json.loads(post_calls[0].request.content.decode("utf-8"))["body"]
    assert (
        "Fallback triggered due to parsing failure" in comment_posted
        or "Simulated pull failure" in comment_posted
    )


def test_rate_limit_and_bot_comments(mock_github_api, monkeypatch):
    """
    Test 3: Webhook rate limit reached + bot's own comments ignored.
    Checks that rate limit middleware returns 429 after MAX_REQUESTS is reached,
    and checks that bot's own comments (containing the bot signature) are ignored (return status 200).
    """
    from app.main import RATE_LIMIT_DB

    RATE_LIMIT_DB.clear()

    # Set rate limit low (e.g. 2)
    monkeypatch.setattr("app.main.MAX_REQUESTS", 2)

    client = TestClient(app)

    bot_payload = {
        "action": "created",
        "issue": {"title": "A bug report", "body": "bot/reproduce key", "number": 103},
        "repository": {"full_name": "testowner/testrepo"},
        "comment": {"body": "I am a bot response <!-- ghost-bot-signature -->"},
    }

    normal_payload = {
        "action": "opened",
        "issue": {"title": "A bug report", "body": "bot/reproduce key", "number": 103},
        "repository": {"full_name": "testowner/testrepo"},
    }

    # Request 1: Bot comment. It is verified, rate limit check records IP request,
    # but the comment itself is ignored.
    resp1 = send_webhook(client, bot_payload)
    assert resp1.status_code == 200
    assert resp1.json() == {"status": "ignored", "reason": "Ignoring bot's own comment"}

    # Request 2: Normal webhook. Permitted by rate limit (2nd request).
    resp2 = send_webhook(client, normal_payload)
    assert resp2.status_code == 200
    assert resp2.json() == {
        "status": "accepted",
        "message": "Reproduction started in background",
    }

    # Request 3: Normal webhook. Exceeds limit (3rd request), gets 429.
    resp3 = send_webhook(client, normal_payload)
    assert resp3.status_code == 429
    assert resp3.json() == {"detail": "Too Many Requests. Please slow down."}


def test_git_bisect_execution_failure(mock_github_api, monkeypatch):
    """
    Test 4: Git bisect is executed but the docker run inside bisect fails or grep fails.
    Checks that if bisect run fails or grep fails, the reproduction itself is still success=True,
    but bisect_result is recorded as empty.
    """
    original_exec_run = MockContainer.exec_run

    def mock_exec_run(self, cmd, workdir=None):
        script = ""
        if isinstance(cmd, list) and len(cmd) >= 4:
            script = cmd[-1]

        if "git bisect" in script:
            # Simulate grep/docker run inside bisect run failing
            return (
                1,
                b"git bisect run failed: grep did not find the expected keyword, or run exited 1",
            )

        return original_exec_run(self, cmd, workdir)

    monkeypatch.setattr(MockContainer, "exec_run", mock_exec_run)

    client = TestClient(app)

    payload = {
        "action": "opened",
        "issue": {
            "title": "A bug report",
            "body": "This is a bug report with bot/reproduce keyword",
            "number": 104,
        },
        "repository": {"full_name": "testowner/testrepo"},
    }

    response = send_webhook(client, payload)
    assert response.status_code == 200

    # Verify database logging shows success=True but bisect_result is empty
    db = DBService()
    history = db.get_history(104)
    assert len(history) > 0
    assert history[0]["success"] == 1
    assert history[0]["bisect_result"] == ""
    assert "git bisect run failed" in history[0]["logs"]
