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


_warn_local_oserror = _warn_local  # 後方互換 alias


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


def _run_git(args: list[str], cwd: str) -> list[str]:
    """git コマンドを実行してファイル一覧を返す。失敗時は空リスト。"""
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return []
        return [line for line in result.stdout.splitlines() if line.strip()]
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return []


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
