# Fox Den Music

Fox Den Music is a self-hosted, security-focused music-management service for a
Jellyfin library. Stage 2 adds a live dashboard, a read-only index of the real
music collection, artist/album/health views, a persistent Spotify acquisition
queue, a human-assisted SpotiDownloader handoff, unified history, and small
read-only status APIs.

The Stage 1 importer remains the only processing backend. It accepts an album
ZIP or supported audio file, stages it outside the live library, validates and
inspects it, matches MusicBrainz releases, pauses for ambiguous metadata, tags
a working copy, checks duplicates, assembles a complete album, and publishes it
with one no-overwrite atomic rename.

Stage 2 does **not** automate a browser, CAPTCHA, download, or private provider
endpoint. It does not circumvent DRM or access controls. The user downloads
normally in their own browser and uploads the result to the acquisition job.

## Architecture

The project continues to use one image and two ordinary processes:

- `web`: FastAPI, Jinja2, and small polling fragments. It accepts bounded
  uploads, validates Spotify references, presents acquisition/import history,
  renders library data already indexed in SQLite, and serves `/health` plus the
  read-only `/api/*` endpoints. It does not mount the real `/music` library.
- `worker`: one SQLite-backed consumer. It runs the Stage 1 importer, owns the
  real `/music` mount, synchronizes linked acquisition state, performs
  read-only library scans, refreshes cached storage statistics, and requests
  Jellyfin scans.
- SQLite in `/config`: durable Stage 1 jobs plus separate acquisition, scanned
  inventory, library scan, and storage-snapshot domains. One web process and
  one worker are intentional.
- `/staging/jobs/<import-job-id>`: immutable incoming source plus generated
  extraction and working areas.
- `/staging/acquisitions/<acquisition-id>/incoming`: reserved Stage 3 handoff
  location. Stage 2 creates it but does not watch it; use the acquisition upload
  form.
- `/music/.imports/<import-job-id>/album`: complete pre-publish album.
- `/music/<Album Artist>/<Album> (<Year>)`: final library destination.

See [docs/architecture.md](docs/architecture.md) for state, migration, scan, and
failure details; [docs/container-security.md](docs/container-security.md) for
mount and runtime controls; and
[docs/milestone-2-acquisition.md](docs/milestone-2-acquisition.md) for the exact
manual acquisition boundary.

## Requirements

- Docker Engine with Docker Compose v2
- A local Linux filesystem for `CONFIG_PATH`
- A music path writable by the configured worker UID/GID and readable by
  Jellyfin
- ffmpeg/ffprobe (included in the image)
- Internet access from the worker for MusicBrainz and Cover Art Archive
- A contact email or URL for the required MusicBrainz User-Agent
- Optional: Jellyfin URL and API key

Do not put `/config` on SMB, NFS, cloud sync, or another network filesystem.
SQLite uses journal files and filesystem locking beside the database. The
staging and music paths may be large; plan space for the upload, extracted copy,
tagged working copy, and prepared album at the same time.

For the current UGREEN NAS deployment, keep the host music binding set to:

```dotenv
MUSIC_PATH=/mnt/@usb/sdc2/Media/Music
```

Do not recursively change ownership of that existing collection without first
checking the Jellyfin and shared-group permission model.

## Mandatory Stage 1 to Stage 2 upgrade

Stage 2 automatically migrates the SQLite schema from v1 to v2 on the first
web/worker startup. The migration is additive: it creates acquisition and
inventory tables and does not rebuild or discard Stage 1 job/history tables.
It is serialized by the existing cross-process schema lock.

Before that startup, make one intact, adjacent copy of the **entire configured
`/config` directory**. This is mandatory. Do not copy only a live `.db` file.

1. In the NAS terminal, change to the Fox Den Music project directory and stop
   both processes:

   ```sh
   docker compose stop web worker
   ```

2. Read `CONFIG_PATH` from `.env`, resolve it to the explicit host directory,
   and copy that directory to a new sibling. Substitute the real absolute paths
   below; the destination must not already exist:

   ```sh
   cp -a "/absolute/path/to/config" \
     "/absolute/path/to/config.stage1-pre-stage2-20260816"
   test -f "/absolute/path/to/config.stage1-pre-stage2-20260816/foxden-music.db"
   ```

   Keep this backup intact until Stage 2 has passed the restart and workflow
   checks below. Do not put the backup inside the original `config` directory.

