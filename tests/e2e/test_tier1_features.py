import hmac
import hashlib
import json
import time
import pytest
import docker
import httpx
from fastapi.testclient import TestClient

from app.main import app
from app.config import settings
from app.services.db_service import DBService
from app.services.audit_service import AuditService
from app.services.llm_service import LLMService
from app.schemas import ReproductionContext


def configure_github_mock(
    mock_github_api, ghost_yml_text=None, comments_list=None, issue_json=None
):
    """
    Helper to clear and rebuild respx github routes, preventing regex precedence issues.
    """
    mock_github_api.clear()

    # 1. ghost.yml route
    if ghost_yml_text is not None:
        mock_github_api.get(
            url__regex=r"https://api.github.com/repos/[^/]+/[^/]+/contents/ghost.yml"
        ).mock(return_value=httpx.Response(200, text=ghost_yml_text))
    else:
        mock_github_api.get(
            url__regex=r"https://api.github.com/repos/[^/]+/[^/]+/contents/ghost.yml"
        ).mock(return_value=httpx.Response(404, text="Not Found"))

    # 2. GET comments
    mock_github_api.get(
        url__regex=r"https://api.github.com/repos/[^/]+/[^/]+/issues/\d+/comments\?per_page=100"
    ).mock(
        return_value=httpx.Response(
            200, json=comments_list if comments_list is not None else []
        )
    )

    # 3. GET issue
    mock_github_api.get(
        url__regex=r"https://api.github.com/repos/[^/]+/[^/]+/issues/\d+"
    ).mock(
        return_value=httpx.Response(
            200,
            json=issue_json
            if issue_json is not None
            else {"title": "test", "body": "test"},
        )
    )

    # 4. POST comments
    mock_github_api.post(
        url__regex=r"https://api.github.com/repos/[^/]+/[^/]+/issues/\d+/comments"
    ).mock(return_value=httpx.Response(201, json={"message": "created"}))


@pytest.fixture(autouse=True)
def setup_test_env(monkeypatch, mock_github_api):
    # Ensure GITHUB_PRIVATE_KEY is empty so get_github_token() uses the dummy PAT token
    # (which won't raise header syntax exceptions in httpx)
    monkeypatch.setattr("app.config.settings.GITHUB_PRIVATE_KEY", "")

    # Configure default github mocked responses
    configure_github_mock(mock_github_api, ghost_yml_text="ghost:\n  max_retries: 2\n")

    # Monkeypatch LLMService.generate_conversational_response to be dynamic
    async def mock_generate_conversational_response(
        issue_title: str,
        issue_body: str,
        conversation_history: str,
        sandbox_logs: str,
        success: bool,
        bisect_result: str,
    ) -> str:
        status = "SUCCESS" if success else "FAILED"
        comment = f"Ghost: Reproduction status is {status}.\nLogs:\n{sandbox_logs}"
        if bisect_result:
            comment += f"\nBisect Result: {bisect_result}"
        return comment

    monkeypatch.setattr(
        LLMService,
        "generate_conversational_response",
        mock_generate_conversational_response,
    )

    # Monkeypatch MockContainer.exec_run to return the script content if not clone/bisect
    def mock_exec_run(self, cmd, workdir=None):
        script = ""
        if isinstance(cmd, list):
            if len(cmd) >= 4:
                script = cmd[-1]
            elif len(cmd) >= 3 and "base64 -d" in cmd[2]:
                import base64
                import re
                match = re.search(r"echo ([A-Za-z0-9+/=]+) \| base64 -d", cmd[2])
                if match:
                    script = base64.b64decode(match.group(1)).decode()
        if "git clone" in script:
            return (0, b"Stage 1 Setup Success - cloned repo")
        elif "git bisect" in script:
            return (0, b"d3b07384d113edec49eaa6238ad5ff00 is the first bad commit\n")
        # Default behavior: return the script content so we can see what was executed
        return (0, script.encode("utf-8"))

    monkeypatch.setattr("tests.e2e.conftest.MockContainer.exec_run", mock_exec_run)


# ==============================================================================
# F1: LLM Error Handling (5 Tests)
# ==============================================================================


