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

## post は `async` hook (0.10.0)

Agent ツールは **subagent が背景に移った時点で戻る**。公式 docs (`PreToolUse input` の
Agent 表) 逐語:

> `status` ... `"completed"` for foreground subagents, `"async_launched"` for background
> subagents. As of v2.1.198, subagents run in the background by default, so an omitted
> `run_in_background` also produces `"async_launched"`
>
> For background subagents, the tool returns when the task moves to the background

つまり PostToolUse(Agent) は Explore の**完了時ではなく起動直後**に発火する。0.9.1 までの
post は同期 hook のまま最大 `TIMEOUT_SEC` (60) 秒ポーリングしていたので、「並走して待ち
時間を隠す」設計と裏腹に、Explore が走り出した直後に親を 60 秒止めていた。

hooks.json で post を `"async": true` にして解消する (docs `Run hooks in the background`):

> set `"async": true` to run the hook in the background while Claude continues working.
>
> After the background process exits, Claude Code delivers the `additionalContext` and
> `systemMessage` fields from the hook's JSON response to Claude on the next conversation
> turn. Unlike a synchronous hook's `systemMessage`, neither field is shown to you.

**発火条件 (イベント / matcher) は変えていない**。変わるのは「親をブロックするか」と
「結果が届くのが次ターンになるか」の 2 点だけ。

- `tool_response.status` を読んで待機を出し分ける案は採らなかった。async 化すると
  foreground / background のどちらでも親は止まらないので、分岐しても挙動が変わらない
