from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import platform
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from selenium import webdriver
from selenium.common.exceptions import StaleElementReferenceException, TimeoutException, WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from statement_parser import DB_PATH, DATA_DIR, account_id, classify, connection, init_db, normalize_space, parse_money, utc_now


CITIZENS_SITE = "citizens_2439"
CITIZENS_NAME = "Citizens Checking"
CITIZENS_ACCOUNT_TYPE = "checking"
CITIZENS_LOGIN_URL = "https://www.citizensbankonline.com/olb-root/login"
CITIZENS_HOME_URL = "https://www.citizensbankonline.com/olb-root/home/accounts"
CITIZENS_PROFILE_NAME = "citizens"
CITIZENS_DEBUGGING_PORT = 9223
CITIZENS_DEBUGGER_ADDRESS = f"127.0.0.1:{CITIZENS_DEBUGGING_PORT}"
AMEX_SITE = "amex_gold"
AMEX_NAME = "Amex Gold Card"
AMEX_ACCOUNT_TYPE = "credit"
AMEX_REWARDS_SITE = "amex_gold_rewards"
AMEX_REWARDS_NAME = "Amex Gold Card Rewards"
AMEX_REWARDS_ACCOUNT_TYPE = "rewards"
AMEX_DASHBOARD_URL = "https://global.americanexpress.com/dashboard"
AMEX_PROFILE_NAME = "amex"
AMEX_DEBUGGING_PORT = 9224
AMEX_DEBUGGER_ADDRESS = f"127.0.0.1:{AMEX_DEBUGGING_PORT}"
CHASE_SITE = "chase_prime_visa"
CHASE_NAME = "Chase Prime Visa"
CHASE_ACCOUNT_TYPE = "credit"
CHASE_REWARDS_SITE = "chase_prime_rewards"
CHASE_REWARDS_NAME = "Chase Rewards"
CHASE_REWARDS_ACCOUNT_TYPE = "rewards"
CHASE_DASHBOARD_URL = "https://secure04ea.chase.com/web/auth/dashboard#/dashboard/overview"
CHASE_PROFILE_NAME = "chase"
CHASE_DEBUGGING_PORT = 9225
CHASE_DEBUGGER_ADDRESS = f"127.0.0.1:{CHASE_DEBUGGING_PORT}"
CITI_SITE = "citi_costco_visa"
CITI_NAME = "Citi Costco Anywhere"
CITI_ACCOUNT_TYPE = "credit"
CITI_REWARDS_SITE = "citi_costco_rewards"
CITI_REWARDS_NAME = "Costco Cash Rewards"
CITI_REWARDS_ACCOUNT_TYPE = "rewards"
CITI_DASHBOARD_URL = "https://online.citi.com/US/ag/dashboard/summary"
CITI_PROFILE_NAME = "citi"
CITI_DEBUGGING_PORT = 9226
CITI_DEBUGGER_ADDRESS = f"127.0.0.1:{CITI_DEBUGGING_PORT}"
VANGUARD_SITE = "vanguard_retirement"
VANGUARD_NAME = "Vanguard Retirement"
VANGUARD_ACCOUNT_TYPE = "retirement"
VANGUARD_LOGIN_URL = "https://my.vanguardplan.com/login/participant"
VANGUARD_PROFILE_NAME = "vanguard"
VANGUARD_DEBUGGING_PORT = 9227
VANGUARD_DEBUGGER_ADDRESS = f"127.0.0.1:{VANGUARD_DEBUGGING_PORT}"
SYNC_DIR = DATA_DIR / "sync"
PROFILE_DIR = SYNC_DIR / "chrome_profiles" / CITIZENS_PROFILE_NAME
DOWNLOAD_DIR = SYNC_DIR / "downloads" / "citizens"
AMEX_PROFILE_DIR = SYNC_DIR / "chrome_profiles" / AMEX_PROFILE_NAME
AMEX_DOWNLOAD_DIR = SYNC_DIR / "downloads" / "amex"
CHASE_PROFILE_DIR = SYNC_DIR / "chrome_profiles" / CHASE_PROFILE_NAME
CHASE_DOWNLOAD_DIR = SYNC_DIR / "downloads" / "chase"
CITI_PROFILE_DIR = SYNC_DIR / "chrome_profiles" / CITI_PROFILE_NAME
CITI_DOWNLOAD_DIR = SYNC_DIR / "downloads" / "citi"
VANGUARD_PROFILE_DIR = SYNC_DIR / "chrome_profiles" / VANGUARD_PROFILE_NAME
VANGUARD_DOWNLOAD_DIR = SYNC_DIR / "downloads" / "vanguard"
SCREENSHOTS_DIR = SYNC_DIR / "screenshots"
SYNC_STATE_PATH = SYNC_DIR / "sync_state.json"

STATUS_IDLE = "idle"
STATUS_RUNNING = "running"
STATUS_WAITING_FOR_LOGIN = "waiting_for_login"
STATUS_WAITING_FOR_USER_ACTION = "waiting_for_user_action"
STATUS_SUCCESS = "success"
STATUS_FAILED = "failed"

LOGIN_ENV_PREFIXES = {
    "amex": "AMEX",
    "chase": "CHASE",
    "citizens": "CITIZENS",
    "citi": "CITI",
    "vanguard": "VANGUARD",
}


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def env_first(names: list[str]) -> str | None:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return None


def login_credentials(institution: str | None) -> tuple[str, str] | None:
    prefix = LOGIN_ENV_PREFIXES.get(institution or "")
    if not prefix:
        return None
    username = env_first(
        [
            f"FIN_DASH_{prefix}_USERNAME",
            f"FIN_DASH_{prefix}_USER",
            f"FD_{prefix}_USERNAME",
            f"FD_{prefix}_USER",
        ]
    )
    password = env_first([f"FIN_DASH_{prefix}_PASSWORD", f"FD_{prefix}_PASSWORD", f"{prefix}_PASSWORD"])
    if username and password:
        return username, password
    return None


def base_result(mode: str, institution: str = "citizens") -> dict[str, Any]:
    display = {"amex": "Amex", "chase": "Chase", "citi": "Citi", "citizens": "Citizens", "vanguard": "Vanguard"}.get(institution, institution.title())
    return {
        "success": False,
        "status": STATUS_RUNNING,
        "institution": institution,
        "mode": mode,
        "message": f"{display} sync is running.",
        "started_at": utc_now(),
        "finished_at": None,
        "data": {"balances": [], "transactions": [], "transaction_exports": []},
        "errors": [],
        "screenshots": [],
    }


def finish_result(result: dict[str, Any], status: str, message: str, success: bool | None = None) -> dict[str, Any]:
    result["status"] = status
    result["success"] = status == STATUS_SUCCESS if success is None else success
    result["message"] = message
    result["finished_at"] = utc_now()
    write_json(SYNC_STATE_PATH, result)
    return result


def ensure_sync_tables() -> None:
    init_db()
    SYNC_DIR.mkdir(parents=True, exist_ok=True)
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    AMEX_DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    CHASE_DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    CITI_DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    VANGUARD_DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)


def find_chrome_executable() -> str | None:
    candidates = [
        shutil.which("chrome"),
        shutil.which("chrome.exe"),
        shutil.which("google-chrome"),
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return str(candidate)
    return None


def chrome_debugger_available_at(debugger_address: str) -> bool:
    try:
        with urllib.request.urlopen(f"http://{debugger_address}/json/version", timeout=1) as response:
            return response.status == 200
    except (OSError, urllib.error.URLError):
        return False


def chrome_debugger_available() -> bool:
    return chrome_debugger_available_at(CITIZENS_DEBUGGER_ADDRESS)


def terminate_chrome_profile(profile_dir: Path, port: int) -> None:
    profile_path = str(profile_dir.resolve())
    port_text = str(port)
    if platform.system().lower() == "windows":
        env = {**dict(os.environ), "FD_CHROME_PROFILE": profile_path, "FD_CHROME_PORT": port_text}
        subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                (
                    "$profile=$env:FD_CHROME_PROFILE; $port=$env:FD_CHROME_PORT; "
                    "Get-CimInstance Win32_Process | "
                    "Where-Object { $_.Name -like 'chrome*' -and "
                    "(($_.CommandLine -like \"*$profile*\") -or ($_.CommandLine -like \"*remote-debugging-port=$port*\")) } | "
                    "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
                ),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
            check=False,
        )
    else:
        for pattern in [profile_path, f"remote-debugging-port={port_text}"]:
            subprocess.run(["pkill", "-f", pattern], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    time.sleep(1)


def terminate_profile_chrome() -> None:
    terminate_chrome_profile(PROFILE_DIR, CITIZENS_DEBUGGING_PORT)


def close_completed_sync_browser(result: dict[str, Any], profile_dir: Path, port: int) -> None:
    status = result.get("status")
    if status in {STATUS_WAITING_FOR_LOGIN, STATUS_WAITING_FOR_USER_ACTION, STATUS_RUNNING}:
        return
    terminate_chrome_profile(profile_dir, port)


def open_chrome_profile(
    profile_dir: Path,
    port: int,
    url: str,
    message: str,
    terminate_existing: bool = False,
    background: bool = False,
) -> dict[str, Any]:
    ensure_sync_tables()
    chrome = find_chrome_executable()
    if not chrome:
        raise RuntimeError("Google Chrome was not found. Install Chrome or add chrome.exe to PATH.")
    if terminate_existing:
        terminate_chrome_profile(profile_dir, port)
    profile_dir.mkdir(parents=True, exist_ok=True)
    command = [
        chrome,
        f"--user-data-dir={profile_dir}",
        f"--remote-debugging-port={port}",
        "--no-first-run",
        "--new-window",
    ]
    if background:
        command.extend(["--window-position=-32000,0", "--window-size=1280,900"])
    command.append(url)
    creationflags = 0
    if platform.system().lower() == "windows":
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
        if hasattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB"):
            creationflags |= subprocess.CREATE_BREAKAWAY_FROM_JOB
    process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=creationflags)
    return {
        "status": STATUS_WAITING_FOR_LOGIN,
        "message": message,
        "pid": process.pid,
    }


def open_citizens_browser(terminate_existing: bool = False) -> dict[str, Any]:
    return open_chrome_profile(
        PROFILE_DIR,
        CITIZENS_DEBUGGING_PORT,
        CITIZENS_LOGIN_URL,
        "Opened Citizens in a root dashboard Chrome profile. Saved Chrome credentials will be submitted automatically during sync.",
        terminate_existing,
    )


def open_amex_browser(terminate_existing: bool = False) -> dict[str, Any]:
    return open_chrome_profile(
        AMEX_PROFILE_DIR,
        AMEX_DEBUGGING_PORT,
        AMEX_DASHBOARD_URL,
        "Opened Amex in a root dashboard Chrome profile. Saved Chrome credentials will be submitted automatically during sync.",
        terminate_existing,
    )


def open_chase_browser(terminate_existing: bool = False) -> dict[str, Any]:
    return open_chrome_profile(
        CHASE_PROFILE_DIR,
        CHASE_DEBUGGING_PORT,
        CHASE_DASHBOARD_URL,
        "Opened Chase in a root dashboard Chrome profile. Saved Chrome credentials will be submitted automatically during sync.",
        terminate_existing,
    )


def open_citi_browser(terminate_existing: bool = False) -> dict[str, Any]:
    return open_chrome_profile(
        CITI_PROFILE_DIR,
        CITI_DEBUGGING_PORT,
        CITI_DASHBOARD_URL,
        "Opened Citi in a root dashboard Chrome profile. Saved Chrome credentials will be submitted automatically during sync.",
        terminate_existing,
    )


def open_vanguard_browser(terminate_existing: bool = False) -> dict[str, Any]:
    return open_chrome_profile(
        VANGUARD_PROFILE_DIR,
        VANGUARD_DEBUGGING_PORT,
        VANGUARD_LOGIN_URL,
        "Opened Vanguard in a root dashboard Chrome profile. Saved Chrome credentials will be submitted automatically during sync.",
        terminate_existing,
    )


def open_citizens_sync_browser() -> dict[str, Any]:
    return open_chrome_profile(
        PROFILE_DIR,
        CITIZENS_DEBUGGING_PORT,
        CITIZENS_LOGIN_URL,
        "Opened Citizens sync browser in the background.",
        terminate_existing=False,
        background=True,
    )


def open_amex_sync_browser() -> dict[str, Any]:
    return open_chrome_profile(
        AMEX_PROFILE_DIR,
        AMEX_DEBUGGING_PORT,
        AMEX_DASHBOARD_URL,
        "Opened Amex sync browser in the background.",
        terminate_existing=False,
        background=True,
    )


def open_chase_sync_browser() -> dict[str, Any]:
    return open_chrome_profile(
        CHASE_PROFILE_DIR,
        CHASE_DEBUGGING_PORT,
        CHASE_DASHBOARD_URL,
        "Opened Chase sync browser in the background.",
        terminate_existing=False,
        background=True,
    )


def open_citi_sync_browser() -> dict[str, Any]:
    return open_chrome_profile(
        CITI_PROFILE_DIR,
        CITI_DEBUGGING_PORT,
        CITI_DASHBOARD_URL,
        "Opened Citi sync browser in the background.",
        terminate_existing=False,
        background=True,
    )


def open_vanguard_sync_browser() -> dict[str, Any]:
    return open_chrome_profile(
        VANGUARD_PROFILE_DIR,
        VANGUARD_DEBUGGING_PORT,
        VANGUARD_LOGIN_URL,
        "Opened Vanguard sync browser in the background.",
        terminate_existing=False,
        background=True,
    )


def create_driver_for(profile_dir: Path, download_dir: Path, debugger_address: str) -> webdriver.Chrome:
    profile_dir.mkdir(parents=True, exist_ok=True)
    download_dir.mkdir(parents=True, exist_ok=True)
    options = Options()
    options.add_experimental_option("debuggerAddress", debugger_address)
    options.add_experimental_option(
        "prefs",
        {
            "download.default_directory": str(download_dir.resolve()),
            "download.prompt_for_download": False,
            "download.directory_upgrade": True,
            "safebrowsing.enabled": True,
        },
    )
    driver = webdriver.Chrome(options=options)
    try:
        driver.execute_cdp_cmd(
            "Page.setDownloadBehavior",
            {"behavior": "allow", "downloadPath": str(download_dir.resolve())},
        )
    except WebDriverException:
        pass
    return driver


def create_driver() -> webdriver.Chrome:
    return create_driver_for(PROFILE_DIR, DOWNLOAD_DIR, CITIZENS_DEBUGGER_ADDRESS)


def create_amex_driver() -> webdriver.Chrome:
    return create_driver_for(AMEX_PROFILE_DIR, AMEX_DOWNLOAD_DIR, AMEX_DEBUGGER_ADDRESS)


def create_chase_driver() -> webdriver.Chrome:
    return create_driver_for(CHASE_PROFILE_DIR, CHASE_DOWNLOAD_DIR, CHASE_DEBUGGER_ADDRESS)


def create_citi_driver() -> webdriver.Chrome:
    return create_driver_for(CITI_PROFILE_DIR, CITI_DOWNLOAD_DIR, CITI_DEBUGGER_ADDRESS)


def create_vanguard_driver() -> webdriver.Chrome:
    return create_driver_for(VANGUARD_PROFILE_DIR, VANGUARD_DOWNLOAD_DIR, VANGUARD_DEBUGGER_ADDRESS)


def move_sync_window_to_background(driver: webdriver.Chrome) -> None:
    try:
        driver.set_window_rect(x=-32000, y=0, width=1280, height=900)
        return
    except WebDriverException:
        pass
    try:
        window = driver.execute_cdp_cmd("Browser.getWindowForTarget", {})
        driver.execute_cdp_cmd("Browser.setWindowBounds", {"windowId": window.get("windowId"), "bounds": {"left": -32000, "top": 0, "width": 1280, "height": 900}})
    except WebDriverException:
        pass


def capture_screenshot(driver: webdriver.Chrome | None, result: dict[str, Any], label: str) -> str | None:
    if not driver:
        return None
    safe_label = re.sub(r"[^a-z0-9_-]+", "_", label.lower()).strip("_") or "screenshot"
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = SCREENSHOTS_DIR / f"{stamp}_{safe_label}.png"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        driver.save_screenshot(str(path))
        result.setdefault("screenshots", []).append(str(path))
        return str(path)
    except WebDriverException:
        return None


def wait_ready(driver: webdriver.Chrome, timeout: int = 30) -> bool:
    try:
        WebDriverWait(driver, timeout).until(lambda active: active.execute_script("return document.readyState") == "complete")
        return True
    except TimeoutException:
        return False


def safe_get(driver: webdriver.Chrome, url: str) -> None:
    driver.get(url)
    wait_ready(driver)


def visible_text(driver: webdriver.Chrome) -> str:
    chunks: list[str] = []
    deep_text_script = """
        function rootText(root) {
            const base = root.body
                ? root.body.innerText
                : Array.from(root.children || []).map((node) => node.innerText || node.textContent || '').join('\\n');
            const chunks = [base || ''];
            for (const node of Array.from(root.querySelectorAll ? root.querySelectorAll('*') : [])) {
                if (node.shadowRoot) chunks.push(rootText(node.shadowRoot));
            }
            return chunks.filter(Boolean).join('\\n');
        }
        return rootText(document);
    """
    try:
        driver.switch_to.default_content()
        chunks.append(driver.find_element(By.TAG_NAME, "body").text)
        chunks.append(str(driver.execute_script(deep_text_script) or ""))
    except WebDriverException:
        pass
    try:
        frames = driver.find_elements(By.CSS_SELECTOR, "iframe, frame")
    except WebDriverException:
        frames = []
    for frame in frames:
        try:
            driver.switch_to.default_content()
            driver.switch_to.frame(frame)
            chunks.append(driver.find_element(By.TAG_NAME, "body").text)
            chunks.append(str(driver.execute_script(deep_text_script) or ""))
        except WebDriverException:
            continue
    try:
        driver.switch_to.default_content()
    except WebDriverException:
        pass
    return "\n".join(chunk for chunk in chunks if chunk)