def test_f1_llm_exception_during_parsing(monkeypatch, mock_github_api):
    """
    Test 1: LLM API throws Exception during parsing: should use fallback context,
    try reproduction, log failure, and comment back.
    """

    class FailCompletions:
        async def create(self, *args, **kwargs):
            raise Exception("LLM API parsing error")

    class FailClient:
        chat = type("Chat", (), {"completions": FailCompletions()})()

    monkeypatch.setattr("app.services.llm_service.client", FailClient())

    client = TestClient(app)

    payload = {
        "action": "opened",
        "issue": {
            "title": "LLM Exc Issue",
            "body": "Test issue body bot/reproduce",
            "number": 101,
        },
        "repository": {"full_name": "testowner/testrepo"},
    }
    body = json.dumps(payload).encode("utf-8")
    signature = hmac.new(
        settings.WEBHOOK_SECRET.encode("utf-8"), msg=body, digestmod=hashlib.sha256
    ).hexdigest()

    headers = {
        "x-hub-signature-256": f"sha256={signature}",
        "Content-Type": "application/json",
    }

    response = client.post("/webhook", content=body, headers=headers)
    assert response.status_code == 200

    # Verify GitHub comment posted
    post_calls = [
        c
        for c in mock_github_api.calls
        if c.request.method == "POST" and "/comments" in str(c.request.url)
    ]
    assert len(post_calls) > 0
    posted_body = json.loads(post_calls[0].request.content.decode("utf-8"))["body"]
    assert "LLM Parsing Failed" in posted_body

    # Verify DB logging
    db = DBService()
    history = db.get_history(101)
    assert len(history) > 0
    assert history[0]["success"] == 0
    assert "LLM Parsing Failed" in history[0]["logs"]


def test_f1_llm_empty_response(monkeypatch, mock_github_api):
    """
    Test 2: LLM API returns empty response: should use fallback context and continue.
    """

    class EmptyCompletions:
        async def create(self, *args, **kwargs):
            class MockResponse:
                choices = []

            return MockResponse()

    class EmptyClient:
        chat = type("Chat", (), {"completions": EmptyCompletions()})()

    monkeypatch.setattr("app.services.llm_service.client", EmptyClient())

    client = TestClient(app)
    payload = {
        "action": "opened",
        "issue": {
            "title": "LLM Empty Issue",
            "body": "bot/reproduce test",
            "number": 102,
        },
        "repository": {"full_name": "testowner/testrepo"},
    }
    body = json.dumps(payload).encode("utf-8")
    signature = hmac.new(
        settings.WEBHOOK_SECRET.encode("utf-8"), msg=body, digestmod=hashlib.sha256
    ).hexdigest()
    headers = {
        "x-hub-signature-256": f"sha256={signature}",
        "Content-Type": "application/json",
    }

    response = client.post("/webhook", content=body, headers=headers)
    assert response.status_code == 200

    db = DBService()
    history = db.get_history(102)
    assert len(history) > 0
    assert history[0]["success"] == 0
    assert "LLM Parsing Failed" in history[0]["logs"]


def test_f1_llm_invalid_json(monkeypatch, mock_github_api):
    """
    Test 3: LLM API returns invalid JSON format (no markdown blocks, just bad JSON):
    should handle it or fallback.
    """

    class BadJSONCompletions:
        async def create(self, *args, **kwargs):
            class MockMessage:
                content = "{bad_json"

            class MockChoice:
                message = MockMessage()

            class MockResponse:
                choices = [MockChoice()]

            return MockResponse()

    class BadJSONClient:
        chat = type("Chat", (), {"completions": BadJSONCompletions()})()

    monkeypatch.setattr("app.services.llm_service.client", BadJSONClient())

    client = TestClient(app)
    payload = {
        "action": "opened",
        "issue": {
            "title": "LLM Bad JSON Issue",
            "body": "bot/reproduce test",
            "number": 103,
        },
        "repository": {"full_name": "testowner/testrepo"},
    }
    body = json.dumps(payload).encode("utf-8")
    signature = hmac.new(
        settings.WEBHOOK_SECRET.encode("utf-8"), msg=body, digestmod=hashlib.sha256
    ).hexdigest()
    headers = {
        "x-hub-signature-256": f"sha256={signature}",
        "Content-Type": "application/json",
    }

    response = client.post("/webhook", content=body, headers=headers)
    assert response.status_code == 200

    db = DBService()
    history = db.get_history(103)
    assert len(history) > 0
    assert history[0]["success"] == 0
    assert "LLM Parsing Failed" in history[0]["logs"]


