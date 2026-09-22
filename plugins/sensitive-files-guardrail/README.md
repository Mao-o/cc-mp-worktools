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
| `Grep` で `.env` の **一致行** が返る (0.34.0) | `redact-sensitive-reads` | `PreToolUse` (Grep) |
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

有効化すると `PreToolUse(Read | Bash | Grep | Edit | Write)` / `Stop` の hook が
自動登録される (`settings.json` の手動編集不要)。

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
4. **`.npmrc` だけは内容ゲート** (0.34.0) → 認証らしい行が 1 行も無ければ
   `allow` (下の「`.npmrc` の内容ゲート」節)

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
  5. PLACEHOLDER   <type=str>  <placeholder>  matched="your_*_here"  length=24
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
- `length=<N>`: 値の文字数 (生の値。bucket せずそのまま)。「秘密鍵が短すぎる」
  「ダンプ混入で 4096 超」等の異常検知に有用
- `matched="..."`: placeholder 一致時の辞書 literal / pattern label。判定は
  ヒューリスティックだが、0.31.0 から**環境名そのもの** (`local` / `staging`) や
  雛形 (`<your-key>`) のような形に絞ってある — `dev_a8f3c2e1b9d7` のような
  **実トークン**は `<placeholder>` ではなく `<set>` になる (「値が入っていない」と
  誤って伝えない方向に倒す)

> **エンコーディング (0.31.0)**: BOM 付き UTF-8 / UTF-16 (BOM 有無どちらも) の
> ファイルも正しくデコードして鍵名を返す。それ以前は BOM 付きで先頭 1 鍵が消え、
> UTF-16 は `entries: 0` = 「空ファイル」と報告していた (deny 自体は出るので値は
> 漏れないが、報告内容が誤っていた)。推定できない形式 (BOM の無い latin-1 等) は
> 「一部のバイトが UTF-8 として不正」という note を添える。NUL を含むだけの
> バイナリ (証明書の DER 等) を UTF-16 と誤って言い切らないよう、推定を受け入れる
> 前に可読文字比率で確認する。
>
> 32KB 超のファイルは逐次読みのため範囲が狭い: **UTF-8 の BOM は落とす**
> (先頭 1 鍵の欠落と、armored 鍵が `format: pem` に乗らない問題が直る) が、
> **BOM 無し UTF-16 / UTF-32 は未対応**で、鍵が 1 件も拾えなかった場合に
> 「エンコーディングか format が違うかもしれない」旨を出す。UTF-16 の armored
> 鍵は意図的に `pem` 扱いにしない (block 数を数えられず「0 block」と言い切って
> しまうため、鍵 0 件の開示に倒す)。

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

#### `.npmrc` の内容ゲート (0.34.0)

`.npmrc` は pnpm / yarn を使う repo でほぼ必ず commit される**設定ファイル**
(`engine-strict` / `auto-install-peers` / `@scope:registry`) で、認証トークンを
含むのは一部にすぎない。既定 patterns からは**外さず**、Read が既に開いている
ファイルの**中身**で確定する:

- 認証らしい行が 1 行も無い → **allow** (内容をそのまま Claude に渡す)
- 認証らしい行がある → 従来どおり **deny** + 鍵名のみの minimal info
- 読めない / UTF-8 として decode できない / 64KiB 超 → **deny** (fail-closed)

「認証らしい行」は、行頭の空白を除き `#` / `;` コメント行を捨てたうえで、
**キー部** (最初の `=` の左。`=` が無ければ行全体) か**値部**が次のいずれかに
当たる行:

- キー部が `//` で始まる (`//registry.npmjs.org/:_authToken=...` のような
  registry 単位設定)
- キー部が npm の認証系設定キーに**完全一致**する — `key` / `cert` /
  `keyfile` / `certfile` / `cafile` / `otp` / `_auth` / `_authToken` /
  `_password` / `username` / `email` / `always-auth`
- キー部が識別力のある語を**含む** — `_auth` / `_password` / `username` /
  `email` / `always-auth` / `keyfile` / `certfile` / `cafile`
- **値部**が `scheme://user[:pass]@host` 形 (URL に埋め込んだ credential。`:` を省いた `TOKEN@host` も npm は Basic 認証に載せるので含める)。
  キー名は問わない (`registry=` / `@scope:registry=` / `proxy=` /
  `https-proxy=` など)

