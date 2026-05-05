# sensitive-files-guard 設計詳細 (DESIGN.md)

利用者向けの要約は [README.md](../README.md)、保守者向けの実務ガイドは
[CLAUDE.md](../CLAUDE.md)、判定結果の完全マトリクスは [MATRIX.md](./MATRIX.md)、
パターン設定の詳細は [PATTERNS.md](./PATTERNS.md) を参照。

本ドキュメントは「**なぜこの設計にしたか**」の根拠と実測ログを集約する。

## 設計原則

1. **Fail-closed in doubt** — read 側の内部失敗は `ask` (bypass モード時は `deny`)
   にフォールバック。Stop 側は応答停止を招かないため fail-open (stderr warning +
   空出力)。
2. **値そのものは出さない、デバッグ情報は積極的に返す** (0.9.0 で拡張) —
   minimal info の核は鍵名・順序・型・件数だが、思想 2 (block 時は意図を
   汲んだメッセージを返す) を満たすため、値の **品質情報** (set / empty /
   placeholder / short / long / looks_truncated) と長さ (生バイト数)、
   識別子型の prefix (sk_live_ / AKIA / ghp_ 等) を併せて返す。実値そのもの
   (鍵名 prefix を除く一切) は LLM の文脈に入れない原則は維持。
3. **Secrets never in logs** — path・値・展開後情報を一切記録しない。
4. **Latency <100ms 目標** — timeout 2 秒、文字列処理のみ、外部コマンド呼出なし。
5. **情報注入は `permissionDecisionReason` 一択** — `systemMessage` 非依存
   (後述 Phase 0 実測参照)。

## Phase 0 実測結果

### 2026-04-11 — PreToolUse envelope と reason 配信経路

- `permissionDecisionReason` は deny 時に 1KB/8KB/32KB までモデルに完全配信される
- `systemMessage` トップレベルは **モデルに届かない** (公式 docs の誤り)。依存禁止
- `ask` reason はモデルには届かず、ユーザー UI のみ。bypass モードでは自動 allow
- envelope には `permission_mode` フィールドがあり bypass / plan 等の検出に使える
- `tool_input` 形状: `Read:file_path` / `Bash:command,description` など

詳細は `~/shared-context/security/claude-code-pretooluse-hook-spec.md` に恒久記録。

### 2026-04-22 — plan mode での hook 発火有無 (0.3.3 → 0.6.0 で撤去)

`hooks/_debug/capture_envelope.py` (一時スクリプト) で実測。
現行 CLI (2.1.101 系) では **plan mode で PreToolUse hook が発火しない** 観測
(= Case C)。

0.3.3 では「将来 CLI が plan mode でも hook を発火させるよう変わったときの
前方互換層」として `LENIENT_MODES` に `"plan"` を加えていたが、0.6.0 で
**「想像できる将来のための dead code は思想に反する」** という方針に基づき
撤去した (REVIEW_TASKS_2026-05-06.md A5)。

CLI 仕様が変わって plan mode で hook が発火するようになったら、
[CLAUDE.md](../CLAUDE.md) の "CLI バージョンアップ時の再実測手順" Runbook で
再実測した上で `LENIENT_MODES` に再追加する。

## LENIENT_MODES 方針

`core/output.py::ask_or_allow` は bash handler の静的解析不能ケースで使う三態
判定。`permission_mode` が `LENIENT_MODES` に含まれれば allow に、そうでなければ
ask に倒す。

| mode | `ask_or_allow` | 理由 |
|---|---|---|
| `default` | ask | 明示的にユーザー介在を期待 |
| `plan` | ask | 0.3.3 で前方互換のため allow にしていたが 0.6.0 で撤去 (現行 CLI では hook 非発火 dead entry) |
| `acceptEdits` | ask | Edit/Write 専用モード。Bash lenient の意図なし |
| `auto` | allow | CLI 前段 classifier モード、autonomous 実行意図 |
| `dontAsk` | ask | 明示的な非 lenient 判断として既存方針維持 |
| `bypassPermissions` | allow | 全確認スキップモード、autonomous 実行意図 |

