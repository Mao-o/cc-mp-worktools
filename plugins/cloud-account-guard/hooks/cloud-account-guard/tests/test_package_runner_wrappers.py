"""JS のパッケージマネージャと mise の起動の形 (v0.32.0)。

`bunx` / `pnpx` / `yarn dlx` / `npm exec` / `npm exec --` / `mise exec <tool>@<版> --` / `mise x ... --` と、
CLI 名の前の `pnpm` / `yarn` (`pnpm firebase deploy`) を透過 wrapper として剥がし、後ろのクラウド CLI を
検証の候補にする。0.31.2 までは `npx` / `bun x` / `pnpm exec` / `pnpm dlx` / `mise exec --` だけを剥がし、
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

`yarn exec` (v0.33.0。2026-11 に yarn 1.22.22 / 2.4.3 / 3.8.7 / 4.9.2 で実測。docs/wrapper-env-audit.md の D16 表):

- `yarn exec <CLI> <引数>` は PATH (と依存の bin) の CLI を、コマンドを打ったディレクトリで起動する。
  `-` で始まる語の無い引数は、どの版でもそのまま渡す (`'a;b'` / `'$HOME'` / `'*'` / `''` / `"a -- b"` も)。
  前置きの `AWS_PROFILE=...` は後ろの CLI に届く (yarn 4 は project の `.env.yarn` の値を上に重ねる)
- CLI 名の後ろに `-` で始まる語があると、yarn 1 はその語 (`--project p` は次の語も) を自分の option として読んで
  CLI に渡さず、`--` も落とす。yarn 2〜4 はそのまま渡す。版で CLI が受け取る引数が割れるので、この形は剥がさない
- yarn 3 / 4 は CLI 名の語をシェルのコードとして読む (`yarn exec 'gh pr create'` は gh を起動し、
  `yarn exec gh\\;aws ...` は gh と aws を起動する)。yarn 1 / 2 はその名前の CLI を探して止まる
- CLI 名の位置の代入 (`yarn exec AWS_PROFILE=prod aws ...`) は、どの版も aws を起動しないので剥がさない
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


# 足した形 → 同じ読みになるはずの既存の形 (0.31.1 で剥がしていた `npx` / `mise exec --`)。
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
    "yarn exec": "npx",
}

# CLI 名の後ろに `-` で始まる語があると剥がさない形 (`_WRAPPERS_TAKE_LATER_OPTIONS`)
TAKE_LATER_OPTIONS = ("npm exec", "yarn exec")

# 検証の対象の CLI ごとの本体 (`-` で始まる語を含まない。`--` の無い `npm exec` と `yarn exec` は、後ろに
# option があると剥がさない)
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
        # pnpm は CLI 名の後ろの `--` も前の語もそのまま CLI に渡す (pnpm 8〜11 で実測) ので、直接打った形と同じに読む
        ("pnpm firebase deploy -- --project p", "firebase deploy -- --project p"),
        ("pnpm firebase deploy '--' --project p", "firebase deploy '--' --project p"),
        # 引用符の中の `--` は 1 語の一部で、yarn は CLI にそのまま渡す (yarn 1.22 で実測)
        ('yarn firebase deploy -m "a -- b" --project p', 'firebase deploy -m "a -- b" --project p'),
        # `yarn exec` (v0.33.0)。`-` で始まる語の無い引数は、yarn 1.22 / 2.4 / 3.8 / 4.9 のどれもそのまま渡す
        ("yarn exec gh pr create", "gh pr create"),
        ("yarn exec firebase deploy", "firebase deploy"),
        ("yarn exec gcloud run deploy svc 'a;b' x=1 '$HOME'", "gcloud run deploy svc 'a;b' x=1 '$HOME'"),
        ('yarn exec kubectl apply "a b" \'c  d\' 2>&1', 'kubectl apply "a b" \'c  d\' 2>&1'),
        ("yarn exec ./node_modules/.bin/firebase deploy", "firebase deploy"),
        ("yarn  exec  gh pr create", "gh pr create"),
        ("sudo yarn exec gh pr create", "gh pr create"),
        ("yarn exec npx gh pr create", "gh pr create"),
        ("yarn exec yarn exec aws s3 rm s3://b/k", "aws s3 rm s3://b/k"),
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
        # yarn 1 は CLI 名の後ろに `--` があると、前の語を自分の option として読み、`--` を落として残りを CLI に
        # 渡す (yarn 1.22 で実測: `deploy -- --project p` は `deploy --project p`、`deploy --project p -- x` は
        # `deploy x`)。hook が照合に使う引数と CLI が受け取る引数が食い違うので剥がさない
        "yarn firebase deploy -- --project p",
        "yarn firebase deploy -- -P p",
        "yarn firebase -- deploy --project p",
        "yarn firebase deploy --project p -- x",
        "yarn firebase deploy '--' --project p",
        "yarn kubectl apply -f x.yaml -- --context other",
        "yarn wrangler deploy -- --env production",
        # 引用符が閉じず語に分けられない形は、空白で分けた語で `--` を探す
        "yarn firebase deploy -- --project p -m \"it's",
        "yarn firebase -- deploy -m \"it's",
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

    def test_double_dash_in_another_segment(self):
        """`--` が後ろの段 (別のコマンド) にあるだけなら、yarn の段は剥がして照合する。"""
        command = "yarn firebase deploy --project p && kubectl exec po -- ls"
        targets = _targets(command)
        self.assertEqual([t[:2] for t in targets], [
            ("firebase", ["firebase deploy --project p"]),
            ("kubectl", ["kubectl exec po -- ls"]),
        ])
        self.assertEqual(targets[0][3], {"project": "p"})


class TestYarnExec(unittest.TestCase):
    """`yarn exec` (v0.33.0)。実測した形を 1 つずつ固定する (yarn 1.22.22 / 2.4.3 / 3.8.7 / 4.9.2)。

    剥がさない形は、剥がした結果が元のままであることと、候補・知らせが空であることを見る。どの形にも
    `-` を消した対照を並べ、対照は候補になる (空どうしの一致で通っていない) ことを見る。
    """

    # (形, `-` で始まる語を消した対照, 対照の候補, 実測: yarn 1.22 の CLI が受け取る引数 / yarn 2.4〜4.9 の引数)
    MEASURED_OPTION_FORMS = (
        ("yarn exec firebase deploy --project p", "yarn exec firebase deploy p", "firebase deploy p",
         "deploy / deploy --project p"),
        ("yarn exec firebase deploy -- --project p", "yarn exec firebase deploy p", "firebase deploy p",
         "deploy --project p / deploy -- --project p"),
        ("yarn exec firebase deploy --project p -- x", "yarn exec firebase deploy p x", "firebase deploy p x",
         "deploy x / deploy --project p -- x"),
        ("yarn exec -- firebase deploy --project p", "yarn exec firebase deploy p", "firebase deploy p",
         "deploy --project p / deploy --project p"),
        ("yarn exec gh -h", "yarn exec gh pr create", "gh pr create",
         "(引数なし) / -h"),
    )

    # 語の頭の引用符・`\`・`$` を外すと `-` で始まる語 (`_WRAPPERS_TAKE_LATER_QUOTED_OPTIONS`)。シェルが
    # 外して渡すので、yarn 1 は option として読む (`yarn exec x deploy '-P' p` の x は yarn 1.22 で `deploy` を
    # 受け取る)
    QUOTED_OPTION_FORMS = (
        ("yarn exec firebase deploy '-P' p", "yarn exec firebase deploy 'P' p"),
        ('yarn exec firebase deploy "--project" p', 'yarn exec firebase deploy "project" p'),
        ("yarn exec firebase deploy \\-P p", "yarn exec firebase deploy \\P p"),
        ("yarn exec firebase deploy $'-P' p", "yarn exec firebase deploy $'P' p"),
        ("yarn exec firebase deploy ''-P p", "yarn exec firebase deploy ''P p"),
        ("yarn exec '--' firebase deploy", "yarn exec firebase deploy"),
        ('yarn exec "-" firebase deploy', "yarn exec firebase deploy"),
    )

    def _assert_kept(self, command):
        self.assertEqual(strip_transparent_wrappers(command), command)
        self.assertEqual(_targets(command), [])
        self.assertEqual(unverified.detect(c for c, _e in extract_candidates(command)), [])

    def _assert_candidate(self, command, cand):
        targets = _targets(command)
        self.assertEqual(len(targets), 1, targets)
        self.assertEqual(targets[0][1], [cand])

    def test_measured_option_forms_are_not_candidates(self):
        for command, control, cand, _measured in self.MEASURED_OPTION_FORMS:
            with self.subTest(command=command):
                self._assert_kept(command)
            with self.subTest(control=control):
                self._assert_candidate(control, cand)

    def test_quoted_option_words_are_not_candidates(self):
        for command, control in self.QUOTED_OPTION_FORMS:
            with self.subTest(command=command):
                self._assert_kept(command)
            with self.subTest(control=control):
                self.assertEqual(len(_targets(control)), 1, _targets(control))

    def test_npm_exec_keeps_the_unquoted_check(self):
        """語の頭の引用符を外して探すのは `yarn exec` だけ。`npm exec` は 0.32.0 の判定 (空白で分けた語の頭が `-`)
        のままで、引用符で包んだ `'-R'` は剥がして照合する (0.32.1 と同じ。変えると照合していた形が検証から外れる)。"""
        command = "npm exec gh pr create '-R' o/r"
        self.assertEqual(strip_transparent_wrappers(command), "gh pr create '-R' o/r")
        self.assertEqual(strip_transparent_wrappers("yarn exec gh pr create '-R' o/r"), "yarn exec gh pr create '-R' o/r")

    def test_dash_inside_quotes_after_a_space_is_kept(self):
        """引用符の中の空白の後ろの `-` (`"a -b"` / `"a -- b"`) でも剥がさない (空白で分けた語の頭で探すため。
        yarn はこの語を 1 つの引数としてそのまま渡す (実測) ので照合はできるが、検証が新しく外れる形は無い:
        0.32.1 までは `yarn exec` を剥がしていなかった)。"""
        for command in (
            'yarn exec gcloud run deploy svc "a -b"',
            "yarn exec gcloud run deploy svc 'x --y'",
            'yarn exec kubectl apply "a -- b"',
        ):
            with self.subTest(command=command):
                self._assert_kept(command)

    def test_wrangler_with_option_is_not_notified(self):
        """検証の対象外の CLI の知らせも同じ分解を使う。option 付きは剥がさないので知らせない。"""
        self.assertEqual(unverified.detect(c for c, _e in extract_candidates("yarn exec wrangler deploy")), ["wrangler"])
        for command in ("yarn exec wrangler deploy --env production", "yarn exec wrangler deploy '-e' x"):
            with self.subTest(command=command):
                self._assert_kept(command)

    def test_cli_name_read_as_shell_code_is_not_a_candidate(self):
        """yarn 3 / 4 は CLI 名の語をシェルのコードとして読み、yarn 1 / 2 はその名前の CLI を探して止まる (実測)。
        版で起動するものが割れる形は候補にしない (README の守らない範囲)。"""
        for command in (
            "yarn exec 'gh pr create && aws s3 rm s3://b/k'",
            "yarn exec gh\\;aws s3 rm s3://b/k",
            "yarn exec 'AWS_PROFILE=prod aws' s3 rm s3://b/k",
            "yarn exec aws\\ s3 rm s3://b/k",
            "yarn exec 'aws s3 rm' s3://b/k",
        ):
            with self.subTest(command=command):
                self.assertEqual(_targets(command), [])

    def test_assignment_in_the_cli_name_position_is_not_stripped(self):
        """CLI 名の位置の代入 (`yarn exec AWS_PROFILE=prod aws ...`) は、どの版でも aws を起動しない (yarn 1 / 2 は
        `AWS_PROFILE=prod` という CLI を探して止まり、yarn 3 / 4 は代入だけを実行して終わる。実測) ので剥がさない。
        剥がすと、シェル経由の wrapper の中で後ろの段の aws を代入の値 (prod) で照合する (実際は起動時の値で走る)。"""
        for command in (
            "yarn exec AWS_PROFILE=prod aws s3 rm s3://b/k",
            "yarn exec A=1 gh pr create",
            "yarn exec a[1]=x aws s3 rm s3://b/k",
        ):
            with self.subTest(command=command):
                self._assert_kept(command)
        # 外側の `yarn exec` は剥がし、内側で止まる
        command = "yarn exec yarn exec AWS_PROFILE=prod aws s3 rm s3://b/k"
        self.assertEqual(strip_transparent_wrappers(command), "yarn exec AWS_PROFILE=prod aws s3 rm s3://b/k")
        self.assertEqual(_targets(command), [])
        command = "watch 'yarn exec AWS_PROFILE=prod aws s3 rm s3://b/k && aws s3 rm s3://b/k'"
        targets = _targets(command)
        self.assertEqual(len(targets), 1, targets)
        self.assertEqual(targets[0][:3], ("aws", ["aws s3 rm s3://b/k"], {}))
        # `env` の後ろの代入は env が子に渡す (剥がして照合する)
        targets = _targets("yarn exec env AWS_PROFILE=prod aws s3 rm s3://b/k")
        self.assertEqual(len(targets), 1, targets)
        self.assertEqual(targets[0][:3], ("aws", ["aws s3 rm s3://b/k"], {"AWS_PROFILE": "prod"}))


class TestYarnExecDispatch(unittest.TestCase):
    """`yarn exec` の判定は、剥がした形を直接打ったときと同じ。option 付きの形は検証しない (止めない)。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.project = self.tmp / "project"
        cfg = self.project / ".claude" / "cloud-account-guard"
        cfg.mkdir(parents=True)
        (cfg / "accounts.local.json").write_text('{"firebase": "proj", "aws": "123456789012"}', encoding="utf-8")
        (self.tmp / "cache").mkdir()
        patcher = mock.patch.dict(
            os.environ, {"CLAUDE_PROJECT_DIR": str(self.project), "TMPDIR": str(self.tmp / "cache")}
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _decision(self, command: str) -> str:
        try:
            with mock.patch("services.firebase.verify", return_value="不一致"), mock.patch(
                "services.aws.verify", return_value="不一致"
            ), mock.patch("subprocess.run", side_effect=AssertionError("CLI を呼ばない")):
                out = dispatch(command, str(self.project))
        except Exception as e:  # noqa: BLE001
            self.fail(f"dispatch が例外を投げた: {type(e).__name__}: {e}")
        if out is None:
            return "allow"
        self.assertIsInstance(out, dict)
        return out.get("hookSpecificOutput", {}).get("permissionDecision", "context")

    def test_mismatch(self):
        for body in ("firebase deploy", "aws s3 rm s3://b/k"):
            with self.subTest(body=body):
                self.assertEqual(self._decision(body), "deny")
                self.assertEqual(self._decision(f"yarn exec {body}"), "deny")
        for command, *_rest in TestYarnExec.MEASURED_OPTION_FORMS[:4]:
            with self.subTest(command=command):
                self.assertEqual(self._decision(command), "allow")


class TestEveryServiceBecomesACandidate(unittest.TestCase):
    def test_new_form_times_service(self):
        """足した形ごと × service ごとに、後ろの CLI が write の候補になる (0.31.1 は候補が空)。"""
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

        `--` の無い `npm exec` と `yarn exec` は npm / yarn 1 が option を読むので剥がさない (TestStripForms.KEPT /
        TestYarnExec)。
        """
        forms = [f for f in NEW_FORMS if f not in TAKE_LATER_OPTIONS] + ["pnpm exec", "pnpm dlx", "bun x", "npx"]
        self.assertNotIn("yarn exec", forms)
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
        for form in ("bunx", "pnpx", "yarn dlx", "npm exec", "npm exec --", "mise x --", "mise exec node@20 --",
                     "yarn exec"):
            command = f"{form} cd sub"
            with self.subTest(command=command):
                self.assertEqual(command_parser.external_chdir_candidates(command), frozenset({"cd sub"}))


class TestGhTokenSegmentReadsLikeDirectGh(unittest.TestCase):
    """gh の token に値を読めない代入をしたセグメントは、足した形の gh でも直接の gh と同じくセグメントごと 0.29.9 の
    読みになり、同じセグメントの aws も候補にならない (README の守らない範囲。0.31.1 は足した形の gh を剥がさず、
    aws を照合していた。CHANGELOG の 0.32.0 に書いた、止めていたコマンドを通す唯一の種類)。"""

    TEMPLATES = (
        "GH_TOKEN=$(z) watch '{w} && aws s3 rm s3://b/k'",
        "GH_TOKEN=$(z) {w} <<EOF && X=$(z) aws s3 rm s3://b/k\nx\nEOF",
    )

    def test_same_segment_cli_is_not_a_candidate(self):
        for form in NEW_FORMS:
            for tmpl in self.TEMPLATES:
                command = tmpl.replace("{w}", f"{form} gh pr create")
                with self.subTest(command=command):
                    self.assertEqual(_targets(command), [])
                    self.assertEqual(_targets(command), _targets(tmpl.replace("{w}", "gh pr create")))
                    # 印が無ければ aws は候補 (空どうしの一致で通っていない)
                    self.assertIn("aws", [t[0] for t in _targets(command.replace("GH_TOKEN=$(z) ", "", 1))])


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
