# external-ai-assist

Cursor / Codex などの外部 AI CLI を Claude Code に並走・クロスレビューに活用する hook 集。

## 同梱 hook

| Hook | 発火イベント | 役割 |
|---|---|---|
| `explore-parallel` | `PreToolUse(Agent)` + `PostToolUse(Agent)` | `Explore` サブエージェント起動時に Cursor Agent を並走させ、完了時に `additionalContext` として親 Claude に注入 |
| `exitplan-review` | `PreToolUse(ExitPlanMode)` | プラン承認前に **Cursor (既存コードベース整合) + Codex (要件・アーキ) を並列クロスレビュー** し、指摘を `decision: block` で Claude に差し戻す |
| `post-implementation-review` | `PreToolUse(Bash)` + `PostToolUse(Write/Edit/NotebookEdit/Bash)` + `Stop` | **そのターンにこのセッションが編集したファイルだけ**を Cursor でレビューし、影響範囲・リグレッションリスク等を `decision: block` で Claude に返す |

## インストール

```
/plugin marketplace add Mao-o/cc-mp-worktools
/plugin install external-ai-assist@mao-worktools
```

## 前提

- **Python 3.11+** (標準ライブラリのみ使用)
- `cursor` CLI: `explore-parallel` / `exitplan-review` / `post-implementation-review` の全てで使う。
  3 hook とも読み取り専用 (`cursor agent --mode plan`) で起動し、作業ツリーは書き換えさせない
  (read-only は cursor-agent の help 記述「`--mode plan` = read-only/planning (no edits)」に
  基づく。実機で書込が抑止されることは本 plugin 側では検証していない)
- `codex` CLI: `exitplan-review` の要件・アーキ観点担当 (`codex exec -s read-only --ephemeral`)

**どちらの CLI も未インストールでも Claude Code 本体の動作には影響しない** (fail-open)。
片方だけインストールされていれば、その片方の観点だけでレビューが成立する。

## 動作サマリ

### explore-parallel

`Explore` サブエージェントの視野を Cursor Agent で広げる。詳細は `hooks/explore-parallel/CLAUDE.md`。

Cursor Agent は読み取り専用 (`--mode plan`) で並走させる (0.4.1 から。それ以前は書込可能な
`-p` 単独で起動していた)。

### exitplan-review (クロスレビュー)

`ExitPlanMode` 呼び出し時に Cursor と Codex を **並列実行** し、両者の出力を統合して `decision: block` で Claude に返す。

- **Cursor (primary)**: コードベース上の具体的な根拠を持つ観点
  - 既存コードベースとの整合性・影響範囲・依存の妥当性・見落とし箇所・テスト戦略
- **Codex (補完)**: 要件とアーキ方針の観点
  - 要件取り違え・スコープ過不足・アーキ上の危険信号・非機能要件・早期固定すべき前提

両者のプロンプトは `hooks/exitplan-review/prompts/planning-{cursor,codex}.md` に外部化されている。出力は 5 項目立ての箇条書きに固定され、ノイズが少ない。

- **セッション × プラン単位で最大 N 回ブロック** (既定 2 回、`EXTERNAL_AI_REVIEW_MAX` で変更可。`0` で無効化)
- プラン内容の SHA-256 ハッシュ (先頭 2000 文字の正規化版) で同一性判定
- レビュー結果は `$TMPDIR/plan-review-<session_id>.txt` にも保存
- 両方のレビュアーが失敗した場合は fail-open (exit 0)
- 「指摘なし」は `REVIEW_CLEAN` sentinel で判定する。コードフェンス・装飾行・「指摘なし」を
  述べる短い前置き 1 文は許容し、sentinel + 指摘本文は block する
  (判定規則は `hooks/_common/sentinel.py`。両 hook 共通)

### post-implementation-review (ターンスコープ差分レビュー)

Claude の作業が一段落した時点 (Stop) で Cursor に差分レビューを依頼し、影響範囲・リグレッション・不足テストを指摘させる。

**レビュー対象は「前回 Stop がレビュー対象として消費した時点以降に、このセッションが変更したファイル」だけ**。
作業ツリー全体の `git diff HEAD` は使わない。同一ディレクトリで複数セッションが動くと、一行も編集して
いないセッションが隣のセッションの編集を 5〜10 分かけてレビューしてしまうため。

変更パスの収集経路は 2 つ:

| 経路 | hook | 拾えるもの |
|---|---|---|
| 編集ツール | `PostToolUse(Write/Edit/NotebookEdit)` | `tool_input.file_path` (サブエージェント経由の編集も親セッションに帰属) |
| Bash | `PreToolUse(Bash)` + `PostToolUse(Bash)` | `sed -i` / フォーマッタ / スクリプト生成。実行前後の `git status` を突き合わせて検出 (Bash 1 回あたり約 70 ms) |