def test_f1_llm_failed_pydantic_validation(monkeypatch, mock_github_api):
    """
    Test 4: LLM API returns JSON that fails Pydantic schema validation: should fallback.
    """

    class BadPydanticCompletions:
        async def create(self, *args, **kwargs):
            class MockMessage:
                content = '{"base_image": 123, "required_packages": "should_be_list"}'

            class MockChoice:
                message = MockMessage()

            class MockResponse:
                choices = [MockChoice()]

            return MockResponse()

    class BadPydanticClient:
        chat = type("Chat", (), {"completions": BadPydanticCompletions()})()

    monkeypatch.setattr("app.services.llm_service.client", BadPydanticClient())

    client = TestClient(app)
    payload = {
        "action": "opened",
        "issue": {
            "title": "LLM Bad Pydantic Issue",
            "body": "bot/reproduce test",
            "number": 104,
        },
        "repository": {"full_name": "testowner/testrepo"},
    }
    body = json.dumps(payload).encode("utf-8")
    signature = hmac.new(
        settings.WEBHOOK_SECRET.encode("utf-8"), msg=body, digestmod=hashlib.sha256
    ).hexdigest()
    headers = {
        "x-hub-signature-256": f"sha256={signature}",
        "Content-Type": "application/json",
    }

    response = client.post("/webhook", content=body, headers=headers)
    assert response.status_code == 200

    db = DBService()
    history = db.get_history(104)
    assert len(history) > 0
    assert history[0]["success"] == 0
    assert "LLM Parsing Failed" in history[0]["logs"]


def test_f1_security_block_base_image(monkeypatch, mock_github_api):
    """
    Test 5: LLM API returns base image not allowed in ghost.yml (security block):
    should catch ValueError, cancel container run, write log to db/audit, and comment
    security block message to issue.
    """
    # Setup custom ghost.yml with allowed_base_images, using helper to override routing
    configure_github_mock(
        mock_github_api,
        ghost_yml_text="ghost:\n  allowed_base_images:\n    - trusted_image\n",
    )

    # LLM returns untrusted base image
    class SecurityCompletions:
        async def create(self, *args, **kwargs):
            content = """{
                "base_image": "untrusted_image",
                "required_packages": [],
                "env_vars": {},
                "reproduction_commands": ["echo 'should not run'"],
                "expected_error_keywords": [],
                "known_good_commit": null,
                "requires_network": false
            }"""

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

    class SecurityClient:
        chat = type("Chat", (), {"completions": SecurityCompletions()})()

    monkeypatch.setattr("app.services.llm_service.client", SecurityClient())

    # Capture calls to docker containers run to verify container execution was skipped
    run_calls = []

    def mock_run(*args, **kwargs):
        run_calls.append(args)
        return type(
            "DummyContainer",
            (),
            {"attrs": {"NetworkSettings": {"Networks": {}}}},
        )()

    monkeypatch.setattr("tests.e2e.conftest.MockContainers.run", mock_run)

    client = TestClient(app)
    payload = {
        "action": "opened",
        "issue": {
            "title": "Security Issue",
            "body": "bot/reproduce test",
            "number": 105,
        },
        "repository": {"full_name": "testowner/testrepo"},
    }
    body = json.dumps(payload).encode("utf-8")
    signature = hmac.new(
        settings.WEBHOOK_SECRET.encode("utf-8"), msg=body, digestmod=hashlib.sha256
    ).hexdigest()

    headers = {
        "x-hub-signature-256": f"sha256={signature}",
        "Content-Type": "application/json",
    }

    response = client.post("/webhook", content=body, headers=headers)
    assert response.status_code == 200

    # Verify container was NOT run
    assert len(run_calls) == 0

    # Verify comment containing security block message
    post_calls = [
        c
        for c in mock_github_api.calls
        if c.request.method == "POST" and "/comments" in str(c.request.url)
    ]
    assert len(post_calls) > 0
    posted_body = json.loads(post_calls[0].request.content.decode("utf-8"))["body"]
    assert "SECURITY BLOCK" in posted_body
    assert "untrusted_image" in posted_body

    # Verify DB contains logs of the failure
    db = DBService()
    history = db.get_history(105)
    assert len(history) > 0
    assert history[0]["success"] == 0
    assert "SECURITY BLOCK" in history[0]["logs"]

    # Verify Audit trail
    assert AuditService.verify_chain() is True


# ==============================================================================
# F2: Docker Error Handling (5 Tests)
# ==============================================================================


def test_f2_docker_daemon_down(monkeypatch, mock_github_api):
    """
    Test 6: Docker daemon down: SandboxService raises DockerException (or
    docker.from_env() raises it) during initialization or run; should degrade
    gracefully, catch the exception, log "Sandbox Exception", DB log, and post
    comment showing failure.
    """

    def mock_from_env(*args, **kwargs):
        raise docker.errors.DockerException("Docker daemon down")

    monkeypatch.setattr("docker.from_env", mock_from_env)

    client = TestClient(app)
    payload = {
        "action": "opened",
        "issue": {
            "title": "Daemon issue",
            "body": "bot/reproduce test",
            "number": 201,
        },
        "repository": {"full_name": "testowner/testrepo"},
    }
    body = json.dumps(payload).encode("utf-8")
    signature = hmac.new(
        settings.WEBHOOK_SECRET.encode("utf-8"), msg=body, digestmod=hashlib.sha256
    ).hexdigest()
    headers = {
        "x-hub-signature-256": f"sha256={signature}",
        "Content-Type": "application/json",
    }

    response = client.post("/webhook", content=body, headers=headers)
    assert response.status_code == 200

    # Verify comment shows Sandbox Exception
    post_calls = [
        c
        for c in mock_github_api.calls
        if c.request.method == "POST" and "/comments" in str(c.request.url)
    ]
    assert len(post_calls) > 0
    posted_body = json.loads(post_calls[0].request.content.decode("utf-8"))["body"]
    assert "Sandbox Exception" in posted_body

    # DB verification
    db = DBService()
    history = db.get_history(201)
    assert len(history) > 0
    assert history[0]["success"] == 0
    assert "Docker daemon is unreachable" in history[0]["logs"]


