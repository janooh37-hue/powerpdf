"""Lightweight entry point shared by the frozen TUI and command shims."""
from __future__ import annotations

import importlib
import json
import subprocess
import sys
from importlib.metadata import version
from pathlib import Path


def main() -> int | None:
    args = sys.argv[1:]
    if args == ["--version"]:
        print(f"powerpdf {version('powerpdf')}")
        return 0
    if args == ["--runtime-info"]:
        from pdf_card import TESSERACT
        from pdf_runtime import resolve_tool
        print(json.dumps({"ghostscript": resolve_tool("ghostscript"),
                          "tesseract": TESSERACT, "fzf": resolve_tool("fzf")}))
        return 0
    if len(args) >= 2 and args[0] == "--cli":
        command = args[1]
        sys.argv = [f"pdf{command}", *args[2:]]
        if command == "print":
            script = Path(__file__).with_name("pdf_print.ps1")
            return subprocess.call(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
                                    "-File", str(script), *args[2:]])
        if command in ("split", "merge", "compress", "image"):
            return importlib.import_module(f"pdf_{command}").main()
        raise SystemExit(f"Unknown PowerPDF command: {command}")
    from powerpdf import main as tui_main
    return tui_main()


if __name__ == "__main__":
    raise SystemExit(main())
