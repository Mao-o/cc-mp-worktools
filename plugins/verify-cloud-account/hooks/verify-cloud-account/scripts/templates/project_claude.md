# verify-cloud-account / accounts.local.json (Claude 向け案内)

このディレクトリ配下の `accounts.local.json` は verify-cloud-account の設定
ファイルです。**編集は builder スクリプト経由 (Bash) が正規経路**です
(書込パス固定・フォーマット統一のため)。

`*.local.json` のような機密ファイルパターンへの直接アクセスを制限するセキュリティ
系 hook が有効な環境では、Claude (LLM) からの `Read` / `Write` / `Edit` および
`cat` 経由の参照が拒否されることがあります。その場合も builder 経由であれば
同じ正規経路で操作できます (Bash の operand に機密ファイルの path を直接含めない
呼び出し方のため、多くのフックの制約を通過します)。

## 編集の正規経路

verify-cloud-account plugin の **builder スクリプト経由 (Bash) が唯一の
正規経路**です。

### Agent Skill (推奨)

| 用途 | スキル |
|---|---|
| 新規作成 / 想定アカウント追加 | `/verify-cloud-account:accounts-init` |
| 現状確認 / CLI 値との diff | `/verify-cloud-account:accounts-show` |
| 旧パスから新パスへ統合 | `/verify-cloud-account:accounts-migrate` |

各 skill は dry-run → AskUserQuestion 承認 → commit の確認フローを内包する
ため、通常はこちらを使います。

### builder スクリプトの直接呼び出し

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/hooks/verify-cloud-account/scripts/accounts_builder.py \
  <init|show|migrate|set|remove> [args...]

# set / remove の引数例
#   set --service <svc> --value <v> [--host <h>] --commit
#   remove --service <svc> [--host <h>] --commit
#   (--host 指定時、削除後に残る entry がその service にとって使えない
#    形になる場合はキーごと削除されます)
```

`${CLAUDE_PLUGIN_ROOT}` は plugin hook 経由で展開される変数です。Agent Skill
から呼び出すと自動展開されます。

## 対象ファイルの決まり方

builder は **dispatcher (hook) が実際に読むファイル**を対象にします。cwd 階層に
`accounts.local.json` が無ければ親ディレクトリを 1 階層ずつ遡り、最初に見つかった
ファイルを編集します (worktree から親 repo の設定を継承している場合はその親の
ファイル)。解決したパスは dry-run / commit の出力の先頭に「対象: ...」として
表示されます。

- 継承中に `init` を実行すると、cwd 直下に作ったファイルが継承中の設定を覆い隠す
  (記載していない service が一斉に未設定になる) ため拒否されます。値の変更・削除は
  `set` / `remove` を使ってください
- この階層専用の設定を意図的に作りたい場合のみ `--path <file>` で対象を明示します
  (指定できるのは dispatcher が読む配置のみ)

## やってはいけないこと

- `Read` / `Edit` / `Write` で `accounts.local.json` を直接操作する
- `cat .claude/verify-cloud-account/accounts.local.json` のように機密ファイルの
  path を Bash operand に直接含めて実行する (セキュリティ系 hook が有効な環境では
  deny されます)
- `.claude/accounts.local.json` (旧パス) と
  `.claude/verify-cloud-account/accounts.local.json` (新パス) の **両方** を
  作る (verify-cloud-account dispatcher が複数パス検出時に fail-closed で
  deny する)

## このファイルについて

このファイルは `accounts_builder.py` の `init` / `set` / `remove` / `migrate`
の `--commit` 初回実行時に自動生成されます。既に存在する場合は上書きされません
(ユーザー編集を尊重)。不要であれば削除して構いません — signpost が消えるだけで
verify-cloud-account 本体の動作には影響しません。
