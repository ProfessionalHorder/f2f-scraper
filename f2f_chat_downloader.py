#!/usr/bin/env python3
"""
f2f_chat_downloader.py - download chat history (messages + media) from f2f.com

Usage:
    python f2f_chat_downloader.py -u daisypeach -c cookies.txt -o ./downloads

Output (under <outdir>/<username>/chat/):
    transcript.json     full message history, chronological, incl. raw API data
    transcript.txt      readable text transcript
    <media files>       downloaded images/videos, named
                        <datetime>_chat_<mediatype>_<uuid>.<ext>
    _unresolved_media.json  (only if some media could not be downloaded -
                        contains the raw API items so we can fix parsing)

Videos use the same proven pipeline: direct binary download, HLS segment
download + ffmpeg remux, yt-dlp as last resort.
Dependencies: pip install requests tqdm yt-dlp   (+ ffmpeg on PATH)
"""

import argparse
import glob
import json
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse, urljoin

import requests
from tqdm import tqdm

try:
    import yt_dlp
except ImportError:
    yt_dlp = None


API_BASE = "https://f2f.com/api"

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://f2f.com/",
}

VIDEO_EXTS = (".mp4", ".m3u8", ".mov", ".webm", ".mkv")
IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".gif")
PLAYABLE_VIDEO_EXTS = (".mp4", ".mov", ".webm", ".mkv")
VIDEO_MIN_BYTES = 50_000


# --------------------------------------------------------------------------
# generic helpers (same as the working scraper)
# --------------------------------------------------------------------------

def load_cookies(path):
    cookies = {}
    with path.open("r", encoding="utf-8") as fp:
        for line in fp:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) >= 7:
                cookies[parts[5]] = parts[6]
                continue
            if "=" in line:
                name, _, value = line.partition("=")
                cookies[name.strip()] = value.strip()
    print(f"[i] Loaded {len(cookies)} cookie(s): {', '.join(cookies.keys())}")
    return cookies


def _safe_json(resp):
    try:
        return resp.json()
    except ValueError:
        raise RuntimeError(
            f"API returned non-JSON for {resp.url!r}\n"
            f"status: {resp.status_code}\n"
            f"content-type: {resp.headers.get('content-type')}\n"
            f"body snippet: {resp.text[:200]!r}"
        )


def http_get(session, url, **kwargs):
    try:
        resp = session.get(url, timeout=20, **kwargs)
        resp.raise_for_status()
        return resp
    except requests.HTTPError as exc:
        if exc.response is not None and exc.response.status_code == 401:
            raise RuntimeError(
                "401 Unauthorized - session cookie missing or expired. "
                "Log in to f2f.com again and re-export cookies.txt"
            ) from exc
        raise RuntimeError(f"Error fetching {url}: {exc}") from exc
    except Exception as exc:
        raise RuntimeError(f"Error fetching {url}: {exc}") from exc


def download_file(session, url, dest):
    dest.parent.mkdir(parents=True, exist_ok=True)
    resp = http_get(session, url, stream=True)
    with dest.open("wb") as fp:
        for chunk in resp.iter_content(chunk_size=8192):
            fp.write(chunk)


def resolve_url(value):
    """Return a downloadable http(s) URL from a media field value (str or dict)."""
    if isinstance(value, str) and value.startswith("http"):
        return value
    if isinstance(value, dict):
        for key in ("mp4", "video", "url", "src", "file", "original",
                    "source", "link", "hls", "image", "thumbnail"):
            u = resolve_url(value.get(key))
            if u:
                return u
        for k, v in value.items():
            if isinstance(k, str) and k.lower() in (
                    "poster", "thumbnail", "preview", "blurhash"):
                continue
            u = resolve_url(v)
            if u:
                return u
    if isinstance(value, list):
        for v in value:
            u = resolve_url(v)
            if u:
                return u
    return None


