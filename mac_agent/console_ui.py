"""
console_ui.py — Cocoa console for Daimon.app.

Log area on top, input bar pinned to bottom. Transcript grows with content
and auto-scrolls to the latest line.
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
    NSFontAttributeName,
    NSForegroundColorAttributeName,
    NSMakeRect,
    NSPopUpButton,
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
from Foundation import NSAttributedString, NSMakeSize, NSObject, NSTimer
import objc
from PyObjCTools import AppHelper

INPUT_H = 50
MARGIN = 10
SEND_W = 80
BACKEND_W = 130


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

    def _logAttributes(self):
        return {
            NSFontAttributeName: NSFont.monospacedSystemFontOfSize_weight_(14, 0),
            NSForegroundColorAttributeName: NSColor.labelColor(),
        }

    def appendLog_(self, text):
        if not text:
            return
        self._transcript += text
        storage = self.textView.textStorage()
        storage.appendAttributedString_(
            NSAttributedString.alloc().initWithString_attributes_(text, self._logAttributes())
        )
        self._syncTextViewSize()
        self._scrollToEnd()

    def _syncTextViewSize(self):
        w = self._logWidth()
        container = self.textView.textContainer()
        container.setWidthTracksTextView_(False)
        container.setContainerSize_((w, 1.0e7))
        layout = self.textView.layoutManager()
        layout.ensureLayoutForTextContainer_(container)
        used = layout.usedRectForTextContainer_(container)
        content_h = max(used.size.height + 24, self.scrollView.frame().size.height)
        self.textView.setMinSize_(NSMakeSize(w, content_h))
        self.textView.setMaxSize_(NSMakeSize(w, content_h))
        self.textView.setFrameSize_(NSMakeSize(w, content_h))

    def _scrollToEnd(self):
        self.textView.scrollToEndOfDocument_(None)
        clip = self.scrollView.contentView()
        doc_h = self.textView.frame().size.height
        view_h = clip.bounds().size.height
        if doc_h > view_h:
            clip.scrollToPoint_((0, doc_h - view_h))
            self.scrollView.reflectScrolledClipView_(clip)

    def _logWidth(self):
        w = self.scrollView.frame().size.width - 16
        return max(int(w), 200)

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

    def backendChanged_(self, sender):
        from mac_agent import settings

        name = "claude" if sender.indexOfSelectedItem() == 0 else "ollama"
        settings.set_backend(name)
        self.appendLog_(f"\n  backend → {name}\n")

    def _syncBackendPopup(self):
        from mac_agent import settings

        name = settings.get_backend()
        self.backendPopup.selectItemAtIndex_(0 if name == "claude" else 1)

    def relayoutWindow(self):
        bounds = self.window.contentView().bounds()
        w = bounds.size.width
        h = bounds.size.height
        self.scrollView.setFrame_(NSMakeRect(0, INPUT_H, w, max(100, h - INPUT_H)))
        field_x = MARGIN + BACKEND_W + 8
        field_w = max(100, w - field_x - MARGIN - SEND_W - 8)
        self.backendPopup.setFrame_(NSMakeRect(MARGIN, MARGIN, BACKEND_W, INPUT_H - MARGIN * 2))
        self.inputField.setFrame_(NSMakeRect(field_x, MARGIN, field_w, INPUT_H - MARGIN * 2))
        self.sendButton.setFrame_(NSMakeRect(w - MARGIN - SEND_W, MARGIN, SEND_W, INPUT_H - MARGIN * 2))
        self._syncTextViewSize()
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

        self.backendPopup = NSPopUpButton.alloc().initWithFrame_(
            NSMakeRect(MARGIN, MARGIN, BACKEND_W, 30)
        )
        self.backendPopup.addItemsWithTitles_(["Claude", "Ollama"])
        self.backendPopup.setAutoresizingMask_(NSViewMaxYMargin)
        self.backendPopup.setTarget_(self)
        self.backendPopup.setAction_("backendChanged:")
        self._syncBackendPopup()

        self.inputField = NSTextField.alloc().initWithFrame_(
            NSMakeRect(MARGIN + BACKEND_W + 8, MARGIN, cw - BACKEND_W - 140, 30)
        )
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
        content.addSubview_(self.backendPopup)
        content.addSubview_(self.inputField)
        content.addSubview_(self.sendButton)

        self.relayoutWindow()
        from mac_agent import settings

        backend_name = settings.get_backend()
        connect_msg = (
            "Connecting to Claude (Anthropic API)…\n\n"
            if backend_name == "claude"
            else "Connecting to Ollama (localhost:11434)…\n\n"
        )
        self.appendLog_(
            "─── Daimon log ───\n"
            "gated · logged\n"
            f"{connect_msg}"
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
