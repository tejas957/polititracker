"""
fetch_votes.py

Strategy: For each bill in our DB, hit Congress.gov's bill actions endpoint.
Actions contain a `recordedVotes` field with direct URLs to the clerk XML
(clerk.house.gov or senate.gov). Fetch each XML and parse individual member votes.

This is the correct approach — the /vote list endpoint is brand new (May 2025)
and only covers House votes. The bill actions approach works for both chambers.

Run from polititrace/backend/:
    python -m app.pipeline.fetch_votes
"""

import requests
import os
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from dotenv import load_dotenv
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from app.models.database import SessionLocal

load_dotenv()

API_KEY = os.getenv("CONGRESS_API_KEY")
BASE_URL = "https://api.congress.gov/v3"
CHUNK_SIZE = 100

# ── Position normalization ────────────────────────────────────────────────────

POSITION_MAP = {
    "Yea": "Yes", "Aye": "Yes",
    "Nay": "No",  "No": "No",
    "Not Voting": "Not Voting",
    "Present": "Present",
    "Paired For": "Paired For",
    "Paired Against": "Paired Against",
    "Announced For": "Announced For",
    "Announced Against": "Announced Against",
}

def normalize(raw: str) -> str:
    return POSITION_MAP.get(raw.strip(), raw.strip())


# ── DB helpers ────────────────────────────────────────────────────────────────

def get_db():
    return SessionLocal()

def execute_with_retry(db, sql, rows, max_attempts=3):
    chunks = [rows[i:i + CHUNK_SIZE] for i in range(0, len(rows), CHUNK_SIZE)]
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
                    print(f"    [ERROR] Skipping chunk: {str(e)[:80]}")
                    db = get_db()
                    break
                wait = 2 ** attempt
                print(f"    [WARN] DB error, retry in {wait}s...")
                time.sleep(wait)
                db = get_db()
    return db


# ── Congress.gov: get recorded vote URLs from a bill's actions ────────────────

def get_recorded_vote_urls(bill_id: str) -> list[str]:
    """
    Parse bill_id like 'HR1234-118' → call /bill/118/hr/1234/actions
    and extract all clerk XML URLs from recordedVotes fields.
    """
    # Parse bill_id
    parts = bill_id.rsplit("-", 1)
    if len(parts) != 2:
        return []
    id_part, congress = parts[0], parts[1]

    # Map prefix → API bill type
    type_map = [
        ("HJRES", "hjres"), ("SJRES", "sjres"),
        ("HCONRES", "hconres"), ("SCONRES", "sconres"),
        ("HRES", "hres"), ("SRES", "sres"),
        ("HR", "hr"), ("S", "s"),
    ]
    bill_type, number = None, None
    for prefix, api_type in type_map:
        if id_part.startswith(prefix):
            bill_type = api_type
            number = id_part[len(prefix):]
            break

    if not bill_type or not number:
        return []

    url = f"{BASE_URL}/bill/{congress}/{bill_type}/{number}/actions"
    urls = []
    offset = 0

    while True:
        try:
            r = requests.get(url, params={"api_key": API_KEY, "limit": 250, "offset": offset}, timeout=20)
            if r.status_code != 200:
                break
            actions = r.json().get("actions", [])
            for action in actions:
                for rv in action.get("recordedVotes", []):
                    clerk_url = rv.get("url")
                    if clerk_url:
                        urls.append(clerk_url)
            if len(actions) < 250:
                break
            offset += 250
        except Exception:
            break

    return urls


# ── Parse clerk XML (works for both House and Senate) ─────────────────────────

def parse_house_xml(xml_text: str) -> list[dict]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    members = []
    for record in root.findall(".//recorded-vote"):
        leg = record.find("legislator")
        vote_el = record.find("vote")
        if leg is None or vote_el is None:
            continue
        bio = leg.get("name-id")
        if bio:
            members.append({"bioguide": bio, "position": normalize(vote_el.text or "")})
    return members


def parse_senate_xml(xml_text: str) -> list[dict]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    members = []
    for member in root.findall(".//member"):
        bio = (member.findtext("bioguide_id") or "").strip()
        if not bio:
            continue
        vote_cast = (member.findtext("vote_cast") or "").strip()
        members.append({"bioguide": bio, "position": normalize(vote_cast)})
    return members


