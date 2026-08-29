import pytest
import docker
import respx
import httpx

class MockImage:
    def __init__(self, name):
        self.tags = [name]

class MockImages:
    def __init__(self):
        self.pulled = []

    def get(self, name):
        if "not-found" in name or name not in self.pulled:
            raise docker.errors.ImageNotFound(f"Image {name} not found")
        return MockImage(name)

    def pull(self, name):
        self.pulled.append(name)
        return MockImage(name)

class MockNetwork:
    def __init__(self, net_id):
        self.net_id = net_id

    def disconnect(self, container):
        pass

    def connect(self, container):
        pass

class MockNetworks:
    def get(self, net_id):
        return MockNetwork(net_id)

class MockContainer:
    def __init__(self, image, command=None, environment=None, **kwargs):
        self.image = image
        self.command = command
        self.environment = environment or {}
        self.kwargs = kwargs
        self.attrs = {
            "NetworkSettings": {
                "Networks": {
                    "bridge": {
                        "NetworkID": "bridge-net-id"
                    }
                }
            }
        }

    def reload(self):
        pass

    def exec_run(self, cmd, workdir=None):
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
                else:
                    return (1, f"REGEX FAILED ON: {cmd[2]}".encode())

        if "git clone" in script:
            return (0, b"Stage 1 Setup Success - cloned repo")
        elif "git bisect" in script:
            return (0, b"d3b07384d113edec49eaa6238ad5ff00 is the first bad commit\n")
        elif "LLM Parsing Failed" in script:
            return (1, b"LLM Parsing Failed")
        else:
            return (0, f"FALLBACK RETURN. Script was: {script}, cmd was: {cmd}".encode())

    def remove(self, **kwargs):
        pass

class MockContainers:
    def __init__(self):
        self.active_containers = []

    def run(self, image, command=None, environment=None, **kwargs):
        container = MockContainer(image, command, environment, **kwargs)
        self.active_containers.append(container)
        return container

class MockDockerClient:
    def __init__(self):
        self.images = MockImages()
        self.containers = MockContainers()
        self.networks = MockNetworks()

@pytest.fixture(autouse=True)
def mock_temp_paths(tmp_path, monkeypatch):
    test_db_path = tmp_path / "ghost.db"
    test_audit_path = tmp_path / "audit.log"
    monkeypatch.setattr("app.services.db_service.DB_PATH", test_db_path)
    monkeypatch.setattr("app.services.audit_service.AUDIT_LOG_PATH", test_audit_path)

@pytest.fixture(autouse=True)
def mock_docker(monkeypatch):
    mock_client = MockDockerClient()
    monkeypatch.setattr("docker.from_env", lambda *args, **kwargs: mock_client)
    return mock_client

@pytest.fixture(autouse=True)
def mock_llm_client(monkeypatch):
    class MockChatCompletions:
        async def create(self, *args, **kwargs):
            messages = kwargs.get("messages", [])
            system_prompt = next((m["content"] for m in messages if m["role"] == "system"), "")
            
            # Check if it's the structured output extraction
            if "reproduction_commands" in system_prompt or kwargs.get("response_format", {}).get("type") == "json_object":
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
                content = "Ghost: The reproduction completed successfully! <details><summary>Logs</summary>reproduced</details>"
            
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
            
    class MockChat:
        def __init__(self):
            self.completions = MockChatCompletions()
            
    class MockAsyncOpenAI:
        def __init__(self, *args, **kwargs):
            self.chat = MockChat()
            
    monkeypatch.setattr("app.services.llm_service.client", MockAsyncOpenAI())
    monkeypatch.setattr("openai.AsyncOpenAI", MockAsyncOpenAI)

@pytest.fixture(autouse=True)
def mock_github_api():
    with respx.mock(assert_all_called=False) as respx_mock:
        # Mock GET contents/ghost.yml
        respx_mock.get(url__regex=r"https://api.github.com/repos/[^/]+/[^/]+/contents/ghost.yml").mock(
            return_value=httpx.Response(200, text="ghost:\n  max_retries: 2\n")
        )
        
        # Mock GET comments
        respx_mock.get(url__regex=r"https://api.github.com/repos/[^/]+/[^/]+/issues/\d+/comments\?per_page=100").mock(
            return_value=httpx.Response(200, json=[])
        )
        
        # Mock GET issue
        respx_mock.get(url__regex=r"https://api.github.com/repos/[^/]+/[^/]+/issues/\d+").mock(
            return_value=httpx.Response(200, json={"title": "test issue", "body": "bot/reproduce test"})
        )
        
        # Mock POST comments
        respx_mock.post(url__regex=r"https://api.github.com/repos/[^/]+/[^/]+/issues/\d+/comments").mock(
            return_value=httpx.Response(201, json={"message": "created"})
        )
        
        yield respx_mock

