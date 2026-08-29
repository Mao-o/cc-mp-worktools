"""大ファイル用の streaming 鍵名抽出。

``^(\\w[\\w.-]*)\\s*[:=]`` にマッチする行から鍵名のみを抽出する。
32KB 超のファイルや構造不明ファイルで使用。値には一切触れない。
"""
from __future__ import annotations

import io
import re
from pathlib import Path
from typing import IO

from .pem import closes_pem_block, opens_pem_block
from .sanitize import sanitize_key

_KEY_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][\w.\-]*)\s*[:=]")

# 最大抽出鍵数 (reason コンテキスト圧迫防止)
MAX_KEYS = 500

# streaming 読み込みの 1 回あたりの chunk サイズ。
_CHUNK_BYTES = 64 * 1024

# 1 行のうち鍵名判定に使う先頭 byte 数の上限。``_KEY_RE`` は ``^`` 固定で
# 鍵名は行頭にあり、``sanitize_key`` の上限も 128 文字なので、これを超えた
# 位置を見る必要はない。レコード長に依存しないメモリ使用量を保証するための
# 頭打ち (改行を含まない巨大レコード対策)。
_MAX_LINE_PREFIX = 512


def scan_keys(text: str) -> list[str]:
    """テキスト全体から鍵名を抽出する (重複は順序を保って一意化)。"""
    seen: set[str] = set()
    ordered: list[str] = []
    # PEM 本文の行は末尾のパディング ``=`` が ``KEY=`` に見えるため、
    # ``-----BEGIN`` / ``-----END`` のブロック状態を追跡して本文行を捨てる
    # (詳細は redaction/pem.py)。候補文字列の形で弾くヒューリスティックは
    # ``oauth2ClientSecretProduction`` のような正当な鍵名まで巻き込むため使わない。
    in_pem_block = False
    for line in text.splitlines():
        if in_pem_block:
            if closes_pem_block(line):
                in_pem_block = False
            continue
        if opens_pem_block(line):
            in_pem_block = not closes_pem_block(line)
        m = _KEY_RE.match(line)
        if not m:
            continue
        key = sanitize_key(m.group(1))
        if key in seen:
            continue
        seen.add(key)
        ordered.append(key)
        if len(ordered) >= MAX_KEYS:
            break
    return ordered


