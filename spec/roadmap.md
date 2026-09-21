---
title: "Recall 开发路线图"
aliases: [Recall Roadmap, 路线图]
tags: [Recall, 路线图, 项目管理]
created: 2026-09-04
---

# 🗺️ Recall 开发路线图 (Roadmap)

> **版本**：v0.2.0（含变更登记铁律）｜**依据**：`tech.md`（技术栈与架构契约）+ `code_standards.md`（技术规范）
> **执行者**：AI｜**决策者**：项目工程师（项目所有者）
> **本文件是"活文档"**：随写代码过程动态更新，任何一步受阻都按第二节协议处理，**绝不静默跳过或删除原步骤**。

---

## 一、 文档定位

1. 本路线图把 `tech.md` §13 落地路线（P0~P3）与 `code_standards.md` 的模块清单**细化为一串可执行、可验证的步骤**（R-01 起编号），AI 按顺序逐步执行。
2. 每一步都有**明确动作**与**验证方式**（测试/检查点），完成标准 = 动作做完 + 验证通过 + **git 提交**（R-04 仓库就绪后执行；提交信息格式：`<阶段>/<编号> <简述>`）。
3. 路线图只解决"做什么、按什么顺序做、卡住了怎么办"；"怎么做、做成什么样"以 `tech.md` / `code_standards.md` 为准。

---

## 二、 使用规则（AI 必读，项目工程师确认）

### 步骤状态标记
| 标记 | 含义 |
| :--- | :--- |
| `- [ ]` | 未开始 |
| `- [x]` | 已完成（验证通过） |
| `⚠️ 问题：` | 该步骤受阻，原因记录在旁 |
| `🧭 项目工程师指示：` | 项目工程师对问题的决策记录 |

### 问题处理协议（铁律，顺序执行）
1. **不删除、不修改原步骤**：在原步骤下方标记 `⚠️ 问题：<具体原因与现象>`；
2. **在下一行写新步骤**：以原编号加字母后缀命名（如 `R-13b`），写明替代方案；
3. **运行测试**：执行新步骤对应的测试，确认验证通过后才算完成；
4. **请示项目工程师**：带着"问题描述 → 影响范围 → 建议方案"三要素请示，等待指示并记录 `🧭 项目工程师指示：...` 后再继续；
5. 原步骤保留为历史记录，**永不删除**。

### 变更登记铁律（每一次改动都必须落入本文件）
> **没有写进本文件的改动 = 没有发生过的改动。** AI 动手前先查本文件，动手后必须回写。

1. **每步必录**：任何代码 / 配置 / 依赖 / 文档 / 环境改动，完成后必须在对应 `R-xx` 步骤更新状态与 `✅ 验证` 记录，**禁止"代码改了、路线图没动"**。
2. **未写明的情形必录**：凡不在现有 `R-01~R-42` 描述范围内的工作（新工具、新依赖、新平台、新脚本、环境变更、契约微调、临时绕行方案等），一律视为"roadmap 未覆盖的情形"，必须**先补录、再执行；确因紧急先执行的，执行后立即补录**。
3. **记录落点（写在哪一格）**：

   | 情形 | 落点 |
   | :--- | :--- |
   | 步骤完成 | 对应 `R-xx` 勾选 `- [x]` + `✅ 验证：...` |
   | 步骤受阻 | 原步骤下 `⚠️ 问题：...` + 新步骤 `R-xxb` |
   | 契约 / 选型 / 范围变更 | §五 问题日志 + 请示记录 `🧭 项目工程师指示：...` |
   | **roadmap 未覆盖的新情况** | §七 变更日志登记（需要时新增 `R-xx` 步骤或新增 Phase） |
   | 阶段收尾 | §六 进度快照更新（当前阶段 / 当前步骤 / 版本号） |

4. **只增不改历史**：补充记录一律"新增行 + 追加说明"，已勾选步骤与历史问题记录**永不删除、不改写**。
5. **自检**：每完成一步反问一句——"这一步的所有改动，在 roadmap 里都能找到对应记录吗？"找不到就补录。

### 必须请示项目工程师的情形
- 涉及**契约变更**（`recall/models.py` 字段、payload 字段、API 端点、错误规范、引用格式）；
- 涉及**技术选型变更**（Qdrant / bge-m3 / FlagEmbedding / DeepSeek / 切分参数 / 检索三件套等 `tech.md` 已定案事项）；
- 涉及**任务范围变更**（新增/删减功能、新增 Connector 平台）；
- 任何**无法在 30 分钟内自行解决**的问题。

