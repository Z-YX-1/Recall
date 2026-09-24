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
- [x] **R-05** 编写 `pyproject.toml`（ruff / mypy / pytest 配置按 code_standards §1：`line-length=100`、mypy strict）+ 依赖清单：`qdrant-client`、`fastapi`、`uvicorn[standard]`、`fastmcp`、`pydantic`、`FlagEmbedding`、`torch`、`openai`（DeepSeek 走 OpenAI 兼容接口）、`httpx`、`pytest`、`ruff`、`mypy`；执行 `pip install`（清华镜像）；下载模型 `BAAI/bge-m3` 与 `BAAI/bge-reranker-v2-m3`（先设置 `$env:HF_ENDPOINT="https://hf-mirror.com"`）。
    ✅ 验证：模型文件落盘（HF cache 目录可见）；`python -c "import FlagEmbedding, qdrant_client, fastmcp"` 无报错。
    ✅ 实测（2026-09-22）：`pip install -e ".[dev]" -i 清华镜像` 成功（FlagEmbedding 1.4.2 / qdrant-client 1.19.1 / fastmcp 2.14.7 / transformers 5.17.0 / pydantic 2.13.5）；HF cache 落盘 `bge-m3` 2.19GB、`bge-reranker-v2-m3` 2.3GB；`import FlagEmbedding, qdrant_client, fastmcp, fastapi, pydantic, openai, httpx, frontmatter, tiktoken` 全部无报错。
    ✅ 补充（2026-09-22）：新增 `requirements.lock`（157 项显式 pin，code_standards §14）；pyproject 追加 `python-dotenv`、`numpy`。详见 §七 变更日志。
- [x] **R-06** 冒烟验证：`AsyncQdrantClient(url="http://127.0.0.1:6333")` 建一个临时 collection 再删除，确认读写链路通；GPU 上跑一次 bge-m3 单句编码确认显存占用正常。
    ✅ 验证：临时 collection 建删成功；编码返回 `dense_vecs` 与 `lexical_weights` 两键。
    ✅ 实测（2026-09-22）：`torch 2.6.0+cu124 / cuda_available=True / RTX 4050 Laptop GPU`；临时 collection `recall__smoke-test`（dense 1024 cosine + sparse）建删成功，metadata 读回 `{"embedding_model":"bge-m3","embedding_version":"v1","chunker":"md-heading-v1","created_at":"2026-09-04"}`；`BGEM3FlagModel` 编码返回 `dense_vecs (1,1024)` + `lexical_weights`（10 terms）；显存 allocated 1093MB / reserved 1112MB（fp16，6GB 卡余量充足）。

### Phase 1：数据契约与摄取管道（对应 tech.md P0）

- [x] **R-07** 契约门禁（⚠️ 项目工程师确认）：核对 `recall/models.py` 骨架与 `tech.md` §3 字段、§8 端点完全一致后定稿（`Identity` / `ChunkPayload` / `Evidence` / `SearchResult` / `AnswerResult` / `DocRecord`）。
    ⛔ 前置门禁：**契约已定稿，后续实现已启动。**
    ✅ 实测（2026-09-22）：`recall/models.py` 落地 6 个定稿模型（字段与 tech.md §3.2/§3.3/§8 逐字一致）+ `RawDoc`（code_standards §4.1）+ `Chunk`（切分器产出，实现层新增）；id 规则函数 `chunk_content_hash` / `chunk_point_id` 同文件落地（code_standards §3.1）。契约外新增项已登记 §七。
- [x] **R-08** 按定稿契约**原样落地** `recall/models.py`（禁止增删字段），补全 Pydantic 校验（code_standards §3）。
    ✅ 验证（2026-09-22）：`tests/test_models.py` 13 项全绿——`chunk_point_id` 确定性 + uuid5 版本校验、`ChunkPayload` 12 字段 model_dump 与 tech.md §3.3 逐字一致、`updated_at_ts` 拒绝非整数、`groups` 默认值不跨实例共享、必填字段缺失抛 `ValidationError`。
- [x] **R-09** 实现 `recall/chunker.py`：两级级联切分——`#/##` 标题层级主切，节超 `MAX_CHUNK_TOKENS=800` 时递归切分兜底（`\n\n→\n→句号→空格` 优先级），overlap ≈ 100 字符，子块继承 `heading_path` + `sub_index`；导航类小节（<100 token）不合并不重切。**必须确定性**（同输入同输出）。
    ✅ 验证（2026-09-22）：`tests/test_chunker.py` 12 项全绿——同输入同输出；`#/##` 为切分点、`###` 留在节内；导航小节单块；超长节切分后 `sub_index` 连续且相邻块重叠；递归切分**内容零丢失**（`"".join(pieces) == body`）；代码围栏内的 `#` 不误判为标题。
- [x] **R-10** 实现 id 规则函数（code_standards §3.1）：`chunk_content_hash`（256-bit sha256 → payload.content_hash）与 `chunk_point_id`（**uuid5**，`recall://{doc_id}/{chunk_index}/{content_hash}`）。
    ✅ 验证（2026-09-22）：`tests/test_models.py` 覆盖——sha256 与 `hashlib` 一致；同 (doc_id, chunk_index, content_hash) 恒等；位置/文本/文档任一变化即换 id；产物为合法 UUIDv5（Qdrant PointId 合法）。
- [x] **R-11** 实现 `recall/registry.py`：SQLite 文档注册表，字段与 tech.md §3.2 一致（doc_id / source_type / source_uri / title / frontmatter / content_hash / updated_at / indexed_at / owner / visibility / chunk_count / error）。
    ✅ 验证（2026-09-22）：`tests/test_registry.py` 8 项全绿——建表幂等、读写往返、frontmatter 含 YAML 日期可序列化、`record_error` 保留已入库字段、`list_all` 有序、删除生效。SQLite 阻塞调用统一 `asyncio.to_thread` 隔离（code_standards §0.4）。
- [x] **R-12** 实现 `recall/connectors/obsidian.py`（按 code_standards §4.1 的 `Connector` 协议）：扫描 vault `.md` → 抽 frontmatter → `RawDoc`；**单文档失败记 error 并继续**，不中断 run。
    ✅ 验证（2026-09-22）：`tests/test_connectors.py` 8 项全绿——frontmatter 抽取、相对 POSIX `source_uri`、跳过 `.obsidian`/`.trash`、**单文档读取失败隔离并继续**、同名文件按来源路径短哈希确定性消歧、vault 不存在只记 error 不抛。`Connector` 协议仅 2 方法（`list`/`hash_of`），错误缓冲由 `BaseConnector` 提供，不改协议。
- [x] **R-13** 实现 `recall/embedder.py`：`BGEM3FlagModel('BAAI/bge-m3', use_fp16=True)`，`encode(return_dense=True, return_sparse=True)`；batch_size 16~32；模型版本从 spec 注入（code_standards §3/§14）。
    ✅ 验证（2026-09-22）：真实 vault 3121 块全部编码成功；`embedding_model="bge-m3"` / `embedding_version="v1"` 由构造参数注入并写入 collection metadata 与每条 payload；sparse indices 升序（Qdrant 要求）；fp16 常驻（R-06 显存实测）。
- [x] **R-14** 实现 `recall/store.py`：建 collection（名 `recall__bge-m3@v1__md`，metadata 含 embedding_model/embedding_version/chunker/created_at；dense+sparse 双向量配置）；payload index 现在就建（`doc_id`/`owner`/`visibility`/`groups` keyword、`updated_at_ts` integer）；批量写入用 `upload_points`（batch_size=64，禁止逐点 upsert）；实现孤儿清理。
    ✅ 验证（2026-09-22）：`tests/test_store.py` 6 项全绿；真实 collection 读回 `payload_schema = ['doc_id','groups','owner','updated_at_ts','visibility']`、`dense`(1024,cosine) + `sparse` 命名向量；`upload_points(batch_size=64, max_retries=3)` 实测为**同步方法**（await 会 TypeError）→ 用 `asyncio.to_thread` 隔离；同输入重跑点数不变（幂等），`delete_orphans` 只删不在 keep 集合中的点。
    ⚠️ 问题：qdrant-client 1.19.1 的 `AsyncQdrantClient.upload_points` 实为**同步**方法（返回 `None`，内部同步完成上传），按 code_standards §4.3 的字面写法 `await upload_points(...)` 会抛 `TypeError: object NoneType can't be used in 'await' expression`（2026-09-22，R-14 验证阶段实测）。
- [x] **R-14b** 改为「同步调用 + `asyncio.to_thread` 隔离」：仍严格使用 `upload_points`（batch_size=64、max_retries=3、非逐点 upsert，满足 code_standards §4.3），同时满足 §0.4「阻塞点用 `asyncio.to_thread` 隔离」。`tests/test_store.py` 6 项全绿，真实 collection 写入 3121 点。
    🧭 项目工程师指示：**待复核**（本机运行期间无第二决策人；已按 code_standards §0.4/§4.3 自行收敛，不影响 tech.md 任何契约，备查 §五/§七）
- [x] **R-15** 实现 `ingest.py` CLI：`--update` / `--rebuild` / `--collection` / `--model` / `--chunker` / `--force`；幂等三机制落地（文档 hash 跳过 → 内容寻址 upsert → 孤儿清理）；断点续传可重复执行（tech.md §5）。
    ✅ 验证（2026-09-22）：`tests/test_ingest.py` 7 项端到端全绿（真跑扫描→切分→编码→upsert→记账）；额外覆盖「解析失败 ≠ 源已删除」（失败文档不被误删旧块）；`--rebuild` 忽略 hash 跳过整篇重灌且不删旧库。
- [x] **R-16** 编写 `tests/`：切分确定性、`chunk_point_id` 确定性、幂等（同输入跑两次结果一致）、孤儿清理、registry 读写（code_standards §13 必测项）。
    ✅ 验证：`pytest` 全绿；`ruff check` + `mypy` 零错误。
    ✅ 实测（2026-09-22）：**54 passed**（`ruff check` All checks passed；`mypy` Success: no issues found in 20 source files）。
    ⚠️ 问题：本机 DSH 文件沙箱在 workspace-write 模式下拒绝写入 `mode=0o700` 创建的目录（`tempfile.mkdtemp` 与 pytest 的 basetemp/tmp_path 都用该模式），导致 14 个用 `tmp_path` 的用例在 setup 阶段 `PermissionError`（2026-09-22）。
- [x] **R-16b** 文件策略放开为 `danger-full-access` 后运行：`pytest -q` → **54 passed**；`mypy` / `ruff` 同步复跑零错误。非代码问题，代码与测试未为沙箱做任何妥协。
    🧭 项目工程师指示：**待复核**（环境策略由项目工程师在本会话中放开；测试代码保持标准 `tmp_path` 写法，未做沙箱适配）
- [x] **R-17** 首次真实摄取：`python ingest.py --rebuild` 对你的 Obsidian vault 跑通。
    ✅ 验证：Qdrant 按 doc_id 查到 chunk；`chunk_count` 与 registry 一致；摄取日志无 error（tech.md P0 验收标准）。向项目工程师汇报（完成项/测试结论/遗留问题）。
    ✅ 实测（2026-09-22）：vault `D:/黑曜石/note/大模型应用`，268 篇 → **3121 块**，耗时 172.0s，失败 0；`registry` 268 篇且 `chunk_count` 求和 = 3121 = Qdrant `points_count`（一致）；按 `doc_id` 查到该篇 41 块，payload 12 字段与 tech.md §3.3 逐字一致（`updated_at_ts` 为 int、`owner=me` / `visibility=private` / `groups=[]`）；紧接 `--update` 复跑 **扫描=268 跳过=268 入库=0 耗时 1.0s**（幂等跳过成立，退出码 0）。

### Phase 2：检索链路（对应 tech.md P1）

- [x] **R-18** `recall/store.py` 增加混合查询：dense+sparse 双路检索（各 top_k=50）→ **RRF 融合** → 支持 payload filter（tech.md §4 顺序固定）。
    ✅ 实测（2026-09-22）：`QdrantStore.hybrid_search` 用 `prefetch` 双路 + `FusionQuery(Fusion.RRF)`，`RECALL_TOP_K=50`；`tests/test_auth.py::test_client_filter_cannot_widen_visibility` 用真实 Qdrant 端到端验证「双路检索 + 服务端过滤」链路。
