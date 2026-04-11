# explore-parallel

PreToolUse/PostToolUse(Agent) フックとして動作し、`Explore` サブエージェント起動時に
別の補助アナライザ（現状 cursor agent）をバックグラウンドで並走させ、
Explore 完了時に結果を `additionalContext` で親 Claude に注入する。

## 目的

Claude Code の `Explore` サブエージェントはリポジトリのコードインデックスを持たないため、
大規模リポジトリでの調査が浅くなることがある。独自の強力なインデックスを持つ
外部ツール（cursor 等）を**並走**させ、結果を統合することで Explore の視野を広げる。

pre フェーズで並走起動 → Explore 本体と同時に調査進行 → post フェーズで結果待ち受けして注入、
という非同期パターンにより Explore 本体の応答遅延を最小化している。

## ディレクトリ構成

```
explore-parallel/
├── CLAUDE.md           このドキュメント
├── __main__.py         エントリポイント。--phase pre|post でフェーズ振り分け、ANALYZERS を順に回す
├── state.py            tool_use_id ベースの一時ファイルパス管理 (/tmp/explore-parallel/)
└── cursor.py           cursor agent の pre(起動) / post(待機+結果取得)
```

**実行フロー**:

- **pre フェーズ**: `__main__.py` が stdin から hook input を読み取り、`subagent_type == "Explore"` をチェック。`ANALYZERS` に登録されたアナライザのうち `is_available()` が True のものを順に `pre(tool_use_id, prompt)` で起動する。`cursor.pre()` は cursor agent をバックグラウンド起動し、PID と結果ファイルを `/tmp/explore-parallel/` に記録
- **post フェーズ**: `__main__.py` が同じく hook input を受け取り、各アナライザの `post(tool_use_id)` を呼ぶ。`cursor.post()` は PID を見て最大 `TIMEOUT_SEC` 秒待機、結果ファイルを読み取って整形済み文字列を返す。`__main__.py` は複数アナライザの結果を `\n\n` で結合し、1 つの `additionalContext` JSON にまとめて stdout に出力

Python 3.9+ 想定。標準ライブラリのみ使用（外部依存なし）。

## アナライザの追加

新しいアナライザ（例: Gemini）を追加する手順:

1. `gemini.py` を作成し、以下 4 つを公開する:

   | 名前 | 型 | 説明 |
   |---|---|---|
   | `NAME` | `str` | 識別子（英数字。state ファイル名に使用） |
   | `is_available()` | `() -> bool` | CLI 存在確認等の事前チェック |
   | `pre(tool_use_id, prompt)` | `(str, str) -> None` | バックグラウンド起動 |
   | `post(tool_use_id)` | `(str) -> str \| None` | 待機 + 結果取得。整形済み文字列 or None |

2. `__main__.py` に 2 行追加:
   ```python
   import gemini
   ANALYZERS = [cursor, gemini]
   ```

**プラグイン契約（`TIMEOUT_SEC` 等の必須属性や基底クラス）は意図的に導入していない**。
cursor と Gemini では待機方式や結果の整形方法が異なる可能性があるため、
各アナライザ内部で自由にロジックを持てるようにしている。共通パターンが 2 つ目で
見えてきたらその時点で抽象化する方針（YAGNI）。

## 状態管理

`state.py` の `paths(name, tool_use_id)` が `(result_file, pid_file)` のタプルを返す:

- パス命名: `/tmp/explore-parallel/<name>-<tool_use_id>.{txt,pid}`
- `<name>` はアナライザごとの識別子（`NAME` 定数）
- `<tool_use_id>` は hook input に含まれる一意 ID
- `TMPDIR` 環境変数があればそちらを優先

複数アナライザが同時実行されてもファイル名で衝突しない設計。
post 実行後は `cleanup()` で PID/結果ファイルを削除する。

### tool_use_id の重要性

pre と post は**同じ `tool_use_id`** で呼ばれることが前提。これで別の Explore 実行との
結果ファイルが混ざらない。hook input の `tool_use_id` が空の場合は no-op で抜ける。

## 呼び出し側（plugin の `hooks/hooks.json`）

```json
"PreToolUse": [{
  "matcher": "Agent",
  "hooks": [{
    "type": "command",
    "command": "python3 ${CLAUDE_PLUGIN_ROOT}/hooks/explore-parallel --phase pre",
    "timeout": 5
  }]
}],
"PostToolUse": [{
  "matcher": "Agent",
  "hooks": [{
    "type": "command",
    "command": "python3 ${CLAUDE_PLUGIN_ROOT}/hooks/explore-parallel --phase post",
    "timeout": 90
  }]
}]
```

`${CLAUDE_PLUGIN_ROOT}` は Claude Code が plugin ロード時に展開する絶対パス。
インストール後は `~/.claude/plugins/cache/<plugin>/` 配下に展開される。

- **timeout は秒単位**（Claude Code の仕様。ミリ秒ではない）
- **pre の timeout**: バックグラウンド起動で即 return するので短くて OK（5 秒）
- **post の timeout**: アナライザ待機があるため長め（90 秒 = cursor の TIMEOUT_SEC=60 + 余裕 30）

## テスト

標準入力に hook input JSON を流し込む:

```bash
# plugin dir からの相対パスで実行 (dev 時)
cd "$(dirname "$(dirname "$0")")"  # hooks/ の親 = plugin root
HOOK=hooks/explore-parallel

# pre フェーズ (cursor をバックグラウンド起動)
echo '{"tool_input":{"subagent_type":"Explore","prompt":"テストクエリ"},"tool_use_id":"test-001"}' \
  | python3 "$HOOK" --phase pre

# 少し待ってから post フェーズ (結果待機 + 注入)
sleep 10
echo '{"tool_input":{"subagent_type":"Explore"},"tool_use_id":"test-001"}' \
  | python3 "$HOOK" --phase post
```

期待動作:
- `subagent_type` が Explore 以外 → 出力なし、exit 0
- cursor 未インストール → 出力なし（スキップ）
- cursor 正常終了 → `additionalContext` JSON を stdout に出力
- cursor タイムアウト → 出力なし + stderr にメッセージ
- 予期しない例外 → stderr にメッセージ、exit 0（**hook は絶対に失敗させない**）

## 設計判断の履歴

- **Python 化** — bash + python3 heredoc の eval は JSON パースが脆弱で保守性が低い。標準ライブラリの `subprocess` / `json` で統一
- **4 ファイル分離（`__main__.py` + `state.py` + `cursor.py` + `CLAUDE.md`）** — pre/post 振り分けと cursor 固有ロジックを分離し、将来のアナライザ追加時に cursor.py の構造をコピーしやすくしている
- **プラグイン契約なし（YAGNI）** — 現状 cursor 1 つだけなので抽象化しない。2 つ目（Gemini）追加時に共通パターンが見えたら段階的に抽象化する
- **`state.py` 分離** — 一時ファイル管理を共通化。2 つ目のアナライザ追加時にパス命名の衝突を回避しつつ、state ロジックの重複を防ぐ
- **例外の完全捕捉** — `__main__.py` の最外周で全例外を捕捉し exit 0 する。hook の失敗が Claude Code 本体の動作に影響しないようにする
