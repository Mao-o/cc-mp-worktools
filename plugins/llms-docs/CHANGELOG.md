# Changelog

All notable changes to this plugin will be documented here.

## [0.14.0] - 2026-06-08

### researching-ai-sdk: ai-sdk.dev 上流構造変更に追従 (bd_092a232e-n9i)

ai-sdk.dev/llms.txt が ~2KB / 46 行のインデックス + 検索 API 案内 + llms-full.txt
リンクに分離され、本体は `llms-full.txt` (~5MB / 530 doc) に移動した。
旧構造 (1 ファイルに全 doc 連結) を前提にしていた `parse-ai-sdk.py` では
`search "streamText"` 等の基本 API 検索がゼロ件になっていたのを修正。

#### 変更内容

- `LLMS_TXT_URL` を `https://ai-sdk.dev/llms.txt` → `https://ai-sdk.dev/llms-full.txt`
  に切替
- `DEFAULT_CACHE_FILENAME` を `ai-sdk-llms.txt` → `ai-sdk-llms-full.txt` に変更
  (旧 cache との混在を避ける)
- fetch timeout を 60s → 120s (5MB ファイルのため余裕を確保)
- docstring / argparse description / 各種コメントを `llms-full.txt` 起点の
  説明に更新
- `llms-full.txt` 先頭は frontmatter ではなく contributing guide
  (TypeScript コード) で始まるが、`split_documents` は最初の `---` まで
  無視するため挙動影響なし
- SKILL.md / references/llms-txt-structure.md / README.md の cache filename
  と doc 数記述を更新

#### CLI I/O 仕様

既存サブコマンド (search / search-index / search-content / sections /
content / fetch-index) の I/O 仕様は維持。利用側のコマンド書き換えは不要。

#### 検証

- `search "streamText"` で複数 doc が hit (28 body hits in top doc)
- `search "useChat"` / `search "generateText"` も正常 hit
- `search "stream object"` (space 区切り) で structured data ガイドが top
- `fetch-index --compact` で 530 documents
- `sections 22` / `content 22 "<heading_path>"` で本文取得確認

#### SKILL.md metadata version

`3.3.0` → `3.3.1` (script 仕様変更に伴うキャッシュ filename 変更を反映。
skill 起動条件・トリガー語は不変なので patch bump)

## [0.13.0] - 2026-06-08

### researching-ai-sdk / researching-firebase: 使用感ベース改善の横展開

0.12.0 で `researching-claude-docs` に入れた改善パターンを ai-sdk /
firebase にも適用。3 SKILL の構造を揃え、初見ユーザーが同じ感覚で扱える
ようにした。

#### researching-ai-sdk: 3.2.0 → 3.3.0

- **Quick Start を冒頭に追加**: 2 コマンドのみの最小ブロック
- **`when_to_use` を独立フィールド化** + verb 拡張:
  `implementing, debugging, configuring, reviewing, or designing`
  + `especially before editing code that imports from ai / @ai-sdk/*`
- **Triggers に最新 API 名を追加**:
  `streamObject` / `generateObject` / `useObject` / `tool` / `tools` /
  `embed` / `embedMany` / `convertToModelMessages` / `provider`
- **出力フォーマット強制度を緩和** (claude-docs と同じ書き換え)
- `paths:` は追加しない (AI SDK は特徴的ファイル名がなく誤発火リスクが
  高いため、description ベースの auto-invoke に任せる)

#### researching-firebase: 2.0.0 → 2.1.0

- **Quick Start を冒頭に追加**: 2 コマンドのみの最小ブロック
- **`when_to_use` を独立フィールド化** + verb 拡張:
  `implementing, debugging, configuring, reviewing, or designing`
  + `especially before editing firebase.json / .firebaserc / *.rules /
  *.indexes.json`
- **Triggers に最新プロダクト名を追加**:
  `AI Logic` / `Genkit` / `App Hosting` / `Data Connect` /
  `security rules` / `firestore.rules` / `storage.rules`
- **`paths:` を新規追加** (Firebase 固有ファイル名で auto-trigger):
  `**/firebase.json` / `**/.firebaserc` / `**/firestore.rules` /
  `**/firestore.indexes.json` / `**/storage.rules` /
  `**/database.rules.json` / `**/remoteconfig.template.json` /
  `**/apphosting.yaml`
