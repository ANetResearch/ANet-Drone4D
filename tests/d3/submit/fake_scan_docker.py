"""测试用 docker 替身（提交镜像扫描，D3-AC-59）：状态目录取环境变量 FAKE_SCAN_STATE，镜像表 `<state>/images.json`
`{ref: {Os, Architecture, Size, report?, fail?}}`（`report` 为 Trivy JSON 报告，`fail` 为前 n 次扫描以非 0 退出）。

- `image inspect REF --format …`：REF 在镜像表中时输出 inspect JSON（`--format {{.Id}}` 时只输出 Id），否则退出 1；
- `save -o PATH REF`：写入归档（内容为 REF），REF 缺失时退出 1；
- `run … -v SRC:/in/image.tar:ro … IMAGE image --input …`：读取归档得到 REF，输出其 `report`；
- `rm -f NAME`：无操作。每次调用的参数追加到 `<state>/calls.jsonl` 供断言。"""

import json
import os
import sys
from pathlib import Path

st = Path(os.environ["FAKE_SCAN_STATE"])
st.mkdir(parents=True, exist_ok=True)
a = sys.argv[1:]
with open(st / "calls.jsonl", "a", encoding="utf-8") as f:
    f.write(json.dumps(a) + "\n")
images = json.loads((st / "images.json").read_text()) if (st / "images.json").exists() else {}

if a[:2] == ["image", "inspect"]:
    ref = a[2]
    if ref not in images:
        print(f"Error: No such image: {ref}", file=sys.stderr)
        sys.exit(1)
    info = {k: v for k, v in images[ref].items() if k not in ("report", "fail")}
    print(ref if "{{.Id}}" in a else json.dumps({"Id": ref, **info}))
elif a[0] == "save":
    out, ref = a[a.index("-o") + 1], a[-1]
    if ref not in images:
        sys.exit(1)
    Path(out).write_text(ref)
elif a[0] == "run":
    src = next(v.split(":")[0] for v in a if v.endswith(":/in/image.tar:ro"))
    ref = Path(src).read_text()
    img = images[ref]
    n = int((st / f"fails-{ref[-8:]}").read_text()) if (st / f"fails-{ref[-8:]}").exists() else 0
    if n < img.get("fail", 0):
        (st / f"fails-{ref[-8:]}").write_text(str(n + 1))
        print("FATAL scan error", file=sys.stderr)
        sys.exit(1)
    print(json.dumps(img.get("report", {"SchemaVersion": 2, "Trivy": {"Version": "0.75.0"}, "Results": []})))
elif a[0] == "rm":
    pass
else:
    sys.exit(2)
