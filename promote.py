"""
Promote a reviewed draft artifact to production.

Discovery always emits status="draft". A human reads it (steps, locators,
inputs, checkpoint), then promotes it here. Replay can be run with
--require-approved so unattended/production callers never execute an
unreviewed recording.

Usage:
    python promote.py evidence/runs/<run_id>/discovered_artifact.json --reviewer "Your Name"
"""

import argparse
import json
import os

from schema import artifact_from_dict

HERE = os.path.dirname(os.path.abspath(__file__))


def promote(src: str, reviewer: str, dest_dir: str = os.path.join(HERE, "artifacts")) -> str:
    with open(src) as f:
        d = json.load(f)
    art = artifact_from_dict(d)          # validates before promoting
    d["status"] = "approved"
    if not d.get("provenance"):
        d["provenance"] = {"discovered_by": "unknown", "discovery_run_id": "unknown", "notes": ""}
    d["provenance"]["reviewed_by"] = reviewer
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, f"{art.artifact_id}.json")
    with open(dest, "w") as f:
        json.dump(d, f, indent=2)
    return dest


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("draft")
    ap.add_argument("--reviewer", required=True)
    a = ap.parse_args()
    print("Promoted to", promote(a.draft, a.reviewer))
