from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parent
APP_URL = "http://127.0.0.1:5051/"
PORT = 5051
VENV_DIR = ROOT / ".venv"
PYTHON_EXE = VENV_DIR / "Scripts" / "python.exe"
DATA_DIR = ROOT / "data"
REQUIREMENTS_PATH = ROOT / "requirements.txt"
REQUIREMENTS_MARKER_PATH = DATA_DIR / "dashboard-requirements.sha256"
LOG_PATH = DATA_DIR / "dashboard-console.log"
PIP_OUT_PATH = DATA_DIR / "dashboard-pip.out.log"
PIP_ERR_PATH = DATA_DIR / "dashboard-pip.err.log"
SERVER_OUT_PATH = DATA_DIR / "dashboard-server.out.log"
SERVER_ERR_PATH = DATA_DIR / "dashboard-server.err.log"
BROWSER_PROFILE = DATA_DIR / "dashboard_window_profile"

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
CURRENT_PID = os.getpid()
PARENT_PID = os.getppid()


def startupinfo() -> subprocess.STARTUPINFO | None:
    if os.name != "nt":
        return None
    info = subprocess.STARTUPINFO()
    info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    info.wShowWindow = 0
    return info


def run_hidden(args: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(
        args,
        creationflags=CREATE_NO_WINDOW,
        startupinfo=startupinfo(),
        **kwargs,
    )


def popen_hidden(args: list[str], **kwargs) -> subprocess.Popen:
    return subprocess.Popen(
        args,
        creationflags=CREATE_NO_WINDOW,
        startupinfo=startupinfo(),
        **kwargs,
    )


def write_log(message: str) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8", errors="replace") as handle:
        handle.write(f"[{time.strftime('%Y-%m-%dT%H:%M:%S')}] {message}\n")


def reset_log() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    LOG_PATH.write_text(
        f"[{time.strftime('%Y-%m-%dT%H:%M:%S')}] Financial Dashboard launcher started.\n",
        encoding="utf-8",
    )


def append_file_to_log(path: Path) -> None:
    if not path.exists():
        return
    try:
        with path.open("r", encoding="utf-8", errors="replace") as source:
            with LOG_PATH.open("a", encoding="utf-8", errors="replace") as target:
                target.write(source.read())
    except OSError:
        pass


def dashboard_ready(path: str = "") -> bool:
    try:
        with urllib.request.urlopen(f"{APP_URL}{path}", timeout=2) as response:
            return response.status == 200
    except (OSError, urllib.error.URLError):
        return False


def post_shutdown() -> None:
    request = urllib.request.Request(f"{APP_URL}api/shutdown", method="POST")
    try:
        urllib.request.urlopen(request, timeout=2).read()
        write_log("Shutdown request sent to dashboard server.")
    except (OSError, urllib.error.URLError) as error:
        write_log(f"Shutdown request failed: {error}")


def stop_pid(pid: int, tree: bool = True) -> None:
    if pid <= 0 or pid in {CURRENT_PID, PARENT_PID}:
        return
    args = ["taskkill", "/PID", str(pid), "/F"]
    if tree:
        args.append("/T")
    run_hidden(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def process_ids_matching(command_text: str) -> set[int]:
    if os.name != "nt":
        return set()

    result = None
    wmic = shutil.which("wmic.exe")
    if wmic:
        escaped_text = command_text.replace("'", "''")
        where_clause = f"CommandLine like '%{escaped_text}%'"
        result = run_hidden(
            [wmic, "process", "where", where_clause, "get", "ProcessId", "/VALUE"],
            cwd=ROOT,
            text=True,
            capture_output=True,
        )
        output = result.stdout
    else:
        powershell = shutil.which("powershell.exe")
        if not powershell:
            return set()
        escaped_text = command_text.replace("'", "''")
        script = (
            f"$needle = '{escaped_text}'; "
            "Get-CimInstance Win32_Process | "
            "Where-Object { $_.ProcessId -ne $PID -and $_.CommandLine -like ('*' + $needle + '*') } | "
            "ForEach-Object { $_.ProcessId }"
        )
        result = run_hidden(
            [powershell, "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", script],
            cwd=ROOT,
            text=True,
            capture_output=True,
        )
        output = result.stdout

    ids: set[int] = set()
    for line in output.splitlines():
        line = line.strip()
        try:
            pid = int(line.split("=", 1)[-1])
        except ValueError:
            continue
        if pid != os.getpid():
            ids.add(pid)
    return ids


def stop_previous_launchers() -> None:
    for pid in process_ids_matching("dashboard_launcher.ps1"):
        stop_pid(pid, tree=True)


def stop_dashboard_browsers() -> None:
    for pid in process_ids_matching(str(BROWSER_PROFILE)):
        stop_pid(pid, tree=True)


def port_owner_pids() -> set[int]:
    result = run_hidden(
        ["netstat", "-ano", "-p", "tcp"],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )
    ids: set[int] = set()
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) < 5 or parts[0].upper() != "TCP":
            continue
        local_address = parts[1]
        state = parts[3].upper()
        if state != "LISTENING" or not local_address.endswith(f":{PORT}"):
            continue
        try:
            ids.add(int(parts[-1]))
        except ValueError:
            pass
    return ids


def stop_dashboard_port_owner() -> None:
    for pid in port_owner_pids():
        stop_pid(pid, tree=True)


def find_python_for_venv() -> list[str]:
    py_launcher = shutil.which("py.exe") or shutil.which("py")
    if py_launcher:
        return [py_launcher, "-3"]
    return [sys.executable]


def ensure_venv() -> None:
    if PYTHON_EXE.exists():
        return
    write_log("Creating virtual environment.")
    process = run_hidden(
        [*find_python_for_venv(), "-m", "venv", str(VENV_DIR)],
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if process.returncode != 0:
        raise RuntimeError(f"Failed to create virtual environment. Exit code {process.returncode}.")


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def ensure_requirements() -> None:
    requirements_hash = file_sha256(REQUIREMENTS_PATH)
    installed_hash = ""
    if REQUIREMENTS_MARKER_PATH.exists():
        installed_hash = REQUIREMENTS_MARKER_PATH.read_text(encoding="utf-8", errors="replace").splitlines()[0:1]
        installed_hash = installed_hash[0] if installed_hash else ""

    if requirements_hash == installed_hash:
        write_log("Requirements already current.")
        return

    write_log("Installing requirements.")
    for path in (PIP_OUT_PATH, PIP_ERR_PATH):
        try:
            path.unlink()
        except FileNotFoundError:
            pass

    with PIP_OUT_PATH.open("w", encoding="utf-8", errors="replace") as stdout, PIP_ERR_PATH.open(
        "w", encoding="utf-8", errors="replace"
    ) as stderr:
        process = run_hidden(
            [
                str(PYTHON_EXE),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--quiet",
                "-r",
                "requirements.txt",
            ],
            cwd=ROOT,
            stdout=stdout,
            stderr=stderr,
        )

    append_file_to_log(PIP_OUT_PATH)
    append_file_to_log(PIP_ERR_PATH)
    if process.returncode != 0:
        raise RuntimeError(f"Failed to install requirements. Exit code {process.returncode}.")
    REQUIREMENTS_MARKER_PATH.write_text(requirements_hash, encoding="utf-8")


def start_server() -> subprocess.Popen:
    write_log("Starting dashboard server.")
    for path in (SERVER_OUT_PATH, SERVER_ERR_PATH):
        try:
            path.unlink()
        except FileNotFoundError:
            pass

    stdout = SERVER_OUT_PATH.open("w", encoding="utf-8", errors="replace")
    stderr = SERVER_ERR_PATH.open("w", encoding="utf-8", errors="replace")
    return popen_hidden([str(PYTHON_EXE), "app.py"], cwd=ROOT, stdout=stdout, stderr=stderr)


def wait_for_server() -> None:
    deadline = time.monotonic() + 30
    while not dashboard_ready():
        if time.monotonic() > deadline:
            append_file_to_log(SERVER_OUT_PATH)
            append_file_to_log(SERVER_ERR_PATH)
            raise RuntimeError(f"Server did not respond at {APP_URL}.")
        time.sleep(0.5)


def find_browser() -> str | None:
    for executable in ("msedge.exe", "chrome.exe"):
        command = shutil.which(executable)
        if command:
            return command

    candidates = [
        os.environ.get("ProgramFiles", "") + r"\Microsoft\Edge\Application\msedge.exe",
        os.environ.get("ProgramFiles(x86)", "") + r"\Microsoft\Edge\Application\msedge.exe",
        os.environ.get("ProgramFiles", "") + r"\Google\Chrome\Application\chrome.exe",
        os.environ.get("ProgramFiles(x86)", "") + r"\Google\Chrome\Application\chrome.exe",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return candidate
    return None


def open_dashboard_window(browser_path: str) -> subprocess.Popen:
    BROWSER_PROFILE.mkdir(parents=True, exist_ok=True)
    write_log("Opening dashboard app window.")
    return popen_hidden(
        [browser_path, f"--app={APP_URL}", f"--user-data-dir={BROWSER_PROFILE}", "--no-first-run"],
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def show_log() -> None:
    notepad = shutil.which("notepad.exe") or "notepad.exe"
    popen_hidden([notepad, str(LOG_PATH)], cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main() -> None:
    os.chdir(ROOT)
    reset_log()
    server_process: subprocess.Popen | None = None
    launched_managed_browser = False

    try:
        write_log("Closing any previous dashboard instance.")
        had_previous_instance = bool(process_ids_matching(str(BROWSER_PROFILE)) or port_owner_pids())
        stop_dashboard_browsers()
        stop_previous_launchers()
        stop_dashboard_port_owner()
        if had_previous_instance:
            time.sleep(0.75)

        ensure_venv()
        ensure_requirements()
        server_process = start_server()
        wait_for_server()

        if os.environ.get("FD_LAUNCHER_SMOKE") == "1":
            write_log("Smoke test mode reached a ready dashboard server.")
            launched_managed_browser = True
            return

        browser = find_browser()
        if browser:
            launched_managed_browser = True
            browser_process = open_dashboard_window(browser)
            time.sleep(3)
            while browser_process.poll() is None or process_ids_matching(str(BROWSER_PROFILE)):
                time.sleep(2)
            write_log("Dashboard app window closed.")
        else:
            write_log("Edge or Chrome was not found. Opening the default browser without automatic close detection.")
            os.startfile(APP_URL)  # type: ignore[attr-defined]
    except Exception as error:
        write_log(f"Launcher error: {type(error).__name__}: {error}")
        show_log()
    finally:
        if launched_managed_browser:
            post_shutdown()
            time.sleep(1)
            if server_process and server_process.poll() is None:
                stop_pid(server_process.pid, tree=True)
                write_log("Stopped launcher-owned server process.")


if __name__ == "__main__":
    main()
