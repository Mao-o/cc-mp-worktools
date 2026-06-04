# llms-docs

Claude 公式ドキュメント、AI SDK 公式ドキュメント、Firebase 公式ドキュメントを `llms.txt` 経由で段階的に調査するスキル集。
全文読み込みを避け、**キーワード検索 → セクション特定 → コンテンツ取得**の順で必要な部分だけを取得する。

## Skills

| スキル | 対象 | 推奨エントリポイント |
|--------|------|---------------------|
| `researching-claude-docs` | Claude Code / Claude Developer Platform | `search` (URL-join 統合検索) → `content <page_ref> "<heading_path>"` |
| `researching-ai-sdk` | Vercel AI SDK (ai-sdk.dev) | `search` (top N 候補 + 本文 hits) → `content <page_ref> "<heading_path>"` |
| `researching-firebase` | Firebase (firebase.google.com) | `search` (top N on-demand fetch + 本文 hits) → `content <page_ref> "<heading_path>"` |

3 script で `search` / `search-index` / `search-content` / `sections` / `content` / `fetch-index` の
サブコマンド名・引数・`<page_ref>` 形式が統一されている (0.7.0)。

## Components

| 種類 | パス |
|------|------|
| Skill | `skills/researching-claude-docs/SKILL.md` |
| Skill | `skills/researching-ai-sdk/SKILL.md` |
| Skill | `skills/researching-firebase/SKILL.md` |
| Script | `scripts/parse-claude-docs.py` |
| Script | `scripts/parse-ai-sdk.py` |
| Script | `scripts/parse-firebase.py` |
| Shared | `scripts/_common.py` (FenceTracker / extract_sections / fetch_url ほか共通ヘルパー) |

## 前提条件

- `python3` (3.10+) — `parse-*.py` は PEP 604 記法 (`list[X]` / `X | None`) を使用。`scripts/_common.py` のみ `from __future__ import annotations` で 3.8+ 互換
- ネットワーク到達性（初回取得時に外部 llms.txt をダウンロード）
- `/tmp` 書込み権限（キャッシュ保存先）

## 動作確認

リポジトリルートから:
```bash
claude --plugin-dir ./plugins/llms-docs
```

スクリプト単体テスト:
```bash
# python3 <script> の形式で呼び出す（直接実行 ./script.py は非対応）

# search (推奨入口: 候補絞り込み + 本文 hits を 1 コマンド)
python3 plugins/llms-docs/scripts/parse-claude-docs.py search "hook matcher"
python3 plugins/llms-docs/scripts/parse-ai-sdk.py search "streamText onFinish"
python3 plugins/llms-docs/scripts/parse-firebase.py search "Firestore query limit"

# search-index (候補だけ取得したいとき; 軽量 llms.txt ベース)
python3 plugins/llms-docs/scripts/parse-claude-docs.py search-index "hook matcher"
python3 plugins/llms-docs/scripts/parse-ai-sdk.py search-index "streamText onFinish"
python3 plugins/llms-docs/scripts/parse-firebase.py search-index "Firestore query limit"

# search-content (特定ページ内だけ本文検索)
python3 plugins/llms-docs/scripts/parse-claude-docs.py search-content "matcher PreToolUse" --page-ref hooks
python3 plugins/llms-docs/scripts/parse-ai-sdk.py search-content "useChat onFinish" --page-ref 153
python3 plugins/llms-docs/scripts/parse-firebase.py search-content "orderBy limit" --page-ref 2972

# fetch-index (フォールバック)
python3 plugins/llms-docs/scripts/parse-claude-docs.py fetch-index
python3 plugins/llms-docs/scripts/parse-firebase.py fetch-index --limit 10
```

## キャッシュ

| スキル | キャッシュファイル |
|--------|-------------------|
| claude-docs (Code) | `/tmp/claude-code-llms.txt`, `/tmp/claude-code-llms-full.txt` |
| claude-docs (Platform) | `/tmp/claude-platform-llms.txt`, `/tmp/claude-platform-llms-full.txt` |
| ai-sdk | `/tmp/ai-sdk-llms.txt` |
| firebase | `/tmp/firebase-llms.txt` (index), `/tmp/firebase-docs/` (per-page) |

