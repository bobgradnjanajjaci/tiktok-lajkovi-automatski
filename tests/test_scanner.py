import pytest

from app.comment_finder import ScanLimits, scan_video
from app.models import CommentScope, ScanStatus
from app.providers.fixture_reader import FixtureCommentReader

KEYWORD = "Mael Vorran"


def reader(data):
    return FixtureCommentReader(data=data)


def comment(cid, text, likes, *, owner="user", replies=0, uid=None):
    record = {
        "comment_id": cid,
        "text": text,
        "like_count": likes,
        "reply_count": replies,
        "owner_username": owner,
    }
    if uid:
        record["owner_user_id"] = uid
    return record


def video(pages, replies=None):
    return {
        "video_id": "7300000000000000001",
        "video_url": "https://www.tiktok.com/@creator/video/7300000000000000001",
        "pages": pages,
        "replies": replies or {},
    }


async def run(data, scope=CommentScope.ALL, limits=None):
    return await scan_video(
        reader(data),
        video_id="7300000000000000001",
        video_url="https://www.tiktok.com/@creator/video/7300000000000000001",
        keyword=KEYWORD,
        requested_scope=scope,
        limits=limits,
    )


@pytest.mark.asyncio
async def test_match_on_a_later_page_is_found():
    data = video(
        [
            {"has_more": True, "cursor_out": "c1", "comments": [comment("1", "nothing", 3)]},
            {
                "has_more": False,
                "cursor_out": None,
                "comments": [comment("2", "Mael Vorran!", 5)],
            },
        ]
    )
    result = await run(data)
    assert result.status is ScanStatus.COMPLETE
    assert result.target is not None
    assert result.target.comment_id == "2"


@pytest.mark.asyncio
async def test_maximum_after_the_match_still_wins():
    data = video(
        [
            {
                "has_more": True,
                "cursor_out": "c1",
                "comments": [comment("1", "Mael Vorran", 10)],
            },
            {
                "has_more": False,
                "cursor_out": None,
                "comments": [comment("2", "unrelated but huge", 950)],
            },
        ]
    )
    result = await run(data)
    assert result.target.comment_id == "1"
    assert result.observed_top_likes == 950
    assert result.observed_top_comment_id == "2"


@pytest.mark.asyncio
async def test_first_match_is_selected_not_the_most_liked_match():
    data = video(
        [
            {
                "has_more": False,
                "cursor_out": None,
                "comments": [
                    comment("1", "early Mael Vorran mention", 2),
                    comment("2", "much more popular Mael Vorran mention", 900),
                ],
            }
        ]
    )
    result = await run(data)
    assert result.target.comment_id == "1"
    assert result.target.like_count == 2
    assert result.observed_top_likes == 900


@pytest.mark.asyncio
async def test_reply_can_be_the_maximum_when_scope_is_all():
    data = video(
        [
            {
                "has_more": False,
                "cursor_out": None,
                "comments": [comment("1", "Mael Vorran", 10, replies=1)],
            }
        ],
        replies={
            "1": [
                {
                    "has_more": False,
                    "cursor_out": None,
                    "comments": [comment("1-1", "a very liked reply", 4000)],
                }
            ]
        },
    )
    result = await run(data, scope=CommentScope.ALL)
    assert result.status is ScanStatus.COMPLETE
    assert result.observed_top_likes == 4000
    assert result.actual_scope is CommentScope.ALL

    top_only = await run(data, scope=CommentScope.TOP_LEVEL)
    assert top_only.observed_top_likes == 10


@pytest.mark.asyncio
async def test_traversal_is_parent_then_its_replies_then_next_parent():
    data = video(
        [
            {
                "has_more": False,
                "cursor_out": None,
                "comments": [
                    comment("p1", "parent one", 1, replies=1),
                    comment("p2", "parent two", 2, replies=1),
                ],
            }
        ],
        replies={
            "p1": [{"has_more": False, "cursor_out": None, "comments": [comment("r1", "Mael Vorran in a reply", 1)]}],
            "p2": [{"has_more": False, "cursor_out": None, "comments": [comment("r2", "Mael Vorran later", 1)]}],
        },
    )
    result = await run(data)
    assert result.target.comment_id == "r1"
    assert result.target.source_order == 2  # p1, r1, p2, r2
    assert result.traversal == "parent_source_order_then_its_replies_source_order"


