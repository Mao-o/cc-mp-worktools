"""bytes → text のデコード層 (0.31.0、内部バックログ)。

0.30.0 までは全経路が ``raw.decode("utf-8", errors="replace")`` の一択だった。
これは **verdict (deny) は変えない**が、block 時に返す minimal info を
**事実と食い違わせる** (思想 2 の破綻):

- **BOM 付き UTF-8** (``utf-8-sig``): 先頭に U+FEFF が残る。Python の ``\\s`` は
  Cf カテゴリの U+FEFF に一致しないため、``dotenv`` / ``keyonly_scan`` /
  ``opaque`` の行頭 regex が**先頭 1 行だけ**不一致になり、先頭の鍵が黙って
  消える (entries も 1 件少なくなる)
- **UTF-16**: ほぼ全バイトが U+FFFD に置換され、1 件も鍵が拾えず
  ``entries: 0`` / ``(no entries)`` になる。**生きた鍵を含むファイルを「空」と
  モデルに伝える**

ここでは BOM と (BOM 無し UTF-16 の) NUL 配置からテキストエンコーディングを
推定し、判明したコーデックでデコードする。**判定境界には一切影響しない** —
deny/allow はパス名で決まっており、本モジュールは deny 済みの reason に載せる
情報の精度だけを上げる。

### 推定の順序と根拠

1. **BOM** (``codecs.BOM_*``) を先頭バイト列で見る。UTF-32 の BOM は UTF-16 の
   BOM を**接頭辞として含む** (``FF FE 00 00`` vs ``FF FE``) ので、UTF-32 を
   先に判定する (順序を逆にすると UTF-32 ファイルが UTF-16 として誤デコード
   される)
2. BOM が無ければ、先頭 ``_HEAD_BYTES`` byte の **NUL の位置 (パリティ)** を見る。
   UTF-16 で符号化された ASCII 文字は「片側のパリティだけが必ず NUL」になる
   (LE なら奇数 index、BE なら偶数 index)。偏りが閾値を超えたときだけ UTF-16 と
   見なす。密度ではなくパリティを見るのは、NUL が**両パリティに散る**バイナリ
   (ランダム / 圧縮データ) を巻き込まないため
3. パリティ条件を通っても、**NUL がパリティ整列しているバイナリ**は残る
   (16bit PCM / ASN.1 DER の ``02 02 00 xx`` 形 / ``(0x?? 0x00)*``)。
   そこで推定コーデックで先頭 ``_HEAD_BYTES`` byte を decode し、可読文字比率が
   ``_MIN_TEXT_RATIO`` 未満なら推定を棄却して UTF-8 に倒す (``_looks_like_text``)。
   **BOM がある場合はこの guard を掛けない** (明示的な宣言を内容で覆さない)
4. どちらでもなければ従来どおり UTF-8 (``errors="replace"``)

**BOM 無し UTF-32 は検出しない** (パリティ支配条件が偽になる)。誤検出ではなく
取りこぼし方向なので、UTF-8 + ``errors="replace"`` の取りこぼし note に落ちる。

### 閾値 (``_MIN_NUL_RATIO`` = 0.30) の根拠

チケットの根拠だけでは一意に決まらないので、**検出したい入力と誤検出したく
ない入力の両方**から決めた:

- ASCII のみの UTF-16 テキスト → 支配パリティの NUL 比率は **1.0**
- ``.env`` は鍵名・``=``・改行が ASCII なので、値が非 ASCII でも比率は高いまま
  (実測: 値を全部日本語にしても 0.5 超)
- UTF-8 / latin-1 のテキストは NUL を含まない → **0.0**。誤検出しない
- バイナリは NUL が両パリティに散るので ``dominant > 2 * other`` の条件で落ちる

0.30 は「支配パリティの符号単位の 30% 以上が U+0000〜U+00FF」を要求する値で、
上の実測レンジ (テキスト 0.5〜1.0 / 非 UTF-16 0.0) の中間より下側に置いた。

### 既知の適用範囲 (0.31.0 時点)

本モジュールを通るのは **32KB 以下の inline 経路** (``engine.redact`` /
``file_render``)。32KB 超の streaming 経路 (``engine.redact_large_file`` →
``keyonly_scan.scan_stream``) は chunk 単位の逐次デコードで、BOM / UTF-16 の
状態を chunk 間で持ち回す作りになっていないため**据え置き**。その経路で鍵が
1 件も拾えなかった場合は ``format_keyonly`` が「エンコーディングかもしれない」
旨を出す (誤情報を「不明」に落とす保険)。
"""
from __future__ import annotations