### 与 code_standards.md 的分工（防止把琐碎违规升级为请示）
- 代码质量违规按三级响应处理：**L1** 自查自改（不在本文件标记）、**L2** 记录在汇报中、**L3** 才走本协议请示；质量门槛见 `code_standards.md` §13；
- 本文件的 `⚠️ 问题` 标记**只用于"步骤受阻"**，不被代码风格类琐碎违规污染。

### 问题处理格式示例
```text
- [ ] R-13 实现 embedder.py 的 dense+sparse 编码
    ⚠️ 问题：BGEM3FlagModel 加载后显存超限（bge-m3 fp32 + reranker 同驻 6GB 溢出，2026-09-xx）
- [x] R-13b 改为 reranker 懒加载 + fp16，运行 test_embedder.py 通过
    🧭 项目工程师指示：同意 R-13b；embedder 恒 fp16，reranker 按需加载
```

---

## 三、 阶段总览

| 阶段 | 主题 | 对应依据 | 依赖 |
| :--- | :--- | :--- | :--- |
| Phase 0 | 环境与仓库初始化 | tech.md §2/§12 | 无 |
| Phase 1 | 数据契约与摄取管道 | tech.md §3/§5 · code_standards §3/§4 | Phase 0 |
| Phase 2 | 检索链路（kb_search） | tech.md §4 · code_standards §5/§7 | Phase 1 |
| Phase 3 | 接入层：REST + MCP + DSH | tech.md §8/§9 · code_standards §6 | Phase 2 |
| Phase 4 | 胖端点与评测体系 | tech.md §10 · code_standards §8/§11 | Phase 2 |
| Phase 5 | 集成验收与运维演练 | tech.md §13 | Phase 3、4 |
| Phase 6 | 后续迭代预留（不在首版验收范围） | tech.md §7 S2+ | Phase 5 |

---

## 四、 详细步骤

### Phase 0：环境与仓库初始化

- [x] **R-01** 确认本机环境：Miniconda 创建环境 `recall`（`conda create -n recall python=3.11 -y` → `conda activate recall`，Python 3.11.x）；`nvidia-smi` 记录驱动版本并确认支持 CUDA 12.4（RTX 4050）；`python --version` / 驱动信息记录结果（tech.md §2/§12）。
    ✅ 验证：Python 3.11.16（`D:\miniconda\envs\recall`）；驱动 576.02 / CUDA 12.9；GPU RTX 4050 Laptop 6GB。
- [ ] **R-02** 安装 GPU 版 PyTorch：`pip install torch --index-url https://download.pytorch.org/whl/cu124`（网络慢时换清华镜像源）。
    ⚠️ 问题：官方 pip 源下载 2.5GB wheel 多次超时/中断；conda 安装因清华镜像连接超时失败（2026-09-21）。
- [ ] **R-02b** 本地下载 wheel 至 `tools/wheels/` 后 `pip install` 本地文件安装 PyTorch cu124。
    ✅ 验证：`python -c "import torch; print(torch.cuda.is_available())"` 输出 `True`。
- [x] **R-03** 安装并启动 Qdrant 单机服务（native 二进制或 Docker 二选一，按本机环境定），监听 `127.0.0.1:6333`。
    ⚠️ 问题：Docker Desktop 未运行（`dockerDesktopLinuxEngine` pipe 不存在）；改用 native 二进制方案 R-03b。
- [x] **R-03b** 下载 Qdrant v1.19.0 Windows 原生二进制至 `tools/qdrant/`，启动 `qdrant.exe`。
    ✅ 验证：`GET http://127.0.0.1:6333/collections` 返回 `{"result":{"collections":[]},"status":"ok"}`。
- [x] **R-04** 初始化 Git 仓库（`git init`），创建 `.gitignore`（忽略 `.env`、`data/`、`__pycache__/`、`.pytest_cache/`、`.mypy_cache/`、`.ruff_cache/`、`*.pyc`），创建根 `README.md`（引用 `spec/` 下三份文档：tech.md、code_standards.md、roadmap.md）。
    ✅ 验证：仓库已连接 `origin/main`；`.gitignore` + `README.md` 已创建。
