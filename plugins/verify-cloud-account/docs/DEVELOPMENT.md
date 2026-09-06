# verify-cloud-account 実装者ガイド

**plugin の保守・拡張者向け**。利用者向けの概要・設定方法・判定表は
[README.md](../README.md) が正本。

## このドキュメントの方針

以前この内容は追跡されないローカルファイルにしか無く、外部の読者
(と worktree でチェックアウトした自分自身) から見えなかった。同時に
「テスト件数」「ディレクトリツリー」「キャッシュキーの構成要素」といった
**コードを直せば必ず陳腐化する記述**が更新されないまま残っていた。

そのため本ドキュメントは次の線引きで書く:

- **コードが正本のもの (属性の一覧・件数・定数値・分類表) はここに複製しない。**
  参照先のファイル名を書き、そこを読ませる
- ここに書くのは **規則と、その規則にした理由** (コードを読んでも復元できない部分)

## 目的

`PreToolUse:Bash` hook として動作し、Bash コマンドの実行前にクラウド CLI の
アクティブアカウントがプロジェクトの想定値と一致するかを検証する。不一致なら
deny し、切り替え手順を提示する。

複数の AWS / GCP / Firebase / GitHub / Kubernetes アカウントを切り替えて作業する
とき、間違ったアカウントで `gh pr create` / `firebase deploy` / `kubectl apply`
等を実行する事故を防ぐ。期待値は `accounts.local.json` に置き、hook 実行時に
CLI から現在値を取得して照合する。

## ディレクトリ構成

```
verify-cloud-account/
├── .claude-plugin/plugin.json
├── README.md                       利用者向け概要 (判定表の正本)
├── CHANGELOG.md
├── docs/
│   ├── DEVELOPMENT.md              このドキュメント
│   └── wrapper-env-audit.md        D16 の監査記録 (透過 wrapper × env 伝播)
├── skills/                         Agent Skill (Claude 向けプロンプト)
│   ├── accounts-init/SKILL.md
│   ├── accounts-show/SKILL.md
│   └── accounts-migrate/SKILL.md
└── hooks/
    ├── hooks.json                  PreToolUse:Bash の単一エントリ
    └── verify-cloud-account/
        ├── __main__.py             エントリポイント (stdin → dispatch → stdout)
        ├── core/
        │   ├── budget.py           hook 1 回分の実時間予算
        │   ├── cache.py            検証成功の短期キャッシュ
        │   ├── cli_options.py      CLI 名直後の global option 剥がし + context option 抽出
        │   ├── command_parser.py   コマンド分解 (chain split / env strip / wrapper strip)
        │   ├── dispatcher.py       サービス振り分けと検証オーケストレーション
        │   ├── output.py           deny / warn の hookSpecificOutput JSON ビルダー
        │   └── paths.py            accounts.local.json の配置パス解決 (3-tier + 親遡及)
        ├── services/               サービスごとの CLI 呼び出しと照合
        ├── scripts/
        │   ├── accounts_builder.py accounts.local.json 専用 writer (init/show/set/remove/migrate)
        │   └── templates/          プロジェクト側 signpost のテンプレート
        └── tests/                  unittest (標準ライブラリのみ)
```

### 実行フロー

1. `__main__.py` が stdin から hook input (`tool_input.command`, `cwd`) を読む
2. `core.dispatcher.dispatch()` を呼ぶ (ここで実時間予算を張る)
3. `core.command_parser.extract_candidates()` がコマンドを
   `(セグメント, インライン env)` のリストに分解する
4. 各セグメントをサービスにマッチング。readonly 除外・dedup・context option 抽出
5. `accounts.local.json` を解決して読み、サービスごとに `verify()` を実行
   (キャッシュ hit / 自己修復の切替はスキップ)
6. `core.output.deny()` / `warn()` で整形して stdout に返す

**起動コマンド**: `python3 ${CLAUDE_PLUGIN_ROOT}/hooks/verify-cloud-account`
(ディレクトリを渡すと `__main__.py` が実行される)

Python 3.11+。標準ライブラリのみ (外部依存なし)。

## サービスモジュールの契約

**公開すべき属性・関数の一覧は `services/__init__.py` の冒頭 docstring が正本。**
ここに表を複製すると必ず片方が古くなるので置かない。以下は「なぜその規則か」だけ。

