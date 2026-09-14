"""Resolve whatever the operator pasted into a verified canonical video URL.

Security rules enforced here:
  * https only, and only on the intended TikTok hosts;
  * every hop of a redirect chain is re-validated, not just the first URL;
  * hostnames that resolve to private, loopback, link-local or reserved
    addresses are rejected (SSRF guard);
  * the redirect chain is bounded.

The canonical URL produced here is the exact value sent as the panel's ``link``.
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from urllib.parse import urlsplit, urlunsplit

import httpx

from .models import VideoIdentity

ALLOWED_HOSTS = frozenset(
    {
        "tiktok.com",
        "www.tiktok.com",
        "m.tiktok.com",
        "vm.tiktok.com",
        "vt.tiktok.com",
    }
)

MAX_REDIRECTS = 5

_CANONICAL = re.compile(
    r"^/@(?P<handle>[A-Za-z0-9._]{1,64})/(?:video|photo)/(?P<video_id>\d{5,32})/?$"
)
_LEGACY_V = re.compile(r"^/v/(?P<video_id>\d{5,32})(?:\.html)?/?$")
_EMBED = re.compile(r"^/embed/(?:v2/)?(?P<video_id>\d{5,32})/?$")
_SHORT_PATHS = (re.compile(r"^/t/(?P<code>[A-Za-z0-9]+)/?$"), re.compile(r"^/(?P<code>[A-Za-z0-9]{5,})/?$"))


class UrlError(ValueError):
    """The pasted link is not a usable TikTok video URL."""


def _split(url: str):
    parts = urlsplit(url.strip())
    if parts.scheme.lower() != "https":
        raise UrlError(f"only https URLs are accepted, got {parts.scheme or 'no scheme'!r}")
    host = (parts.hostname or "").lower()
    if host not in ALLOWED_HOSTS:
        raise UrlError(f"host {host or '(none)'!r} is not an allowed TikTok host")
    if parts.port not in (None, 443):
        raise UrlError("non-standard ports are not allowed")
    return parts, host


def canonical_url(handle: str, video_id: str) -> str:
    return f"https://www.tiktok.com/@{handle}/video/{video_id}"


def parse_canonical(url: str) -> tuple[str, str] | None:
    """Return (handle, video_id) when the URL already identifies a video."""
    try:
        parts, _host = _split(url)
    except UrlError:
        return None
    path = parts.path or "/"
    if not path.endswith("/"):
        pass
    match = _CANONICAL.match(path)
    if match:
        return match.group("handle"), match.group("video_id")
    return None


def extract_video_id(url: str) -> str | None:
    """Extract a video id from any recognised long-form TikTok URL shape."""
    try:
        parts, _host = _split(url)
    except UrlError:
        return None
    path = parts.path or "/"
    for pattern in (_CANONICAL, _LEGACY_V, _EMBED):
        match = pattern.match(path)
        if match:
            return match.group("video_id")
    return None


def is_short_link(url: str) -> bool:
    try:
        parts, host = _split(url)
    except UrlError:
        return False
    path = parts.path or "/"
    if host in {"vm.tiktok.com", "vt.tiktok.com"}:
        return _SHORT_PATHS[1].match(path) is not None
    if host in {"www.tiktok.com", "tiktok.com"}:
        return _SHORT_PATHS[0].match(path) is not None
    return False


def _strip_tracking(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


async def _assert_public_host(host: str) -> None:
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:  # pragma: no cover - network dependent
        raise UrlError(f"cannot resolve host {host!r}: {exc}") from exc
    for info in infos:
        address = info[4][0]
        ip = ipaddress.ip_address(address)
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            raise UrlError(f"host {host!r} resolves to a non-public address {address}")


async def resolve_video(client: httpx.AsyncClient, input_url: str) -> VideoIdentity:
    """Resolve ``input_url`` to a verified canonical video URL and string id."""
    current = input_url.strip()
    if not current:
        raise UrlError("empty link")
    if "://" not in current:
        current = "https://" + current.lstrip("/")

    parts, host = _split(current)

    # A link that is already canonical needs no request at all, so there is no
    # DNS lookup and no redirect walk on the fast path.
    direct = parse_canonical(current)
    if direct:
        handle, video_id = direct
        return VideoIdentity(
            input_url=input_url,
            video_id=video_id,
            canonical_url=canonical_url(handle, video_id),
            author_handle=handle,
            verified=True,
        )

    await _assert_public_host(host)

    # Long-form but not canonical (for example /v/<id>.html or /embed/<id>):
    # we have a trustworthy id but no handle, so a redirect walk is still needed
    # to produce a canonical URL we are willing to send to the panel.
    redirects = 0
    location = current
    while redirects <= MAX_REDIRECTS:
        parts, host = _split(location)
        await _assert_public_host(host)
        response = await client.get(
            location,
            follow_redirects=False,
            headers={
                "user-agent": (
                    "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/120 Mobile Safari/537.36"
                ),
                "accept-language": "en-US,en;q=0.9",
            },
        )
        if response.status_code in (301, 302, 303, 307, 308):
            next_location = response.headers.get("location")
            if not next_location:
                raise UrlError("redirect response had no Location header")
            location = str(httpx.URL(location).join(next_location))
            redirects += 1
            continue
        break
    else:
        raise UrlError(f"redirect chain exceeded {MAX_REDIRECTS} hops")

    resolved = _strip_tracking(location)
    direct = parse_canonical(resolved)
    if direct:
        handle, video_id = direct
        return VideoIdentity(
            input_url=input_url,
            video_id=video_id,
            canonical_url=canonical_url(handle, video_id),
            author_handle=handle,
            verified=True,
            redirects=redirects,
        )

    video_id = extract_video_id(resolved) or extract_video_id(current)
    if not video_id:
        raise UrlError(
            "could not extract a video id; the link may be a profile, a live "
            "stream, deleted, or region blocked"
        )
    # We know the id but not the handle. The id alone is enough to READ comments,
    # but not to build a canonical URL we can honestly call verified, so Live
    # ordering will be blocked with target_unverified.
    return VideoIdentity(
        input_url=input_url,
        video_id=video_id,
        canonical_url=resolved,
        author_handle=None,
        verified=False,
        redirects=redirects,
        note="resolved without an @handle; canonical URL is unverified",
    )


def normalize_lock_key(panel_url: str, video_id: str) -> str:
    """Duplicate protection key: the panel plus the resolved video id.

    Short-link aliases and username changes cannot evade this because the key
    depends only on the resolved id.
    """
    return f"{panel_url.rstrip('/')}|video:{video_id}"
