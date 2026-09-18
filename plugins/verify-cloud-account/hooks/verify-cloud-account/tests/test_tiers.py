"""`core/tiers.py` (READONLY / QUERY / WRITE の分類) のテスト。

**判定境界そのもの**を固定する表なので、行を消すと素通し / 誤 deny がそのまま
通る。特に次の 3 方向を明示的に押さえる:

- 開示 option (`--show-token` / `--raw` 等) を宣言から外すと READONLY が素通しに戻る
- QUERY の regex を広げると write が「リモート read」に誤分類され、不一致でも
  警告だけで通ってしまう (= ガードの無効化)
- DISCLOSING は QUERY より**先**に見る (両方の表に当たる形があるため)

tier は dispatcher と同じ経路 (`extract_candidates` → `_match_service` →
`_candidate_forms` → `tiers.classify`) で求める。classify に直接 tuple を渡すと
「wrapper 剥がし後の形」「global option を剥がした形」が本番と違う形になり、
表が本番の判定を写さなくなる。
"""
from __future__ import annotations

import re
import unittest

import _testutil  # noqa: F401

from core import tiers  # noqa: E402
from core.command_parser import extract_candidates  # noqa: E402
from core.dispatcher import (  # noqa: E402
    _candidate_forms,
    _match_service,
    _service_name,
)
from services import ALL as SERVICES  # noqa: E402

READONLY = tiers.READONLY
QUERY = tiers.QUERY
WRITE = tiers.WRITE


def tier_of(command: str) -> tuple[str | None, str | None]:
    """(service 名, tier) を dispatcher と同じ経路で求める (単一セグメント前提)。"""
    candidates = extract_candidates(command)
    assert len(candidates) == 1, f"複数セグメントに分解された: {command}"
    cand, _env = candidates[0]
    svc = _match_service(cand)
    if svc is None:
        return None, None
    forms = _candidate_forms(cand, svc)
    return _service_name(svc), tiers.classify(forms, svc)


