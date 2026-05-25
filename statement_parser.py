from __future__ import annotations

import csv
import hashlib
import io
import calendar
import json
import re
import sqlite3
from collections import defaultdict
from datetime import date, datetime, time, timezone
from pathlib import Path
from statistics import mean, median
from typing import Any

from pypdf import PdfReader


ROOT_DIR = Path(__file__).resolve().parent
STATEMENTS_DIR = ROOT_DIR / "Statements"
DATA_DIR = ROOT_DIR / "data"
DB_PATH = DATA_DIR / "dashboard.db"

ACCOUNTS = {
    "amex_gold": ("Amex Gold Card", "credit"),
    "citi_costco_visa": ("Citi Costco Anywhere", "credit"),
    "chase_prime_visa": ("Chase Prime Visa", "credit"),
    "vanguard_retirement": ("Vanguard Retirement", "retirement"),
}

LEGACY_SITE_MAP = {
    "amex": "amex_gold",
    "citi": "citi_costco_visa",
    "chase": "chase_prime_visa",
}

DEFAULT_SETTINGS = {
    "recurring_min_occurrences": 2,
    "night_mode": 0,
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_space(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def parse_money(value: str | None) -> float | None:
    text = normalize_space(value).replace("$", "").replace(",", "")
    if not text:
        return None
    if text.startswith("(") and text.endswith(")"):
        text = f"-{text[1:-1]}"
    if text.startswith("."):
        text = f"0{text}"
    if text.startswith("-."):
        text = text.replace("-.", "-0.", 1)
    try:
        return float(text)
    except ValueError:
        return None


def parse_date(value: str | None) -> date | None:
    text = normalize_space(value)
    text = re.sub(r",\s*", ", ", text)
    for fmt in ("%m/%d/%Y", "%B %d, %Y", "%b. %d, %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    return None


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def init_db() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                site TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                account_type TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS statements (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER NOT NULL,
                file_path TEXT NOT NULL,
                file_name TEXT NOT NULL,
                period_start TEXT,
                period_end TEXT,
                ending_balance REAL,
                file_hash TEXT NOT NULL UNIQUE,
                imported_at TEXT NOT NULL,
                FOREIGN KEY (account_id) REFERENCES accounts(id)
            );

            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER NOT NULL,
                site TEXT NOT NULL,
                transaction_date TEXT NOT NULL,
                posted_date TEXT,
                description TEXT NOT NULL,
                amount REAL NOT NULL,
                debit_credit TEXT NOT NULL,
                category TEXT NOT NULL,
                source_file TEXT NOT NULL,
                source_hash TEXT NOT NULL UNIQUE,
                imported_at TEXT NOT NULL,
                FOREIGN KEY (account_id) REFERENCES accounts(id)
            );

            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS scan_folders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                folder_path TEXT NOT NULL UNIQUE,
                site TEXT NOT NULL,
                account_name TEXT NOT NULL,
                account_type TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS manual_assets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                institution TEXT,
                asset_type TEXT NOT NULL,
                balance_type TEXT NOT NULL,
                amount REAL NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS sync_balances (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER NOT NULL,
                site TEXT NOT NULL,
                account_name TEXT NOT NULL,
                account_type TEXT NOT NULL,
                balance_type TEXT NOT NULL DEFAULT 'current',
                amount REAL NOT NULL,
                currency TEXT NOT NULL DEFAULT 'USD',
                captured_at TEXT NOT NULL,
                source TEXT NOT NULL,
                raw_text TEXT,
                FOREIGN KEY (account_id) REFERENCES accounts(id)
            );

            CREATE TABLE IF NOT EXISTS sync_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                institution TEXT NOT NULL,
                mode TEXT NOT NULL,
                status TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                message TEXT,
                result_json TEXT
            );
            """
        )
        for site, (name, account_type) in ACCOUNTS.items():
            conn.execute(
                "INSERT OR IGNORE INTO accounts (site, name, account_type) VALUES (?, ?, ?)",
                (site, name, account_type),
            )
        for key, value in DEFAULT_SETTINGS.items():
            conn.execute(
                "INSERT OR IGNORE INTO app_settings (key, value) VALUES (?, ?)",
                (key, str(value)),
            )
        conn.execute(
            """
            UPDATE app_settings
            SET value = '2'
            WHERE key = 'recurring_min_occurrences'
              AND value = '3'
            """
        )
        conn.execute(
            """
            UPDATE transactions
            SET category = 'Transfers & Payments'
            WHERE lower(description) LIKE 'to savings%'
               OR lower(description) LIKE 'from savings%'
            """
        )


def connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
    return slug or "custom"


def setting_int(settings: dict[str, Any], key: str) -> int:
    try:
        return int(settings.get(key, DEFAULT_SETTINGS[key]))
    except (TypeError, ValueError):
        return int(DEFAULT_SETTINGS[key])


def get_settings() -> dict[str, Any]:
    init_db()
    with connection() as conn:
        rows = conn.execute("SELECT key, value FROM app_settings").fetchall()
        values = {row["key"]: row["value"] for row in rows}
        settings = {
            key: int(values.get(key, default))
            for key, default in DEFAULT_SETTINGS.items()
        }
        folders = [
            dict(row)
            for row in conn.execute(
                """
                SELECT id, folder_path, site, account_name, account_type, enabled
                FROM scan_folders
                ORDER BY account_name, folder_path
                """
            )
        ]
        accounts = [
            dict(row)
            for row in conn.execute("SELECT site, name, account_type FROM accounts ORDER BY name")
        ]
        manual_assets = [
            dict(row)
            for row in conn.execute(
                """
                SELECT id, name, institution, asset_type, balance_type, amount, updated_at
                FROM manual_assets
                ORDER BY balance_type, institution, name
                """
            )
        ]
    return {
        "settings": settings,
        "folders": folders,
        "built_in_folders": built_in_folders(),
        "accounts": accounts,
        "manual_assets": manual_assets,
    }


def built_in_folders() -> list[dict[str, Any]]:
    folders = []
    known = {
        "citizens": ("citizens", "Citizens Checking", "checking"),
        "amex": ("amex_gold", "Amex Gold Card + HYSA", "credit/savings"),
        "citi bank": ("citi_costco_visa", "Citi Costco Anywhere", "credit"),
        "chase": ("chase_prime_visa", "Chase Prime Visa", "credit"),
    }
    if not STATEMENTS_DIR.exists():
        return folders
    for child in sorted(path for path in STATEMENTS_DIR.iterdir() if path.is_dir()):
        key = child.name.lower()
        site, account_name, account_type = known.get(key, (slugify(child.name), child.name, "debit"))
        files = [path for path in child.rglob("*") if path.is_file() and path.suffix.lower() in {".csv", ".pdf"}]
        folders.append(
            {
                "folder_path": str(child),
                "site": site,
                "account_name": account_name,
                "account_type": account_type,
                "enabled": 1,
                "source": "default",
                "file_count": len(files),
            }
        )
    return folders


def update_settings(payload: dict[str, Any]) -> dict[str, Any]:
    init_db()
    updates = {
        "recurring_min_occurrences": max(2, min(24, int(payload.get("recurring_min_occurrences", DEFAULT_SETTINGS["recurring_min_occurrences"])))),
        "night_mode": 1 if str(payload.get("night_mode", DEFAULT_SETTINGS["night_mode"])).lower() in {"1", "true", "yes", "on"} else 0,
    }
    with connection() as conn:
        for key, value in updates.items():
            conn.execute(
                """
                INSERT INTO app_settings (key, value)
                VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, str(value)),
            )
    return get_settings()


