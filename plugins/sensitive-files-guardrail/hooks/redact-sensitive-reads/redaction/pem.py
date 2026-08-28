"""PEM / armored 鍵ファイルの minimal-info 化 (0.21.0)。

``keyonly_scan`` の ``_KEY_RE`` は ``^\\s*(?:export\\s+)?([A-Za-z_][\\w.\\-]*)\\s*[:=]``
で「鍵名 = 値」の行を拾うが、**PEM の最終行は base64 パディング ``=`` で終わる**ため
``KEY=`` 形式と誤判定され、base64 本体がそのまま「鍵名」として reason に出ていた。
``[\\w.\\-]`` は base64 の ``+`` ``/`` を含まないので、**最終行に ``+`` ``/`` が
無いときだけ**発火する確率的な漏れで、テストの fixture に PEM が 1 件も無かった
ため 941 件のテストを通過していた。

対処は 2 層:

1. **本モジュール** — ファイル全体が armored 鍵なら専用経路に分け、
   block 種別と件数だけを返す (base64 本体には一切触れない)
2. ``keyonly_scan`` / ``dotenv`` 側の妥当性ゲート — 鍵名候補が base64 断片の
   形をしていたら棄却する (``.env`` に PEM を値として埋めた形など、
   本モジュールが介入しない経路の多層防御)

判定境界は変えない。``.pem`` / ``.key`` / ``id_rsa*`` はいずれも従来どおり
機密パターンに一致して deny になり、変わるのは reason の**中身**だけ。
"""
from __future__ import annotations

import re

from .sanitize import sanitize_key

# ``-----BEGIN RSA PRIVATE KEY-----`` / ``-----BEGIN CERTIFICATE-----`` 等。
# label は空も許す (``-----BEGIN -----`` のような壊れた入力でも落ちないように)。
_BEGIN_RE = re.compile(r"^-{5}BEGIN ([A-Z0-9 ._-]*)-{5}\s*$", re.MULTILINE)
_END_RE = re.compile(r"^-{5}END ([A-Z0-9 ._-]*)-{5}\s*$", re.MULTILINE)

# 先頭付近に BEGIN があるかを見る窓。PEM は先頭にヘッダコメントを持つことが
# あるので、行数で緩く見る。
_SNIFF_LINES = 40

# 列挙する block の上限 (証明書バンドルは数百 block になりうる)。
_MAX_BLOCKS = 50


def looks_pem(text: str) -> bool:
    """テキストが armored 鍵 / 証明書ファイルらしいか判定する。

    先頭 ``_SNIFF_LINES`` 行以内に ``-----BEGIN ...-----`` があれば True。
    ``.env`` の値として PEM を埋めた形は対象外 (dotenv 経路のまま扱いたいので、
    呼出側が format 未確定 (opaque) のときだけ本関数を使う)。
    """
    if not isinstance(text, str) or not text:
        return False
    head = "\n".join(text.splitlines()[:_SNIFF_LINES])
    return _BEGIN_RE.search(head) is not None


def redact_pem(text: str) -> dict:
    """PEM テキストから block の種別と件数だけを抽出する。

    base64 本体には一切触れない。``armored_bytes`` はファイル全体の byte 数で、
    鍵の実バイト長ではない (鍵長の推定に使える情報は返さない)。
    """
    labels: list[str] = []
    for m in _BEGIN_RE.finditer(text):
        label = sanitize_key(m.group(1).strip() or "(unlabeled)")
        labels.append(label)
        if len(labels) >= _MAX_BLOCKS:
            break

    ordered_unique: list[str] = []
    seen: set[str] = set()
    for label in labels:
        if label not in seen:
            seen.add(label)
            ordered_unique.append(label)

    end_count = len(_END_RE.findall(text))
    return {
        "format": "pem",
        "blocks": len(labels),
        "block_types": ordered_unique,
        "end_markers": end_count,
        "armored_bytes": len(text.encode("utf-8", errors="replace")),
        "truncated_blocks": len(labels) >= _MAX_BLOCKS,
    }


def format_pem(info: dict) -> str:
    lines = [
        "format: pem (armored key / certificate)",
        f"blocks: {info['blocks']}",
    ]
    if info["block_types"]:
        lines.append("block types:")
        for i, label in enumerate(info["block_types"], 1):
            lines.append(f"  {i}. {label}")
    else:
        lines.append("(no BEGIN marker parsed)")
    if info["truncated_blocks"]:
        lines.append(f"note: only the first {_MAX_BLOCKS} blocks were counted.")
    if info["blocks"] != info["end_markers"]:
        lines.append(
            f"note: BEGIN markers ({info['blocks']}) and END markers "
            f"({info['end_markers']}) do not match; the file may be truncated."
        )
    lines.append(f"armored bytes: {info['armored_bytes']}")
    lines.append(
        "note: key material is never parsed or returned. "
        "only block labels and counts are shown."
    )
    return "\n".join(lines)


# --------------------------------------------------------------------------
# 多層防御: 鍵名候補が base64 断片かどうか
# --------------------------------------------------------------------------

# PEM 本文の行は 64 文字幅が標準で、大小英字 + 数字が混在する。環境変数名は
# 慣習的に ``_`` 区切りの大文字か、短い camelCase なので、
# 「長い / ``_`` を含まない / 大小混在 / 数字を含む」を全て満たすものは
# 実運用の鍵名としては現れないとみなして棄却する。
#
# 閾値を 24 にしているのは ``someVeryLongCamelCaseKey`` のような正当な名前を
# 巻き込まないため (数字を含む条件と併せて false negative を抑える)。
_MIN_B64_FRAGMENT_LEN = 24


def looks_base64_fragment(candidate: str) -> bool:
    """鍵名候補が base64 本体の断片に見えるか判定する (多層防御)。

    ``keyonly_scan`` / ``dotenv`` が ``KEY=`` として拾ってしまった行が、
    実際には PEM 本文の 1 行である場合を弾く。
    """
    if not isinstance(candidate, str):
        return False
    if len(candidate) < _MIN_B64_FRAGMENT_LEN:
        return False
    if "_" in candidate:
        return False
    if not candidate.isascii() or not candidate.isalnum():
        return False
    has_upper = any(c.isupper() for c in candidate)
    has_lower = any(c.islower() for c in candidate)
    has_digit = any(c.isdigit() for c in candidate)
    return has_upper and has_lower and has_digit
