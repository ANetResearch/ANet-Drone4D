"""AWR（ANet Drone / World Runtime）唯一 Python 命名空间；入口一律 python -m awr.<pkg>（AWR-03 §4.1、ADR-050）。

所有者：M00（AWR-03 §4.3）。
"""

# 产品版本（ADR-118，V0.2-demo 为 0.2.0）的单一来源是 pyproject.toml 与 apps/web/package.json（tools/lint/check-version.mjs）。
# 本常量另被 World Package 生成器写入 generator.version（visual/pointcloud/metadata.json 进入 files[]，参与 contentVersion），
# 改动它会使重建的世界换 contentVersion 与派生缓存键（tests/world/test_determinism.py）；在 M03 把生成器版本与产品版本解耦之前
# 保持 0.1.0（请求见 .cache/impl/requests/d2-WP-16-to-M03.md）。
__version__ = "0.1.0"
