#!/usr/bin/env python3
"""純粋関数: 閾値テーブル・tier/emit 判定。ファイルシステムアクセスなし。

行数閾値は「設計ルールではなくレビューを発火させるメトリクス」という前提に
立ち、line_count 単独のゲートではなく言語/role で調整した行数 tier と構造
シグナル数を組み合わせて emit を判定する。
"""
from __future__ import annotations

from dataclasses import dataclass

from metrics import Metrics

BASE_THRESHOLDS: dict[str, float] = {
    "note": 150,
    "review": 300,
    "warn": 500,
    "strong": 800,
}

TIER_ORDER: tuple[str, ...] = ("ok", "note", "review", "warn", "strong")
_TIER_PROGRESSION = ("note", "review", "warn", "strong")

LANGUAGE_MULTIPLIER: dict[str, float] = {
    "python": 0.7,
    "javascript": 1.0,
    "typescript": 1.0,
    "javascriptreact": 1.15,
    "typescriptreact": 1.15,
    "java": 1.5,
    "csharp": 1.5,
    "kotlin": 1.4,
    "dart": 1.3,
    "go": 1.0,
    "rust": 1.1,
    "ruby": 1.0,
    "php": 1.1,
    "generic": 1.0,
}

ROLE_MULTIPLIER: dict[str, float] = {
    "test": 1.6,
    "normal": 1.0,
}

DECLARATIVE_THRESHOLD = 0.02
DECLARATIVE_RELAXATION = 1.6
HIGH_DENSITY_SIGNAL_THRESHOLD = 0.25
IMPORT_DIVERSITY_SIGNAL_THRESHOLD = 4
DEF_COUNT_SIGNAL_THRESHOLD = 20
NOTE_PROMOTION_SIGNAL_COUNT = 2


@dataclass(frozen=True)
class Verdict:
    tier: str
    should_emit: bool
    signals: tuple[str, ...]
    thresholds: dict[str, float]


def _effective_thresholds(language: str, role: str, metrics: Metrics) -> dict[str, float]:
    multiplier = LANGUAGE_MULTIPLIER.get(language, 1.0) * ROLE_MULTIPLIER.get(role, 1.0)
    if metrics.control_flow_density < DECLARATIVE_THRESHOLD:
        multiplier *= DECLARATIVE_RELAXATION
    return {tier: base * multiplier for tier, base in BASE_THRESHOLDS.items()}


def _compute_tier(line_count: int, thresholds: dict[str, float]) -> str:
    tier = "ok"
    for candidate in _TIER_PROGRESSION:
        if line_count >= thresholds[candidate]:
            tier = candidate
    return tier


def _collect_signals(metrics: Metrics, role: str) -> tuple[str, ...]:
    signals = []
    if metrics.import_category_count >= IMPORT_DIVERSITY_SIGNAL_THRESHOLD:
        signals.append("import_diversity")
    if metrics.vague_filename:
        signals.append("vague_filename")
    if role != "test" and metrics.def_count >= DEF_COUNT_SIGNAL_THRESHOLD:
        signals.append("def_count")
    is_declarative = metrics.control_flow_density < DECLARATIVE_THRESHOLD
    if not is_declarative and metrics.control_flow_density >= HIGH_DENSITY_SIGNAL_THRESHOLD:
        signals.append("control_flow_density")
    return tuple(signals)


def judge(metrics: Metrics, language: str, role: str) -> Verdict:
    """emit 判定行列:

    - tier が review/warn/strong → 常に emit (signal 数によらない)
    - tier が note → signal 数 >= NOTE_PROMOTION_SIGNAL_COUNT のときのみ emit
    - tier が ok → 常に emit しない

    review 以上は signal 数によらず emit する設計は意図的な判断 (README/CLAUDE.md
    に根拠を記載): 行数の大きさそのものをレビュー発火の十分条件として扱う一方、
    構造シグナルは effective_thresholds (言語/role/宣言的緩和) と note tier の
    昇格判定という 2 箇所で行数評価の解像度を上げる役割を持つ。
    """
    thresholds = _effective_thresholds(language, role, metrics)
    tier = _compute_tier(metrics.line_count, thresholds)
    signals = _collect_signals(metrics, role)

    if tier in ("review", "warn", "strong"):
        should_emit = True
    elif tier == "note":
        should_emit = len(signals) >= NOTE_PROMOTION_SIGNAL_COUNT
    else:
        should_emit = False

    return Verdict(tier=tier, should_emit=should_emit, signals=signals, thresholds=thresholds)
