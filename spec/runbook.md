# Recall 启动与运维手册（runbook）

> **用途**：每次开机后照本页依次启动**五个常驻窗口**；出问题时按 §4 排查。
> **决定**：项目工程师 2026-09-30 明确**不做开机自启** ⇒ 保持手工启动，本页就是操作步骤。
> **关联**：`spec/tech.md` §11/§12（拓扑与目录）、`spec/roadmap.md` R-38/R-39、
> `docs/R-39-public-access.md`（公网接入与验收判据）。

## 0. 一句话顺序

```
Qdrant  →  recall.api  →  （recall.watchdog、cloudflared、recall.feishu_bot 可并行）
  ↑            ↑
必须最先起   依赖 Qdrant
```

- **watcher 必须在 API 起来之后再起**（它启动时会调一次 `POST /kb/ingest` 补同步）。
- **cloudflared 只依赖 API**（它把公网流量转发到 `127.0.0.1:8000`），不依赖 Qdrant。
- **feishu_bot 只依赖 API**（每次提问都调 `POST /kb/answer`）⇒ 必须**在 API 之后**起，
  否则用户在飞书里只会收到"服务暂时不可用"的降级卡片。
- 五个常驻窗口都要**保持开着**（Ctrl+C 就是停服务）。

---

## 1. 五个窗口的启动命令

### 窗口 ①　Qdrant（向量库）

```bat
cd /d D:\Project\Recall\tools\qdrant
set QDRANT__SERVICE__HOST=127.0.0.1
qdrant.exe
```

- ⚠️ **必须加 `set QDRANT__SERVICE__HOST=127.0.0.1`**：Qdrant 默认绑 `0.0.0.0` 且**它自己没有 API key**
  ⇒ 等于把整库（可读、可删、可写）交给同网段。⚠️ **手动启动的老方式（不带这行）会让它重新暴露**，
  详见 §7。
- ⚠️ **必须在这个目录下启动**：`storage\` / `snapshots\` 是按**工作目录**找的。换个目录启动会开出一个
  **空库**（磁盘上的真实数据还在，但 `/kb/stats` 会显示 0 点，看着像"数据丢了"）。
- 验收（另开一个终端）：
  ```bat
  curl.exe -s http://127.0.0.1:6333/healthz
  curl.exe -s http://127.0.0.1:6333/
  ```
  期望：`healthz check passed`；版本 `"version":"1.19.1"`（**不能是 1.19.0**）。
- **推荐用脚本**（自动设好上面的环境变量 + 隐藏窗口 + 轮询健康检查，通过才报成功）：
  ```bat
  cd /d D:\Project\Recall
  powershell -NoProfile -ExecutionPolicy Bypass -File tools\start-qdrant.ps1
  ```
  加 `-Visible` 可看到它的日志窗口。⚠️ `.ps1` 必须保持 **UTF-8 with BOM**（否则 PowerShell 5.1 按 ANSI 读、语法报错）。
- ⚠️ **脚本是"分离 + 隐藏"启动**：它用 `Start-Process` 另起一个进程，然后自己轮询健康检查就**退出**了
  ⇒ 所以**命令提示符会立刻还给你**（这**不是**没启动成功！），同时**你没有 Qdrant 的日志窗口**。
  - 想**看日志**：`-Visible`（有窗口）或干脆手动 `qdrant.exe`（那种方式会**占住**窗口、不还提示符）。
  - 想**确认它到底在不在跑**（推荐养成习惯，见 §7 的启动竞争）：
    ```bat
    Get-Process qdrant
    Get-NetTCPConnection -LocalPort 6333 -State Listen
    curl.exe -s http://127.0.0.1:6333/healthz
    ```
- 🔴 **同一时刻只能有一个 Qdrant 进程**（一个 storage 目录只能被一个进程打开）。重复启动的**典型报错**是
  启动 panic：`Wal error: Can't init WAL: Kind(WouldBlock)` —— 见 §4 排查表末行。
