# verify-cloud-account

Bash コマンド実行の直前に、クラウド CLI
(`gh` / `firebase` / `aws` / `gcloud` / `kubectl`) の
**アクティブアカウントがプロジェクトの想定値と一致するか** を検証する hook。
複数アカウントを切り替えて作業するときに、間違ったアカウントで
`gh pr create` / `firebase deploy` / `aws s3 ...` / `gcloud run deploy` /
`kubectl apply` 等を実行する事故を防ぐ。

不一致なら `permissionDecision: deny` で停止し、切り替えコマンドを提示する。

## インストール

```bash
/plugin marketplace add Mao-o/cc-mp-worktools
/plugin install verify-cloud-account@mao-worktools
```

有効化すると `PreToolUse:Bash` hook が自動登録される。
`settings.json` を手で編集する必要はない。

開発時はローカルパスを直接ロードする方が速い:

```bash
claude --plugin-dir /path/to/cc-mp-worktools/plugins/verify-cloud-account
```

## 初回セットアップ

accounts.local.json の作成・更新は **builder スクリプト経由の Agent Skill**
を使う。builder は書込パスの固定・JSON フォーマットの一貫化・既存キーの
温存を一元管理するため、動作の安定やフォーマット統一の観点から手動書込より
一貫した結果になる:

```
/verify-cloud-account:accounts-init
```

または自然言語でも発火する (例: 「accounts.local.json を作りたい」)。
Agent Skill は Claude の description トリガから自発的にロードされ、以下を
自動で行う:

1. 各 service (github / firebase / aws / gcloud / kubectl) の現在値を CLI から取得
2. `.claude/verify-cloud-account/accounts.local.json` に書き込む **提案**を生成
3. 値を表示する前に AskUserQuestion で確認
4. 承認後に `--commit` でファイル書き込み

配置パスは **`.claude/verify-cloud-account/accounts.local.json`** (v0.3.0 から)。
旧パス (`.claude/accounts.local.json` / `.claude/accounts.json`) は
deprecation 案内付きで後方互換 — ただし新旧両方存在する場合は fail-closed で
deny する (`/verify-cloud-account:accounts-migrate` で統合)。

builder の `init --commit` / `migrate --commit` は同ディレクトリに
`CLAUDE.md` (Claude 向け signpost) も自動生成する (v0.3.1 から)。
`*.local.json` のような機密ファイルパターンへの直接アクセスを制限する
セキュリティ系 hook が有効な環境で、Claude (LLM) からの直接アクセスが deny
された場合でも、同ディレクトリの CLAUDE.md を覗くだけで builder 経由の正規
経路に辿り着けるようにするための案内ファイル。既存 CLAUDE.md は上書きされず、
削除しても plugin 本体の動作には影響しない。

## accounts.local.json の形式

```json
{
  "github":   "Mao-o",
  "firebase": "my-project-id",
  "aws":      "123456789012",
  "gcloud":   "my-gcp-project",
  "kubectl":  "prod-cluster"
}
```

必要なキーだけ書けばよい。未記載のサービスコマンドは検証対象外 (= allow)。

### `.gitignore`

プロジェクトでは `accounts.local.json` を git 管理外にする:

```gitignore
# .gitignore
.claude/verify-cloud-account/accounts.local.json
# (旧パス残置している間も)
.claude/accounts.local.json
```

## Agent Skill

| skill | 用途 |
|---|---|
| `/verify-cloud-account:accounts-init` | 新規プロジェクトで accounts.local.json を対話生成 |
| `/verify-cloud-account:accounts-show` | 既存値と CLI 現在値の diff を表示 |
| `/verify-cloud-account:accounts-migrate` | 旧パスから新パスへの統合 |

各 skill は description のトリガから Claude が自発的にロードする。明示的に
呼び出したいときは `/verify-cloud-account:<skill-name>` を使う。

builder (`scripts/accounts_builder.py`) を直接呼ぶことも可能:

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/hooks/verify-cloud-account/scripts/accounts_builder.py \
  init --service github --commit
```

ただし Agent Skill の方が dry-run → AskUserQuestion 承認 → commit の確認
フローを含むため、通常はこちらを使う。

### 既存値の更新・削除 (`set` / `remove`)

`init` は既存キーを上書きしない (異なる値なら `skipped` して終了する)。
値を切り替えたい・削除したいときは `set` / `remove` サブコマンドを使う:

```bash
# 既存値を上書き (無ければ新規追加)。--from-cli で CLI 現在値を使うことも可能
python3 ${CLAUDE_PLUGIN_ROOT}/hooks/verify-cloud-account/scripts/accounts_builder.py \
  set --service github --value new-user --commit

# dict 値 (GHE の hostname / Firebase の alias 等) の特定キーだけを追加・上書き
python3 ${CLAUDE_PLUGIN_ROOT}/hooks/verify-cloud-account/scripts/accounts_builder.py \
  set --service github --host ghe.example.com --value mao-corp --commit

# キー全体を削除
python3 ${CLAUDE_PLUGIN_ROOT}/hooks/verify-cloud-account/scripts/accounts_builder.py \
  remove --service github --commit

