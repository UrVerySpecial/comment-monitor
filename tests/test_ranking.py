from comment_monitor.ranking import PAST_TARGET, build_stats, rank_authors

NAMES = ("김가수", "밴드")


def row(author="UCa", *, target="김가수", severity=3, sentiment="부정", legal_type="모욕",
        review=None, video="v1", published="2026-11-01T00:00:00Z", name=None, missing=None,
        captured=0, failed=0):
    return {
        "platform": "youtube", "comment_id": "c", "video_id": video,
        "author_name": name or f"이름-{author}", "author_channel_id": author,
        "author_profile_url": f"http://example.com/{author}" if author else None,
        "published_at": published, "first_collected_at": "2026-11-05T00:00:00Z",
        "missing": missing, "sentiment": sentiment, "target": target if sentiment else None,
        "legal_type": legal_type if sentiment == "부정" else ("해당 없음" if sentiment else None),
        "severity": severity if sentiment == "부정" else (0 if sentiment else None),
        "captured": captured, "failed": failed, "review_status": review,
    }


def test_score_is_the_sum_of_severity_and_orders_the_ranking():
    rows = [
        row("UCa", severity=2), row("UCa", severity=2), row("UCa", severity=2),   # 6점, 최고 2
        row("UCb", severity=5, legal_type="협박"), row("UCb", severity=1, legal_type="단순 비판"),
        row("UCc", severity=4),
        row("UCd", sentiment="긍정"),
    ]
    ranked = rank_authors(rows, NAMES)
    assert [(a.rank, a.channel_id, a.score) for a in ranked] == [
        (1, "UCb", 6), (2, "UCa", 6), (3, "UCc", 4)]          # 동점이면 최고 심각도가 높은 쪽
    top = ranked[0]
    assert (top.count, top.max_severity, top.by_type) == (2, 5, {"협박": 1})


def test_other_people_and_excluded_reviews_do_not_count():
    rows = [
        row("UCa", severity=5, target="타인"),
        row("UCa", severity=3, review="제외"),
        row("UCa", severity=2, review="확인"),
        row("UCa", severity=1, review="보류"),
        row("UCb", severity=5, review="제외"),
    ]
    ranked = rank_authors(rows, NAMES)
    assert [(a.channel_id, a.score, a.count) for a in ranked] == [("UCa", 3, 2)]


def test_per_target_scores_add_up_and_pick_the_main_target():
    rows = [
        row("UCa", target="밴드", severity=4), row("UCa", target="김가수", severity=3),
        row("UCa", target="불명", severity=2), row("UCa", target="예전 이름", severity=1),
        row("UCb", target="밴드", severity=3), row("UCb", target="김가수", severity=3),
        row("UCc", target="불명", severity=1),
    ]
    a, b, c = rank_authors(rows, NAMES)
    assert a.by_target == {"밴드": 4, "김가수": 3, "불명": 2, PAST_TARGET: 1}
    assert sum(a.by_target.values()) == a.score == 10
    assert a.main_target == "밴드"
    assert b.main_target == "김가수"            # 동점이면 설정에서 먼저 나오는 대상
    assert c.main_target == "불명"


def test_authors_are_grouped_by_channel_id_and_show_the_latest_name():
    rows = [
        row("UCa", name="옛 이름", published="2026-01-01T00:00:00Z", video="v1", missing=1),
        row("UCa", name="새 이름", published="2026-03-01T00:00:00Z", video="v2"),
        row(None, name="이름만"), row(None, name="이름만"), row(None, name="다른 사람"),
    ]
    ranked = rank_authors(rows, NAMES)
    assert [(a.name, a.count) for a in ranked] == [("새 이름", 2), ("이름만", 2), ("다른 사람", 1)]
    first = ranked[0]
    assert (first.first_at, first.last_at) == ("2026-01-01T00:00:00Z", "2026-03-01T00:00:00Z")
    assert (len(first.videos), first.missing) == (2, 1)


def test_stats_count_by_video_and_target_regardless_of_review():
    videos = [
        {"video_id": "v1", "title": "영상 1", "channel_title": "채널", "comment_count": 10,
         "collect_status": "full"},
        {"video_id": "v2", "title": "영상 2", "channel_title": "채널", "comment_count": 50,
         "collect_status": "partial"},
        {"video_id": "v3", "title": "안 모은 영상", "channel_title": "채널", "comment_count": 5,
         "collect_status": "none"},
    ]
    rows = [
        row("UCa", video="v1", severity=3, review="제외", captured=1),
        row("UCa", video="v1", target="밴드", severity=5, legal_type="협박", missing=1),
        row("UCb", video="v1", target="타인", severity=2),
        row("UCb", video="v1", sentiment="긍정"),
        row("UCc", video="v2", severity=1, legal_type="단순 비판"),
        row("UCc", video="v2", sentiment=None, failed=1),
        row("UCc", video="v2", sentiment=None),
    ]
    stats = build_stats(rows, videos, NAMES)
    assert (stats.comments, stats.unclassified, stats.failed) == (7, 2, 1)
    assert stats.sentiments == {"부정": 4, "긍정": 1}

    v1, v2 = stats.videos                       # 한 건도 모으지 않은 영상은 빠진다
    assert v1.negative_by_target == {"김가수": 1, "밴드": 1, "타인": 1}
    assert v1.ratio("부정") == 0.75 and v1.representative
    assert (v2.collected, v2.unclassified, v2.classified) == (3, 2, 1) and not v2.representative

    by_name = {t.target: t for t in stats.targets}
    assert list(by_name) == ["김가수", "밴드", "타인", "불명"]     # 이전 대상 칸은 있을 때만
    singer = by_name["김가수"]
    assert (singer.negative, singer.by_severity, len(singer.authors)) == (2, {3: 1, 1: 1}, 2)
    assert singer.by_type == {"모욕": 1, "단순 비판": 1} and singer.captured == 1
    assert (by_name["밴드"].missing, by_name["밴드"].by_type) == (1, {"협박": 1})
    assert by_name["불명"].negative == 0

    stale = build_stats([row(target="예전 이름")], videos, NAMES)
    assert stale.targets[-1].target == PAST_TARGET and stale.targets[-1].negative == 1
