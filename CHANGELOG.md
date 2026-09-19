# Changelog (marketplace)

このファイルは **marketplace 全体**の変更履歴です。記録するのは次の 4 種だけです:

- plugin entry の**追加 / rename / 削除** (`.claude-plugin/marketplace.json`)
- **root ドキュメント**の方針変更 (README / CLAUDE.md / SECURITY.md / CONTRIBUTING.md)
- **CI / 共通スクリプト**の変更 (`.github/workflows/` / `scripts/` / `Makefile`)
- **repo 衛生**の変更 (`.gitignore` / LICENSE / repo 同梱の設定ファイル)

個々の plugin の機能変更・修正は**各 plugin の `CHANGELOG.md`** に書き、ここには
書きません (`plugins/<name>/CHANGELOG.md`)。plugin を 1 つ直すたびに 2 ファイルを
更新する運用は破綻するためです。

見出しは **日付 (`## YYYY-MM-DD`)** です。marketplace 自体はバージョン番号を持たず
(更新判定のキーは各 plugin の `plugin.json` の `version`)、semver を振ると
「plugin のバージョン」と混同されるため採っていません。

## 2026-09-19

このファイルの開始時点。以降の marketplace レベルの変更をここに追記していきます。

### Added

- root に `CHANGELOG.md` (このファイル) / `SECURITY.md` / `CONTRIBUTING.md` を新設。
- README に「Privacy & data flow」表 (plugin ごとの外部送信の有無・送信先・送る内容・
  無効化方法) を追加。
- 各 plugin entry に `displayName` を設定 (UI 表示名)。
- marketplace entry と `plugin.json` の `description` / `keywords` の同期を検証する
  `scripts/check_marketplace_entry_sync.py` を追加し、CI と `make validate` に組み込み。
- sensitive-files-guardrail の repo 同梱パターン tier
  (`.claude/sensitive-files-guardrail/patterns.txt`) を commit 対象にし、テスト
  fixture のダミー鍵をこのリポジトリ全体で除外対象として共有。

### Changed

- entry 側の `description` / `keywords` を各 `plugin.json` の値に揃えた
  (`plugin.json` を single source of truth として扱う)。
- README / CLAUDE.md の ref 固定の説明を修正。到達不能な tag を例示していたのをやめ、
  `@<ref>` が固定するのは **marketplace の checkout** であり plugin のバージョンとは
  独立であることを明記。

### 開始時点の plugin バージョン

| Plugin | Version |
|---|---|
| `external-ai-assist` | 0.11.0 |
| `file-split-advisor` | 0.5.0 |
| `llms-docs` | 0.24.1 |
| `sensitive-files-guardrail` | 0.33.1 |
| `session-facts` | 0.12.0 |
| `verify-cloud-account` | 0.15.0 |

これ以前の履歴は各 plugin の `CHANGELOG.md` を参照してください。
