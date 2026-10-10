---
name: project-accounts
description: |
  aws / gcloud / firebase のアカウント (profile / project) をプロジェクトごとに固定し、
  そのリポジトリでは最初から正しいアカウントで動くようにする。VCA は切り替えず、
  各 CLI の公式の仕組みを使う: aws は AWS_PROFILE、gcloud は
  CLOUDSDK_ACTIVE_CONFIG_NAME (一致する構成が無ければ CLOUDSDK_CORE_PROJECT /
  CLOUDSDK_CORE_ACCOUNT) を .claude/settings.local.json の env に書き (リポジトリの
  全 worktree に効く)、firebase は firebase use をそのディレクトリで 1 回実行する。
  固定に使う値は builder の pin-env で期待値から出し、書く前にユーザーの承認を得る。
  gh はこの方法では固定できないので auto-switch skill を使う。
  Use when: プロジェクトやディレクトリごとにアカウントを自動で使い分けたい、
  このリポジトリでは aws / gcloud / firebase を特定のアカウントで使いたいと言われた場合。
  Triggers: "プロジェクトごとにアカウントを切り替えたい", "プロジェクトごとにアカウントを固定",
  "ディレクトリごとにアカウントを自動で切り替え", "このリポジトリでは aws を",
  "AWS_PROFILE をプロジェクトごとに", "gcloud の構成をプロジェクトごとに",
  "firebase のプロジェクトをディレクトリごとに", "aws も自動で切り替えて",
  "gcloud も自動切り替え", "/verify-cloud-account:project-accounts",
  "pin account per project", "per-project AWS profile"
allowed-tools:
  - Bash(python3 "${CLAUDE_PLUGIN_ROOT}/hooks/verify-cloud-account/scripts/accounts_builder.py" pin-env)
  - Bash(python3 "${CLAUDE_PLUGIN_ROOT}/hooks/verify-cloud-account/scripts/accounts_builder.py" pin-env --service aws)
  - Bash(python3 "${CLAUDE_PLUGIN_ROOT}/hooks/verify-cloud-account/scripts/accounts_builder.py" pin-env --service gcloud)
  - Bash(python3 "${CLAUDE_PLUGIN_ROOT}/hooks/verify-cloud-account/scripts/accounts_builder.py" pin-env --service firebase)
  - Bash(python3 "${CLAUDE_PLUGIN_ROOT}/hooks/verify-cloud-account/scripts/accounts_builder.py" show)
  - AskUserQuestion
metadata:
  author: mao
  version: "0.19.0"
---

<!--
allowed-tools は「確認なしで使える」付与で、制限ではない。確認なしにするのは
読み取り専用の builder の呼び出し (pin-env と show) を、引数まで書いた形だけにする
(`*` を付けたルールは、期待値を書き換えるサブコマンドや --show-values まで通す)。
--show-values / --path を付けた形、--service を 2 つ以上付けた形、
.claude/settings.local.json の編集、firebase use は通常の権限確認を通す。
-->

# project-accounts

aws / gcloud / firebase のアカウントを、各 CLI の公式の仕組みでプロジェクトごとに
固定するスキル。VCA は切り替えず、固定に使う値を期待値 (accounts.local.json) から
出して、照合を続ける。全体と既知の制限は plugin の `README.md` の
「プロジェクトごとにアカウントを固定する」節 (`${CLAUDE_PLUGIN_ROOT}/README.md`) が正本。

## できること (ユーザーに説明する内容)

- 「切り替える」のではなく、**そのリポジトリでは最初から正しいアカウントで動く**
  ようにする。マシン全体の状態は変えないので、並行して動いている別のリポジトリの
  作業には影響しない
- 置き場所は `.claude/settings.local.json` の `env`。Claude Code はこのファイルを
  リポジトリのルートから読み、worktree からも main checkout のファイルを使う。
  commit されない個人設定
- 固定の単位は**リポジトリ** (サブディレクトリや 1 つの worktree だけ別のアカウント、
  はできない)。firebase だけはディレクトリ単位 (firebase-tools が作業ディレクトリ
  ごとに記録する)
