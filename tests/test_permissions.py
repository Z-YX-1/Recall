"""文档权限（frontmatter → payload / 注册表）测试（roadmap R-39 待办 A）。

**为什么需要它**：R-40 的权限过滤按 payload 的 ``owner`` / ``visibility`` / ``groups``
判定可见范围，而此前 ``ingest.py`` 把这三个字段**硬编码**成 ``me`` / ``private`` / ``[]``
（且不读 frontmatter）⇒ 任何"给 Coze 单独发一把 token"的做法都会**检索不到任何东西**，
权限模型停在"全有或全无"。

本文件覆盖三件事：

1. **解析**：frontmatter 的键名与 payload 字段同名；非法值一律 **fail-closed**
   （写错只会更私有，绝不意外公开）；
2. **落盘**：payload 与注册表账本都拿到解析后的权限；
3. ⚠️ **改权限必须生效**：``content_hash`` 只对**正文**取哈希，所以"只改 ``visibility``
   不改正文"时哈希不变 —— 若跳过判定只看哈希，**权限改动将永远不生效**。
   这是本文件最重要的一条断言。
"""

from __future__ import annotations

from typing import Any

import pytest
from qdrant_client import models as qmodels

from ingest import _permissions_from, run_ingest
from recall.api import kb_search_core
from recall.models import Identity, SearchRequest
from tests.helpers import IngestEnv, ingest_args, write_note

_BODY = """\
# 混合检索

混合检索把 dense 与 sparse 两路召回结果用 RRF 融合，再交给 bge-reranker-v2-m3 精排。
"""


def _note(*, owner: str = "me", visibility: str = "private", groups: str = "[]") -> str:
    """拼一篇带 frontmatter 的笔记（正文固定，便于"只改权限"的用例）。"""
    return f"---\nowner: {owner}\nvisibility: {visibility}\ngroups: {groups}\n---\n\n{_BODY}"


async def _payloads(env: IngestEnv, doc_id: str) -> list[dict[str, Any]]:
    """读某文档在 Qdrant 里的全部 payload。"""
    points, _ = await env.store.client.scroll(
        collection_name=env.collection,
        scroll_filter=qmodels.Filter(
            must=[qmodels.FieldCondition(key="doc_id", match=qmodels.MatchValue(value=doc_id))]
        ),
        limit=100,
        with_payload=True,
    )
    return [dict(point.payload or {}) for point in points]


# --------------------------------------------------------------------------- 解析（fail-closed）


def test_missing_fields_fall_back_to_the_most_private_defaults() -> None:
    """没写权限字段 ⇒ ``me`` / ``private`` / ``[]``（与历史行为一致）。"""
    assert _permissions_from({}, doc_id="d") == ("me", "private", [])


def test_permission_fields_are_read_from_frontmatter() -> None:
    """键名与 payload 字段同名，直接取用。"""
    parsed = _permissions_from(
        {"owner": "alice", "visibility": "public", "groups": ["team-a", "team-b"]},
        doc_id="d",
    )

    assert parsed == ("alice", "public", ["team-a", "team-b"])


def test_groups_accepts_a_comma_separated_string() -> None:
    """YAML 里写成 ``groups: a, b`` 也认（人写 frontmatter 时两种写法都常见）。"""
    assert _permissions_from({"groups": "a, b"}, doc_id="d")[2] == ["a", "b"]


def test_unknown_visibility_is_treated_as_private() -> None:
    """**fail-closed**：拼错 ``public`` 只会更私有，绝不意外公开。"""
    assert _permissions_from({"visibility": "publci"}, doc_id="d")[1] == "private"
    assert _permissions_from({"visibility": "PUBLIC"}, doc_id="d")[1] == "private"


@pytest.mark.parametrize("bad", [123, 1.5, {"a": 1}])
def test_non_string_permission_values_fall_back(bad: object) -> None:
    """类型不对 ⇒ 回落默认值（并且会记 WARNING 便于排查）。"""
    parsed = _permissions_from({"owner": bad, "groups": bad}, doc_id="d")

    assert parsed == ("me", "private", [])


def test_blank_strings_fall_back_too() -> None:
    """空串/纯空白等同于没写。"""
    assert _permissions_from({"owner": "   ", "visibility": ""}, doc_id="d") == (
        "me",
        "private",
        [],
    )


# --------------------------------------------------------------------------- 落盘


