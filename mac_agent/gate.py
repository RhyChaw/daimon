"""
gate.py — the confirmation gate for risky actions.

Risky actions cannot proceed unless this returns True. Deterministic only — no AI.
Uses Touch ID (LocalAuthentication) when available; falls back to terminal y/N for
dev runs without pyobjc or outside a GUI session.
"""

import sys
import threading
import time

_TOUCH_ID = None
_RUN_ON_MAIN = None


def _load_touch_id():
    global _TOUCH_ID, _RUN_ON_MAIN
    if _TOUCH_ID is not None:
        return _TOUCH_ID, _RUN_ON_MAIN
    try:
        from Foundation import NSDate, NSRunLoop, NSDefaultRunLoopMode
        from LocalAuthentication import (
            LAContext,
            LAPolicyDeviceOwnerAuthentication,
            LAPolicyDeviceOwnerAuthenticationWithBiometrics,
        )
        from PyObjCTools import AppHelper
    except ImportError:
        _TOUCH_ID = False
        _RUN_ON_MAIN = None
        return False, None

    def _pump_until(event):
        while not event.is_set():
            NSRunLoop.currentRunLoop().runMode_beforeDate_(
                NSDefaultRunLoopMode,
                NSDate.dateWithTimeIntervalSinceNow_(0.05),
            )

    def _touch_id_confirm(reason):
        ctx = LAContext.alloc().init()
        policy = LAPolicyDeviceOwnerAuthenticationWithBiometrics
        ok, _err = ctx.canEvaluatePolicy_error_(policy, None)
        if not ok:
            policy = LAPolicyDeviceOwnerAuthentication
            ok, _err = ctx.canEvaluatePolicy_error_(policy, None)
            if not ok:
                return False

        result = {"ok": False}
        done = threading.Event()

        def reply(success, error):
            result["ok"] = bool(success)
            done.set()

        ctx.evaluatePolicy_localizedReason_reply_(policy, reason, reply)
        _pump_until(done)
        return result["ok"]

    def run_on_main(func, *args, **kwargs):
        if threading.current_thread() is threading.main_thread():
            return func(*args, **kwargs)
        result = {"value": False, "error": None}
        done = threading.Event()

        def wrapper():
            try:
                result["value"] = func(*args, **kwargs)
            except Exception as exc:
                result["error"] = exc
            finally:
                done.set()

        AppHelper.callAfter(wrapper)
        _pump_until(done)
        if result["error"] is not None:
            raise result["error"]
        return result["value"]

    _TOUCH_ID = _touch_id_confirm
    _RUN_ON_MAIN = run_on_main
    return _TOUCH_ID, _RUN_ON_MAIN


def _in_frozen_app():
    return getattr(sys, "frozen", False)


def _terminal_confirm(action, args):
    print(f"\n  [!] This action needs your OK: {action}")
    for key, value in args.items():
        print(f"        {key}: {value}")
    answer = input("  Allow? [y/N] ").strip().lower()
    return answer == "y"


def _touch_id_allow(action, args):
    touch_id, run_on_main = _load_touch_id()
    if not touch_id:
        return _terminal_confirm(action, args)

    to_line = args.get("to", "")
    subject = args.get("subject", "")
    reason = f"Allow daimon to {action.replace('_', ' ')}"
    if to_line:
        reason += f" to {to_line}"
    if subject:
        reason += f" — {subject}"

    print(f"\n  [!] Touch ID required: {action}")
    for key, value in args.items():
        print(f"        {key}: {value}")

    if run_on_main is not None and threading.current_thread() is not threading.main_thread():
        return run_on_main(touch_id, reason)
    return touch_id(reason)


def allow(action, args):
    if _in_frozen_app():
        touch_id, _run_on_main = _load_touch_id()
        if not touch_id:
            print("\n  [!] Touch ID unavailable in this build.")
            return False
        return _touch_id_allow(action, args)

    touch_id, _run_on_main = _load_touch_id()
    if touch_id:
        try:
            return _touch_id_allow(action, args)
        except Exception:
            pass
    return _terminal_confirm(action, args)
