# sensitive-files-guard 設計詳細 (DESIGN.md)

利用者向けの要約は [README.md](../README.md)、保守者向けの実務ガイドは
[CLAUDE.md](../CLAUDE.md)、判定結果の完全マトリクスは [MATRIX.md](./MATRIX.md)、
パターン設定の詳細は [PATTERNS.md](./PATTERNS.md) を参照。

本ドキュメントは「**なぜこの設計にしたか**」の根拠と実測ログを集約する。

## 設計原則

1. **Fail-closed in doubt** — read 側の内部失敗は `ask` (bypass モード時は `deny`)
   にフォールバック。Stop 側は応答停止を招かないため fail-open (stderr warning +
   空出力)。
2. **値そのものは出さない、デバッグ情報は積極的に返す** (0.9.0 で Read 側を
   拡張、0.10.0 で Bash 側にも適用) — minimal info の核は鍵名・順序・型・
   件数だが、思想 2 (block 時は意図を汲んだメッセージを返す) を満たすため、
   値の **品質情報** (set / empty / placeholder / short / long / looks_truncated)
   と長さ (生バイト数)、識別子型の prefix (sk_live_ / AKIA / ghp_ 等) を
   併せて返す。0.10.0 で Bash deny でも operand path の dotenv を実 read して
   Read 同等の minimal info を reason 内に埋め込むようにし、grep family では
   pattern から抽出した env-var 名と dotenv parse 結果を照合した
   `matched_pattern_keys` を出す。実値そのもの (鍵名 prefix を除く一切) は
   LLM の文脈に入れない原則は維持。
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
当時の CLI (2.1.101 系) では **plan mode で PreToolUse hook が発火しない** 観測
(= Case C)。

0.3.3 では「将来 CLI が plan mode でも hook を発火させるよう変わったときの
前方互換層」として `LENIENT_MODES` に `"plan"` を加えていたが、0.6.0 で
**「想像できる将来のための dead code は思想に反する」** という方針に基づき
撤去した (REVIEW_TASKS_2026-05-06.md A5)。

### 2026-05-18 — plan mode で Bash hook が発火する観測 (0.13.0 で再追加)

ユーザー実機で plan mode 中に `grep ... | head -50` のような調査ワンライナーが
sensitive-files-guard の `ask_or_allow` を経由して ask に倒れ、確認ダイアログが
出る現象を確認。2026-04-22 時点の "Case C: 非発火" 観測と乖離している。
CLI バージョンアップ (2.1.101 → 2.1.x 系) のどこかで plan mode 中も Bash
PreToolUse hook を発火する仕様変更が入ったものとみなす。

0.13.0 で `LENIENT_MODES` に `"plan"` を再追加し、`ask_or_allow` (Bash 静的解析
不能ケース) の plan 挙動を allow に倒す。plan mode は副作用が plan 承認まで
保留される dry-run 的な状態のため、autonomous (auto / bypass) と同等の lenient
扱いで操作性を優先する。機密 path 確定 match (`make_deny`) と Read/Edit handler
の `ask_or_deny` は plan mode でも引き続き安全側 (deny / ask) を維持。

## LENIENT_MODES 方針

`core/output.py::ask_or_allow` は bash handler の静的解析不能ケースで使う三態
判定。`permission_mode` が `LENIENT_MODES` に含まれれば allow に、そうでなければ
ask に倒す。

| mode | `ask_or_allow` | 理由 |
|---|---|---|
| `default` | ask | 明示的にユーザー介在を期待 |
| `plan` | allow | plan 承認まで副作用が保留される dry-run 状態のため (0.13.0 で auto と同等の lenient 扱いに復活) |
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

### read-only first_token allow-list (0.12.0)

`_SAFE_READ_FIRST_TOKENS` (副作用なしの read-only コマンド: `ls cat head tail
nl tac bat less more view wc file stat du df tree grep egrep fgrep rg ag ack
od xxd hexdump`) を `handlers/bash/constants.py` に定義。第一トークンがこの
セットに該当する segment は、`_segment_has_residual_metachar` の ask 経路を
**スキップ** して operand scan に直行する。

