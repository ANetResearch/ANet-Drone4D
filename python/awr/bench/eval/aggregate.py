"""聚合与榜单（D3 §5.6.9；`d3-score-0` 原型榜与 `d3-score-1` 排行榜 v1，均不含合成权重）。

对每项指标 m ∈ {EC, EU, AO, CE}：事件级 `m_e = mean_{seed, rep} m(r)`，赛道级 `m_lane = mean_e m_e`（事件等权）。
95% 置信区间：两阶段分层自助法（每个事件内对种子有放回重采样，再对被抽中种子的重复有放回重采样；四项指标共用同一重采样序列，
缺省 10,000 次）；重采样种子：`d3-score-0` 取 `sha256(entry_id ‖ board_version ‖ lane)` 前 8 字节，`d3-score-1` 取
`HMAC-SHA256(K_board, entry_id ‖ board_version ‖ lane)` 前 8 字节（K_board 只存于该榜单版本的 EvalStore）。
名次：每项指标按点估计降序，相邻条目的配对差值 95% 区间含 0 时归入同一并列组；非支配层级：A 在四项点估计均不低于 B 且至少一项
配对差值区间整体大于 0 时 A 支配 B，逐层剥离。基线为参考行，不参与名次与分层（不影响他者层级；其 `dominance_tier` 为支配它的条目的最高层 + 1）。
无效条目（无效 Run 占比 > 20%）与非排名运行的处理见 `build_board`；合成接口 `compose` 只在评分版本给定权重时使用（缺省 `weights = null`）。

排行榜 v1（`d3-score-1`，D3-AC-43、D3-AC-57；M27-FR-164、FR-166、FR-177 至 FR-182）：榜单版本由 `configs/bench.d3/board-v1/board.json`
（`drone4d.bench.board_config.v1`，不含任何秘密）定义分轨、赛道、K、R 与评分版本；`seal_board` 按 test 划分把 test 事件归入事件类型赛道，
以 CSPRNG 生成秘密种子集与 K_board（只写入 EvalStore 文档 `drone4d.bench.board_evalstore.v1`，0600，服务端保存），并给出只含承诺值的
封板记录 `drone4d.bench.board_lock.v1`：每个 test 事件 `HMAC-SHA256(K_eval, "seed_set:" lane ":" event ":" seeds)`、每个赛道
`HMAC-SHA256(K_eval, "lane_seed_set:" lane ":" event=commit,...)`、榜单 `HMAC-SHA256(K_eval, "board_seed_set:" board_version ":" lane=commit,...)`。
服务端 `config.json` 为模板 + 封板记录 + EvalStore 路径（`board_config`），种子明文不进入配置、评测行、榜单与任何接口；评测行的 `seed`
为种子序号 k ∈ [0, K)。`build_board(board=cfg)` 按配置的分轨与赛道顺序输出全部分轨与赛道（含尚无条目者），只计入登记分轨、赛道与
test 事件的行；上榜条件为该赛道全部 test 事件的 K × R 个 Run 均完成，否则 `status = incomplete`（不参与名次与分层）；条目带
`display_order`（§5.6.9 缺省显示次序：非支配层级升序、同层按首次上榜时刻；其后为不排名条目，最后为基线参考行）。`frozen_diff` 给出
与已发布版本不同的冻结项（分轨、赛道与 test 事件清单、评分版本、test 划分版本、种子集承诺、评测器主版本、平台摘要、GPU 型号与
CPU ISA 等级），任一项不同须发布为新榜单版本。`d3-score-0` 的计算与输出逐字节不变（M32 §8.2 D3-INV-01 至 D3-INV-18）。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from collections import defaultdict
from pathlib import Path

import numpy as np

from awr.bench.eval.thresholds import EVALUATOR_VERSION, ORACLE_VERSION, SCORE_VERSION

__all__ = ["BOARD_CONFIG", "BOARD_EVALSTORE", "BOARD_LOCK", "EVENT_LANES", "METRICS", "SCORE_V1", "SCORE_VERSIONS", "BoardConfigError", "admits",
           "board_config", "board_key", "build_board", "check_board_config", "compose", "entry_details", "expand_runs", "frozen_diff", "is_v1",
           "lane_metrics", "load_board_template", "ranked_row", "run_spec_for", "score_definition", "seal_board", "verify_lock"]

METRICS = ("ec", "eu", "ao", "ce")
N_BOOT = 10_000


def _units(rows: list[dict]) -> dict[str, dict[int, list[dict]]]:
    """event → seed → [components]（重复）。"""
    u: dict[str, dict[int, list[dict]]] = defaultdict(lambda: defaultdict(list))
    for r in rows:
        u[r["event"]][int(r.get("seed", 0))].append(r["evaluation"]["components"])
    return u


def _point(units, m: str) -> float | None:
    ev = []
    for seeds in units.values():
        vals = [c[m] for reps in seeds.values() for c in reps if c.get(m) is not None]
        if vals:
            ev.append(float(np.mean(vals)))
    return float(np.mean(ev)) if ev else None


def _boot(units, rng: np.random.Generator, n: int) -> np.ndarray:
    """→ (n, 4) 重采样后的赛道级指标（向量化：每个事件一次抽取全部 n 组种子与重复下标）。"""
    acc = np.zeros((n, len(METRICS)))
    k = np.zeros((n, len(METRICS)))
    for e in sorted(units):
        seeds = [units[e][s] for s in sorted(units[e])]
        r = np.array([len(x) for x in seeds])
        a = np.full((len(seeds), int(r.max()), len(METRICS)), np.nan)
        for i, reps in enumerate(seeds):
            a[i, :len(reps)] = [[np.nan if c.get(m) is None else c[m] for m in METRICS] for c in reps]
        pick = rng.integers(0, len(seeds), (n, len(seeds)))
        rp = r[pick]
        ridx = (rng.random((n, len(seeds), a.shape[1])) * rp[..., None]).astype(np.int64)
        v = a[pick[..., None], ridx]
        v[np.arange(a.shape[1])[None, None, :] >= rp[..., None]] = np.nan
        ev = _nanmean(v.reshape(n, -1, len(METRICS)), axis=1)
        ok = np.isfinite(ev)
        acc += np.where(ok, ev, 0.0)
        k += ok
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(k > 0, acc / np.maximum(k, 1), np.nan)


def _nanmean(x: np.ndarray, axis: int) -> np.ndarray:
    ok = np.isfinite(x)
    c = ok.sum(axis=axis)
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(c > 0, np.where(ok, x, 0.0).sum(axis=axis) / np.maximum(c, 1), np.nan)


def _rng(*parts: str, secret: bytes | None = None) -> np.random.Generator:
    """重采样随机数：`secret` 缺省时为 `sha256(parts)`（`d3-score-0`），给出时为 `HMAC-SHA256(K_board, parts)`（`d3-score-1`）。"""
    msg = "‖".join(parts).encode()
    h = hashlib.sha256(msg).digest() if secret is None else hmac.new(secret, msg, hashlib.sha256).digest()
    return np.random.default_rng(int.from_bytes(h[:8], "little"))


def lane_metrics(rows: list[dict], *, key: tuple[str, str, str], n_boot: int = N_BOOT, secret: bytes | None = None) -> tuple[dict, np.ndarray]:
    """`key = (entry_id, board_version, lane)`；`secret` 为榜单版本的 K_board（`d3-score-1`），缺省按 `d3-score-0` 派生。"""
    units = _units(rows)
    boots = _boot(units, _rng(*key, secret=secret), n_boot)
    out = {}
    for j, m in enumerate(METRICS):
        v = _point(units, m)
        col = boots[:, j][np.isfinite(boots[:, j])]
        ci = [float(np.percentile(col, 2.5)), float(np.percentile(col, 97.5))] if len(col) and v is not None else [0.0, 0.0]
        rd = None
        per_seed = [[c[m] for c in rs if c.get(m) is not None] for seeds in units.values() for rs in seeds.values()]
        if any(len(x) > 1 for x in per_seed):
            rd = float(max(max(x) - min(x) for x in per_seed if len(x) > 1))
        out[m] = {"value": None if v is None else round(v, 6), "ci95": [round(ci[0], 6), round(ci[1], 6)], "repeat_delta": rd}
    return out, boots


def _paired_diff_ci(a: np.ndarray, b: np.ndarray) -> tuple[float, float]:
    d = a - b
    d = d[np.isfinite(d)]
    if not len(d):
        return (0.0, 0.0)
    return float(np.percentile(d, 2.5)), float(np.percentile(d, 97.5))


def ranked_row(r: dict) -> bool:
    """可进入正式榜的运行：Realistic 档的排名运行（Fast 档、SITL 与开发运行标注 `physics_not_ranked` 或 `ranked = false`）。"""
    ev = r["evaluation"]
    return ev.get("ranked", True) is not False and "physics_not_ranked" not in ev.get("invalid_reasons", ())


class _Pairs:
    """同一赛道内条目两两配对差值的 95% 区间（按需计算并缓存）。"""

    def __init__(self, boots: dict[str, np.ndarray]) -> None:
        self.boots, self.cache = boots, {}

    def ci(self, a: str, b: str, j: int) -> tuple[float, float]:
        k = (a, b, j)
        if k not in self.cache:
            self.cache[k] = _paired_diff_ci(self.boots[a][:, j], self.boots[b][:, j])
        return self.cache[k]

    def dominates(self, a: dict, b: dict) -> bool:
        """A 支配 B：四项点估计均不低于 B，且至少一项配对差值区间整体大于 0。"""
        va = [a["metrics"][m]["value"] for m in METRICS]
        vb = [b["metrics"][m]["value"] for m in METRICS]
        if any(x is None or y is None or x < y for x, y in zip(va, vb, strict=True)):
            return False
        return any(self.ci(a["entry_id"], b["entry_id"], j)[0] > 0 for j in range(len(METRICS)))


def _rank_lanes(entries: list[dict], boots_of: dict[tuple[str, str, str], np.ndarray]) -> None:
    """逐"分轨 × 赛道"：有效的非基线条目逐项名次与统计并列组、非支配层级；基线参考行只标注其所处层（就地写入 entries）。"""
    for (tr, lane) in sorted({(e["track"], e["lane"]) for e in entries}):
        here = [e for e in entries if e["track"] == tr and e["lane"] == lane and (tr, lane, e["entry_id"]) in boots_of and e["status"] == "valid"]
        pairs = _Pairs({e["entry_id"]: boots_of[(tr, lane, e["entry_id"])] for e in here})
        cand = [e for e in here if not e["baseline"]]
        for j, m in enumerate(METRICS):
            ranked = sorted([e for e in cand if e["metrics"][m]["value"] is not None], key=lambda e: (-e["metrics"][m]["value"], e["entry_id"]))
            group, rank = 0, 1
            for i, e in enumerate(ranked):
                if i > 0:
                    lo, hi = pairs.ci(ranked[i - 1]["entry_id"], e["entry_id"], j)
                    if not (lo <= 0.0 <= hi):
                        group += 1
                        rank = i + 1
                e["metrics"][m]["rank"] = rank
                e["metrics"][m]["tie_group"] = group
        remaining, tier = list(cand), 1
        while remaining:
            nd = [a for a in remaining if not any(pairs.dominates(b, a) for b in remaining if b is not a)]
            for a in nd:
                a["dominance_tier"] = tier
            remaining = [a for a in remaining if a not in nd]
            tier += 1
        for b in [e for e in here if e["baseline"]]:                     # 参考行：不改变他者层级，只标注其所处层
            b["dominance_tier"] = 1 + max((a["dominance_tier"] for a in cand if pairs.dominates(a, b)), default=0)


def build_board(rows: list[dict], *, board_version: str, n_boot: int = N_BOOT, track: dict | None = None,
                board: dict | None = None) -> dict:
    """rows: [{entry_id, team, display_name, baseline, track, lane, event, seed, evaluation, first_listed_unix_ns?}]。

    非排名运行（Fast 档、SITL、开发运行）不进入榜单；`track.dev = true` 的开发榜（Fast 档自检）例外，且不得发布为当前版本。
    `ranked_valid = false` 的 Run 占比 > 20% 的条目 `status = invalid`：指标只按有效 Run 计算并显示，不参与名次与分层
    （`dominance_tier = null`）。不同分轨、不同赛道的条目分组计算，互不混排。

    `board` 为榜单版本配置（`boardstore` 的 `config.json`）：`drone4d.bench.board_config.v1`（`d3-score-1`）时按排行榜 v1 规则计算
    （`_build_v1`）；其余（原型榜 `d3-score-0`）按本函数原有口径，输出逐字节不变。"""
    if _v1_selected(board):
        return _build_v1(rows, board, board_version=board_version, n_boot=n_boot)
    dev = bool((track or {}).get("dev"))
    groups: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    for r in rows:
        if dev or ranked_row(r):
            groups[(r["track"], r["lane"], r["entry_id"])].append(r)
    entries, boots_of = [], {}
    for (tr, lane, eid), rs in sorted(groups.items()):
        valid = [r for r in rs if r["evaluation"]["ranked_valid"]]
        invalid = len(valid) < 0.8 * len(rs) and not dev
        use = rs if dev else valid
        met, boots = lane_metrics(use, key=(eid, board_version, lane), n_boot=n_boot) if use else ({m: {"value": None, "ci95": [0.0, 0.0]}
                                                                                                    for m in METRICS}, None)
        r0 = rs[0]
        e = {"entry_id": eid, "team": r0.get("team", "internal"), "display_name": r0.get("display_name", eid), "track": tr, "lane": lane,
             "metrics": met, "status": "invalid" if invalid else "valid", "dominance_tier": None,
             "first_listed_unix_ns": int(r0.get("first_listed_unix_ns", 0)), "baseline": bool(r0.get("baseline"))}
        entries.append(e)
        if boots is not None:
            boots_of[(tr, lane, eid)] = boots
    _rank_lanes(entries, boots_of)
    used = [r for rs in groups.values() for r in rs]
    tracks = sorted({r["track"] for r in used})
    lanes = sorted({(r["lane"], r.get("event_type", "rendezvous_handoff")) for r in used})
    seeds = sorted({int(r.get("seed", 0)) for r in used})
    commit = hashlib.sha256(",".join(map(str, seeds)).encode()).hexdigest()
    t = track or {}
    return {"schema": "drone4d.bench.board.v1", "board_version": board_version,
            "tracks": [{"track": x, "fleet_ref": t.get("fleet_ref", "3x-x500"), "obs_level": t.get("obs_level", "L1"), "physics": "realistic",
                        "mode": "exogenous", "agent_class": t.get("agent_class", "python"), "appearance": "mesh"} for x in tracks],
            "lanes": [{"lane": ln, "kind": "event_type", "event_type": et, "test_events": sorted({r["event"] for r in used if r["lane"] == ln}),
                       "seed_set_commit": commit} for ln, et in lanes],
            "seed_set_commit": commit, "evaluator_major": int(EVALUATOR_VERSION.split(".")[0]),
            "platform_digest": "sha256:" + hashlib.sha256(f"{SCORE_VERSION}|{EVALUATOR_VERSION}|{t.get('platform', 'd3-gz:dev')}".encode()).hexdigest(),
            "entries": entries}


def compose(rows: list[dict], weights: dict[str, float], *, board_version: str, n_boot: int = N_BOOT, track: dict | None = None,
            board: dict | None = None) -> list[dict]:
    """合成接口（D3-AC-57 (h)；ADR-191）：以评分版本给定的权重，由存储的分量重算各条目的合成值与 95% 置信区间，无需重跑。
    合成值 = Σ w_m · m / Σ w_m；区间取同一重采样序列（与 `build_board` 相同的重采样种子）上合成值的 2.5% 与 97.5% 分位。
    `board` 为排行榜 v1 配置时，行的筛选、分组顺序与重采样种子（K_board）与 `build_board` 相同。"""
    w = np.array([float(weights[m]) for m in METRICS])
    if np.any(w < 0) or w.sum() <= 0:
        raise ValueError("weights must be non-negative with a positive sum")
    w = w / w.sum()
    secret = None
    if _v1_selected(board):
        v1 = _V1(board, board_version)
        items, secret = list(v1.groups(rows).items()), v1.secret
        dev = False
    else:
        dev = bool((track or {}).get("dev"))
        groups: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
        for r in rows:
            if dev or ranked_row(r):
                groups[(r["track"], r["lane"], r["entry_id"])].append(r)
        items = sorted(groups.items())
    out = []
    for (tr, lane, eid), rs in items:
        use = rs if dev else [r for r in rs if r["evaluation"]["ranked_valid"]]
        if not use:
            continue
        met, boots = lane_metrics(use, key=(eid, board_version, lane), n_boot=n_boot, secret=secret)
        vals = [met[m]["value"] for m in METRICS]
        if any(v is None for v in vals):
            out.append({"entry_id": eid, "track": tr, "lane": lane, "value": None, "ci95": [0.0, 0.0]})
            continue
        col = boots @ w
        col = col[np.isfinite(col)]
        out.append({"entry_id": eid, "track": tr, "lane": lane, "value": round(float(np.dot(w, vals)), 6),
                    "ci95": [round(float(np.percentile(col, 2.5)), 6), round(float(np.percentile(col, 97.5)), 6)]})
    return out


def entry_details(rows: list[dict], *, board: dict | None = None) -> dict[str, dict]:
    """BN-84 条目详情：entry_id → 按（分轨、赛道、test 事件）分解的四项指标均值、Run 数与无效 Run 数（不含合成值；
    `repeat_delta` 在 P0a 无重复运行，为 null）。`board` 为排行榜 v1 配置时只计入登记分轨、赛道与 test 事件的排名运行（与榜单同口径）。"""
    if _v1_selected(board):
        rows = [r for rs in _V1(board, board["board_version"], keyed=False).groups(rows).values() for r in rs]
    groups: dict[tuple[str, str, str, str], list[dict]] = defaultdict(list)
    for r in rows:
        groups[(r["entry_id"], r["track"], r["lane"], r["event"])].append(r)
    out: dict[str, dict] = {}
    for (eid, tr, lane, ev), rs in sorted(groups.items()):
        valid = [r for r in rs if r["evaluation"]["ranked_valid"]]
        use = valid or rs
        met = {}
        for m in METRICS:
            vals = [r["evaluation"]["components"][m] for r in use if r["evaluation"]["components"].get(m) is not None]
            met[m] = float(np.mean(vals)) if vals else None
        reasons = sorted({x for r in rs for x in r["evaluation"].get("invalid_reasons", [])})
        d = out.setdefault(eid, {"entry_id": eid, "display_name": rs[0].get("display_name", eid), "baseline": bool(rs[0].get("baseline")),
                                 "tracks": {}})
        lanes = d["tracks"].setdefault(tr, {})
        lanes.setdefault(lane, []).append({"event": ev, "n_runs": len(rs), "invalid_runs": len(rs) - len(valid), "invalid_reasons": reasons,
                                           "seeds": sorted({int(r.get("seed", 0)) for r in rs}), "metrics": met, "repeat_delta": None})
    return out


# ------------------------------------------------------------------------------------------------------------ 排行榜 v1（d3-score-1）
SCORE_V1 = "d3-score-1"
BOARD_CONFIG = "drone4d.bench.board_config.v1"
BOARD_LOCK = "drone4d.bench.board_lock.v1"
BOARD_EVALSTORE = "drone4d.bench.board_evalstore.v1"
# 事件类型赛道与 event_type 的对应（AWR-05 §5.6.9；M27 §15.3）
EVENT_LANES = {"reid": "reid_occlusion", "handoff": "rendezvous_handoff", "vehicle": "vehicle_track", "search": "target_search"}
# 评分版本：只定义分量与归一化，`weights = null`（ADR-191；Q-D3-19）；`rng` 为置信区间重采样随机数的派生方式，`lanes` 为赛道来源
SCORE_VERSIONS = {
    SCORE_VERSION: {"stage": "prototype", "rng": "sha256(entry_id‖board_version‖lane)", "lanes": "rows", "weights": None},
    SCORE_V1: {"stage": "board-v1", "rng": "hmac-sha256(K_board, entry_id‖board_version‖lane)", "lanes": "config", "weights": None},
}
# 榜单版本冻结项（M27-FR-164、FR-177）：任一项与已发布版本不同即须新榜单版本
FROZEN = ("score_version", "tracks", "lanes", "seed_set_commit", "test_split_version", "evaluator_major", "platform_digest", "gpu_model",
          "cpu_isa_level")
# 平台镜像摘要（AWR-05 §5.6.9"榜单版本"）：gz 与插件、M25 Worker、Env、基线镜像
PLATFORM_IMAGES = ("gz", "m25_worker", "env", "baseline")
_SECRET_KEYS = frozenset({"seed", "seeds", "k_eval", "k_board"})     # 配置与封板记录中不得出现的键（种子明文与密钥只在 EvalStore）
_HEX64 = frozenset("0123456789abcdef")
_OBS = ("L1", "L2", "L2-depth")
_TEMPLATE = Path(__file__).resolve().parents[4] / "configs" / "bench.d3" / "board-v1" / "board.json"


class BoardConfigError(ValueError):
    """排行榜 v1 的榜单版本配置、封板记录或 EvalStore 不合规（`boardstore` 转为 `BoardVersionError`）。"""


def _hex64(x) -> bool:
    return isinstance(x, str) and len(x) == 64 and set(x) <= _HEX64


def _major() -> int:
    return int(EVALUATOR_VERSION.split(".")[0])


def is_v1(cfg: dict | None) -> bool:
    """榜单版本配置是否为排行榜 v1（`drone4d.bench.board_config.v1`）。"""
    return bool(cfg) and cfg.get("schema") == BOARD_CONFIG


def _v1_selected(cfg: dict | None) -> bool:
    """`build_board`、`compose`、`entry_details` 的分派：v1 配置走 v1 规则；声明了要求配置赛道的评分版本却不是 v1 配置的，拒绝。"""
    if is_v1(cfg):
        return True
    sv = (cfg or {}).get("score_version")
    if sv is not None and SCORE_VERSIONS.get(sv, {}).get("lanes") == "config":
        raise BoardConfigError(f"score_version {sv} needs a {BOARD_CONFIG} board config (configs/bench.d3/board-v1) sealed by seal_board")
    return False


def score_definition(version: str) -> dict:
    """`bench/score.v1` 的评分版本定义：四项分量与归一化，`weights = null`（合成主得分不计算、不显示）。"""
    if version not in SCORE_VERSIONS:
        raise BoardConfigError(f"unknown score_version {version}")
    return {"schema": "drone4d.bench.score.v1", "score_version": version, "components": list(METRICS),
            "normalization": {"ec": "ratio of key events captured", "eu": "report F1",
                              "ao": {"metric": "ao_real", "oracle": ORACLE_VERSION, "clip": [0, 1]},
                              "ce": {"form": "g_hat * f_res", "g_hat": "clip((V(U) - max_u V({u})) / (1 - 1/|U|), 0, 1)",
                                     "f_res": "clip(1 - 0.5 * max(0, rho - 1), 0, 1)", "budget": "bench/task.v1.budget"},
                              "ranked_valid_false": "all components 0",
                              "aggregate": "event value = mean over seeds x repeats; lane value = equal-weight mean over test events",
                              "ci95": {"method": "two-stage stratified bootstrap", "n_boot": N_BOOT, "rng": SCORE_VERSIONS[version]["rng"]}},
            "weights": SCORE_VERSIONS[version]["weights"]}


def load_board_template(path: Path | None = None) -> dict:
    """读取排行榜 v1 的榜单版本模板（缺省 `configs/bench.d3/board-v1/board.json`）。"""
    return json.loads(Path(path or _TEMPLATE).read_text(encoding="utf-8"))


def _secret_paths(x, path: str = "") -> list[str]:
    if isinstance(x, dict):
        return [p for k, v in x.items() for p in ([f"{path}.{k}"] if k in _SECRET_KEYS else []) + _secret_paths(v, f"{path}.{k}")]
    if isinstance(x, list):
        return [p for i, v in enumerate(x) for p in _secret_paths(v, f"{path}[{i}]")]
    return []


def check_board_config(cfg: dict, *, sealed: bool = False) -> list[str]:
    """排行榜 v1 榜单版本配置的检查（→ 问题列表，空为合规）：评分版本 `d3-score-1`、物理档 realistic、事件模式 exogenous、
    分轨唯一且外观层为 mesh、事件类型赛道与 event_type 一一对应、K、R 与每赛道事件数为正整数、配置与封板记录中无种子明文或密钥；
    `sealed = True` 时还要求封板记录（`lock`）与 EvalStore 路径，且封板记录与模板一致（赛道集合、每赛道 test 事件数、承诺值格式、
    test 事件不跨事件类型赛道）。"""
    p: list[str] = []
    if cfg.get("schema") != BOARD_CONFIG:
        return [f"schema must be {BOARD_CONFIG}"]
    if cfg.get("score_version") != SCORE_V1:
        p.append(f"score_version must be {SCORE_V1}")
    bv = str(cfg.get("board_version", ""))
    if not bv or not bv[0].isalnum() or len(bv) > 64 or not all(c.isalnum() or c in "._-" for c in bv):
        p.append(f"board_version {bv!r} is not a valid board version name")
    if cfg.get("physics") != "realistic":
        p.append("physics must be realistic (gz lockstep only)")
    if cfg.get("mode") != "exogenous":
        p.append("mode must be exogenous")
    p += [f"{k} must be a positive integer" for k in ("seeds_per_event", "repeats", "events_per_lane")
          if not isinstance(cfg.get(k), int) or isinstance(cfg.get(k), bool) or cfg[k] < 1]
    p += [f"secret key {x} must not appear in a board config (seeds and keys live only in the EvalStore)" for x in _secret_paths(cfg)]
    tracks = cfg.get("tracks") or []
    ids = [t.get("track") for t in tracks]
    if not tracks or len(set(ids)) != len(ids):
        p.append("tracks must be non-empty with unique ids")
    for t in tracks:
        for k in ("track", "fleet_ref", "obs_level", "agent_class", "appearance", "n_uav", "vehicle_ref"):
            if t.get(k) in (None, ""):
                p.append(f"track {t.get('track')}: {k} is required")
        if t.get("obs_level") not in _OBS:
            p.append(f"track {t.get('track')}: obs_level must be one of {list(_OBS)}")
        if t.get("appearance") != "mesh":
            p.append(f"track {t.get('track')}: ranked tracks only take appearance mesh (aerial from P3 once registered)")
        if (t.get("run_spec") or {}).get("physics", "realistic") != "realistic":
            p.append(f"track {t.get('track')}: run_spec.physics must be realistic")
    lanes = cfg.get("lanes") or []
    names = [ln.get("lane") for ln in lanes]
    if not lanes or len(set(names)) != len(names):
        p.append("lanes must be non-empty with unique ids")
    for ln in lanes:
        if ln.get("kind") == "event_type":
            if EVENT_LANES.get(ln.get("lane")) != ln.get("event_type"):
                p.append(f"lane {ln.get('lane')}: an event-type lane maps {EVENT_LANES} one to one")
        elif ln.get("kind") == "task":
            if not ln.get("test_events"):
                p.append(f"lane {ln.get('lane')}: a research task lane lists its test events explicitly")
        else:
            p.append(f"lane {ln.get('lane')}: kind must be event_type or task")
    if not sealed:
        return p
    lock = cfg.get("lock")
    if not isinstance(lock, dict) or lock.get("schema") != BOARD_LOCK:
        return [*p, f"lock ({BOARD_LOCK}) is required: seal the board version with seal_board"]
    if not cfg.get("evalstore"):
        p.append("evalstore (server-side path of the EvalStore) is required")
    if lock.get("board_version") != bv or lock.get("score_version") != cfg.get("score_version"):
        p.append("lock board_version or score_version differs from the config")
    if [ln.get("lane") for ln in lock.get("lanes", [])] != names:
        p.append("lock lanes differ from the config lanes")
    seen: dict[str, str] = {}
    for ln in lock.get("lanes", []):
        evs = ln.get("test_events") or []
        if len(evs) != cfg.get("events_per_lane") or len(set(evs)) != len(evs):
            p.append(f"lane {ln.get('lane')}: needs exactly {cfg.get('events_per_lane')} distinct test events, has {len(evs)}")
        if sorted(ln.get("event_commits") or {}) != sorted(evs) or not all(_hex64(c) for c in (ln.get("event_commits") or {}).values()):
            p.append(f"lane {ln.get('lane')}: event_commits must hold one HMAC per test event")
        if not _hex64(ln.get("seed_set_commit")):
            p.append(f"lane {ln.get('lane')}: seed_set_commit must be a 64-hex HMAC")
        if ln.get("kind") == "event_type":
            for e in evs:
                if e in seen:
                    p.append(f"test event {e} is in both lanes {seen[e]} and {ln.get('lane')}")
                seen[e] = ln.get("lane")
    if not _hex64(lock.get("seed_set_commit")):
        p.append("lock seed_set_commit must be a 64-hex HMAC")
    if not lock.get("test_split_version"):
        p.append("lock test_split_version is required")
    plat = lock.get("platform") or {}
    if sorted(plat) != sorted(PLATFORM_IMAGES) or not all(isinstance(v, str) and v.startswith("sha256:") and _hex64(v[7:]) for v in plat.values()):
        p.append(f"lock platform must give a sha256 image digest for each of {list(PLATFORM_IMAGES)}")
    for k in ("gpu_model", "cpu_isa_level"):
        if not lock.get(k):
            p.append(f"lock {k} is required")
    return p


def _mac(key: bytes, msg: str) -> str:
    return hmac.new(key, msg.encode(), hashlib.sha256).hexdigest()


def _event_commit(k_eval: bytes, lane: str, event: str, seeds: list[int]) -> str:
    return _mac(k_eval, f"seed_set:{lane}:{event}:" + ",".join(str(int(s)) for s in seeds))


def _lane_commit(k_eval: bytes, lane: str, event_commits: dict[str, str]) -> str:
    return _mac(k_eval, f"lane_seed_set:{lane}:" + ",".join(f"{e}={c}" for e, c in sorted(event_commits.items())))


def _board_commit(k_eval: bytes, board_version: str, lane_commits: list[tuple[str, str]]) -> str:
    return _mac(k_eval, f"board_seed_set:{board_version}:" + ",".join(f"{ln}={c}" for ln, c in lane_commits))


def _draw_seeds(n: int, taken: set[int]) -> list[int]:
    """CSPRNG 种子（M27-FR-005、FR-166）：`secrets.randbits(63)`，不为 0（0 为公开冒烟种子），在整个榜单版本内不重复。"""
    out: list[int] = []
    while len(out) < n:
        s = secrets.randbits(63)
        if s and s not in taken:
            taken.add(s)
            out.append(s)
    return out


def seal_board(cfg: dict, test_events: dict[str, str], *, test_split_version: str, platform: dict[str, str], gpu_model: str, cpu_isa_level: str,
               sealed_utc: str, store: dict | None = None) -> tuple[dict, dict]:
    """封板（M27-FR-166、FR-177）：`test_events` 为 test 划分中的事件 → event_type（`awr.bench.split` 登记的 test 事件及其 `plan.json`）。
    事件类型赛道按 event_type 自动归属（模板中显式列出 `test_events` 的赛道以列出者为准）；每赛道须恰有 `events_per_lane` 个 test 事件。
    `store` 缺省时以 CSPRNG 生成 K_eval、K_board 与每个（赛道、test 事件）K 个秘密种子；给出时（重新封板、复核）沿用其种子与密钥。
    → (封板记录 `drone4d.bench.board_lock.v1`，只含承诺值；EvalStore 文档 `drone4d.bench.board_evalstore.v1`，含明文，只在服务端保存)。"""
    p = check_board_config(cfg)
    if p:
        raise BoardConfigError("; ".join(p))
    k, bv = int(cfg["seeds_per_event"]), str(cfg["board_version"])
    picked: list[tuple[dict, list[str]]] = []
    for ln in cfg["lanes"]:
        if ln.get("test_events"):
            evs = [str(e) for e in ln["test_events"]]
            bad = [e for e in evs if e not in test_events or (ln["kind"] == "event_type" and test_events[e] != ln["event_type"])]
            if bad:
                raise BoardConfigError(f"lane {ln['lane']}: {bad} are not test events of type {ln.get('event_type')}")
        else:
            evs = sorted(e for e, t in test_events.items() if t == ln.get("event_type"))
        if len(evs) != int(cfg["events_per_lane"]):
            raise BoardConfigError(f"lane {ln['lane']}: the test split holds {len(evs)} {ln.get('event_type')} events, the board version needs "
                                   f"{cfg['events_per_lane']} (list them in board.json lanes[].test_events when there are more)")
        picked.append((ln, evs))
    if store is None:
        taken: set[int] = set()
        store = {"schema": BOARD_EVALSTORE, "board_version": bv, "k_eval": secrets.token_hex(32), "k_board": secrets.token_hex(32),
                 "seeds": {ln["lane"]: {e: _draw_seeds(k, taken) for e in evs} for ln, evs in picked}}
    k_eval = bytes.fromhex(store["k_eval"])
    lanes, lane_commits = [], []
    for ln, evs in picked:
        try:
            ec = {e: _event_commit(k_eval, ln["lane"], e, store["seeds"][ln["lane"]][e]) for e in evs}
        except KeyError as e:
            raise BoardConfigError(f"evalstore has no seeds for {ln['lane']}/{e}") from None
        lc = _lane_commit(k_eval, ln["lane"], ec)
        lane_commits.append((ln["lane"], lc))
        lanes.append({"lane": ln["lane"], "kind": ln["kind"], **({"event_type": ln["event_type"]} if ln.get("event_type") else {}),
                      "test_events": evs, "event_commits": ec, "seed_set_commit": lc})
    lock = {"schema": BOARD_LOCK, "board_version": bv, "score_version": cfg["score_version"], "test_split_version": str(test_split_version),
            "lanes": lanes, "seed_set_commit": _board_commit(k_eval, bv, lane_commits), "evaluator_major": _major(),
            "platform": {x: str(platform[x]) for x in PLATFORM_IMAGES if x in platform}, "gpu_model": str(gpu_model),
            "cpu_isa_level": str(cpu_isa_level), "sealed_utc": str(sealed_utc)}
    p = check_board_config({**cfg, "lock": lock, "evalstore": "-"}, sealed=True) + verify_lock(lock, store, seeds_per_event=k)
    if p:
        raise BoardConfigError("; ".join(p))
    return lock, store


def verify_lock(lock: dict, store: dict, *, seeds_per_event: int | None = None) -> list[str]:
    """以 EvalStore 中的明文复核封板记录的承诺值（M27-AC-046）：事件、赛道与榜单三级 HMAC 全部相符，种子个数为 K、不为 0、
    在榜单版本内不重复。→ 问题列表（不含种子明文）。"""
    p: list[str] = []
    if store.get("schema") != BOARD_EVALSTORE or store.get("board_version") != lock.get("board_version"):
        return ["evalstore schema or board_version does not match the lock"]
    k_eval = bytes.fromhex(store["k_eval"])
    allseeds: list[int] = []
    lane_commits = []
    for ln in lock.get("lanes", []):
        seeds = (store.get("seeds") or {}).get(ln["lane"], {})
        ec = {}
        for e in ln.get("test_events", []):
            ss = [int(x) for x in seeds.get(e, [])]
            allseeds += ss
            if seeds_per_event is not None and len(ss) != seeds_per_event:
                p.append(f"{ln['lane']}/{e}: evalstore holds {len(ss)} seeds, the board version needs {seeds_per_event}")
            ec[e] = _event_commit(k_eval, ln["lane"], e, ss)
            if not hmac.compare_digest(ec[e], str((ln.get("event_commits") or {}).get(e, ""))):
                p.append(f"{ln['lane']}/{e}: event seed commit does not match the evalstore")
        lc = _lane_commit(k_eval, ln["lane"], ec)
        lane_commits.append((ln["lane"], lc))
        if not hmac.compare_digest(lc, str(ln.get("seed_set_commit", ""))):
            p.append(f"{ln['lane']}: lane seed_set_commit does not match the evalstore")
    if not hmac.compare_digest(_board_commit(k_eval, str(lock["board_version"]), lane_commits), str(lock.get("seed_set_commit", ""))):
        p.append("board seed_set_commit does not match the evalstore")
    if 0 in allseeds or len(set(allseeds)) != len(allseeds):
        p.append("secret seeds must be non-zero and unique within the board version")
    if not _hex64(store.get("k_board")):
        p.append("evalstore k_board must be 32 bytes hex")
    return p


def board_config(cfg: dict, lock: dict, evalstore: Path | str) -> dict:
    """服务端 `boards/<board_version>/config.json` 的内容：模板 + 封板记录 + EvalStore 路径（不含种子明文与密钥）。"""
    out = {**cfg, "lock": lock, "evalstore": str(evalstore)}
    p = check_board_config(out, sealed=True)
    if p:
        raise BoardConfigError("; ".join(p))
    return out


def board_key(cfg: dict) -> bytes:
    """读取 EvalStore 中的 K_board（置信区间重采样的 HMAC 密钥），并先以明文复核封板记录；EvalStore 缺失或不符时拒绝（失败关闭）。"""
    path = Path(str(cfg.get("evalstore") or ""))
    if not cfg.get("evalstore") or not path.is_file():
        raise BoardConfigError(f"board {cfg.get('board_version')}: evalstore {cfg.get('evalstore')} is not readable")
    store = json.loads(path.read_text(encoding="utf-8"))
    p = verify_lock(cfg["lock"], store, seeds_per_event=int(cfg["seeds_per_event"]))
    if p:
        raise BoardConfigError(f"board {cfg.get('board_version')}: " + "; ".join(p))
    return bytes.fromhex(store["k_board"])


def admits(cfg: dict, track: str, lanes: list[str] | None = None) -> str | None:
    """该榜单版本能否接收某提交（领取排队提交时判定）：→ None 可接收；否则原因 `track_unknown` 或 `lane_unknown`（682）。"""
    if track not in {t["track"] for t in cfg.get("tracks", [])}:
        return "track_unknown"
    if lanes and not set(lanes) <= {ln["lane"] for ln in cfg.get("lanes", [])}:
        return "lane_unknown"
    return None


def expand_runs(cfg: dict, track: str | None = None, lanes: list[str] | None = None) -> list[dict]:
    """正式评测展开（M27-FR-160、FR-183）："分轨 × 赛道 × test 事件 × 种子序号 × 重复"，不含种子明文；缺省全部分轨与全部赛道。"""
    p = check_board_config(cfg, sealed=True)
    if p:
        raise BoardConfigError("; ".join(p))
    out = []
    for t in cfg["tracks"]:
        if track is not None and t["track"] != track:
            continue
        for ln in cfg["lock"]["lanes"]:
            if lanes and ln["lane"] not in lanes:
                continue
            for e in ln["test_events"]:
                out += [{"track": t["track"], "lane": ln["lane"], "event": e, "seed_index": k, "rep": rep}
                        for k in range(int(cfg["seeds_per_event"])) for rep in range(int(cfg["repeats"]))]
    return out


def run_spec_for(cfg: dict, track: str, *, world_id: str, event_id: str, event_version: int, starts: list[dict], policy: dict) -> dict:
    """某分轨在某 test 事件上的单个排名 Run 规格（`bench/run_spec.v1`，不含种子：评测员按种子序号从 EvalStore 取明文并签发
    `seed_commit`）。`starts` 为各机起飞位姿（`{"kind": "pose", ...}`，取自事件的起飞点），个数须等于分轨机数。"""
    t = next((x for x in cfg["tracks"] if x["track"] == track), None)
    if t is None:
        raise BoardConfigError(f"track {track} is not registered in board {cfg.get('board_version')}")
    if len(starts) != int(t["n_uav"]):
        raise BoardConfigError(f"track {track} flies {t['n_uav']} UAVs, got {len(starts)} start poses")
    rs = {"schema": "drone4d.bench.run_spec.v1",
          "experiment": {"name": f"board {cfg['board_version']} {track} {event_id}", "project": "d3-board-v1"},
          "world": {"id": world_id}, "event": {"id": event_id, "version": int(event_version)},
          "fleet": [{"uav_id": f"uav-{i + 1:02d}", "vehicle_ref": t["vehicle_ref"], "start": s} for i, s in enumerate(starts)],
          "policy": policy, "physics": "realistic", "obs_level": t["obs_level"]}
    return {**rs, **{k: v for k, v in (t.get("run_spec") or {}).items() if k not in rs}, "ranked": True}


def frozen_diff(old: dict, new: dict) -> list[str]:
    """两个 `board.json` 之间不同的冻结项（M27-FR-164）；非空即须发布为新榜单版本。"""
    return [k for k in FROZEN if old.get(k) != new.get(k)]


def _platform_digest(lock: dict) -> str:
    doc = {"score_version": lock["score_version"], "evaluator_major": lock["evaluator_major"], "platform": lock["platform"],
           "gpu_model": lock["gpu_model"], "cpu_isa_level": lock["cpu_isa_level"]}
    return "sha256:" + hashlib.sha256(json.dumps(doc, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class _V1:
    """排行榜 v1 的行筛选与分组（`build_board`、`compose`、`entry_details` 共用）。"""

    def __init__(self, cfg: dict, board_version: str, *, keyed: bool = True) -> None:
        p = check_board_config(cfg, sealed=True)
        if p:
            raise BoardConfigError("; ".join(p))
        if cfg["board_version"] != board_version:
            raise BoardConfigError(f"board config is for {cfg['board_version']}, published as {board_version}")
        if int(cfg["lock"]["evaluator_major"]) != _major():
            raise BoardConfigError(f"board {board_version} is frozen at evaluator major {cfg['lock']['evaluator_major']}, platform is "
                                   f"{_major()}: publish a new board version")
        self.cfg, self.lock = cfg, cfg["lock"]
        self.k, self.r = int(cfg["seeds_per_event"]), int(cfg["repeats"])
        self.tracks = [t["track"] for t in cfg["tracks"]]
        self.lanes = [ln["lane"] for ln in self.lock["lanes"]]
        self.events = {ln["lane"]: list(ln["test_events"]) for ln in self.lock["lanes"]}
        self.secret = board_key(cfg) if keyed else None

    def groups(self, rows: list[dict]) -> dict[tuple[str, str, str], list[dict]]:
        """（分轨、赛道、条目）→ 行，按配置的分轨与赛道顺序、条目 id 排序，组内按（事件、种子序号、重复序号、run_id）排序（结果与
        rows.jsonl 的追加次序无关）；只计入登记分轨、赛道与该赛道 test 事件的排名运行；评测行的 `seed` 须为种子序号 k ∈ [0, K)
        （否则视为种子明文混入，拒绝）。"""
        g: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
        for r in rows:
            if not ranked_row(r) or r.get("track") not in self.tracks or r.get("lane") not in self.events or r.get("event") not in self.events[r["lane"]]:
                continue
            k = int(r.get("seed", 0))
            if not 0 <= k < self.k:
                raise BoardConfigError(f"row of {r.get('entry_id')} carries seed {'index' if k < 0 else 'value'} outside [0, {self.k}): rows hold "
                                       "seed indices only")
            g[(r["track"], r["lane"], r["entry_id"])].append(r)
        order = {**{t: i for i, t in enumerate(self.tracks)}, **{ln: i for i, ln in enumerate(self.lanes)}}
        return {k: sorted(rs, key=_run_key) for k, rs in sorted(g.items(), key=lambda kv: (order[kv[0][0]], order[kv[0][1]], kv[0][2]))}

    def complete(self, lane: str, rs: list[dict]) -> bool:
        """上榜条件：该赛道全部 test 事件的 K × R 个 Run 均完成（平台原因失败的 Run 已重跑，只追加成功的一次）。"""
        n: dict[tuple[str, int], int] = defaultdict(int)
        for r in rs:
            n[(r["event"], int(r.get("seed", 0)))] += 1
        return all(n[(e, k)] >= self.r for e in self.events[lane] for k in range(self.k))


def _run_key(r: dict) -> tuple:
    return (r["event"], int(r.get("seed", 0)), int(r.get("rep", 0)), str(r.get("run_id", "")))


def _display_order(entries: list[dict]) -> None:
    """§5.6.9 缺省显示次序（只决定列表顺序）：逐"分轨 × 赛道"，排名条目按非支配层级升序、同层按首次上榜时刻；其后为无效与未完成条目
    （按首次上榜时刻）；最后为基线参考行。写入 `display_order`（1 起）。"""
    by: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for e in entries:
        by[(e["track"], e["lane"])].append(e)
    for es in by.values():
        def key(e: dict) -> tuple:
            block = 2 if e["baseline"] else 0 if e["status"] == "valid" and e["dominance_tier"] is not None else 1
            return (block, (e["dominance_tier"] or 0) if block == 0 else 0, e["first_listed_unix_ns"], e["entry_id"])
        for i, e in enumerate(sorted(es, key=key)):
            e["display_order"] = i + 1


def _build_v1(rows: list[dict], cfg: dict, *, board_version: str, n_boot: int) -> dict:
    """排行榜 v1：见 `build_board` 与模块说明。"""
    v1 = _V1(cfg, board_version)
    entries, boots_of = [], {}
    for (tr, lane, eid), rs in v1.groups(rows).items():
        valid = [r for r in rs if r["evaluation"]["ranked_valid"]]
        status = "incomplete" if not v1.complete(lane, rs) else "invalid" if len(valid) < 0.8 * len(rs) else "valid"
        met, boots = (lane_metrics(valid, key=(eid, board_version, lane), n_boot=n_boot, secret=v1.secret) if valid
                      else ({m: {"value": None, "ci95": [0.0, 0.0]} for m in METRICS}, None))
        r0 = rs[0]
        entries.append({"entry_id": eid, "team": r0.get("team", "internal"), "display_name": r0.get("display_name", eid), "track": tr, "lane": lane,
                        "metrics": met, "status": status, "dominance_tier": None, "first_listed_unix_ns": int(r0.get("first_listed_unix_ns", 0)),
                        "baseline": bool(r0.get("baseline"))})
        if boots is not None:
            boots_of[(tr, lane, eid)] = boots
    _rank_lanes(entries, boots_of)
    _display_order(entries)
    pos = {(e["track"], e["lane"]): (v1.tracks.index(e["track"]), v1.lanes.index(e["lane"])) for e in entries}
    entries.sort(key=lambda e: (*pos[(e["track"], e["lane"])], e["display_order"]))
    lock = v1.lock
    return {"schema": "drone4d.bench.board.v1", "board_version": board_version, "score_version": cfg["score_version"],
            "tracks": [{"track": t["track"], "fleet_ref": t["fleet_ref"], "obs_level": t["obs_level"], "physics": "realistic", "mode": "exogenous",
                        "agent_class": t["agent_class"], "appearance": t["appearance"]} for t in cfg["tracks"]],
            "lanes": [{"lane": ln["lane"], "kind": ln["kind"], **({"event_type": ln["event_type"]} if ln.get("event_type") else {}),
                       "test_events": list(ln["test_events"]), "seed_set_commit": ln["seed_set_commit"]} for ln in lock["lanes"]],
            "seed_set_commit": lock["seed_set_commit"], "test_split_version": lock["test_split_version"], "evaluator_major": int(lock["evaluator_major"]),
            "platform_digest": _platform_digest(lock), "gpu_model": lock["gpu_model"], "cpu_isa_level": lock["cpu_isa_level"],
            "seeds_per_event": v1.k, "repeats": v1.r, "entries": entries}