# (コマンド, service, 期待 tier)。
_ROWS: list[tuple[str, str, str]] = [
    # ---- READONLY: ローカル / 情報系。検証しない ----
    ("gh auth status", "github", READONLY),
    ("gh auth status --hostname ghe.example.com", "github", READONLY),
    ("gh auth status -h ghe.example.com", "github", READONLY),
    ("gh auth status --active", "github", READONLY),
    ("gh auth list", "github", READONLY),
    ("gh auth logout", "github", READONLY),
    ("gh auth setup-git", "github", READONLY),
    ("gh auth login --skip-ssh-key", "github", READONLY),
    ("gh --version", "github", READONLY),
    ("aws sts get-caller-identity", "aws", READONLY),
    ("aws configure list", "aws", READONLY),
    ("aws configure list-profiles", "aws", READONLY),
    # operand が secret でない `configure get` は状態確認なので素通しのまま。
    ("aws configure get region", "aws", READONLY),
    ("aws configure get aws_access_key_id", "aws", READONLY),
    ("aws configure set region us-east-1 --profile prod", "aws", READONLY),
    ("aws sso login --profile prod", "aws", READONLY),
    ("aws --profile prod sso login", "aws", READONLY),
    ("aws --version", "aws", READONLY),
    ("gcloud auth list", "gcloud", READONLY),
    ("gcloud config get-value project", "gcloud", READONLY),
    ("gcloud beta auth login", "gcloud", READONLY),
    ("gcloud --project x config get-value project", "gcloud", READONLY),
    ("kubectl config current-context", "kubectl", READONLY),
    ("kubectl config view", "kubectl", READONLY),
    ("kubectl config view -o json", "kubectl", READONLY),
    ("kubectl config view --minify", "kubectl", READONLY),
    ("kubectl --kubeconfig k.yaml config view", "kubectl", READONLY),
    ("kubectl config get-contexts", "kubectl", READONLY),
    ("kubectl cluster-info", "kubectl", READONLY),
    ("firebase use", "firebase", READONLY),
    ("firebase login", "firebase", READONLY),
    ("npx firebase-tools login", "firebase", READONLY),
    ("firebase --version", "firebase", READONLY),
    # ---- QUERY: リモート read。検証するが止めない ----
    ("gh pr list", "github", QUERY),
    ("gh pr list --state all", "github", QUERY),
    ("gh pr view 123", "github", QUERY),
    ("gh pr diff 123", "github", QUERY),
    ("gh pr checks", "github", QUERY),
    ("gh issue list", "github", QUERY),
    ("gh repo view owner/name", "github", QUERY),
    ("gh release download v1", "github", QUERY),
    ("gh run list", "github", QUERY),
    ("gh secret list", "github", QUERY),
    ("gh status", "github", QUERY),
    ("gh browse", "github", QUERY),
    # `gh api` は option 次第で write になるので allow-list で証明する。
    ("gh api repos/o/r", "github", QUERY),
    ("gh api -X GET repos/o/r", "github", QUERY),
    ("gh api --method get repos/o/r", "github", QUERY),
    ("gh api repos/o/r --jq .name", "github", QUERY),
    ("gh api repos/o/r --paginate --silent", "github", QUERY),
    # `-t` は `gh api` では `--template` (`gh auth status` の `--show-token` では
    # ない)。DISCLOSING をコマンド形ごとに宣言しているのでここは開示にならない。
    ("gh api repos/o/r -t {{.name}}", "github", QUERY),
    ("aws s3 ls", "aws", QUERY),
    ("aws s3 ls s3://bucket", "aws", QUERY),
    ("aws --profile prod s3 ls", "aws", QUERY),
    ("aws ec2 describe-instances", "aws", QUERY),
    ("aws iam list-users", "aws", QUERY),
    ("aws s3api head-object --bucket b --key k", "aws", QUERY),
    ("aws sso list-accounts --access-token t", "aws", QUERY),
    # リモートの secret / 復号値を出力する read は `get-*` 一括 QUERY より先に
    # DISCLOSING で拾い、不一致は deny (0.14.0)。復号しない ssm get-parameter は QUERY。
    ("aws secretsmanager get-secret-value --secret-id x", "aws", WRITE),
    ("aws secretsmanager batch-get-secret-value --secret-id-list x", "aws", WRITE),
    ("aws ssm get-parameter --name /x --with-decryption", "aws", WRITE),
    ("aws ssm get-parameters-by-path --path /x --with-decryption", "aws", WRITE),
    ("aws ssm get-parameter --name /x", "aws", QUERY),
    ("aws kms decrypt --ciphertext-blob fileb://c", "aws", WRITE),
    ("aws secretsmanager list-secrets", "aws", QUERY),
    ("gcloud compute instances list", "gcloud", QUERY),
    ("gcloud beta compute instances list", "gcloud", QUERY),
    ("gcloud projects describe my-proj", "gcloud", QUERY),
    ("gcloud projects get-iam-policy my-proj", "gcloud", QUERY),
    ("gcloud --project x compute instances list", "gcloud", QUERY),
    # group 名が read verb の位置に来る形 (`run` は group、`services` は group)。
    ("gcloud run services list", "gcloud", QUERY),
    ("kubectl get pods", "kubectl", QUERY),
    # Secret の名前一覧 (option なし) と `describe` は値を出さないので QUERY のまま。
    ("kubectl get secrets", "kubectl", QUERY),
    ("kubectl describe secret my-secret", "kubectl", QUERY),
    # label selector に secret という語が入るだけの形を巻き込まない。
    ("kubectl get pods -l app=secrets -o yaml", "kubectl", QUERY),
    ("kubectl describe pod p", "kubectl", QUERY),
    ("kubectl logs pod", "kubectl", QUERY),
    ("kubectl top nodes", "kubectl", QUERY),
    ("kubectl explain pods", "kubectl", QUERY),
    ("kubectl api-resources", "kubectl", QUERY),
    ("kubectl --context foo get pods", "kubectl", QUERY),
    ("firebase projects:list", "firebase", QUERY),
    ("npx firebase-tools projects:list", "firebase", QUERY),
    ("firebase hosting:sites:list", "firebase", QUERY),
    # 宣言外の option が付いた READONLY 形は QUERY に降格する (deny ではない)。
    ("gh auth status --json", "github", QUERY),
    ("aws configure import --csv file://c.csv", "aws", QUERY),
    # ---- WRITE: それ以外 (不一致なら deny) ----
    ("gh pr create", "github", WRITE),
    ("gh repo create foo", "github", WRITE),
    ("gh pr close 1", "github", WRITE),
    ("gh secret set NAME", "github", WRITE),
    ("gh api -X POST repos/o/r/issues", "github", WRITE),
    ("gh api --method=DELETE repos/o/r", "github", WRITE),
    ("gh api repos/o/r -f title=hi", "github", WRITE),
    ("gh api graphql -F query=@q.graphql", "github", WRITE),
    ("gh api repos/o/r --input body.json", "github", WRITE),
    ("gh api repos/o/r --field a=b", "github", WRITE),
    # 未知 option / 短縮の連結形は「読むだけ」と証明できない → WRITE に倒す。
    ("gh api repos/o/r --brand-new-flag", "github", WRITE),
    ("gh api -Xpost repos/o/r", "github", WRITE),
    ("gh auth refresh --scopes admin:org", "github", WRITE),
    ("gh auth login", "github", WRITE),
    ("aws s3 rm s3://b/k", "aws", WRITE),
    ("aws s3 cp a b", "aws", WRITE),
    ("aws ec2 terminate-instances --instance-ids i", "aws", WRITE),
    ("gcloud run deploy svc", "gcloud", WRITE),
    ("gcloud config set project other", "gcloud", WRITE),
    ("gcloud secrets versions access latest --secret=s", "gcloud", WRITE),
    # group token の繰り返しは mutating verb を跨がない。跨ぐと `list` という名前の
    # リソースへの write が「リモート read」に化ける (代表形だけ置き、網羅は
    # TestGcloudQueryStopsAtMutatingVerbs)。
    ("gcloud functions deploy list --runtime python312", "gcloud", WRITE),
    ("gcloud run deploy list --image x", "gcloud", WRITE),
    ("gcloud config set project list", "gcloud", WRITE),
    ("kubectl apply -f x.yaml", "kubectl", WRITE),
    ("kubectl delete pod p", "kubectl", WRITE),
    # `\b` ではなく `(?=\s|$)` で区切っているので別コマンドを巻き込まない。
    # service ごとに 1 行ずつ置く (1 service だけだと他 service の規約違反を
    # 検出できない)。
    ("kubectl get-foo bar", "kubectl", WRITE),
    ("gh pr list-all", "github", WRITE),
    ("gcloud projects list-x", "gcloud", WRITE),
    ("firebase deploy", "firebase", WRITE),
    ("firebase use other", "firebase", WRITE),
    # ---- DISCLOSING (modifier): READONLY / QUERY を取り消して WRITE ----
    ("gh auth status --show-token", "github", WRITE),
    ("gh auth status -t", "github", WRITE),
    ("gh auth status --show-token=false", "github", WRITE),
    ("gh auth status -at", "github", WRITE),
    ("gh auth status --hostname ghe.example.com --show-token", "github", WRITE),
    ("gh auth list --show-token", "github", WRITE),
    ("gh auth token", "github", WRITE),
    ("kubectl config view --raw", "kubectl", WRITE),
    ("kubectl config view --raw -o yaml", "kubectl", WRITE),
    ("kubectl --context c config view --raw", "kubectl", WRITE),
    # `--flatten` は `--raw` 無しでも credential を平文で出す (実測 kubectl v1.34.1)
    # ので `--raw` と同格。`--minify` 併用でも redaction は戻らない。
    ("kubectl config view --flatten", "kubectl", WRITE),
    ("kubectl config view --minify --flatten", "kubectl", WRITE),
    ("kubectl cluster-info dump", "kubectl", WRITE),
    # Secret の値を出力する read (`data` は base64 だけで実質平文)。
    ("kubectl get secret my-secret -o yaml", "kubectl", WRITE),
    ("kubectl get secrets -o json", "kubectl", WRITE),
    ("kubectl get secret/my-secret -o yaml", "kubectl", WRITE),
    ("kubectl get -o yaml secret my-secret", "kubectl", WRITE),
    # 日常形は namespace / context 指定が CLI 名の直後に入る。この形は
    # 「global option を剥がした形」で照合されるので、剥がしと DISCLOSING の
    # 組み合わせを固定する (剥がさない形は `^kubectl\s+get` に当たらない)。
    ("kubectl -n kube-system get secrets -o yaml", "kubectl", WRITE),
    ("kubectl --context c get secret x -o=json", "kubectl", WRITE),
    ("aws configure get aws_secret_access_key", "aws", WRITE),
    ("aws configure get aws_session_token --profile p", "aws", WRITE),
    ("aws configure get --profile p aws_secret_access_key", "aws", WRITE),
    ("aws configure export-credentials", "aws", WRITE),
    ("aws configure export-credentials --format env", "aws", WRITE),
    ("aws sts get-session-token", "aws", WRITE),
    # 一時 credential 発行の兄弟 API / 出力そのものが認証トークンの read。
    # `get-*` の一括 QUERY より先に DISCLOSING で拾う (entry ごとに 1 行置く —
    # まとめると 1 つ消しても表が落ちない)。
    ("aws sts get-federation-token --name n --policy p", "aws", WRITE),
    ("aws ecr get-login-password", "aws", WRITE),
    ("aws ecr-public get-login-password", "aws", WRITE),
    ("aws eks get-token --cluster-name c", "aws", WRITE),
    ("aws codeartifact get-authorization-token --domain d", "aws", WRITE),
    ("gcloud auth print-access-token", "gcloud", WRITE),
    ("gcloud auth print-identity-token", "gcloud", WRITE),
    ("gcloud beta auth print-access-token", "gcloud", WRITE),
    ("gcloud auth application-default print-access-token", "gcloud", WRITE),
    ("firebase login:ci", "firebase", WRITE),
    ("npx firebase-tools login:ci", "firebase", WRITE),
]


