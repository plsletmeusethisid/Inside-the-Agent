"""Request-local execution events and a bounded, disconnect-aware SSE bridge."""

import asyncio
import json
from concurrent.futures import TimeoutError as FutureTimeout
from datetime import datetime, timezone
from threading import Event
from time import perf_counter
from uuid import uuid4

import psycopg
from fastapi import HTTPException
from starlette.responses import StreamingResponse


class TraceCancelled(Exception):
    """Stop at the next pipeline boundary after the browser disconnects."""


class Trace:
    def __init__(self, sink=None, document_id=None, cancelled=None):
        self.sink = sink
        self.document_id = str(document_id) if document_id else None
        self.run_id = str(uuid4())
        self.sequence = 0
        self.started = perf_counter()
        self.stage = "request"
        self.cancelled = cancelled

    def check(self):
        if self.cancelled and self.cancelled():
            raise TraceCancelled()

    def emit(self, event, **data):
        self.check()
        if not self.sink:
            return
        self.sequence += 1
        self.sink({
            "event": event, "run_id": self.run_id, "sequence": self.sequence,
            "document_id": self.document_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "elapsed_ms": round((perf_counter() - self.started) * 1000, 2),
            "data": data,
        })

    def start(self, stage, **data):
        self.stage = stage
        self.emit(f"{stage}_started", **data)
        return perf_counter()

    def complete(self, stage, started, **data):
        self.emit(f"{stage}_completed", duration_ms=round((perf_counter() - started) * 1000, 2), **data)


# Keep workers alive until they reach a cancellation boundary and close resources.
_workers = set()


def trace_response(operation, document_id=None):
    async def stream():
        loop = asyncio.get_running_loop()
        queue = asyncio.Queue(maxsize=64)
        stopped = Event()

        def send(event):
            if stopped.is_set():
                raise TraceCancelled()
            pending = asyncio.run_coroutine_threadsafe(queue.put(event), loop)
            while True:
                try:
                    pending.result(timeout=0.1)
                    return
                except FutureTimeout:
                    if stopped.is_set():
                        pending.cancel()
                        raise TraceCancelled()

        trace = Trace(send, document_id, stopped.is_set)

        def work():
            try:
                trace.emit("trace_started")
                result = operation(trace)
                trace.emit("completed", result=result)
            except TraceCancelled:
                pass
            except Exception as exc:
                if isinstance(exc, HTTPException):
                    message, code = exc.detail, exc.status_code
                elif isinstance(exc, psycopg.Error):
                    message, code = "Database operation failed. Saved batches are retained; retry when it recovers.", 503
                else:
                    message, code = "This pipeline step failed. Please retry.", 500
                try:
                    trace.emit("error", stage=trace.stage, message=message, status_code=code)
                except TraceCancelled:
                    pass

        worker = asyncio.create_task(asyncio.to_thread(work))
        _workers.add(worker)
        worker.add_done_callback(_workers.discard)
        try:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=10)
                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"
                    continue
                yield f"id: {trace.run_id}:{event['sequence']}\nevent: {event['event']}\ndata: {json.dumps(event, ensure_ascii=True, allow_nan=False)}\n\n"
                if event["event"] in ("completed", "error"):
                    break
        finally:
            stopped.set()

    return StreamingResponse(stream(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no",
    })
