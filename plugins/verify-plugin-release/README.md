# verify-plugin-release

Claude Code の plugin marketplace repo で、`gh pr create` / `gh pr ready` を実行する前に
**PR 前の完了条件**を検査する PreToolUse hook です。条件を満たしていなければ PR の作成を
止め、何が足りないかを Claude に返します。

「version を bump し忘れて既存ユーザーに更新が届かない」「CHANGELOG を書き忘れた」
「テストが落ちたまま PR を出した」といった、レビューで毎回指摘される類の漏れを
PR の手前で機械的に落とすのが目的です。

## 目的と限界

**目的はリリース漏れの「うっかり」を防ぐことで、意図的なすり抜けを防ぐ仕組みではありません。**
Claude が普段の書き方で `gh pr create` / `gh pr ready` を実行したときに、version bump の
忘れやテストの失敗を PR の手前で止めます。

- shell の書き方は無数にあり、すべての経路を静的に解析することはできません。読めないと分かった
  形 (コマンド置換・`bash -c`・`pushd` など) は止める側に倒していますが、普段書かない形では読めない
  ことに気づかず、PR 操作を検査しないまま通すことがあります (下の一覧)。別の実行経路
  (スクリプトファイル経由、`gh api` で直接 PR を作る等) は対象外です
- 次の書き方の PR 操作は、見つけられずに検査しないまま通すことがあります (普段の `gh pr create` の
  書き方ではない形。意図して細工した形なので塞ぎません):
  - `sudo` / `xargs` / パス付きの `env` (`/usr/bin/env`) の後ろに置いた PR 操作
  - コマンド語をエスケープや引用で組み立てた形 (`g\h pr create`)、backtick の中で引用したコマンド語
    (`` echo `'gh' pr create` ``)、`gh` と `pr` の間にリダイレクトや展開を挟む形
    (`gh 2>/dev/null pr create` / `gh $e pr create` / `gh <<<x pr create`)
  - 本文を読む shell の名前を、引用や展開で組み立てた形 (`b'as'h -c '...'` / `${SH:-bash} -c '...'`)
  - `bash -c` の引数の中の、エスケープした引用の入れ子 (`bash -c "V=\"p q\" gh pr create"`)
  - 珍しい代入や値の後ろの PR 操作 (`X+=1 gh pr create` / `X[0]=1 gh pr create` / `X=$[1 + 2] gh pr create` /
    `X=(a b)gh pr create` / `X=<(echo a)#b gh pr create`)
  - 展開で組み立てたコマンド語の `cd` (`${c}d <dir>`) と、`${x#(}` を含む語の後ろの `cd`。`cd` と読まないので、
    移動先を追わずに元の cwd を検査します。また `cd` は成功するものとして検査先を決めるので、失敗した
    `cd` の後ろの絶対パスの `cd` を採用して、別の場所を検査することがあります
  - 構文エラーになる形 (閉じていない引用など) は、shell が実行しないので読みません
- 安全網は「数えた実行位置の数」と「検出した呼び出しの数」を比べるだけなので、`\gh` と引用の中の
  `$(gh ...)` を 1 つのコマンドに並べた形 (`\gh pr create -B main; echo "$(gh pr create -B other)"`) では、
  数が合って別の `gh` の検査が抜けることがあります。意図して細工した形なので、うっかり予防の
  範囲外として塞ぎません。同じ `gh` を単独で書けば、従来どおり止まります
- plugin repo かどうかは hook の時点の作業ツリーで決めます。同じコマンドの前のほうで、hook の時点で在る
  dir を plugin repo に変える形 (空の dir への clone `git clone URL empty && cd empty && gh pr create`・
  `ln -sfn <plugin repo> link` での付け替え・`rm -rf d && git clone URL d` での取り直し) は、確かな cd でも、
  確かでない cd の後ろでも検査しません (普段は clone と PR の作成を 1 つのコマンドにまとめないので塞ぎません)