- [x] **R-19** 实现 `recall/rerank.py`：`FlagReranker('BAAI/bge-reranker-v2-m3', use_fp16=True)`，`normalize=True` 输出 0~1 分数，精排 Top-20。
    ✅ 实测（2026-09-22）：`tests/test_rerank.py` 6 项全绿——相关文档排第一、分数落在 0~1（`normalize=True`）、`top_n` 生效、同输入同输出、单条输入返回标量也能兼容；`DEFAULT_TOP_N=20`、`MAX_LENGTH=1024`（避免 800 token 块被静默截断）。
    ⚠️ 问题：检索链路只把**块正文**喂给精排，丢掉了 `heading_path`。R-33 基线测量显示这让精排变成**净负收益**（MRR 0.601，还不如不要精排的 0.650）——RRF 原本排第 1 的文档被压到第 2~3（2026-09-22）。
- [x] **R-19b** 新增 `rerank.build_rerank_document(heading_path, text)`：精排输入改为「标题路径 + 块正文」，检索链路统一改用它。
    ✅ 根因定位（三步取证，详见 `eval/BASELINE.md` §1）：
    ① **否证截断**——用 reranker 自己的分词器统计 972 块，`p50=485 / p90=733 / p99=889 / max=1055`，仅 **1 块（0.1%）** 超过 `MAX_LENGTH=1024`；
    ② **逐题对比**——精排相对 hybrid-only 变差 8 题（多为原第 1 名被压到 2~3）、变好 5 题（多为第 4~7 名提到 1~6）；
    ③ **证因**——同一批候选分别喂「纯正文」与「标题+正文」，**13 题变好、0 题变差**：MRR 0.591 → 0.859、Recall@1 0.467 → 0.767。原因：这些笔记的话题信号大部分在标题里（如「🎲 ai-Temperature 与确定性控制 —— 全景解析」）。
    ✅ 修复后端到端复测（30 题黄金集，v1 与 v2 各跑一遍、结果一致）：
    | 指标 | 修复前 | **修复后** |
    | :--- | ---: | ---: |
    | Recall@1 | 0.467 | **0.767** |
    | Recall@3 | 0.733 | **0.933** |
    | Recall@5 | 0.767 | **0.933** |
    | Recall@10 | 0.833 | **1.000** |
    | MRR | 0.601 | **0.860** |
    名次分布：Top-1 14→**23** 题，未命中 5→**0** 题。
    ✅ `tests/test_rerank.py` 补 2 项（拼接与空标题回落）、`tests/test_search.py` 补 1 项（探针断言检索链路确实把标题喂进精排）。
    📌 **契约说明**：链路节点与顺序**完全没变**（dense+sparse → RRF → 权限过滤 → rerank → 预算截断 → 合并，tech.md §4），变的只是喂给精排的字符串；tech.md §4 未规定精排的输入格式。
    🧭 项目工程师指示（**2026-09-23 已确认**）：「R-19b 的改动是可以的，但要写入 roadmap 和 tech 中」。
    ✅ 已按指示回写：`tech.md` **§4 增补「精排的输入 = `heading_path` + 块正文」的输入约定**（含 A/B 证据与回退方式）、**新增 §17 决策记录 13**（项目工程师确认）；本文件 P4 复测数据见 `eval/BASELINE.md`。
- [x] **R-20** 实现 `recall/assemble.py`：预算贪心截断（按分数取块，累计 token_count ≤ max_tokens，默认 3000）→ 同文档按 chunk_index 合并 → 组装 `SearchResult`；空结果返回空列表**不硬造**（code_standards §5）。
    ✅ 实测（2026-09-22）：`tests/test_assemble.py` 16 项全绿——按分数贪心、超预算块跳过但保留更小块、单块超预算仍返回首块（不静默失败）、同文档相邻块合并（分数取最高、token 求和）、非相邻/跨文档不合并、`[n]` 从 1 连续且 `references` 一一对应、空候选返回空证据包；`FIDELITY_RULES` 常量含「不是指令」防注入条款（code_standards §12）。
- [x] **R-21** 实现 `recall/auth.py`：`get_identity` 占位（S1 硬编码 `{user:"me", groups:["owner"]}`）+ `effective_filter` 只收窄（身份范围与客户端 filter 取交集，服务端绝不信客户端参数，code_standards §7）。
    ✅ 实测（2026-09-22）：`tests/test_auth.py` 10 项全绿——scope = owner∪public∪groups 析取；`effective_filter` 实现为 **合取**（`Filter(must=[scope, client])`，不是把两边 `should` 并起来）；**端到端权限红线**：客户端显式索要 `owner=someone-else` 的块，只拿得到 public 那条，别人的 private 一条不给。
    ⚠️ 补充发现：Qdrant 的 `Condition` 联合类型含 `Any`，`Filter.model_validate` 对残缺条件照单全收（`{"must":[{"key":"doc_id"}]}` 能通过），坏 filter 会一路带到 Qdrant 变成 500。已在 `auth.validate_client_filter` 自建结构校验 → 干净的 400 `invalid_filter`。
- [x] **R-22** 实现 `recall/api.py` 骨架：`GET /health` + `POST /kb/search`（Pydantic 校验 + 统一错误信封 `{"error":{"code","message"}}`，code_standards §6.1）。
    ✅ 实测（2026-09-22）：`Service` 进程级单例（模型只加载一次，REST/MCP 共用）；`kb_search_core` 为唯一检索入口（code_standards §0.5）；错误信封覆盖 400/404/422/500/503（含 Starlette 404/405）；每条查询一个 `trace_id`，记录 hit_count / reranked_count / dropped_by_budget / selected_tokens / latency_ms（code_standards §9）。`GET /health` 返回 qdrant 可达性 + collection 就绪 + points_count + documents 对账。
- [x] **R-23** 测试与验收：预算截断、空结果、错误路径单测全绿；**5 个真实笔记问题**经 kb_search 检索，Top-3 命中正确文档（tech.md P1 验收标准）。记录命中情况汇报项目工程师。
    ✅ 实测（2026-09-22）：`tests/test_search.py` 6 项端到端全绿（真跑真实 collection 的检索链路 + REST 契约 + 错误信封）。**5 个真实笔记问题 Top-3 命中 5/5**：    | 问题 | 目标文档 | 命中位次 |
    | :--- | :--- | :--- |
    | Embedding 向量的几何意义是什么？ | `Embedding (向量) 的几何意义.md` | Top-2 |
    | Function Calling 的原理是什么？ | `ai-Function Calling (函数调用) 原理.md` | **Top-1** |
    | 怎么让大模型稳定输出 JSON？ | `ai-JSON 模式与结构化输出.md` | Top-2 |
    | Temperature 怎么影响输出的确定性？ | `ai-Temperature 与确定性控制.md` | **Top-1** |
    | Token 是怎么计费和拆分的？ | `ai-Token (词元) 的计费与拆分机制.md` | **Top-1** |
    ⚠️ 问题：pytest 会话中连续多次装载 bge-m3 触发原生层崩溃——`Windows fatal exception: access violation`，栈在 `transformers/core_model_loading.py::_materialize_copy`（transforms 5.x 权重装载使用内部线程池并行 materialize，两个线程同时装载模型即崩；2026-09-22）。
- [x] **R-23b** 新增 `recall/model_cache.py`：**进程级模型缓存 + 装载锁**（`load_once`），embedder / reranker 统一改为按 `(模型名, fp16, 设备)` 复用同一实例。既消除原生层崩溃，也满足 tech.md §14 显存纪律（6GB 卡容不下多份副本）。
    ✅ 验证：`pytest -q` **91 passed**（崩溃消失，且套件耗时从 88s 降到 72s）；真实检索链路连续跑 5 个问题稳定。
    🧭 项目工程师指示：**待复核**（新增模块，属实现层基础设施，未触及 tech.md 契约；登记 §七）
- [x] **R-23c** Obsidian Connector 增加**工程产物目录跳过**：真实 vault 里混着 `project/*/node_modules/**`，把第三方 CHANGELOG / LICENSE / README 灌进索引。
    ⚠️ 问题：首轮真实摄取 268 篇里 203 篇是 node_modules 产物（`CHANGELOG.md`/`LICENSE.md`/`readme.md`），严重稀释检索质量（2026-09-22 实测）。
    ✅ 处理：`DEFAULT_SKIP_DIRS` 增加 `node_modules`/`dist`/`build`/`.venv`/`venv`/`__pycache__`，并开放 `--skip-dirs` 覆盖；重跑 `--update` → 扫描 65 篇、**文档删除 203**、孤儿清理生效，collection 从 3121 点降到 972 点。
    🧭 项目工程师指示：**待复核**（属摄取范围细化，未改 Connector 协议与 tech.md 契约）

### Phase 3：接入层 REST + MCP + DSH（对应 tech.md P2）

- [x] **R-24** FastMCP 挂载（code_standards §6.3）：`mcp_app = mcp.http_app(path="/")` → `FastAPI(lifespan=mcp_app.lifespan)` → `app.mount("/mcp", mcp_app)`；最终端点 `http://127.0.0.1:8000/mcp`。
    ✅ 验证：`GET /mcp`（或 MCP 握手）有响应；`/health` 正常。
    ✅ 实测（2026-09-22）：`uvicorn recall.api:app --host 127.0.0.1 --port 8000` 启动；`GET /health` → `{"status":"ok","qdrant":true,"collection":"recall__bge-m3@v1__md","collection_ready":true,"points_count":972,"documents":65}`；真实 MCP 客户端（`fastmcp.Client("http://127.0.0.1:8000/mcp")`）握手成功。⚠️ lifespan 组合方式：`mcp_app.lifespan` 挂在 FastAPI lifespan 内（子应用 mount 不会自动跑生命周期）。
- [x] **R-25** 实现 MCP 工具：`kb_search`（只读，docstring 按"召唤词"规范写清**何时调用 + [n] 引用规则 + 忠实度要求**）、`kb_stats`（只读）；返回 Pydantic 模型（自动 outputSchema + structuredContent，code_standards §6.2）。
    ✅ 实测（2026-09-22）：`tests/test_mcp.py` 5 项全绿（含进程内 ASGI 生命周期下的真实握手）。工具列表 `['kb_search','kb_stats']`；`kb_search` outputSchema = `{evidence, references}`，`kb_stats` outputSchema 含 10 个字段；docstring 含「何时调用」「[n]」「只依据证据回答」；工具内异常转可读文本（空 query → `参数不合法：query: String should have at least 1 character`，`is_error=True` 且无 traceback）。
- [x] **R-26** 注册进 DSH：`$DSH_HOME/mcp-servers.json` 添加 `{"serverName":"recall","transport":"streamable-http","url":"http://127.0.0.1:8000/mcp","enabled":true}`。
    ✅ 验证：DSH 新会话出现 `mcp__recall__kb_search` 等工具。
    ✅ 实测（2026-09-22）：`$DSH_HOME` 实为 `D:\dsh-data`（非 `~/.dsh`），已写入 `D:\dsh-data\mcp-servers.json`，Context7 条目保留。**待项目工程师在新会话确认工具出现**（MCP 服务器在会话启动时装载，本会话注册前已启动，看不到 `mcp__recall__*`）。
- [x] **R-27** 编写 `skill/recall-assembly.md`（frontmatter：name `recall-assembly` + description；正文 = 组装规范：query 措辞、[n] 引用格式、忠实度、证据即数据，tech.md §9）并复制到 `~/.dsh/skills/`。
    ✅ 验证：DSH 新会话 skill 目录（available_skills）出现 `recall-assembly`。
    ✅ 实测（2026-09-22）：仓库 `skill/recall-assembly.md` 写好（3527 字节）并复制到 `D:\dsh-data\skills\recall-assembly.md`；**本会话 skill 目录已实时出现 `recall-assembly`**（文件系统 watcher 生效），且 `skill` 工具加载成功、正文完整。
