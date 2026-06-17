# Google Vehicle Ads feed for Truck Solutions

Daily pipeline that transforms the dealership's CWS Platform inventory export
into a Google Merchant Center vehicle listings feed.

## How it works

```
CWS XML feed  →  transform_feed.py  →  feed.xml  →  Merchant Center scheduled fetch
   (source)         (this repo)        (raw URL)         (every 24h)
```

A GitHub Actions cron job runs `transform_feed.py` once a day. The script
fetches the live CWS export, filters out items that aren't eligible for Google
Vehicle Ads, remaps the rest into Google's required RSS 2.0 format, and writes
`feed.xml` back to this repo. Merchant Center then pulls `feed.xml` from its
raw URL on its own schedule.

## One-time setup

### 1. Push this repo to GitHub
Public repo is fine (the feed file needs to be reachable by Google), or use a
private repo with a deploy token if you'd rather not expose VINs publicly.

### 2. Edit `STORE_CODE_BY_CITY` in `transform_feed.py`
Replace the placeholder store codes with the real `store_code` values from your
Google Business Profile locations. **The feed will fail in Merchant Center
without this** — Vehicle Ads requires each listing to be tied to a verified
dealership location.

### 3. Confirm linkages in Merchant Center
- Vehicle Ads program enabled
- Google Business Profile linked to Merchant Center
- Each GBP location has the same `store_code` you set in step 2

### 4. Add the feed in Merchant Center
Merchant Center → Data sources → Add primary data source → **Vehicle listings**
program → "Scheduled fetch" → use the GitHub **raw** URL:

```
https://raw.githubusercontent.com/<your-user>/<your-repo>/main/feed.xml
```

Set the fetch schedule to daily. Pick a time a couple hours after the cron job
in `.github/workflows/update-feed.yml` (default: 06:00 UTC).

### 5. Trigger the workflow manually once
Actions tab → "Update Google Merchant Center vehicle feed" → "Run workflow".
This commits an initial `feed.xml` so Merchant Center has something to fetch.

## What the script filters out (and why)

Looking at the current CWS export, expect roughly **8–10 of the ~48 listings**
to make it into the feed. The rest get excluded for reasons Google won't accept:

| Reason | Why Google rejects it |
|---|---|
| `price = 0` ("Request a Quote") | Vehicle Ads requires a real positive price |
| Missing or invalid VIN | Required, must be 17-char standard VIN |
| `odometer-type = Hours` | Off-highway equipment, not a vehicle |
| Bodies only (Dry Van Body, Reefer Body) | Parts/accessories, not a saleable vehicle |
| `model-year` out of range | Sanity check |

When the script runs it prints a breakdown so you can see exactly which
listings were excluded and why.

## Things to verify before relying on this

**Vehicle Ads eligibility for heavy trucks.** Google Vehicle Ads launched
focused on consumer autos. Class 6–8 commercial trucks, tow trucks, water
trucks, hooklift trucks, etc. may not be eligible regardless of feed quality.
Confirm with Google support that your category mix is accepted before
investing time chasing data quality issues.

**Real prices, not "Request a Quote".** The single biggest fixable problem is
that most of the inventory has no price in CWS. If you want those vehicles to
run ads, you need to enter real prices in the CWS dealer back-office. No feed
transformer can work around that — Google requires the price.

**VIN data quality.** Several CWS listings have `identification-type = Serial`
with values that aren't valid VINs (dealer stock IDs, blanks, etc.). The script
filters these out. To get those listings into the feed, fix the VINs upstream
in CWS.

## Local testing

```bash
python3 transform_feed.py
```

Outputs `feed.xml` in the current directory and prints the inclusion/exclusion
breakdown. No dependencies beyond Python 3.9+.

## Files

- `transform_feed.py` — the transformer
- `feed.xml` — the live Google Merchant Center feed (auto-updated by Actions)
- `.github/workflows/update-feed.yml` — the daily cron job
