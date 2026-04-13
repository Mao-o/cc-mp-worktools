# doc-researcher

Claude 公式ドキュメントと AI SDK 公式ドキュメントを `llms.txt` 経由で段階的に調査するスキル集。
全文読み込みを避け、インデックス → セクション → コンテンツの順で必要な部分だけを取得する。

## Skills

| スキル | 対象 | エントリポイント |
|--------|------|-----------------|
| `researching-claude-docs` | Claude Code / Claude Developer Platform | `fetch-index` でページ一覧 → 絞込み |
| `researching-ai-sdk` | Vercel AI SDK (ai-sdk.dev) | `search` でキーワード検索 → 絞込み |

## Components

| 種類 | パス |
|------|------|
| Skill | `skills/researching-claude-docs/SKILL.md` |
| Skill | `skills/researching-ai-sdk/SKILL.md` |
| Script | `scripts/parse-claude-docs.py` |
| Script | `scripts/parse-ai-sdk.py` |

## 前提条件

- `python3` (3.9+)
- ネットワーク到達性（初回取得時に外部 llms.txt をダウンロード）
- `/tmp` 書込み権限（キャッシュ保存先）

## 動作確認

リポジトリルートから:
```bash
./scripts/dev.sh doc-researcher
```

スクリプト単体テスト:
```bash
# python3 <script> の形式で呼び出す（直接実行 ./script.py は非対応）
python3 plugins/doc-researcher/scripts/parse-claude-docs.py fetch-index
python3 plugins/doc-researcher/scripts/parse-ai-sdk.py search /tmp/ai-sdk-llms.txt "streaming"
```

## キャッシュ

| スキル | キャッシュファイル |
|--------|-------------------|
| claude-docs (Code) | `/tmp/claude-code-llms.txt`, `/tmp/claude-code-llms-full.txt` |
| claude-docs (Platform) | `/tmp/claude-platform-llms.txt`, `/tmp/claude-platform-llms-full.txt` |
| ai-sdk | `/tmp/ai-sdk-llms.txt` |

最新版が必要な場合は該当ファイルを `rm` してから再実行する。

## 既知の制約

- 全文読み込み禁止: 必ず段階的に絞り込むこと
- コードフェンス保護: コードブロックの途中分割を自動防止
- テーブル保護 (claude-docs のみ): Markdown テーブルの途中分割を自動防止

## 保守メモ

`parse-claude-docs.py` と `parse-ai-sdk.py` はドキュメント分割方式が根本的に異なるため
独立したスクリプトとして管理している。バグ修正や機能改善を行う際は両方を確認すること。
