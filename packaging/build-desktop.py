"""Build CI-only Qt probes natively; never publish or impersonate the GUI app."""
from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import urllib.request

ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / "build/desktop"
DIST = ROOT / "dist"
STAGE = ROOT / "build/desktop-vendor"
VENDOR = STAGE / "vendor"
LICENSES = STAGE / "licenses"
INPUTS = json.loads((ROOT / "packaging/desktop-inputs.json").read_text())
CONFIG = json.loads((ROOT / "packaging/desktop-config.json").read_text())
NAME = CONFIG["harness"]["name"]
ARCH = "arm64" if platform.machine().lower() in ("arm64", "aarch64") else "x64"
OS = {"win32": "windows", "darwin": "macos", "linux": "linux"}[sys.platform]
STEM = f"powerpdf-packaging-smoke-{OS}-{ARCH}"


def run(*args: object, cwd: Path = ROOT, env: dict[str, str] | None = None) -> None:
    print("+", " ".join(map(str, args)), flush=True)
    subprocess.run(list(map(str, args)), cwd=cwd, env=env, check=True)


def fetch(record: dict[str, str], destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not destination.exists():
        print("Downloading", record["url"], flush=True)
        with urllib.request.urlopen(record["url"]) as response, destination.open("wb") as stream:
            shutil.copyfileobj(response, stream)
    digest = hashlib.file_digest(destination.open("rb"), "sha256").hexdigest()
    if digest != record["sha256"]:
        raise RuntimeError(f"Checksum mismatch: {destination}: {digest}")
    return destination


def source(name: str) -> Path:
    archive = fetch(INPUTS[name], BUILD / "downloads" / (name + ".tar"))
    target = BUILD / "sources" / name
    target.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive) as bundle:
        bundle.extractall(target, filter="data")
    return next(path for path in target.iterdir() if path.is_dir())


def cmake_build(name: str, path: Path, prefix: Path, *options: str) -> None:
    target = BUILD / (name + "-build")
    run("cmake", "-S", path, "-B", target, "-DCMAKE_BUILD_TYPE=Release",
        f"-DCMAKE_INSTALL_PREFIX={prefix}", f"-DCMAKE_PREFIX_PATH={prefix}",
        "-DBUILD_SHARED_LIBS=OFF", "-DCMAKE_OSX_DEPLOYMENT_TARGET=13.0", *options)
    run("cmake", "--build", target, "--parallel", "3")
    run("cmake", "--install", target)