@pytest.mark.asyncio
async def test_short_page_with_has_more_is_not_the_last_page():
    data = video(
        [{"has_more": True, "cursor_out": "c1", "comments": [comment("1", "Mael Vorran", 9)]}]
    )
    result = await run(data)
    assert result.status is ScanStatus.INCOMPLETE
    assert "stream_ended_with_has_more:top_level" in result.incomplete_reasons
    assert result.observed_top_likes == 9  # observed maximum is preserved


@pytest.mark.asyncio
async def test_repeated_cursor_is_detected():
    page = {"has_more": True, "cursor_out": "same", "comments": [comment("1", "x", 1)]}
    data = video([dict(page), dict(page, comments=[comment("2", "y", 2)])])
    result = await run(data)
    assert result.status is ScanStatus.INCOMPLETE
    assert any(reason.startswith("repeated_cursor") for reason in result.incomplete_reasons)


@pytest.mark.asyncio
async def test_has_more_without_a_cursor_is_incomplete():
    data = video([{"has_more": True, "cursor_out": None, "comments": [comment("1", "x", 1)]}])
    result = await run(data)
    assert result.status is ScanStatus.INCOMPLETE
    assert "missing_cursor_with_has_more:top_level" in result.incomplete_reasons


@pytest.mark.asyncio
async def test_missing_like_count_makes_the_scan_incomplete():
    data = video(
        [
            {
                "has_more": False,
                "cursor_out": None,
                "comments": [
                    comment("1", "Mael Vorran", 5),
                    {"comment_id": "2", "text": "broken", "like_count": None, "owner_username": "u"},
                ],
            }
        ]
    )
    result = await run(data)
    assert result.status is ScanStatus.INCOMPLETE
    assert result.stats.invalid_like_counts >= 1
    assert result.observed_top_likes == 5  # the untrusted record never sets the max


def short_thread():
    """A parent advertising five replies where only one comes back."""
    return video(
        [
            {
                "has_more": False,
                "cursor_out": None,
                "comments": [comment("p1", "parent", 1, replies=5)],
            }
        ],
        replies={
            "p1": [
                {
                    "has_more": False,
                    "cursor_out": None,
                    "comments": [comment("r1", "one of five", 1)],
                }
            ]
        },
    )


@pytest.mark.asyncio
async def test_truncated_replies_are_incomplete_by_default():
    """Strict by default: an end marker does not settle a reply-count gap."""
    result = await run(short_thread())
    assert result.status is ScanStatus.INCOMPLETE
    assert any(reason.startswith("truncated_replies") for reason in result.incomplete_reasons)
    assert result.scope_limitations == []


@pytest.mark.asyncio
async def test_default_scan_limits_are_strict_about_reply_counts():
    assert ScanLimits().trust_provider_reply_end is False


@pytest.mark.asyncio
async def test_explicit_opt_in_accepts_the_shortfall_but_labels_the_result():
    result = await run(
        short_thread(), limits=ScanLimits(trust_provider_reply_end=True)
    )
    assert result.status is ScanStatus.COMPLETE
    # The narrowed meaning of "complete" is a first-class field, not a log line.
    assert any("reply_count_mismatch" in entry for entry in result.scope_limitations)
    assert "1 of 5" in result.scope_limitations[0]
    assert result.as_json()["completeness_basis"].startswith("provider-visible")


@pytest.mark.asyncio
async def test_opt_in_cannot_excuse_a_stream_that_still_reports_more():
    data = video(
        [
            {
                "has_more": False,
                "cursor_out": None,
                "comments": [comment("p1", "parent", 1, replies=2)],
            }
        ],
        replies={
            "p1": [
                {
                    "has_more": True,
                    "cursor_out": None,
                    "comments": [comment("r1", "one", 1)],
                }
            ]
        },
    )
    result = await run(data, limits=ScanLimits(trust_provider_reply_end=True))
    assert result.status is ScanStatus.INCOMPLETE
    assert result.scope_limitations == []


