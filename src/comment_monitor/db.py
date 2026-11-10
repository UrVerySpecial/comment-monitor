"""SQLite 스키마와 접근.

이 DB가 원본이다. 지켜야 할 것:
- 댓글의 고유 키는 (platform, comment_id)이고 중복 방지는 전부 이 키로 한다.
- 증거 번호는 처음 저장될 때 부여하고 이후 바뀌지 않는다.
- 댓글, 수정 이력, AI 판정, 캡처 기록은 지우지 않는다. 트리거로 막아 둔다.
- 시각은 UTC ISO 8601 문자열(예: 2026-11-10T11:33:00Z)로 저장한다.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

SCHEMA_VERSION = 1

KST = timezone(timedelta(hours=9), "KST")
# 유튜브 할당량은 태평양 시간 자정에 초기화된다.
QUOTA_TZ = ZoneInfo("America/Los_Angeles")

# 영상 발견 경로. 우선순위 순.
SOURCE_DIRECT, SOURCE_CHANNEL, SOURCE_SEARCH = "direct", "channel", "search"
# 영상 수집 상태
COLLECT_NONE, COLLECT_PARTIAL, COLLECT_FULL = "none", "partial", "full"
# 분류 단계
STAGE_FIRST, STAGE_FINAL = "first", "final"
# 분류와 캡처의 처리 결과
STATUS_OK, STATUS_FAILED = "ok", "failed"

SENTIMENTS = ("긍정", "중립", "부정")
LEGAL_TYPES = ("해당 없음", "단순 비판", "모욕", "명예훼손 의심", "성희롱", "협박", "스토킹성")
REVIEW_STATUSES = ("확인", "제외", "보류")

# upsert_comment 결과
NEW, EDITED, UNCHANGED = "new", "edited", "unchanged"

_SOURCE_PRIORITY = {SOURCE_DIRECT: 0, SOURCE_CHANNEL: 1, SOURCE_SEARCH: 2}

# 유효한 최종 판정이 없거나, 수정되어 다시 분류해야 하는 댓글.
# 2단 분류가 1차만 마치고 멈춘 댓글도 최종 판정이 없으니 여기에 든다.
_UNCLASSIFIED = (
    "(c.reclassify_needed = 1 OR NOT EXISTS (SELECT 1 FROM classifications k "
    "WHERE k.platform = c.platform AND k.comment_id = c.comment_id AND k.is_current = 1 "
    f"AND k.stage = '{STAGE_FINAL}'))"
)
# 지금 조회 불가 상태인 댓글. 다시 보이게 된 댓글은 last_seen_at이 더 뒤다.
_MISSING_NOW = "(missing_detected_at IS NOT NULL AND missing_detected_at >= last_seen_at)"
_CHUNK = 500


def _quoted(values: Sequence[str]) -> str:
    return ", ".join(f"'{v}'" for v in values)


_SCHEMA = f"""
CREATE TABLE videos (
    platform          TEXT NOT NULL,
    video_id          TEXT NOT NULL,
    title             TEXT,
    channel_title     TEXT,
    channel_id        TEXT,
    published_at      TEXT,
    comment_count     INTEGER,
    source            TEXT NOT NULL CHECK (source IN ('direct', 'channel', 'search')),
    source_detail     TEXT,               -- 채널 ID 또는 검색어
    excluded          INTEGER NOT NULL DEFAULT 0,
    collect_status    TEXT NOT NULL DEFAULT 'none'
                      CHECK (collect_status IN ('none', 'partial', 'full')),
    resume_token      TEXT,               -- 이어하기 위치
    skip_reason       TEXT,               -- 댓글이 막힌 영상 등
    first_seen_at     TEXT NOT NULL,
    last_collected_at TEXT,
    PRIMARY KEY (platform, video_id)
);

CREATE TABLE search_cache (
    id          INTEGER PRIMARY KEY,
    platform    TEXT NOT NULL,
    keyword     TEXT NOT NULL,
    sort_order  TEXT NOT NULL,
    params      TEXT NOT NULL,            -- 나머지 검색 조건 (키를 정렬한 JSON)
    results     TEXT NOT NULL,            -- JSON: 요청한 개수와 순위대로 나열한 영상 ID 목록
    searched_at TEXT NOT NULL,
    UNIQUE (platform, keyword, sort_order, params)
);

CREATE TABLE comments (
    evidence_no         INTEGER PRIMARY KEY AUTOINCREMENT,   -- 증거 번호
    platform            TEXT NOT NULL,
    comment_id          TEXT NOT NULL,
    video_id            TEXT NOT NULL,
    parent_id           TEXT,             -- 답글이면 원댓글의 comment_id
    text                TEXT NOT NULL,
    author_name         TEXT,
    author_channel_id   TEXT,
    author_profile_url  TEXT,
    like_count          INTEGER,
    published_at        TEXT,
    updated_at          TEXT,
    first_collected_at  TEXT NOT NULL,
    last_seen_at        TEXT NOT NULL,    -- 마지막으로 조회에 성공한 시각
    missing_detected_at TEXT,             -- 조회 불가를 감지한 시각
    reclassify_needed   INTEGER NOT NULL DEFAULT 0,          -- 수정되어 다시 분류해야 함
    raw_json            TEXT,             -- API 원본 응답
    UNIQUE (platform, comment_id),
    FOREIGN KEY (platform, video_id) REFERENCES videos (platform, video_id)
);
CREATE INDEX idx_comments_video ON comments (platform, video_id);
CREATE INDEX idx_comments_author ON comments (platform, author_channel_id);

