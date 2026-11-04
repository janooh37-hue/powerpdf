"""Build the complete PowerPDF desktop and terminal distribution natively."""
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
import tomllib

ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / "build/desktop"
DIST = ROOT / "dist"
STAGE = ROOT / "build/desktop-vendor"
VENDOR = STAGE / "vendor"
LICENSES = STAGE / "licenses"
INPUTS = json.loads((ROOT / "packaging/desktop-inputs.json").read_text())
CONFIG = json.loads((ROOT / "packaging/desktop-config.json").read_text())
NAME = "PowerPDF"
VERSION = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
ARCH = "arm64" if platform.machine().lower() in ("arm64", "aarch64") else "x64"
OS = {"win32": "windows", "darwin": "macos", "linux": "linux"}[sys.platform]
STEM = f"powerpdf-{VERSION}-{OS}-{ARCH}"


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


def unix_helpers() -> None:
    # Pinned source-built helper closure, using only baseline OS libraries.
    prefix = BUILD / "native-prefix"
    zlib = source("zlib")
    run("./configure", "--static", f"--prefix={prefix}", cwd=zlib)
    run("make", "-j3", cwd=zlib)
    run("make", "install", cwd=zlib)
    png = source("libpng")
    cmake_build("png", png, prefix, "-DPNG_SHARED=OFF", "-DPNG_FRAMEWORK=OFF", "-DPNG_TESTS=OFF",
                f"-DZLIB_LIBRARY={prefix}/lib/libz.a", f"-DZLIB_INCLUDE_DIR={prefix}/include")
    jpeg = source("libjpeg")
    cmake_build("jpeg", jpeg, prefix, "-DENABLE_SHARED=OFF", "-DENABLE_STATIC=ON",
                "-DWITH_TURBOJPEG=OFF", "-DCMAKE_INSTALL_LIBDIR=lib")
    # Leptonica's built-in bitmap fonts are TIFF-compressed even when the app
    # sends PNG images. Leptonica compiles TIFF I/O only with JPEG support too.
    tiff = source("libtiff")
    cmake_build("tiff", tiff, prefix, "-Dtiff-tools=OFF", "-Dtiff-tests=OFF",
                "-Dtiff-contrib=OFF", "-Dtiff-docs=OFF", "-Dtiff-opengl=OFF", "-Dcxx=OFF",
                "-Djpeg=ON", "-Dold-jpeg=OFF", "-Djpeg12=OFF", "-Djbig=OFF", "-Dlerc=OFF",
                "-Dlzma=OFF", "-Dzstd=OFF", "-Dwebp=OFF", "-Dlibdeflate=OFF",
                f"-DJPEG_LIBRARY={prefix}/lib/libjpeg.a", f"-DJPEG_INCLUDE_DIR={prefix}/include",
                f"-DZLIB_LIBRARY={prefix}/lib/libz.a", f"-DZLIB_INCLUDE_DIR={prefix}/include")
    lept = source("leptonica")
    # libtiff 4.7's static config omits its CMath imported target. Use CMake's
    # stock FindTIFF module and the explicit static library instead.
    cmake_build("leptonica", lept, prefix, "-DBUILD_PROG=OFF", "-DENABLE_GIF=OFF", "-DENABLE_JPEG=ON",
                "-DENABLE_TIFF=ON", "-DENABLE_WEBP=OFF", "-DENABLE_OPENJPEG=OFF",
                f"-DCMAKE_IGNORE_PATH={prefix}/lib/cmake/tiff",
                f"-DJPEG_LIBRARY={prefix}/lib/libjpeg.a", f"-DJPEG_INCLUDE_DIR={prefix}/include",
                f"-DTIFF_LIBRARY={prefix}/lib/libtiff.a", f"-DTIFF_INCLUDE_DIR={prefix}/include",
                f"-DPNG_LIBRARY={prefix}/lib/libpng.a", f"-DPNG_PNG_INCLUDE_DIR={prefix}/include",
                f"-DZLIB_LIBRARY={prefix}/lib/libz.a", f"-DZLIB_INCLUDE_DIR={prefix}/include")
    tess = source("tesseract")
    cmake_build("tesseract", tess, prefix, "-DBUILD_TRAINING_TOOLS=OFF", "-DBUILD_TESTS=OFF",
                "-DDISABLE_ARCHIVE=ON", "-DDISABLE_CURL=ON", "-DOPENMP_BUILD=OFF",
                "-DDISABLE_TIFF=ON", "-DGRAPHICS_DISABLED=ON", "-DENABLE_NATIVE=OFF",
                f"-DLeptonica_DIR={prefix}/lib/cmake/leptonica",
                # Leptonica exports JPEG before TIFF. ELF static archives are
                # scanned left-to-right, so TIFF's JPEG12 symbols need JPEG last.
                *([f"-DCMAKE_CXX_STANDARD_LIBRARIES={prefix}/lib/libjpeg.a"] if sys.platform == "linux" else []))
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
        if sys.platform == "darwin":
            run("codesign", "--force", "-s", "-", target)
    for part in ("lib", "Resource", "iccprofiles"):
        shutil.copytree(gs / part, VENDOR / "ghostscript" / part, dirs_exist_ok=True)
    for name, src in (("Ghostscript", gs), ("Tesseract", tess), ("Leptonica", lept), ("libpng", png), ("libjpeg-turbo", jpeg), ("libtiff", tiff), ("zlib", zlib)):
        # Static codec libraries retain their own notices, not only GS's AGPL.
        for license_file in src.rglob("*"):
            relative = license_file.relative_to(src)
            is_license = license_file.name.lower().startswith(("license", "copying", "copyright", "notice"))
            is_license |= license_file.name.lower() in ("readme.ijg", "ftl.txt", "gplv2.txt")
            is_license |= relative.as_posix() == "jpeg/README"
            if license_file.is_file() and is_license:
                destination = LICENSES / name / license_file.relative_to(src)
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(license_file, destination)
    if sys.platform == "darwin":
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
    # Exact Ubuntu 22.04 debs provide the desktop/printing shared-library closure.
    # Ghostscript and Tesseract themselves come only from the pinned source build.
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
    for package in lock:
        copyright_file = extracted / "usr/share/doc" / package["package"] / "copyright"
        destination = LICENSES / "ubuntu" / package["package"]
        destination.mkdir(parents=True, exist_ok=True)
        shutil.copy2(copyright_file, destination / "copyright")
    shutil.copytree(extracted / "usr/share/common-licenses", LICENSES / "ubuntu/common-licenses", dirs_exist_ok=True)
    shutil.copy2(ROOT / "packaging/linux-runtime-lock.json", STAGE / "LINUX-PROVENANCE.json")