- [ ] **R-05** 编写 `pyproject.toml`（ruff / mypy / pytest 配置按 code_standards §1：`line-length=100`、mypy strict）+ 依赖清单：`qdrant-client`、`fastapi`、`uvicorn[standard]`、`fastmcp`、`pydantic`、`FlagEmbedding`、`torch`、`openai`（DeepSeek 走 OpenAI 兼容接口）、`httpx`、`pytest`、`ruff`、`mypy`；执行 `pip install`（清华镜像）；下载模型 `BAAI/bge-m3` 与 `BAAI/bge-reranker-v2-m3`（先设置 `$env:HF_ENDPOINT="https://hf-mirror.com"`）。
    ✅ 验证：模型文件落盘（HF cache 目录可见）；`python -c "import FlagEmbedding, qdrant_client, fastmcp"` 无报错。
- [ ] **R-06** 冒烟验证：`AsyncQdrantClient(url="http://127.0.0.1:6333")` 建一个临时 collection 再删除，确认读写链路通；GPU 上跑一次 bge-m3 单句编码确认显存占用正常。
    ✅ 验证：临时 collection 建删成功；编码返回 `dense_vecs` 与 `lexical_weights` 两键。

### Phase 1：数据契约与摄取管道（对应 tech.md P0）

- [ ] **R-07** 契约门禁（⚠️ 项目工程师确认）：核对 `recall/models.py` 骨架与 `tech.md` §3 字段、§8 端点完全一致后定稿（`Identity` / `ChunkPayload` / `Evidence` / `SearchResult` / `AnswerResult` / `DocRecord`）。
    ⛔ 前置门禁：**契约已定稿，后续实现已启动。**
- [ ] **R-08** 按定稿契约**原样落地** `recall/models.py`（禁止增删字段），补全 Pydantic 校验（code_standards §3）。
- [ ] **R-09** 实现 `recall/chunker.py`：两级级联切分——`#/##` 标题层级主切，节超 `MAX_CHUNK_TOKENS=800` 时递归切分兜底（`\n\n→\n→句号→空格` 优先级），overlap ≈ 100 字符，子块继承 `heading_path` + `sub_index`；导航类小节（<100 token）不合并不重切。**必须确定性**（同输入同输出）。
- [ ] **R-10** 实现 id 规则函数（code_standards §3.1）：`chunk_content_hash`（256-bit sha256 → payload.content_hash）与 `chunk_point_id`（**uuid5**，`recall://{doc_id}/{chunk_index}/{content_hash}`）。
- [ ] **R-11** 实现 `recall/registry.py`：SQLite 文档注册表，字段与 tech.md §3.2 一致（doc_id / source_type / source_uri / title / frontmatter / content_hash / updated_at / indexed_at / owner / visibility / chunk_count / error）。
- [ ] **R-12** 实现 `recall/connectors/obsidian.py`（按 code_standards §4.1 的 `Connector` 协议）：扫描 vault `.md` → 抽 frontmatter → `RawDoc`；**单文档失败记 error 并继续**，不中断 run。
- [ ] **R-13** 实现 `recall/embedder.py`：`BGEM3FlagModel('BAAI/bge-m3', use_fp16=True)`，`encode(return_dense=True, return_sparse=True)`；batch_size 16~32；模型版本从 spec 注入（code_standards §3/§14）。
- [ ] **R-14** 实现 `recall/store.py`：建 collection（名 `recall__bge-m3@v1__md`，metadata 含 embedding_model/embedding_version/chunker/created_at；dense+sparse 双向量配置）；payload index 现在就建（`doc_id`/`owner`/`visibility`/`groups` keyword、`updated_at_ts` integer）；批量写入用 `upload_points`（batch_size=64，禁止逐点 upsert）；实现孤儿清理。
- [ ] **R-15** 实现 `ingest.py` CLI：`--update` / `--rebuild` / `--collection` / `--model` / `--chunker` / `--force`；幂等三机制落地（文档 hash 跳过 → 内容寻址 upsert → 孤儿清理）；断点续传可重复执行（tech.md §5）。
- [ ] **R-16** 编写 `tests/`：切分确定性、`chunk_point_id` 确定性、幂等（同输入跑两次结果一致）、孤儿清理、registry 读写（code_standards §13 必测项）。
    ✅ 验证：`pytest` 全绿；`ruff check` + `mypy` 零错误。
- [ ] **R-17** 首次真实摄取：`python ingest.py --rebuild` 对你的 Obsidian vault 跑通。
    ✅ 验证：Qdrant 按 doc_id 查到 chunk；`chunk_count` 与 registry 一致；摄取日志无 error（tech.md P0 验收标准）。向项目工程师汇报（完成项/测试结论/遗留问题）。

### Phase 2：检索链路（对应 tech.md P1）

