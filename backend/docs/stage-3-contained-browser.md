# Stage 3 proposal: contained human-assisted acquisition browser

Stage 3 can make the manual provider workflow smoother without giving a browser
access to the music library or automating a CAPTCHA. This is a concrete design
proposal only; Stage 2 does not deploy a browser service.

## Security objective

Treat the browser and every downloaded byte as hostile. A browser compromise
must not expose `/music`, `/config`, CSRF or Jellyfin secrets, host sockets,
other acquisition jobs, or the Docker control plane. Completion of a browser
download must still enter the unchanged Stage 1 archive/audio validation path.

## Component boundary

Add a separately built `foxden-music-browser` image with pinned Chromium and
Playwright plus a contained interactive-view server (for example, Xvfb and
noVNC/WebSocket proxy). Run it as a non-root UID/GID with:

- a read-only root filesystem and a private, size-limited `/tmp` tmpfs;
- all capabilities dropped and `no-new-privileges`;
- no privileged mode, device mounts, host networking, host PID namespace,
  Docker socket, Podman socket, containerd socket, or SSH agent socket;
- no `/music` mount and no `/config` mount;
- no CSRF secret, Jellyfin key, Spotify credential, or unrelated provider
  credential;
- filesystem access limited to one newly created, per-job write-only staging
  target, never the acquisitions parent;
- memory, CPU, PID, file-size, and wall-clock limits;
- one fresh browser profile per acquisition, destroyed at session end.

The preferred deployment is one short-lived container per active acquisition.
If the NAS cannot safely create containers on demand without exposing a socket
to the web/worker, use a separately administered fixed browser service that
enforces the same one-job sandbox internally. Do not give the application a
Docker socket merely to gain per-job lifecycle control.

## Filesystem access

The browser session receives at most one job-scoped target:

```text
/staging/acquisitions/<job-id>/browser-drop
```

It must not mount the parent `acquisitions` directory. The preferred design is
a handoff broker that exposes a narrow create/append/finalize stream and no
directory listing or read operation, so the browser has true write-only access
to an empty per-job target. If the NAS runtime cannot enforce a write-only bind
mount, do not broaden permissions: use the broker or an intermediate one-job
volume, grant no pre-existing data, and revoke the browser's access before the
worker can read the completed artifact.

Downloads land under broker-generated names ending in `.part`. After Chromium
reports completion and the size remains stable, the receiving handoff broker
fsyncs the file and directory, calculates SHA-256, and performs a no-replace
rename. It then emits a small manifest containing:

- acquisition job ID and an unguessable handoff nonce;
- canonical source type and identifier (not a credential-bearing URL);
- browser display filename and generated stored filename;
- byte size, SHA-256, media timestamp, and provider adapter version;
- completion time and manifest format version.

The helper signs or authenticates the manifest to a broker-held per-session
secret. It never chooses a destination path from page content. The worker
validates the job ID, nonce, generated basename, regular-file type, containment,
size, hash, and stable completion before moving the artifact into a newly
created Stage 1 import job. It then independently repeats all archive and audio
validation. A failed validation never retries through a weaker path.

## Interactive CAPTCHA handling

The adapter may navigate visibly to the administrator-configured provider page
and enter the already validated canonical Spotify URL through normal browser
controls. It must not call undocumented endpoints or attempt to detect, solve,
outsource, or bypass a CAPTCHA.

When human action is required, the acquisition enters `WAITING_FOR_USER`. The
Fox Den Music UI offers **Open interactive browser** only to an authenticated
user. That route obtains a short-lived, single-use, job-bound view token from a
broker and proxies the interactive session without publishing a new NAS port.
The view must use TLS, origin checks, CSRF protection for session creation, idle
timeout, and explicit close/cancel controls. Tokens and browser cookies never
appear in normal logs or acquisition events.

The user completes CAPTCHA, consent, and quality selection manually in the
visible browser. The adapter may observe the browser's ordinary download event,
but it must not turn CAPTCHA handling into a background automation step.

## Network policy

Place the browser on a dedicated internal network with no access to web,
worker, SQLite, Jellyfin, NAS management, RFC1918/ULA/link-local ranges,
metadata endpoints, or host gateway addresses. Give it only broker access plus
allowlisted outbound DNS and HTTPS required by the configured provider and its
documented content/CDN hosts. Re-resolve and enforce destinations at the
network layer to resist DNS rebinding; application URL validation alone is not
an egress firewall.

The broker should accept a narrow authenticated protocol: start session,
retrieve view capability, query coarse status, acknowledge a completed handoff,
and cancel. It must never expose arbitrary command execution, arbitrary URL
navigation, host path selection, or container-engine control to the web app.

## Lifecycle and resource controls

- Limit concurrent sessions globally and per authenticated user.
- Rate-limit creation and enforce a maximum queued lifetime.
- Bound browser runtime, idle time, download bytes, file count, and total job
  storage.
- Cancel on token replay, acquisition cancellation, manifest mismatch, sandbox
  health failure, or timeout.
- Terminate the browser before the worker accepts the handoff; unmount/delete
  the browser profile after retaining only completed artifact and concise audit
  metadata.
- Remove partial downloads after a documented retention period. Preserve a
  failed handoff only when useful for an administrator and never publish it.
- Pin browser and adapter versions and rebuild routinely for Chromium security
  updates. Provider failure must degrade only acquisition automation.

## Proposed Stage 3 state flow

```text
WAITING_FOR_USER
  -> BROWSER_STARTING
  -> WAITING_FOR_USER (interactive CAPTCHA/consent)
  -> DOWNLOADING
  -> VERIFYING_HANDOFF
  -> FILE_RECEIVED
  -> IMPORT_STARTED (existing Stage 1 job)
```

Browser-session state should live in a new provider-session table linked to the
existing Stage 2 `AcquisitionJob`; do not overload the import job. The existing
acquisition artifact and import relationship can accept the completed manifest
without a database redesign.

## Required tests before deployment

- The browser container has no `/music`, `/config`, secret, socket, device, or
  sibling-job mount and runs as non-root with its filesystem controls active.
- Attempts to reach NAS management, Jellyfin, cloud metadata, RFC1918, link
  local, host gateway, and non-allowlisted destinations fail.
- View tokens expire, reject replay/cross-job use, and are unavailable without
  the authenticated Fox Den Music front door.
- Traversal names, symlinks, hard links, FIFOs/devices, oversized downloads,
  partial files, unstable files, hash mismatch, nonce mismatch, and manifest
  replay never hand off.
- Browser termination occurs before intake; crash, timeout, cancel, and broker
  restart leave durable, understandable acquisition states.
- A valid synthetic ZIP flows from handoff through every unchanged Stage 1
  validation and imports exactly once.
- Provider outage or adapter disablement leaves direct upload, Stage 2 manual
  upload, library inventory, and existing imports fully operational.