- **出力フォーマット強制度を緩和** (claude-docs と同じ書き換え)

#### 統一の効果

3 SKILL とも以下の構造で揃った:

```
---
description: |
  ... (各 source 固有の説明)
when_to_use: |
  Use when implementing, debugging, configuring, reviewing, or designing ...
  Use proactively before answering spec questions ...
  Triggers: ...
context: fork
model: sonnet
allowed-tools: [Read, Bash, WebFetch]
paths: [...]  # claude-docs / firebase のみ
metadata: { author, version }
---

# Title

## Quick Start
(2 コマンドの最小ブロック)

(以降は source 固有の詳細)

## 出力フォーマット (参考)
(緩和された最低限要素のみ)
```

## [0.12.0] - 2026-06-08

### researching-claude-docs: 使用感ベース改善 (AgentSkill 仕様調査の実体験から)

skill 自身を使って AgentSkill の最新仕様 (frontmatter フィールド一覧 /
ロード段階 / `context: fork` 挙動 / `paths:` 条件 / `hooks:` / Skill vs
Subagent) を verbatim 取得した実体験から、以下 3 点を改善。

#### 1. SKILL.md 冒頭に Quick Start を追加

9 セクション・183 行に対して「最初に何をすればいいか」が掴みづらかった。
冒頭に 2 コマンドのみの最小ブロックを置き、迷ったらここから始められるよう
にした。`--source both` / `--source platform` の指針も 1 行で示す。

```bash
# 1. キーワードで候補ページと本文ヒットを 1 コマンドで取得
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/parse-claude-docs.py" search "<キーワード>"
# 2. 返ってきた [doc_idx] と heading_path を使って本文取得
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/parse-claude-docs.py" content <doc_idx> "<heading_path>"
```

#### 2. 最新 frontmatter フィールド名を Triggers に追加

`disable-model-invocation` / `user-invocable` / `argument-hint` / `effort` /
`arguments` / `context: fork` / `paths` / `SubagentStop` / `$ARGUMENTS` /
`$CLAUDE_SKILL_DIR` / `output style` を `Triggers:` に追加。新しいフィールド
名で質問されても description マッチで auto-invoke されるようにした。

合算 char 数: 689 → 約 980 chars (1,536 cap headroom 556)

#### 3. `paths:` glob に `**/.claude/skills/**` を追加

skill の `references/` や `scripts/` を編集する際にも自動 trigger されるよう
拡張。既存の `**/SKILL.md` は SKILL.md ファイル本体しかカバーしていなかった
ため、skill ディレクトリ全体作業時のロード抜けを補う。

#### 4. when_to_use の action verb 拡張

旧 `implementing, debugging, configuring, or reviewing`
新 `implementing, debugging, configuring, reviewing, or **designing**`
+ `especially before editing SKILL.md / agent / hook files` の Use-before
ガイダンスを追記。「Skill 自身を設計・編集している最中こそ skill が必要」
という観点を明示。

#### 5. 出力フォーマット強制度を緩和

「### 調査結果 / ### コード例 / ### 情報源 / ### 注意事項」の固定 4 セクション
を必須から「参考スケルトン」に変更。複数フィールドの仕様調査では表組み、
複数引用の比較では blockquote の方が読みやすい体験から、最低限満たすべき
要素 (発見事項 / 引用元 / コード例 / 注意事項) のみ示して、構成は柔軟に
できるようにした。

SKILL.md metadata version: `3.3.0` → `3.4.0` (Skill 自発 invoke 判定材料が
変わるため minor bump)

## [0.11.1] - 2026-06-06

### researching-claude-docs: paths あり skill 向け description ガイドラインに準拠

`paths` 指定がある skill では where (path 条件) が paths field に外出しできる
ため、`description` のトークンを「**何をする** (action) + **task 文脈での
トリガー語** (when)」に再配分するのが推奨。これに合わせて frontmatter を
ブラッシュアップ。

