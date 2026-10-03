---
name: auto-switch
description: |
  verify-cloud-account の自動切替 (auto-switch) を有効化・無効化する。有効にすると、
  gh のアカウント不一致で deny する代わりに、hook がログイン済みの期待アカウントへ
  `gh auth switch` してからコマンドを通す。切替は同じマシンの全ターミナル・
  セッションの gh に効くため、有効化・無効化はユーザーが明示的に頼んだときだけ行う。
  deny への対処を聞かれただけなら、アカウントの切替か期待値の見直しを案内し、
  自動切替は持ち出さない (止まるのが頻繁で困っているとユーザーが言ったときは、
  副作用を添えて選択肢として挙げてよい)。範囲 (このプロジェクト / 全プロジェクト) を
  AskUserQuestion で確認し、
  プロジェクト単位は builder の `auto-switch` サブコマンドで、全プロジェクトは
  `~/.claude/settings.json` の `env` で設定する。
  Use when: ユーザーが gh アカウントの自動切り替えを有効 / 無効にしたいと明示した、
  gh のアカウント不一致で毎回止まるのをやめたいと言った場合。
  Triggers: "自動切り替えして", "自動切り替えを有効にして", "自動切替をオン",
  "自動切替をオフ", "自動切り替えをやめて", "gh のアカウントを自動で切り替えて",
  "gh の切替で止まるのをやめたい", "auto-switch", "VERIFY_CLOUD_ACCOUNT_AUTO_SWITCH",
  "/verify-cloud-account:auto-switch", "enable auto-switch", "disable auto-switch",
  "switch gh account automatically"
allowed-tools:
  - Bash(gh auth status)
  - Bash(python3 "${CLAUDE_PLUGIN_ROOT}/hooks/verify-cloud-account/scripts/accounts_builder.py" show)
  - Bash(python3 "${CLAUDE_PLUGIN_ROOT}/hooks/verify-cloud-account/scripts/accounts_builder.py" auto-switch --enable --dry-run)
  - Bash(python3 "${CLAUDE_PLUGIN_ROOT}/hooks/verify-cloud-account/scripts/accounts_builder.py" auto-switch --disable --dry-run)
  - AskUserQuestion
metadata:
  author: mao
  version: "0.17.0"
---

<!--
allowed-tools は「確認なしで使える」付与で、制限ではない。マシン全体に効く設定を扱う
skill なので、確認なしにするのは読み取り (gh auth status) と、builder の読み取り専用の
呼び出し (show と auto-switch の --dry-run) を引数まで書いた形だけに絞る。書き込む
--commit、値を表示する --show-values、--path を付けた形、~/.claude/settings.json の
編集は通常の権限確認を通す。
-->

# auto-switch

verify-cloud-account の自動切替 (auto-switch) を有効化・無効化するスキル。
切り替える条件の全体と既知の制限は plugin の `README.md` の「自動切替」節
(`${CLAUDE_PLUGIN_ROOT}/README.md`) が正本。

## 自動切替で起きること (ユーザーに説明する内容)

- enforce で **deny になる gh の不一致に限り**、hook が期待アカウントへ
  `gh auth switch` し、もう一度検証して一致したらコマンドを通す。切り替えた事実は
  `[verify-cloud-account] 自動切替 (auto-switch): ...` という文面で伝わる。
  読むだけのコマンドの警告や、warn / off モードでは切り替えない
- **切替はマシン全体に効く**。gh の設定は同じマシンの全ターミナル・全セッションで
  共有されるので、並行して動いている作業の gh も切替後のアカウントで動く
- 切り替えられるのは **gh にログイン済み**のアカウントだけ (対話が要るログインは
  自動化しない)。トークン用の環境変数 (`GH_TOKEN` 等) や `GH_HOST` があるときも
  切り替えない
- 直前 60 秒以内に別のセッションが同じ host を別のアカウントへ自動切替していたら、
  切り替えずに従来どおり deny する (並行セッションで切替を奪い合わないため)
- 対応しているのは github だけ。aws / gcloud / firebase は各 CLI の公式の仕組みで
  プロジェクトごとに固定できる (切り替えではなく、そのリポジトリでは最初から正しい
  アカウントで動く) — `/verify-cloud-account:project-accounts` の手順に従う

## 守ること

- **有効化・無効化はユーザーが明示的に頼んだときだけ行う。** deny への対処を
  聞かれただけなら、アカウントの切替か期待値の見直しを案内し、自動切替は持ち出さない。
  止まるのが頻繁で困っているとユーザーが言ったときは、副作用 (マシン全体の gh に効く)
  を添えて選択肢として挙げてよい
- accounts.local.json は Read / Write / Edit / Bash(cat) で直接触らない。
  プロジェクト単位の設定は builder の `auto-switch` サブコマンドで書く
- **commit される `.claude/settings.json` (プロジェクトの共有設定) には書かない。**
  自動切替は各自のマシン全体に効く個人の選好で、共有設定に入れると他の開発者にも効く
