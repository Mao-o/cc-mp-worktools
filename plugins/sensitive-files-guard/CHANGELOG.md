# Changelog

## 0.3.4

**shim 削除 + Bash input redirect 解析を自前 parser に刷新**。0.3.3 で予告して
いた `core/matcher.py` shim の削除と、`<` 入力リダイレクト target 抽出を
**有効な bash syntax を網羅**する形に再設計。挙動変更は「0.3.3 で
`ask_or_allow` (auto/bypass allow) だったケースが機密一致時に deny に確定する」
方向のみで、新たに allow に倒るケースは無い。

### 主要な変更

1. **`core/matcher.py` shim 削除** — `hooks/redact-sensitive-reads/core/matcher.py`
   (1 行の re-export 層) を削除。`handlers/read_handler.py` / `handlers/bash_handler.py`
   / `handlers/edit_handler.py` / `tests/test_matcher.py` の import を
   `_shared.matcher` 直参照に書換。`check-sensitive-files/checker.py` は既に
   `_shared.matcher` 直参照で統一されており、redact 側だけ残っていた非対称を解消。
   `tests/test_shared_import.py` から shim 契約テスト
   (`test_core_matcher_reexports_shared`) を削除。公開 API (hook stdin/stdout
   envelope) 変更なし。
2. **Bash input redirect 解析を character-level parser に刷新** — 0.3.2 の
   regex (`(?:^|[^<&0-9])<\s+(\S+)`) は空白あり + 非 quote + fd 前置きなしの
   形式しか拾えず、`cat<.env` / `cat 0< .env` / `cat N<target` / `cat < ".env"`
   等が false-negative で `ask_or_allow` に倒っていた。検討した `shlex.split`
   ベースは `<` を演算子として分割しないため B スコープ (全有効 redirect 構文)
   を達成できず、**character-level quote-aware parser** を新設
   (`handlers/bash/redirects.py::_scan_input_redirect_targets_chars`)。
   - 対応: `<` / `<target` / `N<` / `N<target` / quote 付き (`< "t"`, `<'t'`)
     / 複数 (`cmd1 && cmd2`)
   - 除外: `<<` heredoc, `<<<` herestring, `<&N`/`<&-` fd dup, `<(...)` process sub
     (後者は depth tracking で閉じ `)` までスキップし内部の `<` を拾わない)
   - `<` を含むのに target が取れなかった場合は
     `bash_classify:input_redirect_empty_extract` ログで観測可能
3. **DESIGN.md に "Bash handler の対応文法範囲" 節を新設** — character-level
   parser と shlex-based segment 解析の使い分け、対応/対応外の境界、観測ログ
   tag 一覧を明文化 (Codex review 指摘 3/5 対応)。