- `description` を**動詞先頭**に変更:
  `Fetch verbatim sections from Claude Code (code.claude.com) and Claude
   Developer Platform (platform.claude.com) 公式 docs — ...`
  (旧: 名詞列「Claude Code … 公式ドキュメントから … 取得する」)
- `when_to_use` の action verb を強化:
  旧 `implementing or debugging` → 新 `implementing, debugging,
  configuring, or reviewing`。`Skills/AgentSkill` を 1 トークンに統合
- Triggers から description で既出 / ユーザー prompt に出にくい host 系を削除:
  `"Claude Code ドキュメント"`, `"code.claude.com"`, `"platform.claude.com"`
- 合算 char 数: 774 → 673 chars (1,536 cap headroom 863)

SKILL.md metadata version: `3.2.0` → `3.3.0` (Skill 自発 invoke 判定材料が
変わるため minor bump)

## [0.11.0] - 2026-06-05

### researching-claude-docs: 3 つの機能追加

#### `paths:` auto-activation (SKILL.md frontmatter)

`SKILL.md` / plugin manifest / agent / command / hook / settings / MCP 設定
ファイル等を編集する直前に skill が自動でロードされる。Claude が WebFetch で
取りに行く事故を最小コストで減らす。

```yaml
paths:
  - "**/SKILL.md"
  - "**/.claude-plugin/**"
  - "**/.claude/agents/**.md"
  - "**/.claude/commands/**.md"
  - "**/.claude/hooks/**"
  - "**/.claude/settings*.json"
  - "**/.mcp.json"
  - "**/hooks.json"
```

#### `search --source both` 並列検索

`code` (Claude Code) と `platform` (Claude Developer Platform) を 1 コマンドで
横断検索する。Skill / hook / MCP のように両ソースに解説が散らばる topic で
切替コストが消える。

- 結果に `[code]` / `[platform]` プレフィックスを付けて区別
- `doc_idx` は **source 内ユニーク**なため follow-up 呼び出しに
  `--source <code|platform>` を明示するよう Note と Next hint で誘導
- `_search_one_source(args, source_key)` と `_print_search_results(results,
  label_source)` に分割してテスト・拡張容易性を確保

#### `content` 本文中の docs リンクに `→ [doc_idx N]` を付与

本文内の Markdown link (`[Text](/en/...)` / `[Text](/docs/en/...)` /
`[Text](https://code.claude.com/...)`) のうち**同 source 内の既知ページ**を
指すものに、自動でアノテーションが付く。follow-up の page 切替が 1 ステップ
減る。

- 絶対 URL と相対 path (`/en/...` ↔ `/docs/en/...` の alias 解決) の両方に対応
- self-link (現在ページへの link) は除外
- コードフェンス内 / Markdown テーブル行はスキップ (formatting 保護)
- `--no-link-annotations` で抑制可

#### SKILL.md metadata version

`3.1.0` → `3.2.0`

## [0.10.0] - 2026-06-05

### researching-claude-docs: 使用感ベースの改善

実際に AgentSkill 仕様を調査する過程で観察した摩擦点を 4 つ改善した。

#### `content` 出力にサブセクション hint を追加 (parse-claude-docs.py)

`content <doc_idx> "<heading_path>"` の本文末尾に、そのセクション直下の子
`heading_path` を一覧表示する。これまでは深掘り時に `sections` を再呼び出し
する必要があり 1 ステップ多かった。

- `heading_path` 指定時 → 直接の子 (level + 1) を表示
- `heading_path` 省略時 → トップレベル (L2) セクションを表示
- 出力例:

  ```
  --- Subsections of 'Configure skills/Frontmatter reference' (2) ---
    - Configure skills/Frontmatter reference/How a skill gets its command name
    - Configure skills/Frontmatter reference/Available string substitutions [code]

  Next: parse-claude-docs.py content 83 "<heading_path from above>"
  ```

- `--no-subsection-hints` で抑制可
- target.line_end は次の見出し開始でしかないため、target と同レベル以下の
  次セクションまでをブロック終端として再計算する

#### `search` / `search-content` の overflow セクション表示