- [ ] **R-18** `recall/store.py` 增加混合查询：dense+sparse 双路检索（各 top_k=50）→ **RRF 融合** → 支持 payload filter（tech.md §4 顺序固定）。
- [ ] **R-19** 实现 `recall/rerank.py`：`FlagReranker('BAAI/bge-reranker-v2-m3', use_fp16=True)`，`normalize=True` 输出 0~1 分数，精排 Top-20。
- [ ] **R-20** 实现 `recall/assemble.py`：预算贪心截断（按分数取块，累计 token_count ≤ max_tokens，默认 3000）→ 同文档按 chunk_index 合并 → 组装 `SearchResult`；空结果返回空列表**不硬造**（code_standards §5）。
- [ ] **R-21** 实现 `recall/auth.py`：`get_identity` 占位（S1 硬编码 `{user:"me", groups:["owner"]}`）+ `effective_filter` 只收窄（身份范围与客户端 filter 取交集，服务端绝不信客户端参数，code_standards §7）。
- [ ] **R-22** 实现 `recall/api.py` 骨架：`GET /health` + `POST /kb/search`（Pydantic 校验 + 统一错误信封 `{"error":{"code","message"}}`，code_standards §6.1）。
- [ ] **R-23** 测试与验收：预算截断、空结果、错误路径单测全绿；**5 个真实笔记问题**经 kb_search 检索，Top-3 命中正确文档（tech.md P1 验收标准）。记录命中情况汇报项目工程师。

### Phase 3：接入层 REST + MCP + DSH（对应 tech.md P2）

- [ ] **R-24** FastMCP 挂载（code_standards §6.3）：`mcp_app = mcp.http_app(path="/")` → `FastAPI(lifespan=mcp_app.lifespan)` → `app.mount("/mcp", mcp_app)`；最终端点 `http://127.0.0.1:8000/mcp`。
    ✅ 验证：`GET /mcp`（或 MCP 握手）有响应；`/health` 正常。
- [ ] **R-25** 实现 MCP 工具：`kb_search`（只读，docstring 按"召唤词"规范写清**何时调用 + [n] 引用规则 + 忠实度要求**）、`kb_stats`（只读）；返回 Pydantic 模型（自动 outputSchema + structuredContent，code_standards §6.2）。
- [ ] **R-26** 注册进 DSH：`$DSH_HOME/mcp-servers.json` 添加 `{"serverName":"recall","transport":"streamable-http","url":"http://127.0.0.1:8000/mcp","enabled":true}`。
    ✅ 验证：DSH 新会话出现 `mcp__recall__kb_search` 等工具。
- [ ] **R-27** 编写 `skill/recall-assembly.md`（frontmatter：name `recall-assembly` + description；正文 = 组装规范：query 措辞、[n] 引用格式、忠实度、证据即数据，tech.md §9）并复制到 `~/.dsh/skills/`。
    ✅ 验证：DSH 新会话 skill 目录（available_skills）出现 `recall-assembly`。
- [ ] **R-28** 端到端验收：DSH 会话中用自然语言问笔记（如"我笔记里关于 RAG 检索质量的结论？"），回答**带 [n] 引用且忠于证据**（tech.md P2 验收标准）。记录问答样例汇报项目工程师。

### Phase 4：胖端点与评测体系（对应 tech.md P3）

- [ ] **R-29** 实现 `kb_answer`（胖端点）：内部 = kb_search + 组装 + DeepSeek 生成；**JSON 模式约束** `{"answer","citations":[n,...]}` 防引用幻觉；`references` 与 [n] 一一对应（code_standards §8）；MCP 侧同步暴露，描述显式声明副作用。
- [ ] **R-30** 创建黄金集 `eval/golden_set.jsonl` 初版 20~30 题（`{"question","expected_sources"[]}`，每数据源覆盖，版本化，code_standards §11）。
- [ ] **R-31** 实现 `eval/eval_retrieval.py`：`--collection` 参数化，输出 Recall@K / MRR；**query 必须用该 collection 的 embedding 模型编码**。
    ✅ 验证：对 `recall__bge-m3@v1__md` 跑出基线分数并记录（留档 tech.md §14）。
- [ ] **R-32** 接入 Ragas（faithfulness / answer relevancy，评测 kb_answer 回答）+ promptfoo（prompt 回归）。
    ✅ 验证：kb_answer 的 citations 与 references 一一对应；Ragas 首轮分数记录。
- [ ] **R-33** 汇报基线评测结果（黄金集分数 + Ragas 分数 + 发现的检索质量问题），供项目工程师决定是否进入调优（Phase 6 R-42）。

### Phase 5：集成验收与运维演练

