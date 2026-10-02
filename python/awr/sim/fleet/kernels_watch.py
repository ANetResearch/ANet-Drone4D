"""numba cmd_watch 预筛核（M08-FR-057；ADR-060 大机群路径的融合实现，FX2-R2）。

`CommandEngine._done_np` 与 `_watch_rows_np` 对在途调用逐行求值的向量化判据，在一次遍历内完成（此前每个判据一次 numpy
花式下标与掩码运算，N = 1000 时 cmd_watch 约 2.3 ms/次，与 125 Hz 的 l1/tap/contact 同 tick 时单步超过 3 ms）：

- `done_scan`：goto、follow_path（距离、速度与模式判据）、有限圈 orbit（交回 + 圈数）、hover、takeoff 的完成判据与 `_hold` 计时
  （`hold_since` 原地更新）；持续 orbit（turns = 0）的半径判据用 `math.hypot`，与逐行实现须逐位一致，标记后留给调用方
  按原实现求值；
- `stall_scan`：暂停顺延截止、停滞 203（导航与 TRAJ 参考进度的滑动判据，参照点原地更新）与截止 202 的判定。

运算次序与 `command.py` 的逐行实现与 numpy 向量实现逐项相同（距离为 `sqrt(dx·dx + dy·dy + dz·dz)` 的同一求和次序，
`tests/sim/test_cmd_watch_vec.py` 对拍）。numba 只允许出现在 `awr/sim/fleet/kernels_*.py`。
"""

from __future__ import annotations

import math

import numpy as np

from .kernels_l1 import njit

__all__ = ["done_scan", "max_dev_update", "progress_pick", "stall_scan", "watch_triage"]

M_PATH, M_ORBIT, M_HOLD, M_TRAJ = 4, 5, 6, 15


@njit(cache=True, fastmath=False)
def done_scan(rows, slots, t, hold1, hold3, op_goto, op_fp, op_orbit, op_hover, live, paused, op, cm, vel, pos, goal, tol,
              aux, orb, o_turn, prov, hold_since, out_vn, out_done, out_vec, out_orb0, op_rtl, op_land, fs, fs_landed,
              fs_disarmed, z_rtl, seen_landed, op_takeoff, fs_flying, alt, ground_up):
    """返回需要调用方复核的行数（done 或非向量化命令）。out_vn：各行速度模（同 `np.linalg.norm(vel, axis=1)`）。
    rtl、land：`seen_landed` 原地更新（同 `_done_eval`），只把可能完成的行（降落类：已触地且已上锁；rtl 不降落：高度已到
    z_rtl ± 1 m）标为候选，由调用方按 `_done_eval`（含 `math.hypot` 的 home 距离）逐行复核。fs 与 rows 对齐。
    takeoff：`|z − (ground_up + alt)| < max(0.3, 0.05·alt)`、FLYING、|v_z| < 0.3 持续 1 s（同 `_done_eval`；此前逐行求值，
    N = 1000 批量起飞期间 cmd_watch 约 6 ms/次）。"""
    n = rows.shape[0]
    cnt = 0
    for k in range(n):
        r = rows[k]
        s = slots[k]
        v0 = vel[s, 0]
        v1 = vel[s, 1]
        v2 = vel[s, 2]
        vn = math.sqrt(v0 * v0 + v1 * v1 + v2 * v2)
        out_vn[k] = vn
        out_done[k] = False
        out_vec[k] = False
        out_orb0[k] = False
        ok = live[r] and not paused[r]
        if not ok:
            continue
        o = op[r]
        c = cm[s]
        hm = False
        hc = False
        hn = hold1
        if o == op_goto or o == op_fp:  # noqa: SIM109  （numba 核内保持标量比较）
            dx = pos[s, 0] - goal[r, 0]
            dy = pos[s, 1] - goal[r, 1]
            dz = pos[s, 2] - goal[r, 2]
            d = math.sqrt(dx * dx + dy * dy + dz * dz)
            slow = vn < 0.5
            if o == op_goto:
                hc = (d < tol[r]) and slow and (c != M_TRAJ)
            else:
                hc = (c != M_PATH) and (c != M_TRAJ) and (d < 0.5) and slow
            hm = True
            out_vec[k] = True
        elif o == op_orbit:
            turns = aux[r, 2]
            if turns > 0:
                back = (c != M_ORBIT) and (c != M_TRAJ)
                out_done[k] = back and (prov[r] or orb[s, o_turn] >= turns - 1e-6)
                out_vec[k] = True
            else:
                out_orb0[k] = True     # 持续环绕：半径判据由调用方以 math.hypot 求值（与逐行实现逐位一致）
                out_vec[k] = True
                hn = hold3
        elif o == op_hover:
            hc = vn < 0.3
            hm = True
            out_vec[k] = True
        elif o == op_takeoff:
            a = alt[r]
            z_t = ground_up[s] + a
            err = abs(pos[s, 2] - z_t)
            hc = fs[k] == fs_flying and err < max(0.3, 0.05 * a) and abs(v2) < 0.3
            hm = True
            out_vec[k] = True
        elif o == op_land or (o == op_rtl and aux[r, 0] == 0.0):  # rtl 的 aux[0] = 1 表示 `land: false`
            if fs[k] == fs_landed:
                seen_landed[r] = True
            out_done[k] = seen_landed[r] and fs[k] == fs_disarmed
            out_vec[k] = True
        elif o == op_rtl:
            out_done[k] = abs(pos[s, 2] - z_rtl[s]) < 1.0
            out_vec[k] = True
        if hm:
            hs = hold_since[r]
            if hc:
                if hs < 0:
                    hs = t
            else:
                hs = -1
            hold_since[r] = hs
            out_done[k] = hc and (t - hs >= hn)
        if out_done[k] or not out_vec[k]:
            cnt += 1
    return cnt


