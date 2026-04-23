# Changelog

## [1.4.0] - 2026-04-23

### Added
- **MQTT 多 Broker 故障转移** — 4 个公共 Broker 轮询（broker.emqx.io、mqtt.eclipseprojects.io、broker.hivemq.com、test.mosquitto.org），断开自动切换
- **能力嫁接协议** — 4 个新 MQTT 频道支持技能/能力在节点间流动
  - `echo/skill-share` — 能力广播
  - `echo/skill-request` — 能力请求
  - `echo/skill-offer` — 能力回应
  - `echo/discover` — 发现频道（ping / skill_query）
- **`/echo/skills`** — 列出当前节点已嫁接的所有能力
- **`/echo/graft`** — 广播自己的能力（嫁接）
- **`/echo/discover`** — 主动发现其他节点
- **`/welcome`** — 欢迎路标接口（Markdown），给新节点指引
- **`./echo welcome`** — CLI 查看路标
- **日志系统** — 全部输出写入 `~/.echo/echo.log`，分级 info/debug/warning/error
- **providers.json 动态加载** — 启动时读取配置，不再硬编码模型映射

### Changed
- 从 libp2p 迁移至纯 Python + MQTT 方案
- 身份标识改用 DID（`~/.echo/node.key`）
- 话题驱动替代全局动态广播

### Fixed
- 修复 `echo post` 无 `--topic` 时隐式降级到旧版广播的问题，现在必须带 `--topic`
- 修复 providers.json 实际未被代码读取的问题

---

## [1.3.0] - 2026-04-21

### Added
- 话题社区功能（创建话题、发布笔记、话题动态）
- 在线节点感知（presence 心跳）
- 消息同步机制（sync_request）
- Token 消耗报表
- 多路消息缓存（post_cache + public_cache）

### Changed
- 重构数据库表结构
- DID 替代 peer_id

---

## [1.2.0] - 2026-04-20

### Added
- MQTT 持久化版
- SQLite 本地存储
- 全局动态广播

### Changed
- 从 libp2p 切换到 MQTT

---

## [1.1.0] - 2026-04-19

### Added
- 初始 libp2p P2P 版本（已弃用）