def url_suffix(url):
    if not isinstance(url, str):
        return ""
    try:
        return Path(urlparse(url).path).suffix.lower()
    except Exception:
        return ""


def ext_from_url(url, media_type):
    if not isinstance(url, str):
        return ".jpg" if media_type == "image" else ".mp4"
    path = urlparse(url).path
    return Path(path).suffix or (".jpg" if media_type == "image" else ".mp4")


def sanitize(text, max_len=None):
    if not text:
        return ""
    text = re.sub(r'[\\/:*?"<>|\r\n\t]+', " ", str(text))
    text = re.sub(r"\s+", "_", text.strip())
    text = text.strip("._")
    if max_len:
        text = text[:max_len].rstrip("._")
    return text


def stamp_from_iso(iso):
    if not iso:
        return "nodate"
    try:
        d = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        return d.strftime("%Y-%m-%d_%H-%M-%S")
    except (ValueError, TypeError):
        return sanitize(str(iso), 25) or "nodate"


def find_existing_video(dest_stem):
    for p in dest_stem.parent.glob(dest_stem.name + ".*"):
        if p.suffix.lower() in PLAYABLE_VIDEO_EXTS and p.stat().st_size > VIDEO_MIN_BYTES:
            return p
    return None


# --------------------------------------------------------------------------
# video pipeline (proven working version)
# --------------------------------------------------------------------------

def _normalize_m3u8(text, base_url):
    out = []
    for line in text.splitlines():
        s = line.strip()
        if not s:
            out.append(line)
            continue
        if s.startswith("#"):
            m = re.search(r'URI="([^"]+)"', s)
            if m:
                abs_uri = urljoin(base_url, m.group(1))
                s = s.replace(m.group(1), abs_uri)
            out.append(s)
        else:
            out.append(urljoin(base_url, s))
    return "\n".join(out)


def _yt_dlp_download(url, dest_stem, cookies, referer):
    if yt_dlp is None:
        print("\n    [!] yt-dlp is not installed. Run: pip install -U yt-dlp")
        return None

    if not url.startswith("http"):
        url = Path(url).resolve().as_uri()

    cookie_header = "; ".join(f"{k}={v}" for k, v in cookies.items())
    headers = {
        "User-Agent": DEFAULT_HEADERS["User-Agent"],
        "Accept": "application/vnd.apple.mpegurl, application/x-mpegURL, "
                  "video/mp4, video/webm, video/*;q=0.9, */*;q=0.8",
        "Referer": referer,
        "Origin": "https://f2f.com",
        "Cookie": cookie_header,
    }
    base_opts = {
        "outtmpl": str(dest_stem) + ".%(ext)s",
        "http_headers": headers,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "enable_file_urls": True,
    }
    attempts = [
        {**base_opts, "format": "bv*+ba/b", "merge_output_format": "mp4"},
        {**base_opts, "format": "b"},
    ]
    for i, opts in enumerate(attempts, 1):
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                ret = ydl.download([url])
        except Exception as e:
            print(f"\n    [!] yt-dlp attempt {i} error: {e}")
            continue
        if ret == 0:
            matches = [
                Path(p) for p in sorted(glob.glob(str(dest_stem) + ".*"))
                if Path(p).suffix.lower() in PLAYABLE_VIDEO_EXTS
                and Path(p).stat().st_size > VIDEO_MIN_BYTES
            ]
            if matches:
                return matches[0]
        print(f"\n    [!] yt-dlp attempt {i} did not produce a playable file")
    return None