def macos_helpers() -> None:
    # Reproducible source builds, not Homebrew bottles: static helper closure and
    # Apple system libraries only. No absolute Homebrew install names to rewrite.
    prefix = BUILD / "native-prefix"
    zlib = source("zlib")
    run("./configure", "--static", f"--prefix={prefix}", cwd=zlib)
    run("make", "-j3", cwd=zlib)
    run("make", "install", cwd=zlib)
    png = source("libpng")
    cmake_build("png", png, prefix, "-DPNG_SHARED=OFF", "-DPNG_FRAMEWORK=OFF", "-DPNG_TESTS=OFF",
                f"-DZLIB_LIBRARY={prefix}/lib/libz.a", f"-DZLIB_INCLUDE_DIR={prefix}/include")
    # Leptonica's built-in bitmap fonts are TIFF-compressed even when the app
    # sends PNG images. Include that codec rather than accepting missing-font
    # diagnostics from an otherwise successful Tesseract process.
    tiff = source("libtiff")
    cmake_build("tiff", tiff, prefix, "-Dtiff-tools=OFF", "-Dtiff-tests=OFF",
                "-Dtiff-contrib=OFF", "-Dtiff-docs=OFF", "-Dtiff-opengl=OFF", "-Dcxx=OFF",
                "-Djpeg=OFF", "-Dold-jpeg=OFF", "-Djpeg12=OFF", "-Djbig=OFF", "-Dlerc=OFF",
                "-Dlzma=OFF", "-Dzstd=OFF", "-Dwebp=OFF", "-Dlibdeflate=OFF",
                f"-DZLIB_LIBRARY={prefix}/lib/libz.a", f"-DZLIB_INCLUDE_DIR={prefix}/include")
    lept = source("leptonica")
    cmake_build("leptonica", lept, prefix, "-DBUILD_PROG=OFF", "-DENABLE_GIF=OFF", "-DENABLE_JPEG=OFF",
                "-DENABLE_TIFF=ON", "-DENABLE_WEBP=OFF", "-DENABLE_OPENJPEG=OFF",
                f"-DTIFF_LIBRARY={prefix}/lib/libtiff.a", f"-DTIFF_INCLUDE_DIR={prefix}/include",
                f"-DPNG_LIBRARY={prefix}/lib/libpng.a", f"-DPNG_PNG_INCLUDE_DIR={prefix}/include",
                f"-DZLIB_LIBRARY={prefix}/lib/libz.a", f"-DZLIB_INCLUDE_DIR={prefix}/include")
    tess = source("tesseract")
    cmake_build("tesseract", tess, prefix, "-DBUILD_TRAINING_TOOLS=OFF", "-DBUILD_TESTS=OFF",
                "-DDISABLE_ARCHIVE=ON", "-DDISABLE_CURL=ON", "-DOPENMP_BUILD=OFF",
                "-DDISABLE_TIFF=ON", "-DGRAPHICS_DISABLED=ON", "-DENABLE_NATIVE=OFF",
                f"-DLeptonica_DIR={prefix}/lib/cmake/leptonica")
    gs = source("ghostscript")
    gs_prefix = BUILD / "gs-prefix"
    env = os.environ.copy()
    env.update({"PKG_CONFIG": "/usr/bin/false", "PKGCONFIG": "/usr/bin/false", "MACOSX_DEPLOYMENT_TARGET": "13.0"})
    run("./configure", f"--prefix={gs_prefix}", "--without-x", "--disable-cups", "--disable-gtk",
        "--without-tesseract", "--without-libidn", "--without-libpaper", "--disable-fontconfig", "--disable-dbus",
        "--enable-compile-inits", cwd=gs, env=env)
    run("make", "-j3", cwd=gs, env=env)
    run("make", "install", cwd=gs, env=env)
    for name, binary in (("ghostscript", gs_prefix / "bin/gs"), ("tesseract", prefix / "bin/tesseract")):
        target = VENDOR / name / "bin" / binary.name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(binary, target)
        run("codesign", "--force", "-s", "-", target)
    for part in ("lib", "Resource", "iccprofiles"):
        shutil.copytree(gs / part, VENDOR / "ghostscript" / part, dirs_exist_ok=True)
    for name, src in (("Ghostscript", gs), ("Tesseract", tess), ("Leptonica", lept), ("libpng", png), ("libtiff", tiff), ("zlib", zlib)):
        # Static codec libraries retain their own notices, not only GS's AGPL.
        for license_file in src.rglob("*"):
            if license_file.is_file() and license_file.name.lower().startswith(("license", "copying", "copyright", "notice")):
                destination = LICENSES / name / license_file.relative_to(src)
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(license_file, destination)
    proof = []
    for binary in (VENDOR / "ghostscript/bin/gs", VENDOR / "tesseract/bin/tesseract"):
        output = subprocess.check_output(["otool", "-L", str(binary)], text=True)
        proof.append(output)
        print(output, flush=True)
        for line in output.splitlines()[1:]:
            dependency = line.strip().split(" ")[0]
            if not dependency.startswith(("/usr/lib/", "/System/Library/")):
                raise RuntimeError(f"Nonrelocatable helper dependency: {dependency}")
    (DIST / "native-linkage.txt").write_text("\n".join(proof))


