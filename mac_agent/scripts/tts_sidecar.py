"""tts_sidecar.py — the long-lived Kokoro TTS engine, outside the app bundle.

Run by `playback.KokoroPlayer` with the sidecar venv's interpreter, NOT by the
bundled one. It must never be imported by `mac_agent`: it depends on torch,
kokoro and sounddevice, none of which are in the bundle, and `pip install`ing
them would change `pyproject.toml` and trigger the rebuild that silently drops
the macOS Accessibility grant.

Why this process exists at all: `/usr/bin/say` costs ~1350 ms from exec to first
audio and chunking spawns one per chunk, which shipped a 39% regression on total
utterance wall time. Measured 2026-08-03, this sidecar costs
**527 ms fixed + 7.36 ms/char** warm — see docs/superpowers/specs/step-4-brief.md.

PROTOCOL, newline-delimited JSON over a unix socket. Every rule below is
measured, not asserted; the brief records what happens when each is violated.

    -> {"cmd":"speak","id":N,"text":"..."}
    <- {"ev":"ack","id":N}          accepted; synthesis has NOT started
    <- {"ev":"busy","id":N}         another speak is in flight -- see MUST 7
    <- {"ev":"done","id":N,"cancelled":bool}   output device has DRAINED
    -> {"cmd":"stop","id":N}
    <- {"ev":"stopped","id":N}
    -> {"cmd":"status"}          -> {"ev":"status","ready":true}
    <- {"ev":"error","id":N,"error":"..."}     errors are DATA, never speech

1. stop(id) marks id cancelled whether or not synthesis has started, and a
   request cancelled at any point before its first sample produces NO audio.
   `ack` means accepted, so stop legitimately races synthesis start; a stop that
   no-ops on "not currently playing" would let the chunk speak anyway and defeat
   the late-stop re-check in speech.Utterance._play.
2. `done` means the output device has drained. stream.write() returns when data
   is QUEUED -- the tail is still in the device buffer. Sending `done` there
   makes speech.wait_idle() return over live audio, which is the premature-idle
   bug step 3 removed.
6. stop aborts the stream, discarding audio already handed to the device.
   Flagging "queue nothing further" leaves the buffer draining and measures a
   fiction -- the same class of error as polling a capture file's size.
7. A second concurrent speak is REJECTED, not queued. Queuing here would create
   a second place speech is pending, and speech.wait_idle() would return with
   audio still queued on this side of the socket.

The sidecar is MUTE UNTIL ASKED, including for its own errors. Nothing it
decides to say on its own initiative can reach the output device: such audio
never touches speech._PENDING, so the one-producer guard cannot see it.
"""
import argparse
import json
import os
import socket
import sys
import threading
import time

SR = 24000
BLOCK = 1024
VOICE = "af_heart"
DEVICE_NAME = "default"

# A made-up proper noun, absent from Kokoro's lexicon. Shared with the
# regression test on purpose: the startup check and the test assert the same
# property, so they cannot drift.
OOD_PROBE = "Zorbulon"


def _wire_espeak(lib, data):
    """Point phonemizer at a working espeak-ng, and prove it works.

    ORDER MATTERS. `misaki/espeak.py` calls EspeakWrapper.set_library(
    espeakng_loader.get_library_path()) at MODULE IMPORT TIME, so setting these
    before the import is silently overwritten. The bundled espeakng_loader dylib
    has its build machine's data path compiled in and makes espeak's C code
    exit() the process -- not raise -- so a try/except around EspeakFallback
    proves nothing.

    Without a working fallback, KPipeline builds en.G2P(unk=''), and every
    out-of-dictionary word -- calendar titles, contact names, app names --
    phonemises to the empty string and VANISHES from the audio, silently. That
    is the 600-char browser truncation wearing a different hat, and it is why
    this refuses to start rather than degrading.
    """
    from misaki import espeak
    if lib:
        espeak.EspeakWrapper.set_library(lib)
    if data:
        espeak.EspeakWrapper.set_data_path(data)

    from misaki.en import MToken
    fallback = espeak.EspeakFallback(british=False)
    phonemes, _ = fallback(MToken(text=OOD_PROBE, tag="NNP", whitespace=""))
    if not phonemes:
        raise RuntimeError(
            f"espeak fallback produced no phonemes for {OOD_PROBE!r}; refusing "
            "to start. Running with unk='' would silently drop out-of-"
            "dictionary words from speech."
        )
    return phonemes