def _hls_download_direct(text, base_url, dest_stem, cookies, referer):
    if shutil.which("ffmpeg") is None:
        print("\n    [!] ffmpeg not found on PATH - cannot remux HLS segments")
        return None

    lines = [l.strip() for l in text.splitlines()]
    manifest_text = text
    manifest_url = base_url

    if "#EXT-X-STREAM-INF" in text:
        best_url, best_bw = None, -1
        for i, line in enumerate(lines):
            if line.startswith("#EXT-X-STREAM-INF"):
                m = re.search(r"BANDWIDTH=(\d+)", line)
                bw = int(m.group(1)) if m else 0
                if i + 1 < len(lines) and not lines[i + 1].startswith("#"):
                    u = urljoin(base_url, lines[i + 1])
                    if bw > best_bw:
                        best_bw, best_url = bw, u
        if not best_url:
            print("\n    [!] Master playlist contained no variant stream")
            return None
        s = requests.Session()
        s.headers.update({
            "User-Agent": DEFAULT_HEADERS["User-Agent"],
            "Accept": "*/*",
            "Referer": referer,
        })
        s.cookies.update(cookies)
        r = s.get(best_url, timeout=30)
        if r.status_code != 200:
            print(f"\n    [!] Variant playlist returned HTTP {r.status_code}")
            return None
        manifest_text = r.text
        manifest_url = best_url
        lines = [l.strip() for l in manifest_text.splitlines()]

    if "#EXT-X-KEY" in manifest_text:
        print("\n    [!] Playlist is encrypted - direct segment download will not work")
        return None

    segments = [urljoin(manifest_url, l) for l in lines
                if l and not l.startswith("#")]
    if not segments:
        print("\n    [!] No segments found in playlist")
        return None

    s = requests.Session()
    s.headers.update({
        "User-Agent": DEFAULT_HEADERS["User-Agent"],
        "Accept": "*/*",
        "Referer": referer,
    })
    s.cookies.update(cookies)

    print(f"\n    [i] Downloading {len(segments)} segment(s) directly ...")
    tmpdir = tempfile.mkdtemp(prefix="f2f_hls_")
    try:
        ts_path = Path(tmpdir) / "combined.ts"
        total = 0
        with ts_path.open("wb") as out:
            for n, seg_url in enumerate(segments, 1):
                try:
                    r = s.get(seg_url, timeout=30)
                    if r.status_code != 200:
                        print(f"\n    [!] segment {n}/{len(segments)} -> HTTP {r.status_code}")
                        continue
                    out.write(r.content)
                    total += len(r.content)
                except Exception as e:
                    print(f"\n    [!] segment {n}/{len(segments)} failed: {e}")
        if total < VIDEO_MIN_BYTES:
            print(f"\n    [!] Only {total} bytes downloaded - aborting")
            return None

        dest = dest_stem.parent / (dest_stem.name + ".mp4")
        dest.parent.mkdir(parents=True, exist_ok=True)
        cmd = ["ffmpeg", "-y", "-loglevel", "error",
               "-i", str(ts_path), "-c", "copy", str(dest)]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            print(f"\n    [!] ffmpeg remux failed: {proc.stderr[:300]}")
            return None
        if not dest.exists() or dest.stat().st_size < VIDEO_MIN_BYTES:
            print("\n    [!] ffmpeg output missing or too small")
            return None
        return dest
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def download_video(url, dest_stem, cookies, depth=0):
    """Download a video. dest_stem = path without extension. Returns Path or None."""
    if depth > 3:
        print("\n    [!] Too many redirects while resolving video URL")
        return None

    username = dest_stem.parent.name
    if username.lower() in ("chat", "stories"):
        username = dest_stem.parent.parent.name
    referer = f"https://f2f.com/{username}/" if username else "https://f2f.com/"

    s = requests.Session()
    s.headers.update({
        "User-Agent": DEFAULT_HEADERS["User-Agent"],
        "Accept": "*/*",
        "Referer": referer,
        "Origin": "https://f2f.com",
    })
    s.cookies.update(cookies)

    r = None
    for use_range in (True, False):
        extra_headers = {"Range": "bytes=0-"} if use_range else {}
        try:
            r = s.get(url, timeout=30, stream=True, allow_redirects=True,
                      headers=extra_headers)
        except Exception as e:
            print(f"\n    [!] Video request failed: {e}")
            return None
        if r.status_code != 416:
            break
        r.close()

    if r is None:
        return None

    if r.status_code >= 400:
        try:
            preview = r.text[:300]
        except Exception:
            preview = ""
        print(f"\n    [!] Video request failed HTTP {r.status_code}: {preview}")
        r.close()
        return None

    content_type = (r.headers.get("Content-Type") or "").lower()
    content_length = int(r.headers.get("Content-Length") or 0)

    try:
        first = next(r.iter_content(chunk_size=16384), b"")
    except Exception:
        first = b""

    if not first:
        print("\n    [!] Video response was empty")
        r.close()
        return None

    is_mp4 = b"ftyp" in first[:64] or b"styp" in first[:64]
    is_webm = first[:4] == b"\x1aE\xdf\xa3"
    looks_video_binary = is_mp4 or is_webm or content_type.startswith("video/")

    if looks_video_binary or (
        content_type.startswith("application/octet-stream")
        and content_length > VIDEO_MIN_BYTES
    ):
        if is_webm or "webm" in content_type:
            ext = ".webm"
        else:
            ext = url_suffix(r.url) or ".mp4"
        dest = dest_stem.parent / (dest_stem.name + ext)
        dest.parent.mkdir(parents=True, exist_ok=True)
        size = 0
        with dest.open("wb") as fp:
            fp.write(first)
            size += len(first)
            for chunk in r.iter_content(chunk_size=65536):
                fp.write(chunk)
                size += len(chunk)
        if size < VIDEO_MIN_BYTES:
            preview = first[:200].decode("utf-8", "replace")
            dest.unlink(missing_ok=True)
            print(f"\n    [!] Downloaded video too small ({size} bytes). "
                  f"Preview: {preview!r}")
            return None
        return dest

    rest = b"".join(r.iter_content(chunk_size=16384))
    body = first + rest
    text = body.decode("utf-8", "replace")

    if text.lstrip().upper().startswith("#EXTM3U") or "mpegurl" in content_type:
        print("\n    [i] Stream is HLS/M3U8; downloading segments directly ...")
        normalized = _normalize_m3u8(text, str(r.url))
        manifest_path = dest_stem.with_suffix(".m3u8")
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(normalized, encoding="utf-8")

        saved = _hls_download_direct(text, str(r.url), dest_stem, cookies, referer)
        if not saved:
            print("    [i] Direct download failed - falling back to yt-dlp ...")
            saved = _yt_dlp_download(str(manifest_path), dest_stem, cookies, referer)
        if saved:
            manifest_path.unlink(missing_ok=True)
            return saved
        print(f"    [!] Could not convert HLS. Manifest kept: {manifest_path}")
        return None

    if content_type.startswith("application/json") or text.lstrip()[:1] in ("{", "["):
        try:
            data = json.loads(text)
            extracted = resolve_url(data)
            if extracted and extracted != url:
                return download_video(extracted, dest_stem, cookies, depth + 1)
        except Exception:
            pass

    print("\n    [?] Video endpoint returned non-video content.")
    print(f"    final URL: {r.url}")
    print(f"    content-type: {content_type}")
    print(f"    preview: {text[:700]!r}")
    return None


