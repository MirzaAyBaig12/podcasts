#!/usr/bin/env python3
"""YouTube channels -> separate podcast feeds in one repo.

Each show gets its own feed, cover and state file under feeds/<slug>/ and
state/<slug>.json. Audio is uploaded to this repo's Releases with a namespaced
tag (<slug>-yt-<videoid>). The newest FEED_EPISODES go in the feed; releases
older than KEEP_RELEASES are deleted so storage stays bounded.

Needs: yt-dlp, ffmpeg, ImageMagick, and gh (GH_TOKEN set in CI).
"""
import datetime
import email.utils
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from xml.sax.saxutils import escape

ROOT = pathlib.Path(__file__).resolve().parent.parent
CFG = json.loads((ROOT / "config.json").read_text())
REPO = os.environ.get("GITHUB_REPOSITORY") or CFG["repo"]
BASE = CFG["pages_url"].rstrip("/")
MAX_NEW = int(os.environ.get("MAX_NEW") or CFG.get("max_new_per_run", 20))
FEED_N = int(CFG.get("feed_episodes", 20))
KEEP_N = int(CFG.get("keep_releases", 100))
ART = int(CFG.get("art_size", 1400))
SCAN_N = max(FEED_N, KEEP_N, MAX_NEW)
SKIP_LIVE = ("is_live", "is_upcoming", "post_live")


def run(cmd, **kw):
    return subprocess.run(cmd, check=True, text=True, **kw)


def ytdlp_base():
    args = ["yt-dlp", "--no-warnings"]
    # yt-dlp needs a JS runtime for YouTube; prefer deno (default), fall back to node
    if shutil.which("node") and not shutil.which("deno"):
        args += ["--js-runtimes", "node"]
    cookies = ROOT / "cookies.txt"
    if cookies.exists():
        args += ["--cookies", str(cookies)]
    return args


def magick():
    return [shutil.which("magick")] if shutil.which("magick") else ["convert"]


def feed_dir(slug):
    return ROOT / "feeds" / slug


def state_file(slug):
    return ROOT / "state" / f"{slug}.json"


def ensure_cover(show):
    """Channel profile picture -> square cover.jpg for this show."""
    out_dir = feed_dir(show["slug"])
    out_dir.mkdir(parents=True, exist_ok=True)
    cover = out_dir / "cover.jpg"
    if cover.exists():
        return
    channel_root = show["channel_url"].removesuffix("/videos")
    out = run(
        ytdlp_base() + ["-J", "--flat-playlist", "--playlist-end", "1", channel_root],
        capture_output=True,
    ).stdout
    avatars = [t for t in json.loads(out).get("thumbnails", []) if "avatar" in str(t.get("id"))]
    if not avatars:
        raise RuntimeError(f"could not find avatar for {show['slug']}")
    raw = out_dir / "avatar.tmp"
    urllib.request.urlretrieve(avatars[-1]["url"], raw)
    run(
        magick()
        + [str(raw), "-resize", f"{ART}x{ART}^", "-gravity", "center",
           "-extent", f"{ART}x{ART}", "-strip", "-quality", "90", str(cover)]
    )
    raw.unlink(missing_ok=True)


def list_channel(show):
    out = run(
        ytdlp_base()
        + ["-J", "--flat-playlist", "--playlist-end", str(SCAN_N), show["channel_url"]],
        capture_output=True,
    ).stdout
    return [e for e in json.loads(out).get("entries", []) if e and e.get("id")]


def release_exists(tag):
    r = subprocess.run(
        ["gh", "release", "view", tag, "--repo", REPO], capture_output=True, text=True
    )
    return r.returncode == 0


def make_episode(show, vid, tmp):
    slug = show["slug"]
    link = f"https://www.youtube.com/watch?v={vid}"
    run(
        ytdlp_base()
        + [
            "-f", "bestaudio[ext=m4a]/bestaudio/best",
            "-x", "--audio-format", "m4a",
            "--write-info-json",
            "-o", str(tmp / "%(id)s.%(ext)s"),
            link,
        ]
    )
    info = json.loads((tmp / f"{vid}.info.json").read_text())
    audio = tmp / f"{vid}.m4a"

    tag = f"{slug}-yt-{vid}"
    if release_exists(tag):
        run(["gh", "release", "upload", tag, str(audio), "--clobber", "--repo", REPO])
    else:
        run(["gh", "release", "create", tag, str(audio), "--repo", REPO,
             "--title", info["title"], "--notes", link])

    ts = info.get("release_timestamp") or info.get("timestamp")
    if ts:
        dt = datetime.datetime.fromtimestamp(ts, datetime.timezone.utc)
    else:
        d = info.get("upload_date") or datetime.date.today().strftime("%Y%m%d")
        dt = datetime.datetime.strptime(d, "%Y%m%d").replace(
            hour=12, tzinfo=datetime.timezone.utc
        )

    return {
        "id": vid,
        "title": info["title"],
        "description": (info.get("description") or info["title"])[:4000],
        "pub": dt.isoformat(),
        "duration": int(info.get("duration") or 0),
        "bytes": audio.stat().st_size,
        "audio_url": f"https://github.com/{REPO}/releases/download/{tag}/{audio.name}",
        "link": link,
    }


