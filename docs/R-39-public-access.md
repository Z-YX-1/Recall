# R-39 公网接入手册（Coze / 扣子）

> **状态**：**待你选路线**（仓库侧前置件已全部就绪）｜ 2026-09-26 更新 ｜ 对应 roadmap **v0.27.0**
> **用法**：选定路线后照着做；标注「待核实」的条目**必须按当时文档实测**，不要照抄。
>
> 📌 **2026-09-26 回写**：本手册初稿（09-25）列的三项待办 **A/B/C 当天就已全部实现**（见 §8）。
> §0 第 ③ 条与 §7 里"尚未实现 / 全有或全无"的措辞**已过时**，本次一并更正
> ⇒ **选路线之前不再需要任何立项**，只差你的"路线 + 域名"决定。

---

## 0. 先读这三条 —— 它们决定方案能不能成立

**① 本机只有一份模型（bge-m3 + reranker ≈4.5GB 显存）。**
⇒ **不要为公网另起一个 API 实例**（双份显存，正是 R-23b 那类 `access violation` 崩溃的土壤，
还要与本地实例抢卡）。公网与本地**必须共用同一个进程** ⇒ 这也决定了工具权限只能**按身份**分，
不能靠"另一个实例配一份白名单"。

**② `RECALL_API_KEYS` 为空 = 不鉴权**（有意的 fail-open，见 tech.md §7.1）。
⇒ **公开之前必须先配 key 表**；否则 `POST /kb/ingest` 这个**写端点**就是公开的，
任何人都能重灌知识库。

**③ 所有文档目前都是 `owner=me, visibility=private`。**
`ingest.py` **硬编码**这两个字段（第 320-321 行），**不读 frontmatter**。
⇒ 给 Coze 的 token **必须映射到 `me`**；映射成别的身份（如 `coze`）会被 R-40 的权限过滤
**全部挡掉，检索恒为空**（实测：`evidence: []`）。
📌 **2026-09-25 已实现**（roadmap R-39 待办 A，`ingest.py::_permissions_from()`）：ingest 现在会从
frontmatter 读 `owner` / `visibility` / `groups`，并**fail-closed**（缺失 / 类型错 / 未知取值一律按
最私有处理并告警）；且**只改权限也会重索引**（比对权限三元组，堵掉"正文哈希未变⇒被跳过"的陷阱，
见 tech.md §3.4）。⇒ 现在有**两条可选姿势**：

- **省事**：Coze 的 token 映射到 `me`（能看到全部 65 篇，含私有）；
- **分权**：给想让外部看的笔记加 `visibility: public`，Coze 用**非 `me`** 身份 ⇒ 只看得到那些公开笔记。
  ⚠️ 当前库里**一篇 public 都没有**（65 篇全 `me/private`），`tools/verify_phase6.py` 的
  "文档权限分布"一节会如实报出来。

---

## 1. 路线选择

| 路线 | 说明 | 结论 |
| :--- | :--- | :--- |
| **A. Cloudflare Tunnel**（`cloudflared` 跑在**本机**） | 本机主动向 Cloudflare 建**出站**连接，把 `127.0.0.1:8000` 暴露成一个 HTTPS 域名 | ✅ **推荐**：无需公网 IP、无需开放端口、**无需把 `RECALL_HOST` 改成 0.0.0.0** |
| B1. 云服务器做反向代理回本机 | 仍要本机对公网可达 ⇒ 等于再套一层隧道 | ❌ 不推荐：更复杂、多一跳延迟 |
| B2. 把 Recall 整体搬到 GPU 云服务器 | 要重灌索引、同步 vault、付 GPU 租金；换来"本机关机也能用" | ⚠️ 仅当确有该需求才做，属**另一条产品路线**（不是"公网接入"） |

**为什么 A 更安全**：cloudflared 在本机连 `127.0.0.1:8000`，所以服务继续**只绑回环**，
攻击面比"绑 0.0.0.0 + 防火墙"小得多。

---

## 2. 安全前置清单（**逐条打勾再暴露**）

- [ ] `.env` 加 `RECALL_API_KEYS="<token>:me"` —— token 用
      `python -c "import secrets;print(secrets.token_urlsafe(32))"` 生成