def is_logged_in(driver: webdriver.Chrome) -> bool:
    text = visible_text(driver).lower()
    if "connecting to online banking" in text or ("user id" in text and "password" in text):
        return False
    dashboard_terms = [
        "checking accounts",
        "account summary",
        "available balance",
        "current balance",
        "account details",
        "recent transactions",
    ]
    return any(term in text for term in dashboard_terms)


def page_needs_login(driver: webdriver.Chrome) -> bool:
    text = visible_text(driver).lower()
    url = ""
    try:
        url = driver.current_url.lower()
    except WebDriverException:
        pass
    return "login" in url or "log in" in text or ("user id" in text and "password" in text)


def page_needs_user_action(driver: webdriver.Chrome) -> bool:
    text = visible_text(driver).lower()
    terms = [
        "verification code",
        "security code",
        "one-time",
        "one time",
        "verify your identity",
        "captcha",
        "device verification",
    ]
    return any(term in text for term in terms)


def wait_for_login_or_dashboard(driver: webdriver.Chrome, timeout: int = 25) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        wait_ready(driver, timeout=3)
        if is_logged_in(driver) or page_needs_login(driver) or page_needs_user_action(driver):
            return
        time.sleep(1)


def click_by_text(driver: webdriver.Chrome, labels: list[str], exact: bool = False) -> bool:
    script = """
        const labels = arguments[0].map((label) => label.replace(/\\s+/g, ' ').trim().toLowerCase());
        const exact = arguments[1];
        function collect(root) {
            const selectors = 'a, button, [role="button"], input, [aria-label], [title]';
            const found = Array.from(root.querySelectorAll(selectors));
            for (const node of Array.from(root.querySelectorAll('*'))) {
                if (node.shadowRoot) found.push(...collect(node.shadowRoot));
            }
            return found;
        }
        const nodes = collect(document);
        function visible(node) {
            const style = window.getComputedStyle(node);
            const rect = node.getBoundingClientRect();
            return style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0;
        }
        function nodeText(node) {
            return [node.innerText, node.textContent, node.getAttribute('aria-label'), node.getAttribute('title'), node.value]
                .filter(Boolean).join(' ').replace(/\\s+/g, ' ').trim().toLowerCase();
        }
        for (const node of nodes) {
            if (!visible(node)) continue;
            const text = nodeText(node);
            for (const label of labels) {
                if ((exact && text === label) || (!exact && text.includes(label))) {
                    node.scrollIntoView({block: 'center'});
                    node.click();
                    return true;
                }
            }
        }
        return false;
    """
    def attempt_in_current_context() -> bool:
        try:
            return bool(driver.execute_script(script, labels, exact))
        except WebDriverException:
            return False

    try:
        driver.switch_to.default_content()
    except WebDriverException:
        pass
    clicked = attempt_in_current_context()
    if not clicked:
        try:
            frames = driver.find_elements(By.CSS_SELECTOR, "iframe, frame")
        except WebDriverException:
            frames = []
        for frame in frames:
            try:
                driver.switch_to.default_content()
                driver.switch_to.frame(frame)
            except WebDriverException:
                continue
            clicked = attempt_in_current_context()
            if clicked:
                break
    try:
        driver.switch_to.default_content()
    except WebDriverException:
        pass
    if clicked:
        wait_ready(driver, timeout=10)
    return clicked


def _submit_autofilled_login_in_context(driver: webdriver.Chrome, labels: list[str]) -> str:
    script = """
        const labels = arguments[0].map((label) => label.replace(/\\s+/g, ' ').trim().toLowerCase());
        function collect(root) {
            const found = Array.from(root.querySelectorAll('input, button, a, [role="button"], [aria-label], [title]'));
            for (const node of Array.from(root.querySelectorAll('*'))) {
                if (node.shadowRoot) found.push(...collect(node.shadowRoot));
            }
            return found;
        }
        function visible(node) {
            const style = window.getComputedStyle(node);
            const rect = node.getBoundingClientRect();
            return style.visibility !== 'hidden' && style.display !== 'none'
                && rect.width > 0 && rect.height > 0 && node.getAttribute('aria-hidden') !== 'true';
        }
        function nodeText(node) {
            return [
                node.innerText,
                node.textContent,
                node.getAttribute('aria-label'),
                node.getAttribute('title'),
                node.getAttribute('name'),
                node.getAttribute('id'),
                node.value,
            ].filter(Boolean).join(' ').replace(/\\s+/g, ' ').trim().toLowerCase();
        }
        function scoreSubmit(node, form) {
            if (!visible(node) || node.disabled || node.getAttribute('aria-disabled') === 'true') return -1;
            const text = nodeText(node);
            let score = 0;
            if (form && node.form === form) score += 10;
            if ((node.getAttribute('type') || '').toLowerCase() === 'submit') score += 8;
            if (node.tagName.toLowerCase() === 'button') score += 4;
            if (node.getAttribute('role') === 'button') score += 3;
            if (labels.some((label) => text === label)) score += 20;
            if (labels.some((label) => text.includes(label))) score += 12;
            if (/^(next|continue|submit)$/i.test(text)) score += 6;
            return score;
        }
        const nodes = collect(document);
        const passwordFields = nodes.filter((node) => {
            return node.tagName && node.tagName.toLowerCase() === 'input'
                && (node.getAttribute('type') || '').toLowerCase() === 'password'
                && visible(node);
        });
        if (!passwordFields.length) return 'no_password';
        const filledPassword = passwordFields.find((node) => String(node.value || '').trim().length > 0);
        if (!filledPassword) return 'empty_password';
        const form = filledPassword.closest('form');
        const fields = nodes.filter((node) => node.tagName && node.tagName.toLowerCase() === 'input' && visible(node));
        for (const field of fields) {
            try {
                field.dispatchEvent(new Event('input', {bubbles: true}));
                field.dispatchEvent(new Event('change', {bubbles: true}));
                field.dispatchEvent(new Event('blur', {bubbles: true}));
            } catch (_) {}
        }
        const candidates = nodes
            .map((node) => ({node, score: scoreSubmit(node, form)}))
            .filter((item) => item.score >= 0)
            .sort((a, b) => b.score - a.score);
        if (candidates.length && candidates[0].score > 0) {
            candidates[0].node.scrollIntoView({block: 'center'});
            candidates[0].node.click();
            return 'clicked';
        }
        try {
            filledPassword.focus();
            filledPassword.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', code: 'Enter', bubbles: true}));
            filledPassword.dispatchEvent(new KeyboardEvent('keyup', {key: 'Enter', code: 'Enter', bubbles: true}));
        } catch (_) {}
        if (form) {
            try {
                if (form.requestSubmit) form.requestSubmit();
                else form.submit();
                return 'submitted';
            } catch (_) {}
        }
        return 'enter';
    """
    try:
        return str(driver.execute_script(script, labels))
    except WebDriverException:
        return "error"


def _fill_login_credentials_in_context(driver: webdriver.Chrome, username: str, password: str) -> str:
    script = """
        const username = arguments[0];
        const password = arguments[1];
        function collect(root) {
            const found = Array.from(root.querySelectorAll('input'));
            for (const node of Array.from(root.querySelectorAll('*'))) {
                if (node.shadowRoot) found.push(...collect(node.shadowRoot));
            }
            return found;
        }
        function visible(node) {
            const style = window.getComputedStyle(node);
            const rect = node.getBoundingClientRect();
            return style.visibility !== 'hidden' && style.display !== 'none'
                && rect.width > 0 && rect.height > 0 && node.getAttribute('aria-hidden') !== 'true';
        }
        function setValue(node, value) {
            const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
            setter.call(node, value);
            node.dispatchEvent(new Event('input', {bubbles: true}));
            node.dispatchEvent(new Event('change', {bubbles: true}));
            node.dispatchEvent(new Event('blur', {bubbles: true}));
        }
        function fieldText(node) {
            return [
                node.getAttribute('autocomplete'),
                node.getAttribute('aria-label'),
                node.getAttribute('placeholder'),
                node.getAttribute('name'),
                node.getAttribute('id'),
            ].filter(Boolean).join(' ').toLowerCase();
        }
        const inputs = collect(document).filter((node) => visible(node) && !node.disabled && node.type !== 'hidden');
        const passwordFields = inputs.filter((node) => (node.getAttribute('type') || '').toLowerCase() === 'password');
        if (!passwordFields.length) return 'no_password';
        const passwordField = passwordFields[0];
        const textInputs = inputs.filter((node) => {
            const type = (node.getAttribute('type') || 'text').toLowerCase();
            return !['password', 'checkbox', 'radio', 'submit', 'button'].includes(type);
        });
        let usernameField = textInputs.find((node) => {
            const text = fieldText(node);
            return /(user|username|user id|login|email|online id)/.test(text);
        });
        if (!usernameField && passwordField.form) {
            usernameField = textInputs.filter((node) => node.form === passwordField.form).at(-1);
        }
        if (!usernameField) usernameField = textInputs.at(-1);
        if (!usernameField) return 'no_username';
        setValue(usernameField, username);
        setValue(passwordField, password);
        return 'filled';
    """
    try:
        return str(driver.execute_script(script, username, password))
    except WebDriverException:
        return "error"


def _focus_login_field_in_context(driver: webdriver.Chrome, prefer_password: bool = False) -> str:
    script = """
        const preferPassword = arguments[0];
        function collect(root) {
            const found = Array.from(root.querySelectorAll('input'));
            for (const node of Array.from(root.querySelectorAll('*'))) {
                if (node.shadowRoot) found.push(...collect(node.shadowRoot));
            }
            return found;
        }
        function visible(node) {
            const style = window.getComputedStyle(node);
            const rect = node.getBoundingClientRect();
            return style.visibility !== 'hidden' && style.display !== 'none'
                && rect.width > 0 && rect.height > 0 && node.getAttribute('aria-hidden') !== 'true';
        }
        function fieldText(node) {
            return [
                node.getAttribute('autocomplete'),
                node.getAttribute('aria-label'),
                node.getAttribute('placeholder'),
                node.getAttribute('name'),
                node.getAttribute('id'),
            ].filter(Boolean).join(' ').toLowerCase();
        }
        const inputs = collect(document).filter((node) => visible(node) && !node.disabled && node.type !== 'hidden');
        const textInputs = inputs.filter((node) => {
            const type = (node.getAttribute('type') || 'text').toLowerCase();
            return !['password', 'checkbox', 'radio', 'submit', 'button'].includes(type);
        });
        const passwordField = inputs.find((node) => (node.getAttribute('type') || '').toLowerCase() === 'password');
        let usernameField = textInputs.find((node) => {
            const text = fieldText(node);
            return /(user|username|user id|login|email|online id)/.test(text);
        });
        if (!usernameField && passwordField && passwordField.form) {
            usernameField = textInputs.filter((node) => node.form === passwordField.form).at(-1);
        }
        if (!usernameField) usernameField = textInputs.at(-1);
        const target = (preferPassword || (usernameField && usernameField.value && passwordField && !passwordField.value))
            ? (passwordField || usernameField)
            : (usernameField || passwordField);
        if (!target) return 'not_found';
        target.scrollIntoView({block: 'center'});
        target.focus();
        target.click();
        return 'focused';
    """
    try:
        return str(driver.execute_script(script, prefer_password))
    except WebDriverException:
        return "error"


def dispatch_browser_key(driver: webdriver.Chrome, key: str, code: str, key_code: int) -> None:
    for event_type in ["rawKeyDown", "keyUp"]:
        driver.execute_cdp_cmd(
            "Input.dispatchKeyEvent",
            {
                "type": event_type,
                "key": key,
                "code": code,
                "windowsVirtualKeyCode": key_code,
                "nativeVirtualKeyCode": key_code,
            },
        )


def choose_chrome_password_suggestion(driver: webdriver.Chrome) -> bool:
    def press_suggestion_keys() -> bool:
        try:
            time.sleep(0.9)
            dispatch_browser_key(driver, "ArrowDown", "ArrowDown", 40)
            time.sleep(0.2)
            dispatch_browser_key(driver, "Enter", "Enter", 13)
            time.sleep(1.3)
            return True
        except WebDriverException:
            try:
                driver.switch_to.active_element.send_keys(Keys.ARROW_DOWN)
                driver.switch_to.active_element.send_keys(Keys.ENTER)
                time.sleep(1.3)
                return True
            except WebDriverException:
                return False

    def try_context() -> bool:
        for prefer_password in [False, True]:
            if _focus_login_field_in_context(driver, prefer_password=prefer_password) == "focused" and press_suggestion_keys():
                return True
        return False

    for attempt in range(2):
        try:
            driver.switch_to.default_content()
        except WebDriverException:
            pass
        if try_context():
            return True
        try:
            frames = driver.find_elements(By.CSS_SELECTOR, "iframe, frame")
        except WebDriverException:
            frames = []
        for frame in frames:
            try:
                driver.switch_to.default_content()
                driver.switch_to.frame(frame)
            except WebDriverException:
                continue
            if try_context():
                try:
                    driver.switch_to.default_content()
                except WebDriverException:
                    pass
                return True
        try:
            driver.switch_to.default_content()
        except WebDriverException:
            pass
        if attempt == 0:
            press_suggestion_keys()
    return False


def fill_login_credentials(driver: webdriver.Chrome, credentials: tuple[str, str] | None) -> str:
    if not credentials:
        return "missing"
    username, password = credentials
    try:
        driver.switch_to.default_content()
    except WebDriverException:
        pass
    action = _fill_login_credentials_in_context(driver, username, password)
    if action == "filled":
        return action
    try:
        frames = driver.find_elements(By.CSS_SELECTOR, "iframe, frame")
    except WebDriverException:
        frames = []
    for frame in frames:
        try:
            driver.switch_to.default_content()
            driver.switch_to.frame(frame)
        except WebDriverException:
            continue
        frame_action = _fill_login_credentials_in_context(driver, username, password)
        if frame_action == "filled":
            try:
                driver.switch_to.default_content()
            except WebDriverException:
                pass
            return frame_action
    try:
        driver.switch_to.default_content()
        active = driver.switch_to.active_element
        active.send_keys(username)
        active.send_keys(Keys.TAB)
        time.sleep(0.2)
        driver.switch_to.active_element.send_keys(password)
        return "keyboard"
    except WebDriverException:
        return action
    finally:
        try:
            driver.switch_to.default_content()
        except WebDriverException:
            pass


def submit_autofilled_login_controls(driver: webdriver.Chrome, labels: list[str]) -> str:
    try:
        driver.switch_to.default_content()
    except WebDriverException:
        pass
    action = _submit_autofilled_login_in_context(driver, labels)
    if action not in {"no_password", "error"}:
        try:
            driver.switch_to.default_content()
        except WebDriverException:
            pass
        return action
    try:
        frames = driver.find_elements(By.CSS_SELECTOR, "iframe, frame")
    except WebDriverException:
        frames = []
    for frame in frames:
        try:
            driver.switch_to.default_content()
            driver.switch_to.frame(frame)
        except WebDriverException:
            continue
        frame_action = _submit_autofilled_login_in_context(driver, labels)
        if frame_action not in {"no_password", "error"}:
            try:
                driver.switch_to.default_content()
            except WebDriverException:
                pass
            return frame_action
    try:
        driver.switch_to.default_content()
    except WebDriverException:
        pass
    return action


def submit_autofilled_login(
    driver: webdriver.Chrome,
    logged_in,
    needs_login,
    needs_user_action,
    wait_seconds: int = 40,
    institution: str | None = None,
) -> str | None:
    labels = ["sign in", "signin", "log in", "login", "continue", "submit"]
    credentials = login_credentials(institution)
    if logged_in(driver):
        return None
    deadline = time.time() + wait_seconds
    reveal_clicked = False
    fill_attempted = False
    password_suggestion_attempts = 0
    while time.time() < deadline:
        wait_ready(driver, timeout=3)
        if logged_in(driver):
            return None
        if needs_user_action(driver):
            return STATUS_WAITING_FOR_USER_ACTION

        if credentials and not fill_attempted:
            fill_attempted = fill_login_credentials(driver, credentials) in {"filled", "keyboard"}

        action = submit_autofilled_login_controls(driver, labels)
        if action == "no_password":
            if credentials and fill_attempted:
                try:
                    driver.switch_to.default_content()
                    driver.switch_to.active_element.send_keys(Keys.ENTER)
                    action = "enter"
                except WebDriverException:
                    pass
            if action == "enter":
                post_submit_deadline = min(time.time() + 12, deadline)
                while time.time() < post_submit_deadline:
                    wait_ready(driver, timeout=3)
                    if logged_in(driver):
                        return None
                    if needs_user_action(driver):
                        return STATUS_WAITING_FOR_USER_ACTION
                time.sleep(1)
                continue
            if not credentials and password_suggestion_attempts < 3:
                password_suggestion_attempts += 1
                choose_chrome_password_suggestion(driver)
                continue
            if not reveal_clicked:
                reveal_clicked = click_by_text(driver, ["sign in", "signin", "log in", "login"], exact=False)
                if reveal_clicked:
                    time.sleep(2)
                    continue
            if not needs_login(driver):
                return None
            time.sleep(1)
            continue
        if action == "empty_password":
            if not credentials and password_suggestion_attempts < 3:
                password_suggestion_attempts += 1
                choose_chrome_password_suggestion(driver)
                continue
            time.sleep(1)
            continue
        if action in {"clicked", "submitted", "enter"}:
            post_submit_deadline = min(time.time() + 12, deadline)
            while time.time() < post_submit_deadline:
                wait_ready(driver, timeout=3)
                if logged_in(driver):
                    return None
                if needs_user_action(driver):
                    return STATUS_WAITING_FOR_USER_ACTION
                time.sleep(1)
            continue
        time.sleep(1)
    if needs_user_action(driver):
        return STATUS_WAITING_FOR_USER_ACTION
    return STATUS_WAITING_FOR_LOGIN if needs_login(driver) else None


