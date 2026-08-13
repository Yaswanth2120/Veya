import json
import unittest

from veya.ipc.errors import ErrorCode, ProtocolError
from veya.ipc.protocol import (
    ErrorPayload,
    ErrorResponse,
    Event,
    PROTOCOL_VERSION,
    Response,
    parse_incoming_line,
    serialize,
)


class ParseIncomingLineTests(unittest.TestCase):
    def test_valid_request_parses(self):
        line = json.dumps(
            {"version": 1, "id": "req-1", "type": "request", "method": "system.ping", "params": {}}
        )
        request = parse_incoming_line(line)
        self.assertEqual(request.id, "req-1")
        self.assertEqual(request.method, "system.ping")
        self.assertEqual(request.params, {})

    def test_missing_params_defaults_to_empty_dict(self):
        line = json.dumps({"version": 1, "id": "req-1", "type": "request", "method": "system.ping"})
        request = parse_incoming_line(line)
        self.assertEqual(request.params, {})

    def test_trailing_newline_is_ignored(self):
        line = json.dumps({"version": 1, "id": "req-1", "type": "request", "method": "system.ping"}) + "\n"
        request = parse_incoming_line(line)
        self.assertEqual(request.id, "req-1")

    def test_empty_line_is_invalid_request(self):
        with self.assertRaises(ProtocolError) as ctx:
            parse_incoming_line("   \n")
        self.assertEqual(ctx.exception.code, ErrorCode.INVALID_REQUEST)

    def test_invalid_json_is_invalid_request(self):
        with self.assertRaises(ProtocolError) as ctx:
            parse_incoming_line("{not json")
        self.assertEqual(ctx.exception.code, ErrorCode.INVALID_REQUEST)

    def test_non_object_json_is_invalid_request(self):
        with self.assertRaises(ProtocolError) as ctx:
            parse_incoming_line(json.dumps([1, 2, 3]))
        self.assertEqual(ctx.exception.code, ErrorCode.INVALID_REQUEST)

    def test_wrong_version_is_unsupported_version(self):
        line = json.dumps({"version": 99, "id": "req-1", "type": "request", "method": "system.ping"})
        with self.assertRaises(ProtocolError) as ctx:
            parse_incoming_line(line)
        self.assertEqual(ctx.exception.code, ErrorCode.UNSUPPORTED_VERSION)

    def test_missing_version_is_unsupported_version(self):
        line = json.dumps({"id": "req-1", "type": "request", "method": "system.ping"})
        with self.assertRaises(ProtocolError) as ctx:
            parse_incoming_line(line)
        self.assertEqual(ctx.exception.code, ErrorCode.UNSUPPORTED_VERSION)

    def test_wrong_type_is_invalid_request(self):
        line = json.dumps({"version": 1, "id": "req-1", "type": "event", "method": "system.ping"})
        with self.assertRaises(ProtocolError) as ctx:
            parse_incoming_line(line)
        self.assertEqual(ctx.exception.code, ErrorCode.INVALID_REQUEST)

    def test_missing_id_is_invalid_request(self):
        line = json.dumps({"version": 1, "type": "request", "method": "system.ping"})
        with self.assertRaises(ProtocolError) as ctx:
            parse_incoming_line(line)
        self.assertEqual(ctx.exception.code, ErrorCode.INVALID_REQUEST)

    def test_missing_method_is_invalid_request(self):
        line = json.dumps({"version": 1, "id": "req-1", "type": "request"})
        with self.assertRaises(ProtocolError) as ctx:
            parse_incoming_line(line)
        self.assertEqual(ctx.exception.code, ErrorCode.INVALID_REQUEST)

    def test_non_object_params_is_invalid_request(self):
        line = json.dumps({"version": 1, "id": "req-1", "type": "request", "method": "system.ping", "params": [1]})
        with self.assertRaises(ProtocolError) as ctx:
            parse_incoming_line(line)
        self.assertEqual(ctx.exception.code, ErrorCode.INVALID_REQUEST)


class SerializeTests(unittest.TestCase):
    def test_response_serializes_as_one_json_line(self):
        line = serialize(Response(id="req-1", result={"pong": True}))
        self.assertTrue(line.endswith("\n"))
        self.assertEqual(line.count("\n"), 1)
        parsed = json.loads(line)
        self.assertEqual(
            parsed,
            {"version": PROTOCOL_VERSION, "id": "req-1", "type": "response", "result": {"pong": True}},
        )

    def test_error_response_serializes_correctly(self):
        line = serialize(ErrorResponse(id="req-1", error=ErrorPayload(code="INVALID_REQUEST", message="bad")))
        parsed = json.loads(line)
        self.assertEqual(parsed["type"], "error")
        self.assertEqual(parsed["error"], {"code": "INVALID_REQUEST", "message": "bad"})

    def test_error_response_id_can_be_none(self):
        line = serialize(ErrorResponse(id=None, error=ErrorPayload(code="INVALID_REQUEST", message="bad")))
        parsed = json.loads(line)
        self.assertIsNone(parsed["id"])

    def test_event_serializes_correctly(self):
        line = serialize(Event(event="transcript.partial", data={"session_id": "s1", "text": "hi"}))
        parsed = json.loads(line)
        self.assertEqual(parsed["type"], "event")
        self.assertEqual(parsed["event"], "transcript.partial")
        self.assertEqual(parsed["data"], {"session_id": "s1", "text": "hi"})
        self.assertNotIn("id", parsed)

    def test_serialized_line_uses_snake_case_keys_only(self):
        line = serialize(Event(event="question.detected", data={"session_id": "s1", "question_id": "q1"}))
        parsed = json.loads(line)
        for key in parsed:
            self.assertNotIn(key, ["sessionId", "questionId"])


if __name__ == "__main__":
    unittest.main()
