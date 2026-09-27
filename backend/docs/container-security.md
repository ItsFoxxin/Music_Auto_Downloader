# Container security and storage

Fox Den Music uses one image with two commands. The web and worker containers
share only the paths required for the persisted job workflow.

| Service | Published ports | `/config` | `/staging` | Host music library |
| --- | --- | --- | --- | --- |
| `web` | TCP 8000 only | read/write | read/write | not mounted |
| `worker` | none | read/write | read/write | mounted at `/music`, read/write |

The web service's internal `MUSIC_DIR` points into its private `/tmp` tmpfs only
to satisfy application initialization. It has no path to the real host library.
The dashboard and library pages read the worker-built SQLite inventory; they do
not require a web-side music mount.

Stage 2 acquisition jobs reserve directories beneath `/staging/acquisitions`,
but no browser container or watched-folder service is present. Acquisition
uploads are streamed into ordinary Stage 1 job staging and do not bypass any
archive or audio validation.

## Runtime controls

Both containers:

- run as the numeric `PUID:PGID`, with a non-root image user as the fallback;
- reject UID or GID 0 during the image build;
- set `no-new-privileges` and drop every Linux capability;
- use a read-only image filesystem and a small, `noexec`, `nosuid`, `nodev`
  tmpfs at `/tmp`;
- use Docker's small init process for signal forwarding and child reaping;
- use the default isolated Compose network, not host networking;
- have no privileged mode, device mounts, or Docker socket mount.

The image build rejects UID/GID 0, and the runtime wrapper independently
refuses effective UID or GID 0. This keeps an old image fail-closed if someone
later changes only the Compose `user` override.

Compose also applies `WEB_MEMORY_LIMIT` (768 MiB by default) and
`WORKER_MEMORY_LIMIT` (1 GiB by default). The worker preflights bounded ID3,
FLAC, MP4, and Ogg metadata regions before Mutagen parses them and rejects
oversized text/artwork. The memory limit remains a final host-protection
backstop if a third-party parser regresses; an OOM-restarted worker leaves its
persisted active job recoverable rather than publishing a partial album.

FFmpeg and ffprobe run under the same unprivileged worker identity. Untrusted
archives and audio remain under `/staging` until the worker has validated and
assembled a complete album.

The web service sets `TMPDIR=/staging/.upload-tmp`. Starlette can spool a large
multipart body before the intake handler streams it, so upload spooling must be
disk-backed by the capacity-limited staging filesystem rather than charged to
the 256 MiB `/tmp` memory filesystem. The non-root runtime wrapper creates this
private directory with mode `0700`; include its contents in normal stale-job
cleanup and capacity monitoring.

## Create and own the host directories

Compose uses `create_host_path: false`. This is deliberate: a typo must not make
Docker silently create a root-owned directory. On a Linux host, from the project
directory:

```sh
cp .env.example .env

FOX_UID="$(id -u)"
FOX_GID="$(id -g)"
mkdir -p data/config data/staging data/music
sudo chown "$FOX_UID:$FOX_GID" data/config data/staging data/music
chmod 0750 data/config data/staging data/music
```

Put the same numeric values in `.env` as `PUID` and `PGID`, then rebuild. Do not
use 0. Changing either ID requires `docker compose up -d --build` so the image
user and runtime override stay aligned.

For an existing Jellyfin library, do not recursively change ownership without
first confirming the Jellyfin service account and current permissions. A common
safe model is a shared media group: Fox Den Music owns new content and Jellyfin
is a member of the configured `PGID`, with directories group-traversable and
files group-readable. The default `FILE_UMASK=0027` produces owner-writeable,
group-readable files without world access.

Docker Desktop translates Windows bind-mount permissions through its Linux VM;
production ownership checks should be performed on the Linux Docker host.

## Atomic import filesystem requirement

`/music/.imports/<job-id>` must be a directory inside the same `/music` mount
and underlying filesystem as `/music/<Album Artist>/<Album> (<Year>)`. Do not
make `.imports` a separate volume or a symlink to another filesystem. The final
rename is atomic only when source and destination are on the same filesystem.

`/staging` may be a different filesystem because it is not the source of the
final rename; the complete album is first assembled and verified under
`/music/.imports`.

## SQLite storage

Both processes open `/config/foxden-music.db` directly. Mount the `/config`
directory, not only the database file, because SQLite must create journal files
beside it. Keep `/config` on a local filesystem. NFS, SMB/CIFS, cloud-sync
folders, and distributed filesystems do not provide the locking guarantees this
two-process SQLite design requires.

Fox Den Music intentionally uses SQLite rollback-journal mode, a 30-second busy
timeout, one web process, and one worker. Transactions must remain short and
must not span ffprobe, network requests, tagging, or filesystem operations.

For a consistent backup, stop both services before copying `/config`, or use
SQLite's online backup API. Never copy only the `.db` file while a write may be
active.

The Stage 1-to-Stage 2 startup migration changes the database schema from v1 to
v2 by adding acquisition, inventory, scan-run, and storage-snapshot tables. It
does not rebuild the Stage 1 tables, but an intact adjacent backup of the entire
configured `/config` directory is mandatory before upgrade. Stage 1 cannot use
the migrated database. Rollback means stopping both services and restoring the
schema-v1 directory backup, not editing the schema version by hand.

## Secrets

The populated `.env` is ignored by Git. Restrict it on Linux:

```sh
chmod 0600 .env
```

Compose turns `CSRF_SECRET` and `JELLYFIN_API_KEY` into per-service secret files.
Only the web container receives the CSRF secret; only the worker receives the
Jellyfin key. The non-root entrypoint reads supported `*_FILE` values, places
them in the just-starting process environment for the existing settings model,
and immediately replaces itself with the application process. It never prints
secret paths or values.

Generate a CSRF value with:

```sh
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Leave both `JELLYFIN_URL` and `JELLYFIN_API_KEY` empty to disable Jellyfin. Never
put the API key in a URL query string, command line, image layer, or log message.

## Health checks

The web health check calls `http://127.0.0.1:8000/health` inside the container.
It does not make the web port more broadly accessible.

The worker writes `/tmp/foxden-music-worker-heartbeat.json` atomically and the
health check rejects a missing, future-dated, or stale heartbeat. The heartbeat
must be updated during long-running processing as well as idle polling. Because
it lives in the worker's private tmpfs, a restarted container cannot inherit a
healthy marker from an old process.

Neither health check calls Jellyfin, MusicBrainz, or Cover Art Archive. An
optional external outage must not cause Docker to restart an otherwise healthy
import service.

## Exposure and validation

The default bind address is `127.0.0.1`. Keep it for a same-host reverse proxy,
or set `WEB_BIND_IP` to a specific LAN address (or `0.0.0.0`) for direct LAN
access. The worker has no `ports` or `expose` entry.

Before starting:

```sh
docker compose config --quiet
docker compose up -d --build --wait
docker compose ps
```

Stage 2 adds no published port. The **Open SpotiDownloader** control opens the
administrator-configured public HTTPS URL in the user's own browser; the server
does not fetch the submitted Spotify URL or proxy provider content. Only exact
`https://open.spotify.com/{track,album,playlist}/<id>` references can enter the
queue. Query strings and fragments are discarded during canonicalization;
arbitrary schemes, hosts, credentials, ports, and path forms are rejected.

Inspect the effective mounts and security settings when troubleshooting:

```sh
docker compose config
docker inspect fox-den-music-web-1
docker inspect fox-den-music-worker-1
```
