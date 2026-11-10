import json
import sqlite3
from dataclasses import replace

import pytest

from comment_monitor import db as dbm
from comment_monitor.db import CommentData, Database, DatabaseError

from .conftest import VIDEO_ID

T1 = "2026-11-10T01:00:00Z"
T2 = "2026-11-11T01:00:00Z"


def comment(comment_id="c1", text="본문", **kwargs):
    return CommentData(
        platform="youtube", comment_id=comment_id, video_id=VIDEO_ID, text=text,
        author_name="작성자", author_channel_id="UCauthor", like_count=1,
        raw={"id": comment_id, "text": text}, **kwargs,
    )


def row(db, comment_id="c1"):
    return db.conn.execute(
        "SELECT * FROM comments WHERE comment_id = ?", (comment_id,)
    ).fetchone()


def classify(db, comment_id, *, sentiment="부정", target="홍길동", severity=3, stage="final"):
    db.conn.execute(
        "INSERT INTO classifications (platform, comment_id, stage, sentiment, target, "
        "legal_type, severity, model, prompt_version, classified_at) "
        "VALUES ('youtube', ?, ?, ?, ?, '모욕', ?, 'm', 'v1', ?)",
        (comment_id, stage, sentiment, target, severity, T1),
    )


def test_schema_is_created_and_reopening_keeps_data(tmp_path):
    path = tmp_path / "nested" / "monitor.db"
    with Database(path) as first:
        assert first.scalar("PRAGMA user_version") == dbm.SCHEMA_VERSION
        first.start_run("status")
    with Database(path) as second:
        assert second.scalar("SELECT COUNT(*) FROM runs") == 1


def test_newer_database_is_refused(tmp_path):
    path = tmp_path / "monitor.db"
    with Database(path) as database:
        database.conn.execute(f"PRAGMA user_version = {dbm.SCHEMA_VERSION + 1}")
    with pytest.raises(DatabaseError, match="새 버전"):
        Database(path)


def test_same_comment_twice_is_stored_once(db):
    assert db.upsert_comment(comment(), now=T1) == dbm.NEW
    assert db.upsert_comment(comment(), now=T2) == dbm.UNCHANGED
    assert db.scalar("SELECT COUNT(*) FROM comments") == 1
    assert db.scalar("SELECT COUNT(*) FROM comment_revisions") == 0
    stored = row(db)
    assert stored["first_collected_at"] == T1
    assert stored["last_seen_at"] == T2
    assert stored["reclassify_needed"] == 0


def test_evidence_numbers_are_sequential_and_stable(db):
    for comment_id in ("c1", "c2", "c3"):
        db.upsert_comment(comment(comment_id))
    db.upsert_comment(comment("c1", text="고친 본문"))
    db.upsert_comment(comment("c4"))
    numbers = dict(db.conn.execute("SELECT comment_id, evidence_no FROM comments").fetchall())
    assert numbers == {"c1": 1, "c2": 2, "c3": 3, "c4": 4}


def test_edit_keeps_previous_text_and_marks_for_reclassification(db):
    db.upsert_comment(comment(text="처음 본문"), now=T1)
    result = db.upsert_comment(
        replace(comment(text="고친 본문"), like_count=7, author_name="새 이름"), now=T2
    )
    assert result == dbm.EDITED
    stored = row(db)
    assert stored["text"] == "고친 본문"
    assert stored["like_count"] == 7
    assert stored["author_name"] == "새 이름"
    assert stored["first_collected_at"] == T1
    assert stored["reclassify_needed"] == 1
    revision = db.conn.execute("SELECT * FROM comment_revisions").fetchone()
    assert (revision["old_text"], revision["new_text"]) == ("처음 본문", "고친 본문")
    assert revision["detected_at"] == T2
    assert json.loads(revision["old_raw_json"])["text"] == "처음 본문"
    # 같은 수정본을 다시 받아도 이력이 늘지 않고 재분류 표시는 유지된다.
    assert db.upsert_comment(comment(text="고친 본문")) == dbm.UNCHANGED
    assert db.scalar("SELECT COUNT(*) FROM comment_revisions") == 1
    assert row(db)["reclassify_needed"] == 1


def test_same_comment_id_on_other_platform_is_a_different_comment(db):
    db.conn.execute(
        "INSERT INTO videos (platform, video_id, source, first_seen_at) "
        "VALUES ('instagram', ?, 'direct', ?)",
        (VIDEO_ID, T1),
    )
    db.upsert_comment(comment())
    assert db.upsert_comment(replace(comment(), platform="instagram")) == dbm.NEW
    assert db.scalar("SELECT COUNT(*) FROM comments") == 2


def test_comment_requires_known_video(db):
    with pytest.raises(sqlite3.IntegrityError):
        db.upsert_comment(replace(comment(), video_id="unknownvid0"))


def test_collected_records_cannot_be_deleted_or_renumbered(db):
    db.upsert_comment(comment(text="처음"))
    db.upsert_comment(comment(text="수정"))
    classify(db, "c1")
    db.conn.execute(
        "INSERT INTO captures (platform, comment_id, status, captured_at) "
        "VALUES ('youtube', 'c1', 'ok', ?)",
        (T1,),
    )
    for table in ("comments", "comment_revisions", "classifications", "captures"):
        with pytest.raises(sqlite3.IntegrityError, match="지울 수 없습니다"):
            db.conn.execute(f"DELETE FROM {table}")
    with pytest.raises(sqlite3.IntegrityError, match="바꿀 수 없습니다"):
        db.conn.execute("UPDATE comments SET evidence_no = 99")
    with pytest.raises(sqlite3.IntegrityError, match="바꿀 수 없습니다"):
        db.conn.execute("UPDATE comments SET comment_id = 'other'")