### `verify()` の実装規則

- **成功は `None`、失敗は「理由 + 解決手順」を 1 つの文字列で返す** (deny の
  reason にそのまま出るため、理由だけ返すとユーザーが次の一手を打てない)
- **例外を raise しない。** CLI 未インストール (`FileNotFoundError`)、実行不能
  (`OSError`)、timeout も文字列で返す。hook プロセスが異常終了すると JSON が
  出ず、公式仕様上は non-blocking error として**無音でコマンドが進む**
  (= fail-open)。ガードが消える方向の失敗なので、必ず捕まえて deny 文字列にする
- **subprocess の timeout は `core.budget.call_timeout(<既定>)` を通す。**
  直値を書くと残り予算を無視してフルの timeout を使い、複合コマンドで hook
  timeout を超える (下記「実時間の予算」)
- `expected` の型チェック (str / dict) は各 service 内で行う。dispatcher は
  `isinstance(entry, (str, dict))` だけ弾く (許容する形が service ごとに違うため)
- `env` は**そのまま `subprocess.run(env=)` に渡す**。マージ
  (`{**os.environ, **inline}`) は dispatcher 側で済んでいる
  (service → core の依存を作らないため)。内部の `_run_*` / `_get` にも貫通させる
- `project_dir` を使わないサービスも引数では受け取る (interface 統一)

### 照合規則が動的な service は `matches()` を公開する

`verify()` は deny 理由を組み立てる責務があるので戻り値が str になる。一方
builder の `show` は `[match]` / `[mismatch]` を出すのに bool が要る。**同じ規則を
2 箇所に実装すると必ずずれる。** 実例: gh が GitHub Enterprise を先に列挙する環境
で、`show` は「最初の host」と照合して `[mismatch]`、hook は `github.com` を優先
照合して allow、という乖離が出た (内部バックログ)。

照合先が動的に決まる service は `matches(expected, current) -> bool` を公開し、
`verify()` 側もその規則 (github なら `scalar_target_host()`) を使う。builder は
`matches()` があればそちらに委譲し、無ければ汎用近似に落ちる。

### `PATTERNS` / `READONLY` の規則

- **先頭アンカ `^` 必須。** `PATTERNS` は `re.search` で評価されるため、アンカ
  無しだと `echo "gh"` のような文字列でも発火する。dispatcher は分解後のセグメント
  単位でマッチするので、セグメント先頭のアンカが効く
- **ラッパ対応を PATTERNS に書かない。** `npx` / `mise exec --` 等の剥がしは
  `core.command_parser.strip_transparent_wrappers` に集約済みなので、各 service は
  `^<cli>(?=\s|$)` 相当のシンプルな形でよい。0.1.0 では Firebase だけが PATTERNS に
  wrapper を詰め込んでいたが、他 service にも必要になって破綻した
- **`\b` ではなく空白/終端で区切る。** `\b` はハイフンも語境界とみなすため、
  `gh-ost` / `kubectl-foo` / `aws-vault` のような**別コマンド**まで拾う
- **状態確認コマンドを必ず `READONLY` に入れる。** 入れ忘れると
  「accounts 未設定 → deny → 設定のため現在値を確認したい → deny」の
  デッドロックになる。新規サービス追加時の必須チェック項目
- **マッチ順序**: `dispatcher._match_service` は `services.ALL` を先頭から評価し、
  最初にマッチしたサービスを採用する。2 つのサービスで PATTERNS が競合する設計は
  避ける (曖昧なコマンドは各サービス側で除外するのが正解)

## コマンド分解 (`core.command_parser`)

`extract_candidates(command)` がコマンド文字列を `(候補セグメント, インライン env)`
のリストに変換する。具体的な入出力例は
`tests/test_command_parser.py` が網羅している (表を複製しない)。

規則と理由:

- **quote-aware 分割**: single/double quote・`$()`・backtick の内側にある
  `&&` / `;` / `|` / 改行は区切りにしない (手書き状態マシン)
- **subshell を含む代入は保守的に stop**: `FOO=$(date) cmd` は剥がすと意味が
  変わりうるため剥がさない
- **`env -i` / `env --` / `env -u NAME` は opaque**: 環境を書き換えるので剥がさない
  (= 静的解析対象外 → 検証スキップ)