def test_f2_docker_image_pull_fails(monkeypatch, mock_docker, mock_github_api):
    """
    Test 7: Docker Image pull fails: docker.errors.ImageNotFound or DockerException
    raised; should log failure, clean up container, and return expected_found=False.
    """

    def mock_pull(name):
        raise docker.errors.ImageNotFound("Image pull failed")

    monkeypatch.setattr(mock_docker.images, "pull", mock_pull)
    # Also raise when get is called so it tries to pull
    monkeypatch.setattr(mock_docker.images, "get", mock_pull)

    client = TestClient(app)
    payload = {
        "action": "opened",
        "issue": {
            "title": "Pull issue",
            "body": "bot/reproduce test",
            "number": 202,
        },
        "repository": {"full_name": "testowner/testrepo"},
    }
    body = json.dumps(payload).encode("utf-8")
    signature = hmac.new(
        settings.WEBHOOK_SECRET.encode("utf-8"), msg=body, digestmod=hashlib.sha256
    ).hexdigest()
    headers = {
        "x-hub-signature-256": f"sha256={signature}",
        "Content-Type": "application/json",
    }

    response = client.post("/webhook", content=body, headers=headers)
    assert response.status_code == 200

    db = DBService()
    history = db.get_history(202)
    assert len(history) > 0
    assert history[0]["success"] == 0
    assert "Image pull failed" in history[0]["logs"]


def test_f2_git_clone_fails(monkeypatch, mock_github_api):
    """
    Test 8: Git clone in Stage 1 fails (exit code != 0): should log Stage 1 setup failure,
    stop, return expected_found=False, and log to DB.
    """

    def mock_exec_run(self, cmd, workdir=None):
        script = cmd[-1] if isinstance(cmd, list) and len(cmd) >= 4 else ""
        if "git clone" in script:
            return (
                128,
                b"fatal: repository 'testowner/testrepo' not found or clone failed",
            )
        return (0, b"")

    monkeypatch.setattr("tests.e2e.conftest.MockContainer.exec_run", mock_exec_run)

    client = TestClient(app)
    payload = {
        "action": "opened",
        "issue": {
            "title": "Clone issue",
            "body": "bot/reproduce test",
            "number": 203,
        },
        "repository": {"full_name": "testowner/testrepo"},
    }
    body = json.dumps(payload).encode("utf-8")
    signature = hmac.new(
        settings.WEBHOOK_SECRET.encode("utf-8"), msg=body, digestmod=hashlib.sha256
    ).hexdigest()
    headers = {
        "x-hub-signature-256": f"sha256={signature}",
        "Content-Type": "application/json",
    }

    response = client.post("/webhook", content=body, headers=headers)
    assert response.status_code == 200

    db = DBService()
    history = db.get_history(203)
    assert len(history) > 0
    assert history[0]["success"] == 0
    assert "clone failed" in history[0]["logs"]


def test_f2_stage2_fails(monkeypatch, mock_github_api):
    """
    Test 9: Stage 2 execution fails (reproduction commands return non-zero exit code):
    should capture traceback in logs, return expected_found=False, and comment back
    with the logs.
    """

    def mock_exec_run(self, cmd, workdir=None):
        script = cmd[-1] if isinstance(cmd, list) and len(cmd) >= 4 else ""
        if "git clone" in script:
            return (0, b"Stage 1 Setup Success - cloned repo")
        # Stage 2 fails
        return (
            1,
            b"Traceback (most recent call last):\nFile 'app.py', line 10\nAssertionError: test fails",
        )

    monkeypatch.setattr("tests.e2e.conftest.MockContainer.exec_run", mock_exec_run)

    client = TestClient(app)
    payload = {
        "action": "opened",
        "issue": {
            "title": "Stage 2 issue",
            "body": "bot/reproduce test",
            "number": 204,
        },
        "repository": {"full_name": "testowner/testrepo"},
    }
    body = json.dumps(payload).encode("utf-8")
    signature = hmac.new(
        settings.WEBHOOK_SECRET.encode("utf-8"), msg=body, digestmod=hashlib.sha256
    ).hexdigest()
    headers = {
        "x-hub-signature-256": f"sha256={signature}",
        "Content-Type": "application/json",
    }

    response = client.post("/webhook", content=body, headers=headers)
    assert response.status_code == 200

    db = DBService()
    history = db.get_history(204)
    assert len(history) > 0
    assert history[0]["success"] == 0
    assert "Traceback" in history[0]["logs"]
    assert "AssertionError" in history[0]["logs"]


