"""Real Windows desktop recording and input; never renders or drives an emulated app.

The session subcommand runs the unmodified powerpdf module on a real terminal.
The record subcommand sends OS keyboard input and captures gdigrab desktop video.
"""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import runpy
import shutil
import shlex
import struct
import subprocess
import sys
import time
import traceback
import urllib.request
import zipfile
import io


def save_json(path: Path, data: object) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def api():
    user = ctypes.WinDLL("user32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    user.SetProcessDPIAware()
    user.GetForegroundWindow.restype = wintypes.HWND
    user.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    kernel.SetConsoleTitleW.argtypes = [wintypes.LPCWSTR]
    user.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user.IsWindowVisible.argtypes = [wintypes.HWND]
    user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user.SetForegroundWindow.argtypes = [wintypes.HWND]
    user.MoveWindow.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.BOOL]
    user.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    kernel.GetConsoleWindow.restype = wintypes.HWND
    kernel.GetStdHandle.argtypes = [wintypes.DWORD]
    kernel.GetStdHandle.restype = wintypes.HANDLE
    kernel.GetConsoleMode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.SetConsoleMode.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    return user, kernel


def title(user, hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(1024)
    user.GetWindowTextW(hwnd, buf, len(buf))
    return buf.value


def windows(user) -> list[dict]:
    result = []
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    @callback_type
    def callback(hwnd, _):
        if user.IsWindowVisible(hwnd):
            pid = wintypes.DWORD()
            user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            result.append({"hwnd": int(hwnd), "pid": pid.value, "title": title(user, hwnd)})
        return True

    user.EnumWindows(callback, 0)
    return result


def screen_size(user) -> tuple[int, int]:
    return user.GetSystemMetrics(0), user.GetSystemMetrics(1)


def configure_display(user) -> dict:
    """Ask the existing display driver for a real 1080p desktop, not a scaled video."""
    attempts = []
    for width, height in [(1920, 1080), (1600, 900)]:
        mode = ctypes.create_string_buffer(220)  # Windows DEVMODEW, including its union.
        struct.pack_into("H", mode, 68, 220)
        if not user.EnumDisplaySettingsW(None, -1, mode):
            attempts.append({"error": "EnumDisplaySettingsW failed", "winerror": ctypes.get_last_error()})
            break
        fields = struct.unpack_from("I", mode, 72)[0]
        struct.pack_into("I", mode, 72, fields | 0x80000 | 0x100000)
        struct.pack_into("II", mode, 172, width, height)
        result = user.ChangeDisplaySettingsW(mode, 0)
        time.sleep(1)
        attempts.append({"requested": [width, height], "result": result, "actual": screen_size(user)})
        if screen_size(user)[0] >= 1600 and screen_size(user)[1] >= 900:
            break
    return {"attempts": attempts, "actual": screen_size(user)}


def console_font(kernel, face: str) -> None:
    class Coord(ctypes.Structure):
        _fields_ = [("X", ctypes.c_short), ("Y", ctypes.c_short)]

    class Font(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.ULONG), ("nFont", wintypes.DWORD), ("dwFontSize", Coord),
                    ("FontFamily", wintypes.UINT), ("FontWeight", wintypes.UINT), ("FaceName", wintypes.WCHAR * 32)]

    font = Font()
    font.cbSize = ctypes.sizeof(font)
    font.dwFontSize = Coord(0, 24)
    font.FontFamily = 54
    font.FontWeight = 400
    font.FaceName = face
    kernel.SetCurrentConsoleFontEx.argtypes = [wintypes.HANDLE, wintypes.BOOL, ctypes.POINTER(Font)]
    kernel.SetCurrentConsoleFontEx(kernel.GetStdHandle(-11), False, ctypes.byref(font))
    kernel.SetConsoleOutputCP(65001)
    kernel.SetConsoleCP(65001)


