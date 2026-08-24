#!/usr/bin/env python3
"""
Auction123 CSV feed -> Google Merchant Center Products feed.

Fetches the Auction123 inventory export from the dealer website (CSV format),
filters out items ineligible for Google ads, and remaps each vehicle into
Google's product feed format (RSS 2.0 with the g: namespace). Vehicle-specific
attributes are included for forward-compatibility with Vehicle Ads.

Uses only the Python standard library. No pip installs needed.
"""

from __future__ import annotations

import csv
import html
import io
import re
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from xml.etree import ElementTree as ET

# ----------------------------- CONFIG --------------------------------------

SOURCE_FEED_URL = "https://www.netrucksolutions.com/feeds.asp?feed=Auction123Feedv2"

# Google Business Profile store codes, keyed by the city in the feed's
# "Location" column (lowercased). NOTE: the feed says "North Smithfield, MA"
# even though the location is actually in RI, so we match on city name only.
STORE_CODE_BY_CITY = {
    "north smithfield": "NETRUCK_RI",
    "avon": "NETRUCK_MA",
}
DEFAULT_STORE_CODE = "NETRUCK_RI"

# Restrict the feed to a single location to avoid the Merchant Center
# "multistate offers" policy violation. Set to None to include all locations.
RESTRICT_TO_CITY = "north smithfield"

CURRENCY = "USD"
OUTPUT_PATH = Path("feed.xml")

CHANNEL_TITLE = "Truck Solutions Vehicle Inventory"
CHANNEL_LINK = "https://netrucksolutions.com"
CHANNEL_DESCRIPTION = "New and used commercial truck inventory for Google Merchant Center."

GOOGLE_PRODUCT_CATEGORY = "Vehicles & Parts > Vehicles > Motor Vehicles > Cars, Trucks & Vans"

# ----------------------------- FILTERS -------------------------------------

# Categories that aren't a complete vehicle. Matched as substrings against the
# lowercased Category column.
EXCLUDED_CATEGORY_SUBSTRINGS = (
    "bodies only",
    "body only",
)

VIN_RE = re.compile(r"^[A-HJ-NPR-Z0-9]{17}$")

TAG_RE = re.compile(r"<[^>]+>")


# ----------------------------- HELPERS -------------------------------------


