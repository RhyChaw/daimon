"""
audit.py — append-only record of everything the agent does.

OpenClaw's biggest gap is that it has no audit trail. This is the cheapest
possible fix: one JSON line per decision. Later this becomes the thing that
lets a user review and undo what the agent did, which is what earns trust.
"""

import json
import os
import time

LOG = os.environ.get("MACAGENT_AUDIT", "audit.jsonl")


def log(**record):
    record["ts"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    with open(LOG, "a") as f:
        f.write(json.dumps(record) + "\n")
