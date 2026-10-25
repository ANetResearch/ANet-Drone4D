"""渲染进程组 GPU 丢失归类测试用的渲染进程处理器（`RenderGroup` 以文件路径载入；不导入 Env，子进程启动快）。"""

from __future__ import annotations

import os
import signal


class GpuLostProc:
    """`("cuda100",)` 抛出 Warp 在设备丢失后的原文异常；`("segv",)` 以 SIGSEGV 结束本进程（EGL 驱动段错误）；`("boom",)` 抛出与 GPU
    无关的异常；其余消息原样回送。"""

    def __init__(self, arg: dict, index: int) -> None:
        self.index = index

    def handle(self, msg: tuple):
        if msg[0] == "cuda100":
            raise RuntimeError("Warp CUDA error 100: no CUDA-capable device is detected (in function init_cuda_driver)")
        if msg[0] == "segv":
            os.kill(os.getpid(), signal.SIGSEGV)
        if msg[0] == "boom":
            raise ValueError("bad pose")
        return msg

    def close(self) -> None:
        return None
