# sensitive-files-guard 設計詳細 (DESIGN.md)

利用者向けの要約は [README.md](../README.md)、保守者向けの実務ガイドは
[CLAUDE.md](../CLAUDE.md)、判定結果の完全マトリクスは [MATRIX.md](./MATRIX.md)、
パターン設定の詳細は [PATTERNS.md](./PATTERNS.md) を参照。

本ドキュメントは「**なぜこの設計にしたか**」の根拠と実測ログを集約する。

## 設計原則

1. **Fail-closed in doubt** — read 側の内部失敗は `ask` (bypass モード時は `deny`)
   にフォールバック。Stop 側は応答停止を招かないため fail-open (stderr warning +
   空出力)。
2. **情報量最小化** — minimal info (鍵名・順序・型・件数) のみ返却、値は
   bool/小整数含めて原則マスク。
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

### 2026-04-22 — plan mode での hook 発火有無 (0.3.3)

`hooks/_debug/capture_envelope.py` (一時スクリプト) で実測。
現行 CLI (2.1.101 系) では **plan mode で PreToolUse hook が発火しない** 観測
(= Case C)。したがって `LENIENT_MODES` に `"plan"` を加えた 0.3.3 の変更は
**現行 CLI では dead entry** となる。

ただしこの変更には 2 つの意味がある:

1. **前方互換**: 将来 CLI が plan mode でも hook を発火させるよう変わったとき、
   再リリース不要で正しい挙動 (= Bash opaque ケースを allow に倒す) に収束する
2. **意思表明**: plan mode は「tool 実行に至らない setup フェーズ」であり、
   本来 Bash opaque ケースで止める必要がない。設計意図として `LENIENT_MODES` に
   列挙することで「なぜ含めているか」が明文化される

CLI バージョンアップ時の再実測手順は [CLAUDE.md](../CLAUDE.md) の "CLI バージョン
アップ時の再実測手順" セクションを参照。

## LENIENT_MODES 方針

`core/output.py::ask_or_allow` は bash handler の静的解析不能ケースで使う三態
判定。`permission_mode` が `LENIENT_MODES` に含まれれば allow に、そうでなければ
ask に倒す。

| mode | `ask_or_allow` | 理由 |
|---|---|---|
| `default` | ask | 明示的にユーザー介在を期待 |
| `plan` | **allow** (0.3.3 追加) | plan 中は tool 実行に至らない / Case C の前方互換層 |
| `acceptEdits` | ask | Edit/Write 専用モード。Bash lenient の意図なし |
| `auto` | allow | CLI 前段 classifier モード、autonomous 実行意図 |
| `dontAsk` | ask | 明示的な非 lenient 判断として既存方針維持 |
| `bypassPermissions` | allow | 全確認スキップモード、autonomous 実行意図 |

Read/Edit handler の `ask_or_deny` は別 frozenset で `bypassPermissions` のみ
deny に倒す (機密可能性があるものは ask 維持で bypass だけ deny する)。

### Phase 0 Case C での留意

前述のとおり `"plan"` エントリは現行 CLI では dead。unit test では
`ask_or_allow({"permission_mode": "plan"}, ...)` が確実に `{}` (allow) を返す
ことのみを確認する (integration 実態は docs に注記で補う方針)。

## Bash handler 判定フロー

0.3.2 で確定した三態判定は 0.3.3 でも挙動変更なし。詳細な mermaid フローは
[CLAUDE.md](../CLAUDE.md) 側に集約。コマンド別の deny/allow/ask 一覧は
[MATRIX.md](./MATRIX.md) を参照。

### 責務境界 (0.3.3 再設計)

0.3.3 では `bash_handler.py` が 662 行に肥大化していたのを、責務境界で以下のよう
に分割した:

| モジュール | 責務 | 依存先 |
|---|---|---|
| `bash_handler.py` | orchestration + plugin ステート依存 + test seam | `core.*`, `handlers/bash/*` |
| `handlers/bash/constants.py` | compile-time 定数 (regex / frozenset) | なし |
| `handlers/bash/segmentation.py` | quote-aware セグメント分割 / hard-stop 検出 | `constants` |
| `handlers/bash/operand_lexer.py` | glob 判定 / literalize / path 候補抽出 | `constants` |
| `handlers/bash/redirects.py` | 安全リダイレクト剥離 / 残留 metachar 判定 | `constants` |

`handlers/bash/` 配下のモジュールは **副作用なし・plugin ステート非依存**
(SFG_CASE_SENSITIVE 環境変数の参照のみ、`is_sensitive` 側と整合)。

テストは `handlers.bash_handler.X` の形で patch / import するため、
`bash_handler.py` は以下の symbol を **再 export** して従来の import path を
維持する:

- `handle` (orchestration)
- `_normalize_segment_prefix` (patch seam)
- `_operand_is_sensitive` / `_glob_operand_is_sensitive` (plugin ステート依存)
- `_extract_input_redirect_targets` (patch seam)
- `_literalize` / `_glob_candidates` (pure だが test_glob_candidates.py が直接 import)
- `load_patterns` (test_failclosed.py の `mock.patch` 対象)
- 各定数 (test が直接参照する可能性に備えて)

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

