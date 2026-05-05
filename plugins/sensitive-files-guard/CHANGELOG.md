# Changelog

## 0.8.0

**思想ベース再評価レビュー (`docs/REVIEW_TASKS_2026-05-06.md`) PR 3 を消化**:
思想 1 (うっかり露出予防、敵対的防御は非目的) 仕上げの 3 件 (A4 / B2 / B3) を
一体で撤廃。prefix normalize / 鍵名 prompt-injection 文言除去 / 既定 rules
候補列挙を縮約し、autonomous で素通りするケースを増やすことで「うっかり書かない
形」(``FOO=1 cat .env`` / ``cat *.key`` / 鍵名 ``system:foo``) への過剰対応を
取り払った。

### 主要な変更

1. **A4: prefix normalize 撤廃**
   — `bash_handler._normalize_segment_prefix`、`bash/operand_lexer._is_absolute_or_relative_path_exec`、
   `bash/constants._TRANSPARENT_COMMANDS` を撤廃し、`_analyze_segment` に
   `_is_opaque_first_token` の inline 判定を新設。第一トークンが env-assignment
   (``FOO=1``) / `env` / `command` / `builtin` / `nohup` / opaque wrapper
   (`bash` / `sudo` / `eval` / `time` / ...) / 任意 path exec (``/bin/cat`` /
   ``./script``) のいずれかなら `ask_or_allow("opaque_prefix")` に倒す。
   `_OPAQUE_WRAPPERS` には `env` / `command` / `builtin` / `nohup` を統合。
   0.3.2〜0.7.x で行っていた「``FOO=1 cat .env`` を ``cat .env`` と解釈する
   prefix 透過」は撤廃。
2. **B2: `_INJECTION_PATTERNS` を `</?DATA` のみに縮小**
   — `redaction/sanitize._INJECTION_PATTERNS` を case-insensitive な
   `</?\\s*DATA` 1 行に縮小。`ignore previous` / `ignore all` / `system:` /
   `assistant:` / `</?system|</?user|</?assistant` の prompt-injection 文言を
   鍵名・basename から `[?]` 置換するロジックを撤廃 (思想 1 外)。制御文字除去 +
   `MAX_KEY_LEN=128` / `MAX_BASENAME_LEN=128` の長さ切り詰めは維持。
   Read 側 ``<DATA untrusted="true">`` 外殻の破壊防止に必要な DATA タグ衝突
   検出は引き続き残す。
3. **B3: glob 候補列挙を `_glob_operand_is_dotenv_match` に縮約**
   — `bash_handler._glob_operand_is_sensitive`、`bash/operand_lexer._literalize`
   / `_glob_candidates` を撤廃し、`_glob_operand_is_dotenv_match(operand)`
   (operand_lexer.py) を新設。``fnmatchcase(stem, op_glob)`` で stem ∈
   `(.env, .envrc)` の literal 一致を判定。`SFG_CASE_SENSITIVE` 環境変数を
   尊重 (未設定時は lower 比較)。`_analyze_segment` の glob 分岐は新関数で
   True なら deny 固定、False なら `ask_or_allow` に倒す形に変更。0.3.2〜0.7.x
   の既定 rules 候補列挙 (`cat *.key` / `cat id_rsa*` / `cat cred*.json` を
   交差で deny) は撤廃。

### 挙動変更 (0.7.0 → 0.8.0)

| ケース | 0.7.0 | 0.8.0 |
|---|---|---|
| `FOO=1 cat .env` / `FOO=1 BAR=2 cat .env` | deny | ask_or_allow (default=ask, auto/bypass=allow) |
| `env cat .env` / `env FOO=1 cat .env` | deny | ask_or_allow |
| `command cat .env` / `builtin cat .env` / `nohup cat .env` | deny | ask_or_allow |
| `nohup command cat .env` / `command env FOO=1 cat .env` | deny | ask_or_allow |
| `/usr/bin/env FOO=1 cat .env` / `/bin/command cat .env` | deny | ask_or_allow |
| `cat .env` / `grep X .env` / `cp .env y` / `git show HEAD:.env` | deny | deny (維持) |
| `cat .env*` (`fnmatchcase(".env", ".env*")=True`) | deny | deny (維持) |
| `cat *.envrc` / `cat .envrc*` (`.envrc` literal 一致) | deny | deny (維持) |
| `cat .e[n]v` / `cat .en?` / `cat [.]env` (`.env` literal 一致) | deny | deny (維持) |
| `cat .env.*` (`.env` literal に fnmatch しない) | deny | ask_or_allow |
| `cat id_rsa*` / `cat id_*` / `cat *.key` / `cat cred*.json` | deny | ask_or_allow |
| `cat .env.example` (literal、exclude rule) | allow | allow (維持) |
| `cat .env.example*` | allow (exclude 決着) | ask_or_allow |
| `cat *.log` | allow (rules 非交差) | ask_or_allow (default=ask, auto/bypass=allow) |
| 鍵名 `IGNORE PREVIOUS instructions` | `[?]` | そのまま (制御文字除去 + 長さ切り詰めのみ) |
| 鍵名 `system:do_x` / `assistant:foo` | `[?]` | そのまま |
| 鍵名 / basename `</DATA>` / `<DATA` | `[?]` | `[?]` (維持) |

### 内部 API の変更 (caller があれば追従が必要)

- `handlers.bash_handler` から削除: `_normalize_segment_prefix`,
  `_glob_operand_is_sensitive` (test seam)。
- `handlers.bash.operand_lexer` から削除: `_literalize`, `_glob_candidates`,
  `_is_absolute_or_relative_path_exec`。
- `handlers.bash.constants` から削除: `_TRANSPARENT_COMMANDS`。
  `_OPAQUE_WRAPPERS` に `env` / `command` / `builtin` / `nohup` を統合。
  `_ENV_PREFIX_RE` は残し (`_is_opaque_first_token` で 1 回だけ使う)。
- `handlers.bash.operand_lexer` に新設: `_glob_operand_is_dotenv_match(operand)`、
  および定数 `_DOTENV_GLOB_STEMS = (".env", ".envrc")`。
- `handlers.bash_handler` に新設: `_is_opaque_first_token(token)` (内部 helper)。
- `redaction.sanitize._INJECTION_PATTERNS` の regex 内容変更:
  `(ignore\\s+previous|...|</?DATA|</?system|...)` → `</?\\s*DATA`。
- 観測ログ追加: `bash_classify:glob_uncertain_lenient` (glob で dotenv stem と
  一致しなかったとき)。

### テスト

- 削除:
  - `tests/test_prefix_normalize.py` (24 ケース、`_normalize_segment_prefix`
    直接 import)
  - `tests/test_glob_candidates.py` (25 ケース、`_glob_candidates` /
    `_glob_operand_is_sensitive` / `_literalize` 直接 import)
  - `tests/test_sanitize.py::TestSanitizeKey.test_injection_ignore` /
    `test_injection_system` (3 ケース、prompt 文言系の `[?]` 置換アサート)
  - `tests/test_sanitize.py::TestSanitizeBasename.test_injection`