- **透過 wrapper は「env を素通しする」ものだけ**: `ssh` / `docker` /
  `kubectl exec` のように**別の実行コンテキストへ移送する** wrapper は、ローカルの
  行頭 env が届かないので追加しない。`bash` / `sh` / `eval` / `python -c` も中身が
  script なので剥がさない
- **多段ネスト**: `sudo time mise exec -- firebase deploy` を 1 回の strip で
  解決するため、剥がせなくなるまでループする (上限あり)

wrapper ごとの env 伝播クラス (passthrough / scrub / opaque) の完全な表・実機根拠・
**wrapper を追加するときのチェックリスト**は
[docs/wrapper-env-audit.md](./wrapper-env-audit.md) が正本 (D16)。分類は
`core/command_parser.py` の `_WRAPPER_ENV_CLASS` で宣言し、
`tests/test_command_parser.py` のガードテストが未分類の追加を機械的に落とす。

## 短期キャッシュ (`core.cache`)

PreToolUse は Bash のたびに発火するので、`gh pr list && gh pr view && ...` の
連打で毎回 CLI を呼ぶとレイテンシが積み上がる (`aws sts get-caller-identity` は
1〜3 秒)。**検証成功だけ**を短時間キャッシュする。

- **キー**: サービス名 / project_dir / 期待値 / **インライン env** /
  **context option** (`--profile` 等) の JSON を sha256 したもの。
  構成要素は `cache._cache_key` が正本。
  インライン env と context を入れないと、`AWS_PROFILE=prod` の成功 entry を
  `AWS_PROFILE=other` の実行が hit して未検証のまま allow される
  (`os.environ` 全体は入れない — `PATH` 等で毎回無効化されて意味を失う)
- **成功のみ**: 失敗 (文字列返却) は常に再検証する。切り替え直後に使いたいため
- **無効化**: TTL 超過 / `accounts.local.json` の mtime 変化 / 破損・欠損 /
  アカウント状態を変えうるコマンドの検出 / epoch 不一致
- **書き込み失敗は無視** (best-effort)。キャッシュ書けないことで deny は出さない

意図的にしないこと: 失敗のキャッシュ (切替直後に再検証したい) / 長時間キャッシュ
(他端末での切替が反映されない) / プロセス内キャッシュ (hook は毎回別プロセス)。

並行 hook との競合 (epoch + in-flight 窓) の仕様は README のパフォーマンス節と
`core/cache.py` の docstring を参照。

## 実時間の予算 (`core.budget`)

hook は `hooks/hooks.json` の `timeout` を超えると Claude Code 側で打ち切られ、
**出力が破棄されて tool call がそのまま進む** (公式仕様上の fail-open)。個々の
subprocess timeout は 1 コマンドあたりの上限でしかないので、複合コマンドで
サービスを直列に検証すると合計が hook timeout を超えうる。CLI 未検出も CLI
timeout も deny に倒している以上、ここだけ無音で通るのは判定表の穴になる。

- `dispatch()` が `budget.start()` で締切を置き、`finally` で必ず解除する
  (解除しないと builder 側の subprocess timeout まで黙って縮む)
- 各 service は `budget.call_timeout(<既定>)` で「残り予算」に丸めた timeout を使う
- 予算を使い切ったサービスは **CLI を呼ばずに deny** に集約する。判定は CLI 起動の
  直前に置く — cache hit と自己修復の切替は時間を使わないので通す
- 「予算 + 超過見積り < hook timeout」は `tests/test_budget.py` が `hooks.json` を
  読んで機械的に照合する。**定数を動かすならこのテストが不等式を検算する**

**並列化 (ThreadPoolExecutor) を採らなかった理由**: `concurrent.futures` の worker
は非 daemon スレッドで、インタプリタ終了時に atexit で join される。ハングした
subprocess を抱えたまま「予算切れ」を返してもプロセスが終了できず、結局 hook
timeout に落ちる。fail-open を塞ぐ目的には締切の伝播で足りる。

## 配置パスの解決 (`core.paths`)

3-tier lookup (推奨 / deprecated / legacy) と親ディレクトリ遡及を担当する。
利用者向けの説明は README、判断の背景は下記 D4。

- **複数 tier が同一階層に同居したら fail-closed で deny** (D4)。どれが正本か
  曖昧なまま検証を通すと、どの設定が効いているか不透明になる
