# verify-cloud-account 実装者ガイド

**plugin の保守・拡張者向け**。利用者向けの概要・設定方法・判定表は
[README.md](../README.md) が正本。

## このドキュメントの方針

以前この内容は追跡されないローカルファイルにしか無く、外部の読者
(と worktree でチェックアウトした自分自身) から見えなかった。同時に
「テスト件数」「ディレクトリツリー」「キャッシュキーの構成要素」といった
**コードを直せば必ず陳腐化する記述**が更新されないまま残っていた。

そのため本ドキュメントは次の線引きで書く:

- **コードが正本のもの (属性の一覧・件数・定数値・分類表) はここに複製しない。**
  参照先のファイル名を書き、そこを読ませる
- ここに書くのは **規則と、その規則にした理由** (コードを読んでも復元できない部分)

## 目的

`PreToolUse:Bash` hook として動作し、Bash コマンドの実行前にクラウド CLI の
アクティブアカウントがプロジェクトの想定値と一致するかを検証する。不一致なら
deny し、切り替え手順を提示する。

複数の AWS / GCP / Firebase / GitHub / Kubernetes アカウントを切り替えて作業する
とき、間違ったアカウントで `gh pr create` / `firebase deploy` / `kubectl apply`
等を実行する事故を防ぐ。期待値は `accounts.local.json` に置き、hook 実行時に
CLI から現在値を取得して照合する。

## ディレクトリ構成

```
verify-cloud-account/
├── .claude-plugin/plugin.json
├── README.md                       利用者向け概要 (判定表の正本)
├── CHANGELOG.md
├── docs/
│   ├── DEVELOPMENT.md              このドキュメント
│   └── wrapper-env-audit.md        D16 の監査記録 (透過 wrapper × env 伝播)
├── skills/                         Agent Skill (Claude 向けプロンプト)
│   ├── accounts-init/SKILL.md
│   ├── accounts-show/SKILL.md
│   ├── accounts-migrate/SKILL.md
│   ├── auto-switch/SKILL.md        自動切替の有効化 / 無効化 (v0.16.1)
│   └── project-accounts/SKILL.md   aws / gcloud / firebase のプロジェクトごとの固定 (v0.17.0)
└── hooks/
    ├── hooks.json                  PreToolUse:Bash の単一エントリ
    └── verify-cloud-account/
        ├── __main__.py             エントリポイント (stdin → dispatch → stdout)
        ├── core/
        │   ├── auto_switch.py      不一致 deny を hook の切替で置き換える opt-in (自動切替)
        │   ├── budget.py           hook 1 回分の実時間予算
        │   ├── cache.py            検証成功の短期キャッシュ
        │   ├── cli_config.py      ローカル CLI 設定ファイルの読取 (最小 YAML / INI)
        │   ├── cli_options.py      CLI 名直後の global option 剥がし + context option 抽出
        │   ├── command_parser.py   コマンド分解 (chain split / env strip / wrapper strip)
        │   ├── dispatcher.py       サービス振り分けと検証オーケストレーション
        │   ├── mode.py             検証モード (enforce / warn / off) の解決
        │   ├── output.py           deny / warn の hookSpecificOutput JSON ビルダー
        │   ├── paths.py            accounts.local.json の配置パス解決 (3-tier + 親遡及 + グローバル既定)
        │   └── tiers.py            セグメントの tier 分類 (READONLY / QUERY / WRITE + DISCLOSING)
        ├── services/               サービスごとの CLI 呼び出しと照合
        ├── scripts/
        │   ├── accounts_builder.py accounts.local.json 専用 writer (init/show/set/remove/migrate)
        │   ├── pin_env.py          プロジェクトごとの固定の提案 (builder の pin-env。読み取り専用)
        │   └── templates/          プロジェクト側 signpost のテンプレート
        └── tests/                  unittest (標準ライブラリのみ)
```

### 実行フロー

1. `__main__.py` が stdin から hook input (`tool_input.command`, `cwd`) を読む
2. `core.dispatcher.dispatch()` を呼ぶ (ここで実時間予算を張る)
3. `core.command_parser.extract_candidates()` がコマンドを
   `(セグメント, インライン env)` のリストに分解する
4. 各セグメントをサービスにマッチング。readonly 除外・dedup・context option 抽出
5. `core.mode.from_env()` で検証モードを見る (`off` なら cache 破棄だけ行って終了)
6. `accounts.local.json` を解決して読み (プロジェクト側 → グローバル既定)、
   `"$mode"` を反映してから、サービスごとに `verify()` を実行
   (キャッシュ hit / 自己修復の切替はスキップ)。deny になる不一致で自動切替が
   有効なら、`core.auto_switch.attempt()` が切り替えて再検証する
7. `core.output.deny()` / `warn()` で整形して stdout に返す
   (`warn` モードでは deny 相当の本文を `warn()` 側に回す)

**起動コマンド**: `python3 "${CLAUDE_PLUGIN_ROOT}/hooks/verify-cloud-account"`
(ディレクトリを渡すと `__main__.py` が実行される)

Python 3.11+。標準ライブラリのみ (外部依存なし)。

## サービスモジュールの契約

**公開すべき属性・関数の一覧は `services/__init__.py` の冒頭 docstring が正本。**
ここに表を複製すると必ず片方が古くなるので置かない。以下は「なぜその規則か」だけ。

### `verify()` の実装規則

- **成功は `None`、失敗は「理由 + 解決手順」を 1 つの文字列で返す** (deny の
  reason にそのまま出るため、理由だけ返すとユーザーが次の一手を打てない)
- **例外を raise しない。** CLI 未インストール (`FileNotFoundError`)、実行不能
  (`OSError`)、timeout も文字列で返す。hook プロセスが異常終了すると JSON が
  出ず、公式仕様上は non-blocking error として**無音でコマンドが進む**
  (= fail-open)。ガードが消える方向の失敗なので、必ず捕まえて deny 文字列にする
- **subprocess の timeout は `core.budget.call_timeout(<既定>)` を通す。**
  直値を書くと残り予算を無視してフルの timeout を使い、複合コマンドで hook
  timeout を超える (下記「実時間の予算」)
- `expected` の型チェック (str / dict) は各 service 内で行う。dispatcher は
  `isinstance(entry, (str, dict))` だけ弾く (許容する形が service ごとに違うため)
- `env` は**そのまま `subprocess.run(env=)` に渡す**。マージ
  (`{**os.environ, **inline}`) は dispatcher 側で済んでいる
  (service → core の依存を作らないため)。内部の `_run_*` / `_get` にも貫通させる
- `project_dir` を使わないサービスも引数では受け取る (interface 統一)
- **ローカル設定ファイルから現在値を読む場合 (v0.13.0: github / gcloud) は、
  「allow だけをローカルで決め、エラー方向は必ず CLI で取り直す」**。
  誤読が deny を新造しない形にしておくと、未知の env 上書き・設定ファイル外の
  プロパティ (gcloud の installation 設定など) を取りこぼしても、コストは
  「CLI を 1 回呼ぶ」だけで済む。逆にローカル読取の結果で直接 deny すると、
  誤読がそのまま誤 deny になり remediation の手がかりも CLI の文面とずれる。
  読み取り側の実装 (`core.cli_config`) は**想定外の形を見たら必ず `None`** を
  返し、判読できたつもりで違う値を返さないこと
- **現在値の取得元を増やしたら、テストの隔離も増やす。** ローカル読取は
  `$HOME` / `$XDG_CONFIG_HOME` / `GH_CONFIG_DIR` / `CLOUDSDK_CONFIG` を見るため、
  隔離しないと**開発者の実環境が verdict を決めてしまう** (実際に、CLI を mock した
  テストが開発者の `hosts.yml` で allow に短絡して壊れた)。
  `tests/_testutil.start_isolation()` を `setUpModule()` から呼ぶ

### 照合規則が動的な service は `matches()` を公開する

`verify()` は deny 理由を組み立てる責務があるので戻り値が str になる。一方
builder の `show` は `[match]` / `[mismatch]` を出すのに bool が要る。**同じ規則を
2 箇所に実装すると必ずずれる。** 実例: gh が GitHub Enterprise を先に列挙する環境
で、`show` は「最初の host」と照合して `[mismatch]`、hook は `github.com` を優先
照合して allow、という乖離が出た (内部バックログ)。

照合先が動的に決まる service は `matches(expected, current) -> bool` を公開し、
`verify()` 側もその規則 (github なら `scalar_target_host()`) を使う。builder は
`matches()` があればそちらに委譲し、無ければ汎用近似に落ちる。

### `PATTERNS` / `READONLY` の規則

- **先頭アンカ `^` 必須。** `PATTERNS` は `re.search` で評価されるため、アンカ
  無しだと `echo "gh"` のような文字列でも発火する。dispatcher は分解後のセグメント
  単位でマッチするので、セグメント先頭のアンカが効く
- **ラッパ対応を PATTERNS に書かない。** `npx` / `mise exec --` 等の剥がしは
  `core.command_parser.strip_transparent_wrappers` に集約済みなので、各 service は
  `^<cli>(?=\s|$)` 相当のシンプルな形でよい。0.1.0 では Firebase だけが PATTERNS に
  wrapper を詰め込んでいたが、他 service にも必要になって破綻した
- **`\b` ではなく空白/終端で区切る。** `\b` はハイフンも語境界とみなすため、
  `gh-ost` / `kubectl-foo` / `aws-vault` のような**別コマンド**まで拾う
- **状態確認コマンドを必ず `READONLY` に入れる。** 入れ忘れると
  「accounts 未設定 → deny → 設定のため現在値を確認したい → deny」の
  デッドロックになる。新規サービス追加時の必須チェック項目
- **マッチ順序**: `dispatcher._match_service` は `services.ALL` を先頭から評価し、
  最初にマッチしたサービスを採用する。2 つのサービスで PATTERNS が競合する設計は
  避ける (曖昧なコマンドは各サービス側で除外するのが正解)

