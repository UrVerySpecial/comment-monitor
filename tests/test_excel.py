from datetime import datetime

from openpyxl import Workbook, load_workbook

from comment_monitor.db import KST, CommentData
from comment_monitor.exporters.excel import COMMENT_COLUMNS, export_excel

from .conftest import VIDEO_ID
from .helpers import mark_negative

AI_HEADERS = ["감성", "대상", "법적 유형", "심각도", "근거"]


def add(db, comment_id, text, parent_id=None):
    db.upsert_comment(
        CommentData(
            platform="youtube", comment_id=comment_id, video_id=VIDEO_ID, text=text,
            parent_id=parent_id, author_name="작성자", author_channel_id="UCabc",
            author_profile_url="http://www.youtube.com/channel/UCabc", like_count=3,
            published_at="2026-11-10T11:33:00Z", updated_at="2026-11-10T11:33:00Z",
        ),
        now="2026-11-01T12:00:00Z",
    )


def read(path):
    sheet = load_workbook(path)["전체 댓글"]
    rows = list(sheet.iter_rows())
    headers = [cell.value for cell in rows[0]]
    return sheet, headers, [dict(zip(headers, row)) for row in rows[1:]]


def test_all_comments_are_exported_with_blank_ai_columns(db, tmp_path):
    add(db, "c1", "첫 댓글")
    add(db, "c1.r1", "답글", parent_id="c1")
    path = tmp_path / "output" / "댓글증거.xlsx"
    result = export_excel(db, path)

    assert result.path == path and result.rows == {"전체 댓글": 2} and not result.renamed
    sheet, headers, rows = read(path)
    assert headers == [column.header for column in COMMENT_COLUMNS]
    assert headers[-5:] == AI_HEADERS
    first, reply = rows
    assert first["증거 번호"].value == 1 and reply["증거 번호"].value == 2
    assert first["작성일시 (KST)"].value == "2026-11-10 20:33:00"
    assert first["댓글 주소"].value == f"https://www.youtube.com/watch?v={VIDEO_ID}&lc=c1"
    assert first["영상 제목"].value == "시험 영상"
    assert reply["원댓글"].value == "첫 댓글" and first["원댓글"].value is None
    assert all(row[header].value is None for row in rows for header in AI_HEADERS)
    assert sheet.freeze_panes == "B2" and sheet.auto_filter.ref == "A1:W3"


def test_ai_columns_fill_in_once_a_classification_exists(db, tmp_path):
    add(db, "c1", "나쁜 말")
    add(db, "c2", "좋은 말")
    mark_negative(db, "c1", severity=4)
    _, _, rows = read(export_excel(db, tmp_path / "out.xlsx").path)
    assert [rows[0][header].value for header in AI_HEADERS] == [
        "부정", "홍길동", "모욕", 4, "욕설",
    ]
    assert rows[1]["감성"].value is None


def test_comment_text_is_never_read_as_a_formula(db, tmp_path):
    texts = ['=HYPERLINK("http://evil","클릭")', "+1+1", "-2", "@SUM(A1)", "제어\x0b문자"]
    for index, text in enumerate(texts):
        add(db, f"c{index}", text)
    _, _, rows = read(export_excel(db, tmp_path / "out.xlsx").path)
    cells = [row["댓글 본문"] for row in rows]
    assert [cell.value for cell in cells] == [*texts[:4], "제어문자"]
    assert all(cell.data_type == "s" for cell in cells)


def test_edits_and_missing_comments_are_shown(db, tmp_path):
    add(db, "c1", "원래 글")
    db.upsert_comment(
        CommentData(platform="youtube", comment_id="c1", video_id=VIDEO_ID, text="고친 글"),
        now="2026-11-02T00:00:00Z",
    )
    add(db, "c2", "사라질 글")
    db.mark_missing_ids("youtube", ["c2"])
    _, _, rows = read(export_excel(db, tmp_path / "out.xlsx").path)
    assert rows[0]["수정 여부"].value == "수정됨 (이력 1건)"
    assert rows[0]["조회 불가 감지일시 (KST)"].value is None
    assert rows[1]["조회 불가 감지일시 (KST)"].value


def test_open_file_falls_back_to_a_new_name(db, tmp_path, monkeypatch):
    add(db, "c1", "댓글")
    path = tmp_path / "out.xlsx"
    real_save = Workbook.save

    def save(self, target):
        if target == path:
            raise PermissionError("열려 있음")
        real_save(self, target)

    monkeypatch.setattr(Workbook, "save", save)
    result = export_excel(db, path, now=datetime(2026, 11, 10, 9, 30, 0, tzinfo=KST))
    assert result.renamed and result.path.name == "out_20261110_093000.xlsx"
    assert result.path.is_file() and not path.exists()