いずれも大文字小文字を区別せず、キー名の `-` と `_` の差は吸収する。
**値の有無は問わない** — `//registry/:_authToken=${NPM_TOKEN}` のような
環境変数参照も deny 側に倒す (境界を「認証の設定行が存在するか」に固定して
単純に保つため)。`key` / `cert` / `otp` だけを完全一致にしてあるのは、部分
一致にすると `keyword` / `certainty` のような無関係なキーに誤爆するため。

deny したときは reason の**先頭 1 行**に「認証設定行 N 件 (キー名: …)」が
付く (**値は出さない**)。ここを出さないと、既存の鍵名要約 (ini の keys-only
scan) には認証キーが載らないため「無害な設定キーだけが並んだ deny」に見え、
除外レシピを足す方向へ誘導してしまう。

npm が読まないキー名 (`mytoken=...`) に秘密を書いた `.npmrc` は allow。
「`.npmrc` は npm の設定ファイルである」という前提そのもので、内容ゲートの
設計上の受容範囲。

同じ判定を `Stop` hook も使う (tracked / untracked の `.npmrc` は認証行が
無ければ報告しない)。ただし Stop の判定は **working tree の内容**で行う —
index / history に認証行が残っていても、worktree 側で消えていれば報告しない
(既知の限界)。**`Bash` / `Grep` / `Edit` / `Write` は対象外** —
`cat .npmrc` は従来どおり deny。Bash の operand は path とは限らない文字列で、
operand ごとにファイルを開くのは設計変更にあたるため。`.pypirc` / `.netrc` も
従来どおり内容に依らず deny。

### `PreToolUse(Bash)` — redact-sensitive-reads

**三態判定** (deny / ask_or_allow / allow) で静的解析する:

- **deny 固定**: literal operand が機密パターンに一致、または operand glob が
  shell の展開で dotenv stem (`.env` / `.envrc`) に一致しうる (`.env*` /
  `*/.env`。裸の `*` は dotfile に展開されないので対象外)。bypass / auto を
  含めて全 mode で block。grep 系 / jq / awk / sed の第 1 positional (pattern /
  script) と値が path ではない option の値 (`git log -S.env` /
  `--exclude='.env'`) は operand として数えない (0.22.0)。単純変数展開
  (`$NAME` / `${NAME}`) を含む operand も、変数の展開結果に依らず basename が
  確定する形 (`cat $PWD/.env` — 何に展開されても basename は `.env`) は
  deny に届く (0.25.0)
- **ask_or_allow**: 静的解析不能ケース (`<` 入力リダイレクト、heredoc / process
  sub / コマンド置換 / shell wrapper / 任意 path 実行、および展開結果が判定を
  左右する変数 operand (`cat $X` / `cat $X.env`) 等)。`default` /
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
> アクションを含まない場合のみ allow (`find . -name .env -exec cat .env ';'` は
> deny)。同様に `file -f` / `wc --files0-from` / `tree --fromfile` 等、operand の
> 中身をファイル名リストとして読み echo するオプション付き形も allow-list から
> 外れる (`file .env` / `wc -l .env` の通常形は allow)。**外れた形は operand scan
> に回るだけで、deny になるのは operand に機密 path 候補があるときだけ** —
> `file -f .env` は deny、`file -f list.txt` や `find . -name list.txt -delete` は
> allow。`git ls-files` は plain path-listing のみ
> metadata-only として allow し、`-s` / `--stage` / `--format` は blob object name
> (= 内容の指紋) を出せるため operand scan に回す (機密 operand を伴う
> `git ls-files -s .env` は deny、`git ls-files -s` 単体は allow)。
> `git status` は `-v`/`--verbose` が staged diff (機密の
> 旧値/新値) を出すため allowlist 外 (裸 `git status` は allow、
> `git status -v -- .env` は deny)。`cat` / `head` / `grep` 等の内容出力系と
> `cp` / `mv` (複製で漏洩面が広がる)、`git show` / `git diff` / `git add` は
> 従来通り deny 固定。`echo KEY=val > .env` のような書込み形は residual metachar
> の ask 経路が先に効くため緩まない。metadata-only ∩ safe_read コマンドの
> `ls > .env` 系 redirect 書込みも deny (破壊的書込み。`>|` clobber 上書きを
> 含む — 0.25.0 で splitter が `>|` を 1 演算子として読むようになり、`|` の
> 分割で target が別 segment に割れて全 mode allow に素通りしていた
> 取りこぼしを解消)。