- ℹ️ 启动时那几条 `Config file not found: config/config` / `config/development`、
  `Filesystem type check is not supported on this platform` 都是**正常噪音**，可忽略。

### 窗口 ②　Recall API（REST + `/mcp`）

```bat
cd /d D:\Project\Recall
python -m recall.api
```

- 前提 ①：Qdrant 已在跑。
- 前提 ②：**conda 环境已激活** —— 提示符应为 `(recall)`；没有就先 `conda activate recall`
  （环境路径 `D:\miniconda\envs\recall`）。
- 它**只绑回环** `127.0.0.1:8000`（刻意的安全选择，`tech.md` §7.1）—— **不要**改成 `0.0.0.0`，
  公网入口由隧道负责。
- 验收：
  ```bat
  curl.exe -s http://127.0.0.1:8000/health
  curl.exe -s -H "X-API-Key: <本机 token>" http://127.0.0.1:8000/kb/stats
  ```
  期望：`/health` 返回 `{"status":"ok",...}`；`/kb/stats` 带 key 返回 200（无 key 应 **401**）。
- ⚠️ **首次检索会装载 bge-m3 + bge-reranker（冷启动约 16~25 秒）**。想避免"第一个提问的人卡住"，
  起完先预热一次：
  ```bat
  curl.exe -s -o NUL -H "X-API-Key: <本机 token>" -H "Content-Type: application/json" -d "{\"query\":\"混合检索\",\"top_k\":1}" http://127.0.0.1:8000/kb/search
  ```
  热了之后单次检索约 **5~7 秒**。

### 窗口 ③　watcher（笔记改动自动增量同步）

```bat
cd /d D:\Project\Recall
python -m recall.watchdog
```

- ⚠️ **必须在 API 起来之后再起**。启动时它先补同步一次；API 没起就会重试 3 次后放弃
  （日志 `watchdog.ingest_unreachable` ×3 + `ingest_giving_up`），**只影响那一次补跑**，
  之后观察到的文件变更照常触发。
- 顺序反了的补救（API 起来后执行一次即可）：
  ```bat
  python -m recall.watchdog --once
  ```
- 它用 `.env` 里的 `RECALL_WATCHDOG_API_KEY`（已配；启用鉴权后**回环不豁免**，没这个 key 会一直 401）。
- 验收：`data\logs\watchdog.log` 出现 `watchdog.started`；改一篇笔记后应看到
  `change_detected` → `ingest_ok`，且 `/kb/stats` 的点数/篇数随之变化。
- 排错用：`python -m recall.watchdog --once`（同步一次即退出）。

### 窗口 ④　cloudflared（公网隧道）

```bat
"C:\Program Files (x86)\cloudflared\cloudflared.exe" tunnel run recall
```

- 前提：`iamzyx.xyz` 的 NS 已交给 Cloudflare、`recall` 隧道已创建、`~\.cloudflared\config.yml` 就位
  （三者均已完成，见 `docs/R-39-public-access.md` §5.1）。
- 验收：
  ```bat
  "C:\Program Files (x86)\cloudflared\cloudflared.exe" tunnel info recall
  curl.exe -s -o NUL -w "%{http_code}\n" https://recall.iamzyx.xyz/health
  ```
  期望：`CONNECTIONS` 列有值（连上边缘）；`/health` 返回 **200**。
- 公网入口：`https://recall.iamzyx.xyz/mcp/`（**给 Coze 用，末尾 `/` 不能少**）、
  `https://recall.iamzyx.xyz/kb/search`（REST，需 `X-API-Key`）。
- ⚠️ **临时隧道不要日常使用**：`cloudflared tunnel --url http://127.0.0.1:8000` 生成的
  `*.trycloudflare.com` 地址**每次重启都会变**，只适合压测验证。
- 改过配置后离线校验（⚠️ `--config` 必须写在子命令**之前**）：
  ```bat
  "C:\Program Files (x86)\cloudflared\cloudflared.exe" tunnel --config "%USERPROFILE%\.cloudflared\config.yml" ingress validate
  ```