def session(args) -> int:
    user, kernel = api()
    folder = Path(args.folder)
    state_path = Path(args.state)
    state = {"pid": os.getpid(), "scenario": args.scenario, "python": sys.version,
             "stdin_tty": sys.stdin.isatty(), "stdout_tty": sys.stdout.isatty(),
             "console_hwnd": int(kernel.GetConsoleWindow() or 0), "started_unix": time.time(),
             "POWERPDF_BIDI": os.environ.get("POWERPDF_BIDI"), "MSYS": os.environ.get("MSYS"),
             "TERM": os.environ.get("TERM"), "folder": str(folder)}
    try:
        state["terminal_size"] = list(os.get_terminal_size())
    except OSError as error:
        state["terminal_size_error"] = str(error)
    save_json(state_path, state)
    if not state["stdin_tty"] or not state["stdout_tty"]:
        state.update(exit_code=86, error="Native Python did not receive a real terminal")
        save_json(state_path, state)
        return 86
    # Enable VT before textual_image probes; conhost otherwise prints the probes literally.
    output_mode = wintypes.DWORD()
    output_handle = kernel.GetStdHandle(-11)
    if kernel.GetConsoleMode(output_handle, ctypes.byref(output_mode)):
        kernel.SetConsoleMode(output_handle, output_mode.value | 0x0007)
    if args.scenario == "c":
        console_font(kernel, args.font)
        kernel.SetConsoleTitleW(f"PowerPDF C - plain conhost - POWERPDF_BIDI=on")
    os.chdir(args.repo)
    sys.path.insert(0, args.repo)
    sys.argv = ["powerpdf", str(folder)]
    code = 0
    # Forward every Python terminal write unchanged while retaining its ANSI transcript.
    # Native console API writes may bypass Python streams; screenshots/video remain the visual evidence.
    transcript = state_path.with_name(f"scenario-{args.scenario}-terminal.log")
    stream_log = transcript.open("w", encoding="utf-8", errors="backslashreplace")
    originals = (sys.stdout, sys.stderr, sys.__stdout__, sys.__stderr__)

    class TerminalLog:
        def __init__(self, terminal):
            self.terminal = terminal

        def __getattr__(self, name):
            return getattr(self.terminal, name)

        def write(self, text):
            written = self.terminal.write(text)
            stream_log.write(text)
            stream_log.flush()
            return written

        def flush(self):
            self.terminal.flush()
            stream_log.flush()

    sys.stdout = sys.__stdout__ = TerminalLog(originals[0])
    sys.stderr = sys.__stderr__ = TerminalLog(originals[1])
    state["terminal_transcript"] = str(transcript)
    try:
        runpy.run_module("powerpdf", run_name="__main__")
    except SystemExit as error:
        code = error.code if isinstance(error.code, int) else (1 if error.code else 0)
    except BaseException:
        code = 1
        state["exception"] = traceback.format_exc()
        Path(args.log).write_text(state["exception"], encoding="utf-8")
        traceback.print_exc()
    finally:
        sys.stdout, sys.stderr, sys.__stdout__, sys.__stderr__ = originals
        stream_log.close()
        state.update(exit_code=code, ended_unix=time.time())
        save_json(state_path, state)
    return code


