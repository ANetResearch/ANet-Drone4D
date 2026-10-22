"""Bounded transport to an isolated scene-authoring worker; API never imports sim code."""

from __future__ import annotations

import asyncio
import json
import sys

MAX_REQUEST_BYTES = 32 * 1024
MAX_RESPONSE_BYTES = 4 * 1024 * 1024


async def target_authoring(operation: str, world: str, body: dict, *, timeout_s: float = 180) -> dict:
    if operation not in ("preview", "scene", "rehearsal"):
        raise ValueError("Unknown target authoring operation")
    data = json.dumps({"operation": operation, "world": world, "body": body}, allow_nan=False).encode()
    if len(data) > MAX_REQUEST_BYTES:
        raise ValueError("Target authoring request too large")
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "awr.sim.activities.authoring",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )

    async def exchange():
        process.stdin.write(data)
        await process.stdin.drain()
        process.stdin.close()
        output = bytearray()
        while chunk := await process.stdout.read(65536):
            output.extend(chunk)
            if len(output) > MAX_RESPONSE_BYTES:
                raise ValueError("Target authoring response too large")
        await process.wait()
        return output

    try:
        output = await asyncio.wait_for(exchange(), timeout_s)
        if process.returncode:
            raise ValueError("Target authoring worker failed")
        reply = json.loads(output)
        if not isinstance(reply, dict) or not isinstance(reply.get("ok"), bool):
            raise ValueError("Invalid target authoring response")
        if not reply["ok"]:
            raise ValueError(str(reply.get("error", "Target authoring rejected"))[:240])
        if not isinstance(reply.get("result"), dict):
            raise ValueError("Invalid target authoring result")
        return reply["result"]
    except TimeoutError:
        raise ValueError("Target authoring timed out") from None
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
