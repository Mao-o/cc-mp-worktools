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

## 使い方

### 1. プロジェクトルートに期待アカウントを書く

`.claude/accounts.local.json` にプロジェクト専用のアカウント名を記述する:

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

### 2. `.gitignore` に追加する

`accounts.local.json` はローカル専用のため、コミット対象にしない:

```gitignore
# .gitignore
.claude/accounts.local.json
```

> **旧名 `accounts.json`**: 古いプロジェクトで `.claude/accounts.json` を使っていた場合、
> hook は下位互換で検出する (検証は通しつつ warn でリネームを促す)。新規は必ず
> `accounts.local.json` を使うこと。

### 3. 通常のコマンドを叩く

想定値と一致しているかは hook が自動で CLI から現在値を取って照合する。
一致していれば透過、不一致なら deny:

```
$ gh pr create
✖ Bash hook denied:
  GitHub アカウント不一致: 現在=some-other, 期待=Mao-o
  — 切り替え: gh auth switch --user Mao-o
```

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

## `accounts.local.json` が未設定のとき

対象サービスのコマンドを叩くと deny になり、セットアップ手順を提示する:

```
.claude/accounts.local.json が未設定です。
gh auth status で現在のアカウントを確認し、以下で作成してください:
mkdir -p .claude && echo '{"github":"YOUR_ACCOUNT"}' > .claude/accounts.local.json
...
```

設定したくない (= 検証対象から外したい) プロジェクトでは、この plugin を
**そのプロジェクトのみ無効化** するか、CLI の readonly コマンドのみを使う。

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
3. `.claude/accounts.local.json` の JSON 構文エラーを確認 (破損時は deny)
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
