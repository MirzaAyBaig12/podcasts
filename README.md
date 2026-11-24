# Podcasts

Three independent podcast feeds generated from YouTube channels, hosted on GitHub Pages with audio in GitHub Releases.

## Feeds

- Pente Patrol Star Wars — `https://mirzaaybaig12.github.io/podcasts/feeds/ppsw/feed.xml`
- Fantasy Folklore — `https://mirzaaybaig12.github.io/podcasts/feeds/fantasy-folklore/feed.xml`
- The Star Wars Galaxy — `https://mirzaaybaig12.github.io/podcasts/feeds/star-wars-galaxy/feed.xml`

Landing page: `https://mirzaaybaig12.github.io/podcasts/`. Each show has its own cover, feed and state; nothing is shared between shows except the workflow.

## How it works

The workflow in `.github/workflows/update.yml` runs every 15 minutes (`7,22,37,52 * * * *`) and runs `scripts/update.py`:

- scans only the **20 newest videos** per channel (`scan_count`) so old uploads are never backfilled; this is the only source of new episodes
- treats a video as new only when its ID is absent from `state/<slug>.json` **and** has no existing release
- skips anything already downloaded — a release check runs before yt-dlp, so existing episodes are never downloaded again
- downloads audio for genuinely new videos with yt-dlp + ffmpeg
- uploads one release per episode, tagged `<slug>-yt-<videoid>`
- writes **every episode in state** to `feeds/<slug>/feed.xml` — no feed cap
- **never deletes releases** — no auto-clean, no retention limit
- also records processed IDs in `seen/<slug>.json` as a secondary ledger

Limits in `config.json` are `0` for `max_new_per_run`, `feed_episodes` and `keep_releases`, which means unlimited in each case. Only `scan_count` stays bounded (20) so the job only looks for new videos. (The workflow passes `MAX_NEW=20`, which matches the scan window, so it doesn't restrict anything further.)

Channel descriptions are stored once in `config.json`; each episode's description is pulled from its YouTube video description on every run.

The `YT_COOKIES` repository secret holds a Netscape-format `cookies.txt` exported while logged into YouTube. Never commit it.

## Reuse this for your own podcasts

1. **Fork this repo** (or make a fresh one with the same layout) and enable **GitHub Pages** on `main` at `/`.
2. **Edit `config.json`**:
   - set `repo` to `YourUser/YourRepo` and `pages_url` to `https://youruser.github.io/YourRepo`
   - replace the `shows` array with your channels — each needs a unique `slug`, a `channel_url` ending in `/videos`, and title/author/category
   - leave `max_new_per_run`, `feed_episodes` and `keep_releases` at `0` for unlimited, or set numbers if you want caps
   - `scan_count` controls how many newest videos are checked for new uploads; keep it small
3. **Add the cookie secret** (only needed if YouTube bot-challenges the runner):
   ```bash
   gh secret set YT_COOKIES --repo YourUser/YourRepo < ~/Downloads/www.youtube.com_cookies.txt
   ```
4. **Add initial state files**: `state/<slug>.json` containing `[]` and `seen/<slug>.json` containing `[]`. The script creates everything else on the first run.
5. **Trigger the first run**:
   ```bash
   gh workflow run update.yml --repo YourUser/YourRepo
   ```
   It downloads the newest `scan_count` videos, creates releases, and writes the feeds. If you want older uploads too, raise `scan_count` temporarily for one run, then lower it again.
6. **Add the feed URL** to your podcast app: `https://youruser.github.io/YourRepo/feeds/<slug>/feed.xml`.

Requirements are installed by the workflow automatically: `yt-dlp`, `ffmpeg`, ImageMagick and the `gh` CLI (via `GH_TOKEN`).

If you want a different update cadence, change the `cron` line in `.github/workflows/update.yml`.

See `.opencode/README.md` for the full internal design notes.