導入背景: 0.11.0 までの実測ログで `bash_classify` の ask 発火の **約 80%** が
`segment_residual_metachar_lenient` (= `>` 出力リダイレクトや `&` background
を含むコマンド) 起因だった。`grep foo README.md > /tmp/out` のような調査用
ワンライナーが ask に倒れて UX を阻害していたため、副作用なしの read-only
コマンドに限り redirect / background を許容する。

安全 net:
- 機密 redirect target (`grep foo > .env`) は operand scan で `.env` を捕まえて
  deny 固定。
- hard-stop (`$(...)` / backtick / heredoc / `<`) は依然 `ask_or_allow` (= 静的
  解析不能、shell 展開で別コマンド出力が混入する経路を塞げないため)。
- `_OPAQUE_WRAPPERS` (`awk`, `sed`, `bash -c`, `eval`, `sudo` 等) / `_SHELL_KEYWORDS`
  (`if`, `for` 等) は allow-list と disjoint なので、これらの ask 経路は不変。
- `find` は `-delete` / `-exec` で副作用持ちうるため allow-list **外**。
- `echo` は stdout 出力で「見る・数える」とは異なるため allow-list **外**。

**0.11.0 (F1)**: hard-stop char (`$`, バッククォート, `(`, `)`, `{`, `}`, `<`,
`\r`) は **segment 単位で再評価** する。0.10.0 までは command 全体に hard-stop
が 1 つでもあると `ask_or_allow` で early return していたため、
`cat .env | sed 's/(=)/X/'` のような複合で sed segment の `(` が原因で全体
ask に倒れ autonomous で `cat .env*` が素通りしていた。0.11.0 では segment ごとに
`_has_hard_stop` を再判定し、hard-stop / shlex 失敗の segment は `pending_ask`
に格納して continue (deny 確定 segment を優先し、無ければ最後に `pending_ask` を
畳む)。攻撃シナリオ `cat <(echo \(\)) < .env` は全 segment が hard-stop と
なるため挙動不変 (思想 1 整合)。

### metadata-only first_token allow-list (0.14.0)

`_METADATA_ONLY_FIRST_TOKENS` (`ls tree stat file du df test wc basename
dirname realpath readlink echo printf`) と `_GIT_METADATA_SUBCOMMANDS`
(`check-ignore` / `ls-files` / `status`、`git <sub>` 直書き形のみ) を
`handlers/bash/constants.py` に定義。該当 segment は **operand scan 自体を
スキップして allow** に倒す (機密 operand でも deny しない)。`find` は単体
集合に含めず、`_FIND_DANGEROUS_ACTIONS` (`-exec` / `-execdir` / `-ok` /
`-okdir` / `-delete` / `-fprint*` / `-fls`) を含まない場合のみ metadata-only
として扱う条件付き判定にする (後述)。

導入背景: 離脱分析 (2026-05、transcript 実測) で、実 deny 15 件のうち
`find -name X` / `ls -la X` / `git check-ignore X` のような所在・属性確認が
1/3 を占めた。これらは operand の **内容** を stdout に出さないため、deny して
も露出予防効果がなく、ユーザー離脱 (plugin 無効化) だけが起きていた。
「値が LLM コンテキストに載らない操作は思想 1 (うっかり**露出**予防) の射程外」
として allow に倒す。

`_SAFE_READ_FIRST_TOKENS` (0.12.0) との関係:
- SAFE_READ は「residual metachar の ask をスキップする」リスト (cat / grep
  等の内容出力系を含む)。METADATA_ONLY は「operand scan をスキップする」リスト
  (内容を出さないコマンドのみ)。直交する 2 軸で、`ls` 等は両方に属する。
- 判定順序: opaque → residual metachar (非 SAFE_READ のみ) → shell keyword →
  **metadata-only** → operand scan。residual より後段のため、`echo KEY=val >
  .env` (書込み形、echo は SAFE_READ 外) は従来通り residual の ask に倒れ、
  metadata-only では緩まない。

安全 net:
- 内容出力系 (`cat` / `head` / `grep` / `od` 等) と `cp` / `mv` (複製で漏洩面が
  広がる)、`git show` / `git diff` / `git add` は従来通り deny 固定。