@njit(cache=True, fastmath=False)
def stall_scan(rows, slots, t, dt5, st_running, op_goto, op_fp, op_orbit, live, paused, status, op, prov, cm, pos, pos_ref,
               stall_ref, stall_t, stall_rref, stall_rmove, goal, tol, deadline, out_stall, out_late):
    """返回停滞或截止的行数。暂停行的截止顺延 dt5（ns）；参照点与计时原地更新（同 `_watch_rows_np`）。"""
    n = rows.shape[0]
    cnt = 0
    for k in range(n):
        r = rows[k]
        s = slots[k]
        out_stall[k] = False
        out_late[k] = False
        if not live[r]:
            continue
        if paused[r]:
            deadline[r] += dt5
            continue
        o = op[r]
        if status[r] == st_running and (o == op_goto or o == op_fp or o == op_orbit):  # noqa: SIM109  （numba 核内保持标量比较）
            c = cm[s]
            traj = prov[r] and c == M_TRAJ
            if traj:
                dx = pos_ref[s, 0] - stall_rref[r, 0]
                dy = pos_ref[s, 1] - stall_rref[r, 1]
                dz = pos_ref[s, 2] - stall_rref[r, 2]
                if math.sqrt(dx * dx + dy * dy + dz * dz) >= 0.2:
                    stall_rref[r, 0] = pos_ref[s, 0]
                    stall_rref[r, 1] = pos_ref[s, 1]
                    stall_rref[r, 2] = pos_ref[s, 2]
                    stall_rmove[r] = True
            dx = pos[s, 0] - stall_ref[r, 0]
            dy = pos[s, 1] - stall_ref[r, 1]
            dz = pos[s, 2] - stall_ref[r, 2]
            if math.sqrt(dx * dx + dy * dy + dz * dz) >= 0.2:
                stall_ref[r, 0] = pos[s, 0]
                stall_ref[r, 1] = pos[s, 1]
                stall_ref[r, 2] = pos[s, 2]
                stall_t[r] = t
                stall_rmove[r] = False
            elif t - stall_t[r] >= 5_000_000_000:
                if o == op_orbit:
                    far = True
                else:
                    gx = pos[s, 0] - goal[r, 0]
                    gy = pos[s, 1] - goal[r, 1]
                    gz = pos[s, 2] - goal[r, 2]
                    far = math.sqrt(gx * gx + gy * gy + gz * gz) > max(1.0, tol[r] * 2)
                fail = far and c != M_HOLD and ((not traj) or stall_rmove[r])
                if fail:
                    out_stall[k] = True
                else:
                    stall_ref[r, 0] = pos[s, 0]
                    stall_ref[r, 1] = pos[s, 1]
                    stall_ref[r, 2] = pos[s, 2]
                    stall_t[r] = t
                    stall_rmove[r] = False
        if not out_stall[k] and t > deadline[r]:
            out_late[k] = True
        if out_stall[k] or out_late[k]:
            cnt += 1
    return cnt


