# Changelog

## 0.6.0

**実行コンテキスト (runtime/venv) の可視化 + 依存収集のハイブリッド化 (v0.6)**。
実セッションで「kaggle は入っているか」を調べた際、`.venv` 内に kaggle が
インストールされていたにもかかわらず Project Facts に venv/依存/runtime の情報が
無く、エージェントがグローバル基準で「未インストール」と誤検出した。出力に
実行コンテキストと主要依存を載せ、グローバル基準での誤判定を防ぐ。

> **制約**: session-facts は `.venv`/`venv` 内部 (インストール済みパッケージ) を
> git ls-files でも walk_files でも見られない (SKIP_DIRS)。よって依存検出は
> `requirements.txt` / `pyproject.toml` 等の**宣言**経由のみ。venv は「存在」を
> `pyvenv.cfg` で確認し、エージェントに確認を促す形で伝える。

1. **runtime/venv 検出** (`core/runtime.py` 新規, `collectors/runtime_env.py` 新規,
   `detectors/mise.py`, `renderer.py`, `core/context.py`) — mise/asdf のツール
   バージョン (`.mise.toml` / 旧 `mise.toml` / `.config/mise/config.toml` /
   `.tool-versions`)、`.python-version`、`.venv`/`venv` の存在 (`pyvenv.cfg`
   ガードで `venv/` という名のソースディレクトリ誤検出を防止) を検出し、header に
   1 行追加:
   `- runtime: mise (python 3.12); venv .venv present (python 3.12.3); run tools via .venv/bin/`。
   mise detector は共有 `has_mise()` 経由に変更し、従来拾えなかったドット無し
   `mise.toml` と `.config/mise/` を認識するようになった (返り値 `["mise"]` は不変)。
2. **major_dependencies のハイブリッド化** (`collectors/dependencies.py`) —
   従来の allow-list (IMPORTANT_DEPENDENCIES) マッチのみだと kaggle 等の宣言依存が
   出なかった。allow-list マッチを最優先 (tier 0) に並べ、枠 (`--max-major-deps`,
   既定 8) が余れば `requirements`/`pyproject` の直接宣言 runtime 依存で埋め (tier 1)、
   dev 依存 (pytest 等) は後置 (tier 2) する方式に変更。枠埋めは **Python のみ**に
   スコープ (JS package.json / pubspec / go.mod は uncurated なため allow-list 据置)。
   cap は tier 安定ソート**後**に適用し、source 順で遅く現れる allow-list マッチが
   切られないようにした。あわせて pyproject を tomllib 不使用の table-scoped パーサ
   (`[project] dependencies` / `[project.optional-dependencies]` / poetry 各テーブル /
   Pipfile グループ) で正確化。副次的に、旧「どこでもマッチ」正規表現が poetry の
   dev グループ依存を runtime と誤表示していた潜在バグも解消した。
3. **Likely Commands の runtime 補正** (`collectors/scripts.py`) — venv があれば
   `.venv/bin/python -m pytest`、mise-python なら `mise exec -- python -m pytest` を
   出す (venv 優先)。`uv run` / `poetry run` は自前 env 管理のため不変。pyproject も
   lockfile も無い bare-python (.py 比率検出) でも、runner が確定するときだけ pytest
   行を補う。

テスト 138 件 (新規 48 件: `test_runtime.py` / `test_scripts.py` 新設、
`test_dependencies.py` にハイブリッド/parser テスト追加)。

### Codex plugin 兼用対応 (hook + skill)

session-facts を **Codex plugin としても**配布できるよう、エージェント非依存の
コア (`hooks/session-facts/`) を無改造のまま、Codex 用の配線レイヤを追加した。
Codex の hooks framework は Claude Code とほぼ同型 (`SessionStart` +
`hookSpecificOutput.additionalContext`、plain stdout も additionalContext として
受理) のため、コアの再利用が成立する。

- **`.codex-plugin/plugin.json`** — Codex plugin manifest。`skills` / `hooks` /
  `interface` フィールドを持つ (`hooks: "./hooks/codex-hooks.json"`)。
