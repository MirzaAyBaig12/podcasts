#!/usr/bin/env python3
"""YouTube channels -> separate podcast feeds in one repo.

Each show gets its own feed, cover and state file under feeds/<slug>/ and
state/<slug>.json. Audio is uploaded to this repo's Releases with a namespaced
tag (<slug>-yt-<videoid>). By default there are no caps: every episode in state
goes in the feed and no releases are deleted. Only the newest SCAN_N videos are
checked per run, so old uploads are never backfilled automatically.

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
# 0 means unlimited in all three cases.
MAX_NEW = int(os.environ.get("MAX_NEW") or CFG.get("max_new_per_run", 0))
FEED_N = int(CFG.get("feed_episodes", 0))
KEEP_N = int(CFG.get("keep_releases", 0))
ART = int(CFG.get("art_size", 1400))
SCAN_N = int(os.environ.get("SCAN_N") or CFG.get("scan_count", 1000))
SHOW_SLUG = os.environ.get("SHOW_SLUG", "").strip()
SHOWS = [s for s in CFG["shows"] if not SHOW_SLUG or s["slug"] == SHOW_SLUG]
SKIP_LIVE = ("is_live", "is_upcoming", "post_live")


def run(cmd, **kw):
    return subprocess.run(cmd, check=True, text=True, **kw)


def clean_text(value):
    """Strip control chars that are illegal in XML, keep newlines."""
    text = str(value or "")
    return "".join(ch for ch in text if ch in "\n\t\r" or ord(ch) >= 32).strip()


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


def seen_file(slug):
    return ROOT / "seen" / f"{slug}.json"


def load_seen(show, eps):
    """Every video ID already processed, so nothing is ever downloaded twice.

    Seeded from the persistent ledger, the current state, and existing release
    tags — that last source covers runs interrupted before the ledger was saved.
    """
    sf = seen_file(show["slug"])
    seen = set()
    if sf.exists():
        try:
            seen |= set(json.loads(sf.read_text()))
        except (ValueError, TypeError):
            pass
    seen |= {e["id"] for e in eps}

    prefix = f"{show['slug']}-yt-"
    r = subprocess.run(
        ["gh", "release", "list", "--repo", REPO, "--limit", "10000", "--json", "tagName"],
        capture_output=True, text=True,
    )
    if r.returncode == 0:
        try:
            seen |= {
                item["tagName"][len(prefix):]
                for item in json.loads(r.stdout)
                if item.get("tagName", "").startswith(prefix)
            }
        except (ValueError, TypeError):
            pass
    return sf, seen


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


def episode_from_release(show, vid):
    """Rebuild a state entry from an existing release, with no download."""
    tag = f"{show['slug']}-yt-{vid}"
    r = subprocess.run(
        ["gh", "release", "view", tag, "--repo", REPO,
         "--json", "name,body,publishedAt,assets"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        return None
    d = json.loads(r.stdout)
    asset = next((a for a in d.get("assets", []) if a["name"].endswith(".m4a")), None)
    if asset is None:
        return None
    body = (d.get("body") or "").strip()
    link = body if body.startswith("http") else f"https://www.youtube.com/watch?v={vid}"
    title = d.get("name") or vid
    pub = (d.get("publishedAt") or "").replace("Z", "+00:00")
    if not pub:
        pub = datetime.datetime.now(datetime.timezone.utc).isoformat()
    return {
        "id": vid,
        "title": title,
        "description": title,
        "pub": pub,
        "duration": 0,
        "bytes": asset["size"],
        "audio_url": asset["url"],
        "link": link,
    }


def make_episode(show, vid, tmp):
    slug = show["slug"]
    tag = f"{slug}-yt-{vid}"
    # Never download an episode that already has a release.
    if release_exists(tag):
        print(f"[{slug}] {vid} already has a release; skipping download")
        return None
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
        "description": clean_text(info.get("description") or info["title"]),
        "pub": dt.isoformat(),
        "duration": int(info.get("duration") or 0),
        "bytes": audio.stat().st_size,
        "audio_url": f"https://github.com/{REPO}/releases/download/{tag}/{audio.name}",
        "link": link,
    }


def prune(show, eps):
    """Keep KEEP_N episodes on disk; 0 means never delete anything."""
    slug = show["slug"]
    eps.sort(key=lambda e: e["pub"], reverse=True)
    if KEEP_N <= 0:
        return eps
    keep, drop = eps[:KEEP_N], eps[KEEP_N:]
    for e in drop:
        print(f"[{slug}] pruning {e['id']}")
        subprocess.run(
            ["gh", "release", "delete", f"{slug}-yt-{e['id']}",
             "--cleanup-tag", "--yes", "--repo", REPO]
        )
    return keep


def feed_episodes(eps):
    """All episodes in newest-first order; FEED_N=0 means no cap."""
    ordered = sorted(eps, key=lambda e: e["pub"], reverse=True)
    return ordered[:FEED_N] if FEED_N > 0 else ordered


def build_feed(show, eps):
    slug = show["slug"]
    base = f"{BASE}/feeds/{slug}"
    items = []
    for e in feed_episodes(eps):
        pub = email.utils.format_datetime(datetime.datetime.fromisoformat(e["pub"]))
        items.append(
            f"""  <item>
    <title>{escape(e['title'])}</title>
    <link>{escape(e['link'])}</link>
    <guid isPermaLink="false">{escape(e['id'])}</guid>
    <pubDate>{pub}</pubDate>
    <description>{escape(clean_text(e['description']))}</description>
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
  <description>{escape(clean_text(show['description']))}</description>
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
      <p>{escape(clean_text(s['description']))}</p>
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


