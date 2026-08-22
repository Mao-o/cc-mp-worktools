"""REVIEW_CLEAN sentinel の判定。

レビュアー (cursor / codex) は「critical な指摘が無ければ `REVIEW_CLEAN` の 1 行だけを
返す」契約だが、LLM はこれをコードフェンスで囲んだり、前置きの 1 文を添えたりする。
0.3.x までは「非空行が 1 行だけ」を要求していたため、

    critical 指摘はない
    ```
    REVIEW_CLEAN
    ```

のような実出力 (2026-08-20 の Stop hook 実例) が指摘扱いになり、Stop / ExitPlanMode を
誤って block していた。prompts 自体が sentinel をフェンス内で提示していたことも一因で、
0.4.0 で prompts 側もフェンス無しの 1 行に改めた。

## 判定規則 (迷ったら clean にしない)

1. コードフェンス行 (``` / ~~~、言語指定付き含む)・装飾だけの行 (`---` / `***` / `===` 等)・
   空行を取り除く。残りが無ければ、元の出力が空のときだけ clean (フェンスや罫線だけの
   出力は指摘扱い)
2. 残りがすべて sentinel 行 (前後の `` ` `` / `*` / `#` / 括弧 / 句読点を除いて
   `REVIEW_CLEAN`) なら clean
3. 残りが「sentinel 1 行 + もう 1 行」で、その 1 行が **「指摘なし」を述べる短い 1 文**
   なら clean。「短い 1 文」は次をすべて満たす行に限る:
   - 箇条書き / 番号付き / 見出しの行頭マーカーが無い
   - `MAX_PREAMBLE_CHARS` 文字以下
   - ファイルパス・行参照・強調 (`**`)・インラインコード・表の区切りを含まない
   - 文の区切り (`。` `;` `,` `、` `→` 等) を含まない = 1 文だけ
   - 逆接・例外表現 (「ただし」「以外」「除き」「but」「otherwise」「minor」等) を含まない
   - 行全体が否定文の allow-list (`_NO_FINDINGS_RE`) に **完全一致** する。許す前置きは
     「このプランに」「レビューの結果」「There are」「I found」程度で、
     「X が壊れている点以外は問題なし」「Missing null check; otherwise no issues」のように
     本文が前に付く行は一致しない
4. それ以外 (sentinel + 指摘本文、指摘のみ、3 行以上) は clean ではない

3 を設けないと前置き付きの clean を block し続け、exitplan-review では block 枠
(`EXTERNAL_AI_REVIEW_MAX`) も消費してしまう。逆に 3 の条件を緩めると「指摘 1 行 +
REVIEW_CLEAN」を clean と誤判定して critical feedback を silently drop しうる。
誤 block は「指摘なしの本文を Claude が読んで無視する」1 ターンの損失で済むが、
誤 clean は指摘が消えるので、条件は否定文の allow-list として狭く保つ
(`search` + 末尾アンカーでは「本文。末尾に否定文」が素通りしたため fullmatch にしている)。
"""
import re

REVIEW_CLEAN = "REVIEW_CLEAN"

# 「指摘なし」を述べる前置き 1 文として許容する最大文字数。
MAX_PREAMBLE_CHARS = 80

_FENCE_RE = re.compile(r"^\s*(?:`{3,}|~{3,})\s*(?P<info>[\w+.-]*)\s*$")
_DECORATION_RE = re.compile(r"^\s*([-*_=~])(?:\s*\1){2,}\s*$")
_SENTINEL_TRIM_RE = re.compile(
    r"^[\s`*#_>()（）\[\]「」『』\"']+|[\s`*#_()（）\[\]「」『』\"'.。!！]+$"
)
_EMPHASIS_TRIM_RE = re.compile(
    r"^[\s*_()（）「」『』\"']+|[\s*_()（）「」『』\"'.。!！]+$"
)
_LIST_MARKER_RE = re.compile(r"^\s*(?:[-*+•]|\d+[.)]|#{1,6})\s")
_FINDING_HINT_RE = re.compile(
    r"[/`*|]|:\d+|\.(?:py|js|ts|tsx|jsx|md|json|ya?ml|toml|sh|go|rs)\b"
)
_SEPARATOR_RE = re.compile(r"[。．.;；,，、→—–:：]")
_CAVEAT_RE = re.compile(
    r"\b(?:but|however|except|although|though|unless|otherwise|minor|nits?|"
    r"apart\s+from|aside\s+from|other\s+than)\b"
    r"|ただし|しかし|だが|ものの|けど|一方|以外|除き|除いて|除けば|他に|その他|軽微",
    re.IGNORECASE,
)