- [x] **R-27c** 补齐 tech.md §9 的**第三杠杆**：工作区兜底文件 `ASSEMBLY.md`。
    ⚠️ 问题：tech.md §9 列了三条 DSH 集成杠杆（① 工具契约 ② skill 指令 ③ 工作区文件 `Recall/ASSEMBLY.md` 兜底），但 §四 步骤里只覆盖了前两条，第三条属"roadmap 未覆盖的情形"（2026-09-22 自查发现）。
    ✅ 处理：按登记铁律先补录本步骤再执行——新增仓库根 `ASSEMBLY.md`（skill 的兜底副本：分工表 / 何时调用 / [n] 引用格式 / 忠实度 / 证据即数据 / 胖端点例外），明确"冲突以 spec/ 为准"。
    🧭 项目工程师指示：**待复核**（新增文档，未触及契约）
- [x] **R-27d** 补齐 tech.md §11 的"日志"落点：结构化日志落盘 `data/logs/`。
    ⚠️ 问题：`Settings.log_dir` 早已存在但**无人使用**（悬空配置）；tech.md §11 明确 `data/` 含日志，tech.md §2 可观测行要求结构化日志（2026-09-22 自查发现）。
    ✅ 处理：`recall/config.py` 新增 `configure_logging(settings, level, component)`——控制台 + `RotatingFileHandler`（5MB × 3 份，UTF-8），并把 stdout/stderr 切 UTF-8；`ingest.py` 与 `api.py` 统一改用它；测试用 `RECALL_LOG_TO_FILE=0` 关闭以免污染 `data/`。
    ✅ 验证：`python ingest.py --update` 后 `data/logs/ingest.log` 落盘（`ingest.finished` 一行）。
    🧭 项目工程师指示：**待复核**
- [x] **R-27e** 补齐 code_standards §15 的两条硬性要求（自查发现）。
    ⚠️ 问题：① §15 要求"公共协议必须有示例"，`Connector` 协议只有签名没有用法示例；② §15 要求"任何影响架构的决策回写 spec 并在 tech.md §15 决策记录追加条目"，而实现期已产生 4 项此类决策（模型缓存/装载锁、`--rebuild` 可续跑、摄取排除工程产物、MCP 工具名与 REST 函数改名）却未回写（2026-09-22）。
    ✅ 处理：`Connector` 补 Google 风格 `Example:`（完整可抄的最小来源实现）；`tech.md` 新增 **§16 决策记录 9~12**。
- [x] **R-27f** 清理悬空配置 + 让 `host`/`port` 真正生效（自查发现）。
    ⚠️ 问题：`Settings.ingest_workers` 声明并读环境变量但**全项目无人使用**；`Settings.host` / `port` 同样无人使用（README 让用户手抄 uvicorn 参数）；`hf_endpoint` 只是"顺带"被 `load_dotenv` 写进环境变量，本身没有被程序化应用（2026-09-22）。
    ✅ 处理：① 删除 `ingest_workers`（无用配置比没有配置更危险）；② `recall/api.py` 新增 `main()` + `if __name__ == "__main__"`，`python -m recall.api` 按 `Settings.host/port` 启动，README 同步更新；③ `Settings.from_env()` 显式 `os.environ.setdefault("HF_ENDPOINT", …)`，保证镜像在任何模型加载前生效。
    ✅ 验证：`python -m recall.api` 启动成功——`/health` 200（972 点 / 65 篇）、MCP 四个工具可列；`data/logs/api.log` 落盘；`Settings` 无 `ingest_workers` 属性、`HF_ENDPOINT` 环境变量被正确设置。
    🧭 项目工程师指示：**待复核**
- [x] **R-27g** 清理三处"有文档、没人用"的死代码（自查发现）。
    ⚠️ 问题：`QdrantStore.list_collections` 的 docstring 写着"``kb_stats`` 使用"但 `kb_stats` 从未调用；`Embedder.dimension` 写着"建 collection 用"但 `ensure_collection` 用的是常量 `DENSE_DIM`；`model_cache.cached_models()` / `clear_cache()` 写着"诊断 / 测试用"但从无调用者（2026-09-22）。
    ✅ 处理（一律"接上"而不是"删掉"，因为它们各自都有真实用途）：① `kb_stats` 返回新增 `collections` 字段（A/B 时能看到新旧库并排，docstring 成真）；② `ingest.py` 建库时传 `dense_dim=embedder.dimension`（collection 维度跟着 embedder 走，而不是跟着常量）；③ 新增 `tests/test_model_cache.py` 4 项，实测缓存复用与装载锁——**顺带抓出一个真实 bug**：`cached_models()` 用裸 `sorted()` 排序含 `None`/`"cpu"` 的键会抛 `TypeError`，已改为 None 安全排序。
    ✅ 验证：`pytest` **113 passed**（新增 4 项）；ruff/mypy 零错误。
    🧭 项目工程师指示：**待复核**
- [x] **R-27h** 补齐三块**此前完全没有测试覆盖**的代码（自查发现）。
    ⚠️ 问题：① `recall/llm.py` 的重试 / 退避 / 返回体解析**零覆盖**，而它是"生成失败"类问题的唯一防线；② `kb_answer` 的 LLM 失败路径（502 `llm_failed`）未测；③ 所有 Qdrant 调用都走的 `store.with_retry` 重试原语、`store.ping()` 降级路径、以及"忘了先跑 ingest"时最常撞到的 `collection_not_found` (503) 全部未测（2026-09-22）。
    ✅ 处理：新增 `tests/test_llm.py`（11 项，用假 OpenAI 客户端测重试次数/JSON 模式入参/非法 JSON 不重试/空内容/未配 key）；`tests/test_answer.py` 补 502 路径；`tests/test_store.py` 补 `ping()` 不可达与 `with_retry` 成功/耗尽；`tests/test_search.py` 补缺少 collection 的 503 路径（`kb_search` 与 `kb_answer` 各一条）。
    ✅ 顺带改进：`recall/llm.py` 抽出常量 `RETRY_BASE_DELAY`（退避基数可被测试注入，生产默认 0.5s 不变）。
    ✅ 验证：`pytest` **129 passed**（本轮 +16 项）；ruff/mypy 零错误。
    🧭 项目工程师指示：**待复核**
- [x] **R-27i** Qdrant 不可达时返回语义化 503 而不是带堆栈的 500（**由实战日志发现**）。
    ⚠️ 问题：2026-09-23 17:40 的运行日志里，Qdrant 未启动时 `POST /kb/search` 返回 **500 Internal Server Error**，日志打出十几层 `httpcore.ConnectError` 堆栈，错误码是笼统的 `internal_error`。违反 code_standards §6.1「语义化 HTTP 状态码」——对 DSH agent 而言，它拿到的是 `internal_error: ResponseHandlingException: All connection attempts failed`，而不是「Qdrant 没启动，去开 qdrant.exe」。
    ✅ 处理：`recall/store.py` 新增 `StoreUnavailableError`——`with_retry` 重试耗尽后沿 `__cause__`/`__context__` 判断异常链里是否有连接/超时类错误（qdrant-client 会把 `httpx.ConnectError` 包一层 `ResponseHandlingException`，只看最外层类型判断不出来），是则转成该异常；`recall/api.py` 在 `kb_search_core` 的两个 Qdrant 调用点转成 `ApiError("qdrant_unavailable", …, 503)`，并额外注册全局异常处理器兜住 `/kb/ingest` 等路径。错误信息带可操作提示（"请确认 tools/qdrant/qdrant.exe 已启动"）。
    ✅ 测试：`tests/test_store.py::test_unreachable_qdrant_raises_store_unavailable`、`tests/test_search.py::test_kb_search_reports_qdrant_down_as_semantic_503`（把服务指向死端口，断言 503 + `qdrant_unavailable` + 提示文案）。
    ✅ 验证：`pytest` **134 passed**；ruff/mypy 零错误。
    🧭 项目工程师指示：**待复核**
- [x] **R-28** 端到端验收：DSH 会话中用自然语言问笔记（如"我笔记里关于 RAG 检索质量的结论？"），回答**带 [n] 引用且忠于证据**（tech.md P2 验收标准）。记录问答样例汇报项目工程师。
    ⚠️ 问题：AI 执行者**无法自行开启一个 DSH 会话**（MCP 服务器只在会话启动时装载，本会话看不到 `mcp__recall__*` 工具），R-28 的字面验收必须由项目工程师在新会话中确认（2026-09-22）。
    📌 追加（2026-09-23，来自实战日志）：项目工程师实测期间访问了 `GET /kb/stats` → **404**。按 tech.md §8，`kb_stats` 只作为 **MCP 工具**存在，REST 只有 `/health`、`/kb/search`、`/kb/answer`、`/kb/ingest` 四个端点，所以 404 是符合契约的行为。**若希望 REST 也提供统计端点，属契约新增，需项目工程师确认后再加。**
    ✅ **验收通过（2026-09-24，项目工程师确认）**：原文 ——「R-28 的我已经确认完毕，不管是打开新会话还是开一个新的工作区问问题，都较好地完成了。」即 `mcp__recall__*` 工具在会话中可用、问答链路端到端成立（tech.md P2 验收标准达成）。
    📌 遗留（不阻塞 R-28）：`GET /kb/stats` → 404 是否要补一个 REST 统计端点，仍待项目工程师决定（见 §六 待请示事项）。
- [x] **R-28b** 以等价方式完成 R-28 的**除"会话装载"外的全部链路验证**：用真实 MCP 客户端连 `http://127.0.0.1:8000/mcp` 调 `kb_search`，按 `recall-assembly` 规范组装。
    ✅ 实测（2026-09-22）：问题「我笔记里关于 RAG 检索质量的结论？」→ 返回 5 条证据、`references` 与 `[n]` 一一对应，Top-1 = `project/Recall/spec/roadmap.md`（0.9348）、Top-2 = `AI/ai-文本切分器 (Text Splitter).md`（0.5731，"粒度是整条 RAG 链路里影响 recall 最大的单一参数"）、Top-3 = `AI/ai-Embedding (向量) 的几何意义.md`（0.4550，"文档实际用的是 Recall@K"）。
    ⚠️ 检索质量观察：Top-1 命中的是 vault 里的 **Recall 路线图自身**（含大量 RAG/检索字样）而非技术笔记；`query="我笔记里关于 RAG 检索质量的结论？"` 属"元问题"，与笔记正文的措辞分布不匹配。留待 R-42 用黄金集量化（tech.md §10 触发点）。
    🧭 项目工程师指示：**待确认**——请在新会话中问一句笔记问题，确认回答带 `[n]` 引用且忠于证据

### Phase 4：胖端点与评测体系（对应 tech.md P3）

- [x] **R-29** 实现 `kb_answer`（胖端点）：内部 = kb_search + 组装 + DeepSeek 生成；**JSON 模式约束** `{"answer","citations":[n,...]}` 防引用幻觉；`references` 与 [n] 一一对应（code_standards §8）；MCP 侧同步暴露，描述显式声明副作用。
    ✅ 实测（2026-09-22）：新增 `recall/llm.py`（DeepSeek OpenAI 兼容 + JSON 模式 + 指数退避重试，密钥不入日志）；`assemble()` 纯函数产出 `(prompt, ref_map)`（code_standards §8 签名）；`kb_answer_core` = kb_search → 组装 → 生成 → 引用清洗；REST `POST /kb/answer` + MCP 工具 `kb_answer`（描述显式声明"会把证据发到 DeepSeek"）。
    ✅ `tests/test_answer.py` 11 项全绿（LLM 用注入桩，其余链路真跑）：prompt 含规则/编号证据/JSON 契约；预算生效；**无证据时不调 LLM 且返回"没检索到"**（禁止硬答）；越界/重复/非数字引用被剔除；未配 key → 503 `llm_not_configured` + 统一错误信封；MCP 四个工具都显式声明副作用。
    ✅ **真实 DeepSeek 调用验证通过（2026-09-23，项目工程师填入 key 后）**：3 个真实问题全部返回结构化回答，`citations` 全部合法、正文角标齐全、无越界编号（引用一致性 3/3）。
    ⚠️ 问题：真实调用暴露 4 个"整段回答作废"级缺陷（英文双引号截断 JSON / 复述证据致输出截断 / 过度拒答 / JSON 前后夹带文字）——详见 R-29b。
