# verify-plugin-release

Claude Code の plugin marketplace repo で、`gh pr create` / `gh pr ready` を実行する前に
**PR 前の完了条件**を検査する PreToolUse hook です。条件を満たしていなければ PR の作成を
止め、何が足りないかを Claude に返します。

「version を bump し忘れて既存ユーザーに更新が届かない」「CHANGELOG を書き忘れた」
「テストが落ちたまま PR を出した」といった、レビューで毎回指摘される類の漏れを
PR の手前で機械的に落とすのが目的です。

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

## 止める / 止めないの判定

| 状況 | 動作 |
|---|---|
| FAIL が 1 つ以上ある | **PR 作成を止める** (deny)。FAIL / WARN の行を理由として返す |
| ゲートを完了できない (制限時間切れ・設定ファイルの破損・想定外のエラー) | **止める** |
| `gh pr create --draft` | 検査はするが止めない (結果を伝えるだけ) |
| WARN のみ | 止めない (結果を伝える) |
| すべて PASS | 何も出力しない |

Claude Code は PreToolUse hook が時間切れになるとコマンドをそのまま実行します。このため
ゲート内部に制限時間 (既定 90 秒) を持ち、hook 自体の timeout (120 秒) より先に打ち切って
「止める」判断を返します。

**制限時間に注意**: テストの実行時間も 90 秒に含まれます。テストに時間のかかる plugin を
含む PR や、複数の plugin にまたがる PR では制限時間を超えて止められることがあります。
その場合は `test_command` で実行範囲を絞るか、`timeout_seconds` を延ばしてください
(上限 110 秒)。それでも収まらない場合は `VERIFY_PLUGIN_RELEASE_MODE=warn` を使います。

`cd "$DIR" && gh pr create` のように移動先が変数で、どの repo を検査すればよいか
静的に分からない場合も、「ゲートを完了できない」扱いで止めます (絶対パスで書けば通ります)。

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
| `strict_validate` | `false` | `true` で `claude plugin validate` の warning を FAIL にする |
| `fetch` | `true` | 検査前に `git fetch origin <base>` する (15 秒で打ち切り、失敗したら手元の ref で検査) |
| `timeout_seconds` | `90` | ゲート全体の制限時間。上限 110 |
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
python3 <plugin-root>/hooks/verify-plugin-release check [--base main] [path/to/repo]
```

終了コードは `0` = PASS / `1` = FAIL / `2` = ゲートを完了できなかった、です。

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