### 窗口 ⑤　feishu_bot（飞书入口，roadmap R-49）

```bat
cd /d D:\Project\Recall
python -m recall.feishu_bot
```

- 前提 ①：**API 已在跑**（每次提问都调 `POST /kb/answer`）。
- 前提 ②：`.env` 里 `FEISHU_APP_ID` / `FEISHU_APP_SECRET` **两个都**配了 —— 缺任一个会
  **fail-closed 直接退出（退出码 2）**，并在日志里写明缺什么、要去开发者后台开什么。
- 前提 ③ —— **飞书侧 5 项一次性配置**（详见 `roadmap.md` §四 R-49 末尾）：
  ① 权限 `im:message` + `im:message:send_as_bot`；② 开通「机器人」能力；
  ③ 「事件与回调」选 **长连接** 方式并订阅 `im.message.receive_v1`；
  ④ **可用范围包含本人**（否则私聊搜不到机器人）；⑤ 创建版本并发布。
  ⛔ 原 R-41 那套 `wiki:*` / `docx:document:readonly` / `drive:*` **不需要开**。
- ⚠️ **启动到连上约 10 秒**：`import lark_oapi` 冷启动实测 **8.28s**
  （SDK 会急切导入它全部生成的 API）—— 不是卡死，等一下即可。
- ⚠️ **不需要公网、不占端口**：走**长连接**（WebSocket），事件由飞书推到本进程 ⇒
  不依赖窗口 ④ 的隧道，也不用动防火墙。
- ⚠️ 它**不装载任何模型**（只 HTTP 调本机 API）⇒ **不占显存**，可以随时起停，
  不会和窗口 ② / `pytest` 抢 GPU。
- 验收：日志出现 `feishu_bot.starting` 且**没有** `feishu_bot.not_configured`；
  然后在飞书里给机器人发一句**笔记里有的**问题，应收到**带 `[n]` 引用的卡片**。
- 排错就看这几条结构化事件：`event_accepted`（收到了）→ `replied`（答了）。
  只有 `event_accepted` 没有 `replied` ⇒ 多半是飞书权限 / 可用范围 / 版本未发布。
  `duplicate_event_skipped` 是**正常**的：断线重连会重放历史事件，我们按 `event_id` 去重。

### （按需）窗口 ⑥　重灌 / 手动摄取

不是常驻窗口，需要时在**本机**执行（公网的 `/kb/ingest` 已被隧道层挡掉，这是刻意的）：

```bat
cd /d D:\Project\Recall
python ingest.py --update        :: 增量（等价于 watcher 的一次同步）
python ingest.py --rebuild       :: 整篇重灌（会持有模型锁数分钟，期间检索排队）
```

---

## 2. 起完后的验收五连

```bat
cd /d D:\Project\Recall
curl.exe -s http://127.0.0.1:6333/healthz
python tools\verify_phase6.py --api-key <本机 token>
python tools\verify_r49.py
curl.exe -s -o NUL -w "%{http_code}\n" https://recall.iamzyx.xyz/health
```

| 项 | 期望 |
| :--- | :--- |
| `healthz` | `healthz check passed` |
| `verify_phase6.py` | **16 项通过 / 0 失败**（鉴权、审计、门槛、文档权限、工具白名单、watcher 都在里面） |
| **`verify_r49.py`** | **23 项通过 / 0 失败**（飞书凭证 / 依赖窗口 / SDK 表面 / 卡片转义 / 本机检索链路 / 长连接握手；含一次真 WSS 握手） |
| 公网 `/health` | `200` |
| **飞书入口** | 窗口 ⑤ 日志出现 `feishu_bot.starting`；在飞书里问一句**笔记里有的**问题 ⇒ 收到**带 `[n]` 引用的卡片** |

