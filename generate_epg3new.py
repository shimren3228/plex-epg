import logging
import os
import sys
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

PLEX_TOKEN = os.getenv("PLEX_TOKEN", "rXEXyKCsBa855MFDRZzV").strip()
CLIENT_ID = os.getenv("PLEX_CLIENT_ID", "6439juv8dcadgfibnktjsa09").strip()

PLEX_EPG_BASE = "https://epg.provider.plex.tv"
PLEX_PLAY_BASE = "https://play.provider.plex.tv"
OUTPUT_FILE = os.path.join(os.getcwd(), "plex_epg.xml")
DAYS_TO_FETCH = 2
MAX_WORKERS = 2
REQUEST_DELAY = 0.35
MAX_RETRIES = 5


def create_session():
    session = requests.Session()
    session.headers.update({
        "Accept": "application/xml",
        "User-Agent": "Mozilla/5.0",
        "X-Plex-Token": PLEX_TOKEN,
        "X-Plex-Client-Identifier": CLIENT_ID,
        "X-Plex-Product": "Plex Web",
        "X-Plex-Version": "4.163.0",
    })
    return session


def unix_to_xmltv(ts):
    try:
        return datetime.fromtimestamp(int(float(ts)), tz=timezone.utc).strftime("%Y%m%d%H%M%S") + " +0000"
    except Exception:
        return None


def get_with_retry(session, url, params=None):
    for attempt in range(MAX_RETRIES):
        time.sleep(REQUEST_DELAY)
        resp = session.get(url, params=params, timeout=30)
        if resp.status_code == 429:
            retry_after = int(resp.headers.get("Retry-After", 5 * (attempt + 1)))
            logging.warning(f"Rate limited (429). Sleeping {retry_after}s before retry...")
            time.sleep(retry_after)
            continue
        return resp
    return resp


def discover_lineup_paths(session):
    """Discovers current genre/filter channel lineup endpoints from Plex's provider XML."""
    fallback_paths = [
        "/lineups/plex/channels?genre=featured",
        "/lineups/plex/channels?genre=bingeworthy",
        "/lineups/plex/channels?genre=movies",
        "/lineups/plex/channels?genre=true-crime",
        "/lineups/plex/channels?genre=news",
        "/lineups/plex/channels?genre=sports",
        "/lineups/plex/channels?genre=reality",
        "/lineups/plex/channels?genre=classics",
        "/lineups/plex/channels?genre=adrenaline-sci-fi",
        "/lineups/plex/channels?genre=comedy",
        "/lineups/plex/channels?genre=daytime-tv-games",
        "/lineups/plex/channels?genre=explore",
        "/lineups/plex/channels?genre=food-home-culture",
        "/lineups/plex/channels?genre=kids-family",
        "/lineups/plex/channels?genre=en-espanol",
        "/lineups/plex/channels?genre=global",
        "/lineups/plex/channels?genre=music",
    ]
    try:
        resp = get_with_retry(session, f"{PLEX_PLAY_BASE}/")
        if resp.status_code == 200 and "GridChannelFilter" in resp.text:
            root = ET.fromstring(resp.text)
            paths = []
            for elem in root.findall(".//*[@key]"):
                key = elem.get("key", "")
                if "/lineups/plex/channels" in key and key not in paths:
                    paths.append(key)
            if paths:
                logging.info(f"Discovered {len(paths)} live channel filter endpoints from Plex.")
                return paths
    except Exception as exc:
        logging.warning(f"Could not auto-discover filters ({exc}); using built-in genre list.")

    return fallback_paths


def fetch_channels(session):
    logging.info("Fetching Plex Live TV channel lineup across categories...")
    paths = discover_lineup_paths(session)
    channels = {}

    for path in paths:
        url = f"{PLEX_EPG_BASE}{path}"
        resp = get_with_retry(session, url)
        if resp.status_code != 200 or "<Channel" not in resp.text:
            continue

        root = ET.fromstring(resp.text)
        for ch in root.findall(".//Channel"):
            grid_key = (ch.get("gridKey") or ch.get("id") or ch.get("key", "").split("/")[-1]).strip()
            slug = (ch.get("slug") or grid_key).strip()
            title = (ch.get("title") or ch.get("callSign") or "Unknown").strip()
            thumb = (ch.get("thumb") or "").strip()
            if grid_key and grid_key not in channels:
                channels[grid_key] = {
                    "id": slug,
                    "grid_key": grid_key,
                    "name": title,
                    "logo": thumb,
                }

    if not channels:
        raise RuntimeError(
            "No channels returned from epg.provider.plex.tv. "
            "Check if your PLEX_TOKEN is still valid."
        )

    logging.info(f"Found {len(channels)} unique channels.")
    return channels