def clean_text(s: str) -> str:
    """Unescape HTML entities and strip HTML tags from feed text."""
    if not s:
        return ""
    s = html.unescape(s)
    s = TAG_RE.sub(" ", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def first_int(s: str) -> Optional[int]:
    """Pull the first integer out of strings like '221603 mi' or '122890'."""
    if not s:
        return None
    m = re.search(r"\d[\d,]*", s)
    if not m:
        return None
    return int(m.group(0).replace(",", ""))


def first_float(s: str) -> Optional[float]:
    if not s:
        return None
    cleaned = re.sub(r"[^\d.]", "", s)
    if not cleaned or cleaned == ".":
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def map_condition(raw: str) -> str:
    r = (raw or "").strip().lower()
    return "new" if r == "new" else "used"


def city_from_location(location: str) -> str:
    """'North Smithfield, MA' -> 'north smithfield'. State is unreliable."""
    if not location:
        return ""
    return location.split(",")[0].strip().lower()


def store_code_for(location: str) -> str:
    return STORE_CODE_BY_CITY.get(city_from_location(location), DEFAULT_STORE_CODE)


def build_link_template(listing_url: str) -> str:
    """Listing URL + Google's required {store_code} ValueTrack parameter."""
    if not listing_url:
        return ""
    separator = "&" if "?" in listing_url else "?"
    return f"{listing_url}{separator}store={{store_code}}"


def normalize_color(raw: str) -> str:
    """'WHITE' -> 'White'. Empty -> 'Unspecified'."""
    c = (raw or "").strip()
    if not c:
        return "Unspecified"
    return c.title()


def is_eligible(row: dict, seen_vins: set) -> tuple[bool, str]:
    category = (row.get("Category") or "").lower()
    if any(sub in category for sub in EXCLUDED_CATEGORY_SUBSTRINGS):
        return False, f"excluded category: {category}"

    if RESTRICT_TO_CITY:
        city = city_from_location(row.get("Location") or "")
        if city != RESTRICT_TO_CITY.lower():
            return False, f"location filtered (not {RESTRICT_TO_CITY})"

    vin = (row.get("VIN") or "").strip().upper()
    if not vin:
        return False, "missing VIN"
    if not VIN_RE.match(vin):
        return False, f"invalid VIN format: {vin!r}"
    if vin in seen_vins:
        return False, f"duplicate VIN: {vin}"

    price = first_float(row.get("SellingPrice") or "")
    if price is None or price <= 0:
        return False, "no price (Call for Price)"

    year = first_int(row.get("Year") or "")
    if not year or year < 1981 or year > datetime.now().year + 2:
        return False, f"invalid year: {year}"

    if not (row.get("Detail-Page-URL") or "").strip():
        return False, "missing detail page URL"

    return True, ""


def build_item(row: dict) -> ET.Element:
    NS = "{http://base.google.com/ns/1.0}"
    item = ET.Element("item")

    vin = (row.get("VIN") or "").strip().upper()
    year = (row.get("Year") or "").strip()
    make_raw = (row.get("Make") or "").strip()
    make = make_raw.title() if make_raw.isupper() else make_raw
    model = (row.get("Model") or "").strip()
    condition = map_condition(row.get("Type") or "")
    price = first_float(row.get("SellingPrice") or "")
    miles = first_int(row.get("Miles") or "") or 0
    category = (row.get("Category") or "").strip()
    location = (row.get("Location") or "").strip()
    listing_url = (row.get("Detail-Page-URL") or "").strip()
    color = normalize_color(row.get("ExteriorColor") or "")

    description = clean_text(row.get("Description") or "")
    if not description:
        description = f"{year} {make} {model}"

    # Title: "New 2026 Hino L6 Box Trucks - Cargo / Straight" style, trimmed
    parts = []
    if condition == "new":
        parts.append("New")
    parts.extend(p for p in (year, make, model) if p)
    if category and category.lower() not in " ".join(parts).lower():
        parts.append(category)
    title = re.sub(r"\s+", " ", " ".join(parts)).strip()[:150]

    # ----- CORE PRODUCT FIELDS -----
    ET.SubElement(item, f"{NS}id").text = vin
    ET.SubElement(item, "title").text = title
    ET.SubElement(item, "description").text = description[:5000]
    ET.SubElement(item, "link").text = listing_url
    ET.SubElement(item, f"{NS}link_template").text = build_link_template(listing_url)
    ET.SubElement(item, f"{NS}condition").text = condition
    ET.SubElement(item, f"{NS}price").text = f"{price:.2f} {CURRENCY}"
    ET.SubElement(item, f"{NS}availability").text = "in stock"

    if make:
        ET.SubElement(item, f"{NS}brand").text = make

    ET.SubElement(item, f"{NS}identifier_exists").text = "no"
    ET.SubElement(item, f"{NS}google_product_category").text = GOOGLE_PRODUCT_CATEGORY
    ET.SubElement(item, f"{NS}color").text = color

    if category:
        ET.SubElement(item, f"{NS}product_type").text = (
            f"Commercial Trucks > {category.replace(' - ', ' > ')}"
        )

    # ----- VEHICLE-SPECIFIC ATTRIBUTES (future-proofed for Vehicle Ads) -----
    ET.SubElement(item, f"{NS}vin").text = vin
    ET.SubElement(item, f"{NS}year").text = year
    ET.SubElement(item, f"{NS}make").text = make
    ET.SubElement(item, f"{NS}model").text = model
    ET.SubElement(item, f"{NS}mileage").text = f"{miles} miles"
    ET.SubElement(item, f"{NS}store_code").text = store_code_for(location)

    # MSRP — only when the feed's MSRP column has a real value
    msrp = first_float(row.get("MSRP") or "")
    if msrp is not None and msrp > 0:
        ET.SubElement(item, f"{NS}vehicle_msrp").text = f"{msrp:.2f} {CURRENCY}"

    # ----- IMAGES (PhotoURLs is a single comma-separated field) -----
    photo_urls = [u.strip() for u in (row.get("PhotoURLs") or "").split(",") if u.strip()]
    if photo_urls:
        ET.SubElement(item, f"{NS}image_link").text = photo_urls[0]
        for extra in photo_urls[1:11]:
            ET.SubElement(item, f"{NS}additional_image_link").text = extra

    return item


def fetch_source(url: str) -> str:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 (compatible; feed-transformer/2.0)"},
    )
    with urllib.request.urlopen(req, timeout=90) as resp:
        raw = resp.read()
    # Auction123 feeds are typically UTF-8 or Windows-1252
    for enc in ("utf-8-sig", "utf-8", "cp1252"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def build_feed(source_csv: str) -> tuple[ET.ElementTree, dict]:
    reader = csv.DictReader(io.StringIO(source_csv))

    ET.register_namespace("g", "http://base.google.com/ns/1.0")
    rss = ET.Element("rss", {"version": "2.0"})
    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, "title").text = CHANNEL_TITLE
    ET.SubElement(channel, "link").text = CHANNEL_LINK
    ET.SubElement(channel, "description").text = CHANNEL_DESCRIPTION
    ET.SubElement(channel, "lastBuildDate").text = datetime.now(timezone.utc).strftime(
        "%a, %d %b %Y %H:%M:%S +0000"
    )

    stats = {"total": 0, "included": 0, "excluded": 0, "reasons": {}, "colors": {}}
    seen_vins: set = set()

    for row in reader:
        # Skip blank rows (some exports have empty lines between records)
        if not any((v or "").strip() for v in row.values()):
            continue
        stats["total"] += 1

        ok, reason = is_eligible(row, seen_vins)
        if not ok:
            stats["excluded"] += 1
            stats["reasons"][reason] = stats["reasons"].get(reason, 0) + 1
            continue

        vin = (row.get("VIN") or "").strip().upper()
        seen_vins.add(vin)

        item = build_item(row)
        channel.append(item)
        stats["included"] += 1

        color_el = item.find("{http://base.google.com/ns/1.0}color")
        if color_el is not None:
            stats["colors"][color_el.text] = stats["colors"].get(color_el.text, 0) + 1

    return ET.ElementTree(rss), stats


def main() -> int:
    print(f"Fetching {SOURCE_FEED_URL} ...")
    try:
        source = fetch_source(SOURCE_FEED_URL)
    except Exception as e:
        print(f"ERROR: could not fetch source feed: {e}", file=sys.stderr)
        return 1

    print(f"Fetched {len(source):,} characters. Transforming ...")
    tree, stats = build_feed(source)

    ET.indent(tree, space="  ")
    tree.write(OUTPUT_PATH, encoding="utf-8", xml_declaration=True)

    print(f"Wrote {OUTPUT_PATH} ({OUTPUT_PATH.stat().st_size:,} bytes)")
    print(f"  Total listings:    {stats['total']}")
    print(f"  Included in feed:  {stats['included']}")
    print(f"  Excluded:          {stats['excluded']}")
    if stats["reasons"]:
        print("  Exclusion breakdown:")
        for reason, n in sorted(stats["reasons"].items(), key=lambda x: -x[1]):
            print(f"    {n:>4}  {reason}")
    if stats["colors"]:
        print("  Colors:")
        for color, n in sorted(stats["colors"].items(), key=lambda x: -x[1]):
            print(f"    {n:>4}  {color}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