CREATE TABLE comment_revisions (
    id           INTEGER PRIMARY KEY,
    platform     TEXT NOT NULL,
    comment_id   TEXT NOT NULL,
    old_text     TEXT NOT NULL,
    new_text     TEXT NOT NULL,
    old_raw_json TEXT,
    detected_at  TEXT NOT NULL,
    FOREIGN KEY (platform, comment_id) REFERENCES comments (platform, comment_id)
);
CREATE INDEX idx_revisions_comment ON comment_revisions (platform, comment_id);

CREATE TABLE classifications (
    id             INTEGER PRIMARY KEY,
    platform       TEXT NOT NULL,
    comment_id     TEXT NOT NULL,
    stage          TEXT NOT NULL CHECK (stage IN ('first', 'final')),
    status         TEXT NOT NULL DEFAULT 'ok' CHECK (status IN ('ok', 'failed')),
    sentiment      TEXT CHECK (sentiment IN ({_quoted(SENTIMENTS)})),
    target         TEXT,                  -- 설정 targets의 name, 또는 타인 / 불명
    legal_type     TEXT CHECK (legal_type IN ({_quoted(LEGAL_TYPES)})),
    severity       INTEGER CHECK (severity BETWEEN 0 AND 5),
    uncertain      INTEGER NOT NULL DEFAULT 0,               -- 1차 분류의 "불확실"
    reason         TEXT,                  -- 판정 근거 또는 실패 사유
    model          TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    classified_at  TEXT NOT NULL,
    is_current     INTEGER NOT NULL DEFAULT 1,
    run_id         INTEGER REFERENCES runs (id),
    CHECK (status = 'failed' OR (sentiment IS NOT NULL AND target IS NOT NULL
           AND legal_type IS NOT NULL AND severity IS NOT NULL)),
    FOREIGN KEY (platform, comment_id) REFERENCES comments (platform, comment_id)
);
-- 댓글 하나에 단계별로 유효한 판정은 하나뿐이다. 이전 판정은 is_current = 0으로 남는다.
CREATE UNIQUE INDEX idx_classifications_current
    ON classifications (platform, comment_id, stage) WHERE is_current = 1;
CREATE INDEX idx_classifications_comment ON classifications (platform, comment_id);

CREATE TABLE captures (
    id               INTEGER PRIMARY KEY,
    platform         TEXT NOT NULL,
    comment_id       TEXT NOT NULL,
    status           TEXT NOT NULL CHECK (status IN ('ok', 'failed')),
    error            TEXT,                -- 실패 사유
    composite_path   TEXT,                -- 머리띠 합성본. 출력 폴더 기준 상대 경로
    composite_sha256 TEXT,
    raw_path         TEXT,                -- 가공하지 않은 원본
    raw_sha256       TEXT,
    captured_at      TEXT NOT NULL,
    run_id           INTEGER REFERENCES runs (id),
    FOREIGN KEY (platform, comment_id) REFERENCES comments (platform, comment_id)
);
CREATE INDEX idx_captures_comment ON captures (platform, comment_id);

CREATE TABLE reviews (
    platform      TEXT NOT NULL,
    comment_id    TEXT NOT NULL,
    review_status TEXT CHECK (review_status IN ({_quoted(REVIEW_STATUSES)})),
    note          TEXT,
    imported_at   TEXT NOT NULL,
    PRIMARY KEY (platform, comment_id),
    FOREIGN KEY (platform, comment_id) REFERENCES comments (platform, comment_id)
);

CREATE TABLE runs (
    id            INTEGER PRIMARY KEY,
    command       TEXT NOT NULL,
    args          TEXT,                   -- 실행 옵션 (JSON)
    started_at    TEXT NOT NULL,
    ended_at      TEXT,
    counts        TEXT,                   -- 단계별 건수 (JSON)
    limits_hit    TEXT,                   -- 상한에 걸린 항목 (JSON)
    quota_used    INTEGER,
    input_tokens  INTEGER,
    output_tokens INTEGER,
    error         TEXT
);

-- 아래 두 테이블은 호출할 때마다 한 줄씩 쌓는다. 실행이 중간에 죽어도 사용량이 남는다.
CREATE TABLE quota_usage (
    id        INTEGER PRIMARY KEY,
    platform  TEXT NOT NULL,
    quota_day TEXT NOT NULL,              -- 할당량 기준일 (태평양 시간)
    api       TEXT NOT NULL,              -- 예: search.list
    units     INTEGER NOT NULL,
    used_at   TEXT NOT NULL,
    run_id    INTEGER REFERENCES runs (id)
);
CREATE INDEX idx_quota_day ON quota_usage (platform, quota_day);

CREATE TABLE token_usage (
    id                          INTEGER PRIMARY KEY,
    model                       TEXT NOT NULL,
    stage                       TEXT,
    input_tokens                INTEGER NOT NULL DEFAULT 0,
    output_tokens               INTEGER NOT NULL DEFAULT 0,
    cache_creation_input_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_input_tokens     INTEGER NOT NULL DEFAULT 0,
    comment_count               INTEGER NOT NULL DEFAULT 0,  -- 이 요청에 담은 댓글 수 (견적 보정용)
    used_at                     TEXT NOT NULL,
    run_id                      INTEGER REFERENCES runs (id)
);

CREATE TRIGGER comments_no_delete BEFORE DELETE ON comments
BEGIN SELECT RAISE(ABORT, '수집한 댓글은 지울 수 없습니다'); END;

CREATE TRIGGER comments_keep_identity
BEFORE UPDATE OF evidence_no, platform, comment_id, first_collected_at ON comments
BEGIN SELECT RAISE(ABORT, '증거 번호와 댓글 식별 정보는 바꿀 수 없습니다'); END;

CREATE TRIGGER comment_revisions_no_delete BEFORE DELETE ON comment_revisions
BEGIN SELECT RAISE(ABORT, '수정 이력은 지울 수 없습니다'); END;

