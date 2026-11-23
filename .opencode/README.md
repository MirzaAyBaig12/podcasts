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
5. First successful full build produced 20 episodes per show in each feed.

## Layout

```
config.json              shows + limits (single source of truth)
scripts/update.py        all logic
.github/workflows/update.yml   hourly cron
feeds/<slug>/feed.xml    published RSS
feeds/<slug>/cover.jpg   square channel avatar, also used on every item
state/<slug>.json        newest 100 episodes per show
seen/<slug>.json         append-only list of every video ID ever processed
index.html               generated landing page
cookies.txt              NEVER committed (gitignored; CI writes from secret)
```

## Settings (in config.json)

- `max_new_per_run`: 20 new videos per show per run
- `feed_episodes`: 20 newest episodes published in each feed
- `keep_releases`: 100 episodes kept per show; older releases are deleted
- `art_size`: 1400px square cover
- workflow cron: `0 * * * *` (hourly)
- release tags: `<slug>-yt-<videoid>` (namespaced so IDs can't collide)

## Description handling

- Podcast/channel `<description>` was **pulled once** from each YouTube channel's About text and written into `config.json`. It is not refetched every run.
- Each episode item's `<description>` is pulled from that video's YouTube description on every run, stored in state, and rendered in full (no truncation). XML-illegal control characters are stripped.

## Never re-downloading episodes

`state/<slug>.json` only holds the newest 100 episodes, so it can't be the dedupe source. `seen/<slug>.json` is an append-only ledger of every video ID ever processed.

At the start of each show, the ledger is unioned with:
- IDs in current state
- IDs parsed from existing release tags (`<slug>-yt-<id>`)

That second source covers runs interrupted before the ledger was committed. A video ID in the ledger is skipped before yt-dlp is invoked, so no audio is downloaded twice.

## Auth / gotchas

- `YT_COOKIES` secret holds a Netscape-format `cookies.txt` exported from a logged-in YouTube session. It was set on this repo; the local Downloads copy was deleted.
- GitHub Actions IPs get bot-challenged by YouTube; cookies are the mitigation, but they expire and may need re-exporting.
- Free GitHub Pages + Releases is public. Keep it personal.
- PPSW's feed describes the owner's permission; the other two do not claim it.
- The old repos' Pages sites/releases were left for the owner to delete on GitHub.

## Useful commands

```bash
# trigger a run
gh workflow run update.yml --repo MirzaAyBaig12/podcasts --field max_new=20

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
3. Commit and push; the next hourly run builds its cover, feed and releases.
