import requests
import os
import time
from dotenv import load_dotenv
from sqlalchemy.orm import Session
from app.models.database import SessionLocal, Politician

load_dotenv()

API_KEY = os.getenv("CONGRESS_API_KEY")
BASE_URL = "https://api.congress.gov/v3"

def fetch_current_members(chamber: str):
    """Fetch all current members with pagination."""
    url = f"{BASE_URL}/member"
    all_members = []
    offset = 0

    while True:
        params = {
            "api_key": API_KEY,
            "currentMember": "true",
            "limit": 250,
            "offset": offset
        }

        for attempt in range(3):
            try:
                response = requests.get(url, params=params, timeout=30)
                response.raise_for_status()
                break
            except (requests.exceptions.ChunkedEncodingError,
                    requests.exceptions.ConnectionError) as e:
                print(f"  Network error (attempt {attempt+1}/3): {e}")
                if attempt == 2:
                    raise
                time.sleep(3)

        data = response.json()
        batch = data.get("members", [])

        # Filter by chamber manually — API chamber filter is unreliable
        filtered = [
            m for m in batch
            if any(
                term.get("chamber", "").lower() == chamber.lower()
                for term in m.get("terms", {}).get("item", [])
            )
        ]

        all_members.extend(filtered)
        print(f"  Offset {offset}: got {len(batch)} total, {len(filtered)} {chamber} members (running total: {len(all_members)})")

        if len(batch) < 250:
            break
        offset += 250
        time.sleep(0.5)

    return all_members

def save_members(members: list, chamber: str, db: Session):
    saved = 0
    skipped = 0
    for m in members:
        bio = m.get("bioguideId")
        if not bio:
            continue
        existing = db.query(Politician).filter(Politician.id == bio).first()
        if existing:
            # Update chamber if they moved from house to senate
            existing.chamber = chamber
            existing.party = m.get("partyName")
            skipped += 1
        else:
            politician = Politician(
                id=bio,
                name=m.get("name", ""),
                party=m.get("partyName"),
                state=m.get("state"),
                chamber=chamber,
                twitter_handle=None
            )
            db.add(politician)
            saved += 1

    db.commit()
    print(f"  Saved {saved} new, updated {skipped} existing {chamber} members.")

if __name__ == "__main__":
    db = SessionLocal()
    print("Fetching House members...")
    house = fetch_current_members("House of Representatives")
    print("Fetching Senate members...")
    senate = fetch_current_members("Senate")
    save_members(house, "house", db)
    save_members(senate, "senate", db)
    db.close()
    print("Done.")