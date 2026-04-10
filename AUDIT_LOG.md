# PhotoSync 静态审计日志（不运行）

- 审计日期：2026-04-09
- 审计范围：`work_bulid/PhotoSync/`（Server + Web + Android）
- 审计方式：仅静态阅读/检索代码与配置；未启动服务、未执行同步。

## 分级说明

- **P0**：高风险/高概率导致核心功能错误或明显安全风险；建议优先修。
- **P1**：中高风险/会造成误导性状态、数据不一致、或较大运维成本。
- **P2**：中风险/边界场景问题、可观测性不足、隐性稳定性风险。
- **P3**：低风险/工程化与可维护性问题。

## 摘要（当前轮次）

- P0：2
- P1：2
- P2：3
- P3：2

---

## P0（必须优先）

### P0-1 Android 断开连接上报接口与服务端不一致，导致“断网仍显示已连接”

**现象/影响**
- Android 端尝试上报“断开连接”时调用了不存在的接口，服务端不会把 `connected` 置回 `false`。
- Web UI/PC 端会长期被旧状态误导（例如：手机关 Wi‑Fi 仍显示已连接）。
- 该问题也会放大“连接状态 stale（过期不回落）”问题。

**证据**
- Android 调用：`android/app/src/main/java/com/photosync/app/SyncClient.kt:146` 使用 `POST $baseUrl/api/phone/disconnect`
- 服务端接口：`server/main.py:1212` 仅提供 `POST /api/phone/unregister`
- 连接状态依赖：`server/main.py:2036` 的 `/api/wifi/status` 直接返回 `wifi_sync_status.connected`

**建议修复**
- 二选一（建议 A）：Android 端把 `/api/phone/disconnect` 改为 `/api/phone/unregister`。
- 或（建议 B）：服务端新增别名接口 `/api/phone/disconnect` 指向 `unregister` 逻辑，兼容旧客户端。

---

### P0-2 服务端默认监听 `0.0.0.0` 且缺少鉴权，局域网内任意人可改配置/上传文件（明显安全风险）

**现象/影响**
- 服务端监听所有网卡（`0.0.0.0`），且 API（含设置/上传）没有任何鉴权/来源限制。
- 在共享局域网环境下，任意同网段设备可以：
  - 修改端口、存储路径、TLS 开关等（影响可用性/可能导致数据落盘到意外位置）；
  - 调用 `/api/upload` 写入文件（可导致磁盘被占满；在某些配置下可能覆盖/污染同步目录）。

**证据**
- 监听地址：`server/main.py:2427`（`uvicorn_kwargs["host"] = "0.0.0.0"`）
- 设置接口（无鉴权）：
  - `server/main.py:1074` `POST /api/settings/port`
  - `server/main.py:1153` `POST /api/settings/storage`
  - `server/main.py:1118` `POST /api/settings/tls`
- 上传接口（无鉴权）：`server/main.py:2067` `POST /api/upload`

**建议修复**
- 最小化改动方案：
  - 默认只监听 `127.0.0.1`，并提供“显式开启局域网访问”的开关；或
  - 增加简单的共享密钥（token）校验（至少覆盖 settings/upload 相关接口）。

---

## P1（建议尽快）

### P1-1 “connected” 状态没有 TTL/心跳回落机制，容易产生过期状态

**现象/影响**
- `connected` 只在 `register/unregister` 时变更，若 App 崩溃/网络断开/请求丢失，服务端可能长期停留在 `connected=true`。
- Web UI 会持续显示已连接，干扰用户判断。

**证据**
- 状态结构未包含 `last_seen`：`server/main.py:674` 的 `wifi_sync_status` 无时间戳字段
- `/api/wifi/status` 仅对 `stopping` 做超时收敛：`server/main.py:2036`（无 `connected` 的 TTL 逻辑）

**建议修复**
- 在 `phone_register` 写 `last_seen_ts = now`；手机端定期心跳（或复用现有轮询接口时顺便刷新）。
- 在 `/api/wifi/status` 中：若 `now - last_seen_ts > N` 自动置 `connected=false`（N 可先设 10~30 秒）。

---

### P1-2 端口“自动回退并写回 config”会造成端口漂移，进而引发跨端口不一致问题

**现象/影响**
- 服务器启动时若端口占用，会自动寻找可用端口并写回 `config.json`。
- 这会导致“用户以为端口是 A，但实际跑在 B”，并进一步放大：Android/Web/ADB reverse 端口不一致带来的连接失败。

