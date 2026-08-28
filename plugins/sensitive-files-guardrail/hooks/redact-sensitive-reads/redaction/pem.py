"""PEM / armored 鍵ファイルの minimal-info 化 (0.23.0)。

``keyonly_scan`` の ``_KEY_RE`` は ``^\\s*(?:export\\s+)?([A-Za-z_][\\w.\\-]*)\\s*[:=]``
で「鍵名 = 値」の行を拾うが、**PEM の最終行は base64 パディング ``=`` で終わる**ため
``KEY=`` 形式と誤判定され、base64 本体がそのまま「鍵名」として reason に出ていた。
``[\\w.\\-]`` は base64 の ``+`` ``/`` を含まないので、**最終行に ``+`` ``/`` が
無いときだけ**発火する確率的な漏れで、テストの fixture に PEM が 1 件も無かった
ため 941 件のテストを通過していた。

対処は 2 層とも **parser context** で行う:

1. **本モジュール** — ファイル全体が armored 鍵なら専用経路に分け、
   block 種別と件数だけを返す (base64 本体には一切触れない)
2. ``keyonly_scan`` / ``dotenv`` — ``PEM_BEGIN_MARKER`` / ``PEM_END_MARKER`` で
   ブロック状態を追跡し、block 内の行を丸ごと捨てる (``.env`` に PEM を値として
   埋めた形など、本モジュールが介入しない経路)

初版は「候補文字列が base64 断片に見えるか」というヒューリスティックで弾いて
いたが、(a) RSA / EC PKCS#8 の短い末尾行が閾値を素通りする
(b) ``oauth2ClientSecretProduction`` のような正当な鍵名を巻き込んで黙って
落とす、の両方向に外れるため撤去した。ブロック状態の追跡は位置にも綴りにも
依存しない。

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

# 他モジュール (dotenv) が block 内かどうかを追跡するために使う軽量 marker。
# ``_BEGIN_RE`` / ``_END_RE`` は行全体一致 (MULTILINE + 行末まで) を要求するが、
# ``.env`` の値に埋め込まれた形 (``KEY=-----BEGIN X-----``) では行頭に来ないため、
# 部分一致で拾える版を別に持つ。
PEM_BEGIN_MARKER = re.compile(r"-{5}BEGIN [A-Z0-9 ._-]*-{5}")
PEM_END_MARKER = re.compile(r"-{5}END [A-Z0-9 ._-]*-{5}")

# インラインコメント (`` #`` 以降) を落とすための regex。dotenv の
# ``_preprocess_value`` と同じ形。
_INLINE_COMMENT_RE = re.compile(r"\s+#")


def _without_inline_comment(text: str) -> str:
    """行末コメントを落とす。marker 判定を**コメント除去後**に行うため。

    ``A=one # -----BEGIN PRIVATE KEY-----`` のように例示として書いただけの
    marker で block が開くと、END が現れるまで以降のキーが丸ごと報告から消える。
    """
    if not isinstance(text, str):
        return ""
    m = _INLINE_COMMENT_RE.search(text)
    return text[: m.start()] if m else text


def opens_pem_block(text: str) -> bool:
    """この行で armored block が開くか (コメント除去後で判定)。

    ``closes_pem_block`` と対で使う。**dotenv / keyonly_scan の両方がこの 2 つを
    呼ぶ**こと — 片方だけがコメント除去を実装すると挙動が割れる (実際に割れた)。
    """
    return PEM_BEGIN_MARKER.search(_without_inline_comment(text)) is not None


def closes_pem_block(text: str) -> bool:
    """この行で armored block が閉じるか (コメント除去後で判定)。"""
    return PEM_END_MARKER.search(_without_inline_comment(text)) is not None


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
    # 件数は**全件**数える。``_MAX_BLOCKS`` は列挙する label の preview 上限で
    # あって block 数の上限ではない (上限で break すると 60 block の bundle が
    # 「50 block」と報告され、END との差から誤った不一致 note まで出る)。
    labels: list[str] = []
    for m in _BEGIN_RE.finditer(text):
        if len(labels) < _MAX_BLOCKS:
            labels.append(sanitize_key(m.group(1).strip() or "(unlabeled)"))
        else:
            labels.append("")  # 上限超過分は件数だけ数える

    ordered_unique: list[str] = []
    seen: set[str] = set()
    for label in labels:
        if label and label not in seen:
            seen.add(label)
            ordered_unique.append(label)

    end_count = len(_END_RE.findall(text))
    return {
        "format": "pem",
        "blocks": len(labels),
        "block_types": ordered_unique,
        "end_markers": end_count,
        "armored_bytes": len(text.encode("utf-8", errors="replace")),
        # label が実際に省かれたときだけ立てる。ちょうど ``_MAX_BLOCKS`` 件なら
        # 全件列挙できているので「最初の N 件のみ」とは言わない。
        "truncated_blocks": len(labels) > _MAX_BLOCKS,
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
        lines.append(
            f"note: only the first {_MAX_BLOCKS} block labels are listed "
            "(the count above covers all blocks scanned)."
        )
    if info.get("truncated_scan"):
        lines.append(
            "note: the file is larger than the scan limit; "
            "counts cover the scanned portion only."
        )
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
# 32KB 超の bundle 用: ストリーム全体の marker を数える
# --------------------------------------------------------------------------

# chunk 境界で marker が分断されないよう保持する重なり幅。
# ``-----BEGIN CERTIFICATE REQUEST-----`` 級でも 40 文字未満なので余裕を取る。
_MARKER_OVERLAP = 128

_STREAM_CHUNK = 64 * 1024


def scan_pem_markers(f, max_bytes: int = 1024 * 1024) -> dict:
    """file-like をストリームで走査し BEGIN / END marker を数える。

    ``redact_pem`` は全文を str で受けるが、32KB 超の bundle を全文展開せずに
    **正確な block 数**を出すための streaming 版。先頭 8KB だけを見て
    その件数を「完全な blocks 数」として提示すると、20 block の証明書バンドルが
    「5 block」と報告される (Codex P2)。

    ``max_bytes`` を超えた分は走査せず ``truncated_scan`` を立てる
    (件数を「完全」と偽らないため)。呼出側は seek 位置を先頭に戻しておくこと。
    close はしない。
    """
    labels: list[str] = []
    end_count = 0
    read_bytes = 0
    carry = ""
    truncated = False

    while read_bytes < max_bytes:
        chunk = f.read(min(_STREAM_CHUNK, max_bytes - read_bytes))
        if not chunk:
            break
        read_bytes += len(chunk)
        carry_len = len(carry)
        text = carry + chunk.decode("utf-8", errors="replace")
        # carry 内で**完結している** marker は前回すでに数えている。
        # 二重計上を避けるため、carry の末尾を越えて終わる match だけを採る
        # (境界を跨いだ marker は前回は不完全でマッチせず、今回初めて完成する)。
        for m in PEM_BEGIN_MARKER.finditer(text):
            if m.end() <= carry_len:
                continue
            if len(labels) < _MAX_BLOCKS:
                inner = m.group(0)[len("-----BEGIN "):-len("-----")]
                labels.append(sanitize_key(inner.strip() or "(unlabeled)"))
            else:
                labels.append("")  # 上限超過分は件数だけ数える
        end_count += sum(
            1 for m in PEM_END_MARKER.finditer(text) if m.end() > carry_len
        )
        carry = text[-_MARKER_OVERLAP:] if len(text) > _MARKER_OVERLAP else text
    else:
        # max_bytes に到達してループを抜けた = まだ続きがあるかもしれない
        truncated = bool(f.read(1))

    named = [x for x in labels if x]
    ordered_unique: list[str] = []
    seen: set[str] = set()
    for label in named:
        if label not in seen:
            seen.add(label)
            ordered_unique.append(label)

    return {
        "format": "pem",
        "blocks": len(labels),
        "block_types": ordered_unique,
        "end_markers": end_count,
        "armored_bytes": read_bytes,
        # redact_pem と同じ境界 (label が実際に省かれたときだけ)
        "truncated_blocks": len(labels) > _MAX_BLOCKS,
        "truncated_scan": truncated,
    }