- 改修:
  - `tests/test_bash_handler.py::TestPrefixStrippingDeny` を
    `TestOpaquePrefixAskOrAllow` (12 ケース、deny → ask/allow) に書き換え
  - `tests/test_bash_handler.py::TestPrefixStrippingOpaque` を
    `TestPrefixWithOptionsOpaque` (8 ケース、内容維持) に rename
  - `tests/test_bash_handler.py::TestWrapperBypass.test_nohup_cat` を
    `_default` / `_auto` 2 ケースに分割し ask/allow に書き換え
  - `tests/test_bash_handler.py::TestGlobMatch` (15 ケース) を
    `TestGlobDotenvDeny` (6 ケース、deny 維持: ``.env*`` / ``*.envrc`` /
    ``.envrc*`` / ``.e[n]v`` / ``.en?`` / ``[.]env``) +
    `TestGlobUncertainAskOrAllow` (12 ケース、ask 系: ``.env.*`` / ``id_rsa*`` /
    ``id_*`` / ``*.key`` / ``cred*.json`` / ``*.log`` / ``.env.example*``) +
    `TestGlobLiteralExcludeAllow` (2 ケース、literal exclude 維持) に再編
  - `tests/test_e2e.py::test_bash_auto_env_prefix_dotenv_denies` →
    `_allows` に書き換え (auto モードで allow)
  - `tests/test_e2e.py::test_bash_auto_abs_env_basename_denies` →
    `_allows` に書き換え
  - `tests/test_sanitize.py::TestSanitizeKey` に
    `test_injection_data_tag_open` / `test_prompt_text_passthrough` を新設、
    `TestSanitizeBasename` に `test_data_tag_collision` /
    `test_prompt_text_passthrough` を新設
- 新規:
  - `tests/test_glob_dotenv.py` (18 ケース、`_glob_operand_is_dotenv_match`
    の単体テスト: dotenv 一致 / 非一致 / edge case / case sensitivity)
- 累計 495 → **465 件 OK** (redact 468→438 件 / check 27 件維持)

### コード削減

| ファイル | 行数差 |
|---|---|
| `handlers/bash_handler.py` | -75 (`_normalize_segment_prefix` 60 行 + `_glob_operand_is_sensitive` 14 行 + re-export) |
| `handlers/bash/operand_lexer.py` | -50 (`_literalize` / `_glob_candidates` / `_is_absolute_or_relative_path_exec` 削除、`_glob_operand_is_dotenv_match` + `_DOTENV_GLOB_STEMS` 新設で打ち消し) |
| `handlers/bash/constants.py` | -3 (`_TRANSPARENT_COMMANDS` 削除、`_OPAQUE_WRAPPERS` に 4 値統合) |
| `redaction/sanitize.py` | -3 (`_INJECTION_PATTERNS` の regex 縮小) |
| 合計 | **-131 行** (見込み ≈158 行を若干下回り、新設 helper の docstring が想定より厚かったため) |

### ドキュメント

- `docs/MATRIX.md`: Bash deny 表から prefix 系 (FOO=1 / env / command /
  builtin / nohup / /usr/bin/env / /bin/command) と glob 系 (`*.key` /
  `id_rsa*` / `cred*.json`) を削除し、ask_or_allow 表に追加。glob 系で deny
  維持された行 (`.env*` / `*.envrc` / `.e[n]v` / `.en?` / `[.]env`) を整理。
  PR 3 撤廃 note を追加。
- `docs/DESIGN.md`: 「Bash handler の対応文法範囲」表から「前置き正規化」を撤廃し、
  「対応 (deny/allow 確定できる)」に dotenv glob 一致行を追加。「対応外」表に
  「opaque first token (env-assignment / env / command / builtin / nohup /
  任意 path exec)」「dotenv stem 不一致 glob」の 2 行を追加。既知制限 #2 と #4
  を 0.8.0 に書換、責務境界の test seam テーブルを更新。`_glob_candidates`
  歴史セクションを「glob operand 判定の歴史 (0.3.2 → 0.8.0)」に書換。
- `README.md`: glob false positive note を 0.8.0 で書換、prefix normalize 撤廃
  note を追加、テスト件数を 438 (0.8.0) に更新。
- `CLAUDE.local.md`: 進行中レビュー進捗を 0.8.0 (PR 3) で更新、ディレクトリ
  構成の plugin.json version を 0.8.0、patch seam テーブルから旧 symbol を
  整理、Bash handler 判定フロー mermaid を opaque first token / dotenv glob
  match の新フローに書換。
- `core/messages.py` / `redaction/sanitize.py` / `handlers/bash_handler.py` /
  `handlers/bash/constants.py` / `handlers/bash/operand_lexer.py` の docstring
  を 0.8.0 撤廃内容に合わせて更新。

### 関連レビュー

`docs/REVIEW_TASKS_2026-05-06.md` の **A4 / B2 / B3** を消化。残るは
**E1〜E6 + D1 / D2** (PR 4-6, 0.9.0〜1.0.0)。

## 0.7.0

**思想ベース再評価レビュー (`docs/REVIEW_TASKS_2026-05-06.md`) PR 2 を消化**:
思想衝突の主役 3 件 (A1 / A2 / A3) を一体で撤廃し、`<` 入力リダイレクトの
character-level parser、RedirectForm タグ、`<SFG_DENY>` 構造化包装を削除。
deny 動作の判定境界 1 種類を `ask_or_allow` に格下げし、deny reason を plain
text に戻す **breaking-ish** リリース。`permissionDecision` の判定ロジック自体は
変更せず、deny reason 文字列の形式のみ変わる。

### 主要な変更

1. **A1: `<` 入力リダイレクト char-level parser を撤廃**
   — 0.3.4 で導入した quote-aware parser
   (`_scan_input_redirect_targets_chars` / `_scan_input_redirect_targets_with_form`
   / `_consume_redirect_target` / `_classify_redirect_form` 等) と
   `bash_handler._extract_input_redirect_targets` / `_scan_input_redirects` を
   削除。`cat <(echo \(\)) < .env` の escape paren depth tracking、
   `[[ ... ]]` 引数位置判定、process sub `<(...)` 内 word boundary、
   POSIX double-quote escape semantics など R3〜R8 系の修正は **すべて敵対的
   バイパス対策のコード負債**であり、思想 1 (うっかり露出予防が目的、敵対的防御は
   非目的) と整合しないため撤廃。`<` を含む command は他の hard-stop と同じく
   ``ask_or_allow`` (default で ask、autonomous で allow) に倒す。

2. **A2: `RedirectForm` タグ (M5, 0.5.0) を撤廃**
   — `bare` / `fd_prefixed` / `no_space` / `quoted` の 4 種を返す
   `RedirectForm` Literal、`_classify_redirect_form` 関数、`bash_deny` の
   `form` キーワード引数、SFG_DENY body の `form: <値>` 行を削除。
   A1 と一体で input redirect 系の deny 経路自体を撤廃したため、form 情報を
   返す価値が消滅した。

