"""Vapi webhook helpers: authentication, payload parsing, response shaping.

Vapi sends tool calls as {"message": {"type": "tool-calls", ...}} and expects
{"results": [{"name", "toolCallId", "result"}]} back. Two payload shapes exist
in the wild and both are accepted: `toolCallList` (id/name/parameters) and the
OpenAI-style `toolCalls` (id/function.name/function.arguments, where arguments
may be an object or a JSON string).
"""

import hmac
import json
import logging
import re
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, Field

logger = logging.getLogger("lineback.vapi")

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]+")


class ToolCall(BaseModel):
    id: str
    name: str = ""
    arguments: dict[str, Any] = Field(default_factory=dict)


class CallContext(BaseModel):
    call_id: str | None = None
    customer_number: str | None = None
    business_number: str | None = None


def clean_text(value: Any, max_len: int) -> str | None:
    """Coerce a tool argument to a short, single-line string (or None)."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        value = str(value)
    if not isinstance(value, str):
        return None
    value = _CONTROL_CHARS.sub(" ", value).strip()
    if not value:
        return None
    return value[:max_len].strip()


def is_authorized(headers: Mapping[str, str], secret: str | None) -> bool:
    """True if the request carries the shared secret as a Bearer token or X-Vapi-Secret."""
    if not secret:
        return False
    supplied: list[str] = []
    auth = headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        supplied.append(auth[7:].strip())
    legacy = headers.get("x-vapi-secret")
    if legacy:
        supplied.append(legacy.strip())
    expected = secret.encode()
    return any(hmac.compare_digest(s.encode(), expected) for s in supplied)


def _dig(obj: Any, *path: str) -> Any:
    for key in path:
        if not isinstance(obj, Mapping):
            return None
        obj = obj.get(key)
    return obj


def parse_call_context(message: Mapping[str, Any]) -> CallContext:
    return CallContext(
        call_id=clean_text(_dig(message, "call", "id"), 100),
        customer_number=clean_text(_dig(message, "call", "customer", "number"), 40),
        business_number=clean_text(_dig(message, "call", "phoneNumber", "number"), 40),
    )


def _coerce_arguments(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def extract_tool_calls(message: Mapping[str, Any]) -> list[ToolCall]:
    raw_calls = message.get("toolCallList")
    if not isinstance(raw_calls, list) or not raw_calls:
        raw_calls = message.get("toolCalls")
    if not isinstance(raw_calls, list):
        return []

    calls: list[ToolCall] = []
    for item in raw_calls:
        if not isinstance(item, dict):
            continue
        function = item.get("function") if isinstance(item.get("function"), dict) else {}
        tool_id = item.get("id")
        if not isinstance(tool_id, str) or not tool_id:
            logger.warning("Skipping a tool call with no id (cannot answer it)")
            continue
        name = item.get("name") or function.get("name") or ""
        raw_args = item.get("parameters")
        if raw_args is None:
            raw_args = item.get("arguments")
        if raw_args is None:
            raw_args = function.get("arguments")
        calls.append(
            ToolCall(
                id=tool_id,
                name=name if isinstance(name, str) else "",
                arguments=_coerce_arguments(raw_args),
            )
        )
    return calls


def tool_result(call: ToolCall, text: str) -> dict[str, str]:
    return {"name": call.name, "toolCallId": call.id, "result": text}