## tier 分類 (`core.tiers`) — v0.14.0

セグメントは 3 tier に分かれ、`DISCLOSING` が modifier として tier を取り消す。
**利用者向けの表は README の「対象コマンドと検証スキップ」が正本**で、ここには
「なぜその構造か」だけ書く。

| tier | 不一致時 | 例 |
|---|---|---|
| `READONLY` | 検証しない | `gh auth status` / `firebase use` (引数なし) |
| `QUERY` | allow + `additionalContext` 警告 | `gh pr list` / `aws s3 ls` / `kubectl get` |
| `WRITE` (既定) | deny | `gh pr create` / `firebase deploy` |
| `DISCLOSING` (modifier) | READONLY / QUERY を取り消して WRITE | `gh auth status --show-token` |

- **判定順は DISCLOSING → READONLY → QUERY → WRITE。** `aws sts get-session-token`
  のように QUERY と DISCLOSING の両方に当たる形があるため、順序自体を
  `tests/test_tiers.py` で固定する (順序を入れ替えると開示形が warn だけで通る)
- **緩める方向は QUERY の 1 箇所だけ。** だから `QUERY` に載せるのは「読むだけと
  証明できる形」に限る。regex で表せない形 (`gh api` は option 次第で write) は
  service の `is_query()` が**安全な option の allow-list** で証明する。危険な
  option の denylist にすると、CLI に option が増えるたび黙って穴が開く
- **`READONLY_SAFE_OPTIONS` は「option の allow-list」。** READONLY に載せた形でも
  宣言外の option が付いていたら QUERY に降格する (deny ではない)。「コマンド名が
  安全」では不十分で、option 次第で内容 / 認証情報リーダーに化ける形が繰り返し
  見つかっていたため (`--show-token` / `--raw` / `cluster-info dump`)
- **降格を deny にしない**のは lenient 方針との整合。降格は「検証を走らせて結果を
  伝える」だけなので、未知の option が付いた状態確認コマンドで新たな deny が
  生えることはない
- **認証取得系 (`gh auth login` / `aws sso login` / `gcloud auth login` /
  `firebase login`) には `READONLY_SAFE_OPTIONS` を宣言しない。** あれらは
  未ログインのデッドロックを解くためのエントリで、降格させると deny 文面が案内する
  コマンド自身が検証予算を使い始める
- **option の検出は `cli_options.find_option_names` に一本化する。** 分離形 / `=`
  形 / 短縮連結 (`-at` は `-a -t`) / 値 token の消費を 1 箇所で扱う。値の真偽は
  見ない (`--show-token=false` も「書かれている」と数える) — 真偽で緩めると
  `--skip-ssh-key=false` ですり抜けた穴を逆向きに作り直すことになる

### 切替 + 書込の連結 (`_unexpected_switch_before_write`) — v0.15.0

tier は**セグメント単位**の分類だが、hook が PreToolUse で 1 回しか動かないことに
由来する穴は**セグメントの並び**にある。`gh auth switch --user other && gh pr create`
は write が切替**前**の状態で検証され、切替前が期待値どおりなら allow される。
そこで dispatcher は出現順のセグメント列 (`_analyze_command` の第 3 戻り値) を
service ごとに走査し、次の並びを deny する:

| 切替側 (先) | 書込側 (後) | 判定 |
|---|---|---|
| その service の状態を変える (`STATE_CHANGING`) かつ `changes_identity` が True かつ `is_self_remediation` が False | 同 service の **WRITE tier** かつ `is_self_remediation` が False | **deny** |
| 期待値へ向かう切替 (`is_self_remediation` が True) | 同上 | 従来どおり (切替前の状態で通常検証) |
| identity を変えない状態変更 (`changes_identity` が False) | 同上 | 従来どおり |
| 任意 | QUERY / READONLY | 従来どおり |

**利用者向けの表は README の「切替と書込を同一コマンドに連結した形は deny」が正本。**
ここに書くのは設計理由だけ:

- **切替側は `STATE_CHANGING` 全体ではなく「identity を変える形」に絞る**
  (`changes_identity`)。`STATE_CHANGING` は成功キャッシュの破棄が目的なので
  「次のコマンドがどのアカウントで動くか」を変えない形も含んでいる。破棄が過剰でも
  再検証 1 回で済むが、**連結規則で過剰に数えると切替ですらない日常形に deny が
  生える** — `kubectl config set-context --current --namespace=foo && kubectl apply`
  (kubectl の最頻出 idiom) / `gcloud config set compute/region x && gcloud run deploy`
  / `gh auth refresh -s project && gh pr create` がそれで、いずれも identity は
  静的に見て不変。inert 側は **form 単位の allow-list** で宣言する
  (`set-credentials` は context 名を変えないまま identity を差し替えるので inert に
  してはいけない。`gh auth refresh` は `-u/--user` を受けるので subcommand 単位で
  inert 宣言してはいけない)。未宣言の service は既定 True = 従来どおり
- **`is_self_remediation` は装飾 option を剥がしてから照合する**
  (`core/cli_options.strip_allowed_options`)。0.14.0 までこの述語は「検証するか」を
  決めるだけで、False でも通常検証に落ちれば現在値が合っていれば allow だったため、
  anchored (`\s*$`) の厳しさが表に出なかった。v0.15.0 で**同じ述語が deny を抑止する
  唯一の出口**になったので、案内形に `--quiet` を 1 つ足しただけで deny になる。
  許容は「**着地先を変えない**」と言える option だけの allow-list
  (`--configuration` / `--kubeconfig` / `--config` / `--project` は着地先か照合先を
  変えるので入れない)。blanket な option 許容にはしない
- **判定に CLI を呼ばない** (cache / self-remediation / verify より**前**で deny する)。
  現在値が何であっても切替後の状態は検証できないので verify の結果で判定が変わる
  余地がなく、確実に deny になるコマンドで実時間予算 (`core/budget.py`) を使うと、
  同じコマンド行の他 service が予算切れ deny に落ち、最悪は hook timeout
  (= 出力が破棄されて無音で通る fail-open) に近づく
- **切替先が静的に判らない形は deny 側に倒す。** 引数なしの `gh auth switch`
  (インタラクティブ選択) / `--user $VAR` / login 系 / `is_self_remediation` を
  宣言しない service (aws) が該当する。「判らないから通す」は検証が消える方向の
  失敗で、`_is_expected_switch` の例外も同じ側に倒してある
  (`_changes_identity` の例外は逆向きの True = 同じく deny 側)。
  どちらの例外経路も `TestChainedSwitchAndWrite` に床のテストがある
- **deny 文面には期待値そのものを載せる** (`_expected_display`)。「切替を単独で実行
  してから」だけだと、検出コマンド行 (`gh auth switch --user other`) を単独実行する
  読みになる。それは allow されるので 2 往復かかり、しかもユーザーの gh 状態は期待外に
  移ったまま残る。**切替コマンドの実形は書かない** — この文面は
  `_guides_remediation` を通さない chain error なので、`REMEDIATION_PATTERNS` に
  一致する形を混ぜると「案内されたコマンドは単独で実行せよ」の注記の契約を汚す
- **書込側からも self-remediation を除く。** 除くと
  `firebase login && firebase use <期待 alias>` (deny 文面自身が案内する連結形) が
  deny になり、案内どおり打てなくなる。`firebase use <期待>` は WRITE tier だが
  実行後の状態は期待値なので、連結を止める理由が無い
- **QUERY は書込側に数えない** (`"$readonly": "deny"` でも)。あれは QUERY 不一致の
  扱いを戻す設定で、セグメントの tier を変える設定ではない。リモートを変えない
  コマンドが切替後に走っても資源は変わらないので、厳格化を連結された write だけに限る
- **同一セグメントが切替でも WRITE でもある形** (`gh auth switch --user other` 単体) を
  自己 flag しないよう、走査は「書込判定 → pending への追加」の順で行う。逆順にすると
  切替コマンド単体が常に deny になり、remediation の出口が消える
- 問題は `errors` に積む (tier ごとの `problems` ではない)。規則の発火条件が
  「同 service に WRITE tier のセグメントがある」ことなので、同 service の別 target
  (QUERY tier) を処理している最中でも止める側が正しい

### 何を deny のまま残すか

QUERY が緩めるのは「アカウントが期待値と違う」「期待値のキーが無い」「未設定」
「検証しきれなかった (予算切れ)」の 4 つだけ。**設定そのものが壊れている / 曖昧な
状態 (複数パス競合 / JSON 不正 / 値の型不正) は tier に関係なく deny** のまま:
「どの設定が効くか決まらない」状態では読むだけでも判定の土台が無く、かつ引数なしの
状態確認コマンドは READONLY なのでデッドロックにもならない。

`"$readonly": "deny"` (accounts.local.json の予約キー) で QUERY を WRITE と同じ扱い
に戻せる。`"$mode"` と同じ制約が付く — ファイルを読めたときだけ参加し、グローバル
既定に書いた場合は自前の accounts.local.json を持たないプロジェクトにしか効かない。

## コマンド分解 (`core.command_parser`)

`extract_candidates(command)` がコマンド文字列を `(候補セグメント, インライン env)`
のリストに変換する。具体的な入出力例は
`tests/test_command_parser.py` が網羅している (表を複製しない)。

規則と理由:

- **quote-aware 分割**: single/double quote・`$()`・backtick の内側にある
  `&&` / `;` / `|` / 改行は区切りにしない (手書き状態マシン)
- **subshell を含む代入は保守的に stop**: `FOO=$(date) cmd` は剥がすと意味が
  変わりうるため剥がさない
- **`env -i` / `env --` / `env -u NAME` は opaque**: 環境を書き換えるので剥がさない
  (= 静的解析対象外 → 検証スキップ)