- 次の書き方は、PR 操作が無い、または検査先が 1 つに決まるのに止めることがあります。
  `cd <絶対パス> && gh pr create` の形にするか、本文を `--body-file` で渡すと通ります:
  - comment や引用符の中の `gh pr create` の文字列 (`echo hi # ; gh pr create` /
    `gh pr create --body "a; $y gh pr create b"`)
  - 走る cwd が 1 つに決まるのに、読みを足していない形 (`if ! cd A; then exit 1; fi; gh pr create` /
    `cat x | (cd A && gh pr create)` / `[ -d A ] && { cd A; gh pr create; }` /
    `cd A; make & wait; gh pr create`)。hook の cwd か cd の行き先のどれかが plugin repo のとき、
    行き先が hook の時点で無いとき、hook の環境に `CDPATH` があるとき (相対の名前の cd) に止めます
- 環境変数 `VERIFY_PLUGIN_RELEASE_MODE` で止めずに通すこともできます
- 最終的な品質保証は CI とレビューが担う前提です。この hook はその手前の早期警告です

## インストール

```
/plugin marketplace add Mao-o/cc-mp-worktools
/plugin install verify-plugin-release@mao-worktools
```

## 動作する条件

- Bash ツールで実行するコマンドに `gh pr create` (別名 `gh pr new`) または `gh pr ready` が含まれる
  (`git push && gh pr create ...` のような複合コマンドや `cd <dir> && ...` も追跡します)
- コマンドを実行する repo が plugin repo である
  (`.claude-plugin/marketplace.json` か `.claude-plugin/plugin.json` がある)

それ以外のコマンド・repo では何もしません。

## 検査項目

base と HEAD の差分を対象にします。base は `gh pr create` と同じ順で決めます:
`--base <branch>` → `git config branch.<現在の branch>.gh-merge-base` → `origin` の default branch。

| 検査 | 内容 | 結果 |
|---|---|---|
| `branch` | base と同じ branch / detached HEAD で PR を作ろうとしていないか | FAIL |
| `diff` | base との差分があるか | FAIL |
| `uncommitted` | 検査対象の plugin に未 commit の変更が無いか (テストと validate は作業ツリーで走るため、あると PR の中身を検査したことにならない) | FAIL |
| `uncommitted-other` | それ以外の場所の未 commit の変更 | WARN |
| `single-plugin` | 1 PR = 1 plugin になっているか (**設定で有効化したときだけ**) | FAIL |
| `version[<plugin>]` | 変更した plugin の `plugin.json` の version が上がっているか (semver で比較。下げは FAIL、semver でなければ WARN) | FAIL / WARN |
| `changelog[<plugin>]` | その plugin の `CHANGELOG.md` を更新したか (ファイルが無ければ SKIP) | FAIL |
| `tests[<plugin>]` | その plugin のテストが通るか | FAIL |
| `validate[<plugin>]` | `claude plugin validate` が通るか (warning は既定で WARN) | FAIL / WARN |
| `listed[<plugin>]` | 新規 plugin が `marketplace.json` に登録されているか | WARN |
| `removed[<plugin>]` | 削除した plugin の entry が `marketplace.json` に残っていないか | FAIL |
| `validate[marketplace]` | `marketplace.json` を変えた / plugin を追加したときの marketplace 全体の validate | FAIL / WARN |
| `workflow-yaml` | 変更した `.github/workflows/*.yml` が YAML として読めるか (PyYAML がある場合のみ) | FAIL |
| `merge` | base と競合しないか (`git merge-tree --write-tree` で作業ツリーに触れず試算) | FAIL |

- plugin 内の変更が `README.md` / `CHANGELOG.md` / `LICENSE` / `docs/` 配下だけなら、
  配布に影響しないため version と CHANGELOG の検査は省きます
  (`SKILL.md` など機能に効く Markdown は対象です)
- テストは既定で、plugin 配下の `tests/` ディレクトリ (`test*.py` を含むもの) ごとに
  `python -m unittest discover tests` を実行します。Python 以外の plugin は
  `test_command` で指定してください。検出できなければ SKIP します
- `claude` コマンドが PATH に無い環境では validate を SKIP します
- `claude plugin validate` が出す「Plugin directory checks」の節 (公式のプラグインディレクトリに申請するとき向けの
  検査。CLI 自身が結果を変えないと明記している) の warning は、`strict_validate` でも FAIL にせず WARN で表示します。
  節の見出しか締めの行が見つからないときは節として扱わず、従来どおりすべての warning を数えます

## 止める / 止めないの判定

