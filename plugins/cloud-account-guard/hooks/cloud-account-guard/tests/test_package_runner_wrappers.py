"""JS のパッケージマネージャと mise の起動の形 (v0.32.0)。

`bunx` / `pnpx` / `yarn dlx` / `npm exec` / `npm exec --` / `mise exec <tool>@<版> --` / `mise x ... --` と、
CLI 名の前の `pnpm` / `yarn` (`pnpm firebase deploy`) を透過 wrapper として剥がし、後ろのクラウド CLI を
検証の候補にする。0.31.0 までは `npx` / `bun x` / `pnpm exec` / `pnpm dlx` / `mise exec --` だけを剥がし、
これらの形のクラウド CLI は候補にならず検証されなかった。

起動の根拠 (2026-11 に実測。docs/wrapper-env-audit.md の D16 表):

- bun 1.3.13: `bunx gh ...` は PATH の gh を、`bunx firebase ...` は node_modules/.bin の firebase を
  引数をそのまま渡して起動する。`bunx --help` の option は `--bun` / `-p, --package` / `--no-install` /
  `--verbose` / `--silent`
- npm 11: `npm exec -- firebase deploy --project p` の firebase は `deploy --project p` を受け取る。`--` の
  無い `npm exec firebase deploy --project p` では npm が `--project` を自分の設定として読み、firebase は
  `deploy p` を受け取る (`-P p` も `deploy p`)
- pnpm 11.25: `pnpm gh ...` (同名の script が無い) は PATH の gh を、`pnpm firebase ...` は
  node_modules/.bin の firebase を起動する。同名の script があればそちらが走る。`pnpx --help` は
  `pnpm dlx` の usage を出す (別名)
- yarn 1.22: `yarn firebase ...` は node_modules/.bin の firebase を package.json のあるディレクトリで
  起動する。同名の script があればそちらが走る。yarn の docs (`yarn dlx`) は「The binary will run within
  the current cwd」
- mise 2026.9: `mise exec node@20 -- gh ...` / `mise x -- gh ...` / `mise x node@20 -- gh ...` /
  `mise exec node -- gh ...` は後ろの gh を起動する (tool の解決に失敗した名前があっても警告して起動する)。
  `--` の無い `mise exec node@20 gh ...` は「[-- COMMAND] が無い」エラーで起動しない
- どの形も、前置きの `AWS_PROFILE=...` は後ろの CLI に届く
"""
from __future__ import annotations

import os
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _testutil  # noqa: F401

from core import command_parser, unverified  # noqa: E402
from core.command_parser import extract_candidates, strip_transparent_wrappers  # noqa: E402
from core.dispatcher import _analyze_command, _service_name, dispatch  # noqa: E402
from services import ALL as ALL_SERVICES  # noqa: E402

_ISOLATION = None
_ISOLATION_ROOT = None


def setUpModule():
    """実環境の `$HOME` / CLI 設定 / モード env を読ませない (test_dispatcher と同じ)。"""
    global _ISOLATION, _ISOLATION_ROOT
    _ISOLATION_ROOT = tempfile.mkdtemp()
    _ISOLATION = _testutil.start_isolation(Path(_ISOLATION_ROOT))


def tearDownModule():
    if _ISOLATION is not None:
        _ISOLATION.stop()
    if _ISOLATION_ROOT is not None:
        shutil.rmtree(_ISOLATION_ROOT, ignore_errors=True)


# 足した形 → 同じ読みになるはずの既存の形 (0.31.0 から剥がしていた `npx` / `mise exec --`)。
# 剥がした後の文字列が同じなら、検証の候補・env・切替・知らせもすべて同じになる。
NEW_FORMS = {
    "bunx": "npx",
    "pnpx": "npx",
    "yarn dlx": "npx",
    "npm exec": "npx",
    "npm exec --": "npx",
    "mise x --": "mise exec --",
    "mise exec node@20 --": "mise exec --",
    "mise exec aws-cli@2 --": "mise exec --",
    "mise x node@20 python@3.12 --": "mise exec --",
    "mise exec node --": "mise exec --",
    "pnpm": "npx",
    "yarn": "npx",
}

# 検証の対象の CLI ごとの本体 (`-` で始まる語を含まない。`--` の無い `npm exec` は、後ろに option が
# あると剥がさない)
BODIES = {
    "github": "gh pr create",
    "firebase": "firebase deploy",
    "aws": "aws s3 rm s3://b/k",
    "gcloud": "gcloud run deploy svc",
    "kubectl": "kubectl apply",
}


