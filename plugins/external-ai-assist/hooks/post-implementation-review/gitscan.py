"""git 作業ツリーの走査ヘルパー (パス正規化 / status スナップショット / パス単位 diff)。

## レビュー粒度は「HEAD 基準」を選択している (パス単位 hash による重複抑止つき)

`git diff HEAD -- <path>` は「そのファイルの HEAD 以降の全変更」を返す。turn 1 と
turn 7 で同じファイルを編集すると turn 1 の hunk が turn 7 でも再掲される。
選択肢は 2 つあった:

- (a) レビュー済み時点の内容をキャッシュし `diff --no-index` で前回比だけ渡す
- (b) HEAD 基準を維持し、レビュアーにファイル全体の変更文脈を与える

**(b) を選択した。** 理由:

1. レビュー観点が「この差分が既存コードベース上で何を壊しうるか」(prompts/ 参照)
   であり、直前 1 ターンの hunk だけを切り出すと文脈が落ちて指摘精度が下がる。
   turn 1 で足した関数を turn 7 で 1 行いじった場合、(a) はその 1 行しか見せない
2. (a) はレビュー済み時点の全ファイル内容を $TMPDIR に複製し続ける必要があり、
   状態量と GC 対象が跳ね上がる。機密ファイルの内容が $TMPDIR に残る面でも不利
3. 「commit まで同じ変更が再レビューされる」という元の不満は、(b) 単独ではなく
   **パス単位の diff hash** で潰せる: そのパスの HEAD 基準 diff が前回レビュー時と
   1 バイトも変わっていなければレビューに載せない (state.reviewed)。差分が本当に
   変わった時だけ、変わった文脈ごとレビューに出す

結果として「同じ変更の再レビュー」は起きず、レビュアーは常にファイル全体の
変更文脈を受け取る。(a) が優位になるのは巨大ファイルを何十ターンも編集し続ける
ケースだが、そこは MAX_DIFF_BYTES の truncate で頭打ちになる。
"""
from __future__ import annotations

import os
import subprocess

# 内部 timeout は hooks.json の hook timeout に**収まる**ように決める。超えると
# ハーネスの kill が先に来て、自前の fail-open 経路 (None を返して skip) に到達しない。
#
#   pre-tool / post-tool (hook 10s): rev-parse 2 + status 5 = 最悪 7s
#   stop (hook 660s, うち cursor 600s → git に使えるのは約 60s):
#     rev-parse 2 + ls-files 10 + rev-parse 2 + パス単位 diff (COLLECT_BUDGET_SEC 30) = 44s
REV_PARSE_TIMEOUT_SEC = 2
STATUS_TIMEOUT_SEC = 5
LS_FILES_TIMEOUT_SEC = 10
PATH_DIFF_TIMEOUT_SEC = 5

MAX_SNAPSHOT_ENTRIES = 5000


