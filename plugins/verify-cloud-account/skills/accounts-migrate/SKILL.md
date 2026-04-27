---
name: accounts-migrate
description: |
  verify-cloud-account の旧配置パス (`.claude/accounts.local.json` /
  `.claude/accounts.json`) を新パス
  (`.claude/verify-cloud-account/accounts.local.json`) へ builder 経由で
  統合する。builder は 3-tier パス解決・merge 戦略・値衝突検出を一元化する
  ため、動作の安定やフォーマット統一の観点から手動コピーより安全で一貫した
  結果になる。v0.3.0 で配置パスが変わり、dispatcher は複数パス存在時に
  fail-closed で deny する (D4) ため、旧プロジェクトで作業を再開するときは
  このスキルで統合してから進める。
  Use when: verify-cloud-account が「複数のパスに accounts.local.json が存在
  します」と deny した、旧パス (`.claude/accounts.local.json` /
  `.claude/accounts.json`) から新パスへ統合したい、配置パス移行の deprecation
  案内を受け取った場合。
  Triggers: "accounts.local.json 統合", "accounts-migrate",
  "複数のパスに accounts.local.json", "旧パス 移行", "fail-closed deny 統合",
  "verify-cloud-account 3-tier lookup", "/verify-cloud-account:accounts-migrate"
allowed-tools:
  - Bash
  - AskUserQuestion
metadata:
  author: mao
  version: "0.3.1"
---

# accounts-migrate

旧配置パスの accounts.local.json を新パスへ統合するスキル。builder は 3-tier
パス解決と merge 戦略を一元化し、値衝突時は自動マージせず deny して手動解決を
促す。v0.3.0 で配置パスが変わり、dispatcher は複数パス存在時に fail-closed で
deny する (D4) ため、旧プロジェクトで作業を再開するときはこのスキルで統合して
から進める。

## 前提

- **accounts.local.json の編集は builder 経由で行う**。動作の安定と
  フォーマット統一のため、Claude は Read / Write / Bash(cat|mv|cp) で
  直接触らない。
- stdout は最初は値隠蔽で確認し、必要なら AskUserQuestion の承認を経て
  `--show-values` で再実行する。

## 3-tier lookup

builder は以下の順でパスをスキャンし、統合する:

1. **new**: `.claude/verify-cloud-account/accounts.local.json` (推奨)
2. **deprecated**: `.claude/accounts.local.json`
3. **legacy**: `.claude/accounts.json`

統合ルール:

- 新パス優先 (new のキーはそのまま残る)
- 旧パスにだけあるキーは new にマージ
- **同一キーで値が衝突する場合は deny** (自動マージは危険のため手動解決を促す)

## 実行フロー

1. **dry-run で proposal を表示**:

   ```bash
   python3 ${CLAUDE_PLUGIN_ROOT}/hooks/verify-cloud-account/scripts/accounts_builder.py migrate --dry-run
   ```

2. stdout を読み、以下のケース分岐:

   - **`nothing to migrate`**: 新パスのみ存在または全パス無し。ユーザーに
     「移行作業は不要」と伝えて終了。
   - **`error: 同一キーで値が衝突しています`** (exit 1): 新旧で値が食い違う。
     `AskUserQuestion`:
     - question: 「衝突したキーの具体値を表示して確認しますか?」
     - options: `値を表示して原因特定する (Recommended)` / `表示せず手動解決` /
       `キャンセル`
     - 「表示」選択時は `--show-values --dry-run` で再実行。
     - ユーザーに「新旧どちらが正しいかを判断し、間違っている側のファイルを
       手動で削除するか値を合わせてから再実行」と案内。
   - **正常な merge proposal**: `+ merged from deprecated/legacy: <key>` が並ぶ
     stdout。`AskUserQuestion`:
     - question: 「統合内容を値込みで確認しますか?」
     - options: `値を表示して確認する (Recommended)` / `確認せずコミット` /
       `キャンセル`

3. ユーザー選択に応じて:
   - 「表示」→ `--show-values --dry-run` で再実行、stdout をユーザーに読ませて
     さらに `AskUserQuestion` 「この内容でコミットしますか?」
     (options: `コミット` / `キャンセル`)
   - 「コミット」→ step 4 へ
   - 「キャンセル」→ 処理中断

4. コミット実行:

   ```bash
   python3 ${CLAUDE_PLUGIN_ROOT}/hooks/verify-cloud-account/scripts/accounts_builder.py migrate --commit
   ```

5. stdout に「旧パスは保持されています」と出る。ユーザーに以下を伝える:
   - 新パス (`.claude/verify-cloud-account/accounts.local.json`) に統合完了
   - 旧パスは**自動削除されない** (安全側のため)
   - 不要なら手動削除する (`rm .claude/accounts.local.json` など)

6. **CLAUDE.md 自動同梱の確認** — `--commit` 成功時、builder は同ディレクトリに
   `CLAUDE.md` (Claude 向け signpost) を自動生成する (既存の場合はスキップ、
   stdout に `created:` または `(skipped: ... already exists)` の 1 行が
   出る)。これは将来のセッションで Claude が `accounts.local.json` を直接
   編集しようとして deny されたとき、同ディレクトリの CLAUDE.md を見て
   builder 経由の正規経路に辿り着けるようにするためのファイル。ユーザーに
   「不要なら削除可、編集も可。verify-cloud-account の動作には影響しない」
   と一言添えること。

## 注意

- migrate は書込対象が `.claude/verify-cloud-account/accounts.local.json` に
  固定 (D2)。argv で別パスへの書込を指定することはできない。
- 旧パスが壊れた JSON を含む場合、migrate は読込 error で exit 1。ユーザーに
  「旧パスを手動で修正するか削除してから再実行」と案内する。
