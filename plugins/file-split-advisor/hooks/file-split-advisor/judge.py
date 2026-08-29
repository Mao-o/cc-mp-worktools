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
    # 未登録の言語は 1.0 (``.get(language, 1.0)``)。
    #
    # python は 0.7 だったが 0.2.0 で 1.0 に見直した: review 閾値が 210 行と
    # なり、pylint の too-many-lines (既定 1000) や ESLint の max-lines
    # (既定 300) と比べて突出して厳しく、通常の実装ファイルが初回編集で
    # 発火する主因になっていた。
    "python": 1.0,
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
    # 0.2.0 で拡張子 allowlist に追加した言語のうち、java/csharp/kotlin と同じ
    # 「宣言・ヘッダのボイラープレートで行数が伸びる」クラスにあたるものだけ
    # 明示する。それ以外 (scala/elixir/shell/lua/perl/r/clojure/haskell/erlang/
    # julia/zig/nim/groovy) は allowlist 化前の generic と同じ 1.0 に据え置き。
    "swift": 1.2,
    "c": 1.2,
    "cpp": 1.3,
    "objectivec": 1.4,
    "powershell": 1.2,
    "vue": 1.15,
    "svelte": 1.15,
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
REVIEW_PROMOTION_SIGNAL_COUNT = 1


@dataclass(frozen=True)
class Verdict:
    tier: str
    should_emit: bool
    signals: tuple[str, ...]
    thresholds: dict[str, float]
    applied_multipliers: dict[str, float]
    scale: float = 1.0


def _effective_thresholds(
    language: str, role: str, metrics: Metrics, scale: float = 1.0
) -> tuple[dict[str, float], dict[str, float]]:
    """実効閾値と、その根拠になった個別係数 (``applied_multipliers``) を返す。

    ``applied_multipliers`` は message.py が「なぜこの閾値になったか」を
    メモに明示するために使う (language は常に、declarative は実際に緩和が
    効いたときだけ 1.0 でない値になる)。role はここでは記録するが、
    message.py 側は既存の role_note 表示と役割が重複するため breakdown には
    含めない (test 係数の可視化は role_note に残す)。

    ``scale`` (``FILE_SPLIT_ADVISOR_SCALE``) はユーザーが設定するグローバルな
    倍率で、ファイル個別の推論シグナルではないため ``applied_multipliers`` には
    含めない (message.py の breakdown 表示対象外)。実効閾値の計算には反映する。
    ただし表示から完全に消すと「目安の数値が printed 係数から導出できない」
    (0.3.0 で顕在化した欠陥) になるため、``Verdict.scale`` という別フィールドで
    保持し、message.py はそこから role_note と同じ形の専用 parenthetical
    (``(全体 N倍)``) を組み立てる。
    """
    is_declarative = metrics.control_flow_density < DECLARATIVE_THRESHOLD
    multipliers = {
        "language": LANGUAGE_MULTIPLIER.get(language, 1.0),
        "role": ROLE_MULTIPLIER.get(role, 1.0),
        "declarative": DECLARATIVE_RELAXATION if is_declarative else 1.0,
    }
    combined = (
        multipliers["language"] * multipliers["role"] * multipliers["declarative"] * scale
    )
    thresholds = {tier: base * combined for tier, base in BASE_THRESHOLDS.items()}
    return thresholds, multipliers


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


def judge(metrics: Metrics, language: str, role: str, scale: float = 1.0) -> Verdict:
    """emit 判定行列:

    - tier が warn/strong → 常に emit (signal 数によらない)
    - tier が review → signal 数 >= REVIEW_PROMOTION_SIGNAL_COUNT のときのみ emit
    - tier が note → signal 数 >= NOTE_PROMOTION_SIGNAL_COUNT のときのみ emit
    - tier が ok → 常に emit しない

    0.1.0 では review 以上を signal 数によらず emit していたが、行数だけを根拠に
    した通知が emit 全体の大半を占め、通常サイズの実装ファイルが初回編集で発火
    する状態になっていた (根拠は CHANGELOG 0.2.0 の実測)。「大きさそのものを
    レビュー発火の十分条件として扱う」設計は warn 以上に引き上げ、review tier は
    「大きく、かつ責務混在の兆候が 1 つ以上ある」ときに限定する。

    構造シグナルは行数判定を上書きする独立ゲートではなく、(1) effective_thresholds
    (言語/role/宣言的緩和)、(2) note tier の昇格判定、(3) review tier の昇格判定
    の 3 箇所で行数評価の解像度を上げる役割を持つ。

    ``scale`` (既定 1.0) は ``FILE_SPLIT_ADVISOR_SCALE`` から呼び出し側が渡す
    グローバルな倍率。全閾値に一律で掛かる (0.3.0)。
    """
    thresholds, multipliers = _effective_thresholds(language, role, metrics, scale)
    tier = _compute_tier(metrics.line_count, thresholds)
    signals = _collect_signals(metrics, role)

    if tier in ("warn", "strong"):
        should_emit = True
    elif tier == "review":
        should_emit = len(signals) >= REVIEW_PROMOTION_SIGNAL_COUNT
    elif tier == "note":
        should_emit = len(signals) >= NOTE_PROMOTION_SIGNAL_COUNT
    else:
        should_emit = False

    return Verdict(
        tier=tier,
        should_emit=should_emit,
        signals=signals,
        thresholds=thresholds,
        applied_multipliers=multipliers,
        scale=scale,
    )
