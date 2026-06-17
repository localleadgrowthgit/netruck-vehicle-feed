#!/usr/bin/env python3
"""
CWS Platform -> Google Merchant Center Vehicle Listings Feed Transformer
=========================================================================
Fetches the dealer inventory XML from CWS Platform and converts it into a
Google Merchant Center "vehicle listings" TSV feed for Vehicle Ads.

Usage:
    python transform_feed.py                      # fetches from CWS_FEED_URL
    python transform_feed.py --input local.xml    # use a local XML file
    python transform_feed.py --output feed.txt

Configuration is at the top of this file (STORE_CODES, CWS_FEED_URL).

Outputs:
    vehicle_feed.txt      Tab-separated Google vehicle listings feed
    skipped_report.csv    Listings excluded from the feed, with reasons
"""

import argparse
import csv
import html
import io
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

# ----------------------------------------------------------------------------
# CONFIGURATION - EDIT THESE
# ----------------------------------------------------------------------------

# The CWS Platform export URL for this dealer
CWS_FEED_URL = "https://admin.cwsplatform.com/export/c71b77"

# Google Business Profile store codes, keyed by (city, state) of the listing
# location. These MUST match the store codes in the Business Profile linked
# to your Merchant Center account, or every item will be disapproved.
# Find them in Google Business Profile > (location) > Advanced settings > Store code.
STORE_CODES = {
    ("avon", "ma"): "REPLACE_WITH_AVON_STORE_CODE",
    ("north smithfield", "ri"): "REPLACE_WITH_NS_STORE_CODE",
}
DEFAULT_STORE_CODE = "REPLACE_WITH_DEFAULT_STORE_CODE"

# Maximum number of additional images per vehicle (Google allows up to 10)
MAX_ADDITIONAL_IMAGES = 10

# ----------------------------------------------------------------------------

VIN_RE = re.compile(r"^[A-HJ-NPR-Z0-9]{17}$")  # 17 chars, no I, O, Q

COLOR_WORDS = [
    "white", "black", "silver", "gray", "grey", "red", "blue", "green",
    "yellow", "orange", "brown", "tan", "beige", "gold", "maroon", "burgundy",
]

FEED_COLUMNS = [
    "id",
    "store_code",
    "vin",
    "title",
    "description",
    "brand",
    "model",
    "year",
    "mileage",
    "condition",
    "color",
    "price",
    "link",
    "image_link",
    "additional_image_link",
    "vehicle_option",
]


def text(node, tag, default=""):
    """Get stripped text of a child tag, handling CDATA whitespace."""
    el = node.find(tag)
    if el is None or el.text is None:
        return default
    return html.unescape(el.text).strip()


def clean_ws(s):
    return re.sub(r"\s+", " ", s or "").strip()