- **親遡及**は「worktree に accounts.local.json を複製せず親 repo の設定を継承する」
  ための経路。cwd 階層で 1 つでも見つかればそこで採用 (cwd 優先)
- 遡及の停止条件は **階層数 + git repo の toplevel + `$HOME`**。階層数だけを上限に
  すると `<home>/dev/<org>/<repo>` のような配置で `$HOME` に届き、無関係な設定を
  継承する (しかも verify 成功時は継承注釈が出ないので気付けない)。linked worktree
  の `.git` は**ファイル**なので停止条件にならず、親 repo までは上れる
- **builder は親遡及しない読み方をしない**が、書込先は解決結果に従う
  (「hook が読むファイルを編集する」ため)。対象は出力の `対象:` 行に必ず出る

## サービスを追加する

1. `services/<name>.py` を作り、`services/__init__.py` の docstring にある契約を実装
2. `services/__init__.py` の import と `ALL` に追加する
3. README の対応表と `accounts.local.json` のサンプルに新しいキーを追記
4. `tests/test_services.py` にテストを追加する。最低限:
   一致 / 不一致 / CLI 未インストール / timeout / 状態確認コマンドが readonly
5. `tests/test_budget.py` の service 横断テストが自動で新 service も見るので、
   subprocess の timeout が `budget.call_timeout()` 経由か確認する

動的ディスカバリではなく**明示 import** にしているのは、IDE 補完・型チェッカが
効き、import エラーが沈黙せず surface し、`ALL` への登録漏れがレビューで見えるため。

## hook 登録

plugin として install すれば `hooks/hooks.json` が自動適用される。利用者が
`settings.json` を手で編集する必要はない。

- `matcher: "Bash"` のみ。**`if` フィールドは使わない** — 一部の hook ランナーが
  `if` を無視して全 Bash コマンドに発火し、無関係なコマンドを誤 deny した事故が
  あった。振り分けは常にスクリプト内部 (`dispatcher._match_service`) で行い、
  ランタイム差異に依存しない
- `timeout` は**秒単位** (公式 hooks 仕様)。過去にミリ秒と誤記して 1000 倍の
  スケールずれを起こした実績があるので注意
- hook timeout は内部の実時間予算より数秒長く取る (`core/budget.py` の
  `worst_case_seconds()` との不等式をテストが検算する)

## テスト

```bash
cd hooks/verify-cloud-account
python3 -m unittest discover tests
```

標準ライブラリのみで動く (pytest / `pip install` 不要)。**件数はここに書かない** —
上のコマンドの出力を正とする。クラス・メソッド単位の実行方法は README を参照。

### スモーク (stdin に hook input JSON を流す)

```bash
cd plugins/verify-cloud-account

# 対象外 → 出力なし、exit 0
echo '{"tool_input":{"command":"git status"},"cwd":"/tmp"}' \
  | python3 hooks/verify-cloud-account

# 対象 + accounts 未設定 → deny JSON
echo '{"tool_input":{"command":"gh pr list"},"cwd":"/tmp"}' \
  | python3 hooks/verify-cloud-account

# readonly → 出力なし、exit 0
echo '{"tool_input":{"command":"gh auth status"},"cwd":"/tmp"}' \
  | python3 hooks/verify-cloud-account
```

`VERIFY_CLOUD_ACCOUNT_DEBUG=1` を付けると、セグメント分解・マッチしたサービス・
readonly 判定・cache hit・verify 所要 ms・最終判定を stderr に 1 行 JSON で出す。

### E2E

```bash
claude --plugin-dir plugins/verify-cloud-account
```

実セッションで `gh pr list` / `firebase use` / `kubectl get pod` 等を叩き、
accounts.local.json の有無・一致/不一致・チェーン・ラッパで挙動を確認する。

## 設計判断の履歴

### 初期 (0.1.0〜0.2.0)

- **Python 採用** — bash では正規表現と JSON パースが煩雑で保守性が低い
- **サービスごとのモジュール分離** — CLI・出力パース・エラーメッセージがサービス
  ごとに違い、JSON 設定で汎用化しようとすると破綻する (各 CLI の出力形式が
  標準化されていない)
- **明示 import (動的ディスカバリ不採用)** — 補完とデバッグ性を優先。追加時の
  2 行コストは許容範囲
