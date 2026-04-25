---
name: accounts-show
description: |
  verify-cloud-account の accounts.local.json の期待値と CLI アクティブ値の
  diff を builder スクリプト経由で表示する。builder は 3-tier パス解決・
  競合検出・CLI 現在値の突合を一元化するため、動作の安定やフォーマット統一
  の観点から手動で突き合わせるより一貫した結果になる。stdout は既定で値を
  表示せず、AskUserQuestion で承認されたときだけ `--show-values` で露出する。
  Use when: 現在の accounts.local.json 設定を確認したい、想定アカウントと
  CLI アクティブ値の diff を見たい、不一致の原因を調査したい、複数パス競合
  (D4) で検証が止まった後に状況を確認したい場合。
  Triggers: "accounts.local.json を確認", "想定アカウント 現在値",
  "accounts 確認", "accounts-show", "cloud account 検証 状態",
  "accounts.local.json diff", "/verify-cloud-account:accounts-show"
allowed-tools:
  - Bash
  - AskUserQuestion
metadata:
  author: mao
  version: "0.3.0"
---

# accounts-show

verify-cloud-account の accounts.local.json を builder 経由で参照し、各 service
の期待値と CLI アクティブ値を突き合わせるスキル。builder は 3-tier パス解決、
競合検出、CLI 現在値との一致判定を一元化する。

## 前提

- **accounts.local.json の参照は builder 経由で行う**。動作の安定と
  フォーマット統一のため、Claude は Read / Bash(cat) で直接読まない。
- stdout は最初は値隠蔽で確認し、必要なら AskUserQuestion の承認を経て
  `--show-values` で再実行する。

## 引数

`$ARGUMENTS` に service 名を 1 つ受け取ると絞り込み表示。省略時は全 service
を表示。

## 実行フロー

1. **値隠蔽で show** を実行:

   ```bash
   python3 ${CLAUDE_PLUGIN_ROOT}/hooks/verify-cloud-account/scripts/accounts_builder.py show [--service <svc>]
   ```

   stdout の各行には `<service>: <hidden>  [match]` / `[mismatch]` /
   `[CLI unavailable or not logged in]` などの状態が出る。
   この時点で値そのものは表示されない。

2. stdout を読み、以下のいずれかの状態をユーザーに報告:
   - 全 service `[match]` → 「想定通り。変更は不要」と伝えて終了。
   - 1 つでも `[mismatch]` → `AskUserQuestion`:
     - question: 「不一致が検出されました。値を表示して詳細を確認しますか?」
     - options:
       - `値を表示して確認する (Recommended)`
       - `値を確認せず終了する`
   - `[CLI unavailable]` → ユーザーに「対象 CLI (gh/firebase/...) の
     未ログインまたは未インストール」を伝え、適切な login/install 手順を
     案内する。

3. 「値を表示」選択時に `--show-values` で再実行:

   ```bash
   python3 ${CLAUDE_PLUGIN_ROOT}/hooks/verify-cloud-account/scripts/accounts_builder.py show [--service <svc>] --show-values
   ```

   stdout に `<service>: <expected>  [mismatch] current=<current>` の形で
   期待値と現在値が出る。ユーザーに「CLI の切り替え」か「accounts.local.json
   の更新」のどちらを行うか相談する。

## 複数パス競合時

show が exit 1 で「複数のパスに accounts.local.json が存在します」と stderr
に出した場合、**accounts-migrate スキル** (`/verify-cloud-account:accounts-migrate`)
を使って統合するようユーザーに案内する (D4: fail-closed)。