- **`find` の内容出力・副作用アクション** (`-exec` / `-execdir` / `-ok` /
  `-okdir` / `-delete` / `-fprint*` / `-fls`) を含む形は metadata-only から
  除外して operand scan → deny。`find -exec cat {} +` は `{}` が hard-stop の
  ため segment 単位で ask に倒れるが、`find -exec cat .env ';'` のように `{}`
  を使わず literal path + クォート `;` で hard-stop を回避する形は
  `_is_metadata_only` の `_FIND_DANGEROUS_ACTIONS` 判定で捕捉する (Codex P1,
  2026-06-12)。`-print` / `-printf` / `-ls` (stdout への metadata 出力) は安全
  なので metadata-only 維持。
- **`file` / `wc` / `du` / `tree` の「ファイル名リスト読込」オプション**
  (`file -f` / `--files-from`、`wc`/`du` の `--files0-from`、`tree --fromfile`
  = `_METADATA_CONTENT_READING_OPTS`) を含む形も metadata-only から除外して
  deny。これらは operand ファイルの **中身** を別パスのリストとして読み、その
  名前 (= 中身) を stdout / エラーに echo するため。`file -f .env` は .env の
  各行を `<行>: cannot open` でエラー出力し実値を漏らす (Codex P2 第2弾,
  2026-06-12)。`file .env` / `wc -l .env` (通常形、型判定・行数のみ) は安全で
  metadata-only 維持。分離形 (`-f .env`) / 値結合形 (`--files0-from=.env` /
  `-f.env`) 両対応。
- `git -C dir check-ignore` のような global option 前置形は保守的に対象外
  (従来通り operand scan → deny)。
- **`git status` は allowlist から除外** — `-v` / `--verbose` が staged 変更の
  diff (機密の旧値/新値) を出力するため (`git status -v -- .env` で実値が漏れる)。
  option-gate するより allowlist から外す方が単純で穴も無い。`check-ignore`
  (gitignore ルール表示) / plain な `ls-files` (名前のみ) は内容を出さないため
  維持。**裸の `git status` は機密 operand が無いため
  operand scan で allow に倒れる** (常用ケース無影響)、`git status [-v] -- .env`
  等 operand 明示形は deny (Codex P1 第2弾, 2026-06-12)。
- **`git ls-files` は object-name 出力オプション付き形を除外** — plain な
  `git ls-files .env` / `--error-unmatch` は名前一覧のみなので metadata-only
  維持。`-s` / `--stage` / `--format` は blob object name (= 内容の安定した
  指紋) を出せるため operand scan → deny に倒す (Codex P2 第3弾, 2026-06-12)。
- **機密 path への redirect 書込み** (`ls > .env` で .env を truncate) は
  metadata-only ∩ safe_read コマンドだと residual metachar 判定を skip して
  shortcut allow に倒れる穴があった (0.14.0 の regression)。`_sensitive_redirect_target`
  で書込み target を抽出し機密なら deny に倒す (Codex P2)。`>` / `>>` / `n>` /
  `&>` の spaced / fused 形に対応。内容露出ではなく破壊的書込みの懸念であり、
  Edit/Write の機密書込み deny と整合させる。`ls -la .env > /tmp/x` (read operand
  のみ機密、書込み先非機密) は allow 維持。

### 対応 (deny/allow 確定できる)

| カテゴリ | 構文 | 使用 parser |
|---|---|---|
| セグメント区切り | `cmd1 && cmd2`, `cmd1 \| cmd2` | `_split_command_on_operators` (0.3.0) |
| operand scan (literal) | `cmd file1 file2`, URI/VCS | shlex token (`_operand_is_sensitive`) |
| operand scan (glob, dotenv stem 一致) | `cat .env*`, `cat *.envrc`, `cat .e[n]v` | shlex token + ``fnmatchcase`` (`_glob_operand_is_dotenv_match`, 0.8.0) |

### 対応外 (opaque `ask_or_allow` 扱い)

