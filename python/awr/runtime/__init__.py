"""横切运行时库 awr.runtime（所有者 M11；AWR-03 §4.3；M11 §7.4、§9.1）。

模块（按需导入，本包不做重型导入；zenoh 只在 bus.py）：
- statering：StateRing（mmap seqlock，K = 32、8 游标、open_or_create、identity、header 一致性读）与 LocalRing
- bus：Bus 接口、ZenohBus（peer、只监听回环、SHM 关闭、DROP）、LocalBus
- events：EventPublisher（按步合批、seq、_replay）与 EventSubscriber（缺口补拉、重排、去重）
- heartbeat、child：主循环心跳文件；init_child 与 RunCtx
- supervisor、config、quota：进程监管、runtime.yaml 加载校验、runs/ 配额
- checkpoint、source、principal、statebus_zenoh：checkpoint 格式与存储、回放 Source 协议、分钥与 token、ZenohStateBus 桩
- cli：运维 CLI `awr`；logjson：JSON 行日志
"""
