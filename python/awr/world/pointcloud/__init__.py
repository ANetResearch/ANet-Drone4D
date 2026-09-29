"""点云源格式与 ANET_Q16 八叉树切片器（AWR-16 §4、§6.1；M03 §6.7、§6.8）。

所有者：M03（AWR-03 §4.3）。公共接口签名由 AWR-16 §4.12 冻结：`tile`、`read_container`、`iter_nodes`、
`oct16_encode`、`oct16_decode`、`forest_split`、`build_octree`。
"""

from .encode import baked_height, oct16_decode, oct16_encode
from .octree import build_octree, forest_split, morton_sort, world_cube
from .reader import Container, NodeView, iter_nodes, parse_hierarchy, read_container
from .tiler import tile
from .writer import RootEntry, rule_g_level, write_container

__all__ = [
    "Container",
    "NodeView",
    "RootEntry",
    "baked_height",
    "build_octree",
    "forest_split",
    "iter_nodes",
    "morton_sort",
    "oct16_decode",
    "oct16_encode",
    "parse_hierarchy",
    "read_container",
    "rule_g_level",
    "tile",
    "world_cube",
    "write_container",
]
