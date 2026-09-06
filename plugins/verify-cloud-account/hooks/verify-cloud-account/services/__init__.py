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
                                    verify() の str 分岐が照合する対象が
                                    **実行時の状態に依らず**特定のキーに固定される
                                    ときだけ宣言する (現状は gcloud の "project"
                                    だけ — scalar 分岐が `_check_project()` しか
                                    呼ばない)。builder の migrate はこれで
                                    scalar↔dict の非損失性を判定し、未宣言なら
                                    混在を**両方向とも** conflict (手動解決) に倒す。
                                    宣言してはいけない例:
                                      - firebase: dict キー (alias) が verify() の
                                        verdict に効かない (畳み込むと alias 名の
                                        情報が消える)
                                      - github: scalar 分岐が「github.com が active
                                        ならそれ、無ければ最初の active host」という
                                        動的な照合をするため、どの静的 hostname とも
                                        等価にならない
     - REMEDIATION_PATTERNS: tuple[str, ...]
                                    verify() が deny 理由で案内する remediation
                                    コマンドの**実形** (引数付きの正規表現。例:
                                    `gh auth switch --`)。dispatcher はこれが
                                    deny 文面に一致したときだけ「案内した
                                    コマンドは単独で実行せよ」の注記を足す。
                                    文言や語幹で判定するとインストール案内や
                                    診断文まで拾うため実形で宣言する。
                                    **全 service 必須** (契約テストが強制)
     - REMEDIATION_NOTE: str        (任意) 上の注記の文面を service 固有に
                                    差し替える。aws だけ remediation がインライン
                                    env (`AWS_PROFILE=<p>` を元のコマンド行頭に
                                    付ける) で「単独で実行」が成り立たないため
     - is_self_remediation(candidate, expected) -> bool
                                    (任意) 「候補セグメントが**期待値へ向かう**
                                    切替コマンドか」。dispatcher は全セグメントが
                                    これに該当するとき検証をスキップする
                                    (deny が案内した切替コマンド自身が deny
                                    される self-remediation loop を防ぐ)。
                                    期待値以外への切替では False を返すこと
     - verify(expected, project_dir, env=None, context=None) -> str | None
                                    検証関数 (None=成功, 文字列=エラー理由)。
                                    env はインライン環境変数をマージ済みの完全 env
                                    (None=親環境継承)、context は候補コマンドの
                                    context option (`--profile` / `--project` 等)。
                                    subprocess の timeout は直値ではなく
                                    `core.budget.call_timeout(<既定>)` を通すこと
                                    (hook 全体の実時間予算で頭打ちにするため)
     - matches(expected, current) -> bool
                                    (任意) 「CLI 実測値 current が期待値
                                    expected を満たすか」の述語。verify() と
                                    **同じ規則**を bool で返す実装を 1 つだけ
                                    置き、verify() 側もそれを使うこと。builder
                                    (`scripts/accounts_builder.py` の show) が
                                    [match]/[mismatch] の判定に使い、未宣言なら
                                    builder の汎用近似 (`_entries_equal`) に
                                    落ちる。**照合先が動的に決まる service は
                                    必ず宣言する** — github は「github.com が
                                    active ならそれ、無ければ最初の host」と
                                    照合するため、汎用近似 (最初の host) では
                                    show と hook の verdict がずれる。任意入力
                                    (accounts.local.json の生値) を受けるので
                                    例外を投げないこと
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
