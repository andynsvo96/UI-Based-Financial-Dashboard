from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait

from config import (
    CHROME_PROFILES_DIR,
    DOWNLOAD_TIMEOUT_SECONDS,
    LAST_RESULT_PATH,
    PAGE_READY_TIMEOUT_SECONDS,
    SCREENSHOTS_DIR,
    SCREENSHOT_FORMAT,
    STATUS_WAITING_FOR_LOGIN,
    STATUS_WAITING_FOR_MFA,
    STATUS_WAITING_FOR_USER_ACTION,
)


def find_chrome_executable() -> str | None:
    candidates = [
        shutil.which("chrome"),
        shutil.which("chrome.exe"),
        shutil.which("google-chrome"),
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return str(candidate)
    return None


def chrome_debugger_available(debugger_address: str) -> bool:
    try:
        with urllib.request.urlopen(f"http://{debugger_address}/json/version", timeout=1) as response:
            return response.status == 200
    except (OSError, urllib.error.URLError):
        return False


def terminate_existing_chrome_processes(
    profile_name: str | None = None,
    debugging_port: int | None = None,
) -> None:
    """Close existing app-controlled Chrome processes before opening a new one."""
    system = platform.system().lower()
    profile_path = str((CHROME_PROFILES_DIR / profile_name).resolve()) if profile_name else ""
    port_text = str(debugging_port) if debugging_port else ""

    if system == "windows":
        env = {
            **dict(os.environ),
            "FD_CHROME_PROFILE": profile_path,
            "FD_CHROME_PORT": port_text,
        }
        subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                (
                    "$profile=$env:FD_CHROME_PROFILE; "
                    "$port=$env:FD_CHROME_PORT; "
                    "Get-CimInstance Win32_Process | "
                    "Where-Object { $_.Name -like 'chrome*' -and "
                    "(($profile -and $_.CommandLine -like \"*$profile*\") -or "
                    "($port -and $_.CommandLine -like \"*remote-debugging-port=$port*\")) } | "
                    "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
                ),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
            check=False,
        )
    elif system == "darwin":
        patterns = [profile_path, f"remote-debugging-port={port_text}" if port_text else ""]
        for pattern in [value for value in patterns if value]:
            subprocess.run(
                ["pkill", "-f", pattern],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
    else:
        patterns = [profile_path, f"remote-debugging-port={port_text}" if port_text else ""]
        for pattern in [value for value in patterns if value]:
            subprocess.run(
                ["pkill", "-f", pattern],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
    time.sleep(1)


def open_manual_chrome_profile(
    profile_name: str,
    url: str,
    debugging_port: int | None = None,
    terminate_existing: bool = True,
) -> subprocess.Popen[Any]:
    """Open a normal user-controlled Chrome profile for manual login/setup."""
    chrome_exe = find_chrome_executable()
    if not chrome_exe:
        raise RuntimeError("Google Chrome was not found. Install Chrome or add chrome.exe to PATH.")

    if terminate_existing:
        terminate_existing_chrome_processes(profile_name=profile_name, debugging_port=debugging_port)

    profile_path = CHROME_PROFILES_DIR / profile_name
    profile_path.mkdir(parents=True, exist_ok=True)
    command = [
        chrome_exe,
        f"--user-data-dir={profile_path}",
        "--no-first-run",
        "--new-window",
    ]
    if debugging_port:
        command.append(f"--remote-debugging-port={debugging_port}")
    command.append(url)

    creationflags = 0
    if platform.system().lower() == "windows":
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
        if hasattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB"):
            creationflags |= subprocess.CREATE_BREAKAWAY_FROM_JOB

    return subprocess.Popen(
        command,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creationflags,
    )


def create_driver(
    profile_name: str,
    download_dir: str | Path,
    detach: bool = False,
    debugger_address: str | None = None,
) -> webdriver.Chrome:
    profile_path = CHROME_PROFILES_DIR / profile_name
    profile_path.mkdir(parents=True, exist_ok=True)
    download_path = Path(download_dir)
    download_path.mkdir(parents=True, exist_ok=True)

    options = Options()
    options.add_argument(f"--user-data-dir={profile_path}")
    options.add_argument("--start-maximized")
    options.add_argument("--disable-notifications")
    options.add_experimental_option(
        "prefs",
        {
            "download.default_directory": str(download_path.resolve()),
            "download.prompt_for_download": False,
            "download.directory_upgrade": True,
            "safebrowsing.enabled": True,
        },
    )
    if detach and not debugger_address:
        options.add_experimental_option("detach", True)
    if debugger_address:
        options.add_experimental_option("debuggerAddress", debugger_address)

    return webdriver.Chrome(options=options)


def safe_get(driver: webdriver.Chrome, url: str) -> None:
    driver.get(url)
    wait_for_page_ready(driver)


def wait_for_page_ready(driver: webdriver.Chrome, timeout: int = PAGE_READY_TIMEOUT_SECONDS) -> bool:
    try:
        WebDriverWait(driver, timeout).until(
            lambda active_driver: active_driver.execute_script("return document.readyState") == "complete"
        )
        return True
    except TimeoutException:
        return False


def save_screenshot(driver: webdriver.Chrome, label: str) -> str | None:
    SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    clean_label = re.sub(r"[^a-zA-Z0-9_.-]+", "_", label).strip("_") or "screenshot"
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = SCREENSHOTS_DIR / f"{timestamp}_{clean_label}.{SCREENSHOT_FORMAT}"
    try:
        driver.save_screenshot(str(path))
        return str(path)
    except WebDriverException:
        return None


def write_result(payload: dict[str, Any]) -> None:
    write_json(LAST_RESULT_PATH, payload)


def read_json(path: str | Path, default: Any) -> Any:
    json_path = Path(path)
    if not json_path.exists():
        return default
    try:
        return json.loads(json_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def write_json(path: str | Path, payload: Any) -> None:
    json_path = Path(path)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def compute_file_hash(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def wait_for_downloads(download_dir: str | Path, timeout: int = DOWNLOAD_TIMEOUT_SECONDS) -> list[str]:
    directory = Path(download_dir)
    directory.mkdir(parents=True, exist_ok=True)
    deadline = time.time() + timeout
    while time.time() < deadline:
        partials = list(directory.glob("*.crdownload")) + list(directory.glob("*.tmp"))
        if not partials:
            return [str(path) for path in directory.iterdir() if path.is_file()]
        time.sleep(1)
    return [str(path) for path in directory.iterdir() if path.is_file() and not path.name.endswith(".crdownload")]


def detect_possible_login_or_mfa(driver: webdriver.Chrome) -> dict[str, Any]:
    """Detect prompts that require a human without attempting to solve or bypass them."""
    reasons: list[str] = []
    status = STATUS_WAITING_FOR_USER_ACTION

    current_url = ""
    page_text = ""
    try:
        current_url = driver.current_url.lower()
        page_text = driver.find_element("tag name", "body").text.lower()
    except WebDriverException:
        page_text = ""

    try:
        password_fields = driver.find_elements("css selector", "input[type='password']")
    except WebDriverException:
        password_fields = []

    login_terms = ["log in", "sign in", "username", "user id", "password"]
    mfa_terms = [
        "verification code",
        "security code",
        "one-time",
        "one time",
        "multi-factor",
        "multifactor",
        "mfa",
        "captcha",
        "device verification",
        "verify your identity",
    ]

    if password_fields or any(term in current_url for term in ["login", "signin", "signon"]):
        reasons.append("login prompt detected")
        status = STATUS_WAITING_FOR_LOGIN
    try:
        login_inputs = driver.find_elements(
            "css selector",
            "input[name*='user' i], input[id*='user' i], input[name*='login' i], input[id*='login' i]",
        )
    except WebDriverException:
        login_inputs = []

    if any(term in current_url for term in ["login", "signin", "signon"]):
        reasons.append("login url detected")
        status = STATUS_WAITING_FOR_LOGIN
    elif login_inputs and any(term in page_text for term in login_terms):
        reasons.append("login form detected")
        status = STATUS_WAITING_FOR_LOGIN
    if any(term in page_text for term in mfa_terms):
        reasons.append("security verification prompt detected")
        status = STATUS_WAITING_FOR_MFA

    return {
        "detected": bool(reasons),
        "status": status,
        "message": "Please complete login or verification in the opened browser, then click Sync Citizens again.",
        "reasons": reasons,
    }
