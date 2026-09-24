#!/usr/bin/env python3
"""
f2f_scraper.py - download posts (images + videos) and stories from f2f.com

Usage:
    python f2f_scraper.py -u <username> -c cookies.txt -j --stories -o ./downloads

Filenames:
    posts:   <datetime>_<protection>_<access>_<first N chars of description>_<uuid>.<ext>
    stories: <created>_story_full_<uuid>.<ext>   (stored in stories/ subfolder)

Videos are downloaded via yt-dlp + ffmpeg, with a direct-segment HLS fallback.
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


API_BASE = "https://f2f.com/api/creators"

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


def load_cookies(path):
    """Load cookies from Netscape cookies.txt or simple name=value lines."""
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
    """Parse JSON, with a clear error message on failure."""
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
                "401 Unauthorized - your session cookie is missing or expired. "
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
        for key in ("mp4", "video", "url", "src", "file", "original", "source", "link", "hls"):
            u = resolve_url(value.get(key))
            if u:
                return u
        for v in value.values():
            u = resolve_url(v)
            if u:
                return u
    return None


def url_suffix(url):
    """Lowercase file extension of a URL path, or ''."""
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
    """Make a string safe for use in Windows/Linux filenames."""
    if not text:
        return ""
    text = re.sub(r'[\\/:*?"<>|\r\n\t]+', " ", str(text))
    text = re.sub(r"\s+", "_", text.strip())
    text = text.strip("._")
    if max_len:
        text = text[:max_len].rstrip("._")
    return text


def stamp_from_iso(iso):
    """'2022-02-05T15:51:37.061422+01:00' -> '2022-02-05_15-51-37'."""
    if not iso:
        return "nodate"
    try:
        d = datetime.fromisoformat(str(iso))
        return d.strftime("%Y-%m-%d_%H-%M-%S")
    except (ValueError, TypeError):
        return sanitize(str(iso), 25) or "nodate"


def build_stem(post, desc_len):
    """Post filename stem: <datetime>_<protection>_<access>_<description>_<uuid>."""
    stamp = stamp_from_iso(post.get("datetime"))
    protection = sanitize(post.get("protection"), 30) or "na"
    access = sanitize(post.get("access"), 20) or "na"
    desc = sanitize(post.get("content"), desc_len)
    uuid = sanitize(post.get("uuid"), 40)
    parts = [stamp, protection, access]
    if desc:
        parts.append(desc)
    parts.append(uuid)
    return "_".join(p for p in parts if p)


def build_story_stem(story):
    """Story filename stem: <created>_story_full_<uuid>."""
    stamp = stamp_from_iso(story.get("created"))
    uuid = sanitize(story.get("uuid"), 40)
    return f"{stamp}_story_full_{uuid}"


def find_existing_video(dest_stem):
    """Return an already-downloaded playable video file for this stem, if any."""
    for p in dest_stem.parent.glob(dest_stem.name + ".*"):
        if p.suffix.lower() in PLAYABLE_VIDEO_EXTS and p.stat().st_size > VIDEO_MIN_BYTES:
            return p
    return None


def _extract_media_from_text(text, base_url):
    """Try to find a direct mp4/m3u8 URL inside HTML/JSON/JS text."""
    text = text.replace("\\u002F", "/").replace("\\/", "/")

    patterns = [
        r'https?://[^"\'\s<>\\]+?\.(?:mp4|m3u8|mov|webm)(?:[^"\'\s<>\\]*)',
        r'(?:video|mp4|m3u8|src|source|url|file)["\']?\s*[:=]\s*["\'](https?://[^"\']+)',
    ]

    for pat in patterns:
        for candidate in re.findall(pat, text, re.I):
            u = urljoin(base_url, candidate)
            if u.startswith("http") and not u.endswith((".js", ".css")):
                return u

    return None


def _normalize_m3u8(text, base_url):
    """Make relative M3U8 URLs absolute."""
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
    """Download with yt-dlp. Used mostly for HLS/M3U8 streams."""
    if yt_dlp is None:
        print("\n    [!] yt-dlp is not installed. Run: pip install -U yt-dlp")
        return None

    # If url is a local file path (not http), convert to file:// URL
    if not url.startswith("http"):
        manifest_path = Path(url).resolve()
        url = manifest_path.as_uri()

    cookie_header = "; ".join(f"{k}={v}" for k, v in cookies.items())

    headers = {
        "User-Agent": DEFAULT_HEADERS["User-Agent"],
        "Accept": "application/vnd.apple.mpegurl, application/x-mpegURL, video/mp4, video/webm, video/*;q=0.9, */*;q=0.8",
        "Referer": referer,
        "Origin": "https://f2f.com",
        "Sec-Fetch-Dest": "video",
        "Sec-Fetch-Mode": "no-cors",
        "Sec-Fetch-Site": "same-origin",
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
                Path(p)
                for p in sorted(glob.glob(str(dest_stem) + ".*"))
                if Path(p).suffix.lower() in PLAYABLE_VIDEO_EXTS
                and Path(p).stat().st_size > VIDEO_MIN_BYTES
            ]
            if matches:
                return matches[0]

        print(f"\n    [!] yt-dlp attempt {i} did not produce a playable file")

    return None


def _hls_download_direct(text, base_url, dest_stem, cookies, referer):
    """
    Fallback: download all HLS segments with requests, concatenate them,
    then remux to .mp4 with ffmpeg. Returns the saved Path or None.
    """
    if shutil.which("ffmpeg") is None:
        print("\n    [!] ffmpeg not found on PATH - cannot remux HLS segments")
        return None

    lines = [l.strip() for l in text.splitlines()]
    manifest_text = text
    manifest_url = base_url

    # Master playlist? Pick the highest-bandwidth variant.
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
        cmd = [
            "ffmpeg", "-y", "-loglevel", "error",
            "-i", str(ts_path),
            "-c", "copy",
            str(dest),
        ]
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
    """
    Download a video (direct file, HLS stream, or JSON/HTML redirect).
    dest_stem: Path WITHOUT extension. Returns the saved Path, or None.
    """
    if depth > 3:
        print("\n    [!] Too many redirects while resolving video URL")
        return None

    # Build a better Referer from the output folder structure.
    username = dest_stem.parent.name
    if username.lower() == "stories":
        username = dest_stem.parent.parent.name

    referer = f"https://f2f.com/{username}/" if username else "https://f2f.com/"

    s = requests.Session()
    s.headers.update({
        "User-Agent": DEFAULT_HEADERS["User-Agent"],
        "Accept": "*/*",
        "Referer": referer,
        "Origin": "https://f2f.com",
        "Sec-Fetch-Dest": "video",
        "Sec-Fetch-Mode": "no-cors",
        "Sec-Fetch-Site": "same-origin",
    })
    s.cookies.update(cookies)

    r = None

    # Some endpoints accept Range requests, some do not. Try both.
    for use_range in (True, False):
        extra_headers = {"Range": "bytes=0-"} if use_range else {}

        try:
            r = s.get(
                url,
                timeout=30,
                stream=True,
                allow_redirects=True,
                headers=extra_headers,
            )
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
            preview = r.text[:500]
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

    looks_video_binary = (
        is_mp4
        or is_webm
        or content_type.startswith("video/")
    )

    if looks_video_binary or (
        content_type.startswith("application/octet-stream")
        and content_length > VIDEO_MIN_BYTES
    ):
        if is_webm or "webm" in content_type:
            ext = ".webm"
        else:
            ext = url_suffix(r.url)

            if not ext:
                if "mp4" in content_type or is_mp4:
                    ext = ".mp4"
                else:
                    ext = ".mp4"

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
            print(
                f"\n    [!] Downloaded video was too small ({size} bytes); "
                f"probably not a playable video. Preview: {preview!r}"
            )
            return None

        return dest

    # If we get here, the response is probably text: M3U8, JSON, HTML, or an error.
    rest = b"".join(r.iter_content(chunk_size=16384))
    body = first + rest
    text = body.decode("utf-8", "replace")

    # HLS / M3U8 manifest
    if text.lstrip().upper().startswith("#EXTM3U") or "mpegurl" in content_type:
        print("\n    [i] Stream is HLS/M3U8; trying yt-dlp/ffmpeg ...")

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

        print(f"    [!] Could not convert HLS automatically. Manifest saved: {manifest_path}")
        return None

    # JSON response containing the real media URL
    if content_type.startswith("application/json") or text.lstrip()[:1] in ("{", "["):
        try:
            data = json.loads(text)
            extracted = resolve_url(data)
            if extracted and extracted != url:
                return download_video(extracted, dest_stem, cookies, depth + 1)
        except Exception:
            pass

    # HTML / JS response containing a direct media URL
    extracted = _extract_media_from_text(text, str(r.url))
    if extracted and extracted != url:
        print(f"\n    [i] Extracted media URL from response: {extracted[:120]}...")
        return download_video(extracted, dest_stem, cookies, depth + 1)

    print("\n    [?] Video endpoint returned non-video content.")
    print(f"    final URL: {r.url}")
    print(f"    content-type: {content_type}")
    print(f"    preview: {text[:700]!r}")
    return None


def fetch_profile(session, username):
    url = f"{API_BASE}/{username}/"
    return _safe_json(http_get(session, url))


def fetch_posts(session, username, next_url=None):
    url = next_url or f"{API_BASE}/{username}/posts/"
    js = _safe_json(http_get(session, url))
    return js.get("results", []), js.get("next")


def fetch_comments(session, username, post_uuid):
    url = f"{API_BASE}/{username}/posts/{post_uuid}/comments/"
    try:
        js = _safe_json(http_get(session, url))
        return js.get("results", [])
    except RuntimeError:
        return []


def fetch_story_uuids(session, username):
    """Return list of currently active story UUIDs."""
    url = f"{API_BASE}/{username}/stories/"
    js = _safe_json(http_get(session, url))
    uuids = []
    if isinstance(js, list):
        for group in js:
            uuids.extend(group.get("stories", []))
    return uuids


def fetch_story_detail(session, username, story_uuid):
    url = f"{API_BASE}/{username}/stories/{story_uuid}/"
    return _safe_json(http_get(session, url))


def download_stories(session, username, outroot, save_json, cookies):
    """Download all currently active stories into outroot/stories/."""
    print("\nChecking stories...")
    try:
        uuids = fetch_story_uuids(session, username)
    except RuntimeError as e:
        print(f"    [!] {e}")
        return 0

    if not uuids:
        print("    No active stories right now (they only live for 24 hours).")
        return 0

    stories_dir = outroot / "stories"
    stories_dir.mkdir(parents=True, exist_ok=True)
    print(f"    {len(uuids)} active story item(s) found")

    downloaded = 0
    for story_uuid in tqdm(uuids, unit="story", desc="stories"):
        try:
            story = fetch_story_detail(session, username, story_uuid)
        except RuntimeError as e:
            print(f"\n    [!] {e}")
            continue

        media = story.get("media")
        if not isinstance(media, dict):
            print(f"\n    [?] story {story_uuid}: unexpected media structure: "
                  f"{json.dumps(media)[:200]}")
            continue

        media_type = media.get("media_type", "unknown")
        media_url = resolve_url(media.get("file"))
        if not media_url:
            print(f"\n    [?] story {story_uuid}: no URL in media: "
                  f"{json.dumps(media)[:200]}")
            continue

        stem = build_story_stem(story)
        is_video = media_type == "video" or url_suffix(media_url) in VIDEO_EXTS
        saved_name = None

        if is_video:
            dest_stem = stories_dir / stem
            existing = find_existing_video(dest_stem)
            if existing:
                saved_name = existing.name
            else:
                saved = download_video(media_url, dest_stem, cookies)
                if saved:
                    saved_name = saved.name
        else:
            ext = ext_from_url(media_url, media_type)
            dest = stories_dir / f"{stem}{ext}"
            if dest.exists():
                saved_name = dest.name
            else:
                try:
                    download_file(session, media_url, dest)
                    saved_name = dest.name
                except RuntimeError as e:
                    print(f"\n    [!] {e}")

        if not saved_name:
            continue

        downloaded += 1

        if save_json:
            meta = {
                "type": "story",
                "uuid": story.get("uuid"),
                "created": story.get("created"),
                "expires": story.get("expires"),
                "media_type": media_type,
                "file": saved_name,
                "explicit": media.get("explicit"),
                "viewed": story.get("viewed"),
                "liked": story.get("liked"),
            }
            meta_path = stories_dir / f"{stem}.json"
            meta_path.write_text(
                json.dumps(meta, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )

    print(f"    Stories done: {downloaded} file(s) in {stories_dir}")
    return downloaded


def main():
    parser = argparse.ArgumentParser(description="Download posts/stories from f2f.com")
    parser.add_argument("-u", "--user", required=True, help="Target username")
    parser.add_argument("-c", "--cookies", default="cookies.txt",
                        help="Cookies file (Netscape txt or simple name=value lines)")
    parser.add_argument("-j", "--json", action="store_true",
                        help="Write per-post/story JSON metadata")
    parser.add_argument("--stories", action="store_true",
                        help="Also download currently active stories (24h posts)")
    parser.add_argument("-n", "--name-len", type=int, default=30,
                        help="Number of description characters in filenames (default 30)")
    parser.add_argument("-o", "--outdir", default=".", help="Root output directory")
    args = parser.parse_args()

    original_username = args.user.strip()
    args.user = original_username.lower()
    if args.user != original_username:
        print(f"[i] Normalized username: {original_username} -> {args.user}")

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

    outroot = Path(args.outdir) / args.user
    outroot.mkdir(parents=True, exist_ok=True)

    print(f"Fetching profile for @{args.user} ...")
    profile = fetch_profile(session, args.user)
    print(f"    {profile.get('display_name', '')} - "
          f"{profile.get('visible_post_count', '?')} visible posts, "
          f"{profile.get('follower_count', '?')} followers")

    for key, filename in (("profile_image", "avatar"), ("profile_banner", "banner")):
        url = resolve_url(profile.get(key))
        if url:
            ext = ext_from_url(url, "image")
            dest = outroot / f"{filename}{ext}"
            try:
                download_file(session, url, dest)
                print(f"    saved {dest.name}")
            except RuntimeError as e:
                print(f"    [!] {e}")

    print("\nDownloading posts...")
    next_url = None
    total = 0
    skipped_locked = 0
    unresolved = 0
    rows = []

    with tqdm(unit="post", desc="posts") as pbar:
        while True:
            posts, next_url = fetch_posts(session, args.user, next_url)
            for post in posts:
                uuid = post.get("uuid")
                if not uuid:
                    continue

                stem = build_stem(post, args.name_len)
                media_items = post.get("media", [])

                downloaded_any = False
                saved_files = []
                for idx, media_item in enumerate(media_items):
                    if media_item.get("access") != "full":
                        continue

                    media_type = media_item.get("media_type", "unknown")
                    media_url = resolve_url(media_item.get("file"))
                    if not media_url:
                        unresolved += 1
                        print(f"\n    [?] no URL found in media item: "
                              f"{json.dumps(media_item)[:300]}")
                        continue

                    idx_suffix = f"_{idx}" if len(media_items) > 1 else ""
                    is_video = media_type == "video" or url_suffix(media_url) in VIDEO_EXTS

                    if is_video:
                        suffix = url_suffix(media_url)
                        if suffix in IMAGE_EXTS:
                            # Video item resolved to an image URL (likely a poster).
                            print(f"\n    [?] video media item resolved to an image URL. "
                                  f"Raw item:")
                            print(json.dumps(media_item, indent=1)[:800])
                            continue

                        dest_stem = outroot / f"{stem}{idx_suffix}"
                        existing = find_existing_video(dest_stem)
                        if existing:
                            saved_files.append(existing.name)
                            downloaded_any = True
                            continue

                        saved = download_video(media_url, dest_stem, cookies)
                        if saved:
                            saved_files.append(saved.name)
                            downloaded_any = True
                        continue

                    # Image branch
                    ext = ext_from_url(media_url, media_type)
                    dest = outroot / f"{stem}{idx_suffix}{ext}"
                    if dest.exists():
                        saved_files.append(dest.name)
                        downloaded_any = True
                        continue
                    try:
                        download_file(session, media_url, dest)
                        saved_files.append(dest.name)
                        downloaded_any = True
                    except RuntimeError as e:
                        print(f"\n    [!] {e}")

                if not downloaded_any:
                    skipped_locked += 1
                    pbar.update(1)
                    continue

                comments_raw = fetch_comments(session, args.user, uuid)
                comments = [
                    {
                        "username": (c.get("user") or {}).get("username"),
                        "display_name": (c.get("user") or {}).get("display_name"),
                        "comment": c.get("content"),
                        "created": c.get("created"),
                    }
                    for c in comments_raw
                ]

                if args.json:
                    meta = {
                        "type": "post",
                        "uuid": uuid,
                        "datetime": post.get("datetime"),
                        "description": post.get("content"),
                        "files": saved_files,
                        "media_types": [m.get("media_type") for m in media_items],
                        "likes": post.get("likes"),
                        "saves": post.get("bookmarks"),
                        "comment_count": post.get("comments"),
                        "pinned": post.get("pinned"),
                        "access": post.get("access"),
                        "protection": post.get("protection"),
                        "comments": comments,
                    }
                    meta_path = outroot / f"{stem}.json"
                    meta_path.write_text(
                        json.dumps(meta, indent=2, ensure_ascii=False),
                        encoding="utf-8",
                    )

                rows.append((
                    uuid, ", ".join(saved_files), post.get("likes"),
                    post.get("bookmarks"), post.get("comments"),
                ))
                total += 1
                pbar.update(1)

            if not next_url:
                break

    print(f"\nDone - {total} post(s) downloaded to {outroot}")
    if skipped_locked:
        print(f"     {skipped_locked} post(s) skipped (locked/premium - not subscribed)")
    if unresolved:
        print(f"     {unresolved} media item(s) had no recognizable URL")

    if args.stories:
        download_stories(session, args.user, outroot, args.json, cookies)

    if rows:
        print(f"\n{'Post ID':<10} {'Files':<60} {'Likes':>5} {'Saves':>5} {'Com':>5}")
        print("-" * 90)
        for r in rows:
            print(f"{r[0][:8]:<10} {r[1][:58]:<60} {r[2]:>5} {r[3]:>5} {r[4]:>5}")


if __name__ == "__main__":
    main()