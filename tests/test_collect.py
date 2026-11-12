from comment_monitor import db as dbm
from comment_monitor.collectors.base import (
    CAPPED,
    DONE,
    MODE_BACKFILL,
    MODE_FULL,
    MODE_NEW,
    SKIPPED,
    STOPPED,
    run_collection,
)
from comment_monitor.collectors.youtube import YouTubeCollector
from comment_monitor.planner import build_plan, register_plan

from .helpers import V1, V2, V3, collect, make_client, mark_negative


def one_video(limits: str = "", budget: int = 8000) -> str:
    return f"""
    youtube:
      videos: ["{V1}"]
      daily_quota_budget: {budget}
    limits:
      max_videos_per_run: 50
    {limits}
    """


def texts(db):
    return dict(db.conn.execute("SELECT comment_id, text FROM comments"))


def missing_now(db):
    return {
        row[0] for row in db.conn.execute(
            "SELECT comment_id FROM comments WHERE missing_detected_at >= last_seen_at"
        )
    }


def test_rerun_does_not_duplicate_and_keeps_evidence_numbers(cfg, store, fake):
    fake.add_video(V1, comments=5)
    config = cfg(one_video())
    _, first = collect(config, store, fake, page_size=2)
    numbers = dict(store.conn.execute("SELECT comment_id, evidence_no FROM comments"))
    _, second = collect(config, store, fake, page_size=2)

    assert (first.new, second.new, second.edited, second.missing) == (5, 0, 0, 0)
    assert dict(store.conn.execute("SELECT comment_id, evidence_no FROM comments")) == numbers
    video = store.get_video("youtube", V1)
    assert (video["collect_status"], video["resume_token"]) == (dbm.COLLECT_FULL, None)
    assert video["comment_count"] == 5 and video["last_collected_at"]


def test_edited_comment_keeps_old_text_in_history(cfg, store, fake):
    fake.add_video(V1, comments=3)
    config = cfg(one_video())
    collect(config, store, fake)
    fake.edit(f"{V1}-c1", "고친 댓글")
    _, summary = collect(config, store, fake)

    assert (summary.new, summary.edited) == (0, 1)
    assert texts(store)[f"{V1}-c1"] == "고친 댓글"
    revision = store.conn.execute("SELECT old_text, new_text FROM comment_revisions").fetchone()
    assert tuple(revision) == ("댓글 1", "고친 댓글")
    assert store.scalar(
        "SELECT reclassify_needed FROM comments WHERE comment_id = ?", (f"{V1}-c1",)
    ) == 1


def test_replies_beyond_inline_ones_are_fetched(cfg, store, fake):
    fake.add_video(V1)
    fake.add_comment(V1, "many", "답글 많은 댓글", replies=8)
    fake.add_comment(V1, "few", "답글 적은 댓글", replies=3)
    _, summary = collect(cfg(one_video()), store, fake)

    assert summary.new == 13
    assert [call["parentId"] for call in fake.called("comments")] == ["many"]
    parents = dict(store.conn.execute("SELECT comment_id, parent_id FROM comments"))
    assert parents["many.r7"] == "many" and parents["few.r2"] == "few" and parents["many"] is None


def test_missing_is_marked_only_after_a_complete_rescan(cfg, store, fake):
    fake.add_video(V1, comments=6)
    collect(cfg(one_video()), store, fake, page_size=2)        # 4 units
    fake.remove(f"{V1}-c0")                                     # 가장 오래된 댓글, 마지막 쪽

    # 영상 조회 1 + 댓글 두 쪽까지만 되는 예산. 훑다가 끊긴다.
    _, stopped = collect(cfg(one_video(budget=7)), store, fake, page_size=2)
    assert stopped.results[0].outcome == STOPPED and stopped.stopped_reason
    assert stopped.missing == 0 and missing_now(store) == set()

    _, done = collect(cfg(one_video()), store, fake, page_size=2)
    assert done.results[0].outcome == DONE and done.missing == 1
    assert missing_now(store) == {f"{V1}-c0"}
    assert texts(store)[f"{V1}-c0"] == "댓글 0"                 # 수집한 내용은 그대로 남는다

    # 이미 표시한 댓글을 다음 실행에서 또 세지 않는다.
    _, again = collect(cfg(one_video()), store, fake, page_size=2)
    assert again.missing == 0 and missing_now(store) == {f"{V1}-c0"}


