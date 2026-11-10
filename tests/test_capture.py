import hashlib
import io

import pytest
from openpyxl import load_workbook
from PIL import Image

from comment_monitor import cli
from comment_monitor.capture import (
    Blocked,
    CaptureError,
    CaptureFailed,
    Capturer,
    Seen,
    compose,
    header_fields,
    same_text,
)
from comment_monitor.cli import main
from comment_monitor.db import CommentData, Database

from .conftest import VIDEO_ID
from .helpers import mark_negative

NOW = "2026-11-10T03:04:05Z"
SCOPE = {"min_severity": 2, "targets": ("홍길동", "불명")}


def png(width=400, height=300, color="white"):
    out = io.BytesIO()
    Image.new("RGB", (width, height), color).save(out, format="PNG")
    return out.getvalue()


def add(db, comment_id, text="나쁜 말", *, severity=3, target="홍길동"):
    db.upsert_comment(
        CommentData(
            platform="youtube", comment_id=comment_id, video_id=VIDEO_ID, text=text,
            author_name="작성자", author_channel_id="UCabc", published_at="2026-11-01T15:30:00Z",
        ),
        now="2026-11-02T00:00:00Z",
    )
    if severity:
        mark_negative(db, comment_id, severity=severity, target=target)


class FakeCamera:
    """브라우저 대신 정해 둔 화면을 돌려준다. shown에 없는 댓글은 DB 본문 그대로 보인다."""

    def __init__(self, db, shown=None):
        self.db = db
        self.shown = shown or {}
        self.opened = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass

    def view(self, url, comment_id):
        self.opened.append(comment_id)
        result = self.shown.get(comment_id)
        if isinstance(result, Exception):
            raise result
        if result is None:
            result = self.db.scalar("SELECT text FROM comments WHERE comment_id = ?", (comment_id,))
        if isinstance(result, str):
            result = Seen(result, "@작성자", "시험 영상", "시험 채널")
        return result

    def shoot(self):
        return png()


def run(cfg, db, camera, **options):
    worker = Capturer(cfg(), db, camera, sleep=lambda s: None, now=lambda: NOW)
    return worker.run(db.comments_to_capture(**SCOPE, **options))


def captures(db):
    return [dict(row) for row in db.conn.execute("SELECT * FROM captures ORDER BY id")]


def test_targets_are_negative_severe_enough_and_not_other(db):
    add(db, "low", severity=1)
    add(db, "mid", severity=2)
    add(db, "high", severity=5)
    add(db, "other", severity=5, target="타인")
    add(db, "unknown", severity=4, target="불명")
    add(db, "plain", severity=0)
    rows = db.comments_to_capture(**SCOPE)
    assert [row["comment_id"] for row in rows] == ["high", "unknown", "mid"]
    assert db.count_to_capture(**SCOPE) == 3
    assert [r["comment_id"] for r in db.comments_to_capture(**SCOPE, limit=2)] == ["high", "unknown"]


def test_capture_saves_raw_and_composite_with_hashes(cfg, db, tmp_path):
    add(db, "Ugx.reply-1", "나쁜 말")
    summary = run(cfg, db, FakeCamera(db))
    assert (summary.captured, summary.failed) == (1, 0)

    (row,) = captures(db)
    no = db.scalar("SELECT evidence_no FROM comments")
    assert row["status"] == "ok" and row["error"] is None and row["captured_at"] == NOW
    # DB에는 출력 폴더 기준 상대 경로를 적는다.
    assert row["composite_path"] == f"captures/{no:06d}_Ugx.reply-1.png"
    assert row["raw_path"] == f"captures/raw/{no:06d}_Ugx.reply-1.png"
    output = tmp_path / "output"
    raw = (output / row["raw_path"]).read_bytes()
    composite = (output / row["composite_path"]).read_bytes()
    assert raw == png()                                   # 원본은 가공하지 않는다
    assert row["raw_sha256"] == hashlib.sha256(raw).hexdigest()
    assert row["composite_sha256"] == hashlib.sha256(composite).hexdigest()
    # 합성본은 원본 위에 머리띠를 붙인 것이다.
    image = Image.open(io.BytesIO(composite))
    assert image.width == 400 and image.height > 300
    assert image.crop((0, image.height - 300, 400, image.height)).tobytes() == Image.open(
        io.BytesIO(raw)
    ).convert("RGB").tobytes()