> **0.19.0 で次善策コマンドを metadata-only に追加**: 両 hook
> の reason が「tracked なら `git rm --cached <path>` で untrack」「`chmod 600 .env`」
> と案内しながら Bash hook 自身がそれらを deny する自己矛盾があった。
> `git rm --cached` (`--cached` 完全一致。`--no-cached` / `--pathspec-from-file` や
> その省略形を含む「既知の安全な option 以外」が 1 つでもあれば fail-closed で
> 通常経路) は index からの除去のみで実ファイルは残り内容も出ないため allow。`chmod` /
> `chown` / `chgrp` / `touch` は内容を読む option が存在しない (`--reference` /
> `-r` は mode / owner / timestamp のみ) ため allow。plain `git rm` (作業ツリー
> 削除 = 破壊操作) と `git rm --cached --pathspec-from-file=<file>` (中身を pathspec
> として読み不一致行を echo) は allow-list 外 = operand scan へ (`git rm .env` は
> deny、`git rm list.txt` は allow)、`chmod 600 x > .env` の書込み形は echo
> と同じく residual metachar の ask_or_allow のまま。あわせて `git` の deny reason
> を subcommand 別 (show / diff / log = 閲覧、add / rm / mv / restore = 操作) に
> 分け、`git rm .env` が「閲覧しようとした」と返していた誤った意図文を解消した。

**False positive の注意**: unified operand scan は「コマンドが実際に file の
内容を出力するか」までは判別しないため、`cat` / `grep` 等の内容出力系コマンド
では、operand が機密パターンに literal 一致すれば実際の用途を問わず deny される
(0.14.0 で `echo .env` / `ls .env` 等の metadata-only 系は allow に解消済み)。
恒久的に許可したい場合は `patterns.local.txt` の
`[project:<プロジェクトの絶対パス>]` セクション配下に `!<root 相対パス>`
(承認した 1 ファイルだけ、0.24.0) または `!<basename>` (同名すべて) を追加する
(全プロジェクト共通にしたい場合のみヘッダー無しの行。deny reason の hint は
path 形を既定に、basename 形を併記して案内する。
[docs/PATTERNS.md](./docs/PATTERNS.md))。

> **0.8.0 で glob 候補列挙を撤廃**: 0.3.2〜0.7.x では `cat *.json` を既定 rules の
> `credentials*.json` と交差させて deny に倒していたが、思想 1 (うっかり露出予防、
> 敵対的防御は非目的) に対し deny 寄り過ぎる (`cat *.json` `cat *.key` `cat *.log`
> 等の日常 glob まで巻き込む) ため 0.8.0 で撤廃した。現在は operand glob が
> shell の pathname expansion で `.env` / `.envrc` literal に展開されうるとき
> だけ deny 固定で、それ以外の glob (`id_rsa*`, `*.key`, `cred*.json`, `*.log`
> 等、および shell では dotfile に展開されない `*` / `?env` / `[.]env` /
> `*.envrc`) は ``ask_or_allow`` (default=ask, autonomous=allow) に倒す
> (0.22.0 で fnmatch の意味論から shell の意味論に修正)。

### `PreToolUse(Grep)` — redact-sensitive-reads (0.34.0)

`tool_input` の `path` と `glob` だけを見る最小対応。`output_mode: "content"`
の Grep は**一致行をそのまま返す**ため、機密ファイルを指した Grep は値の一部を
コンテキストに載せる。

- `path` が機密名の**通常ファイル** → **deny**
- `glob` が literal で機密名、または dotenv stem (`.env` / `.envrc`) に展開
  されうる glob (`.env*` / `**/.env`) → **deny** (Bash operand と同じ規則)
- `glob` がそれ以外の**ワイルドカード**を含む (`*.py` / `*.pem` / `id_rsa*` /
  `*.env` / `*.{ts,tsx}`) → `ask` (autonomous では `allow`。Bash operand の
  `glob_uncertain` と同じ三態)
- `path` が**ディレクトリ** / 存在しない / 非機密、`path` も `glob` も未指定、
  `glob` が非機密の literal → **allow**
- `path` が機密名の symlink / 特殊ファイル → `ask` (bypass 下は `deny`、Read と同じ)

