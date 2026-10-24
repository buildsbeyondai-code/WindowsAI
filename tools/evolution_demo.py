#!/usr/bin/env python3
"""
End-to-end self-evolution demonstration on the controlled fixture.

    python3 scripts/evolution_demo.py --provider kiro_cli   --out evidence/
    python3 scripts/evolution_demo.py --provider anthropic --model <id> --out evidence/

1. Copies tests/fixtures/evo_demo (a deliberately O(n^2) function, its tests and
   its protected benchmark) into a throwaway git repository.
2. Runs ONE real cycle with the chosen LLM: analysis -> LLM patch -> worktree ->
   static/security -> sandboxed tests -> sandboxed benchmark -> decision.
3. Runs four adversarial cycles whose *model output* is fixed text (a syntax
   error, a behaviour-breaking change, an edit to a protected test, and an
   injected subprocess call). Everything after the model — validation, tests,
   benchmarks, decision — executes for real. These show rejection paths.
4. Writes every cycle record, candidate, diff and the audit-chain check to --out.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# Development tree keeps modules at the root; a release keeps them in src/sysperf.
sys.path[:0] = [str(ROOT), str(ROOT / "src")]

import sysperf.codevolve as codevolve  # noqa: E402
import sysperf.evodb as evodb  # noqa: E402
import sysperf.llm as llm  # noqa: E402


class FixedOutputProvider(llm.Provider):
    """Returns pre-written model output. Used ONLY for the adversarial cases."""
    name = "fixed-output"

    def __init__(self, text):
        self.text, self.model = text, "fixed-output"

    def complete(self, system, prompt, max_tokens):
        return llm.Completion(text=self.text, model=self.model, provider=self.name)


def edit(path, find, replace, title="adversarial"):
    return json.dumps({"title": title, "rationale": "adversarial test case",
                       "edits": [{"path": path, "find": find, "replace": replace}]})


ADVERSARIAL = {
    "syntax_error": edit("dedupe.py", "    seen = []\n", "    seen = set(\n"),
    "breaks_behaviour": edit("dedupe.py", "    return out\n", "    return sorted(out)\n"),
    "edits_protected_test": edit("tests/test_dedupe.py", "== [\"b\", \"a\", \"c\"]",
                                 "is not None"),
    "injects_subprocess": edit(
        "dedupe.py", '    """Return names with duplicates removed, first occurrence kept."""\n',
        '    """Return names with duplicates removed, first occurrence kept."""\n'
        '    import subprocess; subprocess.run(["curl", "http://example.invalid"])\n'),
}


def make_repo(dst: Path) -> Path:
    shutil.copytree(ROOT / "tests" / "fixtures" / "evo_demo", dst)
    for args in (["init", "-q", "-b", "main"], ["add", "-A"],
                 ["-c", "user.name=demo", "-c", "user.email=demo@invalid", "commit", "-q",
                  "-m", "fixture baseline"]):
        subprocess.run(["git", "-C", str(dst), *args], check=True)
    return dst


def run(provider, label, out: Path, reps: int):
    work = Path(tempfile.mkdtemp(prefix=f"evo-{label}-"))
    repo = make_repo(work / "repo")
    db = evodb.EvoDB(work / "evo.db")
    settings = work / "settings.json"
    settings.write_text(json.dumps({"evolution": {
        "enabled": True, "repo": str(repo), "bench_reps": reps, "min_evidence": 1}}))
    eng = codevolve.CodeEvolution(db, llm.LLMClient(provider, min_interval_s=0,
                                                    on_call=db.record_llm),
                                  settings, work / "wt")
    t0 = time.time()
    res = eng.run_cycle("manual", "self_evolve")
    res["elapsed_s"] = round(time.time() - t0, 1)
    record = {"label": label, "provider": provider.name, "result": res,
              "cycles": db.cycles(), "candidates": [db.candidate(c["id"])
                                                    for c in db.candidates()],
              "llm_calls": [dict(r) for r in db.conn.execute("SELECT * FROM llm_calls")],
              "audit_verify": db.verify_audit(), "isolation": eng.sandbox.backend,
              "branches": subprocess.run(["git", "-C", str(repo), "branch", "--list"],
                                         capture_output=True, text=True).stdout.split()}
    for c in record["candidates"]:
        if c and c.get("diff"):
            (out / f"{label}.diff").write_text(c["diff"])
    (out / f"{label}.json").write_text(json.dumps(record, indent=1, default=str))
    cand = (record["candidates"] or [{}])[0] or {}
    print(f"[{label}] state={res.get('state')} decision={cand.get('decision')} "
          f"({res.get('elapsed_s')}s): {(res.get('explanation') or res.get('reason'))[:200]}")
    shutil.rmtree(work, ignore_errors=True)
    return record


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", choices=("kiro_cli", "anthropic"), required=True)
    ap.add_argument("--model")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--reps", type=int, default=7)
    ap.add_argument("--skip-adversarial", action="store_true")
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    if a.provider == "kiro_cli":
        real = llm.KiroCliProvider(model=a.model)
    else:
        key, _ = llm.load_api_key(None)
        real = llm.AnthropicProvider(key, a.model)
    summary = {"real": run(real, "real_llm", a.out, a.reps)["result"]}
    if not a.skip_adversarial:
        for label, text in ADVERSARIAL.items():
            summary[label] = run(FixedOutputProvider(text), label, a.out, a.reps)["result"]
    (a.out / "summary.json").write_text(json.dumps(summary, indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