def test_quota_stop_saves_position_and_next_run_resumes(cfg, store, fake):
    fake.add_video(V1, comments=10)
    big = "  full_rescan_max_comments: 3"
    plan, stopped = collect(cfg(one_video(big, budget=3)), store, fake, page_size=2)
    assert plan.videos[0].mode == MODE_BACKFILL
    assert stopped.results[0].outcome == STOPPED
    video = store.get_video("youtube", V1)
    assert store.count_comments("youtube", V1) == 4
    assert (video["collect_status"], video["resume_token"]) == (dbm.COLLECT_PARTIAL, "4")

    before = len(fake.called("commentThreads"))
    plan, resumed = collect(cfg(one_video(big)), store, fake, page_size=2)
    assert (plan.videos[0].mode, plan.videos[0].label) == (MODE_BACKFILL, "이어하기")
    assert resumed.new == 6 and store.count_comments("youtube", V1) == 10
    # 새 댓글 확인 한 쪽 + 남은 세 쪽. 처음부터 다시 받지 않는다.
    tokens = [call.get("pageToken") for call in fake.called("commentThreads")[before:]]
    assert tokens == [None, "4", "6", "8"]
    video = store.get_video("youtube", V1)
    assert (video["collect_status"], video["resume_token"]) == (dbm.COLLECT_FULL, None)


def test_expired_resume_position_restarts_from_the_top(cfg, store, fake):
    fake.add_video(V1, comments=10)
    big = "  full_rescan_max_comments: 3"
    collect(cfg(one_video(big, budget=3)), store, fake, page_size=2)
    fake.expired_tokens.add("4")
    _, resumed = collect(cfg(one_video(big)), store, fake, page_size=2)

    assert resumed.results[0].outcome == DONE
    assert resumed.new == 6 and store.count_comments("youtube", V1) == 10


def test_comment_cap_stops_and_raising_it_gets_the_rest(cfg, store, fake):
    fake.add_video(V1, comments=10)
    capped = "  max_comments_per_video: 4"
    plan, summary = collect(cfg(one_video(capped)), store, fake, page_size=2)
    assert (plan.videos[0].label, plan.videos[0].over_cap) == ("부분 수집", 6)
    assert summary.results[0].outcome == CAPPED
    assert store.count_comments("youtube", V1) == 4
    assert store.get_video("youtube", V1)["collect_status"] == dbm.COLLECT_PARTIAL
    assert missing_now(store) == set()

    fake.add_comment(V1, "new1", "새 댓글")
    _, summary = collect(cfg(one_video(capped)), store, fake, page_size=2)
    assert summary.new == 1 and summary.missing == 0

    _, summary = collect(cfg(one_video()), store, fake, page_size=2)
    assert summary.results[0].outcome == DONE
    assert store.count_comments("youtube", V1) == 11 and missing_now(store) == set()


def test_large_video_checks_only_new_comments(cfg, store, fake):
    fake.add_video(V1, comments=6)
    big = "  full_rescan_max_comments: 3"
    collect(cfg(one_video(big)), store, fake, page_size=2)
    fake.add_comment(V1, "new1", "새 댓글")
    fake.remove(f"{V1}-c0")
    fake.edit(f"{V1}-c1", "고친 댓글")

    before = len(fake.called("commentThreads"))
    plan, summary = collect(cfg(one_video(big)), store, fake, page_size=2)
    assert (plan.videos[0].mode, plan.videos[0].label) == (MODE_NEW, "새 댓글만")
    assert (summary.new, summary.edited, summary.missing) == (1, 0, 0)
    assert len(fake.called("commentThreads")) - before == 2     # 아는 댓글만 나온 쪽에서 멈춘다
    assert missing_now(store) == set()                           # 부분 확인으로는 표시하지 않는다

    plan, summary = collect(cfg(one_video(big)), store, fake, page_size=2, full=True)
    assert plan.videos[0].mode == MODE_FULL
    assert (summary.edited, summary.missing) == (1, 1)