3. Preserve the previous Stage 1 source/image or Compose bundle as well, then
   build and start Stage 2:

   ```sh
   docker compose up -d --build
   docker compose ps
   docker compose logs --tail=200 web worker
   ```

4. Confirm both services are healthy and `GET /api/health` reports
   `"status": "ok"`. Existing Stage 1 history should still appear under
   **History**.

Do not edit `database_metadata` by hand. Stage 1 cannot open a schema-v2
database. To roll back, stop the stack, preserve the failed Stage 2 config under
a different sibling name, restore the schema-v1 directory copy at the exact
configured path with its original UID/GID, restore the Stage 1 code/image, and
start Stage 1:

```sh
docker compose down
mv "/absolute/path/to/config" "/absolute/path/to/config.stage2-failed-20260816"
cp -a "/absolute/path/to/config.stage1-pre-stage2-20260816" \
  "/absolute/path/to/config"
# Restore the saved Stage 1 project/image, then:
docker compose up -d
```

This rollback preserves both copies and deletes nothing.

## Fresh installation

1. Copy the example environment file:

   ```sh
   cp .env.example .env
   ```

2. Choose a non-root service identity and pre-create the host directories. This
   example uses UID/GID `10001`; use a shared music group if Jellyfin already
   owns the library:

   ```sh
   export FOX_UID=10001 FOX_GID=10001
   sudo install -d -o "$FOX_UID" -g "$FOX_GID" -m 0750 \
     ./data/config ./data/staging ./data/music
   ```

3. Generate a CSRF secret and place it in `.env`:

   ```sh
   python3 -c 'import secrets; print(secrets.token_urlsafe(48))'
   ```

4. Edit `.env`:

   - set `PUID`, `PGID`, and the three host paths;
   - set `MUSICBRAINZ_CONTACT` to a real contact email or HTTPS URL;
   - optionally set `JELLYFIN_URL` and `JELLYFIN_API_KEY`;
   - set `REMOTE_BROWSER_URL` to the externally reachable browser address;
     keep `REMOTE_BROWSER_KIOSK=0` so downloader popups remain reachable as tabs;
   - keep `WEB_BIND_IP=127.0.0.1` behind a local reverse proxy, or set one
     specific LAN address for direct LAN access;
   - leave `SPOTIDOWNLOADER_URL` as the administrator-controlled public HTTPS
     page unless the provider's legitimate public URL changes.

5. Build and start:

   ```sh
   docker compose up -d --build
   docker compose ps
   ```

6. Open `http://<WEB_BIND_IP>:<WEB_PORT>/` (port `8000` by default). The worker
   automatically queues the initial library scan.

Stop the stack with `docker compose down`. This does not delete bind-mounted
configuration, staging jobs, acquisitions, or music.

### Accounts, API keys, and values you must supply

Stage 2 does not need a Spotify developer account or SpotiDownloader API key.
Its provider step is deliberately manual. Put environment-specific values in
the private `.env` file—not in Compose, source code, or this README:

- `MUSICBRAINZ_CONTACT`: a real contact email or HTTPS URL. MusicBrainz needs
  no account or API key, but this value is required for automatic matching. If
  it is blank, imports pause safely so you can choose incoming tags.
- `JELLYFIN_URL` and `JELLYFIN_API_KEY`: optional. Create the key in your own
  Jellyfin administrator dashboard only when you want Fox Den Music to request
  a library refresh. Leave both blank to keep the integration disabled.
- `CSRF_SECRET`: optional. A blank value is generated once and persisted under
  `/config`; set it yourself only when you want to manage the secret externally.
- `SPOTIDOWNLOADER_URL`: the configured public HTTPS landing page. It is not a
  credential and Fox Den Music never sends a submitted Spotify URL to it.

After changing `.env`, recreate the two containers so Compose supplies the new
values. Never paste API keys into the web UI or commit the populated file.

## Environment variables

