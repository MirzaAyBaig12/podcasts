#!/usr/bin/env python3
"""YouTube channels -> separate podcast feeds in one repo.

Each show gets its own feed, cover and state file under feeds/<slug>/ and
state/<slug>.json. Audio is uploaded to this repo's Releases with a namespaced
tag (<slug>-yt-<videoid>). By default there are no caps: every episode in state
goes in the feed and no releases are deleted. Only the newest SCAN_N videos are
checked per run, so old uploads are never backfilled automatically.

Needs: yt-dlp, ffmpeg, ImageMagick, and gh (GH_TOKEN set in CI).
"""
import concurrent.futures
import datetime
import email.utils
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import time
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
if SHOW_SLUG.lower() == "all":
    SHOW_SLUG = ""
REPAIR_DATES = os.environ.get("REPAIR_DATES", "").lower() in ("1", "true", "yes")
REPAIR_WORKERS = max(1, int(os.environ.get("REPAIR_WORKERS", "8")))
SHOWS = [s for s in CFG["shows"] if not SHOW_SLUG or s["slug"] == SHOW_SLUG]
ALLOW_FAILURES = os.environ.get("ALLOW_FAILURES", "").lower() in ("1", "true", "yes")
MAX_WORKERS = max(1, int(os.environ.get("MAX_WORKERS", "4")))
CHECKPOINT_EVERY = max(1, int(os.environ.get("CHECKPOINT_EVERY", "50")))
DOWNLOAD_RETRIES = max(1, int(os.environ.get("DOWNLOAD_RETRIES", "3")))
DOWNLOAD_DELAY = max(1, int(os.environ.get("DOWNLOAD_DELAY", "5")))
SKIP_LIVE = ("is_live", "is_upcoming", "post_live")
# Channel-members-only videos cannot be downloaded with our cookies. yt-dlp
# reports them via `availability` in playlist metadata; the error hints are a
# fallback for versions that omit that field.
MEMBER_ONLY_AVAIL = ("subscriber_only", "members_only", "premium")
MEMBER_ONLY_ERR = (
    "members-only", "members only", "subscriber-only", "subscriber only",
    "join this channel", "exclusive content",
)


class MemberOnlyError(Exception):
    """Raised when a video is gated behind channel membership."""


def is_member_only(entry):
    return (entry.get("availability") or "").lower() in MEMBER_ONLY_AVAIL


def run(cmd, **kw):
    return subprocess.run(cmd, check=True, text=True, **kw)


def run_retry(cmd, attempts=3, delay=5, **kw):
    """Run a command, retrying transient failures without hiding output."""
    kw.setdefault("text", True)
    last = None
    for i in range(attempts):
        last = subprocess.run(cmd, **kw)
        if last.returncode == 0:
            return last
        if i < attempts - 1:
            print(
                f"retry {i + 1}/{attempts - 1} for: {' '.join(map(str, cmd))}",
                file=sys.stderr,
            )
            time.sleep(delay * (i + 1))
    raise subprocess.CalledProcessError(
        last.returncode, cmd, output=last.stdout, stderr=last.stderr
    )


def clean_text(value):
    """Strip control chars that are illegal in XML, keep newlines."""
    text = str(value or "")
    return "".join(ch for ch in text if ch in "\n\t\r" or ord(ch) >= 32).strip()


def entry_pub(entry):
    """Real upload date from yt-dlp metadata. Never the release timestamp."""
    ts = entry.get("release_timestamp") or entry.get("timestamp")
    if ts:
        return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).isoformat()
    upload = entry.get("upload_date")
    if upload:
        return datetime.datetime.strptime(
            str(upload), "%Y%m%d"
        ).replace(hour=12, tzinfo=datetime.timezone.utc).isoformat()
    return None


def ytdlp_base():
    # An explicit socket timeout prevents one hung YouTube request from
    # stalling an entire backfill run until the workflow timeout.
    args = ["yt-dlp", "--no-warnings", "--socket-timeout", "20",
            "--sleep-interval", "1"]
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
    out = run_retry(
        ytdlp_base()
        + ["-J", "--flat-playlist", "--playlist-end", str(SCAN_N), show["channel_url"]],
        attempts=3,
        delay=5,
        capture_output=True,
    ).stdout
    return [e for e in json.loads(out).get("entries", []) if e and e.get("id")]


