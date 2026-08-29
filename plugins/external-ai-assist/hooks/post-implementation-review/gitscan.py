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
ケースだが、そこは 1 ファイルあたりの上限 (`__main__.MAX_FILE_DIFF_BYTES`) の切り詰めで
頭打ちになる (切り詰めた場合も hash は全文で記録するので、変わらない限り再掲しない)。
"""
from __future__ import annotations

import os
import subprocess

# 内部 timeout は hooks.json の hook timeout に**収まる**ように決める。超えると
# ハーネスの kill が先に来て、自前の fail-open 経路 (None を返して skip) に到達しない。
#
#   pre-tool (hook 10s): rev-parse 1 + status 1 = 最悪 7s
#   post-tool / Bash (hook 10s, bd_092a232e-zh5.16 で rev-parse が 1 → 2 に増加):
#     rev-parse (worktree_root) 1 + status 1 + rev-parse (head_sha) 1 = 最悪 9s
#   post-tool / Edit,Write,NotebookEdit (hook 10s, 0.6.0 まで git 呼び出し無しだったが
#     bd_092a232e-zh5.16 で追加): rev-parse (worktree_root) 1 + rev-parse (head_sha) 1
#     = 最悪 4s
#   stop (hook 690s, うち cursor 600s + kill 猶予 15s → git に使えるのは約 75s):
#     rev-parse 2 + ls-files (symlink 一覧) 10 + ls-files (untracked) 10
#     + パス単位 diff (COLLECT_BUDGET_SEC 30 + 予算判定後に走る最後の 1 パス。
#       bd_092a232e-zh5.16 の base_sha フォールバックで最悪 2 回 git diff を呼ぶため
#       PATH_DIFF_TIMEOUT_SEC の 2 倍で見る = 10) = 64s
#   実際の予算計算とテストは tests/test_review_set.py::TestTimeoutBudgets を参照。
#   ここでの数値は目安のコメントに過ぎず、乖離したらテストの方を正とする。
REV_PARSE_TIMEOUT_SEC = 2
STATUS_TIMEOUT_SEC = 5
LS_FILES_TIMEOUT_SEC = 10
PATH_DIFF_TIMEOUT_SEC = 5

MAX_SNAPSHOT_ENTRIES = 5000

# repo 内 symlink の収集 (symlink_map)。untracked の symlink は root から scandir で探すが、
# node_modules のような巨木で止まらないよう BFS のエントリ数と深さに上限を置く
# (浅い階層から見るので、root 直下の `credentials/` のような別名は必ず拾う)。
SYMLINK_SCAN_DEPTH = 3
SYMLINK_SCAN_BUDGET = 5000
MAX_SYMLINKS = 500


def _git(root: str, args: list[str], timeout: int = PATH_DIFF_TIMEOUT_SEC):
    """git を起動する。パスは常に **literal pathspec** として渡す。

    既定の pathspec は `*` `?` `[...]` を glob として解釈する。`app/[id]/page.tsx` のような
    名前 (Next.js の動的ルート) を pathspec に渡すと `app/i/page.tsx` にもマッチし、
    claim していない (除外判定も通っていない) 別セッションのファイルの diff が混入する。
    旧 state に残った `[.]env` のようなエントリが tracked の `.env` を拾う経路も同じ。
    """
    try:
        return subprocess.run(
            ["git", "--literal-pathspecs", *args],
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


def head_sha(root: str) -> str | None:
    """現在の HEAD の commit SHA (コミットが無ければ None)。

    bd_092a232e-zh5.16: pending 記録時点の HEAD を残しておくために使う
    (`__main__.record_pending` 呼び出し元)。
    """
    res = _git(root, ["rev-parse", "HEAD"], timeout=REV_PARSE_TIMEOUT_SEC)
    if res is None or res.returncode != 0:
        return None
    sha = _decode(res.stdout).strip()
    return sha or None


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
# repo 内 symlink の一覧 (除外判定の別名生成用)
# --------------------------------------------------------------------------


def symlink_map(root: str) -> dict[str, str]:
    """作業ツリー内の symlink を {link_rel: target_rel} で返す (target は realpath の root 相対)。

    Bash 経由の変更は pre/post の `git status` 比較で拾うが、status は実体パス
    (`ordinary/data.json`) しか返さない。`credentials/` → `ordinary/` の symlink 経由で
    `sed -i credentials/data.json` しても別名は claim に現れないため、ここで symlink を列挙し
    `exclusion.expand_aliases` で別名を作って除外判定に当てる (Codex PR レビュー R2 P1)。

    - tracked: index の mode 120000 (`git ls-files -s`)。深さの制限なし
    - untracked (+ tracked の取りこぼし): root から `SYMLINK_SCAN_DEPTH` 階層までを BFS で
      scandir (`.git` は見ない)。エントリ数 `SYMLINK_SCAN_BUDGET`、件数 `MAX_SYMLINKS` で打ち切る

    ツリー外を指す symlink は含めない (その先のファイルは作業ツリー外として落ちる)。
    git が失敗 / timeout しても scandir 側の結果は返す (fail-open。取りこぼしは除外の見逃しに
    なるが、Stop を止めるより優先)。
    """
    links: dict[str, str] = {}
    res = _git(root, ["ls-files", "-s", "-z"], timeout=LS_FILES_TIMEOUT_SEC)
    if res is not None and res.returncode == 0:
        for entry in _decode(res.stdout).split("\0"):
            if not entry.startswith("120000 "):
                continue
            _meta, _tab, rel = entry.partition("\t")  # "<mode> <sha> <stage>\t<path>"
            if rel:
                _add_link(root, links, rel)
            if len(links) >= MAX_SYMLINKS:
                return links

    queue: list[tuple[str, int]] = [("", 0)]
    scanned = 0
    while queue and scanned < SYMLINK_SCAN_BUDGET and len(links) < MAX_SYMLINKS:
        rel_dir, depth = queue.pop(0)
        try:
            with os.scandir(os.path.join(root, rel_dir) if rel_dir else root) as it:
                for entry in it:
                    scanned += 1
                    if scanned > SYMLINK_SCAN_BUDGET or len(links) >= MAX_SYMLINKS:
                        break
                    rel = f"{rel_dir}/{entry.name}" if rel_dir else entry.name
                    try:
                        if entry.is_symlink():
                            _add_link(root, links, rel)
                        elif (
                            entry.name != ".git"
                            and depth + 1 < SYMLINK_SCAN_DEPTH
                            and entry.is_dir(follow_symlinks=False)
                        ):
                            queue.append((rel, depth + 1))
                    except OSError:
                        continue
        except OSError:
            continue
    return links


def _add_link(root: str, links: dict[str, str], rel: str) -> None:
    target = to_relative(root, os.path.join(root, rel))
    if target and target != rel:
        links[rel] = target


# --------------------------------------------------------------------------
# status スナップショット (Bash 経由の変更検出)
# --------------------------------------------------------------------------


def status_snapshot(root: str) -> dict[str, list] | None:
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

    **失敗 (timeout / 非 0 終了) と、収まりきらない (`MAX_SNAPSHOT_ENTRIES` 超過で
    不完全) 場合は `None` を返す** (bd_092a232e-zh5.7)。以前はどちらも `{}` を
    返しており、呼び出し側 (`__main__._record_bash_changes`) の
    `changed_between(pre, post)` が「空」と「取得できた全件消えた」を区別できず、
    片方が失敗したもう片方の全エントリ (他セッション・他人の変更を含む) を
    "変化あり" として pending に積んでいた。不完全な部分集合も同じ理由で危険:
    pre/post で切り詰めの境界 (どのエントリまで拾えたか) がずれると、実際は
    変化していないパスが「片方にしか無い」ことになり誤検出する。
    """
    res = _git(
        root,
        ["status", "--porcelain", "-z", "--untracked-files=all"],
        timeout=STATUS_TIMEOUT_SEC,
    )
    if res is None or res.returncode != 0:
        return None

    snapshot: dict[str, list] = {}
    for code, rel in _parse_porcelain_z(_decode(res.stdout)):
        if rel.endswith("/"):
            continue
        if len(snapshot) >= MAX_SNAPSHOT_ENTRIES:
            return None  # 収まりきらない (不完全) → 比較不能として諦める
        try:
            st = os.stat(os.path.join(root, rel))
            snapshot[rel] = [code, st.st_size, st.st_mtime_ns]
        except OSError:
            snapshot[rel] = [code, -1, -1]
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


