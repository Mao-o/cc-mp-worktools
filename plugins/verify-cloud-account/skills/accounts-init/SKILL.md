---
name: accounts-init
description: |
  verify-cloud-account の accounts.local.json を builder スクリプト経由で
  対話的に初期化する。builder は CLI 現在値の取得、書込パスの固定、既存
  キーの温存、JSON フォーマットの一貫化を一元管理するため、動作の安定や
  フォーマット統一の観点から手動書込より一貫した結果になる。このスキルは
  dry-run で内容を確認し、AskUserQuestion で承認を得てから `--commit` する
  フローを Claude に提示する。
  Use when: 新規プロジェクトで `accounts.local.json` を作成したい、
  verify-cloud-account を初めて設定する、想定アカウント (github / firebase /
  aws / gcloud / kubectl) を追加したい、CLI の現在値から accounts.local.json
  を起こしたい場合。
  Triggers: "accounts.local.json を作りたい", "verify-cloud-account を設定",
  "accounts-init", "アカウント設定 初期化", "想定アカウント 追加",
  "cloud account 検証 初期設定", "/verify-cloud-account:accounts-init"
allowed-tools:
  - Bash
  - AskUserQuestion
metadata:
  author: mao
  version: "0.3.1"
---

# accounts-init

verify-cloud-account plugin の accounts.local.json を builder スクリプト
(`scripts/accounts_builder.py`) 経由で初期化するスキル。builder は CLI 現在値
取得、書込パスの固定、既存キーの温存、JSON フォーマットの一貫化を一元管理
する。

## このスキルが担うこと

- builder の対話フロー (dry-run → AskUserQuestion 承認 → commit) を制御する
- 書込先パス (`.claude/verify-cloud-account/accounts.local.json`)、JSON の
  インデント・改行・ソート順、既存キーの扱いは builder 内部で固定されており、
  手動編集より一貫した結果になる
- stdout は既定で値を表示しない。明示の `--show-values` を付けたときだけ
  露出する (AskUserQuestion で承認を得てから切り替える)

## 前提

- **accounts.local.json の編集は builder 経由で行う**。動作の安定と
  フォーマット統一のため、Claude は Read / Write / Edit / Bash(cat|ls) で
  直接触らない。
- stdout は最初は値隠蔽で確認し、必要なら AskUserQuestion の承認を経て
  `--show-values` で再実行する。

## 引数

`$ARGUMENTS` に service 名を 1 つ受け取る
(`github` / `firebase` / `aws` / `gcloud` / `kubectl`)。省略時は対話で選ぶ。

## 実行フロー

1. `$ARGUMENTS` を確認。有効な service 名が含まれていなければ
   `AskUserQuestion`:
   - question: 「どの service の accounts.local.json エントリを初期化しますか?」
   - options: `github` / `firebase` / `aws` / `gcloud` / `kubectl` / `キャンセル`

2. **値なしで dry-run** を実行して proposal を確認:

   ```bash
   python3 ${CLAUDE_PLUGIN_ROOT}/hooks/verify-cloud-account/scripts/accounts_builder.py init --service <service> --dry-run
   ```

   stdout を読み、どのキーが `+ add` / `= unchanged` / `! skipped` かだけ確認
   (値はまだ表示されていない)。

3. proposal に応じて分岐:

   - **add** のとき `AskUserQuestion`:
     - question: 「init proposal の具体値を stdout に表示して確認しますか?」
     - options:
       - `値を表示して確認する (Recommended)`
       - `値を確認せずそのままコミットする`
       - `キャンセル`
   - **unchanged** のとき: 既にコミット済み。ユーザーに「変更は不要」と
     伝えて終了。
   - **skipped** のとき: 既存値と異なる値が提案された。init は overwrite
     しない。ユーザーに「既存値を変えたい場合は一度手動で
     accounts.local.json をクリアするか、将来の switch サブコマンドを使う」
     と伝えて終了。

4. ユーザー選択に応じて:
   - 「値を表示」→ `--show-values --dry-run` で再実行:
     ```bash
     python3 ${CLAUDE_PLUGIN_ROOT}/hooks/verify-cloud-account/scripts/accounts_builder.py init --service <service> --dry-run --show-values
     ```
     表示後に再度 `AskUserQuestion`: 「この値でコミットしますか?」
     (options: `コミット` / `キャンセル`)
   - 「表示せずコミット」→ 直接 step 5 へ
   - 「キャンセル」→ 処理中断

5. コミット実行:
   ```bash
   python3 ${CLAUDE_PLUGIN_ROOT}/hooks/verify-cloud-account/scripts/accounts_builder.py init --service <service> --commit
   ```
   (既定では `--show-values` なし。commit の stdout も値隠蔽)

6. 書き込まれたパス (`.claude/verify-cloud-account/accounts.local.json`) を
   ユーザーに伝え、`.gitignore` にまだ入れていなければ追加を促す。

7. **CLAUDE.md 自動同梱の確認** — `--commit` 成功時、builder は同ディレクトリに
   `CLAUDE.md` (Claude 向け signpost) を自動生成する (既存の場合はスキップ、
   stdout に `created:` または `(skipped: ... already exists)` の 1 行が
   出る)。これは将来のセッションで Claude が `accounts.local.json` を直接
   編集しようとして deny されたとき、同ディレクトリの CLAUDE.md を見て
   builder 経由の正規経路に辿り着けるようにするためのファイル。ユーザーに
   「不要なら削除可、編集も可。verify-cloud-account の動作には影響しない」
   と一言添えること。

## エラーハンドリング

builder が exit 1 で失敗する主なケース:

- **既存 JSON が壊れている** → stderr に「手動で修正してから再実行」と出る。
  ユーザーに原因を伝え、手動修正を依頼 (Claude は JSON を書き換えない)。
- **CLI が未ログイン / 未インストール** (`--value` 省略時の suggestion 失敗)
  → stderr に理由が出る。`gh auth login` / `firebase login` 等の手順を
  ユーザーに案内する。
- **書き込み失敗 (権限等)** → stderr に原因が出る。ユーザーに伝える。
