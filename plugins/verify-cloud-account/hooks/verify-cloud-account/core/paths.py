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

import re
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


def _parse_gitdir_value(text: str) -> str | None:
    """`.git` ファイルの内容から `gitdir:` の値を取り出す (無ければ None)。"""
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.lower().startswith(_GITDIR_PREFIX):
            value = stripped[len(_GITDIR_PREFIX):].strip()
            return value or None
        break
    return None


def _resolve_gitdir_value(directory: Path, raw: str, parts: list[str]) -> Path | None:
    """gitdir の値を絶対パスへ正規化する (`.git` ファイルのある階層が基準)。

    `Path.resolve()` を通すため、`..` と symlink を含んだ表記でも同じ実体が
    同じパスに落ちる。区切りは `/` と `\\` の両方を受ける。
    """
    if not parts:
        return None
    normalized = raw.replace("\\", "/")
    try:
        if _is_windows_absolute(normalized):
            # ドライブ文字 (`C:/...`) / UNC (`//server/share/...`) は絶対パス。
            # 相対として directory に繋ぐと別の common dir と比較してしまい、
            # 正当な linked worktree を境界と誤判定する (マージ前レビューの指摘)。
            target = Path(normalized)
        elif normalized.startswith("/"):
            target = Path("/", *parts)
        else:
            target = directory.joinpath(*parts)
        return target.resolve()
    except (OSError, ValueError):
        return None


_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:/")


def _is_windows_absolute(normalized: str) -> bool:
    """`/` 区切りに正規化済みの文字列が Windows の絶対パスかを返す。"""
    return bool(_WINDOWS_DRIVE_RE.match(normalized)) or normalized.startswith("//")


def _classify_gitdir_parts(parts: list[str]) -> str:
    """gitdir のパス要素を "worktree" / "submodule" / "plain" に分類する。

    git が `.git` ファイルに書く gitdir は、**common directory からの相対で**
    `worktrees/<name>` / `modules/<name>` という末尾を持つ:

      - linked worktree : `<common>/worktrees/<name>`
      - submodule       : `<common>/modules/<name>`
      - submodule の worktree: `<common>/modules/<name>/worktrees/<name>`

    判定は **末尾 2 要素だけ**を見る。common directory の名前は `.git` とは
    限らない — bare repository (`<name>.git`) や `--separate-git-dir` で
    初期化した repo から作った linked worktree は `repo.git/worktrees/<name>` /
    `/custom/gitdir/worktrees/<name>` のようになり、パス中に `.git` という
    要素が現れない。`.git` という名前を要求すると、これらの正当な linked
    worktree が判読不能 = 境界に落ちる (マージ前レビューの指摘)。

    どちらの末尾でもないもの (repo 本体の gitdir を直接指す
    `--separate-git-dir` の main worktree など) は "plain" — その階層自身は
    独立した repo の root なので境界として扱う。
    """
    if len(parts) < 2:
        return "plain"
    # 末尾は `<keyword>/<name>`。入れ子 (`modules/a/modules/b` は submodule、
    # `modules/sub/worktrees/wt` は worktree) も最後の keyword で決まる。
    keyword = parts[-2]
    if keyword == "worktrees":
        return "worktree"
    if keyword == "modules":
        return "submodule"
    return "plain"


def _inspect_dot_git(directory: Path) -> tuple[str, Path | None]:
    """`<directory>/.git` を読んで (種別, gitdir の絶対パス) を返す。

    種別:
      - "none"       : `.git` が無い
      - "dir"        : `.git` が **ディレクトリ** (通常の repo toplevel)
      - "worktree"   : gitdir が `<common>/worktrees/<name>`
      - "submodule"  : gitdir が `<common>/modules/<name>`
      - "plain"      : gitdir が repo 本体の git directory を直接指す形
      - "unreadable" : `.git` はあるが種別を確定できない

    判定は `.git` の**読み取りだけ**で行う (git コマンドは呼ばない)。
    """
    dot_git = directory / ".git"
    try:
        if dot_git.is_dir():
            try:
                return "dir", dot_git.resolve()
            except (OSError, ValueError):
                return "unreadable", None
        if not dot_git.is_file():
            return "none", None
    except OSError:
        # 種別を確かめられない = 境界かどうか分からない → 停止側に倒す
        return "unreadable", None

    try:
        with dot_git.open("rb") as handle:
            raw = handle.read(_GITDIR_FILE_MAX_BYTES)
    except OSError:
        return "unreadable", None
    value = _parse_gitdir_value(raw.decode("utf-8", errors="replace"))
    if not value:
        return "unreadable", None
    parts = _split_path_components(value)
    target = _resolve_gitdir_value(directory, value, parts)
    if target is None:
        return "unreadable", None
    return _classify_gitdir_parts(parts), target


def _common_git_dir(directory: Path) -> tuple[bool, Path | None]:
    """`directory` が属する repo の **common git directory** を求める。

    2 つのディレクトリが同じ repo に属するかは、この common directory が
    一致するかで判定できる (linked worktree もその common directory で
    親 repo と結び付く)。

    Returns:
        (has_marker, common):
          - has_marker: `.git` が存在したか (種別不明でも True)
          - common: 求まった common git directory。求まらなければ None
    """
    kind, target = _inspect_dot_git(directory)
    if kind == "none":
        return False, None
    if kind == "dir":
        return True, target
    if kind == "worktree" and target is not None:
        # `<common>/worktrees/<name>` → `<common>`
        return True, target.parent.parent
    if kind in ("submodule", "plain") and target is not None:
        # submodule / repo 本体は gitdir 自身が common directory
        return True, target
    return True, None


