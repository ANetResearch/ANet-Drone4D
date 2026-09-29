"""Capability probes shared by the engine stubs (P-M01-6: probe, do not hard-code)."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]


def has_cuda_gpu() -> bool:
    """True when an NVIDIA device node or `nvidia-smi` is present (the D1 host has none, AWR-03 Q1)."""
    return Path("/dev/nvidia0").exists() or shutil.which("nvidia-smi") is not None


def venv_python(name: str) -> Path:
    """Interpreter of an engine venv (`.venv-<name>`, AWR-19); AWR_RECON_VENV_<NAME> overrides."""
    env = os.environ.get(f"AWR_RECON_VENV_{name.upper()}")
    base = Path(env) if env else REPO / f".venv-{name}"
    return base / "bin" / "python"


def models_dir(name: str) -> Path:
    return Path(os.environ.get("AWR_MODELS_DIR", REPO / "data" / "models")) / name


def gpu_reason() -> str:
    return "NOT_IMPLEMENTED" if has_cuda_gpu() else "GPU_REQUIRED"