def release_exists(tag):
    r = subprocess.run(
        ["gh", "release", "view", tag, "--repo", REPO], capture_output=True, text=True
    )
    return r.returncode == 0


def episode_from_release(show, vid, entry=None):
    """Rebuild state from an existing release using real YouTube metadata."""
    tag = f"{show['slug']}-yt-{vid}"
    r = subprocess.run(
        ["gh", "release", "view", tag, "--repo", REPO,
         "--json", "name,assets"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        return None
    d = json.loads(r.stdout)
    asset = next((a for a in d.get("assets", []) if a["name"].endswith(".m4a")), None)
    if asset is None:
        return None
    link = f"https://www.youtube.com/watch?v={vid}"
    title = d.get("name") or (entry or {}).get("title") or vid
    entry = entry or {}
    pub = entry_pub(entry)
    duration = int(entry.get("duration") or 0)
    description = clean_text(entry.get("description") or title) or title

    # Flat playlist entries often omit upload dates, so fall back to a per-video
    # metadata lookup. Never fall back to the GitHub release timestamp: that is
    # only when CI uploaded the audio, not when the video was published.
    if not pub:
        try:
            meta = run_retry(
                ytdlp_base() + ["-J", "--skip-download", link],
                attempts=2,
                delay=3,
                capture_output=True,
            )
            m = json.loads(meta.stdout)
            pub = entry_pub(m)
            duration = duration or int(m.get("duration") or 0)
            description = clean_text(m.get("description") or title) or title
        except (subprocess.CalledProcessError, ValueError, TypeError, KeyError):
            pass
    if not pub:
        print(f"[{show['slug']}] {vid} has no real upload date; skipping restore",
              file=sys.stderr)
        return None

    return {
        "id": vid,
        "title": title,
        "description": description,
        "pub": pub,
        "duration": duration,
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
    for attempt in range(1, DOWNLOAD_RETRIES + 1):
        if release_exists(tag):
            print(f"[{slug}] {vid} already has a release; skipping download")
            return None
        try:
            run_retry(
                ytdlp_base()
                + [
                    "-f", "bestaudio[ext=m4a]/bestaudio/best",
                    "-x", "--audio-format", "m4a",
                    "--write-info-json",
                    "-o", str(tmp / "%(id)s.%(ext)s"),
                    link,
                ],
                attempts=1,
                capture_output=True,
            )
            break
        except subprocess.CalledProcessError as ex:
            text = f"{ex.stdout or ''} {ex.stderr or ''}".lower()
            if any(h in text for h in MEMBER_ONLY_ERR):
                raise MemberOnlyError(vid) from ex
            if attempt >= DOWNLOAD_RETRIES:
                raise
            print(
                f"[{slug}] {vid} download failed (attempt {attempt}), retrying",
                file=sys.stderr,
            )
            for partial in tmp.glob(f"{vid}.*"):
                partial.unlink(missing_ok=True)
            time.sleep(DOWNLOAD_DELAY * attempt)
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


def download_one(show, entry):
    """Worker-safe download of a single episode. Returns (vid, episode, error)."""
    vid = entry["id"]
    if is_member_only(entry):
        return vid, None, "member-only"
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="podcasts-"))
    try:
        return vid, make_episode(show, vid, tmp), None
    except MemberOnlyError:
        return vid, None, "member-only"
    except Exception as ex:  # reported by the main thread; never kills a worker
        return vid, None, str(ex)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def commit_checkpoint(slug):
    """Push partial state so a later failure cannot lose hours of work."""
    # The workflow's final commit step configures this later, so set it here too.
    subprocess.run(["git", "config", "user.name", "github-actions[bot]"],
                   cwd=ROOT, check=False, capture_output=True, text=True)
    subprocess.run(
        ["git", "config", "user.email",
         "41898282+github-actions[bot]@users.noreply.github.com"],
        cwd=ROOT, check=False, capture_output=True, text=True,
    )
    files = [
        f"state/{slug}.json",
        f"seen/{slug}.json",
        f"feeds/{slug}/feed.xml",
    ]
    subprocess.run(["git", "add", *files], cwd=ROOT, check=False,
                   capture_output=True, text=True)
    if subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=ROOT).returncode == 0:
        return
    subprocess.run(["git", "commit", "-m", f"Checkpoint backfill: {slug}"],
                   cwd=ROOT, check=True)
    for attempt in range(1, 21):
        subprocess.run(["git", "rebase", "--abort"], cwd=ROOT,
                       check=False, capture_output=True, text=True)
        pull = subprocess.run(["git", "pull", "--rebase", "origin", "main"],
                              cwd=ROOT, capture_output=True, text=True)
        err = pull.stderr.strip()
        if pull.returncode == 0:
            push = subprocess.run(["git", "push", "origin", "main"],
                                  cwd=ROOT, capture_output=True, text=True)
            if push.returncode == 0:
                print(f"checkpoint pushed for {slug}")
                return
            err = push.stderr.strip()
        else:
            # During a rebase stage 3 is our own commit, so keep our version of
            # any feed/state file that conflicts with upstream changes.
            conflicted = subprocess.run(
                ["git", "diff", "--name-only", "--diff-filter=U"],
                cwd=ROOT, capture_output=True, text=True,
            ).stdout.split()
            if conflicted:
                subprocess.run(["git", "checkout", "--theirs", "--", *conflicted],
                               cwd=ROOT, check=False, capture_output=True, text=True)
                subprocess.run(["git", "add", "-A"], cwd=ROOT, check=False)
                env = dict(os.environ, GIT_EDITOR="true")
                cont = subprocess.run(["git", "rebase", "--continue"],
                                      cwd=ROOT, env=env, capture_output=True, text=True)
                if cont.returncode != 0:
                    # Our commit became empty after resolution; upstream has it.
                    subprocess.run(["git", "rebase", "--skip"],
                                   cwd=ROOT, env=env, capture_output=True, text=True)
                    subprocess.run(["git", "rebase", "--abort"], cwd=ROOT,
                                   check=False, capture_output=True, text=True)
                    print(f"checkpoint already present for {slug}")
                    return
                push = subprocess.run(["git", "push", "origin", "main"],
                                      cwd=ROOT, capture_output=True, text=True)
                if push.returncode == 0:
                    print(f"checkpoint pushed for {slug}")
                    return
                err = push.stderr.strip()
        print(f"checkpoint push retry {attempt}: {err}", file=sys.stderr)
        subprocess.run(["git", "rebase", "--abort"], cwd=ROOT,
                       check=False, capture_output=True, text=True)
        time.sleep(15)
    raise RuntimeError(f"could not push checkpoint for {slug}")