> ⚠️ `verify_r49.py` 的 E 节会真调一次 `/kb/answer`：**冷启动要 35 秒以上**是正常的
> （装模型 16~25s + 检索 5s + DeepSeek 5s），脚本默认超时已放到 90s。
> 想只跑本地检查用 `python tools\verify_r49.py --skip-network`。
>
> 飞书入口**没有可 curl 的端点**（它是长连接，不监听端口）⇒ 最后一项只能"看日志 + 真问一句"，
> 见 §1 窗口 ⑤。

---

## 3. 关闭顺序（与启动相反）

1. **cloudflared**（先断公网入口）
2. **feishu_bot**（再断飞书入口 —— 否则用户提问会打到正在关的 API 上）
3. **watcher**
4. **recall.api**
5. **Qdrant**

每个窗口 `Ctrl+C` 即可。Qdrant 建议让它自己收尾（Ctrl+C 会触发优雅关闭）。

---

## 4. 故障排查

| 症状 | 最可能原因 | 处理 |
| :--- | :--- | :--- |
| `/kb/search` 返回 **503** `qdrant_unavailable` | Qdrant 没起 | 起窗口 ①；⚠️ 必须在 `tools\qdrant` 目录 |
| 本机 DSH 正常，**Coze 报错** | 隧道没跑 / NS 失效 | `tunnel info recall` 看 `CONNECTIONS`；`Resolve-DnsName -Type NS iamzyx.xyz` |
| 所有客户端一律 **401** | 客户端没带 `X-API-Key` | 本机：`$DSH_HOME\mcp-servers.json` 的 `headers`；Coze：MCP 配置的 `headers` |
| watcher 日志 `ingest_giving_up` | 起 watcher 时 API 没起 | 先起 API → `python -m recall.watchdog --once` |
| 首次检索 **15~25 秒** | 模型冷启动 | 正常现象；先预热（见窗口 ②） |
| Coze 提示**工具调用超时** | 撞上冷启动 | 同上，先在本机预热一次再让 Coze 问 |
| 公网 `/kb/stats`、`/kb/ingest` 返回 **403** | **正常**（隧道层刻意挡死） | 无需处理；要重灌请走本机 `ingest.py` |
| 公网 `/mcp`（**少了尾斜杠**）返回 **307** | Starlette 挂载点重定向 | 外部配置一律写 `/mcp/` |
| Qdrant 建 payload 索引报 `IO Error: 拒绝访问` | Qdrant 偶发降级（`tech.md` §12.3） | **重启 Qdrant**（该状态重试无用）；仍复现就跑 `tools\clean_qdrant_orphans.py` 清泄漏 |
| 日志里出现 `store.index_failed` | 某字段索引**重试后仍失败**，已**降级为告警**（2026-10-03 起不再致命，`tech.md` §12.3） | 结果**仍然正确**，只是**过滤检索会变慢**（退化成全量扫描）。`grep store.index_failed data/logs/*.log` 看漏了哪些字段；**若由 Qdrant 瞬时 IO 故障引起，重启 Qdrant 才是根治** |
| `pytest` 崩溃后留下 `recall-test-*` collection（约 0.7~1.5GB） | 崩溃让夹具的 `finally` 跑不到 ⇒ `delete_collection` 从未执行，**Qdrant 仍认得**它（与"孤儿目录"不是一回事） | `python tools\clean_qdrant_orphans.py` 先看分类，确认后 **`--yes --include-known`**（先经 API 删 collection、再删磁盘目录，**顺序不可颠倒**）；⚠️ 执行前确认**测试没在跑** |
| 端口 6333 / 8000 被占用 | 已经有一个实例在跑 | `Get-NetTCPConnection -LocalPort 8000 -State Listen`；**别起两份**（双份模型会 OOM/崩溃） |
| 改了 `.env` 但没生效 | 配置只在**进程启动时**读取 | 重启对应进程；`verify_phase6.py` 的"配置-运行态一致性"检查能查出这种假绿 |
| 跑全量 `pytest` 崩溃 / **`0xC0000005` 访问违例**（崩在 `transformers` 模型加载处） | 🔴 **主机内存不够 —— 不只是显存**。本机总内存 **15.7 GB**，而 `recall.api` 常驻 **7.8 GB**、`ollama app` 约 **2 GB** ⇒ 全量 pytest 再装一份 bge-m3 就耗尽（实测可用内存仅 ~**4.3 GB**）。⚠️ 比干净的 OOM 更难读，**别指望看到清晰的 OOM 报错** | ① **首选**：先停 `recall.api` 再跑全量（同时解决显存与内存）；② **不想停 API 的替代**：`$env:RECALL_TEST_DEVICE='cpu'` **且逐文件跑**（`pytest tests/test_x.py`）—— 每个进程退出即释放内存，实测可行（`test_ingest.py` 12 passed / `test_rerank.py` 8 passed）；③ 崩后 `conftest` 的清理跑不到 ⇒ **必须查一次 Qdrant 孤儿 collection**（`recall-test-*`） |
| 飞书里问机器人**完全没反应** | 事件没订阅上 / 版本未发布 / 可用范围没加自己 | 逐项核对窗口 ⑤「前提 ③」那 5 条；若 `feishu_bot` 日志里**连 `event_accepted` 都没有**，就是事件根本没推过来（配置问题），不是代码问题 |
| 飞书机器人回**"服务暂时不可用"** | API 没起，或 `recall.api` 正在重启 | 起窗口 ②。这条卡片是**刻意的降级**（用户提问就该拿到回复），不是 bug |
| 飞书机器人**启动即退出（退出码 2）** | `FEISHU_APP_ID` / `FEISHU_APP_SECRET` 缺一个 | 看日志 `feishu_bot.not_configured` 的 `hint`；补齐后重启 |
| 飞书机器人启动后**约 10 秒才连上** | `import lark_oapi` 冷启动实测 **8.28s** | **正常**（SDK 急切导入它全部生成的 API），不是卡死 |
| `pip check` 报 `lark-oapi` / `websockets` 版本冲突 | `websockets` 被顶到 ≥16 | 重新 `pip install -e ".[dev,eval]"`；`pyproject.toml` 已显式钉 `websockets>=15.0.1,<16`（**15.0.1 是六方约束唯一解**，见 `tech.md` §18 决策 19） |
| **Qdrant 启动即 panic**：`Failed to load local shard … Wal error: Can't init WAL: Kind(WouldBlock)` | **已经有一个 Qdrant 在用这个 storage 目录**（WAL 文件被占用）—— 不是数据损坏 | 先查：`Get-Process qdrant` / `Get-NetTCPConnection -LocalPort 6333 -State Listen`。**若已有一个健康的在跑，直接关掉你刚开的那个窗口即可**（panic 的是"第二个"，第一个没受影响）；确认没有在跑再启动 |
| 启动时 `Config file not found: config/config`、`Filesystem type check is not supported` | Qdrant 找不到**可选**的配置文件、Windows 不支持文件系统类型检查 | **正常噪音**，忽略 |
| 手动 curl 检索返回空/异常 | `-H "X-API-Key: <本机token>"` 里的 **`<本机token>` 是占位符**，忘了替换 | 真值在 `.env` 的 `RECALL_API_KEYS`（第一个 token）；没带对 key 会返回 **401** 而不是空 |
| 手动 curl 查**中文** query 时 `/kb/search` 返回 **0 条证据**（英文 query 正常） | **PowerShell 5.1 的 `Invoke-RestMethod -Body <字符串>` 会把中文按 ANSI 编码**，查询词被弄坏 ⇒ 检索自然为空。**不是产品故障** | 用 **UTF-8 字节**传参：`$b=[Text.Encoding]::UTF8.GetBytes((@{query='内容哈希 幂等';top_k=5}\|ConvertTo-Json -Compress)); Invoke-RestMethod ... -Body $b`。实测同一查询：字符串传参 0 条 → 字节传参 **4 条 / top1 0.954** |

