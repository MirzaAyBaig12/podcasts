# Notes for future OpenCode sessions

- This repo publishes three YouTube channels as separate RSS feeds via GitHub Pages + Releases.
- Single source of truth is `config.json`; do not hand-edit `feeds/*/feed.xml`, `state/*.json`, or `index.html` — `scripts/update.py` regenerates them.
- A video is downloaded only if its ID is missing from `state/<slug>.json` **and** it has no existing release; `make_episode()` checks the release before yt-dlp. `seen/<slug>.json` is a secondary ledger for releases missing from state; never remove entries from it.
- Only the newest `scan_count` (20) channel videos are scanned, so older uploads are never downloaded automatically.
- Keep release tags namespaced (`<slug>-yt-<id>`). Removing the prefix would cause cross-show collisions.
- `cookies.txt` is gitignored. In CI it comes from the `YT_COOKIES` secret. Never commit cookie contents.
- Channel descriptions are static in `config.json` (pulled once on purpose). Per-video descriptions are refreshed from yt-dlp each run.
- Limits: `feed_episodes`, `keep_releases` and `max_new_per_run` are all `0` (unlimited / no deletion). Only `scan_count` is bounded (20 newest videos) so CI only looks for new uploads. Cron runs every 15 minutes.
- All existing releases were backfilled into state manually once; feeds now list every release with no cap.
- Old standalone repos were retired locally; the owner handles GitHub-side deletion.
- Operator is Ayaan Mirza (@MirzaAyBaig12). Prefer `gh` CLI over raw API where possible.
