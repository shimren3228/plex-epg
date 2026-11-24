import logging
import os
import sys
import time
import uuid
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# Uses `or` so an empty GitHub Secret safely falls back to your default values
RAW_TOKEN = (os.getenv("PLEX_TOKEN") or "rXEXyKCsBa855MFDRZzV").strip()
CLIENT_ID = (os.getenv("PLEX_CLIENT_ID") or "6439juv8dcadgfibnktjsa09").strip()

PLEX_EPG_BASE = "https://epg.provider.plex.tv"
PLEX_PLAY_BASE = "https://play.provider.plex.tv"
OUTPUT_FILE = os.path.join(os.getcwd(), "plex_epg.xml")
DAYS_TO_FETCH = 2
MAX_WORKERS = 2
REQUEST_DELAY = 0.35
MAX_RETRIES = 5


def base_plex_headers(client_id):
    return {
        "Accept": "application/xml",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "X-Plex-Product": "Plex Web",
        "X-Plex-Version": "4.163.0",
        "X-Plex-Client-Identifier": client_id,
        "X-Plex-Platform": "Chrome",
        "X-Plex-Platform-Version": "131.0",
        "X-Plex-Device": "Windows",
        "X-Plex-Device-Name": "Chrome",
        "X-Plex-Model": "hosted",
        "X-Plex-Features": "external-media,indirect-media,hub-style-list",
        "X-Plex-Provider-Version": "7.2",
        "X-Plex-Text-Format": "plain",
        "X-Plex-Language": "en",
    }


def fetch_anonymous_token():
    """Fetches a fresh temporary Plex Web token if the configured token is expired or missing."""
    new_client_id = str(uuid.uuid4())
    headers = base_plex_headers(new_client_id)
    headers["Accept"] = "application/json"
    logging.info("Requesting fresh anonymous Plex Web token...")
    resp = requests.post("https://plex.tv", headers=headers, timeout=30)
    resp.raise_for_status()
    token = resp.json().get("authToken")
    if not token:
        raise RuntimeError("Anonymous token response did not contain authToken.")
    return token, new_client_id


def create_session(token, client_id):
    session = requests.Session()
    headers = base_plex_headers(client_id)
    headers["X-Plex-Token"] = token
    session.headers.update(headers)
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
    fallback_paths = [
        "/lineups/plex/channels/featured",
        "/lineups/plex/channels?genre=bingeworthy",
        "/lineups/plex/channels?genre=movies",
        "/lineups/plex/channels?genre=crime",
        "/lineups/plex/channels?genre=news",
        "/lineups/plex/channels?genre=sports",
        "/lineups/plex/channels?genre=reality",
        "/lineups/plex/channels?genre=classic-tv",
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
    last_status = None

    for path in paths:
        url = f"{PLEX_EPG_BASE}{path}"
        resp = get_with_retry(session, url)
        last_status = resp.status_code
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

    return channels, last_status


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
        session = create_session(RAW_TOKEN, CLIENT_ID)
        channels, status = fetch_channels(session)

        # If configured token failed (401/403/empty), automatically grab a fresh guest token and retry
        if not channels:
            logging.warning(f"Initial token returned 0 channels (HTTP {status}). Falling back to fresh anonymous token...")
            fresh_token, fresh_cid = fetch_anonymous_token()
            session = create_session(fresh_token, fresh_cid)
            channels, status = fetch_channels(session)

        if not channels:
            raise RuntimeError(f"Still 0 channels after anonymous token fallback (HTTP {status}).")

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
