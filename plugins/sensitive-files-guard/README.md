# sensitive-files-guard

機密ファイル (`.env`, `*.secret`, `*.local.*`, 秘密鍵, 証明書, クレデンシャル) が
Claude Code セッション経由で漏れる事故を、1 プラグインで予防する多段 hook セット。

| 事故 | 対応 hook | タイミング |
|---|---|---|
| `Read` で `.env` の **実値** が LLM コンテキストに載る | `redact-sensitive-reads` | `PreToolUse` (Read) |
| `Bash` の `cat .env` / `source .env` で実値が観測される | `redact-sensitive-reads` | `PreToolUse` (Bash) |
| `Edit` / `Write` で機密パスに書き込み | `redact-sensitive-reads` | `PreToolUse` (Edit/Write) |
| `.env` / 秘密鍵を **tracked / untracked** のまま残す | `check-sensitive-files` | `Stop` |

両 hook は同一の `patterns.txt` を共有し、`hooks/_shared/` に集約された matcher
ロジックで判定が剥離しない構成。

## 関連ドキュメント

- **[docs/DESIGN.md](./docs/DESIGN.md)** — 設計原則、Phase 0 実測結果、既知制限、
  責務境界、`LENIENT_MODES` 方針
- **[docs/MATRIX.md](./docs/MATRIX.md)** — 判定結果の完全マトリクス (6 mode 列)
- **[docs/PATTERNS.md](./docs/PATTERNS.md)** — `patterns.txt` / `patterns.local.txt`
  の仕様と設定例
- **[CLAUDE.md](./CLAUDE.md)** — 保守者向け実務ガイド (テスト、リリース、CLI
  再実測 Runbook)
- **[CHANGELOG.md](./CHANGELOG.md)** — 全バージョンのリリースノート

## インストール

```bash
/plugin marketplace add Mao-o/cc-mp-worktools
/plugin install sensitive-files-guard@mao-worktools
```

有効化すると `PreToolUse(Read | Bash | Edit | Write)` / `Stop` の hook が自動
登録される (`settings.json` の手動編集不要)。

> **MultiEdit**: 現行 Claude Code CLI (2.1.x) には `MultiEdit` tool が搭載されて
> いないため、本 plugin の `hooks.json` からも matcher を除外している。Edit の
> `replace_all` オプションで同等の複数箇所書き換えがカバーされる仕様。将来
> MultiEdit が再搭載された場合、handler (`handlers/edit_handler.py`) と argparse
> choices は既に対応しているため、`hooks.json` に matcher を 1 エントリ追加する
> だけで復活できる。

## 挙動の要約

コマンド / 操作別の deny / allow / ask は [docs/MATRIX.md](./docs/MATRIX.md) に
完全マトリクスがある。要約:

### `PreToolUse(Read)` — redact-sensitive-reads

Claude が `Read` で機密パターン一致のファイルを開こうとすると:

1. 通常ファイル → `deny` + `permissionDecisionReason` に **鍵名・順序・型・
   件数のみ** を返す
2. symlink / FIFO / 特殊ファイル → `ask` (bypass モード下は `deny`)
3. 32KB 超の大ファイル → streaming で鍵名のみ抽出

返却される reason の形:

```
<DATA untrusted="true" source="redact-hook" guard="sfg-v1">
NOTE: sanitized data from a sensitive file. Real values are NOT in context.
file: .env
format: dotenv
entries: 3
keys (in order):
  1. DATABASE_URL  <type=str>
  2. JWT_SECRET    <type=jwt>
  3. DEBUG         <type=bool>
note: all values and comments removed for safety.
</DATA>
```

値は一切含まれない (bool/null も型のみ)。

### `PreToolUse(Bash)` — redact-sensitive-reads

**三態判定** (deny / ask_or_allow / allow) で静的解析する:

- **deny 固定**: 機密 path 一致 / glob 候補列挙 hit / 前置き剥がし後の確定 match /
  `< target` の target が機密。bypass / auto / plan を含めて全 mode で block
- **ask_or_allow**: 静的解析不能ケース。`default` / `acceptEdits` / `dontAsk` で
  は `ask` (ユーザー介在)、`auto` / `bypassPermissions` / `plan` では `allow`
  (autonomous / plan 実行で日常コマンドが止まるのを避ける)
- **allow**: 全 operand が非機密

詳細なコマンド別挙動は [docs/MATRIX.md](./docs/MATRIX.md) 参照。

**False positive の注意**: unified operand scan は「コマンドが実際に file を
読むかどうか」を判別しないため、`echo .env` `ls .env` のように文字列表示だけの
呼び出しでも operand が機密パターンに一致すれば deny される。
`cat *.json` のような glob も既定 rules の `credentials*.json` と交差するため
deny される。許可したい場合は `patterns.local.txt` に `!<basename>` を追加する
([docs/PATTERNS.md](./docs/PATTERNS.md))。

### `PreToolUse(Edit | Write)` — redact-sensitive-reads

`tool_input.file_path` が機密パターン一致なら **新規/既存問わず deny 固定**。
書き込み経路から機密データが混入/置換される事故を防ぐ (ask を挟まない、
実機観測でうっかり承認による既存値喪失が発生した教訓から)。

dotenv 系 (`.env` / `.env.*` / `*.envrc`) を Edit/Write で block した際は、
`tool_input` から追加予定のキー名を抽出して reason に代替案として添える。
値そのものは含まれない (キー名のみ)。