async def test_frontmatter_permissions_reach_payload_and_registry(ingest_env: IngestEnv) -> None:
    """解析结果必须同时进 payload（供过滤）与注册表账本（供对账）。"""
    write_note(
        ingest_env.vault,
        "公开笔记.md",
        _note(owner="alice", visibility="public", groups="[team-a]"),
    )
    report = await run_ingest(ingest_args(ingest_env))
    assert report.failed == []

    payloads = await _payloads(ingest_env, "公开笔记")
    assert payloads, "应当写入了 point"
    assert {item["owner"] for item in payloads} == {"alice"}
    assert {item["visibility"] for item in payloads} == {"public"}
    assert {tuple(item["groups"]) for item in payloads} == {("team-a",)}

    record = await _registry_record(ingest_env, "公开笔记")
    assert (record.owner, record.visibility) == ("alice", "public")


async def _registry_record(env: IngestEnv, doc_id: str) -> Any:
    """读注册表里的文档记录。"""
    from recall.registry import Registry

    registry = Registry(env.registry_db)
    await registry.initialize()
    record = await registry.get(doc_id)
    assert record is not None, f"注册表里应有 {doc_id}"
    return record


# ------------------------------------------------------- 改权限必须生效（本文件的核心）


async def test_changing_only_permissions_is_not_skipped_by_the_hash_shortcut(
    ingest_env: IngestEnv,
) -> None:
    """⚠️ **只改 ``visibility``、正文一字不动** ⇒ 必须重新索引，权限才会更新。

    ``content_hash`` 只对正文取哈希（``RawDoc.text`` 不含 frontmatter），所以这种改动
    **哈希完全不变**。若跳过判定只看哈希，权限改动会被账本快路径吞掉、**永远不生效** ——
    这正是本用例要钉住的陷阱。
    """
    write_note(ingest_env.vault, "笔记.md", _note(visibility="private"))
    first = await run_ingest(ingest_args(ingest_env))
    assert first.indexed_docs == 1
    assert {item["visibility"] for item in await _payloads(ingest_env, "笔记")} == {"private"}

    # 正文完全相同，只把 visibility 改成 public
    write_note(ingest_env.vault, "笔记.md", _note(visibility="public"))
    second = await run_ingest(ingest_args(ingest_env))

    assert second.indexed_docs == 1, "改权限必须触发重索引（不能走哈希快路径跳过）"
    assert second.skipped == 0
    assert {item["visibility"] for item in await _payloads(ingest_env, "笔记")} == {"public"}


async def test_unchanged_permissions_still_skip(ingest_env: IngestEnv) -> None:
    """权限与正文都没变 ⇒ 仍然整篇跳过（幂等机制 1 不能被这次改动破坏）。"""
    write_note(ingest_env.vault, "笔记.md", _note(visibility="public"))
    await run_ingest(ingest_args(ingest_env))

    again = await run_ingest(ingest_args(ingest_env))

    assert again.skipped == 1
    assert again.indexed_docs == 0


# ------------------------------------------------------- 权限真的影响检索（R-39 场景）


async def test_public_note_is_visible_to_another_identity(
    ingest_env: IngestEnv, api_service: object
) -> None:
    """把笔记标成 ``public`` ⇒ **别的身份也能检索到**（这就是 R-39 需要的效果）。

    两侧都断言，才算把因果钉住：``coze`` 看得到公开笔记、看不到私有笔记；
    而所有者 ``me`` 两者都能看到。
    """
    del api_service  # 仅用它的副作用：把服务单例指向测试 collection
    write_note(ingest_env.vault, "公开.md", _note(visibility="public"))
    write_note(ingest_env.vault, "私有.md", _note(visibility="private"))
    report = await run_ingest(ingest_args(ingest_env))
    assert report.failed == []

    stranger = Identity(user="coze", groups=[])
    as_stranger = await kb_search_core(
        SearchRequest(query="混合检索把两路召回怎么融合", top_k=10), stranger
    )
    as_owner = await kb_search_core(
        SearchRequest(query="混合检索把两路召回怎么融合", top_k=10), Identity()
    )

    stranger_sources = {item.source_uri for item in as_stranger.evidence}
    owner_sources = {item.source_uri for item in as_owner.evidence}
    assert "公开.md" in stranger_sources
    assert "私有.md" not in stranger_sources
    assert "公开.md" in owner_sources and "私有.md" in owner_sources