def prune(show, eps):
    """Keep KEEP_N episodes on disk; delete older releases."""
    slug = show["slug"]
    eps.sort(key=lambda e: e["pub"], reverse=True)
    keep, drop = eps[:KEEP_N], eps[KEEP_N:]
    for e in drop:
        print(f"[{slug}] pruning {e['id']}")
        subprocess.run(
            ["gh", "release", "delete", f"{slug}-yt-{e['id']}",
             "--cleanup-tag", "--yes", "--repo", REPO]
        )
    return keep


def build_feed(show, eps):
    slug = show["slug"]
    base = f"{BASE}/feeds/{slug}"
    items = []
    for e in sorted(eps, key=lambda x: x["pub"], reverse=True)[:FEED_N]:
        pub = email.utils.format_datetime(datetime.datetime.fromisoformat(e["pub"]))
        items.append(
            f"""  <item>
    <title>{escape(e['title'])}</title>
    <link>{escape(e['link'])}</link>
    <guid isPermaLink="false">{escape(e['id'])}</guid>
    <pubDate>{pub}</pubDate>
    <description>{escape(e['description'])}</description>
    <enclosure url="{escape(e['audio_url'])}" length="{e['bytes']}" type="audio/mp4"/>
    <itunes:duration>{e['duration']}</itunes:duration>
    <itunes:image href="{base}/cover.jpg"/>
  </item>"""
        )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd" xmlns:atom="http://www.w3.org/2005/Atom">
<channel>
  <title>{escape(show['title'])}</title>
  <link>{escape(show['channel_url'].removesuffix('/videos'))}</link>
  <atom:link href="{base}/feed.xml" rel="self" type="application/rss+xml"/>
  <description>{escape(show['description'])}</description>
  <language>{escape(show.get('language', 'en'))}</language>
  <itunes:author>{escape(show['author'])}</itunes:author>
  <itunes:explicit>false</itunes:explicit>
  <itunes:image href="{base}/cover.jpg"/>
  <itunes:category text="{escape(show.get('category', 'TV & Film'))}"/>
  <image>
    <url>{base}/cover.jpg</url>
    <title>{escape(show['title'])}</title>
    <link>{escape(show['channel_url'].removesuffix('/videos'))}</link>
  </image>
{chr(10).join(items)}
</channel>
</rss>
"""


def build_index():
    cards = "\n".join(
        f"""    <li>
      <img src="feeds/{s['slug']}/cover.jpg" alt="{escape(s['title'])} cover">
      <h2>{escape(s['title'])}</h2>
      <p>{escape(s['description'])}</p>
      <a href="feeds/{s['slug']}/feed.xml">RSS feed</a>
    </li>"""
        for s in CFG["shows"]
    )
    (ROOT / "index.html").write_text(f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Podcasts</title>
  <style>
    body {{ font: 16px/1.5 system-ui, sans-serif; max-width: 44rem; margin: 2rem auto; padding: 0 1rem; }}
    li {{ list-style: none; margin-bottom: 2.5rem; }}
    img {{ width: 140px; height: 140px; border-radius: 8px; object-fit: cover; float: right; margin-left: 1rem; }}
    h2 {{ margin-top: 0; }}
  </style>
</head>
<body>
  <h1>Podcasts</h1>
  <p>Independent audio feeds generated from YouTube channels.</p>
  <ul>
{cards}
  </ul>
</body>
</html>
""")


def main():
    failed = 0
    for show in CFG["shows"]:
        slug = show["slug"]
        print(f"=== {slug} ===")
        try:
            ensure_cover(show)
            sf = state_file(slug)
            sf.parent.mkdir(parents=True, exist_ok=True)
            eps = json.loads(sf.read_text()) if sf.exists() else []
            known = {e["id"] for e in eps}

            if MAX_NEW > 0:
                entries = list_channel(show)
                todo = [
                    e for e in entries
                    if e["id"] not in known and e.get("live_status") not in SKIP_LIVE
                ][:MAX_NEW]
                print(f"{len(entries)} scanned, {len(todo)} new to process")
                for e in todo:
                    tmp = pathlib.Path(tempfile.mkdtemp())
                    try:
                        eps.append(make_episode(show, e["id"], tmp))
                        sf.write_text(json.dumps(eps, indent=1))
                        print(f"done {e['id']}")
                    except Exception as ex:
                        failed += 1
                        print(f"FAILED {e['id']}: {ex}", file=sys.stderr)
                    finally:
                        shutil.rmtree(tmp, ignore_errors=True)

            eps = prune(show, eps)
            sf.write_text(json.dumps(eps, indent=1))
            feed_dir(slug).mkdir(parents=True, exist_ok=True)
            (feed_dir(slug) / "feed.xml").write_text(build_feed(show, eps))
            print(f"feed written with {len(eps[:FEED_N])} episodes "
                  f"({len(eps)} kept in state)")
        except Exception as ex:
            failed += 1
            print(f"FAILED show {slug}: {ex}", file=sys.stderr)

    build_index()
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