| 状況 | 動作 |
|---|---|
| FAIL が 1 つ以上ある | **PR 作成を止める** (deny)。FAIL / WARN の行を理由として返す |
| ゲートを完了できない (制限時間切れ・設定ファイルの破損・想定外のエラー) | **止める** |
| `gh pr create --draft` | 検査はするが止めない (結果を伝えるだけ) |
| WARN のみ | 止めない (結果を伝える) |
| すべて PASS | 何も出力しない |

Claude Code は PreToolUse hook が時間切れになるとコマンドをそのまま実行します。このため
hook 全体で 1 つの予算 (105 秒) を持ち、hook 自体の timeout (120 秒) より先に打ち切って「止める」判断を
返します。コマンドの読み取り (15 秒まで)、repo の確認と設定の読み (各 10 秒まで)、ゲート (既定 90 秒) はどれも
予算の残りを上限に動きます。1 つのコマンドに PR 操作が複数ある形 (`gh pr create --draft && gh pr ready`) も
同じ予算を分け合います。予算で制限時間が縮んで止まったときは、理由にそう書きます。

**制限時間に注意**: テストの実行時間も 90 秒に含まれます。test suite は並列に実行するので、
所要時間はおおむね「最も遅い suite」程度ですが、それでも時間のかかる suite を含む PR では
制限時間を超えて止められることがあります。
その場合は `test_command` で実行範囲を絞るか、`timeout_seconds` を延ばしてください
(上限 110 秒。ただし上の予算の残りが先に効きます)。それでも収まらない場合は `VERIFY_PLUGIN_RELEASE_MODE=warn` を使います。

`cd "$DIR" && gh pr create` のように移動先が変数で、どの repo を検査すればよいか
静的に分からない場合も、「ゲートを完了できない」扱いで止めます (絶対パスで書けば通ります)。
cd が何度もあるときは順に合成します (`cd plugins/x && make test && cd ../.. && gh pr create` は
repo root を検査します)。shell の cwd を変えない cd (`(cd X && make) && gh pr create` のサブシェルの中、
`ROOT=$(cd .. && pwd); gh pr create` の値の中、パイプの左、`&` の前、`env` / `nohup` の後) の後ろは、元の cwd を検査します。
その後に相対パスの cd が続く場合と、`cd -` を合成する場合は、移動先が決まらないので止めます。
cwd を変えるか確かでない cd の後ろも止めます: `cd A || cd B` の両側 (`cd A || exit` の A は決まります)、パイプの右、
`[ -d A ] && cd A; gh pr create` (`&&` の前に別のコマンドがあり後ろが `;` か改行で続く形。`make && cd A && gh pr create` は検査します)、
`if` の分岐を閉じた後、ループ・`case`・関数の中、`command cd` / `time cd` (シェルで読みが割れます)、引数なしの `cd`、
`CDPATH=` / `CDPATH+=` / `HOME=` の代入がある形。止まったら `cd <絶対パス> && gh pr create` の形にしてください。
ただし、確かでない cd の後ろでも、shell が居うる cwd (hook の cwd に、それまでの cd を走った・走らなかったの組み合わせで
当てた先) をすべて挙げられ、どれも plugin repo でなければ何もしません。plugin repo でない repo と repo の外で
`git push && cd A; gh pr create` や `[ -d A ] && cd A; gh pr create` を止めないためです。挙げた cwd の 1 つでも plugin repo なら
止めます。挙げられない形も止めます: 引数なしの `cd`、変数・glob・`~user` / `~-` を含む引数、`cd -`、ループ・関数・`case` の中の cd、
`chdir`、読まなかった `cd` の語 (引用や `trap` の本文の中)、`eval` / `source` / `.` や展開で決まるコマンド語がある形、
hook の環境に `CDPATH` があるときの相対の名前 (`cd A` / `cd A/B`。`./A`・`../A`・`~`・絶対パスは CDPATH を引かないので挙げる)、
hook の時点で行き先が無い cd (同じコマンドの前のほうの `git clone URL d` / `mkdir -p d` が作りうるので、確かな cd の
「移動先が存在しない」と同じく止めます。前の cd の先から当てた組み合わせ (`(cd A); cd A` の `A/A`) だけが無い形も含みます)、
Windows のドライブの無い絶対パス (`/tmp` は Git Bash が別の場所に読み替えます)、挙げた cwd が 16 を超える形。
`eval "cd A"` や `. ./env.sh` の中の cd は読みません。
Windows の Git Bash の `/c/Users/...` の綴りは、移動先も hook の cwd も `C:/Users/...` と読みます。
heredoc の本文はコマンドとして読まないので、本文に `'` があっても止めません。区切り語に引用がある
heredoc (`<<'EOF'` など) の本文に `gh pr create` の使い方を書いても止めません (引用の無い区切り語の
本文の `$(...)` は shell が実行するので、従来どおり止めます)。
`--body "$(cat <<'EOF' ...)"` の形の本文も、奇数個の `"` / `'` があっても止めません (こちらは本文の行頭に
`gh pr create` があると止めます)。ただし、コマンドのどこか (本文を含む) に `cd` / `chdir` / `pushd` / `popd` /
`eval` / `source` / `builtin` / `trap` / `HOME=` / `CDPATH=` の文字列があると本文を読み飛ばさず、奇数個の引用符で
止めます (本文を分けて単独の `gh pr create` にすると通ります)。

