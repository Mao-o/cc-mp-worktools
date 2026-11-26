# cloud-account-guard

Bash コマンド実行の直前に、クラウド CLI
(`gh` / `firebase` / `aws` / `gcloud` / `kubectl`) の
**アクティブアカウントがプロジェクトの想定値と一致するか** を検証する hook。
複数アカウントを切り替えて作業するときに、間違ったアカウントで
`gh pr create` / `firebase deploy` / `aws s3 ...` / `gcloud run deploy` /
`kubectl apply` 等を実行する事故を防ぐ。

不一致なら `permissionDecision: deny` で停止し、切り替えコマンドを提示する。
**止めるのは書込系だけ**で、リモートを読むだけのコマンド (`gh pr list` /
`aws s3 ls` 等) は警告を添えて通す ([検証の 3 tier](#検証の-3-tier--v0140))。
opt-in の[自動切替](#自動切替-auto-switch--v0160)を有効にすると、gh の不一致は
止める代わりに hook がログイン済みの期待アカウントへ切り替えて通す。

aws / gcloud / firebase のように CLI 自身にディレクトリ単位でアカウントを固定する
公式の仕組みがあるものは、期待値を登録しなくても、その仕組みで固定されていれば
通す ([期待値の代わりにディレクトリ単位の固定を使う](#期待値の代わりにディレクトリ単位の固定を使う--v0190))。
起動したリポジトリの外で走るコマンドは照合できないので止める
([起動したリポジトリの外での実行](#起動したリポジトリの外での実行--v0190))。

## インストール

```bash
/plugin marketplace add Mao-o/cc-mp-worktools
/plugin install cloud-account-guard@mao-worktools
```

有効化すると `PreToolUse:Bash` hook が自動登録される。
`settings.json` を手で編集する必要はない。

開発時はローカルパスを直接ロードする方が速い:

```bash
claude --plugin-dir /path/to/cc-mp-worktools/plugins/cloud-account-guard
```

## 初回セットアップ

accounts.local.json の作成・更新は **builder スクリプト経由の Agent Skill**
を使う。builder は書込パスの固定・JSON フォーマットの一貫化・既存キーの
温存を一元管理するため、動作の安定やフォーマット統一の観点から手動書込より
一貫した結果になる:

```
/cloud-account-guard:accounts-init
```

または自然言語でも発火する (例: 「accounts.local.json を作りたい」)。
Agent Skill は Claude の description トリガから自発的にロードされ、以下を
自動で行う:

1. 各 service (github / firebase / aws / gcloud / kubectl) の現在値を CLI から取得
2. `.claude/cloud-account-guard/accounts.local.json` に書き込む **提案**を生成
3. 値を表示する前に AskUserQuestion で確認
4. 承認後に `--commit` でファイル書き込み

配置パスは **`.claude/cloud-account-guard/accounts.local.json`** (v0.3.0 から)。
旧パス (`.claude/accounts.local.json` / `.claude/accounts.json`) は
deprecation 案内付きで後方互換 — ただし新旧両方存在する場合は fail-closed で
deny する (`/cloud-account-guard:accounts-migrate` で統合)。

builder の `init --commit` / `migrate --commit` は同ディレクトリに
`CLAUDE.md` (Claude 向け signpost) も自動生成する (v0.3.1 から)。
`*.local.json` のような機密ファイルパターンへの直接アクセスを制限する
セキュリティ系 hook が有効な環境で、Claude (LLM) からの直接アクセスが deny
された場合でも、同ディレクトリの CLAUDE.md を覗くだけで builder 経由の正規
経路に辿り着けるようにするための案内ファイル。既存 CLAUDE.md は上書きされず、
削除しても plugin 本体の動作には影響しない。生成先が git で ignore されていない
ときは作らない (v0.24.0 から。`.claude/` 配下の CLAUDE.md は Claude Code が読み込む
ため、リポジトリにコミットされるとこの plugin を使っていない人のセッションにも
混ざる)。作りたい場合は `.git/info/exclude` などで ignore してから `init --commit` などの書き込みを再実行する。
git の外では従来どおり作る。

## accounts.local.json の形式

```json
{
  "github":   "your-github-user",
  "firebase": "my-project-id",
  "aws":      "123456789012",
  "gcloud":   "my-gcp-project",
  "kubectl":  "prod-cluster"
}
```

**そのプロジェクトで使う service のキーは全て書く必要がある** (fail-closed)。
キーが無い service のコマンドは「検証対象外 (= allow)」ではなく **deny** される。
期待値が宣言されていない状態で通すと、この plugin が防ぐはずの「別アカウントでの
書き込み」をそのまま素通しすることになるため。そのプロジェクトで一度も CLI を
叩かない service はキーを書かなくてよい (発火しないので deny も起きない)。
例外は、aws / gcloud / firebase をディレクトリ単位の公式の仕組みで固定している場合で、
キーが無くても通す (v0.19.0。[期待値の代わりにディレクトリ単位の固定を使う](#期待値の代わりにディレクトリ単位の固定を使う--v0190))。

キーが無いまま対象コマンドを叩くと、deny 文面にキー追加コマンドが出る:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/hooks/cloud-account-guard/scripts/accounts_builder.py" \
  set --service github --from-cli --dry-run
```

`init` ではなく `set` を案内するのは、accounts.local.json を親から継承している
階層では `init` が「継承中の設定を覆い隠す」として exit 2 で拒否されるのに対し、
`set` は継承元を直接編集してどちらの階層でも通るため
(→ [builder も同じ解決を使う](#builder-も同じ解決を使う))。

`--commit` ではなく `--dry-run` を案内するのは、`--from-cli` が
**現在ログイン中のアカウント**を期待値として提案するため。間違ったアカウントに
入ったまま commit すると、この plugin が防ぐはずの状態をそのまま正解として
焼き付けてしまう。提案値が意図したアカウントであることを確認してから
`--commit` に変えること。

### `.gitignore`

プロジェクトでは `accounts.local.json` を git 管理外にする。builder の `--commit` は
`.gitignore` が既にあればそこへ追記するが、`*.local.json` などのパターン・
`.git/info/exclude`・グローバルの excludesFile で既に ignore されているときは
`.gitignore` を変えない (v0.24.0 から。判定は `git check-ignore`):

```gitignore
# .gitignore
.claude/cloud-account-guard/accounts.local.json
# (旧パス残置している間も)
.claude/accounts.local.json
```

## 検証モード (enforce / warn / off) — v0.13.0

deny を一時的に止める手段 (escape hatch)。従来は `/plugin disable` か
`accounts.local.json` の書き換えしかなく、user scope で install した直後は
設定を置いていない**全プロジェクト**で deny が始まっていた。

| mode | 挙動 |
|---|---|
| `enforce` (既定) | 不一致・キー未記載 (ディレクトリ単位の固定が無いとき)・未設定・パス競合・起動リポジトリの外での実行を deny (リモート read だけの QUERY tier は警告して通す。[検証の 3 tier](#検証の-3-tier--v0140)) |
| `warn` | 検証は走らせ、deny 相当の内容を `additionalContext` (Claude への通知) で伝えるだけ |
| `off` | 検証そのものを行わない (CLI も起動しない。検証の対象外のクラウド CLI の知らせ (v0.31.0) だけは出る) |

**判定表 (何を問題とみなすか) は mode で変わらない**。変わるのは「止めるか /
伝えるだけか / 見ないか」だけ。

指定方法は 2 つあり、**環境変数が優先**される:

```jsonc
// .claude/settings.json (セッション全体に効かせる)
{ "env": { "CLOUD_ACCOUNT_GUARD_MODE": "warn" } }
```

```json
// accounts.local.json (プロジェクト単位)
{
  "$mode": "warn",
  "github": "your-github-user"
}
```

- 解決順: `CLOUD_ACCOUNT_GUARD_MODE` → `accounts.local.json` の `"$mode"` → `enforce`
- `"$mode"` は予約キー (`$` 始まりなので service キーと衝突しない)。
  [グローバル既定](#グローバル既定-v0130) のファイルに書いた `"$mode"` が効くのは
  **`accounts.local.json` を持たないプロジェクト**だけ。自前の設定があるプロジェクトは
  グローバル既定を一切読まないので、そちらは `CLOUD_ACCOUNT_GUARD_MODE` か
  **そのプロジェクトの `"$mode"`** を使う
- **`"$mode"` は `accounts.local.json` を読めたときだけ効く**。未設定 / JSON 破損
  (UTF-8 でない・入れ子が深すぎるものを含む。v0.18.0) / 複数パス競合の deny は
  ファイルを読む前に確定するため、そこを `warn` / `off` に弱められるのは
  `CLOUD_ACCOUNT_GUARD_MODE` のみ
- `"$mode"` は **builder が値を書かない予約キー** (`"$readonly"` も同じ)。検証を
  弱めるキーなので、hook に見張られている Claude がスクリプトで切れないようにして
  いる。`init` / `set` / `remove` / `migrate` は既存の値を壊さず保持するが、設定・
  変更は**エディタで手編集**する。現在値は `/cloud-account-guard:accounts-show` が
  `[mode]` として表示する
- 不正な値 (`CLOUD_ACCOUNT_GUARD_MODE=yes` 等) は **enforce として扱い**、
  deny 文面にその旨を添える (黙って無視すると「off にしたのに deny される」の
  原因が分からなくなるため)。`off` で検証しない場合も、env の不正値は
  `additionalContext` で通知する (綴り間違いが黙って無視されないように)
- `off` でも**アカウント切替コマンドの検出による cache 破棄は行う**
  (off の間の切替が cache に残って、enforce に戻した直後に古い成功で通る事故を防ぐ)

`warn` / `off` は検証を弱める設定なので、**常用するなら
`accounts.local.json` を整えて `enforce` に戻すこと**を前提にしている。

## 自動切替 (auto-switch) — v0.16.0

gh のアカウント不一致で deny する代わりに、**hook が期待アカウントへ
`gh auth switch` してからコマンドを通す** opt-in 機能。複数アカウントを頻繁に
行き来する運用で、不一致のたびに「deny → 案内された切替を単独で実行 → 元の
コマンドを打ち直す」往復で止まるのを無くす。

**既定は無効**。切替は gh の設定 (`hosts.yml`) を書き換えるので、**同じマシンの
他のターミナル・Claude Code セッションの gh もそのアカウントで動くようになる**。
この副作用を受け入れる場合だけ有効にする。

Claude Code に「gh の自動切り替えを有効にして」のように頼むと、
`/cloud-account-guard:auto-switch` skill (v0.16.1) が副作用を説明し、範囲
(このプロジェクト / 全プロジェクト) を確かめてから設定する。自分で設定する場合、
指定方法は 2 つあり、**環境変数が優先**される:

```jsonc
// ~/.claude/settings.json (全プロジェクト)
// または .claude/settings.local.json (このプロジェクトだけ。commit しない個人設定)
{ "env": { "CLOUD_ACCOUNT_GUARD_AUTO_SWITCH": "github" } }
```

```json
// accounts.local.json (プロジェクト単位)
{
  "$auto_switch": ["github"],
  "github": "your-github-user"
}
```

commit される `.claude/settings.json` (プロジェクトの共有設定) には書かない。自動切替は
各自のマシン全体に効く個人の選好で、共有設定に入れると他の開発者にも効いてしまう。

- 解決順: `CLOUD_ACCOUNT_GUARD_AUTO_SWITCH` → `accounts.local.json` の
  `"$auto_switch"` → 無効。env の `off` はファイルの指定も含めて無効にする。env を
  消しただけでは、各プロジェクトの `"$auto_switch"` は有効なまま残る
- env を Claude Code の設定の `env` で指定した場合、同じキーはプロジェクトの設定
  (`.claude/settings.local.json` / `.claude/settings.json`) が `~/.claude/settings.json`
  より優先される (Claude Code の設定の優先順位)。`~/.claude/settings.json` の `off` で
  まとめて止めても、プロジェクトの設定に `github` を書いたプロジェクトでは有効なまま
- settings.json / settings.local.json の `env` は、値を足す・変える (有効化・`off`) と
  保存した時点で起動中のセッションにも反映される。**キーを消しても起動中のセッション
  には残り、再起動するまで有効なまま** (Claude Code の仕様)。すぐ止めたいなら、消す
  より `off` にする。`accounts.local.json` の `"$auto_switch"` は hook が毎回読むので、
  足しても消しても次の gh コマンドから効く
- 値は **service 名の並び** (env はカンマ区切り)。対応しているのは現状 **`github`
  のみ**。`true` のような一括指定は受け付けない (対応 service を増やしたときに
  既存の設定が黙って広がらないようにするため)。aws / gcloud / firebase は CLI の
  公式の仕組みで[プロジェクトごとに固定](#プロジェクトごとにアカウントを固定する-公式の方法--v0170)
  できるので、自動切替は足さない
- 不正な値・対応していない service 名は**無効として扱い** (= 従来どおり deny)、
  deny 文面にその旨を添える。env に綴り間違いがあってもファイル側の指定には
  落とさない (「env で止めたつもりが切り替わる」を作らない)
- `"$auto_switch"` は builder の `auto-switch` サブコマンド (v0.16.1) で書ける
  (予約キーのうち builder が書くのはこれだけ。保護を弱めない — 切替先は期待値の
  アカウントに限られ、切り替えた後に再検証する)。手編集してもよい。
  `/cloud-account-guard:accounts-show` が `[auto-switch: github]` と表示する:

  ```bash
  # 有効化 (--dry-run で変更内容を確かめてから --commit)
  python3 "${CLAUDE_PLUGIN_ROOT}/hooks/cloud-account-guard/scripts/accounts_builder.py" \
    auto-switch --enable --commit
  # 無効化 (有効な service が残らなければキーごと消す)
  python3 "${CLAUDE_PLUGIN_ROOT}/hooks/cloud-account-guard/scripts/accounts_builder.py" \
    auto-switch --disable --commit
  ```

  対象は `set` と同じく hook が読むファイル (親ディレクトリ遡及込み)。hook は
  このファイルを毎回読むので、再起動しなくても次の gh コマンドから効く。
  accounts.local.json がまだ無いときは作らずに拒否する (`"$auto_switch"` だけの
  ファイルは[グローバル既定](#グローバル既定-v0130)を覆い隠し、期待値を書いていない
  service がすべて未設定の扱い (書込は deny、リモート read のみのコマンドは警告) になるため)
- [グローバル既定](#グローバル既定-v0130) のファイルに書いた `"$auto_switch"` が
  効くのは、自前の `accounts.local.json` を持たないプロジェクトだけ (`"$mode"` と
  同じ)。全プロジェクトで有効にするなら環境変数を使う

**切り替えるのは次をすべて満たすときだけ**:

| 条件 | 満たさないとき |
|---|---|
| mode が `enforce` で、その不一致が **deny になる** (WRITE tier。`"$readonly": "deny"` なら QUERY も) | 従来どおり。QUERY の不一致は警告して通す (読むだけのために他の作業の gh を動かさない)。`warn` / `off` では切り替えない |
| 期待アカウントが **gh にログイン済み** (`gh auth status` に非アクティブのアカウントとして出ている) | deny + 「ログイン済みアカウントが見つかりません」の注記。ブラウザ認証が要るログインは自動化しない |
| 期待値が dict 形式なら、**全 host** が切り替えられる | どの host も切り替えない (一部だけ切り替えて deny、を作らない) |
| `GH_TOKEN` / `GITHUB_TOKEN` / `GH_ENTERPRISE_TOKEN` / `GITHUB_ENTERPRISE_TOKEN` / `GH_HOST` が無い | 切り替えない (トークン env は `hosts.yml` より優先されるので切り替えても実行アカウントが変わらない / 切り替える host を決められない)。行頭の代入の値がコマンド置換・バッククォートなどで読めない形 (`GH_TOKEN=$(gh auth token --user x) gh pr create`) は、その gh を照合もしない (0.29.9 と同じ。v0.29.10)。値が変数参照の形 (`GH_TOKEN=$T gh ...` / `GH_TOKEN="$(cmd)" gh ...`) は、値を検証の env に入れないのでこの条件に当たらない (0.29.9 から) |
| コマンド自身がアカウント状態を変える操作 (`gh auth refresh` 等) を含まない | 切り替えない (明示的な操作に hook の切替を重ねない) |
| 対象のコマンドのうち `--help` の付かないものが 1 つ以上ある (すべてが `--help` 付き (`gh pr create --draft --help` など) ではない) (v0.21.0) | 切り替えない (ヘルプの表示のためにマシン全体の gh を動かさない)。deny は従来どおりで、切り替えなかった理由を添える。`-h` はヘルプとして数えない (gh の `auth` 配下では `--hostname` の短い形)。0.30.0 から、[サブコマンドの後ろの `--help`](#検証をスキップする-readonly-コマンド) の多く (`gh pr create --help`) はそもそも照合しないので、ここに来るのは直前が option の形や、グループの直後の語が組み込みの子でない形 (`gh pr ship --help`。alias でありうる) などだけ |
| 直前 60 秒以内に、同じ host を**別のアカウントへ**自動切替した記録が無い | 切り替えない (下の並行セッションのガード) |
| 1 コマンド分の[検証時間の予算](#検証時間の予算-v0120)が残っている | 切り替えない |

[切替と書込の連結](#切替と書込を同一コマンドに連結した形は-deny--v0150) の deny は
従来どおり先に決まる (切替後の状態は検証できないため)。

切り替えるときは:

- 切り替える**前に**成功 cache を破棄する (他のプロジェクトの成功 cache が、切替後の
  状態で TTL 分通ってしまうのを防ぐ)
- 切り替えた**後にもう一度検証**し、一致したときだけ通す
- 切り替えた事実を `additionalContext` で Claude に伝える
  (例: `github.com: work → your-github-user`)。同じコマンド行の別 service が deny
  された場合も、deny 文面に添える。あわせて、これが利用者の有効にした機能の想定どおりの
  動作であり、謝罪や元のアカウントへ戻す提案はせず切り替えたことを 1 行で伝えるよう
  Claude に指示する (v0.22.0。「他のセッションの gh も切り替わる」という事実だけを渡すと、
  事故として扱われることがあったため)
- 切替に失敗したら従来どおり deny し、失敗理由を添える
- 所要時間: 切り替える場面でだけ、`gh auth status` (CLI。ネットワーク往復込みで
  〜500ms) を 1 回と、host ごとに `gh auth switch` (ローカルの設定と keyring の書換で、
  gh 2.101.0 の実測で約 50ms) を呼ぶ。いずれも検証時間の予算の内側で動き、CLI を
  起動する前ごとに予算の残りを確かめる

**並行セッションのガード**: 別のアカウントを期待する 2 つのセッションが同時に
動いていると、片方の hook が通したコマンドが実行される前にもう片方が切り替え、
**通したコマンドが別アカウントで動きうる** (この plugin が防ぐ事故そのもの)。
そのため、直前 60 秒以内に同じ host を別のアカウントへ自動切替した記録があれば、
切り替えずに deny する。deny 文面には「どのプロジェクトが何秒前に切り替えたか」と
「切り替える前にユーザーに確認する」旨が出る。同じアカウントへの切替 (同じ repo の
worktree 同士など) では見送らない。記録は
`$TMPDIR/cc-mp-cloud-account-guard/github.autoswitch.json`。記録を読めないとき
(壊れている・stat できない) と、この dir を使えないとき ([成功 cache と同じ条件](#パフォーマンス-短期キャッシュ)) は、
記録が無いのと同じでガードは働かない。

使う前に:

- 使うアカウントそれぞれで一度 `gh auth login` しておく (gh 2.40 以上は 1 つの host に
  複数のアカウントを登録できる)。`gh auth status` に期待アカウントが出れば切替先になれる
- 別アカウントで作業するセッションを同時に動かすなら、ガードで deny になる場面が
  残る ([既知の制限](#既知の制限))

## プロジェクトごとにアカウントを固定する (公式の方法) — v0.17.0

プロジェクトごとに別のアカウントを使う運用では、切り替えるより、**そのリポジトリでは
最初から正しいアカウントで動く**ように固定する方が安全で手間も少ない。マシン全体の
状態を変えないので、並行して動いている別のリポジトリの作業にも影響しない。
aws / gcloud / firebase には CLI 自身に公式の仕組みがあるので、VCA は独自に切り替えず、
その仕組みを案内する (照合はこれまでどおり続ける)。

| CLI | 公式の仕組み | 書く場所 | 効く範囲 |
|---|---|---|---|
| aws | `AWS_PROFILE` | `.claude/settings.local.json` の `env` | そのリポジトリ (全 worktree 共通) |
| gcloud | `CLOUDSDK_ACTIVE_CONFIG_NAME` (名前付き構成)。一致する構成が無ければ `CLOUDSDK_CORE_PROJECT` / `CLOUDSDK_CORE_ACCOUNT` | 同上 | 同上 |
| kubectl | `KUBECONFIG` (そのリポジトリ用の kubeconfig。skill と `pin-env` は未対応なので手で書く) | 同上 | 同上 |
| firebase | `firebase use <alias>` (アカウントは `firebase login:use <email>`) | firebase-tools が project root (firebase.json のあるディレクトリ。`--config` があればそのファイルのディレクトリ) ごとに記録 | その project root |
| gh | プロジェクトごとに安全に分けられる公式の方法が無い | — | [自動切替](#自動切替-auto-switch--v0160) (マシン全体) を使う |

```jsonc
// <リポジトリのルート>/.claude/settings.local.json (commit しない個人設定)
{
  "env": {
    "AWS_PROFILE": "dev-admin",
    "CLOUDSDK_ACTIVE_CONFIG_NAME": "work"
  }
}
```

Claude Code に「このリポジトリでは aws を dev-admin で使いたい」「プロジェクトごとに
アカウントを自動で使い分けたい」のように頼むと、`/cloud-account-guard:project-accounts`
skill が期待値から固定する値を出し (builder の `pin-env`。読み取り専用で、
settings.local.json は書かない)、書く前に確認してから設定する。

- **書き込み先**: Claude Code は `.claude/settings.local.json` を git リポジトリの
  ルートから読み、worktree からも main checkout のルートのファイルを使う (Claude Code
  の docs。`claude -p` で実測)。1 回書けば全 worktree に効く。git の外・ルートが
  ホームディレクトリ・Windows などでは起動したディレクトリのファイルを読むので、
  `pin-env` は書き込み先を決めずに理由を出す
- **単位はリポジトリ**。サブディレクトリや 1 つの worktree だけ別のアカウント、は
  できない (firebase を除く)
- commit される `.claude/settings.json` には書かない (個人の選好が他の開発者にも効く)
- **優先順位**: コマンドの行頭に付けた値 (`AWS_PROFILE=x aws ...`) > settings の
  `env` > ターミナルで export した値
- **反映**: 値を足す・変えると、保存した時点で起動中のセッションにも反映される。
  キーを消しても起動中のセッションには残り、再起動するまで有効なまま (Claude Code の
  仕様)
- **`/cd` で別のリポジトリへ移ると、前のリポジトリの `env` が残る** (書いていない
  キーは前の値のまま)。使う CLI のキーはリポジトリごとに全部書いておく。書き漏れても、
  VCA の照合で期待値と違えば止まる
- **gcloud は構成名でも `CLOUDSDK_CORE_PROJECT` / `CLOUDSDK_CORE_ACCOUNT` でも固定できる**。
  どちらも VCA は設定ファイルと環境変数から直接読む (0.18.0 までは `CLOUDSDK_CORE_*` が
  あると検証のたびに `gcloud config get-value` を起動していた。v0.19.0 から、この 2 つは
  gcloud と同じく設定ファイルより優先して読む)。account まで固定するなら、その account は
  ログイン済み (`gcloud auth list` に出る) であること
- **aws**: 同じアカウントに profile が複数あると role (権限) が違いうるので、どれに
  するかは自分で選ぶ。固定してもログインはしない (SSO のトークンが切れていれば
  `aws sso login --profile <profile>`)
- **firebase**: project を選ぶ環境変数が無い (公式 docs に記載なし)。`firebase use`
  の記録は project root (firebase.json のあるディレクトリ) ごとなので、別の worktree では最初に 1 回要ることがある。
  firebase.json の無いサブディレクトリで実行すると親の project root に記録され、兄弟のディレクトリにも効く。
  VCA は firebase のアカウントは検証しない。`firebase use` は引数を `.firebaserc` の
  alias として先に解決する。`pin-env` は、scalar の期待値には project ID を案内する (v0.25.0。
  hook の自己回復が期待値そのものしか通さないため)。dict の期待値には、このディレクトリの
  `.firebaserc` で期待した project に解決される alias を案内し、無ければ期待値の project ID で
  案内する (v0.17.1)。project ID で案内できるものが複数あるときは、確認を通った ID の
  `firebase use` を候補のコマンドとして全部出し (値は既定で隠す。`--show-values` で出る)、
  どれにするかをユーザーに選んでもらう (v0.26.0)。
  `.firebaserc` を firebase-tools と同じ内容に読めると確かめられないとき (UTF-8 でない・
  大きすぎる (256 KiB を超える)・U+FEFF がある・`//` か `/*` がある (文字列の中の URL なども
  含む)・JSON として読めない
  (`NaN` など JSON に無い値・構文の誤り・深い入れ子)・`projects` がオブジェクトでないか
  文字列でない値を持つ、など) は、firebase-tools と同じ行き先を確かめられないので「固定
  できません」と出す (v0.17.1)。判定は保守的で、URL を含むだけの厳密な JSON も対象になる。
  並べた条件は網羅ではない (Python が読めない桁の多すぎる整数など、版で変わるものは並べない)
- 前後に空白のある期待値は固定できない (照合は完全一致なので、どの現在値とも一致しない)。
  `pin-env` は「固定できません」と出す (v0.17.1)
- 検証の成功 cache は、アカウントを決める環境変数の値もキーに含める (v0.17.0)。
  固定した値を変えた直後でも、前の値での成功で通らない
- 対話セッションでの workspace trust との関係は実測していない (Claude Code の docs は、
  プロジェクトの `env` をワークスペースを trust した後に適用すると書いている)

## 期待値の代わりにディレクトリ単位の固定を使う — v0.19.0

accounts.local.json に service のキーが無いとき、0.18.0 までは一律に deny していた。
v0.19.0 から、その CLI に**ディレクトリ単位でアカウントを固定する公式の仕組み**があり、
それで固定されていれば、期待値が無くても照合せずに通す。固定の値そのものが、その
ディレクトリで意図したアカウントの宣言になるため。固定されていなければ、固定の方法と
期待値の登録の両方を案内して deny する。

| CLI | 固定済みとみなす条件 | 未登録・未固定のとき |
|---|---|---|
| gcloud | 環境変数 `CLOUDSDK_CORE_ACCOUNT` が空でない、または `CLOUDSDK_ACTIVE_CONFIG_NAME` が account を持つ構成を指している (project は問わない。project が要るコマンドは gcloud 自身が未設定をエラーにする) | deny。固定の方法と期待値の登録を案内 |
| aws | 環境変数 `AWS_PROFILE` が空でない (認証情報そのものを置く `AWS_ACCESS_KEY_ID` は固定とみなさない) | 同上 |
| firebase | そのディレクトリで `firebase use` の切替先が記録されている (commit される `.firebaserc` の `default` だけでは固定とみなさない)。`--config` / `-c` 付きのコマンドは、そのファイルのあるディレクトリの記録で判定する (v0.29.4。firebase-tools がそこを project root にするため。ファイルが見つからないときは走るディレクトリの記録で判定する)。`--config` が起動したリポジトリの外を指すときも、その先の記録で判定する | 同上。案内した `firebase use <x>` は未登録の間も止めない |
| gh / kubectl | (固定の公式の仕組みが無い) | 従来どおり deny。期待値の登録を案内 |

- 判定に使うのは **hook が見ている環境変数** (Claude Code を起動したときの値 +
  `.claude/settings.local.json` の `env`) だけで、どこで設定したかは問わない (ディレクトリ
  単位で環境変数を切り替えるツール (mise・direnv など) で起動前に入った値も含む)。
  **どのディレクトリでも入っている値 (シェルの起動時に export したもの) も固定とみなす**
  ので、そういう値を置いている場合は期待値を登録して照合させる
- コマンドの中でその場だけ指定した値は固定とみなさない: 行頭のインライン env
  (`AWS_PROFILE=x aws ...`。起動時の値と同じなら可。値を静的に読めない代入
  (`AWS_PROFILE=$(cmd) aws ...` / `AWS_PROFILE=$VAR aws ...` / 追記 `AWS_PROFILE+=x`) は、値によらず
  固定とみなさない。v0.30.3)、同じコマンドの前段の `export` /
  `declare -x` / `unset` / 代入、内容を読めない `source` / `.` / `eval` / `set` の後。
  これを固定とみなすと、未登録の deny を行頭の 1 語で抜けられる (その値でアカウントが
  決まるのに、何とも照合しない)
- 固定は、そのコマンドが走りうる**すべての**ディレクトリで確かめる (v0.29.3)。
  `cd a && firebase deploy; cd ../b && firebase deploy` は a と b の両方で `firebase use` の記録が
  要る。括弧・条件・パイプの中の `cd` があるコマンド (`(cd sub && npm test); firebase deploy`) は、
  その `cd` が後続に効かない場合もあるので、移動前と移動後の両方で確かめる。`cd` より後ろにだけ
  パイプ・`||`・背景実行・波括弧・制御構文がある形 (`cd sub && firebase deploy | tee log`) は移動後
  だけで確かめる。外部プログラムとして走る `cd` (`nohup cd sub` / `env cd sub` など。シェルは移動
  しない) も、移動前と移動後の両方で確かめる。環境変数で固定する aws / gcloud は、ディレクトリに
  よらず固定とみなす。作業ディレクトリを
  変える `sudo` の option (`-D` / `--chdir` / `-R` / `--chroot` / `-i` / `--login`) の中で走る
  コマンドは、どのディレクトリで走るかを確かめないので固定とみなさない (環境変数で固定する
  aws / gcloud も同じ)
- 期待値を登録してある service は、固定されていても従来どおり照合する (ずれていれば止める)。
  固定した環境変数が期待値と違うとき、gcloud の deny は `gcloud config set` ではなく、
  環境変数の直し方と `--account` / `--project` を案内する (環境変数が設定ファイルより
  優先されるので、`config set` では直らない)
- 固定の状況は `/cloud-account-guard:accounts-show` で確かめられる。期待値が未登録の
  service ごとに「固定済み (通す) / 未固定 (止める) / 固定の仕組みが無い (止める)」を出す
  (値は出さない)。判定は show を実行した Bash の環境変数で行う。hook は Claude を起動した
  ときの環境変数で判定するので、セッション中にディレクトリ単位の環境変数が変わった後は
  食い違うことがある

## 起動したリポジトリの外での実行 — v0.19.0

hook は、**Claude Code を起動したときの環境変数** (+ settings の `env`) と、起動した
ディレクトリの accounts.local.json で照合する。ディレクトリ単位で環境変数を切り替える
ツールの値は、Claude Code の Bash には (SessionStart / CwdChanged hook などで) 入るが、
PreToolUse hook には届かない ([direnv / CLAUDE_ENV_FILE 経由の env は届かない](#direnv--claude_env_file-経由の-env-は届かない))。
そのため別のリポジトリで走るコマンドは、そのリポジトリで意図されたアカウントを照合
できない。v0.19.0 から、検証対象のコマンドが**起動したリポジトリ (起動ディレクトリを
含む git の作業ツリー。git の外なら起動ディレクトリ) の外で走る**ときは、照合せずに
止める (リモート read のみなら警告だけで通す)。

- 走るディレクトリは、Bash の作業ディレクトリ (hook input の `cwd`) と、コマンド中の
  `cd <パス>` / `pushd <パス>` から静的に求める。`cd ../other && gcloud ...` は外、
  `cd sub && gcloud ...` (起動リポジトリの中) は内
- 行き先を静的に決められない `cd` (`cd "$D"` / `cd -` / `popd` / glob など) の後は外として
  扱う。subshell の括弧 (`(cd x && ...)`) は分解の時点で落ちるので、括弧の後のコマンドも
  移動先で走るものとして扱う (止める側に倒れる)。条件 (`false && cd x;` / `if ...; then cd x; fi`)
  やパイプ (`cd x | cat`) の中の `cd` も同じ。これが止める側に倒れるのは内外の判定だけで、
  kubectl の相対パスの `--kubeconfig` / `KUBECONFIG` を**どのファイルに解決するか**には使えない
  (実際には走らない `cd` の移動先のファイルで照合してしまう)。そのため、引用符の外に括弧・波括弧・
  `|` / `||`・背景実行の `&`・backtick・制御構文の語 (`if` / `for` / `case` 等) がある、または
  `&&` の右の `cd` の後に `;` / 改行で次のコマンドが続くコマンドでは、相対パスの kubeconfig を
  決められないとして止める (リモート read のみなら警告。v0.28.0)。`cd a; cd b && kubectl ...` の
  ように `&&` と `;` だけで繋いだ `cd` は移動先で解決する。firebase の相対パスの `--config` も
  同じ規則で解決する (v0.29.3)。外部プログラムとして走る `cd` (`nohup cd sub` / `env cd sub` /
  `sudo cd sub` / `/usr/bin/cd sub`、zsh の `command cd sub`) があるコマンドも止める (シェルは移動
  しない。v0.29.3)
- 行き先を静的に決められない `cd` (`cd "$D"` / `cd -` / `popd`、zsh の `chdir`、`CDPATH` がある
  ときの相対パスの `cd sub`) の後のコマンドは、起動リポジトリの外かもしれないとして止める。
  `CDPATH` は環境に export されたものと `CDPATH=x cd sub` の前置きを見る (v0.29.3)
- 作業ディレクトリを変える `sudo` の option (`-D` / `--chdir` / `-R` / `--chroot` / `-i` /
  `--login`) の中で走るコマンドは、相対パスの kubeconfig / `--config` をどのファイルに解決するかを
  決められないとして止める (リモート read のみなら警告。v0.29.3。0.29.2 までは起動側のファイルで
  照合した)。内外の判定は変えない (移動先は見ない)
- 別のリポジトリで作業するときは、そのリポジトリで Claude Code を起動し直す。Subagent に
  任せても、hook と Bash の環境変数は変わらない
- 起動リポジトリの中でも、サブディレクトリごとに環境変数を切り替えている場合の差は
  検出しない

## Agent Skill

| skill | 用途 |
|---|---|
| `/cloud-account-guard:accounts-init` | 新規プロジェクトで accounts.local.json を対話生成 |
| `/cloud-account-guard:accounts-show` | 既存値と CLI 現在値の diff を表示 |
| `/cloud-account-guard:accounts-migrate` | 旧パスから新パスへの統合 |
| `/cloud-account-guard:auto-switch` | [自動切替](#自動切替-auto-switch--v0160)の有効化 / 無効化 (頼まれたときだけ使い、Claude からは勧めない) |
| `/cloud-account-guard:project-accounts` | aws / gcloud / firebase の[プロジェクトごとの固定](#プロジェクトごとにアカウントを固定する-公式の方法--v0170) (公式の仕組みを使う) |

各 skill は description のトリガから Claude が自発的にロードする。明示的に
呼び出したいときは `/cloud-account-guard:<skill-name>` を使う。

builder (`scripts/accounts_builder.py`) を直接呼ぶことも可能:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/hooks/cloud-account-guard/scripts/accounts_builder.py" \
  init --service github --commit
```

ただし Agent Skill の方が dry-run → AskUserQuestion 承認 → commit の確認
フローを含むため、通常はこちらを使う。

### 既存値の更新・削除 (`set` / `remove`)

`init` は既存キーを上書きしない (異なる値なら `skipped` して終了する)。
値を切り替えたい・削除したいときは `set` / `remove` サブコマンドを使う:

```bash
# 既存値を上書き (無ければ新規追加)。--from-cli で CLI 現在値を使うことも可能
python3 "${CLAUDE_PLUGIN_ROOT}/hooks/cloud-account-guard/scripts/accounts_builder.py" \
  set --service github --value new-user --commit

# dict 値 (GHE の hostname / Firebase の alias 等) の特定キーだけを追加・上書き
python3 "${CLAUDE_PLUGIN_ROOT}/hooks/cloud-account-guard/scripts/accounts_builder.py" \
  set --service github --host ghe.example.com --value your-corp-user --commit

# キー全体を削除
python3 "${CLAUDE_PLUGIN_ROOT}/hooks/cloud-account-guard/scripts/accounts_builder.py" \
  remove --service github --commit

# dict 値の特定キーだけを削除 (最後の 1 つを消すとキー自体が削除される —
# 空オブジェクトを残すと該当 service が「オブジェクトが空です」で永久 deny
# されるため。最後の 1 つでなくても、残りが該当 service にとって使えない
# 形になる場合 (例: gcloud で project/account 以外のキーだけが残る) は
# 同じくキー自体を削除し、理由を出力する)
python3 "${CLAUDE_PLUGIN_ROOT}/hooks/cloud-account-guard/scripts/accounts_builder.py" \
  remove --service github --host ghe.example.com --commit
```

`{"github": null}` のように値が壊れている (dispatcher が設定なしと同じ扱いで
必ず deny する) entry も、`--host` を付けずに `remove --service <svc> --commit`
すればキーごと削除できる。

`--host` は既存値がオブジェクト (または未設定) のときだけ使える。既存値が
文字列の service に `--host` を付けるとエラーになる (先に `remove` で削除
するか `--host` を外して上書きする)。`--host` と `--from-cli` は併用できない
(CLI の現在値がどの host/alias のものかを builder は判別できないため、
`--host` 指定時は `--value` で値を明示する)。`--value` は `init` と同じく JSON
object 文字列 (例: `'{"project":"p","account":"a"}'`) を渡すと dict として
保存し、それ以外は生文字列として保存する。dict を受け付けない service
(aws / kubectl) にオブジェクトを渡す、gcloud に未対応キーを渡す、といった
形式不正は `--commit` 前に検出して exit 1 にする (accounts.local.json への
書込は行われない)。いずれも既定では stdout に値を出さず、`--show-values`
明示時のみ表示する (D3 と同じ)。

対象ファイルは **hook が実際に読むもの**に揃う (3-tier lookup + 親ディレクトリ
遡及)。解決したパスは dry-run / commit の出力先頭に `対象: <パス>` として出る。
worktree などで親から継承している場合は親側のファイルが編集され、この階層専用の
設定を作りたいときだけ `--path <file>` で明示する (詳細は
[builder も同じ解決を使う](#builder-も同じ解決を使う))。

## 対象コマンドと検証スキップ

### 検証の 3 tier — v0.14.0

セグメントは 3 tier に分かれ、不一致 (および期待値の未設定) のときの扱いが変わる。

| tier | 何が入るか | 不一致・未設定のとき |
|---|---|---|
| **READONLY** | ローカルの状態確認 / 情報系 / 認証取得系 (`gh auth status` / `firebase use` / `aws sso login` / `--version`) | 検証しない (常に通す) |
| **QUERY** | リモートを**読むだけ** (`gh pr list` / `gh api` の GET / `aws s3 ls` / `aws ec2 describe-*` / `gcloud ... list` / `kubectl get` / `firebase projects:list`) | **通す** + `additionalContext` で「現在=X 期待=Y」を警告 |
| **WRITE** (既定) | それ以外 (`gh pr create` / `firebase deploy` / `aws s3 rm` / `kubectl apply` / `gcloud run deploy`) | **deny** |

さらに **認証情報を出力するコマンド / オプション (DISCLOSING)** は、READONLY /
QUERY に当たっていても取り消して WRITE として扱う (= 不一致なら deny)。
**この表が DISCLOSING の正本**で、「リモートの機密そのものを返す read」も含む:

| サービス | DISCLOSING な形 |
|---|---|
| GitHub | `gh auth status --show-token` (`-t`) / `gh auth list --show-token` / `gh auth token` |
| Kubernetes | `kubectl config view --raw` / `--flatten` / `kubectl cluster-info dump` / `kubectl get secret(s)` にオプション (`-o` / `--output` / `--template`) を付けた形 |
| AWS | `aws configure get aws_secret_access_key` (`aws_session_token`) / `aws configure export-credentials` / `aws sts get-session-token` / `get-federation-token` / `aws secretsmanager get-secret-value` (`batch-get-secret-value`) / `aws ssm get-parameter*` の `--with-decryption` 付き / `aws kms decrypt` / `aws ecr get-login-password` (`ecr-public` も) / `aws eks get-token` / `aws codeartifact get-authorization-token` |
| GCP | `gcloud auth print-access-token` / `print-identity-token` / `gcloud auth application-default print-access-token` |
| Firebase | `firebase login:ci` (CI 用トークンを出力する) |

- 一致していれば通るので、開示コマンドが恒久的に使えなくなるわけではない
  (「**期待したアカウントで**認証情報を見る」ことだけが許される)
- 値の真偽は見ない。`--show-token=false` のような明示 false も「書かれている」
  として扱う (厳格側)
- `kubectl config view --flatten` は `--raw` 無しでも token / `client-key-data` を
  平文で出す (kubectl v1.34.1 で実測。オプション無し / `--minify` だけなら
  `REDACTED` / `DATA+OMITTED`)。`--flatten` は「そのまま認証に使える kubeconfig を
  作る」オプションなので、`--raw` と同格に扱う
- Secret の `data` は base64 エンコードだけで実質平文なので、`kubectl get secret -o
  yaml` は「リモートの secret を出力する read」として扱う。名前一覧
  (`kubectl get secrets`) と `kubectl describe secret` は値を出さないので QUERY のまま

**READONLY に載っている形でも、想定していないオプションが付いていたら QUERY に
降格**する (= 検証は走るが、不一致でも止めない)。例: `gh auth status --json` /
`aws configure import --csv ...`。「コマンド名が安全」ではなくオプションまで含めて
安全と言える形だけを素通しする設計で、想定外のオプションは **deny せず警告に倒す**。

#### 切替と書込を同一コマンドに連結した形は deny — v0.15.0

hook は**コマンド実行前に 1 回だけ**動くため、`gh auth switch --user other &&
gh pr create` のように「切替 → 同じ service の書込」を 1 行に連結すると、書込は
**切替前**の状態で検証される。切替前が期待値どおりなら allow され、切替後 (期待外)
のアカウントで書込が走っていた。v0.15.0 でこの連結形を deny する:

| 同一コマンド内の形 | 判定 |
|---|---|
| **期待値以外**への切替 → 同 service の WRITE (`gh auth switch --user other && gh pr create`) | **deny** (v0.15.0 で追加) |
| 切替先が静的に判らない切替 → 同 service の WRITE (引数なしの `gh auth switch` / `--user $VAR` / `gh auth login` / `aws sso login`) | **deny** (判らない = 検証できない側に倒す) |
| **期待値**への切替 → 同 service の WRITE (`gh auth switch --user <期待> && gh pr create`) | 従来どおり (切替前の状態で通常検証) |
| **アカウントを変えない**設定変更 → 同 service の WRITE (`kubectl config set-context --current --namespace=foo && kubectl apply ...`) | 従来どおり (下記) |
| 切替 → QUERY / READONLY (`gh auth switch --user other && gh pr list`) | 従来どおり (deny しない) |
| 切替 → **別 service** の WRITE (`gh auth switch --user other && aws s3 cp ...`) | 従来どおり (切替の影響を受けない) |
| 書込 → 切替 (`gh pr create && gh auth switch --user other`) | 従来どおり (書込は検証済みの状態で走る) |

- **対処は「期待値へ切り替える操作を単独で実行してから、書込をもう一度打つ」**。切替を
  検出した時点で成功キャッシュは破棄されるので、次の書込は切替後の状態で検証される。
  deny 文面は期待値そのものを載せる (検出コマンドの切替先ではなく)
  期待値に表示できない値 (空白・絵文字などを含む名前) があるときは、`(表示しない値)` と出し、出所だけを言う文
  (「表示していない期待値があります (accounts.local.json の "<key>" を確認してください)」) を添える
- **deny は現在のアカウントに依らない** (切替後の状態は原理的に検証できないため)。
  判定に CLI を呼ばないので検証時間の予算も使わない
- **「アカウントを変えない設定変更」は切替として数えない。** 成功キャッシュの破棄は
  これらでも行うが (過剰でも再検証 1 回で済む)、連結の deny は「次のコマンドがどの
  アカウントで動くか」を変える形だけに限る。数えない形の allow-list:
  - `kubectl config set-context --current [--namespace=x]` (current-context 名を
    変えない形。`set-credentials` / `set-cluster` / 位置引数で context 名を書く形 /
    `--user` / `--cluster` 付きは**数える** — 同じ名前のまま identity が差し替わる)
  - `gcloud config set <非 identity key>` (`compute/region` / `compute/zone` /
    `run/region` / `functions/region` / `disable_prompts` /
    `core/disable_usage_reporting` / `core/verbosity` / `core/user_output_enabled` /
    `core/disable_color`。`project` / `account` /
    `auth/impersonate_service_account` / `container/cluster` や、`--configuration`
    のような宣言外オプション付きは**数える**)
  - `gh auth refresh` (scope の追加。`--user` / `-u` 付きはアカウントを切り替えるので
    **数える**)
- 別 CLI が状態を書き換える形 (`gcloud container clusters get-credentials ... &&
  kubectl apply ...` / `aws eks update-kubeconfig ... && kubectl apply ...`) も同じ扱い
- AWS は期待値 (Account ID) と profile 名の照合が hook からは不能で「期待値への切替」を
  判定できないため、`aws sso login --profile prod && aws s3 cp ...` のような連結は
  常に deny になる。単独実行の `aws sso login` は従来どおり readonly で通る
- `"$readonly": "deny"` は QUERY 不一致の扱いを戻す設定で、QUERY をこの規則の**書込側**に
  数えることはしない (リモートを変えないため)
- `CLOUD_ACCOUNT_GUARD_MODE=warn` / `off` は従来どおり全体を弱める
- **この deny はコマンド行のセグメント分解に依存する。** `&&` / `;` / 改行 / `||` /
  `(...)` / `|` / 単独の `&` / `if…then` / `watch '...'` 等は分解するが、
  **`bash -c '...'` の内側は分解しない**ため、
  `gh auth switch --user other && bash -c 'gh pr create'` は検出されない
  ([既知の制限](#既知の制限))。`gh auth switch --user other & gh pr create` は
  v0.29.0 から連結として止める (背景の切替と write の前後は競合次第)

#### 従来どおり deny させたいとき (`"$readonly"`)

QUERY の不一致も止めたい場合は `accounts.local.json` に書く:

```json
{
  "$readonly": "deny",
  "github": "your-github-user"
}
```

- 既定は `"warn"` (通して警告)。`"deny"` で v0.13.0 までの挙動 (QUERY も WRITE 扱い)
- `"$mode"` と同じ予約キーで、**builder は値を書かない** (エディタで手編集。現在値は
  `/cloud-account-guard:accounts-show` が表示する)
- `"$mode"` と同じく **`accounts.local.json` を読めたときだけ効く**。
  [グローバル既定](#グローバル既定-v0130) に書いた場合は、自前の
  `accounts.local.json` を持たないプロジェクトにだけ効く
- 不正な値 (`"yes"` 等) は **`deny` として扱い** (fail-closed)、その旨を文面に添える

**設定が壊れている / 曖昧なときは tier に関係なく deny** のまま: 複数パスに
`accounts.local.json` がある (競合) / JSON が壊れている (UTF-8 でない・入れ子が
深すぎるものを含む。v0.18.0) / 期待値の型が不正。
「どの設定が効くか決まらない」状態では読むだけでも判定の土台が無いため。

### 発火するコマンド

コマンドを**セグメントに分解して先頭が対象 CLI になる形**を照合する。以下の
チェーン・構文・ラッパは自動で解析・剥がしたうえで CLI 部分だけを照合する:

- **コマンドチェーン**: `&&` / `||` / `;` / `|` / `|&` / 単独の `&` (バックグラウンド実行。
  v0.29.0) / 改行 (quote / `$()` / `${}` / backtick 内は保護。`2>&1` / `>&` / `&>` は
  リダイレクトなので区切りにしない)。`&` で終わるリストはサブシェルで走るので、その中の
  `cd` は `&` の後ろのコマンドの走るディレクトリに数えない (`cd sub & aws ...` の aws は
  元のディレクトリで走る)
- **heredoc** (v0.9.0): `cat > x.sh <<'EOF'` 〜 `EOF` の**本文は解析しない**。
  本文に書かれた CLI 例は実行されないため (`<<-` のタブ除去、quote 付き
  delimiter、`END-OF-FILE` のようなハイフン入り delimiter、1 行に複数 heredoc、
  here-string `<<<` の非対象化に対応)。delimiter 行の次のコマンドは従来どおり
  別セグメントとして検証する。本文は候補文字列からも落とすので、本文中の
  `--profile` 等がコマンドの option として解釈されることもない
- **シェル構文プレフィックス** (v0.9.0): `(` / `{` / `!` / 予約語
  (`if` `then` `elif` `else` `do` `while` `until`) / コマンド本体前の
  リダイレクト (`</dev/null gh ...` / `2>/dev/null gh ...`)、および末尾の
  `)` `;` `}` `&`。`(gh pr create --fill)` や
  `for f in *; do gh release upload ...; done` も検証対象になる
- **コマンド名のパス / エスケープ** (v0.9.0): `/opt/homebrew/bin/gh` /
  `./node_modules/.bin/firebase` / `\gh` は basename に正規化して照合する。コマンド語の無い
  代入だけのセグメント (`FT=/x/node_modules/firebase-tools`) は対象外で、値が CLI 名で終わっても
  その CLI のコマンドとは見ない (v0.21.4。`FOO=1 firebase deploy` のような代入 + コマンドと、
  次のセグメントのコマンドは従来どおり検出する。`FT=... "$FT" deploy` のように値を後でコマンドとして
  使う形は変数経由の一般則の範囲のまま。値に `$(` / バッククォートを含む代入
  (`X=$(/usr/bin/firebase deploy)`) は従来どおり、置換の中の CLI を検出する。後ろにコマンドが続く
  `X=$(/usr/bin/firebase deploy) aws ...` は、その firebase と後ろの aws の両方を検出する (v0.29.10))
- **先頭の環境変数割当**: `FOO=bar gh ...`。配列の代入・追記・要素の代入
  (`a=(1 2) aws ...` / `a+=x aws ...` / `a[1]=x aws ...`) も剥がす (v0.29.2。値は検証に使わない)。
  値にコマンド置換・バッククォート・算術展開・空白を含むパラメータ展開がある代入
  (`X=$(cmd) aws ...` / ``X=`cmd` gh ...`` / `X=$((1 + 2)) gh ...` / `X=${Y:-a b} gh ...`) も、値を
  読まずに飛ばして後ろのコマンドを照合する (v0.29.10。値は検証に使わず、hook の env で照合する。
  前に同じキーへの静的な代入があれば、その値で照合する (`AWS_PROFILE=dev AWS_PROFILE=$(cmd) aws ...`
  は dev。0.29.9 から)。0.29.9 までは後ろのコマンドを検証していなかった)。例外は gh の token / host の
  env (`GH_TOKEN` / `GITHUB_TOKEN` / `GH_ENTERPRISE_TOKEN` / `GITHUB_ENTERPRISE_TOKEN` / `GH_HOST`) で、
  ここに読めない値を代入したセグメントの gh は 0.29.9 と同じく読む。`GH_TOKEN=$(gh auth token --user x) gh pr create`
  は照合も自動切替もしない (gh が書き込むアカウントは代入したトークンで決まり、hook からは決められない)。
  `GH_TOKEN=$T gh ...` は 0.29.9 から照合している。同じ代入の後ろの gh 以外の CLI は照合する
  (gh と同じセグメントにない CLI。シェル経由の wrapper の中で gh (wrapper を剥がした後の gh も。`npx gh` / `pnpm gh` /
  `bunx gh` など) と並ぶ CLI (`GH_TOKEN=$(cmd) watch 'gh pr create && aws ...'` の aws) は、
  セグメントごと 0.29.9 と同じく読むので照合しない)。
  前置きの代入は、シェルと同じくそのコマンドにだけ効くものとして照合する。heredoc を置いた行
  (`AWS_PROFILE=prod cat <<EOF | aws s3 cp - ...` + 改行 + 本文 + `EOF`) の後ろの段も、起動時の env で
  照合する (v0.30.3。0.30.2 までは先頭のコマンドの代入を同じ行の後ろの段にも渡していた)。シェル経由の
  wrapper の中身 (`AWS_PROFILE=prod watch 'true && aws ...'`) は、wrapper が起動するシェルに渡るので
  中の全段に効く
- **シェルで読み方が割れる形** (v0.29.2): bash 3.2 と zsh で分解が割れる形
  (パラメータ展開の中の素の `{`、配列代入の閉じの直後の `#`) と ANSI-C 引用符 `$'...'` を含む
  コマンドは、別の読みでも分解して候補の和集合を検証する
- **透過的 wrapper**: `sudo` / `time` / `nohup` / `command` / `builtin` / `exec` /
  `env [KEY=val...]` (ただし `env -i` / `env --` など option 付きは不透明扱い)
- **実行ラッパ** (v0.9.0): `timeout` (先頭の DURATION 引数も消費) / `nice` /
  `stdbuf` / `setsid` / `caffeinate` / `watch` / `xargs`。
  `timeout 30 gh pr create` や `... | xargs -I{} gh pr close {}` の後段も検証する
- **ランタイム / パッケージマネージャ**: `npx` / `pnpm exec` / `pnpm dlx` /
  `mise exec --` / `bun x`。v0.32.0 から `bunx` / `pnpx` / `yarn dlx` / `npm exec` / `npm exec --` /
  `mise exec <tool>@<版> --` / `mise x [<tool>@<版>] --` (tool の指定は 0 個以上) と、CLI 名の前の
  `pnpm` / `yarn` (`pnpm firebase deploy` / `yarn firebase deploy`。名前が gh / firebase / aws / gcloud /
  kubectl と、検証の対象外の wrangler / cf のときだけ。yarn は依存の bin (node_modules/.bin) だけを起動し、PATH の
  gh / aws / gcloud / kubectl は起動しない (yarn 1.22 で実測)) も剥がす。v0.33.0 から `yarn exec` (PATH と依存の bin の
  CLI を、打ったディレクトリで起動する。yarn 1.22 / 2.4 / 3.8 / 4.9 で実測) も剥がす。`npx` 以外は語を落とすだけで option は
  読まない (option 付きの形は[守る範囲と守らない範囲](#守る範囲と守らない範囲)を参照)
- 行頭インライン env の伝播は wrapper の実行時 env 挙動に従う
  (`sudo` は preserve 無しだと env を scrub。詳細は
  [`docs/wrapper-env-audit.md`](docs/wrapper-env-audit.md))
- 上記は多段ネストにも対応 (例: `sudo time mise exec -- firebase deploy` →
  `firebase deploy` として検証)
- **CLI 名直後の global option** (v0.8.0): `aws --profile prod sso login` /
  `gcloud --project X config set ...` / `kubectl --context X config use-context ...` /
  `firebase -P X use ...` のようにサブコマンドの前に置かれた global option は、
  剥がした形 (`aws sso login`) でも readonly / 切替 (cache 無効化) /
  self-remediation を判定する (deny 文面の検出コマンドには元の形を表示)。各 service
  が値を取る option / 取らない option を宣言し (`GLOBAL_OPTIONS_WITH_VALUE` /
  `GLOBAL_FLAGS`)、未知の option が先頭にある場合は剥がさず通常検証する。
  `--profile` / `--project` / `--context` の**値**をどう検証に使うかは
  [コンテキスト指定 flag の反映](#コンテキスト指定-flag-の反映-v090) を参照

| サービス | マッチ対象 | 期待値の取得 |
|---|---|---|
| GitHub | `gh ...` | `gh auth status` のアクティブアカウント |
| Firebase | `firebase ...` | `firebase use` (非 TTY) の解決済み project ID → CLI から取れない時のみローカル設定 (configstore / `.firebaserc`) |
| AWS | `aws ...` | `aws sts get-caller-identity --query Account` |
| GCP | `gcloud ...` | `gcloud config get-value project` (+ optional `account`) |
| Kubernetes | `kubectl ...` | `kubectl config current-context` |

Firebase の現在値は firebase-tools 本体と同じ順で解決する (v0.7.3)。
`firebase use <alias|project>` の切替先は CLI の configstore
(`~/.config/configstore/firebase-tools.json` の `activeProjects`) にだけ保存され
`.firebaserc` には反映されないため、まず `firebase use` (非 TTY では解決済みの
project ID を 1 行出力) を読む。その cwd は `firebase.json` を親方向に探した
project root (無ければ project dir) に固定し、hook / builder プロセスの cwd は
継承しない (builder を project の外から起動しても無関係な project を報告しない)。
project root は project dir の symlink を解いた実体のパスから探す (firebase-tools が見る
`process.cwd()` と同じ。`--config` の無いコマンド。configstore もその root のキーだけを引く。
v0.26.1。`--config` が symlink を通るときは root が論理パスのまま。0.18.0)。
CLI から取れないとき (hook の PATH に無い / 実行不可 / 非ゼロ終了 / 出力が空 /
複数行) は CLI と同じローカル設定から同じ規則で解決する (起点は同じ project root):
configstore の切替先を `.firebaserc` の alias で解決 → 無ければ `.firebaserc` の
alias が 1 つならその値 → `default`。`npx firebase ...` のように hook 側に
`firebase` が無い構成でも、configstore 経由で切替を見落とさない。ただし
`.firebaserc` を firebase-tools と同じ内容に読めると確かめられないとき (条件は
[pin-env](#プロジェクトごとにアカウントを固定する-公式の方法--v0170) の firebase と同じ)
は、ローカル設定から解決せず、現在値を取得できないとして deny する (v0.18.0)。alias の
行き先を firebase-tools と違う project に読んで照合しないため。
firebase.json (`--config` 付きのコマンドではそのファイル) に旧形式のキー `"firebase"` がある
とき (値に関わらず) と、そのキーの有無を確かめられないときも同じく deny する (v0.20.0)。
firebase-tools は configstore の切替先が無いと、`.firebaserc` の alias より先にこのキーの project
で動く。確かめられないのは、読めない・UTF-8 でない・大きすぎる (256 KiB を超える)・U+FEFF が
ある・コメントがある (cjson が除く形。URL のような文字列の中の `//` は当たらない)・厳密な JSON
として読めない (NaN・末尾のカンマ・構文の誤りなど) とき。空のファイルは firebase-tools と同じく
`{}` として読む。configstore が大きすぎる (256 KiB を超える) ときも、`firebase use` の切替先の
有無を確かめられないとして deny する (v0.25.0)。
`firebase use` が timeout したときは fallback せず「firebase use がタイムアウト
しました」で deny する (fail-closed)。

`--config` / `-c` 付きのコマンドは、指定したファイルのあるディレクトリを project root
にする (firebase-tools と同じ。v0.18.0)。`firebase use` の cwd と、ローカル設定から解決する
ときの起点がそこに移る (`firebase use` にも同じ `--config` を付ける。ローカル設定の切替先は、
firebase-tools と同じく symlink を解かないそのパスの親方向だけを探す)。firebase-tools は
`firebase use` の切替先を project root ごとに記録し、`--config` 付きのコマンドはそのファイルの
あるディレクトリから親へ探した切替先で動く。そのディレクトリがプロジェクトのディレクトリと
別で、そこに切替先が記録されていると、プロジェクトのディレクトリで切り替えても変わらない。
このため `--config` 付きのコマンドの deny は、同じ `--config` を CLI 名の直後に付けた切替
(`firebase --config <そのファイルの絶対パス> use <期待値>`) を案内する (v0.27.0。firebase.json で
ない名前のファイルや、symlink を通るパスでも効く形)。パスか期待値が案内に使える形でないときは、
そのファイルのあるディレクトリで切り替えるよう文で案内する (0.18.0〜0.26.1 はいつもこの文。
`--config` のパスが symlink を通るときは、そのディレクトリで切り替えても効かないことがある。
firebase-tools は symlink を解かないパスで切替先を探すため。[既知の制限](#既知の制限))。
この deny の先頭行に示す現在値 (`現在=`) と期待値 (`期待=`。現在値を取得できない deny も示す)
も、[値を文面に示す形](#期待値へ向かう切替コマンドは許可-self-remediation) のものだけを示す (`firebase use` に入れる形より広い。
ドメイン付きの project ID や日本語の名前も示す。外れていれば「表示しない値」)。期待値に示さない
値があるときは、「表示していない期待値があります (accounts.local.json の "firebase" を確認して
ください)」と出所を添える (現在値を取得できない deny も同じ)。そうした値はどの project とも
一致しないので、切り替えても deny が続くため。

### 検証をスキップする readonly コマンド

「アカウント設定のための状態確認」でデッドロックしないよう、以下は素通し:

**いずれも [DISCLOSING](#検証の-3-tier--v0140) なオプション / 形が付いた場合は
素通しの対象外**になり、想定外のオプションが付いた場合は QUERY に降格する (v0.14.0)。

- `gh auth status` / `gh auth list` (`--show-token` / `-t` 付きは除く)
- `firebase use` (引数なし)
- `firebase login` / `firebase logout` (`login:add` / `login:use` 等の
  サブコマンド含む。**CI トークンを出力する `login:ci` は除く**) — project を
  変更しない認証操作。未ログインだと `firebase use`
  が認証必須で失敗して現在値を取れず、`firebase login` 自体が deny されるデッド
  ロックになるのを防ぐ (v0.7.3)
- **認証取得系** (v0.8.0): `gh auth logout` / `setup-git`
  (`logout` はローカル `hosts.yml` のエントリ削除、`setup-git` はローカル git config の
  credential.helper 設定で、どちらもアカウント側には何も書かない。**`gh auth refresh` は
  含まない** — 保存済み認証情報の権限を拡張・修正するコマンドで
  `--scopes admin:org` のようにアカウント側の OAuth grant を変更しうるため通常検証する)、
  `gh auth login` は **リモートに何も書かない形のみ** — (a) SSH 鍵のアップロードが
  起きない (`--skip-ssh-key` / `--with-token` / `--git-protocol https` (`-p https`)
  付き。flag 文字列の有無ではなく実効 boolean を解釈し、`--skip-ssh-key=false` /
  `--with-token=0` のような明示 false や `--git-protocol ssh` は無効、繰り返しは後勝ち。
  SSH git protocol を選ぶ login は既存の SSH 公開鍵を GitHub アカウントに
  アップロードしうる) かつ (b) **`-s` / `--scopes` で OAuth grant scope を要求して
  いない** (`gh auth login --skip-ssh-key --scopes admin:org` は鍵操作こそ起きないが
  アカウント側の grant を拡張するため通常検証。値を取る flag なので `=false` では
  無効化できず、付いていれば無条件に検証対象)。それ以外の `gh auth login` は
  通常検証し、deny 文面では `--skip-ssh-key` 付きの形を案内する、
  `aws sso login` / `aws sso logout` / `aws login` / `aws logout` /
  `aws configure ...` (認証情報を出力する `configure export-credentials` は除く)、
  `gcloud auth login` / `gcloud auth application-default login` / `revoke` /
  `set-quota-project` / `gcloud auth activate-service-account` / `gcloud auth revoke`
  (`firebase login` 系は `npx firebase-tools login` の形も同じ) — クラウド資源を
  変更せず、ローカルの認証状態・profile 設定を作るだけ。未ログインで検証
  (`aws sts get-caller-identity` 等) が失敗する状態では、deny 文面が案内する
  `aws sso login --profile <profile>` / `gh auth login` 自体が deny される
  remediation loop になっていた。これらは同時に「アカウント状態を変えうる
  コマンド」として成功 cache を破棄するため、直後の write は必ず再検証される
  (後述)
- `aws sts get-caller-identity` / `aws configure get <非機密キー>`
  (`aws configure get aws_secret_access_key` / `aws_session_token` は除く)
- `gcloud auth list` / `gcloud config get-value project` / `... account`
- `kubectl config current-context` / `... get-contexts` / `... view`
  (`--raw` 付きは除く) / `... get-clusters` / `... get-users` /
  `kubectl cluster-info` (`cluster-info dump` は除く)
- **情報系コマンド** (各 CLI の `--version` / `--help` / `version` / `help`) —
  アカウント検証不要。診断で打つ `command aws --version` 等が誤って検証対象に
  なり deny されるのを防ぐ (v0.7.0)
- **gh のサブコマンドの後ろの `--help`** (v0.30.0): `gh auth switch --help` /
  `gh auth token --help` / `gh pr create --title x --help 2>&1` のように、組み込みの
  コマンドに続く最後の語が `--help` の形 (後ろはリダイレクトだけ)。グループ (子を持つ
  コマンド。`gh pr` / `gh auth` / `gh repo deploy-key` など) は直後が `--help` の形
  (`gh pr --help`) だけ。gh はヘルプを出して
  終わり、コマンドを実行しない。[成功 cache の破棄](#切替ログイン系コマンドでの即時無効化-v080)
  と[切替と書込の連結](#切替と書込を同一コマンドに連結した形は-deny--v0150)にも数えない。
  次の形は従来どおり検証する:
  - 直前の語が `=` を含まない option (`gh pr create --title --help` は `--help` が
    `--title` の値になり、create が走る。bool の flag の直後 (`--draft --help`) も
    hook には区別できないので同じ扱い)
  - `-h` (gh の `auth` 配下などでは `--hostname` の短い形。`gh auth switch -h <host>` は切替)
  - `--help` の後ろに語がある (`--help --help=false` は後勝ちでヘルプを取り消す) /
    `--` の後ろの `--help` / 引用符・`$`・glob・`~` などを含む語がある
  - alias と extension は `--help` をそのまま中身に渡すので対象外。alias はトップレベル
    (`gh co` など) のほか、グループの下にも置ける (`gh issue mine`。`gh alias set --help` の例)
    ので、グループの直後の語が組み込みの子でない形 (`gh pr ship --help` /
    `gh pr -R o/r ship --help`) は検証する。組み込みでも `copilot` / `extension` (引数を
    別のプログラムに渡す) と `(preview)` と表示されるコマンド (`agent-task` / `discussion` /
    `event` / `skill`)、組み込みの別名 (`gh issue ls` など) は対象外。組み込みの表は gh 2.105 のもの
    (版の違いは[守らない範囲](#守る範囲と守らない範囲))

  **対応は gh だけ**。aws / gcloud / kubectl / firebase のサブコマンドの後ろの `--help`
  (`firebase use --help` / `kubectl apply --help` など) は従来どおり検証する。
  どの CLI でも `<cli> help <サブコマンド>` の形 (`gh help auth switch` /
  `firebase help use`) は上の情報系として検証しない
- **存在確認** (v0.9.0): `command -v gh` / `command -V aws` / 連結形 `command -pv aws`。
  `command` は `-v` / `-V` 付きだと**後続 CLI を実行せずパスを表示するだけ**
  (`man 1 bash`) なので、透過 wrapper として剥がさず素通しする。剥がしていた頃は
  インストール確認の定型句だけでアカウント検証が走って deny されていた。
  `command gh ...` / `command -p gh ...` / `command -- gh ...` は実行するので従来どおり検証

同一コマンドチェーン内で readonly と非 readonly が混在する場合
(例: `gh auth status && gh pr list`) は非 readonly セグメントについて検証が走る。
deny されたときは reason 末尾の `(検出コマンド: ...)` でどのセグメントが検証を
起動したかを確認できる。

### 期待値へ向かう切替コマンドは許可 (self-remediation)

deny メッセージが案内する「期待値への切替コマンド」自体が `^gh\b` 等にマッチして
deny される remediation loop を防ぐため、**accounts.local.json の期待値へ向かう
切替コマンドだけは検証なしで許可**する:

- `gh auth switch --user <期待アカウント>` (dict 形式は `--hostname` も照合、省略時 github.com)
- `gcloud config set project <期待値>` / `gcloud config set account <期待値>`
- `firebase use <期待 alias または project ID>`
- `kubectl config use-context <期待コンテキスト>`

**装飾オプション付きの形も同じ扱いになる (v0.15.0)。** 出力や対話の制御だけを足した形は
上と同じ「期待値への切替」として認識する — `gcloud` は `--quiet` / `-q` /
`--verbosity=...` / `--format=...` / `--no-user-output-enabled`、`kubectl` は
`--v=...` / `--request-timeout=...`、`firebase` は `--non-interactive` / `--debug` /
`--json`、`gh` はオプションの順序・`=` 形・短縮形すべて。

**切替の着地先を変える context option も、切替先が期待値なら同じ扱いになる (v0.27.0)。**
gcloud の `--configuration`、kubectl の `--kubeconfig` は、その option で選んだ構成 / ファイルの
状態を書き換える。これらを付けたコマンドの deny は同じ option を付けた切替を案内するので
(`gcloud config set project <期待値> --configuration <構成>` /
`kubectl config use-context <期待値> --kubeconfig <ファイル>`)、その形を期待値への切替として通す。
0.26.1 までは末尾に付けた形は通常検証に落ちて、案内どおりにしても往復が閉じなかった (CLI 名の直後に
付けた形は以前から通っていた)。その構成 / ファイルを使うコマンドも使わないコマンドも実行時に照合される。
firebase は末尾の `--config` / `-c` を剥がさない (末尾に付けた切替は通常検証に落ちる)。firebase の
切替の名前は `--config` のファイルのあるディレクトリの `.firebaserc` で alias として解決され、hook は
その解決先を照合しない (既知の穴。別途対応) ので、剥がすとこの穴と組み合わさって通る形が増えるため。
deny は CLI 名の直後に置く形 (`firebase --config <ファイルの絶対パス> use <期待値>`) で案内し、この形は
以前から期待値への切替として通る。

**それ以外のオプションは対象外** (許可オプションの allow-list で、コマンドの allow-list では
ない)。`gcloud config set project <期待値> --flags-file f.yaml` (任意の flag を注入できる)、
`kubectl config use-context <期待値> --context other` (照合先を差し替える)、
`firebase use <期待値> --project other` などは従来どおり通常検証に落ちる (現在値が期待値と
一致していれば allow、[連結](#切替と書込を同一コマンドに連結した形は-deny--v0150) されて
いれば deny)。

**案内された切替 / ログインコマンドは案内された形のまま単独で実行すること。** 元のコマンド
を同じコマンド行に連結すると (`gh auth switch ... && gh pr create` 等)、そちらが切替**前**
の状態で検証されるため deny される (案内文自身が連結している `firebase login && firebase
use <x>` は、書込側が期待値への切替なので許可される形)。remediation を案内する deny
文面にはこの注記が付く (v0.11.1)。値の一部でもコマンドの形で案内しなかった deny (下の
「手で確認してください」の文がある deny) には、ほかの entry の切替を案内していても付けない
(v0.17.1)。ただし AWS は、外れた profile 名を文に置き換えたうえで `AWS_PROFILE=<profile>` を
必ず案内するので、AWS 専用の注記を付ける。文面に表示する値 (全 service の `現在=` / `期待=`、
検出したコマンドが指定した値 (`コマンド指定 --context=<値>` など)、`.firebaserc` の alias の
行き先 (`--project <alias> (→ <project>)`)、gh の host 名 (`GitHub [<host>]`)、自動切替の
注記に出す host とアカウント、[切替と書込の連結](#切替と書込を同一コマンドに連結した形は-deny--v0150)
の deny の期待値) は、空白 (Unicode の空白を含む) も `=` も含まず、文字・数字・句読点・
数学記号・通貨記号・修飾記号と、値の途中の結合文字 (NFD で書いた濁点など) だけからなる値を
出す (外れていれば「(表示しない値)」「表示しない host」。
日本語の名前や `_local` / `a,b` のような普通の名前、`=` 以外の ASCII の記号を含む名前は、案内コマンドには
入れないが値としては出す)。示す文字を許可リストで持つので (v0.32.1。それまでは示さない文字を並べる
禁止リスト)、制御文字・書式文字・行区切り / 段落区切り・未割り当て / 私用領域 / サロゲートと、
絵文字・囲み文字などのその他の記号 (©・★・㈱ などを含む名前も出さない) は出さない。その他の記号には
空白に見える文字 (点字の空白、U+FFFC など) があるため。示す種類 (文字・結合文字) に入るが、既定で
無視される文字 (Hangul filler・異体字セレクタなど) と空白に見える文字 (エジプト聖刻文字の空白など) も
出さない。この 2 つだけは個別に並べた範囲で持つので (普通の名前に現れない block は、その block の文字ごと出さない)、将来の Unicode で同じ種類の文字が足されると、
範囲に足すまで出す。
結合文字で始まる値も出さない (前の固定文の `=` と合成されて別の記号に見える)。
値は期待値のファイル・`.firebaserc`・CLI の設定・コマンドの引数から来るので、改行を含む値で
偽の行 (切替コマンドだけの行など) を文面に差し込めないように、また値の形だけで注記が付かない
ようにするため (切替コマンドの形はどれも空白を要する。v0.21.0 で全部の表示に揃えた。それまでは
一部の表示だけ)。検出したコマンドが指定した値は、外れていても `(検出コマンド: ...)` の行で
分かる。その行と、転記する CLI の出力 (gh の切替の失敗、aws の認証情報のエラー) は、値を
置き換えずに制御文字と書式文字 (双方向制御の文字など) だけをエスケープして (`\n` / `\xNN` /
`\uNNNN` / `\UNNNNNNNN`) 1 行に収める (v0.21.0)。deny 文面に出す起動ディレクトリ由来のパス
(期待値ファイル・親ディレクトリ・起動リポジトリ・グローバル既定・配置パスの競合の一覧) も
同じ形にする (v0.21.1。途中のディレクトリ名は、リポジトリ側が改行などを含む名前にできる)。
builder と `pin-env` の出力に出すパスも同じ形にし、旧ファイルの削除 (`rm`) と `--path` の案内は、
制御文字を含むパスではコマンドの形にしない (v0.21.2)。
builder の値の表示 (変更の差分・`show` の期待値と CLI 現在値) と、`pin-env` の候補・現在値・env の断片は、
値を置き換えずに JSON の `\uXXXX` で 1 行に収める (v0.21.3。DEL・C1・行区切り・書式文字・孤立サロゲートが
対象。JSON として読めば元の値に戻り、settings の `env` に貼る断片はそのまま使える)。`pin-env` の値の行だけは
deny 文面と同じく外れた値を「(表示しない値)」にする。

**案内するコマンドに入れる値は、シェルでそのまま 1 語になる形に限る (v0.17.1)。** 期待値・
host 名・profile 名が英数字で始まり、英数字と `.` `_` `-` `:` `/` `@` `+` だけからなる
ときだけコマンドの形で案内する (firebase の alias / project ID は `.` `_` `-` まで)。
`;` / `$()` / 空白 / 改行や先頭の `-` を含む値は、案内どおりに打つと別のコマンドや option
として走りうるので、コマンドにせず「手で確認してください」と書く (deny / allow は
変わらない)。これが防ぐのはコマンド構文の注入だけで、許容形を通る値による意味の上での誘導
(コミットされた期待値ファイルの host を攻撃者の host にするなど) は防がない。期待値ファイルの内容は
repo の管理者が決めたものとみなす。詳しくは[守らない範囲](#守る範囲と守らない範囲)を参照。

**firebase の `firebase use <x>` / `--project <x>` の案内は、`.firebaserc` の解決先も照合する
(v0.21.4)。** firebase-tools は x を `.firebaserc` の alias として先に解決するので、期待値と同じ
名前の alias が別の project を指していると、案内どおりにしても期待した project にならない。案内するのは、
解決先が期待した project の語だけ (dict は `pin-env` と同じ順 (alias → project ID)。scalar の
`firebase use` は project ID だけ (`is_self_remediation` が期待値そのものしか通さないため)。scalar の
`--project` は、project ID が別の project を指す alias に影になっているとき、期待した project を指す
alias も案内する)。着く語が無いとき (`.firebaserc` を firebase-tools と同じ内容に読めると確かめ
られない場合を含む) は、コマンドの形で案内せず `.firebaserc` と `accounts.local.json` を手で確認する文に
する (deny / allow は変わらない)。

期待値**以外**への切替は従来どおり通常検証 (実行前の状態) に落ちる。ただし同じ
コマンド行に**同じ service の書込が連結**されていれば、現在のアカウントに依らず
deny する ([切替と書込の連結](#切替と書込を同一コマンドに連結した形は-deny--v0150))。
切替コマンド (self-remediation を含む) を検出した時点で当該 service の成功 cache を
破棄し、切替コマンド自身の検証成功も cache しないため、切替後の最初の write は成功
cache の残り時間に関係なく次回 hook で再検証される (v0.8.0。詳細は
[パフォーマンス (短期キャッシュ)](#パフォーマンス-短期キャッシュ))。

AWS は期待値 (Account ID) と profile 名の照合が hook からは不能なため、この特例の
対象外。代わりに deny 文面は `AWS_PROFILE=<profile> aws ...` (行頭インライン env。
検証にも反映される) を第一に、`aws sso login --profile <profile>` (readonly で
検証なしに通る) を第二に案内し、`~/.aws/config` (または `$AWS_CONFIG_FILE`) に期待
Account ID の `sso_account_id` / `role_arn` を持つ profile があれば `<profile>` を
具体名にする (profile 名以外の内容は文面に出さない)。`export AWS_PROFILE=...` は
Claude Code の Bash では次の呼出に持ち越されず、hook (Claude 本体の env を継承)
の検証にも反映されないため、コマンドとしては案内しない (v0.8.0)。

## コンテキスト指定 flag の反映 (v0.9.0)

`aws --profile other s3 rm ...` のように、**コマンド側で照合先を切り替える
flag** は、その値を検証に反映する。従来は hook の既定コンテキスト
(既定 profile / アクティブ project / current-context) だけを見ていたため、
「検証は既定 / 実行は other」という誤 allow になっていた。

| サービス | 見る option | 検証への反映 |
|---|---|---|
| AWS | `--profile` | 検証コマンドにも `--profile` を付けて実行 (CLI の資格情報解決順を実行時と揃える) |
| GCP | `--project` / `--account` | 値を期待値と直接照合 (アクティブ設定は見ない) |
| GCP | `--configuration` | 現在値の取得コマンドに引き渡す |
| Firebase | `--project` / `-P` | `.firebaserc` の alias を解決してから照合 (CLI 本体と同じ規則)。`.firebaserc` を firebase-tools と同じ内容に読めると確かめられなければ deny (v0.18.0) |
| Firebase | `--config` / `-c` | 指定したファイルのあるディレクトリを project root にする (`.firebaserc` と `firebase use` の起点。CLI 本体と同じ)。相対パスは、そのコマンドが走るディレクトリ (Bash の作業ディレクトリ + コマンド中の `cd`) の実体のパスから探し (CLI 本体と同じ。v0.29.3。0.29.2 まではプロジェクトのディレクトリから探していた)、見つからなければ deny (v0.18.0)。決められない形は kubectl の `--kubeconfig` と同じく deny |
| Kubernetes | `--context` | 値を期待値と直接照合 |
| Kubernetes | `--kubeconfig` | 現在値の取得コマンドに引き渡す。相対パスは、そのコマンドが走るディレクトリ (Bash の作業ディレクトリ + コマンド中の `cd`) の実体のパスに連結した絶対パスにしてから渡し、deny の案内もその絶対パスで出す。相対パスの `KUBECONFIG` も同じディレクトリで解決する。同じ相対パスのコマンドが別々のディレクトリで走る形と、括弧・条件・パイプの中の `cd` と同じコマンドにある形は、どのファイルか決められないので deny (v0.28.0) |

- 記法は `--opt value` / `--opt=value` / 短縮の分離形 (`-P prod`) / 値の連結形
  (`-Pprod`) を拾う。`-P=prod` は hook では値 `prod` と読むが、firebase-tools (commander) は
  値を `=prod` と読む (`-c=x` も `=x` というファイル)。実在しない project / ファイルを指す通常の
  構成ではコマンドが失敗する。`=` で始まる alias やファイルを細工したリポジトリでは、照合先と
  実行先が食い違いうる ([既知の制限](#既知の制限))。`--` 以降は後続コマンドの
  引数なので見ない (`kubectl exec pod -- cmd --context x` の `--context` は採用しない)
- 値が `~` で始まる独立した語 (`--kubeconfig ~/.kube/prod.yaml` / `-c ~/x/firebase.json`) は、
  シェルと同じく HOME に展開してから照合する (v0.29.6。0.29.5 までは作業ディレクトリの
  `~/...` を探して誤 deny になっていた)。展開するのは bash と zsh で読みが一致する形 (語の先頭の
  引用符の外の `~` で、直後が語の終わりか `/`) だけ。次の形は展開せずに 0.29.5 までと同じ読みにする:
  引用符の中 (`'~/x'`)、`=` や短い option に続けた形 (`--kubeconfig=~/x` / `-c~/x`。どちらの
  シェルも展開しない)、`~user` / `~+` / `~-` / `~:x` / `~"/x"`、コマンドが `HOME` を書き換えうるとき
  (行頭の代入の `~` と同じ条件)、シェル経由の wrapper の中身 (`watch '...'` / `sudo -i ...` /
  `npx -c '...'`)。引用符で包んだ中身は wrapper が起動するシェルが読み直す。引用符で包まずに並べた形
  (`watch kubectl ... ~/x` / `sudo -i kubectl ... ~/x`) は Bash のシェルが先に展開するが、wrapper を
  剥がした後の文字列からは包んだ形かどうかを区別できないので、展開しない側に倒す (0.29.5 と同じ読み)。
  heredoc を置いた行の後ろの段 (`cat <<EOF && kubectl --kubeconfig ~/x ...`) は Bash のシェルが読む語
  なので展開する。行の先頭の段がシェル経由の wrapper でも、区切りが wrapper の引用符の外にあれば
  (`watch 'cat' <<EOF && kubectl --kubeconfig ~/x ...` / `sudo -i cat <<EOF | kubectl ...`)、後ろの段は
  外のシェルが読むので展開する (v0.30.4。0.30.3 までは wrapper の中身と読んで展開しなかった)
- ユーザーの zsh で `setopt magic_equal_subst` が有効だと、`--kubeconfig=~/x` の `~` も zsh が HOME に
  展開する。hook は展開しない読み (bash と `zsh -f` の既定) で照合するので、この設定では照合先と
  CLI が読むファイルが食い違いうる
- **短いオプションを結合したトークンに、照合先を決める短い option が含まれる形は deny する**
  (v0.20.0)。Firebase の `-jP prod` / `-iPprod` / `-jc sub/firebase.json` は、firebase-tools
  (commander) では `-j -P prod` などに分かれる。hook は結合形を展開しない (CLI ごとの展開の規則を
  再現しない) ので、照合先を確かめられないとして止める。`-P` / `-c` を単独のトークンに分けて
  書けば照合する。値を取らない文字だけの結合形 (`-ji`) は従来どおり読み飛ばし、`-Pprod` のように
  先頭の文字が `-P` / `-c` の形は値の連結形として読む。短い形の context option を持つのは
  Firebase だけ (aws / gcloud / kubectl の context option は long option だけ) なので、ほかの
  service には当たらない
- 同じ option が複数あれば**最後が有効** (各 CLI の実装と同じ)
- サブコマンドの**後ろ**に書かれた形 (`aws s3 ls --profile prod`) も拾う
- **GCP は key ごとに独立**して上書きする。`gcloud --project ok run deploy` でも
  `account` の期待値があれば account はアクティブ値と照合する
- 値が変数展開などで静的に解決できない場合 (`--profile $PROF`) は、従来どおり
  既定コンテキストで照合する
- 1 コマンド内で異なる指定が混ざる形
  (`aws --profile a s3 rm x && aws --profile b s3 rm y`) は**別々に検証**する。
  成功キャッシュも指定ごとに分かれる
- **`gh` は対象外**。`gh auth <sub> --hostname <host>` / `--user <user>` は
  「どのアカウントで実行するか」ではなく**操作対象**の指定なので、照合先は常に
  アクティブアカウント (後述の既知の制限を参照)

## インライン環境変数の伝播 (v0.7.0)

コマンド行頭のインライン環境変数 (`AWS_PROFILE=prod aws ...`) は、剥がして CLI
部分を照合するだけでなく **検証 subprocess にも渡される**。これにより
「コマンドを実行しようとしている env」と同じ条件でアカウント検証が走る。

```bash
# 例: SSO profile 運用で行頭に AWS_PROFILE を付けて実行
AWS_PROFILE=prod aws s3 ls
```

従来は行頭 env を剥がすだけで検証には使わなかったため、`~/.aws/config` に
`[default]` を置かない SSO 運用では、ログイン済みでも検証が default profile で
失敗し **永久に deny** されていた。v0.7.0 ではインライン env を
`{**os.environ, **inline}` としてマージし検証 subprocess に渡すことで解消した。

- 対象は全 5 service (`AWS_PROFILE` / `AWS_REGION` / `CLOUDSDK_*` / `KUBECONFIG` /
  `GH_HOST` 等、行頭に書いた任意の `KEY=VALUE`)
- profile が異なれば短期キャッシュも別エントリになり、profile A の成功が
  profile B で誤って allow されることはない
- 値に未展開の変数参照 (`AWS_PROFILE=$SOMEVAR`) を含む場合は静的に解決できない
  ため検証 env には渡さない (コマンドからは剥がす)。値にコマンド置換・バッククォートを含む場合
  (`AWS_PROFILE=$(cmd) aws ...`) も同じで、後ろの CLI は hook の env で照合する (v0.29.10)。
  ただし前に同じキーへの静的な代入があれば、その値で照合する (`AWS_PROFILE=dev AWS_PROFILE=$(cmd) aws ...`
  は dev で照合する。シェルでは後ろの値が勝つ。0.29.9 から)
- kubectl の行頭の `KUBECONFIG=a.yaml kubectl apply ...` (起動時の値と違うもの) の deny は、
  同じ `KUBECONFIG` を前に付けた切替 (`KUBECONFIG=<絶対パス> kubectl config use-context <期待値>`)
  を案内する (v0.29.4)。相対パスの要素は、そのコマンドが走るディレクトリの絶対パスにする。
  0.29.3 までは素の `kubectl config use-context` を案内しており、それは Bash の env の kubeconfig に
  書くので、案内どおりにしても同じ deny に戻った。値が案内に使える形でない (空白・引用符付きの
  `~` 始まり・空など) ときはコマンドの形では案内せず、文で言う
- 値の先頭と `:` の後の `~` は、シェルと同じく HOME に展開してから検証に渡す (v0.29.5)。
  `KUBECONFIG=~/.kube/prod.yaml kubectl apply ...` は HOME の下の `.kube/prod.yaml` で照合する
  (0.29.4 までは作業ディレクトリの `~/.kube/prod.yaml` を探して誤 deny になっていた)。
  展開するのは bash と zsh で読みが一致する形 (`~` の直後が値の終わり・`/`・`:`) だけで、
  引用符の中の `~` (`'~/x'`) はシェルと同じく展開しない。次の形は展開せずに 0.29.4 までと同じ
  読みにする: `~user` / `~+` / `~-`、引用符付きの `:` の後や `~` の直後が引用符の形 (zsh だけが
  展開する)、wrapper の後ろの代入の形の引数 (`env KUBECONFIG=~/x ...`。bash だけが展開する)、
  コマンドが `HOME` を書き換えうるとき (コマンドに `HOME` の語、`${HOME:=x}`、ファイルを読む `.`
  (`. ./env.sh`)、`source` / `eval` がある。名前の一部の `HOME` (`XDG_CONFIG_HOME`)、読むだけの
  `$HOME` / `${HOME}`、引数の `.` (`kubectl apply -k .` / `git add .`) は数えない)、シェル経由の
  wrapper の中身 (`watch '...'` / `sudo -i ...` / `npx -c '...'`)。heredoc を置いた行の後ろの段の
  代入 (`cat <<EOF && KUBECONFIG=~/x kubectl ...`) は Bash のシェルが行う代入なので展開する (v0.30.2。
  0.30.1 までは展開せず、作業ディレクトリの `~/x` を探して誤 deny になっていた)。行の先頭の段がシェル経由の
  wrapper で、区切りが wrapper の引用符の外にある行 (`watch 'cat' <<EOF && KUBECONFIG=~/x kubectl ...`) の
  後ろの段も同じ (v0.30.4)
- 予約語の `time` の後ろの代入 (`time KUBECONFIG=~/x kubectl ...`) も、シェル自身が行う代入なので展開する
  (v0.30.4。0.30.3 までは wrapper の後ろの引数と読み、作業ディレクトリの `~/x` を探していた。そこに期待値どおりの
  kubeconfig があれば、kubectl が読む HOME の下が違っても通していた)。予約語として読まれない `time`
  (`X=1 time ...` / `\time ...` / `nohup time ...`) は外部の time で、代入の形の語をコマンド名として起動しようと
  して失敗し後ろの CLI は走らないので、従来どおり展開しない。option 付きの `time -p` (zsh は `-p` をコマンド名と
  して起動する) と、`time` の後ろの wrapper の後ろの代入 (`time env KUBECONFIG=~/x ...`) も展開しない

### 透過 wrapper を跨ぐときの env 伝播

行頭 env が**透過 wrapper の前**に置かれた場合、その wrapper が実行時に env を
素通すかどうかで伝播可否が変わる:

- `time` / `nohup` / `command` / `exec` / `npx` / `pnpm exec` / `mise exec --` /
  `bun x` などは env を素通すため、`AWS_PROFILE=prod time aws ...` の `AWS_PROFILE`
  は検証にも反映される
- **`sudo` (preserve 無し) は継承 env を scrub する**ため、
  `AWS_PROFILE=prod sudo aws ...` の `AWS_PROFILE` は実行時の `sudo aws ...` には
  届かない。検証側もこれに合わせ pre-sudo env を伝播せず、検証はデフォルト env で
  走る (= 「検証は prod / 実行は別アカウント」の誤 allow を防ぐ)。`sudo -E` /
  `--preserve-env` を付けた場合は env が保持されるので検証にも反映される
- `env -i` / `env -u` / `env --` は環境をリセット/縮小するため透過剥がしの対象外
  (そのセグメントは検証スキップ)

wrapper ごとの env 挙動の完全な分類と将来 wrapper 追加時の方針は
[`docs/wrapper-env-audit.md`](docs/wrapper-env-audit.md) を参照。

### direnv / CLAUDE_ENV_FILE 経由の env は届かない

`.envrc` (direnv) や他の `CwdChanged` hook が `CLAUDE_ENV_FILE` 経由で注入する
環境変数は **PreToolUse hook には渡らない**。Claude Code 公式仕様で
`CLAUDE_ENV_FILE` は SessionStart / Setup / CwdChanged / FileChanged のみに
提供され、PreToolUse は対象外のため (hooks.md に明記)。`.envrc` に
`export AWS_PROFILE=...` を書いても Bash ツール実行時には効くが、本 hook の検証
subprocess には反映されず deny される。

これは harness 仕様起因で plugin 側では解決できない。通したい場合の回避策:

1. **インライン env** (上記): `AWS_PROFILE=prod aws ...` と行頭に付ける (最も手軽・確実)
2. **`.claude/settings.local.json` の `env`**: Claude プロセスの env に設定する
   Claude Code の公式機能で、hook の検証にも同じ値が届く (v0.17.0 で `claude -p` に
   より実測)。保存した時点で起動中のセッションにも反映される。プロジェクトごとに
   固定するならこれ — [プロジェクトごとにアカウントを固定する](#プロジェクトごとにアカウントを固定する-公式の方法--v0170)
   (例: `{"env": {"AWS_PROFILE": "prod"}}`。commit される `.claude/settings.json` には
   書かない)
3. **起動時 env**: `AWS_PROFILE=prod claude` で Claude 自体を起動する。ディレクトリ単位で
   環境変数を切り替えるツール (mise・direnv など) をシェルで有効にしているなら、その
   ディレクトリで Claude Code を起動すれば、起動時の値が hook にも届く。セッション中に
   別のリポジトリへ移った後の値は届かないので、v0.19.0 からは
   [起動したリポジトリの外での実行](#起動したリポジトリの外での実行--v0190)を止める

## 拡張フォーマット (object 形式)

### GitHub Enterprise / 複数 hostname

`github` を object で書くと、hostname ごとのアクティブアカウントを個別検証する:

```json
{
  "github": {
    "github.com":        "your-github-user",
    "ghe.example.com":   "your-corp-user"
  }
}
```

どちらかの hostname のアクティブアカウントが期待値と違う、または hostname 自体
ログインしていないと deny する。

### Firebase の複数 alias

`.firebaserc` の `projects` マップと対応させて、どれか 1 つの project ID が
アクティブなら OK にできる:

```json
{
  "firebase": {
    "default": "proj-dev",
    "prod":    "proj-prod"
  }
}
```

`firebase use prod` で `proj-prod` に切り替えてあれば allow。どちらでもなければ
deny し「proj-dev, proj-prod のいずれか」を提示する。

### GCP の account も検証

`gcloud` を object にすると project に加えてアクティブアカウント
(メールアドレス) も検証する:

```json
{
  "gcloud": {
    "project": "my-proj",
    "account": "me@example.com"
  }
}
```

`project` / `account` は片方だけでも可。

## 配置パスの 3-tier lookup (v0.3.0)

dispatcher は以下の順に accounts.local.json を探す:

1. **新**: `.claude/cloud-account-guard/accounts.local.json` (推奨)
2. **deprecated**: `.claude/accounts.local.json` (警告付きで受け入れ)
3. **legacy**: `.claude/accounts.json` (警告付きで受け入れ)

### 旧パスからの移行

旧パスのみ存在する場合は検証は通るが、以下のように deny/warn で案内が出る:

```
.claude/accounts.local.json は旧パスです。
.claude/cloud-account-guard/accounts.local.json への移行を推奨します。
旧パスから統合するには builder の migrate サブコマンドを使用してください: ...
```

**新旧両方存在する場合は fail-closed で deny** する。どちらが正本か曖昧な状態で
検証を通すと、どの設定が効いているか不透明になる。
`/cloud-account-guard:accounts-migrate` で統合するか、不要な方を手動削除する。
`migrate --commit` は旧パスを消さないので、消すまでこの deny は続く。skill は統合の後に
旧パスを消すかを確認し、承認されたら `migrate --commit --remove-old` で消す (v0.27.0。
builder は、新パスが書いた内容のとおりで、旧パスが統合したときから変わっておらず、新パスの唯一の
実体でないときだけ消す。消した後に新パスが統合した内容でなくなっていれば書き戻して exit 1 にする)。

**stat できない配置パスは「ある (が読めない)」に数える** (v0.19.1)。途中のディレクトリに
権限が無い・長すぎる名前を指す symlink・symlink のループなどで、そこにファイルがあるかどうかを
確かめられないパスは、無いものとして次の配置パス・親ディレクトリ・グローバル既定へ進まず、
読めない期待値ファイルとして deny する (リモート read のみのコマンドも)。同じ階層の配置パスに
正しいファイルがあっても、そちらでは照合しない。deny の文面は確かめられないパスと理由を示す。
無いとみなすのは、無いと確かめられたとき (行き先の無い symlink を含む) と、通常のファイルで
ない (ディレクトリなど) ときだけ。

## 親ディレクトリ遡及 (v0.4.0)

cwd 階層に accounts.local.json が無い場合、**親ディレクトリを 1 階層ずつ
遡って探す**。git worktree から作業しているとき、worktree 内に
accounts.local.json を複製しなくても親 repo (本体 checkout) の設定を
自動継承する。

```
/repo/main-checkout/.claude/cloud-account-guard/accounts.local.json
/repo/main-checkout/.worktrees/feature-x/   ← cwd (worktree)
```

worktree (`/repo/main-checkout/.worktrees/feature-x/`) で `gh pr create` を
叩いても、親 repo の accounts.local.json が継承されて検証が走る。
worktree 内に同名ファイルを置く必要は無い。

**仕様**:

- 探索順は cwd → cwd.parent → ... と 1 階層ずつ上る。最初に見つかった階層を採用
- cwd 階層に何かあれば親は見ない (cwd 優先)
- 同一階層に複数 tier が同居する場合は従来どおり fail-closed deny (D4)
- 安全側上限として `max_levels=10` (`core/paths.py`)
- **停止条件 (v0.12.0)**: 次の境界を越えて上らない。境界の階層自身は探索する
  - **git repo の toplevel** (`.git` **ディレクトリ**を持つ階層)
  - **submodule の root** (`.git` が `<common>/modules/<name>` を指すファイルの
    階層)。submodule も独立した repo の境界なので、superproject の設定は継承しない
  - **`.git` がファイルで、内容を判読できない階層** (`gitdir:` が読めない /
    上記いずれの形でもない)。分からない場合は止める側 (fail-closed) に倒す
  - **`$HOME` およびその上** (`/Users`, `/` 等)
  - **linked worktree は「その worktree を持つ repo の側」にだけ上る**
    (`.git` が `<common>/worktrees/<name>` を指すファイル)。worktree から
    親 repo の設定を継承する上記の運用はそのまま。`<common>` は git の
    common directory で、その名前は `.git` とは限らない — bare repository
    (`repo.git/worktrees/<name>`) や `--separate-git-dir` で初期化した repo
    (`/custom/gitdir/worktrees/<name>`) から作った worktree も同じく通過する
    (common directory の名前には依存しない)。
    **linked worktree かどうかは gitdir 側のメタデータで確かめる** — gitdir が
    実在するディレクトリで、git が置く `commondir` (common directory への
    パス) と `gitdir` (作業ツリーの `.git` への back-pointer) があり、
    back-pointer がいま読んでいる `.git` を指すこと。パスの末尾が
    `worktrees/<name>` でも、`--separate-git-dir` の gitdir が偶然その形の
    場合 (`/store/worktrees/repo` など) は独立した repo の main checkout なので
    境界にする。`<common>` は `commondir` の内容から求める。
    **通過するのは `<common>` が祖先側の repo のものと一致する場合だけ** —
    linked worktree は無関係な repo の中にも置けるため (repo A の中に
    repo B の worktree を追加する形)、形だけで通すと **repo A の設定を継承**
    してしまう。祖先の repo が別物、または祖先の `.git` を判読できない場合は
    worktree root で止める。祖先に repo が 1 つも無い場合 (workspace 直下に
    worktree を並べる配置) は従来どおり上る
  - 判定は `.git` と gitdir 内メタファイルの読み取りだけで行う (git コマンドは
    実行しない)。`gitdir:` の指す先は**種別と所属の判定にしか使わず、探索先
    としては辿らない**
  - **非互換**: repo の toplevel より上 (複数 repo を束ねる親ディレクトリ)、
    submodule から見た superproject、`$HOME` に置いた設定は継承されなくなる
    (`$HOME` の固定パス `~/.claude/cloud-account-guard/accounts.local.json`
    (グローバル既定。v0.13.0 から。0.22.0 までは `~/.claude/verify-cloud-account/` 側) が
    あればそれで検証する。無ければ未設定の扱い。書込は deny、リモート read のみのコマンドは警告)。**別の repo の中に
    置いた linked worktree から、その外側 repo の設定を継承していた場合も同じ**
    (worktree root で止まる)。各 repo の toplevel に複製するか `--path` で明示する
- 親採用時は deny / warn メッセージに `accounts.local.json は親ディレクトリ
  <絶対パス> から継承しています` の 1 行注釈が付く (verify 成功時は silent)

### builder も同じ解決を使う

`accounts_builder.py` の `init` / `show` / `set` / `remove` / `migrate` は
**dispatcher と同じ解決** (3-tier lookup + 親ディレクトリ遡及) で対象ファイルを
決め、解決したパスを dry-run / commit の出力の先頭に `対象: <パス>` として表示する。
読む側と書く側で解決を共有しないと、継承中の worktree で `set` が編集した service
だけを含む子ファイルを作り、dispatcher の遡及がそこで止まって**継承していた他の
service が一斉に未設定の扱い** (書込は deny、リモート read のみのコマンドは警告) になる。

- 継承中の `set` / `remove` / `migrate` は**継承元のファイル**を直接編集する
- 継承中の `init` は cwd 直下に作ると継承中の設定を覆い隠すため **exit 2 で拒否**
  し、`set` / `remove` (値の変更・削除) か `--path` (この階層専用の設定を作る) を
  案内する
- `--path <file>` を全サブコマンドに指定でき、明示時は解決を飛ばしてそのファイルを
  対象にする。受け付けるのは dispatcher が読む配置
  (`.claude/cloud-account-guard/accounts.local.json` と旧 2 パス) だけで、書込を
  伴うコマンドでは新パスのみ — 他の場所に書いても hook が読まないため。
  指定先が解決結果と食い違う場合は「hook が現在読むのは別ファイル」の警告を出す
  (近い側を指定すると現在の設定を覆い隠し、遠い側を指定すると書いても読まれない)
- 対象階層に複数 tier が同居する場合は builder も同じ理由で拒否する (D4)

**書込範囲についての注意**: 解決は最大 10 階層まで親を遡る
(`ANCESTOR_SEARCH_MAX_LEVELS`)。つまり accounts.local.json を持たないプロジェクトで
`set` を実行すると、遡及範囲内の祖先にファイルがあれば **そちらが編集対象になる**。
これは「hook が読むファイルを編集する」という意図どおりの挙動だが、builder の書込
範囲は cwd 配下に限られない。対象は出力先頭の `対象:` 行に必ず出るので、commit 前に
確認すること (この階層専用の設定にしたい場合は `--path`)。
`.gitignore` への追記も同じ階層に対して行われる。
v0.12.0 以降は遡及自体が git repo の境界 (toplevel / submodule root) と `$HOME`
で止まるため、**その repo の外や `$HOME` 以上が編集対象になることはない**
(submodule で作業していれば superproject 側も対象外)。linked worktree を repo の
外に置いている場合は、**その worktree を持たない別の repo の中にあれば worktree
root 自身**が、外側に repo が無ければ `$HOME` が境界になる (遡及はファイルシステム
の親方向にしか進まないため、`.git` ファイルの gitdir 先は辿らない)。

### グローバル既定 (v0.13.0)

プロジェクト側 (3-tier lookup + 親遡及) で何も見つからなかったときだけ、
**固定パスのグローバル既定**を読む:

```
~/.claude/cloud-account-guard/accounts.local.json
```

- user scope で install した直後に「設定していない全プロジェクトで deny」に
  なる状態から抜けるための明示的な fallback。`"$mode": "warn"` をここに書けば
  [warn モード](#検証モード-enforce--warn--off--v0130) を
  **`accounts.local.json` を持たないプロジェクト**の既定にできる。
  自前の設定があるプロジェクトはこのファイルを読まない (下の「常に優先」) ので、
  そちらを弱めるには `CLOUD_ACCOUNT_GUARD_MODE` かそのプロジェクトの `"$mode"` を使う
- **プロジェクト側の設定が常に優先**。グローバル既定を採用したときは deny / warn に
  `プロジェクトに accounts.local.json が無いため、グローバル既定 <絶対パス> を
  使用しています` の 1 行注釈が付く (verify 成功時は silent)
- 親遡及は従来どおり `$HOME` を越えない。グローバル既定は**遡及ではなく固定パスの
  専用経路**にしてある — 遡及で `$HOME` まで上らせると「たまたま `$HOME` 配下に
  あるプロジェクトだけが継承する」位置依存の挙動になる (v0.12.0 で塞いだ不具合)
- 認めるのは**現行パスのみ**。`~/.claude/accounts.local.json` /
  `~/.claude/accounts.json` (旧パス) はグローバル既定として読まない
  (無関係な `~/.claude/accounts.json` の継承が v0.12.0 で塞いだ不具合そのもの)
- 同一階層に複数 tier が同居する競合 (D4) は従来どおり fail-closed で deny し、
  グローバル既定では救済しない
- stat できないグローバル既定も、プロジェクト側の配置パスと同じく読めない期待値ファイル
  として deny する (v0.19.1。[3-tier lookup](#配置パスの-3-tier-lookup-v030))
- **builder はグローバル既定へ落ちない** (`init` / `set` / `remove` / `migrate`)。
  プロジェクト設定を作るつもりの編集が、利用者の全プロジェクトに効くファイルを
  書き換えてしまわないようにするため。グローバル既定は手で作るか `--path` で明示する
- 上の帰結として、**グローバル既定で検証されているプロジェクトに
  `accounts.local.json` を新規作成すると、グローバル既定のキーは継承されない**
  (キー単位のマージはしない = 書かなかった service は未設定の扱い。書込は deny、
  リモート read のみのコマンドは警告)。builder は
  新規作成になるとき「グローバル既定 `<path>` の N キーは継承されません」と警告し、
  `accounts-show` は「プロジェクトに無い」ときグローバル既定の存在と
  「hook はこのファイルで検証します」を表示する。グローバル既定を直したいときは
  `--path <グローバル既定のパス>` を付けて操作する

## パフォーマンス (短期キャッシュ)

PreToolUse は Bash の度に発火するため、`gh pr list && gh pr view && gh pr comment`
のような連打で毎回 `gh auth status` (〜500ms) や `aws sts get-caller-identity`
(〜1-3s) を走らせるとストレスになる。そのため **検証成功を 30 秒キャッシュ** する。

- 保存先: `$TMPDIR/cc-mp-cloud-account-guard/<service>-<sha256>.json`
  (epoch は同じディレクトリの `<service>.epoch`)
- この dir は、**自分の所有で、他のユーザーが書けない** (mode の group / other に w が
  無い) 実ディレクトリ (symlink でない) のときだけ使う (v0.19.1)。TMPDIR の無い Linux
  などでは共有の `/tmp` に置かれ、別のユーザーが先に同じ名前の dir を作れるため。作るときは
  0700。条件を満たさない dir は直さずに使わず、cache を読まず書かずに毎回検証する (umask 002
  の環境で以前の版が作った dir もこれに当たる。消せば次の実行で 0700 で作り直される)。
  [自動切替の記録](#自動切替-auto-switch--v0160)と、旧パスの移行案内を 1 日 1 回に絞る記録、
  検証の対象外のクラウド CLI の知らせをセッション × エージェントごとに 1 回に絞る記録
  (`unverified-cli-<sha256>.flag`。v0.31.0) も同じ dir に置くので、使えなければ記録しない
  (移行案内と対象外の CLI の知らせは毎回出る)
- 無効化: TTL 経過 / `accounts.local.json` の mtime 変化 / ファイル破損 (読めない
  entry や、値が期待した型でない entry も使わない。timestamp が数値でない・未来の時刻の
  ものなど。v0.19.1) / **アカウント状態を変えうるコマンドの検出** (v0.8.0、下記) /
  entry の epoch が現在と異なる
- **失敗 (deny) 状態はキャッシュしない** — 切り替え後は即座に再検証が走る
- キーには行頭の inline env と context option (`--profile` 等) に加え、**hook
  プロセスの env のうちアカウントを決める変数** (`AWS_*` / `CLOUDSDK_*` /
  `KUBECONFIG` / `GH_TOKEN` / `GH_CONFIG_DIR` 等) を含める (v0.17.0)。settings の
  `env` で[プロジェクトごとに固定](#プロジェクトごとにアカウントを固定する-公式の方法--v0170)
  した値を変えると、保存した時点で起動中のセッションに反映されるため、前の値での
  成功を使い回さない。値はキーの hash にだけ使い、cache ファイルには書かない

### ローカル設定ファイルからの現在値取得 (v0.13.0)

`gh` と `gcloud` は、**アクティブアカウントをローカルの設定ファイルに書いている**。
cache が無い (TTL 切れ / 初回) ときの現在値取得を、まずそのファイル読取で済ませる:

| service | 読むファイル | 読む値 |
|---|---|---|
| GitHub | `$GH_CONFIG_DIR` \| `$XDG_CONFIG_HOME/gh` \| `~/.config/gh` の `hosts.yml` | host ごとの `user:` (アクティブアカウント) |
| GCP | `$CLOUDSDK_CONFIG` \| `~/.config/gcloud` の `active_config` + `configurations/config_<name>` | `[core]` の `project` / `account` |

`gh auth status` は全 host のトークンを **API で検証**するためネットワーク往復が入り
(〜500ms)、オフラインでは失敗して deny に倒れていた。`gcloud config get-value` は
Python CLI の起動込みで 1 回 〜1s (dict 期待値では project / account の 2 回)。
ファイル読取ならどちらも無くなる。

**ローカル読取で通せるのは allow だけ**で、エラー方向 (不一致 / 未ログイン / 未設定) は
必ず CLI で取り直してから判断する。これにより:

- 速くなるのは成功ケース (= 大多数)
- ローカル読取を誤っても **deny を新造しない** (誤読のコストは「CLI を 1 回呼ぶ」だけで、
  deny 文面と判定は従来どおり CLI の出力から作られる)

次の場合はローカル読取を使わず、従来どおり CLI を実行する (速度は従来と同じ):

- GitHub: `GH_TOKEN` / `GITHUB_TOKEN` / `GH_ENTERPRISE_TOKEN` /
  `GITHUB_ENTERPRISE_TOKEN` / `GH_HOST` のいずれかが env にある /
  `hosts.yml` が読めない・想定の形でない / str 期待値で `github.com` が無く
  host が複数 (照合先が記載順に依存するため)
- GCP: `CLOUDSDK_CONFIG` と `CLOUDSDK_ACTIVE_CONFIG_NAME` 以外の `CLOUDSDK_*` /
  `GOOGLE_CLOUD_PROJECT` / `GCLOUD_PROJECT` / `GOOGLE_CLOUD_QUOTA_PROJECT` が
  env にある (プロパティの優先順位をエミュレートせず gcloud 自身に決めさせる) /
  設定ファイルが読めない・INI として解釈できない / configuration 名が
  gcloud の命名規則から外れる
- **両方共通: コマンド行頭の `HOME=...` が hook プロセスの `$HOME` と違う**
  (`HOME=/other gh pr create` など)。`HOME` は gh / gcloud のどちらも設定
  ディレクトリ解決に使うため、実行される CLI は**別の設定ファイル**を読む。
  hook 側のファイルで判断すると違うアカウントで allow しうるので CLI に委ねる
- GCP の `CLOUDSDK_*` 判定は **prefix 一致**なので、`CLOUDSDK_PYTHON` のように
  値に無関係な変数が 1 つでも立っていると CLI 実行に戻る (安全側だが「速く
  ならない」ように見える)。安全な変数の個別許可は、漏れが false allow になる
  ため入れていない
- `firebase` / `aws` / `kubectl` は従来どおり (aws は `sts` 呼出が必須、
  firebase / kubectl は既存の解決経路を変えていない)
- builder (`accounts-show` / `--from-cli`) は**常に CLI** を使う
  (期待値の提案・突合では「CLI 自身が報告する値」を優先する)

### 切替・ログイン系コマンドでの即時無効化 (v0.8.0)

次のコマンドを含む Bash を検出すると、実行前 (PreToolUse) の時点で当該 service の
成功 cache を **project / 期待値 / インライン env を問わず全て破棄**し、そのコマンド
自身の検証成功 (実行前の状態) も cache しない。CLI のアカウント状態はマシン全体で
共有される (`hosts.yml` / gcloud 設定 / configstore / kubeconfig / SSO token cache)
ため service 単位で破棄する。過剰な破棄のコストは再検証 1 回で済む。

| サービス | cache を破棄するコマンド |
|---|---|
| GitHub | `gh auth switch` / `login` / `logout` / `refresh` |
| Firebase | 引数ありの `firebase use ...` (`--clear` / `--add` 含む。引数なしは表示のみで除外) / `firebase login*` / `logout` (`npx firebase-tools ...` 形も同じ) |
| AWS | `aws sso login` / `sso logout` / `aws login` / `logout` / `aws configure ...` (表示系の `configure list` / `list-profiles` / `get` / `export-credentials` は除く) |
| GCP | `gcloud config set` / `unset` / `gcloud config configurations activate` / `create` / `gcloud auth login` / `activate-service-account` / `revoke` / `application-default login` / `revoke` / `gcloud init` |
| Kubernetes | `kubectl config use-context` / `set-context` / `set-cluster` / `set-credentials` / `set` / `unset` / `delete-*` / `rename-context`、および別 CLI / plugin 経由の kubeconfig 書換 `gcloud container clusters get-credentials` / `aws eks update-kubeconfig` / `az aks get-credentials` / `kubectx` / `kubectl ctx` |

切替セグメントが同じコマンド内の write と別セグメントでも (readonly の `gh auth login
--skip-ssh-key && gh pr create`、inline env が異なる `gh auth switch --user other &&
GH_HOST=... gh pr create`)、その service の検証成功は cache しない (判定は service 単位)。
[自動切替](#自動切替-auto-switch--v0160) (v0.16.0) も、hook 自身が切り替える前に
同じ規則で github の成功 cache を破棄する。

**並行する hook との競合 (epoch + in-flight 窓)**: 無効化は entry の削除だけでなく
service ごとの epoch (`<service>.epoch`、単調増加) を進め、切替を検出した時刻
(tombstone) を記録する。entry には verify 開始時点の epoch を記録し、読む側は epoch が
現在と違えば無視、書く側は開始時と現在の epoch が違えば書かない (無効化**前**に開始
した並行検証の結果を公開しない)。さらに tombstone から 60 秒 (`IN_FLIGHT_SEC`、定数)
は「切替の実行中」とみなして成功 cache を書かない (無効化**後**・切替完了**前**に
開始した並行検証の結果を公開しない)。hook は実行前にしか走らず切替コマンドの完了
時刻が分からないため時間で区切る。代償は切替後 60 秒間の毎回再検証。

従来 (〜0.7.3) は `gh pr list` (検証成功・cache 書込) → `gh auth switch --user other`
→ `gh pr create` が 30 秒以内なら cache hit で別アカウントの write が通っていた。

### 検証時間の予算 (v0.12.0)

hook は `hooks/hooks.json` の `timeout` (20 秒) を超えると Claude Code 側で打ち切られ、
**出力が破棄されてコマンドがそのまま実行される** (公式仕様上の fail-open)。
一方で各 CLI 呼び出しの timeout は 1 コマンドあたりの上限でしかなく、
`gh ... && aws ... && gcloud ...` のような複合コマンドはサービスごとに直列で
検証するため、合計は hook timeout を超えうる。CLI 未検出も CLI timeout も deny に
倒しているのに、ここだけ無音で通ると判定表に穴が空く。

そこで hook 1 回分に **総予算 15 秒** (`core/budget.py`) を置く。

- 各 CLI 呼び出しの timeout は「既定値」と「残り予算」の小さい方
  (下限 1 秒 — 0 秒 timeout は必ず失敗する無意味な呼び出しになるため)
- 予算を使い切った時点で、まだ検証していないサービスは **CLI を呼ばずに deny**
  (「検証時間の予算を使い切った」旨と対処を表示)
- cache hit と self-remediation (期待値への切替) は CLI を起動しないので、
  予算切れでもそのまま通る
- 予算切れの判定は検証の**手前**でしか行えないため、締切直前に始まった検証の分だけ
  超過しうる (上限 2 秒)。`15 + 2 < 20` が成り立つことはテストが `hooks.json` を
  読んで機械的に照合する

実運用で予算切れに当たるのは、先行するサービスの CLI が遅く予算を消費した場合。後続
サービスの timeout は残予算に合わせて短縮され、その deny は各サービスの timeout 文面で
出る (再試行すると検証済みサービスは cache hit する)。
それでも切れるならコマンドをサービスごとに分けるか、遅い CLI (未ログイン・ネットワーク
待ち) を解消してから再試行する。

## 守る範囲と守らない範囲

この plugin の目的は**うっかり予防**: Claude (や人) が**普段どおりに書くコマンド**で、期待外の
アカウント・プロジェクト・context に対して書き込んでしまうのを止めること。意図的に検証を
すり抜けようとする書き方 (シェルの珍しい構文・option の省略形・細工した設定ファイル) は守る
範囲に入れない。全部を防ぐことは目指さず、間違ったアカウントで進めてしまうのを検出できる範囲で
防ぐ。シェルの書き方の穴埋めには終わりが無く、直すたびに別の形の読み違い (それまで止めていた形を
通してしまう退行) を生むため。

**原則: 不一致が確定したときだけ deny し、解析で決められない形は deny しない。** 現行版には、
決められない形を止める判定がまだ残っている (相対パスの context をどのファイルに解決するかを
決められない形など)。これは順にこの原則へ揃える。

守る範囲 (例):

- 素のコマンド (`aws s3 rm ...` / `firebase deploy` / `kubectl apply ...`)
- `cd dir && <CLI>`、`;` / `&&` / `||` / `|` による連結
- 前置きの環境変数の代入 (`AWS_PROFILE=x aws ...`。値にコマンド置換を含む `X=$(cmd) aws ...` も。
  値は読まずに、後ろの CLI を照合する)
- よく使う透過 wrapper の素の形 (`sudo` / `env` / `npx` / `bunx` / `pnpm dlx` / `yarn dlx` / `npm exec` /
  `yarn exec` / `mise exec` など) と、CLI 名の前の `pnpm` / `yarn` (`pnpm firebase deploy`)
- コンテキストを指定する option の正式な綴り (`--profile` / `--project` / `--context` /
  `--kubeconfig` / `--config` など)

守らない範囲 (検出しない形。どれも普段どおりに書くコマンドではない):

- wrapper の long option の省略形 (`sudo --us root` / `sudo --chd sub` / `npx` の `--prefix` の省略形
  など) の後ろのクラウド CLI。検証が丸ごと抜ける
- option 付きの `env` (`env -C dir` / `env -u X`) と、option 付きの `mise exec` / `pnpm exec`
  (`mise exec -C dir -- gcloud ...` / `pnpm exec -C dir aws ...`) の後ろのクラウド CLI。
  option 付きは剥がさないので、候補にならず検証されない
- option 付きの `bunx` / `bun x` / `pnpx` / `pnpm dlx` / `yarn dlx` / `npm exec` / `mise x`
  (`bunx --bun firebase ...` / `npm exec --yes -- firebase ...` / `npm exec -c '...'`) と、`--` の無い
  `npm exec` で CLI 名の後ろに `-` で始まる語を置いた形 (`npm exec firebase deploy --project p`) の後ろの
  クラウド CLI。後者は npm がその語を自分の option として読んで CLI に渡さない (npm 11 で実測) ので、CLI が
  受け取る引数を決められない。どれも候補にならず検証されない (`npm exec -- firebase deploy --project p` は守る範囲)
- `yarn exec` で CLI 名の後ろに `-` で始まる語を置いた形 (`yarn exec firebase deploy --project p` /
  `yarn exec firebase deploy -- --project p` / `yarn exec firebase deploy '-P' p` / `yarn exec gh -h`) と、
  `yarn exec -- firebase deploy` / `yarn exec --silent gh ...` の後ろのクラウド CLI。yarn 1 はその語 (`--project p` は
  次の語も) を自分の option として読んで CLI に渡さず、`--` も落とす。yarn 2 以降はそのまま渡す (yarn 1.22 / 2.4 /
  3.8 / 4.9 で実測。`yarn exec firebase deploy --project p` の firebase は、yarn 1 で `deploy`、yarn 2 以降で
  `deploy --project p` を受け取る) ので、CLI が受け取る引数を決められない。`yarn exec -- <CLI>` はどの版も `--` の後ろを
  そのまま渡す (実測) が、`--` を含む形は一律に剥がさない。どれも候補にならず検証されない。引用符で包んだ
  語の中の空白の後ろの `-` (`yarn exec gcloud ... "a -b"`) でも同じく検証しない
- `yarn exec` の CLI 名の語に空白やシェルの記号を含めた形 (`yarn exec 'gh pr create && aws s3 rm ...'` /
  `yarn exec gh\;aws s3 rm ...`) の中のクラウド CLI。yarn 3 以降はその語をシェルのコードとして実行し
  (yarn 3.8 / 4.9 で実測)、yarn 1 / 2 はその名前の CLI を見つけられずに止まる。候補にならず検証されない
- yarn 4 が `.yarnrc.yml` の `injectEnvironmentFiles` (既定は `.env.yarn`) で読む env ファイルの env。yarn 4 は
  `yarn exec` / `yarn <CLI 名>` の CLI にそのファイルの値を渡し、前置きの代入 (`AWS_PROFILE=dev yarn exec aws ...`) より
  優先する (yarn 4.9 で実測) が、hook はそのファイルを読まず、前置きの代入と hook の env で照合する
- `yarn exec` と CLI 名の間に redirect を挟んで代入の形の語を置いた形 (`yarn exec 2>/dev/null NAME=値 <CLI> ...`)。
  yarn は CLI を起動しないが、hook は代入として読む (`npx` などと同じ読み)
- `pnpm` / `yarn` と CLI 名の間に option やサブコマンドを挟んだ形 (`pnpm -C app firebase ...` /
  `pnpm run firebase ...` / `yarn run firebase ...` / `yarn workspace app firebase ...`) と、別名の `npm x`
  (`npm exec` の別名) / `pn` / `pnx` (pnpm 11 の別名) の後ろのクラウド CLI
- `yarn <CLI 名>` の作業ディレクトリ。yarn 1 は package.json のあるディレクトリで CLI を起動する (yarn 1.22 で
  実測。yarn 2.4 / 3.8 / 4.9 は打ったディレクトリで起動する) が、hook はコマンドを打ったディレクトリの文脈で照合する
  (`yarn exec` はどの版も打ったディレクトリで起動するので、照合と揃う)
- `yarn <CLI 名>` で CLI 名の後ろに `--` の語を置いた形 (`yarn firebase deploy -- --project p` /
  `yarn firebase deploy --project p -- x`) の後ろのクラウド CLI。yarn 1 は `--` より前の語を自分の option として
  読み、`--` を落として残りを CLI に渡す (yarn 1.22 で実測。firebase はそれぞれ `deploy --project p` / `deploy x`
  を受け取る) が、hook は `--` の後ろの option を読まず、前の option は CLI に届くと読むので、照合先を決められない。
  候補にならず検証されない (`pnpm` は `--` も前の語もそのまま渡す (pnpm 8〜11 で実測) ので、
  `pnpm firebase deploy -- --project p` は直接打った形と同じに読む)
- 配列の閉じの直後に語の断片が続く代入 (`a=(1 2)y aws ...`) の後ろのクラウド CLI
- 期待値を登録した service で、identity env に値を読めない代入をした形 (`AWS_PROFILE=$(cmd) aws ...`)。
  代入した値ではなく起動時の値で照合する (起動時の値が期待どおりなら通る)。期待値が未登録でディレクトリ単位の
  固定で通す service では、この形を固定とみなさない
- コマンド語の無い heredoc に演算子が続く形 (`<<E && aws ...`) の後ろのクラウド CLI
- heredoc と、シェル経由の wrapper (`watch` / `sudo -s` / `sudo -i` / `npx -c`) に引用符で包んで渡す
  複合コマンドを組み合わせた形 (`cat <<EOF && watch 'true && kubectl ...'` + 改行 + 本文 + `EOF` /
  `watch 'cat <<EOF && kubectl ...'`) の中のクラウド CLI。候補にならず検証されない。heredoc だけの形
  (`cat <<EOF && kubectl ...`) と wrapper だけの形 (`watch 'true && kubectl ...'`) は守る範囲
- シェル経由の wrapper (`watch` / `sudo -s` / `sudo -i`) に引用符で包んで渡したコマンドの後ろに、リダイレクトや
  heredoc の後ろの段が続く形の、引用符の中のクラウド CLI (`watch 'kubectl delete po x' >/dev/null` /
  `sudo -s 'aws s3 rm s3://b/k' 2>&1` / `sudo -i 'aws s3 rm s3://b/k' <<EOF && true` + 改行 + 本文 + `EOF`)。
  引用符の塊が残り全体ではないので候補にならず、検証されない。heredoc の行が `; }` や `)` で終わる形
  (`{ watch 'aws ...' <<EOF; }`) も同じ。残り全体が 1 つの引用符の塊のとき (`watch 'kubectl delete po x'`) と、
  `|` / `&&` / `;` が続くだけのとき (`watch 'aws ...' | cat`) は検証する。リダイレクトを引用符の塊の手前に置く
  (`>/dev/null watch 'aws ...'`) か、wrapper を使わずにクラウド CLI を直接書くと検証される
- heredoc を置いた行で、`gh` (wrapper を剥がした後の gh も。`pnpm gh` / `bunx gh` など) の前置きの
  `GH_TOKEN=$(...)` と、後ろの段の値を読めない代入が並ぶ形
  (`GH_TOKEN=$(cmd) gh pr create <<EOF && X=$(cmd) aws s3 rm s3://b/k` + 改行 + 本文 + `EOF`) の、後ろの段の
  クラウド CLI。候補にならず検証されない (後ろの段の値を読めない代入が無い形と、`gh` の前置きが静的な値の形は
  検証する)。コマンドを行ごとに分ける (heredoc の行に後ろの段を置かない) と検証される
- シェル経由の wrapper に渡す引用符の中の、最初の段の前置きの代入 (`watch 'AWS_PROFILE=dev true ; aws s3 rm s3://b/k'`)
  は、中の全段に効くと読む。aws は `dev` で照合するが、実際のシェルでは前置きの代入は最初の段だけに効き、
  aws は起動時の値で走る (後ろの段の `;` の前に置いた代入 `'true ; AWS_PROFILE=dev aws ...'` は、その段の
  ものとして読むので一致する)。照合する値と実際の値を揃えるには、代入を aws の直前に置く
- heredoc の行が `; }` で終わり、その行の wrapper (`npx -c`) の中身が複数の段のとき
  (`{ AWS_PROFILE=prod npx -c 'true && aws s3 rm s3://b/k' <<EOF; }` + 改行 + 本文 + `EOF`)、wrapper の前置きの
  代入 (`AWS_PROFILE=prod`) を 2 段目以降の CLI に継がず、起動時の値で照合する (実際のシェルでは `prod` で走る)。
  中身が 1 段のときと、行が `)` で終わる形 (`( ... <<EOF )`) は継いで照合する。前置きの代入を使わず、
  代入を CLI の直前に置くか、行を `; }` で終えない形に書くと照合が揃う
- 代入の頭の珍しい形 (添字の中の空白 `a[ 1 ]=x aws ...`) の後ろのクラウド CLI
- 旧式の算術展開 `$[...]` の中に空白がある代入 (`X=$[1 + 2] aws ...`) の後ろのクラウド CLI。
  `$[` を 1 つの値として読まず、中の空白で値を終える (空白の無い `X=$[1+2] aws ...` は守る範囲)
- コマンド置換やバッククォートの中だけにあるクラウド CLI (`echo $(aws ...)` / 代入の値の
  `X=$(aws ...) cmd`。後ろの `cmd` は照合する)
- 二重引用符の中の置換 (`"$(...)"` / `"${...}"` / ``"`...`"``) の中の引用符の対応を、シェルと違って
  読む形の後ろのクラウド CLI。hook は二重引用符を中の次の `"` で閉じると読む (置換の中を新しい引用の
  文脈として読まない) ので、次の形で後ろの CLI が候補にならない: 先頭の代入の値の、中の二重引用符に
  空白がある形 (`X="$(git log --format="%h %s")" gh ...`)、中の二重引用符に空白の後ろの `#` がある形
  (`echo "$(printf "Refs #12")"; aws ...`。`#` から行末を comment と読む)、中の一重引用符に `"` があり
  `"` の数が対にならない形 (`echo "$(grep -c '"' f)"; aws ...`)、置換の中の heredoc の本文に対応しない
  `"` がある形 (`--body "$(cat <<'EOF'` + 改行 + `3" screens` + 改行 + `EOF` + 改行 + `)" && aws ...`)。
  中の引用符が対になる普段の形は守る範囲 (`gh pr create --title "$(git log -1 --format="%h %s")" && aws ...`、
  heredoc の本文に `'` があるだけの `git commit -m "$(cat <<'EOF'` … `)" && gh pr create`)
- 引用符で包まないコマンド置換 (`$(...)`) の中の heredoc の本文に、対にならない `'`・`"`・backtick や
  対応しない `(` がある形の後ろのクラウド CLI (`X=$(cat <<'EOF'` + 改行 + `don't` + 改行 + `EOF` + 改行 +
  `) && aws ...` / `gh pr create --body $(cat <<'EOF'` … `) && aws ...`)。hook は置換の中の heredoc の本文を
  読み分けず、本文の引用符と括弧で置換の閉じを見失うので、候補にならず検証されない。zsh ではこの形が走る
  (bash 3.2 は構文エラーにする)。置換を二重引用符で包む普段の形 (`X="$(cat <<'EOF'` … `)" && aws ...`) は、
  本文に `'` があっても守る範囲
- context option の値に `--` や `$(` を使う形 (`kubectl --context '$(id)' apply` /
  `kubectl --context -- apply`)。指定値として扱わず、現在値で照合する
- シェル展開で option を作る形 (ブレース展開の `firebase deploy -{j,P} prod` / `{-j,-P} prod`、
  ANSI-C クォートの `$'-P' prod`)。展開を読まず、既定の文脈で照合する
- firebase の `-P=prod` / `-c=path` と、`=` で始まる alias を置いた `.firebaserc` の組み合わせ
  (下の「既知の制限」も参照)
- kubectl の短い option の結合形で、値を取る文字が末尾にあり後ろの `--context` を値として飲み込む形
  (`kubectl -An --context x apply`)。hook は `x` で照合し、kubectl は既定の context で動く
- 読み取り権限の無いディレクトリへの、大文字小文字だけ違う綴りの `cd` (大文字小文字を区別しない
  FS)。firebase の `firebase use` の記録の照合で、綴りが firebase-tools と食い違いうる
- `-E` の無い `sudo` の後ろの aws / gcloud。`sudo` は環境変数を消すが、固定の判定は hook の
  環境変数 (`AWS_PROFILE` など) で固定とみなす
- `case` 文の分岐 (`case x in x) kubectl ...;; esac`) と関数定義 (`f() { kubectl ...; }; f`) の中の
  クラウド CLI。分岐や定義の先頭の語を含む 1 つの断片として読むので候補にならず、検証されない
  (`if` / `then` / `else` / `do` の中は検証する)
- firebase の CLI 名の直後に置いた短い option `-j` (`firebase -j use <alias>`)。`--json` は剥がして
  読むが `-j` は剥がさないので、切替として読まない。後ろの書込との連結 (`... && firebase deploy`) の
  deny と、切替後の cache の破棄に乗らない。`--json` と書く
- zsh の `&` の手前の `cd dir &&` の後ろのクラウド CLI (`cd dir && x & aws ...`)。zsh ではこの `cd` が
  今のシェルに残って aws は dir で走るが (bash ではサブシェルなので残らない)、hook は `&` で区切って
  起動したディレクトリの文脈で照合する
- gcloud の設定ディレクトリを指す環境変数 `CLOUDSDK_CONFIG` (と、deny 文面の profile 案内に使う
  `AWS_CONFIG_FILE`) の相対パス (`cd sub && CLOUDSDK_CONFIG=./c gcloud ...`)。hook は自分のカレント
  ディレクトリで読むので、コマンドの `cd` の行き先の側のファイルを読むとは限らない。絶対パスで書く
- gh のサブコマンドの後ろの `--help` を読み取りにする表 (gh 2.105 の組み込みのコマンドのパス) にあって、
  手元の gh では組み込みでない名前の alias / extension (gh 2.105 より古い gh で、後の版で組み込みになった
  名前を alias にしている形など)。組み込みのヘルプとみなして読み取りにするので、alias の中身は照合されない
- リポジトリにコミットされた期待値ファイル (tracked の旧パス `.claude/accounts.json` など) を細工して、
  [案内に使える形](#期待値へ向かう切替コマンドは許可-self-remediation) を通る値 (gh の host など) で別の行き先 (攻撃者の host への
  `gh auth login --hostname <host>` など) へ案内を誘導する形。期待値ファイルの内容は repo の管理者が決めた
  ものとみなし、案内の材料に使い続ける。値はコマンド構文の注入を防ぐために許容形で検証・クォートして
  いるが、意味の上での誘導は防がない

新しい形が見つかったときは「普段どおりに書くコマンドか」で範囲を決める。範囲外ならこの一覧に
足す (読みや追跡は増やさない)。

検証の対象外のクラウド CLI (`wrangler` / `cf`) は照合せずに通し、コマンドの位置に現れたら対象外であることを
`additionalContext` で知らせる (v0.31.0。同じセッションの同じエージェント (メインと各サブエージェント) では
CLI ごとに 1 回。deny はせず、deny したコマンドには足さない。検証モードにはよらない。一覧は
`hooks/cloud-account-guard/core/unverified.py`)。メインとサブエージェントは互いの記録で黙らない
(サブエージェントは同じ `session_id` を持ち、知らせはツールを呼んだエージェントにだけ届くため、hook input の
`agent_id` で分ける)。検証と同じ分解で見つけるので、検証が読まない起動の形 (`bunx --bun wrangler` /
`pnpm -C app wrangler` など) では知らせも出ない (`bunx wrangler` / `pnpm wrangler` / `yarn wrangler` は v0.32.0 から、
`yarn exec wrangler` は v0.33.0 から知らせる)。

## 既知の制限

- **`pnpm <CLI 名>` / `yarn <CLI 名>` は、同じ名前の package.json の script でも CLI として照合する** (v0.32.0)。
  pnpm / yarn は同名の script があればそちらを走らせる (pnpm 11 / yarn 1.22 で実測) が、hook は package.json を
  読まない。止まる条件は直接 `firebase deploy` と打ったときと同じで、期待値との不一致が確定したときに加えて、
  期待値が未登録で固定も無いときや相対パスの context を決められないときなど、今の版が止める側に倒す形でも
  止まる。script を走らせるつもりなら `pnpm run <名前>` / `yarn run <名前>` と書く (検証しない)。
  yarn は依存の bin (node_modules/.bin) だけを起動し、PATH の CLI は起動しない (yarn 1.22 で実測)。依存に無い
  CLI の `yarn gh ...` / `yarn aws ...` は yarn が `Command not found` で止めるが、hook は CLI として照合する
  (gh の自動切替も走る)
- **QUERY tier の「読むだけ」はコマンド形で判定している** (v0.14.0)。`aws \S+
  (describe|list|get)-*` をまとめて QUERY にしているため、表に無い「リモートの
  機密を返す read」があれば期待外アカウントでも警告のみになる。既知の形は
  [DISCLOSING の表](#検証の-3-tier--v0140)に載せて不一致 deny を維持している。
  それ以外で止めたいプロジェクトは
  [`"$readonly": "deny"`](#従来どおり-deny-させたいとき-readonly) を設定する
- **`kubectl get` のリソース指定を畳んだ形は Secret として検出できない**
  (v0.14.0)。`kubectl get cm,secret -o yaml` のようなカンマ結合と
  `kubectl get all -o yaml` は DISCLOSING に当たらず QUERY (警告のみ) になる。
  Secret 単体を指定する形 (`secret` / `secrets` / `secret/<name>`、オプションの
  前後どちらでも) は検出する
- **gcloud の QUERY はサブコマンド位置の書込動詞で打ち切る** (v0.14.0)。
  `gcloud functions deploy list` のように「`list` という名前のリソースへの書込」を
  リモート read と誤読しないための停止条件で、代償として group 名が停止語と同じ
  綴りの系統 (`gcloud deploy ...` = Cloud Deploy) は read でも QUERY にならず
  WRITE 扱いになる (v0.13.0 までと同じ扱いがこの系統だけ残る = 緩和が届かない
  だけで、新たな deny は生えない)
- **Firebase の `-P=<値>` / `-c=<値>` は hook と firebase-tools で読み方が違う**。hook は `=` の
  後ろを値と読み、firebase-tools (commander) は `=` から値と読む。ふつうは firebase-tools 側が
  実在しない project / ファイルを指して失敗するが、`.firebaserc` に `=` で始まる alias
  (`{"projects": {"=prod": "other-project"}}` など) や `=` で始まる名前の設定ファイルを置いた
  リポジトリでは、hook が `prod` で照合して通したコマンドが別の project で動きうる。`-P` / `-c` は
  `=` を付けずに書く (`-P prod` / `--project=prod`)
- **未知のオプションは「安全と証明できない」側に倒すので、判定は CLI の
  オプション表に追随しない**。`gh api` は安全なオプションの allow-list で
  読み取りを証明するため、新しいオプションが増えると (実際は読み取りでも)
  WRITE として検証される。誤 deny 側の失敗なので、気付いたら報告してほしい
- `gh auth status` / `firebase use` / `aws sts` / `gcloud config` /
  `kubectl config` の**出力フォーマット**に依存している。CLI 本体の
  major update で壊れる可能性あり。`gh auth status` は **gh 2.40 以上を推奨**
  (2.40 で複数アカウント対応の `Active account: true/false` marker が追加され、
  それ以前の単一アカウント形式にも fallback で対応するが、どちらの形式にも
  一致しない出力は「解釈できません」で deny する。`gh --version` で確認可)
- `bash -c '...'` / `eval` 内に埋め込まれた CLI 呼び出しは静的解析できず検証
  対象外 (透過 wrapper のリストに `bash` は含めていない)
- `gh auth <sub> --hostname <host>` / `--user <user>` のように**操作対象の
  host / アカウントをオプションで指定する形**は照合しない。これらは「どの
  アカウントで実行するか」ではなく操作の**対象**指定なので、検証はあくまで現在の
  アクティブアカウントに対して行う。アクティブが期待値なら別 host / 別ユーザー
  向けの操作 (例: `gh auth refresh --hostname ghe.example.com`) は allow される
  (str 形式の期待値で複数 host にログインしている場合は dict 形式にすると
  host ごとに照合できる)
- **`git push` / `git clone` / `git fetch` など `gh` 以外の GitHub 操作は
  検証対象外**。PATTERNS は `^gh(?=\s|$)` のみで `git` コマンド自体には一致
  しない。SSH 鍵や git credential helper 経由で別アカウントの GitHub へ
  push/clone しても、この plugin は関与しない (対象はあくまで `gh` CLI 経由の
  操作)
- **`aws-vault exec prod -- aws ...` のような別コマンド経由の実行は検証対象外**
  (v0.9.0)。`aws-vault` は `aws` とは別のコマンドなので発火しない。従来は
  `^aws\b` がハイフンを語境界として拾い、hook の**既定 profile**で `sts` を
  実行していた — 未設定なら永久 deny、既定が期待値なら実行 profile が prod でも
  allow という二重の誤りだったため、いったん対象外に倒した。`aws-vault exec <profile> --`
  を「`AWS_PROFILE` を合成する条件付き wrapper」として扱う対応は今後の課題
- **コンテキスト指定 flag の誤検出**: 値を取る未知の option の値がたまたま
  `--profile` 等に見える形 (`aws s3 cp --exclude --profile x`) では、その値を
  コンテキスト指定と誤読しうる。全サブコマンドの option 表を持てないため
  避けられない。誤読しても照合先が変わるだけで、検証自体はスキップされない
- heredoc 本文は解析対象から外す (開始行だけを候補に残す)。ただし **terminator が
  無い未終端 heredoc は heredoc として扱わない** — 「閉じていなければ末尾まで
  本文とみなす」方式だと delimiter を読み違えた瞬間に「以降すべて検証しない」に
  化けるため、通常の改行分割に戻す。結果として未終端 heredoc の本文行は候補に
  なりうる (bash 側でもエラーになる壊れたコマンドなので、そちらの代償を取る)
- `kubectl --kubeconfig <file> --context <ctx>` は `--context` の**名前だけ**で
  照合する。コンテキスト名は kubeconfig ファイルを跨いで一意ではないため、
  別ファイルの同名コンテキストは区別できない
- `if false; then aws ...; fi` のように**実行されない分岐**の中身も候補になる。
  予約語 (`then` / `else` / `do`) を剥がして本体を検証対象にしている副作用で、
  条件の真偽までは静的に評価しない (実行される場合を取りこぼさない側に倒している)
- 複数行にわたる配列の代入 (`a=(` + 改行 + `aws s3 ls` + 改行 + `)`) は、改行で区切って読むので
  要素の行も候補になり、止まることがある (要素なので実際には実行されない。止めすぎ側)
- 二重引用符の中の置換の中の二重引用符に区切り (`;` / `&&` / `|`) があると、そこで区切って読むので、
  後ろの語も候補になり止まることがある (`gh pr create --body "$(printf "a && aws s3 rm ...")"` の aws。
  文字列の一部なので実際には実行されない。止めすぎ側。守らない範囲の「二重引用符の中の置換」と同じ読み)
- シェルが構文エラーにする形 (配列の代入の中の here-document `a=(1 <<E` など) は、bash 3.2 (エラーの
  行を捨てて次の行から読み続ける) とも zsh (全体を解析できず何も実行しない) とも違う読みになることがある
- subshell 内のコマンド (`FOO=$(gh ...) cmd` の内側の gh) は検証対象外 (外側の `cmd` は
  v0.29.10 から検証する)
- **切替と write を同一コマンドで連結した形は「検証できない」ので deny する**
  (v0.15.0。[判定表](#切替と書込を同一コマンドに連結した形は-deny--v0150))。
  hook は実行前にしか動かないため切替後の状態は原理的に検証できず、通す側に倒すと
  期待外アカウントでの write がそのまま通っていた。代償として、切替先が静的に
  判らない形 (引数なしの `gh auth switch` / `--user $VAR` / login 系 / AWS 全般) は
  期待値への切替であっても連結を deny する = **誤 deny 側の失敗が残る**。
  切替を単独で実行すれば cache が破棄され、次の write は切替後の状態で再検証される
- **上の deny はコマンド行のセグメント分解に依存する。** `bash -c '...'` の内側は
  分解されないため、`gh auth switch --user other && bash -c 'gh pr create'` は
  検出されない。単独の `&` (バックグラウンド実行) は v0.29.0 から区切りとして扱うので、
  `gh auth switch --user other & gh pr create` は連結として止め、
  `gh pr create & aws s3 cp a s3://b` の aws 側も検証する (0.28.0 までは 1 セグメントに
  畳まれ、`&` の後ろのクラウド CLI がどれも検証されなかった)
- 期待値への切替と認識する形に、着地先を変える context option
  (`--configuration` / `--kubeconfig` / `--config`) 以外のオプション
  (`--flags-file` / `--context` / `--project` 等) を足したものは、従来どおり通常検証に
  落ちる ([self-remediation](#期待値へ向かう切替コマンドは許可-self-remediation))。
  gcloud の `--configuration` を付けた切替が指定した構成に書くことは gcloud の仕様からの
  推論で、実機の gcloud では確かめていない (v0.27.0)
- **60 秒 (`IN_FLIGHT_SEC`) を超える対話 login** (`gh auth login --web` /
  `aws sso login` 等のブラウザ認証) の最中に、同一 service の並行検証 (並列 Bash
  呼出) があった場合、その検証が login 前の状態で成功すると entry が書かれ、login
  完了後も最大 30 秒 (TTL) 有効になりうる。PostToolUse hook で実行後に無効化すれば
  閉じるが、全 Bash 呼出に Python プロセスが恒久的に乗るため採用していない
- Firebase の alias object 形式は `.firebaserc` の `projects` マップとの
  対応を前提にしており、ユーザー任意の key 名を受け付けるだけで "alias 名"
  自体のバリデーションはしない
- Firebase の `.firebaserc` を firebase-tools と同じ内容に読めると確かめられないとき
  (コメント・U+FEFF・URL を含む文字列など。条件は
  [pin-env](#プロジェクトごとにアカウントを固定する-公式の方法--v0170) の firebase と同じ)
  は、`--project` 付きのコマンドを deny し (先頭の文に期待値 (`期待=`) を示す。`--config` 付きの
  コマンドの deny と同じく許容形のものだけを示し、外れる値があれば出所を添える)、CLI から現在値を
  取れないとき (hook の PATH に `firebase` が無い `npx firebase` の構成など) は現在値を取得できない
  として deny する (v0.18.0。fail-closed。後者の文面は、hook の PATH に `firebase` が無ければ
  「firebase コマンドが見つかりません」、CLI はあるが現在値を返さなければ (非ゼロ終了など。
  timeout のときは「firebase use がタイムアウトしました」で、切替は案内しない)「現在のプロジェクトを取得できません」
  で、こちらは期待した project への切替を案内する。案内できる期待値は切替コマンドの形で示し、
  コマンドの形で 1 つも案内できないとき (`--config` 付きのコマンドでは常に) は `期待=` に許容形の
  値を示す。ローカルの設定からも決められなかった原因が `.firebaserc` を確かめられないことにある
  ときは、どちらの文面にも「ローカルの設定からも現在の project を決められません」の 1 文を添えて、
  `.firebaserc` を直せば抜けられることを示す (v0.26.0。文面だけで、判定は変えない))。判定は保守的で、
  firebase-tools が期待した project で動くファイルも対象になる。`--project`
  を外すと、コマンドはアクティブな project で動く (指定していた project ではなくなるので、deny が
  示す期待した project がアクティブかを確かめてから外す)。そのうえで hook の PATH から `firebase` を
  使えるようにすると (CLI 自身が `.firebaserc` を読んで答える) 照合できる
- CLI から現在値を取れないとき (`npx firebase` の構成など)、firebase.json に旧形式のキー
  `"firebase"` があるか、コメント付きなど厳密な JSON でない firebase.json では、現在値を取得
  できないとして deny する (v0.20.0。fail-closed)。判定は保守的で、firebase-tools がそのキーを
  使わない形 (configstore に切替先がある・値が空文字など) も対象になる。旧形式のキーは
  firebase-tools 自身も非推奨と警告し、`.firebaserc` への移行 (`firebase use --add`) を案内している。
  hook の PATH から `firebase` を使えるようにすると (CLI 自身が答える) 照合できる
- Firebase の `--config` / `-c` のパスが symlink を通るとき (symlink を通る絶対パス・symlink の
  ディレクトリを通る相対パス) は、deny の案内どおり `--config` のファイルのあるディレクトリで
  切り替えても効かないことがある (v0.18.0)。firebase-tools はこのパスの symlink を解かずに
  切替先を探すが、そのディレクトリで切り替えると実体のパスで記録されるため。hook も
  firebase-tools と同じ切替先を引く (CLI が無いときのローカル設定の解決も、symlink を解かない
  パスでだけ探す) ので、誤 allow にはならず、同じ deny が続く
- **direnv / `.envrc` / `CLAUDE_ENV_FILE` 経由の env は検証 subprocess に届かない**
  (PreToolUse hook には `CLAUDE_ENV_FILE` が渡らない harness 仕様)。回避策は
  [インライン環境変数の伝播](#インライン環境変数の伝播-v070) を参照
- **`hosts.yml` のアクティブアカウントのトークンが失効している**場合、
  ローカル設定ファイルからの現在値取得 (v0.13.0) では「そのアカウントで
  ログイン中」と読めるため allow になる。従来 (`gh auth status` が API 検証で
  失敗 → deny) との差分だが、失効トークンでは書き込み自体ができないため
  **別アカウントでの書き込みにはならない** (実行した `gh` が認証エラーで失敗する)
- 上と同じ理由で、**[ローカル読取](#ローカル設定ファイルからの現在値取得-v0130)
  で一致した場合は CLI を起動しない**ため、CLI 側でしか分からない失敗も検出
  されない。従来は deny だったものが allow になる例:
  - `gh` / `gcloud` が PATH に無い (従来は「コマンドが見つかりません」で deny)
  - `gh auth status` の出力を解釈できない (将来のフォーマット変更)
  - **複数 host のうち一部の host だけトークンが失効している** (dict 形式の
    期待値。従来はその host が `gh auth status` の一覧から落ちて per-host deny)

  いずれも「検証をすり抜けた後、実行した CLI 自身が失敗する」形に留まる
  (別アカウントでの書き込みにはならない)。CLI の存在や出力形式を hook 側で
  確かめるには `CLOUD_ACCOUNT_GUARD_MODE` とは別に毎回 CLI を起動する必要が
  あり、ローカル読取の利点 (速度・オフライン耐性) を打ち消すため採っていない
- `warn` / `off` [モード](#検証モード-enforce--warn--off--v0130) は検証を
  意図的に弱める設定。`off` の間は不一致でも通る (cache 破棄だけは継続する)
- **[自動切替](#自動切替-auto-switch--v0160) はコマンドの実行前 (PreToolUse) に
  行う**。切り替えた後にそのコマンドが permission の確認で拒否された・別の hook に
  deny された場合も、gh のアカウントは切り替わったまま残る (次に動く gh は
  そのアカウントを使う)
- 自動切替の**並行セッションのガードは自動切替の記録だけを見る**。手動の切替
  (ターミナルでの `gh auth switch` や、deny の案内に従って Claude が打つ切替) は
  記録されないので、手動の切替との競合は検出しない。また、hook が通したコマンドが
  permission の承認待ちで 60 秒を超えて止まっている間に、別のセッションが
  自動切替しうる (承認後のコマンドは切り替わった後のアカウントで動く)。記録の読み書きは
  ロックしないので、ほぼ同時に走った 2 つの hook はどちらも相手の記録を見る前に切り替え
  うる。別アカウントのセッションを同時に動かす運用では、これらの窓が残ることを前提にする
- `bash -c '...'` の内側などセグメント分解できない `gh` は検証対象外なので、
  自動切替も起きない

## 発火しなかったとき

1. `cat "${CLAUDE_PLUGIN_ROOT}/hooks/hooks.json"` でフックが登録されているか確認
2. `python3 "${CLAUDE_PLUGIN_ROOT}/hooks/cloud-account-guard"` を stdin 付きで
   手動実行し、対象コマンドで deny JSON が出るかスモーク (書込のコマンドで試す。
   リモート read のみの `gh pr list` などは、既定では deny せず警告にとどめる):
   ```bash
   echo '{"tool_input":{"command":"gh pr create"},"cwd":"/tmp"}' \
     | python3 "${CLAUDE_PLUGIN_ROOT}/hooks/cloud-account-guard"
   ```
3. `.claude/cloud-account-guard/accounts.local.json` の JSON 構文エラーを確認
   (破損時は deny)
4. 対象 CLI (`gh` / `firebase` / `aws` / `gcloud` / `kubectl`) が PATH に
   通っているかを確認
5. `CLOUD_ACCOUNT_GUARD_MODE=off` / `accounts.local.json` の `"$mode": "off"` に
   なっていないかを確認 ([検証モード](#検証モード-enforce--warn--off--v0130))。
   `off` では検証そのものを行わないため、deny も warn も出ない (検証の対象外のクラウド CLI の
   知らせ (v0.31.0) だけは出る)

hook の出力を確認するには `claude --verbose` でセッションを起動する
(hook の stdout/stderr がターミナルに表示される)。

### デバッグトレース (`CLOUD_ACCOUNT_GUARD_DEBUG=1`)

hook 実行時に環境変数 `CLOUD_ACCOUNT_GUARD_DEBUG=1` を立てると、コマンドの
分解結果を stderr に 1 行 JSON で出す (`claude --verbose` で確認可能)。

```json
{"segments": [{"segment": "gh pr create", "service": "github", "readonly": false,
   "tier": "write"}],
 "cache_hit": {}, "verify_ms": {"github": 12.3}, "auto_switch": {},
 "elapsed_ms": 13.1, "decision": "deny"}
```

- `segments`: 抽出した各セグメントと、マッチした service / readonly 判定 /
  [tier](#検証の-3-tier--v0140) (`readonly` / `query` / `write`)
- `cache_hit`: 成功 cache を使って verify を省略した service
- `verify_ms`: 実際に `verify()` を呼んだ service とその所要時間 (ms)
- `auto_switch`: [自動切替](#自動切替-auto-switch--v0160) を試みた service ごとの結果
  (`resolved` = 切替後の再検証が一致した / `switched` = 切り替えた
  `[host, 切替前, 切替後]` / `note` = 切り替えなかった・失敗した理由)
- `unverified`: 分解で見つけた検証の対象外のクラウド CLI の名前 (v0.31.0)。知らせを出したかどうか
  (同じセッションの同じエージェントで既に出した・deny した) には依らない
- `decision`: 最終的な判定 (`allow` / `deny` / `warn`)

判定表 (allow/deny/warn) 自体には一切影響しない観測専用の出力。

### 内部エラー時の fail-open

`dispatch()` 内で未捕捉の例外が起きた場合、従来は exit 1 (JSON 無し) になり
公式仕様上は non-blocking error として **無音で action が進行する** (fail-open
だが理由が分からない) だけだった。現在は例外を捕捉し、
`additionalContext` で `[cloud-account-guard] 内部エラーのため検証をスキップ
しました: <例外の型>: <メッセージ>` を明示し、同じ理由を stderr にも出す
(`claude --verbose` で確認可能)。いずれの場合も action 自体は fail-open で
進行する (実行を止めない)。

詳細な設計背景・拡張手順は [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) を参照。

## 互換性

- Python 3.11+ (標準ライブラリのみ)

## テスト実行

```bash
cd hooks/cloud-account-guard
python3 -m unittest discover tests
```

標準ライブラリのみで動く (pytest / pip install 不要)。

クラス単位・メソッド単位で 1 件だけ指定して実行することもできる:

```bash
python3 -m unittest tests.test_services.TestAws.test_match -v
python3 -m unittest tests.test_dispatcher.TestRouting -v
# pytest がインストールされていれば node id 指定も可能
pytest tests/test_services.py::TestAws::test_match -q
```

## ライセンス

MIT