| Variable | Default | Purpose |
| --- | ---: | --- |
| `PUID` / `PGID` | `10001` | Non-root image and runtime numeric identity |
| `CONFIG_PATH` | `./data/config` | SQLite and generated state; local filesystem only |
| `STAGING_PATH` | `./data/staging` | Uploads, acquisition roots, extraction, and working copies |
| `MUSIC_PATH` | `./data/music` | Final Jellyfin music library; worker only |
| `DOWNLOAD_INBOX_PATH` / `DOWNLOAD_INBOX_DIR` | `./data/download-inbox` / `/downloads` | Shared host folder and container path for completed browser downloads |
| `REMOTE_BROWSER_URL` | blank | Externally reachable URL for the human-controlled server browser |
| `REMOTE_BROWSER_BIND_IP` / `REMOTE_BROWSER_PORT` | `127.0.0.1` / `5800` | Published server-browser address |
| `REMOTE_BROWSER_KIOSK` | `0` | Keep Firefox tabs visible for downloader popups; Compose forces popup windows into tabs, sets `SPOTIDOWNLOADER_URL` as the homepage, and disables session restoration |
| `WEB_BIND_IP` / `WEB_PORT` | `127.0.0.1` / `8000` | The only published service port |
| `WEB_MEMORY_LIMIT` / `WORKER_MEMORY_LIMIT` | `768m` / `1g` | Container memory backstops |
| `CSRF_SECRET` | blank | Optional form-signing secret; blank persists a generated value under `/config` |
| `MUSICBRAINZ_CONTACT` | required for auto matching | Real contact email/HTTPS URL; no MusicBrainz account or key |
| `JELLYFIN_URL` / `JELLYFIN_API_KEY` | blank | Optional local Jellyfin base URL and admin-created API key; worker only |
| `MAX_UPLOAD_BYTES` | `4294967296` | Total upload-file limit plus bounded multipart allowance |
| `ARCHIVE_MAX_FILES` | `2000` | ZIP entry/file ceiling |
| `ARCHIVE_MAX_TOTAL_BYTES` | `8589934592` | Expanded ZIP byte ceiling |
| `ARCHIVE_MAX_ENTRY_BYTES` | `2147483648` | One expanded ZIP member ceiling |
| `ARCHIVE_MAX_COMPRESSION_RATIO` | `250` | Bomb-defense ratio ceiling for large members |
| `AUTOMATIC_MATCH_THRESHOLD` | `94` | Minimum local confidence for auto-accept |
| `AUTOMATIC_MATCH_MARGIN` | `8` | Required lead over the runner-up |
| `LIBRARY_SCAN_INTERVAL_SECONDS` | `21600` | Periodic read-only rescan interval; `0` disables scheduled rescans after initial scan |
| `STORAGE_SNAPSHOT_INTERVAL_SECONDS` | `900` | Background cached storage refresh interval; `0` disables periodic refresh |
| `INVENTORY_COMMIT_BATCH_SIZE` | `50` | Inventory rows flushed per batch inside one atomic scan publication |
| `SPOTIDOWNLOADER_URL` | `https://spotidownloader.com/` | Static/configured public provider page opened by the client |
| `SPOTIFY_BATCH_MAX_URLS` | `100` | Maximum acquisition jobs per submission |
| `SPOTIFY_BATCH_MAX_CHARACTERS` | `20000` | Maximum pasted batch size |
| `FILE_UMASK` | `0027` | Conservative new-file permissions |
| `FFMPEG_TIMEOUT_SECONDS` | `180` | Limit for losslessly wrapping raw AAC as taggable M4A |

All supported settings are in `.env.example` and
`src/foxden_music/config.py`. Never commit the populated `.env` file.

## Using Stage 2

The navigation is:

```text
Dashboard | Add Music | Queue | Review | Library | History
```

The Library page links to the dedicated **Health** view.

### Dashboard

The dashboard reads persisted SQLite aggregates and never scans media during a
page request. It shows real artist, album, track, byte, codec/quality, activity,
storage, and health counts, plus active acquisition/import jobs and recent
successful imports. Before the initial scan completes, the UI says the library
has not been scanned and inventory counts are not yet authoritative. Use the
**Refresh library** button after a batch of downloads to update these counts.

Active jobs poll every 8 seconds; acquisition detail polls every 5 seconds;
dashboard metrics/recent imports poll every 30 seconds. No WebSocket or large
client framework is required.

### Queue Spotify acquisition requests