- 値を足す・変えると、保存した時点で起動中のセッションにも反映される。**キーを
  消しても起動中のセッションには残り、再起動するまで有効なまま**
- 優先順位: コマンドの行頭に付けた値 (`AWS_PROFILE=x aws ...`) > settings の `env` >
  ターミナルで export した値
- セッション中に `/cd` で別のリポジトリへ移ると、前のリポジトリの `env` が残る
  (書いていないキーは前の値のまま)。使う CLI のキーはリポジトリごとに全部書いて
  おく。書き漏れても、VCA の照合で期待値と違えば止まる
- VCA は照合を続ける。固定した値が期待値と違えば、従来どおり deny する
- 期待値を登録していない aws / gcloud / firebase も、この方法で固定されていれば VCA は
  止めない (v0.19.0)。期待値を登録せず固定だけで運用したいと言われたら、固定する値
  (profile 名・アカウント・project) はユーザーに聞く (推測しない)。pin-env は期待値から
  値を出すので、この場合は使えない
- 固定の値は、ディレクトリ単位で環境変数を切り替えるツール (mise・direnv など) で入れても
  よい。その場合も、hook に届くのは Claude Code を起動したときの値なので、そのリポジトリで
  Claude Code を起動するよう伝える (起動したリポジトリの外で走るコマンドは止まる)

## 守ること

- 書くのは `.claude/settings.local.json` だけ。**commit される `.claude/settings.json`
  には書かない** (個人の選好が他の開発者にも効く)
- `~/.aws/config`、gcloud の設定ファイル、accounts.local.json は Read / Bash(cat) で
  直接読まない。固定に使う値は builder の `pin-env` から取る (出すのは profile 名・
  構成名・alias 名だけで、期待値は既定で隠れる)
- 書く前に、足す行と書き込み先をユーザーに見せて承認を得る。既存のキー
  (`permissions` など) と `env` の他の値は残し、`env` にマージする
- aws の profile や gcloud の構成の候補が複数あるときは、`AskUserQuestion` で選んで
  もらう (aws は role の権限が違いうる)。候補が無いときは推測しない
- gh はこの方法では固定できない (プロジェクトごとに安全に分けられる公式の方法が
  無い)。gh を頼まれたら `/verify-cloud-account:auto-switch` を案内する

## 実行フロー

1. **対象の CLI を確かめる。** 依頼から読めなければ `AskUserQuestion` で聞く
   (aws / gcloud / firebase)。gh が含まれていれば、その分は auto-switch skill に回す

