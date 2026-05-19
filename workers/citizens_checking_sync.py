from __future__ import annotations

import argparse
import csv
import hashlib
import re
import shutil
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from selenium.common.exceptions import StaleElementReferenceException, TimeoutException, WebDriverException
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.remote.webelement import WebElement
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import Select
from selenium.webdriver.support.ui import WebDriverWait

import config
from automation_audit import log_event
from automation_runtime import (
    chrome_debugger_available,
    compute_file_hash,
    create_driver,
    detect_possible_login_or_mfa,
    open_manual_chrome_profile,
    read_json,
    safe_get,
    save_screenshot,
    wait_for_downloads,
    wait_for_page_ready,
    write_result,
)


WORKER_NAME = "citizens_checking_sync"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def base_result(started_at: str) -> dict[str, Any]:
    return {
        "success": False,
        "status": config.STATUS_FAILED,
        "site": config.CITIZENS_SITE_KEY,
        "worker": WORKER_NAME,
        "message": "",
        "started_at": started_at,
        "finished_at": None,
        "data": {"balances": [], "documents": [], "downloads": [], "transactions": [], "transaction_exports": []},
        "errors": [],
        "screenshots": [],
    }


def finish_result(result: dict[str, Any], success: bool, status: str, message: str) -> dict[str, Any]:
    result["success"] = success
    result["status"] = status
    result["message"] = message
    result["finished_at"] = utc_now()
    write_result(result)
    return result


def get_db_connection() -> sqlite3.Connection:
    connection = sqlite3.connect(config.DATABASE_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def get_or_create_citizens_account() -> int:
    now = utc_now()
    with get_db_connection() as connection:
        row = connection.execute(
            "SELECT id FROM accounts WHERE site = ? AND account_type = ?",
            (config.CITIZENS_SITE_KEY, config.CITIZENS_ACCOUNT_TYPE),
        ).fetchone()
        if row:
            return int(row["id"])
        cursor = connection.execute(
            """
            INSERT INTO accounts (
                site, display_name, account_type, masked_account, enabled,
                supports_csv, supports_documents, chrome_profile_path,
                created_at, updated_at
            )
            VALUES (?, ?, ?, ?, 1, 0, 1, ?, ?, ?)
            """,
            (
                config.CITIZENS_SITE_KEY,
                config.CITIZENS_DISPLAY_NAME,
                config.CITIZENS_ACCOUNT_TYPE,
                None,
                str(config.CITIZENS_PROFILE_PATH),
                now,
                now,
            ),
        )
        return int(cursor.lastrowid)


def ensure_transactions_table() -> None:
    with get_db_connection() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER,
                site TEXT NOT NULL,
                transaction_date TEXT,
                posted_date TEXT,
                description TEXT NOT NULL,
                amount REAL,
                currency TEXT NOT NULL DEFAULT 'USD',
                debit_credit TEXT,
                status TEXT,
                category TEXT,
                source TEXT,
                source_hash TEXT NOT NULL,
                captured_at TEXT NOT NULL,
                raw_text TEXT,
                UNIQUE(account_id, source_hash),
                FOREIGN KEY (account_id) REFERENCES accounts(id)
            )
            """
        )


def parse_money(text: str) -> float | None:
    match = re.search(r"\$?\s*(-?\d{1,3}(?:,\d{3})*(?:\.\d{2})|-?\d+(?:\.\d{2}))", text)
    if not match:
        return None
    return float(match.group(1).replace(",", ""))


def record_balance(account_id: int, raw_text: str) -> dict[str, Any] | None:
    amount = parse_money(raw_text)
    if amount is None:
        return None
    captured_at = utc_now()
    with get_db_connection() as connection:
        connection.execute(
            """
            INSERT INTO balance_snapshots (
                account_id, balance_type, amount, currency, captured_at, source, raw_text
            )
            VALUES (?, ?, ?, 'USD', ?, ?, ?)
            """,
            (account_id, "current", amount, captured_at, "citizens_web", raw_text[:500]),
        )
    return {
        "account_id": account_id,
        "balance_type": "current",
        "amount": amount,
        "currency": "USD",
        "captured_at": captured_at,
        "raw_text": raw_text,
    }


def record_document(account_id: int, metadata: dict[str, Any]) -> None:
    with get_db_connection() as connection:
        connection.execute(
            """
            INSERT INTO documents (
                account_id, site, document_type, statement_name, received_date,
                account_label, local_path, file_hash, downloaded_at, source_url_optional
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                account_id,
                config.CITIZENS_SITE_KEY,
                metadata.get("document_type"),
                metadata.get("statement_name"),
                metadata.get("received_date"),
                metadata.get("account_label"),
                metadata.get("local_path"),
                metadata.get("file_hash"),
                metadata.get("downloaded_at") or utc_now(),
                metadata.get("source_url_optional"),
            ),
        )


