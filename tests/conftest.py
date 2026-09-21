from __future__ import annotations

import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
os.environ["CARBON_DB"] = str(ROOT / "tests" / "_test_carbon.db")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


@pytest.fixture()
def client():
    db_file = Path(os.environ["CARBON_DB"])
    if db_file.exists():
        db_file.unlink()
    with TestClient(app) as test_client:
        yield test_client
    if db_file.exists():
        db_file.unlink()
