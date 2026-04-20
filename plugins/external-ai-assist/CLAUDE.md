# external-ai-assist (実装者向けガイド)

このファイルは **plugin の保守・拡張者向け**。利用者向け概要は [README.md](./README.md)。

## 目的と非目的

### 目的

1. Claude Code の `Explore` / `ExitPlanMode` に対して、外部 AI CLI (Cursor, Codex) を
   **副作用なしで** 並走・レビューに使う hook をまとめて提供する
2. 外部 CLI 未インストール・失敗・タイムアウトいずれでも Claude Code 本体の動作を
   止めない (fail-open)
3. 2 つの hook は**独立性を保ったまま** 1 plugin に同居させ、将来アナライザを追加
   (Gemini 等) しても構造が壊れない

### 非目的

- **外部 AI 間の結果マージ** — Cursor / Codex の出力を相互参照しない。それぞれ独立に Claude に注入
- **永続キャッシュ** — レビュー結果は `$TMPDIR` のみ。再起動で消えてよい
- **敵対的防御** — プロンプトインジェクション対策はしない (参照用コードではない)

## ディレクトリ構成

```
external-ai-assist/
├── .claude-plugin/plugin.json
├── README.md
├── CLAUDE.md                        ← このファイル
└── hooks/
    ├── hooks.json                   # PreToolUse(Agent,ExitPlanMode) + PostToolUse(Agent) を定義
    ├── explore-parallel/            # Agent hook (pre/post 振り分け)
    │   ├── __main__.py              # ANALYZERS を回して pre/post
    │   ├── cursor.py                # Cursor Agent の pre(起動) / post(待機+結果取得)
    │   ├── state.py                 # tool_use_id ベースの一時ファイルパス管理
    │   └── CLAUDE.md                # explore-parallel 専用の実装ガイド
    └── exitplan-review-codex/       # ExitPlanMode hook (単ファイル hook)
        └── __main__.py              # Codex exec でプランレビュー → decision:block
```

## 2 つの hook を 1 plugin に同居させた理由

- **外部 AI CLI 依存** という共通スコープ
- **ユーザー視点で関連機能**: 両方とも「Claude の作業を外部 AI で補強する」目的
- **hooks.json のまとまり**: 個別 plugin に分けると `external-ai-assist-explore` /
  `external-ai-assist-exitplan` と命名が冗長になり、ユーザーが両方とも入れるのが常

ただし実装は**相互依存なし**。それぞれの hook は単独で動作し、片方を無効化しても
もう片方に影響しない。共通ヘルパーも意図的に作らない (YAGNI)。

## タイムアウト設計

| hook | `timeout` (秒) | 意図 |
|---|---|---|
| `explore-parallel --phase pre` | 5 | バックグラウンド起動で即 return するため短くて OK |
| `explore-parallel --phase post` | 90 | Cursor の `TIMEOUT_SEC=60` + 余裕 30 |
| `exitplan-review-codex` | 1560 | Codex の `subprocess.run(timeout=1500)` + 余裕 60 |

**Claude Code の hook `timeout` は秒単位** (ms ではない)。
`exitplan-review-codex` の 1560s は長めだが、プラン承認時のレビューは 1 回限り
(セッション × プラン × 最大 2 回) なので、通常の作業フローで頻繁には発火しない。

## 設計判断の履歴

- **plugin 名を `external-ai-assist`** — Cursor / Codex 具体名を名前に含めない。
  将来 Gemini / Claude CLI / 任意の LLM CLI を追加しても意味が壊れないように
- **hook 間で共通化しない** — state 管理・プロンプトテンプレート・タイムアウト値
  など hook 固有のロジックが既に各 hook 内で整理済みで、共通化する価値は乏しい
  (2 hooks で共通化対象がほぼない)
- **`exitplan-review-codex` を単ファイルから `<dir>/__main__.py` に** — `explore-parallel`
  と呼び出し形式を揃える (`python3 ${CLAUDE_PLUGIN_ROOT}/hooks/<name>`)。
  将来 `state.py` 等を追加する余地も残す
- **plugin.json に `keywords` を手厚く** — `external-ai`, `codex`, `cursor` 等を
  入れ、marketplace 検索ヒット率を上げる

## 発火しない/壊れたときの確認手順

1. **hook 自体が登録されているか**
   - `cat ${CLAUDE_PLUGIN_ROOT}/hooks/hooks.json` で JSON が正しいか
   - Claude Code 起動時のログに hook 登録メッセージが出ているか
2. **外部 CLI が PATH に通っているか**
   - `which cursor` / `which codex` で存在確認
   - 未インストールなら `is_available()` / `shutil.which` で no-op 終了するのが期待動作
3. **stderr のエラーメッセージ**
   - `[cursor]` / `[explore-parallel]` / `[exitplan-review-codex]` プレフィクス付きの
     stderr ログを Claude Code のハーネスログで確認
4. **hook timeout に引っかかっていないか**
   - post phase が 90 秒で timeout → Cursor の応答が遅い可能性。`cursor.py` の
     `TIMEOUT_SEC` を上げるか Claude Code 側 `timeout` を上げる
5. **一時ファイルが残って衝突していないか**
   - `/tmp/explore-parallel/` / `/tmp/plan-review-markers/` を手動で削除して再試行

## テスト

```bash
cd hooks/explore-parallel
echo '{"tool_input":{"subagent_type":"Explore","prompt":"x"},"tool_use_id":"t-1"}' \
  | python3 . --phase pre
sleep 10
echo '{"tool_input":{"subagent_type":"Explore"},"tool_use_id":"t-1"}' \
  | python3 . --phase post
```

`exitplan-review-codex` は stdin に `tool_name="ExitPlanMode"` の envelope を渡す
(詳細は `exitplan-review-codex/__main__.py` の main() 冒頭)。
自動テストは未整備 (plan 本文と Codex 応答のモックが必要なため)。現状は手動検証。

## 依存関係

標準ライブラリのみ。`pip install` 不要。
Python 3.9+ 想定 (annotations / `tuple[str, int]` 型ヒントを使うため 3.9+)。

## 既知の落とし穴

- **SIGALRM は UNIX 専用**: 本プラグインは `signal.SIGTERM` で子プロセスを止めるが、
  Windows 環境では動作未検証 (開発者が macOS のみなため)
- **`/tmp/explore-parallel/` ゴミ残り**: Explore が異常終了し post が呼ばれない
  ケースで PID ファイルが残る。次回 pre で同じ `tool_use_id` なら上書きされるが、
  異なる ID だと蓄積し続ける。長期運用時は `/tmp/` のクリーンアップに委ねる
- **`plan-review-markers` はセッション粒度**: 同一セッションで複数の異なるプランを
  立てる場合、**セッション × 直近のプラン 2 回** で別プランのハッシュが上書きされる。
  プラン内容の**全履歴**を追えるわけではない

## 関連

- `../sensitive-files-guard/` — 同じく hook を同居させる構造の先例 (設計参考)
- `../session-facts/` — SessionStart/SubagentStart 系 hook の別例
- [Claude Code hooks reference](https://code.claude.com/docs/en/hooks.md)