- **透過 wrapper は「env を素通しする」ものだけ**: `ssh` / `docker` /
  `kubectl exec` のように**別の実行コンテキストへ移送する** wrapper は、ローカルの
  行頭 env が届かないので追加しない。`bash` / `sh` / `eval` / `python -c` も中身が
  script なので剥がさない
- **多段ネスト**: `sudo time mise exec -- firebase deploy` を 1 回の strip で
  解決するため、剥がせなくなるまでループする (上限あり)

wrapper ごとの env 伝播クラス (passthrough / scrub / opaque) の完全な表・実機根拠・
**wrapper を追加するときのチェックリスト**は
[docs/wrapper-env-audit.md](./wrapper-env-audit.md) が正本 (D16)。分類は
`core/command_parser.py` の `_WRAPPER_ENV_CLASS` で宣言し、
`tests/test_command_parser.py` のガードテストが未分類の追加を機械的に落とす。

## 短期キャッシュ (`core.cache`)

PreToolUse は Bash のたびに発火するので、`gh pr list && gh pr view && ...` の
連打で毎回 CLI を呼ぶとレイテンシが積み上がる (`aws sts get-caller-identity` は
1〜3 秒)。**検証成功だけ**を短時間キャッシュする。

- **キー**: サービス名 / project_dir / 期待値 / **インライン env** /
  **context option** (`--profile` 等) の JSON を sha256 したもの。
  構成要素は `cache._cache_key` が正本。
  インライン env と context を入れないと、`AWS_PROFILE=prod` の成功 entry を
  `AWS_PROFILE=other` の実行が hit して未検証のまま allow される
  (`os.environ` 全体は入れない — `PATH` 等で毎回無効化されて意味を失う)
- **identity env (v0.17.0)**: hook プロセスの env のうち、CLI がどのアカウントで
  動くかを決める変数もキーに入れる (`cache.identity_env`。何を入れるかは各 service の
  `IDENTITY_ENV_VARS` / `IDENTITY_ENV_PREFIXES`)。プロジェクトごとの固定
  (settings の `env` の `AWS_PROFILE` 等) は保存した時点で起動中のセッションに反映
  されるので、入れないと値を変えた直後の TTL の間は前の値での成功で通る。同じ
  リポジトリを固定したセッションと固定していないセッションも entry を共有しない。
  snapshot は検証に渡すのと同じ env (hook プロセスの env + インライン env) から取り、
  読む側と書く側で同じものを使う。値は hash の材料にだけ使い、cache ファイルには
  書かない
- **成功のみ**: 失敗 (文字列返却) は常に再検証する。切り替え直後に使いたいため
- **無効化**: TTL 超過 / `accounts.local.json` の mtime 変化 / 破損・欠損 /
  アカウント状態を変えうるコマンドの検出 / epoch 不一致
- **書き込み失敗は無視** (best-effort)。キャッシュ書けないことで deny は出さない

意図的にしないこと: 失敗のキャッシュ (切替直後に再検証したい) / 長時間キャッシュ
(他端末での切替が反映されない) / プロセス内キャッシュ (hook は毎回別プロセス)。

並行 hook との競合 (epoch + in-flight 窓) の仕様は README のパフォーマンス節と
`core/cache.py` の docstring を参照。

## 実時間の予算 (`core.budget`)

hook は `hooks/hooks.json` の `timeout` を超えると Claude Code 側で打ち切られ、
**出力が破棄されて tool call がそのまま進む** (公式仕様上の fail-open)。個々の
subprocess timeout は 1 コマンドあたりの上限でしかないので、複合コマンドで
サービスを直列に検証すると合計が hook timeout を超えうる。CLI 未検出も CLI
timeout も deny に倒している以上、ここだけ無音で通るのは判定表の穴になる。

- `dispatch()` が `budget.start()` で締切を置き、`finally` で必ず解除する
  (解除しないと builder 側の subprocess timeout まで黙って縮む)
- 各 service は `budget.call_timeout(<既定>)` で「残り予算」に丸めた timeout を使う
- 予算を使い切ったサービスは **CLI を呼ばずに deny** に集約する。判定は CLI 起動の
  直前に置く — cache hit と自己修復の切替は時間を使わないので通す
- 「予算 + 超過見積り < hook timeout」は `tests/test_budget.py` が `hooks.json` を
  読んで機械的に照合する。**定数を動かすならこのテストが不等式を検算する**

**並列化 (ThreadPoolExecutor) を採らなかった理由**: `concurrent.futures` の worker
は非 daemon スレッドで、インタプリタ終了時に atexit で join される。ハングした
subprocess を抱えたまま「予算切れ」を返してもプロセスが終了できず、結局 hook
timeout に落ちる。fail-open を塞ぐ目的には締切の伝播で足りる。

## 配置パスの解決 (`core.paths`)

3-tier lookup (推奨 / deprecated / legacy) と親ディレクトリ遡及を担当する。
利用者向けの説明は README、判断の背景は下記 D4。

- **複数 tier が同一階層に同居したら fail-closed で deny** (D4)。どれが正本か
  曖昧なまま検証を通すと、どの設定が効いているか不透明になる
- **親遡及**は「worktree に accounts.local.json を複製せず親 repo の設定を継承する」
  ための経路。cwd 階層で 1 つでも見つかればそこで採用 (cwd 優先)
- 遡及の停止条件は **階層数 + git repo の境界 + `$HOME`**。階層数だけを上限に
  すると `<home>/dev/<org>/<repo>` のような配置で `$HOME` に届き、無関係な設定を
  継承する (しかも verify 成功時は継承注釈が出ないので気付けない)
- **git repo の境界は `.git` の種別で決める** (`_is_repo_boundary`)。`.git`
  ディレクトリ = toplevel は境界。`.git` ファイル (gitdir ポインタ) は内容で分岐し、
  `<common>/worktrees/<name>` を指す linked worktree **だけ**が境界にならず親 repo
  まで上れる。`<common>/modules/<name>` を指す submodule root は境界 — ファイル形を
  一律に通過扱いにすると submodule から superproject の設定を継承し、**未設定の
  submodule で状態変更コマンドが repo 境界で fail-closed せず allow される**。
  判読できない `.git` ファイル (prefix 違い / common directory を直接指す形 /
  読み取り失敗) も境界に倒す。「継承先が増える方向」は allow 側なので、分からない
  ときは止める
- **linked worktree の判別は gitdir 側のメタデータで確かめる** — 末尾 2 要素
  (`worktrees/<name>` / `modules/<name>`) は**予備分類**にとどめ、`<common>`
  (git の common directory) の名前が `.git` であることには依存しない。bare
  repository から作った worktree は `repo.git/worktrees/<name>`、
  `--separate-git-dir` で初期化した repo から作った worktree は
  `/custom/gitdir/worktrees/<name>` になり、**パス中に `.git` という要素が
  現れない**。`.git` を厳密に要求すると、これらの正当な worktree が判読不能 =
  境界に落ち、その repo の accounts.local.json を継承できず設定済みの状態変更
  コマンドが deny される (マージ前レビューの指摘)。
  一方で**末尾だけで確定させると逆方向に穴が開く** — `--separate-git-dir` で
  初期化した独立 repo の gitdir が偶然 `worktrees/<name>` で終わる場合
  (`/store/worktrees/repo` など) にその main checkout を worktree と誤分類し、
  accounts.local.json を持つ workspace の配下に (間に `.git` を挟まず) 置かれて
  いれば所属確認も通って**独立 repo の root を越えて継承**する
  (マージ前レビューの指摘)。そこで `_linked_worktree_common()` が
  「gitdir が実在するディレクトリ」「`<gitdir>/gitdir` (back-pointer) が
  いま読んでいる `<directory>/.git` を指す」「`<gitdir>/commondir` が読める」の
  3 つを確かめ、1 つでも欠ければ "plain" (境界) に落とす。**common directory は
  `commondir` の内容から解決する** — 末尾 2 要素を落とす推定より、git 自身が
  書いた値のほうが信頼できる (`_ancestor_repo_owns` の比較にもこちらを使う)。
  予備分類の入れ子の扱いは従来どおり — `modules/a/modules/b` は境界、
  `modules/sub/worktrees/wt` は通過。`--separate-git-dir` repo の **main**
  worktree は gitdir が common directory を直接指す (`worktrees/` が付かない)
  ため、これまでどおり境界 = repo toplevel として扱われる
- **linked worktree が通過するのは、gitdir の common dir が祖先の repo のものと
  一致する場合だけ** (`_ancestor_repo_owns`)。形だけで通すと、無関係な repo A の
  中に置かれた repo B の worktree (`repo-a/vendor/b-wt`) から探索が repo B を
  離れ、**repo A の accounts.local.json を継承**する。repo A の期待アカウントが
  active session と一致すれば、未設定の repo B worktree で状態変更コマンドが
  allow される (マージ前レビューの指摘)。所属確認は探索と同じ方向 (親方向) へ
  同じ停止条件 (`$HOME` / ルート / 階層数) で走査し、**最初に見付かった git
  marker** で判定する — それより上は「その repo の中」であり、間の階層もその repo
  に属するため。祖先の種別を確定できない場合は止める側に倒す。祖先に repo が
  1 つも無い場合 (workspace 直下に worktree を並べる配置) は継承元を取り違え
  ようがないため従来どおり上る。比較は「同じ repo に属するか」を common dir 同士で
  見る形にしてあり、`.git` ディレクトリ / submodule / `--separate-git-dir` /
  祖先自身が linked worktree のいずれでも同じ 1 実装で判定できる
  (`_common_git_dir`)。パスは `Path.resolve()` で正規化してから比較する
