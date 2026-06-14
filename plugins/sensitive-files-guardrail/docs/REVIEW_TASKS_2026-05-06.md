# Review Tasks (2026-05-06)

`sensitive-files-guardrail` v0.5.0 の **思想ベース再評価** レビュー結果。
`REVIEW_TASKS_2026-05-03.md` (0.5.0 で完結) とは別レビューサイクル。

## このレビューの動機

ユーザーから 2 つの思想が改めて確認された。

### 思想 1 — うっかり露出予防 (敵対的防御は非目的)

> 悪意のない無意識的な操作による LLM 露出を防ぐためのガードである。
> 例:「API が失敗している → `.env` にダミーでないキーが設定されているか
> Read や cat で確認してみよう」のような軽い気持ちで機密情報を覗いてしまう
> うっかりミスを防ぐのが目的。
> **悪意あるいは誤ったプロンプトであの手この手で機密を覗こうとするケースには
> 率先して対応しない** (全てに対応するとコストがかかりすぎるため)。

### 思想 2 — block 時は意図を汲んだメッセージを返す

> 「機密ファイルは閲覧禁止」だけ返されると API 失敗の原因究明が止まる。
> **値そのものではなく値の品質情報** (set / empty / placeholder / dummy 様) を
> 返して次の作業に繋がるようにする。
> コマンドごとに目的が違う (`cat`, `grep`, `cp`, `git show`, `source`) ので
> それぞれに合わせたメッセージにする。

### 現状実装との乖離

CHANGELOG 0.3.4 の R3〜R8 修正は思想 1 に反する: `cat <(echo \(\)) < .env` の
escape paren depth tracking、`tee [[ "$x" < .env ]]` の command word 位置判定など、
**Vibe Coder のうっかり書き方ではあり得ない構文** に対する対応を「security
regression」と呼んで多数取り込んでいる。

また現状の deny reason は思想 2 の半分しか実現できていない:
- Read 側: dotenv の鍵名・型・件数は返す ✓
- Read 側: 値の品質状態 (set / empty / placeholder) は **未対応** ✗
- Bash 側: `bash_deny` で 4 種類 (literal/glob/input_redirect/input_redirect_glob)
  のみで、`cat` (全体把握) と `grep DATABASE_URL .env` (1 鍵確認) と
  `cp .env backup.env` (バックアップ) で同じ汎用文言 ✗

## 着手前提

- Python 3.11+ 標準ライブラリのみで完結する設計方針 (CLAUDE.md, README.md) は維持
- `permissionDecisionReason` のハード上限 3KB (`core/output.py::MAX_REASON_BYTES`)
- 既存テスト **670 件** (redact 643 + check 27) は **挙動仕様**として扱うが、
  A 系の削除タスクで多数 retire される。削除に伴うテスト削減も各タスクで明記
- 思想ベースのレビューであり、**機能追加よりも削除と思想整合の方が優先度が高い**
- リリース粒度はマイナーバージョン単位 (0.6.0, 0.7.0, ...) で 1 PR = 1 リリース想定

---

## A (Aggressive 削除) — 思想 1 衝突 / overengineering

### A1. `<` 入力リダイレクト char-level parser を撤廃 → ask_or_allow に格下げ

- **対象**: `hooks/redact-sensitive-reads/handlers/bash/redirects.py`
  全体 (`_scan_input_redirect_targets_with_form`, `_scan_input_redirect_targets_chars`,
  `_consume_redirect_target`, `_classify_redirect_form`)、
  `bash_handler.py::_scan_input_redirects`, `_extract_input_redirect_targets`
- **現状**: 0.3.4 で character-level quote-aware parser に移行し、R1〜R8 の
  細かい修正 (escape paren / `[[`の予約語位置 / process sub の word boundary 等)
  を取り込んでいる。`cat<.env`, `cat 0< .env`, `tee [[ "$x" < .env ]]` まで
  対応済み。**これらは Vibe Coder のうっかり書き方ではない** (普通は `cat .env`
  か `cat < .env` 空白あり)。
- **修正方針**:
  - `<` を含む command は `_has_hard_stop` 経由で丸ごと `ask_or_allow` (default
    で ask, autonomous で allow) に倒す
  - `redirects.py` 全体を削除、`bash_handler.py` から redirect 関連の import /
    呼び出しを削除
  - 妥協案として 0.3.2 の単純 regex (`(?:^|[^<&0-9])<\s+(\S+)`) のみ復活させて、
    空白付き bare 形式の literal target だけ deny する形も可。R3〜R8 の
    edge case は意図的に拾わない
- **回帰検査**:
  - `tests/test_input_redirect.py` を全削除 (約 80 件)
  - `tests/test_bash_handler.py::TestInputRedirectFormInReason` (8 件) も削除
  - `tests/test_messages.py::TestBashDenyInputRedirectForm` (8 件) も削除
- **コード削減**: ≈300 行
- **Pri**: P1 (思想衝突の最大要因)
- **依存**: A2 (RedirectForm) と一体で行う

### A2. `RedirectForm` (M5, 0.5.0) を撤廃

- **対象**: `handlers/bash/redirects.py::RedirectForm`,
  `_classify_redirect_form`, `_scan_input_redirect_targets_with_form` の form
  返却部、`core/messages.py::bash_deny` の `form` キーワード引数、GUARDRAIL_DENY body
  の `form: <値>` 行
- **現状**: `bare`/`fd_prefixed`/`no_space`/`quoted` の 4 種を返して LLM に
  「どう書かれていたか」を伝える機能。0.5.0 で +36 テスト追加。
- **修正方針**:
  - `bash_deny(..., form=...)` の引数を削除
  - GUARDRAIL_DENY body の `form:` 行は出さない (A3 と一体で GUARDRAIL_DENY 自体を撤廃するなら同時)
- **回帰検査**:
  - 0.5.0 の M5 関連テスト 36 件削除
- **コード削減**: ≈100 行
- **Pri**: P1
- **依存**: A1 と同時。A3 との同時実施が効率的

### A3. `<GUARDRAIL_DENY>` 構造化包装 (M4, 0.4.2) を plain text に戻す

- **対象**: `core/messages.py::_wrap_guardrail_deny`, `_GUARDRAIL_GUARD`, `BashDenyKind`,
  `EditDenyKind`, `bash_deny`, `edit_deny`, `policy_unavailable("deny")`、
  `redaction/sanitize.py::escape_xml_tag`
- **現状**: `<GUARDRAIL_DENY tool reason guard>` で deny reason を構造化し、後段
  hook が grep/parse できる schema を提供。0.4.2 で導入。
- **修正方針**:
  - 「後段 hook が機械処理する」前提だが、worktools にその後段 hook は存在せず
    overengineering
  - deny reason を plain text に戻す (人間 + LLM が読みやすい形)
  - `escape_xml_tag` を削除 (Read 側 `<DATA>` 包装が使う `escape_data_tag` だけ
    残す)。`escape_xml_tag(text, "DATA")` の 1 行版に再縮約
  - `kind` 引数も廃止可能 (reason 文章で symlink/special の文脈を示す)
- **回帰検査**:
  - `tests/test_messages.py::TestSfgDenyEnvelope` (12 件) 削除
  - `tests/test_sanitize.py::TestEscapeXmlTag` (7 件) 削除、
    `TestEscapeDataTag` 系のみ残す
  - 各 deny テストの `assertIn("<GUARDRAIL_DENY")` を `assertIn(<key fragment>)` に置換
- **コード削減**: ≈80 行
- **Pri**: P1
- **依存**: A1, A2 と同時 PR が効率的

### A4. prefix normalize を撤廃

- **対象**: `handlers/bash_handler.py::_normalize_segment_prefix`,
  `handlers/bash/operand_lexer.py::_is_absolute_or_relative_path_exec`,
  `handlers/bash/constants.py::_TRANSPARENT_COMMANDS`, `_OPAQUE_WRAPPERS`,
  `_ENV_PREFIX_RE`
- **現状**: `FOO=1 cat .env`, `nohup cat .env`, `command cat .env`,
  `/usr/bin/env FOO=1 cat .env` 等を「前置き剥がしで literal cat .env と同じ」と
  扱って deny 固定。0.3.2 で導入。
- **修正方針**:
  - これらは「うっかり書く形」ではないため deny する必要がない
  - 第一トークンが literal command (`cat`, `less`, `head`, `source`, ...) で
    operand が literal sensitive path のときだけ deny
  - 上記以外の prefix (`env`, `command`, `builtin`, `nohup`, `FOO=1`, abs path) は
    `ask_or_allow` (autonomous で allow)
- **回帰検査**:
  - `tests/test_prefix_normalize.py` を全削除
  - MATRIX.md の関連行 (`FOO=1 cat .env` 等の deny 列) を削除
- **コード削減**: ≈70 行
- **Pri**: P3 (P1 ほど緊急ではない)
- **依存**: なし

### A5. LENIENT_MODES の `"plan"` dead entry 削除

- **対象**: `core/output.py::LENIENT_MODES`, `tests/test_envelope_shapes.py`,
  関連 docs (`DESIGN.md`, `MATRIX.md`, `CHANGELOG.md` の 0.3.3 記述参照)
- **現状**: 0.3.3 で「plan mode 対応の前方互換層」として追加されたが、Phase 0
  実測 (2026-04-22) で **現行 CLI では plan mode で hook が発火しない** ことが
  確認されており dead entry。実装者が想像する将来のための dead code は思想に反する
- **修正方針**:
  - `LENIENT_MODES = frozenset({"auto", "bypassPermissions"})` に縮小
  - CLI が plan mode で hook を発火するよう変更されたら再追加 (再実測 Runbook
    が CLAUDE.md にある)
- **回帰検査**:
  - `tests/test_envelope_shapes.py::TestLenientModesSubset` の plan 関連
    assertion を更新
  - MATRIX.md の plan 列を削除 (mode 6 → 5)
- **コード削減**: ≈5 行 (本体) + docs の column 削減
- **Pri**: P2 (リスクゼロ)
- **依存**: なし

### A6. MultiEdit dead handler を削除

- **対象**: `__main__.py::_parse_args` の `multiedit` choice、`_dispatch` の
  multiedit 分岐、`handlers/edit_handler.py` の `tool_label="MultiEdit"` 経路、
  `_extract_dotenv_keys` の MultiEdit edits 連結ロジック、関連テスト
- **現状**: `hooks.json` から MultiEdit matcher 除外済みなのに handler 側コードと
  argparse choice は残っている。「将来 CLI に再搭載されたとき」のため dead code
