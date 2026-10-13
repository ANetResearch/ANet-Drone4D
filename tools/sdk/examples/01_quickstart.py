"""01 快速开始：创建沙盒（满员时自动排队）、读会话状态、申请倍速、订阅会话状态 5 秒、导出快照、结束。

    python 01_quickstart.py [http://127.0.0.1:8002]
"""

from __future__ import annotations

from _common import sandbox


def main() -> None:
    sb = sandbox()
    print("世界：", [w["world_id"] for w in sb.worlds() if w["available"]])

    def on_queue(kind: str, view: dict) -> None:
        print(kind, view.get("position"), view.get("eta_s"))

    with sb.create("synthcity", "t5_area_guard", on_queue=on_queue) as s:
        st = s.state()
        print("会话", s.sid, st["state"], "剩余寿命", st["expires_in_s"], "s")
        g = s.clock("speed", rate=5)
        print("申请 x5，授予", g["granted_rate"], "上限", g["rate_cap"], "受限原因", g.get("limited_by"))
        for m in s.stream(["sandbox/session"], seconds=5):
            print(m.topic, m.data["state"], m.data["clock"])
        snap = s.snapshot()
        print("快照：", len(snap.get("vehicles", [])), "架机，", len(snap.get("targets", [])), "个识别物")


if __name__ == "__main__":
    main()
