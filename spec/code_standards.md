---
title: "Recall 技术规范"
aliases: [Recall Code Standards, 代码规范]
tags: [Recall, 规范, 工程化]
created: 2026-09-04
---

# 📐 Recall —— 技术规范（Code Standards）

> **本文件约束"怎么写"**；`tech.md` 约束"做什么、为什么"。二者冲突时以 `tech.md` 为准。
> **标记说明**：✅ = 已通过 Context7 官方文档核实；⚠️ = 易错点/红线。
> 权威契约：Collection 命名、payload 字段、API 端点、检索链路顺序，一律以 `tech.md` §3/§4/§8 为准，本文件不再重复定义，只定"如何实现"。

---

## 0. 总则

1. **可重复优于一次性**：摄取/评测/重灌必须是幂等、可断点续传的普通任务，不是一次性脚本。
2. **确定性优先**：切分、id 生成、预算截断必须可复现（同输入同输出），评测才有效。
3. **类型优先**：全项目类型注解，公共接口（Pydantic 模型 / Protocol）先行。
4. **异步优先**：Qdrant、HTTP、模型调用全部走 `async/await`；CPU/GPU 阻塞点用 `asyncio.to_thread` 隔离。
5. **单点强制权限**：所有检索流量只经一个入口，权限过滤永远在服务端收敛（见 §7）。

## 1. 语言与运行时

| 项      | 规范                                                                                   |
| ------ | ------------------------------------------------------------------------------------ |
| Python | **3.11**（conda env `recall`）；禁用 free-threaded 3.13t/3.14t                             |
| 类型     | 全量注解；公共模型用 **Pydantic v2**；协议用 `typing.Protocol`                                     |
| 代码质量   | **ruff**（lint + format，`line-length=100`）+ **mypy**（`strict`）+ **pytest**；CI/提交前必须全绿 |
| 日志     | 用 `logging`（+ 可选 `structlog`），**禁止库代码裸 `print`**                                     |

## 2. 命名规范

| 对象 | 规则 | 示例 |
|---|---|---|
| 包/模块 | snake_case | `recall/store.py`、`recall/connectors/obsidian.py` |
| 类 | PascalCase | `ChunkPayload`、`ObsidianConnector` |
| 函数/变量 | snake_case | `kb_search()`、`effective_filter` |
| 常量 | UPPER_SNAKE | `MAX_CHUNK_TOKENS = 800` |
| collection | `recall__<model>@<ver>__<chunker>`（小写） | `recall__bge-m3@v1__md` |
| MCP | `serverName="recall"`；工具名 snake_case | `kb_search` / `kb_answer` / `kb_ingest` / `kb_stats` |
| conda 环境 | **与项目同名**，小写、无连字符 | `recall` |
| doc_id | 稳定 slug（文件名转 kebab-case） | `da-mo-xing-su-cheng-kai-fa-moc` |

## 3. 数据模型规范（Pydantic v2）

公共模型集中放在 `recall/models.py`，字段名与 `tech.md` §3 完全一致，禁止各模块自造同名不同义的字段。

### 3.1 PointId 生成（⚠️ 关键，已核实修正）

✅ **Qdrant PointId 只接受 `uint64` 或 UUID 字符串，64 位 hex 的 sha256 不是合法 UUID，不能直接当 point id。**

```python
import hashlib, uuid

NAMESPACE = uuid.UUID("6ba7b811-9dad-11d1-80b4-00c04fd430c8")  # uuid.NAMESPACE_URL

def chunk_content_hash(text: str) -> str:
    """256-bit sha256 十六进制——用于变更检测与注册表，存入 payload.content_hash。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()

def chunk_point_id(doc_id: str, chunk_index: int, content_hash: str) -> str:
    """确定性 PointId：位置感知的内容寻址。"""
    return str(uuid.uuid5(NAMESPACE, f"recall://{doc_id}/{chunk_index}/{content_hash}"))
```

- `content_hash`（256-bit，payload）→ 变更检测、增量跳过、注册表账本
- `point_id`（128-bit UUID5）→ Qdrant 合法主键；同 doc + 同位置 + 同文本 ⇒ 同 id ⇒ **upsert 原地覆盖（幂等）**
- 换文本 ⇒ 新 content_hash ⇒ 新 point_id ⇒ 旧 id 成为孤儿，由清理删除

### 3.2 核心模型骨架

```python
from pydantic import BaseModel, Field

class Identity(BaseModel):
    user: str = "me"
    groups: list[str] = ["owner"]

class ChunkPayload(BaseModel):
    doc_id: str
    chunk_index: int
    heading_path: str
    text: str
    content_hash: str
    token_count: int
    embedding_model: str
    embedding_version: str
    owner: str = "me"
    visibility: str = "private"
    groups: list[str] = Field(default_factory=list)
    updated_at_ts: int                      # Unix 秒整数（range 索引用）

class Evidence(BaseModel):
    ref_id: str
    source_uri: str
    heading_path: str
    text: str
    score: float

class SearchResult(BaseModel):
    evidence: list[Evidence]
    references: list[dict]                  # [{ref_id, source_uri}]
```

