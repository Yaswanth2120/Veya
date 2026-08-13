"""Wire protocol: JSON Lines, versioned, snake_case.

One JSON object per line. Four message shapes, discriminated by `type`:

    request  {version, id, type: "request", method, params}
    response {version, id, type: "response", result}
    error    {version, id, type: "error", error: {code, message}}
    event    {version, type: "event", event, data}

`parse_incoming_line` is the single place that turns a raw stdin line into
a validated `IncomingMessage` (or raises `ProtocolError`) — the dispatcher
never sees unvalidated input. `serialize` is the single place that turns
an outgoing message into the exact line written to stdout.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Optional, Union

from .errors import ErrorCode, ProtocolError

PROTOCOL_VERSION = 1


@dataclass(frozen=True)
class Request:
    id: str
    method: str
    params: dict[str, Any] = field(default_factory=dict)
    version: int = PROTOCOL_VERSION
    type: str = "request"


@dataclass(frozen=True)
class Response:
    id: str
    result: dict[str, Any]
    version: int = PROTOCOL_VERSION
    type: str = "response"


@dataclass(frozen=True)
class ErrorPayload:
    code: str
    message: str


@dataclass(frozen=True)
class ErrorResponse:
    id: Optional[str]
    error: ErrorPayload
    version: int = PROTOCOL_VERSION
    type: str = "error"


@dataclass(frozen=True)
class Event:
    event: str
    data: dict[str, Any]
    version: int = PROTOCOL_VERSION
    type: str = "event"


OutgoingMessage = Union[Response, ErrorResponse, Event]
IncomingMessage = Request


def parse_incoming_line(line: str) -> Request:
    """Parse and validate one line of stdin into a `Request`.

    Raises `ProtocolError` (never a bare exception) for anything
    malformed: invalid JSON, wrong/missing `version`, wrong `type`,
    missing/invalid `id`/`method`, or non-object `params`.
    """
    stripped = line.strip()
    if not stripped:
        raise ProtocolError(ErrorCode.INVALID_REQUEST, "Empty line is not a valid message.")

    try:
        raw = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise ProtocolError(ErrorCode.INVALID_REQUEST, f"Line is not valid JSON: {exc}") from exc

    if not isinstance(raw, dict):
        raise ProtocolError(ErrorCode.INVALID_REQUEST, "Message must be a JSON object.")

    version = raw.get("version")
    if version != PROTOCOL_VERSION:
        raise ProtocolError(
            ErrorCode.UNSUPPORTED_VERSION,
            f"Unsupported protocol version: {version!r} (expected {PROTOCOL_VERSION}).",
        )

    if raw.get("type") != "request":
        raise ProtocolError(ErrorCode.INVALID_REQUEST, "Only 'request' messages are accepted on stdin.")

    request_id = raw.get("id")
    if not isinstance(request_id, str) or not request_id:
        raise ProtocolError(ErrorCode.INVALID_REQUEST, "Missing or invalid 'id'.")

    method = raw.get("method")
    if not isinstance(method, str) or not method:
        raise ProtocolError(ErrorCode.INVALID_REQUEST, "Missing or invalid 'method'.")

    params = raw.get("params", {})
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise ProtocolError(ErrorCode.INVALID_REQUEST, "'params' must be a JSON object.")

    return Request(id=request_id, method=method, params=params)


def _to_dict(message: OutgoingMessage) -> dict[str, Any]:
    if isinstance(message, Response):
        return {
            "version": message.version,
            "id": message.id,
            "type": message.type,
            "result": message.result,
        }
    if isinstance(message, ErrorResponse):
        return {
            "version": message.version,
            "id": message.id,
            "type": message.type,
            "error": {"code": message.error.code, "message": message.error.message},
        }
    if isinstance(message, Event):
        return {
            "version": message.version,
            "type": message.type,
            "event": message.event,
            "data": message.data,
        }
    raise TypeError(f"Unknown outgoing message type: {type(message)!r}")


def serialize(message: OutgoingMessage) -> str:
    """Serialize an outgoing message to exactly one JSON Lines line,
    including the trailing newline."""
    return json.dumps(_to_dict(message), ensure_ascii=True, separators=(",", ":")) + "\n"