class TestTierTable(unittest.TestCase):
    def test_rows(self):
        for command, service, expected in _ROWS:
            with self.subTest(command=command):
                found_service, found_tier = tier_of(command)
                self.assertEqual(found_service, service, "service が違う")
                self.assertEqual(found_tier, expected)

    def test_table_covers_every_service_and_tier(self):
        """空振り防止: 表が 5 service × 3 tier を実際に含んでいること。"""
        pairs = {(service, tier) for _cmd, service, tier in _ROWS}
        for svc in SERVICES:
            name = _service_name(svc)
            for tier in (READONLY, QUERY, WRITE):
                self.assertIn((name, tier), pairs, f"{name} の {tier} 行が無い")


class TestDisclosingBeatsQuery(unittest.TestCase):
    """DISCLOSING は QUERY より先に見る (両方の表に当たる形で順序が効く)。"""

    def test_get_session_token_matches_both_tables(self):
        command = "aws sts get-session-token"
        from services import aws

        self.assertTrue(
            any(re.search(p, command) for p in aws.QUERY),
            "前提が崩れている: この形が QUERY に当たらなくなった",
        )
        self.assertEqual(tier_of(command), ("aws", WRITE))

    def test_disclosing_beats_readonly(self):
        from services import github

        command = "gh auth status --show-token"
        self.assertTrue(
            any(re.search(p, command) for p in github.READONLY),
            "前提が崩れている: この形が READONLY に当たらなくなった",
        )
        self.assertEqual(tier_of(command), ("github", WRITE))