Read/Edit handler の `ask_or_deny` は別 frozenset で `bypassPermissions` のみ
deny に倒す (機密可能性があるものは ask 維持で bypass だけ deny する)。

## Bash handler の対応文法範囲

Bash handler の静的解析は **shlex.split (POSIX mode)** ベース。
`bash_handler.py::handle` 内で `_split_command_on_operators` (quote-aware セグメント
分割) 後の各 segment を shlex.split し、コマンド token 単位で解析する
(opaque first token 判定, shell keyword 検出, operand scan)。

### 対応 (deny/allow 確定できる)

| カテゴリ | 構文 | 使用 parser |
|---|---|---|
| セグメント区切り | `cmd1 && cmd2`, `cmd1 \| cmd2` | `_split_command_on_operators` (0.3.0) |
| operand scan (literal) | `cmd file1 file2`, URI/VCS | shlex token (`_operand_is_sensitive`) |
| operand scan (glob, dotenv stem 一致) | `cat .env*`, `cat *.envrc`, `cat .e[n]v` | shlex token + ``fnmatchcase`` (`_glob_operand_is_dotenv_match`, 0.8.0) |

### 対応外 (opaque `ask_or_allow` 扱い)

| 構文 | 備考 |
|---|---|
| `<` 入力リダイレクト全般 (`cmd < t`, `cmd<t`, `cmd 0<t`, `cmd < "t"` 等) | 0.3.4〜0.6.x で character-level parser による target 抽出 + literal/glob 一致 deny を行っていたが、escape paren depth tracking や `[[ ... ]]` 引数位置判定など敵対的バイパス対策のコード負債が思想 1 (うっかり露出予防、敵対的防御は非目的) と整合しないため 0.7.0 で撤廃。`<` を含む command は他の hard-stop と同じ ``ask_or_allow`` |
| `<<` heredoc, `<<-` | delimiter/body は read 対象外 |
| `<<<` herestring | literal 渡しで file read ではない |
| `<&N`, `<&-` fd dup | 既存 fd 複製、file read ではない |
| `<(cmd)` process sub | Bash 拡張、`hard_stop` 経由で opaque |
| `$(cmd)`, `` `cmd` `` | 動的展開。静的解析不能 |
| `[[ cond ]]`, `(( expr ))` | Bash 条件式 / 算術。hard_stop で opaque |
| `bash -c "..."`, `eval`, `python -c` | wrapper。内部 script は未解析 |
| 第一トークンが env-assignment (`FOO=1`) / `env` / `command` / `builtin` / `nohup` / 任意 path exec (`/bin/cat`, `./script`) | 0.3.2〜0.7.x で透過剥がし (`FOO=1 cat .env` を `cat .env` と解釈) で deny に倒していたが、思想 1 (うっかり露出予防、敵対的防御は非目的) に整合しないため 0.8.0 で撤廃。これらは ``ask_or_allow`` |
| operand glob (`*` / `?` / `[` 含む) で dotenv literal stem (`.env` / `.envrc`) に fnmatch しないもの (`id_rsa*` / `*.key` / `cred*.json` / `*.log` / `.env.*` / `.env.example*` 等) | 0.3.2〜0.7.x で既定 rules 候補列挙 (`_glob_candidates` / `_glob_operand_is_sensitive`) で deny に倒していたが、`*.log` 等の日常 glob まで巻き込む False positive と思想 1 不整合のため 0.8.0 で撤廃。dotenv stem 一致のみ deny を維持 |

### 観測ログ

`L.log_info("bash_classify", ...)` の tag:

- `opaque_prefix_lenient` / `segment_residual_metachar_lenient` /
  `shell_keyword_lenient:<kw>` / `shlex_fail:<err>` / `hard_stop_lenient` — 各 lenient 経路
