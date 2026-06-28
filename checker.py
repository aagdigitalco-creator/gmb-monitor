import asyncio
import os
import re
import csv
import json
import requests
import psycopg2
from datetime import date
from playwright.async_api import async_playwright

# ── ENV ───────────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
DATABASE_URL     = os.environ["DATABASE_URL"]
SHEETS_CSV_URL   = os.environ["SHEETS_CSV_URL"]

FIELD_LABELS = {
    "rating":       "⭐ Rating",
    "review_count": "💬 Reviews",
    "name":         "🏷️ Name",
    "address":      "📍 Address",
    "phone":        "📞 Phone",
    "website":      "🌐 Website",
    "category":     "🗂️ Category",
    "hours":        "🕐 Hours",
    "status":       "🔴 Open/Closed Status",
}

# ── TELEGRAM ──────────────────────────────────────────────────────────────────
def send_telegram(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    # Telegram has a 4096 char limit — split if needed
    chunks = [text[i:i+4000] for i in range(0, len(text), 4000)]
    for chunk in chunks:
        requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": chunk,
            "parse_mode": "HTML"
        })

# ── GOOGLE SHEET ──────────────────────────────────────────────────────────────
def fetch_clients_from_sheet():
    resp = requests.get(SHEETS_CSV_URL, timeout=30)
    resp.raise_for_status()
    reader = csv.reader(resp.text.splitlines())
    clients = []
    for row in reader:
        if len(row) < 2:
            continue
        name, url = row[0].strip(), row[1].strip()
        if not url.startswith("http"):
            continue
        clients.append((name, url))
    return clients

# ── SCRAPING ──────────────────────────────────────────────────────────────────
async def scrape_location(page, url):
    data = {
        "rating":       None,
        "review_count": None,
        "name":         None,
        "address":      None,
        "phone":        None,
        "website":      None,
        "category":     None,
        "hours":        None,
        "status":       None,
    }

    try:
        await page.goto(url, timeout=45000, wait_until="networkidle")
        await page.wait_for_timeout(4000)

        # ── NAME ──────────────────────────────────────────────────────────────
        try:
            el = await page.query_selector("h1")
            if el:
                data["name"] = (await el.inner_text()).strip()
        except Exception:
            pass

        # ── RATING ────────────────────────────────────────────────────────────
        try:
            el = await page.query_selector("div.F7nice")
            if el:
                text = await el.inner_text()
                m = re.search(r'(\d+[.,]\d+)', text)
                if m:
                    data["rating"] = round(float(m.group(1).replace(",", ".")), 1)
        except Exception:
            pass

        if data["rating"] is None:
            try:
                for el in await page.query_selector_all("span[aria-label]"):
                    label = (await el.get_attribute("aria-label") or "").lower()
                    if "star" in label:
                        m = re.search(r'(\d+[.,]\d+)', label)
                        if m:
                            data["rating"] = round(float(m.group(1).replace(",", ".")), 1)
                            break
            except Exception:
                pass

        # ── REVIEW COUNT ──────────────────────────────────────────────────────
        try:
            for el in await page.query_selector_all("span[aria-label]"):
                label = (await el.get_attribute("aria-label") or "").lower()
                if "review" in label:
                    m = re.search(r'([\d,]+)', label)
                    if m:
                        data["review_count"] = int(m.group(1).replace(",", ""))
                        break
        except Exception:
            pass

        if data["review_count"] is None:
            try:
                el = await page.query_selector("div.F7nice")
                if el:
                    text = await el.inner_text()
                    # Rating block usually shows "4.5\n(120)" or "4.5(120)"
                    m = re.search(r'\(([\d,]+)\)', text)
                    if m:
                        data["review_count"] = int(m.group(1).replace(",", ""))
            except Exception:
                pass

        # ── ADDRESS ───────────────────────────────────────────────────────────
        try:
            selectors = [
                "[data-item-id='address'] .Io6YTe",
                "button[data-tooltip='Copy address'] .Io6YTe",
                "button[aria-label*='ddress'] .Io6YTe",
            ]
            for sel in selectors:
                el = await page.query_selector(sel)
                if el:
                    text = (await el.inner_text()).strip()
                    if text:
                        data["address"] = text
                        break
        except Exception:
            pass

        # ── PHONE ─────────────────────────────────────────────────────────────
        try:
            selectors = [
                "[data-tooltip='Copy phone number'] .Io6YTe",
                "[data-item-id^='phone:'] .Io6YTe",
                "button[aria-label*='hone'] .Io6YTe",
            ]
            for sel in selectors:
                el = await page.query_selector(sel)
                if el:
                    text = (await el.inner_text()).strip()
                    if text:
                        data["phone"] = text
                        break
        except Exception:
            pass

        # ── WEBSITE ───────────────────────────────────────────────────────────
        try:
            selectors = [
                "a[data-item-id='authority']",
                "a[data-tooltip='Open website']",
                "a[aria-label*='ebsite']",
            ]
            for sel in selectors:
                el = await page.query_selector(sel)
                if el:
                    href = await el.get_attribute("href")
                    if href and href.startswith("http"):
                        data["website"] = href
                        break
        except Exception:
            pass

        # ── CATEGORY ──────────────────────────────────────────────────────────
        try:
            selectors = [
                "button.DkEaL",
                "span.YhemCb",
                "[jsaction*='category']",
            ]
            for sel in selectors:
                el = await page.query_selector(sel)
                if el:
                    text = (await el.inner_text()).strip()
                    if text:
                        data["category"] = text
                        break
        except Exception:
            pass

        # ── HOURS (today's displayed hours) ───────────────────────────────────
        try:
            selectors = [
                ".t39EBf .G8aQO",
                ".o0Svhf",
                "[data-item-id='oh'] .Io6YTe",
                ".OqCZI .Io6YTe",
            ]
            for sel in selectors:
                el = await page.query_selector(sel)
                if el:
                    text = (await el.inner_text()).strip()
                    if text:
                        data["hours"] = text
                        break
        except Exception:
            pass

        # ── OPEN / CLOSED STATUS ──────────────────────────────────────────────
        try:
            selectors = [
                ".dHPLDd",
                "span.ZDu9vd",
                ".JzHdmf span",
            ]
            for sel in selectors:
                el = await page.query_selector(sel)
                if el:
                    text = (await el.inner_text()).strip()
                    if text:
                        data["status"] = text
                        break
        except Exception:
            pass

    except Exception as e:
        print(f"  ✗ Fatal error scraping {url}: {e}")

    return data


