"""清理 Qdrant 里泄漏的测试 collection 目录（roadmap 2026-09-25 存储卫生）。

## 为什么会有这个问题

测试夹具 `tests/conftest.py::ingest_env` 每个用例建一个一次性 collection，
用完调 `delete_collection`。**API 层面删除是成功的**（`GET /collections` 不再列出它），
但 Qdrant 在 Windows 上的**磁盘目录没有随之删除** —— 于是每跑一次全量测试就漏下
约 700MB 的孤儿目录。实测（2026-09-25）：

- `storage/collections/` 里 12 个目录，API 只认得 2 个 ⇒ **10 个孤儿（~7.2GB）**；
- 一次全量测试让 D 盘可用空间从 8.62GB 掉到 7.76GB（≈ 一个 700MB 的 collection）；
- D 盘一紧，Qdrant 就报 `IO Error: 拒绝访问 (os error 5)` 并进入"未从先前错误恢复"的
  降级态 ⇒ 对后续请求一律 500 ⇒ 测试表现为**随机 flaky**（见 `tech.md` §12.3）。

## 安全性

只删**名字匹配 ``recall-test-*``** 的目录：

- 生产 collection 的命名契约是 ``recall__<模型>@<版本>__<切分器>``（tech.md §3.1），
  **不可能**撞上这个前缀；
- 这些目录也**不在** Qdrant 的 collection 列表里（Qdrant 已经不认它们），
  所以删掉不会影响任何在用数据。

⚠️ 仍请在**测试没在跑**的时候执行 —— 万一有正在进行的用例，它的临时 collection 会被误删。

用法::

    python tools/clean_qdrant_orphans.py            # 只列出（默认，不动任何文件）
    python tools/clean_qdrant_orphans.py --yes      # 真正删除
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

TEST_PREFIX = "recall-test-"
"""测试用一次性 collection 的前缀（`tests/conftest.py::unique_collection`）。"""

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def _known_collections(qdrant_url: str) -> list[str]:
    """向 Qdrant 要"当前认得的 collection"列表；取不到时返回空表。"""
    try:
        with urllib.request.urlopen(f"{qdrant_url}/collections", timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        return []
    return [str(item.get("name", "")) for item in payload.get("result", {}).get("collections", [])]


def _size_mb(path: Path) -> float:
    """目录的逻辑大小（MB）。"""
    total = sum(item.stat().st_size for item in path.rglob("*") if item.is_file())
    return total / (1024 * 1024)


def main() -> int:
    """列出（可选删除）泄漏的测试 collection 目录。"""
    parser = argparse.ArgumentParser(description="清理 Qdrant 泄漏的测试 collection 目录")
    parser.add_argument(
        "--qdrant-url",
        default="http://127.0.0.1:6333",
        help="Qdrant 地址（用于取『认得的』列表）",
    )
    parser.add_argument(
        "--storage",
        default=str(REPO_ROOT / "tools" / "qdrant" / "storage" / "collections"),
        help="Qdrant collections 存储目录",
    )
    parser.add_argument("--yes", action="store_true", help="真正删除（默认只列出）")
    args = parser.parse_args()

    root = Path(args.storage)
    if not root.is_dir():
        print(f"[FAIL] 找不到存储目录：{root}")
        return 1

    known = _known_collections(args.qdrant_url)
    print()
    print("=" * 62)
    print(" Qdrant 测试 collection 泄漏清理")
    print(f" 存储目录：{root}")
    print(f" Qdrant 认得 {len(known)} 个：{', '.join(known) or '（取不到列表）'}")
    print("=" * 62)

    orphans = sorted(
        path
        for path in root.iterdir()
        if path.is_dir() and path.name.startswith(TEST_PREFIX) and path.name not in known
    )
    if not orphans:
        print("\n没有泄漏的测试目录 ✓\n")
        return 0

    total = 0.0
    print(f"\n发现 {len(orphans)} 个泄漏目录：")
    for path in orphans:
        size = _size_mb(path)
        total += size
        print(f"  {path.name:<34} {size:8.0f} MB")
    print(f"\n合计约 {total / 1024:.2f} GB")

    if not args.yes:
        print("\n（默认只列出，不删任何东西）确认后重跑并加 --yes：")
        print(f"  {sys.executable} tools\\clean_qdrant_orphans.py --yes")
        print("⚠️ 执行前请确认**测试没在跑**，否则可能删掉正在进行用例的临时 collection。")
        print()
        return 0

    removed = 0
    for path in orphans:
        try:
            shutil.rmtree(path)
            removed += 1
        except OSError as exc:
            print(f"  [FAIL] 删除 {path.name} 失败：{exc}")
    print(f"\n已删除 {removed}/{len(orphans)} 个目录，预计释放约 {total / 1024:.2f} GB")
    print()
    return 0 if removed == len(orphans) else 1


if __name__ == "__main__":
    raise SystemExit(main())