import codecs
from typing import NamedTuple

# BOM 無し UTF-16 判定で見る先頭バイト数。
_HEAD_BYTES = 512

# 支配パリティの NUL 比率の下限 (モジュール docstring の根拠を参照)。
_MIN_NUL_RATIO = 0.30

# BOM 無し UTF-16 と推定した後、その推定を受け入れるために必要な
# 「可読文字比率」の下限 (0.31.0 隔離内レビュー P2-4)。
#
# パリティ条件だけでは **NUL がパリティ整列しているバイナリ**を落とせない。
# 16bit PCM (低位バイトだけが動く) / ASN.1 DER の ``02 02 00 xx`` 形 TLV /
# ``(0x?? 0x00)*`` 形はいずれも片側パリティに NUL が偏るため、UTF-16 と
# 誤判定されていた。誤判定すると ``replaced`` が False になるので取りこぼし
# note も出ず、代わりに ``decoded as utf-16-be`` という**断定の偽情報**が出る。
#
# 閾値 0.90 は実測レンジの間に置いた (テキスト側の最小 0.970 =
# 制御文字混入 UTF-16 / バイナリ側の最大 0.871 = ASN.1 DER 風)。
_MIN_TEXT_RATIO = 0.90

# 置換文字。``errors="replace"`` が入れたものの検出に使う。
_REPLACEMENT = "�"

# (BOM, codec 名)。**UTF-32 を UTF-16 より先に置く** (BOM の接頭辞衝突)。
_BOMS: tuple[tuple[bytes, str], ...] = (
    (codecs.BOM_UTF32_LE, "utf-32"),
    (codecs.BOM_UTF32_BE, "utf-32"),
    (codecs.BOM_UTF8, "utf-8-sig"),
    (codecs.BOM_UTF16_LE, "utf-16"),
    (codecs.BOM_UTF16_BE, "utf-16"),
)


class Decoded(NamedTuple):
    """デコード結果。

    Attributes:
        text: デコード済みテキスト (常に ``errors="replace"``、例外は投げない)。
        encoding: 採用したコーデック名 (``utf-8`` / ``utf-8-sig`` / ``utf-16`` /
            ``utf-16-le`` / ``utf-16-be`` / ``utf-32``)。
        replaced: U+FFFD が含まれるか (= バイト列を取りこぼした可能性)。
            **元テキストが literal な U+FFFD を含む場合も True になる**ため、
            「かもしれない」以上の主張には使わない。
    """

    text: str
    encoding: str
    replaced: bool


def _bom_codec(raw: bytes) -> str | None:
    """先頭の BOM からコーデック名を返す (無ければ None)。"""
    for bom, name in _BOMS:
        if raw.startswith(bom):
            return name
    return None


def _looks_like_text(text: str) -> bool:
    """デコード結果が「テキストらしいか」を可読文字比率で判定する (0.31.0)。

    パリティ条件を通ったあとの 2 段目の guard。``str.isprintable()`` は
    改行・タブ・復帰を False にするので、それらは明示的に可読側へ数える
    (``.env`` / YAML は改行が必須、値に tab や CRLF が混じる形もある)。
    """
    if not text:
        return False
    ok = sum(1 for ch in text if ch.isprintable() or ch in "\n\r\t")
    return ok / len(text) >= _MIN_TEXT_RATIO