`(60 body hits, showing 3)` 表示時、`--max-hits` (既定 3) で切り捨てた残り
セクションを `Other sections with hits (not shown):` として heading_path と
ヒット数を一覧表示する。`_common.py` の `search_content_in_body` 戻り値に
`overflow_sections` フィールドを追加 (既存呼び出し元は無視するだけなので
非破壊)。

#### SKILL.md frontmatter の再構成

公式 Skills ベストプラクティス (key use case first / 1,536 char cap) に
合わせて `description` と `when_to_use` を分離。

- `description`: 「公式ドキュメントから verbatim 取得 / WebFetch 回避」を
  冒頭に置き、Triggers/Use when を分離して説明性を上げた
- `when_to_use`: Triggers キーワード列 + Use when を集約。`"AgentSkill"`,
  `"Skill"` を Triggers に追加 (今回の調査で頻出キーワードだった)
- skill metadata version: `3.0.0` → `3.1.0`

#### SKILL.md 本文の重複削減

- `v3 互換性` セクションを削除 (時間依存表記の回避: `~/.claude/rules/claude/skills/principles.md`)
- 「推奨フロー」と「コマンドリファレンス」で重複していた `page_ref` /
  `heading_path` の説明を「リファレンス」セクションに集約
- 「サブフロー」をテーブル化して縦方向に圧縮
- サブセクション hint 仕様を `Step 2` 内に文書化

## [0.9.0] - 2026-06-05

### plugin rename: `doc-researcher` → `llms-docs` (BREAKING)

`context: fork` の skill が `<plugin>:<skill>` 完全修飾名 + description の
「独立コンテキストで実行され」表現により、**Skill ツールではなく Agent ツールの
`subagent_type` として誤呼び出し**される問題を解消した
(`Agent type 'doc-researcher:researching-claude-docs' not found`)。

- plugin 名 `doc-researcher` → `llms-docs` に変更。"doc-research**er**" の `-er`
  語尾が agent (researcher) を連想させていたのを除去
- ディレクトリ `plugins/doc-researcher/` → `plugins/llms-docs/` (`git mv`)
- skill 名 (`researching-claude-docs` / `researching-ai-sdk` /
  `researching-firebase`) は**動名詞命名のため維持** (Anthropic skill 命名推奨に従う。
  外部 rule の table 追従も不要)
- 3 SKILL.md の description「独立コンテキストで実行されメインセッションを消費しない」
  → 「Skill ツールで起動し、メインの会話コンテキストを消費しない」に変更
  (subagent 連想を排し、Skill ツール起動を明示)
- `marketplace.json` の entry name / source、SessionStart hook メッセージを追従
- `scripts/` は `${CLAUDE_PLUGIN_ROOT}` 経由参照のため動作影響なし
- `context: fork` + `model: sonnet` の設計 (subagent fork + Sonnet) は維持

#### 移行 (利用者向け)

旧名でインストール済みの場合は再インストールが必要:

```
/plugin uninstall doc-researcher@mao-worktools
/plugin install llms-docs@mao-worktools
```

## [0.8.0] - 2026-05-26

### SessionStart hook 追加 (5dk.7)

- doc-researcher plugin 有効時に SessionStart で「WebFetch より doc-researcher スキルを優先」リマインドを注入
- 生 stdout 形式で 1 行のみ (SessionStart では plain stdout が Claude に届く — 公式推奨。トークン最小化)
- plugin hook のため doc-researcher 未インストール環境では発火しない

### キャッシュ TTL 既定値統一 (5dk.9)

- 3 スクリプト全てに `--max-age` を統一実装 (既定: 604800 秒 = 7 日)
- `_common.py` に `DEFAULT_MAX_AGE_SECONDS` 定数 + `add_max_age_arg()` ヘルパー追加
- `parse-ai-sdk.py` / `parse-firebase.py` に `--max-age` CLI 引数を新規追加
- `parse-claude-docs.py` のローカル `_add_max_age_arg` を共通版に統一
- 強制 re-fetch: `--max-age 0`
- 3 SKILL.md に「キャッシュ期限切れ」行を追加

## [0.7.0] - 2026-05-23

### 3 script API 統一 (BREAKING)