def add_scan_folder(payload: dict[str, Any]) -> dict[str, Any]:
    init_db()
    folder_path = normalize_space(payload.get("folder_path"))
    account_name = normalize_space(payload.get("account_name")) or "Custom Account"
    account_type = normalize_space(payload.get("account_type")).lower()
    if account_type not in {"debit", "credit", "checking", "savings"}:
        account_type = "debit"
    if not folder_path:
        raise ValueError("Folder path is required.")
    path = Path(folder_path).expanduser()
    if not path.exists() or not path.is_dir():
        raise ValueError(f"Folder does not exist: {folder_path}")
    site = slugify(payload.get("site") or account_name)
    with connection() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO accounts (site, name, account_type)
            VALUES (?, ?, ?)
            """,
            (site, account_name, account_type),
        )
        conn.execute(
            """
            INSERT INTO scan_folders (folder_path, site, account_name, account_type, enabled, created_at)
            VALUES (?, ?, ?, ?, 1, ?)
            ON CONFLICT(folder_path) DO UPDATE SET
                site = excluded.site,
                account_name = excluded.account_name,
                account_type = excluded.account_type,
                enabled = 1
            """,
            (str(path.resolve()), site, account_name, account_type, utc_now()),
        )
    return get_settings()


def delete_scan_folder(folder_id: int) -> dict[str, Any]:
    init_db()
    with connection() as conn:
        conn.execute("DELETE FROM scan_folders WHERE id = ?", (folder_id,))
    return get_settings()


def add_manual_asset(payload: dict[str, Any]) -> dict[str, Any]:
    init_db()
    name = normalize_space(payload.get("name"))
    institution = normalize_space(payload.get("institution"))
    asset_type = normalize_space(payload.get("asset_type")).lower() or "other"
    balance_type = normalize_space(payload.get("balance_type")).lower()
    amount = parse_money(str(payload.get("amount", "")))
    if not name:
        raise ValueError("Asset name is required.")
    if balance_type not in {"asset", "liability"}:
        balance_type = "asset"
    if amount is None:
        raise ValueError("Amount is required.")
    with connection() as conn:
        conn.execute(
            """
            INSERT INTO manual_assets (name, institution, asset_type, balance_type, amount, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (name, institution, asset_type, balance_type, abs(amount), utc_now()),
        )
    return get_settings()


def delete_manual_asset(asset_id: int) -> dict[str, Any]:
    init_db()
    with connection() as conn:
        conn.execute("DELETE FROM manual_assets WHERE id = ?", (asset_id,))
    return get_settings()


def net_worth_category(account_type: str | None, balance_type: str | None) -> str:
    if normalize_space(balance_type).lower() == "liability":
        return "liability"
    account_type = normalize_space(account_type).lower()
    if account_type == "rewards":
        return "rewards"
    if account_type in {"investment", "retirement"}:
        return "investment"
    return "asset"


def account_id(conn: sqlite3.Connection, site: str) -> int:
    site = LEGACY_SITE_MAP.get(site, site)
    row = conn.execute("SELECT id FROM accounts WHERE site = ?", (site,)).fetchone()
    if row:
        return int(row["id"])
    if site == "citizens_2439":
        name, account_type = ("Citizens Checking", "checking")
    elif site == "citizens_6746":
        name, account_type = ("Citizens Savings", "savings")
    elif site == "amex_gold":
        name, account_type = ("Amex Gold Card", "credit")
    elif site == "amex_gold_rewards":
        name, account_type = ("Amex Gold Card Rewards", "rewards")
    elif site == "chase_prime_visa":
        name, account_type = ("Chase Prime Visa", "credit")
    elif site == "chase_prime_rewards":
        name, account_type = ("Chase Rewards", "rewards")
    elif site == "citi_costco_visa":
        name, account_type = ("Citi Costco Anywhere", "credit")
    elif site == "citi_costco_rewards":
        name, account_type = ("Costco Cash Rewards", "rewards")
    elif site == "vanguard_retirement":
        name, account_type = ("Vanguard Retirement", "retirement")
    else:
        name, account_type = ACCOUNTS.get(site, (site.replace("_", " ").title(), "debit"))
    cursor = conn.execute("INSERT INTO accounts (site, name, account_type) VALUES (?, ?, ?)", (site, name, account_type))
    return int(cursor.lastrowid)


