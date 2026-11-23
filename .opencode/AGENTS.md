# Notes for future OpenCode sessions

- This repo publishes three YouTube channels as separate RSS feeds via GitHub Pages + Releases.
- Single source of truth is `config.json`; do not hand-edit `feeds/*/feed.xml`, `state/*.json`, or `index.html` — `scripts/update.py` regenerates them.
- Dedupe is `seen/<slug>.json`, not `state/`. Never remove entries from `seen/`; that would cause re-downloads.
- Keep release tags namespaced (`<slug>-yt-<id>`). Removing the prefix would cause cross-show collisions.
- `cookies.txt` is gitignored. In CI it comes from the `YT_COOKIES` secret. Never commit cookie contents.
- Channel descriptions are static in `config.json` (pulled once on purpose). Per-video descriptions are refreshed from yt-dlp each run.
- Limits: 20 episodes in each feed, 100 kept per show, 20 new per run, hourly cron.
- Old standalone repos were retired locally; the owner handles GitHub-side deletion.
- Operator is Ayaan Mirza (@MirzaAyBaig12). Prefer `gh` CLI over raw API where possible.