`X=$(echo a b) gh pr create` や `GH_REPO=$(basename $(pwd)) gh pr create` のように、代入の値に空白や置換がある形の
後ろの PR 操作は「解析できない」で止めます。先に別の行で変数に入れてください (`X=$(echo a b); gh pr create`)。
引用符の中に閉じた値 (`ROOT="$(git rev-parse --show-toplevel)" gh pr create`) は検査します。
`(` `{` backtick が多すぎるコマンドの PR 操作は、読むと時間がかかりすぎるので止めます。数えるのは、空白も区切りも無い 1 つの語に
これらが合わせて 256 個以上ある形と、コマンド全体で `(a=` や `{>` のように直後に代入かリダイレクトが続くものが 256 個以上ある形です
(詰めた JSON を `--body` に直接書いたときも `--body "$(cat <<'EOF' ...)"` の本文に書いたときも数えます。単独のコマンドにしても
止まるので、本文はファイルに置いて `--body-file` で渡してください)。`gh` の `--repo` を 9 個以上続けて並べた形も同じ理由で止めます。

**大きいコマンドは読みません。** コマンド全体が 32768 文字 (32 KB) を超えると、中身を読まずに語だけを見ます。`gh` という語 (パス付きも含む)、
単独の `pr`、`create` / `new` / `ready` が揃う大きいコマンドと、`--repo` を 9 個以上続けて並べた大きいコマンドは止めます。それ以外は読まずに通します。大きさは読む前の文字数で数えるので、
heredoc の本文も含みます。語の並びは見ないので、PR 操作でなくても語が揃えば止まります。

| 止まる大きいコマンドの例 | 直し方 |
|---|---|
| `gh pr create --body "..."` / `--body "$(cat <<'EOF' ...)"` の本文が約 32 KB を超える | 本文をファイルに置いて `--body-file` で渡す |
| 長いファイルを heredoc で書くコマンドの本文に `gh pr create` という文字列がある | ファイルの作成と PR 操作を別のコマンドに分け、長いファイルは heredoc 以外の方法で書く |
| `gh pr view` と別の場所の `new` のように、語が別々にある長いコマンド / `gh pr ready --undo` を含む長いコマンド | コマンドを分ける |

**読み取りには期限があります。** 4096 文字以上で `gh` と `pr` の文字を含むコマンドは、別のプロセスで読みます。15 秒で読み終わらないとき、
読むプロセスが異常終了したとき、結果を受け取れないときは止めます (`--body-file` を案内します)。読むのに時間がかかる形が hook の時間切れで
素通しにならないようにするためです。

同様に、PR 操作がコマンド置換 (`url="$(gh pr create ...)"`)・関数・`pushd` の後などにあって
検査対象を特定できない場合も止めます。**`gh pr create` / `gh pr ready` は単独のコマンド
(前に置くのは `cd <絶対パス> &&` や `git push &&` 程度) として実行してください。**

PR の作成先は gh と同じ順で決まります: `--repo` → 環境変数 `GH_REPO` → `gh repo set-default`
の既定 → `origin`。これが `origin` と別の repo を指している場合も止めます (手元で検査した
base と PR の base が一致しないため)。

`gh pr ready` では `gh pr view` で PR の branch・base・head commit を取得し、手元の checkout と
照合します。別 branch の PR、または PR の head commit と手元の HEAD が違う (push していない
commit がある等) 場合は、手元の検査結果が PR に当てはまらないため止めます。`gh pr view` が
失敗した場合も「ゲートを完了できない」扱いで止めます。