def test_f2_container_remove_fails(monkeypatch, mock_github_api):
    """
    Test 10: Container removal fails in finally block: should handle the exception
    gracefully without failing the whole flow.
    """

    def mock_remove(self, **kwargs):
        raise docker.errors.DockerException("Container removal failed")

    monkeypatch.setattr("tests.e2e.conftest.MockContainer.remove", mock_remove)

    client = TestClient(app)
    payload = {
        "action": "opened",
        "issue": {
            "title": "Remove issue",
            "body": "bot/reproduce test",
            "number": 205,
        },
        "repository": {"full_name": "testowner/testrepo"},
    }
    body = json.dumps(payload).encode("utf-8")
    signature = hmac.new(
        settings.WEBHOOK_SECRET.encode("utf-8"), msg=body, digestmod=hashlib.sha256
    ).hexdigest()
    headers = {
        "x-hub-signature-256": f"sha256={signature}",
        "Content-Type": "application/json",
    }

    response = client.post("/webhook", content=body, headers=headers)
    assert response.status_code == 200

    db = DBService()
    history = db.get_history(205)
    assert len(history) > 0
    assert history[0]["success"] == 1  # still succeeds despite remove failure!


# ==============================================================================
# F3: GitHub API Failures (5 Tests)
# ==============================================================================


def test_f3_ghost_yml_missing(monkeypatch, mock_github_api):
    """
    Test 11: ghost.yml is missing (returns 404): should default to default_config.
    """
    # Setup: ghost.yml missing (404), using configure_github_mock helper
    configure_github_mock(mock_github_api, ghost_yml_text=None)

    # We want to verify how many times the reproduction attempt runs.
    # Default config has max_retries = 3.
    attempts = []

    def mock_exec_run(self, cmd, workdir=None):
        script = cmd[-1] if isinstance(cmd, list) and len(cmd) >= 4 else ""
        if "git clone" in script:
            return (0, b"Stage 1 Setup Success")
        attempts.append(cmd)
        return (0, b"normal log")

    monkeypatch.setattr("tests.e2e.conftest.MockContainer.exec_run", mock_exec_run)

    client = TestClient(app)
    payload = {
        "action": "opened",
        "issue": {
            "title": "Missing config issue",
            "body": "bot/reproduce test",
            "number": 301,
        },
        "repository": {"full_name": "testowner/testrepo"},
    }
    body = json.dumps(payload).encode("utf-8")
    signature = hmac.new(
        settings.WEBHOOK_SECRET.encode("utf-8"), msg=body, digestmod=hashlib.sha256
    ).hexdigest()
    headers = {
        "x-hub-signature-256": f"sha256={signature}",
        "Content-Type": "application/json",
    }

    response = client.post("/webhook", content=body, headers=headers)
    assert response.status_code == 200

    # With default config, max_retries = 3.
    assert len(attempts) == 3


def test_f3_webhook_signature_invalid():
    """
    Test 12: Webhook signature is invalid: returns 401 Unauthorized.
    """
    client = TestClient(app)
    payload = {
        "action": "opened",
        "issue": {"title": "X", "number": 302},
        "repository": {"full_name": "X/Y"},
    }
    body = json.dumps(payload).encode("utf-8")
    headers = {
        "x-hub-signature-256": "sha256=invalid_signature",
        "Content-Type": "application/json",
    }

    response = client.post("/webhook", content=body, headers=headers)
    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid signature"}


def test_f3_webhook_payload_malformed():
    """
    Test 13: Webhook payload is malformed JSON: returns 400 Bad Request.
    """
    client = TestClient(app)
    body = b"invalid json {"
    signature = hmac.new(
        settings.WEBHOOK_SECRET.encode("utf-8"), msg=body, digestmod=hashlib.sha256
    ).hexdigest()
    headers = {
        "x-hub-signature-256": f"sha256={signature}",
        "Content-Type": "application/json",
    }

    response = client.post("/webhook", content=body, headers=headers)
    assert response.status_code == 400
    assert response.json() == {"detail": "Invalid payload"}


