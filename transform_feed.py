#!/usr/bin/env python3
"""
CWS Platform -> Google Merchant Center Products feed.

Fetches the CWS XML inventory export, filters out items that are ineligible
for Google ads, remaps each remaining vehicle into Google's product feed format
(RSS 2.0 with the g: namespace). Vehicle-specific attributes are also included
for forward-compatibility if the account is approved for Vehicle Ads.

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
STORE_CODE_BY_CITY = {
    "north smithfield": "NETRUCK_RI",
    "avon": "NETRUCK_MA",
}
DEFAULT_STORE_CODE = "NETRUCK_RI"

CURRENCY = "USD"
OUTPUT_PATH = Path("feed.xml")

CHANNEL_TITLE = "Truck Solutions Vehicle Inventory"
CHANNEL_LINK = "https://netrucksolutions.com"
CHANNEL_DESCRIPTION = "New and used commercial truck inventory for Google Merchant Center."

GOOGLE_PRODUCT_CATEGORY = "Vehicles & Parts > Vehicles > Motor Vehicles > Cars, Trucks & Vans"

# ----------------------------- FILTERS -------------------------------------

EXCLUDED_CATEGORIES = {
    "dry van body only",
    "reefer/refrigerated body",
    "truck bodies only",
}

VIN_RE = re.compile(r"^[A-HJ-NPR-Z0-9]{17}$")

COLOR_PATTERNS = [
    ("Off-White", r"\boff[\s-]?white\b"),
    ("Pearl White", r"\bpearl\s+white\b"),
    ("Snow White", r"\bsnow\s+white\b"),
    ("White", r"\bwhite\b"),
    ("Black", r"\bblack\b"),
    ("Silver", r"\bsilver\b"),
    ("Gray", r"\b(gr[ae]y)\b"),
    ("Red", r"\bred\b"),
    ("Blue", r"\bblue\b"),
    ("Green", r"\bgreen\b"),
    ("Yellow", r"\byellow\b"),
    ("Orange", r"\borange\b"),
    ("Brown", r"\bbrown\b"),
    ("Tan", r"\btan\b"),
    ("Beige", r"\bbeige\b"),
    ("Gold", r"\bgold\b"),
    ("Maroon", r"\bmaroon\b"),
    ("Burgundy", r"\bburgundy\b"),
]

COLOR_NEGATIVE_CONTEXT = re.compile(
    r"\b(interior|seat|seats|wheels?|aluminum|steel|trim|stripe|tape|liner|"
    r"floor|tank|fuel|hose|cable|wire|label|tag|sticker)\s+\w*\s*$",
    re.IGNORECASE,
)


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


def build_link_template(listing_url: str) -> str:
    """
    Build the link_template URL per Google's spec.

    Google requires the {store_code} ValueTrack parameter. We use each
    vehicle's specific listing URL as the base so deep-linking is preserved,
    appending ?store={store_code} (or &store={store_code} if the URL already
    has a query string).
    """
    if not listing_url:
        return ""
    separator = "&" if "?" in listing_url else "?"
    return f"{listing_url}{separator}store={{store_code}}"


def extract_color(description: str, short_desc: str = "") -> str:
    text_to_search = f"{short_desc} {description}".lower()
    if not text_to_search.strip():
        return "Unspecified"

    for color_label, pattern in COLOR_PATTERNS:
        for match in re.finditer(pattern, text_to_search, re.IGNORECASE):
            preceding = text_to_search[max(0, match.start() - 30):match.start()]
            if COLOR_NEGATIVE_CONTEXT.search(preceding):
                continue
            return color_label
    return "Unspecified"


def build_rich_title(year: str, make: str, model: str, category: str,
                     condition: str, listing: ET.Element) -> str:
    parts = []
    if condition.lower() == "new":
        parts.append("New")
    if year:
        parts.append(year)
    if make:
        parts.append(make.title() if make.isupper() else make)
    if model:
        parts.append(model)

    cat_clean = category.strip()
    if cat_clean and cat_clean.lower() not in (" ".join(parts).lower()):
        cat_clean = re.sub(r"\s*-\s*Straight Truck\s*$", "", cat_clean)
        parts.append(cat_clean)

    short_desc = text(listing.find("description-short"))
    if short_desc and len(" ".join(parts)) < 100:
        snippet = short_desc.split(".")[0].strip()
        if snippet and snippet.lower() not in " ".join(parts).lower():
            parts.append("-")
            parts.append(snippet)

    title = " ".join(parts)
    title = re.sub(r"\s+", " ", title).strip()
    return title[:150]


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
    make_raw = text(listing.find("manufacturer"))
    make = make_raw.title() if make_raw.isupper() else make_raw
    model = text(listing.find("model"))
    condition = map_condition(text(listing.find("condition")))
    price = first_float(text(listing.find("price")))
    odometer = first_int(text(listing.find("odometer"))) or 0
    odo_type = text(listing.find("odometer-type")).lower() or "miles"
    category = text(listing.find("category"))
    cat_type = text(listing.find("cat-type"))

    description_long = text(listing.find("description-long"))
    description_short = text(listing.find("description-short"))
    description = description_long or description_short or f"{year} {make} {model}"
    listing_url = text(listing.find("listing-url"))

    location = listing.find("location")
    city = text(location.find("city")) if location is not None else ""
    state = text(location.find("state")) if location is not None else ""
    postal = text(location.find("postal-code")) if location is not None else ""

    title = build_rich_title(year, make, model, category, condition, listing)
    color = extract_color(description_long, description_short)
    link_template = build_link_template(listing_url)

    # ----- CORE PRODUCT FIELDS -----
    ET.SubElement(item, f"{NS}id").text = vin
    ET.SubElement(item, "title").text = title
    ET.SubElement(item, "description").text = description[:5000]
    ET.SubElement(item, "link").text = listing_url
    if link_template:
        ET.SubElement(item, f"{NS}link_template").text = link_template
    ET.SubElement(item, f"{NS}condition").text = condition
    ET.SubElement(item, f"{NS}price").text = f"{price:.2f} {CURRENCY}"
    ET.SubElement(item, f"{NS}availability").text = "in stock"

    if make:
        ET.SubElement(item, f"{NS}brand").text = make

    ET.SubElement(item, f"{NS}identifier_exists").text = "no"
    ET.SubElement(item, f"{NS}google_product_category").text = GOOGLE_PRODUCT_CATEGORY
    ET.SubElement(item, f"{NS}color").text = color

    product_type = cat_type.split("|")[0] if "|" in cat_type else (cat_type or category)
    if product_type:
        product_type_clean = product_type.replace(" - ", " > ").replace("|", " > ")
        ET.SubElement(item, f"{NS}product_type").text = f"Commercial Trucks > {product_type_clean}"

    # ----- VEHICLE-SPECIFIC ATTRIBUTES (future-proofed for Vehicle Ads) -----
    # Note: g:vehicle_fulfillment intentionally omitted. It's a structured
    # sub-attribute (colon-delimited) per Google's spec, only meaningful when
    # the account is enrolled in Vehicle Ads. As a plain string in a products
    # feed it triggers an "invalid format for sub-attributes" error and
    # limits visibility, so we drop it. If/when Vehicle Ads is enabled, we'd
    # add it back with the correct structured value per the current spec.
    ET.SubElement(item, f"{NS}vin").text = vin
    ET.SubElement(item, f"{NS}year").text = year
    ET.SubElement(item, f"{NS}make").text = make
    ET.SubElement(item, f"{NS}model").text = model
    ET.SubElement(item, f"{NS}mileage").text = f"{odometer} {'miles' if 'mile' in odo_type else 'km'}"
    ET.SubElement(item, f"{NS}store_code").text = store_code_for(city)

    if state and postal and location is not None:
        addr = location.find("address-1")
        addr1 = text(addr) if addr is not None else ""
        full_addr = f"{addr1}, {city}, {state} {postal}".strip(", ")
        ET.SubElement(item, f"{NS}vehicle_dealer_address").text = full_addr

    # ----- IMAGES -----
    photos = listing.findall("listing-photos/photo/url")
    if photos:
        ET.SubElement(item, f"{NS}image_link").text = text(photos[0])
        for extra in photos[1:11]:
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

    stats = {"total": len(listings), "included": 0, "excluded": 0,
             "reasons": {}, "colors": {}}

    for listing in listings:
        ok, reason = is_vehicle_listing(listing)
        if not ok:
            stats["excluded"] += 1
            stats["reasons"][reason] = stats["reasons"].get(reason, 0) + 1
            continue
        item = build_item(listing)
        channel.append(item)
        stats["included"] += 1
        color_el = item.find("{http://base.google.com/ns/1.0}color")
        if color_el is not None:
            c = color_el.text
            stats["colors"][c] = stats["colors"].get(c, 0) + 1

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
    if stats["colors"]:
        print("  Color extraction:")
        for color, n in sorted(stats["colors"].items(), key=lambda x: -x[1]):
            print(f"    {n:>4}  {color}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