- [ ] 重启 `python -m recall.api`（**必须重启**：改配置对已跑进程无效，验收脚本能查出这一点）
- [ ] 跑 `python tools\verify_phase6.py --api-key <token>`：鉴权几项应全绿
- [ ] 确认 `GET /kb/stats` **无 key 返回 401**（别把索引规模和建库参数暴露给公网）
- [ ] 公网**只放读端点**：`/kb/search`、`/kb/answer`；在**网关层挡住** `/kb/ingest`（写）与 `/kb/stats`
- [ ] `RECALL_INGEST_RATE_LIMIT` 保持开启（默认 `10/60`）；公网建议**网关再加一道**
- [ ] 走 MCP 时的工具收窄：⚠️ **本项目身份取"省事档"（Coze 的 token 也映射到 `me`）** ⇒
      **不要**写 `RECALL_MCP_TOOL_POLICY="me:kb_search"` —— 白名单**按用户**生效且**回环不豁免**，
      那一行会**连带把本机 DSH 的工具也砍掉**。正确做法是**在 Coze 侧只启用 `kb_search`**
      （Coze 的 MCP 工具有启用开关）。若将来确实要应用层收窄，得先把 Coze 换成**独立身份**
      （配合 `visibility: public` 或按 `groups` 授权）
- [ ] 确认 `data/logs/audit.jsonl` 在写、且**不含密钥**
- [ ] 想清楚 `/kb/answer` 会**烧 DeepSeek 额度** ⇒ 网关侧配额或干脆不给 Coze 开这个工具

---

## 3. 路线 A：Cloudflare Tunnel

### 3.1 先快速验证（临时域名，几分钟）

```powershell
winget install --id Cloudflare.cloudflared      # 或到官网下 zip 解压
cloudflared tunnel --url http://127.0.0.1:8000
```

输出里会有形如 `https://xxx-yyy-zzz.trycloudflare.com` 的地址。**仅用于验证**：
无 SLA、域名每次重启都变。

在**外网**（手机热点 / 另一台机器）验证：

```powershell
curl.exe -s -o NUL -w "%{http_code}`n" https://<临时域名>/health          # 期望 200（免鉴权探活）
curl.exe -s -o NUL -w "%{http_code}`n" https://<临时域名>/kb/stats        # 期望 401
curl.exe -s -H "X-API-Key: <token>" -H "Content-Type: application/json" `
  -d '{\"query\":\"混合检索\",\"top_k\":3}' https://<临时域名>/kb/search   # 期望 200 + evidence
```

### 3.2 长期（named tunnel + 自有域名）

```powershell
cloudflared tunnel login
cloudflared tunnel create recall
cloudflared tunnel route dns recall recall.example.com
# 写 config.yml：tunnel / credentials-file / ingress（hostname → service: http://127.0.0.1:8000）
cloudflared tunnel run recall
cloudflared service install     # 计划任务式开机自启
```

### 3.3 在网关层挡住不该公开的路径

- **Cloudflare Access / WAF 规则**：`/kb/ingest`、`/kb/stats` → Block
- **Rate Limiting**：Cloudflare 侧速率规则（应用层已有写端点限流，网关再加一道覆盖读端点）

---

## 4. Coze（扣子）侧接入