- `match:<first_token>` / `glob_match:<first_token>` — operand scan で deny 確定

## Bash handler 判定フロー

0.3.2 で確定した三態判定は 0.3.3 でも挙動変更なし。詳細な mermaid フローは
[CLAUDE.md](../CLAUDE.md) 側に集約。コマンド別の deny/allow/ask 一覧は
[MATRIX.md](./MATRIX.md) を参照。

### 責務境界 (0.3.3 再設計、0.8.0 で簡素化)

0.3.3 では `bash_handler.py` が 662 行に肥大化していたのを、責務境界で以下のよう
に分割した:

| モジュール | 責務 | 依存先 |
|---|---|---|
| `bash_handler.py` | orchestration + plugin ステート依存 + test seam | `core.*`, `handlers/bash/*` |
| `handlers/bash/constants.py` | compile-time 定数 (regex / frozenset) | なし |
| `handlers/bash/segmentation.py` | quote-aware セグメント分割 / hard-stop 検出 | `constants` |
| `handlers/bash/operand_lexer.py` | glob 判定 / dotenv glob 一致 / path 候補抽出 | `constants` |
| `handlers/bash/redirects.py` | 安全リダイレクト剥離 / 残留 metachar 判定 | `constants` |

`handlers/bash/` 配下のモジュールは **副作用なし・plugin ステート非依存**
(SFG_CASE_SENSITIVE 環境変数の参照のみ、`is_sensitive` 側と整合)。

テストは `handlers.bash_handler.X` の形で patch / import するため、
`bash_handler.py` は以下の symbol を **再 export** して従来の import path を
維持する:

- `handle` (orchestration)
- `_operand_is_sensitive` (plugin ステート依存)
- `_glob_operand_is_dotenv_match` (0.8.0 新設、operand glob と dotenv stem の fnmatch)
- `load_patterns` (test_failclosed.py の `mock.patch` 対象)
- 各定数 (test が直接参照する可能性に備えて)

> 0.7.x までの patch seam だった `_normalize_segment_prefix` (prefix 透過処理)、
> `_literalize` / `_glob_candidates` / `_glob_operand_is_sensitive` (既定 rules
> 候補列挙) は 0.8.0 で撤廃。`_is_absolute_or_relative_path_exec` も同時撤廃。

## 判定ロジックの詳細

### Read handler

| ケース | 判定 |
|---|---|
| パターン非該当 | no-op |
| 機密 + 通常ファイル成功 | `deny` + minimal info を `permissionDecisionReason` |
| 機密 + symlink | `ask_or_deny` |
| 機密 + 特殊ファイル (FIFO/socket/device) | `ask_or_deny` |
| 機密 + 読み取り失敗 (権限/IO) | `ask_or_deny` (fail-closed) |
| redaction engine 内部例外 | `ask_or_deny` (fail-closed) |
| patterns.txt 読込失敗 | `ask_or_deny` + stderr 警告 |
| サイズ 32KB 超 | keyonly_scan で streaming 鍵名抽出 |

### dotenv minimal info の拡張 (0.9.0, E1 + E2)

`redaction/dotenv.py` で生成する minimal info に以下を追加 (実値は出さない):

| 項目 | 内容 | 例 |
|---|---|---|
| `<type=...>` | 値クラス (14 種、0.9.0 で 8 種拡張) | `<type=stripe_secret>` |
| `prefix="..."` | 識別子型のみ、公開済み prefix を表示 (Q3 採用) | `<type=stripe_secret prefix="sk_live_">` |
| `<set>` / `<empty>` / `<placeholder>` | 値の有無・placeholder 一致 | `<set>` |
| `<short>` / `<long>` / `<looks_truncated>` | 型整合性・truncation ヒント (複数併記可) | `<set> <short>` |
| `length=<N>` | 値のバイト長 (Q2 採用、bucket せず生長さ) | `length=68` |
| `matched="..."` | placeholder 一致時の辞書 literal / pattern label | `matched="your_jwt_secret_here"` |

