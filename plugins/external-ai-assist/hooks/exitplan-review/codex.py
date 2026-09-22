"""Codex によるプランレビュー (要件・アーキ観点)。

プロンプト (prompts/planning-codex.md) とプラン本文を連結し、**stdin 一本**で渡す
(`codex exec … -`。`-` が stdin 指定)。

0.9.1 まではプロンプトを引数、プラン本文を stdin に分けており、codex の
「引数のプロンプトと piped stdin を併用すると stdin が block として追記される」挙動に
依存していた。この併用挙動が無い版では **stdin が無視されてプラン本文抜きでレビューが
走る** — 結果は当然 clean にならず、利用者から見ると「プランを見ていないレビューで
差し戻された」ことになる。しかも失敗が静かなので気付けない。

stdin 一本化でこの版依存を外す。`-` を解さない版では引数が足りず非 0 終了になり、
`subproc.run_for_output` が None を返して **fail-open** (レビューなしで通す) に倒れる。
レビュアーの失敗は fail-open という既存の契約と同じ側で、静かな誤差し戻しより軽い。
引数長の上限に当たるリスクも同時に消える (長いプランを argv に載せない)。

起動と存在確認は **`_common.backends` の registry 経由** (0.12.0)。argv
(`codex exec -s read-only --ephemeral -`) も stdin 一本という渡し方も registry 側に
そのまま移しただけで、0.11.0 から変わらない。この module に残るのは **hook 固有のもの**
(プロンプト / timeout の既定値と上限 / 出力の切り詰め) だけ。
`_common` は `__main__.py` (テストでは `tests/_testutil.py`) が sys.path に載せる。
"""
from __future__ import annotations

from pathlib import Path

from _common import backends, settings

NAME = backends.codex.NAME
BINARY = backends.codex.BINARY

#: 既定の timeout。**0.6.0 で 1500 → 600 に短縮** (挙動変更)。cursor と並列に走るので
#: 承認前の待ち時間は max(cursor, codex) = 25 分 → 10 分になる。長考させたい場合は
#: `EXTERNAL_AI_PLAN_REVIEW_TIMEOUT=1500` で従来値に戻せる。
TIMEOUT_SEC = 600

#: env で伸ばせる上限 (従来の既定値)。根拠は cursor.py の同名定数を参照。
MAX_TIMEOUT_SEC = 1500

MAX_OUTPUT_BYTES = 16000

ENV_TIMEOUT = "EXTERNAL_AI_PLAN_REVIEW_TIMEOUT"

_PROMPT_FILE = Path(__file__).parent / "prompts" / "planning-codex.md"


def is_available() -> bool:
    return backends.codex.is_available()


def timeout_sec() -> float:
    """実効 timeout。未設定なら `TIMEOUT_SEC`、`MAX_TIMEOUT_SEC` で clamp。"""
    return settings.duration(ENV_TIMEOUT, TIMEOUT_SEC, MAX_TIMEOUT_SEC)


def review(plan_text: str, *, cwd: str | None = None) -> str | None:
    """Codex でプランをレビューし、整形済み結果を返す。失敗時は None。

    `cwd` は git 作業ツリーの root を渡すこと (`__main__.main` が payload の cwd から
    解決して渡す)。未指定 (None) だと codex は hook プロセス自身の cwd で起動され、
    Claude Code をサブディレクトリで起動したセッションではリポジトリ全体を見ずに
    レビューすることになる (内部バックログ)。
    """
    try:
        template = _PROMPT_FILE.read_text(encoding="utf-8")
    except OSError:
        return None

    full_prompt = f"{template}\n\n---\n\n## レビュー対象プラン\n\n{plan_text}"
    result = backends.codex.run(full_prompt, cwd=cwd, timeout=timeout_sec())
    result = result.truncated(MAX_OUTPUT_BYTES)
    return result.text if result.is_ok else None
