import os
import re
import asyncio
import csv
import io
import psycopg2
import requests
from datetime import date, datetime
from playwright.async_api import async_playwright

TELEGRAM_TOKEN  = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT   = os.environ["TELEGRAM_CHAT_ID"]
DB_URL          = os.environ["DATABASE_URL"]
SHEETS_CSV_URL  = os.environ["SHEETS_CSV_URL"]

def get_db():
    return psycopg2.connect(DB_URL)

def send_telegram(message: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    for chunk in [message[i:i+4000] for i in range(0, len(message), 4000)]:
        requests.post(url, json={"chat_id": TELEGRAM_CHAT, "text": chunk, "parse_mode": "HTML"})

def fetch_clients_from_sheet() -> list:
    resp = requests.get(SHEETS_CSV_URL, timeout=30)
    resp.raise_for_status()
    clients = []
    for row in csv.reader(io.StringIO(resp.text)):
        row = [c.strip() for c in row]
        if len(row) >= 2:
            name, maps_url = row[0], row[1]
        elif len(row) == 1:
            name = maps_url = row[0]
        else:
            continue
        # Skip header rows and anything that isn't a real URL
        if not maps_url.startswith("http"):
            continue
        clients.append({"name": name, "maps_url": maps_url})
    return clients

def sync_clients_to_db(clients: list):
    conn = get_db()
    cur = conn.cursor()
    for c in clients:
        cur.execute("""
            INSERT INTO clients (name, maps_url)
            VALUES (%s, %s)
            ON CONFLICT (maps_url) DO UPDATE SET name = EXCLUDED.name
        """, (c["name"], c["maps_url"]))
    conn.commit()
    cur.execute("SELECT id, name, maps_url FROM clients ORDER BY id")
    rows = cur.fetchall()
    cur.close(); conn.close()
    return [{"id": r[0], "name": r[1], "maps_url": r[2]} for r in rows]

async def scrape_location(context, url: str, name: str) -> dict:
    page = await context.new_page()
    try:
        await page.goto(url, wait_until="networkidle", timeout=45000)
        await page.wait_for_timeout(2500)
        rating = None
        review_count = None

        try:
            el = await page.query_selector("div.F7nice")
            if el:
                text = await el.inner_text()
                lines = text.strip().split("\n")
                if lines:
                    try:
                        rating = float(lines[0].strip())
                    except ValueError:
                        pass
                if len(lines) > 1:
                    digits = re.sub(r"[^\d]", "", lines[1])
                    if digits:
                        review_count = int(digits)
        except Exception:
            pass

        if rating is None:
            try:
                for el in await page.query_selector_all("span[aria-label]"):
                    label = (await el.get_attribute("aria-label") or "").lower()
                    if "star" in label:
                        m = re.search(r"(\d+\.?\d*)", label)
                        if m:
                            rating = float(m.group(1))
                            break
            except Exception:
                pass

        if rating is None:
            try:
                title = await page.title()
                m = re.search(r"(\d+\.?\d*)\s*[★⭐]", title)
                if m:
                    rating = float(m.group(1))
            except Exception:
                pass

        return {"rating": rating, "review_count": review_count, "ok": rating is not None}
    except Exception as e:
        print(f"  Scrape error for {name}: {e}")
        return {"rating": None, "review_count": None, "ok": False}
    finally:
        await page.close()

def get_last_snapshot(client_id):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT rating, review_count FROM location_snapshots
        WHERE client_id = %s ORDER BY snapshot_date DESC LIMIT 1
    """, (client_id,))
    row = cur.fetchone()
    cur.close(); conn.close()
    return row

def save_snapshot(client_id, rating, review_count):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO location_snapshots (client_id, snapshot_date, rating, review_count)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (client_id, snapshot_date) DO UPDATE SET
            rating = EXCLUDED.rating, review_count = EXCLUDED.review_count
    """, (client_id, date.today(), rating, review_count))
    conn.commit(); cur.close(); conn.close()

def log_change(client_id, client_name, change_type, old_val, new_val):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO change_log (client_id, client_name, change_type, old_value, new_value)
        VALUES (%s, %s, %s, %s, %s)
    """, (client_id, client_name, change_type, str(old_val), str(new_val)))
    conn.commit(); cur.close(); conn.close()

async def run_all(clients):
    changes = []
    sem = asyncio.Semaphore(3)

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"]
        )
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 800},
        )

        async def process(client):
            async with sem:
                await asyncio.sleep(1)
                print(f"  Checking: {client['name']}")
                data = await scrape_location(context, client["maps_url"], client["name"])
                if not data["ok"]:
                    print(f"  Could not read data for {client['name']}")
                    return

                rating       = data["rating"]
                review_count = data["review_count"]
                last         = get_last_snapshot(client["id"])
                save_snapshot(client["id"], rating, review_count)

                if not last:
                    print(f"  First snapshot saved for {client['name']}")
                    return

                prev_rating, prev_count = last
                msgs = []

                if rating is not None and prev_rating is not None:
                    if round(float(rating), 1) != round(float(prev_rating), 1):
                        msgs.append(f"📊 Rating: {prev_rating} → <b>{rating}</b>")
                        log_change(client["id"], client["name"], "rating_changed", prev_rating, rating)

                if review_count is not None and prev_count is not None:
                    diff = int(review_count) - int(prev_count)
                    if diff != 0:
                        sign = "+" if diff > 0 else ""
                        msgs.append(f"⭐ Reviews: {prev_count} → <b>{review_count}</b> ({sign}{diff})")
                        log_change(client["id"], client["name"], "review_count_changed", prev_count, review_count)

                if msgs:
                    changes.append(f"📍 <b>{client['name']}</b>\n" + "\n".join(msgs))

        await asyncio.gather(*[process(c) for c in clients])
        await browser.close()

    return changes

def main():
    print(f"[{datetime.now()}] GMB Monitor starting...")

    print("Reading client list from Google Sheet...")
    try:
        sheet_clients = fetch_clients_from_sheet()
    except Exception as e:
        send_telegram(f"⚠️ GMB Monitor: Could not read Google Sheet — {e}")
        return

    if not sheet_clients:
        send_telegram("⚠️ GMB Monitor: No clients found in sheet. Check your SHEETS_CSV_URL secret and make sure the sheet has URLs starting with http.")
        return

    print(f"Found {len(sheet_clients)} clients. Syncing to DB...")
    clients = sync_clients_to_db(sheet_clients)

    print(f"Scraping {len(clients)} locations...")
    changes = asyncio.run(run_all(clients))

    today = date.today()
    if changes:
        header = f"🔔 <b>GMB Daily Report — {today}</b>\n{len(changes)} location(s) changed\n\n"
        send_telegram(header + "\n\n".join(changes))
    else:
        send_telegram(f"✅ <b>GMB Daily Report — {today}</b>\nNo changes across {len(clients)} locations.")

    print(f"[{datetime.now()}] Done.")

if __name__ == "__main__":
    main()