"""#27, #28: vendored MapLibre with integrity, and security headers."""

import base64
import hashlib
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pev_buddy.api import create_app

WEB = Path(__file__).resolve().parent.parent / "web"


@pytest.fixture()
def client(synth_data_dir):
    with TestClient(create_app(data_dir=str(synth_data_dir))) as c:
        yield c


def test_page_loads_no_third_party_script_or_style():
    html = (WEB / "index.html").read_text()
    for tag in re.findall(r"<(?:script|link)\b[^>]*>", html):
        src = re.search(r'(?:src|href)="([^"]+)"', tag)
        if src and ("<script" in tag or 'rel="stylesheet"' in tag):
            assert not src.group(1).startswith(("http:", "https:", "//")), tag


def test_vendored_maplibre_matches_its_integrity_hashes():
    html = (WEB / "index.html").read_text()
    pinned = re.findall(r'(?:src|href)="(vendor/[^"]+)" integrity="(sha384-[^"]+)"', html)
    assert len(pinned) == 2
    for path, sri in pinned:
        digest = "sha384-" + base64.b64encode(hashlib.sha384((WEB / path).read_bytes()).digest()).decode()
        assert digest == sri, path


def test_service_worker_precaches_the_vendored_files():
    sw = (WEB / "sw.js").read_text()
    assert "/vendor/maplibre-gl@4.7.1/maplibre-gl.js" in sw and "/vendor/maplibre-gl@4.7.1/maplibre-gl.css" in sw
    assert "jsdelivr" not in sw


def test_security_headers_on_pages_and_api(client):
    for path in ("/", "/api/health"):
        h = client.get(path).headers
        assert h["strict-transport-security"].startswith("max-age=")
        assert h["x-content-type-options"] == "nosniff"
        assert h["referrer-policy"] == "strict-origin-when-cross-origin"
        assert h["x-frame-options"] == "DENY"
        assert "geolocation=(self)" in h["permissions-policy"]
        csp = h["content-security-policy-report-only"]
        assert "frame-ancestors 'none'" in csp and "script-src 'self'" in csp and "worker-src 'self' blob:" in csp


def test_csp_can_be_enforced(synth_data_dir, monkeypatch):
    monkeypatch.setenv("PEV_BUDDY_CSP_ENFORCE", "1")
    with TestClient(create_app(data_dir=str(synth_data_dir))) as c:
        h = c.get("/").headers
        assert "content-security-policy" in h and "content-security-policy-report-only" not in h


def test_csp_reports_are_accepted(client):
    r = client.post("/api/csp-report", content=b'{"csp-report": {"violated-directive": "script-src"}}')
    assert r.status_code == 204
