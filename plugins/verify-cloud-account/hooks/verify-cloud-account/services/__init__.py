"""登録済みサービス一覧。

新しいサービスを追加するには:
  1. services/<name>.py を作成し、以下を定義する:
     - PATTERNS: list[str]          コマンドマッチ用の正規表現
     - READONLY: list[str]          検証をスキップする読み取り専用コマンド
     - is_readonly(candidate) -> bool
                                    (任意) 正規表現で表せない readonly 判定
                                    (github: flag の実効 boolean を見る login 形)
     - STATE_CHANGING: list[str]    (任意) アカウント状態 (次のコマンドがどの
                                    アカウントで動くか) を変えうるコマンド。
                                    dispatcher が検出すると成功 cache を破棄し、
                                    そのコマンド自身の検証成功も cache しない
     - GLOBAL_OPTIONS_WITH_VALUE / GLOBAL_FLAGS: frozenset[str]
                                    (任意) CLI 名直後に置ける global option
                                    (`aws --profile prod sso login`)。dispatcher が
                                    剥がした形でも READONLY / STATE_CHANGING /
                                    self-remediation を判定する (core/cli_options.py)
     - ACCOUNT_KEY: str             accounts.local.json 上のキー名
     - SETUP_HINT: str              accounts.local.json 未設定時の案内文
     - verify(expected, project_dir) -> str | None  検証関数 (None=成功, 文字列=エラー理由)
     - get_active_account(project_dir) -> str | dict | None  現在のアクティブ値
     - suggest_accounts_entry(project_dir) -> str | dict | None  builder 書込用 suggestion
         (scalar/dict の形状は service 側の判断。取得不可は None)
     - github のみ: parse_active_accounts(text) -> dict[str, str]  (gh 出力パーサ)
  2. 下記 import と ALL リストに追加する。

get_active_account / suggest_accounts_entry は `scripts/accounts_builder.py`
から呼ばれる。副作用なく現在値を取得すること。
"""
from . import aws, firebase, gcloud, github, kubectl

ALL = [github, firebase, aws, gcloud, kubectl]