2. **固定に使う値を出す** (読み取り専用。書いてあるとおりに実行する):

   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/hooks/verify-cloud-account/scripts/accounts_builder.py" pin-env
   ```

   特定の CLI だけなら `--service aws` のように付ける (繰り返し指定可)。出力の見方:

   - `error:` + exit 1 で「... がありません」 → 期待値が無い。先に
     `/verify-cloud-account:accounts-init` で設定する。グローバル既定で検証している
     と案内されたら `--path <グローバル既定のパス>` を付けて再実行する
   - `書き込み先: 決められません — <理由>` → 理由をユーザーに伝え、どのファイルに
     書くかを確かめる (推測しない)
   - `固定できません: <理由>` → その service は固定しない。理由を伝える (aws の
     profile が見つからなければ、ユーザーに profile を作ってもらうか名前を聞く)
   - `(value hidden. use --show-values to reveal)` の行 (gcloud の
     `CLOUDSDK_CORE_*` / firebase の project ID) は、`AskUserQuestion` で「値を表示して
     固定する値を確かめますか?」と聞き、承認されたら `--show-values` を付けて再実行する
   - `- ` で始まる行は注意点。ユーザーに伝える (gcloud は構成名で固定する方が
     速い理由、firebase は worktree ごとに 1 回要ることがある、など)

3. **候補を選んでもらう。** `候補 "a", "b" (1 つ選ぶ)` の行 (名前はクォート付き。env の断片の
   `<上の候補から 1 つ>` は目印で、名前はこの候補の行から選んでもらう) があれば `AskUserQuestion` で
   選んでもらう。値の行に `(表示しない値)` が出たら、固定する値は env の断片 (JSON) の値。その値で
   固定してよいかを `AskUserQuestion` で確かめる

4. **env を書く** (aws / gcloud):
   - 書き込み先のファイルを読み、`env` にキーを足す。ファイル全体を書き直したり、
     他のキーを消したりしない。ファイルが無ければ `{"env": {...}}` で作る
   - 書く前に、足す行と書き込み先をユーザーに見せて承認を得る
   - 書けなかった (権限確認で断られた、別の hook に止められた等) ときは、別の経路で
     書こうとしない。足す行をユーザーに示し、自分で追記してもらう

5. **firebase**: 出力の `このディレクトリで 1 回実行: firebase use <alias または project ID>`
   を、そのまま単独で実行する (他のコマンドと連結しない。VCA は期待値への切替として
   通す)。pin-env が「案内できる期待値の project ID は N 個あり」と注記したときは、
   実行する前に、案内された ID (`--show-values` で確かめる) で固定してよいかを
   `AskUserQuestion` で確かめる。ユーザーが別の ID を選んだら、Claude は `firebase use` を
   組み立てて実行しない (その ID は pin-env の形と行き先の確認を通っていない)。ユーザーに
   手で実行してもらう

6. **反映を確かめる** (必須):
   - pin-env をもう一度実行し、`現在: このセッション=` に足した値が出ているかを見る。
     出ていなければ反映されていない — 再起動か、ワークスペースの trust を確かめる
     よう伝えて、ここで止める
   - `/verify-cloud-account:accounts-show` (service で絞り込まない show) で、固定した
     CLI が一致になっているかを確かめる。aws は SSO のトークンが切れていれば
     `aws sso login --profile <profile>` が要る (ログインはユーザーが行う)

7. **ユーザーに伝える**: 効く範囲 (このリポジトリの全 worktree)、保存した時点で
   起動中のセッションにも効くこと、キーを消すときは再起動が要ること、`/cd` で
   移ったときの注意

## エラーハンドリング

- **builder が exit 1 で「旧パスに accounts.local.json が存在します」「複数のパス」** →
  `/verify-cloud-account:accounts-migrate` で統合してから再実行する
- **pin-env の `注意: 書き込み先の JSON として読めません` / `JSON の最上位がオブジェクト
  ではありません` / `"env" がオブジェクトではありません`** → Claude は直さない
  (ファイルを書き直さない)。ユーザーに手で直してもらってから書き足す
- **pin-env の `固定できません: gcloud の期待値の形が不正です`** → 期待値の project /
  account に文字列でない値や空白だけの値がある。`/verify-cloud-account:accounts-show` で
  確かめ、builder の `set` で直してから再実行する (片方だけで固定しても検証は通らない)
- **pin-env の `固定できません: ... の前後に空白があります`** → 期待値の前後に空白がある
  (検証は完全一致なので、どの現在値とも一致しない)。builder の `set` で空白を除いた値に
  直してから再実行する
- **pin-env の `固定できません: .firebaserc を firebase-tools と同じ内容に読めると確かめられません`** →
  `.firebaserc` が括弧の中の条件 (UTF-8 でない・U+FEFF がある・`//` か `/*` がある (文字列の
  中の URL なども含む)・JSON として読めない・`projects` がオブジェクトでないか文字列でない値を
  持つ、など。網羅ではない) に当たる。firebase-tools とは読み方が違いうるので、`firebase use` の行き先を
  確かめられない。`.firebaserc` はリポジトリのファイルなので Claude は直さない。理由を
  ユーザーに伝え、これらに当たらない形に直すかはユーザーに任せる (直したら再実行する。URL の
  ように消せない内容なら、この方法では firebase を固定できない)
- **固定したのに deny される** → 固定した値が期待値と違う (別のアカウントの profile /
  構成を選んだ) か、ログインが切れている。show の結果をユーザーに伝える