3. **A3: `<SFG_DENY>` 構造化包装 (M4, 0.4.2) を plain text に戻す**
   — `core/messages.py` から `_wrap_sfg_deny` / `_SFG_GUARD` /
   `BashDenyKind` / `EditDenyKind` / `bash_deny(kind=...)` /
   `bash_deny(form=...)` / `edit_deny(kind=...)` を削除。
   `redaction/sanitize.py::escape_xml_tag` も削除し、`escape_data_tag` を
   DATA タグ専用の直接実装に縮約。0.4.2 で「後段 hook が deny reason を
   機械パースできる schema」として導入したが、worktools にそうした後段 hook
   が存在せず overengineering だったため。Read 側の ``<DATA untrusted="true">``
   包装と `escape_data_tag` は維持 (鍵名が LLM コンテキストに残るため最低限の
   包装防御として意味あり)。

### 挙動変更 (0.6.0 → 0.7.0)

| ケース | 0.6.0 | 0.7.0 |
|---|---|---|
| `cat < .env` (literal target) | deny 固定 | ask (default) / allow (autonomous) |
| `cat<.env` / `cat 0< .env` / `cat < ".env"` | deny 固定 | ask / allow (同上) |
| `cat < .env*` (glob target) | deny 固定 | ask / allow (同上) |
| `< .env cat` (引数順序逆) | deny 固定 | ask / allow (同上) |
| `cat .env` (operand scan literal) | deny | deny (維持) |
| `cat .env*` (operand scan glob) | deny | deny (維持) |
| `cat $X` / `cat <(...)` 等の他 hard-stop | ask / allow | ask / allow (維持) |
| Bash deny の reason 文字列 | `<SFG_DENY tool reason guard>...</SFG_DENY>` | plain text (note / matched_operand / first_token / suggestion を改行区切り) |
| Edit/Write deny の reason 文字列 | `<SFG_DENY tool="..." reason="sensitive_path[_symlink/_special]">` | plain text + extra_note 行で symlink/special 文脈を表現 |
| `policy_unavailable("deny")` の reason | `<SFG_DENY tool="Hook" reason="policy_unavailable">` | plain text (1 段落) |
| Read 側 ``<DATA untrusted="true">`` 包装 | 維持 | 維持 (escape_data_tag も維持) |
| ask 系 reason (read_ask / edit_pause / bash_lenient / policy_unavailable("pause")) | plain text | plain text (変更なし) |

### 内部 API の変更 (caller があれば追従が必要)

- `core.messages.bash_deny`: シグネチャ
  `bash_deny(first_token, operand, kind, *, form=None)` →
  `bash_deny(first_token, operand)` に縮小。`kind` / `form` を渡している
  caller は引数を削除する必要あり。
- `core.messages.edit_deny`: `kind` キーワード引数を削除。symlink / special
  の文脈は `extra_note` のみで表現する。`edit_handler.py` 側で
  `kind="sensitive_path_symlink"` / `kind="sensitive_path_special"` を
  渡していた箇所を撤去済み。
- `core.messages` から削除: `_wrap_sfg_deny`, `_SFG_GUARD`, `BashDenyKind`,
  `EditDenyKind`。`SfgDenyReason` 概念自体を撤廃。
- `redaction.sanitize` から削除: `escape_xml_tag`。`escape_data_tag` は
  直接実装に縮約 (引数 `tag_name` を持たない)。
- `handlers.bash.redirects` から削除: `RedirectForm`,
  `_consume_redirect_target`, `_classify_redirect_form`,
  `_scan_input_redirect_targets_with_form`,
  `_scan_input_redirect_targets_chars`, `_DQ_BACKSLASH_ESCAPABLE`。
  `_strip_safe_redirects` / `_segment_has_residual_metachar` /
  `_is_safe_redirect_token` のみ残す。
- `handlers.bash_handler` から削除: `_extract_input_redirect_targets`
  (patch seam), `_scan_input_redirects` (内部関数)。`<` を含む command は
  `_has_hard_stop` 経由で `ask_or_allow` に倒る。
- 観測ログ削除: `bash_classify:input_redirect_empty_extract` /
  `input_redirect_glob_match` / `input_redirect_match`。

### テスト

- 削除:
  - `tests/test_input_redirect.py` (130 件、`_extract_input_redirect_targets`
    / `_scan_input_redirect_targets_with_form` を直接 import していた regression
    テスト)
  - `tests/test_messages.py::TestSfgDenyEnvelope` (12 件)、
    `TestBashDenyInputRedirectForm` (8 件)、
    `TestBashDenyInputRedirect` (2 件)、
    `TestBashDenyLiteral` / `TestBashDenyGlob` を `TestBashDeny` に統合 (4 件)
  - `tests/test_sanitize.py::TestEscapeXmlTag` (7 件)
  - `tests/test_bash_handler.py::TestInputRedirectFormInReason` (8 件)、
    `TestDenyReasonContent.test_input_redirect_includes_target` (1 件)、
    `TestDenyReasonContent.test_glob_match_includes_glob_operand` の "glob"
    文字列 assert (kind 撤廃で文言から消える)
- 改修:
  - `tests/test_messages.py::TestDenyPlainText` (新規 6 件) — deny 系が plain
    text で出ることを assert
  - `tests/test_messages.py::TestVocabularyConsistency.test_deny_uses_block`
    を kind 引数なしで再記述
  - `tests/test_bash_handler.py::TestInputRedirectAskOrAllow` (3 件、旧
    `TestInputRedirectDeny` を ask 系に書換) — `<` 入力リダイレクトが ask /
    allow に倒ることを regression として残す
  - `tests/test_e2e.py::test_bash_auto_input_redirect_allows` (旧 `denies`
    を allow に書換)
- 累計 630 → **468 件 OK** (redact, -162 件) + 27 件 OK (check) = 495 件

### ドキュメント

- `docs/MATRIX.md`: 「`<` target 抽出」関連 4 行を Bash deny 表から削除し、
  「Bash 静的解析不能 (三態判定)」表に「`<` 入力リダイレクト, 0.7.0 で格下げ」
  行を追加。
- `docs/DESIGN.md`: 「Bash handler の対応文法範囲」セクションから
  character-level quote-aware parser の記述を撤去 (shlex.split 一本化)。
  既知制限 #3 を「`<` 入力リダイレクトは ask_or_allow 扱い (0.7.0)」に書換。
  責務境界の `_extract_input_redirect_targets` patch seam を撤去。
- `README.md`: Bash 三態判定の説明から「`< target` の target が機密」を削除し、
  ask_or_allow 側の例として `<` 入力リダイレクトを明示。0.7.0 格下げ note を
  追加。`5 mode 列` に統一。
- `CLAUDE.local.md`: 「進行中のレビュー」進捗を 0.7.0 (PR 2) 完了で更新。
  ディレクトリ構成の plugin.json version を 0.7.0、MATRIX を 5 mode 列に。
  patch seam テーブルから `test_input_redirect.py` 行を撤去。
  Bash handler 判定フロー mermaid から hard-stop 内 input redirect 分岐を撤去。
- `core/messages.py` / `redaction/sanitize.py` / `handlers/bash/redirects.py`
  / `handlers/bash_handler.py` の docstring を 0.7.0 撤去内容に合わせて更新。

### コード削減