- **hook を 1 エントリに集約 (`if` 廃止)** — ランナー差異による誤発火事故を受けて、
  振り分けをスクリプト内部へ一元化
- **コマンド分解を dispatcher に集約 (0.2.0)** — service ごとの wrapper 対応が
  露出しきれなくなったため `core.command_parser` を新設
- **`expected` を str | dict に (0.2.0)** — GHE の host 別 / Firebase alias /
  GCP account 対応。dispatcher は型を事前判別せず各 service に委ねる
- **成功のみキャッシュ (0.2.0)** — 失敗もキャッシュすると切替後の即時検証ができない

### 0.3.0 (builder + 配置パス移行)

**D1: builder を唯一の正規経路にする**

`accounts.local.json` の編集は `scripts/accounts_builder.py` 経由に統一する。
書込先パスの固定 (D2) / JSON のインデント・改行・ソート順 / 既存キーの温存 /
CLI 現在値との突合 (show) / 旧パス統合 (D5) / 値表示の制御 (D3) を builder 側で
一元管理するため。Claude は Read / Write / Edit / `cat` で直接触らず、Agent Skill
経由で builder を呼ぶ。

**D2: builder の書込制約 (responsibility confinement)**

builder は `accounts.local.json` 1 ファイル専用の writer。書込対象パスは
`core/paths.py` の定数に集約し、**argv からは指定できない**。basename が
`accounts.local.json` であることを assertion し、テストで「異常な出力パスが
拒否される」ことを固定する (将来の拡張で書込対象が広がらない保証)。

**D3: 値表示の制御**

builder の stdout は Claude が読む。アカウント ID / project 名 / user 名は通常の
確認フローではノイズなので、既定は隠し `--show-values` でのみ出す。Agent Skill は
「値なし dry-run → 必要なら承認を取って `--show-values` → 最終承認後 `--commit`」の
フローを指示する。

**D4: 新旧パス競合時の fail-closed**

2 つ以上の tier にファイルがあれば deny + 手動解決要求。どれが正本か曖昧なまま
検証を通すと、どの設定が効いているか不透明になる。

**D5: builder の migrate サブコマンド**

旧パス → 新パスの統合を builder が提供する。新パス優先で旧パスの追加キーをマージし、
値衝突時は自動マージせず deny + 手動解決要求。`--commit` でも旧ファイルは自動削除
しない (安全側)。

### 0.3.1 (プロジェクト側 signpost の同梱)

**D6: signpost をテンプレートファイルに切り出す** — 文言更新を builder のロジック
変更と分離し、レビュー差分を読みやすくする。

**D7: action に依存しない signpost 生成** — `init --commit` の signpost 生成は
add / unchanged / skipped に依存しない。既に accounts.local.json だけ持っている
ユーザーが後から signpost を入れ直せる経路を残すため。

**D8: dry-run では生成しない** — dry-run と commit の I/O 影響境界を一致させる。

**D9: plugin 同士を疎結合に保つ** — `*.local.json` の読み書き制限は別 plugin の
所掌。こちらは signpost で回避経路を案内するに留め、**相手側の deny 文言や許可
パターンに本 plugin の知識を入れない**。これは marketplace 全体の設計原則。

**D10: signpost は best-effort** — テンプレート読み込みや書き込みが失敗しても
warning 1 行で builder 自体は成功させる。signpost が無くても hook 本体は動くので、
read-only volume や quota 超過でアカウント登録までブロックしない。

### 0.7.0 (インライン env の伝播 + 診断性)

**D11: インライン env を検証 subprocess に伝播する**

`AWS_PROFILE=prod aws ...` のように行頭 env で profile を切り替える運用で、
「剥がすが使わない」非対称のせいでログイン済みでも既定 profile で検証が失敗し
永久 deny していた。

- **全伝播 (allow-list にしない)** — 必要な env キーは service ごとに違い、
  allow-list の漏れが別の永久 deny を生む。hook はコマンド実行の直前なので
  「実行時と同条件で検証」が原則
- **静的解決のみ** — 値に未展開の `$VAR` を含む env は剥がすが伝播しない。
  誤った値を渡すより素の環境の方が安全
- **マージは dispatcher に一元化** — service は受け取った env をそのまま渡す。
  インラインが空なら `env=None` (親環境継承) にする (空 dict は環境変数皆無になる)
