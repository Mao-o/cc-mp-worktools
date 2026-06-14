# sensitive-files-guardrail

機密ファイル (`.env`, `*.secret`, 秘密鍵, 証明書, クレデンシャル) が
Claude Code セッション経由で漏れる事故を、1 プラグインで予防する多段 hook セット。

> `*.local.json` 等のローカル設定系パターンは **0.14.0 で既定から撤去**
> (`settings.local.json` / `accounts.local.json` のような Claude Code
> エコシステムの個人設定ファイルを誤 block していたため。
> [docs/PATTERNS.md](./docs/PATTERNS.md) に復活レシピあり)。

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
- **[docs/MATRIX.md](./docs/MATRIX.md)** — 判定結果の完全マトリクス (5 mode 列)
- **[docs/PATTERNS.md](./docs/PATTERNS.md)** — `patterns.txt` / `patterns.local.txt`
  の仕様と設定例
- **[CLAUDE.md](./CLAUDE.md)** — 保守者向け実務ガイド (テスト、リリース、CLI
  再実測 Runbook)
- **[CHANGELOG.md](./CHANGELOG.md)** — 全バージョンのリリースノート

## インストール

```bash
/plugin marketplace add Mao-o/cc-mp-worktools
/plugin install sensitive-files-guardrail@mao-worktools
```

有効化すると `PreToolUse(Read | Bash | Edit | Write)` / `Stop` の hook が自動
登録される (`settings.json` の手動編集不要)。

> **MultiEdit**: 現行 Claude Code CLI (2.1.x) には `MultiEdit` tool が搭載されて
> いないため、本 plugin は対応コードを 0.6.0 で撤去した。Edit の `replace_all`
> オプションで同等の複数箇所書き換えがカバーされる仕様。将来 MultiEdit が再
> 搭載された場合は、`handlers/edit_handler.py` の docstring と `__main__.py`
> argparse `choices` / `_dispatch` 分岐に `multiedit` を追加し、
> `_extract_dotenv_keys` に edits 連結ブランチを足してから、`hooks.json` に
> matcher を 1 エントリ追加する。

## 挙動の要約

コマンド / 操作別の deny / allow / ask は [docs/MATRIX.md](./docs/MATRIX.md) に
完全マトリクスがある。要約:

### `PreToolUse(Read)` — redact-sensitive-reads

Claude が `Read` で機密パターン一致のファイルを開こうとすると:

1. 通常ファイル → `deny` + `permissionDecisionReason` に **鍵名・順序・型・
   prefix・長さ・status タグ・placeholder ヒント** を返す (実値は出さない)
2. symlink / FIFO / 特殊ファイル → `ask` (bypass モード下は `deny`)
3. 32KB 超の大ファイル → streaming で鍵名のみ抽出

返却される reason の形 (0.9.0):

```
<DATA untrusted="true" source="redact-hook" guard="guardrail-v1">
NOTE: sanitized data from a sensitive file. Real values are NOT in context.
file: .env
format: dotenv
entries: 6
keys (in order):
  1. DATABASE_URL  <type=url>  <set>  length=42
  2. JWT_SECRET    <type=jwt prefix="ey">  <set>  length=287
  3. STRIPE_KEY    <type=stripe_secret prefix="sk_live_">  <set>  length=68
  4. TOKEN         <type=str>  <set>  <looks_truncated>  length=20
  5. PLACEHOLDER   <type=str>  <placeholder>  matched="your_jwt_secret_here"  length=24
  6. EMPTY_KEY     <type=str>  <empty>
note: real values are not in context. only key names, type, prefix,
length, status tags, and placeholder hints are returned.
</DATA>
```

**実値は一切含まれない**。出されるのは:
- `<type=...>`: 値クラス (str / bool / null / num / jwt / url / email / uuid /
  aws_access_key / stripe_secret / stripe_pk / github_pat / openai_key)
- `prefix="..."`: 識別子型 (jwt / aws / stripe_* / github_pat / openai_key) のみ。
  本番鍵 (`sk_live_`) とテスト鍵 (`sk_test_`) を区別できるためローテーション
  判断に有用
