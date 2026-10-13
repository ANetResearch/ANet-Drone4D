"""04 识别物与明确捕获：放置一个人，派一架机定点盯看，等待 `perception.capture` 事件。"""

from __future__ import annotations

from _common import sandbox


def main() -> None:
    sb = sandbox()
    with sb.create("synthcity", "t1_point_watch") as s:
        t = s.targets.create({"class": "person", "pos": [60.0, 40.0, None], "label": "人-01"})
        print("识别物", t["id"])
        task = s.tasks.create({"type": "point_watch", "params": {"point": [60.0, 40.0, 0.0], "alt_m": 60, "look": {"mode": "target",
                                                                                                                  "target": t["id"]}}})
        s.tasks.start(task["tid"])
        s.clock("speed", rate=5)
        for m in s.stream(["event"], seconds=120):
            ev = m.data
            if isinstance(ev, dict) and ev.get("type", ev.get("kind")) == "perception.capture":
                print("明确捕获：", ev.get("data"))
                break


if __name__ == "__main__":
    main()