class TestGcloudQueryStopsAtMutatingVerbs(unittest.TestCase):
    """gcloud の QUERY は group token の繰り返しで **mutating verb を跨がない**。

    跨ぐと `list` / `describe` という名前のリソースに対する write が「リモート
    read」に化け、不一致でも警告だけで通る (= ガードの無効化)。逆に停止条件を
    広げすぎると `gcloud run services list` のような正しい read が WRITE になる
    ので、両方向を同じ表で押さえる。
    """

    QUERY_FORMS = (
        "gcloud compute instances list",
        "gcloud beta compute instances list",
        "gcloud alpha projects list",
        "gcloud projects describe my-proj",
        "gcloud projects get-iam-policy my-proj",
        "gcloud compute instances list --format=json",
        "gcloud config list",
        "gcloud container clusters list",
        "gcloud iam service-accounts keys list --iam-account a",
        "gcloud sql instances describe db",
        "gcloud secrets versions list --secret s",
        # `run` は group 名なので停止語に入れてはいけない。
        "gcloud run services list",
        "gcloud run revisions list",
        "gcloud logging logs list",
        "gcloud components list",
        # 停止語の**前方一致**にすぎない group 名を巻き込まないこと
        # (`stop`/`start` → storage、`enable` → endpoints、`remove` → resource-manager)。
        # 停止語の `(?=\s)` を外すとここが落ちる。
        "gcloud storage buckets list",
        "gcloud endpoints services list",
        "gcloud resource-manager tags list",
    )
    WRITE_FORMS = (
        "gcloud functions deploy list --runtime python312",
        "gcloud run deploy list --image x",
        "gcloud run services delete list",
        "gcloud pubsub topics delete list",
        "gcloud storage buckets delete list",
        "gcloud compute firewall-rules create list --allow tcp",
        "gcloud iam service-accounts delete list",
        "gcloud secrets delete describe",
        "gcloud compute instances delete describe",
        "gcloud compute instances delete list",
        "gcloud compute instances create list",
        "gcloud sql instances patch describe",
        "gcloud config set project list",
        "gcloud projects list-x",
    )

    def test_read_verbs_after_group_tokens_are_query(self):
        for command in self.QUERY_FORMS:
            with self.subTest(command=command):
                self.assertEqual(tier_of(command), ("gcloud", QUERY))

    def test_read_verb_as_operand_of_a_write_is_not_query(self):
        for command in self.WRITE_FORMS:
            with self.subTest(command=command):
                self.assertEqual(tier_of(command), ("gcloud", WRITE))


