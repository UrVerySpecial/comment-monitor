import pytest

from comment_monitor import db as dbm
from comment_monitor.collectors.base import MODE_BACKFILL, MODE_FULL, MODE_NEW
from comment_monitor.config import apply_overrides
from comment_monitor.planner import build_plan

from .helpers import V1, V2, V3, collect, make_client

SEARCH = f"""
youtube:
  videos: ["{V1}"]
  search:
    enabled: true
    default_top_n: 3
    keywords:
      - "홍길동"
      - {{ q: "홍길동 논란", top_n: 2, order: viewCount }}
"""


@pytest.fixture
def searchable(fake):
    ids = [f"srch{index:07d}" for index in range(8)]
    for video_id in (V1, *ids):
        fake.add_video(video_id, comments=1)
    fake.searches["홍길동"] = [V1, *ids[:4]]
    fake.searches["홍길동 논란"] = ids[4:]
    return ids


def plan_for(config, db, fake, **options):
    return build_plan(config, db, make_client(config, db, fake), **options)


def ids_of(videos):
    return [video.info.video_id for video in videos]


def test_each_keyword_uses_its_own_top_n_and_order(cfg, store, fake, searchable):
    plan = plan_for(cfg(SEARCH), store, fake)
    calls = fake.called("search")
    assert [(c["q"], c["maxResults"], c["order"]) for c in calls] == [
        ("홍길동", 3, "relevance"), ("홍길동 논란", 2, "viewCount"),
    ]
    assert set(ids_of(plan.videos)) == {V1, *searchable[:2], *searchable[4:6]}
    by_id = {video.info.video_id: video for video in plan.videos}
    # 직접 지정한 영상은 검색에도 나오지만 경로는 지정으로 남고 맨 앞에 온다.
    assert plan.videos[0].info.video_id == V1 and by_id[V1].source == dbm.SOURCE_DIRECT
    assert (by_id[searchable[4]].source, by_id[searchable[4]].source_detail) == (
        dbm.SOURCE_SEARCH, "홍길동 논란",
    )
    assert plan.searches_run == 2


def test_search_top_option_beats_keyword_setting(cfg, store, fake, searchable):
    config = apply_overrides(cfg(SEARCH), search_top=1)
    plan_for(config, store, fake)
    assert [c["maxResults"] for c in fake.called("search")] == [1, 1]


def test_search_results_are_cached(cfg, store, fake, searchable):
    config = cfg(SEARCH)
    first = plan_for(config, store, fake, now="2026-11-01T00:00:00Z")
    second = plan_for(config, store, fake, now="2026-11-05T00:00:00Z")
    assert (second.searches_run, second.searches_cached) == (0, 2)
    assert len(fake.called("search")) == 2
    assert ids_of(second.videos) == ids_of(first.videos)

    # 더 많이 달라고 하면 저장된 결과로는 모자라므로 다시 검색한다.
    wider = plan_for(apply_overrides(config, search_top=4), store, fake,
                     now="2026-11-05T00:00:00Z")
    assert wider.searches_run == 2
    # 줄여 달라고 하면 저장된 결과의 앞부분을 쓴다.
    narrower = plan_for(apply_overrides(config, search_top=1), store, fake,
                        now="2026-11-05T00:00:00Z")
    assert (narrower.searches_run, narrower.searches_cached) == (0, 2)
    assert len(narrower.videos) == 2                # V1(지정이자 검색 1위)과 논란 검색 1위

    forced = plan_for(config, store, fake, refresh_search=True, now="2026-11-05T00:00:00Z")
    assert forced.searches_run == 2
    expired = plan_for(config, store, fake, now="2026-11-20T00:00:00Z")
    assert expired.searches_run == 2


def test_excluded_and_unavailable_videos_are_left_out(cfg, store, fake, searchable):
    gone = searchable[1]
    del fake.videos[gone]
    config = cfg(SEARCH + f'  exclude_videos: ["{searchable[0]}"]\n')
    plan = plan_for(config, store, fake)
    assert plan.excluded == [searchable[0]] and plan.unavailable == [gone]
    assert not {searchable[0], gone} & set(ids_of(plan.videos))


def test_channel_videos_come_after_direct_ones(cfg, store, fake):
    for video_id in (V1, V2, V3):
        fake.add_video(video_id, comments=1)
    channel, unknown = "UC" + "a" * 22, "UC" + "b" * 22
    fake.channels[channel] = [V3, V2, V1]
    config = cfg(f"""
    youtube:
      videos: ["{V1}"]
      channels: ["{channel}", "{unknown}"]
      channel_max_videos: 2
    """)
    plan = plan_for(config, store, fake)
    assert [(v.info.video_id, v.source) for v in plan.videos] == [
        (V1, dbm.SOURCE_DIRECT), (V3, dbm.SOURCE_CHANNEL), (V2, dbm.SOURCE_CHANNEL),
    ]
    assert any(unknown in note for note in plan.notes)


