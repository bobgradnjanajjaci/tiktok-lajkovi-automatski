import httpx
import pytest

from app import url_resolver
from app.url_resolver import (
    UrlError,
    canonical_url,
    extract_video_id,
    is_short_link,
    normalize_lock_key,
    parse_canonical,
    resolve_video,
)

BIG_ID = "7312345678901234567"  # larger than 2**53 - 1


@pytest.fixture(autouse=True)
def allow_dns(monkeypatch):
    async def ok(_host):
        return None

    monkeypatch.setattr(url_resolver, "_assert_public_host", ok)


def test_canonical_parsing():
    url = f"https://www.tiktok.com/@some.user/video/{BIG_ID}"
    assert parse_canonical(url) == ("some.user", BIG_ID)
    assert extract_video_id(url) == BIG_ID
    assert isinstance(extract_video_id(url), str)


def test_trailing_slash_does_not_change_identity():
    a = parse_canonical(f"https://www.tiktok.com/@u/video/{BIG_ID}")
    b = parse_canonical(f"https://www.tiktok.com/@u/video/{BIG_ID}/")
    assert a == b == ("u", BIG_ID)


@pytest.mark.parametrize(
    "url",
    [
        "http://www.tiktok.com/@u/video/7312345678901234567",
        "https://tiktok.com.evil.example/@u/video/7312345678901234567",
        "https://example.com/@u/video/7312345678901234567",
        "https://www.tiktok.com:8443/@u/video/7312345678901234567",
    ],
)
def test_rejected_hosts_and_schemes(url):
    assert parse_canonical(url) is None
    assert extract_video_id(url) is None


def test_short_link_detection():
    assert is_short_link("https://vm.tiktok.com/ZMabcdefg/") is True
    assert is_short_link("https://vt.tiktok.com/ZSabcdefg") is True
    assert is_short_link("https://www.tiktok.com/t/ZTabcdefg/") is True
    assert is_short_link(f"https://www.tiktok.com/@u/video/{BIG_ID}") is False


@pytest.mark.asyncio
async def test_canonical_url_needs_no_network():
    calls = []

    def handler(request):
        calls.append(request.url)
        return httpx.Response(200)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        identity = await resolve_video(client, f"https://www.tiktok.com/@creator/video/{BIG_ID}")
    assert identity.video_id == BIG_ID
    assert identity.canonical_url == canonical_url("creator", BIG_ID)
    assert identity.verified is True
    assert calls == []


@pytest.mark.asyncio
async def test_short_link_is_followed_to_the_canonical_url():
    def handler(request):
        if request.url.host == "vm.tiktok.com":
            return httpx.Response(
                302,
                headers={
                    "location": f"https://www.tiktok.com/@creator/video/{BIG_ID}?is_from_webapp=1"
                },
            )
        return httpx.Response(200)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        identity = await resolve_video(client, "https://vm.tiktok.com/ZMabcdefg/")

    assert identity.video_id == BIG_ID
    assert identity.canonical_url == canonical_url("creator", BIG_ID)
    assert "is_from_webapp" not in identity.canonical_url
    assert identity.redirects == 1
    assert identity.verified is True


@pytest.mark.asyncio
async def test_redirect_to_a_foreign_host_is_refused():
    def handler(request):
        return httpx.Response(302, headers={"location": "https://evil.example/whatever"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(UrlError):
            await resolve_video(client, "https://vm.tiktok.com/ZMabcdefg/")


@pytest.mark.asyncio
async def test_redirect_loop_is_bounded():
    def handler(request):
        return httpx.Response(302, headers={"location": "https://vm.tiktok.com/ZMnext/"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(UrlError):
            await resolve_video(client, "https://vm.tiktok.com/ZMabcdefg/")


@pytest.mark.asyncio
async def test_profile_link_is_rejected():
    def handler(request):
        return httpx.Response(200)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(UrlError):
            await resolve_video(client, "https://www.tiktok.com/@creator")


def test_lock_key_is_alias_independent():
    a = normalize_lock_key("https://godofpanel.com/api/v2", BIG_ID)
    b = normalize_lock_key("https://godofpanel.com/api/v2/", BIG_ID)
    assert a == b
    assert BIG_ID in a
