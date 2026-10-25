"""交汇与交接事件类型 `rendezvous_handoff`（M23 §15.3 第 2 行；M23-FR-042、058、071、074、077；AWR-05 §5.2.8；D3-AC-62 交汇与交接条款）。

门 1 必备配置（`@requirement`；`E-TYPE` 611，`detail.rule` 区分条目，一次报出全部）只对 P0b 形式的事件执行，即至少一名演员给出
`roles`；P0a 固定事件 `crop-handoff-001`（无 `roles`、无物体）保持 M23-AC-037 的 P0a 口径：

- `participant_count`：≥ 2 名 `roles` 含 `participant` 的人员；`participant_class`：参与者为 `person`；
- `object_carry`：≥ 1 件 `object` 类物体被某名人员以 `pickup` 拾取（P0b 起，§15.3）。

门 3 检测器 `object_handover`（§6.9.1，P0b；可声明为注释类型）：

```text
lab_o[k] = p   物体 o 状态 3 且 container 为人员 p（被 p 携带）
         = −1  状态 1、2（放在地面）
         = −2  其余（随携带者在车内、在建筑内、缺席或消失）
携带段 = lab = p 的连续段；同一 p 的两段之间只隔 −2 时合并（携带者上下车、进出建筑不中断携带）
交接 = 相邻携带段 (p1: …, b1] → [a2, …: p2)，p1 ≠ p2，其间只有 −1（或为空，即直接交接），且
       a2 − (b1 + 1) ≤ max_gap_s（10 s）、两人在 b1 与 a2 两帧均可见且水平距离 < dist_m（2 m）
区间 [t(b1 + 1), t(a2)]（直接交接时为瞬时 [t, t]）；参与者 (交出者, 接收者, 物体)；evidence {object, dist_m, gap_ms}；显著性 1
```

门 3 类型验证器（`@validator`），关键事件写入 `event_truth.json.key_events[]`（M23-FR-077），参数写入 `validator_params`
（进入 `truth_sha256`，§15.3 规则 1），report 映射写入 `report_map`（规则 4，AWR-05 §5.2.8 规则 5）：

```text
人员集合 P = roles 含 participant 的人员（P0b 形式），否则全部脚本人员
meet      两两可见且水平距离 ≤ meet.dist_m（2 m）的连续帧段，持续 ≥ meet.min_dur_s（5 s）；共享成员且时间重叠的段传递合并，
          区间为并集，参与者为成员并集（2–4 人交汇）；params {min_dist_m, pairs[[a, b, t0, t1]]}
handover  object_handover 真值事件中，交出者与接收者同属某次 meet 且交接区间含于该 meet 区间者；params {giver, receiver, object,
          meet, dist_m, gap_ms}；未落在任何 meet 内的交接只告警 W-HANDOVER-OUTSIDE
disperse  每次 meet 结束后 disperse.max_s（120 s）内的首帧，使其参与者两两"不同时在场（状态 1、2、3）或水平距离 ≥ sep_m（10 m）"；
          分散前其中 ≥ 2 人再次交汇时，该次 meet 不产出 disperse（分散归属其后的交汇）；
          瞬时；params {meet, sep_m, after_s}
board、alight、enter_building  门 3 检测器 board_vehicle、alight_vehicle、enter_building 中首个参与者属于 P 或 meet 成员者
关系边    handover during meet；meet before disperse；meet before 其成员此后的 board、alight、enter_building；
          handover causes 接收者此后首个 board 或 enter_building（携物离开）。全部按门 3 关系谓词成立（构造保证并复核）
```

与磁带的一致性（strict 613，lenient 告警）：

- `S-CARRY`：按 `dsl.carry_schedule` 由 plan 求出逐帧应有携带者，与 lab 逐帧比对（携带者不符、应携带而在地面、不应携带而在车内或
  建筑内、放下后位置不等于放下帧携带者位置或在地面期间移动）；
- `S-HANDOVER`：plan 中的每次交接（A 放下后 ≤ max_gap_s 内由 B ≠ A 拾取）都须产出区间相同的 `handover` 关键事件；
- `S-KEY-MEET`：P0b 形式的事件没有任何 meet（P0a 形式只告警 `W-NO-MEET`）。

`gate3.extra.rendezvous` 写入逐物体携带段、plan 交接与统计（编译报告）。
"""

from __future__ import annotations

import itertools

import numpy as np

