# verify-cloud-account

Bash コマンド実行の直前に、クラウド CLI (`gh` / `firebase` / `aws` / `gcloud`) の
**アクティブアカウントがプロジェクトの想定値と一致するか** を検証する hook。
複数アカウントを切り替えて作業するときに、間違ったアカウントで
`gh pr create` / `firebase deploy` / `aws s3 ...` / `gcloud run deploy` 等を
実行する事故を防ぐ。

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
  "gcloud":   "my-gcp-project"
}
```

必要なキーだけ書けばよい (例: GitHub と Firebase しか使わないなら `github` と
`firebase` だけで OK)。未記載のサービスコマンドは検証対象外 (= allow)。

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

想定値と一致しているかは `hook` が自動で CLI から現在値を取って照合する。
一致していれば透過、不一致なら deny:

```
$ gh pr create
✖ Bash hook denied:
  GitHub アカウント不一致: 現在=some-other, 期待=Mao-o
  — 切り替え: gh auth switch --user Mao-o
```

## 対象コマンドと検証スキップ

### 発火するコマンド (先頭マッチ)

| サービス | 先頭パターン | 期待値の取得 |
|---|---|---|
| GitHub | `gh ...` | `gh auth status` のアクティブアカウント |
| Firebase | `firebase ...` / `npx firebase` / `pnpm exec firebase` / `mise exec -- firebase` | `firebase use` → fallback `.firebaserc` |
| AWS | `aws ...` | `aws sts get-caller-identity --query Account` |
| GCP | `gcloud ...` | `gcloud config get-value project` |

### 検証をスキップする readonly コマンド

「アカウント設定のための状態確認」でデッドロックしないよう、以下は素通し:

- `gh auth status` / `gh auth list`
- `firebase use` (引数なし)
- `aws sts get-caller-identity`
- `gcloud auth list`
- `gcloud config get-value project` / `gcloud config get-value account`

## `accounts.local.json` が未設定のとき

`.claude/accounts.local.json` が存在しないプロジェクトで対象コマンドを叩くと
deny になり、セットアップ手順を提示する:

```
accounts.local.json が未設定です。gh auth status で現在のアカウントを確認し、
以下で作成してください:
mkdir -p .claude && echo '{"github":"YOUR_ACCOUNT"}' > .claude/accounts.local.json
```

設定したくない (= 検証対象から外したい) プロジェクトでは、この plugin を
**そのプロジェクトのみ無効化** するか、CLI の readonly コマンドのみを使う。

## 既知の制限

- `npx gh` / `pnpm exec gh` 等の `gh` ラッパ経由は未対応 (Firebase のみラッパ対応)。
  必要であれば `hooks/verify-cloud-account/services/github.py` の `PATTERNS` を
  拡張する
- GitHub CLI のアクティブアカウント判定は `gh auth status` の出力形式に依存する。
  `gh` 本体のバージョンアップで出力フォーマットが変わった場合は破綻する
- Firebase の `.firebaserc` fallback は `projects.default` のみ見る
  (alias 切り替えは `firebase use` 経由でのみ反映)

## 発火しなかったとき

1. `cat ${CLAUDE_PLUGIN_ROOT}/hooks/hooks.json` でフックが登録されているか確認
2. `python3 ${CLAUDE_PLUGIN_ROOT}/hooks/verify-cloud-account` を stdin 付きで
   手動実行し、対象コマンドで deny JSON が出るかスモーク
3. `.claude/accounts.local.json` の JSON 構文エラーを確認 (破損時は deny になる)
4. `aws` / `gcloud` CLI が PATH に通っているかを確認

詳細なトラブルシュートと設計背景は [CLAUDE.md](./CLAUDE.md) を参照。

## ライセンス

MIT