- **修正方針**:
  - argparse choices を `["read", "bash", "edit", "write"]` に縮小
  - `_extract_dotenv_keys` の MultiEdit ブランチを削除
  - 関連テスト `test_edit_handler.py::TestMultiEdit*` 系を削除
  - 再搭載されたら 1 コミットで復活できるよう CHANGELOG にメモ残す
- **コード削減**: ≈40 行
- **Pri**: P2 (リスクゼロ)
- **依存**: なし

### A7. 2-tier lookup の fallback (XDG_CONFIG_HOME / ~/.config) を削除

- **対象**: `hooks/_shared/patterns.py::_resolve_local_patterns_paths`,
  `_resolve_local_patterns_path`, `load_patterns` の fallback 試行ループ、
  `core/patterns.py` / `check-sensitive-files/checker.py` の `warn_callback`
- **現状**: 0.4.0 で `~/.claude/sensitive-files-guardrail/` を preferred とし、
  旧 `$XDG_CONFIG_HOME/sensitive-files-guardrail/` を fallback として試行する
  2-tier 解決を導入。0.6.0 で fallback 削除予定と CHANGELOG に明記。
- **修正方針**:
  - 当 PR (0.6.0 想定) で fallback を削除し 1-tier に戻す
  - `_resolve_local_patterns_paths` を `_resolve_local_patterns_path` (単数形に
    戻す) に
  - `warn_callback("deprecated_config_dir")` パスを削除
  - README.md / PATTERNS.md / CLAUDE.md に「旧パスからの手動 mv 手順」を 1 段落で
    残す
- **回帰検査**:
  - `tests/test_patterns_loader.py` の 2-tier 関連 4 ケース削除
  - 旧 `$XDG_CONFIG_HOME` 採用テストを削除
- **コード削減**: ≈40 行
- **Pri**: P2
- **依存**: なし

---

## B (簡素化) — 思想と整合するが過剰

### B2. `_INJECTION_PATTERNS` 鍵名サニタイズを縮小

- **対象**: `redaction/sanitize.py::_INJECTION_PATTERNS`,
  `sanitize_key`, `sanitize_basename`
- **現状**: `(?i)(ignore previous|system:|<DATA|<assistant|<user)` 等の
  prompt-injection 文言が鍵名・basename に混入したら `[?]` 置換。
- **修正方針**:
  - 鍵名にこれらが入るのはほぼ攻撃シナリオ (=思想 1 外)
  - `<DATA` の正規表現のみ残す (Read 側の `<DATA untrusted>` 包装が破壊される
    のを防ぐため。これは値そのものに `</DATA>` が混入したケースの最低限の防御)
  - `system:` / `assistant:` / `ignore previous` 等の prompt 系は削除
  - 制御文字除去 + `MAX_KEY_LEN=128` / `MAX_BASENAME_LEN=128` の長さ切り詰めは維持
- **回帰検査**:
  - `tests/test_sanitize.py::TestInjectionPatterns` 系を縮小
- **コード削減**: ≈20 行
- **Pri**: P3
- **依存**: なし

### B3. glob 候補列挙を ask_or_allow に格下げ

- **対象**: `handlers/bash/operand_lexer.py::_glob_candidates`,
  `bash_handler.py::_glob_operand_is_sensitive`
- **現状**: `cat *.json` を `credentials*.json` 交差で deny 固定。
  「うっかり cat *.json で credentials.json が読まれる」 vs 「日常 JSON 一覧で止まる」
  の境界判定で、現状は **deny 寄り**。
- **修正方針**:
  - glob 含む operand は `ask_or_allow` (default で ask、autonomous で allow)
    に格下げ
  - `_glob_candidates`, `_glob_operand_is_sensitive` を削除
  - ただし `cat .env*` のように operand 自身が dotenv stem を含む場合のみ、
    operand stem の literal `.env` 直接 fnmatch で deny を維持する単純判定は残す
    (うっかり頻出ケース)
- **回帰検査**:
  - `tests/test_glob_candidates.py` の大半を削除
  - MATRIX.md の glob 行を更新
- **コード削減**: ≈80 行
- **Pri**: P3
- **依存**: なし

### B4. soft timeout (`SIGALRM` / `_RedactionTimeout`) を撤廃

- **対象**: `redaction/engine.py::_soft_timeout`, `_RedactionTimeout`,
  `REDACTION_SOFT_TIMEOUT`
- **現状**: catastrophic backtracking 等の保護として 1 秒 SIGALRM を仕掛ける。
  hook の外側で `timeout: 2` がかかっているため二重防御。
- **修正方針**:
  - dotenv parse は ReDoS の経路がほぼなく、外部 timeout 2 秒に任せる
  - `_soft_timeout` / `_RedactionTimeout` 削除、`engine.py` の関数は素直に動く
  - `__main__._is_unsupported_platform` の SIGALRM チェックも、A1〜A4 完了後に
    再評価して撤去判断
- **回帰検査**:
  - `tests/test_engine_timeout.py` 削除
- **コード削減**: ≈30 行
- **Pri**: P2 (リスクゼロ)
- **依存**: なし

### B5. fd reader の `fstat` 再確認を撤廃

- **対象**: `core/safepath.py::open_regular` の `fstat` 再確認部、
  `tests/test_safepath.py` の関連
- **現状**: `O_NOFOLLOW` で open した後さらに `os.fstat` で `S_ISREG` 再確認。
  「TOCTOU 完全排除は非目的」と DESIGN に書いている割に実装が手厚い
- **修正方針**:
  - `O_NOFOLLOW` は 1 行で済むので残す (うっかり symlink 経由が防げる)
  - `fstat` 再確認は撤廃 (敵対的 race の対策で思想 1 外)
- **回帰検査**:
  - `tests/test_safepath.py` の race 関連テスト 1〜2 件削除
- **コード削減**: ≈10 行
- **Pri**: P2
- **依存**: なし

> **B1 は撤回**: 前回レビューで「json/toml/yaml の format 別解析を opaque 統一」と
> 提案したが、思想 2 (意図汲み取り) を踏まえて **逆方向 (status 拡張) に倒す** ため
> E5 で扱う。

---

## E (Expansion 拡張) — 思想 2 = 意図汲み取り強化

### E1. dotenv の値「品質ステータス」を追加

- **対象**: `redaction/dotenv.py::redact_dotenv`, `format_dotenv`
- **現状**: 鍵名・順序・型 (`<type=str|jwt|bool|num>`)・件数のみ返却。
  値が「set されているか / empty か / placeholder か」が分からないため、
  API 失敗のデバッグでは不十分。
- **修正方針**:
  - 各鍵に value status を追加 (値そのものは出さない):
    - `<set>`: 値あり、placeholder にも empty にも該当しない
    - `<empty>`: `KEY=` または `KEY=""` / 空白のみ
    - `<placeholder>`: placeholder 辞書 (E2) 一致
    - `<short>`: 型から想定される最低長を下回る (jwt なのに 4 文字, URL なのに 8 文字未満)
    - `<long>`: 型から想定される長さを大きく超える (デバッグダンプ混入の可能性)
    - `<looks_truncated>`: 末尾が `...`, `<truncated>`, 改行で途中終わり
  - 値長は **coarse bucket** で出す (`length=<16` / `<64` / `<256` / `>=256`)。
    生長さは値復元の手がかりになるため bucket 化
  - placeholder 一致時は `matched="your_jwt_secret_here"` のように **辞書側のリテラル**
    だけ返す (実値ではない)
  - bool/num の `value_kind=true` / `value_kind=int` は許容 (型情報範囲内)
- **出力例**:
  ```
  keys (in order):
    1. DATABASE_URL  <type=url>  <set>          length=<64
    2. JWT_SECRET    <type=jwt>  <placeholder>  matched="your_jwt_secret_here"
    3. STRIPE_KEY    <type=str>  <empty>
    4. DEBUG         <type=bool> <set>          value_kind=true
  ```
- **回帰検査**:
  - 既存 `tests/test_redaction_minimal.py::TestDotenv` を「status 列が出る」
    assert に拡張
  - 新規テストで status 6 種それぞれの判定を網羅
- **コード追加**: ≈80 行 (engine 60, format 表示 20)
- **Pri**: P4 (思想 2 のコア)
- **依存**: E2 (placeholder 辞書) 必須

### E2. placeholder 辞書を新設 (`redaction/placeholders.py`)

- **対象**: 新規ファイル `redaction/placeholders.py`
- **修正方針**:
  ```python
  PLACEHOLDER_LITERALS = frozenset({
      "dummy", "sample", "example", "placeholder", "todo", "fixme",
      "tbd", "xxx", "changeme", "change_me", "replace_me", "your_key",
      "your_secret", "your_token", "your_password", "test", "fake",
      "lorem", "ipsum", "foobar", "asdf",
  })
  PLACEHOLDER_PATTERNS = [
      re.compile(r"^your[_-].*[_-]here$", re.I),
      re.compile(r"^<.*>$"),                  # <your-key>
      re.compile(r"^\*{3,}$"),                # ***
      re.compile(r"^x{3,}$", re.I),           # xxx, XXX
      re.compile(r"^(test|dev|local|staging)[_-]?\w*$", re.I),
  ]

  def looks_placeholder(value: str) -> tuple[bool, str | None]:
      """値が placeholder っぽいか判定。一致した辞書文字列を返す。"""
  ```
  - case-insensitive 比較
  - 戻り値は `(True, "your_jwt_secret_here")` のように辞書側リテラルを含めて返す
  - ユーザが `placeholders.local.txt` で追加できる仕組みは **作らない**
    (シンプルに保つ。要望が来たら段階的に対応)
- **回帰検査**:
  - 新規 `tests/test_placeholders.py` で 20 件程度 (各リテラル / パターン / mix)
- **コード追加**: ≈50 行
- **Pri**: P4 (E1 と一体)
- **依存**: なし

### E3. Bash コマンド別 reason テンプレートを導入

- **対象**: `core/messages.py::bash_deny`, `handlers/bash_handler.py` の reason
  生成箇所
- **現状**: `bash_deny` は kind (literal/glob/input_redirect/input_redirect_glob)
  での分岐のみ。`first_token` は表示するだけで、コマンド意図に応じた切替なし。