- `<set>` / `<empty>` / `<placeholder>` / `<short>` / `<long>` /
  `<looks_truncated>`: 値の品質状態 (複数併記可)。「JWT なのに 4 文字」
  「placeholder のまま」「末尾 truncated」等を検知してデバッグの次の
  作業を判断できる
- `length=<N>`: 値のバイト長 (生長さ)。「秘密鍵が短すぎる」「ダンプ混入で
  4096 超」等の異常検知に有用
- `matched="..."`: placeholder 一致時の辞書 literal / pattern label

> 思想 2 (block 時は意図を汲んだメッセージを返す) を 0.9.0 で実装。「機密
> ファイルは閲覧禁止」だけでは API 失敗の原因究明が止まるが、上記の品質情報
> があれば「JWT_SECRET が `<placeholder>` のまま → これを実値にセットしないと
> 当然 401」「DATABASE_URL が `<short> length=4` → DSN 文字列が壊れている」
> のように次の作業に直接つなげられる。

> **Unreleased (PR 6, E5)** で同等の status タグ
> (`<set>` / `<empty>` / `<placeholder>` / `<long>` / `<looks_truncated>`)
> + `length` + `matched="..."` を **JSON / TOML の str scalar 値**、および
> **YAML の top-level 抽出** にも横展開する。`<short>` は型クラス (jwt / url 等)
> 前提のため dotenv 限定。bool / num / null / 構造 (array / object) には status
> を出さない (値を持たないため意味がない)。

返却される JSON / TOML の reason 例 (Unreleased):

```
<DATA untrusted="true" source="redact-hook" guard="guardrail-v1">
NOTE: sanitized data from a sensitive file. Real values are NOT in context.
file: config.json
format: json
entries: 3
<object, 3 children>
  api_key  <type=str>  <placeholder>  matched="changeme"  length=8
  retries  <type=num>
  endpoint  <type=str>  <set>  length=24
note: string scalar values are summarized to status tags and length only.
 array/object counts shown; non-string values removed.
</DATA>
```

TOML も同じフォーマットで返る (`format: toml`、内部実装は `_walk` を JSON と
共有)。

返却される YAML の reason 例 (Unreleased):

```
<DATA untrusted="true" source="redact-hook" guard="guardrail-v1">
NOTE: sanitized data from a sensitive file. Real values are NOT in context.
file: secrets.yaml
format: yaml
entries: 2 (top-level)
top-level keys (in order):
  1. database
  2. features
nested entries: 4 (not parsed)
note: nested structure not parsed. only top-level key names returned.
</DATA>
```

> YAML は完全パースしない (anchor / alias / flow style / multi-document は
> 対象外)。top-level の鍵名と nested 件数だけで「設定の規模感」と「主要
> セクション」を伝える設計 (思想 1 = うっかり露出予防の射程、完全な情報遮断
> ではない)。`<nested>` で 1 件カウントするのみで nested の key 名は出さない。

### `PreToolUse(Bash)` — redact-sensitive-reads

**三態判定** (deny / ask_or_allow / allow) で静的解析する:

- **deny 固定**: literal operand が機密パターンに一致、または operand glob が
  dotenv stem (`.env` / `.envrc`) に fnmatch 一致。bypass / auto を含めて全 mode
  で block
- **ask_or_allow**: 静的解析不能ケース (`<` 入力リダイレクト、heredoc / process
  sub / 動的展開 / shell wrapper / 任意 path 実行 等)。`default` /
  `acceptEdits` / `dontAsk` では `ask` (ユーザー介在)、`auto` /
  `bypassPermissions` では `allow` (autonomous 実行で日常コマンドが止まるのを
  避ける)
- **allow**: 全 operand が非機密、または first_token が read-only allow-list
  (`_SAFE_READ_FIRST_TOKENS`、0.12.0 で導入)

詳細なコマンド別挙動は [docs/MATRIX.md](./docs/MATRIX.md) 参照。

