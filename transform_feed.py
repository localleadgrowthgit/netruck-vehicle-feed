#!/usr/bin/env python3
"""
CWS Platform -> Google Merchant Center Vehicle Listings transformer.

Fetches the CWS XML inventory export, filters out items that are ineligible
for Google Vehicle Ads, remaps each remaining vehicle into Google's required
RSS 2.0 vehicle listings format, and writes feed.xml.

Uses only the Python standard library. No pip installs needed.
"""

from __future__ import annotations

import re
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from xml.etree import ElementTree as ET

# ----------------------------- CONFIG --------------------------------------

SOURCE_FEED_URL = "https://admin.cwsplatform.com/export/c71b77"

# Set these to the store_code values from your Google Business Profile.
# Vehicle Ads REQUIRES this and it must match the store_code on each GBP
# location exactly. Replace the placeholders below with the real codes.
STORE_CODE_BY_CITY = {
    "north smithfield": "NETRUCK_RI",
    "avon": "NETRUCK_MA",
}
DEFAULT_STORE_CODE = "NETRUCK_RI"

CURRENCY = "USD"
OUTPUT_PATH = Path("feed.xml")

CHANNEL_TITLE = "Truck Solutions Vehicle Inventory"
CHANNEL_LINK = "https://netrucksolutions.com"
CHANNEL_DESCRIPTION = "New and used commercial truck inventory feed for Google Vehicle Ads."

# ----------------------------- FILTERS -------------------------------------

EXCLUDED_CATEGORIES = {
    "dry van body only",
    "reefer/refrigerated body",
    "truck bodies only",
}

VIN_RE = re.compile(r"^[A-HJ-NPR-Z0-9]{17}$")


# ----------------------------- HELPERS -------------------------------------


def text(elem: Optional[ET.Element]) -> str:
    if elem is None or elem.text is None:
        return ""
    return elem.text.strip()


def first_int(s: str) -> Optional[int]:
    if not s:
        return None
    digits = re.sub(r"[^\d]", "", s.split(".")[0])
    return int(digits) if digits else None


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
    r = raw.strip().lower()
    if r == "new":
        return "new"
    if r == "used":
        return "used"
    return "used"


def store_code_for(city: str) -> str:
    return STORE_CODE_BY_CITY.get(city.strip().lower(), DEFAULT_STORE_CODE)


def is_vehicle_listing(listing: ET.Element) -> tuple[bool, str]:
    category = text(listing.find("category")).lower()
    if any(ex in category for ex in EXCLUDED_CATEGORIES):
        return False, f"excluded category: {category}"

    vin = text(listing.find("identification-number")).upper()
    if not vin:
        return False, "missing VIN"
    if not VIN_RE.match(vin):
        return False, f"invalid VIN format: {vin!r}"

    price_val = first_float(text(listing.find("price")))
    if price_val is None or price_val <= 0:
        return False, "no price (Request a Quote)"

    odo_type = text(listing.find("odometer-type")).lower()
    if odo_type and odo_type not in ("miles", "kilometers", "km"):
        return False, f"odometer in {odo_type}, not miles"

    year = first_int(text(listing.find("model-year")))
    if not year or year < 1981 or year > datetime.now().year + 2:
        return False, f"invalid year: {year}"

    return True, ""


def build_item(listing: ET.Element) -> ET.Element:
    NS = "{http://base.google.com/ns/1.0}"
    item = ET.Element("item")

    vin = text(listing.find("identification-number")).upper()
    year = text(listing.find("model-year"))
    make = text(listing.find("manufacturer"))
    model = text(listing.find("model"))
    condition = map_condition(text(listing.find("condition")))
    price = first_float(text(listing.find("price")))
    odometer = first_int(text(listing.find("odometer"))) or 0
    odo_type = text(listing.find("odometer-type")).lower() or "miles"
    description = text(listing.find("description-long")) or text(
        listing.find("description-short")
    ) or f"{year} {make} {model}"
    listing_url = text(listing.find("listing-url"))

    location = listing.find("location")
    city = text(location.find("city")) if location is not None else ""
    state = text(location.find("state")) if location is not None else ""
    postal = text(location.find("postal-code")) if location is not None else ""

    title = f"{year} {make} {model}".strip()
    title = re.sub(r"\s+", " ", title)[:150]

    ET.SubElement(item, f"{NS}id").text = vin
    ET.SubElement(item, "title").text = title
    ET.SubElement(item, "description").text = description[:5000]
    ET.SubElement(item, "link").text = listing_url
    ET.SubElement(item, f"{NS}condition").text = condition
    ET.SubElement(item, f"{NS}price").text = f"{price:.2f} {CURRENCY}"
    ET.SubElement(item, f"{NS}availability").text = "in stock"

    ET.SubElement(item, f"{NS}vehicle_fulfillment").text = "for_sale_online"
    ET.SubElement(item, f"{NS}vin").text = vin
    ET.SubElement(item, f"{NS}year").text = year
    ET.SubElement(item, f"{NS}make").text = make
    ET.SubElement(item, f"{NS}model").text = model
    ET.SubElement(item, f"{NS}mileage").text = f"{odometer} {'miles' if 'mile' in odo_type else 'km'}"

    ET.SubElement(item, f"{NS}store_code").text = store_code_for(city)

    if state and postal:
        addr = listing.find("location/address-1")
        addr1 = text(addr) if addr is not None else ""
        full_addr = f"{addr1}, {city}, {state} {postal}"
        ET.SubElement(item, f"{NS}vehicle_dealer_address").text = full_addr

    photos = listing.findall("listing-photos/photo/url")
    if photos:
        ET.SubElement(item, f"{NS}image_link").text = text(photos[0])
        for extra in photos[1:10]:
            url = text(extra)
            if url:
                ET.SubElement(item, f"{NS}additional_image_link").text = url

    return item


def fetch_source(url: str) -> bytes:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "feed-transformer/1.0 (+netrucksolutions.com)"},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read()


def build_feed(source_xml: bytes) -> tuple[ET.ElementTree, dict]:
    root = ET.fromstring(source_xml)
    listings = root.findall(".//listing")

    ET.register_namespace("g", "http://base.google.com/ns/1.0")
    rss = ET.Element("rss", {"version": "2.0"})
    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, "title").text = CHANNEL_TITLE
    ET.SubElement(channel, "link").text = CHANNEL_LINK
    ET.SubElement(channel, "description").text = CHANNEL_DESCRIPTION
    ET.SubElement(channel, "lastBuildDate").text = datetime.now(timezone.utc).strftime(
        "%a, %d %b %Y %H:%M:%S +0000"
    )

    stats = {"total": len(listings), "included": 0, "excluded": 0, "reasons": {}}

    for listing in listings:
        ok, reason = is_vehicle_listing(listing)
        if not ok:
            stats["excluded"] += 1
            stats["reasons"][reason] = stats["reasons"].get(reason, 0) + 1
            continue
        channel.append(build_item(listing))
        stats["included"] += 1

    return ET.ElementTree(rss), stats


def main() -> int:
    print(f"Fetching {SOURCE_FEED_URL} ...")
    try:
        source = fetch_source(SOURCE_FEED_URL)
    except Exception as e:
        print(f"ERROR: could not fetch source feed: {e}", file=sys.stderr)
        return 1

    print(f"Fetched {len(source):,} bytes. Transforming ...")
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