- **修正方針**:
  - `first_token` 別に「想定意図 → 提供する情報」を切り替える builder 体系に再編
  - コマンド分類:
    | first_token | 想定意図 | 返す情報 |
    |---|---|---|
    | `cat`, `less`, `more`, `bat`, `xxd`, `od`, `hexdump`, `base64` | 全体把握 | Read 同等の minimal info (鍵 list + status) |
    | `head`, `tail` | 先頭/末尾確認 | 鍵 list の先頭/末尾 N 件 (`-n N` を読む) |
    | `grep`, `rg`, `ag`, `ack`, `egrep`, `fgrep` | 特定行検索 | パターン抽出 → 該当キーの詳細 (E4) |
    | `awk`, `sed` | 加工 | 「加工は実行できないが現在の鍵 list は以下」+ minimal info |
    | `source`, `.` | shell load | 「load 目的なら direnv (`.envrc`) や `dotenv-cli` を推奨。現在の鍵 list:」 |
    | `cp`, `mv` | ファイル操作 | 「コピー/移動は別パスへの漏洩リスクで block。バックアップが目的なら 1Password CLI / pass / git-secret 等の secrets manager を推奨。`.env.example` 派生で運用するなら `cp .env.example .env.local` で代替」 |
    | `git show HEAD:.env`, `git diff .env`, `git log -p .env` | 履歴確認 | 「git 経由で過去 commit の値を見ようとしました。**この .env が tracked になっているなら漏洩済み** → `git rm --cached .env` 後に rotate 推奨。tracked でないなら別パスを参照」 |
    | `curl file://`, `wget`, `scp`, `rsync` | 転送 | 「ネット越し / リモートへの転送は強く非推奨」 |
    | `tar`, `zip`, `gzip` | アーカイブ | 「機密ファイルをアーカイブに含めようとしました。`--exclude=.env` の指定を推奨」 |
    | その他 | 不明 | 既存の generic reason + 鍵 list |
  - 各分岐は `messages.py` の独立関数 (`bash_deny_cat`, `bash_deny_grep` ...) に
    切り出し、`bash_deny` から dispatch する
- **回帰検査**:
  - 新規 `tests/test_bash_reason_templates.py` で各 first_token の reason に
    特徴的な suggestion (`direnv`, `git rm --cached`, `1Password CLI` 等) が
    含まれることを assert
- **コード追加**: ≈120 行
- **Pri**: P5
- **依存**: A3 (GUARDRAIL_DENY 撤去) を先に行うとメッセージ整形が楽

### E4. grep のパターン抽出 + 1 鍵詳細表示

- **対象**: `core/messages.py::bash_deny_grep` (E3 で新設)、
  `handlers/bash_handler.py::_analyze_segment` で grep 検出時に envelope から
  パターンを取得する処理
- **修正方針**:
  - 第一トークン `grep / rg / ag / ack / egrep / fgrep` を判定
  - operand から「環境変数名らしき token」を抽出:
    - `^[A-Z][A-Z0-9_]{2,}$` (env var 命名規則)
    - `--regex=...` / `-e ...` / `--pattern=...` の中身
    - `-E '...|...'` の `|` 分割
  - 抽出キー名を rules ではなく **dotenv parse 結果との照合** で「該当キーがあれば
    status を返す、なければ `nomatch`」
  - 出力例:
    ```
    note: grep で .env の特定キーを確認しようとしました。
    matched_pattern_keys: [DATABASE_URL]
    result:
      DATABASE_URL  <type=url>  <set>  length=<64
    suggestion: API 失敗の調査なら他の鍵 (JWT_SECRET=<placeholder>) も見直してください。
    ```
- **回帰検査**:
  - 新規テスト 10 件程度: pattern 抽出 / 照合 / nomatch / 複数 pattern
- **コード追加**: ≈60 行
- **Pri**: P5 (思想 2 で最も実用的)
- **依存**: E1, E2, E3

### E5. JSON / TOML / YAML にも status 拡張 (B1 撤回)

- **対象**: `redaction/jsonlike.py::redact_jsonlike`, `format_jsonlike`,
  `redaction/tomllike.py::redact_toml`, `format_toml`,
  `redaction/opaque.py::redact_opaque` (yaml の最低限抽出を新設)
- **現状**: json/toml は鍵名・件数を返すが status はなし。yaml は完全 opaque。
- **修正方針**:
  - dotenv と同じ `<set>` / `<empty>` / `<placeholder>` / `<short>` /
    `<long>` を json/toml の値判定にも適用
  - yaml は新規に簡易 key 抽出器を追加: `^([A-Za-z_][A-Za-z0-9_-]*):` の行抽出
    ベースで top-level keys + nested は `<nested>` で 1 件カウントのみ
  - **ネストは追わない** (思想 1: 完全パースは過剰)
- **回帰検査**:
  - `tests/test_redaction_minimal.py` の JSON / TOML / YAML テストを status
    列が出る形に拡張
- **コード追加**: ≈80 行
- **Pri**: P6
- **依存**: E1, E2

### E6. Edit/Write の意図汲み取りメッセージ拡張

- **対象**: `core/messages.py::edit_deny`, `handlers/edit_handler.py`
- **現状**: dotenv キー名抽出 + `.env.example` 移行案内はあるが、
  「新規作成 vs 既存上書き」「symlink 経由」等のニュアンス分岐が薄い。
- **修正方針**:
  - 状況分岐を細かく:
    - **新規 `.env` 作成 + キー名抽出**: 「同じキーで `.env.example` を作って
      空値にし、実値は手動入力 or 1Password CLI 経由でセットしてください」
    - **既存 `.env` 上書き** (Read 前提済みのケース): 「`.env.example` の差分のみ
      取り込みたいなら `dotenv-cli` の merge 機能を推奨」
    - **symlink 経由**: 「symlink 先が意図した参照か。実体が `~/secrets/` 等の
      共有 path なら、コピーではなく symlink を維持する運用を推奨」
- **回帰検査**:
  - `tests/test_edit_handler.py::TestDenyReason*` を分岐別に拡張
- **コード追加**: ≈30 行
- **Pri**: P6
- **依存**: A3 (GUARDRAIL_DENY 撤去) を先に

---

## F (Fix / 細粒度化) — 思想 1 整合の細粒度化

### F1. hard-stop を segment 単位で再評価

- **対象**: `hooks/redact-sensitive-reads/handlers/bash_handler.py` の
  `handle()` における全体 hard-stop early return
- **動機**: 現行 (0.10.0 まで) は command 全体に hard-stop char (`$`,
  バッククォート, `(`, `)`, `{`, `}`, `<`, `\r`) が 1 つでもあると
  `ask_or_allow` で early return していたため、`cat .env | sed 's/(=)/X/'`
  のような複合で sed segment の `(` が原因で全体 ask に倒れ、autonomous
  (auto / bypassPermissions) で `cat .env*` 系が素通りしていた。ユーザー
  報告の `ls ... && ... && cat .env.local 2>/dev/null | sed -E 's/(=).*/\1.../' | head -20`
  が代表例。
- **思想整合**: cat segment は「うっかり cat .env」 → 思想 1 (うっかり露出
  予防が目的、敵対的防御は非目的) の射程内。攻撃シナリオ
  `cat <(echo \(\)) < .env` は全 segment が hard-stop となるため挙動不変
  (どの segment も `pending_ask` に格納されて ask に倒る) → 思想 1 を
  侵害しない
- **修正方針**: 全体 hard-stop early return を撤廃。`_split_command_on_operators`
  で分割した各 segment ごとに `_has_hard_stop` を再判定。hard-stop / shlex
  失敗の segment は `pending_ask` に格納して **continue** (即 return
  しない)。これにより `cat .env | grep '(=)'` のように先頭 segment で
  literal match すれば deny 短絡し、`cat $X | ls .env | head` のように
  後段 segment で literal match すれば pending_ask を超えて deny に到達する
- **回帰検査**: 既存 605 件 (redact 578 + check 27) regression 0、新規
  `TestSegmentHardStopReevaluate` を `tests/test_bash_handler.py` に追加
  (14 件: 核心 3 mode + seg deny short-circuit 4 + 既存挙動継続 5 +
  境界 1 + reason 文確認 1)
- **コード差分**: `bash_handler.py` 約 +18 / -8 行 (実質 +10 行)、
  `bash/segmentation.py` docstring +約 7 行、テスト約 +130 行
- **Pri**: P1 (autonomous での素通り穴を塞ぐ最大の修正)
- **依存**: なし

### F2. read-only first_token allow-list で residual metachar の ask を allow に倒す

- **対象**: `hooks/redact-sensitive-reads/handlers/bash/constants.py`
  (`_SAFE_READ_FIRST_TOKENS` 新設)、`handlers/bash_handler.py::_analyze_segment`
- **動機**: 0.11.0 までのログ実測で `bash_classify` の ask 発火の **約 80%**
  が `segment_residual_metachar_lenient` 起因 (`>` 出力リダイレクトや `&`
  background)。`grep foo README.md > /tmp/out` / `ls > listing.txt` /
  `cat README.md | wc -l > out` のような調査ワンライナーが ask に倒れて
  ユーザー体感 UX を阻害していた
- **思想整合**: 「うっかり露出予防」が目的で、副作用なしの read-only コマンド
  (`ls cat head tail wc grep ...`) が `>` で出力を保存する形は「うっかり機密
  露出」とは異質。機密 redirect target (`grep foo > .env`) は operand scan で
  deny 固定、hard-stop (`$()` / `<`) は引き続き ask 維持で safety net を保つ
- **修正方針**:
  - `_SAFE_READ_FIRST_TOKENS = frozenset({"ls","cat","head","tail","nl","tac",
    "bat","less","more","view","wc","file","stat","du","df","tree","grep",
    "egrep","fgrep","rg","ag","ack","od","xxd","hexdump"})` を新設
  - 第一トークンが該当する場合のみ `_segment_has_residual_metachar` の ask
    経路をスキップして operand scan に直行
  - `_OPAQUE_WRAPPERS` / `_SHELL_KEYWORDS` とは disjoint なので opaque /
    shell_keyword 経路には影響しない
  - `awk` / `sed` / `find` / `xargs` / `echo` は副作用持つ可能性のため
    allow-list **外**
- **回帰検査**: 既存 619 件 (redact 592 + check 27) regression 0、新規
  `TestSafeReadAllowlist` を `tests/test_bash_handler.py` に追加
  (21 件: redirect allow + 機密 deny 固定 + hard-stop ask 維持 + allow-list 外
  ask 維持 + 複合)
- **コード差分**: `bash/constants.py` 約 +32 行、`bash_handler.py` 約 +14 / -3
  (実質 +11 行)、テスト約 +130 行
- **Pri**: P2 (ユーザー体感 UX の主要不満解消)
- **依存**: なし (F1 完了後の状態に対する追加)

---

## C (Conserve 維持) — 思想ど真ん中