#### Read と Edit/Write の symlink 対応の非対称性

| tool | 機密 + symlink | 理由 |
|---|---|---|
| `Read` | `ask_or_deny` (非 bypass は ask) | symlink 先が意図した参照 (共有 template / 外部参照) の可能性がある。ユーザー介在で判断 |
| `Edit` / `Write` | **`deny` 固定** | 書き込み先が意図せず外部 path を向くと実害が不可逆。ask なしで block |

### `Stop` — check-sensitive-files

応答が終わるたびに cwd が git 管理下なら、**tracked / untracked を問わず** 機密
パターンに一致するファイルを検出して `decision: block` で Claude に再確認を促す。

- **tracked**: `.gitignore` 済みでも block される (`git rm --cached` が必要な
  ため)。対応は「`.gitignore` に追加 + `git rm --cached <path>`」
- **untracked**: `.gitignore` 済みのものは `git ls-files --others --exclude-standard`
  により既に除外済み。対応は「`.gitignore` に追加 or 意図的に管理対象化」
- **submodule**: 0.2.0 以降、`git ls-files --recurse-submodules` で submodule 内の
  **tracked** も検査対象。submodule 内の **untracked** は現状範囲外

block reason には tracked / untracked を別セクションで列挙し、それぞれ対応手順
を添える。

**注意**: 2 回目以降の `Stop` は `stop_hook_active=true` で素通りする (無限ループ
防止)。**block が見えたら必ず対応する**。無視して次のターンに進むと、以降は
チェックが効かなくなる。

## パターン設定

ユーザー個別のパターンは plugin を fork せずに patterns.local.txt に書ける
(0.4.0 から 2-tier lookup):

- **優先**: `~/.claude/sensitive-files-guard/patterns.local.txt`
- **fallback (deprecated)**: `$XDG_CONFIG_HOME/sensitive-files-guard/patterns.local.txt`
  または `~/.config/sensitive-files-guard/patterns.local.txt` (XDG 未設定時)。
  **0.6.0 で削除予定** (fallback 採用時に deprecation 通知が出る)。

両 hook が自動で合流。last-match-wins (gitignore 風)、既定 case-insensitive。

詳細な設定例・false positive 対策・`_detect_format` との同期は
[docs/PATTERNS.md](./docs/PATTERNS.md) 参照。

## 既知制限 (要点)

詳細は [docs/DESIGN.md](./docs/DESIGN.md) の既知制限セクション参照。

1. **MCP 経路は対象外** — MCP server 経由のアクセスは hook が介在しない
2. **Bash 間接アクセスは autonomous / plan で allow** — `bash -c`, `eval`,
   heredoc, process substitution, `/bin/cat`, `./script` 等は静的解析不能のため
   autonomous / plan モードでは allow (日常コマンドを止めない方針)
3. **TOCTOU 完全排除は非目的** — fd ベース reader により「同一プロセス内の
   再 open」race は排除済みだが、hook 読取と Claude 実 Read/Write の分離は範囲外
4. **Windows は現状 fail-closed で deny exit** — SIGALRM 非対応のため
5. **`!` プレフィックス (Claude Code bash mode) は対象外** — ユーザー明示操作で
   `! cat .env` を実行した場合は stdout が transcript に追加される (hook 介在外)

## Fail-closed vs fail-open

| hook | 機密検出時 | 判定不能時 | 備考 |
|---|---|---|---|
| `redact-sensitive-reads` (Read) | **deny** + minimal info | **ask_or_deny** | non-bypass は ask、bypass は deny |
| `redact-sensitive-reads` (Edit/Write) | **deny 固定** | **ask_or_deny** | ask を挟まない |
| `redact-sensitive-reads` (Bash) | **deny 固定** | **ask_or_allow** | default/acceptEdits/dontAsk は ask、auto/bypass/plan は **allow** |
| `redact-sensitive-reads` (Bash, patterns.txt 読込失敗) | — | **deny 固定** | policy 欠如時は全 mode block |
| `check-sensitive-files` (Stop) | `decision: block` | **fail-open** (exit 0 + 空出力) | patterns.txt 読込失敗時は stderr warning のみ |

## 設計上のトレードオフ

- **Vibe Coder の誤操作予防**が目的。敵対的防御 (prompt injection, 悪意ある
  agent) は非目的
- 完全な情報遮断ではない。basename と鍵名は LLM に見える
- TOCTOU race は完全には防げない
- Python 3.11+ / Git 1.7+ / macOS / Linux 対応 (Windows 非対応)

## テスト

```bash
# redact-sensitive-reads (411 tests)
cd hooks/redact-sensitive-reads
python3 -m unittest discover tests

# check-sensitive-files (27 tests)
cd hooks/check-sensitive-files
python3 -m unittest discover tests
```

## ログ

`redact-sensitive-reads` の動作ログは `~/.claude/logs/redact-hook.log` に
書かれる (plugin cache が消えても残るよう `$HOME` 側に固定)。ログには鍵名・
パス・値を一切書かない (エラー種別・classify 結果のみ)。

## 互換性

- Claude Code CLI 2.1.100+ 想定
- Python 3.11+ 想定 (標準ライブラリのみ、`pip install` 不要)
- Git 1.7+ (submodule scan 用)
- macOS / Linux 対応、Windows 非対応 (現状 fail-closed で deny)
