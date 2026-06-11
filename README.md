# mac-agent (v0)

A local-first, **gated** computer-use agent for macOS. You type a request; a
local model (via Ollama) proposes one action from a fixed whitelist; risky
actions stop and ask you; everything is logged. No cloud, no API key, nothing
leaves your machine.

This is the v0 spine — the loop, the whitelist, and the gate. It is deliberately
tiny. Voice, the cloud/local split, and Touch ID come later (see Build order).

## The one idea

The model can only ever request a verb that exists in `mac_agent/actions.py`.
Unknown verbs are rejected by code, not by the model's judgement. That whitelist
is the security boundary. To let the agent do something new, you add a verb
on purpose. Risky verbs (`send_email`) are marked `confirm` and must pass the
gate before they run.

```
you type  ->  local model proposes ONE action  ->  whitelist check
          ->  gate (if risky)  ->  execute  ->  audit log  ->  repeat
```

## Run it

Requirements: macOS, Python 3.10+, and [Ollama](https://ollama.com).

```bash
ollama pull llama3.2            # or any model you like
git clone <your-repo-url> mac-agent && cd mac-agent
pip install -e .
macagent start
```

Then try:

```
you > say good morning
you > open hacker news
you > draft an email to me@example.com about lunch tomorrow
you > send an email to me@example.com saying the meeting moved to 3pm
```

The first time it controls Mail, macOS will pop its own permission dialog
("Terminal wants to control Mail"). That prompt is the OS consent layer doing
exactly what you want — allow it.

The `send_email` step will stop and ask `Allow? [y/N]` before sending. That
prompt is the gate. At v1 it becomes Touch ID.

Every action is appended to `audit.jsonl`.

## What's where

| File | Role |
|---|---|
| `mac_agent/actions.py` | the verb whitelist + handlers — **the security boundary** |
| `mac_agent/gate.py` | confirmation for risky actions (terminal now, Touch ID later) |
| `mac_agent/audit.py` | append-only log of every action |
| `mac_agent/agent.py` | the loop: propose → check → gate → execute → log |
| `mac_agent/ollama_client.py` | talks to the local Ollama server |
| `mac_agent/cli.py` | `macagent start` |

## Build order

- **v0 (this):** local loop, whitelist, terminal gate, audit log. Prove it works.
- **v1:** risk tiers wired to a small **signed native helper** that does the
  real Touch ID confirmation; more verbs; undo. Plug in palace for memory
  ("who is my prof").
- **v2:** optional cloud orchestrator + local redaction proxy, exposed as a
  setting (fully-local vs hybrid). Deterministic redaction first, model second.
- **v3:** voice — whisper.cpp in, macOS speech synthesis out.

## Notes

- `send_email` actually sends. Everything else is safe/reversible.
- The model proposes; this code disposes. Keep `gate.py` deterministic — never
  let an AI decide whether an AI is allowed to act.