1. Open **Add Music** (`/add`).
2. Paste one Spotify track, album, or playlist URL per line.
3. Select FLAC (default) or MP3 320.
4. Select **Add all to queue** and open a request under **Queue**.

Only exact HTTPS `open.spotify.com` track/album/playlist URLs with a
22-character base62 ID are accepted. Credentials, explicit ports, arbitrary
schemes/hosts, and unexpected paths are rejected. Query parameters and
fragments are discarded when the URL is canonicalized. The server does not
fetch Spotify or forward the submitted URL to the provider. One bad batch line
creates no jobs; duplicates in the same submission coalesce.

Queueing needs no Spotify API credential. Stage 2 intentionally does not scrape
display metadata; before import, a request may be labeled by type and Spotify
identifier.

### Human-assisted SpotiDownloader handoff

1. Open the acquisition detail page.
2. Select **Copy Spotify URL**.
3. Select **Open SpotiDownloader**. Fox Den Music opens only the configured
   public HTTPS page in your browser and does not append the Spotify URL.
4. Paste the URL, complete any CAPTCHA yourself, select quality, and download
   through the site's normal public interface.
5. Return to Fox Den Music and select **Upload download**.
6. Choose one album ZIP or one FLAC, MP3, M4A/AAC, Opus, or OGG file. The page
   shows the selected filename, size, and live upload progress. A loose audio
   file is intentionally treated as a one-track release.

The application records an acquisition artifact, creates a linked Stage 1
import job, and then uses the same stream limits, archive checks, ffprobe,
metadata review, tagging, duplicate checks, atomic publish, and Jellyfin flow
as a direct import. The acquisition page shows the combined timeline.

If a request is stuck while it is still waiting for the human download, open
its detail page and select **Cancel request**. Cancellation is available only
before an import is linked (`QUEUED`, `WAITING_FOR_USER`, or
`WAITING_FOR_DOWNLOAD`). It preserves the request and cancellation event in
history and does not delete the reserved staging directory, browser downloads,
uploaded artifacts, or music. Once Stage 1 has the file, open the linked import.
A queued, review-paused, or failed import can be cancelled/dismissed there; its
history and staged evidence remain intact. An actively processing import cannot
be interrupted mid-write and must first reach a safe pause or terminal state.

Do not manually copy a file into
`/staging/acquisitions/<acquisition-id>/incoming` expecting it to run. Stage 2
reserves that directory for Stage 3 but has no watcher. The upload form is the
supported association path.

### Direct import

The Stage 1 direct path remains under **Add Music** and `/imports`:

1. Select one album ZIP or one supported audio file. The page identifies which
   kind was selected, checks the configured limit before sending, and displays
   live upload progress.
2. Submit it. The client filename is display-only; bytes stream to a
   server-generated path below `/staging/jobs/<job-id>/incoming`.
3. Follow the job page while the worker validates, inspects, matches, tags, and
   organizes.
4. If it becomes **Needs review**, compare release title, artist credit, date,
   country, media, track count, disambiguation, and confidence. Select the exact
   MusicBrainz release or explicitly use incoming tags.
5. A successful job records a library-relative output and Jellyfin refresh
   result in history.

Raw `.aac` is hash-verified and losslessly wrapped as M4A with ffmpeg before
tags are written; audio is not re-encoded.

When **Use incoming tags** is selected, a multi-track disc must still have
explicit, unique positions covering `1..N`. Fox Den Music may safely infer an
omitted track total from that verified sequence and writes the inferred total
to every output file. An explicit total that disagrees with the uploaded files
still pauses for review because it can indicate a partial album. Selected
MusicBrainz release dates take precedence; otherwise a supplied release date or
year is preserved and written using the standard date tag for the output
format. Fox Den Music does not invent a missing year.

### Library inventory and health

The worker automatically queues an initial read-only scan and a scheduled scan
every six hours by default. **Refresh library** is available on both the
dashboard and Library page; if a scan is already queued/running, the request
reuses it instead of creating a concurrent crawl.

The scan:

- skips `/music/.imports`, symlinks, reparse points, devices, and unsupported
  files;
- never changes media, tags, timestamps, or artwork;
- reads tags and ffprobe data and hashes new/changed supported audio;
- reuses an unchanged successful row when relative path, size, nanosecond mtime,
  and prior hash agree;
