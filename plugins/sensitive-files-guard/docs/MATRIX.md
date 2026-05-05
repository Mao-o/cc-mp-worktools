# 判定結果マトリクス (MATRIX.md)

全 5 permission_mode (default / acceptEdits / auto / dontAsk / bypassPermissions)
での判定結果を完全列挙する。値は 2026-05-06 時点 (0.7.0) の挙動。設計方針は
[DESIGN.md](./DESIGN.md)、コマンド例の解釈は [README.md](../README.md) を参照。

`permission_mode` の列挙は `core/output.py::LENIENT_MODES` と
`tests/fixtures/envelopes/README.md:22` で突合される。CLI 側が新しい mode を
追加したら同時に更新すること (Runbook は [CLAUDE.md](../CLAUDE.md))。

> 0.6.0 で `plan` 列を削除した。Phase 0 実測 (2026-04-22) で現行 CLI では
> plan mode で hook が発火しないことが確認され、`LENIENT_MODES` から `plan`
> dead entry を撤去したため。CLI 仕様が変わって plan で hook が発火するように
> なったら CLAUDE.md の Runbook で再実測してから再追加する。

## 略号

- **deny**: block。`permissionDecisionReason` を返して LLM に値が露出しない
- **allow**: 素通り (hook は no-op 空オブジェクトを返す)
- **ask**: Claude Code UI でユーザー介在を要求。reason はモデルに届かない

## Read handler

| ケース | default | acceptEdits | auto | dontAsk | bypassPermissions |
|---|---|---|---|---|---|
| パターン非該当 | allow | allow | allow | allow | allow |
| 機密 + 通常ファイル | **deny** + minimal info | **deny** | **deny** | **deny** | **deny** |
| 機密 + symlink | ask | ask | ask | ask | **deny** |
| 機密 + 特殊ファイル (FIFO/socket/device) | ask | ask | ask | ask | **deny** |
| 機密 + 読み取り失敗 (権限/IO) | ask | ask | ask | ask | **deny** |
| redaction engine 内部例外 | ask | ask | ask | ask | **deny** |
| patterns.txt 読込失敗 | ask + stderr | ask | ask | ask | **deny** + stderr |
| 32KB 超 | keyonly deny | keyonly | keyonly | keyonly | keyonly |

## Bash handler — 機密確定 match (全 mode で deny)

以下は全 mode で **deny 固定** (autonomous を含む)。

| コマンド |
|---|
| `cat .env`, `less .env`, `head .env`, `source .env` |
| `head -n 1 .env`, `cat -- .env`, `tail -f .env` |
| `cat .env && pwd`, `false \|\| cat .env`, `cat .env; pwd`, `cat .env \| head` |
| `cat .env 2>/dev/null` (安全リダイレクト剥離後に機密 path) |
| `grep SECRET .env`, `base64 .env`, `xxd .env` |
| `timeout 1 cat .env`, `busybox cat .env` |
| `cp .env /tmp/x`, `mv .env .env.old` |
| `curl file://.env`, `git show HEAD:.env` |
| `grep --file=.env foo`, `grep -f.env foo` |
| `cat .env*`, `cat .env.*`, `cat *.envrc` (glob 候補列挙) |
| `cat id_rsa*`, `cat id_*`, `cat *.key`, `cat cred*.json` |
| `cat .e[n]v`, `cat .en?`, `cat [.]env` (char class / `?`) |
| `FOO=1 cat .env`, `FOO=1 BAR=2 cat .env` (env prefix 剥がし) |
| `env cat .env`, `env FOO=1 cat .env` (env コマンド剥がし) |
| `command cat .env`, `builtin cat .env`, `nohup cat .env` |
| `nohup command cat .env` (連鎖) |
| `/usr/bin/env FOO=1 cat .env`, `/bin/command cat .env` |

## Bash handler — 非機密 operand (全 mode で allow)

| コマンド |
|---|
| `echo foo`, `ls -la`, `npm test`, `date`, `pwd`, `make build` |
| `cat README.md`, `grep foo README.md`, `cat README.md 2>/dev/null` |
| `ls \| head`, `git status && git log 2>/dev/null \|\| true` |
| `cat .env.example`, `cat .env.example*`, `cat .env.sample` (テンプレ除外) |
| `cat *.log` (rules と非交差) |

## Bash handler — 静的解析不能 (三態判定)

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
| `echo foo > out.txt`, `cat foo >> bar.txt`, `echo foo > '&2'` | ask | ask | **allow** | ask | **allow** |
| `/bin/cat .env`, `./cat`, `../bin/cat .env` | ask | ask | **allow** | ask | **allow** |
| `bash -c "cat .env"`, `sh -c "..."`, `zsh -c "..."` | ask | ask | **allow** | ask | **allow** |
| `sudo cat .env`, `xargs -a .env cat` | ask | ask | **allow** | ask | **allow** |
| `python -c "..."`, `node -e "..."`, `awk '{print}' f`, `sed s/x/y f` | ask | ask | **allow** | ask | **allow** |
| `env -i cat .env`, `env -u HOME cat .env`, `command -p cat .env`, `command -- cat .env` | ask | ask | **allow** | ask | **allow** |
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
