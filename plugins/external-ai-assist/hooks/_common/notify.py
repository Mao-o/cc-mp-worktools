"""利用者向け通知 (`systemMessage`) の組み立て。

## なぜ stderr では足りないか

`hooklog` の stderr は **exit 0 の hook では debug log にしか出ない** (公式 docs)。
外部 AI レビューは 5〜25 分ブロックしうるのに、0.5.0 までは exitplan-review が
その間も完了時も何も表示しなかった。利用者から見ると「Claude が無言で固まった」
状態で、Esc (ターンごと中断) 以外の判断材料が無い。`systemMessage` はハーネスが
利用者に見せる共通フィールドなので、**所要時間と結果の要約**をここに出す。

## 何を書いてよいか (公開 plugin としての制約)

- 書いてよい: 所要時間、レビュアー名、件数、skip 理由、対象ファイル名
- **書いてはいけない**: レビュー本文そのもの、diff の中身、外部 AI の生出力

レビュー本文は `decision` / `reason` で Claude に返る (block 時) か、参照コピーの
ファイルに残る。`systemMessage` に混ぜると、ブロックしていないターンでも長文が
画面に出て通知として機能しなくなる。

`systemMessage` の配信は対話 UI 以外では未確認なので、各 hook は同じ内容を
`hooklog` にも書く (通知が出ない環境でも `claude --debug` で追える)。
"""
from __future__ import annotations


def format_elapsed(seconds: float) -> str:
    """所要時間を `4分12秒` / `45秒` の形にする (秒未満は切り捨て)。"""
    total = max(0, int(seconds))
    if total < 60:
        return f"{total}秒"
    return f"{total // 60}分{total % 60:02d}秒"


def compose(prefix: str, lines: list[str]) -> str | None:
    """`[prefix] line1\\nline2 ...` を作る。行が無ければ None (通知しない)。"""
    kept = [line for line in lines if line]
    if not kept:
        return None
    return f"[{prefix}] " + "\n".join(kept)
