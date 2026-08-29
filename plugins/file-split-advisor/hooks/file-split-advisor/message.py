#!/usr/bin/env python3
"""additionalContext 文面組み立て。ファイルシステムアクセスなし。

事実文スタイルで統一する (命令形は使わない — 公式ドキュメントが「命令文は
プロンプトインジェクション対策に誤検知されうる」と明記しているため)。
"""
from __future__ import annotations

from pathlib import Path

from judge import Verdict
from metrics import Metrics

_TIER_SEQUENCE = ("note", "review", "warn", "strong")

_SIGNAL_FALLBACK_LABELS: dict[str, str] = {
    "import_diversity": "import カテゴリ多様性",
    "vague_filename": "命名が抽象的",
    "def_count": "定義数過多",
    "control_flow_density": "制御フロー密度高",
}


def _display_tiers(tier: str) -> tuple[str, str]:
    """目安として表示する 2 tier (判定 tier + 隣接 tier) を選ぶ。

    以前は常に review/warn の固定ペアを表示していたため、note/strong 判定時に
    実際の判定 tier の閾値が表示されない (無関係な review/warn の数値だけが
    出る) 問題があった。判定 tier が最上位 (strong) なら「1 つ下 + 自身」、
    それ以外は「自身 + 1 つ上」を表示する。tier が ``ok`` (emit されないため
    通常は到達しない) の場合は防御的に先頭 2 tier にフォールバックする。
    """
    if tier not in _TIER_SEQUENCE:
        return _TIER_SEQUENCE[0], _TIER_SEQUENCE[1]
    idx = _TIER_SEQUENCE.index(tier)
    if idx + 1 < len(_TIER_SEQUENCE):
        return _TIER_SEQUENCE[idx], _TIER_SEQUENCE[idx + 1]
    return _TIER_SEQUENCE[idx - 1], _TIER_SEQUENCE[idx]


def _format_multiplier(value: float) -> str:
    """1.0 / 1.15 / 1.6 等を末尾ゼロを詰めた形式で表示する (常に小数点以下 1 桁以上)。"""
    text = f"{value:.2f}".rstrip("0")
    return text if not text.endswith(".") else f"{text}0"


def _multiplier_breakdown(language: str, verdict: Verdict) -> str:
    """実効閾値の根拠になった係数を「言語 係数 (× 宣言的 係数)」の形で示す。

    role (test 係数) は既存の role_note 表示と役割が重複するためここには
    含めない (test: 閾値 1.6倍 という別の注記が既にある)。
    """
    parts = [f"{language} {_format_multiplier(verdict.applied_multipliers['language'])}"]
    declarative = verdict.applied_multipliers.get("declarative", 1.0)
    if declarative != 1.0:
        parts.append(f"宣言的 {_format_multiplier(declarative)}")
    return " × ".join(parts)


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
    tier_a, tier_b = _display_tiers(verdict.tier)
    thresholds_str = " ".join(
        f"{tier}={round(verdict.thresholds[tier])}" for tier in (tier_a, tier_b)
    )
    breakdown = _multiplier_breakdown(language, verdict)
    # scale (FILE_SPLIT_ADVISOR_SCALE) は applied_multipliers に含めない
    # (judge.py の設計判断: グローバル config であり per-file の推論シグナル
    # ではないため) が、role_note と同じ形の専用 parenthetical で別枠表示する。
    # これが無いと、倍率が 1.0 以外のとき「目安」の数値が printed 係数だけから
    # 導出できなくなる (P2-1)。
    scale_note = (
        f" (全体 {_format_multiplier(verdict.scale)}倍)" if verdict.scale != 1.0 else ""
    )
    role_note = " (test: 閾値 1.6倍)" if role == "test" else ""
    header = (
        f"静的解析メモ (file-split-advisor): {path}\n"
        f"行数: {metrics.line_count} (言語: {language}, 判定: {verdict.tier}"
        f" / 目安 {thresholds_str} ({breakdown}){scale_note}{role_note})"
    )

    if verdict.signals:
        details = " / ".join(_format_signal(key, metrics) for key in verdict.signals)
        signal_line = f"検出シグナル: {details}"
    elif verdict.applied_multipliers.get("declarative", 1.0) != 1.0:
        # signal_count == 0 (行数のみが emit 根拠) の透明性確保: 何が根拠で
        # 出力されたかを隠さない。宣言的コードの推測は、実際に宣言的緩和が
        # 適用された (control_flow_density < 0.02) ときだけ表示する — 適用
        # されていないのに一律で表示すると、制御フロー密度の高いファイル
        # (例: 分岐の多いハンドラ) にも誤って「宣言的では」と表示してしまう。
        signal_line = (
            "検出された構造シグナル: なし (行数のみが基準に該当。宣言的なコード"
            "(ルーティング定義・型定義など) の可能性があります)"
        )
    else:
        signal_line = "検出された構造シグナル: なし (行数のみが基準に該当)"

    footer = (
        "行数は分割要否の直接的根拠ではなく、責務凝集・変更理由の単一性・可読性のレビューを"
        "促す目安として提示しています。"
    )

    return "\n".join([header, signal_line, footer])
