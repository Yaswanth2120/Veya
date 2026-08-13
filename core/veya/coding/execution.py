"""Opt-in, bounded local source execution.

There is no shell invocation, execution uses an empty environment and an
ephemeral workspace, and only Python is supported in V1. This is a safety
boundary, not a security sandbox: untrusted code execution stays disabled
unless the user explicitly enables it in the host configuration.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

from ..ipc.errors import ErrorCode, ProtocolError

MAX_OUTPUT_BYTES = 64 * 1024


async def run_python(source: str, timeout_seconds: float = 5.0) -> dict:
    if os.environ.get("VEYA_CODE_EXECUTION_ENABLED") != "1":
        raise ProtocolError(ErrorCode.INVALID_PARAMS, "Local code execution is disabled in this build.")
    timeout = min(max(float(timeout_seconds), 0.1), 10.0)
    with tempfile.TemporaryDirectory(prefix="veya-code-") as temporary:
        source_path = Path(temporary) / "main.py"
        source_path.write_text(source, encoding="utf-8")
        try:
            process = await asyncio.create_subprocess_exec(
                sys.executable, "-I", "-B", str(source_path), cwd=temporary, env={},
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except TimeoutError:
            process.kill()
            await process.wait()
            return {"exit_code": -1, "timed_out": True, "stdout": "", "stderr": "Execution timed out."}
    return {"exit_code": process.returncode, "timed_out": False,
            "stdout": stdout[:MAX_OUTPUT_BYTES].decode("utf-8", errors="replace"),
            "stderr": stderr[:MAX_OUTPUT_BYTES].decode("utf-8", errors="replace")}
