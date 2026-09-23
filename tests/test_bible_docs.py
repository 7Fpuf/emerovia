"""Bible docs — every new v1.2.0 route is present in /openapi.json."""
from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

from fastapi.testclient import TestClient

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


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