ブレース展開 (`{a,b}`) は**分岐ごとに**判定して最も強い結論を採る
(`{.env,*.py}` → deny)。Bash 側に同等の機構は無い (`{` は hard-stop として
`ask` に倒れるだけ) ので、これは Grep だけの扱い。

**`ask` を作らないのはディレクトリ走査だけ**: `path` がディレクトリ / 未指定
のときは allow に倒す。`glob` は Bash と同じ三態 (0.34.0 のマージ前レビューで
「Grep の方が緩い」ことが実測されたため揃えた)。例外経路は Read と同じ
`ask_or_deny`。`pattern` / `output_mode` / `type` / `head_limit` は判定に
使わない。

> **`Grep` ツールは macOS / Linux の既定では tool set に載らない** — 公式
> tools reference のとおり、Claude はこれらの OS では Bash の `find` / `grep`
> を使う。Grep が実際に呼ばれるのは **Windows 既定** / `--tools`
> `--allowedTools` で明示指名した場合 / Bash が deny されている場合 /
> subagent の tools に Grep があって Bash が無い場合。この matcher は
> 「第一級の読み取り経路を塞ぐ」ものではなく、**Bash 経由の `grep` と判定を
> 揃えるための対称性**の対応 (0.34.0 で Windows の無条件 deny を撤去したので、
> Windows 既定の経路が実際に意味を持つようになった)。

> **揃え先は Bash の positional operand**: Grep の `glob` は「検索対象を絞る
> filter」なので Bash での真の同型は `grep -rn X --include='*.py' .`
> (実測 allow) とも読めるが、判定境界は `grep -rn X *.py` (実測 ask) 側、
> つまり**過剰 ask 側**に倒した。`*.py` のような無害な glob も default では
> ask になる (autonomous では allow)。**先頭ドットだけは Bash と違う**: Grep の
> `glob` は ripgrep (gitignore 流) が解釈し、`*.env` / `[.]env` / `?env` は `.env`
> に一致するので deny (Bash の `cat *.env` は shell の展開規則で ask)。`*` /
> `dir/**` は絞り込みではなく走査なので、ディレクトリ走査と同じ扱い。

> **ディレクトリ走査は allow**: `path` にディレクトリを渡した Grep は、配下の
> 機密ファイルの行が結果に混ざりうるが deny しない。Bash の `grep -r X .` と
> 同じ既知の限界として扱う (`python -m venv .env` のように機密名のディレクトリ
> がある repo で全検索が止まるのを避けるため)。

### `PreToolUse(Edit | Write)` — redact-sensitive-reads

`tool_input.file_path` が機密パターン一致なら **新規/既存問わず deny 固定**。
書き込み経路から機密データが混入/置換される事故を防ぐ (ask を挟まない、
実機観測でうっかり承認による既存値喪失が発生した教訓から)。

dotenv 系 (`.env` / `.env.*` / `*.env` / `.envrc` / `*.envrc`) を Edit/Write で
block した際は、
`tool_input` から追加予定のキー名を抽出して reason に代替案として添える。
値そのものは含まれない (キー名のみ)。

block の理由は書き込み先の状態で 4 分岐する (0.20.0)。**判定はいずれも deny
固定で変わらず、変わるのは案内の文面だけ**:

| 書き込み先 | 案内 |
|---|---|
| 新規作成 | 同じキー名で `.env.example` を作り値を空にする (実値は手動入力かシークレット管理ツール経由)。`.envrc` は `.envrc.example` |
| 既存ファイルの書き換え | **既存ファイルの minimal info** (キー名・型・値の状態) + `dotenv-cli` の merge で既存値を保つ案内。`.envrc` は direnv 前提の案内 (dotenv-cli merge の対象外) |
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
と恒久除外レシピ (`[project:$CLAUDE_PROJECT_DIR]` セクション + `!<root 相対パス>`
行。`$CLAUDE_PROJECT_DIR` は実際の絶対パスに置き換える) を添える (0.19.0)。
0.24.0 からレシピは承認した 1 ファイルだけを外す **path 形** が既定で、同名すべてを
外す basename 形 (`!<basename>`) は明示的な選択として併記する。表示の file 一覧は
cwd 相対のまま、レシピは project root 相対 (サブディレクトリで発火しても
`!sub/.env`)。