def classify(description: str, raw_category: str | None, amount: float, site: str) -> str:
    text = f"{description} {raw_category or ''}".lower()
    if any(word in text for word in ["payroll", "salary", "ucbenefits", "irs treas", "direct dep"]):
        return "Income"
    if any(
        word in text
        for word in [
            "online transfer",
            "payment thank you",
            "mobile payment - thank you",
            "online payment - thank you",
            "amex epayment",
            "citi card online payment",
            "chase credit crd",
            "americanexpress transfer",
            "applecard gsbank payment",
            "synchrony bank cc pymt",
            "to savings",
            "from savings",
        ]
    ):
        return "Transfers & Payments"
    if any(word in text for word in ["electric", "peco", "verizon", "comcast", "xfinity", "insurance", "utility", "phone"]):
        return "Bills & Utilities"
    if any(word in text for word in ["membership", "subscription", "netflix", "spotify", "prime", "apple.com"]):
        return "Subscriptions"
    if any(word in text for word in ["grocery", "groceries", "supermarket", "costco whse", "market"]):
        return "Groceries"
    if any(word in text for word in ["restaurant", "dining", "coffee", "cafe", "pizza", "doordash", "banh mi"]):
        return "Food & Dining"
    if any(word in text for word in ["gas", "fuel", "parking", "uber", "lyft", "transit", "toll"]):
        return "Transportation"
    if any(word in text for word in ["hotel", "lodging", "airline", "travel", "resort", "iberostar"]):
        return "Travel"
    if any(word in text for word in ["pharmacy", "medical", "health", "doctor", "cvs pharmacy"]):
        return "Health"
    if any(word in text for word in ["fee", "adjustment", "finance charge"]):
        return "Fees & Adjustments"
    if any(word in text for word in ["shopping", "merchandise", "amazon", "target", "walmart", "store"]):
        return "Shopping"
    if site.startswith("citizens") and amount > 0:
        return "Income"
    fallback = normalize_space(raw_category)
    return fallback.split("-")[-1] if fallback else "Other"