from awr.sim.events.dsl import CARRY_PICKUP, Issue, carry_schedule
from awr.sim.events.verify.base import VISIBLE, Context, Detection, rnd, runs
from awr.sim.events.verify.engine import _rel_ok
from awr.sim.events.verify.registry import detector, requirement, type_issue, validator

__all__ = ["DEFAULTS", "EVENT_TYPE", "KEY_TYPES", "REPORT_MAP", "carry_labels", "holds", "object_handover", "planned_handovers",
           "requirements", "validate_rendezvous"]

EVENT_TYPE = "rendezvous_handoff"
DEFAULTS = {"meet": {"dist_m": 2.0, "min_dur_s": 5.0}, "disperse": {"sep_m": 10.0, "max_s": 120.0}}
HANDOVER_DEFAULTS = {"dist_m": 2.0, "max_gap_s": 10.0}
KEY_TYPES = {"board_vehicle": "board", "alight_vehicle": "alight", "enter_building": "enter_building"}
REPORT_MAP = {"meet": ["meet"], "handover": ["handover"], "disperse": ["disperse"], "board": ["board"], "alight": ["alight"],
              "enter_building": ["enter_building"], "relation": ["key_relations"]}
REST, AWAY = -1, -2
MAX_FAILS = 10
PRESENT = (1, 2, 3)


# ---------------------------------------------------------------------------------------------------- 门 1
@requirement(EVENT_TYPE)
def requirements(doc: dict, anchors: dict[str, dict]) -> list:
    """门 1 必备配置（M23-FR-071，§15.3 第 2 列）；无 `roles` 的 P0a 形式事件不检查（见模块说明）。"""
    acts = doc.get("actors", [])
    if not any(a.get("roles") for a in acts):
        return []
    part = [i for i, a in enumerate(acts) if "participant" in (a.get("roles") or ())]
    out = []
    n_person = sum(1 for i in part if acts[i]["class"] == "person")
    if n_person < 2:
        out.append(type_issue("/actors", f"交汇与交接事件须至少 2 名参与者人员（roles 含 participant），实为 {n_person} 名",
                              f"A rendezvous event requires at least 2 participant persons (roles containing participant), found {n_person}",
                              event_type=EVENT_TYPE, rule="participant_count", found=n_person, required=2,
                              actors=[acts[i]["id"] for i in part]))
    out.extend(type_issue(f"/actors/{i}/class", f"交汇与交接事件的参与者须为人员，{acts[i]['id']} 为 {acts[i]['class']}",
                          f"Participants of a rendezvous event must be persons, {acts[i]['id']} is a {acts[i]['class']}",
                          event_type=EVENT_TYPE, rule="participant_class", actor=acts[i]["id"]) for i in part if acts[i]["class"] != "person")
    cls_of = {a["id"]: a["class"] for a in acts}
    carried = sorted(oid for oid, steps in carry_schedule(acts).items()
                     if cls_of.get(oid) == "object" and any(s.kind == CARRY_PICKUP and cls_of.get(s.actor) == "person" for s in steps))
    if not carried:
        out.append(type_issue("/actors", "交汇与交接事件须至少 1 件物体由人员拾取（object 类演员与 pickup、drop 动作）",
                              "A rendezvous event requires at least one object picked up by a person (an object actor with pickup and drop)",
                              event_type=EVENT_TYPE, rule="object_carry", found=0, required=1))
    return out


# ---------------------------------------------------------------------------------------------------- 携带段与交接检测
def carry_labels(ctx: Context, o: int) -> np.ndarray:
    """物体 o 的逐帧携带标签（见模块说明）：人员 idx、`REST`（−1）或 `AWAY`（−2）。"""
    st = ctx.state[:, o]
    ct = ctx.container[:, o].astype(np.int64)
    person = np.array([c == "person" for c in ctx.cls[:ctx.n_scripted]] + [False], bool)
    held = (st == 3) & person[np.where(ct < ctx.n_scripted, ct, ctx.n_scripted)]
    return np.where(held, ct, np.where(np.isin(st, VISIBLE), REST, AWAY))