### Bash handler (三態判定)

コマンド別の deny / allow / ask は [MATRIX.md](./MATRIX.md) を参照。mermaid
フロー図は CLAUDE.md 側にある。

**unified operand scan**: 全セグメントで非 option トークンを一律
`_operand_is_sensitive` / `_glob_operand_is_sensitive` に通す。コロンを含む
operand (`HEAD:.env`, `user@host:/p/.env`) はコロン分割後の各片の basename も判定。
コマンドが実際に file を読むかどうかは静的に判別しないため false positive
(`echo .env`, `ls .env`, `mkdir .env`) が出るが、`patterns.local.txt` の
`!<basename>` exclude で個別対処できる。

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
  `tool_input` からキー名抽出 (Edit=new_string / Write=content /
  MultiEdit=edits[].new_string 連結)
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

## 既知制限 (0.3.3 時点)

1. **MCP 経路は対象外** — MCP server 経由のファイルアクセスは hook が介在しない
2. **Bash 間接アクセス (静的解析不能)** — `bash -c`, `eval`, `python3 -c`, `sudo`,
   `awk`, `sed`, `xargs`, heredoc, process substitution, `/bin/cat`, `./script`
   などは静的解析できず、default モードでは ask、auto/bypass/plan モードでは
   **allow** に倒す。0.3.2 で前置き正規化が入ったため `FOO=1 cat .env`,
   `env cat .env`, `command cat .env`, `nohup cat .env`,
   `/usr/bin/env FOO=1 cat .env` は確定 match で deny に確定する。`< .env` 形式も
   target 抽出により deny に確定する。
3. **`<` 入力リダイレクト target 抽出の限界** — 単純 regex
   (`(?:^|[^<&0-9])<\s+(\S+)`) による抽出のため、quote を厳密処理しない。
   `cat < "a file.env"` のような quoted space 名は false negative (target 抽出に
   失敗 → opaque ask_or_allow)。`cat<.env` (空白なし) も regex 仕様により拾わない
   (これらは後段の `ask_or_allow` に倒るだけで false-deny は出ない方向の限界)。
4. **autonomous / planning モードでの opaque 緩和** — `bash -c 'cat .env'` の
   ような shell wrapper 内に機密 path があっても auto/bypass/plan では allow に
   倒る。wrapper 内部の script を解析しないため検出できない。autonomous モード
   を選んだユーザーが「日常コマンドを止めない」意図と平等な扱いとしての設計上の
   トレードオフ。完全防御を求める場合は default モードで運用する。
5. **`__main__.py` catch-all は 0.3.3 でも未緩和** — bash handler 内部で未捕捉
   例外が起きた場合、`__main__` 側の catch-all は従来通り `ask_or_deny`
   (auto=ask / bypass=deny)。tool 種別だけで一律 lenient にすると fail-closed
   境界が粗くなるため、0.3.4 以降で「特定の意図された例外クラスのみ allow 緩和」
   として再設計予定。
6. **親ディレクトリ差し替え race** — `O_NOFOLLOW` は最終要素のみ保護し、
   途中要素の symlink 差し替え race は対象外 (原理的に完全防御不能)
7. **TOCTOU 完全排除は非目的** — hook 読取と Claude 実 Read/Write の分離は範囲外
8. **`<DATA untrusted>` モデル解釈保証なし** — 包装 + sanitize + DATA タグ
   エスケープで多段防御するが、モデルが敵対的文脈として扱う保証は無い
9. **Windows は fail-closed で deny exit** — SIGALRM 非対応のため hook 冒頭で
   deny exit する (Step 0-c 実測結果確定前の暫定方針)
10. **submodule 内 untracked は非対象** — `git ls-files --recurse-submodules` は
    tracked のみ。untracked を submodule 内まで拾う git native オプションは無い
11. **Git バージョン依存** — `--recurse-submodules` は git 1.7+ が必要
12. **`!` プレフィックス (Claude Code bash mode) は防御対象外** — ユーザーが
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

## `_glob_candidates` の設計判断 (0.3.2 以降維持)

プランの初期案には (op_stem + pt_stem) / (pt_stem + op_stem) の **連結候補** を
加える項目もあったが、`*.log` に対して `.env` rule との連結 `.env.log` が候補化
され、`is_sensitive(".env.log")` が `.env.*` rule で True になる結果
`cat *.log` が deny されてしまう問題があった。usability 上 `*.log` は allow して
おきたいので、連結候補は採用しない (`cred*.json` `id_*` `*.envrc` 等の交差は
rule pt_stem の direct match だけで網羅できる)。

## Step 0-c 実測 (将来更新予定)

プラン v3 の Step 0-c (outer timeout 発火時の Claude 挙動実測) は未実施。
暫定方針として Case A (timeout kill → allow/fail-open の最悪ケース想定) で
Windows (SIGALRM 非対応) を hook 冒頭で deny exit にしている。

実測手順は [CLAUDE.md](../CLAUDE.md) の "Step 0-c 実測結果" セクション参照。
