"""案内するコマンドに、値をシェルの 1 語として埋め込む (v0.17.1)。

**なぜ必要か**: deny 文面の切替案内 (`kubectl config use-context <期待値>` 等) や
builder の `pin-env` が出す `firebase use <x>` は、Claude がそのまま実行しがちな
「次に打つコマンド」。値は accounts.local.json の期待値、リポジトリの `.firebaserc`、
CLI の設定 (AWS config の profile 名、gh の host 名) から来て、期待値のファイルや
`.firebaserc` はリポジトリに置かれうる (clone しただけのリポジトリでも中身を決められる)。
値に `;` / `$()` / 空白 / 改行などのシェルの構文や、option と解釈される先頭の `-` が
あると、案内どおりに打ったコマンドが別のコマンドや option として走る。

**どう埋め込むか**: 許容形に `fullmatch` した値だけをコマンドに出し、`shlex.quote` も
通す。許容形から外れた値はコマンドの形で案内せず、呼び出し側が「手で確認してください」
の文に落とす (`UNSAFE` を添える)。dispatcher は `UNSAFE` を含む deny に「案内した
コマンドは単独で実行」の注記を付けない (`期待=...` の表示がコマンドの形に見えても、
それを案内したことにしない。外れた名前を文に置き換えたうえで必ず案内を出す aws は
除く)。許容形の文字は `shlex.quote` がクォートを付けない
範囲に収めてあるので、普通の値の文面 (と、案内したコマンドを通す
`is_self_remediation` / 注記を付ける `REMEDIATION_PATTERNS` が見る形) は変わらない。
quote は許容形を将来緩めたときの二重化。

**値を文面に示すとき** (`現在=` / `期待=` / コマンド指定 / alias の行き先 / host など。
コマンドの形で案内するのではなく、値そのものを見せる表示) は、出どころに関わらず
`shown` / `shown_all` を通す (v0.21.0)。示すのは、許容形 (`WORD`) に `fullmatch` する値か、
空白 (Unicode の空白を含む) も `=` も含まず、Unicode の制御文字 (Cc)・書式文字 (Cf)・行区切り
(Zl)・段落区切り (Zp)・サロゲート (Cs)・私用領域 (Co)・未割り当て (Cn)、既定で無視される文字
(Default_Ignorable_Code_Point。Hangul filler・異体字セレクタ・結合書記素接合子など)、空白に
見える文字 (点字の空白など) も含まず、結合文字 (Mn / Me / Mc) で始まらない値 (`can_show`)。
それ以外は `NOT_SHOWN` に置き換える。不可視の文字を空白の代わりに使えば、空白を含まない値でも
文面の上ではコマンドの形 (`xㅤkubectlㅤconfigㅤuse-contextㅤevil`) に見え、先頭の結合文字は
前の固定文 (`期待=` の `=`) と合成されて別の記号 (`≠`) に見える。値の途中の結合文字は隠さない
(NFD で書いた日本語の名前の濁点など、普通の名前に現れる)。
値は期待値のファイル・`.firebaserc`・CLI の設定・コマンドの引数から来て、改行を含む値を
そのまま出すと値の外に偽の行 (「切り替え: ...」) を差し込める。改行を含まなくても、
`REMEDIATION_PATTERNS` の形 (`x kubectl config use-context evil`) を書いた値は、表示しただけで
dispatcher の「案内したコマンドは単独で実行」の注記の判定に当たる。どのパターンも空白を要し
(`AWS_PROFILE=` の形は `=` を要する)、示す値はどちらも含まないので、示した値が単独でその形に
なることはない (前後の固定文と合わせて形を作らないことは、tests の TestShownValueContract が
service ごとに確かめる)。示す形は案内に使える形 (`WORD`) より広い: シェル上は無害な普通の名前
(kubectl の `_local` / `a,b` / 日本語の context 名) を隠すと、何が不一致かが文面から消える。
0.20.0 までは、検出したコマンド自身が指定した値を `shlex.quote` だけ通して示していたが、quote は
改行も空白入りのコマンドの形も残す。指定した値はそのまま `(検出コマンド: ...)` の行に出るので、
ここでは示さなくても何を指定したかは分かる。

**値をそのまま (置き換えずに) 出すとき** (`(検出コマンド: ...)` の行に出すコマンド、CLI の
エラー出力の転記、並行セッションの記録にあるパスなど。空白や `=` を含むのが普通で、`shown` を
通すと何も分からなくなる値) は `escape_controls` を通す。C0 / C1 の制御文字・DEL・Unicode の
行区切り / 段落区切り・書式文字 (Cf) をエスケープした形 (`\\n` / `\\xNN` / `\\uNNNN` /
`\\UNNNNNNNN`) にして、値が文面の中で 1 行に収まるようにする (改行で偽の行を差し込めない。
端末の制御シーケンスも、表示の向きを入れ替える双方向制御の文字も残さない)。

**値を JSON として示すとき** (builder の期待値・CLI 現在値の表示、`pin-env` の候補・現在値・env の断片)
は `json_one_line` を通す。`json.dumps` の出力のうち行を割る文字・孤立サロゲートだけを JSON の
`\\uXXXX` にするので、表示を写して使っても元の値に戻る (v0.21.3)。
"""
from __future__ import annotations

