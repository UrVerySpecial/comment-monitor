"""대상 확정, 사전 견적, 상한 적용.

수집을 시작하기 전에 어떤 영상을 어떤 방식으로 다룰지 정하고,
할당량과 분류 비용, 캡처 시간이 얼마나 들지 계산한다.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from . import db as dbm
from .collectors.base import MODE_BACKFILL, MODE_FULL, MODE_NEW, PlannedVideo, VideoInfo
from .collectors.youtube import COSTS, ID_BATCH, PLATFORM, YouTubeClient
from .config import Config
from .db import Database

# 답글이 많은 스레드는 답글 호출이 따로 든다. 쪽수에 이만큼을 곱해 어림한다.
REPLY_CALL_FACTOR = 1.5
# 분류 자료가 없는 첫 실행에서 가정하는 캡처 대상 비율
DEFAULT_CAPTURE_RATE = 0.05
SECONDS_PER_CAPTURE = 12
# 2단 분류에서 1차 결과 중 최종 판정으로 넘어간다고 보는 비율
SECOND_PASS_RATE = 0.10
# 댓글 한 건의 분류 비용(원). DESIGN.md 14장의 추정치에서 온 값이고,
# 분류를 한 번이라도 돌린 뒤에는 기록된 토큰 사용량으로 계산한 실측값을 대신 쓴다.
KRW_PER_COMMENT = {"opus": 4.0, "haiku": 0.1}
# 토큰 100만 개당 달러 값: (입력, 출력, 캐시 읽기 배수). 캐시 쓰기는 입력의 1.25배다.
USD_PER_MTOK = {
    "opus": (4.0, 20.0, 0.05),
    "sonnet": (2.0, 10.0, 0.05),
    "haiku": (0.10, 0.50, 0.10),
}
CACHE_WRITE_FACTOR = 1.25
KRW_PER_USD = 1400  # 비용 표시에 쓰는 어림 환율

_SOURCE_LABELS = {
    dbm.SOURCE_DIRECT: "지정",
    dbm.SOURCE_CHANNEL: "채널",
    dbm.SOURCE_SEARCH: "검색",
}
_PRIORITY = {dbm.SOURCE_DIRECT: 0, dbm.SOURCE_CHANNEL: 1, dbm.SOURCE_SEARCH: 2}


def source_label(source: str) -> str:
    return _SOURCE_LABELS[source]


@dataclass(frozen=True)
class Estimate:
    units: int                    # 이번 수집의 예상 할당량
    quota_used: int               # 오늘 이미 쓴 할당량 (견적을 내느라 쓴 것 포함)
    quota_budget: int
    new_comments: int             # 새로 들어올 것으로 보는 댓글 수
    over_cap_comments: int        # 영상당 상한 때문에 가져오지 않을 댓글 수
    classify_count: int           # 이번 실행에서 분류할 건수
    classify_left: int            # 실행당 분류 상한 때문에 남을 건수
    classify_cost_krw: int
    cost_needs_confirm: bool      # 예상 분류 비용이 확인 기준을 넘는가
    capture_count: int
    capture_left: int
    capture_seconds: int
    capture_rate: float
    capture_rate_assumed: bool    # 실제 자료가 없어 기본 비율을 썼는가

    @property
    def quota_left(self) -> int:
        return max(0, self.quota_budget - self.quota_used)

    @property
    def over_budget(self) -> bool:
        return self.units > self.quota_left


@dataclass
class Plan:
    videos: list[PlannedVideo] = field(default_factory=list)      # 이번에 처리할 영상
    deferred: list[PlannedVideo] = field(default_factory=list)    # 댓글 수 순위가 영상 수 상한 밖
    disabled: list[VideoInfo] = field(default_factory=list)       # 댓글이 막힌 영상
    excluded: list[str] = field(default_factory=list)             # 제외 목록에 있는 영상 ID
    unavailable: list[str] = field(default_factory=list)          # 조회되지 않는 영상 ID
    searches_run: int = 0
    searches_cached: int = 0
    notes: list[str] = field(default_factory=list)
    sources: dict[str, tuple[str, str | None]] = field(default_factory=dict)  # 영상 ID별 발견 경로
    estimate: Estimate | None = None


def _family(model: str) -> str:
    """모델 이름이 속한 가격대. 모르는 모델은 비싼 쪽으로 잡는다."""
    for family in USD_PER_MTOK:
        if family in model.lower():
            return family
    return "opus"


def tokens_cost_krw(usage: Any) -> float:
    """토큰 사용 기록 한 줄(model과 토큰 수 네 가지)의 실제 비용(원)."""
    price_in, price_out, cache_read = USD_PER_MTOK[_family(usage["model"])]
    usd = (
        usage["input_tokens"] * price_in
        + usage["cache_creation_input_tokens"] * price_in * CACHE_WRITE_FACTOR
        + usage["cache_read_input_tokens"] * price_in * cache_read
        + usage["output_tokens"] * price_out
    ) / 1_000_000
    return usd * KRW_PER_USD


def spent_krw(db: Database, *, run_id: int | None = None) -> float:
    """기록된 토큰 사용량으로 계산한 분류 비용(원)."""
    return sum(tokens_cost_krw(row) for row in db.token_usage_by_model(run_id=run_id))


def krw_per_comment(model: str, db: Database | None = None) -> float:
    """댓글 한 건의 분류 비용. 같은 가격대 모델의 실제 사용 기록이 있으면 그 평균을 쓴다."""
    family = _family(model)
    if db is not None:
        rows = [row for row in db.token_usage_by_model() if _family(row["model"]) == family]
        comments = sum(row["comment_count"] for row in rows)
        if comments:
            return sum(tokens_cost_krw(row) for row in rows) / comments
    return KRW_PER_COMMENT.get(family, max(KRW_PER_COMMENT.values()))


def classify_cost_krw(
    config: Config, count: int, db: Database | None = None, *, two_pass: bool = False
) -> int:
    """count건을 분류하는 예상 비용(원). 2단 분류는 1차 전체 + 최종 SECOND_PASS_RATE만큼."""
    classifier = config.classifier
    per_comment = krw_per_comment(classifier.model, db)
    if two_pass:
        per_comment = (
            krw_per_comment(classifier.first_pass_model, db) + SECOND_PASS_RATE * per_comment
        )
    return math.ceil(count * per_comment)


def _scan_units(comments: int) -> int:
    """댓글 수만큼 훑는 데 드는 예상 units. 확인만 해도 한 번은 호출한다."""
    if comments <= 0:
        return 1
    return math.ceil(math.ceil(comments / 100) * REPLY_CALL_FACTOR)


def _find_targets(
    config: Config, db: Database, client: YouTubeClient, plan: Plan, *, refresh_search: bool,
    now: str,
) -> dict[str, tuple[str, str | None]]:
    """지정 영상, 채널 영상, 검색 결과를 우선순위대로 합친다. 먼저 나온 경로가 남는다."""
    youtube = config.youtube
    found: dict[str, tuple[str, str | None]] = {}
    for video_id in youtube.videos:
        found.setdefault(video_id, (dbm.SOURCE_DIRECT, None))

    if youtube.channels:
        playlists = client.uploads_playlists(youtube.channels)
        for channel_id in youtube.channels:
            if channel_id not in playlists:
                plan.notes.append(f"채널을 찾을 수 없습니다: {channel_id}")
                continue
            video_ids = client.playlist_video_ids(playlists[channel_id], youtube.channel_max_videos)
            for video_id in video_ids:
                found.setdefault(video_id, (dbm.SOURCE_CHANNEL, channel_id))

    search = youtube.search
    if search.enabled:
        params = {
            "published_after": search.published_after.isoformat() if search.published_after else None
        }
        fresh_after = dbm.format_utc(dbm.parse_utc(now) - timedelta(days=search.cache_days))
        for keyword in search.keywords:
            cached = None if refresh_search else db.get_search_cache(
                PLATFORM, keyword.q, keyword.order, params
            )
            # 저장된 결과가 기간 안이고, 필요한 개수를 덮으면 다시 검색하지 않는다.
            usable = (
                cached is not None
                and cached["searched_at"] > fresh_after
                and (cached["requested"] >= keyword.top_n
                     or len(cached["video_ids"]) < cached["requested"])
            )
            if usable:
                video_ids = cached["video_ids"][:keyword.top_n]
                plan.searches_cached += 1
            else:
                video_ids = client.search(
                    keyword.q, order=keyword.order, top_n=keyword.top_n,
                    published_after=search.published_after,
                )
                db.put_search_cache(
                    PLATFORM, keyword.q, keyword.order, params,
                    requested=keyword.top_n, video_ids=video_ids, now=now,
                )
                plan.searches_run += 1
            for video_id in video_ids:
                found.setdefault(video_id, (dbm.SOURCE_SEARCH, keyword.q))

    excluded = set(youtube.exclude_videos)
    plan.excluded = [video_id for video_id in found if video_id in excluded]
    return {video_id: source for video_id, source in found.items() if video_id not in excluded}


def _plan_video(
    info: VideoInfo, source: tuple[str, str | None], row, stored: int, config: Config,
    *, full: bool, quick: bool,
) -> PlannedVideo:
    """영상 한 개의 수집 방식과 견적을 정한다 (DESIGN.md 5장 재확인 방식)."""
    limits = config.limits
    cap = limits.max_comments_per_video
    count = info.comment_count or 0
    never = row is None or row["collect_status"] == dbm.COLLECT_NONE
    resumable = row is not None and row["resume_token"] is not None and stored < cap
    reachable = min(count, cap)
    over_cap = max(0, count - cap)

    if full:
        mode, label = MODE_FULL, "전체 재확인"
    elif never:
        mode = MODE_FULL if count <= limits.full_rescan_max_comments and not quick else MODE_BACKFILL
        label = "부분 수집" if over_cap else "첫 수집"
    elif quick:
        mode, label = MODE_NEW, "새 댓글만"
    elif count <= limits.full_rescan_max_comments:
        mode, label = MODE_FULL, "전체 재확인"
    elif resumable:
        mode, label = MODE_BACKFILL, "이어하기"
    else:
        mode, label = MODE_NEW, "새 댓글만"

    # 새 댓글 수는 마지막 수집 때의 댓글 수와 비교해 어림한다.
    baseline = row["comment_count"] if row is not None and row["comment_count"] else stored
    grown = max(0, count - baseline)
    if mode == MODE_FULL:
        units = _scan_units(reachable)
        new_comments = max(0, reachable - stored)
    elif mode == MODE_NEW:
        units = _scan_units(grown)
        new_comments = grown
        over_cap = 0
    else:
        remaining = max(0, reachable - stored)
        units = _scan_units(remaining) + (_scan_units(grown) if stored else 0)
        new_comments = remaining
    return PlannedVideo(
        info=info, source=source[0], source_detail=source[1], is_new=never, mode=mode,
        label=label, est_units=units, est_new_comments=new_comments, over_cap=over_cap,
    )


def _estimate(
    config: Config, db: Database, plan: Plan, *, limit: int | None, two_pass: bool, now: str
) -> Estimate:
    limits = config.limits
    capture = config.capture
    negatives = len(db.negative_comments(PLATFORM))
    units = sum(video.est_units for video in plan.videos) + math.ceil(negatives / ID_BATCH) * COSTS[
        "comments"
    ]
    new_comments = sum(video.est_new_comments for video in plan.videos)

    to_classify = db.unclassified_count() + new_comments
    classify_count = min(to_classify, limits.max_classify_per_run, limit or to_classify)
    cost = classify_cost_krw(config, classify_count, db, two_pass=two_pass)

    measured = db.capture_rate(min_severity=capture.min_severity, targets=capture.targets)
    rate = DEFAULT_CAPTURE_RATE if measured is None else measured
    pending = db.status_summary(
        capture_min_severity=capture.min_severity, capture_targets=capture.targets, now=now
    )["captures_pending"]
    to_capture = pending + round(classify_count * rate)
    capture_count = min(to_capture, limits.max_captures_per_run, limit or to_capture)

    return Estimate(
        units=units,
        quota_used=db.quota_used_today(platform=PLATFORM, now=now),
        quota_budget=config.youtube.daily_quota_budget,
        new_comments=new_comments,
        over_cap_comments=sum(video.over_cap for video in plan.videos),
        classify_count=classify_count,
        classify_left=to_classify - classify_count,
        classify_cost_krw=cost,
        cost_needs_confirm=cost > limits.confirm_cost_over_krw,
        capture_count=capture_count,
        capture_left=to_capture - capture_count,
        capture_seconds=capture_count * SECONDS_PER_CAPTURE,
        capture_rate=rate,
        capture_rate_assumed=measured is None,
    )


def build_plan(
    config: Config,
    db: Database,
    client: YouTubeClient,
    *,
    full: bool = False,
    quick: bool = False,
    refresh_search: bool = False,
    limit: int | None = None,
    two_pass: bool = False,
    now: str | None = None,
) -> Plan:
    """대상 영상을 확정하고 견적을 낸다. 댓글은 가져오지 않는다.

    검색 결과가 저장되어 있지 않은 키워드는 여기서 실제로 검색한다(키워드당 100 units).
    """
    now = now or dbm.utc_now()
    plan = Plan()
    targets = _find_targets(config, db, client, plan, refresh_search=refresh_search, now=now)
    plan.sources = targets
    infos = client.videos(list(targets))
    plan.unavailable = [video_id for video_id in targets if video_id not in infos]

    rows = db.videos_by_id(PLATFORM)
    stored = db.comment_counts(PLATFORM)
    candidates = []
    for video_id, source in targets.items():
        info = infos.get(video_id)
        if info is None:
            continue
        if info.comment_count is None:
            plan.disabled.append(info)
            continue
        candidates.append(
            _plan_video(
                info, source, rows.get(video_id), stored.get(video_id, 0), config,
                full=full, quick=quick,
            )
        )

    # 댓글이 많은 영상부터 상한까지만 대상으로 삼는다. 댓글 수가 같으면 지정, 채널, 검색 순,
    # 그 안에서는 최신 영상부터. 상한 밖의 영상은 순위가 바뀌기 전에는 계속 대상에서 빠진다.
    candidates.sort(key=lambda video: video.info.published_at, reverse=True)
    candidates.sort(key=lambda video: (-(video.info.comment_count or 0), _PRIORITY[video.source]))

    limit_videos = config.limits.max_videos_per_run
    plan.videos, plan.deferred = candidates[:limit_videos], candidates[limit_videos:]
    plan.estimate = _estimate(config, db, plan, limit=limit, two_pass=two_pass, now=now)
    return plan


def register_plan(db: Database, plan: Plan, config: Config) -> None:
    """대상 영상을 DB에 올린다. 이번에 처리하지 못하는 영상도 미수집 상태로 남아 status에 보인다."""
    with db.transaction():
        for video in (*plan.videos, *plan.deferred):
            info = video.info
            db.upsert_video(
                platform=info.platform, video_id=info.video_id, title=info.title,
                channel_title=info.channel_title, channel_id=info.channel_id,
                published_at=info.published_at, comment_count=info.comment_count,
                source=video.source, source_detail=video.source_detail,
            )
        for info in plan.disabled:
            db.upsert_video(
                platform=info.platform, video_id=info.video_id, title=info.title,
                channel_title=info.channel_title, channel_id=info.channel_id,
                published_at=info.published_at, comment_count=None,
                source=plan.sources[info.video_id][0], source_detail=plan.sources[info.video_id][1],
            )
            db.set_video_skip(PLATFORM, info.video_id, "댓글 사용 중지")
        db.sync_excluded(PLATFORM, config.youtube.exclude_videos)