- [x] **R-29b** 修复胖端点的 4 个生成健壮性缺陷（全部由真实调用暴露）。
    | # | 现象 | 根因 | 修复 |
    | :--- | :--- | :--- | :--- |
    | 1 | `LlmError: 返回体不是合法 JSON`，整段作废 | 模型在 JSON 字符串里塞**未转义的英文双引号**（原文 `把不可计算的"意思"变成"坐标"`）⇒ `Expecting ',' delimiter` | 系统提示词硬性规则：answer 内禁用英文双引号，需要时用「」 |
    | 2 | 同上（另一形态） | 模型**复述整段证据** ⇒ 输出被 `max_tokens` 截断 ⇒ JSON 不闭合 | 长度规则搬进系统提示词（≤300 字 + 写完立即闭合）；上限 2048 → 3072 |
    | 3 | 证据明明相关却答"没有相关内容" | 只写"证据不足就拒答"会诱发**过度拒答** | 忠实度规则第 2 条改**双向约束**（相关必须答；确实不涉及才拒答） |
    | 4 | 模型在 JSON 前后夹带说明文字 / 字符串含裸换行 | 直接 `json.loads` 太严 | 新增 `llm.parse_json_object()`：`strict=False` + 扫描首个配对完整的 `{...}`；**只救完整对象**（截断的半截 JSON 仍判失败） |
    ✅ 验证：3 个真实问题连续复跑全部解析成功；`tests/test_llm.py` 新增 7 项覆盖容错解析与报错可诊断性。
    🧭 项目工程师指示：**待复核**
- [x] **R-30** 创建黄金集 `eval/golden_set.jsonl` 初版 20~30 题（`{"question","expected_sources"[]}`，每数据源覆盖，版本化，code_standards §11）。
    ✅ 实测（2026-09-22）：**30 题**，字段严格为 `question` + `expected_sources`；覆盖 17 篇 AI 技术笔记、2 篇 MOC、3 篇 Recall 自身 spec、8 篇 bamboo-old/项目文档；答案来源取自真实 registry 的 `source_uri`（逐条核对存在）。
- [x] **R-31** 实现 `eval/eval_retrieval.py`：`--collection` 参数化，输出 Recall@K / MRR；**query 必须用该 collection 的 embedding 模型编码**。
    ✅ 验证：对 `recall__bge-m3@v1__md` 跑出基线分数并记录（留档 tech.md §14）。
    ✅ 实测（2026-09-22，`--k 1,3,5,10`，30 题，105.3s）：
    | 指标 | Recall@1 | Recall@3 | Recall@5 | Recall@10 | MRR |
    | :--- | :--- | :--- | :--- | :--- | :--- |
    | hybrid + rerank（默认，即生产链路） | 0.467 | **0.733** | 0.767 | 0.833 | 0.601 |
    逐题明细落盘 `eval/baseline_v1.json`（含每题 rank / 实得 Top-3），改动后可 A/B 对比。
    未命中 5 题集中在 `project/bamboo-old/spec/*`（同目录下多篇高度相似的教练类文档互相挤占）；留待 R-42 用黄金集量化调优。另提供 `--no-rerank` 以分阶段对比"混合召回 vs 召回+精排"。
- [ ] **R-32** 接入 Ragas（faithfulness / answer relevancy，评测 kb_answer 回答）+ promptfoo（prompt 回归）。
    ✅ 验证：kb_answer 的 citations 与 references 一一对应；Ragas 首轮分数记录。
    🔧 已就绪（2026-09-22）：`eval/eval_ragas.py` 写好——真跑 kb_answer 收集回答与证据 → `check_citation_consistency` 校验 `citations`↔`references` ↔ 正文角标 → Ragas `aevaluate`（DeepSeek 当裁判 + **本地 bge-m3** 当 embedding，符合"内容不出域"）；`eval/promptfoo/promptfooconfig.yaml` 写好（引用格式 / 忠实度 / 无证据不硬答三类断言）。
    ✅ **已完成（2026-09-23）**：
    | 项 | 结果 |
    | :--- | :--- |
    | 引用一致性 | **1.000**（10/10 题：citations 全部落在 references 范围内且正文都有角标） |
    | Ragas faithfulness | **0.705** |
    | Ragas answer_relevancy | **0.678** |
    | promptfoo 回归（并发 4） | **3 通过 / 1 失败 / 0 错误**，26s |
    ⚠️ 说明：① 单次 Ragas 分数有明显**裁判方差**（同实现两次 faithfulness 得 0.867 / 0.705），趋势对比需多次取均值；② promptfoo 唯一失败项正是「问笔记外主题时未拒答」——已作为**待决策缺口**记录在 `eval/BASELINE.md` §5 与下方 R-32c；③ 该次 promptfoo 运行同时充当**并发回归**验证（并发 4 下 0 错误）。
    ⚠️ 问题：`answer_relevancy` 首次运行直接缺席——ragas 有两套 embedding 接口（`text/texts` 与 `query/documents`），适配器只实现了前者 ⇒ `AttributeError: no attribute 'embed_query'`；已两套都实现。
    ⚠️ 问题：promptfoo 并发 4 触发 `/kb/answer` **500**——见 R-32c。
- [x] **R-32c** 修复"共享模型被并发踩坏"（**严重**，由 promptfoo 并发暴露）。
    ⚠️ 问题：`RuntimeError: expected scalar type Float but found Half`，栈在 `FlagEmbedding/inference/reranker/encoder_only/base.py:115 if self.use_fp16: self.model.half()`。
    根因：`FlagReranker.compute_score` **每次调用都会改模型本身**，而 R-23b 引入的模型缓存让多个请求**共享同一个实例** ⇒ 并发时一边变形一边推理。**单线程调用从未复现**，所以此前所有验证都是绿的。
    ✅ 处理：`recall/model_cache.py` 新增 `inference_lock()`（`threading.Lock`，进程内唯一、跨事件循环安全），`Embedder._encode_sync` 与 `Reranker._rerank_sync` 的推理段串行化。单卡上并发推理本来也没有收益。
    ✅ 验证：并发 **6** 个 `/kb/answer` 全部 200（修复前并发即 500）；promptfoo 并发 4 复跑 **0 错误**；新增 2 项单测（锁唯一性 + 临界区不重叠）。
    🧭 项目工程师指示：**待复核**
- [x] **R-32d** 记录待决策的忠实度缺口（**未改实现**）。
    ⚠️ 问题：问**笔记里完全没有的主题**时，检索仍返回 top-K 条"最不相关"的证据，模型拿它们硬答（实测：问 2026 年诺贝尔物理学奖，答了一大段"联网搜索原理"），而不是回答"笔记里没有相关内容"。**citation 一致性检查看不出来**——它内部自洽。
    根因：**混合检索没有相关性下限**，所以 code_standards §5 的"空结果不硬答"条款永远不会触发。tech.md §2 之所以要求 rerank `normalize=True`（"跨查询不可直接比"），本意正是为了能跨查询设阈值，但目前没有用上。
    ✅ 已记录：`eval/BASELINE.md` §5 与 promptfoo 的"无证据不硬答"断言都留了这条；候选修法 = 给证据加 rerank 分数阈值，需先用黄金集量出"真问题 vs 无关问题"的分数分布再定阈值。
    🧭 项目工程师指示：**待确认**——属 R-42 调优范围，未获确认前不改检索实现。
    ✅ **已定案（2026-09-24，项目工程师指示）**：改走**回答模板**路线，**不加检索阈值**。项目工程师实测后确认「这种回答我认为还是可以的」，并给出模板要求：
    > 主要就是要在问到笔记中没有的东西的时候**不要硬答**，而是**通过联网搜索去找答案**，并且也要**回答笔记里没有出现相关内容**，这样才是一个好的回答模板。
    ✅ 已落实（agent 侧）：
    - `skill/recall-assembly.md` 新增「**笔记里没有的内容：三段式模板**」——① 先直接回答问题本身（可联网搜索，并说明信息来自联网）；② 显式声明"我按规范检索了你的个人知识库，没有检索到任何关于 X 的内容"；③ 披露命中的相邻主题，说明"这些不是答案、只是相邻内容"并照常标 `[n]`。强调**顺序不能颠倒**、相邻主题的 `[n]` 是"透明披露"而非"答案的证据"。
    - `ASSEMBLY.md`（工作区兜底副本）同步同一模板。
    - 已重新同步到 `$DSH_HOME/skills/recall-assembly.md`（5176 字节）。
    ⚠️ **胖端点（`kb_answer`）试改后回退，附实测证据**：给 `FIDELITY_RULES` 加"若只是相邻主题必须点名、不得当答案"后，**过度拒答复发**——问"文本切分器的粒度该怎么选？"（笔记里明确写过，且上一版能正确作答）被答成"笔记里没有相关内容，证据只涉及相邻主题"。回退后恢复正常。
    📌 **结论（给 R-42 的新证据）**：**"相关 vs 相邻"这个判断光靠提示词稳不住**，需要在检索侧给信号（如把 rerank 分数作为提示词里的字段，或加分数下限）。但 fat endpoint 是次要路径（tech.md §6 默认由 agent 组装），且 agent 侧有联网搜索 + 更强推理，模板路线在 agent 侧已被验证有效 ⇒ **暂不动检索实现**，把"给胖端点加分数信号"登记为 R-42 候选。
    📌 另记：项目工程师实测时还遇到 DSH 联网搜索首次 `fetch failed`——那是 **DSH 的 web search 插件**端点配置问题，与 Recall 无关（不在本项目范围内）。
- [x] **R-32b** 依赖收敛：`pyproject.toml` 增加 `eval` 可选依赖组并**钉死 `langchain-community>=0.3,<0.4`**；同时把 `openai` 约束由 `<2` 放宽到 `<4`（ragas 依赖链装上了 openai 3.3.0，经核验 `AsyncOpenAI(api_key=...)/chat.completions.create(response_format=..., max_tokens=...)` 在 3.3.0 下仍可用）。
    ✅ 验证：`import ragas` 成功；`recall.api` / `recall.llm` 导入正常；`pytest` 全绿（见下）。
    🧭 项目工程师指示：**待复核**（依赖约束调整，属 R-32 的前置修复）
- [ ] **R-33** 汇报基线评测结果（黄金集分数 + Ragas 分数 + 发现的检索质量问题），供项目工程师决定是否进入调优（Phase 6 R-42）。
    ✅ 已完成（检索半，2026-09-22）：新增 `eval/BASELINE.md`——指标表 / 名次分布 / **分类命中率** / 5 题未命中明细 / 质量观察 / R-42 调优候选（按预期收益排序）。
    📌 关键结论：**问题不在 embedding，在语料结构**——AI 技术笔记命中率 0.95、Recall spec 1.00，而 `project/bamboo-old/spec/` 集群只有 0.50（十几篇同主题文档词汇高度重叠、互相挤占 Top-K），4/5 的未命中都出自该集群。
    📌 **第二结论（2026-09-22，已闭环）**：`--no-rerank` 分解实验发现精排曾是**净负收益**（MRR 0.601 vs hybrid-only 0.650）。按"只做 A/B 取证、不改链路顺序"的原则逐步定位，**根因是检索链路只把块正文喂给精排、丢掉了 `heading_path`**（见 R-19b）。修复后 **MRR 0.601 → 0.860、Recall@1 0.467 → 0.767、Recall@10 0.833 → 1.000、未命中 5 → 0 题**，且明显优于"不要精排"的 0.650 ⇒ 精排是净收益，前提是喂对输入。
    📌 **契约边界说明**：链路节点与顺序**完全没变**（tech.md §4），只改了喂给精排的字符串，故按实现修正处理并登记 §七；若项目工程师认为这仍属契约范围，回退只需改 `build_rerank_document` 一行。
    ✅ 顺带验证：`eval_retrieval.py --no-rerank` 这条此前从未执行过的分支已跑通；`eval_ragas.py` 在无 key 时**干净退出**（依赖链导入全通过，链路已验到 key 边界）。
    ⏳ **Ragas 半待补**：`eval/eval_ragas.py` 已就绪，等 `.env` 里的 `DEEPSEEK_API_KEY`；跑完把 `faithfulness` / `answer_relevancy` 与引用一致性补进 `eval/BASELINE.md` §6。
    ✅ **已完成（2026-09-23，两半齐了）**：`eval/BASELINE.md` 已含检索指标（R-19b 修复后 Recall@1=0.767 / @3=0.933 / **@10=1.000** / MRR=0.860）、Ragas 分数（faithfulness 0.705 / answer_relevancy 0.678 / 引用一致性 1.000）、promptfoo 结果（3 通过 / 1 失败 / 0 错误）、5 个已修缺陷、1 个待决策缺口、R-42 调优候选（按预期收益排序）。
    📌 本轮新增结论：**修复精排输入后 Recall@10 达 1.000**（30 题全部命中前 10）；**胖端点在并发下曾整段 500**，已修（R-32c）；**检索无相关性下限导致笔记外问题被硬答**，待决策（R-32d）。