class Desktop:
    def __init__(self, output: Path):
        self.user, self.kernel = api()
        self.output = output
        self.events = []
        self.started = time.perf_counter()
        self.scenario_start = self.started
        self.scenario = None
        self.hwnd = None

        class Mouse(ctypes.Structure):
            _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                        ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]

        class Keyboard(ctypes.Structure):
            _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                        ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]

        class Union(ctypes.Union):
            _fields_ = [("mi", Mouse), ("ki", Keyboard)]

        class Input(ctypes.Structure):
            _anonymous_ = ("data",)
            _fields_ = [("type", wintypes.DWORD), ("data", Union)]

        self.Input, self.Keyboard = Input, Keyboard
        self.user.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(Input), ctypes.c_int]

    def event(self, stage: str, **extra) -> None:
        now = time.perf_counter()
        self.events.append({"scenario": self.scenario, "stage": stage, "elapsed": now - self.started,
                            "video_seconds": now - self.scenario_start,
                            "unix": time.time(), "foreground_title": title(self.user, self.user.GetForegroundWindow()),
                            **extra})
        save_json(self.output / "timeline.json", self.events)

    def screenshot(self, name: str) -> None:
        try:
            from PIL import ImageGrab
            ImageGrab.grab(all_screens=True).save(self.output / f"{name}.png")
        except Exception:
            (self.output / f"{name}-screenshot-error.txt").write_text(traceback.format_exc(), encoding="utf-8")

    def focus(self) -> None:
        if not self.hwnd:
            raise RuntimeError("No terminal window selected for input")
        self.user.ShowWindow(self.hwnd, 9)
        # ALT unlocks Windows' foreground-switch restriction for automation on WinSta0.
        self.key(0x12)
        self.user.SetForegroundWindow(self.hwnd)
        time.sleep(0.12)
        if self.user.GetForegroundWindow() != self.hwnd:
            raise RuntimeError(f"Could not focus the terminal hwnd={self.hwnd}; refusing to type elsewhere")

    def input(self, vk=0, scan=0, flags=0) -> None:
        item = self.Input()
        item.type = 1
        item.ki = self.Keyboard(vk, scan, flags, 0, 0)
        if self.user.SendInput(1, ctypes.byref(item), ctypes.sizeof(item)) != 1:
            raise ctypes.WinError(ctypes.get_last_error())

    def key(self, vk: int, ctrl=False) -> None:
        if ctrl:
            self.input(0x11)
        self.input(vk)
        self.input(vk, flags=2)
        if ctrl:
            self.input(0x11, flags=2)

    def text(self, text: str, delay=0.012) -> None:
        # KEYEVENTF_UNICODE emits real desktop Unicode key events, not clipboard or app APIs.
        units = struct.unpack(f"<{len(text.encode('utf-16-le')) // 2}H", text.encode("utf-16-le"))
        for unit in units:
            self.input(scan=unit, flags=4)
            self.input(scan=unit, flags=6)
            time.sleep(delay)

    def at(self, seconds: float) -> None:
        time.sleep(max(0, self.scenario_start + seconds - time.perf_counter()))


def make_fixtures(output: Path) -> dict:
    from PIL import Image, ImageDraw, ImageFont
    from pypdf import PdfReader

    root = output / "fixtures"
    folder = root / "مستندات السيارات"
    folder.mkdir(parents=True, exist_ok=True)
    try:
        font = ImageFont.truetype("C:/Windows/Fonts/arial.ttf", 42)
    except OSError:
        font = ImageFont.load_default(size=42)
    names = ["رخصة المركبة.pdf", "شهادة 2026.pdf", "Ahmed عقد.pdf",
             "تقرير الترخيص 2026 (2).pdf", "لا لالا.pdf"]
    names += [f"{i:02d} مستند عربي للمراجعة.pdf" for i in range(1, 23)]
    for index, name in enumerate(names, 1):
        image = Image.new("RGB", (900, 1200), "#f3f0e9")
        draw = ImageDraw.Draw(image)
        draw.rectangle((50, 50, 850, 1150), outline="#215d66", width=6)
        draw.text((90, 110), "PowerPDF / PDF document", font=font, fill="#15383f")
        draw.text((90, 210), f"Document {index:02d} - Arabic filename", font=font, fill="#15383f")
        draw.text((90, 330), name.removesuffix(".pdf"), font=font, fill="#15383f")
        for y in range(460, 1000, 70):
            draw.line((95, y, 790, y), fill="#9bb4b7", width=3)
        image.save(root / name, "PDF", resolution=110)
        # Confirm fixtures are actual readable PDFs, not renamed image files.
        if len(PdfReader(root / name).pages) != 1:
            raise RuntimeError(f"Invalid fixture: {name}")
    shutil.copyfile(root / names[0], folder / names[0])
    return {"root": str(root), "folder": str(folder), "pdfs": [str(root / name) for name in names],
            "description": "27 genuine one-page PDFs created by Pillow; requested samples plus Arabic cross-pane rows; nested PDF; verified with pypdf"}