block reason には**出力の文字数上限**がある (0.27.0)。該当ファイルが多い / パスが
長いときは、ファイル一覧が `... (N more files; see git status)`、除外レシピが
`... (N more)` に畳まれる (省略件数は必ず表示される)。AskUserQuestion の案内・
除外レシピの追記先 (`[project:...]` ヘッダー)・影響範囲の説明といった**静的な
案内文は畳まれない**。

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

**`.npmrc` の内容ゲートは working tree だけを見る (0.34.0 の既知の限界)**:
tracked な `.npmrc` でも判定に使うのは作業ツリー上の中身なので、index /
history に認証行が残ったまま worktree 側から消した状態では報告が止まる。

**注意**: 同一ターン内の 2 回目以降の `Stop` は `stop_hook_active=true` で素通り
する (無限ループ防止)。**block が見えたら必ず対応する**。無視して次のターンに
進むと、同じ集合については以降チェックが出ない。

> **沈黙は「直った」ことを意味しない (0.31.0 で reason に明記)**。block が出なく
> なるのは「同じ集合を報告済み」だからで、hook は対処が有効だったかを再確認して
> いない。特に tracked ファイルは `.gitignore` に足しただけでは index に残る
> (対処として無効) のに、次のターンからは黙る。実際に外れたかは
> `git ls-files <path>` の出力が空になったことで確認する。

**時間予算 (0.32.0)**: この hook には 15 秒の timeout があり、到達すると Claude
Code は hook を kill して出力を **discard** する (= 機密ファイルの報告が 1 byte も
出ない)。そのため hook 側で **12 秒の予算**を持ち、git 呼出とパターン照合の両方が
この締切を共有する。超過したときは黙らず:

- 検出 0 件で打ち切った場合 → `systemMessage` で「このターンは検査が**不完全**
  です (「機密なし」ではありません)」と表示する (block はしない)
- 1 件以上見つかっていた場合 → block reason の冒頭に「一覧は不完全です」を添える

**大規模 repo で予算超過が出る場合**は、未 ignore のディレクトリ (`node_modules` /
`build` / `dist` / キャッシュ類) を `.gitignore` に入れると `git ls-files --others`
の列挙が大幅に速くなる。ネットワークファイルシステム上の repo でも起きやすい。

## パターン設定

ユーザー個別のパターンは plugin を fork せずに patterns.local.txt に書ける:

- `~/.claude/sensitive-files-guardrail/patterns.local.txt` (0.6.0 から単一パス)

> 0.4.0〜0.5.x で fallback として参照していた
> `$XDG_CONFIG_HOME/sensitive-files-guardrail/patterns.local.txt` /
> `~/.config/sensitive-files-guardrail/patterns.local.txt` は **0.6.0 で撤去**。
> 旧パスを使っていた場合は手動で
> `mv "${XDG_CONFIG_HOME:-$HOME/.config}/sensitive-files-guardrail/patterns.local.txt" ~/.claude/sensitive-files-guardrail/patterns.local.txt` する。

**貢献者・CI と共有したい除外** (テスト fixture / サンプルのダミー鍵など) は
repo に commit できる (0.32.0):

- `<project root>/.claude/sensitive-files-guardrail/patterns.txt`

> user 単位ファイルはホーム配下なので commit できず CI でも効かないため、
> ダミー鍵を持つ repo では貢献者全員が毎セッション同じ block を踏んでいた。
> tier の強さは **`user 単位` > `repo 同梱` > `既定`** (clone してきた repo の
> 除外をユーザーが自分のファイルで打ち消せる向き)。`!` 除外に加えて include 行も
> 有効。第三者の repo を開くときはこのファイルも差分レビューの対象にすること
> (`!` 行は保護を弱めうる。読み込み時に `project_patterns_in_use` を記録する —
> この記録は `SFG_LOG_LEVEL` を上げても残る)。

> **`.claude/` を `.gitignore` していると commit できない**: ignore された状態
> では置いた本人の手元でだけ効き、**clone した貢献者と CI には存在しない**
> (本人の手元では `project_patterns_in_use` が出るので「効いている」と見える)。
> `git check-ignore -v .claude/sensitive-files-guardrail/patterns.txt` で確認し、
> ignore されていれば `.gitignore` に `!.claude/sensitive-files-guardrail/` の
> negation を足して commit する。未 commit のままだと `git worktree add` が
> 持ち込まないため **worktree セッションでは tier が丸ごと消える** (無警告)。
> 詳細は [docs/PATTERNS.md](./docs/PATTERNS.md) の同節。

