"""泄漏红队：种子与噪声反演（R3）、行为攻击（R4）、特权档 662 与目标搜索先验泄漏扫描（M26 §8.3、FR-103；D3-AC-16、62；A07）。

在排名配置（`ranked = True`、63 位 CSPRNG 种子、M27 种子承诺值、realistic 噪声档）上运行 L1 回合，录下 Agent 收到的全部消息，
在 A06 的 `capture`/`truth`/`search` 框架上执行红队方法 R3 与 R4：

R3 种子与噪声反演（§8.3）：候选集 = 收到的全部整数字段值、非图像载荷中的全部 4 与 8 字节窗口、`agent_seed`、[0, 2²⁰) 的小整数、
`run_id` 的 base32 解码值。对每个候选重算 GNSS 的一阶高斯–马尔可夫（GM）误差过程与 L1 地面位置噪声，与真实噪声实现比对（相关系数
≤ 0.5）；并以"导航解减去候选噪声"估计本机位置，修正后位置误差 RMS 与标称 sigma 之比的 95% 置信下界 ≥ 0.9（即不优于标称）。判据同时
断言运行种子与派生键不在候选集内（反演无从起步）。正向对照：把真实种子注入候选集后反演复现相关系数 ≈ 1，证明估计器可用。

R4 行为攻击（§8.3）：A1 航点收敛后以指令点估计本机位置（估计系 goto，收敛误差即导航误差，不优于 sigma_h）；A3 跨帧 `det_id` 不可关联
（关联率 0、det_id 与演员序号卡方 p ≥ 0.01）；A4 列表次序与演员序号秩相关 |rho| ≤ 0.05；A5/A7 在线限幅与告警探测——演员相关的限幅、
接触与禁入告警不出现，链路相关字段（RSSI、余量、他机投递结果）不下发（schema 不可表达，L-13、L-14、L-21）。

特权档（M26-FR-020、FR-029；§7.11）：排名运行请求 T0 特征档返回 662；`BenchEnv` 不接受 L1/L2/L2-depth 之外的观测档。

L1 检测（L-04、L-05；A07）：`det_id` 每帧由带密钥 BLAKE2b 生成，跨帧不可关联、不暴露内部随机键；检测不含真值 id 与 `n_in_fov`。

目标搜索先验泄漏扫描（D3-AC-62）：`brief.json` 与 `bench/task.v1` 只含先验（公开锚点、目标外观描述、任务区），不含演员轨迹、
事件区间与参与者身份；以 `search.audit_frames` 对投影做结构化检索，演员位置与标注区间 0 命中。目标搜索事件类型本身由 E08 实现
（A07 路径不含事件代码），本用例以可用的合成事件验证简报投影的通用泄漏口径，`target_search` 专项见遗留。

渲染以运动学开发档、真实 SDK 基线（Unix 套接字）；正式口径（realistic gz 物理、真实渲染、Agent 容器、3 机 × 360 s × 2 种子）
由 B10/A08 在 dmax 上以 `record_container_episode` 执行。
"""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import math
import sys
from pathlib import Path

import numpy as np
import pytest

from awr.agentsdk.env.core import TICK_NS, BenchEnv
from awr.agentsdk.env.l1 import L1Model, det_id_key
from awr.agentsdk.env.nav import _PROFILES
from awr.agentsdk.env.spec import EnvSpec, FleetEntry
from awr.agentsdk.task import brief_for_agent, task_from_event
from awr.agentsdk.wire import PROTOCOL, T, WireSocket
from awr.bench.redteam import (
    ProbedEnv,
    Secrets,
    TruthSet,
    baseline_launcher,
    leak_report,
    record_socket_episode,
)
from awr.bench.redteam.truth import derived_keys
from awr.sim.sensors import cbrng

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
SDK = ROOT / "tools/sdk/drone4d/src"
sys.path.insert(0, str(HERE.parents[0] / "env"))

_p = HERE.parents[0] / "env" / "conftest.py"
_spec = importlib.util.spec_from_file_location("d3_env_conftest_inv", _p)
_m = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_m)


@pytest.fixture(scope="session")
def synth_event(tmp_path_factory):
    """合成交接事件，舞台寻路固定为栅格后端：本模块的统计判据按该磁带标定（Detour 与栅格的背景演员轨迹不同，见 env conftest
    `synth_event_at`），结果不随本机是否装有 navsvc 依赖或 `AWR_EVENT_NAV` 取值而变。"""
    yield from _m.synth_event_at(tmp_path_factory, nav="grid")