最新版が必要な場合は該当ファイルを `rm` してから再実行する。

## 既知の制約

- 全文読み込み禁止: 必ず段階的に絞り込むこと
- コードフェンス保護: コードブロックの途中分割を自動防止
- テーブル保護 (claude-docs / firebase): Markdown テーブルの途中分割を自動防止

## 設計判断: subagent fork + Sonnet は維持する

3 SKILL とも `context: fork` + `model: sonnet` で起動する。これは spawn
オーバーヘッドと引き換えに以下を保証する設計判断:

- **context rot 防止**: llms.txt / llms-full.txt は数 MB あり、親 context に流すと
  数回の調査でメインセッションが肥大化 → 後続作業の精度が劣化する (context rot)。
  fork で隔離することで親には「調査結果の要約」だけが返る
- **正確性の優先**: 親 (例: Opus 4.7) の作業と SKILL の調査を分離し、要約モデルが
  混在しない決定論的な出力を得る (`researching-claude-docs` description で
  「verbatim 取得・幻覚回避」と明示している通り)
- **コスト分離**: Sonnet で動かすことで Opus 親セッションのトークン消費に乗らない

**軽量化方向 (fork 外し / 親 model 継承 / 軽量モード追加) は採用しない。**
spawn オーバーヘッドは受け入れ、低品質回答や context rot を避ける方を優先する。

「軽い質問でも WebFetch に流れる」課題への対応策は次の 3 系統で、いずれも
fork + Sonnet 構成を崩さない:

1. **description の充実** (`Use proactively` / `Triggers:` を 3 SKILL で揃える、0.6.0)
2. **`~/.claude/rules/` の `*-doc-first.md` rule** で「自身の知識より先に
   `researching-*` を呼ぶ」を明示 (ai-sdk / claude-docs / firebase)
3. **`search` 統合サブコマンドの提供** (0.7.0) で 1 コマンド完結の調査体験

WebFetch との比較メモ: WebFetch は要約モデル経由のため field 抜け落ち・幻覚の
リスクがあり、Anthropic API / Firebase / AI SDK のような schema を厳密に扱う
ドメインでは fork + Sonnet + 決定論的 grep の方が信頼できる。spawn 数秒 vs
要約幻覚で半日 debug を比較すれば、前者の方が安い。

## 保守メモ

`parse-claude-docs.py`、`parse-ai-sdk.py`、`parse-firebase.py` はドキュメント分割方式が
根本的に異なるため独立したスクリプトとして管理している。特に `parse-firebase.py` は
Firebase 側に `llms-full.txt` が存在しないため、index + per-page on-demand fetch 方式を
採用している。そのため Firebase の `search` は top N 件 (default 5) を順次 fetch する
ヒューリスティクスを持つ。`search-content` の `--page-ref` は単数指定 (省略時は全ページ
横断、ただし重いので明示指定推奨)。バグ修正や機能改善を行う際は 3 本すべてを確認すること。

3 script で API を 0.7.0 で揃えた: `search` / `search-index` / `search-content` /
`sections` / `content` / `fetch-index` の 6 サブコマンドが共通、`<page_ref>` は
int / URL slug / 完全 URL を受け付ける (ai-sdk のみ URL がないため int / title 部分一致)、
`--file` flag は省略時に cache を auto-fetch する。

共通ロジック (code-fence scanner / section & content extraction / llms.txt index parser /
HTTP fetch / エラーヘルパー / metadata header / Next hint / argparse skeleton /
**keyword search (search_index_entries, search_content_in_body, score_entry)**) は
`scripts/_common.py` に集約済み。新しい doc source を追加する際は、source 固有の
`split_documents` と表示層のみを書き、共通部分は `_common` から import すること。

`search-content` はセクション単位の AND 検索 — 指定した全キーワードが同じセクション内に
揃って出現するセクションのみを返す。OR 挙動（どれか 1 つでもマッチすれば hit）ではない
点に注意。
