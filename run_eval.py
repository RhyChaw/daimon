#!/usr/bin/env python3
"""
run_eval.py — the daimon memory ablation.

Holds the model fixed and weak, varies only the memory, and measures whether
memory lets the small local model do real work — and whether palace's
query-relevant recall beats a fixed last-3 window.

Three arms (same model, same low temperature, same whitelist prompt):
  A  none    : no memory at all
  B  simple  : fact store (keyword) + recent() = fixed last 3 events
  C  palace  : same fact store + `palace mem recall` (query-relevant episodic)

The ONLY difference between B and C is the episodic layer. That isolates exactly
what palace adds today.

Run:  python run_eval.py
Needs: Ollama running locally. Arm C also needs `palace` on PATH (it's skipped
       gracefully if absent, so A and B still produce a result).
"""

import json
import os
import re
import shutil
import subprocess
import time
import urllib.request

MODEL = os.environ.get("MACAGENT_MODEL", "llama3.2")
TEMP = float(os.environ.get("EVAL_TEMP", "0.2"))      # low + fixed: this is an ablation, not a vibe check
OLLAMA = "http://localhost:11434"
MEM_ROOT = os.path.abspath("./eval-mem")              # palace memory root for this run
HERE = os.path.dirname(os.path.abspath(__file__))


def _palace_bin():
    return os.environ.get("DAIMON_PALACE_BIN", "palace")


def _palace_available():
    """True when palace-ai v2 (mem recall) is reachable."""
    bin_path = _palace_bin()
    if os.path.sep in bin_path:
        if not (os.path.isfile(bin_path) and os.access(bin_path, os.X_OK)):
            return False
    elif shutil.which(bin_path) is None:
        return False
    try:
        proc = subprocess.run(
            [bin_path, "mem", "recall", "--help"],
            capture_output=True,
            timeout=5,
        )
        return proc.returncode == 0
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return False

ACTIONS = ["say(text)", "open_url(url)", "draft_email(to,subject,body)",
           "send_email(to,subject,body)", "remember(key,value)",
           "read_calendar()", "get_weather(location)"]


def system_prompt():
    lines = [
        "You control a Mac through a FIXED set of actions.",
        'Reply with ONLY a JSON object: {"action": <name>, "args": {...}}.',
        "Allowed actions: " + ", ".join(ACTIONS) + ".",
        "Use Known facts and history below for email addresses and other details.",
        'Answer questions with "say" and put the answer in text (include emails, units, etc.).',
        'Use "remember" only when the user explicitly asks you to save something new.',
        "For weather or calendar questions, use get_weather or read_calendar — do not guess.",
        "Output nothing except the JSON object.",
    ]
    return "\n".join(lines)


# ---- model call -------------------------------------------------------------

def _ollama_up():
    try:
        with urllib.request.urlopen(OLLAMA, timeout=3) as r:
            return r.status == 200
    except Exception:
        return False