| 構文 | 備考 |
|---|---|
| `<` 入力リダイレクト全般 (`cmd < t`, `cmd<t`, `cmd 0<t`, `cmd < "t"` 等) | 0.3.4〜0.6.x で character-level parser による target 抽出 + literal/glob 一致 deny を行っていたが、escape paren depth tracking や `[[ ... ]]` 引数位置判定など敵対的バイパス対策のコード負債が思想 1 (うっかり露出予防、敵対的防御は非目的) と整合しないため 0.7.0 で撤廃。`<` を含む segment は他の hard-stop と同じ ``ask_or_allow``。0.11.0 から segment 単位で再評価するため `cat $X | ls .env | head` のように後段で literal match があれば deny に到達する |
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

### Bash deny の category 別 reason (0.10.0, E3 + E4)

`core/messages.py::bash_deny` を first_token カテゴリ別 dispatch に再編し、
コマンド意図 → 提供する情報・代替案を切り替える (思想 2 を Bash 側でも実装)。

| category | first_token | 返す情報 |
|---|---|---|
| `read_full` | `cat` / `less` / `more` / `bat` / `xxd` / `od` / `hexdump` / `base64` | 「全体閲覧」note + Read 同等 minimal info + Read tool 推奨 |
| `read_partial` | `head` / `tail` | 「先頭/末尾 N 行確認」note + 鍵 list の N 件 (head=先頭、tail=末尾)。`-n N` / `-N` (BSD) / `--lines=N` から N を抽出 |
| `search` | `grep` / `rg` / `ag` / `ack` / `egrep` / `fgrep` | 「検索」note + `matched_pattern_keys: [...]` / `nomatch_pattern_keys: [...]` (E4 で抽出した env-var 名と dotenv の照合結果)。pattern 抽出 / 照合とも失敗時は全鍵 list (minimal info) に降りる |
| `mutate` | `awk` / `sed` | 「加工」note + minimal info + patch / diff 適用推奨 |
| `load` | `source` / `.` | 「shell load」note + minimal info + direnv (.envrc) / dotenv-cli / 1Password CLI 推奨 |
| `move` | `cp` / `mv` | 「コピー / 移動」note + 1Password CLI / pass / git-secret + .env.example 派生推奨 |
| `history` | `git` (subcommand `show` / `diff` / `log` で `.env` を参照したケース) | 「commit / 差分閲覧」note + 「tracked なら漏洩済みの可能性」+ `git rm --cached <basename>` + rotate 推奨。VCS pathspec の `:` 後尾から basename 抽出 |
| `transfer` | `curl` / `wget` / `scp` / `rsync` | 「転送」note + Vault / SOPS / 1Password CLI 推奨 |
| `archive` | `tar` / `zip` / `gzip` | 「アーカイブ」note + `--exclude=<basename>` / `-x <basename>` 推奨 |
| `generic` | 上記以外 | 0.7.0〜0.9.0 と同等の note + minimal info (新規) |

**file_render の流れ** (`redaction/file_render.py::render_for_bash`):

1. `normalize(operand, cwd)` で path を解決 (失敗 → `(None, None)`)
2. `classify(path)` で regular ファイルか確認 (非 regular → `(None, None)`、
   `OSError` / `ValueError` で lstat 失敗 → `(None, None)` で握り潰し)
3. `open_regular(path)` で fd と size を取得 (`O_NOFOLLOW`)
4. format 判定 (`_detect_format`):
   - dotenv → `redact_dotenv` で info dict を取得 → `format_dotenv` で body
     文字列 → `build_reason` で `<DATA untrusted>` 包装 → (reason, info) を返す
   - dotenv 以外 (json / toml / yaml / opaque / 32KB 超) → `engine.redact` /
     `redact_large_file` で reason を取得 → (reason, None) を返す
5. 内部例外は握り潰し `(None, None)` (Bash 側 deny は generic reason に降りる)

**E4 の grep extraction** (`handlers/bash/grep_extract.py::extract_grep_keys`):

- 抽出対象: env-var 形式 (`[A-Z][A-Z0-9_]{2,}`) を `re.finditer` で全 token から
  拾う
- `-e PATTERN` / `-E PATTERN` / `-G PATTERN` (次 token consume)、`--regex=...` /
  `--pattern=...` / `-e=...` (RHS) に対応
