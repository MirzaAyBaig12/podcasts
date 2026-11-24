# OpenCode notes — podcasts repo

This folder records what was built here, why, and how to operate it.

## What this is

One GitHub repo (`MirzaAyBaig12/podcasts`) that publishes **three independent podcast RSS feeds** from YouTube channels, hosted on GitHub Pages with audio in GitHub Releases:

| Show | Slug | Feed |
|---|---|---|
| Pente Patrol Star Wars | `ppsw` | `https://mirzaaybaig12.github.io/podcasts/feeds/ppsw/feed.xml` |
| Fantasy Folklore | `fantasy-folklore` | `https://mirzaaybaig12.github.io/podcasts/feeds/fantasy-folklore/feed.xml` |
| The Star Wars Galaxy | `star-wars-galaxy` | `https://mirzaaybaig12.github.io/podcasts/feeds/star-wars-galaxy/feed.xml` |

Root index: `https://mirzaaybaig12.github.io/podcasts/`

## History

1. Started as three separate repos (`ppsw-podcast`, `fantasy-folklore-podcast`, `star-wars-galaxy-podcast`), each with its own workflow, feed and releases.
2. PPSW was the template. The owner had approved that one; the other two are marked “unofficial feed”.
3. Combined all three into this repo. **No audio, releases, or state were ported** — the new repo downloaded everything fresh.
4. Old repos' update workflows were disabled so they wouldn't run in parallel. Local folders were removed later; GitHub-side deletion was left to the owner (token lacked `delete_repo` scope).
5. First successful full build produced 20 episodes per show; later runs grew state to 60 each.
6. Owner changed the workflow: 15-minute cadence, scan only the newest videos, and "new" means not yet in state.
7. State was backfilled manually from all 258 existing releases; feeds now contain every episode (100 / 85 / 73) with no caps or pruning.

## Layout

```
config.json              shows + limits (single source of truth)
scripts/update.py        all logic
.github/workflows/update.yml   15-minute cron
feeds/<slug>/feed.xml    published RSS (all episodes, no cap)
feeds/<slug>/cover.jpg   square channel avatar, also used on every item
state/<slug>.json        all episodes per show; also the "have" set for dedupe
seen/<slug>.json         secondary append-only ledger of processed video IDs
index.html               generated landing page
cookies.txt              NEVER committed (gitignored; CI writes from secret)
```

## Settings (in config.json)

- `max_new_per_run`: `0` = unlimited new videos within the scan window
- `feed_episodes`: `0` = publish every episode in state
- `keep_releases`: `0` = never delete releases
- `scan_count`: `20` — only the 20 newest channel videos are checked, so old uploads are never backfilled
- `art_size`: 1400px square cover
- workflow cron: `7,22,37,52 * * * *` (every 15 minutes)
- release tags: `<slug>-yt-<videoid>` (namespaced so IDs can't collide)

## CI summary

Every run writes a Markdown table to GitHub's job summary ($GITHUB_STEP_SUMMARY): one row per show with new-episode count, feed count, kept count, and status, followed by details listing each added video title/ID and any failed video or show-level error with its message. The run still exits non-zero if anything failed.

## Description handling

- Podcast/channel `<description>` was **pulled once** from each YouTube channel's About text and written into `config.json`. It is not refetched every run.
- Each episode item's `<description>` is pulled from that video's YouTube description on every run, stored in state, and rendered in full (no truncation). XML-illegal control characters are stripped.

## Never re-downloading episodes

A video is downloaded only when its ID is **not in `state/<slug>.json`** and **has no existing release tag**. `make_episode()` checks `release_exists()` *before* invoking yt-dlp, so a re-download is impossible even if state is missing an entry. If a release exists but its state entry vanished (interrupted run), the entry is rebuilt from release metadata with no download.

`seen/<slug>.json` is maintained as a secondary ledger, seeded from existing release tags (`<slug>-yt-<id>`), and is used to detect that "already processed but missing from state" case. The channel scan is bounded by `scan_count` (20) and YouTube's `/videos` order, so only the newest uploads are ever considered; older videos are never backfilled by CI. The one-time backfill of all existing releases was done manually.

## Auth / gotchas

- `YT_COOKIES` secret holds a Netscape-format `cookies.txt` exported from a logged-in YouTube session. It was set on this repo; the local Downloads copy was deleted.
- GitHub Actions IPs get bot-challenged by YouTube; cookies are the mitigation, but they expire and may need re-exporting.
- Free GitHub Pages + Releases is public. Keep it personal.
- PPSW's feed describes the owner's permission; the other two do not claim it.
- The old repos' Pages sites/releases were left for the owner to delete on GitHub.

## Useful commands

```bash
# trigger a run (workflow default max_new=20; pass 0 for unlimited within the scan window)
gh workflow run update.yml --repo MirzaAyBaig12/podcasts

# watch latest run
gh run list --repo MirzaAyBaig12/podcasts --workflow update.yml --limit 1 \
  --json databaseId,status,conclusion

gh run watch <RUN_ID> --repo MirzaAyBaig12/podcasts --interval 30 --exit-status

# live step status
gh api repos/MirzaAyBaig12/podcasts/actions/runs/<RUN_ID>/jobs \
  --jq '.jobs[] | {status, steps: [.steps[] | {name, status, conclusion}]}'

# what's been uploaded
gh release list --repo MirzaAyBaig12/podcasts --limit 100

# full / failed logs (after completion)
gh run view <RUN_ID> --repo MirzaAyBaig12/podcasts --log
gh run view <RUN_ID> --repo MirzaAyBaig12/podcasts --log-failed

# refresh cookies if YouTube starts challenging again
gh secret set YT_COOKIES --repo MirzaAyBaig12/podcasts < ~/Downloads/www.youtube.com_cookies.txt
```

## Adding another show

1. Add an object to `shows` in `config.json` with a unique `slug`.
2. Add `seen/<slug>.json` containing `[]` (script creates state/feed/cover on first run).
3. Commit and push; the next 15-minute run builds its cover, feed and releases.
