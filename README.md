# f2f-scraper

Personal bulk-downloader for **f2f.com** (formerly f2f.net): posts, stories,
and full chat conversations — including paid (fan) content you have access to.

Built to replace the broken
[Gertje823/f2f-downloader](https://github.com/Gertje823/f2f-downloader), which
stopped working when f2f.net migrated to f2f.com and moved from HTML scraping
to a cookie-authenticated JSON API.

> ⚠️ Private tool for archiving content **you legally have access to** with
> your own account. Respect creators' rights — do not redistribute. Using
> scrapers may violate the site's ToS and could get your account banned.

---

## Features

### `f2f_scraper.py` — posts & stories
- Full post feed for any creator (paginated via the API cursor)
- **Images** downloaded from signed CDN URLs
- **Videos**, including HLS streams: direct MP4 download, else HLS segment
  download + `ffmpeg` remux to MP4, else `yt-dlp` fallback
- **Stories** (24h posts) via `--stories`
- **Comments, like/save counts** and per-post metadata via `-j`
- Avatar + profile banner saved once per creator
- Resumable & idempotent: existing files are skipped on rerun
- Locked/paid posts you haven't paid for are skipped cleanly

### `f2f_chat_downloader.py` — chat conversations
- Complete message history (cursor-paginated) for any creator chat
- All downloadable media: images + videos (same HLS pipeline)
- `transcript.json` — full history incl. raw API data + downloaded-file map
- `transcript.txt` — readable transcript with timestamps & sender
- Smart handling of edge cases:
  - **Paywalled (PPV) messages** → marked in transcript with price, poster
    preview saved
  - **Deleted messages** → marked, nothing fetched
  - **Reply-quote messages** → correctly ignored
- Rerun after purchasing a PPV message and it downloads automatically

### `run_all.py` — one command, everything
- **Auto-discovery**: finds every creator you chat with, detects your own
  account and excludes it
- **Subscription scope filter** using the creator-profile flags
  (`following`, `subscribed`, `cancelled`):
  - `--scope followed` → only creators you currently follow (free) **or**
    are a fan of (paid) — excludes old chat partners
  - `--scope fan` → paid subscriptions only
  - `--scope auto` (default) → followed + fan, falls back to chats if the
    profile check is unavailable
  - `--scope all` → every chat creator (legacy behavior)
- Runs posts + stories, then chat, per creator; one failure doesn't stop the run
- Final **SUMMARY table** per creator (`posts` / `chat` → OK / FAIL / SKIP)
- `--users a,b,c` to limit the run to specific creators

---

## Requirements

- **Python 3.10+**
- **ffmpeg** on your PATH (needed for HLS video conversion)
- Python packages:

```powershell
pip install -r requirements.txt
```

(`requirements.txt` contains: `requests`, `tqdm`, `yt-dlp`.)

---

## Cookie setup

The f2f.com API requires a logged-in session, passed via browser cookies.

1. The repo ships **`cookies-template.txt`** — it contains the cookie
   *names* only (no values): `sessionid`, `csrftoken`, `shield_FPC`, `splash`.
2. Copy it to `cookies.txt` in the same folder:

```powershell
copy cookies-template.txt cookies.txt
```

3. Fill in the real values for your logged-in session, one `name=value` per
   line. Easiest way to get them:
   - Log in at https://f2f.com
   - Install the browser extension **"Get cookies.txt LOCALLY"**
     (Chrome/Edge) and export — or copy each value from DevTools
     (`F12` → Application → Cookies → `https://f2f.com`)

Cookies expire: if you see `401 Unauthorized`, re-export and rerun.

---

## Commands

### One command for everything

| Command | What it does |
|---|---|
| `python run_all.py -c cookies.txt` | Download everything for all creators you **follow or are a fan of** |
| `python run_all.py -c cookies.txt --scope fan` | Only paid (fan) subscriptions |
| `python run_all.py -c cookies.txt --scope followed` | Follow (free) + fan (paid) — same as auto when profiles resolve |
| `python run_all.py -c cookies.txt --scope all` | Every chat creator, no scope filter |
| `python run_all.py -c cookies.txt --users kimholland` | Limit the run to specific creators |
| `python run_all.py -c cookies.txt --no-chat` | Posts/stories only |
| `python run_all.py -c cookies.txt --no-posts` | Chats only |
| `python run_all.py -c cookies.txt --no-stories` | Skip stories |
| `python run_all.py -c cookies.txt -o .\downloads` | Choose the output root folder |

### Single creator

```powershell
# posts + stories + JSON metadata for one creator
python f2f_scraper.py -u kimholland -j --stories -o downloads

# full chat (transcript + media) for one creator
python f2f_chat_downloader.py -u kimholland -c cookies.txt -o downloads
```

Useful flags:

- `-c/--cookies` — cookie file (default `cookies.txt`)
- `-o/--outdir` — output root directory
- `-j` — write per-post/story JSON metadata (scraper)
- `--stories` — include 24h stories (scraper)
- `-n/--name-len` — description characters used in filenames (default 30)
- `--chat-uuid` — address a chat by UUID instead of username (chat downloader)
- `--no-media` — transcript export only, skip media (chat downloader)

---

## Output layout

```
downloads/
└── <creator>/
    ├── avatar.jpg, banner.png
    ├── posts/                    images, videos, JSON metadata
    ├── stories/                  24h posts
    └── chat/
        ├── transcript.json       full history + raw API data
        ├── transcript.txt        readable transcript
        ├── *_chat_*.{jpg,mp4}    downloaded media
        └── *_paywalled_preview_*.jpg   poster previews of PPV messages
```

---

## Repository files

| File | Purpose |
|---|---|
| `run_all.py` | Orchestrator: discovery, scope filter, runs both tools per creator |
| `f2f_scraper.py` | Post/story downloader (images, HLS video, comments, metadata) |
| `f2f_chat_downloader.py` | Chat transcript + media downloader |
| `requirements.txt` | Python dependencies (`pip install -r requirements.txt`) |
| `cookies-template.txt` | Cookie *names* template — copy to `cookies.txt` and fill in values |
| `cookies.txt` | **Local only** — your real session cookies; never committed |
| `README.md` | This file |

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `401 Unauthorized` | Cookies expired → re-export into `cookies.txt` |
| `[!] script not found` | Keep all three `.py` files + `cookies.txt` in one folder |
| `TabError: inconsistent use of tabs and spaces` | VS Code: `Ctrl+Shift+P` → "Convert Indentation to Spaces" |
| HLS video fails | Ensure `ffmpeg` is on PATH; `pip install -U yt-dlp` for the fallback |
| `Repository not found` on `git push` | See "Pushing to GitHub" below |
| `_unresolved_media.json` appears in `chat/` | Contains raw API data of items that failed — inspect it to fix parsing |
| Downloaded locked content looks blurred | That's what the API serves for content you haven't unlocked |

---


## Notes

- Signed CDN URLs expire quickly; media must be downloaded in the run that lists it.
- Everything is idempotent — reruns only fetch new content.
- After buying a PPV chat message, rerun the chat downloader to fetch it.
- `fan` scope = paid subscription (full access); `follow` = free follow
  (public content only).

---

## Disclaimer

Not affiliated with or endorsed by f2f.com. Use at your own risk. Do not use this tool to distribute copyrighted material.