- `--` 以降は positional 扱いで pattern 抽出停止、short option (`-i` 等) は skip
- `|` 分割は `re.finditer` の境界処理で自然に処理 (`A_KEY|B_KEY` から両方抽出)
- 出現順 dedup された list[str] を返す

**`bash_deny` シグネチャ** (positional 互換維持):

```python
def bash_deny(
    first_token: str,
    operand: str,
    *,
    command: str = "",          # head/tail の -n N 抽出に使う
    file_render: str = "",      # render_for_bash の 1 番目の戻り値
    dotenv_info: dict | None = None,  # render_for_bash の 2 番目の戻り値
    grep_keys: list[str] | None = None,  # extract_grep_keys の戻り値
) -> str: ...
```

旧 0.7.0〜0.9.0 の `bash_deny(first_token, operand)` 呼び出しは generic
builder で 0.9.0 とほぼ同等の出力を生成するため互換維持。

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

## 既知制限 (0.14.0 時点)

1. **MCP 経路は対象外** — MCP server 経由のファイルアクセスは hook が介在しない
2. **Bash 間接アクセス (静的解析不能)** — `bash -c`, `eval`, `python3 -c`, `sudo`,
   `awk`, `sed`, `xargs`, heredoc, process substitution, `/bin/cat`, `./script`
   などは静的解析できず、default モードでは ask、auto/bypass モードでは
   **allow** に倒す。0.8.0 で `FOO=1 cat .env`, `env cat .env`,
   `command cat .env`, `nohup cat .env`, `/usr/bin/env FOO=1 cat .env` も
   ``ask_or_allow`` に格下げした (0.3.2〜0.7.x の prefix normalize は撤廃)。
3. **`<` 入力リダイレクトは ask_or_allow 扱い (0.7.0、0.11.0 で segment 単位
   再評価)** — 0.3.4〜0.6.x では character-level quote-aware parser で
   `cat < .env` / `cat<.env` / `cat 0<.env` / `cat < ".env"` などから target
   を抽出し literal/glob 一致で deny に倒していたが、`cat <(echo \(\)) < .env`
   の escape paren depth tracking や `[[ ... ]]` 引数位置判定など敵対的バイパス
   対策のコードが思想 1 (うっかり露出予防が目的、敵対的防御は非目的) に反する
   ため 0.7.0 で撤廃。`<` を含む segment は他の hard-stop と同じ
   ``ask_or_allow`` (default で ask、autonomous で allow) に倒す。0.11.0 で
   segment 単位再評価に細粒度化したため、`<` が含まれる segment は当該 segment
   のみ ask に倒り、他 segment が literal match すれば deny に到達する
   (`cat $X | ls .env | head` 等)。攻撃シナリオ `cat <(echo \(\)) < .env` は
   全 segment が hard-stop となるため挙動不変。
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
14. **Bash redirect 書込み (`echo KEY=val > .env` / heredoc `cat > .env <<EOF`)
    は受容** — residual metachar / hard-stop 経由の `ask_or_allow` に倒るため、
    autonomous モードでは allow で通る。Edit/Write tool 経路の deny 固定とは
    非対称だが、**ユーザー確認 (2026-06-12) で「受容」に確定**: 本 plugin の
    主目的は悪意のないうっかり露出の予防であり、セキュリティを担保する
    plugin ではない。redirect / heredoc で機密 path に書き込む形はうっかりの
    範疇を超えるため対象外として通す (「うっかり予防のついでに少し守れれば
    十分」の思想)。ただし **metadata-only ∩ safe_read コマンド**
    (`ls` / `stat` / `wc` 等) の `ls > .env` 形だけは 0.14.0 で metadata-only
    shortcut を入れた結果 regression したため、`_sensitive_redirect_target` で
    deny を復活させている (Codex P2)
15. **`>|` clobber override redirect は未対応** — `tree >| .env` の `>|` は
    `|` が segment 分割で pipe として割られ `tree >` と `.env` に分離するため、
    機密 redirect target を検出できず allow に倒る。`>|` を意図的に書くのは
    `noclobber` を理解した上級者で「うっかり」ではない (思想 1 射程外) ため
    既知限界とする。`>` / `>>` / `n>` / `&>` の通常 redirect は検出する

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
