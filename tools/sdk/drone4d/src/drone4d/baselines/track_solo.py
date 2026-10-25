"""`track_solo`（r6 §12.3 `track3_nocomm`，M26-FR-081）：与 `track_share` 相同的观察位、跟踪站位与丢失外推，但不通信——
不发送目击、跟踪声明与"已报"告知，收到的消息一律忽略；各机只依本机轨迹选择跟踪目标（本机看到的目标优先、近者优先），
report 的同类型 30 s 抑制只对本机生效。用于在同一机队规模下对照"共享与接力"的协同收益（r6 结论 3、4）。"""

from __future__ import annotations

from drone4d.baselines.track_share import TrackShare

__all__ = ["TrackSolo"]


class TrackSolo(TrackShare):
    name = "track_solo"

    def on_inbox(self, inbox):
        return []

    def messages(self, obs):
        self.just_reported = []
        return []
