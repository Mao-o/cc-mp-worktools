"""``.npmrc`` の内容ゲート — 両 hook 共通実装 (0.34.0)。

## なぜ内容で判定するか

``.npmrc`` は pnpm / yarn / npm を使う repo でほぼ必ず commit される**設定
ファイル**で (``engine-strict`` / ``auto-install-peers`` / ``@scope:registry``
など)、認証トークンを含むのは一部にすぎない。それでも既定 patterns に
``.npmrc`` が入っているため、該当 repo では Read / Bash の deny に加えて
Stop hook が毎ターン block していた。0.14.0 で撤去した ``*.local.json``
(非機密の設定を機密名で誤検出) と同じ構図。

一方で「名前を patterns から外す」と、**本当にトークンが書かれている
``.npmrc`` まで素通り**する。Read と Stop は**ファイルを開いて中身を見る**
経路なので、名前は候補に残したまま**内容で確定**できる:

- 認証らしい行が 1 行も無い → 機密ではない (allow / 報告しない)
- 認証らしい行がある → 従来どおり deny / 報告
- 読めない / decode できない / 上限超 → **deny 側** (fail-closed)

``Bash`` / ``Edit`` / ``Write`` handler は**この判定を使わない**。Bash の
operand は「path とは限らない文字列」で、``cat .npmrc`` の operand ごとに
ファイルを開くのは設計変更にあたるため (``docs/MATRIX.md`` の Bash 表は不変)。

## 「認証らしい行」の定義

行頭の空白を除き、``#`` / ``;`` で始まるコメント行を捨てたうえで、**キー部**
(最初の ``=`` の左。``=`` が無ければ行全体) と**値部**を見る。次のいずれかに
当たれば認証行と見なす:

1. キー部が ``//`` で始まる — registry 単位の設定
   (``//registry.npmjs.org/:_authToken=…``)
2. キー部が認証系の npm 設定キーに**完全一致**する: ``key`` / ``cert`` /
   ``keyfile`` / ``certfile`` / ``cafile`` / ``otp`` / ``_auth`` /
   ``_authToken`` / ``_password`` / ``username`` / ``email`` / ``always-auth``
3. キー部が識別力のある部分文字列 (``_auth`` / ``_password`` / ``username`` /
   ``email`` / ``always-auth`` / ``keyfile`` / ``certfile`` / ``cafile``) を含む
4. **値部**が ``scheme://user:pass@host`` 形 (URL の userinfo に credential を
   埋めた形) — キー名は問わない (``registry=`` / ``@scope:registry=`` /
   ``proxy=`` / ``https-proxy=`` など)

いずれも大文字小文字を区別せず、キー名の ``-`` と ``_`` の差は吸収する
(``always-auth`` と ``always_auth`` は同じ)。

2 と 4 は 0.34.0 のマージ前レビューで追加した (それ以前の 0.33.2 は
``.npmrc`` を内容に依らず deny していたので、下表の 8 形は**すべて deny**
だった。1/3 だけでは 0.34.0 が新たに allow を開いてしまう):

===========================================  ======================
``.npmrc`` の 1 行                           npm での意味
===========================================  ======================
``registry=https://u:<TOKEN>@host/``         registry URL の userinfo
``@scope:registry=https://u:<TOKEN>@host/``  scoped registry の同形
``proxy=http://u:<TOKEN>@host:8080/``        proxy 認証
``https-proxy=http://u:<TOKEN>@host:8080/``  同上
``key=<PEM 本体>``                           client TLS 秘密鍵そのもの
``keyfile=/path/to/client.key``              秘密鍵ファイルの場所
``certfile=/path/to/client.crt``             証明書ファイルの場所
``otp=<6 桁>``                               2FA ワンタイムパスワード
===========================================  ======================

``key`` / ``cert`` / ``otp`` を**完全一致**にしてあるのは、部分文字列にすると
``keyword`` / ``certainty`` のような無関係なキーに誤爆するため。逆に
``keyfile`` / ``certfile`` / ``cafile`` のように識別力のある語は部分一致でも
拾う (``//host/:keyfile=`` は 1 の経路でも拾えるが、``scope:keyfile=`` のような
非標準の書き方も落とさないため)。

**値の有無は問わない**: 空値でも、``${NPM_TOKEN}`` のような環境変数参照でも
認証行として扱う。環境変数参照は実害が小さいが、境界を「認証の設定行が存在
するか」に固定して単純に保つ方を採る (値の形を見始めると「どこまでが秘密か」の
判断が必要になり、判定が内容依存の度合いを深めるため)。4 は例外だが、これは
「キー名では区別できないので値の**形**だけを見る」ものであって、値の中身を
読んで秘密らしさを測っているわけではない。

**非目的**: npm が読まないキー名 (``mytoken=…``) で秘密を書いた ``.npmrc`` は
allow。「``.npmrc`` は npm の設定ファイルである」という前提そのもので、内容
ゲートの設計上の受容範囲。
"""
from __future__ import annotations

