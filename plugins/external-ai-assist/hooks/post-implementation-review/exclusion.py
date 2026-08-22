"""外部 AI (cursor) に差分を送らないファイルの判定。

post-implementation-review は編集パス全件の HEAD 基準 diff (untracked は全文) を Cursor に
渡す。0.4.1 までは gitignore 済み以外の除外が無く、tracked の `.env` / `*.pem` / 認証情報や
議事録・顧客メールの `.txt` もそのまま外部に送っていた。

判定の順序 (先に当たったものが勝つ):

0. `EXTERNAL_AI_POST_REVIEW_EXCLUDE` の `!glob` (否定) に当たれば **必ず送る**
   (既定除外 / CODE_ONLY より優先。`*secret*` 相当の既定にコードが巻き込まれた時の逃げ道)
1. 既定除外 glob (`DEFAULT_EXCLUDE_GLOBS`) と既定除外語 (`DEFAULT_EXCLUDE_WORDS`) —
   機密ファイルの名前パターン (`EXTERNAL_AI_POST_REVIEW_EXCLUDE_DEFAULTS=0` で無効化できる)
2. `EXTERNAL_AI_POST_REVIEW_EXCLUDE` — カンマ区切りの追加 glob
3. `EXTERNAL_AI_POST_REVIEW_CODE_ONLY` — 真ならコード以外 (`NON_CODE_SUFFIXES` の拡張子) も外す

glob は **basename と作業ツリー相対パスの両方**に、**大文字小文字を区別せず**当てる。
`fnmatch` の `*` は `/` にもマッチするので、`docs/*` は深い階層も拾う。除外語は
「英数字以外で区切られた単語」として相対パス全体に当てる (`config/secrets/db.yaml` や
`client_secret.json` は当たり、`secretary.py` / `secretsanta.ts` は当たらない)。
symlink は `__main__._resolve_paths` がリンク名と実体パスの両方を候補として渡す
(どちらかが当たれば除外)。

除外は恒久で、除外されたパスは claim から落とし pending にも reviewed にも残さない
(作業ツリー外のパスと同じ扱い)。他 plugin (sensitive-files-guardrail 等) には依存しない —
この hook が外部に送るものはこの hook 自身が決める。

git へのパス受け渡しは `gitscan._git` が `--literal-pathspecs` で行う。これが無いと
`app/[id]/page.tsx` のような名前が glob として解釈され、claim していない (除外判定も通って
いない) 別ファイルの diff が同じ section に混入する。
"""
from __future__ import annotations

import fnmatch
import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

ENV_EXCLUDE = "EXTERNAL_AI_POST_REVIEW_EXCLUDE"
ENV_EXCLUDE_DEFAULTS = "EXTERNAL_AI_POST_REVIEW_EXCLUDE_DEFAULTS"
ENV_CODE_ONLY = "EXTERNAL_AI_POST_REVIEW_CODE_ONLY"

# 既定で外部に送らないファイル名パターン (小文字で比較)。「名前からして機密」のものだけを
# 載せ、`*token*` / `*password*` のようにコード (tokenizer / password_validator) を巻き込む
# 広いパターンは入れない。`id_*` も `id_generator.py` を拾うため SSH 鍵の実名に限定している。
DEFAULT_EXCLUDE_GLOBS: tuple[str, ...] = (
    # dotenv / direnv
    ".env",
    ".env.*",
    "*.env",
    ".envrc",
    # 秘密鍵・鍵束・暗号化ファイル
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*.p8",
    "*.jks",
    "*.keystore",
    "*.ppk",
    "*.gpg",
    "*.pgp",
    "*.asc",
    "*.kdbx",
    "id_rsa*",
    "id_dsa*",
    "id_ecdsa*",
    "id_ed25519*",
    # クラウド / VPN / k8s の認証情報
    "*service-account*.json",
    "*service_account*.json",
    "kubeconfig*",
    "*.ovpn",
    # 認証情報を持つ設定ファイル
    ".htpasswd",
    "*.htpasswd",
    ".netrc",
    "_netrc",
    ".npmrc",
    ".pypirc",
    ".pgpass",
    ".git-credentials",
    # インフラの state / 変数 (接続文字列・鍵が平文で入る)
    "*.tfstate",
    "*.tfstate.*",
    "*.tfvars",
    "*.tfvars.*",
)

# 相対パスに「単語として」含まれると除外する語 (ディレクトリ名も対象)。glob `*secret*` だと
# secretary.py / secretsanta.ts / nosecret のようなコードまで巻き込むため、前後が英数字以外
# (`_` `-` `.` `/` や端) のときだけ当てる。
DEFAULT_EXCLUDE_WORDS: tuple[str, ...] = ("secret", "secrets", "credential", "credentials")

