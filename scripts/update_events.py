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

JAMBASE_API_URL = "https://data.jambase.com/api/v3/events"
STATE_CODE = "CO"
DAYS_AHEAD = int(os.environ.get("DAYS_AHEAD", 180))
PER_PAGE = 50
HTML_PATH = os.environ.get("HTML_PATH", "index.html")


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
            "stateCode": STATE_CODE,
            "dateFrom": date_from,
            "dateTo": date_to,
            "perPage": PER_PAGE,
            "page": page,
        }
        headers = {"x-api-key": api_key}

        try:
            resp = requests.get(JAMBASE_API_URL, params=params, headers=headers, timeout=30)
            if not resp.ok:
                print(f"  ✗ HTTP {resp.status_code} on page {page}", file=sys.stderr)
                print(f"  Response body: {resp.text[:500]}", file=sys.stderr)
                resp.raise_for_status()
        except requests.RequestException as exc:
            print(f"  ✗ Request failed on page {page}: {exc}", file=sys.stderr)
            sys.exit(1)

        data = resp.json()

        if not data.get("success", False):
            print(f"  ✗ API returned success=false: {data}", file=sys.stderr)
            sys.exit(1)

        events = data.get("events", [])
        all_events.extend(events)

        pagination = data.get("pagination", {})
        if total_pages is None:
            total_pages = pagination.get("totalPages", 1)

        print(f"  Page {page}/{total_pages} — {len(all_events)} events so far")

        if page >= total_pages or not events:
            break

        page += 1

    return all_events


def _first_str(lst: list, field: str, fallback: str = "") -> str:
    return lst[0].get(field, fallback) if lst else fallback


def map_event(raw: dict) -> dict:
    identifier = raw.get("identifier", "")
    event_id = identifier.rsplit(":", 1)[-1] if identifier else raw.get("id", "")

    location = raw.get("location", {})
    address = location.get("address", {})
    geo = location.get("geo", {})

    performers = raw.get("performer", [])
    headliner = _first_str(performers, "name")
    artists_str = " | ".join(p.get("name", "") for p in performers if p.get("name"))

    genres_str = ",".join(
        g.get("name", "").lower().replace(" ", "-")
        for g in raw.get("genre", [])
        if g.get("name")
    )

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
        print("  ✗ Could not locate `const ALL_EVENTS = [...]` in the HTML.", file=sys.stderr)
        sys.exit(1)

    with open(html_path, "w", encoding="utf-8") as fh:
        fh.write(updated)

    print(f"  ✓ Wrote {len(events)} events to {html_path}")


def main() -> None:
    api_key = os.environ.get("JAMBASE_API_KEY", "").strip()
    if not api_key:
        print("Error: JAMBASE_API_KEY environment variable is not set.", file=sys.stderr)
        sys.exit(1)

    raw_events = fetch_all_events(api_key)
    print(f"\nTotal raw events: {len(raw_events)}")

    raw_events = [
        e for e in raw_events
        if e.get("eventStatus") not in ("EventCancelled", "EventPostponed")
    ]
    print(f"After filtering cancelled/postponed: {len(raw_events)}")

    events = [map_event(e) for e in raw_events]
    events.sort(key=lambda e: (e["date"], e["name"]))

    print(f"\nPatching {HTML_PATH} …")
    patch_html(events)

    print("\nDone ✓")


if __name__ == "__main__":
    main()