def test_f3_post_comment_fails(monkeypatch, mock_github_api):
    """
    Test 14: Posting issue comment fails (GitHub returns 403/422): should handle
    HTTPStatusError and log it, without crashing.
    """
    # Mock POST /comments to fail with 403
    configure_github_mock(mock_github_api)
    mock_github_api.post(
        url__regex=r"https://api.github.com/repos/testowner/testrepo/issues/\d+/comments"
    ).mock(return_value=httpx.Response(403, text="Forbidden"))

    client = TestClient(app)
    payload = {
        "action": "opened",
        "issue": {
            "title": "Post comment fail issue",
            "body": "bot/reproduce test",
            "number": 304,
        },
        "repository": {"full_name": "testowner/testrepo"},
    }
    body = json.dumps(payload).encode("utf-8")
    signature = hmac.new(
        settings.WEBHOOK_SECRET.encode("utf-8"), msg=body, digestmod=hashlib.sha256
    ).hexdigest()
    headers = {
        "x-hub-signature-256": f"sha256={signature}",
        "Content-Type": "application/json",
    }

    response = client.post("/webhook", content=body, headers=headers)
    assert response.status_code == 200

    # Verify that DB still has the log entry (proves no unhandled exception crashed the background task)
    db = DBService()
    history = db.get_history(304)
    assert len(history) > 0
    assert history[0]["success"] == 1


def test_f3_get_comments_fails(monkeypatch, mock_github_api):
    """
    Test 15: Getting issue comments returns empty list or fails: should default to
    empty string conversation history and proceed.
    """
    # Mock GET /comments to fail with 500
    configure_github_mock(mock_github_api)
    mock_github_api.get(
        url__regex=r"https://api.github.com/repos/testowner/testrepo/issues/\d+/comments\?per_page=100"
    ).mock(return_value=httpx.Response(500, text="Internal Server Error"))

    # We record comments passed to the LLM
    llm_calls = []

    async def mock_extract(
        issue_title, issue_body, conversation_history, previous_attempts
    ):
        llm_calls.append(conversation_history)
        # Return default ReproductionContext
        return ReproductionContext(
            base_image="ubuntu:22.04",
            required_packages=[],
            env_vars={},
            reproduction_commands=["echo 'ok'"],
            expected_error_keywords=["ok"],
        )

    monkeypatch.setattr(
        "app.services.llm_service.LLMService.extract_reproduction_context",
        mock_extract,
    )

    client = TestClient(app)
    payload = {
        "action": "opened",
        "issue": {
            "title": "Get comments fail issue",
            "body": "bot/reproduce test",
            "number": 305,
        },
        "repository": {"full_name": "testowner/testrepo"},
    }
    body = json.dumps(payload).encode("utf-8")
    signature = hmac.new(
        settings.WEBHOOK_SECRET.encode("utf-8"), msg=body, digestmod=hashlib.sha256
    ).hexdigest()
    headers = {
        "x-hub-signature-256": f"sha256={signature}",
        "Content-Type": "application/json",
    }

    response = client.post("/webhook", content=body, headers=headers)
    assert response.status_code == 200

    # Verify that conversation history passed to LLM was empty string
    assert len(llm_calls) > 0
    assert llm_calls[0] == ""


# ==============================================================================
# F4: Network Resiliency (5 Tests)
# ==============================================================================


def test_f4_rate_limiter_middleware(monkeypatch):
    """
    Test 16: Rate limiter middleware: client IP requests > 150 times per minute,
    webhook returns 429.
    """
    from app.main import RATE_LIMIT_DB, MAX_REQUESTS

    # Populate with 150 requests from testclient
    ip = "testclient"
    RATE_LIMIT_DB[ip] = [time.time()] * MAX_REQUESTS

    client = TestClient(app)
    payload = {
        "action": "opened",
        "issue": {
            "title": "Rate limit test",
            "body": "bot/reproduce",
            "number": 401,
        },
        "repository": {"full_name": "testowner/testrepo"},
    }
    body = json.dumps(payload).encode("utf-8")
    signature = hmac.new(
        settings.WEBHOOK_SECRET.encode("utf-8"), msg=body, digestmod=hashlib.sha256
    ).hexdigest()
    headers = {
        "x-hub-signature-256": f"sha256={signature}",
        "Content-Type": "application/json",
    }

    response = client.post("/webhook", content=body, headers=headers)
    assert response.status_code == 429
    assert "Too Many Requests" in response.json()["detail"]

    # Clean up after test
    RATE_LIMIT_DB.clear()


