# doc-researcher

Claude 公式ドキュメント、AI SDK 公式ドキュメント、Firebase 公式ドキュメントを `llms.txt` 経由で段階的に調査するスキル集。
全文読み込みを避け、**キーワード検索 → セクション特定 → コンテンツ取得**の順で必要な部分だけを取得する。

## Skills

| スキル | 対象 | 推奨エントリポイント |
|--------|------|---------------------|
| `researching-claude-docs` | Claude Code / Claude Developer Platform | `search-index` でページを絞込み → `search-content` で本文横断 |
| `researching-ai-sdk` | Vercel AI SDK (ai-sdk.dev) | `search-index` でドキュメントを絞込み → `search-content` で本文横断 |
| `researching-firebase` | Firebase (firebase.google.com) | `search-index` (必須の入口) → `search-content --pages` で候補ページの本文検索 |

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
claude --plugin-dir ./plugins/doc-researcher
```

スクリプト単体テスト:
```bash
# python3 <script> の形式で呼び出す（直接実行 ./script.py は非対応）

# search-index (軽量 llms.txt ベース)
python3 plugins/doc-researcher/scripts/parse-claude-docs.py search-index "hook matcher"
python3 plugins/doc-researcher/scripts/parse-ai-sdk.py search-index /tmp/ai-sdk-llms.txt "streamText onFinish"
python3 plugins/doc-researcher/scripts/parse-firebase.py search-index "Firestore query limit"

# search-content (本文横断キーワード検索)
python3 plugins/doc-researcher/scripts/parse-claude-docs.py search-content /tmp/claude-code-llms-full.txt "matcher PreToolUse"
python3 plugins/doc-researcher/scripts/parse-ai-sdk.py search-content /tmp/ai-sdk-llms.txt "useChat onFinish"
python3 plugins/doc-researcher/scripts/parse-firebase.py search-content "orderBy limit" --pages 2972

# fetch-index (フォールバック)
python3 plugins/doc-researcher/scripts/parse-claude-docs.py fetch-index
python3 plugins/doc-researcher/scripts/parse-firebase.py fetch-index --limit 10
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

## 保守メモ

`parse-claude-docs.py`、`parse-ai-sdk.py`、`parse-firebase.py` はドキュメント分割方式が
根本的に異なるため独立したスクリプトとして管理している。特に `parse-firebase.py` は
Firebase 側に `llms-full.txt` が存在しないため、index + per-page on-demand fetch 方式を
採用している。そのため Firebase の `search-content` は `--pages` で候補ページを明示指定する
設計になっている。バグ修正や機能改善を行う際は 3 本すべてを確認すること。

共通ロジック (code-fence scanner / section & content extraction / llms.txt index parser /
HTTP fetch / エラーヘルパー / metadata header / Next hint / argparse skeleton /
**keyword search (search_index_entries, search_content_in_body, score_entry)**) は
`scripts/_common.py` に集約済み。新しい doc source を追加する際は、source 固有の
`split_documents` と表示層のみを書き、共通部分は `_common` から import すること。

`search-content` はセクション単位の AND 検索 — 指定した全キーワードが同じセクション内に
揃って出現するセクションのみを返す。OR 挙動（どれか 1 つでもマッチすれば hit）ではない
点に注意。