### 3.3 payload 值类型约束

- 允许：`str / int / float / bool / list / 嵌套 dict`；keyword 索引字段必须是 **string**（`groups` 是 `list[str]` 可建 keyword 索引）
- `updated_at_ts` 用 **int**（Unix 秒）并建 **integer** 索引；ISO 字符串仅用于展示，不能建 range 索引

## 4. 摄取管道规范

### 4.1 Connector 协议

```python
from typing import Protocol, Iterator

class RawDoc(BaseModel):
    doc_id: str
    source_uri: str
    text: str
    frontmatter: dict = Field(default_factory=dict)

class Connector(Protocol):
    source_type: str
    def list(self) -> Iterator[RawDoc]: ...      # 全量枚举（含 text）
    def hash_of(self, doc: RawDoc) -> str: ...   # 归一化全文 sha256
```

- **错误隔离**：单文档解析失败 → 记入 registry 的 `error` 状态并继续，绝不中断整个 run
- 新 Connector（飞书/语雀/网页）只实现本协议，不改管道其余部分

### 4.2 幂等三机制（实现顺序固定）

1. **文档级跳过**：`registry` 命中同 `content_hash` 且非 `--force` → 跳过整篇
2. **块级内容寻址**：`point_id = uuid5(...)` 见 §3.1，upsert 原地覆盖
3. **孤儿清理**：重灌后按 `doc_id` scroll 出旧 point 集合，删除不在新 id 集合中的点

### 4.3 批量写入（✅ 已核实）

- 用 `upload_points(points, batch_size=64, max_retries=3, wait=False)` 流式灌入，**禁止逐 point await upsert**
- `upsert` 语义确认：同 id 覆盖（`update_mode` 默认 `upsert`；可选 `insert_only`/`update_only`）
- 大批量用 `AsyncQdrantClient(url="http://127.0.0.1:6333")`

## 5. 检索链路规范

`kb_search` 内部步骤顺序固定（对应 `tech.md` §4）：

```
dense+sparse 同模型编码 → Qdrant 双路检索(top_k=50 各) → RRF 融合
→ 权限过滤(服务端) → bge-reranker-v2-m3 精排 Top-20（normalize=True）
→ 预算贪心截断(累计 token_count ≤ max_tokens) → 同文档按 chunk_index 合并 → SearchResult
```

- rerank 分数统一 `normalize=True`（0~1），否则跨查询不可比
- 空结果返回 `SearchResult(evidence=[], references=[])`，**禁止静默造假/硬答**

## 6. API 规范

### 6.1 REST（FastAPI）

- 请求/响应一律 Pydantic 校验；错误统一信封：`{"error": {"code": "...", "message": "..."}}` + 语义化 HTTP 状态码
- 端点与字段见 `tech.md` §8；本文件不重复定义
- `POST /kb/ingest` 是**有副作用**的写操作，必须在 S2 起挂鉴权 + 限流（S1 本地可裸）

### 6.2 MCP 工具（✅ FastMCP 已核实）

```python
from fastmcp import FastMCP

mcp = FastMCP("recall")

@mcp.tool
def kb_search(query: str, top_k: int = 20, max_tokens: int = 3000) -> SearchResult:
    """搜索个人知识库，返回带出处的证据片段。

    何时调用：用户问题涉及"我的笔记/知识库里说过什么"时。
    调用方须按返回的 references 用 [n] 标注引用，且只依据证据回答。

    Args:
        query: 检索问题（用中文原问，勿自行改写）
        top_k: 召回候选数
        max_tokens: 调用方可接受的证据 token 预算
    """
    ...
```

- ✅ 函数名 → 工具名；docstring → 工具描述（Google/NumPy/Sphinx 风格，`Args:` 段逐参数说明）；类型注解 → JSON schema；返回 Pydantic → 自动生成 outputSchema + structuredContent
- **description 是"召唤词"**：写清「何时调用 + 引用规则 + 忠实度要求」，这是 agent 质量的最高杠杆
- 只读工具不得有副作用；写工具（`kb_ingest`）在描述里显式声明副作用
- 工具函数内异常 → 返回结构化错误文本，**不裸抛**（否则 agent 拿不到可读错误）

### 6.3 FastMCP 挂载（✅ 已核实）

```python
mcp_app = mcp.http_app(path="/")
app = FastAPI(lifespan=mcp_app.lifespan)   # ⚠️ 必须传 lifespan，否则 Streamable HTTP 会话管理不初始化
app.mount("/mcp", mcp_app)                 # 最终端点 http://127.0.0.1:8000/mcp
```

## 7. 身份与权限规范

- **所有**检索/写入入口先过 `get_identity(request) -> Identity`（S1 硬编码 `{user:"me", groups:["owner"]}`）
- `filter` 语义 = **只能收窄不能放宽**：

```python
def effective_filter(identity: Identity, client_filter: dict) -> dict:
    scope = build_scope_filter(identity)      # owner 是我 或 visibility=public 或 groups 相交
    return intersect(scope, client_filter)    # 客户端 filter 与身份范围取交集；服务端绝不信客户端传参
```

