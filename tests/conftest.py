import pytest
from pathlib import Path

@pytest.fixture(autouse=True)
def mock_db_paths(tmp_path, monkeypatch):
    test_db_path = tmp_path / "ghost.db"
    test_db_path.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("app.main.DB_PATH", test_db_path)
    monkeypatch.setattr("app.services.db_service.DB_PATH", test_db_path)
