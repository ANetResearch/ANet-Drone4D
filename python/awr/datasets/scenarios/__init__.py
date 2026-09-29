"""剧本编写与离线复核（M16-FR-029；M16 §9.1、§9.2）。

- `authoring`：几何原语（ring、square、circle_zone）、ladder 布局、全部内置剧本、catalog 与 zones 的生成（逐字节确定）；
- `energy`：编写期离线能量复核（12 §5.8 风阻模型与 1-D 悬停功率模型；门禁以 M10 能量预检与 M09 估价为准）；
- `geometry`：航迹采样、两两最小间距、区域相交、ladder 各阶段间距构造值；
- `catalog`：剧本清单与剧本文件的静态校验（V-SC-13 与 M16 的附加规则）。

命令行：`python -m awr.datasets.scenarios {generate,pin,check,energy}`（`make scenarios-*`，mk/m16.mk）。
"""

from .authoring import build_all, dumps_canonical

__all__ = ["build_all", "dumps_canonical"]
