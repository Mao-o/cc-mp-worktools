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
sensitive-files-guardrail 等で `accounts.local.json` への直接アクセスが deny
された Claude (LLM) が、同ディレクトリの CLAUDE.md を覗くだけで builder
経由の正規経路に辿り着けるようにするための案内ファイル。既存 CLAUDE.md
は上書きされず、削除しても plugin 本体の動作には影響しない。

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

## 対象コマンドと検証スキップ

### 発火するコマンド

各サービスの CLI が含まれるコマンドはすべて発火する。以下のチェーン・ラッパは
自動で解析・剥がしたうえで CLI 部分だけを照合する:

- **コマンドチェーン**: `&&` / `||` / `;` / `|` / 改行 (quote / $() / backtick 内は保護)
- **先頭の環境変数割当**: `FOO=bar gh ...`
- **透過的 wrapper**: `sudo` / `time` / `nohup` / `command` / `builtin` / `exec` /
  `env [KEY=val...]` (ただし `env -i` / `env --` など option 付きは不透明扱い)
- **ランタイム / パッケージマネージャ**: `npx` / `pnpm exec` / `pnpm dlx` /
  `mise exec --` / `bun x`
- 行頭インライン env の伝播は wrapper の実行時 env 挙動に従う
  (`sudo` は preserve 無しだと env を scrub。詳細は
  [`docs/wrapper-env-audit.md`](docs/wrapper-env-audit.md))
- 上記は多段ネストにも対応 (例: `sudo time mise exec -- firebase deploy` →
  `firebase deploy` として検証)

| サービス | マッチ対象 | 期待値の取得 |
|---|---|---|
| GitHub | `gh ...` | `gh auth status` のアクティブアカウント |
| Firebase | `firebase ...` | `firebase use` → fallback `.firebaserc` |
| AWS | `aws ...` | `aws sts get-caller-identity --query Account` |
| GCP | `gcloud ...` | `gcloud config get-value project` (+ optional `account`) |
| Kubernetes | `kubectl ...` | `kubectl config current-context` |

### 検証をスキップする readonly コマンド

「アカウント設定のための状態確認」でデッドロックしないよう、以下は素通し:

- `gh auth status` / `gh auth list`
- `firebase use` (引数なし)
- `aws sts get-caller-identity`
- `gcloud auth list` / `gcloud config get-value project` / `... account`
- `kubectl config current-context` / `... get-contexts` / `... view` /
  `... get-clusters` / `... get-users` / `kubectl cluster-info`
- **情報系コマンド** (各 CLI の `--version` / `--help` / `version` / `help`) —
  アカウント検証不要。診断で打つ `command aws --version` 等が誤って検証対象に
  なり deny されるのを防ぐ (v0.7.0)

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

期待値**以外**への切替は従来どおり通常検証に落ち、切替後の write 操作も次回 hook で
再検証されるため fail-closed は維持される。AWS は切替が `export AWS_PROFILE=...`
(シェル組込のため hook 対象外) で、期待値 (Account ID) と profile 名の照合も hook
からは不能なため、この特例の対象外。

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

## パフォーマンス (短期キャッシュ)

PreToolUse は Bash の度に発火するため、`gh pr list && gh pr view && gh pr comment`
のような連打で毎回 `gh auth status` (〜500ms) や `aws sts get-caller-identity`
(〜1-3s) を走らせるとストレスになる。そのため **検証成功を 30 秒キャッシュ** する。

- 保存先: `$TMPDIR/cc-mp-verify-cloud-account/<sha256>.json`
- 無効化: TTL 経過 / `accounts.local.json` の mtime 変化 / ファイル破損
- **失敗 (deny) 状態はキャッシュしない** — 切り替え後は即座に再検証が走る

## 既知の制限

- `gh auth status` / `firebase use` / `aws sts` / `gcloud config` /
  `kubectl config` の**出力フォーマット**に依存している。CLI 本体の
  major update で壊れる可能性あり
- `bash -c '...'` / `eval` 内に埋め込まれた CLI 呼び出しは静的解析できず検証
  対象外 (透過 wrapper のリストに `bash` は含めていない)
- `kubectl --context foo ...` の `--context` オプション指定による実行時
  コンテキスト override は検出しない (現在のデフォルトコンテキストだけ照合)
- subshell 内のコマンド (`FOO=$(gh ...) cmd` の内側の gh) は検証対象外
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

詳細な設計背景は CLAUDE.local.md (開発者向け、リポジトリ未同梱) を参照。

## テスト実行

```bash
cd hooks/verify-cloud-account
python3 -m unittest discover tests
```

標準ライブラリのみで動く (pytest / pip install 不要)。

## ライセンス

MIT