import bisect
import json
import re
import shlex
import unicodedata

# 英数字で始まり、英数字と `.` `_` `-` だけからなる名前。firebase の alias /
# project ID (deny 文面の `firebase use` / `--project` と builder の `pin-env`)。
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
NAME_RULE = "使えるのは英数字で始まり、英数字と . _ - だけからなる名前"

# NAME に `:` `/` `@` `+` を足したもの。kubectl の context 名 (EKS の
# `arn:aws:eks:<region>:<account>:cluster/<name>`、kubeadm の
# `kubernetes-admin@kubernetes`)、gcloud の account (メールアドレス) とドメイン付きの
# project ID (`example.com:my-project`)、AWS の profile 名、gh の host 名 / アカウント名、
# firebase の dict の案内行で `#` の後ろに置く project (コメントなので alias より広くてよい)。
# 足した 4 文字はシェルで特別な意味を持たず、`shlex.quote` もクォートを付けない
# (`~` / `=` / `%` / `,` は入れない。`~` と `=` は語の先頭で展開されうる)。
WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]*")

# 許容形から外れた値の代わりに置く文 (コマンドの形をとらない。「〜は」「〜が」に続ける)。
# 「危険な文字を含む」とは言わない: 許容形は狭く、シェル上は無害な値 (kubectl の
# `_local` / `a,b` / 日本語の context 名、`firebase use` に渡す `example.com:proj` など)
# も外れる。言えるのは「案内に使える形ではない」という事実だけ。
UNSAFE = (
    "案内に使える形 (英数字で始まり、英数字と一部の記号だけからなる形) ではないため、"
    "コマンドの形では案内しません"
)


def arg(value, pattern: re.Pattern = WORD) -> str | None:
    """value を案内コマンドの 1 引数にした文字列。許容形でなければ None。

    照合は `fullmatch` で行う (`$` は末尾の改行の前でも一致するため)。
    """
    if not isinstance(value, str) or not pattern.fullmatch(value):
        return None
    return shlex.quote(value)


# 文面に示せない値の代わりに置く語 (`shown`)。コマンドの形も `UNSAFE` の文も含まない。
NOT_SHOWN = "(表示しない値)"


# `shown` が示す形のうち、`WORD` より広い側 (空白と `=` を含まない)。
_SHOWABLE = re.compile(r"[^\s=]+")
# 示さない Unicode の一般カテゴリ (制御文字・書式文字・行区切り・段落区切り・サロゲート・
# 私用領域・未割り当て)。私用領域と未割り当ては、端末やフォントによって何が見えるかが決まらない。
_HIDDEN_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp", "Cs", "Co", "Cn"})