| 項目 | 維持理由 |
|---|---|
| `cat .env` / `head .env` 等の literal first token + literal operand スキャン | うっかり防止のコア |
| Edit/Write の deny 固定 (`ask` 挟まない) | ask 疲労による誤承認の実機教訓 (0.2.0 beta) |
| 安全リダイレクト剥離 (`2>/dev/null` 等) | `git status \| head 2>/dev/null` を allow するための日常 UX |
| dotenv の鍵名・型・件数の minimal info | 思想 2 のコア機能 |
| Stop hook の tracked/untracked 検出 + AskUserQuestion 案内 | コミット直前の最後の砦 |
| `patterns.local.txt` の `!<basename>` exclude 仕組み | 簡潔で利用者が制御可能 |
| `patterns.txt` 読込失敗時の fail-closed (Bash deny / Read ask_or_deny) | 最低限のポリシー欠如対策 |
| `<DATA untrusted="true">` Read 包装 + `escape_data_tag` | 鍵名は LLM コンテキストに残るため最低限の包装防御 |
| `O_NOFOLLOW` での Read 側 fd open | 1 行で「うっかり symlink follow」を防げる |

---

## D (Documentation 整理)

### D1. ドキュメントを README + CHANGELOG + CLAUDE.md の 3 ファイルに集約

- **対象**: `docs/DESIGN.md` (318 行), `docs/MATRIX.md` (140 行),
  `docs/PATTERNS.md` (254 行), `docs/REVIEW_TASKS_2026-05-03.md`,
  `CLAUDE.local.md` (175 行)
- **修正方針**:
  - `README.md` (現 203 行) に「インストール / 思想 / 既知制限 /
    patterns.local.txt / 動作の要約」を集約 (約 200 行を維持)
  - `CLAUDE.md` (現 saved-not-checked-in 175 行) に「保守者向け: 設計原則 /
    テスト / リリース手順 / Phase 0 ログ要点 / Runbook」を集約 (約 100 行)
  - `docs/` 配下を全削除
  - 完了済み `docs/REVIEW_TASKS_2026-05-03.md` は git tag (`v0.5.0` 等) で参照
    可能。本ファイル `REVIEW_TASKS_2026-05-06.md` は完結まで `docs/` に残す
  - `MATRIX.md` の判定マトリクスは README に圧縮 (mode 5 列のみ、A5 完了後)
- **コード削減**: ドキュメント約 700 行削減
- **Pri**: P6 (A〜E の最後)
- **依存**: A 系の完了後 (mode 列削減 / 構造化包装の説明撤去等が反映される)

### D2. テスト総数を整理

- **対象**: 全 `tests/` 配下
- **現状**: 670 件
- **修正方針**:
  - A 系で約 220 件削除 (input_redirect 80, RedirectForm 36, GUARDRAIL_DENY 12+7,
    prefix_normalize ≈30, glob_candidates ≈50, multiedit ≈10)
  - B 系で約 30 件削除 (sanitize injection / engine_timeout / safepath race)
  - E 系で約 80 件追加 (placeholders / dotenv status / Bash reason templates /
    grep extraction / json toml yaml status)
  - 最終: 約 500 件 (差し引き ≈170 件減)
- **Pri**: 各 A/B/E PR と同時 (独立タスクではない)

---

## 実装順序 (PR 単位、リリース粒度)

| PR # | 含まれるタスク | 想定 version | リスク | 価値 |
|---|---|---|---|---|
| 1 | A5 + A6 + A7 + B4 + B5 | 0.6.0 | ゼロ (純粋撤去) | dead code 削除 ≈125 行 |
| 2 | A1 + A2 + A3 | 0.7.0 | API 互換性 (内部のみ) | 思想衝突解消 ≈480 行削減 |
| 3 | A4 + B2 + B3 | 0.8.0 | autonomous で素通りするケースが増える | 思想 1 仕上げ ≈170 行削減 |
| 4 | E1 + E2 | 0.9.0 | 機能追加 | 思想 2 のコア (dotenv status) |
| 5 | E3 + E4 | 0.10.0 | 機能追加 | 思想 2 の応用 (Bash コマンド別 reason) |
| 5.5 | F1 | 0.11.0 | 細粒度化 (autonomous での素通り穴を塞ぐ) | 思想 1 整合の細粒度化 |
| 5.6 | F2 | 0.12.0 | 副作用 redirect を allow に倒す (ユーザー体感 UX 修正) | read-only first_token allow-list 新設 |
| 6 | E5 + E6 + D1 + D2 | 1.0.0 | 機能追加 + ドキュメント | 思想 2 の完成形 |

**1.0.0 で本レビューサイクル完結**を想定。

PR 1 から順に着手することで、後の PR で「メッセージ整形 (E3) と
GUARDRAIL_DENY 包装 (A3 撤去) の同時並行で衝突する」等の手戻りを回避する。

---

## 論点 (実装着手前にユーザー確認が必要なもの)

### Q1. `<placeholder>` 判定基準の真面目さ

- 簡易版 (E2 の literals + 5 つの regex) で開始するか
- 最初から `placeholders.local.txt` を作って拡張可能にするか
- **推奨**: 簡易で開始。要望が来たら拡張

### Q2. 値長 bucket (`<16` / `<64` / `<256` / `>=256`) の妥当性

- 4 段階 bucket は値復元の手がかりにならない設計
- ただし「秘密鍵が `<16`」なら明らかに壊れている = デバッグに有用
- **推奨**: 4 段階で開始。実運用で再評価

### Q3. type 推定の prefix-only 表示の可否

- 候補: `url / email / uuid / base64 / hex / aws_access_key (AKIA...) /
  aws_secret / stripe_pk (pk_live_) / github_pat (ghp_)`
- prefix だけ表示する案: `<type=stripe_secret prefix="sk_live_">`
- 利点: ローテート判断 (production key であること) に有用
- 欠点: 値の一部が漏れる (sec ops 視点では許容範囲か境界判定)
- **推奨**: prefix 表示は **採用しない**。`<type=stripe_secret>` までで止める

これらは E1〜E2 の実装着手前 (PR 4) に `AskUserQuestion` で確定する。

---

## 進捗

### 2026-05-06: PR 1 完了 (0.6.0 リリース) — A5 / A6 / A7 / B4 / B5

リスクゼロ撤去 5 件をまとめて消化。non-breaking で「想像できる将来のための
前方互換層」「現行 CLI で使われない機能」「敵対的 race の二重防御」を縮小。

#### 実装

- **A5**: `LENIENT_MODES` から `"plan"` 削除 (2 値: auto / bypassPermissions)
- **A6**: MultiEdit dead handler 撤去 (argparse choices / dispatch / edit_handler
  の MultiEdit ブランチ / 4 件のテスト / multiedit.json fixture を削除)
- **A7**: `patterns.local.txt` の参照先を `~/.claude/sensitive-files-guardrail/`
  単一パスに縮小 (`_resolve_local_patterns_paths` 複数形と
  `warn_callback("deprecated_config_dir")` 経路を撤去)
- **B4**: `redaction/engine.py` の `_soft_timeout` / `SIGALRM` 撤廃
  (外部 hook timeout 2s に集約)
- **B5**: `open_regular` の `fstat` 再確認撤廃 (`O_NOFOLLOW` のみで symlink
  防御)

#### テスト結果

- 累計 670 → **657 件 OK** (redact 643→630 件 / check 27 件維持)
- 削除: `test_engine_timeout.py`, `test_multiedit_envelope`, `TestMultiEdit`,
  `test_multiedit_aggregates_keys`, `test_multiedit_dotenv_denies`,
  `TestPreferredFallback2Tier` 系 + 2-tier resolve 系
- 改修: `test_plan_returns_*` 2 件 (allow → ask), `TestBashLenient::test_hard_stop`
  (plan 文字列 assert 削除), `TestLocalPatternsLoader` の write 先を preferred
  path に変更

#### ドキュメント更新

- `docs/MATRIX.md`: 6 mode 列 → 5 mode 列 (plan 列削除)
- `docs/DESIGN.md`: Phase 0 plan ログを 0.6.0 撤去記録に更新
- `docs/PATTERNS.md`: 2-tier lookup → 単一パスに書換
- `README.md`: パターン設定 + MultiEdit note を整理
- `CLAUDE.local.md`: 2-tier 説明 + mermaid 図 + 非目的記述を更新
- `CHANGELOG.md`: 0.6.0 エントリ追加 (本 PR の差分まとめ)

#### `claude plugin validate` 結果

```
$ claude plugin validate plugins/sensitive-files-guardrail
Validating plugin manifest: .../sensitive-files-guardrail/.claude-plugin/plugin.json
✔ Validation passed
```

#### 完了状況サマリ (0.6.0 時点)

| Pri / カテゴリ | タスク | 状態 |
|---|---|---|
| P2 | A5 (LENIENT_MODES の plan dead entry 削除) | **0.6.0 ✓** |
| P2 | A6 (MultiEdit dead handler 削除) | **0.6.0 ✓** |
| P2 | A7 (2-tier lookup の fallback 削除) | **0.6.0 ✓** |
| P2 | B4 (soft timeout 撤廃) | **0.6.0 ✓** |
| P2 | B5 (fstat 再確認撤廃) | **0.6.0 ✓** |
| P1 | A1 (`<` redirect char-level parser 撤去) | 未着手 (PR 2) |
| P1 | A2 (RedirectForm 撤去) | 未着手 (PR 2) |
| P1 | A3 (`<GUARDRAIL_DENY>` 構造化包装撤去) | 未着手 (PR 2) |
| P3 | A4 (prefix normalize 撤廃) | 未着手 (PR 3) |
| P3 | B2 (`_INJECTION_PATTERNS` 縮小) | 未着手 (PR 3) |
| P3 | B3 (glob 候補列挙を ask 格下げ) | 未着手 (PR 3) |
| P4 | E1 (dotenv value status) | 未着手 (PR 4) |
| P4 | E2 (placeholder 辞書) | 未着手 (PR 4) |
| P5 | E3 (Bash コマンド別 reason) | 未着手 (PR 5) |
| P5 | E4 (grep パターン抽出) | 未着手 (PR 5) |
| P6 | E5 (json/toml/yaml status) | 未着手 (PR 6) |
| P6 | E6 (Edit/Write リッチ化) | 未着手 (PR 6) |
| P6 | D1 / D2 (docs / tests 整理) | 未着手 (PR 6) |

### 2026-05-06: PR 2 完了 (0.7.0 リリース) — A1 / A2 / A3