### Phase 5：集成验收与运维演练

- [x] **R-34** 重灌演练：用同一管道参数化**并排**重建新 collection（模拟换 embedding 版本），旧库不删；验证重灌脚本可重复、幂等、断点续传（tech.md §5/§3.1）。
    ✅ 实测（2026-09-22）：
    | 动作 | 结果 |
    | :--- | :--- |
    | `ingest.py --rebuild --collection recall__bge-m3@v2__md --model bge-m3@v2` | 扫描 65 / 入库 58 篇 / **972 块** / 失败 0 / 65.1s |
    | 旧库 `recall__bge-m3@v1__md` | **972 点，metadata 仍为 v1**（旧库不删，供评测与回滚） |
    | 新库 `recall__bge-m3@v2__md` | 972 点，metadata `embedding_version=v2` |
    | 重跑同一条 `--rebuild`（断点续传演练） | **扫描 65 / 跳过 65 / 入库 0 / 3.0s** —— 幂等且可续跑 |
    | A/B 检索指标（`--no-rerank` 之外同参数） | v1 与 v2 **完全一致**：Recall@1=0.467 / @3=0.733 / @5=0.767 / @10=0.833 / MRR=0.601 ⇒ 重灌忠实（同模型同切分器） |
    ✅ 顺带修复：`--rebuild` 原先等于"无条件重灌"，中断后只能从头再来；改为「忽略账本快路径，但目标库已与本次切分结果一致时跳过」⇒ **真正可断点续传**；要无条件重灌用 `--force`。`tests/test_ingest.py` 新增 3 项覆盖（新库并排 / 续跑 / force）。
- [x] **R-35** 全量回归：`pytest` 全绿 + `ruff` / `mypy` 零错误 + DSH 端到端复测（R-28 场景）。
    ✅ 实测（2026-09-22）：`pytest` **109 passed**；`ruff check` 零告警；`mypy` strict 35 文件（recall + ingest + eval + tests）零错误；`/health` ok（v1 972 点 / 65 篇）。
    ⏳ DSH 端到端复测（R-28 场景）仍需项目工程师在新会话确认。
- [x] **R-36** 备份确认：vault 原文 git 备份 + Qdrant snapshot + registry SQLite 备份路径记录（tech.md §2 备份行）。
    ✅ 实测（2026-09-22）：
    | 备份对象 | 位置 | 说明 |
    | :--- | :--- | :--- |
    | vault 原文（第一备份） | 用户自有 Obsidian 仓库（git） | 摄取只读，不写回 vault |
    | registry SQLite | `data/registry.db` | 65 条文档账本；**恢复路径 = 重灌脚本** |
    | Qdrant 向量快照 | `tools/qdrant/snapshots/` | **已实际产出**：`recall__bge-m3@v1__md-…-05-15-21.snapshot`（882MB）、`recall__bge-m3@v2__md-…-05-15-34.snapshot`（881.9MB），各带 `.checksum` |
    | 恢复演练 | `python ingest.py --rebuild` 即可从 vault 原文重建全部向量 | 见 R-34（972 块 65.1s） |
    ⚠️ 注意：`data/` 与 `tools/qdrant/storage/`、`tools/qdrant/snapshots/` 均在 `.gitignore` 中（体积大、可再生），**唯一的必需备份是 vault 原文**。快照创建耗时 >60s（约 882MB/库），调用时需把客户端 timeout 放大到 180s（默认 60s 会 `ResponseHandlingException`）。
- [x] **R-37** 项目工程师验收：按 tech.md §12 启动说明亲自跑完整流程——摄取 → 检索 → DSH 问答带引用 → 评测出分，全部符合验收标准后签字。
    ✅ **验收通过（2026-09-24，项目工程师亲自执行）**：四段全部跑通，原话「5 个步骤都已经做了，结果还是不错的」。
    | 段 | 动作 | 实测结果 | 判据 | 结论 |
    | :--- | :--- | :--- | :--- | :--- |
    | 1 摄取 | `ingest.py --update` | 扫描 65 / 跳过 65 / 失败 0 | 幂等成立、无失败 | ✅ |
    | 2 检索服务 | `python -m recall.api` + `/health` | 服务起来、Qdrant 与 collection 就绪 | 200 + `status=ok` | ✅ |
    | 3 检索 | `POST /kb/search` | 证据包 + `[n]` 与 `references` 一一对应 | 命中期望来源 | ✅ |
    | 4 DSH 问答 | 新会话自然语言问笔记 | 回答带 `[n]` 引用、忠于证据 | 引用可核对 | ✅ |
    | 5 评测出分 | `eval_retrieval.py` | **Recall@1=0.767 / @3=0.933 / @5=0.933 / @10=1.000 / MRR=0.860**（231.5s） | **与基线逐位一致** | ✅ |
    | 5 评测出分 | `eval_ragas.py --limit 10` | 题数 10 / **引用一致性 1.000** / **faithfulness=0.858** / **answer_relevancy=0.758**（374.2s） | 引用一致性满分；Ragas 落在方差区间内 | ✅ |
    📌 **里程碑**：R-37 通过即 **Phase 0~5 全部完成**（首版交付验收达成）。
    📌 检索指标与基线**逐位一致**（Recall@10 仍为 1.000）——这是确定性链路（同输入同输出，code_standards §0.2）应有的表现，也说明本次验收环境与基线环境等价。
    📌 Ragas 两次独立运行落点：faithfulness 0.705 → 0.858；answer_relevancy 0.678 → 0.758，**均高于此前记录的基线**，且都在已说明的裁判方差区间内（三次 faithfulness 实测：0.867 / 0.705 / 0.858，均值 0.810）。

### Phase 6：后续迭代预留（不在首版验收范围，项目工程师另行排期）

- [ ] **R-38** watchdog 常驻增量同步（监听 vault 变化自动 `ingest --update`）。
- [ ] **R-39** Coze 接入：公网网关（Cloudflare Tunnel / 云服务器）+ API key + 限流 + 审计——权限 S2 的触发点（tech.md §7）。
- [ ] **R-40** 权限 S2：API key 中间件实现，`get_identity` 换真实实现，审计日志上线。
- [ ] **R-41** 新 Connector：飞书 / 语雀 / 网页（按 Connector 协议新增，不改管道其余部分）。
- [ ] **R-42** 检索调优 A/B：top_k / rerank / 切分参数用黄金集 + Recall@K 并排对比，数据驱动决策（tech.md §10 触发点）。
- [x] **R-43** `HF_HUB_OFFLINE` 升为配置项（**默认离线**）：把"模型加载前不回连 HF Hub"从"评测时的临时建议"变成服务进程的默认行为（tech.md §12 的"先建配置、后加载模型"顺序不变）。
    ⚠️ 现场证据（2026-09-23 真实 DSH 会话）：`kb_search` **每一次**都在 ~42s 后失败——`mcp__recall__kb_stats` 却全程正常（Qdrant 活着），说明故障不在检索库。`data/logs/api.log` 的异常链给出确切位置：`api.kb_search_core` → `embedder._encode_sync` → `model_cache.load_once` → `BGEM3FlagModel.__init__` → `transformers…tokenization_auto.from_pretrained` → **`transformers/utils/hub.py::list_repo_templates`** → `huggingface_hub.hf_api.list_repo_tree` → `httpx.ConnectTimeout`。
    根因：transformers 5.x 装载 tokenizer 时会去 Hub 拉 `chat_template.jinja` 清单，**权重已在本地缓存也照样走一次网络**；本机出网间歇不可达（§七 2026-09-23 R-32 环境记录），该请求挂在 TCP 连接上直到 httpx 超时 ⇒ 整个 `kb_search` 被拖死（实测 6 次调用 23:18:09→23:22:08 全部同一栈）。
    ✅ 处理：`recall/config.py` 新增 `DEFAULT_HF_HUB_OFFLINE = True`、`Settings.hf_hub_offline`（env `HF_HUB_OFFLINE`）与 `_read_bool()`（`0/false/no/off` 为假，与 `log_to_file` 同一套词法）；`Settings.from_env()` 用 `os.environ.setdefault("HF_HUB_OFFLINE", …)` 落盘 ⇒ **真实环境变量优先**，要下载新模型时 `HF_HUB_OFFLINE=0` 依然管用。`.env` 同步写入 `HF_HUB_OFFLINE=1` 与说明。
    ✅ 验证（2026-09-23）：`HF_HUB_OFFLINE=1` 下 `BGEM3FlagModel("BAAI/bge-m3", use_fp16=True)` **离线装载 8.0s、编码 2.0s**（dense 1024 维 / sparse 6 项），此前同一步骤 42s 超时；两个模型（bge-m3 / bge-reranker-v2-m3）快照完整落盘（R-05 已补全，故不会 `IncompleteSnapshotError`）；新增 `tests/test_config.py` 12 项覆盖默认值 / 逃生口 / 词法 / 落盘到 `os.environ`。
    🧭 项目工程师指示：**待复核**（配置默认值调整，未改 tech.md 任何契约；如需"默认在线"改一个常量即可）
- [x] **R-43b** 修 R-43 的**致命遗漏**：默认离线在服务路径下**根本不生效**（2026-09-24 现场复现）。
    ⚠️ 问题：R-43 只在"先设环境变量、后 import HF 栈"的顺序下有效。而 `recall.api` 的实际顺序相反——模块顶部 `from recall.embedder import …` 会连带导入 FlagEmbedding → transformers → huggingface_hub，**之后**才在模块级执行 `Settings.from_env()`。而 `huggingface_hub.constants.HF_HUB_OFFLINE` 是在 **import 时**从环境变量读出并固定的常量（源码 `_is_true(os.environ.get("HF_HUB_OFFLINE"))`）⇒ 晚设无效，模型装载照样回连 Hub。
    ✅ 实证：`import huggingface_hub.constants as C; from recall.config import Settings; Settings.from_env()` ⇒ 环境变量是 `'1'` 而 `C.HF_HUB_OFFLINE` 仍是 `False`；真实调用栈逐字复现 R-43 的那一条 `transformers/utils/hub.py::list_repo_templates → hf_api.list_repo_tree → httpx.ConnectTimeout`。
    📌 **这解释了为什么项目工程师新会话里 `kb_search` 仍会全量失败**——R-43 的"已修复"是被测试顺序骗过的（单独跑 `Embedder().encode()` 时 `from_env()` 恰好在前，所以通过）。
    ✅ 双层修复：
    ① **结构性**：`recall/__init__.py` 增加 `_bootstrap_environment()`——包一被导入就先 `Settings.from_env()`。"先建配置、后加载模型"从此由**导入顺序**保证，不靠调用方自觉；
    ② **兜底**：`recall/config.py` 新增 `_sync_hf_offline()`——若库已先于配置导入，直接把 `huggingface_hub.constants.HF_HUB_OFFLINE` 改成目标值并记一条 INFO。
    ✅ 验证：同一失败路径（`kb_answer` 三连问）现在**两个模型全部装载成功、零超时**（此前三问全部 ConnectTimeout 且 `cached models: []`）；"先 import HF 再配置"的顺序下 `C.HF_HUB_OFFLINE` 由 `False` 变 `True`。
    🧭 项目工程师指示：**待复核**