# --------------------------------------------------------------------------
# chat API
# --------------------------------------------------------------------------

def fetch_all_chats(session):
    """Return the full list of chat objects (paginated)."""
    chats = []
    url = f"{API_BASE}/chats/"
    while url:
        js = _safe_json(http_get(session, url))
        chats.extend(js.get("results", []))
        url = js.get("next")
    return chats


def find_chat_for_user(session, username):
    """Find the chat with the given creator username. Returns chat dict or None."""
    target = username.strip().lower()
    for chat in fetch_all_chats(session):
        other = (chat.get("other_user") or {}).get("username", "")
        if other.lower() == target:
            return chat
    return None


def fetch_all_messages(session, chat_uuid):
    """Return all messages for a chat (paginated via cursor), newest first."""
    messages = []
    url = f"{API_BASE}/chats/{chat_uuid}/messages/"
    with tqdm(unit="page", desc="messages") as pbar:
        while url:
            js = _safe_json(http_get(session, url))
            messages.extend(js.get("results", []))
            url = js.get("next")
            pbar.update(1)
    return messages


def iter_media_sources(msg):
    """Yield (media_type, url, raw_item) candidates from one chat message."""
    containers = []
    media = msg.get("media")
    if isinstance(media, list):
        containers.extend(media)
    elif isinstance(media, dict):
        containers.append(media)
    for field in ("user_media", "unlock"):
        val = msg.get(field)
        if isinstance(val, dict):
            containers.append(val)
        elif isinstance(val, list):
            containers.extend(val)

    for item in containers:
        if not isinstance(item, dict):
            continue
        mtype = (item.get("media_type") or item.get("type")
                 or msg.get("message_type") or "media")
        url = resolve_url(item.get("file")) or resolve_url(item)
        if url:
            yield str(mtype), url, item