| ファイル | 行数差 |
|---|---|
| `handlers/bash/redirects.py` | -413 行 (475 → 62) |
| `handlers/bash_handler.py` | -69 行 (input redirect 関連の関数 + docstring) |
| `core/messages.py` | -108 行 (SFG_DENY 包装 + kind/form 引数 + 関連 docstring) |
| `redaction/sanitize.py` | -19 行 (escape_xml_tag 一般化版を撤去) |
| `handlers/edit_handler.py` | -2 行 (kind 引数渡し) |
| 合計 | **-611 行** |

(見込み 480 行を上回る撤去。R1〜R8 fix の depth tracking / [[ command-position
判定 / process sub word boundary 等のコード負債と、SFG_DENY schema docstring
が想定より嵩んでいたため)

### 関連レビュー

`docs/REVIEW_TASKS_2026-05-06.md` の **A1 / A2 / A3** を消化。残るは
**A4 / B2 / B3** (PR 3, 0.8.0)、**E1〜E6 + D1 / D2** (PR 4-6, 0.9.0〜1.0.0)。

## 0.6.0

**思想ベース再評価レビュー (`docs/REVIEW_TASKS_2026-05-06.md`) PR 1 を消化**:
リスクゼロの dead code / overengineering を 5 件まとめて撤去する non-breaking
リリース。挙動変更は「想像できる将来のための前方互換層」「現行 CLI で使われない
機能」「敵対的 race の二重防御」の 3 種に限定し、思想 1 (うっかり防御) と整合
しないコードを縮小した。

### 主要な変更

1. **A5: `LENIENT_MODES` から `"plan"` を削除** — 0.3.3 で前方互換層として
   追加していたが、Phase 0 実測 (2026-04-22) で現行 CLI では plan mode で hook が
   発火しないことが確認され dead entry だったため撤去。CLI 仕様が変わって plan
   mode で hook が発火するようになったら、`CLAUDE.md` の Runbook で再実測した
   上で再追加する。`ask_or_allow({"permission_mode": "plan"})` は default と
   同じ ask 扱いに変更。
2. **A6: MultiEdit dead handler 撤去** — `hooks.json` から既に matcher 除外済み
   だった MultiEdit 対応コードを `__main__.py` argparse choices /
   `_dispatch` 分岐 / `edit_handler._extract_dotenv_keys` の edits 連結ロジック
   から撤去。テスト 4 件削除、fixture `multiedit.json` 削除。再搭載時は README
   の手順に従って復活。
3. **A7: 2-tier lookup の fallback 削除** — `patterns.local.txt` の参照先を
   `~/.claude/sensitive-files-guard/patterns.local.txt` 単一パスに縮小。
   0.4.0〜0.5.x で fallback として参照していた
   `$XDG_CONFIG_HOME/sensitive-files-guard/patterns.local.txt` /
   `~/.config/sensitive-files-guard/patterns.local.txt` を撤去
   (0.4.0 で deprecation 通知済みの予定通り)。`_resolve_local_patterns_paths`
   (複数形) と `warn_callback("deprecated_config_dir")` 経路は削除。
4. **B4: 内部 soft-timeout (SIGALRM 1s) 撤廃** — `redaction/engine.py` の
   `_soft_timeout` / `_RedactionTimeout` / `REDACTION_SOFT_TIMEOUT` を削除。
   dotenv parse は ReDoS の経路がほぼなく、外部 hook timeout (2s) で十分。
   テスト `test_engine_timeout.py` 削除。
5. **B5: `open_regular` の `fstat` 再確認撤廃** — `O_NOFOLLOW` で symlink
   follow を防ぐ層は維持しつつ、open 後の `os.fstat` で `S_ISREG` を再確認する
   敵対的 race 対策ロジックを撤去 (思想 1 外)。

### 挙動変更 (0.5.0 → 0.6.0)

| ケース | 0.5.0 | 0.6.0 |
|---|---|---|
| Bash opaque ケース in plan mode | allow (LENIENT_MODES) | ask (default 同等) |
| `--tool multiedit` 起動 | argparse 受理して edit_handler 呼び出し | argparse error |
| `~/.config/sensitive-files-guard/patterns.local.txt` のみ存在 | fallback 採用 + deprecation 通知 | 無視 (preferred 不在として扱う) |
| 機密検出時の redaction | SIGALRM 1s で内部 timeout | 内部 timeout なし (外部 2s のみ) |
| open_regular の race-attacked symlink swap | 通常 fstat S_ISREG 再確認で拒否 | O_NOFOLLOW のみで防御 (race window あり) |
| その他 | 変更なし | 変更なし |

### 内部構造の変更

- `core/output.py::LENIENT_MODES`: `frozenset({"auto", "bypassPermissions"})`
  に縮小 (3 → 2 値)
- `__main__.py` argparse `choices`: `["read", "bash", "edit", "write"]`
  (5 → 4 値)
- `_shared/patterns.py::_resolve_local_patterns_paths` (複数形) を削除、
  `_resolve_local_patterns_path` (単数形) のみ残置
- `core/patterns.py` / `check-sensitive-files/checker.py` の
  `warn_callback` から `deprecated_config_dir` 分岐を削除
- `redaction/engine.py` から `signal` / `contextmanager` import を削除
- `core/safepath.py::open_regular` の `S_ISREG` 再確認を削除
  (`stat` import は `is_regular_directory` 等で引き続き使用)

### テスト

- 削除: `test_engine_timeout.py` (2 件), `multiedit.json` fixture, `test_envelope_shapes.py::test_multiedit_envelope`,
  `test_edit_handler.py::TestMultiEdit` + `test_multiedit_aggregates_keys`,
  `test_e2e.py::test_multiedit_dotenv_denies`,
  `test_patterns_loader.py::TestPreferredFallback2Tier` 系 + 2-tier resolve 系
- 改修: `test_output.py::test_plan_returns_ask` (旧 returns_allow から書換),
  `test_failclosed.py::test_plan_returns_ask`,
  `test_messages.py::TestBashLenient::test_hard_stop` (plan 文字列 assert 削除),
  `test_checker.py::TestLocalPatternsLoader` の write 先を preferred path に
- 累計 643 → **630 件 OK** (redact, -13 件) + 27 件 OK (check) = 657 件

### ドキュメント

- `docs/MATRIX.md`: 6 mode 列 → 5 mode 列 (plan 列削除)、全 footnote `[^plan]` 削除
- `docs/DESIGN.md`: 2026-04-22 Phase 0 ログを「0.6.0 で撤去した」記述に更新、
  LENIENT_MODES 表の plan 行を ask に修正
- `docs/PATTERNS.md`: 「2-tier lookup, 0.4.0 以降」セクションを「0.6.0 から
  単一パス」に書き換え、`_resolve_local_patterns_paths` 説明を単数形に縮小
- `README.md`: パターン設定セクションを 1-tier 化、MultiEdit note を撤去手順に書換
- `CLAUDE.local.md`: 「## 進行中のレビュー」セクション維持、2-tier lookup 説明を
  単一パスに更新、Bash handler mermaid の `auto/bypass/plan=allow` を
  `auto/bypass=allow` に修正、非目的記述から MultiEdit を撤去
- `tests/fixtures/envelopes/README.md`: MultiEdit エントリと
  `LENIENT_MODES` 説明 (3 値 → 2 値) を更新

### 利用者影響

- **旧パスからの移行が必要**: `~/.config/sensitive-files-guard/patterns.local.txt`
  または `$XDG_CONFIG_HOME/sensitive-files-guard/patterns.local.txt` を使って
  いた利用者は手動で
  `mv "${XDG_CONFIG_HOME:-$HOME/.config}/sensitive-files-guard/patterns.local.txt" ~/.claude/sensitive-files-guard/patterns.local.txt`
  を実施する必要がある。0.4.0 で deprecation 通知した予定通りの撤去。

### 関連レビュー

`docs/REVIEW_TASKS_2026-05-06.md` の **A5 / A6 / A7 / B4 / B5** を消化。
残るは **A1 / A2 / A3** (PR 2: `<` redirect parser / RedirectForm /
SFG_DENY 撤去), **A4 / B2 / B3** (PR 3), **E1〜E6** (PR 4-6: 思想 2 の意図
汲み取り強化)。

## 0.5.0

**M5 (入力リダイレクト形式タグ) 実装 + B (Bash パーサ採否) の最終決定**。
0.3.4 で導入した character-level quote-aware parser を拡張し、各 input
redirect target に **構文形式タグ** (``bare`` / ``fd_prefixed`` / ``no_space``
/ ``quoted``) を付与して SFG_DENY body に出力する。これにより後段 hook /
LLM が同じ ``reason="input_redirect"`` のなかで `cat<.env` (no_space) と
`cat 0< .env` (fd_prefixed) を機械的に区別できる。

### B (bashlex 採否) の結論: 自前 IR 化を採用

別セッション議論として懸案だった Bash パーサ採否を最終確定:

| 候補 | 結果 | 理由 |
|---|---|---|
| **bashlex** (PyPI 0.18) | **採用不可** | License は **GPLv3+** で MIT 必須の worktools と非互換 |
| **tree-sitter-bash** (PyPI 0.25.1) | **見送り** | MIT + wheel 配布完備 (cp310-cp313 全 plat) だが pip 依存追加が必要。Plugin の「pip install 不要・即使える」UX (CLAUDE.md 「標準ライブラリのみで完結」方針) を破壊する代償が大きい |
| **自前 IR 化 (採用)** | ✓ | 0.3.4 の char-level parser をそのまま拡張し、target 抽出と同時に form 情報を返す形に。依存ゼロ維持、M5 限定の最小工数 (~150 行 + テスト 36 件) |

長期的な bash 文法サポート増のコストは引き続き手書きで吸収する方針を継続。
bashlex のライセンスが将来 MIT 互換に変わったり、tree-sitter-bash の wheel
を vendor 同梱できる手段が固まれば再評価の余地あり。

### 主要な変更

1. **`RedirectForm` Literal を新設** (`handlers/bash/redirects.py`):

   ```python
   RedirectForm = Literal["bare", "fd_prefixed", "no_space", "quoted"]
   ```

   優先順位 (target 1 つにつき 1 種): ``fd_prefixed`` > ``no_space`` >
   ``quoted`` > ``bare``。

   | 例 | form |
   |---|---|
   | `cat < .env` | ``bare`` |
   | `cat <.env` | ``no_space`` |
   | `cat 0< .env` / `cat 0<.env` | ``fd_prefixed`` |
   | `cat < ".env"` / `cat <".env"` | ``quoted`` |
   | `cat 0< ".env"` | ``fd_prefixed`` (fd 優先) |
   | `echo abc0<.env` | ``no_space`` (word 内部 digit は fd と区別) |

2. **`_scan_input_redirect_targets_with_form` 新設** — 戻り値
   `list[tuple[str, RedirectForm]]` で target と form を一緒に返す。本体
   ロジックは 0.3.4 の `_scan_input_redirect_targets_chars` と同じだが、単独
   ``<`` 検出位置で `_classify_redirect_form` ヘルパーを呼んで form を判定。

3. **`_scan_input_redirect_targets_chars` を thin wrapper 化** — 戻り値型
   `list[str]` は維持。74 件の戻り値型 assert テスト (`TestExtractInput
   RedirectTargets*` 9 クラス) を **無改修** で後方互換維持する。新規実装は
   form 付き版を直接呼ぶ。

4. **`bash_deny` に `form` キーワード引数追加** (`core/messages.py`):

   ```python
   def bash_deny(
       first_token: str,
       operand: str,
       kind: BashDenyKind,
       *,
       form: RedirectForm | None = None,
   ) -> str: ...
   ```

   `input_redirect` / `input_redirect_glob` で caller が form を指定すると
   SFG_DENY body の ``matched_operand:`` / ``first_token:`` の後、``suggestion:``
   の前に ``form: <値>`` 行を追加。`literal` / `glob` (operand scan) は
   default None で出力しない。

5. **`bash_handler._scan_input_redirects` を form 対応に置換** — 内部実装
   から `_scan_input_redirect_targets_with_form` を直接呼び、deny 時に form
   を `bash_deny` に渡す。test seam の `_extract_input_redirect_targets`
   (戻り値 `list[str]`) は不変。

### `<SFG_DENY>` schema 拡張

```
<SFG_DENY tool="Bash" reason="input_redirect" guard="sfg-v1">
note: ...
matched_operand: .env
form: fd_prefixed                ← M5 で追加 (input_redirect 系のみ)
suggestion: ...
</SFG_DENY>
```

reason 値 (8 種) は変えていない (M4 schema との互換維持)。後段 hook の
パターンマッチを破壊せず、追加情報のみが増える形。

### テスト

- **新設** `tests/test_input_redirect.py::TestExtractInputRedirectTargetsWithForm`
  — 19 件。form 4 種 + 優先順位 + word 内 digit 除外 + segment 境界 + thin
  wrapper との組合せ
- **新設** `tests/test_input_redirect.py::TestExtractInputRedirectTargetsCharsThinWrapper`
  — 1 件。`_scan_input_redirect_targets_chars` の戻り値型 (`list[str]`)
  維持の保証
- **新設** `tests/test_bash_handler.py::TestInputRedirectFormInReason`
  — 8 件。handle() 経由で SFG_DENY body に ``form:`` 行が出る regression
  防止 (bare / no_space / fd_prefixed / quoted の各 form と、operand scan
  経路では form 行が出ないことの確認)
- **新設** `tests/test_messages.py::TestBashDenyInputRedirectForm`
  — 8 件。builder 単体で form 引数を反映、省略時は出力しない、行位置 (form
  行が suggestion 行より前)
- 累計 **643 + 27 = 670 件 OK** (M5 で +36 件)

### 非互換性

- 内部 API: `bash_deny` に `form` キーワード引数追加 (default None で後方
  互換)。既存 caller は無改修で動く
- SFG_DENY body に新規行 ``form: <値>`` 追加 (input_redirect 系のみ)。
  reason 値 / 外殻 schema は不変
- `_scan_input_redirect_targets_chars` (`list[str]`) は thin wrapper として
  温存。新規実装は `_scan_input_redirect_targets_with_form` を直接呼ぶこと

### 関連レビュー

`docs/REVIEW_TASKS_2026-05-03.md` の **B (bashlex 採否)** と **M5 (リダイレ
クト形式タグ)** を消化。これで H1 / H2 / H3 / M1 / M2 / M3 / M4 / M5 / L1
/ L2 / L3 / L4 / L5 / B のすべてが完了。**0.5.0 で本レビューサイクルを
完結**。

## 0.4.4

**L5 消化**: 既存テストの allow チェックを `output.is_allow(r)` 述語に
一括置換。Phase 0 spec が将来 `permissionDecision: "allow"` 明示出力に
変わってもテストが壊れないようにする変更耐性向上。コードベース側の挙動
変更なし。

### 主要な変更

- `tests/test_bash_handler.py`: `self.assertIsNone(_decision(r))` /
  `self.assertEqual(r, {})` 計 55 件を `self.assertTrue(output.is_allow(r))`
  に置換
- `tests/test_edit_handler.py`: 同パターン 4 件を置換
- 既存 helper `_decision(resp)` は deny / ask 検証で引き続き使うため温存
- 置換漏れ 0、残存 0 を grep で確認

### テスト結果

```
hooks/redact-sensitive-reads: Ran 607 tests in 0.129s — OK
hooks/check-sensitive-files: Ran 27 tests in 1.9s — OK
```

### 関連レビュー

`docs/REVIEW_TASKS_2026-05-03.md` の L5 を消化。これで H1 / H2 / H3 /
M1 / M2 / M3 / M4 / L1 / L2 / L3 / L4 / L5 がすべて完了。残るは
M5 (リダイレクト形式タグ、B と合流) と B (bashlex 採否、別セッション議論)。

## 0.4.3

**小改善 4 件 (L1〜L4) を消化**。logging の detail 文字種 sanitize、dotenv parse の
bare except 分類、HookSpecificOutput の TypedDict 化、is_allow 述語導入。
Phase 0 spec 変更耐性とコード変更時の意図せぬ秘密漏洩リスクを下げる小規模リファクタ。
機能変更なし。

### 主要な変更

1. **L1: `core/logging.py` の detail に文字種ホワイトリスト導入** —
   `_sanitize_detail` を追加し、`log_error` / `log_info` が detail を
   `^[A-Za-z0-9_:.\-\[\]!]{0,64}$` で filter する。違反は `_BAD` placeholder
   に置換してログに書き出す。設計コメントで依存していた呼出側責任 (公開可
   情報のみ渡す) の最終防御層として、コード変更時の意図せぬ秘密混入
   (path / 値 / basename / コマンド文字列) を実行時に止める。

2. **L2: `_extract_dotenv_keys` の bare except を狭める** —
   `except Exception:` を `except (ValueError, UnicodeDecodeError,
   AttributeError, TypeError)` に変更。失敗時は
   `L.log_info("dotenv_parse_failed", type(e).__name__)` で種別をログに残す。
   想定外例外 (KeyboardInterrupt / SystemExit 等) は握りつぶさず伝播。

3. **L3: `HookSpecificOutput` / `HookResponse` を TypedDict 化** —
   `core/output.py` に Phase 0 spec の shape を型定義として追加。各 builder
   (`make_deny` / `make_ask` / `make_allow` / `ask_or_deny` / `ask_or_allow`)
   の戻り値型注釈を `HookResponse` に更新。実行時挙動は不変。

4. **L4: `is_allow(r)` 述語を導入** —
   `make_allow()` が `{}` を返す現行仕様 + 将来 spec が
   `permissionDecision: "allow"` 明示出力に拡張された場合の両方で True を
   返す。テストは新述語で書くことを推奨し、既存テストヘルパー
   (`_decision(r)`) は将来一括置換のため温存。

### テスト

- **新設** `tests/test_logging.py` 9 件 (L1)
- **新設** `tests/test_output.py` 20 件 (L3 / L4)
- **追加** `tests/test_edit_handler.py::TestDotenvParseFailureLogged` 3 件 (L2)
- 累計 **607 件 (redact) + 27 件 (check) = 634 件 OK**

### 非互換性

- 内部 API: `core.output` の builder 戻り値型を `dict` → `HookResponse`
  (TypedDict) に変更。実行時は dict のままで挙動変更なし。
- `core.logging.log_info` / `log_error` の detail に許可外文字 (path / 値 /
  空白 / 改行など) を渡すとログに `_BAD` として記録される。既存の呼出箇所は
  すべてホワイトリスト内で動作することを実測で確認済み。

### 関連レビュー

`docs/REVIEW_TASKS_2026-05-03.md` の L1 / L2 / L3 / L4 を消化。残るは
M5 (リダイレクト形式タグ) / L5 (M1 / M4 で部分対応済み、別途消化) /
B (bashlex 採否、別セッション議論)。

## 0.4.2

**deny 系 reason の `<SFG_DENY>` 構造化包装**。LLM が deny の根拠を機械的に
パースできるよう、Bash / Edit / Write / MultiEdit / Hook (policy_unavailable)
の deny を共通の外殻タグで包む。後段 hook (review / 集計) が `reason` 値を
grep して block 種別を再分類できるようにする schema を確定。ask 系
(ask_or_deny / ask_or_allow) は plain text のまま (機械処理ニーズが薄いため)。

### 主要な変更

1. **`<SFG_DENY tool reason guard>` 構造化包装** — 全 deny reason を以下の
   schema で包む:

   ```
   <SFG_DENY tool="<Bash|Edit|Write|MultiEdit|Hook>" reason="<kind>" guard="sfg-v1">
   note: <人間向け説明文>
   matched_operand: <Bash の operand>      ← Bash 系のみ
   first_token: <Bash コマンド名>          ← Bash 系のみ
   basename: <Edit/Write の basename>      ← Edit 系のみ
   suggested_keys:                        ← edit_deny の dotenv 系
     KEY_NAME=
     ...
   suggestion_alt: <代替案>                ← 任意
   extra_note: <symlink / special 等>      ← 任意
   suggestion: <patterns.local.txt 案内>   ← 必須
   </SFG_DENY>
   ```

2. **`reason` 値を 8 種類に確定** (`SfgDenyReason`):
   - `literal` / `glob` / `input_redirect` / `input_redirect_glob` (Bash 系)
   - `sensitive_path` / `sensitive_path_symlink` / `sensitive_path_special`
     (Edit 系)
   - `policy_unavailable` (Hook 系)

3. **`edit_deny` に `kind` 引数を追加**: symlink / special を `reason` 属性で
   区別できるよう、handler 側 (`edit_handler.py`) で
   `kind="sensitive_path_symlink"` / `kind="sensitive_path_special"` を渡す。
   default は `kind="sensitive_path"`。

4. **`escape_xml_tag(text, tag_name)` 一般化** —
   `redaction/sanitize.py::escape_data_tag` を一般化し、任意タグ名で外殻破壊
   防御を効かせられる形に。`escape_data_tag` は薄い wrapper として残す
   (後方互換)。Read 側 `<DATA>` 包装と Bash/Edit 側 `<SFG_DENY>` 包装が
   同じ防御層を共有する。

5. **テスト追加** — 575 + 27 = 602 件:
   - `tests/test_messages.py::TestSfgDenyEnvelope` 12 件 — 外殻属性、reason 値、
     body 各行、外殻破壊耐性 (body に偽 `</SFG_DENY>` を仕込んでも 1 組のみ)、
     ask 系が plain text のままであることの確認
   - `tests/test_sanitize.py::TestEscapeXmlTag` 7 件 — 一般化版の挙動と
     `escape_data_tag` の後方互換

### 後段 hook 連携の例

外部 review hook が deny 種別を grep する場合:

```bash
# Bash の機密 operand 検出 (literal / glob / input_redirect / input_redirect_glob)
grep -E '^<SFG_DENY tool="Bash" reason="(literal|glob|input_redirect)' deny.log

# Edit/Write の symlink 経由 deny だけ抽出
grep '^<SFG_DENY tool="(Edit|Write|MultiEdit)" reason="sensitive_path_symlink"' deny.log
```

### 非互換性

- 内部 API: `messages.bash_deny` / `messages.edit_deny` /
  `messages.policy_unavailable("deny")` の戻り値が plain text → 構造化包装に
  変化。reason 文字列に依存する自動 grep / 文言マッチはアップデート必要。
- `messages.edit_deny` に `kind` キーワード引数を追加 (default あり、後方互換)。
- ask 系 (`read_ask` / `edit_pause` / `bash_lenient` /
  `policy_unavailable("pause")`) は plain text のままで変更なし。

### 関連レビュー

`docs/REVIEW_TASKS_2026-05-03.md` の M4 を消化。M5 (リダイレクト形式タグ) /
L1〜L5 / B (bashlex 採否) は次リリース以降。

## 0.4.1

**reason 文言の品質改善 + reason builder 集約**。`permissionDecisionReason` に
入れる文字列を `core/messages.py` に集約し、文言の語彙ルールと basename 展開を
1 箇所で管理する形にリファクタ。Bash deny の operand 名漏れ (literal match で
operand 名がメッセージに含まれない) を修正。

### 主要な変更

1. **`core/messages.py` 新設** — reason builder を集約:
   - `bash_deny(first_token, operand, kind)`
     (`literal` / `glob` / `input_redirect` / `input_redirect_glob`)
   - `edit_deny(tool_label, basename, new_keys, extra_note)`
   - `policy_unavailable(severity, tool_label)`
   - `read_ask(kind)` (`symlink` / `special` / `io_error` / `normalize_failed`
     / `redaction_failed` / `open_failed`)
   - `edit_pause(kind, tool_label)` (`normalize_failed` / `io_error` /
     `parent_not_directory`)
   - `bash_lenient(kind, detail)` (`hard_stop` / `opaque_prefix` /
     `residual_metachar` / `shell_keyword` / `tokenize_failed` /
     `normalize_failed`)
   - `hook_invocation_error` / `stdin_parse_failed` / `unsupported_platform` /
     `handler_internal_error` (`__main__` wrapper 用)

2. **Bug fix: Bash deny reason に operand 名を含める** — literal match で
   operand 名 (引っかかったパス) が文言に欠けていた。glob 側は出ていたが
   literal 側だけ抜けていたため、LLM が `cat foo bar .env baz` のうちどれが
   NG か判断できず、推奨される `!<basename>` 追加も具体化できなかった。

3. **`!<basename>` を実 basename に展開** — 旧版はプレースホルダ
   `<basename>` がベタ書きで LLM がコピペで `patterns.local.txt` に追記でき
   なかった。実 basename (`os.path.basename` 結果) を埋め込む形に変更。

4. **語彙ルールを統一**:
   - `make_deny` 系 → 「block しました」
   - `ask_or_deny` 系 → 「~してから再試行してください」
   - `ask_or_allow` 系 → 「判定不能のため確認を挟みます (auto / bypass /
     plan では通過)」

5. **LLM 向け文言への書き換え**:
   - 「続行しますか？」(Read symlink / special) → 「symlink 先が意図した
     参照か確認してから再試行してください」
   - 「管理者に連絡してください」(`__main__`) → 「settings.json の hooks
     定義 (--tool 引数) を確認してください」「~/.claude/logs/redact-hook.log
     を確認してください」
   - 「安全側で deny します」「安全側で一時停止します」のような揺れを排除し、
     語彙ルールに沿った末尾フレーズに集約。

6. **handler 側のリファクタ** — `read_handler.py` / `bash_handler.py` /
   `edit_handler.py` / `__main__.py` のインライン f-string reason をすべて
   `core.messages` の builder 経由に置換。`edit_handler._build_deny_reason`
   関数と `MAX_SUGGESTED_KEYS` 定数は撤去 (messages.py に集約)。

7. **テスト追加** — 累計 528 件 → **555 件** (+27 件):
   - `tests/test_messages.py` 40 件 — builder 単体テスト + 動詞ルールの
     fixate (`TestVocabularyConsistency`)
   - `tests/test_bash_handler.py::TestDenyReasonContent` 4 件 — operand 名 /
     basename 案内が reason に含まれる regression 防止
   - `tests/test_edit_handler.py::TestDenyReasonBasename` 3 件 — basename
     展開の regression 防止

### 非互換性

- 内部 API: `edit_handler._build_deny_reason` 関数と
  `edit_handler.MAX_SUGGESTED_KEYS` 定数を削除。`core/messages.edit_deny`
  に集約。外部 import 利用は確認範囲では無し。
- reason 文言が変わるが、`permissionDecision` (deny / ask) の判定は不変。
  reason 文言に依存する自動 grep / 文言マッチはアップデート対応が必要。

### 関連レビュー

`docs/REVIEW_TASKS_2026-05-03.md` の H1 / H3 / M1 / H2 / M2 / M3 を消化。
M4 (`<SFG_DENY>` 構造化包装) / M5 (リダイレクト形式タグ) / L1〜L5 / B
(bashlex 採否) は次リリース以降。

## 0.4.0

**`patterns.local.txt` 配置パスの 2-tier lookup 導入**。0.3.x までは
`$XDG_CONFIG_HOME/sensitive-files-guard/patterns.local.txt`
(未設定時 `~/.config/sensitive-files-guard/patterns.local.txt`) のみを参照して
いたが、Claude Code のユーザー設定と同じディレクトリツリーに集約するため
`~/.claude/sensitive-files-guard/patterns.local.txt` を優先パスとして追加。

### 主要な変更

1. **`_resolve_local_patterns_path` → `_resolve_local_patterns_paths` に改名**
   (複数形)。戻り値が `list[Path]` になり、preferred (`~/.claude/...`) と
   fallback (`$XDG_CONFIG_HOME/...` or `~/.config/...`) の 2 要素を返す。
   旧単数形は preferred を返す後方互換 alias として残留。
2. **`load_patterns` が preferred → fallback の順に試行**。両方存在時は
   preferred のみ採用 (fallback は無視)。fallback 採用時は
   `warn_callback("deprecated_config_dir")` を呼ぶ。
3. **呼出側の warn_callback 分岐**:
   - Read hook (`core/patterns.py`): deprecation は `log_info` で LOG_PATH
     のみ 1 行 (`permissionDecisionReason` に載せない — LLM 文脈のノイズ回避)
   - Stop hook (`check-sensitive-files/checker.py`): stderr に 1 行
     (Claude Code UI で可視化されるが LLM には載らない)
4. **docs/PATTERNS.md / README.md / CLAUDE.md を 2-tier lookup に対応**。
   初回作成手順を新パスへ更新、旧パスからの移行手順を追記。
5. **テスト追加** (`test_patterns_loader.py` に 4 ケース):
   - `~/.claude/` 優先が採用される
   - `$XDG_CONFIG_HOME` fallback が採用される
   - 両方存在時は `~/.claude/` 勝ち
   - fallback 採用時は deprecation warn が発火

### 方針

**0.6.0 で fallback (XDG_CONFIG_HOME / ~/.config) を削除予定**。それまでに
既存利用者は `~/.claude/sensitive-files-guard/patterns.local.txt` へ移行する
こと。patterns.txt (plugin 同梱) 側は変更なし。

### 非互換性

- 新しい関数名 `_resolve_local_patterns_paths` (複数形) を internal API として
  公開。単数形 `_resolve_local_patterns_path` は後方互換 alias として残存する
  が、将来削除の可能性あり
- 戻り値型変更 (`Path` → `list[Path]`) に伴い、internal API 直参照していた
  コードは書き換えが必要 (`check-sensitive-files/checker.py` /
  `redact-sensitive-reads/core/patterns.py` は 0.4.0 で追従済み)

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
4. **character-level parser を word 概念ベースに強化 (Codex PR review R1-R8)**
   — PR レビューで 8 件の指摘を受けて修正:
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
   - **R4 (P2)**: double quote 内 backslash escape が任意文字を unescape
     していたため、``cat < ".\\env"`` (literal ``.\\env``) が ``.env`` として
     解釈されて誤って deny する false-positive があった。POSIX sh の
     double-quote semantics に準拠し、``\\X`` は X が ``$`` ``\\`` ``"`` ``\\\\``
     ``\\n`` のいずれかのときのみ X を取り込み、それ以外は ``\\`` も literal
     として保持する形に修正。`_DQ_BACKSLASH_ESCAPABLE` frozenset を新設。
   - **R5 (P1, security)**: `<(...)` ブランチ終了直後に ``at_word_start`` を
     更新せず、続く ``#`` を誤ってシェルコメント扱いしていた。例:
     ``cat <(echo x)#bar < .env`` では bash 上 ``<(echo x)#bar`` が 1 word なので
     ``#`` は comment ではないが、parser は ``# < .env`` まで skip してしまい
     後続 redirect を取りこぼし、auto/plan で ``ask_or_allow`` → allow に倒って
     **機密 bypass** を許す regression。修正: ``<(...)`` 終了後 ``at_word_start
     = False`` (process sub は 1 word)。``<<`` / ``<&`` 後は ``at_word_start =
     True`` (operator なので word 開始)、target 抽出後も ``False`` を明示。
   - **R6 (P2)**: ``[[ "$x"<.env ]]`` のような bash conditional / arithmetic
     式内の ``<`` を redirect target として抽出して deny に倒っていた。bash で
     は ``[[ ... ]]`` 内の ``<`` は文字列比較演算子、``(( ... ))`` 内の ``<``
     は算術比較演算子で、いずれも redirect ではない。修正: ``[[`` (word start
     位置のみ) を検出したら閉じ ``]]`` まで quote / escape を尊重しつつスキップ。
     ``((`` も同様で内部 nesting に対応するため depth tracking を実装。元の
     regex 挙動 (空白必須なので ``[[ "$x"<.env ]]`` 等は target 取れず ask) と
     整合する形に戻した。
   - **R7 (P1, security)**: R6 で追加した ``[[`` 検出条件が「word が ``[[`` で
     始まる」だけで、``[[foo`` のような通常 word も予約語扱いしていた。bash の
     ``[[`` 予約語は **直後に空白必須** が仕様。``tee [[foo < .env`` で
     ``[[foo`` 以降が予約語ブロックと誤認され、閉じ ``]]`` を探し続けて command
     末まで消費 → 後続の ``< .env`` を取りこぼし、auto/plan で
     ``ask_or_allow`` → allow に倒って **機密 bypass** を許す regression。
     修正: ``[[`` 検出条件に ``command[i + 2] in " \\t\\n"`` を追加。``((`` は
     bash 仕様上 space optional のため変更なし。
   - **R8 (P1, security)**: R6/R7 後も ``[[`` ``((`` を「word start で空白に
     続けば全位置で予約語扱い」していた。bash 仕様では **command word 位置**
     (segment 先頭 = 入力先頭 / `;` / `&` / `|` / 改行 直後) でのみ予約語。
     引数位置 (例: ``tee [[ "$x" < .env ]]``) では通常 word なので、内部の
     ``< .env`` は本物の input redirect として扱う必要がある。修正: parser に
     ``at_command_start`` 状態を追加 (segment separator ``|&;\\n`` 直後で True、
     word / quote / escape 消費で False)。``[[`` ``((`` を予約語扱いするのは
     ``at_command_start = True`` のときのみに限定。

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
| `cat < ".\env"` (literal `.\env`, R4 fix) | **誤 deny** (`.env` と誤解釈) | ask_or_allow (literal `.\env` で rule 非 match) |
| `cat < ".env\*"` (literal `.env\*`, R4 fix) | 誤って glob 展開対象扱い | literal `.env\*` (rule 非 match) |
| `cat <(echo x)#bar < .env` (proc sub + 連結 `#`, R5 fix) | auto/plan で **bypass** (security regression) | **deny** (`#` は word 内、target 抽出成功) |
| `[[ "$x"<.env ]]` (条件式内の `<`, R6 fix) | **誤 deny** (`<` を redirect 扱い) | ask_or_allow (`[[ ]]` 内 skip で target なし) |
| `(( a<5 ))` (算術内の `<`, R6 fix) | **誤 deny** (同上) | ask_or_allow (`(( ))` 内 skip で target なし) |
| `tee [[foo < .env` (`[[foo` は通常 word, R7 fix) | auto/plan で **bypass** (security regression) | **deny** (`[[` 予約語に space 必須、target 抽出成功) |
| `tee [[ "$x" < .env ]]` (引数位置の `[[`, R8 fix) | auto/plan で **bypass** (security regression) | **deny** (引数位置 `[[` は通常 word、target 抽出成功) |
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
- テスト件数: 411 → 502 件 (+92 追加, -1 削除, 2 件は挙動変更に伴う書換)。
  内訳: inline/fd 10 + quote 6 + exclusion 7 + handle 11 + R1 concat word 6 +
  R2 comment 7 + handle R1/R2 4 + R3 escape paren 5 + handle R3 2 +
  R4 dq escape 6 + handle R4 1 + R5 proc-sub word boundary 3 + handle R5 2 +
  R6 conditional/arith 8 + handle R6 2 + R7 [[ space-required 4 + handle R7 2 +
  R8 [[ command-position 4 + handle R8 2
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