- flushes bounded database batches inside one atomic publication transaction,
  keeping the last completed inventory if a scan fails or the worker restarts;
- stores safe relative paths, not host absolute paths.

Browse `/library/artists` and `/library/albums`; their list pages are paginated
and searchable. Album detail includes ordered tracks, codec, bitrate, sample
rate, bit depth, duration, and metadata status.

The initial health view detects missing known/embedded artwork, missing
title/artist/album/track-number tags, inspection failures, duplicate SHA-256
groups, repeated MusicBrainz recording IDs, and MP3 files below 320 kbps. It
does not claim to detect fake FLAC or automatically replace lower-quality media.

Album identity is folder-oriented: supported audio files in one containing
directory form one inventory album. Tags determine the display artist/title;
folder names are a display fallback while missing tags remain flagged.

## Read-only integration APIs

These endpoints are intended for a future Fox Den Observatory integration:

| Endpoint | Contents | Cache behavior |
| --- | --- | --- |
| `GET /api/health` | App version and database check | no-store; 503 when degraded |
| `GET /api/status` | Latest scan state, activity counts, and up to 10 active items | no-store |
| `GET /api/stats` | Library, quality, activity, health, and cached storage aggregates | public, 15 seconds |

Each payload has an API response `schema_version` currently set to `1` and a
UTC `generated_at` where applicable. This is independent of SQLite schema v2.
The APIs do not return keys, secret values, provider tokens, absolute host
paths, Python tracebacks, or arbitrary URLs.

The legacy `GET /api/jobs/<job-id>` remains available for one import job's safe
status. The internal `/health` endpoint remains the Docker web health check.

## Generate a safe test album

The repository includes a three-track, two-second sine-tone album generator. It
uses Korean and accented Unicode metadata and contains no copyrighted music:

```sh
docker compose run --rm --entrypoint python worker \
  /app/scripts/generate_test_album.py /staging/Fox-Den-Test-Album.zip
```

The ZIP appears under the configured host `STAGING_PATH`. Copy it to the device
running your browser if that staging directory is not exposed through a safe
file share. The fictional album probably has no MusicBrainz match; choose **Use
incoming tags**. Its expected destination is
`Fox Den Test Artist/Signals from the Den (2026)` with three FLAC files and
`cover.jpg`.

## Exact Stage 2 manual acceptance checklist

Run this against the existing NAS only after the adjacent `/config` backup has
been made. Use media you own or are authorized to download.

1. Start/rebuild the stack and confirm both services are healthy:

   ```sh
   docker compose up -d --build
   docker compose ps
   docker compose logs --tail=200 web worker
   ```

2. Open the dashboard. Confirm it identifies the library as not yet scanned or
   shows a real scan state; it must not present invented nonzero metrics.

3. Open **Library**. If the initial scan is not active, select **Rescan
   library**. Watch the persisted status reach `COMPLETE` and note discovered,
   inspected, reused, and failed counts. A large first scan can take time
   because every supported file is inspected and hashed.

4. Confirm the resulting track/album/artist and codec totals correspond to the
   actual collection mounted from `/mnt/@usb/sdc2/Media/Music`. Open **Artists**,
   an artist, an album, and several tracks. Spot-check tags, ordering, codec,
   sample rate/bit depth, size, and artwork status against real files.

5. Open **Health**. Inspect missing metadata/artwork, low-bitrate MP3, inspection
   errors, and duplicate groups. Confirm each finding is plausible. Stage 2 must
   not change or delete any music while scanning.

6. Test batch validation without contacting Spotify or a provider. Paste these
   syntactically valid references into **Add Music**, choose FLAC, and add all:

   ```text
   https://open.spotify.com/album/AAAAAAAAAAAAAAAAAAAAAA?si=discarded
   https://open.spotify.com/track/BBBBBBBBBBBBBBBBBBBBBB
   https://open.spotify.com/playlist/CCCCCCCCCCCCCCCCCCCCCC
   ```

   Confirm three persistent requests appear. Confirm the first stored URL no
   longer contains the query string. Submit a batch containing
   `file:///etc/passwd` or `https://example.com/album/AAAAAAAAAAAAAAAAAAAAAA`;
   confirm it is rejected with no partial jobs created.

