"""시험용 가짜 YouTube Data API.

실제 API의 응답 모양을 흉내 내는 전송 함수다. 영상과 댓글을 메모리에 두고,
쪽 나눔, 답글 5개 제한, 할당량 초과, 만료된 쪽 위치를 재현한다.
"""

from __future__ import annotations

from comment_monitor.collectors.youtube import HttpError

INLINE_REPLIES = 5  # 스레드 응답에 함께 오는 답글 수


def _error(status: int, reason: str) -> HttpError:
    return HttpError(status, {"error": {"message": reason, "errors": [{"reason": reason}]}})


class FakeYouTube:
    def __init__(self) -> None:
        self.videos: dict[str, dict] = {}
        self.threads: dict[str, list[dict]] = {}   # 영상별 스레드. 최신이 앞이다.
        self.searches: dict[str, list[str]] = {}
        self.channels: dict[str, list[str]] = {}
        self.counts: dict[str, int] = {}           # 댓글 수를 실제와 다르게 알려줄 영상
        self.disabled: set[str] = set()
        self.calls: list[tuple[str, dict]] = []
        self.quota_after: int | None = None        # 이만큼 호출한 뒤부터 할당량 초과로 거절
        self.expired_tokens: set[str] = set()      # 한 번 거절하고 나면 풀린다

    # ---- 자료 넣기 ----

    def add_video(self, video_id: str, *, title: str | None = None, channel: str = "채널",
                  published: str = "2026-10-01T00:00:00Z", comments: int = 0) -> None:
        self.videos[video_id] = {"title": title or f"영상 {video_id}", "channel": channel,
                                 "published": published}
        self.threads[video_id] = []
        for index in range(comments):
            self.add_comment(video_id, f"{video_id}-c{index}", f"댓글 {index}")

    def add_comment(self, video_id: str, comment_id: str, text: str, *, replies: int = 0,
                    author: str = "작성자") -> None:
        """새 댓글을 맨 위(최신)에 단다."""
        thread = {
            "id": comment_id, "text": text, "author": author,
            "replies": [
                {"id": f"{comment_id}.r{index}", "text": f"답글 {index}", "author": author}
                for index in range(replies)
            ],
        }
        self.threads[video_id].insert(0, thread)

    def find(self, comment_id: str) -> dict:
        for threads in self.threads.values():
            for thread in threads:
                for comment in (thread, *thread["replies"]):
                    if comment["id"] == comment_id:
                        return comment
        raise KeyError(comment_id)

    def edit(self, comment_id: str, text: str) -> None:
        self.find(comment_id)["text"] = text

    def remove(self, comment_id: str) -> None:
        for threads in self.threads.values():
            for thread in list(threads):
                if thread["id"] == comment_id:
                    threads.remove(thread)
                thread["replies"] = [r for r in thread["replies"] if r["id"] != comment_id]

    def called(self, resource: str) -> list[dict]:
        return [params for name, params in self.calls if name == resource]

    # ---- 응답 ----

    def __call__(self, resource: str, params) -> dict:
        if self.quota_after is not None and len(self.calls) >= self.quota_after:
            raise _error(403, "quotaExceeded")
        self.calls.append((resource, dict(params)))
        return getattr(self, f"_{resource}")(params)

    @staticmethod
    def _item(comment: dict, parent_id: str | None = None) -> dict:
        snippet = {
            "textDisplay": comment["text"],
            "authorDisplayName": comment["author"],
            "authorChannelId": {"value": f"UC{comment['author']}"},
            "authorChannelUrl": f"http://www.youtube.com/channel/UC{comment['author']}",
            "likeCount": 0,
            "publishedAt": "2026-11-01T00:00:00Z",
            "updatedAt": "2026-11-01T00:00:00Z",
        }
        if parent_id:
            snippet["parentId"] = parent_id
        return {"kind": "youtube#comment", "id": comment["id"], "snippet": snippet}

    @staticmethod
    def _page(items: list, params, size: int) -> tuple[list, str | None]:
        start = int(params.get("pageToken") or 0)
        size = min(size, int(params.get("maxResults", size)))
        end = start + size
        return items[start:end], (str(end) if end < len(items) else None)

    def _search(self, params) -> dict:
        items, token = self._page(self.searches.get(params["q"], []), params, 50)
        data = {"items": [{"id": {"kind": "youtube#video", "videoId": v}} for v in items]}
        if token:
            data["nextPageToken"] = token
        return data

    def _channels(self, params) -> dict:
        return {"items": [
            {"id": channel_id,
             "contentDetails": {"relatedPlaylists": {"uploads": f"UU{channel_id[2:]}"}}}
            for channel_id in params["id"].split(",") if channel_id in self.channels
        ]}

    def _playlistItems(self, params) -> dict:  # noqa: N802 - API 자원 이름 그대로
        video_ids = self.channels["UC" + params["playlistId"][2:]]
        items, token = self._page(video_ids, params, 50)
        data = {"items": [{"contentDetails": {"videoId": v}} for v in items]}
        if token:
            data["nextPageToken"] = token
        return data

    def _videos(self, params) -> dict:
        items = []
        for video_id in params["id"].split(","):
            video = self.videos.get(video_id)
            if video is None:
                continue
            statistics = {"viewCount": "1"}
            if video_id not in self.disabled:
                actual = sum(1 + len(t["replies"]) for t in self.threads[video_id])
                statistics["commentCount"] = str(self.counts.get(video_id, actual))
            items.append({
                "id": video_id,
                "snippet": {"title": video["title"], "channelTitle": video["channel"],
                            "channelId": "UCowner", "publishedAt": video["published"]},
                "statistics": statistics,
            })
        return {"items": items}

    def _commentThreads(self, params) -> dict:  # noqa: N802
        video_id = params["videoId"]
        if video_id in self.disabled:
            raise _error(403, "commentsDisabled")
        if video_id not in self.videos:
            raise _error(404, "videoNotFound")
        if params.get("pageToken") in self.expired_tokens:
            self.expired_tokens.discard(params["pageToken"])
            raise _error(400, "invalidPageToken")
        assert params["textFormat"] == "plainText" and params["order"] == "time"
        threads, token = self._page(self.threads[video_id], params, 100)
        data = {"items": [
            {
                "id": thread["id"],
                "snippet": {"videoId": video_id, "topLevelComment": self._item(thread),
                            "totalReplyCount": len(thread["replies"])},
                **({"replies": {"comments": [
                    self._item(reply, thread["id"]) for reply in thread["replies"][:INLINE_REPLIES]
                ]}} if thread["replies"] else {}),
            }
            for thread in threads
        ]}
        if token:
            data["nextPageToken"] = token
        return data

    def _comments(self, params) -> dict:
        assert params["textFormat"] == "plainText"
        if "parentId" in params:
            replies = self.find(params["parentId"])["replies"]
            items, token = self._page(replies, params, 100)
            data = {"items": [self._item(reply, params["parentId"]) for reply in items]}
            if token:
                data["nextPageToken"] = token
            return data
        items = []
        for comment_id in params["id"].split(","):
            try:
                items.append(self._item(self.find(comment_id)))
            except KeyError:
                pass
        return {"items": items}