CREATE TRIGGER classifications_no_delete BEFORE DELETE ON classifications
BEGIN SELECT RAISE(ABORT, '판정 이력은 지울 수 없습니다'); END;

CREATE TRIGGER captures_no_delete BEFORE DELETE ON captures
BEGIN SELECT RAISE(ABORT, '캡처 기록은 지울 수 없습니다'); END;
"""

# 버전 N의 스크립트는 버전 N-1의 DB를 N으로 올린다. 스키마를 바꿀 때는 뒤에 덧붙인다.
_MIGRATIONS: tuple[str, ...] = (_SCHEMA,)
assert len(_MIGRATIONS) == SCHEMA_VERSION


class DatabaseError(Exception):
    """DB를 열 수 없거나 버전이 맞지 않을 때."""


def utc_now() -> str:
    return format_utc(datetime.now(timezone.utc))


def format_utc(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_utc(text: str) -> datetime:
    moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment


def to_kst(text: str | None) -> str:
    """DB의 UTC 시각을 화면과 엑셀에 쓰는 한국 시간 문자열로 바꾼다."""
    if not text:
        return ""
    return parse_utc(text).astimezone(KST).strftime("%Y-%m-%d %H:%M:%S")


def quota_day(utc_text: str) -> str:
    """이 시각이 속한 할당량 기준일."""
    return parse_utc(utc_text).astimezone(QUOTA_TZ).date().isoformat()


@dataclass(frozen=True)
class CommentData:
    """수집기가 DB에 넘기는 댓글 한 건."""

    platform: str
    comment_id: str
    video_id: str
    text: str
    parent_id: str | None = None
    author_name: str | None = None
    author_channel_id: str | None = None
    author_profile_url: str | None = None
    like_count: int | None = None
    published_at: str | None = None
    updated_at: str | None = None
    raw: Any = None


class Database:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        # 트랜잭션은 transaction()에서 직접 연다.
        self.conn = sqlite3.connect(str(path), isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA busy_timeout = 5000")
        self._migrate()

    def __enter__(self) -> Database:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self.conn.close()

    def _migrate(self) -> None:
        version = self.conn.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise DatabaseError(
                f"DB({self.path})가 이 프로그램보다 새 버전입니다 "
                f"(DB {version}, 프로그램 {SCHEMA_VERSION}). 프로그램을 최신으로 올려 주세요."
            )
        for target in range(version + 1, SCHEMA_VERSION + 1):
            try:
                self.conn.executescript(
                    f"BEGIN IMMEDIATE;\n{_MIGRATIONS[target - 1]}\n"
                    f"PRAGMA user_version = {target};\nCOMMIT;"
                )
            except BaseException:
                if self.conn.in_transaction:
                    self.conn.execute("ROLLBACK")
                raise

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """묶음 쓰기. 이미 트랜잭션 안이면 바깥 것에 합류한다."""
        if self.conn.in_transaction:
            yield self.conn
            return
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        self.conn.execute("COMMIT")

    def scalar(self, sql: str, params: Sequence[Any] = ()) -> Any:
        row = self.conn.execute(sql, params).fetchone()
        return row[0] if row else None

    # ---- 영상 ----

    def get_video(self, platform: str, video_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM videos WHERE platform = ? AND video_id = ?", (platform, video_id)
        ).fetchone()

    def videos_by_id(self, platform: str) -> dict[str, sqlite3.Row]:
        rows = self.conn.execute("SELECT * FROM videos WHERE platform = ?", (platform,))
        return {row["video_id"]: row for row in rows}

    def upsert_video(
        self,
        *,
        platform: str,
        video_id: str,
        title: str | None,
        channel_title: str | None,
        channel_id: str | None,
        published_at: str | None,
        comment_count: int | None,
        source: str,
        source_detail: str | None = None,
        now: str | None = None,
    ) -> bool:
        """대상 영상을 등록한다. 새로 등록했으면 True.

        이미 있는 영상은 제목 같은 표시 정보만 갱신한다. 댓글 수는 마지막 수집 때의 값을
        견적의 기준으로 쓰므로 여기서 바꾸지 않는다. 발견 경로는 더 높은 순위로만 바뀐다.
        """
        existing = self.get_video(platform, video_id)
        if existing is None:
            self.conn.execute(
                """
                INSERT INTO videos (platform, video_id, title, channel_title, channel_id,
                    published_at, comment_count, source, source_detail, first_seen_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (platform, video_id, title, channel_title, channel_id, published_at,
                 comment_count, source, source_detail, now or utc_now()),
            )
            return True
        if _SOURCE_PRIORITY[source] >= _SOURCE_PRIORITY[existing["source"]]:
            source, source_detail = existing["source"], existing["source_detail"]
        self.conn.execute(
            """
            UPDATE videos SET title = ?, channel_title = ?, channel_id = ?, published_at = ?,
                source = ?, source_detail = ?
            WHERE platform = ? AND video_id = ?
            """,
            (title, channel_title, channel_id, published_at, source, source_detail,
             platform, video_id),
        )
        return False

    def sync_excluded(self, platform: str, excluded_ids: Sequence[str]) -> None:
        """설정의 제외 목록을 영상의 제외 여부에 반영한다. 목록에서 빠지면 다시 대상이 된다."""
        with self.transaction() as conn:
            conn.execute("UPDATE videos SET excluded = 0 WHERE platform = ?", (platform,))
            conn.executemany(
                "UPDATE videos SET excluded = 1 WHERE platform = ? AND video_id = ?",
                [(platform, video_id) for video_id in excluded_ids],
            )

    def set_video_progress(
        self, platform: str, video_id: str, *, collect_status: str, resume_token: str | None
    ) -> None:
        self.conn.execute(
            "UPDATE videos SET collect_status = ?, resume_token = ? "
            "WHERE platform = ? AND video_id = ?",
            (collect_status, resume_token, platform, video_id),
        )

    def finish_video(
        self, platform: str, video_id: str, *, comment_count: int | None, now: str | None = None
    ) -> None:
        """이번 실행에서 이 영상의 수집을 끝까지(또는 상한까지) 마쳤음을 기록한다."""
        self.conn.execute(
            "UPDATE videos SET last_collected_at = ?, comment_count = COALESCE(?, comment_count), "
            "skip_reason = NULL WHERE platform = ? AND video_id = ?",
            (now or utc_now(), comment_count, platform, video_id),
        )

    def set_video_skip(self, platform: str, video_id: str, reason: str) -> None:
        self.conn.execute(
            "UPDATE videos SET skip_reason = ? WHERE platform = ? AND video_id = ?",
            (reason, platform, video_id),
        )

    # ---- 검색 결과 재사용 ----

    def get_search_cache(
        self, platform: str, keyword: str, sort_order: str, params: Mapping[str, Any]
    ) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT results, searched_at FROM search_cache "
            "WHERE platform = ? AND keyword = ? AND sort_order = ? AND params = ?",
            (platform, keyword, sort_order, json.dumps(dict(params), sort_keys=True)),
        ).fetchone()
        if row is None:
            return None
        return {**json.loads(row["results"]), "searched_at": row["searched_at"]}

    def put_search_cache(
        self,
        platform: str,
        keyword: str,
        sort_order: str,
        params: Mapping[str, Any],
        *,
        requested: int,
        video_ids: Sequence[str],
        now: str | None = None,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO search_cache (platform, keyword, sort_order, params, results, searched_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (platform, keyword, sort_order, params)
            DO UPDATE SET results = excluded.results, searched_at = excluded.searched_at
            """,
            (
                platform, keyword, sort_order, json.dumps(dict(params), sort_keys=True),
                json.dumps({"requested": requested, "video_ids": list(video_ids)}),
                now or utc_now(),
            ),
        )

    # ---- 댓글 ----

    def count_comments(self, platform: str, video_id: str) -> int:
        return self.scalar(
            "SELECT COUNT(*) FROM comments WHERE platform = ? AND video_id = ?",
            (platform, video_id),
        )

    def comment_counts(self, platform: str) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT video_id, COUNT(*) AS n FROM comments WHERE platform = ? GROUP BY video_id",
            (platform,),
        )
        return {row["video_id"]: row["n"] for row in rows}

    def existing_comment_ids(self, platform: str, comment_ids: Sequence[str]) -> set[str]:
        found: set[str] = set()
        ids = list(comment_ids)
        for start in range(0, len(ids), _CHUNK):
            chunk = ids[start:start + _CHUNK]
            marks = ", ".join("?" for _ in chunk)
            found.update(
                row[0] for row in self.conn.execute(
                    f"SELECT comment_id FROM comments WHERE platform = ? "
                    f"AND comment_id IN ({marks})",
                    (platform, *chunk),
                )
            )
        return found

    def mark_missing_ids(
        self, platform: str, comment_ids: Sequence[str], *, now: str | None = None
    ) -> int:
        """조회되지 않은 댓글에 감지 시각을 남긴다. 이미 조회 불가인 댓글은 처음 감지한 시각을 지킨다."""
        now = now or utc_now()
        marked = 0
        ids = list(comment_ids)
        with self.transaction() as conn:
            for start in range(0, len(ids), _CHUNK):
                chunk = ids[start:start + _CHUNK]
                marks = ", ".join("?" for _ in chunk)
                marked += conn.execute(
                    f"UPDATE comments SET missing_detected_at = ? WHERE platform = ? "
                    f"AND comment_id IN ({marks}) AND NOT {_MISSING_NOW}",
                    (now, platform, *chunk),
                ).rowcount
        return marked

    def mark_missing_except(
        self, platform: str, video_id: str, seen_ids: set[str], *, now: str | None = None
    ) -> int:
        """전체 재확인에서 보이지 않은 이 영상의 댓글을 조회 불가로 표시한다."""
        stored = self.conn.execute(
            f"SELECT comment_id FROM comments WHERE platform = ? AND video_id = ? "
            f"AND NOT {_MISSING_NOW}",
            (platform, video_id),
        ).fetchall()
        gone = [row[0] for row in stored if row[0] not in seen_ids]
        return self.mark_missing_ids(platform, gone, now=now)

    def negative_comments(self, platform: str) -> list[sqlite3.Row]:
        """유효한 최종 판정이 부정인 댓글. 매 실행마다 ID로 다시 조회할 대상이다."""
        return self.conn.execute(
            "SELECT c.comment_id, c.video_id, c.parent_id FROM comments c "
            "JOIN classifications k ON k.platform = c.platform AND k.comment_id = c.comment_id "
            f"AND k.is_current = 1 AND k.stage = '{STAGE_FINAL}' AND k.status = '{STATUS_OK}' "
            "WHERE c.platform = ? AND k.sentiment = '부정' ORDER BY c.evidence_no",
            (platform,),
        ).fetchall()

    def unclassified_count(self) -> int:
        return self.scalar(f"SELECT COUNT(*) FROM comments c WHERE {_UNCLASSIFIED}")

    def capture_rate(self, *, min_severity: int, targets: Sequence[str]) -> float | None:
        """최종 판정을 받은 댓글 중 캡처 대상이 된 비율. 판정이 아직 없으면 None."""
        marks = ", ".join("?" for _ in targets)
        row = self.conn.execute(
            f"SELECT COUNT(*) AS total, COALESCE(SUM(sentiment = '부정' AND severity >= ? "
            f"AND target IN ({marks})), 0) AS hits FROM classifications "
            f"WHERE is_current = 1 AND stage = '{STAGE_FINAL}' AND status = '{STATUS_OK}'",
            (min_severity, *targets),
        ).fetchone()
        return row["hits"] / row["total"] if row["total"] else None

    # ---- 캡처 ----

    @staticmethod
    def _capture_from(targets: Sequence[str]) -> str:
        """캡처 대상(DESIGN.md 11장)을 고르는 FROM과 WHERE. 매개변수는 (min_severity, *targets)다."""
        marks = ", ".join("?" for _ in targets)
        return (
            "FROM comments c "
            "JOIN videos v ON v.platform = c.platform AND v.video_id = c.video_id "
            "JOIN classifications k ON k.platform = c.platform AND k.comment_id = c.comment_id "
            f"AND k.is_current = 1 AND k.stage = '{STAGE_FINAL}' AND k.status = '{STATUS_OK}' "
            f"WHERE k.sentiment = '부정' AND k.severity >= ? AND k.target IN ({marks}) "
            "AND NOT EXISTS (SELECT 1 FROM captures p WHERE p.platform = c.platform "
            f"AND p.comment_id = c.comment_id AND p.status = '{STATUS_OK}')"
        )

    def count_to_capture(self, *, min_severity: int, targets: Sequence[str]) -> int:
        return self.scalar(
            f"SELECT COUNT(*) {self._capture_from(targets)}", (min_severity, *targets)
        )

    def comments_to_capture(
        self, *, min_severity: int, targets: Sequence[str], limit: int | None = None
    ) -> list[sqlite3.Row]:
        """캡처할 댓글. 심각도가 높은 것부터, 같으면 실패한 적이 적은 것부터 준다.

        실패한 댓글도 다음 실행에서 다시 시도하지만, 계속 실패하는 댓글이 상한을 다 쓰지 않게 뒤로 보낸다.
        """
        sql = f"""
            SELECT c.evidence_no, c.platform, c.comment_id, c.video_id, c.parent_id, c.text,
                   c.author_name, c.author_channel_id, c.published_at,
                   v.title AS video_title, v.channel_title AS video_channel,
                   k.severity, k.target,
                   (SELECT COUNT(*) FROM captures f WHERE f.platform = c.platform
                        AND f.comment_id = c.comment_id AND f.status = '{STATUS_FAILED}') AS failures
            {self._capture_from(targets)}
            ORDER BY k.severity DESC, failures, c.evidence_no
        """
        params: list[Any] = [min_severity, *targets]
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        return self.conn.execute(sql, params).fetchall()

    def save_capture(
        self,
        platform: str,
        comment_id: str,
        *,
        captured_at: str,
        error: str | None = None,
        composite_path: str | None = None,
        composite_sha256: str | None = None,
        raw_path: str | None = None,
        raw_sha256: str | None = None,
        run_id: int | None = None,
    ) -> None:
        """캡처 한 번의 결과. error가 있으면 실패 기록이다. 경로는 출력 폴더 기준 상대 경로다."""
        self.conn.execute(
            "INSERT INTO captures (platform, comment_id, status, error, composite_path, "
            "composite_sha256, raw_path, raw_sha256, captured_at, run_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                platform, comment_id, STATUS_FAILED if error else STATUS_OK, error,
                composite_path, composite_sha256, raw_path, raw_sha256, captured_at, run_id,
            ),
        )

    def export_rows(self, *, only_negative: bool = False) -> Iterator[sqlite3.Row]:
        """엑셀 출력용. 댓글 한 건에 영상, 원댓글, 유효한 최종 판정을 붙여 증거 번호 순으로 준다."""
        where = "WHERE k.sentiment = '부정'" if only_negative else ""
        return self.conn.execute(
            f"""
            SELECT c.evidence_no, c.platform, c.comment_id, c.video_id, c.parent_id, c.text,
                   c.author_name, c.author_channel_id, c.author_profile_url, c.like_count,
                   c.published_at, c.updated_at, c.first_collected_at,
                   CASE WHEN c.missing_detected_at >= c.last_seen_at
                        THEN c.missing_detected_at END AS missing_at,
                   (SELECT COUNT(*) FROM comment_revisions r WHERE r.platform = c.platform
                        AND r.comment_id = c.comment_id) AS revision_count,
                   v.title AS video_title, v.channel_title AS video_channel,
                   p.text AS parent_text,
                   k.sentiment, k.target, k.legal_type, k.severity, k.reason,
                   cap.composite_path AS capture_path, cap.composite_sha256 AS capture_sha256,
                   rv.review_status, rv.note AS review_note
            FROM comments c
            JOIN videos v ON v.platform = c.platform AND v.video_id = c.video_id
            LEFT JOIN comments p ON p.platform = c.platform AND p.comment_id = c.parent_id
            LEFT JOIN classifications k ON k.platform = c.platform
                AND k.comment_id = c.comment_id AND k.is_current = 1
                AND k.stage = '{STAGE_FINAL}' AND k.status = '{STATUS_OK}'
            LEFT JOIN captures cap ON cap.id = (
                SELECT MAX(x.id) FROM captures x WHERE x.platform = c.platform
                    AND x.comment_id = c.comment_id AND x.status = '{STATUS_OK}')
            LEFT JOIN reviews rv ON rv.platform = c.platform AND rv.comment_id = c.comment_id
            {where}
            ORDER BY c.evidence_no
            """
        )

    def negative_count(self) -> int:
        return self.scalar(
            "SELECT COUNT(*) FROM classifications WHERE is_current = 1 "
            f"AND stage = '{STAGE_FINAL}' AND status = '{STATUS_OK}' AND sentiment = '부정'"
        )

    def judged_rows(self) -> Iterator[sqlite3.Row]:
        """랭킹과 통계용. 댓글마다 유효한 최종 판정, 검토 상태, 캡처와 조회 불가 여부를 붙인다."""
        return self.conn.execute(
            f"""
            SELECT c.platform, c.comment_id, c.video_id, c.author_name, c.author_channel_id,
                   c.author_profile_url, c.published_at, c.first_collected_at,
                   {_MISSING_NOW} AS missing,
                   k.sentiment, k.target, k.legal_type, k.severity,
                   EXISTS (SELECT 1 FROM captures x WHERE x.platform = c.platform
                       AND x.comment_id = c.comment_id AND x.status = '{STATUS_OK}') AS captured,
                   EXISTS (SELECT 1 FROM classifications f WHERE f.platform = c.platform
                       AND f.comment_id = c.comment_id AND f.is_current = 1
                       AND f.status = '{STATUS_FAILED}') AS failed,
                   rv.review_status
            FROM comments c
            LEFT JOIN classifications k ON k.platform = c.platform
                AND k.comment_id = c.comment_id AND k.is_current = 1
                AND k.stage = '{STAGE_FINAL}' AND k.status = '{STATUS_OK}'
            LEFT JOIN reviews rv ON rv.platform = c.platform AND rv.comment_id = c.comment_id
            ORDER BY c.evidence_no
            """
        )

    def all_videos(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM videos ORDER BY first_seen_at, video_id").fetchall()

    # ---- 분류 ----

    def _classify_where(
        self, restale: tuple[str, Sequence[str]] | None
    ) -> tuple[str, list[Any]]:
        """분류 대상 조건. restale=(프롬프트 버전, 유효한 대상 값)이면 다시 분류할 댓글도 포함한다.

        다시 분류할 댓글: 분류에 실패했거나, 판정의 프롬프트 버전이 지금과 다르거나,
        판정의 대상이 지금 설정에 없는 이름인 댓글.
        """
        if restale is None:
            return _UNCLASSIFIED, []
        prompt_version, target_values = restale
        marks = ", ".join("?" for _ in target_values)
        stale = (
            "EXISTS (SELECT 1 FROM classifications k WHERE k.platform = c.platform "
            f"AND k.comment_id = c.comment_id AND k.is_current = 1 AND k.stage = '{STAGE_FINAL}' "
            f"AND (k.status = '{STATUS_FAILED}' OR k.prompt_version != ? "
            f"OR k.target NOT IN ({marks})))"
        )
        return f"({_UNCLASSIFIED} OR {stale})", [prompt_version, *target_values]

    def count_to_classify(self, *, restale: tuple[str, Sequence[str]] | None = None) -> int:
        where, params = self._classify_where(restale)
        return self.scalar(f"SELECT COUNT(*) FROM comments c WHERE {where}", params)

    def comments_to_classify(
        self, *, limit: int | None = None, restale: tuple[str, Sequence[str]] | None = None
    ) -> list[sqlite3.Row]:
        """분류할 댓글을 최신 작성 순으로, 판정에 쓸 맥락(영상 제목, 채널, 원댓글)과 함께 준다."""
        where, params = self._classify_where(restale)
        return self._with_context(where, params, limit)

    def comments_to_compare(self, limit: int) -> list[sqlite3.Row]:
        """모델 비교에 쓸 표본. 분류 여부와 무관하게 최신 댓글부터 고른다."""
        return self._with_context("1 = 1", [], limit)

    def _with_context(
        self, where: str, params: Sequence[Any], limit: int | None
    ) -> list[sqlite3.Row]:
        return self.conn.execute(
            f"""
            SELECT c.evidence_no, c.platform, c.comment_id, c.video_id, c.text,
                   v.title AS video_title, v.channel_title AS video_channel,
                   p.text AS parent_text
            FROM comments c
            JOIN videos v ON v.platform = c.platform AND v.video_id = c.video_id
            LEFT JOIN comments p ON p.platform = c.platform AND p.comment_id = c.parent_id
            WHERE {where}
            ORDER BY c.published_at DESC, c.evidence_no DESC
            LIMIT ?
            """,
            (*params, -1 if limit is None else limit),
        ).fetchall()

    def save_classification(
        self,
        platform: str,
        comment_id: str,
        *,
        model: str,
        prompt_version: str,
        status: str = STATUS_OK,
        sentiment: str | None = None,
        target: str | None = None,
        legal_type: str | None = None,
        severity: int | None = None,
        reason: str | None = None,
        stage: str = STAGE_FINAL,
        run_id: int | None = None,
        now: str | None = None,
    ) -> bool:
        """판정을 유효한 판정으로 저장한다. 이전 판정은 지우지 않고 이력(is_current = 0)으로 남긴다.

        실패 기록은 유효한 정상 판정이 이미 있으면 저장하지 않고 False를 돌려준다.
        다시 분류하다 실패했다고 멀쩡한 이전 판정을 잃으면 안 되기 때문이다.
        """
        key = (platform, comment_id, stage)
        with self.transaction() as conn:
            if status == STATUS_FAILED and conn.execute(
                "SELECT 1 FROM classifications WHERE platform = ? AND comment_id = ? "
                f"AND stage = ? AND is_current = 1 AND status = '{STATUS_OK}'", key,
            ).fetchone():
                return False
            conn.execute(
                "UPDATE classifications SET is_current = 0 WHERE platform = ? "
                "AND comment_id = ? AND stage = ? AND is_current = 1", key,
            )
            conn.execute(
                """
                INSERT INTO classifications (platform, comment_id, stage, status, sentiment,
                    target, legal_type, severity, reason, model, prompt_version, classified_at,
                    run_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (*key, status, sentiment, target, legal_type, severity, reason, model,
                 prompt_version, now or utc_now(), run_id),
            )
            if stage == STAGE_FINAL:
                # 1차 판정만으로는 다시 분류할 일이 끝나지 않는다.
                conn.execute(
                    "UPDATE comments SET reclassify_needed = 0 "
                    "WHERE platform = ? AND comment_id = ?",
                    (platform, comment_id),
                )
        return True

    # ---- 사람의 검토 결과 ----

    def reviews(self) -> dict[tuple[str, str], tuple[str | None, str | None]]:
        rows = self.conn.execute("SELECT platform, comment_id, review_status, note FROM reviews")
        return {(r["platform"], r["comment_id"]): (r["review_status"], r["note"]) for r in rows}

    def save_reviews(
        self, rows: Sequence[tuple[str, str, str | None, str | None]], *, now: str | None = None
    ) -> int:
        """엑셀에서 읽은 (플랫폼, 댓글 ID, 검토 상태, 비고)를 저장한다. DB에 없는 댓글은 건너뛴다."""
        now = now or utc_now()
        saved = 0
        with self.transaction() as conn:
            for platform, comment_id, status, note in rows:
                saved += conn.execute(
                    """
                    INSERT INTO reviews (platform, comment_id, review_status, note, imported_at)
                    SELECT ?1, ?2, ?3, ?4, ?5 WHERE EXISTS (
                        SELECT 1 FROM comments WHERE platform = ?1 AND comment_id = ?2)
                    ON CONFLICT (platform, comment_id) DO UPDATE SET
                        review_status = excluded.review_status, note = excluded.note,
                        imported_at = excluded.imported_at
                    """,
                    (platform, comment_id, status, note, now),
                ).rowcount
        return saved

    def upsert_comment(self, comment: CommentData, *, now: str | None = None) -> str:
        """댓글 한 건을 저장하고 NEW, EDITED, UNCHANGED 중 하나를 돌려준다.

        이미 있는 댓글이면 증거 번호와 최초 수집 시각은 그대로 둔다.
        본문이 달라졌으면 이전 본문을 수정 이력에 남기고 재분류 대상으로 표시한다.
        조회 불가 감지 시각은 여기서 지우지 않는다. 다시 보이게 된 댓글은
        last_seen_at이 missing_detected_at보다 뒤라는 것으로 구분한다.
        """
        now = now or utc_now()
        raw_json = (
            None if comment.raw is None else json.dumps(comment.raw, ensure_ascii=False)
        )
        key = (comment.platform, comment.comment_id)
        with self.transaction() as conn:
            existing = conn.execute(
                "SELECT text, raw_json FROM comments WHERE platform = ? AND comment_id = ?", key
            ).fetchone()
            if existing is None:
                conn.execute(
                    """
                    INSERT INTO comments (
                        platform, comment_id, video_id, parent_id, text, author_name,
                        author_channel_id, author_profile_url, like_count, published_at,
                        updated_at, first_collected_at, last_seen_at, raw_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        *key, comment.video_id, comment.parent_id, comment.text,
                        comment.author_name, comment.author_channel_id,
                        comment.author_profile_url, comment.like_count, comment.published_at,
                        comment.updated_at, now, now, raw_json,
                    ),
                )
                return NEW

            edited = existing["text"] != comment.text
            if edited:
                conn.execute(
                    """
                    INSERT INTO comment_revisions
                        (platform, comment_id, old_text, new_text, old_raw_json, detected_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (*key, existing["text"], comment.text, existing["raw_json"], now),
                )
            conn.execute(
                """
                UPDATE comments SET
                    text = ?, author_name = ?, author_channel_id = ?, author_profile_url = ?,
                    like_count = ?, updated_at = ?, last_seen_at = ?, raw_json = ?,
                    reclassify_needed = MAX(reclassify_needed, ?)
                WHERE platform = ? AND comment_id = ?
                """,
                (
                    comment.text, comment.author_name, comment.author_channel_id,
                    comment.author_profile_url, comment.like_count, comment.updated_at, now,
                    raw_json, int(edited), *key,
                ),
            )
            return EDITED if edited else UNCHANGED

    # ---- 실행 기록과 사용량 ----

    def start_run(
        self, command: str, args: Mapping[str, Any] | None = None, *, now: str | None = None
    ) -> int:
        cursor = self.conn.execute(
            "INSERT INTO runs (command, args, started_at) VALUES (?, ?, ?)",
            (command, json.dumps(dict(args or {}), ensure_ascii=False), now or utc_now()),
        )
        return cursor.lastrowid

    def finish_run(
        self,
        run_id: int,
        *,
        counts: Mapping[str, Any] | None = None,
        limits_hit: Sequence[str] | None = None,
        error: str | None = None,
        now: str | None = None,
    ) -> None:
        """실행을 닫는다. 할당량과 토큰 합계는 쌓인 사용 기록에서 계산해 넣는다."""
        self.conn.execute(
            """
            UPDATE runs SET
                ended_at = ?2, counts = ?3, limits_hit = ?4, error = ?5,
                quota_used = (SELECT COALESCE(SUM(units), 0) FROM quota_usage WHERE run_id = ?1),
                input_tokens = (
                    SELECT COALESCE(SUM(input_tokens + cache_creation_input_tokens
                                        + cache_read_input_tokens), 0)
                    FROM token_usage WHERE run_id = ?1),
                output_tokens = (
                    SELECT COALESCE(SUM(output_tokens), 0) FROM token_usage WHERE run_id = ?1)
            WHERE id = ?1
            """,
            (
                run_id,
                now or utc_now(),
                json.dumps(dict(counts or {}), ensure_ascii=False),
                json.dumps(list(limits_hit or []), ensure_ascii=False),
                error,
            ),
        )

    def record_quota(
        self,
        api: str,
        units: int,
        *,
        platform: str = "youtube",
        run_id: int | None = None,
        now: str | None = None,
    ) -> None:
        now = now or utc_now()
        self.conn.execute(
            "INSERT INTO quota_usage (platform, quota_day, api, units, used_at, run_id) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (platform, quota_day(now), api, units, now, run_id),
        )

    def quota_used_today(self, *, platform: str = "youtube", now: str | None = None) -> int:
        return self.scalar(
            "SELECT COALESCE(SUM(units), 0) FROM quota_usage WHERE platform = ? AND quota_day = ?",
            (platform, quota_day(now or utc_now())),
        )

    def record_tokens(
        self,
        model: str,
        *,
        stage: str | None = None,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_creation_input_tokens: int = 0,
        cache_read_input_tokens: int = 0,
        comment_count: int = 0,
        run_id: int | None = None,
        now: str | None = None,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO token_usage (
                model, stage, input_tokens, output_tokens, cache_creation_input_tokens,
                cache_read_input_tokens, comment_count, used_at, run_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                model, stage, input_tokens, output_tokens, cache_creation_input_tokens,
                cache_read_input_tokens, comment_count, now or utc_now(), run_id,
            ),
        )

    def token_usage_by_model(self, *, run_id: int | None = None) -> list[sqlite3.Row]:
        """모델별 토큰 합계. 실제 비용 계산과 견적 보정에 쓴다."""
        where, params = ("WHERE run_id = ?", (run_id,)) if run_id is not None else ("", ())
        return self.conn.execute(
            f"""
            SELECT model, COUNT(*) AS requests,
                   SUM(input_tokens) AS input_tokens, SUM(output_tokens) AS output_tokens,
                   SUM(cache_creation_input_tokens) AS cache_creation_input_tokens,
                   SUM(cache_read_input_tokens) AS cache_read_input_tokens,
                   SUM(comment_count) AS comment_count
            FROM token_usage {where} GROUP BY model ORDER BY model
            """,
            params,
        ).fetchall()

    # ---- 현황 ----

    def status_summary(
        self,
        *,
        capture_min_severity: int,
        capture_targets: Sequence[str],
        now: str | None = None,
    ) -> dict[str, Any]:
        """`status` 명령이 보여줄 숫자들."""
        now = now or utc_now()
        videos = {
            row["collect_status"]: row["n"]
            for row in self.conn.execute(
                "SELECT collect_status, COUNT(*) AS n FROM videos "
                "WHERE excluded = 0 AND skip_reason IS NULL GROUP BY collect_status"
            )
        }
        marks = ", ".join("?" for _ in capture_targets)
        # 부정 여부와 캡처 대상은 유효한 최종 판정을 기준으로 한다.
        final_negative = (
            "FROM comments c JOIN classifications k "
            "ON k.platform = c.platform AND k.comment_id = c.comment_id "
            f"AND k.is_current = 1 AND k.stage = '{STAGE_FINAL}' AND k.status = '{STATUS_OK}' "
            "WHERE k.sentiment = '부정'"
        )
        tokens = self.conn.execute(
            "SELECT COALESCE(SUM(input_tokens + cache_creation_input_tokens "
            "+ cache_read_input_tokens), 0) AS input, "
            "COALESCE(SUM(output_tokens), 0) AS output FROM token_usage"
        ).fetchone()
        last_run = self.conn.execute(
            "SELECT command, started_at, ended_at, error FROM runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return {
            "videos_full": videos.get(COLLECT_FULL, 0),
            "videos_partial": videos.get(COLLECT_PARTIAL, 0),
            "videos_none": videos.get(COLLECT_NONE, 0),
            "videos_excluded": self.scalar("SELECT COUNT(*) FROM videos WHERE excluded = 1"),
            "videos_skipped": self.scalar(
                "SELECT COUNT(*) FROM videos WHERE excluded = 0 AND skip_reason IS NOT NULL"
            ),
            "comments": self.scalar("SELECT COUNT(*) FROM comments"),
            "comments_missing": self.scalar(
                f"SELECT COUNT(*) FROM comments WHERE {_MISSING_NOW}"
            ),
            "unclassified": self.unclassified_count(),
            "videos_resumable": self.scalar(
                "SELECT COUNT(*) FROM videos WHERE excluded = 0 AND resume_token IS NOT NULL"
            ),
            "classify_failed": self.scalar(
                "SELECT COUNT(DISTINCT platform || ':' || comment_id) FROM classifications "
                f"WHERE is_current = 1 AND status = '{STATUS_FAILED}'"
            ),
            "negative": self.scalar(f"SELECT COUNT(*) {final_negative}"),
            "captures_ok": self.scalar(
                "SELECT COUNT(DISTINCT platform || ':' || comment_id) FROM captures "
                f"WHERE status = '{STATUS_OK}'"
            ),
            "captures_pending": self.scalar(
                f"SELECT COUNT(*) {final_negative} AND k.severity >= ? AND k.target IN ({marks}) "
                "AND NOT EXISTS (SELECT 1 FROM captures p WHERE p.platform = c.platform "
                f"AND p.comment_id = c.comment_id AND p.status = '{STATUS_OK}')",
                (capture_min_severity, *capture_targets),
            ),
            "quota_day": quota_day(now),
            "quota_used_today": self.quota_used_today(now=now),
            "input_tokens": tokens["input"],
            "output_tokens": tokens["output"],
            "last_run": dict(last_run) if last_run else None,
        }
