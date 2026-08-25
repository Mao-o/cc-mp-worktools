# Changelog

## 0.9.0

**候補コマンドの切り出しと解釈を 1 つの解析層として作り直した** (内部バックログ
6 件)。誤 deny (使えない) と検証バイパス (守れていない) の両方向に出ていた問題を
まとめて塞ぐ。方針は従来どおり lenient — 誤 deny は allow 方向に倒し、バイパスは
deny を増やすのではなく**「検証を走らせる」**方向に倒す。

### 変更内容

1. **コンテキスト指定 flag を検証に反映** (この batch の主眼) — `aws --profile
   other s3 rm` / `gcloud --project other run deploy` / `firebase deploy -P other` /
   `kubectl --context other delete pod` は、これまで hook の**既定コンテキスト**
   だけを照合していた。既定が期待値なら「検証は既定 / 実行は other」で allow され、
   誤アカウント事故の典型経路がそのまま通っていた (false-allow)。
   `core/cli_options.py` に `find_context_options` を追加し、候補の**行全体**から
   コンテキスト option の値を拾う。各 service は `CONTEXT_OPTIONS` を宣言し、
   AWS は検証コマンドに `--profile` を付与 (CLI の資格情報解決順を実行時と揃える)、
   GCP は `--project` / `--account` を直接照合し `--configuration` を引き渡し、
   Firebase は `.firebaserc` の alias を解決してから照合、Kubernetes は
   `--context` を直接照合し `--kubeconfig` を引き渡す。
   **GCP は key ごとに独立して上書き**する — project を flag で上書きしても
   account の期待値があれば account はアクティブ値と照合する (早期 return にすると
   新たな false-allow を作る)。gh は `CONTEXT_OPTIONS` を宣言しない
   (`--hostname` / `--user` は「どのアカウントで実行するか」ではなく操作**対象**の
   指定のため。README 既知の制限のとおり)。
   option の記法 (`--opt value` / `--opt=value` / `-Pprod` / `-P=prod` / `--` 終端 /
   値トークンの消費) は既存の `strip_leading_options` と `_option_name_value` を
   共有し、走査の打ち切り条件だけが違う (正規化は未知 option で打ち切る / context
   抽出は global option を後方からも拾うため走査を続ける)。値が変数展開等で静的に
   解決できないときは従来どおり既定コンテキストで照合する。
   dispatcher は context を inline env と同じく **grouping key と cache key に含める**
   — 含めないと `aws --profile a s3 rm x && aws --profile b s3 rm y` が 1 エントリに
   畳まれて後段が検証されず、既定 profile の成功 cache を別 profile が hit する。
   未配線の dead code だった `kubectl._context_override` は共通スキャナに統合して削除。