# --- 「指摘なし」の否定文 (行全体に fullmatch させる) ---------------------------
_JA_PREFIX = (
    r"(?:(?:この|本|今回の)(?:プラン|差分|変更|実装|コード)"
    r"(?:に|には|では|について|については|に関して)?|レビューの結果|現時点では?|今回は|特に)?"
)
_JA_QUALIFIER = r"(?:(?:critical|クリティカル|重大|致命的|blocking)\s*な?\s*)?"
_JA_NOUN = r"(?:指摘|問題|懸念|異常|不備|修正(?:が必要な|すべき)?(?:点|箇所)?|blocker|ブロッカー)"
_JA_SUFFIX = r"(?:事項|点|箇所)?"
_JA_PARTICLE = r"(?:は|が|も|については)?"
_JA_QUANTIFIER = r"(?:特に|1\s*件も|一件も|何も|現時点では?)?"
_JA_NEGATION = (
    r"(?:ない|無い|なし|無し|ありません|ございません|見当たりません|見当たらない"
    r"|見つかりません|認められません|確認できません(?:でした)?|確認されませんでした)"
)
_JA_TAIL = r"(?:です|でした|と判断します|と判断しました|と考えます|と考えました)?"
_JA_STATEMENT = (
    _JA_PREFIX + r"\s*" + _JA_QUALIFIER + _JA_NOUN + r"\s*" + _JA_SUFFIX + r"\s*"
    + _JA_PARTICLE + r"\s*" + _JA_QUANTIFIER + r"\s*" + _JA_NEGATION + _JA_TAIL
)
_JA_BARE = r"特に(?:ありません|ございません|なし|無し|ない|無い)" + _JA_TAIL

_EN_PREFIX = r"(?:(?:i|we)\s+(?:found|see|have|identified)|there\s+(?:are|is|were)|overall|in\s+short)?"
_EN_STATEMENT = (
    _EN_PREFIX + r"\s*(?:no|zero)\s+(?:(?:critical|blocking|major|significant|notable|serious)\s+)?"
    r"(?:issues?|findings?|concerns?|problems?|blockers?|defects?)"
    r"(?:\s+(?:were\s+|was\s+)?(?:found|detected|identified)|\s+to\s+report|\s+here"
    r"|\s+(?:in|with)\s+(?:this|the)\s+(?:diff|plan|change|implementation|code))?"
)
_EN_BARE = (
    r"nothing\s+(?:critical|blocking|to\s+(?:report|flag|fix))"
    r"|(?:looks|all)\s+(?:good|clean|fine)(?:\s+to\s+me)?"
    r"|lgtm"
)
_NO_FINDINGS_RE = re.compile(
    rf"(?:{_JA_STATEMENT}|{_JA_BARE}|{_EN_STATEMENT}|{_EN_BARE})", re.IGNORECASE
)


def is_sentinel_line(line: str) -> bool:
    """装飾を除いた行が REVIEW_CLEAN そのものか (大文字小文字は区別しない)。"""
    return _SENTINEL_TRIM_RE.sub("", line).upper() == REVIEW_CLEAN


def is_no_findings_statement(line: str) -> bool:
    """「指摘なし」を述べる短い 1 文か。

    allow-list 方式: 行全体が否定文のパターンに完全一致し、かつ指摘本文の兆候
    (箇条書き・パス・強調・文区切り・逆接・長文) が無い行だけを True にする。
    """
    if _LIST_MARKER_RE.match(line):
        return False
    core = _EMPHASIS_TRIM_RE.sub("", line)
    if not core or len(core) > MAX_PREAMBLE_CHARS:
        return False
    if _FINDING_HINT_RE.search(core) or _SEPARATOR_RE.search(core) or _CAVEAT_RE.search(core):
        return False
    return _NO_FINDINGS_RE.fullmatch(core) is not None


def content_lines(text: str) -> list[str]:
    """フェンス行・装飾行・空行を除いた行を返す。

    フェンス行の info string が sentinel のとき (```REVIEW_CLEAN) は sentinel 行として残す。
    """
    lines: list[str] = []
    for raw in text.split("\n"):
        line = raw.strip()
        if not line:
            continue
        fence = _FENCE_RE.match(line)
        if fence:
            info = fence.group("info")
            if info and info.upper() == REVIEW_CLEAN:
                lines.append(info)
            continue
        if _DECORATION_RE.match(line):
            continue
        lines.append(line)
    return lines


def is_clean_review(text: str) -> bool:
    """REVIEW_CLEAN sentinel が「単独で」返されているときのみ True。

    LLM が REVIEW_CLEAN + 後続指摘を混在させた出力を clean 扱いして critical feedback を
    silently drop しないよう、sentinel 以外に許すのは「指摘なし」を述べる短い 1 文だけ
    (モジュール docstring の判定規則を参照)。
    """
    lines = content_lines(text)
    if not lines:
        # 空出力は clean (呼び出し側は空を「結果なし」として先に弾くので到達しない)。
        # フェンスや罫線だけの出力は sentinel を含まないので指摘扱いに倒す
        return not text.strip()
    others = [line for line in lines if not is_sentinel_line(line)]
    sentinel_count = len(lines) - len(others)
    if sentinel_count == 0:
        return False
    if not others:
        return True
    if sentinel_count == 1 and len(others) == 1:
        return is_no_findings_statement(others[0])
    return False
