from html.parser import HTMLParser

import pytest
from fastapi.testclient import TestClient

from foxden_music import __version__
from foxden_music.web import create_app


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.current = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "a" and attrs.get("aria-current") == "page":
            self.current.append(attrs["href"])


@pytest.mark.parametrize("path,active", [
    ("/", ["/"]), ("/add", ["/add"]),
    ("/acquisitions", ["/acquisitions"]), ("/review", ["/review"]),
    ("/history", ["/history"]), ("/library", ["/library"]),
    ("/library/albums", ["/library", "/library/albums"]),
    ("/library/artists", ["/library", "/library/artists"]),
    ("/library/health", ["/library", "/library/health"]),
])
def test_workspace_shell_and_current_navigation(settings, database, path, active):
    with TestClient(create_app(settings, database)) as client:
        response = client.get(path)
        assert response.status_code == 200
        assert 'class="sidebar"' in response.text
        assert 'aria-label="Main navigation"' in response.text
        assert 'aria-label="Browse library"' in response.text
        assert 'href="#main-content"' in response.text
        assert f'/workspace.css?v={__version__}' in response.text
        links = Links()
        links.feed(response.text)
        assert links.current == active
        assert "default-src 'self'" in response.headers["content-security-policy"]


def test_dashboard_keeps_polling_and_manual_scan_form(settings, database):
    with TestClient(create_app(settings, database)) as client:
        html = client.get("/").text
        for fragment in ("metrics", "active", "recent"):
            assert f'data-poll-url="/partials/dashboard/{fragment}"' in html
        assert 'action="/library/scans" method="post"' in html
        assert 'name="csrf_token"' in html
        assert 'aria-label="How music reaches your library"' in html
        assert client.get("/static/workspace.css").status_code == 200