---

## 5. 密钥与凭据（只记位置与用途，**值绝不写进仓库**）

| 凭据 | 位置 | 用途 |
| :--- | :--- | :--- |
| `RECALL_API_KEYS` | `.env` | `token:user` 逗号分隔；当前**两个 token 都映射 `me`**（本机一个、Coze 一个，便于单独吊销） |
| `RECALL_WATCHDOG_API_KEY` | `.env` | watcher 调 `POST /kb/ingest` |
| `DEEPSEEK_API_KEY` | `.env` | 胖端点 `kb_answer` 的生成 |
| `FEISHU_APP_ID` / `FEISHU_APP_SECRET` | `.env` | 飞书入口（`recall.feishu_bot`）的应用凭证；开发者后台「凭证与基础信息」取。**两个都要有**才算配置完成 |
| `X-API-Key`（本机 token） | `$DSH_HOME\mcp-servers.json` | DSH 经 MCP 调 Recall |
| `X-API-Key`（Coze token） | Coze 的自定义 MCP 配置 | Coze 调 Recall |
| `cert.pem`、`<tunnel-id>.json` | `%USERPROFILE%\.cloudflared\` | cloudflared 认证与隧道凭据（**绝不入库**） |

**轮换某个 token**：`python -c "import secrets;print(secrets.token_urlsafe(32))"` → 改 `.env` 与对应客户端 → 重启 `recall.api`。

---

## 6. 环境事实速查

| 项 | 值 |
| :--- | :--- |
| conda 环境 | `D:\miniconda\envs\recall`（提示符 `(recall)`） |
| Qdrant | `tools\qdrant\qdrant.exe`，**v1.19.1**，`127.0.0.1:6333` |
| API | `python -m recall.api`，**只绑** `127.0.0.1:8000` |
| watcher | `python -m recall.watchdog` |
| **feishu_bot** | `python -m recall.feishu_bot`（roadmap R-49）—— **长连接**，**不监听任何端口** ⇒ 不需要公网、不占端口；`import lark_oapi` 冷启动 **8.28s**；**不装载模型** ⇒ 不占显存 |
| cloudflared | `C:\Program Files (x86)\cloudflared\cloudflared.exe`，**2026.9.3** |
| 隧道 | 名称 `recall`，ID `83a05aa9-0966-4c88-9f9d-9e918b07bf60` |
| 域名 | `iamzyx.xyz`（**阿里云注册**，NS = `zita/paul.ns.cloudflare.com`） |
| 公网入口 | `https://recall.iamzyx.xyz/mcp/`（Coze）、`/kb/search`（REST，需 key） |
| 隧道配置 | `%USERPROFILE%\.cloudflared\config.yml`（模板：`tools/cloudflared/config.example.yml`） |
| 日志 | `data\logs\api.log`、`watchdog.log`、**`feishu_bot.log`**、`audit.jsonl`（审计，含 `peer`/`client_source`） |
| 磁盘 | D 盘需留余量；全量 `pytest` 每次泄漏 0.7~1.4GB，定期 `python tools\clean_qdrant_orphans.py` |
| **内存** | 总 **15.7 GB**；⚠️ `recall.api` 常驻 **7.8 GB**、`ollama app` 约 **2 GB** ⇒ 剩余通常只有 **4~5 GB**。**跑全量 `pytest` 会因此崩溃**（见 §4），要么先停 API，要么 `RECALL_TEST_DEVICE=cpu` + 逐文件跑 |
| GPU | RTX 4050 Laptop **6141 MiB**；`recall.api` 常驻模型 + 浏览器等桌面程序后，**常常只剩 300~500 MiB** ⇒ 跑 GPU 用例前务必先停 API |

