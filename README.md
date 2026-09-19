# IPO Security CSV Generator

A small GitHub Actions + GitHub Pages project that fetches **all Upstox `open` and `upcoming` IPOs** and generates an NSE-security-master-shaped CSV from a fixed template row.

## What it changes

For every IPO, only these four columns are replaced:

- `FinInstrmId` → unique random 6-digit integer
- `TckrSymb` → Upstox `symbol`
- `FinInstrmNm` → Upstox `name`
- `ISIN` → Upstox `isin`

Every other column is copied **exactly from row 2 of `template/NSE_CM_security_template.csv`**.

If the same IPO appears in both `open` and `upcoming`, it is emitted only once, with `open` taking precedence.

## Setup

1. Create a **public GitHub repository** and upload this project.
2. In the repository, go to **Settings → Secrets and variables → Actions**.
3. Add a repository secret:
   - Name: `UPSTOX_ACCESS_TOKEN`
   - Value: your usable Upstox access token.
4. Enable GitHub Pages:
   - Settings → Pages
   - Source: **GitHub Actions**
5. Run the `Generate IPO CSV` workflow manually once.
6. The generated file will appear at:
   - `output/ipo_security_master.csv`
   - The website will show a download link to the latest file.

## Automatic schedule

The workflow is configured for **5:00 PM Asia/Kolkata every day**.

GitHub scheduled workflows can be delayed occasionally by platform load, so this is a target schedule rather than a hard real-time guarantee.

## Manual run

Go to **Actions → Generate IPO CSV → Run workflow**.

The workflow can also be used to rebuild the GitHub Pages site after generation.

## Security

The Upstox token is read only from the GitHub Actions secret. Never put it in `site/`, JavaScript, the CSV, or committed source files.

The generated CSV is public if the repository is public. Do not use this project if the generated output itself must remain private.

## Local test

Requires Python 3.11+:

```bash
python scripts/generate_csv.py --mock tests/mock_upstox.json
```

The real workflow uses:

```bash
python scripts/generate_csv.py
```
# ipo-security-csv-generator