**证据**
- 自动写回：`server/main.py:2392` `config.data["server_port"] = run_port` + `config.save()`
- 日志提示：`server/main.py:2394` `print("[启动] 端口...已自动切换")`
- 当前本机配置端口：`server/config.json` 显示 `server_port = 9002`

**建议修复**
- 更保守的行为：仅提示冲突并退出，或仅在用户确认后写回配置。
- 或在 Web UI 显示“请求端口/实际监听端口”并强提醒（减少误解）。

---

## P2（中风险/建议排期）

### P2-1 Android 默认端口/回退逻辑分散，仍存在“隐式回到 8920”风险

**现象/影响**
- Android 侧默认端口仍为 8920，且多个调用路径可能在未显式注入端口时回退到 8920。
- 这类问题通常表现为：WiFi 端口改了但某些流程仍打旧端口。

**证据**
- 默认端口：`android/app/src/main/java/com/photosync/app/SyncClient.kt:52`（`var serverPort: Int = 8920`）
- Service extra 默认：`android/app/src/main/java/com/photosync/app/SyncService.kt:79`（`getIntExtra(..., 8920)`）

**建议修复**
- 把端口作为“强制必填参数”贯穿 UI → Service → Client，避免隐式默认。
- 或集中在一个 `Defaults`/`Config` 常量处，并减少散落默认值。

---

### P2-2 Web 端对 `server_port` 的兜底回退会掩盖真实端口漂移

**现象/影响**
- Web UI 在 `server_port` 缺失时回退 8920，会让 UI 展示与实际不一致（虽然目前 `/api/status` 一般会返回端口）。

**证据**
- `server/static/js/app.js:272`（`const currentPort = data.server_port || 8920;`）

**建议修复**
- 更安全的做法是：缺失时显示“未知/加载失败”，不要猜默认值。

---

### P2-3 `enforce_https_middleware` 可能受 Host 头影响导致重定向目标不可信（边界安全问题）

**现象/影响**
- 若部署在复杂代理/自定义 Host 的环境，重定向目标域名可能被 Host 头影响。

**证据**
- `server/main.py:1006`~`1018` 使用 `request.url.hostname` 参与构造 `https://{host}:{port}`

**建议修复**
- 固定使用 `config` 中的“展示域名/IP”（或从白名单选择），避免直接信任 Host。

---

### P2-4 文档（memo.md）与当前代码存在不一致，可能误导排障与回归判断

**现象/影响**
- `memo.md` 中记录的部分行为与当前代码静态检查结果不一致，可能导致“以为已经修过但实际上没有”的误判。
- 特别会影响连接状态/断开接口/心跳 TTL 等敏感问题的排查。

**证据**
- 代码静态检查显示：服务端提供 `POST /api/phone/unregister`（`server/main.py:1212`），Android 仍调用 `/api/phone/disconnect`（`SyncClient.kt:146`）。
- `memo.md` 的相关工作日志条目描述了相反方向的接口对齐与心跳超时策略，但在 `server/main.py` 当前实现中未看到对应字段/逻辑（如 `last_seen_ts`）。

**建议修复**
- 以代码为准，修复后同步更新 `memo.md` 的对应工作日志（追加更正说明，不改旧记录）。

---

## P3（工程化/可维护性）

### P3-1 服务器端日志主要靠 `print`，缺少分级与结构化日志

**现象/影响**
- 无法按级别过滤（info/warn/error），排障时噪音大；也不利于落盘与检索。

**证据**
- `server/main.py` 多处 `print(...)`（例如 `server/main.py:2394`、`server/main.py:638`）

**建议修复**
- 统一替换为 `logging`，并按模块/功能设置 logger；关键路径打印包含 request id/serial/album 等必要字段。

---

### P3-2 存在静默吞异常/忽略错误的点，影响可观测性

**现象/影响**
- 某些异常被 `pass` 吞掉，导致故障无法从日志反推（例如 ADB reverse remove 失败）。

**证据**
- `server/main.py:631` `except Exception: pass`（ADB reverse remove 失败被忽略）
- `server/main.py:2229` `except Exception: pass`（照片列表扫描时忽略异常）
- `android/app/src/main/java/com/photosync/app/SyncClient.kt:125`~`156` 多处 catch 后“忽略错误”

**建议修复**
- 至少记录 debug 日志（可通过配置开关控制），并在 UI 上给出可诊断的错误提示。