def linux_helpers() -> None:
    # Exact Ubuntu 22.04 debs and SHA256s are checked in, including the complete
    # helper link closure. Extracting avoids all reliance on runner-installed gs.
    lock = json.loads((ROOT / "packaging/linux-runtime-lock.json").read_text())
    extracted = BUILD / "ubuntu-runtime"
    for package in lock:
        archive = fetch(package, BUILD / "downloads" / (package["package"] + ".deb"))
        run("dpkg-deb", "-x", archive, extracted)
    # Ubuntu resources include absolute /var/lib and /usr/share symlinks.
    # Rebase them into the extracted root before dereferencing into the bundle.
    for link in extracted.rglob("*"):
        if link.is_symlink() and os.readlink(link).startswith("/"):
            target = extracted / os.readlink(link).lstrip("/")
            link.unlink()
            link.symlink_to(os.path.relpath(target, link.parent))
    (VENDOR / "lib").mkdir(parents=True, exist_ok=True)
    # Keep the host's glibc/loader together, never mix an extracted libc with it.
    excluded = re.compile(r"^(ld-linux|libnss_|libthread_db|lib(c|m|pthread|dl|rt|resolv|util|nsl|anl|BrokenLocale)\.so)")
    for directory in (extracted / "lib/x86_64-linux-gnu", extracted / "usr/lib/x86_64-linux-gnu"):
        for library in directory.glob("*.so*"):
            if not excluded.match(library.name) and library.is_file():
                shutil.copy2(library.resolve(), VENDOR / "lib" / library.name)
    for name, binary in (("ghostscript", "gs"), ("tesseract", "tesseract")):
        destination = VENDOR / name / "bin" / binary
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(extracted / "usr/bin" / binary, destination)
    gs_share = next((extracted / "usr/share/ghostscript").glob("9.*"))
    for part in ("lib", "Resource", "iccprofiles"):
        if (gs_share / part).exists():
            shutil.copytree(gs_share / part, VENDOR / "ghostscript" / part, dirs_exist_ok=True)
    shutil.copytree(extracted / "usr/share/fonts/type1/urw-base35", VENDOR / "ghostscript/Resource/Font", dirs_exist_ok=True)
    # Debian copyright files contain each package's licensing terms. Unrelated
    # NEWS/changelog symlinks can be dangling even in Ubuntu's own package.
    for package in lock:
        copyright_file = extracted / "usr/share/doc" / package["package"] / "copyright"
        destination = LICENSES / "ubuntu" / package["package"]
        destination.mkdir(parents=True, exist_ok=True)
        shutil.copy2(copyright_file, destination / "copyright")
    shutil.copytree(extracted / "usr/share/common-licenses", LICENSES / "ubuntu/common-licenses", dirs_exist_ok=True)
    shutil.copy2(ROOT / "packaging/linux-runtime-lock.json", STAGE / "LINUX-PROVENANCE.json")
    # Package copyright files contain upstream source links; this exact source
    # archive endpoint also retains Ubuntu's security patches and build recipes.
    pointers = []
    for package in lock:
        name, *version = package["source"].split()
        exact_version = version[0].strip("()") if version else package["version"]
        pointers.append(f"{package['package']} {package['version']}: https://launchpad.net/ubuntu/+source/{name}/{exact_version}")
    (STAGE / "LINUX-SOURCES.txt").write_text("\n".join(pointers) + "\n")


