# post-implementation-review

`PreToolUse(Bash)` / `PostToolUse(Write|Edit|NotebookEdit|Bash)` / `Stop` の 3 フックで動作し、
**そのターンにこのセッションが変更したファイルだけ**を Cursor に差分レビューさせ、
critical な指摘があれば `decision: block` で Claude に差し戻す。

## 目的

v0.2.0 は Stop 時に `git diff HEAD` **全体**をレビューしていた。これは同一作業ツリーで
複数セッションが動くと破綻する。実測 (2026-08-14, 2 セッション同時稼働):

| 時刻 | セッション | 出来事 |
|---|---|---|
| 22:13:26 | A (前景) | スロット確保 → cursor agent 起動 |
| 22:14:18 | B (bg) | 応答完了 → Stop 発火 → **同じ diff で cursor 二重起動** |
| 22:16頃 | B | 成果なくスロット解放 (並走による失敗) |
| 22:18:34 | A | レビュー結果を書き出して block (5分08秒) |
| 22:20:33 | B | 応答完了 → Stop 発火 → cursor 再起動 |
| 22:23:39 | B | ユーザーの次メッセージ到着と**同一秒**に解放 (割り込みで死亡) |

原因は 2 つ:

1. マーカーは `session_id` 単位なのに、レビュー対象 diff は**作業ツリー単位**。
   自分が一行も編集していないセッションが、隣のセッションの編集を 5〜10 分かけてレビューする
2. commit するまで `git diff HEAD` が消えないので、毎ターン同じ変更が再レビュー対象になる

v0.3.0 はレビュー対象を「**前回 Stop がレビュー対象として消費した時点以降に、
このセッションが変更したファイル**」に限定してこれを解消する。

## ディレクトリ構成

```
post-implementation-review/
├── CLAUDE.md           このドキュメント
├── __main__.py         エントリポイント。--phase pre-tool|post-tool|stop で振り分け
├── state.py            pending/in-flight 状態機械 + state lock / cursor lock
├── stategc.py          $TMPDIR の TTL GC (旧 post-review-markers も掃除)
├── gitscan.py          パス正規化 + git status スナップショット + パス単位 diff
├── cursor.py           cursor agent 呼び出し (TIMEOUT_SEC = 600)
├── prompts/
│   └── post-implementation-cursor.md
└── tests/              受け入れ基準の unittest スイート
```

## 3 phase の役割

| phase | hook | 役割 |
|---|---|---|
| `pre-tool` | `PreToolUse(Bash)` | Bash 実行前の `git status` スナップショットを `tool_use_id` キーで保存 |
| `post-tool` | `PostToolUse(Write/Edit/NotebookEdit/Bash)` | 変更パスを `session_id` キーの `pending` に積む |
| `stop` | `Stop` | `pending` を claim してレビュー、結果を配信 |

### なぜ Bash にも張るのか

`sed -i` / フォーマッタ / スクリプト生成によるファイル変更は Write/Edit だけ見ていると
取りこぼす。Bash 実行の前後で `git status --porcelain` を突き合わせれば、Bash 経由の
変更も**どのセッションがやったか付きで**拾える。公式
`claude-plugins-official/security-guidance` も `PostToolUse` に `Bash` matcher を使う先例がある。

コストは Bash 呼び出しごとに `git status` 2 回。巨大 repo で重い場合は
`EXTERNAL_AI_POST_REVIEW_BASH_TRACKING=0` で切れる (その場合 `sed -i` 等は拾えなくなる)。

**pre/post のスナップショット比較は行の集合ではなくタプル比較**であること。
すでに HEAD から変更済みのファイルを `sed -i` で書き換えると porcelain の行は
` M seed.txt` のまま変わらず、行集合の差分では検出できない。
`(status_code, size, mtime_ns)` まで見て初めて拾える (`gitscan.status_snapshot`)。

## 状態機械: in-flight 予約付き drain-at-Stop

```
PostToolUse            Stop (claim)               Stop (complete)
------------           ------------               ---------------
pending += path   ->   in-flight[cid] = paths  -> in-flight から削除
                       pending = {}               reviewed[path] = diff hash

cursor 失敗時: restore_claim() で pending へ戻す (未レビューのため)
kill された時: in-flight が残る -> TTL 超過を後続 Stop が pending へ回収
```