# dict 値の特定キーだけを削除 (最後の 1 つを消すとキー自体が削除される —
# 空オブジェクトを残すと該当 service が「オブジェクトが空です」で永久 deny
# されるため。最後の 1 つでなくても、残りが該当 service にとって使えない
# 形になる場合 (例: gcloud で project/account 以外のキーだけが残る) は
# 同じくキー自体を削除し、理由を出力する)
python3 ${CLAUDE_PLUGIN_ROOT}/hooks/verify-cloud-account/scripts/accounts_builder.py \
  remove --service github --host ghe.example.com --commit
```

`{"github": null}` のように値が壊れている (dispatcher が設定なしと同じ扱いで
必ず deny する) entry も、`--host` を付けずに `remove --service <svc> --commit`
すればキーごと削除できる。

`--host` は既存値がオブジェクト (または未設定) のときだけ使える。既存値が
文字列の service に `--host` を付けるとエラーになる (先に `remove` で削除
するか `--host` を外して上書きする)。`--host` と `--from-cli` は併用できない
(CLI の現在値がどの host/alias のものかを builder は判別できないため、
`--host` 指定時は `--value` で値を明示する)。`--value` は `init` と同じく JSON
object 文字列 (例: `'{"project":"p","account":"a"}'`) を渡すと dict として
保存し、それ以外は生文字列として保存する。dict を受け付けない service
(aws / kubectl) にオブジェクトを渡す、gcloud に未対応キーを渡す、といった
形式不正は `--commit` 前に検出して exit 1 にする (accounts.local.json への
書込は行われない)。いずれも既定では stdout に値を出さず、`--show-values`
明示時のみ表示する (D3 と同じ)。

対象ファイルは **hook が実際に読むもの**に揃う (3-tier lookup + 親ディレクトリ
遡及)。解決したパスは dry-run / commit の出力先頭に `対象: <パス>` として出る。
worktree などで親から継承している場合は親側のファイルが編集され、この階層専用の
設定を作りたいときだけ `--path <file>` で明示する (詳細は
[builder も同じ解決を使う](#builder-も同じ解決を使う))。

## 対象コマンドと検証スキップ

### 発火するコマンド

コマンドを**セグメントに分解して先頭が対象 CLI になる形**を照合する。以下の
チェーン・構文・ラッパは自動で解析・剥がしたうえで CLI 部分だけを照合する:

- **コマンドチェーン**: `&&` / `||` / `;` / `|` / 改行 (quote / $() / backtick 内は保護)
- **heredoc** (v0.9.0): `cat > x.sh <<'EOF'` 〜 `EOF` の**本文は解析しない**。
  本文に書かれた CLI 例は実行されないため (`<<-` のタブ除去、quote 付き
  delimiter、`END-OF-FILE` のようなハイフン入り delimiter、1 行に複数 heredoc、
  here-string `<<<` の非対象化に対応)。delimiter 行の次のコマンドは従来どおり
  別セグメントとして検証する。本文は候補文字列からも落とすので、本文中の
  `--profile` 等がコマンドの option として解釈されることもない
- **シェル構文プレフィックス** (v0.9.0): `(` / `{` / `!` / 予約語
  (`if` `then` `elif` `else` `do` `while` `until`) / コマンド本体前の
  リダイレクト (`</dev/null gh ...` / `2>/dev/null gh ...`)、および末尾の
  `)` `;` `}` `&`。`(gh pr create --fill)` や
  `for f in *; do gh release upload ...; done` も検証対象になる
- **コマンド名のパス / エスケープ** (v0.9.0): `/opt/homebrew/bin/gh` /
  `./node_modules/.bin/firebase` / `\gh` は basename に正規化して照合する
- **先頭の環境変数割当**: `FOO=bar gh ...`
- **透過的 wrapper**: `sudo` / `time` / `nohup` / `command` / `builtin` / `exec` /
  `env [KEY=val...]` (ただし `env -i` / `env --` など option 付きは不透明扱い)
- **実行ラッパ** (v0.9.0): `timeout` (先頭の DURATION 引数も消費) / `nice` /
  `stdbuf` / `setsid` / `caffeinate` / `watch` / `xargs`。
  `timeout 30 gh pr create` や `... | xargs -I{} gh pr close {}` の後段も検証する
- **ランタイム / パッケージマネージャ**: `npx` / `pnpm exec` / `pnpm dlx` /
  `mise exec --` / `bun x`
- 行頭インライン env の伝播は wrapper の実行時 env 挙動に従う
  (`sudo` は preserve 無しだと env を scrub。詳細は
  [`docs/wrapper-env-audit.md`](docs/wrapper-env-audit.md))
- 上記は多段ネストにも対応 (例: `sudo time mise exec -- firebase deploy` →
  `firebase deploy` として検証)
- **CLI 名直後の global option** (v0.8.0): `aws --profile prod sso login` /
  `gcloud --project X config set ...` / `kubectl --context X config use-context ...` /
  `firebase -P X use ...` のようにサブコマンドの前に置かれた global option は、
  剥がした形 (`aws sso login`) でも readonly / 切替 (cache 無効化) /
  self-remediation を判定する (deny 文面の検出コマンドには元の形を表示)。各 service
  が値を取る option / 取らない option を宣言し (`GLOBAL_OPTIONS_WITH_VALUE` /
  `GLOBAL_FLAGS`)、未知の option が先頭にある場合は剥がさず通常検証する。
  `--profile` / `--project` / `--context` の**値**をどう検証に使うかは
  [コンテキスト指定 flag の反映](#コンテキスト指定-flag-の反映-v090) を参照

| サービス | マッチ対象 | 期待値の取得 |
|---|---|---|
| GitHub | `gh ...` | `gh auth status` のアクティブアカウント |
| Firebase | `firebase ...` | `firebase use` (非 TTY) の解決済み project ID → CLI から取れない時のみローカル設定 (configstore / `.firebaserc`) |
| AWS | `aws ...` | `aws sts get-caller-identity --query Account` |
| GCP | `gcloud ...` | `gcloud config get-value project` (+ optional `account`) |
| Kubernetes | `kubectl ...` | `kubectl config current-context` |

Firebase の現在値は firebase-tools 本体と同じ順で解決する (v0.7.3)。
`firebase use <alias|project>` の切替先は CLI の configstore
(`~/.config/configstore/firebase-tools.json` の `activeProjects`) にだけ保存され
`.firebaserc` には反映されないため、まず `firebase use` (非 TTY では解決済みの
project ID を 1 行出力) を読む。その cwd は `firebase.json` を親方向に探した
project root (無ければ project dir) に固定し、hook / builder プロセスの cwd は
継承しない (builder を project の外から起動しても無関係な project を報告しない)。
CLI から取れないとき (hook の PATH に無い / 実行不可 / 非ゼロ終了 / 出力が空 /
複数行) は CLI と同じローカル設定から同じ規則で解決する (起点は同じ project root):
configstore の切替先を `.firebaserc` の alias で解決 → 無ければ `.firebaserc` の
alias が 1 つならその値 → `default`。`npx firebase ...` のように hook 側に
`firebase` が無い構成でも、configstore 経由で切替を見落とさない。
`firebase use` が timeout したときは fallback せず「firebase use がタイムアウト
しました」で deny する (fail-closed)。

### 検証をスキップする readonly コマンド

「アカウント設定のための状態確認」でデッドロックしないよう、以下は素通し:

- `gh auth status` / `gh auth list`
- `firebase use` (引数なし)
- `firebase login` / `firebase logout` (`login:ci` / `login:add` / `login:use` 等の
  サブコマンド含む) — project を変更しない認証操作。未ログインだと `firebase use`
  が認証必須で失敗して現在値を取れず、`firebase login` 自体が deny されるデッド
  ロックになるのを防ぐ (v0.7.3)
- **認証取得系** (v0.8.0): `gh auth logout` / `setup-git`
  (`logout` はローカル `hosts.yml` のエントリ削除、`setup-git` はローカル git config の
  credential.helper 設定で、どちらもアカウント側には何も書かない。**`gh auth refresh` は
  含まない** — 保存済み認証情報の権限を拡張・修正するコマンドで
  `--scopes admin:org` のようにアカウント側の OAuth grant を変更しうるため通常検証する)、
  `gh auth login` は **リモートに何も書かない形のみ** — (a) SSH 鍵のアップロードが
  起きない (`--skip-ssh-key` / `--with-token` / `--git-protocol https` (`-p https`)
  付き。flag 文字列の有無ではなく実効 boolean を解釈し、`--skip-ssh-key=false` /
  `--with-token=0` のような明示 false や `--git-protocol ssh` は無効、繰り返しは後勝ち。
  SSH git protocol を選ぶ login は既存の SSH 公開鍵を GitHub アカウントに
  アップロードしうる) かつ (b) **`-s` / `--scopes` で OAuth grant scope を要求して
  いない** (`gh auth login --skip-ssh-key --scopes admin:org` は鍵操作こそ起きないが
  アカウント側の grant を拡張するため通常検証。値を取る flag なので `=false` では
  無効化できず、付いていれば無条件に検証対象)。それ以外の `gh auth login` は
  通常検証し、deny 文面では `--skip-ssh-key` 付きの形を案内する、
  `aws sso login` / `aws sso logout` / `aws login` / `aws logout` /
  `aws configure ...` (認証情報を出力する `configure export-credentials` は除く)、
  `gcloud auth login` / `gcloud auth application-default login` / `revoke` /
  `set-quota-project` / `gcloud auth activate-service-account` / `gcloud auth revoke`
  (`firebase login` 系は `npx firebase-tools login` の形も同じ) — クラウド資源を
  変更せず、ローカルの認証状態・profile 設定を作るだけ。未ログインで検証
  (`aws sts get-caller-identity` 等) が失敗する状態では、deny 文面が案内する
  `aws sso login --profile <profile>` / `gh auth login` 自体が deny される
  remediation loop になっていた。これらは同時に「アカウント状態を変えうる
  コマンド」として成功 cache を破棄するため、直後の write は必ず再検証される
  (後述)
- `aws sts get-caller-identity`
- `gcloud auth list` / `gcloud config get-value project` / `... account`
- `kubectl config current-context` / `... get-contexts` / `... view` /
  `... get-clusters` / `... get-users` / `kubectl cluster-info`
- **情報系コマンド** (各 CLI の `--version` / `--help` / `version` / `help`) —
  アカウント検証不要。診断で打つ `command aws --version` 等が誤って検証対象に
  なり deny されるのを防ぐ (v0.7.0)
- **存在確認** (v0.9.0): `command -v gh` / `command -V aws` / 連結形 `command -pv aws`。
  `command` は `-v` / `-V` 付きだと**後続 CLI を実行せずパスを表示するだけ**
  (`man 1 bash`) なので、透過 wrapper として剥がさず素通しする。剥がしていた頃は
  インストール確認の定型句だけでアカウント検証が走って deny されていた。
  `command gh ...` / `command -p gh ...` / `command -- gh ...` は実行するので従来どおり検証

同一コマンドチェーン内で readonly と非 readonly が混在する場合
(例: `gh auth status && gh pr list`) は非 readonly セグメントについて検証が走る。
deny されたときは reason 末尾の `(検出コマンド: ...)` でどのセグメントが検証を
起動したかを確認できる。

### 期待値へ向かう切替コマンドは許可 (self-remediation)

deny メッセージが案内する「期待値への切替コマンド」自体が `^gh\b` 等にマッチして
deny される remediation loop を防ぐため、**accounts.local.json の期待値へ向かう
切替コマンドだけは検証なしで許可**する:

- `gh auth switch --user <期待アカウント>` (dict 形式は `--hostname` も照合、省略時 github.com)
- `gcloud config set project <期待値>` / `gcloud config set account <期待値>`
- `firebase use <期待 alias または project ID>`
- `kubectl config use-context <期待コンテキスト>`

**案内された切替 / ログインコマンドは案内された形のまま単独で実行すること。** 元のコマンド
を同じコマンド行に連結すると (`gh auth switch ... && gh pr create` 等)、そちらが切替**前**
の状態で検証されて再び deny される (案内文自身が連結している `firebase login && firebase
use <x>` は許可される形)。remediation を案内する deny 文面にはこの注記が付く (v0.11.1)。

期待値**以外**への切替は従来どおり通常検証 (実行前の状態) に落ちる。切替コマンド
(self-remediation を含む) を検出した時点で当該 service の成功 cache を破棄し、
切替コマンド自身の検証成功も cache しないため、切替後の最初の write は成功 cache
の残り時間に関係なく次回 hook で再検証される (v0.8.0。詳細は
[パフォーマンス (短期キャッシュ)](#パフォーマンス-短期キャッシュ))。

AWS は期待値 (Account ID) と profile 名の照合が hook からは不能なため、この特例の
対象外。代わりに deny 文面は `AWS_PROFILE=<profile> aws ...` (行頭インライン env。
検証にも反映される) を第一に、`aws sso login --profile <profile>` (readonly で
検証なしに通る) を第二に案内し、`~/.aws/config` (または `$AWS_CONFIG_FILE`) に期待
Account ID の `sso_account_id` / `role_arn` を持つ profile があれば `<profile>` を
具体名にする (profile 名以外の内容は文面に出さない)。`export AWS_PROFILE=...` は
Claude Code の Bash では次の呼出に持ち越されず、hook (Claude 本体の env を継承)
の検証にも反映されないため、コマンドとしては案内しない (v0.8.0)。

## コンテキスト指定 flag の反映 (v0.9.0)

`aws --profile other s3 rm ...` のように、**コマンド側で照合先を切り替える
flag** は、その値を検証に反映する。従来は hook の既定コンテキスト
(既定 profile / アクティブ project / current-context) だけを見ていたため、
「検証は既定 / 実行は other」という誤 allow になっていた。

| サービス | 見る option | 検証への反映 |
|---|---|---|
| AWS | `--profile` | 検証コマンドにも `--profile` を付けて実行 (CLI の資格情報解決順を実行時と揃える) |
| GCP | `--project` / `--account` | 値を期待値と直接照合 (アクティブ設定は見ない) |
| GCP | `--configuration` | 現在値の取得コマンドに引き渡す |
| Firebase | `--project` / `-P` | `.firebaserc` の alias を解決してから照合 (CLI 本体と同じ規則) |
| Kubernetes | `--context` | 値を期待値と直接照合 |
| Kubernetes | `--kubeconfig` | 現在値の取得コマンドに引き渡す |

- 記法は `--opt value` / `--opt=value` / 短縮の分離形 (`-P prod`) / 連結形
  (`-Pprod`) / `-P=prod` を同じ規則で扱う。`--` 以降は後続コマンドの引数なので
  見ない (`kubectl exec pod -- cmd --context x` の `--context` は採用しない)
- 同じ option が複数あれば**最後が有効** (各 CLI の実装と同じ)
- サブコマンドの**後ろ**に書かれた形 (`aws s3 ls --profile prod`) も拾う
- **GCP は key ごとに独立**して上書きする。`gcloud --project ok run deploy` でも
  `account` の期待値があれば account はアクティブ値と照合する
- 値が変数展開などで静的に解決できない場合 (`--profile $PROF`) は、従来どおり
  既定コンテキストで照合する
- 1 コマンド内で異なる指定が混ざる形
  (`aws --profile a s3 rm x && aws --profile b s3 rm y`) は**別々に検証**する。
  成功キャッシュも指定ごとに分かれる
- **`gh` は対象外**。`gh auth <sub> --hostname <host>` / `--user <user>` は
  「どのアカウントで実行するか」ではなく**操作対象**の指定なので、照合先は常に
  アクティブアカウント (後述の既知の制限を参照)

## インライン環境変数の伝播 (v0.7.0)

コマンド行頭のインライン環境変数 (`AWS_PROFILE=prod aws ...`) は、剥がして CLI
部分を照合するだけでなく **検証 subprocess にも渡される**。これにより
「コマンドを実行しようとしている env」と同じ条件でアカウント検証が走る。

```bash
# 例: SSO profile 運用で行頭に AWS_PROFILE を付けて実行
AWS_PROFILE=prod aws s3 ls
```

従来は行頭 env を剥がすだけで検証には使わなかったため、`~/.aws/config` に
`[default]` を置かない SSO 運用では、ログイン済みでも検証が default profile で
失敗し **永久に deny** されていた。v0.7.0 ではインライン env を
`{**os.environ, **inline}` としてマージし検証 subprocess に渡すことで解消した。

- 対象は全 5 service (`AWS_PROFILE` / `AWS_REGION` / `CLOUDSDK_*` / `KUBECONFIG` /
  `GH_HOST` 等、行頭に書いた任意の `KEY=VALUE`)
- profile が異なれば短期キャッシュも別エントリになり、profile A の成功が
  profile B で誤って allow されることはない
- 値に未展開の変数参照 (`AWS_PROFILE=$SOMEVAR`) を含む場合は静的に解決できない
  ため検証 env には渡さない (コマンドからは剥がす)

### 透過 wrapper を跨ぐときの env 伝播

行頭 env が**透過 wrapper の前**に置かれた場合、その wrapper が実行時に env を
素通すかどうかで伝播可否が変わる:

- `time` / `nohup` / `command` / `exec` / `npx` / `pnpm exec` / `mise exec --` /
  `bun x` などは env を素通すため、`AWS_PROFILE=prod time aws ...` の `AWS_PROFILE`
  は検証にも反映される
- **`sudo` (preserve 無し) は継承 env を scrub する**ため、
  `AWS_PROFILE=prod sudo aws ...` の `AWS_PROFILE` は実行時の `sudo aws ...` には
  届かない。検証側もこれに合わせ pre-sudo env を伝播せず、検証はデフォルト env で
  走る (= 「検証は prod / 実行は別アカウント」の誤 allow を防ぐ)。`sudo -E` /
  `--preserve-env` を付けた場合は env が保持されるので検証にも反映される
- `env -i` / `env -u` / `env --` は環境をリセット/縮小するため透過剥がしの対象外
  (そのセグメントは検証スキップ)

wrapper ごとの env 挙動の完全な分類と将来 wrapper 追加時の方針は
[`docs/wrapper-env-audit.md`](docs/wrapper-env-audit.md) を参照。

### direnv / CLAUDE_ENV_FILE 経由の env は届かない

`.envrc` (direnv) や他の `CwdChanged` hook が `CLAUDE_ENV_FILE` 経由で注入する
環境変数は **PreToolUse hook には渡らない**。Claude Code 公式仕様で
`CLAUDE_ENV_FILE` は SessionStart / Setup / CwdChanged / FileChanged のみに
提供され、PreToolUse は対象外のため (hooks.md に明記)。`.envrc` に
`export AWS_PROFILE=...` を書いても Bash ツール実行時には効くが、本 hook の検証
subprocess には反映されず deny される。

これは harness 仕様起因で plugin 側では解決できない。通したい場合の回避策:

1. **インライン env** (上記): `AWS_PROFILE=prod aws ...` と行頭に付ける (最も手軽・確実)
2. **`.claude/settings.json` の `env`**: Claude プロセスの env に設定する公式機能。
   hook subprocess も親環境を継承するため効くと考えられる
   (例: `{"env": {"AWS_PROFILE": "prod"}}`、要セッション再起動・本 plugin では未実測)
3. **起動時 env**: `AWS_PROFILE=prod claude` で Claude 自体を起動する

## 拡張フォーマット (object 形式)

### GitHub Enterprise / 複数 hostname

`github` を object で書くと、hostname ごとのアクティブアカウントを個別検証する:

```json
{
  "github": {
    "github.com":        "Mao-o",
    "ghe.company.com":   "mao-corp"
  }
}
```

どちらかの hostname のアクティブアカウントが期待値と違う、または hostname 自体
ログインしていないと deny する。

### Firebase の複数 alias

`.firebaserc` の `projects` マップと対応させて、どれか 1 つの project ID が
アクティブなら OK にできる:

```json
{
  "firebase": {
    "default": "proj-dev",
    "prod":    "proj-prod"
  }
}
```

`firebase use prod` で `proj-prod` に切り替えてあれば allow。どちらでもなければ
deny し「proj-dev, proj-prod のいずれか」を提示する。

### GCP の account も検証

`gcloud` を object にすると project に加えてアクティブアカウント
(メールアドレス) も検証する:

```json
{
  "gcloud": {
    "project": "my-proj",
    "account": "me@example.com"
  }
}
```

`project` / `account` は片方だけでも可。

## 配置パスの 3-tier lookup (v0.3.0)

dispatcher は以下の順に accounts.local.json を探す:

1. **新**: `.claude/verify-cloud-account/accounts.local.json` (推奨)
2. **deprecated**: `.claude/accounts.local.json` (警告付きで受け入れ)
3. **legacy**: `.claude/accounts.json` (警告付きで受け入れ)

### 旧パスからの移行

旧パスのみ存在する場合は検証は通るが、以下のように deny/warn で案内が出る:

```
.claude/accounts.local.json は旧パスです。
.claude/verify-cloud-account/accounts.local.json への移行を推奨します。
旧パスから統合するには builder の migrate サブコマンドを使用してください: ...
```

**新旧両方存在する場合は fail-closed で deny** する。どちらが正本か曖昧な状態で
検証を通すと、どの設定が効いているか不透明になる。
`/verify-cloud-account:accounts-migrate` で統合するか、不要な方を手動削除する。

## 親ディレクトリ遡及 (v0.4.0)

cwd 階層に accounts.local.json が無い場合、**親ディレクトリを 1 階層ずつ
遡って探す**。git worktree から作業しているとき、worktree 内に
accounts.local.json を複製しなくても親 repo (本体 checkout) の設定を
自動継承する。

```
/repo/main-checkout/.claude/verify-cloud-account/accounts.local.json
/repo/main-checkout/.worktrees/feature-x/   ← cwd (worktree)
```

worktree (`/repo/main-checkout/.worktrees/feature-x/`) で `gh pr create` を
叩いても、親 repo の accounts.local.json が継承されて検証が走る。
worktree 内に同名ファイルを置く必要は無い。

**仕様**:

- 探索順は cwd → cwd.parent → ... と 1 階層ずつ上る。最初に見つかった階層を採用
- cwd 階層に何かあれば親は見ない (cwd 優先)
- 同一階層に複数 tier が同居する場合は従来どおり fail-closed deny (D4)
- 安全側上限として `max_levels=10` (`core/paths.py`)
- 親採用時は deny / warn メッセージに `accounts.local.json は親ディレクトリ
  <絶対パス> から継承しています` の 1 行注釈が付く (verify 成功時は silent)

### builder も同じ解決を使う

`accounts_builder.py` の `init` / `show` / `set` / `remove` / `migrate` は
**dispatcher と同じ解決** (3-tier lookup + 親ディレクトリ遡及) で対象ファイルを
決め、解決したパスを dry-run / commit の出力の先頭に `対象: <パス>` として表示する。
読む側と書く側で解決を共有しないと、継承中の worktree で `set` が編集した service
だけを含む子ファイルを作り、dispatcher の遡及がそこで止まって**継承していた他の
service が一斉に未設定 (deny)** になる。

- 継承中の `set` / `remove` / `migrate` は**継承元のファイル**を直接編集する
- 継承中の `init` は cwd 直下に作ると継承中の設定を覆い隠すため **exit 2 で拒否**
  し、`set` / `remove` (値の変更・削除) か `--path` (この階層専用の設定を作る) を
  案内する
- `--path <file>` を全サブコマンドに指定でき、明示時は解決を飛ばしてそのファイルを
  対象にする。受け付けるのは dispatcher が読む配置
  (`.claude/verify-cloud-account/accounts.local.json` と旧 2 パス) だけで、書込を
  伴うコマンドでは新パスのみ — 他の場所に書いても hook が読まないため。
  指定先が解決結果と食い違う場合は「hook が現在読むのは別ファイル」の警告を出す
  (近い側を指定すると現在の設定を覆い隠し、遠い側を指定すると書いても読まれない)
- 対象階層に複数 tier が同居する場合は builder も同じ理由で拒否する (D4)

**書込範囲についての注意**: 解決は最大 10 階層まで親を遡る
(`ANCESTOR_SEARCH_MAX_LEVELS`)。つまり accounts.local.json を持たないプロジェクトで
`set` を実行すると、10 階層以内の祖先 (ホームディレクトリを含む) にファイルがあれば
**そちらが編集対象になる**。これは「hook が読むファイルを編集する」という意図どおりの
挙動だが、builder の書込範囲は cwd 配下に限られない。対象は出力先頭の `対象:` 行に
必ず出るので、commit 前に確認すること (この階層専用の設定にしたい場合は `--path`)。
`.gitignore` への追記も同じ階層に対して行われる。

## パフォーマンス (短期キャッシュ)

PreToolUse は Bash の度に発火するため、`gh pr list && gh pr view && gh pr comment`
のような連打で毎回 `gh auth status` (〜500ms) や `aws sts get-caller-identity`
(〜1-3s) を走らせるとストレスになる。そのため **検証成功を 30 秒キャッシュ** する。

- 保存先: `$TMPDIR/cc-mp-verify-cloud-account/<service>-<sha256>.json`
  (epoch は同じディレクトリの `<service>.epoch`)
- 無効化: TTL 経過 / `accounts.local.json` の mtime 変化 / ファイル破損 /
  **アカウント状態を変えうるコマンドの検出** (v0.8.0、下記) / entry の epoch が
  現在と異なる
- **失敗 (deny) 状態はキャッシュしない** — 切り替え後は即座に再検証が走る

### 切替・ログイン系コマンドでの即時無効化 (v0.8.0)

次のコマンドを含む Bash を検出すると、実行前 (PreToolUse) の時点で当該 service の
成功 cache を **project / 期待値 / インライン env を問わず全て破棄**し、そのコマンド
自身の検証成功 (実行前の状態) も cache しない。CLI のアカウント状態はマシン全体で
共有される (`hosts.yml` / gcloud 設定 / configstore / kubeconfig / SSO token cache)
ため service 単位で破棄する。過剰な破棄のコストは再検証 1 回で済む。

| サービス | cache を破棄するコマンド |
|---|---|
| GitHub | `gh auth switch` / `login` / `logout` / `refresh` |
| Firebase | 引数ありの `firebase use ...` (`--clear` / `--add` 含む。引数なしは表示のみで除外) / `firebase login*` / `logout` (`npx firebase-tools ...` 形も同じ) |
| AWS | `aws sso login` / `sso logout` / `aws login` / `logout` / `aws configure ...` (表示系の `configure list` / `list-profiles` / `get` / `export-credentials` は除く) |
| GCP | `gcloud config set` / `unset` / `gcloud config configurations activate` / `create` / `gcloud auth login` / `activate-service-account` / `revoke` / `application-default login` / `revoke` / `gcloud init` |
| Kubernetes | `kubectl config use-context` / `set-context` / `set-cluster` / `set-credentials` / `set` / `unset` / `delete-*` / `rename-context`、および別 CLI / plugin 経由の kubeconfig 書換 `gcloud container clusters get-credentials` / `aws eks update-kubeconfig` / `az aks get-credentials` / `kubectx` / `kubectl ctx` |

切替セグメントが同じコマンド内の write と別セグメントでも (readonly の `gh auth login
--skip-ssh-key && gh pr create`、inline env が異なる `gh auth switch --user other &&
GH_HOST=... gh pr create`)、その service の検証成功は cache しない (判定は service 単位)。

**並行する hook との競合 (epoch + in-flight 窓)**: 無効化は entry の削除だけでなく
service ごとの epoch (`<service>.epoch`、単調増加) を進め、切替を検出した時刻
(tombstone) を記録する。entry には verify 開始時点の epoch を記録し、読む側は epoch が
現在と違えば無視、書く側は開始時と現在の epoch が違えば書かない (無効化**前**に開始
した並行検証の結果を公開しない)。さらに tombstone から 60 秒 (`IN_FLIGHT_SEC`、定数)
は「切替の実行中」とみなして成功 cache を書かない (無効化**後**・切替完了**前**に
開始した並行検証の結果を公開しない)。hook は実行前にしか走らず切替コマンドの完了
時刻が分からないため時間で区切る。代償は切替後 60 秒間の毎回再検証。

従来 (〜0.7.3) は `gh pr list` (検証成功・cache 書込) → `gh auth switch --user other`
→ `gh pr create` が 30 秒以内なら cache hit で別アカウントの write が通っていた。

## 既知の制限

- `gh auth status` / `firebase use` / `aws sts` / `gcloud config` /
  `kubectl config` の**出力フォーマット**に依存している。CLI 本体の
  major update で壊れる可能性あり。`gh auth status` は **gh 2.40 以上を推奨**
  (2.40 で複数アカウント対応の `Active account: true/false` marker が追加され、
  それ以前の単一アカウント形式にも fallback で対応するが、どちらの形式にも
  一致しない出力は「解釈できません」で deny する。`gh --version` で確認可)
- `bash -c '...'` / `eval` 内に埋め込まれた CLI 呼び出しは静的解析できず検証
  対象外 (透過 wrapper のリストに `bash` は含めていない)
- `gh auth <sub> --hostname <host>` / `--user <user>` のように**操作対象の
  host / アカウントをオプションで指定する形**は照合しない。これらは「どの
  アカウントで実行するか」ではなく操作の**対象**指定なので、検証はあくまで現在の
  アクティブアカウントに対して行う。アクティブが期待値なら別 host / 別ユーザー
  向けの操作 (例: `gh auth refresh --hostname ghe.example.com`) は allow される
  (str 形式の期待値で複数 host にログインしている場合は dict 形式にすると
  host ごとに照合できる)
- **`git push` / `git clone` / `git fetch` など `gh` 以外の GitHub 操作は
  検証対象外**。PATTERNS は `^gh(?=\s|$)` のみで `git` コマンド自体には一致
  しない。SSH 鍵や git credential helper 経由で別アカウントの GitHub へ
  push/clone しても、この plugin は関与しない (対象はあくまで `gh` CLI 経由の
  操作)
- **`aws-vault exec prod -- aws ...` のような別コマンド経由の実行は検証対象外**
  (v0.9.0)。`aws-vault` は `aws` とは別のコマンドなので発火しない。従来は
  `^aws\b` がハイフンを語境界として拾い、hook の**既定 profile**で `sts` を
  実行していた — 未設定なら永久 deny、既定が期待値なら実行 profile が prod でも
  allow という二重の誤りだったため、いったん対象外に倒した。`aws-vault exec <profile> --`
  を「`AWS_PROFILE` を合成する条件付き wrapper」として扱う対応は今後の課題
- **コンテキスト指定 flag の誤検出**: 値を取る未知の option の値がたまたま
  `--profile` 等に見える形 (`aws s3 cp --exclude --profile x`) では、その値を
  コンテキスト指定と誤読しうる。全サブコマンドの option 表を持てないため
  避けられない。誤読しても照合先が変わるだけで、検証自体はスキップされない
- heredoc 本文は解析対象から外す (開始行だけを候補に残す)。ただし **terminator が
  無い未終端 heredoc は heredoc として扱わない** — 「閉じていなければ末尾まで
  本文とみなす」方式だと delimiter を読み違えた瞬間に「以降すべて検証しない」に
  化けるため、通常の改行分割に戻す。結果として未終端 heredoc の本文行は候補に
  なりうる (bash 側でもエラーになる壊れたコマンドなので、そちらの代償を取る)
- `kubectl --kubeconfig <file> --context <ctx>` は `--context` の**名前だけ**で
  照合する。コンテキスト名は kubeconfig ファイルを跨いで一意ではないため、
  別ファイルの同名コンテキストは区別できない
- `if false; then aws ...; fi` のように**実行されない分岐**の中身も候補になる。
  予約語 (`then` / `else` / `do`) を剥がして本体を検証対象にしている副作用で、
  条件の真偽までは静的に評価しない (実行される場合を取りこぼさない側に倒している)
- subshell 内のコマンド (`FOO=$(gh ...) cmd` の内側の gh) は検証対象外
- 期待値以外への切替と write を**同一コマンド**で実行した場合
  (`gh auth switch --user other && gh pr create`) は、実行前の状態で検証されるため
  切替後の write は検証されない (hook は実行前にしか動かない)。別々のコマンドで
  実行すれば切替で cache が破棄され、write は次回 hook で再検証される
- **60 秒 (`IN_FLIGHT_SEC`) を超える対話 login** (`gh auth login --web` /
  `aws sso login` 等のブラウザ認証) の最中に、同一 service の並行検証 (並列 Bash
  呼出) があった場合、その検証が login 前の状態で成功すると entry が書かれ、login
  完了後も最大 30 秒 (TTL) 有効になりうる。PostToolUse hook で実行後に無効化すれば
  閉じるが、全 Bash 呼出に Python プロセスが恒久的に乗るため採用していない
- Firebase の alias object 形式は `.firebaserc` の `projects` マップとの
  対応を前提にしており、ユーザー任意の key 名を受け付けるだけで "alias 名"
  自体のバリデーションはしない
- **direnv / `.envrc` / `CLAUDE_ENV_FILE` 経由の env は検証 subprocess に届かない**
  (PreToolUse hook には `CLAUDE_ENV_FILE` が渡らない harness 仕様)。回避策は
  [インライン環境変数の伝播](#インライン環境変数の伝播-v070) を参照

## 発火しなかったとき

1. `cat ${CLAUDE_PLUGIN_ROOT}/hooks/hooks.json` でフックが登録されているか確認
2. `python3 ${CLAUDE_PLUGIN_ROOT}/hooks/verify-cloud-account` を stdin 付きで
   手動実行し、対象コマンドで deny JSON が出るかスモーク:
   ```bash
   echo '{"tool_input":{"command":"gh pr list"},"cwd":"/tmp"}' \
     | python3 ${CLAUDE_PLUGIN_ROOT}/hooks/verify-cloud-account
   ```
3. `.claude/verify-cloud-account/accounts.local.json` の JSON 構文エラーを確認
   (破損時は deny)
4. 対象 CLI (`gh` / `firebase` / `aws` / `gcloud` / `kubectl`) が PATH に
   通っているかを確認

hook の出力を確認するには `claude --verbose` でセッションを起動する
(hook の stdout/stderr がターミナルに表示される)。

### デバッグトレース (`VERIFY_CLOUD_ACCOUNT_DEBUG=1`)

hook 実行時に環境変数 `VERIFY_CLOUD_ACCOUNT_DEBUG=1` を立てると、コマンドの
分解結果を stderr に 1 行 JSON で出す (`claude --verbose` で確認可能)。

```json
{"segments": [{"segment": "gh pr list", "service": "github", "readonly": false}],
 "cache_hit": {}, "verify_ms": {"github": 12.3}, "elapsed_ms": 13.1, "decision": "deny"}
