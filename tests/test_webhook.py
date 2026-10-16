import json

import pytest
from fastapi.testclient import TestClient

from lineback import handlers
from lineback.config import Settings
from lineback.main import create_app

from conftest import SECRET, CALL, RecordingNotifier, legacy, listed

BOOK = {"customerName": "Jane Roe", "serviceAddress": "12 Oak St", "serviceType": "AC repair",
        "preferredTime": "tomorrow morning", "notes": "dog in yard"}


def post(client, payload, **kw):
    return client.post("/webhook", json=payload, **kw)


# ---------------------------------------------------------------- auth

class TestAuth:
    def test_rejects_missing_and_wrong_secret(self, client):
        client.headers.pop("Authorization")
        assert post(client, listed("checkAvailability", {})).status_code == 401
        assert post(client, listed("checkAvailability", {}),
                    headers={"Authorization": "Bearer nope"}).status_code == 401
        assert post(client, listed("checkAvailability", {}),
                    headers={"X-Vapi-Secret": "nope"}).status_code == 401

    def test_accepts_bearer_and_legacy_header(self, client):
        client.headers.pop("Authorization")
        ok = post(client, listed("checkAvailability", {}), headers={"Authorization": f"Bearer {SECRET}"})
        legacy_ok = post(client, listed("checkAvailability", {}), headers={"X-Vapi-Secret": SECRET})
        assert ok.status_code == legacy_ok.status_code == 200

    def test_fails_closed_when_no_secret_configured(self, tmp_path, notifier):
        s = Settings(db_path=str(tmp_path / "a.db"))
        with TestClient(create_app(s, notifier), raise_server_exceptions=False) as c:
            assert c.post("/webhook", json=listed("checkAvailability", {})).status_code == 503

    def test_insecure_mode_is_explicit_opt_in(self, tmp_path, notifier):
        s = Settings(db_path=str(tmp_path / "b.db"), allow_insecure=True)
        with TestClient(create_app(s, notifier)) as c:
            assert c.post("/webhook", json=listed("checkAvailability", {})).status_code == 200

    def test_health_needs_no_auth(self, client):
        client.headers.pop("Authorization")
        assert client.get("/health").json() == {"status": "ok"}


# ---------------------------------------------------------------- bad input

class TestBadInput:
    def test_malformed_json_is_400_not_500(self, client):
        r = client.post("/webhook", content=b"not json", headers={"content-type": "application/json"})
        assert r.status_code == 400

    @pytest.mark.parametrize("body", [[], "x", 5, {}, {"message": "str"}])
    def test_non_envelope_is_400(self, client, body):
        assert client.post("/webhook", json=body).status_code == 400

    def test_oversized_payload_is_413(self, tmp_path, notifier):
        s = Settings(webhook_secret=SECRET, db_path=str(tmp_path / "c.db"), max_body_bytes=200)
        with TestClient(create_app(s, notifier)) as c:
            r = c.post("/webhook", json={"message": {"type": "x", "pad": "y" * 500}},
                       headers={"Authorization": f"Bearer {SECRET}"})
            assert r.status_code == 413

    def test_unhandled_message_types_are_acknowledged(self, client):
        assert post(client, {"message": {"type": "status-update"}}).json() == {"received": True}
        r = post(client, {"message": {"type": "end-of-call-report", "durationSeconds": 183}})
        assert r.json() == {"received": True}

    def test_tool_call_problems_never_500(self, client):
        payload = {"message": {"type": "tool-calls", "toolCalls": [
            {"id": "a"},                                   # no function at all
            {"id": "b", "function": {"name": "nope"}},     # unknown tool
            {"function": {"name": "checkAvailability"}},   # no id: cannot be answered
            "garbage",
            {"id": "c", "function": {"name": "checkAvailability", "arguments": "{not json"}},
        ]}}
        r = post(client, payload)
        assert r.status_code == 200
        results = {x["toolCallId"]: x["result"] for x in r.json()["results"]}
        assert set(results) == {"a", "b", "c"}
        assert "not available" in results["b"]

    def test_no_tool_calls_gives_empty_results(self, client):
        assert post(client, {"message": {"type": "tool-calls"}}).json() == {"results": []}


