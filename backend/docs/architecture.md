# Stage 2 architecture

Stage 2 keeps the Stage 1 importer intact and adds two deliberately separate
domains: a provider-neutral acquisition queue and a read-only index of the real
music library. Losing an acquisition provider must never make direct upload,
metadata review, atomic import, history, or Jellyfin refresh unavailable.

## Processes and trust boundaries

`web` accepts untrusted multipart bodies and Spotify references. It can write
`/config` and `/staging`, but its `/music` is an empty private tmpfs path. It
renders the dashboard, queue, review, history, library, artist, album, and
health pages and serves the read-only status APIs.

`worker` is the only process with the real `/music` bind mount. It runs the
unchanged Stage 1 importer, synchronizes linked acquisition states, scans the
library without modifying media, and refreshes cached storage snapshots.

Neither service receives the Docker socket, privileged mode, host networking,
devices, or Linux capabilities. Stage 2 adds no browser container and exposes
no additional port.

## Acquisition is not import

An `AcquisitionJob` records intent to obtain one Spotify track, album, or
playlist. It stores a canonical Spotify reference, requested quality, provider,
constrained state, events, artifacts, and at most one linked Stage 1 import job.
It does not process media.

The Stage 2 manual path is:

```text
Spotify URL
  -> AcquisitionJob (SPOTIDOWNLOADER_MANUAL)
  -> user opens the configured public provider page
  -> user completes any CAPTCHA and downloads normally
  -> user uploads the resulting ZIP/audio to that AcquisitionJob
  -> AcquisitionArtifact + linked Stage 1 Job
  -> existing validation, metadata, tagging, atomic import, and Jellyfin flow
```

The provider adapter supplies instructions and a configured public HTTPS URL;
it is not permitted to fetch the submitted Spotify URL, call a private provider
endpoint, or place arbitrary user input into an outbound request. The external
provider URL never includes the submitted Spotify reference.

Each request reserves
`/staging/acquisitions/<acquisition-id>/incoming` for a future contained browser
handoff. Stage 2 does **not** watch or ingest manually copied files from that
directory. The supported handoff is the acquisition detail page's upload form,
which streams into a normal `/staging/jobs/<import-job-id>/incoming` source and
therefore crosses every existing Stage 1 validation boundary.

Acquisition states are `QUEUED`, `WAITING_FOR_USER`,
`WAITING_FOR_DOWNLOAD`, `FILE_RECEIVED`, `IMPORT_STARTED`, `NEEDS_REVIEW`,
`COMPLETE`, `FAILED`, and `CANCELLED`. Import states remain the Stage 1 state
machine. The effective acquisition state mirrors its linked import when that
import needs review, fails, resumes, or completes.

## Stage 1 durable import flow

```text
QUEUED -> STAGING -> EXTRACTING -> INSPECTING -> MATCHING_METADATA
                                           |             |
                                           |             +-> NEEDS_REVIEW -> QUEUED
                                           v
TAGGING -> ORGANIZING -> VALIDATING -> IMPORTING -> JELLYFIN_SCAN -> COMPLETE
   |            |             |            |
   +------------+-------------+------------+-> NEEDS_REVIEW

Any active stage -> FAILED -> QUEUED (explicit retry)
```

An individual audio file moves from `STAGING` directly to `INSPECTING`. Every
transition and significant result adds an immutable job-event row. The worker
makes short SQLite transactions around claims, transitions, and saved results;
ffprobe, filesystem work, hashing, and HTTP calls happen outside transactions.

On worker startup, jobs left in active states become `FAILED` with
`WORKER_INTERRUPTED` and an explicit retry action. `NEEDS_REVIEW` and
`COMPLETE` jobs are never reclaimed. Linked acquisition state is synchronized
without changing those recovery guarantees.

## Identification and confidence

Incoming tags provide hints, not truth. Fox Den Music searches MusicBrainz
releases, then looks up candidate media, release tracks, recordings, artist
credits, durations, and ISRCs. Local confidence uses album title, album artist,
track count, ordered titles, durations, ISRC overlap, and date. Auto-accept also
requires an exact track count and a configured lead over the runner-up.

