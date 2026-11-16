"""Build the version 2 execution manifest for Plan R2 from its Markdown plan.

Usage (owner side, from the repository root):
    /usr/bin/python3 -I docs/audits/issue5-r2-kit/make-manifest-r2.py > docs/plans/issue5-r2.manifest.json

The Markdown plan stays the reviewed source. Each stage's brief is the plan's
preamble, its "Run prerequisites", "Verification rules" and "Design decisions"
sections, and that stage's own section, verbatim. The manifest adds only what Markdown cannot
express: human gates the runner enforces.
- Before Stage 2 and before Stage 3: a credential-free isolated test of the
  previous stage's accepted SHA, then a person's go-ahead.
- Before completion: the same isolated test at the final SHA, then a go-ahead.
Rerunning on an unchanged plan prints byte-identical output.
"""

import hashlib
import json
import re
import sys
from pathlib import Path

PLAN = Path("docs/plans/issue5-r2-complete-and-verify-engine-git-authority.md")
STAGES = [
    ("issue5-r2-stage-1-repair-verification", "Stage R2.1"),
    ("issue5-r2-stage-2-close-authority-bypasses", "Stage R2.2"),
    ("issue5-r2-stage-3-compatibility-and-residual-git-execution", "Stage R2.3"),
]


def isolated_test_gate(gate_id: str, subject: str) -> dict:
    return {
        "id": gate_id,
        "title": f"Credential-free isolated test of {subject}",
        "kind": "manual",
        "reason": (
            f"A person runs sh /Users/Shared/sparring-recovery-r2/test-r2.sh <sha> as sparring-agent at "
            f"the exact accepted SHA of {subject}, copies the evidence to the owner's account, and answers "
            "pass only if the focused run and the full suite both exit 0 at that SHA with no tracked file "
            "changed. A reviewer's READY is not this evidence."
        ),
    }


def go_ahead_gate(gate_id: str, what: str) -> dict:
    return {
        "id": gate_id,
        "title": f"Go-ahead to {what}",
        "kind": "authorization",
        "reason": f"A person's explicit approval to {what}, given after the isolated test above passed.",
    }


def sections(text: str) -> tuple[str, dict[str, str]]:
    parts = re.split(r"(?m)^## ", text)
    preamble, named = parts[0], {}
    for part in parts[1:]:
        heading = part.split("\n", 1)[0].strip()
        named[heading] = "## " + part
    return preamble, named


def main() -> int:
    raw = PLAN.read_bytes()
    preamble, named = sections(raw.decode("utf-8"))
    shared = [
        named["Run prerequisites (run configuration, not plan scope)"],
        named["Verification rules (every stage)"],
        named["Design decisions"],
    ]
    stage_sections = [body for heading, body in named.items() if heading.startswith("Stage ")]
    if len(stage_sections) != len(STAGES):
        raise SystemExit(f"expected {len(STAGES)} stages in {PLAN}, found {len(stage_sections)}")
    stages = []
    for index, ((stage_id, label), body) in enumerate(zip(STAGES, stage_sections)):
        title = body.split("\n", 1)[0].removeprefix("## ").split("—", 1)[1].strip()
        brief = "\n".join([preamble.rstrip() + "\n", *[s.rstrip() + "\n" for s in shared], body.rstrip() + "\n"])
        entry = {"stage_id": stage_id, "label": label, "title": title, "brief": brief, "mode": "implementation"}
        if index > 0:
            previous = STAGES[index - 1][1]
            entry["gates_before"] = [
                isolated_test_gate(f"isolated-tests-before-{stage_id}", f"the accepted {previous} candidate"),
                go_ahead_gate(f"go-ahead-{stage_id}", f"start {label}"),
            ]
        stages.append(entry)
    manifest = {
        "version": 2,
        "plan_label": str(PLAN),
        "source_digest": "sha256:" + hashlib.sha256(raw).hexdigest(),
        "stages": stages,
        "completion_gates": [
            isolated_test_gate("isolated-tests-final", "the final Plan R2 candidate"),
            go_ahead_gate("go-ahead-complete", "report Plan R2 complete"),
        ],
    }
    sys.stdout.write(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