def source_hash(site: str, row: dict[str, Any]) -> str:
    key = "|".join(
        [
            site,
            row.get("transaction_date") or "",
            row.get("posted_date") or "",
            row.get("description") or "",
            f"{row.get('amount', 0):.2f}",
            row.get("source_file") or "",
        ]
    )
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def insert_transaction(conn: sqlite3.Connection, site: str, row: dict[str, Any]) -> bool:
    site = LEGACY_SITE_MAP.get(site, site)
    row["source_hash"] = source_hash(site, row)
    cursor = conn.execute(
        """
        INSERT OR IGNORE INTO transactions (
            account_id, site, transaction_date, posted_date, description, amount,
            debit_credit, category, source_file, source_hash, imported_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            account_id(conn, site),
            site,
            row["transaction_date"],
            row.get("posted_date"),
            row["description"],
            row["amount"],
            row["debit_credit"],
            row["category"],
            row["source_file"],
            row["source_hash"],
            utc_now(),
        ),
    )
    return cursor.rowcount > 0


def csv_rows(path: Path) -> list[dict[str, str]]:
    text = ""
    for encoding in ("utf-8-sig", "utf-8", "cp1252"):
        try:
            text = path.read_text(encoding=encoding)
            break
        except UnicodeDecodeError:
            pass
    lines = text.splitlines()
    header_index = 0
    for index, line in enumerate(lines):
        if "Date" in line and ("Description" in line or "Transaction Date" in line):
            header_index = index
            break
    return [dict(row) for row in csv.DictReader(io.StringIO("\n".join(lines[header_index:])))]


def parse_amex(path: Path) -> list[dict[str, Any]]:
    parsed = []
    for row in csv_rows(path):
        tx_date = parse_date(row.get("Date"))
        amount = parse_money(row.get("Amount"))
        description = normalize_space(row.get("Description"))
        if not tx_date or amount is None or not description:
            continue
        signed = abs(amount) if amount < 0 or "payment" in description.lower() else -abs(amount)
        parsed.append(
            {
                "transaction_date": tx_date.isoformat(),
                "posted_date": tx_date.isoformat(),
                "description": description[:300],
                "amount": signed,
                "debit_credit": "credit" if signed > 0 else "debit",
                "category": classify(description, row.get("Category"), signed, "amex"),
                "source_file": str(path),
            }
        )
    return parsed


def parse_citi(path: Path) -> list[dict[str, Any]]:
    parsed = []
    for row in csv_rows(path):
        tx_date = parse_date(row.get("Date"))
        debit = parse_money(row.get("Debit"))
        credit = parse_money(row.get("Credit"))
        description = normalize_space(row.get("Description"))
        if not tx_date or not description:
            continue
        if debit is not None:
            signed = -abs(debit)
        elif credit is not None:
            signed = abs(credit)
        else:
            continue
        parsed.append(
            {
                "transaction_date": tx_date.isoformat(),
                "posted_date": tx_date.isoformat(),
                "description": description[:300],
                "amount": signed,
                "debit_credit": "credit" if signed > 0 else "debit",
                "category": classify(description, row.get("Category"), signed, "citi"),
                "source_file": str(path),
            }
        )
    return parsed


def parse_chase(path: Path) -> list[dict[str, Any]]:
    parsed = []
    for row in csv_rows(path):
        tx_date = parse_date(row.get("Transaction Date"))
        post_date = parse_date(row.get("Post Date"))
        amount = parse_money(row.get("Amount"))
        description = normalize_space(row.get("Description"))
        if not tx_date or amount is None or not description:
            continue
        parsed.append(
            {
                "transaction_date": tx_date.isoformat(),
                "posted_date": (post_date or tx_date).isoformat(),
                "description": description[:300],
                "amount": amount,
                "debit_credit": "credit" if amount > 0 else "debit",
                "category": classify(description, row.get("Category"), amount, "chase"),
                "source_file": str(path),
            }
        )
    return parsed


def normalized_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def row_value(row: dict[str, str], names: tuple[str, ...]) -> str:
    by_key = {normalized_key(key): value for key, value in row.items()}
    for name in names:
        value = by_key.get(normalized_key(name))
        if value not in (None, ""):
            return normalize_space(value)
    return ""


def parse_generic_csv(path: Path, site: str, account_type: str) -> list[dict[str, Any]]:
    parsed = []
    is_credit = account_type == "credit"
    for row in csv_rows(path):
        tx_date = parse_date(row_value(row, ("Date", "Transaction Date", "Trans Date", "Posted Date", "Posting Date")))
        post_date = parse_date(row_value(row, ("Post Date", "Posted Date", "Posting Date")))
        description = row_value(row, ("Description", "Payee", "Merchant", "Name", "Memo", "Transaction"))
        amount = parse_money(row_value(row, ("Amount", "Transaction Amount", "Debit/Credit")))
        debit = parse_money(row_value(row, ("Debit", "Withdrawal", "Withdrawals", "Charge")))
        credit = parse_money(row_value(row, ("Credit", "Deposit", "Deposits", "Payment")))
        raw_category = row_value(row, ("Category", "Type"))
        if not tx_date or not description:
            continue
        if debit is not None:
            signed = -abs(debit)
        elif credit is not None:
            signed = abs(credit)
        elif amount is not None and is_credit:
            signed = abs(amount) if amount < 0 or "payment" in description.lower() else -abs(amount)
        elif amount is not None:
            signed = amount
        else:
            continue
        category = classify(description, raw_category, signed, site)
        if signed > 0 and not is_credit and category != "Transfers & Payments":
            category = "Income"
        parsed.append(
            {
                "transaction_date": tx_date.isoformat(),
                "posted_date": (post_date or tx_date).isoformat(),
                "description": description[:300],
                "amount": signed,
                "debit_credit": "credit" if signed > 0 else "debit",
                "category": category,
                "source_file": str(path),
            }
        )
    return parsed


def statement_period(text: str, path: Path) -> tuple[date | None, date | None]:
    compact = normalize_space(text)
    match = re.search(
        r"Beginning\s+([A-Za-z]+\.?\s+\d{1,2},\s*\d{4})\s+through\s+([A-Za-z]+\.?\s+\d{1,2},\s*\d{4})",
        compact,
        re.IGNORECASE,
    )
    if match:
        return parse_date(match.group(1)), parse_date(match.group(2))
    name_match = re.search(r"STATEMENTS,([A-Za-z]+)(\d{4})", path.name)
    if name_match:
        month_name, year = name_match.groups()
        parsed = datetime.strptime(f"{month_name} 1 {year}", "%B %d %Y").date()
        last_day = calendar.monthrange(parsed.year, parsed.month)[1]
        return None, date(parsed.year, parsed.month, last_day)
    return None, None


def resolve_mmdd(mmdd: str, start: date | None, end: date | None) -> date | None:
    if not start and not end:
        return None
    month, day = [int(part) for part in mmdd.split("/")]
    years = []
    if start:
        years.append(start.year)
    if end and end.year not in years:
        years.append(end.year)
    for year in years:
        candidate = date(year, month, day)
        if (not start or candidate >= start) and (not end or candidate <= end):
            return candidate
    return date((end or start).year, month, day)


def parse_citizens_pdf(path: Path) -> tuple[list[dict[str, Any]], date | None, date | None, float | None]:
    reader = PdfReader(str(path))
    text = "\n".join((page.extract_text(extraction_mode="layout") or "") for page in reader.pages)
    start, end = statement_period(text, path)
    compact = normalize_space(text)
    balance_matches = re.findall(r"Current Balance\s*=?\s*([+-]?\s*(?:\d[\d,]*|\d*)?\.\d{2})", compact, re.IGNORECASE)
    ending_balance = parse_money(balance_matches[-1]) if balance_matches else None
    mode: str | None = None
    parsed = []
    transaction_line = re.compile(r"^\s*(\d{2}/\d{2})\s+((?:\d[\d,]*|\d*)?\.\d{2})\s+(.+?)\s*$")

    for raw in text.splitlines():
        line = normalize_space(raw)
        lowered = line.lower()
        if lowered.startswith("withdrawals & debits") or lowered.startswith("other withdrawals"):
            mode = "debit"
            continue
        if lowered.startswith("deposits & credits"):
            mode = "credit"
            continue
        if "daily balance" in lowered or lowered.startswith("news from citizens"):
            mode = None
        if not mode:
            continue
        match = transaction_line.match(raw)
        if match:
            tx_date = resolve_mmdd(match.group(1), start, end)
            amount = parse_money(match.group(2))
            description = normalize_space(re.sub(r"\s+Total\s+.*$", "", match.group(3), flags=re.IGNORECASE))
            if not tx_date or amount is None or not description:
                continue
            signed = abs(amount) if mode == "credit" else -abs(amount)
            parsed.append(
                {
                    "transaction_date": tx_date.isoformat(),
                    "posted_date": tx_date.isoformat(),
                    "description": description[:300],
                    "amount": signed,
                    "debit_credit": mode,
                    "category": classify(description, None, signed, "citizens"),
                    "source_file": str(path),
                }
            )
        elif parsed and line and not lowered.startswith(("date amount", "page ", "total ", "current balance", "previous balance")):
            parsed[-1]["description"] = normalize_space(f"{parsed[-1]['description']} {line}")[:300]
            parsed[-1]["category"] = classify(parsed[-1]["description"], None, parsed[-1]["amount"], "citizens")
    return parsed, start, end, ending_balance


def parse_amex_hysa_pdf(path: Path) -> tuple[date | None, date | None, float | None, list[dict[str, Any]]]:
    reader = PdfReader(str(path))
    text = "\n".join((page.extract_text(extraction_mode="layout") or "") for page in reader.pages)
    readable_text = "\n".join((page.extract_text() or "") for page in reader.pages)
    compact = normalize_space(text)
    readable_compact = normalize_space(readable_text)
    period_match = re.search(
        r"Statement Period:\s+([A-Za-z]+\.?\s+\d{1,2},\s*\d{4})\s+-\s+([A-Za-z]+\.?\s+\d{1,2},\s*\d{4})",
        compact,
        re.IGNORECASE,
    )
    start = parse_date(period_match.group(1)) if period_match else None
    end = parse_date(period_match.group(2)) if period_match else None
    balance_matches = re.findall(r"Ending Balance\s+\$?([+-]?(?:\d[\d,]*|\d*)?\.\d{2})", compact, re.IGNORECASE)
    ending_balance = parse_money(balance_matches[-1]) if balance_matches else None
    interest_rows = []
    for match in re.finditer(
        r"(\d{2}/\d{2}/\d{4})\s+Interest Payment\s+\$?([+-]?(?:\d[\d,]*|\d*)?\.\d{2})",
        readable_compact,
        re.IGNORECASE,
    ):
        tx_date = parse_date(match.group(1))
        amount = parse_money(match.group(2))
        if not tx_date or amount is None:
            continue
        interest_rows.append(
            {
                "transaction_date": tx_date.isoformat(),
                "posted_date": tx_date.isoformat(),
                "description": "Interest Payment",
                "amount": abs(amount),
                "debit_credit": "credit",
                "category": "Income",
                "source_file": str(path),
            }
        )
    if not interest_rows:
        interest_match = re.search(
            r"Interest Credited This Period\s+\$?([+-]?(?:\d[\d,]*|\d*)?\.\d{2})",
            readable_compact,
            re.IGNORECASE,
        )
        amount = parse_money(interest_match.group(1)) if interest_match else None
        if end and amount is not None:
            interest_rows.append(
                {
                    "transaction_date": end.isoformat(),
                    "posted_date": end.isoformat(),
                    "description": "Interest Payment",
                    "amount": abs(amount),
                    "debit_credit": "credit",
                    "category": "Income",
                    "source_file": str(path),
                }
            )
    return start, end, ending_balance, interest_rows


def citizens_account_metadata(path: Path) -> tuple[str, str, str]:
    suffix_match = re.search(r"-(\d+)\.pdf$", path.name, re.IGNORECASE)
    suffix = suffix_match.group(1) if suffix_match else "account"
    lower_name = path.name.lower()
    if suffix == "6746" or "savings" in lower_name:
        return f"citizens_{suffix}", "Citizens Savings", "savings"
    return f"citizens_{suffix}", "Citizens Checking", "checking"


def statement_files() -> list[Path]:
    if not STATEMENTS_DIR.exists():
        return []
    return sorted(path for path in STATEMENTS_DIR.rglob("*") if path.is_file() and path.suffix.lower() in {".csv", ".pdf"})


def custom_scan_folders(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in conn.execute(
            """
            SELECT folder_path, site, account_name, account_type
            FROM scan_folders
            WHERE enabled = 1
            ORDER BY folder_path
            """
        )
    ]


def import_statements() -> dict[str, Any]:
    init_db()
    stats = {"files_seen": 0, "files_imported": 0, "transactions_seen": 0, "transactions_inserted": 0, "statements_inserted": 0, "errors": []}
    with connection() as conn:
        conn.execute("DELETE FROM transactions WHERE source_file NOT LIKE 'sync:%'")
        conn.execute("DELETE FROM statements")
        conn.execute("DELETE FROM accounts WHERE site = 'citizens'")
        scan_items: list[tuple[Path, dict[str, Any] | None]] = [(path, None) for path in statement_files()]
        for folder in custom_scan_folders(conn):
            folder_path = Path(folder["folder_path"])
            if not folder_path.exists():
                stats["errors"].append({"file": str(folder_path), "error": "Scan folder does not exist."})
                continue
            for path in sorted(folder_path.rglob("*")):
                if path.is_file() and path.suffix.lower() in {".csv", ".pdf"}:
                    scan_items.append((path, folder))

        seen_paths: set[tuple[str, str]] = set()
        for path, folder in scan_items:
            path_key = (str(path.resolve()).lower(), (folder or {}).get("site", "builtin"))
            if path_key in seen_paths:
                continue
            seen_paths.add(path_key)
            stats["files_seen"] += 1
            try:
                parts = {part.lower() for part in path.parts}
                site = None
                rows: list[dict[str, Any]] = []
                if folder and path.suffix.lower() == ".csv":
                    site = LEGACY_SITE_MAP.get(folder["site"], folder["site"])
                    account_name, account_type = ACCOUNTS.get(site, (folder["account_name"], folder["account_type"]))
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO accounts (site, name, account_type)
                        VALUES (?, ?, ?)
                        """,
                        (site, account_name, account_type),
                    )
                    rows = parse_generic_csv(path, site, account_type)
                elif folder and path.suffix.lower() == ".pdf" and "citizens" not in folder["site"].lower():
                    continue
                elif path.suffix.lower() == ".pdf" and path.name.upper().startswith("STATEMENTS,") and ("citizens" in parts or (folder and "citizens" in folder["site"].lower())):
                    site, account_name, account_type = citizens_account_metadata(path)
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO accounts (site, name, account_type)
                        VALUES (?, ?, ?)
                        """,
                        (site, account_name, account_type),
                    )
                    rows, start, end, ending_balance = parse_citizens_pdf(path)
                    digest = file_hash(path)
                    cursor = conn.execute(
                        """
                        INSERT INTO statements (
                            account_id, file_path, file_name, period_start, period_end,
                            ending_balance, file_hash, imported_at
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            account_id(conn, site),
                            str(path),
                            path.name,
                            start.isoformat() if start else None,
                            end.isoformat() if end else None,
                            ending_balance,
                            digest,
                            utc_now(),
                        ),
                    )
                    stats["statements_inserted"] += cursor.rowcount
                elif path.suffix.lower() == ".csv" and "amex" in parts:
                    site = "amex_gold"
                    rows = parse_amex(path)
                elif path.suffix.lower() == ".pdf" and "amex" in parts:
                    site = "amex_hysa"
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO accounts (site, name, account_type)
                        VALUES (?, ?, ?)
                        """,
                        (site, "Amex High Yield Savings", "savings"),
                    )
                    start, end, ending_balance, rows = parse_amex_hysa_pdf(path)
                    if ending_balance is None:
                        continue
                    digest = file_hash(path)
                    cursor = conn.execute(
                        """
                        INSERT INTO statements (
                            account_id, file_path, file_name, period_start, period_end,
                            ending_balance, file_hash, imported_at
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            account_id(conn, site),
                            str(path),
                            path.name,
                            start.isoformat() if start else None,
                            end.isoformat() if end else None,
                            ending_balance,
                            digest,
                            utc_now(),
                        ),
                    )
                    stats["statements_inserted"] += cursor.rowcount
                elif path.suffix.lower() == ".csv" and "citi bank" in parts:
                    site = "citi_costco_visa"
                    rows = parse_citi(path)
                elif path.suffix.lower() == ".csv" and "chase" in parts:
                    site = "chase_prime_visa"
                    rows = parse_chase(path)
                if not site:
                    continue
                inserted = sum(1 for row in rows if insert_transaction(conn, site, row))
                stats["transactions_seen"] += len(rows)
                stats["transactions_inserted"] += inserted
                stats["files_imported"] += 1
            except Exception as exc:
                stats["errors"].append({"file": str(path), "error": str(exc)})
    return stats