def common_data() -> None:
    fetch(INPUTS["font"], STAGE / "smoke-font.ttf")
    fetch(INPUTS["latin_font"], STAGE / "smoke-latin-font.ttf")
    fetch(INPUTS["font_license"], LICENSES / "Noto-OFL.txt")
    fetch(INPUTS["tessdata_license"], LICENSES / "Tessdata-Apache-2.0.txt")
    fetch(INPUTS["python_license"], LICENSES / "CPython-3.13.11.txt")
    for language, checksum in {
        "eng": "7d4322bd2a7749724879683fc3912cb542f19906c83bcc1a52132556427170b2",
        "ara": "e3206d3dc87fd50c24a0fb9f01838615911d25168f4e64415244b67d2bb3e729",
        "osd": "9cf5d576fcc47564f11265841e5ca839001e7e6f38ff7f7aacf46d15a96b00ff",
    }.items():
        fetch({"url": f"https://raw.githubusercontent.com/tesseract-ocr/tessdata_fast/4.1.0/{language}.traineddata", "sha256": checksum},
              VENDOR / "tesseract/tessdata" / (language + ".traineddata"))
    gs_source = fetch(INPUTS["ghostscript"], DIST / "ghostscript-10.08.0-source.tar.xz")
    with tarfile.open(gs_source) as archive:
        copying = archive.getmember("ghostscript-10.08.0/doc/COPYING")
        (LICENSES / "PowerPDF-AGPL-3.0-or-later.txt").write_bytes(archive.extractfile(copying).read())
    shutil.copy2(ROOT / "packaging/desktop-inputs.json", STAGE / "SOURCE-INPUTS.json")
    shutil.copy2(ROOT / "packaging/DESKTOP-NOTICES.txt", STAGE / "THIRD-PARTY-NOTICES.txt")
    shutil.copy2(ROOT / "packaging/DESKTOP-INSTALL.txt", STAGE / "FIRST-LAUNCH.txt")
    for distribution in importlib.metadata.distributions():
        for file in distribution.files or []:
            if any(part.lower().startswith(("license", "copying", "notice")) for part in file.parts):
                src = Path(distribution.locate_file(file))
                if src.is_file():
                    dest = LICENSES / "python" / distribution.metadata["Name"] / str(file)
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dest)
    run("git", "archive", "--format=tar.gz", "--prefix=powerpdf-source/", "-o", DIST / "powerpdf-corresponding-source.tar.gz", "HEAD")


def freeze() -> Path:
    args = [sys.executable, "-m", "PyInstaller", CONFIG["harness"]["entry"], "--onedir", "--name", NAME,
            "--noconfirm", "--clean", "--paths", str(ROOT), "--distpath", str(DIST),
            "--workpath", str(BUILD / "pyinstaller"), "--specpath", str(BUILD),
            "--collect-data", "pypdfium2", "--collect-data", "pypdfium2_raw"]
    for module in CONFIG["harness"]["hidden_imports"]:
        args.extend(["--hidden-import", module])
    for module in CONFIG["harness"]["collect_submodules"]:
        args.extend(["--collect-submodules", module])
    for module in CONFIG["excluded_modules"]:
        args.extend(["--exclude-module", module])
    # Add helpers after freezing: on macOS static helper binaries must not be
    # rewritten/flattened by PyInstaller's Python-library dependency collector.
    args.append("--windowed" if sys.platform == "darwin" else "--console")
    run(*args)
    payload = DIST / (NAME + ".app") / "Contents/Resources" if sys.platform == "darwin" else DIST / NAME / "_internal"
    shutil.copytree(STAGE, payload, dirs_exist_ok=True)
    if sys.platform == "darwin":
        run("codesign", "--force", "--deep", "-s", "-", DIST / (NAME + ".app"))
        run("codesign", "--verify", "--deep", "--strict", DIST / (NAME + ".app"))
    return payload