def test_captured_comments_are_not_captured_again(cfg, db):
    add(db, "c1")
    add(db, "c2")
    run(cfg, db, FakeCamera(db))
    again = FakeCamera(db)
    summary = run(cfg, db, again)
    assert again.opened == [] and summary.total == 0
    assert len(captures(db)) == 2
    assert db.count_to_capture(**SCOPE) == 0


def test_text_mismatch_is_recorded_as_failure_and_retried_last(cfg, db, tmp_path):
    add(db, "edited", "원래 본문", severity=5)
    add(db, "fine", "그대로", severity=5)
    add(db, "gone", severity=5)
    camera = FakeCamera(db, {
        "edited": "고친 본문",
        "gone": CaptureFailed("강조된 댓글이 보이지 않음"),
    })
    summary = run(cfg, db, camera)
    assert (summary.done, summary.captured, summary.failed) == (3, 1, 2)
    by_id = {row["comment_id"]: row for row in captures(db)}
    assert by_id["edited"]["status"] == "failed" and "본문" in by_id["edited"]["error"]
    assert by_id["edited"]["composite_path"] is None
    assert by_id["gone"]["error"] == "강조된 댓글이 보이지 않음"
    assert by_id["fine"]["status"] == "ok"
    assert len(list((tmp_path / "output" / "captures").glob("*.png"))) == 1

    # 실패한 댓글은 다시 대상이 되지만, 같은 심각도에서는 실패한 적 없는 댓글 뒤로 간다.
    add(db, "new", severity=5)
    assert [r["comment_id"] for r in db.comments_to_capture(**SCOPE)] == ["new", "edited", "gone"]


def test_whitespace_and_invisible_differences_are_not_mismatch():
    assert same_text("첫 줄\n둘째 줄 ❤", "첫 줄\r\n둘째 줄  ❤️​")
    assert not same_text("좋아요", "싫어요")


def test_missing_title_or_author_on_screen_fails(cfg, db):
    add(db, "c1", "본문")
    summary = run(cfg, db, FakeCamera(db, {"c1": Seen("본문", "", "시험 영상", "시험 채널")}))
    assert summary.failed == 1 and "작성자 표시명" in summary.failures[0][1]


def test_blocked_screen_stops_without_recording(cfg, db):
    add(db, "c1", severity=5)
    add(db, "c2", severity=4)
    add(db, "c3", severity=3)
    camera = FakeCamera(db, {"c2": Blocked("google.com")})
    summary = run(cfg, db, camera)
    assert camera.opened == ["c1", "c2"]
    assert summary.blocked == "google.com" and summary.captured == 1 and summary.failed == 0
    assert [row["comment_id"] for row in captures(db)] == ["c1"]


def test_pauses_between_comments(cfg, db):
    for n in range(3):
        add(db, f"c{n}")
    naps = []
    Capturer(cfg(), db, FakeCamera(db), pause=4, sleep=naps.append, now=lambda: NOW).run(
        db.comments_to_capture(**SCOPE)
    )
    assert naps == [4, 4]


def test_header_carries_exact_times_in_kst(db):
    add(db, "c1")
    (row,) = db.comments_to_capture(**SCOPE)
    fields = dict(header_fields(row, NOW))
    assert fields["증거 번호"] == f"{row['evidence_no']:06d}"
    assert fields["댓글 주소"] == f"https://www.youtube.com/watch?v={VIDEO_ID}&lc=c1"
    assert fields["영상 제목"] == "시험 영상"
    assert fields["작성자"] == "작성자  ·  채널 ID UCabc"
    assert fields["작성 시각"] == "2026-11-02 00:30:00 (KST)"
    assert fields["캡처 시각"] == "2026-11-10 12:04:05 (KST)"