- **`hooks/codex-hooks.json`** (新規) — `SessionStart` (matcher `startup|resume`)
  で `${PLUGIN_ROOT}/hooks/session-facts` を実行し自動注入 (Claude の SessionStart
  自動注入と同等)。Claude の `hooks/hooks.json` とは別ファイルで干渉しない。
  Claude 側の `--no-recent-commits` は付けない (Codex が git 情報を自動注入するか
  未確認のため、subagent と同じく recent_commits を残す保守側に倒す)。
- **`skills/session-facts/SKILL.md`** — オンデマンド再生成用の skill (構成変更後の
  手動更新)。自動注入は hook が担うため両者は補完関係。
- **`.agents/plugins/marketplace.json`** — ローカル marketplace 登録
  (`codex plugin marketplace add <repo>` 用)。

実機 `codex-cli 0.142.2` で検証済み (隔離 `CODEX_HOME`): `plugin add` で
`installed, enabled 0.6.0`、manifest/marketplace/hook を error なく受理。version は
plugin.json から解決。

> **保留**: `SubagentStart` hook は Codex の subagent matcher 値が未確認のため
> 見送り (誤 matcher での silent 失敗 / 全 subagent への過剰注入を避ける)。
> SessionStart 自動注入で主要価値はカバー。`--emit subagent-json` モードは
> Codex SubagentStart にも流用可能 (将来対応)。

### 見送り

- **tomllib fast-path** (Python ≥ 3.11 の構造的パース) — house style の正規表現
  一本を優先し、バージョン間差異リスクを回避。将来の選択肢。
- **setup.cfg `[options.extras_require]` の dev 取り込み** — dev 後置の主目的は
  他ソースで達成済みのため YAGNI。

## 0.5.0

**harness 注入との棲み分け + SubagentStart 注入修復 (v0.5)**。実セッションでの
出力評価フィードバック (実行者視点) を受け、harness が無条件注入する情報との
重複排除と、機能していなかった SubagentStart 注入の修復を行った。

1. **SubagentStart 注入修復** (`cli.py`, `hooks/hooks.json`) — SubagentStart では
   plain stdout がモデルに届かない (公式仕様: plain stdout の自動注入は
   SessionStart のみの特権。SubagentStart は `hookSpecificOutput.additionalContext`
   JSON が必須)。`--emit subagent-json` を追加し、hooks.json の SubagentStart
   (Explore/Plan) 側を JSON 包装に切替。従来登録は dead config だった
   (Explore subagent 自身にコンテキストを報告させる実測で確認)。
2. **recent_commits の SessionStart 抑制** (`core/context.py`,
   `collectors/git_progress.py`, `cli.py`, `hooks/hooks.json`) — main セッション
   には harness が gitStatus (直近 5 commit) を常時注入しており、recent_commits
   (3 件) はその完全サブセットだった。`--no-recent-commits` を SessionStart 側に
   のみ付与。subagent には harness の git 情報が一切注入されない (実測) ため、
   SubagentStart 側では維持する。
3. **purpose の dirname fallback 廃止** (`cli.py`) — fallback chain
   (package.json description → README 先頭行 → ディレクトリ名) の最終段は
   repo_root の再掲で情報量ゼロのため、field ごと省略する。
4. **Test Snapshot の「テスト無し」明示** (`collectors/tests.py`) —
   test_files=0 のとき code_files 単独表示 (ミスリード) をやめ、
   `- tests: none detected` の 1 行に置換。

テスト 90 件 (新規 9 件: `test_cli.py` 新設、git_progress / collectors に追加)。

## 0.4.0

**出力品質改善 (v0.4 ロードマップ P1〜P3、計 10 件)**。SessionStart injection の
選定基準 (`~/.claude/rules/claude/sessionstart-injection.md`) に沿って、出力の
圧縮・ノイズ削減・言語サポート拡張・進行情報の追加を行った。session-facts 初の
ユニットテスト一式 (77 件) を同梱。

### P1: 出力圧縮・ノイズ削減

1. **Structure ツリーの dynamic depth** (`core/tree.py`, `collectors/structure.py`) —
   固定 depth=3 をやめ、`--max-tree-lines` を超えない範囲で最も深い depth を
   自動選択 (1〜5)。`build_dir_tree` は MAX で 1 回だけ走らせ、`render_tree` を
   depth ごとに試して超過直前を採用する方式 (行数は depth に対し単調増加)。
   採用 depth は見出し `## Structure (dirs only, depth=N)` に反映。