動作:

- **編集 0 件のターンは cursor を起動しない** (即 exit 0)
- `stop_hook_active` が true なら skip (再帰防止の公式パターン)
- 前回レビュー時と diff が 1 バイトも変わっていないパスは載せない
- 作業ツリー外の絶対パスは対象外
- 差分は**パスごとに HEAD 基準**で取得 (レビュアーにファイル全体の変更文脈を渡すため。
  判断根拠は `hooks/post-implementation-review/gitscan.py` の docstring)
- 同一作業ツリーで `cursor agent` が同時に 2 つ起動しないよう flock で直列化
- レビュー結果を配信できた時だけ消費を確定し、cursor 失敗時は次ターンに持ち越す
- **機密ファイル・非コードファイルは外部に送らない** (後述の除外規則。除外は恒久で、
  次ターンにも再掲しない)
- 1 ターンあたり 60 パスまで。超過分は捨てずに次ターンへ繰り越す
- 差分は **ファイル単位で** 合計 40 KB の予算に積む。収まらないファイルは送らずに次ターンへ
  繰り越す (レビュー済みにはしない。繰り越し分は次ターンの先頭に来る)。1 ファイルが 32 KB を
  超える場合だけ先頭を `(truncated)` 付きで送り、そのファイルはレビュー済みとして扱う
  (超えた分の hunk は、そのファイルの差分が変わるまでレビューされない)
- 除外・繰り越し・切り詰めが起きたターンは、対象ファイル名と理由を `systemMessage` と
  stderr に出す (ファイルの内容は出さない)
- レビュー結果は `$TMPDIR/post-implementation-review/reviews/<session_id>.txt` にも保存
- 状態ファイルは 48 時間で GC (旧 `$TMPDIR/post-review-markers/` の残骸も掃除する)

プロンプトは `hooks/post-implementation-review/prompts/post-implementation-cursor.md` に外部化され、出力は 5 項目 (直接影響 / 間接影響 / 未検証ケース / 追加テスト / マージ前確認) に固定。

#### 外部に送らないファイル (除外規則)

差分を Cursor に渡す前に、パスごとに次の順で判定し、どれかに当たれば**そのファイルの差分は
送らない** (判定は `hooks/post-implementation-review/exclusion.py`。他 plugin には依存しない):

0. **`EXTERNAL_AI_POST_REVIEW_EXCLUDE` の `!glob`** に当たるファイルは**必ず送る** (以下より優先。
   既定除外にコードが巻き込まれた時の逃げ道。例: `!credentials-service/*`)
1. **既定除外** — 名前からして機密なもの (`EXTERNAL_AI_POST_REVIEW_EXCLUDE_DEFAULTS=0` で無効化):
   - glob: `.env` `.env.*` `*.env` `.envrc` / `*.pem` `*.key` `*.p12` `*.pfx` `*.p8` `*.jks`
     `*.keystore` `*.ppk` `*.gpg` `*.pgp` `*.asc` `*.kdbx` / `id_rsa*` `id_dsa*` `id_ecdsa*`
     `id_ed25519*` / `*service-account*.json` `*service_account*.json` `kubeconfig*` `*.ovpn` /
     `.htpasswd` `*.htpasswd` `.netrc` `_netrc` `.npmrc` `.pypirc` `.pgpass` `.git-credentials` /
     `*.tfstate` `*.tfstate.*` `*.tfvars` `*.tfvars.*`
   - 語: パス (ディレクトリ名を含む) に `secret` / `secrets` / `credential` / `credentials` を
     **単語として** (前後が英数字以外) 含む。`client_secret.json` `config/secrets/db.yaml`
     `aws_credentials` は当たり、`secretary.py` `secretsanta.ts` は当たらない
2. **`EXTERNAL_AI_POST_REVIEW_EXCLUDE`** — カンマ区切りで追加する glob
   (例: `docs/, *.csv, notes/*.md`。`dir/` は `dir/*`、先頭の `./` `/` は無視。brace 展開
   `*.{py,js}` は非対応。`docs` とだけ書くと `docs` という名前のファイルにしか当たらない)
3. **`EXTERNAL_AI_POST_REVIEW_CODE_ONLY=1`** — コード以外を外す。対象は拡張子で判定:
   文書 (`.md .markdown .rst .txt .text .adoc .asciidoc .org .tex .rtf .pdf .doc .docx .odt .xls .xlsx .ods .ppt .pptx .odp`)、
   データ (`.csv .tsv .jsonl .ndjson .log .parquet .avro .sqlite .sqlite3 .db`)、
   メール等 (`.eml .msg .mbox .ics .vcf`)、画像・音声・動画、アーカイブ。
   JSON / YAML / TOML / XML / HTML / CSS と拡張子無し (Makefile 等) はコード扱いで送る