`gh pr create --head <branch>` で現在の checkout と違う branch を指定した場合も止めます。
その branch を checkout してから実行してください。
`--head owner:branch` (fork 側の branch) と、`--repo [HOST/]OWNER/REPO` が `origin` と別の repo
(host 違いを含む。host を省いた場合は `GH_HOST`、無ければ `github.com`) を指す場合も、
手元の checkout では PR の中身を検査できないため止めます。

`gh pr create --draft && gh pr ready` のように 1 つのコマンドに PR 操作が複数あれば、
すべてを検査します (どれか 1 つでも止める対象なら止めます)。

## 設定

repo ごとに `<repo>/.claude/verify-plugin-release.json` を置けます (無ければ既定値)。
**hook は commit 済みの設定だけを読みます。** 未 commit の書き換えで検査を弱められないように
するためで、作業ツリーの内容が commit と違う場合はその旨を伝えます。`.claude/` を
`.gitignore` している repo では、このファイルだけ除外指定 (`!.claude/verify-plugin-release.json`)
が必要です。

```json
{
  "single_plugin_per_pr": false,
  "strict_validate": false,
  "fetch": true,
  "timeout_seconds": 90,
  "test_command": null
}
```

| キー | 既定 | 内容 |
|---|---|---|
| `single_plugin_per_pr` | `false` | `true` で、複数の plugin (または plugin と repo 直下のファイル) にまたがる PR を FAIL にする |
| `strict_validate` | `false` | `true` で `claude plugin validate` の warning を FAIL にする (plugin directory checks の節の warning は除く) |
| `fetch` | `true` | 検査前に `git fetch origin <base>` する (15 秒で打ち切り、失敗したら手元の ref で検査) |
| `timeout_seconds` | `90` | ゲート全体の制限時間。上限 110。hook 全体の予算 (105 秒) の残りが先に効く |
| `test_command` | `null` | `null` = Python unittest を自動検出 / `false` = テストを走らせない / `["make", "test"]` のような配列 = 変更した各 plugin のディレクトリで実行 |

未知のキーや型の誤りは「設定の破損」として扱い、PR 作成を止めます (有効にしたつもりの
検査が黙って効いていない状態を避けるため)。

環境変数 `VERIFY_PLUGIN_RELEASE_MODE`:

| 値 | 動作 |
|---|---|
| `enforce` (既定) | 上の判定どおり止める |
| `warn` | 検査はするが止めない |
| `off` | 何もしない |

## 手動実行

hook と同じ検査を手元で実行できます。

```bash
python3 <plugin-root>/hooks/verify-plugin-release check [--base main] [--strict-validate] [path/to/repo]
```

終了コードは `0` = PASS / `1` = FAIL / `2` = ゲートを完了できなかった、です。
`--strict-validate` は `claude plugin validate` の warning (plugin directory checks の節は除く) を FAIL にします (CI や独自のゲートから
呼ぶとき用。設定ファイルの `strict_validate` より優先)。

## 例

```text
[verify-plugin-release] PR 前の完了条件を満たしていない。FAIL を解消してから再実行する。
base: origin/main / branch: feat/example
FAIL  version[example-plugin]: version が据え置き (1.2.0)。bump しないと既存ユーザーに更新が届かない
FAIL  changelog[example-plugin]: plugins/example-plugin/CHANGELOG.md が未更新
WARN  uncommitted: PR に載らない未 commit の変更がある: plugins/example-plugin/notes.txt
(一時的に止めずに通すには VERIFY_PLUGIN_RELEASE_MODE=warn)
```

## 注意: テストコードを実行する

この hook は repo 内のテストと `test_command` を、permission prompt なしで実行します。
信頼できない repo では `VERIFY_PLUGIN_RELEASE_MODE=off` にしてください。

## 外部送信

plugin 自身は外部へデータを送りません。ただし検査のために次のコマンドを起動し、
それらはネットワークにアクセスします。

- `git fetch origin <base>` (`fetch: false` で無効化)
- `gh pr view` (`gh pr ready` のときのみ)
- `claude plugin validate`

## 要件

- Python 3.11+ / git (競合検査は 2.38 以上。それ未満では SKIP)
- `gh` (`gh pr ready` の branch 確認に使用)
