"""수집 시험에서 함께 쓰는 도우미."""

from __future__ import annotations

from comment_monitor.collectors.base import run_collection
from comment_monitor.collectors.youtube import YouTubeClient, YouTubeCollector
from comment_monitor.planner import build_plan, register_plan

V1, V2, V3 = "vid00000001", "vid00000002", "vid00000003"


def make_client(config, db, fake, *, page_size=100):
    return YouTubeClient(
        fake, db, daily_budget=config.youtube.daily_quota_budget, page_size=page_size,
        sleep=lambda seconds: None,
    )


def collect(config, db, fake, *, page_size=100, **options):
    """plan부터 수집까지 한 번 실행하고 (계획, 수집 결과)를 돌려준다."""
    client = make_client(config, db, fake, page_size=page_size)
    plan = build_plan(config, db, client, **options)
    register_plan(db, plan, config)
    collector = YouTubeCollector(db, client, max_comments=config.limits.max_comments_per_video)
    return plan, run_collection(collector, plan.videos)


def mark_negative(db, comment_id, *, severity=3, target="홍길동"):
    db.conn.execute(
        "INSERT INTO classifications (platform, comment_id, stage, sentiment, target, "
        "legal_type, severity, reason, model, prompt_version, classified_at) "
        "VALUES ('youtube', ?, 'final', '부정', ?, '모욕', ?, '욕설', 'm', 'v1', "
        "'2026-11-02T00:00:00Z')",
        (comment_id, target, severity),
    )
