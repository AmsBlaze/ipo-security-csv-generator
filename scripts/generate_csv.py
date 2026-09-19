#!/usr/bin/env python3

"""
IPO Security CSV Generator

Fetches all OPEN and UPCOMING IPOs from Upstox and creates
an NSE-security-master-shaped CSV using row 2 of the template.

Rules:
- Only OPEN and UPCOMING IPOs are considered.
- IPOs missing symbol/ISIN after Upstox detail lookup are skipped.
- Missing IPOs do NOT stop the generation job.
- FinInstrmId is a unique random 6-digit number.
- TckrSymb comes from Upstox symbol.
- ISIN comes from Upstox ISIN.
- FinInstrmNm comes from the Upstox company/security name,
  with a trailing "IPO" removed.
- Every other template field is copied unchanged.
- Output filename:
    NSE_CM_security_DDMMYYYY.csv

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
from zoneinfo import ZoneInfo


# ============================================================
# PATHS
# ============================================================

ROOT = Path(__file__).resolve().parents[1]

TEMPLATE = (
    ROOT
    / "template"
    / "NSE_CM_security_template.csv"
)

OUTPUT_DIR = ROOT / "output"

METADATA = OUTPUT_DIR / "latest.json"


# ============================================================
# UPSTOX
# ============================================================

UPSTOX_URL = (
    "https://api.upstox.com/v2/ipos"
)

STATUSES = (
    "open",
    "upcoming",
)


# ============================================================
# TEMPLATE COLUMNS
# ============================================================

# These are the ONLY four columns modified
# in the generated CSV.
REQUIRED_REPLACEMENTS = (
    "FinInstrmId",
    "TckrSymb",
    "FinInstrmNm",
    "ISIN",
)


# Required fields from Upstox before an IPO
# can be included in the CSV.
REQUIRED_IPO_FIELDS = (
    "symbol",
    "name",
    "isin",
)


# ============================================================
# FATAL ERROR
# ============================================================

def fail(message: str) -> NoReturn:
    print(
        f"ERROR: {message}",
        file=sys.stderr,
    )
    raise SystemExit(1)


# ============================================================
# LOAD TEMPLATE
# ============================================================

def load_template() -> tuple[list[str], list[str]]:
    """
    Load the NSE security-master template.

    Row 2 is used as the base row for every IPO.
    """

    if not TEMPLATE.exists():
        fail(
            f"Template not found: {TEMPLATE}"
        )

    with TEMPLATE.open(
        "r",
        encoding="utf-8-sig",
        newline="",
    ) as file:

        rows = list(
            csv.reader(file)
        )

    if len(rows) < 2:
        fail(
            "Template CSV must contain "
            "a header and at least one data row."
        )

    header = rows[0]
    template_row = rows[1]

    if len(header) != len(template_row):
        fail(
            "Template header has "
            f"{len(header)} columns but row 2 has "
            f"{len(template_row)} columns."
        )

    missing = [
        column
        for column in REQUIRED_REPLACEMENTS
        if column not in header
    ]

    if missing:
        fail(
            "Template is missing required columns: "
            + ", ".join(missing)
        )

    return header, template_row


# ============================================================
# HTTP REQUEST
# ============================================================

def make_request(
    url: str,
    token: str,
) -> urllib.request.Request:

    return urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "Authorization": (
                f"Bearer {token}"
            ),
            "User-Agent": (
                "IPO-Security-CSV-Generator/1.0"
            ),
        },
        method="GET",
    )


# ============================================================
# FETCH IPO PAGE
# ============================================================

def fetch_page(
    token: str,
    status: str,
    page: int,
) -> dict:

    query = urllib.parse.urlencode(
        {
            "status": status,
            "page_number": page,
            "records": 30,
        }
    )

    url = (
        f"{UPSTOX_URL}?{query}"
    )

    request = make_request(
        url,
        token,
    )

    try:

        with urllib.request.urlopen(
            request,
            timeout=30,
        ) as response:

            body = response.read().decode(
                "utf-8"
            )

            if response.status != 200:
                fail(
                    "Upstox returned HTTP "
                    f"{response.status}: "
                    f"{body[:500]}"
                )

            return json.loads(body)

    except urllib.error.HTTPError as exc:

        body = exc.read().decode(
            "utf-8",
            errors="replace",
        )

        fail(
            "Upstox request failed for "
            f"status={status}, page={page}. "
            f"HTTP {exc.code}: {body[:500]}"
        )

    except urllib.error.URLError as exc:

        fail(
            "Upstox request failed for "
            f"status={status}, page={page}: "
            f"{exc.reason}"
        )

    except json.JSONDecodeError as exc:

        fail(
            "Upstox returned invalid JSON for "
            f"status={status}, page={page}: "
            f"{exc}"
        )

    except Exception as exc:

        fail(
            "Upstox request failed for "
            f"status={status}, page={page}: "
            f"{exc}"
        )


# ============================================================
# FETCH IPO DETAILS
# ============================================================

def fetch_ipo_details(
    token: str,
    ipo_id: str,
) -> dict:
    """
    Try to retrieve missing IPO fields from:
        GET /v2/ipos/{id}

    Failure is NOT fatal.
    """

    encoded_id = urllib.parse.quote(
        str(ipo_id),
        safe="",
    )

    url = (
        f"{UPSTOX_URL}/{encoded_id}"
    )

    request = make_request(
        url,
        token,
    )

    try:

        with urllib.request.urlopen(
            request,
            timeout=30,
        ) as response:

            body = response.read().decode(
                "utf-8"
            )

            if response.status != 200:

                print(
                    "WARNING: Details request for "
                    f"{ipo_id} returned HTTP "
                    f"{response.status}"
                )

                return {}

            payload = json.loads(body)

            if payload.get("status") not in (
                None,
                "success",
            ):

                print(
                    "WARNING: Details request for "
                    f"{ipo_id} returned an API error."
                )

                return {}

            data = payload.get(
                "data"
            )

            if isinstance(
                data,
                dict,
            ):
                return data

            return {}

    except Exception as exc:

        print(
            "WARNING: Could not fetch details "
            f"for IPO {ipo_id}: {exc}"
        )

        return {}


# ============================================================
# MISSING FIELD CHECK
# ============================================================

def get_missing_fields(
    ipo: dict,
) -> list[str]:

    missing: list[str] = []

    for field in REQUIRED_IPO_FIELDS:

        value = str(
            ipo.get(field) or ""
        ).strip()

        if not value:
            missing.append(field)

    return missing


# ============================================================
# ENRICH INCOMPLETE IPOs
# ============================================================

def enrich_missing_fields(
    token: str,
    ipos: list[dict],
) -> tuple[list[dict], list[dict]]:
    """
    Try the Upstox IPO details endpoint for incomplete records.

    IPOs still missing symbol or ISIN are flagged/skipped.
    """

    valid_ipos: list[dict] = []

    flagged_ipos: list[dict] = []

    details_cache: dict[str, dict] = {}

    for ipo in ipos:

        ipo_copy = dict(ipo)

        missing_before = (
            get_missing_fields(
                ipo_copy
            )
        )

        if missing_before:

            ipo_id = str(
                ipo_copy.get("id") or ""
            ).strip()

            if ipo_id:

                if ipo_id not in details_cache:

                    print(
                        "Fetching details for "
                        f"incomplete IPO: {ipo_id}"
                    )

                    details_cache[
                        ipo_id
                    ] = fetch_ipo_details(
                        token,
                        ipo_id,
                    )

                details = (
                    details_cache[
                        ipo_id
                    ]
                )

                # Only fill missing fields.
                # Never overwrite values already
                # provided by the listing endpoint.
                for field in REQUIRED_IPO_FIELDS:

                    current_value = str(
                        ipo_copy.get(field)
                        or ""
                    ).strip()

                    detail_value = str(
                        details.get(field)
                        or ""
                    ).strip()

                    if (
                        not current_value
                        and detail_value
                    ):

                        ipo_copy[
                            field
                        ] = detail_value

        missing_after = (
            get_missing_fields(
                ipo_copy
            )
        )

        if missing_after:

            flagged = {
                "id": str(
                    ipo_copy.get("id")
                    or ""
                ),
                "name": str(
                    ipo_copy.get("name")
                    or ""
                ),
                "status": str(
                    ipo_copy.get("status")
                    or ""
                ).lower(),
                "missing_fields": (
                    missing_after
                ),
            }

            flagged_ipos.append(
                flagged
            )

            print(
                "FLAGGED / SKIPPED: "
                f"{flagged['name'] or flagged['id']} "
                f"missing "
                f"{', '.join(missing_after)}"
            )

            continue

        valid_ipos.append(
            ipo_copy
        )

    return (
        valid_ipos,
        flagged_ipos,
    )


# ============================================================
# FETCH ALL OPEN + UPCOMING IPOs
# ============================================================

def fetch_all_real(
    token: str,
) -> list[dict]:

    if not token.strip():
        fail(
            "UPSTOX_ACCESS_TOKEN is empty."
        )

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

            if payload.get(
                "status"
            ) not in (
                None,
                "success",
            ):

                fail(
                    "Upstox API reported an error: "
                    f"{payload}"
                )

            records = (
                payload.get("data")
                or []
            )

            if not isinstance(
                records,
                list,
            ):

                fail(
                    "Unexpected Upstox response: "
                    "`data` is not a list."
                )

            all_ipos.extend(
                records
            )

            page_info = (
                (
                    payload.get(
                        "meta_data"
                    )
                    or {}
                ).get("page")
                or {}
            )

            total_pages = int(
                page_info.get(
                    "total_pages"
                )
                or 1
            )

            print(
                f"Fetched {len(records)} IPOs: "
                f"status={status}, "
                f"page={page}/{total_pages}"
            )

            page += 1

    return all_ipos


# ============================================================
# MOCK
# ============================================================

def fetch_mock(
    path: Path,
) -> list[dict]:

    if not path.exists():
        fail(
            f"Mock file not found: {path}"
        )

    with path.open(
        "r",
        encoding="utf-8",
    ) as file:

        payload = json.load(file)

    if isinstance(
        payload,
        list,
    ):
        return payload

    records: list[dict] = []

    for status in STATUSES:

        records.extend(
            payload.get(
                status,
                [],
            )
        )

    return records


# ============================================================
# NORMALIZE + DEDUPE
# ============================================================

def normalize_and_dedupe(
    ipos: list[dict],
) -> list[dict]:
    """
    Keep only OPEN and UPCOMING IPOs.

    If the same IPO appears multiple times:
    OPEN takes precedence over UPCOMING.
    """

    priority = {
        "open": 0,
        "upcoming": 1,
    }

    selected: dict[str, dict] = {}

    for ipo in ipos:

        status = str(
            ipo.get("status")
            or ""
        ).lower()

        if status not in STATUSES:
            continue

        ipo_id = str(
            ipo.get("id")
            or ""
        ).strip()

        key = (
            ipo_id
            or (
                f"{ipo.get('symbol', '')}|"
                f"{ipo.get('isin', '')}|"
                f"{ipo.get('name', '')}"
            )
        ).strip()

        if not key:

            print(
                "FLAGGED / SKIPPED: "
                "IPO record has no usable identifier."
            )

            continue

        existing = selected.get(
            key
        )

        if existing is None:

            selected[key] = ipo

        else:

            old_status = str(
                existing.get(
                    "status"
                )
                or ""
            ).lower()

            if priority.get(
                status,
                99,
            ) < priority.get(
                old_status,
                99,
            ):

                selected[key] = ipo

    return list(
        selected.values()
    )


# ============================================================
# SECURITY MASTER NAME
# ============================================================

def build_fin_instrm_name(
    ipo: dict,
) -> str:
    """
    Convert the Upstox IPO name into the security-master
    instrument name.

    Example:

        Robokidz Eduventures IPO
        ->
        Robokidz Eduventures

    No NSE lookup is required for an IPO to be considered
    complete.
    """

    name = str(
        ipo.get("name")
        or ""
    ).strip()

    if not name:
        return ""

    # Remove trailing "IPO" only.
    if name.upper().endswith(
        " IPO"
    ):

        name = name[
            :-4
        ].strip()

    return name


# ============================================================
# UNIQUE SIX-DIGIT IDs
# ============================================================

def generate_unique_ids(
    count: int,
) -> list[str]:

    if count > 900_000:
        fail(
            "Cannot generate enough unique "
            "6-digit IDs."
        )

    values: set[int] = set()

    while len(values) < count:

        values.add(
            secrets.randbelow(
                900_000
            )
            + 100_000
        )

    return [
        str(value)
        for value in values
    ]


# ============================================================
# OUTPUT FILENAME
# ============================================================

def get_output_filename() -> str:
    """
    Generate:

        NSE_CM_security_DDMMYYYY.csv

    using India Standard Time.
    """

    india_now = datetime.now(
        ZoneInfo(
            "Asia/Kolkata"
        )
    )

    date_string = (
        india_now.strftime(
            "%d%m%Y"
        )
    )

    return (
        "NSE_CM_security_"
        f"{date_string}.csv"
    )


# ============================================================
# GENERATE CSV
# ============================================================

def generate(
    ipos: list[dict],
    token: str | None = None,
) -> tuple[
    int,
    Path,
    list[dict],
    list[dict],
]:
    """
    Generate the final CSV.

    Returns:

        generated_count
        output_path
        valid_ipos
        flagged_ipos
    """

    header, template_row = (
        load_template()
    )

    # --------------------------------------------------------
    # Normalize and deduplicate
    # --------------------------------------------------------

    ipos = normalize_and_dedupe(
        ipos
    )

    # --------------------------------------------------------
    # Real mode:
    # Try details endpoint for incomplete records.
    # --------------------------------------------------------

    if token:

        valid_ipos, flagged_ipos = (
            enrich_missing_fields(
                token,
                ipos,
            )
        )

    # --------------------------------------------------------
    # Mock mode
    # --------------------------------------------------------

    else:

        valid_ipos = []
        flagged_ipos = []

        for ipo in ipos:

            missing = (
                get_missing_fields(
                    ipo
                )
            )

            if missing:

                flagged_ipos.append(
                    {
                        "id": str(
                            ipo.get(
                                "id"
                            )
                            or ""
                        ),
                        "name": str(
                            ipo.get(
                                "name"
                            )
                            or ""
                        ),
                        "status": str(
                            ipo.get(
                                "status"
                            )
                            or ""
                        ).lower(),
                        "missing_fields": (
                            missing
                        ),
                    }
                )

            else:

                valid_ipos.append(
                    ipo
                )

    # --------------------------------------------------------
    # No complete IPOs
    # --------------------------------------------------------

    if not valid_ipos:

        fail(
            "No complete IPOs are available "
            "for CSV generation."
        )

    # --------------------------------------------------------
    # Locate template columns
    # --------------------------------------------------------

    indices = {
        column: header.index(
            column
        )
        for column in REQUIRED_REPLACEMENTS
    }

    # --------------------------------------------------------
    # Generate unique FinInstrmId values
    # --------------------------------------------------------

    instrument_ids = (
        generate_unique_ids(
            len(valid_ipos)
        )
    )

    rows: list[list[str]] = []

    # --------------------------------------------------------
    # Generate rows
    # --------------------------------------------------------

    for ipo, instrument_id in zip(
        valid_ipos,
        instrument_ids,
    ):

        symbol = str(
            ipo.get("symbol")
            or ""
        ).strip()

        isin = str(
            ipo.get("isin")
            or ""
        ).strip()

        fin_instrm_name = (
            build_fin_instrm_name(
                ipo
            )
        )

        # Final validation.
        if not symbol or not isin:

            missing_fields = []

            if not symbol:
                missing_fields.append(
                    "symbol"
                )

            if not isin:
                missing_fields.append(
                    "isin"
                )

            print(
                "FLAGGED / SKIPPED: "
                f"{ipo.get('name', '<unknown>')} "
                f"missing "
                f"{', '.join(missing_fields)}"
            )

            flagged_ipos.append(
                {
                    "id": str(
                        ipo.get(
                            "id"
                        )
                        or ""
                    ),
                    "name": str(
                        ipo.get(
                            "name"
                        )
                        or ""
                    ),
                    "status": str(
                        ipo.get(
                            "status"
                        )
                        or ""
                    ).lower(),
                    "missing_fields": (
                        missing_fields
                    ),
                }
            )

            continue

        if not fin_instrm_name:

            print(
                "FLAGGED / SKIPPED: "
                f"{ipo.get('name', '<unknown>')} "
                "has no usable FinInstrmNm."
            )

            flagged_ipos.append(
                {
                    "id": str(
                        ipo.get(
                            "id"
                        )
                        or ""
                    ),
                    "name": str(
                        ipo.get(
                            "name"
                        )
                        or ""
                    ),
                    "status": str(
                        ipo.get(
                            "status"
                        )
                        or ""
                    ).lower(),
                    "missing_fields": [
                        "FinInstrmNm"
                    ],
                }
            )

            continue

        # ----------------------------------------------------
        # COPY ROW 2 OF TEMPLATE
        # ----------------------------------------------------

        row = template_row.copy()

        # ----------------------------------------------------
        # ONLY THESE FOUR VALUES CHANGE
        # ----------------------------------------------------

        row[
            indices["FinInstrmId"]
        ] = instrument_id

        row[
            indices["TckrSymb"]
        ] = symbol

        row[
            indices["FinInstrmNm"]
        ] = fin_instrm_name

        row[
            indices["ISIN"]
        ] = isin

        rows.append(
            row
        )

    # --------------------------------------------------------
    # Final safety check
    # --------------------------------------------------------

    if not rows:

        fail(
            "No complete IPOs are available "
            "for CSV generation."
        )

    # --------------------------------------------------------
    # Create output directory
    # --------------------------------------------------------

    OUTPUT_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    # --------------------------------------------------------
    # Dated filename
    # --------------------------------------------------------

    output_filename = (
        get_output_filename()
    )

    output_path = (
        OUTPUT_DIR
        / output_filename
    )

    # --------------------------------------------------------
    # Write CSV
    # --------------------------------------------------------

    with output_path.open(
        "w",
        encoding="utf-8",
        newline="",
    ) as file:

        writer = csv.writer(
            file,
            lineterminator="\n",
        )

        writer.writerow(
            header
        )

        writer.writerows(
            rows
        )

    # ========================================================
    # WEBSITE METADATA
    # ========================================================

    # This structure intentionally supports the existing
    # GitHub Pages website.

    complete_ipos = []

    for ipo in valid_ipos:

        symbol = str(
            ipo.get("symbol")
            or ""
        ).strip()

        isin = str(
            ipo.get("isin")
            or ""
        ).strip()

        instrument_name = (
            build_fin_instrm_name(
                ipo
            )
        )

        # Only expose records that actually made it
        # into the generated CSV.
        if (
            symbol
            and isin
            and instrument_name
        ):

            complete_ipos.append(
                {
                    "ipo_name": str(
                        ipo.get(
                            "name"
                        )
                        or ""
                    ).strip(),

                    "status": str(
                        ipo.get(
                            "status"
                        )
                        or ""
                    ).strip().lower(),

                    "symbol": symbol,

                    "instrument_name": (
                        instrument_name
                    ),

                    "isin": isin,
                }
            )

    india_now = datetime.now(
        ZoneInfo(
            "Asia/Kolkata"
        )
    )

    # --------------------------------------------------------
    # IMPORTANT:
    #
    # Keep BOTH "filename" and "csv_filename".
    #
    # The existing workflow expects:
    #     csv_filename
    #
    # The website can use:
    #     filename
    #
    # Keeping both prevents compatibility problems.
    # --------------------------------------------------------

    metadata = {

        "generated_utc": (
            datetime.now(
                timezone.utc
            ).isoformat()
        ),

        "generated_ist": (
            india_now.isoformat()
        ),

        "generated_date_ddmmyyyy": (
            india_now.strftime(
                "%d/%m/%Y"
            )
        ),

        "filename": (
            output_filename
        ),

        "csv_filename": (
            output_filename
        ),

        "ipo_count": (
            len(rows)
        ),

        "skipped_count": (
            len(flagged_ipos)
        ),

        "open_count": sum(
            1
            for ipo in complete_ipos
            if ipo["status"] == "open"
        ),

        "upcoming_count": sum(
            1
            for ipo in complete_ipos
            if ipo["status"] == "upcoming"
        ),

        # THIS is what the current website expects.
        "complete_ipos": (
            complete_ipos
        ),

        "statuses": [
            "open",
            "upcoming",
        ],

        "template": (
            TEMPLATE.name
        ),

        "replaced_columns": list(
            REQUIRED_REPLACEMENTS
        ),

        "skipped_ipos": (
            flagged_ipos
        ),
    }

    with METADATA.open(
        "w",
        encoding="utf-8",
    ) as file:

        json.dump(
            metadata,
            file,
            indent=2,
            ensure_ascii=False,
        )

    return (
        len(rows),
        output_path,
        valid_ipos,
        flagged_ipos,
    )


# ============================================================
# MAIN
# ============================================================

def main() -> None:

    parser = argparse.ArgumentParser(
        description=(
            "Generate an NSE security-master-shaped "
            "CSV from Upstox IPO data."
        )
    )

    parser.add_argument(
        "--mock",
        type=Path,
        help=(
            "Use a local mock Upstox JSON file "
            "instead of the real API."
        ),
    )

    args = parser.parse_args()

    # --------------------------------------------------------
    # MOCK MODE
    # --------------------------------------------------------

    if args.mock:

        ipos = fetch_mock(
            args.mock
        )

        (
            count,
            output_path,
            valid_ipos,
            flagged_ipos,
        ) = generate(
            ipos
        )

    # --------------------------------------------------------
    # REAL UPSTOX MODE
    # --------------------------------------------------------

    else:

        token = os.environ.get(
            "UPSTOX_ACCESS_TOKEN",
            "",
        )

        ipos = fetch_all_real(
            token
        )

        (
            count,
            output_path,
            valid_ipos,
            flagged_ipos,
        ) = generate(
            ipos,
            token,
        )

    # --------------------------------------------------------
    # SUMMARY
    # --------------------------------------------------------

    print()
    print(
        "=========================================="
    )
    print(
        "IPO SECURITY CSV GENERATION COMPLETE"
    )
    print(
        "=========================================="
    )

    print(
        f"Generated file : {output_path.name}"
    )

    print(
        f"Generated rows : {count}"
    )

    print(
        f"Skipped IPOs   : {len(flagged_ipos)}"
    )

    print(
        f"Open rows      : "
        f"{sum(1 for ipo in valid_ipos if str(ipo.get('status', '')).lower() == 'open')}"
    )

    print(
        f"Upcoming rows  : "
        f"{sum(1 for ipo in valid_ipos if str(ipo.get('status', '')).lower() == 'upcoming')}"
    )

    print()

    if flagged_ipos:

        print(
            "Flagged / skipped IPOs:"
        )

        for ipo in flagged_ipos:

            name = (
                ipo.get("name")
                or ipo.get("id")
                or "<unknown>"
            )

            missing = ", ".join(
                ipo.get(
                    "missing_fields",
                    [],
                )
            )

            print(
                f"  - {name}: "
                f"missing {missing}"
            )

    else:

        print(
            "No IPOs were flagged or skipped."
        )

    print(
        "=========================================="
    )


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":
    main()