def _targets(command: str) -> list:
    """(service 名, 候補の列, env, context, tier) の列 (並びは出現順)。"""
    try:
        targets, _switching, _segments = _analyze_command(command)
    except Exception as e:  # noqa: BLE001
        raise AssertionError(f"_analyze_command が例外を投げた: {type(e).__name__}: {e}") from e
    return [
        (_service_name(svc), [cand for cand, _norm in cands], env, ctx, tier)
        for svc, cands, env, ctx, tier in targets
    ]


def _reading(command: str) -> dict:
    """判定が読む項目 (targets・切替・service の付いた段・知らせ・cd と env の印)。"""
    try:
        targets, switching, segments = _analyze_command(command)
    except Exception as e:  # noqa: BLE001
        raise AssertionError(f"_analyze_command が例外を投げた: {type(e).__name__}: {e}") from e
    service_segments = [
        None if s is None else (_service_name(s[0]), s[1], s[2])
        for s in segments
        if s is None or s[0] is not None
    ]
    cands = [c for c, _e in extract_candidates(command)]
    keep = {c for _svc, cs, *_r in _targets(command) for c in cs}
    return {
        "targets": _targets(command),
        "switching": [_service_name(s) for s in switching],
        "segments": service_segments,
        "unverified": unverified.detect(cands),
        "external_chdir": sorted(command_parser.external_chdir_candidates(command)),
        "wrapper_chdir": sorted(c for c in command_parser.wrapper_chdir_candidates(command) if c in keep),
        "unread": sorted(
            (k, sorted(v)) for k, v in command_parser.unread_env_candidates(command).items() if k in keep
        ),
    }


class TestStripForms(unittest.TestCase):
    # (入力, 剥がした結果)
    STRIPPED = (
        ("bunx firebase-tools deploy", "firebase-tools deploy"),
        ("bunx gh pr create", "gh pr create"),
        ("pnpx firebase-tools deploy", "firebase-tools deploy"),
        ("yarn dlx firebase-tools deploy", "firebase-tools deploy"),
        ("npm exec firebase deploy", "firebase deploy"),
        ("npm exec firebase-tools deploy", "firebase-tools deploy"),
        ("npm exec -- firebase deploy --project p", "firebase deploy --project p"),
        ("npm exec -- firebase deploy -P p", "firebase deploy -P p"),
        ("mise exec -- firebase deploy", "firebase deploy"),
        ("mise x -- firebase deploy", "firebase deploy"),
        ("mise exec node@20 -- firebase deploy", "firebase deploy"),
        ("mise exec aws-cli@2 -- aws s3 rm s3://b/k", "aws s3 rm s3://b/k"),
        ("mise x node@20 python@3.12 -- gcloud run deploy svc", "gcloud run deploy svc"),
        ("mise exec node -- gh pr create", "gh pr create"),
        ("mise x node@20 --", ""),
        ("pnpm firebase deploy", "firebase deploy"),
        ("pnpm gh pr create", "gh pr create"),
        ("pnpm aws s3 rm s3://b/k", "aws s3 rm s3://b/k"),
        ("pnpm gcloud run deploy svc", "gcloud run deploy svc"),
        ("pnpm kubectl apply -f x.yaml", "kubectl apply -f x.yaml"),
        ("pnpm wrangler deploy", "wrangler deploy"),
        ("pnpm cf push", "cf push"),
        ("yarn firebase deploy --project p", "firebase deploy --project p"),
        ("yarn kubectl apply -f x.yaml", "kubectl apply -f x.yaml"),
        ("yarn wrangler deploy", "wrangler deploy"),
        # 多段・綴りの変種
        ("sudo bunx firebase deploy", "firebase deploy"),
        ("bunx pnpm firebase deploy", "firebase deploy"),
        ("mise x -- pnpm gh pr create", "gh pr create"),
        ("\\bunx firebase deploy", "firebase deploy"),
        ("/opt/x/bin/pnpm firebase deploy", "firebase deploy"),
        ("npm  exec  firebase deploy", "firebase deploy"),
    )
    # 剥がさない形 (option 付き・`--` の無い mise・CLI 名でない名前・`--` の無い npm exec の後ろの option)
    KEPT = (
        "bunx --bun firebase deploy",
        "bunx -p firebase-tools firebase deploy",
        "pnpx --package=firebase-tools firebase deploy",
        "yarn dlx -p firebase-tools firebase deploy",
        "npm exec firebase deploy --project p",
        "npm exec firebase deploy -P p",
        "npm exec gh pr create --title x",
        "npm exec --yes firebase deploy",
        "npm exec --yes -- firebase deploy",
        "npm exec -c 'firebase deploy'",
        "mise exec node@20 firebase deploy",
        "mise x firebase deploy",
        "mise exec -C dir -- gcloud run deploy svc",
        "mise x -j 2 node@20 -- gcloud run deploy svc",
        "mise x node@20 -E x -- gcloud run deploy svc",
        "pnpm firebase-tools deploy",
        "pnpm firebase@13 deploy",
        "pnpm run firebase deploy",
        "pnpm -C app firebase deploy",
        "pnpm install firebase-tools",
        "pnpm 'gh' pr create",
        "yarn add firebase-tools",
        "yarn run firebase deploy",
        "yarn --cwd app firebase deploy",
        "yarn ghx pr create",
    )

    def test_stripped(self):
        for command, expected in self.STRIPPED:
            with self.subTest(command=command):
                self.assertEqual(strip_transparent_wrappers(command), expected)

    def test_kept(self):
        for command in self.KEPT:
            with self.subTest(command=command):
                self.assertEqual(_targets(command), [])
                self.assertEqual(unverified.detect(c for c, _e in extract_candidates(command)), [])


