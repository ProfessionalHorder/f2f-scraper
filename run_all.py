#!/usr/bin/env python3
"""
run_all.py - one command: download posts + stories + chat for every creator
you currently follow or are a fan of on f2f.com.

Usage:
    python run_all.py -c cookies.txt
    python run_all.py -c cookies.txt -o ./downloads
    python run_all.py -c cookies.txt --scope fan        # paid subscriptions only
    python run_all.py -c cookies.txt --scope followed   # follow + fan (default)
    python run_all.py -c cookies.txt --scope all        # everyone with a chat
    python run_all.py -c cookies.txt --users daisypeach,naughtymila
    python run_all.py -c cookies.txt --no-chat          # posts/stories only
    python run_all.py -c cookies.txt --no-posts         # chats only

Must sit in the same folder as f2f_scraper.py and f2f_chat_downloader.py.

Scope is decided from each creator's profile JSON (/api/creators/<u>/):
    following  = free follow
    subscribed = paid fan subscription
    cancelled  = fan subscription was cancelled
"""

import argparse
import subprocess
import sys
import time
from pathlib import Path

import requests

API_BASE = "https://f2f.com/api"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://f2f.com/",
}

ME_PROBES = ["/me/", "/users/me/", "/user/"]


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
    return cookies


def api_get_json(session, url):
    try:
        r = session.get(url, timeout=20)
    except Exception:
        return None
    if r.status_code != 200:
        return None
    if "json" not in (r.headers.get("content-type") or ""):
        return None
    try:
        return r.json()
    except ValueError:
        return None


def discover_own_username(session):
    for path in ME_PROBES:
        js = api_get_json(session, API_BASE + path)
        if isinstance(js, dict) and isinstance(js.get("username"), str):
            return js["username"].lower()
    return None


def discover_chat_creators(session):
    found = set()
    url = f"{API_BASE}/chats/"
    while url:
        js = api_get_json(session, url)
        if js is None:
            break
        for chat in js.get("results", []):
            other = chat.get("other_user") or {}
            if other.get("is_creator") and other.get("username"):
                found.add(other["username"].lower())
        url = js.get("next")
    return found


def classify_creator(session, username):
    """Return dict with following/subscribed/cancelled from the creator profile,
    or None if the profile could not be read."""
    js = api_get_json(session, f"{API_BASE}/creators/{username}/")
    if not isinstance(js, dict):
        return None
    return {
        "following": bool(js.get("following")),
        "subscribed": bool(js.get("subscribed")),
        "cancelled": bool(js.get("cancelled")),
    }


def scope_label(info):
    if info is None:
        return "unknown"
    if info["subscribed"] and not info["cancelled"]:
        return "fan"
    if info["subscribed"] and info["cancelled"]:
        return "fan(cancelled)"
    if info["following"]:
        return "follow"
    return "none"


def keep_for_scope(scope, info):
    if scope == "all":
        return True
    if info is None:
        return False  # strict: unknown relationship is not downloaded
    if scope == "fan":
        return info["subscribed"] and not info["cancelled"]
    # followed (and auto): free follow or active fan
    return info["following"] or (info["subscribed"] and not info["cancelled"])


def run_tool(label, cmd):
    print(f"\n>>> {label}")
    print(f"    {' '.join(str(c) for c in cmd)}")
    try:
        proc = subprocess.run([str(c) for c in cmd])
    except FileNotFoundError:
        print("    [!] script not found")
        return False
    return proc.returncode == 0


def main():
    parser = argparse.ArgumentParser(
        description="Download posts, stories and chats from f2f.com for the "
                    "creators you follow or are a fan of.")
    parser.add_argument("-c", "--cookies", default="cookies.txt")
    parser.add_argument("-o", "--outdir", default="downloads")
    parser.add_argument("--users",
                        help="comma-separated creators to run (overrides scope)")
    parser.add_argument("--scope", choices=["auto", "followed", "fan", "all"],
                        default="auto",
                        help="auto/followed: follow + active fan; "
                             "fan: paid subs only; all: every chat creator")
    parser.add_argument("--no-posts", action="store_true")
    parser.add_argument("--no-chat", action="store_true")
    parser.add_argument("--no-stories", action="store_true")
    parser.add_argument("--name-len", type=int, default=30)
    args = parser.parse_args()

    scope = "followed" if args.scope == "auto" else args.scope

    cookie_file = Path(args.cookies)
    if not cookie_file.is_file():
        sys.exit(f"[!] cookie file not found: {cookie_file}")
    cookies = load_cookies(cookie_file)
    if not cookies:
        sys.exit("[!] no cookies parsed - check the file format")

    session = requests.Session()
    session.headers.update(HEADERS)
    session.cookies.update(cookies)

    scraper = Path("f2f_scraper.py")
    chatdl = Path("f2f_chat_downloader.py")
    for p in (scraper, chatdl):
        if not p.is_file():
            sys.exit(f"[!] missing {p} - keep all three files in one folder")

    print("Discovering creators ...")
    own = discover_own_username(session)
    if own:
        print(f"    logged in as @{own}")

    if args.users:
        creators = [u.strip().lower() for u in args.users.split(",") if u.strip()]
        print(f"    using explicit --users list: {', '.join(creators)}")
        infos = {u: classify_creator(session, u) for u in creators}
    else:
        candidates = discover_chat_creators(session)
        candidates.discard(own)
        print(f"    {len(candidates)} creator(s) from chats")

        infos = {}
        for u in sorted(candidates):
            infos[u] = classify_creator(session, u)

        creators = [u for u in sorted(candidates) if keep_for_scope(scope, infos[u])]
        dropped = [u for u in sorted(candidates) if not keep_for_scope(scope, infos[u])]

        print(f"    scope '{scope}': {len(creators)} creator(s) to download")
        for u in dropped:
            print(f"      - skip @{u} ({scope_label(infos[u])})")

    if not creators:
        sys.exit("[!] no creators matched the selected scope")

    # show the plan
    print("\nCreators to process:")
    for u in creators:
        print(f"    @{u:<25} [{scope_label(infos.get(u))}]")

    results = []
    for i, user in enumerate(creators, 1):
        print("\n" + "=" * 62)
        print(f"[{i}/{len(creators)}] @{user}  [{scope_label(infos.get(user))}]")
        print("=" * 62)
        row = {"user": user, "posts": None, "chat": None}

        if not args.no_posts:
            cmd = [sys.executable, scraper, "-u", user, "-c", cookie_file,
                   "-j", "-n", args.name_len, "-o", args.outdir]
            if not args.no_stories:
                cmd.append("--stories")
            row["posts"] = run_tool("posts + stories", cmd)

        if not args.no_chat:
            cmd = [sys.executable, chatdl, "-u", user, "-c", cookie_file,
                   "-o", args.outdir]
            row["chat"] = run_tool("chat", cmd)

        results.append(row)
        if i < len(creators):
            time.sleep(2)

    print("\n" + "=" * 62)
    print("SUMMARY")
    print("=" * 62)
    state = lambda v: "OK" if v else ("SKIP" if v is None else "FAIL")
    print(f"{'creator':<25} {'scope':<14} {'posts':<6} {'chat':<6}")
    for r in results:
        print(f"{r['user']:<25} {scope_label(infos.get(r['user'])):<14} "
              f"{state(r['posts']):<6} {state(r['chat']):<6}")
    fails = [r for r in results if False in (r["posts"], r["chat"])]
    print(f"\nDone - {len(results)} creator(s), {len(fails)} with failures.")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[interrupted]")
        sys.exit(130)