# ---------------------------------------------------------------- availability

class TestAvailability:
    def test_never_invents_openings(self, client):
        r = post(client, legacy("checkAvailability", {"date": "2026-10-15"})).json()
        text = r["results"][0]["result"]
        assert r["results"][0]["toolCallId"] == "tc_1"
        assert r["results"][0]["name"] == "checkAvailability"
        assert "9am" not in text and "openings" not in text
        assert "Do not offer or promise specific times" in text


# ---------------------------------------------------------------- booking

class TestBooking:
    def test_records_a_request_and_never_says_booked(self, client, rows):
        r = post(client, legacy("bookAppointment", BOOK)).json()["results"][0]["result"]
        assert "LB-0001" in r and "NOT confirmed" in r
        assert "Confirmation sent" not in r and "Booked a" not in r
        row = rows("SELECT * FROM booking_requests")[0]
        assert row["status"] == "pending"
        assert (row["customer_name"], row["service_type"], row["preferred_time"]) == (
            "Jane Roe", "AC repair", "tomorrow morning")
        assert row["customer_phone"] == CALL["customer"]["number"]   # caller ID fallback
        assert row["call_id"] == "call-abc"
        assert rows("SELECT customer_number FROM calls")[0]["customer_number"] == "+15551234567"

    def test_documented_toolcalllist_shape_works(self, client, rows):
        r = post(client, listed("bookAppointment", BOOK)).json()["results"][0]["result"]
        assert "LB-0001" in r and len(rows("SELECT * FROM booking_requests")) == 1

    def test_arguments_as_json_string_work(self, client, rows):
        payload = legacy("bookAppointment", json.dumps(BOOK))
        assert "LB-0001" in post(client, payload).json()["results"][0]["result"]
        assert rows("SELECT customer_name FROM booking_requests")[0]["customer_name"] == "Jane Roe"

    def test_original_tool_schema_still_works(self, client, rows):
        old = {"date": "Friday 2pm", "serviceType": "furnace"}      # what your live tool sends today
        assert "LB-0001" in post(client, legacy("bookAppointment", old)).json()["results"][0]["result"]
        assert rows("SELECT preferred_time FROM booking_requests")[0]["preferred_time"] == "Friday 2pm"

    def test_missing_time_asks_instead_of_saving(self, client, rows):
        r = post(client, legacy("bookAppointment", {"serviceType": "AC"})).json()["results"][0]["result"]
        assert "preferred day and time is missing" in r
        assert rows("SELECT * FROM booking_requests") == []

    def test_missing_phone_asks_instead_of_saving(self, client, rows):
        payload = legacy("bookAppointment", {"preferredTime": "Monday"}, call={"id": "c9"})
        assert "no callback number" in post(client, payload).json()["results"][0]["result"]
        assert rows("SELECT * FROM booking_requests") == []

    def test_explicit_phone_beats_caller_id(self, client, rows):
        post(client, legacy("bookAppointment", {**BOOK, "customerPhone": "+1 (555) 999-0000"}))
        assert rows("SELECT customer_phone FROM booking_requests")[0]["customer_phone"] == "+1 (555) 999-0000"

    def test_junk_phone_falls_back_to_caller_id(self, client, rows):
        post(client, legacy("bookAppointment", {**BOOK, "customerPhone": "this number"}))
        assert rows("SELECT customer_phone FROM booking_requests")[0]["customer_phone"] == "+15551234567"

    def test_inputs_are_cleaned_and_capped(self, client, rows):
        evil = {**BOOK, "notes": "x" * 10_000, "customerName": "Bob\r\nBcc: evil@x.com\x00", "serviceAddress": 12345}
        post(client, legacy("bookAppointment", evil))
        row = rows("SELECT * FROM booking_requests")[0]
        assert len(row["notes"]) == handlers.NOTES_MAX
        assert "\n" not in row["customer_name"] and "\x00" not in row["customer_name"]
        assert row["service_address"] == "12345"


