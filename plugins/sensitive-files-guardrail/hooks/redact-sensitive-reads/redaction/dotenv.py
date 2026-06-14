"""dotenv (.env / .env.*) の minimal-info 化 (0.9.0 で E1 を取り込み)。

保持: 鍵名の順序 + 型クラス + (識別子型のみ) prefix + value status + 長さ +
placeholder ヒント。破棄: 値そのもの、コメント、空行、先頭 ``export``。

「値そのものは出さない」原則は維持しつつ、思想 2 (block 時は意図を汲んだ
メッセージを返す) を満たすため、デバッグに必要な品質情報を積極的に返す:

- ``<type=...>``: 値クラス (str / bool / null / num / jwt / url / email / uuid /
  aws_access_key / stripe_secret / stripe_pk / github_pat / openai_key)
- ``prefix="..."``: 識別子型に限り、公開済み prefix (sk_live_ / AKIA / ghp_ 等)
  を表示。本番鍵 (sk_live_) とテスト鍵 (sk_test_) を区別できるためロー
  テーション判断に有用。Q3 (REVIEW_TASKS_2026-05-06.md) 採用方針
- value status: ``<set>`` / ``<empty>`` / ``<placeholder>`` / ``<short>`` /
  ``<long>`` / ``<looks_truncated>`` の組み合わせを併記
- ``length=<N>``: 値のバイト長 (Q2 採用、bucket せず生長さ)。
  ``<empty>`` のときだけ length を出さない
- ``matched="..."``: placeholder 一致時に辞書 literal / pattern label を表示
"""
from __future__ import annotations

import re

from .placeholders import looks_placeholder
from .sanitize import sanitize_key

# KEY=VALUE / export KEY=VALUE の行をざっくり捕捉
# 完全な dotenv spec parser ではないが、Vibe Coder が書く .env を想定した範囲で十分
_LINE_RE = re.compile(
    r"""
    ^\s*
    (?:export\s+)?
    ([A-Za-z_][A-Za-z0-9_.\-]*)   # key
    \s*=\s*
    (.*?)                          # value (raw; used for type classification only)
    \s*$
    """,
    re.VERBOSE,
)

# JWT (header.payload.signature の base64url 3 パート、prefix="ey")
_JWT_RE = re.compile(r"^ey[A-Za-z0-9_-]+\.ey[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$")

# URL (scheme://...)
_URL_RE = re.compile(r"^[a-z][a-z0-9+\-.]*://", re.IGNORECASE)

# Email (簡易形)
_EMAIL_RE = re.compile(
    r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$"
)

# UUID (case-insensitive)
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

# 識別子付き型 (regex, type_class, returned_prefix)。順序は match 優先順。
# 値そのものは返さず、prefix だけ返す (Q3 = prefix 表示採用)。
_PREFIX_TYPE_MAP: list[tuple[re.Pattern[str], str, str]] = [
    # AWS access key (AKIA / ASIA, 末尾 16 文字以上の英数)
    (re.compile(r"^AKIA[A-Z0-9]{12,}$"), "aws_access_key", "AKIA"),
    (re.compile(r"^ASIA[A-Z0-9]{12,}$"), "aws_access_key", "ASIA"),
    # Stripe secret key (sk_live_ / sk_test_ / rk_live_ / rk_test_)
    (re.compile(r"^sk_live_[A-Za-z0-9]{20,}$"), "stripe_secret", "sk_live_"),
    (re.compile(r"^sk_test_[A-Za-z0-9]{20,}$"), "stripe_secret", "sk_test_"),
    (re.compile(r"^rk_live_[A-Za-z0-9]{20,}$"), "stripe_secret", "rk_live_"),
    (re.compile(r"^rk_test_[A-Za-z0-9]{20,}$"), "stripe_secret", "rk_test_"),
    # Stripe publishable key (pk_live_ / pk_test_)
    (re.compile(r"^pk_live_[A-Za-z0-9]{20,}$"), "stripe_pk", "pk_live_"),
    (re.compile(r"^pk_test_[A-Za-z0-9]{20,}$"), "stripe_pk", "pk_test_"),
    # GitHub personal access token (5 種)
    (re.compile(r"^ghp_[A-Za-z0-9]{30,}$"), "github_pat", "ghp_"),
    (re.compile(r"^gho_[A-Za-z0-9]{30,}$"), "github_pat", "gho_"),
    (re.compile(r"^ghu_[A-Za-z0-9]{30,}$"), "github_pat", "ghu_"),
    (re.compile(r"^ghs_[A-Za-z0-9]{30,}$"), "github_pat", "ghs_"),
    (re.compile(r"^ghr_[A-Za-z0-9]{30,}$"), "github_pat", "ghr_"),
    # OpenAI API key (sk-プレフィックス、英数 + - / _)
    (re.compile(r"^sk-[A-Za-z0-9_\-]{20,}$"), "openai_key", "sk-"),
]