2. **heredoc 本文を候補にしない** — `core/command_parser.py` は改行を無条件の
   区切りにしており、`cat > deploy.sh <<'EOF' ... EOF` の本文 1 行 1 行が候補に
   なっていた。本文の CLI 例は実行されないため、スクリプトや PR 本文を heredoc で
   書くだけでファイル生成そのものが deny される (実際に本 batch の作業中、
   インストール済みの 0.8.0 が commit message 作成を 2 回 deny した)。
   `man 1 bash` の Here Documents 節に沿って `<<[-]word` を検出し、delimiter 行まで
   同一セグメントに取り込む (quote 付き / `\` 付き delimiter、`<<-` のタブ除去、
   1 行に複数 heredoc に対応。here-string `<<<` は対象外)。delimiter 行の改行では
   セグメントを閉じるので、heredoc の**次**のコマンドは従来どおり検証される。
   bare 形の delimiter は識別子に限定し、`(( x = 1 << 2 ))` の算術左シフトを
   heredoc と誤検出して後続セグメントを飲み込むことがないようにした。

3. **`command -v gh` を素通し** — `command` は透過 wrapper だが、`-v` / `-V` 付きは
   後続 CLI を**実行せずパスを表示するだけ** (`man 1 bash`: `command [-pVv]`)。
   剥がすと `gh` 単体の候補になり、インストール確認の定型句でアカウント検証が
   走って deny されていた。`-v` / `-V` (連結形 `-pv` 含む) があれば剥がさない。
   `command gh ...` / `command -p gh ...` / `command -- gh ...` は従来どおり検証。

4. **PATTERNS の `\b` を `(?=\s|$)` に** — `\b` はハイフンも語境界とするため
   `aws-vault exec prod -- aws s3 rm` が aws として発火し、hook の既定 profile で
   `sts` を実行していた。既定資格情報が無ければ永久 deny、既定が期待値なら実行
   profile が prod でも allow という**二重の誤り**。`gh-ost` / `kubectl-*` も同様。
   全 service を空白/終端の lookahead に統一した。Firebase だけは
   `npx firebase-tools deploy` が剥がし後に `firebase-tools deploy` になるため
   `-tools` を明示的に許可する。これにより `aws-vault` 経由の実行は
   「誤った検証」から「検証対象外」に変わる (README 既知の制限に明記)。

5. **検証バイパスだったセグメント形を正規化** — `(gh pr create --fill)` /
   `{ gh pr create; }` / `for f in *; do gh release upload ...; done` /
   `if gh pr create; then` / `! gh pr create` / `</dev/null gh pr create` /
   `2>/dev/null gh pr create` / `/opt/homebrew/bin/gh pr create` / `\gh pr create`
   は、いずれも行頭 anchored な PATTERNS に一致せず service=None (検証スキップ) に
   なっていた。先頭の `(` `{` `!` / 予約語 / リダイレクト、末尾の `)` `;` `}` `&`、
   コマンド名のパスと `\` を正規化する。末尾の括弧は**開き括弧と対応していない
   ときだけ**落とすので `gh pr view $(echo 1)` の引数は壊さない。

6. **実行ラッパを追加** — `timeout` / `nice` / `stdbuf` / `setsid` / `caffeinate` /
   `watch` / `xargs` を透過 wrapper に追加した。`timeout 30 gh pr create` /
   `... | xargs -I{} gh pr close {}` / `watch -n 5 kubectl get pods` は全て
   service=None で素通りしていた。`timeout` は flag だけでなく先頭の位置引数
   DURATION も消費する (GNU の文法に一致しない形なら剥がさず不透明のまま =
   従来どおり検証スキップ)。全て `_WRAPPER_ENV_CLASS` に `passthrough` として
   登録。option 表の根拠と、取り違えた場合の劣化方向 (常に「検証スキップ」側で
   誤 deny にはならない) は `docs/wrapper-env-audit.md` に記録した。
   `ionice` は cloud CLI と組み合わせる現実的な形も開発機の man page も確認できず、
   推測で allow-list を広げない方針から**追加していない**。

7. **独立レビューで検出した自作の退行を修正** — 上記 2〜6 を入れた直後に、
   0.8.0 と 0.9.0 の出力を突き合わせる敵対的レビューを別途かけ、**新規に作り込んだ
   false-allow を 5 経路**見つけて潰した。いずれも「安全側に倒したつもりが、
   検証そのものを消していた」型の誤り:
   - `npx firebase-tools@13.31.0 deploy` — CLI 名の lookahead が `@` で失敗し
     **検証対象から丸ごと外れていた**。`@<version>` を明示的に許可し、PATTERNS /
     READONLY / STATE_CHANGING / self-remediation で同じ prefix を共有する
     (片方だけ許可すると `login` が切替として認識されず成功 cache が残る)
   - `cat <<END-OF-FILE` / `<<EOF.txt` — delimiter を識別子形に限定していたため
     `END` までしか読めず、terminator が現れないまま**後続コマンドを全部**
     本文として飲み込んでいた。delimiter は 1 トークン丸ごと取る
   - `(( x = 1 << y ))` — 算術左シフトを heredoc と誤認して同様に飲み込む。
     **terminator の実在を確認してから本文として扱う**方式に変更し、
     「閉じていなければ末尾まで飲み込む」挙動そのものを廃止した
     (誤検出が「以降すべて検証しない」に化ける経路を構造的に塞ぐ)
   - heredoc 本文の行が option 走査に食われる — `--template-file - <<EOF` の本文に
     `--profile prod` があると **prod で検証して allow** し、YAML マニフェストの
     `- --context` / `- prod` があると**誤 deny** していた。本文は候補文字列からも
     落とす
   - context option の値として `-` (次の option / stdin) と `{}`
     (`xargs -I{}` の置換 placeholder) を採用していた

### テスト

448 → 530 件 (+82)。追加分は 22 パターンの mutation で検証済み — 各修正を 1 つずつ
元に戻すと対応するテストが必ず落ちることを確認した。

`TestWrapperEnvPropagationContract` に「passthrough 分類の全 wrapper が env 伝播
ケースを持つ」guard を追加した (D16 チェックリスト手順 6 の機械化)。追加した時点で
`builtin` の欠落を検出したのでケースを補っている。

ハイフン付き別コマンドのテストは deny の有無ではなく `verify` の**呼び出し回数**で
判定する。deny の有無だけを見ると、実 CLI がたまたま期待値にログインしている環境
では素通りして vacuous になる (mutation 実行で実際に検出漏れとして現れた)。

### 見送り

- `aws-vault exec <profile> --` を「`AWS_PROFILE` を合成する条件付き wrapper」として
  扱う対応 (今回は検証対象外に倒すところまで)
- `gh auth status --show-token` 等の開示系オプションの扱い、READONLY の前方一致
  regex 機構そのものの再設計 (いずれも別途)

## 0.8.0

**remediation loop の解消 (ログイン系の素通し / AWS 切替案内の修正) と、切替
コマンドでの成功 cache 即時無効化** (内部バックログ)。
verify-cloud-account は「記載済み service の不一致 × 書込系コマンド」だけを deny
し、それ以外は lenient に倒す方針に沿って、deny 文面が案内するコマンド自体が deny
される 3 つの経路を塞いだ。

### 変更内容

1. **認証取得系コマンドを検証スキップ (READONLY) に** (内部バックログ) — `aws sso login` /
   `aws sso logout` / `aws login` / `aws logout` / `aws configure ...`、`gh auth
   logout` / `setup-git`、`gh auth login` は SSH 鍵のアップロードが起きず、かつ
   scope 要求も無い形 (`--skip-ssh-key` / `--with-token` / `--git-protocol https`
   (`-p https`) 付き、かつ `-s` / `--scopes` 無し)
   のみ。判定は regex ではなく `github.is_readonly()` が token 列を走査して flag の
   **実効 boolean** を解釈する (`--skip-ssh-key=false` / `--with-token=0` のような明示
   false は無効、`=true|1|yes` と裸形は有効、`--git-protocol` は値が `https` のときだけ、
   繰り返しは後勝ち。gh は `--flag=false` を無効として扱い鍵アップロード経路に入る
   ため) (SSH git protocol を選ぶ login は既存の SSH 公開鍵を GitHub アカウントに
   アップロードしうる = 期待外アカウントへのリモート write。それ以外の login は通常
   検証し、deny 文面は `gh auth login --skip-ssh-key` / `--hostname <host>
   --skip-ssh-key` を案内する。aws / gcloud / firebase のログイン系に同種の副作用は
   無い)、`gcloud auth login` /
   `application-default login` / `revoke` / `set-quota-project` /
   `activate-service-account` / `revoke` (firebase の `login` / `logout` は 0.7.3 で
   対応済み。`npx firebase-tools login` の形も同じ扱いに揃えた)。いずれもクラウド
   資源を変更せず、ローカルの認証状態・profile 設定を作るだけ。従来は `^aws\b` 等に
   一致して検証され、SSO token 期限切れ等で `aws sts get-caller-identity` が失敗すると
   deny 文面が案内する `aws sso login --profile <profile>` 自体が deny され、Claude
   内で回復できなかった。`--with-token` / `--web` / `--key-file` 等のオプション付きも
   資源に触らないため同じ扱い。READONLY に載せた形については accounts.local.json
   未設定のプロジェクトでも deny しない。認証情報を出力する
   `aws configure export-credentials` /
   `gcloud auth application-default print-access-token` は `gh auth token` と同じく
   検証対象のまま。
   **素通しの基準は「コマンド名」ではなく「リモートに何も書かないと証明できる形」**
   — 名前で括るとオプション次第で write に化ける形まで巻き込むため、証明できない
   ものは READONLY に載せず、regex で表せない場合は `is_readonly()` で実効値を
   解釈する。この基準により **`gh auth refresh` は READONLY に含めない** — 保存済み
   認証情報の権限を拡張・修正するコマンドで、`gh auth refresh --scopes admin:org`
   のように CLI OAuth app の grant scope をアカウント側 (= リモート) で変更しうる
   ため、期待外アカウントに対してはリモート write と同じ扱いにする。`refresh` だけは
   READONLY から外れるので accounts.local.json 未設定なら他の write と同様に deny
   される (deny 文面は `refresh` を案内しないので remediation loop にはならない)。
   一方 `STATE_CHANGING` には残すため、実行後の成功 cache は従来どおり破棄される。
   **同じ論拠を `gh auth login` にも適用**し、`-s` / `--scopes` が付いた login も
   readonly から外した (gh 2.98 `gh-auth-login.1`: `-s, --scopes <strings>
   Additional authentication scopes to request`)。`gh auth login --skip-ssh-key
   --scopes admin:org` は SSH 鍵操作こそ起きないが、`refresh --scopes` と同じく
   アカウント側の OAuth grant を拡張する。`--scopes` は値を取る flag なので
   `=false` では無効化できず、付いていれば無条件に readonly から外す
   (`_login_is_keyless` が分離形 `-s <v>` / `=` 形 / 連結形 `-s<v>` を解釈し、
   分離形は値 token も消費する)。deny 文面が案内するのは `--skip-ssh-key` /
   `--hostname <host> --skip-ssh-key` の形だけなので remediation contract は壊れない。
   `gh auth logout` (`-h/--hostname` / `-u/--user`) はローカル `hosts.yml` のエントリ
   削除、`gh auth setup-git` (`-f/--force` / `-h/--hostname`) はローカル git config の
   credential.helper 設定で、取りうるオプションを含めていずれもアカウント側の OAuth
   grant には触れないため READONLY のまま (gh 2.98 の各 man page で確認)。
2. **「deny 文面が案内するコマンドは必ず allow 経路にある」contract テスト** (内部バックログ) —
   `tests/test_dispatcher.py::TestRemediationGuidanceContract` が各 service の verify
   を mock せず subprocess だけ差し替えて実際の deny 文面 (不一致 / 未ログイン / 未設定
   / SETUP_HINT) を生成し、案内コマンドを抽出して dispatcher に通す。案内コマンドは
   readonly または self-remediation として検証なしで allow されるか、AWS の
   `AWS_PROFILE=<profile> aws ...` はその env で検証されることを assert する。
   今後 deny 文面に allow 経路の無いコマンドを書くと機械的に落ちる。
3. **アカウント状態を変えうるコマンドで成功 cache を即時無効化** (内部バックログ) — 各 service
   に `STATE_CHANGING` パターンを追加し、dispatcher が `gh auth switch` / `login` /
   `logout` / `refresh`、引数ありの `firebase use ...` / `firebase login*` / `logout`、
   `aws sso login` / `aws login` / `aws configure ...`、`gcloud config set` / `unset` /
   `configurations activate` / `create` / `gcloud auth login` /
   `activate-service-account` / `revoke` / `application-default` / `gcloud init`、
   `kubectl config use-context` / `set-*` / `unset` / `delete-*` / `rename-context`
   を検出すると、PreToolUse 時点で `cache.invalidate(service)` により当該 service の
   entry を project / 期待値 / inline env を問わず全て破棄する (CLI のアカウント状態
   はマシン全体で共有されるため service 単位)。readonly 扱いのログイン系や
   self-remediation (期待値への切替) も無条件に破棄する (切替が失敗して状態が
   変わらなかった場合に古い成功が残らないように)。あわせて**切替コマンドを含む
   コマンドでは、その service の検証成功を cache しない** — 検証は実行前の状態に
   対して行われるため、これを cache すると直後の write が切替後の状態を検証せずに
   通る。判定は service 単位で、readonly の `gh auth login && gh pr create` や
   inline env が異なる `gh auth switch --user other && GH_HOST=... gh pr create` の
   write 側も cache しない。STATE_CHANGING は service の PATTERNS に一致しない候補
   にも当てるため、`gcloud container clusters get-credentials` /
   `aws eks update-kubeconfig` / `az aks get-credentials` / `kubectx` / `kubectl ctx`
   のように別 CLI / plugin が kubeconfig を書き換える形でも kubectl の cache を
   破棄する。表示系の `aws configure list` / `list-profiles` / `get` /
   `export-credentials` は状態を変えないため対象外。従来は `gh pr list`
   (検証成功・cache) → `gh auth switch --user other` → `gh pr create` が 30 秒以内
   なら cache hit で別アカウントの write が通っていた (0.7.3 の README 注記を解消)。
   cache ファイル名を `<service>-<sha256>.json` に変更 (service 単位の glob 削除の
   ため。旧形式のファイルは読まれず TTL 後に無害なゴミとして残るのみ)。
   **並行する hook との競合 (epoch + in-flight 窓)**: 無効化が entry の削除だけだと、
   切替 hook と並行して走った同 service の hook が旧状態を検証して新しい entry を
   書き、切替後に TTL 残り分だけ通る。`invalidate()` は service ごとの epoch
   (`<service>.epoch`、`max(現在 + 1, time_ns)` で単調増加) と切替検出時刻
   (tombstone) を先に書いてから削除し、entry には verify 開始時点の epoch を記録する。
   読む側は epoch が現在と違えば無視、書く側は開始時と現在の epoch が違えば書かず
   (`set_success(..., epoch=)`)、さらに tombstone から 60 秒 (`IN_FLIGHT_SEC`、定数)
   以内は切替の実行中とみなして書かない。PreToolUse は実行前にしか走らず切替の
   完了時刻が分からないため、この窓で「無効化後・切替完了前に開始した並行検証」の
   結果が公開されるのを防ぐ (残る穴は 60 秒超の対話 login 中の並行検証のみ。README
   既知の制限)。PostToolUse hook で実行後に無効化する案は、全 Bash 呼出に Python
   プロセスが恒久的に乗ることと plugin 再読込の非互換を避けるため採用しない。
   cache / epoch の書き込みは tmp + `os.replace` の atomic write。
4. **AWS deny 文面の切替案内を Claude Code で効く形に** (内部バックログ) — 従来は
   `export AWS_PROFILE=<profile>` を第一に案内していたが、Claude Code の Bash は
   呼出ごとに env を持ち越さず、hook は Claude 本体の env を継承するため、`export`
   しても次の `aws ...` は同じ理由で deny され続けた。案内を
   `AWS_PROFILE=<profile> aws ...` (行頭インライン。この形のみ検証に反映) →
   `aws sso login --profile <profile>` (1. で readonly 化) → 認証情報なしのときは
   `aws configure` の順に改め、`export` は「次の呼出に持ち越されず検証にも反映され
   ない (ターミナル側で設定して claude を起動し直す場合のみ有効)」の注記に変更した。
   さらに `~/.aws/config` (`$AWS_CONFIG_FILE` / `~` 展開対応) を走査し、期待 Account
   ID を `sso_account_id` (IAM Identity Center) または `role_arn`
   (`arn:aws:iam::<id>:role/...`) に持つ profile (`[default]` 含む) があれば
   `<profile>` を具体名にし、複数あれば一覧を添える。見つからなければ `<profile>`
   のまま `aws configure list-profiles` (readonly) で確認するよう案内する。config の
   profile 名以外の内容 (sso_start_url 等) は読んでも文面に出さない。
5. **Firebase の dict 期待値 + 現在値未解決のときの案内** — 従来は
   `firebase login && firebase use YOUR_PROJECT` (placeholder) を案内していたが、
   `firebase use YOUR_PROJECT` を文字どおり実行すると self-remediation に乗らず同じ
   deny を繰り返す。不一致時と同じ alias ごとの `firebase use <alias>  # → <project>`
   一覧を案内するようにした (contract テストで dict + 未解決ケースを固定)。あわせて
   期待値の形の検証 (空 dict / 非文字列) を CLI 呼出より前に移した。
6. **`npx firebase-tools ...` 形の整合** — wrapper 剥がし後の `firebase-tools ...` は
   PATTERNS (`^firebase\b`) に一致する一方、READONLY / self-remediation は
   `^firebase\s+` だったため `npx firebase-tools login` が検証され (未ログインなら
   deny)、`npx firebase-tools use prod` の切替も通常 write として cache されていた。
   READONLY / STATE_CHANGING / self-remediation を `^firebase(?:-tools)?\s+` に揃えた。
7. **AWS config 走査の堅牢化** — HOME 未設定で `Path.home()` が `RuntimeError` を投げる
   環境でも例外を漏らさない (漏れると hook が異常終了して無音 fail-open)。
   `AWS_CONFIG_FILE=~user/...` も展開する。
8. **CLI 名直後の global option を剥がして判定** (PR #43 Codex P1) — AWS CLI は
   `aws [global options] <command> <subcommand>` を受理し `--profile` は global
   option のため、`aws --profile default configure sso` / `aws --profile prod sso login`
   が READONLY / STATE_CHANGING の anchored pattern に一致せず、default profile の
   成功 cache が 30 秒残って別アカウントの write が通り、login 形も readonly に
   乗らなかった。`core/cli_options.py` の `strip_leading_options` が各 service の
   宣言 (`GLOBAL_OPTIONS_WITH_VALUE` / `GLOBAL_FLAGS`: aws / gcloud / kubectl /
   firebase。gh は root に `--help` / `--version` 以外の global option が無い) に
   従って先頭の option を剥がし、元の形と剥がした形の両方で READONLY /
   STATE_CHANGING を、剥がした形で self-remediation を判定する。剥がした option の
   値は将来の flag 照合 (内部バックログ) 用に返し、deny 文面の検出コマンドには元の形を表示
   する。未知の option が先頭にあれば剥がさず通常検証 (保守的)。`--help` /
   `--version` は宣言しない (剥がすと `aws --version` が readonly から外れるため)。
   **boolean flag の `--flag=<bool>` 形も剥がす** (Codex R5 P1-A) — Go の pflag /
   cobra は bool に分離形 `--flag value` を許さず `=` 形のみ受け付けるため、
   `kubectl --insecure-skip-tls-verify=true config use-context other` は正当な
   呼び出し形。当初は `name in flags and not eq` として `=` 付きを「未知 option」
   扱いで無変更にしており、STATE_CHANGING に当たらず切替後も古い成功 cache が
   TTL 分残っていた。**値の真偽で剥がすかどうかは変えない** (剥がす目的は後続の
   subcommand を見つけることで、flag の実効値は「どの操作が走るか」に影響しない)。
   これは 内部バックログ の `--skip-ssh-key=false` (`github.is_readonly`) と同じ失敗形が
   `cli_options` 側に残っていたもの。
9. **README** — readonly 一覧に認証取得系を追加、self-remediation 節の「成功 cache 内は
   再検証されない」注記と `export AWS_PROFILE` の記述を実装に合わせて更新、cache 節に
   「切替・ログイン系コマンドでの即時無効化」の表を追加、解析対象に「CLI 名直後の
   global option」を追記、既知の制限に「期待値以外への切替 + write を同一コマンドで
   実行すると実行前の状態で検証される」を追記。
10. **gcloud の release track 形を READONLY / STATE_CHANGING に含める**
    (Codex R5 P1-B + 自己 sweep) — gcloud はほぼ全てのコマンドを `alpha` / `beta`
    (一部 `preview`) でも公開しており、同じ操作が同じ副作用で走る。anchored pattern を
    GA 形だけで書いていたため track 形がすり抜けていた。Codex の指摘は cross-CLI の
    `gcloud beta container clusters get-credentials` (kubectl の cache を破棄すべき形)
    だったが、自己 sweep の結果 **gcloud 自身の STATE_CHANGING 全パターンが同じ穴**
    だった: `gcloud beta config set project other` が切替として検出されず、warm な
    成功 cache が残ったまま次の `gcloud run deploy` が通っていた。共有の
    `_TRACK = r"(?:(?:alpha|beta|preview)\s+)?"` を READONLY (`auth list` /
    `config get-value` / 認証取得系) と STATE_CHANGING (`config set|unset` /
    `configurations activate|create` / `auth` 系 / `init`) の各 anchored pattern に
    入れ、kubectl 側の cross-CLI pattern には `(?:(?:alpha|beta)\s+)?` を入れた。
    track × command の実在は SDK 生成物 `data/cli/gcloud_completions.py` の
    command tree で確認 (config / init は alpha・beta・preview、auth 系と
    container clusters get-credentials は alpha・beta)。存在しない組に当たっても
    実行が失敗するだけなので prefix は全パターンで統一した。
    **資格情報を出力する `auth print-access-token` /
    `auth application-default print-access-token` は track 形でも READONLY にしない**
    (0.8.0 の carve-out を track 追加で広げていないことをテストで固定)。

### 非互換性

- 切替・ログイン系コマンドの直後は必ず再検証が走る (cache hit しない)。切替コマンド
  自身も (期待値以外への `gh auth switch` 等は) cache を使わず毎回検証する。
- cache ファイル名の形式変更。`$TMPDIR/cc-mp-verify-cloud-account/` の旧形式
  (`<sha256>.json`) は参照されなくなる。
- 切替・ログイン系コマンドの検出から 60 秒間 (`IN_FLIGHT_SEC`) は当該 service の成功
  cache を書かない (毎回再検証)。
- 素の `gh auth login` (`--skip-ssh-key` / `--with-token` / `--git-protocol https` 無し)
  は 0.8.0 でも従来どおり検証対象 (readonly にしたのは上記 3 形のみ)。
- `-s` / `--scopes` 付きの `gh auth login` も検証対象 (`--skip-ssh-key` 等が付いていても
  readonly にならない)。scope 要求はアカウント側の OAuth grant を拡張するため。
- `gh auth refresh` も 0.8.0 で readonly にはしない (従来どおり検証対象)。
  accounts.local.json 未設定 / 期待アカウント以外がアクティブなら deny される。
  `STATE_CHANGING` には含めるので成功 cache の即時破棄は従来どおり働く。

### 対象外 (別項で扱う)

- 期待値以外への切替と write を同一コマンドで実行する形
  (`gh auth switch --user other && gh pr create`)。hook は実行前の状態でしか検証
  できないため、README の既知の制限に記載した。
- 読み取り専用サブコマンド (`gh pr list` 等) の不一致時 allow + 警告 (内部バックログ)、
  `--profile` / `--project` flag の解析 (内部バックログ)。

### テスト

- `tests/test_cache.py` に 4 件 (`invalidate` の service 単位破棄 / 他 service 非影響
  / project・期待値・env 横断 / prefix 衝突なし)
- `tests/test_dispatcher.py::TestAccountSwitchInvalidation` 13 件 (切替 → write の
  再検証、切替自身を cache しない、readonly ログインと self-remediation の無効化、
  全 service の切替表、readonly 切替 + write の複合や inline env 違いの別 target でも
  cache しない、別 CLI 経由の kubeconfig 書換、`npx firebase-tools` 形、他 service
  非影響、`firebase use prod && firebase deploy`、表示系 readonly は cache 維持、
  未設定時の無効化)。**回帰ガードの実効性修正**: 上記と `TestLeadingGlobalOptions` の
  無効化テストは subTest 間で cache dir を共有しており、前 row の tombstone
  (in-flight 窓) が warm-up の cache publish を抑止するため、STATE_CHANGING を壊しても
  green のままだった (24 ケースが vacuous)。row ごとに cache dir を作り直し、warm-up が
  cache を publish したことを直接 assert するようにして、全ケースが mutation で
  bite することを確認した
- `tests/test_dispatcher.py::TestLoginCommandsReadonly` 3 件 (ログイン系 30 コマンドが
  検証なしで allow / 未設定でも allow / 類似 write・認証情報出力・`gh auth refresh`
  (裸形 / `-s repo` / `--scopes admin:org` / `--remove-scopes repo`) は従来どおり deny)
- `tests/test_dispatcher.py::TestRemediationGuidanceContract` 14 件 (上記 2.)
- `tests/test_services.py` に `TestAws` 5 件 (案内の順序と `export` 不在 / config から
  の profile 解決 / 認証情報なし / HOME 不能時に例外を漏らさない) +
  `TestAwsProfileScan` 8 件 + `TestFirebase` 3 件 (dict 未解決の alias 案内 / 不正な
  形で CLI を叩かない / `firebase-tools use` の self-remediation) +
  `TestAuthCommandPatterns` 3 件 (READONLY / STATE_CHANGING のパターン表)
- `tests/test_cli_options.py` 新設 11 件 (global option 剥がしの形: `--opt value` /
  `--opt=value` / flag / 短縮形 / `--` 終端 / 未知 option・値欠落・quote 不正は無変更 /
  quote 保持 / `--help` `--version` は剥がさない) +
  `tests/test_dispatcher.py::TestLeadingGlobalOptions` 7 件 (global option 先行形の
  readonly 判定 / 切替での cache 無効化 (Codex P1 の再現) / 別 CLI 経由 kubeconfig /
  self-remediation / deny 文面は元の形 / 未知 option は通常検証 / `--version` 維持)
- `tests/test_cache.py` に epoch / in-flight 窓 10 件 (無効化で epoch が進み旧 epoch の
  結果は書かれない / 窓内は現在の epoch でも書かない / 窓を過ぎれば書ける / 窓は
  service 別 / 削除と競合して残った entry の無視 / epoch ファイル消失後の単調性 /
  後方互換 / 破損 epoch / service 別 epoch / tmp 残骸なし) +
  `tests/test_dispatcher.py::TestConcurrentInvalidationRace` 3 件 (検証中に無効化が
  走った結果は公開されない / 無効化後・切替完了前に開始した検証は窓内で公開されない /
  窓を過ぎれば cache が再開) +
  `tests/test_main.py` 新設 4 件 (stdin → stdout E2E: 未設定 write の deny JSON /
  readonly login は無出力で epoch を進める / 非対象コマンドと不正 JSON は無出力)
- `tests/test_services.py::TestGithubLoginKeyless` 5 件 (login flag の実効 boolean:
  有効 18 形 / 無効 18 形 / quote 不正時の fallback / `-s` `--scopes` 付きは
  鍵操作抑止 flag があっても readonly にしない 11 形 / `--` 以降の `--scopes` は
  引数扱い)
- 新規 91 件のうち 60 件は旧実装 (0.7.3 の cache / dispatcher / services に差し替えて
  実行、レビュー対応分は各修正前の実装) で fail することを確認済み。残りは旧実装でも
  成り立つ契約の固定 (既存 self-remediation / readonly が案内コマンドを通すこと、
  表示系 readonly が cache を保つこと、他 service の cache に影響しないこと、類似 write
  の deny、検出コマンドの表示、未知 option の保守的扱い、tmp 残骸なし)

テスト 352 → 443 件。

## 0.7.3

**Firebase の現在値解決順を firebase-tools 本体に合わせる (P0 bug fix,
`services/firebase.py`)**: v0.3.2 以降は `.firebaserc` の `default` を
`firebase use` の出力より優先していたが、firebase-tools (`lib/command.js` の
applyRC) は `--project` → configstore の activeProjects (`firebase use
<alias|project>` で更新) → `.firebaserc` の順で解決し、`firebase use X` は
configstore しか書き換えない (`.firebaserc` は `--add` / `--alias` 時のみ)。
このため 2 つの誤判定が起きていた:

- **false-allow**: 期待値が `.firebaserc` の default (例 `proj-dev`) のとき、
  `firebase use prod` で切り替えていても default で照合され `firebase deploy` が
  prod に通っていた
- **永久 deny**: 期待値 `proj-prod`・default `proj-dev` では `firebase use proj-prod`
  を実行しても「現在=proj-dev」と判定され続けていた

### 変更内容

1. **解決順の反転** — `firebase use` (非 TTY では解決済み project ID を 1 行出力)
   を最優先し、CLI から取れないとき (hook の PATH に無い / 非ゼロ終了 / 出力が空 /
   複数行ヘルプ) だけローカル設定に fallback する。`get_active_account()`
   (builder の `accounts-show` / `accounts-init` が使用) も同じ順。
2. **fallback も applyRC と同じ規則・同じ情報源にする** — `.firebaserc` だけを
   読むと `firebase use <alias>` の切替 (configstore にしか無い) を見落として
   default で照合してしまうため、configstore
   (`$XDG_CONFIG_HOME` または `~/.config` 配下の `configstore/firebase-tools.json`
   の `activeProjects`、project_dir から親方向に探索) の切替先を `.firebaserc` の
   alias で解決 → 無ければ alias が 1 つならその値 → `default` の順にした。
   `npx firebase ...` / `pnpm exec firebase ...` のように hook 側に `firebase` が
   無い構成でも切替を見落とさない。configstore は JSON として読むが
   `activeProjects` 以外は使わず、内容をメッセージに出さない。
   探索の起点は firebase-tools の `detectProjectRoot` と同じく `firebase.json` を
   親方向に探した project root (無ければ cwd) で、monorepo の子ディレクトリで
   Claude を起動しても親の `.firebaserc` で alias を解決できる。
   `.firebaserc` / configstore が不正な形 (list /
   非 UTF-8 等) でも例外にせず未解決扱い (従来は top-level が list だと
   AttributeError で hook が異常終了 = fail-open)。
3. **timeout の専用メッセージ** — `firebase use` が 10 秒でタイムアウトしたときは
   `.firebaserc` に fallback せず「Firebase: firebase use がタイムアウトしました。
   再試行するか、ネットワーク接続を確認してください。」で deny する (他 service
   の timeout 文言と同形)。従来は timeout が「firebase login && firebase use ...」
   の誤案内になっていた。fallback しないのは、`.firebaserc` が configstore の
   切替を知らないため timeout 経路だけ false-allow が残るのを防ぐため (fail-closed)。
4. **`firebase login` / `logout` を検証スキップ (READONLY) に** — CLI 優先化に伴う
   regression 防止。未ログイン (`firebase use` が requireAuth で非ゼロ終了) かつ
   configstore が期待外に切替済みだと、`firebase login` 自体が fallback の
   configstore 値で deny され、案内される `firebase use <期待>` も認証必須で失敗する
   デッドロックになるため。`login:ci` / `login:add` / `login:use` / `logout` を含む
   (project を変更しない認証操作。直後の write は次回 hook で再検証される)。
5. **CLI が PATH にあるが実行できないときに hook が異常終了しない** —
   `subprocess.run` が投げる FileNotFoundError 以外の `OSError` (実行権限なし /
   形式不正等) を firebase は「CLI 不可 → ローカル設定 fallback」に乗せ、他 4 service
   (gh / aws / gcloud / kubectl) は「コマンドを実行できません」の deny にした。
   従来は例外が漏れて hook が非ゼロ終了し、PreToolUse の無音 fail-open になっていた。
6. **README** — 期待値取得の表と解説、readonly 一覧、切替後の再検証に関する注記
   (成功 cache 内は再検証されない) を実装に合わせて更新。
7. **`firebase use` の cwd を project root に固定** — 従来は hook / builder
   プロセスの cwd を継承していたため、builder (`accounts-init` / `accounts-show`)
   を project_dir の外から起動すると CLI 優先化によって無関係なディレクトリの
   project を報告・書込しうる。`firebase.json` を親方向に探した project root
   (無ければ project_dir) を cwd にし、ローカル設定 fallback と解決の起点を揃えた。
   project_dir が存在しない場合は cwd 指定の `OSError` を CLI 不可として扱い、
   従来どおり fallback する。

### 非互換性

- `firebase use <alias>` で default 以外に切り替えているプロジェクトでは、期待値が
  default の project ID のままだと deny に変わる (本来の意図どおり)。`.firebaserc`
  の default で運用しているプロジェクトは挙動不変。
- `.firebaserc` があっても毎回 `firebase use` (認証チェックを含む) が走るように
  なった (30 秒の成功 cache は従来どおり)。オフライン等で 10 秒を超えると
  timeout deny になる。

### 対象外 (別項で扱う)

- 切替コマンド直後 30 秒以内の成功 cache による false-allow (`firebase use prod &&
  firebase deploy` や、切替を allow した直後の deploy が cache hit で検証されない)。
  全 service 共通の cache 無効化として別途対応する。
- `--project` / `-P` flag による実行時 override。

### テスト

- `tests/test_services.py::TestFirebase` に 25 件追加 (CLI 優先 / false-allow と
  永久 deny の解消 / timeout 専用メッセージ / 空出力・非ゼロ終了・実行不可
  (PermissionError)・単一 alias の fallback / configstore の切替先参照 (CLI 不在・
  非ゼロ終了・親ディレクトリ・実体パス・env の `XDG_CONFIG_HOME`・破損時) /
  `firebase.json` を起点にした project root 解決 / `firebase use` の cwd 固定と
  project_dir 不在時の扱い / 不正な `.firebaserc`)
- `tests/test_services.py::TestCliExecErrors` 新設 4 件 (gh / aws / gcloud / kubectl
  の `OSError` が deny 文字列になり例外が漏れない)
- `tests/test_active_account.py::TestFirebaseActiveAccount` に 5 件追加 (CLI 優先 /
  timeout は None / configstore 参照 / builder 経路の cwd 固定)、1 件を fallback
  意味論の名前に変更
- `tests/test_dispatcher.py::TestFirebaseResolutionOrderE2E` 4 件追加
  (`firebase.verify` を mock せず subprocess だけ差し替えた end-to-end。npx 構成、
  未ログイン + configstore 切替済みでの `firebase login` allow / `deploy` deny を含む)
- 新規 38 件のうち 33 件は旧実装 (main) で fail することを確認済み (残り 5 件は
  fallback 条件の仕様固定)

テスト 314 → 352 件。

## 0.7.2

**D11 インライン env 伝播の透過 wrapper 挙動を監査・体系化 (ドキュメント + 回帰テスト)**:
v0.7.0 (D11) で導入した「行頭インライン env を検証 subprocess に伝播する」設計が、
PR #33 の Codex レビュー 3 round + 8zr で連続して env 関連 edge case
(複合コマンド bypass / 透過 wrapper 跨ぎの override 漏れ / sudo の env scrub 未考慮)
を生んだことを受け、**透過 wrapper × env 挙動を全数監査し、再発防止の guard を
入れた**。コード挙動の変更は無し (8zr の sudo 修正で現リストは健全と判定)、
分類の固定化と将来 wrapper 追加時のガードのみ追加。

### 変更内容

1. **wrapper env 伝播クラスの分類宣言** (`core/command_parser.py`) —
   `_WRAPPER_ENV_CLASS` を追加し、全透過 wrapper を `"passthrough"` (継承 env を
   素通す → pre-wrapper env を伝播してよい) / `"conditional_scrub"` (フラグ依存で
   scrub する → 無条件には伝播しない、現状 `sudo` のみ) に分類した。これは
   parser の振る舞いそのものではなく**分類の宣言**で、実際の剥がし/収集は従来の
   `_strip_one_wrapper` / `_normalize_segment` / `_sudo_preserves_env` が行う。
   `env` は `_strip_one_wrapper` で特別扱い (オプション無しのみ剥がし、`env -i` /
   `env -u` / `env --` は opaque) するため意図的に分類対象外。
2. **分類 guard の回帰テスト** (`tests/test_command_parser.py`) —
   `TestWrapperEnvClassificationGuard` (4 件) が「`_WRAPPERS_SINGLE/TWO/THREE` の
   全要素が `_WRAPPER_ENV_CLASS` に分類済み」「死にエントリが無い」「分類値が既知」
   「conditional_scrub は sudo のみ」を assert する。将来 wrapper を `_WRAPPERS_*`
   に足したのに分類を忘れると即座に落ちるため、env 挙動の検討漏れによる
   whack-a-mole 再発を機械的に防ぐ。
3. **wrapper env 伝播の contract テスト** (`tests/test_command_parser.py`) —
   `TestWrapperEnvPropagationContract` (5 件) が passthrough wrapper の pre-wrapper
   env 伝播 / sudo の scrub・preserve / `env -i`・`-u`・`--` の opaque 維持 /
   オプション無し `env` の収集を 1 つの表で固定化する。
4. **監査ドキュメント追加** (`docs/wrapper-env-audit.md`) — wrapper × env 挙動の
   完全な表 (実機 probe 根拠付き)、伝播可否の方針、誤 deny 回避ポリシーとの整合、
   **将来 wrapper (`ssh` / `docker run -e` / `kubectl exec` 等) 追加時のチェック
   リスト**を記録。`ssh` / `docker` / `kubectl exec` のように別実行コンテキストへ
   移送する wrapper は「ローカル行頭 env が届かない」ため透過 wrapper に足さない
   (検証スキップ = 誤 deny 回避ポリシーと整合) という原則を明文化。
5. **README 追記** (`README.md`) — インライン env 伝播の節に「透過 wrapper を
   跨ぐときの env 伝播」サブセクションを追加し、`sudo` の env scrub 挙動を利用者
   向けに説明 + 監査ドキュメントへのリンクを張った。

### 監査結論

8zr の `sudo` scrub 補正により**現行 wrapper リストの env 挙動はすべて正しく分類・
処理されている**。`sudo` が唯一の env scrub wrapper、`env -i`/`-u`/`--` が唯一の
env reset 形式で、どちらも対応済み。残る passthrough wrapper は実機で env 素通しを
確認した。過剰な再設計 (全 wrapper allow-list 化等) は誤 deny を増やすため不採用とし、
分類 guard で将来の再発を防ぐ方針とした。コードの検証ロジックは無変更。

テスト 314 件 (新規 9 件: classification guard 4 + propagation contract 5)。

## 0.7.1

**sudo の env scrub を考慮した pre-sudo インライン env の非伝播 (セキュリティ修正)**:
`AWS_PROFILE=prod sudo aws s3 rm s3://x` のように透過 wrapper (`sudo`) の**前**に
インライン env を置くと、検証 subprocess には pre-sudo の `AWS_PROFILE=prod` が
伝播する一方、実際の `sudo aws ...` は `-E`/`--preserve-env` 無しでは継承環境を
scrub するため root のデフォルト環境 (prod 無し) で実行されていた。結果として
「検証は prod / 実行は別アカウント」の非対称が生じ、未承認 profile で mutating
コマンドが通る false-allow バイパスになりうる問題 (0.7.0 の D11 で混入) を修正した。

### 変更内容

1. **pre-sudo インライン env の scrub 補正** (`core/command_parser.py`) —
   `_normalize_segment` が `sudo` を剥がす直前に preserve-env 指定の有無を判定し、
   `-E` / `--preserve-env` / `--preserve-env=LIST` のいずれも無い場合は、それまでに
   収集した pre-sudo env を破棄する。sudo が継承環境を scrub する実挙動に検証側を
   合わせ、検証≠実行の非対称を解消する。env を捨てると検証はデフォルト環境で走り
   deny されうるが、これは安全方向 (false-allow → 安全側 deny)。
   - preserve-env 判定は新ヘルパ `_sudo_preserves_env()` が担当。`sudo` 直後の
     flag 領域 (`--` 単独トークンまたは非 `-` トークンが来るまで) に preserve-env
     フラグが現れるかを `_drop_wrapper_flags` と同じ flag 消費規則で走査する。
     `--preserve-env=LIST` はリスト内容や sudoers の env_keep/env_reset まで静的に
     不可知のため、preserve 指定があれば保守的に伝播を許す (保持しすぎ方向は誤 deny
     を増やすだけで安全側)。
   - **対象は sudo のみ**。`time` / `nohup` / `command` / `exec` / `env` 等の他
     wrapper は env を scrub しないため pre-wrapper env 伝播 (D11) を従来どおり維持。
   - **post-sudo の command-line env** (`sudo FOO=bar cmd` の `FOO=bar`) は sudo
     自身が target へ渡すため伝播を維持する (pre-sudo env とは別物として扱う)。
   - c731faf の inner-wins (内側 env 優先) 挙動は非 scrub wrapper で不変。
2. **回帰テスト追加** (`tests/test_command_parser.py`) — `TestSudoEnvScrub` クラス
   12 件を追加 (pre-sudo scrub / `-E`・`--preserve-env`・`--preserve-env=LIST` での
   保持 / 非 sudo wrapper での D11 維持 / 多段 sudo 経由での drop / post-sudo
   command-line env の伝播 / `sudo --` 終端 / inner-wins 維持)。あわせて従来
   `FOO=bar sudo gh ...` がバグ挙動 (pre-sudo env 伝播) を固定化していた
   `test_env_after_wrapper_collected` を、D11 本来の意図 (非 scrub wrapper の
   pre-wrapper env 収集) を表す `test_env_before_nonscrub_wrapper_collected`
   (`time` wrapper) に置き換えた。

テスト 305 件 (新規 12 件、既存 1 件を正しい期待値に修正)。

## 0.7.0

**インライン環境変数の検証 subprocess への伝播 + 診断性改善**: コマンド行頭の
インライン env (`AWS_PROFILE=prod aws ...`) が検証 subprocess に渡されず、SSO
ログイン済みでも永久に deny される問題を解消した。あわせて deny の出所明示・
情報系コマンドの誤検証防止・診断性向上を同梱。

### 変更内容

1. **インライン env の検証 subprocess への伝播** (`core/command_parser.py` +
   `core/dispatcher.py` + 5 service files + `core/cache.py`) — 従来
   `extract_candidates` は先頭 `KEY=VALUE` を剥がして捨てていたが、剥がした env
   を保持して検証 subprocess に渡すようにした。`AWS_PROFILE=prod aws ...` の
   インライン profile 運用で、ログイン済みでも default profile で検証が失敗し
   永久 deny する問題 (剥がすが使わない非対称) を解消:
   - `extract_candidates` が `(候補断片, inline_env dict)` を返す
   - dispatcher が `{**os.environ, **inline_env}` をマージして `verify(env=)` に渡す
     (マージは dispatcher に一元化し、service → core 依存を作らない)
   - 各 service の `verify(expected, project_dir, env=None)` → 内部 `_run_*(env)` →
     `subprocess.run(env=)`。env=None は従来どおり親環境継承 (後方互換)
   - cache キーに inline_env を含め、profile A の成功が profile B で誤 allow されない
   - 値に未展開の `$VAR` を含む env は静的解決不能のため伝播しない (剥がしはする)
2. **deny メッセージの出所明示** (`core/output.py`) — 全 deny の先頭に
   `[verify-cloud-account] ... (CLI 本体のエラーではありません)` タグを付け、
   AWS CLI 等のナマエラー (`Unable to locate credentials` 等) と誤認され CLI
   レベルの切り分けに時間を浪費するのを防ぐ
3. **情報系コマンドの検証スキップ** (5 service files) — `aws --version` /
   `gcloud help` 等のバージョン・ヘルプ表示コマンドを READONLY に追加。
   `command aws --version` 等の診断コマンドが誤って検証対象になり deny される
   問題を解消 (5 サービス共通の穴を一括対応)
4. **検出セグメントの併記** (`core/dispatcher.py`) — verify 失敗の deny に
   `(検出コマンド: ...)` を付け、複合コマンドのどのセグメントが検証を起動した
   かを明示して診断性を改善
5. **direnv / CLAUDE_ENV_FILE の制限を明文化** (README + CLAUDE.local.md) —
   公式仕様 (hooks.md) で `CLAUDE_ENV_FILE` は PreToolUse hook に渡らない
   (SessionStart / Setup / CwdChanged / FileChanged のみ) ため、`.envrc` /
   direnv 経由の env は検証 subprocess に届かない。これは harness 仕様起因で
   plugin では根本解決できないため、回避策 (インライン env / settings.json env /
   起動時 env) を明記する方針とした

テスト 280 件 (新規 19 件: env 抽出 6 + service env 伝播 6 + cache 分離 3 +
dispatch 統合 4)。

## 0.6.0

**self-remediation loop の解消**: deny reason が案内する切替コマンド (例:
`gh auth switch --hostname github.com --user <期待>`) 自体が自サービスの
PATTERNS にマッチして deny され、案内に従えない問題を解消した。
gh / gcloud / firebase / kubectl の 4 サービスで同型の loop を確認し一括対応。

### 変更内容

1. **期待値へ向かう切替コマンドの許可** (`core/dispatcher.py` + 4 service files) —
   各サービスに `is_self_remediation(candidate, expected)` を追加し、dispatcher は
   候補セグメントが全て期待値へ向かう切替のとき検証をスキップして許可する:
   - github: `gh auth switch --user <期待>` (`-u` / `--user=` / dict 期待値の
     `--hostname` 照合に対応、hostname 省略時は github.com)
   - gcloud: `gcloud config set project|account <期待値>`
   - firebase: `firebase use <期待 alias | project ID>`
   - kubectl: `kubectl config use-context <期待値>`
2. **安全性の維持**:
   - 期待値以外への切替・`--user` 無し (インタラクティブ) は通常検証に落ちる
   - 切替 + write の合せ技 (`gh auth switch -u X && gh pr create`) は write 側が
     通常検証される (切替前の現在値で照合)
   - remediation skip は成功キャッシュを書かないため、直後の write は再検証される
   - 期待値が未設定のサービスは従来どおり設定誘導の deny
   - aws は期待値 (Account ID) と切替手段 (profile / SSO) の照合が不能のため対象外
     (主経路 `export AWS_PROFILE` はシェル組込で元々 hook 対象外)
3. **`_collect_targets` の集約形式変更** (`core/dispatcher.py`) — 同一サービスの
   候補セグメントを `(svc, [cand, ...])` に集約 (verify は従来どおりサービスごと 1 回)

テスト 261 件 (新規 31 件: services 24 + dispatcher 7)。

## 0.5.1

**UX 改善 (P3)**: UX 監査の残 P3 フィードバック 9 件を反映。

### 改善内容

1. **Firebase CLI 未インストール検出** (`services/firebase.py`) —
   `shutil.which` で CLI の存在を確認し、未インストール時は
   `npm install -g firebase-tools` を案内 (従来は「プロジェクト取得
   できません」と未設定と区別できなかった)
2. **タイムアウト時の再試行案内** (4 service files) — 全サービスの
   timeout エラーに「再試行するか、ネットワーク接続を確認してください」
   を追加
3. **.gitignore 自動エントリ追加** (`scripts/accounts_builder.py`) —
   `init --commit` / `migrate --commit` 時に `.gitignore` へ
   accounts.local.json のエントリを best-effort で追加。
   `.gitignore` 未存在時は作成しない
4. **GCP 複数エラーの表示階層** (`services/gcloud.py`) — dict 形式で
   project + account の両方がエラーの場合、ヘッダ付きインデントリストで
   表示
5. **SETUP_HINT 重複出力の解消** (5 service files + `core/dispatcher.py`)
   — 共通の init コマンド参照を dispatcher に集約し、SETUP_HINT は
   サービス固有の最小 JSON 例のみに簡素化
6. **README の CLAUDE.md 断リンク修正** (`README.md`) — 存在しない
   `CLAUDE.md` へのリンクを修正
7. **README に `--verbose` デバッグヒント追加** (`README.md`) —
   hook 出力を確認する方法として `claude --verbose` を案内
8. **plugin.json description 短縮** — 冗長な機能列挙を 1 文に集約
9. **output.py warn hookEventName** — 確認済み、変更不要

### スキップした P3

- **deny-first 設計 (SessionStart 早期通知)** — 新規 hook 追加を伴う
  設計変更のため今回はスキップ

### テスト

- Firebase CLI 未インストール検出テスト 1 件追加 (`test_services.py`)
- .gitignore 自動エントリテスト 5 件追加 (`test_accounts_builder.py`)

合計テスト件数: 222 → 228。

## 0.5.0

**UX 改善**: architect-reviewer 4 視点 UX 監査の P2/P3 フィードバックを反映。
deny/warn メッセージの具体性向上、SETUP_HINT の最小 JSON 例追加、
deprecation warn の alert fatigue 対策。

### P2 (8 件)

1. **AWS deny メッセージ改善** (`services/aws.py`) — stderr 先頭行を表示、
   切り替え手順を `AWS_PROFILE` / `aws sso login` / `aws configure` の
   3 パターンで具体化
2. **Firebase deny メッセージ改善** (`services/firebase.py`) — alias → project
   逆引き一覧を表示し `firebase use <alias>` の具体例を案内
3. **Deprecation warn 1 日 1 回制限** (`core/dispatcher.py`) — tmpdir に
   flag ファイルを置き同一プロジェクトへの warn を 86400 秒に制限。
   deny 内の note は制限しない
4. **GitHub str 形式の照合改善** (`services/github.py`) — 複数 host ログイン時
   に `github.com` を優先照合、deny にホスト名を表示、dict 形式への移行案内
5. **SETUP_HINT に最小 JSON 例追加** (5 service files) — 全サービスの
   SETUP_HINT に `{"<key>": "<value>"}` の最小例を追加
6. **skipped ケースの解決手順具体化** (`skills/accounts-init/SKILL.md`) —
   `accounts-show` での比較 → 該当キー削除 → 再実行のステップを案内
7. **marketplace.json category** — 既に設定済みのため変更不要
8. **Skill triggers に英語フレーズ追加** (3 SKILL.md files) — 英語環境での
   自動ロード率を向上

### P3 (1 件)

- **未設定 deny メッセージ改善** (`core/dispatcher.py`) — 「全サービス不要
  なら記述不要」の 1 行を追加し、accounts.local.json が部分記述で OK と明示

### 非互換性

なし。メッセージ文言の改善のみで判定ロジックの変更はない。

### テスト

既存 222 テスト全 green (テストケースの追加なし)。

## 0.4.0

**Feature**: 親ディレクトリ遡及による `accounts.local.json` 発見。
git worktree 配下で作業しているとき、worktree 内に `accounts.local.json`
を複製しなくても親 repo (本体 checkout) の設定を自動で継承して検証する。

### 主要な変更

1. **`core/paths.py` に `discover_accounts_files_with_ancestors()` を追加** —
   `project_dir` から始めて 1 階層ずつ親へ遡り、最初に accounts.local.json が
   見つかった階層を採用する。`max_levels` (既定 10) で安全側の上限を持つ。
2. **`core/dispatcher._find_accounts_file` を親遡及対応に拡張** — 返値に
   `resolved_dir` を追加し、親階層採用時は deny / warn メッセージに
   `accounts.local.json は親ディレクトリ <絶対パス> から継承しています
   (worktree 内に同名ファイルは不要)。` の 1 行注釈を前置きする。
3. **採用優先度** — cwd 階層に何か 1 つでもあればそこを採用 (親階層は見ない)。
   親採用は cwd に何も無いときのフォールバック経路。
4. **3-tier 競合 (D4) 維持** — 同一階層に new/deprecated/legacy が同居する
   場合は従来どおり fail-closed deny。親遡及対象は「最初に見つかった階層」
   のみで、複数階層を横断した競合検出はしない (worktree 親採用は曖昧では
   ないため)。

### 非互換性

なし。cwd に accounts.local.json がある既存プロジェクトの挙動は完全に同じ。
worktree から親 repo の accounts.local.json が継承される挙動は追加機能。

### テスト

- `tests/test_dispatcher.py::TestAncestorLookup` に 6 ケース追加
  (親採用成功 / 失敗 deny + 親注釈 / cwd 優先 / 親階層 D4 競合 /
   親含め未設定 / 親 deprecated パス warn)
- `tests/test_dispatcher.py::TestAncestorDepthLimit` に 1 ケース追加
  (`max_levels` 上限テスト)

合計テスト件数: 215 → 222。

### 想定ユースケース

```
/repo/main-checkout/.claude/verify-cloud-account/accounts.local.json
/repo/main-checkout/.worktrees/feature-x/   ← cwd (worktree)
```

worktree (`/repo/main-checkout/.worktrees/feature-x/`) で `gh pr create` を
叩いても、親 repo の `accounts.local.json` が継承されて検証が走る。

## 0.3.2

**Bug fix**: Firebase の `firebase use` 出力パース修正
(`services/firebase.py`)。アクティブ project が無い状態で `firebase use`
が出力する複数行ヘルプメッセージの末尾トークン (例: "folder.") を
project ID として誤取得し、`.firebaserc` が正しく配置されていても
全 firebase コマンドが「Firebase プロジェクト不一致: 現在=folder.」で
block される回帰を修正。

### 主要な変更

1. **`_from_cli()` 堅牢化** — 単一行・単一トークンのみを project ID と
   みなす。複数行 (ヘルプメッセージ) や空白を含む行は空文字を返す。
2. **`_from_firebaserc()` 優先** — `get_active_account()` および `verify()`
   の評価順序を `_from_firebaserc() or _from_cli()` に逆転。`.firebaserc`
   が JSON で構造化された Firebase CLI 標準設定ファイルであり、CLI 出力
   フォーマットの version 依存性を回避するため。

### 非互換性

なし。`.firebaserc` 配置プロジェクトは bug 解消、未配置プロジェクトは
従来どおり `_from_cli()` にフォールバック (堅牢化により誤値ではなく
`None` を返すように改善)。

### テスト

- `tests/test_active_account.py::TestFirebaseActiveAccount` に 2 ケース追加
  (ヘルプメッセージ単独 / ヘルプメッセージ + `.firebaserc` 優先)
- `tests/test_services.py::TestFirebase` に 1 ケース追加 (`verify()` 経由の
  回帰防止)

合計テスト件数: 212 → 215。

## 0.3.1

**プロジェクト側 `.claude/verify-cloud-account/CLAUDE.md` の builder 同梱**。
Claude (LLM) が `accounts.local.json` を直接 Read / Write / Edit しようとして
sensitive-files-guardrail 等で deny された後も、同ディレクトリの CLAUDE.md を
覗いた瞬間に「builder 経由 (Bash) が正規経路」と理解できるようにする。

### 主要な変更

1. **builder が CLAUDE.md を同梱** —
   `scripts/accounts_builder.py` の `init --commit` および `migrate --commit`
   が成功した直後に、新パスのディレクトリ
   (`.claude/verify-cloud-account/`) に `CLAUDE.md` を配置する。
   既に存在する場合はスキップ (ユーザー編集を尊重)。テンプレートは
   `scripts/templates/project_claude.md`。
2. **best-effort** — テンプレート読み込み失敗・書き込み失敗のいずれも
   warning 1 行を出すだけで builder 自体は成功させる。CLAUDE.md は
   dispatcher が読みに来るパスではないため、欠損しても plugin 本体の動作には
   影響しない。
3. **疎結合の維持** — 本変更は verify-cloud-account 内で完結する。
   sensitive-files-guardrail 側の deny reason やパターンには手を入れない
   (cc-marketplaces の plugin 設計原則)。

### 設計判断

- **D6**: signpost を「埋め込まれた static 文字列」ではなく
  `scripts/templates/project_claude.md` に切り出した。テンプレートだけ更新
  すれば文言を反映できる + diff レビューが容易。
- **D7**: `init --commit` での CLAUDE.md 生成は `action` (add / unchanged /
  skipped) に依存しない。既存 `accounts.local.json` だけ持っていて signpost
  が無いユーザーが、再度 init を流せば後付けで signpost を入れられる経路を
  担保する。
- **D8**: dry-run では生成しない。実際にファイルが書かれる commit 時のみ
  signpost を置く (dry-run と commit の I/O 影響境界を一致させる)。

### 非互換性

なし。CLAUDE.md は dispatcher の判定経路に関与しないため、既存挙動への
影響はない。

### テスト

`tests/test_accounts_builder.py::TestProjectClaudeMd` を新設 (8 ケース):

- init/migrate commit で CLAUDE.md が生成される
- 既存 CLAUDE.md は上書きしない
- dry-run では生成しない
- action=unchanged でも signpost が後付けされる
- template 欠損時に builder が成功する (best-effort)

合計テスト件数: 204 → 212。

## 0.3.0

**accounts.local.json builder + 配置パス刷新**。Claude (LLM) が
accounts.local.json を安全に作成・参照・更新できる正規経路として
`scripts/accounts_builder.py` を新設し、配置パスを
`.claude/verify-cloud-account/accounts.local.json` へ移行。旧パスは
deprecation 案内付きで後方互換、新旧両方存在時は fail-closed で deny。

### 主要な変更

1. **配置パスの 3-tier lookup + 競合時 fail-closed** — `core/paths.py` を新設し
   定数 (`ACCOUNTS_FILE_NEW` / `ACCOUNTS_FILE_DEPRECATED` / `ACCOUNTS_FILE_LEGACY`)
   と helper (`accounts_file_new()` / `discover_all_accounts_files()`) を提供。
   `core/dispatcher._find_accounts_file` を書き直し、複数パス検出時は deny +
   migrate 案内を返す。
2. **builder スクリプト新設** — `scripts/accounts_builder.py` に `init` /
   `show` / `migrate` の 3 サブコマンドを実装:
   - 書込対象パスは `paths.ACCOUNTS_FILE_NEW` に固定 (argv 指定不可、assertion
     で担保) — D2
   - 既定で stdout の値は隠蔽、`--show-values` 明示時のみ露出 — D3
   - 旧 → 新の統合は migrate で行う。値衝突時は自動マージせず deny — D5
3. **Agent Skill 3 本** — `skills/accounts-init/SKILL.md` /
   `skills/accounts-show/SKILL.md` / `skills/accounts-migrate/SKILL.md`。
   いずれも「動作の安定とフォーマット統一のため builder 経由で操作する」
   「値表示前に AskUserQuestion で承認を得る」フローを Claude プロンプトで
   明示 — D1/D3。description のトリガから Claude が自発的にロードする。
4. **services 公開 API 追加** — 5 service 全てに `get_active_account()` と
   `suggest_accounts_entry()` を追加 (scalar / dict は service 側で自動選択)。
   `_parse_active_accounts` は `parse_active_accounts` に昇格 (alias 残置)。
   `services/__init__.py` の契約コメント更新。
5. **SETUP_HINT の書き換え** — 従来の `mkdir -p .claude && echo ...` の手動
   作成案内を削除し、`/verify-cloud-account:accounts-init` への誘導に置換。
6. **テスト拡張**:
   - `tests/test_active_account.py` 新設 (27 ケース: 5 service の
     `get_active_account` / `suggest_accounts_entry` を subprocess mock でテスト)
   - `tests/test_accounts_builder.py` 新設 (28 ケース: D2/D3 特化 + migrate 3
     シナリオ + 値衝突 deny)
   - `tests/test_dispatcher.py` に `TestPathMigration` クラス追加 (6 ケース:
     3-tier lookup + 競合検出 deny)
7. **ドキュメント刷新** — README.md / CLAUDE.md を新パス + builder + 設計判断
   (D1〜D5) に対応。

### 非互換性

- 配置パスが `.claude/accounts.local.json` → `.claude/verify-cloud-account/accounts.local.json`
  に移行。**旧パスのみの環境は後方互換で動作し続ける** が、deny/warn に
  deprecation 案内が付く。**新旧両方存在する環境では fail-closed で deny**
  される (D4)。
- 旧パス → 新パスへの統合は `scripts/accounts_builder.py migrate --commit`
  または `/verify-cloud-account:accounts-migrate` で行う。

### D1〜D5 (設計判断の要点)

- **D1**: 動作の安定とフォーマット統一のため、builder が唯一の正規書込/参照
  経路。Agent Skill が対話フローを提供する。
- **D2**: builder の書込対象は 1 ファイル固定 (argv で変えられない)。
- **D3**: stdout の値表示は `--show-values` 明示時のみ。Agent Skill は
  AskUserQuestion で第二段階の確認フローを提供。
- **D4**: 複数パス存在は deny (自動採用せず)。
- **D5**: migrate で旧 → 新を統合、値衝突は deny。

## 0.2.0

初期公開版 (以前のローカル hook からの plugin 化)。
