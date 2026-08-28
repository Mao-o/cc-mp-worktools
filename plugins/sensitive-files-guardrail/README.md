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
  責務境界、`LENIENT_MODES` 方針。判断困難な Bash を deny 強制しない根拠は
  [ハーネス委譲方針 (defense-in-depth の一層)](./docs/DESIGN.md#ハーネス委譲方針-defense-in-depth-の一層)
- **[docs/MATRIX.md](./docs/MATRIX.md)** — 判定結果の完全マトリクス (5 mode 列)
- **[docs/PATTERNS.md](./docs/PATTERNS.md)** — `patterns.txt` / `patterns.local.txt`
  の仕様と設定例
- **[docs/MAINTAINING.md](./docs/MAINTAINING.md)** — 保守者向け実務ガイド (テスト、
  validate、リリース手順、CLI 再実測 Runbook、ログ規則)
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

> **0.14.0 (E5)** で同等の status タグ
> (`<set>` / `<empty>` / `<placeholder>` / `<long>` / `<looks_truncated>`)
> + `length` + `matched="..."` を **JSON / TOML の str scalar 値**、および
> **YAML の top-level 抽出** にも横展開済み。`<short>` は型クラス (jwt / url 等)
> 前提のため dotenv 限定。bool / num / null / 構造 (array / object) には status
> を出さない (値を持たないため意味がない)。

返却される JSON / TOML の reason 例 (0.14.0, E5):

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

返却される YAML の reason 例 (0.14.0, E5):

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

PEM / armored 鍵 (`.pem` / `.key` / `id_rsa` など) は専用経路で block の種別と
件数だけを返す (0.23.0):

```
<DATA untrusted="true" source="redact-hook" guard="guardrail-v1">
NOTE: sanitized data from a sensitive file. Real values are NOT in context.
file: id_rsa
format: pem (armored key / certificate)
blocks: 1
block types:
  1. RSA PRIVATE KEY
armored bytes: 1679
note: key material is never parsed or returned. only block labels and counts are shown.
</DATA>
```

> 判定は basename ではなく**内容の sniff** (先頭 40 行以内の `-----BEGIN ...-----`)
> で行う。`id_rsa` のような拡張子なしファイルを basename だけで判別できないため。
> format が既に確定しているケース (`.env` に PEM を値として埋めた形など) には
> 介入せず、その形式のパーサをそのまま使う。

### `PreToolUse(Bash)` — redact-sensitive-reads

**三態判定** (deny / ask_or_allow / allow) で静的解析する:

- **deny 固定**: literal operand が機密パターンに一致、または operand glob が
  shell の展開で dotenv stem (`.env` / `.envrc`) に一致しうる (`.env*` /
  `*/.env`。裸の `*` は dotfile に展開されないので対象外)。bypass / auto を
  含めて全 mode で block。grep 系 / jq / awk / sed の第 1 positional (pattern /
  script) と値が path ではない option の値 (`git log -S.env` /
  `--exclude='.env'`) は operand として数えない (0.22.0)
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
> metadata-only として allow し、`-s` / `--stage` / `--format` は blob object name
> (= 内容の指紋) を出せるため operand scan に回す (機密 operand を伴う
> `git ls-files -s .env` は deny、`git ls-files -s` 単体は allow)。
> `git status` は `-v`/`--verbose` が staged diff (機密の
> 旧値/新値) を出すため allowlist 外 (裸 `git status` は allow、
> `git status -v -- .env` は deny)。`cat` / `head` / `grep` 等の内容出力系と
> `cp` / `mv` (複製で漏洩面が広がる)、`git show` / `git diff` / `git add` は
> 従来通り deny 固定。`echo KEY=val > .env` のような書込み形は residual metachar
> の ask 経路が先に効くため緩まない。metadata-only ∩ safe_read コマンドの
> `ls > .env` 系 redirect 書込みも deny (破壊的書込み)。

> **0.19.0 で次善策コマンドを metadata-only に追加**: 両 hook
> の reason が「tracked なら `git rm --cached <path>` で untrack」「`chmod 600 .env`」
> と案内しながら Bash hook 自身がそれらを deny する自己矛盾があった。
> `git rm --cached` (`--cached` 完全一致。`--no-cached` / `--pathspec-from-file` や
> その省略形を含む「既知の安全な option 以外」が 1 つでもあれば fail-closed で
> 通常経路) は index からの除去のみで実ファイルは残り内容も出ないため allow。`chmod` /
> `chown` / `chgrp` / `touch` は内容を読む option が存在しない (`--reference` /
> `-r` は mode / owner / timestamp のみ) ため allow。plain `git rm` (作業ツリー
> 削除 = 破壊操作) と `git rm --cached --pathspec-from-file=<file>` (中身を pathspec
> として読み不一致行を echo) は deny 維持、`chmod 600 x > .env` の書込み形は echo
> と同じく residual metachar の ask_or_allow のまま。あわせて `git` の deny reason
> を subcommand 別 (show / diff / log = 閲覧、add / rm / mv / restore = 操作) に
> 分け、`git rm .env` が「閲覧しようとした」と返していた誤った意図文を解消した。

**False positive の注意**: unified operand scan は「コマンドが実際に file の
内容を出力するか」までは判別しないため、`cat` / `grep` 等の内容出力系コマンド
では、operand が機密パターンに literal 一致すれば実際の用途を問わず deny される
(0.14.0 で `echo .env` / `ls .env` 等の metadata-only 系は allow に解消済み)。
恒久的に許可したい場合は `patterns.local.txt` の
`[project:<プロジェクトの絶対パス>]` セクション配下に `!<basename>` を追加する
(全プロジェクト共通にしたい場合のみヘッダー無しの行。0.19.0 から deny reason の
hint もこの形を案内する。[docs/PATTERNS.md](./docs/PATTERNS.md))。

> **0.8.0 で glob 候補列挙を撤廃**: 0.3.2〜0.7.x では `cat *.json` を既定 rules の
> `credentials*.json` と交差させて deny に倒していたが、思想 1 (うっかり露出予防、
> 敵対的防御は非目的) に対し deny 寄り過ぎる (`cat *.json` `cat *.key` `cat *.log`
> 等の日常 glob まで巻き込む) ため 0.8.0 で撤廃した。現在は operand glob が
> shell の pathname expansion で `.env` / `.envrc` literal に展開されうるとき
> だけ deny 固定で、それ以外の glob (`id_rsa*`, `*.key`, `cred*.json`, `*.log`
> 等、および shell では dotfile に展開されない `*` / `?env` / `[.]env` /
> `*.envrc`) は ``ask_or_allow`` (default=ask, autonomous=allow) に倒す
> (0.22.0 で fnmatch の意味論から shell の意味論に修正)。

### `PreToolUse(Edit | Write)` — redact-sensitive-reads

`tool_input.file_path` が機密パターン一致なら **新規/既存問わず deny 固定**。
書き込み経路から機密データが混入/置換される事故を防ぐ (ask を挟まない、
実機観測でうっかり承認による既存値喪失が発生した教訓から)。

dotenv 系 (`.env` / `.env.*` / `*.envrc`) を Edit/Write で block した際は、
`tool_input` から追加予定のキー名を抽出して reason に代替案として添える。
値そのものは含まれない (キー名のみ)。

block の理由は書き込み先の状態で 4 分岐する (0.20.0)。**判定はいずれも deny
固定で変わらず、変わるのは案内の文面だけ**:

| 書き込み先 | 案内 |
|---|---|
| 新規作成 | 同じキー名で `.env.example` を作り値を空にする (実値は手動入力かシークレット管理ツール経由) |
| 既存ファイルの書き換え | **既存ファイルの minimal info** (キー名・型・値の状態) + `dotenv-cli` の merge で既存値を保つ案内 |
| symlink 経由 | 実体側が書き換わる旨と、コピーではなく symlink を維持する運用の確認 |
| FIFO / socket / device | 通常ファイルを対象にするか、パス指定の誤りの確認 |

既存ファイルの書き換えでは、`Edit` (対象を絞った置換) と `Write`
(ファイル全体の置換) で代替案が変わる — 「現在の値がすべて失われる」のは
`Write` だけなので、その警告は `Write` にのみ出る。

既存ファイルの minimal info は Read tool の deny reason と同じ粒度
(キー名・型・prefix・length・値の状態タグ・placeholder ヒント) で、実値は
含まれない。取得のための読み取りには byte 上限があり (改行を含まない巨大な
ファイルでも上限を超えて読まない)、取得できなくても block の判定は変わらない。

#### Read と Edit/Write の symlink 対応の非対称性

| tool | 機密 + symlink | 理由 |
|---|---|---|
| `Read` | `ask_or_deny` (非 bypass は ask) | symlink 先が意図した参照 (共有 template / 外部参照) の可能性がある。ユーザー介在で判断 |
| `Edit` / `Write` | **`deny` 固定** | 書き込み先が意図せず外部 path を向くと実害が不可逆。ask なしで block |

### `Stop` — check-sensitive-files

応答が終わるたびに cwd が git 管理下なら、**tracked / untracked を問わず** 機密
パターンに一致するファイルを検出して `decision: block` で Claude に再確認を促す。

- **tracked**: `.gitignore` 済みでも block される (`git rm --cached` が必要な
  ため)。対応は「`.gitignore` に追加 + `git rm --cached <path>`」 (0.19.0 から
  この `git rm --cached` は Bash hook を通過する)
- **untracked**: `.gitignore` 済みのものは `git ls-files --others --exclude-standard`
  により既に除外済み。対応は「`.gitignore` に追加 or 意図的に管理対象化」
- **submodule**: 0.2.0 以降、`git ls-files --recurse-submodules` で submodule 内の
  **tracked** も検査対象。submodule 内の **untracked** は現状範囲外

block reason には tracked / untracked を別セクションで列挙し、それぞれ対応手順
と恒久除外レシピ (`[project:$CLAUDE_PROJECT_DIR]` セクション + `!<basename>` 行。
`$CLAUDE_PROJECT_DIR` は実際の絶対パスに置き換える) を添える (0.19.0)。

**session 単位の once-only (0.19.0)**: 同一セッションで同じファイル集合を報告済み
なら、以降の `Stop` は block しない (「意図的に管理対象とする」と承認した tracked
`.env` / committed 証明書で毎ターン block が出続けるのを止めるため)。新しい機密
ファイルが増えたとき、または untracked → tracked のように状態が変わったときだけ
再 block する。報告済み集合は
`~/.claude/sensitive-files-guardrail/stop-ack/<session_id>` に「repo root を
realpath で正規化した絶対パス + status」の sha256 digest で記録し (平文 path は
残さない。entry 自体の symlink は dereference しない。別 repo に `cd` すれば
再 block、同じ repo 内のサブディレクトリや submodule への移動では再 block
しない)、最後の block から 7 日で自動 GC。hook input に `session_id` が無ければ従来通り
毎回 block する。state の読み書きに失敗したときは stderr に
`stop_ack_unavailable` を出して従来通り block する。

**注意**: 同一ターン内の 2 回目以降の `Stop` は `stop_hook_active=true` で素通り
する (無限ループ防止)。**block が見えたら必ず対応する**。無視して次のターンに
進むと、同じ集合については以降チェックが出ない。

## パターン設定

ユーザー個別のパターンは plugin を fork せずに patterns.local.txt に書ける:

- `~/.claude/sensitive-files-guardrail/patterns.local.txt` (0.6.0 から単一パス)

> 0.4.0〜0.5.x で fallback として参照していた
> `$XDG_CONFIG_HOME/sensitive-files-guardrail/patterns.local.txt` /
> `~/.config/sensitive-files-guardrail/patterns.local.txt` は **0.6.0 で撤去**。
> 旧パスを使っていた場合は手動で
> `mv "${XDG_CONFIG_HOME:-$HOME/.config}/sensitive-files-guardrail/patterns.local.txt" ~/.claude/sensitive-files-guardrail/patterns.local.txt` する。

両 hook が自動で合流。last-match-wins (gitignore 風)、既定 case-insensitive。

> **プロジェクトスコープの rule (0.15.0)**: 同じファイル内に
> `[project:/abs/path/to/project]` セクションを書くと、そのプロジェクトで
> Claude Code が動いているときだけ適用される rule を追加できる (グローバル
> 1 ファイルという方針は維持)。詳細は [docs/PATTERNS.md](./docs/PATTERNS.md) の
> 「プロジェクトスコープの rule」節を参照。0.19.0 から両 hook の除外案内は
> このセクション配下への追記を既定として案内する (全プロジェクト共通の行は
> 明示的な選択)。案内中の `$CLAUDE_PROJECT_DIR` は **展開されない** プレース
> ホルダで、書くときはプロジェクト root の絶対パスを literal に書く。空ヘッダー
> (`[project:]`) や未展開の変数参照 (ヘッダー値が `$CLAUDE_PROJECT_DIR` /
> `$NAME` / `${NAME}` そのもの、またはそれで始まり直後が `/` の standalone 形)
> のヘッダーはどのプロジェクトにも一致せず無視されるが、黙らず
> `local_patterns_header_invalid` を stderr (Read/Bash 側は
> `~/.claude/logs/redact-hook.log` にも) に出す。`/work/project$prod` や
> `/work/repo$CLAUDE_PROJECT_DIR-prod` のように `$` や予約語を途中に含むだけの
> literal パスは正当なヘッダーとして扱う。

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

plugin root から実行する (`cd` はサブシェルに閉じ込める — 裸の `cd` を続けて貼ると
2 つ目が 1 つ目の cd 先を起点に解決されて失敗する):

```bash
# redact-sensitive-reads (934 tests, 0.23.0 時点)
(cd hooks/redact-sensitive-reads && python3 -m unittest discover tests)

# check-sensitive-files (80 tests, 0.23.0 時点)
(cd hooks/check-sensitive-files && python3 -m unittest discover tests)
```

validate / リリース手順 / CLI 再実測 Runbook などの保守者向け手順は
[docs/MAINTAINING.md](./docs/MAINTAINING.md) にまとめてある。

## ログ

`redact-sensitive-reads` の動作ログは `~/.claude/logs/redact-hook.log` に
書かれる (plugin cache が消えても残るよう `$HOME` 側に固定)。ログには鍵名・
パス・値を一切書かない (エラー種別・classify 結果のみ)。

## 互換性

- Claude Code CLI 2.1.100+ 想定
- Python 3.11+ 想定 (標準ライブラリのみ、`pip install` 不要)
- Git 1.7+ (submodule scan 用)
- macOS / Linux 対応、Windows 非対応 (現状 fail-closed で deny)