class TestLineContinuationIsFolded(unittest.TestCase):
    r"""quote 外の行継続 (`\` + 改行) はシェルと同じく取り除いてから判定する。

    取り除かないと候補文字列に改行が残り、**改行を跨げない判定表エントリだけが
    死ぬ**。`aws ssm ... --with-decryption` は DISCLOSING (deny) が外れて 1 行目
    だけで一致する QUERY (警告のみ) に落ちる = 0.13.0 より緩む方向の退行。
    """

    def test_forms_split_by_continuations_keep_their_tier(self):
        rows = (
            ("aws ssm get-parameter \\\n  --name n --with-decryption", "aws", WRITE),
            ("aws ssm get-parameter --name n \\\n  --with-decryption", "aws", WRITE),
            ("aws configure \\\n  export-credentials", "aws", WRITE),
            ("aws configure \\\nexport-credentials", "aws", WRITE),
            ("aws configure get \\\n  aws_secret_access_key", "aws", WRITE),
            ("kubectl cluster-info \\\n  dump", "kubectl", WRITE),
            ("gh auth status \\\n  --show-token", "github", WRITE),
            # 行継続は「削除」なので語の途中でも**シェルと同じ結果**になる。
            # 空白 1 個に置換すると `aws configure exp ort-credentials` に化けて
            # READONLY の `configure` に当たり、実際に走る開示形が素通しする。
            ("aws configure exp\\\nort-credentials", "aws", WRITE),
            # 行継続そのものはセグメントの区切りではない (READONLY / QUERY 側も
            # 継続をまたいで同じ tier のまま)。
            ("aws configure \\\n  list", "aws", READONLY),
            ("gh pr list \\\n  --state all", "github", QUERY),
        )
        for command, service, expected in rows:
            with self.subTest(command=command):
                self.assertEqual(tier_of(command), (service, expected))

    def test_continuation_inside_quotes_is_not_folded(self):
        """quote の内側は構文として解釈しない (畳むと引数の内容が変わる)。"""
        from core.command_parser import extract_candidates

        candidates = [cand for cand, _env in extract_candidates("echo 'a \\\n b'")]
        self.assertEqual(candidates, ["echo 'a \\\n b'"])


