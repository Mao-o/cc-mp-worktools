# external-ai-assist (実装者向けガイド)

このファイルは **plugin の保守・拡張者向け**。利用者向け概要は [README.md](./README.md)。

## 目的と非目的

### 目的

1. Claude Code の `Explore` / `ExitPlanMode` / `Stop` に対して、外部 AI CLI (Cursor, Codex) を
   **副作用なしで** 並走・レビューに使う hook をまとめて提供する
2. 外部 CLI 未インストール・失敗・タイムアウトいずれでも Claude Code 本体の動作を
   止めない (fail-open)
3. 3 つの hook は**独立性を保ったまま** 1 plugin に同居させ、将来レビュアーを追加
   (Gemini 等) しても構造が壊れない

### 非目的

- **レビュアー間の結果マージ** — 並列実行はするが、出力は独立セクションで併記。相互参照はしない
- **永続キャッシュ** — レビュー結果は `$TMPDIR` のみ。再起動で消えてよい
- **敵対的防御** — プロンプトインジェクション対策はしない (参照用コードではない)

## ディレクトリ構成

```
external-ai-assist/
├── .claude-plugin/plugin.json
├── README.md
├── CLAUDE.md                          ← このファイル
└── hooks/
    ├── hooks.json                      # 4 hook (Agent pre/post, ExitPlanMode, Stop) を定義
    ├── explore-parallel/               # Agent hook (pre/post 振り分け)
    │   ├── __main__.py
    │   ├── cursor.py
    │   ├── state.py
    │   └── CLAUDE.md
    ├── exitplan-review/                # ExitPlanMode hook (並列クロスレビュー)
    │   ├── __main__.py                 # 並列実行 + マーカー + block 応答
    │   ├── cursor.py                   # Cursor レビュー (コードベース整合観点) primary
    │   ├── codex.py                    # Codex レビュー (要件・アーキ観点) 補完
    │   └── prompts/
    │       ├── planning-cursor.md
    │       └── planning-codex.md
    └── post-implementation-review/     # Stop hook (差分レビュー)
        ├── __main__.py                 # git diff 取得 + マーカー + block 応答
        ├── cursor.py                   # Cursor レビュー
        └── prompts/
            └── post-implementation-cursor.md
```

## 3 つの hook を 1 plugin に同居させた理由

- **外部 AI CLI 依存** という共通スコープ
- **ユーザー視点で関連機能**: いずれも「Claude の作業を外部 AI で補強する」目的
- **hooks.json のまとまり**: 個別 plugin に分けると命名が冗長になる

ただし実装は**相互依存なし**。それぞれの hook は単独で動作し、無効化 (環境変数 `0`) しても
他に影響しない。共通ヘルパーも意図的に作らない (YAGNI)。

## タイミング別レビュー観点の設計

| タイミング | 担当 hook | プロンプト | 担当観点 |
|---|---|---|---|
| 計画確定前 | `exitplan-review` | `planning-cursor.md` | 既存コードベース整合・影響範囲・依存妥当性・見落とし・テスト戦略 |
| 計画確定前 | `exitplan-review` | `planning-codex.md` | 要件妥当性・スコープ・アーキ方針・非機能要件 |
| 実装完了後 | `post-implementation-review` | `post-implementation-cursor.md` | 差分の直接/間接影響・リグレッション・未検証ケース・不足テスト・マージ前確認 |

プロンプトは各タイミングで**担当観点を明示的に狭める**ことでノイズを減らしている。
「この観点以外は書くな」と指示し、出力も 5 項目立ての箇条書きに固定している。

**タイミング別レビューの設計思想** (6 種類のレビュー観点と分業方針) は `prompts/*.md` の
冒頭コメントを参照。将来 `mid-implementation-review` / `pr-review` / `merge-review` を
足す場合も、同じ構造を踏襲する。

## タイムアウト設計

| hook | `hooks.json` timeout (秒) | 個別レビュアーの timeout | 意図 |
|---|---|---|---|
| `explore-parallel --phase pre` | 5 | — | バックグラウンド起動で即 return |
| `explore-parallel --phase post` | 90 | cursor 60s | cursor の TIMEOUT_SEC + 余裕 30 |
| `exitplan-review` | 1560 | cursor 600s + codex 1500s | 並列実行なので max + 余裕 60 |
| `post-implementation-review` | 660 | cursor 600s | cursor + 余裕 60 |

**Claude Code の hook `timeout` は秒単位** (ms ではない)。

`exitplan-review` / `post-implementation-review` は発火頻度が限定的 (プラン確定時 or Stop 時、
かつセッション × ハッシュ単位でブロック回数制限あり) なので、長めでも実用上問題ない。

## 並列実行とフェイルセーフ

`exitplan-review/__main__.py` は `concurrent.futures.ThreadPoolExecutor` で cursor と codex を
並列起動する。`as_completed` で順次回収し、片方が timeout/失敗しても残りの結果で block 成立
できる。両方失敗した場合のみ fail-open (exit 0) する。

```python
# exitplan-review/__main__.py より要点
REVIEWERS = [cursor, codex]  # primary を先頭に
active = [r for r in REVIEWERS if r.is_available()]
with ThreadPoolExecutor(max_workers=len(active)) as pool:
    futures = {pool.submit(r.review, plan_text): r for r in active}
    for future in as_completed(futures, timeout=overall_timeout):
        ...
```

## 設計判断の履歴