def test_f4_sandbox_run_requires_network_false(monkeypatch, mock_github_api):
    """
    Test 17: Sandbox run with requires_network=False: verifies that Stage 2 execution
    disconnects network from the container.
    """

    class NetworkFalseCompletions:
        async def create(self, *args, **kwargs):
            content = """{
                "base_image": "ubuntu:22.04",
                "required_packages": [],
                "env_vars": {},
                "reproduction_commands": ["echo 'reproduced'"],
                "expected_error_keywords": ["reproduced"],
                "known_good_commit": null,
                "requires_network": false
            }"""

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

    class NetworkFalseClient:
        chat = type("Chat", (), {"completions": NetworkFalseCompletions()})()

    monkeypatch.setattr("app.services.llm_service.client", NetworkFalseClient())

    # Track disconnect calls
    disconnect_calls = []

    def mock_disconnect(self, container):
        disconnect_calls.append(container)

    monkeypatch.setattr("tests.e2e.conftest.MockNetwork.disconnect", mock_disconnect)

    client = TestClient(app)
    payload = {
        "action": "opened",
        "issue": {
            "title": "Net false issue",
            "body": "bot/reproduce test",
            "number": 402,
        },
        "repository": {"full_name": "testowner/testrepo"},
    }
    body = json.dumps(payload).encode("utf-8")
    signature = hmac.new(
        settings.WEBHOOK_SECRET.encode("utf-8"), msg=body, digestmod=hashlib.sha256
    ).hexdigest()
    headers = {
        "x-hub-signature-256": f"sha256={signature}",
        "Content-Type": "application/json",
    }

    response = client.post("/webhook", content=body, headers=headers)
    assert response.status_code == 200

    # Verify that network disconnect was indeed called
    assert len(disconnect_calls) > 0


def test_f4_sandbox_run_requires_network_true(monkeypatch, mock_github_api):
    """
    Test 18: Sandbox run with requires_network=True: verifies that Stage 2 execution
    does NOT disconnect network.
    """
    
    async def mock_get_repo_file(repo, path, token):
        return "ghost:\n  allowed_network_access: true\n"
    monkeypatch.setattr("app.services.github_service.GitHubService.get_repo_file", mock_get_repo_file)

    class NetworkTrueCompletions:
        async def create(self, *args, **kwargs):
            content = """{
                "base_image": "ubuntu:22.04",
                "required_packages": [],
                "env_vars": {},
                "reproduction_commands": ["echo 'reproduced'"],
                "expected_error_keywords": ["reproduced"],
                "known_good_commit": null,
                "requires_network": true
            }"""

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

    class NetworkTrueClient:
        chat = type("Chat", (), {"completions": NetworkTrueCompletions()})()

    monkeypatch.setattr("app.services.llm_service.client", NetworkTrueClient())

    # Track disconnect calls
    disconnect_calls = []

    def mock_disconnect(self, container):
        disconnect_calls.append(container)

    monkeypatch.setattr("tests.e2e.conftest.MockNetwork.disconnect", mock_disconnect)

    client = TestClient(app)
    payload = {
        "action": "opened",
        "issue": {
            "title": "Net true issue",
            "body": "bot/reproduce test",
            "number": 403,
        },
        "repository": {"full_name": "testowner/testrepo"},
    }
    body = json.dumps(payload).encode("utf-8")
    signature = hmac.new(
        settings.WEBHOOK_SECRET.encode("utf-8"), msg=body, digestmod=hashlib.sha256
    ).hexdigest()
    headers = {
        "x-hub-signature-256": f"sha256={signature}",
        "Content-Type": "application/json",
    }

    response = client.post("/webhook", content=body, headers=headers)
    assert response.status_code == 200

    # Verify that network disconnect was NOT called
    assert len(disconnect_calls) == 0