def classify_message(msg):
    """Return 'deleted', 'paywalled', 'reply-quote', or 'normal'."""
    if msg.get("deleted"):
        return "deleted"
    # paid-media is only downloadable when unlocked; unlocked is an ISO
    # timestamp string once paid, False/None otherwise
    if msg.get("message_type") == "paid-media" and not msg.get("unlocked"):
        return "paywalled"
    own_media = msg.get("media")
    reply = msg.get("reply_to")
    if not own_media and isinstance(reply, dict) and reply.get("media"):
        return "reply-quote"
    return "normal"

# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Download chat history (messages + media) from f2f.com")
    parser.add_argument("-u", "--user",
                        help="Creator username to find the chat with")
    parser.add_argument("--chat-uuid",
                        help="Chat UUID (skip lookup; from /api/chats/)")
    parser.add_argument("-c", "--cookies", default="cookies.txt",
                        help="Cookies file (default: cookies.txt)")
    parser.add_argument("-o", "--outdir", default=".",
                        help="Root output directory (default: .)")
    parser.add_argument("--no-media", action="store_true",
                        help="Only export the transcript, skip media downloads")
    args = parser.parse_args()

    if not args.user and not args.chat_uuid:
        parser.error("provide -u <username> or --chat-uuid <uuid>")

    cookie_file = Path(args.cookies)
    if not cookie_file.is_file():
        print(f"[!] Cookie file not found: {cookie_file}")
        sys.exit(1)
    cookies = load_cookies(cookie_file)
    if not cookies:
        print("[!] No cookies parsed from the file - check the format")
        sys.exit(1)

    session = requests.Session()
    session.headers.update(DEFAULT_HEADERS)
    session.cookies.update(cookies)

    # --- locate the chat ---------------------------------------------------
    if args.chat_uuid:
        chat_uuid = args.chat_uuid.strip()
        creator_name = args.user.strip().lower() if args.user else chat_uuid
        creator_title = creator_name
    else:
        creator_name = args.user.strip().lower()
        print(f"Looking for chat with @{creator_name} ...")
        chat = find_chat_for_user(session, creator_name)
        if not chat:
            print(f"[!] No chat found with @{creator_name}. "
                  f"Available chats:")
            for c in fetch_all_chats(session):
                print(f"     - {(c.get('other_user') or {}).get('username')} "
                      f"({c.get('title')})")
            sys.exit(1)
        chat_uuid = chat["uuid"]
        creator_title = chat.get("title") or creator_name
        print(f"    found chat {chat_uuid} - \"{creator_title}\"")

    # --- fetch all messages ------------------------------------------------
    print("Fetching message history ...")
    messages = fetch_all_messages(session, chat_uuid)
    messages = list(reversed(messages))  # chronological order
    print(f"    {len(messages)} message(s) total")

    chat_dir = Path(args.outdir) / creator_name / "chat"
    chat_dir.mkdir(parents=True, exist_ok=True)

    # --- download media ----------------------------------------------------
    unresolved = []
    media_files = {}   # message uuid -> list of saved filenames

    skipped_paywalled = []
    skipped_deleted = []
    skipped_quote = []

    if not args.no_media:
        # remove stale dump from a previous run
        old_dump = chat_dir / "_unresolved_media.json"
        old_dump.unlink(missing_ok=True)

        media_msgs = [
            m for m in messages
            if (m.get("media") or [])
            or list(iter_media_sources(m))
            or classify_message(m) in ("paywalled", "deleted")
        ]
        print(f"    {len(media_msgs)} message(s) contain media")

        for msg in tqdm(media_msgs, unit="msg", desc="media"):
            msg_uuid = msg.get("uuid", "unknown")
            stamp = stamp_from_iso(msg.get("datetime"))
            status = classify_message(msg)

            if status == "deleted":
                skipped_deleted.append(msg_uuid)
                continue

            if status == "paywalled":
                unlock = msg.get("unlock") or {}
                skipped_paywalled.append({
                    "message_uuid": msg_uuid,
                    "price": unlock.get("price") if isinstance(unlock, dict) else None,
                    "total_due_eur": unlock.get("total_due_eur") if isinstance(unlock, dict) else None,
                })
                # save poster previews so the paywalled items aren't invisible
                for idx, item in enumerate(msg.get("media") or []):
                    if isinstance(item, dict) and isinstance(item.get("file"), dict):
                        poster = item["file"].get("poster")
                        if poster:
                            stem_name = (f"{stamp}_chat_paywalled_preview_"
                                         f"{sanitize(msg_uuid, 40)}_{idx}")
                            dest = chat_dir / f"{stem_name}.jpg"
                            if not dest.exists():
                                try:
                                    download_file(session, poster, dest)
                                except RuntimeError:
                                    pass
                continue

            if status == "reply-quote":
                skipped_quote.append(msg_uuid)
                continue

            saved = []
            for idx, (mtype, url, raw) in enumerate(iter_media_sources(msg)):
                idx_suffix = f"_{idx}" if len(saved) > 0 or idx > 0 else ""
                stem_name = f"{stamp}_chat_{sanitize(mtype, 20)}_{sanitize(msg_uuid, 40)}{idx_suffix}"
                dest_stem = chat_dir / stem_name
                is_video = (sanitize(mtype, 20).lower().find("video") >= 0
                            or url_suffix(url) in VIDEO_EXTS)

                try:
                    if is_video:
                        existing = find_existing_video(dest_stem)
                        if existing:
                            saved.append(existing.name)
                            continue
                        result = download_video(url, dest_stem, cookies)
                        if result:
                            saved.append(result.name)
                        else:
                            unresolved.append({
                                "message_uuid": msg_uuid,
                                "reason": "video download failed",
                                "url": url,
                                "raw": raw,
                                "message": msg,
                            })
                    else:
                        ext = ext_from_url(url, mtype)
                        dest = chat_dir / f"{stem_name}{ext}"
                        if dest.exists():
                            saved.append(dest.name)
                            continue
                        download_file(session, url, dest)
                        saved.append(dest.name)
                except RuntimeError as e:
                    print(f"\n    [!] {e}")
                    unresolved.append({
                        "message_uuid": msg_uuid,
                        "reason": str(e),
                        "url": url,
                        "raw": raw,
                        "message": msg,
                    })

            # messages flagged as media types but without resolvable URLs
            if not saved and not list(iter_media_sources(msg)):
                pass  # handled below

            if saved:
                media_files[msg_uuid] = saved

        # flag only genuinely unexpected media messages
        for msg in messages:
            mtype = msg.get("message_type", "")
            status = classify_message(msg)
            if ("media" in mtype or msg.get("unlock") is not None) \
                    and status == "normal" \
                    and msg.get("uuid") not in media_files \
                    and not any(u["message_uuid"] == msg.get("uuid") for u in unresolved):
                unresolved.append({
                    "message_uuid": msg.get("uuid"),
                    "reason": "no URL found in message",
                    "message": msg,
                })

        if skipped_paywalled or skipped_deleted or skipped_quote:
            print(f"    skipped: {len(skipped_paywalled)} paywalled, "
                  f"{len(skipped_deleted)} deleted, "
                  f"{len(skipped_quote)} reply-quotes")

    if unresolved:
        dump_path = chat_dir / "_unresolved_media.json"
        dump_path.write_text(
            json.dumps(unresolved, indent=2, ensure_ascii=False),
            encoding="utf-8")
        print(f"\n[!] {len(unresolved)} media item(s) could not be downloaded.")
        print(f"    Raw data saved to {dump_path} - paste it back for a fix.")

    # --- write transcripts ---------------------------------------------------
    json_path = chat_dir / "transcript.json"
    json_msgs = []
    for msg in messages:
        entry = dict(msg)
        if msg.get("uuid") in media_files:
            entry["downloaded_files"] = media_files[msg["uuid"]]
        json_msgs.append(entry)

    meta = {
        "chat_uuid": chat_uuid,
        "creator": creator_name,
        "title": creator_title,
        "exported": datetime.now().isoformat(),
        "message_count": len(messages),
        "media_downloaded": sum(len(v) for v in media_files.values()),
        "media_unresolved": len(unresolved),
        "messages": json_msgs,
    }
    json_path.write_text(
        json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"    transcript -> {json_path}")

    txt_path = chat_dir / "transcript.txt"
    with txt_path.open("w", encoding="utf-8") as fp:
        fp.write(f"Chat with {creator_title} (@{creator_name})\n")
        fp.write(f"Chat UUID: {chat_uuid}\n")
        fp.write(f"Exported: {datetime.now().isoformat()}\n")
        fp.write("=" * 70 + "\n\n")
        for msg in messages:
            stamp = stamp_from_iso(msg.get("datetime")).replace("_", " ").replace("-", ":", 3)
            sender = creator_title if msg.get("received") else "me"
            mtype = msg.get("message_type", "text")
            content = (msg.get("content") or "").strip()
            content = (msg.get("content") or "").strip()
            files = media_files.get(msg.get("uuid"), [])
            line = f"[{stamp}] {sender}"
            if mtype != "text":
                line += f" ({mtype})"
            if status == "paywalled":
                unlock = msg.get("unlock") or {}
                tag = " [PAYWALLED"
                if isinstance(unlock, dict):
                    if unlock.get("price") is not None:
                        tag += f" - {unlock['price']} credits"
                    if unlock.get("total_due_eur") is not None:
                        tag += f" / EUR {unlock['total_due_eur']}"
                tag += "]"
                line += tag
            elif status == "deleted":
                line += " [DELETED]"
            elif status == "reply-quote":
                line += " [reply quoting a media message]"
            line += f": {content}" if content else ":"
            fp.write(line + "\n")
            for f in files:
                fp.write(f"    -> {f}\n")
            if status == "paywalled":
                fp.write("    [locked - purchase required; poster preview saved if available]\n")
            elif status == "deleted":
                fp.write("    [message was deleted by the sender]\n")
            elif not content and not files and mtype != "text":
                fp.write("    [no downloadable content - see transcript.json]\n")
    print(f"    transcript -> {txt_path}")

    print(f"\nDone - {len(messages)} messages, "
          f"{sum(len(v) for v in media_files.values())} media file(s) in {chat_dir}")


if __name__ == "__main__":
    main()