---

## 7. 安全加固：Qdrant 必须只绑回环（2026-09-30 实测发现）

**问题**：Qdrant 默认 `service.host = 0.0.0.0`，而它**自身没有任何鉴权**。同时本机 Windows 防火墙里
存在两条**用户级放行规则**（`TCP Query User{…}qdrant.exe` / `UDP Query User{…}qdrant.exe`，
配置 = **Public**），而本机 WLAN 当前恰好就是 **Public** ⇒ 实测 `192.168.0.3:6333` **可连**。
⇒ **同网段（同一个 Wi-Fi / 局域网）的任何设备都能完整读写、甚至删除你的向量库。**

**两层修复（都要做，互为兜底）**：

1. **让 Qdrant 只绑回环**（仓库侧已做，`.ps1` 已内置；手动启动要自己加）：
   ```bat
   set QDRANT__SERVICE__HOST=127.0.0.1
   ```
   或直接用 `tools\start-qdrant.ps1`（它已经设好）。改完**重启 Qdrant**，再用
   `Get-NetTCPConnection -LocalPort 6333 -State Listen` 应看到 `LocalAddress = 127.0.0.1`。
2. **确认防火墙里那两条规则是 `Block`**（需要**管理员**终端）。⚠️ **2026-09-30 实测：本机这两条规则
   已经是 `Block`（显式拒绝）—— 那就已经到位，无需删除、也无需改动**（拒绝规则优先于一切放行）：
   ```powershell
   # 查看当前状态（以管理员身份打开 PowerShell）
   Get-NetFirewallRule -DisplayName 'qdrant' | Select-Object DisplayName,Profile,Action,Enabled | Format-Table -AutoSize
   ```
   - 若显示 **`Block`** ⇒ ✅ 完成（**这就是本机现在的情况**，两个独立防护层都关上了）；
   - 若显示 **`Allow`** ⇒ 改成阻止或删掉：
     ```powershell
     Get-NetFirewallRule -DisplayName 'qdrant' | Set-NetFirewallRule -Action Block
     # 或者直接删：Get-NetFirewallRule -DisplayName 'qdrant' | Remove-NetFirewallRule
     ```
   ℹ️ 本机**其它端口的暴露面核对**（2026-09-30 实测）：`8000` 只绑 `127.0.0.1` ✓；
   `cloudflared` 的两条防火墙规则是 **Block**（入站被拦，正确，隧道本来只需出站）✓。

