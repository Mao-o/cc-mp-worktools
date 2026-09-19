# Contributing

> ドキュメントは日本語です。Issue / PR も日本語で構いません。

## 前提

- **Python 3.11+** (PATH 上の `python3`) — hook は標準ライブラリのみで動きます
  (サードパーティパッケージを追加しないでください)
- **git**
- **Claude Code CLI** — `claude plugin validate` と `--plugin-dir` 起動に使います

## リポジトリ構成

```
.
├── .claude/sensitive-files-guardrail/patterns.txt   # repo 同梱の除外パターン (commit 対象)
├── .claude-plugin/marketplace.json   # marketplace カタログ (entry 一覧)
├── plugins/<plugin-name>/            # plugin 本体 (1 ディレクトリ = 1 plugin)
│   ├── .claude-plugin/plugin.json    # plugin manifest (version はここだけ)
│   ├── hooks/hooks.json              # hook 登録
│   ├── hooks/<hook-name>/            # hook 実装 + tests/
│   ├── README.md
│   └── CHANGELOG.md
├── scripts/                          # repo 共通の検証スクリプト
├── CHANGELOG.md                      # marketplace レベルの変更のみ
└── Makefile                          # validate / test のショートカット
```

`.claude/` は原則 gitignore していますが、
`.claude/sensitive-files-guardrail/patterns.txt` **だけは例外的に commit** しています。
テスト fixture のダミー鍵を機密判定から外す設定を、貢献者・CI・worktree セッションで
共有するためです (無視したままだと置いた本人の手元でしか効きません)。このファイルは
**保護を緩める方向にも働く**ので、変更は差分レビューの対象です。

## 開発フロー

### 1 plugin だけをロードして試す

marketplace への登録は不要です。

```bash
claude --plugin-dir ./plugins/<plugin-name>
```

### 検証

```bash
make validate   # marketplace + 各 plugin の validate + 整合性チェック
make test       # 全 plugin の unit test (= scripts/test-all.sh)
```

`make validate` が実行する内容:

| コマンド | 検証内容 |
|---|---|
| `claude plugin validate .` | marketplace manifest のスキーマ |
| `claude plugin validate plugins/<name>` | 各 plugin manifest のスキーマ |
| `scripts/check_codex_manifest_version.py` | Codex 向け manifest がある plugin の version 一致 |
| `scripts/check_marketplace_entry_sync.py` | entry と `plugin.json` の `description` / `keywords` 一致 |

**warning もゼロに保ってください。** warning を許容すると本当の欠落 (version /
description / author 未設定) が埋もれます。

個別の plugin のテストだけを流す場合は、テストを持つパッケージ root で:

```bash
cd plugins/<plugin-name>/hooks/<hook-name>
python3 -m unittest discover tests
```

### hook を実機で動かして確かめる

hook の発火はユニットテストでは確認できません。`--plugin-dir` で起動した
セッションで実際に該当ツールを使うか、hook のエントリポイントに envelope JSON を
標準入力で流してください:

```bash
echo '{"cwd": "'"$PWD"'", "tool_name": "Read", "tool_input": {"file_path": "..."}}' \
  | python3 plugins/<plugin-name>/hooks/<hook-name> --tool read
```

## 変更を出すときの規約

### テスト

**挙動を変える修正には回帰テストを追加してください。** 既存の `tests/` の書式に
合わせます (標準ライブラリの `unittest` のみ)。

ガードやバリデータを追加するときは、**それが空チェックでないこと**を確かめてください。
「フィールドが無ければ比較をスキップ」のような実装は、対象が消えた瞬間に構造的に常に
0 件で通過します。検査対象を壊した状態で **fail することを 1 度は確認**してください。

### version

- **version は `plugin.json` 側にだけ書きます。** marketplace entry には書きません
- 両方に書くと `plugin.json` の値が使われ、marketplace entry 側の bump は無視されます
  (「bump したつもりで配布されていない」事故の原因)
- bump 幅: 挙動変更・機能追加は **minor**、修正のみは **patch**
- **version を bump しないと既存利用者に更新が届きません** (version が更新判定の
  キーです)

### CHANGELOG

| 変更の種類 | 書く場所 |
|---|---|
| plugin の機能追加・修正 | `plugins/<name>/CHANGELOG.md` |
| entry の追加 / rename / 削除、root docs、CI、`scripts/`、`.gitignore` | root の `CHANGELOG.md` |

両方に同じ内容を書かないでください。

### marketplace entry を触るとき

`marketplace.json` の entry は `plugin.json` と `description` / `keywords` を
**一致させる**必要があります。`claude plugin list --available` の一覧は entry の値
だけを見ており、entry から省くと `plugin.json` に fallback せず表示から消えます。
食い違ったら **entry 側を `plugin.json` に合わせて**ください
(`scripts/check_marketplace_entry_sync.py` が検証します)。

### plugin を追加するとき

1. `plugins/<name>/.claude-plugin/plugin.json` (`name` / `version` / `description` /
   `author` / `license` / `keywords`)
2. `plugins/<name>/hooks/hooks.json` — 自 plugin のファイル参照は
   `${CLAUDE_PLUGIN_ROOT}` 経由で書く (plugin ルートの外への相対参照は、install 時の
   キャッシュコピーで壊れます)
3. `plugins/<name>/README.md` と `CHANGELOG.md`
4. `.claude-plugin/marketplace.json` に entry (`name` / `displayName` / `source` /
   `description` / `category` / `keywords`)
5. root `README.md` の Plugins 表と、**外部送信があるなら Privacy & data flow 表**に
   追記
6. `make validate && make test`

### PR

- **1 PR = 1 plugin** を目安にしてください (同じ `CHANGELOG.md` を複数の作業で
  同時に触ると必ず衝突します)
- commit message は既存の慣例に合わせます: `fix(<plugin>): 要約 (vX.Y.Z)` /
  `feat(<plugin>): ...` (日本語可)
- CI (`.github/workflows/validate.yml`) が green であること
- PR には **何を / なぜ / どうテストしたか**を書いてください。裏が取れなかった点は
  隠さず「◯◯ 側に倒した」と明記してください (レビュアーが一次情報を持っていれば
  そこで解消できます)
- PR には自動レビュー (Codex) が走ることがあります。指摘は「直す」か「直さない理由を
  書く」のどちらかで必ず応答してください

### ドキュメントの書き方

- **公開ドキュメントに個人環境の情報を書かない** — 実在するローカルパス
  (`/Users/...` 等)、社内プロジェクト名、個人のアカウント名。例が必要なときは
  `/path/to/project` のようなダミーを使います
- **外部送信に関わる記述は実装で裏を取る。** 「送信していないと書いてあるのに実際は
  送信している」が最悪の失敗なので、README の Privacy & data flow 表を変えるときは
  該当コードの行を確認してください
- 図解が必要なときは Mermaid を使ってください (ASCII art の箱・矢印は使いません)。
  ディレクトリ構成だけなら上記のような ASCII ツリーで構いません

## セキュリティ上の問題を見つけたら

Issue ではなく [SECURITY.md](SECURITY.md) の手順で報告してください。
