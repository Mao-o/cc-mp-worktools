"""cursor agent CLI の共通部分 (存在確認と読み取り専用の起動 argv)。

3 hook (explore-parallel の並走調査 / exitplan-review / post-implementation-review) は
いずれも cursor に「読むだけ」を求めるので、起動 argv は `readonly_argv` に一本化する。
待機方式は hook ごとに違う (explore-parallel はバックグラウンド Popen + 結果ファイル、
review 系 2 hook は `subproc.run_for_output`) ため、ここで共有するのは argv だけ。
"""
from .subproc import cli_available

NAME = "cursor"
BINARY = "cursor"


def is_available() -> bool:
    return cli_available(BINARY)


def readonly_argv(prompt: str) -> list[str]:
    """読み取り専用 (`--mode plan`) の print モードでプロンプトを 1 回実行する argv。

    `--print` (`-p`) 単独は cursor-agent の help で「Has access to all tools, including write
    and shell」とされる書込可能モードで、`--mode plan` が「read-only/planning (no edits)」。
    0.4.0 までの explore-parallel は `-p` 単独だったため、調査の裏で作業ツリーを書き換えうる
    agent が走っていた (0.4.1 で修正)。`--trust` は workspace 信頼の確認ダイアログを省くためで、
    書込許可ではない (0.2.0 からの既定)。
    """
    return [BINARY, "agent", "--trust", "--print", "--mode", "plan", prompt]