```

- `segments`: 抽出した各セグメントと、マッチした service / readonly 判定
- `cache_hit`: 成功 cache を使って verify を省略した service
- `verify_ms`: 実際に `verify()` を呼んだ service とその所要時間 (ms)
- `decision`: 最終的な判定 (`allow` / `deny` / `warn`)

判定表 (allow/deny/warn) 自体には一切影響しない観測専用の出力。

### 内部エラー時の fail-open

`dispatch()` 内で未捕捉の例外が起きた場合、従来は exit 1 (JSON 無し) になり
公式仕様上は non-blocking error として **無音で action が進行する** (fail-open
だが理由が分からない) だけだった。現在は例外を捕捉し、
`additionalContext` で `[verify-cloud-account] 内部エラーのため検証をスキップ
しました: <例外の型>: <メッセージ>` を明示し、同じ理由を stderr にも出す
(`claude --verbose` で確認可能)。いずれの場合も action 自体は fail-open で
進行する (実行を止めない)。

詳細な設計背景は CLAUDE.local.md (開発者向け、リポジトリ未同梱) を参照。

## 互換性

- Python 3.11+ (標準ライブラリのみ)

## テスト実行

```bash
cd hooks/verify-cloud-account
python3 -m unittest discover tests
```

標準ライブラリのみで動く (pytest / pip install 不要)。

クラス単位・メソッド単位で 1 件だけ指定して実行することもできる:

```bash
python3 -m unittest tests.test_services.TestAws.test_match -v
python3 -m unittest tests.test_dispatcher.TestRouting -v
# pytest がインストールされていれば node id 指定も可能
pytest tests/test_services.py::TestAws::test_match -q
```

## ライセンス

MIT
