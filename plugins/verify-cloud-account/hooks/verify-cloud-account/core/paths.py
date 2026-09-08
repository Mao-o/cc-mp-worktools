"""accounts.local.json 配置パスの定数と helper。

verify-cloud-account は accounts.local.json の配置パスを 3 世代サポートする。
新規プロジェクトは `.claude/verify-cloud-account/accounts.local.json` (new) を
使い、旧パスは deprecation 案内しつつ後方互換で受け入れる。

このモジュールは `scripts/accounts_builder.py` と `core/dispatcher.py` の
両方から参照される。定数で配置パスを一元管理し、`accounts_file_new()` は
assertion で basename が "accounts.local.json" であることを保証する
(builder がこのパスに書き込むため、argv 経由で書込先が変わる余地を排除する)。
"""
from __future__ import annotations

from pathlib import Path

ACCOUNTS_FILE_NEW = Path(".claude") / "verify-cloud-account" / "accounts.local.json"
ACCOUNTS_FILE_DEPRECATED = Path(".claude") / "accounts.local.json"
ACCOUNTS_FILE_LEGACY = Path(".claude") / "accounts.json"

_ALLOWED_BASENAME = "accounts.local.json"


def accounts_file_new(project_dir: str) -> Path:
    """現行推奨パス (`<project>/.claude/verify-cloud-account/accounts.local.json`) を返す。

    basename が "accounts.local.json" であることを assert で保証する。
    builder はこのパスにのみ書き込むため、変更経路を固定する責務がある。
    """
    p = Path(project_dir) / ACCOUNTS_FILE_NEW
    assert p.name == _ALLOWED_BASENAME, (
        "accounts_file_new must point to accounts.local.json "
        f"(got {p.name!r})"
    )
    return p


def accounts_file_deprecated(project_dir: str) -> Path:
    """旧 "deprecated" パス (`.claude/accounts.local.json`) の絶対パス。"""
    return Path(project_dir) / ACCOUNTS_FILE_DEPRECATED


def accounts_file_legacy(project_dir: str) -> Path:
    """legacy パス (`.claude/accounts.json`) の絶対パス。"""
    return Path(project_dir) / ACCOUNTS_FILE_LEGACY


def discover_all_accounts_files(project_dir: str) -> list[tuple[str, Path]]:
    """配置候補のうち存在するものを (kind, absolute_path) のリストで返す。

    kind は "new" / "deprecated" / "legacy" のいずれか。優先度順
    (new → deprecated → legacy) で並ぶ。

    返却リストの長さが 2 以上なら、dispatcher は fail-closed で deny する。
    """
    candidates = [
        ("new", accounts_file_new(project_dir)),
        ("deprecated", accounts_file_deprecated(project_dir)),
        ("legacy", accounts_file_legacy(project_dir)),
    ]
    return [(kind, path) for kind, path in candidates if path.is_file()]


# 親ディレクトリ遡及の最大階層数。`project_dir` 自身を含めてこの段数まで
# 探索する。git worktree が `<repo>/.worktrees/<branch>/<subdir>/...` の
# ように深く配置されていても十分到達できる範囲を取りつつ、上方暴走を
# 防ぐためのガード値。**階層数だけでは上限にならない** (下記の境界も参照)。
ANCESTOR_SEARCH_MAX_LEVELS = 10


# `.git` ファイル (gitdir ポインタ) を読むときの上限バイト数。git が書くのは
# `gitdir: <path>` の 1 行だけなので、これを超える内容は判読対象にしない。
_GITDIR_FILE_MAX_BYTES = 4096
_GITDIR_PREFIX = "gitdir:"


def _split_path_components(raw: str) -> list[str]:
    """gitdir の値を OS 非依存にパス要素へ分解する。

    `.git` ファイルは POSIX 形式でも Windows 形式でも書かれうるため、`/` と
    `\\` の両方を区切りとして扱い、空要素と `.` を落とす。
    """
    return [
        part
        for part in raw.replace("\\", "/").split("/")
        if part not in ("", ".")
    ]