def locate_git() -> dict:
    candidates = [Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Git",
                  Path("C:/Program Files (x86)/Git")]
    git = shutil.which("git")
    if git:
        candidates.extend([Path(git).resolve().parent.parent, Path(git).resolve().parent])
    for root in candidates:
        mintty = root / "usr/bin/mintty.exe"
        bash = root / "bin/bash.exe"
        if mintty.is_file() and bash.is_file():
            return {"root": str(root), "mintty": str(mintty), "bash": str(bash),
                    "winpty": str(root / "usr/bin/winpty.exe")}
    raise RuntimeError("Git for Windows mintty.exe and bash.exe were not found")


def install_font(output: Path) -> dict:
    result = {"face": "Consolas", "rtl_fallback": "Courier New"}
    try:
        url = "https://github.com/dejavu-fonts/dejavu-fonts/releases/download/version_2_37/dejavu-fonts-ttf-2.37.zip"
        with urllib.request.urlopen(url, timeout=45) as response:
            data = response.read()
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            member = next(name for name in archive.namelist() if name.endswith("/DejaVuSansMono.ttf"))
            path = output / "DejaVuSansMono.ttf"
            path.write_bytes(archive.read(member))
        gdi = ctypes.WinDLL("gdi32", use_last_error=True)
        gdi.AddFontResourceExW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p]
        count = gdi.AddFontResourceExW(str(path), 0, None)
        if not count:
            raise ctypes.WinError(ctypes.get_last_error())
        # Fonts registered without FR_PRIVATE are visible to mintty and conhost too.
        ctypes.WinDLL("user32").SendNotifyMessageW(0xFFFF, 0x001D, 0, 0)
        result.update(face="DejaVu Sans Mono", path=str(path), registered_fonts=count, source=url)
    except Exception:
        result["fallback_reason"] = traceback.format_exc()
    return result


def posix_path(path: str | Path) -> str:
    text = str(path).replace("\\", "/")
    return f"/{text[0].lower()}{text[2:]}" if len(text) > 1 and text[1] == ":" else text


def command_version(command: list[str]) -> dict:
    try:
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20)
        return {"command": command, "returncode": result.returncode, "stdout": result.stdout, "stderr": result.stderr}
    except Exception:
        return {"command": command, "error": traceback.format_exc()}


