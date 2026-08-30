"""Cursor によるプランレビュー (既存コードベース整合観点、primary)。

プラン本文はプロンプト末尾に埋め込んで cursor agent の --print (読み取り専用 `--mode plan`) で渡す。
Cursor はセマンティック検索でコードベース全体を参照しながらレビューする。

起動は `_common.subproc` 経由 (独自 process group + timeout + 残出力の読み捨て)。
`_common` は `__main__.py` (テストでは `tests/_testutil.py`) が sys.path に載せる。
"""
from __future__ import annotations

from pathlib import Path

from _common import cursorcli, settings, subproc

NAME = cursorcli.NAME

#: 既定の timeout (0.6.0 で 600 のまま据置)。`EXTERNAL_AI_PLAN_REVIEW_TIMEOUT` で変更可。
TIMEOUT_SEC = 600

#: env で伸ばせる上限。hooks.json の ExitPlanMode timeout (1560s) から kill 猶予
#: (3 × KILL_GRACE_SEC) を引いた範囲に収める。超えるとハーネスの kill が先に来て
#: release_slot (枠を戻す) に到達しない (`tests/test_cli_timeout.py::TestTimeoutBudget`)。
MAX_TIMEOUT_SEC = 1500

MAX_OUTPUT_BYTES = 16000

ENV_TIMEOUT = "EXTERNAL_AI_PLAN_REVIEW_TIMEOUT"

_PROMPT_FILE = Path(__file__).parent / "prompts" / "planning-cursor.md"


def is_available() -> bool:
    return cursorcli.is_available()


def timeout_sec() -> float:
    """実効 timeout。未設定なら `TIMEOUT_SEC`、`MAX_TIMEOUT_SEC` で clamp。

    モジュール変数を呼び出しのたびに読む (テストが `TIMEOUT_SEC` を差し替えられるように
    束縛しない)。cursor と codex で同じ env を見るので、利用者が調整するつまみは 1 つ。
    """
    return settings.duration(ENV_TIMEOUT, TIMEOUT_SEC, MAX_TIMEOUT_SEC)


def review(plan_text: str, *, cwd: str | None = None) -> str | None:
    """Cursor でプランをレビューし、整形済み結果を返す。失敗時は None。

    `cwd` は git 作業ツリーの root を渡すこと (`__main__.main` が payload の cwd から
    解決して渡す)。未指定 (None) だと cursor は hook プロセス自身の cwd で起動され、
    Claude Code をサブディレクトリで起動したセッションでは cursor のワークスペースが
    リポジトリ全体からずれる (内部バックログ)。
    """
    try:
        template = _PROMPT_FILE.read_text(encoding="utf-8")
    except OSError:
        return None

    full_prompt = f"{template}\n\n---\n\n## レビュー対象プラン\n\n{plan_text}"
    return subproc.run_for_output(
        cursorcli.readonly_argv(full_prompt),
        timeout_sec=timeout_sec(),
        cwd=cwd,
        max_output_chars=MAX_OUTPUT_BYTES,
    )