削除ではなく予約にするのは、割り込みで Stop hook がプロセスごと落ちるため
(上表 22:23:39 が実例)。単純な drain だとその瞬間にパスが消えて永久に未レビューになる。
`UserPromptSubmit` でリセットしないのは、バックグラウンドで走り続ける Stop hook と
競合するため (公式 `security-guidance/hooks/diffstate.py` が同じ race を踏んで TTL ベースに
置き換えている)。境界は「直前の user メッセージ以降」ではなく「前回 Stop の消費時点以降」で、
両者は cursor 失敗・割り込みの持ち越しケースで食い違う。詳細は `state.py` の docstring。

### 失敗時の復元は「片側だけ」

| 結果 | in-flight | pending | reviewed hash |
|---|---|---|---|
| `cursor.review()` が None (失敗) | 削除 | **戻す** | 記録しない |
| REVIEW_CLEAN | 削除 | 戻さない | **記録する** |
| block | 削除 | 戻さない | **記録する** |

REVIEW_CLEAN でパスを戻すと、毎ターン同じファイルを再レビューする元のバグを作り直す。
逆に失敗時に hash を記録すると、未レビューの変更が「レビュー済み」扱いで永久に skip される。

### TTL は cursor の timeout を超える必要がある

`IN_FLIGHT_TTL_SEC = cursor.TIMEOUT_SEC + 300`。これは運用上の推奨ではなく**正しさの制約**で、
下回ると正常に走っている in-flight を別の Stop が途中で横取りする。手で乖離できないよう
`cursor.TIMEOUT_SEC` から導出している (`tests/test_state.py::test_ttl_derives_from_cursor_timeout`)。

## ロックは 2 種類。ネストしたまま cursor を回さない

| ロック | 対象 | 保持時間 |
|---|---|---|
| state lock | 状態ファイルの read-modify-write | 短時間 (ms) |
| cursor lock | cwd をキーに `cursor agent` を直列化 | `review()` 実行中ずっと (最大 600s) |

Stop の取得順は **cursor lock → state lock → (state 解放) → review**。
state lock を握ったまま review すると、全セッションの PostToolUse が最大 600 秒ブロックされる。
PostToolUse は state lock しか取らないため、この順序で循環待ちは起きない。

**cursor lock に stale claim TTL を置いていないのは意図的**。flock はプロセス終了時に
カーネルが解放するので、TTL を足すと「まだ走っている cursor のロックを奪う」経路を
自分で作ることになる。残る穴は、hook が SIGKILL されたとき cursor の子プロセスが孤児として
ロックより長生きしうる点のみ。

## レビュー粒度は HEAD 基準 + パス単位 diff hash

`git diff HEAD -- <path>` は「そのファイルの HEAD 以降の全変更」を返すため、
turn 1 と turn 7 で同じファイルを編集すると turn 1 の hunk が turn 7 でも再掲される。

選択肢と判断は `gitscan.py` の docstring に記載。要約すると **HEAD 基準を維持** (レビュアーに
ファイル全体の変更文脈を渡すため) した上で、**パス単位の diff hash** で重複を潰す:
そのパスの diff が前回レビュー時と 1 バイトも変わっていなければレビューに載せない。

## 実機で確認した前提 (CLI 2.1.233, 2026-08-16)

推測で組むと壊れる箇所なので、nested `claude -p --plugin-dir` で payload を実測した。

| 前提 | 実測結果 |
|---|---|
| `PostToolUse` はサブエージェントのツール呼び出しでも発火するか | **発火する**。`session_id` は親と同一で、`agent_id` / `agent_type` が追加で付く。サブエージェントの編集は親セッションの成果なのでそのまま対象に含める |
| `tool_input.file_path` の安定性 | Write / Edit ともに**絶対パス**で安定 |
| `NotebookEdit` | 現環境に**非搭載**。搭載環境向けに matcher には残し、`notebook_path` も見る |
| `MultiEdit` | 現環境に**存在しない**。公式の matcher `Edit\|Write\|MultiEdit\|NotebookEdit` をそのまま写さず外した |
| `session_id` の compact / resume 耐性 | `--resume` / `--continue` / `/compact` のいずれでも**不変**。pending の孤児化は起きない |
| `tool_use_id` | `PreToolUse` / `PostToolUse` 双方に存在 → Bash の pre/post スナップショットを対応付けられる |

## 却下した設計案

- **作業ツリー全体の blob hash スナップショット** — 編集手段を問わず完全に拾えるが、
  **どのセッションの変更か区別できない**ためセッション分離の要件を満たせない
- **transcript 解析 (ステートレス)** — 状態ファイルもロックも不要だが、再現できる境界が
  「直前の user メッセージ以降」で要件と別物。持ち越し分を永久にレビューできない。
  加えて transcript の JSONL 形状は非公式でフォーマット変更に弱い