def common_data() -> None:
    for name in ("LICENSE", "NOTICE"):
        shutil.copy2(ROOT / name, STAGE / name)
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
    run("git", "-c", "core.autocrlf=false", "-c", "core.eol=lf", "archive", "--format=tar.gz", "--prefix=powerpdf-source/", "-o", DIST / "powerpdf-corresponding-source.tar.gz", "HEAD")


def freeze() -> Path:
    run(sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
        "--distpath", DIST, "--workpath", BUILD / "freeze", ROOT / "packaging/desktop.spec")
    if sys.platform == "darwin":
        bin_dir = DIST / (NAME + ".app") / "Contents/MacOS"
        payload = DIST / (NAME + ".app") / "Contents/Resources"
    else:
        bin_dir = DIST / NAME
        payload = bin_dir / "_internal"
    shutil.copytree(STAGE, payload, dirs_exist_ok=True)
    for command in ("split", "merge", "compress", "image") + (("print",) if sys.platform == "win32" else ()):
        if sys.platform == "win32":
            (bin_dir / f"pdf{command}.cmd").write_text(f'''@echo off
"%~dp0powerpdf.exe" --cli {command} %*
''')
        elif sys.platform == "darwin":
            (bin_dir / f"pdf{command}").symlink_to("powerpdf")
        else:
            path = bin_dir / f"pdf{command}"
            path.write_text(f'''#!/bin/sh
exec "$(dirname "$0")/powerpdf" --cli {command} "$@"
''')
            path.chmod(0o755)
    if sys.platform == "darwin":
        run("codesign", "--force", "--deep", "-s", "-", DIST / (NAME + ".app"))
        run("codesign", "--verify", "--deep", "--strict", DIST / (NAME + ".app"))
    # Runtime resolution also considers Contents/Resources for macOS helpers.
    return payload


