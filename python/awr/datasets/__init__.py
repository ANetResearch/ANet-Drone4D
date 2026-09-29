"""演示数据、剧本编写与演示前检查（M16，AWR-03 §4.3；M16 §9.1）。

- `awr.datasets.urbanscene3d`：六城事实常量（引用 16 §10）与航线文件解析桩（V0.2）；
- `awr.datasets.scenarios`：剧本与 curated zones 的编写工具、离线能量与几何复核、剧本清单校验；
- `awr.datasets.demo`：演示前检查（`python -m awr.datasets.demo check`）与演示提示卡。

不含 ingest 适配器（M03）与剧本加载器（M10）。
"""