# 型ごとの最低長閾値。これより短いと ``<short>`` タグを付ける (型整合性ヒント)。
_MIN_LENGTH_BY_TYPE: dict[str, int] = {
    "jwt": 30,
    "aws_access_key": 16,
    "stripe_secret": 25,
    "stripe_pk": 25,
    "github_pat": 30,
    "openai_key": 20,
    "url": 8,
    "uuid": 36,
    "email": 6,
}

# 4096 byte 超は ``<long>`` (デバッグダンプ混入のヒント)。
_MAX_LENGTH_GENERIC = 4096


def _preprocess_value(raw: str) -> str:
    """型判定 / status 判定 / length 計測の共通前処理。

    - クォート (``"..."`` / ``'...'``) を剥がす
    - 非クォート時の inline comment (``\\s+#``) を削る
    - quote 剥がし後の値はそのまま (前後 strip しない、内部空白も維持)
    """
    if not isinstance(raw, str):
        return ""
    v = raw.strip()
    was_quoted = len(v) >= 2 and v[0] in ('"', "'") and v[-1] == v[0]
    if was_quoted:
        return v[1:-1]
    m = re.search(r"\s+#", v)
    if m:
        return v[: m.start()].rstrip()
    return v


def _detect_type_and_prefix(v: str) -> tuple[str, str | None]:
    """前処理済みの値 ``v`` から型クラスと prefix を返す。

    Returns:
        (type_class, prefix). prefix は識別子型 (jwt / aws_access_key /
        stripe_* / github_pat / openai_key) のみ非 None。
    """
    if v == "":
        return ("str", None)
    lower = v.lower()
    if lower in ("true", "false"):
        return ("bool", None)
    if lower in ("null", "nil", "none"):
        return ("null", None)
    try:
        int(v)
        return ("num", None)
    except ValueError:
        pass
    try:
        float(v)
        return ("num", None)
    except ValueError:
        pass

    # 識別子付き型 (prefix を返す)
    for pattern, type_class, prefix in _PREFIX_TYPE_MAP:
        if pattern.match(v):
            return (type_class, prefix)

    if _JWT_RE.match(v):
        return ("jwt", "ey")

    if _URL_RE.match(v):
        return ("url", None)
    if _EMAIL_RE.match(v):
        return ("email", None)
    if _UUID_RE.match(v):
        return ("uuid", None)

    return ("str", None)


