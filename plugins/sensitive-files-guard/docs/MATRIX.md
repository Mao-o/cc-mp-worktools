# 判定結果マトリクス (MATRIX.md)

全 6 permission_mode (default / plan / acceptEdits / auto / dontAsk /
bypassPermissions) での判定結果を完全列挙する。値は 2026-05-18 時点 (0.13.0)
の挙動。設計方針は [DESIGN.md](./DESIGN.md)、コマンド例の解釈は
[README.md](../README.md) を参照。

> 表は紙幅の都合で従来通り 5 列 (default / acceptEdits / auto / dontAsk /
> bypassPermissions) のみ列挙する。**`plan` 列は `auto` 列と同じ判定** (Bash
> 静的解析不能ケースは allow、機密 path 確定 match は deny、Read/Edit の判定
> 不能ケースは ask) として読むこと。`acceptEdits` / `dontAsk` のような ask 維持
> 系とは挙動が異なるため、`auto` 列を参照する。

`permission_mode` の列挙は `core/output.py::LENIENT_MODES` と
`tests/fixtures/envelopes/README.md:22` で突合される。CLI 側が新しい mode を
追加したら同時に更新すること (Runbook は [CLAUDE.md](../CLAUDE.md))。

> 0.13.0 (2026-05-18) で `plan` 列を `auto` と同等の lenient 扱いに戻した。
> 0.6.0 で「現行 CLI では hook 非発火」と判断して dead entry を撤去していたが、
> ユーザー実機で plan mode 中の Bash PreToolUse hook 発火を確認したため再追加
> (詳細は [DESIGN.md](./DESIGN.md) の 2026-05-18 エントリ)。plan mode は副作用
> が plan 承認まで保留されるため、Bash 静的解析不能ケースの `ask_or_allow` は
> autonomous と同じく allow に倒す。機密 path 確定 match (`make_deny`) と
> Read/Edit 用 `ask_or_deny` は plan でも安全側 (deny / ask) を維持する。

## 略号

- **deny**: block。`permissionDecisionReason` を返して LLM に値が露出しない
- **allow**: 素通り (hook は no-op 空オブジェクトを返す)
- **ask**: Claude Code UI でユーザー介在を要求。reason はモデルに届かない

## Read handler

| ケース | default | acceptEdits | auto | dontAsk | bypassPermissions |
|---|---|---|---|---|---|
| パターン非該当 | allow | allow | allow | allow | allow |
| 機密 + 通常ファイル | **deny** + minimal info (鍵名・型・prefix・status・length) | **deny** | **deny** | **deny** | **deny** |
| 機密 + symlink | ask | ask | ask | ask | **deny** |
| 機密 + 特殊ファイル (FIFO/socket/device) | ask | ask | ask | ask | **deny** |
| 機密 + 読み取り失敗 (権限/IO) | ask | ask | ask | ask | **deny** |
| redaction engine 内部例外 | ask | ask | ask | ask | **deny** |
| patterns.txt 読込失敗 | ask + stderr | ask | ask | ask | **deny** + stderr |
| 32KB 超 | keyonly deny | keyonly | keyonly | keyonly | keyonly |