def fetch_and_parse_clerk_xml(clerk_url: str) -> list[dict]:
    try:
        r = requests.get(clerk_url, timeout=20)
        if r.status_code != 200:
            return []
        xml_text = r.text
        # Detect chamber from URL
        if "clerk.house.gov" in clerk_url:
            return parse_house_xml(xml_text)
        elif "senate.gov" in clerk_url:
            return parse_senate_xml(xml_text)
    except Exception:
        pass
    return []


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    db = get_db()

    politician_ids = {row[0] for row in db.execute(text("SELECT id FROM politicians"))}
    print(f"Loaded {len(politician_ids)} politicians.")

    # Only process bills that have actually had floor votes (passed committee etc.)
    # Focus on 118 and 119 to keep it tractable
    bill_rows = db.execute(text("""
        SELECT id FROM bills
        WHERE id LIKE '%-118' OR id LIKE '%-119'
        ORDER BY id
    """)).fetchall()
    bill_ids_to_process = [r[0] for r in bill_rows]
    print(f"Processing {len(bill_ids_to_process)} bills from congress 118 & 119.")

    # Load existing floor vote pairs to skip
    seen_pairs = {
        (r[0], r[1])
        for r in db.execute(text(
            "SELECT politician_id, bill_id FROM votes "
            "WHERE position IN ('Yes','No','Not Voting','Present',"
            "'Paired For','Paired Against','Announced For','Announced Against')"
        ))
    }
    print(f"{len(seen_pairs)} floor vote pairs already in DB.\n")

    votes_to_insert = []
    total_inserted = 0
    bills_with_votes = 0

    for i, bill_id in enumerate(bill_ids_to_process):
        if (i + 1) % 500 == 0:
            print(f"  [{i+1}/{len(bill_ids_to_process)}] bills checked, "
                  f"{bills_with_votes} had floor votes, {total_inserted} votes inserted so far")

        clerk_urls = get_recorded_vote_urls(bill_id)
        if not clerk_urls:
            time.sleep(0.05)
            continue

        bills_with_votes += 1

        for clerk_url in clerk_urls:
            members = fetch_and_parse_clerk_xml(clerk_url)
            for m in members:
                bio = m["bioguide"]
                if not bio or bio not in politician_ids:
                    continue
                pair = (bio, bill_id)
                if pair in seen_pairs:
                    continue
                votes_to_insert.append({
                    "politician_id": bio,
                    "bill_id": bill_id,
                    "position": m["position"],
                    "date": None,  # date is on the bill itself, fine as null
                })
                seen_pairs.add(pair)

            time.sleep(0.1)  # polite to clerk servers

        # Flush every 500
        if len(votes_to_insert) >= 500:
            db = execute_with_retry(db, """
                INSERT INTO votes (politician_id, bill_id, position, date)
                VALUES (:politician_id, :bill_id, :position, :date)
                ON CONFLICT DO NOTHING
            """, votes_to_insert)
            total_inserted += len(votes_to_insert)
            print(f"  Flushed {len(votes_to_insert)} votes (total: {total_inserted})")
            votes_to_insert = []

        time.sleep(0.15)  # polite to Congress.gov

    # Final flush
    if votes_to_insert:
        db = execute_with_retry(db, """
            INSERT INTO votes (politician_id, bill_id, position, date)
            VALUES (:politician_id, :bill_id, :position, :date)
            ON CONFLICT DO NOTHING
        """, votes_to_insert)
        total_inserted += len(votes_to_insert)
        print(f"  Final flush: {len(votes_to_insert)} votes")

    print(f"\nDone. {bills_with_votes} bills had recorded floor votes.")
    print(f"Inserted {total_inserted} new floor vote records.")

    # Summary
    breakdown = db.execute(text("""
        SELECT position, COUNT(*) FROM votes
        GROUP BY position ORDER BY COUNT(*) DESC
    """)).fetchall()
    print("\nVotes by position:")
    for row in breakdown:
        print(f"  {row[0]:<25} {row[1]:>8,}")

    db.close()