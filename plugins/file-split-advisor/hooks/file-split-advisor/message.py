#!/usr/bin/env python3
"""additionalContext 文面組み立て。ファイルシステムアクセスなし。

事実文スタイルで統一する (命令形は使わない — 公式ドキュメントが「命令文は
プロンプトインジェクション対策に誤検知されうる」と明記しているため)。
"""
from __future__ import annotations

from pathlib import Path

from judge import Verdict
from metrics import Metrics

_THRESHOLD_DISPLAY_TIERS = ("review", "warn")

_SIGNAL_FALLBACK_LABELS: dict[str, str] = {
    "import_diversity": "import カテゴリ多様性",
    "vague_filename": "命名が抽象的",
    "def_count": "定義数過多",
    "control_flow_density": "制御フロー密度高",
}


def _format_signal(key: str, metrics: Metrics) -> str:
    if key == "import_diversity":
        names = ", ".join(metrics.import_categories)
        return f"import カテゴリ多様性 {metrics.import_category_count}種 ({names})"
    if key == "vague_filename":
        return "命名が抽象的 (utils/common/helper 等の総称語のみ)"
    if key == "def_count":
        return f"定義数 {metrics.def_count}"
    if key == "control_flow_density":
        pct = round(metrics.control_flow_density * 100)
        return f"制御フロー密度 {pct}%"
    return _SIGNAL_FALLBACK_LABELS.get(key, key)


def build(path: Path, language: str, role: str, verdict: Verdict, metrics: Metrics) -> str:
    thresholds_str = " ".join(
        f"{tier}={round(verdict.thresholds[tier])}"
        for tier in _THRESHOLD_DISPLAY_TIERS
        if tier in verdict.thresholds
    )
    role_note = " (test: 閾値 1.6倍)" if role == "test" else ""
    header = (
        f"静的解析メモ (file-split-advisor): {path}\n"
        f"行数: {metrics.line_count} (言語: {language}, 判定: {verdict.tier}"
        f" / 目安 {thresholds_str}{role_note})"
    )

    if verdict.signals:
        details = " / ".join(_format_signal(key, metrics) for key in verdict.signals)
        signal_line = f"検出シグナル: {details}"
    else:
        # signal_count == 0 (行数のみが emit 根拠) の透明性確保: 何が根拠で
        # 出力されたかを隠さない。
        signal_line = (
            "検出された構造シグナル: なし (行数のみが基準に該当。宣言的なコード"
            "(ルーティング定義・型定義など) の可能性があります)"
        )

    footer = (
        "行数は分割要否の直接的根拠ではなく、責務凝集・変更理由の単一性・可読性のレビューを"
        "促す目安として提示しています。"
    )

    return "\n".join([header, signal_line, footer])
