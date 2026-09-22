"""胖端点与 prompt 组装测试（tech.md §6/§8；code_standards §8；roadmap R-29）。

LLM 调用用**注入的桩**替换（不产生真实 API 费用、不需要密钥），
其余链路（检索 → 组装 → JSON 解析 → 引用清洗 → 证据包）全部真跑。
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from fastmcp import Client

from ingest import run_ingest
from recall.api import ApiError, Service, app, kb_answer_core, kb_ingest_core, mcp
from recall.assemble import FIDELITY_RULES, assemble
from recall.llm import DeepSeekClient
from recall.models import AnswerRequest, Evidence, IngestRequest
from tests.helpers import IngestEnv, ingest_args, write_note

_RAG_NOTE = """\
# RAG 检索

## 混合检索

混合检索把 dense 与 sparse 两路召回结果用 RRF 融合，再交给 bge-reranker-v2-m3 精排。
检索质量主要取决于切分粒度，必须用 golden QA 评测校准。
"""

_COOKING_NOTE = """\
# 家常菜

## 红烧肉

五花肉切块冷水下锅焯水，加冰糖炒糖色，小火慢炖四十分钟收汁。
"""


class _StubLlm(DeepSeekClient):
    """把 ``complete_json`` 换成固定返回的桩（其余行为与真客户端一致）。"""

    def __init__(self, payload: dict[str, Any]) -> None:
        super().__init__(api_key=None, base_url="http://stub", model="stub")
        self._payload = payload
        self.calls: list[str] = []

    @property
    def available(self) -> bool:
        return True

    async def complete_json(
        self, prompt: str, *, system: str = "", max_tokens: int = 0
    ) -> dict[str, Any]:
        del system, max_tokens
        self.calls.append(prompt)
        return self._payload


def _evidence(ref_id: str, text: str = "证据正文") -> Evidence:
    return Evidence(
        ref_id=ref_id,
        source_uri=f"笔记{ref_id}.md",
        heading_path="标题 > 小节",
        text=text,
        score=0.9,
    )


async def _prepare_corpus(ingest_env: IngestEnv) -> None:
    write_note(ingest_env.vault, "RAG检索.md", _RAG_NOTE)
    write_note(ingest_env.vault, "家常菜.md", _COOKING_NOTE)
    report = await run_ingest(ingest_args(ingest_env))
    assert report.indexed_docs == 2


def _service(api_service: object) -> Service:
    assert isinstance(api_service, Service)
    return api_service


def _use_stub(api_service: object, payload: dict[str, Any]) -> _StubLlm:
    stub = _StubLlm(payload)
    _service(api_service).llm = stub
    return stub


def _unconfigured(api_service: object) -> None:
    _service(api_service).llm = DeepSeekClient(
        api_key=None, base_url="http://unused", model="unused"
    )


# --------------------------------------------------------------------------- assemble


def test_assemble_prompt_carries_rules_numbered_evidence_and_json_contract() -> None:
    prompt, ref_map = assemble([_evidence("1"), _evidence("2")])

    assert FIDELITY_RULES.strip() in prompt
    assert "[1] 来源：笔记1.md > 标题 > 小节" in prompt
    assert "[2] 来源：笔记2.md > 标题 > 小节" in prompt
    assert '"answer"' in prompt and '"citations"' in prompt
    assert ref_map == {"1": "笔记1.md > 标题 > 小节", "2": "笔记2.md > 标题 > 小节"}


def test_assemble_is_a_pure_function() -> None:
    evidence = [_evidence("1"), _evidence("2")]
    assert assemble(evidence) == assemble(evidence)


def test_assemble_respects_token_budget() -> None:
    long_text = "混合检索与预算控制。" * 400
    prompt, ref_map = assemble(
        [_evidence("1", long_text), _evidence("2", long_text)], max_tokens=50
    )

    assert list(ref_map) == ["1"]  # 第一条已超预算仍保留，第二条被截掉
    assert "[2]" not in prompt


def test_assemble_without_evidence_says_so() -> None:
    prompt, ref_map = assemble([])
    assert "（无证据）" in prompt
    assert ref_map == {}


# --------------------------------------------------------------------------- kb_answer


async def test_kb_answer_returns_cited_answer(ingest_env: IngestEnv, api_service: object) -> None:
    await _prepare_corpus(ingest_env)
    stub = _use_stub(api_service, {"answer": "切分粒度决定检索质量 [1]。", "citations": [1]})

    result = await kb_answer_core(AnswerRequest(query="检索质量取决于什么？"))

    assert result.answer.startswith("切分粒度")
    assert result.citations == [1]
    assert len(stub.calls) == 1
    assert "[1] 来源：" in stub.calls[0]
    # references 与 [n] 一一对应（索引 = n-1）
    assert result.references
    assert result.references[0]["ref_id"] == "1"
    assert result.references[0]["source_uri"]


async def test_kb_answer_drops_out_of_range_and_duplicate_citations(
    ingest_env: IngestEnv, api_service: object
) -> None:
    await _prepare_corpus(ingest_env)
    _use_stub(
        api_service,
        {"answer": "答案 [1][2][99]。", "citations": [1, 99, "2", 2, "abc", True]},
    )

    result = await kb_answer_core(AnswerRequest(query="检索质量取决于什么？"))

    assert result.citations == [1, 2]  # 越界 / 重复 / 非数字全部剔除，保序去重


async def test_kb_answer_without_evidence_does_not_call_llm(
    ingest_env: IngestEnv, api_service: object
) -> None:
    """无证据时禁止硬答（code_standards §5），且**不得**调用 LLM。"""
    stub = _use_stub(api_service, {"answer": "不该被调用", "citations": [1]})
    service = _service(api_service)
    empty_collection = f"{ingest_env.collection}-empty"
    await service.store.ensure_collection(
        empty_collection, embedding_model="bge-m3", embedding_version="v1", chunker="md-heading-v1"
    )
    service.collection = empty_collection
    try:
        result = await kb_answer_core(AnswerRequest(query="任何问题"))
    finally:
        await service.store.client.delete_collection(empty_collection)

    assert result.citations == []
    assert result.references == []
    assert "没有检索到" in result.answer
    assert stub.calls == []


async def test_kb_answer_reports_missing_api_key_as_503(
    ingest_env: IngestEnv, api_service: object
) -> None:
    await _prepare_corpus(ingest_env)
    _unconfigured(api_service)

    with pytest.raises(ApiError) as excinfo:
        await kb_answer_core(AnswerRequest(query="检索质量取决于什么？"))

    assert excinfo.value.code == "llm_not_configured"
    assert excinfo.value.status_code == 503


async def test_kb_answer_rest_endpoint_uses_error_envelope(
    ingest_env: IngestEnv, api_service: object
) -> None:
    await _prepare_corpus(ingest_env)
    _unconfigured(api_service)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://recall.test") as client:
        response = await client.post("/kb/answer", json={"query": "检索质量取决于什么？"})

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "llm_not_configured"


# --------------------------------------------------------------------------- MCP


async def test_answer_and_ingest_tools_declare_side_effects() -> None:
    async with Client(mcp) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}

    assert set(tools) == {"kb_search", "kb_answer", "kb_ingest", "kb_stats"}
    answer_desc = tools["kb_answer"].description or ""
    assert "副作用" in answer_desc and "DeepSeek" in answer_desc
    ingest_desc = tools["kb_ingest"].description or ""
    assert "副作用" in ingest_desc and "写操作" in ingest_desc
    assert "只读" in (tools["kb_search"].description or "")


async def test_kb_ingest_core_runs_the_pipeline(ingest_env: IngestEnv, api_service: object) -> None:
    del api_service
    write_note(ingest_env.vault, "RAG检索.md", _RAG_NOTE)

    summary = await kb_ingest_core(IngestRequest(mode="update", collection=ingest_env.collection))
    assert summary.indexed_docs == 1
    assert summary.collection == ingest_env.collection
    assert summary.failed == []

    again = await kb_ingest_core(IngestRequest(mode="update", collection=ingest_env.collection))
    assert again.skipped == 1
    assert again.indexed_docs == 0
