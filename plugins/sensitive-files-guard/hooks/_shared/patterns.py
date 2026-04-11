"""patterns.txt / patterns.local.txt のロード — 両 hook 共通実装。

ログ戦略は呼出側で注入する (read 側は logfile 付き, Stop 側は stderr のみ)。
FileNotFoundError は黙って既定のみ返す。その他 OSError は ``warn_callback``
に委譲する。

_resolve_local_patterns_path:
- ``$XDG_CONFIG_HOME`` があれば ``$XDG_CONFIG_HOME/sensitive-files-guard/patterns.local.txt``
- 未設定なら ``~/.config/sensitive-files-guard/patterns.local.txt``
- 返り値は実在しなくてもよい (呼出側で FileNotFoundError を処理)
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Callable, Optional


def _resolve_local_patterns_path() -> Path:
    """ローカル patterns.local.txt のパスを解決する。"""
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "sensitive-files-guard" / "patterns.local.txt"


def _parse_patterns_text(text: str) -> list[tuple[str, bool]]:
    """patterns.txt 形式のテキストを rules list にパースする。

    - 空行・``#`` で始まる行は無視 (先頭空白 strip 後に判定)
    - ``!pattern`` → ``(pattern, True)`` (exclude)
    - それ以外 → ``(pattern, False)`` (include)
    - 出現順を保持する (last-match-wins で順序が意味を持つため)
    """
    rules: list[tuple[str, bool]] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("!"):
            rules.append((stripped[1:], True))
        else:
            rules.append((stripped, False))
    return rules


def load_patterns(
    patterns_file: Path,
    warn_callback: Optional[Callable[[str], None]] = None,
) -> list[tuple[str, bool]]:
    """既定 patterns.txt + ローカル patterns.local.txt を読んで rules list を返す。

    既定 → ローカルの順で連結 (last match wins なので末尾のローカルが強い)。
    ローカル非存在は無視、読み取り中の OSError は ``warn_callback`` に渡して
    既定のみ返す。

    Raises:
        FileNotFoundError: 既定 patterns.txt が存在しない
        OSError: 既定 patterns.txt の読み取りに失敗した
    """
    rules = _parse_patterns_text(patterns_file.read_text())

    local_path = _resolve_local_patterns_path()
    try:
        local_text = local_path.read_text()
    except FileNotFoundError:
        return rules
    except OSError as e:
        if warn_callback is not None:
            warn_callback(type(e).__name__)
        return rules

    rules.extend(_parse_patterns_text(local_text))
    return rules