思想衝突の主役 3 件を一体で消化。`<` 入力リダイレクトの character-level parser、
RedirectForm タグ、`<GUARDRAIL_DENY>` 構造化包装を撤廃し、deny reason を plain text に
戻した。**deny 動作の判定境界は 1 種類のみ変化** (`<` 入力リダイレクト → 他の
hard-stop と同じ ``ask_or_allow`` 扱い) で、operand scan 経路の deny は変更なし。

#### 実装

- **A1**: `handlers/bash/redirects.py` から `_scan_input_redirect_targets_chars`
  / `_scan_input_redirect_targets_with_form` / `_consume_redirect_target` /
  `_classify_redirect_form` / `_DQ_BACKSLASH_ESCAPABLE` を撤去。
  `bash_handler` から `_extract_input_redirect_targets` (patch seam) /
  `_scan_input_redirects` を削除し、`_has_hard_stop` 経由で `<` を含む command を
  ``ask_or_allow`` に倒す形に変更。
- **A2**: `handlers/bash/redirects.py::RedirectForm` Literal、
  `core/messages.py::bash_deny(form=...)` キーワード引数、GUARDRAIL_DENY body の
  `form: <値>` 行を撤廃。
- **A3**: `core/messages.py` から `_wrap_guardrail_deny` / `_GUARDRAIL_GUARD` /
  `BashDenyKind` / `EditDenyKind` / `bash_deny(kind=...)` / `edit_deny(kind=...)`
  を撤去し、`bash_deny` / `edit_deny` / `policy_unavailable("deny")` を plain text
  形式に書換。`redaction/sanitize.py::escape_xml_tag` を撤去し、`escape_data_tag`
  を DATA タグ専用の直接実装に縮約。`handlers/edit_handler.py` の symlink /
  special caller から `kind=...` 引数渡しを削除 (extra_note のみで文脈表現)。

#### テスト結果

- 累計 657 → **495 件 OK** (redact 630→468 件 / check 27 件維持)
- 削除: `tests/test_input_redirect.py` (130 件全削除)、
  `test_messages.py::TestSfgDenyEnvelope` (12 件)、
  `TestBashDenyInputRedirectForm` (8 件)、
  `TestBashDenyInputRedirect` (2 件)、
  `test_sanitize.py::TestEscapeXmlTag` (7 件)、
  `test_bash_handler.py::TestInputRedirectFormInReason` (8 件)、
  `TestDenyReasonContent.test_input_redirect_includes_target` (1 件)
- 統合: `TestBashDenyLiteral` / `TestBashDenyGlob` を `TestBashDeny` (4 件) に統合
- 新規: `test_messages.py::TestDenyPlainText` (6 件、deny 系が plain text で
  出ることの assert)、`test_bash_handler.py::TestInputRedirectAskOrAllow`
  (3 件、`<` 入力リダイレクトの ask/allow 動作 regression)
- 改修: `test_messages.py::TestVocabularyConsistency.test_deny_uses_block`
  (kind 引数なし)、`test_e2e.py::test_bash_auto_input_redirect_allows`
  (旧 `denies` を allow に書換)

#### コード削減

- `handlers/bash/redirects.py`: 475 → 62 行 (-413 行)
- `core/messages.py`: -108 行 (GUARDRAIL_DENY 包装 + kind/form 引数 + 関連 docstring)
- `handlers/bash_handler.py`: -69 行
- `redaction/sanitize.py`: -19 行
- `handlers/edit_handler.py`: -2 行
- **合計: -611 行**

#### ドキュメント更新

- `docs/MATRIX.md`: 「`<` target 抽出」関連 4 行を Bash deny 表から削除し、
  「Bash 静的解析不能 (三態判定)」表に「`<` 入力リダイレクト, 0.7.0 で格下げ」を追加
- `docs/DESIGN.md`: 「Bash handler の対応文法範囲」セクションから
  character-level quote-aware parser の記述を撤去。既知制限 #3 を「`<`
  入力リダイレクトは ask_or_allow 扱い (0.7.0)」に書換。`_extract_input_redirect_targets`
  patch seam を撤去
- `README.md`: Bash 三態判定の説明から「`< target` の target が機密」を削除し、
  ask_or_allow 側の例として `<` 入力リダイレクトを明示
- `CLAUDE.local.md`: 「進行中のレビュー」進捗を 0.7.0 完了で更新、
  patch seam テーブル / Bash handler mermaid を GUARDRAIL_DENY / input redirect
  撤去後に整合
- `CHANGELOG.md`: 0.7.0 エントリ追加 (本 PR の差分まとめ)

#### `claude plugin validate` 結果

```
$ claude plugin validate plugins/sensitive-files-guardrail
Validating plugin manifest: .../sensitive-files-guardrail/.claude-plugin/plugin.json
✔ Validation passed
```

#### 完了状況サマリ (0.7.0 時点)

| Pri / カテゴリ | タスク | 状態 |
|---|---|---|
| P2 | A5 / A6 / A7 / B4 / B5 | **0.6.0 ✓** |
| P1 | A1 (`<` redirect char-level parser 撤去) | **0.7.0 ✓** |
| P1 | A2 (RedirectForm 撤去) | **0.7.0 ✓** |
| P1 | A3 (`<GUARDRAIL_DENY>` 構造化包装撤去) | **0.7.0 ✓** |
| P3 | A4 (prefix normalize 撤廃) | 未着手 (PR 3) |
| P3 | B2 (`_INJECTION_PATTERNS` 縮小) | 未着手 (PR 3) |
| P3 | B3 (glob 候補列挙を ask 格下げ) | 未着手 (PR 3) |
| P4 | E1 (dotenv value status) | 未着手 (PR 4) |
| P4 | E2 (placeholder 辞書) | 未着手 (PR 4) |
| P5 | E3 (Bash コマンド別 reason) | 未着手 (PR 5) |
| P5 | E4 (grep パターン抽出) | 未着手 (PR 5) |
| P6 | E5 (json/toml/yaml status) | 未着手 (PR 6) |
| P6 | E6 (Edit/Write リッチ化) | 未着手 (PR 6) |
| P6 | D1 / D2 (docs / tests 整理) | 未着手 (PR 6) |

### 2026-05-06: PR 3 完了 (0.8.0 リリース) — A4 / B2 / B3

思想 1 (うっかり露出予防、敵対的防御は非目的) 仕上げの 3 件を一体で消化。
prefix normalize / 鍵名 prompt-injection 文言除去 / 既定 rules 候補列挙を撤廃
し、autonomous で素通りするケースを増やすことで「うっかり書かない形」
(``FOO=1 cat .env`` / ``cat *.key`` / 鍵名 ``system:foo``) への過剰対応を取り
払った。**deny 動作の判定境界が複数変化** (prefix 系 deny → ask_or_allow、
非 dotenv glob deny → ask_or_allow、ただし ``cat .env`` 等 literal や ``cat .env*``
等 dotenv glob は deny 維持)。

#### 実装

- **A4**: `bash_handler._normalize_segment_prefix` (60 行)、
  `bash/operand_lexer._is_absolute_or_relative_path_exec`、
  `bash/constants._TRANSPARENT_COMMANDS` を撤廃し、`_analyze_segment` に
  `_is_opaque_first_token` の inline 判定を新設。第一トークンが env-assignment
  (``FOO=1``) / `env` / `command` / `builtin` / `nohup` / opaque wrapper /
  任意 path exec のいずれかなら `ask_or_allow("opaque_prefix")` に倒す。
  `_OPAQUE_WRAPPERS` に `env` / `command` / `builtin` / `nohup` を統合。
- **B2**: `redaction/sanitize._INJECTION_PATTERNS` を case-insensitive な
  `</?\\s*DATA` 1 行に縮小。`ignore previous` / `ignore all` / `system:` /
  `assistant:` / `</?system|</?user|</?assistant` の prompt-injection 文言を
  鍵名・basename から `[?]` 置換するロジックを撤廃 (思想 1 外)。制御文字
  除去 + `MAX_KEY_LEN=128` / `MAX_BASENAME_LEN=128` の長さ切り詰めは維持。
- **B3**: `bash_handler._glob_operand_is_sensitive`、
  `bash/operand_lexer._literalize` / `_glob_candidates` を撤廃し、
  `_glob_operand_is_dotenv_match(operand)` (operand_lexer.py) を新設。
  ``fnmatchcase(stem, op_glob)`` で stem ∈ `(.env, .envrc)` の literal 一致を
  判定。`_analyze_segment` の glob 分岐は新関数で True なら deny、False なら
  ``ask_or_allow`` に倒す形に変更。

#### テスト結果

- 累計 495 → **465 件 OK** (redact 468→438 件 / check 27 件維持)
- 削除: `tests/test_prefix_normalize.py` (24 件)、
  `tests/test_glob_candidates.py` (25 件)、
  `tests/test_sanitize.py::TestSanitizeKey.test_injection_ignore` /
  `test_injection_system` / `TestSanitizeBasename.test_injection`
- 改修: `TestPrefixStrippingDeny` を `TestOpaquePrefixAskOrAllow` に書き換え
  (12 ケース、deny → ask/allow)、`TestPrefixStrippingOpaque` を
  `TestPrefixWithOptionsOpaque` に rename (内容維持)、`TestWrapperBypass.test_nohup_cat`
  を `_default` / `_auto` に分割 (ask/allow)、`TestGlobMatch` を `TestGlobDotenvDeny` /
  `TestGlobUncertainAskOrAllow` / `TestGlobLiteralExcludeAllow` に再編、
  `tests/test_e2e.py` の `auto_env_prefix_dotenv_denies` / `auto_abs_env_basename_denies`
  を `_allows` に書き換え
- 新規: `tests/test_glob_dotenv.py` (18 件、`_glob_operand_is_dotenv_match`
  単体: dotenv 一致 / 非一致 / edge case / case sensitivity)、
  `test_sanitize.py::TestSanitizeKey.test_injection_data_tag_open` /
  `test_prompt_text_passthrough`、`TestSanitizeBasename.test_data_tag_collision` /
  `test_prompt_text_passthrough`

#### コード削減

| ファイル | 行数差 |
|---|---|
| `handlers/bash_handler.py` | -75 (`_normalize_segment_prefix` 60 行 + `_glob_operand_is_sensitive` 14 行 + re-export) |
| `handlers/bash/operand_lexer.py` | -50 (削除/新設の差分、新 helper の docstring 厚め) |
| `handlers/bash/constants.py` | -3 |
| `redaction/sanitize.py` | -3 |
| 合計 | **-131 行** |

#### ドキュメント更新

- `docs/MATRIX.md`: 5 mode 列を維持しつつ、Bash deny 表から prefix 系 / 一般
  glob 系を ask_or_allow 表に移動。dotenv stem 一致 glob は deny 維持で整理
