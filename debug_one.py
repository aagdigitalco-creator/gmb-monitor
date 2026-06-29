"""
Diagnostic script — scrapes your FIRST client and sends a full debug
report to Telegram showing exactly what Playwright sees on the page.
Run via the "Debug Scrape" GitHub Actions workflow.
"""
import asyncio, os, re, csv, json, requests, psycopg2
from playwright.async_api import async_playwright

TELEGRAM_TOKEN   = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
DATABASE_URL     = os.environ["DATABASE_URL"]
SHEETS_CSV_URL   = os.environ["SHEETS_CSV_URL"]

def send_telegram(text):
    for chunk in [text[i:i+4000] for i in range(0, len(text), 4000)]:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": chunk}
        )

def fetch_first_client():
    resp = requests.get(SHEETS_CSV_URL, timeout=30)
    for row in csv.reader(resp.text.splitlines()):
        if len(row) >= 2 and row[1].strip().startswith("http"):
            return row[0].strip(), row[1].strip()
    return None, None

async def main():
    name, url = fetch_first_client()
    if not url:
        send_telegram("DEBUG ERROR: No clients found in sheet.")
        return

    send_telegram(f"🔍 DEBUG: Scraping — {name}\n{url[:80]}...")

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"]
        )
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 720},
            locale="en-US",
        )
        page = await context.new_page()
        await page.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )

        await page.goto(url, timeout=45000, wait_until="networkidle")
        await page.wait_for_timeout(5000)

        title = await page.title()

        h1_text = None
        el = await page.query_selector("h1")
        if el:
            h1_text = (await el.inner_text()).strip()

        # Collect ALL aria-labels on the page
        all_labels = []
        for el in await page.query_selector_all("[aria-label]"):
            lbl = (await el.get_attribute("aria-label") or "").strip()
            if lbl:
                all_labels.append(lbl)

        # Filter to ones that look relevant
        keywords = ["star", "review", "address", "phone", "website",
                    "open", "close", "hour", "rating", "google"]
        relevant = [l for l in all_labels if any(k in l.lower() for k in keywords)]

        # First 400 chars of page text
        page_text = await page.evaluate("() => document.body.innerText")
        snippet = page_text[:400].replace("\n", " | ")

        await browser.close()

    # Build Telegram message
    lines = [
        f"📄 Page title: {title}",
        f"🏷️ H1 (name): {h1_text or 'NOT FOUND'}",
        f"",
        f"🔎 Relevant aria-labels ({len(relevant)} found):",
    ]
    for lbl in relevant[:25]:
        lines.append(f"  • {lbl}")
    if not relevant:
        lines.append("  ❌ NONE FOUND — page may not have loaded correctly")
    lines += ["", f"📝 Page text snippet:", snippet]

    # DB check — what do we have stored for this client?
    try:
        conn = psycopg2.connect(DATABASE_URL)
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM clients WHERE maps_url = %s", (url,))
            row = cur.fetchone()
            if row:
                client_id = row[0]
                cur.execute("""
                    SELECT snapshot_date, rating, review_count, snapshot_data
                    FROM location_snapshots
                    WHERE client_id = %s
                    ORDER BY snapshot_date DESC LIMIT 3
                """, (client_id,))
                snaps = cur.fetchall()
                lines += ["", f"🗄️ Last {len(snaps)} DB snapshot(s):"]
                for snap_date, rating, review_count, snap_data in snaps:
                    d = {}
                    if snap_data:
                        d = snap_data if isinstance(snap_data, dict) else json.loads(snap_data)
                    lines.append(
                        f"  {snap_date} — rating={rating} reviews={review_count} "
                        f"(stored reviews={d.get('review_count')} phone={d.get('phone')})"
                    )
            else:
                lines.append("  ❌ Client not found in DB")
        conn.close()
    except Exception as e:
        lines.append(f"  DB error: {e}")

    send_telegram("\n".join(lines))
    print("\n".join(lines))

asyncio.run(main())