@pytest.mark.asyncio
async def test_opt_in_cannot_excuse_an_exceeded_budget():
    pages = [
        {"has_more": True, "cursor_out": f"c{i}", "comments": [comment(str(i), "x", i)]}
        for i in range(1, 6)
    ]
    result = await run(
        video(pages),
        limits=ScanLimits(max_pages=2, trust_provider_reply_end=True),
    )
    assert result.status is ScanStatus.INCOMPLETE


@pytest.mark.asyncio
async def test_opt_in_cannot_excuse_invalid_records():
    """Only the invalid like count is wrong here - nothing else.

    The earlier version of this fixture omitted reply_count on the malformed
    record, so the scanner also opened a reply stream the fixture never
    supplied and the result failed on ``no_pages_returned`` instead. The record
    now carries an explicit reply_count of 0, isolating the condition.
    """
    data = video(
        [
            {
                "has_more": False,
                "cursor_out": None,
                "comments": [
                    comment("1", "Mael Vorran", 5, uid="u1"),
                    {
                        "comment_id": "2",
                        "text": "readable text, unreadable count",
                        "like_count": None,
                        "reply_count": 0,
                        "owner_username": "other",
                        "owner_user_id": "u2",
                    },
                ],
            }
        ]
    )
    result = await run(data, limits=ScanLimits(trust_provider_reply_end=True))
    assert result.status is ScanStatus.INCOMPLETE
    assert "invalid_like_counts_present" in result.incomplete_reasons
    assert not any("no_pages_returned" in r for r in result.incomplete_reasons)
    # The target was still selected, but the scan may not be ordered from.
    assert result.target is not None
    assert result.complete is False


@pytest.mark.asyncio
async def test_all_known_invalidity_reasons_are_reported_together():
    """A later reason must not hide behind the first incomplete condition."""
    data = video(
        [
            {
                "has_more": True,  # stream ends while more is reported
                "cursor_out": "c1",
                "comments": [
                    {
                        "comment_id": "1",
                        "like_count": None,      # invalid count
                        "reply_count": 0,
                        "owner_username": "a",
                        "owner_user_id": "u1",
                    },                            # and no text field at all
                ],
            }
        ]
    )
    result = await run(data)
    assert {
        "invalid_like_counts_present",
        "unreadable_comment_text",
    } <= set(result.incomplete_reasons)
    assert any("stream_ended_with_has_more" in r for r in result.incomplete_reasons)


@pytest.mark.asyncio
async def test_ordering_is_unchanged_by_the_opt_in():
    """Relaxing reply-count strictness must not change which comment wins."""
    data = video(
        [
            {
                "has_more": False,
                "cursor_out": None,
                "comments": [
                    comment("p1", "plain parent", 2, replies=3, uid="u1"),
                    comment("p2", "another parent", 4, replies=0, uid="u2"),
                ],
            }
        ],
        replies={
            "p1": [
                {
                    "has_more": False,
                    "cursor_out": None,
                    "comments": [
                        comment("r1", "Mael Vorran in a reply", 7, owner="three", uid="u3")
                    ],
                }
            ]
        },
    )
    lenient = await run(data, limits=ScanLimits(trust_provider_reply_end=True))
    assert lenient.target.comment_id == "r1"
    assert lenient.target.source_order == 2  # parent, then its reply, then p2
    assert lenient.observed_top_likes == 7
    assert lenient.status is ScanStatus.COMPLETE

    strict = await run(data)
    assert strict.status is ScanStatus.INCOMPLETE
    assert strict.target.comment_id == "r1"  # same selection, blocked outcome


@pytest.mark.asyncio
async def test_ten_thousand_threshold_exits_early_without_a_target():
    data = video(
        [
            {
                "has_more": True,
                "cursor_out": "c1",
                "comments": [comment("1", "no keyword at all", 10000)],
            },
            {
                "has_more": False,
                "cursor_out": None,
                "comments": [comment("2", "Mael Vorran", 1)],
            },
        ]
    )
    result = await run(data)
    assert result.status is ScanStatus.EARLY_EXIT_THRESHOLD
    assert result.target is None
    assert result.threshold_trigger_comment_id == "1"
    assert result.complete is False  # the scan is NOT claimed to be complete
    assert result.stats.pages_read == 1  # stopped immediately