- [x] **R-44** MCP Streamable HTTP 默认改为**无会话（stateless）**：把"会话 id 存在服务端内存里"这个隐性依赖去掉，让客户端**不可能**再拿到过期的会话（tech.md §8 端点契约不变）。
    ⚠️ 现场报错（2026-09-24，R-43 修好之后项目工程师新开会话）：多次调用 MCP 工具全部失败——`Error: Streamable HTTP error: Error POSTing to endpoint: {"jsonrpc":"2.0","id":"server-error","error":{"code":-32600,"message":"Session not found"}}`（HTTP 404）。
    根因（三层，逐层取证）：
    ① **服务端**：`mcp` SDK 1.30 的 `StreamableHTTPSessionManager` 只认自己内存里签发的 session id，收到未知 id 直接 **HTTP 404**（本机复现：带假 id POST `/mcp/` ⇒ `404 {"code":-32600,"message":"Session not found"}`，与现场报文逐字一致）。会话表是**进程内存**，两条与代码无关的路径都会让它丢：**空闲 30 分钟被回收**（SDK 默认 `DEFAULT_SESSION_IDLE_TIMEOUT = 1800`；实测日志 `2026-09-24 00:21:04 Session d37ab607… idle timeout`）与**服务进程重启**（23:25 / 01:40 两次重启，此前签发的 id 全部作废）。
    ② **客户端不回退**：MCP Python SDK 的 `StreamableHTTPClientTransport` 收到 404 只做一件事——把响应翻成 `Session terminated` 错误**并 return**（`client/streamable_http.py:372`），**规范要求的"重新 initialize"没有实现**；DSH 的 `@deepseek-ai/dsh-mcp-client@0.1.5-rc.3` 只在 transport `onclose` 时重连（`lib/index.js:560 generationDown`），而 404 是**正常 HTTP 响应**、不触发 onclose。
    ③ **所以"新开会话"救不了**：MCP 连接是**每服务器一条、跨对话复用**的（插件激活时建立），新对话不会重新握手 ⇒ 客户端一直拿着死 id，表现为"多次调用全部失败"。
    ✅ 处理：`recall/config.py` 新增 `DEFAULT_MCP_STATELESS = True` 与 `Settings.mcp_stateless`（env `RECALL_MCP_STATELESS`）；`recall/api.py` 改为 `mcp.http_app(path="/", stateless_http=Settings.from_env().mcp_stateless)`。无会话模式下每个请求自带一次握手、服务端不再签发 session id ⇒ 上面三条路径同时消失。需要服务端推送（`notifications/tools/list_changed`、SSE 续传）时设 `RECALL_MCP_STATELESS=0` 回到状态化。
    ✅ 验证（2026-09-24，全部实测）：
    | 动作 | 结果 |
    | :--- | :--- |
    | 带**过期 session id** POST `/mcp/` `initialize` | **HTTP 200**（修复前 404），且响应**不签发** `Mcp-Session-Id` |
    | 同一过期 id 继续 `tools/call kb_stats` | HTTP 200 / `isError=false` / `points=972 docs=65` |
    | **真实 DSH 会话**（未重开会话）调 `mcp__recall__kb_search` | 成功返回 7 条证据 ⇒ 现场故障消失 |
    | 日志形态 | 每请求一行 `Terminating session: None`，再无 `Session … idle timeout` |
    新增 `tests/test_mcp.py::test_stale_session_id_is_not_rejected`（把"过期 id 必须能用"钉成回归点）+ `tests/test_mcp.py::test_stateful_mode_still_rejects_unknown_session`（同一请求在状态化模式下仍是 404，钉死因果）+ `tests/test_config.py` 2 项。
    ⚠️ 测试隔离坑（本轮踩到，已修）：``StreamableHTTPSessionManager.run()`` **每个实例只能跑一次**（`mcp/server/streamable_http_manager.py:155`），新用例若复用模块级 `mcp_app` 的 lifespan，会在**全量跑**时与"挂载 + lifespan"用例冲突（单跑必过、全量必挂）。改为用例内自建 ``mcp.http_app(...)`` 实例。
    🧭 项目工程师指示：**待复核**（默认值由"有会话"改为"无会话"；代价是失去服务端主动推送，本项目未使用；回退只需一个环境变量）

---

## 五、 问题日志（开发中按时间追加，原记录永不删除）

| 日期 | 原步骤 | 问题描述 | 新步骤 | 项目工程师指示 | 状态 |
| :--- | :--- | :--- | :--- | :--- | :--- |
| （示例）2026-09-xx | R-13 | embedder 显存溢出（fp32 + reranker 同驻 6GB） | R-13b：恒 fp16 + reranker 懒加载 | 同意，按 code_standards §10 处理 | ✅ 已解决 |
| 2026-09-22 | R-14 | `AsyncQdrantClient.upload_points`（qdrant-client 1.19.1）是**同步**方法，`await` 它抛 `TypeError: object NoneType can't be used in 'await' expression`；按 §4.3 字面写法无法通过验证 | R-14b：改为同步调用 + `asyncio.to_thread` 隔离 | **待复核**（已按 code_standards §0.4/§4.3 自行收敛，不动任何契约） | ✅ 已解决 |
| 2026-09-22 | R-16 | 本机 DSH 沙箱（workspace-write）拒绝写入 `mode=0o700` 目录（`tempfile.mkdtemp`、pytest basetemp 皆用），14 个 `tmp_path` 用例 setup 阶段 `PermissionError` | R-16b：文件策略放开为 `danger-full-access` 后复跑 | **待复核**（环境策略由项目工程师放开；测试代码未做沙箱妥协） | ✅ 已解决 |
| 2026-09-22 | R-23 | pytest 会话中连续装载 bge-m3 触发 `Windows fatal exception: access violation`（栈在 `transformers/core_model_loading.py::_materialize_copy`；transforms 5.x 权重装载用内部线程池并行 materialize，两线程同时装载即崩） | R-23b：新增 `recall/model_cache.py`（进程级缓存 + 装载锁），embedder/reranker 统一 `load_once` | **待复核**（实现层基础设施，不动契约） | ✅ 已解决 |
| 2026-09-22 | R-23 | 真实 vault 首轮摄取 268 篇里 203 篇是 `project/*/node_modules/**` 的第三方 CHANGELOG/LICENSE/README，严重稀释检索质量 | R-23c：`DEFAULT_SKIP_DIRS` 增加工程产物目录 + 开放 `--skip-dirs` 覆盖，重灌后 65 篇 / 972 点 | **待复核**（摄取范围细化，不改 Connector 协议） | ✅ 已解决 |
| 2026-09-22 | R-21 | Qdrant `Condition` 联合类型含 `Any`，`Filter.model_validate` 对残缺条件照单全收（`{"must":[{"key":"doc_id"}]}` 能通过），坏 filter 会一路带到 Qdrant 变成 500 而不是干净的 400 | 在 `recall/auth.py` 内自建结构校验 `validate_client_filter` | **待复核**（API 错误规范内的实现细节） | ✅ 已解决 |
| 2026-09-22 | R-28 | AI 执行者无法自行开启 DSH 会话：MCP 服务器只在会话启动时装载，本会话看不到 `mcp__recall__*`，R-28 的字面验收无法由 AI 独立完成 | R-28b：用真实 MCP 客户端完成除"会话装载"外的全部链路验证；并在新会话中由项目工程师确认 | **已确认（2026-09-24）**：项目工程师实测新会话与新工作区均可正常问答 | ✅ 已解决 |
| 2026-09-23 | R-43 | 真实会话里 `mcp__recall__kb_search` **全量失败**（每次 ~42s 后 `fetch failed`），而 `kb_stats` 一直正常；`api.log` 栈指向 `transformers…list_repo_templates` → `hf_api.list_repo_tree` → `httpx.ConnectTimeout`（模型已缓存仍回连 HF Hub，本机出网间歇不可达） | R-43：`HF_HUB_OFFLINE` 升为配置项且**默认离线**（`.env` + `Settings.from_env()` 落盘 `os.environ`，`HF_HUB_OFFLINE=0` 可下载新模型）；新增 `tests/test_config.py` | **待复核**（配置默认值调整，不动契约） | ✅ 已解决 |
| 2026-09-24 | R-44 | R-43 修好后项目工程师**新开会话**依旧全量失败：`Streamable HTTP error … {"code":-32600,"message":"Session not found"}`（HTTP 404）。服务端会话表在进程内存里（空闲 30 分钟回收 + 进程重启即失效），而 MCP 客户端收到 404 **不会重新 initialize**（SDK 只翻成 `Session terminated` 就 return；DSH mcp-client 只在 transport onclose 时重连）⇒ 客户端永远拿着死 id，且 MCP 连接跨对话复用，"新开会话"也无解 | R-44：MCP 默认改为**无会话**（`Settings.mcp_stateless` / `RECALL_MCP_STATELESS`，`http_app(stateless_http=True)`）；新增过期 id 回归测试 | **待复核**（默认模式调整，tech.md §8 端点契约不变；代价是失去服务端主动推送） | ✅ 已解决 |
| 2026-09-24 | R-43 | **R-43 的修复实际未生效**：默认离线只在"先设环境变量、后 import HF 栈"的顺序下有效；`recall.api` 是反的（顶部先 import embedder → FlagEmbedding → transformers → huggingface_hub），而 `huggingface_hub.constants.HF_HUB_OFFLINE` 在 import 时冻结 ⇒ 晚设无效，模型装载照样回连 Hub。**这正是项目工程师新会话里 `kb_search` 仍全量失败的真正原因**（R-43 的验证被测试顺序骗过：单跑 `Embedder().encode()` 时 `from_env()` 恰好在前） | R-43b：① `recall/__init__.py` 增加 `_bootstrap_environment()`（包导入即建配置，顺序由导入保证）；② `recall/config.py` 增加 `_sync_hf_offline()`（库已先导入时直接改其常量）；新增 3 项回归测试 | **待复核**（实现层修复，不动契约） | ✅ 已解决 |

---

## 六、 当前进度快照（每步完成/受阻后更新）

- **当前阶段**：✅ **Phase 0~5 全部完成（首版交付验收达成）**；Phase 6（R-38~R-42）待项目工程师排期
- **当前步骤**：**R-37 验收通过（2026-09-24）** —— 全部 R-01~R-37 已闭环（除 R-02/R-03 两条已由 R-02b/R-03b 绕行）
- **已通过项**：R-01、R-02b、R-03b、R-04、R-05、R-06、R-07~R-17、R-18、R-19、R-19b、R-20~R-23c、R-24、R-25、R-26、R-27、R-27c~R-27i、R-28、R-28b、R-29、R-29b、R-30、R-31、R-32、R-32b、R-32c、R-33、R-34、R-35、R-36、R-37、R-43、R-43b、R-44
- **未通过项**：R-02（官方源网络超时，已走 R-02b）、R-03（Docker 未运行，已走 R-03b）
- **待请示事项**（Phase 0~5 已验收，以下为**非阻塞**的后续选择）：
  - **需你拍板**：
    1. **R-28 遗留**：`GET /kb/stats` 返回 404 是否符合你的预期；若要在 REST 也提供统计端点，属**契约新增**（tech.md §8 目前只列四个端点）；
    2. **Phase 6 是否开工**（R-38 watchdog / R-39 Coze 公网 / R-40 权限 S2 / R-41 新 Connector / R-42 检索调优）；
  - **需你复核默认值（改一个常量即可回退）**：
    3. **R-43 / R-43b**：`HF_HUB_OFFLINE` 默认离线（`DEFAULT_HF_HUB_OFFLINE`）；
    4. **R-44**：MCP 默认无会话（`RECALL_MCP_STATELESS=0` 可回退到状态化）；
  - **需你背书的实现细节（均未触及 tech.md 契约）**：
    5. R-14b / R-16b / R-21 / R-23b / R-23c / R-27c~R-27i / R-29b / R-32c / R-43b 的「项目工程师指示」待复核；
  - **已决**：~~R-19b 是否属契约变更~~ → **已确认（2026-09-23）**（已回写 tech.md §4 与 §17）；~~R-32d 检索阈值~~ → **已定案（2026-09-24）走回答模板路线**，胖端点侧候选转入 R-42。
