# v2.3.3: browser view and shared server address

The gateway now publishes the existing WEB_PORT (8000 by default). It routes
Fox Den requests to web:8000 and /server-browser/ to download-browser:5800,
including the WebSocket connection. Use the same server IP as the main app;
no Tailscale browser address or separate port 5800 access is required.

Compose supplies REMOTE_BROWSER_URL=/server-browser/ regardless of an older
value in .env. Keep your current .env and secrets. The optional direct 5800
port remains available for troubleshooting. The gateway follows the browser
image's documented URL-path reverse proxy configuration.

Extract the complete package over your project, including gateway/nginx.conf,
then run:

```sh
docker compose up -d --build --force-recreate
```

Recreate the entire project: the gateway takes over the old web host port.
If the NAS interface recreates services individually, stop the old web container
before starting the gateway to release that port. Keep all existing data folders.

Manual test on desktop and phone:

1. Open Fox Den using the server IP and existing web port; health shows 2.3.3.
2. Open a request and choose Copy URL & start watched download.
3. Confirm the browser appears under a toolbar on the same server address.
4. Complete a download. Firefox now defaults to /downloads.
5. Choose Back to request. The same request reloads with its latest status;
   the inbox watcher continues even after leaving the view.
6. Reopen the view and choose Refresh browser view. Only the viewer reconnects;
   Firefox and its ongoing downloads are not restarted.

The gateway does not create remote network access. Your phone still needs a
route to the main Fox Den address, as it did when opening the main page.
