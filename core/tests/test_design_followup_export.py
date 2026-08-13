import asyncio
import base64
import json
import tempfile
import unittest
from pathlib import Path

from veya.ipc.dispatcher import Dispatcher, WorkerContext
from veya.ipc.protocol import Request


class FakeProvider:
    def __init__(self, responses):
        self._responses = list(responses)

    async def check_availability(self):
        return None

    async def generate_stream(self, prompt, *, timeout):
        await asyncio.sleep(0)
        yield json.dumps(self._responses.pop(0))


async def _ignore_event(name, data):
    return None


class DesignFollowupExportTests(unittest.IsolatedAsyncioTestCase):
    async def test_followup_evolves_state_and_preserves_untouched_nodes(self):
        with tempfile.TemporaryDirectory() as temporary:
            provider = FakeProvider([{
                "title": "URL Shortener",
                "nodes": [{"id": "api", "label": "API", "kind": "service"}, {"id": "cache", "label": "Redis Cache", "kind": "cache"}],
                "edges": [{"source": "api", "target": "cache", "label": "hot lookups"}],
                "decisions": ["Use base62 encoding"],
                "assumptions": [], "requirements": ["100M redirects/day"], "risks": ["cache stampede"], "trade_offs": [], "action_items": [],
            }])
            context = WorkerContext(emit_event=_ignore_event, architecture_state_directory=Path(temporary), llm_provider_factory=lambda: provider)
            dispatcher = Dispatcher()
            await dispatcher.dispatch(Request(id="1", method="design.replace", params={"session_id": "s-1", "title": "URL Shortener", "nodes": [{"id": "api", "label": "API", "kind": "service"}], "edges": [], "base_version": None}), context)

            result = await dispatcher.dispatch(Request(id="2", method="design.followup", params={"session_id": "s-1", "request": "How do we handle hot URLs?"}), context)
            self.assertEqual({n["id"] for n in result["nodes"]}, {"api", "cache"})
            self.assertIn("Use base62 encoding", result["decisions"])
            self.assertIn("cache stampede", result["risks"])
            self.assertEqual(result["version"], 3)

    async def test_export_formats(self):
        with tempfile.TemporaryDirectory() as temporary:
            context = WorkerContext(emit_event=_ignore_event, architecture_state_directory=Path(temporary))
            dispatcher = Dispatcher()
            await dispatcher.dispatch(Request(id="1", method="design.replace", params={"session_id": "s-1", "title": "Checkout", "nodes": [{"id": "api", "label": "API", "kind": "service"}, {"id": "db", "label": "DB", "kind": "database"}], "edges": [{"source": "api", "target": "db", "label": "reads"}], "decisions": ["Use PostgreSQL"], "base_version": None}), context)

            mermaid_result = await dispatcher.dispatch(Request(id="2", method="design.export", params={"session_id": "s-1", "format": "mermaid"}), context)
            self.assertIn("api -->|reads| db", mermaid_result["content"])

            json_result = await dispatcher.dispatch(Request(id="3", method="design.export", params={"session_id": "s-1", "format": "json"}), context)
            parsed = json.loads(json_result["content"])
            self.assertEqual(parsed["title"], "Checkout")

            markdown_result = await dispatcher.dispatch(Request(id="4", method="design.export", params={"session_id": "s-1", "format": "markdown"}), context)
            self.assertIn("Use PostgreSQL", markdown_result["content"])

            pdf_result = await dispatcher.dispatch(Request(id="5", method="design.export", params={"session_id": "s-1", "format": "pdf"}), context)
            pdf_bytes = base64.b64decode(pdf_result["content_base64"])
            self.assertTrue(pdf_bytes.startswith(b"%PDF-1.4"))
            self.assertTrue(pdf_bytes.rstrip().endswith(b"%%EOF"))
