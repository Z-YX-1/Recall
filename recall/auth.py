"""身份与权限收敛（tech.md §7；code_standards §7；roadmap R-21）。

**S1（现在）**：无身份模型，:func:`get_identity` 硬编码 ``{user:"me", groups:["owner"]}``。
接口与字段第一天就位，S2 起只换实现不换链路（tech.md §7「现在就埋的三件套」）。

``filter`` 语义 = **只能收窄不能放宽**：客户端 filter 与身份可见范围取**交集**，
服务端绝不信客户端传参（code_standards §7）。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from fastapi import Request
from pydantic import ValidationError
from qdrant_client import models

from recall.models import Identity

PUBLIC_VISIBILITY = "public"
"""公开可见的 ``visibility`` 取值。"""


class InvalidFilterError(ValueError):
    """客户端 ``filter`` 不是合法的 Qdrant 过滤表达式。"""


def get_identity(request: Request | None = None) -> Identity:
    """解析调用者身份（S1 占位实现）。

    S1 无身份模型，恒定返回 ``Identity()``（``me`` / ``owner``）。S2 起换成为
    「API key → 身份中间件」，本函数签名与调用点不变（tech.md §7）。

    Args:
        request: 当前 HTTP 请求；S1 不读取，保留参数以固化链路。

    Returns:
        调用者身份。
    """
    del request  # S1 无身份模型，参数仅为固化调用链
    return Identity()


def build_scope_filter(identity: Identity) -> models.Filter:
    """身份可见范围：``owner 是我`` 或 ``visibility=public`` 或 ``groups 相交``。

    Args:
        identity: 调用者身份。

    Returns:
        Qdrant ``should``（OR）形式的可见范围过滤器；``groups`` 为空时不生成该条件。
    """
    should: list[models.Condition] = [
        models.FieldCondition(key="owner", match=models.MatchValue(value=identity.user)),
        models.FieldCondition(key="visibility", match=models.MatchValue(value=PUBLIC_VISIBILITY)),
    ]
    if identity.groups:
        should.append(
            models.FieldCondition(key="groups", match=models.MatchAny(any=identity.groups))
        )
    return models.Filter(should=should)


def effective_filter(
    identity: Identity, client_filter: Mapping[str, Any] | None = None
) -> models.Filter:
    """把客户端 filter 收敛进身份可见范围（**只能收窄不能放宽**）。

    实现方式为**合取**（``Filter(must=[scope, client])``），而不是把两边的 ``should``
    并起来——后者会变成并集，等于放宽可见范围。

    Args:
        identity: 调用者身份。
        client_filter: 客户端传入的 Qdrant 过滤表达式；空表示不额外收窄。

    Returns:
        服务端强制施加的过滤条件。

    Raises:
        InvalidFilterError: 客户端 filter 结构非法。
    """
    scope = build_scope_filter(identity)
    if not client_filter:
        return scope
    payload = dict(client_filter)
    validate_client_filter(payload)
    try:
        parsed = models.Filter.model_validate(payload)
    except ValidationError as exc:
        raise InvalidFilterError(f"filter 结构非法：{exc.error_count()} 处错误") from exc
    return intersect(scope, parsed)


_FILTER_GROUPS = ("must", "should", "must_not")
_FILTER_KEYS = frozenset({"must", "should", "must_not", "min_should"})
_FIELD_CONDITION_PAYLOADS = frozenset(
    {
        "match",
        "range",
        "geo_bounding_box",
        "geo_radius",
        "geo_polygon",
        "values_count",
        "is_empty",
        "is_null",
    }
)
_OTHER_CONDITION_KEYS = frozenset({"has_id", "has_vector", "nested", "is_empty", "is_null"})


def validate_client_filter(raw: Mapping[str, Any]) -> None:
    """校验客户端 ``filter`` 的结构。

    ⚠️ 必须自己校验：Qdrant 的 ``Condition`` 联合类型里带 ``Any``（还有
    ``tuple[str, Any]``），``Filter.model_validate`` 对残缺条件照单全收，
    坏 filter 会一路带到 Qdrant 变成 500，而不是干净的 400。

    Args:
        raw: 客户端传入的过滤表达式。

    Raises:
        InvalidFilterError: 结构不是合法的 Qdrant 过滤表达式。
    """
    _validate_filter_body(dict(raw), "filter")


def _validate_filter_body(body: dict[str, Any], path: str) -> None:
    for key in body:
        if key not in _FILTER_KEYS:
            raise InvalidFilterError(f"{path} 含未知字段 {key!r}")
    for group in _FILTER_GROUPS:
        if group in body:
            for index, condition in enumerate(_condition_list(body[group], f"{path}.{group}")):
                _validate_condition(condition, f"{path}.{group}[{index}]")
    min_should = body.get("min_should")
    if min_should is not None:
        if not isinstance(min_should, dict):
            raise InvalidFilterError(f"{path}.min_should 必须是对象")
        conditions = _condition_list(min_should.get("conditions"), f"{path}.min_should.conditions")
        for index, condition in enumerate(conditions):
            _validate_condition(condition, f"{path}.min_should.conditions[{index}]")


def _condition_list(value: Any, path: str) -> list[Any]:  # noqa: ANN401 - 入参是未受信的 JSON
    if value is None:
        return []
    if isinstance(value, dict):
        return [value]
    if isinstance(value, (list, tuple)):
        return list(value)
    raise InvalidFilterError(f"{path} 必须是条件数组")


def _validate_condition(condition: Any, path: str) -> None:  # noqa: ANN401 - 入参是未受信的 JSON
    if not isinstance(condition, dict):
        raise InvalidFilterError(f"{path} 必须是对象")
    keys = set(condition)
    if keys & _FILTER_KEYS:
        _validate_filter_body(condition, path)  # 嵌套 filter 子树
        return
    if "key" in keys:
        if not keys & _FIELD_CONDITION_PAYLOADS:
            raise InvalidFilterError(f"{path} 的字段条件缺少 match / range 等判定体")
        return
    if keys & _OTHER_CONDITION_KEYS:
        return
    raise InvalidFilterError(f"{path} 不是合法的 Qdrant 过滤条件")


def intersect(scope: models.Filter, client: models.Filter) -> models.Filter:
    """两个过滤条件的**合取**（Qdrant 支持把 ``Filter`` 作为嵌套条件）。

    Args:
        scope: 身份可见范围。
        client: 客户端收窄条件。

    Returns:
        仅当两者同时成立时才命中的过滤器。
    """
    return models.Filter(must=[scope, client])
