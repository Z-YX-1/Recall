"""Recall 评测包：黄金集 + 检索指标 + RAG 质量（tech.md §10；code_standards §11）。

作为包导入（``from eval.eval_retrieval import load_golden``）与作为脚本直接运行
（``python eval/eval_retrieval.py``）都要能用，因此本文件让 ``eval`` 成为正规包，
两个脚本各自带 sys.path 引导。
"""
