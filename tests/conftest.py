import sqlite3

import pytest
from fastapi.testclient import TestClient

from lineback.config import Settings
from lineback.main import create_app

SECRET = "test-secret-123"


class RecordingNotifier:
    delivers = True

    def __init__(self):
        self.sent: list[tuple[str, str]] = []
        self.fail = False

    def send(self, subject: str, body: str) -> None:
        if self.fail:
            raise RuntimeError("smtp down")
        self.sent.append((subject, body))


@pytest.fixture
def notifier():
    return RecordingNotifier()


@pytest.fixture
def settings(tmp_path):
    return Settings(webhook_secret=SECRET, db_path=str(tmp_path / "t.db"), business_name="Acme HVAC")


@pytest.fixture
def client(settings, notifier):
    app = create_app(settings, notifier)
    with TestClient(app, raise_server_exceptions=False) as c:
        c.headers.update({"Authorization": f"Bearer {SECRET}"})
        yield c


@pytest.fixture
def rows(settings):
    def query(sql: str, params=()):
        conn = sqlite3.connect(settings.db_path)
        conn.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]
        finally:
            conn.close()

    return query


# --- payload builders -------------------------------------------------------

CALL = {"id": "call-abc", "customer": {"number": "+15551234567"}, "phoneNumber": {"number": "+15557654321"}}


def legacy(name, args, tool_id="tc_1", call=CALL):
    """The `toolCalls` shape your live agent sends today."""
    return {"message": {"type": "tool-calls", "call": call,
                        "toolCalls": [{"id": tool_id, "type": "function",
                                       "function": {"name": name, "arguments": args}}]}}


def listed(name, params, tool_id="tc_1", call=CALL):
    """The `toolCallList` shape from Vapi's documentation."""
    return {"message": {"type": "tool-calls", "call": call,
                        "toolCallList": [{"id": tool_id, "name": name, "parameters": params}]}}
