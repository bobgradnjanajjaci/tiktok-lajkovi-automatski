"""Comment reader adapters.

``ScrapeCreatorsCommentReader`` (``COMMENT_READER=scrapecreators``, the default)
    The concrete live integration. Endpoints, parameters and field mapping are
    implemented in code from ScrapeCreators' published documentation, so the
    operator configures nothing except ``READER_API_KEY``.

``ContractHttpCommentReader`` (``COMMENT_READER=http``)
    Compatibility adapter for a different provider, driven by an
    operator-written contract file. Optional; it is not the only path to a
    working setup.

``FixtureCommentReader`` (``COMMENT_READER=fixture``)
    Local JSON samples for demos and tests. Never a live source, and never
    selected automatically - if the real reader fails, the failure is reported.

There is no TikTok endpoint contacted directly anywhere in this package.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .base import CommentReaderBase, ReaderCapabilities, ReaderContract
from .contract_http_reader import ContractHttpCommentReader
from .fixture_reader import FixtureCommentReader
from .scrapecreators_reader import DOCS_URL, SIGNUP_URL, ScrapeCreatorsCommentReader

if TYPE_CHECKING:  # avoids importing validated settings at package import time
    from ..config import Settings

__all__ = [
    "CommentReaderBase",
    "ContractHttpCommentReader",
    "FixtureCommentReader",
    "ReaderCapabilities",
    "ReaderContract",
    "ScrapeCreatorsCommentReader",
    "build_reader",
    "reader_status",
]

#: Where the operator gets each adapter's credentials.
PROVIDER_LINKS = {
    "scrapecreators": {"docs": DOCS_URL, "signup": SIGNUP_URL},
}


def build_reader(settings: "Settings", http_client=None) -> CommentReaderBase:
    """Return exactly the adapter that was selected.

    There is no fallback: an unconfigured or failing live reader reports its
    blocker instead of quietly becoming the fixture reader.
    """
    if settings.COMMENT_READER == "scrapecreators":
        return ScrapeCreatorsCommentReader(settings, http_client)
    if settings.COMMENT_READER == "http":
        return ContractHttpCommentReader(settings, http_client)
    return FixtureCommentReader(settings)


def reader_status(settings: "Settings") -> dict[str, object]:
    """Structured description of the reader, for /health and the dashboard."""
    reader = build_reader(settings)
    caps = reader.capabilities
    return {
        "adapter": reader.name,
        "configured": reader.configured,
        "usable_for_dry_run": reader.usable_for_dry_run,
        "live_capable": reader.configured and caps.supplies_owner_username,
        "blocker": reader.blocker,
        "supports_replies": caps.supports_replies,
        "supplies_owner_username": caps.supplies_owner_username,
        "supports_owner_lookup": caps.supports_owner_lookup,
        "documented_ordering": caps.documented_ordering,
        "provider_limits": reader.provider_limits,
        "links": PROVIDER_LINKS.get(reader.name, {}),
    }