> **0.12.0 で read-only first_token allow-list を導入**: ログ実測で
> `bash_classify` の ask 発火の **約 80%** が `>` 出力リダイレクトや `&`
> background を含むコマンド (`segment_residual_metachar_lenient`) 起因だった
> ため、第一トークンが副作用なしの見る・数える系 (`ls cat head tail nl tac bat
> less more view wc file stat du df tree grep egrep fgrep rg ag ack od xxd
> hexdump`) なら residual metachar の ask 経路を **スキップして operand scan
> に直行** する判定を追加。`grep foo README.md > /tmp/out` / `ls > listing` /
> `cat README.md | wc -l > count` のような調査用ワンライナーが allow に倒る。
> 機密 redirect target (`grep foo > .env`) は operand scan で deny 固定、
> hard-stop (`$()` / `<`) は ask 維持で safety net を保つ。`awk` / `sed` /
> `find` / `echo` は副作用持ちうるため allow-list **外**。

> **0.10.0 で Bash deny reason を category 別 dispatch に再編**: 思想 2
> (block 時は意図を汲んだメッセージを返す) を Bash 側でも実装。first_token を
> 9 カテゴリ (`read_full` / `read_partial` / `search` / `mutate` / `load` /
> `move` / `history` / `transfer` / `archive`) にマッピングし、コマンド意図
> ごとの note と代替案を返す (`source .env` なら direnv / dotenv-cli、
> `cp .env backup.env` なら 1Password CLI、`git show HEAD:.env` なら
> `git rm --cached` + rotate、`tar czf b.tar .env` なら `--exclude=.env` 等)。
> deny 時に operand path の dotenv を実 read して Read 同等の minimal info を
> reason 内に `<DATA untrusted>` 包装で埋め込む。grep family では operand から
> env-var 名候補 (`[A-Z][A-Z0-9_]{2,}`) を抽出して dotenv parse 結果と照合し、
> `matched_pattern_keys` / `nomatch_pattern_keys` を出す (E4)。**deny 動作の
> 判定境界は 0.9.0 と完全に同じ**で、reason 文字列の情報量だけが拡張された。

> **0.7.0 で `<` 入力リダイレクトを ask_or_allow に格下げ**: 0.3.4〜0.6.x では
> `cat < .env` / `cat<.env` / `cat 0<.env` 等から target を抽出して deny に
> 倒していたが、escape paren depth tracking など敵対的バイパス対策のコード負債
> が思想 1 (うっかり露出予防が目的、敵対的防御は非目的) と整合しないため
> 0.7.0 で撤廃した。`<` を含む command は他の hard-stop と同じく ``ask_or_allow``
> に倒る。
>
> **0.8.0 で prefix normalize を撤廃**: 0.3.2〜0.7.x では `FOO=1 cat .env` /
> `env cat .env` / `nohup cat .env` / `/usr/bin/env FOO=1 cat .env` を「前置き
> 剥がし後の literal cat .env」と解釈して deny に倒していたが、これらは
> 「うっかり書く形」ではないため思想 1 に整合せず 0.8.0 で撤廃。第一トークンが
> env-assignment / `env` / `command` / `builtin` / `nohup` / 任意 path exec の
> いずれかなら全て ``ask_or_allow`` に倒る。

> **0.14.0 で metadata-only first_token allow-list を導入**: 離脱分析
> (2026-05、transcript 実測) で実 deny 15 件のうち `find -name X` / `ls -la X` /
> `git check-ignore X` のような **所在・属性確認** が 1/3 を占め、いずれも値の
> 露出につながらない操作だった。`ls` / `tree` / `stat` / `file` / `du` / `df` /
> `test` / `wc` / `basename` / `dirname` / `realpath` / `readlink` / `echo` /
> `printf`、および `git check-ignore` / `git ls-files` / `git status`
> (subcommand 直書き形) は operand の内容を stdout に出さないため、機密 operand
> でも **allow** に倒す。`find` は `-exec` / `-delete` 等の内容出力・副作用
> アクションを含まない場合のみ allow (`find -exec cat .env ';'` は deny)。
> 同様に `file -f` / `wc --files0-from` / `tree --fromfile` 等、operand の中身を
> ファイル名リストとして読み echo するオプション付き形も deny (`file .env` /
> `wc -l .env` の通常形は allow)。`git ls-files` は plain path-listing のみ
> allow し、`-s` / `--stage` / `--format` は blob object name (= 内容の指紋) を
> 出せるため deny。`git status` は `-v`/`--verbose` が staged diff (機密の
> 旧値/新値) を出すため allowlist 外 (裸 `git status` は allow、
> `git status -v -- .env` は deny)。`cat` / `head` / `grep` 等の内容出力系と
> `cp` / `mv` (複製で漏洩面が広がる)、`git show` / `git diff` / `git add` は
> 従来通り deny 固定。`echo KEY=val > .env` のような書込み形は residual metachar
> の ask 経路が先に効くため緩まない。metadata-only ∩ safe_read コマンドの
> `ls > .env` 系 redirect 書込みも deny (破壊的書込み)。