@pytest.mark.parametrize(
    "state, count, options, mode, label",
    [
        (None, 100, {}, MODE_FULL, "첫 수집"),
        (None, 6000, {}, MODE_BACKFILL, "첫 수집"),
        (None, 30000, {}, MODE_BACKFILL, "부분 수집"),
        (None, 100, {"quick": True}, MODE_BACKFILL, "첫 수집"),
        ("full", 100, {}, MODE_FULL, "전체 재확인"),
        ("full", 6000, {}, MODE_NEW, "새 댓글만"),
        ("resumable", 6000, {}, MODE_BACKFILL, "이어하기"),
        ("full", 100, {"quick": True}, MODE_NEW, "새 댓글만"),
        ("full", 6000, {"full": True}, MODE_FULL, "전체 재확인"),
    ],
)
def test_rescan_mode_follows_video_size(cfg, store, fake, state, count, options, mode, label):
    fake.add_video(V1)
    fake.counts[V1] = count
    if state:
        store.conn.execute(
            "INSERT INTO videos (platform, video_id, source, first_seen_at, collect_status, "
            "resume_token, comment_count, last_collected_at) "
            "VALUES ('youtube', ?, 'direct', '2026-11-01T00:00:00Z', ?, ?, ?, "
            "'2026-11-01T00:00:00Z')",
            (V1, "full" if state == "full" else "partial",
             None if state == "full" else "tok", count - 30),
        )
    video = plan_for(cfg(f'youtube:\n  videos: ["{V1}"]\n'), store, fake, **options).videos[0]
    assert (video.mode, video.label) == (mode, label)
    if count == 30000:
        assert video.over_cap == 10000
    if mode == MODE_NEW:
        assert (video.est_new_comments, video.est_units) == (30, 2)


def estimate_for(cfg, store, fake, count, body="", **options):
    fake.add_video(V1)
    fake.counts[V1] = count
    config = cfg(f'youtube:\n  videos: ["{V1}"]\n' + body)
    return plan_for(config, store, fake, **options).estimate


def test_estimate_numbers(cfg, store, fake):
    estimate = estimate_for(cfg, store, fake, 250)
    assert estimate.units == 5                       # 3쪽 × 답글 호출 여유 1.5
    assert estimate.new_comments == estimate.classify_count == 250
    assert estimate.classify_cost_krw == 1000 and not estimate.cost_needs_confirm
    assert (estimate.capture_count, estimate.capture_seconds) == (12, 144)
    assert estimate.capture_rate_assumed
    assert (estimate.quota_used, estimate.quota_left) == (1, 7999)
    assert not estimate.over_budget


def test_estimate_flags_cost_over_the_confirm_threshold(cfg, store, fake):
    estimate = estimate_for(cfg, store, fake, 3000)
    assert estimate.classify_cost_krw == 12000 and estimate.cost_needs_confirm


def test_estimate_applies_limits_and_reports_leftovers(cfg, store, fake):
    body = "limits:\n  max_classify_per_run: 200\n  max_captures_per_run: 4\n"
    estimate = estimate_for(cfg, store, fake, 250, body)
    assert (estimate.classify_count, estimate.classify_left) == (200, 50)
    assert (estimate.capture_count, estimate.capture_left) == (4, 6)


def test_limit_option_caps_classify_and_capture(cfg, store, fake):
    estimate = estimate_for(cfg, store, fake, 250, limit=100)
    assert (estimate.classify_count, estimate.classify_left) == (100, 150)
    assert estimate.classify_cost_krw == 400 and estimate.capture_count == 5


def test_two_pass_cost_and_quota_over_budget(cfg, store, fake):
    body = "  daily_quota_budget: 3\n"
    estimate = estimate_for(cfg, store, fake, 250, body, two_pass=True)
    assert estimate.classify_cost_krw == 125         # 250 × (0.1 + 4 × 10%)
    assert estimate.over_budget


def test_estimate_counts_unclassified_comments_already_stored(cfg, store, fake):
    fake.add_video(V1, comments=4)
    config = cfg(f'youtube:\n  videos: ["{V1}"]\n')
    collect(config, store, fake)
    fake.add_comment(V1, "new1", "새 댓글")
    estimate = plan_for(config, store, fake).estimate
    assert (estimate.new_comments, estimate.classify_count) == (1, 5)