def launch(args, desktop: Desktop, git: dict, font: dict, fixtures: dict, letter: str, strategy="pcon"):
    out, repo = Path(args.output), Path(args.repo)
    state = out / f"scenario-{letter}-session.json"
    state.unlink(missing_ok=True)
    environment = os.environ.copy()
    for key in ("NO_COLOR", "HERDR_ENV", "HERDR_PANE_ID", "HERDR_SOCKET_PATH", "FORCE_COLOR", "TEXTUAL_DEVTOOLS"):
        environment.pop(key, None)
    environment.update(POWERPDF_BIDI="off" if letter == "a" else "on", PYTHONUTF8="1", PYTHONIOENCODING="utf-8",
                       MSYS="enable_pcon", LANG="C.UTF-8", LC_ALL="C.UTF-8", TERM="xterm-256color", COLORTERM="truecolor")
    appdata = out / f"settings-{letter}"
    settings = appdata / "powerpdf/settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    save_json(settings, {"recent": fixtures["pdfs"][:3], "theme": "powerpdf"})
    environment["APPDATA"] = str(appdata)
    child = [str(repo / ".venv/Scripts/python.exe"), str(Path(__file__).resolve()), "session", "--repo", str(repo),
             "--folder", fixtures["root"], "--scenario", letter, "--state", str(state), "--font", font["face"],
             "--log", str(out / f"scenario-{letter}-app-error.log")]
    if letter == "c":
        command = [str(Path(os.environ["SystemRoot"]) / "System32/conhost.exe"), *child]
    else:
        bash_child = [posix_path(child[0]), posix_path(child[1]), *child[2:]]
        if strategy == "winpty":
            environment["MSYS"] = "disable_pcon"
            bash_child.insert(0, posix_path(git["winpty"]))
        command = [git["mintty"], "-d", "-h", "always", "-s", "145,40", "-p", "0,0", "-T",
                   f"PowerPDF {letter.upper()} - mintty native Bidi=2 - POWERPDF_BIDI={environment['POWERPDF_BIDI']}",
                   "-o", "Bidi=2", "-o", f"ConPTY={'off' if strategy == 'winpty' else 'on'}", "-o", f"Font={font['face']}",
                   "-o", "FontHeight=18", "-o", "FontRTL=Courier New", "-o", "FontRender=uniscribe",
                   "-o", "Locale=C.UTF-8", "-e", git["bash"], "--noprofile", "--norc", "-c", "exec " + shlex.join(bash_child)]
    save_json(out / f"scenario-{letter}-launch-{strategy}.json", {"command": command, "strategy": strategy,
              "environment": {key: environment.get(key) for key in ("APPDATA", "POWERPDF_BIDI", "MSYS", "TERM", "LANG")},
              "recent_seeded": True, "recent_note": "Logical recent settings seeded with three existing fixture PDFs; no default PDF viewer opened."})
    process = subprocess.Popen(command, env=environment, cwd=repo)
    deadline = time.monotonic() + 30
    session_state = None
    while time.monotonic() < deadline:
        if state.exists():
            try:
                session_state = json.loads(state.read_text(encoding="utf-8"))
                break
            except json.JSONDecodeError:
                pass
        time.sleep(0.2)
    if session_state is None:
        desktop.screenshot(f"scenario-{letter}-launch-failure")
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True)
        raise RuntimeError(f"Scenario {letter}: session did not start; command and desktop evidence saved")
    if session_state.get("exit_code") == 86:
        desktop.event("non-tty-launch", strategy=strategy, state=session_state)
        desktop.screenshot(f"scenario-{letter}-{strategy}-non-tty")
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True)
        if letter != "c" and strategy == "pcon" and Path(git["winpty"]).exists():
            save_json(out / f"scenario-{letter}-pcon-probe.json", session_state)
            return launch(args, desktop, git, font, fixtures, letter, "winpty")
        raise RuntimeError(f"Scenario {letter}: no real terminal even with {strategy}")
    deadline = time.monotonic() + 15
    hwnd = None
    while time.monotonic() < deadline:
        matches = [w for w in windows(desktop.user) if w["pid"] == process.pid or f"PowerPDF {letter.upper()} -" in w["title"]]
        if letter == "c":
            visible = [w for w in windows(desktop.user) if w["hwnd"] == session_state["console_hwnd"]]
            matches = visible or matches
        if matches:
            hwnd = matches[0]["hwnd"]
            break
        time.sleep(0.2)
    if not hwnd:
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True)
        raise RuntimeError(f"Scenario {letter}: session has no visible desktop window")
    desktop.hwnd = hwnd
    width, height = screen_size(desktop.user)
    desktop.user.MoveWindow(hwnd, 0, 0, width, height - 42, True)
    desktop.user.ShowWindow(hwnd, 3)
    desktop.focus()
    desktop.user.SetCursorPos(width - 10, height - 10)
    # Never send text into import-time terminal probes; wait for the real UI to draw.
    transcript = out / f"scenario-{letter}-terminal.log"
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        content = transcript.read_text(encoding="utf-8", errors="replace") if transcript.exists() else ""
        if "Activity" in content and "Queue" in content:
            break
        if state.exists() and "exit_code" in json.loads(state.read_text(encoding="utf-8")):
            raise RuntimeError(f"Scenario {letter}: app exited before rendering; inspect session/error log")
        time.sleep(0.25)
    else:
        desktop.screenshot(f"scenario-{letter}-startup-timeout")
        raise RuntimeError(f"Scenario {letter}: no rendered app UI after 45 seconds")
    time.sleep(2)  # Let the real timed splash close before desktop input.
    desktop.event("terminal-launched", process_pid=process.pid, session_pid=session_state["pid"], hwnd=hwnd,
                  strategy=strategy, windows=windows(desktop.user), tty=session_state)
    return process, state


