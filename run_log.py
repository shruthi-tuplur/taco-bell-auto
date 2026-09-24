"""
Structured run logging (observability, assignment section 3.5).

Every discovery or replay run gets its own folder:
    evidence/runs/<run_id>/
        events.jsonl     one JSON object per event: what happened and why
        result.json      the final structured result
        *.png / *.txt    screenshots + accessibility snapshots on failure

Every event passes through policy.redact_obj() before it is written, so
nothing sensitive lands on disk even if a caller forgets to scrub it.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from typing import Iterable

from policy import redact, redact_obj

HERE = os.path.dirname(os.path.abspath(__file__))
RUNS_DIR = os.path.join(HERE, "evidence", "runs")


class RunLog:
    def __init__(self, kind: str, label: str = "", secrets: Iterable[str] = (), root: str = RUNS_DIR,
                 echo: bool = True):
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]
        safe_label = "".join(c if c.isalnum() or c in "-_" else "_" for c in label)[:40]
        self.run_id = f"{stamp}_{kind}" + (f"_{safe_label}" if safe_label else "")
        self.dir = os.path.join(root, self.run_id)
        os.makedirs(self.dir, exist_ok=True)
        self.secrets = [s for s in secrets if s]
        self.echo = echo
        self._t0 = time.time()
        self._fh = open(os.path.join(self.dir, "events.jsonl"), "a")

    def path(self, name: str) -> str:
        return os.path.join(self.dir, name)

    def event(self, event: str, **fields) -> None:
        rec = {"t": round(time.time() - self._t0, 3),
               "ts": datetime.now(timezone.utc).isoformat(),
               "event": event, **fields}
        rec = redact_obj(rec, self.secrets)
        self._fh.write(json.dumps(rec, default=str) + "\n")
        self._fh.flush()
        if self.echo:
            summary = {k: v for k, v in rec.items() if k not in ("ts", "t")}
            print(f"[{rec['t']:>7.2f}s] {json.dumps(summary, default=str)[:300]}")

    def write_text(self, name: str, text: str) -> str:
        p = self.path(name)
        with open(p, "w") as f:
            f.write(redact(text, self.secrets))
        return p

    def write_json(self, name: str, obj) -> str:
        p = self.path(name)
        with open(p, "w") as f:
            json.dump(redact_obj(obj, self.secrets), f, indent=2, default=str)
        return p

    def close(self) -> None:
        try:
            self._fh.close()
        except Exception:
            pass