def is_transfer_or_payment(row: dict[str, Any]) -> bool:
    description = (row.get("description") or "").lower()
    if is_internal_citizens_transfer(row):
        return True
    return (row.get("category") or "").lower() == "transfers & payments"


def is_internal_citizens_transfer(row: dict[str, Any]) -> bool:
    description = (row.get("description") or "").lower()
    return any(marker in description for marker in ["to savings", "from savings"])


def income_type(row: dict[str, Any]) -> str:
    description = (row.get("description") or "").lower()
    if any(marker in description for marker in ["zelle", "venmo", "cash app", "paypal", "mobile deposit", "realtime credit sender"]) and float(row.get("amount") or 0) > 0:
        return "Contributions"
    if any(marker in description for marker in ["payroll", "salary", "direct dep", "direct deposit", "printfly"]):
        return "Payroll"
    return "Interest"


def transaction_identity(row: dict[str, Any]) -> tuple[Any, ...]:
    description = (row.get("description") or "").lower()
    description = re.sub(r"please see additional information.*$", "", description)
    description = re.sub(r"one deposit savings.*$", "", description)
    if "zelle" in description:
        if "duc luong" in description:
            description = "zelle duc luong"
        else:
            description = re.sub(r"\b(?:net|epp|id|us)\w*\b|[0-9]{5,}", "", description)
    elif "printfly" in description and "payroll" in description:
        description = "printfly payroll"
    else:
        description = re.sub(r"\b\d{4,}\b", "", description)
    description = normalize_space(description)
    return (
        row.get("site"),
        row.get("transaction_date"),
        round(float(row.get("amount") or 0), 2),
        description,
    )


