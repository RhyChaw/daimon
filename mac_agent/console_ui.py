"""
console_ui.py — Cocoa console for Daimon.app.

Log area on top, input bar pinned to bottom. Transcript is a plain Python
string re-rendered with setString_ so layout bugs cannot hide text.
"""

import io
import queue
import sys
import threading
import traceback

from AppKit import (
    NSApplication,
    NSBackingStoreBuffered,
    NSBezelStyleRounded,
    NSButton,
    NSColor,
    NSFont,
    NSMakeRect,
    NSScrollView,
    NSTextField,
    NSTextView,
    NSViewHeightSizable,
    NSViewMaxYMargin,
    NSViewMinXMargin,
    NSViewWidthSizable,
    NSWindow,
    NSWindowStyleMaskClosable,
    NSWindowStyleMaskMiniaturizable,
    NSWindowStyleMaskResizable,
    NSWindowStyleMaskTitled,
)
from Foundation import NSMakeSize, NSObject, NSTimer
import objc
from PyObjCTools import AppHelper

INPUT_H = 50
MARGIN = 10
SEND_W = 80


class ConsoleWriter(io.TextIOBase):
    def __init__(self, controller):
        self._controller = controller

    def write(self, text):
        if text:
            self._controller.enqueueLog_(text)
        return len(text)

    def flush(self):
        pass