# ---- 부정 댓글, 작성자 랭킹, 통계 요약, 영상 목록 ----


def table(path, title):
    sheet = load_workbook(path)[title]
    rows = list(sheet.iter_rows(values_only=True))
    return sheet, list(rows[0]), [dict(zip(rows[0], row)) for row in rows[1:]]


def export_all(cfg, db, tmp_path, body=""):
    from comment_monitor.exporters.excel import build_sheets, import_reviews

    config = cfg(body)
    path = tmp_path / "out.xlsx"
    imported = import_reviews(db, path)
    return path, imported, export_excel(db, path, sheets=build_sheets(config, db))


def fill_review(path, comment_id, status, note):
    """사람이 엑셀에서 검토 상태와 비고를 적은 것처럼 고친다."""
    workbook = load_workbook(path)
    sheet = workbook["부정 댓글"]
    headers = {cell.value: cell.column for cell in sheet[1]}
    for line in sheet.iter_rows(min_row=2):
        if line[headers["댓글 ID"] - 1].value == comment_id:
            line[headers["검토 상태"] - 1].value = status
            line[headers["비고"] - 1].value = note
    workbook.save(path)


def test_default_tabs_and_negative_sheet(cfg, db, tmp_path):
    add(db, "c1", "평범한 댓글")
    add(db, "c2", "=욕설()")
    mark_negative(db, "c2", severity=4)
    path, _, result = export_all(cfg, db, tmp_path)

    assert load_workbook(path).sheetnames == ["부정 댓글", "작성자 랭킹", "통계 요약", "영상 목록"]
    assert result.units == {
        "부정 댓글": "건", "작성자 랭킹": "명", "통계 요약": "개 영상", "영상 목록": "개 영상",
    }
    sheet, headers, rows = table(path, "부정 댓글")
    assert headers[-4:] == ["캡처", "캡처 해시 (SHA-256)", "검토 상태", "비고"]
    assert [row["댓글 ID"] for row in rows] == ["c2"]
    assert rows[0]["댓글 본문"] == "=욕설()" and rows[0]["심각도"] == 4
    body = sheet.cell(row=2, column=headers.index("댓글 본문") + 1)
    assert body.data_type == "s"                           # 수식으로 읽히지 않는다
    # 검토 상태 칸은 고를 수 있는 값이 정해져 있다.
    rules = sheet.data_validations.dataValidation
    assert len(rules) == 1 and rules[0].formula1 == '"확인,제외,보류"'

    path, _, _ = export_all(cfg, db, tmp_path, "export:\n  include_all_comments: true\n")
    assert load_workbook(path).sheetnames[-1] == "전체 댓글"
    assert len(table(path, "전체 댓글")[2]) == 2


def test_review_status_and_notes_survive_a_re_export(cfg, db, tmp_path):
    for name in ("c1", "c2", "c3"):
        add(db, name, "욕")
        mark_negative(db, name)
    path, first, _ = export_all(cfg, db, tmp_path)
    assert not first.found

    fill_review(path, "c1", "확인", "신고 예정")
    fill_review(path, "c2", "제외", None)
    path, imported, _ = export_all(cfg, db, tmp_path)
    assert (imported.found, imported.saved, imported.invalid) == (True, 2, ())
    rows = {row["댓글 ID"]: row for row in table(path, "부정 댓글")[2]}
    assert (rows["c1"]["검토 상태"], rows["c1"]["비고"]) == ("확인", "신고 예정")
    assert (rows["c2"]["검토 상태"], rows["c3"]["검토 상태"]) == ("제외", None)

    # 제외로 표시한 댓글은 랭킹 점수에서 빠진다. 통계는 AI 판정 그대로다.
    ranking = table(path, "작성자 랭킹")[2]
    assert (ranking[0]["악성 점수"], ranking[0]["부정 댓글 수"]) == (6, 2)

    # 선택지에 없는 값은 알리고, 이전 상태를 지킨다. 비고는 받아들인다.
    fill_review(path, "c1", "나중에", "다시 볼 것")
    fill_review(path, "c2", None, None)                    # 지우면 지워진다
    path, imported, _ = export_all(cfg, db, tmp_path)
    assert imported.invalid == ("c1: 나중에",)
    rows = {row["댓글 ID"]: row for row in table(path, "부정 댓글")[2]}
    assert (rows["c1"]["검토 상태"], rows["c1"]["비고"]) == ("확인", "다시 볼 것")
    assert rows["c2"]["검토 상태"] is None


def test_unreadable_existing_file_is_not_overwritten(cfg, db, tmp_path):
    import pytest

    from comment_monitor.exporters.excel import ExportError, import_reviews

    path = tmp_path / "out.xlsx"
    path.write_bytes(b"not an excel file")
    with pytest.raises(ExportError, match="덮어쓰지 않았습니다"):
        import_reviews(db, path)
    assert path.read_bytes() == b"not an excel file"


