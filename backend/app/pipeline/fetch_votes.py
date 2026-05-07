import requests
import os
import time
from datetime import datetime
from dotenv import load_dotenv
from sqlalchemy.orm import Session
from app.models.database import SessionLocal, Politician, Bill, Vote

load_dotenv()

API_KEY = os.getenv("CONGRESS_API_KEY")
BASE_URL = "https://api.congress.gov/v3"

def fetch_sponsored(bioguide: str, endpoint: str):
    """Fetch sponsored or cosponsored legislation for a member."""
    url = f"{BASE_URL}/member/{bioguide}/{endpoint}"
    results = []
    params = {"api_key": API_KEY, "limit": 250, "offset": 0}

    while True:
        for attempt in range(3):
            try:
                r = requests.get(url, params=params, timeout=30)
                r.raise_for_status()
                break
            except Exception as e:
                if attempt == 2:
                    return results
                time.sleep(2)

        data = r.json()
        key = "sponsoredLegislation" if "sponsored" in endpoint and "co" not in endpoint else "cosponsoredLegislation"
        batch = data.get(key, [])
        results.extend(batch)

        if len(batch) < 250:
            break
        params["offset"] += 250
        time.sleep(0.3)

    return results

def save_legislation(bioguide: str, items: list, position_type: str, db: Session):
    """Save bills and record member's sponsorship as a position."""
    saved = 0
    for item in items:
        congress = item.get("congress")
        number = item.get("number")
        bill_type = item.get("type", "")
        policy_area = item.get("policyArea", {})
        topic = policy_area.get("name") if policy_area else None

        if not number or not congress:
            continue

        bill_id = f"{bill_type}{number}-{congress}"

        # Save bill
        bill = db.query(Bill).filter(Bill.id == bill_id).first()
        if not bill:
            bill = Bill(
                id=bill_id,
                title=item.get("title", "")[:500],
                summary=item.get("latestAction", {}).get("text", ""),
                topic=topic,
                topic_confidence=1.0 if topic else 0.0,  # congress-assigned = high confidence
                date=datetime.strptime(
                    item["introducedDate"], "%Y-%m-%d"
                ) if item.get("introducedDate") else None
            )
            db.add(bill)
        elif topic and not bill.topic:
            bill.topic = topic
            bill.topic_confidence = 1.0

        # Record sponsorship as a FOR vote
        existing = db.query(Vote).filter(
            Vote.politician_id == bioguide,
            Vote.bill_id == bill_id
        ).first()

        if not existing:
            vote = Vote(
                politician_id=bioguide,
                bill_id=bill_id,
                position="Yes" if position_type == "sponsored" else "Cosponsored",
                date=datetime.strptime(
                    item["introducedDate"], "%Y-%m-%d"
                ) if item.get("introducedDate") else None
            )
            db.add(vote)
            saved += 1

    db.commit()
    return saved

if __name__ == "__main__":
    db = SessionLocal()
    politicians = db.query(Politician).all()
    print(f"Loaded {len(politicians)} politicians.")

    total_saved = 0

    for i, p in enumerate(politicians):
        try:
            # Fetch sponsored legislation
            sponsored = fetch_sponsored(p.id, "sponsored-legislation")
            s_saved = save_legislation(p.id, sponsored, "sponsored", db)

            # Fetch cosponsored legislation
            cosponsored = fetch_sponsored(p.id, "cosponsored-legislation")
            c_saved = save_legislation(p.id, cosponsored, "cosponsored", db)

            total_saved += s_saved + c_saved

            if (i + 1) % 10 == 0 or s_saved + c_saved > 0:
                print(f"[{i+1}/{len(politicians)}] {p.name}: +{s_saved} sponsored, +{c_saved} cosponsored (total: {total_saved})")

            time.sleep(0.5)

        except Exception as e:
            print(f"  Error for {p.name}: {e}")
            continue

    db.close()
    print(f"\nDone. Total positions saved: {total_saved}")