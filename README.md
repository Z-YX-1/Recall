# Recall（拾忆）

个人 RAG 知识库：本地优先、隐私安全，支持 Obsidian 笔记摄取与混合检索。

## 规范文档

| 文档 | 说明 |
| --- | --- |
| [spec/tech.md](spec/tech.md) | 技术栈与架构契约（做什么、为什么） |
| [spec/code_standards.md](spec/code_standards.md) | 代码规范（怎么写） |
| [spec/roadmap.md](spec/roadmap.md) | 开发路线图（按步骤执行） |

> 冲突时以 `tech.md` 为准；实现细节以 `code_standards.md` 为准。

## 环境要求

- Python 3.11（conda env: `recall`）
- NVIDIA GPU + CUDA 12.4（PyTorch cu124）
- Qdrant 单机服务（127.0.0.1:6333）

## 快速启动（Phase 0 完成后补充）

```powershell
conda activate recall
# 进程 1: Qdrant
# 进程 2: uvicorn recall.api:app --host 127.0.0.1 --port 8000
# 进程 3: python ingest.py --update
```

## 仓库

- 远程：`git@github.com:Z-YX-1/Recall.git`
- 提交格式：`<阶段>/<编号> <简述>`（见 roadmap R-04）