def _bom_less_utf16_codec(raw: bytes) -> str | None:
    """BOM 無し UTF-16 を NUL のパリティ偏りから推定する (無ければ None)。

    LE で符号化された ASCII は奇数 index が、BE では偶数 index が NUL になる。
    片側が ``_MIN_NUL_RATIO`` 以上を占め、かつ反対側の 2 倍より多いときだけ
    UTF-16 と見なす (NUL が両側に散るバイナリを除く)。

    **パリティ条件だけでは足りない**: NUL がパリティ整列しているバイナリ
    (16bit PCM / ASN.1 DER / ``(0x?? 0x00)*``) はここを通ってしまうため、
    呼出側 (``decode_text``) が ``_looks_like_text`` で 2 段目の guard を掛ける。
    """
    head = raw[:_HEAD_BYTES]
    pairs = len(head) // 2
    if pairs < 2:
        return None
    head = head[: pairs * 2]
    nul_even = head[0::2].count(0)
    nul_odd = head[1::2].count(0)
    threshold = _MIN_NUL_RATIO * pairs
    if nul_odd >= threshold and nul_odd > 2 * nul_even:
        return "utf-16-le"
    if nul_even >= threshold and nul_even > 2 * nul_odd:
        return "utf-16-be"
    return None


def decode_text(raw: bytes) -> Decoded:
    """バイト列をテキストにデコードする (例外を投げない)。

    BOM → BOM 無し UTF-16 の推定 → UTF-8 の順に試し、いずれも
    ``errors="replace"`` でデコードする。推定したコーデックでのデコードが
    ``LookupError`` / ``UnicodeError`` になった場合は UTF-8 に退避する
    (未知のコーデック名や壊れた入力で hook を落とさないため)。

    **呼出側が ``MAX_INLINE_BYTES`` で切ったバイト列を渡してよい**:
    途中で切れたマルチバイト列・サロゲートペアは ``errors="replace"`` が
    吸収する (UTF-16 の奇数長も同じ)。
    """
    if not raw:
        return Decoded("", "utf-8", False)

    codec = _bom_codec(raw)
    if codec is None:
        # BOM 無し推定は「テキストらしさ」を確認してから受け入れる (0.31.0)。
        # **BOM がある場合には掛けない** — 明示的な BOM は内容より強い証拠で、
        # 中身がどう見えても書き手の宣言を尊重する (証明書の DER を BOM 付きで
        # 保存するような形は存在しない)。
        guess = _bom_less_utf16_codec(raw)
        if guess is not None and _looks_like_text(
            raw[:_HEAD_BYTES].decode(guess, errors="replace")
        ):
            codec = guess
    if codec is None:
        codec = "utf-8"
    try:
        text = raw.decode(codec, errors="replace")
    except (LookupError, UnicodeError):
        codec = "utf-8"
        text = raw.decode("utf-8", errors="replace")
    return Decoded(text, codec, _REPLACEMENT in text)


def decode_note(decoded: Decoded) -> str | None:
    """``reason`` に添える 1 行の注記 (不要なら None)。

    - BOM 付き UTF-8 (``utf-8-sig``) → **「非 UTF-8」とは書かない** (0.31.0
      隔離内レビュー P3-1)。実体は UTF-8 + BOM なので、コーデック名をそのまま
      出すと事実に反する説明になる
    - それ以外の UTF-8 以外で読めた → そのコーデック名を出す (モデルが
      「なぜ見慣れない形なのか」を判断できる)
    - UTF-8 だがバイトを取りこぼした → 鍵名・長さが不正確かもしれない旨

    reason の byte 予算を食うので **1 行・短文**に留める。
    """
    if decoded.encoding == "utf-8-sig":
        return "note: decoded as utf-8 with BOM."
    if decoded.encoding != "utf-8":
        return f"note: decoded as {decoded.encoding} (non-UTF-8 text encoding)."
    if decoded.replaced:
        return (
            "note: some bytes are not valid UTF-8 (replaced);"
            " key names and lengths may be inexact."
        )
    return None
