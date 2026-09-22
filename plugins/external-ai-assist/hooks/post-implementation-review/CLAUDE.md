# post-implementation-review

`PreToolUse(Bash)` / `PostToolUse(Write|Edit|NotebookEdit)` / `PostToolUse(Bash)` / `Stop` の
4 登録で動作し、**そのターンにこのセッションが変更したファイルだけ**を Cursor に差分
レビューさせ、critical な指摘があれば `hookSpecificOutput.additionalContext`
(既定 `auto`。0.8.0 から) で Claude に返す。0.12.0 からは、Bash の中で `git commit` が
あれば「その Bash の直前に Stop が来ていたら送っていたのと同一だと示せる差分」だけを
同じ経路でレビューする (「commit 単位レビュー」節)。`auto` は実行中の Claude Code の版数を自動検出し、2.1.163 未満・不明
なら 0.7.0 までの `decision: block` に自動で fail-closed する (`EXTERNAL_AI_POST_REVIEW_MODE`
に `block`/`context` を明示すれば固定できる。詳細は「出力形式: hook error に見せない
(0.8.0)」節)。

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
├── gitscan.py          パス正規化 + git status スナップショット + パス単位 diff + 窓の範囲 diff / stat 突合 (0.12.0)
├── reflog.py           Bash 窓の中で作られた commit の検出と窓の条件 W1〜W3 (0.12.0)
├── exclusion.py        外部に送らないファイルの判定 (既定 glob / 追加 glob / CODE_ONLY)
├── selection.py        送信先の候補 / 選択戦略 / フォールバックと待ち時間の上限 (0.12.0)
├── cursor.py           cursor agent 呼び出し (既定 300s / 上限 600s。起動は hooks/_common/backends)
├── codex.py            codex 呼び出し (同条件。**既定では使わない**)
├── prompts/
│   ├── post-implementation-cursor.md
│   └── post-implementation-codex.md
└── tests/              受け入れ基準の unittest スイート
```

## 3 phase の役割

| phase | hook | 役割 |
|---|---|---|
| `pre-tool` | `PreToolUse(Bash)` | Bash 実行前の `git status` スナップショットと **HEAD reflog の窓の起点** (0.12.0) を `tool_use_id` キーで保存 |
| `post-tool` | `PostToolUse(Write/Edit/NotebookEdit)` / `PostToolUse(Bash)` | 変更パスを `session_id` キーの `pending` に積む。Bash では加えて **窓の中の commit をレビューして配信** (0.12.0) |
| `stop` | `Stop` | `pending` を claim してレビュー、結果を配信 (**未 commit の差分専任**) |

`hooks.json` の `PostToolUse` は **matcher を 2 つに割ってある** (0.12.0):
編集系 (`Write|Edit|NotebookEdit`) は git を一切呼ばないので timeout 10 秒のまま、
Bash 側は commit レビューで外部 AI CLI を起動しうるので Stop と同じ組み方の予算
(720 秒) を取る。`PreToolUse(Bash)` は `rev-parse` が 1 回増えたので 10 → 15 秒。
突合は `tests/test_review_set.py::TestTimeoutBudgets`。

### なぜ Bash にも張るのか

`sed -i` / フォーマッタ / スクリプト生成によるファイル変更は Write/Edit だけ見ていると
取りこぼす。Bash 実行の前後で `git status --porcelain` を突き合わせれば、Bash 経由の
変更も**どのセッションがやったか付きで**拾える。公式
`claude-plugins-official/security-guidance` も `PostToolUse` に `Bash` matcher を使う先例がある。

コストは Bash 呼び出しごとに hook プロセス 2 回 (`git status` 各 1 回)。実測
(macOS / CLI 2.1.233):

| 対象 | `git status -uall` | hook プロセス全体 |
|---|---|---|
| `~/.claude` (54 tracked / 実ファイル 6255) | 9.1 ms | 約 35 ms |
| `worktools` | 10.1 ms | 約 36 ms |

`-uall` でも ignore されたディレクトリは git が subtree ごと skip するため、
実ファイル数ではなく「ignore されていない木の大きさ」で決まる。**Bash 1 回あたり
約 70 ms (機能が有効かつ cursor 導入済みの場合のみ)** で、既定 ON のままで問題ない
水準。支配項は git ではなく Python のインタプリタ起動 (`python3 -c pass` だけで
8.7 ms)。`EXTERNAL_AI_POST_REVIEW=0` または cursor 未インストールの環境では
`handle_pre_tool` / `handle_post_tool` が先頭で即 return し、この git 呼び出し自体
発生しない。

**0.12.0 の commit 検出を足した分の増分** (macOS, Python 3.14, 40 ファイルの repo を
dirty にした状態で `commit を含まない` Bash を 30 回ずつ計測した中央値。0.12.0 Phase A
版を `git archive` で展開して同条件で比較):

| phase | Phase A | Phase B (commit 検出あり) | 増分 |
|---|---|---|---|
| `pre-tool` | 45.9 ms | 55.1 ms | **+9.2 ms** (`git rev-parse --git-path logs/HEAD HEAD` 1 回 + 小さな JSON 書込) |
| `post-tool` | 45.9 ms | 46.3 ms | **+0.4 ms** (reflog ファイルの stat。追記が無ければ読まない) |
| 合計 | 91.8 ms | 101.5 ms | **+9.7 ms** |

commit を**含む** Bash では、ここに `--name-only` と diff の取得 + 外部 AI CLI の
待ち時間が乗る (最悪ケースの積み上げは `tests/test_review_set.py::TestTimeoutBudgets::
test_post_tool_bash_budget_covers_commit_review`)。

**0.12.0 の内容指紋 (P6) を足した分の増分** (同じ機械 / Python 3.14。40 ファイルを
dirty にした repo で `PostToolUse(Write)` を 30 回ずつ起動した中央値。同じ hook を
`EXTERNAL_AI_POST_REVIEW_COMMIT` の 0/1 で A/B した — `0` のとき指紋を記録しない):

| 書いたファイルのサイズ | 指紋なし | 指紋あり | 増分 |
|---|---|---|---|
| 4 KiB | 26.0 ms | 26.2 ms | **+0.2 ms** |
| 64 KiB | 26.1 ms | 26.1 ms | **+0.0 ms** |
| 1 MiB (`FINGERPRINT_MAX_BYTES`) | 26.2 ms | 26.8 ms | **+0.7 ms** |

支配項は Python のインタプリタ起動のままで、sha256 は上限いっぱいでも 1 ms 未満。
commit を**含む** Bash 側では `git ls-files -v` (P4') 1 回と `git cat-file
--batch-check` / `--batch` (P6) 各 1 回が増える。

commit を**含まない** Bash (pre-tool + post-tool を 1 組で 30 回、中央値) は
`EXTERNAL_AI_POST_REVIEW_COMMIT` の 0/1 で 89.5 ms → 97.7 ms。この差は 0.12.0 の
commit 検出 (pre-tool の `rev-parse` 1 回) がそのまま出たもので、**P6 は commit の
無い窓では state を一切開かない**ので上乗せしない (`_expire_fingerprints` は
変化 0 件なら即 return、`_handle_bash` は `commits` が空なら指紋を読まない。
`state` の読み出しは flock + read-modify-write なので「読むだけ」でも書き込みになる)。

極端に大きい作業ツリーで重い場合は `EXTERNAL_AI_POST_REVIEW_BASH_TRACKING=0` で
切れる (その場合 `sed -i` 等の Bash 経由の変更は拾えなくなる)。

**pre/post のスナップショット比較は行の集合ではなくタプル比較**であること。
すでに HEAD から変更済みのファイルを `sed -i` で書き換えると porcelain の行は
` M seed.txt` のまま変わらず、行集合の差分では検出できない。
`(status_code, size, mtime_ns)` まで見て初めて拾える (`gitscan.status_snapshot`)。

**入れ子の git リポジトリは `-uall` でも `dir/` のまま返る** (`.claude/worktrees/<name>/`
を作った場合など)。中身は別リポジトリの変更なので、末尾 `/` のエントリは snapshot から
捨てる。`_resolve_paths` 側でもディレクトリを弾いており、旧版が state に書いた
エントリを掴まない。

**ただしこれは Bash 経由の経路にしか効かない。** Write / Edit は `tool_input.file_path`
の絶対パスをそのまま pending に積むので、`git status` を一度も通らない。cwd が外側の
checkout のセッション (非隔離サブエージェントを含む) が
`<root>/.claude/worktrees/<name>/...` の中のファイルを編集すると、root 基準の
`git diff HEAD -- <rel>` が**必ず空**になり、「差分が空で取得できませんでした
(commit 済みの可能性)」を毎ターン出し続けていた (内容は送っていない)。

`_resolve_paths` に `_in_nested_git()` を置いて、**root とそのファイルの間のどこかの
親ディレクトリに `.git` がある**パスを**作業ツリー外と同じ扱い**で落とす
(`git worktree add` と submodule の `.git` はファイル、`git init` のそれは
ディレクトリなので `os.path.exists` で両方を見る。**submodule も同じ判定で落ちる** —
0.11.0 は submodule 内のパスを「差分が空で取得できませんでした」と通知していたが、
0.12.0 は通知しない。`.git` という名前の通常ファイルがある親の配下も同様)。戻さない・hash を記録しない・通知しない・送らない。
git は呼ばず `os.path.exists` だけで、同じ親は呼び出し内で memo する。root 自身の
`.git` は見ない (見ると全パスが落ちる)。

**入れ子側の diff を取りに行く経路は作らない。** その作業ツリーを cwd にしたセッション
(隔離サブエージェントを含む) が自分の hook で見るのが正しい担当分けで、外側から覗くと
「別の作業ツリーの内容を送る」経路を新設することになる。床は
`tests/test_stop_flow.py::TestNestedWorktree`。

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

パス単位では、cursor に渡す前に次の振り分けがある (0.5.0):

| パスの扱い | pending | reviewed hash | 通知 |
|---|---|---|---|
| 作業ツリー外 / ディレクトリ / **入れ子の作業ツリー・git リポジトリ・submodule の中** | 戻さない | 記録しない | なし |
| **除外** (exclusion.py に当たる) | 戻さない (恒久) | 記録しない | ファイル名 + 理由 |
| 上限 (`MAX_REVIEW_PATHS`) / 時間予算 (`COLLECT_BUDGET_SEC`) の繰り越し | **戻す** | 記録しない | ファイル名 |
| 合計予算 (`MAX_DIFF_BYTES`) に収まらない | **戻す** | 記録しない | ファイル名 |
| 1 ファイル上限 (`MAX_FILE_DIFF_BYTES`) 超過 → 先頭のみ送信 | 戻さない | **記録する** (全文の hash) | ファイル名 + バイト数 |

## 外部に送らないファイルと予算 (0.5.0)

**除外** は `_resolve_paths` で、作業ツリー外パスと同じ位置で落とす。`MAX_REVIEW_PATHS` の
手前なので枠を食わず、overflow として pending に戻ることもない。規則 (既定 glob /
`EXTERNAL_AI_POST_REVIEW_EXCLUDE` / `EXTERNAL_AI_POST_REVIEW_CODE_ONLY`) は `exclusion.py` の
docstring と README を参照。判定には実体 (realpath 相対。git に渡すのもこれ)、lexical なパス
(root だけ realpath で同定し、配下の symlink 構成要素名を保持: `_lexical_relative`)、および
作業ツリー内の symlink から作った別名 (`gitscan.symlink_map` + `exclusion.expand_aliases`) を
渡し、どれかが当たれば除外する。`credentials/` → `ordinary/` の symlink ディレクトリ経由でも
機密名のリンクでも、root の別名 (`/tmp` → `/private/tmp`) 経由でも落ちない。別名が要るのは
Bash 経由の変更で、`git status` は実体名 (`ordinary/data.json`) しか返さないため lexical 名が
claim に現れない (マージ前レビューの指摘)。symlink の列挙は tracked が index (mode 120000)、untracked が
root から 3 階層の BFS scandir (5000 エントリ / 500 件で打ち切り)。Stop の git 予算は
rev-parse 2秒×2 (worktree_root + head_exists) + ls-files (symlink) 10秒
+ ls-files (untracked) 10秒 + diff 収集 30秒 + 予算判定後の最後の 1 パス分 diff 5秒
= 59s (詳細は `gitscan.py` モジュール docstring の予算表)。

**予算** は `_collect_diffs` がファイル単位で積む。0.4.1 までは結合後に末尾を切っていたため、
切り落とされたファイルの hash まで記録され「Cursor が見ていないのにレビュー済み」になっていた。
収まらないファイルは送らず pending に戻す (hash なし)。1 ファイルが `MAX_FILE_DIFF_BYTES` を
超える場合だけ先頭を `(truncated)` 付きで送り、その時だけ hash (全文で計算) を記録する。

**順序は claim 順** (`_resolve_paths` はソートしない)。繰り越したパスは claim 順 (予算超過 →
時間切れ → `MAX_REVIEW_PATHS` の overflow) で 1 回の `record_pending` により空の pending に最初に
積まれるので次ターンの先頭に来る。`MAX_FILE_DIFF_BYTES <= MAX_DIFF_BYTES` により先頭のファイルは
必ず収まるため、繰り越しが永久に続くことはない
(`tests/test_stop_flow.py::TestByteBudgetFlow::test_deferred_file_is_not_starved_by_new_edits`)。
`MAX_PENDING_PATHS` の上限超過は末尾 (新しい編集) から落とし、先頭の繰り越し分を守る。

**git にはパスを literal pathspec で渡す** (`gitscan._git` の `--literal-pathspecs`)。既定では
`app/[id]/page.tsx` が `app/i/page.tsx` にもマッチし、claim していない別セッションのファイルの
diff が混入する (旧 state の `[.]env` が tracked の `.env` を拾う経路も同じ)。`git diff` には
`--no-color` も付ける (`color.ui=always` で ANSI が混ざると hash と予算が狂う)。

除外・繰り越し・切り詰めはファイル名と理由を `systemMessage`
(指摘ありのターンは既定で `hookSpecificOutput`、`MODE=block` なら `decision` と同居) と
stderr に出す。内容は出さない。`systemMessage` は公式 docs の全イベント共通フィールドで
Stop でも discard されないが、対話 UI 以外での表示は未確認なので stderr を併用している。

### TTL は cursor の timeout 上限を超える必要がある

`IN_FLIGHT_TTL_SEC = cursor.MAX_TIMEOUT_SEC + 300`。これは運用上の推奨ではなく**正しさの制約**で、
下回ると正常に走っている in-flight を別の Stop が途中で横取りする。手で乖離できないよう
cursor 側から導出している (`tests/test_state.py::test_ttl_derives_from_cursor_timeout_ceiling`)。

**導出元は既定値 (`TIMEOUT_SEC`) ではなく上限 (`MAX_TIMEOUT_SEC`)** (0.6.0)。
`EXTERNAL_AI_POST_REVIEW_TIMEOUT` で timeout が可変になったため、既定値から導くと
「timeout を短く設定したセッションが、長く設定した別セッションの in-flight を TTL 超過と
みなして奪う」経路ができる。TTL は全セッションで同じ値でなければならない。

同じ理由で、hooks.json の hook timeout に対する予算テストも上限側で検証する
(`tests/test_review_set.py::TestTimeoutBudgets`)。既定値で見ると「env を上限まで設定した
最悪ケース」を誰も守らなくなる。

## 送信先の選択 (0.12.0)

どの外部 AI に差分を送るかは `selection.py` が決める。**既定は cursor のみ** =
0.11.0 と同じ送信先で、`_common/backends` の registry に backend を足しても変わらない
(`DEFAULT_BACKENDS`)。`EXTERNAL_AI_POST_REVIEW_BACKENDS` を利用者が書いたときだけ
候補が増える。

| 戦略 (`EXTERNAL_AI_POST_REVIEW_STRATEGY`) | 試す順 |
|---|---|
| `fixed` | 列挙順そのまま |
| `available` | 今使えるものを先に、残りを後ろに (**落とさない**) |
| `alternate` (候補 2 つ以上の既定) | 前回レビューを返した backend 以外を先に |
| `random` | シャッフル |

**どの戦略も候補集合を変えない** (並べ替えるだけ)。`available` で「今は使えない」と
判定された backend も末尾に残すのは、検出が probe の予算切れで保留に倒れることが
あり、そこで候補ごと捨てるとレビューが黙って止まる経路になるため (送信先が増える
わけではないので残す側が安全)。

### フォールバックの 2 つの境界

- **集合の境界**: 次の候補は**利用者が列挙した集合の中**からしか選ばない。未知の名前は
  `backends.select()` が落として通知に回す (既定の全件へ fallback しない)
- **時間の境界**: 2 つ目以降は「その backend の `timeout_sec()` + 停止猶予
  (`3 × KILL_GRACE_SEC`)」が `MAX_TOTAL_TIMEOUT_SEC` (= 全 backend の上限の最大) の
  残り予算に収まるときだけ起動する。**第一候補だけは残り予算を見ずに起動する**ので、
  1 回の Stop の最悪待ち時間は `worst_case_wall_sec()` = 615 秒 — 0.11.0 の cursor 単体
  と同じ値で、`hooks.json` の Stop timeout 690 秒の予算計算は変わっていない
  (`tests/test_review_set.py::TestTimeoutBudgets`)

帰結として**第一候補が timeout いっぱい待って失敗した場合は次へ回らない** (既定 300 秒
なら 315 + 300 + 15 > 600)。フォールバックが効くのは候補が速く失敗したとき
(未インストール / 即エラー / 利用上限) で、そこがこの機能の狙いでもある。

### `alternate` のための 1 値

`state.last_backend` に**実際にレビュー結果を返した** backend 名だけを持つ。失敗した
backend は記録しない — この値の意味は「前回どこにレビューさせたか」であって「前回どこが
落ちたか」ではない (同じ行を 2 回見るときに別の目で見せるのが目的)。パス単位の記録も
「レビュー済み hunk」の重複除去 state も作らない (ユーザー決定)。

`_normalize()` がスカラーを引き継がないと**読むたびに空へ戻って `alternate` が毎回
「前回不明」になる** (`last_review_at` と同じ静かな壊れ方。`tests/test_state.py::
TestInFlightTtl::test_last_backend_survives_other_writes`)。

### backend の timeout 上限は揃える

`cursor.MAX_TIMEOUT_SEC == codex.MAX_TIMEOUT_SEC` は運用上の好みではなく制約。揃って
いないと `state.IN_FLIGHT_TTL_SEC` (= 全 backend の上限の最大 + 300) と Stop の hook
timeout 予算が**送信先の選択次第で動く**ため、異なる backend を選んだセッション同士が
互いの in-flight を奪い合う (`tests/test_selection.py::TestTotalWaitBudget::
test_all_backends_share_the_same_timeout_ceiling`)。

## ロックは 2 種類。ネストしたまま cursor を回さない

| ロック | 対象 | 保持時間 |
|---|---|---|
| state lock | 状態ファイルの read-modify-write | 短時間 (ms) |
| cursor lock | cwd をキーに `cursor agent` を直列化 | `review()` 実行中ずっと (既定 300s / 上限 600s) |

Stop の取得順は **cursor lock → state lock → (state 解放) → review**。
state lock を握ったまま review すると、全セッションの PostToolUse が cursor の timeout 上限
(600 秒) までブロックされる。

0.12.0 から **PostToolUse(Bash) も commit を含む窓では cursor lock を取る**
(外部 AI CLI を起動するため)。それでも循環待ちは起きない: `_handle_bash` は
state lock を**解放してから** cursor lock を**非ブロッキング (try-lock) でのみ**
取るので hold-and-wait が成立しない。取れなかった窓は**無言にせず** 1 行通知する
(窓は 1 回きりで次のレビューに回らないため。床は
`test_commit_flow.py::TestCommitReviewSerialization`)。

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

### HEAD 基準が空になったパスは復元せず通知する

HEAD 基準には副作用がある: pending に積んだ後、Stop までの間にそのパスを commit
すると `git diff HEAD -- <path>` が空になり、0.8.0 以前は黙って消費されて一度も
レビューされないまま消えていた。

**基点を「pending 記録時点の HEAD」にずらす素朴な案は採らない。** `git diff <base> --
<path>` は「base 時点の内容」対「現在のディスク上の内容」の比較なので、base 以降に
そのパスを**通過した全変更**を拾う。他人の commit が pull で入っただけでも成立し、
この plugin は差分を外部 AI CLI に送るため送信範囲が広がってしまう。

0.9.0 は一度、この基点を Stop 側で記録し、`git rev-list --count <base>..HEAD` と
`--not --remotes` 付きの同カウントを比較する「手元由来の証明」(その範囲の commit が
全てリモートに存在せず手元だけで作られたものか) が通ったときだけ基点まで遡った
diff を復元する経路を実装した。**この証明は「リモート由来でない」ことしか示して
おらず、「このセッションが書いた」ことは示していない。** 同一 worktree を共有する
別のローカルの書き手 (別セッション・人間の手動 commit) が push せずに同じパスへ
commit すると、その内容が丸ごと外部へ送信されてしまう — pull/merge 経由の混入は
正しく遮断できていたが、これは別ベクトルであり、マージ前レビューで実際に送信
されることが実演されたため復元経路そのものを撤去した。

**確定した設計 (復元せず、常に通知する)**:

- HEAD 基準 diff が空だった tracked パスは `batch.unretrievable` に積む
  (**ディスク上に実在するかは問わない**、マージ前レビューの指摘)。空になった理由
  (同一ターン内 commit・別の書き手の commit・pull/merge・単に無変更) は区別しない
  — 区別しても「復元してよいか」の判断には使わないため
  - 以前はここに「そのパスが実際に存在する (phantom な pending エントリでは
    ない)」という条件も加えていたが、これだと**追跡ファイルの削除が同一ターン内で
    commit されたケース** (HEAD にもディスクにもパスが無くなる) が通知対象から
    漏れて黙って消費されていた。一度も commit されていない phantom エントリ
    (作成後に同一ターン内で削除して commit しなかった一時ファイル等) との区別は
    cheap な git 状態だけでは付かない (`git cat-file -e HEAD:<path>` は削除
    commit 後どちらのケースでも失敗する) ため、区別を諦めて常に通知する側を
    選んだ (正当な削除の見落としの方が実害が大きいため)
- `_run_review` は `batch.unretrievable` を `systemMessage` に
  「差分が空で取得できませんでした (commit 済みの可能性。内容は送信していません)」
  として列挙し、pending からは外す (黙って消費しない。ただし取得できなかった旨を
  伝えるだけで、commit されたと断定はしない — 実際には revert のみで commit が
  無かった可能性もあるため)
- 安全な復元には編集時点の内容退避 (PostToolUse の時点でファイル内容を退避し、
  Stop 時にそれと比較する) が要るが、これは「PostToolUse を軽く保つ」という既存の
  設計意図と衝突するため、この batch では見送る

**失敗方向は明確: 送信範囲が広がる側には倒さない。** 復元できないときは常に
「取得できなかった」と可視化するだけにする。「黙って消える」を「取得できな
かったと報告される」に変えるのが本対応の主眼で、基点まで遡った復元は撤去した
(設計の変遷は `CHANGELOG.md` の該当節を参照)。regression テストは
`tests/test_stop_flow.py::TestSameTurnCommitNotification`
(`test_other_local_writer_commit_is_not_leaked` が今回撤去した脆弱性の再現、
`test_committed_deletion_is_reported_not_silently_dropped` が
「ディスク上に実在するか」を条件にしていた頃の見落としの再現)。

#### 復元を再実装するときの床 (0.11.0 で確認した不足)

「編集直後のディスク内容の指紋 (sha256) が Stop 時点の内容と一致するなら、HEAD に
入っている内容は自分が書いたものだ」という証明で復元する案を実装したが、**内容を
変えなかった編集**で破れることが分かったため出荷を取り消した。HEAD 基準 diff が
空になる最も多い原因は同一ターン内 commit ではなく「編集したが内容が変わらな
かった」(同じ内容の Write、revert して戻した編集) で、このとき指紋は当然一致する
一方、「そのパスを最後に変更した commit」はこのターンの成果ではない。その commit の
差分を復元すると、**このセッションが一度も見ていない内容** (その commit が消した行)
まで外部 AI CLI へ送ることになる。

床テスト: `tests/test_stop_flow.py::TestSameTurnCommitNotification`
`::test_noop_edit_does_not_send_an_unrelated_historical_diff` (撤去した実装の上では
落ちることを確認済み)。再実装するなら「内容がこのセッションのものか」ではなく
**「その commit をこのターンに作ったか」**を示す必要がある。

## commit 単位レビュー (0.12.0)

上の「復元を再実装するときの床」が求めていたのは **「その commit をこのターンに
作ったか」を示すこと**だった。reflog の追記を窓にすると、それが**推定なしで**分かる。
Stop は未 commit の差分専任のまま、commit そのものを別のレビュー単位にする。

実装は `reflog.py` (窓の作り方・対象行・fail-closed をモジュール docstring に逐語の
実測表つきで記載) と `__main__.py` の「commit 単位レビュー」節。

### 不変条件は I1' (等価性)。「安全な git 操作」の列挙はしない

最初の実装は「reflog の message が `commit:` 系なら、その `old..new` はその commit が
作った差分 = このセッションの成果」と置いた。**これは偽で、マージ前レビューで 3 経路が
実演された**:

| 経路 | なぜ通るか |
|---|---|
| `reviewed` を送信許可に使う | `reviewed` はセッション全履歴。一度編集したパスは以後ずっと許可になる |
| `git reset --soft <過去>` → `commit` | `commit:` 行の `old` が任意の過去になり、`old..new` が squash 範囲全体に広がる |
| `merge --squash` / `cherry-pick -n` / `checkout <ref> -- <path>` / `restore --source` / `stash pop` / `apply` → `commit` | **`logs/HEAD` に 1 行も書かない** (2026-09-20 実測) ので、素の `commit:` 行にしか見えない |

共通の根は「**操作の種類から内容の来歴を推論した**」こと。git には履歴を動かさずに
内容を運ぶ経路が多数あり、列挙は終わらない。確定した不変条件は等価性で書く:

> commit レビューが外部へ送る差分は、**その Bash の直前に Stop が来ていたら Stop
> 経路が送っていた差分 (`git diff HEAD -- <path>`) と同一**でなければならない。
> 同一だと示せないパスは内容を送らず、理由とファイル名だけを通知する。

Stop 経路の露出 (pending のパスの未 commit 内容) を**上限**に取り、それを 1 バイトも
超えない。条件の逐語と「なぜ等価になるか」の導出は `__main__.py` の該当節、床テストは
`tests/test_commit_flow.py::TestWindowEquivalence` (送信本文の hunk と、窓を開く直前に
Stop 経路が集めた diff の hunk が一致することを assert する)。

### 条件は窓側 4 つ・パス側 5 つ

| 種別 | 条件 | 実装 |
|---|---|---|
| W1 | reflog の fail-closed (pre 欠落 / 読めない / 短縮 / parse 不能 / 追記バイト上限 / commit 数上限) | `reflog.appended` |
| W2 | 追記が `commit:` 系の行だけ (`old == new` の行は無視) | `reflog.appended` |
| W3 | 行が連鎖 (先頭の old == pre の HEAD、以降 old_i == new_{i-1}) | `reflog.appended` |
| W4 | pre-tool の `git status` スナップショットがある | `_commit_review` |
| P1 | pending ∪ in-flight にある (**reviewed は含めない**) | `state.recorded_paths` |
| P2 | pre の status に載っている (窓を開いた時点で dirty) | `_pre_stat` |
| P3' | `(size, mtime_ns, ctime_ns)` が pre と現在で一致 | `gitscan.stat_entry` |
| P4' | index のタグが `H` (通常) | `gitscan.flagged_index_entries` |
| P4 | commit 後に `git diff HEAD -- <path>` が空 | `gitscan.changed_vs_head` |
| **P6** | **commit された blob の生バイトの sha256 = 編集ツールが最後に書いた内容の指紋** | `state.fingerprints` / `gitscan.blob_digests` |
| P5 | 既存の除外規則と予算 | `_resolve_paths` / `_collect_commit_diffs` |
| — | Stop がレビュー済みの内容 (`reviewed` の hash と同値) は送らない | `_already_reviewed` |

**W2 と W3 は等価性の前提** (「窓の終わりの HEAD = 最後の `<new>`」「基点 = 窓の
始まりの HEAD」) を成り立たせるための条件で、それ自体は十分条件ではない。

**P3' と P4 は代理判定で、どちらも独立に偽になる** (マージ前レビュー 2 巡目で実演):

| 条件 | 何を代理しているか | どう破れるか |
|---|---|---|
| P3' | 「窓の間に作業ツリーが書き換わっていない」 | `(size, mtime_ns)` は `touch -r` / `cp -p` / `rsync -t` / `tar -xp` で復元できる。`ctime_ns` を足して `os.utime` は閉じたが、**粒度の粗い FS** (HFS+ / exFAT / bind mount) では同一秒内の書き換えを閉じられない |
| P4 | 「作業ツリーの内容がそのまま commit された」 | `git diff HEAD` は `assume-unchanged` / `skip-worktree` のエントリで**作業ツリーを見ない** (index の他者版が commit されても空になる)。P4' で index のタグを見て塞ぐ |

**最後の砦は P6**。commit された blob のバイトを直接読んで指紋と突き合わせるので、
P3' / P4' / P4 のどれが破れても「送る差分の新しい側 = このセッションのツールが
書いたバイト列」が崩れない。

ただし **P6 にも前提が 1 つある**: 指紋は「編集ツールの直後に `PostToolUse` が
ディスク上で見たバイト列」であって、ツールが書いたバイト列そのものではない。
ツールの書き込みと hook の読み取りの間に別の書き手が同じパスを上書きすれば、指紋は
その内容を承認する。0.11.0 の Stop も同じもの (pending のパスの現在の内容) を送るので
送信範囲は広がらないが、**「編集ツールが書いた」ことの証明ではない**。閉じるには
ツールの書き込み内容そのものをハーネスから受け取る必要があり、hook の入力には
含まれていない。

床テストは
`test_commit_flow.py::TestRestoredStatAndIndexFlags` (3 攻撃) で、これは
**P3' と P4' を両方外した使い捨てコピーでも green** になる (= P6 単独で止まる)。
逆に P6 だけ外しても P3' / P4' が個別に止め、3 つとも外すと 3 件とも落ちる
(床が空でないことの負の対照)。

### P6 と「撤去した内容指紋による復元」の違い

0.11.0 で撤去したのは、**指紋の一致を根拠に基点を過去の commit へ探しに行く**案
だった。内容を変えない編集をすると指紋は当然一致し、「そのパスを最後に変更した
commit」= このターンの成果ではない commit の差分 (その commit が消した行を含む) を
送ってしまう。0.12.0 の P6 は**基点を reflog の窓で固定したまま**で、指紋は
「新しい側の内容の同定」にしか使わない。同じ攻撃 (同一内容の書き込み + 無関係な
過去 commit) を commit 経路でも床にしてある
(`TestSendScope::test_noop_edit_does_not_send_an_unrelated_historical_diff`)。

### 指紋の記録と失効

- `PostToolUse(Write/Edit/NotebookEdit)` で、書かれた直後のファイルの生バイトの
  sha256 を `state.fingerprints[<絶対パス>]` に記録する (最後の編集が勝つ)。
  **git は呼ばない** — この経路は hook timeout 10 秒を git 無しで回している
- 1 MiB (`FINGERPRINT_MAX_BYTES`) 超 / 読めない / 通常ファイルでない (symlink 等) は
  **エントリを消す** = 指紋なし = そのパスは commit レビューで内容を送らない
- `_expire_fingerprints` は、Bash が**作業ツリーのファイルを書き換えた**パス
  (P3' と同じ突合で判定) の指紋を捨てる。**`changed_between` が返すもの全部を
  捨ててはいけない**: 「status から消えた」(= commit されて HEAD と一致した) も
  変化として返るので、全部捨てると「Write → 次の Bash で commit」という主要フローで
  指紋が commit レビューの直前に消える。`git add` のように status の code だけが
  動くケースも同じ理由で残す (実測: `git status` / `git add` / `git commit` は
  作業ツリーのファイルの `[size, mtime_ns, ctime_ns]` を変えない)
- pending から外れたら消す (`drop_pending` / `complete_claim`)。`restore_claim` は
  pending へ戻すので残す。記録のたびに pending ∪ in-flight に無いキーを刈る
- `_handle_bash` は **P1 の集合を `_record_bash_changes` の前に、指紋をその後に**
  読む。前者は「他者のパスを P1 に入れない」ため、後者は「同じ Bash が書き換えた
  ファイルの失効をこの窓にも効かせる」ため

### `changed_between` に `ctime_ns` を入れない理由

スナップショットのエントリは `[code, size, mtime_ns, ctime_ns]` だが、
`gitscan.changed_between` は `[:3]` しか比較しない (`_CHANGE_FIELDS`)。chmod や
xattr の書き込みは ctime だけを動かすので、比較に入れると**他の書き手のファイルが
「このセッションが変更した」として pending に入り、Stop の送信範囲が広がる**。
`ctime_ns` が要るのは P3' の突合だけなので、そこに閉じる。

### 送る差分は窓全体で 1 本

`git diff <窓を開いた時点の HEAD> <最後の new> -- <path>`。commit ごとに分けると
等価性が成り立たない (窓の途中の状態は Stop からは見えない)。副次的に `range_paths` も
`git diff --name-only` 1 回で済む。

### P1 の集合を読む順序と、pending に積む側の guard

`_handle_bash` は **`_record_bash_changes` より前に** `state.recorded_paths()` を
読む。`gitscan.changed_between` は「status から消えたパス」も変化として返す
(commit / checkout で HEAD と一致したケースを拾うため) ので、`git commit -a` を
走らせた Bash では**他人が作業ツリーに残していた変更**まで pending に入る。その後で
積集合を取ると、他人の内容が P1 を通ってしまう。

逆に「レビューを先に回してから status を撮る」形にもしない — レビューは最悪 10 分
かかるので、その間の別セッションの編集を全部このセッションに帰属させてしまう。

**読む順の入れ替えだけでは足りない** (マージ前レビューの指摘): 積まれた他者パスは
pending に残り、**次の窓では正規の送信許可**になっていた。commit のあった窓 (および
窓が信用できなかった窓) では、「status から消えただけで P1 の集合にも無い」パスを
`_record_bash_changes` が積まない。post にまだ残っているパス (= 未 commit の変更が
現にある) は従来どおり積む — こちらは Bash が実際に作業ツリーを変えた証拠で、
0.11.0 の Stop も同じものを見る。

**guard は「窓が信用できなかった窓」(W1〜W3 の fail-closed) にも掛けている** —
設計書は「commit を検出した窓」だけを指定していたが、W2 で捨てた窓でも他者のパスは
status から消えて pending に入りうるため広げた。帰結として、その窓で status から
消えたパスは **0.11.0 なら Stop が「差分が空で取得できませんでした」と名前を出して
いたところ、名前がどこにも出なくなる** (窓レベルの 1 行だけ)。送信範囲は 0.11.0 より
狭い側の変化なので受け入れるが、「黙って消える」を潰すのがこの plugin の趣旨である
以上、限界として明示しておく。commit の無い普通の Bash (`commits == []`) は
0.11.0 と同じ挙動のまま。

### fail-closed / 条件の中には「送信の有無では mutation を判別できない」ものがある

- `reflog.py` の「reflog ファイルが読めない」は、外しても**読むデータ自体が無い**ので
  送信は起きない。床テスト (`TestFailClosed::test_unreadable_reflog_sends_nothing`) が
  固定しているのは「例外で hook を落とさない」「別経路で窓を作り直さない」こと
- **W4** (pre の status が無い窓) も、外すと空の status として扱われ P2 が全パスを
  落とすため送信は起きない。床テスト
  (`TestMissingPreStatus`) は**利用者への理由通知**まで含めて固定する
- **`range_paths` の取得失敗** を `[]` として続行しても、パスが 1 つも無いので送信も
  通知も起きない (`None` チェックは fail-closed の明示と debug log のため残している)
- **P3' / P4'** は P6 を足した後、送信の有無では判別できない — 3 攻撃はどれも P6
  だけで送信ゼロになるため。床は**利用者に返す「落とした理由」の区分**で固定する
  (`TestProxyCheckSkipReasons`)。多層防御を黙って失わないための措置で、送信範囲の
  差ではない
- **P1 の集合を読む順序**も同じ理由で送信では測れない (積む側の guard と P6 が同じ
  漏れ方を二重に塞ぐ)。`TestRecordedSetIsReadFirst` は呼び出し順そのものを固定する

これ以外の W2 / W3 / P1 / P2 / P4 / P6 / 重複抑止 / backend lock /
`COMMIT_PREFIXES` は、それぞれを単独で外す mutation が `tests/` の対応する床テストを
assertion failure で落とすことを確認済み (共通ルールの「mutation が実バグの再現に
なっているか先に確認する」)。

### state の扱い

- **成功時**: 送ったパスのうち、作業ツリーの内容がレビューした commit と同じもの
  (`git diff <窓の最後の commit> -- <path>` に出ないもの) を pending から外す
  (`state.drop_pending`)。外さないと Stop が「差分が空で取得でき
  ませんでした」と誤通知する。判定は `gitscan.changed_vs_commit` の **git 1 回**
  (パスごとに回すと最悪 60 回で PostToolUse の予算に収まらない)。送信前の P4 で
  同じ確認をしているが、レビューの待ち時間 (最悪 10 分) の間に再編集されうるので
  **ここでもう一度確認する**
- **`reviewed` には書かない**: `reviewed` の値は *HEAD 基準* diff の hash で、commit
  レビューが見たのは窓の range diff。空 diff の hash を入れても `_collect_diffs` は
  空 diff を hash 判定より手前で落とすので一度も参照されず、LRU の枠を食って本物の
  エントリを追い出すだけになる
- **全 backend 失敗時、送った側 (`sent.sent_rels`) の pending は触らない**
  (`last_review_at` だけは Stop と同じく更新する — cooldown は「レビューとレビューの
  間隔」で成否を問わないため)。**重複抑止で送らなかった側 (`sent.deduplicated`) は
  backend の成否に関わらず settle する** — Stop が既にレビュー済みの内容と確定して
  いるため、backend が落ちても pending に残すと次の Stop が「差分が空で取得できません
  でした」と誤通知する (混在 batch で新規パス側の backend が全滅しただけで、無関係な
  重複抑止パスまで巻き込む経路があった)
- **state 整理の比較基準は HEAD ではなくレビューした commit (窓の最後の commit)**
  (`gitscan.changed_vs_commit`、0.12.1)。HEAD 基準だと、backend の待ち時間中に
  同じセッションの別 Bash が同じパスを編集・commit したとき (その窓は cursor lock が
  取れず commit レビューを見送る) に「HEAD と差が無い」ので pending から外れ、後続の
  Stop でも拾われなかった。全 3 経路 (重複抑止のみ / 失敗・例外 / 成功) に効く
- claim は取らない。commit の差分は作業ツリーの状態に依存しない不変の範囲なので、
  途中で死んでも「次の Stop に持ち越す」対象が無い

### 受け取った指摘は state 整理の失敗で捨てない

`try` の範囲は**送信前まで**。`_prepare_commit_review` / `_send_commit_review` の
例外は「送らない」に倒すが、backend が指摘を返した後の `_deliver_commit_review` は
try の外で走り、state 整理だけを `_quiet()` が個別に握り潰す。以前は 1 つの `try` が
配信まで覆っており、state 整理の git 呼び出しの例外で**外部へ送りレビュー結果も受け取った後に
指摘ごと捨てる** (cooldown だけ消費する) 形になっていた (マージ前レビューの指摘)。
床テストは `TestDeliveryAfterReview`。

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
- **`git rev-list --not --remotes` による「手元由来の証明」を通った基点まで
  diff を遡って復元する** — 「リモートに存在しない」ことしか示せず「このセッション
  が書いた」ことは示せないため、同一 worktree の別のローカルの書き手が push せずに
  commit した内容まで復元して送ってしまう (詳細は「HEAD 基準が空になったパスは
  復元せず通知する」節)
- **編集直後の内容指紋 (sha256) の一致だけを根拠に復元する** — 内容を変えなかった
  編集でも一致してしまい、このターンの成果でない commit の差分を送る (同節
  「復元を再実装するときの床」)
- **差分レビューに `all` (複数 backend へ同時送信) を用意する** — プランレビューと違い
  Stop のたびに走るので、送信量と課金が backend の数だけ倍になる。回数上限も無い。
  複数の目で見たい場合は `alternate` で「回ごとに別の backend」にする
- **プロジェクト側の設定ファイル (`<repo>/.claude/external-ai-assist/config.json` 等)
  で送信先を指定できるようにする** — clone してきた repo が「この repo では差分を外部
  サービス X にも送る」と宣言できてしまう。設定は env のみにして、利用者自身の
  `~/.claude/settings.json` か **workspace を trust した後の** プロジェクト設定でしか
  効かない形に保つ
- **未知の backend 名を既定の全件へ fallback させる** — タイプミス 1 つで「外したはずの
  backend が黙って走る」= 送信先が増える方向。候補は空のままにして通知する
- **commit レビューを `SubagentStop` で返す / `agent_id` ごとに pending を分ける**
  (0.12.0) — 同期 `PostToolUse(Bash)` の `additionalContext` は公式 Hooks reference で
  「ツール結果の隣に入る」と定められており、サブエージェントの commit をその本人に
  返すにはこれで足りる。**CLI 2.1.278 の nested セッションで実測**: メインの commit
  では `additionalContext` がメインに届き、隔離した作業ツリーで動くサブエージェントの
  commit では**そのサブエージェントにだけ**届いて親には伝播しない。送信本文は該当
  ファイルの差分だけで、Stop の誤通知も出なかった (公式 docs には subagent 宛の明記が
  無いので、CLI の版が変わったら再確認する。`SubagentStart` には "added to the
  subagent's context" の明示があるのと対照的)。それでも SubagentStop を採らないのは、
  `agent_id` 別の pending が状態量とロックの粒度を増やすうえ、「サブエージェントの
  編集は親セッションの成果」という既存の帰属方針 (実機で確認済み) と食い違うため
- **commit レビューを async hook で配信する** (0.12.0) — 公式 docs は async の出力を
  「次の会話ターンに配信」「`decision` は無効」「`-p` では kill されうる」と定めており、
  ツール結果の隣には入らない。commit のたびに待つコストは受け入れて同期にする
- **`git push` のタイミングでまとめてレビューする** (0.12.0) — `origin..HEAD` は
  pull / merge / rebase で入った**他人の commit を含みうる**。窓を Bash 1 回に閉じて
  「窓を開いた時点の作業ツリーと同一だと示せるもの」だけを見るほうが、範囲が構造的に狭い
- **Bash のコマンド文字列 (`tool_input.command`) を解析して `git commit` を見つける**
  (0.12.0) — `sh -c` / alias / スクリプト / `make release` の内側の commit は文字列に
  現れず、`echo "git commit"` のような偽陽性も作れる。**実際に ref が動いたか**を
  見るほうが解析の当たり外れに依存しない

## $TMPDIR のレイアウトと GC

```
$TMPDIR/post-implementation-review/
├── state/<session_id>.json                  pending / in_flight / reviewed / last_backend
├── bashsnap/<session>__<tool_use_id>.json   Bash 実行前の git status スナップショット
├── bashsnap/<session>__<tool_use_id>.reflog.json   Bash 実行前の reflog の窓の起点 (0.12.0)
├── locks/cursor-<cwd hash>.lock             cursor 直列化ロック
└── reviews/<session_id>.txt                 レビュー結果の参照コピー
```

reflog の起点を status と**別ファイル**にしてあるのは、失敗条件が独立しているため:
巨大な作業ツリー (`MAX_SNAPSHOT_ENTRIES` 超過) や `git status` の timeout で status 側が
保存できなくても、commit 検出は成立させたい。

Stop のたびに `stategc.gc_stale()` が mtime 48 時間超のファイルを削除する。
ただし `bashsnap/` だけは **1 時間**の別 TTL を当てる — スナップショットは対応する
PostToolUse が pop するまでしか意味を持たず、Bash が実行されなかった場合
(permission 拒否 / 別 hook の block / 中断) は PostToolUse が来ずに孤児になるため。
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
新しい正規のスイッチは `EXTERNAL_AI_POST_REVIEW=0`。**撤廃済みの死んだ別名**なので
`EXTERNAL_AI_POST_REVIEW` が設定されていればそちらが勝つ — exitplan-review の
`EXTERNAL_AI_REVIEW_MAX` は現役の回数予算で AND で効き、扱いが違う (0.6.0)。

## レビューの頻度と待ち時間 (0.6.0)

Stop は編集のあった全ターンで発火し、その間ブロックする。0.5.0 は利用者向けの出力が
一切無く (stderr は exit 0 の hook では debug log 止まり)、最大 11 分の無言になっていた。

| 環境変数 | 既定 | 効果 |
|---|---|---|
| `EXTERNAL_AI_POST_REVIEW_TIMEOUT` | `300` | cursor の timeout (上限 `cursor.MAX_TIMEOUT_SEC` = 600) |
| `EXTERNAL_AI_POST_REVIEW_MIN_LINES` | `0` | 送る diff の変更行数がこれ未満のターンは見送り |
| `EXTERNAL_AI_POST_REVIEW_COOLDOWN_SEC` | `0` | 前回レビュー**完了**から N 秒未満のターンは見送り |

**見送りは pending を消費しない**。cooldown は `claim_pending()` の前に判定して claim 自体を
行わず (cursor lock も取らない)、min-lines は diff を見ないと行数が分からないので claim 後に
`restore_claim()` で戻す (cursor 失敗時と同じ経路。hash も記録しない)。

**min-lines は「そのターンの gate」であって遅延キューではない**。しきい値未満の編集で
セッションを終えると、そのファイルは追加の編集が来るまでレビューされない。pending の
タイムスタンプは `setdefault(p, now)` で再投入のたびに更新されるため、経過時間で開放弁を
作ろうとすると「変更の古さ」ではなく「最後に積まれてからのターン数」を測ってしまう。
既定 `0` (無効) にしてトレードオフを README に明記した上で、意図的にこの挙動を採っている
(`tests/test_throttle_flow.py::TestMinLines`)。

cooldown の起点 `last_review_at` は状態ファイルに持つ (= **セッション単位**。作業ツリー単位
なのは cursor lock だけ)。`_normalize()` は dict のキーだけをループしていたので、スカラーを
足すときは明示的に引き継ぐこと — 引き継ぎ漏れは「読むたびに 0 に戻って cooldown が永久に
効かない」という静かな壊れ方をする。

完了時は所要時間・ファイル数・結果を `systemMessage` に出す (除外・繰り越し通知と同じ枠に
まとめ、指摘ありのターンは既定で `hookSpecificOutput`、`MODE=block` なら `decision` と同居)。
レビュー本文は入れない (方針は `_common/notify.py`)。

## 出力形式: hook error に見せない (0.8.0)

0.7.0 までは指摘ありのターンで常に `decision: "block"` + `reason` を返しており、
Claude Code のトランスクリプト上で毎回**エラー扱い**として表示されていた。公式 Hooks
reference (`Stop decision control` 節) 逐語:

> `hookSpecificOutput.additionalContext`: Non-error feedback for Claude. The
> conversation continues so Claude can act on it, but unlike `decision: "block"` it
> is shown in the transcript as hook feedback rather than a hook error.
>
> It keeps the conversation going through the same loop protections as
> `decision: "block"`, namely the `stop_hook_active` input and the
> 8-consecutive-continuation cap, but the transcript labels it "Stop hook feedback"
> and no hook error notification is shown.

- 既定を `{"hookSpecificOutput": {"hookEventName": "Stop", "additionalContext": reason}}`
  に変更 (`get_mode()`)。ループ保護 (`stop_hook_active` / 8 連続上限) はハーネス側の
  同一機構なので、Claude が指摘を読んで継続する挙動そのものは変わらない
- **実機確認 (nested `claude -p`, CLI 2.1.251, 2026-08-30)**: `build_reason()` と
  同じ形の現実的な指摘文 (プレースホルダの injection 文言ではない) を
  `additionalContext` で返すと、次の Stop で Claude が指摘を 1 件ずつ評価し、
  「critical でない/指示のスコープ外」と判断した理由を添えて明示的にスキップする
  応答をした。プレースホルダ文言 ("reply with the single word BANANA") を使った
  最初の実験では injected instruction とみなされ拒否されたが、現実的な指摘文では
  再現しなかった — テストするなら実際の `build_reason()` 出力形で行うこと
- **公式 changelog 記載の対応下限は CLI 2.1.163 (2026-06-04)**。これ未満では
  `additionalContext` が Stop で効かない

### 未対応 CLI での自動 fail-closed (同じ 0.8.0 batch、マージ前レビューの指摘)

上記の下限を README に書いて利用者に周知するだけでは、2.1.163 未満の CLI で plugin を
更新した既存ユーザーが opt-in (`EXTERNAL_AI_POST_REVIEW_MODE=block`) の存在を知らない
限り、`additionalContext` が黙って無視されレビュー指摘が届かないまま Stop してしまう。
しかも `_run_review` は指摘を組み立てた時点で既に `state.complete_claim(...)` を
呼んでいるため、この指摘は再試行されず永久に失われる (マージ前レビューの指摘)。

`EXTERNAL_AI_POST_REVIEW_MODE` を 3 値に拡張して対処した:

- `block` / `context` の明示指定は版数判定を飛ばし、そのまま使う (`context` を明示した
  利用者は 2.1.163 未満での既知の問題を承知の上という前提 — 利用者の責任)
- それ以外 (未設定 / `auto` / 未知の値) は `_claude_code_version()` で実行中の
  Claude Code の版数を検出し、2.1.163 以上なら `context`、**未満または検出できなければ
  自動的に `block` に fail-closed する** (指摘を届かないまま失う方向には倒さない)
- 版数検出は 3 段: (a) 環境変数 `CLAUDE_CODE_VERSION`、(b) 環境変数
  `CLAUDE_CODE_EXECPATH` のパス要素のうち版数だけの文字列、(c) `claude --version` の
  subprocess 実行 (timeout 3秒)。**(a)(b) はいずれも hooks 向けの公式契約の外側**
  (`llms-docs:researching-claude-docs` で逐語確認: `CLAUDE_CODE_VERSION` という変数名
  自体は公式 settings reference に存在するが Enterprise `policyHelper` 専用で hook への
  注入は不明記、`CLAUDE_CODE_EXECPATH` は公式コーパス全体でゼロヒットの未文書化の内部
  実装詳細)。このため (c) を最終的な信頼できるフォールバックとして必ず残す
- 版数を理由に auto 解決が `block` に倒れたときだけ `systemMessage` に付記文を足す
  (どの版数だったか、または「不明」)。明示 `MODE=block` では付記文を混ぜない
- Stop の待ち時間の絶対上限が 674 秒 → 677 秒に変化 (`claude --version` の timeout
  3秒が worst case に加わる。690秒の hook timeout には収まっている。
  `tests/test_review_set.py::TestTimeoutBudgets` 参照)
- テストは実機の `claude` を起動しない。既存テストを含む全体が
  `tests/_testutil.py::PINNED_VERSION_ENV` (`CLAUDE_CODE_VERSION` を対応版数に固定) の
  下で走るため、実行環境の実際の Claude Code 版数に依存しない
  (`tests/test_version_detect.py` / `test_throttle_flow.py::TestVersionAwareMode`)

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
| **入れ子の作業ツリー / git リポジトリ / submodule の中のファイルを黙って落とす (誤通知しない)** | `TestNestedWorktree` |
| 上限超過パスを黙って捨てない | `TestOverflowCarryOver` |
| コードフェンス付き REVIEW_CLEAN (+ 前置き) を指摘扱いしない | `TestFencedCleanSentinel` (判定規則の網羅は `hooks/_common/tests/test_sentinel.py`) |
| 機密・非コードファイルの差分を外部に送らない (恒久除外 + 通知) | `TestExclusion` (判定規則の網羅は `tests/test_exclusion.py`) |
| glob に見えるファイル名で他セッションの差分が混入しない | `TestLiteralPathspecFlow` (git 単体は `test_gitscan.py::TestLiteralPathspec`) |
| 予算に収まらないファイルをレビュー済みにしない / 巨大ファイルは切り詰めて hash 記録 | `TestByteBudgetFlow` (単体は `test_review_set.py::TestByteBudget`) |
| HEAD 基準の diff が空のパス (同一ターン内 commit 等) は復元せず黙って消費せず通知する | `TestSameTurnCommitNotification` (別のローカルの書き手の commit を送らない regression は `test_other_local_writer_commit_is_not_leaked`、内容を変えなかった編集で無関係な履歴を送らない床は `test_noop_edit_does_not_send_an_unrelated_historical_diff`) |
| しきい値・cooldown の見送りが pending を消費しない | `test_throttle_flow.py::TestMinLines` / `TestCooldown` |
| レビュー完了を利用者に通知する (本文は混ぜない) | `test_throttle_flow.py::TestCompletionNotice` |
| 指摘ありは既定 (`auto`) で `additionalContext`、`MODE=block` で旧 `decision:block` に戻せる | `test_throttle_flow.py::TestOutputMode` |
| `auto` は版数非対応・不明なら自動で `block` に fail-closed する (マージ前レビューの指摘) | `test_throttle_flow.py::TestVersionAwareMode` |
| 版数検出の 3 段 (env var → EXECPATH → subprocess) と閾値判定 | `test_version_detect.py` |
| **`BACKENDS` 未設定なら codex が使えても差分は cursor にしか行かない** | `test_selection.py::TestDefaultDestination` (2 ターン回す — 1 ターンだけだと「既定を全 backend + alternate に広げる」改変でも偶然素通りする) |
| 戦略 (fixed / available / alternate / random) と既定の決まり方 | `test_selection.py::TestStrategies` |
| フォールバックが列挙集合の外へ出ない / 全滅時に pending を戻す | `test_selection.py::TestFallback` |
| 候補を複数書いても待ち時間の上限が 0.11.0 と同じ | `test_selection.py::TestTotalWaitBudget` |
| どの backend に何 (パス名とバイト数) を送ったかを記録する | `test_selection.py::TestSendLog` |
| **Bash の窓の中の commit がレビューされ、Stop が同じパスを誤通知しない** | `test_commit_flow.py::TestCommitInWindowIsReviewed` |
| **I1': 送信本文の hunk が「窓を開く直前に Stop が集めた diff」と一致する** (plain / amend / 1 窓 2 commit / 新規 untracked)。削除と「前の窓で入った他者内容」は P6 により**送らず通知する**側に厳格化 | `test_commit_flow.py::TestWindowEquivalence` |
| **W2: pull / merge / cherry-pick / revert / reset が混ざった窓では何も送らない** | `test_commit_flow.py::TestNonCommitReflogLines` |
| **W3: 行が連鎖していない (偽装された) 窓では何も送らない** | `test_commit_flow.py::TestChainedWindow` |
| **W4: pre の status スナップショットが無い窓では何も送らず理由を通知する** | `test_commit_flow.py::TestMissingPreStatus` |
| **P1: `reviewed` にしか無いパスは送信許可にならない** | `test_commit_flow.py::TestReviewedIsNotASendPermission` |
| **P2: 窓の中で初めて内容が入ったパス (`merge --squash` / `cherry-pick -n` / `stash pop` / `restore --source`) は送らない** | `test_commit_flow.py::TestContentThatArrivedInsideTheWindow` |
| **P3': 窓の間に作業ツリーが書き換わったパス (`checkout <ref> --` / フック書き換え) は送らない** | `test_commit_flow.py::TestWorktreeRewrittenInsideTheWindow` |
| **P4: 部分 commit (index だけを確定した commit) は送らない** | `test_commit_flow.py::TestPartialCommit` |
| **代理判定を復元・迂回する 3 攻撃 (同一サイズ + mtime 復元 / `assume-unchanged` / `skip-worktree`) で他者の内容が出ない** | `test_commit_flow.py::TestRestoredStatAndIndexFlags` (P6 単独でも green) |
| **P3' / P4' 単体の床 (落とした理由の区分)** | `test_commit_flow.py::TestProxyCheckSkipReasons` |
| **P6: 指紋の更新規律 (最後の書き込みが勝つ / 窓の外の差し替え / Bash 書き換えによる失効 / `git add` では失効しない / サイズ上限 / 旧 state からの移行)** | `test_commit_flow.py::TestFingerprintDiscipline` |
| **Stop がレビュー済みの内容を commit 経路が再送しない / 変わった内容は送る** | `test_commit_flow.py::TestReviewedContentIsNotResent` |
| **conflict 解決後の merge commit を含む窓では何も送らない (`COMMIT_PREFIXES` の床)** | `test_commit_flow.py::TestConflictedMergeCommit` |
| **P1 の集合を pending の更新より前に読む (呼び出し順)** | `test_commit_flow.py::TestRecordedSetIsReadFirst` |
| **backend lock を他が握っている窓は送らず 1 行通知する** | `test_commit_flow.py::TestCommitReviewSerialization` |
| **symlink 別名で pending に入ったパスをレビュー後に外す (直後の Stop が誤通知しない)** | `test_commit_flow.py::TestSymlinkAliasIsSettled` |
| **配信の整形で例外が出ても無言で終わらない** | `test_commit_flow.py::TestDeliveryFormattingFailure` |
| **`git ls-files -v` のタグ判定 / commit された blob の sha256 (上限・欠落・ズレ検出)** | `test_gitscan.py::TestFlaggedIndexEntries` / `TestBlobDigests` |
| **指紋の寿命 (claim / complete / restore / drop / 上限 / 旧 state)** | `test_state.py::TestFingerprints` |
| **窓の外の別の書き手の commit / `git commit -a` が巻き込んだ記録の無いパス / 除外パス / 内容を変えない編集 + 無関係な履歴を送らない** | `test_commit_flow.py::TestSendScope` |
| **通知したパスを pending に積まない (次の窓の送信許可にしない)** | `test_commit_flow.py::TestUnrecordedPathsDoNotBecomePermission` |
| **conflict 解決後の `revert --continue` (同一窓 / 窓をまたぐ形の両方)** | `test_commit_flow.py::TestRevertContinue` |
| **amend は増分だけ (他人の commit を amend しても元の内容は送らない)** | `test_commit_flow.py::TestAmendScope` |
| **fail-closed の各条件で送信ゼロ** | `test_commit_flow.py::TestFailClosed` |
| **commit 経路の予算・しきい値・取得失敗 (合計バイト / 1 ファイル切り詰め / MIN_LINES / cooldown / range_paths 失敗 / P4 判定失敗 / 追記バイト上限)** | `test_commit_flow.py::TestCommitBudgets` |
| **受け取った指摘を state 整理の失敗で捨てない / 送信前の例外では送らない** | `test_commit_flow.py::TestDeliveryAfterReview` |
| **linked worktree の commit をその worktree の reflog で拾う** | `test_commit_flow.py::TestLinkedWorktree` |
| **全 backend 失敗時、送った側の pending は触らないが、重複抑止パスは settle する (混在 batch) / 別 repo への commit では窓が伸びない** | `test_commit_flow.py::TestStateOnFailure` / `TestCommitInAnotherRepo` |
| **state 整理はレビューした commit 基準 (待ち時間中に同じパスが commit されたら外さない / 無関係な commit では巻き込まない)** | `test_commit_flow.py::TestSettleAgainstReviewedCommit` |
| commit レビューを含む PostToolUse(Bash) の hook timeout 予算 | `test_review_set.py::TestTimeoutBudgets::test_post_tool_bash_budget_covers_commit_review` |
| env 未設定なら 0.5.0 と同じ挙動 | 各クラスの `test_unset_*` (基底クラスが `EXTERNAL_AI_` を接頭辞で一掃する) |

**テストから実機の外部 AI CLI を起動しない**ための前提が 1 つある: `sys.modules` から
hook のモジュールを外す処理 (`tests/test_posix_guard.py::_purge_hook_modules`) は
**名前で列挙せずパッケージディレクトリ由来のものを機械的に全部**落とす。列挙が漏れると、
外し損ねた古いモジュールが古い `cursor` への参照を抱えたまま残り、後続テストが
`sys.modules["cursor"]` に当てた patch を素通りして**本物の cursor CLI が起動する**
(0.12.0 で `selection` を足したときに実際に踏んだ — 全 suite が hang して発覚)。

`TestBashAttribution.test_sed_on_already_dirty_file` は**すでに dirty なファイルを
同一バイト数で書き換える**という最も厳しい条件を使っている。clean なファイルから始めると
行集合比較の素朴な実装でもテストが通ってしまい、バグが素通りする。

hook は合成 stdin で直接起動できるので `/plugin` 更新なしで手動確認もできるが、
**必ず `TMPDIR` を一時ディレクトリに差し替えること** (本番の状態ファイルを消費してしまう)。

## 発火しないときの確認手順

1. `which cursor-agent; which cursor` — この順に検出する (0.11.0。**汎用名 `agent` は
   候補ではない** — 無関係な実体に差分を渡さないため。そこに本物を置いている環境は
   `EXTERNAL_AI_CURSOR_COMMAND=agent`)。どれも無ければ no-op 終了が期待動作。検出結果は
   `$TMPDIR/external-ai-assist/cursorcli.json` に TTL 付きでキャッシュされる
   (詳細は `hooks/_common/cursorcli.py`)
2. `env | grep EXTERNAL_AI_POST_REVIEW` — `0` で無効化されていないか。
   `_BACKENDS` に未知の名前だけを書いていないか (その場合は毎ターン
   `systemMessage` で通知が出る)
3. `cat $TMPDIR/post-implementation-review/state/<session_id>.json` —
   `pending` が空なら「このセッションはこのターンで何も編集していない」が正しい判定
4. `in_flight` にエントリが残り続けている → 前回の Stop が kill された。
   TTL (`cursor.MAX_TIMEOUT_SEC + 300` = 900 秒) 経過後の Stop で回収される
5. stderr の `[post-implementation-review]` プレフィクス付きログを確認
6. 他セッションが `cursor agent` を走らせている間は skip する
   (「同一作業ツリーで別セッションがレビュー中」ログ)

commit レビュー (0.12.0) が動かないときは追加で:

7. `EXTERNAL_AI_POST_REVIEW_COMMIT` が `0` でないか。`..._BASH_TRACKING=0` でも止まる
   (窓の起点を保存しなくなるため)
8. stderr に `commit レビューを見送り (fail-closed): <理由>` が出ていないか
   (理由の一覧は `reflog.py` の docstring)
9. その commit は **Bash 経由**か。IDE・別ターミナルの commit は窓に入らない
10. その Bash の中で **commit 以外の ref 操作**をしていないか (`git reflog --date=iso |
    head` で確認できる)。`reset` / `merge` / `rebase` / `pull` / `cherry-pick` /
    `revert` / `switch`、および conflict 解決後の `commit (merge):` /
    `commit (cherry-pick):` が 1 行でもあれば**窓ごと**対象外 (通知が 1 行出る)
11. commit に入ったパスが `pending` / `in_flight` にあるか。`reviewed` にしか無い
    パスは対象外 (「編集記録が無いため内容を送信していません」通知が正しい判定)
12. そのパスは **Bash を実行する前から未 commit の変更があった**か。同じ Bash の中で
    編集して commit した / 窓の間に書き換わった / 部分 commit だったパスは、
    「窓を開いた時点の変更と一致しない」「部分 commit」通知になる (設計どおり)
