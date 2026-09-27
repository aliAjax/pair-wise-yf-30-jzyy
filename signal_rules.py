"""Safety signal detection rules.

判定规则单独维护，不依赖数据库和 HTTP 层：
- 按产品 + 事件词归并有效案例（已合并的来源案例只跟随目标案例，不计入）
- 同一组合案例数达到阈值（默认 3 例）或出现死亡即命中信号
"""
from __future__ import annotations

from typing import Any, Iterable

# 同一产品+事件词组合达到多少例即建立信号
CASE_COUNT_THRESHOLD = 3

FATAL_RULE = "fatal_outcome"
CASE_COUNT_RULE = "case_count_threshold"


def normalize_term(value: Any) -> str:
    """产品名/事件词归并键：去除多余空白并忽略大小写。"""
    return " ".join(str(value or "").split()).casefold()


def is_valid_case(case: dict[str, Any]) -> bool:
    """有效案例：合并产生的来源案例不再独立计数（其来源已跟随目标案例）。"""
    return case.get("status") != "merged"


def evaluate_rules(group: dict[str, Any]) -> list[str]:
    rules: list[str] = []
    if group["fatal_count"] > 0:
        rules.append(FATAL_RULE)
    if group["case_count"] >= CASE_COUNT_THRESHOLD:
        rules.append(CASE_COUNT_RULE)
    return rules


def build_groups(cases: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """把有效案例按 (产品, 事件词) 归并，并统计例数、严重、死亡和涉及区域。"""
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    order: list[tuple[str, str]] = []
    for case in cases:
        if not is_valid_case(case):
            continue
        product = str(case.get("product", "")).strip()
        event_term = str(case.get("event_term", "")).strip()
        key = (normalize_term(product), normalize_term(event_term))
        group = grouped.get(key)
        if group is None:
            group = {
                "product": product,
                "event_term": event_term,
                "product_key": key[0],
                "event_key": key[1],
                "cases": [],
                "case_count": 0,
                "serious_count": 0,
                "fatal_count": 0,
                "regions": set(),
            }
            grouped[key] = group
            order.append(key)
        group["cases"].append(case)
        group["case_count"] += 1
        if case.get("serious"):
            group["serious_count"] += 1
        if case.get("fatal"):
            group["fatal_count"] += 1
        if case.get("region"):
            group["regions"].add(case["region"])

    result: list[dict[str, Any]] = []
    for key in order:
        group = grouped[key]
        group["regions"] = sorted(group["regions"])
        group["matched_rules"] = evaluate_rules(group)
        result.append(group)
    return result


def rule_snapshot(matched_rules: list[str]) -> dict[str, Any]:
    """记录信号建立时命中的规则和阈值，便于事后追溯。"""
    return {
        "thresholds": {"case_count": CASE_COUNT_THRESHOLD},
        "matched_rules": matched_rules,
    }
