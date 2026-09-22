"""Bash 1 回の窓の中で **この作業ツリーに作られた commit** を reflog から検出する (0.12.0)。

## なぜ Bash のコマンド文字列を見ないのか

`git commit` を検出する素朴な方法は `tool_input.command` の解析だが、
`sh -c '...'` / alias / スクリプト / `make release` の内側など、コマンド文字列に
`git commit` の 3 文字が現れない commit がいくらでもある。逆に
`echo "git commit"` のような偽陽性も作れる。**実際に ref が動いたか**を見るほうが
素直で、解析の当たり外れに依存しない。

## 窓の作り方

`PreToolUse(Bash)` で「その worktree の HEAD reflog ファイル」の

- パス (`git rev-parse --git-path logs/HEAD`)
- **バイト長**
- そのときの HEAD の SHA

を記録し (`gitscan.head_log_snapshot`)、`PostToolUse(Bash)` で**同じファイルの
「pre 時点のバイト長より後ろ」だけ**を読む。reflog は追記専用なので、これで
「この Bash の中で起きた ref の移動」だけが取れる。git の追加呼び出しは
pre の rev-parse 1 回だけで、post 側は stat と部分読み込みしかしない。

`--git-path` は **linked worktree では worktree ごとの絶対パス**
(`<common>/.git/worktrees/<name>/logs/HEAD`) を返し、main worktree では cwd 相対の
`.git/logs/HEAD` を返す (git 2.50.1 実測)。どちらも `os.path.join(root, ...)` で
解決できる (絶対パスなら join はそのまま返す)。linked worktree の commit が
main worktree の `logs/HEAD` に現れないことも実測済み — つまり「別の worktree で
起きた commit」はそもそも窓に入らない。

## 窓の条件 (W2 / W3): 「commit だけで構成され、連鎖している」窓しか使わない

行の形式は `<old> <new> <ident> <ts> <tz>\\t<message>`。窓が使えるのは次の **両方**を
満たすときだけで、1 行でも外れたら **窓ごと何も送らない**:

- **W2**: 追記された行が `COMMIT_PREFIXES` の行だけで構成される。それ以外の行で
  `old != new` のもの (reset / merge / rebase / pull / cherry-pick / revert /
  switch 等) が 1 行でもあれば窓を捨てる。`old == new` の行 (`checkout -b` 等) は
  ref を動かさないので無視してよい
- **W3**: 行が連鎖している。先頭行の `<old>` が pre 時点の HEAD と一致し、以降は
  `old_i == new_{i-1}`

git 2.50.1 で実測した `commit` 系の表記 (`builtin/commit.c` が組み立てる 5 形):

| 操作 | message |
|---|---|
| 通常の commit | `commit: <subject>` |
| 最初の commit | `commit (initial): <subject>` |
| `--amend` | `commit (amend): <subject>` |
| **conflict 解決後の merge commit** | `commit (merge): <subject>` |
| **conflict 解決後の cherry-pick** | `commit (cherry-pick): <subject>` |

後ろの 2 つは `COMMIT_PREFIXES` に入れていないので、W2 により**窓ごと落ちる**。

対象外の表記も実測済み: `pull -q --no-rebase: Fast-forward` /
`pull -q --no-rebase: Merge made by the 'ort' strategy.` / `merge <ref>: ...` /
`rebase (start|pick|continue|finish): ...` / `cherry-pick: <subject>` /
`revert: Revert "<subject>"` / `reset: moving to ...` / `checkout: moving from A to B` /
`clone: from <url>`。いずれも `commit` で始まらず `old != new` なので W2 で窓が落ちる。

### 行の形だけでは足りない (マージ前レビューで 3 経路が実演された)

- conflict した `git revert` を `--continue` で確定すると message は
  **`commit: Revert "<subject>"`** になる (実測)
- `git merge --squash` / `git cherry-pick -n` / `git checkout <ref> -- <path>` /
  `git restore --source=<ref>` / `git stash pop` / `git apply` は
  **`logs/HEAD` に 1 行も書かない** (2026-09-20 実測: `merge --squash` / `cherry-pick -n`
  ともに追記バイト数 0)。続く `git commit` は素の `commit:` 行にしか見えない
- `git reset --soft <過去>` → `git commit` は W2 で落ちるが、落ちなければ `old..new` が
  squash 範囲全体に広がる

つまり **message の allow-list だけでは「その commit が作った差分 = このセッションの
成果」を保証できない**。保証は `__main__.py` 側のパス条件 (I1': 窓を開いた時点の
`git diff HEAD -- <path>` と一致することを `pre` の status スナップショットと
`git diff HEAD` の再確認で示す) が担う。ここでの W2 / W3 は、その等価性の前提
(「窓の終わりの HEAD = 最後の `<new>`」「基点 = 窓の始まりの HEAD」) を成り立たせる
ための条件であって、それ自体が十分条件ではない。

## fail-closed

次のどれかに当たったら **commit レビューを一切行わない** (何も送らない)。
「窓が信用できないなら送らない」= 送信範囲が広がる側に倒さない、という
この plugin の一貫した失敗方向:

1. pre のスナップショットが無い / 壊れている
2. reflog ファイルが読めない (pre 時点で存在していたのに消えた・権限が変わった)
3. 現在のバイト長が pre より**短い** (`git reflog expire` / 書き換え)
4. 行が連鎖していない (W3) — 先頭行の `<old>` が pre 時点の HEAD と違う、または
   途中で `old_i != new_{i-1}` になる (窓の外で ref が動いた / 追記が偽装された)
5. 行が parse できない / 追記が大きすぎる / 窓の中の commit が多すぎる
6. commit 以外の ref 移動が混ざっている (W2)

3 と 4 が無いと「reflog を書き換えて任意の `old..new` を差し込む」経路が残る。
`git config core.logAllRefUpdates false` のように **reflog をそもそも持たない** repo は
「pre のバイト長 0 かつ今もファイルが無い」= 追記ゼロとして扱い、エラーにしない
(この plugin の機能が 1 つ効かないだけで、異常ではないため)。

## 利用者への通知

fail-closed の理由のうち、**利用者の操作で起きて次に直せるもの**だけ 1 行通知する
(`appended` の 3 つ目の返り値)。「窓に merge が混ざった」「commit が多すぎる」は
その場の操作で説明が付くが、`pre` スナップショット欠落のような内部事情は毎回の
Bash で出ると雑音になるため stderr の debug log に留める。
"""
from __future__ import annotations

