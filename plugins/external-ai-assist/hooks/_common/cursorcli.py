"""cursor agent CLI の共通部分 (存在確認と review 用 argv)。

explore-parallel のバックグラウンド起動 (`-p`、結果はファイルへ) は待機方式が違うため
argv をここに寄せていない。review 系 2 hook (exitplan-review / post-implementation-review)
だけが `review_argv` を共有する。
"""
from .subproc import cli_available

NAME = "cursor"
BINARY = "cursor"


def is_available() -> bool:
    return cli_available(BINARY)


def review_argv(prompt: str) -> list[str]:
    """読み取り専用 (`--mode plan`) の print モードでプロンプトを 1 回実行する argv。

    `--trust` は workspace 信頼の確認ダイアログを省くためで、書込許可ではない
    (0.2.0 からの既定)。
    """
    return [BINARY, "agent", "--trust", "--print", "--mode", "plan", prompt]