def ffmpeg_filter(letter: str, width: int, height: int, font_path: str) -> str:
    label = {"a": "A | MINTTY native Bidi=2 | POWERPDF_BIDI=off",
             "b": "B | MINTTY native Bidi=2 | POWERPDF_BIDI=on",
             "c": "C | PLAIN CONHOST | POWERPDF_BIDI=on"}[letter]
    # Native captured pixels are untouched: only an 84px caption strip is appended above them.
    escaped_font = font_path.replace("\\", "/").replace(":", "\\:").replace("'", "\\'")
    base = f"fontfile='{escaped_font}':fontcolor=white:fontsize=27:x=24"
    result = f"pad=iw:ih+84:0:84:color=0x111820,drawtext={base}:y=10:text='{label}'"
    phases = [(0, 3, "REAL WINDOWS DESKTOP CAPTURE - same fixtures and keys in all scenarios"),
              (3, 9, "Navigate real Arabic folders and PDF names with desktop arrow keys"),
              (9, 19, "Queue 3 real PDFs - Arabic in tree and queue across the same rows"),
              (19, 23, "Recent tab - seeded logical paths to existing PDFs; no PDF viewer launched"),
              (23, 32, "F2 rename - type Arabic via SendInput; Esc cancels; q exits")]
    for start, end, caption in phases:
        result += f",drawtext=fontfile='{escaped_font}':fontcolor=0xc5d9dd:fontsize=20:x=24:y=48:text='{caption}':enable='between(t,{start},{end})'"
    return result


