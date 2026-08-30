"""Cursor による実装直後の差分レビュー。

git diff 本文をプロンプト末尾に埋め込んで cursor agent の --print (読み取り専用 `--mode plan`) で渡す。
Cursor がコードベース全体を参照しながら影響範囲・リグレッションリスクを評価する。

起動は `_common.subproc` 経由 (独自 process group + timeout + 残出力の読み捨て)。
`_common` は `__main__.py` (テストでは `tests/_testutil.py`) が sys.path に載せる。
"""
from __future__ import annotations

from pathlib import Path

from _common import cursorcli, settings, subproc

NAME = cursorcli.NAME

#: 既定の timeout。**0.6.0 で 600 → 300 に短縮** (挙動変更)。Stop は編集のあった全ターンで
#: 発火するので、待ち時間の期待値が体感を決める。長考させたい場合は
#: `EXTERNAL_AI_POST_REVIEW_TIMEOUT=600` で従来値に戻せる。
TIMEOUT_SEC = 300

#: env で伸ばせる上限 (従来の既定値)。hooks.json の Stop timeout 690s は
#: 「git 予算 59s + cursor 600s + kill 猶予 15s」で組んであるので、ここを上げるなら
#: hooks.json 側も上げる必要がある (`tests/test_review_set.py::TestTimeoutBudgets`)。
MAX_TIMEOUT_SEC = 600

MAX_OUTPUT_BYTES = 16000

ENV_TIMEOUT = "EXTERNAL_AI_POST_REVIEW_TIMEOUT"

_PROMPT_FILE = Path(__file__).parent / "prompts" / "post-implementation-cursor.md"


def is_available() -> bool:
    return cursorcli.is_available()


def timeout_sec() -> float:
    """実効 timeout。未設定なら `TIMEOUT_SEC`、`MAX_TIMEOUT_SEC` で clamp。

    **`state.IN_FLIGHT_TTL_SEC` はこの値ではなく `MAX_TIMEOUT_SEC` から導出する**。
    設定値から導けば、timeout を短くしたセッションが、長く設定した別セッションの
    in-flight を「TTL 超過」とみなして横取りしてしまう。
    """
    return settings.duration(ENV_TIMEOUT, TIMEOUT_SEC, MAX_TIMEOUT_SEC)


def review(diff_text: str, *, cwd: str | None = None) -> str | None:
    """Cursor で差分をレビューし、整形済み結果を返す。失敗時は None。

    `cwd` は git 作業ツリーの root を渡すこと (`__main__._run_review` が渡す)。
    未指定 (None) だと cursor は hook プロセス自身の cwd で起動され、Claude Code を
    サブディレクトリで起動したセッションでは diff のパス (worktree root 相対) と
    cursor のワークスペースが食い違い、cursor 側の参照・探索が外れる (内部バックログ)。
    """
    try:
        template = _PROMPT_FILE.read_text(encoding="utf-8")
    except OSError:
        return None

    full_prompt = (
        f"{template}\n\n---\n\n## レビュー対象 git diff\n\n```diff\n{diff_text}\n```"
    )
    return subproc.run_for_output(
        cursorcli.readonly_argv(full_prompt),
        timeout_sec=timeout_sec(),
        cwd=cwd,
        max_output_chars=MAX_OUTPUT_BYTES,
    )