import os
import re

#: 対象にする reflog message の接頭辞 (モジュール docstring の表を参照)。
#: **`commit (merge): ` / `commit (cherry-pick): ` を足してはいけない。**
COMMIT_PREFIXES = ("commit: ", "commit (amend): ", "commit (initial): ")

#: 窓の追記としてこれ以上のバイト数は読まない (壊れた reflog / 機械生成の暴走対策)。
MAX_APPEND_BYTES = 262144

#: 1 回の Bash の窓で扱う commit 数の上限。超えたら fail-closed にする。
#: diff の取得が commit 数 × パス数に比例するため、PostToolUse(Bash) の
#: hook timeout 予算 (`tests/test_review_set.py::TestTimeoutBudgets`) から決めている。
MAX_COMMITS = 5

#: `<old>` / `<new>` として受理する形 (SHA-1 の 40 桁 / SHA-256 の 64 桁、短縮形も一応)。
_SHA_RE = re.compile(r"^[0-9a-f]{7,64}$")


class Commit:
    """窓の中で作られた commit 1 件。

    diff の範囲は commit ごとではなく **窓全体** (`pre の HEAD`..`最後の new`) を使う
    (理由は `__main__.py` の「commit 単位レビュー」節の I1')。ここが持つのは、その
    範囲が成り立つことを確かめるための連鎖情報 (`old` / `new`) と、W2 の判定に使う
    `message`。

    dataclass にしていないのは、テストが hook のモジュール群を `sys.modules` 未登録の
    まま読む経路があるため (`__main__.ReviewBatch` / `selection.Outcome` と同じ理由)。
    """

    def __init__(self, old: str, new: str, message: str) -> None:
        self.old = old
        self.new = new
        self.message = message


def is_null(sha: str) -> bool:
    """全ゼロの SHA (「まだ何も無い」を表す git の表記)。桁数は hash 方式で変わる。"""
    return bool(sha) and set(sha) == {"0"}


def _parse_line(line: str) -> tuple[str, str, str] | None:
    """`<old> <new> <ident> <ts> <tz>\\t<message>` を (old, new, message) に分解する。

    tab が無い行 (linked worktree を作った直後の 1 行目に実在する) や、SHA に
    見えないトークンで始まる行は None = parse 不能として扱う。
    """
    left, tab, message = line.partition("\t")
    if not tab:
        return None
    parts = left.split(" ")
    if len(parts) < 3:
        return None
    old, new = parts[0], parts[1]
    if not _SHA_RE.match(old) or not _SHA_RE.match(new):
        return None
    return old, new, message