def _classify_status(
    v: str,
    type_class: str,
    placeholder_label: str | None,
) -> tuple[list[str], int]:
    """前処理済みの値 ``v`` から status タグ群と length (バイト長) を返す。

    status タグは複数併記可:
    - ``<empty>``: 値なし (``KEY=`` または ``""`` / 空白のみ)。単独で返す
    - ``<placeholder>``: placeholder 一致 (``<set>`` の代わりに併記)
    - ``<set>``: それ以外で値あり
    - ``<short>``: 型から想定される最低長を下回る
    - ``<long>``: 4096 byte 超
    - ``<looks_truncated>``: 末尾が ``...`` / ``<truncated>`` / バックスラッシュ

    length は ``<empty>`` のとき 0、それ以外は前処理後の値のバイト長。
    """
    if v == "":
        return (["<empty>"], 0)

    tags: list[str] = []
    if placeholder_label is not None:
        tags.append("<placeholder>")
    else:
        tags.append("<set>")

    n = len(v)
    min_len = _MIN_LENGTH_BY_TYPE.get(type_class)
    if min_len is not None and n < min_len:
        tags.append("<short>")
    if n > _MAX_LENGTH_GENERIC:
        tags.append("<long>")

    # truncated 判定 (末尾 sentinel)
    if (
        v.endswith("...")
        or v.endswith("<truncated>")
        or v.endswith("\\")
    ):
        tags.append("<looks_truncated>")

    return (tags, n)


def redact_dotenv(text: str) -> dict:
    """dotenv テキストから minimal info を抽出する。

    Returns:
        {
          "format": "dotenv",
          "entries": int,
          "keys": [
            {
              "name": str,
              "type": str,
              "status": list[str],
              "length": int,
              "prefix": str,            # 識別子型のみ
              "placeholder": str,        # placeholder 一致時のみ
            },
            ...
          ],
        }
    """
    keys: list[dict] = []
    for line in text.splitlines():
        # コメント・空行スキップ
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        m = _LINE_RE.match(line)
        if not m:
            continue
        raw_key, raw_val = m.group(1), m.group(2)
        v = _preprocess_value(raw_val)
        type_class, prefix = _detect_type_and_prefix(v)
        is_ph, ph_label = looks_placeholder(v)
        tags, length = _classify_status(
            v, type_class, ph_label if is_ph else None
        )

        entry: dict = {
            "name": sanitize_key(raw_key),
            "type": type_class,
            "status": tags,
            "length": length,
        }
        if prefix is not None:
            entry["prefix"] = prefix
        if is_ph and ph_label is not None:
            entry["placeholder"] = ph_label
        keys.append(entry)

    return {
        "format": "dotenv",
        "entries": len(keys),
        "keys": keys,
    }


def format_dotenv(info: dict) -> str:
    """``redact_dotenv`` の結果を人間可読文字列に整形 (reason 用)。

    出力例 (entries=6 のケース):

    .. code-block:: text

        format: dotenv
        entries: 6
        keys (in order):
          1. DATABASE_URL  <type=url>  <set>  length=42
          2. JWT_SECRET    <type=jwt prefix="ey">  <set>  length=287
          3. STRIPE_KEY    <type=stripe_secret prefix="sk_live_">  <set>  length=68
          4. TOKEN         <type=str>  <set>  <looks_truncated>  length=20
          5. PLACEHOLDER   <type=str>  <placeholder>  matched="your_jwt_secret_here"  length=24
          6. EMPTY_KEY     <type=str>  <empty>
        note: real values are not in context. only key names, type, prefix,
              length, status tags, and placeholder hints are returned.
    """
    lines = ["format: dotenv", f"entries: {info['entries']}"]
    if info["entries"] == 0:
        lines.append("(no entries)")
        return "\n".join(lines)
    lines.append("keys (in order):")
    for i, k in enumerate(info["keys"], 1):
        type_part = f"<type={k['type']}"
        if "prefix" in k:
            type_part += f' prefix="{k["prefix"]}"'
        type_part += ">"

        status_part = "  ".join(k["status"])

        line = f"  {i}. {k['name']}  {type_part}  {status_part}"
        if "placeholder" in k:
            line += f'  matched="{k["placeholder"]}"'
        # <empty> のときだけ length を出さない (常に 0 で意味なし)
        if "<empty>" not in k["status"]:
            line += f"  length={k['length']}"
        lines.append(line)

    lines.append(
        "note: real values are not in context. only key names, type, prefix,"
        " length, status tags, and placeholder hints are returned."
    )
    return "\n".join(lines)