# ---------------------------------------------------------------- idempotency

class TestIdempotency:
    def test_retried_tool_call_id_runs_once(self, client, rows, notifier):
        first = post(client, legacy("bookAppointment", BOOK, tool_id="tc_dup")).json()
        again = post(client, legacy("bookAppointment", BOOK, tool_id="tc_dup")).json()
        assert first == again
        assert len(rows("SELECT * FROM booking_requests")) == 1
        assert len(notifier.sent) == 1

    def test_same_request_twice_in_one_call_is_one_row(self, client, rows):
        post(client, legacy("bookAppointment", BOOK, tool_id="t1"))
        r = post(client, legacy("bookAppointment", BOOK, tool_id="t2")).json()["results"][0]["result"]
        assert "already recorded" in r
        assert len(rows("SELECT * FROM booking_requests")) == 1

    def test_different_time_in_same_call_is_a_new_request(self, client, rows):
        post(client, legacy("bookAppointment", BOOK, tool_id="t1"))
        post(client, legacy("bookAppointment", {**BOOK, "preferredTime": "Friday"}, tool_id="t2"))
        assert len(rows("SELECT * FROM booking_requests")) == 2

    def test_internal_failure_returns_a_safe_fallback(self, client, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("disk on fire")
        monkeypatch.setattr(handlers, "book_appointment", boom)
        r = post(client, legacy("bookAppointment", BOOK))
        assert r.status_code == 200
        assert "take the caller's name and phone number" in r.json()["results"][0]["result"]


# ---------------------------------------------------------------- notifications

class TestNotifications:
    def test_owner_is_told_and_marked_notified(self, client, notifier, rows):
        post(client, legacy("bookAppointment", BOOK))
        assert len(notifier.sent) == 1
        subject, body = notifier.sent[0]
        assert "Acme HVAC" in subject and "LB-0001" in subject and "needs confirmation" in subject
        assert "Jane Roe" in body and "+15551234567" in body and "not confirmed" in body
        assert rows("SELECT notified_at FROM booking_requests")[0]["notified_at"]

    def test_failed_send_is_recorded_then_retried_next_time(self, client, notifier, rows):
        notifier.fail = True
        post(client, legacy("bookAppointment", BOOK, tool_id="t1"))
        row = rows("SELECT notified_at, notify_error FROM booking_requests")[0]
        assert row["notified_at"] is None and "smtp down" in row["notify_error"]

        notifier.fail = False
        post(client, legacy("bookAppointment", {**BOOK, "preferredTime": "Friday"}, tool_id="t2"))
        assert len(notifier.sent) == 2                       # the earlier one was retried too
        assert all(r["notified_at"] for r in rows("SELECT notified_at FROM booking_requests"))

    def test_unsent_requests_are_retried_on_startup(self, settings, rows):
        failing = RecordingNotifier()
        failing.fail = True
        with TestClient(create_app(settings, failing), raise_server_exceptions=False) as c:
            c.post("/webhook", json=legacy("bookAppointment", BOOK),
                   headers={"Authorization": f"Bearer {SECRET}"})
        assert rows("SELECT notified_at FROM booking_requests")[0]["notified_at"] is None

        healthy = RecordingNotifier()
        with TestClient(create_app(settings, healthy)):
            pass
        assert len(healthy.sent) == 1

    def test_without_smtp_request_is_saved_but_not_marked_notified(self, settings, rows):
        from lineback.notify import LogNotifier
        with TestClient(create_app(settings, LogNotifier())) as c:
            c.post("/webhook", json=legacy("bookAppointment", BOOK),
                   headers={"Authorization": f"Bearer {SECRET}"})
        assert rows("SELECT notified_at FROM booking_requests")[0]["notified_at"] is None