# 示さない文字の範囲 (両端を含む)。標準ライブラリには Default_Ignorable_Code_Point の判定が
# 無いので、範囲を定数で持つ。出典は Unicode の DerivedCoreProperties.txt の
# Default_Ignorable_Code_Point (Unicode 14.0〜17.0 の DerivedCoreProperties.txt と一致)。
# 既定で無視される文字は表示されないか幅を持たず、空白の代わりに置くと、空白を含まない値が
# コマンドの形に見える。大半は Cf / Cn (カテゴリでも隠れる。範囲は出典どおりに持つ) だが、
# Lo (Hangul filler) と Mn (U+034F / U+17B4-17B5 / U+180B-180D・U+180F / 異体字セレクタ) も含む。
# 「空白に見える」と注記した行は Default_Ignorable ではないが空白に見える文字 (点字の空白
# U+2800、契丹小字の filler U+16FE4、エジプト聖刻文字の空白 U+13441-13442、音符記号の
# NULL NOTEHEAD U+1D159、OBJECT REPLACEMENT CHARACTER U+FFFC、IDEOGRAPHIC HALF FILL SPACE
# U+303F)。隠しても deny / allow は変わらず、表示されないだけなので安全側。
# 名前で拾うこの禁止リストは閉じない。新しい Unicode の版で空白に見える文字が増えうる
# (U+13441 は Unicode 15.0 で足された。Python 3.11 以前では未割り当てなのでカテゴリでも隠れる)。
# bisect で引くので開始の昇順に並べる。
_INVISIBLE_RANGES = (
    (0x00AD, 0x00AD),  # SOFT HYPHEN
    (0x034F, 0x034F),  # COMBINING GRAPHEME JOINER
    (0x061C, 0x061C),  # ARABIC LETTER MARK
    (0x115F, 0x1160),  # HANGUL CHOSEONG / JUNGSEONG FILLER
    (0x17B4, 0x17B5),  # KHMER VOWEL INHERENT AQ / AA
    (0x180B, 0x180F),  # MONGOLIAN FREE VARIATION SELECTOR 1-4 / VOWEL SEPARATOR
    (0x200B, 0x200F),  # ZERO WIDTH SPACE .. RIGHT-TO-LEFT MARK
    (0x202A, 0x202E),  # LEFT-TO-RIGHT EMBEDDING .. RIGHT-TO-LEFT OVERRIDE
    (0x2060, 0x206F),  # WORD JOINER .. NOMINAL DIGIT SHAPES (未割り当てを含む)
    (0x2800, 0x2800),  # BRAILLE PATTERN BLANK (空白に見える)
    (0x303F, 0x303F),  # IDEOGRAPHIC HALF FILL SPACE (空白に見える)
    (0x3164, 0x3164),  # HANGUL FILLER
    (0xFE00, 0xFE0F),  # VARIATION SELECTOR 1-16
    (0xFEFF, 0xFEFF),  # ZERO WIDTH NO-BREAK SPACE
    (0xFFA0, 0xFFA0),  # HALFWIDTH HANGUL FILLER
    (0xFFF0, 0xFFF8),  # 未割り当て (Default_Ignorable として予約)
    (0xFFFC, 0xFFFC),  # OBJECT REPLACEMENT CHARACTER (空白に見える)
    (0x13441, 0x13442),  # EGYPTIAN HIEROGLYPH FULL BLANK / HALF BLANK (空白に見える)
    (0x16FE4, 0x16FE4),  # KHITAN SMALL SCRIPT FILLER (空白に見える)
    (0x1BCA0, 0x1BCA3),  # SHORTHAND FORMAT LETTER OVERLAP .. UP STEP
    (0x1D159, 0x1D159),  # MUSICAL SYMBOL NULL NOTEHEAD (空白に見える)
    (0x1D173, 0x1D17A),  # MUSICAL SYMBOL BEGIN BEAM .. END PHRASE
    (0xE0000, 0xE0FFF),  # タグ文字・VARIATION SELECTOR 17-256 と、その前後の予約
)
_INVISIBLE_STARTS = tuple(lo for lo, _ in _INVISIBLE_RANGES)

# 値の先頭に来ると、前の固定文の文字と合成される結合文字の一般カテゴリ。
_COMBINING_CATEGORIES = frozenset({"Mn", "Me", "Mc"})


def _invisible(code: int) -> bool:
    """code が `_INVISIBLE_RANGES` のどれかに入るか。"""
    i = bisect.bisect_right(_INVISIBLE_STARTS, code) - 1
    return i >= 0 and code <= _INVISIBLE_RANGES[i][1]


def can_show(value) -> bool:
    """value を文面にそのまま示してよいか (`shown` の判定)。

    `WORD` に合うか、次のどれも満たす文字列なら真。str 以外は偽。

    - 空白も `=` も含まない
    - 制御文字・書式文字・行区切り・段落区切り・サロゲート・私用領域・未割り当てを含まない
    - 既定で無視される文字 (Hangul filler・異体字セレクタなど) と、空白に見える文字
      (点字の空白など) を含まない (`_INVISIBLE_RANGES`)
    - 結合文字 (Mn / Me / Mc) で始まらない (途中の結合文字は NFD の濁点などなので示す)
    """
    if not isinstance(value, str):
        return False
    if WORD.fullmatch(value):
        return True
    if not _SHOWABLE.fullmatch(value):
        return False
    if unicodedata.category(value[0]) in _COMBINING_CATEGORIES:
        return False
    return not any(
        unicodedata.category(ch) in _HIDDEN_CATEGORIES or _invisible(ord(ch)) for ch in value
    )