2. **subtree モードの Structure 圧縮** (`collectors/structure.py`) —
   cwd != repo_root のとき、repo 全体の Structure は top-level dir 名のみ
   (depth=1) に圧縮し、詳細は cwd 配下の Subtree 側に寄せる。
3. **chain 圧縮** (`core/tree.py::render_tree`) — 子が 1 つだけの中間ディレクトリ
   を `a/b/c/` の 1 行に畳む。行数を稼ぎつつ可読性を維持。
4. **Repo-Specific Notes の汎用 note 抑制** (`collectors/repo_notes.py`) —
   `api-related files...` の閾値を `>= 5` から `>= 20` に引き上げ。ほぼ全 repo で
   出てノイズ化していたため、本当に api-heavy な repo でのみ出すようにした。
5. **test_dir の共通祖先集約** (`core/util.py::aggregate_paths`, `collectors/tests.py`) —
   sibling な test ディレクトリを `plugins/*/hooks/*/tests` のような glob 1 行に
   集約。単一の test_dir はそのまま個別表示。

### P2: 言語サポート拡張

6. **Flutter/Dart 対応** (`detectors/flutter.py` 新規, `core/constants.py`,
   `collectors/dependencies.py`, `collectors/scripts.py`) — tracked な
   pubspec.yaml 検出 (monorepo の `apps/<name>/pubspec.yaml` 等、repo root 直下
   以外も対象) で `stack: flutter, dart`、`.dart` を CODE_EXTENSIONS に追加
   (Service Entry Points / Test Snapshot に反映)、pubspec の主要依存
   (firebase_core / riverpod / dio 等) を
   major_dependencies に、`flutter pub get` / `flutter run` / `flutter test` を
   Likely Commands に追加。
7. **Python requirements/Pipfile/setup.cfg の依存取得** (`collectors/dependencies.py`) —
   従来 pyproject.toml のみだった major_dependencies を requirements*.txt /
   Pipfile / setup.cfg からも取得 (優先度 pyproject > Pipfile > requirements >
   setup.cfg)。celery / alembic / redis / gunicorn / httpx 等を IMPORTANT_DEPENDENCIES
   に追加。
8. **Makefile target の抽出** (`core/makefile.py` 新規, `collectors/scripts.py`) —
   `make` 1 行だけでなく、Makefile の conventional target (`make test` /
   `make build` / `make dev` 等) を優先度順に Likely Commands へ。変数代入・
   `.PHONY`・recipe 行・非定型 target は除外。

### P3: 価値の高い情報の追加

9. **git 進行情報** (`core/git.py`, `collectors/git_progress.py` 新規,
   `renderer.py`) — Project Facts に `branch` (ahead/behind vs upstream) と
   `recent_commits` (直近 3 件、subject + 相対日時) を追加。デフォルトブランチ
   (main/master) で差分が無いときは branch 行を省略。detached HEAD / upstream
   無し / 非 git は silent skip。
10. **Domain Types 検出のパス緩和** (`collectors/domain_types.py`) —
    対象パスに `/repositories/` `/services/` `/schemas/` `/dto/` 等を追加、
    走査をファイル先頭 200 行に限定、stop_names 拡張 + infra suffix
    (`*Repository` / `*Service` 等) 除外、「unique 型名 5 個以上」を表示条件に
    追加してノイズを抑制。クラスタ判定は打ち切り前の件数で行うため、
    `--max-domain-types` を 5 未満にしても (リポジトリが実際に 5 型以上持つなら)
    型が表示される。

### その他

- **テスト新設** — `hooks/session-facts/tests/` に unittest 一式 (77 件)。
  `python3 -m unittest discover tests` で実行。`_testutil.py` / `conftest.py` が
  sys.path を整備。
- **CLI** — `--tree-depth` は固定深さの override (未指定で dynamic)、
  `--min-tree-depth` / `--max-tree-depth` を追加。未使用化した `DEFAULT_TREE_DEPTH`
  定数を削除。
- **後方互換** — 既存の出力構造・既定挙動は維持。dynamic depth と chain 圧縮は
  全 repo に効くが、セクション構成は不変。

### 見送り

- **P4 (`--exclude-if-in-claudemd`)** — CLAUDE.md 既出情報の出力抑止モードは、
  受け入れ基準に複数プロジェクトでの false-positive ゼロ検証を含むため将来対応に
  見送り。