7. Open one queued acquisition. Verify **Copy Spotify URL** copies only its
   canonical Spotify reference and **Open SpotiDownloader** opens the static
   configured public page without putting that reference in the provider URL.
   Manually close the provider tab; no server-side request is required.

8. Generate the synthetic test ZIP above and upload it through that acquisition
   job's **Upload download** form. Confirm the acquisition immediately shows a
   linked import job and a file-received/import-started timeline. Do not use the
   generic direct upload for this check.

9. Follow the linked import. If prompted, choose **Use incoming tags**. Confirm
   the import reaches `COMPLETE`, the acquisition also becomes complete, and
   the final synthetic album contains exactly three FLAC files plus `cover.jpg`.
   Confirm the output was published once and no existing path was overwritten.

10. Open **History** and confirm you can trace Spotify reference → acquisition
    → received artifact → import events → final relative destination → Jellyfin
    result. Open the dashboard and confirm the completed job appears under
    recent imports.

11. Select **Refresh library** once after the batch. When it completes, confirm
    the synthetic album appears in the artist/album pages and changes the real
    dashboard counts. Dashboard requests themselves never crawl the filesystem.

12. If Jellyfin is configured, confirm the import records a successful refresh
    request and then verify the album in Jellyfin after its asynchronous scan.
    If Jellyfin is stopped, confirm the media import remains complete and only
    the refresh is retryable.

13. Restart both containers:

    ```sh
    docker compose restart web worker
    docker compose ps
    ```

    Confirm the same acquisition queue, linked history, completed import, and
    library inventory remain. Confirm no duplicate scan/job or second album was
    created. A scan interrupted by restart may be marked failed, but the last
    completed inventory must remain and a later scan must recover normally.

14. Fetch the integration endpoints:

    ```sh
    curl -fsS http://127.0.0.1:8000/api/health
    curl -fsS http://127.0.0.1:8000/api/status
    curl -fsS http://127.0.0.1:8000/api/stats
    ```

    Replace the address when running from another LAN device. Confirm valid
    JSON, correct counts/states, relative detail links, and no API key, CSRF
    value, absolute NAS path, traceback, or provider token.

15. Re-run one Stage 1 direct import from **Add Music**. Then upload the same
    synthetic album again and confirm exact duplicate/conflict handling creates
    no second published copy. On disposable staging, also confirm a ZIP with
    traversal, symlink, nested archive, or renamed archive magic is rejected
    without changing `/music` or paths outside that job.

16. Review `docker compose logs --tail=300 web worker`. There should be no secret
    value, unhandled traceback, browser container, CAPTCHA automation, or extra
    published service. Keep the adjacent schema-v1 `/config` backup until all
    checks pass and you have a normal post-upgrade backup.

To exercise the actual external-provider handoff after the synthetic test,
create an acquisition with one real Spotify URL, use the buttons, manually
complete the provider's normal CAPTCHA/download flow, and upload the authorized
result. The application side should behave exactly like steps 8–12.

## Jellyfin setup

1. In Jellyfin, create/select a dedicated **Music** library whose folder uses
   the same host collection as `MUSIC_PATH`.
2. Create an API key in Jellyfin administration.
3. Put the base URL and key in `.env`, then recreate the worker:

   ```sh
   docker compose up -d --force-recreate worker
   ```

After atomic rename, Fox Den Music sends `POST
<JELLYFIN_URL>/Library/Refresh` with Jellyfin's `MediaBrowser Token` scheme. A
success means an asynchronous scan was requested, not completed. If Jellyfin is
offline or rejects it, the music remains complete and the job offers a separate
refresh retry.

## Backups and restore

The safest regular backup is an offline copy of the entire configured config
directory:

```sh
docker compose stop web worker
tar -C ./data -czf "foxden-music-config-$(date +%Y%m%d-%H%M%S).tar.gz" config
docker compose start web worker
```

This example assumes `CONFIG_PATH=./data/config`; substitute the configured
parent and directory when different. Back up the music library through the
normal storage-level process. `/staging` is useful for retry/investigation but
is not the system of record after import.

Do not copy only a live SQLite `.db`. For online backup, use SQLite's backup API
or `VACUUM INTO` from a compatible maintenance process. Before every schema
upgrade, stop both services and keep one clearly named adjacent copy of the
whole config directory plus the matching application release.