glob は **basename と作業ツリー相対パスの両方**に、**大文字小文字を区別せず**当てる。`*` は `/` にも
マッチするので `docs/*` は深い階層も拾う。symlink は編集時のパス (途中のディレクトリ名やリンク名を
そのまま残した lexical なパス)、実体 (realpath)、および作業ツリー内の symlink から作った別名
(`credentials/` → `ordinary/` なら `ordinary/data.json` に `credentials/data.json`) のどれかが
当たれば除外。別名は Bash 経由の変更 (`sed -i` 等。`git status` が実体名しか返さない) のために
必要で、tracked の symlink は全て、untracked の symlink は作業ツリー直下から 3 階層まで
(走査 5000 エントリ / 500 件で打ち切り) を見る。
git へのパス渡しは literal pathspec (`app/[id]/page.tsx` のような名前を glob として解釈させず、
claim していないファイルの差分が混入しない)。

除外されたファイルは pending にも reviewed にも残さない (恒久除外)。該当ターンの末尾に
`[post-implementation-review] 2 ファイルを外部 AI レビューから除外 (内容は送信していません): .env (既定除外: .env), docs/meeting-notes.txt (CODE_ONLY: .txt)`
のような通知が出る (`systemMessage`。同じ内容を stderr にも書く)。

```bash
# 議事録と CSV も送らない
EXTERNAL_AI_POST_REVIEW_EXCLUDE="docs/, *.csv" claude
# コードだけ送る
EXTERNAL_AI_POST_REVIEW_CODE_ONLY=1 claude
```

## 設計原則

1. **hook は絶対に失敗させない** — 全ての外周で例外を捕捉し exit 0
2. **fail-open** — CLI 未インストール / タイムアウト / 応答空 の各ケースで Claude Code の動作を止めない
3. **観点の分離** — タイミングごとに担当観点を変え、プロンプトを外部ファイルに分離して保守性を確保
4. **クロスレビュー時は並列実行** — Cursor と Codex を `ThreadPoolExecutor` で並行起動。片方だけ取れても block 成立
5. **共通化は同型が出揃ってから** — hook 間で同型だった処理 (sentinel 判定 / 外部 CLI 起動 /
   flock / ログ) は `hooks/_common/` に集約し、各 hook は `__main__.py` 冒頭で `hooks/` を
   `sys.path` に載せて参照する (plugin root 内の相対配置なので cache コピーでも壊れない)。
   hook 固有の状態機械は共通化しない
6. **外部 CLI は独自 process group で起動** — timeout 時は `os.killpg` でグループごと停止
   (SIGTERM → 猶予 → SIGKILL) し、同じグループに居る孫プロセス (stdout を継承した
   helper 等) を取り残さない。killpg は reap 前の子にだけ送る (pid 再利用の誤送信防止)。
   kill 猶予は hooks.json の hook timeout に織り込んである (各 tests が式で固定)
7. **外部 AI は読み取り専用で起動する** — cursor は `--mode plan`、codex は
   `exec -s read-only --ephemeral`。調査 (explore-parallel) もレビューも外部 AI に作業ツリーを
   書き換えさせない。cursor の起動 argv は `hooks/_common/cursorcli.readonly_argv` に一本化し、
   3 hook それぞれの偽 CLI テストが `--mode plan` を固定する

## 環境変数

| 変数 | 既定値 | 意味 |
|---|---|---|
| `EXTERNAL_AI_REVIEW_MAX` | `2` | `exitplan-review` のセッション × プラン単位の最大ブロック回数。`0` で hook 自体を無効化 |
| `EXTERNAL_AI_POST_REVIEW` | `1` | `post-implementation-review` の有効/無効。`0` / `false` / `off` で無効化 |
| `EXTERNAL_AI_POST_REVIEW_BASH_TRACKING` | `1` | Bash 経由の変更検出。`0` にすると Bash 前後の `git status` を打たなくなる (巨大 repo での軽量化用。`sed -i` 等の変更は拾えなくなる) |
| `EXTERNAL_AI_POST_REVIEW_EXCLUDE` | — | `post-implementation-review` で外部に送らないファイルをカンマ区切り glob で追加 (例: `docs/, *.csv`)。既定除外に加算される。`!glob` は逆に「必ず送る」 (例: `!credentials-service/*`) |
| `EXTERNAL_AI_POST_REVIEW_EXCLUDE_DEFAULTS` | `1` | 既定除外 (`.env*` / `*.pem` / 語 `secret` `credential` 等) の有効/無効。`0` で無効化 (追加 glob と CODE_ONLY は残る) |
| `EXTERNAL_AI_POST_REVIEW_CODE_ONLY` | `0` | `1` でコード以外 (`.md` / `.txt` / `.csv` / `.pdf` / 画像等。一覧は上の除外規則) を外部に送らない |
| `EXTERNAL_AI_POST_REVIEW_MAX` | — | **v0.3.0 で撤廃** (レビュー回数の予算)。`0` を無効化スイッチとして使っていた環境のため、`0` のときだけ後方互換で無効化として解釈する (経緯は `CHANGELOG.md` の 0.3.0 Deprecated 節) |

