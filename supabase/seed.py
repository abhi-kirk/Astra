"""
One-time seed script: imports portfolio_history.csv and convictions.json into Supabase.
Run once after schema is created:
    python -m supabase.seed
"""

import json
import math
from pathlib import Path

import pandas as pd
from src.db import get_client

ROOT = Path(__file__).parent.parent


def seed_trades():
    csv_path = ROOT / "data" / "portfolio_history.csv"
    if not csv_path.exists():
        print("portfolio_history.csv not found — skipping trades seed")
        return

    df = pd.read_csv(csv_path, engine="python", quotechar='"', on_bad_lines="skip")
    df.columns = [c.strip() for c in df.columns]

    def clean_dollar(val):
        if pd.isna(val):
            return None
        v = str(val).replace("$","").replace(",","").replace("(","").replace(")","").strip()
        try:
            return float(v)
        except ValueError:
            return None

    def safe_val(v):
        if v is None:
            return None
        try:
            if math.isnan(float(v)):
                return None
        except (TypeError, ValueError):
            pass
        return v

    rows = []
    for _, r in df.iterrows():
        rows.append({
            "activity_date": str(r["Activity Date"]).strip() if pd.notna(r.get("Activity Date")) else None,
            "process_date":  str(r["Process Date"]).strip() if pd.notna(r.get("Process Date")) else None,
            "settle_date":   str(r["Settle Date"]).strip() if pd.notna(r.get("Settle Date")) else None,
            "ticker":        str(r["Instrument"]).strip() if pd.notna(r.get("Instrument")) else None,
            "description":   str(r["Description"]).strip() if pd.notna(r.get("Description")) else None,
            "trans_code":    str(r["Trans Code"]).strip() if pd.notna(r.get("Trans Code")) else None,
            "quantity":      safe_val(pd.to_numeric(r.get("Quantity"), errors="coerce")),
            "price":         safe_val(clean_dollar(r.get("Price"))),
            "amount":        safe_val(clean_dollar(r.get("Amount"))),
        })

    # Insert in batches of 200
    db = get_client()
    batch_size = 200
    total = 0
    for i in range(0, len(rows), batch_size):
        batch = rows[i:i+batch_size]
        db.table("trades").insert(batch).execute()
        total += len(batch)
        print(f"  Inserted trades {i+1}–{total}")

    print(f"Seeded {total} trade rows.")


def seed_convictions():
    conv_path = ROOT / "convictions.json"
    if not conv_path.exists():
        print("convictions.json not found — skipping")
        return

    with open(conv_path) as f:
        content = json.load(f)

    db = get_client()
    existing = db.table("convictions").select("id").limit(1).execute()
    if existing.data:
        db.table("convictions").update({"content": content}).eq("id", existing.data[0]["id"]).execute()
        print("Updated existing convictions row.")
    else:
        db.table("convictions").insert({"content": content}).execute()
        print("Inserted convictions.")


if __name__ == "__main__":
    print("Seeding trades...")
    seed_trades()
    print("\nSeeding convictions...")
    seed_convictions()
    print("\nDone. You can now delete the local CSV and convictions.json if desired.")
    print("(They remain available locally as fallback but are no longer needed in git.)")