def scan_stream(f: IO[bytes], max_bytes: int = 1024 * 1024) -> tuple[list[str], int]:
    """file-like オブジェクトを streaming で読み鍵名を抽出する。

    呼出側は seek 位置を適切に戻しておく (先頭から読みたければ f.seek(0))。
    close はしない (呼出側責務)。

    ### ``max_bytes`` は **実効的な上限** (0.20.0 で修正)

    0.19.1 までは ``f.readline()`` で 1 行ずつ読み、``read_bytes < max_bytes``
    を **行の切れ目でしか** 見ていなかった。``readline()`` は改行が来るまで
    読み続けるため、**改行を含まない巨大なレコード 1 本で上限を突破する** —
    8MB の 1 行ファイルで実測 8MB 読み込み / peak 16.1MB (公称上限 1MB)。
    hook は 2 秒 timeout で、outer timeout の挙動は fail-open の可能性がある
    (``__main__._is_unsupported_platform`` の注記) ため、**情報提供のための
    描画がガードレール自体を落としうる**状態だった。

    現在は固定長 chunk で読み、``max_bytes`` を 1 byte も超えない。行の判定に
    必要なのは **行頭だけ** (``_KEY_RE`` は ``^`` 固定) なので、行バッファは
    ``_MAX_LINE_PREFIX`` byte で頭打ちにし、超過分は捨てる。したがって
    メモリ使用量は **レコード長に依存しない** (chunk + prefix のみ)。

    行頭から ``_MAX_LINE_PREFIX`` byte を超えた位置にある鍵名は拾えなくなるが、
    ``sanitize_key`` の上限が 128 文字であることを踏まえれば通常入力では届かない
    (敵対的入力は非目的)。

    Returns:
        (keys, total_bytes_read) — ``total_bytes_read <= max_bytes`` が常に成立。
    """
    keys_seen: set[str] = set()
    ordered: list[str] = []
    read_bytes = 0
    pending = bytearray()
    # scan_keys と同じ PEM ブロック追跡 (list で包むのは closure から書き換えるため)
    in_pem_block = [False]

    def _consume(raw: bytes) -> bool:
        """1 行分の先頭バイト列から鍵名を拾う。MAX_KEYS 到達で True。"""
        try:
            line = raw.decode("utf-8", errors="replace")
        except Exception:
            return False
        if in_pem_block[0]:
            if closes_pem_block(line):
                in_pem_block[0] = False
            return False
        if opens_pem_block(line):
            in_pem_block[0] = not closes_pem_block(line)
        m = _KEY_RE.match(line)
        if not m:
            return False
        key = sanitize_key(m.group(1))
        if key in keys_seen:
            return False
        keys_seen.add(key)
        ordered.append(key)
        return len(ordered) >= MAX_KEYS

    try:
        while read_bytes < max_bytes:
            chunk = f.read(min(_CHUNK_BYTES, max_bytes - read_bytes))
            if not chunk:
                break
            read_bytes += len(chunk)
            start = 0
            done = False
            while True:
                nl = chunk.find(b"\n", start)
                seg_end = len(chunk) if nl < 0 else nl
                room = _MAX_LINE_PREFIX - len(pending)
                if room > 0:
                    # 行頭 prefix 分だけを slice する (chunk 末尾までを一度
                    # コピーしない — 長い行で無駄な 64KB コピーが出るため)。
                    pending += chunk[start:min(seg_end, start + room)]
                if nl < 0:
                    break
                done = _consume(bytes(pending))
                pending.clear()
                start = nl + 1
                if done:
                    break
            if done:
                break
        # 最後に残った行頭バッファを評価する。これは
        # (a) 改行で終わらないファイルの最終行、または
        # (b) ``max_bytes`` に達して途中で切れた行の**先頭**
        # のどちらかで、いずれも **行頭から始まっている** (改行ごとに clear する
        # ため)。``_KEY_RE`` は行頭しか見ないので、行が未完結でも鍵名の判定は
        # 成立する — 0.19.1 の ``readline()`` 版が「巨大な 1 行の先頭にある鍵名」
        # を拾えていた挙動を、全体を読まずに維持するための処理。
        if pending:
            _consume(bytes(pending))
    except OSError:
        pass
    return ordered, read_bytes


def scan_file(path: Path, max_bytes: int = 1024 * 1024) -> tuple[list[str], int]:
    """パスを開いて scan_stream を呼ぶ簡易ラッパ (テスト互換用)。

    本体ロジックは ``scan_stream`` に置き、``engine.redact_large_file`` からは
    fd 経由で呼ばれる。テストや他の呼出からは path 経由で呼んで良い。
    """
    try:
        with path.open("rb") as f:
            return scan_stream(f, max_bytes=max_bytes)
    except OSError:
        return [], 0


# ``format:`` 行に必ず入る keys-only scan の marker。``core.messages`` は
# 折り畳み時の省略単位を「行」ではなく「鍵」と表示するためにこの文字列を
# sniff する (``core`` → ``redaction`` の依存を作らないよう定数の実体は
# core 側にも置き、``tests/test_messages.py`` の assumption test で突合する)。
KEYONLY_SCAN_MARKER = "keys-only scan"

# reason に載せる鍵名の上限 (LLM コンテキスト圧迫防止)。``MAX_KEYS`` は
# スキャンそのものの上限で、こちらは表示側の上限。
PREVIEW_CAP = 60

# keys-only scan に降りた理由 → ``format:`` 行に載せるラベル (0.26.0)。
# 旧実装は理由を問わず一律 "large" と表示しており、43 byte の壊れた JSON でも
# 「(large, keys-only scan)」と出て小ファイルなのに「大きすぎる」と誤った事実を
# 伝えていた (バグ報告そのものが LLM に伝わらなくなる)。``format_opaque`` 経由で
# 呼ばれる際に理由を引き渡し、事実どおりのラベルに分岐する。
_KEYONLY_REASON_LABELS: dict[str, str] = {
    "large": "large",
    "parse_failed": "parse failed",
    "toml_unsupported": "unsupported",
}


def _keyonly_note(reason: str, fmt_hint: str) -> str:
    """理由別の ``note:`` 行本文を返す (常に「値は読んでいない」で終える)。"""
    if reason == "parse_failed":
        return (
            f"note: {fmt_hint.upper()} parse failed — file may be malformed."
            " values never read."
        )
    if reason == "toml_unsupported":
        return (
            "note: TOML structure needs Python 3.11+ (tomllib unavailable)."
            " values never read."
        )
    return "note: file too large for full parse. values never read."