def parse_price(raw):
    """Parse price strings like '122890.', '0.00', '115000.00'. Returns float or None."""
    raw = (raw or "").replace(",", "").replace("$", "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        m = re.search(r"\d+(?:\.\d+)?", raw)
        return float(m.group()) if m else None


def guess_color(description):
    """Best-effort color extraction from the description text."""
    d = (description or "").lower()
    # 'Color: White' style first
    m = re.search(r"colou?r:\s*([a-z]+)", d)
    if m and m.group(1) in COLOR_WORDS:
        return m.group(1).capitalize()
    for c in COLOR_WORDS:
        if re.search(rf"\b{c}\b", d):
            return "Gray" if c == "grey" else c.capitalize()
    return ""


def build_options(listing):
    """Collect non-empty specifications as vehicle_option values."""
    opts = []
    specs = listing.find("specifications")
    if specs is None:
        return opts
    for spec in specs.findall("specification"):
        name = clean_ws(text(spec, "specification-name"))
        value = clean_ws(text(spec, "specification-value"))
        if not name or not value or name.isdigit():
            continue
        opts.append(f"{name}: {value}")
    return opts


def transform(xml_bytes):
    root = ET.fromstring(xml_bytes)
    rows, skipped = [], []
    seen_vins = set()

    for listing in root.iter("listing"):
        stock = text(listing, "stock-number")
        make = clean_ws(text(listing, "manufacturer"))
        model = clean_ws(text(listing, "model"))
        year = text(listing, "model-year")
        ident = text(listing, "identification-number").upper().replace(" ", "")
        label = f"{year} {make} {model}".strip()
        url = text(listing, "listing-url")
        condition_raw = text(listing, "condition").lower()
        price_val = parse_price(text(listing, "price"))
        desc = clean_ws(text(listing, "description-long")) or clean_ws(
            text(listing, "description-short")
        )

        def skip(reason):
            skipped.append({
                "stock_number": stock, "vehicle": label, "vin": ident,
                "price": price_val if price_val is not None else "",
                "reason": reason, "url": url,
            })

        # --- Validation / exclusions -------------------------------------
        if not VIN_RE.match(ident):
            skip("Missing or invalid VIN (17 chars required; bodies-only items can't be advertised)")
            continue
        if ident in seen_vins:
            skip("Duplicate VIN - already included in feed")
            continue
        if price_val is None or price_val <= 0:
            skip("No price / 'Request a Quote' - Google requires a real price > 0")
            continue
        if condition_raw not in ("new", "used"):
            skip(f"Unrecognized condition '{condition_raw}'")
            continue
        if not year.isdigit():
            skip("Missing model year")
            continue
        if not make or not model:
            skip("Missing make or model")
            continue

        photos = []
        photo_parent = listing.find("listing-photos")
        if photo_parent is not None:
            for p in photo_parent.findall("photo"):
                u = text(p, "url")
                if u:
                    photos.append(u)
        if not photos:
            skip("No photos - image_link is required")
            continue

        # --- Field mapping -------------------------------------------------
        odo_raw = text(listing, "odometer")
        odo_type = text(listing, "odometer-type").lower()
        try:
            odo_val = int(float(odo_raw)) if odo_raw else 0
        except ValueError:
            odo_val = 0
        if odo_type != "miles":
            # Hours / missing units: only safe to default for new vehicles
            if condition_raw == "new":
                odo_val = odo_val if odo_val < 10000 else 0
            else:
                skip(f"Odometer in '{odo_type or 'unknown'}' units on a used vehicle - mileage required")
                continue
        mileage = f"{odo_val} miles"

        loc = listing.find("location")
        city = clean_ws(text(loc, "city")).lower() if loc is not None else ""
        state = clean_ws(text(loc, "state")).lower() if loc is not None else ""
        store_code = STORE_CODES.get((city, state), DEFAULT_STORE_CODE)

        brand_clean = make.title() if make.isupper() else make
        title = f"{year} {brand_clean} {model}".strip()

        seen_vins.add(ident)
        rows.append({
            "id": ident,
            "store_code": store_code,
            "vin": ident,
            "title": title[:150],
            "description": desc[:5000],
            "brand": brand_clean,
            "model": model,
            "year": year,
            "mileage": mileage,
            "condition": condition_raw,
            "color": guess_color(desc),
            "price": f"{price_val:.2f} USD",
            "link": url,
            "image_link": photos[0],
            "additional_image_link": ",".join(photos[1:1 + MAX_ADDITIONAL_IMAGES]),
            "vehicle_option": ",".join(build_options(listing))[:1000],
        })

    return rows, skipped


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", help="Local XML file (otherwise fetches CWS_FEED_URL)")
    ap.add_argument("--output", default="vehicle_feed.txt")
    ap.add_argument("--skipped", default="skipped_report.csv")
    args = ap.parse_args()

    if args.input:
        with open(args.input, "rb") as f:
            xml_bytes = f.read()
    else:
        import requests
        print(f"Fetching {CWS_FEED_URL} ...")
        resp = requests.get(CWS_FEED_URL, timeout=60,
                            headers={"User-Agent": "DealerFeedBot/1.0"})
        resp.raise_for_status()
        xml_bytes = resp.content

    # CWS sometimes embeds <script> junk injected by browser extensions when
    # saved from a browser; strip empty script tags defensively.
    xml_text = xml_bytes.decode("utf-8", errors="replace")
    xml_text = re.sub(r"<script\b[^>]*/>|<script\b[^>]*>.*?</script>", "", xml_text, flags=re.S)
    rows, skipped = transform(xml_text.encode("utf-8"))

    with open(args.output, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FEED_COLUMNS, delimiter="\t",
                           quoting=csv.QUOTE_MINIMAL)
        w.writeheader()
        w.writerows(rows)

    with open(args.skipped, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["stock_number", "vehicle", "vin",
                                          "price", "reason", "url"])
        w.writeheader()
        w.writerows(skipped)

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(f"[{stamp}] Feed written: {args.output} ({len(rows)} vehicles included)")
    print(f"Skipped: {len(skipped)} listings -> see {args.skipped}")
    missing_color = sum(1 for r in rows if not r["color"])
    if missing_color:
        print(f"WARNING: {missing_color} included vehicles have no color "
              f"(Google requires 'color' for vehicle ads; add colors in CWS).")
    if any(r["store_code"].startswith("REPLACE_WITH") for r in rows):
        print("WARNING: store codes are still placeholders - edit STORE_CODES "
              "at the top of this script before uploading to Merchant Center.")
    if not rows:
        print("ERROR: no eligible vehicles - feed is empty.")
        sys.exit(1)


if __name__ == "__main__":
    main()
