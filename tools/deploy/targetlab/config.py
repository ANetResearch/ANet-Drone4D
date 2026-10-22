"""从上游配置派生私有验收 profile；不修改共享配置，也不启动模型代理或开发服务器。"""

from pathlib import Path

import yaml

root = Path(__file__).resolve().parents[3]
doc = yaml.safe_load((root / "configs/runtime.yaml").read_text())
profile = dict(doc["profiles"]["sandbox"])
profile["procs_enable"] = ["core", "sandbox"]
doc["profiles"]["targetlab"] = profile
for proc in doc["procs"]:
    if proc["name"] == "sim-core":
        proc["liveness"]["startup_grace_s"] = 120
out = root / ".cache/targetlab/runtime.yaml"
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(yaml.safe_dump(doc, allow_unicode=True, sort_keys=False))
print(out)