class TestEveryServiceBecomesACandidate(unittest.TestCase):
    def test_new_form_times_service(self):
        """足した形ごと × service ごとに、後ろの CLI が write の候補になる (0.31.0 は候補が空)。"""
        for form in NEW_FORMS:
            for service, body in BODIES.items():
                command = f"{form} {body}"
                with self.subTest(command=command):
                    targets = _targets(command)
                    self.assertEqual(len(targets), 1, targets)
                    name, cands, env, ctx, tier = targets[0]
                    self.assertEqual(name, service)
                    self.assertEqual(cands, [body])
                    self.assertEqual((env, ctx, tier), ({}, {}, "write"))

    def test_firebase_tools_package_name(self):
        """パッケージ名で起動する形 (`bunx firebase-tools deploy`) も firebase の候補になる。"""
        for form in ("bunx", "pnpx", "yarn dlx", "npm exec", "npm exec --"):
            command = f"{form} firebase-tools deploy"
            with self.subTest(command=command):
                targets = _targets(command)
                self.assertEqual(len(targets), 1, targets)
                self.assertEqual(targets[0][0], "firebase")
                self.assertEqual(targets[0][1], ["firebase-tools deploy"])

    def test_pre_wrapper_env_reaches_the_cli(self):
        """前置きの代入は後ろの CLI の検証の env に入る (どの形も env を素通すことを実測した)。"""
        for form in NEW_FORMS:
            command = f"AWS_PROFILE=prod {form} aws s3 rm s3://b/k"
            with self.subTest(command=command):
                targets = _targets(command)
                self.assertEqual(len(targets), 1, targets)
                self.assertEqual(targets[0][:3], ("aws", ["aws s3 rm s3://b/k"], {"AWS_PROFILE": "prod"}))

    def test_context_option_after_the_cli(self):
        """CLI 名の後ろの option は CLI にそのまま渡る (実測) ので、context の option を照合に使う。

        `--` の無い `npm exec` だけは npm が option を読むので剥がさない (TestStripForms.KEPT)。
        """
        forms = [f for f in NEW_FORMS if f != "npm exec"] + ["pnpm exec", "pnpm dlx", "bun x", "npx"]
        for form in forms:
            command = f"{form} firebase deploy --project p"
            with self.subTest(command=command):
                targets = _targets(command)
                self.assertEqual(len(targets), 1, targets)
                self.assertEqual(targets[0][0], "firebase")
                self.assertEqual(targets[0][1], ["firebase deploy --project p"])
                self.assertEqual(targets[0][3], {"project": "p"})


class TestSameReadingAsExistingWrapper(unittest.TestCase):
    """足した形は、剥がした後が既存の形 (`npx` / `mise exec --`) と同じなので、判定が読む項目も同じ。

    前置き・前後の段・既にある読みの印 (値を読めない代入・gh の token の代入・シェル経由 wrapper・
    heredoc・別の読みの起動) と組んでも、経路で結果が変わらないこと。
    """

    CONTEXTS = (
        "{w}",
        "AWS_PROFILE=prod {w}",
        "AWS_PROFILE=$(echo prod) {w}",
        "GH_TOKEN=$(gh auth token --user x) {w}",
        "GH_TOKEN=$T {w}",
        "X=$(z) {w}",
        "cd sub && {w}",
        "sudo {w}",
        "time {w}",
        "( {w} )",
        "cat <<EOF && {w}\nbody 'x' (\nEOF",
        "{w} && aws s3 rm s3://b/k",
        "{w} | cat",
        "gh auth switch --user other && {w}",
        "AWS_PROFILE=$(z) aws s3 ls && {w}",
        "watch '{w}'",
        "echo $'\\'' ; {w}",
        "a=(1 2)# ; {w}",
    )

    def test_product(self):
        for form, ref in NEW_FORMS.items():
            with_targets = 0
            for body in BODIES.values():
                for context in self.CONTEXTS:
                    command = context.replace("{w}", f"{form} {body}")
                    reference = context.replace("{w}", f"{ref} {body}")
                    with self.subTest(command=command):
                        got = _reading(command)
                        self.assertEqual(got, _reading(reference))
                        with_targets += bool(got["targets"])
            # gh の token の代入に読めない値を置いた gh (0.29.9 と同じく読む) のほかは候補がある (空どうしの一致で
            # 通っていない)
            with self.subTest(form=form, check="候補のある組の数"):
                self.assertGreaterEqual(with_targets, len(BODIES) * len(self.CONTEXTS) - 2)

    def test_external_cd_does_not_move_the_shell(self):
        """wrapper の後ろの `cd` は外部プロセスで走り、シェルを移動させない (`npx cd sub` と同じ)。"""
        for form in ("bunx", "pnpx", "yarn dlx", "npm exec", "npm exec --", "mise x --", "mise exec node@20 --"):
            command = f"{form} cd sub"
            with self.subTest(command=command):
                self.assertEqual(command_parser.external_chdir_candidates(command), frozenset({"cd sub"}))