def _ancestor_repo_owns(
    directory: Path,
    common: Path,
    *,
    home: Path | None,
    levels: int,
) -> bool:
    """`directory` の祖先側に、`common` を持つ repo があるか。

    linked worktree の `.git` が指す先は「どの repo に属するか」しか教えて
    くれない。**その repo が、この後探索する祖先ディレクトリのものであること**
    を確かめないと、無関係な repo A の中に置かれた repo B の worktree から
    repo A の accounts.local.json を継承してしまう (マージ前レビューの指摘)。

    走査は探索と同じ方向 (ファイルシステムの親方向) にしか進まず、同じ停止
    条件 (`$HOME` / ルート / 階層数) を使う。**最初に見付かった git marker**
    で判定する — それより上は「その repo の中」であり、間の階層も含めて
    その repo に属するため。

    Returns:
        True  : 最初に見付かった祖先 repo の common directory が一致した、
                または探索範囲の祖先に repo が 1 つも無かった (外側 repo が
                存在しないので継承元を取り違えようがない)
        False : 一致しない repo が外側にある、または祖先の種別を確定できない
                (比較できない = fail-closed)
    """
    current = directory
    for _ in range(max(levels, 0)):
        parent = current.parent
        if parent == current:
            break
        if _crosses_home(parent, home):
            break
        current = parent
        has_marker, ancestor_common = _common_git_dir(current)
        if not has_marker:
            continue
        return ancestor_common is not None and ancestor_common == common
    return True


def _is_repo_boundary(
    directory: Path,
    *,
    home: Path | None = None,
    ancestor_levels: int = ANCESTOR_SEARCH_MAX_LEVELS,
) -> bool:
    """`directory` が遡及を止めるべき repo 境界か。

    - `.git` が **ディレクトリ** → 通常の repo toplevel → 境界
    - `.git` が **ファイル** (gitdir ポインタ) → 内容で分岐する
      - linked worktree (gitdir が `<common>/worktrees/<name>`) → **`<common>`
        を持つ repo が祖先側にある (または祖先に repo が無い) ときだけ境界に
        しない**。worktree から親 repo の設定を継承する運用
        (`<repo>/.worktrees/<branch>` が cwd) はそのまま通り、無関係な repo の
        中に置かれた worktree はその root で止まる
      - submodule (gitdir が `<common>/modules/<name>`) → **境界**。submodule
        root は独立した repo の境界であり、superproject の accounts.local.json
        を継承させると未設定の submodule で状態変更コマンドが素通りする
      - 判読できない内容 → **境界** (fail-closed)。継承先が増える方向へ倒すと
        deny すべき場面を allow してしまうため、分からない場合は止める
    - `.git` が無い → 境界ではない

    判定は `.git` の**読み取りだけ**で行う (git コマンドは呼ばない)。

    Args:
        directory: 判定対象の階層。
        home: `$HOME` (祖先走査の停止条件に使う)。
        ancestor_levels: この後探索されうる祖先の段数。linked worktree の
            所属確認をこの範囲に限る。
    """
    kind, target = _inspect_dot_git(directory)
    if kind == "none":
        return False
    if kind != "worktree" or target is None:
        return True
    common = target.parent.parent
    return not _ancestor_repo_owns(
        directory, common, home=home, levels=ancestor_levels
    )


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
        (`<common>/modules/<name>`) を指す階層 (submodule root)。その階層自身は
        探すが、その親へは上らない。linked worktree
        (`<common>/worktrees/<name>`) は、`<common>` を持つ repo が祖先側に
        あるとき (または祖先に repo が無いとき) だけ境界にせず上らせる
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
    判読できない場合は停止側に倒す。種別は gitdir の**末尾 2 要素**
    (`worktrees/<name>` か `modules/<name>` か) で決め、common directory の
    名前が `.git` であることには依存しない — bare repository や
    `--separate-git-dir` から作った linked worktree はパス中に `.git` 要素を
    持たないため、その名前を要求すると正当な worktree が境界に落ちてしまう
    (マージ前レビューの指摘)。

    linked worktree の通過に**所属確認**を課した理由 (マージ前レビューの指摘):
    正当な linked worktree は無関係な repo の中にも置ける (repo A の
    `repo-A/vendor/b-wt` に repo B の worktree を追加する形)。gitdir の形だけで
    通過させると、探索が repo B を離れて **repo A の accounts.local.json を
    継承**し、repo A の期待アカウントが active session と一致すれば未設定の
    repo B worktree で状態変更コマンドが allow される。そのため gitdir の
    common directory (`<common>/worktrees/<name>` の `<common>`) が、この後
    探索する祖先の repo のものであることを確かめ、確かめられなければ worktree
    root を境界にする。祖先に repo が 1 つも無い場合は継承元を取り違えようが
    ないので従来どおり上らせる (repo の外に置いた worktree が workspace 直下の
    設定を継承する運用)。祖先の種別が判読できない場合は停止側に倒す。

    gitdir は**所属の判定にしか使わず、探索先としては辿らない**。linked
    worktree が repo の**外**に置かれている場合 (gitdir が別の場所を指す形) に
    親 repo へ届かないのは従来どおり (遡及はファイルシステムの親方向にしか
    進まない)。新たな探索経路を増やすと「見つかる場所が増える」= allow 側に
    倒れるため、本件の趣旨 (拾いすぎを止める) と逆方向になる。

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
    for level in range(max_levels):
        found = discover_all_accounts_files(str(current))
        if found:
            return found, current
        # この後まだ探索されうる祖先の段数 (linked worktree の所属確認の範囲)
        remaining_ancestors = max_levels - level - 1
        if _is_repo_boundary(
            current, home=home, ancestor_levels=remaining_ancestors
        ):
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
