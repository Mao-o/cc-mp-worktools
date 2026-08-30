# post-implementation-review

`PreToolUse(Bash)` / `PostToolUse(Write|Edit|NotebookEdit|Bash)` / `Stop` の 3 フックで動作し、
**そのターンにこのセッションが変更したファイルだけ**を Cursor に差分レビューさせ、
critical な指摘があれば `hookSpecificOutput.additionalContext` (既定 `auto`。0.8.0 から) で
Claude に返す。`auto` は実行中の Claude Code の版数を自動検出し、2.1.163 未満・不明
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
├── gitscan.py          パス正規化 + git status スナップショット + パス単位 diff
├── exclusion.py        外部に送らないファイルの判定 (既定 glob / 追加 glob / CODE_ONLY)
├── cursor.py           cursor agent 呼び出し (既定 300s / 上限 600s。起動は hooks/_common/subproc.py)
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
| 作業ツリー外 / ディレクトリ | 戻さない | 記録しない | なし |
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
claim に現れない (Codex R2 P1)。symlink の列挙は tracked が index (mode 120000)、untracked が
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

## ロックは 2 種類。ネストしたまま cursor を回さない

| ロック | 対象 | 保持時間 |
|---|---|---|
| state lock | 状態ファイルの read-modify-write | 短時間 (ms) |
| cursor lock | cwd をキーに `cursor agent` を直列化 | `review()` 実行中ずっと (既定 300s / 上限 600s) |

Stop の取得順は **cursor lock → state lock → (state 解放) → review**。
state lock を握ったまま review すると、全セッションの PostToolUse が cursor の timeout 上限
(600 秒) までブロックされる。
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

## $TMPDIR のレイアウトと GC

```
$TMPDIR/post-implementation-review/
├── state/<session_id>.json                  pending / in_flight / reviewed
├── bashsnap/<session>__<tool_use_id>.json   Bash 実行前のスナップショット
├── locks/cursor-<cwd hash>.lock             cursor 直列化ロック
└── reviews/<session_id>.txt                 レビュー結果の参照コピー
```

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

### 未対応 CLI での自動 fail-closed (同じ 0.8.0 batch、Codex R1 P1 対応)

上記の下限を README に書いて利用者に周知するだけでは、2.1.163 未満の CLI で plugin を
更新した既存ユーザーが opt-in (`EXTERNAL_AI_POST_REVIEW_MODE=block`) の存在を知らない
限り、`additionalContext` が黙って無視されレビュー指摘が届かないまま Stop してしまう。
しかも `_run_review` は指摘を組み立てた時点で既に `state.complete_claim(...)` を
呼んでいるため、この指摘は再試行されず永久に失われる (Codex PR #69 R1 レビュー指摘)。

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
| 上限超過パスを黙って捨てない | `TestOverflowCarryOver` |
| コードフェンス付き REVIEW_CLEAN (+ 前置き) を指摘扱いしない | `TestFencedCleanSentinel` (判定規則の網羅は `hooks/_common/tests/test_sentinel.py`) |
| 機密・非コードファイルの差分を外部に送らない (恒久除外 + 通知) | `TestExclusion` (判定規則の網羅は `tests/test_exclusion.py`) |
| glob に見えるファイル名で他セッションの差分が混入しない | `TestLiteralPathspecFlow` (git 単体は `test_gitscan.py::TestLiteralPathspec`) |
| 予算に収まらないファイルをレビュー済みにしない / 巨大ファイルは切り詰めて hash 記録 | `TestByteBudgetFlow` (単体は `test_review_set.py::TestByteBudget`) |
| HEAD 基準の diff が空のパス (同一ターン内 commit 等) は復元せず黙って消費せず通知する | `TestSameTurnCommitNotification` (別のローカルの書き手の commit を送らない regression は `test_other_local_writer_commit_is_not_leaked`) |
| しきい値・cooldown の見送りが pending を消費しない | `test_throttle_flow.py::TestMinLines` / `TestCooldown` |
| レビュー完了を利用者に通知する (本文は混ぜない) | `test_throttle_flow.py::TestCompletionNotice` |
| 指摘ありは既定 (`auto`) で `additionalContext`、`MODE=block` で旧 `decision:block` に戻せる | `test_throttle_flow.py::TestOutputMode` |
| `auto` は版数非対応・不明なら自動で `block` に fail-closed する (Codex R1 P1) | `test_throttle_flow.py::TestVersionAwareMode` |
| 版数検出の 3 段 (env var → EXECPATH → subprocess) と閾値判定 | `test_version_detect.py` |
| env 未設定なら 0.5.0 と同じ挙動 | 各クラスの `test_unset_*` (基底クラスが `EXTERNAL_AI_` を接頭辞で一掃する) |

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
   TTL (`cursor.MAX_TIMEOUT_SEC + 300` = 900 秒) 経過後の Stop で回収される
5. stderr の `[post-implementation-review]` プレフィクス付きログを確認
6. 他セッションが `cursor agent` を走らせている間は skip する
   (「同一作業ツリーで別セッションがレビュー中」ログ)