**型推定 (0.9.0 拡張)**: `str` / `bool` / `null` / `num` / `jwt` (既存) +
`url` / `email` / `uuid` / `aws_access_key` (AKIA / ASIA) / `stripe_secret`
(sk_live_ / sk_test_ / rk_live_ / rk_test_) / `stripe_pk` (pk_live_ / pk_test_) /
`github_pat` (ghp_ / gho_ / ghu_ / ghs_ / ghr_) / `openai_key` (sk-)。

**prefix を返す型**: jwt (`ey`)、aws_access_key (`AKIA` / `ASIA`)、stripe_secret
(`sk_live_` / `sk_test_` / `rk_live_` / `rk_test_`)、stripe_pk (`pk_live_` /
`pk_test_`)、github_pat (`ghp_` / `gho_` / `ghu_` / `ghs_` / `ghr_`)、openai_key
(`sk-`)。

**short の閾値**: jwt < 30 / aws_access_key < 16 / stripe_* < 25 /
github_pat < 30 / openai_key < 20 / url < 8 / uuid < 36 / email < 6。
**long の閾値**: 4096 byte 超 (デバッグダンプ混入の検知)。

**placeholder 判定**: `redaction/placeholders.py::looks_placeholder` が
PLACEHOLDER_LITERALS (21 個) と PLACEHOLDER_PATTERNS (5 個 regex) で判定。
ユーザー拡張点 (placeholders.local.txt) は **作らない** (Q1 = 簡易版で開始)。

### Bash handler (三態判定)

コマンド別の deny / allow / ask は [MATRIX.md](./MATRIX.md) を参照。mermaid
フロー図は CLAUDE.md 側にある。

**unified operand scan**: 全セグメントで非 option トークンを一律
`_operand_is_sensitive` (literal path / URI / VCS pathspec) または
`_glob_operand_is_dotenv_match` (glob 含み、dotenv stem 一致) に通す。コロンを
含む operand (`HEAD:.env`, `user@host:/p/.env`) はコロン分割後の各片の basename も
判定。コマンドが実際に file を読むかどうかは静的に判別しないため false positive
(`echo .env`, `ls .env`, `mkdir .env`) が出るが、`patterns.local.txt` の
`!<basename>` exclude で個別対処できる。glob で dotenv stem と一致しないものは
``ask_or_allow`` (0.8.0)。

### Edit/Write handler

| ケース | 判定 |
|---|---|
| 機密 path への新規/既存 書き込み (通常ファイル) | **`deny` 固定** + dotenv ならキー名を reason に添える |
| 機密 path + symlink / special | **`deny` 固定** + 対応の extra note |
| `.env.example` 等テンプレ除外 | allow |
| 親ディレクトリが symlink / 特殊 / 不在 | `ask_or_deny` (判定不能、fail-closed) |
| patterns.txt 読込失敗 / normalize 失敗 / stat 失敗 | `ask_or_deny` (fail-closed) |

deny reason のキー名ガイド:
- dotenv 系 basename (`_detect_format(basename) == "dotenv"`) の時だけ
  `tool_input` からキー名抽出 (Edit=new_string / Write=content)
- 抽出結果を reason に箇条書きで添え、`.env.example` への移行を促す
- 値そのものは一切 reason に含めない (キー名のみ、既存の minimal-info 原則と一致)

`ask_or_deny`: `permission_mode == "bypassPermissions"` なら `deny`、それ以外は
`ask`。**機密検出済み** のケースは `ask` を挟まず常に `deny` 固定 (うっかり
承認防止)。

### Stop handler