def dedupe_transaction_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[Any, ...]] = set()
    deduped = []
    for row in rows:
        key = transaction_identity(row)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(row)
    return deduped


def merchant_name(description: str) -> str:
    text = re.sub(r"\b\d{4,}\b", "", description.upper())
    text = re.sub(r"[^A-Z0-9&' ]+", " ", text)
    words = normalize_space(text).split()
    return " ".join(words[:4]).title() if words else "Unknown"


def similar_amounts(values: list[float], tolerance_percent: int) -> bool:
    if not values:
        return False
    center = median(values)
    if center == 0:
        return True
    allowed = abs(center) * (tolerance_percent / 100)
    return all(abs(value - center) <= allowed for value in values)


def recurring_transaction_key(description: str) -> str:
    text = re.sub(r"\b\d{4,}\b", "", description.upper())
    text = re.sub(r"\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b", "", text)
    text = re.sub(r"[^A-Z0-9&' ]+", " ", text)
    words = normalize_space(text).split()
    return " ".join(words[:6]).title() if words else "Unknown"


def recurring_candidates(rows: list[dict[str, Any]], settings: dict[str, Any]) -> list[dict[str, Any]]:
    min_occurrences = setting_int(settings, "recurring_min_occurrences")
    by_transaction: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if is_transfer_or_payment(row):
            continue
        by_transaction[recurring_transaction_key(row["description"])].append(row)

    candidates = []
    for name, transaction_rows in by_transaction.items():
        if len(transaction_rows) < min_occurrences:
            continue
        sorted_rows = sorted(transaction_rows, key=lambda row: (row["transaction_date"], row.get("id", 0)), reverse=True)
        amounts = [float(row["amount"]) for row in sorted_rows]
        candidates.append(
            {
                "name": name,
                "count": len(sorted_rows),
                "latest_date": sorted_rows[0]["transaction_date"],
                "total": round(sum(amounts), 2),
                "average": round(mean(amounts), 2),
                "transactions": [
                    {
                        "transaction_date": row["transaction_date"],
                        "account_name": row["account_name"],
                        "description": row["description"],
                        "category": row["category"],
                        "amount": round(float(row["amount"]), 2),
                    }
                    for row in sorted_rows
                ],
            }
        )
    candidates.sort(key=lambda item: (item["count"], item["latest_date"]), reverse=True)
    return candidates


def subtract_months(value: date, months: int) -> date:
    month_index = value.month - months - 1
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def period_window(period: str | None) -> tuple[str, date | None, date | None]:
    selected = normalize_space(period or "ytd").lower()
    today = date.today()
    if selected in {"all", "show_all"}:
        return "all", None, None
    if selected in {"1m", "month", "past_month"}:
        return "1m", subtract_months(today, 1), today
    if selected in {"3m", "past_3_months"}:
        return "3m", subtract_months(today, 3), today
    if selected in {"6m", "past_6_months"}:
        return "6m", subtract_months(today, 6), today
    if selected in {"12m", "year", "past_year"}:
        return "12m", subtract_months(today, 12), today
    return "ytd", date(today.year, 1, 1), today


def row_in_period(row: dict[str, Any], start: date | None, end: date | None) -> bool:
    if not start and not end:
        return True
    row_date = datetime.strptime(row["transaction_date"], "%Y-%m-%d").date()
    if start and row_date < start:
        return False
    if end and row_date > end:
        return False
    return True


def month_bounds(month_key: str) -> tuple[str, str]:
    year, month = [int(part) for part in month_key.split("-")]
    start = date(year, month, 1)
    end = date(year, month, calendar.monthrange(year, month)[1])
    return start.isoformat(), end.isoformat()


def filtered_transaction_rows(conn: sqlite3.Connection, institution: str | None, period: str | None) -> tuple[list[dict[str, Any]], str, date | None, date | None]:
    selected = normalize_space(institution or "all").lower()
    selected = "all" if selected in {"", "all"} else selected
    selected_period, start, end = period_window(period)
    params: tuple[Any, ...] = ()
    where = ""
    if selected == "citizens":
        where = "WHERE t.site IN ('citizens_2439', 'citizens_6746', 'citizens')"
    elif selected != "all":
        where = "WHERE t.site = ?"
        params = (selected,)
    rows = [
        dict(row)
        for row in conn.execute(
            f"""
            SELECT t.*, a.name AS account_name, a.account_type
            FROM transactions t
            JOIN accounts a ON a.id = t.account_id
            {where}
            ORDER BY t.transaction_date ASC, t.id ASC
            """,
            params,
        )
    ]
    return [row for row in rows if row_in_period(row, start, end)], selected_period, start, end