- **plugin 名を `external-ai-assist`** — Cursor / Codex 具体名を名前に含めない。将来他の LLM CLI を追加しても意味が壊れないように
- **hook 間で共通化しない** — state 管理・プロンプト・タイムアウト値が hook 固有で、共通化の価値が乏しい
- **`exitplan-review-codex` → `exitplan-review` (0.2.0)** — Cursor を primary レビュアーに加え、Codex は要件・アーキ観点に特化。単一ファイルから `<dir>/{__main__.py,cursor.py,codex.py,prompts/}` に再構成
- **プロンプトの外部ファイル化** — タイミングごとの担当観点が複雑化したため、`prompts/*.md` に切り出し。観点の追加・調整を Python コードを触らずに行えるようにした
- **クロスレビューは並列実行** — 逐次だと合計 35 分かかる (cursor 10 + codex 25)。並列なら max 25 分に収まる。`ThreadPoolExecutor` で I/O 待ちを重ねる
- **`post-implementation-review` は `git diff HEAD`** — Stop hook の `transcript_path` を読んで diff を再構成するより、`git diff` で uncommitted changes を取る方がシンプルで信頼できる
- **`stop_hook_active` 必須チェック** — 再帰呼び出し防止の公式パターン。`decision: block` は Claude に追加作業をさせる指示であり、それが再び Stop を呼ぶため
- **環境変数による無効化** — `EXTERNAL_AI_REVIEW_MAX=0` / `EXTERNAL_AI_POST_REVIEW_MAX=0` で hook 自体を no-op にできる。ブロック回数の調整も同じ変数で行える
- **`plugin.json` に `keywords` を手厚く** — `cross-review`, `diff-review`, `stop` 等を入れて marketplace 検索ヒット率を上げる

## 発火しない/壊れたときの確認手順

1. **hook 自体が登録されているか**
   - `cat ${CLAUDE_PLUGIN_ROOT}/hooks/hooks.json` で JSON が正しいか
   - Claude Code 起動時のログに hook 登録メッセージが出ているか
2. **外部 CLI が PATH に通っているか**
   - `which cursor` / `which codex` で存在確認
   - 未インストールなら `is_available()` で no-op 終了するのが期待動作
3. **環境変数で無効化されていないか**
   - `env | grep EXTERNAL_AI` で `_MAX=0` になっていないか
4. **stderr のエラーメッセージ**
   - `[explore-parallel]` / `[exitplan-review]` / `[post-implementation-review]` / `[cursor]` プレフィクス付きの
     stderr ログを Claude Code のハーネスログで確認
5. **hook timeout に引っかかっていないか**
   - `exitplan-review` が 1560s で timeout → Cursor または Codex の応答が遅い可能性
   - 個別 `TIMEOUT_SEC` を上げるか Claude Code 側 `timeout` を上げる
6. **一時ファイルが残って衝突していないか**
   - `/tmp/explore-parallel/` / `/tmp/plan-review-markers/` / `/tmp/post-review-markers/` を手動削除
7. **`post-implementation-review` が発火しない**
   - `git diff HEAD` が空 (変更なし) の可能性 → `cd <cwd> && git diff HEAD | head` で確認
   - `stop_hook_active` が true で再帰防止に引っかかっている可能性
   - 同一 diff のマーカーが残っている可能性 (上記の `/tmp/post-review-markers/<session>.post.marker` を削除)

## テスト

### 手動 smoke test

```bash
cd hooks/exitplan-review
echo '{"tool_name":"ExitPlanMode","session_id":"test-session","tool_input":{"plan":"テスト用プラン本文"}}' \
  | EXTERNAL_AI_REVIEW_MAX=1 python3 .
# → stdout に decision:block JSON、stderr にログ
```

```bash
cd hooks/post-implementation-review
echo '{"session_id":"test-session","cwd":"'"$PWD"'","stop_hook_active":false}' \
  | EXTERNAL_AI_POST_REVIEW_MAX=1 python3 .
# → stdout に decision:block JSON (git diff がある場合) or 空
```

### plugin validate

```bash
claude plugin validate plugins/external-ai-assist
```

自動テストは未整備。プロンプトと外部 CLI の応答をモックする必要があり整備コスト高め。
現状は手動検証 + `py_compile` での構文チェックのみ。

## 依存関係

標準ライブラリのみ。`pip install` 不要。
Python 3.9+ 想定 (`tuple[str, int]` 型ヒント、`from __future__ import annotations`)。

## 既知の落とし穴

- **SIGALRM は UNIX 専用**: 本プラグインは `signal.SIGTERM` で子プロセスを止めるが、Windows 未検証
- **`/tmp/` ゴミ残り**: 異常終了時に PID ファイル・マーカーが残る可能性あり。異なる session_id/tool_use_id なら衝突しないが長期運用で蓄積するので `/tmp/` のクリーンアップに委ねる
- **`plan-review-markers` はセッション粒度**: 同一セッションで複数の異なるプランを立てる場合、**セッション × 直近のプラン 2 回** で別プランのハッシュが上書きされる
- **`cursor agent --trust`**: 現状 `--trust` フラグで起動しているため cursor が書き込み権限を持つ。レビュー用途では prompt で「コードは読むだけ」と明示しているが、フラグレベルで readonly にできる方が望ましい (cursor CLI のオプション次第)
- **`git diff HEAD` に依存**: `post-implementation-review` は git repo 外で動かすと diff が取れず常に skip になる。非 git ディレクトリは対象外
- **diff truncate**: 40 KB を超える差分は途中で切る。巨大な機械生成差分 (package-lock.json 大量変更等) ではレビュー品質が落ちる

## 関連

- `../sensitive-files-guard/` — 同じく hook を同居させる構造の先例
- `../session-facts/` — SessionStart/SubagentStart 系 hook の別例
- [Claude Code hooks reference](https://code.claude.com/docs/en/hooks.md)