class Engine:
    """Kokoro plus one long-lived output stream."""

    def __init__(self, device_name, voice):
        import numpy as np
        import sounddevice as sd
        from kokoro import KPipeline

        self._np = np
        self._sd = sd
        self.voice = voice
        self.pipeline = KPipeline(lang_code="a")
        self.pipeline.load_voice(voice)

        # Warm the lazy paths -- spaCy, the lexicon, the voice pack and the
        # first forward pass -- and DISCARD the audio. "ready" has to mean
        # "ready to hit the warm numbers", not "process alive", or the first
        # real utterance pays a cost the readiness signal promised was gone.
        for _ in self.pipeline("warming up now", voice=voice):
            pass

        self.device = self._find_device(device_name)
        # ONE stream for the process lifetime. sd.play() opens and tears down a
        # PortAudio stream per call; doing that across a few hundred requests
        # from a few hundred threads killed the process with no Python
        # traceback -- reproduced at 155 and 12 chunks but not at 3, so it is a
        # race, not a threshold.
        self.stream = sd.OutputStream(samplerate=SR, channels=1,
                                      device=self.device, dtype="float32",
                                      blocksize=BLOCK)
        self.stream.start()

    def _find_device(self, name):
        if not name:
            return None
        for i, d in enumerate(self._sd.query_devices()):
            if name in d["name"] and d["max_output_channels"] > 0:
                return i
        raise RuntimeError(f"output device {name!r} not found")

    def synthesise(self, text):
        parts = [r.audio.numpy() for r in self.pipeline(text, voice=self.voice)
                 if r.audio is not None]
        if not parts:
            return self._np.zeros(0, dtype="float32")
        return self._np.concatenate(parts)

    def play_blocking(self, buf, cancelled):
        """Write `buf` to the device. Returns True if it drained, False if cut.

        `cancelled()` is polled between blocks so stop() can cut mid-playback.
        """
        for i in range(0, len(buf), BLOCK):
            if cancelled():
                self.abort()
                return False
            self.stream.write(buf[i:i + BLOCK])
        if cancelled():
            self.abort()
            return False
        # write() returns once the data is queued; the tail is still in the
        # device buffer. MUST 2: do not claim done until it has drained.
        time.sleep(self.stream.latency)
        return True

    def abort(self):
        """Discard buffered audio, then make the stream usable again."""
        self.stream.abort()
        self.stream.start()


class Sidecar:
    def __init__(self, engine):
        self.engine = engine
        self._lock = threading.Lock()
        self._active = None      # id of the speak in flight, or None
        self._cancelled = set()

    def cancel(self, req_id):
        with self._lock:
            self._cancelled.add(req_id)
            active = self._active
        # Only abort the device if the cancelled request is the one making
        # sound. Aborting for an unrelated id would cut someone else's audio.
        if active == req_id:
            self.engine.abort()

    def _is_cancelled(self, req_id):
        with self._lock:
            return req_id in self._cancelled

    def claim(self, req_id):
        """MUST 7: reject a concurrent speak rather than queuing it."""
        with self._lock:
            if self._active is not None:
                return False
            self._active = req_id
            return True

    def release(self, req_id):
        with self._lock:
            if self._active == req_id:
                self._active = None
            self._cancelled.discard(req_id)

    def speak(self, req_id, text, send):
        try:
            # MUST 1: cancelled before synthesis started -> no audio at all.
            if self._is_cancelled(req_id):
                send({"ev": "done", "id": req_id, "cancelled": True})
                return
            buf = self.engine.synthesise(text)
            if self._is_cancelled(req_id):
                send({"ev": "done", "id": req_id, "cancelled": True})
                return
            drained = self.engine.play_blocking(
                buf, lambda: self._is_cancelled(req_id))
            send({"ev": "done", "id": req_id, "cancelled": not drained})
        except Exception as exc:                      # noqa: BLE001
            # Errors are DATA. The sidecar never speaks its own errors.
            send({"ev": "error", "id": req_id, "error": str(exc)})
            send({"ev": "done", "id": req_id, "cancelled": True})
        finally:
            self.release(req_id)