- 判定は `.git` と gitdir 内メタファイル (`commondir` / `gitdir`) の**読み取りのみ**
  で行う (git コマンドは呼ばない)。gitdir は種別と所属の判定にしか使わず**探索先
  としては辿らない** — 探索経路を増やすと「見つかる場所が増える」= allow 側に
  倒れる。区切りは `/` と `\` の両方を受けて OS 非依存に分解し、ドライブ文字
  (`C:/...`) や UNC (`//server/...`) は絶対パスとして扱う (相対として繋ぐと
  別の common dir と比較してしまう)
- **builder も dispatcher と同じ解決を使う** (3-tier lookup + 親遡及)。読む側と
  書く側で解決がずれると、継承中の worktree で `set` が子ファイルを作り、
  dispatcher の遡及がそこで止まって**継承していた他の service が一斉に未設定
  (deny)** になる。書込先は「hook が読むファイル」に従い、対象は出力の
  `対象:` 行に必ず出る (この階層専用にしたいときだけ `--path`)
- **グローバル既定 (v0.13.0) は遡及ではなく固定パスの専用経路**
  (`resolve_accounts_file_for_verification`)。プロジェクト側で何も見つからない
  ときだけ `$HOME/.claude/verify-cloud-account/accounts.local.json` を読む。
  遡及で `$HOME` まで上らせると「たまたま `$HOME` 配下にあるプロジェクトだけが
  継承する」位置依存の挙動に戻ってしまう。認めるのは現行パスのみ (旧名を認めると
  塞いだ「無関係な `~/.claude/accounts.json` の継承」を復活させる)。
  **この関数は dispatcher 専用** — builder (書込先の決定) が使うと、プロジェクト
  設定を作るつもりの編集が利用者の全プロジェクトに効くファイルを書き換える

## 検証モード (`core.mode`)

`enforce` / `warn` / `off` の 3 モード。**判定表 (何を問題とみなすか) は mode で
変わらない** — 変えるのは「deny で止めるか / `additionalContext` で伝えるだけか /
検証そのものをしないか」だけ。解決順は env (`VERIFY_CLOUD_ACCOUNT_MODE`) →
`accounts.local.json` の `"$mode"` → `enforce` で、**既定 (どちらも無い) の挙動は
従来と完全に同じ**。

- env を上に置くのは「ファイルを書き換えずに一時的に外せる」ことが escape hatch の
  要件だから。`"$mode"` を下に置くのは、プロジェクトの設定より今のセッションの
  指示を優先したいから
- 不正な値は **enforce に倒す** (fail-closed) が、`invalid_note` を deny / warn の
  文面に添える。黙って enforce に戻すと「off にしたのに deny される」の原因が
  分からない
- `off` の early return は **cache 破棄 (`cache.invalidate`) より後**に置く。
  前に出すと off の間の切替が cache に残り、enforce へ戻した直後に古い成功で通る
- deny 側には mode の案内を 1 行添える (`mode.DENY_HINT`)。deny を消したい相手に
  builder の `set --from-cli --commit` を勧めると「間違ったアカウントを正解として
  焼き付ける」使い方を誘発するため、**期待値に触らない出口**を先に見せる

## 自動切替 (`core.auto_switch`) — v0.16.0

opt-in (`VERIFY_CLOUD_ACCOUNT_AUTO_SWITCH` → `"$auto_switch"` → 無効) で、deny に
なる不一致を「hook が期待値へ切り替えて再検証し、一致したら通す」に置き換える。
利用者向けの条件表は README の「自動切替」節が正本。実装の規則:

- **呼ぶのは dispatcher が「止める」と決めた target だけ** (`stops` = errors 行き、
  かつ mode が enforce)。QUERY の警告や warn モードでは呼ばない — 切替はマシン全体に
  効く副作用なので、「deny を置き換える」場面に閉じる。判定表 (何を不一致とみなすか)
  は変えない
- 連結規則 / self-remediation / cache hit / 予算切れは**すべて verify() より前**に
  決まるので、自動切替はそれらの後ろ (verify() が不一致を返した後) にしか入らない
- `attempt()` の段の並び: コマンド自身の状態変更 (`switching_here`) → 予算 →
  `plan_switch` → ガード → 予算 → `cache.invalidate` → `apply_switch` → 記録 →
  予算 → 再検証。**CLI を起動する前ごとに予算を確かめる**のは、`budget.worst_case_seconds`
  が「最後の確認の後に起動する呼び出し数」を前提にしているため (切替経路は計画の
  `gh auth status`・host ごとの `gh auth switch`・再検証で、verify() の後に CLI を
  3 回以上呼びうる)
- **計画は CLI (`gh auth status`) から取る**。ローカル読取 (`hosts.yml`) は allow
  にしか使わない方針 (`services/github.verify` の docstring) で、ここは状態を書き換える
  前の確認なので gh 自身の報告に拠る。呼ばれるのは deny になる不一致のときだけ
- **all-or-nothing**: dict 期待値で 1 host でも切り替えられなければ何も切り替えない。
  一部だけ切り替えて結局 deny、は「副作用だけ残る」最悪の形
- **cache は切り替える前に破棄する**。後に回すと、切替と並行した他プロジェクトの
  検証が旧状態の成功を書く窓が開く (`invalidate` の tombstone で、in-flight 窓の間は
  自分の検証成功も publish されない)
- **ガードは切替先の値で照合する** (プロジェクトのパスではない)。同じ repo の
  worktree 同士は同じ期待値を持つので、そこで見送る理由が無い。記録は
  `<service>.autoswitch.json` (成功 cache の glob `<service>-*.json` に掛からない名前。
  `cache.service_state_path` が `-` 始まりの suffix を拒否する)
- 注記・通知の文面に **CLI コマンドの実形を書かない**。deny 文面の案内は verify() の
  分だけに保つ (`_guides_remediation` と TestRemediationGuidanceContract が文面から
  コマンドを拾う)
- 自動切替の内部例外は dispatcher (`_auto_switch`) で握り、「切り替えなかった」扱いで
  **deny を残す**。握らずに `__main__` まで上げると内部エラー = fail-open になり、
  deny を置き換える補助が deny そのものを消す
- 通知 (`auto_switch.notice`) は allow でも必ず返す (`additionalContext`)。同じ行の
  別 service が deny した場合も deny 文面に添える
- **設定の入口 (v0.16.1)**: `"$auto_switch"` は builder の `auto-switch` サブコマンドが
  書く (予約キーのうち builder が書くのはこれだけ。builder の docstring の D15)。
  値の解釈は hook と同じ `auto_switch.from_accounts` に委ねるので、builder が「有効」と
  書いたものを hook が「不正」と読む食い違いは起きない。Claude が頼まれたときの手順は
  `skills/auto-switch/SKILL.md` (D30)

## サービスを追加する

1. `services/<name>.py` を作り、`services/__init__.py` の docstring にある契約を実装
2. `services/__init__.py` の import と `ALL` に追加する
3. README の対応表と `accounts.local.json` のサンプルに新しいキーを追記
4. `tests/test_services.py` にテストを追加する。最低限:
   一致 / 不一致 / CLI 未インストール / timeout / 状態確認コマンドが readonly
5. `tests/test_budget.py` の service 横断テストが自動で新 service も見るので、
   subprocess の timeout が `budget.call_timeout()` 経由か確認する
6. `IDENTITY_ENV_VARS` / `IDENTITY_ENV_PREFIXES` に、その CLI のアカウント・認証・
   設定の場所を決める環境変数を宣言する (`tests/test_services.py` の
   `TestIdentityEnvContract` が全 service に強制する)。漏れると成功 cache が env の
   変化を見落とす。CLI にプロジェクトごとの公式の固定方法があれば、自動切替ではなく
   その方法を README の「プロジェクトごとにアカウントを固定する」節と `pin-env` で
   案内する (D31)

動的ディスカバリではなく**明示 import** にしているのは、IDE 補完・型チェッカが
効き、import エラーが沈黙せず surface し、`ALL` への登録漏れがレビューで見えるため。

## hook 登録

plugin として install すれば `hooks/hooks.json` が自動適用される。利用者が
`settings.json` を手で編集する必要はない。

- `matcher: "Bash"` のみ。**`if` フィールドは使わない** — 一部の hook ランナーが
  `if` を無視して全 Bash コマンドに発火し、無関係なコマンドを誤 deny した事故が
  あった。振り分けは常にスクリプト内部 (`dispatcher._match_service`) で行い、
  ランタイム差異に依存しない
- `timeout` は**秒単位** (公式 hooks 仕様)。過去にミリ秒と誤記して 1000 倍の
  スケールずれを起こした実績があるので注意
- hook timeout は内部の実時間予算より数秒長く取る (`core/budget.py` の
  `worst_case_seconds()` との不等式をテストが検算する)

## テスト

```bash
cd hooks/verify-cloud-account
python3 -m unittest discover tests
```

標準ライブラリのみで動く (pytest / `pip install` 不要)。**件数はここに書かない** —
上のコマンドの出力を正とする。クラス・メソッド単位の実行方法は README を参照。

### スモーク (stdin に hook input JSON を流す)

```bash
cd plugins/verify-cloud-account

# 対象外 → 出力なし、exit 0
echo '{"tool_input":{"command":"git status"},"cwd":"/tmp"}' \
  | python3 hooks/verify-cloud-account

# 対象 + accounts 未設定 → deny JSON
echo '{"tool_input":{"command":"gh pr list"},"cwd":"/tmp"}' \
  | python3 hooks/verify-cloud-account

# readonly → 出力なし、exit 0
echo '{"tool_input":{"command":"gh auth status"},"cwd":"/tmp"}' \
  | python3 hooks/verify-cloud-account
```

`VERIFY_CLOUD_ACCOUNT_DEBUG=1` を付けると、セグメント分解・マッチしたサービス・
readonly 判定・cache hit・verify 所要 ms・最終判定を stderr に 1 行 JSON で出す。

### E2E

```bash
claude --plugin-dir plugins/verify-cloud-account
```