import os
import re
import stat

# 内容ゲートのために読む上限 byte 数。``.npmrc`` は通常 1KB 未満なので、
# これを超えるものは「想定外の形」として判定を諦め deny 側に倒す。
MAX_NPMRC_BYTES = 64 * 1024

# キー部に含まれていれば認証行と見なす部分文字列 (正規化後の形で比較)。
# ``_authtoken`` は ``_auth`` に包含されるが、定義を docs と 1:1 で読める
# ようにするため明示しておく。
#
# **部分一致のまま残すこと**: 完全一致リスト (``_AUTH_EXACT_KEYS``) だけに
# 整理すると ``foo_auth=`` のような非標準キーが deny → allow に落ち、
# P1-1 (0.34.0 マージ前レビュー) が塞いだのと同じ「新規露出」を作り直す。
# 完全一致は「部分一致にすると誤爆する短いキー」を足すための追加経路であって、
# 部分一致の置き換えではない。
_AUTH_KEY_SUBSTRINGS = (
    "_auth",
    "_authtoken",
    "_password",
    "username",
    "email",
    "always_auth",
    "keyfile",
    "certfile",
    "cafile",
)

# キー名そのものが認証情報を運ぶ npm 設定キー (完全一致)。部分一致にすると
# ``keyword`` / ``certainty`` / ``bot_password`` の ``key`` ``cert`` 等に
# 誤爆するため、ここだけは完全一致で判定する。
_AUTH_EXACT_KEYS = frozenset(
    {
        "key",        # client TLS 秘密鍵の PEM 本体
        "cert",       # client 証明書の PEM 本体
        "keyfile",    # 秘密鍵ファイルの場所
        "certfile",   # 証明書ファイルの場所
        "cafile",     # CA 証明書ファイルの場所
        "otp",        # 2FA ワンタイムパスワード
        "_auth",
        "_authtoken",
        "_password",
        "username",
        "email",
        "always_auth",
    }
)

# 値が ``scheme://user:pass@host`` 形か (URL の userinfo に credential を
# 埋めた形)。``registry`` / ``proxy`` / ``https-proxy`` 等、キー名では
# 区別できない経路を拾う。``@`` より前に ``:`` があることを要求するので
# ``https://registry.example.invalid/`` のような素の URL には一致しない。
_USERINFO_URL_RE = re.compile(r"^[a-z][a-z0-9+.\-]*://[^/@\s]*:[^/@\s]*@", re.I)

_COMMENT_PREFIXES = ("#", ";")


def is_npmrc_basename(basename: str) -> bool:
    """basename が ``.npmrc`` か (大文字小文字を区別しない)。

    既定の matcher は case-insensitive (``SFG_CASE_SENSITIVE=1`` で opt-out)
    なので、``.NPMRC`` が機密判定に引っかかる環境ではこのゲートも効くべき。
    逆に ``SFG_CASE_SENSITIVE=1`` の環境では ``.NPMRC`` はそもそも
    ``is_sensitive`` に一致せずここへ来ない。
    """
    return basename.lower() == ".npmrc"


def decode_npmrc(raw: bytes) -> str | None:
    """``.npmrc`` のバイト列をテキストにする。判定できなければ ``None``。

    ``.npmrc`` は ini 形式のテキストで、実運用では UTF-8 (まれに BOM 付き)。
    それ以外は「想定外の形」として ``None`` を返し、呼出側が **deny 側**に
    倒す (``errors="replace"`` で無理やり読むと、壊れた復号結果に認証行が
    見つからず allow に倒れうる — 保護を落とす方向の失敗になる)。
    """
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None


def _normalize_key(raw_key: str) -> str:
    """キー部を比較用に正規化する (小文字化 + ``-`` を ``_`` に寄せる)。

    ``always-auth`` と ``always_auth`` のような綴り差を吸収する。**比較する
    語彙側も正規化後の形 (``always_auth``) で書いておくこと** — 片側だけ
    正規化すると、その語だけ静かに一致しなくなる。
    """
    return raw_key.strip().lower().replace("-", "_")


def _value_has_userinfo(value: str) -> bool:
    """値が ``scheme://user:pass@host`` 形 (URL 埋め込み credential) か。"""
    return bool(_USERINFO_URL_RE.match(value.strip().strip('"').strip("'")))


