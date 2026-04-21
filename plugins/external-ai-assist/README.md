# external-ai-assist

Cursor / Codex などの外部 AI CLI を Claude Code に並走・クロスレビューに活用する hook 集。

## 同梱 hook

| Hook | 発火イベント | 役割 |
|---|---|---|
| `explore-parallel` | `PreToolUse(Agent)` + `PostToolUse(Agent)` | `Explore` サブエージェント起動時に Cursor Agent を並走させ、完了時に `additionalContext` として親 Claude に注入 |
| `exitplan-review` | `PreToolUse(ExitPlanMode)` | プラン承認前に **Cursor (既存コードベース整合) + Codex (要件・アーキ) を並列クロスレビュー** し、指摘を `decision: block` で Claude に差し戻す |
| `post-implementation-review` | `Stop` | 実装完了時に `git diff HEAD` の差分を Cursor でレビューし、影響範囲・リグレッションリスク等を `decision: block` で Claude に返す |

## インストール

```
/plugin marketplace add Mao-o/cc-mp-worktools
/plugin install external-ai-assist@mao-worktools
```

## 前提

- **Python 3.9+** (標準ライブラリのみ使用)
- `cursor` CLI: `explore-parallel` / `exitplan-review` / `post-implementation-review` の全てで使う
- `codex` CLI: `exitplan-review` の要件・アーキ観点担当

**どちらの CLI も未インストールでも Claude Code 本体の動作には影響しない** (fail-open)。
片方だけインストールされていれば、その片方の観点だけでレビューが成立する。

## 動作サマリ

### explore-parallel

`Explore` サブエージェントの視野を Cursor Agent で広げる。詳細は `hooks/explore-parallel/CLAUDE.md`。

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

### post-implementation-review (差分レビュー)

Claude の作業が一段落した時点 (Stop) で Cursor に差分レビューを依頼し、影響範囲・リグレッション・不足テストを指摘させる。

- `git diff HEAD` が空なら skip (そもそも変更なし)
- `stop_hook_active` が true なら skip (再帰防止の公式パターン)
- 同一 diff でレビュー済みなら skip
- **セッション × diff 単位で最大 N 回ブロック** (既定 2 回、`EXTERNAL_AI_POST_REVIEW_MAX` で変更可)
- 差分は 40 KB まで、超過分は truncate
- レビュー結果は `$TMPDIR/post-review-<session_id>.txt` にも保存

プロンプトは `hooks/post-implementation-review/prompts/post-implementation-cursor.md` に外部化され、出力は 5 項目 (直接影響 / 間接影響 / 未検証ケース / 追加テスト / マージ前確認) に固定。

## 設計原則

1. **hook は絶対に失敗させない** — 全ての外周で例外を捕捉し exit 0
2. **fail-open** — CLI 未インストール / タイムアウト / 応答空 の各ケースで Claude Code の動作を止めない
3. **観点の分離** — タイミングごとに担当観点を変え、プロンプトを外部ファイルに分離して保守性を確保
4. **クロスレビュー時は並列実行** — Cursor と Codex を `ThreadPoolExecutor` で並行起動。片方だけ取れても block 成立
5. **YAGNI** — 共通化は 2 つ目のパターンが見えたタイミングで行う (現状 hook 間の共通ヘルパーなし)

## 環境変数

| 変数 | 既定値 | 意味 |
|---|---|---|
| `EXTERNAL_AI_REVIEW_MAX` | `2` | `exitplan-review` のセッション × プラン単位の最大ブロック回数。`0` で hook 自体を無効化 |
| `EXTERNAL_AI_POST_REVIEW_MAX` | `2` | `post-implementation-review` のセッション × diff 単位の最大ブロック回数。`0` で hook 自体を無効化 |

どちらも一時的に無効化したい場合は `0` を設定するのが手軽:

```bash
EXTERNAL_AI_REVIEW_MAX=0 EXTERNAL_AI_POST_REVIEW_MAX=0 claude
```

## ファイル構成

```
external-ai-assist/
├── .claude-plugin/plugin.json
├── README.md                               ← このファイル
├── CLAUDE.md                               ← 保守・拡張者向けガイド
└── hooks/
    ├── hooks.json                          ← 4 hook を定義
    ├── explore-parallel/
    │   ├── __main__.py
    │   ├── cursor.py
    │   ├── state.py
    │   └── CLAUDE.md
    ├── exitplan-review/
    │   ├── __main__.py                     ← 並列実行 + マーカー管理
    │   ├── cursor.py                       ← コードベース整合観点
    │   ├── codex.py                        ← 要件・アーキ観点
    │   └── prompts/
    │       ├── planning-cursor.md
    │       └── planning-codex.md
    └── post-implementation-review/
        ├── __main__.py                     ← Stop hook + git diff + マーカー
        ├── cursor.py                       ← 差分レビュー
        └── prompts/
            └── post-implementation-cursor.md
```

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
