"""機密ファイルパターン検出 — Stop hook 用。

matcher / patterns のロジックは ``_shared`` パッケージに一元化されている。
このモジュールは:
- ``_shared.patterns.load_patterns`` を Stop 固有の warn_callback で呼ぶ
- ``_shared.matcher.is_sensitive`` で評価する
- git 管理下の tracked / untracked ファイル一覧を取得する
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from _shared.matcher import is_sensitive, root_relative
from _shared.patterns import (
    _parse_patterns_text,
    _resolve_local_patterns_path,
)
from _shared.patterns import load_patterns as _shared_load_patterns


def _warn_local(msg: str) -> None:
    """Stop hook 側の warn_callback — patterns.local.txt の OS エラーを stderr に
    1 行記録する。

    0.6.0 で 2-tier lookup の fallback を削除したため、deprecation 分岐は不要。
    """
    sys.stderr.write(
        f"[check-sensitive-files] local_patterns_unavailable: {msg}\n"
    )


def _warn_migrate(token: str) -> None:
    """Stop hook 側の migrate_warn_callback — rename 前の旧 patterns.local.txt を
    fallback 読み込みしたことを stderr に 1 行記録し、移行を促す。

    新パス ``~/.claude/sensitive-files-guardrail/patterns.local.txt`` へ ``mv``
    するよう案内する (token はパスを含まない固定文字列)。
    """
    sys.stderr.write(
        f"[check-sensitive-files] {token}: "
        "rename 前の ~/.claude/sensitive-files-guard/patterns.local.txt を "
        "fallback で読込中。"
        "~/.claude/sensitive-files-guardrail/patterns.local.txt へ移動してください\n"
    )


def _warn_header(token: str) -> None:
    """Stop hook 側の header_warn_callback — patterns.local.txt の ``[project:]``
    ヘッダーが空 / 未展開 placeholder のとき stderr に 1 行記録する (0.19.0)。

    そのセクションは黙って捨てられる (除外が効かず block が続く) ため、原因を
    可視化する。token はパスを含まない固定文字列。
    """
    sys.stderr.write(
        f"[check-sensitive-files] local_patterns_header_invalid: {token} "
        "([project:...] ヘッダーはプロジェクト root の絶対パスを literal に書く)\n"
    )


def load_patterns(patterns_file: Path, cwd: str = "") -> list[tuple[str, bool]]:
    """既定 patterns.txt + ローカル patterns.local.txt を読んで rules list を返す。

    Stop 側は hook 間の Python 依存を避けるため stderr 直書きで warn する
    (``core.logging`` を import しない)。``cwd`` は ``[project:<path>]``
    セクションの一致判定に使う (``_shared.patterns.load_patterns`` 参照)。
    """
    return _shared_load_patterns(
        patterns_file,
        warn_callback=_warn_local,
        migrate_warn_callback=_warn_migrate,
        cwd=cwd,
        header_warn_callback=_warn_header,
    )


def _run_git_raw(args: list[str], cwd: str) -> "subprocess.CompletedProcess[str] | None":
    """git を実行して ``CompletedProcess`` を返す。呼出自体の失敗は ``None``。

    ``_run_git`` / ``_run_git_nul`` の共通土台 (内部バックログ)。git_unavailable
    の stderr 報告をここに一元化する。

    ``FileNotFoundError`` (git 実行ファイルが無い) / ``TimeoutExpired``
    (プロセスが応答しない) は git **呼出そのもの**の失敗であり、区別できないと
    「機密ファイルなし」(= 対象が git リポジトリでない等、git 自体は起動できたが
    非ゼロで終了した場合。これは正常系) と同じ沈黙に落ちて検査が実行されな
    かったことに気づけない。patterns_unavailable と同じ形式で、この関数の
    呼出ごとに stderr へ 1 行報告する (呼出全体を通して「1 回」ではない —
    1 回の Stop hook 実行で `rev-parse` / `ls-files` 系 / (P2-1 以降は)
    submodule のネスト段数ぶん、複数回 git を呼びうるため、失敗が複数箇所で
    起きれば複数行になる)。fail-open の挙動 (呼出元は引き続き空リストとして
    扱う) 自体は変えない — 可視性を足すだけで判定境界には触れない。

    既知の残課題: ``PermissionError`` (git はあるが実行権限がない) は
    ``OSError`` の subclass だがこの ``except`` 節では捕捉しない (意図的 —
    ``FileNotFoundError`` / ``TimeoutExpired`` 以外の ``OSError`` まで広げると
    予期しない例外を fail-open に倒す範囲が広がり、判定表を変えない、という
    本件のスコープを超える)。
    """
    try:
        return subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        sys.stderr.write(
            f"[check-sensitive-files] git_unavailable: {type(e).__name__}\n"
        )
        return None


def _run_git(args: list[str], cwd: str) -> list[str]:
    """git コマンドを実行して行 (改行区切り) のリストを返す。失敗時は空リスト。

    ``returncode != 0`` (対象が git リポジトリでない等) は正常系として黙って
    ``[]`` を返す。呼出自体の失敗の扱いは ``_run_git_raw`` を参照。
    """
    result = _run_git_raw(args, cwd)
    if result is None or result.returncode != 0:
        return []
    return [line for line in result.stdout.splitlines() if line.strip()]


def _run_git_nul(args: list[str], cwd: str) -> list[str]:
    """git コマンドを実行して NUL 区切り (``-z``) の要素リストを返す。

    ``git ls-files -z`` は non-ASCII / 特殊文字を含む path が
    ``core.quotePath`` によって 8 進エスケープの引用符付き文字列に変換される
    (改行区切りだと誤ってその形のまま 1 要素として返ってしまう) のを避ける
    標準的な使い方 (``submodule_paths`` が使用)。
    """
    result = _run_git_raw(args, cwd)
    if result is None or result.returncode != 0:
        return []
    return [item for item in result.stdout.split("\0") if item]


def is_git_repo(cwd: str) -> bool:
    """cwd が git リポジトリ内かどうか"""
    result = _run_git(["rev-parse", "--is-inside-work-tree"], cwd)
    return bool(result) and result[0] == "true"


def repo_context(cwd: str) -> tuple[str, str] | None:
    """cwd が git 作業ツリー内なら ``(toplevel, prefix)`` を返す (0.19.0)。

    - ``toplevel``: repo root の絶対パス (``git rev-parse --show-toplevel``)
    - ``prefix``: cwd の repo root からの相対パス (``--show-prefix``、末尾 ``/``。
      root なら空文字列)

    1 回の ``git rev-parse`` で両方を得る (``is_git_repo`` の呼出を置き換えるので
    Stop hook の git 呼出回数は増えない)。作業ツリー外 / 失敗は None。
    stop-ack の digest を「repo root + root 相対 path」で作るために使う —
    ``git ls-files`` は cwd 相対で出力するため、root とサブディレクトリで同じ
    物理ファイルが別 digest にならないよう prefix を前置する (Codex R2 P2-2)。
    表示 (block reason) は従来通り cwd 相対のまま (``git rm --cached <path>`` を
    cwd でそのまま実行できる)。
    """
    lines = _run_git(["rev-parse", "--show-toplevel", "--show-prefix"], cwd)
    if not lines:
        return None
    toplevel = lines[0]
    # root では --show-prefix が空行を出し _run_git が落とすため 1 行しか無い
    prefix = lines[1] if len(lines) >= 2 else ""
    return toplevel, prefix


def _ls_tracked(cwd: str) -> list[str]:
    """tracked ファイル一覧を取得する (submodule 内の tracked を含む)。

    ``git ls-files --recurse-submodules`` を使用。未対応の古い git ではフラグが
    認識されず空リストが返るため、その場合は素の ``ls-files`` にフォールバック。
    submodule 内の**untracked** は ``--others`` と ``--recurse-submodules`` を
    組み合わせるサポートが無いため範囲外 (README 既知制限)。

    必要 git バージョン: 1.7+ (``--recurse-submodules`` 対応)。
    """
    result = _run_git(["ls-files", "--recurse-submodules"], cwd)
    if result:
        return result
    # fallback: --recurse-submodules 非対応の古い git、または repo が本当に空の場合
    return _run_git(["ls-files"], cwd)


def submodule_paths(
    cwd: str, _prefix: str = "", _visited: set[str] | None = None
) -> set[str]:
    """cwd から見える submodule mount path 一覧 (cwd 相対、ネスト込み) を返す
    (内部バックログ、P2-1 で手動再帰化)。

    ``git ls-files --stage`` は **直下**の submodule だけを gitlink
    (mode ``160000``) の 1 entry として返し、submodule 配下の submodule
    (ネスト) までは辿らない。``--recurse-submodules`` を ``--stage`` に
    足しても gitlink は返らない (git 2.50.1 実測)。tracked な機密ファイルが
    ネストした submodule 配下にあると、親 repo からの
    ``git rm --cached <path>`` は**そのファイルには効かない**
    (submodule は別の git index を持つため) にも関わらず、案内が外側の
    (実際には効かない) submodule ディレクトリを指してしまう — 各 submodule
    ディレクトリで ``git ls-files --stage`` を手動再帰して埋める。
    ``in_submodule`` はこの結果から最長一致 (= 最も深い submodule) を返す。

    再帰先ディレクトリ (``os.path.join(cwd, path)``) が実際に checkout 済みの
    submodule でなければ再帰しない。``git submodule add`` はネストした
    submodule の working copy までは自動で ``update --init`` しないため、
    ``git clone`` を ``--recurse-submodules`` 無しで行った直後などでは
    ディレクトリ自体は (gitlink 用の空の placeholder として) 存在するが
    ``.git`` を持たない — このとき ``git`` をそこで実行すると、cwd 自身が
    親の gitlink 位置と一致することから親の index にある同じ gitlink を
    ``./`` という自己参照パスで返してしまい (実測: git 2.50.1)、
    ``full + "./"`` のような不正な要素が集合に混入する。存在確認だけでは
    この placeholder ディレクトリを弾けない (``os.path.isdir`` は true を
    返す) ため、``.git`` の有無で「実際に checkout 済みか」を判定する。
    (``.git`` が無ければ独立した index も working copy も無いので、再帰しても
    ``os.path.isdir`` だけの旧チェックより安全に「対象なし」を返せる。
    ``FileNotFoundError`` (存在しない cwd で git を呼んで ``_run_git_raw`` が
    ``git_unavailable`` を誤検知する経路) も併せて防げる。)
    ``_visited`` は実パスの循環 (壊れた/悪意ある構成が symlink 等で祖先を
    指す防御的ケース、``TestNestedSubmoduleGuidancePaths
    .test_symlinked_submodule_cycle_terminates_without_recursion_error`` で
    再現・確認済み) を検出して安全に打ち切るための集合。この guard が無い
    場合の実際の挙動は OS / git のシンボリックリンク解決に依存し、
    macOS + git 2.50.1 の実測では symlink の循環が ELOOP 相当で早期に
    黙って解決不能になり (Python の再帰エラーには達しない)、代わりに
    ``vendor/deep/deep/deep/...`` のような**存在しない submodule を指す
    誤ったディレクトリ名**が結果に混入した (無駄な git 呼出も約 16 回分
    発生)。bind mount 等シンボリックリンクを経由しない循環では OS 側の
    ループ検出が効かず深い Python 再帰になりうるため、実装をどちらか一方の
    失敗モードに依存させず ``_visited`` で経路によらず確実に止める。
    呼出コストがあるため、tracked な機密ファイルが 1 件も無いときは
    呼ばないこと (呼出側の責務)。
    """
    visited = _visited if _visited is not None else set()
    real_cwd = os.path.realpath(cwd)
    if real_cwd in visited:
        return set()
    visited.add(real_cwd)

    paths: set[str] = set()
    for entry in _run_git_nul(["ls-files", "--stage", "-z"], cwd):
        # 形式: "<mode> <object> <stage>\t<path>" (gitlink は mode 160000)
        meta, sep, path = entry.partition("\t")
        if not sep or not path:
            continue
        if (meta.split(" ", 1)[0] if meta else "") != "160000":
            continue
        full = _prefix + path
        paths.add(full)
        sub_cwd = os.path.join(cwd, path)
        if os.path.exists(os.path.join(sub_cwd, ".git")):
            paths.update(submodule_paths(sub_cwd, full + "/", visited))
    return paths


def in_submodule(path: str, submod_paths: set[str]) -> str | None:
    """``path`` (cwd 相対) がどの submodule 配下にあるかを返す (無ければ None)。

    ``<submodule>/`` prefix 一致 (submodule 配下のファイル) だけを見る。
    ネストした submodule (例: ``vendor`` と ``vendor/deep`` の両方が
    submodule として ``submod_paths`` に入っている) では**最長一致**を返す
    — そのファイルを実際に持つ git index は常に一番深い submodule の
    ものだから (P2-1)。

    ``path`` が submodule path と**完全一致**する場合 (= gitlink そのもの)
    は意図的に対象外とする (P2-3, 外部レビュー R3)。未初期化 submodule
    (working copy が無い) では ``git ls-files --recurse-submodules`` が
    配下に再帰できず、gitlink の path 自体を通常の tracked entry として
    返す。この entry の実体は**親 repo の index が持つ gitlink**であり、
    submodule 自身の index には何も無い (未初期化なら index 自体が存在
    しない)。submodule のマウント名が機密パターンに一致すると (例:
    ``.env``) この完全一致が発生し、`git rm --cached` が親から直接効く
    にも関わらず「親では実行不可能、submodule ディレクトリに `cd` せよ」
    という実行不能な案内 (空の未初期化ディレクトリを指す) を出してしまって
    いた。gitlink は常に親 index 由来なので、完全一致は「submodule 配下
    ではない」= 通常の親 repo 向け案内のままにする。
    """
    matches = [sp for sp in submod_paths if path.startswith(sp + "/")]
    if not matches:
        return None
    return max(matches, key=len)


def root_offset(cwd: str, root: str | None) -> str | None:
    """``cwd`` の project root からの相対 prefix を返す (0.24.0)。

    - ``cwd`` が root 自身 → ``""``
    - root 配下のサブディレクトリ → ``"sub/dir"`` (POSIX 区切り、末尾 ``/`` 無し)
    - root 不明 (None) / ``cwd`` が root 配下でない → None

    ``git ls-files`` は cwd 相対で出力するため、path 形 rule と比較する root
    相対 path は ``<offset>/<path>`` で組み立てる。None のときは path 形 rule を
    評価しない (0.23.0 までと同じ挙動)。``git rev-parse --show-prefix`` を使わ
    ないのは、基準を git toplevel ではなく ``[project:]`` の key
    (``resolve_project_root``) に揃えるため — monorepo で両者が違うとき、Stop
    が出したレシピが Read / Edit / Bash で効かなくなる。
    """
    if not root or not cwd:
        return None
    if os.path.normpath(cwd) == os.path.normpath(root):
        return ""
    return root_relative(cwd, root)


def find_sensitive_files(
    cwd: str,
    rules: list[tuple[str, bool]],
    *,
    root: str | None = None,
) -> list[dict]:
    """git 管理下の tracked + untracked ファイルから機密パターン一致を抽出する。

    - tracked: 無条件で検査対象 (``.gitignore`` 済みでも block する)。
      Step 6 で submodule 内 tracked も検査対象に追加 (``--recurse-submodules``)。
    - untracked: ``git ls-files --others --exclude-standard`` を使うため
      ``.gitignore`` 済みは既に除外されている。submodule 内 untracked は範囲外。

    ``root`` (0.24.0): path 形 rule の基準 (``resolve_project_root(cwd)``)。
    ``cwd`` が root 配下なら各 path を **root 相対** (``root_offset`` + cwd 相対
    path) に組み立てて ``is_sensitive`` に渡す。したがって親 dir 名の評価
    (parts) も root 相対で行われ、サブディレクトリで発火したときも root で
    発火したときと同じ verdict になる (0.23.0 までは cwd 相対だったため、cwd と
    root の間のディレクトリ名は見ていなかった)。root 不明 / 配下でなければ
    従来どおり cwd 相対のまま評価する。戻り値の ``path`` は表示と stop-ack の
    ため **cwd 相対のまま**。

    Returns:
        ``[{"path": "relative/path", "status": "tracked" | "untracked"}, ...]``
    """
    if not rules:
        return []

    tracked = _ls_tracked(cwd)
    untracked = _run_git(["ls-files", "--others", "--exclude-standard"], cwd)

    offset = root_offset(cwd, root)
    match_root = root if offset is not None else None

    def _subject(filepath: str) -> str:
        return f"{offset}/{filepath}" if offset else filepath

    results: list[dict] = []

    for filepath in tracked:
        if is_sensitive(_subject(filepath), rules, root=match_root):
            results.append({"path": filepath, "status": "tracked"})

    for filepath in untracked:
        if is_sensitive(_subject(filepath), rules, root=match_root):
            results.append({"path": filepath, "status": "untracked"})

    return results
