# file-split-advisor

`Write` / `Edit` の直後に、行数と責務混在の構造シグナルを組み合わせてファイル
分割の検討を促す advisory メモを返す hook。**block/deny は一切しない** —
`sensitive-files-guardrail` の "guardrail" (block する) と対比した "advisor"
(判断材料を提示するだけ) という命名。

## 同梱 hook

| Hook | 発火イベント | 役割 |
|---|---|---|
| `file-split-advisor` | `PostToolUse(Write\|Edit)` | 行数 tier + 構造シグナルを判定し、閾値超過時に `additionalContext` で分割検討メモを注入 |

## インストール

```bash
/plugin marketplace add Mao-o/cc-mp-worktools
/plugin install file-split-advisor@mao-worktools
```

## なぜ行数だけで判定しないか

言語ごとの記述密度やファイルの役割 (ロジック/宣言的/型定義/生成コード/テスト)
によって「適正な長さ」は変わる。単純な行数一律基準は誤検出が多いため、以下を
組み合わせて判定する:

1. **言語係数** — Java/C#/Kotlin/Swift/C++ 等はボイラープレートで長くなりやすい
   ので閾値を上げる
2. **role 係数** — テストファイルは記述が単調に伸びやすいため閾値を 2.5 倍緩和
3. **宣言的コード緩和** — 制御フロー密度が低い (ルーティング定義・型定義・DTO
   等) ファイルは閾値をさらに 1.6 倍緩和 (**テストファイルには重ねない**)
4. **構造シグナル** — import カテゴリの多様性・命名の抽象度・定義数過多・制御
   フロー密度の高さを検出し、行数だけでは判断できない責務混在を拾い上げる