def linux_installers(payload: Path) -> None:
    appdir = BUILD / "AppDir"
    shutil.copytree(DIST / NAME, appdir / "usr/lib/powerpdf-packaging-smoke", dirs_exist_ok=True)
    (appdir / "AppRun").write_text('#!/bin/sh\nHERE="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"\nexport LD_LIBRARY_PATH="$HERE/usr/lib/powerpdf-packaging-smoke/_internal/vendor/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"\nexec "$HERE/usr/lib/powerpdf-packaging-smoke/' + NAME + '" "$@"\n')
    (appdir / "AppRun").chmod(0o755)
    shutil.copy2(ROOT / "packaging/powerpdf-packaging-smoke.desktop", appdir)
    shutil.copy2(ROOT / "packaging/powerpdf-packaging-smoke.svg", appdir)
    tool = fetch(INPUTS["appimagetool"], BUILD / "appimagetool.AppImage")
    runtime = fetch(INPUTS["appimage_runtime"], BUILD / "appimage-runtime-x86_64")
    tool.chmod(0o755)
    env = os.environ.copy()
    env.update({"ARCH": "x86_64", "APPIMAGE_EXTRACT_AND_RUN": "1"})
    run(tool, "--no-appstream", "--runtime-file", runtime, appdir, DIST / (STEM + ".AppImage"), env=env)
    deb = BUILD / "deb"
    install = deb / "opt/powerpdf-packaging-smoke"
    shutil.copytree(appdir / "usr/lib/powerpdf-packaging-smoke", install, dirs_exist_ok=True)
    for src, dest in ((ROOT / "packaging/powerpdf-packaging-smoke.desktop", deb / "usr/share/applications/powerpdf-packaging-smoke.desktop"),
                      (ROOT / "packaging/powerpdf-packaging-smoke.svg", deb / "usr/share/icons/hicolor/scalable/apps/powerpdf-packaging-smoke.svg")):
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
    launcher = deb / "usr/bin/powerpdf-packaging-smoke"
    launcher.parent.mkdir(parents=True, exist_ok=True)
    launcher.write_text('#!/bin/sh\nexport LD_LIBRARY_PATH="/opt/powerpdf-packaging-smoke/_internal/vendor/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"\nexec /opt/powerpdf-packaging-smoke/' + NAME + ' "$@"\n')
    launcher.chmod(0o755)
    (deb / "DEBIAN").mkdir(parents=True, exist_ok=True)
    (deb / "DEBIAN/control").write_text("Package: powerpdf-packaging-smoke\nVersion: 0.7.0\nArchitecture: amd64\nMaintainer: PowerPDF <noreply@github.com>\nDepends: libc6 (>= 2.35)\nSection: graphics\nPriority: optional\nDescription: PowerPDF CI packaging probe (not the application)\n")
    run("dpkg-deb", "--root-owner-group", "--build", deb, DIST / (STEM + ".deb"))
    # ldd proof uses only the packaged loader search paths, not runner paths.
    env = os.environ.copy()
    env["LD_LIBRARY_PATH"] = str(payload / "vendor/lib")
    proof = []
    for binary in (payload / "vendor/ghostscript/bin/gs", payload / "vendor/tesseract/bin/tesseract"):
        text = subprocess.check_output(["ldd", str(binary)], text=True, env=env)
        if "not found" in text:
            raise RuntimeError(text)
        proof.append(str(binary) + "\n" + text)
        print(text)
    (DIST / "native-linkage.txt").write_text("\n".join(proof))


def main() -> None:
    for path in (BUILD, DIST, VENDOR, LICENSES):
        path.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        run("pwsh", "-NoProfile", "-File", "packaging/build-windows.ps1", "-RuntimeOnly")
    elif sys.platform == "darwin":
        macos_helpers()
    else:
        linux_helpers()
    common_data()
    payload = freeze()
    if sys.platform == "win32":
        compiler = Path(os.environ["ProgramFiles(x86)"]) / "Inno Setup 6/ISCC.exe"
        if not compiler.exists():
            run("choco", "install", "innosetup", "--yes", "--no-progress")
        run(compiler, "packaging/desktop-smoke.iss")
    elif sys.platform == "darwin":
        image_root = BUILD / "dmg"
        image_root.mkdir(exist_ok=True)
        shutil.copytree(DIST / (NAME + ".app"), image_root / (NAME + ".app"), symlinks=True)
        (image_root / "Applications").symlink_to("/Applications")
        shutil.copy2(ROOT / "packaging/DESKTOP-INSTALL.txt", image_root / "FIRST-LAUNCH.txt")
        run("hdiutil", "create", "-volname", NAME, "-srcfolder", image_root, "-ov", "-format", "UDZO", DIST / (STEM + ".dmg"))
    else:
        linux_installers(payload)
    sizes = {path.name: path.stat().st_size for path in DIST.iterdir() if path.is_file()}
    (DIST / "artifact-sizes.json").write_text(json.dumps(sizes, indent=2) + "\n")
    print(json.dumps(sizes, indent=2))


if __name__ == "__main__":
    main()
