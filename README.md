# World Cup 2026 Tracker — cloud updater

Self-contained builder for Austin's interactive 2026 World Cup tracker.
`build_app.py` pulls the FIFA World Cup schedule/results from ESPN's public
`fifa.world` feed, renders a single self-contained `index.html`, and uploads
it to the `austin-brief-audio` S3 bucket at a stable, pinned URL.

## How it runs
A GitHub Actions workflow (`.github/workflows/update.yml`) runs the builder
twice daily (≈7am & 7pm Pacific) and on manual dispatch. No personal computer
required — the data stays fresh for anyone with the link.

## Secrets (set in repo Settings → Secrets → Actions)
- `AWS_ACCESS_KEY_ID` — the S3-scoped `brief-audio-uploader` key
- `AWS_SECRET_ACCESS_KEY`

Region is hard-coded to `us-east-2` and the bucket/key prefix live in
`config.json` (the pinned token keeps the public URL stable across rebuilds).

## Note
Time-bound project — slated for retirement after the 2026 World Cup
(see `~/Documents/assistant/memory/active-projects.md`).