To restore, stop both services, preserve the current config separately, restore
the complete backup and any required consistent music snapshot with the same
UID/GID, then start the matching application version.

## Security model

- Both containers use a configurable nonzero UID/GID, read-only root
  filesystem, all capabilities dropped, `no-new-privileges`, a private tmpfs,
  and no Docker socket, privileged mode, devices, or host network.
- Only the web port is published. The worker has no listener. Stage 2 adds no
  browser container or port.
- Web never receives the real music mount. A future acquisition browser must
  follow the same rule.
- Uploads have an ASGI request-stream limit and a second copy-time byte counter.
  Multipart spooling uses private staging storage.
- ZIP extraction preflights and streams members without `extract()` or
  `extractall()`, rejecting traversal, absolute/drive paths, links/devices,
  encryption, unsupported content/compression, nested archive magic, Unicode
  and case collisions, bombs, and limits.
- Audio is accepted only after ffprobe finds an audio stream; extension and
  client content type are not trusted.
- Source files stay immutable. Tags are written only to working copies, and
  lossy media is never described as proven lossless.
- Final albums require a complete manifest, hashes, same-filesystem validation,
  import lock, and no-replace atomic rename. There is no copy or overwrite
  fallback.
- Spotify queue input is never fetched server-side. Only exact supported HTTPS
  host/path forms are persisted. The provider link comes from validated admin
  configuration rather than an arbitrary submitted host.
- Inventory scans never write media, skip links/reparse points and `.imports`,
  and persist relative paths and concise errors.
- MusicBrainz calls share a one-request-per-second worker limiter and a
  contactable User-Agent.
- Secrets are mounted only to the process that needs them. Forms use CSRF,
  templates autoescape, and security headers block framing and active
  third-party content.

The app still has no built-in user authentication. Do not expose it directly to
the public Internet. Put it behind TLS and an authenticated reverse proxy such
as the existing Authentik-protected gateway.

## Troubleshooting

### A container is unhealthy

```sh
docker compose ps
docker compose logs --tail=200 web worker
```

Web health checks the app and SQLite. Worker health checks a current heartbeat,
not MusicBrainz, Jellyfin, Spotify, or SpotiDownloader. Long ffprobe, scan, and
network tasks retain an independent heartbeat.

### A library scan is slow or failed

The first scan must inspect and hash every supported audio file. Later scans
reuse successful files only when path, size, mtime, and stored hash are stable.
Check worker logs, ffprobe availability, music permissions, unreadable
directories, and whether the real root is a symlink/reparse point. A failed scan
keeps the last completed inventory. Fix the problem and request a rescan.

If filesystem traversal is noticeable on a very large library, increase
`LIBRARY_SCAN_INTERVAL_SECONDS` and/or `STORAGE_SNAPSHOT_INTERVAL_SECONDS`.
Dashboard requests themselves never crawl the filesystem.

### Permission denied under `/config`, `/staging`, or `/music`

```sh
docker compose run --rm worker id
stat -c '%u:%g %a %n' ./data/config ./data/staging /actual/music/path
```

Substitute configured paths. `/music/.imports` and final albums must share one
filesystem. `EXDEV` is intentionally fatal; the importer will not copy a
partial album into the live collection.

### An acquisition upload is not starting

Use the upload on the acquisition detail page. Copying a file into its reserved
`/staging/acquisitions/<id>/incoming` directory does nothing in Stage 2. An
acquisition accepts one linked import; open that import to retry/review it
instead of attaching another artifact.

If the request is still waiting for the manual download and should no longer be
active, open it and select **Cancel request**. The button intentionally
disappears after handoff. Cancellation does not remove a file already downloaded
to another computer. After handoff, open the linked import to cancel it while
queued/review-paused or dismiss it after failure; active processing is not
interrupted mid-write.

### A job waits for metadata review

- Verify `MUSICBRAINZ_CONTACT` is a real email or HTTPS URL.
- Confirm the worker reaches MusicBrainz and Cover Art Archive.
- Select the exact edition; search relevance is not proof.
- For untagged input, use a descriptive ZIP/folder name and track-numbered
  filenames. Incoming tags still require title, artist, album artist, and album.