def _continues(head: str, first_old: str) -> bool:
    """追記の最初の行が、pre 時点の HEAD から続いているか。

    pre 時点で HEAD が無かった repo (初回 commit 前) は `head` が空文字列で、その
    ときだけ全ゼロの `old` を受け入れる。`head` の取得に失敗した repo も空文字列に
    なるが、その場合は実在する SHA と一致しないので自然に fail-closed に落ちる。
    """
    if head:
        return first_old == head
    return is_null(first_old)


#: 利用者に 1 行通知する fail-closed の文面 (`appended` の 3 つ目の返り値)。
#: 固定文言のみで、パス名も reflog の中身も混ぜない。
NOTICE_FOREIGN_OP = (
    "この Bash の中で commit 以外の ref 操作 (merge / rebase / reset / pull 等) が"
    "起きたため、commit レビューを行いませんでした"
)
NOTICE_TOO_MANY = (
    f"この Bash の中の commit が上限 ({MAX_COMMITS} 件) を超えたため、"
    "commit レビューを行いませんでした"
)


def appended(pre) -> tuple[list[Commit] | None, str, str | None]:
    """pre 以降に追記された行から対象 commit を取り出す。

    返り値は `(commits, reason, notice)`。**`commits` が None なら fail-closed**
    (`reason` はその理由。debug log 用で、利用者向けの文面ではない)。
    空リストは「窓の中に対象 commit が無かった」= 正常。
    `notice` は利用者に 1 行出すべき fail-closed のときだけ非 None
    (モジュール docstring「利用者への通知」節)。
    """
    if not isinstance(pre, dict):
        return None, "pre スナップショットが無い", None
    path = pre.get("path")
    size = pre.get("size")
    head = pre.get("head")
    if (
        not isinstance(path, str)
        or not path
        or not isinstance(size, int)
        or isinstance(size, bool)
        or size < 0
        or not isinstance(head, str)
    ):
        return None, "pre スナップショットが壊れている", None

    try:
        current = os.path.getsize(path)
    except OSError:
        if size == 0:
            # reflog を持たない repo (core.logAllRefUpdates=false 等)。異常ではない
            return [], "", None
        return None, "reflog ファイルを読めない", None

    if current < size:
        return None, "reflog が pre より短い (expire / 書き換え)", None
    if current == size:
        return [], "", None
    if current - size > MAX_APPEND_BYTES:
        return None, "reflog の追記が大きすぎる", None

    try:
        with open(path, "rb") as f:
            f.seek(size)
            chunk = f.read(MAX_APPEND_BYTES)
    except OSError:
        return None, "reflog ファイルを読めない", None

    lines = [ln for ln in chunk.decode("utf-8", errors="replace").split("\n") if ln]
    if not lines:
        return None, "reflog の追記を parse できない", None

    commits: list[Commit] = []
    # W3: 窓の中で HEAD が連鎖していること。`expected` は「次の行の `<old>` は
    # こうでなければならない」値で、先頭だけ pre の HEAD (HEAD が無い repo では
    # 全ゼロ) を受け入れる `_continues` を通す。
    expected = ""
    for index, line in enumerate(lines):
        parsed = _parse_line(line)
        if parsed is None:
            return None, "reflog の行を parse できない", None
        old, new, message = parsed
        if index == 0:
            if not _continues(head, old):
                return None, "reflog の追記が pre の HEAD から続いていない", None
        elif old != expected:
            return None, "reflog の行が連鎖していない (窓の外で ref が動いた)", None
        expected = new
        if message.startswith(COMMIT_PREFIXES):
            commits.append(Commit(old, new, message))
        elif old != new:
            # W2: ref を動かす commit 以外の操作 (reset / merge / rebase / pull /
            # cherry-pick / revert / switch)。他人の内容を運びうるので窓ごと捨てる
            return None, "窓の中で commit 以外の ref 移動が起きた", NOTICE_FOREIGN_OP
    if len(commits) > MAX_COMMITS:
        return (
            None,
            f"1 回の Bash に commit が {len(commits)} 件 (上限 {MAX_COMMITS})",
            NOTICE_TOO_MANY,
        )
    return commits, "", None