- `docs/DESIGN.md`: Bash handler 対応文法範囲表に「dotenv glob 一致 / opaque
  first token / 非 dotenv glob」を追加、既知制限 #2 / #4 を 0.8.0 に書換、
  責務境界 test seam テーブルから旧 symbol を整理、`_glob_candidates` 歴史
  セクションを「glob operand 判定の歴史 (0.3.2 → 0.8.0)」に書換
- `README.md`: glob false positive note を 0.8.0 で書換、prefix normalize 撤廃
  note を追加、テスト件数を 438 に更新
- `CLAUDE.local.md`: 進捗を 0.8.0 (PR 3) で更新、plugin.json version 0.8.0、
  patch seam テーブル整理、Bash handler 判定フロー mermaid を opaque first
  token / dotenv glob match の新フローに書換
- `CHANGELOG.md`: 0.8.0 エントリ追加 (本 PR の差分まとめ)

#### `claude plugin validate` 結果

```
$ claude plugin validate plugins/sensitive-files-guardrail
Validating plugin manifest: .../sensitive-files-guardrail/.claude-plugin/plugin.json
Validating plugin: .../sensitive-files-guardrail/CLAUDE.local.md
⚠ Found 1 warning:
  ❯ root: CLAUDE.local.md at the plugin root is not loaded as project context.
✔ Validation passed with warnings
```

`CLAUDE.local.md` 配置の warning は plugin 設計上の既知事項 (個人環境メモを
公開リポジトリの plugin root に置かないようにすべき、という指針)。本 PR の
範囲外なので残置。

#### 完了状況サマリ (0.8.0 時点)

| Pri / カテゴリ | タスク | 状態 |
|---|---|---|
| P2 | A5 / A6 / A7 / B4 / B5 | **0.6.0 ✓** |
| P1 | A1 / A2 / A3 | **0.7.0 ✓** |
| P3 | A4 (prefix normalize 撤廃) | **0.8.0 ✓** |
| P3 | B2 (`_INJECTION_PATTERNS` 縮小) | **0.8.0 ✓** |
| P3 | B3 (glob 候補列挙を ask 格下げ) | **0.8.0 ✓** |
| P4 | E1 (dotenv value status) | 未着手 (PR 4) |
| P4 | E2 (placeholder 辞書) | 未着手 (PR 4) |
| P5 | E3 (Bash コマンド別 reason) | 未着手 (PR 5) |
| P5 | E4 (grep パターン抽出) | 未着手 (PR 5) |
| P6 | E5 (json/toml/yaml status) | 未着手 (PR 6) |
| P6 | E6 (Edit/Write リッチ化) | 未着手 (PR 6) |
| P6 | D1 / D2 (docs / tests 整理) | 未着手 (PR 6) |

### 2026-05-06: PR 4 完了 (0.9.0 リリース) — E1 / E2

思想 2 (block 時は意図を汲んだメッセージを返す) のコア機能を実装。dotenv の
minimal info に value status / 生バイト長 / 識別子型 prefix / placeholder ヒント
を追加し、「機密ファイルは閲覧禁止」だけでは API 失敗の原因究明が止まる問題を
解消した。**deny 動作の判定境界は変化なし**で、reason 文字列の情報量だけが拡張
された機能追加リリース。

#### 論点 Q1〜Q3 のユーザー確定回答

- Q1: **簡易版で開始** (PLACEHOLDER_LITERALS 21 個 + 5 regex、ユーザー拡張点なし)
- Q2: **bucket なし、生長さを返す** (`length=42` 形式)
- Q3: **prefix も表示する** (`<type=stripe_secret prefix="sk_live_">`)

ユーザー強調: 「うっかりミスによる流出を防ぎつつ開発作業を停滞させないための
機構が最優先」→ **情報量は積極的に返す**方針。実値そのものは引き続き出さない
が、長さ / 型 / prefix / placeholder 情報は LLM が次の作業を判断できる粒度で渡す。

#### 実装

- **E2**: `redaction/placeholders.py` を新設。PLACEHOLDER_LITERALS (21 個) +
  PLACEHOLDER_PATTERNS (5 個 (regex, label) tuple) + `looks_placeholder(value)`
  を実装。case-insensitive、quote 剥がし対応、戻り値は辞書側 literal /
  pattern label。
- **E1**: `redaction/dotenv.py` 大改修。
  - `_classify_value` を `_detect_type_and_prefix` に置換、型を 14 種に拡張
    (str / bool / null / num / jwt / url / email / uuid / aws_access_key /
    stripe_secret / stripe_pk / github_pat / openai_key)
  - `_classify_status` 新設、6 タグ (set/empty/placeholder/short/long/
    looks_truncated) を複数併記可能に判定
  - `_preprocess_value` で型判定 / status 判定 / length 計測を共通化
  - `format_dotenv` を新出力 (type/prefix/status/length/matched 多列) に書換

#### テスト結果

- 累計 465 → **545 件 OK** (redact 438→518 件、+80 件 / check 27 件維持)
- 新規:
  - `test_placeholders.py` (29 件)
  - `test_redaction_minimal.py::TestDotenvTypeExpansion` (15 件)
  - `test_redaction_minimal.py::TestDotenvPrefix` (11 件)
  - `test_redaction_minimal.py::TestDotenvStatus` (16 件)
  - `test_redaction_minimal.py::TestDotenvFormatOutput` (9 件)
- 既存テストはすべて維持 (format_dotenv の出力変更があっても、既存の
  `assertEqual(info["format"], "dotenv")` / `<type=bool>` / `<type=num>` の
  存在チェックは互換)

#### コード追加

| ファイル | 行数差 |
|---|---|
| `redaction/placeholders.py` | +60 (新規) |
| `redaction/dotenv.py` | +175 (E1 拡張) |
| `tests/test_placeholders.py` | +153 (新規) |
| `tests/test_redaction_minimal.py` | +290 (新規 4 クラス) |
| 合計 | **+678 行** (機能追加 PR のため増加方向、E3〜E6 で削減見込み) |

#### ドキュメント更新

- `README.md`: reason の例を新フォーマットに、minimal info 各フィールド説明、
  思想 2 が dotenv で実装された旨を追加。テスト件数を 518 (0.9.0) に更新
- `docs/DESIGN.md`: 設計原則 #2 を「値そのものは出さない、デバッグ情報は積極的
  に返す」に拡張。「dotenv minimal info の拡張 (0.9.0, E1 + E2)」セクション追加
- `docs/MATRIX.md`: Read handler の minimal info 説明を 0.9.0 拡張内容に更新
- `CLAUDE.local.md`: 進捗を 0.9.0 (PR 4)、ディレクトリ構成に
  `redaction/placeholders.py` 追記、plugin.json version 0.9.0、
  permissionDecisionReason フォーマット例を新出力に
- `CHANGELOG.md`: 0.9.0 エントリ追加 (本 PR の差分まとめ)
- `redaction/engine.py` / `redaction/dotenv.py` の docstring を 0.9.0 拡張
  内容に合わせて更新

#### `claude plugin validate` 結果

```
$ claude plugin validate plugins/sensitive-files-guardrail
Validating plugin manifest: .../sensitive-files-guardrail/.claude-plugin/plugin.json
Validating plugin: .../sensitive-files-guardrail/CLAUDE.local.md
⚠ Found 1 warning:
  ❯ root: CLAUDE.local.md at the plugin root is not loaded as project context.
✔ Validation passed with warnings
```

`CLAUDE.local.md` 配置の warning は plugin 設計上の既知事項 (PR 1〜3 と同じ)。

#### 完了状況サマリ (0.9.0 時点)

| Pri / カテゴリ | タスク | 状態 |
|---|---|---|
| P2 | A5 / A6 / A7 / B4 / B5 | **0.6.0 ✓** |
| P1 | A1 / A2 / A3 | **0.7.0 ✓** |
| P3 | A4 / B2 / B3 | **0.8.0 ✓** |
| P4 | E1 (dotenv value status) | **0.9.0 ✓** |
| P4 | E2 (placeholder 辞書) | **0.9.0 ✓** |
| P5 | E3 (Bash コマンド別 reason) | 未着手 (PR 5) |
| P5 | E4 (grep パターン抽出) | 未着手 (PR 5) |
| P6 | E5 (json/toml/yaml status) | 未着手 (PR 6) |
| P6 | E6 (Edit/Write リッチ化) | 未着手 (PR 6) |
| P6 | D1 / D2 (docs / tests 整理) | 未着手 (PR 6) |

### 2026-05-07: PR 5 完了 (0.10.0 リリース) — E3 / E4

思想 2 (block 時は意図を汲んだメッセージを返す) の応用層として、Bash deny を
**first_token カテゴリ別 dispatch** + **operand path の dotenv 実 read による
Read 同等 minimal info 埋込** + **grep family の env-var 名 pattern 抽出 + 該当
キー詳細表示** に拡張する **機能追加リリース**。`bash_deny` シグネチャは positional
2 引数 (`first_token`, `operand`) は互換維持しつつ keyword 引数 4 つを追加した。
**deny 動作の判定境界は変化なし**で、reason 文字列の情報量と分類だけが拡張された。

#### 論点 L1〜L3 のユーザー確定回答

- L1: **カテゴリ別 dispatcher** を採用。first_token を 9 カテゴリ + `generic` に
  マッピング (`_BASH_DENY_CATEGORY` / `_BASH_DENY_BUILDERS`)。「同じ意図 = 同じ
  文言」が機械的に保証され、保守負荷が低い。
- L2 + L3 (統合): Bash deny 時に operand path の dotenv を **実 read して Read
  同等 minimal info を返す** 方針を採用。`redaction/file_render.py` を新設して
  Read handler 同等の流れ (normalize → classify → open_regular → redact_dotenv /
  redact) を共通化。失敗時 (file 不在 / parse 失敗 / open 失敗 / NUL byte 等)
  は generic reason に静かに降りる。

ユーザー強調 (PR 4 から継続): 「うっかりミスによる流出を防ぎつつ開発作業を停滞
させないための機構が最優先」→ Bash 側でも情報量は積極的に返す。実値そのものは
引き続き出さない。

#### 実装

- **E3: Bash deny の category 別 dispatcher を新設** (`core/messages.py`)
  - `bash_deny(first_token, operand, *, command="", file_render="", dotenv_info=None,
    grep_keys=None)` シグネチャに拡張。positional 2 引数は互換維持。
  - 9 category builder (`_bash_deny_read_full` / `_read_partial` / `_search` /
    `_mutate` / `_load` / `_move` / `_history` / `_transfer` / `_archive` +
    `_generic`) を実装。
  - 共通 helper: `_common_meta_lines` / `_append_minimal_info` /
    `_extract_head_tail_n` / `_format_dotenv_key_line` / `_suggestion_other_keys`。
  - `_basename_of` を VCS-aware に拡張 (``HEAD:.env`` / ``user@host:.env`` で
    ``:`` 後尾の最終要素を抽出)。
