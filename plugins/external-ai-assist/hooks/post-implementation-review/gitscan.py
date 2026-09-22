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

import hashlib
import os
import re
import subprocess

from _common import gitroot

#: object id として受理する形 (SHA-1 40 桁 / SHA-256 64 桁。短縮形も一応許す)。
_SHA_RE = re.compile(r"^[0-9a-f]{7,64}$")

# 内部 timeout は hooks.json の hook timeout に**収まる**ように決める。超えると
# ハーネスの kill が先に来て、自前の fail-open 経路 (None を返して skip) に到達しない。
#
# **git 以外に cursor CLI の検出 (`__main__.PER_TOOL_PROBE_BUDGET_SEC` = 2s) も同じ枠で
# 走る** (0.11.0。キャッシュが使えない環境では hook 1 回ごとに probe する)。下の数値は
# git のぶんだけなので、pre-tool / post-tool ではそこに 2s を足して読む。
#
#   pre-tool (hook 15s): REV_PARSE_TIMEOUT_SEC × 2 (worktree_root + reflog の起点)
#     + STATUS_TIMEOUT_SEC × 1 = 最悪 9s (+ 検出 2s = 11s)
#   post-tool / Bash (hook 720s): commit の無い窓は worktree_root + status_snapshot
#     = 最悪 7s (+ 検出 2s)。commit を含む窓はそこに range_paths / symlink_map /
#     flagged_index_entries (P4') / changed_vs_head × 2 / CAT_FILE_TIMEOUT_SEC × 2
#     (P6) / COMMIT_COLLECT_BUDGET_SEC と外部 AI CLI の待ち時間が乗る
#   post-tool / Edit,Write,NotebookEdit (hook 10s): git 呼び出し無し (0s。+ 検出 2s)
#   stop (hook 690s, うち cursor 600s + kill 猶予 15s → git に使えるのは約 75s):
#     REV_PARSE_TIMEOUT_SEC × 2 (worktree_root + head_exists)
#     + LS_FILES_TIMEOUT_SEC × 2 (symlink_map + untracked_among)
#     + COLLECT_BUDGET_SEC 30 + PATH_DIFF_TIMEOUT_SEC × 1 (予算判定後に走る
#       最後の 1 パス) = 59s
#   実際の予算計算とテストは tests/test_review_set.py::TestTimeoutBudgets を参照。
#   ここでの数値は目安のコメントに過ぎず、乖離したらテストの方を正とする。
REV_PARSE_TIMEOUT_SEC = 2
STATUS_TIMEOUT_SEC = 5
LS_FILES_TIMEOUT_SEC = 10
PATH_DIFF_TIMEOUT_SEC = 5
#: `cat-file --batch-check` / `--batch` (commit レビューの P6)。object DB の読み出し
#: だけなので短くてよい。2 回走るので、長くすると PostToolUse(Bash) の予算を食う。
CAT_FILE_TIMEOUT_SEC = 5

MAX_SNAPSHOT_ENTRIES = 5000

# repo 内 symlink の収集 (symlink_map)。untracked の symlink は root から scandir で探すが、
# node_modules のような巨木で止まらないよう BFS のエントリ数と深さに上限を置く
# (浅い階層から見るので、root 直下の `credentials/` のような別名は必ず拾う)。
SYMLINK_SCAN_DEPTH = 3
SYMLINK_SCAN_BUDGET = 5000
MAX_SYMLINKS = 500


