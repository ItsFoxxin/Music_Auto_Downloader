# Stage 2 manual acquisition workflow

Stage 2 implements a persistent provider-neutral acquisition queue while
keeping every media-processing decision in the existing Stage 1 importer. It
does not include a browser container or browser automation.

## What Stage 2 implements

- Strict queueing of canonical Spotify track, album, and playlist URLs.
- Batch creation of as many requests as allowed by
  `SPOTIFY_BATCH_MAX_URLS` (100 by default), with FLAC as the default requested
  format and MP3 320 as an alternative.
- A separate `AcquisitionJob` state machine, event stream, and artifact record.
- A manual `SPOTIDOWNLOADER_MANUAL` provider adapter whose public page comes
  from the validated, administrator-controlled `SPOTIDOWNLOADER_URL` setting.
- One reserved `/staging/acquisitions/<id>/incoming` directory per acquisition
  for future contained-browser handoff.
- An acquisition-scoped upload that creates and links an ordinary Stage 1
  import job. The file receives the same streaming limit, archive validation,
  audio inspection, duplicate checks, and atomic publishing as a direct upload.
- A unified acquisition/import timeline and history.
- A CSRF-protected **Cancel request** action for unlinked requests still in
  `QUEUED`, `WAITING_FOR_USER`, or `WAITING_FOR_DOWNLOAD`.

## Safe URL handling

The parser accepts only HTTPS URLs with the exact host `open.spotify.com`, no
credentials or explicit port, and exactly one of these path forms:

```text
/track/<22-character base62 identifier>
/album/<22-character base62 identifier>
/playlist/<22-character base62 identifier>
```

Query parameters and fragments are discarded during canonicalization;
unexpected path components are rejected. The application does not fetch the
submitted URL, resolve its host, run a shell command, or forward its query
parameters. This avoids creating an SSRF or command-injection primitive.

Batch validation is atomic: a malformed line returns a line-numbered error and
creates no acquisition jobs. Duplicate canonical URLs in one submission are
coalesced. Queueing a URL does not require Spotify credentials and does not
scrape Spotify metadata; until import metadata is available, its display title
uses the safe entity type and identifier.

## Human-assisted workflow

1. Open **Add music**.
2. Paste one supported Spotify URL per line, choose FLAC or MP3 320, and select
   **Add all to queue**.
3. Open a request from **Queue**.
4. Use **Copy Spotify URL**.
5. Use **Open SpotiDownloader**. This opens only the configured public HTTPS
   provider page; it does not append the Spotify URL.
6. Paste the URL into the provider page, complete any CAPTCHA yourself, choose
   the desired format, and download normally.
7. Return to the acquisition detail page and use **Upload download** with the
   ZIP or supported audio file.
8. Fox Den Music records the artifact, creates a linked import job, and hands
   the untrusted bytes to the unchanged Stage 1 importer.
9. Follow the combined acquisition/import timeline. If metadata needs review,
   use the linked import review; completion propagates back to the acquisition.

Before step 7, a request that is no longer wanted can be stopped with **Cancel
request** on its detail page. The action is idempotent, records a durable
`CANCELLED` event, and removes the request from active queue views. It leaves the
reserved acquisition directory and all files untouched. After handoff, open the
linked Stage 1 import: queued or review-paused work can be cancelled, and a
failed import can be dismissed. Active processing is never interrupted
mid-write. Neither action removes staged evidence, library media, or a file
already downloaded by the user's browser or provider.

Fox Den Music does not solve or bypass CAPTCHAs, circumvent DRM or access
controls, call undocumented private provider APIs, or automate downloads in
Stage 2.

## Acquisition and import data model

`AcquisitionJob` stores provider, canonical source URL/type/identifier, display
metadata, preferred format, state, safe error information, timestamps, a
relative acquisition directory, and a unique optional import-job relationship.
`AcquisitionEvent` is its append-only audit trail. `AcquisitionArtifact` records
the display filename, size, hash when available, safe relative stored path,
receipt method, and import handoff without treating the provider output as
trusted.

The provider interface returns human instructions independently of import.
Replacing the provider therefore does not require changes to archive
validation, metadata matching, tagging, duplicate detection, atomic publish,
or Jellyfin refresh.

## The reserved acquisition directory is not watched

Stage 2 creates this future handoff location:

```text
/staging/acquisitions/<acquisition-job-id>/incoming
```

There is intentionally no watched-folder auto-ingest in Stage 2. Copying a file
there manually does not start an import. This avoids guessing whether an
external copy is complete and keeps the first release's association behavior
explicit. Use the acquisition page's upload form.

The reserved layout is the stable boundary that a future browser container can
target using a completed, hashed, no-replace handoff manifest. That work is a
Stage 3 proposal, not a Stage 2 component; see
[stage-3-contained-browser.md](stage-3-contained-browser.md).

## Provider failure isolation

If the configured provider page changes, becomes unavailable, or is disabled,
existing acquisitions and imports remain persisted. Direct ZIP/audio upload,
manual acquisition upload, metadata review, library browsing, scanning, atomic
import, and Jellyfin refresh do not depend on the provider site.
