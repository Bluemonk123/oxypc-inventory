"""
Integration tests for /api/v1/* endpoints.
All auth-protected routes should 401 without a valid Bearer token.
Health endpoint is public and should always return 200 or 503.
"""
import pytest
from httpx import AsyncClient, ASGITransport
from main import app


# ──────────────── Devices ────────────────────────────────────────────────────

async def test_devices_list_requires_bearer():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get("/api/v1/devices")
    assert resp.status_code == 401


async def test_devices_list_invalid_key():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get(
            "/api/v1/devices",
            headers={"Authorization": "Bearer ok_live_" + "x" * 64},
        )
    assert resp.status_code == 401


async def test_device_barcode_lookup_no_auth():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get("/api/v1/devices/NONEXISTENT-BARCODE")
    assert resp.status_code == 401


# ──────────────── Lots ───────────────────────────────────────────────────────

async def test_lots_list_requires_auth():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get("/api/v1/lots")
    assert resp.status_code == 401


async def test_lot_by_number_requires_auth():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get("/api/v1/lots/LOT-2024-0001")
    assert resp.status_code == 401


# ──────────────── Sales ──────────────────────────────────────────────────────

async def test_sales_list_requires_auth():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get("/api/v1/sales")
    assert resp.status_code == 401


# ──────────────── Spare Parts ────────────────────────────────────────────────

async def test_spare_parts_list_requires_auth():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get("/api/v1/spare-parts")
    assert resp.status_code == 401


# ──────────────── IQC register ───────────────────────────────────────────────

async def test_iqc_register_requires_auth():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post(
            "/api/v1/iqc/register",
            json={"barcode": "OXY-001", "lot_id": "00000000-0000-0000-0000-000000000000"},
        )
    assert resp.status_code == 401



# ──────────────── Health ─────────────────────────────────────────────────────

async def test_health_endpoint_is_public():
    """Health endpoint requires NO auth — uptime monitors call it without keys."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get("/api/v1/health")
    assert resp.status_code in (200, 503)
    data = resp.json()
    assert "status" in data
    assert "modules" in data
    assert "registered_modules" in data


async def test_health_response_shape():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get("/api/v1/health")
    data = resp.json()
    assert data["status"] in ("ok", "degraded")
    assert "database" in data["modules"]


# ──────────────── API Keys admin ─────────────────────────────────────────────

async def test_api_keys_list_requires_session():
    """
    /api/v1/api-keys uses session-cookie auth (browser admin panel).
    Without a session cookie the server redirects to /auth/login (307)
    rather than returning 401 — that is the expected session-auth behaviour.
    """
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        follow_redirects=False,
    ) as c:
        resp = await c.get("/api/v1/api-keys")
    assert resp.status_code in (302, 303, 307, 401, 403)


async def test_api_key_create_requires_session():
    """Same as above — POST without session gets redirect to /auth/login."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        follow_redirects=False,
    ) as c:
        resp = await c.post(
            "/api/v1/api-keys",
            json={"name": "Test", "scopes": ["devices:read"]},
        )
    assert resp.status_code in (302, 303, 307, 401, 403)
