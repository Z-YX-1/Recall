# Recall（拾忆）

个人 RAG 知识库：本地优先、隐私安全。Obsidian 笔记摄取 → bge-m3 混合检索（dense + sparse + RRF + Rerank）→ 证据包 / 带引用回答。

## 规范文档

| 文档 | 说明 |
| --- | --- |
| [spec/tech.md](spec/tech.md) | 技术栈与架构契约（做什么、为什么） |
| [spec/code_standards.md](spec/code_standards.md) | 代码规范（怎么写） |
| [spec/roadmap.md](spec/roadmap.md) | 开发路线图（按步骤执行，含变更登记） |

> 冲突时以 `tech.md` 为准；实现细节以 `code_standards.md` 为准。

## 环境要求

- Python 3.11（conda env: `recall`）
- NVIDIA GPU + CUDA 12.4（PyTorch cu124）；本机实测 RTX 4050 Laptop 6GB，bge-m3 fp16 常驻约 1.1GB
- Qdrant 单机服务（`127.0.0.1:6333`）
- 模型：`BAAI/bge-m3`、`BAAI/bge-reranker-v2-m3`（走 `HF_ENDPOINT=https://hf-mirror.com`）

## 安装

```powershell
conda activate recall
pip install -e ".[dev]" -i https://pypi.tuna.tsinghua.edu.cn/simple
# torch 单独装 cu124：pip install torch --index-url https://download.pytorch.org/whl/cu124
# 精确复现环境：pip install -r requirements.lock
copy .env.example .env   # 若无 .env.example，见下方「配置」
```

## 配置（`.env`，已 gitignore）

```dotenv
RECALL_VAULT_PATH=D:/黑曜石/note/大模型应用   # Obsidian vault 根目录
QDRANT_URL=http://127.0.0.1:6333
HF_ENDPOINT=https://hf-mirror.com
# DEEPSEEK_API_KEY=...                        # Phase 4 胖端点用，绝不入库/入日志
```

## 运行拓扑（三进程）

```powershell
# 进程 1: Qdrant（native 二进制）
tools\qdrant\qdrant.exe

# 进程 2: 检索服务（REST + MCP 同端口；host/port 从 .env 读）
python -m recall.api
# 等价写法：uvicorn recall.api:app --host 127.0.0.1 --port 8000

# 进程 3: 摄取（手动触发；幂等、可重复执行）
python ingest.py --update          # 增量：文档 hash 未变即整篇跳过
python ingest.py --rebuild         # 重灌同一 collection（可断点续传）
python ingest.py --rebuild --collection recall__bge-m3@v2__md --model bge-m3@v2
```

## 质量门槛

```powershell
ruff check recall ingest.py eval tests   # 零告警
ruff format --check recall ingest.py eval tests
mypy recall ingest.py eval tests         # strict，零错误
pytest -q                                # 全绿；依赖不可用的集成用例会 skip 并给出原因
```

## 评测（tech.md §10）

```powershell
# 检索指标：Recall@K / MRR（--collection 参数化 ⇒ 新旧库并排 A/B）
python eval/eval_retrieval.py --collection recall__bge-m3@v1__md --k 1,3,5,10 --output eval/baseline_v1.json
python eval/eval_retrieval.py --collection recall__bge-m3@v1__md --no-rerank   # 只看混合召回

# RAG 质量：Ragas faithfulness / answer relevancy（需要 DEEPSEEK_API_KEY）
pip install -e ".[eval]"
python eval/eval_ragas.py --limit 10 --output eval/ragas_baseline.json

# prompt 回归（需要服务在跑）：npx promptfoo@latest eval -c eval/promptfoo/promptfooconfig.yaml
```

当前基线（`recall__bge-m3@v1__md`，30 题）：Recall@1=0.467 / Recall@3=0.733 / MRR=0.601。

## 幂等三机制（tech.md §5）

| 机制 | 作用 |
| --- | --- |
| 文档级 hash 跳过 | `sha256(归一化全文)` 未变 ⇒ 整篇跳过（`--update`） |
| 块级内容寻址 | `point_id = uuid5(doc_id, chunk_index, content_hash)` ⇒ 未变块原地覆盖 |
| 孤儿清理 | 重灌后按 `doc_id` 扫出旧 point，删除不在新 id 集合中的点 |

跳过语义：`--update` 走账本快路径；`--rebuild` 忽略账本但目标库已一致时跳过（可断点续传）；
`--force` 无条件重新嵌入。

## 仓库

- 远程：`git@github.com:Z-YX-1/Recall.git`
- 提交格式：`<阶段>/<编号> <简述>`（见 roadmap R-04）
