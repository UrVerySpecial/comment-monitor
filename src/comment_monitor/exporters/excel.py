"""엑셀 출력.

파일 하나에 탭 넷을 만든다: 부정 댓글, 작성자 랭킹, 통계 요약, 영상 목록. 설정에서 켜면 전체 댓글 탭도 붙인다.
파일은 실행할 때마다 DB 기준으로 통째로 다시 만든다. 사람이 엑셀에 적은 검토 상태와 비고는
다시 만들기 전에 import_reviews()로 DB에 옮겨 두었다가 새 파일에 다시 채운다.

표 모양의 탭은 SheetSpec(이름, 컬럼 목록, 행)으로, 통계처럼 자유로운 배치는 GridSpec으로 정의한다.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from zipfile import BadZipFile

from openpyxl import Workbook, load_workbook
from openpyxl.cell import WriteOnlyCell
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.utils.exceptions import InvalidFileException
from openpyxl.worksheet.datavalidation import DataValidation

from .. import ranking
from ..collectors.youtube import PLATFORM, comment_url, video_url
from ..config import Config
from ..planner import source_label
from ..db import (
    COLLECT_FULL,
    COLLECT_PARTIAL,
    KST,
    LEGAL_TYPES,
    REVIEW_STATUSES,
    SENTIMENTS,
    Database,
    to_kst,
)

MAX_ROWS = 1_048_576  # 엑셀 시트 한 장의 행 수 한도
MAX_CELL_CHARS = 32_767

NEGATIVE_TITLE = "부정 댓글"
RANKING_TITLE = "작성자 랭킹"
STATS_TITLE = "통계 요약"
VIDEOS_TITLE = "영상 목록"
REVIEW_HEADER, NOTE_HEADER = "검토 상태", "비고"

_HEADER_FONT = Font(bold=True, color="FFFFFF")
_HEADER_ALIGNMENT = Alignment(horizontal="center", vertical="center", wrap_text=True)
_TITLE_FONT = Font(bold=True, size=12)
_NOTE_FONT = Font(italic=True, color="7F7F7F")
_DEFAULT_FILL = "595959"
# 컬럼 묶음별 머리글 색
_GROUP_FILLS = {
    "식별": "44546A",
    "시각": "5B7B9A",
    "위치": "4F81BD",
    "작성자": "8064A2",
    "내용": "376092",
    "AI 판정": "C0504D",
    "증거": "7F6000",
    "사람 입력": "548235",
    "점수": "C0504D",
    "건수": "376092",
}


class ExportError(Exception):
    """엑셀을 만들 수 없을 때. 기존 파일은 건드리지 않는다."""


@dataclass(frozen=True)
class Link:
    """파일로 가는 링크 칸. 경로는 엑셀 파일이 있는 폴더 기준이다."""

    path: str
    label: str = "열기"


@dataclass(frozen=True)
class Column:
    header: str
    group: str
    width: float
    value: Callable[[Any], Any]  # 행에서 칸에 넣을 값을 꺼낸다
    choices: tuple[str, ...] = ()  # 사람이 고르는 칸이면 선택지


@dataclass(frozen=True)
class SheetSpec:
    """머리글 한 줄에 행이 이어지는 표."""

    title: str
    columns: Sequence[Column]
    rows: Callable[[Database], Iterable[Any]]
    count: Callable[[Database], int] | None = None  # 없으면 행을 먼저 다 만든 뒤 센다
    unit: str = "건"


@dataclass(frozen=True)
class GridSpec:
    """자유 배치. build는 (종류, 칸 목록)의 목록과 요약 건수를 돌려준다.

    종류는 "title"(구역 제목), "header"(표 머리글), "note"(설명), "row"(자료)다.
    """

    title: str
    build: Callable[[Database], tuple[list[tuple[str, list[Any]]], int]]
    widths: Sequence[float] = ()
    unit: str = "건"


@dataclass(frozen=True)
class ExportResult:
    path: Path
    rows: dict[str, int]      # 탭 이름별 행 수
    renamed: bool             # 원래 파일이 열려 있어 다른 이름으로 저장했는가
    units: dict[str, str] = field(default_factory=dict)  # 탭 이름별 세는 단위


@dataclass(frozen=True)
class ReviewImport:
    found: bool = False       # 기존 파일에 검토 칸이 있었는가
    saved: int = 0
    invalid: tuple[str, ...] = ()  # 선택지에 없는 검토 상태가 적힌 댓글


def _field(name: str) -> Callable[[Any], Any]:
    return lambda row: row[name]


def _kst(name: str) -> Callable[[Any], str]:
    return lambda row: to_kst(row[name])


def _edited(row: Any) -> str:
    if row["revision_count"]:
        return f"수정됨 (이력 {row['revision_count']}건)"
    if row["updated_at"] and row["published_at"] and row["updated_at"] != row["published_at"]:
        return "수정됨"
    return ""


def _capture_link(row: Any) -> Any:
    return Link(row["capture_path"]) if row["capture_path"] else None


# DESIGN.md 12장 탭 1의 컬럼 순서. 전체 댓글 탭은 여기까지만 쓴다.
COMMENT_COLUMNS: tuple[Column, ...] = (
    Column("증거 번호", "식별", 10, _field("evidence_no")),
    Column("플랫폼", "식별", 9, _field("platform")),
    Column("댓글 ID", "식별", 30, _field("comment_id")),
    Column("작성일시 (KST)", "시각", 20, _kst("published_at")),
    Column("수집일시 (KST)", "시각", 20, _kst("first_collected_at")),
    Column("수정 여부", "시각", 16, _edited),
    Column("조회 불가 감지일시 (KST)", "시각", 20, _kst("missing_at")),
    Column("댓글 주소", "위치", 40, lambda row: comment_url(row["video_id"], row["comment_id"])),
    Column("영상 ID", "위치", 14, _field("video_id")),  # 영상 목록 탭과 잇는 키
    Column("영상 제목", "위치", 40, _field("video_title")),
    Column("영상 주소", "위치", 40, lambda row: video_url(row["video_id"])),
    Column("영상 채널명", "위치", 20, _field("video_channel")),
    Column("작성자명", "작성자", 20, _field("author_name")),
    Column("작성자 채널 ID", "작성자", 28, _field("author_channel_id")),
    Column("프로필 주소", "작성자", 40, _field("author_profile_url")),
    Column("댓글 본문", "내용", 70, _field("text")),
    Column("원댓글", "내용", 50, _field("parent_text")),
    Column("좋아요 수", "내용", 10, _field("like_count")),
    Column("감성", "AI 판정", 8, _field("sentiment")),
    Column("대상", "AI 판정", 11, _field("target")),
    Column("법적 유형", "AI 판정", 14, _field("legal_type")),
    Column("심각도", "AI 판정", 8, _field("severity")),
    Column("근거", "AI 판정", 50, _field("reason")),
)

# 부정 댓글 탭에만 붙는 증거와 사람 입력 칸. 캡처는 구현 순서 6번 뒤에 채워진다.
EVIDENCE_COLUMNS: tuple[Column, ...] = (
    Column("캡처", "증거", 9, _capture_link),
    Column("캡처 해시 (SHA-256)", "증거", 22, _field("capture_sha256")),
)
REVIEW_COLUMNS: tuple[Column, ...] = (
    Column(REVIEW_HEADER, "사람 입력", 11, _field("review_status"), choices=REVIEW_STATUSES),
    Column(NOTE_HEADER, "사람 입력", 40, _field("review_note")),
)

NEGATIVE_COMMENTS = SheetSpec(
    title=NEGATIVE_TITLE,
    columns=(*COMMENT_COLUMNS, *EVIDENCE_COLUMNS, *REVIEW_COLUMNS),
    rows=lambda db: db.export_rows(only_negative=True),
    count=lambda db: db.negative_count(),
)

ALL_COMMENTS = SheetSpec(
    title="전체 댓글",
    columns=COMMENT_COLUMNS,
    rows=lambda db: db.export_rows(),
    count=lambda db: db.scalar("SELECT COUNT(*) FROM comments"),
)

SHEETS: tuple[SheetSpec, ...] = (ALL_COMMENTS,)


def _attr(name: str) -> Callable[[Any], Any]:
    return lambda item: getattr(item, name)


def ranking_sheet(target_names: Sequence[str], *, with_past: bool) -> SheetSpec:
    """작성자 랭킹 탭. 대상별 악성 점수 칸은 설정의 대상 수만큼 생긴다."""
    buckets = [b for b in ranking.ranking_buckets(target_names)
               if b != ranking.PAST_TARGET or with_past]

    def score_header(bucket: str) -> str:
        if bucket == ranking.PAST_TARGET:
            return f"{bucket} 점수 ※재분류 필요"
        return f"{bucket} 점수"

    columns = (
        Column("순위", "식별", 7, _attr("rank")),
        Column("작성자명", "작성자", 22, _attr("name")),
        Column("작성자 채널 ID", "작성자", 28, _attr("channel_id")),
        Column("프로필 주소", "작성자", 40, _attr("profile_url")),
        Column("악성 점수", "점수", 10, _attr("score")),
        *(
            Column(score_header(bucket), "점수", 13,
                   lambda a, bucket=bucket: a.by_target.get(bucket, 0))
            for bucket in buckets
        ),
        Column("주 대상", "점수", 14, _attr("main_target")),
        Column("부정 댓글 수", "건수", 10, _attr("count")),
        Column("최고 심각도", "건수", 10, _attr("max_severity")),
        *(
            Column(legal_type, "건수", 10,
                   lambda a, legal_type=legal_type: a.by_type.get(legal_type, 0))
            for legal_type in ranking.RANKED_TYPES
        ),
        Column("활동 영상 수", "건수", 10, lambda a: len(a.videos)),
        Column("첫 작성일 (KST)", "시각", 20, lambda a: to_kst(a.first_at)),
        Column("최근 작성일 (KST)", "시각", 20, lambda a: to_kst(a.last_at)),
        Column("조회 불가 건수", "건수", 10, _attr("missing")),
    )
    return SheetSpec(
        title=RANKING_TITLE,
        columns=columns,
        rows=lambda db: ranking.rank_authors(db.judged_rows(), target_names),
        unit="명",
    )


_COLLECT_LABELS = {COLLECT_FULL: "전체", COLLECT_PARTIAL: "부분"}


def _found_by(row: Any) -> str:
    label = source_label(row["source"])
    return f"{label}: {row['source_detail']}" if row["source_detail"] else label


# 댓글 탭의 "영상 ID"로 찾아볼 수 있는 영상 목록. 댓글을 한 건이라도 모은 영상만 올린다.
VIDEO_LIST = SheetSpec(
    title=VIDEOS_TITLE,
    columns=(
        Column("영상 ID", "식별", 14, _field("video_id")),
        Column("영상 주소", "위치", 44, lambda row: video_url(row["video_id"])),
        Column("영상 제목", "위치", 50, _field("title")),
        Column("영상 채널명", "위치", 22, _field("channel_title")),
        Column("전체 댓글 수", "건수", 11, _field("comment_count")),
        Column("수집된 댓글 수", "건수", 11, _field("collected")),
        # 긍정과 부정은 수집해서 분류까지 마친 댓글만 센 값이다. 영상 전체의 수가 아니다.
        Column("긍정 댓글 수", "건수", 11, _field("positives")),
        Column("부정 댓글 수", "건수", 11, _field("negatives")),
        Column("수집 상태", "건수", 10,
               lambda row: _COLLECT_LABELS.get(row["collect_status"], "미수집")),
        Column("발견 경로", "위치", 24, _found_by),
        Column("게시일시 (KST)", "시각", 20, _kst("published_at")),
        Column("최근 수집일시 (KST)", "시각", 20, _kst("last_collected_at")),
    ),
    rows=lambda db: db.video_rows(),
    unit="개 영상",
)


@dataclass(frozen=True)
class Percent:
    value: float | None


def _period(start: str | None, end: str | None) -> str:
    return f"{to_kst(start)} ~ {to_kst(end)}" if start and end else ""


def stats_sheet(target_names: Sequence[str]) -> GridSpec:
    """통계 요약 탭: 전체 합계, 영상별, 대상별."""

    def build(db: Database) -> tuple[list[tuple[str, list[Any]]], int]:
        stats = ranking.build_stats(db.judged_rows(), db.all_videos(), target_names)
        buckets = [t.target for t in stats.targets]
        classified = sum(stats.sentiments.values())
        grid: list[tuple[str, list[Any]]] = [("title", ["전체 합계"])]
        grid += [
            ("row", ["대상", ", ".join(target_names)]),
            ("row", ["영상 수", len(stats.videos)]),
            ("row", ["수집된 댓글 수", stats.comments]),
            ("row", ["분류된 댓글 수", classified]),
            ("row", ["미분류 건수", stats.unclassified]),
            ("row", ["  그중 분류 실패", stats.failed]),
        ]
        for sentiment in SENTIMENTS:
            count = stats.sentiments.get(sentiment, 0)
            grid.append(("row", [sentiment, count, Percent(count / classified if classified else None)]))
        grid += [
            ("row", ["수집 기간 (KST)", _period(stats.collected_from, stats.collected_to)]),
            ("row", ["댓글 작성 기간 (KST)", _period(stats.published_from, stats.published_to)]),
            ("note", ["비율은 분류된 댓글을 기준으로 한다. 이 탭은 검토 상태와 무관하게 AI 판정 그대로 센다."]),
            ("blank", []),
            ("title", ["영상별"]),
            ("header", [
                "영상 제목", "채널", "영상 주소", "전체 댓글 수", "수집된 댓글 수", "수집 상태",
                *SENTIMENTS, *(f"{s} 비율" for s in SENTIMENTS),
                *(f"부정: {bucket}" for bucket in buckets), "미분류 건수", "비고",
            ]),
        ]
        for video in stats.videos:
            remarks = []
            if video.collect_status != COLLECT_FULL:
                remarks.append("부분 수집")
            if video.unclassified:
                remarks.append(f"미분류 {video.unclassified:,}건")
            if remarks:
                remarks.append("비율이 영상 전체를 대표하지 않음")
            grid.append(("row", [
                video.title, video.channel, video_url(video.video_id), video.total_comments,
                video.collected, _COLLECT_LABELS.get(video.collect_status, "미수집"),
                *(video.sentiments.get(s, 0) for s in SENTIMENTS),
                *(Percent(video.ratio(s)) for s in SENTIMENTS),
                *(video.negative_by_target.get(bucket, 0) for bucket in buckets),
                video.unclassified, " · ".join(remarks),
            ]))
        legal_types = [t for t in LEGAL_TYPES if t != "해당 없음"]
        grid += [
            ("blank", []),
            ("title", ["대상별 (부정 댓글)"]),
            ("header", [
                "대상", "부정 댓글 수", *(f"심각도 {n}" for n in ranking.SEVERITIES),
                *legal_types, "작성자 수", "캡처 건수", "조회 불가 건수",
            ]),
        ]
        for target in stats.targets:
            label = target.target
            if label == ranking.PAST_TARGET:
                label += " ※재분류 필요"
            grid.append(("row", [
                label, target.negative,
                *(target.by_severity.get(n, 0) for n in ranking.SEVERITIES),
                *(target.by_type.get(t, 0) for t in legal_types),
                len(target.authors), target.captured, target.missing,
            ]))
        return grid, len(stats.videos)

    return GridSpec(title=STATS_TITLE, build=build, widths=(44, 22, 40), unit="개 영상")


def build_sheets(config: Config, db: Database) -> tuple[SheetSpec | GridSpec, ...]:
    """설정에 맞는 탭 목록. 대상 이름에 따라 랭킹과 통계의 칸이 달라진다."""
    names = config.target_names
    marks = ", ".join("?" for _ in config.target_values)
    with_past = bool(db.scalar(
        "SELECT EXISTS (SELECT 1 FROM classifications WHERE is_current = 1 AND status = 'ok' "
        f"AND stage = 'final' AND sentiment = '부정' AND target NOT IN ({marks}))",
        tuple(config.target_values),
    ))
    sheets: list[SheetSpec | GridSpec] = [
        NEGATIVE_COMMENTS, ranking_sheet(names, with_past=with_past), stats_sheet(names),
        VIDEO_LIST,
    ]
    if config.export.include_all_comments:
        sheets.append(ALL_COMMENTS)
    return tuple(sheets)


def import_reviews(db: Database, path: Path) -> ReviewImport:
    """기존 엑셀의 부정 댓글 탭에서 검토 상태와 비고를 읽어 DB에 옮긴다.

    파일이 있는데 읽을 수 없으면 ExportError를 낸다. 그대로 덮어쓰면 사람이 적은 내용을 잃는다.
    """
    if not path.is_file():
        return ReviewImport()
    try:
        workbook = load_workbook(path, read_only=True, data_only=True)
    except (BadZipFile, InvalidFileException, KeyError, OSError) as exc:
        raise ExportError(
            f"기존 엑셀 파일을 읽을 수 없습니다: {path} ({exc}). 검토 상태와 비고를 잃지 않도록 "
            "덮어쓰지 않았습니다. 파일을 닫거나 다른 곳으로 옮긴 뒤 다시 실행해 주세요."
        ) from exc
    try:
        if NEGATIVE_TITLE not in workbook.sheetnames:
            return ReviewImport()
        rows = workbook[NEGATIVE_TITLE].iter_rows(values_only=True)
        headers = {name: index for index, name in enumerate(next(rows, ()) or ())}
        if not {"댓글 ID", REVIEW_HEADER, NOTE_HEADER} <= headers.keys():
            return ReviewImport()

        def cell(row: Sequence[Any], header: str) -> str | None:
            index = headers.get(header)
            value = row[index] if index is not None and index < len(row) else None
            text = "" if value is None else str(value).strip()
            return text or None

        existing = db.reviews()
        updates, invalid = [], []
        for row in rows:
            comment_id = cell(row, "댓글 ID")
            if comment_id is None:
                continue
            key = (cell(row, "플랫폼") or PLATFORM, comment_id)
            status, note = cell(row, REVIEW_HEADER), cell(row, NOTE_HEADER)
            previous = existing.get(key)
            if status is not None and status not in REVIEW_STATUSES:
                # 선택지에 없는 값은 버리지 않고 알린다. 상태는 이전 값을 지킨다.
                invalid.append(f"{comment_id}: {status}")
                status = previous[0] if previous else None
            if (status, note) != (previous or (None, None)):
                updates.append((*key, status, note))
        return ReviewImport(True, db.save_reviews(updates), tuple(invalid))
    finally:
        workbook.close()


def _cell(sheet, value: Any) -> Any:
    """칸에 넣을 값. 문자열은 엑셀이 받지 못하는 문자를 빼고, 수식으로 읽히지 않게 한다."""
    if isinstance(value, Link):
        target = value.path.replace("\\", "/").replace('"', '""')
        return WriteOnlyCell(sheet, value=f'=HYPERLINK("{target}","{value.label}")')
    if isinstance(value, Percent):
        cell = WriteOnlyCell(sheet, value=value.value)
        cell.number_format = "0.0%"
        return cell
    if not isinstance(value, str):
        return value
    value = ILLEGAL_CHARACTERS_RE.sub("", value)[:MAX_CELL_CHARS]
    if not value.startswith("="):
        # 문자열로 저장된 칸은 +, -, @로 시작해도 엑셀이 수식으로 풀지 않는다.
        return value
    cell = WriteOnlyCell(sheet, value=value)
    cell.data_type = "s"  # =로 시작해도 수식이 아닌 문자열로 기록한다
    return cell


def _header_cell(sheet, text: str, color: str = _DEFAULT_FILL) -> Any:
    cell = WriteOnlyCell(sheet, value=text)
    cell.font = _HEADER_FONT
    cell.alignment = _HEADER_ALIGNMENT
    cell.fill = PatternFill("solid", start_color=color, end_color=color)
    return cell


def _write_grid(workbook: Workbook, spec: GridSpec, db: Database) -> int:
    grid, count = spec.build(db)
    sheet = workbook.create_sheet(spec.title)
    widest = max((len(cells) for _, cells in grid), default=0)
    for index in range(1, widest + 1):
        width = spec.widths[index - 1] if index <= len(spec.widths) else 14
        sheet.column_dimensions[get_column_letter(index)].width = width
    for kind, cells in grid:
        if kind == "header":
            sheet.append([_header_cell(sheet, text) for text in cells])
        elif kind in ("title", "note"):
            cell = WriteOnlyCell(sheet, value=cells[0])
            cell.font = _TITLE_FONT if kind == "title" else _NOTE_FONT
            sheet.append([cell])
        else:
            sheet.append([_cell(sheet, value) for value in cells])
    return count


def _write_sheet(workbook: Workbook, spec: SheetSpec | GridSpec, db: Database) -> int:
    if isinstance(spec, GridSpec):
        return _write_grid(workbook, spec, db)
    rows = spec.rows(db)
    if spec.count is None:
        rows = list(rows)
        total = len(rows)
    else:
        total = spec.count(db)
    if total + 1 > MAX_ROWS:
        raise ValueError(
            f"'{spec.title}' 탭이 엑셀의 행 수 한도({MAX_ROWS:,})를 넘습니다: {total:,}건"
        )
    sheet = workbook.create_sheet(spec.title)
    # 쓰기 전용 모드에서는 틀 고정, 필터, 열 너비를 행보다 먼저 정해야 한다.
    last_column = get_column_letter(len(spec.columns))
    sheet.freeze_panes = "B2"
    sheet.auto_filter.ref = f"A1:{last_column}{total + 1}"
    header = []
    for index, column in enumerate(spec.columns, start=1):
        sheet.column_dimensions[get_column_letter(index)].width = column.width
        header.append(
            _header_cell(sheet, column.header, _GROUP_FILLS.get(column.group, _DEFAULT_FILL))
        )
        if column.choices:
            # 선택지 밖의 값은 엑셀이 입력을 막는다.
            validation = DataValidation(
                type="list", formula1='"' + ",".join(column.choices) + '"', allow_blank=True,
                showErrorMessage=True, errorTitle=column.header,
                error=" / ".join(column.choices) + " 중에서 골라 주세요.",
            )
            letter = get_column_letter(index)
            validation.add(f"{letter}2:{letter}{max(total + 1, 2)}")
            sheet.data_validations.append(validation)
    sheet.append(header)

    written = 0
    for row in rows:
        sheet.append([_cell(sheet, column.value(row)) for column in spec.columns])
        written += 1
    return written


def export_excel(
    db: Database,
    path: Path,
    *,
    sheets: Sequence[SheetSpec | GridSpec] = SHEETS,
    now: datetime | None = None,
) -> ExportResult:
    """DB 기준으로 엑셀 파일을 통째로 다시 만든다.

    엑셀에서 파일을 열어둔 채라 덮어쓸 수 없으면 시각을 붙인 다른 이름으로 저장한다.
    """
    workbook = Workbook(write_only=True)
    rows = {spec.title: _write_sheet(workbook, spec, db) for spec in sheets}
    units = {spec.title: spec.unit for spec in sheets}

    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        workbook.save(path)
        return ExportResult(path, rows, renamed=False, units=units)
    except PermissionError:
        stamp = (now or datetime.now(KST)).strftime("%Y%m%d_%H%M%S")
        fallback = path.with_name(f"{path.stem}_{stamp}{path.suffix}")
        workbook.save(fallback)
        return ExportResult(fallback, rows, renamed=True, units=units)
