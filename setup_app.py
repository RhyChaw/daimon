#!/usr/bin/env python3
"""Build Daimon.app with py2app."""

from setuptools import setup

APP = ["mac_agent/app_main.py"]

PLIST = {
    "CFBundleName": "Daimon",
    "CFBundleDisplayName": "Daimon",
    "CFBundleIdentifier": "com.rhychaw.daimon",
    "CFBundleVersion": "0.0.1",
    "CFBundleShortVersionString": "0.0.1",
    "CFBundlePackageType": "APPL",
    "LSMinimumSystemVersion": "12.0",
    "NSHighResolutionCapable": True,
    "NSAppleEventsUsageDescription": (
        "Daimon controls Mail and Calendar to draft emails and read your schedule."
    ),
    "NSCalendarsUsageDescription": (
        "Daimon reads today's calendar events when you ask about your schedule."
    ),
    "NSHumanReadableCopyright": "Copyright © 2026",
}

OPTIONS = {
    "py2app": {
        "argv_emulation": False,
        "plist": PLIST,
        "packages": ["mac_agent"],
        "includes": [
            "LocalAuthentication",
            "AppKit",
            "Foundation",
            "PyObjCTools.AppHelper",
        ],
        # Avoid strip invalidating signatures mid-build (py2app re-signs after).
        "strip": False,
        "excludes": [
            "setuptools",
            "distutils",
            "pip",
            "wheel",
            "pytest",
            "tkinter",
            "test",
            "tests",
        ],
    }
}

setup(
    name="Daimon",
    app=APP,
    options={"py2app": OPTIONS["py2app"]},
    setup_requires=["py2app"],
)
