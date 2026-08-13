"""The long-running worker process: reads JSON Lines requests from stdin,
dispatches them, writes responses/events to stdout, and logs (stderr
only, metadata-only) throughout.

Swift owns starting/stopping this process (see
`Sources/Veya/Bridge/PythonWorkerManager.swift`); this module only knows
about its own stdin/stdout/stderr, never about how it was launched.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from typing import Optional

from . import __version__
from .ipc.dispatcher import Dispatcher, WorkerContext
from .ipc.errors import ProtocolError
from .ipc.events import worker_ready
from .ipc.protocol import (
    PROTOCOL_VERSION,
    ErrorPayload,
    ErrorResponse,
    Event,
    OutgoingMessage,
    Response,
    parse_incoming_line,
    serialize,
)

logger = logging.getLogger("veya.worker")


def configure_logging(level: int = logging.INFO) -> None:
    """stderr only, metadata-only. Never call `logging` with transcript
    text, answers, prompts, or any other sensitive payload — every log
    call in this codebase logs ids/method names/counts, not content."""
    logging.basicConfig(
        level=level,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


class OutputWriter:
    """The one path every outgoing message goes through. The lock is what
    guarantees a response and a concurrently-emitted event can never
    interleave their bytes on stdout."""

    def __init__(self, stream=None) -> None:
        self._stream = stream if stream is not None else sys.stdout
        self._lock = asyncio.Lock()

    async def write(self, message: OutgoingMessage) -> None:
        line = serialize(message)
        async with self._lock:
            self._stream.write(line)
            self._stream.flush()


class Worker:
    def __init__(
        self,
        *,
        output_writer: Optional[OutputWriter] = None,
    ) -> None:
        self._output = output_writer if output_writer is not None else OutputWriter()
        self._dispatcher = Dispatcher()
        self._context = WorkerContext(emit_event=self._emit_event)

    @property
    def context(self) -> WorkerContext:
        return self._context

    async def _emit_event(self, event_name: str, data: dict) -> None:
        await self._output.write(Event(event=event_name, data=data))

    async def handle_line(self, line: str) -> None:
        """Parse, dispatch, and respond to exactly one incoming line.
        Public (not `_handle_line`) so tests can drive the worker without
        a real stdin pipe."""
        try:
            request = parse_incoming_line(line)
        except ProtocolError as exc:
            logger.warning("protocol error: code=%s", exc.code)
            await self._output.write(ErrorResponse(id=None, error=ErrorPayload(code=exc.code, message=exc.message)))
            return

        logger.info("request method=%s id=%s", request.method, request.id)
        try:
            result = await self._dispatcher.dispatch(request, self._context)
            await self._output.write(Response(id=request.id, result=result))
        except ProtocolError as exc:
            logger.warning("request failed method=%s id=%s code=%s", request.method, request.id, exc.code)
            await self._output.write(
                ErrorResponse(id=request.id, error=ErrorPayload(code=exc.code, message=exc.message))
            )

    async def run(self, reader: Optional[asyncio.StreamReader] = None) -> None:
        """Runs until `worker.shutdown` is received, stdin closes, or a
        termination signal arrives. `reader` is injectable for tests;
        production always connects real stdin."""
        logger.info("worker starting version=%s pid=%s", __version__, os.getpid())

        loop = asyncio.get_running_loop()
        self._install_signal_handlers(loop)

        owns_reader = reader is None
        if reader is None:
            reader = await self._connect_stdin(loop)

        await self._emit_event("worker.ready", worker_ready(PROTOCOL_VERSION, __version__))
        logger.info("worker ready")

        try:
            while not self._context.shutdown_event.is_set():
                raw_line = await reader.readline()
                if not raw_line:
                    logger.info("stdin closed, shutting down")
                    break
                await self.handle_line(raw_line.decode("utf-8", errors="replace"))
        finally:
            await self._context.cancel_feed_task_if_running()
            logger.info("worker exiting")

    @staticmethod
    async def _connect_stdin(loop: asyncio.AbstractEventLoop) -> asyncio.StreamReader:
        stream_reader = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(stream_reader)
        await loop.connect_read_pipe(lambda: protocol, sys.stdin)
        return stream_reader

    def _install_signal_handlers(self, loop: asyncio.AbstractEventLoop) -> None:
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, self._context.shutdown_event.set)
            except (NotImplementedError, RuntimeError):
                # Signal handlers aren't installable in every environment
                # (e.g. some test harnesses) — falling back to relying on
                # stdin closing is acceptable there.
                pass


async def main() -> None:
    configure_logging()
    worker = Worker()
    await worker.run()