@pytest.mark.asyncio
async def test_keyword_not_found_only_reported_on_a_complete_scan():
    complete = await run(
        video([{"has_more": False, "cursor_out": None, "comments": [comment("1", "nope", 1)]}])
    )
    assert complete.status is ScanStatus.COMPLETE
    assert complete.target is None

    partial = await run(
        video([{"has_more": True, "cursor_out": "c1", "comments": [comment("1", "nope", 1)]}])
    )
    assert partial.status is ScanStatus.INCOMPLETE
    assert partial.target is None


@pytest.mark.asyncio
async def test_duplicate_comment_ids_are_dropped_and_order_preserved():
    data = video(
        [
            {
                "has_more": True,
                "cursor_out": "c1",
                "comments": [comment("1", "Mael Vorran first", 3)],
            },
            {
                "has_more": False,
                "cursor_out": None,
                "comments": [comment("1", "Mael Vorran first", 3), comment("2", "later", 4)],
            },
        ]
    )
    result = await run(data)
    assert result.stats.duplicates_dropped == 1
    assert result.stats.unique_comments == 2
    assert result.target.comment_id == "1"


@pytest.mark.asyncio
async def test_page_budget_produces_incomplete():
    pages = [
        {"has_more": True, "cursor_out": f"c{i}", "comments": [comment(str(i), "x", i)]}
        for i in range(1, 6)
    ]
    pages.append({"has_more": False, "cursor_out": None, "comments": []})
    result = await run(video(pages), limits=ScanLimits(max_pages=2))
    assert result.status is ScanStatus.INCOMPLETE
    assert any("max_pages_reached" in reason for reason in result.incomplete_reasons)


@pytest.mark.asyncio
async def test_same_owner_multiple_matches_flags_ambiguity():
    data = video(
        [
            {
                "has_more": False,
                "cursor_out": None,
                "comments": [
                    comment("1", "Mael Vorran once", 2, owner="double"),
                    comment("2", "Mael Vorran twice", 3, owner="double"),
                ],
            }
        ]
    )
    result = await run(data)
    assert result.target_ambiguous is True
    assert result.owner_duplicate_comment_ids == ["1", "2"]


@pytest.mark.asyncio
async def test_different_owners_matching_is_not_ambiguous():
    data = video(
        [
            {
                "has_more": False,
                "cursor_out": None,
                "comments": [
                    comment("1", "Mael Vorran once", 2, owner="one"),
                    comment("2", "Mael Vorran twice", 3, owner="two"),
                ],
            }
        ]
    )
    result = await run(data)
    assert result.target_ambiguous is False
    assert result.target.comment_id == "1"


@pytest.mark.asyncio
async def test_presentation_at_is_stripped_but_preserved_in_diagnostics():
    data = video(
        [{"has_more": False, "cursor_out": None, "comments": [comment("1", "Mael Vorran", 1, owner="@handle.x")]}]
    )
    result = await run(data)
    assert result.target.comment_owner_username == "handle.x"
    assert result.target.raw_owner_username == "@handle.x"


@pytest.mark.asyncio
async def test_username_present_in_comment_data_triggers_no_lookups():
    data = video(
        [{"has_more": False, "cursor_out": None, "comments": [comment("1", "Mael Vorran", 1, owner="known")]}]
    )
    result = await run(data)
    assert result.stats.owner_lookups == 0
    assert result.stats.profile_navigations == 0
    assert result.target.owner_username_source == "comment_record"


@pytest.mark.asyncio
async def test_an_iterator_that_yields_no_pages_is_not_a_completed_empty_scan():
    result = await run(video([]))
    assert result.status is ScanStatus.INCOMPLETE
    assert any("no_pages_returned" in reason for reason in result.incomplete_reasons)
    assert result.target is None
    assert result.observed_top_likes is None


