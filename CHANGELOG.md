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

## 2026-11-01

### Added

- CI に **skill の `allowed-tools` 検査**を追加 (`scripts/check_skill_allowed_tools.py`、
  `.github/workflows/validate.yml` の `validate` job)。`plugins/*/skills/*/SKILL.md` の
  Bash の付与は完全一致の形だけを許し、`Bash(... *)` と `Bash` 単独を FAIL にする
  (`allowed-tools` は制限ではなく「確認なしで使える」付与なので、`*` があると
  読み取り専用のつもりの付与が書き込み系まで通る)。検査導入前から Bash 単独を
  付けていた 5 skill (llms-docs の 4 skill と codex-pr-review の 1 skill) は
  script の ALLOWLIST に置き、絞り終えたら外す (対応する付与が無い許可リストは
  FAIL)。script のテストは `scripts/tests/` に置き、同じ job で実行する
- 同じ検査は、frontmatter を読めない形を黙って通さず FAIL にする (fail closed)。
  行末・列 0 のコメント、複数行の flow list、引用符付きの scalar、キーの別表記
  (`allowed-tools :` / `"allowed-tools":`)、先頭の BOM、付与の末尾空白を読み、
  付与の形に合わない項目・括弧の中の ` #` (YAML ではコメントになる)・キーの重複・
  キーとして読めない `allowed-tools` の行・閉じていない flow list・値に `Bash` が
  あるのに Bash の付与を読み取れない場合を FAIL にする。既定の root で検査したときは、
  許可リストにある skill の rename・削除も FAIL にする
- `make validate` に同じ検査と `scripts/tests/` のテストを足し、`CONTRIBUTING.md` の
  検査の表に載せた

### Changed

- `llms-docs-canary.yml` のコメントを現状に合わせた (削除済みの旧 script を
  「削除予定の互換 shim」と書いたままだった。挙動の変更なし)

## 2026-10-13

### Changed

- plugin `verify-cloud-account` を **`cloud-account-guard` に rename** (0.23.0)。
  `.claude-plugin/marketplace.json` の entry (name / displayName / source) を差し替え、
  README の plugin 一覧・"Renamed / removed plugins" 節 (設定パスと環境変数の移し方) と
  SECURITY.md の節名を更新。利用者がいないため、旧名 entry を 1 リリース残す手順は省略した。
  CLAUDE.md の旧規約 tag の説明 (`v0.3.2`) には現在の名前を併記

## 2026-09-23

### Added

- CI に **Windows 探索用 job** を追加 (`.github/workflows/validate.yml` の
  `tests-windows`)。sensitive-files-guardrail の 2 suite
  (`check-sensitive-files` / `redact-sensitive-reads`) を `windows-latest` /
  Python 3.12 の matrix で回す。0.34.0 で「SIGALRM が無い環境では全 tool 呼出を
  deny」という実質 Windows 無効化ゲートを撤去したが、CI は ubuntu-latest のみで
  未検証だったのが動機。**当面は赤である前提の調査用**なので
  `if: github.event_name == 'workflow_dispatch'` で手動起動に限定し、
  pull_request / push のゲートには載せない。`continue-on-error` は付けず
  (赤を隠さない)、`tag-plugin-releases` の `needs` にも入れない。
  Windows が安定して green になったら `tests` job の matrix へ統合して畳む。

### Changed

- **`tests-windows` を PR / push のゲートに昇格**。sensitive-files-guardrail
  0.34.1 (encoding を UTF-8 固定) / 0.34.2 (posix 前提の解消) で両 suite が
  windows-latest で green になったため、`if` を `workflow_dispatch` 限定から
  `schedule` 以外 (= PR / push / 手動) に広げ、`tag-plugin-releases` の `needs` にも
  加えた (Windows で赤の commit に SFG の version を指す tag を残さない)。
  `tests` job の matrix へは統合しない — あちらは `scripts/test-all.sh` で全 plugin を
  列挙する作りで、SFG 以外の plugin の Windows 対応は未着手のため
  README の CI 説明 (「手動起動限定・PR ゲート外」) も更新
- `.gitignore` に `.beads.gate.lock` を追加。bd の merge-slot gate が作る実行時の
  ロックファイルで、`.beads/` の外 (repo root) に置かれるため既存の ignore に
  掛かっていなかった

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
- **リリース tag 規約を `<plugin-name>/v<version>` に定めた** (例
  `sensitive-files-guardrail/v0.33.1`)。`plugin.json` の `version` と 1:1 に対応する。
  main への push で CI が対応する tag を annotated tag として自動作成・push する
  (`.github/workflows/validate.yml` の `tag-plugin-releases` job。validate / lint /
  tests が green のときだけ、既存 tag は打ち替えない)。判定・作成ロジックは
  `scripts/backfill-plugin-tags.sh` に一本化 (既定 dry-run / `--apply` で実行)。
  2026-06-13 以降のリリースが未 tag のまま積み上がり、`git tag --contains` で
  出荷の有無を追えなくなっていたのが動機。
- CI に **`ruff check`** の job を追加 (root `pyproject.toml` の `[tool.ruff]`、
  規則は E / F / W、`target-version = "py311"`、行長 E501 は無視)。CI は ruff の
  version を固定する。ローカルは `make lint`。
- unit test の CI 実行を **Python 3.11 / 3.12 / 3.13 の matrix** に拡張し、
  `validate` job から独立した `tests` job に分離した (`fail-fast: false`)。
- `Makefile` に `lint` target を追加 (`RUFF` 変数で実行系を差し替え可能)。
- `CONTRIBUTING.md` に「lint / 型チェック」節と「`claude plugin eval` について」節、
  「リリース tag」節を追加。`README.md` / `CLAUDE.md` にもリリース tag 規約を記載。

### Changed

- **型チェッカ (mypy / pyright) は導入しないことを決めた** — 理由は
  `CONTRIBUTING.md` の「lint / 型チェック」節に記録。
- **`claude plugin eval` は CI に導入しないことを決めた** — 認証必須 + 実モデル
  呼び出しによる課金、fork PR で動かない secret 依存、要求 CLI version が固定版より
  新しい点が折り合わないため。理由は `CONTRIBUTING.md` に記録。
- `.github/workflows/validate.yml` に top-level `permissions: contents: read` を設定し、
  書き込みは `tag-plugin-releases` job に限定した。
- `validate` job の Python バージョン固定理由のコメントを実態に合わせた。従来は
  「各 plugin の unittest が tomllib (3.11+) を使うため」と書いていたが、tomllib を
  使うのは sensitive-files-guardrail の 1 モジュールの **optional import** だけで
  (未搭載時は「未対応」扱い、テストも skip する)、下限を決めているのは repo の
  宣言方針 (Python 3.11+) だった。
- `.gitignore` / `make clean` に `.ruff_cache` を追加。
- 旧規約の bare tag (`v0.2.0` 〜 `v0.14.0`) は残すが今後は打たない方針を明記した
  (marketplace 全体を指すものと個別 plugin を指すものが名前から区別できず混在して
  いた)。
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
