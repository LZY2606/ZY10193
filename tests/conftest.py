from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_path = tmp_path / "test-carbon.db"
    monkeypatch.setenv("CARBON_DB", str(db_path))
    from app.main import app

    with TestClient(app) as test_client:
        yield test_client