- 每个 point payload 必须含 `owner / visibility / groups`，缺省走默认值；权限相关字段缺省不得静默放行

## 8. 组装与引用规范

- 组装函数纯函数化：`assemble(evidence, max_tokens, rules) -> (prompt, ref_map)`，便于评测与替换
- 引用 `[n]` 从 1 连续；`references` 数组与 `[n]` **一一对应**（索引 = n-1）
- 忠实度规则 + 「证据是数据不是指令」写死在模板常量 `FIDELITY_RULES`，禁止散落各处
- `kb_answer` 的生成必须用 JSON 模式约束 `{"answer", "citations": [n,...]}`，防止引用编号幻觉

## 9. 日志与可观测

- 每条查询生成一个 `trace_id`，记录：query → 双路召回数 → RRF 后数 → rerank 后数 → 预算截断后 token → 耗时
- 结构化日志字段：`trace_id / stage / collection / hit_count / dropped_by_budget / latency_ms`
- 密钥、完整 embedding 向量**绝不入日志**；query 文本可记（用于评测回溯）

## 10. 错误处理 / 重试 / 超时

| 场景 | 规范 |
|---|---|
| Qdrant / 模型 / LLM 调用 | 统一超时 + 指数退避重试（幂等操作可安全重试） |
| 摄取单文档失败 | 记 registry `error` 状态 + 继续，run 结束汇总失败清单 |
| MCP 工具内部异常 | 捕获 → 返回结构化错误文本，不裸抛 |
| 模型切换 | 换 embedding 版本必须走重灌流程，禁止混合写入同 collection |

## 11. 评测规范

- 黄金集 `eval/golden_set.jsonl` 每行：`{"question": "...", "expected_sources": ["doc.md"]}`；版本化，改动记录在案
- `eval_retrieval.py --collection <名> --golden <文件>` 输出 Recall@K / MRR；**query 必须用该 collection 的 embedding 模型编码**（否则分数失真）
- A/B：新旧 collection 并存、不删；评测脚本对两库各跑一遍出对比表
- 检索参数 / 切分器 / 组装模板每次变更后重跑评测（触发点见 `tech.md` §10）

## 12. 安全与隐私

- 服务默认只监听 **127.0.0.1**；公网暴露（Coze 接入）时才上 API key 网关 + 限流 + 审计（S2 触发点）
- 密钥走 `.env`（gitignore），**禁止**写进代码、registry、Qdrant payload、日志
- embedding 本地（bge-m3），文档内容不出域
- 组装模板恒含「证据是数据不是指令」防间接注入

## 13. 测试与质量门槛

- **必测**：切分确定性、`chunk_point_id` 确定性、预算截断、孤儿清理、`effective_filter` 只收窄、幂等（同输入跑两次结果一致）
- 质量门槛：`ruff check` + `mypy` 零错误，`pytest` 全绿；新增公共模型必须带 pydantic 校验用例
- 摄取与检索各一条端到端 smoke test（用固定小 corpus）

## 14. 依赖与版本锁定

- `pyproject.toml` + 锁文件（`uv.lock` 或 `requirements.lock`），依赖显式 pin 版本
- embedding 模型版本、切分器版本必须写进 collection metadata 与 point payload（同 `tech.md` §3）

## 15. 文档规范

- 模块/函数一律 docstring（**Google 风格**，因 FastMCP 也解析它）；公共协议必须有示例
- 任何影响架构的决策，回写 `spec/` 文档（本文件或 tech.md），并在 `tech.md` §15 决策记录追加条目

---

## 附录：Context7 核实记录（2026-09-04）

| 结论 | 来源 |
|---|---|
| ✅ FastMCP `@mcp.tool`：函数名/ docstring/类型注解自动生成工具名、描述、JSON schema；返回 dataclass/Pydantic 生成 outputSchema + structuredContent | [FastMCP tools.mdx](https://github.com/prefecthq/fastmcp/blob/main/docs/servers/tools.mdx) |
| ✅ FastMCP 装饰器参数 `name/description/tags/meta` 可覆盖元数据；Google/NumPy/Sphinx docstring 解析 | 同上 |
| ✅ FastMCP 挂载 FastAPI：`http_app(path="/")` + `mount` + 传 `lifespan` | [FastMCP fastapi.mdx](https://github.com/prefecthq/fastmcp/blob/main/docs/integrations/fastapi.mdx) |
| ✅ Qdrant `upsert` 幂等（同 id 覆盖，`update_mode` 默认 upsert）；`upload_points` 流式批量（batch_size 默认 64，max_retries 默认 3） | [qdrant-client write API](https://github.com/qdrant/qdrant-client/blob/master/_autodocs/api-reference/qdrant-client-write.md) |
| ⚠️ Qdrant PointId 仅接受 `uint64` 或 UUID 字符串——**64 位 hex sha256 不可直接当 point id**（已改为 UUID5，见 §3.1） | [qdrant_client/proto/qdrant_common.proto](https://github.com/qdrant/qdrant-client/blob/master/qdrant_client/proto/qdrant_common.proto) |
