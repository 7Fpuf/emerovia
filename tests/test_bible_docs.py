"""Bible docs — every new v1.2.0 route is present in /openapi.json.

Plus: every mutating route the server exposes is documented in BOTH
/openapi.json and server/static/agents.txt (the agent-facing contract).
"""
from __future__ import annotations

import importlib
import os
import re
import sys
from pathlib import Path

from fastapi.testclient import TestClient

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _make_client(tmp_path, monkeypatch):
    monkeypatch.setenv("AC_DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("AC_OPERATOR_PUBKEY", "ab" * 32)
    import server.app as appmod
    importlib.reload(appmod)
    return TestClient(appmod.app), appmod


def _mutating_routes(appmod):
    """(path, method) for every mutating route on the app."""
    out = []
    for route in appmod.app.routes:
        methods = getattr(route, "methods", None) or set()
        mut = sorted(m for m in methods if m in MUTATING_METHODS)
        if mut:
            for m in mut:
                out.append((route.path, m))
    return sorted(out)


def test_mutating_routes_in_openapi_and_agents_txt(tmp_path, monkeypatch):
    client, appmod = _make_client(tmp_path, monkeypatch)
    paths = client.get("/openapi.json").json()["paths"]
    agents_txt = (REPO / "server" / "static" / "agents.txt").read_text()

    missing_openapi = []
    missing_txt = []
    for path, method in _mutating_routes(appmod):
        # 1) OpenAPI surface
        if path not in paths or method.lower() not in paths[path]:
            missing_openapi.append(f"{method} {path}")
        # 2) agents.txt — path placeholders ({proposal_id} vs {id}) are
        # normalized to a wildcard so doc shorthand doesn't trip the check.
        pattern = re.sub(r"\{[^}]*\}", r"{[^}]*}", re.escape(path))
        if not re.search(pattern, agents_txt):
            missing_txt.append(f"{method} {path}")
    assert not missing_openapi, f"mutating routes missing from OpenAPI: {missing_openapi}"
    assert not missing_txt, f"mutating routes missing from agents.txt: {missing_txt}"

    # The migration announcements (Bible §2.4, announced not silent) must
    # appear in BOTH /world/info and agents.txt.
    info = client.get("/world/info").json()
    assert "migration" in info, "/world/info must document the §2.4 migration"
    assert "iron_ore" in info["migration"]["ore_to_iron_ore"]
    assert "sand" in info["migration"]["glass_becomes_refined"]
    assert "iron_ore" in agents_txt and "MIGRATION" in agents_txt


def test_openapi_covers_bible_routes(tmp_path, monkeypatch):
    monkeypatch.setenv("AC_DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("AC_OPERATOR_PUBKEY", "ab" * 32)
    import server.app as appmod
    importlib.reload(appmod)
    client = TestClient(appmod.app)
    paths = client.get("/openapi.json").json()["paths"]
    expected = {
        "/eat": ["post"],
        "/world/gather": ["post"],
        "/world/craft": ["post"],
        "/world/experiment": ["post"],
        "/world/recipes": ["get"],
        "/world/refine": ["post"],
        "/world/claim": ["post"],
        "/world/build": ["post"],
        "/world/demolish": ["post"],
        "/world/transfer": ["post"],
        "/world/farm": ["post"],
        "/world/tithe": ["post"],
        "/world/settlements/name": ["post"],
        "/world/settlements/contribute": ["post"],
        "/world/settlements/disburse": ["post"],
        "/world/settlements/disburse/approve": ["post"],
        "/world/settlements/projects": ["post"],
        "/world/settlements/projects/contribute": ["post"],
        "/world/settlements/projects/complete": ["post"],
        "/world/settlements": ["get"],
        "/world/settlements/{settlement_id}": ["get"],
        "/world/settlements/{settlement_id}/ledger": ["get"],
        "/world/info": ["get"],
    }
    for path, methods in expected.items():
        assert path in paths, f"missing from OpenAPI: {path}"
        for method in methods:
            assert method in paths[path], f"{path} missing {method}"
    # Removed Bible-obsolete routes stay gone.
    for gone in ("/world/settlements/form", "/world/settlements/join",
                 "/world/settlements/feast"):
        assert gone not in paths, f"obsolete route still served: {gone}"
    # Seasons are published in /world/info.
    info = client.get("/world/info").json()
    assert info["seasons"]["season"] in ("spring", "summer", "autumn", "winter")
    assert len(info["seasons"]["multipliers"]) == 12
