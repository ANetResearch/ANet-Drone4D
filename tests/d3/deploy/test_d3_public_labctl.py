"""公开样例发布线 labctl.py（tools/deploy/emax/d3-public；D3-AC-21；AWR-05 §7.9 实现约束 1–3、AWR-17 §18.8.5、M27-FR-134、FR-138）。

覆盖：索引生成（SHA256SUMS、每条制品的 bytes 与 sha256、R04 切片清单并入、契约 schema 校验）；发布前校验（符号链接、> 64 MB、
test 集与真值文件、摘要不一致、前端会拒绝的 id、排行榜与提交特性、ModelScope 外链规则）；push（本机 rsync --link-dest，未变的
文件为硬链接）→ activate（原子切换 index.json 与 lab/current、保留 2 个版本、磁盘账 Q-D3-02 去重计数与超限拒绝）→ rollback；
站点安装（nginx -t 失败时恢复原文件）与撤下 /lab/（逐字节恢复 D2 站点文件）；pin 目标；命令行退出码。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from d3public_fixtures import (
    EVENT,
    MIB,
    RUN,
    blob,
    draft,
    lab_dist,
    load,
    published,
    slices_manifest,
    version_dir,
    write,
)

labctl = load("labctl")
labsite = load("labsite")
SCHEMA = Path(__file__).resolve().parents[3] / "packages" / "contracts" / "bench" / "lab_static_index.v1.schema.json"


def _validator():
    jsonschema = pytest.importorskip("jsonschema")
    return jsonschema.Draft202012Validator(json.loads(SCHEMA.read_text(encoding="utf-8")))


# ---------------------------------------------------------------------------------------------------------------------
# index 与契约


def test_index_fills_bytes_sha_and_validates_against_contract(tmp_path):
    v, dist = published(labctl, tmp_path, "2026.10.19-1")
    idx = json.loads((v / labctl.INDEX_IN_VERSION).read_text(encoding="utf-8"))
    _validator().validate(idx)
    sums = labctl.read_sums(v)
    assert set(sums) == {p.relative_to(v).as_posix() for p in v.rglob("*") if p.is_file() and p.name not in (labctl.SUMS, ".index.json")}
    run = idx["events"][0]["runs"][0]
    assert run["slices"] == [{"event_id": "encounter_ab", "t_from_s": 17.5, "t_to_s": 27.5, "rrd": f"runs/{RUN}/slices/encounter_ab.rrd",
                              "bytes": 512 * 1024, "sha256": sums[f"runs/{RUN}/slices/encounter_ab.rrd"]}]
    assert run["basemap"] == f"runs/{RUN}/slices/basemap.rrd" and run["basemap_bytes"] == 300 * 1024
    assert all(x["sha256"] == sums[x["path"]] and x["bytes"] == (v / x["path"]).stat().st_size for x in run["videos"])
    assert idx["events"][0]["bytes"] == 64 * 1024 and idx["events"][0]["sha256"] == sums[f"events/{EVENT}/tape.bin"]
    total = sum((v / r).stat().st_size for r in sums) + (v / labctl.SUMS).stat().st_size
    assert idx["files_bytes_total"] == total and idx["files_sha256"] == "SHA256SUMS"
    assert idx["features"] == {"inspect": True, "compare": False, "leaderboard": False, "submit": False}
    assert idx["lab_build"] == "20261019-060000-2a8f58d555" and idx["version"] == "2026.10.19-1"
    # 同一输入再生成一次：逐字节相同（built_unix_ns 固定时）
    before = (v / labctl.INDEX_IN_VERSION).read_bytes()
    d = draft()
    labctl.merge_slices(d, [slices_manifest()])
    labctl.build_index(v, d, lab_build="20261019-060000-2a8f58d555", built_unix_ns=1_793_000_000_000_000_000)
    assert (v / labctl.INDEX_IN_VERSION).read_bytes() == before
    rep = labctl.check_version_dir(v)
    assert rep["ok"], rep["problems"]
    assert labctl.check_lab_dist(dist)["ok"]


_PARSE_TS = """import { readFileSync } from 'node:fs'
import { parseStaticIndex, sampleRuns, sampleFiles } from './staticIndex.ts'
const idx = parseStaticIndex(JSON.parse(readFileSync(process.argv[2], 'utf8')))
const runs = sampleRuns(idx)
console.log(JSON.stringify({ version: idx.version, features: idx.features,
  files: runs.flatMap((r) => sampleFiles(r).map((f) => [f.kind, f.path, f.hosted])) }))