L1_FLEET = (FleetEntry("uav-01", (-10.0, 50.0, 40.0), 0.0, airborne=True),
            FleetEntry("uav-02", (12.0, 48.0, 40.0), 0.0, airborne=True),
            FleetEntry("uav-03", (0.0, 38.0, 44.0), 0.0, airborne=True))
CORR_MAX = 0.5                 # R3：噪声复现相关系数上界
RATIO_LB = 0.9                 # R3：修正后位置误差 RMS / 标称 sigma 的 95% 置信下界
RANK_MAX = 0.05                # R4-A4：次序与演员序号秩相关上界
SMALL_INT_SWEEP = 2048         # [0, 2²⁰) 小整数候选的有界批量（本机；全量在 dmax）


def _ranked_spec(synth_event, out: Path, case: str, *, obs_level: str, fleet, dur: float, l2: dict | None = None) -> EnvSpec:
    """排名配置：63 位高熵种子、M27 形式的运行标识与 HMAC 种子承诺值（种子按用例名以带密钥 BLAKE2b 派生，可复现）。"""
    w, ev = synth_event
    d = hashlib.blake2b(case.encode(), key=b"d3-redteam-a07", digest_size=32).digest()
    seed = int.from_bytes(d[:8], "little") >> 1
    run_id = f"run-{d[8:16].hex()}"
    commit = "hmac-sha256:" + hashlib.sha256(d[16:] + seed.to_bytes(8, "little")).hexdigest()
    return EnvSpec(run_id, seed, w, ev, fleet, obs_level=obs_level, physics="kinematic", duration_s=dur, out_dir=out, ranked=True,
                   seed_commit=commit, l2=dict(l2 or {}))


# ============================================================================================ 反演器（R3）


def _gm_white(seeds: np.ndarray, uav_no: int, n_samples: int) -> np.ndarray:
    """候选种子集的 GNSS GM 驱动白噪声增量 w[i]（流 22，通道 nav_gm 0..2）→ (ns, n_samples·3)。

    GM 误差 e[i] = phi·e[i-1] + sigma·sqrt(1−phi²)·w[i]（e[0] = sigma·w[0]）。白化观测误差（见 `_whiten`，用公开的标称
    sigma、tau）还原的正是 w[i]：对真实种子逐位复现，对其他种子给出 IID 的独立过程，相关检验不受 tau 长相关时间导致的样本
    自相关影响（这正是 §8.3 R3 用标称噪声模型反演的口径）。与 `cbrng.normal_elem` 同键，`NavModel._sample` 以同一 w 递推。"""
    seeds = np.asarray(seeds, np.uint64)
    i = np.arange(n_samples)
    w = cbrng.normal_elem(seeds[:, None, None], 22, np.uint64(uav_no), i[None, :, None], np.array([0, 1, 2]))
    return w.reshape(len(seeds), -1)


def _whiten(e_samp: np.ndarray, prof: tuple[float, float, float, float]) -> np.ndarray:
    """1 s 网格误差样本 (N, 3) → 驱动白噪声增量 w (N, 3)：w[0] = e[0]/sigma、w[i] = (e[i] − phi·e[i-1])/(sigma·sqrt(1−phi²))。"""
    sig_h, sig_v, tau, _ = prof
    sig = np.array([sig_h, sig_h, sig_v])
    phi = math.exp(-1.0 / tau)
    c = sig * math.sqrt(1.0 - phi * phi)
    w = np.empty_like(e_samp)
    w[0] = e_samp[0] / sig
    w[1:] = (e_samp[1:] - phi * e_samp[:-1]) / c
    return w