実セッションで `gh pr list` / `firebase use` / `kubectl get pod` 等を叩き、
accounts.local.json の有無・一致/不一致・チェーン・ラッパで挙動を確認する。

## 設計判断の履歴

### 初期 (0.1.0〜0.2.0)

- **Python 採用** — bash では正規表現と JSON パースが煩雑で保守性が低い
- **サービスごとのモジュール分離** — CLI・出力パース・エラーメッセージがサービス
  ごとに違い、JSON 設定で汎用化しようとすると破綻する (各 CLI の出力形式が
  標準化されていない)
- **明示 import (動的ディスカバリ不採用)** — 補完とデバッグ性を優先。追加時の
  2 行コストは許容範囲
- **hook を 1 エントリに集約 (`if` 廃止)** — ランナー差異による誤発火事故を受けて、
  振り分けをスクリプト内部へ一元化
- **コマンド分解を dispatcher に集約 (0.2.0)** — service ごとの wrapper 対応が
  露出しきれなくなったため `core.command_parser` を新設
- **`expected` を str | dict に (0.2.0)** — GHE の host 別 / Firebase alias /
  GCP account 対応。dispatcher は型を事前判別せず各 service に委ねる
- **成功のみキャッシュ (0.2.0)** — 失敗もキャッシュすると切替後の即時検証ができない

### 0.3.0 (builder + 配置パス移行)

**D1: builder を唯一の正規経路にする**

`accounts.local.json` の編集は `scripts/accounts_builder.py` 経由に統一する。
書込先パスの固定 (D2) / JSON のインデント・改行・ソート順 / 既存キーの温存 /
CLI 現在値との突合 (show) / 旧パス統合 (D5) / 値表示の制御 (D3) を builder 側で
一元管理するため。Claude は Read / Write / Edit / `cat` で直接触らず、Agent Skill
経由で builder を呼ぶ。

**D2: builder の書込制約 (responsibility confinement)**

builder は `accounts.local.json` 1 ファイル専用の writer。書込対象パスは
`core/paths.py` の定数に集約し、**argv からは指定できない**。basename が
`accounts.local.json` であることを assertion し、テストで「異常な出力パスが
拒否される」ことを固定する (将来の拡張で書込対象が広がらない保証)。

**D3: 値表示の制御**

builder の stdout は Claude が読む。アカウント ID / project 名 / user 名は通常の
確認フローではノイズなので、既定は隠し `--show-values` でのみ出す。Agent Skill は
「値なし dry-run → 必要なら承認を取って `--show-values` → 最終承認後 `--commit`」の
フローを指示する。

**D4: 新旧パス競合時の fail-closed**

2 つ以上の tier にファイルがあれば deny + 手動解決要求。どれが正本か曖昧なまま
検証を通すと、どの設定が効いているか不透明になる。

**D5: builder の migrate サブコマンド**

旧パス → 新パスの統合を builder が提供する。新パス優先で旧パスの追加キーをマージし、
値衝突時は自動マージせず deny + 手動解決要求。`--commit` でも旧ファイルは自動削除
しない (安全側)。

### 0.3.1 (プロジェクト側 signpost の同梱)

**D6: signpost をテンプレートファイルに切り出す** — 文言更新を builder のロジック
変更と分離し、レビュー差分を読みやすくする。

**D7: action に依存しない signpost 生成** — `init --commit` の signpost 生成は
add / unchanged / skipped に依存しない。既に accounts.local.json だけ持っている
ユーザーが後から signpost を入れ直せる経路を残すため。

**D8: dry-run では生成しない** — dry-run と commit の I/O 影響境界を一致させる。

**D9: plugin 同士を疎結合に保つ** — `*.local.json` の読み書き制限は別 plugin の
所掌。こちらは signpost で回避経路を案内するに留め、**相手側の deny 文言や許可
パターンに本 plugin の知識を入れない**。これは marketplace 全体の設計原則。

**D10: signpost は best-effort** — テンプレート読み込みや書き込みが失敗しても
warning 1 行で builder 自体は成功させる。signpost が無くても hook 本体は動くので、
read-only volume や quota 超過でアカウント登録までブロックしない。

### 0.7.0 (インライン env の伝播 + 診断性)

**D11: インライン env を検証 subprocess に伝播する**

`AWS_PROFILE=prod aws ...` のように行頭 env で profile を切り替える運用で、
「剥がすが使わない」非対称のせいでログイン済みでも既定 profile で検証が失敗し
永久 deny していた。

- **全伝播 (allow-list にしない)** — 必要な env キーは service ごとに違い、
  allow-list の漏れが別の永久 deny を生む。hook はコマンド実行の直前なので
  「実行時と同条件で検証」が原則
- **静的解決のみ** — 値に未展開の `$VAR` を含む env は剥がすが伝播しない。
  誤った値を渡すより素の環境の方が安全
- **マージは dispatcher に一元化** — service は受け取った env をそのまま渡す。
  インラインが空なら `env=None` (親環境継承) にする (空 dict は環境変数皆無になる)
- **cache キーに含める** — profile が違えば別キー (上記キャッシュ節)

**D12: deny の出所明示** — `output.deny()` の 1 箇所で全 deny の先頭にこの hook
由来である旨のタグを付ける。CLI 本体の生エラーと誤認され、CLI レベルの切り分けに
時間を浪費するのを防ぐ。

**D13: 情報系コマンドの readonly 化** — 全 service の `--version` / `--help` /
`version` / `help` を READONLY に入れる。診断コマンドが誤検証で deny されるのは
全 service 共通の穴だったため一括対応。

**D14: 検出セグメントの併記** — verify 失敗の deny reason に
`(検出コマンド: ...)` を付け、複合コマンドのどのセグメントが検証を起動したかを
特定できるようにする。

**D15: direnv / `CLAUDE_ENV_FILE` は対応しない** — その env は PreToolUse hook に
渡らない (harness の仕様)。hook 側で `.envrc` を評価する案は技術的には可能だが
採らない: direnv 専用の部分対応は「direnv なら通るが他のツールでは通らない」新しい
非対称を生む / 読み取り検証 hook が任意コードを実行する責務拡大になる / 外部ツール
依存が増える。回避策 (インライン env [D11] / settings.json の env / 起動時 env) を
README に明記する方針。

### 0.7.2 (透過 wrapper × env の監査)

**D16: 透過 wrapper の env 伝播クラスを分類し、ガードで固定する**

D11 は「静的に解析した行頭 env = 実行時 env」を前提にするが、透過 wrapper を跨ぐと
この前提が崩れ、env の edge case を連続して生んだ。全数監査の結論は
「現在のリストは健全、検証ロジックは無変更」。再設計はしない (過剰な allow-list 化は
誤 deny を増やす) 代わりに、分類を `_WRAPPER_ENV_CLASS` で宣言し、テストで将来の
wrapper 追加時に分類を機械的に強制する。完全な表・実機根拠・追加チェックリストは
[docs/wrapper-env-audit.md](./wrapper-env-audit.md)。

### 0.12.0 (docs と実装の整合 + 予算)

**D17: キー未記載サービスは deny (fail-closed) で確定させる**

「そのプロジェクトで使うサービスなのにキーが無い」は「検証しなくてよい」ではなく
「期待値を宣言し忘れている」状態で、素通しするとこの plugin が防ぐはずの事故
(別アカウントでの write) がそのまま通る。README と deny 文面はかつて「未記載の
サービスは検証対象外 (= allow)」と逆を約束していたので、**実装 (deny) を正として
文面を揃え**、キー追加の具体コマンドを deny に載せた。

案内が `init` ではなく `set` なのは、キー未記載の deny が「accounts.local.json は
見つかっている」ときにしか出ないため。そのファイルが親から継承されている場合
`init` は「継承中の設定を覆い隠す」として拒否するが、`set` は継承元を直接編集する
のでどちらの階層でも通る。

**D18: 判定規則は service 側の 1 実装に寄せる** — 上記「照合規則が動的な service は
`matches()` を公開する」を参照。

**D19: 遡及の停止条件に境界を足す** — 上記「配置パスの解決」を参照。落とす方向
(見つからず deny) は fail-closed なので安全側。`$HOME` や repo より上にグローバル
既定を置く用途の専用経路は現時点では無い (別途検討)。`.git` ファイルは種別で
分岐し、linked worktree だけ通して submodule と判読不能は境界にする。その
linked worktree も、**gitdir の common dir が祖先の repo のものと一致すること**を
確かめてから通す (いずれもマージ前レビューの指摘)。

**D20: 予算切れは deny** — 上記「実時間の予算」を参照。

### 0.13.0 (離脱率低減: ローカル読取 + escape hatch)

**D21: 現在値はローカル設定ファイルから読むが、allow だけを決めさせる**

`gh auth status` (API 往復 〜500ms、オフラインで失敗 → deny) と
`gcloud config get-value` (CLI 起動込み 〜1s ×最大 2 回) を、cache が無いときの
毎回のコストとして払っていた。どちらもアクティブアカウントはローカル設定ファイル
(`hosts.yml` の `<host>.user` / `configurations/config_<name>` の `[core]`) に
書かれているので、そこから読む経路を足した。

**エラー方向は必ず CLI で取り直す**のが要点 (上記「`verify()` の実装規則」)。
env 上書きや設定ファイル外のプロパティを取りこぼしても、誤読のコストは「CLI を
1 回呼ぶ」だけで、deny 文面と判定は従来どおり CLI の出力から作られる。
env の優先順位をエミュレートせず「触られていたら CLI に委ねる」に倒したのも同じ
理由 (gh の token env / `GH_HOST`、gcloud の `CLOUDSDK_*` / `GOOGLE_CLOUD_PROJECT`)。