- **`timeout` は async 化後は効かない** (docs: "Once an async hook is running in the
  background, Claude Code doesn't enforce `timeout` on it")。hooks.json の `90` は
  async 化後は強制されない (docs 逐語)。意図の記録として残してある。
  待機の実上限は `cursor.TIMEOUT_SEC` 側
- **セッションが idle だと配信は次のユーザー操作まで待つ** (docs: "Hook output is
  delivered on the next conversation turn. If the session is idle, the response waits
  until the next user interaction")。この hook は Explore の起動直後に走るので、通常は
  親がまだ Explore 結果を処理している最中に届く。docs には code 2 で即時起床する
  `asyncRewake` もあるが、そちらは `timeout` が強制されるうえ「起こす」ほどの緊急性が
  無いので採らない
- **`claude -p` (headless) では teardown で kill される** (docs: outcome `cancelled`)。
  その場合 post の後始末に到達しないので、残骸は次回起動時の TTL GC が拾う。README の
  「headless では `EXTERNAL_AI_*=0` を推奨」はこの意味でも有効
- **背景 subagent の完了時に PostToolUse(Agent) が再発火するかは公式 docs に記述が無い**
  (肯定も否定もされていない)。再発火しない前提なので、cursor の解析予算は起動から
  `TIMEOUT_SEC` 秒で、Explore の実行時間には連動しない

## 無効化 (`EXTERNAL_AI_EXPLORE_PARALLEL=0`, 0.6.0)

0.5.0 まではスイッチが皆無で、`cursor` を PATH から外す以外に止める手段が無かった。
`EXTERNAL_AI_EXPLORE_PARALLEL=0` で並走を止められる (他 2 hook とは独立。
`EXTERNAL_AI_REVIEW_MAX=0` は exitplan-review にしか効かない)。

**止めるのは pre だけで、post は常に回す**。post も止めると、直前のターンで起動済みの
cursor と pid / 結果ファイルが孤児になる — 無効化した瞬間だけ発生するので気付きにくい。
何も起動していなければ post は元から no-op (アナライザ未インストール時と同じ経路) なので、
ゲートを外しておくコストは無い (`tests/test_disable_switch.py`)。

## ディレクトリ構成

```
explore-parallel/
├── CLAUDE.md           このドキュメント
├── __main__.py         エントリポイント。--phase pre|post でフェーズ振り分け、ANALYZERS を順に回す + 残骸 GC
├── state.py            tool_use_id ベースの一時ファイルパス管理 + TTL 判定 ($TMPDIR/explore-parallel/)
├── cursor.py           cursor agent の pre(読み取り専用で起動) / post(待機+結果取得) / 停止
└── tests/              起動引数 (--mode plan) と結果注入の unittest (偽 cursor)
```

**実行フロー**:

- **pre フェーズ**: `__main__.py` が stdin から hook input を読み取り、`subagent_type == "Explore"` をチェック。`ANALYZERS` に登録されたアナライザのうち `is_available()` が True のものを順に `pre(tool_use_id, prompt)` で起動する。`cursor.pre()` は cursor agent を**読み取り専用** (`--mode plan`。argv は review 系 2 hook と共通の `_common/cursorcli.readonly_argv`) でバックグラウンド起動し、PID と結果ファイルを `$TMPDIR/explore-parallel/` (TMPDIR 未設定なら `/tmp`) に記録
- **post フェーズ**: `__main__.py` が同じく hook input を受け取り、各アナライザの `post(tool_use_id)` を呼ぶ。`cursor.post()` は PID を見て最大 `TIMEOUT_SEC` 秒待機、結果ファイルを読み取って整形済み文字列を返す。`__main__.py` は複数アナライザの結果を `\n\n` で結合し、1 つの `additionalContext` JSON にまとめて stdout に出力

Python 3.11+ 想定。標準ライブラリのみ使用 (外部依存なし)。ログと cursor の存在確認は
`hooks/_common/` (`hooklog` / `cursorcli`) を使う。`_common` は `__main__.py` が `hooks/` を
sys.path に載せて解決する (plugin root 内の相対配置なので cache コピーでも壊れない)。

## アナライザの追加

新しいアナライザ（例: Gemini）を追加する手順:

1. `gemini.py` を作成し、以下 4 つを公開する:

   | 名前 | 型 | 説明 |
   |---|---|---|
   | `NAME` | `str` | 識別子（英数字。state ファイル名に使用） |
   | `is_available()` | `() -> bool` | CLI 存在確認等の事前チェック |
   | `pre(tool_use_id, prompt)` | `(str, str) -> None` | バックグラウンド起動 |
   | `post(tool_use_id)` | `(str) -> str \| None` | 待機 + 結果取得。整形済み文字列 or None。**停止を確認できたときだけ** pid / 結果ファイルを掃除する |
   | `reap_orphan(pid_file)` | `(Path) -> str` | TTL 超過の残骸を停止し、停止の確度 (`state.REAP_*`) を返す (GC から呼ばれる) |

   **掃除の契約は post と GC で同じ**。停止を確認できた (`REAP_STOPPED` /
   `REAP_SIGNALED` 相当) ときだけ pid / 結果ファイルを対で消し、確認できないときは
   対で残す:

   | 状況 | post | `reap_orphan` の戻り値 | pid / 結果ファイル |
   |---|---|---|---|
   | 走っていない / group にも生存メンバーが居ない | 掃除する | `REAP_STOPPED` | 消す |
   | 停止 signal を送出できた (group が空だった場合を含む) | 掃除する | `REAP_SIGNALED` | 消す |
   | リーダーは死んだが group に生存メンバーが残っている | group を停止 → 掃除する | `REAP_SIGNALED` | 消す |
   | まだ走っているが同一性を確認できない | **残す** | `REAP_UNCONFIRMED` | 残す |
   | リーダー亡き group の同一性を確認できない | **残す** | `REAP_UNCONFIRMED` | 残す |
   | 同一性は確認できたが signal を送出できない (EPERM 等) | **残す** | `REAP_UNCONFIRMED` | 残す |
   | `reap_orphan()` 自体が例外で落ちた | — | (`gc_orphans` が未確定扱い) | 残す |

   **リーダーの生死判定 (`_is_running`) は zombie を「走っていない」側に数える**ので、
   zombie のリーダーは上の表の下 2 行 (leaderless group) の経路に入る。group にも zombie
   しか残っていなければ 1 行目 = 掃除してよい。

   pid ファイルは**その孤児を追える唯一の記録**なので、確認できていない状態で消すと
   以後どの経路も再試行できない。結果ファイルを道連れにしないのは、孤児がまだ書いて
   いる最中でありうるうえ、pid だけ残しても対になる出力が失われるため。

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

### 残骸の TTL GC (0.10.0)

`post()` の後始末に到達しない経路がある — Agent ツールの失敗、ユーザー中断、セッション
終了、`async` hook が `claude -p` の teardown で kill される場合。0.9.1 まではこれらで
pid / 結果ファイルが無期限に残り、バックグラウンドの cursor も自然完了まで走り続けて
いた (課金)。

- `state.stale_entries()` が `ORPHAN_TTL_SEC` (900 秒) を超えた残骸を拾い、
  `__main__.gc_orphans()` が **pre / post の両方**で掃除する
- 経過時間は **pid ファイルの mtime (= 起動時刻)** で測る。結果ファイルの mtime は
  analyzer が書くたびに更新されるので、それを基準にすると「走り続けている孤児ほど
  新しく見えて残る」逆転が起きる
- **現在の tool_use_id は除外**する (TTL があるので通常は掛からないが、今起動した
  ばかりのプロセスを GC が撃つ経路を構造的に潰す)
- `GC_BUDGET_SEC` (2.0 秒) で打ち切る。pre の hook timeout は 5 秒しかないため、
  残骸が大量にあっても起動を遅らせない。取りこぼしは次回の GC が拾う
- **停止を確認できないときは pid ファイルを残す** (マージ前レビューの指摘)。
  `reap_orphan()` は停止の確度を `state.REAP_*` で返し、`gc_orphans()` は
  `REAP_STOPPED` (走っていない / pid 記録が壊れている) と `REAP_SIGNALED`
  (停止 signal の送出を実際に試みた) のときだけ掃除する。`REAP_UNCONFIRMED`
  (まだ走っているのに `ps` が一時的に使えない・cmdline が切り詰められた等で同一性を
  確認できず signal を送っていない) と `reap_orphan()` 自体が例外で落ちた場合は、
  pid / 結果ファイルを残して次回の GC に委ねる。pid ファイルは**その孤児を追える唯一の
  記録**なので、確認できていない状態で消すと以後どの GC も再試行できず、ハングした
  cursor が走り続けて課金され続ける。結果ファイルも一緒に残す — 孤児がまだ書いている
  最中でありうるうえ、pid だけ残しても対になる出力が失われる。
  analyzer が登録から外れた名前 / pid ファイルの無い結果だけの残骸は、そもそも止める
  対象を追えない (残しても次回できることが増えない) ので従来どおり掃除する
- **`post()` の掃除も同じ契約に揃えてある** (マージ前レビューの指摘)。timeout 時に
  analyzer がまだ走っていて `terminate()` が False を返す経路 (同一性を確認できない /
  signal を送出できない) でも、以前は無条件に pid ファイルを消し、続けて結果ファイルも
  消していた。GC が拾える唯一の記録を post が消してしまうと、走り続ける cursor を
  もう誰も追えない。**未確定なら post も両ファイルを残す** (上の契約表)。結果の読み取り
  自体は best-effort で続ける — 掃除しないだけで、その後にファイルを見るのは中身を
  読まない GC だけなので二重注入にはならない
- **`post()` は掃除の前に group 側の判定も通す** (マージ前レビューの指摘)。post は
  リーダーの生死しか見ていなかったため、リーダーが post の完了前に exit / crash して
  group に孫が残った場合、停止を試みないまま掃除に進んでいた。**その group を追える
  唯一の記録 (pid = pgid) がそこで消える**ので、下の leaderless-group GC も以後その孫に
  手が届かない。`reap_orphan` と同じ `_reap_leaderless_group` を通し、生存メンバーが
  居れば停止を試み、未確定なら両ファイルを残す。pid が正でないときは group 判定に入らない
  (`killpg(0, sig)` は**呼び出し側自身の process group** = hook プロセスを撃つため)

`PostToolUseFailure(Agent)` を hooks.json に足して即時掃除する案は**採っていない**。
イベント自体は実在するが、新しいイベントの登録は「どの hook がどの条件で発火するか」
の変更にあたる。TTL GC が同じ失敗モードを発火条件を変えずに覆うので、まずこちらで足りる。

### 停止は process group ごと + PID 同一性の確認 (0.10.0)

`pre` は `start_new_session=True` で起動する (pgid == pid) のに、0.9.1 までの停止は
`os.kill(pid, SIGTERM)` で**グループリーダーだけ**だった。cursor-agent (node) が生成した
孫プロセスが取り残されて走り続ける。`cursor.terminate()` で `os.killpg` に統一し、
SIGTERM → 猶予 (`KILL_GRACE_SEC`) → SIGKILL のエスカレーションを入れた
(review 系 2 hook の `_common/subproc.kill_process_group` と同じ考え方)。

signal を送る前に **cmdline の署名とプロセスの開始時刻の 2 段**で pid の同一性を確認する。
pid ファイルは TTL 超過まで残りうるので、その間に pid が別プロセスへ再利用されている
ことがあるため。

1. `ps -ww -o command=` で cmdline を取り、`cursorcli.readonly_argv` から導出した署名
   (`agent --trust --print --mode plan`) と突合する
2. `ps -o etime=` で開始時刻を取り、**pid ファイルの mtime (= 起動時刻) 以前**であることを
   要求する (`_START_SKEW_SEC` = 5 秒の余裕付き)

- **署名だけでは足りない** (マージ前レビューの指摘)。同じ `readonly_argv` で cursor を
  起動する hook が本 plugin 内に他にもある (exitplan-review / post-implementation-review)。
  TTL 超過まで残った pid ファイルの pid がそれらに再利用されていると、署名照合を素通りして
  無関係なレビューを `killpg` で撃つ。`pre` は Popen 直後に pid ファイルを書くので、自分の
  analyzer なら開始時刻は必ず mtime 以前になる。再利用された pid は mtime より後に起動して
  いるので弾ける
- **`etime` を使う理由**。`etimes` (秒の直値) は procps 拡張で macOS の `ps` には無い
  (`ps: etimes: keyword not found`)。`lstart` は表記が locale 依存。POSIX の `etime`
  (`[[DD-]HH:]MM:SS`) を `_common/subproc.parse_etime` で秒に直す。秒未満は ps が切り捨てる
  ので開始時刻が最大 1 秒ぶん後ろにずれて見える — `_START_SKEW_SEC` はこのずれと
  ファイルシステムの時刻粒度のぶんの余裕
- **argv[0] (実行ファイル名) は照合しない**。`cursor` は実体へ `exec` するシムのことが
  あり、その場合 ps が返すのは実体側の名前になる。引数は `exec "$REAL" "$@"` で保たれる
  ので、名前ではなくフラグの組み合わせで見る。名前まで要求すると「シム環境では一切
  kill できない」= ガードではなく停止処理の無効化になる
- **判定できないときは送らない側に倒す** (`ps` が使えない・出力が解析できない・mtime が
  取れない、のいずれも)。無関係なプロセスに SIGTERM を送る事故のほうが、cursor を 1 つ
  取り残すより重い

#### リーダーが先に死んだ group (マージ前レビューの指摘)

`pre` が作った独立 process group には、cursor 本体 (グループリーダー) が exit / crash
した後も孫が残って走り続けることがある。リーダーの生死だけを見て「停止済み」と報告すると、
GC が pid 記録を消した時点で group を撃つ機会が永久に失われる。`reap_orphan()` は
リーダーが居なければ `_reap_leaderless_group()` で group 側を見る。**`post()` も掃除の
前に同じ経路を通る** — post だけリーダーの生死で判断していると、GC が拾う前に post が
記録を消してしまい、leaderless-group の判定そのものに到達できない。

リーダーの cmdline はもう読めないので、同一性は次の 2 つで確認する:

1. group に**生きた (非 zombie) メンバーが居る**こと。pgid の番号はメンバー (zombie
   含む) が残っている限り新しい pid として再割当てされない (`_common/subproc` の kill
   経路と同じ前提) ので、生存メンバーが居る group は起動時に作った group とみなせる
2. 生きたメンバーの**開始時刻がいずれも pid ファイルの mtime (= 起動時刻) より前でない**
   こと。メンバーは analyzer の子孫なので、起動時刻以降に生まれているはず。
   `ps -A -o pid=,pgid=,stat=,etime=` を 1 回呼んでメンバー単位で見る

判定不能 (`ps` が使えない・開始時刻を読めるメンバーが居ない・mtime が取れない) はすべて
`REAP_UNCONFIRMED` = 送らない側。**限界**: group が一度完全に空になってから pgid の番号が
再利用され、その新しいリーダーも既に死んでいる、という二重の偶然までは弾けない
(リーダーが生きている経路と違って cmdline を照合できないため)。2. で「記録より前から
居る group」は落とせる。

#### zombie のリーダーは「走っていない」側 (マージ前レビューの指摘)

リーダーの生死を `os.kill(pid, 0)` だけで見ると、**PID 1 が孤児を reap しないコンテナ**で
停止経路が永久に収束しない。処理を終えたリーダーは zombie として group に残り
`os.kill(pid, 0)` が成功し続けるが、zombie の `ps -o command=` は `<defunct>` しか返さない
ので署名を照合できず、`terminate()` は毎回「同一性を確認できない」= `REAP_UNCONFIRMED` に
倒れる。結果として pid / 結果ファイルが永遠に残り (孤児は既に居ないのに GC が収束しない)、
group に孫が残っていてもそれが撃たれない。

`_is_running()` は `os.kill(pid, 0)` に `_common/subproc.pid_is_zombie()` を重ね、
**zombie は停止済み**として group 側の判定 (`_reap_leaderless_group`) に進める。止めるべき
孫が残っていればそこで撃てるし、group にも zombie しか居なければ `group_is_stopped()` が
停止扱いにするので掃除できる。判定不能 (`/proc` も `ps` も使えない) は従来どおり
`os.kill` の結果に従う = 走行中側 (同一性を確認したうえで停止を試みる側)。
テストヘルパー (`tests/test_orphan_gc.py` の `_alive`) と同じ契約。

#### signal を送出できたかを返す (マージ前レビューの指摘)

`os.killpg` が `PermissionError` / その他の `OSError` を投げた場合、停止 signal は届いて
いない。0.10.0 の途中まではこれを握りつぶしたうえで `terminate()` が無条件に True を
返しており、**TERM も KILL も送れていないのに `REAP_SIGNALED` として pid 記録が消えて
いた**。`_signal_group()` が送出可否を返し、`_stop_group()` は TERM / KILL の**どちらも
送出できなければ False** を返す。`ProcessLookupError` (group にメンバーが居ない) は
「止めるものが無い」= 目的達成なので True 側。TERM が通って KILL だけ失敗した場合も
True (停止 signal は届いており、group が残っていれば次回の GC が同じ手順で再試行できる)

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
    "timeout": 90,
    "async": true
  }]
}]
```

`${CLAUDE_PLUGIN_ROOT}` は Claude Code が plugin ロード時に展開する絶対パス。
インストール後は `~/.claude/plugins/cache/<plugin>/` 配下に展開される。

- **timeout は秒単位**（Claude Code の仕様。ミリ秒ではない）
- **pre の timeout**: バックグラウンド起動で即 return するので短くて OK（5 秒）。
  pre は `async` にしない — Agent ツールが走り出す前にアナライザを起動する必要があり、
  async にすると「並走」の起点が Agent ツールの実行と競争になる。Claude に返す出力も
  持たないので async にする利点が無い（`tests/test_hook_registration.py`）
- **post の timeout**: 90 秒（= cursor の TIMEOUT_SEC=60 + 余裕 30）。ただし `async: true`
  なので実際には強制されない（上記「post は `async` hook」節）。意図の記録として残す

## テスト

```bash
cd hooks/explore-parallel
python3 -m unittest discover tests     # 偽 cursor (PATH 先頭の bash script) で argv と注入を固定
```

`tests/test_cursor_launch.py` は TMPDIR を隔離し、pre が
`cursor agent --trust --print --mode plan <prompt>` で起動すること (書込可能な `-p` 単独に
戻らないこと) と、post が結果を `additionalContext` に注入して pid / 結果ファイルを掃除する
ことを検証する。cursor 本体は起動しない。

`tests/test_early_returns.py` は `tool_use_id` / `prompt` 空の no-op、
アナライザ `is_available()=False` かつ何も起動していない場合の真の no-op、
および pre 成功後に `is_available()` が False になっても起動済み analyzer
(pid ファイル) を post が reap すること (`is_available()` は pre の起動可否
ゲートに限定され、post の後始末とは無関係) を固定する。`tests/test_result_handling.py`
は timeout → SIGTERM・8000 バイト超の出力切詰・pid ファイル欠落時の fallback を
境界ケースとして固定する。いずれも `cursor` 本体は起動しない。

`tests/test_orphan_gc.py` は 0.10.0 で入れた停止・GC の契約を固定する: 停止が
process group ごとであること (孫を取り残さない)、analyzer と一致しない pid には
signal を送らないこと (PID 再利用ガード)、**署名が一致しても pid ファイルより後に起動した
プロセスには送らないこと** (`TestStartTimeGuard`)、**停止を確認できなかった孤児は pid /
結果ファイルを残して次回の GC が再試行すること**、TTL 判定が pid ファイルの mtime を
見ること、現在の tool_use_id を除外すること、GC が pre / post の両方で走ること。
加えて **`post()` も未確定なら両ファイルを残すこと** (`TestPostRetention`)、
**リーダーが先に死んだ group を停止すること / 記録より前から居る group には送らないこと**
(`TestLeaderlessGroup`)、**`post()` も掃除の前に group 側を見ること**
(`TestPostLeaderlessGroup`)、**zombie のリーダーを走行中と読まないこと**
(`TestZombieLeader`)、**signal を送出できなければ `terminate()` が False を返すこと**
(`TestSignalDelivery`)。

停止側のテストは「送るべき形」と「送ってはいけない形」を**同じフィクスチャの差分**で
組んである (mtime だけ違う / `killpg` だけ EPERM にする)。片方だけが落ちることで、
ガードの効果が署名照合や group 判定の副作用ではないと分かる。

**生死判定のヘルパー (`_alive`) は zombie を死んだ扱いにする** (`_common/subproc.pid_is_zombie`
を使う。`TestAliveHelper` が zombie を人工的に作って固定している)。`os.kill(pid, 0)` だけだと、
PID 1 が孤児を reap しないコンテナで kill 済みの孫が zombie として残った場合に成功し続け、
停止が正しく効いているのに group 停止系のテストが待ち時間ののちに落ちる。
**production 側 (`cursor._is_running`) も同じ契約**に揃えてある (上の「zombie のリーダー」節)
— ヘルパーだけ直しても、実装が zombie を走行中と読んでいる限り GC は収束しない。
zombie のリーダーを人工的に作るフィクスチャ (`spawn_zombie_leader_group`) は `Popen` を
保持したまま `poll()` / `wait()` を呼ばない。参照を捨てると `Popen.__del__` が
`subprocess._active` へ積み、次の `Popen` 生成時の `_cleanup()` が reap してしまう。

**「走っている孤児を止める」テストは pid ファイルの mtime を過去へずらして作らない**。
本番の孤児は mtime と同時刻に起動して TTL を超えて生き残ったプロセスなので、mtime だけを
ずらすと「mtime より後に起動した = 再利用された pid」の形になり、開始時刻の照合から見て
別プロセスと区別が付かない (自分で作った再利用の状況を「停止できること」の根拠にしてしまう)。
`state.ORPHAN_TTL_SEC` を縮めて待てば時系列は本番と同じまま短縮できる (`stale_entries` は
TTL を既定引数に束縛せず呼び出しのたびに読む)。
`tests/test_hook_registration.py` は hooks.json 側の登録形 (post は `async`、pre は同期、
発火条件は据置) を固定する。

**偽 cursor は `exec` しない** (`sleep 30 &` + `wait` で argv を保つ)。`exec sleep 30`
だとプロセスイメージごと差し替わって argv が失われ、PID 再利用ガードから見て
「無関係なプロセス」と区別が付かなくなる。実物の cursor はシムでも `exec "$REAL" "$@"`
で引数を保つので、argv を保ったまま待つ形が忠実な模倣。

手動で確認するときは標準入力に hook input JSON を流し込む (**必ず `TMPDIR` を一時ディレクトリに
差し替えること** — 本番の結果ファイルと混ざる):

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
- **読み取り専用起動 (0.4.1)** — 0.4.0 までは `cursor agent --trust -p` で起動しており、cursor-agent の help では `-p` 単独は「Has access to all tools, including write and shell」。調査の裏で作業ツリーを書き換えうる agent が走っていたため `--mode plan` (read-only/planning) を付け、argv を review 系 2 hook と `_common/cursorcli.readonly_argv` で共有する。`--sandbox enabled` は cursor-agent 側の既定値と `--mode plan` との組み合わせを確認できていないため付けていない (必要なら別 issue)