class TestUnverifiedNotice(unittest.TestCase):
    def test_new_forms_notify_wrangler(self):
        """検証の対象外の CLI の知らせも同じ分解を使うので、足した形の wrangler を知らせる。"""
        for form in NEW_FORMS:
            command = f"{form} wrangler deploy"
            with self.subTest(command=command):
                self.assertEqual(unverified.detect(c for c, _e in extract_candidates(command)), ["wrangler"])


class TestBinRunnerNames(unittest.TestCase):
    """`pnpm <名前>` / `yarn <名前>` を剥がす名前は、services/ の CLI 名と対象外の CLI 名に揃える。"""

    NAMES = command_parser._BIN_RUNNER_NAMES

    def test_every_service_has_its_cli_name(self):
        for svc in ALL_SERVICES:
            with self.subTest(service=_service_name(svc)):
                matched = [
                    n for n in self.NAMES if any(re.search(p, f"{n} x") for p in svc.PATTERNS)
                ]
                self.assertTrue(matched, f"{_service_name(svc)} の CLI 名が _BIN_RUNNER_NAMES に無い")

    def test_every_name_is_a_cli(self):
        for name in self.NAMES:
            with self.subTest(name=name):
                is_service = any(
                    re.search(p, f"{name} x") for svc in ALL_SERVICES for p in svc.PATTERNS
                )
                self.assertTrue(is_service or name in unverified.UNVERIFIED_CLIS, name)

    def test_unverified_names_are_included(self):
        for name in unverified.UNVERIFIED_CLIS:
            with self.subTest(name=name):
                self.assertIn(name, self.NAMES)


class TestSameNameScriptIsVerifiedLikeTheCli(unittest.TestCase):
    """`pnpm firebase deploy` は同名の package.json の script でも CLI として照合する (README の既知の制限)。

    止まる条件は直接 `firebase deploy` を打ったときと同じ (期待値との不一致、期待値が未登録で固定も無い、など)。
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.project = self.tmp / "project"
        self.cfg = self.project / ".claude" / "cloud-account-guard"
        self.cfg.mkdir(parents=True)
        (self.tmp / "cache").mkdir()
        patcher = mock.patch.dict(
            os.environ, {"CLAUDE_PROJECT_DIR": str(self.project), "TMPDIR": str(self.tmp / "cache")}
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _decision(self, command: str) -> str:
        try:
            out = dispatch(command, str(self.project))
        except Exception as e:  # noqa: BLE001
            self.fail(f"dispatch が例外を投げた: {type(e).__name__}: {e}")
        if out is None:
            return "allow"
        self.assertIsInstance(out, dict)
        return out.get("hookSpecificOutput", {}).get("permissionDecision", "context")

    def test_mismatch_and_unregistered(self):
        worlds = {
            "mismatch": ('{"firebase": "proj"}', "不一致"),
            "match": ('{"firebase": "proj"}', None),
            "unregistered": ('{"github": "me"}', None),
        }
        for world, (accounts, verdict) in worlds.items():
            (self.cfg / "accounts.local.json").write_text(accounts, encoding="utf-8")
            with mock.patch("services.firebase.verify", return_value=verdict), mock.patch(
                "subprocess.run", side_effect=AssertionError("CLI を呼ばない")
            ):
                direct = self._decision("firebase deploy")
                for form in ("pnpm", "yarn"):
                    with self.subTest(world=world, form=form):
                        self.assertEqual(self._decision(f"{form} firebase deploy"), direct)
            with self.subTest(world=world, check="direct"):
                self.assertEqual(direct, "allow" if world == "match" else "deny")


if __name__ == "__main__":
    unittest.main()