- `"$mode"` / `"$readonly"` はこのスキルの対象外 (検証を弱めるキーなので builder も
  書かない。変えたいと言われたら手編集か環境変数を案内する)

## 実行フロー

1. **有効化か無効化かを確かめる。** 依頼から読めなければ `AskUserQuestion` で聞く。
   無効化なら step 6 へ。gh 以外 (aws / gcloud / firebase) の自動切替を頼まれたら、
   その分は `/verify-cloud-account:project-accounts` の手順で進める

2. **前提を確かめる** (どちらも読み取りだけ)。次の 2 つを**別々の Bash 呼び出しで、
   書いてあるとおりに**実行する (`;` / `&&` で連結したり `2>&1` を足したりすると、
   確認なしで通す `allowed-tools` の形から外れて権限確認が出る):

   ```bash
   gh auth status
   ```

   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/hooks/verify-cloud-account/scripts/accounts_builder.py" show
   ```

   - show に `github:` の行があるか。無ければ期待値が未設定なので、先に
     `/verify-cloud-account:accounts-init` で github を設定する
   - **切替先 (github の期待値) が gh にログイン済みか**を確かめる。show は既定で値を
     隠すので、このままでは `gh auth status` のどのアカウントが切替先か分からない。
     `AskUserQuestion` で「期待値を表示して、ログイン済みのアカウントと照合しますか?」
     と聞く (step 3 の範囲の質問と同じ呼び出しにまとめてよい):
     - `値を表示して照合する (Recommended)` → 次を実行し、期待値のアカウントが
       `gh auth status` の該当 host に出ているかを確かめる (Active でなくてよい。
       グローバル既定で検証している場合は `--path <グローバル既定のパス>` も付ける)

       ```bash
       python3 "${CLAUDE_PLUGIN_ROOT}/hooks/verify-cloud-account/scripts/accounts_builder.py" show --service github --show-values
       ```

     - `表示せずに進める` → `gh auth status` に出ているアカウントの一覧だけを示し、
       期待アカウントが含まれているかはユーザーに確かめてもらう

     期待アカウントがログインしていなければ、ユーザーに `! gh auth login` で一度
     ログインしてもらう (対話が要るので Claude は実行しない)。ログインしないまま
     有効にしても、hook は切り替えずに従来どおり deny し、その理由を添える
   - show が `no accounts.local.json found at ...` と「グローバル既定 ... が存在します
     (hook はこのファイルで検証します)」を出したら、このプロジェクトは自前のファイルを
     持たずグローバル既定で検証されている。step 3 では「このプロジェクトだけ」の
     代わりに次の 3 つから選んでもらう (builder が拒否時に出す案内と同じ):
     - `グローバル既定のファイルで有効にする (Recommended)` — step 4 の builder に
       `--path <グローバル既定のパス>` を付ける。自前のファイルを持たず、グローバル
       既定で検証している全プロジェクトに効く。再起動は不要
     - `全プロジェクト` — step 5 (環境変数。自前のファイルを持つプロジェクトにも効く)
     - `このプロジェクト専用のファイルを作る` — `/verify-cloud-account:accounts-init`
       で作ってから step 4。グローバル既定の値は継承されない (書かなかった service は
       未設定の扱い。書込は deny、リモート read のみのコマンドは警告) ので、init の警告に
       従って必要な値を入れる

3. **範囲を `AskUserQuestion` で確かめる。** 質問文に「切替はこのマシンの全ターミナル・
   セッションの gh に効く」ことを一文で添える:
   - `このプロジェクトだけ (Recommended)` — accounts.local.json に
     `"$auto_switch": ["github"]` を builder で書く。次の gh コマンドから効く
     (再起動は不要)
   - `全プロジェクト` — `~/.claude/settings.json` の `env` に
     `"VERIFY_CLOUD_ACCOUNT_AUTO_SWITCH": "github"` を足す。保存した時点で、
     このマシンで起動中の Claude Code セッションにも反映される
   - `やめる`

4. **このプロジェクトだけ** の場合、dry-run で変更内容を確かめてから書く
   (1 つずつ、書いてあるとおりに実行する):

   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/hooks/verify-cloud-account/scripts/accounts_builder.py" auto-switch --enable --dry-run
   ```

   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/hooks/verify-cloud-account/scripts/accounts_builder.py" auto-switch --enable --commit
   ```

   - stdout 先頭の `対象:` 行をユーザーに伝える。「祖先ディレクトリ ... から継承」
     なら書き込むのは親のファイルで、worktree などからも同じ設定が効く
   - `error:` + exit 1 で「... がありません」と出たら、accounts.local.json がまだ
     無い。stderr の案内 (init で作る / `--path <グローバル既定のパス>` / 環境変数) の
     どれにするかをユーザーに確かめる。グローバル既定の案内が出た場合は step 2 の
     3 択と同じ
   - 「環境変数 VERIFY_CLOUD_ACCOUNT_AUTO_SWITCH=... が設定されています」と出たら、
     環境変数がファイルより優先される。値 (特に `'off'`) をユーザーに伝え、
     どちらを残すか確かめる
   - 「期待値が未設定です」「期待値の形が不正です」と出たら、このままでは自動切替は
     働かない。`/verify-cloud-account:accounts-init` (未設定) か
     `/verify-cloud-account:accounts-show` (不正な値の確認) で期待値を直す

5. **全プロジェクト** の場合:
   - `~/.claude/settings.json` を読み、**既存の `env` オブジェクトにキーを 1 つ
     足す** (`env` が無ければ `env` ごと足す)。ファイル全体を書き直したり、他の
     キーを消したりしない。ファイルが無ければ
     `{"env": {"VERIFY_CLOUD_ACCOUNT_AUTO_SWITCH": "github"}}` で作る
   - 書く前に、足す 1 行と置き場所をユーザーに見せて承認を得る
   - 書いた後、「保存した時点で、起動中のセッションも含めて効く (再起動は不要)」と
     伝える
   - プロジェクトの設定 (`.claude/settings.local.json` / `.claude/settings.json`) の
     `env` に同じキーがあると、そのプロジェクトではそちらが優先される (step 6)。
     このプロジェクトにあれば、その値もユーザーに伝える

6. **無効化**。環境変数は accounts.local.json の `"$auto_switch"` より優先される。
   環境変数を Claude Code の設定の `env` で指定した場合、同じキーはプロジェクトの設定
   (`.claude/settings.local.json` / `.claude/settings.json`) が `~/.claude/settings.json`
   より優先される。先に、どこで有効になっているかを確かめる:
   - `~/.claude/settings.json` と、このプロジェクトの `.claude/settings.local.json` /
     `.claude/settings.json` の `env` に `VERIFY_CLOUD_ACCOUNT_AUTO_SWITCH` があるか
     (読めなければユーザーに確かめてもらう)
   - accounts.local.json の `"$auto_switch"` は、service で絞り込まない `show` に
     `[auto-switch: github]` と出るかで分かる。自前のファイルを持たずグローバル既定で
     検証しているプロジェクトでは、`show` と下の `--disable` に
     `--path <グローバル既定のパス>` を付ける

   そのうえで、どれを変えるかをユーザーに確かめてから変える:
   - このプロジェクトの accounts.local.json: `auto-switch --disable --dry-run` で
     確かめてから `--commit` (有効な service が残らなければ `"$auto_switch"` のキーごと
     消える。再起動は不要)
   - このプロジェクトの `.claude/settings.local.json`: `env` から
     `VERIFY_CLOUD_ACCOUNT_AUTO_SWITCH` を消す。commit される `.claude/settings.json` に
     書かれていたら、共有設定なので変えるかどうかはユーザーに任せる
   - 全プロジェクト向けの有効化を取り消す: `~/.claude/settings.json` の `env` から消す。
     **各プロジェクトの `"$auto_switch"` と、プロジェクトの設定の `env` は残る**
   - どこでも無効にする: `~/.claude/settings.json` の `env` で値を `"off"` にする
     (キーが無ければ足す)。`"$auto_switch"` を書いたプロジェクトも無効になるが、
     プロジェクトの設定の `env` に同じキーがあるプロジェクトではそちらが優先されるので、
     そこも消すか `"off"` にする
   - 依頼が「全部やめて」なら `"off"`、「全プロジェクト向けの設定だけ外して」なら削除。
     どちらか読めなければ `AskUserQuestion` で確かめる。設定ファイルを書く前に、
     変える 1 行をユーザーに見せて承認を得る (step 5 と同じ)
   - 設定ファイルの `env` は、値を変える (`"off"` にする) と保存した時点で起動中の
     セッションにも反映される。**キーを消しても起動中のセッションには残り、再起動する
     まで自動切替は有効なまま**なので、消した場合は再起動をユーザーに伝える (すぐ
     止めたいなら、消すより `"off"` にする)

7. **効いているかの確かめ方をユーザーに伝える**: 次に gh の不一致が起きたとき、
   コマンドが止まらずに通り、`[verify-cloud-account] 自動切替 (auto-switch): ...`
   が出れば効いている。プロジェクト単位の設定は `/verify-cloud-account:accounts-show`
   (service で絞り込まない show) に `[auto-switch: github]` と出る

## エラーハンドリング

- **builder が exit 1 で「旧パスに accounts.local.json が存在します」** →
  `/verify-cloud-account:accounts-migrate` で新パスへ統合してから再実行する
- **既存 JSON が壊れている** → stderr に「手動で修正してから再実行」と出る。
  ユーザーに原因を伝え、手動修正を依頼する (Claude は JSON を書き換えない)
- **`~/.claude/settings.json` の編集が拒否された** (権限確認で断られた、別の hook に
  止められた等) → 無理に別の経路で書かない。足す 1 行を示し、ユーザー自身に
  追記してもらう
