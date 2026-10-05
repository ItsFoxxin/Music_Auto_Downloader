# v2.4.0 - Match Media Downloader workspace styling and layout

## Changes

- Reference: `ItsFoxxin/Media_Downloader` dev commit `230dd7c`.
- Shared charcoal, peach, mint, and muted-violet palette with softer panels.
- Fixed desktop sidebar and two horizontally scrollable mobile navigation rows.
- Current-section highlighting, library shortcuts, breadcrumb header, and footer.
- Dashboard typography, metric cards, and download/check/tag/import workflow strip.
- Consistent forms, upload controls, tables, metadata review, library cards, and
  full-screen browser toolbar. Existing request URLs and form/polling hooks remain.
- Theme isolated in `workspace.css`, after existing component CSS, with versioned
  asset URLs. No third-party fonts, scripts, or other new network dependencies.

No new APIs, accounts, environment variables, or data migrations. Import safety,
MusicBrainz matching, download watching, and browser controls are unchanged.
The standalone music repository is updated; the separate Media Downloader
repository and its bundled music snapshot are not modified by this release.

## Deploy on the NAS

Back up your current project files and keep your existing `.env`, secrets,
`data/`, browser profile, downloads, and music. Extract the NAS ZIP over the
existing Fox-Den-Music project (do not replace it with an empty project).

In the NAS terminal, from that project directory:

```sh
docker compose -f docker-compose.yaml up -d --build --force-recreate
docker compose -f docker-compose.yaml ps
```

Both Compose filenames are maintained identically; explicitly use `.yaml`.
The NAS ZIP uses the flat build context `.`; the GitHub checkout uses `./backend`.
Do not mix their Compose files. Hard-refresh the page once if it looks unchanged.
Recreating containers can interrupt an active import/download; deploy when idle.

## Manual check

1. Open the dashboard. Confirm version `2.4.0` in the sidebar/footer, peach
   actions, and the new sidebar. Existing counts should be preserved.
2. Visit Add music, Queue, Review, Library, and History. On a phone, swipe the
   first navigation row to reach History; use the second row for library views.
3. Add a Spotify link. Open its request and confirm the copy/start action still
   opens the browser. Try Downloader home and Back to request; status should
   refresh and the watcher should continue running.
4. Upload a small known-good album ZIP. Check file selection/progress, metadata
   review, and completion. This release does not change matching decisions.
5. Use Refresh library when ready, then browse Artists, Albums, and Library
   health. Tables can scroll horizontally on small screens.
6. Use Tab on desktop to verify visible focus and Skip to main content.

## Validation boundary

- Full Python suite: **224 passed, 2 skipped** (Windows cannot create symlinks).
- Added 10 checks covering shared navigation, current sections, versioned
  stylesheet delivery, CSP, polling hooks, and CSRF-protected scan form.
- Headless Edge: 12 populated screens at 1440, 1024, 768, 390, and 320px;
  no document overflow or JavaScript errors. Link submission and browser return
  worked at each width. Dashboard/upload/review/library screenshots inspected.
- The remote viewer was stubbed in these visual checks. No production downloads,
  library files, metadata, or NAS settings were touched.
- Live NAS deployment and a real phone check remain required after uploading.
