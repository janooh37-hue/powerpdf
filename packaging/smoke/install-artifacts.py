"""Exercise installer outputs, never the unfrozen Python source tree."""
from __future__ import annotations

import json
import os
from pathlib import Path
import platform
import plistlib
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
DIST = ROOT / "dist"
EVIDENCE = ROOT / "build/evidence"
NAME = "PowerPDF-Packaging-Smoke"


def run(*args: object, env: dict[str, str] | None = None) -> None:
    print("+", " ".join(map(str, args)), flush=True)
    subprocess.run(list(map(str, args)), check=True, env=env, timeout=300)


def smoke(executable: Path, label: str) -> None:
    env = os.environ.copy()
    env["QT_QPA_PLATFORM"] = "offscreen"
    env["PYTHONIOENCODING"] = "utf-8"
    output = EVIDENCE / label
    output.mkdir(parents=True, exist_ok=True)
    run(executable, "--output", output, env=env)
    if not (output / "qt-smoke.png").is_file():
        raise RuntimeError(f"Missing screenshot: {label}")
    print("SMOKE", label, (output / "runtime.json").read_text(), flush=True)


def main() -> None:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        target = Path(os.environ["RUNNER_TEMP"]) / "installed-smoke"
        run(DIST / "powerpdf-packaging-smoke-windows-x64-setup.exe", "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/SP-", f"/DIR={target}")
        try:
            smoke(target / (NAME + ".exe"), "windows-x64-installed")
        finally:
            run(target / "unins000.exe", "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART")
        if (target / (NAME + ".exe")).exists():
            raise RuntimeError("Uninstall left the harness installed")
    elif sys.platform == "darwin":
        arch = "arm64" if platform.machine() == "arm64" else "x64"
        image = DIST / f"powerpdf-packaging-smoke-macos-{arch}.dmg"
        mount = ROOT / "build/mounted-smoke"
        mount.mkdir(parents=True, exist_ok=True)
        run("hdiutil", "attach", "-nobrowse", "-readonly", "-mountpoint", mount, image)
        try:
            app = mount / (NAME + ".app")
            run("codesign", "--verify", "--deep", "--strict", app)
            smoke(app / "Contents/MacOS" / NAME, "macos-" + arch + "-mounted")
        finally:
            run("hdiutil", "detach", mount)
    else:
        image = DIST / "powerpdf-packaging-smoke-linux-x64.AppImage"
        image.chmod(0o755)
        env = os.environ.copy()
        env.update({"QT_QPA_PLATFORM": "offscreen", "APPIMAGE_EXTRACT_AND_RUN": "1"})
        output = EVIDENCE / "linux-x64-appimage"
        output.mkdir(exist_ok=True)
        run(image, "--appimage-extract-and-run", "--output", output, env=env)
        run("sudo", "dpkg", "-i", DIST / "powerpdf-packaging-smoke-linux-x64.deb")
        try:
            smoke(Path("/usr/bin/powerpdf-packaging-smoke"), "linux-x64-deb")
        finally:
            run("sudo", "dpkg", "-r", "powerpdf-packaging-smoke")
        # No apt installs in the container: only the unmodified Ubuntu base and
        # our AppImage. Offscreen must load without borrowing runner libraries.
        run("docker", "run", "--rm", "-v", f"{DIST}:/artifacts:ro", "-v", f"{EVIDENCE}:/evidence",
            "-e", "QT_QPA_PLATFORM=offscreen", "ubuntu:24.04", "bash", "-ec",
            'cd /tmp; /artifacts/powerpdf-packaging-smoke-linux-x64.AppImage --appimage-extract-and-run --output /evidence/linux-x64-clean-ubuntu24; '
            '/artifacts/powerpdf-packaging-smoke-linux-x64.AppImage --appimage-extract >/dev/null; '
            'export LD_LIBRARY_PATH=/tmp/squashfs-root/usr/lib/powerpdf-packaging-smoke/_internal/vendor/lib; '
            'for binary in /tmp/squashfs-root/usr/lib/powerpdf-packaging-smoke/_internal/vendor/ghostscript/bin/gs '
            '/tmp/squashfs-root/usr/lib/powerpdf-packaging-smoke/_internal/vendor/tesseract/bin/tesseract; do '
            'echo "$binary"; ldd "$binary"; done | tee /evidence/clean-container-ldd.txt; '
            '! grep -q "not found" /evidence/clean-container-ldd.txt')
    for path in EVIDENCE.rglob("qt-smoke.png"):
        print("SCREENSHOT", path, path.stat().st_size, flush=True)


if __name__ == "__main__":
    main()
