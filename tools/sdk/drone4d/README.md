# drone4d SDK

`drone4d` 是 Drone4D 基准的 Python SDK（M26-FR-080），不依赖仓库内的 `awr` 包。本版提供内部工作区的帧取回（P0a-1）：

```python
from drone4d.bench import BenchClient

with BenchClient("https://<bench host>", token="d3bt_...") as bc:
    run = bc.run("cap-crop-3uav-demo")
    for ch in run["frames"]:
        print(ch["uav_id"], ch["sensor_id"], ch["latest_seq"])
    f = bc.frame("cap-crop-3uav-demo", "uav-01", "cam0", 60)          # 交付字节（JPEG），sha256 已校验
    img = f.image()                                                  # numpy (H, W, 3) uint8，需要 extras "images"
    for f in bc.frames("cap-crop-3uav-demo", "uav-02", "cam0", 1, 300, stride=10):
        ...
    depth = bc.frame("cap-crop-3uav-demo", "uav-01", "cam0.depth", 60).depth()   # (H, W) float32，米
```

令牌只经 `Authorization: Bearer` 发送，不写入 URL。错误以 `BenchError(code, name, status, detail)` 抛出（原因码见 AWR-17 §18.12）。
Agent 接口（`DroneAgent`、`drone4d.bench.make`）随 P0a-3 加入。