def dashboard_summary(institution: str | None = "all", period: str | None = "ytd") -> dict[str, Any]:
    init_db()
    selected = normalize_space(institution or "all").lower()
    selected = "all" if selected in {"", "all"} else selected
    settings_payload = get_settings()
    settings = settings_payload["settings"]
    with connection() as conn:
        rows, selected_period, period_start, period_end = filtered_transaction_rows(conn, selected, period)
        statement_where = ""
        statement_params: tuple[Any, ...] = ()
        if selected == "citizens":
            statement_where = "WHERE a.site IN ('citizens_2439', 'citizens_6746', 'citizens')"
        elif selected != "all":
            statement_where = "WHERE a.site = ?"
            statement_params = (selected,)
        statements = [dict(row) for row in conn.execute(
            f"""
            SELECT s.*, a.name AS account_name
            FROM statements s
            JOIN accounts a ON a.id = s.account_id
            {statement_where}
            ORDER BY s.period_end DESC, s.file_name DESC
            """,
            statement_params,
        )]
        institution_rows = [
            dict(row)
            for row in conn.execute("SELECT site, name, account_type FROM accounts ORDER BY name")
        ]
        if any(row["site"] in {"citizens_2439", "citizens_6746", "citizens"} for row in institution_rows):
            institution_rows = [
                {"site": "citizens", "name": "Citizens Checking + Savings", "account_type": "checking/savings"},
                *[row for row in institution_rows if row["site"] not in {"citizens_2439", "citizens_6746", "citizens"}],
            ]
    net_worth = net_worth_summary()
    rows = dedupe_transaction_rows(rows)

    if not rows:
        return {
            "date_range": None,
            "selected_institution": selected,
            "selected_period": selected_period,
            "period_start": period_start.isoformat() if period_start else None,
            "period_end": period_end.isoformat() if period_end else None,
            "institutions": institution_rows,
            "recurring_definition": {
                "min_occurrences": settings["recurring_min_occurrences"],
            },
            "totals": {},
            "monthly": [],
            "metric_details": {"bills_transactions": [], "income_types": []},
            "categories": [],
            "accounts": [],
            "recurring": [],
            "latest": [],
            "statements": statements,
            "net_worth": net_worth,
        }

    monthly: dict[str, dict[str, float]] = defaultdict(lambda: {"income": 0.0, "expenses": 0.0, "bills": 0.0, "net": 0.0})
    categories: dict[str, float] = defaultdict(float)
    accounts: dict[str, dict[str, Any]] = defaultdict(lambda: {"income": 0.0, "expenses": 0.0, "count": 0, "latest": ""})
    rows_for_recurring: list[dict[str, Any]] = []
    expense_detail_rows: list[dict[str, Any]] = []
    bill_detail_rows: list[dict[str, Any]] = []
    income_detail: dict[str, dict[str, Any]] = defaultdict(lambda: {"total": 0.0, "count": 0, "transactions": []})

    for row in rows:
        amount = float(row["amount"])
        month = row["transaction_date"][:7]
        category = row["category"]
        account = row["account_name"]
        accounts[account]["count"] += 1
        accounts[account]["latest"] = max(accounts[account]["latest"], row["transaction_date"])
        if not is_transfer_or_payment(row):
            rows_for_recurring.append(row)
        if category == "Income" and amount > 0 and not is_transfer_or_payment(row):
            kind = income_type(row)
            income_detail[kind]["total"] += amount
            income_detail[kind]["count"] += 1
            income_detail[kind]["transactions"].append(
                {
                    "transaction_date": row["transaction_date"],
                    "account_name": row["account_name"],
                    "description": row["description"],
                    "amount": round(amount, 2),
                }
            )
            monthly[month]["income"] += amount
            monthly[month]["net"] += amount
            accounts[account]["income"] += amount
        elif amount < 0 and not is_transfer_or_payment(row):
            expense = abs(amount)
            monthly[month]["expenses"] += expense
            monthly[month]["net"] -= expense
            categories[category] += expense
            accounts[account]["expenses"] += expense
            expense_detail_rows.append(
                {
                    "transaction_date": row["transaction_date"],
                    "account_name": row["account_name"],
                    "description": row["description"],
                    "category": category,
                    "amount": round(abs(amount), 2),
                }
            )
            if category in {"Bills & Utilities", "Subscriptions"}:
                monthly[month]["bills"] += expense
                bill_detail_rows.append(
                    {
                        "transaction_date": row["transaction_date"],
                        "account_name": row["account_name"],
                        "description": row["description"],
                        "category": category,
                        "amount": round(abs(amount), 2),
                    }
                )

    month_rows = []
    for key, values in sorted(monthly.items()):
        month_start, month_end = month_bounds(key)
        month_rows.append(
            {
                "month": key,
                "period_start": month_start,
                "period_end": month_end,
                **{name: round(value, 2) for name, value in values.items()},
            }
        )
    expenses = [row["expenses"] for row in month_rows if row["expenses"] > 0]
    income = [row["income"] for row in month_rows if row["income"] > 0]
    bills = [row["bills"] for row in month_rows if row["bills"] > 0]
    recurring_rows = recurring_candidates(rows_for_recurring, settings)
    account_rows = [
        {
            "name": name,
            "transactions": values["count"],
            "expenses": round(values["expenses"], 2),
            "income": round(values["income"], 2),
            "latest": values["latest"],
        }
        for name, values in sorted(accounts.items())
    ]
    latest = sorted(
        [row for row in rows if not is_internal_citizens_transfer(row)],
        key=lambda row: (row["transaction_date"], row["id"]),
        reverse=True,
    )[:200]
    latest_statement_balance = next((s for s in statements if s.get("ending_balance") is not None), None)
    expense_detail_rows = sorted(expense_detail_rows, key=lambda row: (row["transaction_date"], row["amount"]), reverse=True)
    bill_detail_rows = sorted(bill_detail_rows, key=lambda row: (row["transaction_date"], row["amount"]), reverse=True)
    income_detail_rows = [
        {
            "type": key,
            "total": round(value["total"], 2),
            "count": value["count"],
            "transactions": value["transactions"],
        }
        for key, value in sorted(income_detail.items(), key=lambda item: item[1]["total"], reverse=True)
    ]

    return {
        "date_range": {"start": rows[0]["transaction_date"], "end": rows[-1]["transaction_date"]},
        "selected_institution": selected,
        "selected_period": selected_period,
        "period_start": period_start.isoformat() if period_start else None,
        "period_end": period_end.isoformat() if period_end else None,
        "institutions": institution_rows,
        "recurring_definition": {
            "min_occurrences": settings["recurring_min_occurrences"],
        },
        "totals": {
            "transactions": len(rows),
            "average_monthly_expenses": round(mean(expenses), 2) if expenses else 0,
            "median_monthly_expenses": round(median(expenses), 2) if expenses else 0,
            "average_monthly_income": round(mean(income), 2) if income else 0,
            "median_monthly_income": round(median(income), 2) if income else 0,
            "average_monthly_bills": round(mean(bills), 2) if bills else 0,
            "median_monthly_bills": round(median(bills), 2) if bills else 0,
            "projected_monthly_net": round((median(income) if income else 0) - (median(expenses) if expenses else 0), 2),
            "latest_statement_balance": latest_statement_balance["ending_balance"] if latest_statement_balance else None,
            "latest_statement_date": latest_statement_balance["period_end"] if latest_statement_balance else None,
        },
        "monthly": month_rows,
        "metric_details": {
            "expenses_transactions": expense_detail_rows,
            "bills_transactions": bill_detail_rows,
            "income_types": income_detail_rows,
        },
        "categories": [{"category": key, "amount": round(value, 2)} for key, value in sorted(categories.items(), key=lambda item: item[1], reverse=True)[:12]],
        "accounts": account_rows,
        "recurring": recurring_rows[:50],
        "latest": latest,
        "statements": statements,
        "net_worth": net_worth,
    }


