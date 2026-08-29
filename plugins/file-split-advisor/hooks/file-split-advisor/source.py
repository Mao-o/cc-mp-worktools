#!/usr/bin/env python3
"""I/O 境界: パス解決・早期 skip 判定・安全なファイル読み込み。

language.py / metrics.py / judge.py を純粋関数のまま保つため、ファイルシステム
アクセスをこのモジュールに閉じ込める。
"""
from __future__ import annotations

import fnmatch
import os
import stat
from dataclasses import dataclass
from pathlib import Path

_LOCKFILE_NAMES = frozenset(
    {
        "package-lock.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "Cargo.lock",
        "Pipfile.lock",
        "poetry.lock",
        "go.sum",
        "composer.lock",
    }
)

_MINIFIED_SUFFIXES = (".min.js", ".min.css", ".map")

_GENERATED_NAME_PATTERNS = (
    "*.pb.go",
    "*_pb2.py",
    "*_pb2_grpc.py",
    "*.g.dart",
    "*.freezed.dart",
    "*_generated.*",
)


@dataclass(frozen=True)
class LoadedFile:
    text: str
    lines: list[str]


def resolve_path(file_path: str, cwd: str) -> Path:
    """相対パスは cwd と結合、絶対パスはそのまま返す。

    ``Path.resolve()`` は使わない: symlink を先に正規化すると
    ``load_text()`` の ``lstat`` ベース symlink 判定が意味を失う。
    """
    path = Path(file_path)
    if path.is_absolute():
        return path
    return Path(cwd) / path if cwd else path


def _realpath(path: Path) -> Path:
    """containment 判定専用の realpath 正規化 (存在しないパスでも動作する)。

    macOS では ``/tmp`` / ``/var`` が ``/private/tmp`` / ``/private/var`` への
    symlink であり (``ls -ld /tmp /var`` で確認できる)、正規化しないと
    containment 判定が表記揺れで壊れる:

    - path が resolved 形 (``/private/var/folders/...``) で渡るのに roots の
      素の列挙 (``/var/folders``) にしか一致しない (一時ファイルが skip
      されない方向)
    - ``cwd`` と ``path`` で表記が割れ (例: cwd は resolved、path は
      unresolved)、本来 ``cwd`` の内側にあるファイルが「``cwd`` の外の別の
      一時ディレクトリ」と誤判定されて skip される方向

    in-repo 先例:
    ``external-ai-assist/hooks/post-implementation-review/gitscan.py::worktree_root``
    が同じ罠を realpath で解決している。

    ``resolve_path()`` の「symlink を正規化しない」方針 (``load_text`` の
    ``lstat`` ベース symlink 判定を守るため) はここでは変更しない —
    containment 判定用にここだけ別途正規化した値を使う。
    """
    return Path(os.path.realpath(str(path)))


def _temp_dir_roots() -> tuple[Path, ...]:
    """一時ディレクトリとして扱う既知のルート (realpath 正規化済み)。

    ``$TMPDIR`` に加え、環境によらず存在しうる固定パスも列挙する
    (``$TMPDIR`` が未設定/別プロセスの値のまま渡ってくる場合の保険)。
    """
    roots = [Path("/tmp"), Path("/private/tmp"), Path("/var/folders")]
    env_tmpdir = os.environ.get("TMPDIR", "").strip()
    if env_tmpdir:
        env_path = Path(env_tmpdir)
        if env_path not in roots:
            roots.append(env_path)
    normalized: list[Path] = []
    for root in roots:
        normalized_root = _realpath(root)
        if normalized_root not in normalized:
            normalized.append(normalized_root)
    return tuple(normalized)


def is_under_temp_dir(path: Path) -> bool:
    """``path`` が ``$TMPDIR`` / ``/tmp`` / ``/private/tmp`` / ``/var/folders`` 配下か。

    containment 判定は realpath 正規化した上で行う (macOS の ``/tmp``/``/var``
    symlink による表記揺れ対策)。
    """
    normalized_path = _realpath(path)
    for root in _temp_dir_roots():
        try:
            if normalized_path.is_relative_to(root):
                return True
        except (TypeError, ValueError):
            continue
    return False


def should_skip_temp_dir(path: Path, cwd: str) -> bool:
    """一時領域配下のファイルを、``cwd`` に含まれない限り常時 skip する (opt-out 機構なし)。

    Claude が一時スクリプト・分析用ダンプ・handoff メモを scratchpad
    (``$TMPDIR`` 配下) に書く運用があり、プロジェクト外のこれらのファイルに
    まで分割助言を出すのは有用でない。

    ``path`` が ``cwd`` 配下にあるとき (session 全体がその場限りの一時
    プロジェクトである場合を含む) は skip しない — この場合 ``path`` は
    announce された cwd の一部であり、単に「一時領域にある」というだけで
    対象外にすると、本来判定したいファイルまで黙って落としてしまう。

    逆に ``cwd`` 自身が一時領域配下でも、``path`` が cwd の**外**にある別の
    一時ディレクトリ (兄弟プロジェクト等) のときは skip する。
    (``FILE_SPLIT_ADVISOR_CWD_ONLY`` を待たず、一時領域同士でも対象外にする)。
    """
    if not is_under_temp_dir(path):
        return False
    if cwd:
        try:
            if _realpath(path).is_relative_to(_realpath(Path(cwd))):
                return False
        except (TypeError, ValueError):
            pass
    return True


