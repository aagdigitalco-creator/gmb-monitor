# add_clients.py
# Run once to import your client list into Supabase.
#
# HOW TO USE:
#   1. Open your Google Sheet
#   2. File → Download → Comma Separated Values (.csv)
#   3. Put that CSV file in C:\gmb-monitor\
#   4. In Git Bash:  python add_clients.py your_file.csv
#
# CSV format — two columns, no header row needed:
#   Business Name, https://www.google.com/maps/place/...
#
# If your sheet only has URLs (no name column), that also works.

import sys
import csv
import os
import psycopg2

DB_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://postgres.utgdxyntajkeibmwhglb:aag_digital_co@aws-1-ap-southeast-2.pooler.supabase.com:6543/postgres"
)

def main():
    if len(sys.argv) < 2:
        print("Usage: python add_clients.py your_clients.csv")
        sys.exit(1)

    csv_file = sys.argv[1]
    if not os.path.exists(csv_file):
        print(f"File not found: {csv_file}")
        sys.exit(1)

    conn = psycopg2.connect(DB_URL)
    cur = conn.cursor()
    added = skipped = errors = 0

    with open(csv_file, "r", encoding="utf-8-sig") as f:
        for row in csv.reader(f):
            row = [c.strip() for c in row if c.strip()]
            if not row:
                continue

            # Skip header rows
            if row[0].lower() in ("name", "business name", "client", "client name"):
                continue

            if len(row) >= 2:
                name, maps_url = row[0], row[1]
            else:
                name = maps_url = row[0]

            if not maps_url.startswith("http"):
                print(f"  Skipping (not a URL): {row}")
                continue

            try:
                cur.execute(
                    "INSERT INTO clients (name, maps_url) VALUES (%s, %s) ON CONFLICT (maps_url) DO NOTHING",
                    (name, maps_url)
                )
                if cur.rowcount:
                    added += 1
                    print(f"  ✓ {name}")
                else:
                    skipped += 1
                    print(f"  - Already exists: {name}")
            except Exception as e:
                errors += 1
                print(f"  ✗ Error on {name}: {e}")

    conn.commit()
    cur.close()
    conn.close()
    print(f"\nDone — Added: {added}  |  Skipped: {skipped}  |  Errors: {errors}")

if __name__ == "__main__":
    main()
