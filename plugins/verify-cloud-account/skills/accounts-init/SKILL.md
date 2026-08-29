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
  "cloud account 検証 初期設定", "/verify-cloud-account:accounts-init",
  "create accounts.local.json", "set up verify-cloud-account",
  "initialize cloud account config", "add expected account"
allowed-tools:
  - Bash
  - AskUserQuestion
metadata:
  author: mao
  version: "0.3.2"
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
- 対象ファイルは **hook (dispatcher) が実際に読むもの**に揃う。cwd 階層に無ければ
  親ディレクトリを遡って探し、解決したパスを出力の先頭に `対象: <パス>` として
  表示する。祖先から継承している階層で `init` を実行すると、cwd 直下のファイルが
  継承中の設定を覆い隠す (記載していない service が一斉に未設定になる) ため
  **exit 2 で拒否**される — 値の変更・削除は `set` / `remove`、この階層専用の設定を
  作る場合だけ `--path <file>` で明示する
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
     しない。ユーザーに以下を案内して終了:
     1. `/verify-cloud-account:accounts-show` で現在の設定値と CLI 値を比較
     2. 変更が必要なら builder の `set` サブコマンドで更新する (accounts.local.json
        を手動編集する必要はない):
        ```bash
        python3 ${CLAUDE_PLUGIN_ROOT}/hooks/verify-cloud-account/scripts/accounts_builder.py set --service <service> --value <new-value> --commit
        ```
        CLI の現在値をそのまま使うなら `--value <new-value>` の代わりに
        `--from-cli` を付ける。dict 値 (GitHub の GHE host 等) の特定
        host/alias だけを追加・上書きしたいときは `--host <host>` を併用する。

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

   `error:` + exit 2 で「祖先ディレクトリから継承しています」と出た場合は、
   その階層に新しいファイルを作ると継承中の設定を覆い隠す。ユーザーに
   「継承元の値を変えるなら `set`、この階層専用の設定を作るなら `--path` で
   明示」を案内し、どちらにするか `AskUserQuestion` で確認する。

6. 書き込まれたパス (stdout 冒頭の `対象:` 行) をユーザーに伝える。
   builder は commit 時に `.gitignore` へのエントリ追加も
   自動で行う (stdout に `updated: ... (... を追加)` の行が出る)。
   `.gitignore` 自体がプロジェクトに存在しない場合だけ自動追加されない
   (builder は `.gitignore` を新規作成しない) ため、その場合のみ手動追加を
   促す。

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