### Jellyfin refresh failed

Confirm the base URL/path, key, TLS trust, permissions, and container
reachability. Use **Retry refresh** on the completed import. Do not re-import;
refresh status is intentionally separate.

### An archive was rejected

The importer is intentionally strict. Remove nested archives, executables,
active documents, symlinks, and unsupported extras. Supported inert sidecars
are CUE, LOG, TXT, M3U/M3U8, and NFO; artwork is JPEG, PNG, or WebP.

## Development and tests

With Python 3.12:

```sh
python -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
pytest -q
```

The recorded pre-Stage-2 Stage 1 baseline was:

```text
124 passed, 2 skipped in 3.57s
```

Both skips were Windows tests that require the local privilege to create
symlinks. This baseline was captured before Stage 2 code changes and is the
regression floor.

The current Stage 2 regression run was:

```text
200 passed, 2 skipped in 7.50s
```

The same two Windows symlink-privilege tests were skipped; there were no test
failures. Docker/NAS verification is recorded separately during deployment so
this local result is not presented as a container-runtime test.

The suite uses generated/synthetic bytes and mocked external HTTP. Stage 2 adds
coverage for strict Spotify URL parsing and batch atomicity, acquisition
persistence/transitions/uploads, schema-v1 migration, inventory aggregates and
Unicode, health/duplicate/quality detection, scan reuse/recovery, safe APIs,
and the combined web flow. Stage 1 archive, metadata, duplicate, no-overwrite,
Jellyfin, restart, CSRF, and synthetic end-to-end tests remain in the full run.

## Known limitations and performance notes

- One web process and one worker are supported with SQLite.
- Stage 2 provides manual upload association only. It has no watched acquisition
  folder, browser container, provider automation, CAPTCHA automation, or
  download observation.
- Spotify credentials are not required, but that also means queue items do not
  resolve artist/album names before import; the Spotify type/identifier is the
  initial label.
- One acquisition links to one import job. Playlist fan-out into independently
  named album jobs is not implemented.
- Cancellation stops an unlinked request waiting for its human-assisted
  download. A linked import can be cancelled only while queued, review-paused,
  or failed; active writes are never interrupted. Neither action deletes
  provider/browser downloads, staged evidence, or library media.
- Album inventory grouping follows containing directories and folds conventional
  `CD1`, `Disc 2`, or `Disk 03` subdirectories into their parent album. Strong
  tagging and the normal artist/album folder layout produce the best browse
  result.
- A completed import updates library inventory when you select **Refresh
  library** or the periodic scan becomes due. Dashboard rendering never
  triggers a filesystem scan.
- The first scan hashes every supported file. Later scans avoid repeated hashes
  for unchanged successful files, but still traverse directories. Storage
  snapshots also traverse music/staging (without ffprobe or hashing) on their
  configured background interval; raise the interval for extremely large or
  slow filesystems.
- A scan completes all filesystem inspection before atomically publishing the
  new inventory snapshot. This protects the last completed view after a failed
  scan, but a very large first scan can approach the worker memory limit and
  delays imports because Stage 2 intentionally uses one background worker.
- Health signals are factual checks, not replacement decisions. MP3 below 320
  kbps is labeled low bitrate; no fake-lossless heuristic or automatic media
  replacement exists.
- Duplicate/destination conflicts are preserved for review; there is no
  in-app replace/upgrade/keep-both action yet.
- Artwork uses known external cover filenames or embedded art. There is no
  manual artwork editor.
- Exact duplicate suppression remains album/track-slot scoped so legitimate
  compilation/reissue appearances are not globally suppressed.
- Jellyfin refresh is a full asynchronous request; scan completion is not
  tracked.
- There is no application login, configurable naming-template UI, or global
  orphan-manifest sweep after unrelated manual database deletion. Restore
  `/config` and music from a consistent set.

## Stage 3

The contained human-assisted browser design is in
[docs/stage-3-contained-browser.md](docs/stage-3-contained-browser.md). It
proposes a separate non-root browser sandbox with no `/music`, `/config`,
secrets, or host socket; per-job write-only staging; an authenticated short-lived
interactive view for manual CAPTCHA completion; a hashed handoff manifest;
strict lifecycle/network/resource controls; and an unchanged Stage 1 validation
boundary.