- **E4: grep family の pattern 抽出を新設** (`handlers/bash/grep_extract.py`)
  - `extract_grep_keys(tokens) -> list[str]` を export。
  - 抽出ルール: env-var 形式 (``[A-Z][A-Z0-9_]{2,}``) を `re.finditer` で全 token
    から拾う。``-e PATTERN`` / ``-E PATTERN`` / ``-G PATTERN`` / ``--regex=...`` /
    ``--pattern=...`` 形式に対応。``--`` 以降は positional 扱いで pattern 抽出
    停止。short option (``-i`` 等) は skip。
  - `is_grep_command(first_token)` で grep family 判定 (``grep`` / ``rg`` /
    ``ag`` / ``ack`` / ``egrep`` / ``fgrep``)。
- **shared helper: `redaction/file_render.py` を新設**
  - `render_for_bash(operand, cwd) -> tuple[str | None, dict | None]` を export。
    Read handler と同じ流れ (normalize → classify → open_regular → redact /
    redact_dotenv) を operand path から走らせる。
  - 失敗 (空 operand / normalize 失敗 / non-regular / open 失敗 / redact 例外
    / NUL byte の lstat ValueError 等) は ``(None, None)`` に倒し、Bash 側 deny
    は generic reason に降りる。
  - dotenv の場合は info dict も返す (E4 で `keys[]` を grep_keys と照合する用途)。
- **handlers/bash_handler.py の deny 経路 enrichment**
  - `_build_deny_response(tokens, operand, envelope)` を新設。glob 一致 deny /
    literal 一致 deny の 2 経路で `render_for_bash` 呼出と `extract_grep_keys`
    呼出 (grep family 限定) を共通化し、`bash_deny` に新 keyword 引数を渡す。
  - `_analyze_segment` の `output.make_deny(M.bash_deny(first_token, operand))`
    呼出 2 箇所を `_build_deny_response(tokens, operand, envelope)` に置換。

#### テスト結果

- 累計 545 → **605 件 OK** (redact 518→578 件、+60 件 / check 27 件維持)
- 新規:
  - `tests/test_bash_reason_templates.py` (31 件、9 category × 2-3 ケース +
    backwards compat 2 件)
  - `tests/test_grep_extraction.py` (18 件、`is_grep_command` 2 件 +
    `extract_grep_keys` 16 件)
  - `tests/test_file_render.py` (11 件、dotenv / .envrc / abs path / json /
    toml / yaml fallback / empty operand / missing / symlink / directory /
    NUL byte normalize failure)
- 既存テストはすべて維持 (`bash_deny(first_token, operand)` の 2 引数呼び出しが
  互換のため、`TestBashDeny` / `TestDenyPlainText` / `TestVocabularyConsistency`
  / `test_e2e.py` 系は無改修)

#### コード追加

| ファイル | 行数差 |
|---|---|
| `redaction/file_render.py` | +90 (新規) |
| `handlers/bash/grep_extract.py` | +95 (新規) |
| `core/messages.py` | +330 (dispatcher + 9 builder + helper 群) |
| `handlers/bash_handler.py` | +35 (`_build_deny_response` + import) |
| `tests/test_bash_reason_templates.py` | +330 (新規) |
| `tests/test_grep_extraction.py` | +110 (新規) |
| `tests/test_file_render.py` | +130 (新規) |
| 合計 | **+1120 行** (機能追加 PR のため増加方向、E5/E6 + D1/D2 で削減見込み) |

#### ドキュメント更新

- `CHANGELOG.md`: 0.10.0 エントリ追加 (本 PR の差分まとめ)。
- `CLAUDE.local.md`: 「進行中のレビュー」進捗を 0.10.0 (PR 5) で更新、
  ディレクトリ構成に `redaction/file_render.py` / `handlers/bash/grep_extract.py`
  を追記、`permissionDecisionReason` フォーマット例に Bash deny の新形式を追加。
- `README.md`: 思想 2 が Bash 側でも実装された旨を強調、テスト件数を 578
  (0.10.0) に更新。
- `docs/DESIGN.md`: 「dotenv minimal info の拡張 (0.9.0)」セクションに「Bash
  deny の category 別 reason (0.10.0, E3 + E4)」を追加。
- `core/messages.py` 等 docstring を 0.10.0 拡張に合わせて更新。

#### `claude plugin validate` 結果

```
$ claude plugin validate plugins/sensitive-files-guardrail
Validating plugin manifest: .../sensitive-files-guardrail/.claude-plugin/plugin.json
Validating plugin: .../sensitive-files-guardrail/CLAUDE.local.md
⚠ Found 1 warning:
  ❯ root: CLAUDE.local.md at the plugin root is not loaded as project context.
✔ Validation passed with warnings
```

`CLAUDE.local.md` 配置の warning は plugin 設計上の既知事項 (PR 1〜4 と同じ)。

#### 完了状況サマリ (0.10.0 時点)

| Pri / カテゴリ | タスク | 状態 |
|---|---|---|
| P2 | A5 / A6 / A7 / B4 / B5 | **0.6.0 ✓** |
| P1 | A1 / A2 / A3 | **0.7.0 ✓** |
| P3 | A4 / B2 / B3 | **0.8.0 ✓** |
| P4 | E1 / E2 | **0.9.0 ✓** |
| P5 | E3 (Bash コマンド別 reason) | **0.10.0 ✓** |
| P5 | E4 (grep パターン抽出) | **0.10.0 ✓** |
| P6 | E5 (json/toml/yaml status) | 未着手 (PR 6) |
| P6 | E6 (Edit/Write リッチ化) | 未着手 (PR 6) |
| P6 | D1 / D2 (docs / tests 整理) | 未着手 (PR 6) |

---

### 2026-05-08: PR 5.5 完了 (0.11.0 リリース) — F1

思想 1 整合の細粒度化。0.10.0 までの「全体 hard-stop early return」を
segment 単位再評価に細粒度化し、`cat .env | sed 's/(=)/X/'` のような
複合で autonomous モードで `cat .env*` 系が素通りしていた最大の穴を塞ぐ。

#### 実装

- **F1**: `bash_handler.py::handle()` の全体 hard-stop early return を撤廃。
  `_split_command_on_operators` で分割した各 segment ごとに `_has_hard_stop`
  を再判定し、hard-stop / shlex 失敗の segment は `pending_ask` に格納して
  continue (即 return しない)。deny 確定 segment があれば短絡
  (`ls .env || cat $X || echo done`)、後段で literal match すれば
  pending_ask を超えて deny (`cat $X | ls .env | head`)。
- 攻撃シナリオ `cat <(echo \(\)) < .env` は全 segment hard-stop で挙動不変
  (思想 1: うっかり露出予防、敵対的防御は非目的)。

#### テスト結果

- 累計 605 → **619 件 OK** (redact 578→592 件 / check 27 件維持)
- 新規: `tests/test_bash_handler.py::TestSegmentHardStopReevaluate` 14 件
  - 核心: ユーザー報告ケース 3 mode (default / auto / bypassPermissions)
  - 短絡: `cat .env && echo $HOME` / `ls .env || cat $X || echo done` /
    `cat .env | grep '(=)'` / `cat $X | ls .env | head`
  - 挙動継続: `(cat .env)` / `echo "secret=$(cat .env)"` /
    `cat <(echo \(\)) < .env` (default ask + auto allow) /
    `cat $X || cat $Y` / `sed 's/(=)/X/' .env`
  - reason 文確認: minimal info に `first_token: cat` / `.env.local` /
    `DATABASE_URL` が埋まる (E3/E4 dispatch との整合)

#### コード追加

| ファイル | 行数差 |
|---|---|
| `handlers/bash_handler.py` | 約 +18 / -8 (実質 +10) |
| `handlers/bash/segmentation.py` | 約 +7 (docstring のみ) |
| `tests/test_bash_handler.py` | 約 +130 (新規 class) |
| 合計 | **約 +147 行** |

#### ドキュメント更新

- `docs/REVIEW_TASKS_2026-05-06.md`: 実装順序表に PR 5.5 / 0.11.0 行追加、
  `## F (Fix / 細粒度化)` セクション新設、本進捗エントリ + 完了状況サマリ
  (0.11.0 時点) 追加。
- `docs/DESIGN.md`: Bash handler の対応文法範囲 / 既知制限を 0.11.0
  segment 単位再評価で更新。
- `docs/MATRIX.md`: 機密確定 match 表に複合コマンド deny 例追加、静的解析
  不能表で「全 segment 含む場合のみ ask」を明記。
- `CLAUDE.local.md`: 進捗に 0.11.0 (PR 5.5, F1) 追加、Bash handler 判定
  フロー mermaid を per-segment ループに書き換え。
- `CHANGELOG.md`: 0.11.0 エントリ追加。
- `.claude-plugin/plugin.json`: version `"0.10.0"` → `"0.11.0"`。

#### `claude plugin validate` 結果

```
$ claude plugin validate plugins/sensitive-files-guardrail
Validating plugin manifest: .../sensitive-files-guardrail/.claude-plugin/plugin.json
Validating plugin: .../sensitive-files-guardrail/CLAUDE.local.md
⚠ Found 1 warning:
  ❯ root: CLAUDE.local.md at the plugin root is not loaded as project context.
✔ Validation passed with warnings
```

`CLAUDE.local.md` 配置の warning は plugin 設計上の既知事項 (PR 1〜5 と同じ)。

#### 完了状況サマリ (0.11.0 時点)

| Pri / カテゴリ | タスク | 状態 |
|---|---|---|
| P2 | A5 / A6 / A7 / B4 / B5 | **0.6.0 ✓** |
| P1 | A1 / A2 / A3 | **0.7.0 ✓** |
| P3 | A4 / B2 / B3 | **0.8.0 ✓** |
| P4 | E1 / E2 | **0.9.0 ✓** |
| P5 | E3 (Bash コマンド別 reason) | **0.10.0 ✓** |
| P5 | E4 (grep パターン抽出) | **0.10.0 ✓** |
| P1 | F1 (hard-stop の segment 単位再評価) | **0.11.0 ✓** |
| P6 | E5 (json/toml/yaml status) | 未着手 (PR 6) |
| P6 | E6 (Edit/Write リッチ化) | 未着手 (PR 6) |
| P6 | D1 / D2 (docs / tests 整理) | 未着手 (PR 6) |