@pytest.mark.asyncio
async def test_a_page_with_an_unknown_completion_marker_is_incomplete():
    from app.models import CommentPage, NormalizedComment, utcnow

    class UnknownMarkerReader:
        name = "unknown-marker"
        supports_replies = True
        provider_limits: dict = {}
        call_stats: dict = {}

        async def iter_top_level_pages(self, video_id, video_url):
            yield CommentPage(
                comments=[
                    NormalizedComment(
                        video_id=video_id,
                        video_url=video_url,
                        comment_id="1",
                        parent_comment_id=None,
                        text="Mael Vorran",
                        like_count=4,
                        reply_count=0,
                        comment_owner_username="someone",
                        comment_owner_user_id="u1",
                        source_order=0,
                        fetched_at=utcnow(),
                    )
                ],
                cursor_in=None,
                cursor_out="next",
                has_more=False,
                completion_unknown=True,
            )

        async def iter_reply_pages(self, video_id, video_url, parent_comment_id):
            return
            yield  # pragma: no cover

    result = await scan_video(
        UnknownMarkerReader(),
        video_id="7300000000000000001",
        video_url="https://www.tiktok.com/@creator/video/7300000000000000001",
        keyword=KEYWORD,
        requested_scope=CommentScope.ALL,
    )
    assert result.status is ScanStatus.INCOMPLETE
    assert any("unknown_completion_marker" in r for r in result.incomplete_reasons)


@pytest.mark.asyncio
async def test_same_owner_without_the_keyword_makes_the_target_ambiguous():
    """The panel gets video + username, so any second comment by that owner counts."""
    data = video(
        [
            {
                "has_more": False,
                "cursor_out": None,
                "comments": [
                    comment("1", "Mael Vorran mentioned", 2, owner="fan", uid="u9"),
                    comment("2", "no keyword here at all", 6, owner="fan", uid="u9"),
                ],
            }
        ]
    )
    result = await run(data)
    assert result.target.comment_id == "1"
    assert result.target_ambiguous is True
    assert result.owner_duplicate_comment_ids == ["1", "2"]


@pytest.mark.asyncio
async def test_an_earlier_comment_by_the_same_owner_also_counts():
    data = video(
        [
            {
                "has_more": False,
                "cursor_out": None,
                "comments": [
                    comment("1", "warming up", 2, owner="fan", uid="u9"),
                    comment("2", "Mael Vorran mentioned", 3, owner="fan", uid="u9"),
                ],
            }
        ]
    )
    result = await run(data)
    assert result.target.comment_id == "2"
    assert result.target_ambiguous is True


@pytest.mark.asyncio
async def test_conflicting_owner_ids_behind_one_handle_are_reported():
    """The panel would target both by the same username, so this is unsettled.

    Preferring the stable id and silently declaring "different accounts" was
    wrong: the submitted payload carries the handle, not the id.
    """
    data = video(
        [
            {
                "has_more": False,
                "cursor_out": None,
                "comments": [
                    comment("1", "Mael Vorran", 2, owner="same.looking", uid="u1"),
                    comment("2", "unrelated", 9, owner="same.looking", uid="u2"),
                ],
            }
        ]
    )
    result = await run(data)
    assert result.target_ambiguous is True
    assert "owner_identity_conflict" in result.incomplete_reasons
    assert result.complete is False


@pytest.mark.asyncio
async def test_a_handle_only_record_reconciles_with_a_uid_record():
    """Mixed identifier availability is one account, not two."""
    data = video(
        [
            {
                "has_more": False,
                "cursor_out": None,
                "comments": [
                    comment("1", "Mael Vorran", 2, owner="same.owner", uid="101"),
                    comment("2", "ordinary text", 5, owner="same.owner"),
                ],
            }
        ]
    )
    result = await run(data)
    assert result.target_ambiguous is True
    assert result.owner_duplicate_comment_ids == ["1", "2"]


@pytest.mark.asyncio
async def test_reconciliation_works_for_a_record_before_the_target():
    data = video(
        [
            {
                "has_more": False,
                "cursor_out": None,
                "comments": [
                    comment("1", "ordinary text", 5, owner="same.owner"),
                    comment("2", "Mael Vorran", 2, owner="same.owner", uid="101"),
                ],
            }
        ]
    )
    result = await run(data)
    assert result.target.comment_id == "2"
    assert result.target_ambiguous is True


