"""Frozen console entry, including native macOS executable-name aliases."""
from pathlib import Path
import sys

from powerpdf_cli import main

if __name__ == "__main__":
    alias = Path(sys.argv[0]).stem
    if alias in {"pdfsplit", "pdfmerge", "pdfcompress", "pdfimage"}:
        sys.argv[1:1] = ["--cli", alias[3:]]
    raise SystemExit(main())