def serve(sidecar, conn):
    """One connection. The player opens one per chunk, plus control probes."""
    wlock = threading.Lock()

    def send(obj):
        with wlock:
            try:
                conn.sendall((json.dumps(obj) + "\n").encode())
            except OSError:
                pass          # peer went away mid-utterance; nothing to do

    try:
        rf = conn.makefile("r", encoding="utf-8")
        for line in rf:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                send({"ev": "error", "id": None, "error": "bad json"})
                continue
            cmd = msg.get("cmd")
            if cmd == "status":
                send({"ev": "status", "ready": True})
            elif cmd == "stop":
                sidecar.cancel(msg.get("id"))
                send({"ev": "stopped", "id": msg.get("id")})
            elif cmd == "speak":
                req_id = msg.get("id")
                if not sidecar.claim(req_id):
                    send({"ev": "busy", "id": req_id})
                    continue
                send({"ev": "ack", "id": req_id})
                # In a THREAD, not inline. Speaking inline blocks this
                # connection's read loop for the whole utterance, so the
                # {"cmd":"stop"} that arrives mid-playback is never read --
                # the player then hits its stop grace, escalates, and kills a
                # perfectly healthy sidecar. Measured 614 ms before this fix,
                # against a 150 ms bar.
                threading.Thread(target=sidecar.speak,
                                 args=(req_id, msg.get("text") or "", send),
                                 daemon=True).start()
            else:
                send({"ev": "error", "id": msg.get("id"),
                      "error": f"unknown cmd {cmd!r}"})
    except OSError:
        pass
    finally:
        try:
            conn.close()
        except OSError:
            pass


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--socket", required=True)
    ap.add_argument("--device", default=DEVICE_NAME)
    ap.add_argument("--voice", default=VOICE)
    ap.add_argument("--espeak-lib", default="/opt/homebrew/lib/libespeak-ng.dylib")
    ap.add_argument("--espeak-data", default="/opt/homebrew/share/espeak-ng-data")
    args = ap.parse_args(argv)

    started = time.monotonic()
    probe = _wire_espeak(args.espeak_lib, args.espeak_data)
    engine = Engine(None if args.device == "default" else args.device, args.voice)
    sidecar = Sidecar(engine)

    if os.path.exists(args.socket):
        os.unlink(args.socket)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(args.socket)
    srv.listen(8)

    # Readiness is a real, observable state, not a race that resolves by
    # timeout. The socket is bound only once the engine is warm, AND this line
    # is written to stdout, so the player can wait on either. Until then
    # Player.play() falls back to SayHandle.
    print(json.dumps({"ev": "ready", "load_s": round(time.monotonic() - started, 3),
                      "probe": probe, "voice": args.voice}), flush=True)

    try:
        while True:
            conn, _ = srv.accept()
            threading.Thread(target=serve, args=(sidecar, conn),
                             daemon=True).start()
    except (KeyboardInterrupt, OSError):
        pass
    finally:
        try:
            os.unlink(args.socket)
        except OSError:
            pass


if __name__ == "__main__":
    sys.exit(main())
