# Installation Guide

This covers every way to run Media Manager: a quick local/bare-metal setup,
Docker, and Kubernetes (via the bundled Helm chart), plus how the CLI, config,
and integrations fit together.

## Contents
- [Prerequisites](#prerequisites)
- [Quick install (bare metal)](#quick-install-bare-metal)
- [Configuration reference](#configuration-reference)
- [Running as background services (systemd)](#running-as-background-services-systemd)
- [Docker](#docker)
- [Kubernetes (Helm)](#kubernetes-helm)
- [First login & configuring integrations](#first-login--configuring-integrations)
- [Configuring Plex/Emby/Jellyfin playback tracking](#configuring-plexembyjellyfin-playback-tracking)
- [Updating](#updating)
- [Uninstalling](#uninstalling)
- [Troubleshooting](#troubleshooting)
- [H.265 transcode worker](#h265-transcode-worker-host-nvidia-gpu)
- [Fixing library ownership](#fixing-library-ownership-root-owned-files)

## Prerequisites

- **Python 3.10+**
- **ffmpeg** (provides `ffprobe`, used to read media file technical metadata)
- **git**
- Optional: **[FileBot](https://www.filebot.net/)** if you want the watcher to
  auto-rename/organize files after moving them (`FILEBOT_SCRIPTS_PATH`); the
  watcher still works without it, it just won't run the FileBot AMC step.
  AMC files each arrival under its library root as `Show/Season 1/1x05 - Title.ext`
  and `Movie/Movie (Year).ext` (`series_format()` / `MOVIE_FORMAT` in
  `media_manager/filebot.py`), so no separate scheduled `filebot -rename` pass is
  needed to tidy new downloads. For Sonarr-named episodes (`Show - 1x05 - Title`)
  AMC is given `--q "<Show>"` and files under that exact folder, so ambiguous
  names (two "Tracker" series) still resolve in strict mode; other names fall
  back to FileBot's own name, reusing an existing `Show (Year)` folder. If AMC
  can't identify a Sonarr-named episode, the watcher files it from the name
  (`file_sonarr_episode`) unless that episode number is already in the season
  folder. Anything else unidentified stays loose in the library root, invisible
  to Plex.
- For the Kubernetes path: a working cluster, `kubectl`, and `helm`.
- For Docker: `docker`.

Debian/Ubuntu system packages:
```bash
sudo apt-get update
sudo apt-get install -y python3 python3-venv ffmpeg git
```

## Quick install (bare metal)

```bash
git clone <this-repo-url> media-manager
cd media-manager
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Or just run `./setup.sh`, which does the above (plus the systemd service
install below) interactively - see
[Running as background services](#running-as-background-services-systemd).

Start the web portal:
```bash
python3 main.py serve --host 0.0.0.0 --port 8088
```

Open `http://localhost:8088`. If no users exist yet, the first login as
`INITIAL_ADMIN_USER` with the password in `INITIAL_ADMIN_PASSWORD` creates the
admin account, and the app then asks you to change that password.
`INITIAL_ADMIN_PASSWORD` has no default: if it is unset, no admin is created
and every login fails. `./setup.sh` generates one and writes it to `.env`.

## Configuration reference

Everything is configured via environment variables (see
`media_manager/config.py` for the source of truth). None are required to get
a working local instance except `INITIAL_ADMIN_PASSWORD` (see above). The
rest runs on defaults: SQLite database, no external integrations configured.

| Variable | Default | Purpose |
|---|---|---|
| `WEB_HOST` | `0.0.0.0` | Web server bind address |
| `WEB_PORT` | `8088` | Web server port |
| `DB_PATH` | `media_manager/media.db` | SQLite file path (used unless `USE_POSTGRES=true`) |
| `USE_POSTGRES` | `false` | Set `true` to use Postgres instead of SQLite |
| `POSTGRES_URI` | `postgresql+asyncpg://mediauser:mediapass@localhost:5432/mediadb` | Async SQLAlchemy URI, only read if `USE_POSTGRES=true` |
| `SYNC_POSTGRES_URI` | derived from `POSTGRES_URI` | Sync driver URI (for CLI commands); auto-derived, rarely needs setting directly |
| `JWT_SECRET` | `dev-secret-key-change-in-production` | **Change this in any real deployment** - signs session tokens |
| `JWT_EXPIRATION_HOURS` | `720` | Session token lifetime |
| `RP_ID` | `localhost` | WebAuthn/passkey relying party ID - set to your actual domain if using passkeys |
| `RP_NAME` | `Media Manager Portal` | WebAuthn/passkey display name |
| `INITIAL_ADMIN_USER` | `admin` | Bootstrap admin username (created on first login attempt if no users exist) |
| `INITIAL_ADMIN_PASSWORD` | none (required) | One-time bootstrap admin password; unset means no admin is created. Changed on first login |
| `MEDIA_DIRECTORIES` | example paths in `config.py` | Comma-separated list of directories to scan/search (e.g. `/media/Movies,/media/TvShows`) |
| `SOURCE_DIRECTORIES` | example paths in `config.py` | JSON array of `{"from_dir","to_dir","type"}` objects for the watcher - see below |
| `FILEBOT_SCRIPTS_PATH` | `~/filebot_scripts` | Path to FileBot AMC scripts, if installed |
| `PLAYBACK_SYNC_INTERVAL_HOURS` | `6` | How often the Plex/Emby/Jellyfin playback sync runs in the background |

**`SOURCE_DIRECTORIES` example** (what the automated watcher monitors and
where it moves completed downloads to):
```bash
export SOURCE_DIRECTORIES='[
  {"from_dir": "/downloads/movies", "to_dir": "/mnt/Movies", "type": "movie"},
  {"from_dir": "/downloads/tv", "to_dir": "/mnt/TvShows", "type": "tv"}
]'
```
The defaults in `config.py` are examples - **set this for your setup**,
or the watcher will log
warnings that those directories don't exist and do nothing.

Radarr, Sonarr, SABnzbd, TMDb, Fanart.tv, and SMTP are **not** configured via
environment variables - see
[First login & configuring integrations](#first-login--configuring-integrations).

## Running as background services (systemd)

There are three long-running pieces:
- **Web portal** (`main.py serve`) - the UI/API. This is the only process that opens the database.
- **Watcher** (`main.py watch`) - monitors `SOURCE_DIRECTORIES` and moves
  completed downloads into your library. Only relevant if you use it for
  that; skip it if you just want the web portal (inventory browsing,
  duplicate detection, Radarr/Sonarr/SABnzbd dashboards, etc.).
- **H.265 transcoder** (`main.py transcode-worker`) - host NVIDIA encode
  worker. It talks to the portal over HTTP and does not open the database.
  See [H.265 transcode worker](#h265-transcode-worker-host-nvidia-gpu).

Install the watcher as a systemd service:
```bash
./install.sh
```
This detects the current user/directory and creates
`media-manager-watcher.service`, enabled and started. The unit POSTs a
heartbeat to `http://127.0.0.1:18088/api/v1/watcher/heartbeat` (a NodePort
to the portal running in Kubernetes) so Admin/Logs can show liveness. Override with
`WATCHER_HEARTBEAT_URL` if the portal is elsewhere. Manage it with the
usual `sudo systemctl {start,stop,status} media-manager-watcher` /
`sudo journalctl -u media-manager-watcher -f`. Remove it with `./uninstall.sh`.

There's no bundled systemd unit for the web portal since most people run it
via Docker/Kubernetes (below) - but the pattern is identical if you want one:
```ini
[Unit]
Description=Media Manager Web Portal
After=network.target

[Service]
Type=simple
User=youruser
WorkingDirectory=/path/to/media-manager
ExecStart=/path/to/media-manager/.venv/bin/python3 main.py serve --host 0.0.0.0 --port 8088
Restart=always
RestartSec=10
EnvironmentFile=-/path/to/media-manager/.env

[Install]
WantedBy=multi-user.target
```

## Docker

```bash
docker build -t media-manager:latest .
docker run -d --name media-manager \
  -p 8088:8088 \
  -e JWT_SECRET="$(openssl rand -hex 32)" \
  -e INITIAL_ADMIN_PASSWORD="a-real-password" \
  -v media-manager-config:/config \
  -e DB_PATH=/config/media.db \
  -v /path/to/your/movies:/mnt/Movies \
  -v /path/to/your/tv:/mnt/TvShows \
  -e MEDIA_DIRECTORIES="/mnt/Movies,/mnt/TvShows" \
  media-manager:latest
```

The watcher (`main.py watch`) isn't started by the container's default
command (only `serve` is) - run a second container with the same image and
`command: ["python3", "main.py", "watch"]` (and the same volumes/env) if you
want automated file moving too.

## Kubernetes (Helm)

This repo's `k8s/helm/` chart is a **standalone/generic** install path.


For a generic install from this repo:

```bash
helm install media-manager k8s/helm \
  --set-string secrets.JWT_SECRET="$(openssl rand -hex 32)" \
  --set-string secrets.INITIAL_ADMIN_PASSWORD="$(openssl rand -base64 18)"
```

Key values to review in `k8s/helm/values.yaml` before installing (or override
with `--set`):
- `image.repository` / `image.tag` - where your built image lives (a local
  tag, or a registry path)
- `env.USE_POSTGRES` / `secrets.POSTGRES_URI` - only needed if you want
  Postgres instead of the default SQLite-on-a-PVC
- `mediaPersistence.mounts` - your actual media library PVCs; disabled by
  default (empty), since these are specific to your storage setup
- `ingress.enabled` - off by default; turn on and set `ingress.host` (and
  `ingress.tls.enabled` + a cert-manager annotation, or your own TLS secret)
  if you want external HTTPS access

The chart always provisions and mounts a `configPersistence` PVC at
`/config` for the SQLite database - disable it (`configPersistence.enabled:
false`) only if you're using Postgres and don't need any other persistent
app state.

Verify:
```bash
kubectl get pods -l app=media-manager
kubectl port-forward svc/media-manager 8088:8088
```

## First login & configuring integrations

1. Log in with the bootstrap admin credentials (`INITIAL_ADMIN_USER` /
   `INITIAL_ADMIN_PASSWORD`), then immediately change the password (the
   **Password** button next to your username).
2. Go to **Admin → Setup** and open a service row to connect external
   services. They are stored in the database, not via environment
   variables:
   - **Radarr** / **Sonarr**: API URL + API key (found in each app's
     Settings → General)
   - **SABnzbd**: API URL + API key (Settings → General in SABnzbd)
   - **TMDb**: a free API key from
     [themoviedb.org/settings/api](https://www.themoviedb.org/settings/api)
     - powers posters, metadata, Coming Soon, Trending, Calendar enrichment, and title detail
   - **Syncthing**: API URL + API key — folder sync status on Downloads
   - **Fanart.tv**: a free API key from
     [fanart.tv](https://fanart.tv/get-an-api-key/) - supplemental artwork
   - **Email (SMTP)**: for 2FA codes, password-recovery emails, and H.265 migration summaries
3. Go to **Admin → Paths** to add the directories the scanner should walk.
4. Go to **Admin → Users** to create real accounts for anyone else who needs
   access, and to see last-login times / force password resets.

Every integration is optional - the app degrades gracefully (e.g. no
Radarr/Sonarr configured just means those dashboard sections show a "not
configured" message instead of data).

## Configuring Plex/Emby/Jellyfin playback tracking

A background service pulls per-movie/per-episode play counts and
last-played timestamps from any combination of Plex, Emby, and Jellyfin,
matches them to your inventory, and shows them on Inventory cards, the
title detail modal, and **Admin → Playback**. The Library Cleanup wizard
uses the same data to suggest unwatched titles. Like Radarr/Sonarr, these
sources are configured in the database via the API rather than environment
variables. There is still no Admin form for the URL and token, so use
`curl` (as an already logged-in admin, reusing your session cookie or
bearer token):

```bash
# Plex: api_key is a Plex token (Settings > Account, or see
# https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/)
curl -X PUT http://localhost:8088/api/v1/playback/config/plex \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"api_url": "http://plex:32400", "api_key": "YOUR_PLEX_TOKEN"}'

# Emby / Jellyfin: api_key is an API key generated under
# Dashboard > Advanced > API Keys
curl -X PUT http://localhost:8088/api/v1/playback/config/jellyfin \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"api_url": "http://jellyfin:8096", "api_key": "YOUR_API_KEY"}'
```

If the media server sees your library under a different mount path than
this app does (e.g. a container sees `/data/Movies` while this app scanned
`/mnt/Movies`), add `"path_mappings": [{"source_prefix": "/data", "local_prefix": "/mnt"}]`
to the request body so the sync can still match files by path.

Trigger a sync immediately with `POST /api/v1/playback/sync` (admin) or
`python3 main.py sync-playback`, instead of waiting for the scheduled
interval (`PLAYBACK_SYNC_INTERVAL_HOURS`, default 6). Read the data back via
`GET /api/v1/playback/items` (per movie/episode) and `GET
/api/v1/playback/series` (per TV series rollup).

## Updating

**Bare metal:**
```bash
git pull
pip install -r requirements.txt
sudo systemctl restart media-manager-watcher      # if installed
sudo systemctl restart media-manager-transcoder   # if installed
# restart your `serve` process
```

**Docker:** rebuild the image and recreate the container.

**Kubernetes:** `helm upgrade media-manager k8s/helm` after pushing/pulling a
new image tag.

Schema changes are applied automatically on startup (`init_db_schema` /
`init_db_schema_sync` in `media_manager/database.py`) - no separate migration
step needed.

## Uninstalling

```bash
./uninstall.sh   # removes the watcher systemd service, if installed
```
Then just delete the directory / stop and remove the container / `helm
uninstall media-manager` as appropriate. Your database (`media.db` or your
Postgres instance) isn't touched by any of these - delete it separately if
you want a clean slate.

## Troubleshooting

For watcher issues (files not moving, permission errors), check
`media_manager.log` and the in-app Logs view first.

For anything else, check the logs:
- Bare metal watcher: `media_manager.log` in the project root, or
  `journalctl -u media-manager-watcher -f` if installed as a service
- Web portal: stdout, or wherever your process manager/container captures it
- The in-app **Logs** view (admin only) streams live structured log events

## H.265 transcode worker (host, NVIDIA GPU)

The H.265 migration encodes on the host's GPU with a small stdlib-only worker,
`media-manager-transcoder.service`. It never opens a database: it claims jobs
from, and reports back to, the web app over `/api/v1/transcode/worker/*`.

1. **Shared token.** The web pod reads `TRANSCODE_WORKER_TOKEN` from the
   environment (in Kubernetes, from a Secret). The worker endpoints return
   503 until it is set - there is no default.
2. **Env file** (mode 600), default `~/.config/media-manager/transcoder.env`:

   ```
   TRANSCODE_API_URL=https://media.example.com
   TRANSCODE_WORKER_TOKEN=<same value as the Secret>
   TRANSCODE_SCRATCH_DIR=/var/tmp/mm-transcode-scratch   # local disk, NOT inside a library/download dir
   FFMPEG_BIN=/usr/bin/ffmpeg
   FFPROBE_BIN=/usr/bin/ffprobe
   TRANSCODE_GPU_INDEX=0
   TRANSCODE_TLS_VERIFY=1
   # optional: TRANSCODE_ENCODER=libx265 (CPU), TRANSCODE_SAMPLE_CHECK=0, TRANSCODE_WORKER_ID=<name>
   ```
3. **Install the unit:** `sudo bash scripts/install_transcoder.sh`. It seeds the
   dedicated worker checkout `~/media-manager-worker` (CI keeps it in sync on
   every deploy) and starts the service.
4. **Check it:** `journalctl -u media-manager-transcoder -f` should show it
   heartbeating; the H.265 Migration page shows the worker as online. Nothing
   converts until an admin picks a mode (Pilot / Full) and presses Start.

The host ffmpeg must have `hevc_nvenc` (`ffmpeg -hide_banner -encoders | grep nvenc`).
`python3 main.py transcode-worker --once` runs a single job in the foreground for testing.

5. **Optional - nightly GPU self-heal:** `sudo bash scripts/install_gpu_reboot_timer.sh`
   installs `media-manager-gpu-reboot.timer`. At 03:30 local time it reboots the
   host only if `nvidia-smi` reports a driver/library version mismatch (an
   unattended NVIDIA upgrade). `--uninstall` removes it. See
   `docs/runbooks/nvidia-driver-mismatch.md`.


## Fixing library ownership (root-owned files)

The web pod runs as root, so folders and files it creates in the library
(`.nfo` metadata, new season folders) end up owned by root. Host processes
that run as the media user - the transcode worker and the watcher - then can't write
there; the H.265 migration skips those files as `permission_denied`.

`scripts/fix_permissions.sh` puts everything back to the media user (UID/GID 1000)
with group-writable folders (775) and files (664). It only *adds* permission
bits, skips CIFS/NFS shares (their ownership comes from the mount options) and
empty/unmounted folders, and is a dry run unless you pass `--apply`:

```
bash scripts/fix_permissions.sh                 # report only
sudo bash scripts/fix_permissions.sh --apply    # fix every library folder
sudo bash scripts/fix_permissions.sh --apply "/media/TvShows/Some Show"   # just one folder
```

Re-run it after anything recreates root-owned files; skipped transcode jobs
can then be requeued from the H.265 Migration page.
