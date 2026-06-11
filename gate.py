"""
gate.py — the confirmation gate for risky actions.

Right now this is a terminal y/N prompt. At v1 you replace the prompt with a
call into a small signed native helper that triggers Touch ID via Apple's
LocalAuthentication framework. The *shape* stays identical: a risky action
cannot proceed unless this returns True. Keep this function dumb and
deterministic — it must never be an AI.
"""


def allow(action, args):
    print(f"\n  [!] This action needs your OK: {action}")
    for key, value in args.items():
        print(f"        {key}: {value}")
    # v1: replace the line below with a Touch ID check from the native helper.
    answer = input("  Allow? [y/N] ").strip().lower()
    return answer == "y"