class ConsoleController(NSObject):
    def init(self):
        self = objc.super(ConsoleController, self).init()
        if self is None:
            return None
        self._pending = queue.Queue()
        self._input = queue.Queue()
        self._transcript = ""
        self._repl_active = True
        return self

    def enqueueLog_(self, text):
        self._pending.put(text)

    def appendLog_(self, text):
        if not text:
            return
        self._transcript += text
        self.textView.setString_(self._transcript)
        self.textView.setTextColor_(NSColor.labelColor())
        self._scrollToEnd()

    def _scrollToEnd(self):
        length = self.textView.string().length()
        if length:
            self.textView.scrollRangeToVisible_((length - 1, 1))

    def _logWidth(self):
        """Use the scroll view frame — contentSize is 0 before first layout."""
        w = self.scrollView.frame().size.width - 16
        return max(int(w), 200)

    def _fixTextViewLayout(self):
        w = self._logWidth()
        h = max(self.textView.frame().size.height, self.scrollView.frame().size.height)
        self.textView.setFrame_(NSMakeRect(0, 0, w, h))
        self.textView.setMinSize_(NSMakeSize(w, 0))
        self.textView.setMaxSize_(NSMakeSize(w, 1_000_000))
        container = self.textView.textContainer()
        container.setWidthTracksTextView_(True)
        container.setContainerSize_(NSMakeSize(w, 1_000_000))

    def drainLog_(self, timer):
        while True:
            try:
                chunk = self._pending.get_nowait()
            except queue.Empty:
                break
            self.appendLog_(chunk)

    def inputFn(self):
        def read_line(prompt=""):
            return self._input.get()

        return read_line

    def submitFromField_(self, sender):
        text = self.inputField.stringValue().strip()
        if not text:
            return
        # Show in log BEFORE clearing the field.
        self.appendLog_(f"you > {text}\n")
        self.inputField.setStringValue_("")
        if text in ("exit", "quit"):
            self._repl_active = False
            self._input.put(text)
            NSApplication.sharedApplication().terminate_(None)
            return
        self._input.put(text)
        self.window.makeFirstResponder_(self.inputField)

    def control_textView_doCommandBySelector_(self, control, textView, commandSelector):
        if str(commandSelector) == "insertNewline:":
            self.submitFromField_(control)
            return True
        return False

    def windowDidResize_(self, notification):
        self.relayoutWindow()

    def relayoutWindow(self):
        bounds = self.window.contentView().bounds()
        w = bounds.size.width
        h = bounds.size.height
        self.scrollView.setFrame_(NSMakeRect(0, INPUT_H, w, max(100, h - INPUT_H)))
        field_w = max(100, w - MARGIN * 2 - SEND_W - 8)
        self.inputField.setFrame_(NSMakeRect(MARGIN, MARGIN, field_w, INPUT_H - MARGIN * 2))
        self.sendButton.setFrame_(NSMakeRect(w - MARGIN - SEND_W, MARGIN, SEND_W, INPUT_H - MARGIN * 2))
        self._fixTextViewLayout()
        self.textView.setString_(self._transcript)
        self._scrollToEnd()

    def buildWindow(self):
        app = NSApplication.sharedApplication()
        style = (
            NSWindowStyleMaskTitled
            | NSWindowStyleMaskClosable
            | NSWindowStyleMaskMiniaturizable
            | NSWindowStyleMaskResizable
        )
        self.window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(120, 120, 800, 560), style, NSBackingStoreBuffered, False
        )
        self.window.setTitle_("Daimon")
        self.window.setMinSize_(NSMakeSize(500, 340))
        self.window.setDelegate_(self)
        content = self.window.contentView()
        cw = content.bounds().size.width
        ch = content.bounds().size.height

        # Input bar (bottom) — add first so scroll sits behind but input stays on top when re-added
        self.inputField = NSTextField.alloc().initWithFrame_(NSMakeRect(MARGIN, MARGIN, cw - 120, 30))
        self.inputField.setAutoresizingMask_(NSViewWidthSizable | NSViewMaxYMargin)
        self.inputField.setBezelStyle_(NSBezelStyleRounded)
        self.inputField.setPlaceholderString_("Type here — Return or Send")
        self.inputField.setFont_(NSFont.systemFontOfSize_(14))
        self.inputField.setDelegate_(self)

        self.sendButton = NSButton.alloc().initWithFrame_(NSMakeRect(cw - MARGIN - SEND_W, MARGIN, SEND_W, 30))
        self.sendButton.setAutoresizingMask_(NSViewMinXMargin | NSViewMaxYMargin)
        self.sendButton.setTitle_("Send")
        self.sendButton.setBezelStyle_(NSBezelStyleRounded)
        self.sendButton.setTarget_(self)
        self.sendButton.setAction_("submitFromField:")

        log_h = max(100, ch - INPUT_H)
        self.scrollView = NSScrollView.alloc().initWithFrame_(NSMakeRect(0, INPUT_H, cw, log_h))
        self.scrollView.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)
        self.scrollView.setHasVerticalScroller_(True)
        self.scrollView.setAutohidesScrollers_(False)
        self.scrollView.setBorderType_(1)
        self.scrollView.setDrawsBackground_(True)
        self.scrollView.setBackgroundColor_(NSColor.textBackgroundColor())

        # Frame width from scroll view, NOT clip contentSize (often 0 at init).
        self.textView = NSTextView.alloc().initWithFrame_(NSMakeRect(0, 0, cw - 16, log_h))
        self.textView.setEditable_(False)
        self.textView.setSelectable_(True)
        self.textView.setRichText_(False)
        self.textView.setFont_(NSFont.monospacedSystemFontOfSize_weight_(14, 0))
        self.textView.setTextColor_(NSColor.labelColor())
        self.textView.setBackgroundColor_(NSColor.textBackgroundColor())
        self.textView.setHorizontallyResizable_(False)
        self.textView.setVerticallyResizable_(True)
        self.textView.setAutoresizingMask_(NSViewWidthSizable)
        self.scrollView.setDocumentView_(self.textView)

        content.addSubview_(self.scrollView)
        content.addSubview_(self.inputField)
        content.addSubview_(self.sendButton)

        self.relayoutWindow()
        self.appendLog_(
            "─── Daimon log ───\n"
            "local · gated · logged\n"
            "Connecting to Ollama (localhost:11434)…\n\n"
        )

        NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            0.05, self, "drainLog:", None, True
        )

        self.window.makeKeyAndOrderFront_(None)
        self.window.orderFrontRegardless()
        app.activateIgnoringOtherApps_(True)
        self.window.makeFirstResponder_(self.inputField)
        AppHelper.callLater(0.2, self.relayoutWindow)

    def startRepl_(self, repl_fn):
        sys.stdout = ConsoleWriter(self)
        sys.stderr = ConsoleWriter(self)

        def run():
            try:
                repl_fn(self.inputFn())
            except Exception:
                traceback.print_exc()
                self.enqueueLog_("\n[agent stopped — see traceback above]\n")
            finally:
                self._repl_active = False

        threading.Thread(target=run, daemon=True).start()


def run_console(repl_fn):
    NSApplication.sharedApplication()
    controller = ConsoleController.alloc().init()
    controller.buildWindow()
    controller.startRepl_(repl_fn)
    AppHelper.runEventLoop()