- **cache キーに含める** — profile が違えば別キー (上記キャッシュ節)

**D12: deny の出所明示** — `output.deny()` の 1 箇所で全 deny の先頭にこの hook
由来である旨のタグを付ける。CLI 本体の生エラーと誤認され、CLI レベルの切り分けに
時間を浪費するのを防ぐ。

**D13: 情報系コマンドの readonly 化** — 全 service の `--version` / `--help` /
`version` / `help` を READONLY に入れる。診断コマンドが誤検証で deny されるのは
全 service 共通の穴だったため一括対応。

**D14: 検出セグメントの併記** — verify 失敗の deny reason に
`(検出コマンド: ...)` を付け、複合コマンドのどのセグメントが検証を起動したかを
特定できるようにする。

**D15: direnv / `CLAUDE_ENV_FILE` は対応しない** — その env は PreToolUse hook に
渡らない (harness の仕様)。hook 側で `.envrc` を評価する案は技術的には可能だが
採らない: direnv 専用の部分対応は「direnv なら通るが他のツールでは通らない」新しい
非対称を生む / 読み取り検証 hook が任意コードを実行する責務拡大になる / 外部ツール
依存が増える。回避策 (インライン env [D11] / settings.json の env / 起動時 env) を
README に明記する方針。

### 0.7.2 (透過 wrapper × env の監査)

**D16: 透過 wrapper の env 伝播クラスを分類し、ガードで固定する**

D11 は「静的に解析した行頭 env = 実行時 env」を前提にするが、透過 wrapper を跨ぐと
この前提が崩れ、env の edge case を連続して生んだ。全数監査の結論は
「現在のリストは健全、検証ロジックは無変更」。再設計はしない (過剰な allow-list 化は
誤 deny を増やす) 代わりに、分類を `_WRAPPER_ENV_CLASS` で宣言し、テストで将来の
wrapper 追加時に分類を機械的に強制する。完全な表・実機根拠・追加チェックリストは
[docs/wrapper-env-audit.md](./wrapper-env-audit.md)。

### 0.12.0 (docs と実装の整合 + 予算)

**D17: キー未記載サービスは deny (fail-closed) で確定させる**

「そのプロジェクトで使うサービスなのにキーが無い」は「検証しなくてよい」ではなく
「期待値を宣言し忘れている」状態で、素通しするとこの plugin が防ぐはずの事故
(別アカウントでの write) がそのまま通る。README と deny 文面はかつて「未記載の
サービスは検証対象外 (= allow)」と逆を約束していたので、**実装 (deny) を正として
文面を揃え**、キー追加の具体コマンドを deny に載せた。

案内が `init` ではなく `set` なのは、キー未記載の deny が「accounts.local.json は
見つかっている」ときにしか出ないため。そのファイルが親から継承されている場合
`init` は「継承中の設定を覆い隠す」として拒否するが、`set` は継承元を直接編集する
のでどちらの階層でも通る。

**D18: 判定規則は service 側の 1 実装に寄せる** — 上記「照合規則が動的な service は
`matches()` を公開する」を参照。

**D19: 遡及の停止条件に境界を足す** — 上記「配置パスの解決」を参照。落とす方向
(見つからず deny) は fail-closed なので安全側。`$HOME` にグローバル既定を置きたい
場合は、この遡及ではなく専用の経路を用意すべき、という切り分け。

**D20: 予算切れは deny** — 上記「実時間の予算」を参照。

## 既知の制限

利用者から見える制限は README の「既知の制限」が正本。実装者向けに補足すると、
`bash -c` / `eval` / `python -c` / subshell の内側は静的解析できず**検証対象外
(= allow)** になる。これは「解析できないものを deny すると誤 deny が爆発する」
判断で、透過 wrapper に足さないことで表現している。

## リリース手順

1. `.claude-plugin/plugin.json` の `version` を semver で bump
   (挙動変更・機能追加なら minor、修正のみなら patch)
2. `CHANGELOG.md` を更新する
3. `claude plugin validate <plugin dir>` で warning 0 を確認
4. `python3 -m unittest discover hooks/verify-cloud-account/tests` で全 green
5. commit / tag / push

`version` は plugin.json 側にだけ書く (marketplace entry には書かない)。両方に
書くと plugin.json が警告なく勝ち、marketplace 側で bump したつもりが反映されない。