| ケース | 判定 |
|---|---|
| `stop_hook_active=true` | exit 0 (ループ防止) |
| cwd が git 管理下でない | exit 0 |
| tracked でパターン一致 | `decision: block` (`.gitignore` 済みでも) |
| untracked でパターン一致 + `.gitignore` 未登録 | `decision: block` |
| patterns.txt 読込失敗 (FileNotFoundError / OSError) | exit 0 + stderr warning (fail-open) |

## 既知制限 (0.8.0 時点)

1. **MCP 経路は対象外** — MCP server 経由のファイルアクセスは hook が介在しない
2. **Bash 間接アクセス (静的解析不能)** — `bash -c`, `eval`, `python3 -c`, `sudo`,
   `awk`, `sed`, `xargs`, heredoc, process substitution, `/bin/cat`, `./script`
   などは静的解析できず、default モードでは ask、auto/bypass モードでは
   **allow** に倒す。0.8.0 で `FOO=1 cat .env`, `env cat .env`,
   `command cat .env`, `nohup cat .env`, `/usr/bin/env FOO=1 cat .env` も
   ``ask_or_allow`` に格下げした (0.3.2〜0.7.x の prefix normalize は撤廃)。
3. **`<` 入力リダイレクトは ask_or_allow 扱い (0.7.0)** — 0.3.4〜0.6.x では
   character-level quote-aware parser で `cat < .env` / `cat<.env` /
   `cat 0<.env` / `cat < ".env"` などから target を抽出し literal/glob 一致で
   deny に倒していたが、`cat <(echo \(\)) < .env` の escape paren depth
   tracking や `[[ ... ]]` 引数位置判定など敵対的バイパス対策のコードが
   思想 1 (うっかり露出予防が目的、敵対的防御は非目的) に反するため 0.7.0
   で撤廃。`<` を含む command は他の hard-stop と同じ ``ask_or_allow``
   (default で ask、autonomous で allow) に倒す。
4. **glob operand の判定は dotenv stem 限定 (0.8.0)** — 0.3.2〜0.7.x で行っていた
   既定 rules 候補列挙 (`_glob_candidates` / `_glob_operand_is_sensitive`) は
   `cat *.log` `cat *.json` のような日常 glob まで「`.env` rule との連結候補」で
   deny に巻き込む False positive があり、思想 1 と整合しないため 0.8.0 で撤廃。
   現在は operand glob が dotenv literal stem (`.env` / `.envrc`) に
   ``fnmatchcase`` で一致するときだけ deny 固定 (`cat .env*`, `cat *.envrc`,
   `cat .e[n]v`, `cat .en?`, `cat [.]env`)。それ以外の glob (`id_rsa*`,
   `*.key`, `cred*.json`, `*.log`, `.env.*`, `.env.example*` 等) は
   ``ask_or_allow`` (default=ask, autonomous=allow)。
5. **autonomous モードでの opaque 緩和** — `bash -c 'cat .env'` の
   ような shell wrapper 内に機密 path があっても auto/bypass では allow に
   倒る。wrapper 内部の script を解析しないため検出できない。autonomous モード
   を選んだユーザーが「日常コマンドを止めない」意図と平等な扱いとしての設計上の
   トレードオフ。完全防御を求める場合は default モードで運用する。
6. **`__main__.py` catch-all は未緩和** — bash handler 内部で未捕捉
   例外が起きた場合、`__main__` 側の catch-all は従来通り `ask_or_deny`
   (auto=ask / bypass=deny)。tool 種別だけで一律 lenient にすると fail-closed
   境界が粗くなる。
7. **親ディレクトリ差し替え race** — `O_NOFOLLOW` は最終要素のみ保護し、
   途中要素の symlink 差し替え race は対象外 (原理的に完全防御不能)
8. **TOCTOU 完全排除は非目的** — hook 読取と Claude 実 Read/Write の分離は範囲外
9. **`<DATA untrusted>` モデル解釈保証なし** — 包装 + sanitize + DATA タグ
   エスケープで多段防御するが、モデルが敵対的文脈として扱う保証は無い
