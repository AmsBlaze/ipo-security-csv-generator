#!/usr/bin/env python3
"""Generate the latest NSE CM security-master CSV from Upstox IPO data.

Rules:
- Fetch only `open` and `upcoming` IPOs.
- If an IPO is missing symbol/ISIN/name, try the Upstox IPO details endpoint.
- A record is considered complete only when symbol, ISIN, and an NSE equity
  instrument name can be resolved.
- Incomplete records are skipped and counted; they never stop the run.
- `FinInstrmNm` comes from Upstox Instrument Search (NSE equity name), not
  from the IPO marketing name.
- Only four columns in the fixed template are changed:
    FinInstrmId, TckrSymb, FinInstrmNm, ISIN
- Output filename is NSE_CM_security_DDMMYYYY.csv using Asia/Kolkata date.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import secrets
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import NoReturn
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "template" / "NSE_CM_security_template.csv"
OUTPUT_DIR = ROOT / "output"
METADATA = OUTPUT_DIR / "latest.json"
UPSTOX_IPO_URL = "https://api.upstox.com/v2/ipos"
UPSTOX_INSTRUMENT_SEARCH_URL = "https://api.upstox.com/v2/instruments/search"
STATUSES = ("open", "upcoming")
REQUIRED_REPLACEMENTS = ("FinInstrmId", "TckrSymb", "FinInstrmNm", "ISIN")
REQUIRED_IPO_FIELDS = ("symbol", "name", "isin")
IST = ZoneInfo("Asia/Kolkata")


def fail(message: str) -> NoReturn:
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(1)


def current_output_path() -> Path:
    date_text = datetime.now(IST).strftime("%d%m%Y")
    return OUTPUT_DIR / f"NSE_CM_security_{date_text}.csv"


def load_template() -> tuple[list[str], list[str]]:
    if not TEMPLATE.exists():
        fail(f"Template not found: {TEMPLATE}")

    with TEMPLATE.open("r", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.reader(f))

    if len(rows) < 2:
        fail("Template CSV must contain a header and row 2.")

    header, template_row = rows[0], rows[1]
    if len(header) != len(template_row):
        fail(f"Template has {len(header)} columns but row 2 has {len(template_row)}.")

    missing = [c for c in REQUIRED_REPLACEMENTS if c not in header]
    if missing:
        fail(f"Template is missing required columns: {', '.join(missing)}")

    return header, template_row


def make_request(url: str, token: str) -> urllib.request.Request:
    return urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "IPO-Security-CSV-Generator/2.0",
        },
        method="GET",
    )


def get_json(url: str, token: str, context: str) -> dict:
    request = make_request(url, token)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read().decode("utf-8")
            if response.status != 200:
                raise RuntimeError(f"HTTP {response.status}: {body[:500]}")
            payload = json.loads(body)
            if not isinstance(payload, dict):
                raise RuntimeError("Unexpected JSON response.")
            return payload
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {body[:500]}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(str(exc.reason)) from exc
    except Exception as exc:
        raise RuntimeError(str(exc)) from exc


def fetch_page(token: str, status: str, page: int) -> dict:
    query = urllib.parse.urlencode({"status": status, "page_number": page, "records": 30})
    return get_json(f"{UPSTOX_IPO_URL}?{query}", token, f"IPO listing {status} page {page}")


def fetch_ipo_details(token: str, ipo_id: str) -> dict:
    encoded = urllib.parse.quote(ipo_id, safe="")
    try:
        payload = get_json(f"{UPSTOX_IPO_URL}/{encoded}", token, f"IPO details {ipo_id}")
        if payload.get("status") not in (None, "success"):
            return {}
        return payload.get("data") if isinstance(payload.get("data"), dict) else {}
    except Exception as exc:
        print(f"WARNING: Details lookup failed for {ipo_id}: {exc}")
        return {}


def fetch_instrument_name_by_isin(token: str, isin: str, symbol: str) -> str | None:
    """Resolve the actual NSE equity security name, not the IPO title."""
    query = urllib.parse.urlencode(
        {
            "query": isin,
            "exchanges": "NSE",
            "segments": "EQ",
            "page_number": 1,
            "records": 30,
        }
    )

    try:
        payload = get_json(
            f"{UPSTOX_INSTRUMENT_SEARCH_URL}?{query}",
            token,
            f"instrument search {isin}",
        )
    except Exception as exc:
        print(f"WARNING: Instrument lookup failed for {symbol}/{isin}: {exc}")
        return None

    for item in payload.get("data") or []:
        if not isinstance(item, dict):
            continue
        item_isin = str(item.get("isin") or "").strip()
        segment = str(item.get("segment") or "").strip()
        exchange = str(item.get("exchange") or "").strip()
        trading_symbol = str(item.get("trading_symbol") or "").strip()
        name = str(item.get("name") or "").strip()

        if item_isin != isin or segment != "NSE_EQ" or exchange != "NSE" or not name:
            continue

        # Prefer the exact ticker match, but accept the same ISIN/NSE_EQ security
        # if Upstox returns a minor symbol variant.
        if trading_symbol == symbol:
            return name

    # Second pass: same ISIN + NSE_EQ, even if ticker formatting differs.
    for item in payload.get("data") or []:
        if not isinstance(item, dict):
            continue
        if (
            str(item.get("isin") or "").strip() == isin
            and str(item.get("segment") or "").strip() == "NSE_EQ"
            and str(item.get("exchange") or "").strip() == "NSE"
            and str(item.get("name") or "").strip()
        ):
            return str(item["name"]).strip()

    return None


def missing_fields(ipo: dict) -> list[str]:
    return [field for field in REQUIRED_IPO_FIELDS if not str(ipo.get(field) or "").strip()]


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
            print(f"Fetched {len(records)} IPOs: status={status}, page={page}/{total_pages}")
            page += 1
    return all_ipos


def fetch_mock(path: Path) -> list[dict]:
    with path.open("r", encoding="utf-8") as f:
        payload = json.load(f)
    if isinstance(payload, list):
        return payload
    records: list[dict] = []
    for status in STATUSES:
        records.extend(payload.get(status, []))
    return records


def normalize_and_dedupe(ipos: list[dict]) -> list[dict]:
    priority = {"open": 0, "upcoming": 1}
    selected: dict[str, dict] = {}
    for ipo in ipos:
        status = str(ipo.get("status") or "").lower()
        if status not in STATUSES:
            continue
        key = str(ipo.get("id") or f"{ipo.get('symbol','')}|{ipo.get('isin','')}|{ipo.get('name','')}").strip()
        if not key or key == "||":
            continue
        existing = selected.get(key)
        if existing is None:
            selected[key] = ipo
        else:
            old_status = str(existing.get("status") or "").lower()
            if priority.get(status, 99) < priority.get(old_status, 99):
                selected[key] = ipo
    return list(selected.values())


def enrich_and_filter(token: str, ipos: list[dict]) -> tuple[list[dict], int]:
    """Return only records that have enough data to create a valid NSE row."""
    valid: list[dict] = []
    skipped = 0
    details_cache: dict[str, dict] = {}
    instrument_cache: dict[str, str | None] = {}

    for ipo in ipos:
        item = dict(ipo)
        missing = missing_fields(item)
        if missing:
            ipo_id = str(item.get("id") or "").strip()
            if ipo_id:
                if ipo_id not in details_cache:
                    print(f"Fetching details for incomplete IPO: {ipo_id}")
                    details_cache[ipo_id] = fetch_ipo_details(token, ipo_id)
                details = details_cache[ipo_id]
                for field in REQUIRED_IPO_FIELDS:
                    if not str(item.get(field) or "").strip() and str(details.get(field) or "").strip():
                        item[field] = details[field]

        missing = missing_fields(item)
        if missing:
            skipped += 1
            print(f"FLAGGED / SKIPPED: {item.get('name') or item.get('id') or '<unknown>'} missing {', '.join(missing)}")
            continue

        symbol = str(item["symbol"]).strip()
        isin = str(item["isin"]).strip()
        if isin not in instrument_cache:
            instrument_cache[isin] = fetch_instrument_name_by_isin(token, isin, symbol)
        instrument_name = instrument_cache[isin]

        if not instrument_name:
            skipped += 1
            print(f"FLAGGED / SKIPPED: {item.get('name') or item.get('id') or '<unknown>'} has no resolvable NSE_EQ instrument name for ISIN {isin}")
            continue

        item["instrument_name"] = instrument_name
        valid.append(item)

    return valid, skipped


def generate_unique_ids(count: int) -> list[str]:
    if count > 900_000:
        fail("Cannot generate enough unique 6-digit IDs.")
    values: set[int] = set()
    while len(values) < count:
        values.add(secrets.randbelow(900_000) + 100_000)
    return [str(v) for v in values]


def remove_old_generated_csvs(current: Path) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    for path in OUTPUT_DIR.glob("NSE_CM_security_*.csv"):
        if path != current:
            path.unlink(missing_ok=True)


def generate(ipos: list[dict], token: str | None = None) -> tuple[int, int, Path]:
    header, template_row = load_template()
    ipos = normalize_and_dedupe(ipos)

    if token:
        valid_ipos, skipped_count = enrich_and_filter(token, ipos)
    else:
        # Mock/test mode: use IPO name as a stand-in only for tests.
        valid_ipos = []
        skipped_count = 0
        for ipo in ipos:
            if missing_fields(ipo):
                skipped_count += 1
                continue
            item = dict(ipo)
            item["instrument_name"] = str(item.get("mock_instrument_name") or item["name"]).strip()
            valid_ipos.append(item)

    if not valid_ipos:
        fail("No complete IPOs are available for CSV generation.")

    output = current_output_path()
    remove_old_generated_csvs(output)

    indices = {name: header.index(name) for name in REQUIRED_REPLACEMENTS}
    ids = generate_unique_ids(len(valid_ipos))
    rows: list[list[str]] = []

    complete_ipos_for_site: list[dict] = []
    for ipo, instrument_id in zip(valid_ipos, ids):
        symbol = str(ipo["symbol"]).strip()
        ipo_name = str(ipo["name"]).strip()
        isin = str(ipo["isin"]).strip()
        instrument_name = str(ipo["instrument_name"]).strip()

        row = template_row.copy()
        row[indices["FinInstrmId"]] = instrument_id
        row[indices["TckrSymb"]] = symbol
        row[indices["FinInstrmNm"]] = instrument_name
        row[indices["ISIN"]] = isin
        rows.append(row)

        complete_ipos_for_site.append(
            {
                "id": str(ipo.get("id") or ""),
                "ipo_name": ipo_name,
                "status": str(ipo.get("status") or "").lower(),
                "symbol": symbol,
                "instrument_name": instrument_name,
                "isin": isin,
            }
        )

    with output.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f, lineterminator="\n")
        writer.writerow(header)
        writer.writerows(rows)

    generated_ist = datetime.now(IST).isoformat()
    metadata = {
        "generated_at_ist": generated_ist,
        "generated_date_ddmmyyyy": datetime.now(IST).strftime("%d%m%Y"),
        "csv_filename": output.name,
        "ipo_count": len(rows),
        "skipped_count": skipped_count,
        "statuses": ["open", "upcoming"],
        "template": TEMPLATE.name,
        "replaced_columns": list(REQUIRED_REPLACEMENTS),
        "complete_ipos": complete_ipos_for_site,
    }

    with METADATA.open("w", encoding="utf-8") as f:
        json.dump(metadata, f, indent=2)

    return len(rows), skipped_count, output


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mock", type=Path, help="Use local mock data instead of the real Upstox API.")
    args = parser.parse_args()

    if args.mock:
        ipos = fetch_mock(args.mock)
        count, skipped, output = generate(ipos)
    else:
        token = os.environ.get("UPSTOX_ACCESS_TOKEN", "")
        ipos = fetch_all_real(token)
        count, skipped, output = generate(ipos, token)

    print(f"Generated {output} with {count} complete IPO rows.")
    print(f"Flagged/skipped IPOs: {skipped}")
    print(f"Download filename: {output.name}")


if __name__ == "__main__":
    main()
