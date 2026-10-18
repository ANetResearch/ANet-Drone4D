#!/usr/bin/env python3
"""tools/bench/d3/refdet/train.py：训练参考检测器 `ref_det_v1`（M26-FR-121）。在 dmax 的 pytorch 镜像（GPU 2 或 3）中运行。

网络（CenterNet 式，输出步长 4）：输入为 Agent 侧同一预处理的 960×540 RGB（`drone4d.perception.ref_det.decode_small`：JPEG DCT 域
1/2 缩放解码，底部补 4 行 0 到 544），`x/255 − 0.5`；stem 4×4/4 分块卷积（32，步长 4，跳连）→ 深度可分离块 b2 32→48/2（步长 8）→ b3 48→48 →
b4（膨胀 2）→ b5（膨胀 4）→ b6（残差）→ 最近邻 ×2 上采样与 stem 拼接（80）→ 1×1 80→32 → 1×1 32→6：
两类中心热图（person、vehicle）、框尺寸 log(h、w，输入像素）、中心亚像素偏移。

数据：gen_data.py 的分片；首次运行把全部帧解码缩放为 uint8 memmap（`<data>/cache-960x540.u8`）。段号 % 10 == 0 的段为验证集。
导出：BN 折叠进卷积，权重存 `ref_det_v1.npz`（float32），与 numpy 推理（drone4d.perception.ref_det）在验证帧上逐元素比对（≤ 1e-3）。

用法：python train.py --data out/refdet/data --out out/refdet/model [--epochs 40] [--batch 32]
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "sdk" / "drone4d" / "src"))

W_IN, H_IN, H_PAD, STRIDE = 960, 540, 544, 4
HM_W, HM_H = W_IN // STRIDE, H_PAD // STRIDE
ARCH = [("stem", 3, 32, 4, 1), ("b2", 32, 48, 2, 1), ("b3", 48, 48, 1, 1), ("b4", 48, 48, 1, 2), ("b5", 48, 48, 1, 4),
        ("b6", 48, 48, 1, 1)]


def _load_frame(args) -> np.ndarray:
    from drone4d.perception.ref_det import decode_small

    path, off, ln = args
    with open(path, "rb") as f:
        f.seek(off)
        b = f.read(ln)
    return decode_small(b)[0]


def build_cache(data: Path) -> tuple[np.memmap, list[dict]]:
    rows = []
    for idx in sorted(data.glob("shard-*.jsonl")):
        b = idx.with_suffix(".bin")
        for ln in idx.read_text(encoding="utf-8").splitlines():
            r = json.loads(ln)
            r["bin"] = str(b)
            rows.append(r)
    cache = data / f"cache-{W_IN}x{H_IN}.u8"
    mm_shape = (len(rows), H_IN, W_IN, 3)
    if not cache.exists() or cache.stat().st_size != int(np.prod(mm_shape)):
        mm = np.memmap(cache, dtype=np.uint8, mode="w+", shape=mm_shape)
        t0 = time.perf_counter()
        with ProcessPoolExecutor(16) as ex:
            for i, a in enumerate(ex.map(_load_frame, [(r["bin"], r["off"], r["len"]) for r in rows], chunksize=32)):
                mm[i] = a
        mm.flush()
        del mm
        print(f"[cache] {len(rows)} frames in {time.perf_counter() - t0:.0f} s", flush=True)
    return np.memmap(cache, dtype=np.uint8, mode="r", shape=mm_shape), rows


def targets(boxes: list, flip: bool):
    """→ hm (2, HM_H, HM_W)、ign (HM_H, HM_W)、centers [(c, iy, ix, log h, log w, ox, oy)]（输入像素尺度，全分辨率 / 2）。"""
    hm = np.zeros((2, HM_H, HM_W), np.float32)
    ign = np.zeros((HM_H, HM_W), np.float32)
    cen = []
    yy, xx = np.mgrid[0:HM_H, 0:HM_W]
    for c, x, y, w, h, ig in boxes:
        x, y, w, h = x / 2.0, y / 2.0, max(w / 2.0, 1.0), max(h / 2.0, 1.0)
        if flip:
            x = W_IN - x - w
        if ig:
            x0, y0 = int(max(0, x // STRIDE)), int(max(0, y // STRIDE))
            x1, y1 = int(min(HM_W - 1, (x + w) // STRIDE)), int(min(HM_H - 1, (y + h) // STRIDE))
            ign[y0:y1 + 1, x0:x1 + 1] = 1.0
            continue
        cx, cy = (x + w / 2) / STRIDE, (y + h / 2) / STRIDE
        ix, iy = int(min(HM_W - 1, cx)), int(min(HM_H - 1, cy))
        sig = max(0.6, 0.15 * math.hypot(w, h) / STRIDE)
        g = np.exp(-((xx - ix) ** 2 + (yy - iy) ** 2) / (2 * sig * sig))
        hm[c] = np.maximum(hm[c], g)
        cen.append((c, iy, ix, math.log(h), math.log(w), cx - ix, cy - iy))
    return hm, ign, cen


class FrameSet:
    """训练样本：memmap 帧 + 水平翻转与亮度增益（按 (seed, epoch, 帧号) 取随机数，与工作进程数无关）。"""

    def __init__(self, cache: Path, shape, rows, idx, seed: int) -> None:
        self.cache, self.shape, self.rows, self.idx, self.seed, self.epoch = cache, shape, rows, list(idx), seed, 0
        self.mm = None

    def __len__(self) -> int:
        return len(self.idx)

    def __getitem__(self, i: int):
        if self.mm is None:
            self.mm = np.memmap(self.cache, dtype=np.uint8, mode="r", shape=self.shape)
        fi = self.idx[i]
        rng = np.random.default_rng([self.seed, self.epoch, fi])
        flip = bool(rng.random() < 0.5)
        a = self.mm[fi]
        if flip:
            a = a[:, ::-1]
        x = np.zeros((3, H_PAD, W_IN), np.float32)
        x[:, :H_IN] = np.clip(a.transpose(2, 0, 1) / 255.0 * rng.uniform(0.8, 1.2), 0.0, 1.0) - 0.5
        hm, ign, cen = targets(self.rows[fi]["boxes"], flip)
        c = np.zeros((64, 7), np.float32)
        c[:min(64, len(cen))] = np.asarray(cen[:64], np.float32).reshape(-1, 7)
        return x, hm, ign, c, min(64, len(cen))


def make_model():
    import torch
    from torch import nn

    class DS(nn.Module):
        def __init__(self, ci, co, s, d):
            super().__init__()
            self.dw = nn.Conv2d(ci, ci, 3, s, padding=d, dilation=d, groups=ci, bias=False)
            self.bn1 = nn.BatchNorm2d(ci)
            self.pw = nn.Conv2d(ci, co, 1, bias=False)
            self.bn2 = nn.BatchNorm2d(co)
            self.res = ci == co and s == 1

        def forward(self, x):
            y = torch.relu(self.bn1(self.dw(x)))
            y = torch.relu(self.bn2(self.pw(y)))
            return x + y if self.res else y

    class Net(nn.Module):
        def __init__(self):
            super().__init__()
            self.stem = nn.Conv2d(3, 32, 4, 4, bias=False)
            self.bn0 = nn.BatchNorm2d(32)
            self.blocks = nn.ModuleList([DS(ci, co, s, d) for _n, ci, co, s, d in ARCH[1:]])
            self.h1 = nn.Conv2d(48 + 32, 32, 1)
            self.h2 = nn.Conv2d(32, 6, 1)
            nn.init.constant_(self.h2.bias, 0.0)
            with torch.no_grad():
                self.h2.bias[:2] = -4.0

        def forward(self, x):
            f4 = torch.relu(self.bn0(self.stem(x)))
            x = f4
            for b in self.blocks:
                x = b(x)
            up = torch.nn.functional.interpolate(x, scale_factor=2, mode="nearest")
            return self.h2(torch.relu(self.h1(torch.cat([up, f4], 1))))

    return Net()


def export(net, path: Path) -> dict:
    """BN 折叠：conv(x)·g/s + (b − m·g/s)，s = sqrt(var + eps)。"""
    import torch

    def fold(conv, bn):
        w = conv.weight.detach().double()
        s = (bn.weight / torch.sqrt(bn.running_var + bn.eps)).detach().double()
        b = (bn.bias - bn.running_mean * bn.weight / torch.sqrt(bn.running_var + bn.eps)).detach().double()
        return (w * s.view(-1, *([1] * (w.dim() - 1)))).float().cpu().numpy(), b.float().cpu().numpy()

    out = {}
    out["stem.w"], out["stem.b"] = fold(net.stem, net.bn0)
    for (name, *_), blk in zip(ARCH[1:], net.blocks, strict=True):
        out[f"{name}.dw.w"], out[f"{name}.dw.b"] = fold(blk.dw, blk.bn1)
        out[f"{name}.pw.w"], out[f"{name}.pw.b"] = fold(blk.pw, blk.bn2)
    for h in ("h1", "h2"):
        m = getattr(net, h)
        out[f"{h}.w"], out[f"{h}.b"] = m.weight.detach().float().cpu().numpy(), m.bias.detach().float().cpu().numpy()
    np.savez(path, **out)
    return out


def focal(pred, gt, ign):
    import torch

    p = torch.sigmoid(pred).clamp(1e-4, 1 - 1e-4)
    pos = gt.eq(1).float()
    neg = (1 - pos) * (1 - ign.unsqueeze(1))
    pl = -torch.log(p) * (1 - p) ** 2 * pos
    nl = -torch.log(1 - p) * p ** 2 * (1 - gt) ** 4 * neg
    n = pos.sum().clamp(min=1)
    return (pl.sum() + nl.sum()) / n


def decode(out: np.ndarray, thr: float = 0.3, k: int = 64) -> list[tuple[int, float, float, float, float, float]]:
    """(6, HM_H, HM_W) logits → [(cls, score, x, y, w, h)]（输入像素）；3×3 极大值抑制。"""
    hm = 1.0 / (1.0 + np.exp(-out[:2]))
    pad = np.pad(hm, ((0, 0), (1, 1), (1, 1)), constant_values=0.0)
    mx = np.max(np.stack([pad[:, dy:dy + HM_H, dx:dx + HM_W] for dy in range(3) for dx in range(3)]), axis=0)
    keep = (hm >= mx) & (hm >= thr)
    c, iy, ix = np.nonzero(keep)
    sc = hm[c, iy, ix]
    order = np.argsort(-sc, kind="stable")[:k]
    res = []
    for j in order:
        cj, y, x = int(c[j]), int(iy[j]), int(ix[j])
        h, w = math.exp(float(out[2, y, x])), math.exp(float(out[3, y, x]))
        cx, cy = (x + float(out[4, y, x])) * STRIDE, (y + float(out[5, y, x])) * STRIDE
        res.append((cj, float(sc[j]), cx - w / 2, cy - h / 2, w, h))
    return res


def _iou(a, b) -> float:
    iw = max(0.0, min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0]))
    ih = max(0.0, min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1]))
    u = a[2] * a[3] + b[2] * b[3] - iw * ih
    return iw * ih / u if u > 0 else 0.0


def evaluate(net, mm, rows, idx, dev, thr: float = 0.3) -> dict:
    import torch

    net.eval()
    tp = fp = fn = 0
    per = {0: [0, 0, 0], 1: [0, 0, 0]}
    with torch.no_grad():
        for i in range(0, len(idx), 32):
            ii = idx[i:i + 32]
            x = _batch_x(mm, ii, [False] * len(ii)).to(dev)
            out = net(x).float().cpu().numpy()
            for j, fi in enumerate(ii):
                dets = decode(out[j], thr)
                gts = [(c, x / 2, y / 2, w / 2, h / 2) for c, x, y, w, h, ig in rows[fi]["boxes"] if not ig]
                igs = [(x / 2, y / 2, w / 2, h / 2) for c, x, y, w, h, ig in rows[fi]["boxes"] if ig]
                used = set()
                for c, _s, x0, y0, w, h in dets:
                    best, bi = 0.3, None
                    for g, gt in enumerate(gts):
                        if g in used or gt[0] != c:
                            continue
                        v = _iou((x0, y0, w, h), gt[1:])
                        if v >= best:
                            best, bi = v, g
                    if bi is not None:
                        used.add(bi)
                        tp += 1
                        per[c][0] += 1
                    elif not any(_iou((x0, y0, w, h), g) > 0.3 for g in igs):
                        fp += 1
                        per[c][1] += 1
                fn += len(gts) - len(used)
                for g, gt in enumerate(gts):
                    if g not in used:
                        per[gt[0]][2] += 1
    net.train()
    pr = tp / max(tp + fp, 1)
    rc = tp / max(tp + fn, 1)
    return {"precision": round(pr, 4), "recall": round(rc, 4), "f1": round(2 * pr * rc / max(pr + rc, 1e-9), 4),
            "per_class": {n: {"tp": per[c][0], "fp": per[c][1], "fn": per[c][2]} for c, n in enumerate(("person", "vehicle"))}}


def _batch_x(mm, ii, flips):
    import torch

    x = np.zeros((len(ii), 3, H_PAD, W_IN), np.float32)
    for j, (fi, fl) in enumerate(zip(ii, flips, strict=True)):
        a = mm[fi]
        if fl:
            a = a[:, ::-1]
        x[j, :, :H_IN] = a.transpose(2, 0, 1) / 255.0 - 0.5
    return torch.from_numpy(x)


def main(argv: list[str] | None = None) -> int:
    import torch

    ap = argparse.ArgumentParser(prog="train.py")
    ap.add_argument("--data", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--epochs", type=int, default=40)
    ap.add_argument("--batch", type=int, default=32)
    ap.add_argument("--lr", type=float, default=2e-3)
    ap.add_argument("--seed", type=int, default=1)
    a = ap.parse_args(argv)
    a.out.mkdir(parents=True, exist_ok=True)
    torch.manual_seed(a.seed)
    mm, rows = build_cache(a.data)
    val = [i for i, r in enumerate(rows) if r["seg"] % 10 == 0]
    tr = [i for i, r in enumerate(rows) if r["seg"] % 10 != 0]
    dev = torch.device("cuda")
    net = make_model().to(dev)
    opt = torch.optim.AdamW(net.parameters(), lr=a.lr, weight_decay=1e-4)
    steps = a.epochs * (len(tr) // a.batch)
    sched = torch.optim.lr_scheduler.OneCycleLR(opt, max_lr=a.lr, total_steps=steps, pct_start=0.1)
    log = []
    t0 = time.perf_counter()
    ds = FrameSet(a.data / f"cache-{W_IN}x{H_IN}.u8", mm.shape, rows, tr, a.seed)
    for ep in range(a.epochs):
        ds.epoch = ep
        g = torch.Generator()
        g.manual_seed(a.seed * 1000 + ep)
        dl = torch.utils.data.DataLoader(ds, batch_size=a.batch, shuffle=True, drop_last=True, num_workers=12, generator=g, persistent_workers=False)
        tot, nb = 0.0, 0
        for x, hm, ign, cen, ncen in dl:
            x, hm, ign = x.to(dev, non_blocking=True), hm.to(dev, non_blocking=True), ign.to(dev, non_blocking=True)
            out = net(x)
            l_hm = focal(out[:, :2], hm, ign)
            m = torch.arange(64)[None, :] < ncen[:, None]
            bi = torch.nonzero(m)[:, 0]
            c = cen[m]
            if len(bi):
                yi, xi = c[:, 1].long(), c[:, 2].long()
                pr = out[bi.to(dev), 2:, yi.to(dev), xi.to(dev)]
                tg = c[:, 3:].to(dev)
                l_reg = torch.nn.functional.l1_loss(pr[:, :2], tg[:, :2]) + torch.nn.functional.l1_loss(pr[:, 2:], tg[:, 2:])
            else:
                l_reg = out.sum() * 0.0
            loss = l_hm + l_reg
            opt.zero_grad()
            loss.backward()
            opt.step()
            sched.step()
            tot += float(loss)
            nb += 1
        ev = evaluate(net, mm, rows, val, dev) if (ep + 1) % 5 == 0 or ep == a.epochs - 1 else None
        log.append({"epoch": ep + 1, "loss": round(tot / max(1, nb), 4), "val": ev, "t_s": round(time.perf_counter() - t0)})
        print(json.dumps(log[-1]), flush=True)
    net.eval().cpu()
    export(net, a.out / "ref_det_v1.npz")
    torch.save(net.state_dict(), a.out / "ref_det_v1.pt")
    # numpy 推理与 torch 比对（验证帧前 8 帧）
    from drone4d.perception.ref_det import RefDet

    rd = RefDet(a.out / "ref_det_v1.npz")
    x = _batch_x(mm, val[:8], [False] * 8)
    with torch.no_grad():
        ref = net(x).numpy()
    err = max(float(np.abs(rd.forward(mm[fi]) - ref[j]).max()) for j, fi in enumerate(val[:8]))
    final = evaluate(net.to(dev), mm, rows, val, dev)
    card = {"schema": "drone4d.refdet.model.v1", "model": "ref_det_v1", "input": [W_IN, H_IN], "stride": STRIDE, "classes": ["person", "vehicle"],
            "arch": ARCH, "train_frames": len(tr), "val_frames": len(val), "epochs": a.epochs, "val": final, "numpy_max_abs_err": err,
            "data_meta": json.loads((a.data / "meta.json").read_text(encoding="utf-8")), "log": log}
    (a.out / "model_card.json").write_text(json.dumps(card, indent=1) + "\n", encoding="utf-8")
    print(json.dumps({"val": final, "numpy_max_abs_err": err}), flush=True)
    return 0 if err <= 1e-3 else 1


if __name__ == "__main__":
    sys.exit(main())