def transactions_for_month(institution: str | None, period: str | None, month: str) -> dict[str, Any]:
    init_db()
    if not re.fullmatch(r"\d{4}-\d{2}", month or ""):
        raise ValueError("Month must be in YYYY-MM format.")
    with connection() as conn:
        rows, selected_period, period_start, period_end = filtered_transaction_rows(conn, institution, period)
    rows = dedupe_transaction_rows(rows)
    month_rows = [row for row in rows if row["transaction_date"].startswith(month)]
    income = sum(float(row["amount"]) for row in month_rows if row["amount"] > 0 and row["category"] == "Income")
    expenses = sum(abs(float(row["amount"])) for row in month_rows if row["amount"] < 0 and not is_transfer_or_payment(row))
    bills = sum(abs(float(row["amount"])) for row in month_rows if row["amount"] < 0 and row["category"] in {"Bills & Utilities", "Subscriptions"})
    return {
        "month": month,
        "selected_period": selected_period,
        "period_start": period_start.isoformat() if period_start else None,
        "period_end": period_end.isoformat() if period_end else None,
        "totals": {
            "income": round(income, 2),
            "expenses": round(expenses, 2),
            "bills": round(bills, 2),
            "net": round(income - expenses, 2),
            "transactions": len(month_rows),
        },
        "transactions": sorted(month_rows, key=lambda row: (row["transaction_date"], row["id"]), reverse=True),
    }


def net_worth_summary() -> dict[str, Any]:
    init_db()
    entries: list[dict[str, Any]] = []
    with connection() as conn:
        accounts = [dict(row) for row in conn.execute("SELECT id, site, name, account_type FROM accounts ORDER BY name")]
        live_balances: dict[int, dict[str, Any]] = {}
        for row in conn.execute(
            """
            SELECT account_id, account_name, account_type, balance_type, amount, captured_at, source, raw_text
            FROM sync_balances
            WHERE id IN (
                SELECT MAX(id)
                FROM sync_balances
                GROUP BY account_id
            )
            """
        ):
            live_balances[int(row["account_id"])] = dict(row)
        for account in accounts:
            live_balance = live_balances.get(int(account["id"]))
            if live_balance:
                amount = float(live_balance["amount"])
                balance_type = normalize_space(live_balance.get("balance_type")).lower()
                if balance_type not in {"asset", "liability"}:
                    balance_type = "asset" if amount >= 0 else "liability"
                source_detail = live_balance["source"]
                raw_text = live_balance.get("raw_text") or ""
                if account["account_type"] == "rewards" and raw_text:
                    try:
                        raw_payload = json.loads(raw_text)
                        points = raw_payload.get("membership_points", raw_payload.get("rewards_points"))
                        if points is not None:
                            source_detail = f"{source_detail} ({int(points):,} points at $0.01/point)"
                    except (TypeError, ValueError, json.JSONDecodeError):
                        pass
                entries.append(
                    {
                        "name": live_balance.get("account_name") or account["name"],
                        "institution": account["name"],
                        "account_type": live_balance.get("account_type") or account["account_type"],
                        "balance_type": balance_type,
                        "amount": abs(round(amount, 2)),
                        "source": "sync",
                        "source_detail": source_detail,
                        "as_of": live_balance["captured_at"],
                    }
                )
                continue
            if account["account_type"] in {"checking", "savings", "debit"}:
                statement = conn.execute(
                    """
                    SELECT ending_balance, period_end, file_name, file_path
                    FROM statements
                    WHERE account_id = ? AND ending_balance IS NOT NULL
                    ORDER BY period_end DESC, id DESC
                    LIMIT 1
                    """,
                    (account["id"],),
                ).fetchone()
                if statement:
                    amount = float(statement["ending_balance"])
                    entries.append(
                        {
                            "name": account["name"],
                            "institution": account["name"],
                            "account_type": account["account_type"],
                            "balance_type": "asset" if amount >= 0 else "liability",
                            "amount": abs(round(amount, 2)),
                            "source": "statement",
                            "source_detail": f"{Path(statement['file_path']).parent.name}/{statement['file_name']}",
                            "as_of": statement["period_end"],
                        }
                    )
        manual_entries = [
            dict(row)
            for row in conn.execute(
                """
                SELECT name, institution, asset_type AS account_type, balance_type, amount, updated_at AS as_of
                FROM manual_assets
                ORDER BY balance_type, institution, name
                """
            )
        ]
        for row in manual_entries:
            row["source"] = "manual"
            row["source_detail"] = "Settings > Manual Assets & Liabilities"
            row["amount"] = round(float(row["amount"]), 2)
            entries.append(row)

    for entry in entries:
        entry["net_worth_category"] = net_worth_category(entry.get("account_type"), entry.get("balance_type"))

    assets = round(sum(entry["amount"] for entry in entries if entry["net_worth_category"] == "asset"), 2)
    rewards = round(sum(entry["amount"] for entry in entries if entry["net_worth_category"] == "rewards"), 2)
    investments = round(sum(entry["amount"] for entry in entries if entry["net_worth_category"] == "investment"), 2)
    liabilities = round(sum(entry["amount"] for entry in entries if entry["net_worth_category"] == "liability"), 2)
    total_assets = round(assets + rewards + investments, 2)
    return {
        "assets": assets,
        "rewards": rewards,
        "investments": investments,
        "total_assets": total_assets,
        "liabilities": liabilities,
        "net_worth": round(total_assets - liabilities, 2),
        "entries": entries,
    }