def holds(lab: np.ndarray) -> list[list[int]]:
    """携带段 [[人员 idx, 首帧, 末帧]]：同一人员的相邻段之间只隔 `AWAY` 时合并，隔 `REST`（被放下）则断开。"""
    out: list[list[int]] = []
    cut = np.flatnonzero(np.diff(lab)) + 1
    starts = np.concatenate([[0], cut]).astype(int)
    ends = np.concatenate([cut - 1, [len(lab) - 1]]).astype(int)
    gap_rest = False
    for a, b in zip(starts, ends, strict=True):
        v = int(lab[a])
        if v == REST:
            gap_rest = True
        elif v >= 0:
            if out and out[-1][0] == v and not gap_rest:
                out[-1][2] = int(b)
            else:
                out.append([v, int(a), int(b)])
            gap_rest = False
    return out


def _dist(ctx: Context, k: int, i: int, j: int) -> float:
    if not (ctx.vis[k, i] and ctx.vis[k, j]):
        return float("inf")
    return float(np.hypot(*(ctx.xy[k, i] - ctx.xy[k, j])))


@detector("object_handover", defaults=dict(HANDOVER_DEFAULTS), instant=True, phase="P0b")
def object_handover(ctx: Context, p: dict) -> list[Detection]:
    out = []
    lim, max_gap = float(p["dist_m"]), round(float(p["max_gap_s"]) * 1000)
    for o in ctx.scripted("object"):
        lab = carry_labels(ctx, o)
        hs = holds(lab)
        for (p1, _a1, b1), (p2, a2, _b2) in itertools.pairwise(hs):
            between = lab[b1 + 1:a2]
            if p1 == p2 or (len(between) and not (between == REST).all()):
                continue
            kr = b1 + 1
            gap = ctx.t_ms(a2) - ctx.t_ms(kr)
            d = max(_dist(ctx, b1, p1, p2), _dist(ctx, a2, p1, p2))
            if gap > max_gap or not d < lim:
                continue
            at = ctx.xy[b1, p1]
            out.append(Detection("object_handover", (ctx.ids[p1], ctx.ids[p2], ctx.ids[o]), (ctx.t_ms(kr), ctx.t_ms(a2)), 1.0,
                                 {"object": ctx.ids[o], "dist_m": rnd(d), "gap_ms": int(gap)}, (float(at[0]), float(at[1]))))
    return out


