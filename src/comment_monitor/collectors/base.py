"""플랫폼 공통 인터페이스.

수집기는 플랫폼마다 다르지만, 대상 영상을 순서대로 돌고 상한이나 할당량에 걸리면
멈추는 흐름은 같다. 그 흐름과 주고받는 자료형을 여기에 둔다.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Protocol

# 수집 방식
MODE_FULL = "full"          # 처음부터 끝까지 다시 훑는다. 수정과 사라짐까지 잡는다.
MODE_NEW = "new"            # 새 댓글만 확인한다.
MODE_BACKFILL = "backfill"  # 아직 다 가져오지 못한 영상을 이어서 가져온다.

# 영상 한 개의 수집 결과
DONE, CAPPED, STOPPED, SKIPPED, ERROR = "done", "capped", "stopped", "skipped", "error"


class CollectorError(Exception):
    """수집 중 생긴 오류."""


class QuotaStop(CollectorError):
    """하루 할당량 예산에 닿았거나 플랫폼이 할당량 초과로 거절했다. 수집을 멈춘다."""


class VideoUnavailable(CollectorError):
    """댓글이 막혔거나 영상을 볼 수 없다. 이 영상만 건너뛴다."""


@dataclass(frozen=True)
class VideoInfo:
    platform: str
    video_id: str
    title: str
    channel_title: str
    channel_id: str
    published_at: str
    comment_count: int | None  # 댓글이 막힌 영상은 None


@dataclass(frozen=True)
class PlannedVideo:
    """이번 실행에서 다룰 영상 한 개와 그 견적."""

    info: VideoInfo
    source: str
    source_detail: str | None
    is_new: bool                  # 아직 한 번도 수집하지 않은 영상
    mode: str
    label: str                    # 화면에 보여줄 수집 방식
    est_units: int = 0
    est_new_comments: int = 0
    over_cap: int = 0             # 영상당 댓글 상한 때문에 가져오지 않을 댓글 수


@dataclass
class VideoResult:
    video_id: str
    title: str
    mode: str
    new: int = 0
    edited: int = 0
    missing: int = 0
    outcome: str = DONE
    note: str = ""


@dataclass
class RecheckResult:
    checked: int = 0
    edited: int = 0
    missing: int = 0


@dataclass
class CollectionSummary:
    results: list[VideoResult] = field(default_factory=list)
    recheck: RecheckResult = field(default_factory=RecheckResult)
    stopped_reason: str | None = None
    not_reached: list[PlannedVideo] = field(default_factory=list)

    @property
    def new(self) -> int:
        return sum(result.new for result in self.results)

    @property
    def edited(self) -> int:
        return self.recheck.edited + sum(result.edited for result in self.results)

    @property
    def missing(self) -> int:
        return self.recheck.missing + sum(result.missing for result in self.results)


class Collector(Protocol):
    platform: str

    def recheck_negative(self) -> RecheckResult:
        """부정 판정된 댓글을 ID로 다시 조회해 수정과 사라짐을 확인한다."""

    def collect_video(self, target: PlannedVideo) -> VideoResult:
        """영상 한 개를 수집한다. 할당량에 걸리면 outcome이 STOPPED인 결과를 돌려준다."""


def run_collection(
    collector: Collector,
    targets: Sequence[PlannedVideo],
    *,
    on_result: Callable[[int, VideoResult], None] | None = None,
) -> CollectionSummary:
    """증거로 쓸 댓글의 변동을 먼저 확인하고, 대상 영상을 우선순위 순서대로 수집한다."""
    summary = CollectionSummary()
    try:
        summary.recheck = collector.recheck_negative()
    except QuotaStop as exc:
        summary.stopped_reason = str(exc)
        summary.not_reached = list(targets)
        return summary

    for index, target in enumerate(targets):
        result = collector.collect_video(target)
        summary.results.append(result)
        if on_result:
            on_result(index, result)
        if result.outcome == STOPPED:
            summary.stopped_reason = result.note
            summary.not_reached = list(targets[index + 1:])
            break
    return summary
