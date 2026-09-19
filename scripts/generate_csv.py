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
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import NoReturn


ROOT = Path(__file__).resolve().parents[1]

TEMPLATE = ROOT / "template" / "NSE_CM_security_template.csv"

OUTPUT_DIR = ROOT / "output"
OUTPUT = OUTPUT_DIR / "ipo_security_master.csv"
METADATA = OUTPUT_DIR / "latest.json"

UPSTOX_URL = "https://api.upstox.com/v2/ipos"

STATUSES = ("open", "upcoming")

REQUIRED_REPLACEMENTS = (
    "FinInstrmId",
    "TckrSymb",
    "FinInstrmNm",
    "ISIN",
)

REQUIRED_IPO_FIELDS = (
    "symbol",
    "name",
    "isin",
)


def fail(message: str) -> NoReturn:
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
        fail(
            f"Template is missing required columns: {', '.join(missing)}"
        )

    return header, template_row


def make_request(url: str, token: str) -> urllib.request.Request:
    return urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "IPO-Security-CSV-Generator/1.0",
        },
        method="GET",
    )


def fetch_page(token: str, status: str, page: int) -> dict:
    query = urllib.parse.urlencode(
        {
            "status": status,
            "page_number": page,
            "records": 30,
        }
    )

    url = f"{UPSTOX_URL}?{query}"

    request = make_request(url, token)

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read().decode("utf-8")

            if response.status != 200:
                fail(
                    f"Upstox returned HTTP {response.status}: "
                    f"{body[:500]}"
                )

            return json.loads(body)

    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")

        fail(
            f"Upstox request failed for status={status}, page={page}. "
            f"HTTP {exc.code}: {body[:500]}"
        )

    except urllib.error.URLError as exc:
        fail(
            f"Upstox request failed for status={status}, page={page}: "
            f"{exc.reason}"
        )

    except Exception as exc:
        fail(
            f"Upstox request failed for status={status}, page={page}: "
            f"{exc}"
        )


def fetch_ipo_details(
    token: str,
    ipo_id: str,
) -> dict:
    """
    Fetch detailed information for one IPO.

    Returns an empty dictionary if the details request fails.
    A failure here should NOT stop the entire generation process.
    """

    encoded_id = urllib.parse.quote(str(ipo_id), safe="")

    url = f"{UPSTOX_URL}/{encoded_id}"

    request = make_request(url, token)

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read().decode("utf-8")

            if response.status != 200:
                print(
                    f"WARNING: Details request for {ipo_id} "
                    f"returned HTTP {response.status}"
                )
                return {}

            payload = json.loads(body)

            if payload.get("status") not in (None, "success"):
                print(
                    f"WARNING: Details request for {ipo_id} "
                    f"returned an API error."
                )
                return {}

            data = payload.get("data")

            if isinstance(data, dict):
                return data

            return {}

    except Exception as exc:
        print(
            f"WARNING: Could not fetch details for IPO {ipo_id}: {exc}"
        )
        return {}


def get_missing_fields(ipo: dict) -> list[str]:
    missing = []

    for field in REQUIRED_IPO_FIELDS:
        value = str(ipo.get(field) or "").strip()

        if not value:
            missing.append(field)

    return missing


def enrich_missing_fields(
    token: str,
    ipos: list[dict],
) -> tuple[list[dict], list[dict]]:
    """
    Try to fill missing symbol/name/isin values using the
    Upstox IPO details endpoint.

    IPOs that remain incomplete are skipped and returned
    separately as flagged records.
    """

    valid_ipos: list[dict] = []
    flagged_ipos: list[dict] = []

    details_cache: dict[str, dict] = {}

    for ipo in ipos:
        ipo_copy = dict(ipo)

        missing_before = get_missing_fields(ipo_copy)

        if missing_before:
            ipo_id = str(ipo_copy.get("id") or "").strip()

            if ipo_id:
                if ipo_id not in details_cache:
                    print(
                        f"Fetching details for incomplete IPO: {ipo_id}"
                    )

                    details_cache[ipo_id] = fetch_ipo_details(
                        token,
                        ipo_id,
                    )

                details = details_cache[ipo_id]

                # Only fill fields that are missing.
                # Existing Upstox listing values are never overwritten.
                for field in REQUIRED_IPO_FIELDS:
                    existing_value = str(
                        ipo_copy.get(field) or ""
                    ).strip()

                    detail_value = str(
                        details.get(field) or ""
                    ).strip()

                    if not existing_value and detail_value:
                        ipo_copy[field] = detail_value

        missing_after = get_missing_fields(ipo_copy)

        if missing_after:
            flagged = {
                "id": str(ipo_copy.get("id") or ""),
                "name": str(ipo_copy.get("name") or ""),
                "status": str(ipo_copy.get("status") or ""),
                "missing_fields": missing_after,
            }

            flagged_ipos.append(flagged)

            print(
                "FLAGGED / SKIPPED: "
                f"{flagged['id'] or '<unknown>'} "
                f"missing {', '.join(missing_after)}"
            )

            continue

        valid_ipos.append(ipo_copy)

    return valid_ipos, flagged_ipos


def fetch_all_real(token: str) -> list[dict]:
    if not token.strip():
        fail("UPSTOX_ACCESS_TOKEN is empty.")

    all_ipos: list[dict] = []

    for status in STATUSES:
        page = 1
        total_pages = 1

        while page <= total_pages:
            payload = fetch_page(
                token,
                status,
                page,
            )

            if payload.get("status") not in (None, "success"):
                fail(
                    f"Upstox API reported an error: {payload}"
                )

            records = payload.get("data") or []

            all_ipos.extend(records)

            page_info = (
                (payload.get("meta_data") or {})
                .get("page")
                or {}
            )

            total_pages = int(
                page_info.get("total_pages") or 1
            )

            print(
                f"Fetched {len(records)} IPOs: "
                f"status={status}, "
                f"page={page}/{total_pages}"
            )

            page += 1

    return all_ipos


