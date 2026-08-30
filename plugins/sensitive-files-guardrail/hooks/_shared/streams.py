"""stdout へ **ストリームの encoding に依らず**書き出すための共有ヘルパー。

hook の判定 JSON は stdout に書く。``print`` / ``sys.stdout.write`` は
``sys.stdout`` の encoding に従うため、``PYTHONIOENCODING=ascii`` のように
非 UTF-8 の stdout で hook が spawn された環境では、日本語を含む reason で
``UnicodeEncodeError`` が送出される (``sys.stdout`` の error handler は
``strict``)。hook 側はこれを捕まえないので終了ステータス 1 で落ち、判定 JSON が
1 byte も出ない:

- PreToolUse (``redact-sensitive-reads``): 判定が届かず tool 呼出がそのまま
  通る (**fail-open** — deny したかった Bash / Read / Edit が実行される)
- Stop (``check-sensitive-files``): block が出ず、機密ファイルが報告されない

どちらも「保護が静かに消える」方向の失敗なので、encoding に依存しない経路で
書く必要がある (外部レビュー R2 P2-A)。

``json.dumps(..., ensure_ascii=True)`` で ASCII に逃がす手もあるが**採用しない**:
日本語 1 文字が ``\\uXXXX`` の 6 文字に膨らむため、``check-sensitive-files`` の
出力予算 (``MAX_OUTPUT_CHARS``) を固定の日本語案内だけで食い潰す。代わりに
直列化済みの文字列を UTF-8 bytes に変換し、テキスト層を迂回して
``sys.stdout.buffer`` に書く。

なお ``sys.stderr`` は CPython が既定で ``backslashreplace`` を使うため
(``PYTHONIOENCODING=ascii`` でも実測で ``errors='backslashreplace'``)、
非 ASCII の警告文を書いても送出されない。stderr 側にこのヘルパーは要らない。
"""
from __future__ import annotations

import sys


def write_stdout(text: str) -> None:
    """``text`` を UTF-8 bytes として stdout に書き、flush する。

    バイナリ層 (``sys.stdout.buffer``) があればそこへ直接書く。テストが
    ``StringIO`` へ差し替えている場合など ``buffer`` を持たないストリームでは
    テキスト書込みにフォールバックする (差し替え側は encoding を持たないので
    そもそも本件の失敗モードが起きない)。

    encode の ``errors="replace"`` は意図的: hook 入力 JSON は ``\\udXXX``
    のような lone surrogate を正当に含みうる (``json.loads`` はそのまま str に
    通す) ため、strict だとここで ``UnicodeEncodeError`` が起きて、修正しよう
    としている fail-open に逆戻りする。U+FFFD は JSON 文字列として妥当なので
    envelope 自体は壊れない。
    """
    buffer = getattr(sys.stdout, "buffer", None)
    if buffer is not None:
        buffer.write(text.encode("utf-8", errors="replace"))
        buffer.flush()
        return
    sys.stdout.write(text)
    sys.stdout.flush()
