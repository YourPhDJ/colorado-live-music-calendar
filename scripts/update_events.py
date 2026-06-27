#!/usr/bin/env python3
"""
update_events.py — Fetches Colorado live music events from the Jambase API
and hot-patches the ALL_EVENTS array inside index.html.

Usage:
    JAMBASE_API_KEY=your_key python scripts/update_events.py

Optional env vars:
    DAYS_AHEAD   How many days forward to fetch (default: 180)
    HTML_PATH    Path to index.html relative to repo root (default: index.html)
"""

import json
import os
import re
import sys
from datetime import date, timedelta

import requests

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
JAMBASE_API_URL = "https://api.data.jambase.com/v3/events"
STATE_CODE = "CO"
DAYS_AHEAD = int(os.environ.get("DAYS_AHEAD", 180))
PER_PAGE = 50
HTML_PATH = os.environ.get("HTML_PATH", "index.html")


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------
def fetch_all_events(api_key: str) -> list[dict]:
    today = date.today()
    date_from = today.strftime("%Y-%m-%d")
    date_to = (today + timedelta(days=DAYS_AHEAD)).strftime("%Y-%m-%d")

    all_events: list[dict] = []
    page = 1
    total_pages = None

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
            "User-Agent": "JamBaseData/1.0",
        }

        try:
            resp = requests.get(JAMBASE_API_URL, params=params, headers=headers, timeout=30)
        except requests.RequestException as exc:
            print(f"  ✗ Network error on page {page}: {exc}", file=sys.stderr)
            sys.exit(1)

        # Always log status so we can see what happened in CI
        print(f"  HTTP {resp.status_code}")

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

        # On the first page, print the response structure so we can
        # verify field names in the CI logs without guessing
        if page == 1:
            print(f"  Top-level response keys: {list(data.keys())}")
            events_sample = data.get("events") or data.get("data") or []
            if events_sample:
                first = events_sample[0]
                print(f"  First event keys: {list(first.keys())}")
                print(f"  First event (truncated): {json.dumps(first, default=str)[:600]}")

        # Support both {"events": [...]} and {"data": [...]} response shapes
        events = data.get("events") or data.get("data") or []

        if page == 1 and not events:
            print("  ✗ No events found in response — check parameter names or API plan", file=sys.stderr)
            print(f"  Full response: {json.dumps(data, default=str)[:1000]}", file=sys.stderr)
            sys.exit(1)

        all_events.extend(events)

        # Support both {"pagination": {...}} and {"meta": {...}} shapes
        pagination = data.get("pagination") or data.get("meta") or {}
        if total_pages is None:
            total_pages = (
                pagination.get("totalPages")
                or pagination.get("total_pages")
                or 1
            )

        print(f"  Page {page}/{total_pages} — {len(all_events)} events so far")

        if page >= total_pages or not events:
            break

        page += 1

    return all_events


# ---------------------------------------------------------------------------
# Map
# ---------------------------------------------------------------------------
def _first_str(lst: list, field: str, fallback: str = "") -> str:
    return lst[0].get(field, fallback) if lst else fallback


def map_event(raw: dict) -> dict:
    """Convert a raw Jambase v3 event object to the ALL_EVENTS schema."""

    # ID: "jambase:event:15106153" → "15106153"
    identifier = raw.get("identifier", "")
    event_id = identifier.rsplit(":", 1)[-1] if identifier else str(raw.get("id", ""))

    # Location
    location = raw.get("location", {})
    address = location.get("address", {})
    geo = location.get("geo", {})

    # Performers
    performers = raw.get("performer", [])
    headliner = _first_str(performers, "name")
    artists_str = " | ".join(p.get("name", "") for p in performers if p.get("name"))

    # Genres
    genres_str = ",".join(
        g.get("name", "").lower().replace(" ", "-")
        for g in raw.get("genre", [])
        if g.get("name")
    )

    # Tickets
    offers = raw.get("offers", [])
    tickets_url = _first_str(offers, "url")

    return {
        "id": event_id,
        "name": raw.get("name", ""),
        "date": raw.get("startDate", ""),
        "venue": location.get("name", ""),
        "city": address.get("addressLocality", ""),
        "lat": geo.get("latitude") or 0,
        "lng": geo.get("longitude") or 0,
        "headliner": headliner,
        "artists": artists_str,
        "genres": genres_str,
        "url": raw.get("url", ""),
        "tickets": tickets_url,
    }


# ---------------------------------------------------------------------------
# Patch HTML
# ---------------------------------------------------------------------------
_ALL_EVENTS_RE = re.compile(
    r"(const ALL_EVENTS\s*=\s*)\[[\s\S]*?\];",
    re.MULTILINE,
)


def patch_html(events: list[dict], html_path: str = HTML_PATH) -> None:
    if not os.path.isfile(html_path):
        print(f"  ✗ {html_path} not found (run from repo root?)", file=sys.stderr)
        sys.exit(1)

    with open(html_path, "r", encoding="utf-8") as fh:
        content = fh.read()

    events_json = json.dumps(events, separators=(",", ":"), ensure_ascii=False)
    replacement = rf"\g<1>{events_json};"

    updated, n_subs = _ALL_EVENTS_RE.subn(replacement, content)

    if n_subs == 0:
        print(
            "  ✗ Could not locate `const ALL_EVENTS = [...]` in the HTML.\n"
            "    Check that the pattern hasn't changed.",
            file=sys.stderr,
        )
        sys.exit(1)

    with open(html_path, "w", encoding="utf-8") as fh:
        fh.write(updated)

    print(f"  ✓ Wrote {len(events)} events to {html_path}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    api_key = os.environ.get("JAMBASE_API_KEY", "").strip()
    if not api_key:
        print("✗ Error: JAMBASE_API_KEY environment variable is not set.", file=sys.stderr)
        sys.exit(1)

    print(f"API key present: {api_key[:8]}…")  # show first 8 chars to confirm it loaded

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

    # 4. Sort chronologically
    events.sort(key=lambda e: (e["date"], e["name"]))

    # 5. Patch HTML
    print(f"\nPatching {HTML_PATH} …")
    patch_html(events)

    print("\nDone ✓")


if __name__ == "__main__":
    main()