def test_long_header_values_wrap_instead_of_overflowing():
    short = Image.open(io.BytesIO(compose(png(), [("영상 제목", "짧은 제목")])))
    long = Image.open(io.BytesIO(compose(png(), [("영상 제목", "아주긴제목" * 40)])))
    assert long.height > short.height and long.width == short.width == 400


# ---- 명령 ----


@pytest.fixture
def site(write_config, tmp_path, monkeypatch):
    """캡처 대상 세 건이 든 DB와 설정 경로, 가짜 카메라."""
    path = write_config()
    cameras = []
    with Database(tmp_path / "data" / "monitor.db") as db:
        db.conn.execute(
            "INSERT INTO videos (platform, video_id, title, source, first_seen_at) "
            "VALUES ('youtube', ?, '시험 영상', 'direct', '2026-11-01T00:00:00Z')", (VIDEO_ID,),
        )
        for n, severity in enumerate((2, 5, 3), start=1):
            add(db, f"c{n}", f"나쁜 말 {n}", severity=severity)

    def make_camera(*, headless):
        camera = FakeCamera(Database(tmp_path / "data" / "monitor.db"))
        camera.headless = headless
        cameras.append(camera)
        return camera

    monkeypatch.setattr(cli, "make_camera", make_camera)
    monkeypatch.setattr("comment_monitor.capture.PAUSE_SECONDS", 0)
    monkeypatch.setattr("comment_monitor.capture.time.sleep", lambda s: None)
    return str(path), cameras


def test_capture_command_limit_takes_most_severe_first(site, tmp_path, capsys):
    path, cameras = site
    assert main(["capture", "--config", path, "--limit", "2", "--headed"]) == 0
    out = capsys.readouterr().out
    assert "캡처 대상: 3건 중 2건" in out and "창을 띄워 실행" in out
    assert "캡처 완료: 2건 · 실패 0건 · 남은 미캡처 1건" in out
    assert cameras[0].headless is False and cameras[0].opened == ["c2", "c3"]
    with Database(tmp_path / "data" / "monitor.db") as db:
        run_row = db.conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        assert run_row["command"] == "capture" and '"limit"' in run_row["limits_hit"]
        assert '"captured": 2' in run_row["counts"]

    # 다시 실행하면 남은 한 건만 찍고, 그다음에는 할 일이 없다.
    assert main(["capture", "--config", path]) == 0
    assert cameras[1].headless is True and cameras[1].opened == ["c1"]
    assert main(["capture", "--config", path]) == 0
    assert "캡처할 댓글이 없습니다" in capsys.readouterr().out
    assert len(cameras) == 2


def test_excel_capture_cell_links_to_relative_path_next_to_workbook(site, tmp_path):
    path, _ = site
    assert main(["capture", "--config", path]) == 0
    assert main(["export", "--config", path]) == 0
    book = tmp_path / "output" / "댓글증거.xlsx"
    sheet = load_workbook(book).worksheets[0]
    rows = list(sheet.iter_rows(values_only=True))
    header = next(row for row in rows if "캡처" in row)
    link_at, hash_at = header.index("캡처"), header.index("캡처 해시 (SHA-256)")
    data = [row for row in rows[rows.index(header) + 1:] if row[link_at]]
    assert len(data) == 3
    for row in data:
        formula = row[link_at]
        assert formula.startswith('=HYPERLINK("captures/') and formula.endswith('","열기")')
        target = formula.split('"')[1]
        # 상대 경로이고, 엑셀 파일이 있는 폴더에서 실제 파일로 이어진다.
        assert not target.startswith("/") and ":" not in target
        image = book.parent / target
        assert image.is_file()
        assert row[hash_at] == hashlib.sha256(image.read_bytes()).hexdigest()


def test_capture_command_reports_browser_problem(site, monkeypatch, capsys):
    path, _ = site

    def broken(*, headless):
        raise CaptureError("캡처용 브라우저(Chromium)가 설치되어 있지 않습니다.")

    monkeypatch.setattr(cli, "make_camera", broken)
    assert main(["capture", "--config", path]) == 1
    captured = capsys.readouterr()
    assert "설치되어 있지 않습니다" in captured.err
    assert "캡처 완료: 0건" in captured.out
