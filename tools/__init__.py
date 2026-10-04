"""Recall 运维/验收脚本包（`tech.md` §11 目录树）。

与 :mod:`eval` 同一个约定：这些脚本**既能当包导入**（`from tools.mark_public import decide`，
用例需要），**也能当脚本直接运行**（`python tools/mark_public.py`）——
故本文件让 ``tools`` 成为正规包，而各脚本自己带 ``sys.path`` 引导。

⚠️ 本目录还混放了非 Python 资产（`.ps1` 启动脚本、`qdrant/` 二进制与数据），
它们与本包无关，不受 `__init__.py` 影响。
"""