def _classify_gitdir_pointer(text: str) -> str:
    """`.git` ファイルの内容を "worktree" / "submodule" / "unknown" に分類する。

    git の実 gitdir は `<repo>/.git` 以下が `modules/<name>` と
    `worktrees/<name>` の繰り返しになる:

      - linked worktree : `<repo>/.git/worktrees/<name>`
      - submodule       : `<super>/.git/modules/<name>`
      - submodule の worktree: `<super>/.git/modules/<name>/worktrees/<name>`

    最後に現れたキーワードが種別を決める。この形に当てはまらないもの
    (`--separate-git-dir` で `.git` の外を指す形、prefix 違い、読めない内容)
    は **"unknown"** とし、呼び出し側で停止側 (fail-closed) に倒す。
    """
    gitdir = None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.lower().startswith(_GITDIR_PREFIX):
            gitdir = stripped[len(_GITDIR_PREFIX):].strip()
        break
    if not gitdir:
        return "unknown"

    parts = _split_path_components(gitdir)
    try:
        anchor = len(parts) - 1 - parts[::-1].index(".git")
    except ValueError:
        return "unknown"
    rest = parts[anchor + 1:]
    if not rest or len(rest) % 2 != 0:
        return "unknown"

    kind = "unknown"
    for index in range(0, len(rest), 2):
        keyword = rest[index]
        if keyword == "worktrees":
            kind = "worktree"
        elif keyword == "modules":
            kind = "submodule"
        else:
            return "unknown"
    return kind


def _is_repo_boundary(directory: Path) -> bool:
    """`directory` が遡及を止めるべき repo 境界か。

    - `.git` が **ディレクトリ** → 通常の repo toplevel → 境界
    - `.git` が **ファイル** (gitdir ポインタ) → 内容で分岐する
      - linked worktree (`.git/worktrees/<name>`) → **境界ではない**。worktree
        から親 repo の設定を継承する運用 (`<repo>/.worktrees/<branch>` が cwd)
        を残すため、従来どおり親 repo の toplevel まで上らせる
      - submodule (`.git/modules/<name>`) → **境界**。submodule root は独立した
        repo の境界であり、superproject の accounts.local.json を継承させると
        未設定の submodule で状態変更コマンドが素通りする
      - 判読できない内容 → **境界** (fail-closed)。継承先が増える方向へ倒すと
        deny すべき場面を allow してしまうため、分からない場合は止める
    - `.git` が無い → 境界ではない

    判定は `.git` の**読み取りだけ**で行う (git コマンドは呼ばない)。
    """
    dot_git = directory / ".git"
    try:
        if dot_git.is_dir():
            return True
        if not dot_git.is_file():
            return False
    except OSError:
        # 種別を確かめられない = 境界かどうか分からない → 停止側に倒す
        return True

    try:
        with dot_git.open("rb") as handle:
            raw = handle.read(_GITDIR_FILE_MAX_BYTES)
    except OSError:
        return True
    kind = _classify_gitdir_pointer(raw.decode("utf-8", errors="replace"))
    return kind != "worktree"


def _home_dir() -> Path | None:
    """`$HOME` の絶対パス (取得不能なら None)。"""
    try:
        return Path.home().resolve()
    except (RuntimeError, OSError):
        return None


def _crosses_home(candidate: Path, home: Path | None) -> bool:
    """`candidate` へ上ると `$HOME` かその上 (`/Users`, `/` 等) に入るなら True。"""
    if home is None:
        return False
    try:
        return home == candidate or home.is_relative_to(candidate)
    except (OSError, ValueError):
        return False