_SCOPED_REGISTRY_LABEL = "//<registry>/:… (scoped registry auth)"
_USERINFO_LABEL = "<url with user:password@>"


def _auth_key_of(stripped: str) -> str | None:
    """コメント除去済みの 1 行が認証行なら**語彙側のラベル**を返す。

    判定の定義はモジュール docstring を参照。戻り値は deny reason に載るため、
    **入力から切り出した文字列は一切返さない** — 一致した語彙 (``_authtoken``
    等) か固定文言だけを返す。``raw_key`` (入力の head 側) を返していた頃は、
    ``=`` の無い行 / 値の中の ``=`` / 空白の無い行で head に値が混ざり、
    理由文へ credential が写った (マージ前レビューの指摘 3 巡分)。表面を
    切り出しで縮めるのではなく、出力を語彙に固定して経路ごと閉じる。
    """
    head, sep, tail = stripped.partition("=")
    key = _normalize_key(head)
    if not key:
        return None
    if key.startswith("//"):
        return _SCOPED_REGISTRY_LABEL
    if key in _AUTH_EXACT_KEYS:
        return key
    # 長い語彙を先に当てる (``_authtoken`` が ``_auth`` に食われないように)
    for token in sorted(_AUTH_KEY_SUBSTRINGS, key=len, reverse=True):
        if token in key:
            return token
    if sep and _value_has_userinfo(tail):
        return _USERINFO_LABEL
    return None


def scan_auth_lines(text: str) -> tuple[int, list[str]]:
    """認証行の **件数** と、そのキー名 (出現順・重複除去) を返す。

    キー名だけで**値は一切返さない**。deny reason に「なぜ block したか」を
    載せるための材料 (``core.messages.npmrc_auth_prefix``)。
    """
    count = 0
    keys: list[str] = []
    seen: set[str] = set()
    for line in text.splitlines():
        stripped = line.lstrip()
        if not stripped or stripped.startswith(_COMMENT_PREFIXES):
            continue
        raw_key = _auth_key_of(stripped)
        if raw_key is None:
            continue
        count += 1
        norm = _normalize_key(raw_key)
        if norm not in seen:
            seen.add(norm)
            keys.append(raw_key)
    return count, keys


def has_auth_line(text: str) -> bool:
    """``.npmrc`` のテキストに「認証らしい行」が 1 行でもあるか。

    定義はモジュール docstring を参照。
    """
    for line in text.splitlines():
        stripped = line.lstrip()
        if not stripped or stripped.startswith(_COMMENT_PREFIXES):
            continue
        if _auth_key_of(stripped) is not None:
            return True
    return False


def bytes_auth_scan(raw: bytes) -> tuple[bool, int, list[str]]:
    """``.npmrc`` バイト列の block 判定 + 認証行の件数 / キー名。

    戻り値は ``(block するか, 認証行の件数, キー名)``。decode できなければ
    ``(True, 0, [])`` — block はするが「認証行を見つけたから」ではないので
    件数は 0 のまま (deny reason が嘘の根拠を出さないようにするため)。
    """
    text = decode_npmrc(raw)
    if text is None:
        return True, 0, []
    count, keys = scan_auth_lines(text)
    return count > 0, count, keys


def bytes_require_block(raw: bytes) -> bool:
    """読み込み済みの ``.npmrc`` バイト列が block 対象か。

    上限判定は呼出側が済ませている前提 (fd 経由と path 経由で読み方が違うため)。
    decode できなければ ``True`` (fail-closed)。
    """
    return bytes_auth_scan(raw)[0]


def path_requires_block(path: str) -> bool:
    """``path`` の ``.npmrc`` が block 対象か (Stop hook 用の path 経由版)。

    **判定できない事情はすべて ``True`` (= 従来どおり報告する)** に倒す:

    - ``lstat`` / ``open`` / ``read`` が失敗する
    - 通常ファイルでない (symlink / ディレクトリ / 特殊ファイル)
    - ``MAX_NPMRC_BYTES`` を超える
    - UTF-8 として decode できない

    symlink を辿らないのは Read handler の ``classify`` と同じ方針 (リンク先を
    開いた結果で判定すると、判定対象と実体がずれる)。Stop hook は
    ``git ls-files`` の結果を見るだけで内容を LLM に渡さないため、ここでの
    「報告する」は block reason にファイル名が載るだけで値は出ない。
    """
    try:
        st = os.lstat(path)
    except OSError:
        return True
    if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_NPMRC_BYTES:
        return True
    try:
        with open(path, "rb") as f:
            raw = f.read(MAX_NPMRC_BYTES + 1)
    except OSError:
        return True
    if len(raw) > MAX_NPMRC_BYTES:
        return True
    return bytes_require_block(raw)
