import os
import json
import requests
import psycopg2
from datetime import date, datetime
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request

# ── ENV VARS ──────────────────────────────────────────────────────────────────
CLIENT_ID      = os.environ["GOOGLE_CLIENT_ID"]
CLIENT_SECRET  = os.environ["GOOGLE_CLIENT_SECRET"]
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT  = os.environ["TELEGRAM_CHAT_ID"]
DB_URL         = os.environ["DATABASE_URL"]

SCOPES = ["https://www.googleapis.com/auth/business.manage"]

# ── DATABASE ──────────────────────────────────────────────────────────────────
def get_db():
    return psycopg2.connect(DB_URL)

# ── TELEGRAM ──────────────────────────────────────────────────────────────────
def send_telegram(message: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    chunks = [message[i:i+4000] for i in range(0, len(message), 4000)]
    for chunk in chunks:
        requests.post(url, json={"chat_id": TELEGRAM_CHAT, "text": chunk, "parse_mode": "HTML"})

# ── TOKEN REFRESH ─────────────────────────────────────────────────────────────
def refresh_creds(client: dict) -> str:
    creds = Credentials(
        token=client["access_token"],
        refresh_token=client["refresh_token"],
        token_uri="https://oauth2.googleapis.com/token",
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        scopes=SCOPES,
    )
    if creds.expired or not creds.valid:
        creds.refresh(Request())
        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            "UPDATE clients SET access_token=%s, token_expiry=%s WHERE account_id=%s",
            (creds.token, creds.expiry, client["account_id"])
        )
        conn.commit()
        cur.close()
        conn.close()
    return creds.token

# ── GMB API ───────────────────────────────────────────────────────────────────
def get_locations(account_id: str, token: str) -> list:
    headers = {"Authorization": f"Bearer {token}"}
    url = f"https://mybusinessbusinessinformation.googleapis.com/v1/{account_id}/locations"
    params = {"readMask": "name,title,storefrontAddress,websiteUri,regularHours,phoneNumbers,categories"}
    resp = requests.get(url, headers=headers, params=params)
    data = resp.json()
    if "error" in data:
        print(f"Location fetch error for {account_id}: {data['error']}")
        return []
    return data.get("locations", [])

def get_reviews(account_id: str, location_id: str, token: str) -> dict:
    headers = {"Authorization": f"Bearer {token}"}
    url = f"https://mybusiness.googleapis.com/v4/{account_id}/{location_id}/reviews"
    resp = requests.get(url, headers=headers)
    data = resp.json()
    if "error" in data:
        return {"count": 0, "average": 0.0, "reviews": []}
    return {
        "count": data.get("totalReviewCount", 0),
        "average": float(data.get("averageRating", 0)),
        "reviews": data.get("reviews", [])
    }

# ── SNAPSHOT & CHANGE DETECTION ───────────────────────────────────────────────
def get_last_snapshot(account_id, location_id):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT data, review_count, average_rating FROM location_snapshots
        WHERE account_id=%s AND location_id=%s
        ORDER BY snapshot_date DESC LIMIT 1
    """, (account_id, location_id))
    row = cur.fetchone()
    cur.close()
    conn.close()
    return row

def save_snapshot(account_id, location_id, location_name, data, review_data):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO location_snapshots
            (account_id, location_id, location_name, snapshot_date, data, review_count, average_rating)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (account_id, location_id, snapshot_date) DO UPDATE SET
            data=EXCLUDED.data, review_count=EXCLUDED.review_count, average_rating=EXCLUDED.average_rating
    """, (account_id, location_id, location_name, date.today(),
          json.dumps(data), review_data["count"], review_data["average"]))
    conn.commit()
    cur.close()
    conn.close()

