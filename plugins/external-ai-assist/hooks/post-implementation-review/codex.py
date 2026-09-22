"""Codex による実装直後の差分レビュー (0.12.0 新設)。

**cursor 側と同じ観点・同じ出力形式**を使う (`prompts/post-implementation-codex.md` は
`post-implementation-cursor.md` と同じ 5 項目立て + `REVIEW_CLEAN` sentinel)。どちらの
backend が返した結果でも `_common/sentinel.py` の判定と `build_reason()` の組み立てが
そのまま効くようにするため、観点や書式を backend ごとに変えない。

起動は `_common.backends.codex` 経由 (`codex exec -s read-only --ephemeral -`)。
exitplan-review の codex 起動形をそのまま踏襲する — read-only で起動し、プロンプトと
差分は stdin 一本で渡す (長い diff を argv に載せない)。

**timeout は cursor と同じつまみ (`EXTERNAL_AI_POST_REVIEW_TIMEOUT`) を共有し、上限も
同じ 600 秒**にしてある。これは運用上の好みではなく制約で、backend ごとに上限が違うと

- `state.IN_FLIGHT_TTL_SEC` (= 全 backend の上限の最大 + 300) が backend の増減で動き、
  異なる backend を選んだセッション同士が互いの in-flight を奪い合う
- Stop の hook timeout (690 秒) の予算計算が backend の選択に依存する

ことになる (`selection.py` の `MAX_TOTAL_TIMEOUT_SEC` と
`tests/test_review_set.py::TestTimeoutBudgets`)。

**この backend は既定では使われない。** 差分レビューの既定の送信先は 0.11.0 と同じ
cursor のみで、`EXTERNAL_AI_POST_REVIEW_BACKENDS` に明示的に `codex` を書いたときだけ
送信先になる (`selection.py` の `DEFAULT_BACKENDS`)。
"""
from __future__ import annotations

from pathlib import Path

from _common import backends, settings

NAME = backends.codex.NAME

#: 既定の timeout。cursor 側 (`cursor.TIMEOUT_SEC`) と揃える。
TIMEOUT_SEC = 300

#: env で伸ばせる上限。**cursor と同じ値でなければならない** (module docstring)。
MAX_TIMEOUT_SEC = 600

MAX_OUTPUT_BYTES = 16000

ENV_TIMEOUT = "EXTERNAL_AI_POST_REVIEW_TIMEOUT"

_PROMPT_FILE = Path(__file__).parent / "prompts" / "post-implementation-codex.md"


def is_available(deadline: float | None = None) -> bool:
    """codex CLI を起動できるか。`deadline` は interface を揃えるためだけに受ける。"""
    return backends.codex.is_available(deadline)


def timeout_sec() -> float:
    """実効 timeout。未設定なら `TIMEOUT_SEC`、`MAX_TIMEOUT_SEC` で clamp。

    モジュール変数を呼び出しのたびに読む (テストが `TIMEOUT_SEC` を差し替えられるように
    束縛しない)。
    """
    return settings.duration(ENV_TIMEOUT, TIMEOUT_SEC, MAX_TIMEOUT_SEC)


def review(diff_text: str, *, cwd: str | None = None) -> str | None:
    """Codex で差分をレビューし、整形済み結果を返す。失敗時は None。

    `cwd` は git 作業ツリーの root を渡すこと (`__main__._run_review` が渡す)。
    未指定 (None) だと codex は hook プロセス自身の cwd で起動され、diff のパス
    (worktree root 相対) と codex のワークスペースが食い違う。
    """
    try:
        template = _PROMPT_FILE.read_text(encoding="utf-8")
    except OSError:
        return None

    full_prompt = (
        f"{template}\n\n---\n\n## レビュー対象 git diff\n\n```diff\n{diff_text}\n```"
    )
    result = backends.codex.run(full_prompt, cwd=cwd, timeout=timeout_sec())
    result = result.truncated(MAX_OUTPUT_BYTES)
    return result.text if result.is_ok else None