両 hook が自動で合流。last-match-wins (gitignore 風)、既定 case-insensitive。

> **git worktree (0.32.0)**: `claude --worktree` / `--bg` / sub-agent の
> `isolation: worktree` では `$CLAUDE_PROJECT_DIR` が worktree 自身のパスに
> なるため、`[project:...]` セクションの一致判定は **worktree 自身 + main repo
> root の 2 候補**を見る (main repo のパスで書いておけば worktree でも効く)。
> `~` はヘッダーでも展開される。

> **path 形 rule (0.24.0)**: `/` を含む行 (`!config/prod.pem` / `!fixtures/` /
> `secrets/**`) は basename ではなく **プロジェクト root からの相対 path 全体**と
> gitignore 準拠の意味論 (`*` は `/` を跨がない、`**` は跨ぐ、先頭 `/` は root
> アンカー、末尾 `/` はディレクトリ) で比較する。承認した 1 ファイルだけを
> 除外できるのはこの形 (root 直下のファイルは `!/.env` のように先頭 `/` を付ける —
> 付けないと basename 形になる)。root は `[project:]` セクションと同じ解決
> (`$CLAUDE_PROJECT_DIR` → `.git` 上方探索) で、root を解決できない場所では
> 一致しない。`/` を含まない行は従来どおり basename 形 (同名すべて)。詳細は
> [docs/PATTERNS.md](./docs/PATTERNS.md) の「rule の形で比較対象が決まる」節。

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
   予防が主目的。設計判断として受容済み)。0.25.0 から、単純変数展開だけが
   理由で解析を放棄していた形 (`cat $PWD/.env` — 展開結果に依らず basename
   `.env` が確定) は deny に届く。展開結果が判定を左右する形 (`cat $X`) は
   従来どおり allow に倒る
3. **TOCTOU 完全排除は非目的** — fd ベース reader により「同一プロセス内の
   再 open」race は排除済みだが、hook 読取と Claude 実 Read/Write の分離は範囲外
4. **Windows は未検証** (0.34.0) — 0.33.x までは `signal.SIGALRM` の有無を
   Windows 判定の proxy にして hook 冒頭から全 tool 呼出を deny していたが、
   根拠だった内部 soft-timeout は 0.6.0 で撤去済みで、外部 timeout の fail-open
   は全 OS 共通だったため、この無条件 deny を撤去した。Windows でも通常判定を
   通し、内部失敗は catch-all の `ask_or_deny` に倒れる (fail-closed)。
   `O_NOFOLLOW` / `O_CLOEXEC` が無い環境では symlink 検知が `lstat` 判定の
   fallback に依存する。0.34.1 で patterns / stdin / git 出力 / ログの encoding を
   UTF-8 に固定した (0.34.0 では `PYTHONUTF8` 未設定の Windows で同梱 patterns の
   読込に失敗し、全 tool 呼出が catch-all に落ちていた)。**実機での検証は未実施**
5. **`!` プレフィックス (Claude Code bash mode) は対象外** — ユーザー明示操作で
   `! cat .env` を実行した場合は stdout が transcript に追加される (hook 介在外)
6. **Grep は最小対応 / Glob・NotebookEdit は対象外** (0.34.0) — `Grep` は
   `path` / `glob` が機密名を指すときだけ deny し、判定できない `glob` は
   `ask_or_allow` に倒す (上の `PreToolUse(Grep)` 節)。ディレクトリ走査で
   配下の機密ファイルの行が返る経路は
   Bash の `grep -r X .` と同じ既知の限界。`Glob` (パス列挙のみで内容を返さない)
   と `NotebookEdit` (`edits` の形状が違う) には発火しない。補うには Claude Code
   本体の `permissions.deny` に `Read(<path>)` ルールを追加する (公式仕様上
   best-effort で Grep / Glob にも適用される。`Glob(...)` という形の path rule は
   認識されず無視されるため注意)
