# v2.3.4 - Downloader home

The former Refresh browser view button is now Downloader home. It navigates the
active tab in remote Firefox to SPOTIDOWNLOADER_URL (https://spotidownloader.com/
by default) through the existing viewer connection. It leaves the copied Spotify
link intact and does not restart Firefox or the download watcher.

Back to request still returns to the current request with refreshed status.
No .env changes are needed. Deploy the package and rebuild/recreate web and worker
(or the entire Compose project as usual).

Test: after connecting, navigate away from the downloader home page, then tap
Downloader home. The remote address bar should return to spotidownloader.com.
Check on mobile too. If the viewer is disconnected, the control displays an error
instead of claiming it navigated. Completing a CAPTCHA remains a manual action.