4. **character-level parser を word 概念ベースに強化 (Codex PR review R1/R2/R3)**
   — PR レビューで 3 件の指摘を受けて修正:
   - **R1 (P2)**: `_consume_redirect_target` が closing quote で即 return して
     いたため、``cat < ".env".example`` の suffix ``.example`` を落として
     ``.env`` だけを抽出していた (挙動リグレッション)。POSIX sh の word 概念に
     従い、word boundary (quote 外の whitespace / operator) まで quote / bare /
     backslash を mix して読み続ける形に変更。
   - **R2 (P3)**: scanner が unquoted shell comment (`# ...` ) 内の `<` を
     拾ってしまう false-positive を塞いだ。`#` が word start 位置 (先頭 / 空白 /
     operator 直後) にある場合のみ行末まで skip する (Bash 仕様通り)。
     word 内部の `#` (例: ``abc#def``) や quote 内の `#` はコメント扱いしない。
   - **R3 (P1, security)**: process sub `<(...)` の depth tracking が escape
     された `\(` `\)` を通常括弧として数えていた。例: ``cat <(echo \\() < .env``
     で escape された `\(` が depth を増やし続け、`)` で 0 に戻らず後続の
     ``< .env`` を取りこぼし、auto/plan モードで `ask_or_allow` → allow に倒って
     **機密 bypass** を許す regression。修正: depth scan 内でも quote 外
     backslash escape を尊重し、escape された `(` `)` を depth 計算から除外する。

### 挙動変更 (0.3.3 → 0.3.4)

| コマンド | 0.3.3 | 0.3.4 |
|---|---|---|
| `cat<.env` (空白なし) | ask_or_allow | **deny** |
| `cat<".env"` (inline quoted) | ask_or_allow | **deny** |
| `cat 0< .env` (fd 前置き + 空白) | ask_or_allow | **deny** |
| `cat 0<.env`, `cat N<target` (fd 前置き inline / 任意 fd) | ask_or_allow | **deny** |
| `cat < ".env"`, `cat < '.env'` (quote + 空白) | ask_or_allow | **deny** |
| `cat < ".env.local"`, `cat < ".env*"` (quoted glob) | ask_or_allow | **deny** |
| `cat < "a file.env"` (rule 非 match の quoted space 名) | ask_or_allow | ask_or_allow (抽出成功、rule 非 match で維持) |
| `cat < ".env".example` (連結 word, exclude 決着, R1 fix) | ask_or_allow | ask_or_allow (target `.env.example` で exclude 決着) |
| `cat < ".env".local` (連結 word, R1 fix) | ask_or_allow | **deny** (target `.env.local` で rule 一致) |
| `echo ok #cat<.env` (シェルコメント内, R2 fix) | ask_or_allow | ask_or_allow (comment skip で target 空) |
| `cat <(echo \(\)) < .env` (process sub 内 escape paren, R3 fix) | auto/plan で **bypass** (security regression) | **deny** (depth tracking 修正で target 抽出成功) |
| `cat <<EOF`, `cat <<< '.env'`, `cat <&2`, `cat <(cat .env)` | 変更なし | 変更なし (opaque 維持) |
| 既存 `cat < .env` / `cat < .env*` 等 | deny | deny (維持) |

### 内部構造の変更

- `handlers/bash/redirects.py` に `_scan_input_redirect_targets_chars` /
  `_consume_redirect_target` を新設 (pure helper)
- `_extract_input_redirect_targets` は `handlers/bash_handler.py` 内で
  character-level parser を呼ぶ thin wrapper (patch seam 維持)
- `handlers/bash/constants.py::_INPUT_REDIRECT_RE` を削除 (fallback 不要)
- `core/matcher.py` 削除 (redact-sensitive-reads のみ、_shared は不変)
- `_scan_input_redirects` に `input_redirect_empty_extract` 観測ログ追加
- テスト件数: 411 → 468 件 (+58 追加, -1 削除, 2 件は挙動変更に伴う書換)。
  内訳: inline/fd 10 + quote 6 + exclusion 7 + handle 11 + R1 concat word 6 +
  R2 comment 7 + handle R1/R2 4 + R3 escape paren 5 + handle R3 2
- 公開 API / patch seam / LENIENT_MODES 不変

### 既知の未対応 (0.3.5 以降に分離)

- 例外クラス単位での `__main__` catch-all 緩和 (旧 H1。bash_handler 内で
  raise する意味論的経路が実在しないことが判明したため設計練り直し)
- shell wrapper (`bash -c "cat .env"` 等) 内部 script 解析
- Windows (Step 0-c) 実測方針確定
- Safe-search ラッパスクリプト (`scripts/safe_grep.py` / `safe_find.py`)

## 0.3.3

**ブラッシュアップ + plan mode lenient 化**。0.3.2 の誤爆ガード緩和 (三態判定 +
前置き正規化 + glob 候補列挙) の挙動は維持したまま、plan mode への対応、
`bash_handler.py` の責務境界分解、ドキュメント再編を実施。

### 主要な変更

1. **plan mode を `LENIENT_MODES` に追加** — `core/output.py::ask_or_allow` は
   `"auto"` / `"bypassPermissions"` / **`"plan"`** の 3 つで allow に倒す。Bash
   handler の静的解析不能ケース (opaque wrapper / hard-stop metachar / shell
   keyword / 任意 path exec / 残留 metachar / shlex 失敗) が plan mode でも素通り
   する。`acceptEdits` / `dontAsk` は意図的に非 lenient を維持 (ask)。
2. **`bash_handler.py` を責務境界で分解** — 662 行の肥大化を解消。pure helper と
   compile-time 定数を `handlers/bash/` サブパッケージに切り出し:
   - `handlers/bash/constants.py` — regex / frozenset (hard-stop, opaque
     wrappers, shell keywords 等)
   - `handlers/bash/segmentation.py` — quote-aware セグメント分割 / hard-stop 検出
   - `handlers/bash/operand_lexer.py` — glob 判定 / literalize / path 候補抽出
   - `handlers/bash/redirects.py` — 安全リダイレクト剥離 / 残留 metachar 判定
   - `bash_handler.py` に残したのは orchestration + plugin ステート依存
     (`is_sensitive` / `load_patterns` / envelope 操作) + test seam 用の
     再 export のみ。**既存テスト 409 件は書き換えなしで pass** (patch seam
     維持)。
3. **LENIENT_MODES 回帰検知 assert 追加** — `tests/test_envelope_shapes.py` に
   `TestLenientModesSubset` を新設。`LENIENT_MODES` が `_KNOWN_PERMISSION_MODES`
   (default / plan / acceptEdits / auto / dontAsk / bypassPermissions) の subset
   であることを確認する。CLI が新 mode を追加したら red になって気付ける構造。
4. **ドキュメント再編** — README / CLAUDE.md の肥大化を解消:
   - `docs/DESIGN.md` — 設計原則 / Phase 0 実測 / 既知制限 / 責務境界
   - `docs/MATRIX.md` — 判定結果の完全マトリクス (6 permission_mode 列)
   - `docs/PATTERNS.md` — パターン仕様・設定例・`_detect_format` 同期チェック
   - `README.md` を 650 → 約 200 行に縮小 (リリースノートを本 CHANGELOG へ統合)
   - `CLAUDE.md` を 558 → 約 330 行に縮小、**CLI バージョンアップ時の再実測
     Runbook** を新設
5. **Phase 0 実測 (2026-04-22)** — plan mode での Bash hook 発火有無を実測。
   現行 CLI (2.1.101 系) では **Case C** (plan mode で hook 非発火) を観測。
   本リリースの `"plan"` 追加は **将来 CLI 変更への前方互換層** として機能する
   (hook が発火する CLI では正しく allow に、発火しない CLI では dead entry
   として無害)。詳細は `docs/DESIGN.md` 参照。

### 挙動変更 (0.3.2 → 0.3.3)

| ケース | 0.3.2 | 0.3.3 |
|---|---|---|
| Bash opaque wrapper / hard-stop / shell keyword 等 in plan mode | ask (現行 CLI では hook 非発火なので UI 反映なし) | **allow** (ask_or_allow) (将来 CLI 変更時) |
| その他の permission_mode (default / auto / bypass / acceptEdits / dontAsk) | 変更なし | 変更なし |
| 確定 match の deny / patterns.txt 読込失敗の deny | 変更なし | 変更なし |

### 内部構造の変更

- `handlers/bash/` サブパッケージ新設
- `bash_handler.py`: 662 行 → 415 行
- テスト件数: 409 → 411 件 (LENIENT_MODES 回帰検知 2 件追加)
- 公開 API / patch seam は完全維持 (テスト書換なし)

### 既知の未対応 (0.3.4 以降に分離)

0.3.2 の CHANGELOG で予告していた **`__main__.py` catch-all の `ask_or_allow`
緩和** は 0.3.3 では実施しない。`args.tool == "bash"` で一律緩和する設計は
「bash_handler 内のどの段階で壊れても同じ扱い」となり、fail-closed 境界を
tool 種別だけで決める形になる。0.3.4 以降で「`bash_handler` が opaque 扱いを
確定させた後に意図的に raise する特定例外クラスのみ `ask_or_allow`」という
粒度で再設計する。

その他の 0.3.4 以降予定項目:
- 例外クラス単位での `__main__` catch-all 緩和 (上記)
- shell wrapper (`bash -c "cat .env"` 等) 内部 script 解析
- `<` 入力リダイレクト target の quote-aware 抽出
- `redact/core/matcher.py` 互換 re-export 層の削除
- Windows (Step 0-c) 実測方針確定
- **Safe-search ラッパスクリプト** (`scripts/safe_grep.py` / `safe_find.py`) —
  `grep *.json` のような日常検索が機密 glob 交差 (`credentials.json` との
  fnmatch) で deny される体験を改善するため、plugin 同梱の薄いラッパで
  機密ヒットを自動除外する案

## 0.3.2

Bash handler を **誤爆ガード緩和** 方向に再設計。autonomous 実行モード
(`--permission-mode auto` / `bypassPermissions`) を選んだユーザーが日常コマンドで
意図せず止められる問題を解消する。

### 主要な変更

1. **三態判定 `ask_or_allow` を新設** — 静的解析不能ケースを default=ask /
   auto/bypass=allow に切り替える。対象:
   - hard-stop metachar (`$`, バッククォート, `(`, `)`, `{`, `}`, `<<`, `<(`, fd-dup)
   - shell wrapper / インタプリタ (`bash -c`, `eval`, `python3 -c`, `sudo`,
     `awk`, `sed`, `xargs`, `parallel`, `time`, `!`, `exec` 等)
   - shell keyword / 制御構文 (`if`, `for`, `while`, `do`, `coproc` 等)
   - basename が透過対象でない絶対/相対パス実行 (`/bin/cat`, `./script`)
   - 残留 metachar (非安全リダイレクト `> out.txt`, quoted `'&2'` 等)
   - shlex.split / normalize 失敗
   - `env -i` / `env -u` / `env --` / `command -p` / `command --` 等のオプション付き
2. **前置き正規化を新設** — 以下の prefix は剥がして再判定し、確定 match なら
   全 mode で `deny` 固定:
   - 環境変数 prefix (`FOO=1`)
   - `env [ASSIGNMENTS...]` (option 無しのみ)
   - `command` / `builtin` / `nohup` (option 無しのみ)
   - 連鎖 (`nohup command cat .env`, `command env FOO=1 cat .env`)
   - 絶対/相対パスでも basename が `env` / `command` / `builtin` / `nohup` のもの
     (`/usr/bin/env FOO=1 cat .env`, `/bin/command cat .env`)
3. **glob 候補列挙 `_glob_operand_is_sensitive` を新設** — operand が glob
   (`*` `?` `[`) を含む場合、(a) operand 自身の literal stem と (b) 既定 rules の
   pt_stem で operand glob に fnmatch する候補を生成し、`is_sensitive` で 1 つでも
   True なら **deny 固定**。include/exclude の last-match-wins は既存ロジックで整合。
   - deny 例: `cat .env*`, `cat .env.*`, `cat *.envrc`, `cat id_rsa*`, `cat id_*`,
     `cat *.key`, `cat cred*.json`, `cat .e[n]v`, `cat [.]env`, `cat .en?`
   - allow 例: `cat *.log`, `cat .env.example*` (全候補が exclude 決着)
4. **`<` 入力リダイレクトの target 抽出** — hard-stop に該当する `< file` 形式は
   target を regex 抽出して先に operand scan に流す。target が機密一致なら全 mode
   で deny。heredoc (`<<EOF`), process substitution (`<(...)`), fd dup (`<&N`),
   数値 fd 前置 (`0<`) は regex で除外され opaque (`ask_or_allow`) に倒る。
5. **`time` / `!` / `exec` を opaque へ移動** — `_SHELL_KEYWORDS` から
   `_OPAQUE_WRAPPERS` に移動 (shell 文法要素 / プロセス置換挙動として統一)。
6. **`patterns.txt` 読込失敗 = bash handler は全 mode で `make_deny` 固定** —
   policy 欠如時に lenient で素通りすることを避ける。Read/Edit handler は
   `ask_or_deny` のまま (regression guard あり)。

### 挙動変更 (0.3.1 → 0.3.2)

| コマンド | mode | 0.3.1 | 0.3.2 |
|---|---|---|---|
| `cat .env*` | 全 mode | ask_or_deny | **deny** (glob 候補列挙) |
| `cat .e[n]v` | 全 mode | ask_or_deny | **deny** (glob 候補列挙) |
| `cat *.log` | 全 mode | ask_or_deny | **allow** (rules と非交差) |
| `FOO=1 cat .env` | 全 mode | ask_or_deny | **deny** (前置き剥がし) |
| `env cat .env` | 全 mode | ask_or_deny | **deny** (前置き剥がし) |
| `command cat .env` | 全 mode | ask_or_deny | **deny** (前置き剥がし) |
| `nohup cat .env` | 全 mode | deny (operand match) | deny (前置き剥がしで同じ結果) |
| `/usr/bin/env FOO=1 cat .env` | 全 mode | ask_or_deny | **deny** (basename=env 透過) |
| `cat < .env` | 全 mode | ask_or_deny | **deny** (target 抽出) |
| `bash -c 'date'` | default | ask | ask |
| `bash -c 'date'` | auto/bypass | deny (旧 ask が bypass で deny) | **allow** |
| `if true; then echo x; fi` | auto/bypass | deny (shell keyword fail-closed) | **allow** |
| `/bin/cat README.md` | auto/bypass | deny (path exec fail-closed) | **allow** |
| `echo foo > out.txt` | auto/bypass | deny (residual metachar) | **allow** |
| `cat .env` 等の確定 match | 全 mode | deny | deny (維持) |
| `patterns.txt 読込失敗` (bash) | default/auto | ask | **deny** |
| `patterns.txt 読込失敗` (bash) | bypass | deny | deny (維持) |

### 既知制限の追記

- `<` 入力リダイレクトの target 抽出は単純 regex。quote を厳密処理しないため
  `cat < "a file.env"` のような quoted space 名は false negative に倒る場合あり
- `_glob_candidates` は (op_stem + pt_stem) / (pt_stem + op_stem) の連結候補は
  **採用しない** (採用すると `cat *.log` が `.env.log` 候補で false-deny になるため)。
  代わりに「rule 自体が operand glob と直接 fnmatch する」場合のみ候補化する

## 0.3.1

0.3.0 の bash_handler を **unified operand scan** に再設計し、未知コマンド /
wrapper / VCS pathspec / 制御構文セグメント経由の機密 path bypass を塞ぐ。
Codex 自動レビューで累計 4 件の P1 指摘を受けた内容を一括解決。

### 主要な変更

1. **unified operand scan** — コマンド名ベースの allow-list (`_SAFE_READ_CMDS`)
   を廃止。全セグメントの非 option トークンを機密判定し、一致すれば **deny 固定**。
   未知コマンド + 機密 operand の bypass (`grep SECRET .env`, `base64 .env`,
   `timeout cat .env`, `busybox cat .env`) を全て塞ぐ。非機密 operand なら
   未知コマンドでも allow を維持 (`grep foo README.md`, `npm test` 等)。
2. **VCS pathspec / URI 対応** — `git show HEAD:.env`, `curl file://.env`,
   `user@host:/etc/.env` のようにコロンを含む operand は分割して各片の basename
   も機密判定する。
3. **glob operand → fail-closed** — `cat .env*`, `grep SECRET .e[n]v` のように
   `*` `?` `[` を含む operand は shell 展開結果を静的に追えないため **ask**。
4. **予約語追加** — `_SHELL_KEYWORDS` に `coproc` を追加 (0.3.0 で漏れていた)。
5. **escape 数え直し** — `_split_command_on_operators` のダブルクォート閉じ
   判定を「連続バックスラッシュの偶奇」でやり直し。`echo "\\"; cat .env` のような
   偶数バックスラッシュ run で閉じクォートを見落とす bypass を塞いだ (0.3.0.1
   に相当)。

### 挙動変更 (0.3.0 → 0.3.1)

| コマンド | 0.3.0 | 0.3.1 |
|---|---|---|
| `grep SECRET .env` | allow | **deny** |
| `base64 .env` / `xxd .env` / `hexdump .env` / `od .env` | allow | **deny** |
| `timeout 1 cat .env` / `nohup cat .env` / `busybox cat .env` | allow | **deny** |
| `git show HEAD:.env` / `curl file://.env` | allow | **deny** |
| `cp .env /tmp` / `mv .env backup` | allow | **deny** |
| `coproc cat .env` | allow | **ask_or_deny** |
| `cat .env*` / `cat [.]env` / `cat *.log` | allow | **ask_or_deny** |
| `echo "\\"; cat .env` (偶数 backslash) | allow | **deny** |
| `grep foo README.md` (非機密) | allow | allow (維持) |
| `npm test`, `date`, `pwd` | allow | allow (維持) |
| `git status && git log 2>/dev/null \|\| true` | allow | allow (維持) |

### 既知の false positive (0.3.1)

unified operand scan は「コマンドが実際に file を読むか」を判別しない。以下は
**deny になるが実際には値漏れしない** ケース:

- `echo .env` (文字列 `.env` を stdout に表示するだけ)
- `ls .env` (ファイル名メタデータを表示するだけ)
- `mkdir .env` (ディレクトリ作成のみ)
- `touch .env.new` (空ファイル作成のみ)

これらは hook の対象外にしたい場合、`patterns.local.txt` に `!.env.new` のような
明示 exclude を追加するか、引数をリテラル含まない形に書き換える運用になる。

## 0.3.0

Bash handler の静的解析を拡張。0.2.0 からの breaking change は「同じコマンドで
ask だったものが deny / allow に確定する」方向のみで、値が新たに LLM に露出する
方向の緩和は無い。

### 主要な変更

1. **セグメント分割** — `&&` `||` `;` `|` `\n` を quote-aware に分割して各
   セグメントを独立判定。`git status && git log || true` のような日常複合コマンドが
   allow 可能に。ただし任意のセグメントに機密 path が現れれば **deny 固定**
   (0.2.0 の「単一コマンド時の deny」と整合)。
2. **安全リダイレクト剥離** — `>/dev/null` / `1>/dev/null` / `2>/dev/null`
   / `&>/dev/null` / `>/dev/stderr` / `>/dev/stdout` / `2>&1` / `>&2` 等を
   トークン列から除外してから判定。`cat README.md 2>/dev/null` のような
   単なる stderr 黙殺が allow に。`> out.txt` のような通常ファイルへの
   リダイレクトは剥がさず **ask_or_deny**。
3. **hard-stop の縮小** — `$` `` ` `` `<` `(` `)` `{` `}` `\r` のみ hard-stop。
   `&` `|` `;` `>` `\n` はセグメント分割 / リダイレクト剥離で扱われる。
4. **クォート尊重のセグメント分割** — `echo "a && b"` のようにクォート内に
   演算子がある場合は分割しない。

### 挙動変更 (0.2.0 → 0.3.0)

| コマンド | 0.2.0 | 0.3.0 |
|---|---|---|
| `cat .env && pwd` | ask | **deny** |
| `false \|\| cat .env` | ask | **deny** |
| `cat .env; pwd` | ask | **deny** |
| `cat .env \| head` | ask | **deny** |
| `pwd\ncat .env` | ask | **deny** |
| `git status && git log 2>/dev/null` | ask | allow |
| `cat README.md 2>/dev/null` | ask | allow |
| `ls -la \| head -n 5` | ask | allow |

## 0.2.0

**breaking release** — 複数のセキュリティ強化と false-positive 増加方向の調整。
`SFG_CASE_SENSITIVE=1` の opt-out を除けば、感度は上がる方向にのみ変化する。

### 主要な変更

1. **Case-insensitive 評価を既定化** — `.ENV`, `ID_RSA`, `CREDENTIALS.JSON` を
   検出。旧挙動は `SFG_CASE_SENSITIVE=1` で復帰可能
2. **Bash handler を新設** — `cat .env`, `source .env` 等を **deny 固定**。
   間接アクセス (動的展開) は ask_or_deny (fail-closed)
3. **Edit/Write handler を新設** — 新規/既存問わず機密 path への書き込みを
   **deny 固定** (ask を挟まない)。MultiEdit は Claude Code 現行版で非搭載のため
   matcher 除外 (handler は復活時のため保持)
4. **deny reason にキー名ガイド** — dotenv 系への Write/Edit block 時、
   追加予定のキー名を reason に列挙して `.env.example` への移行を案内
5. **Submodule 内 tracked を Stop hook の検査対象に追加** — git 1.7+ 必要
6. **`foo.env` / `*.envrc` を dotenv 扱い + patterns.txt に `.envrc`/`*.envrc`** —
   matcher と engine 双方で direnv 対応
7. **fd ベース reader (TOCTOU 緩和)** — path の再 open を排除
8. **DATA タグ強化** — 固定 `guard="sfg-v1"` marker、body の `</DATA>` /
   `<DATA` / `<data>` をエンティティ化、MAX_REASON_BYTES を 4KB → 3KB に縮小
9. **Matcher / patterns のロジックを `hooks/_shared/` に集約** — 両 hook で
   同じ実装を参照 (剥離防止)
10. **Windows fail-closed** — hook 冒頭で SIGALRM 非対応環境は deny exit