- [ ] **R-34** 重灌演练：用同一管道参数化**并排**重建新 collection（模拟换 embedding 版本），旧库不删；验证重灌脚本可重复、幂等、断点续传（tech.md §5/§3.1）。
- [ ] **R-35** 全量回归：`pytest` 全绿 + `ruff` / `mypy` 零错误 + DSH 端到端复测（R-28 场景）。
- [ ] **R-36** 备份确认：vault 原文 git 备份 + Qdrant snapshot + registry SQLite 备份路径记录（tech.md §2 备份行）。
- [ ] **R-37** 项目工程师验收：按 tech.md §12 启动说明亲自跑完整流程——摄取 → 检索 → DSH 问答带引用 → 评测出分，全部符合验收标准后签字。

### Phase 6：后续迭代预留（不在首版验收范围，项目工程师另行排期）

- [ ] **R-38** watchdog 常驻增量同步（监听 vault 变化自动 `ingest --update`）。
- [ ] **R-39** Coze 接入：公网网关（Cloudflare Tunnel / 云服务器）+ API key + 限流 + 审计——权限 S2 的触发点（tech.md §7）。
- [ ] **R-40** 权限 S2：API key 中间件实现，`get_identity` 换真实实现，审计日志上线。
- [ ] **R-41** 新 Connector：飞书 / 语雀 / 网页（按 Connector 协议新增，不改管道其余部分）。
- [ ] **R-42** 检索调优 A/B：top_k / rerank / 切分参数用黄金集 + Recall@K 并排对比，数据驱动决策（tech.md §10 触发点）。

---

## 五、 问题日志（开发中按时间追加，原记录永不删除）

| 日期 | 原步骤 | 问题描述 | 新步骤 | 项目工程师指示 | 状态 |
| :--- | :--- | :--- | :--- | :--- | :--- |
| （示例）2026-09-xx | R-13 | embedder 显存溢出（fp32 + reranker 同驻 6GB） | R-13b：恒 fp16 + reranker 懒加载 | 同意，按 code_standards §10 处理 | ✅ 已解决 |

---

## 六、 当前进度快照（每步完成/受阻后更新）

- **当前阶段**：Phase 0（环境与仓库初始化）
- **当前步骤**：R-02b（本地下载 wheel 安装 PyTorch）
- **已通过项**：R-01、R-03b、R-04
- **未通过项**：R-02（网络超时）、R-03（Docker 未运行，已走 R-03b）
- **待请示事项**：无
- **最近一次测试结果**：Qdrant native 启动成功；PyTorch 安装进行中
- **本文件版本**：v0.2.0（2026-09-04 新增"变更登记铁律"§二 与 §七 变更日志；上一版 v0.1.1 为术语调整）

---

## 七、 变更日志（每一次改动按时间追加，原记录永不删除）

> 本表是"变更登记铁律"的落点：**所有 roadmap 未写明的情形、临时绕行方案、非步骤性改动都登记在此**，并在 §四 补录对应步骤（如涉及）。
> 与 §五 问题日志的分工：§五 记"**步骤受阻**"，本表记"**一切改动与未覆盖情形**"。

| 日期 | 关联步骤 | 变更类型 | 变更内容 | 备注 |
| :--- | :--- | :--- | :--- | :--- |
| 2026-09-04 | — | 规则新增 | 新增"变更登记铁律"（§二）与本节变更日志（§七）：每一次改动都必须写入本文件，roadmap 未写明的情形必须补录 | 依据项目工程师指示；本行即为该规则的首条记录 |
| 2026-09-04 | — | 术语调整 | 决策者称呼由"总设计师"统一改为"项目工程师"（全文 14 处），版本 v0.1.0 → v0.1.1 | 依据项目工程师指示，补记录 |
| 2026-09-21 | R-03 | 环境绕行 | Docker Desktop 未运行，改用 Qdrant v1.19.0 native 二进制（`tools/qdrant/qdrant.exe`） | 符合 roadmap 问题处理协议 |
| 2026-09-21 | R-02 | 环境绕行 | pip/conda 在线安装 PyTorch cu124 网络不稳定，新增 R-02b 本地下载 wheel 安装 | 符合 roadmap 问题处理协议 |
| 2026-09-21 | R-04 | 项目初始化 | 新增 `.gitignore`、`README.md`；`.gitignore` 追加 `tools/qdrant/` 二进制忽略规则 | Phase 0 |
| 2026-09-21 | R-05 | 依赖配置 | 新增 `pyproject.toml`（ruff/mypy/pytest + 依赖清单） | Phase 0，待 pip install |