7. **exec option を持つコマンドの列挙は網羅しない** — `ag --pager` /
   `git rebase --exec` / `sort --compress-program` のように、引数を別プロセスに
   渡すオプションの一覧は本質的に不完全で、敵対的バイパス対策は本 plugin の
   非目的。未知のコマンド・オプションは従来どおり `ask_or_allow`
   (default=ask、autonomous=allow) に倒れるので、列挙漏れがあっても 0.17.0 より
   後退はしない。**同類の指摘に対して個別対応は行わない** — 対応するとしたら
   「列挙」ではなく設計 (inert allow-list の縮小 / 緩和の撤回) の再検討として
   行う。0.34.0 の gawk `@include` / `@load` 追加は、operand が 1 つも無くても
   プログラム文字列からファイルを開く点で `-f prog.awk` と同型だったための例外
8. **repo root のパスに改行を含む場合** — Stop hook の `git rev-parse
   --show-toplevel --show-prefix` を改行区切りで読むため toplevel / prefix が
   誤 parse され、stop-ack の digest が別ファイルと衝突しうる (0.30.0 時点の既知
   の残課題。ファイル名側は `-z` で対応済み)

## Fail-closed vs fail-open

| hook | 機密検出時 | 判定不能時 | 備考 |
|---|---|---|---|
| `redact-sensitive-reads` (Read) | **deny** + minimal info | **ask_or_deny** | non-bypass は ask、bypass は deny |
| `redact-sensitive-reads` (Grep) | **deny 固定** | **ask_or_deny** (内部失敗) / **ask_or_allow** (判定できない `glob`) | 0.34.0。`path` / `glob` のみ判定。`glob` は Bash operand と同じ三態 (ただし ripgrep の意味論で先頭ドットは特別扱いされず、`*.env` / `[.]env` / `?env` は deny)、ディレクトリ走査と `*` / `dir/**` だけ allow 側 |
| `redact-sensitive-reads` (Edit/Write) | **deny 固定** | **ask_or_deny** | ask を挟まない |
| `redact-sensitive-reads` (Bash) | **deny 固定** | **ask_or_allow** | default/acceptEdits/dontAsk は ask、auto/bypass は **allow** |
| `redact-sensitive-reads` (Bash, patterns.txt 読込失敗) | — | **deny 固定** | policy 欠如時は全 mode block |
| `check-sensitive-files` (Stop) | `decision: block` | **fail-open** (exit 0。内部例外時は `systemMessage` で通知) | patterns.txt 読込失敗時は stderr warning のみ |

> **0.33.0: lenient allow を Claude にも開示する (判定は不変)**。`ask_or_allow` が
> autonomous mode (`auto` / `bypassPermissions` / `plan`) で allow に倒し、かつ
> **コマンド文字列に機密パターンらしい token が含まれる**とき、
> `hookSpecificOutput.additionalContext` に「静的解析では機密パスの有無を判定でき
> ないコマンドを、確認なしで通した」旨の短い 1 文を添える。`permissionDecisionReason`
> は allow / ask ではユーザーにしか表示されない仕様なので、それまで Claude 側には
> 「静的判定できないまま通った」事実が一切届いていなかった。**`permissionDecision`
> は出さないため許可の強さは変わらず**、静的に「機密でない」と確定した allow
> (`ls .env` 等) には付かない。文面は固定 1 文で、コマンド文字列・パス・値は
> 含めない。
>
> 絞り込みは「lenient allow が全 Bash 呼出の 4 割強」という実測に対する措置で、
> `bash -c 'cat .env'` / `cat *.key` / `{ cat .env; }` のように機密に触れうる形
> だけを開示する。`bash -c 'date'` / `cat *.log` のように機密らしい token を
> 含まない形では素の allow に戻す (判定はどちらも同じ allow)。glob や変数は
> 展開せず文字列として照合するため、`cat id_*` / `cat $SECRET` のように判定
> できない形は**開示しない側**に倒れる。詳細は
> [docs/DESIGN.md](docs/DESIGN.md) の「lenient allow の開示」。

> **0.33.0: Stop block の追記案内に実行手段を明記 (判定は不変)**。`.gitignore` /
> `patterns.local.txt` への追記は **Edit / Write ツール**で行うよう案内する。
> `echo '.env' >> .gitignore` のようなシェルリダイレクト形は Bash 判定のリダイレクト
> 経路で確認 (ask) に落ちるため、block した直後に承認ダイアログが挟まる 2 段の
> 摩擦になっていた (deny ではないので機能は損なわれない)。判定表は変えず、案内する
> 手段だけを全 mode で allow される側に寄せた。

## 設計上のトレードオフ

