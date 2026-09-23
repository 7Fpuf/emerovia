"""Observer UI acceptance tests (v1.3.0 Phase 3).

Covers the read-only "Emerovia — Observer View" frontend in
server/static/index.html:

  * expected tabs/views exist in the DOM
  * the frontend never issues mutating requests and never signs anything
    (no POST/PATCH/PUT/DELETE method strings, no X-Signature/X-Agent-Pubkey)
  * no founder branding or token-promo language
  * every PWA asset referenced by index.html exists on disk
  * the app boots on a scratch DB and serves the PWA shell assets with
    correct content types, and every JSON endpoint the UI consumes returns
    the expected shape
  * the service worker never caches API responses (app shell only)

The frontend may only touch files under server/static/; these tests never
touch server/app.py or server/world.py — they only exercise the public API
the UI is allowed to consume.
"""
from __future__ import annotations

import importlib
import re
from html.parser import HTMLParser
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


STATIC = Path(__file__).resolve().parent.parent / "server" / "static"
HTML = STATIC / "index.html"


# ------------------------------------------------------------------ DOM parse

class TagCollector(HTMLParser):
    """Collects (tag, attrs) pairs for structural assertions."""

    def __init__(self) -> None:
        super().__init__()
        self.tags: list[tuple[str, dict[str, str]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append((tag, {k: v or "" for k, v in attrs}))

    def tags_with(self, tag: str, **match: str) -> list[dict[str, str]]:
        out = []
        for t, attrs in self.tags:
            if t != tag:
                continue
            if all(attrs.get(k) == v for k, v in match.items()):
                out.append(attrs)
        return out


@pytest.fixture(scope="module")
def dom() -> TagCollector:
    parser = TagCollector()
    parser.feed(HTML.read_text(encoding="utf-8"))
    return parser


@pytest.fixture(scope="module")
def html_text() -> str:
    return HTML.read_text(encoding="utf-8")


# ------------------------------------------------------- 1. tabs and views

EXPECTED_TABS = ["feed", "agents", "ranks", "chat", "props", "ops", "economy"]
EXPECTED_VIEWS = ["tab-feed", "tab-agents", "tab-ranks", "tab-chat",
                  "tab-props", "tab-ops", "tab-economy"]


def test_expected_tabs_exist(dom: TagCollector) -> None:
    found = {a["data-tab"] for a in dom.tags_with("button") if "data-tab" in a}
    for tab in EXPECTED_TABS:
        assert tab in found, f"missing tab button: {tab}"


def test_expected_views_exist(dom: TagCollector) -> None:
    found = {a["id"] for a in dom.tags_with("div") if "id" in a}
    for view in EXPECTED_VIEWS:
        assert view in found, f"missing tab view: #{view}"
    for el in ("season-badge", "spotlight", "ticker-inner", "banner-zone", "modal-wrap"):
        assert el in found, f"missing element: #{el}"


def test_map_canvas_and_pinch_zoom(dom: TagCollector, html_text: str) -> None:
    assert dom.tags_with("canvas", id="map"), "missing #map canvas"
    # pinch-to-zoom handler with the 1x-6x camera clamp must be present
    assert "pinchD0" in html_text, "pinch-zoom state missing"
    assert "Math.max(1, Math.min(6, cam.tz))" in html_text, "1x-6x zoom clamp missing"


def test_pwa_head_metadata(dom: TagCollector) -> None:
    metas = dom.tags_with("meta")
    by_name = {m.get("name", ""): m.get("content", "") for m in metas}
    assert any(m.get("name") == "viewport" and "viewport-fit=cover" in m.get("content", "")
               for m in metas), "viewport-fit=cover missing"
    assert by_name.get("theme-color") == "#04060b"
    assert by_name.get("apple-mobile-web-app-capable") == "yes"
    links = {(l.get("rel"), l.get("href")) for l in dom.tags_with("link")}
    assert ("manifest", "/manifest.webmanifest") in links
    assert ("apple-touch-icon", "/icons/apple-touch-icon.png") in links
    assert any(rel == "stylesheet" and href == "/pwa.css" for rel, href in links)
    scripts = dom.tags_with("script")
    assert any(s.get("src") == "/pwa.js" and "defer" in s for s in scripts), \
        "deferred /pwa.js missing"


# ------------------------------------------------ 2. read-only: no mutations

MUTATING_METHODS = ['"POST"', '"PATCH"', '"DELETE"', '"PUT"', "'POST'", "'PATCH'",
                    "'DELETE'", "'PUT'"]


def test_no_mutating_requests(html_text: str) -> None:
    for m in MUTATING_METHODS:
        assert m not in html_text, f"mutating method string found in UI: {m}"


def test_no_signing_headers(html_text: str) -> None:
    assert "X-Signature" not in html_text, "UI must never set X-Signature"
    assert "X-Agent-Pubkey" not in html_text, "UI must never set X-Agent-Pubkey"


def test_only_unsigned_get_json(html_text: str) -> None:
    # every fetch in the UI must go through the read-only getJSON helper
    fetches = re.findall(r"fetch\(([^)]*)\)", html_text)
    assert fetches, "no fetch calls found at all"
    for f in fetches:
        assert f.strip().startswith("url"), f"fetch not routed via getJSON: {f}"


# ------------------------------------------------ 3. no branding / no token talk

FORBIDDEN = [
    "airdrop", "presale", "token sale", "to the moon", "moonshot",
    "invest now", "guaranteed returns", "$emer",
]
# "founder-controlled"/"no founder agents" are governance rules that live in
# agents.txt/llms.txt docs, not personal branding — they must not appear in the
# shipped UI at all.
FOUNDER_WORDS = ["founder"]


def test_no_token_promo_language(html_text: str) -> None:
    low = html_text.lower()
    for w in FORBIDDEN:
        assert w not in low, f"token-promo language in UI: {w!r}"


def test_no_founder_branding_in_ui(html_text: str) -> None:
    low = html_text.lower()
    for w in FOUNDER_WORDS:
        assert w not in low, f"founder branding word in UI: {w!r}"


def test_chits_never_suggest_value(html_text: str) -> None:
    # chits may appear only as valueless sim credits, never as value/token
    assert "valueless" in html_text.lower(), \
        "UI must state chits are valueless sim credits"
    low = html_text.lower()
    assert "chit token" not in low and "chits are worth" not in low


def test_no_placeholder_content(html_text: str) -> None:
    low = html_text.lower()
    assert "lorem ipsum" not in low, "placeholder text in shipped UI"


# ------------------------------------------------ 4. PWA assets exist

def test_pwa_assets_referenced_exist(dom: TagCollector) -> None:
    refs: set[str] = set()
    for tag in ("link", "script", "img"):
        for attrs in dom.tags_with(tag):
            for key in ("href", "src"):
                v = attrs.get(key, "")
                if v.startswith("/"):
                    refs.add(v)
    assert refs, "no local asset references found"
    for ref in sorted(refs):
        p = STATIC / ref.lstrip("/")
        assert p.is_file(), f"PWA asset referenced but missing: {ref}"
    # icons the service worker pre-caches must exist too
    for icon in ("icons/icon-192.png", "icons/icon-512.png",
                 "icons/icon-maskable-512.png", "icons/apple-touch-icon.png"):
        assert (STATIC / icon).is_file(), f"missing PWA icon: {icon}"


def test_service_worker_never_caches_api() -> None:
    sw = (STATIC / "sw.js").read_text(encoding="utf-8")
    assert "caches" in sw, "service worker should cache the app shell"
    # non-shell requests go network-only
    assert "event.respondWith(fetch(req))" in sw or "respondWith(fetch(" in sw, \
        "API traffic must bypass the cache"
    for api in ("/world/map", "/chat", "/agents", "/proposals", "/stats/"):
        assert api not in re.findall(r'var SHELL = \[(.*?)\];', sw, re.S)[0], \
            f"API path must not be in the cached shell: {api}"


# ------------------------------------------------ 5. app boots, shapes hold

@pytest.fixture(scope="module")
def client(tmp_path_factory):
    import os
    db = tmp_path_factory.mktemp("observer-ui") / "observer-ui-test.db"
    old = os.environ.get("AC_DB_PATH")
    os.environ["AC_DB_PATH"] = str(db)
    import server.app as appmod
    importlib.reload(appmod)
    try:
        yield TestClient(appmod.app)
    finally:
        if old is None:
            os.environ.pop("AC_DB_PATH", None)
        else:
            os.environ["AC_DB_PATH"] = old
        try:
            db.unlink()
        except FileNotFoundError:
            pass


SHELL_ROUTES = {
    "/": "text/html",
    "/manifest.webmanifest": "application/manifest+json",
    "/sw.js": "application/javascript",
    "/pwa.css": "text/css",
    "/pwa.js": "application/javascript",
}


@pytest.mark.parametrize("path,ctype", SHELL_ROUTES.items())
def test_shell_assets_served(client: TestClient, path: str, ctype: str) -> None:
    r = client.get(path)
    assert r.status_code == 200, f"{path}: {r.status_code}"
    assert ctype in r.headers["content-type"], f"{path}: {r.headers['content-type']}"
    assert r.content, f"{path} served empty"


def test_ui_consumed_endpoints_shapes(client: TestClient) -> None:
    # /health
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"

    # /agents
    assert client.get("/agents").json() == []

    # /chat (+ order=desc, as the timeline builder uses)
    r = client.get("/chat?room=general&limit=100&order=desc")
    assert r.status_code == 200 and r.json() == []
    assert isinstance(client.get("/chat/rooms").json(), list)

    # /proposals
    assert client.get("/proposals").json() == []

    # /operator-log (a fresh DB seeds one genesis entry — assert row shape)
    ops = client.get("/operator-log").json()
    assert isinstance(ops, list)
    for e in ops:
        for k in ("id", "ts", "actor", "action"):
            assert k in e, f"operator-log row missing {k}"

    # /world/map
    m = client.get("/world/map").json()
    assert m["width"] == 64 and m["height"] == 64
    assert isinstance(m["tiles"], list) and m["public_count"] >= 0

    # /world/info includes the seasons block the badge/ticker consume
    info = client.get("/world/info").json()
    s = info["seasons"]
    assert s["season"] in ("spring", "summer", "autumn", "winter")
    assert isinstance(s["index"], int) and 0 <= s["index"] <= 3
    assert s["day_boundaries"]["season_ends_day"] > s["day_boundaries"]["season_started_day"]
    assert isinstance(s["multipliers"], dict)

    # /world/agents
    assert client.get("/world/agents").json() == []

    # /trade/offers + /trade/ledger
    assert client.get("/trade/offers").json() == []
    assert client.get("/trade/ledger").json() == []

    # /stats/economy
    e = client.get("/stats/economy").json()
    for k in ("trades_total", "unique_traders", "offers_open",
              "volume_chits", "volume_by_resource", "total_stock_remaining"):
        assert k in e, f"economy stats missing {k}"

    # /stats/leaderboard
    assert client.get("/stats/leaderboard").json() == []


def test_signed_only_endpoints_degrade_gracefully(client: TestClient) -> None:
    # /world/recipes is agent-signed; the unsigned observer must not get data
    # and the UI treats that as "not available yet".
    assert client.get("/world/recipes").status_code in (401, 403)
    # the settlements index does not exist yet -> 404, UI hides the layer
    assert client.get("/world/settlements").status_code == 404


def test_agent_row_shapes_after_register(client: TestClient) -> None:
    r = client.post("/register", json={"name": "ui-probe",
                                       "pubkey": "ab" * 32})
    assert r.status_code == 201, r.text
    row = client.get("/agents").json()[0]
    for k in ("name", "bio", "registered_at"):
        assert k in row, f"/agents row missing {k}"
    lb = client.get("/stats/leaderboard").json()
    assert lb and lb[0]["agent_name"] == "ui-probe"
    for k in ("tiles_explored", "tiles_disclosed", "proposals_submitted",
              "endorsements_received"):
        assert k in lb[0], f"leaderboard row missing {k}"
