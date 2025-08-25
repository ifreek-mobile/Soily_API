import pytest
from fastapi.testclient import TestClient
from app.main import app


@pytest.fixture
def client():
    return TestClient(app)


class DummyResp:
    def __init__(self, text: str):
        # routers 内で getattr(resp, "output_text", None) を参照するため
        self.output_text = text
