"""patterns.txt / patterns.local.txt のロード — 両 hook 共通実装。

ログ戦略は呼出側で注入する (read 側は logfile 付き, Stop 側は stderr のみ)。
FileNotFoundError は黙って次の候補へ進む。その他 OSError は ``warn_callback``
に委譲する。

_resolve_local_patterns_paths:
- 優先 (new): ``~/.claude/sensitive-files-guard/patterns.local.txt`` (0.4.0+)
- fallback (deprecated): ``$XDG_CONFIG_HOME/sensitive-files-guard/patterns.local.txt``
  (または ``$XDG_CONFIG_HOME`` 未設定なら ``~/.config/sensitive-files-guard/patterns.local.txt``)
- 返り値は list[Path] (実在する/しないは呼出側で判定)。

fallback パスは後方互換のためサポートされているが、**0.6.0 で削除予定**。
fallback が採用された場合、``warn_callback("deprecated_config_dir")`` が呼ばれる。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Callable, Optional

_PREFERRED_SUBPATH = Path(".claude") / "sensitive-files-guard" / "patterns.local.txt"


def _resolve_local_patterns_paths() -> list[Path]:
    """ローカル patterns.local.txt の候補パスを優先度順で返す (list[Path])。

    - index 0: 優先パス (``~/.claude/sensitive-files-guard/patterns.local.txt``)
    - index 1: fallback (``$XDG_CONFIG_HOME/.../patterns.local.txt`` or
      ``~/.config/.../patterns.local.txt``)

    preferred と fallback が偶然同一パスに解決された場合は 1 要素リストを返す。
    """
    home = Path.home()
    preferred = home / _PREFERRED_SUBPATH

    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else home / ".config"
    fallback = base / "sensitive-files-guard" / "patterns.local.txt"

    if preferred == fallback:
        return [preferred]
    return [preferred, fallback]


def _resolve_local_patterns_path() -> Path:
    """Deprecated alias; 優先パスを返す。

    既存コードの後方互換のため残す。新規コードは ``_resolve_local_patterns_paths``
    を使うこと。
    """
    return _resolve_local_patterns_paths()[0]


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
    2-tier 解決: 優先パスが存在すればそれのみ。無ければ fallback を試し、
    採用時は ``warn_callback("deprecated_config_dir")`` を呼ぶ。
    ローカル非存在は無視、読み取り中の OSError (FileNotFound 以外) は
    ``warn_callback(err_name)`` に渡して既定のみ返す。

    Raises:
        FileNotFoundError: 既定 patterns.txt が存在しない
        OSError: 既定 patterns.txt の読み取りに失敗した
    """
    rules = _parse_patterns_text(patterns_file.read_text())

    local_paths = _resolve_local_patterns_paths()
    local_text: str | None = None
    used_fallback = False

    for i, path in enumerate(local_paths):
        try:
            local_text = path.read_text()
        except FileNotFoundError:
            continue
        except OSError as e:
            if warn_callback is not None:
                warn_callback(type(e).__name__)
            return rules
        used_fallback = i > 0
        break

    if local_text is None:
        return rules

    if used_fallback and warn_callback is not None:
        warn_callback("deprecated_config_dir")

    rules.extend(_parse_patterns_text(local_text))
    return rules