class TestReadonlySafeOptionsContract(unittest.TestCase):
    """宣言が黙って無効化されないことの機械チェック。"""

    def test_declared_keys_are_actual_readonly_patterns(self):
        for svc in SERVICES:
            declared = getattr(svc, "READONLY_SAFE_OPTIONS", None) or {}
            for pattern in declared:
                with self.subTest(service=_service_name(svc), pattern=pattern):
                    self.assertIn(
                        pattern,
                        svc.READONLY,
                        "READONLY_SAFE_OPTIONS のキーが READONLY に無い "
                        "(regex を書き換えて宣言が無効化された)",
                    )

    def test_some_service_declares_safe_options(self):
        """空振り防止: option 審査の宣言がどこかに実在すること。"""
        declared = [
            _service_name(svc)
            for svc in SERVICES
            if getattr(svc, "READONLY_SAFE_OPTIONS", None)
        ]
        self.assertTrue(declared, "READONLY_SAFE_OPTIONS を宣言した service が無い")

    def test_defensive_disclosing_entries_are_declared(self):
        """**今日 tier を動かさない** DISCLOSING 宣言が消えないことを固定する。

        以下の 4 entry は READONLY にも QUERY にも当たらない形なので、削除しても
        tier は WRITE のまま = 表 (`_ROWS`) では守れない。宣言の目的は「将来
        READONLY / QUERY を広げたときに素通しへ戻らないこと」なので、その目的を
        membership として直接固定する。
        """
        from services import aws, gcloud, github

        for module, needle in (
            (github, r"^gh\s+auth\s+token"),
            (aws, r"^aws\s+configure\s+export-credentials"),
            (aws, r"^aws\s+kms\s+decrypt"),
            (gcloud, "print-(access|identity)-token"),
        ):
            with self.subTest(service=_service_name(module), pattern=needle):
                self.assertTrue(
                    any(needle in pattern for pattern, _opts in module.DISCLOSING),
                    "開示形の宣言が消えている (tier は変わらないので表では検出できない)",
                )

    def test_patterns_are_anchored_and_compile(self):
        for svc in SERVICES:
            name = _service_name(svc)
            for pattern in getattr(svc, "QUERY", ()):
                with self.subTest(service=name, pattern=pattern):
                    re.compile(pattern)
                    self.assertTrue(pattern.startswith("^"), "先頭アンカが無い")
            for pattern, options in getattr(svc, "DISCLOSING", ()):
                with self.subTest(service=name, pattern=pattern):
                    re.compile(pattern)
                    self.assertTrue(pattern.startswith("^"), "先頭アンカが無い")
                    self.assertIsInstance(options, frozenset)


class TestUnauditedOptionDowngradesInsteadOfDenying(unittest.TestCase):
    """未宣言 option は **deny ではなく QUERY** (lenient 方針との整合)。"""

    def test_downgrade_is_query_not_write(self):
        for command in (
            "gh auth status --json",
            "aws configure import --csv file://c.csv",
        ):
            with self.subTest(command=command):
                self.assertEqual(tier_of(command)[1], QUERY)


class TestPolicyFromAccounts(unittest.TestCase):
    def test_absent_key_defaults_to_warn(self):
        self.assertEqual(
            tiers.policy_from_accounts({"github": "u"}), (tiers.POLICY_WARN, None)
        )

    def test_non_dict_defaults_to_warn(self):
        self.assertEqual(
            tiers.policy_from_accounts(["github"]), (tiers.POLICY_WARN, None)
        )

    def test_valid_values(self):
        for value in tiers.VALID_POLICIES:
            with self.subTest(value=value):
                self.assertEqual(
                    tiers.policy_from_accounts({tiers.POLICY_KEY: value}),
                    (value, None),
                )

    def test_case_and_space_insensitive(self):
        self.assertEqual(
            tiers.policy_from_accounts({tiers.POLICY_KEY: " DENY "}),
            (tiers.POLICY_DENY, None),
        )

    def test_invalid_value_falls_back_to_deny_with_note(self):
        """不正値は fail-closed (止める側) に倒し、理由を note で返す。

        空文字 / `null` も「書かれているが読めない値」なので同じ扱い。未設定
        (既定 warn) に倒すと **書いたのに効かない**のが黙って起きるうえ、builder の
        表示 (「不正な値 — deny として扱われます」) と挙動が食い違う。
        """
        for value in ("yes", "  ", "", None, True, 1, ["deny"], {"a": 1}):
            with self.subTest(value=value):
                policy, note = tiers.policy_from_accounts({tiers.POLICY_KEY: value})
                self.assertEqual(policy, tiers.POLICY_DENY)
                self.assertIn(tiers.POLICY_KEY, note)
                self.assertIn("不正", note)


    def test_reserved_key_does_not_collide_with_services(self):
        self.assertNotIn(tiers.POLICY_KEY, [svc.ACCOUNT_KEY for svc in SERVICES])
        self.assertTrue(tiers.POLICY_KEY.startswith("$"))


if __name__ == "__main__":
    unittest.main()