The selected release controls release and recording identifiers and track
positions. Track IDs and Recording IDs are not conflated. Mismatched counts or
large duration disagreements return to review.

## Working and final hashes

The import database stores the SHA-256 of immutable extracted audio and another
SHA-256 after tags/artwork. Tagging changes bytes, so both are required for
duplicates and audit. Duplicate signals remain scoped to a normalized album
artist, album, disc, track, and title identity.

The scanned inventory has its own SHA-256 field. An unchanged file is reused
only when relative path, size, nanosecond modification time, prior hash, and
successful prior inspection all agree. This avoids re-reading and re-hashing
the whole collection on every scan while still re-inspecting changed files.

## Atomic publish

The worker builds and verifies:

```text
/music/.imports/<job-id>/album/
  .foxden-import.json
  cover.jpg
  01 - Track.flac
  ...
```

It checks every final hash, verifies source and destination share `st_dev`,
rejects symlinked ancestors, takes a library import lock, and invokes Linux
`renameat2` with `RENAME_NOREPLACE`. Cross-device and overwrite fallbacks are
forbidden. The complete album directory becomes visible at the final name at
once.

If a crash occurs after rename but before SQLite registration, retry reconciles
only a destination whose job manifest and digest match. A different destination
is preserved and reported as a conflict. Jellyfin refresh happens afterward;
its failure is separately retryable and never removes imported music.

## Read-only library inventory

The worker scans supported audio beneath `/music`, excluding `.imports`. The
root and each traversed entry are checked without following symlinks or reparse
points. A scan reads tags, invokes ffprobe, hashes changed/new files, detects
artwork, and persists relative paths only. It never writes tags, artwork, or
media.

`library_inventory_tracks` represents every observed supported audio file.
`library_albums` and `library_artists` hold indexed aggregates for fast browse
and dashboard queries. `library_scan_runs` records durable progress and
outcome. A completed generation removes stale database rows for files no longer
present; a failed/interrupted scan keeps the previously completed inventory.
Upserts are flushed in bounded batches after filesystem inspection, then the
entire generation is committed as one atomic publication transaction.

The worker queues an initial scan automatically. It then queues at most one
scheduled scan after `LIBRARY_SCAN_INTERVAL_SECONDS` (six hours by default).
Setting the interval to zero disables scheduled scans after the initial scan.
Manual **Rescan library** requests are coalesced when a scan is already queued
or running.

Dashboard and API requests query the persisted index; they do not traverse
`/music`, invoke ffprobe, hash media, or contact MusicBrainz. Before the initial
scan and storage snapshot complete, unavailable values are represented as such
rather than fabricated.

## Dashboard and status APIs

Dashboard metric, active-job, and recent-import fragments poll at a modest
interval with HTMX. All values come from indexed database aggregates. Storage
figures come from `storage_snapshots`, refreshed in the background rather than
being recursively calculated for each request.

`/api/health`, `/api/status`, and `/api/stats` use an independent response
`schema_version` currently set to `1`; it is not the SQLite schema version.
Responses contain relative application links and aggregate data only. They do
not contain secrets, host filesystem paths, provider tokens, or stack traces.

## SQLite schema and migration

The SQLite database schema is version 2. Migration from schema v1 is automatic,
additive, and serialized between web and worker with the existing schema lock.
It creates:

- `acquisition_jobs`, `acquisition_events`, and `acquisition_artifacts`;
- `library_artists`, `library_albums`, and `library_inventory_tracks`;
- `library_scan_runs` and `storage_snapshots`.

Stage 1 job, track, release-candidate, duplicate-ledger (`library_tracks`),
event, and metadata-cache tables are not rebuilt. Existing jobs and history
survive the upgrade.

The system retains rollback-journal mode, a 30-second busy timeout, foreign keys
on every connection, full synchronous durability, one web process, one worker,
and short transactions. `/config` must remain on a local filesystem.

The migration is not a substitute for a backup. Stop both services and create
an intact, adjacent copy of the entire configured `/config` directory before
starting Stage 2. Stage 1 cannot open a schema-v2 database; rollback requires
restoring that schema-v1 directory copy.