## $TMPDIR のレイアウトと GC

```
$TMPDIR/post-implementation-review/
├── state/<session_id>.json                  pending / in_flight / reviewed
├── bashsnap/<session>__<tool_use_id>.json   Bash 実行前のスナップショット
├── locks/cursor-<cwd hash>.lock             cursor 直列化ロック
└── reviews/<session_id>.txt                 レビュー結果の参照コピー
```

Stop のたびに `stategc.gc_stale()` が mtime 48 時間超のファイルを削除する。
v0.2.0 以前が残した `$TMPDIR/post-review-markers/` と `$TMPDIR/post-review-*.txt` も
同じ TTL で掃除する (旧版が並行稼働していても書き込み直後のファイルは消さない)。

保持中の cursor lock ファイルは取得時に `os.utime` で mtime を更新している。
GC が保持中のロックを消すと inode が分岐して排他が壊れるため
(`tests/test_state.py::test_held_lock_file_survives_gc`)。

## 撤廃した `DEFAULT_MAX_REVIEWS`

v0.2.0 の `DEFAULT_MAX_REVIEWS = 2` は**セッション単位**の予算で、長いセッションでは
3 ターン目以降レビューが黙って止まっていた。block 後の Stop は `stop_hook_active=true` で
来るため**同一ターン内の「block → 修正 → 再レビュー」は一度も発生しない** (修正は次ターンの
Stop でレビューされる — 意図した挙動)。つまり MAX=2 は事実上「ターンをまたいだ回数制限」
としてしか効いておらず、ターンスコープ化で不要になったので撤廃した。

`EXTERNAL_AI_POST_REVIEW_MAX=0` を無効化スイッチとして使っていた環境があるため、
**`0` のときだけ**後方互換で無効化として解釈する (それ以外の値は無視)。
新しい正規のスイッチは `EXTERNAL_AI_POST_REVIEW=0`。

## テスト

```bash
cd hooks/post-implementation-review
python3 -m unittest discover tests     # CI と同じ実行経路
pytest tests/                          # pytest でも動く (conftest.py で sys.path 整備)
```

`tests/` は受け入れ基準に 1:1 対応している:

| 基準 | テスト |
|---|---|
| 編集 0 件のターンで cursor が起動しない | `TestNoEditsNoReview` |
| セッション A の編集が B のレビュー対象に入らない | `TestSessionIsolation` |
| REVIEW_CLEAN の後に再レビューされない | `TestReviewedPathsAreNotRepeated` |
| cursor 失敗の後に再レビューされる | `TestCursorFailureRestoresPaths` |
| kill された in-flight が TTL 経過後に回収される | `TestInFlightRecovery` |
| `sed -i` など Bash 経由の変更でもレビューが走る | `TestBashAttribution` |
| cursor agent が同時に 2 つ起動しない | `TestCursorSerialization` |
| TTL 超過した状態ファイルが削除される | `TestGc` |
| 未追跡ファイルのみの新規作成でもレビューが走る | `TestUntrackedOnly` |
| 作業ツリー外の絶対パスが除外される | `TestOutsideWorktree` |
| 上限超過パスを黙って捨てない | `TestOverflowCarryOver` |

`TestBashAttribution.test_sed_on_already_dirty_file` は**すでに dirty なファイルを
同一バイト数で書き換える**という最も厳しい条件を使っている。clean なファイルから始めると
行集合比較の素朴な実装でもテストが通ってしまい、バグが素通りする。

hook は合成 stdin で直接起動できるので `/plugin` 更新なしで手動確認もできるが、
**必ず `TMPDIR` を一時ディレクトリに差し替えること** (本番の状態ファイルを消費してしまう)。

## 発火しないときの確認手順

1. `which cursor` — 未インストールなら no-op 終了が期待動作
2. `env | grep EXTERNAL_AI_POST_REVIEW` — `0` で無効化されていないか
3. `cat $TMPDIR/post-implementation-review/state/<session_id>.json` —
   `pending` が空なら「このセッションはこのターンで何も編集していない」が正しい判定
4. `in_flight` にエントリが残り続けている → 前回の Stop が kill された。
   TTL (`cursor.TIMEOUT_SEC + 300` 秒) 経過後の Stop で回収される
5. stderr の `[post-implementation-review]` プレフィクス付きログを確認
6. 他セッションが `cursor agent` を走らせている間は skip する
   (「同一作業ツリーで別セッションがレビュー中」ログ)