def md(value):
    """Make arbitrary text safe for a Markdown table cell."""
    return " ".join(str(value or "").split()).replace("|", "\\|")


def write_summary(rows):
    """Write a per-show report into the GitHub Actions job summary."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return

    lines = [
        "## Podcast feed update",
        "",
        "| Podcast | New episodes | In feed | Kept | Status |",
        "|---|---:|---:|---:|---|",
    ]
    for r in rows:
        ok = not r["failed"] and not r["show_error"]
        lines.append(
            f"| {md(r['title'])} | {len(r['new'])} | {r['feed_count']} | "
            f"{r['kept']} | {'✅ OK' if ok else '⚠️ Failed'} |"
        )

    lines += ["", "### Details", ""]
    for r in rows:
        lines.append(f"#### {md(r['title'])} (`{r['slug']}`)")
        if r["show_error"]:
            lines.append(f"- **Show failed:** {md(r['show_error'])}")
        if r["new"]:
            for vid, title in r["new"]:
                lines.append(f"- Added: **{md(title)}** (`{vid}`)")
        else:
            lines.append("- No new episodes")
        for vid, error in r["failed"]:
            lines.append(f"- **Failed:** `{vid}` — {md(error)}")
        lines.append("")

    with open(path, "a", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def main():
    failed = 0
    summary = []
    for show in SHOWS:
        slug = show["slug"]
        row = {
            "slug": slug,
            "title": show["title"],
            "new": [],
            "failed": [],
            "show_error": "",
            "feed_count": 0,
            "kept": 0,
        }
        print(f"=== {slug} ===")
        try:
            ensure_cover(show)
            sf = state_file(slug)
            sf.parent.mkdir(parents=True, exist_ok=True)
            eps = json.loads(sf.read_text()) if sf.exists() else []
            seen_sf, seen = load_seen(show, eps)

            # Only the newest SCAN_N videos are scanned. Nothing older can
            # ever be downloaded, even during a manual unlimited run.
            entries = list_channel(show)
            have = {x["id"] for x in eps}
            todo = [
                e for e in entries
                if e["id"] not in have and e.get("live_status") not in SKIP_LIVE
            ]
            if MAX_NEW > 0:
                todo = todo[:MAX_NEW]
            print(f"{len(entries)} scanned, {len(todo)} not yet in state")
            for e in todo:
                vid = e["id"]
                if vid in seen:
                    # Already has a release / was processed before. Restore the
                    # state entry from release metadata, but NEVER re-download.
                    ep = episode_from_release(show, vid)
                    if ep:
                        eps.append(ep)
                        sf.write_text(json.dumps(eps, indent=1))
                        row["new"].append((vid, f"{ep['title']} (restored, no download)"))
                        print(f"restored {vid} from release (no download)")
                    continue
                tmp = pathlib.Path(tempfile.mkdtemp())
                try:
                    ep = make_episode(show, vid, tmp)
                    if ep is None:
                        continue
                    eps.append(ep)
                    seen.add(vid)
                    sf.write_text(json.dumps(eps, indent=1))
                    seen_sf.parent.mkdir(parents=True, exist_ok=True)
                    seen_sf.write_text(json.dumps(sorted(seen), indent=1))
                    row["new"].append((vid, ep["title"]))
                    print(f"done {vid}")
                except Exception as ex:
                    failed += 1
                    row["failed"].append((vid, str(ex)))
                    print(f"FAILED {vid}: {ex}", file=sys.stderr)
                finally:
                    shutil.rmtree(tmp, ignore_errors=True)

            eps = prune(show, eps)
            sf.write_text(json.dumps(eps, indent=1))
            seen_sf.parent.mkdir(parents=True, exist_ok=True)
            seen_sf.write_text(json.dumps(sorted(seen), indent=1))
            feed_dir(slug).mkdir(parents=True, exist_ok=True)
            (feed_dir(slug) / "feed.xml").write_text(build_feed(show, eps))
            row["feed_count"] = len(feed_episodes(eps))
            row["kept"] = len(eps)
            print(f"feed written with {row['feed_count']} episodes "
                  f"({row['kept']} kept in state)")
        except Exception as ex:
            failed += 1
            row["show_error"] = str(ex)
            print(f"FAILED show {slug}: {ex}", file=sys.stderr)
        finally:
            summary.append(row)

    build_index()
    write_summary(summary)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