# `EXTERNAL_AI_POST_REVIEW_CODE_ONLY` で「コード以外」とみなす拡張子 (小文字)。
# JSON / YAML / TOML / XML / HTML / CSS は設定・マークアップとしてレビュー対象に残す。
# 拡張子の無いファイル (Makefile / Dockerfile / LICENSE) もコード扱いで残す。
NON_CODE_SUFFIXES: frozenset[str] = frozenset(
    {
        # 文書・テキスト
        ".md", ".markdown", ".rst", ".txt", ".text", ".adoc", ".asciidoc", ".org",
        ".tex", ".rtf", ".pdf", ".doc", ".docx", ".odt",
        ".xls", ".xlsx", ".ods", ".ppt", ".pptx", ".odp",
        # データ・ログ
        ".csv", ".tsv", ".jsonl", ".ndjson", ".log",
        ".parquet", ".avro", ".sqlite", ".sqlite3", ".db",
        # メール・連絡先・予定
        ".eml", ".msg", ".mbox", ".ics", ".vcf",
        # 画像・音声・動画
        ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".ico", ".tif", ".tiff",
        ".psd", ".heic", ".mp3", ".wav", ".ogg", ".flac", ".mp4", ".mov", ".avi",
        ".mkv", ".webm",
        # アーカイブ
        ".zip", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".7z", ".rar",
    }
)

_TRUTHY = ("1", "true", "on", "yes")
_FALSY = ("0", "false", "off", "no")

_WORD_PATTERNS: dict[str, re.Pattern[str]] = {
    word: re.compile(rf"(?<![a-z0-9]){re.escape(word)}(?![a-z0-9])")
    for word in DEFAULT_EXCLUDE_WORDS
}


def _normalize_glob(pattern: str) -> str:
    """`./docs/` `/docs/` → `docs/*`。作業ツリー相対パスに当てるので先頭の `/` `./` は無意味。"""
    pattern = pattern.strip()
    while pattern.startswith("./"):
        pattern = pattern[2:]
    pattern = pattern.lstrip("/")
    if pattern.endswith("/"):
        pattern += "*"
    return pattern


def parse_globs(raw: str | None) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """カンマ区切りの glob を (除外 glob, 否定 glob) に分けて正規化する。

    `!glob` は否定 (必ず送る)。空要素は捨て、重複は最初の 1 つだけ残す。
    brace 展開 (`*.{py,js}`) は fnmatch に無いので対応しない (README に明記)。
    """
    excludes: list[str] = []
    includes: list[str] = []
    for item in (raw or "").split(","):
        item = item.strip()
        negate = item.startswith("!")
        pattern = _normalize_glob(item[1:] if negate else item)
        if not pattern:
            continue
        target = includes if negate else excludes
        if pattern not in target:
            target.append(pattern)
    return tuple(excludes), tuple(includes)


@dataclass(frozen=True)
class Policy:
    default_globs: tuple[str, ...]
    default_words: tuple[str, ...]
    extra_globs: tuple[str, ...]
    include_globs: tuple[str, ...]
    code_only: bool

    def explain(self, rels: Iterable[str | None]) -> tuple[str, str] | None:
        """候補パス (作業ツリー相対) のどれかが除外対象なら (当たった候補, 理由) を返す。

        理由にはパターン名だけを入れ、ファイル内容は決して含めない (systemMessage /
        stderr にそのまま出すため)。当たった候補を返すのは、symlink で実体名とリンク名が
        違う時に「どの名前が理由に当たったか」を通知に出すため。
        """
        candidates: list[tuple[str, str]] = []  # (表示名, 小文字の相対パス)
        for rel in rels:
            if not rel:
                continue
            shown = rel.replace(os.sep, "/")
            if all(shown != s for s, _ in candidates):
                candidates.append((shown, shown.lower()))
        if not candidates:
            return None

        for shown, lowered in candidates:
            if any(_glob_hit(lowered, glob) for glob in self.include_globs):
                return None

        for glob in self.default_globs:
            for shown, lowered in candidates:
                if _glob_hit(lowered, glob):
                    return shown, f"既定除外: {glob}"
        for word in self.default_words:
            for shown, lowered in candidates:
                if _WORD_PATTERNS[word].search(lowered):
                    return shown, f'既定除外: 語 "{word}"'
        for glob in self.extra_globs:
            for shown, lowered in candidates:
                if _glob_hit(lowered, glob):
                    return shown, f"{ENV_EXCLUDE}: {glob}"
        if self.code_only:
            for shown, lowered in candidates:
                suffix = os.path.splitext(lowered.rsplit("/", 1)[-1])[1]
                if suffix in NON_CODE_SUFFIXES:
                    return shown, f"CODE_ONLY: {suffix}"
        return None

    def reason(self, rels: Iterable[str | None]) -> str | None:
        hit = self.explain(rels)
        return hit[1] if hit else None


def _glob_hit(lowered_rel: str, glob: str) -> bool:
    """相対パス全体と basename の両方に glob を当てる (glob 側も小文字化)。"""
    pattern = glob.lower()
    if fnmatch.fnmatchcase(lowered_rel, pattern):
        return True
    base = lowered_rel.rsplit("/", 1)[-1]
    return base != lowered_rel and fnmatch.fnmatchcase(base, pattern)


def load_policy(environ: Mapping[str, str] | None = None) -> Policy:
    env = os.environ if environ is None else environ
    defaults_on = env.get(ENV_EXCLUDE_DEFAULTS, "").strip().lower() not in _FALSY
    extra_globs, include_globs = parse_globs(env.get(ENV_EXCLUDE))
    return Policy(
        default_globs=DEFAULT_EXCLUDE_GLOBS if defaults_on else (),
        default_words=DEFAULT_EXCLUDE_WORDS if defaults_on else (),
        extra_globs=extra_globs,
        include_globs=include_globs,
        code_only=env.get(ENV_CODE_ONLY, "").strip().lower() in _TRUTHY,
    )