def test_only_one_current_classification_per_stage(db):
    db.upsert_comment(comment())
    classify(db, "c1")
    with pytest.raises(sqlite3.IntegrityError):
        classify(db, "c1")
    # 이전 판정을 이력으로 돌리면 새 판정을 넣을 수 있고, 1차 판정은 따로 둘 수 있다.
    db.conn.execute("UPDATE classifications SET is_current = 0")
    classify(db, "c1")
    classify(db, "c1", stage="first")
    assert db.scalar("SELECT COUNT(*) FROM classifications") == 3


def test_classification_values_are_checked(db):
    db.upsert_comment(comment())
    with pytest.raises(sqlite3.IntegrityError):
        classify(db, "c1", sentiment="나쁨")
    with pytest.raises(sqlite3.IntegrityError):
        classify(db, "c1", severity=6)
    # 분류 실패는 판정 값 없이 기록할 수 있다.
    db.conn.execute(
        "INSERT INTO classifications (platform, comment_id, stage, status, reason, model, "
        "prompt_version, classified_at) VALUES ('youtube', 'c1', 'final', 'failed', '거부', "
        "'m', 'v1', ?)",
        (T1,),
    )


def test_transaction_rolls_back_as_a_whole(db):
    with pytest.raises(RuntimeError):
        with db.transaction():
            db.upsert_comment(comment("c1"))
            db.upsert_comment(comment("c2"))
            raise RuntimeError("중간에 끊김")
    assert db.scalar("SELECT COUNT(*) FROM comments") == 0


def test_quota_is_counted_per_pacific_day(db):
    # 태평양 시간으로 11월 9일 23시와 11월 10일 0시 30분
    late = "2026-11-10T07:00:00Z"
    after_reset = "2026-11-10T08:30:00Z"
    assert dbm.quota_day(late) == "2026-11-09"
    assert dbm.quota_day(after_reset) == "2026-11-10"
    db.record_quota("search.list", 100, now=late)
    db.record_quota("commentThreads.list", 1, now=after_reset)
    db.record_quota("videos.list", 1, now=after_reset)
    assert db.quota_used_today(now=late) == 100
    assert db.quota_used_today(now=after_reset) == 2


def test_run_totals_come_from_usage_records(db):
    run_id = db.start_run("run", {"limit": 200}, now=T1)
    other = db.start_run("collect")
    db.record_quota("search.list", 100, run_id=run_id)
    db.record_quota("videos.list", 1, run_id=run_id)
    db.record_quota("videos.list", 1, run_id=other)
    db.record_tokens("m", input_tokens=10, cache_read_input_tokens=90, output_tokens=5,
                     comment_count=20, run_id=run_id)
    db.finish_run(run_id, counts={"new": 3}, limits_hit=["max_classify_per_run"], now=T2)
    run = db.conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    assert (run["started_at"], run["ended_at"]) == (T1, T2)
    assert (run["quota_used"], run["input_tokens"], run["output_tokens"]) == (101, 100, 5)
    assert json.loads(run["args"]) == {"limit": 200}
    assert json.loads(run["counts"]) == {"new": 3}
    assert json.loads(run["limits_hit"]) == ["max_classify_per_run"]
    assert run["error"] is None


def test_status_summary_counts(db):
    for comment_id in ("c1", "c2", "c3", "c4", "c5"):
        db.upsert_comment(comment(comment_id), now=T1)
    classify(db, "c1", severity=4)                      # 캡처 완료
    classify(db, "c2", severity=3)                      # 미캡처
    classify(db, "c3", severity=1)                      # 심각도 미달
    classify(db, "c4", severity=5, target="타인")       # 대상 아님
    # c5는 미분류. c2는 수정되어 재분류 대상이기도 하다.
    db.upsert_comment(comment("c2", text="고침"), now=T1)
    db.conn.execute(
        "INSERT INTO captures (platform, comment_id, status, captured_at) VALUES "
        "('youtube', 'c1', 'ok', ?), ('youtube', 'c2', 'failed', ?)",
        (T1, T1),
    )
    db.conn.execute("UPDATE comments SET missing_detected_at = ? WHERE comment_id = 'c3'", (T2,))
    db.record_quota("videos.list", 7, now=T1)
    db.start_run("collect", now=T1)

    summary = db.status_summary(
        capture_min_severity=2, capture_targets=("홍길동", "불명"), now=T1
    )
    assert summary["videos_none"] == 1
    assert summary["comments"] == 5
    assert summary["comments_missing"] == 1
    assert summary["unclassified"] == 2
    assert summary["negative"] == 4
    assert summary["captures_ok"] == 1
    assert summary["captures_pending"] == 1
    assert summary["quota_used_today"] == 7
    assert summary["last_run"]["command"] == "collect"


def test_reappeared_comment_is_not_counted_as_missing(db):
    db.upsert_comment(comment(), now=T1)
    db.conn.execute("UPDATE comments SET missing_detected_at = ?", (T1,))
    db.upsert_comment(comment(), now=T2)
    summary = db.status_summary(capture_min_severity=2, capture_targets=("불명",))
    assert summary["comments_missing"] == 0
    assert row(db)["missing_detected_at"] == T1


def test_kst_display():
    assert dbm.to_kst("2026-11-10T15:30:00Z") == "2026-11-11 00:30:00"
    assert dbm.to_kst(None) == ""