claude-docs v3 (0.5.0) で実装した `search` 統合 + `--page-ref` + `--file` flag 化を
ai-sdk / firebase にも展開し、3 script で以下の 6 サブコマンドを共通化:

- `fetch-index` — 軽量 index 一覧
- `search-index` — title/description でランキング (候補だけ取得)
- `search-content` — 本文横断キーワード検索 (`--page-ref` で 1 ページに絞れる)
- `search` — 統合検索 (`search-index` + 本文 hits を 1 コマンドで返す、推奨入口)
- `sections` — 指定ページの見出し一覧
- `content` — ページ全体 / セクション本文

`<page_ref>` は 3 形式を受け付ける (ai-sdk は URL を持たないため int / title 部分一致のみ):

- 整数 index
- URL slug (last path component)
- 完全 URL

### `researching-ai-sdk` skill 3.2.0 (BREAKING)

- `<file>` positional 引数を全廃止 → `--file` flag 化 (省略時は cache を auto-fetch)
- `search-content` の `--doc-index` → `--page-ref` (int / title 部分一致)
- `sections <file> <doc_index>` → `sections <page_ref>`
- `content <file> <doc_index> "<heading_path>"` → `content <page_ref> "<heading_path>"`
- 旧 `search` (= `search-index` alias) を廃止し、**統合 search** に置き換え。
  top N (`--top-n`, default 5) 候補をスコアリングし、各 body を keyword 検索して
  heading_path + スニペットを返す
- SKILL.md を 2 段階フロー (`search` → `content`) に書き直し、v3.1.0 → v3.2.0 migration 例を追記

旧 → 新 置換例:

| v3.1.0 (旧) | v3.2.0 (新) |
|------------|------------|
| `search-index /tmp/ai-sdk-llms.txt "X"` | `search-index "X"` |
| `search-content /tmp/ai-sdk-llms.txt "X" --doc-index 42` | `search-content "X" --page-ref 42` |
| `sections /tmp/ai-sdk-llms.txt 42` | `sections 42` |
| `content /tmp/ai-sdk-llms.txt 42 "X"` | `content 42 "X"` |
| `search /tmp/ai-sdk-llms.txt "X"` (旧 alias) | `search "X"` (統合検索) |

### `researching-firebase` skill 2.0.0 (BREAKING)

- `sections <doc_index>` → `sections <page_ref>` (int / URL slug / 完全 URL)
- `content <doc_index>` → `content <page_ref>`
- `search-content --pages <idx,idx,...>` (REQUIRED 複数) → `--page-ref <ref>` (optional 単数)
- 新規 `search` 統合 (top N on-demand fetch + 本文 hits)。Firebase は llms-full.txt が
  ないため top N ページを順次 HTTP fetch するヒューリスティクス (初回のみ重い、cache hit 後は高速)
- 旧 `--pages` 廃止 → 複数ページ横断したいときは `search` 経由を使う
- SKILL.md を 2 段階フロー (`search` → `content`) に書き直し、v1.1.0 → v2.0.0 migration 例を追記

旧 → 新 置換例:

| v1.1.0 (旧) | v2.0.0 (新) |
|------------|------------|
| `search-content "X" --pages 42` | `search-content "X" --page-ref 42` |
| `search-content "X" --pages 42,43,44` | `search "X" --top-n 3` (search 経由が推奨) |
| (新規可能) | `sections vector-search` (URL slug で直接アクセス) |

### `researching-claude-docs` skill (変更なし)

claude-docs は 0.5.0 で既に新 API を実装済み。本 release で他 2 script が claude-docs と
揃ったため、3 script 共通の使い方が一貫した。

### scripts/_common.py

- 変更なし (既存の `score_entry` / `search_index_entries` / `search_content_in_body` /
  `normalize_doc_url` / `build_url_to_full_index` 等を ai-sdk / firebase の新規 `cmd_search`
  / `_resolve_page_ref` から再利用)

### 検証

- 3 script の `--help` および各サブコマンド の `--help` が argparse error なし
- `parse-ai-sdk.py search "streamText onFinish" --top-n 2` で top 2 候補 + 本文 hits が
  正常に取得できる (cache hit 時 ~1 秒)
