"""
fetch_bills.py

Downloads all bills for congress 118 & 119, inserts them into the bills table,
and records sponsor positions in the votes table.

Run from polititrace/backend/:
    python -m app.pipeline.fetch_bills
"""

import requests
import os
import time
from datetime import datetime
from dotenv import load_dotenv
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from app.models.database import SessionLocal

load_dotenv()

API_KEY = os.getenv("CONGRESS_API_KEY")
BASE_URL = "https://api.congress.gov/v3"

CHUNK_SIZE = 100  # rows per INSERT — safe for Supabase free tier


# ── DB helpers ────────────────────────────────────────────────────────────────

def get_db():
    """Return a fresh session."""
    return SessionLocal()


def execute_with_retry(db, sql, params_list, max_attempts=3):
    """
    Execute a bulk INSERT with automatic reconnect on connection drop.
    Inserts in chunks of CHUNK_SIZE to avoid statement timeouts.
    Returns the db session (possibly a new one if we had to reconnect).
    """
    chunks = [params_list[i:i + CHUNK_SIZE] for i in range(0, len(params_list), CHUNK_SIZE)]

    for chunk in chunks:
        for attempt in range(max_attempts):
            try:
                db.execute(text(sql), chunk)
                db.commit()
                break
            except OperationalError as e:
                db.rollback()
                try:
                    db.close()
                except Exception:
                    pass
                if attempt == max_attempts - 1:
                    print(f"    [ERROR] Gave up after {max_attempts} attempts: {str(e)[:120]}")
                    db = get_db()  # fresh session, skip this chunk
                    break
                wait = 2 ** attempt  # 1s, 2s, 4s
                print(f"    [WARN] DB error, retrying in {wait}s... ({str(e)[:80]})")
                time.sleep(wait)
                db = get_db()

    return db


# ── Congress.gov fetchers ─────────────────────────────────────────────────────

def fetch_all_bills_for_congress(congress: int, bill_type: str) -> list:
    url = f"{BASE_URL}/bill/{congress}/{bill_type}"
    all_bills = []
    params = {"api_key": API_KEY, "limit": 250, "offset": 0}

    while True:
        for attempt in range(3):
            try:
                r = requests.get(url, params=params, timeout=30)
                r.raise_for_status()
                break
            except Exception as e:
                if attempt == 2:
                    raise
                time.sleep(2)

        batch = r.json().get("bills", [])
        all_bills.extend(batch)

        if len(batch) < 250:
            break
        params["offset"] += 250
        time.sleep(0.2)
        print(f"  {bill_type.upper()} {congress}: {len(all_bills)} fetched...", end="\r")

    return all_bills


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    db = get_db()

    # Load valid politician IDs
    politician_ids = {row[0] for row in db.execute(text("SELECT id FROM politicians"))}
    print(f"Loaded {len(politician_ids)} politicians.")

    # Load already-saved bill IDs
    existing_bill_ids = {row[0] for row in db.execute(text("SELECT id FROM bills"))}
    print(f"{len(existing_bill_ids)} bills already in DB.")

    # Load existing vote pairs
    seen_vote_pairs = {
        (r[0], r[1])
        for r in db.execute(text("SELECT politician_id, bill_id FROM votes"))
    }
    print(f"{len(seen_vote_pairs)} vote pairs already in DB.\n")

    seen_bill_ids = set(existing_bill_ids)

    congresses = [118, 119]
    bill_types = ["s", "hr", "sjres", "hjres", "sconres", "hconres"]

    for congress in congresses:
        for bill_type in bill_types:
            print(f"Fetching {bill_type.upper()} bills for congress {congress}...")
            try:
                bills = fetch_all_bills_for_congress(congress, bill_type)
                print(f"  Got {len(bills)} bills")
            except Exception as e:
                print(f"  Error fetching: {e}")
                continue

            bills_to_insert = []
            votes_to_insert = []

            for bill in bills:
                number = bill.get("number")
                if not number:
                    continue

                policy_area = bill.get("policyArea") or {}
                topic = policy_area.get("name")
                intro_date = bill.get("introducedDate")
                bill_id = f"{bill_type.upper()}{number}-{congress}"

                date_val = None
                if intro_date:
                    try:
                        date_val = datetime.strptime(intro_date, "%Y-%m-%d")
                    except Exception:
                        pass

                if bill_id not in seen_bill_ids:
                    bills_to_insert.append({
                        "id": bill_id,
                        "title": (bill.get("title") or "")[:500],
                        "summary": (bill.get("latestAction") or {}).get("text", ""),
                        "topic": topic,
                        "topic_confidence": 1.0 if topic else 0.0,
                        "date": date_val,
                    })
                    seen_bill_ids.add(bill_id)

                for s in bill.get("sponsors", []):
                    bio = s.get("bioguideId")
                    if bio and bio in politician_ids:
                        pair = (bio, bill_id)
                        if pair not in seen_vote_pairs:
                            votes_to_insert.append({
                                "politician_id": bio,
                                "bill_id": bill_id,
                                "position": "Sponsored",
                                "date": date_val,
                            })
                            seen_vote_pairs.add(pair)

            # Insert bills in safe chunks
            if bills_to_insert:
                print(f"  Inserting {len(bills_to_insert)} bills in chunks of {CHUNK_SIZE}...")
                db = execute_with_retry(db, """
                    INSERT INTO bills (id, title, summary, topic, topic_confidence, date)
                    VALUES (:id, :title, :summary, :topic, :topic_confidence, :date)
                    ON CONFLICT (id) DO UPDATE SET
                        topic = COALESCE(bills.topic, EXCLUDED.topic),
                        topic_confidence = GREATEST(bills.topic_confidence, EXCLUDED.topic_confidence)
                """, bills_to_insert)

            # Insert sponsor votes in safe chunks
            if votes_to_insert:
                print(f"  Inserting {len(votes_to_insert)} sponsor votes in chunks of {CHUNK_SIZE}...")
                db = execute_with_retry(db, """
                    INSERT INTO votes (politician_id, bill_id, position, date)
                    VALUES (:politician_id, :bill_id, :position, :date)
                    ON CONFLICT DO NOTHING
                """, votes_to_insert)

            time.sleep(0.5)

    # Final counts
    bill_count = db.execute(text("SELECT COUNT(*) FROM bills")).scalar()
    vote_count = db.execute(text("SELECT COUNT(*) FROM votes")).scalar()
    print(f"\nDone! Bills: {bill_count:,}, Sponsor positions: {vote_count:,}")
    db.close()