⚠️ **改防火墙不会影响你自己的服务**：本机 API 走的是 `127.0.0.1:6333`，而**回环不受 Windows 防火墙入站规则约束**，
所以不管是 Block 还是删除，**服务照常**。改完可以跑一次
`curl.exe -s -H "X-API-Key: <token>" -H "Content-Type: application/json" -d "{\"query\":\"混合检索\",\"top_k\":1}" http://127.0.0.1:8000/kb/search`
确认检索链路没受影响（`<token>` 换成 `.env` 里 `RECALL_API_KEYS` 的第一个真值）。

### 7.1 本机的最终状态（2026-09-30 项目工程师实测确认）

| 层 | 状态 | 证据 |
| :--- | :--- | :--- |
| 绑定层 | ✅ 已修 | `Get-NetTCPConnection -LocalPort 6333,6334 -State Listen` ⇒ 两个都是 **`127.0.0.1`** |
| 网络层 | ✅ 已到位 | `Get-NetFirewallRule -DisplayName 'qdrant'` ⇒ 两条都是 **`Block`** |
| 外部可达性 | ✅ 已关闭 | `Test-NetConnection -ComputerName 192.168.0.3 -Port 6333 -InformationLevel Quiet` ⇒ **`False`**（此前是 `True`） |
| 服务健康 | ✅ 正常 | `curl.exe -s http://127.0.0.1:6333/healthz` ⇒ `healthz check passed`；`/kb/search` 200 |

**可选的第三层**（需改代码，未实施）：给 Qdrant 配 `service.api_key`，并在 `recall.store.QdrantStore`
里带 `api_key` 建客户端。收益是"即使误绑 0.0.0.0 也要有钥匙"，代价是配置与代码各改一处 + 用例。