一時的に無効化したい場合は `0` を設定するのが手軽:

```bash
EXTERNAL_AI_REVIEW_MAX=0 EXTERNAL_AI_POST_REVIEW=0 claude
```

## ファイル構成

```
external-ai-assist/
├── .claude-plugin/plugin.json
├── README.md                               ← このファイル
├── CHANGELOG.md                            ← 版ごとの変更履歴・設計判断
└── hooks/
    ├── hooks.json                          ← 6 hook を定義
    ├── _common/                            ← hook 間の共通ヘルパー (sys.path 経由で参照)
    │   ├── sentinel.py                     ← REVIEW_CLEAN 判定
    │   ├── subproc.py                      ← 外部 CLI 起動 (process group + timeout)
    │   ├── cursorcli.py                    ← cursor agent の存在確認 / 読み取り専用 (--mode plan) 起動 argv (3 hook 共通)
    │   ├── flock.py                        ← flock 付き read-modify-write
    │   ├── hooklog.py                      ← stderr ログ
    │   └── tests/
    ├── explore-parallel/
    │   ├── __main__.py
    │   ├── cursor.py                       ← 読み取り専用でバックグラウンド起動 + 待機
    │   ├── state.py
    │   ├── CLAUDE.md
    │   └── tests/                          ← 起動引数 (--mode plan) と注入の unittest
    ├── exitplan-review/
    │   ├── __main__.py                     ← 並列実行 + マーカー管理
    │   ├── cursor.py                       ← コードベース整合観点
    │   ├── codex.py                        ← 要件・アーキ観点
    │   ├── prompts/
    │   │   ├── planning-cursor.md
    │   │   └── planning-codex.md
    │   └── tests/                          ← block / 非 block 判定と偽 CLI の unittest
    └── post-implementation-review/
        ├── __main__.py                     ← 3 phase (pre-tool / post-tool / stop) の振り分け
        ├── state.py                        ← pending/in-flight 状態機械 + flock
        ├── stategc.py                      ← $TMPDIR の TTL GC
        ├── gitscan.py                      ← パス正規化 + status スナップショット + パス単位 diff
        ├── exclusion.py                    ← 外部に送らないファイルの判定 (既定 glob / 追加 glob / CODE_ONLY)
        ├── cursor.py                       ← 差分レビュー
        ├── prompts/
        │   └── post-implementation-cursor.md
        └── tests/                          ← 受け入れ基準の unittest スイート
```

テストは各 `tests/` の親ディレクトリで `python3 -m unittest discover tests` を回す
(cursor / codex は起動しない。モックか PATH 先頭の偽 CLI で検証する)。hook ごとに同名の
モジュール (`cursor` / `state`) を持つため、plugin root からの pytest 一括実行には対応しない。

## 拡張ポイント

### 新しいレビュアー (Gemini 等) を追加

`exitplan-review/` 以下に同形のモジュールを追加し、`__main__.py` の `REVIEWERS` に加える。

```python
# exitplan-review/gemini.py
NAME = "gemini"
TIMEOUT_SEC = 600
def is_available() -> bool: ...
def review(plan_text: str) -> str | None: ...
```

```python
# exitplan-review/__main__.py
import gemini
REVIEWERS = [cursor, codex, gemini]
_HEADERS["gemini"] = "## Gemini レビュー (xxx 観点)"
```

プロンプトは `prompts/planning-gemini.md` として追加。

### 他のタイミングへの拡張

`mid-implementation-review` (実装途中) や `pr-review` (PR 作成前) を追加する場合は、
同じ hook 構造 (`__main__.py` + `cursor.py` + `prompts/*.md`) を踏襲して新ディレクトリを作り、
`hooks.json` に `PreToolUse(Bash)` matcher + `gh` コマンド検出などを組み合わせる。

## ライセンス

MIT
