# Podcasts

Three independent podcast feeds generated from YouTube channels, hosted on GitHub Pages with audio in this repo's Releases.

## Feeds

- Pente Patrol Star Wars — `feeds/ppsw/feed.xml`
- Fantasy Folklore — `feeds/fantasy-folklore/feed.xml`
- The Star Wars Galaxy — `feeds/star-wars-galaxy/feed.xml`

Full URLs are under `https://mirzaaybaig12.github.io/podcasts/`. Each show has its own cover and state; nothing is shared between shows except the workflow.

## How it works

The hourly workflow in `.github/workflows/update.yml` runs `scripts/update.py`:

- downloads audio for new uploads with yt-dlp + ffmpeg
- uploads one release per episode, tagged `<slug>-yt-<videoid>`
- writes each show's feed to `feeds/<slug>/feed.xml` with the newest 20 episodes
- keeps 100 episodes per show in `state/<slug>.json`, deleting older releases

The `YT_COOKIES` repository secret is needed if YouTube challenges the runner with "Sign in to confirm you're not a bot". It is a Netscape-format `cookies.txt` exported while logged into YouTube. Never commit it.
