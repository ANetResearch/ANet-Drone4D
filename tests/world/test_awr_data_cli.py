"""M03-FR-045：`awr data *` 以插件方式挂到 M11 的 `awr` CLI（`awr.jobs.awr_cli.register`）。"""

from __future__ import annotations

import argparse

from awr.jobs import awr_cli


def _parser():
    ap = argparse.ArgumentParser(prog="awr")
    sub = ap.add_subparsers(dest="cmd", required=True)
    awr_cli.register(sub)
    awr_cli.register(sub)                           # 重复注册无副作用
    return ap


def test_fetch_worlds_without_mirror_is_data_missing(monkeypatch, repo_env, capsys):
    monkeypatch.delenv("AWR_DATA_MIRROR", raising=False)
    a = _parser().parse_args(["data", "fetch", "worlds"])
    assert a.fn(a) == 4
    assert capsys.readouterr().out.strip().splitlines()[-1] == "make worlds"


def test_fetch_unknown_dataset_rejected():
    import pytest

    with pytest.raises(SystemExit):
        _parser().parse_args(["data", "fetch", "kitti"])


def test_runtime_cli_picks_up_plugin():
    from awr.runtime import cli

    ap = cli.build_parser()
    a = ap.parse_args(["data", "prune-archive", "urbanscene3d", "--raw", "/nonexistent-awr"])
    assert a.fn is awr_cli._run