- **Vibe Coder の誤操作予防**が目的。敵対的防御 (prompt injection, 悪意ある
  agent) は非目的
- 完全な情報遮断ではない。basename と鍵名は LLM に見える
- TOCTOU race は完全には防げない
- Python 3.11+ / Git 1.7+ / macOS / Linux で検証済み。Windows は未検証
  (0.34.0 で無条件 deny は撤去、0.34.1 でテキスト I/O の encoding を UTF-8 固定。
  既知制限 4 を参照)

## テスト

plugin root から実行する (`cd` はサブシェルに閉じ込める — 裸の `cd` を続けて貼ると
2 つ目が 1 つ目の cd 先を起点に解決されて失敗する):

```bash
# redact-sensitive-reads (1,496 tests, 0.34.1 時点)
(cd hooks/redact-sensitive-reads && python3 -m unittest discover tests)

# check-sensitive-files (181 tests, 0.34.1 時点)
(cd hooks/check-sensitive-files && python3 -m unittest discover tests)
```

validate / リリース手順 / CLI 再実測 Runbook などの保守者向け手順は
[docs/MAINTAINING.md](./docs/MAINTAINING.md) にまとめてある。

## ログ

`redact-sensitive-reads` の動作ログは既定で `~/.claude/logs/redact-hook.log`
に書かれる (plugin cache が消えても残るよう `$HOME` 側に固定)。ログには鍵名・
パス・値を一切書かない (エラー種別・classify 結果のみ)。`SFG_LOG_PATH` 環境
変数を設定するとログ書込み先をその値で上書きできる (主にテスト実行時の隔離
用途)。既定 5MB を超えると `redact-hook.log.1` へ 1 世代ローテーションする
(直近世代のみ保持、2 回目以降のローテーションは `.1` を上書き)。ローテーションは
サイドカー lock ファイル `redact-hook.log.lock` の `flock` でプロセス間排他して
おり、並行する hook プロセスが同時にローテーションして前世代を消すことはない
(lock を取れなかったプロセスは待たずにローテーションを譲り、そのまま追記する)。

**ログ量を減らしたい場合 (`SFG_LOG_LEVEL`、0.32.0)**: ログは Bash 呼出のたびに
allow 経路でも 1 行書くため増えやすい (実測 7.3MB / 12 万行)。
`SFG_LOG_LEVEL=WARNING` を設定すると **最終判定が allow だった呼出の診断行だけ**
が落ちる。deny / ask 経路の診断行とエラー行は level に関わらず必ず残る
(`DEBUG` / `INFO` / `WARNING` / `ERROR` を受け付け、未設定・不正値は `INFO`)。
repo 同梱 patterns を読み込んだ記録 (`project_patterns_in_use`) も level に
関わらず残る — 「なぜ block されないのかを辿れる」と開示している緩和策なので、
まさに除外が効いた (= allow に倒れた) 呼出で消えてはいけないため。

> **既定 (未設定) は `INFO` で挙動は従来と完全に同一**。「この呼出は allow 経路か」
> は記録時点では決まらない (autonomous モードでは `ask` が `allow` になり、同一
> コマンド内の後続 deny が先行の判定を上書きする) ため、hook は判定が確定するまで
> 記録をバッファして最終判定で出すか決める。行の label は `INFO` のままなので、
> 既存の grep / 集計はそのまま使える。

## 互換性

- Claude Code CLI 2.1.100+ 想定
- Python 3.11+ 想定 (標準ライブラリのみ、`pip install` 不要)。3.11 未満の
  環境で動かした場合、TOML ファイルの reason は opaque な keys-only scan に
  フォールバックし、`format: toml (unsupported, keys-only scan)` と
  `Python 3.11+` を明記した note が付く (fail-open にはならず、hook 起動時にも
  ログへ 1 回記録する — サイレント劣化ではない)
- Git 1.7+ (submodule scan 用)
- macOS / Linux で検証済み。**Windows は未検証** — 0.34.0 で「SIGALRM 非対応
  なら全 tool 呼出を deny」という冒頭ゲートを撤去し、通常判定を通すように
  なった。内部失敗は catch-all の `ask_or_deny` に倒れる (fail-closed)。
  0.34.1 で patterns / stdin / git 出力の encoding を UTF-8 に固定したが、
  実機での検証は行っていない (既知制限 4)
