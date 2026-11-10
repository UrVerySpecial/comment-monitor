"""모델 비교 결과의 엑셀 출력. 판정이 갈린 댓글과 요약을 담는다."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..collectors.youtube import comment_url
from ..compare import FIELDS, SERIOUS, Compared, Comparison, ModelRun, ratio
from ..db import Database
from .excel import Column, ExportResult, GridSpec, Percent, SheetSpec, export_excel

FILE_NAME = "모델비교.xlsx"
DIFF_TITLE = "판정 차이"
SUMMARY_TITLE = "요약"


def compare_path(export_path: Path) -> Path:
    """비교 결과 파일의 위치. 증거 엑셀과 같은 폴더에 둔다."""
    return export_path.with_name(FILE_NAME)


def _note(item: Compared, reference: str, candidate: str) -> str:
    if item.missed:
        return f"{candidate}가 부정을 놓침"
    if item.extra:
        return f"{candidate}만 부정으로 봄"
    return ""


def _judgment_columns(model: str, side: str, group: str) -> list[Column]:
    def value(name: str):
        return lambda item: getattr(getattr(item, side), name)

    columns = [Column(f"{model}\n{label}", group, 15, value(name)) for label, name in FIELDS]
    columns.append(Column(f"{model}\n근거", group, 45, value("reason")))
    return columns


def diff_sheet(comparison: Comparison) -> SheetSpec:
    reference, candidate = comparison.reference.model, comparison.candidate.model
    row = lambda name: (lambda item: item.row[name])  # noqa: E731
    columns = (
        Column("증거 번호", "식별", 10, row("evidence_no")),
        Column("댓글 ID", "식별", 30, row("comment_id")),
        Column("댓글 주소", "위치", 40,
               lambda item: comment_url(item.row["video_id"], item.row["comment_id"])),
        Column("영상 제목", "위치", 40, row("video_title")),
        Column("댓글 본문", "내용", 70, row("text")),
        Column("원댓글", "내용", 50, row("parent_text")),
        Column("갈린 항목", "점수", 22, lambda item: ", ".join(item.differs)),
        Column("심각도 차이", "점수", 10, lambda item: item.severity_gap),
        Column("구분", "점수", 26, lambda item: _note(item, reference, candidate)),
        *_judgment_columns(reference, "reference", "AI 판정"),
        *_judgment_columns(candidate, "candidate", "사람 입력"),
    )
    return SheetSpec(DIFF_TITLE, columns, rows=lambda db: comparison.to_review())


def _usage_row(run: ModelRun) -> list[Any]:
    served = ", ".join(name for name in run.served_by if name != run.model)
    per_comment = run.krw_per_comment
    return [
        run.model, run.requests, run.tokens["input_tokens"],
        run.tokens["cache_creation_input_tokens"], run.tokens["cache_read_input_tokens"],
        run.tokens["output_tokens"], run.judged, run.failed, run.refusals,
        round(run.cost_krw, 1), None if per_comment is None else round(per_comment, 2),
        f"대체 모델 응답 {run.fallback_requests}회 ({served})" if run.fallback_requests else "",
    ]


def summary_sheet(comparison: Comparison) -> GridSpec:
    def build(db: Database) -> tuple[list[tuple[str, list[Any]]], int]:
        reference, candidate = comparison.reference, comparison.candidate
        both, different = len(comparison.both), len(comparison.different)
        negative = len(comparison.reference_negative)
        both_negative = len(comparison.both_negative)
        grid: list[tuple[str, list[Any]]] = [
            ("title", ["비교 개요"]),
            ("row", ["기준 모델", reference.model]),
            ("row", ["비교 모델", candidate.model]),
            ("row", ["비교한 댓글 수", len(comparison.items)]),
            ("row", ["두 모델 모두 판정한 댓글 수", both]),
            ("row", ["  네 항목이 모두 같은 댓글", both - different,
                     Percent(ratio(both - different, both))]),
            ("row", ["  한 항목이라도 갈린 댓글", different, Percent(ratio(different, both))]),
            ("row", ["한쪽이라도 분류에 실패한 댓글 수", len(comparison.one_sided)]),
        ]
        if len(comparison.items) < comparison.requested:
            grid.append(("note", [
                f"{comparison.requested:,}건을 비교하려다 중간에 멈춰 "
                f"{len(comparison.items):,}건만 비교했다."
            ]))
        grid += [
            ("blank", []),
            ("title", ["항목별"]),
            ("header", ["항목", "갈린 건수", "일치율"]),
        ]
        for label, count in comparison.field_differences().items():
            grid.append(("row", [label, count, Percent(ratio(both - count, both))]))
        missed, serious = len(comparison.missed), len(comparison.missed_serious)
        grid += [
            ("blank", []),
            ("title", [f"부정 댓글 (기준: {reference.model})"]),
            ("row", ["기준 모델이 부정으로 본 댓글", negative]),
            ("row", ["  비교 모델도 부정으로 본 댓글", both_negative,
                     Percent(ratio(both_negative, negative))]),
            ("row", ["  비교 모델이 놓친 댓글", missed, Percent(ratio(missed, negative))]),
            ("row", [f"    그중 기준 심각도 {SERIOUS} 이상", serious]),
            ("row", ["비교 모델만 부정으로 본 댓글", len(comparison.extra)]),
            ("row", ["둘 다 부정일 때 심각도 차이가 1 이하", comparison.severity_close,
                     Percent(ratio(comparison.severity_close, both_negative))]),
            ("note", ["기준 모델의 판정도 정답은 아니다. 갈린 댓글은 '판정 차이' 탭에서 직접 확인한다."]),
            ("blank", []),
            ("title", ["모델별 실제 사용량과 비용"]),
            ("header", [
                "모델", "요청 수", "입력 토큰", "캐시 쓰기 토큰", "캐시 읽기 토큰", "출력 토큰",
                "판정 건수", "분류 실패", "거부된 요청", "실제 비용 (원)", "댓글 한 건당 (원)", "비고",
            ]),
            ("row", _usage_row(reference)),
            ("row", _usage_row(candidate)),
        ]
        if candidate.cost_krw and reference.cost_krw:
            grid.append(("row", [
                "비용 비율 (기준 ÷ 비교)", round(reference.cost_krw / candidate.cost_krw, 1),
            ]))
        return grid, len(comparison.items)

    return GridSpec(SUMMARY_TITLE, build, widths=(40, 26, 14, 16, 16, 14, 12, 12, 12, 16, 18, 36))


def export_comparison(db: Database, path: Path, comparison: Comparison) -> ExportResult:
    return export_excel(db, path, sheets=(summary_sheet(comparison), diff_sheet(comparison)))