"""


def test_index_accepted_by_frontend_parser(tmp_path):
    """labctl 写出的索引能被前端解析器（apps/web/src/app/lab-public/staticIndex.ts parseStaticIndex）整份接受：两侧的 id、路径与特性
    规则不一致时前端会拒绝整份索引。以 node --experimental-strip-types 直接运行该源文件的副本（只把 flags 的构建期常量换成字面量）。"""
    import subprocess

    node = shutil.which("node")
    if node is None:
        pytest.skip("需要 node（≥ 22.6，--experimental-strip-types）")
    web = Path(__file__).resolve().parents[3] / "apps" / "web" / "src" / "app" / "lab-public"
    ts = tmp_path / "ts"
    ts.mkdir()
    (ts / "flags.ts").write_text((web / "flags.ts").read_text(encoding="utf-8")
                                 .replace("import.meta.env.VITE_AWR_LAB_ONLY", "'1'").replace("import.meta.env.VITE_AWR_LAB_PUBLIC", "'0'"),
                                 encoding="utf-8")
    (ts / "staticIndex.ts").write_text((web / "staticIndex.ts").read_text(encoding="utf-8").replace("from './flags'", "from './flags.ts'"),
                                       encoding="utf-8")
    (ts / "run.ts").write_text(_PARSE_TS, encoding="utf-8")
    v, _ = published(labctl, tmp_path / "src", "2026.10.19-1", big_mib=1)
    r = subprocess.run([node, "--experimental-strip-types", "--no-warnings", str(ts / "run.ts"), str(v / labctl.INDEX_IN_VERSION)],
                       capture_output=True, text=True, cwd=ts, timeout=60)
    if r.returncode != 0 and "experimental-strip-types" in r.stderr:
        pytest.skip(f"node {subprocess.run([node, '--version'], capture_output=True, text=True).stdout.strip()} 不支持 --experimental-strip-types")
    assert r.returncode == 0, r.stderr
    got = json.loads(r.stdout)
    assert got["version"] == "2026.10.19-1"
    assert got["features"] == {"inspect": True, "compare": False}          # 解析器只保留 inspect、compare（另两项须为 false）
    assert {(k, p) for k, p, hosted in got["files"] if hosted} == {
        ("video", f"runs/{RUN}/video/uav-01_cam0.mp4"), ("video", f"runs/{RUN}/video/uav-01_thermal0.mp4"),
        ("video", f"runs/{RUN}/video/uav-02_cam0.mp4"), ("slice", f"runs/{RUN}/slices/encounter_ab.rrd"),
        ("basemap", f"runs/{RUN}/slices/basemap.rrd")}


def test_index_rejects_leaderboard_missing_files_and_bad_version(tmp_path):
    v = version_dir(tmp_path, "2026.10.19-1")
    with pytest.raises(labctl.LabError, match="R-D3-46"):
        labctl.build_index(v, {**draft(), "features": {"leaderboard": True}})
    d = draft()
    d["events"][0]["runs"][0]["videos"].append({"uav": "uav-03", "cam": "cam0", "path": f"runs/{RUN}/video/nope.mp4"})
    with pytest.raises(labctl.LabError, match="版本目录中没有"):
        labctl.build_index(v, d)
    d = draft()
    d["events"][0]["tape"] = "../escape.bin"
    with pytest.raises(labctl.LabError, match="不合法"):
        labctl.build_index(v, d)
    bad = version_dir(tmp_path, "latest")
    with pytest.raises(labctl.LabError, match="版本名"):
        labctl.build_index(bad, draft())
    with pytest.raises(labctl.LabError, match="不在草稿"):
        labctl.merge_slices(draft(), [{**slices_manifest(), "run_id": "run-zzzzzzzzzz"}])


def test_external_modelscope_entry_for_oversize_artifact(tmp_path):
    """> 64 MB 的制品不放版本目录，只给 ModelScope 链接（ADR-201）；v1.6（ADR-221）任意大小的制品都可只经 ModelScope 分发，
    此时索引须给出 bytes 与 sha256；外链只接受 ModelScope。"""
    v = version_dir(tmp_path, "2026.10.19-1")
    d = draft()
    ext = {"uav": "uav-09", "cam": "cam0", "path": f"runs/{RUN}/video/uav-09_cam0.mp4", "bytes": 200 * MIB, "sha256": "a" * 64,
           "external": {"channel": "modelscope", "url": "https://modelscope.cn/datasets/anet/drone4d-d3-samples/file/view/master/x.mp4"}}
    d["events"][0]["runs"][0]["videos"].append(ext)
    idx = labctl.build_index(v, d)
    _validator().validate(idx)
    assert labctl.check_version_dir(v)["ok"]
    idx["events"][0]["runs"][0]["videos"][-1]["bytes"] = 10 * MIB                  # ADR-221：小文件也可只外链
    labctl.write_json_atomic(v / labctl.INDEX_IN_VERSION, idx)
    assert labctl.check_version_dir(v)["ok"]
    del idx["events"][0]["runs"][0]["videos"][-1]["sha256"]
    labctl.write_json_atomic(v / labctl.INDEX_IN_VERSION, idx)
    assert any("bytes 与 sha256" in p for p in labctl.check_version_dir(v)["problems"])
    idx["events"][0]["runs"][0]["videos"][-1].update(sha256="a" * 64)
    idx["events"][0]["runs"][0]["videos"][-1]["external"]["url"] = "https://huggingface.co/x"
    labctl.write_json_atomic(v / labctl.INDEX_IN_VERSION, idx)
    assert any("ModelScope" in p for p in labctl.check_version_dir(v)["problems"])


def test_external_basemap_and_viewer_wasm(tmp_path):
    """ADR-221：底图可只经 ModelScope 分发（basemap_external）；viewer_wasm 透传到索引并按 ModelScope 规则校验。"""
    v = version_dir(tmp_path, "2026.10.19-1")
    d = draft()
    r = d["events"][0]["runs"][0]
    r.update(basemap=f"runs/{RUN}/slices/far_basemap.rrd", basemap_bytes=5 * MIB, basemap_sha256="b" * 64,
             basemap_external={"channel": "modelscope", "url": "https://www.modelscope.cn/datasets/anet/s/resolve/master/basemap.rrd"})
    d["viewer_wasm"] = {"channel": "modelscope", "url": "https://www.modelscope.cn/datasets/anet/s/resolve/master/re_viewer_bg.wasm",
                        "bytes": 51_225_405, "sha256": "c" * 64}
    idx = labctl.build_index(v, d)
    _validator().validate(idx)
    assert idx["viewer_wasm"]["bytes"] == 51_225_405
    assert labctl.check_version_dir(v)["ok"]
    idx["viewer_wasm"]["url"] = "https://example.org/re_viewer_bg.wasm"
    labctl.write_json_atomic(v / labctl.INDEX_IN_VERSION, idx)
    assert any("viewer_wasm" in p for p in labctl.check_version_dir(v)["problems"])


# ---------------------------------------------------------------------------------------------------------------------
# check


@pytest.mark.parametrize(("mutate", "needle"), [
    (lambda v: os.symlink("/etc/passwd", v / "runs" / RUN / "leak.txt"), "符号链接"),
    (lambda v: write(v, f"runs/{RUN}/test/x.bin", b"x"), "test 集文件"),
    (lambda v: write(v, f"runs/{RUN}/extra.json", json.dumps({"split": "test"})), "test 集文件"),
    (lambda v: write(v, f"runs/{RUN}/truth/fleet.npz", b"x"), "真值文件"),
    (lambda v: write(v, f"runs/{RUN}/slices/eval.rrd", b"..../eval/hidden/uav-01...."), "真值文件"),
    (lambda v: write(v, f"runs/{RUN}/stray.bin", b"x"), "不在 SHA256SUMS"),
    (lambda v: (v / f"runs/{RUN}/slices/encounter_ab.rrd").write_bytes(blob(512 * 1024, "tampered")), "sha256"),
    (lambda v: (v / f"runs/{RUN}/tracks_obs.bin").unlink(), "列出但不存在"),
    (lambda v: write(v, "leaderboard/L2.json", "{}"), "leaderboard"),
    (lambda v: write(v, "runs/.hidden", b"x"), "不可直出"),
])
def test_check_version_dir_findings(tmp_path, mutate, needle):
    v, _ = published(labctl, tmp_path, "2026.10.19-1")
    assert labctl.check_version_dir(v)["ok"]
    mutate(v)
    rep = labctl.check_version_dir(v)
    assert not rep["ok"] and any(needle in p for p in rep["problems"]), rep["problems"]


def test_check_oversize_and_frontend_id_rules(tmp_path):
    v, _ = published(labctl, tmp_path, "2026.10.19-1", video_mib=2.0)
    rep = labctl.check_version_dir(v, max_file=1 * MIB)
    assert any("> 64 MB" in p and "uav-01_cam0.mp4" in p for p in rep["problems"])
    idx = json.loads((v / labctl.INDEX_IN_VERSION).read_text(encoding="utf-8"))
    idx["events"][0]["runs"][0]["run_id"] = "cap-crop-handoff-001"          # sensorsvc 采集运行的 id：前端会拒绝整份索引
    idx["events"][0]["id"] = "Crop_001"
    idx["features"]["submit"] = True
    labctl.write_json_atomic(v / labctl.INDEX_IN_VERSION, idx)
    probs = labctl.check_version_dir(v)["problems"]
    assert any("run_id" in p for p in probs) and any(".id" in p for p in probs) and any("R-D3-46" in p for p in probs)


def test_check_lab_dist_rejects_forbidden_strings_and_symlinks(tmp_path):
    d = lab_dist(tmp_path)
    assert labctl.check_lab_dist(d)["ok"]
    write(d, "assets/x.js", "fetch('/api/bench/v1/runs')")
    rep = labctl.check_lab_dist(d)
    assert not rep["ok"] and any("禁用字符串" in p for p in rep["problems"])
    (d / "assets" / "x.js").unlink()
    os.symlink("/etc", d / "etc")
    assert any("符号链接" in p for p in labctl.check_lab_dist(d)["problems"])


# ---------------------------------------------------------------------------------------------------------------------
# push → activate → rollback


def _push_activate(src: Path, root: Path, version: str, release: str, **kw) -> dict:
    v, dist = published(labctl, src, version, release=release, **kw)
    labctl.push(v, dist, str(root))
    return labctl.activate(labctl.Layout(root), version)


def test_push_activate_keep_two_and_rollback(tmp_path):
    src, root = tmp_path / "src", tmp_path / "srv"
    lay = labctl.Layout(root)
    r1 = _push_activate(src, root, "2026.10.19-1", "20261019-060000-aaaaaaaaaa")
    assert r1["version"] == "2026.10.19-1" and lay.current_build() == "20261019-060000-aaaaaaaaaa"
    assert json.loads(lay.index.read_text(encoding="utf-8"))["version"] == "2026.10.19-1"
    assert (root / "bin" / "labctl.py").is_file() and (root / "bin" / "scan_public.py").is_file()
    assert not any((root / "lab-static" / ".staging").iterdir())
    # 第 2 版只改切片：其余文件经 --link-dest 成为硬链接，磁盘账只计一次
    _push_activate(src, root, "2026.10.20-1", "20261019-060000-aaaaaaaaaa", slice_seed="s2")
    a = root / "lab-static" / "v" / "2026.10.19-1" / "runs" / RUN / "video" / "uav-01_cam0.mp4"
    b = root / "lab-static" / "v" / "2026.10.20-1" / "runs" / RUN / "video" / "uav-01_cam0.mp4"
    assert a.stat().st_ino == b.stat().st_ino
    s1 = root / "lab-static" / "v" / "2026.10.19-1" / "runs" / RUN / "slices" / "encounter_ab.rrd"
    s2 = root / "lab-static" / "v" / "2026.10.20-1" / "runs" / RUN / "slices" / "encounter_ab.rrd"
    assert s1.stat().st_ino != s2.stat().st_ino
    bud = labctl.budget_report(lay)
    naive = sum(p.stat().st_size for p in (root / "lab-static" / "v").rglob("*") if p.is_file())
    assert bud["ok"] and bud["bytes_total"] < naive
    # 第 3 版换构建：只保留 2 个版本与它们的构建
    r3 = _push_activate(src, root, "2026.10.21-1", "20261021-060000-bbbbbbbbbb")
    assert lay.listed_versions() == ["2026.10.20-1", "2026.10.21-1"]
    assert "lab-static/v/2026.10.19-1" in r3["pruned"]
    assert lay.listed_builds() == ["20261019-060000-aaaaaaaaaa", "20261021-060000-bbbbbbbbbb"]
    st = lay.state()
    assert st["lab_static"] == {"current": "2026.10.21-1", "previous": "2026.10.20-1"}
    assert st["lab"] == {"current": "20261021-060000-bbbbbbbbbb", "previous": "20261019-060000-aaaaaaaaaa"}
    # 回滚：上一版本的索引与它发布时的构建
    rb = labctl.rollback(lay)
    assert rb["to"] == {"version": "2026.10.20-1", "build": "20261019-060000-aaaaaaaaaa"}
    assert json.loads(lay.index.read_text(encoding="utf-8"))["version"] == "2026.10.20-1"
    assert lay.current_build() == "20261019-060000-aaaaaaaaaa" and rb["seconds"] < 10
    # 再前滚到 2026.10.21-1（已有版本目录可重新激活，不可覆盖）
    labctl.activate(lay, "2026.10.21-1")
    assert json.loads(lay.index.read_text(encoding="utf-8"))["version"] == "2026.10.21-1"
    hist = [json.loads(x) for x in lay.history.read_text(encoding="utf-8").splitlines()]
    assert [h["op"] for h in hist] == ["activate", "activate", "activate", "rollback", "activate"]
    assert all(h["t"].endswith("Z") for h in hist)
    # 版本目录只增不改
    v, dist = published(labctl, tmp_path / "again", "2026.10.21-1", release="20261021-060000-bbbbbbbbbb")
    labctl.push(v, dist, str(root))
    with pytest.raises(labctl.LabError, match="不可变"):
        labctl.activate(lay, "2026.10.21-1")


def test_activate_refuses_over_budget_and_bad_staging(tmp_path):
    src, root = tmp_path / "src", tmp_path / "srv"
    lay = labctl.Layout(root)
    v, dist = published(labctl, src, "2026.10.19-1", release="20261019-060000-aaaaaaaaaa", video_mib=2.0)
    labctl.push(v, dist, str(root))
    with pytest.raises(labctl.LabError, match="Q-D3-02"):
        labctl.activate(lay, "2026.10.19-1", budget=1 * MIB)
    assert not lay.index.exists() and lay.current_build() is None              # 拒绝时不切换
    (root / "lab-static" / ".staging" / "2026.10.19-1" / "runs" / RUN / "tracks_obs.bin").write_bytes(b"tampered")
    with pytest.raises(labctl.LabError, match="校验不通过"):
        labctl.activate(lay, "2026.10.19-1")
    with pytest.raises(labctl.LabError, match="先 push"):
        labctl.activate(lay, "2026.10.30-1")
    with pytest.raises(labctl.LabError, match="没有可回滚"):
        labctl.rollback(lay)


def test_push_refuses_unclean_inputs(tmp_path):
    v, dist = published(labctl, tmp_path, "2026.10.19-1")
    write(v, f"runs/{RUN}/hidden/a.bin", b"x")
    with pytest.raises(labctl.LabError, match="不上传"):
        labctl.push(v, dist, str(tmp_path / "srv"))
    assert not (tmp_path / "srv" / "lab-static").exists()
    shutil.rmtree(v / "runs" / RUN / "hidden")
    calls = []

    def fake(cmd):
        calls.append(cmd)

        class R:
            returncode, stdout, stderr = 0, "2026.10.18-1\n::\n20261018-000000-cccccccccc\n", ""
        return R()
    out = labctl.push(v, dist, "emax:/srv/drone4d", runner=fake, dry_run=True)
    assert out["link_dest"] == {"version": "2026.10.18-1", "build": "20261018-000000-cccccccccc"}
    rs = [c for c in calls if c[0] == "rsync"]
    assert "--link-dest=../../v/2026.10.18-1/" in rs[0] and rs[0][-1] == "emax:/srv/drone4d/lab-static/.staging/2026.10.19-1/"
    assert rs[1][-1] == "emax:/srv/drone4d/lab/.staging/20261019-060000-2a8f58d555/" and "--dry-run" in rs[0]


# ---------------------------------------------------------------------------------------------------------------------
# 站点：enable-site 与 withdraw


def test_enable_site_and_withdraw_restore_d2_bytes(tmp_path):
    lay = labctl.Layout(tmp_path / "srv")
    site = tmp_path / "sites-available" / "drone4d.agentnetwork.org.cn.conf"
    site.parent.mkdir()
    d2 = labsite.D2_SITE.read_text(encoding="utf-8")
    site.write_text(d2, encoding="utf-8")
    r = labctl.enable_site(lay, site, nginx_test="true", nginx_reload="true")
    assert site.read_text(encoding="utf-8") == labsite.render(d2) and lay.d2_site.read_text(encoding="utf-8") == d2
    assert lay.state()["site"] == "lab" and r["seconds"] < 10
    # 再次 enable：以保存的 D2 文件为 base，结果不变（不重复插入）
    labctl.enable_site(lay, site, nginx_test="true", nginx_reload="true")
    assert site.read_text(encoding="utf-8") == labsite.render(d2)
    # nginx -t 失败：恢复原文件、不 reload
    marker = tmp_path / "reloaded"
    with pytest.raises(labctl.LabError, match="nginx -t"):
        labctl.withdraw(lay, site, nginx_test="false", nginx_reload=f"touch {marker}")
    assert site.read_text(encoding="utf-8") == labsite.render(d2) and not marker.exists()
    w = labctl.withdraw(lay, site, nginx_test="true", nginx_reload=f"touch {marker}")
    assert site.read_text(encoding="utf-8") == d2 and marker.exists() and lay.state()["site"] == "d2"
    assert w["sha256"] == labsite.sha256_text(d2)
    # 没有保存的 D2 文件时，从"D2 + /lab/"站点文件剥离增补段
    lay2 = labctl.Layout(tmp_path / "srv2")
    site.write_text(labsite.render(d2), encoding="utf-8")
    labctl.withdraw(lay2, site, nginx_test="true", nginx_reload="true")
    assert site.read_text(encoding="utf-8") == d2


def test_withdraw_with_d2_release_runs_install_rollback(tmp_path):
    lay = labctl.Layout(tmp_path / "srv")
    site = tmp_path / "site.conf"
    d2 = labsite.D2_SITE.read_text(encoding="utf-8")
    site.write_text(labsite.render(d2), encoding="utf-8")
    sh = write(tmp_path, "install.sh", 'echo "rollback $*" > "$(dirname "$0")/called"\n')
    out = labctl.withdraw(lay, site, nginx_test="true", nginx_reload="true", d2_release="20261013-131027-f294f06b5a", install_sh=sh)
    assert (tmp_path / "called").read_text().strip() == "rollback --rollback 20261013-131027-f294f06b5a"
    assert out["d2_release"]["exit"] == 0


# ---------------------------------------------------------------------------------------------------------------------
# pin 与命令行


def test_pin_targets_and_cli(tmp_path, capsys):
    v, dist = published(labctl, tmp_path, "2026.10.19-1")
    idx = json.loads((v / labctl.INDEX_IN_VERSION).read_text(encoding="utf-8"))
    assert labctl.pin_targets(idx) == [(RUN, "slices"), (RUN, "video")]
    assert labctl.main(["check", "--dir", str(v), "--lab-dist", str(dist)]) == 0
    assert "通过 version" in capsys.readouterr().out
    write(v, "runs/test/x.bin", b"x")
    assert labctl.main(["--json", "check", "--dir", str(v)]) == 1
    assert json.loads(capsys.readouterr().out)["ok"] is False
    root = tmp_path / "srv"
    assert labctl.main(["--root", str(root), "activate", "--version", "2026.10.30-1"]) == 1
    assert labctl.main(["--root", str(root), "--json", "budget"]) == 0
    assert json.loads(capsys.readouterr().out)["bytes_total"] == 0
    with pytest.raises(SystemExit) as e:
        labctl.main(["nope"])
    assert e.value.code == 2