def _git(
    root: str,
    args: list[str],
    timeout: int = PATH_DIFF_TIMEOUT_SEC,
    stdin_data: bytes | None = None,
):
    """git を起動する。パスは常に **literal pathspec** として渡す。

    既定の pathspec は `*` `?` `[...]` を glob として解釈する。`app/[id]/page.tsx` のような
    名前 (Next.js の動的ルート) を pathspec に渡すと `app/i/page.tsx` にもマッチし、
    claim していない (除外判定も通っていない) 別セッションのファイルの diff が混入する。
    旧 state に残った `[.]env` のようなエントリが tracked の `.env` を拾う経路も同じ。

    `stdin_data` は `cat-file --batch` 系にリクエストを流し込むためだけに使う
    (渡さなければ従来どおり stdin を継承しない `capture_output` のみ)。
    """
    try:
        return subprocess.run(
            ["git", "--literal-pathspecs", *args],
            cwd=root,
            capture_output=True,
            timeout=timeout,
            input=stdin_data,
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

    実装は `_common.gitroot` に一本化している (exitplan-review も cursor/codex の
    起動 cwd を解決するのに同じロジックが要るため。macOS の `/tmp` ->
    `/private/tmp` symlink 対応などの詳細はそちら参照)。
    """
    return gitroot.worktree_root(cwd)


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
# repo 内 symlink の一覧 (除外判定の別名生成用)
# --------------------------------------------------------------------------


def symlink_map(root: str) -> dict[str, str]:
    """作業ツリー内の symlink を {link_rel: target_rel} で返す (target は realpath の root 相対)。

    Bash 経由の変更は pre/post の `git status` 比較で拾うが、status は実体パス
    (`ordinary/data.json`) しか返さない。`credentials/` → `ordinary/` の symlink 経由で
    `sed -i credentials/data.json` しても別名は claim に現れないため、ここで symlink を列挙し
    `exclusion.expand_aliases` で別名を作って除外判定に当てる (マージ前レビューの指摘)。

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
    """dirty / untracked なパス -> [status_code, size, mtime_ns, ctime_ns] を返す。

    **行の集合ではなく (code, size, mtime_ns, ctime_ns) のタプルを記録するのが要点**。
    すでに HEAD から変更済みのファイルを `sed -i` で書き換えると porcelain の行は
    ` M seed.txt` のまま変わらないため、行集合の差分では検出できない。
    size / mtime_ns まで見て初めて Bash 経由の書き換えが拾える。

    **`ctime_ns` は commit レビューの P3' 専用**で、`changed_between` は見ない
    (そちらは `[:3]` だけを比較する)。`os.utime` / `touch -r` / `cp -p` は mtime を
    復元できても ctime は戻せない (2026-09-22 実測) ので、「窓の間に書き換わって
    いない」の判定材料としては ctime が要る。一方で chmod / xattr の書き込みは
    ctime だけを動かすため、`changed_between` にまで入れると **他の書き手の
    ファイルが「このセッションが変更した」として pending に入る** (= Stop の
    送信範囲が広がる)。`ctime_ns` が要るのは P3' の突合だけなので、
    比較範囲はそこに閉じる。

    `--untracked-files=all` を使うのは、新規ディレクトリが `dir/` に畳まれると
    個別ファイルを pending に積めないため。

    ただし `-uall` でも**入れ子の git リポジトリは展開されず `dir/` のまま**返る
    (別の worktree を `.claude/worktrees/` 配下に作った場合など)。中身は別リポジトリの
    変更なのでレビュー対象にしてはならず、末尾 `/` のエントリは捨てる。

    **失敗 (timeout / 非 0 終了) と、収まりきらない (`MAX_SNAPSHOT_ENTRIES` 超過で
    不完全) 場合は `None` を返す**。以前はどちらも `{}` を
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
            snapshot[rel] = [code, st.st_size, st.st_mtime_ns, st.st_ctime_ns]
        except OSError:
            snapshot[rel] = [code, -1, -1, -1]
    return snapshot


def stat_entry(root: str, rel: str) -> list[int]:
    """`status_snapshot` が 1 エントリに記録するのと同じ `[size, mtime_ns, ctime_ns]`。

    commit レビューの P3' (「窓の間に作業ツリーのファイルが書き換わっていないか」) を
    pre スナップショットと突き合わせるために使う。**ファイルが無いときの表現を
    `status_snapshot` と揃える**のが要点 (`os.stat` 失敗は `[-1, -1, -1]`): 削除を
    commit した窓では pre / post ともファイルが無く、同じ値になって一致する。

    **`ctime_ns` を入れているのは `(size, mtime_ns)` が復元できるため**
    (マージ前レビューの P1-1)。`os.utime` / `touch -r` / `cp -p` / `rsync -t` /
    `tar -xp` は mtime を元に戻せるので、同一バイト数の書き換えと組み合わせると
    P3 が素通りする。ctime は `os.utime` では戻せない (実測)。ただし **ctime も
    代理変数**で、粒度の粗いファイルシステム (HFS+ / exFAT / bind mount) では
    同一秒内の書き換えを閉じられない — 最後の砦は P6 (内容指紋)。
    """
    try:
        st = os.stat(os.path.join(root, rel))
    except OSError:
        return [-1, -1, -1]
    return [st.st_size, st.st_mtime_ns, st.st_ctime_ns]


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


#: `changed_between` が見るスナップショットエントリの範囲 (`[code, size, mtime_ns]`)。
#: 4 要素目の `ctime_ns` は commit レビューの P3' 専用で**ここでは見ない** —
#: chmod / xattr の書き込みは ctime だけを動かすため、含めると他の書き手のファイルが
#: 「このセッションが変更した」として pending に入り、Stop の送信範囲が広がる。
_CHANGE_FIELDS = 3


def changed_between(pre: dict[str, list], post: dict[str, list]) -> list[str]:
    """2 つのスナップショットを比較し、変化した相対パスを返す。

    status から消えたパス (commit / checkout で HEAD と一致した等) も「変化した」
    扱いで返す。diff が空になるので Stop 側でそのまま落ちる。

    比較は `[code, size, mtime_ns]` まで (`_CHANGE_FIELDS`)。旧形式 (3 要素) の
    スナップショットとも比較できる。
    """
    changed = []
    for rel, meta in post.items():
        before = pre.get(rel)
        if before is None or list(before[:_CHANGE_FIELDS]) != list(meta[:_CHANGE_FIELDS]):
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


def changed_vs_head(root: str, rels: list[str]) -> set[str] | None:
    """`rels` のうち今も HEAD と差がある (= まだ未 commit の変更が残る) ものを返す。

    commit レビューを終えた後に「どのパスが commit で片付いたか」を **git 呼び出し
    1 回**で判定するために使う (`__main__._settle_commit_review`)。パスごとに
    `git diff HEAD -- <path>` を回すと最悪 60 回になり、PostToolUse の予算に収まらない。

    取得できなければ None。呼び出し側は「何も片付いていない」とみなす
    (pending に残るので、Stop がそのパスを見て通知する = 二重通知になるだけで、
    送信範囲は広がらない側の失敗)。
    """
    if not rels:
        return set()
    res = _git(
        root,
        ["diff", "--no-color", "--name-only", "-z", "HEAD", "--", *rels],
        timeout=LS_FILES_TIMEOUT_SEC,
    )
    if res is None or res.returncode != 0:
        return None
    return {p for p in _decode(res.stdout).split("\0") if p}


# --------------------------------------------------------------------------
# commit 単位 (reflog の old..new) の走査
# --------------------------------------------------------------------------


def head_log_snapshot(root: str) -> dict | None:
    """HEAD reflog ファイルの `{path, size, head}` を返す (`reflog.py` の窓の起点)。

    git 呼び出しは `rev-parse` **1 回だけ**。`--git-path logs/HEAD` と `HEAD` を同じ
    呼び出しで解決する (実測: 両方あれば 2 行・rc 0、HEAD が無い repo は rc 128 でも
    1 行目にパスが出る)。

    - `--git-path` は main worktree では cwd 相対 (`.git/logs/HEAD`)、linked worktree
      では絶対パスを返すため、`os.path.join(root, ...)` で吸収する (git 2.50.1 実測)
    - HEAD が解決できない (初回 commit 前 / 壊れている) ときは `head` を空文字列にする。
      `reflog._continues()` がそのときだけ全ゼロの `<old>` を受け入れる
    - reflog ファイルが無ければ `size` は 0 (追記ゼロの起点として正しい)
    """
    res = _git(root, ["rev-parse", "--git-path", "logs/HEAD", "HEAD"], timeout=REV_PARSE_TIMEOUT_SEC)
    if res is None:
        return None
    lines = _decode(res.stdout).splitlines()
    if not lines or not lines[0].strip():
        return None
    path = os.path.join(root, lines[0].strip())
    head = ""
    if res.returncode == 0 and len(lines) >= 2 and _SHA_RE.match(lines[1].strip()):
        head = lines[1].strip()
    try:
        size = os.path.getsize(path)
    except OSError:
        size = 0
    return {"path": path, "size": size, "head": head}


def empty_tree(root: str) -> str | None:
    """この repo の空ツリーの object id (最初の commit の diff 基点)。

    `4b825dc...` を直書きせずに引くのは、SHA-256 の repo では値が違うため。
    """
    res = _git(root, ["hash-object", "-t", "tree", os.devnull], timeout=REV_PARSE_TIMEOUT_SEC)
    if res is None or res.returncode != 0:
        return None
    value = _decode(res.stdout).strip()
    return value if _SHA_RE.match(value) else None


def range_paths(root: str, old: str, new: str) -> list[str] | None:
    """`old..new` で変更されたパス (root 相対)。取得できなければ None。

    None と空リストを区別するのが要点: 取得失敗を「変更なし」と読むと、
    「このセッションの編集記録が無いパス」の通知が黙って消える。
    """
    res = _git(
        root,
        ["diff", "--no-color", "--name-only", "-z", old, new],
        timeout=PATH_DIFF_TIMEOUT_SEC,
    )
    if res is None or res.returncode != 0:
        return None
    return [p for p in _decode(res.stdout).split("\0") if p]


def flagged_index_entries(root: str, rels: list[str]) -> set[str] | None:
    """`rels` のうち index のタグが `H` (通常) 以外のもの。取得できなければ None。

    commit レビューの **P4'**。`git diff HEAD` は `assume-unchanged` (小文字タグ) と
    `skip-worktree` (`S`) のエントリで**作業ツリーを見ない**ため、P4 の
    「`git diff HEAD` が空 = 作業ツリーの内容がそのまま commit された」が偽になる
    (マージ前レビューの P1-2 で、index に置いた他者版が送信されることを実演済み)。
    unmerged (`M`) など他のタグも「作業ツリー = HEAD」を意味しないので同じく落とす。

    `git ls-files -v` の出力 (git 2.50.1 実測):

        h a.txt      # assume-unchanged
        S b.txt      # skip-worktree
        H c.txt      # 通常

    None を返した窓は fail-closed (呼び出し側が窓ごと諦める) — 「一部だけ取れた」で
    先へ進むと、送ってはいけないパスが素通りする。
    """
    if not rels:
        return set()
    res = _git(root, ["ls-files", "-v", "-z", "--", *rels], timeout=LS_FILES_TIMEOUT_SEC)
    if res is None or res.returncode != 0:
        return None
    flagged: set[str] = set()
    for token in _decode(res.stdout).split("\0"):
        if len(token) > 2 and token[1] == " " and token[0] != "H":
            flagged.add(token[2:])
    return flagged


def blob_digests(
    root: str, rev: str, rels: list[str], max_bytes: int, total_max_bytes: int
) -> dict[str, str]:
    """`rev:<rel>` の blob の**生バイトの sha256** を rel ごとに返す (commit レビューの P6)。

    取れなかったパス (blob でない / 存在しない / 上限超過 / git 失敗) は返り値に
    **入れない** — 呼び出し側は「指紋と一致しなかった」と同じ扱いにする (fail-closed)。

    git は 2 回呼ぶ:

    1. `cat-file --batch-check` で `(oid, type, size)` だけ取る。**ここで
       `blob` かつ `size <= max_bytes`、累計 `total_max_bytes` 以内に絞る** —
       絞らずに `--batch` へ渡すと、窓の中で巨大ファイルに差し替えて commit された
       ケースで hook のメモリに数百 MB を載せてしまう
    2. 残ったパスだけ `cat-file --batch` で本文を読む

    入力は**改行区切り** (`-z` は新しめの git にしか無いので使わない)。このため
    パス名に改行を含む rel は最初から外す (= 指紋なし扱い)。出力は入力順に 1:1 で
    並ぶが、順序だけに頼らず **1 で得た oid を 2 のヘッダと突き合わせて**ズレを
    検出する (ズレたまま読むと別パスの内容で指紋が一致しうる)。
    """
    usable = [r for r in rels if r and "\n" not in r]
    if not usable:
        return {}
    payload = "".join(f"{rev}:{rel}\n" for rel in usable).encode()
    res = _git(root, ["cat-file", "--batch-check"], timeout=CAT_FILE_TIMEOUT_SEC, stdin_data=payload)
    if res is None or res.returncode != 0:
        return {}

    lines = _decode(res.stdout).split("\n")
    wanted: list[tuple[str, str]] = []  # (rel, oid)
    used = 0
    for rel, line in zip(usable, lines):
        parts = line.rsplit(" ", 2)
        if len(parts) != 3 or parts[1] != "blob" or not parts[2].isdigit():
            continue  # missing / ambiguous / tree / commit (submodule) / symlink 以外
        size = int(parts[2])
        if size > max_bytes or used + size > total_max_bytes:
            continue
        used += size
        wanted.append((rel, parts[0]))
    if not wanted:
        return {}

    payload = "".join(f"{rev}:{rel}\n" for rel, _oid in wanted).encode()
    res = _git(root, ["cat-file", "--batch"], timeout=CAT_FILE_TIMEOUT_SEC, stdin_data=payload)
    if res is None or res.returncode != 0:
        return {}
    return _parse_batch_blobs(res.stdout or b"", wanted)


def _parse_batch_blobs(out: bytes, wanted: list[tuple[str, str]]) -> dict[str, str]:
    """`cat-file --batch` の出力を `<rel> -> sha256` に畳む (`blob_digests` の後半)。

    1 件の形は `<oid> SP blob SP <size> LF <contents> LF`。**期待する oid と
    一致しなくなった時点で打ち切る** (ズレたまま読み進めると、別パスの内容から
    計算した指紋が偶然一致する経路を残してしまう)。
    """
    digests: dict[str, str] = {}
    pos = 0
    for rel, oid in wanted:
        newline = out.find(b"\n", pos)
        if newline < 0:
            break
        parts = out[pos:newline].rsplit(b" ", 2)
        pos = newline + 1
        if len(parts) != 3 or parts[1] != b"blob" or not parts[2].isdigit():
            break
        if parts[0].decode("utf-8", errors="replace") != oid:
            break  # 出力が入力とズレた
        size = int(parts[2])
        content = out[pos : pos + size]
        if len(content) != size:
            break
        pos += size + 1  # 本文の後ろの LF
        digests[rel] = hashlib.sha256(content).hexdigest()
    return digests


def range_diff(root: str, old: str, new: str, rel: str) -> str | None:
    """`old..new` の 1 パス分の diff テキスト。差分なしは空文字、**取得失敗は None**。

    `path_diff` と違い作業ツリーを見ない (両端とも commit) ので、Stop が見る
    「未 commit の変更」とは独立している。

    失敗を空文字に畳まないのは、呼び出し側が「差分なし」として黙って skip すると
    そのパスの唯一の commit レビュー機会が失われるため (`range_paths` が変更ありと
    判定したパスで空になるのは失敗しかない。マージ前レビューの指摘)。
    """
    res = _git(root, ["diff", "--no-color", old, new, "--", rel])
    if res is None or res.returncode != 0:
        return None
    return _decode(res.stdout)


def path_diff(root: str, rel: str, untracked: bool, has_head: bool) -> str:
    """1 パス分の diff テキストを返す。差分なし / 取得失敗なら空文字。

    `--no-color` は必須: `color.ui=always` / `color.diff=always` の環境では ANSI が混ざり、
    レビュアーに渡す本文が汚れるうえ hash もバイト予算も狂う。
    """
    if untracked:
        # untracked は HEAD 側に対応物が無いので /dev/null と比較する。
        # --no-index は差分ありで exit 1 を返すため returncode は見ない。
        res = _git(root, ["diff", "--no-color", "--no-index", "--", os.devnull, rel])
        return _decode(res.stdout) if res is not None else ""

    # 初回コミット前の repo には HEAD が無いので staged 差分で代替する
    base = "HEAD" if has_head else "--cached"
    res = _git(root, ["diff", "--no-color", base, "--", rel])
    if res is None or res.returncode != 0:
        return ""
    return _decode(res.stdout)
