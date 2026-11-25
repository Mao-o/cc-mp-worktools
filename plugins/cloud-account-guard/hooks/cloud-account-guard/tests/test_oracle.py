"""比較の道具 (tests/oracle/) の oracle と比較器が空でないことを確かめる (bash と zsh が無ければ skip)。

道具の使い方は docs/DEVELOPMENT.md の「照合の組の比較 (tests/oracle/)」。ここで確かめるのは:

- oracle (シェル側) が、zsh の整数型の特殊な変数への代入で照合用の名前が書き換わったことを組に出す
- hook 側 (この版の dispatch) が、verify に渡った組を出す
- 比較器が、組の違う行だけを数える (組の同じ行は数えない)。旧版の記録は、同じ行を止めたことにした写し
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ORACLE = os.path.join(HERE, "oracle")
PKG = os.path.dirname(HERE)
if HERE not in sys.path:
    sys.path.insert(0, HERE)
from oracle import compare  # noqa: E402

_ZSH = "/bin/zsh" if os.access("/bin/zsh", os.X_OK) else shutil.which("zsh")
_ENV = {"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1"}

ROWS = [
    # zsh は整数型の SECONDS への代入の値を算術式として評価し、KUBECONFIG を 0 に書き換える
    {"id": "rewrite", "tag": "kubectl:arith", "cmd": "KUBECONFIG={W}/good/k.yaml SECONDS=KUBECONFIG=0 kubectl apply -f x",
     "launch": {"KUBECONFIG": "{W}/project/bad.yaml"}, "shellvars": {}},
    # 値の決まらない context option: hook は捨てて既定で照合し、CLI は dev で走る
    {"id": "ctx", "tag": "aws:optvar", "cmd": "aws --profile $X s3 rm s3://b/k",
     "launch": {"AWS_CONFIG_FILE": "{W}/good/aws/config"}, "shellvars": {"X": "dev"}},
    # 対照: 組が同じ
    {"id": "same", "tag": "kubectl:ctrl", "cmd": "KUBECONFIG={W}/good/k.yaml kubectl apply -f x",
     "launch": {"KUBECONFIG": "{W}/project/bad.yaml"}, "shellvars": {}},
    # 対照: 値の決まる context option は hook も照合に使う
    {"id": "ctxs", "tag": "aws:ctrl", "cmd": "aws --profile prod s3 rm s3://b/k",
     "launch": {"AWS_CONFIG_FILE": "{W}/good/aws/config"}, "shellvars": {}},
    # 対照: 相対パスは CLI が走るディレクトリで解決して比べる (hook の作業ディレクトリは別)
    {"id": "rel", "tag": "kubectl:ctrl", "cmd": "KUBECONFIG=k.yaml kubectl apply -f x", "launch": {},
     "shellvars": {}},
]


def _load(path):
    with open(path, encoding="utf-8") as f:
        return {r["id"]: r for r in map(json.loads, f)}


@unittest.skipUnless(os.access("/bin/bash", os.X_OK) and _ZSH, "bash と zsh の両方が要る")
class OracleAndComparatorTests(unittest.TestCase):
    def test_counts_only_the_rows_whose_tuple_differs(self):
        with tempfile.TemporaryDirectory() as tmp:
            decl_path = os.path.join(tmp, "decl.json")
            out = subprocess.run([sys.executable, "-B", "-I", os.path.join(ORACLE, "decl.py"), PKG],
                                 capture_output=True, text=True, env=_ENV, timeout=60)
            self.assertEqual(out.returncode, 0, out.stderr)
            with open(decl_path, "w", encoding="utf-8") as f:
                f.write(out.stdout)
            corpus = os.path.join(tmp, "corpus.jsonl")
            with open(corpus, "w", encoding="utf-8") as f:
                for row in ROWS:
                    f.write(json.dumps(row) + "\n")
            shell_out, hook_out = os.path.join(tmp, "shell.jsonl"), os.path.join(tmp, "hook.jsonl")
            for argv in (
                [os.path.join(ORACLE, "shellside.py"), os.path.join(tmp, "ws"), decl_path, corpus, shell_out],
                ["-I", os.path.join(ORACLE, "hookside.py"), PKG, os.path.join(tmp, "wh"), decl_path, corpus,
                 hook_out],
            ):
                run = subprocess.run([sys.executable, "-B", *argv], capture_output=True, text=True, env=_ENV,
                                     timeout=120)
                self.assertEqual(run.returncode, 0, run.stderr)
            shell, hook = _load(shell_out), _load(hook_out)
            self.assertEqual(sorted(shell), sorted(r["id"] for r in ROWS))
            self.assertEqual(sorted(hook), sorted(r["id"] for r in ROWS))

            def env_of(runs, cli):
                self.assertIsInstance(runs, list)
                self.assertEqual([r["cli"] for r in runs], [cli])
                return runs[0]["tuple"]["env"]

            self.assertIn("zsh", shell["rewrite"])
            self.assertEqual(env_of(shell["rewrite"]["zsh"], "kubectl").get("KUBECONFIG"), "<W>/project/0")
            self.assertEqual([c["tuple"]["env"].get("KUBECONFIG") for c in hook["rewrite"]["calls"]],
                             ["<W>/good/k.yaml"])
            self.assertEqual([c["tuple"]["ctx"] for c in hook["ctx"]["calls"]], [{}])
            self.assertIn("bash", shell["ctx"])
            self.assertEqual({r["cli"]: r["tuple"]["ctx"] for r in shell["ctx"]["bash"]}, {"aws": {"profile": "dev"}})
            # prefix (`AWS_`) で拾う名前も組に入る
            self.assertEqual(env_of(shell["ctx"]["bash"], "aws").get("AWS_CONFIG_FILE"), "<W>/good/aws/config")
            self.assertIn("bash", shell["rel"])
            self.assertEqual(env_of(shell["rel"]["bash"], "kubectl").get("KUBECONFIG"), "<W>/project/k.yaml")

            old = {i: {**r, "d": "deny"} for i, r in hook.items()}
            s = compare.tally(ROWS, old, hook, shell)
            self.assertEqual(sorted(x["id"] for x in s["dod"]), ["ctx", "rewrite"])
            self.assertEqual(sorted(x["id"] for x in s["new_abs"]), ["ctx", "rewrite"])
            self.assertEqual(sorted(x["id"] for x in s["fixed"]), ["ctxs", "rel", "same"])
            self.assertEqual(s["old_abs"], [])
            names = {x["id"]: sorted({n for b in x["bad"] for n in b["names"]}) for x in s["dod"]}
            self.assertEqual(names, {"ctx": ["ctx:profile"], "rewrite": ["env:KUBECONFIG"]})


if __name__ == "__main__":
    unittest.main()