def path_diff(
    root: str, rel: str, untracked: bool, has_head: bool, base_sha: str | None = None
) -> str:
    """1 パス分の diff テキストを返す。差分なし / 取得失敗なら空文字。

    `--no-color` は必須: `color.ui=always` / `color.diff=always` の環境では ANSI が混ざり、
    レビュアーに渡す本文が汚れるうえ hash もバイト予算も狂う。

    `base_sha` は bd_092a232e-zh5.16 向け: pending にこのパスを記録した時点の HEAD。
    素の `"HEAD"` (現行 HEAD) だけを基点にすると、Claude が同一ターン内で編集後に
    `git commit` した場合、Stop 時点では HEAD がその commit を含んでしまい
    `git diff HEAD -- path` が空になって一度もレビューされずに消える。`base_sha` が
    与えられればまずそれで diff を試み、成功すればそれを返す (追加の git 呼び出しは
    無い)。`base_sha` が無効/到達不能 (rebase・GC・repo 再作成等) で diff 自体が
    失敗した場合だけ、従来どおり現行 `"HEAD"` (無ければ `--cached`) にフォールバック
    する。
    """
    if untracked:
        # untracked は HEAD 側に対応物が無いので /dev/null と比較する。
        # --no-index は差分ありで exit 1 を返すため returncode は見ない。
        res = _git(root, ["diff", "--no-color", "--no-index", "--", os.devnull, rel])
        return _decode(res.stdout) if res is not None else ""

    if base_sha:
        res = _git(root, ["diff", "--no-color", base_sha, "--", rel])
        if res is not None and res.returncode == 0:
            return _decode(res.stdout)
        # base_sha が無効/到達不能 → 現行 HEAD にフォールバック (下へ続く)

    # 初回コミット前の repo には HEAD が無いので staged 差分で代替する
    base = "HEAD" if has_head else "--cached"
    res = _git(root, ["diff", "--no-color", base, "--", rel])
    if res is None or res.returncode != 0:
        return ""
    return _decode(res.stdout)