def test_negative_comments_are_rechecked_by_id_regardless_of_video_size(cfg, store, fake):
    fake.add_video(V1, comments=6)
    big = "  full_rescan_max_comments: 3"
    collect(cfg(one_video(big)), store, fake, page_size=2)
    mark_negative(store, f"{V1}-c0")
    mark_negative(store, f"{V1}-c1")
    fake.remove(f"{V1}-c0")
    fake.edit(f"{V1}-c1", "더 심한 말")

    _, summary = collect(cfg(one_video(big)), store, fake, page_size=2)
    assert (summary.recheck.checked, summary.recheck.edited, summary.recheck.missing) == (2, 1, 1)
    assert sorted(fake.called("comments")[-1]["id"].split(",")) == [f"{V1}-c0", f"{V1}-c1"]
    assert missing_now(store) == {f"{V1}-c0"}
    assert texts(store)[f"{V1}-c1"] == "더 심한 말"

    # 사라진 댓글이 다시 보이면 조회 불가에서 빠진다. 감지 기록은 지우지 않는다.
    # (시각은 초 단위라, 사라짐을 감지한 때를 과거로 돌려 놓고 확인한다.)
    store.conn.execute(
        "UPDATE comments SET missing_detected_at = '2026-01-02T00:00:00Z', "
        "last_seen_at = '2026-01-01T00:00:00Z' WHERE comment_id = ?", (f"{V1}-c0",)
    )
    fake.threads[V1].append({"id": f"{V1}-c0", "text": "댓글 0", "author": "작성자", "replies": []})
    _, summary = collect(cfg(one_video(big)), store, fake, page_size=2)
    assert summary.recheck.missing == 0 and missing_now(store) == set()
    assert store.scalar(
        "SELECT missing_detected_at FROM comments WHERE comment_id = ?", (f"{V1}-c0",)
    )


def test_videos_with_comments_turned_off_are_skipped_and_recorded(cfg, store, fake):
    fake.add_video(V1, comments=2)
    fake.add_video(V2, comments=2)
    fake.disabled.add(V1)
    config = cfg(f'youtube:\n  videos: ["{V1}", "{V2}"]\n')
    plan, summary = collect(config, store, fake)
    assert [info.video_id for info in plan.disabled] == [V1]
    assert [result.video_id for result in summary.results] == [V2]
    assert store.get_video("youtube", V1)["skip_reason"] == "댓글 사용 중지"

    # 견적 뒤에 댓글이 막힌 경우: 그 영상만 건너뛰고 계속한다.
    fake.add_video(V3, comments=2)
    config = cfg(f'youtube:\n  videos: ["{V3}", "{V2}"]\n')
    client = make_client(config, store, fake)
    plan = build_plan(config, store, client)
    register_plan(store, plan, config)
    fake.disabled.add(V3)
    summary = run_collection(YouTubeCollector(store, client, max_comments=100), plan.videos)
    assert [result.outcome for result in summary.results] == [SKIPPED, DONE]
    assert store.get_video("youtube", V3)["skip_reason"] == "댓글 사용 중지"


def test_video_limit_keeps_the_most_commented_videos(cfg, store, fake):
    for video_id, comments in ((V1, 1), (V2, 5), (V3, 3)):
        fake.add_video(video_id, comments=comments)
    config = cfg(f"""
    youtube:
      videos: ["{V1}", "{V2}", "{V3}"]
    limits:
      max_videos_per_run: 2
    """)
    plan, summary = collect(config, store, fake)
    assert [video.info.video_id for video in plan.videos] == [V2, V3]   # 댓글 많은 순
    assert [video.info.video_id for video in plan.deferred] == [V1]
    assert len(summary.results) == 2
    assert store.get_video("youtube", V1)["collect_status"] == dbm.COLLECT_NONE

    plan, _ = collect(config, store, fake)                              # 순위가 그대로면 대상도 그대로
    assert [video.info.video_id for video in plan.videos] == [V2, V3]
    assert store.count_comments("youtube", V1) == 0


def test_quota_stop_reports_videos_not_reached(cfg, store, fake):
    fake.add_video(V1, comments=2)
    fake.add_video(V2, comments=2)
    config = cfg(f'youtube:\n  videos: ["{V1}", "{V2}"]\n')
    fake.quota_after = 2                                         # 영상 조회, 첫 영상 한 쪽까지
    _, summary = collect(config, store, fake)
    assert [result.outcome for result in summary.results] == [DONE, STOPPED]
    assert "할당량" in summary.stopped_reason and summary.not_reached == []
    assert store.count_comments("youtube", V2) == 0