**`HOME` も同じ扱い** (`cli_config.home_overridden()`)。`HOME` は gh / gcloud の
どちらも設定ディレクトリ解決に使うため、`HOME=<other> gh ...` の形では実行される
CLI が別のファイルを読む。ここだけは「エミュレートしない」が **false allow を
作りうる**側だった (hook 側のファイルが期待値と一致すると CLI を呼ばずに allow =
ローカル読取導入前は deny だった形の退行) ので、bail 条件として明示した
(マージ前レビューの指摘)。`GH_CONFIG_DIR` / `CLOUDSDK_CONFIG` が明示されていて
`HOME` が効かない場合も区別せず bail する — 判断を単純に保つ側に倒し、代償は
CLI 1 回。

gh 側の env 列挙は**閉じた allowlist** なので、gcloud の prefix denylist と違い
将来の追加を自動では拾えない。`_TOKEN_ENV_VARS` の隣に公式 env 一覧の URL と
「増えたらここに足す」根拠を置いてあるが、**機械検出はできない** (gh の major
update 時に読み直すのが唯一の担保)。閉じた列挙を `GH_*` 全面 bail に変える案は、
`GH_PAGER` のような無害な変数で高速化が消えるため採らなかった。

残った差分は「`hosts.yml` のアクティブアカウントのトークンが失効している」場合に
従来 deny だったものが allow になること (README 既知の制限)。失効トークンでは
write 自体が通らないため、別アカウントでの書き込みにはならない。

**D22: escape hatch は「いつ走らせるか」の層として足す (判定表は変えない)**

`enforce` / `warn` / `off` (上記「検証モード」) と、グローバル既定
(上記「配置パスの解決」) の 2 つで、「user scope で install した直後に設定して
いない全プロジェクトで deny が始まる」状態からの出口を作った。deny / allow の
規則そのものには手を入れていない。

適用範囲の書き方には注意が要る (マージ前レビューの指摘で 2 点直した):

- グローバル既定の `"$mode"` が効くのは **`accounts.local.json` を持たない
  プロジェクトだけ**。「全プロジェクトの既定」と書くと、設定済みで不一致 deny が
  出ている人 (= まさに困っている母集団) に効くと読めてしまう
- `"$mode"` は**ファイルを読めたときだけ**参加する。未設定 / JSON 破損 /
  複数パス競合の deny は `pre_file_mode` (env のみ) で決まる

**読む側と書く側で解決が食い違うと shadowing が起きる**という v0.12.0 の defect
(親遡及) は、グローバル既定の導入で 1 段上に再発した: dispatcher は
`resolve_accounts_file_for_verification()` でグローバル既定に落ちるが、builder は
落ちない (書込先がグローバルに化けるのを防ぐため意図的)。設計としてはこのままで、
**食い違いを黙らせない**方向で閉じた — 新規作成になるときは shadowing 警告を出し
(`_global_default_note()`)、`show` は「プロジェクトに無い」ときグローバル既定の
存在と「hook はこのファイルで検証します」を出す。キー単位マージにする案は判定表
(どのキーが未設定か) への影響が大きいので採らない。

**D23: ローカルを読む機能はテストの隔離を必ず伴う**

現在値の取得元が実環境 (`$HOME` / `~/.config`) に広がると、CLI を mock した
テストが開発者の設定で短絡する。実際に「開発者の `hosts.yml` のアクティブ
アカウントが fixture の期待値と一致して CLI を呼ばずに allow」で既存テストが
壊れた。`tests/_testutil.start_isolation()` を `setUpModule()` から呼ぶ規律に
した (**3 モジュール**: `test_dispatcher.py` / `test_services.py` /
`test_accounts_builder.py`)。`test_main.py` は子プロセスを起動するので
`os.environ` ではなく**渡す env を組む**側だが、除去規則は共有する
(`_testutil.sanitized_env()`) — prefix ループを各所で再実装すると
`LEAKY_ENV_VARS` に足したときに一方だけ更新される。

隔離そのものにも負テストを置く (`tests/test_testutil.py`)。現在のマシンに
`GH_TOKEN` 等が無いと、pop ループを消しても全 suite が green のまま通る
(実際にマージ前レビューの mutation で survive した)。sentinel を立てて
「落ちること」「`stop()` で戻ること」「`os.environ["HOME"]` が patch 後の
`Path.home()` と一致すること」を固定する。最後の 1 つは
`cli_config.home_overridden()` の基準が実環境の `$HOME` にずれないための
不変条件。

### 0.14.0 (判定機構: tier 分類 + 開示 option の取り消し)

**D24: READONLY を「安全と証明できた形の宣言」に組み替える**

READONLY が「CLI 名からの前方一致 regex + option 無審査」だったため、allow-list に
載せたコマンドが option 次第で write / 開示に化ける同型の穴が繰り返し出ていた
(`gh auth login` の SSH 鍵アップロード → `--skip-ssh-key=false` →
`gh auth refresh --scopes` → `gh auth status --show-token`)。都度パッチしても
「次に何が化けるか」は列挙し切れないので、構造を変えた (上記「tier 分類」):

- `DISCLOSING` で「認証情報を出力する形 / option」を宣言し、READONLY / QUERY を
  取り消す (厳格化方向)
- `READONLY_SAFE_OPTIONS` で「安全と言える option 集合」を宣言し、宣言外の option が
  付いたら QUERY に降格する (= option の allow-list)

**表は推測で広げない。** 「first_token が安全」では不十分という失敗の裏返しで、
「たぶん危ない option」を思いつきで足すと今度は誤 deny 側に穴が空く。列挙は
実際に観測された形 (ticket / レビュー指摘) に限り、未知は「未知として降格」で扱う。

**D25: リモート read は deny せず警告で通す (唯一の緩和方向)**

`gh pr list` / `aws s3 ls` のような**資源を変更しない**コマンドが不一致で deny され、
回復手段として案内される `gh auth switch` はユーザー全体の CLI 状態を変える。
読むためにそこまで要求するのは過剰で、離脱の直接要因になっていた。QUERY tier は
検証は走らせたうえで `additionalContext` で「現在=X 期待=Y」を伝え、実行は止めない。

- **警告の文面を新造しない。** `verify()` が返す deny 理由 (切替案内込み) をそのまま
  本文に使い、前置きだけ差し替える。2 箇所に切替案内を持つと必ず片方が古くなる
- **QUERY 不一致は cache しない** (従来どおり「成功のみ cache」なので自動的にそうなる)
- **`"$readonly": "deny"`** で従来挙動に戻せる。mode との合成は「mode=warn/off は
  全 tier を弱める / QUERY の warn は mode=enforce のときの挙動」

**D26: 判定層を変えたら旧版との出力ペア比較で退行を測る**

tier 分類は READONLY の regex を残したまま**実効 verdict**を変えるので、mutation
では「旧版が拾えていた入力を落とした」型の退行を検出できない。merge 済みの旧版と
新版で同じコーパス (テスト / README / 判定表に現れる約 810 コマンド) を流し、
verdict の差を「意図した緩和 / 意図した厳格化 / 未宣言 option の降格 / 説明不能」に
分類して、説明不能をゼロにしてから出した。**`verify → readonly` (検証が消える方向)
は 0 件**であることが受け入れ条件。

**ただしペア比較はコーパスに入っている形しか測れず、安全性の証明ではない。**
「説明不能 0」はその母集団に対する主張にすぎず、コーパスに無い形 (行継続を含む
複数行コマンドなど) の退行はレビューで初めて出た (D27)。外部レビュー / mutation と
補完関係で使う。

**D27: 候補文字列はシェルが実行する形に正規化してから判定表に当てる**

