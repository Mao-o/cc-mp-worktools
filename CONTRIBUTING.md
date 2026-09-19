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
├── pyproject.toml                    # ruff の設定のみ (パッケージは配布しない)
├── CHANGELOG.md                      # marketplace レベルの変更のみ
└── Makefile                          # validate / lint / test のショートカット
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
make lint       # ruff check (設定は pyproject.toml)
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

CI は unit test を **Python 3.11 / 3.12 / 3.13 の matrix** で回します
(`fail-fast: false` なので「3.11 だけ落ちた」のか「全部落ちた」のかが 1 回の run で
分かります)。

- **3.11** = README / この文書で宣言している下限。ここが落ちたら宣言が嘘になります
- **3.12** = 以前 CI が唯一実行していた版 (退行の基準)
- **3.13** = 現行の新しい安定版 (先行して壊れを拾う)

3.9 / 3.10 互換の shim (`from __future__ import annotations` を後付けする等) は
**追加しません**。下限は 3.11 です。

### lint / 型チェック

```bash
# ruff を PATH に入れる (CI と同じ版)
pip install 'ruff==0.16.8'
make lint

# version manager 経由で使う場合は呼び出し方を差し替えられます
make lint RUFF="mise exec ruff@0.16.8 -- ruff"
```

- 設定は root の `pyproject.toml` の `[tool.ruff]` にあります。`[project]` や build
  backend は**書きません** — このリポジトリは Python パッケージを配布しないので、
  `pyproject.toml` は ruff の設定置き場としてだけ存在します
- 有効な規則は **E / F / W** のみ、`target-version = "py311"`。行長 (E501) は
  無視します (日本語コメントを機械的に折ると読みにくくなるため)
- CI は ruff の version を固定しています。ruff は版ごとに規則が追加・強化されるため、
  固定しないと同じ commit が実行日次第で落ちます
- `pyproject.toml` の `ignore` には、**導入時点で既に違反していた規則**
  (F401 / E741 / F541 / E402 / E401 / F811、24 ファイル 34 件) が入っています。
  これらを直すと plugin 本体に触る変更になるため、lint の導入と挙動リスクのある
  修正を同じ PR に混ぜないよう分けました。plugin 側を掃除した PR で 1 つずつ
  外していく前提です
- 抑制しても **F821 undefined-name / F841 unused-variable / F632 / W605 /
  E711 / E712 など実バグに直結する規則は有効**です。新しく `ignore` を増やす
  提案をするときは、それが「空チェック化」にならないか確認してください

**型チェッカ (mypy / pyright) は導入していません。** 理由:

- plugin の hook は `python3 <hook-dir>` で起動される**独立した実行単位**で、
  package として import される library ではありません。`sys.path` を実行時に
  組み立てるブートストラップを各 hook が持っており、型チェッカに解決させるには
  hook ごとの `mypy_path` / `namespace_packages` 設定を人手で維持する必要があります。
  設定の維持コストが、得られる検出に見合いません
- hook は**標準ライブラリのみ**を使うため、型スタブ不足による
  `ignore-missing-imports` の大量付与という典型的な導入動機もありません
- 現状の退行検出は「全 plugin のユニットテスト (計 4,484 件) を 3 バージョンで回す」
  方に寄せています。型チェックを足すなら、まず lint の `ignore` を空にするのが先です

### `claude plugin eval` について

**導入していません。** 公式の plugin eval はプラグインの振る舞いを採点して CI gate に
使える仕組みですが、このリポジトリでは次の 3 点が折り合いません:

1. **実際のモデル呼び出しが発生し、課金されます。** eval run と judge grader は
   認証情報 (`ANTHROPIC_API_KEY` 等) を使った実リクエストで、既定では 1 ケースあたり
   「plugin 有無 2 通り × 複数 run」を走らせます。plugin 6 個ぶんを PR ごとに回すと
   費用が CI の他のチェックと桁違いになります
2. **API キーを CI に置く必要があります。** この repo は plugin を配布するだけで
   secret を必要としておらず、fork からの PR で使えない gate を足すことになります
3. **要求される CLI version が、validate 用に固定している版より新しいです。** 公式は
   plugin eval に v2.1.269 以降を要求しており、CI が固定しているのは 2.1.251 です
   (固定は `claude plugin validate` の pass/fail を実行日に依存させないためのもので、
   eval のために上げると固定の理由と衝突します)。判断を見直すときはこの 2 つの
   version を現在値で確認してください

現状の gate は `claude plugin validate` (manifest スキーマ) + ユニットテスト + ruff で、
いずれも**認証不要・無料・決定論的**です。eval を入れるとしても、PR ごとではなく
「手動 workflow_dispatch か週次で、予算上限付き」の別ワークフローにするのが前提です。

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

### リリース tag

tag 名は **`<plugin-name>/v<version>`** です (例: `sensitive-files-guardrail/v0.33.1`)。
`plugin.json` の `version` と 1:1 に対応します。

- **手で打たないでください。** main への push で CI
  (`.github/workflows/validate.yml` の `tag-plugin-releases` job) が、各 plugin の
  `plugin.json` の version に対応する tag が無ければ annotated tag を作って push します。
  validate / lint / tests が green のときだけ打ちます
- 既存 tag は**打ち替えません**。bump を忘れた場合は「打つ tag が無い」だけなので、
  次に bump した push で作られます
- 判定・作成ロジックは `scripts/backfill-plugin-tags.sh` に一本化しています
  (既定は dry-run、`--apply` で作成 + push)。規約導入時の遡及付与と通常運用で同じ
  コードを使うため、列挙ロジックが二重管理になりません
- **旧規約の `v0.2.0` 〜 `v0.14.0` は残していますが、今後は打ちません。**
  marketplace 全体を指すもの (`v0.2.0`) と個別 plugin のリリースを指すもの
  (`v0.3.2` / `v0.11.0` / `v0.12.0` / `v0.14.0`) が名前から区別できず混在して
  いたのが、形式を変えた理由です
- tag は**更新配布のキーではありません** (それは `plugin.json` の `version`)。
  「どの版がどの commit に入っているか」を後から辿るための印です

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
6. `make validate && make lint && make test`

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