判定の主軸は **1〜3 で調整した行数 tier** で、4 の構造シグナルは補助
(`note`/`review` tier の昇格判定に使う)。詳しくは
「[構造シグナル](#構造シグナル)」節の実測値を参照。

さらに、**判定対象は実コードの拡張子に限定**し、**ファイルを大きくしない編集
(typo 修正など) では通知しない**。

## 判定ロジックサマリ

### 行数 tier (半開区間、`ok < note <= review <= warn <= strong`)

基準値 (係数 1.0 の場合): `note=150 review=300 warn=500 strong=800`。

実効閾値 = 基準値 × 言語係数 × role 係数 × (宣言的なら 1.6 倍) × 全体倍率
(`FILE_SPLIT_ADVISOR_SCALE`、既定 1.0)。

**宣言的緩和は `role=normal` にだけ掛かる。** `role=test` は role 係数 (2.5 倍)
だけで一律に緩和し、制御フロー密度に依らず同じ閾値になる。

| 言語 | 係数 | 言語 | 係数 |
|---|---|---|---|
| java / csharp | 1.5 | dart | 1.3 |
| objectivec | 1.4 | cpp | 1.3 |
| kotlin | 1.4 | swift / c / powershell | 1.2 |
| javascriptreact / typescriptreact | 1.15 | rust / php | 1.1 |
| vue / svelte | 1.15 | 上記以外 | 1.0 |

係数 1.0 の言語: python / javascript / typescript / go / ruby / scala / elixir /
shell / lua / perl / r / groovy / clojure / haskell / erlang / julia / zig / nim。

role 係数: `test=2.5` / `normal=1.0`。宣言的緩和は `control_flow_density < 0.02`
のときに 1.6 倍 (`role=normal` のみ)。

実効閾値の例 (全体倍率 1.0):

| 言語 / role / 宣言的 | note | review | warn | strong |
|---|---|---|---|---|
| python / normal / いいえ | 150 | 300 | 500 | 800 |
| python / normal / はい | 240 | 480 | 800 | 1280 |
| python / test (宣言的かは不問) | 375 | 750 | 1250 | 2000 |
| java (1.5) / normal / いいえ | 225 | 450 | 750 | 1200 |
| java (1.5) / test | 563 | 1125 | 1875 | 3000 |

`java / test` の `note` だけ実値が 562.5 で、メモには切り上げた 563 が出る
(判定は `行数 >= 562.5` なので 563 行で初めて `note` に届く)。

### test 判定

以下のいずれかに一致すると role が `test` になり、role 係数 (2.5倍) が適用され
`def_count` シグナルの評価対象からも除外される (宣言的緩和は重ねない)。

- **ディレクトリ名** (大文字小文字を無視): `test` / `tests` / `__tests__` /
  `spec` / `specs` / `e2e`。**判定するのは `cwd` から見た相対パスの階層だけ**
  — プロジェクトの置き場所がたまたま `~/work/test/myapp/` のような場合に、
  配下の全ソースが test 扱いになるのを防ぐ (`cwd` の外にあるファイルと、
  envelope に `cwd` が無い場合は全階層を見る)。

  **制約**: この絞り込みは `cwd` 自身がテストディレクトリのときに逆向きに
  働く。`cwd` を `/repo/tests` にして (テストディレクトリ直下で作業して)
  その配下のファイルを編集すると、相対部分に `tests` が現れないため
  role が `normal` になる。ファイル名パターン (`test_*.py` 等) に一致する
  ファイルは引き続き `test` と判定されるため、影響を受けるのは
  「テストディレクトリ配下にあるがファイル名が test 形式ではない」
  ヘルパ等に限られる
- **ファイル名パターン** (大文字小文字を区別する。区別しないと `Latest.cs` /
  `Manifest.kt` のような通常のファイル名まで test 扱いになる):

  | パターン | 例 |
  |---|---|
  | `test_*.py` / `*_test.py` / `conftest.py` | `test_foo.py` |
  | `*.test.*` / `*.spec.*` (`js` `mjs` `cjs` `jsx` `ts` `mts` `cts` `tsx`) | `app.test.js` `foo.spec.tsx` |
  | `*_test.rb` / `*_spec.rb` | `user_spec.rb` |
  | `*Test.*` / `*Tests.*` (`java` `cs` `kt` `kts` `php` `swift` `scala` `groovy`) | `FooTests.cs` |
  | `*_test.*` (`go` `dart` `rs` `php` `ex` `exs`) | `foo_test.dart` |

### 構造シグナル

**判定の主軸は行数 tier で、構造シグナルは補助**。シグナルは (1) `note` tier の
昇格判定 (2 個以上)、(2) `review` tier の昇格判定 (1 個以上) にのみ使い、
`warn` 以上は行数だけで emit する。

| シグナル | 条件 | 実測の点火数 |
|---|---|---|
| import カテゴリ多様性 | network/db/ui/logging/testing/auth/filesystem の 7 カテゴリのうち 4 種以上を import | 2 |
| 命名が抽象的 | ファイル名の全トークンが `util/common/helper/service/manager` 等の総称語のみ | 176 |
| 定義数過多 | 関数/クラス定義が 20 以上 (テストファイルは評価しない) | 287 |
| 制御フロー密度高 | 制御フロー構文を含む行が 25% 以上 (宣言的緩和が不適用の場合のみ) | 96 |

「実測の点火数」は複数プロジェクトから集めた約 1 万ファイル (10,097 件) の
コーパスでの件数。**emit の約 4 割はシグナルが 1 個も点火しておらず**
(0.5.0 で 171/446、0.6.0 で 160/434)、`note` tier に入った 1,257 件のうち
emit に至ったのは 2 件だけだった。role=test は「定義数過多」を評価しないため、
実測では `note`/`review` tier からのシグナル昇格が起きておらず、テスト
ファイルの通知は事実上 `warn` 以上 (行数のみが根拠) になっている。つまり現状の
シグナルは「行数が中庸でも責務混在を拾う」経路としてはほとんど働いていない。

閾値を緩める案 (import カテゴリ 4→3 / 密度 0.25→0.20 / `note` 昇格を 1 個に)
も同じコーパスで測ったが、どれもシグナル 0 個の emit を減らさず
(171→164 件)、新たに増えた emit を目視した結果ノイズが過半だったため**見送った**。
先に直すべきは閾値ではなく抽出側 (型宣言だけのファイルで定義数が点火する /
`testing`・`logging` のような横断的カテゴリによる水増し / import 語の誤分類)。

制御フロー密度に数える語:

- 全言語共通: `if` / `for` / `while` / `switch` / `case` / `catch` / `except`
- 言語別に追加: Python `elif` `try` `match` (文頭のみ) / Ruby `elsif` `unless`
  `until` `rescue` `when` / Rust `match` `loop` / Kotlin `when` / Go `select`。
  言語で絞るのは、`match` や `select` が他言語では普通のメソッド名・関数名
  (`str.match(...)`) として頻出するため。Python の `match` は文頭でも
  直後が `=` / `(` / `.` / `[` なら数えない (`match = re.match(...)` /
  `match.group(0)` は soft keyword を変数名として使っているだけ)。ただし
  **行末が `:` なら `(` が続いても数える** — `match (value):` /
  `match (a, b):` は subject を括弧で囲んだ / tuple subject の match 文
- **行コメント・ブロックコメント・文字列リテラルの中は数えない**。分母
  (非空行数) は元のテキストのまま — コメント行も 1 行として数える

### emit するかどうか

- tier が `warn` 以上 → 常に emit (シグナル数によらない。この大きさになれば
  行数そのものをレビュー発火の十分条件として扱う)
- tier が `review` → 構造シグナルが 1 個以上のときのみ emit
- tier が `note` → 構造シグナルが 2 個以上のときのみ emit (行数は中庸だが責務
  混在が疑われるファイルを拾う)
- tier が `ok` → emit しない

## メモに載る情報

```
静的解析メモ (file-split-advisor): src/checkout_flow.py
行数: 1011 (言語: python, 判定: strong / 目安 warn=500 strong=800 (python 1.0))
検出シグナル: 定義数 28 / import カテゴリ多様性 4種 (network, db, ui, auth)
大きい定義 上位5: parse_sections(120行, 214行目〜) / build_index(86行, 334行目〜) / …
import クラスタ: network(requests, urllib) / db(sqlite) / ui(react) / auth(jwt)
行数は分割要否の直接的根拠ではなく、…
```

最初の 3 行 (パス / 行数と判定根拠 / 構造シグナル) は常に出る。4 行目以降は
**分割候補の境界を示す付記**で、材料が無ければ出ない:

- **大きい定義** — 占有行数の大きいトップレベル定義を最大 5 件。行数は Python
  のみ正確 (AST の定義範囲) で、他言語は**次のトップレベル定義までの距離**に
  よる概算 (パーサを持たないため、定義の間にある文も含まれる)
- **import クラスタ** — カテゴリを立てた依存の名前。**2 カテゴリ以上あるとき
  だけ**出す (単一カテゴリの列挙では境界が示せないため)。名前はカテゴリ辞書に
  一致した字面なので、`http` のようにモジュール名でない語も混じる

メモ全体は 10,000 文字に収まるよう組み立て、超える場合は**付記行から先に
落とす** (判定根拠は常に残す)。付記行は表示専用で、emit 判定には一切使わない。

## 通知の抑制 (debounce)

- **ファイルを大きくしない編集では通知しない**
  - `Edit` は `old_string` / `new_string` の行数差で判定する。差が 0 以下
    (typo 修正・同じ行数のリファクタ・行を削る編集) なら通知しない。
    `replace_all` でも 1 箇所あたりの差分の符号は変わらないため考慮不要
  - `Write` は編集前の内容が hook に渡らないため、同一セッション内に直近の
    行数記録があればそれと比較する。記録が無い初回は通知する
- **1 セッション内で 1 ファイル × 1 tier につき 1 回のみ** 通知 (ハイウォーター
  マーク方式。tier が悪化したときのみ再警告し、shrink→regrow で同一 tier に
  戻っても再警告しない)
- `FILE_SPLIT_ADVISOR_MAX_EMITS` (既定 20) — セッション内 emit 数の安全弁

### debounce の記録先

セッションごとに 1 ファイル (`<session_id の sha256 16桁>.json`) を作る。

1. `$TMPDIR/file-split-advisor/` (既定。`$TMPDIR` 未設定なら `/tmp`)
2. 1 に書けない場合は `$XDG_CACHE_HOME/file-split-advisor/`
   (未設定なら `~/.cache/file-split-advisor/`)
3. **どちらにも書けない場合は通知しない**。debounce が成立しない状態で通知すると
   同じファイルを編集するたびに同じメモが出続け、原因がユーザーから見えない。
   理由を stderr に 1 行出す (hook は編集ごとに新プロセスなので、書けない間は編集ごとに 1 行)

新しいセッションの記録を作るときに、同じディレクトリの **7 日より古い
`*.json` を削除**する (opportunistic。失敗は無視する)。削除は「新規作成時」に
限るため、長時間動いているセッション自身の記録は消えない。

## 環境変数

| 変数 | 既定値 | 意味 |
|---|---|---|
| `FILE_SPLIT_ADVISOR_DISABLED` | (未設定) | `1`/`true`/`yes`/`on` で hook を無効化 |
| `FILE_SPLIT_ADVISOR_MAX_EMITS` | `20` | セッション内の最大 emit 回数 |
| `FILE_SPLIT_ADVISOR_CWD_ONLY` | (未設定) | `1`/`true`/`yes`/`on` で `cwd` 外のファイルを skip する。既定 off — `--add-dir` で cwd 外を正当に編集する運用を壊さないための opt-in |
| `FILE_SPLIT_ADVISOR_IGNORE` | (未設定) | 判定対象から除外する glob (カンマ区切り、fnmatch)。ファイル名・フルパスの両方に対して判定する |
| `FILE_SPLIT_ADVISOR_SCALE` | `1.0` | 全閾値 (note/review/warn/strong) に一律で掛ける倍率。0 以下・数値に変換できない値・`nan`/`inf` 等の非有限値に加え、他の係数 (言語/role/宣言的緩和) と組み合わせた結果が非有限になるほど巨大な値 (例: `1e308`) も既定 (1.0) にフォールバックする。メモの目安に表示される倍率 (`(全体 N倍)`) は設定値をそのまま反映し、固定桁数への丸めは行わない (`0.004` は `0.004倍`、`1.004` は `1.004倍` と表示され、いずれも中立値 `1.0倍` と区別できる) |

`FILE_SPLIT_ADVISOR_IGNORE` に加え、`~/.claude/file-split-advisor/ignore.local.txt`
(1 行 1 glob、`#` 始まりはコメント、空行は無視) があれば読み込んで併用する
(gitignore の否定 `!` 等の完全な構文には対応しない、素朴な fnmatch のみ)。
ファイルが存在しない/読めない場合 (非UTF-8 を含む) は無視する (fail-open)。

**glob は fnmatch ベースの完全一致 (anchored) で、ファイル名または
フルパス (絶対パス) のいずれかに対して判定する。** gitignore 風に見えても
相対パス形のパターン (例: `migrations/*`) は絶対パスの途中にしか現れない
文字列には決してマッチしない。ディレクトリを対象にしたい場合は
`*/migrations/*` のように先頭に `*` を置く。

## 早期 skip 対象

- lockfile (`*.lock` 全般 — `yarn.lock` / `Cargo.lock` / `poetry.lock` /
  `uv.lock` / `flake.lock` 等 — に加え `package-lock.json` /
  `npm-shrinkwrap.json` / `pnpm-lock.yaml` / `go.sum`)
- minified (`*.min.js` / `*.min.css` / `*.map`)
- generated ファイル名パターン (`*.pb.go` / `*.pb.*` / `*_pb.js` / `*_pb.ts` /
  `*_pb2.py` / `*_pb2_grpc.py` / `*.g.dart` / `*.freezed.dart` /
  `*_generated.*` / `*.generated.*` / `*.gen.*` / `*_gen.go` / `*.d.ts` /
  `*.snap`)
- **第三者コード・生成物のディレクトリ** (`node_modules` / `vendor` / `venv` /
  `.venv` / `site-packages` / `__pycache__` / `__snapshots__` / `generated`)。
  test 判定と同じく **`cwd` から見た相対パスの階層だけ**を見る (`cwd` が
  無い・`cwd` の外にあるファイルでは全階層を見る)。`dist` / `build` /
  `migrations` / `alembic` / `versions` は、手書きのソースが入る
  ことが普通にあるため**入れていない**
- **上の言語係数表に載っていない拡張子のファイル全般** — Markdown / JSON / YAML /
  TOML / CSV / XML / SVG / HTML / SQL / notebook / プレーンテキスト等。行数だけで
  分割検討を促しても有用でないため判定対象にしない
- **拡張子を持たないファイルのうち、既知の shebang で始まらないもの**
  (`Makefile` / `LICENSE` / `#!/usr/bin/awk -f` 等)。拡張子なしのファイルは
  内容の先頭行を見て判定する: `python` / `node` / `ruby` / `php` / `perl` /
  `bash` / `sh` / `zsh` (`#!/usr/bin/env python3` / `#!/usr/bin/python3.11` /
  `#!/usr/bin/env -S node --loader=ts` のいずれの形も可) は対応する言語として
  判定対象に入る。`bash` / `sh` / `zsh` は `.sh` / `.bash` / `.zsh` と同じ
  `shell` として扱う (拡張子付きは判定するのに shebang 付きは判定しない、
  というファイル名依存の非対称を作らないため)。
  **先頭が `.` のファイル (`.bashrc` / `.envrc` 等) は対象外** — 慣例として
  設定ファイルであり、内容を読む対象を無用に広げないため
- 拡張子がある場合は**拡張子の判定が優先**される (`.py` に
  `#!/usr/bin/env node` と書かれていても python として扱う)
- ファイル先頭 20 行に generated マーカー (`@generated` / `do not edit` /
  `generated by` / `auto-generated` / `auto generated` /
  `automatically generated` / `generated file` / `Revision ID:`) を含むファイル。
  走査幅が 20 行なのは、10〜15 行のライセンスヘッダの後に生成物注記を置く
  生成器 (OpenAPI Generator 等) を拾うため
- symlink / FIFO 等の非通常ファイル、2MB 超、20,000 行超のファイル
- **一時ディレクトリ配下のファイル** (`$TMPDIR` / `/tmp` / `/private/tmp` /
  `/var/folders` 配下)。ただし対象ファイルが `cwd` の内側にあるとき (session
  全体がその場限りの一時プロジェクトである場合を含む) は対象にする。Claude が
  分析用ダンプ・handoff メモ等の一時ファイルを scratchpad に書く運用があり、
  プロジェクト外のファイルにまで分割助言が注入されるのを防ぐ (opt-out 機構なし)
- `FILE_SPLIT_ADVISOR_IGNORE` / `ignore.local.txt` に一致するファイル (上記
  「環境変数」参照)

## 設計原則

1. **block しない** — advisory メモのみ。判断はモデル/ユーザーに委ねる
2. **fail-open** — 何が起きても exit 0。判定不能・IO 失敗はすべて「通知しない」
   側に倒す (advisory hook に fail-closed は不要)。**debounce の記録先に書けない
   ときも沈黙する** — 記録できないまま通知すると同じメモが出続けるため
3. **透明性** — 行数のみが emit 根拠 (構造シグナルなし) のときは、その旨をメモに
   明記する
4. **純粋関数と I/O の分離** — `language.py` / `metrics.py` / `judge.py` は
   ファイルシステムアクセスを持たない。I/O は `source.py` (読み込み) と
   `state.py` (debounce) に閉じ込める

## 既知の限界 (v1)

- **静的解析のみ**。著者数・コミット頻度などの git 履歴ベースのシグナルは
  含まない
- `line_count` が `note` 閾値未満のファイルは、構造シグナルが何個点火していても
  emit しない (小さいが責務混在したファイルの検出は範囲外)
- 拡張子を持たないファイルの言語判定は先頭行の shebang のみで、認識するのは
  上記 8 種のインタプリタに限る。`Rakefile` / `Jenkinsfile` のような
  「名前が言語を示す」ファイルは判定対象に入らない
- **`/compact` の後も同一セッションでは再通知しない**。debounce は `session_id`
  単位で記録し `/compact` を跨いで残るため、メモがコンテキストから消えても
  同じ tier の再通知は行われない (`PostCompact` で記録をリセットする案はある
  が、通知疲れを防ぐという目的と正面から衝突するため実装していない)。
  再通知が必要なら新しいセッションで開き直す
- メモの「大きい定義」の行数は Python 以外は概算 (次のトップレベル定義までの
  距離)。また列挙するのは**インデントのないトップレベル定義**だけなので、
  クラス内のメソッドしか持たないファイル (Java/C# 等) では 1 件も出ない
- メモの「import クラスタ」に並ぶ名前はカテゴリ辞書に一致した字面であり、
  実際の import パスとは一致しないことがある (カテゴリごと最大 6 件)
- `Write` が既存ファイルを縮めたかどうかは、同一セッション内に直近の行数記録が
  あるときしか分からない (記録が無い初回は通知する)
- `def_count` は行頭キーワード正規表現にマッチする行と、`const foo = (a) => …`
  形のアロー関数代入を数える (Python のみ AST で厳密にカウントし、構文エラー
  時だけこの正規表現にフォールバックする)。認識するキーワードは
  `def` / `class` / `function` / `func` / `fn` / `fun` / `interface` /
  `struct` / `enum` / `trait` / `impl` / `type` / `object` と、その前に置ける
  `export` / `export default` / `declare` / `abstract` / `pub` / `pub(crate)` /
  `async`。**関数/メソッド宣言がキーワードで始まらない Java/C# ではほぼ機能
  しない** — アクセス修飾子と戻り値型から始まるため。これらの言語では行数
  (Java/C# 1.5x 係数) と import カテゴリ多様性・制御フロー密度が主戦力になる。
  オブジェクトリテラル/インタフェースのプロパティ名 (`type:` / `enum?:`) と、
  キーワード直後が `.` / `(` / `[` の行 (`object.keys(x)` / `type(x)` /
  `fn()` — 同じ綴りの識別子を使っているだけ) は除外する。代償として、行頭に
  そのまま置かれた無名関数式 (`function(payload) {`) も数えなくなる
  (実コーパス 8,426 ファイルで 2 行)。キーワードの後に識別子が続く行
  (シェルの `type foo` 等) は依然として誤って数えうる。
  アロー関数は仮引数の括弧と `=>` の間に**戻り型注釈**を許す
  (`const parse = (x: Input): Output => …`)。注釈に許すのは `=` / 括弧 /
  波括弧 / `;` を含まない字面だけなので、`Promise<void>` のようなジェネリック
  は通り、オブジェクト型 (`{ a: number }`) や関数型 (`(x) => y`) を戻り型に
  書いた形は数えない
- import カテゴリ分類はキーワード辞書によるヒューリスティックで、精密な import
  resolver ではない。括弧付きの import ブロック (Go の `import ( … )` /
  Python の `from x import ( … )`) は継続行も import 行として扱う。閉じ括弧は
  独立行 (`)`) と内容行の末尾 (`    b)`) の両方で認識し、見失ったときのために
  追跡は 100 行で打ち切る。**開き括弧・閉じ括弧のどちらの判定でも行末コメントを
  無視する** — `import (  # grouped` でブロックが始まり、`    b)  # noqa` で
  閉じる。ブロック内の**コメントだけの行** (`// http clients`) は import 行
  として扱わないが、**import 行に付いた行末コメントは対象外** —
  `"net/http" // redis に切り替え予定` のような行では、コメント側の語からも
  カテゴリが立つ
- JS/TS では**コメント行の中に書かれた `require(…)`** も import 行として数える
  (`// const redis = require('redis')` のようにコメントアウトされた依存)。
  カテゴリが増える = シグナルが増える方向だが、上記のとおり import カテゴリ
  多様性はほとんど点火しないため実害は観測していない
- C# / PowerShell の `using` は import 形 (`using System.IO;` /
  `using static …;` / `using Alias = Namespace.Type;`) だけを数える。同じ綴りの
  リソース関連構文 — `using (var conn = …)` (using 文)、`using var conn = …`
  (C# 8 の using 宣言)、`using resource = getResource()` (TypeScript 5.2 の
  明示的リソース管理) — は import ではないので除外する。エイリアス形と
  TypeScript の宣言形は字面が重なるため、`=` の右辺に呼び出し (`(`) があるか
  どうかで分ける。したがって `using x = y;` (呼び出しを含まない TypeScript の
  リソース宣言) は依然として import として数えうる
- 行頭とは限らない `require(` を import と見るのは **JS/TS 系の言語だけ**で、
  メンバ呼び出し (`x.require(…)`) は除外する。他言語では同じ綴りが普通の
  関数・メソッド名として使われ、コメントや文字列リテラルの中の `require(` まで
  import 行として数えてしまうため。代償として、Lua の
  `local http = require("socket.http")` のような JS/TS 以外の `require(` は
  分類されない (Ruby / PHP の行頭 `require` は別経路で拾う)
- 制御フロー密度のコメント/文字列除去は正規表現ベースで、以下は扱わない。
  いずれも「本来コードである部分まで潰す」か「コメント/文字列を潰し損ねる」
  方向の誤りで、前者は密度を過小評価する (= 通知が減る) 側に倒れる
  - `<!-- -->` (vue/svelte のテンプレート)、Ruby の `=begin`/`=end`
  - JavaScript の正規表現リテラル中の引用符
  - PowerShell の `<# … #>`、Haskell の `{- … -}`、Lua の `--[[ … ]]`
    (いずれもブロックコメントを潰さないため、中の英単語が制御フローとして
    数えられうる)
  - tsx/jsx の JSX 本文に現れるアポストロフィ (`<p>don't</p>` の `'` が
    文字列の開始とみなされ、行末までが文字列として潰される)
  - 三重引用符を見るのは **Python / Elixir (`"""` `'''`) と Kotlin / C# /
    Swift / Julia / Groovy (`"""`)** だけ。Java の text block、Groovy の
    `'''`、C# の `""""` 以上の多重引用符 (raw string) は対象外で、中の行頭
    `if` / `for` が制御フローとして数えられる
  - 三重引用符 (`"""` / `'''`) とバッククォート (JS/TS のテンプレート
    リテラル、Go の raw string) は**改行を跨いで**潰すため、対になる閉じ記号を
    持たない 1 個 (正規表現リテラルの中のバッククォート等) があると、そこから
    先すべてが文字列として潰される。改行を跨ぐ文字列でも `\` に続く 1 文字は
    区切りとして扱わない (テンプレートリテラル中の `` \` `` を閉じ記号と誤認
    しないため) ので、バックスラッシュをエスケープ記号として扱わない Go の
    raw string が `\` で終わる (`` `\d+\` ``) と閉じ記号を見失う
- 内容による generated 判定は先頭 20 行の部分文字列一致なので、散文が
  マーカーに一致すると誤って skip しうる (例: 冒頭のコメントに
  「this generated file is …」と書かれた手書きファイル)。失敗方向は
  「通知しない」側 (fail-open) なので許容している
- 閾値の詳細な上書き (tier ごと・言語ごとの個別設定) はできない。
  `FILE_SPLIT_ADVISOR_SCALE` は全閾値に一律の倍率をかけるのみで、
  `config.local.json` 的なきめ細かい上書き機構ではない
- **`$TMPDIR` は無検証で一時ディレクトリの root として取り込む**。極端に浅い値
  (`/` や `/Users` 等) や、実際にはプロジェクトの祖先ディレクトリにあたる値
  (`/Users/you/dev` 等) が設定されていると、無関係な兄弟ディレクトリのファイル
  まで「一時領域」扱いになり、`cwd` の外にある限り skip されてしまう
  (通常 `$TMPDIR` は OS が管理する専用パスであり、この状況は稀)

詳細な設計判断の経緯は [hooks/file-split-advisor/CLAUDE.md](./hooks/file-split-advisor/CLAUDE.md) 参照。

## 依存関係

標準ライブラリのみ。`pip install` 不要。Python 3.11+ を想定する。

## テスト

```bash
cd hooks/file-split-advisor
python3 -m unittest discover tests
```

クラス単位・メソッド単位で 1 件だけ指定して実行することもできる:

```bash
python3 -m unittest tests.test_message.TestFormatMultiplier.test_integral_value_gets_one_decimal -v
python3 -m unittest tests.test_judge.TestTierBoundaries -v
# pytest がインストールされていれば node id 指定も可能
pytest tests/test_message.py::TestFormatMultiplier::test_integral_value_gets_one_decimal -q
```

## ライセンス

MIT
