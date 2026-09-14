from decimal import Decimal
from urllib.parse import parse_qs

import httpx
import pytest

from app.models import DeliveryState, OrderTarget
from app.smm_client import ServiceIncompatible, SmmClient, sanitize

PANEL = "https://godofpanel.com/api/v2"
BIG_VIDEO_ID = "7312345678901234567"
VIDEO_URL = f"https://www.tiktok.com/@creator/video/{BIG_VIDEO_ID}"

TARGET = OrderTarget(
    video_id=BIG_VIDEO_ID,
    video_url=VIDEO_URL,
    comment_owner_username="real.handle",
    comment_id="7398765432109876543",
    comment_owner_user_id="6100000000000000001",
)


def make_client(handler):
    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(transport=transport)
    return http, SmmClient(http, panel_url=PANEL, api_key="test-key", service_id="5836")


@pytest.mark.asyncio
async def test_order_payload_is_exactly_the_documented_shape():
    seen = {}

    def handler(request):
        seen["method"] = request.method
        seen["url"] = str(request.url)
        seen["content_type"] = request.headers.get("content-type")
        seen["form"] = parse_qs(request.content.decode())
        return httpx.Response(200, json={"order": 234561})

    http, client = make_client(handler)
    async with http:
        result = await client.submit_order(TARGET, 500)

    assert result.accepted is True
    assert result.order_id == "234561"
    assert isinstance(result.order_id, str)

    assert seen["method"] == "POST"
    assert seen["url"] == PANEL
    assert seen["content_type"] == "application/x-www-form-urlencoded"

    form = {key: value[0] for key, value in seen["form"].items()}
    assert form == {
        "key": "test-key",
        "action": "add",
        "service": "5836",
        "link": VIDEO_URL,
        "quantity": "500",
        "username": "real.handle",
    }
    # The link is the VIDEO url, never a comment permalink, and no comment id
    # field is invented.
    assert "/video/" in form["link"]
    assert "comment" not in form
    assert TARGET.comment_id not in form["link"]


@pytest.mark.asyncio
async def test_missing_comment_permalink_does_not_block_the_order():
    """The target carries no permalink at all; the order still succeeds."""

    def handler(request):
        return httpx.Response(200, json={"order": "999"})

    http, client = make_client(handler)
    async with http:
        result = await client.submit_order(TARGET, 150)
    assert result.accepted is True


@pytest.mark.asyncio
async def test_zero_quantity_is_never_submitted():
    def handler(request):  # pragma: no cover - must not be called
        raise AssertionError("no request should be made")

    http, client = make_client(handler)
    async with http:
        with pytest.raises(ValueError):
            await client.submit_order(TARGET, 0)


@pytest.mark.asyncio
async def test_application_error_with_http_200_is_a_failure_not_a_success():
    def handler(request):
        return httpx.Response(200, json={"error": "Not enough funds"})

    http, client = make_client(handler)
    async with http:
        result = await client.submit_order(TARGET, 500)

    assert result.accepted is False
    assert result.unknown is False
    assert result.error == "Not enough funds"


@pytest.mark.asyncio
async def test_unrecognised_body_is_unknown_not_failed():
    def handler(request):
        return httpx.Response(200, json={"something_else": 1})

    http, client = make_client(handler)
    async with http:
        result = await client.submit_order(TARGET, 500)

    assert result.accepted is False
    assert result.unknown is True


@pytest.mark.asyncio
async def test_timeout_is_unknown_and_is_not_retried():
    calls = []

    def handler(request):
        calls.append(1)
        raise httpx.ReadTimeout("timed out", request=request)

    http, client = make_client(handler)
    async with http:
        result = await client.submit_order(TARGET, 500)

    assert result.unknown is True
    assert len(calls) == 1  # exactly one paid request attempt, never a retry


@pytest.mark.asyncio
async def test_server_error_on_order_creation_is_not_retried():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(502, text="bad gateway")

    http, client = make_client(handler)
    async with http:
        result = await client.submit_order(TARGET, 500)

    assert len(calls) == 1
    assert result.accepted is False
    assert result.unknown is True


@pytest.mark.asyncio
async def test_redirects_do_not_forward_the_api_key():
    hosts = []

    def handler(request):
        hosts.append(request.url.host)
        return httpx.Response(302, headers={"location": "https://elsewhere.example/api"})

    http, client = make_client(handler)
    async with http:
        result = await client.submit_order(TARGET, 500)

    assert hosts == ["godofpanel.com"]
    assert result.unknown is True


@pytest.mark.asyncio
async def test_service_metadata_is_validated_and_cached():
    calls = []

    def handler(request):
        calls.append(parse_qs(request.content.decode())["action"][0])
        return httpx.Response(
            200,
            json=[
                {"service": "1", "name": "Other", "type": "Default", "rate": "1.0", "min": "10", "max": "100"},
                {
                    "service": "5836",
                    "name": "TikTok Comment Likes",
                    "type": "15",
                    "category": "TikTok",
                    "rate": "0.85",
                    "min": "10",
                    "max": "1000000",
                    "refill": False,
                },
            ],
        )

    http, client = make_client(handler)
    async with http:
        metadata = await client.get_service_metadata()
        again = await client.get_service_metadata()

    assert metadata.service_id == "5836"
    assert metadata.min_quantity == 10
    assert metadata.max_quantity == 1_000_000
    assert metadata.rate == Decimal("0.85")
    assert client.service_supports_comment_likes(metadata) is True
    assert again is metadata
    assert calls == ["services"]  # cached: not called twice


@pytest.mark.asyncio
async def test_missing_service_is_an_incompatibility():
    def handler(request):
        return httpx.Response(200, json=[{"service": "1", "name": "x", "type": "Default", "min": "1", "max": "2"}])

    http, client = make_client(handler)
    async with http:
        with pytest.raises(ServiceIncompatible):
            await client.get_service_metadata()


@pytest.mark.asyncio
async def test_wrong_service_type_is_incompatible():
    def handler(request):
        return httpx.Response(
            200,
            json=[{"service": "5836", "name": "Views", "type": "Default", "rate": "1", "min": "10", "max": "100"}],
        )

    http, client = make_client(handler)
    async with http:
        metadata = await client.get_service_metadata()
    assert client.service_supports_comment_likes(metadata) is False


@pytest.mark.asyncio
async def test_status_maps_to_delivery_state_and_never_says_delivered_early():
    def handler(request):
        return httpx.Response(
            200,
            json={"charge": "0.4250", "start_count": "12", "status": "In progress", "remains": "488"},
        )

    http, client = make_client(handler)
    async with http:
        state, raw, charge, error = await client.get_order_status("234561")

    assert state is DeliveryState.IN_PROGRESS
    assert charge == Decimal("0.4250")
    assert error is None
    assert state is not DeliveryState.COMPLETED


def test_sanitize_hides_the_key():
    payload = {"key": "super-secret", "action": "add", "link": VIDEO_URL}
    assert sanitize(payload)["key"] == "***redacted***"
    assert "super-secret" not in str(sanitize(payload))