# ── DB HELPERS ────────────────────────────────────────────────────────────────
def get_db():
    return psycopg2.connect(DATABASE_URL)

def upsert_clients(conn, clients):
    with conn.cursor() as cur:
        for name, url in clients:
            cur.execute("""
                INSERT INTO clients (name, maps_url)
                VALUES (%s, %s)
                ON CONFLICT (maps_url) DO UPDATE SET name = EXCLUDED.name
            """, (name, url))
    conn.commit()

def get_all_clients(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT id, name, maps_url FROM clients ORDER BY id")
        return cur.fetchall()

def get_last_snapshot(conn, client_id):
    with conn.cursor() as cur:
        cur.execute("""
            SELECT snapshot_data, rating, review_count
            FROM location_snapshots
            WHERE client_id = %s
            ORDER BY snapshot_date DESC
            LIMIT 1
        """, (client_id,))
        row = cur.fetchone()
        if not row:
            return None
        snapshot_data, rating, review_count = row
        if snapshot_data:
            return snapshot_data if isinstance(snapshot_data, dict) else json.loads(snapshot_data)
        # Fallback: old snapshots that only had rating + review_count
        if rating is not None or review_count is not None:
            return {"rating": float(rating) if rating else None, "review_count": review_count}
        return None

def save_snapshot(conn, client_id, today, data):
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO location_snapshots
                (client_id, snapshot_date, rating, review_count, snapshot_data)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (client_id, snapshot_date) DO UPDATE SET
                rating        = EXCLUDED.rating,
                review_count  = EXCLUDED.review_count,
                snapshot_data = EXCLUDED.snapshot_data
        """, (
            client_id,
            today,
            data.get("rating"),
            data.get("review_count"),
            json.dumps(data),
        ))
    conn.commit()

def log_changes(conn, client_id, client_name, changes):
    with conn.cursor() as cur:
        for field, (old_val, new_val) in changes.items():
            cur.execute("""
                INSERT INTO change_log
                    (client_id, client_name, change_type, old_value, new_value)
                VALUES (%s, %s, %s, %s, %s)
            """, (client_id, client_name, field, str(old_val), str(new_val)))
    conn.commit()

def detect_changes(old_data, new_data):
    """Return {field: (old_value, new_value)} for every field that changed."""
    if not old_data:
        return {}  # First run — nothing to compare against

    changes = {}
    for field, new_val in new_data.items():
        if new_val is None:
            continue  # Scraping failed for this field today — skip
        old_val = old_data.get(field)
        if old_val is None:
            continue  # No previous value — skip (don't false-alarm on first full run)

        if field == "rating":
            if float(old_val) != float(new_val):
                changes[field] = (old_val, new_val)
        elif field == "review_count":
            if int(old_val) != int(new_val):
                changes[field] = (old_val, new_val)
        else:
            if str(old_val).strip() != str(new_val).strip():
                changes[field] = (old_val, new_val)

    return changes


# ── TELEGRAM MESSAGE ──────────────────────────────────────────────────────────
def format_telegram_message(today, all_changes, total_clients):
    date_str = today.strftime("%Y-%m-%d")

    if not all_changes:
        return f"✅ GMB Daily Report — {date_str}\nNo changes across {total_clients} locations."

    lines = [f"🔔 GMB Daily Report — {date_str}"]
    lines.append(f"{len(all_changes)} location(s) changed\n")

    for client_name, changes in all_changes.items():
        lines.append(f"📍 <b>{client_name}</b>")
        for field, (old_val, new_val) in changes.items():
            label = FIELD_LABELS.get(field, field)
            if field == "review_count":
                diff = int(new_val) - int(old_val)
                sign = "+" if diff > 0 else ""
                lines.append(f"  {label}: {old_val} → {new_val} ({sign}{diff})")
            elif field == "rating":
                lines.append(f"  {label}: {old_val} → {new_val}")
            else:
                lines.append(f"  {label}:")
                lines.append(f"    Before: {old_val}")
                lines.append(f"    After:  {new_val}")
        lines.append("")

    return "\n".join(lines).strip()


# ── MAIN ──────────────────────────────────────────────────────────────────────
async def main():
    today = date.today()
    print(f"=== GMB Monitor — {today} ===")

    print("Fetching client list from Google Sheet...")
    sheet_clients = fetch_clients_from_sheet()
    print(f"  {len(sheet_clients)} clients in sheet")

    conn = get_db()
    upsert_clients(conn, sheet_clients)
    all_clients = get_all_clients(conn)
    print(f"  {len(all_clients)} clients in DB\n")

    semaphore = asyncio.Semaphore(3)
    all_changes = {}
    lock = asyncio.Lock()

    async def process_client(client_id, name, url):
        async with semaphore:
            print(f"Scraping: {name}")
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                page = await browser.new_page()
                try:
                    data = await scrape_location(page, url)
                finally:
                    await browser.close()

            old_data = get_last_snapshot(conn, client_id)
            changes  = detect_changes(old_data, data)
            save_snapshot(conn, client_id, today, data)

            if changes:
                log_changes(conn, client_id, name, changes)
                async with lock:
                    all_changes[name] = changes
                print(f"  ✓ {len(changes)} change(s): {', '.join(changes.keys())}")
            else:
                scraped = [k for k, v in data.items() if v is not None]
                print(f"  ✓ No changes (scraped: {', '.join(scraped)})")

    tasks = [process_client(cid, name, url) for cid, name, url in all_clients]
    await asyncio.gather(*tasks)

    conn.close()

    msg = format_telegram_message(today, all_changes, len(all_clients))
    print("\n" + msg)
    send_telegram(msg)
    print("\nDone.")


if __name__ == "__main__":
    asyncio.run(main())