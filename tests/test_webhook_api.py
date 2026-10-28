"""
Integration tests for /api/v1/webhooks/* endpoints.
All routes use session-cookie auth — no session → redirect (307) or 401/403.
"""
import pytest
from httpx import AsyncClient, ASGITransport
from main import app


async def test_webhooks_list_requires_session():
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        follow_redirects=False,
    ) as c:
        resp = await c.get("/api/v1/webhooks")
    assert resp.status_code in (302, 303, 307, 401, 403)


async def test_webhook_create_requires_session():
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        follow_redirects=False,
    ) as c:
        resp = await c.post(
            "/api/v1/webhooks",
            json={
                "name": "Test Hook",
                "url": "https://example.com/webhook",
                "secret": "s3cr3t",
                "event_types": ["DEVICE_REGISTERED"],
            },
        )
    assert resp.status_code in (302, 303, 307, 401, 403)


async def test_webhook_delete_requires_session():
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        follow_redirects=False,
    ) as c:
        resp = await c.delete("/api/v1/webhooks/00000000-0000-0000-0000-000000000000")
    assert resp.status_code in (302, 303, 307, 401, 403)


async def test_webhook_events_requires_session():
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        follow_redirects=False,
    ) as c:
        resp = await c.get("/api/v1/webhooks/00000000-0000-0000-0000-000000000000/events")
    assert resp.status_code in (302, 303, 307, 401, 403)


async def test_webhook_schema_create_request_valid():
    from schemas.webhook import WebhookCreateRequest
    req = WebhookCreateRequest(
        name="My Hook",
        url="https://example.com/wh",
        secret="supersecret",
        event_types=["DEVICE_REGISTERED", "SALE_COMPLETED"],
    )
    assert req.name == "My Hook"
    assert req.event_types == ["DEVICE_REGISTERED", "SALE_COMPLETED"]
    assert req.is_active is True


async def test_webhook_schema_list_item_uuid_coercion():
    import uuid
    from datetime import datetime
    from schemas.webhook import WebhookListItem

    item = WebhookListItem.model_validate({
        "id": uuid.uuid4(),
        "name": "Hook",
        "url": "https://example.com",
        "event_types": ["LOT_CREATED"],
        "is_active": True,
        "created_by": "admin",
        "created_at": datetime(2026, 4, 30, 12, 0, 0),
    })
    assert isinstance(item.id, str)
    assert "2026" in item.created_at


async def test_event_log_schema_optional_fields():
    import uuid
    from datetime import datetime
    from schemas.webhook import EventLogItem

    item = EventLogItem.model_validate({
        "id": uuid.uuid4(),
        "event_type": "DEVICE_REGISTERED",
        "payload": {"barcode": "OXY-001"},
        "source_module": None,
        "published_at": datetime(2026, 4, 30),
        "webhook_attempts": 0,
        "last_attempt_at": None,
        "last_status_code": None,
    })
    assert item.source_module is None
    assert item.last_attempt_at is None
    assert item.webhook_attempts == 0