**False positive の注意**: unified operand scan は「コマンドが実際に file の
内容を出力するか」までは判別しないため、`cat` / `grep` 等の内容出力系コマンド
では、operand が機密パターンに literal 一致すれば実際の用途を問わず deny される
(0.14.0 で `echo .env` / `ls .env` 等の metadata-only 系は allow に解消済み)。
恒久的に許可したい場合は `patterns.local.txt` に `!<basename>` を追加する
([docs/PATTERNS.md](./docs/PATTERNS.md))。

> **0.8.0 で glob 候補列挙を撤廃**: 0.3.2〜0.7.x では `cat *.json` を既定 rules の
> `credentials*.json` と交差させて deny に倒していたが、思想 1 (うっかり露出予防、
> 敵対的防御は非目的) に対し deny 寄り過ぎる (`cat *.json` `cat *.key` `cat *.log`
> 等の日常 glob まで巻き込む) ため 0.8.0 で撤廃した。現在は operand glob が
> `.env` / `.envrc` literal に ``fnmatchcase`` で一致するときだけ deny 固定で、
> それ以外の glob (`id_rsa*`, `*.key`, `cred*.json`, `*.log` 等) は ``ask_or_allow``
> (default=ask, autonomous=allow) に倒す。

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

ユーザー個別のパターンは plugin を fork せずに patterns.local.txt に書ける:

- `~/.claude/sensitive-files-guardrail/patterns.local.txt` (0.6.0 から単一パス)

> 0.4.0〜0.5.x で fallback として参照していた
> `$XDG_CONFIG_HOME/sensitive-files-guardrail/patterns.local.txt` /
> `~/.config/sensitive-files-guardrail/patterns.local.txt` は **0.6.0 で撤去**。
> 旧パスを使っていた場合は手動で
> `mv "${XDG_CONFIG_HOME:-$HOME/.config}/sensitive-files-guardrail/patterns.local.txt" ~/.claude/sensitive-files-guardrail/patterns.local.txt` する。

両 hook が自動で合流。last-match-wins (gitignore 風)、既定 case-insensitive。

詳細な設定例・false positive 対策・`_detect_format` との同期は
[docs/PATTERNS.md](./docs/PATTERNS.md) 参照。

## 既知制限 (要点)

詳細は [docs/DESIGN.md](./docs/DESIGN.md) の既知制限セクション参照。

1. **MCP 経路は対象外** — MCP server 経由のアクセスは hook が介在しない
2. **Bash 間接アクセスは autonomous / plan で allow** — `bash -c`, `eval`,
   heredoc, process substitution, `/bin/cat`, `./script` 等は静的解析不能のため
   autonomous / plan モードでは allow (日常コマンドを止めない方針)。
   `echo KEY=val > .env` / `cat > .env <<EOF` のような redirect / heredoc
   書込みも同様に通る (本 plugin はセキュリティ担保ではなく、うっかり露出
   予防が主目的。設計判断として受容済み)
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
| `redact-sensitive-reads` (Bash) | **deny 固定** | **ask_or_allow** | default/acceptEdits/dontAsk は ask、auto/bypass は **allow** |
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
# redact-sensitive-reads (674 tests, 0.14.0)
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