def submit_login_if_autofilled(driver: webdriver.Chrome) -> str | None:
    return submit_autofilled_login(
        driver,
        is_logged_in,
        page_needs_login,
        page_needs_user_action,
        wait_seconds=45,
        institution="citizens",
    )


def parse_transaction_date(text: str) -> str | None:
    match = re.search(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b", text)
    if not match:
        return None
    year = int(match.group(3) or datetime.now().year)
    if year < 100:
        year += 2000
    try:
        parsed = datetime(year, int(match.group(1)), int(match.group(2))).date()
    except ValueError:
        return None
    if not match.group(3) and parsed > datetime.now().date():
        return parsed.replace(year=parsed.year - 1).isoformat()
    return parsed.isoformat()


def parse_named_transaction_date(text: str) -> str | None:
    match = re.search(
        r"\b(january|february|march|april|may|june|july|august|september|october|november|december)\s+\d{1,2},\s*\d{4}\b",
        text,
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    try:
        return datetime.strptime(match.group(0), "%B %d, %Y").date().isoformat()
    except ValueError:
        return None


def parse_money_values(text: str) -> list[float]:
    values: list[float] = []
    text = normalize_space(text).replace("\u2212", "-").replace("\u2013", "-").replace("\u2014", "-")
    pattern = r"\(?\s*[+-]?\s*\$?\s*(?:\d{1,3}(?:,\d{3})*|\d+)\s*\.\s*\d{2}\s*\)?"
    for match in re.finditer(pattern, text):
        raw = match.group(0).strip()
        value = parse_visible_money(raw)
        if value is None:
            continue
        values.append(value)
    return values


def infer_amount(cells: list[str], raw_text: str) -> tuple[float | None, str]:
    lowered = raw_text.lower()
    for cell in cells:
        text = cell.lower()
        values = parse_money_values(cell)
        if not values:
            continue
        if "withdrawal" in text or "debit" in text:
            return -abs(values[-1]), "debit"
        if "deposit" in text or "credit" in text:
            return abs(values[-1]), "credit"
    values = parse_money_values(raw_text)
    if not values:
        return None, "debit"
    amount = values[-1]
    if amount > 0 and any(marker in lowered for marker in ["deposit", "credit", "payroll", "refund", "interest", "transfer from"]):
        return abs(amount), "credit"
    return (-abs(amount) if amount > 0 else amount), "debit" if amount <= 0 else "credit"


def infer_description(cells: list[str], raw_text: str) -> str:
    for cell in cells:
        text = normalize_space(cell)
        if text and not parse_transaction_date(text) and not parse_money_values(text):
            lowered = text.lower()
            if lowered not in {"date", "description", "amount", "balance", "pending", "posted"}:
                return text[:300]
    cleaned = re.sub(r"\$?\s*-?\d[\d,]*(?:\.\d{2})?", " ", raw_text)
    cleaned = re.sub(r"\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b", " ", cleaned)
    return normalize_space(cleaned)[:300] or "Citizens transaction"


def normalized_csv_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def parse_visible_money(value: str | None) -> float | None:
    text = normalize_space(value).replace("\u2212", "-").replace("\u2013", "-").replace("\u2014", "-")
    match = re.search(r"\(?\s*[+-]?\s*\$?\s*(\d{1,3}(?:,\d{3})*|\d+)\s*\.\s*(\d{2})\s*\)?", text)
    if not match:
        return parse_money(value)
    raw = match.group(0)
    number = float(f"{match.group(1).replace(',', '')}.{match.group(2)}")
    if "-" in raw or (raw.strip().startswith("(") and raw.strip().endswith(")")):
        return -number
    return number


def csv_field(row: dict[str, str], names: list[str]) -> str:
    normalized = {normalized_csv_key(key): value for key, value in row.items()}
    for name in names:
        value = normalized.get(normalized_csv_key(name))
        if value:
            return normalize_space(value)
    return ""


def parse_exported_transactions(path: Path) -> list[dict[str, Any]]:
    text = ""
    for encoding in ["utf-8-sig", "utf-8", "cp1252"]:
        try:
            text = path.read_text(encoding=encoding)
            break
        except UnicodeDecodeError:
            continue
    if not text:
        return []
    try:
        dialect = csv.Sniffer().sniff(text[:2048])
    except csv.Error:
        dialect = csv.excel
    rows = csv.DictReader(io.StringIO(text), dialect=dialect)
    parsed = []
    for row in rows:
        tx_date = parse_transaction_date(csv_field(row, ["Date", "Transaction Date", "Posted Date", "Posting Date"]))
        description = csv_field(row, ["Description", "Payee", "Memo", "Transaction"])
        debit = parse_money(csv_field(row, ["Debit", "Withdrawal"]))
        credit = parse_money(csv_field(row, ["Credit", "Deposit"]))
        amount = parse_money(csv_field(row, ["Amount"]))
        debit_credit = "credit" if amount and amount > 0 else "debit"
        if debit is not None:
            amount = -abs(debit)
            debit_credit = "debit"
        elif credit is not None:
            amount = abs(credit)
            debit_credit = "credit"
        if not tx_date or not description or amount is None:
            continue
        parsed.append(
            {
                "transaction_date": tx_date,
                "posted_date": tx_date,
                "description": description[:300],
                "amount": amount,
                "debit_credit": debit_credit,
                "source_file": "sync:citizens",
            }
        )
    return parsed


def candidate_transaction_rows(driver: webdriver.Chrome) -> list[Any]:
    selectors = [
        "table tbody tr",
        "[role='row']",
        "[data-testid*='transaction' i]",
        "[class*='transaction' i]",
        "[class*='activity' i] li",
        "[class*='activity' i] [class*='row' i]",
    ]
    rows: list[Any] = []
    seen: set[str] = set()
    for selector in selectors:
        try:
            elements = driver.find_elements(By.CSS_SELECTOR, selector)
        except WebDriverException:
            continue
        for element in elements:
            try:
                text = normalize_space(element.text)
            except StaleElementReferenceException:
                continue
            if text and text not in seen:
                seen.add(text)
                rows.append(element)
    return rows


def scrape_visible_transactions(driver: webdriver.Chrome) -> list[dict[str, Any]]:
    parsed: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in candidate_transaction_rows(driver):
        try:
            raw_text = normalize_space(row.text)
            cells = [
                normalize_space(cell.text)
                for cell in row.find_elements(By.CSS_SELECTOR, "td, th, [role='cell'], [role='gridcell']")
                if normalize_space(cell.text)
            ]
        except (StaleElementReferenceException, WebDriverException):
            continue
        lowered = raw_text.lower()
        if not raw_text or any(skip in lowered for skip in ["statement", "document center", "available balance", "current balance"]):
            continue
        tx_date = parse_transaction_date(raw_text) or parse_named_transaction_date(raw_text)
        if not tx_date or not parse_money_values(raw_text):
            continue
        amount, debit_credit = infer_amount(cells, raw_text)
        if amount is None:
            continue
        key = f"{tx_date}|{raw_text}"
        if key in seen:
            continue
        seen.add(key)
        parsed.append(
            {
                "transaction_date": tx_date,
                "posted_date": tx_date,
                "description": infer_description(cells, raw_text),
                "amount": amount,
                "debit_credit": debit_credit,
                "source_file": "sync:citizens",
            }
        )
    text_transactions = scrape_transactions_from_visible_text(driver, [])
    if len(text_transactions) >= len(parsed):
        return text_transactions
    if len(parsed) < 5:
        parsed.extend(scrape_transactions_from_visible_text(driver, parsed))
    return parsed


def scrape_transactions_from_visible_text(
    driver: webdriver.Chrome,
    existing: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    text = visible_text(driver)
    lines = [normalize_space(line) for line in text.splitlines()]
    lines = [line for line in lines if line]
    current_date: str | None = None
    description_parts: list[str] = []
    waiting_for_balance = False
    transactions: list[dict[str, Any]] = []
    seen = {
        f"{row.get('transaction_date')}|{normalize_space(row.get('description'))}|{float(row.get('amount', 0)):.2f}"
        for row in existing or []
    }
    skip_lines = {
        "transactions",
        "account details",
        "account services",
        "filter",
        "export",
        "view",
        "show more",
        "available balance:",
        "account overview",
        "back to account summary",
    }
    for line in lines:
        parsed_date = parse_named_transaction_date(line) or parse_transaction_date(line)
        if parsed_date:
            current_date = parsed_date
            description_parts = []
            waiting_for_balance = False
            continue
        if not current_date:
            continue
        lowered = line.lower().strip()
        values = parse_money_values(line)
        if waiting_for_balance:
            if values:
                waiting_for_balance = False
            continue
        if values:
            if not description_parts:
                continue
            amount = values[0]
            description = normalize_space(" ".join(description_parts))[:300]
            key = f"{current_date}|{description}|{amount:.2f}"
            if key not in seen:
                seen.add(key)
                transactions.append(
                    {
                        "transaction_date": current_date,
                        "posted_date": current_date,
                        "description": description,
                        "amount": amount,
                        "debit_credit": "credit" if amount > 0 else "debit",
                        "source_file": "sync:citizens",
                    }
                )
            description_parts = []
            waiting_for_balance = True
            continue
        if lowered in skip_lines or lowered.endswith(":"):
            continue
        if "balance explanation" in lowered or "citizens" in lowered and len(line) > 80:
            continue
        description_parts.append(line)
    return transactions


def first_visible_money_text(driver: webdriver.Chrome, selectors: list[str]) -> str | None:
    for selector in selectors:
        try:
            elements = driver.find_elements(By.CSS_SELECTOR, selector)
        except WebDriverException:
            continue
        for element in elements:
            try:
                text = normalize_space(element.text)
                if "$" in text:
                    return text
            except StaleElementReferenceException:
                continue
    return None


def scrape_current_checking_balance(driver: webdriver.Chrome) -> str | None:
    selectors = [
        "#product-group-checking-accounts .olb-c-accountSummary__aggregatedBalance",
        "#account-list-checking-accounts .olb-c-accountItem__balance",
        "[data-testid*='balance' i]",
        "[class*='balance' i]",
        "[aria-label*='balance' i]",
        "[id*='balance' i]",
    ]
    return first_visible_money_text(driver, selectors)


def click_checking_account(driver: webdriver.Chrome) -> bool:
    script = """
        const nodes = Array.from(document.querySelectorAll('a, button, [role="button"]'));
        function visible(node) {
            const style = window.getComputedStyle(node);
            const rect = node.getBoundingClientRect();
            return style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0;
        }
        const node = nodes.find((item) => {
            const text = (item.innerText || item.textContent || '').replace(/\\s+/g, ' ').trim().toLowerCase();
            return visible(item) && text.includes('checking') && !text.includes('savings')
                && !text.includes('statement') && !text.includes('document center') && !text.includes('manage card');
        });
        if (!node) return false;
        node.scrollIntoView({block: 'center'});
        node.click();
        return true;
    """
    try:
        clicked = bool(driver.execute_script(script))
    except WebDriverException:
        clicked = False
    if clicked:
        wait_ready(driver, timeout=15)
    return clicked


def navigate_to_checking(driver: webdriver.Chrome) -> None:
    safe_get(driver, CITIZENS_HOME_URL)
    deadline = time.time() + 30
    while time.time() < deadline:
        body = visible_text(driver).lower()
        if "checking accounts" in body or "available balance" in body:
            break
        time.sleep(1)
    click_deadline = time.time() + 30
    while time.time() < click_deadline:
        if "transactions" in driver.current_url.lower() and "export" in visible_text(driver).lower():
            break
        if click_checking_account(driver):
            time.sleep(3)
            if "transactions" in driver.current_url.lower() or "export" in visible_text(driver).lower():
                break
        time.sleep(1)
    for label in ["transactions", "recent transactions", "recent activity", "account activity", "activity"]:
        if click_by_text(driver, [label], exact=True):
            break
    deadline = time.time() + 25
    while time.time() < deadline:
        body = visible_text(driver).lower()
        if "export" in body and (parse_named_transaction_date(body) or parse_transaction_date(body)):
            return
        time.sleep(1)


def newest_download(known_files: set[Path], started_at: float) -> Path | None:
    deadline = time.time() + 45
    while time.time() < deadline:
        partials = list(DOWNLOAD_DIR.glob("*.crdownload")) + list(DOWNLOAD_DIR.glob("*.tmp"))
        candidates = [
            path
            for path in DOWNLOAD_DIR.iterdir()
            if path.is_file() and path not in known_files and path.stat().st_mtime >= started_at - 1
        ]
        if candidates and not partials:
            return max(candidates, key=lambda path: path.stat().st_mtime)
        time.sleep(1)
    return None


def export_transactions(driver: webdriver.Chrome) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    known_files = {path for path in DOWNLOAD_DIR.iterdir() if path.is_file()}
    started_at = time.time()
    if not click_by_text(driver, ["export"], exact=True):
        return None, []
    time.sleep(0.5)
    if not click_by_text(driver, ["comma delimited", "csv"]):
        return None, []
    downloaded = newest_download(known_files, started_at)
    if not downloaded:
        return None, []
    digest = hashlib.sha256(downloaded.read_bytes()).hexdigest()
    metadata = {"format": "comma_delimited", "local_path": str(downloaded), "file_hash": digest, "downloaded_at": utc_now()}
    return metadata, parse_exported_transactions(downloaded)


def record_balance(raw_text: str) -> dict[str, Any] | None:
    amount = parse_visible_money(raw_text)
    if amount is None:
        return None
    captured_at = utc_now()
    with connection() as conn:
        account = account_id(conn, CITIZENS_SITE)
        conn.execute(
            """
            UPDATE accounts
            SET name = ?, account_type = ?
            WHERE id = ?
            """,
            (CITIZENS_NAME, CITIZENS_ACCOUNT_TYPE, account),
        )
        conn.execute(
            """
            INSERT INTO sync_balances (
                account_id, site, account_name, account_type, balance_type,
                amount, currency, captured_at, source, raw_text
            )
            VALUES (?, ?, ?, ?, 'current', ?, 'USD', ?, ?, ?)
            """,
            (account, CITIZENS_SITE, CITIZENS_NAME, CITIZENS_ACCOUNT_TYPE, amount, captured_at, "Citizens online banking", raw_text[:500]),
        )
    return {"account_name": CITIZENS_NAME, "amount": amount, "captured_at": captured_at, "source": "Citizens online banking"}


def transaction_hash(row: dict[str, Any]) -> str:
    key = "|".join(
        [
            CITIZENS_SITE,
            row.get("transaction_date") or "",
            row.get("posted_date") or "",
            row.get("description") or "",
            f"{row.get('amount', 0):.2f}",
            "sync:citizens",
        ]
    )
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def record_transaction(row: dict[str, Any]) -> bool:
    amount = float(row["amount"])
    with connection() as conn:
        account = account_id(conn, CITIZENS_SITE)
        duplicate = conn.execute(
            """
            SELECT id FROM transactions
            WHERE account_id = ?
              AND transaction_date = ?
              AND description = ?
              AND ABS(amount - ?) < 0.001
            LIMIT 1
            """,
            (account, row["transaction_date"], row["description"], amount),
        ).fetchone()
        if duplicate:
            return False
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO transactions (
                account_id, site, transaction_date, posted_date, description, amount,
                debit_credit, category, source_file, source_hash, imported_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                account,
                CITIZENS_SITE,
                row["transaction_date"],
                row.get("posted_date"),
                row["description"],
                amount,
                row["debit_credit"],
                classify(row["description"], None, amount, CITIZENS_SITE),
                "sync:citizens",
                transaction_hash(row),
                utc_now(),
            ),
        )
    return cursor.rowcount > 0


def amex_logged_in(driver: webdriver.Chrome) -> bool:
    text = visible_text(driver).lower()
    if "american express gold card" in text and any(term in text for term in ["accounts", "total balance", "membership rewards"]):
        return True
    return "since last statement" in text and "total balance" in text


def amex_needs_login(driver: webdriver.Chrome) -> bool:
    text = visible_text(driver).lower()
    url = ""
    try:
        url = driver.current_url.lower()
    except WebDriverException:
        pass
    if amex_logged_in(driver):
        return False
    return "login" in url or "log in" in text or "user id" in text or "password" in text


def amex_needs_user_action(driver: webdriver.Chrome) -> bool:
    text = visible_text(driver).lower()
    return any(
        term in text
        for term in [
            "verification code",
            "security code",
            "one-time",
            "one time",
            "verify your identity",
            "captcha",
            "confirm your identity",
        ]
    )


def submit_amex_login_if_autofilled(driver: webdriver.Chrome) -> str | None:
    return submit_autofilled_login(
        driver,
        amex_logged_in,
        amex_needs_login,
        amex_needs_user_action,
        wait_seconds=55,
        institution="amex",
    )


def wait_for_amex_dashboard(driver: webdriver.Chrome, timeout: int = 45) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        wait_ready(driver, timeout=5)
        if amex_logged_in(driver):
            return True
        if amex_needs_user_action(driver):
            return False
        time.sleep(1)
    return amex_logged_in(driver)


def parse_month_day_date(text: str) -> str | None:
    match = re.search(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+(\d{1,2})\b", text, flags=re.IGNORECASE)
    if not match:
        return None
    month_name = "sep" if match.group(1).lower() == "sept" else match.group(1).lower()
    month = datetime.strptime(month_name[:3], "%b").month
    year = datetime.now().year
    try:
        parsed = datetime(year, month, int(match.group(2))).date()
    except ValueError:
        return None
    if parsed > datetime.now().date():
        parsed = parsed.replace(year=year - 1)
    return parsed.isoformat()


def text_lines(driver: webdriver.Chrome) -> list[str]:
    return [normalize_space(line) for line in visible_text(driver).splitlines() if normalize_space(line)]


def money_after_label(lines: list[str], label: str) -> float | None:
    target = label.lower()
    for index, line in enumerate(lines):
        if target not in line.lower():
            continue
        window = " ".join(lines[index:index + 4])
        values = parse_money_values(window)
        if values:
            return values[0]
    return None


def membership_points_from_lines(lines: list[str]) -> int | None:
    def point_number(value: str) -> int | None:
        match = re.search(r"\b\d{1,3}(?:,\d{3})+\b", value)
        return int(match.group(0).replace(",", "")) if match else None

    for index, line in enumerate(lines):
        lowered = line.lower()
        if lowered.startswith("membership rewards") and "points" in lowered and len(line) < 80:
            for candidate in lines[index:index + 4]:
                value = point_number(candidate)
                if value is not None:
                    return value
            for candidate in reversed(lines[max(0, index - 3):index]):
                value = point_number(candidate)
                if value is not None:
                    return value

    for index, line in enumerate(lines):
        lowered = line.lower()
        if lowered == "points" or lowered.endswith(" points"):
            nearby = lines[max(0, index - 2):index + 2]
            if any("membership rewards" in item.lower() for item in nearby):
                for candidate in reversed(lines[max(0, index - 3):index + 1]):
                    value = point_number(candidate)
                    if value is not None:
                        return value
    for index, line in enumerate(lines):
        if "membership rewards" not in line.lower():
            continue
        if len(line) >= 80:
            continue
        for candidate in reversed(lines[max(0, index - 3):index + 1]):
            value = point_number(candidate)
            if value is not None:
                return value
    return None


def click_amex_gold_card(driver: webdriver.Chrome) -> bool:
    script = """
        const nodes = Array.from(document.querySelectorAll('a, button, [role="button"], [tabindex]'));
        function visible(node) {
            const style = window.getComputedStyle(node);
            const rect = node.getBoundingClientRect();
            return style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 20 && rect.height > 20;
        }
        const candidates = nodes
            .map((node) => ({node, text: (node.innerText || node.textContent || '').replace(/\\s+/g, ' ').trim().toLowerCase()}))
            .filter((item) => visible(item.node) && item.text.includes('american express gold card') && item.text.includes('total balance'));
        candidates.sort((a, b) => {
            const score = (item) => item.node.getBoundingClientRect().width / 10000;
            return score(a) - score(b);
        });
        for (const item of candidates) {
            const clickable = item.node;
            clickable.scrollIntoView({block: 'center'});
            clickable.click();
            return true;
        }
        return false;
    """
    try:
        clicked = bool(driver.execute_script(script))
    except WebDriverException:
        clicked = False
    if clicked:
        wait_ready(driver, timeout=15)
        time.sleep(2)
    return clicked


def click_amex_view_all(driver: webdriver.Chrome) -> bool:
    script = """
        const nodes = Array.from(document.querySelectorAll('a, button, [role="button"], [tabindex]'));
        function visible(node) {
            const style = window.getComputedStyle(node);
            const rect = node.getBoundingClientRect();
            return style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0;
        }
        const viewAll = nodes.find((node) => visible(node) && (
            (node.getAttribute('aria-label') || '').toLowerCase().includes('view all recent transactions') ||
            (node.href || '').toLowerCase().includes('/activity/recent')
        ));
        if (!viewAll) return false;
        viewAll.scrollIntoView({block: 'center'});
        viewAll.click();
        return true;
    """
    try:
        clicked = bool(driver.execute_script(script))
    except WebDriverException:
        clicked = False
    if clicked:
        wait_ready(driver, timeout=20)
        time.sleep(2)
    return clicked


def wait_for_amex_transactions_page(driver: webdriver.Chrome, timeout: int = 30) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        body = visible_text(driver).lower()
        if "pending charges" in body and "posted charges" in body and "transactions" in body:
            return True
        time.sleep(1)
    return False


def amex_dashboard_amount(display_amount: float) -> tuple[float, str]:
    amount = abs(display_amount) if display_amount < 0 else -abs(display_amount)
    return amount, "credit" if amount > 0 else "debit"


def amex_transaction_hash(row: dict[str, Any]) -> str:
    key = "|".join(
        [
            AMEX_SITE,
            row.get("transaction_date") or "",
            row.get("posted_date") or "",
            row.get("description") or "",
            f"{row.get('amount', 0):.2f}",
            "sync:amex",
        ]
    )
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def infer_amex_description(cells: list[str], raw_text: str) -> str:
    ignored = {"date", "status", "description", "amount", "pending", "processing", "credit", "4x points", "1x points"}
    parts = []
    for cell in cells:
        text = normalize_space(cell)
        if (
            not text
            or parse_month_day_date(text)
            or parse_money_values(text)
            or text.lower() in ignored
            or re.fullmatch(r"[A-Z]?\d{12,}", text)
        ):
            continue
        parts.append(text)
    if parts:
        return normalize_space(" ".join(parts))[:300]
    cleaned = re.sub(r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+\d{1,2}\b", " ", raw_text, flags=re.IGNORECASE)
    cleaned = re.sub(r"\(?\s*[+-]?\s*\$?\s*(?:\d{1,3}(?:,\d{3})*|\d+)\s*\.\s*\d{2}\s*\)?", " ", cleaned)
    cleaned = re.sub(r"\b(?:pending|processing|credit|4x points|1x points)\b", " ", cleaned, flags=re.IGNORECASE)
    return normalize_space(cleaned)[:300] or "Amex transaction"


def scrape_amex_transactions_from_dom(driver: webdriver.Chrome) -> list[dict[str, Any]]:
    parsed: list[dict[str, Any]] = []
    seen: set[str] = set()
    selectors = ["table tbody tr", "[role='row']", "[class*='transaction' i]"]
    for selector in selectors:
        try:
            elements = driver.find_elements(By.CSS_SELECTOR, selector)
        except WebDriverException:
            continue
        for element in elements:
            try:
                raw_text = normalize_space(element.text)
                cells = [
                    normalize_space(cell.text)
                    for cell in element.find_elements(By.CSS_SELECTOR, "td, th, [role='cell'], [role='gridcell']")
                    if normalize_space(cell.text)
                ]
            except (StaleElementReferenceException, WebDriverException):
                continue
            if not raw_text or "select all" in raw_text.lower():
                continue
            tx_date = parse_month_day_date(raw_text) or parse_named_transaction_date(raw_text)
            values = parse_money_values(raw_text)
            if not tx_date or not values:
                continue
            amount, debit_credit = amex_dashboard_amount(values[-1])
            description = infer_amex_description(cells, raw_text)
            key = f"{tx_date}|{description}|{amount:.2f}"
            if key in seen:
                continue
            seen.add(key)
            parsed.append(
                {
                    "transaction_date": tx_date,
                    "posted_date": tx_date,
                    "description": description,
                    "amount": amount,
                    "debit_credit": debit_credit,
                    "source_file": "sync:amex",
                }
            )
        if parsed:
            return parsed
    return parsed


def scrape_amex_transactions_from_text(driver: webdriver.Chrome) -> list[dict[str, Any]]:
    lines = text_lines(driver)
    start = 0
    for index, line in enumerate(lines):
        if line.lower() == "date" and index + 3 < len(lines) and lines[index + 1].lower() == "status":
            start = index + 4
            break
        if line.lower().startswith("select all on page"):
            start = index + 1
    status_terms = {"pending", "processing", "credit", "4x points", "3x points", "2x points", "1x points"}
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    index = start
    while index < len(lines):
        tx_date = parse_month_day_date(lines[index])
        if not tx_date:
            index += 1
            continue
        index += 1
        parts: list[str] = []
        while index < len(lines):
            if parse_month_day_date(lines[index]):
                break
            values = parse_money_values(lines[index])
            if values:
                if parts:
                    if parts and parts[0].lower() in status_terms:
                        parts = parts[1:]
                    description = normalize_space(" ".join(parts))[:300]
                    amount, debit_credit = amex_dashboard_amount(values[-1])
                    key = f"{tx_date}|{description}|{amount:.2f}"
                    if description and key not in seen:
                        seen.add(key)
                        rows.append(
                            {
                                "transaction_date": tx_date,
                                "posted_date": tx_date,
                                "description": description,
                                "amount": amount,
                                "debit_credit": debit_credit,
                                "source_file": "sync:amex",
                            }
                        )
                index += 1
                break
            lowered = lines[index].lower()
            if lowered not in {"transactions", "sort by most recent", "filter", "download", "print", "tag"}:
                parts.append(lines[index])
            index += 1
    return rows


def scrape_amex_transactions(driver: webdriver.Chrome) -> list[dict[str, Any]]:
    dom_rows = scrape_amex_transactions_from_dom(driver)
    text_rows = scrape_amex_transactions_from_text(driver)
    return text_rows if len(text_rows) >= len(dom_rows) else dom_rows


def record_amex_balance(total_balance: float, pending_charges: float | None, membership_points: int | None) -> dict[str, Any]:
    pending = abs(float(pending_charges or 0))
    computed = float(total_balance) + pending
    balance_type = "liability" if computed >= 0 else "asset"
    amount = abs(round(computed, 2))
    captured_at = utc_now()
    raw_text = json.dumps(
        {
            "total_balance": round(float(total_balance), 2),
            "pending_charges": round(pending, 2),
            "computed_net": round(computed, 2),
            "membership_points": membership_points,
        },
        sort_keys=True,
    )
    with connection() as conn:
        account = account_id(conn, AMEX_SITE)
        conn.execute(
            "UPDATE accounts SET name = ?, account_type = ? WHERE id = ?",
            (AMEX_NAME, AMEX_ACCOUNT_TYPE, account),
        )
        conn.execute(
            """
            INSERT INTO sync_balances (
                account_id, site, account_name, account_type, balance_type,
                amount, currency, captured_at, source, raw_text
            )
            VALUES (?, ?, ?, ?, ?, ?, 'USD', ?, ?, ?)
            """,
            (
                account,
                AMEX_SITE,
                AMEX_NAME,
                AMEX_ACCOUNT_TYPE,
                balance_type,
                amount,
                captured_at,
                "Amex online banking",
                raw_text,
            ),
        )
        if membership_points is not None:
            rewards_account = account_id(conn, AMEX_REWARDS_SITE)
            rewards_amount = round(membership_points * 0.01, 2)
            rewards_raw_text = json.dumps(
                {
                    "membership_points": membership_points,
                    "point_value_dollars": 0.01,
                    "estimated_value": rewards_amount,
                },
                sort_keys=True,
            )
            conn.execute(
                "UPDATE accounts SET name = ?, account_type = ? WHERE id = ?",
                (AMEX_REWARDS_NAME, AMEX_REWARDS_ACCOUNT_TYPE, rewards_account),
            )
            conn.execute(
                """
                INSERT INTO sync_balances (
                    account_id, site, account_name, account_type, balance_type,
                    amount, currency, captured_at, source, raw_text
                )
                VALUES (?, ?, ?, ?, 'asset', ?, 'USD', ?, ?, ?)
                """,
                (
                    rewards_account,
                    AMEX_REWARDS_SITE,
                    AMEX_REWARDS_NAME,
                    AMEX_REWARDS_ACCOUNT_TYPE,
                    rewards_amount,
                    captured_at,
                    "Amex Membership Rewards",
                    rewards_raw_text,
                ),
            )
    return {
        "account_name": AMEX_NAME,
        "account_type": AMEX_ACCOUNT_TYPE,
        "balance_type": balance_type,
        "amount": amount,
        "captured_at": captured_at,
        "source": "Amex online banking",
        "total_balance": round(float(total_balance), 2),
        "pending_charges": round(pending, 2),
        "membership_points": membership_points,
        "rewards_value": round((membership_points or 0) * 0.01, 2) if membership_points is not None else None,
    }


def record_amex_transaction(row: dict[str, Any]) -> bool:
    amount = float(row["amount"])
    with connection() as conn:
        account = account_id(conn, AMEX_SITE)
        conn.execute("UPDATE accounts SET name = ?, account_type = ? WHERE id = ?", (AMEX_NAME, AMEX_ACCOUNT_TYPE, account))
        duplicate = conn.execute(
            """
            SELECT id FROM transactions
            WHERE account_id = ?
              AND transaction_date = ?
              AND description = ?
              AND ABS(amount - ?) < 0.001
            LIMIT 1
            """,
            (account, row["transaction_date"], row["description"], amount),
        ).fetchone()
        if duplicate:
            return False
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO transactions (
                account_id, site, transaction_date, posted_date, description, amount,
                debit_credit, category, source_file, source_hash, imported_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                account,
                AMEX_SITE,
                row["transaction_date"],
                row.get("posted_date"),
                row["description"],
                amount,
                row["debit_credit"],
                classify(row["description"], None, amount, AMEX_SITE),
                "sync:amex",
                amex_transaction_hash(row),
                utc_now(),
            ),
        )
    return cursor.rowcount > 0


def run_amex_sync() -> dict[str, Any]:
    ensure_sync_tables()
    result = base_result("latest", "amex")
    write_json(SYNC_STATE_PATH, result)
    run_id: int | None = None
    with connection() as conn:
        cursor = conn.execute(
            """
            INSERT INTO sync_runs (institution, mode, status, started_at, message, result_json)
            VALUES ('amex', 'latest', ?, ?, ?, ?)
            """,
            (STATUS_RUNNING, result["started_at"], result["message"], json.dumps(result, sort_keys=True)),
        )
        run_id = int(cursor.lastrowid)
    driver: webdriver.Chrome | None = None
    try:
        if not chrome_debugger_available_at(AMEX_DEBUGGER_ADDRESS):
            open_amex_sync_browser()
            deadline = time.time() + 10
            while time.time() < deadline and not chrome_debugger_available_at(AMEX_DEBUGGER_ADDRESS):
                time.sleep(0.5)
        if not chrome_debugger_available_at(AMEX_DEBUGGER_ADDRESS):
            return finish_result(result, STATUS_WAITING_FOR_LOGIN, "Amex browser opened, but Chrome debugging was not ready yet. Run sync again to continue the automated login.", False)

        driver = create_amex_driver()
        move_sync_window_to_background(driver)
        safe_get(driver, AMEX_DASHBOARD_URL)
        login_status = submit_amex_login_if_autofilled(driver)
        if login_status == STATUS_WAITING_FOR_LOGIN:
            capture_screenshot(driver, result, "amex_waiting_for_login")
            return finish_result(result, STATUS_WAITING_FOR_LOGIN, "Amex login could not be submitted automatically because saved Chrome credentials or FIN_DASH_AMEX_* credentials were not available.", False)
        if login_status == STATUS_WAITING_FOR_USER_ACTION or amex_needs_user_action(driver):
            capture_screenshot(driver, result, "amex_waiting_for_security")
            return finish_result(result, STATUS_WAITING_FOR_USER_ACTION, "Amex is asking for a manual security step. Complete it in Chrome, then run sync again.", False)
        if not wait_for_amex_dashboard(driver):
            capture_screenshot(driver, result, "amex_dashboard_not_found")
            return finish_result(result, STATUS_WAITING_FOR_LOGIN, "Amex did not reach the account dashboard after the automated login submit.", False)

        dashboard_lines = text_lines(driver)
        membership_points = membership_points_from_lines(dashboard_lines)
        if "since last statement" not in visible_text(driver).lower():
            if not click_amex_gold_card(driver):
                capture_screenshot(driver, result, "amex_gold_card_click_failed")
                return finish_result(result, STATUS_FAILED, "Amex dashboard loaded, but the Gold Card tile could not be clicked.", False)
        if not wait_for_amex_dashboard(driver, timeout=20):
            capture_screenshot(driver, result, "amex_gold_card_page_not_found")
            return finish_result(result, STATUS_FAILED, "Amex Gold Card page did not load after clicking the account.", False)

        card_lines = text_lines(driver)
        membership_points = membership_points_from_lines(card_lines) or membership_points
        total_balance = money_after_label(card_lines, "Total Balance")
        visible_card_transactions = scrape_amex_transactions(driver)

        if not click_amex_view_all(driver):
            result["errors"].append("Could not click Amex View All; using visible card transactions only.")
            capture_screenshot(driver, result, "amex_view_all_click_failed")
        else:
            wait_for_amex_transactions_page(driver)

        transaction_lines = text_lines(driver)
        pending_charges = money_after_label(transaction_lines, "Pending Charges")
        posted_charges = money_after_label(transaction_lines, "Posted Charges")
        page_total_balance = money_after_label(transaction_lines, "Total Balance")
        total_balance = page_total_balance if page_total_balance is not None else total_balance
        if total_balance is None:
            capture_screenshot(driver, result, "amex_total_balance_missing")
            return finish_result(result, STATUS_FAILED, "Amex sync reached the site, but Total Balance was not detected.", False)

        balance = record_amex_balance(total_balance, pending_charges, membership_points)
        balance["posted_charges"] = round(float(posted_charges or 0), 2)
        result["data"]["balances"].append(balance)

        transactions = scrape_amex_transactions(driver)
        if not transactions:
            transactions = visible_card_transactions
        inserted = 0
        for transaction in transactions:
            if record_amex_transaction(transaction):
                inserted += 1
            result["data"]["transactions"].append(transaction)

        message = (
            f"Amex sync completed. Captured {len(transactions)} transactions, {inserted} new. "
            f"Gold Card {balance['balance_type']}: ${balance['amount']:,.2f}."
        )
        if membership_points is not None:
            message += f" Membership Rewards: {membership_points:,} points."
        if not transactions:
            result["errors"].append("Reached Amex and captured balance, but no latest transaction rows were detected.")
            capture_screenshot(driver, result, "amex_transactions_missing")
        return finish_result(result, STATUS_SUCCESS, message)
    except Exception as exc:
        result["errors"].append(str(exc))
        capture_screenshot(driver, result, "amex_sync_exception")
        return finish_result(result, STATUS_FAILED, f"Amex sync failed: {exc}", False)
    finally:
        if driver:
            try:
                driver.quit()
            except WebDriverException:
                pass
        if run_id is not None:
            final_result = result
            with connection() as conn:
                conn.execute(
                    """
                    UPDATE sync_runs
                    SET status = ?, finished_at = ?, message = ?, result_json = ?
                    WHERE id = ?
                    """,
                    (
                        final_result.get("status", STATUS_FAILED),
                        final_result.get("finished_at") or utc_now(),
                        final_result.get("message", ""),
                        json.dumps(final_result, sort_keys=True),
                        run_id,
                    ),
                )
            close_completed_sync_browser(final_result, AMEX_PROFILE_DIR, AMEX_DEBUGGING_PORT)


def run_citizens_sync() -> dict[str, Any]:
    ensure_sync_tables()
    result = base_result("latest")
    write_json(SYNC_STATE_PATH, result)
    run_id: int | None = None
    with connection() as conn:
        cursor = conn.execute(
            """
            INSERT INTO sync_runs (institution, mode, status, started_at, message, result_json)
            VALUES ('citizens', 'latest', ?, ?, ?, ?)
            """,
            (STATUS_RUNNING, result["started_at"], result["message"], json.dumps(result, sort_keys=True)),
        )
        run_id = int(cursor.lastrowid)
    driver: webdriver.Chrome | None = None
    try:
        if not chrome_debugger_available():
            open_citizens_sync_browser()
            deadline = time.time() + 10
            while time.time() < deadline and not chrome_debugger_available():
                time.sleep(0.5)
        if not chrome_debugger_available():
            return finish_result(result, STATUS_WAITING_FOR_LOGIN, "Citizens browser opened, but Chrome debugging was not ready yet. Run sync again to continue the automated login.", False)

        driver = create_driver()
        move_sync_window_to_background(driver)
        safe_get(driver, CITIZENS_HOME_URL)
        wait_for_login_or_dashboard(driver)
        login_status = submit_login_if_autofilled(driver)
        if login_status == STATUS_WAITING_FOR_LOGIN:
            return finish_result(result, STATUS_WAITING_FOR_LOGIN, "Citizens login could not be submitted automatically because saved Chrome credentials or FIN_DASH_CITIZENS_* credentials were not available.", False)
        if login_status == STATUS_WAITING_FOR_USER_ACTION or page_needs_user_action(driver):
            return finish_result(result, STATUS_WAITING_FOR_USER_ACTION, "Citizens is asking for a manual security step. Complete it in Chrome, then run sync again.", False)
        if not is_logged_in(driver):
            return finish_result(result, STATUS_WAITING_FOR_LOGIN, "Citizens did not reach the account dashboard after the automated login submit.", False)

        raw_balance = scrape_current_checking_balance(driver)
        if raw_balance:
            balance = record_balance(raw_balance)
            if balance:
                result["data"]["balances"].append(balance)
        else:
            result["errors"].append("Could not identify a Citizens checking balance on the account dashboard.")

        navigate_to_checking(driver)
        export_metadata, transactions = export_transactions(driver)
        if export_metadata:
            result["data"]["transaction_exports"].append(export_metadata)
        if not transactions:
            transactions = scrape_visible_transactions(driver)

        inserted = 0
        for transaction in transactions:
            if record_transaction(transaction):
                inserted += 1
            result["data"]["transactions"].append(transaction)

        message = f"Citizens sync completed. Captured {len(transactions)} transactions, {inserted} new."
        if result["data"]["balances"]:
            message += f" Current balance: ${result['data']['balances'][0]['amount']:,.2f}."
        if not result["data"]["balances"] and not transactions:
            result["errors"].append("Reached Citizens, but no balance or transaction rows were detected.")
            return finish_result(result, STATUS_FAILED, "Citizens sync reached the site but did not capture a balance or latest transactions.", False)
        if result["errors"]:
            message += " Some selectors may need tuning."
        return finish_result(result, STATUS_SUCCESS, message)
    except Exception as exc:
        result["errors"].append(str(exc))
        return finish_result(result, STATUS_FAILED, f"Citizens sync failed: {exc}", False)
    finally:
        if driver:
            try:
                driver.quit()
            except WebDriverException:
                pass
        if run_id is not None:
            final_result = result
            with connection() as conn:
                conn.execute(
                    """
                    UPDATE sync_runs
                    SET status = ?, finished_at = ?, message = ?, result_json = ?
                    WHERE id = ?
                    """,
                    (
                        final_result.get("status", STATUS_FAILED),
                        final_result.get("finished_at") or utc_now(),
                        final_result.get("message", ""),
                        json.dumps(final_result, sort_keys=True),
                        run_id,
                    ),
                )
            close_completed_sync_browser(final_result, PROFILE_DIR, CITIZENS_DEBUGGING_PORT)


def chase_logged_in(driver: webdriver.Chrome) -> bool:
    text = visible_text(driver).lower()
    return ("prime visa" in text and "current balance" in text) or ("see all transactions" in text and "credit cards" in text)


def chase_needs_login(driver: webdriver.Chrome) -> bool:
    if chase_logged_in(driver):
        return False
    text = visible_text(driver).lower()
    url = ""
    try:
        url = driver.current_url.lower()
    except WebDriverException:
        pass
    return "login" in text or "sign in" in text or "username" in text or "password" in text or "/auth/" in url


def chase_needs_user_action(driver: webdriver.Chrome) -> bool:
    text = visible_text(driver).lower()
    return any(
        term in text
        for term in [
            "verification code",
            "security code",
            "one-time",
            "one time",
            "verify your identity",
            "captcha",
            "confirm your identity",
            "we need to confirm",
        ]
    )


def submit_chase_login_if_autofilled(driver: webdriver.Chrome) -> str | None:
    return submit_autofilled_login(
        driver,
        chase_logged_in,
        chase_needs_login,
        chase_needs_user_action,
        wait_seconds=60,
        institution="chase",
    )


def wait_for_chase_dashboard(driver: webdriver.Chrome, timeout: int = 50) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        wait_ready(driver, timeout=5)
        if chase_logged_in(driver):
            return True
        if chase_needs_user_action(driver):
            return False
        time.sleep(1)
    return chase_logged_in(driver)


def parse_signed_integer(value: str | None) -> int | None:
    text = normalize_space(value)
    match = re.search(r"[+-]?\s*\d{1,3}(?:,\d{3})*|[+-]?\s*\d+", text)
    if not match:
        return None
    try:
        return int(match.group(0).replace(" ", "").replace(",", ""))
    except ValueError:
        return None


def money_near_label(lines: list[str], label: str) -> float | None:
    target = label.lower()
    for index, line in enumerate(lines):
        if target not in line.lower():
            continue
        for candidate in [line, *lines[max(0, index - 3):index][::-1], *lines[index + 1:index + 4]]:
            values = parse_money_values(candidate)
            if values:
                return values[-1]
    return None


def chase_rewards_points_from_lines(lines: list[str]) -> int | None:
    for index, line in enumerate(lines):
        if "amazon rewards points" not in line.lower():
            continue
        candidates = [line, *lines[max(0, index - 4):index][::-1], *lines[index + 1:index + 3]]
        for candidate in candidates:
            value = parse_signed_integer(candidate)
            if value is not None:
                return value
    for index, line in enumerate(lines):
        if normalize_space(line).lower() == "rewards":
            for candidate in lines[index:index + 6]:
                value = parse_signed_integer(candidate)
                if value is not None:
                    return value
    return None


def click_chase_see_all_transactions(driver: webdriver.Chrome) -> bool:
    script = """
        function collect(root, out) {
            const walker = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT);
            let node = walker.nextNode();
            while (node) {
                out.push(node);
                if (node.shadowRoot) collect(node.shadowRoot, out);
                node = walker.nextNode();
            }
            return out;
        }
        function visible(node) {
            if (!node || !node.getBoundingClientRect) return false;
            const style = window.getComputedStyle(node);
            const rect = node.getBoundingClientRect();
            return style.visibility !== 'hidden'
                && style.display !== 'none'
                && rect.width > 8
                && rect.height > 8;
        }
        function textOf(node) {
            return (node.innerText || node.textContent || node.getAttribute('aria-label') || '')
                .replace(/\\s+/g, ' ')
                .trim();
        }
        const nodes = collect(document, []);
        const candidates = nodes.filter((node) => {
            const tag = (node.tagName || '').toLowerCase();
            const role = (node.getAttribute('role') || '').toLowerCase();
            const text = textOf(node).toLowerCase();
            return visible(node)
                && text.includes('see all transactions')
                && (tag === 'button' || tag === 'a' || role === 'button');
        }).map((node) => {
            const tag = (node.tagName || '').toLowerCase();
            const text = textOf(node).toLowerCase();
            let score = 10;
            if (tag === 'button') score = 0;
            else if (tag === 'a') score = 1;
            if (text === 'see all transactions') score -= 1;
            return {node, score};
        }).sort((a, b) => a.score - b.score);
        if (!candidates.length) return false;
        const target = candidates[0].node;
        target.scrollIntoView({block: 'center', inline: 'center'});
        if (typeof target.click === 'function') target.click();
        return true;
    """
    fallback_script = """
        const nodes = Array.from(document.querySelectorAll('*'));
        function visible(node) {
            const rect = node.getBoundingClientRect();
            const style = window.getComputedStyle(node);
            return style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0;
        }
        const broad = nodes.find((node) => {
            const text = (node.innerText || node.textContent || node.getAttribute('aria-label') || '').replace(/\\s+/g, ' ').trim().toLowerCase();
            return visible(node) && text === 'see all transactions';
        });
        if (!broad) return false;
        broad.scrollIntoView({block: 'center', inline: 'center'});
        const rect = broad.getBoundingClientRect();
        document.elementFromPoint(rect.left + rect.width / 2, rect.top + rect.height / 2)?.click();
        return true;
    """
    clicked = False
    deadline = time.time() + 30
    while time.time() < deadline and not clicked:
        try:
            driver.execute_script("window.scrollTo({top: document.documentElement.scrollHeight || document.body.scrollHeight, behavior: 'instant'});")
            time.sleep(0.5)
            clicked = bool(driver.execute_script(script))
        except WebDriverException:
            clicked = False
        if not clicked:
            try:
                clicked = bool(driver.execute_script(fallback_script))
            except WebDriverException:
                clicked = False
        if not clicked:
            time.sleep(1)
    if clicked:
        wait_ready(driver, timeout=20)
        time.sleep(3)
    return clicked


def wait_for_chase_transactions_page(driver: webdriver.Chrome, timeout: int = 35) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        text = visible_text(driver).lower()
        if "transactions" in text and "activity since last statement" in text and ("showing" in text or "pending charges" in text):
            return True
        time.sleep(1)
    return False


def chase_dashboard_amount(display_amount: float) -> tuple[float, str]:
    amount = abs(display_amount) if display_amount < 0 else -abs(display_amount)
    return amount, "credit" if amount > 0 else "debit"


def chase_transaction_hash(row: dict[str, Any]) -> str:
    key = "|".join(
        [
            CHASE_SITE,
            row.get("transaction_date") or "",
            row.get("posted_date") or "",
            row.get("description") or "",
            f"{row.get('amount', 0):.2f}",
            "sync:chase",
        ]
    )
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def parse_chase_row_date(line: str) -> str | None:
    return parse_named_transaction_date(line) or parse_month_day_date(line)


def clean_chase_description(parts: list[str]) -> str:
    cleaned = []
    seen: set[str] = set()
    skip = {
        "action",
        "amount",
        "category",
        "current balance",
        "date",
        "description",
        "pending",
        "posted",
        "processing",
        "shopping",
        "show details",
        "view transaction details",
    }
    for part in parts:
        text = normalize_space(part)
        if not text or text.lower() in skip or parse_money_values(text) or parse_chase_row_date(text):
            continue
        if re.fullmatch(r"\d{1,2}/\d{1,2}/\d{2,4}", text):
            continue
        lowered = text.lower()
        if (
            "authorized these transactions" in lowered
            or "haven't been finalized" in lowered
            or "havent been finalized" in lowered
            or "select an account" in lowered
            or lowered.startswith("negative $")
            or lowered in {"amazon.com", "-", "\u2014", "\u2013"}
        ):
            continue
        text = re.sub(r"^\d{1,2}/\d{1,2}/\d{2,4}\s+", "", text).strip()
        if "," in text:
            before_comma, after_comma = [piece.strip() for piece in text.split(",", 1)]
            if re.search(r"\b(?:amazon|chase|paypal|venmo|google|apple)\.com\b", after_comma, flags=re.IGNORECASE):
                text = before_comma
        normalized = re.sub(r"[^a-z0-9]+", "", text.lower())
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        cleaned.append(text)
    return normalize_space(" ".join(cleaned))[:300] or "Chase transaction"


def chase_table_noise(line: str) -> bool:
    lowered = normalize_space(line).lower()
    return (
        not lowered
        or lowered in {
            "action",
            "amount",
            "amount, not sorted",
            "category",
            "current balance",
            "date",
            "date, not sorted",
            "description",
            "description, not sorted",
            "show details",
            "showing",
            "view transaction details",
        }
        or lowered.startswith("activity since last statement")
        or lowered.startswith("showing")
        or lowered.startswith("current balance")
        or lowered.endswith("statements")
    )


def scrape_chase_transactions_from_text(driver: webdriver.Chrome) -> list[dict[str, Any]]:
    lines = text_lines(driver)
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    start = 0
    for index, line in enumerate(lines):
        lowered = line.lower()
        if "pending charges" in lowered or "activity since last statement" in lowered:
            start = index
            break
    index = start
    last_tx_date: str | None = None
    pending_section = False
    while index < len(lines):
        lowered_line = normalize_space(lines[index]).lower()
        if lowered_line == "showing":
            pending_section = False
        if "pending charges" in lowered_line:
            pending_section = True
            index += 1
            continue
        tx_date = parse_chase_row_date(lines[index])
        pending_date = False
        if lowered_line == "pending":
            next_line = normalize_space(lines[index + 1]).lower() if index + 1 < len(lines) else ""
            if "pending charges" in next_line:
                pending_section = True
                index += 1
                continue
            tx_date = datetime.now().date().isoformat()
            pending_date = True
        if not tx_date:
            if not last_tx_date or chase_table_noise(lines[index]) or parse_money_values(lines[index]):
                index += 1
                continue
            tx_date = last_tx_date
        else:
            last_tx_date = tx_date
            index += 1
        pending_date = pending_date or pending_section
        parts: list[str] = []
        while index < len(lines):
            if parse_chase_row_date(lines[index]) or normalize_space(lines[index]).lower() in {"pending", "showing"}:
                break
            values = parse_money_values(lines[index])
            if values:
                description = clean_chase_description(parts)
                amount, debit_credit = chase_dashboard_amount(values[-1])
                key = f"{tx_date}|{description}|{amount:.2f}"
                if key not in seen and description:
                    seen.add(key)
                    rows.append(
                        {
                            "transaction_date": tx_date,
                            "posted_date": None if pending_date else tx_date,
                            "description": description,
                            "amount": amount,
                            "debit_credit": debit_credit,
                            "source_file": "sync:chase",
                        }
                    )
                index += 1
                break
            lowered = lines[index].lower()
            if lowered not in {"date", "description", "category", "amount", "view transaction details", "current balance"}:
                parts.append(lines[index])
            index += 1
    return rows


def record_chase_balance(current_balance: float, rewards_points: int | None) -> dict[str, Any]:
    balance_type = "liability" if current_balance >= 0 else "asset"
    amount = abs(round(float(current_balance), 2))
    captured_at = utc_now()
    raw_text = json.dumps({"current_balance": round(float(current_balance), 2), "rewards_points": rewards_points}, sort_keys=True)
    with connection() as conn:
        account = account_id(conn, CHASE_SITE)
        conn.execute("UPDATE accounts SET name = ?, account_type = ? WHERE id = ?", (CHASE_NAME, CHASE_ACCOUNT_TYPE, account))
        conn.execute(
            """
            INSERT INTO sync_balances (
                account_id, site, account_name, account_type, balance_type,
                amount, currency, captured_at, source, raw_text
            )
            VALUES (?, ?, ?, ?, ?, ?, 'USD', ?, ?, ?)
            """,
            (account, CHASE_SITE, CHASE_NAME, CHASE_ACCOUNT_TYPE, balance_type, amount, captured_at, "Chase online banking", raw_text),
        )
        if rewards_points is not None:
            rewards_account = account_id(conn, CHASE_REWARDS_SITE)
            rewards_type = "asset" if rewards_points >= 0 else "liability"
            rewards_amount = abs(round(rewards_points * 0.01, 2))
            rewards_raw_text = json.dumps(
                {"rewards_points": rewards_points, "point_value_dollars": 0.01, "estimated_value": rewards_amount},
                sort_keys=True,
            )
            conn.execute("UPDATE accounts SET name = ?, account_type = ? WHERE id = ?", (CHASE_REWARDS_NAME, CHASE_REWARDS_ACCOUNT_TYPE, rewards_account))
            conn.execute(
                """
                INSERT INTO sync_balances (
                    account_id, site, account_name, account_type, balance_type,
                    amount, currency, captured_at, source, raw_text
                )
                VALUES (?, ?, ?, ?, ?, ?, 'USD', ?, ?, ?)
                """,
                (
                    rewards_account,
                    CHASE_REWARDS_SITE,
                    CHASE_REWARDS_NAME,
                    CHASE_REWARDS_ACCOUNT_TYPE,
                    rewards_type,
                    rewards_amount,
                    captured_at,
                    "Chase Amazon Rewards",
                    rewards_raw_text,
                ),
            )
    return {
        "account_name": CHASE_NAME,
        "account_type": CHASE_ACCOUNT_TYPE,
        "balance_type": balance_type,
        "amount": amount,
        "captured_at": captured_at,
        "source": "Chase online banking",
        "current_balance": round(float(current_balance), 2),
        "rewards_points": rewards_points,
        "rewards_value": abs(round((rewards_points or 0) * 0.01, 2)) if rewards_points is not None else None,
    }


def record_chase_transaction(row: dict[str, Any]) -> bool:
    amount = float(row["amount"])
    with connection() as conn:
        account = account_id(conn, CHASE_SITE)
        conn.execute("UPDATE accounts SET name = ?, account_type = ? WHERE id = ?", (CHASE_NAME, CHASE_ACCOUNT_TYPE, account))
        duplicate = conn.execute(
            """
            SELECT id FROM transactions
            WHERE account_id = ?
              AND transaction_date = ?
              AND description = ?
              AND ABS(amount - ?) < 0.001
            LIMIT 1
            """,
            (account, row["transaction_date"], row["description"], amount),
        ).fetchone()
        if duplicate:
            return False
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO transactions (
                account_id, site, transaction_date, posted_date, description, amount,
                debit_credit, category, source_file, source_hash, imported_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                account,
                CHASE_SITE,
                row["transaction_date"],
                row.get("posted_date"),
                row["description"],
                amount,
                row["debit_credit"],
                classify(row["description"], None, amount, CHASE_SITE),
                "sync:chase",
                chase_transaction_hash(row),
                utc_now(),
            ),
        )
    return cursor.rowcount > 0


def run_chase_sync() -> dict[str, Any]:
    ensure_sync_tables()
    result = base_result("latest", "chase")
    write_json(SYNC_STATE_PATH, result)
    run_id: int | None = None
    with connection() as conn:
        cursor = conn.execute(
            """
            INSERT INTO sync_runs (institution, mode, status, started_at, message, result_json)
            VALUES ('chase', 'latest', ?, ?, ?, ?)
            """,
            (STATUS_RUNNING, result["started_at"], result["message"], json.dumps(result, sort_keys=True)),
        )
        run_id = int(cursor.lastrowid)
    driver: webdriver.Chrome | None = None
    try:
        if not chrome_debugger_available_at(CHASE_DEBUGGER_ADDRESS):
            open_chase_sync_browser()
            deadline = time.time() + 10
            while time.time() < deadline and not chrome_debugger_available_at(CHASE_DEBUGGER_ADDRESS):
                time.sleep(0.5)
        if not chrome_debugger_available_at(CHASE_DEBUGGER_ADDRESS):
            return finish_result(result, STATUS_WAITING_FOR_LOGIN, "Chase browser opened, but Chrome debugging was not ready yet. Run sync again to continue the automated login.", False)

        driver = create_chase_driver()
        move_sync_window_to_background(driver)
        safe_get(driver, CHASE_DASHBOARD_URL)
        login_status = submit_chase_login_if_autofilled(driver)
        if login_status == STATUS_WAITING_FOR_LOGIN:
            capture_screenshot(driver, result, "chase_waiting_for_login")
            return finish_result(result, STATUS_WAITING_FOR_LOGIN, "Chase login could not be submitted automatically because saved Chrome credentials or FIN_DASH_CHASE_* credentials were not available.", False)
        if login_status == STATUS_WAITING_FOR_USER_ACTION or chase_needs_user_action(driver):
            capture_screenshot(driver, result, "chase_waiting_for_security")
            return finish_result(result, STATUS_WAITING_FOR_USER_ACTION, "Chase is asking for a manual security step. Complete it in Chrome, then run sync again.", False)
        if not wait_for_chase_dashboard(driver):
            capture_screenshot(driver, result, "chase_dashboard_not_found")
            return finish_result(result, STATUS_WAITING_FOR_LOGIN, "Chase did not reach the account dashboard after the automated login submit.", False)

        dashboard_lines = text_lines(driver)
        current_balance = money_near_label(dashboard_lines, "Current balance")
        rewards_points = chase_rewards_points_from_lines(dashboard_lines)
        if current_balance is None:
            capture_screenshot(driver, result, "chase_current_balance_missing")
            return finish_result(result, STATUS_FAILED, "Chase dashboard loaded, but Current balance was not detected.", False)
        balance = record_chase_balance(current_balance, rewards_points)
        result["data"]["balances"].append(balance)

        if not click_chase_see_all_transactions(driver):
            result["errors"].append("Could not click Chase See all transactions; using dashboard-visible rows only.")
            capture_screenshot(driver, result, "chase_see_all_click_failed")
        else:
            wait_for_chase_transactions_page(driver)

        transactions = scrape_chase_transactions_from_text(driver)
        inserted = 0
        for transaction in transactions:
            if record_chase_transaction(transaction):
                inserted += 1
            result["data"]["transactions"].append(transaction)

        message = f"Chase sync completed. Captured {len(transactions)} transactions, {inserted} new. Prime Visa {balance['balance_type']}: ${balance['amount']:,.2f}."
        if rewards_points is not None:
            message += f" Amazon Rewards: {rewards_points:,} points."
        if not transactions:
            result["errors"].append("Reached Chase and captured balance, but no latest transaction rows were detected.")
            capture_screenshot(driver, result, "chase_transactions_missing")
        return finish_result(result, STATUS_SUCCESS, message)
    except Exception as exc:
        result["errors"].append(str(exc))
        capture_screenshot(driver, result, "chase_sync_exception")
        return finish_result(result, STATUS_FAILED, f"Chase sync failed: {exc}", False)
    finally:
        if driver:
            try:
                driver.quit()
            except WebDriverException:
                pass
        if run_id is not None:
            final_result = result
            with connection() as conn:
                conn.execute(
                    """
                    UPDATE sync_runs
                    SET status = ?, finished_at = ?, message = ?, result_json = ?
                    WHERE id = ?
                    """,
                    (
                        final_result.get("status", STATUS_FAILED),
                        final_result.get("finished_at") or utc_now(),
                        final_result.get("message", ""),
                        json.dumps(final_result, sort_keys=True),
                        run_id,
                    ),
                )
            close_completed_sync_browser(final_result, CHASE_PROFILE_DIR, CHASE_DEBUGGING_PORT)


def citi_logged_in(driver: webdriver.Chrome) -> bool:
    text = visible_text(driver).lower()
    return "costco anywhere visa" in text and ("current balance" in text or "costco cash rewards" in text)


def citi_needs_login(driver: webdriver.Chrome) -> bool:
    if citi_logged_in(driver):
        return False
    text = visible_text(driver).lower()
    url = ""
    try:
        url = driver.current_url.lower()
    except WebDriverException:
        pass
    return "sign on" in text or "user id" in text or "password" in text or "login" in url


def citi_needs_user_action(driver: webdriver.Chrome) -> bool:
    text = visible_text(driver).lower()
    return any(term in text for term in ["help us verify", "code verification", "enter your code", "one-time identification code"])


def submit_citi_login_if_autofilled(driver: webdriver.Chrome) -> str | None:
    return submit_autofilled_login(
        driver,
        citi_logged_in,
        citi_needs_login,
        citi_needs_user_action,
        wait_seconds=60,
        institution="citi",
    )


def set_interim_sync_result(result: dict[str, Any], status: str, message: str) -> None:
    result["status"] = status
    result["success"] = False
    result["message"] = message
    result["finished_at"] = None
    write_json(SYNC_STATE_PATH, result)


def click_citi_continue(driver: webdriver.Chrome) -> bool:
    return click_by_text(driver, ["continue"], exact=False)


def fill_citi_code(driver: webdriver.Chrome, code: str) -> bool:
    script = """
        const code = arguments[0];
        function collect(root) {
            const found = Array.from(root.querySelectorAll('input, [contenteditable="true"], textarea'));
            for (const node of Array.from(root.querySelectorAll('*'))) {
                if (node.shadowRoot) found.push(...collect(node.shadowRoot));
            }
            return found;
        }
        const inputs = collect(document);
        function visible(node) {
            const style = window.getComputedStyle(node);
            const rect = node.getBoundingClientRect();
            return style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0;
        }
        const target = inputs.find((node) => visible(node) && !node.disabled && ((node.value || '').length <= 10 || node.getAttribute('contenteditable') === 'true'));
        if (!target) return false;
        target.scrollIntoView({block: 'center'});
        target.focus();
        target.click();
        if (target.getAttribute('contenteditable') === 'true') target.textContent = code;
        else {
            const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set
                || Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value')?.set;
            if (setter) setter.call(target, code);
            else target.value = code;
        }
        target.dispatchEvent(new Event('input', {bubbles: true}));
        target.dispatchEvent(new Event('change', {bubbles: true}));
        return true;
    """
    try:
        filled = bool(driver.execute_script(script, code))
    except WebDriverException:
        filled = False
    if not filled:
        try:
            driver.switch_to.active_element.send_keys(code)
            filled = True
        except WebDriverException:
            return False
    time.sleep(0.5)
    return click_citi_continue(driver)


def wait_for_citi_dashboard(driver: webdriver.Chrome, timeout: int = 75) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        wait_ready(driver, timeout=5)
        if citi_logged_in(driver):
            return True
        time.sleep(1)
    return citi_logged_in(driver)


def click_citi_costco_card(driver: webdriver.Chrome) -> bool:
    script = """
        function collect(root) {
            const found = Array.from(root.querySelectorAll('a, button, [role="button"], [tabindex]'));
            for (const node of Array.from(root.querySelectorAll('*'))) {
                if (node.shadowRoot) found.push(...collect(node.shadowRoot));
            }
            return found;
        }
        const nodes = collect(document);
        function visible(node) {
            const style = window.getComputedStyle(node);
            const rect = node.getBoundingClientRect();
            return style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0;
        }
        const node = nodes.find((item) => {
            const text = (item.innerText || item.textContent || item.getAttribute('aria-label') || '').replace(/\\s+/g, ' ').trim().toLowerCase();
            return visible(item) && text.includes('costco anywhere visa');
        });
        if (!node) return false;
        node.scrollIntoView({block: 'center'});
        node.click();
        return true;
    """
    try:
        clicked = bool(driver.execute_script(script))
    except WebDriverException:
        clicked = False
    if clicked:
        wait_ready(driver, timeout=15)
        time.sleep(3)
    return clicked


def citi_dashboard_amount(display_amount: float) -> tuple[float, str]:
    amount = abs(display_amount) if display_amount < 0 else -abs(display_amount)
    return amount, "credit" if amount > 0 else "debit"


def citi_transaction_hash(row: dict[str, Any]) -> str:
    key = "|".join([CITI_SITE, row.get("transaction_date") or "", row.get("description") or "", f"{row.get('amount', 0):.2f}", "sync:citi"])
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def scrape_citi_transactions(driver: webdriver.Chrome) -> list[dict[str, Any]]:
    lines = text_lines(driver)
    rows: list[dict[str, Any]] = []
    start_index: int | None = None
    for index, line in enumerate(lines):
        lowered = line.lower()
        if lowered.startswith("pending total") or lowered.startswith("posted total"):
            start_index = index
            break
    if start_index is None:
        return []
    skip_description_prefixes = (
        "account overview",
        "available credit",
        "last statement balance",
        "filter by",
        "payment due",
        "statement closing",
    )
    for index, line in enumerate(lines):
        if index <= start_index:
            continue
        tx_date = parse_named_transaction_date(line) or parse_month_day_date(line)
        if not tx_date:
            continue
        parts: list[str] = []
        amount: float | None = None
        for candidate in lines[index + 1:index + 8]:
            values = parse_money_values(candidate)
            if values:
                amount = values[-1]
                break
            lowered = candidate.lower()
            if lowered not in {"date", "description", "amount", "running balance", "pending total", "posted total"}:
                parts.append(candidate)
        if amount is None:
            continue
        description = normalize_space(" ".join(parts))[:300] or "Citi transaction"
        if description.lower().startswith(skip_description_prefixes):
            continue
        signed_amount, debit_credit = citi_dashboard_amount(amount)
        rows.append(
            {
                "transaction_date": tx_date,
                "posted_date": tx_date,
                "description": description,
                "amount": round(signed_amount, 2),
                "debit_credit": debit_credit,
                "source_file": "sync:citi",
            }
        )
    deduped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        digest = citi_transaction_hash(row)
        if digest in seen:
            continue
        seen.add(digest)
        deduped.append(row)
    return deduped


def record_citi_balance(current_balance: float, rewards_amount: float | None) -> dict[str, Any]:
    balance_type = "liability" if current_balance >= 0 else "asset"
    amount = abs(round(float(current_balance), 2))
    captured_at = utc_now()
    with connection() as conn:
        account = account_id(conn, CITI_SITE)
        conn.execute("UPDATE accounts SET name = ?, account_type = ? WHERE id = ?", (CITI_NAME, CITI_ACCOUNT_TYPE, account))
        conn.execute(
            """
            INSERT INTO sync_balances (
                account_id, site, account_name, account_type, balance_type,
                amount, currency, captured_at, source, raw_text
            )
            VALUES (?, ?, ?, ?, ?, ?, 'USD', ?, ?, ?)
            """,
            (account, CITI_SITE, CITI_NAME, CITI_ACCOUNT_TYPE, balance_type, amount, captured_at, "Citi online banking", json.dumps({"current_balance": current_balance})),
        )
        if rewards_amount is not None:
            rewards_account = account_id(conn, CITI_REWARDS_SITE)
            conn.execute("UPDATE accounts SET name = ?, account_type = ? WHERE id = ?", (CITI_REWARDS_NAME, CITI_REWARDS_ACCOUNT_TYPE, rewards_account))
            conn.execute(
                """
                INSERT INTO sync_balances (
                    account_id, site, account_name, account_type, balance_type,
                    amount, currency, captured_at, source, raw_text
                )
                VALUES (?, ?, ?, ?, 'asset', ?, 'USD', ?, ?, ?)
                """,
                (
                    rewards_account,
                    CITI_REWARDS_SITE,
                    CITI_REWARDS_NAME,
                    CITI_REWARDS_ACCOUNT_TYPE,
                    abs(round(float(rewards_amount), 2)),
                    captured_at,
                    "Citi Costco Cash Rewards",
                    json.dumps({"costco_cash_rewards_ytd": rewards_amount}),
                ),
            )
    return {
        "account_name": CITI_NAME,
        "account_type": CITI_ACCOUNT_TYPE,
        "balance_type": balance_type,
        "amount": amount,
        "captured_at": captured_at,
        "source": "Citi online banking",
        "current_balance": round(float(current_balance), 2),
        "rewards_value": abs(round(float(rewards_amount or 0), 2)) if rewards_amount is not None else None,
    }


def record_citi_transaction(row: dict[str, Any]) -> bool:
    amount = float(row["amount"])
    with connection() as conn:
        account = account_id(conn, CITI_SITE)
        conn.execute("UPDATE accounts SET name = ?, account_type = ? WHERE id = ?", (CITI_NAME, CITI_ACCOUNT_TYPE, account))
        duplicate = conn.execute(
            """
            SELECT id FROM transactions
            WHERE account_id = ?
              AND transaction_date = ?
              AND description = ?
              AND ABS(amount - ?) < 0.001
            LIMIT 1
            """,
            (account, row["transaction_date"], row["description"], amount),
        ).fetchone()
        if duplicate:
            return False
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO transactions (
                account_id, site, transaction_date, posted_date, description, amount,
                debit_credit, category, source_file, source_hash, imported_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                account,
                CITI_SITE,
                row["transaction_date"],
                row.get("posted_date"),
                row["description"],
                amount,
                row["debit_credit"],
                classify(row["description"], None, amount, CITI_SITE),
                "sync:citi",
                citi_transaction_hash(row),
                utc_now(),
            ),
        )
    return cursor.rowcount > 0


def run_citi_sync(
    code_provider: Callable[[dict[str, Any]], str | None] | None = None,
    verified_callback: Callable[[], None] | None = None,
) -> dict[str, Any]:
    ensure_sync_tables()
    result = base_result("latest", "citi")
    write_json(SYNC_STATE_PATH, result)
    run_id: int | None = None
    with connection() as conn:
        cursor = conn.execute(
            """
            INSERT INTO sync_runs (institution, mode, status, started_at, message, result_json)
            VALUES ('citi', 'latest', ?, ?, ?, ?)
            """,
            (STATUS_RUNNING, result["started_at"], result["message"], json.dumps(result, sort_keys=True)),
        )
        run_id = int(cursor.lastrowid)
    driver: webdriver.Chrome | None = None
    try:
        if not chrome_debugger_available_at(CITI_DEBUGGER_ADDRESS):
            open_citi_sync_browser()
            deadline = time.time() + 10
            while time.time() < deadline and not chrome_debugger_available_at(CITI_DEBUGGER_ADDRESS):
                time.sleep(0.5)
        if not chrome_debugger_available_at(CITI_DEBUGGER_ADDRESS):
            return finish_result(result, STATUS_WAITING_FOR_LOGIN, "Citi browser opened, but Chrome debugging was not ready yet. Run sync again to continue.", False)

        driver = create_citi_driver()
        move_sync_window_to_background(driver)
        safe_get(driver, CITI_DASHBOARD_URL)
        login_status = submit_citi_login_if_autofilled(driver)
        if login_status == STATUS_WAITING_FOR_LOGIN:
            capture_screenshot(driver, result, "citi_waiting_for_login")
            return finish_result(result, STATUS_WAITING_FOR_LOGIN, "Citi login could not be submitted automatically from saved Chrome credentials.", False)

        if citi_needs_user_action(driver):
            if "help us verify" in visible_text(driver).lower():
                click_citi_continue(driver)
                time.sleep(3)
            if code_provider is None:
                capture_screenshot(driver, result, "citi_waiting_for_code")
                return finish_result(result, STATUS_WAITING_FOR_USER_ACTION, "Citi is asking for a 6-digit verification code.", False)
            code_attempt = 0
            while citi_needs_user_action(driver):
                code_attempt += 1
                prompt = "Enter the Citi 6-digit verification code to continue sync."
                if code_attempt > 1:
                    prompt = "Citi verification code was wrong. Try again."
                set_interim_sync_result(result, STATUS_WAITING_FOR_USER_ACTION, prompt)
                capture_screenshot(driver, result, "citi_waiting_for_code")
                code = code_provider(result)
                if not code:
                    return finish_result(result, STATUS_WAITING_FOR_USER_ACTION, "Citi verification code was not provided.", False)
                if not fill_citi_code(driver, code):
                    capture_screenshot(driver, result, "citi_code_submit_failed")
                    return finish_result(result, STATUS_FAILED, "Citi verification code could not be submitted.", False)
                verify_deadline = time.time() + 25
                while time.time() < verify_deadline:
                    wait_ready(driver, timeout=3)
                    if citi_logged_in(driver):
                        break
                    text = visible_text(driver).lower()
                    if citi_needs_user_action(driver) and any(term in text for term in ["incorrect", "invalid", "try again", "doesn't match", "does not match", "wrong"]):
                        break
                    time.sleep(1)
                if citi_logged_in(driver):
                    break
                if citi_needs_user_action(driver):
                    continue
                break

        if not wait_for_citi_dashboard(driver):
            capture_screenshot(driver, result, "citi_dashboard_not_found")
            return finish_result(result, STATUS_WAITING_FOR_USER_ACTION, "Citi verification code was wrong. Try again.", False)
        set_interim_sync_result(result, STATUS_RUNNING, "Citi verification accepted. Sync is continuing.")
        if verified_callback:
            verified_callback()

        if not click_citi_costco_card(driver):
            capture_screenshot(driver, result, "citi_costco_click_failed")
            return finish_result(result, STATUS_FAILED, "Citi dashboard loaded, but Costco Anywhere Visa could not be selected.", False)
        time.sleep(4)
        lines = text_lines(driver)
        current_balance = money_near_label(lines, "Current Balance")
        rewards_amount = money_near_label(lines, "Costco Cash Rewards")
        if current_balance is None:
            capture_screenshot(driver, result, "citi_current_balance_missing")
            return finish_result(result, STATUS_FAILED, "Citi Costco card loaded, but Current Balance was not detected.", False)
        balance = record_citi_balance(current_balance, rewards_amount)
        result["data"]["balances"].append(balance)
        transactions = scrape_citi_transactions(driver)
        inserted = 0
        for transaction in transactions:
            if record_citi_transaction(transaction):
                inserted += 1
            result["data"]["transactions"].append(transaction)
        message = f"Citi sync completed. Captured {len(transactions)} transactions, {inserted} new. Costco Visa {balance['balance_type']}: ${balance['amount']:,.2f}."
        if rewards_amount is not None:
            message += f" Costco Cash Rewards: ${abs(rewards_amount):,.2f}."
        if not transactions:
            result["errors"].append("Reached Citi and captured balance, but no latest transaction rows were detected.")
            capture_screenshot(driver, result, "citi_transactions_missing")
        return finish_result(result, STATUS_SUCCESS, message)
    except Exception as exc:
        result["errors"].append(str(exc))
        capture_screenshot(driver, result, "citi_sync_exception")
        return finish_result(result, STATUS_FAILED, f"Citi sync failed: {exc}", False)
    finally:
        if driver:
            try:
                driver.quit()
            except WebDriverException:
                pass
        if run_id is not None:
            final_result = result
            with connection() as conn:
                conn.execute(
                    """
                    UPDATE sync_runs
                    SET status = ?, finished_at = ?, message = ?, result_json = ?
                    WHERE id = ?
                    """,
                    (
                        final_result.get("status", STATUS_FAILED),
                        final_result.get("finished_at") or utc_now(),
                        final_result.get("message", ""),
                        json.dumps(final_result, sort_keys=True),
                        run_id,
                    ),
                )
            close_completed_sync_browser(final_result, CITI_PROFILE_DIR, CITI_DEBUGGING_PORT)


def vanguard_logged_in(driver: webdriver.Chrome) -> bool:
    text = visible_text(driver).lower()
    if "total balance" not in text:
        return False
    return any(term in text for term in ["good morning", "good afternoon", "good evening", "accounts", "rate of return"])


def vanguard_needs_login(driver: webdriver.Chrome) -> bool:
    if vanguard_logged_in(driver):
        return False
    text = visible_text(driver).lower()
    url = ""
    try:
        url = driver.current_url.lower()
    except WebDriverException:
        pass
    return "login" in url or "log in" in text or "username" in text or "user name" in text or "password" in text


def vanguard_needs_user_action(driver: webdriver.Chrome) -> bool:
    text = visible_text(driver).lower()
    return any(
        term in text
        for term in [
            "verification code",
            "security code",
            "one-time",
            "one time",
            "verify your identity",
            "captcha",
            "multifactor",
            "multi-factor",
        ]
    )


def click_vanguard_text_me(driver: webdriver.Chrome) -> bool:
    return click_by_text(driver, ["Text Me"], exact=True) or click_by_text(driver, ["Text Me"], exact=False)


def fill_vanguard_code(driver: webdriver.Chrome, code: str) -> bool:
    script = """
        const code = arguments[0];
        function collect(root) {
            const found = Array.from(root.querySelectorAll('input, [contenteditable="true"], textarea'));
            for (const node of Array.from(root.querySelectorAll('*'))) {
                if (node.shadowRoot) found.push(...collect(node.shadowRoot));
            }
            return found;
        }
        function visible(node) {
            const style = window.getComputedStyle(node);
            const rect = node.getBoundingClientRect();
            return style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0;
        }
        function fieldText(node) {
            return [
                node.getAttribute('autocomplete'),
                node.getAttribute('aria-label'),
                node.getAttribute('placeholder'),
                node.getAttribute('name'),
                node.getAttribute('id'),
            ].filter(Boolean).join(' ').toLowerCase();
        }
        const inputs = collect(document);
        const target = inputs.find((node) => visible(node) && !node.disabled && /code|otp|one.?time|verification|pin/.test(fieldText(node)))
            || inputs.find((node) => visible(node) && !node.disabled && ((node.value || '').length <= 10 || node.getAttribute('contenteditable') === 'true'));
        if (!target) return false;
        target.scrollIntoView({block: 'center'});
        target.focus();
        target.click();
        if (target.getAttribute('contenteditable') === 'true') target.textContent = code;
        else {
            const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set
                || Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value')?.set;
            if (setter) setter.call(target, code);
            else target.value = code;
        }
        target.dispatchEvent(new Event('input', {bubbles: true}));
        target.dispatchEvent(new Event('change', {bubbles: true}));
        return true;
    """
    try:
        filled = bool(driver.execute_script(script, code))
    except WebDriverException:
        filled = False
    if not filled:
        try:
            driver.switch_to.active_element.send_keys(code)
            filled = True
        except WebDriverException:
            return False
    time.sleep(0.5)
    return click_by_text(driver, ["continue", "submit", "verify", "log in"], exact=False)


def submit_vanguard_login_if_autofilled(driver: webdriver.Chrome) -> str | None:
    credentials = login_credentials("vanguard")
    if credentials and not vanguard_logged_in(driver):
        try:
            driver.switch_to.default_content()
        except WebDriverException:
            pass
        if fill_vanguard_login_form(driver, credentials):
            deadline = time.time() + 20
            while time.time() < deadline:
                wait_ready(driver, timeout=3)
                if vanguard_logged_in(driver):
                    return None
                if vanguard_needs_user_action(driver):
                    return STATUS_WAITING_FOR_USER_ACTION
                time.sleep(1)
    if not credentials and not vanguard_logged_in(driver):
        if select_vanguard_saved_login(driver):
            deadline = time.time() + 25
            while time.time() < deadline:
                wait_ready(driver, timeout=3)
                if vanguard_logged_in(driver):
                    return None
                if vanguard_needs_user_action(driver):
                    return STATUS_WAITING_FOR_USER_ACTION
                time.sleep(1)
    return submit_autofilled_login(
        driver,
        vanguard_logged_in,
        vanguard_needs_login,
        vanguard_needs_user_action,
        wait_seconds=60,
        institution="vanguard",
    )


def select_vanguard_saved_login(driver: webdriver.Chrome) -> bool:
    script_focus = """
        const field = document.querySelector('#username, input[name="Username"], input[type="text"]')
            || document.querySelector('#password, input[name="Password"], input[type="password"]');
        if (!field) return false;
        field.scrollIntoView({block: 'center'});
        field.focus();
        field.click();
        return true;
    """
    script_lengths = """
        const username = document.querySelector('#username, input[name="Username"], input[type="text"]');
        const password = document.querySelector('#password, input[name="Password"], input[type="password"]');
        return {
            username: username ? String(username.value || '').length : 0,
            password: password ? String(password.value || '').length : 0,
        };
    """
    script_submit = """
        function visible(node) {
            const style = window.getComputedStyle(node);
            const rect = node.getBoundingClientRect();
            return style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0;
        }
        function nodeText(node) {
            return [node.innerText, node.textContent, node.value]
                .filter(Boolean).join(' ').replace(/\\s+/g, ' ').trim().toLowerCase();
        }
        const buttons = Array.from(document.querySelectorAll('button, input[type="submit"], [role="button"]'));
        const button = buttons.find((node) => visible(node) && /^(log in|login|sign in|continue)$/i.test(nodeText(node)))
            || buttons.find((node) => visible(node) && /(log in|login|sign in|continue)/i.test(nodeText(node)));
        if (!button || button.disabled || button.getAttribute('aria-disabled') === 'true') return false;
        button.scrollIntoView({block: 'center'});
        button.click();
        return true;
    """
    try:
        driver.set_window_rect(x=80, y=40, width=1280, height=900)
    except WebDriverException:
        pass
    try:
        driver.switch_to.default_content()
        focused = bool(driver.execute_script(script_focus))
    except WebDriverException:
        focused = False
    if not focused:
        return False
    time.sleep(1)
    for _ in range(4):
        try:
            dispatch_browser_key(driver, "ArrowDown", "ArrowDown", 40)
            time.sleep(0.35)
            dispatch_browser_key(driver, "Enter", "Enter", 13)
        except WebDriverException:
            try:
                driver.switch_to.active_element.send_keys(Keys.ARROW_DOWN)
                driver.switch_to.active_element.send_keys(Keys.ENTER)
            except WebDriverException:
                return False
        time.sleep(1.5)
        try:
            lengths = dict(driver.execute_script(script_lengths) or {})
        except WebDriverException:
            lengths = {}
        if int(lengths.get("password") or 0) > 0:
            try:
                return bool(driver.execute_script(script_submit))
            except WebDriverException:
                return False
    return False


def fill_vanguard_login_form(driver: webdriver.Chrome, credentials: tuple[str, str]) -> bool:
    username, password = credentials
    script = """
        const username = arguments[0];
        const password = arguments[1];
        function collect(root) {
            const found = Array.from(root.querySelectorAll('input, button, [role="button"]'));
            for (const node of Array.from(root.querySelectorAll('*'))) {
                if (node.shadowRoot) found.push(...collect(node.shadowRoot));
            }
            return found;
        }
        function visible(node) {
            const style = window.getComputedStyle(node);
            const rect = node.getBoundingClientRect();
            return style.visibility !== 'hidden' && style.display !== 'none'
                && rect.width > 0 && rect.height > 0 && node.getAttribute('aria-hidden') !== 'true';
        }
        function setValue(node, value) {
            const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
            setter.call(node, value);
            node.dispatchEvent(new Event('input', {bubbles: true}));
            node.dispatchEvent(new Event('change', {bubbles: true}));
            node.dispatchEvent(new Event('blur', {bubbles: true}));
        }
        function fieldText(node) {
            return [
                node.getAttribute('autocomplete'),
                node.getAttribute('aria-label'),
                node.getAttribute('placeholder'),
                node.getAttribute('name'),
                node.getAttribute('id'),
            ].filter(Boolean).join(' ').toLowerCase();
        }
        function nodeText(node) {
            return [
                node.innerText,
                node.textContent,
                node.getAttribute('aria-label'),
                node.getAttribute('title'),
                node.value,
            ].filter(Boolean).join(' ').replace(/\\s+/g, ' ').trim().toLowerCase();
        }
        const nodes = collect(document).filter(visible);
        const inputs = nodes.filter((node) => node.tagName && node.tagName.toLowerCase() === 'input' && !node.disabled);
        const passwordField = inputs.find((node) => (node.getAttribute('type') || '').toLowerCase() === 'password');
        const usernameField = inputs.find((node) => {
            const text = fieldText(node);
            const type = (node.getAttribute('type') || 'text').toLowerCase();
            return type !== 'password' && /(user|username|user name|login)/.test(text);
        }) || inputs.find((node) => (node.getAttribute('type') || 'text').toLowerCase() !== 'password');
        if (!usernameField || !passwordField) return false;
        setValue(usernameField, username);
        setValue(passwordField, password);
        const buttons = nodes.filter((node) => ['button', 'a'].includes(node.tagName.toLowerCase()) || node.getAttribute('role') === 'button');
        const loginButton = buttons.find((node) => /^(log in|login|sign in|continue)$/i.test(nodeText(node)))
            || buttons.find((node) => /(log in|login|sign in|continue)/i.test(nodeText(node)));
        if (!loginButton || loginButton.disabled || loginButton.getAttribute('aria-disabled') === 'true') return false;
        loginButton.scrollIntoView({block: 'center'});
        loginButton.click();
        return true;
    """
    def attempt_current_context() -> bool:
        try:
            return bool(driver.execute_script(script, username, password))
        except WebDriverException:
            return False

    try:
        driver.switch_to.default_content()
    except WebDriverException:
        pass
    if attempt_current_context():
        return True
    try:
        frames = driver.find_elements(By.CSS_SELECTOR, "iframe, frame")
    except WebDriverException:
        frames = []
    for frame in frames:
        try:
            driver.switch_to.default_content()
            driver.switch_to.frame(frame)
        except WebDriverException:
            continue
        if attempt_current_context():
            try:
                driver.switch_to.default_content()
            except WebDriverException:
                pass
            return True
    try:
        driver.switch_to.default_content()
    except WebDriverException:
        pass
    return False


def wait_for_vanguard_dashboard(driver: webdriver.Chrome, timeout: int = 60) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        wait_ready(driver, timeout=5)
        if vanguard_logged_in(driver):
            return True
        if vanguard_needs_user_action(driver):
            return False
        time.sleep(1)
    return vanguard_logged_in(driver)


def vanguard_total_balance(lines: list[str]) -> float | None:
    balance = money_after_label(lines, "Total Balance")
    if balance is not None:
        return abs(balance)
    for index, line in enumerate(lines):
        if line.lower().strip(" >") != "total balance":
            continue
        for candidate in lines[index + 1:index + 5]:
            values = parse_money_values(candidate)
            if values:
                return abs(values[0])
    return None


def wait_for_vanguard_total_balance(driver: webdriver.Chrome, timeout: int = 90) -> float | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        wait_ready(driver, timeout=5)
        balance = vanguard_total_balance(text_lines(driver))
        if balance is not None:
            return balance
        time.sleep(2)
    return vanguard_total_balance(text_lines(driver))


def record_vanguard_balance(total_balance: float) -> dict[str, Any]:
    amount = abs(round(float(total_balance), 2))
    captured_at = utc_now()
    raw_text = json.dumps({"total_balance": amount}, sort_keys=True)
    with connection() as conn:
        account = account_id(conn, VANGUARD_SITE)
        conn.execute("UPDATE accounts SET name = ?, account_type = ? WHERE id = ?", (VANGUARD_NAME, VANGUARD_ACCOUNT_TYPE, account))
        conn.execute(
            """
            INSERT INTO sync_balances (
                account_id, site, account_name, account_type, balance_type,
                amount, currency, captured_at, source, raw_text
            )
            VALUES (?, ?, ?, ?, 'asset', ?, 'USD', ?, ?, ?)
            """,
            (account, VANGUARD_SITE, VANGUARD_NAME, VANGUARD_ACCOUNT_TYPE, amount, captured_at, "Vanguard online account", raw_text),
        )
    return {
        "account_name": VANGUARD_NAME,
        "account_type": VANGUARD_ACCOUNT_TYPE,
        "balance_type": "asset",
        "amount": amount,
        "captured_at": captured_at,
        "source": "Vanguard online account",
        "total_balance": amount,
    }


def run_vanguard_sync(
    code_provider: Callable[[dict[str, Any]], str | None] | None = None,
    verified_callback: Callable[[], None] | None = None,
) -> dict[str, Any]:
    ensure_sync_tables()
    result = base_result("latest", "vanguard")
    write_json(SYNC_STATE_PATH, result)
    run_id: int | None = None
    with connection() as conn:
        cursor = conn.execute(
            """
            INSERT INTO sync_runs (institution, mode, status, started_at, message, result_json)
            VALUES ('vanguard', 'latest', ?, ?, ?, ?)
            """,
            (STATUS_RUNNING, result["started_at"], result["message"], json.dumps(result, sort_keys=True)),
        )
        run_id = int(cursor.lastrowid)
    driver: webdriver.Chrome | None = None
    try:
        if not chrome_debugger_available_at(VANGUARD_DEBUGGER_ADDRESS):
            open_vanguard_sync_browser()
            deadline = time.time() + 10
            while time.time() < deadline and not chrome_debugger_available_at(VANGUARD_DEBUGGER_ADDRESS):
                time.sleep(0.5)
        if not chrome_debugger_available_at(VANGUARD_DEBUGGER_ADDRESS):
            return finish_result(result, STATUS_WAITING_FOR_LOGIN, "Vanguard browser opened, but Chrome debugging was not ready yet. Run sync again to continue.", False)

        driver = create_vanguard_driver()
        if not vanguard_logged_in(driver):
            try:
                current_url = driver.current_url.lower()
            except WebDriverException:
                current_url = ""
            if "my.vanguardplan.com/login/participant" not in current_url:
                safe_get(driver, VANGUARD_LOGIN_URL)
        if not vanguard_logged_in(driver):
            try:
                driver.set_window_rect(x=80, y=40, width=1280, height=900)
            except WebDriverException:
                pass
        if not vanguard_logged_in(driver) and "my.vanguardplan.com/login/participant" not in current_url:
            safe_get(driver, VANGUARD_LOGIN_URL)
        login_status = submit_vanguard_login_if_autofilled(driver)
        if login_status == STATUS_WAITING_FOR_LOGIN:
            capture_screenshot(driver, result, "vanguard_waiting_for_login")
            return finish_result(result, STATUS_WAITING_FOR_LOGIN, "Vanguard login could not be submitted automatically. Set FIN_DASH_VANGUARD_USERNAME and FIN_DASH_VANGUARD_PASSWORD, or save Vanguard credentials in the Vanguard Chrome profile.", False)
        if login_status == STATUS_WAITING_FOR_USER_ACTION or vanguard_needs_user_action(driver):
            text = visible_text(driver).lower()
            if "text me" in text or "choose how you'd like" in text or "choose how you would like" in text:
                click_vanguard_text_me(driver)
                time.sleep(3)
            if code_provider is None:
                capture_screenshot(driver, result, "vanguard_waiting_for_code")
                return finish_result(result, STATUS_WAITING_FOR_USER_ACTION, "Enter the Vanguard 6-digit verification code to continue sync.", False)
            code_attempt = 0
            while vanguard_needs_user_action(driver) and not vanguard_logged_in(driver):
                code_attempt += 1
                prompt = "Enter the Vanguard 6-digit verification code to continue sync."
                if code_attempt > 1:
                    prompt = "Vanguard verification code was wrong. Try again."
                set_interim_sync_result(result, STATUS_WAITING_FOR_USER_ACTION, prompt)
                capture_screenshot(driver, result, "vanguard_waiting_for_code")
                code = code_provider(result)
                if not code:
                    return finish_result(result, STATUS_WAITING_FOR_USER_ACTION, "Vanguard verification code was not provided.", False)
                if not fill_vanguard_code(driver, code):
                    capture_screenshot(driver, result, "vanguard_code_submit_failed")
                    return finish_result(result, STATUS_FAILED, "Vanguard verification code could not be submitted.", False)
                verify_deadline = time.time() + 30
                while time.time() < verify_deadline:
                    wait_ready(driver, timeout=3)
                    if vanguard_logged_in(driver):
                        break
                    text = visible_text(driver).lower()
                    if vanguard_needs_user_action(driver) and any(term in text for term in ["incorrect", "invalid", "try again", "doesn't match", "does not match", "wrong"]):
                        break
                    time.sleep(1)
                if vanguard_logged_in(driver):
                    break
                if vanguard_needs_user_action(driver):
                    continue
                break
        if not wait_for_vanguard_dashboard(driver):
            capture_screenshot(driver, result, "vanguard_dashboard_not_found")
            return finish_result(result, STATUS_WAITING_FOR_LOGIN, "Vanguard did not reach the account dashboard after the automated login submit.", False)
        set_interim_sync_result(result, STATUS_RUNNING, "Vanguard verification accepted. Sync is continuing.")
        if verified_callback:
            verified_callback()
        move_sync_window_to_background(driver)

        total_balance = wait_for_vanguard_total_balance(driver)
        if total_balance is None:
            capture_screenshot(driver, result, "vanguard_total_balance_missing")
            return finish_result(result, STATUS_FAILED, "Vanguard dashboard loaded, but Total Balance was not detected.", False)

        balance = record_vanguard_balance(total_balance)
        result["data"]["balances"].append(balance)
        return finish_result(result, STATUS_SUCCESS, f"Vanguard sync completed. Retirement asset: ${balance['amount']:,.2f}.")
    except Exception as exc:
        result["errors"].append(str(exc))
        capture_screenshot(driver, result, "vanguard_sync_exception")
        return finish_result(result, STATUS_FAILED, f"Vanguard sync failed: {exc}", False)
    finally:
        if driver:
            try:
                driver.quit()
            except WebDriverException:
                pass
        if run_id is not None:
            final_result = result
            with connection() as conn:
                conn.execute(
                    """
                    UPDATE sync_runs
                    SET status = ?, finished_at = ?, message = ?, result_json = ?
                    WHERE id = ?
                    """,
                    (
                        final_result.get("status", STATUS_FAILED),
                        final_result.get("finished_at") or utc_now(),
                        final_result.get("message", ""),
                        json.dumps(final_result, sort_keys=True),
                        run_id,
                    ),
                )
            close_completed_sync_browser(final_result, VANGUARD_PROFILE_DIR, VANGUARD_DEBUGGING_PORT)


class SyncManager:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.state_lock = threading.Lock()
        self.thread: threading.Thread | None = None
        self.active_workers: set[str] = set()
        self.worker_results: dict[str, dict[str, Any]] = {}
        self.batch_state: dict[str, Any] | None = None
        self.citi_code_event = threading.Event()
        self.citi_code: str | None = None
        self.citi_waiting_for_code = False
        self.vanguard_code_event = threading.Event()
        self.vanguard_code: str | None = None
        self.vanguard_waiting_for_code = False

    @property
    def institutions(self) -> dict[str, str]:
        return {"citizens": "Citizens", "amex": "Amex", "chase": "Chase", "citi": "Citi", "vanguard": "Vanguard"}

    def normalize_institutions(self, institutions: list[str] | None) -> list[str]:
        requested = list(self.institutions) if institutions is None else institutions
        selected: list[str] = []
        for institution in requested:
            key = normalize_space(str(institution)).lower()
            if key in self.institutions and key not in selected:
                selected.append(key)
        if not selected:
            raise ValueError("Select at least one institution to sync.")
        return selected

    def worker_snapshot(self) -> dict[str, dict[str, str | None]]:
        return {
            key: {
                "label": label,
                "status": self.worker_results.get(key, {}).get("status", STATUS_IDLE),
                "message": self.worker_results.get(key, {}).get("message"),
            }
            for key, label in self.institutions.items()
        }

    def aggregate_batch_state(self, finished: bool = False) -> dict[str, Any]:
        with self.state_lock:
            selected = list((self.batch_state or {}).get("selected_institutions", []))
            workers = self.worker_snapshot()
            selected_workers = [workers[key] for key in selected if key in workers]
            statuses = [str(worker.get("status") or STATUS_IDLE) for worker in selected_workers]
            waiting_statuses = {STATUS_WAITING_FOR_LOGIN, STATUS_WAITING_FOR_USER_ACTION}
            if self.citi_waiting_for_code:
                status = STATUS_WAITING_FOR_USER_ACTION
                institution = "citi"
                message = self.worker_results.get("citi", {}).get("message") or "Enter the Citi 6-digit verification code to continue sync."
            elif self.vanguard_waiting_for_code:
                status = STATUS_WAITING_FOR_USER_ACTION
                institution = "vanguard"
                message = self.worker_results.get("vanguard", {}).get("message") or "Enter the Vanguard 6-digit verification code to continue sync."
            elif not finished and self.active_workers:
                status = STATUS_RUNNING
                institution = "multiple"
                running_labels = [self.institutions[key] for key in sorted(self.active_workers)]
                message = f"Syncing {', '.join(running_labels)}."
            elif any(status == STATUS_FAILED for status in statuses):
                status = STATUS_FAILED
                institution = "multiple"
                failed = [worker["label"] for worker in selected_workers if worker.get("status") == STATUS_FAILED]
                message = f"Sync finished with issues: {', '.join(failed)}."
            elif any(status in waiting_statuses for status in statuses):
                status = STATUS_WAITING_FOR_USER_ACTION
                institution = "multiple"
                waiting = [worker["label"] for worker in selected_workers if worker.get("status") in waiting_statuses]
                message = f"Sync needs attention: {', '.join(waiting)}."
            else:
                status = STATUS_SUCCESS
                institution = "multiple"
                message = f"Sync completed for {', '.join(self.institutions[key] for key in selected)}."
            state = base_result("latest", institution)
            state["status"] = status
            state["success"] = status == STATUS_SUCCESS
            state["message"] = message
            state["selected_institutions"] = selected
            state["workers"] = workers
            state["finished_at"] = utc_now() if finished or status in {STATUS_SUCCESS, STATUS_FAILED} else None
            if self.batch_state:
                state["started_at"] = self.batch_state.get("started_at", state["started_at"])
            self.batch_state = state
            return state

    def update_worker(self, institution: str, status: str, message: str | None = None) -> None:
        with self.state_lock:
            current = dict(self.worker_results.get(institution, {}))
            current["status"] = status
            current["message"] = message
            self.worker_results[institution] = current

    def run_worker(self, institution: str) -> None:
        self.update_worker(institution, STATUS_RUNNING, f"{self.institutions[institution]} sync is running.")
        try:
            if institution == "citizens":
                result = run_citizens_sync()
            elif institution == "amex":
                result = run_amex_sync()
            elif institution == "chase":
                result = run_chase_sync()
            elif institution == "citi":
                result = run_citi_sync(self.wait_for_citi_code, self.mark_citi_verified)
            elif institution == "vanguard":
                result = run_vanguard_sync(self.wait_for_vanguard_code, self.mark_vanguard_verified)
            else:
                raise ValueError(f"Unsupported sync institution: {institution}")
        except Exception as exc:
            result = {"status": STATUS_FAILED, "message": f"{self.institutions[institution]} sync failed: {exc}"}
        finally:
            with self.state_lock:
                self.active_workers.discard(institution)
        self.update_worker(institution, str(result.get("status") or STATUS_FAILED), str(result.get("message") or "Sync finished."))

    def start_many(self, institutions: list[str] | None = None) -> tuple[bool, dict[str, Any]]:
        selected = self.normalize_institutions(institutions)
        if not self.lock.acquire(blocking=False):
            return False, {"status": STATUS_RUNNING, "message": "A sync is already running."}

        self.citi_code = None
        self.citi_waiting_for_code = False
        self.citi_code_event.clear()
        self.vanguard_code = None
        self.vanguard_waiting_for_code = False
        self.vanguard_code_event.clear()
        started_at = utc_now()
        with self.state_lock:
            self.active_workers = set(selected)
            self.worker_results = {
                key: {"status": STATUS_IDLE, "message": None}
                for key in self.institutions
            }
            for key in selected:
                self.worker_results[key] = {"status": STATUS_RUNNING, "message": f"{self.institutions[key]} sync is queued."}
            self.batch_state = base_result("latest", "multiple")
            self.batch_state.update(
                {
                    "status": STATUS_RUNNING,
                    "success": False,
                    "message": f"Starting sync for {', '.join(self.institutions[key] for key in selected)}.",
                    "started_at": started_at,
                    "finished_at": None,
                    "selected_institutions": selected,
                    "workers": self.worker_snapshot(),
                }
            )
            write_json(SYNC_STATE_PATH, self.batch_state)

        def coordinator() -> None:
            threads = [
                threading.Thread(target=self.run_worker, args=(institution,), daemon=True)
                for institution in selected
            ]
            try:
                for worker in threads:
                    worker.start()
                for worker in threads:
                    worker.join()
                final_state = self.aggregate_batch_state(finished=True)
                write_json(SYNC_STATE_PATH, final_state)
            finally:
                self.thread = None
                self.citi_waiting_for_code = False
                self.vanguard_waiting_for_code = False
                self.lock.release()

        self.thread = threading.Thread(target=coordinator, daemon=True)
        self.thread.start()
        return True, {"status": STATUS_RUNNING, "message": f"Sync started for {', '.join(self.institutions[key] for key in selected)}."}

    def start_citizens(self) -> tuple[bool, dict[str, Any]]:
        return self.start_many(["citizens"])

    def setup_citizens(self) -> dict[str, Any]:
        return open_citizens_browser(terminate_existing=False)

    def start_amex(self) -> tuple[bool, dict[str, Any]]:
        return self.start_many(["amex"])

    def setup_amex(self) -> dict[str, Any]:
        return open_amex_browser(terminate_existing=False)

    def start_chase(self) -> tuple[bool, dict[str, Any]]:
        return self.start_many(["chase"])

    def setup_chase(self) -> dict[str, Any]:
        return open_chase_browser(terminate_existing=False)

    def wait_for_citi_code(self, result: dict[str, Any]) -> str | None:
        self.citi_waiting_for_code = True
        self.update_worker("citi", STATUS_WAITING_FOR_USER_ACTION, result.get("message") or "Enter the Citi 6-digit verification code to continue sync.")
        self.citi_code_event.wait(timeout=600)
        code = self.citi_code
        self.citi_code = None
        self.citi_code_event.clear()
        self.update_worker("citi", STATUS_WAITING_FOR_USER_ACTION, "Verifying Citi code.")
        return code

    def mark_citi_verified(self) -> None:
        self.citi_waiting_for_code = False
        self.update_worker("citi", STATUS_RUNNING, "Citi verification accepted. Sync is continuing.")

    def submit_citi_code(self, code: str) -> dict[str, Any]:
        normalized = normalize_space(code)
        if not re.fullmatch(r"\d{6}", normalized):
            return {"status": STATUS_FAILED, "message": "Enter a 6-digit Citi verification code."}
        self.citi_code = normalized
        self.citi_code_event.set()
        return {"status": STATUS_RUNNING, "message": "Citi verification code submitted. Sync is continuing."}

    def start_citi(self) -> tuple[bool, dict[str, Any]]:
        return self.start_many(["citi"])

    def setup_citi(self) -> dict[str, Any]:
        return open_citi_browser(terminate_existing=False)

    def start_vanguard(self) -> tuple[bool, dict[str, Any]]:
        return self.start_many(["vanguard"])

    def setup_vanguard(self) -> dict[str, Any]:
        return open_vanguard_browser(terminate_existing=False)

    def wait_for_vanguard_code(self, result: dict[str, Any]) -> str | None:
        self.vanguard_waiting_for_code = True
        self.update_worker("vanguard", STATUS_WAITING_FOR_USER_ACTION, result.get("message") or "Enter the Vanguard 6-digit verification code to continue sync.")
        self.vanguard_code_event.wait(timeout=600)
        code = self.vanguard_code
        self.vanguard_code = None
        self.vanguard_code_event.clear()
        self.update_worker("vanguard", STATUS_WAITING_FOR_USER_ACTION, "Verifying Vanguard code.")
        return code

    def mark_vanguard_verified(self) -> None:
        self.vanguard_waiting_for_code = False
        self.update_worker("vanguard", STATUS_RUNNING, "Vanguard verification accepted. Sync is continuing.")

    def submit_vanguard_code(self, code: str) -> dict[str, Any]:
        normalized = normalize_space(code)
        if not re.fullmatch(r"\d{6}", normalized):
            return {"status": STATUS_FAILED, "message": "Enter a 6-digit Vanguard verification code."}
        self.vanguard_code = normalized
        self.vanguard_code_event.set()
        return {"status": STATUS_RUNNING, "message": "Vanguard verification code submitted. Sync is continuing."}

    def setup_institutions(self, institutions: list[str] | None = None) -> dict[str, Any]:
        selected = self.normalize_institutions(institutions)
        openers = {
            "citizens": self.setup_citizens,
            "amex": self.setup_amex,
            "chase": self.setup_chase,
            "citi": self.setup_citi,
            "vanguard": self.setup_vanguard,
        }
        results: dict[str, Any] = {}
        for institution in selected:
            try:
                results[institution] = openers[institution]()
            except Exception as exc:
                results[institution] = {"status": STATUS_FAILED, "message": str(exc)}
        return {
            "status": STATUS_SUCCESS,
            "message": f"Opened login browsers for {', '.join(self.institutions[key] for key in selected)}.",
            "results": results,
        }

    def status(self) -> dict[str, Any]:
        ensure_sync_tables()
        with self.state_lock:
            has_active_batch = bool(self.batch_state and (self.active_workers or self.lock.locked()))
        if has_active_batch:
            state = self.aggregate_batch_state(finished=False)
        else:
            state = self.batch_state or read_json(
                SYNC_STATE_PATH,
                {
                    "success": True,
                    "status": STATUS_IDLE,
                    "institution": None,
                    "mode": None,
                    "message": "No sync has run yet.",
                    "started_at": None,
                    "finished_at": None,
                    "data": {"balances": [], "transactions": [], "transaction_exports": []},
                    "errors": [],
                    "screenshots": [],
                },
            )
            if state.get("status") == STATUS_RUNNING:
                state["status"] = STATUS_FAILED
                state["success"] = False
                state["finished_at"] = utc_now()
                state["message"] = "Previous sync stopped before finishing."
                state["workers"] = self.worker_snapshot()
                write_json(SYNC_STATE_PATH, state)
        with connection() as conn:
            runs = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT id, institution, mode, status, started_at, finished_at, message
                    FROM sync_runs
                    ORDER BY started_at DESC
                    LIMIT 100
                    """
                )
            ]
            balances = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT account_name, account_type, balance_type, amount, currency, captured_at, source, raw_text
                    FROM sync_balances
                    ORDER BY captured_at DESC, id DESC
                    LIMIT 100
                    """
                )
            ]
            transactions = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT t.transaction_date, t.description, t.amount, t.category, t.imported_at, a.name AS account_name
                    FROM transactions t
                    JOIN accounts a ON a.id = t.account_id
                    WHERE t.source_file LIKE 'sync:%'
                    ORDER BY t.transaction_date DESC, t.id DESC
                    LIMIT 200
                    """
                )
            ]
        return {"state": state, "running": self.lock.locked(), "runs": runs, "balances": balances, "transactions": transactions}


sync_manager = SyncManager()