def discover_accounts_files_with_ancestors(
    project_dir: str,
    *,
    max_levels: int = ANCESTOR_SEARCH_MAX_LEVELS,
) -> tuple[list[tuple[str, Path]], Path | None]:
    """`project_dir` から親ディレクトリへ遡って accounts.local.json を探す。

    各階層で `discover_all_accounts_files()` を呼び、最初に non-empty を
    返した階層の `(found_list, resolved_dir)` を返す。worktree が親 repo の
    `.claude/verify-cloud-account/accounts.local.json` を共有する運用に
    対応する。**worktree 自体に accounts.local.json を作りたくない**という
    要請を満たすため、cwd で見つからなくても親側で見つかれば採用する。

    探索ポリシー:
      - cwd 階層に何か 1 つでも見つかれば、そこで採用判定する
        (親階層は見ない、cwd 優先)
      - 同一階層に複数 tier が同居する場合は呼び出し側で fail-closed (D4)
      - **git repo の境界を越えない** — `.git` ディレクトリを持つ階層
        (通常の toplevel) と、`.git` ファイルが submodule の gitdir
        (`.git/modules/<name>`) を指す階層 (submodule root)。その階層自身は
        探すが、その親へは上らない。linked worktree
        (`.git/worktrees/<name>`) だけは境界にせず親 repo まで上らせる
      - **`$HOME` およびその上 (`/Users`, `/` 等) へは上らない**
      - 何も見つからずに `Path.parent == Path` (ルート) に到達したら諦める
      - `max_levels` で安全側の上限を設ける

    探索開始階層 (`project_dir` 自身) は上の 2 つの境界に関係なく必ず探す
    (`$HOME` 直下や repo toplevel をプロジェクトにしている場合を落とさない)。

    境界を足した理由 (内部バックログ): 従来は階層数だけが上限だったため、
    `/Users/<u>/dev/<org>/<repo>` のような配置では 5 階層で `$HOME` に届き、
    **無関係な `~/.claude/accounts.json` を継承して検証していた**。しかも
    verify 成功時は継承注釈が出ない (silent) ため気付けない。落とす方向
    (見つからず deny) は fail-closed なので安全側。`$HOME` や repo より上に
    グローバル既定を置く用途の専用経路は現時点では無い (別途検討)。

    submodule を境界にした理由 (マージ前レビューの指摘): submodule root の
    `.git` も**ファイル**のため、ファイル形を一律に通過扱いすると探索が
    superproject へ続く。superproject 側に active な CLI と一致する
    accounts.local.json があると、**未設定の submodule で状態変更コマンドが
    repo 境界で fail-closed せずに allow される**。`.git` ファイルの内容
    (`gitdir:` の指す先の形) で linked worktree と submodule を区別し、
    判読できない場合は停止側に倒す。

    gitdir は**分類にしか使わず、探索先としては辿らない**。linked worktree が
    repo の**外**に置かれている場合 (gitdir が別の場所を指す形) に親 repo へ
    届かないのは従来どおり (遡及はファイルシステムの親方向にしか進まない)。
    新たな探索経路を増やすと「見つかる場所が増える」= allow 側に倒れるため、
    本件の趣旨 (拾いすぎを止める) と逆方向になる。

    Args:
        project_dir: 検索を開始するディレクトリ (絶対パス推奨)。
        max_levels: 親を遡る最大階層数 (`project_dir` 自身を含む)。

    Returns:
        (found_list, resolved_dir):
          - found_list: 採用階層で見つかった `(kind, path)` のリスト。
            複数 (>=2) なら呼び出し側で fail-closed 判定すること。
          - resolved_dir: 採用した階層の絶対パス。何も見つからなければ None。
    """
    try:
        current = Path(project_dir).resolve()
    except OSError:
        return [], None
    home = _home_dir()
    for _ in range(max_levels):
        found = discover_all_accounts_files(str(current))
        if found:
            return found, current
        if _is_repo_boundary(current):
            break
        parent = current.parent
        if parent == current:
            break
        if _crosses_home(parent, home):
            break
        current = parent
    return [], None


def resolve_accounts_file(
    project_dir: str,
) -> tuple[Path | None, str | None, list[tuple[str, Path]], Path | None]:
    """`project_dir` から見て **hook が実際に読む** accounts.local.json を決める。

    3-tier lookup (`discover_all_accounts_files`) と親ディレクトリ遡及
    (`discover_accounts_files_with_ancestors`) を組み合わせた唯一の解決関数。
    `core/dispatcher.py` (検証時) と `scripts/accounts_builder.py` (編集時) の
    **両方がこれを呼ぶ**: builder が独自に「cwd 直下の新パス」を対象にすると、
    親から継承しているプロジェクト (worktree / サブディレクトリ) で編集した
    service だけを含む子ファイルが生まれ、dispatcher の遡及がその子ファイルで
    止まって継承していた他 service が全て未設定になる。読む側と書く側で解決を
    共有することでこの shadowing を構造的に防ぐ。

    Returns:
        (path, kind, conflicts, resolved_dir):
          - path: 採用するファイルのパス。見つからない or 競合時は None
          - kind: "new" / "deprecated" / "legacy" のいずれか (採用されたもの)
          - conflicts: 同一階層に複数 tier が存在した場合の検出リスト
                       (採用は保留。呼び出し側で fail-closed deny する — D4)
          - resolved_dir: 採用 (または競合検出) した階層の絶対パス。親遡及で
                          project_dir の祖先を採用した場合はその祖先。
                          何も見つからなければ None
    """
    found, resolved_dir = discover_accounts_files_with_ancestors(project_dir)
    if len(found) >= 2:
        return None, None, found, resolved_dir
    if len(found) == 1:
        kind, path = found[0]
        return path, kind, [], resolved_dir
    return None, None, [], None
