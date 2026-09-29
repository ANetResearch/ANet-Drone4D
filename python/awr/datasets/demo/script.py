"""演示提示卡（M16-FR-027；M16 §6.5；13 §4.4 为演示脚本内容的定义方）。

终端纯文本，无 emoji 与禁用字形（D1-AC-20）。S1 的时刻取自 AWR-12 §7.2 的业务时间线（仿真秒），×10 时墙钟为十分之一。
"""

from __future__ import annotations

from .check import EXT_AC, CheckItem

__all__ = ["S1_TIMELINE_S", "card_lines", "fmt_wall"]

# AWR-12 §7.2 / M16 §6.4.2：仿真时间（s）
S1_TIMELINE_S = {"p600-01 开始扫描": 82.0, "p600-02 开始扫描": 134.0, "阵风": 420.0, "p600-02 扫描完成": 612.0,
                 "p600-01 扫描完成": 738.0, "两机着陆": 786.0}


def fmt_wall(sim_s: float, rate: float) -> str:
    s = round(sim_s / rate)
    return f"{s // 60}:{s % 60:02d}"


def card_lines(items: list[CheckItem], *, port: int = 8000, host: str = "<host>", ext: dict[str, str] | None = None,
               rehearsal_webm: str | None = None) -> list[str]:
    ok = sum(1 for it in items if it.status in ("pass", "manual", "warn"))
    lines = [f"[demo] 检查清单：{ok}/{len(items)} 项可继续（" +
             "、".join(f"{it.title_zh} {({'pass': '通过', 'fail': '不通过', 'warn': '告警', 'manual': '人工'})[it.status]}"
                      for it in items) + "）"]
    lines.append(f"[demo] 访问：ssh -N -L {port}:127.0.0.1:{port} <user>@{host}  然后打开 http://localhost:{port}/world/shenzhen")
    lines.append("[demo] D0  打开 /world/shenzhen：遮罩揭开后自动加载 S1（深圳双机立面巡检）")
    lines.append("[demo] D1  环绕主塔；着色依次切换 Height、HAG、Class；切到上海再切回深圳（上海为静态浏览）")
    t = S1_TIMELINE_S
    lines.append(f"[demo] D2  命令面板重新加载 S1 并按 ×10 播放：p600-01 约 {fmt_wall(t['p600-01 开始扫描'], 10)}、"
                 f"p600-02 约 {fmt_wall(t['p600-02 开始扫描'], 10)} 开始扫描；阵风约 {fmt_wall(t['阵风'], 10)}；"
                 f"约 {fmt_wall(t['两机着陆'], 10)} 两机着陆，时钟继续走")
    lines.append(f"[demo]     ×1 时刻：扫描开始 {fmt_wall(t['p600-01 开始扫描'], 1)} 与 {fmt_wall(t['p600-02 开始扫描'], 1)}；"
                 f"阵风 {fmt_wall(t['阵风'], 1)}；着陆 {fmt_wall(t['两机着陆'], 1)}")
    lines.append("[demo] D3  ×1；预设切换 rain、thunderstorm；风速调到 8 m/s（预设过渡 30 s）")
    lines.append("[demo] D4  禁飞区：nofly-sz-t2 圆心 (-98, 346.5) 半径 60 m（点选被拒，原因码 102）；GoTo 示例楼顶 (-262, 350.5)")
    lines.append("[demo] D5  加载剧本 ladder-shenzhen（200 架，约 40 s 全体入圆）；全机 RTL 用命令面板 \"RTL all\"")
    ext = ext or {}
    for ac, label in EXT_AC.items():
        st = ext.get(ac, "NONE")
        if st in ("PASS", "WARN"):
            lines.append(f"[demo] {label.split()[0]}  {label.split(maxsplit=1)[1]}：可演示（{ac} {st}）")
        else:
            lines.append(f"[demo] {label.split()[0]}  {label.split(maxsplit=1)[1]}：跳过（{ac} 无通过记录，DEMO-E006）")
    lines.append("[demo]     S3 在纽约：make run WORLD=newyork SCENARIO=s3-newyork-sar")
    lines.append("[demo] D7  打开关于对话框：数据来源 UrbanScene3D（Lin et al., ECCV 2022），科研用途；示意坐标，不得用于真实导航")
    if rehearsal_webm:
        lines.append(f"[demo] 兜底录屏：{rehearsal_webm}")
    else:
        lines.append("[demo] 兜底录屏：尚无（make demo-rehearse RECORD=1 生成）")
    return lines
