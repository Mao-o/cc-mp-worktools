"""ローカル CLI 設定ファイルの読み取り (最小 YAML / INI サブセット)。

**なぜ必要か**: アクティブアカウントを CLI 実行 (`gh auth status` /
`gcloud config get-value`) で取ると PreToolUse hook 1 回あたり数百 ms〜数秒かかる。
`gh auth status` は全 host のトークンを API で検証するため**ネットワーク往復**まで
含み、オフラインでは失敗して「アクティブアカウントを取得できません」で deny する。
一方どちらの CLI も**アクティブアカウントをローカルの設定ファイルに書いている**
ので、読めるときはファイルから決めたほうが速く、オフラインでも動く
(内部バックログ: 離脱率低減)。

**どこまで解釈するか**: 汎用の YAML / INI パーサは持たない (標準ライブラリに YAML は
無く、外部依存を入れない方針)。ここで受け付けるのは実際の設定ファイルが使っている
最小形だけで、**少しでも想定外の形が出てきたら `None` を返す**。呼び出し側は `None`
を「ローカルからは決められない」と解釈して従来の CLI 実行へ落とす。

「読めたつもりで違う値を返す」ほうが「読めなかった」より危険なため、判読できない形は
すべて後者へ倒す (fail to CLI)。CLI 実行は遅いだけで、判定そのものは従来と同じになる。
"""
from __future__ import annotations

import configparser
import os
from pathlib import Path

# 設定ファイルの読み取り上限。gh の hosts.yml / gcloud の config_<name> はいずれも
# 数 KB 以下なので、これを超える内容は「想定している設定ファイルではない」として
# 読まない (巨大ファイルを hook の中で全部読まないためのガードでもある)。
MAX_FILE_BYTES = 256 * 1024

# 値の先頭に現れたら解釈を諦める YAML の記法 (ブロックスカラー / anchor / alias /
# tag / flow collection)。単純な `key: value` 以外は扱わない。
_UNSUPPORTED_VALUE_HEADS = frozenset("|>&*!{}[]?%@`")

_QUOTES = ("'", '"')

HOME_ENV_VAR = "HOME"


def home_overridden(env) -> bool:
    """コマンド実行時の `HOME` が hook プロセスと違うなら True (= CLI に委ねる)。

    `HOME` は **gh も gcloud も設定ディレクトリの解決に使う** env である
    (gh: `GH_CONFIG_DIR` → `$XDG_CONFIG_HOME/gh` → `$HOME/.config/gh`、
    gcloud: `CLOUDSDK_CONFIG` → `$HOME/.config/gcloud`)。`HOME=<other> gh ...` の
    形でインライン指定されたコマンドは**別の設定ファイル**を読んで動くため、
    hook プロセス側の設定ファイルを読んで一致と判断すると、実行される CLI とは
    違うアカウントで allow しうる (ローカル読取の導入前は検証 subprocess にも
    同じ env を渡していたため、この形は不一致として deny されていた)。

    設定ディレクトリを `HOME` から組み直す方向 (env の追従) は採らない —
    gh と gcloud で解決の分岐が違い、推測で実装して取り違えた値で allow する
    より、このモジュールの一貫した方針どおり**エミュレートせず CLI に委ねる**
    方が安全。コストは「CLI を 1 回呼ぶ」だけ。

    `GH_CONFIG_DIR` / `CLOUDSDK_CONFIG` が明示されていて `HOME` が効かない場合も
    区別せず bail する (判断を単純に保つ側に倒す)。
    """
    value = env.get(HOME_ENV_VAR)
    return bool(value) and value != os.environ.get(HOME_ENV_VAR)


def read_text(path: Path) -> str | None:
    """設定ファイルを文字列で読む。読めない / 大きすぎる / UTF-8 でないなら None。"""
    try:
        with Path(path).open("rb") as handle:
            raw = handle.read(MAX_FILE_BYTES + 1)
    except OSError:
        return None
    if len(raw) > MAX_FILE_BYTES:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _unquote(token: str) -> str | None:
    """`'x'` / `"x"` の引用符を外す。閉じていない引用符は解釈不能 (None)。"""
    if len(token) >= 2 and token[0] in _QUOTES and token[-1] == token[0]:
        return token[1:-1]
    if token[:1] in _QUOTES:
        return None
    return token


def parse_nested_scalar_map(text: str) -> dict[str, dict[str, str]] | None:
    """`{トップレベルキー: {直下のキー: スカラー}}` だけを読む最小 YAML パーサ。

    gh の `hosts.yml` が実際に使っている形 (実測: gh 2.9x) に限定して解釈する::

        github.com:
            git_protocol: https
            users:
                some-user:
                other-user:
            user: some-user

    - インデント 0 の `key:` (値なし) がブロックの開始。ブロック内で**最初に現れた
      インデント**をそのブロックの「直下」とみなす
    - 直下の `key: value` だけを採用する。**より深い行は読まない** —
      `users:` の下に並ぶのは「そのホストに紐付く全アカウント」で、アクティブな
      アカウント (`user:`) とは別物。深い行を混ぜると非アクティブなアカウントを
      アクティブと誤認し、**期待値と一致する非アクティブアカウントで allow する**
      という最悪の誤りになる
    - 直下より浅い (かつ 0 でない) インデントが出てきたら形を掴めていないので None

    タブインデント / シーケンス (`- `) / ブロックスカラー / anchor / alias /
    インデント 0 のスカラー (`version: 2` のような将来のフォーマット変更) は
    いずれも None を返す = 呼び出し側が CLI 実行に落ちる。

    Returns:
        トップレベルキーごとの直下スカラー辞書。解釈できない形なら None。
    """
    result: dict[str, dict[str, str]] = {}
    current: dict[str, str] | None = None
    child_indent: int | None = None
    for raw_line in text.splitlines():
        if "\t" in raw_line:
            return None
        line = raw_line.rstrip()
        stripped = line.lstrip(" ")
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("-"):
            return None
        indent = len(line) - len(stripped)
        key_part, sep, value_part = stripped.partition(":")
        if not sep:
            return None
        key = _unquote(key_part.strip())
        if not key:
            return None
        value = value_part.strip()
        if indent == 0:
            # トップレベルにスカラーが現れる形は想定していない (hosts.yml には
            # 現れない)。形が変わったら読まずに CLI へ委ねる。
            if value:
                return None
            current = {}
            child_indent = None
            result[key] = current
            continue
        if current is None:
            return None
        if child_indent is None:
            child_indent = indent
        if indent > child_indent:
            continue
        if indent < child_indent:
            return None
        if not value:
            # 直下のキーがさらに入れ子を持つ形 (`users:`)。中身は読まない。
            continue
        if value[0] in _UNSUPPORTED_VALUE_HEADS:
            return None
        scalar = _unquote(value)
        if scalar is None:
            return None
        current[key] = scalar
    return result


def parse_ini_sections(text: str) -> dict[str, dict[str, str]] | None:
    """`[section]` + `key = value` の INI を `{section: {key: value}}` で返す。

    gcloud の `configurations/config_<name>` が実際に使っている形 (実測)::

        [core]
        account = someone@example.com
        project = my-project-id

    補間 (`%(x)s`) を行わない `RawConfigParser` を使う — 値に `%` が入っていても
    壊れないようにするため。重複キー・セクション外の行など configparser が
    エラーにする形は None を返し、呼び出し側を CLI 実行へ落とす。
    """
    parser = configparser.RawConfigParser()
    try:
        parser.read_string(text)
    except (configparser.Error, ValueError):
        return None
    return {
        section: {key: value for key, value in parser.items(section)}
        for section in parser.sections()
    }