def normalize_space(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def parse_transaction_date(text: str) -> str | None:
    match = re.search(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b", text)
    if not match:
        return None
    month = int(match.group(1))
    day = int(match.group(2))
    year_text = match.group(3)
    now = datetime.now()
    if year_text:
        year = int(year_text)
        if year < 100:
            year += 2000
    else:
        year = now.year
    try:
        parsed = datetime(year, month, day)
    except ValueError:
        return None
    if not year_text and parsed.date() > now.date():
        parsed = datetime(year - 1, month, day)
    return parsed.date().isoformat()


def parse_named_transaction_date(text: str) -> str | None:
    match = re.search(
        r"\b(january|february|march|april|may|june|july|august|september|october|november|december)\s+"
        r"(\d{1,2}),\s*(\d{4})\b",
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
    money_pattern = r"\(?-?\$?\s*(?:\d{1,3}(?:,\d{3})+|\d+)\.\d{2}\)?|\(?-?\$\s*(?:\d{1,3}(?:,\d{3})*|\d+)(?:\.\d{2})?\)?"
    for match in re.finditer(money_pattern, text):
        raw = match.group(0).strip()
        if "/" in raw:
            continue
        is_negative = raw.startswith("(") and raw.endswith(")")
        clean = raw.strip("()").replace("$", "").replace(",", "").replace(" ", "")
        try:
            value = float(clean)
        except ValueError:
            continue
        values.append(-abs(value) if is_negative else value)
    return values


def infer_transaction_amount(cells: list[str], raw_text: str) -> tuple[float | None, str | None]:
    lowered_cells = [cell.lower() for cell in cells]
    lowered_raw = raw_text.lower()
    money_cells = [(index, parse_money_values(cell)) for index, cell in enumerate(cells)]
    flattened = [(index, values[-1]) for index, values in money_cells if values]
    if not flattened:
        values = parse_money_values(raw_text)
        if not values:
            return (None, None)
        amount = values[0] if len(values) >= 2 else values[-1]
        return (amount, "credit" if amount > 0 else "debit")

    withdrawal_index = next(
        (index for index, text in enumerate(lowered_cells) if "withdrawal" in text or "debit" in text),
        None,
    )
    deposit_index = next(
        (index for index, text in enumerate(lowered_cells) if "deposit" in text or "credit" in text),
        None,
    )
    if withdrawal_index is not None and withdrawal_index < len(cells):
        values = parse_money_values(cells[withdrawal_index])
        if values:
            return (-abs(values[-1]), "debit")
    if deposit_index is not None and deposit_index < len(cells):
        values = parse_money_values(cells[deposit_index])
        if values:
            return (abs(values[-1]), "credit")

    if len(flattened) >= 2:
        amount_index, amount = flattened[-2]
    else:
        amount_index, amount = flattened[-1]

    if len(cells) >= 5 and amount > 0:
        if amount_index <= 2:
            return (-abs(amount), "debit")
        if amount_index == 3:
            return (abs(amount), "credit")
    if amount > 0 and any(
        marker in lowered_raw
        for marker in ["deposit", "credit", "payroll", "refund", "interest", "transfer from", "zelle from"]
    ):
        return (abs(amount), "credit")
    if amount > 0 and len(flattened) >= 2:
        return (-abs(amount), "debit")
    return (amount, "credit" if amount > 0 else "debit")


def infer_transaction_description(cells: list[str], raw_text: str) -> str:
    for cell in cells:
        text = normalize_space(cell)
        if not text:
            continue
        if parse_transaction_date(text):
            continue
        if parse_money_values(text):
            continue
        lowered = text.lower()
        if lowered in {"date", "description", "amount", "balance", "pending", "posted"}:
            continue
        return text[:300]
    without_money = re.sub(
        r"\(?-?\$\s*(?:\d{1,3}(?:,\d{3})*|\d+)(?:\.\d{2})?\)?"
        r"|\(?-?\s*(?:\d{1,3}(?:,\d{3})+|\d+)\.\d{2}\)?",
        " ",
        raw_text,
    )
    without_numeric_date = re.sub(r"\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b", " ", without_money)
    without_named_date = re.sub(
        r"\b(?:january|february|march|april|may|june|july|august|september|october|november|december)\s+"
        r"\d{1,2},\s*\d{4}\b",
        " ",
        without_numeric_date,
        flags=re.IGNORECASE,
    )
    without_status = re.sub(
        r"\b(?:preauthorized debit|authorized debit|pending|posted|debit|credit|deposit)\b",
        " ",
        without_named_date,
        flags=re.IGNORECASE,
    )
    return normalize_space(without_status)[:300] or normalize_space(raw_text)[:300]


def normalize_transaction_status(text: str) -> str:
    lowered = text.lower()
    if "pending" in lowered or "preauthorized" in lowered:
        return "pending"
    return "posted"


def transaction_source_hash(account_id: int, transaction: dict[str, Any]) -> str:
    key = "|".join(
        [
            str(account_id),
            transaction.get("transaction_date") or "",
            transaction.get("description") or "",
            "" if transaction.get("amount") is None else f"{transaction.get('amount'):.2f}",
            transaction.get("status") or "",
            transaction.get("raw_text") or "",
        ]
    )
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def record_transaction(account_id: int, transaction: dict[str, Any]) -> bool:
    ensure_transactions_table()
    captured_at = transaction.get("captured_at") or utc_now()
    source_hash = transaction_source_hash(account_id, transaction)
    with get_db_connection() as connection:
        cursor = connection.execute(
            """
            INSERT OR IGNORE INTO transactions (
                account_id, site, transaction_date, posted_date, description, amount,
                currency, debit_credit, status, category, source, source_hash,
                captured_at, raw_text
            )
            VALUES (?, ?, ?, ?, ?, ?, 'USD', ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                account_id,
                config.CITIZENS_SITE_KEY,
                transaction.get("transaction_date"),
                transaction.get("posted_date"),
                transaction.get("description") or "Unknown transaction",
                transaction.get("amount"),
                transaction.get("debit_credit"),
                transaction.get("status"),
                transaction.get("category"),
                transaction.get("source") or "citizens_visible_activity",
                source_hash,
                captured_at,
                transaction.get("raw_text"),
            ),
        )
    return cursor.rowcount > 0


def first_visible_text(driver, selectors: list[str]) -> str | None:
    for selector in selectors:
        try:
            elements = driver.find_elements(By.CSS_SELECTOR, selector)
        except WebDriverException:
            continue
        for element in elements:
            try:
                text = element.text.strip()
                if text and "$" in text:
                    return text
            except StaleElementReferenceException:
                continue
    return None


def scrape_current_checking_balance(driver) -> str | None:
    # TODO: Confirm these Citizens selectors periodically; they are based on the
    # authenticated account summary DOM observed during v1 setup.
    selectors = [
        "#product-group-checking-accounts .olb-c-accountSummary__aggregatedBalance",
        "#account-list-checking-accounts .olb-c-accountItem__balance",
        "[data-testid*='balance' i]",
        "[class*='balance' i]",
        "[aria-label*='balance' i]",
        "[id*='balance' i]",
    ]
    return first_visible_text(driver, selectors)


def click_element(driver, element: WebElement) -> bool:
    try:
        if not element.is_displayed() or not element.is_enabled():
            return False
        try:
            element.location_once_scrolled_into_view
            element.click()
        except WebDriverException:
            driver.execute_script("arguments[0].click();", element)
        wait_for_page_ready(driver)
        return True
    except WebDriverException:
        return False


def click_by_text(driver, labels: list[str], exact: bool = False) -> bool:
    script = """
        const labels = arguments[0].map((label) => label.replace(/\\s+/g, ' ').trim().toLowerCase());
        const exact = arguments[1];
        const nodes = Array.from(document.querySelectorAll(
            'a, button, label, [role="button"], [tabindex], input, [aria-label], [title]'
        ));
        function visible(node) {
            const style = window.getComputedStyle(node);
            const rect = node.getBoundingClientRect();
            return style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0;
        }
        function nodeText(node) {
            const label = node.id ? document.querySelector(`label[for="${CSS.escape(node.id)}"]`) : null;
            return [
                node.innerText,
                node.textContent,
                node.getAttribute('aria-label'),
                node.getAttribute('title'),
                node.value,
                label && label.innerText,
                label && label.textContent
            ].filter(Boolean).join(' ').replace(/\\s+/g, ' ').trim().toLowerCase();
        }
        for (const node of nodes) {
            if (!visible(node)) continue;
            const text = nodeText(node);
            if (!text) continue;
            for (const label of labels) {
                if ((exact && text === label) || (!exact && text.includes(label))) return node;
            }
        }
        return null;
    """
    try:
        element = driver.execute_script(script, labels, exact)
    except WebDriverException:
        element = None
    return bool(element and click_element(driver, element))


def visible_action_labels(driver, limit: int = 30) -> list[str]:
    script = """
        const nodes = Array.from(document.querySelectorAll('a, button, label, [role="button"], [tabindex], input, [aria-label], [title]'));
        function visible(node) {
            const style = window.getComputedStyle(node);
            const rect = node.getBoundingClientRect();
            return style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0;
        }
        const labels = [];
        for (const node of nodes) {
            if (!visible(node)) continue;
            const text = [
                node.innerText,
                node.textContent,
                node.getAttribute('aria-label'),
                node.getAttribute('title'),
                node.value
            ].filter(Boolean).join(' ').replace(/\\s+/g, ' ').trim();
            if (text) labels.push(text);
        }
        return labels.slice(0, arguments[0]);
    """
    try:
        labels = driver.execute_script(script, limit)
    except WebDriverException:
        return []
    sanitized: list[str] = []
    for label in labels or []:
        text = normalize_space(str(label))
        text = re.sub(r"\$\s*-?\d[\d,]*(?:\.\d{2})?", "$[amount]", text)
        text = re.sub(r"\*\d{2,}", "*[account]", text)
        text = re.sub(r"\b\d{4,}\b", "[number]", text)
        if text and text not in sanitized:
            sanitized.append(text[:120])
    return sanitized


def page_diagnostic(driver) -> str:
    try:
        url = driver.current_url
    except WebDriverException:
        url = "unknown"
    try:
        title = driver.title
    except WebDriverException:
        title = "unknown"
    labels = visible_action_labels(driver, limit=20)
    label_text = "; ".join(labels[:12])
    return f"url={url[:140]} title={title[:80]} visible_actions={label_text}"


def has_filled_password_field(driver) -> bool:
    try:
        password_fields = driver.find_elements(By.CSS_SELECTOR, "input[type='password']")
    except WebDriverException:
        return False
    for field in password_fields:
        try:
            if field.is_displayed() and field.get_attribute("value"):
                return True
        except WebDriverException:
            continue
    return False


def login_form_has_empty_credentials(driver) -> bool:
    try:
        password_fields = driver.find_elements(By.CSS_SELECTOR, "input[type='password']")
    except WebDriverException:
        return False
    visible_passwords = []
    for field in password_fields:
        try:
            if field.is_displayed() and field.is_enabled():
                visible_passwords.append(field)
        except WebDriverException:
            continue
    if not visible_passwords:
        return False
    return not any((field.get_attribute("value") or "").strip() for field in visible_passwords)


def is_login_page(driver) -> bool:
    try:
        page_text = driver.find_element(By.TAG_NAME, "body").text.lower()
        current_url = driver.current_url.lower()
    except WebDriverException:
        return False
    return (
        "login" in current_url
        or "log in" in page_text
        or ("user id" in page_text and "password" in page_text)
    )


def nudge_chrome_autofill(driver) -> None:
    selectors = [
        "input[type='password']",
        "input[name*='user' i]",
        "input[id*='user' i]",
        "input[name*='login' i]",
        "input[id*='login' i]",
    ]
    for selector in selectors:
        try:
            fields = driver.find_elements(By.CSS_SELECTOR, selector)
        except WebDriverException:
            continue
        for field in fields:
            try:
                if field.is_displayed() and field.is_enabled():
                    try:
                        field.click()
                    except WebDriverException:
                        driver.execute_script("arguments[0].focus(); arguments[0].click();", field)
                    time.sleep(0.3)
            except WebDriverException:
                continue


def commit_autofilled_login_fields(driver) -> bool:
    script = """
        const fields = Array.from(document.querySelectorAll(
            '#form-user-id, #form-password, input[type="password"], input[name*="user" i], input[id*="user" i]'
        ));
        function visible(node) {
            const style = window.getComputedStyle(node);
            const rect = node.getBoundingClientRect();
            return style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0;
        }
        let committed = false;
        for (const field of fields) {
            if (!visible(field) || field.disabled || field.readOnly) continue;
            const value = field.value || '';
            if (!value) continue;
            field.focus();
            const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
            setter.call(field, value);
            field.dispatchEvent(new KeyboardEvent('keydown', {bubbles: true, cancelable: true}));
            field.dispatchEvent(new Event('input', {bubbles: true}));
            field.dispatchEvent(new Event('change', {bubbles: true}));
            field.dispatchEvent(new KeyboardEvent('keyup', {bubbles: true, cancelable: true}));
            field.blur();
            committed = true;
        }
        return committed;
    """
    try:
        committed = bool(driver.execute_script(script))
    except WebDriverException:
        committed = False

    for selector in ["#form-user-id", "#form-password"]:
        try:
            for field in driver.find_elements(By.CSS_SELECTOR, selector):
                if field.is_displayed() and field.is_enabled() and field.get_attribute("value"):
                    try:
                        field.click()
                    except WebDriverException:
                        driver.execute_script("arguments[0].focus(); arguments[0].click();", field)
                    field.send_keys(Keys.END)
                    field.send_keys(" ")
                    field.send_keys(Keys.BACKSPACE)
                    committed = True
        except WebDriverException:
            continue
    return committed


def click_login_button(driver) -> bool:
    selectors = [
        "#login-btn",
        ".olb-c-login__loginBtn",
        "button[type='submit']",
        "input[type='submit']",
        "button[id*='login' i]",
        "button[name*='login' i]",
        "input[id*='login' i]",
        "input[name*='login' i]",
    ]
    for selector in selectors:
        try:
            elements = driver.find_elements(By.CSS_SELECTOR, selector)
        except WebDriverException:
            continue
        for element in elements:
            if click_element(driver, element):
                return True
    login_labels = ["Log In", "Login", "Sign In", "Sign On"]
    if click_by_text(driver, login_labels, exact=True) or click_by_text(driver, login_labels, exact=False):
        return True
    return False


def submit_login_page(driver) -> bool:
    if not is_login_page(driver):
        return False
    nudge_chrome_autofill(driver)
    commit_autofilled_login_fields(driver)
    deadline = time.time() + 5
    while time.time() < deadline:
        if has_filled_password_field(driver):
            commit_autofilled_login_fields(driver)
            break
        time.sleep(0.5)
    return click_login_button(driver)


def has_verification_code_field(driver) -> bool:
    selectors = [
        "input[autocomplete='one-time-code']",
        "input#cbds-numberInput",
        "input[type='number']",
        "input[inputmode='decimal']",
        "input[inputmode='numeric']",
        "input[name*='code' i]",
        "input[id*='code' i]",
        "input[aria-label*='code' i]",
    ]
    for selector in selectors:
        try:
            for field in driver.find_elements(By.CSS_SELECTOR, selector):
                if field.is_displayed():
                    return True
        except WebDriverException:
            continue
    return False


def find_verification_code_field(driver) -> WebElement | None:
    selectors = [
        "input[autocomplete='one-time-code']",
        "input#cbds-numberInput",
        "input[type='number']",
        "input[inputmode='numeric']",
        "input[inputmode='decimal']",
        "input[name*='code' i]",
        "input[id*='code' i]",
        "input[aria-label*='code' i]",
        "input[type='tel']",
        "input[type='text']",
    ]
    for selector in selectors:
        try:
            fields = driver.find_elements(By.CSS_SELECTOR, selector)
        except WebDriverException:
            continue
        for field in fields:
            try:
                if not field.is_displayed() or not field.is_enabled():
                    continue
                label_text = normalize_space(
                    " ".join(
                        [
                            field.get_attribute("name") or "",
                            field.get_attribute("id") or "",
                            field.get_attribute("aria-label") or "",
                            field.get_attribute("placeholder") or "",
                        ]
                    )
                ).lower()
                if "code" in label_text or selector in {
                    "input[autocomplete='one-time-code']",
                    "input#cbds-numberInput",
                    "input[type='number']",
                    "input[inputmode='numeric']",
                    "input[inputmode='decimal']",
                    "input[type='tel']",
                }:
                    return field
            except WebDriverException:
                continue
    return None


def pending_mfa_code() -> str | None:
    payload = read_json(config.CITIZENS_MFA_CODE_PATH, {})
    code = re.sub(r"\D+", "", str(payload.get("code", ""))) if isinstance(payload, dict) else ""
    return code if len(code) == 6 else None


def clear_pending_mfa_code() -> None:
    try:
        config.CITIZENS_MFA_CODE_PATH.unlink()
    except OSError:
        pass


def set_verification_code_value(driver, field: WebElement, code: str) -> bool:
    try:
        field.click()
        field.send_keys(Keys.CONTROL, "a")
        field.send_keys(Keys.BACKSPACE)
    except WebDriverException:
        pass
    script = """
        const input = arguments[0];
        const value = arguments[1];
        const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
        input.focus();
        setter.call(input, value);
        input.dispatchEvent(new KeyboardEvent('keydown', {bubbles: true, cancelable: true}));
        input.dispatchEvent(new Event('input', {bubbles: true}));
        input.dispatchEvent(new Event('change', {bubbles: true}));
        input.dispatchEvent(new KeyboardEvent('keyup', {bubbles: true, cancelable: true}));
        input.blur();
        return input.value === value;
    """
    try:
        return bool(driver.execute_script(script, field, code))
    except WebDriverException:
        try:
            field.clear()
            field.send_keys(code)
            return (field.get_attribute("value") or "") == code
        except WebDriverException:
            return False


def click_confirm_code_button(driver) -> bool:
    script = """
        function visible(node) {
            const style = window.getComputedStyle(node);
            const rect = node.getBoundingClientRect();
            return style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0;
        }
        function enabled(node) {
            return !node.disabled && node.getAttribute('aria-disabled') !== 'true';
        }
        function textOf(node) {
            return [
                node.innerText,
                node.textContent,
                node.value,
                node.getAttribute('aria-label'),
                node.getAttribute('title'),
                node.getAttribute('id')
            ].filter(Boolean).join(' ').replace(/\\s+/g, ' ').trim().toLowerCase();
        }
        const nodes = Array.from(document.querySelectorAll('button, input[type="button"], input[type="submit"], a, [role="button"]'));
        const confirm = nodes.find((node) => visible(node) && enabled(node) && textOf(node) === 'confirm code')
            || nodes.find((node) => visible(node) && enabled(node) && /confirm code|verify|submit/.test(textOf(node)));
        if (!confirm) return false;
        confirm.scrollIntoView({block: 'center'});
        confirm.dispatchEvent(new MouseEvent('mousedown', {bubbles: true, cancelable: true, view: window}));
        confirm.dispatchEvent(new MouseEvent('mouseup', {bubbles: true, cancelable: true, view: window}));
        confirm.click();
        return true;
    """
    try:
        if bool(driver.execute_script(script)):
            return True
    except WebDriverException:
        pass
    return click_by_text(driver, ["Confirm Code", "Verify", "Submit"], exact=True) or click_by_text(
        driver,
        ["Confirm Code", "Verify", "Submit"],
        exact=False,
    )


def code_entry_page_still_visible(driver) -> bool:
    return has_verification_code_field(driver) or wait_for_code_entry_page(driver, timeout=1)


def submit_pending_mfa_code(driver) -> bool:
    code = pending_mfa_code()
    if not code:
        return False
    field = find_verification_code_field(driver)
    if not field:
        return False
    try:
        if not set_verification_code_value(driver, field, code):
            return False
        submitted = click_confirm_code_button(driver)
        if not submitted:
            field.send_keys(Keys.ENTER)
        wait_for_page_ready(driver)
        try:
            WebDriverWait(driver, 15).until(lambda active_driver: not code_entry_page_still_visible(active_driver))
        except TimeoutException:
            return False
        clear_pending_mfa_code()
        return True
    except WebDriverException:
        return False


def select_text_message_option(driver) -> bool:
    script = """
        const candidates = Array.from(document.querySelectorAll('input[type="radio"], label, [role="radio"]'));
        function visible(node) {
            const style = window.getComputedStyle(node);
            const rect = node.getBoundingClientRect();
            return style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0;
        }
        function textOf(node) {
            const label = node.id ? document.querySelector(`label[for="${CSS.escape(node.id)}"]`) : null;
            return [
                node.innerText,
                node.textContent,
                node.value,
                node.getAttribute('aria-label'),
                node.getAttribute('id'),
                label && label.innerText
            ].filter(Boolean).join(' ').replace(/\\s+/g, ' ').trim().toLowerCase();
        }
        for (const node of candidates) {
            if (!visible(node)) continue;
            const text = textOf(node);
            if (text.includes('text message') || text.includes('textmessage') || text.includes('sms')) {
                return node;
            }
        }
        return null;
    """
    try:
        element = driver.execute_script(script)
    except WebDriverException:
        element = None
    return bool(element and click_element(driver, element))


def click_next_button(driver) -> bool:
    script = """
        const nodes = Array.from(document.querySelectorAll('button, input[type="button"], input[type="submit"], a, [role="button"]'));
        function visible(node) {
            const style = window.getComputedStyle(node);
            const rect = node.getBoundingClientRect();
            return style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0;
        }
        function textOf(node) {
            return [
                node.innerText,
                node.textContent,
                node.value,
                node.getAttribute('aria-label'),
                node.getAttribute('title')
            ].filter(Boolean).join(' ').replace(/\\s+/g, ' ').trim().toLowerCase();
        }
        for (const node of nodes) {
            if (!visible(node)) continue;
            const text = textOf(node);
            if (text === 'next' || text.includes('send code') || text.includes('continue')) return node;
        }
        return null;
    """
    try:
        element = driver.execute_script(script)
    except WebDriverException:
        element = None
    return bool(element and click_element(driver, element))


def wait_for_code_entry_page(driver, timeout: int = 20) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if has_verification_code_field(driver):
            return True
        try:
            page_text = driver.find_element(By.TAG_NAME, "body").text.lower()
            if "enter your 6-digit code" in page_text or "confirm code" in page_text:
                return True
        except WebDriverException:
            pass
        time.sleep(0.5)
    return False


def click_mfa_next_button(driver) -> bool:
    script = """
        function visible(node) {
            const style = window.getComputedStyle(node);
            const rect = node.getBoundingClientRect();
            return style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0;
        }
        function enabled(node) {
            return !node.disabled && node.getAttribute('aria-disabled') !== 'true';
        }
        function textOf(node) {
            return [
                node.innerText,
                node.textContent,
                node.value,
                node.getAttribute('aria-label'),
                node.getAttribute('title'),
                node.getAttribute('id')
            ].filter(Boolean).join(' ').replace(/\\s+/g, ' ').trim().toLowerCase();
        }
        const nodes = Array.from(document.querySelectorAll('button, input[type="button"], input[type="submit"], a, [role="button"]'));
        const next = nodes.find((node) => visible(node) && enabled(node) && /^next\\b/.test(textOf(node)))
            || nodes.find((node) => visible(node) && enabled(node) && /send code|continue/.test(textOf(node)));
        if (!next) return false;
        next.scrollIntoView({block: 'center'});
        next.dispatchEvent(new MouseEvent('mousedown', {bubbles: true, cancelable: true, view: window}));
        next.dispatchEvent(new MouseEvent('mouseup', {bubbles: true, cancelable: true, view: window}));
        next.click();
        return true;
    """
    try:
        return bool(driver.execute_script(script))
    except WebDriverException:
        return False


def request_text_message_code(driver) -> bool:
    if wait_for_code_entry_page(driver, timeout=1):
        return True
    try:
        selected = bool(
            driver.execute_script(
                """
                function visible(node) {
                    const style = window.getComputedStyle(node);
                    const rect = node.getBoundingClientRect();
                    return style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0;
                }
                function textOf(node) {
                    const label = node.id ? document.querySelector(`label[for="${CSS.escape(node.id)}"]`) : null;
                    return [
                        node.innerText,
                        node.textContent,
                        node.value,
                        node.getAttribute('aria-label'),
                        node.getAttribute('title'),
                        node.getAttribute('id'),
                        label && label.innerText
                    ].filter(Boolean).join(' ').replace(/\\s+/g, ' ').trim().toLowerCase();
                }
                const radios = Array.from(document.querySelectorAll('input[type="radio"], [role="radio"], label'));
                const radio = document.querySelector('#cbds-radioButtonGroup-radioButton-text')
                    || radios.find((node) => textOf(node).includes('textmessage') || textOf(node).includes('text message'));
                if (!radio || !visible(radio)) return false;
                const input = radio.matches('label') && radio.getAttribute('for')
                    ? document.getElementById(radio.getAttribute('for'))
                    : radio;
                radio.scrollIntoView({block: 'center'});
                radio.click();
                if (input && 'checked' in input) {
                    input.checked = true;
                    input.dispatchEvent(new Event('input', {bubbles: true}));
                    input.dispatchEvent(new Event('change', {bubbles: true}));
                }
                return true;
                """
            )
        )
    except WebDriverException:
        selected = False
    if not selected:
        selected = select_text_message_option(driver)
    if not selected:
        return False

    try:
        WebDriverWait(driver, 8).until(lambda active_driver: click_mfa_next_button(active_driver))
        clicked_next = True
    except TimeoutException:
        clicked_next = click_next_button(driver)
    if not clicked_next:
        return False
    wait_for_page_ready(driver, timeout=5)
    return wait_for_code_entry_page(driver)


def continue_after_login_submit(driver) -> str | None:
    deadline = time.time() + 12
    while time.time() < deadline:
        wait_for_page_ready(driver, timeout=3)
        if has_verification_code_field(driver):
            return None
        next_action = advance_citizens_authentication(driver, allow_login_submit=False)
        if next_action and next_action != "login_submitted":
            return next_action
        detection = detect_possible_login_or_mfa(driver)
        if detection["detected"] and detection.get("status") == config.STATUS_WAITING_FOR_MFA:
            return "text_mfa_requested" if "text message" in detection.get("message", "").lower() else None
        try:
            if "citizensbankonline.com/olb-root/home" in driver.current_url.lower():
                return None
        except WebDriverException:
            return None
        time.sleep(1)
    try:
        page_text = driver.find_element(By.TAG_NAME, "body").text.lower()
        if is_login_page(driver) and ("is required" in page_text or "required" in page_text):
            return None
    except WebDriverException:
        pass
    return "login_submitted"


def advance_citizens_authentication(driver, allow_login_submit: bool = True) -> str | None:
    try:
        page_text = driver.find_element(By.TAG_NAME, "body").text.lower()
    except WebDriverException:
        return None

    if has_verification_code_field(driver):
        return None

    if allow_login_submit and submit_login_page(driver):
        return "login_submitted"

    mfa_text = any(
        term in page_text
        for term in [
            "verify your identity",
            "verification code",
            "security code",
            "one-time",
            "one time",
            "text message",
            "mobile phone",
        ]
    )
    if not mfa_text:
        return None

    if request_text_message_code(driver):
        return "text_mfa_requested"
    return None


def resume_payload(transactions_only: bool, statement_range: str) -> dict[str, Any]:
    return {"resume": {"transactions_only": transactions_only, "statement_range": statement_range}}


def authentication_wait_detection(action: str, transactions_only: bool, statement_range: str) -> dict[str, Any]:
    if action == "login_submitted":
        return {
            "detected": True,
            "status": config.STATUS_WAITING_FOR_USER_ACTION,
            "message": "Login was submitted. The automation will continue when Citizens shows the next step.",
            "reasons": ["login submitted"],
            "data": resume_payload(transactions_only, statement_range),
        }
    return {
        "detected": True,
        "status": config.STATUS_WAITING_FOR_MFA,
        "message": "Requested a Citizens text-message verification code. Enter the 6-digit code in this dashboard to continue.",
        "reasons": ["text message verification requested"],
        "data": resume_payload(transactions_only, statement_range),
    }


def clarify_login_detection(driver, detection: dict[str, Any]) -> dict[str, Any]:
    if detection.get("status") != config.STATUS_WAITING_FOR_LOGIN:
        return detection
    if not login_form_has_empty_credentials(driver):
        return detection
    return {
        **detection,
        "message": (
            "Citizens login fields are empty in the automation Chrome profile. "
            "Save the Citizens username/password in the opened Chrome profile, then click Sync Citizens again."
        ),
        "reasons": [*detection.get("reasons", []), "automation profile autofill is empty"],
    }


def resolve_mfa_method_detection(driver, detection: dict[str, Any]) -> dict[str, Any]:
    if detection.get("status") != config.STATUS_WAITING_FOR_MFA:
        return detection
    if has_verification_code_field(driver) or wait_for_code_entry_page(driver, timeout=1):
        return {
            **detection,
            "message": "Requested a Citizens text-message verification code. Enter the 6-digit code in this dashboard to continue.",
            "reasons": [*detection.get("reasons", []), "verification code entry visible"],
        }
    if request_text_message_code(driver):
        return {
            **detection,
            "message": "Requested a Citizens text-message verification code. Enter the 6-digit code in this dashboard to continue.",
            "reasons": [*detection.get("reasons", []), "text message verification requested"],
        }
    return {
        **detection,
        "status": config.STATUS_WAITING_FOR_USER_ACTION,
        "message": (
            "Citizens is showing the verification-method screen, but automation could not press Next. "
            "Select Text message and press Next in the opened browser, then click Sync Citizens again."
        ),
        "reasons": [*detection.get("reasons", []), "verification method screen still visible"],
    }


def navigate_to_recent_activity(driver) -> bool:
    if "home/accounts" not in driver.current_url.lower():
        safe_get(driver, config.CITIZENS_HOME_URL)

    moved = click_checking_account(driver)
    for label in ["transactions", "recent transactions", "recent activity", "account activity", "activity"]:
        if click_by_text(driver, [label], exact=True) or click_read_only_navigation(driver, [label]):
            return True
    return moved


def click_checking_account(driver) -> bool:
    exact_labels = ["Checking", "Checking ›", "Checking >"]
    if click_by_text(driver, exact_labels, exact=True):
        return True

    xpath = (
        "//*[self::a or self::button or @role='button']"
        "[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'checking')]"
        "[not(contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'statement'))]"
        "[not(contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'manage card'))]"
    )
    try:
        elements = driver.find_elements(By.XPATH, xpath)
    except WebDriverException:
        return False
    for element in elements:
        if click_element(driver, element):
            return True
    return False


def candidate_transaction_rows(driver) -> list[WebElement]:
    selectors = [
        "table tbody tr",
        "[role='row']",
        "[data-testid*='transaction' i]",
        "[class*='transaction' i]",
        "[class*='activity' i] li",
        "[class*='activity' i] [class*='row' i]",
    ]
    rows: list[WebElement] = []
    seen: set[str] = set()
    for selector in selectors:
        try:
            elements = driver.find_elements(By.CSS_SELECTOR, selector)
        except WebDriverException:
            continue
        for element in elements:
            try:
                text = normalize_space(element.text)
                if not text or text in seen:
                    continue
                seen.add(text)
                rows.append(element)
            except StaleElementReferenceException:
                continue
    return rows


def parse_transaction_row(row: WebElement) -> dict[str, Any] | None:
    try:
        raw_text = normalize_space(row.text)
        cells = [
            normalize_space(cell.text)
            for cell in row.find_elements(By.CSS_SELECTOR, "td, th, [role='cell'], [role='gridcell']")
            if normalize_space(cell.text)
        ]
    except (StaleElementReferenceException, WebDriverException):
        return None

    if not raw_text:
        return None
    lowered = raw_text.lower()
    if any(skip in lowered for skip in ["statement", "document center", "available balance", "current balance"]):
        return None
    transaction_date = parse_transaction_date(raw_text) or parse_named_transaction_date(raw_text)
    if not transaction_date or not parse_money_values(raw_text):
        return None

    amount, debit_credit = infer_transaction_amount(cells, raw_text)
    description = infer_transaction_description(cells, raw_text)
    status = normalize_transaction_status(raw_text)
    return {
        "transaction_date": transaction_date,
        "posted_date": transaction_date,
        "description": description,
        "amount": amount,
        "debit_credit": debit_credit,
        "status": status,
        "source": "citizens_visible_activity",
        "captured_at": utc_now(),
        "raw_text": raw_text[:1000],
    }


def scrape_visible_transactions(driver) -> list[dict[str, Any]]:
    transactions: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in candidate_transaction_rows(driver):
        transaction = parse_transaction_row(row)
        if not transaction:
            continue
        key = normalize_space(transaction.get("raw_text") or "")
        if key in seen:
            continue
        seen.add(key)
        transactions.append(transaction)
    if not transactions:
        transactions = scrape_transactions_from_visible_text(driver)
    return transactions


def scrape_transactions_from_visible_text(driver) -> list[dict[str, Any]]:
    try:
        page_text = driver.find_element(By.TAG_NAME, "body").text
    except WebDriverException:
        return []

    lines = [normalize_space(line) for line in page_text.splitlines()]
    lines = [line for line in lines if line]
    transactions: list[dict[str, Any]] = []
    current_date: str | None = None
    index = 0
    while index < len(lines):
        line = lines[index]
        named_date = parse_named_transaction_date(line)
        if named_date:
            current_date = named_date
            index += 1
            continue

        if current_date:
            amount_values = parse_money_values(line)
            previous_line_is_money = index >= 1 and bool(parse_money_values(lines[index - 1]))
            if amount_values and ("$" in line or line.startswith("-")) and not previous_line_is_money:
                description = lines[index - 2] if index >= 2 else ""
                status_text = lines[index - 1] if index >= 1 else ""
                if description and not parse_named_transaction_date(description):
                    amount = amount_values[0]
                    debit_credit = "credit" if amount > 0 else "debit"
                    raw_text = normalize_space(" ".join([current_date, description, status_text, line]))
                    transactions.append(
                        {
                            "transaction_date": current_date,
                            "posted_date": current_date,
                            "description": description[:300],
                            "amount": amount,
                            "debit_credit": debit_credit,
                            "status": normalize_transaction_status(status_text),
                            "source": "citizens_visible_activity_text",
                            "captured_at": utc_now(),
                            "raw_text": raw_text[:1000],
                        }
                    )
            index += 1
            continue
        index += 1
    return transactions


def click_read_only_navigation(driver, labels: list[str]) -> bool:
    if click_by_text(driver, labels):
        return True
    for label in labels:
        xpath = (
            f"//a[contains(translate(normalize-space(.), "
            f"'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), '{label.lower()}')]"
            f"|//button[contains(translate(normalize-space(.), "
            f"'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), '{label.lower()}')]"
        )
        try:
            elements = driver.find_elements(By.XPATH, xpath)
        except WebDriverException:
            continue
        for element in elements:
            try:
                if element.is_displayed() and element.is_enabled():
                    if click_element(driver, element):
                        return True
            except WebDriverException:
                continue
    return False


def click_in_checking_section(driver, labels: list[str], excluded_labels: list[str] | None = None) -> bool:
    script = """
        const labels = arguments[0].map((label) => label.replace(/\\s+/g, ' ').trim().toLowerCase());
        const excluded = arguments[1].map((label) => label.replace(/\\s+/g, ' ').trim().toLowerCase());
        const actions = Array.from(document.querySelectorAll('a, button, [role="button"], [tabindex], label'));
        function visible(node) {
            const style = window.getComputedStyle(node);
            const rect = node.getBoundingClientRect();
            return style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0;
        }
        function textOf(node) {
            return [
                node.innerText,
                node.textContent,
                node.getAttribute('aria-label'),
                node.getAttribute('title')
            ].filter(Boolean).join(' ').replace(/\\s+/g, ' ').trim().toLowerCase();
        }
        for (const action of actions) {
            if (!visible(action)) continue;
            const text = textOf(action);
            if (!labels.some((label) => text.includes(label))) continue;
            if (excluded.some((label) => text.includes(label))) continue;
            let current = action;
            for (let depth = 0; current && depth < 6; depth += 1, current = current.parentElement) {
                const containerText = textOf(current);
                if (containerText.includes('checking') && !containerText.includes('savings')) return action;
            }
        }
        return null;
    """
    try:
        element = driver.execute_script(script, labels, excluded_labels or [])
    except WebDriverException:
        element = None
    return bool(element and click_element(driver, element))


def click_checking_document_center_link(driver) -> bool:
    return click_in_checking_section(
        driver,
        ["view statements in document center", "statements in document center"],
        ["savings"],
    ) or click_by_text(driver, ["View Statements in Document Center"], exact=True)


def page_looks_like_document_center(driver) -> bool:
    try:
        current_url = driver.current_url.lower()
        page_text = driver.find_element(By.TAG_NAME, "body").text.lower()
    except WebDriverException:
        return False
    return (
        "document-center" in current_url
        or "account-pdfs" in current_url
        or ("document center" in page_text and ("statement" in page_text or "documents" in page_text))
    )


def open_document_center_menu(driver) -> bool:
    menu_labels = [
        "Documents",
        "Document Center",
        "Statements",
        "Accounts",
        "More",
        "Menu",
    ]
    for label in menu_labels:
        if click_by_text(driver, [label], exact=True) or click_by_text(driver, [label], exact=False):
            if page_looks_like_document_center(driver):
                return True
            if click_read_only_navigation(driver, ["document center", "statements", "view statements"]):
                return True
    return False


def navigate_to_document_center(driver) -> bool:
    if page_looks_like_document_center(driver):
        return True
    if "home/accounts" not in driver.current_url.lower():
        safe_get(driver, config.CITIZENS_HOME_URL)

    if click_checking_document_center_link(driver):
        switch_to_citizens_tab(driver, ["document-center", "account-pdfs", "documents", "statements"])
        if page_looks_like_document_center(driver):
            return True

    direct_urls = [
        config.CITIZENS_DOCUMENT_CENTER_URL,
        "https://www.citizensbankonline.com/olb-root/home/documents",
        "https://www.citizensbankonline.com/olb-root/home/statements",
        "https://www.citizensbank.com/account-pdfs",
    ]
    for url in direct_urls:
        try:
            safe_get(driver, url)
            if page_looks_like_document_center(driver):
                return True
        except WebDriverException:
            continue

    safe_get(driver, config.CITIZENS_HOME_URL)
    clicked = (
        click_by_text(driver, ["View Statements in Document Center"], exact=True)
        or click_read_only_navigation(
            driver,
            [
                "view statements in document center",
                "document center",
                "documents",
                "statements",
                "view statements",
            ],
        )
        or open_document_center_menu(driver)
    )
    if not clicked:
        return False
    switch_to_citizens_tab(driver, ["document-center", "account-pdfs", "documents", "statements"])
    return page_looks_like_document_center(driver)


def select_preferred_option(select: Select, preferred_labels: list[str], contains: str | None = None) -> bool:
    normalized_options = [(option, option.text.strip().lower()) for option in select.options]
    for label in preferred_labels:
        target = label.lower()
        for option, text in normalized_options:
            if text == target:
                select.select_by_visible_text(option.text)
                return True
    for label in preferred_labels:
        target = label.lower()
        for option, text in normalized_options:
            if target in text:
                select.select_by_visible_text(option.text)
                return True
    if contains:
        target = contains.lower()
        for option, text in normalized_options:
            if target in text:
                select.select_by_visible_text(option.text)
                return True
    return False


def selected_option_text(select_element: WebElement) -> str:
    try:
        return Select(select_element).first_selected_option.text.strip()
    except WebDriverException:
        return ""


def select_preferred_option_by_js(
    driver,
    select_element: WebElement,
    preferred_labels: list[str],
    contains: str | None = None,
) -> bool:
    script = """
        const select = arguments[0];
        const preferred = arguments[1].map((label) => label.trim().toLowerCase());
        const contains = arguments[2] ? arguments[2].trim().toLowerCase() : null;
        const options = Array.from(select.options);
        function normalized(option) {
            return (option.textContent || option.innerText || '').replace(/\\s+/g, ' ').trim().toLowerCase();
        }
        function choose(match) {
            const option = options.find(match);
            if (!option) return false;
            select.value = option.value;
            option.selected = true;
            select.dispatchEvent(new Event('input', {bubbles: true}));
            select.dispatchEvent(new Event('change', {bubbles: true}));
            return true;
        }
        for (const label of preferred) {
            if (choose((option) => normalized(option) === label)) return true;
        }
        for (const label of preferred) {
            if (choose((option) => normalized(option).includes(label))) return true;
        }
        if (contains) {
            if (choose((option) => normalized(option).includes(contains))) return true;
        }
        return false;
    """
    try:
        return bool(driver.execute_script(script, select_element, preferred_labels, contains))
    except WebDriverException:
        return False


def select_preferred_option_reliably(
    driver,
    select_element: WebElement,
    preferred_labels: list[str],
    contains: str | None = None,
    expected_contains: str | None = None,
) -> bool:
    try:
        if not select_preferred_option(Select(select_element), preferred_labels, contains=contains):
            select_preferred_option_by_js(driver, select_element, preferred_labels, contains=contains)
    except WebDriverException:
        select_preferred_option_by_js(driver, select_element, preferred_labels, contains=contains)

    expected = (expected_contains or contains or (preferred_labels[0] if preferred_labels else "")).lower()
    if not expected:
        return True
    try:
        WebDriverWait(driver, 5).until(
            lambda active_driver: expected in selected_option_text(select_element).lower()
            or any(
                expected in selected_option_text(current_select).lower()
                for current_select in active_driver.find_elements(By.CSS_SELECTOR, "select")
            )
        )
        return True
    except TimeoutException:
        return False


def format_citizens_date(iso_date: str) -> str:
    parsed = datetime.strptime(iso_date, "%Y-%m-%d")
    return f"{parsed.month:02d}/{parsed.day:02d}/{parsed.year}"


def today_citizens_date() -> str:
    now = datetime.now()
    return f"{now.month:02d}/{now.day:02d}/{now.year}"


def visible_date_inputs(driver) -> list[WebElement]:
    script = """
        const inputs = Array.from(document.querySelectorAll('input'))
            .filter((input) => {
                const style = window.getComputedStyle(input);
                const rect = input.getBoundingClientRect();
                const type = (input.type || '').toLowerCase();
                if (['radio', 'checkbox', 'hidden', 'button', 'submit'].includes(type)) return false;
                if (style.visibility === 'hidden' || style.display === 'none') return false;
                if (rect.width <= 0 || rect.height <= 0) return false;
                const text = [
                    input.type,
                    input.name,
                    input.id,
                    input.getAttribute('aria-label'),
                    input.placeholder
                ].filter(Boolean).join(' ').toLowerCase();
                return type === 'date' || /date|from|to|start|end|mm\\/dd\\/yyyy/.test(text);
            });
        function score(input, needles) {
            const text = [
                input.name,
                input.id,
                input.getAttribute('aria-label'),
                input.placeholder
            ].filter(Boolean).join(' ').toLowerCase();
            return needles.some((needle) => text.includes(needle)) ? 0 : 1;
        }
        inputs.sort((left, right) => {
            const leftStart = score(left, ['from', 'start']);
            const rightStart = score(right, ['from', 'start']);
            if (leftStart !== rightStart) return leftStart - rightStart;
            const leftEnd = score(left, ['to', 'end']);
            const rightEnd = score(right, ['to', 'end']);
            if (leftEnd !== rightEnd) return rightEnd - leftEnd;
            return 0;
        });
        return inputs;
    """
    try:
        return list(driver.execute_script(script))
    except WebDriverException:
        return []


def set_date_input(driver, element: WebElement, text_value: str, iso_value: str) -> bool:
    try:
        element.click()
        element.send_keys(Keys.CONTROL, "a")
        element.send_keys(Keys.BACKSPACE)
        element.send_keys(iso_value if (element.get_attribute("type") or "").lower() == "date" else text_value)
        element.send_keys(Keys.TAB)
        driver.execute_script(
            """
            arguments[0].dispatchEvent(new Event('input', {bubbles: true}));
            arguments[0].dispatchEvent(new Event('change', {bubbles: true}));
            """,
            element,
        )
        return True
    except WebDriverException:
        return False


def set_visible_date_inputs(driver, start_date: str, end_date: str) -> bool:
    try:
        WebDriverWait(driver, 10).until(lambda active_driver: len(visible_date_inputs(active_driver)) >= 2)
        inputs = visible_date_inputs(driver)
        if len(inputs) < 2:
            return False
        start_text = format_citizens_date(start_date)
        end_iso = datetime.strptime(end_date, "%m/%d/%Y").strftime("%Y-%m-%d")
        script = """
            const startInput = arguments[0];
            const endInput = arguments[1];
            const startText = arguments[2];
            const endText = arguments[3];
            const startIso = arguments[4];
            const endIso = arguments[5];
            const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
            function setValue(input, textValue, isoValue) {
                input.focus();
                setter.call(input, (input.type || '').toLowerCase() === 'date' ? isoValue : textValue);
                input.dispatchEvent(new Event('input', {bubbles: true}));
                input.dispatchEvent(new Event('change', {bubbles: true}));
                input.blur();
            }
            setValue(startInput, startText, startIso);
            setValue(endInput, endText, endIso);
            return startInput.value && endInput.value;
        """
        return bool(driver.execute_script(script, inputs[0], inputs[1], start_text, end_date, start_date, end_iso))
    except (ValueError, WebDriverException):
        return False


def apply_document_center_filters(driver, statement_range: str) -> bool:
    """Apply read-only Document Center filters for checking statements."""
    try:
        click_by_text(driver, ["Banking documents"], exact=True)
        WebDriverWait(driver, 10).until(EC.presence_of_all_elements_located((By.CSS_SELECTOR, "select")))
        selects = driver.find_elements(By.CSS_SELECTOR, "select")
        if len(selects) < 3:
            return False

        account = Select(selects[1])
        select_preferred_option(account, [], contains="checking")

        selects = driver.find_elements(By.CSS_SELECTOR, "select")
        if len(selects) < 3:
            return False

        document_type = Select(selects[2])
        select_preferred_option(document_type, ["Statements"], contains="statement")

        selects = driver.find_elements(By.CSS_SELECTOR, "select")
        if len(selects) < 3:
            return False

        if statement_range == "all":
            if not select_preferred_option_reliably(
                driver,
                selects[0],
                ["Date range", "Custom range", "Custom date range"],
                contains="date",
                expected_contains="date range",
            ):
                return False
            WebDriverWait(driver, 10).until(lambda active_driver: len(visible_date_inputs(active_driver)) >= 2)
        else:
            if not select_preferred_option_reliably(
                driver,
                selects[0],
                ["Last 60 days", "60 days"],
                contains="60",
                expected_contains="60",
            ):
                return False

        if statement_range == "all":
            if not set_visible_date_inputs(
                driver,
                config.CITIZENS_ALL_STATEMENTS_START_DATE,
                today_citizens_date(),
            ):
                return False

        for element in driver.find_elements(By.XPATH, "//*[self::button or self::a][contains(normalize-space(.), 'Apply')]"):
            if element.is_displayed() and element.is_enabled():
                element.click()
                wait_for_page_ready(driver)
                WebDriverWait(driver, 20).until(
                    lambda active_driver: len(
                        active_driver.find_elements(By.CSS_SELECTOR, "table tbody tr, [role='row']")
                    )
                    > 0
                    or "no documents" in active_driver.find_element(By.TAG_NAME, "body").text.lower()
                )
                return True
    except (TimeoutException, WebDriverException):
        return False
    return False


def set_download_directory(driver, download_dir: Path | None = None) -> None:
    target_dir = download_dir or config.CITIZENS_DOWNLOAD_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    for command in ["Browser.setDownloadBehavior", "Page.setDownloadBehavior"]:
        try:
            driver.execute_cdp_cmd(
                command,
                {
                    "behavior": "allow",
                    "downloadPath": str(target_dir.resolve()),
                },
            )
            return
        except WebDriverException:
            continue


def default_downloads_dir() -> Path:
    return Path.home() / "Downloads"


def file_snapshot(directory: Path) -> dict[Path, tuple[float, int]]:
    if not directory.exists():
        return {}
    return {
        path: (path.stat().st_mtime, path.stat().st_size)
        for path in directory.iterdir()
        if path.is_file() and not path.name.endswith(".crdownload")
    }


def unique_target_path(directory: Path, filename: str) -> Path:
    target = directory / filename
    if not target.exists():
        return target
    stem = target.stem
    suffix = target.suffix
    index = 1
    while True:
        candidate = directory / f"{stem}_{index}{suffix}"
        if not candidate.exists():
            return candidate
        index += 1


def collect_downloaded_file(
    target_dir: Path,
    known_target: dict[Path, tuple[float, int]] | set[Path],
    known_downloads: dict[Path, tuple[float, int]] | None = None,
    started_at: float | None = None,
    timeout: int | None = None,
) -> Path | None:
    target_dir.mkdir(parents=True, exist_ok=True)
    downloads_dir = default_downloads_dir()
    known_downloads = known_downloads if known_downloads is not None else file_snapshot(downloads_dir)
    started_at = started_at or time.time()
    deadline = time.time() + (timeout or config.DOWNLOAD_TIMEOUT_SECONDS)

    def changed_candidates(directory: Path, known: dict[Path, tuple[float, int]] | set[Path]) -> list[Path]:
        if not directory.exists():
            return []
        candidates: list[Path] = []
        for path in directory.iterdir():
            if not path.is_file() or path.name.endswith(".crdownload"):
                continue
            stat = path.stat()
            if isinstance(known, set):
                changed = path not in known
            else:
                previous = known.get(path)
                changed = previous is None or previous != (stat.st_mtime, stat.st_size)
            if changed or stat.st_mtime >= started_at - 1:
                candidates.append(path)
        return candidates

    while time.time() < deadline:
        wait_for_downloads(target_dir, timeout=1)
        if downloads_dir.exists():
            wait_for_downloads(downloads_dir, timeout=1)

        target_candidates = changed_candidates(target_dir, known_target)
        if target_candidates:
            return max(target_candidates, key=lambda path: path.stat().st_mtime)

        download_candidates = changed_candidates(downloads_dir, known_downloads)
        if download_candidates:
            source = max(download_candidates, key=lambda path: path.stat().st_mtime)
            destination = unique_target_path(target_dir, source.name)
            try:
                shutil.move(str(source), str(destination))
                log_event("download moved", source=str(source), path=str(destination))
                return destination
            except OSError:
                try:
                    shutil.copy2(str(source), str(destination))
                    log_event("download copied", source=str(source), path=str(destination))
                    return destination
                except OSError:
                    return source
        time.sleep(1)
    return None


def find_statement_rows(driver) -> list[dict[str, Any]]:
    # TODO: Inspect Citizens Document Center markup and replace this generic table scan
    # with stable selectors for received date, account, type, name, and download link.
    rows: list[dict[str, Any]] = []
    try:
        table_rows = driver.find_elements(By.CSS_SELECTOR, "table tbody tr, [role='row']")
    except WebDriverException:
        return rows

    for row in table_rows:
        try:
            text = row.text.strip()
            if not text or "statement" not in text.lower():
                continue
            cells = [cell.text.strip() for cell in row.find_elements(By.CSS_SELECTOR, "td, [role='cell']")]
            links = row.find_elements(By.CSS_SELECTOR, "a, button")
            download_link = choose_download_link(links)
            rows.append(
                {
                    "received_date": cells[0] if len(cells) > 0 else None,
                    "account_label": cells[1] if len(cells) > 1 else None,
                    "document_type": cells[2] if len(cells) > 2 else "Statement",
                    "statement_name": cells[3] if len(cells) > 3 else text[:120],
                    "row_text": text,
                    "download_link": download_link,
                    "source_url_optional": download_link.get_attribute("href") if download_link else None,
                }
            )
        except (StaleElementReferenceException, WebDriverException):
            continue
    return rows


def choose_download_link(links: list[WebElement]) -> WebElement | None:
    visible_links: list[WebElement] = []
    for link in links:
        try:
            if link.is_displayed() and link.is_enabled():
                visible_links.append(link)
        except WebDriverException:
            continue
    for link in visible_links:
        try:
            label = (link.text or link.get_attribute("aria-label") or "").lower()
            if "download" in label:
                return link
        except WebDriverException:
            continue
    return None


def download_statement(row: dict[str, Any], known_files: set[Path]) -> dict[str, Any] | None:
    link = row.get("download_link")
    if not link:
        return None
    known_downloads = file_snapshot(default_downloads_dir())
    started_at = time.time()
    try:
        try:
            link.location_once_scrolled_into_view
            link.click()
        except WebDriverException:
            link.parent.execute_script("arguments[0].click();", link)
    except WebDriverException:
        return None

    newest = collect_downloaded_file(
        config.CITIZENS_DOWNLOAD_DIR,
        known_files,
        known_downloads=known_downloads,
        started_at=started_at,
    )
    if not newest:
        return None
    return {
        "document_type": row.get("document_type"),
        "statement_name": row.get("statement_name"),
        "received_date": row.get("received_date"),
        "account_label": row.get("account_label"),
        "local_path": str(newest),
        "file_hash": compute_file_hash(newest),
        "downloaded_at": utc_now(),
        "source_url_optional": row.get("source_url_optional"),
    }


def page_looks_like_account_overview(driver) -> bool:
    try:
        page_text = driver.find_element(By.TAG_NAME, "body").text.lower()
    except WebDriverException:
        return False
    return "account overview" in page_text and "export" in page_text


def wait_for_account_summary(driver, timeout: int = 30) -> bool:
    try:
        WebDriverWait(driver, timeout).until(
            lambda active_driver: bool(
                active_driver.execute_script(
                    """
                    const bodyText = (document.body && document.body.innerText || '').toLowerCase();
                    const buttons = Array.from(document.querySelectorAll('olb-mfe-accounts-account-item button, button'));
                    const hasCheckingButton = buttons.some((node) => {
                        const style = window.getComputedStyle(node);
                        const rect = node.getBoundingClientRect();
                        const text = (node.innerText || node.textContent || '').toLowerCase();
                        return style.visibility !== 'hidden'
                            && style.display !== 'none'
                            && rect.width > 0
                            && rect.height > 0
                            && text.includes('checking')
                            && !text.includes('savings')
                            && !text.includes('statement')
                            && !text.includes('document center');
                    });
                    return bodyText.includes('checking accounts') && hasCheckingButton;
                    """
                )
            )
        )
        return True
    except (TimeoutException, WebDriverException):
        return False


def navigate_to_checking_overview(driver) -> bool:
    safe_get(driver, config.CITIZENS_HOME_URL)
    wait_for_account_summary(driver)
    try:
        checking_button = driver.execute_script(
            """
            const nodes = Array.from(document.querySelectorAll('olb-mfe-accounts-account-item button, button'));
            function visible(node) {
                const style = window.getComputedStyle(node);
                const rect = node.getBoundingClientRect();
                return style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0;
            }
            return nodes.find((node) => {
                if (!visible(node)) return false;
                const text = (node.innerText || node.textContent || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                return text.includes('checking')
                    && !text.includes('savings')
                    && !text.includes('statement')
                    && !text.includes('document center')
                    && !text.includes('manage card');
            }) || null;
            """
        )
    except WebDriverException:
        checking_button = None
    if checking_button and click_element(driver, checking_button):
        try:
            WebDriverWait(driver, 15).until(lambda active_driver: page_looks_like_account_overview(active_driver))
            return True
        except TimeoutException:
            pass
    if click_in_checking_section(driver, ["checking"], ["view statements", "document center", "manage card", "savings"]):
        try:
            WebDriverWait(driver, 15).until(lambda active_driver: page_looks_like_account_overview(active_driver))
            return True
        except TimeoutException:
            pass
    if click_checking_account(driver):
        try:
            WebDriverWait(driver, 15).until(lambda active_driver: page_looks_like_account_overview(active_driver))
            return True
        except TimeoutException:
            pass
    return page_looks_like_account_overview(driver)


def export_checking_transactions(driver) -> dict[str, Any] | None:
    config.RAW_CSV_DIR.mkdir(parents=True, exist_ok=True)
    set_download_directory(driver, config.RAW_CSV_DIR)
    known_files = file_snapshot(config.RAW_CSV_DIR)
    known_downloads = file_snapshot(default_downloads_dir())
    started_at = time.time()
    try:
        clicked_export = bool(
            driver.execute_script(
                """
                const nodes = Array.from(document.querySelectorAll('button, [role="button"]'));
                function visible(node) {
                    const style = window.getComputedStyle(node);
                    const rect = node.getBoundingClientRect();
                    return style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0;
                }
                const button = nodes.find((node) => {
                    if (!visible(node)) return false;
                    const text = (node.innerText || node.textContent || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                    return text === 'export';
                });
                if (!button) return false;
                button.scrollIntoView({block: 'center'});
                button.click();
                return true;
                """
            )
        )
    except WebDriverException:
        clicked_export = False
    if not clicked_export:
        return None
    time.sleep(0.5)
    try:
        selected_format = bool(
            driver.execute_script(
                """
                const nodes = Array.from(document.querySelectorAll(
                    '.olb-c-accountTransactions__exportDropdownItem, [class*="exportDropdownItem"], div, button, a'
                ));
                function visible(node) {
                    const style = window.getComputedStyle(node);
                    const rect = node.getBoundingClientRect();
                    return style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0;
                }
                const item = nodes.find((node) => {
                    if (!visible(node)) return false;
                    const text = (node.innerText || node.textContent || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                    return text === 'comma delimited';
                });
                if (!item) return false;
                item.scrollIntoView({block: 'center'});
                item.click();
                return true;
                """
            )
        )
    except WebDriverException:
        selected_format = False
    if not selected_format:
        return None
    newest = collect_downloaded_file(
        config.RAW_CSV_DIR,
        known_files,
        known_downloads=known_downloads,
        started_at=started_at,
    )
    if not newest:
        return None
    return {
        "format": "comma_delimited",
        "local_path": str(newest),
        "file_hash": compute_file_hash(newest),
        "downloaded_at": utc_now(),
    }


def normalized_csv_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def csv_field(row: dict[str, str], names: list[str]) -> str:
    normalized = {normalized_csv_key(key): value for key, value in row.items()}
    for name in names:
        value = normalized.get(normalized_csv_key(name))
        if value:
            return normalize_space(value)
    return ""


def parse_exported_transactions(path: str | Path) -> list[dict[str, Any]]:
    file_path = Path(path)
    text = ""
    for encoding in ["utf-8-sig", "utf-8", "cp1252"]:
        try:
            text = file_path.read_text(encoding=encoding)
            break
        except UnicodeDecodeError:
            continue
    if not text:
        return []
    sample = text[:2048]
    try:
        dialect = csv.Sniffer().sniff(sample)
    except csv.Error:
        dialect = csv.excel
    rows = csv.DictReader(text.splitlines(), dialect=dialect)
    transactions: list[dict[str, Any]] = []
    for row in rows:
        if not row:
            continue
        transaction_date_text = csv_field(row, ["Date", "Transaction Date", "Posted Date", "Posting Date"])
        description = csv_field(row, ["Description", "Payee", "Memo", "Transaction"])
        amount_text = csv_field(row, ["Amount", "Debit", "Credit"])
        debit_text = csv_field(row, ["Debit", "Withdrawal"])
        credit_text = csv_field(row, ["Credit", "Deposit"])
        amount = parse_money(amount_text) if amount_text else None
        debit_credit = None
        if debit_text and parse_money(debit_text) is not None:
            amount = -abs(parse_money(debit_text) or 0)
            debit_credit = "debit"
        elif credit_text and parse_money(credit_text) is not None:
            amount = abs(parse_money(credit_text) or 0)
            debit_credit = "credit"
        elif amount is not None:
            debit_credit = "credit" if amount > 0 else "debit"
        transaction_date = parse_transaction_date(transaction_date_text)
        if not transaction_date or not description:
            raw_text = normalize_space(" ".join(str(value) for value in row.values() if value))
            transaction_date = transaction_date or parse_transaction_date(raw_text)
            description = description or infer_transaction_description([], raw_text)
        if not transaction_date or not description:
            continue
        raw_text = normalize_space(" ".join(str(value) for value in row.values() if value))
        transactions.append(
            {
                "transaction_date": transaction_date,
                "posted_date": transaction_date,
                "description": description[:300],
                "amount": amount,
                "debit_credit": debit_credit,
                "status": "posted",
                "source": "citizens_export_comma_delimited",
                "captured_at": utc_now(),
                "raw_text": raw_text[:1000],
            }
        )
    return transactions


def capture_checking_transactions(driver, result: dict[str, Any], account_id: int) -> tuple[int, int]:
    transactions: list[dict[str, Any]] = []
    export_metadata: dict[str, Any] | None = None
    if navigate_to_checking_overview(driver):
        export_metadata = export_checking_transactions(driver)
        if export_metadata:
            result["data"]["transaction_exports"].append(export_metadata)
            log_event("files downloaded", path=export_metadata["local_path"])
            transactions = parse_exported_transactions(export_metadata["local_path"])

    if not transactions:
        if not page_looks_like_account_overview(driver):
            navigate_to_recent_activity(driver)
        try:
            WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.CSS_SELECTOR, "body")))
        except TimeoutException:
            pass
        transactions = scrape_visible_transactions(driver)

    inserted_transactions = 0
    for transaction in transactions:
        if record_transaction(account_id, transaction):
            inserted_transactions += 1
        result["data"]["transactions"].append(transaction)
    return len(transactions), inserted_transactions


def write_waiting_result(result: dict[str, Any], driver, detection: dict[str, Any]) -> None:
    if config.CITIZENS_CAPTURE_SCREENSHOTS:
        screenshot = save_screenshot(driver, "citizens_user_action_required")
        if screenshot:
            result["screenshots"].append(screenshot)
            log_event("screenshots captured", path=screenshot)
    result["data"] = detection.get("data", {})
    result["errors"] = []
    result["status"] = detection.get("status", config.STATUS_WAITING_FOR_USER_ACTION)
    result["success"] = False
    result["message"] = detection.get(
        "message",
        "Please complete login or verification in the opened browser, then click Sync Citizens again.",
    )
    result["finished_at"] = utc_now()
    write_result(result)
    log_event("user action required", message=result["message"], reasons=", ".join(detection.get("reasons", [])))


def write_auth_waiting_result(
    result: dict[str, Any],
    driver,
    detection: dict[str, Any],
    transactions_only: bool,
    statement_range: str,
) -> None:
    detection = {
        **detection,
        "data": resume_payload(transactions_only, statement_range),
    }
    write_waiting_result(result, driver, detection)


def detect_access_denied(driver) -> bool:
    try:
        page_text = driver.find_element(By.TAG_NAME, "body").text.lower()
        current_url = driver.current_url.lower()
    except WebDriverException:
        return False
    return "access denied" in page_text or "errors.edgesuite.net" in page_text or "edgesuite.net" in current_url


def switch_to_citizens_tab(driver, preferred_fragments: list[str] | None = None) -> bool:
    preferred_fragments = preferred_fragments or []
    candidates: list[tuple[int, str]] = []
    for handle in driver.window_handles:
        try:
            driver.switch_to.window(handle)
            current_url = driver.current_url.lower()
            if "citizensbankonline.com" not in current_url and "citizensbank.com" not in current_url:
                continue
            score = 50
            if "citizensbankonline.com/olb-root/home" in current_url:
                score = 10
            if "document-center" in current_url:
                score = 20
            if "login" in current_url:
                score = 90
            if "citizensbank.com/account-pdfs" in current_url:
                score = 80
            for index, fragment in enumerate(preferred_fragments):
                if fragment.lower() in current_url:
                    score = index
                    break
            candidates.append((score, handle))
        except WebDriverException:
            continue

    if candidates:
        _, handle = sorted(candidates, key=lambda item: item[0])[0]
        driver.switch_to.window(handle)
        wait_for_page_ready(driver)
        return True
    return False


def write_access_denied_result(result: dict[str, Any], driver) -> None:
    if config.CITIZENS_CAPTURE_SCREENSHOTS:
        screenshot = save_screenshot(driver, "citizens_access_denied")
        if screenshot:
            result["screenshots"].append(screenshot)
            log_event("screenshots captured", path=screenshot)
    finish_result(
        result,
        False,
        config.STATUS_FAILED,
        "Citizens denied the automated browser. Use Citizens Setup for manual profile login; v1 sync cannot bypass site access controls.",
    )
    log_event("worker failed", message="Citizens denied the automated browser.", worker=WORKER_NAME)


def write_manual_chrome_required_result(result: dict[str, Any], transactions_only: bool, statement_range: str) -> None:
    open_manual_chrome_profile(
        config.CITIZENS_PROFILE_NAME,
        config.CITIZENS_LOGIN_URL,
        debugging_port=config.CITIZENS_DEBUGGING_PORT,
    )
    action = "latest transactions sync" if transactions_only else "Citizens sync"
    result["data"] = resume_payload(transactions_only, statement_range)
    finish_result(
        result,
        False,
        config.STATUS_WAITING_FOR_USER_ACTION,
        (
            f"Opened Citizens in normal Chrome for {action}. Enter your username/password there, "
            "leave the window open, then click Sync Citizens again. The app will submit login "
            "and request text-message verification when Citizens exposes those controls."
        ),
    )
    log_event("user action required", message=f"Manual Citizens Chrome required for {action}.")


def attach_or_open_citizens_driver(result: dict[str, Any], transactions_only: bool, statement_range: str):
    if not chrome_debugger_available(config.CITIZENS_DEBUGGER_ADDRESS):
        open_manual_chrome_profile(
            config.CITIZENS_PROFILE_NAME,
            config.CITIZENS_LOGIN_URL,
            debugging_port=config.CITIZENS_DEBUGGING_PORT,
        )
        deadline = time.time() + 10
        while time.time() < deadline and not chrome_debugger_available(config.CITIZENS_DEBUGGER_ADDRESS):
            time.sleep(0.5)

    if not chrome_debugger_available(config.CITIZENS_DEBUGGER_ADDRESS):
        write_manual_chrome_required_result(result, transactions_only, statement_range)
        return None

    driver = create_driver(
        config.CITIZENS_PROFILE_NAME,
        config.CITIZENS_DOWNLOAD_DIR,
        detach=True,
        debugger_address=config.CITIZENS_DEBUGGER_ADDRESS,
    )
    switch_to_citizens_tab(driver, ["home/accounts", "login"])
    wait_for_page_ready(driver)
    return driver


def run_setup(started_at: str) -> int:
    result = base_result(started_at)
    open_manual_chrome_profile(
        config.CITIZENS_PROFILE_NAME,
        config.CITIZENS_LOGIN_URL,
        debugging_port=config.CITIZENS_DEBUGGING_PORT,
    )
    finish_result(
        result,
        False,
        config.STATUS_WAITING_FOR_USER_ACTION,
        (
            "Citizens setup browser opened in normal Chrome. Enter your username/password there, "
            "leave that Chrome window open, then click Sync Citizens again. The app will submit login "
            "and request text-message verification when Citizens exposes those controls."
        ),
    )
    log_event("user action required", message="Citizens setup browser opened in normal Chrome.")
    return 0


def run_sync(started_at: str, transactions_only: bool = False, statement_range: str = "last60") -> int:
    result = base_result(started_at)
    account_id = get_or_create_citizens_account()
    ensure_transactions_table()
    driver = None
    try:
        log_event("worker started", worker=WORKER_NAME, site=config.CITIZENS_SITE_KEY)
        driver = attach_or_open_citizens_driver(result, transactions_only, statement_range)
        if not driver:
            return 0

        if detect_access_denied(driver):
            write_access_denied_result(result, driver)
            return 0

        submitted_mfa_code = submit_pending_mfa_code(driver)
        if submitted_mfa_code:
            wait_for_page_ready(driver)
        elif pending_mfa_code() and code_entry_page_still_visible(driver):
            write_auth_waiting_result(
                result,
                driver,
                {
                    "detected": True,
                    "status": config.STATUS_WAITING_FOR_USER_ACTION,
                    "message": (
                        "Automation entered the Citizens code, but could not confirm it. "
                        "Press Confirm Code in the opened browser, then click Sync Citizens again."
                    ),
                    "reasons": ["confirm code did not complete"],
                },
                transactions_only,
                statement_range,
            )
            return 0

        auth_action = advance_citizens_authentication(driver)
        if auth_action == "login_submitted":
            auth_action = continue_after_login_submit(driver)
        if auth_action == "text_mfa_requested" and pending_mfa_code():
            if submit_pending_mfa_code(driver):
                auth_action = None
        if auth_action:
            write_auth_waiting_result(
                result,
                driver,
                authentication_wait_detection(auth_action, transactions_only, statement_range),
                transactions_only,
                statement_range,
            )
            return 0

        detection = detect_possible_login_or_mfa(driver)
        if detection["detected"]:
            detection = clarify_login_detection(driver, detection)
            detection = resolve_mfa_method_detection(driver, detection)
            write_auth_waiting_result(result, driver, detection, transactions_only, statement_range)
            return 0

        if "citizensbankonline.com/olb-root/home/accounts" not in driver.current_url.lower():
            safe_get(driver, config.CITIZENS_HOME_URL)
        try:
            WebDriverWait(driver, 20).until(
                EC.presence_of_element_located(
                    (By.CSS_SELECTOR, "#product-group-checking-accounts, #account-list-checking-accounts")
                )
            )
        except TimeoutException:
            pass
        raw_balance = scrape_current_checking_balance(driver)
        if raw_balance:
            balance = record_balance(account_id, raw_balance)
            if balance:
                result["data"]["balances"].append(balance)
        else:
            result["errors"].append(
                "Could not identify a checking balance with v1 placeholder selectors. Inspect Citizens selectors manually."
            )

        if transactions_only:
            transaction_count, inserted_transactions = capture_checking_transactions(driver, result, account_id)
            if not transaction_count:
                result["errors"].append(
                    "Could not export or identify transaction rows after opening the Checking account page. "
                    + page_diagnostic(driver)
                )
                message = "Citizens latest transactions sync completed, but no visible transaction rows were found."
            else:
                message = (
                    f"Citizens latest transactions sync completed. "
                    f"Captured {transaction_count} rows, {inserted_transactions} new."
                )
            finish_result(result, True, config.STATUS_SUCCESS, message)
            log_event("worker completed", message=message, worker=WORKER_NAME)
            return 0

        safe_get(driver, config.CITIZENS_HOME_URL)
        if navigate_to_document_center(driver):
            set_download_directory(driver)
            detection = detect_possible_login_or_mfa(driver)
            if detection["detected"]:
                detection = clarify_login_detection(driver, detection)
                detection = resolve_mfa_method_detection(driver, detection)
                write_auth_waiting_result(result, driver, detection, transactions_only, statement_range)
                return 0
            apply_document_center_filters(driver, statement_range)
            try:
                WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.CSS_SELECTOR, "body")))
            except TimeoutException:
                pass
            statement_rows = find_statement_rows(driver)
        else:
            statement_rows = []
            result["errors"].append(
                "Could not navigate to Document Center using the View Statements in Document Center link. "
                + page_diagnostic(driver)
            )

        known_files = {path for path in config.CITIZENS_DOWNLOAD_DIR.iterdir() if path.is_file()}
        for row in statement_rows:
            result["data"]["documents"].append(
                {
                    "received_date": row.get("received_date"),
                    "account_label": row.get("account_label"),
                    "document_type": row.get("document_type"),
                    "statement_name": row.get("statement_name"),
                    "source_url_optional": row.get("source_url_optional"),
                }
            )
            metadata_base = {
                "document_type": row.get("document_type"),
                "statement_name": row.get("statement_name"),
                "received_date": row.get("received_date"),
                "account_label": row.get("account_label"),
                "local_path": None,
                "file_hash": None,
                "downloaded_at": utc_now(),
                "source_url_optional": row.get("source_url_optional"),
            }
            metadata = download_statement(row, known_files)
            if metadata:
                known_files.add(Path(metadata["local_path"]))
                result["data"]["downloads"].append(metadata)
                log_event("files downloaded", path=metadata["local_path"])
                record_document(account_id, metadata)
            else:
                record_document(account_id, metadata_base)

        transaction_count, inserted_transactions = capture_checking_transactions(driver, result, account_id)
        if not transaction_count:
            result["errors"].append(
                "Could not export or identify transaction rows after opening the Checking account page. "
                + page_diagnostic(driver)
            )

        message = (
            f"Citizens sync completed. Captured {len(statement_rows)} statement rows "
            f"and {transaction_count} transaction rows ({inserted_transactions} new)."
        )
        if result["errors"]:
            message = "Citizens sync completed with v1 selector TODOs."
        finish_result(result, True, config.STATUS_SUCCESS, message)
        log_event("worker completed", message=message, worker=WORKER_NAME)
        return 0
    except Exception as exc:
        result["errors"].append(str(exc))
        if config.CITIZENS_CAPTURE_SCREENSHOTS and driver:
            screenshot = save_screenshot(driver, "citizens_worker_failed")
            if screenshot:
                result["screenshots"].append(screenshot)
                log_event("screenshots captured", path=screenshot)
        finish_result(result, False, config.STATUS_FAILED, "Citizens sync failed. See last_result.json for details.")
        log_event("worker failed", message=str(exc), worker=WORKER_NAME)
        return 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only Citizens checking sync worker.")
    parser.add_argument("--setup", action="store_true", help="Open the Citizens Chrome profile for manual setup.")
    parser.add_argument(
        "--transactions-only",
        action="store_true",
        help="Capture visible Citizens account activity without downloading statements.",
    )
    parser.add_argument(
        "--statement-range",
        choices=["last60", "all"],
        default="last60",
        help="Document Center statement range to apply.",
    )
    args = parser.parse_args()

    started_at = utc_now()
    if args.setup:
        return run_setup(started_at)
    return run_sync(started_at, transactions_only=args.transactions_only, statement_range=args.statement_range)


if __name__ == "__main__":
    raise SystemExit(main())