### 2026-05-13: PR 5.6 完了 (0.12.0 リリース) — F2

ユーザー体感の調査用ワンライナーが ask に倒れる問題を解消する追加リリース。
ログ実測 (約 1 日分) で `bash_classify` の ask 発火の **約 80%** が
`segment_residual_metachar_lenient` 起因だったため、副作用なしの read-only
コマンドが第一トークンの segment に限り、`residual_metachar` の ask 経路を
スキップして operand scan に直行する判定を追加。

#### 実装

- **F2**: `handlers/bash/constants.py` に `_SAFE_READ_FIRST_TOKENS` を新設
  (`ls cat head tail nl tac bat less more view wc file stat du df tree grep
  egrep fgrep rg ag ack od xxd hexdump` の 24 個)。`handlers/bash_handler.py::
  _analyze_segment` で第一トークンが該当する場合のみ `_segment_has_residual_metachar`
  の ask 経路をスキップ。allow-list ヒット時は `bash_classify` ログに
  `safe_read_allowlist:<first>` を残す。
- `awk` / `sed` / `find` / `xargs` / `echo` は副作用持つ可能性のため allow-list **外**
  (`-i` / `-delete` / `-exec` / script 内 redirect の静的判別が複雑なため)。
- 機密 redirect target (`grep foo > .env`) は operand scan で deny 固定、
  hard-stop (`$(...)` / `<` / heredoc) は引き続き ask 維持。

#### テスト結果

- 累計 619 → **640 件 OK** (redact 592→613 件 / check 27 件維持)
- 新規: `tests/test_bash_handler.py::TestSafeReadAllowlist` 21 件
  - allow: `grep foo > /tmp/out` / `ls > listing` / `cat > /tmp/x` /
    `head -5 > /tmp/x` / `wc -l > /tmp/count` / `file > /tmp/x` /
    `stat > /tmp/x` / `grep ... >> /tmp/out` / pipe + redirect / `&` background
  - deny: 機密 redirect target (`grep foo > .env`) / 機密 operand
    (`grep SECRET .env > out.txt`)
  - ask 維持: `grep foo < .env` / `grep foo $(find . -name x)`
  - allow-list 外 ask 維持: `awk '{print}' > out` / `sed s/x/y > out` /
    `find . -name '*.py' > files.txt` / `echo foo > out.txt`
  - 複合: `grep foo | wc -l > count` allow、`grep ... && cat .env` deny

#### コード追加

| ファイル | 行数差 |
|---|---|
| `handlers/bash/constants.py` | 約 +32 (allow-list 定義 + 詳細コメント) |
| `handlers/bash_handler.py` | 約 +14 / -3 (実質 +11) |
| `tests/test_bash_handler.py` | 約 +130 (新規 class) |
| 合計 | **約 +180 行** |

#### ドキュメント更新

- `docs/REVIEW_TASKS_2026-05-06.md`: 実装順序表に PR 5.6 / 0.12.0 行追加、
  `## F` に F2 タスク定義、本進捗エントリ + 完了状況サマリ (0.12.0 時点) 追加。
- `docs/DESIGN.md`: Bash handler の対応文法範囲に read-only allow-list 説明追加。
- `docs/MATRIX.md`: 「Bash handler — read-only first_token allow-list」表を新設、
  既存「静的解析不能」表から `cat foo >> bar.txt` のような cat redirect 例を
  allow 側に移動。
- `README.md`: `PreToolUse(Bash)` セクションに 0.12.0 allow-list の段落を追加。
- `CHANGELOG.md`: 0.12.0 エントリ追加。
- `.claude-plugin/plugin.json`: version `"0.11.0"` → `"0.12.0"`。

#### `claude plugin validate` 結果

(PR 1〜5.5 と同じく `CLAUDE.local.md` warning は plugin 設計上の既知事項。
本リリース時に再実行する。)

#### 完了状況サマリ (0.12.0 時点)

| Pri / カテゴリ | タスク | 状態 |
|---|---|---|
| P2 | A5 / A6 / A7 / B4 / B5 | **0.6.0 ✓** |
| P1 | A1 / A2 / A3 | **0.7.0 ✓** |
| P3 | A4 / B2 / B3 | **0.8.0 ✓** |
| P4 | E1 / E2 | **0.9.0 ✓** |
| P5 | E3 (Bash コマンド別 reason) | **0.10.0 ✓** |
| P5 | E4 (grep パターン抽出) | **0.10.0 ✓** |
| P1 | F1 (hard-stop の segment 単位再評価) | **0.11.0 ✓** |
| P2 | F2 (read-only first_token allow-list) | **0.12.0 ✓** |
| P6 | E5 (json/toml/yaml status) | 未着手 (PR 6) |
| P6 | E6 (Edit/Write リッチ化) | 未着手 (PR 6) |
| P6 | D1 / D2 (docs / tests 整理) | 未着手 (PR 6) |

### 2026-06-11: 離脱対応 (0.14.0 リリース) — G1 / G2 / G3

本レビューサイクル (PR 6 = E5/E6 + D1/D2) のスコープ外で、**離脱分析に基づく
false positive 解消リリース**として独立実施。0.12.0 / 0.13.0 と同系統の
「ユーザー体感修正」枠。

#### 背景 (離脱の事実)

- **2026-05-21 にユーザー自身が `~/.claude/settings.json` の enabledPlugins で
  本 plugin を無効化** (`~/.claude` git log commit f1d6b0b で確認)。worktools
  の他 4 plugin は有効のまま = 本 plugin 単独の離脱
- transcript 全件スキャン (2026-05-12〜05-21) で実 deny 15 件を特定。**100% が
  `*.local.json` パターン起因の false positive** (settings.local.json ×11 /
  accounts.local.json ×4)、true positive 0 件
- 離脱 2 日前 (05-19) に deny 8 件が集中、agent team セッションでは block 起因の
  人間エスカレーションが 2 回発生
- `patterns.local.txt` の escape hatch は一度も使われずに離脱

#### 実装 (G タスク)

- **G1**: `*.local.json` / `*.local.yaml` / `*.local.yml` / `*.local.toml` を
  既定 patterns.txt から撤去 (patterns.local.txt での復活レシピを
  docs/PATTERNS.md に記載)
- **G2**: `_METADATA_ONLY_FIRST_TOKENS` (ls / find / tree / stat / file / du /
  df / test / wc / basename / dirname / realpath / readlink / echo / printf) +
  `_GIT_METADATA_SUBCOMMANDS` (check-ignore / ls-files / status) を新設。
  内容を stdout に出さないコマンドは機密 operand でも operand scan をスキップ
  して allow
- **G3**: `_exclude_hint` に「ユーザーの承認を得た上で」「承認なしに自分で
  追加しないこと」を明記 (autonomous Claude の self-bypass 抑止)

#### テスト結果

- 累計 640 → **673 件 OK** (redact 613→646 / check 27 維持)
- 新規: `TestMetadataOnlyAllow` 20 件。改修: `test_matcher.py` DEFAULT_RULES /
  `test_local_config_not_sensitive`、`TestSegmentHardStopReevaluate` の
  `ls .env` → `head .env` 差替 (0.11.0 の検証意図維持)

#### 完了状況サマリ (0.14.0 時点)

| Pri / カテゴリ | タスク | 状態 |
|---|---|---|
| P2 | A5 / A6 / A7 / B4 / B5 | **0.6.0 ✓** |
| P1 | A1 / A2 / A3 | **0.7.0 ✓** |
| P3 | A4 / B2 / B3 | **0.8.0 ✓** |
| P4 | E1 / E2 | **0.9.0 ✓** |
| P5 | E3 / E4 | **0.10.0 ✓** |
| P1 | F1 (hard-stop の segment 単位再評価) | **0.11.0 ✓** |
| P2 | F2 (read-only first_token allow-list) | **0.12.0 ✓** |
| — | plan mode LENIENT 差し戻し (HOTFIX) | **0.13.0 ✓** |
| P1 | G1 / G2 / G3 (離脱分析対応) | **0.14.0 ✓** |
| P6 | E5 (json/toml/yaml status) | 未着手 (PR 6) |
| P6 | E6 (Edit/Write リッチ化) | 未着手 (PR 6) |
| P6 | D1 / D2 (docs / tests 整理) | 未着手 (PR 6) |

#### v1.0.0 (PR 6) での追加検討事項 (2026-06-12 追記)

- **plugin rename の最終判断**: 思想 (「うっかり予防のついでに少し守れれば
  十分」) に対して `sensitive-files-guardrail` が過剰名称かをユーザーと議論した
  結果、**名前は維持** (guard は保証語ではなく役割語。deny reason を読む
  モデルへの遵守圧として強めの名前が機能する)、**人間向けの期待値調整は
  description / README が担う**で決着 (0.14.0 で description に思想一行を
  追加済み)。ただし**もし rename するなら breaking change を許容できる
  v1.0.0 のタイミング**とユーザーが言及。PR 6 着手時に rename 要否を最終
  確認すること (候補: `sensitive-files-guardrailrail`。波及先: enabledPlugins
  キー / marketplace entry / 設定 dir `~/.claude/sensitive-files-guardrail/`)

---

## 新規セッションでこのファイルを開いた時の手順

1. 本ファイル `docs/REVIEW_TASKS_2026-05-06.md` を読む
2. 関連の読み込み順 (重要):
   - `README.md` — 現状の挙動概要
   - `CLAUDE.local.md` (個人メモ) — 思想と移設経緯
   - `CHANGELOG.md` — 0.5.0 までの実装経緯
   - `docs/DESIGN.md` — Phase 0 実測ログ (削除予定だが現時点では残存)
   - `docs/MATRIX.md` — 判定マトリクス
   - 該当 handler ファイル
3. **「## 進捗」を確認** して着手済み PR を把握。重複作業を避ける
4. **「## 実装順序」の表を尊重** して PR 1 から順に行う。順序を飛ばしたくなったら
   依存関係を再確認する
5. テストを必ず通す: 各 hook ディレクトリで `python3 -m unittest discover tests`
6. PR 完了したら `## 進捗` に追記し、`CHANGELOG.md` と `plugin.json` の version
   を bump
7. 削除タスク完了時は `docs/MATRIX.md` の該当行も更新する (D1 で最終的に削除予定だが、
   それまでは整合性維持)

## 関連メモリ

- `~/.claude/projects/-Users-mao-dev-personal-cc-marketplaces-worktools/memory/project_sfg_review_2026_05_06.md`
  にこのファイルへのポインタを保存済み