def test_f4_retry_loop_max_retries(monkeypatch, mock_github_api):
    """
    Test 19: Retry loop executes up to max_retries times: if reproduction fails to
    find keywords, LLM is called again with previous logs appended, up to max_retries times.
    """
    # Configure max_retries: 2
    configure_github_mock(mock_github_api, ghost_yml_text="ghost:\n  max_retries: 2\n")

    # Track LLM extract_reproduction_context calls
    llm_calls = []
    from app.services.llm_service import LLMService

    original_extract = LLMService.extract_reproduction_context

    async def mock_extract(
        issue_title, issue_body, conversation_history, previous_attempts
    ):
        llm_calls.append(previous_attempts)
        return await original_extract(
            issue_title, issue_body, conversation_history, previous_attempts
        )

    monkeypatch.setattr(LLMService, "extract_reproduction_context", mock_extract)

    # Mock reproduction execution to fail (return expected_found=False)
    def mock_exec_run(self, cmd, workdir=None):
        script = ""
        if isinstance(cmd, list):
            if len(cmd) >= 4:
                script = cmd[-1]
            elif len(cmd) >= 3 and "base64 -d" in cmd[2]:
                import base64
                import re
                match = re.search(r"echo ([A-Za-z0-9+/=]+) \| base64 -d", cmd[2])
                if match:
                    script = base64.b64decode(match.group(1)).decode()
        if "git clone" in script:
            return (0, b"Stage 1 Setup Success")
        return (1, b"some failure log output")

    monkeypatch.setattr("tests.e2e.conftest.MockContainer.exec_run", mock_exec_run)

    client = TestClient(app)
    payload = {
        "action": "opened",
        "issue": {
            "title": "Retry test issue",
            "body": "bot/reproduce test",
            "number": 404,
        },
        "repository": {"full_name": "testowner/testrepo"},
    }
    body = json.dumps(payload).encode("utf-8")
    signature = hmac.new(
        settings.WEBHOOK_SECRET.encode("utf-8"), msg=body, digestmod=hashlib.sha256
    ).hexdigest()
    headers = {
        "x-hub-signature-256": f"sha256={signature}",
        "Content-Type": "application/json",
    }

    response = client.post("/webhook", content=body, headers=headers)
    assert response.status_code == 200

    # Verify LLM was extracted twice
    assert len(llm_calls) == 2
    # First call: previous_attempts is empty
    assert llm_calls[0] == ""
    # Second call: previous_attempts contains the logs of attempt 1
    assert "Attempt 1 Logs:" in llm_calls[1]
    assert "some failure log output" in llm_calls[1]


def test_f4_webhook_trigger_keyword_checks(mock_github_api):
    """
    Test 20: Webhook trigger keyword checks: checking bot/reproduce thread responses
    and replies (is_issue_trigger, is_comment_trigger, is_thread_reply).
    """
    client = TestClient(app)

    def build_request(payload):
        body = json.dumps(payload).encode("utf-8")
        signature = hmac.new(
            settings.WEBHOOK_SECRET.encode("utf-8"), msg=body, digestmod=hashlib.sha256
        ).hexdigest()
        return body, {
            "x-hub-signature-256": f"sha256={signature}",
            "Content-Type": "application/json",
        }

    # 1. is_issue_trigger: opened with keyword
    payload1 = {
        "action": "opened",
        "issue": {
            "title": "X",
            "body": "Please bot/reproduce this",
            "number": 451,
        },
        "repository": {"full_name": "testowner/testrepo"},
    }
    body1, headers1 = build_request(payload1)
    res1 = client.post("/webhook", content=body1, headers=headers1)
    assert res1.status_code == 200
    assert res1.json()["status"] == "accepted"

    # 2. is_comment_trigger: comment created with keyword
    payload2 = {
        "action": "created",
        "issue": {"title": "X", "body": "normal body", "number": 452},
        "comment": {"body": "bot/reproduce here"},
        "repository": {"full_name": "testowner/testrepo"},
    }
    body2, headers2 = build_request(payload2)
    res2 = client.post("/webhook", content=body2, headers=headers2)
    assert res2.status_code == 200
    assert res2.json()["status"] == "accepted"

    # 3. is_thread_reply: comment created without keyword, but issue has it
    payload3 = {
        "action": "created",
        "issue": {"title": "X", "body": "some bot/reproduce issue", "number": 453},
        "comment": {"body": "any follow up comment"},
        "repository": {"full_name": "testowner/testrepo"},
    }
    body3, headers3 = build_request(payload3)
    res3 = client.post("/webhook", content=body3, headers=headers3)
    assert res3.status_code == 200
    assert res3.json()["status"] == "accepted"

    # 4. Bot's own comment: ignored
    payload4 = {
        "action": "created",
        "issue": {"title": "X", "body": "some bot/reproduce issue", "number": 454},
        "comment": {"body": "any follow up comment <!-- ghost-bot-signature -->"},
        "repository": {"full_name": "testowner/testrepo"},
    }
    body4, headers4 = build_request(payload4)
    res4 = client.post("/webhook", content=body4, headers=headers4)
    assert res4.status_code == 200
    assert res4.json()["status"] == "ignored"
    assert "bot's own comment" in res4.json()["reason"]

    # 5. No keyword anywhere: ignored
    payload5 = {
        "action": "opened",
        "issue": {"title": "X", "body": "just a normal issue", "number": 455},
        "repository": {"full_name": "testowner/testrepo"},
    }
    body5, headers5 = build_request(payload5)
    res5 = client.post("/webhook", content=body5, headers=headers5)
    assert res5.status_code == 200
    assert res5.json()["status"] == "ignored"
    assert "No explicit trigger keyword" in res5.json()["reason"]