> 官方文档核实（2026-09-25，[docs.coze.cn/mcp](https://docs.coze.cn/mcp)）：
> **支持 Streamable HTTP 与 SSE，不支持 STDIO**；自定义 MCP 通过 **MCP JSON** 添加；
> 官方明确提示 MCP 的工具名/说明/参数会**占用 Agent 上下文**并增加 Token 与积分消耗，
> **建议每个 Agent 最多启用 10 个 MCP**。

### 4.1 走 MCP（推荐：工具即能力）

```json
{ "mcpServers": { "recall": { "url": "https://recall.example.com/mcp" } } }
```

⚠️ **待核实（官方文档未明确）**：自定义 MCP 的 JSON **能否直接带自定义 header**。
文档只说"根据页面提示完成第三方授权或填写**连接凭证**"，并有"连接凭证失效 ⇒ 需重新连接"的说法
⇒ 凭证可能是**添加后按页面提示填写**，而不是写在 JSON 里。

因此有两条子路径，**必须实测后二选一**：

| 子路径 | 做法 | 注意 |
| :--- | :--- | :--- |
| 4.1a Coze 侧能填 header | 填 `X-API-Key: <token>` | 最简单，应用层鉴权原样生效 |
| 4.1b Coze 侧只能填"凭证" | 用 **Cloudflare Access 的 Service Token** 在**网关层**鉴权 | 此时应用层与网关层会**两层鉴权**：要么应用层对 `/mcp` 放行（改动 `PUBLIC_PATHS`），要么让 Coze 同时带两种凭证 |

配合 `RECALL_MCP_TOOL_POLICY` 只暴露 `kb_search`（必要时 + `kb_answer`）：既省上下文，
也避免把写端点交给对方。⚠️ **但本项目身份取"省事档"（Coze 与本机 DSH 同为 `me`）⇒ 应用层收窄会
一并限制本机 DSH**（白名单按用户生效、回环不豁免）⇒ **工具收窄请在 Coze 侧做**（只启用 `kb_search`）。

### 4.2 走插件（HTTP API）

- 插件需要 OpenAPI schema —— 可直接用 `https://<域名>/openapi.json`，
  但**必须先删掉 `/kb/ingest`**（别把写端点导进 Coze），并把 `servers` 改成公网域名；
  `/mcp` 是挂载点、本来就不在 schema 里。
- 鉴权在插件配置里填 API Key（header）。

---

## 5. 验收判据

| # | 判据 | 期望 |
| :--- | :--- | :--- |
| 1 | 外网 `GET /health` | 200（免鉴权探活） |
| 2 | 外网 `GET /kb/stats` 无 key / 带 key | **401** / 200 |
| 3 | 外网 `POST /kb/search` 带 key | 200 且有 evidence |
| 4 | 外网 `POST /kb/ingest` | **403**（网关挡住） |
| 5 | 本机 DSH 会话 | **仍正常**（打隧道不应影响本地使用） |
| 6 | `data/logs/audit.jsonl` | 有对应记录；**不含密钥** |
| 7 | Coze 侧真实问一句笔记里的内容 | 引用可核对（与本地问同一问题的答案一致） |

---

## 6. 回滚（三步回到 S1）

```powershell
cloudflared service uninstall        # 或直接 Ctrl+C 掉临时隧道
# .env 删掉 RECALL_API_KEYS，重启 python -m recall.api  ⇒ 回到 S1（不鉴权，仅回环）
# 删掉 Coze / mcp-servers.json 里的配置
```

---

## 7. 已知限制（写清楚，免得踩）

1. **`/kb/answer` 会把证据文本发到 DeepSeek**（出域仅限这一次生成调用；本地 embedding 与库内容不出域）。
2. ~~审计里的 `client` 字段在隧道下会是本机/隧道地址~~ ⇒ **2026-09-25 已实现**（待办 B）：
   审计行新增 `peer` 与 `client_source`，`client` 改为**有效客户端**；**只有直连对端落在
   `RECALL_TRUSTED_PROXIES`（默认 `127.0.0.1,::1`）内才采信** `CF-Connecting-IP` / `X-Forwarded-For`
   （否则转发头可被伪造成任意 IP，比不记还糟）。⇒ 隧道下要看到真实来源，请把隧道 / 反代的
   出口地址加进 `RECALL_TRUSTED_PROXIES`（tech.md §12.2）。
3. **本机是桌面机**：关机 / 休眠 = 公网不可用。这是选路线 A 的固有代价。
4. **限流是进程内的**（`recall/ratelimit.py`）：多进程部署时各算一份 ⇒ 公网场景需网关再加一道。
5. ~~文档权限是"全有或全无"~~ ⇒ **已可按 frontmatter 分权**（待办 A，见 §0 第 ③ 条）。
   剩下的限制是**现状**而非能力：当前 65 篇全是 `me/private` ⇒ 不写 `visibility: public`，
   对外身份就仍然什么都检索不到。

---

## 8. 本手册暴露的三项待办 —— **均已实现（2026-09-25）**，无需立项

> 初稿（09-25）把它们列为"供立项参考"，**当天即全部做完**（变更记录见 `spec/roadmap.md` §四 R-39
> 与 §七）。下表保留原始动机，并补上落地位置与验证方式。

| 编号 | 事项 | 为什么需要 | 落地 |
| :--- | :--- | :--- | :--- |
| 待办 A | ingest 支持从 **frontmatter** 读 `owner` / `visibility` | 否则无法让 Coze 只能看"公开笔记"；权限模型停在"全有或全无" | ✅ `ingest.py::_permissions_from()`（fail-closed）+ 权限三元组参与跳过判定；`tests/test_permissions.py` **12 项**；tech.md **§3.4** |
| 待办 B | 审计记录**网关来源头**（如 `CF-Connecting-IP`） | 公网场景下区分远程调用者，审计才有意义 | ✅ `recall/audit.py`：`TrustedProxies` + `resolve_client()`，审计行加 `peer` / `client_source`；`tests/test_gateway_trust.py` **13 项**；tech.md §7.1 / **§12.2** |
| 待办 C | `/mcp` 的鉴权可在网关层接管 | 若 Coze 侧只能填"凭证"而放不下自定义 header（§4.1b） | ✅ `RECALL_MCP_AUTH_MODE=app\|gateway`（配 `RECALL_MCP_GATEWAY_USER`；网关模式启动 WARNING 提醒**必须**同时配工具白名单）；tech.md §7.1 |

📌 **因此 R-39 只剩「选路线 → 开隧道 → 配 key 表 → 验收」**，仓库侧没有待立项的工作了。
⚠️ 但部署时的两条硬约束不变：① **先配 `RECALL_API_KEYS`**，否则 `POST /kb/ingest` 公开可写；
② 用**网关模式**时**必须**同时配 `RECALL_MCP_TOOL_POLICY`，否则等于把整个知识库连同写端点交出去。