# ---------------------------------------------------------------------------------------------------- plan 侧携带与交接
def _expected(ctx: Context, oid: str, steps: list) -> tuple[np.ndarray, list[tuple[int, int]]]:
    """plan 的逐帧应有携带者（idx 或 −1）与放下 [(帧, 放下者 idx)]。"""
    exp = np.full(ctx.n_ticks, -1, np.int64)
    drops = []
    holder, k0 = -1, 0
    for s in steps:
        k = min(max(s.t_ms // ctx.tick_ms, 0), ctx.n_ticks)
        if s.kind == CARRY_PICKUP:
            holder, k0 = ctx.idx_of[s.actor], k
        elif holder >= 0:
            exp[k0:k] = holder
            drops.append((k, holder))
            holder = -1
    if holder >= 0:
        exp[k0:] = holder
    return exp, drops


def planned_handovers(plan: dict, max_gap_ms: int) -> list[dict]:
    """plan 中的交接：A 放下物体后 ≤ max_gap_ms 内由 B ≠ A 拾取。返回 [{giver, receiver, object, interval_ms}]（帧时刻）。"""
    out = []
    for oid, steps in carry_schedule(plan.get("actors", [])).items():
        last_drop = None
        for s in steps:
            if s.kind != CARRY_PICKUP:
                last_drop = s
                continue
            if last_drop is not None and last_drop.actor != s.actor and s.t_ms - last_drop.t_ms <= max_gap_ms:
                out.append({"giver": last_drop.actor, "receiver": s.actor, "object": oid, "interval_ms": [last_drop.t_ms, s.t_ms]})
            last_drop = None
    return out


def _carry_check(ctx: Context) -> tuple[dict, list[dict], list[dict]]:
    """`S-CARRY`：plan 携带日程与磁带逐帧一致（见模块说明）。返回 (统计, 最差违例, 逐物体携带段)。"""
    rows, chains, n_steps, n_obj = [], [], 0, 0
    f = ctx.frames
    for oid, steps in carry_schedule(ctx.plan.get("actors", [])).items():
        o = ctx.idx_of.get(oid)
        if o is None or o >= ctx.n_scripted or ctx.cls[o] != "object":
            continue
        n_obj += 1
        n_steps += len(steps)
        lab = carry_labels(ctx, o)
        exp, drops = _expected(ctx, oid, steps)
        chains += [{"object": oid, "holder": ctx.ids[h], "interval_ms": [ctx.t_ms(a), ctx.t_ms(b)]} for h, a, b in holds(lab)]
        st = ctx.state[:, o]
        bad = {}
        held = lab >= 0
        bad["holder_mismatch"] = held & (exp != lab)
        bad["not_carried"] = (lab == REST) & (exp >= 0)
        hv = np.where(exp >= 0, exp, 0)
        hs = ctx.state[np.arange(ctx.n_ticks), hv]
        hc = ctx.container[np.arange(ctx.n_ticks), hv]
        away = lab == AWAY
        bad["away_mismatch"] = away & (exp >= 0) & ((hs != st) | ((st == 3) & (hc != ctx.container[:, o])))
        bad["carried_unexpected"] = away & (exp < 0) & np.isin(st, (3, 4))
        rest_bad = np.zeros(ctx.n_ticks, bool)
        for k, h in drops:
            if k >= ctx.n_ticks:
                continue
            seg = np.flatnonzero(lab[k:] != REST)
            k1 = k + (int(seg[0]) if len(seg) else ctx.n_ticks - k)
            for fld in ("x_mm", "y_mm"):
                rest_bad[k:k1] |= f[fld][k:k1, o] != f[fld][k, h]
        bad["rest_position"] = rest_bad
        for why, m in bad.items():
            rows += [{"object": oid, "t_ms": ctx.t_ms(k), "why": why, "expected": None if exp[k] < 0 else ctx.ids[int(exp[k])],
                      "tape": int(lab[k]) if lab[k] < 0 else ctx.ids[int(lab[k])]} for k in np.flatnonzero(m)[:MAX_FAILS]]
    rows.sort(key=lambda r: (r["t_ms"], r["object"], r["why"]))
    return {"n_objects": n_obj, "n_steps": n_steps, "n_fail": len(rows)}, rows[:MAX_FAILS], chains


# ---------------------------------------------------------------------------------------------------- 门 3 类型验证器
def _merge(p: dict) -> dict:
    out = {k: dict(v) for k, v in DEFAULTS.items()}
    for k, v in (p or {}).items():
        if isinstance(v, dict) and k in out:
            out[k].update(v)
    return out


def _meets(ctx: Context, people: list[int], p: dict) -> list[dict]:
    lim, need = float(p["dist_m"]), round(float(p["min_dur_s"]) * 1000)
    segs = []
    for i, j in itertools.combinations(people, 2):
        d = np.hypot(*(ctx.xy[:, i] - ctx.xy[:, j]).T)
        m = ctx.vis[:, i] & ctx.vis[:, j] & (d <= lim)
        segs += [(ctx.ids[i], ctx.ids[j], ctx.t_ms(k0), ctx.t_ms(k1), float(d[k0:k1 + 1].min())) for k0, k1 in runs(m)
                 if ctx.t_ms(k1) - ctx.t_ms(k0) >= need]
    parent = list(range(len(segs)))

    def root(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b in itertools.combinations(range(len(segs)), 2):
        sa, sb = segs[a], segs[b]
        if {sa[0], sa[1]} & {sb[0], sb[1]} and min(sa[3], sb[3]) - max(sa[2], sb[2]) >= 0:
            parent[root(a)] = root(b)
    groups: dict[int, list] = {}
    for x, s in enumerate(segs):
        groups.setdefault(root(x), []).append(s)
    out = []
    for g in groups.values():
        g.sort(key=lambda s: (s[2], s[0], s[1]))
        out.append({"type": "meet", "participants": sorted({x for s in g for x in s[:2]}),
                    "interval_ms": [min(s[2] for s in g), max(s[3] for s in g)],
                    "params": {"min_dist_m": rnd(min(s[4] for s in g)), "pairs": [[s[0], s[1], s[2], s[3]] for s in g]}})
    return sorted(out, key=lambda m: (m["interval_ms"][0], m["participants"]))


def _disperse(ctx: Context, meet: dict, p: dict) -> dict | None:
    who = [ctx.idx_of[x] for x in meet["participants"]]
    k1 = meet["interval_ms"][1] // ctx.tick_ms
    k_end = min(ctx.n_ticks, k1 + 1 + round(float(p["max_s"]) * 1000) // ctx.tick_ms + 1)
    if k1 + 1 >= k_end:
        return None
    sl = slice(k1 + 1, k_end)
    ok = np.ones(k_end - k1 - 1, bool)
    for i, j in itertools.combinations(who, 2):
        both = np.isin(ctx.state[sl, i], PRESENT) & np.isin(ctx.state[sl, j], PRESENT)
        d = np.hypot(*(ctx.xy[sl, i] - ctx.xy[sl, j]).T)
        ok &= ~both | (d >= float(p["sep_m"]))
    hit = np.flatnonzero(ok)
    if not len(hit):
        return None
    t = ctx.t_ms(k1 + 1 + int(hit[0]))
    return {"type": "disperse", "participants": list(meet["participants"]), "interval_ms": [t, t],
            "params": {"sep_m": float(p["sep_m"]), "after_s": rnd((t - meet["interval_ms"][1]) / 1000.0)}}


def _issue(g, code: str, path: str, zh: str, en: str, **detail) -> None:
    if g.strict:
        g.errors.append(Issue(code, 613, path, zh, en, detail))
    else:
        g.warnings.append(Issue(code, None, path, zh, en, detail))


@validator(EVENT_TYPE)
def validate_rendezvous(ctx: Context, g) -> None:
    """门 3 类型验证器（见模块说明）：关键事件、关系边、携带一致性与交接核对。"""
    vp = _merge(ctx.params.get(EVENT_TYPE, {}))
    hp = {**HANDOVER_DEFAULTS, **g.detector_params.get("object_handover", {})}
    vp["handover"] = {"dist_m": float(hp["dist_m"]), "max_gap_s": float(hp["max_gap_s"])}
    typed = any(a.get("roles") for a in ctx.plan["actors"])
    role_p = {a["id"] for a in ctx.plan["actors"] if "participant" in (a.get("roles") or ()) and a["class"] == "person"}
    people = [i for i in ctx.scripted("person") if not typed or ctx.ids[i] in role_p]
    vp["participants"] = "roles" if typed else "persons"
    meets = _meets(ctx, people, vp["meet"])
    enc = [e for e in g.events if e["type"] == "proximity_encounter" and e["declared"]]
    for m in meets:
        m["declared"] = any(set(e["participants"]) <= set(m["participants"]) and min(e["interval_ms"][1], m["interval_ms"][1])
                            - max(e["interval_ms"][0], m["interval_ms"][0]) >= 0 for e in enc)
    keys: list[dict] = [*meets]
    # 交接：object_handover 真值事件 ∩ meet
    for e in g.events:
        if e["type"] != "object_handover":
            continue
        giver, receiver, obj = e["participants"]
        a, b = e["interval_ms"]
        host = next((m for m in meets if {giver, receiver} <= set(m["participants"]) and m["interval_ms"][0] <= a and b <= m["interval_ms"][1]),
                    None)
        if host is None:
            g.warnings.append(Issue("W-HANDOVER-OUTSIDE", None, "/", f"交接 {e['id']}（{giver} → {receiver}）不在任何交汇区间内，未列为关键事件",
                                    f"Handover {e['id']} ({giver} -> {receiver}) lies outside every meet and is not a key event",
                                    {"event": e["id"], "interval_ms": [a, b]}))
            continue
        keys.append({"type": "handover", "participants": [giver, receiver, obj], "interval_ms": [a, b], "ref": e["id"],
                     "declared": bool(e["declared"]), "_meet": id(host),
                     "params": {"giver": giver, "receiver": receiver, "object": obj, "dist_m": e["evidence"].get("dist_m"),
                                "gap_ms": e["evidence"].get("gap_ms")}})
    for j, m in enumerate(meets):
        d = _disperse(ctx, m, vp["disperse"])
        t_d = d["interval_ms"][0] if d is not None else None
        again = any(len(set(n["participants"]) & set(m["participants"])) >= 2 and n["interval_ms"][0] > m["interval_ms"][1]
                    and (t_d is None or n["interval_ms"][0] <= t_d) for n in meets[j + 1:])
        if d is not None and not again:                       # 分散前同一组人再次交汇：分散归属其后的交汇
            d["_meet"] = id(m)
            keys.append(d)
    members = role_p | {x for m in meets for x in m["participants"]}
    for e in g.events:
        if e["type"] in KEY_TYPES and e["participants"][0] in members:
            prm = {"vehicle": e["participants"][1]} if e["type"] != "enter_building" else {"door": e["evidence"].get("door")}
            keys.append({"type": KEY_TYPES[e["type"]], "participants": list(e["participants"]), "interval_ms": list(e["interval_ms"]),
                         "ref": e["id"], "declared": bool(e["declared"]), "params": prm})
    # 编号与关系
    keys.sort(key=lambda k: (k["interval_ms"][0], k["interval_ms"][1], k["type"], k["participants"]))
    for n, k in enumerate(keys, 1):
        k["id"] = f"k{n:02d}-{k['type']}"
    by_obj = {id(m): m for m in meets}
    rels: set[tuple[str, str, str]] = set()
    for k in keys:
        host = by_obj.get(k.pop("_meet", None))
        if host is None:
            continue
        k["params"]["meet"] = host["id"]
        rels.add((k["id"], "during", host["id"]) if k["type"] == "handover" else (host["id"], "before", k["id"]))
    for m in meets:
        rels |= {(m["id"], "before", k["id"]) for k in keys if k["type"] in ("board", "alight", "enter_building")
                 and k["participants"][0] in m["participants"] and k["interval_ms"][0] >= m["interval_ms"][1]}
    for h in (k for k in keys if k["type"] == "handover"):
        nxt = next((k for k in keys if k["type"] in ("board", "enter_building") and k["participants"][0] == h["params"]["receiver"]
                    and k["interval_ms"][0] >= h["interval_ms"][1]), None)
        if nxt is not None:
            rels.add((h["id"], "causes", nxt["id"]))
    iv = {k["id"]: tuple(k["interval_ms"]) for k in keys}
    g.key_relations = [{"src": a, "rel": r, "dst": b} for a, r, b in sorted(rels) if _rel_ok(r, iv[a], iv[b])]
    g.key_events = [{"id": k["id"], "type": k["type"], "participants": k["participants"], "interval_ms": k["interval_ms"],
                     "declared": bool(k.get("declared", False)), **({"ref": k["ref"]} if "ref" in k else {}), "params": k["params"]}
                    for k in keys]
    g.validator_params = vp
    g.report_map = {k: list(v) for k, v in REPORT_MAP.items()}
    # 与磁带和 plan 的一致性
    stat, fails, chains = _carry_check(ctx)
    if fails:
        _issue(g, "S-CARRY", "/actors", f"物体携带与磁带不一致：{stat['n_fail']} 处", f"Object carrying disagrees with the tape: {stat['n_fail']} cases",
               worst=fails, stat=stat)
    planned = planned_handovers(ctx.plan, round(vp["handover"]["max_gap_s"] * 1000))
    got = {(k["params"]["giver"], k["params"]["receiver"], k["params"]["object"], tuple(k["interval_ms"])) for k in g.key_events
           if k["type"] == "handover"}
    det = {(e["participants"][0], e["participants"][1], e["participants"][2], tuple(e["interval_ms"])) for e in g.events
           if e["type"] == "object_handover"}
    for ph in planned:
        key = (ph["giver"], ph["receiver"], ph["object"], tuple(ph["interval_ms"]))
        if key not in got:
            why = "outside_meet" if key in det else "not_detected"
            _issue(g, "S-HANDOVER", "/actors", f"计划中的交接 {ph['giver']} → {ph['receiver']}（{ph['object']}）未产出关键事件（{why}）",
                   f"Planned handover {ph['giver']} -> {ph['receiver']} ({ph['object']}) produced no key event ({why})", why=why, **ph)
    if not meets:
        if typed:
            _issue(g, "S-KEY-MEET", "/actors", "参与者之间没有交汇（≤ 2 m 持续 ≥ 5 s）", "No meet among the participants (<= 2 m for >= 5 s)",
                   participants=sorted(role_p), **vp["meet"])
        else:
            g.warnings.append(Issue("W-NO-MEET", None, "/actors", "人员之间没有交汇（≤ 2 m 持续 ≥ 5 s），关键事件不含 meet",
                                    "No meet among persons (<= 2 m for >= 5 s); the key events contain no meet", dict(vp["meet"])))
    g.extra["rendezvous"] = {"carry": chains, "carry_check": stat, "planned_handovers": planned,
                             "n_key_events": {t: sum(1 for k in g.key_events if k["type"] == t) for t in sorted({k["type"] for k in g.key_events})},
                             "n_key_relations": len(g.key_relations)}
