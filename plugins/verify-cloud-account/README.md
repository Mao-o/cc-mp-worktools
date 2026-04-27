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
sensitive-files-guard 等で `accounts.local.json` への直接アクセスが deny
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

同一コマンドチェーン内で readonly と非 readonly が混在する場合
(例: `gh auth status && gh pr list`) は非 readonly セグメントについて検証が走る。

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

詳細なトラブルシュートと設計背景は [CLAUDE.md](./CLAUDE.md) を参照。

## テスト実行

```bash
cd hooks/verify-cloud-account
python3 -m unittest discover tests
```

標準ライブラリのみで動く (pytest / pip install 不要)。

## ライセンス

MIT
