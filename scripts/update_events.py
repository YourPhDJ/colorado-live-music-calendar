#!/usr/bin/env python3
"""
update_events.py — Fetches Colorado live music events from the Jambase API
and writes them to events.js (loaded by index.html at runtime).

This script intentionally does NOT modify index.html so that UI changes
committed to the repo are never clobbered by a nightly data refresh.

Usage:
    JAMBASE_API_KEY=your_key python scripts/update_events.py

Optional env vars:
    DAYS_AHEAD    How many days forward to fetch (default: 180)
    EVENTS_PATH   Output file path, relative to repo root (default: events.js)
"""

import json
import os
import sys
import time
from datetime import date, timedelta

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
JAMBASE_API_URL = "https://api.data.jambase.com/v3/events"
STATE_CODE = "CO"
DAYS_AHEAD = int(os.environ.get("DAYS_AHEAD", 180))
PER_PAGE = 50
EVENTS_PATH = os.environ.get("EVENTS_PATH", "events.js")

# Jambase genre slug normalisation
def _slug(name: str) -> str:
    return name.strip().lower().replace(" ", "-")


# ---------------------------------------------------------------------------
# HTTP session with retry on transient errors
# ---------------------------------------------------------------------------
def _make_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=4,
        backoff_factor=2,          # waits 2, 4, 8, 16 s between retries
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------
def fetch_all_events(api_key: str) -> list[dict]:
    today = date.today()
    date_from = today.strftime("%Y-%m-%d")
    date_to = (today + timedelta(days=DAYS_AHEAD)).strftime("%Y-%m-%d")

    all_events: list[dict] = []
    seen_ids: set[str] = set()          # deduplicate across pages
    page = 1
    total_pages: int | None = None
    session = _make_session()

    print(f"Fetching CO events {date_from} → {date_to} …")

    while True:
        params = {
            "geoStateIso": f"US-{STATE_CODE}",
            "eventDateFrom": date_from,
            "eventDateTo": date_to,
            "perPage": PER_PAGE,
            "page": page,
        }
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
            "User-Agent": "ColoradoLiveMusicCalendar/1.0",
        }

        try:
            resp = session.get(JAMBASE_API_URL, params=params, headers=headers, timeout=30)
        except requests.RequestException as exc:
            print(f"  ✗ Network error on page {page}: {exc}", file=sys.stderr)
            sys.exit(1)

        print(f"  HTTP {resp.status_code}")

        if resp.status_code == 429:
            # Retry-After header may tell us how long to wait
            wait = int(resp.headers.get("Retry-After", 60))
            print(f"  Rate limited — waiting {wait}s before retry", file=sys.stderr)
            time.sleep(wait)
            continue

        if not resp.ok:
            print(f"  ✗ API error — response body: {resp.text[:500]}", file=sys.stderr)
            sys.exit(1)

        if not resp.text.strip():
            print("  ✗ API returned empty response body", file=sys.stderr)
            sys.exit(1)

        try:
            data = resp.json()
        except Exception as exc:
            print(f"  ✗ Could not parse JSON: {exc}", file=sys.stderr)
            print(f"  Raw response: {resp.text[:500]}", file=sys.stderr)
            sys.exit(1)

        # On the first page dump structure so CI logs tell us what we're
        # working with — this has been invaluable for debugging field names
        if page == 1:
            print(f"  Top-level response keys: {list(data.keys())}")
            # Use explicit key check, not `or`, to avoid treating [] as falsy
            sample_events = (
                data["events"] if "events" in data
                else data.get("data", [])
            )
            if sample_events:
                first = sample_events[0]
                print(f"  First event keys: {list(first.keys())}")
                # Also log the first performer to verify genre field location
                performers = first.get("performer", [])
                if performers:
                    print(f"  First performer keys: {list(performers[0].keys())}")
                    performer_genres = performers[0].get("genre", [])
                    print(f"  First performer genres: {performer_genres[:3]}")
                print(f"  First event (truncated): {json.dumps(first, default=str)[:600]}")

        # Use explicit key presence check — an empty list [] is falsy,
        # which would incorrectly fall through to the "data" key
        if "events" in data:
            page_events = data["events"]
        elif "data" in data:
            page_events = data["data"]
        else:
            page_events = []

        if page == 1 and not page_events:
            print("  ✗ No events in response — check params or API plan", file=sys.stderr)
            print(f"  Full response: {json.dumps(data, default=str)[:1000]}", file=sys.stderr)
            sys.exit(1)

        # Deduplicate by ID across pages
        new_count = 0
        for ev in page_events:
            ev_id = ev.get("identifier", ev.get("id", ""))
            if ev_id not in seen_ids:
                seen_ids.add(ev_id)
                all_events.append(ev)
                new_count += 1

        # Same explicit key check for pagination
        if "pagination" in data:
            pagination = data["pagination"]
        elif "meta" in data:
            pagination = data["meta"]
        else:
            pagination = {}

        if total_pages is None:
            total_pages = (
                pagination.get("totalPages")
                or pagination.get("total_pages")
                or 1
            )

        print(f"  Page {page}/{total_pages} — {new_count} new events (running total: {len(all_events)})")

        if page >= total_pages or not page_events:
            break

        page += 1

    return all_events