def ollama(system, user):
    body = json.dumps({
        "model": MODEL, "format": "json", "stream": False,
        "options": {"temperature": TEMP},
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
    }).encode()
    req = urllib.request.Request(OLLAMA + "/api/chat", data=body,
                                 headers={"Content-Type": "application/json"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=120) as r:
        data = json.loads(r.read())
    latency = time.time() - t0
    tokens = data.get("prompt_eval_count", 0) + data.get("eval_count", 0)
    return data["message"]["content"], tokens, latency


def parse_action(content):
    try:
        msg = json.loads(content)
        return msg.get("action"), msg.get("args", {})
    except Exception:
        return None, {}


# ---- the three memory arms --------------------------------------------------

def fact_context(request, facts):
    """Keyword fact recall (mirrors mac_agent.memory.recall)."""
    words = set(re.findall(r"[a-z0-9]+", request.lower()))
    hits = [f"{k} = {v}" for k, v in facts.items() if k.lower() in words]
    return ("Known facts: " + "; ".join(hits)) if hits else ""


def recent_context(events, n=3):
    last = events[-n:]
    return ("Recently: " + "; ".join(e["agent_notes"] for e in last)) if last else ""


def palace_context(request, root):
    """Arm C: query-relevant episodic recall via palace mem recall. Fail-soft."""
    if not _palace_available():
        return None  # signal: palace unavailable
    try:
        out = subprocess.run(
            [_palace_bin(), "mem", "recall", "--root", root, "--query", request, "--json"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if out.returncode != 0:
            return ""
        data = json.loads(out.stdout)
        if not data.get("ok"):
            return ""
        items = data.get("events") or []
        notes = []
        for it in items[:3]:
            notes.append(it.get("agent_notes") or it.get("task") or json.dumps(it))
        return ("Relevant history: " + "; ".join(notes)) if notes else ""
    except Exception:
        return ""


def _sense_action(request):
    if re.search(r"\b(weather|temperature|temp\b|rain|snow|forecast|humid|wind)\b", request, re.I):
        return "get_weather"
    if re.search(
        r"\b(calendar|schedule|my day|today'?s events|meetings|agenda|how'?s my day|day look like)\b",
        request,
        re.I,
    ):
        return "read_calendar"
    return None


def build_user_msg(request, arm, facts, events, root):
    parts = []
    if arm == "A":
        pass
    elif arm == "B":
        parts += [c for c in (fact_context(request, facts), recent_context(events)) if c]
    elif arm == "C":
        pc = palace_context(request, root)
        if pc is None:
            return None  # palace not installed -> skip arm C
        parts += [c for c in (fact_context(request, facts), pc) if c]
    parts.append("Request: " + request)
    required = _sense_action(request)
    if required:
        parts.append(f"Required action: {required} (do not answer with say).")
    return "\n".join(parts)


# ---- scoring ----------------------------------------------------------------

def score(action, args, expect):
    if "action" in expect and action != expect["action"]:
        return False
    for k, v in expect.get("args_contains", {}).items():
        if v.lower() not in str(args.get(k, "")).lower():
            return False
    if "say_contains" in expect:
        needle = expect["say_contains"].lower()
        if action == "say":
            hay = str(args.get("text", ""))
        elif action == "remember":
            hay = str(args.get("value", ""))
        else:
            return False
        if needle not in hay.lower():
            return False
    return True


# ---- seed palace memory -----------------------------------------------------

def seed_memory(events, root):
    os.makedirs(os.path.join(root), exist_ok=True)
    path = os.path.join(root, "attempts.jsonl")
    with open(path, "w") as f:
        for i, e in enumerate(events):
            rec = {"id": f"seed-{i}", "timestamp": "2026-06-11T00:00:00Z",
                   "session_id": "seed", "outcome": "success", **e}
            f.write(json.dumps(rec) + "\n")


# ---- run --------------------------------------------------------------------

def main():
    if not _ollama_up():
        print("Ollama is not running. Start it with: ollama serve")
        raise SystemExit(1)

    spec = json.load(open(os.path.join(HERE, "tasks.json")))
    facts, events, tasks = spec["facts"], spec["seed_events"], spec["tasks"]
    seed_memory(events, MEM_ROOT)

    arms = ["A", "B", "C"]
    results = {a: {} for a in arms}        # arm -> task_id -> (passed, tokens, latency) or None
    sysp = system_prompt()

    for t in tasks:
        for a in arms:
            msg = build_user_msg(t["request"], a, facts, events, MEM_ROOT)
            if msg is None:                # arm C skipped (no palace)
                results[a][t["id"]] = None
                continue
            content, tokens, lat = ollama(sysp, msg)
            action, args = parse_action(content)
            passed = score(action, args, t["expect"])
            results[a][t["id"]] = (passed, tokens, lat)

    if _palace_available():
        palace_note = f"  palace={_palace_bin()}"
    else:
        palace_note = "  (arm C skipped — palace-ai v2 mem recall not found)"

    # per-task matrix
    print(f"\nmodel={MODEL}  temp={TEMP}{palace_note}\n")
    print(f"{'task':<20}{'category':<20}{'A':>4}{'B':>4}{'C':>4}")
    print("-" * 68)
    for t in tasks:
        row = ""
        for a in arms:
            r = results[a][t["id"]]
            row += f"{'-' if r is None else ('PASS' if r[0] else 'fail'):>4}"
        print(f"{t['id']:<20}{t['category']:<20}{row}")

    # summary
    print("\n" + "=" * 68)
    print(f"{'arm':<10}{'pass rate':>12}{'avg tokens':>14}{'avg latency':>14}")
    print("-" * 68)
    labels = {"A": "A none", "B": "B simple", "C": "C palace"}
    for a in arms:
        vals = [r for r in results[a].values() if r is not None]
        if not vals:
            print(f"{labels[a]:<10}{'(skipped — set DAIMON_PALACE_BIN)':>40}")
            continue
        passes = sum(1 for r in vals if r[0])
        toks = sum(r[1] for r in vals) / len(vals)
        lat = sum(r[2] for r in vals) / len(vals)
        print(f"{labels[a]:<10}{f'{passes}/{len(vals)}':>12}{toks:>14.0f}{lat:>13.2f}s")
    print()


if __name__ == "__main__":
    main()
