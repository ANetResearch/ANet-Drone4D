"""Env 运行写入器（M27 `EnvRecorderSink` 的 P0a 版）：运行目录内的 Agent 层与真值层。

    <run_dir>/
      manifest.json                 运行清单（规格摘要、物理档与样本流摘要、统计；不含 run_seed）
      task.json                     任务契约 bench/task.v1
      agents/<uav>/config.json      交付的 agent_config.v1
      agents/<uav>/acts.jsonl       Agent 原样动作（二进制载荷以 base64 记 {"$b64": ...}）与回执
      agents/<uav>/obs.sha256       每次交付观测的规范 JSON 摘要（k、sha256）
      reports.jsonl                 守卫通过的 report（uav、k_obs、apply_tick、t_apply_ns、report）
      net/records.jsonl             链路逐条记录
      truth/fleet.npz               每 tick 末真值位姿与导航位置（t_ns、pos、vel、yaw、nav、status）
      truth/l1.jsonl                L1 检测的真值映射（uav、seq、t_ns、det_id、actor、h_px、h_unocc_px、vis、ident、delivered）
      truth/l2.jsonl                L2 相机帧的可见度归约（uav、sensor、seq、t_ns、actor、h_unocc_px、vis、bbox_xywh）
      truth/contacts.jsonl          接触事件
      truth/run_seed.json           运行种子（真值层；评测与审计用，不对 Agent 可见）
"""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

import numpy as np

__all__ = ["EnvRecorder", "canonical_json"]


def _enc(x):
    if isinstance(x, bytes | bytearray):
        return {"$b64": base64.b64encode(bytes(x)).decode()}
    if isinstance(x, dict):
        return {k: _enc(v) for k, v in x.items()}
    if isinstance(x, list | tuple):
        return [_enc(v) for v in x]
    if isinstance(x, np.generic):
        return x.item()
    return x


def canonical_json(x) -> bytes:
    return json.dumps(_enc(x), sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


class EnvRecorder:
    def __init__(self, run_dir: Path | None) -> None:
        self.dir = Path(run_dir) if run_dir else None
        self._fh: dict[str, object] = {}
        if self.dir:
            (self.dir / "truth").mkdir(parents=True, exist_ok=True)
            (self.dir / "net").mkdir(exist_ok=True)
            (self.dir / "agents").mkdir(exist_ok=True)

    def _line(self, rel: str, obj) -> None:
        if not self.dir:
            return
        f = self._fh.get(rel)
        if f is None:
            p = self.dir / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            f = self._fh[rel] = p.open("w", encoding="utf-8")
        f.write(canonical_json(obj).decode() + "\n")

    def write_json(self, rel: str, obj) -> None:
        if self.dir:
            p = self.dir / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(_enc(obj), ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")

    def on_obs(self, uav: str, k: int, msg: dict) -> str:
        h = hashlib.sha256(canonical_json(msg)).hexdigest()
        if self.dir:
            f = self._fh.get(f"agents/{uav}/obs.sha256")
            if f is None:
                p = self.dir / "agents" / uav / "obs.sha256"
                p.parent.mkdir(parents=True, exist_ok=True)
                f = self._fh[f"agents/{uav}/obs.sha256"] = p.open("w", encoding="utf-8")
            f.write(f"{k} {h}\n")
        return h

    def on_act(self, uav: str, k: int, act, acks: list[dict], apply_tick: int | None) -> None:
        self._line(f"agents/{uav}/acts.jsonl", {"k": k, "apply_tick": apply_tick, "act": act, "acks": acks})

    def on_report(self, uav: str, k_obs: int, apply_tick: int, t_apply_ns: int, rep: dict) -> None:
        self._line("reports.jsonl", {"uav": uav, "k_obs": k_obs, "apply_tick": apply_tick, "t_apply_ns": t_apply_ns, "report": rep})

    def on_l1(self, uav: str, seq: int, t_ns: int, det: dict, truth: dict) -> None:
        self._line("truth/l1.jsonl", {"uav": uav, "seq": seq, "t_ns": t_ns, "det_id": det["det_id"], "class": det["class"], "h_px": det["h_px"],
                                      **{k: truth[k] for k in ("actor", "h_unocc_px", "vis", "ident")}})

    def on_vis(self, row: dict) -> None:
        self._line("truth/l2.jsonl", row)

    def on_contact(self, ev) -> None:
        self._line("truth/contacts.jsonl", {"uav": ev.uav, "t_ns": ev.t_ns, "kind": ev.kind, "detector": ev.detector,
                                            "pos_enu_m": list(ev.pos_enu_m), "speed_mps": ev.speed_mps})

    def net(self, records: list[dict]) -> None:
        for r in records:
            self._line("net/records.jsonl", r)

    def fleet(self, uavs: list[str], t_ns: list[int], pos, vel, yaw, nav, status) -> None:
        if self.dir:
            np.savez_compressed(self.dir / "truth" / "fleet.npz", uavs=np.array(uavs), t_ns=np.asarray(t_ns, np.int64),
                                pos=np.asarray(pos, np.float64), vel=np.asarray(vel, np.float32), yaw=np.asarray(yaw, np.float32),
                                nav=np.asarray(nav, np.float64), status=np.asarray(status, "U10"))

    def close(self) -> None:
        for f in self._fh.values():
            f.close()
        self._fh.clear()