- **最近一次测试结果**（2026-09-24）：`pytest` **163 passed**；`ruff` 零告警；`mypy` strict 38 文件零错误；promptfoo 3 通过 / 1 失败 / **0 错误**；`kb_answer` 三连问零超时
- **验收实测**（2026-09-24，项目工程师执行）：摄取 65 篇 0 失败；`/health` ok（972 点 / 65 篇）；检索 **Recall@1=0.767 / @3=0.933 / @5=0.933 / @10=1.000 / MRR=0.860**（与基线逐位一致）；Ragas **引用一致性 1.000 / faithfulness 0.858 / answer_relevancy 0.758**；DSH 问答带 `[n]` 引用通过
- **本文件版本**：v0.11.0（2026-09-24 **R-37 验收通过 ⇒ Phase 0~5 全部完成**；R-32d 定案走模板路线、R-43b 修复"默认离线在服务路径下失效"）

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
| 2026-09-22 | R-05 | 依赖安装 | `pip install -e ".[dev]"`（清华镜像）成功：FlagEmbedding 1.4.2 / qdrant-client 1.19.1 / fastmcp 2.14.7 / transformers 5.17.0 / pydantic 2.13.5 | Phase 0 |
| 2026-09-22 | R-05 | 模型下载 | HF cache 落盘 `BAAI/bge-m3`（2.19GB）、`BAAI/bge-reranker-v2-m3`（2.3GB），走 `HF_ENDPOINT=https://hf-mirror.com` | tech.md §12 |
| 2026-09-22 | R-05 | 依赖声明修正 | `pyproject.toml` 追加 `python-dotenv`、`numpy`：二者被 `recall/config.py`、`recall/embedder.py` 直接 import，原先只作传递依赖存在 | 直接依赖必须显式声明 |
| 2026-09-22 | R-05 | 依赖锁定 | 新增 `requirements.lock`（157 项 `==` 显式 pin），满足 code_standards §14「pyproject + 锁文件」 | 首次锁定 |
| 2026-09-22 | R-02b | 事实记录 | `requirements.lock` 记录 torch 实际安装源为 `file:///D:/torch-2.6.0+cu124-cp311-cp311-win_amd64.whl`（wheel 位于 D 盘根目录；`tools/wheels/` 下那份为未完成下载） | 与 R-02b 描述略有出入，按实际记录 |
| 2026-09-22 | R-05 | 环境绕行 | 首轮模型下载用 `ignore_patterns` 跳过 `onnx/*`、`imgs/*`、`*.md`，导致 `HF_HUB_OFFLINE=1` 时 huggingface_hub 报 `IncompleteSnapshotError`（缓存树与磁盘不一致）；改为补全完整快照后离线加载正常 | 测试因此可完全离线复现 |
| 2026-09-22 | R-08 | 实现层新增 | 新增 `recall/config.py`（env/`.env` 单一入口，密钥与路径不硬编码）；`recall/models.py` 增加 `Chunk`（切分器产出，承载 heading_path + sub_index）与 `RawDoc.updated_at`（可选，默认空） | tech.md §3 的 collection/payload/端点契约字段**零变动** |
| 2026-09-22 | R-10 | 实现细节 | `SearchResult.references` 由 `list[dict]` 参数化为 `list[dict[str, str]]` | 满足 code_standards §1「全量注解」+ §13「mypy 零错误」；字段名与语义不变 |
| 2026-09-22 | R-12 | 实现细节 | `doc_id` 保留 CJK（NFKC + 小写 + kebab-case，不引入拼音依赖）；同名文件跨目录冲突时，冲突项统一追加来源路径 sha1 前 8 位消歧 | code_standards §2 只要求"稳定 slug"，tech.md 示例中的拼音为生成方式而非契约 |
| 2026-09-22 | R-14 | 实现细节 | `upload_points` 改同步调用 + `asyncio.to_thread`（见 §五 R-14b） | 语义与 code_standards §4.3 一致 |
| 2026-09-22 | R-15 | 健壮性 | `ingest.py` 启动时把 stdout/stderr 切到 UTF-8（`errors="replace"`）：Windows 控制台默认 GBK，笔记标题含 emoji 时汇总打印会抛 `UnicodeEncodeError` | 否则一次成功的摄取会以失败退出码结束 |
| 2026-09-22 | R-16 | 测试环境 | 本机 DSH 沙箱拒绝 `mode=0o700` 目录导致 `tmp_path` 用例失败（见 §五 R-16b）；文件策略放开后 `pytest -q` 54 passed | 非代码缺陷 |
| 2026-09-22 | — | 忽略规则 | `.gitignore` 追加 `.tmp/`（pytest basetemp 等本地临时物）、`tools/qdrant/snapshots/`、`tools/qdrant/.qdrant-initialized` | 保持工作区干净 |
| 2026-09-22 | — | 环境文件 | 新增 `.env`（已 gitignore）：`RECALL_VAULT_PATH=D:/黑曜石/note/大模型应用`、`QDRANT_URL`、`HF_ENDPOINT`；DeepSeek key 留空待 Phase 4 | 路径用正斜杠，避免反斜杠被当转义 |
| 2026-09-22 | R-22 | 实现层新增 | `recall/models.py` 增加 API 请求/响应模型 `SearchRequest`、`HealthResult`（字段与 tech.md §8 逐字一致） | tech.md §3 payload 契约零变动 |
| 2026-09-22 | R-22 | 实现层新增 | `Settings` 增加 `collection`（env `RECALL_COLLECTION`）：A/B 评测可切库，默认仍为契约名 `recall__bge-m3@v1__md` | tech.md §3.1 多 collection 并排的运维入口 |
| 2026-09-22 | R-23 | 实现层新增 | 新增 `recall/model_cache.py`（进程级模型缓存 + 装载锁） | 见 §五 R-23b；transforms 5.x 并发装载会崩 |
| 2026-09-22 | R-23 | 摄取范围细化 | `ObsidianConnector` 增加 `skip_dirs` 参数与 `DEFAULT_SKIP_DIRS`（含 `node_modules`/`dist`/`build`/`.venv`/`venv`/`__pycache__`）；`ingest.py` 增加 `--skip-dirs` | 见 §五 R-23c |
| 2026-09-22 | R-23 | 数据变更 | 真实 collection 重灌：268 篇 → 65 篇，3121 点 → 972 点（清理 node_modules 产物 203 篇） | 旧 collection 未删（同库清理，非换模型） |
| 2026-09-22 | R-22 | API 契约细化 | 错误信封扩展覆盖 Starlette 的 404/405（`code="http_error"`），使信封全站统一 | code_standards §6.1 |
| 2026-09-22 | R-25 | 实现层新增 | `recall/models.py` 增加 `StatsResult`（kb_stats 返回体）；REST 路由处理函数由 `kb_search` 更名为 `kb_search_endpoint`——MCP 工具名必须等于函数名（code_standards §6.2），二者不能同名 | 工具名 `kb_search` 不变，REST 端点 `/kb/search` 不变 |
| 2026-09-22 | R-24 | 生命周期实现 | `lifespan` 改为组合式：先装配 Recall 服务，再 `async with mcp_app.lifespan(app)` | code_standards §6.3 的 ⚠️ 要点；Starlette mount 不会自动运行子应用生命周期 |
| 2026-09-22 | R-27 | 路径修正 | skill 安装路径由 `~/.dsh/skills/` 修正为 **`$DSH_HOME/skills`**（本机 `DSH_HOME=D:\dsh-data`，`~/.dsh` 不存在）；依据 `@deepseek-ai/dsh-skill-filesystem` 的根目录优先级表（`<dshHome>/skills` = user-dsh，rank 400） | tech.md §9/§11 的 `~/.dsh` 是按默认 `DSH_HOME` 写的 |
| 2026-09-22 | R-26 | 外部配置变更 | 修改 `D:\dsh-data\mcp-servers.json`：新增 `recall` 服务器条目（Context7 保留），**仓库外文件**，故在此登记 | 生效需新开 DSH 会话 |
| 2026-09-22 | R-25 | 测试补强 | 新增 `tests/test_mcp.py`（工具注册/召唤词 docstring/outputSchema/结构化错误/进程内 ASGI 生命周期握手） | code_standards §6.2/§6.3 |
| 2026-09-22 | R-32 | 依赖新增 | `pyproject.toml` 增加 `eval` 可选依赖组：`ragas`、`langchain-community>=0.3,<0.4`、`langchain-openai`、`pandas` | Ragas 为 tech.md §10 指定工具 |
| 2026-09-22 | R-32 | 依赖约束放宽 | `openai` 由 `>=1.50,<2` 放宽到 `>=1.50,<4`：ragas 依赖链装上 openai 3.3.0；实测 `AsyncOpenAI(api_key=…, base_url=…)` 与 `chat.completions.create(response_format=…, max_tokens=…)` 在 3.3.0 下仍可用，`recall.llm` 行为不变 | 见 §五 R-32b |
| 2026-09-22 | R-34 | 语义修正 | `--rebuild` 由"无条件重灌"改为「忽略 registry 快路径，但目标库已与本次切分结果一致时跳过」⇒ 真正可断点续传；无条件重灌改用 `--force` | tech.md §5 要求"幂等、可断点续传、可重复执行"；`--update`/`--force` 语义不变 |
| 2026-09-22 | R-34 | 数据变更 | 新增并排 collection `recall__bge-m3@v2__md`（972 点），旧库 `v1` 保留；A/B 指标完全一致 | tech.md §3.1 换 embedding 版本的演练 |
| 2026-09-22 | R-32 | 工具链 | promptfoo 通过 `npx promptfoo@latest`（0.123.1）可用；Windows 上须用 `npx.cmd`（`npx.ps1` 被执行策略拦截） | 记于 README 评测段 |
| 2026-09-22 | — | 清理 | 删除测试遗留的 `recall-test-*` collection（清理失败不应掩盖用例结论，但需人工回收） | 环境整洁 |
| 2026-09-22 | R-27c | 文档新增 | 新增仓库根 `ASSEMBLY.md`：补 tech.md §9 的第三杠杆（工作区兜底文件） | 见 §四 R-27c；原 §四 未覆盖 |
| 2026-09-22 | R-27d | 配置落地 | 新增 `Settings.log_to_file`（`RECALL_LOG_TO_FILE`）与 `recall.config.configure_logging()`；日志落盘 `data/logs/<component>.log`（5MB×3 轮转，UTF-8） | 见 §四 R-27d；原 `log_dir` 是悬空配置 |
| 2026-09-22 | R-27d | 环境变量新增 | 新增环境变量 `RECALL_LOG_TO_FILE`（默认 `1`）；测试在 `tests/conftest.py` 里设为 `0` | 避免用例污染 `data/` |
| 2026-09-22 | R-36 | 备份落地 | **实际产出** Qdrant 快照两个（v1 882MB / v2 881.9MB，位于 `tools/qdrant/snapshots/`，已 gitignore）；清理重复快照一份 | 快照创建 >60s，客户端 timeout 需放到 180s |
| 2026-09-22 | R-26 | 格式复核 | 依据 `@deepseek-ai/dsh-mcp-client` 文档核对注册格式：`transport: streamable-http` + `serverName`（`[A-Za-z0-9_-]{1,32}`）+ `url`，工具名形如 `mcp__<serverName>__<tool>` ⇒ `mcp__recall__kb_search`，与 tech.md §8 一致 | DSH 侧实际装载仍需新会话确认 |
| 2026-09-22 | — | spec 回写 | **code_standards §15**：`Connector` 协议补 Google 风格 `Example:`（完整可抄的最小来源实现，供 R-41 参考）；**tech.md 新增 §16 决策记录 9~12**（模型缓存+装载锁 / `--rebuild` 可续跑 / 摄取排除工程产物 / MCP 工具名与 REST 处理函数改名） | 影响架构的决策必须回写 spec 并追加 tech.md 决策记录 |
| 2026-09-22 | R-33 | 新增产物 | 新增 `eval/BASELINE.md`（基线评测报告：指标 / 名次分布 / 分类命中率 / 未命中明细 / R-42 调优候选） | R-33 的"汇报"落点；检索半已完成，Ragas 半待 key |
| 2026-09-22 | R-27e | spec 回写 | `Connector` 协议补 `Example:`；`tech.md` 新增 §16 决策记录 9~12 | code_standards §15 两条硬性要求 |
| 2026-09-22 | R-27f | 配置清理 | 删除无用的 `Settings.ingest_workers` 与 `RECALL_INGEST_WORKERS` 环境变量 | 见 §四 R-27f |
| 2026-09-22 | R-27f | 入口新增 | `recall/api.py` 增加 `main()`：`python -m recall.api` 按 `Settings.host/port` 启动服务；README 运行拓扑同步 | 让 `host`/`port` 真正生效 |
| 2026-09-22 | R-27f | 行为显式化 | `Settings.from_env()` 显式 `os.environ.setdefault("HF_ENDPOINT", …)`；新增常量 `DEFAULT_HF_ENDPOINT` | HF 镜像须在任何模型加载前生效（tech.md §12） |
| 2026-09-22 | — | spec 勾选 | `tech.md` §14 三个待验证检查点勾选并附实测证据（Qdrant native 二进制 / 模型下载 / dense+sparse 冒烟） | 检查点已由 R-03b、R-05、R-06 验证 |
| 2026-09-22 | R-33 | 实测补录 | `--no-rerank` 分解实验：hybrid-only MRR 0.650 vs +rerank 0.601（Recall@1 0.567 vs 0.467），延迟 14.3s vs 108s ⇒ **精排净负收益**，列为 R-42 第一优先项 | 见 `eval/BASELINE.md` §1；**不改链路顺序**（属 tech.md §4 契约），只取证 |
| 2026-09-22 | R-19b | **质量修复** | 精排输入由「块正文」改为「`heading_path` + 正文」（`rerank.build_rerank_document`）：MRR **0.601 → 0.860**、Recall@1 0.467 → **0.767**、Recall@10 0.833 → **1.000**、未命中 5 → **0 题**；v1/v2 复测一致 | 根因定位见 §四 R-19b；链路节点与顺序未变 ⇒ 实现修正而非契约变更 |
| 2026-09-22 | R-19b | 取证方法 | ① 用 reranker 自己的分词器否证「截断」假设（972 块仅 1 块 > 1024；若按模型默认 512 则 45% 被截断）；② 逐题 rank 差定位（变差 8 / 变好 5）；③ 同候选两种输入 A/B（13 题变好 / 0 题变差） | 诊断脚本为一次性工具，未入库 |
| 2026-09-23 | R-27i | **Bug 修复** | Qdrant 未启动时 `/kb/search` 返回 500 + 十几层堆栈、错误码 `internal_error` → 新增 `StoreUnavailableError`（沿异常链识别连接/超时错误）并在 API 层转成 **503 `qdrant_unavailable`** + 可操作提示 | 由项目工程师实战日志发现；code_standards §6.1 |
| 2026-09-23 | R-28 | 观察记录 | 实测期间 `GET /kb/stats` → 404。按 tech.md §8 `kb_stats` 仅为 MCP 工具，REST 四个端点中无此项 ⇒ 404 符合契约；若要新增 REST 统计端点属契约变更，待项目工程师确认 | 不改实现 |
| 2026-09-23 | R-29b | **质量修复 ×4** | 胖端点 4 个"整段回答作废"级缺陷：英文双引号截断 JSON / 复述证据致输出截断 / 过度拒答 / JSON 夹带文字；分别用系统提示词硬性规则 ×2、忠实度规则双向约束、`parse_json_object()` 容错解析修复 | 详见 §四 R-29b；全部由真实调用暴露 |
| 2026-09-23 | R-32c | **严重 Bug 修复** | 并发下 `/kb/answer` 500：`FlagReranker.compute_score` 每次调用都改模型（`self.model.half()`），模型缓存让多请求共享实例 ⇒ `expected scalar type Float but found Half`。新增 `inference_lock()` 串行化推理段；并发 6 请求实测全 200 | 由 promptfoo 并发 4 暴露；单线程从未复现 |
| 2026-09-23 | R-32 | 依赖/接口修正 | ragas 两套 embedding 接口（`text/texts` 与 `query/documents`）都实现，`answer_relevancy` 才出分 | 首次运行该指标直接缺席 |
| 2026-09-23 | R-32d | 待决策记录 | 检索无相关性下限 ⇒ 笔记外问题被硬答；候选修法（rerank 分数阈值）属 R-42，未获确认前不改 | 见 §四 R-32d |
| 2026-09-23 | R-32 | 环境记录 | 本机出网**间歇性不可达**（WinINET 配了本地代理 127.0.0.1:3067，开关切换时 httpx 连接挂起）；模型加载默认会回连 HF Hub，缓存已存在也会卡 40s+ ⇒ 评测/验证建议 `HF_HUB_OFFLINE=1` | 已写进 BASELINE.md 与最终汇报 |
| 2026-09-22 | R-27h | 测试补强 | 新增 `tests/test_llm.py`（11 项，假 OpenAI 客户端测重试/JSON 模式/解析失败）；补 502 `llm_failed`、`ping()` 不可达、`with_retry` 成功/耗尽、`collection_not_found` 503 共 5 项；R-19b 再补 3 项（拼接/空标题/检索链路喂标题探针） | `pytest` 113 → **132** |
| 2026-09-22 | R-33 | 新增产物 | `eval/baseline_v1_hybrid_only.json`（hybrid-only 逐题明细） | 供 R-42 A/B |
| 2026-09-22 | R-32 | 边界验证 | `eval/eval_ragas.py` 无 key 时干净退出（ragas/langchain/openai 依赖链导入全通过）⇒ 链路已验到 key 边界 | 补齐"未执行过即未验证"的缺口 |
| 2026-09-22 | R-27g | 死代码清理 | 接上三处"有文档、没人用"的成员：`kb_stats` 增加 `collections` 字段（用上 `list_collections`）、`ingest.py` 传 `dense_dim=embedder.dimension`、新增 `tests/test_model_cache.py` 用上 `cached_models`/`clear_cache` | 见 §四 R-27g |
| 2026-09-22 | R-27g | **Bug 修复** | `recall/model_cache.py::cached_models()` 裸 `sorted()` 在键含 `None` 与 `"cpu"` 混排时抛 `TypeError` → 改为 None 安全排序键；新增单测固定该回归点 | 由新单测发现 |
| 2026-09-22 | R-27g | 契约细化 | `StatsResult` 新增 `collections: list[str]`（现存全部 collection 名） | MCP 工具返回体新增字段，不改 REST 端点契约 |
| 2026-09-22 | R-27h | 实现层小改 | `recall/llm.py` 抽出常量 `RETRY_BASE_DELAY`（退避基数可注入，生产默认 0.5s 不变） | 让重试逻辑可测 |
| 2026-09-23 | R-43 | 配置新增 | 新增 `DEFAULT_HF_HUB_OFFLINE = True`、`Settings.hf_hub_offline`（env `HF_HUB_OFFLINE`）与 `_read_bool()`；`.env` 写入 `HF_HUB_OFFLINE=1` | 见 §四/§五 R-43；默认值可用一个常量回退 |
| 2026-09-23 | R-43 | 行为显式化 | `Settings.from_env()` 同时 `os.environ.setdefault("HF_HUB_OFFLINE", …)`，与 `HF_ENDPOINT` 同一条"模型加载前生效"约束 | 真实环境变量优先 ⇒ 下载新模型仍可 `HF_HUB_OFFLINE=0` |
| 2026-09-23 | R-43 | 测试补强 | 新增 `tests/test_config.py`（12 项：默认离线 / 逃生口优先级 / 布尔词法 / 落盘 `os.environ` / 不挤掉 `HF_ENDPOINT`）；`tests/test_llm.py` 的 `Settings(...)` 补 `hf_hub_offline` 字段 | 修 `kb_search` 全量 ConnectTimeout 的回归点 |
| 2026-09-23 | R-43 | 运维注意 | 服务重启（换进程）后，**已开的 DSH 会话里 `mcp__recall__*` 会持续 `fetch failed`**：DSH 的 MCP 客户端缓存了旧进程的 session id，客户端侧无重连/重试 ⇒ 需**重开 DSH 会话**才恢复（此前 R-28 已记录"服务器只在会话启动时装载"的同源约束） | 本次修复后实测：新会话握手 + `tools/call` 正常（`isError: false`） |
| 2026-09-23 | R-43 | 文档补记 | `eval/BASELINE.md` 末尾补「评测环境的前置条件」：把 2026-09-23 R-32 行提到的"评测建议 `HF_HUB_OFFLINE=1`"落成文字（该行原写"已写进 BASELINE.md"但正文此前并无此内容），并注明现已固化为默认行为 | 补齐 §七 2026-09-23 R-32 行的承诺 |
| 2026-09-24 | R-44 | 配置新增 | 新增 `DEFAULT_MCP_STATELESS = True` 与 `Settings.mcp_stateless`（env `RECALL_MCP_STATELESS`） | 见 §四/§五 R-44；回退只需设 `RECALL_MCP_STATELESS=0` |
| 2026-09-24 | R-44 | 行为变更 | `recall/api.py`：`mcp.http_app(path="/", stateless_http=Settings.from_env().mcp_stateless)` ⇒ `/mcp` 端点默认**不签发会话 id** | tech.md §8 端点路径与工具名不变；仅传输层会话语义变化 |
| 2026-09-24 | R-44 | 测试补强 | 新增 `tests/test_mcp.py::test_stale_session_id_is_not_rejected`（带过期 id 的 `initialize` 必须 200 且不签发新 id）；`tests/test_config.py` 补 2 项 | 把现场 404 钉成回归点 |
| 2026-09-24 | R-28 | **验收归档** | R-28 验收通过（项目工程师：「新会话与新工作区问问题都较好完成」），勾选并记入 §五 | 原"AI 无法自行开启会话"的阻塞解除 |
| 2026-09-24 | R-43,R-44 | 入库补记 | R-43/R-44 的改动此前只落盘未提交，本次正式 commit（`a8a090d`）并复验 gates | 变更登记铁律要求每次改动入库 |
| 2026-09-24 | R-43b | **严重遗漏修复** | 默认离线在服务路径下失效（`huggingface_hub` 常量 import 时冻结，而 `recall.api` 先 import HF 栈再 `from_env`）⇒ ① `recall/__init__.py` 加 `_bootstrap_environment()`（包导入即建配置）② `recall/config.py` 加 `_sync_hf_offline()` 兜底改常量 ③ 新增 3 项回归测试 | 见 §五 R-43 行；**这解释了 `kb_search` 仍全量超时的真正原因** |
| 2026-09-24 | R-32d | **定案：走模板路线** | 项目工程师定案**不加检索阈值**，改为回答模板：① `skill/recall-assembly.md` 新增「笔记里没有的内容：三段式模板」（先直接答/可联网 → 显式声明笔记没有 → 披露相邻主题且说明"不是答案"）；② `ASSEMBLY.md` 同步；③ 重新同步到 `$DSH_HOME/skills/`（5176 字节） | 见 §四 R-32d；agent 侧实测有效（项目工程师样例） |
| 2026-09-24 | R-32d | **试改后回退（附证据）** | 给 `FIDELITY_RULES` 加"相邻主题必须点名"→ **过度拒答复发**：问"切分器粒度怎么选"（笔记明确写过、上一版能正确作答）被答成"笔记里没有相关内容" ⇒ 该条回退，`FIDELITY_RULES` 保持跑出 Ragas 基线的那一版（**基线数字仍有效**） | 结论：**"相关 vs 相邻"光靠提示词稳不住，需检索侧信号** ⇒ 登记为 R-42 首选候选 |
| 2026-09-24 | R-37 | **验收通过** | 项目工程师亲自跑完四段：摄取（65 篇 0 失败）/ 检索服务（`/health` ok）/ 检索（证据包与引用对应）/ DSH 问答带 `[n]` 引用；评测出分 **Recall@1=0.767 @3=0.933 @5=0.933 @10=1.000 MRR=0.860**（与基线**逐位一致**）、**引用一致性 1.000 / faithfulness 0.858 / answer_relevancy 0.758** | **Phase 0~5 全部完成**；Ragas 复跑值高于基线，落在已说明的裁判方差区间内 |
| 2026-09-24 | R-37 | 记录修订 | `eval/BASELINE.md` §5 改为**三列对照**（首轮基线 / R-37 复跑 / 三次实测区间），并明确"引用一致性是确定性的、可当硬门槛；Ragas 单次值不可当门槛" | 避免后人把裁判方差当回归 |