def test_ranking_and_stats_tabs(cfg, db, tmp_path):
    add(db, "c1", "욕 하나")
    add(db, "c2", "욕 둘")
    add(db, "c3", "남 얘기")
    add(db, "c4", "잘 봤어요")
    mark_negative(db, "c1", severity=4)
    mark_negative(db, "c2", severity=2, target="불명")
    mark_negative(db, "c3", severity=5, target="타인")
    path, _, result = export_all(cfg, db, tmp_path)
    assert result.rows == {"부정 댓글": 3, "작성자 랭킹": 1, "통계 요약": 1, "영상 목록": 1}

    _, headers, rows = table(path, "작성자 랭킹")
    assert "홍길동 점수" in headers and "불명 점수" in headers
    assert not any("이전 대상" in header for header in headers)
    top = rows[0]
    assert (top["순위"], top["작성자 채널 ID"], top["악성 점수"]) == (1, "UCabc", 6)
    assert (top["홍길동 점수"], top["불명 점수"], top["주 대상"]) == (4, 2, "홍길동")
    assert (top["부정 댓글 수"], top["최고 심각도"], top["모욕"]) == (2, 4, 2)

    cells = [list(line) for line in load_workbook(path)["통계 요약"].iter_rows(values_only=True)]
    lookup = {line[0]: line for line in cells if line and line[0]}
    assert lookup["수집된 댓글 수"][1] == 4 and lookup["분류된 댓글 수"][1] == 3
    assert lookup["미분류 건수"][1] == 1
    assert lookup["부정"][1] == 3 and lookup["부정"][2] == 1.0
    start = cells.index(next(line for line in cells if line[0] == "대상별 (부정 댓글)"))
    head = cells[start + 1]
    by_target = {line[0]: dict(zip(head, line)) for line in cells[start + 2:] if line[0]}
    assert list(by_target) == ["홍길동", "타인", "불명"]
    assert by_target["홍길동"]["부정 댓글 수"] == 1 and by_target["홍길동"]["심각도 4"] == 1
    assert by_target["타인"]["심각도 5"] == 1 and by_target["불명"]["작성자 수"] == 1


def test_judgments_for_removed_targets_get_their_own_column(cfg, db, tmp_path):
    add(db, "c1", "욕")
    mark_negative(db, "c1", target="예전 이름")
    path, _, _ = export_all(cfg, db, tmp_path)
    _, headers, rows = table(path, "작성자 랭킹")
    assert "기타(이전 대상) 점수 ※재분류 필요" in headers
    assert rows[0]["주 대상"] == "기타(이전 대상)"


def test_video_list_is_keyed_by_the_video_id_on_comment_rows(cfg, db, tmp_path):
    db.conn.execute(
        "INSERT INTO videos (platform, video_id, title, source, source_detail, comment_count, "
        "collect_status, first_seen_at) VALUES ('youtube', 'other000001', '다른 영상', 'search', "
        "'홍길동', 90, 'full', '2026-11-01T00:00:00Z'), ('youtube', 'never000001', '안 모은 영상', "
        "'search', '홍길동', 500, 'none', '2026-11-01T00:00:00Z')"
    )
    add(db, "c1", "욕")
    add(db, "c2", "칭찬")
    add(db, "c3", "아직 분류 안 한 댓글")
    mark_negative(db, "c1")
    db.conn.execute(
        "INSERT INTO classifications (platform, comment_id, stage, sentiment, target, legal_type, "
        "severity, reason, model, prompt_version, classified_at) VALUES ('youtube', 'c2', 'final', "
        "'긍정', '홍길동', '해당 없음', 0, '칭찬', 'm', 'v1', '2026-11-02T00:00:00Z')"
    )
    path, _, result = export_all(cfg, db, tmp_path)

    _, _, comments = table(path, "부정 댓글")
    sheet, headers, videos = table(path, "영상 목록")
    assert result.rows["영상 목록"] == 2 and headers[:3] == ["영상 ID", "영상 주소", "영상 제목"]
    assert [row["영상 ID"] for row in videos] == ["other000001", VIDEO_ID]   # 댓글 많은 순
    assert comments[0]["영상 ID"] == VIDEO_ID
    mine = videos[1]
    assert (mine["영상 제목"], mine["발견 경로"]) == ("시험 영상", "지정")
    assert (mine["수집된 댓글 수"], mine["긍정 댓글 수"], mine["부정 댓글 수"]) == (3, 1, 1)
    assert mine["영상 주소"] == f"https://www.youtube.com/watch?v={VIDEO_ID}"
    assert (videos[0]["발견 경로"], videos[0]["수집 상태"]) == ("검색: 홍길동", "전체")
    assert sheet.auto_filter.ref == "A1:L3"
