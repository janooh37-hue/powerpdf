"""External, stdlib-only artifact probe; never import the unfrozen application.

Used by install-artifacts.py, or directly with --gui PATH --cli PATH --output DIR.
AppImages may use the same path for both executables; --cli is the public frozen
console dispatch. --payload points at a separately extracted _internal directory
because AppImage extract-and-run paths disappear when the process exits.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tomllib

NAMES = ("رخصة المركبة.pdf", "شهادة 2026.pdf", "Ahmed عقد.pdf")


def fixtures(folder: Path) -> Path:
    """Write real, two-page PDFs with enough text for OCR orientation detection."""
    folder.mkdir(parents=True, exist_ok=True)
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>",
               b"<< /Type /Pages /Kids [3 0 R 4 0 R] /Count 2 >>",
               b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 5 0 R >> >> /Contents 6 0 R >>",
               b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 5 0 R >> >> /Contents 7 0 R >>",
               b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    for page in (1, 2):
        lines = [f"PowerPDF packaging page {page}: local PDF tools.",
                 "The quick brown fox jumps over the lazy dog.",
                 "Every document stays private on this computer."] * 9
        commands = "BT /F1 18 Tf 24 TL 45 745 Td\n" + "\nT*\n".join(f"({line}) Tj" for line in lines) + "\nET\n"
        content = commands.encode("ascii")
        objects.append(f"<< /Length {len(content)} >>\nstream\n".encode() + content + b"endstream")
    document = bytearray(b"%PDF-1.4\n% PowerPDF native artifact fixture\n")
    # Legal PDF comments make real Ghostscript compression measurably smaller.
    document.extend((b"% packaging fixture padding " + b"x" * 100 + b"\n") * 512)
    offsets = [0]
    for number, value in enumerate(objects, 1):
        offsets.append(len(document))
        document.extend(f"{number} 0 obj\n".encode() + value + b"\nendobj\n")
    xref = len(document)
    document.extend(f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        document.extend(f"{offset:010} 00000 n \n".encode())
    document.extend(f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    for name in NAMES:
        (folder / name).write_bytes(document)
    return folder / NAMES[1]


def clean_env(output: Path) -> dict[str, str]:
    env = os.environ.copy()
    for key in ("PYTHONPATH", "PYTHONHOME", "GS_LIB", "TESSDATA_PREFIX", "LD_LIBRARY_PATH",
                "DYLD_LIBRARY_PATH", "DYLD_FALLBACK_LIBRARY_PATH", "QT_PLUGIN_PATH",
                "QT_QPA_PLATFORM_PLUGIN_PATH", "QML2_IMPORT_PATH"):
        env.pop(key, None)
    home = output / "home"
    home.mkdir(parents=True, exist_ok=True)
    env.update({"QT_QPA_PLATFORM": "offscreen", "PYTHONIOENCODING": "utf-8", "HOME": str(home),
                "XDG_CONFIG_HOME": str(home / ".config"), "XDG_CACHE_HOME": str(home / ".cache"),
                "XDG_DATA_HOME": str(home / ".local/share"), "APPIMAGE_EXTRACT_AND_RUN": "1"})
    if sys.platform == "win32":
        env.update({"USERPROFILE": str(home), "APPDATA": str(home / "AppData/Roaming"),
                    "LOCALAPPDATA": str(home / "AppData/Local")})
    return env


def run(command: list[object], output: Path, env: dict[str, str], *, cwd: Path | None = None,
        expected_exit: int = 0) -> str:
    args = list(map(str, command))
    print("+", " ".join(args), flush=True)
    process = subprocess.run(args, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             encoding="utf-8", errors="replace", timeout=180)
    text = process.stdout + process.stderr
    with (output / "commands.log").open("a", encoding="utf-8") as log:
        log.write("+ " + " ".join(args) + "\n" + text + f"\nexit={process.returncode}\n")
    if process.returncode != expected_exit:
        raise RuntimeError(f"Artifact command exited {process.returncode}, expected {expected_exit}: {args!r}\n{text}")
    if "function not present" in text.lower():
        raise RuntimeError(f"Incomplete native codec closure: {text}")
    return text


def png(path: Path) -> None:
    header = path.read_bytes()[:24]
    if len(header) != 24 or header[:8] != b"\x89PNG\r\n\x1a\n" or min(struct.unpack(">II", header[16:24])) < 100:
        raise RuntimeError(f"Missing or invalid PNG evidence: {path}")


def helper_env(executable: Path, base: dict[str, str]) -> dict[str, str]:
    vendor = next((parent for parent in executable.parents if parent.name == "vendor"), None)
    if vendor is None or not executable.is_file():
        raise RuntimeError(f"Not a bundled helper: {executable}")
    home = vendor / executable.relative_to(vendor).parts[0]
    env = base.copy()
    env["TESSDATA_PREFIX"] = str(vendor / "tesseract/tessdata")
    env["GS_LIB"] = os.pathsep.join(str(vendor / "ghostscript" / part)
                                      for part in ("lib", "Resource/Init", "Resource/Font"))
    if sys.platform == "win32":
        env["PATH"] = str(executable.parent) + os.pathsep + env.get("PATH", "")
    else:
        key = "DYLD_LIBRARY_PATH" if sys.platform == "darwin" else "LD_LIBRARY_PATH"
        env[key] = os.pathsep.join((str(vendor / "lib"), str(home / "lib")))
    return env


def pe_imports(binary: Path) -> list[str]:
    """Read PE import names without requiring Visual Studio on an installed PC."""
    data = binary.read_bytes()
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    if data[pe:pe + 4] != b"PE\0\0":
        raise RuntimeError(f"Not a PE binary: {binary}")
    count, optional_size = struct.unpack_from("<H12xH", data, pe + 6)
    optional = pe + 24
    magic = struct.unpack_from("<H", data, optional)[0]
    directories = optional + (112 if magic == 0x20B else 96)
    imports = struct.unpack_from("<I", data, directories + 8)[0]
    sections = []
    for index in range(count):
        start = optional + optional_size + index * 40
        virtual_size, address, raw_size, raw = struct.unpack_from("<IIII", data, start + 8)
        sections.append((address, max(virtual_size, raw_size), raw))

    def offset(rva: int) -> int:
        for address, size, raw in sections:
            if address <= rva < address + size:
                return raw + rva - address
        raise RuntimeError(f"Invalid PE import address in {binary}: {rva}")

    names = []
    if imports:
        cursor = offset(imports)
        while any(data[cursor:cursor + 20]):
            name = struct.unpack_from("<I", data, cursor + 12)[0]
            start = offset(name)
            names.append(data[start:data.index(b"\0", start)].decode("ascii"))
            cursor += 20
    return names


def linkage(payload: Path, helpers: list[Path], output: Path, env: dict[str, str]) -> None:
    binaries = set(helpers)
    groups = (("*Qt6PrintSupport*", "QtPrintSupport*"), ("*qoffscreen*",),
              ("*libpdfium*", "pdfium.dll"))
    for patterns in groups:
        found = set()
        for pattern in patterns:
            found.update(path for path in payload.rglob(pattern) if path.is_file()
                         and (path.suffix.lower() in (".so", ".dylib", ".dll", ".pyd")
                              or ".so." in path.name or path.name == "QtPrintSupport"))
        if not found:
            raise RuntimeError(f"Missing installed native library: {patterns}")
        binaries.update(found)
    proof = []
    for binary in sorted(binaries):
        if sys.platform == "win32":
            text = "PE imports: " + ", ".join(pe_imports(binary))
        else:
            command = ["otool", "-L", binary] if sys.platform == "darwin" else ["ldd", binary]
            probe_env = env
            if sys.platform == "linux" and binary not in helpers:
                # Mirror the frozen bootloader's GUI search paths; helper_env
                # intentionally excludes them to isolate native helper processes.
                probe_env = env | {"LD_LIBRARY_PATH": os.pathsep.join(map(str, (
                    payload, payload / "PySide6/Qt/lib", payload / "vendor/lib")))}
            text = run(command, output, probe_env)
            if "not found" in text:
                raise RuntimeError(f"Unresolved installed native linkage: {binary}\n{text}")
        proof.append(str(binary) + "\n" + text)
    (output / "native-linkage.txt").write_text("\n".join(proof) + "\n", encoding="utf-8")


def smoke(gui: Path, cli: Path, output: Path, payload: Path | None = None) -> None:
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    env = clean_env(output)
    folder = output / "ملفات عربية"
    source = fixtures(folder)
    originals = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in folder.glob("*.pdf")}
    run([gui, "--packaging-smoke-output", output, folder, source], output, env, cwd=output)
    png(output / "powerpdf-gui.png")
    runtime = json.loads((output / "runtime.json").read_text(encoding="utf-8"))
    if (runtime["frozen"] is not True or runtime["window_class"] != "MainWindow"
            or Path(runtime["folder"]).resolve() != folder.resolve()
            or Path(runtime["preview_source"]).resolve() != source.resolve()
            or runtime["preview_pages"] != 2 or not set(NAMES).issubset(runtime["filenames"])
            or not {"Noto Sans", "Noto Sans Arabic"}.issubset(runtime["fonts"])):
        raise RuntimeError(f"GUI evidence does not describe the real Arabic fixture workspace: {runtime}")
    version = run([cli, "--version"], output, env)
    expected_version = tomllib.loads((Path(__file__).resolve().parents[2] / "pyproject.toml").read_text())["project"]["version"]
    if version.strip() != f"powerpdf {expected_version}":
        raise RuntimeError(f"Wrong frozen console version: {version}")
    resolved = json.loads(run([cli, "--runtime-info"], output, env))
    for name in ("ghostscript", "tesseract"):
        if not resolved.get(name) or "vendor" not in Path(resolved[name]).parts:
            raise RuntimeError(f"Frozen console resolved a host {name}: {resolved}")
    shim_dir = payload.parent if payload is not None else cli.parent
    for command in ("split", "merge", "compress", "image"):
        suffix = ".cmd" if sys.platform == "win32" else ""
        run([shim_dir / f"pdf{command}{suffix}", "--help"], output, env)
    for command, arguments in (("compress", [source, "-q", "ebook"]),
                               ("image", [source, "-f", "png", "-r", "150"]),
                               ("split", [source])):
        run([cli, "--cli", command, *arguments], output, env)
    compressed = source.with_name(source.stem + "_small.pdf")
    if not compressed.read_bytes().startswith(b"%PDF-") or compressed.stat().st_size >= source.stat().st_size:
        raise RuntimeError("Bundled Ghostscript did not produce a smaller valid PDF")
    pages = sorted(source.with_name(source.stem + "_pages").glob("*.pdf"))
    if len(pages) != 2:
        raise RuntimeError(f"Split did not create two PDF pages: {pages}")
    merged = folder / "مدمج.pdf"
    run([cli, "--cli", "merge", *pages, "-o", merged], output, env)
    # Rendering both transformed documents proves their PDFium readability and counts.
    for document in (compressed, merged):
        run([cli, "--cli", "image", document, "-f", "png", "-r", "100"], output, env)
    for document in (source, compressed, merged):
        images = sorted(document.with_name(document.stem + "_images").glob("*.png"))
        if len(images) != 2:
            raise RuntimeError(f"Image conversion did not produce two pages: {document}")
        for image in images:
            png(image)
    helpers = {}
    for name in ("ghostscript", "tesseract"):
        value = runtime["helpers"].get(name)
        if not value or "vendor" not in Path(value).parts:
            raise RuntimeError(f"GUI resolved host helper: {name}={value}")
        helper = Path(value)
        if payload is not None:
            parts = helper.parts
            helper = payload / Path(*parts[parts.index("vendor"):])
        helpers[name] = helper
    payload = payload or Path(runtime["bundle"])
    gs, tess = helpers["ghostscript"], helpers["tesseract"]
    gs_env, tess_env = helper_env(gs, env), helper_env(tess, env)
    engines = {name: run([binary, "--version"], output, helper_env(binary, env))
               for name, binary in helpers.items()}
    languages = run([tess, "--list-langs"], output, tess_env)
    if not {"eng", "ara", "osd"}.issubset(languages.splitlines()):
        raise RuntimeError(f"Missing bundled OCR languages: {languages}")
    ocr_image = output / "ocr-page.png"
    run([gs, "-q", "-dSAFER", "-dBATCH", "-dNOPAUSE", "-dFirstPage=1", "-dLastPage=1",
         "-sDEVICE=png16m", "-r180", f"-sOutputFile={ocr_image}", source], output, gs_env)
    png(ocr_image)
    for language, mode in (("eng", "6"), ("ara", "6"), ("osd", "0")):
        text = run([tess, ocr_image, "stdout", "-l", language, "--psm", mode], output, tess_env)
        if language == "eng" and "PowerPDF" not in text:
            raise RuntimeError(f"Bundled English OCR did not recognize the fixture: {text}")
        if language == "osd" and "Orientation in degrees:" not in text:
            raise RuntimeError(f"Bundled OSD did not analyze the fixture: {text}")
        (output / f"ocr-{language}.txt").write_text(text, encoding="utf-8")
    if sys.platform == "win32":
        fzf = Path(runtime["helpers"].get("fzf") or "")
        run([fzf, "--version"], output, helper_env(fzf, env))
        # Help and a no-match fzf filter exercise legacy PowerShell without printing.
        run([shim_dir / "pdfprint.cmd", "-Help"], output, env)
        picker_env = env.copy()
        picker_env["FZF_DEFAULT_OPTS"] = "--filter=powerpdf-smoke-no-match-7f184c"
        run([cli, "--cli", "print", folder], output, picker_env, expected_exit=1)
    linkage(payload, list(helpers.values()), output, gs_env)
    for name, digest in originals.items():
        if hashlib.sha256((folder / name).read_bytes()).hexdigest() != digest:
            raise RuntimeError(f"Artifact operation modified an original: {name}")
    (output / "product-smoke.json").write_text(json.dumps({"status": "passed", "console_version": version.strip(),
        "engines": engines, "languages": languages.splitlines(), "original_sha256": originals},
        ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gui", type=Path, required=True)
    parser.add_argument("--cli", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--payload", type=Path)
    args = parser.parse_args()
    smoke(args.gui.resolve(), args.cli.resolve(), args.output, args.payload)


if __name__ == "__main__":
    main()