def save_progress(show, eps, seen, sf, seen_sf, checkpoint=True):
    """Persist state, ledger and feed; optionally push a checkpoint commit."""
    slug = show["slug"]
    sf.write_text(json.dumps(eps, indent=1))
    seen_sf.parent.mkdir(parents=True, exist_ok=True)
    seen_sf.write_text(json.dumps(sorted(seen), indent=1))
    feed_dir(slug).mkdir(parents=True, exist_ok=True)
    (feed_dir(slug) / "feed.xml").write_text(build_feed(show, eps))
    if checkpoint:
        commit_checkpoint(slug)


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

            if REPAIR_DATES:
                # Read the uploadDate JSON-LD field from each watch page. This
                # is lighter and far more reliable than yt-dlp metadata calls,
                # and it never downloads audio.
                by_id = {e["id"]: e for e in eps}

                def fetch_date(ep):
                    vid = ep["id"]
                    url = f"https://www.youtube.com/watch?v={vid}"
                    page_error = ""
                    try:
                        req = urllib.request.Request(
                            url,
                            headers={"User-Agent": "Mozilla/5.0 (compatible; podcast-feed-repair)"},
                        )
                        with urllib.request.urlopen(req, timeout=20) as resp:
                            html = resp.read().decode("utf-8", "ignore")
                        m = re.search(r'"uploadDate"\s*:\s*"([^"]+)"', html)
                        if not m:
                            raise ValueError("uploadDate not found in watch page")
                        pub = datetime.datetime.fromisoformat(
                            m.group(1)
                        ).astimezone(datetime.timezone.utc).isoformat()
                        return vid, pub
                    except Exception as ex:
                        page_error = str(ex)

                    # CI IPs are sometimes served a stripped watch page. Fall
                    # back to yt-dlp metadata, which worked for most videos.
                    try:
                        meta = run_retry(
                            ytdlp_base() + ["-J", "--skip-download", url],
                            attempts=3,
                            delay=5,
                            capture_output=True,
                        )
                        m = json.loads(meta.stdout)
                        pub = entry_pub(m)
                        if pub:
                            return vid, pub
                        raise ValueError("no upload date in yt-dlp metadata")
                    except Exception as ex:
                        print(
                            f"[{slug}] {vid} date lookup failed "
                            f"(page: {page_error}; yt-dlp: {ex})",
                            file=sys.stderr,
                        )
                        return vid, None

                fixed, failed = 0, 0
                with concurrent.futures.ThreadPoolExecutor(
                    max_workers=REPAIR_WORKERS
                ) as pool:
                    for vid, pub in pool.map(fetch_date, eps):
                        ep = by_id[vid]
                        if not pub:
                            failed += 1
                            continue
                        if ep.get("pub") != pub:
                            ep["pub"] = pub
                            fixed += 1
                eps.sort(key=lambda e: e["pub"], reverse=True)
                sf.write_text(json.dumps(eps, indent=1))
                feed_dir(slug).mkdir(parents=True, exist_ok=True)
                (feed_dir(slug) / "feed.xml").write_text(build_feed(show, eps))
                row["feed_count"] = len(feed_episodes(eps))
                row["kept"] = len(eps)
                print(f"repaired {fixed} dates for {slug} "
                      f"({failed} metadata failures, {row['feed_count']} in feed)")
                continue

            # Only the newest SCAN_N videos are scanned. Nothing older can
            # ever be downloaded, even during a manual unlimited run.
            entries = list_channel(show)

            have = {x["id"] for x in eps}
            # Members-only videos cannot be fetched with our cookies; skip them
            # before any download is attempted and remember them as processed.
            seen.update(e["id"] for e in entries if is_member_only(e))
            todo = [
                e for e in entries
                if e["id"] not in have
                and e.get("live_status") not in SKIP_LIVE
                and not is_member_only(e)
            ]
            if MAX_NEW > 0:
                todo = todo[:MAX_NEW]
            print(f"{len(entries)} scanned, {len(todo)} not yet in state")

            # Existing releases are restored from metadata only (no download).
            restore = [e for e in todo if e["id"] in seen]
            downloads = [e for e in todo if e["id"] not in seen]
            for e in restore:
                vid = e["id"]
                ep = episode_from_release(show, vid, e)
                if ep:
                    eps.append(ep)
                    row["new"].append((vid, f"{ep['title']} (restored, no download)"))
                    print(f"restored {vid} from release (no download)")
            if restore:
                save_progress(show, eps, seen, sf, seen_sf)

            # Actual downloads run in a bounded pool. Results are applied on the
            # main thread, so state writes stay race-free.
            if downloads:
                print(f"downloading {len(downloads)} episodes with {MAX_WORKERS} workers")
                since_checkpoint = 0
                with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
                    futures = {
                        pool.submit(download_one, show, e): e["id"] for e in downloads
                    }
                    for future in concurrent.futures.as_completed(futures):
                        vid, ep, err = future.result()
                        if ep is None and err is None:
                            seen.add(vid)
                            print(f"[{slug}] {vid} skipped (release exists)")
                        elif err == "member-only":
                            seen.add(vid)
                            print(f"[{slug}] {vid} skipped (members-only)")
                        elif ep is None:
                            failed += 1
                            row["failed"].append((vid, err or "download failed"))
                            print(f"FAILED {vid}: {err}", file=sys.stderr)
                        else:
                            eps.append(ep)
                            seen.add(vid)
                            row["new"].append((vid, ep["title"]))
                            print(f"done {vid}")
                        since_checkpoint += 1
                        if since_checkpoint >= CHECKPOINT_EVERY:
                            save_progress(show, eps, seen, sf, seen_sf)
                            since_checkpoint = 0
                if since_checkpoint:
                    save_progress(show, eps, seen, sf, seen_sf)

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
    # Backfill runs report individual video failures but still let the chain
    # continue; the regular scheduled run stays strict.
    sys.exit(1 if failed and not ALLOW_FAILURES else 0)


if __name__ == "__main__":
    main()