def is_outside_cwd(path: Path, cwd: str) -> bool:
    """``FILE_SPLIT_ADVISOR_CWD_ONLY=1`` 用: ``path`` が ``cwd`` 配下でないか。

    既定 off の opt-in 専用。既定で有効にすると ``--add-dir`` で cwd 外の
    ディレクトリを正当に編集する運用を壊すため、呼び出し側で env var 判定して
    から使うことを想定する。
    """
    if not cwd:
        return False
    try:
        return not _realpath(path).is_relative_to(_realpath(Path(cwd)))
    except (TypeError, ValueError):
        return False


def _default_ignore_file() -> Path:
    """ユーザーの永続 ignore 設定の既定パス。

    ``sensitive-files-guardrail`` の ``patterns.local.txt`` 慣例に倣い、
    plugin 名を切った独自ディレクトリに置く。
    """
    return Path.home() / ".claude" / "file-split-advisor" / "ignore.local.txt"


def _parse_ignore_globs(text: str) -> tuple[str, ...]:
    """gitignore 風の簡易パーサ: 1 行 1 glob、``#`` 始まりはコメント、空行は無視。

    否定 (``!``) やディレクトリ限定の末尾 ``/`` 等、完全な .gitignore 構文は
    実装しない (fnmatch ベースの素朴な glob のみ)。
    """
    patterns = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        patterns.append(stripped)
    return tuple(patterns)


def load_ignore_globs(env_value: str, ignore_file: Path | None = None) -> tuple[str, ...]:
    """``FILE_SPLIT_ADVISOR_IGNORE`` (カンマ区切り) と ``ignore.local.txt`` を統合する。

    ファイルが存在しない/読めない場合は黙って無視する (fail-open)。
    """
    patterns: list[str] = []
    for part in env_value.split(","):
        stripped = part.strip()
        if stripped:
            patterns.append(stripped)

    path = ignore_file if ignore_file is not None else _default_ignore_file()
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        # UnicodeDecodeError は OSError のサブクラスではないため、
        # ``except OSError`` だけでは捕まらず main() まで伝播していた
        # (P2-2)。ignore.local.txt は ~/.claude/ 配下のユーザーグローバル
        # 設定であり、これが非UTF-8だと全プロジェクトで plugin が無言停止する
        # (fail-open の約束が破れる)。in-repo 先例:
        # sensitive-files-guardrail/hooks/check-sensitive-files/stop_ack.py
        # の ``_looks_like_state_file`` も同じ組み合わせを使っている。
        text = ""
    patterns.extend(_parse_ignore_globs(text))
    return tuple(patterns)


def matches_ignore_glob(path: Path, patterns: tuple[str, ...]) -> bool:
    """``path`` がいずれかの ignore glob (fnmatch) に一致するか。

    ファイル名のみの glob (``test_*.py`` 等) とフルパスの glob
    (``*/migrations/*`` 等) の両方を許すため、``path.name`` と
    ``path.as_posix()`` の両方に対して判定する。
    """
    if not patterns:
        return False
    full = path.as_posix()
    name = path.name
    for pattern in patterns:
        if fnmatch.fnmatchcase(name, pattern) or fnmatch.fnmatchcase(full, pattern):
            return True
    return False


def should_skip_by_name(path: Path) -> bool:
    """lockfile / minified / generated-path パターンに一致するか (内容を見ない早期 skip)。"""
    name = path.name
    if name in _LOCKFILE_NAMES:
        return True
    if any(name.endswith(suffix) for suffix in _MINIFIED_SUFFIXES):
        return True
    if any(fnmatch.fnmatchcase(name, pattern) for pattern in _GENERATED_NAME_PATTERNS):
        return True
    return False


def load_text(
    path: Path,
    max_bytes: int = 2_000_000,
    max_lines: int = 20_000,
) -> LoadedFile | None:
    """安全弁付きでファイルを読み込む。対象外/失敗時は None (呼び出し側は skip)。

    - symlink / FIFO 等の非通常ファイルは ``lstat`` で検出して None
    - ``max_bytes`` 超のファイルは読まずに None
    - 読込後の行数が ``max_lines`` 超なら None
    """
    try:
        st = path.lstat()
    except OSError:
        return None
    if not stat.S_ISREG(st.st_mode):
        return None
    if st.st_size > max_bytes:
        return None
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None
    lines = text.splitlines()
    if len(lines) > max_lines:
        return None
    return LoadedFile(text=text, lines=lines)
