#!/usr/bin/env python3
"""Verify the live integrations with your own keys, without spending panel money.

WHAT IT DOES
    1. Prints the effective configuration (redacted).
    2. Resolves the video URL you pass in and shows the canonical URL and id.
    3. Reads the real comment section through the configured reader: pagination,
       replies, field normalization, owner handles, completeness.
    4. Runs the keyword match and the quantity formula, i.e. a Dry run result.
    5. Optionally reads God of Panel ``services`` and ``balance`` (read-only).

WHAT IT NEVER DOES
    It never sends ``action=add``, ``refill`` or ``cancel``. There is no code
    path in this script that can create or modify an order, so it cannot spend
    your panel balance.

WHAT IT COSTS
    Reader calls are real API calls and consume that provider's credits, even
    though they only read. One video with many comments and many reply threads
    can cost many credits. Use ``--max-pages`` to cap it.

USAGE
    export READER_API_KEY=...            # your reader key
    export COMMENT_READER=scrapecreators
    python scripts/check_integrations.py "https://www.tiktok.com/@someone/video/123..."

    # also read the panel (still read-only):
    export API_KEY=...
    python scripts/check_integrations.py --panel "https://vm.tiktok.com/XXXXX/"

Nothing secret is printed: keys are shown only as present/absent.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# A live check must never be able to place an order.
os.environ["RUN_MODE"] = "dry_run"


def _line(title: str) -> None:
    print("\n" + title)
    print("-" * len(title))


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("video_url", help="A TikTok video link (canonical or short).")
    parser.add_argument(
        "--panel",
        action="store_true",
        help="Also run the read-only God of Panel checks (services, balance).",
    )
    parser.add_argument("--scope", choices=["all", "top_level"], default=None)
    parser.add_argument(
        "--max-pages",
        type=int,
        default=None,
        help="Cap pages read, to limit reader credit spend during a first check.",
    )
    parser.add_argument(
        "--max-threads",
        type=int,
        default=None,
        help="Cap how many reply threads are expanded.",
    )
    args = parser.parse_args(argv)

    import httpx

    from app.comment_finder import ScanLimits, scan_video
    from app.config import get_settings
    from app.like_rules import calculate_quantity
    from app.models import CommentScope, ProviderError
    from app.providers import build_reader, reader_status
    from app.url_resolver import resolve_video

    settings = get_settings()
    scope = CommentScope(args.scope) if args.scope else settings.COMMENT_SCOPE

    _line("1. Configuration")
    print(json.dumps(settings.redacted(), indent=2, sort_keys=True))
    status = reader_status(settings)
    print(json.dumps(status, indent=2, sort_keys=True, default=str))
    if not status["configured"]:
        print("\nBLOCKED:", status["blocker"])
        print("Set the reader credentials and run this again.")
        return 2

    reader = build_reader(settings)
    async with httpx.AsyncClient(timeout=20.0, follow_redirects=False) as client:
        _line("2. URL resolution")
        try:
            identity = await resolve_video(client, args.video_url)
        except Exception as exc:  # noqa: BLE001
            print("FAILED:", exc)
            return 2
        print(f"input      : {identity.input_url}")
        print(f"video id   : {identity.video_id}")
        print(f"canonical  : {identity.canonical_url}")
        print(f"verified   : {identity.verified}  redirects: {identity.redirects}")

        _line("3. Comment read")
        limits = ScanLimits(
            deadline_seconds=settings.SCAN_DEADLINE_SECONDS,
            max_pages=args.max_pages or settings.SCAN_MAX_PAGES,
            max_requests=settings.SCAN_MAX_REQUESTS,
            max_comments=settings.SCAN_MAX_COMMENTS,
            max_threads_expanded=args.max_threads or settings.SCAN_MAX_THREADS,
            trust_provider_reply_end=settings.SCAN_TRUST_PROVIDER_REPLY_END,
        )
        try:
            scan = await scan_video(
                reader,
                video_id=identity.video_id,
                video_url=identity.canonical_url,
                keyword=settings.KEYWORD,
                requested_scope=scope,
                limits=limits,
            )
        except ProviderError as exc:
            print("READER FAILED:", exc)
            print("No order was attempted. Nothing was written to the database.")
            await reader.aclose()
            return 2
        finally:
            pass

        print(f"status          : {scan.status.value} (complete={scan.complete})")
        print(f"requested scope : {scan.requested_scope.value} -> actual {scan.actual_scope.value}")
        print(f"traversal       : {scan.traversal}")
        print(f"pages / requests: {scan.stats.pages_read} / {scan.stats.requests_made}")
        print(f"comments unique : {scan.stats.unique_comments} (dupes dropped {scan.stats.duplicates_dropped})")
        print(f"threads expanded: {scan.stats.threads_expanded}")
        print(f"owner lookups   : {scan.stats.owner_lookups}")
        print(f"invalid counts  : {scan.stats.invalid_like_counts}")
        print(f"duration        : {scan.duration_ms} ms")
        if scan.incomplete_reasons:
            print("incomplete because:", ", ".join(scan.incomplete_reasons))
        for note in scan.provider_notes[:10]:
            print("note:", note)
        print("provider limits / usage:")
        print(json.dumps(scan.provider_limits, indent=2, sort_keys=True, default=str))

        _line("4. Selection and quantity (Dry run)")
        if scan.target is None:
            print("no comment in scope contained the keyword", repr(settings.KEYWORD))
        else:
            target = scan.target
            print(f"comment id      : {target.comment_id}")
            print(f"parent comment  : {target.parent_comment_id}")
            print(f"owner username  : {target.comment_owner_username} "
                  f"(source: {target.owner_username_source})")
            print(f"owner user id   : {target.comment_owner_user_id}")
            print(f"target likes    : {target.like_count}")
            print(f"source order    : {target.source_order}")
            print(f"text            : {target.text[:160]!r}")
        print(f"observed maximum: {scan.observed_top_likes} "
              f"(comment {scan.observed_top_comment_id})")
        if scan.target_ambiguous:
            print("AMBIGUOUS: that account wrote several comments here:",
                  ", ".join(scan.owner_duplicate_comment_ids))
        if scan.observed_top_likes is None:
            print("quantity        : not computable, no trustworthy like count observed")
        else:
            quantity = calculate_quantity(scan.observed_top_likes)
            print(f"quantity        : {quantity} additional likes")
            if not scan.complete:
                print("NOTE: the scan is not complete, so a Live run would refuse to order.")
            if quantity == 0:
                print("NOTE: quantity 0, a Live run would skip this video.")

        await reader.aclose()

        if args.panel:
            _line("5. God of Panel (read-only)")
            if not settings.panel_configured:
                print("API_KEY is not set; skipping.")
            else:
                from app.smm_client import SmmClient

                smm = SmmClient(
                    client,
                    panel_url=settings.PANEL_URL,
                    api_key=settings.API_KEY,
                    service_id=settings.SERVICE_ID,
                    metadata_ttl=settings.SERVICE_METADATA_TTL_SECONDS,
                )
                try:
                    meta = await smm.get_service_metadata()
                    print(f"service {meta.service_id}: {meta.name}")
                    print(f"type {meta.service_type} | min {meta.min_quantity} | "
                          f"max {meta.max_quantity} | rate {meta.rate}")
                    print("accepts comment likes / username:",
                          smm.service_supports_comment_likes(meta))
                except Exception as exc:  # noqa: BLE001
                    print("services check failed:", exc)
                try:
                    amount, currency, raw = await smm.get_balance()
                    print(f"balance: {amount} {currency or ''}".strip())
                except Exception as exc:  # noqa: BLE001
                    print("balance check failed:", exc)
            print("\nNo order was created. This script cannot call action=add.")

    _line("Done")
    print("Reader calls above consumed that provider's credits. Panel balance untouched.")
    return 0 if scan.complete or scan.target is not None else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
