"""YouTube Data API v3 수집기.

YouTubeClient는 API 호출, 할당량 기록, 응답 해석을 맡고
YouTubeCollector는 영상 한 개를 어떤 순서로 훑고 DB에 어떻게 남길지를 맡는다.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

from .. import db as dbm
from ..db import CommentData, Database
from .base import (
    CAPPED,
    ERROR,
    MODE_FULL,
    MODE_NEW,
    SKIPPED,
    STOPPED,
    CollectorError,
    PlannedVideo,
    QuotaStop,
    RecheckResult,
    VideoInfo,
    VideoResult,
    VideoUnavailable,
)

PLATFORM = "youtube"
API_BASE = "https://www.googleapis.com/youtube/v3/"

# 호출당 할당량 (DESIGN.md 9장)
COSTS = {
    "search": 100,
    "channels": 1,
    "playlistItems": 1,
    "videos": 1,
    "commentThreads": 1,
    "comments": 1,
}
ID_BATCH = 50  # 한 번에 조회하는 영상 ID, 댓글 ID 수

_QUOTA_REASONS = {"quotaExceeded", "dailyLimitExceeded"}
_RETRY_REASONS = {"rateLimitExceeded", "userRateLimitExceeded", "backendError"}
_RETRY_STATUSES = {0, 500, 502, 503, 504}
_RETRIES = 3

Transport = Callable[[str, Mapping[str, Any]], dict]


class HttpError(CollectorError):
    """API가 오류로 응답했다. status 0은 연결 자체가 안 된 경우다."""

    def __init__(self, status: int, payload: Any = None) -> None:
        self.status = status
        error = payload.get("error", {}) if isinstance(payload, dict) else {}
        details = error.get("errors") or [{}]
        self.reason = details[0].get("reason", "")
        self.message = error.get("message") or (str(payload) if payload else "")
        super().__init__(f"YouTube API 오류 {status} {self.reason}: {self.message}".strip())


class InvalidPageToken(CollectorError):
    """저장해 둔 이어하기 위치를 더는 쓸 수 없다."""


def http_transport(api_key: str, *, timeout: float = 30) -> Transport:
    """실제 API를 호출하는 전송 함수. 키는 주소가 아니라 헤더로 보내 오류 문구에 남지 않게 한다."""

    def call(resource: str, params: Mapping[str, Any]) -> dict:
        url = API_BASE + resource + "?" + urllib.parse.urlencode(params)
        request = urllib.request.Request(
            url, headers={"X-goog-api-key": api_key, "Accept": "application/json"}
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            try:
                payload = json.loads(body)
            except ValueError:
                payload = {"error": {"message": body[:200]}}
            raise HttpError(exc.code, payload) from None
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            raise HttpError(0, {"error": {"message": f"연결 실패: {exc}"}}) from None

    return call


@dataclass(frozen=True)
class Thread:
    top: CommentData
    replies: tuple[CommentData, ...]  # 응답에 함께 온 답글. 전부가 아닐 수 있다.
    total_reply_count: int


@dataclass(frozen=True)
class ThreadPage:
    threads: tuple[Thread, ...]
    next_token: str | None


def _comment(item: Mapping[str, Any], video_id: str, parent_id: str | None) -> CommentData:
    snippet = item["snippet"]
    channel_id = (snippet.get("authorChannelId") or {}).get("value")
    profile_url = snippet.get("authorChannelUrl") or (
        f"https://www.youtube.com/channel/{channel_id}" if channel_id else None
    )
    return CommentData(
        platform=PLATFORM,
        comment_id=item["id"],
        video_id=video_id,
        # 본문은 항상 plainText 형식의 textDisplay로 받는다. 형식이 섞이면 수정으로 오인한다.
        text=snippet.get("textDisplay") or "",
        parent_id=parent_id,
        author_name=snippet.get("authorDisplayName"),
        author_channel_id=channel_id,
        author_profile_url=profile_url,
        like_count=snippet.get("likeCount"),
        published_at=snippet.get("publishedAt"),
        updated_at=snippet.get("updatedAt"),
        raw=item,
    )


def comment_url(video_id: str, comment_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}&lc={comment_id}"


def video_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


class YouTubeClient:
    def __init__(
        self,
        transport: Transport,
        db: Database,
        *,
        daily_budget: int,
        run_id: int | None = None,
        page_size: int = 100,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.transport = transport
        self.db = db
        self.daily_budget = daily_budget
        self.run_id = run_id
        self.page_size = page_size
        self._sleep = sleep

    def _call(self, resource: str, **params: Any) -> dict:
        cost = COSTS[resource]
        used = self.db.quota_used_today(platform=PLATFORM)
        if used + cost > self.daily_budget:
            raise QuotaStop(
                f"하루 할당량 예산에 닿았습니다 (오늘 {used:,} / 예산 {self.daily_budget:,} units)"
            )
        params = {key: value for key, value in params.items() if value is not None}
        api = f"{resource}.list"
        for attempt in range(_RETRIES + 1):
            try:
                data = self.transport(resource, params)
            except HttpError as exc:
                if exc.reason in _QUOTA_REASONS:
                    raise QuotaStop("유튜브가 할당량 초과로 요청을 거절했습니다") from None
                retryable = exc.status in _RETRY_STATUSES or exc.reason in _RETRY_REASONS
                if retryable and attempt < _RETRIES:
                    self._sleep(2.0 ** attempt)
                    continue
                if exc.status:
                    # 거절된 요청도 할당량을 쓴다.
                    self.db.record_quota(api, cost, platform=PLATFORM, run_id=self.run_id)
                raise
            self.db.record_quota(api, cost, platform=PLATFORM, run_id=self.run_id)
            return data
        raise AssertionError("unreachable")

    # ---- 대상 영상 찾기 ----

    def search(
        self, query: str, *, order: str, top_n: int, published_after: date | None = None
    ) -> list[str]:
        """키워드 검색 결과의 상위 top_n개 영상 ID. 50개마다 호출 한 번(100 units)이다."""
        video_ids: list[str] = []
        token = None
        while len(video_ids) < top_n:
            data = self._call(
                "search", part="snippet", type="video", q=query, order=order,
                maxResults=min(50, top_n - len(video_ids)),
                publishedAfter=f"{published_after.isoformat()}T00:00:00Z" if published_after else None,
                pageToken=token,
            )
            for item in data.get("items", []):
                video_id = (item.get("id") or {}).get("videoId")
                if video_id and video_id not in video_ids:
                    video_ids.append(video_id)
            token = data.get("nextPageToken")
            if not token or not data.get("items"):
                break
        return video_ids[:top_n]

    def uploads_playlists(self, channel_ids: Sequence[str]) -> dict[str, str]:
        """채널 ID별 업로드 재생목록 ID. 찾지 못한 채널은 결과에 없다."""
        found: dict[str, str] = {}
        for start in range(0, len(channel_ids), ID_BATCH):
            chunk = channel_ids[start:start + ID_BATCH]
            data = self._call(
                "channels", part="contentDetails", id=",".join(chunk), maxResults=ID_BATCH
            )
            for item in data.get("items", []):
                uploads = item.get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads")
                if uploads:
                    found[item["id"]] = uploads
        return found

    def playlist_video_ids(self, playlist_id: str, max_videos: int) -> list[str]:
        """업로드 목록의 최신 영상부터 max_videos개."""
        video_ids: list[str] = []
        token = None
        while len(video_ids) < max_videos:
            try:
                data = self._call(
                    "playlistItems", part="contentDetails", playlistId=playlist_id,
                    maxResults=min(50, max_videos - len(video_ids)), pageToken=token,
                )
            except HttpError as exc:
                if exc.status == 404:  # 올린 영상이 없는 채널
                    break
                raise
            for item in data.get("items", []):
                video_id = item.get("contentDetails", {}).get("videoId")
                if video_id and video_id not in video_ids:
                    video_ids.append(video_id)
            token = data.get("nextPageToken")
            if not token:
                break
        return video_ids[:max_videos]

    def videos(self, video_ids: Sequence[str]) -> dict[str, VideoInfo]:
        """영상 정보와 댓글 수. 삭제되었거나 비공개인 영상은 결과에 없다."""
        found: dict[str, VideoInfo] = {}
        for start in range(0, len(video_ids), ID_BATCH):
            chunk = video_ids[start:start + ID_BATCH]
            data = self._call(
                "videos", part="snippet,statistics", id=",".join(chunk), maxResults=ID_BATCH
            )
            for item in data.get("items", []):
                snippet = item.get("snippet", {})
                count = item.get("statistics", {}).get("commentCount")
                found[item["id"]] = VideoInfo(
                    platform=PLATFORM,
                    video_id=item["id"],
                    title=snippet.get("title", ""),
                    channel_title=snippet.get("channelTitle", ""),
                    channel_id=snippet.get("channelId", ""),
                    published_at=snippet.get("publishedAt", ""),
                    comment_count=int(count) if count is not None else None,
                )
        return found

    # ---- 댓글 ----

    def thread_page(self, video_id: str, page_token: str | None = None) -> ThreadPage:
        """댓글 스레드 한 쪽. 최신 댓글부터 온다."""
        try:
            data = self._call(
                "commentThreads", part="snippet,replies", videoId=video_id, order="time",
                textFormat="plainText", maxResults=self.page_size, pageToken=page_token,
            )
        except HttpError as exc:
            if exc.reason == "commentsDisabled":
                raise VideoUnavailable("댓글 사용 중지") from None
            if exc.status == 404:
                raise VideoUnavailable("영상 조회 불가") from None
            if exc.status == 403:
                raise VideoUnavailable(f"접근 불가 ({exc.reason or 'forbidden'})") from None
            if exc.status == 400 and page_token:
                raise InvalidPageToken(str(exc)) from None
            raise
        threads = []
        for item in data.get("items", []):
            snippet = item["snippet"]
            top = _comment(snippet["topLevelComment"], video_id, None)
            replies = tuple(
                _comment(reply, video_id, top.comment_id)
                for reply in item.get("replies", {}).get("comments", [])
            )
            threads.append(Thread(top, replies, int(snippet.get("totalReplyCount") or 0)))
        return ThreadPage(tuple(threads), data.get("nextPageToken"))

    def all_replies(self, video_id: str, parent_id: str) -> list[CommentData]:
        replies: list[CommentData] = []
        token = None
        while True:
            try:
                data = self._call(
                    "comments", part="snippet", parentId=parent_id, textFormat="plainText",
                    maxResults=100, pageToken=token,
                )
            except HttpError as exc:
                if exc.status == 404:  # 조회하는 사이에 원댓글이 사라짐
                    return replies
                raise
            replies.extend(_comment(item, video_id, parent_id) for item in data.get("items", []))
            token = data.get("nextPageToken")
            if not token:
                return replies

    def comments_by_id(self, comment_ids: Sequence[str]) -> dict[str, dict]:
        """댓글 ID로 직접 조회한다. 돌아오지 않은 ID는 지금 조회할 수 없는 댓글이다."""
        data = self._call(
            "comments", part="snippet", id=",".join(comment_ids), textFormat="plainText",
            maxResults=100,
        )
        return {item["id"]: item for item in data.get("items", [])}


class YouTubeCollector:
    platform = PLATFORM

    def __init__(self, db: Database, client: YouTubeClient, *, max_comments: int) -> None:
        self.db = db
        self.client = client
        self.max_comments = max_comments

    def recheck_negative(self) -> RecheckResult:
        """부정 판정된 댓글을 영상 크기와 무관하게 ID로 다시 조회한다."""
        result = RecheckResult()
        rows = self.db.negative_comments(PLATFORM)
        for start in range(0, len(rows), ID_BATCH):
            chunk = rows[start:start + ID_BATCH]
            found = self.client.comments_by_id([row["comment_id"] for row in chunk])
            gone = []
            with self.db.transaction():
                for row in chunk:
                    item = found.get(row["comment_id"])
                    if item is None:
                        gone.append(row["comment_id"])
                        continue
                    outcome = self.db.upsert_comment(
                        _comment(item, row["video_id"], row["parent_id"])
                    )
                    result.edited += outcome == dbm.EDITED
                result.missing += self.db.mark_missing_ids(PLATFORM, gone)
            result.checked += len(chunk)
        return result

    def collect_video(self, target: PlannedVideo) -> VideoResult:
        video_id = target.info.video_id
        result = VideoResult(video_id, target.info.title, target.mode)
        try:
            if target.mode == MODE_NEW:
                self._scan(video_id, "head", result)
            elif target.mode == MODE_FULL:
                self._scan(video_id, "fresh", result)
            else:
                # 이어서 가져오기 전에, 지난번 이후 맨 위에 새로 달린 댓글부터 챙긴다.
                if self.db.count_comments(PLATFORM, video_id):
                    self._scan(video_id, "head", result)
                token = self.db.get_video(PLATFORM, video_id)["resume_token"]
                self._scan(video_id, "backfill", result, token=token)
        except QuotaStop as exc:
            result.outcome, result.note = STOPPED, str(exc)
            return result
        except VideoUnavailable as exc:
            self.db.set_video_skip(PLATFORM, video_id, str(exc))
            result.outcome, result.note = SKIPPED, str(exc)
            return result
        except CollectorError as exc:
            result.outcome, result.note = ERROR, str(exc)
            return result
        self.db.finish_video(PLATFORM, video_id, comment_count=target.info.comment_count)
        return result

    def _thread_comments(self, video_id: str, thread: Thread) -> list[CommentData]:
        replies = {reply.comment_id: reply for reply in thread.replies}
        if thread.total_reply_count > len(replies):
            # 스레드 응답에는 답글이 일부만 들어 있다. 나머지는 답글 API로 가져온다.
            for reply in self.client.all_replies(video_id, thread.top.comment_id):
                replies[reply.comment_id] = reply
        return [thread.top, *replies.values()]

    def _scan(
        self, video_id: str, kind: str, result: VideoResult, *, token: str | None = None
    ) -> None:
        """댓글 스레드를 최신부터 한 쪽씩 훑는다.

        head:     이미 아는 댓글만 있는 쪽이 나오면 멈춘다. 이어하기 위치는 건드리지 않는다.
        fresh:    처음부터 끝까지. 상한에 걸리지 않고 끝나면 보이지 않은 댓글을 조회 불가로 표시한다.
        backfill: 저장된 위치에서 끝까지. 위치가 만료됐으면 처음부터 다시 훑는다.

        한 쪽을 저장할 때 다음 쪽의 위치를 같은 트랜잭션에 남기므로,
        어디서 끊겨도 다음 실행이 그 쪽부터 이어서 한다.
        """
        seen: set[str] = set()
        stored = self.db.count_comments(PLATFORM, video_id)
        restarted = False
        while True:
            # 새로 훑을 때는 이번에 본 댓글 수로, 이어서 채울 때는 쌓인 댓글 수로 상한을 잰다.
            reached = len(seen) if kind == "fresh" else stored
            if kind != "head" and reached >= self.max_comments:
                result.outcome = CAPPED
                result.note = f"영상당 댓글 상한 {self.max_comments:,}건"
                return
            try:
                page = self.client.thread_page(video_id, token)
            except InvalidPageToken:
                if kind != "backfill" or restarted:
                    raise
                token, restarted = None, True
                continue

            comments = [
                comment
                for thread in page.threads
                for comment in self._thread_comments(video_id, thread)
            ]
            top_ids = [thread.top.comment_id for thread in page.threads]
            all_known = len(self.db.existing_comment_ids(PLATFORM, top_ids)) == len(top_ids)

            with self.db.transaction():
                for comment in comments:
                    outcome = self.db.upsert_comment(comment)
                    result.new += outcome == dbm.NEW
                    result.edited += outcome == dbm.EDITED
                    stored += outcome == dbm.NEW
                    seen.add(comment.comment_id)
                if kind != "head":
                    self.db.set_video_progress(
                        PLATFORM, video_id,
                        collect_status=dbm.COLLECT_PARTIAL if page.next_token else dbm.COLLECT_FULL,
                        resume_token=page.next_token,
                    )

            if kind == "head" and all_known:
                return
            token = page.next_token
            if not token:
                break

        if kind == "fresh":
            result.missing += self.db.mark_missing_except(PLATFORM, video_id, seen)
