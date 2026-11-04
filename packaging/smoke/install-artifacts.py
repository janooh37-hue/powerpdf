"""Install and exercise real native products; run after build-desktop.py in CI.

Evidence stays in build/evidence/: real GUI PNGs, frozen CLI transformations,
all three OCR model executions, installed native linkage, and installer logs.
The Linux Docker probe deliberately uses an untouched ubuntu:24.04 base with no
apt installs and no source-tree imports or host Python/runtime library mounts.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import platform
import plistlib
import runpy
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
import zipfile

ROOT = Path(__file__).resolve().parents[2]
DIST = ROOT / "dist"
EVIDENCE = ROOT / "build/evidence"
VERSION = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
PROBE = runpy.run_path(str(Path(__file__).with_name("product-smoke.py")))


def run(*args: object, cwd: Path | None = None) -> None:
    command = list(map(str, args))
    print("+", " ".join(command), flush=True)
    process = subprocess.run(command, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             encoding="utf-8", errors="replace", timeout=900)
    with (EVIDENCE / "installer-commands.log").open("a", encoding="utf-8") as log:
        log.write("+ " + " ".join(command) + "\n" + process.stdout + f"\nexit={process.returncode}\n")
    print(process.stdout, flush=True)
    if process.returncode:
        raise RuntimeError(f"Installer command failed ({process.returncode}): {command!r}")


def smoke(gui: Path, cli: Path, label: str, payload: Path | None = None) -> None:
    PROBE["smoke"](gui, cli, EVIDENCE / label, payload)


def windows() -> None:
    stem = f"powerpdf-{VERSION}-windows-x64"
    target = Path(os.environ["RUNNER_TEMP"]) / "installed-powerpdf"
    run(DIST / f"{stem}-setup.exe", "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/SP-", f"/DIR={target}")
    try:
        smoke(target / "bin/powerpdf-gui.exe", target / "bin/powerpdf.exe", "windows-x64-installed")
    finally:
        run(target / "unins000.exe", "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART",
            f"/LOG={EVIDENCE / 'windows-x64-installed/uninstall.log'}")
    # Inno's uninstaller self-deletes from a temporary child after its launcher
    # exits. Wait for completion rather than racing that asynchronous cleanup.
    deadline = time.monotonic() + 30
    while remaining := [str(path.relative_to(target)) for path in target.rglob("*") if path.is_file()]:
        if time.monotonic() >= deadline:
            raise RuntimeError(f"Windows uninstall left installed product files: {remaining}")
        time.sleep(0.25)
    with tempfile.TemporaryDirectory(prefix="powerpdf-portable-") as temporary:
        portable = Path(temporary)
        with zipfile.ZipFile(DIST / f"{stem}.zip") as archive:
            archive.extractall(portable)
        entries = list(portable.rglob("powerpdf-gui.exe"))
        if len(entries) != 1:
            raise RuntimeError(f"Portable archive must contain one GUI executable: {entries}")
        smoke(entries[0], entries[0].with_name("powerpdf.exe"), "windows-x64-portable")


def macos() -> None:
    arch = "arm64" if platform.machine() == "arm64" else "x64"
    image = DIST / f"powerpdf-{VERSION}-macos-{arch}.dmg"
    with tempfile.TemporaryDirectory(prefix="powerpdf-mounted-") as temporary:
        mount = Path(temporary)
        run("hdiutil", "attach", "-nobrowse", "-readonly", "-mountpoint", mount, image)
        try:
            app = mount / "PowerPDF.app"
            info = plistlib.loads((app / "Contents/Info.plist").read_bytes())
            if info["CFBundleExecutable"] != "powerpdf-gui":
                raise RuntimeError(f"DMG is not the windowed PowerPDF application: {info}")
            run("codesign", "--verify", "--deep", "--strict", app)
            smoke(app / "Contents/MacOS/powerpdf-gui", app / "Contents/MacOS/powerpdf", f"macos-{arch}-mounted")
        finally:
            run("hdiutil", "detach", mount)


def linux() -> None:
    stem = f"powerpdf-{VERSION}-linux-x64"
    image = DIST / f"{stem}.AppImage"
    image.chmod(0o755)
    with tempfile.TemporaryDirectory(prefix="powerpdf-appimage-") as temporary:
        extracted = Path(temporary)
        run(image, "--appimage-extract", cwd=extracted)
        payload = extracted / "squashfs-root/usr/lib/powerpdf/_internal"
        # Launch the actual AppImage, not merely the onedir build output.
        smoke(image, image, "linux-x64-appimage", payload)
    run("sudo", "dpkg", "-i", DIST / f"{stem}.deb")
    try:
        smoke(Path("/usr/bin/PowerPDF"), Path("/usr/bin/powerpdf"), "linux-x64-deb")
    finally:
        run("sudo", "dpkg", "--purge", "powerpdf")
    launchers = ("PowerPDF", "powerpdf", "pdfsplit", "pdfmerge", "pdfcompress", "pdfimage")
    if (any(os.path.lexists("/usr/bin/" + name) for name in launchers)
            or any(path.is_file() for path in Path("/opt/powerpdf").rglob("*"))):
        raise RuntimeError("Debian uninstall left installed product files or launchers")
    container = EVIDENCE / "linux-x64-clean-ubuntu24"
    container.mkdir(parents=True, exist_ok=True)
    fixture_dir = EVIDENCE / "container-fixtures"
    PROBE["fixtures"](fixture_dir)
    run("docker", "run", "--rm", "--network=none",
        "-v", f"{DIST}:/artifacts:ro", "-v", f"{EVIDENCE}:/evidence",
        "-v", f"{Path(__file__).with_name('clean-linux.sh')}:/clean-linux.sh:ro",
        "ubuntu:24.04", "bash", "/clean-linux.sh", f"/artifacts/{image.name}",
        "/evidence/container-fixtures", "/evidence/linux-x64-clean-ubuntu24")
    PROBE["png"](container / "powerpdf-gui.png")
    if json.loads((container / "product-smoke.json").read_text(encoding="utf-8"))["status"] != "passed":
        raise RuntimeError("Clean Ubuntu product smoke did not complete")


def main() -> None:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    for name in ("ghostscript-10.08.0-source.tar.xz", "powerpdf-corresponding-source.tar.gz"):
        if not (DIST / name).is_file() or not (DIST / name).stat().st_size:
            raise RuntimeError(f"Missing corresponding source artifact: {name}")
    if (DIST / "native-linkage.txt").is_file():
        shutil.copy2(DIST / "native-linkage.txt", EVIDENCE / "build-native-linkage.txt")
    if sys.platform == "win32":
        windows()
    elif sys.platform == "darwin":
        macos()
    else:
        linux()
    for path in EVIDENCE.rglob("powerpdf-gui.png"):
        print("SCREENSHOT", path, path.stat().st_size, flush=True)


if __name__ == "__main__":
    main()