def fetch_mock(path: Path) -> list[dict]:
    with path.open("r", encoding="utf-8") as f:
        payload = json.load(f)

    if isinstance(payload, list):
        return payload

    records: list[dict] = []

    for status in STATUSES:
        records.extend(
            payload.get(status, [])
        )

    return records


def normalize_and_dedupe(
    ipos: list[dict],
) -> list[dict]:
    """
    Dedupe by Upstox IPO id, with open taking precedence
    over upcoming.

    If id is absent, use symbol+isin+name as fallback.
    """

    priority = {
        "open": 0,
        "upcoming": 1,
    }

    selected: dict[str, dict] = {}

    for ipo in ipos:
        status = str(
            ipo.get("status") or ""
        ).lower()

        if status not in STATUSES:
            continue

        key = str(
            ipo.get("id")
            or (
                f"{ipo.get('symbol', '')}|"
                f"{ipo.get('isin', '')}|"
                f"{ipo.get('name', '')}"
            )
        ).strip()

        if not key or key == "||":
            print(
                "FLAGGED / SKIPPED: IPO record has no "
                "usable ID/symbol/ISIN/name."
            )
            continue

        existing = selected.get(key)

        if existing is None:
            selected[key] = ipo

        else:
            old_status = str(
                existing.get("status") or ""
            ).lower()

            if priority.get(status, 99) < priority.get(
                old_status,
                99,
            ):
                selected[key] = ipo

    return list(selected.values())


def generate_unique_ids(
    count: int,
) -> list[str]:
    if count > 900_000:
        fail(
            "Cannot generate enough unique 6-digit IDs."
        )

    values: set[int] = set()

    while len(values) < count:
        values.add(
            secrets.randbelow(900_000) + 100_000
        )

    return [
        str(value)
        for value in values
    ]


def generate(
    ipos: list[dict],
    token: str | None = None,
) -> tuple[int, list[dict], list[dict]]:
    header, template_row = load_template()

    ipos = normalize_and_dedupe(ipos)

    if token:
        ipos, flagged_ipos = enrich_missing_fields(
            token,
            ipos,
        )
    else:
        flagged_ipos = []

        valid_ipos: list[dict] = []

        for ipo in ipos:
            missing = get_missing_fields(ipo)

            if missing:
                flagged_ipos.append(
                    {
                        "id": str(
                            ipo.get("id") or ""
                        ),
                        "name": str(
                            ipo.get("name") or ""
                        ),
                        "status": str(
                            ipo.get("status") or ""
                        ),
                        "missing_fields": missing,
                    }
                )
            else:
                valid_ipos.append(ipo)

        ipos = valid_ipos

    if not ipos:
        fail(
            "No valid IPOs remain after validation. "
            "No CSV was generated."
        )

    indices = {
        name: header.index(name)
        for name in REQUIRED_REPLACEMENTS
    }

    ids = generate_unique_ids(
        len(ipos)
    )

    rows: list[list[str]] = []

    for ipo, instrument_id in zip(
        ipos,
        ids,
    ):
        symbol = str(
            ipo.get("symbol") or ""
        ).strip()

        name = str(
            ipo.get("name") or ""
        ).strip()

        isin = str(
            ipo.get("isin") or ""
        ).strip()

        row = template_row.copy()

        # These are the ONLY four fields modified.
        row[
            indices["FinInstrmId"]
        ] = instrument_id

        row[
            indices["TckrSymb"]
        ] = symbol

        row[
            indices["FinInstrmNm"]
        ] = name

        row[
            indices["ISIN"]
        ] = isin

        rows.append(row)

    OUTPUT_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    with OUTPUT.open(
        "w",
        encoding="utf-8",
        newline="",
    ) as f:
        writer = csv.writer(
            f,
            lineterminator="\n",
        )

        writer.writerow(header)
        writer.writerows(rows)

    metadata = {
        "generated_utc": datetime.now(
            timezone.utc
        ).isoformat(),
        "ipo_count": len(rows),
        "skipped_count": len(flagged_ipos),
        "statuses": [
            "open",
            "upcoming",
        ],
        "template": TEMPLATE.name,
        "replaced_columns": list(
            REQUIRED_REPLACEMENTS
        ),
        "skipped_ipos": flagged_ipos,
    }

    with METADATA.open(
        "w",
        encoding="utf-8",
    ) as f:
        json.dump(
            metadata,
            f,
            indent=2,
        )

    return (
        len(rows),
        ipos,
        flagged_ipos,
    )


def main() -> None:
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--mock",
        type=Path,
        help=(
            "Use a local mock Upstox JSON "
            "instead of the real API."
        ),
    )

    args = parser.parse_args()

    if args.mock:
        ipos = fetch_mock(
            args.mock
        )

        count, _, flagged = generate(
            ipos
        )

    else:
        token = os.environ.get(
            "UPSTOX_ACCESS_TOKEN",
            "",
        )

        ipos = fetch_all_real(
            token
        )

        count, _, flagged = generate(
            ipos,
            token,
        )

    print(
        f"Generated {OUTPUT} "
        f"with {count} IPO rows."
    )

    if flagged:
        print(
            f"Flagged/skipped IPOs: "
            f"{len(flagged)}"
        )

        for ipo in flagged:
            print(
                f"  - "
                f"{ipo.get('name') or ipo.get('id')}: "
                f"missing "
                f"{', '.join(ipo['missing_fields'])}"
            )
    else:
        print(
            "No IPOs were flagged or skipped."
        )


if __name__ == "__main__":
    main()