@njit(cache=True, fastmath=False)
def watch_triage(live, slot, applied, active, lifecycle, lost, fs_blk, status, fine_pending, fs_crashed, fs_eland,
                 fs_failsafe, st_accepted, st_running, out_rows, out_slots, out_fs, out_run):
    """cmd_watch 前段的一次遍历（FX2-R3）：按行号升序取在途且 slot 有效的行，写出行、slot、fs 与 running 标志；返回
    (行数, 需走原路径的行数)。需走原路径的行：未 apply、机体失联或坠毁、ELAND/FAILSAFE 升级、accepted 且细校验已结束（待
    读回 running）。全部为 0 时 `watch_tick` 的这些分支都不执行，结果与原实现相同。"""
    n = 0
    spec = 0
    for r in range(live.shape[0]):
        if not live[r]:
            continue
        s = slot[r]
        if s < 0:
            continue
        f = fs_blk[s]
        st = status[r]
        if (not applied[r]) or (not active[s]) or lifecycle[s] == lost or f == fs_crashed or f == fs_eland \
                or f == fs_failsafe or (st == st_accepted and not fine_pending[r]):  # noqa: SIM109  （numba 核内保持标量比较）
            spec += 1
        out_rows[n] = r
        out_slots[n] = s
        out_fs[n] = f
        out_run[n] = st == st_running
        n += 1
    return n, spec


@njit(cache=True, fastmath=False)
def progress_pick(rows, live, paused, batched, status, prog_wall, now_w, min_ns, st_running, max_n, out):
    """`CommandEngine._progress_pick` 的同一选择：running、非批量、未暂停、距上次 ≥ min_ns 的行中按 (prog_wall, 行号)
    取最小的至多 max_n 行（写入 out，返回行数）。"""
    k = 0
    bpw = np.empty(max(max_n, 1), np.int64)
    brow = np.empty(max(max_n, 1), np.int64)
    for i in range(rows.shape[0]):
        r = rows[i]
        if not live[r] or paused[r] or batched[r] or status[r] != st_running:
            continue
        pw = prog_wall[r]
        if now_w - pw < min_ns:
            continue
        if k < max_n:
            j = k
            k += 1
        else:
            if max_n == 0 or bpw[k - 1] < pw or (bpw[k - 1] == pw and brow[k - 1] < r):
                continue
            j = k - 1
        while j > 0 and (bpw[j - 1] > pw or (bpw[j - 1] == pw and brow[j - 1] > r)):
            bpw[j] = bpw[j - 1]
            brow[j] = brow[j - 1]
            j -= 1
        bpw[j] = pw
        brow[j] = r
    for i in range(k):
        out[i] = brow[i]
    return k


@njit(cache=True, fastmath=False)
def max_dev_update(rows, slots, pos_ref, pos, max_dev):
    """`max_dev[rows] = np.maximum(max_dev[rows], ‖pos_ref[slots] − pos[slots]‖)`（范数为 (dx² + dy²) + dz² 的同一求和次序，
    np.maximum 的 NaN 传播）。"""
    for i in range(rows.shape[0]):
        r = rows[i]
        s = slots[i]
        dx = pos_ref[s, 0] - pos[s, 0]
        dy = pos_ref[s, 1] - pos[s, 1]
        dz = pos_ref[s, 2] - pos[s, 2]
        d = math.sqrt(dx * dx + dy * dy + dz * dz)
        m = max_dev[r]
        if m != m:
            continue
        if d != d or d > m:
            max_dev[r] = d


def warmup() -> None:
    """以运行期类型（ENU 只读视图、调用表 SoA）调用一次两个核。"""
    n = 2
    rows = np.arange(n, dtype=np.int64)
    slots = np.arange(n, dtype=np.int64)
    ro = np.zeros((4, 3))
    ro.flags.writeable = False
    b = np.zeros(4, np.bool_)
    u8 = np.zeros(4, np.uint8)
    i64 = np.zeros(4, np.int64)
    f = np.zeros(4)
    f3 = np.zeros((4, 3))
    out_f = np.zeros(n)
    ob = np.zeros(n, np.bool_)
    done_scan(rows, slots, 0, 1, 1, 2, 3, 4, 5, b, b, u8, u8, ro, ro, f3, f, np.zeros((4, 4)), np.zeros((4, 12)), 6, b,
              i64.copy(), out_f, ob, ob.copy(), ob.copy(), 6, 1, np.zeros(n, np.uint8), 9, 3, f.copy(), b.copy(), 0, 5,
              f.copy(), f.copy())
    stall_scan(rows, slots, 0, 1, 1, 2, 3, 4, b, b, u8, u8, b, u8, ro, ro, f3.copy(), i64.copy(), f3.copy(), b.copy(),
               f3, f, i64.copy(), ob, ob.copy())
    watch_triage(b, np.zeros(4, np.int32), b, b, u8, 3, u8, u8, b, 9, 7, 8, 1, 2, np.zeros(4, np.int64), np.zeros(4, np.int64),
                 np.zeros(4, np.uint8), np.zeros(4, np.bool_))
    progress_pick(rows, b, b, b, u8, i64, 0, 1, 2, 4, np.zeros(4, np.int64))
    max_dev_update(rows, slots, ro, ro, f.copy())