def fetch_channel_date_grid(session, channel_info, date_str):
    params = {
        "channelGridKey": channel_info["grid_key"],
        "date": date_str,
    }
    resp = get_with_retry(session, f"{PLEX_EPG_BASE}/grid", params=params)
    if resp.status_code != 200:
        return []

    root = ET.fromstring(resp.text)
    programmes = []

    for video in root.findall(".//Video"):
        gp_title = (video.get("grandparentTitle") or "").strip()
        v_title = (video.get("title") or "").strip()
        title = gp_title or v_title
        sub_title = v_title if gp_title else ""
        desc = (video.get("summary") or "").strip()
        icon = (video.get("thumb") or video.get("grandparentThumb") or "").strip()

        for media in video.findall("Media"):
            s_xml = unix_to_xmltv(media.get("beginsAt"))
            e_xml = unix_to_xmltv(media.get("endsAt"))
            if title and s_xml and e_xml and s_xml < e_xml:
                programmes.append((channel_info["id"], s_xml, e_xml, title, sub_title, desc, icon))

    return programmes


def build_xmltv(channels, all_progs):
    tv = ET.Element("tv", {"generator-info-name": "Plex EPG", "generator-info-url": "https://watch.plex.tv"})
    for ch in channels.values():
        ce = ET.SubElement(tv, "channel", id=ch["id"])
        ET.SubElement(ce, "display-name").text = ch["name"]
        if ch["logo"]:
            ET.SubElement(ce, "icon", src=ch["logo"])

    seen = set()
    added = 0
    for cid, s_xml, e_xml, title, sub, desc, icon in sorted(all_progs, key=lambda x: (x[0], x[1])):
        key = (cid, s_xml, title)
        if key in seen:
            continue
        seen.add(key)
        added += 1
        pe = ET.SubElement(tv, "programme", start=s_xml, stop=e_xml, channel=cid)
        ET.SubElement(pe, "title", lang="en").text = title
        if sub and sub != title:
            ET.SubElement(pe, "sub-title", lang="en").text = sub
        if desc:
            ET.SubElement(pe, "desc", lang="en").text = desc
        if icon:
            ET.SubElement(pe, "icon", src=icon)

    logging.info(f"Built XMLTV: {len(channels)} channels, {added} programmes.")
    return ET.ElementTree(tv)


def main():
    try:
        session = create_session()
        channels = fetch_channels(session)

        today = datetime.now(timezone.utc).date()
        dates = [(today + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(DAYS_TO_FETCH)]
        tasks = [(ch, d) for ch in channels.values() for d in dates]
        all_progs = []

        logging.info(f"Fetching EPG schedules ({len(tasks)} requests)...")
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {
                executor.submit(fetch_channel_date_grid, session, ch, d): (ch["name"], d)
                for ch, d in tasks
            }
            for i, future in enumerate(as_completed(futures), 1):
                ch_name, d_str = futures[future]
                try:
                    all_progs.extend(future.result())
                except Exception as exc:
                    logging.warning(f"Failed grid for {ch_name} ({d_str}): {exc}")
                if i % 50 == 0 or i == len(tasks):
                    logging.info(f"Progress: {i}/{len(tasks)} channels fetched...")

        tree = build_xmltv(channels, all_progs)
        ET.indent(tree, space="  ", level=0)

        tmp_file = f"{OUTPUT_FILE}.tmp"
        tree.write(tmp_file, encoding="UTF-8", xml_declaration=True)
        os.replace(tmp_file, OUTPUT_FILE)
        logging.info(f"Successfully updated {OUTPUT_FILE}")

    except Exception as e:
        logging.error(f"Failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