10. **Windows は fail-closed で deny exit** — SIGALRM 非対応のため hook 冒頭で
    deny exit する (Step 0-c 実測結果確定前の暫定方針)
11. **submodule 内 untracked は非対象** — `git ls-files --recurse-submodules` は
    tracked のみ。untracked を submodule 内まで拾う git native オプションは無い
12. **Git バージョン依存** — `--recurse-submodules` は git 1.7+ が必要
13. **`!` プレフィックス (Claude Code bash mode) は防御対象外** — ユーザーが
    プロンプトに `! cat .env` と直接入力してシェルコマンドを実行した場合、
    公式仕様により **stdout が transcript に追加されて LLM コンテキストに流れ
    込む**。これはユーザーの明示的な意思操作なので hook の介在外

## Edit/Write hook の発火経路 (2026-04-18 実機観測)

Claude Code CLI 2.1.112 における **Edit/Write tool の PreToolUse hook** は、tool
呼び出しの状況によって発火の有無が変わる:

| 操作 | 既存ファイル | 新規作成 |
|---|---|---|
| `Edit` | **hook 未到達** (Read 前提チェックで先に `File must be read first`) | — (Edit は既存前提) |
| `Write` | **hook 未到達** (同上、`Error writing file`) | **hook 発火** (redact-sensitive-reads deny で block) |

現在の防御は二層構造:

1. **本線 (hook)**: 新規作成 Write → edit_handler → deny
2. **副次 (Claude Code 内蔵)**: 既存ファイル Edit/Write → Read 前提チェック → 内部エラー

redact hook が Read を deny している状態では、Claude が Read を試みると失敗 →
Claude が Edit/Write を試みても「Read 済み」にならないため Claude Code が先に弾く。
この **Read 前提チェックの副次防御** により、既存機密ファイルの Edit/Write は
hook まで到達しなくても block される。

将来 Claude Code がこの仕様を変更した場合 (例: bypass モードで Read 前提を緩和)、
副次防御が消えるため**本線の hook が唯一の防御になる**。したがって Edit/Write の
matcher と edit_handler は dead code ではなく、**設計上の必須コンポーネント**。

## glob operand 判定の歴史 (0.3.2 → 0.8.0)

operand glob (`*` / `?` / `[`) の判定は数世代を経ている:

- **0.3.2〜0.7.x**: `_glob_candidates` で operand glob と既定 rules の literal stem を
  fnmatch 交差させて候補化し、`_glob_operand_is_sensitive` で is_sensitive 判定。
  プランの初期案に「op_stem + pt_stem 連結候補」を加える項目もあったが、`*.log`
  に対して `.env` rule との連結 `.env.log` が候補化されて `cat *.log` が deny に
  巻き込まれる False positive があり、連結候補は不採用としていた。
- **0.8.0**: `_glob_operand_is_sensitive` / `_glob_candidates` / `_literalize` を全
  撤廃。dotenv literal stem (`.env` / `.envrc`) に operand glob が ``fnmatchcase``
  で一致するかだけ見る `_glob_operand_is_dotenv_match` に置換。
  `cat *.key` `cat id_rsa*` `cat cred*.json` `cat *.log` `cat .env.example*` 等は
  すべて ``ask_or_allow`` (default=ask, autonomous=allow) に格下げ。思想 1
  (うっかり露出予防、敵対的防御は非目的) に整合させた結果。

## Step 0-c 実測 (将来更新予定)

プラン v3 の Step 0-c (outer timeout 発火時の Claude 挙動実測) は未実施。
暫定方針として Case A (timeout kill → allow/fail-open の最悪ケース想定) で
Windows (SIGALRM 非対応) を hook 冒頭で deny exit にしている。

実測手順は [CLAUDE.md](../CLAUDE.md) の "Step 0-c 実測結果" セクション参照。