def shown(value) -> str:
    """deny 文面などに値そのものを示すときの形。示せない値 (`can_show`) は `NOT_SHOWN`。

    コマンドに埋め込む `arg` と違い quote は付けない (表示であってコマンドではない)。
    str 以外も `NOT_SHOWN` にする。
    """
    if can_show(value):
        return value
    return NOT_SHOWN


def shown_all(values) -> str:
    """値の並びを `shown` に通し、重複を除いて `, ` で並べる (順は辞書順)。"""
    return ", ".join(sorted({shown(value) for value in values}))


# 旧ファイルの削除の案内で、制御文字を含むパスをコマンドの形にしないときの注記
# (builder の migrate と hook の deny 文面が同じ文言を使う)。
NOT_COMMAND_FORM_REMOVE = "(制御文字を含むため、コマンドの形では案内しません。手で削除してください)"


def escape_controls(text) -> str:
    """text の制御文字をエスケープした表示形 (値を置き換えずに 1 行のまま示すとき)。

    C0 (U+0000〜U+001F)・DEL (U+007F)・C1 (U+0080〜U+009F) と、Unicode の行区切り (U+2028)・
    段落区切り (U+2029)・書式文字 (Cf。双方向制御の U+202A〜U+202E / U+2066〜U+2069 /
    U+200E / U+200F / U+061C、ゼロ幅スペースなど)・孤立サロゲート (U+D800〜U+DFFF。argv や
    JSON から入りうる。UTF-8 で書けず、出力で `UnicodeEncodeError` になる) を、改行は `\\n`、
    ほかは `\\xNN` / `\\uNNNN` (BMP の外の書式文字は `\\UNNNNNNNN`) の形にする。それ以外の文字 (空白や日本語)
    はそのまま。str 以外は `str()` を通してから同じ処理をする。

    元の値に一意に戻せる表示ではない (文字どおりのバックスラッシュと区別しない。`"a\\nb"` と
    改行入りの値は同じ形になる)。目的は 1 行に収めること。
    """
    text = text if isinstance(text, str) else str(text)
    out = []
    for ch in text:
        code = ord(ch)
        if ch == "\n":
            out.append("\\n")
        elif code < 0x20 or 0x7F <= code <= 0x9F:
            out.append(f"\\x{code:02x}")
        elif code in (0x2028, 0x2029) or 0xD800 <= code <= 0xDFFF or unicodedata.category(ch) == "Cf":
            out.append(f"\\u{code:04x}" if code <= 0xFFFF else f"\\U{code:08x}")
        else:
            out.append(ch)
    return "".join(out)


def json_one_line(value) -> str:
    """value の JSON 表示を、必ず 1 行・UTF-8 で書ける形にする (v0.21.3)。`json.dumps(ensure_ascii=False)` の後処理。

    `json.dumps` は U+0020 未満の制御文字は直すが、DEL・C1 (`\\x85` など)・行区切り (U+2028) /
    段落区切り (U+2029)・書式文字 (Cf。双方向制御・ゼロ幅スペース・BMP の外のタグ文字など)・
    孤立サロゲートはそのまま出す。前の 4 つは行を割り、最後の 1 つは UTF-8 で書けない。
    それらを JSON として同じ値に戻る `\\uXXXX` (BMP の外は代理対) にする。日本語などの普通の文字は
    変えない。`escape_controls` と違い置き換えではなく JSON の文法の内側の表示なので、JSON として
    読めば元の値に戻る (settings の `env` に貼る断片はそのまま使える)。ただし、隣り合う孤立サロゲートの
    高位・低位の組だけは、読み直すと 1 文字に結合する (argv の surrogateescape の低位が、JSON の
    エスケープの高位の直後に並ぶ経路。builder の `--commit` は UTF-8 で書けない値を断るので、
    ファイルには書かれない)。
    """
    out = []
    for ch in json.dumps(value, ensure_ascii=False):
        code = ord(ch)
        if (
            code == 0x7F
            or 0x80 <= code <= 0x9F
            or code in (0x2028, 0x2029)
            or 0xD800 <= code <= 0xDFFF
            or unicodedata.category(ch) == "Cf"
        ):
            if code > 0xFFFF:
                code -= 0x10000
                out.append(f"\\u{0xD800 + (code >> 10):04x}\\u{0xDC00 + (code & 0x3FF):04x}")
            else:
                out.append(f"\\u{code:04x}")
        else:
            out.append(ch)
    return "".join(out)