def linux_installers(payload: Path) -> None:
    appdir = BUILD / "AppDir"
    shutil.copytree(DIST / NAME, appdir / "usr/lib/powerpdf", dirs_exist_ok=True)
    (appdir / "AppRun").write_text('#!/bin/sh\nHERE="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"\nexport LD_LIBRARY_PATH="$HERE/usr/lib/powerpdf/_internal/vendor/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"\ncase "${1:-}" in --cli|--version|--runtime-info) exec "$HERE/usr/lib/powerpdf/powerpdf" "$@";; esac\nexec "$HERE/usr/lib/powerpdf/PowerPDF" "$@"\n')
    (appdir / "AppRun").chmod(0o755)
    shutil.copy2(ROOT / "packaging/powerpdf.desktop", appdir)
    shutil.copy2(ROOT / "assets/powerpdf.png", appdir)
    tool = fetch(INPUTS["appimagetool"], BUILD / "appimagetool.AppImage")
    runtime = fetch(INPUTS["appimage_runtime"], BUILD / "appimage-runtime-x86_64")
    tool.chmod(0o755)
    env = os.environ.copy()
    env.update({"ARCH": "x86_64", "APPIMAGE_EXTRACT_AND_RUN": "1"})
    run(tool, "--no-appstream", "--runtime-file", runtime, appdir, DIST / (STEM + ".AppImage"), env=env)
    deb = BUILD / "deb"
    install = deb / "opt/powerpdf"
    shutil.copytree(appdir / "usr/lib/powerpdf", install, dirs_exist_ok=True)
    for src, dest in ((ROOT / "packaging/powerpdf.desktop", deb / "usr/share/applications/powerpdf.desktop"),
                      (ROOT / "assets/powerpdf.png", deb / "usr/share/icons/hicolor/256x256/apps/powerpdf.png")):
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
    launchers = deb / "usr/bin"
    launchers.mkdir(parents=True, exist_ok=True)
    for command in ("PowerPDF", "powerpdf", "pdfsplit", "pdfmerge", "pdfcompress", "pdfimage"):
        (launchers / command).symlink_to("/opt/powerpdf/" + command)
    (deb / "DEBIAN").mkdir(parents=True, exist_ok=True)
    (deb / "DEBIAN/control").write_text(f"Package: powerpdf\nVersion: {VERSION}\nArchitecture: amd64\nMaintainer: PowerPDF <noreply@github.com>\nDepends: libc6 (>= 2.35)\nSection: graphics\nPriority: optional\nDescription: Offline PDF desktop and terminal toolkit\n")
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
        run("pwsh", "-NoProfile", "-File", "packaging/fetch-windows-runtime.ps1")
    else:
        unix_helpers()
        if sys.platform == "linux":
            linux_helpers()
    common_data()
    payload = freeze()
    if sys.platform == "win32":
        compiler = Path(os.environ["ProgramFiles(x86)"]) / "Inno Setup 6/ISCC.exe"
        if not compiler.exists():
            run("choco", "install", "innosetup", "--yes", "--no-progress")
        run(compiler, f"/DAppVersion={VERSION}", "packaging/powerpdf.iss")
        portable = BUILD / "portable"
        shutil.copytree(DIST / NAME, portable / "bin", dirs_exist_ok=True)
        shutil.make_archive(str(DIST / STEM), "zip", portable)
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