def record_scenario(args, desktop: Desktop, git: dict, font: dict, fixtures: dict, letter: str) -> dict:
    out = Path(args.output)
    desktop.scenario = letter
    process = None
    recorder = None
    log_handle = None
    result = {"scenario": letter, "success": False}
    try:
        process, state = launch(args, desktop, git, font, fixtures, letter)
        width, height = screen_size(desktop.user)
        # Width/height must be even for yuv420p. Real desktop is still captured at native resolution.
        if width % 2 or height % 2:
            raise RuntimeError(f"Desktop dimensions must be even, got {width}x{height}")
        caption_font = str(out / "DejaVuSansMono.ttf") if (out / "DejaVuSansMono.ttf").exists() else "C:/Windows/Fonts/consola.ttf"
        video = out / f"scenario-{letter}.mp4"
        command = ["ffmpeg", "-hide_banner", "-y", "-f", "gdigrab", "-framerate", "20", "-draw_mouse", "0",
                   "-offset_x", "0", "-offset_y", "0", "-video_size", f"{width}x{height}", "-i", "desktop", "-t", "32",
                   "-vf", ffmpeg_filter(letter, width, height, caption_font), "-an", "-c:v", "libx264", "-preset", "ultrafast",
                   "-crf", "18", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(video)]
        save_json(out / f"scenario-{letter}-ffmpeg-command.json", command)
        log_handle = (out / f"scenario-{letter}-ffmpeg.log").open("w", encoding="utf-8")
        desktop.scenario_start = time.perf_counter()
        recorder = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log_handle, stderr=subprocess.STDOUT)
        desktop.event("title", video=str(video), capture_dimensions=[width, height], output_dimensions=[width, height + 84])
        desktop.screenshot(f"scenario-{letter}-title")
        desktop.at(3)
        desktop.focus()
        desktop.event("main", action="Home / Right / Down / Down; real tree navigation")
        desktop.key(0x24)  # Home: root.
        desktop.key(0x27)  # Right: expand root if needed.
        desktop.key(0x28)  # Arabic child folder.
        desktop.at(5)
        desktop.key(0x28)  # First PDF after directory.
        desktop.at(7)
        desktop.focus()
        desktop.at(9)
        desktop.focus()
        for index in range(3):
            desktop.key(0x20)
            if index < 2:
                desktop.key(0x28)
            time.sleep(0.65)
        desktop.event("queue", action="Space, Down, Space, Down, Space", expected_count=3,
                      evidence="OS input was sent; inspect screenshot/video for resulting queue, not an app-state assertion")
        desktop.at(13)
        desktop.screenshot(f"scenario-{letter}-queue")
        # 22 real Arabic names keep the tree populated at the Queue panel's vertical position.
        desktop.key(0x28)
        desktop.key(0x26)
        desktop.focus()
        desktop.at(19)
        desktop.key(ord("R"))
        desktop.event("recent", note="Three existing logical PDF paths seeded in isolated APPDATA settings")
        desktop.at(21)
        desktop.screenshot(f"scenario-{letter}-recent")
        desktop.at(23)
        desktop.focus()
        desktop.key(ord("R"))
        time.sleep(0.3)
        desktop.key(0x71)  # F2
        desktop.event("rename", action="F2 on highlighted PDF")
        time.sleep(0.65)
        desktop.key(0x41, ctrl=True)
        typed = "مستند عربي جديد ٢٠٢٦.pdf"
        desktop.text(typed, delay=0.07)
        desktop.event("Arabic-typed", text=typed, input_method="Win32 SendInput KEYEVENTF_UNICODE")
        desktop.at(28.5)
        desktop.screenshot(f"scenario-{letter}-arabic-typed")
        desktop.at(30)
        desktop.key(0x1B)
        desktop.event("rename-cancelled", action="Escape cancels rename; app quit binding is q, not Escape")
        time.sleep(0.4)
        desktop.focus()
        desktop.key(ord("Q"))
        desktop.event("quit", action="q")
        recorder.wait(timeout=35)
        result["ffmpeg_exit_code"] = recorder.returncode
        desktop.screenshot(f"scenario-{letter}-exit")
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            session_state = json.loads(state.read_text(encoding="utf-8"))
            if "exit_code" in session_state:
                break
            time.sleep(0.2)
        result["session"] = session_state
        result["probe"] = command_version(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(video)])
        result["success"] = recorder.returncode == 0 and session_state.get("exit_code") == 0
        if not result["success"]:
            result["error"] = "Capture failed or the actual app did not exit cleanly after the delivered input"
    except Exception:
        result["error"] = traceback.format_exc()
        desktop.event("failure", error=result["error"])
        desktop.screenshot(f"scenario-{letter}-failure")
    finally:
        if recorder is not None and recorder.poll() is None:
            recorder.terminate()
            try:
                recorder.wait(timeout=10)
            except subprocess.TimeoutExpired:
                recorder.kill()
                recorder.wait()
        if log_handle:
            log_handle.close()
        if process is not None:
            result["terminal_exit_before_cleanup"] = process.poll()
            if desktop.hwnd:
                desktop.user.PostMessageW(desktop.hwnd, 0x0010, 0, 0)
            time.sleep(0.5)
            if process.poll() is None:
                cleanup = subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, text=True)
                result["cleanup"] = {"exit_code": cleanup.returncode, "stdout": cleanup.stdout, "stderr": cleanup.stderr}
        desktop.hwnd = None
        save_json(out / f"scenario-{letter}-result.json", result)
    return result