判定表は 1 行のコマンドを前提にした regex なので、候補文字列に**改行が残ると
一部のエントリだけが死ぬ**。行継続 (`\` + 改行) を畳まないまま当てると、

```bash
aws ssm get-parameter \
  --name n --with-decryption
```

で `.*` を含む DISCLOSING (deny) が外れ、1 行目だけで一致する QUERY (警告のみ) に
落ちた = 判定が緩む方向の死角。`split_on_operators` の opaque 領域処理で
**quote 外の行継続を削除**して解消した (`core/command_parser.py`)。

- **空白 1 個への置換ではなく削除**にする。置換すると語の途中で改行した形
  (`aws configure exp` / 改行 / `ort-credentials`) が
  `aws configure exp ort-credentials` に化け、READONLY の `configure` に当たって
  実際に走る開示形が素通しする。削除ならシェルの結果と一致する
- quote の内側は畳まない (引数の内容が変わる)。option 判定
  (`cli_options.find_option_names`) は `shlex.split` を通すので元から影響を
  受けていなかった — **同じ入力に対して regex 側と option 側で堅牢性が非対称**
  だった形で、regex 側を合わせた
- 判定表を足すときは「複数行で書かれた同じコマンド」を 1 形テストに入れる

### 0.15.0 (連結された切替 + 書込)

**D28: 「hook が 1 回しか動かない」に由来する穴は、通す側ではなく止める側に倒す**

0.14.0 までは `gh auth switch --user other && gh pr create` を README の既知の
制限として**開示するだけ**だった (実行前の状態で検証 = 切替前が期待値なら allow)。
しかし開示は緩和の理由にならない — この plugin が防ぐ対象そのもの (期待外
アカウントでの write) が、最も自然な連結の書き方で素通しになっていた。
実装は上記「切替 + 書込の連結」。

- **厳格化はこの 1 形だけ**で、tier 分類・mode・cache の規則は一切変えていない。
  self-remediation の**受理形**は装飾 option 付きへ広げた (下記)
- 誤 deny 側の代償を明示的に受け入れている: 切替先が静的に判らない形 (login 系 /
  AWS 全般 / `--user $VAR`) は期待値への切替であっても連結が deny になる。
  失敗方向としては (a) 検証が消える より (b) 過剰に deny する を選んだ
- **「役割が変わった述語は、新しい役割向けに calibrate し直す」。**
  `STATE_CHANGING` 所属と `is_self_remediation` はどちらも
  「検証するか否かを決める述語」として calibrate されていた (過剰でも
  「通常検証に落ちる」= 現在値が合っていれば allow だった)。この規則で同じ 2 つが
  「hard deny するか否かを決める述語」に昇格した瞬間、過剰さがそのまま誤 deny に
  なる。**規則を足すときは、流用する述語の失敗コストが変わっていないかを確認する**
  — 変わっているなら述語側を直すのが正しく、規則側に例外を足すのは誤り
  (`changes_identity` の新設と `is_self_remediation` の option allow-list は
  どちらもこの結論)
- **`is_self_remediation` の緩和は連結形だけに閉じない** (この変更で唯一の緩和方向)。
  同じ述語を `_all_self_remediation` も使うので、装飾 option を受理した分だけ
  「その形を**単独実行**したときの検証もスキップされる」= 現在値が期待値と不一致の
  とき deny → allow に変わる形がある (`gcloud config set project <期待> --quiet` /
  `firebase use <期待> --non-interactive` / `kubectl config use-context <期待> --v=4`)。
  **これは装飾なしの形が 0.8.0 から受けている扱いと同じ**で、実行後の状態が期待値に
  なることが静的に判る形に限られる (着地先を変える option は allow-list 外)。
  コーパス退行測定では検出できない — コーパスは 0.14.0 の tests / docs から抽出する
  ので、この形 (装飾 option 付きの案内形) がそもそも母集団に無い。
  **「緩和 0」を主張するときは、緩和が母集団に出現しうるかを先に確認すること**
- **`pending` は後続の「期待値への切替」でクリアしない。**
  `gh auth switch -u other && gh auth switch -u <期待> && gh pr create` は最終状態が
  期待値でも deny する (過剰 deny だが fail-safe)。クリアする実装にすると firebase で
  誤 allow を作る — `login` は identity を変え `use` は project だけを変えるので、
  `firebase login && firebase use <期待> && firebase deploy` 相当の形で
  「login 後のアカウントが期待外」という未検証の状態が通る
- **mutation で測れる範囲を先に確認する** (D26 と同じ注意)。規則の削除・切替側の
  self-remediation 判定の削除・書込側の tier 条件の緩和・走査順の反転はいずれも
  既存テストを落とすが、「規則を cache hit 判定の後ろに移す」mutation は**落ちない**
  — 切替を含む service は `switching_here` で cache を読まないため、そもそも到達
  しない経路だった。落ちない mutation は「テストが弱い」ではなく「その並びが
  実バグを再現していない」ことを意味するので、テスト側を膨らませずに記録に残す

### 0.16.0 (自動切替)

**D29: 切替の往復を消すのは「hook が切り替える」で、「コマンドを書き換える」ではない**

gh のアカウントを頻繁に切り替える運用で、不一致 deny のたびに作業が止まる
(内部バックログ)。案は 2 つあった:

- (A) hook が `gh auth switch` を実行してから通す (採用)
- (B) PreToolUse の `updatedInput` でコマンドを書き換え、期待アカウントのトークンを
  `GH_TOKEN` で渡す (`gh auth token --user <期待>` を展開する形)

(B) はマシン全体の状態を変えないので並行セッションと競合しない。公式 docs 上も
`updatedInput` は `permissionDecision` を省略すれば通常の permission 評価に乗る
(承認を迂回しない)。それでも (A) にしたのは:

- 書き換えは**実行されるコマンドを変える**。複合コマンド・透過 wrapper・`sudo` の
  env scrub (`docs/wrapper-env-audit.md`)・dict 期待値の GHE host (`GH_ENTERPRISE_TOKEN`)
  を全部正しく扱う必要があり、検証 hook の責務を大きく超える
- `GH_TOKEN` が立つと gh はそのトークンで動くので、同じコマンド内の `gh auth status` /
  `gh auth switch` の意味が変わる
- 利用者が求めたのは「切替で止まらないこと」で、手でやっている操作 (`gh auth switch`)
  を hook が代わりにやる (A) の方が挙動を予想しやすい

(A) の代償 (マシン全体への副作用) は opt-in・「止める場面だけ」・並行セッションの
ガード・通知で抑える。並行セッションで別アカウントを使い続ける運用には (B) の方が
合うので、要望が来たら別機能として検討する。

- **QUERY では切り替えない。** 読むだけのコマンドのために他の作業の gh を動かさない。
  「止める場面を切替で置き換える」機能なので、止めない場面 (QUERY の警告 / warn
  モード) には入らない
- mutation (ガードを 1 つずつ壊してテストが assertion で落ちるか) で、`stops` /
  mode / `switching_here` / ガード / cache 破棄 / トークン env / all-or-nothing /
  予算確認 (3 箇所) / 再検証 / 記録 / 通知 / builder の表示 の各条件が新しいテストで
  守られていることを確認した

### 0.16.1 (自動切替を Claude から設定できるようにする)

**D30: 機能の入口は「人間が読む README」だけでなく、Claude が頼まれたときに届く場所にも置く**

0.16.0 は有効化の方法を README (人間向け) にしか書いておらず、「自動切り替えして」と
頼まれた Claude が正しく動けなかった:

- Claude が skill を読み込むかは description で決まるが、3 つの skill の description の
  どれにも自動切替が無い。本文 (accounts-init / accounts-show) の記述は読まれない
- 読まれても本文は「README の自動切替節を案内する」だけで、手順も README の場所も無い
- プロジェクト単位の `"$auto_switch"` は、Claude が accounts.local.json を直接触らない
  約束 (D1) と、builder に予約キーを書くサブコマンドが無いことの両方で書けない

対応:

- **専用の skill (`auto-switch`) を足す。** description は明示の依頼 (「自動切り替え
  して」「gh の切替で止まるのをやめたい」等) にだけ反応させ、「Claude から勧めない」は
  保つ。既存 skill の description に trigger を足す案より良いのは、(1) skill が
  あること自体が「この版に機能がある」ことを示す (0.15 系に env を足しても何も
  起きない、を避ける) (2) accounts-show の description は diff 表示の話で、有効化と
  混ぜると反応の判定がぼやける、の 2 点
- **builder に `auto-switch` サブコマンドを足す** (書くのは `"$auto_switch"` だけ)。
  `"$mode"` / `"$readonly"` は検証を弱めるキーなので引き続き書かない — 見張られている
  側の Claude がスクリプト 1 回で自分の検証を切れる形にしない。`"$auto_switch"` は
  保護を弱めない (切替先は期待値、切替後に再検証)
- accounts.local.json が無いときは作らずに拒否する。`"$auto_switch"` だけのファイルは
  グローバル既定を覆い隠す (期待値を書いていない service がすべて未設定 = deny)。
  拒否時の案内は「init で作る / `--path <グローバル既定>` / 環境変数」の 3 つで、skill の
  手順 (グローバル既定で検証しているプロジェクトでの範囲の選び方) もこの 3 択に揃える。
  「全プロジェクトの環境変数を勧める」に寄せると、グローバル既定のファイルだけで
  足りる利用者に、自前のファイルを持つプロジェクトまで巻き込む範囲を選ばせてしまう
- 全プロジェクト向けは `~/.claude/settings.json` の `env` を skill の手順で編集する。
  builder には書かせない (D2: builder は accounts.local.json 専用の writer)。commit
  される `.claude/settings.json` には書かない (個人の選好が他の開発者にも効く)
- 無効化の手順は、Claude Code の設定の `env` の 2 つの性質に合わせる (公式 docs の
  記述どおりで、CLI 2.1.284 の `-p` でも確かめた): (1) 同じキーはプロジェクトの設定
  (`.claude/settings.local.json` / `.claude/settings.json`) が `~/.claude/settings.json`
  より優先される (キー単位のマージ。hook から見える値も同じ) (2) 値の追加・変更は
  保存した時点で起動中のセッション (hook を含む) に反映されるが、キーの削除は再起動まで
  反映されない (実測は `.claude/settings.local.json` で行った)。このため「どこでも
  止める」は削除ではなく `off` にし、プロジェクトの設定に同じキーがあればそこも直す
- skill の `allowed-tools` (確認なしで使えるツールの付与。制限ではない) は
  `gh auth status` と builder の呼び出しだけに絞る。settings.json の編集は通常の権限確認を
  通す。CLI 2.1.284 の `-p` + default モードでの実測: `/verify-cloud-account:auto-switch`
  で起動したときは 2 つとも確認なしで通った (builder の形は `${CLAUDE_PLUGIN_ROOT}` の
  置換と引用符を含むが一致した)。Claude が Skill ツールで読み込んだ場合は、付与の書き方に
  関係なく (Bash を丸ごと許可しても) 付与が効かず、通常の権限確認になった。どちらも安全側。
  なお sensitive-files-guardrail のような hook が `ask` を返すと、付与より hook が優先される
  (default モードでは builder の呼び出しに確認が出る)。0.17.0 で、builder の付与は
  読み取り専用の形を引数まで書いた完全一致に絞った (D32)
- 手順のコマンドは 1 つずつ、書いたとおりに実行させる。`;` / `&&` での連結や `2>&1` を
  足すと付与の形から外れる (nested の実測で、連結して実行する例があった)

### 0.17.0 (プロジェクトごとのアカウント固定)

**D31: CLI に公式の「プロジェクトごとの固定」があるなら、VCA は切り替えずにそれを案内する**

利用者の要望は「プロジェクト (ディレクトリ) ごとに、使うアカウントが自動で正しく
なること」。0.16.0 の自動切替 (D29) はマシン全体の状態を切り替えるので、並行する
別リポジトリの作業と競合する。調べると、gh 以外は CLI 自身か Claude Code に公式の
仕組みがあった:

- aws / gcloud / kubectl: アカウントを決める公式の環境変数 (`AWS_PROFILE` /
  `CLOUDSDK_ACTIVE_CONFIG_NAME` 等 / `KUBECONFIG`) を、Claude Code の
  `.claude/settings.local.json` の `env` に書く。Claude Code はこのファイルを git
  リポジトリのルートから読み、worktree からも main checkout のファイルを使う
  (Claude Code の docs。CLI 2.1.284 の `-p` で、worktree のセッションの Bash と hook の
  両方にその値が届くことを実測)
- firebase: `firebase use` / `firebase login:use` が作業ディレクトリごとに記録する
  (CLI 自身の help に明記。web docs には保存場所の記載なし)
- gh: プロジェクトごとに安全に分けられる公式の方法が無い (`GH_TOKEN` を設定ファイルに
  平文で置く以外)。自動切替 (D29) を残す

対応:

- **VCA は aws / gcloud / firebase に自動切替を足さない。** 固定は「そのリポジトリでは
  最初から正しいアカウントで動く」形なので、切替より安全 (マシン全体を変えない)。
  VCA の役割は照合と案内に留める
- **skill (`project-accounts`) と builder の `pin-env` (読み取り専用)** で、期待値から
  固定に使う値を出す。aws は `profiles_for_account` (profile 名)、gcloud は
  `configurations_matching` (構成名)、firebase は `.firebaserc` の alias 名。Claude に
  `~/.aws/config` や gcloud の構成ファイルを直接読ませないため builder が読み、名前だけを
  出す。settings.local.json は builder が書かない (D2) — 書き込みは skill の手順で、
  ユーザーの承認を得てから
- **gcloud は構成名を先に勧める。** `CLOUDSDK_ACTIVE_CONFIG_NAME` は VCA のローカル
  読取を保つ (`_LOCAL_SAFE_ENV_VARS`) が、`CLOUDSDK_CORE_*` があるとローカル読取を
  諦めて毎回 `gcloud config get-value` を起動する (15 秒の予算に対して 1 回 1 秒前後)
- **aws の profile が複数あれば利用者に選ばせる。** 同じアカウントでも role の権限が
  違いうるので、先頭を選ぶと hook が権限を決めることになる
- **成功 cache のキーに identity env を入れる** (「短期キャッシュ」節)。settings の
  `env` は保存した時点で起動中のセッションに反映される (Claude Code の docs と実測) ので、
  これが無いと固定した値を変えた直後に前の値での成功で通る。固定を勧める以上、
  同時に直す必要があった
- 書き込み先を決められない構成 (git の外 / bare / submodule / ルートがホーム / 所有者が
  違う / Windows) では、Claude Code が起動したディレクトリのファイルを読むなど条件が
  分かれるので、`pin-env` は推測せず理由を出す
- **`firebase use <x>` の x は、英数字で始まり英数字と `.` `_` `-` だけからなる名前に限る**
  (マージ前レビューの指摘)。この行は skill の手順で Claude がそのまま実行する。x は
  期待値の alias / project ID か、リポジトリの `.firebaserc` の alias (clone しただけの
  リポジトリでも中身を決められる) から来るので、`;` / `$()` / 空白 / 改行や先頭の `-` が
  あると、その文字列がコマンドや option として走る。`.firebaserc` の外れた alias は使わず
  (alias が無いときと同じく project ID を案内する)、期待値の外れた alias / project ID は
  「固定できません」にする。`shlex.quote` は二重化で、許容形はすでにクォートの要らない
  文字だけ。hook が切替の案内として認める形 (firebase の `REMEDIATION_PATTERNS`) にも
  収まる。照合は `fullmatch` で行う (`$` は末尾の改行の前でも一致するため)
- **`pin-env` は期待値を verify() と同じ基準で読む** (マージ前レビューの指摘)。gcloud は
  `DICT_VALUE_CHECK = "truthy"` に合わせ、falsy な項目は無視し、truthy で文字列でない
  項目や空白だけの項目は不正にする (`gcloud.pin_fields`。構成の照合と `CLOUDSDK_CORE_*`
  の両方がこれを使う)。片方を黙って落として残りで固定すると、verify() が同じ期待値で
  deny し続け、固定の手順が通る状態を作れない
- 実測は `claude -p` だけ。対話セッションで workspace trust が `env` の適用に効く
  条件は確かめていない (docs は trust の後に適用すると書いている)
- mutation で、identity env をキーに入れること (読む側・書く側・cache の key)、
  service ごとの宣言 (aws / gcloud / github / kubectl / firebase)、prefix の照合、
  インライン env を重ねること、`pin-env` の判断 (書き込み先の解決・worktree・ホーム・
  候補が複数のとき選ばせる・構成の照合・値を隠す・alias を使う・期待値の無い service を
  出さない) の各条件が新しいテストで守られていることを確かめた
- cache の修正前後を hook 単体で比べた。同じ cache ディレクトリで「一致の env → 不一致の
  env」を続けて stdin の hook input に流すと、0.16.1 は不一致を前の成功で通し、0.17.0 は
  deny した (gcloud の構成名 / `CLOUDSDK_CORE_PROJECT` の両経路)
- nested (`claude -p` + `--plugin-dir`) で、`.claude/settings.local.json` の `env` に
  書いた値で照合されることを確かめた。同じ write 形のコマンドが、期待値と違えば deny、
  一致すれば通る (gcloud の 2 経路。偽の構成ディレクトリを使い、実行はさせていない)。
  一致で通ることが、settings の env が hook に届いている証拠になる (届いていなければ
  手元の実際の構成を読んで deny になる)
- 振り分けも nested で見た。aws / gcloud を固定したいという依頼 (description に無い
  言い回しを含む) は project-accounts に、gh の自動切替の依頼は auto-switch に回り、
  gh の deny への対処を聞いただけならどちらも選ばれない。project-accounts を読み込んだ
  後は、手順どおり最初に `pin-env` を実行した
- skill の `allowed-tools` (builder の読み取り専用の呼び出しを確認なしにする付与。D32) は、
  Claude が自分で skill を読み込んだ経路では効かないことがあった。CLI 2.1.284 の `-p` で、
  同じコマンドが 6 回中 4 回は承認待ちになった (slash で起動したときは効いた)。Claude Code
  の docs は project skill について「ユーザーが起動しても Claude が起動しても適用する」と
  書くが、plugin skill の記述は無い。承認待ちになっても手順は成り立つ (Claude は別の経路で
  値を探さず、承認か `!` 付きでの実行を頼んで止まった)。付与が効く前提の手順にはしない

**D32: skill の `allowed-tools` は、読み取り専用の呼び出しを引数まで書いた完全一致で並べる**

`allowed-tools` は制限ではなく付与で、skill を呼んだターンの間、一致したコマンドを
権限確認なしで通す。0.16.1 の auto-switch と 0.17.0 の project-accounts は builder を
末尾の ` *` で付与していたので、期待値を書き換える `set` / `remove` / `auto-switch` の
`--commit` や、期待値を表示する `--show-values` まで確認なしで通った (マージ前レビューの
指摘)。accounts-init / accounts-migrate / accounts-show は `Bash` を丸ごと付与していた。

- skill の手順の「書く前に AskUserQuestion で承認を得る」はモデルの振る舞いで、誤った
  呼び出しやプロンプトインジェクションでは飛ばされうる。書き込みと値の表示の前には、
  ハーネスの権限確認を残す
- Claude Code の docs (permissions の Wildcard patterns): `*` の無いルールは 1 つの
  コマンドに完全一致し、`*` は空白を含む任意の文字列に一致する (末尾の ` *` は引数なし
  にも一致)。`*` で「この option だけは除く」は書けない。builder の argparse は option の
  省略形 (`--show` → `--show-values`、`--com` → `--commit`) も受け付けるので、文字列で
  除外しようとしても抜ける
- 対応: 各 skill が実行する読み取り専用の形 (show / pin-env と、init・migrate・
  auto-switch の `--dry-run`) を、引数まで書いた完全一致で並べる。`--commit` /
  `--show-values` / `--path` / `--value` を付けた形は通常の権限確認を通す。完全一致は
  別の形 (連結する・`2>&1` を足す・クォートを変える) には一致しないので、外れたときは
  確認が出る側に倒れる
- docs は、plugin skill の `allowed-tools` 内の Bash ルールでも `${CLAUDE_PLUGIN_ROOT}` を
  置換すると書く (skills の Available string substitutions)。本文と同じ文字列で書けば
  本文のコマンドに一致する
- default モードでは、AskUserQuestion の承認の後にもう一度権限確認が出ることがある
  (利用者の設定の許可ルールに一致しなければ)。二重の確認は許容する (書き込みと値の表示は、どちらも利用者が頼んだときだけ起きる)
- `tests/test_skill_permissions.py` が、skill ごとの付与を期待する集合と照合し、本文の
  コードブロックのコマンドが「読み取り専用なら付与される・書き込みと値の表示は付与されない」
  ことを確かめる。照合は docs の規則を広めに見積もった再現 (末尾の ` *` は引数なしにも
  一致、`Bash` 単独はすべてに一致) で行い、その再現自体も docs の例で確かめる

## 既知の制限

利用者から見える制限は README の「既知の制限」が正本。実装者向けに補足すると、
`bash -c` / `eval` / `python -c` / subshell の内側は静的解析できず**検証対象外
(= allow)** になる。これは「解析できないものを deny すると誤 deny が爆発する」
判断で、透過 wrapper に足さないことで表現している。

## リリース手順

1. `.claude-plugin/plugin.json` の `version` を semver で bump
   (挙動変更・機能追加なら minor、修正のみなら patch)
2. `CHANGELOG.md` を更新する
3. `claude plugin validate <plugin dir>` で warning 0 を確認
4. `python3 -m unittest discover hooks/verify-cloud-account/tests` で全 green
5. commit / tag / push

`version` は plugin.json 側にだけ書く (marketplace entry には書かない)。両方に
書くと plugin.json が警告なく勝ち、marketplace 側で bump したつもりが反映されない。