@pytest.mark.asyncio
async def test_handle_case_differences_reconcile():
    data = video(
        [
            {
                "has_more": False,
                "cursor_out": None,
                "comments": [
                    comment("1", "Mael Vorran", 2, owner="Same.Owner", uid="101"),
                    comment("2", "ordinary text", 5, owner="same.owner"),
                ],
            }
        ]
    )
    result = await run(data)
    assert result.target_ambiguous is True


@pytest.mark.asyncio
async def test_a_renamed_handle_under_one_owner_id_is_still_one_account():
    data = video(
        [
            {
                "has_more": False,
                "cursor_out": None,
                "comments": [
                    comment("1", "Mael Vorran", 2, owner="old.handle", uid="101"),
                    comment("2", "ordinary text", 5, owner="new.handle", uid="101"),
                ],
            }
        ]
    )
    result = await run(data)
    assert result.target_ambiguous is True
    assert any("more than one handle" in note for note in result.provider_notes)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "broken",
    [
        {"comment_id": "1", "like_count": 5, "reply_count": 0, "owner_username": "a",
         "owner_user_id": "u1"},                                            # absent
        {"comment_id": "1", "text": None, "like_count": 5, "reply_count": 0,
         "owner_username": "a", "owner_user_id": "u1"},                     # null
        {"comment_id": "1", "text": 17, "like_count": 5, "reply_count": 0,
         "owner_username": "a", "owner_user_id": "u1"},                     # wrong type
    ],
)
async def test_unreadable_text_before_a_match_blocks_the_selection(broken):
    """An unknown text could itself have been the first match."""
    data = video(
        [
            {
                "has_more": False,
                "cursor_out": None,
                "comments": [broken, comment("2", "Mael Vorran", 3, owner="b", uid="u2")],
            }
        ]
    )
    result = await run(data)
    assert result.status is ScanStatus.INCOMPLETE
    assert "unreadable_comment_text" in result.incomplete_reasons
    assert result.complete is False
    assert result.stats.unreadable_texts == 1
    # Diagnostics keep the context rather than dropping the record.
    assert any("1" in note for note in result.provider_notes)


@pytest.mark.asyncio
async def test_an_explicitly_empty_text_is_valid():
    data = video(
        [
            {
                "has_more": False,
                "cursor_out": None,
                "comments": [
                    comment("1", "", 5, owner="a", uid="u1"),
                    comment("2", "Mael Vorran", 3, owner="b", uid="u2"),
                ],
            }
        ]
    )
    result = await run(data)
    assert result.status is ScanStatus.COMPLETE
    assert result.target.comment_id == "2"
    assert result.stats.unreadable_texts == 0


@pytest.mark.asyncio
async def test_a_repeated_comment_id_is_not_a_second_comment_by_the_owner():
    data = video(
        [
            {
                "has_more": True,
                "cursor_out": "c1",
                "comments": [comment("1", "Mael Vorran", 2, owner="fan", uid="u9")],
            },
            {
                "has_more": False,
                "cursor_out": None,
                "comments": [comment("1", "Mael Vorran", 2, owner="fan", uid="u9")],
            },
        ]
    )
    result = await run(data)
    assert result.stats.duplicates_dropped == 1
    assert result.target_ambiguous is False


@pytest.mark.asyncio
async def test_a_target_that_is_already_the_leader_is_still_processed():
    """No automatic skip: the formula decides, not a ranking heuristic."""
    from app.like_rules import calculate_quantity

    data = video(
        [
            {
                "has_more": False,
                "cursor_out": None,
                "comments": [
                    comment("1", "Mael Vorran", 500, owner="fan", uid="u9"),
                    comment("2", "quieter", 10, owner="other", uid="u8"),
                ],
            }
        ]
    )
    result = await run(data)
    assert result.status is ScanStatus.COMPLETE
    assert result.target.comment_id == "1"
    assert result.observed_top_likes == 500  # the target itself is included
    assert calculate_quantity(result.observed_top_likes) == 650
