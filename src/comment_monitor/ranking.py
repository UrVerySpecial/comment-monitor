"""작성자 랭킹과 통계 요약의 계산.

둘 다 db.judged_rows()가 주는 댓글별 행(유효한 최종 판정, 검토 상태, 캡처와 조회 불가 여부)을
한 번 훑어서 만든다. 엑셀에 어떻게 그릴지는 exporters/excel.py가 정한다.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from .config import TARGET_OTHER, TARGET_UNKNOWN
from .db import COLLECT_FULL, LEGAL_TYPES, SENTIMENTS

NEGATIVE = "부정"
REVIEW_EXCLUDED = "제외"
# 지금 설정에 없는 이름으로 남은 예전 판정을 모으는 칸. 다시 분류해야 한다는 표시다.
PAST_TARGET = "기타(이전 대상)"
# 랭킹에 건수를 따로 세는 유형
RANKED_TYPES = ("모욕", "명예훼손 의심", "성희롱", "협박", "스토킹성")
SEVERITIES = (1, 2, 3, 4, 5)


def target_bucket(target: str, target_names: Sequence[str]) -> str:
    """판정의 대상 값을 집계 칸으로 옮긴다."""
    if target in target_names or target in (TARGET_OTHER, TARGET_UNKNOWN):
        return target
    return PAST_TARGET


def author_key(row: Any) -> tuple[str, str]:
    """작성자 묶음 기준. 채널 ID로 묶고, 없을 때만 이름으로 묶는다."""
    if row["author_channel_id"]:
        return (row["platform"], row["author_channel_id"])
    return (row["platform"], "이름:" + (row["author_name"] or ""))


# ---- 작성자 랭킹 ----


@dataclass
class AuthorRank:
    platform: str
    channel_id: str | None
    name: str | None = None            # 가장 최근 댓글의 작성자명
    profile_url: str | None = None
    rank: int = 0
    score: int = 0                     # 악성 점수 = 심각도의 합
    by_target: dict[str, int] = field(default_factory=dict)   # 대상별 악성 점수
    count: int = 0
    max_severity: int = 0
    by_type: dict[str, int] = field(default_factory=dict)
    videos: set[str] = field(default_factory=set)
    first_at: str | None = None
    last_at: str | None = None
    missing: int = 0
    main_target: str = ""


def ranking_buckets(target_names: Sequence[str]) -> list[str]:
    """대상별 악성 점수 칸의 순서. 주 대상이 동점일 때의 우선순위이기도 하다."""
    return [*target_names, TARGET_UNKNOWN, PAST_TARGET]


def rank_authors(rows: Iterable[Any], target_names: Sequence[str]) -> list[AuthorRank]:
    """부정이고, 대상이 타인이 아니고, 검토 상태가 제외가 아닌 댓글로 작성자 순위를 매긴다."""
    buckets = ranking_buckets(target_names)
    authors: dict[tuple[str, str], AuthorRank] = {}
    for row in rows:
        if row["sentiment"] != NEGATIVE or row["target"] == TARGET_OTHER:
            continue
        if row["review_status"] == REVIEW_EXCLUDED:
            continue
        key = author_key(row)
        author = authors.get(key)
        if author is None:
            author = authors[key] = AuthorRank(row["platform"], row["author_channel_id"])
        severity = row["severity"] or 0
        bucket = target_bucket(row["target"], target_names)
        author.score += severity
        author.by_target[bucket] = author.by_target.get(bucket, 0) + severity
        author.count += 1
        author.max_severity = max(author.max_severity, severity)
        if row["legal_type"] in RANKED_TYPES:
            author.by_type[row["legal_type"]] = author.by_type.get(row["legal_type"], 0) + 1
        author.videos.add(row["video_id"])
        author.missing += 1 if row["missing"] else 0
        published = row["published_at"]
        if published:
            if author.first_at is None or published < author.first_at:
                author.first_at = published
            if author.last_at is None or published >= author.last_at:
                author.last_at = published
                author.name = row["author_name"] or author.name
                author.profile_url = row["author_profile_url"] or author.profile_url
        if author.name is None:
            author.name = row["author_name"]
        if author.profile_url is None:
            author.profile_url = row["author_profile_url"]

    ranked = sorted(
        authors.values(),
        key=lambda a: (-a.score, -a.max_severity, -a.count, a.name or "", a.channel_id or ""),
    )
    for index, author in enumerate(ranked, start=1):
        author.rank = index
        # 점수가 가장 높은 대상. 동점이면 칸 순서가 앞선 쪽.
        author.main_target = max(buckets, key=lambda b: (author.by_target.get(b, 0), -buckets.index(b)))
    return ranked


# ---- 통계 요약 ----


@dataclass
class VideoStats:
    video_id: str
    title: str
    channel: str
    total_comments: int | None        # 플랫폼이 알려준 전체 댓글 수
    collect_status: str
    collected: int = 0
    sentiments: dict[str, int] = field(default_factory=dict)
    negative_by_target: dict[str, int] = field(default_factory=dict)
    unclassified: int = 0

    @property
    def classified(self) -> int:
        return sum(self.sentiments.values())

    def ratio(self, sentiment: str) -> float | None:
        """분류된 댓글 가운데 이 감성의 비율."""
        return self.sentiments.get(sentiment, 0) / self.classified if self.classified else None

    @property
    def representative(self) -> bool:
        """전체를 수집했고 미분류가 없어야 비율이 영상 전체를 대표한다."""
        return self.collect_status == COLLECT_FULL and self.unclassified == 0


@dataclass
class TargetStats:
    target: str
    negative: int = 0
    by_severity: dict[int, int] = field(default_factory=dict)
    by_type: dict[str, int] = field(default_factory=dict)
    authors: set[tuple[str, str]] = field(default_factory=set)
    captured: int = 0
    missing: int = 0


@dataclass
class Stats:
    videos: list[VideoStats]
    targets: list[TargetStats]
    comments: int = 0
    sentiments: dict[str, int] = field(default_factory=dict)
    unclassified: int = 0
    failed: int = 0
    collected_from: str | None = None   # 수집 기간
    collected_to: str | None = None
    published_from: str | None = None   # 댓글 작성 기간
    published_to: str | None = None


def stat_buckets(target_names: Sequence[str]) -> list[str]:
    return [*target_names, TARGET_OTHER, TARGET_UNKNOWN, PAST_TARGET]


def build_stats(
    rows: Iterable[Any], videos: Iterable[Any], target_names: Sequence[str]
) -> Stats:
    """영상별, 대상별, 전체 합계. 검토 상태와 무관하게 AI 판정 그대로 센다."""
    by_video = {
        video["video_id"]: VideoStats(
            video["video_id"], video["title"] or "", video["channel_title"] or "",
            video["comment_count"], video["collect_status"],
        )
        for video in videos
    }
    by_target = {bucket: TargetStats(bucket) for bucket in stat_buckets(target_names)}
    stats = Stats(videos=[], targets=[])

    for row in rows:
        stats.comments += 1
        collected, published = row["first_collected_at"], row["published_at"]
        if collected:
            stats.collected_from = min(stats.collected_from or collected, collected)
            stats.collected_to = max(stats.collected_to or collected, collected)
        if published:
            stats.published_from = min(stats.published_from or published, published)
            stats.published_to = max(stats.published_to or published, published)

        video = by_video.get(row["video_id"])
        if video is not None:
            video.collected += 1
        sentiment = row["sentiment"]
        if sentiment is None:
            stats.unclassified += 1
            stats.failed += 1 if row["failed"] else 0
            if video is not None:
                video.unclassified += 1
            continue
        stats.sentiments[sentiment] = stats.sentiments.get(sentiment, 0) + 1
        if video is not None:
            video.sentiments[sentiment] = video.sentiments.get(sentiment, 0) + 1
        if sentiment != NEGATIVE:
            continue

        bucket = target_bucket(row["target"], target_names)
        if video is not None:
            video.negative_by_target[bucket] = video.negative_by_target.get(bucket, 0) + 1
        target = by_target[bucket]
        target.negative += 1
        severity = row["severity"] or 0
        target.by_severity[severity] = target.by_severity.get(severity, 0) + 1
        target.by_type[row["legal_type"]] = target.by_type.get(row["legal_type"], 0) + 1
        target.authors.add(author_key(row))
        target.captured += 1 if row["captured"] else 0
        target.missing += 1 if row["missing"] else 0

    # 댓글을 한 건도 모으지 않은 제외 영상은 통계에 올리지 않는다.
    stats.videos = [v for v in by_video.values() if v.collected or v.collect_status != "none"]
    stats.targets = [
        t for t in by_target.values() if t.target != PAST_TARGET or t.negative
    ]
    return stats


__all__ = [
    "LEGAL_TYPES", "SENTIMENTS", "PAST_TARGET", "RANKED_TYPES", "SEVERITIES",
    "AuthorRank", "Stats", "TargetStats", "VideoStats",
    "build_stats", "rank_authors", "ranking_buckets", "stat_buckets", "target_bucket",
]