def log_change(account_id, location_id, location_name, change):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO change_log (account_id, location_id, location_name, change_type, old_value, new_value)
        VALUES (%s, %s, %s, %s, %s, %s)
    """, (account_id, location_id, location_name, change["type"],
          json.dumps({"value": change.get("old")}), json.dumps({"value": change.get("new")})))
    conn.commit()
    cur.close()
    conn.close()

def detect_changes(account_id, location_id, location_name, today_loc, review_data) -> list:
    row = get_last_snapshot(account_id, location_id)
    if not row:
        return []  # first time, no comparison

    prev_data, prev_count, prev_avg = row
    changes = []

    # Review count
    today_count = review_data["count"]
    if today_count != prev_count:
        diff = today_count - (prev_count or 0)
        new_reviews = review_data["reviews"][:max(diff, 0)]
        changes.append({"type": "review_count", "old": prev_count, "new": today_count,
                        "diff": diff, "new_reviews": new_reviews})

    # Average rating
    today_avg = round(review_data["average"], 1)
    if today_avg != round(float(prev_avg or 0), 1):
        changes.append({"type": "rating_changed", "old": prev_avg, "new": today_avg})

    # Business info fields
    for field in ["title", "websiteUri", "phoneNumbers", "storefrontAddress"]:
        old_val = (prev_data or {}).get(field)
        new_val = today_loc.get(field)
        if json.dumps(old_val, sort_keys=True) != json.dumps(new_val, sort_keys=True):
            changes.append({"type": "info_changed", "field": field, "old": old_val, "new": new_val})

    return changes

# ── FORMAT MESSAGE ────────────────────────────────────────────────────────────
def format_message(location_name, changes) -> str:
    lines = [f"📍 <b>{location_name}</b>"]
    for c in changes:
        if c["type"] == "review_count":
            diff = c["diff"]
            sign = "+" if diff > 0 else ""
            lines.append(f"⭐ Reviews: {c['old']} → <b>{c['new']}</b> ({sign}{diff})")
            for r in c.get("new_reviews", []):
                name    = r.get("reviewer", {}).get("displayName", "Anonymous")
                stars   = r.get("starRating", "?")
                comment = r.get("comment", "(no comment)")[:200]
                lines.append(f"   └ {name} ({stars}★): {comment}")
        elif c["type"] == "rating_changed":
            lines.append(f"📊 Rating: {c['old']} → <b>{c['new']}</b>")
        elif c["type"] == "info_changed":
            labels = {"title": "Business Name", "websiteUri": "Website",
                      "phoneNumbers": "Phone", "storefrontAddress": "Address"}
            label = labels.get(c["field"], c["field"])
            lines.append(f"✏️ {label} changed\n   Old: {c['old']}\n   New: {c['new']}")
    return "\n".join(lines)

# ── MAIN ──────────────────────────────────────────────────────────────────────
def main():
    print(f"[{datetime.now()}] GMB daily check starting...")

    conn = get_db()
    cur  = conn.cursor()
    cur.execute("SELECT name, account_id, access_token, refresh_token, token_expiry FROM clients")
    clients = [{"name": r[0], "account_id": r[1], "access_token": r[2],
                "refresh_token": r[3], "token_expiry": r[4]} for r in cur.fetchall()]
    cur.close()
    conn.close()

    if not clients:
        send_telegram("⚠️ GMB Monitor: No clients found in database.")
        return

    all_changes = []
    total_locations = 0

    for client in clients:
        try:
            token     = refresh_creds(client)
            locations = get_locations(client["account_id"], token)
            total_locations += len(locations)

            for loc in locations:
                location_id   = loc["name"].split("/")[-1]
                location_name = loc.get("title", location_id)
                full_loc_id   = f"locations/{location_id}"

                try:
                    review_data = get_reviews(client["account_id"], full_loc_id, token)
                except Exception as e:
                    print(f"Review error for {location_name}: {e}")
                    review_data = {"count": 0, "average": 0.0, "reviews": []}

                changes = detect_changes(client["account_id"], location_id,
                                         location_name, loc, review_data)
                save_snapshot(client["account_id"], location_id, location_name,
                              loc, review_data)

                if changes:
                    for c in changes:
                        log_change(client["account_id"], location_id, location_name, c)
                    all_changes.append(format_message(location_name, changes))

        except Exception as e:
            msg = f"⚠️ Error on client <b>{client['name']}</b>: {e}"
            print(msg)
            send_telegram(msg)

    if all_changes:
        header = f"🔔 <b>GMB Daily Report — {date.today()}</b>\n{len(all_changes)} location(s) changed\n\n"
        send_telegram(header + "\n\n".join(all_changes))
    else:
        send_telegram(f"✅ <b>GMB Daily Report — {date.today()}</b>\nNo changes across {total_locations} locations.")

    print(f"[{datetime.now()}] Done.")

if __name__ == "__main__":
    main()
