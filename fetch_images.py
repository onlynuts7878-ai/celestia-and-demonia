#!/usr/bin/env python3
"""Download free-license images from Openverse, Wikimedia Commons or Smithsonian.

Providers
    openverse   (default) Openverse API - no key, medium resolution, widest range
    wikimedia   Wikimedia Commons API - no key, full-resolution originals
    smithsonian Smithsonian Open Access - free api.data.gov key, museum-grade files

Licenses (--license, comma separated)
    cc0, pdm                      public domain, no attribution needed
    by, by-sa, by-nd              Creative Commons Attribution variants
    by-nc, by-nc-sa, by-nc-nd     non-commercial variants
    all                           everything the provider exposes

Usage
    python fetch_images.py "medieval castle" --count 12
    python fetch_images.py "gothic cathedral" -n 8 --provider wikimedia --max-width 2400
    python fetch_images.py "portrait painting" -n 5 --license cc0,pdm,by
    python fetch_images.py "butterfly specimen" -n 5 --provider smithsonian --api-key KEY

Output
    <out>/01-title-slug.jpg ...   downloaded images
    <out>/credits.json            full metadata for every image
    <out>/CREDITS.md              provenance table + required attribution lines

Notes
    * CC0 / PDM images need no credit line; CC BY variants do. The script writes
      ready-to-paste attribution for every CC BY image it downloads.
    * Watermarked stock photos (Shutterstock, Getty, ...) are never fetched:
      removing their watermarks is copyright infringement.
    * Smithsonian needs a free key: https://api.data.gov/signup/ (set SI_API_KEY
      or pass --api-key). Without one, use --provider openverse --source smithsonian.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

OPENVERSE_API = "https://api.openverse.org/v1/images/"
WIKIMEDIA_API = "https://commons.wikimedia.org/w/api.php"
SMITHSONIAN_API = "https://api.si.edu/openaccess/api/v1.0/search"

# Wikimedia only serves these thumbnail widths to external clients; requesting
# anything else returns HTTP 429 (https://w.wiki/GHai).
WM_THUMB_STEPS = (120, 250, 330, 500, 960, 1280, 1920, 3840)

USER_AGENT = os.environ.get(
    "WEBRING_USER_AGENT",
    "webring-image-fetcher/1.1 (free-license image downloader; "
    "set WEBRING_USER_AGENT to add your contact URL)",
)
HEADERS = {"User-Agent": USER_AGENT, "Accept": "application/json"}

LICENSE_IDS = (
    "cc0", "pdm",
    "by", "by-sa", "by-nd",
    "by-nc", "by-nc-sa", "by-nc-nd",
)
ATTRIBUTION_LICENSES = {"by", "by-sa", "by-nd", "by-nc", "by-nc-sa", "by-nc-nd"}
NONCOMMERCIAL_LICENSES = {"by-nc", "by-nc-sa", "by-nc-nd"}


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def log(msg: str) -> None:
    print(msg, flush=True)


def api_get(url: str, retries: int = 4) -> dict:
    """GET JSON with retry/backoff for 429, 5xx and transient network errors."""
    for attempt in range(1, retries + 1):
        req = urllib.request.Request(url, headers=HEADERS)
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            retryable = exc.code == 429 or 500 <= exc.code < 600
            if retryable and attempt < retries:
                wait = int(exc.headers.get("Retry-After") or 3 * attempt)
                wait = max(1, min(wait, 15))
                log(f"  API returned {exc.code}, retrying in {wait}s...")
                time.sleep(wait)
                continue
            raise RuntimeError(f"API error {exc.code} for {url}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            if attempt < retries:
                time.sleep(2 * attempt)
                continue
            raise RuntimeError(f"request failed: {exc}") from exc
    raise RuntimeError("request failed")


def parse_licenses(value: str) -> set[str]:
    wanted = {part.strip().lower() for part in value.split(",") if part.strip()}
    if "all" in wanted:
        return set(LICENSE_IDS)
    unknown = wanted - set(LICENSE_IDS)
    if unknown:
        raise ValueError(
            f"unknown license(s): {', '.join(sorted(unknown))}. "
            f"Choose from: {', '.join(LICENSE_IDS)}, all"
        )
    return wanted


def slugify(text: str, fallback: str) -> str:
    text = (text or "").strip()
    text = re.sub(r"\.(jpe?g|png|gif|webp|tiff?|bmp|svg)$", "", text, flags=re.I)
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    text = re.sub(r"-{2,}", "-", text)[:60].strip("-")
    return text or fallback


def strip_html(value: str) -> str:
    text = re.sub(r"<[^>]+>", " ", value or "")
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def sniff_extension(head: bytes) -> str | None:
    if head.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if head.startswith(b"GIF8"):
        return ".gif"
    if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        return ".webp"
    if head[4:12] in (b"ftypavif", b"ftypavis"):
        return ".avif"
    if head.startswith(b"II*\x00") or head.startswith(b"MM\x00*"):
        return ".tif"
    if head.startswith(b"BM"):
        return ".bmp"
    return None


def fetch_image(url: str, stem: Path, max_bytes: int, retries: int = 4) -> Path:
    """Stream url to <stem><real extension>. Returns the written path."""
    for attempt in range(1, retries + 1):
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                length = resp.headers.get("Content-Length")
                if length and int(length) > max_bytes:
                    raise ValueError(f"too large ({int(length)} bytes)")
                head = resp.read(64)
                ext = sniff_extension(head)
                if ext is None:
                    raise ValueError("response is not an image")
                tmp = stem.with_name(stem.name + ".part")
                written = len(head)
                with open(tmp, "wb") as fh:
                    fh.write(head)
                    while True:
                        chunk = resp.read(256 * 1024)
                        if not chunk:
                            break
                        written += len(chunk)
                        if written > max_bytes:
                            fh.close()
                            tmp.unlink(missing_ok=True)
                            raise ValueError("too large")
                        fh.write(chunk)
                final = stem.with_suffix(ext)
                tmp.replace(final)
                return final
        except urllib.error.HTTPError as exc:
            if exc.code == 429 and attempt < retries:
                wait = int(exc.headers.get("Retry-After") or 0) or 8 * attempt
                time.sleep(max(3, min(wait, 30)))
                continue
            raise
        except (urllib.error.URLError, TimeoutError) as exc:
            if attempt < retries:
                time.sleep(2 * attempt)
                continue
            raise RuntimeError(str(exc)) from exc
    raise RuntimeError("download failed")


def base_record(**kwargs) -> dict:
    record = {
        "title": None,
        "creator": None,
        "creator_url": None,
        "license": None,
        "license_name": None,
        "license_url": None,
        "source": None,
        "foreign_landing_url": None,
        "original_url": None,
        "width": 0,
        "height": 0,
        "attribution": None,
    }
    record.update(kwargs)
    return record


# --------------------------------------------------------------------------- #
# Openverse
# --------------------------------------------------------------------------- #

def search_openverse(query: str, want: int, licenses: set[str], orientation: str | None,
                     source: str | None, max_pages: int) -> list[dict]:
    found: list[dict] = []
    seen: set[str] = set()
    page = 1
    while len(found) < want and page <= max_pages:
        params = {
            "q": query,
            "license": ",".join(sorted(licenses)),
            "page": page,
            "page_size": 20,
        }
        if orientation:
            params["orientation"] = orientation
        if source:
            params["source"] = source
        data = api_get(OPENVERSE_API + "?" + urllib.parse.urlencode(params))
        results = data.get("results") or []
        if not results:
            break
        for item in results:
            if len(found) >= want:
                break
            uid = item.get("id") or item.get("url")
            if not uid or uid in seen:
                continue
            lic = (item.get("license") or "").lower()
            if lic not in licenses:
                continue
            if item.get("mature") or item.get("unstable__sensitivity"):
                continue
            if not item.get("url"):
                continue
            seen.add(uid)
            found.append(base_record(
                title=item.get("title"),
                creator=item.get("creator"),
                creator_url=item.get("creator_url"),
                license=lic,
                license_name=(lic.upper() if lic in ("cc0", "pdm") else f"CC {lic.upper()}"),
                license_url=item.get("license_url"),
                source=item.get("source") or "openverse",
                foreign_landing_url=item.get("foreign_landing_url"),
                original_url=item.get("url"),
                width=item.get("width") or 0,
                height=item.get("height") or 0,
                attribution=item.get("attribution"),
            ))
        page_count = data.get("page_count") or 0
        if page >= page_count:
            break
        page += 1
        time.sleep(0.4)
    return found


# --------------------------------------------------------------------------- #
# Wikimedia Commons
# --------------------------------------------------------------------------- #

def snap_wikimedia_width(width: int) -> int:
    """Round a requested width down to a Wikimedia-served thumbnail step."""
    best = WM_THUMB_STEPS[0]
    for step in WM_THUMB_STEPS:
        if step <= width:
            best = step
        else:
            break
    return best


def wikimedia_license(extmeta: dict) -> tuple[str | None, str | None, str | None]:
    """Return (canonical id, short name, license url) from Commons extmetadata."""
    def val(key: str) -> str:
        entry = extmeta.get(key) or {}
        return str(entry.get("value") or "").strip()

    short = val("LicenseShortName")
    raw = val("License")
    lic_url = val("LicenseUrl") or None
    probe = f"{raw} {short}".lower().replace("_", "-")

    if "cc0" in probe or "cc-zero" in probe:
        return "cc0", short or "CC0", lic_url or "https://creativecommons.org/publicdomain/zero/1.0/"
    if "public domain" in probe or raw.lower() in ("pd", "pdm") or "pdm" in probe:
        return "pdm", short or "Public domain", lic_url or "https://creativecommons.org/publicdomain/mark/1.0/"

    match = re.search(r"cc[\s-]?by(?:[\s-](nc))?(?:[\s-](sa|nd))?", probe)
    if match:
        nc, kind = match.group(1), match.group(2)
        canonical = "by"
        if nc:
            canonical += "-nc"
        if kind:
            canonical += f"-{kind}"
        if canonical not in LICENSE_IDS:
            return None, short or None, lic_url
        return canonical, short or canonical.upper(), lic_url
    return None, short or None, lic_url


def search_wikimedia(query: str, want: int, licenses: set[str],
                     max_width: int, max_pages: int) -> list[dict]:
    found: list[dict] = []
    seen: set[str] = set()
    offset = 0
    for _ in range(max_pages):
        if len(found) >= want:
            break
        params = {
            "action": "query",
            "format": "json",
            "formatversion": 2,
            "generator": "search",
            "gsrsearch": f"filetype:bitmap {query}",
            "gsrnamespace": 6,
            "gsrlimit": 50,
            "prop": "imageinfo",
            "iiprop": "url|size|mime|extmetadata",
            "iiurlwidth": snap_wikimedia_width(max_width),
        }
        if offset:
            params["gsroffset"] = offset
        data = api_get(WIKIMEDIA_API + "?" + urllib.parse.urlencode(params))
        pages = ((data.get("query") or {}).get("pages")) or []
        if not pages:
            break
        for page in pages:
            if len(found) >= want:
                break
            info_list = page.get("imageinfo") or []
            if not info_list:
                continue
            info = info_list[0]
            mime = (info.get("mime") or "").lower()
            if not mime.startswith("image/") or "svg" in mime:
                continue
            title = page.get("title") or ""
            if title in seen:
                continue
            extmeta = info.get("extmetadata") or {}
            canonical, short, lic_url = wikimedia_license(extmeta)
            if not canonical or canonical not in licenses:
                continue

            url = info.get("url")
            thumb = info.get("thumburl")
            width = info.get("width") or 0
            height = info.get("height") or 0
            if thumb and width > max_width:
                url = thumb
                width = info.get("thumbwidth") or width
                height = info.get("thumbheight") or height
            if not url:
                continue

            artist = strip_html(str((extmeta.get("Artist") or {}).get("value") or ""))
            page_url = info.get("descriptionurl") or (
                "https://commons.wikimedia.org/wiki/" + urllib.parse.quote(title.replace(" ", "_"))
            )
            seen.add(title)
            found.append(base_record(
                title=title[5:] if title.lower().startswith("file:") else title,
                creator=artist or "Unknown",
                creator_url=None,
                license=canonical,
                license_name=short or canonical.upper(),
                license_url=lic_url,
                source="wikimedia",
                foreign_landing_url=page_url,
                original_url=url,
                width=width,
                height=height,
            ))
        cont = data.get("continue") or {}
        offset = cont.get("gsroffset")
        if not offset:
            break
        time.sleep(0.4)
    return found


# --------------------------------------------------------------------------- #
# Smithsonian Open Access
# --------------------------------------------------------------------------- #

def si_absolute(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return url
    if url.startswith("//"):
        return "https:" + url
    if not re.match(r"^https?://", url):
        return "https://" + url
    return url


def si_pick_media(media: dict, max_width: int) -> tuple[str, int, int]:
    """Pick the best JPEG URL for a Smithsonian media entry."""
    candidates: list[tuple[str, int, int]] = []
    for res in media.get("resources") or []:
        label = (res.get("label") or "").lower()
        if "jpeg" in label or "jpg" in label:
            candidates.append((
                si_absolute(res.get("url") or ""),
                int(res.get("width") or 0),
                int(res.get("height") or 0),
            ))
    candidates = [c for c in candidates if c[0]]
    # Prefer the largest candidate that stays within max_width.
    candidates.sort(key=lambda c: c[1], reverse=True)
    for url, width, height in candidates:
        if not width or width <= max_width:
            return url, width, height
    fallback = si_absolute(media.get("content") or "")
    return fallback, int(media.get("width") or 0), int(media.get("height") or 0)


def search_smithsonian(query: str, want: int, licenses: set[str],
                       max_width: int, api_key: str, max_pages: int) -> list[dict]:
    if "cc0" not in licenses:
        log("Smithsonian Open Access only publishes CC0 media; add cc0 to --license.")
        return []
    q = query if "online_media_type:" in query else f"{query} online_media_type:Images"
    found: list[dict] = []
    seen: set[str] = set()
    start = 0
    for _ in range(max_pages):
        if len(found) >= want:
            break
        params = {
            "q": q,
            "api_key": api_key,
            "rows": 50,
            "start": start,
        }
        data = api_get(SMITHSONIAN_API + "?" + urllib.parse.urlencode(params))
        rows = ((data.get("response") or {}).get("rows")) or []
        if not rows:
            break
        for row in rows:
            if len(found) >= want:
                break
            dnr = ((row.get("content") or {}).get("descriptiveNonRepeating")) or {}
            if ((dnr.get("metadata_usage") or {}).get("access") or "").upper() != "CC0":
                continue
            media_list = (dnr.get("online_media") or {}).get("media") or []
            for media in media_list:
                if (media.get("type") or "").lower() != "images":
                    continue
                if ((media.get("usage") or {}).get("access") or "").upper() != "CC0":
                    continue
                media_id = media.get("id") or media.get("idsId") or ""
                if not media_id or media_id in seen:
                    continue
                url, width, height = si_pick_media(media, max_width)
                if not url:
                    continue
                seen.add(media_id)
                title = row.get("title") or (dnr.get("title") or {}).get("content") or "Untitled"
                names = ((row.get("content") or {}).get("freetext") or {}).get("name") or []
                creator = names[0].get("content") if names else None
                page_url = si_absolute(dnr.get("guid") or dnr.get("record_link") or "")
                if not page_url:
                    page_url = f"https://www.si.edu/object/{dnr.get('record_ID', '')}"
                found.append(base_record(
                    title=title,
                    creator=creator or "Smithsonian Institution",
                    creator_url="https://www.si.edu",
                    license="cc0",
                    license_name="CC0",
                    license_url="https://creativecommons.org/publicdomain/zero/1.0/",
                    source="smithsonian",
                    foreign_landing_url=page_url,
                    original_url=url,
                    width=width,
                    height=height,
                ))
        total = int((data.get("response") or {}).get("rowCount") or 0)
        start += len(rows)
        if start >= total or not rows:
            break
        time.sleep(0.4)
    return found


# --------------------------------------------------------------------------- #
# credits + main
# --------------------------------------------------------------------------- #

def build_attribution(rec: dict) -> str | None:
    if rec["license"] not in ATTRIBUTION_LICENSES:
        return None
    title = rec.get("title") or "Untitled"
    creator = rec.get("creator") or "Unknown"
    lic = rec.get("license_name") or rec["license"].upper()
    url = rec.get("license_url") or ""
    page = rec.get("foreign_landing_url") or ""
    parts = [f'"{title}" by {creator} is licensed under {lic}']
    if url:
        parts.append(f"({url})")
    if page:
        parts.append(f"source: {page}")
    return ". ".join(parts) + "."


def write_credits(out: Path, records: list[dict], provider: str, licenses: set[str]) -> None:
    for rec in records:
        rec["attribution"] = build_attribution(rec)
    (out / "credits.json").write_text(
        json.dumps(records, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    needs_credit = [r for r in records if r["attribution"]]
    lines = [
        "# Image credits",
        "",
        f"Provider: **{provider}**",
        f"Licenses: **{', '.join(sorted(licenses)).upper()}**",
        "",
        "CC0 and Public Domain Mark images are free for commercial use with no",
        "attribution requirement. CC BY variants do require credit: use the",
        "ready-made lines at the bottom of this file wherever the image appears.",
        "",
        "| File | Title | Creator | License | Source |",
        "| --- | --- | --- | --- | --- |",
    ]
    for rec in records:
        title = (rec.get("title") or "Untitled").replace("|", "\\|")
        creator = (rec.get("creator") or "Unknown").replace("|", "\\|")
        lic = rec.get("license_name") or (rec.get("license") or "").upper()
        page = rec.get("foreign_landing_url") or rec.get("original_url") or ""
        lines.append(f"| {rec['file']} | {title} | {creator} | {lic} | [link]({page}) |")

    if needs_credit:
        lines += ["", "## Required attribution", ""]
        for rec in needs_credit:
            lines.append(f"- **{rec['file']}** — {rec['attribution']}")

    (out / "CREDITS.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Download free-license images from Openverse, Wikimedia Commons or Smithsonian.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("query", help='search terms, e.g. "medieval castle"')
    parser.add_argument("-n", "--count", type=int, default=10, help="images to download (default 10)")
    parser.add_argument("-o", "--out", default="images", help="output directory (default ./images)")
    parser.add_argument("--provider", choices=["openverse", "wikimedia", "smithsonian"],
                        default="openverse", help="image source (default openverse)")
    parser.add_argument("--license", default="cc0,pdm",
                        help=f"comma list of: {', '.join(LICENSE_IDS)}, all (default cc0,pdm)")
    parser.add_argument("--orientation", choices=["landscape", "portrait", "square"],
                        help="preferred orientation (Openverse only)")
    parser.add_argument("--min-width", type=int, default=0, help="skip images narrower than this")
    parser.add_argument("--min-height", type=int, default=0, help="skip images shorter than this")
    parser.add_argument("--max-width", type=int, default=3000,
                        help="width cap for wikimedia/smithsonian (wikimedia rounds down "
                             "to standard sizes: 960/1280/1920/3840)")
    parser.add_argument("--source", help="Openverse provider filter, e.g. flickr, wikimedia, smithsonian")
    parser.add_argument("--api-key", default=os.environ.get("SI_API_KEY"),
                        help="Smithsonian api.data.gov key (or set SI_API_KEY)")
    parser.add_argument("--max-pages", type=int, default=10, help="search pages to scan (default 10)")
    parser.add_argument("--max-mb", type=float, default=30.0, help="per-file size cap in MB (default 30)")
    parser.add_argument("--delay", type=float, default=1.0, help="seconds between downloads (default 1.0)")
    parser.add_argument("--dry-run", action="store_true", help="search and print, download nothing")
    args = parser.parse_args()

    if args.count < 1:
        parser.error("--count must be at least 1")
    try:
        licenses = parse_licenses(args.license)
    except ValueError as exc:
        parser.error(str(exc))
    if args.source and args.provider != "openverse":
        parser.error("--source only applies to --provider openverse")
    if args.provider == "smithsonian" and not args.api_key:
        parser.error(
            "Smithsonian needs a free API key: https://api.data.gov/signup/ "
            "(pass --api-key KEY or set SI_API_KEY). "
            "Or use --provider openverse --source smithsonian."
        )

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    log(f'Searching {args.provider} for "{args.query}" ({", ".join(sorted(licenses))})...')
    try:
        if args.provider == "openverse":
            candidates = search_openverse(args.query, args.count * 2, licenses,
                                          args.orientation, args.source, args.max_pages)
        elif args.provider == "wikimedia":
            candidates = search_wikimedia(args.query, args.count * 2, licenses,
                                          args.max_width, args.max_pages)
        else:
            candidates = search_smithsonian(args.query, args.count * 2, licenses,
                                            args.max_width, args.api_key, args.max_pages)
    except RuntimeError as exc:
        log(f"Search failed: {exc}")
        if "429" in str(exc):
            log("Rate limited. A free personal key (https://api.data.gov/signup/) "
                "raises the Smithsonian quota; Openverse and Wikimedia need no key.")
        elif "401" in str(exc) or "403" in str(exc):
            log("The API rejected the key. Check --api-key / SI_API_KEY.")
        return 1

    if not candidates:
        log("No matching results. Try broader words, another provider, or a wider --license.")
        return 1

    records: list[dict] = []
    index = 0
    for item in candidates:
        if len(records) >= args.count:
            break
        width, height = item.get("width") or 0, item.get("height") or 0
        if width and width < args.min_width:
            continue
        if height and height < args.min_height:
            continue

        index += 1
        stem = out / f"{index:02d}-{slugify(item.get('title'), 'image')}"

        if args.dry_run:
            log(f"  [dry-run] {stem.name}  {width}x{height}  {item['license']}  "
                f"{item.get('foreign_landing_url')}")
            records.append({"file": stem.name, **item})
            continue

        log(f"  [{len(records) + 1}/{args.count}] {stem.name}  {width}x{height}  {item['license']}")
        try:
            path = fetch_image(item["original_url"], stem, int(args.max_mb * 1024 * 1024))
        except Exception as exc:  # one dead link must not abort the batch
            log(f"    skipped: {exc}")
            index -= 1
            continue

        records.append({"file": path.name, **item})
        time.sleep(max(args.delay, 1.5) if args.provider == "wikimedia" else args.delay)

    if not records:
        log("Nothing downloaded (all candidates were filtered out or failed).")
        return 1

    if args.dry_run:
        log(f"\nDry run: {len(records)} candidate(s) would be saved to {out.resolve()}")
        return 0

    write_credits(out, records, args.provider, licenses)
    log(f"\nDone: {len(records)} image(s) in {out.resolve()}")
    log("  credits.json / CREDITS.md written alongside them.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        raise SystemExit(130)
