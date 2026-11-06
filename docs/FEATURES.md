# Features

The full feature list. The [README](../README.md) covers the five main ones.

## Web portal

Single page app with a text top nav (Library, Find, Requests, Calendar, Help, and Admin for operators). On a phone those links sit in a bottom bar. Admin opens Setup, Paths, Users, Playback, Downloads, Duplicates, Cleanup, H.265, and Logs. Light/Dark/System theme modes.

The logo (`media_manager/web/static/img/logo.svg`) is three spines on a shelf: the tall one carries a play symbol and the right one leans in, like a new file being filed. It is the favicon (SVG, plus a 180px `apple-touch-icon.png`), the header mark (inline, drawn in the theme's accent color), and the sign-in logo.

## Media inventory and search

Scans all mounted media directories into SQLite/PostgreSQL, extracting technical metadata (resolution, audio codecs, channels, bitrate, size, creation date) with instant real-time search. Unified Search (local + Radarr/Sonarr) time-boxes remote lookup and TMDb card enrichment so a broad query like "batman" returns JSON instead of a gateway HTML error.

## Duplicate detection

Groups duplicate media files, ranks them by quality score (4K > 1080p, Atmos > TrueHD > DTS, bitrate tie-breaker), and highlights the best version.

## Quality-based file mover

Monitors source directories for new files. Verifies copy file size matching (`[COPY_VERIFIED]`) before safely truncating source files (`[SOURCE_TRUNCATED]`).

## Live log viewer and audit trail

Streams live structured log events via Server-Sent Events (SSE) and records complete audit trails of all file moves in database.

## Authentication, roles, and recovery

Password and WebAuthn / Passkey authentication, 2FA, forced password reset after an admin-initiated change, and self-service **Forgot Password** recovery (via email/SMS verification code).

## Radarr, Sonarr, and SABnzbd integrations

Live queue and download-history dashboards, one-click add to Radarr/Sonarr, and Recyclarr/TRaSH Guides custom-format sync.

## Coming soon (movies and TV)

TMDb-powered discovery of upcoming theatrical/streaming movie releases and new TV series premieres in the next 30/60/90/120 days, with cast, trailers, and one-click add to Radarr/Sonarr.

## Radarr/Sonarr download queue

Live per-movie/episode grab-to-import progress on the Downloads dashboard, alongside the existing SABnzbd and Syncthing views.

## My requests

Per-user view of everything you've requested and its live status (requested → downloading → available), computed against the local library and Radarr/Sonarr.

## Trending now

TMDb-powered "popular right now" discovery for movies/TV, independent of release date - complements Coming Soon's release-date-driven view. Enrichment is time-boxed so a slow TMDb/DNS stall returns titles (or a clear timeout message) instead of a gateway HTML error.

## Title detail with similar and recommended titles

Click any poster/title (Search, Coming Soon, Trending, Calendar) for a full detail view with a "More Like This" row for further discovery.

## Disk space monitoring

Real-time monitoring of all monitored storage directories with color-coded capacity thresholds.

## SMTP notifications

For 2FA login codes and password recovery.

## Download-stack alerts

Admins get one email when Radarr, Sonarr, or SABnzbd stops responding or Radarr/Sonarr can't reach its download client, and one when it recovers. The status shows on Admin > Integrations.

## Playback tracking (Plex, Emby, Jellyfin)

Background service that pulls per-movie/per-episode play counts and last-played timestamps from any configured combination of Plex, Emby, and Jellyfin, merges them across sources, and shows them on Inventory cards, the TV show total-plays line, and the title detail modal - plus a sortable read-only **Admin → Playback** page (name filter, in-page scroll) listing every movie/series. See [SYSTEM_DESIGN.md](SYSTEM_DESIGN.md).

## Library cleanup wizard (admins only)

A 3-step wizard (Settings → Review → Confirm) that suggests - but never automatically deletes - movies and whole TV series nobody's watched, grouped by media type with configurable "unwatched for N days" thresholds (sensible researched defaults included). Every deletion is explicitly reviewed and confirmed by an admin.

## Empty-file re-download

Finds library files that are 0-byte placeholders and, with one admin click on the Cleanup page, asks Radarr/Sonarr to download them again.

## H.265 migration (admins only)

Converts h264/XviD files to HEVC on the host's NVIDIA GPU to reclaim space - dry-run savings report, a hand-picked pilot, then the whole library biggest-wins-first. In full mode, new downloads are auto-queued after they have settled for an hour. The page shows total space saved and how much is actually freed once kept originals expire. Every file is verified before it replaces the original (kept for a configurable number of days), the worker yields the GPU to Plex/Jellyfin/Emby and never swaps a file someone is watching, and admins get daily summary and failure emails, plus an alert when the GPU becomes unusable (an optional nightly timer reboots the host at 03:30 to fix an NVIDIA driver mismatch).

## Codec metadata

Every scan records each file's real video codec, HDR format (SDR/HDR10/HLG/Dolby Vision + profile), bit depth, video bitrate, container, duration and stream counts. The H.265 migration uses these columns to pick candidates. Admins can backfill existing rows via `POST /api/v1/transcode/probe-backfill`.

## Docker and Kubernetes

Dockerfile plus a complete Helm chart (`k8s/helm/`); see [INSTALL.md](INSTALL.md).

## CLI commands

```bash
python3 main.py serve            # Start the web portal
python3 main.py watch             # Start the automated file-move watcher
python3 main.py scan              # Scan all MEDIA_DIRECTORIES into the inventory
python3 main.py sync-metadata     # Refresh cached TMDb metadata/cast/trailers
python3 main.py sync-playback     # Pull play counts/last-played from Plex/Emby/Jellyfin
python3 main.py redownload-empty  # Preview re-downloading 0-byte library files (--yes to trigger)
python3 main.py transcode-worker  # Host H.265 NVENC worker (normally runs as media-manager-transcoder.service)
bash scripts/fix_permissions.sh    # Report root-owned library files (sudo ... --apply to give them back to the media user)
bash scripts/run_tests.sh         # Run the automated test suite (isolated DB)
python3 main.py reset-password    # Reset (or create) an admin user's password
```