> 0.9.0 で dotenv の minimal info を拡張: 値クラス (14 種、`url` / `email` /
> `uuid` / `aws_access_key` / `stripe_secret` / `stripe_pk` / `github_pat` /
> `openai_key` を追加)、識別子型 prefix (`sk_live_` / `AKIA` / `ghp_` 等)、
> value status (`<set>` / `<empty>` / `<placeholder>` / `<short>` / `<long>` /
> `<looks_truncated>`)、生バイト長 (`length=N`)、placeholder 一致ラベル
> (`matched="..."`) を追加。実値そのものは引き続き出さない。詳細は
> [DESIGN.md](./DESIGN.md#dotenv-minimal-info-の拡張-090-e1--e2)。
>
> 0.10.0 で **Bash deny の reason** にも上記 minimal info を埋め込むようになった
> (Read 同等)。`first_token` カテゴリ別 (`read_full` / `read_partial` / `search`
> / `mutate` / `load` / `move` / `history` / `transfer` / `archive`) で意図に
> 応じた suggestion (direnv / 1Password CLI / `git rm --cached` / `--exclude=`
> 等) を返し、grep family では env-var 名抽出 + dotenv 照合で
> `matched_pattern_keys: [...]` を出す。**deny 動作の判定境界は変化なし**で、
> 下表の deny / allow / ask は 0.9.0 と完全に同じ。詳細は
> [DESIGN.md](./DESIGN.md#bash-deny-の-category-別-reason-0100-e3--e4)。

## Bash handler — 機密確定 match (全 mode で deny)

以下は全 mode で **deny 固定** (autonomous を含む)。

| コマンド |
|---|
| `cat .env`, `less .env`, `head .env`, `source .env` |
| `head -n 1 .env`, `cat -- .env`, `tail -f .env` |
| `cat .env && pwd`, `false \|\| cat .env`, `cat .env; pwd`, `cat .env \| head` |
| `cat .env \| sed 's/(=)/X/'`, `cat .env \| grep '(=)'` (0.11.0: 後段 segment の hard-stop は seg 単位再評価のため seg1 の literal match で deny 短絡) |
| `head .env \|\| cat $X \|\| echo done`, `cat $X \| head .env \| wc -l` (0.11.0: hard-stop segment を含んでも他 segment で literal match があれば deny に到達) |
| `cat .env 2>/dev/null` (安全リダイレクト剥離後に機密 path) |
| `grep SECRET .env`, `base64 .env`, `xxd .env` |
| `timeout 1 cat .env`, `nice cat .env`, `stdbuf -o0 cat .env`, `busybox cat .env` |
| `cp .env /tmp/x`, `mv .env .env.old` |
| `curl file://.env`, `git show HEAD:.env` |
| `grep --file=.env foo`, `grep -f.env foo` |
| `cat .env*`, `cat *.envrc`, `cat .envrc*` (operand glob が `.env` / `.envrc` 一致) |
| `cat .e[n]v`, `cat .en?`, `cat [.]env` (`.env` literal 一致 char class / `?`) |

> 0.8.0 で **prefix normalize** (`FOO=1 cat .env` を `cat .env` と解釈する処理) と
> **既定 rules 候補列挙** (`cat *.key` / `cat id_rsa*` / `cat cred*.json` を
> 既定 rules 交差で deny する処理) を撤廃した。これらは思想 1 (うっかり露出予防、
> 敵対的防御は非目的) に整合しないため。撤廃された経路に該当するコマンドは
> 「Bash handler — 静的解析不能」表へ移動。

## Bash handler — 非機密 operand (全 mode で allow)

| コマンド |
|---|
| `echo foo`, `ls -la`, `npm test`, `date`, `pwd`, `make build` |
| `cat README.md`, `grep foo README.md`, `cat README.md 2>/dev/null` |
| `ls \| head`, `git status && git log 2>/dev/null \|\| true` |
| `cat .env.example`, `cat .env.sample` (literal、テンプレ除外 last-match-wins) |

## Bash handler — read-only first_token allow-list (0.12.0 新設, 全 mode で allow)

`first_token` が `_SAFE_READ_FIRST_TOKENS` (`ls cat head tail nl tac bat less
more view wc file stat du df tree grep egrep fgrep rg ag ack od xxd hexdump`)
に該当する segment は、`_segment_has_residual_metachar` の ask 経路を **スキップ
して operand scan に直行** する。`>`/`>>` 出力リダイレクトや `&` background を
含んでも allow に倒る (非機密 operand のとき)。

| コマンド | default | acceptEdits | auto | dontAsk | bypassPermissions |
|---|---|---|---|---|---|
| `grep foo README.md > /tmp/out`, `grep foo file >> /tmp/out` (出力 redirect) | allow | allow | allow | allow | allow |
| `ls -la > /tmp/listing.txt`, `cat README.md > /tmp/out`, `head -5 README.md > /tmp/x` | allow | allow | allow | allow | allow |
| `wc -l README.md > /tmp/count`, `file README.md > /tmp/x`, `stat README.md > /tmp/x` | allow | allow | allow | allow | allow |
| `grep foo README.md \| wc -l > /tmp/count` (pipe + redirect、全 segment が allow-list) | allow | allow | allow | allow | allow |
| `grep foo file.txt &` (background) | allow | allow | allow | allow | allow |
| `grep foo > .env` (機密 redirect target、operand scan で deny 固定) | **deny** | **deny** | **deny** | **deny** | **deny** |
| `grep SECRET .env > out.txt` (機密 operand、operand scan で deny 固定) | **deny** | **deny** | **deny** | **deny** | **deny** |
| `grep foo < .env` (`<` hard-stop、allow-list でも ask 維持) | ask | ask | **allow** | ask | **allow** |
| `grep foo $(find . -name x)` (`$()` hard-stop、allow-list でも ask 維持) | ask | ask | **allow** | ask | **allow** |

allow-list **外** の first_token (`awk`, `sed`, `find`, `xargs`, `parallel`,
`echo`, `bash`, `eval`, `sudo` 等) は依然「Bash handler — 静的解析不能」表に
従う。`awk '{print}' f > out`, `sed s/x/y f > out`, `find . > files.txt`,
`echo foo > out.txt` などはすべて ask 維持。

## Bash handler — metadata-only first_token (0.14.0 新設, 全 mode で allow)

`first_token` が `_METADATA_ONLY_FIRST_TOKENS` (`ls tree stat file du df test wc
basename dirname realpath readlink echo printf`) に該当する segment、または
`git check-ignore` / `git ls-files` / `git status` (subcommand 直書き形) は、
**operand の内容を stdout に出さない** ため operand scan をスキップして allow に
倒す。機密 path が operand に居ても、出力はファイル名・属性・件数・パス文字列
のみで値は LLM コンテキストに載らない (思想 1 の射程外)。

`find` は **条件付き**: `-exec` / `-execdir` / `-ok` / `-okdir` / `-delete` /
`-fprint*` / `-fls` (`_FIND_DANGEROUS_ACTIONS`) を含まない場合のみ metadata-only。
`find -exec cat .env ';'` のように `cat` を実行して内容を出力する形は対象外で
deny に倒る (Codex P1, 0.14.0)。

| コマンド | default | acceptEdits | auto | dontAsk | bypassPermissions |
|---|---|---|---|---|---|
| `ls -la .env`, `stat .env`, `file .env`, `du -h .env`, `tree .env` | allow | allow | allow | allow | allow |
| `find . -name .env`, `find . -name '.env*'` (アクション無し → 内容は出ない) | allow | allow | allow | allow | allow |
| `find . -name .env -printf '%p'` (`-printf` は stdout への metadata 出力) | allow | allow | allow | allow | allow |
| `wc -l .env` (計数のみ), `test -f .env` (存在確認) | allow | allow | allow | allow | allow |
| `echo .env`, `printf '%s' .env` (引数文字列の表示のみ) | allow | allow | allow | allow | allow |
| `realpath .env`, `readlink -f .env`, `basename /app/.env` | allow | allow | allow | allow | allow |
| `git check-ignore -v .env`, `git ls-files .env`, `git status` | allow | allow | allow | allow | allow |
| `ls -la .env > /tmp/x` (read operand 機密でも書込み先が非機密 → metadata allow) | allow | allow | allow | allow | allow |
| `find . -name .env -exec cat .env ';'` (`-exec` で内容露出可) | **deny** | **deny** | **deny** | **deny** | **deny** |
| `find . -name .env -delete` (`-delete` 副作用), `find ... -fprintf` (書込み) | **deny** | **deny** | **deny** | **deny** | **deny** |
| `ls > .env`, `ls >.env`, `stat x 1> .env`, `ls &> .env` (機密 path への redirect 書込み = 破壊的) | **deny** | **deny** | **deny** | **deny** | **deny** |
| `tree >\| .env` (`>\|` clobber は `\|` が segment 分割で割れる既知限界、思想 1 射程外) | allow | allow | allow | allow | allow |

> **metadata-only ∩ safe_read の redirect 書込み (Codex P2, 0.14.0)**: `ls` /
> `stat` / `wc` / `file` / `du` / `df` / `tree` は metadata-only かつ safe_read の
> ため residual metachar 判定を skip する。`ls > .env` のように機密 path へ
> redirect 書込みする形は内容露出こそ無いが `.env` を truncate する破壊的書込み
> なので、`_sensitive_redirect_target` で検出して **deny** に倒す (Edit/Write の
> 機密書込み deny と整合)。`>.env` / `>>.env` の fused 形も対応。`>\|` clobber
> override は `\|` が segment 分割で pipe として割れるため未対応 (obscure・思想 1
> 射程外)。read operand のみ機密で書込み先が非機密な `ls -la .env > /tmp/x` は
> allow 維持 (内容も破壊もしない)。
| `cat .env`, `head .env`, `grep KEY .env`, `od -c .env` (内容出力系は対象外) | **deny** | **deny** | **deny** | **deny** | **deny** |
| `cp .env /tmp/x`, `mv .env /tmp/x` (複製で漏洩面が広がるため対象外) | **deny** | **deny** | **deny** | **deny** | **deny** |
| `git show HEAD:.env`, `git diff .env`, `git add .env` (内容出力 / index 追加) | **deny** | **deny** | **deny** | **deny** | **deny** |
| `git -C /repo check-ignore .env` (global option 前置は保守的に対象外) | **deny** | **deny** | **deny** | **deny** | **deny** |
| `echo KEY=val > .env` (echo は safe-read 外: residual `>` が先に効き ask 維持) | ask | ask | **allow** | ask | **allow** |
| `find . -name .env -exec cat {} +` (`{}` hard-stop が先に効き ask 維持) | ask | ask | **allow** | ask | **allow** |
| `find . -name .env > /tmp/x` (find は safe-read 外: residual `>` で ask 維持) | ask | ask | **allow** | ask | **allow** |

## Bash handler — 静的解析不能 (三態判定)

> **0.11.0 (F1) 注記**: 以下は command を構成する全 segment がいずれも静的
> 解析不能 (hard-stop / shell keyword / opaque wrapper / shlex 失敗) な場合に
> のみ ask に倒る。0.11.0 から hard-stop は **segment 単位で再評価** されるため、
> 1 segment でも literal match があれば deny に到達する
> (`head .env || cat $X || echo done` 等は機密確定 match 表に分類)。
> 攻撃シナリオ `cat <(echo \(\)) < .env` は全 segment が hard-stop となるため
> 引き続き ask 扱い (思想 1 整合)。

| コマンド | default | acceptEdits | auto | dontAsk | bypassPermissions |
|---|---|---|---|---|---|
| `cat $X`, `cat "$X"`, `cat $(echo .env)` (動的展開) | ask | ask | **allow** | ask | **allow** |
| `cat << EOF ... EOF`, `cat <(cat .env)`, `cat <&2` | ask | ask | **allow** | ask | **allow** |
| `cat < .env`, `cat<.env`, `cat 0< .env`, `cat < ".env"` (`<` 入力リダイレクト, 0.7.0 で格下げ) | ask | ask | **allow** | ask | **allow** |
| `cat <<< '.env'` (herestring, literal 渡し) | ask | ask | **allow** | ask | **allow** |
| `(cat .env)`, `{ cat .env; }` (グループ化) | ask | ask | **allow** | ask | **allow** |
| `for i in 1; do cat .env; done`, `if true; then cat .env; fi` | ask | ask | **allow** | ask | **allow** |
| `while cat .env; do pwd; done`, `coproc cat .env` | ask | ask | **allow** | ask | **allow** |
| `time cat .env`, `! cat .env`, `exec cat .env`, `eval cat .env` | ask | ask | **allow** | ask | **allow** |
| `echo foo > out.txt`, `echo foo > '&2'` (allow-list 外 first_token + redirect) | ask | ask | **allow** | ask | **allow** |
| `find . -name '*.py' > files.txt` (allow-list 外 + redirect、副作用判定が複雑なため未組込) | ask | ask | **allow** | ask | **allow** |
| `/bin/cat .env`, `./cat`, `../bin/cat .env` (任意 path exec, 0.8.0 で opaque 統一) | ask | ask | **allow** | ask | **allow** |
| `/usr/bin/env FOO=1 cat .env`, `/bin/command cat .env` (任意 path exec, 0.8.0 で格下げ) | ask | ask | **allow** | ask | **allow** |
| `bash -c "cat .env"`, `sh -c "..."`, `zsh -c "..."` | ask | ask | **allow** | ask | **allow** |
| `sudo cat .env`, `xargs -a .env cat` | ask | ask | **allow** | ask | **allow** |
| `python -c "..."`, `node -e "..."`, `awk '{print}' f`, `sed s/x/y f` | ask | ask | **allow** | ask | **allow** |
| `env cat .env`, `env FOO=1 cat .env`, `env -i cat .env`, `env -u HOME cat .env` (`env`, 0.8.0 で opaque 統一) | ask | ask | **allow** | ask | **allow** |
| `command cat .env`, `command -p cat .env`, `command -- cat .env` (`command`, 0.8.0 で opaque 統一) | ask | ask | **allow** | ask | **allow** |
| `builtin cat .env`, `nohup cat .env`, `nohup command cat .env` (`builtin` / `nohup`, 0.8.0 で opaque 統一) | ask | ask | **allow** | ask | **allow** |
| `FOO=1 cat .env`, `FOO=1 BAR=2 cat .env` (env-assignment prefix, 0.8.0 で格下げ) | ask | ask | **allow** | ask | **allow** |
| `cat .env.*`, `cat .env.example*`, `cat *.log` (dotenv stem 不一致 glob, 0.8.0 で格下げ) | ask | ask | **allow** | ask | **allow** |
| `cat id_rsa*`, `cat id_*`, `cat *.key`, `cat cred*.json` (rules 候補列挙撤廃, 0.8.0 で格下げ) | ask | ask | **allow** | ask | **allow** |
| `cat '.env` (shlex 失敗) | ask | ask | **allow** | ask | **allow** |

## Bash handler — ポリシー欠如 / 内部失敗

| ケース | default | acceptEdits | auto | dontAsk | bypassPermissions |
|---|---|---|---|---|---|
| `patterns.txt` 読込失敗 | **deny** + stderr | **deny** + stderr | **deny** + stderr | **deny** + stderr | **deny** + stderr |
| empty command / rules 空 | allow | allow | allow | allow | allow |

## Edit/Write handler

| ケース | default | acceptEdits | auto | dontAsk | bypassPermissions |
|---|---|---|---|---|---|
| 既存 `.env` を Edit/Write | **deny** (hook 到達なし: Read 前提) | **deny** | **deny** | **deny** | **deny** |
| **新規** `.env` を Write (作成) | **deny** | **deny** | **deny** | **deny** | **deny** |
| `.env.example` / `config.template` を Edit/Write | allow | allow | allow | allow | allow |
| path 最終要素が symlink (機密一致) | **deny** | **deny** | **deny** | **deny** | **deny** |
| path 最終要素が special (FIFO/socket/device) | **deny** | **deny** | **deny** | **deny** | **deny** |
| 親ディレクトリが symlink / 特殊 / 不在 | ask | ask | ask | ask | **deny** |
| patterns.txt / normalize / stat 失敗 | ask | ask | ask | ask | **deny** |

## Stop handler

| ケース | 全 mode (Stop は permission_mode を使わない) |
|---|---|
| `stop_hook_active=true` | exit 0 (ループ防止) |
| cwd が git 管理下でない | exit 0 |
| tracked でパターン一致 | `decision: block` (`.gitignore` 済みでも) |
| untracked でパターン一致 + `.gitignore` 未登録 | `decision: block` |
| patterns.txt 読込失敗 | **exit 0 + stderr warning** (fail-open) |

## `__main__` catch-all (handler 内未捕捉例外)

| ケース | default | acceptEdits | auto | dontAsk | bypassPermissions |
|---|---|---|---|---|---|
| handler 内未捕捉例外 | ask | ask | ask | ask | **deny** |

**0.3.3 時点で `__main__` catch-all は未緩和**。tool 種別だけで一律 `ask_or_allow`
化すると fail-closed 境界が粗くなるため、0.3.4 以降で「特定の意図された例外
クラスのみ allow 緩和」として再設計予定。

## hook timeout (2 秒)

| ケース | 全 mode |
|---|---|
| hook timeout (2s) | **allow** (介在不能、Claude Code が続行) |

timeout だけ fail-open — hook プロセス自体が応答不能だと deny/ask を返せない。
代わりに timeout を短く (2 秒) し発生頻度を抑える方針。
