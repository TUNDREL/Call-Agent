"""Tool-call logic.

Until a real calendar is connected this module never claims an appointment is
booked. It records a *request* that a person confirms, and the text it returns
tells the voice agent exactly that. To add a calendar later, replace
`check_availability` and the insert in `book_appointment`; the idempotency,
validation and notification plumbing around them stays the same.
"""

import json
import logging
import re
import sqlite3
from dataclasses import dataclass

from .db import Database, booking_reference, utcnow
from .vapi import CallContext, ToolCall, clean_text

logger = logging.getLogger("lineback.handlers")

NO_CALENDAR = (
    "Live scheduling is not connected, so exact times cannot be confirmed. "
    "Do not offer or promise specific times. Ask the caller for their preferred day "
    "and time window, then call bookAppointment to submit a request. Tell them the "
    "team will confirm by phone or text."
)
MISSING_TIME = (
    "Cannot record the request: the caller's preferred day and time is missing. "
    "Ask for it, then call bookAppointment again."
)
MISSING_PHONE = (
    "Cannot record the request: there is no callback number. Ask the caller for a "
    "phone number, then call bookAppointment again with customerPhone."
)
UNKNOWN_TOOL = (
    "This tool is not available. Apologize briefly and offer to take a message so the "
    "team can call back."
)
FALLBACK = (
    "Something went wrong saving this request. Apologize briefly, take the caller's "
    "name and phone number out loud, and tell them the team will call them back."
)

NAME_MAX, ADDRESS_MAX, SERVICE_MAX, TIME_MAX, NOTES_MAX, PHONE_MAX = 100, 300, 100, 100, 1000, 40


@dataclass
class Outcome:
    result: str
    booking_id: int | None = None
    replayed: bool = False


def _pick(args: dict, *keys: str, max_len: int) -> str | None:
    for key in keys:
        value = clean_text(args.get(key), max_len)
        if value:
            return value
    return None


def _usable_phone(*candidates: str | None) -> str | None:
    for candidate in candidates:
        if candidate and len(re.sub(r"\D", "", candidate)) >= 7:
            return candidate
    return None


def _requested_message(booking_id: int, service: str | None, preferred: str, existing: bool) -> str:
    ref = booking_reference(booking_id)
    verb = "was already recorded" if existing else "recorded"
    return (
        f"Request {ref} {verb} for {service or 'service'}, preferred time: {preferred}. "
        "It is NOT confirmed yet. Tell the caller the team has their request and will "
        "confirm by phone or text. Do not say it is booked or confirmed."
    )


def check_availability(_args: dict) -> str:
    return NO_CALENDAR


def book_appointment(conn: sqlite3.Connection, ctx: CallContext, call: ToolCall) -> Outcome:
    args = call.arguments
    # `date` is the parameter name the original tool definition used.
    preferred = _pick(args, "preferredTime", "preferred_time", "dateTime", "date", max_len=TIME_MAX)
    if not preferred:
        return Outcome(MISSING_TIME)

    phone = _usable_phone(
        _pick(args, "customerPhone", "phone", max_len=PHONE_MAX),
        ctx.customer_number,
    )
    if not phone:
        return Outcome(MISSING_PHONE)

    name = _pick(args, "customerName", "name", max_len=NAME_MAX)
    address = _pick(args, "serviceAddress", "address", max_len=ADDRESS_MAX)
    service = _pick(args, "serviceType", "service", max_len=SERVICE_MAX)
    notes = _pick(args, "notes", max_len=NOTES_MAX)

    # The model sometimes calls the tool twice with different toolCallIds.
    if ctx.call_id:
        dup = conn.execute(
            "SELECT id FROM booking_requests WHERE call_id = ? "
            "AND lower(preferred_time) = lower(?) AND lower(coalesce(service_type, '')) = lower(?) "
            "ORDER BY id LIMIT 1",
            (ctx.call_id, preferred, service or ""),
        ).fetchone()
        if dup:
            return Outcome(_requested_message(dup["id"], service, preferred, existing=True), dup["id"])

    cur = conn.execute(
        "INSERT INTO booking_requests (tool_call_id, call_id, customer_name, customer_phone, "
        "service_address, service_type, preferred_time, notes, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (call.id, ctx.call_id, name, phone, address, service, preferred, notes, utcnow()),
    )
    booking_id = int(cur.lastrowid)
    return Outcome(_requested_message(booking_id, service, preferred, existing=False), booking_id)


def process_tool_call(db: Database, ctx: CallContext, call: ToolCall) -> Outcome:
    """Run one tool call exactly once per toolCallId; never raises."""
    try:
        with db.transaction() as conn:
            prior = conn.execute(
                "SELECT result FROM tool_calls WHERE tool_call_id = ?", (call.id,)
            ).fetchone()
            if prior:
                row = conn.execute(
                    "SELECT id FROM booking_requests WHERE tool_call_id = ?", (call.id,)
                ).fetchone()
                return Outcome(prior["result"], row["id"] if row else None, replayed=True)

            if ctx.call_id:
                conn.execute(
                    "INSERT OR IGNORE INTO calls (call_id, customer_number, business_number, first_seen_at) "
                    "VALUES (?, ?, ?, ?)",
                    (ctx.call_id, ctx.customer_number, ctx.business_number, utcnow()),
                )

            if call.name == "checkAvailability":
                outcome = Outcome(check_availability(call.arguments))
            elif call.name == "bookAppointment":
                outcome = book_appointment(conn, ctx, call)
            else:
                outcome = Outcome(UNKNOWN_TOOL)

            conn.execute(
                "INSERT INTO tool_calls (tool_call_id, call_id, name, arguments, result, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    call.id,
                    ctx.call_id,
                    call.name or "(none)",
                    json.dumps(call.arguments, ensure_ascii=False, default=str)[:4000],
                    outcome.result,
                    utcnow(),
                ),
            )
            return outcome
    except Exception:
        logger.exception("Tool call failed: tool=%s tool_call_id=%s", call.name, call.id)
        return Outcome(FALLBACK)