def record(args) -> int:
    args.repo = str(Path(args.repo).resolve())
    args.output = str(Path(args.output).resolve())
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    desktop = Desktop(output)
    metadata = {"started_unix": time.time(), "platform": sys.platform, "python": sys.version,
                "capture": "ffmpeg gdigrab desktop; real terminal processes; Win32 SendInput keyboard",
                "app_source_modified": False, "recent_seeded": True,
                "acceptance_note": "Automation logs intended keystrokes, not synthetic UI state. Inspect screenshots/video for visual acceptance.",
                "scenarios": []}
    try:
        desktop.screenshot("desktop-before")
        metadata["initial_windows"] = windows(desktop.user)
        metadata["display"] = configure_display(desktop.user)
        desktop.screenshot("desktop-configured")
        if screen_size(desktop.user)[0] < 1600 or screen_size(desktop.user)[1] < 900:
            raise RuntimeError("The interactive display driver did not provide a desktop of at least 1600x900")
        for tool in ("ffmpeg", "ffprobe"):
            if not shutil.which(tool):
                raise RuntimeError(f"{tool} is not on PATH")
        metadata["ffmpeg_capabilities"] = {
            kind: command_version(["ffmpeg", "-hide_banner", option])
            for kind, option in (("devices", "-devices"), ("encoders", "-encoders"), ("filters", "-filters"))
        }
        missing = [
            capability
            for kind, capability in (("devices", "gdigrab"), ("encoders", "libx264"), ("filters", "drawtext"))
            if capability not in (
                metadata["ffmpeg_capabilities"][kind].get("stdout", "")
                + metadata["ffmpeg_capabilities"][kind].get("stderr", "")
            )
        ]
        if missing:
            raise RuntimeError(
                "ffmpeg lacks required capabilities: " + ", ".join(missing)
                + ". Install a complete Windows ffmpeg build (for example choco ffmpeg-full)."
            )
        git = locate_git()
        metadata["git"] = git
        metadata["versions"] = {"ffmpeg": command_version(["ffmpeg", "-version"]),
                                "mintty": command_version([git["mintty"], "--version"]),
                                "bash": command_version([git["bash"], "--version"])}
        font = install_font(output)
        metadata["font"] = font
        fixtures = make_fixtures(output)
        metadata["fixtures"] = fixtures
        save_json(output / "metadata.json", metadata)
        for letter in "abc":
            metadata["scenarios"].append(record_scenario(args, desktop, git, font, fixtures, letter))
            save_json(output / "metadata.json", metadata)
        # Preserve every scenario even if one failed, and assemble only complete successful captures.
        if all(item["success"] for item in metadata["scenarios"]):
            concat = output / "concat.txt"
            concat.write_text("".join(f"file 'scenario-{letter}.mp4'\n" for letter in "abc"), encoding="utf-8")
            command = ["ffmpeg", "-hide_banner", "-y", "-f", "concat", "-safe", "0", "-i", str(concat),
                       "-c", "copy", "-movflags", "+faststart", str(output / "powerpdf-mintty-arabic.mp4")]
            result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace")
            (output / "concat-ffmpeg.log").write_text(result.stdout + result.stderr, encoding="utf-8")
            metadata["concat_exit_code"] = result.returncode
            metadata["final_probe"] = command_version(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json",
                                                        str(output / "powerpdf-mintty-arabic.mp4")])
            metadata["success"] = result.returncode == 0
        else:
            metadata["success"] = False
            metadata["error"] = "At least one real desktop scenario failed; no success montage was fabricated"
    except Exception:
        metadata.update(success=False, error=traceback.format_exc())
        desktop.event("fatal-error", error=metadata["error"])
    finally:
        desktop.screenshot("desktop-final")
        metadata["final_windows"] = windows(desktop.user)
        metadata["ended_unix"] = time.time()
        save_json(output / "metadata.json", metadata)
        print(json.dumps({"success": metadata.get("success", False), "output": str(output), "error": metadata.get("error")}, ensure_ascii=True))
    return 0 if metadata.get("success") else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    capture = sub.add_parser("record")
    capture.add_argument("--repo", required=True)
    capture.add_argument("--output", required=True)
    child = sub.add_parser("session")
    for name in ("repo", "folder", "scenario", "state", "font", "log"):
        child.add_argument(f"--{name}", required=True)
    args = parser.parse_args()
    if sys.platform != "win32":
        parser.error("This recorder requires an actual Windows interactive desktop")
    return record(args) if args.command == "record" else session(args)


if __name__ == "__main__":
    raise SystemExit(main())
