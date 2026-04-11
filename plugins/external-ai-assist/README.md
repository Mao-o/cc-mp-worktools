# external-ai-assist

Codex / Cursor などの外部 AI CLI を Claude Code に並走・レビューに活用する hook 集。

## 同梱 hook

| Hook | 発火イベント | 役割 |
|---|---|---|
| `explore-parallel` | `PreToolUse(Agent)` + `PostToolUse(Agent)` | `Explore` サブエージェント起動時に Cursor Agent をバックグラウンドで並走させ、Explore 完了時に結果を `additionalContext` として親 Claude に注入 |
| `exitplan-review-codex` | `PreToolUse(ExitPlanMode)` | プラン承認前に Codex CLI でプランをレビューし、同一プランに対して最大 2 回まで `decision: block` でフィードバックを注入 |

## インストール

```
/plugin marketplace add Mao-o/cc-mp-worktools
/plugin install external-ai-assist@mao-worktools
```

## 前提

- **Python 3.9+** (標準ライブラリのみ使用)
- `cursor` CLI: `explore-parallel` が Cursor Agent を呼ぶ際に必要。未インストール時は hook は no-op (他のアナライザがあれば続行)
- `codex` CLI: `exitplan-review-codex` が Codex を呼ぶ際に必要。未インストール時は hook は no-op でフォールスルー

どちらの CLI も**未インストールでも Claude Code 本体の動作には影響しない**設計。

## 動作サマリ

### explore-parallel

Claude Code の `Explore` サブエージェントはリポジトリのコードインデックスを持たないため、
大規模リポジトリでの調査が浅くなることがある。独自のセマンティック検索インデックスを持つ
Cursor Agent を **並走** させ、結果を統合することで Explore の視野を広げる。

- **pre フェーズ**: `subagent_type == "Explore"` を確認し、Cursor Agent をバックグラウンド起動。PID を `/tmp/explore-parallel/` に記録して即 return
- **post フェーズ**: 記録した PID を最大 60 秒待ち、結果ファイルを読み取って `additionalContext` として親 Claude に注入
- Cursor の prompt は「grep で引っかからない関連コード・類似実装・間接依存・波及範囲」に絞り、Explore 本体と重複しないよう制約

### exitplan-review-codex

`ExitPlanMode` 呼び出し時に Codex にプランレビューを依頼し、問題点・矛盾・リスクを
`decision: block` で返して反映を促す。

- **セッション × プラン単位で最大 2 回**ブロック。プラン内容の SHA-256 ハッシュ (先頭 2000 文字の正規化版) で同一性を判定し、同じプランでの再ブロックは 1 回
- Codex は `read-only` かつ `--ephemeral` で実行するので副作用なし
- レビュー結果は `$TMPDIR/plan-review-<session_id>.txt` にも保存し、人間が後で参照可能
- Codex 未インストール・タイムアウト・空応答いずれも fail-open (exit 0 でフォールスルー)

## 設計原則

1. **hook は絶対に失敗させない** — 全ての外周で例外を捕捉し exit 0 する (`explore-parallel/__main__.py` の `_main()` 呼出し等)
2. **fail-open** — 外部 CLI 未インストール / タイムアウト / 応答空 の各ケースで Claude Code 本体の動作を止めない
3. **YAGNI** — analyzer は現状 Cursor のみ。プラグイン契約 (基底クラス等) は導入せず、2 つ目追加時に共通パターンが見えたら抽象化する
4. **state は `tool_use_id` 単位で隔離** — 複数 Explore の同時並走でも結果ファイルが混ざらない

## 拡張ポイント

### 新しいアナライザ (Gemini 等) を追加

`hooks/explore-parallel/CLAUDE.md` の「アナライザの追加」セクション参照。

要点:
1. `hooks/explore-parallel/<new>.py` を作成 (`NAME` / `is_available()` / `pre()` / `post()` の 4 つを公開)
2. `hooks/explore-parallel/__main__.py` の `ANALYZERS = [cursor]` にモジュールを追加

### レビュー回数上限の変更

`hooks/exitplan-review-codex/__main__.py` の `MAX_REVIEWS` 定数を書き換え (既定: 2)。

## ファイル構成

```
external-ai-assist/
├── .claude-plugin/plugin.json
├── README.md            ← このファイル
├── CLAUDE.md            ← 保守・拡張者向けガイド
└── hooks/
    ├── hooks.json       ← 3 hook (Agent pre/post + ExitPlanMode) を定義
    ├── explore-parallel/
    │   ├── __main__.py  ← --phase pre|post で振り分け
    │   ├── cursor.py    ← Cursor Agent の pre/post
    │   ├── state.py     ← /tmp/explore-parallel/ のファイルパス管理
    │   └── CLAUDE.md    ← hook 単位の詳細ガイド
    └── exitplan-review-codex/
        └── __main__.py  ← Codex exec でプランレビュー
```

## ライセンス

MIT