def _git(root: str, args: list[str], timeout: int = PATH_DIFF_TIMEOUT_SEC):
    try:
        return subprocess.run(
            ["git", *args],
            cwd=root,
            capture_output=True,
            timeout=timeout,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None


def _decode(raw: bytes | None) -> str:
    """git の出力は UTF-8 とは限らない (repo 内のファイル名・内容依存)。

    text=True だと locale の strict デコードで例外になりうるので、常に
    errors="replace" で読む。
    """
    return (raw or b"").decode("utf-8", errors="replace")


def worktree_root(cwd: str) -> str | None:
    """cwd を含む git 作業ツリーの root を realpath で返す。git 外なら None。

    realpath を通すのは必須。macOS では `/tmp` が `/private/tmp` の symlink で、
    hook payload の cwd と `git rev-parse --show-toplevel` の出力で表記が割れる。
    素の startswith 比較だと全パスが「作業ツリー外」に落ちる。
    """
    if not cwd:
        return None
    res = _git(cwd, ["rev-parse", "--show-toplevel"], timeout=REV_PARSE_TIMEOUT_SEC)
    if res is None or res.returncode != 0:
        return None
    top = _decode(res.stdout).strip()
    return os.path.realpath(top) if top else None


def head_exists(root: str) -> bool:
    res = _git(root, ["rev-parse", "--verify", "HEAD"], timeout=REV_PARSE_TIMEOUT_SEC)
    return res is not None and res.returncode == 0


def to_relative(root: str, path: str) -> str | None:
    """絶対パスを作業ツリー相対に変換する。ツリー外なら None。

    git のパススペックは root 相対でなければ黙って何にもマッチしないため、
    ここで確実に相対化しておく。
    """
    if not path:
        return None
    real = os.path.realpath(path)
    root_real = os.path.realpath(root)
    if real == root_real:
        return None
    prefix = root_real.rstrip(os.sep) + os.sep
    if not real.startswith(prefix):
        return None
    return real[len(prefix) :]


# --------------------------------------------------------------------------
# status スナップショット (Bash 経由の変更検出)
# --------------------------------------------------------------------------


def status_snapshot(root: str) -> dict[str, list]:
    """dirty / untracked なパス -> [status_code, size, mtime_ns] を返す。

    **行の集合ではなく (code, size, mtime_ns) のタプルを記録するのが要点**。
    すでに HEAD から変更済みのファイルを `sed -i` で書き換えると porcelain の行は
    ` M seed.txt` のまま変わらないため、行集合の差分では検出できない。
    size / mtime_ns まで見て初めて Bash 経由の書き換えが拾える。

    `--untracked-files=all` を使うのは、新規ディレクトリが `dir/` に畳まれると
    個別ファイルを pending に積めないため。

    ただし `-uall` でも**入れ子の git リポジトリは展開されず `dir/` のまま**返る
    (別の worktree を `.claude/worktrees/` 配下に作った場合など)。中身は別リポジトリの
    変更なのでレビュー対象にしてはならず、末尾 `/` のエントリは捨てる。
    """
    res = _git(
        root,
        ["status", "--porcelain", "-z", "--untracked-files=all"],
        timeout=STATUS_TIMEOUT_SEC,
    )
    if res is None or res.returncode != 0:
        return {}

    snapshot: dict[str, list] = {}
    for code, rel in _parse_porcelain_z(_decode(res.stdout)):
        if rel.endswith("/"):
            continue
        try:
            st = os.stat(os.path.join(root, rel))
            snapshot[rel] = [code, st.st_size, st.st_mtime_ns]
        except OSError:
            snapshot[rel] = [code, -1, -1]
        if len(snapshot) >= MAX_SNAPSHOT_ENTRIES:
            break
    return snapshot


def _parse_porcelain_z(payload: str):
    """`status --porcelain -z` を (code, path) に分解する。

    エントリは `XY<space>PATH\\0`。rename/copy のときだけ元パスが次のトークンとして
    続くので読み飛ばす。
    """
    tokens = [t for t in payload.split("\0")]
    i = 0
    while i < len(tokens):
        token = tokens[i]
        i += 1
        if len(token) < 4:
            continue
        code = token[:2]
        path = token[3:]
        if "R" in code or "C" in code:
            i += 1  # 元パスのトークンを読み飛ばす
        if path:
            yield code, path


def changed_between(pre: dict[str, list], post: dict[str, list]) -> list[str]:
    """2 つのスナップショットを比較し、変化した相対パスを返す。

    status から消えたパス (commit / checkout で HEAD と一致した等) も「変化した」
    扱いで返す。diff が空になるので Stop 側でそのまま落ちる。
    """
    changed = []
    for rel, meta in post.items():
        if pre.get(rel) != meta:
            changed.append(rel)
    for rel in pre:
        if rel not in post:
            changed.append(rel)
    return sorted(set(changed))


# --------------------------------------------------------------------------
# パス単位 diff
# --------------------------------------------------------------------------


def untracked_among(root: str, rels: list[str]) -> set[str]:
    """与えたパスのうち untracked (かつ ignore されていない) ものを返す。"""
    if not rels:
        return set()
    res = _git(
        root,
        ["ls-files", "--others", "--exclude-standard", "-z", "--", *rels],
        timeout=LS_FILES_TIMEOUT_SEC,
    )
    if res is None or res.returncode != 0:
        return set()
    return {p for p in _decode(res.stdout).split("\0") if p}


def path_diff(root: str, rel: str, untracked: bool, has_head: bool) -> str:
    """1 パス分の diff テキストを返す。差分なし / 取得失敗なら空文字。"""
    if untracked:
        # untracked は HEAD 側に対応物が無いので /dev/null と比較する。
        # --no-index は差分ありで exit 1 を返すため returncode は見ない。
        res = _git(root, ["diff", "--no-index", "--", os.devnull, rel])
        return _decode(res.stdout) if res is not None else ""

    # 初回コミット前の repo には HEAD が無いので staged 差分で代替する
    base = "HEAD" if has_head else "--cached"
    res = _git(root, ["diff", base, "--", rel])
    if res is None or res.returncode != 0:
        return ""
    return _decode(res.stdout)
