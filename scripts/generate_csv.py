#!/usr/bin/env python3
"""
Generate an NSE-security-master-shaped CSV from the fixed template row.

Real mode:
  UPSTOX_ACCESS_TOKEN=... python scripts/generate_csv.py

Mock mode:
  python scripts/generate_csv.py --mock tests/mock_upstox.json
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import secrets
import sys
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "template" / "NSE_CM_security_template.csv"
OUTPUT_DIR = ROOT / "output"
OUTPUT = OUTPUT_DIR / "ipo_security_master.csv"
METADATA = OUTPUT_DIR / "latest.json"

UPSTOX_URL = "https://api.upstox.com/v2/ipos"
STATUSES = ("open", "upcoming")
REQUIRED_REPLACEMENTS = ("FinInstrmId", "TckrSymb", "FinInstrmNm", "ISIN")


def fail(message: str) -> "NoReturn":
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(1)


def load_template() -> tuple[list[str], list[str]]:
    if not TEMPLATE.exists():
        fail(f"Template not found: {TEMPLATE}")

    with TEMPLATE.open("r", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.reader(f))

    if len(rows) < 2:
        fail("Template CSV must contain a header and at least one data row.")

    header, template_row = rows[0], rows[1]

    if len(header) != len(template_row):
        fail(
            f"Template header has {len(header)} columns but row 2 has "
            f"{len(template_row)} columns."
        )

    missing = [c for c in REQUIRED_REPLACEMENTS if c not in header]
    if missing:
        fail(f"Template is missing required columns: {', '.join(missing)}")

    return header, template_row


def fetch_page(token: str, status: str, page: int) -> dict:
    query = urllib.parse.urlencode(
        {"status": status, "page_number": page, "records": 30}
    )
    request = urllib.request.Request(
        f"{UPSTOX_URL}?{query}",
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "IPO-Security-CSV-Generator/1.0",
        },
        method="GET",
    )

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read().decode("utf-8")
            if response.status != 200:
                fail(f"Upstox returned HTTP {response.status}: {body[:500]}")
            return json.loads(body)
    except Exception as exc:
        fail(f"Upstox request failed for status={status}, page={page}: {exc}")


def fetch_all_real(token: str) -> list[dict]:
    if not token.strip():
        fail("UPSTOX_ACCESS_TOKEN is empty.")

    all_ipos: list[dict] = []

    for status in STATUSES:
        page = 1
        total_pages = 1

        while page <= total_pages:
            payload = fetch_page(token, status, page)

            if payload.get("status") not in (None, "success"):
                fail(f"Upstox API reported an error: {payload}")

            records = payload.get("data") or []
            all_ipos.extend(records)

            page_info = ((payload.get("meta_data") or {}).get("page") or {})
            total_pages = int(page_info.get("total_pages") or 1)

            print(
                f"Fetched {len(records)} IPOs: status={status}, "
                f"page={page}/{total_pages}"
            )
            page += 1

    return all_ipos


def fetch_mock(path: Path) -> list[dict]:
    with path.open("r", encoding="utf-8") as f:
        payload = json.load(f)

    # Accept either a list or {open: [...], upcoming: [...]}.
    if isinstance(payload, list):
        return payload

    records: list[dict] = []
    for status in STATUSES:
        records.extend(payload.get(status, []))
    return records


def normalize_and_dedupe(ipos: list[dict]) -> list[dict]:
    """
    Dedupe by Upstox IPO id, with open taking precedence over upcoming.
    If id is absent, use a deterministic fallback key from symbol+isin+name.
    """
    priority = {"open": 0, "upcoming": 1}
    selected: dict[str, dict] = {}

    for ipo in ipos:
        status = str(ipo.get("status") or "").lower()
        if status not in STATUSES:
            continue

        key = str(
            ipo.get("id")
            or f"{ipo.get('symbol','')}|{ipo.get('isin','')}|{ipo.get('name','')}"
        ).strip()

        if not key or key == "||":
            fail(f"IPO record has no usable id/symbol/isin/name: {ipo}")

        existing = selected.get(key)
        if existing is None:
            selected[key] = ipo
        else:
            old_status = str(existing.get("status") or "").lower()
            if priority.get(status, 99) < priority.get(old_status, 99):
                selected[key] = ipo

    return list(selected.values())


def validate_ipo(ipo: dict) -> tuple[str, str, str]:
    symbol = str(ipo.get("symbol") or "").strip()
    name = str(ipo.get("name") or "").strip()
    isin = str(ipo.get("isin") or "").strip()

    missing = []
    if not symbol:
        missing.append("symbol")
    if not name:
        missing.append("name")
    if not isin:
        missing.append("isin")

    if missing:
        fail(
            f"IPO {ipo.get('id', '<unknown>')} is missing required field(s): "
            + ", ".join(missing)
        )

    return symbol, name, isin


def generate_unique_ids(count: int) -> list[str]:
    # 900,000 possible six-digit values. This app needs only one per IPO.
    if count > 900_000:
        fail("Cannot generate enough unique 6-digit IDs.")

    values: set[int] = set()
    while len(values) < count:
        values.add(secrets.randbelow(900_000) + 100_000)

    return [str(v) for v in values]


def generate(ipos: list[dict]) -> tuple[int, list[dict]]:
    header, template_row = load_template()
    ipos = normalize_and_dedupe(ipos)

    if not ipos:
        fail("Upstox returned zero open/upcoming IPOs. No CSV was generated.")

    indices = {name: header.index(name) for name in REQUIRED_REPLACEMENTS}
    ids = generate_unique_ids(len(ipos))

    rows: list[list[str]] = []

    for ipo, instrument_id in zip(ipos, ids):
        symbol, name, isin = validate_ipo(ipo)

        row = template_row.copy()

        # These are the ONLY four fields modified.
        row[indices["FinInstrmId"]] = instrument_id
        row[indices["TckrSymb"]] = symbol
        row[indices["FinInstrmNm"]] = name
        row[indices["ISIN"]] = isin

        rows.append(row)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    with OUTPUT.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f, lineterminator="\n")
        writer.writerow(header)
        writer.writerows(rows)

    metadata = {
        "generated_utc": __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc
        ).isoformat(),
        "ipo_count": len(rows),
        "statuses": ["open", "upcoming"],
        "template": TEMPLATE.name,
        "replaced_columns": list(REQUIRED_REPLACEMENTS),
    }
    with METADATA.open("w", encoding="utf-8") as f:
        json.dump(metadata, f, indent=2)

    return len(rows), ipos


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mock",
        type=Path,
        help="Use a local mock Upstox JSON instead of the real API.",
    )
    args = parser.parse_args()

    if args.mock:
        ipos = fetch_mock(args.mock)
        count, _ = generate(ipos)
    else:
        token = os.environ.get("UPSTOX_ACCESS_TOKEN", "")
        ipos = fetch_all_real(token)
        count, _ = generate(ipos)

    print(f"Generated {OUTPUT} with {count} IPO rows.")


if __name__ == "__main__":
    main()