def format_keyonly(
    keys: list[str],
    total_bytes: int,
    fmt_hint: str = "unknown",
    reason: str = "large",
) -> str:
    """streaming 鍵名抽出の結果を reason 用に整形する。

    Args:
        keys: 抽出済み鍵名リスト。
        total_bytes: スキャンした byte 数。
        fmt_hint: ``format:`` 行に出す format 名。
        reason: keys-only scan に降りた理由 (``large`` / ``parse_failed`` /
            ``toml_unsupported``)。未知値は ``large`` 相当 (旧来の表示) に
            フォールバックする。``format:`` 行のラベルと末尾 ``note:`` の
            文面の両方を切り替える。

    ### 鍵名は **1 鍵 1 行** で出す (0.26.0 隔離内レビュー P1-1)

    0.26.0 の初版までは全鍵名を ``keys: A, B, C, ...`` の **1 行**に並べて
    いた。``core.messages._fit_data_block_core`` は **行単位でしか畳めない**
    ため、この 1 行が残予算に入らないと行ごと落ち、予算が 1KB 以上余って
    いるのに鍵名が 0 個という状態になっていた (>32KB ファイルと json/toml の
    parse 失敗が該当。長い鍵名 200 個の ``.env`` を Read すると 3,072 byte 中
    2,753 byte を使い残して鍵名 0 個)。0.25.0 は盲目 byte cut だったので
    「途中で切れた 1 行」として数十個は見えており、**折り畳みの導入で情報が
    減る**退行になっていた。

    行の粒度を内容の粒度 (= 1 鍵) に揃えることで、既存の折り畳み機構が
    そのまま効く。盲目 cut にフォールバックする経路でも、鍵名が早い行に
    分かれて並ぶため「0 個」ではなく「途中まで」になる。

    preview 上限の告知は末尾行ではなく **header 行**に置く。末尾行は畳んだ
    ときに真っ先に落ちるうえ、omit marker の件数と二重になって「いくつ
    落ちたか」が読めなくなるため。総数は ``entries:`` が持つ。

    ### header は「上限」として、かつ **短く** 書く (0.26.0 外部レビュー R1)

    文言は ``max N shown`` — **``first N shown`` と書いてはいけない**。
    この header 行は preview 上限を告げるが、そのあとに
    ``core.messages._fit_data_block`` が予算で **さらに** 畳むため、実際に
    残る鍵行は N 未満になりうる (500 鍵で 60 行の preview のうち 23 行しか
    残らない実測例がある)。断定形だと折り畳み後に header だけが「60 個
    見せている」と嘘をつく。上限表現なら畳まれても真のまま。
    実際に何個隠れているかは省略マーカーが ``entries:`` 基準で言う
    (``core.messages._omit_marker`` / ``_entries_total``)。

    ``up to N shown`` (旧 ``first N shown`` と同じ byte 数) ではなく
    **2 byte 短い ``max``** を選んでいるのは、同じ R1 対応で省略マーカーの
    件数が「落とした行数」から「隠れている鍵数」に変わり、桁が増えて 1〜2
    byte 太るため。この 2 byte を header 側で返さないと、きつい予算では
    **鍵行が 1 行落ちる** (コーパス 1,623 件で 6 件が退行、``max`` なら 0 件で
    3 件は逆に増える)。文言を長くするときは同じ計測をやり直すこと。
    """
    label = _KEYONLY_REASON_LABELS.get(reason, "large")
    lines = [
        f"format: {fmt_hint} ({label}, {KEYONLY_SCAN_MARKER})",
        f"entries: {len(keys)}",
        f"scanned_bytes: {total_bytes}",
    ]
    if not keys:
        lines.append("(no keys matched)")
    else:
        shown = keys[:PREVIEW_CAP]
        if len(keys) > PREVIEW_CAP:
            lines.append(f"keys (in order, max {len(shown)} shown):")
        else:
            lines.append("keys (in order):")
        lines.extend(f"  {i}. {k}" for i, k in enumerate(shown, 1))
    lines.append(_keyonly_note(reason, fmt_hint))
    return "\n".join(lines)