def _corr(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """逐候选的皮尔逊相关系数绝对值：a (ns, m)、b (m,) → (ns,)。"""
    a = a - a.mean(1, keepdims=True)
    b = b - b.mean()
    num = a @ b
    den = np.sqrt((a * a).sum(1) * float(b @ b))
    return np.where(den > 0, np.abs(num) / np.where(den > 0, den, 1.0), 0.0)


def _candidate_seeds(frames, truth: TruthSet) -> set[int]:
    """反演候选种子集（§8.3 R3）：收到的全部整数字段值、非图像载荷的 4 与 8 字节窗口（u32/u64）、`agent_seed`、
    [0, 2²⁰) 小整数（有界批量）、`run_id` 的 base32 解码值。全部按 u64 取模收集。"""
    from awr.bench.redteam.search import _Scan

    cands: set[int] = set(range(SMALL_INT_SWEEP))
    mask = (1 << 64) - 1
    for fr in frames:
        sc = _Scan(fr.doc)
        for _p, v in sc.ints:
            cands.add(int(v) & mask)
        # 非图像载荷的 4 与 8 字节窗口
        for _p, b, img in sc.bins:
            if img or len(b) < 4:
                continue
            a = np.frombuffer(b, np.uint8)
            if len(a) >= 8:
                w8 = np.lib.stride_tricks.sliding_window_view(a, 8).copy().view("<u8").ravel()
                cands.update(int(x) for x in np.unique(w8))
            w4 = np.lib.stride_tricks.sliding_window_view(a, 4).copy().view("<u4").ravel()
            cands.update(int(x) for x in np.unique(w4))
    # agent_seed（若在 CONFIG 下发）与 run_id 的 base32 解码
    for fr in frames:
        if fr.kind == "config":
            ag = (fr.doc or {}).get("agent_seed")
            if isinstance(ag, int):
                cands.add(int(ag) & mask)
    try:
        pad = truth.secrets.run_id.split("-", 1)[-1].upper()
        pad += "=" * (-len(pad) % 8)
        raw = base64.b32decode(pad, casefold=True)
        if raw:
            cands.add(int.from_bytes(raw[:8].ljust(8, b"\0"), "little") & mask)
    except Exception:
        pass
    return cands


def _observed_nav_error(frames, truth: TruthSet, uav: str) -> tuple[np.ndarray, np.ndarray]:
    """某机每个 OBS 帧的 (观测导航位置 − 真值位置, 真值位置)。"""
    t_list, e_list = [], []
    for fr in frames:
        if fr.kind != "obs" or fr.uav != uav:
            continue
        nav = fr.doc["obs"]["nav"]
        t_list.append(int(nav["t_ns"]))
        e_list.append(np.asarray(nav["pos_enu_m"], np.float64))
    if not t_list:
        return np.zeros((0, 3)), np.zeros((0, 3))
    t = np.asarray(t_list, np.int64)
    true = truth.own_samples(uav, t)["own_pose/position"]
    return np.asarray(e_list) - true, true


def _grid_error(frames, truth: TruthSet, uav: str) -> np.ndarray:
    """某机在整数秒帧（t % 1 s == 0）上的误差样本 (N, 3)：恰为 GM 过程的 1 s 网格样本（插值 frac = 0）。"""
    t_list, nav = [], []
    for fr in frames:
        if fr.kind != "obs" or fr.uav != uav:
            continue
        t = int(fr.doc["obs"]["nav"]["t_ns"])
        if t % 1_000_000_000 == 0:
            t_list.append(t)
            nav.append(np.asarray(fr.doc["obs"]["nav"]["pos_enu_m"], np.float64))
    if not t_list:
        return np.zeros((0, 3))
    t = np.asarray(t_list, np.int64)
    return np.asarray(nav) - truth.own_samples(uav, t)["own_pose/position"]


def _invert_nav(frames, truth: TruthSet, uav_nos: list[tuple[str, int]], extra_seeds: set[int] | None = None):
    """以候选种子反演导航 GM 噪声（白化增量域）→ (最大相关系数, 候选数, 修正后噪声标准差与标称之比的 95% 置信下界)。

    对每架机白化观测误差得驱动白噪声 w_obs，拼接多机（各机为独立的 GM 实现，流 22 的 agent 维不同）；候选种子的 w_cand 与之
    相关。R3 判据：全部候选相关系数 ≤ 0.5；并以最佳候选做一元回归"扣除候选噪声"，修正后残差标准差与标称（白化域为 1）之比的
    95% 置信下界 ≥ 0.9（即无法把本机定位误差降到标称以下）。`extra_seeds` 注入额外候选（正向对照注入真实种子）。"""
    w_parts = []
    for u, no in uav_nos:
        e_samp = _grid_error(frames, truth, u)
        assert len(e_samp) >= 2
        w_parts.append((int(no), _whiten(e_samp, _PROFILES[truth.noise]).reshape(-1), len(e_samp)))
    w_obs = np.concatenate([w for _no, w, _n in w_parts])
    cands = _candidate_seeds(frames, truth)
    if extra_seeds:
        cands |= extra_seeds
    seeds = np.array(sorted(cands), np.uint64)
    best, w_best = 0.0, None
    for i in range(0, len(seeds), 2048):
        batch = seeds[i:i + 2048]
        cand = np.concatenate([_gm_white(batch, no, n)[:, :3 * n] for (no, _w, n) in w_parts], axis=1)
        c = _corr(cand, w_obs)
        j = int(np.argmax(c))
        if float(c[j]) > best:
            best, w_best = float(c[j]), cand[j].copy()
    beta = float(w_obs @ w_best) / float(w_best @ w_best) if w_best is not None and float(w_best @ w_best) > 0 else 0.0
    resid = w_obs - beta * (w_best if w_best is not None else 0.0)
    k = len(w_obs)
    from scipy.stats import chi2

    lb = math.sqrt(k * float((resid ** 2).mean()) / chi2.ppf(0.95, k))       # 白化域标称标准差为 1
    return best, len(seeds), lb


# ============================================================================================ 脚本 Agent（goto）


class _GotoAgent:
    """脚本 Agent（线协议）：反复 goto 到简报任务区内的固定导航系航点并悬停，用于航点收敛攻击（A1）。"""

    def __init__(self, path: Path) -> None:
        self.path, self.n_obs, self.error = path, 0, None
        import threading

        self._t = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._t.start()

    def join(self, timeout: float) -> None:
        self._t.join(timeout)

    def _run(self) -> None:
        import socket
        import time

        try:
            c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            for _ in range(200):
                try:
                    c.connect(str(self.path))
                    break
                except OSError:
                    time.sleep(0.05)
            ws = WireSocket(c)
            ws.send(T.HELLO, {"protocol": PROTOCOL, "sdk_version": "redteam-a07", "prefs": {}})
            _t, cfg = ws.recv(time.monotonic() + 30)                     # CONFIG
            p0 = cfg["start"]["nav_pos_enu_m"]
            ws.send(T.READY, {})
            while True:
                t, body = ws.recv(time.monotonic() + 60)
                if t != T.OBS:
                    break
                k = body["obs"]["k"]
                self.n_obs += 1
                wp = (p0[0] + 8.0, p0[1] - 6.0, 38.0) if (k // 40) % 2 == 0 else (p0[0] - 6.0, p0[1] + 8.0, 42.0)
                act = {"schema": "drone4d.bench.act.v1", "k": k,
                       "nav": {"mode": "goto", "pos_enu_m": list(wp), "speed_mps": 6.0},
                       "gimbal": {"sensor_id": cfg["sensors"][0]["sensor_id"], "mode": "angles", "pitch_deg": -90.0, "yaw_deg": 0.0}}
                ws.send(T.ACT, act)
            c.close()
        except Exception as e:                                           # 由用例断言
            self.error = e


def _goto_launch(hosts) -> list[_GotoAgent]:
    ags = [_GotoAgent(h.path) for h in hosts.values()]
    for a in ags:
        a.start()
    return ags


# ============================================================================================ 夹具


@pytest.fixture(scope="module")
def ranked_run(synth_event, tmp_path_factory):
    """L1 排名运行：3 机 × 30 s，真实 SDK 基线 `track_share`（子进程，经 Unix 套接字）。供反演与行为攻击复用。"""
    out = tmp_path_factory.mktemp("rt-inv")
    spec = _ranked_spec(synth_event, out / "run", "inv-l1", obs_level="L1", fleet=L1_FLEET, dur=36.0)
    run = record_socket_episode(ProbedEnv(spec), baseline_launcher("track_share", SDK), capture_dir=out / "capture")
    rep = leak_report(run, config={"case": "A07 R3/R4 L1 track_share"})
    return run, rep, out


# ============================================================================================ L1 det_id（L-04、L-05）


def test_det_id_is_keyed_and_unlinkable() -> None:
    """`det_id` 由带密钥 BLAKE2b 生成：同种子逐字节可复现、跨帧与跨演员不同、无密钥不可预测，且不再是内部随机键的直接下发。"""
    m = L1Model.__new__(L1Model)
    m.seed = 0x0123_4567_89AB_CDEF
    m._det_key = det_id_key(m.seed)
    a = m.det_id(1, 7, 100)
    assert len(a) == 16 and all(ch in "0123456789abcdef" for ch in a)
    assert m.det_id(1, 7, 100) == a                                      # 同输入可复现
    assert m.det_id(1, 7, 101) != a and m.det_id(1, 8, 100) != a and m.det_id(2, 7, 100) != a
    # 另一运行种子下同 (uav, actor, seq) 的 det_id 不同（密钥换了）
    m2 = L1Model.__new__(L1Model)
    m2.seed = m.seed ^ 1
    m2._det_key = det_id_key(m2.seed)
    assert m2.det_id(1, 7, 100) != a
    # 不等于旧实现 hex64(seed, 21, ag, seq, 4)（曾直接下发计数器随机键）
    legacy = f"{int(cbrng.key(m.seed, 21, np.uint64(1 * 65536 + 7), np.uint64(100), np.uint64(4))):016x}"
    assert a != legacy
    # 跨帧关联：1000 个 (actor, seq) 的 det_id 互不相同（无跨帧可关联的常量部分）
    ids = {m.det_id(1, ac, sq) for ac in range(20) for sq in range(50)}
    assert len(ids) == 1000


def test_l1_detection_fields_carry_no_truth(ranked_run) -> None:
    """L1 检测字段 ⊆ 白名单，不含真值 id、演员序号或 `n_in_fov`；地面位置带噪（非整毫米真值）。"""
    run, _rep, _out = ranked_run
    allowed = {"det_id", "sensor_id", "seq", "t_mid_ns", "class", "bbox_xywh", "h_px", "slant_range_m", "ground_xy_noisy", "appearance"}
    truth_ids = {a["id"] for a in run.env.tape.actors}
    n_dets = 0
    for fr in run.capture.frames:
        if fr.kind != "obs":
            continue
        for d in (fr.doc["obs"]["sensors"] or {}).get("detections") or []:
            n_dets += 1
            assert set(d) <= allowed and "n_in_fov" not in d
            assert d["det_id"] not in truth_ids and "actor" not in d and "actor_no" not in d
    assert n_dets > 0


# ============================================================================================ R3 反演


def test_candidate_seeds_exclude_run_seed_and_derived(ranked_run) -> None:
    """反演候选集（整数字段、字节窗口、小整数、run_id 解码）不含运行种子与 SplitMix64 派生键——反演无从起步。"""
    run, _rep, _out = ranked_run
    truth = TruthSet.from_env(run.env)
    cands = _candidate_seeds(run.capture.frames, truth)
    assert len(cands) >= SMALL_INT_SWEEP
    seed = int(run.env.spec.seed) & ((1 << 64) - 1)
    assert seed not in cands
    dk = derived_keys(int(run.env.spec.seed))
    assert all((int(v) & ((1 << 64) - 1)) not in cands for v in dk.values())


def _uav_nos(env) -> list[tuple[str, int]]:
    return [(u, env.uavs.index(u)) for u in env.uavs]


def test_nav_noise_inversion_fails(ranked_run) -> None:
    """以候选种子反演导航 GM 噪声（白化增量域，拼接三机独立实现）：全部候选相关系数 ≤ 0.5；正向对照注入真实种子 → ≈ 1。"""
    run, _rep, _out = ranked_run
    truth = TruthSet.from_env(run.env)
    best, n_cand, _lb = _invert_nav(run.capture.frames, truth, _uav_nos(run.env))
    assert n_cand >= 200 and best <= CORR_MAX
    # 正向对照：真实种子在候选集内即被反演复现（估计器可用）
    best_pos, _n, _lb2 = _invert_nav(run.capture.frames, truth, _uav_nos(run.env), extra_seeds={int(run.env.spec.seed)})
    assert best_pos > 0.999


def test_corrected_position_not_better_than_nominal(ranked_run) -> None:
    """以最佳候选"扣除候选噪声"估计本机位置：白化域修正后残差标准差与标称之比的 95% 置信下界 ≥ 0.9（样本 ≥ 200），
    即任一候选都无法把本机定位误差降到标称以下。"""
    run, _rep, _out = ranked_run
    truth = TruthSet.from_env(run.env)
    best, _n, lb = _invert_nav(run.capture.frames, truth, _uav_nos(run.env))
    assert best <= CORR_MAX and lb >= RATIO_LB


def test_l1_ground_noise_inversion_fails(ranked_run) -> None:
    """以候选种子反演 L1 地面位置噪声通道（流 21，l1_gx/l1_gy）：全部候选相关系数 ≤ 0.5；真实种子注入后复现（估计器可用）。

    真实地面噪声由 `L1Model` 以 `nrm(seed, 21, uav_no·65536+actor_no, seq, ch)` 给出。这里以真值层登记的 `det_id → actor`
    直接构造最强攻击（上界）：残差 = ground_xy_noisy − (真值演员 xy + 导航误差 xy) = sigma·[w_gx, w_gy]。相关系数与尺度无关，
    故负向检验不需要 sigma；正向对照以真实斜距估得的 sigma 还原量级。"""
    from awr.agentsdk.env.l1 import L1Params

    run, _rep, _out = ranked_run
    truth = TruthSet.from_env(run.env)
    rows = []                                                            # (uav_no, actor_no, seq, t_ns, gx, gy)
    for fr in run.capture.frames:
        if fr.kind != "obs":
            continue
        uav_no = run.env.uavs.index(fr.uav)
        t = int(fr.doc["obs"]["t_ns"])
        for d in (fr.doc["obs"]["sensors"] or {}).get("detections") or []:
            a = truth.det_actor.get(d["det_id"])
            if a is None:
                continue
            g = d["ground_xy_noisy"]
            rows.append((uav_no, a, int(d["seq"]), t, float(g[0]), float(g[1])))
    if len(rows) < 30:
        pytest.skip("too few identified detections for the ground-noise inversion sample")
    arr = np.array(rows)
    ag = arr[:, 0].astype(np.int64) * 65536 + arr[:, 1].astype(np.int64)
    seqv = arr[:, 2].astype(np.int64)
    # 真实残差与真实斜距（供正向对照还原 sigma）
    resid2, rng = [], []
    for uav_no, a, _seq, t, gx, gy in rows:
        act_xyz = truth.actor_samples(np.array([int(t)]))[0, int(a)]
        e = run.env.nav.error(int(uav_no), int(t))[:2]
        resid2.append([gx - (act_xyz[0] + e[0]), gy - (act_xyz[1] + e[1])])
        rng.append(float(np.linalg.norm(act_xyz - run.env.probe_rows[run.env.uavs[int(uav_no)]][0][0:3])))
    resid2 = np.asarray(resid2)
    resid = resid2.reshape(-1, order="F")                                # [全部 gx, 全部 gy]
    cands = _candidate_seeds(run.capture.frames, truth)
    seeds = np.array(sorted(cands), np.uint64)
    best = 0.0
    for i in range(0, len(seeds), 1024):
        batch = seeds[i:i + 1024]
        gx = cbrng.normal_elem(batch[:, None], 21, ag[None, :], seqv[None, :], 1)
        gy = cbrng.normal_elem(batch[:, None], 21, ag[None, :], seqv[None, :], 2)
        best = max(best, float(_corr(np.concatenate([gx, gy], axis=1), resid).max()))
    assert best <= CORR_MAX
    # 正向对照：真实种子 × 标称 sigma 还原残差
    P = L1Params()
    sig = P.sig0_m + P.sig_per_m * np.asarray(rng)
    gx = cbrng.normal_elem(np.uint64(int(run.env.spec.seed)), 21, ag, seqv, 1) * sig
    gy = cbrng.normal_elem(np.uint64(int(run.env.spec.seed)), 21, ag, seqv, 2) * sig
    recon = np.concatenate([gx, gy])
    assert float(_corr(recon[None, :], resid)[0]) > 0.95


# ============================================================================================ R4 行为攻击


def test_goto_convergence_attack_cannot_beat_nominal(synth_event, tmp_path: Path) -> None:
    """A1 航点收敛攻击：Agent 以估计系 goto 反复收敛到已知导航航点。goto 目标在导航系解释（守卫以当前导航误差换算为真值系），
    收敛后指令点与真值位置之差即导航误差，攻击者据此得不到优于标称 sigma 的本机位置——反演仍失败、修正后残差 95% 下界 ≥ 0.9。"""
    out = tmp_path
    spec = _ranked_spec(synth_event, out / "run", "attack-goto", obs_level="L1", fleet=L1_FLEET, dur=40.0)
    env = ProbedEnv(spec)
    run = record_socket_episode(env, _goto_launch, capture_dir=out / "capture")
    truth = TruthSet.from_env(run.env)
    # goto 确有收敛（status.goto_reached 至少出现一次），确保攻击路径被真正走过
    reached = sum(1 for fr in run.capture.frames if fr.kind == "obs" and fr.doc["obs"]["status"]["goto_reached"])
    assert reached >= 1
    best, _n, lb = _invert_nav(run.capture.frames, truth, _uav_nos(run.env))
    assert best <= CORR_MAX and lb >= RATIO_LB


def test_detid_cross_frame_association_is_zero(ranked_run) -> None:
    """A3 跨帧 `det_id` 关联：同一演员在不同帧的 det_id 互不相同（跨帧关联率 0）；det_id 比特与演员序号卡方 p ≥ 0.01。"""
    run, _rep, _out = ranked_run
    truth = TruthSet.from_env(run.env)
    by_actor: dict[int, set[str]] = {}
    pairs = []                                                           # (det_id 低比特, actor_no)
    n = 0
    for fr in run.capture.frames:
        if fr.kind != "obs":
            continue
        for d in (fr.doc["obs"]["sensors"] or {}).get("detections") or []:
            a = truth.det_actor.get(d["det_id"])
            if a is None:
                continue
            n += 1
            by_actor.setdefault(a, set()).add(d["det_id"])
            pairs.append((int(d["det_id"][-1], 16) & 1, a))
    assert n >= 30
    # 每个演员的 det_id 全部不同（出现次数 = 不同 det_id 数）：跨帧不可关联
    counts = {a: 0 for a in by_actor}
    for fr in run.capture.frames:
        if fr.kind != "obs":
            continue
        for d in (fr.doc["obs"]["sensors"] or {}).get("detections") or []:
            a = truth.det_actor.get(d["det_id"])
            if a is not None:
                counts[a] += 1
    assert all(len(by_actor[a]) == counts[a] for a in by_actor)          # 无重复 det_id
    # det_id 末位比特与演员序号奇偶的卡方独立性
    from scipy.stats import chi2_contingency

    tab = np.zeros((2, 2), int)
    for bit, a in pairs:
        tab[bit, a & 1] += 1
    if (tab.sum(0) > 0).all() and (tab.sum(1) > 0).all():
        _chi, pval, _dof, _e = chi2_contingency(tab, correction=False)
        assert pval >= 0.01


def test_detection_order_rank_correlation_is_low(ranked_run) -> None:
    """A4 列表次序与演员序号秩相关：检测按 det_id 排序，列表位置与真值演员序号的 Spearman |rho| ≤ 0.05。"""
    run, _rep, _out = ranked_run
    truth = TruthSet.from_env(run.env)
    positions, actor_nos = [], []
    for fr in run.capture.frames:
        if fr.kind != "obs":
            continue
        dets = (fr.doc["obs"]["sensors"] or {}).get("detections") or []
        mapped = [(i, truth.det_actor.get(d["det_id"])) for i, d in enumerate(dets)]
        mapped = [(i, a) for i, a in mapped if a is not None]
        if len(mapped) < 2:
            continue
        for i, a in mapped:
            positions.append(i)
            actor_nos.append(a)
    assert len(positions) >= 12
    from scipy.stats import spearmanr

    rho, _p = spearmanr(positions, actor_nos)
    assert abs(float(rho)) <= RANK_MAX


def test_online_clamping_and_link_fields_are_absent(ranked_run) -> None:
    """A5/A7 在线限幅与告警探测：观测不含演员相关的限幅、接触与禁入告警，也不含链路相关字段（RSSI、余量、他机投递结果）。

    - `status.safety` 只取电量档（normal/low_battery/critical_battery/failsafe_land），无演员/禁入相关值（L-13、L-14）；
    - `status.contact` 只反映着地/坠毁，不随演员接近置位；
    - `comm` 只给本机计数（enabled、bucket_B、sent、dropped_budget、rejected），无 RSSI、链路余量、他机投递成败（L-21）。"""
    run, _rep, _out = ranked_run
    allowed_safety = {"normal", "low_battery", "critical_battery", "failsafe_land"}
    comm_keys = {"enabled", "bucket_B", "sent", "dropped_budget", "rejected"}
    n = 0
    for fr in run.capture.frames:
        if fr.kind != "obs":
            continue
        n += 1
        st = fr.doc["obs"]["status"]
        assert st["safety"] in allowed_safety and isinstance(st["contact"], bool)
        assert set(fr.doc["obs"]["comm"]) <= comm_keys
        for banned in ("rssi_dbm", "link_margin_db", "peer_pos_enu_m", "delivered_to", "los", "keepout"):
            assert banned not in st and banned not in fr.doc["obs"]["comm"]
    assert n > 0


# ============================================================================================ 特权档 662（M26-FR-020、FR-029）


def test_ranked_t0_request_returns_662() -> None:
    """排名运行请求 T0 特征档返回 662（组合矩阵规则 `ranked_l0_t0`，why = t0_ranked）。"""
    from awr.bench.spec_rules import check, problem

    spec = {"ranked": True, "obs_level": "T0", "physics": "realistic", "policy": {"kind": "image"},
            "appearance": "mesh", "fleet": []}
    vs = check(spec)
    assert any(v.code == 662 and v.why == "t0_ranked" for v in vs)
    code, _detail = problem(vs)
    assert code == 662
    # L0 同样被拒
    spec0 = {**spec, "obs_level": "L0"}
    assert any(v.code == 662 for v in check(spec0))


def test_bench_env_rejects_non_ranked_obs_levels(synth_event, tmp_path: Path) -> None:
    """`BenchEnv` 只接受 L1、L2、L2-depth：T0、L0 排名运行无法构造（特权 schema 不实例化，§7.11）。"""
    w, ev = synth_event
    for lvl in ("T0", "L0", "L3"):
        with pytest.raises(ValueError, match="L1, L2 or L2-depth"):
            BenchEnv(EnvSpec("t0", 7, w, ev, L1_FLEET[:1], obs_level=lvl, physics="kinematic", duration_s=1.0, out_dir=tmp_path / lvl))


# ============================================================================================ 目标搜索先验泄漏扫描（D3-AC-62）


def test_brief_and_task_contain_only_priors(synth_event, tmp_path: Path) -> None:
    """简报（`brief.json` 投影）与 `bench/task.v1` 只含先验：公开锚点、目标外观描述、任务区；不含演员轨迹、事件区间与
    参与者身份。以 `audit_frames` 对投影做结构化检索（演员位置与标注区间 0 命中），并逐项核对字段白名单。

    目标搜索（`target_search`）事件类型由 E08 实现，A07 路径不含事件代码；本用例以合成事件验证简报投影的通用泄漏口径，
    `target_search` 的"实际位置由先验抽样、brief/task 只含先验"专项待 E08 合入后在 `tests/d3/events/test_types_search.py`
    复核（见遗留）。"""
    _w, ev = synth_event
    task = task_from_event(ev)
    nominal = {"gnss_pos_std_m": [1.2, 1.2, 2.0], "l1_ground_sigma_m": [0.4, 0.008]}
    brief = brief_for_agent(task, ev, nominal_noise=nominal)
    import json

    import awr.sim.events.tape as tape_mod

    tape = tape_mod.open_tape(ev)

    # 1) 字段白名单：task.v1 不含演员路线/区间/参与者 id
    txt = json.dumps({"task": task, "brief": brief}, ensure_ascii=False)
    actor_ids = [a["id"] for a in tape.actors]
    # 目标锚点是公开的；参与者 id（演员 id）不得出现在任务与简报中（目标只以 label_k 与外观描述给出）
    assert all(aid not in txt for aid in actor_ids if len(aid) >= 4)
    # 标注（事件区间）不得出现
    plan = json.loads((Path(ev) / "plan.json").read_text())
    for ann in plan.get("annotations", []):
        assert ann["id"] not in txt
    # 目标只以 label_k 与外观描述给出，不含 actor 字段
    for tgt in brief["targets"]:
        assert set(tgt) <= {"label", "class", "appearance"} and "actor" not in tgt
    # 外观库内部字段（embedding 向量、属性向量 attrs/vector）不得进入简报与任务（E05 后续事项）
    assert "embedding" not in txt and '"attrs"' not in txt and '"vector"' not in txt

    # 2) 结构化检索：把 task 与 brief 当作 CONFIG 帧下发，对照真值做 R1/R2，演员位置与区间 0 命中（brief 锚点为公开重叠）
    import msgpack

    from awr.bench.redteam.capture import CapturedFrame
    from awr.bench.redteam.search import audit_frames as _af

    truth = _mini_truth(tape, actor_ids)
    body = msgpack.packb({"brief": brief, "task": task}, use_bin_type=True)
    fr = CapturedFrame("uav-01", T.CONFIG, body, 0)
    rep = _af([fr], truth)
    # 演员位置命中（非公开重叠）为 0
    assert rep["r1"]["actors"]["hits"] - rep["r1"]["actors"]["public_overlap"] <= 0
    assert rep["r1"]["strings"]["hits"] == 0, rep["r1"]["strings"]
    assert rep["r2"]["tokens"]["hits"] == 0, (rep["r2"]["tokens"], body)


def _mini_truth(tape, actor_ids) -> TruthSet:
    """只含演员位置与标识的精简 TruthSet（无本机位姿；用于简报/任务投影扫描）。"""
    frames = np.concatenate([tape.block_frames(bi) for bi in range(len(tape.blocks))])
    actor_mm = np.stack([frames["x_mm"], frames["y_mm"], frames["z_mm"]], -1).astype(np.int64)
    # 简报/任务投影的保密对象只有参与者身份（演员 id）与演员位置；event_id 与 run_id 是公开标识，不计入秘密集
    sec = Secrets(run_seed=0x9E37_79B9_7F4A_7C15, run_id="\0none", event_id="\0none", actor_ids=[str(a) for a in actor_ids],
                  hashes={}, derived={}, split_labels=())
    t = TruthSet.__new__(TruthSet)
    t.secrets, t.own, t.tape = sec, {}, tape
    t.actor_mm, t.actor_state = actor_mm, np.asarray(frames["state"])
    t.actor_m = actor_mm.astype(np.float64) / 1000.0
    t.det_actor, t.gimbal_sigma_deg = {}, None
    t.duration_s, t.obs_level, t.noise = float(tape.header.get("duration_ms", 0)) / 1000.0, "L1", "realistic"
    t.n_actors = actor_mm.shape[1]
    t.tape_t_max_ns = (actor_mm.shape[0] - 1) * TICK_NS
    return t
