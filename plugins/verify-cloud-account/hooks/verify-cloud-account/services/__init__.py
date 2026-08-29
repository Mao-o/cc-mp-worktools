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
     - ACCEPTS_DICT: bool           期待値に dict 形を許すか (全 service 必須)
     - DICT_ALLOWED_KEYS: frozenset[str]
                                    (任意) dict の許容キー。未宣言 = キー制限なし
     - DICT_VALUE_CHECK: str        (ACCEPTS_DICT=True のとき) verify() が dict の
                                    **値**をどこまで形で拒否するかの宣言。builder は
                                    migrate の緩和モードでこれに合わせて拒否する:
                                      "all"    全キーの値が str 必須 (github)
                                      "truthy" truthy な値だけ str 必須、falsy は
                                               verify() が無視する (gcloud)
                                      "none"   verify() が使えない値を黙って捨てる
                                               ので値の型では拒否しない (firebase)
                                    未宣言時の builder 既定は最も厳しい "all"
     - SCALAR_EQUIVALENT_DICT_KEY: str
                                    (任意) scalar 期待値と**等価**になる dict キー。
                                    verify() の str 分岐が照合する対象が特定の
                                    キーに対応するときだけ宣言する (github:
                                    "github.com" / gcloud: "project")。builder の
                                    migrate はこれで scalar↔dict の非損失性を判定し、
                                    未宣言なら混在を conflict (手動解決) に倒す。
                                    firebase のようにキーが verdict に効かない
                                    service では宣言しないこと (畳み込むと alias 名の
                                    情報が消える)
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