# ---------------------------------------------------------------------------
# Map
# ---------------------------------------------------------------------------
def _extract_genres(raw: dict) -> tuple[str, str]:
    """
    Returns (genres, genres_all).

    Jambase v3 puts genres on performer objects, not on the event itself.
    The event-level `genre` array is checked first as a fallback, but in
    practice it is almost always empty.

    `genres`     — deduplicated genres from ALL performers, in order of
                   first appearance (headliner first).
    `genres_all` — same set, but includes genres only found on supporting
                   acts (superset of `genres` when all performers are the same).
    """
    seen: dict[str, None] = {}  # use dict to preserve insertion order + dedup

    # 1. Event-level genres (usually empty in Jambase v3, but handle it)
    for g in raw.get("genre", []):
        name = (g if isinstance(g, str) else g.get("name", "")).strip()
        if name:
            seen[_slug(name)] = None

    # 2. Each performer's genres (this is where Jambase actually stores them)
    for performer in raw.get("performer", []):
        for g in performer.get("genre", []):
            name = (g if isinstance(g, str) else g.get("name", "")).strip()
            if name:
                seen[_slug(name)] = None

    genres_str = ",".join(seen.keys())
    # genres_all is the same right now; keep it separate in case we later
    # want to split headliner vs support genres
    return genres_str, genres_str


def _best_ticket_url(offers: list[dict]) -> str:
    """
    Prefer an offer explicitly named as a ticket purchase link.
    Fall back to the first offer URL if no better match found.
    """
    ticket_keywords = {"ticket", "tickets", "buy", "purchase", "get tickets"}
    for offer in offers:
        name = offer.get("name", "").lower()
        if any(kw in name for kw in ticket_keywords):
            return offer.get("url", "")
    # Fall back to first offer
    return offers[0].get("url", "") if offers else ""


def map_event(raw: dict) -> dict:
    """Convert a raw Jambase v3 event object to the ALL_EVENTS schema."""

    # ID: "jambase:event:15106153" → "15106153"
    identifier = raw.get("identifier", "")
    event_id = identifier.rsplit(":", 1)[-1] if identifier else str(raw.get("id", ""))

    # Location — use explicit .get with defaults, not `or`, for numeric fields
    location = raw.get("location", {})
    address = location.get("address", {})
    geo = location.get("geo", {})

    # Performers
    performers = raw.get("performer", [])
    headliner = performers[0].get("name", "") if performers else ""
    artists_str = " | ".join(p.get("name", "") for p in performers if p.get("name"))

    # Genres (see _extract_genres docstring)
    genres_str, genres_all_str = _extract_genres(raw)

    # Tickets — pick the most relevant offer URL
    tickets_url = _best_ticket_url(raw.get("offers", []))

    return {
        "id": event_id,
        "name": raw.get("name", ""),
        "date": raw.get("startDate", ""),
        "venue": location.get("name", ""),
        "city": address.get("addressLocality", ""),
        "lat": geo.get("latitude", 0),    # explicit default, not `or 0`
        "lng": geo.get("longitude", 0),
        "headliner": headliner,
        "artists": artists_str,
        "genres": genres_str,
        "genres_all": genres_all_str,
        "url": raw.get("url", ""),
        "tickets": tickets_url,
    }


# ---------------------------------------------------------------------------
# Write events.js
# ---------------------------------------------------------------------------
def write_events_js(events: list[dict], path: str = EVENTS_PATH) -> None:
    """
    Write events to a standalone JS file that index.html loads at runtime.
    This keeps UI code and data completely separate — nightly runs never
    touch index.html.
    """
    events_json = json.dumps(events, separators=(",", ":"), ensure_ascii=False)
    today_str = date.today().isoformat()
    content = (
        f"// Auto-generated by scripts/update_events.py — do not edit manually.\n"
        f"// Last updated: {today_str}\n"
        f"const ALL_EVENTS = {events_json};\n"
    )
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)
    print(f"  ✓ Wrote {len(events)} events to {path} ({len(content):,} bytes)")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    api_key = os.environ.get("JAMBASE_API_KEY", "").strip()
    if not api_key:
        print("✗ JAMBASE_API_KEY is not set.", file=sys.stderr)
        sys.exit(1)

    print(f"API key present: {api_key[:8]}…")

    # 1. Fetch
    raw_events = fetch_all_events(api_key)
    print(f"\nTotal raw events fetched: {len(raw_events)}")

    # 2. Drop cancelled/postponed
    raw_events = [
        e for e in raw_events
        if e.get("eventStatus") not in ("EventCancelled", "EventPostponed")
    ]
    print(f"After filtering cancelled/postponed: {len(raw_events)}")

    # 3. Map to schema
    events = [map_event(e) for e in raw_events]

    # 4. Log genre coverage — if this shows 0 we know the fix didn't work
    with_genres = sum(1 for e in events if e["genres"])
    print(f"Events with genres: {with_genres}/{len(events)} ({100*with_genres//max(len(events),1)}%)")
    if with_genres == 0:
        print(
            "  ⚠ Warning: no genres found. Check that Jambase is returning\n"
            "    `genre` arrays on performer objects (inspect 'First performer genres' above).",
            file=sys.stderr,
        )

    # 5. Sort chronologically
    events.sort(key=lambda e: (e["date"], e["name"]))

    # 6. Write events.js
    print(f"\nWriting {EVENTS_PATH} …")
    write_events_js(events)

    print("\nDone ✓")


if __name__ == "__main__":
    main()
