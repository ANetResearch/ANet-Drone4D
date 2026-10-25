"""Authoring stays outside the API process and has bounded cancellation/IO."""

import asyncio
import json

import pytest

from awr.runtime import target_authoring as transport


def test_real_worker_rejects_cross_scene_paths_without_importing_sim_into_api():
    with pytest.raises(ValueError, match="Invalid scene identity"):
        asyncio.run(transport.target_authoring("preview", "../other", {}))


def test_oversized_request_is_rejected_before_spawn(monkeypatch):
    async def forbidden(*args, **kwargs):
        pytest.fail("must not spawn")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden)
    with pytest.raises(ValueError, match="request too large"):
        asyncio.run(transport.target_authoring("preview", "synthcity", {"prompt": "x" * 33000}))


@pytest.mark.parametrize("mode", ["timeout", "cancel", "overflow", "malformed", "success"])
def test_worker_cleanup_and_response_contract(monkeypatch, mode):
    class Input:
        def write(self, data):
            assert json.loads(data)["world"] == "synthcity"

        async def drain(self):
            pass

        def close(self):
            pass

    class Worker:
        returncode = None
        killed = False
        waited = False
        stdin = Input()

        def __init__(self):
            self.stdout = self
            self.reads = 0

        async def read(self, size):
            self.reads += 1
            if mode in ("timeout", "cancel"):
                await asyncio.Event().wait()
            if mode == "overflow":
                return b"x" * size
            if self.reads > 1:
                return b""
            return b"{}" if mode == "malformed" else b'{"ok":true,"result":{"seed":3}}'

        def kill(self):
            self.killed = True
            self.returncode = -9

        async def wait(self):
            self.waited = True
            self.returncode = self.returncode or 0

    worker = Worker()

    async def spawn(*args, **kwargs):
        assert args[1:] == ("-m", "awr.sim.activities.authoring")
        return worker

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(transport, "MAX_RESPONSE_BYTES", 100)

    async def run():
        if mode == "cancel":
            task = asyncio.create_task(transport.target_authoring("preview", "synthcity", {}))
            await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        elif mode == "success":
            assert await transport.target_authoring("preview", "synthcity", {}) == {"seed": 3}
        else:
            with pytest.raises(
                ValueError,
                match={
                    "timeout": "timed out",
                    "overflow": "response too large",
                    "malformed": "Invalid target authoring response",
                }[mode],
            ):
                await transport.target_authoring("preview", "synthcity", {}, timeout_s=0.01)

    asyncio.run(run())
    assert worker.waited
    assert worker.killed == (mode in ("timeout", "cancel", "overflow"))