- `parse-claude-docs.py search "hook" --index-limit 2` の既存挙動は維持 (regression なし)

### Fix

- `parse-firebase.py` の `_resolve_page_ref` で `page_ref` に完全 URL (e.g.
  `search-index` 出力の `URL:` 行をそのままコピペした `.md.txt` 付き URL)
  を渡したときに `No page found for URL` で fail するバグを修正。ユーザー
  入力側にも `_entry_url_for_match()` を適用して両側で `.md.txt` を剥がして
  から比較するようにした。SKILL.md / README で「完全 URL 受付」と謳って
  いるのと挙動を一致させる (Codex Review P2 feedback on PR #17)
- `parse-ai-sdk.py` の `_default_cache_path` / `parse-firebase.py` の
  `_index_cache_path` / `_pages_cache_dir` で `cache_dir.rstrip("/")` が
  `cache_dir="/"` を空文字列にしてしまい、`os.path.join("", filename)` が
  相対パスを返すバグを修正。`os.path.join` は trailing slash を自動処理する
  ため rstrip は元々不要 (Codex Review P3 feedback on PR #17)

### 設計判断記録 (subagent fork 維持)

3 SKILL の `context: fork + model: sonnet` 構成は **維持** する設計判断を明文化
(README.md の「設計判断」セクション参照)。spawn オーバーヘッドより context rot
回避と正確性を優先するため、軽量化方向 (fork 外し / 親 model 継承 / 軽量モード追加)
は採用しない。「軽い質問でも WebFetch に流れる」課題への対応は description 充実
(0.6.0) + doc-first rules (`~/.claude/rules/`) + `search` 統合 (本 release) の
3 系統で行う。

## [0.6.0] - 2026-05-23

### 3 SKILL description の統一: Triggers / Use proactively / WebFetch 優位文

- `researching-claude-docs` の frontmatter `description` に
  `Use proactively when ...` / `Use when implementing or debugging ... such as ...` /
  `Triggers: ...` 行を追加 (ai-sdk / firebase と同じパターンに揃える)。Triggers は
  "Claude Code", "hook schema", "subagent", "plugin manifest", "slash command",
  "settings.json", "permission", "MCP", "Anthropic API", "code.claude.com",
  "platform.claude.com", "Claude Code ドキュメント", "researching-claude-docs"
  の 13 個。これまで Triggers 列挙がなく ai-sdk / firebase と非対称だった状態を
  解消し、LLM 側の skill 候補マッチ率を底上げする
- 3 SKILL すべての `description` に「WebFetch ではなくこのスキルを使う（要約
  モデル経由ではないため field の抜け落とし・幻覚が起きない）」の優位文を
  統一文型で揃える。claude-docs に既存だったこの文型を ai-sdk / firebase の
  description にも追加し、3 スキル共通のパターンで LLM が「verbatim 取得が
  欲しい」「field 抜け落ちを避けたい」場面を引っかけられるようにする
- SKILL 本体 (markdown) / parse スクリプト I/F / キャッシュ動作の変更なし。
  description のみの非破壊変更

## [0.5.0] - 2026-05-14

### `researching-claude-docs` skill 3.0.0 (UX 改善 + 一部破壊的)

- **NEW** `search` subcommand: `llms.txt` のタイトル/説明ランキングと
  `llms-full.txt` の本文検索を **URL で join** して 1 コマンドで返す統合検索。
  返ってくる `doc_idx` は `content` / `sections` にそのまま渡せる
  (`search-index` / `search-content` 間の doc_idx 乖離問題を根本解決)
- **NEW** `--max-age` flag for `search` / `search-content` / `search-index` /
  `sections` / `content` / `fetch-index`: 既定では `/tmp/` キャッシュは無期限
  再利用、`--max-age N` (秒) を指定すれば期限切れで自動再 fetch
- **NEW** `--max-snippet-chars` flag for `search` / `search-content`:
  スニペット文字数上限 (既定 500 文字、`0` で無制限)
- **NEW** Changelog / Release notes ページの自動 deprioritize (`search` /
  `search-content`)。`--include-changelog-priority` で旧挙動に戻せる
- **BREAKING** `sections` / `content` / `search-content` の引数を再設計:
  - `file` positional 引数を廃止 → `--file` flag 化
  - `sections` / `content` の `doc_index` positional を `page_ref` に拡張、
    整数 / URL slug / 完全 URL を runtime で自動判別
  - `search-content` の `--doc-index` を `--page-ref` に改名 (slug/URL も受付)
  - 旧 `sections /tmp/claude-code-llms-full.txt 5` 形式は argparse error
  - 移行例: `sections 5` / `content hooks "Hook events/PreToolUse"` /
    `content https://code.claude.com/docs/en/hooks "..."`
- SKILL.md を全面書き直し: 推奨フローを 2 段階 (`search` → `content`) に簡略化、
  `page_ref` の 3 形式・slug 曖昧時の対処・v2 → v3 移行例を追記
- `next_hint` が `--source` を伝搬: `--source platform` で実行した時の follow-up
  ヒントが silently `code` (デフォルト) に落ちる事故を防止。デフォルトソース時
  はヒントを短く保つため省略
- `fetch-index` の `Next:` ヒントを v3 形式 (`sections <doc_index>`) に統一
  (旧 file positional 表記が残っていた点を修正)
- `--file` と `--source` の不整合検出: ユーザーが `--file /tmp/claude-platform-llms-full.txt`
  を渡したのに `--source` がデフォルト `code` だった場合などに、silent
  cross-population (platform 名のファイルに code docs を書き込む等) を防ぐため
  fetch 前に fail-fast。未知の `--file` (推測不能なパス) は従来通り通す
- `--include-changelog-priority` の挙動を `cmd_search` と `cmd_search_content`
  で揃える: フラグが ON のときペナルティ項だけを 0 にし、relevance ソート
  (`total_matches` 降順) は維持。以前は `search-content` 側でソート全体を
  skip していたため `--limit` が元の文書順で切られ、高 hit ページが落ちる
  リグレッションがあった
- `--file` 指定時のセマンティクスを **read-only** に変更: 既存 user ファイル
  を `--max-age` で silently 上書きする regression を排除。`--file` ありの時
  は (1) 既知 cache 名と `--source` の不整合を fetch 前に die (前出修正)、
  (2) ファイル不在も fetch せず die (`--file` を外して auto-fetch せよと案内)、
  (3) `--max-age` は無視。fetch-and-cache サイクルは `--file` を渡さない時
  にだけ走る。`--file` で渡したローカルスナップショットは絶対に上書きされない

### `_common.py` 共有ヘルパー強化

- `fetch_url` に `max_age` kwarg を追加 (既存呼び出しは backward compatible)
- `search_content_in_body` に `max_snippet_chars` kwarg を追加 (既存呼び出しは
  backward compatible)
- `normalize_doc_url` / `build_url_to_full_index` を追加 (`.md` suffix /
  trailing slash / query / fragment を剥がした正規化 URL で
  llms.txt ↔ llms-full.txt の 1:1 join を担保)

### 検証

- Claude Code llms.txt と llms-full.txt の **131 entries が 100% URL join**
  することを実機確認 (`.md` suffix strip で一致)
- `search "test"` は join 警告無しで動作

## [0.4.0] - 2026-04-15

- Add `search-index` subcommand to all three parse scripts. Replaces the
  Agent's previous habit of running `grep` over `llms.txt` to locate pages by
  keyword. Ranks pages against title / description (and tags / H1-H2 headings
  for AI SDK) with case-insensitive AND scoring. On `parse-ai-sdk.py`, the
  existing `search` subcommand is renamed and kept as an alias for backwards
  compatibility
- Add `search-content` subcommand to all three parse scripts. Performs
  section-level AND keyword search across `llms-full.txt` bodies (AI SDK /
  Claude) or lazily-fetched pages listed in `--pages` (Firebase, which has no
  `llms-full.txt`). Returns `heading_path`, a snippet with `→` markers on hit
  lines, matched keywords, per-section hit count, source URL, and a grand
  total so the Agent can jump straight to `content` without a follow-up grep
- Promote `search_index_entries` / `search_content_in_body` / `score_entry`
  to `_common.py` so all three sources share one search implementation.
  `parse-ai-sdk.py` drops its private `score_document` helper; the
  equivalent scoring weights (title 10/5, tags 4, description 2, headings 1,
  all-keyword bonus 10) now live in `score_entry`
- Section-level AND semantics: `search-content` requires every query keyword
  to appear somewhere within the same section before it's reported. Sections
  with 20+ hit lines are truncated to the first three with a trailing
  "… (N more hits in this section)" marker to keep output scannable
- Rewrite three SKILL.md files around the new entry points (search-index →
  sections/search-content → content), explicitly forbid the common
  grep/Read-lines fallbacks, and document the `llms.txt` / `llms-full.txt`
  `doc_index` divergence in Claude docs (search-content is the safe chain
  because its `doc_index` is the one sections/content use). Bump skill
  versions: ai-sdk 3.1.0 / claude-docs 2.1.0 / firebase 1.1.0
- Update README subcommand table, dev-test commands, and maintenance notes
  to reflect the new entry points and the AND semantics of `search-content`

## [0.3.0] - 2026-04-15

- Extract shared parser / fetch / output helpers into `scripts/_common.py`
  (`FenceTracker`, `extract_sections`, `extract_content`, `parse_llms_index`,
  `fetch_url`, `load_lines`, `die*` error helpers, `print_metadata_header`,
  `next_hint`, and argparse skeleton helpers). The three `parse-*.py` scripts
  are now thinner and consistent in behavior
- Fix `Next:` hint in `parse-ai-sdk.py` (3 call sites) and `parse-claude-docs.py`
  (2 call sites) which referenced the pre-rename script name
  (`parse-llms-txt.py`). Firebase was already correct; all three now derive the
  hint from `sys.argv[0]`
- No user-visible behavior change beyond the `Next:` hint fix; all other
  subcommand stdout/stderr is byte-identical to 0.2.0
- Unify 3 SKILL.md structure: add `context: fork` / `model: sonnet` frontmatter
  and "出力フォーマット" / "ルール" sections to `researching-claude-docs`;
  patch-bump SKILL versions (ai-sdk 3.0.1 / claude-docs 2.0.1 / firebase 1.0.1)
- Update README: Python requirement corrected to 3.10+ (parse-\*.py uses PEP 604
  syntax; only `_common.py` is 3.8+-compatible via `from __future__ import annotations`);
  add `scripts/_common.py` row to Components table and a paragraph on the shared
  helper layer to the maintenance notes
- Use `os.path.realpath(__file__)` (not `abspath`) when prepending the script
  directory to `sys.path` in the three `parse-*.py` scripts, so symlinked
  invocations cannot be shadowed by an unrelated `_common.py` sitting next
  to the symlink. Verified with an adversarial test (Codex Review P2 feedback
  on PR #3)
- Thread `min_level` through `_common.extract_content` (default 2) and have
  `parse-ai-sdk.py` pass `min_level=1` explicitly. The previous hardcoded
  `min_level=1` meant `cmd_sections` (H2+) and `cmd_content`'s internal
  heading lookup (H1+) disagreed in `parse-firebase.py`, which hands the raw
  page (H1 included) to `extract_content` — a Firebase page with an H1 and
  an H2 sharing the same title could have `content` match the H1 and return
  nearly the whole document instead of the intended H2 section. Claude docs
  and Firebase `content` output is now byte-identical to 0.2.0 again (Codex
  Review P2 feedback on PR #3 commit `e449a21`)

## [0.2.0] - 2026-04-15

- Add `researching-firebase` skill (Firebase docs progressive loader)
- Add `parse-firebase.py` script (per-page on-demand fetch; no llms-full.txt available)
- Use collision-resistant cache filenames (readable path + sha1 hash suffix) so
  Firebase URLs differing only by `/` vs `_` no longer share a cache file
- Update plugin description and keywords to include Firebase
- Update marketplace.json entry and root README

## [0.1.0] - 2026-04-14

- Initial release
- `researching-claude-docs` skill (Claude Code + Platform docs)
- `researching-ai-sdk` skill (Vercel AI SDK docs)
- Script paths updated to use `${CLAUDE_PLUGIN_ROOT}`
