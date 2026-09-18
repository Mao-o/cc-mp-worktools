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
    # **既知の緩和**: `get-*` 形の read はまとめて QUERY にしているため、
    # リモートの secret を読む API も不一致で警告のみになる (README の既知の制限)。
    # 表を service 単位で切り分ける案は別チケット。
    ("aws secretsmanager get-secret-value --secret-id x", "aws", QUERY),
    ("gcloud compute instances list", "gcloud", QUERY),
    ("gcloud beta compute instances list", "gcloud", QUERY),
    ("gcloud projects describe my-proj", "gcloud", QUERY),
    ("gcloud projects get-iam-policy my-proj", "gcloud", QUERY),
    ("gcloud --project x compute instances list", "gcloud", QUERY),
    ("kubectl get pods", "kubectl", QUERY),
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
    ("kubectl config view --flatten", "kubectl", QUERY),
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
    ("kubectl apply -f x.yaml", "kubectl", WRITE),
    ("kubectl delete pod p", "kubectl", WRITE),
    # `\b` ではなく `(?=\s|$)` で区切っているので別コマンドを巻き込まない。
    ("kubectl get-foo bar", "kubectl", WRITE),
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
    ("kubectl cluster-info dump", "kubectl", WRITE),
    ("aws configure get aws_secret_access_key", "aws", WRITE),
    ("aws configure get aws_session_token --profile p", "aws", WRITE),
    ("aws configure get --profile p aws_secret_access_key", "aws", WRITE),
    ("aws configure export-credentials", "aws", WRITE),
    ("aws configure export-credentials --format env", "aws", WRITE),
    ("aws sts get-session-token", "aws", WRITE),
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
            "kubectl config view --flatten",
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

    def test_empty_value_is_unset(self):
        self.assertEqual(
            tiers.policy_from_accounts({tiers.POLICY_KEY: "  "}),
            (tiers.POLICY_WARN, None),
        )

    def test_invalid_value_falls_back_to_deny_with_note(self):
        """不正値は fail-closed (止める側) に倒し、理由を note で返す。"""
        policy, note = tiers.policy_from_accounts({tiers.POLICY_KEY: "yes"})
        self.assertEqual(policy, tiers.POLICY_DENY)
        self.assertIn(tiers.POLICY_KEY, note)
        self.assertIn("不正", note)

    def test_reserved_key_does_not_collide_with_services(self):
        self.assertNotIn(tiers.POLICY_KEY, [svc.ACCOUNT_KEY for svc in SERVICES])
        self.assertTrue(tiers.POLICY_KEY.startswith("$"))


if __name__ == "__main__":
    unittest.main()
