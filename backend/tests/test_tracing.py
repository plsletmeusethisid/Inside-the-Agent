"""Streaming boundaries, safe failures, and cooperative disconnect cleanup."""

import asyncio
import json
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

import httpx
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.generation import generate_answer, partial_answer
from app.main import app
from app.tracing import Trace, trace_response


def decode_frame(frame):
    return json.loads(next(line[6:] for line in frame.splitlines() if line.startswith("data: ")))


class TraceTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_events_arrive_before_work_finishes_and_have_stable_ids(self):
        release = threading.Event()
        def work(trace):
            started = trace.start("parse")
            if not release.wait(3):
                raise RuntimeError("test gate timed out")
            trace.complete("parse", started, page_count=2)
            return {"saved": True}
        response = trace_response(work, uuid4())
        iterator = response.body_iterator
        try:
            first = decode_frame(await asyncio.wait_for(anext(iterator), 2))
            second = decode_frame(await asyncio.wait_for(anext(iterator), 2))
            self.assertEqual(second["event"], "parse_started")
            self.assertFalse(release.is_set())
            release.set()
            rest = [decode_frame(frame) async for frame in iterator]
            events = [first, second, *rest]
            self.assertEqual([e["sequence"] for e in events], [1, 2, 3, 4])
            self.assertEqual(len({e["run_id"] for e in events}), 1)
            self.assertEqual(len({e["document_id"] for e in events}), 1)
            self.assertGreaterEqual(rest[0]["data"]["duration_ms"], 0)
            self.assertEqual(rest[-1]["data"]["result"], {"saved": True})
        finally:
            release.set()
            await iterator.aclose()

    async def test_disconnect_stops_at_next_boundary_and_closes_worker(self):
        release, closed = threading.Event(), threading.Event()
        reached_next_step = []
        def work(trace):
            try:
                trace.start("parse")
                release.wait(3)
                trace.emit("parse_completed")
                reached_next_step.append(True)
            finally:
                closed.set()
        iterator = trace_response(work).body_iterator
        await anext(iterator)
        await anext(iterator)
        await iterator.aclose()
        release.set()
        self.assertTrue(await asyncio.to_thread(closed.wait, 2))
        self.assertEqual(reached_next_step, [])

    async def test_unknown_errors_are_safe_and_do_not_complete(self):
        def work(trace):
            trace.start("storage")
            raise RuntimeError("private-provider-details secret-key")
        frames = [decode_frame(frame) async for frame in trace_response(work).body_iterator]
        self.assertEqual(frames[-1]["event"], "error")
        self.assertEqual(frames[-1]["data"]["stage"], "storage")
        self.assertNotIn("secret-key", json.dumps(frames))
        self.assertNotIn("completed", [frame["event"] for frame in frames])


class StreamingGenerationTests(unittest.TestCase):
    def setUp(self):
        self.chunk_id = str(uuid4())
        self.context = {"question": "VPN?", "results": [{
            "chunk_id": self.chunk_id, "chunk_index": 0, "page_number": 1, "content": "Use VPN.",
        }]}
        self.answer = {"status": "answered", "answer": 'Use "VPN".\n한글 😀', "evidence_chunk_ids": [self.chunk_id]}

    def run_stream(self, answer=None, terminal="response.completed"):
        answer = self.answer if answer is None else answer
        text = json.dumps(answer)
        observed = []
        final_sent = []
        class Body(httpx.SyncByteStream):
            def __iter__(body):
                for char in text:
                    yield ("data: " + json.dumps({"type": "response.output_text.delta", "delta": char}) + "\n\n").encode()
                # Token events must be delivered before the terminal provider event.
                final_sent.append(any(event["event"] == "token" for event in observed))
                if terminal:
                    yield ("data: " + json.dumps({"type": terminal, "response": {
                        "status": "completed", "output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]}],
                    }}) + "\n\n").encode()
        def handler(request):
            self.assertTrue(json.loads(request.content)["stream"])
            return httpx.Response(200, stream=Body())
        with patch("app.generation.create_generation_client", side_effect=lambda: httpx.Client(base_url="https://api.openai.com/v1/", transport=httpx.MockTransport(handler))):
            result = generate_answer(self.context, Trace(observed.append))
        return result, observed, final_sent

    def test_real_deltas_precede_completion_and_decode_split_escapes(self):
        result, events, before_final = self.run_stream()
        self.assertEqual(before_final, [True])
        self.assertEqual("".join(e["data"]["text"] for e in events if e["event"] == "token"), self.answer["answer"])
        self.assertEqual(result["answer"], self.answer["answer"])
        self.assertEqual(events[-1]["event"], "generation_completed")
        self.assertEqual(result["citations"][0]["chunk_id"], self.chunk_id)

    def test_eof_failure_and_invalid_citations_cannot_complete_generation(self):
        for terminal in (None, "response.failed", "response.incomplete", "error", "response.refusal.delta"):
            with self.subTest(terminal=terminal), self.assertRaises(HTTPException):
                self.run_stream(terminal=terminal)
        with self.assertRaises(HTTPException):
            self.run_stream(answer={**self.answer, "evidence_chunk_ids": [str(uuid4())]})

    def test_empty_context_and_insufficient_evidence(self):
        result, _, _ = self.run_stream(answer={"status": "insufficient_evidence", "answer": "", "evidence_chunk_ids": []})
        self.assertEqual(result["citations"], [])
        events = []
        with patch("app.generation.create_generation_client", side_effect=AssertionError("No provider needed")):
            generate_answer({"question": "q", "results": []}, Trace(events.append))
        self.assertIn("generation_skipped", [e["event"] for e in events])
        self.assertNotIn("generation_completed", [e["event"] for e in events])

    def test_partial_parser_does_not_leak_other_fields(self):
        self.assertEqual(partial_answer('{"status":"answer", "evidence_chunk_ids":["answer"], "answer":"hello'), "hello")
        self.assertEqual(partial_answer('{"status":"answer'), "")

    def test_stream_validation_precedes_database_and_provider(self):
        client = TestClient(app)
        self.addCleanup(client.close)
        with patch("app.main.database_connection", side_effect=AssertionError("Invalid input reached database")):
            for route in ("answer", "retrieve"):
                response = client.post(f"/documents/{uuid4()}/{route}/stream", json={"question": " "})
                self.assertEqual(response.status_code, 422)
