"""명령 진입점.

`run`은 검토 결과 읽기부터 수집, 분류, 캡처, 엑셀 출력까지 한 번에 한다(DESIGN.md 4장).
나머지 명령은 그 단계를 하나씩 따로 실행한다. 2단 분류는 `run --two-pass`로 켠다.
"""

from __future__ import annotations

import argparse
import json
import sys
import unicodedata
from collections.abc import Sequence

from . import __version__
from . import classifier as classifier_module
from .capture import (
    CAPTURE_DIR,
    Camera,
    CaptureError,
    Capturer,
    CaptureSummary,
    YouTubeCamera,
    check_captures,
)
from .classifier import Classifier, ClassifierError, ClassifySummary, TwoPassClassifier
from .collectors.base import (
    CAPPED,
    ERROR,
    SKIPPED,
    STOPPED,
    CollectionSummary,
    CollectorError,
    PlannedVideo,
    QuotaStop,
    VideoResult,
    run_collection,
)
from .compare import Comparer, Comparison, ModelRun, compare_cost_krw
from .collectors.youtube import Transport, YouTubeClient, YouTubeCollector, http_transport
from .config import (
    DEFAULT_CONFIG_NAME,
    Config,
    ConfigError,
    apply_overrides,
    load_config,
    require_env,
)
from .db import Database, DatabaseError, to_kst
from .exporters.compare import compare_path, export_comparison
from .exporters.author_folders import AUTHORS_DIR, export_author_folders
from .exporters.excel import ExportError, build_sheets, export_excel, import_reviews
from .planner import (
    Plan,
    build_plan,
    classify_cost_krw,
    register_plan,
    source_label,
    spent_krw,
)

# 명령과 설명
_COMMANDS = {
    "run": "검토 결과 읽기부터 수집, 분류, 캡처, 엑셀 출력까지 한 번에 실행한다",
    "plan": "대상 영상과 예상 규모, 비용만 보여준다. 댓글은 수집하지 않는다",
    "collect": "대상 영상을 확정하고 댓글을 수집한다",
    "classify": "미분류 댓글을 AI로 분류한다",
    "capture": "캡처 대상 댓글을 브라우저로 열어 찍는다",
    "export": "DB 기준으로 엑셀 파일을 다시 만든다",
    "status": "DB 현황, 오늘 할당량 사용량, 미분류와 미캡처 건수를 보여준다",
    "reclassify": "분류 기준을 바꾼 뒤 다시 분류한다. 이전 판정은 이력으로 남는다",
    "recapture": "캡처 기록을 실제 파일과 비교하고, 파일이 없는 캡처를 다시 찍는다",
    "folders": "랭킹 작성자별 폴더에 캡처를 복사해 모은다. 폴더는 매번 지우고 다시 만든다",
}

# 확인 질문에 아니오라고 답한 단계의 반환값. 밖으로는 종료 코드 0으로 나간다.
CANCELLED = -1


def _positive_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"정수여야 합니다: {text!r}") from None
    if value < 1:
        raise argparse.ArgumentTypeError(f"1 이상이어야 합니다: {value}")
    return value


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--config", default=DEFAULT_CONFIG_NAME, metavar="경로",
        help=f"설정 파일 (기본값: {DEFAULT_CONFIG_NAME})",
    )
    common.add_argument(
        "--limit", type=_positive_int, metavar="N", help="이번 실행의 분류와 캡처 건수 제한"
    )
    common.add_argument(
        "--video", action="append", metavar="URL", help="특정 영상만 처리 (여러 번 줄 수 있음)"
    )
    common.add_argument("--no-search", action="store_true", help="키워드 검색을 건너뜀")
    common.add_argument(
        "--search-top", type=_positive_int, metavar="N", help="키워드당 상위 N개 영상만 처리"
    )
    common.add_argument(
        "--refresh-search", action="store_true", help="저장된 검색 결과를 쓰지 않고 다시 검색"
    )
    common.add_argument(
        "--max-comments", type=_positive_int, metavar="N", help="영상당 댓글 상한"
    )
    common.add_argument(
        "--max-videos", type=_positive_int, metavar="N",
        help="대상 영상 수 상한. 댓글이 많은 영상부터 N개만 처리 (기본값: 설정의 "
             "limits.max_videos_per_run, 50)",
    )
    scan = common.add_mutually_exclusive_group()
    scan.add_argument("--full", action="store_true", help="큰 영상도 전체 재확인")
    scan.add_argument("--quick", action="store_true", help="모든 영상을 새 댓글만 확인")
    common.add_argument("--yes", action="store_true", help="확인 질문 없이 진행")

    parser = argparse.ArgumentParser(
        prog="comment-monitor", description="댓글 증거 수집기"
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", required=True, metavar="명령")
    for name, description in _COMMANDS.items():
        sub = commands.add_parser(name, parents=[common], help=description, description=description)
        if name == "classify":
            sub.add_argument(
                "--compare", type=_positive_int, metavar="N",
                help="최신 댓글 N건을 model과 first_pass_model로 각각 분류해 갈린 판정을 "
                     "엑셀로 내보낸다 (판정은 저장하지 않는다)",
            )
        if name in ("run", "capture", "recapture"):
            sub.add_argument(
                "--headed", action="store_true",
                help="브라우저 창을 띄워 캡처 과정을 보여준다 (capture.headless 설정과 무관하게)",
            )
        if name == "run":
            sub.add_argument(
                "--two-pass", action="store_true",
                help="2단 분류: first_pass_model이 전체를 1차 분류하고 1차 부정만 model이 "
                     "최종 판정한다 (기본값: 끔, model 하나로 분류)",
            )
    return parser


def make_transport() -> Transport:
    return http_transport(require_env("YOUTUBE_API_KEY"))


def make_classifier_client():
    return classifier_module.make_client(require_env("ANTHROPIC_API_KEY"))


def make_camera(*, headless: bool) -> Camera:
    """캡처용 브라우저. with로 열고 닫는다."""
    return YouTubeCamera(headless=headless)


def _width(text: str) -> int:
    """터미널에서 차지하는 칸 수. 한글은 두 칸이다."""
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


def _fit(text: str, width: int) -> str:
    """칸 수에 맞춰 자르거나 오른쪽을 채운다."""
    text = " ".join(text.split())
    if _width(text) > width:
        while _width(text) > width - 1:
            text = text[:-1]
        text += "…"
    return text + " " * (width - _width(text))


def _video_line(index: int, video: PlannedVideo) -> str:
    info = video.info
    return (
        f"{index:>4}  {_fit(source_label(video.source), 4)}  {_fit(video.label, 11)}  "
        f"{info.comment_count:>9,}  {video.est_units:>6,}  {'신규' if video.is_new else '    '}  "
        f"{_fit(info.title, 44)}  {_fit(info.channel_title, 16)}"
    ).rstrip()


def _duration(seconds: int) -> str:
    minutes = round(seconds / 60)
    if minutes < 60:
        return f"{minutes}분"
    return f"{minutes // 60}시간 {minutes % 60}분"


def _models_label(config: Config, two_pass: bool) -> str:
    classifier = config.classifier
    if two_pass:
        return f"2단 분류 {classifier.first_pass_model} → {classifier.model}"
    return classifier.model


def _print_plan(plan: Plan, config: Config, *, two_pass: bool = False) -> None:
    estimate = plan.estimate
    limits = config.limits
    by_source: dict[str, int] = {}
    for video in plan.videos:
        by_source[source_label(video.source)] = by_source.get(source_label(video.source), 0) + 1
    breakdown = " · ".join(f"{label} {count}" for label, count in by_source.items())
    print(f"이번에 처리할 영상: {len(plan.videos)}개" + (f" ({breakdown})" if breakdown else ""))
    if plan.videos:
        print(
            f"{'#':>4}  경로  {_fit('수집 방식', 11)}    댓글 수  {'units':>6}  "
            f"      {_fit('제목', 44)}  채널"
        )
        for index, video in enumerate(plan.videos, start=1):
            print(_video_line(index, video))

    print()
    print("[견적]")
    print(
        f"유튜브 할당량: 예상 {estimate.units:,} units · 오늘 사용 {estimate.quota_used:,} / "
        f"예산 {estimate.quota_budget:,} (남은 {estimate.quota_left:,})"
    )
    if plan.searches_run:
        print(
            f"키워드 검색: 방금 {plan.searches_run}회 실행 ({plan.searches_run * 100:,} units, "
            f"위 오늘 사용량에 포함) · 저장된 결과 재사용 {plan.searches_cached}회"
        )
    elif plan.searches_cached:
        print(f"키워드 검색: 저장된 결과 재사용 {plan.searches_cached}회 (검색 할당량 0)")
    print(f"새로 수집될 댓글: 약 {estimate.new_comments:,}건")
    models = _models_label(config, two_pass)
    print(
        f"분류(classify 실행 시): {estimate.classify_count:,}건 · "
        f"약 {estimate.classify_cost_krw:,}원 ({models})"
        + (
            f" · 확인 기준 {limits.confirm_cost_over_krw:,}원 초과"
            if estimate.cost_needs_confirm else ""
        )
    )
    rate_note = "자료가 없어 가정" if estimate.capture_rate_assumed else "지금까지의 실제 비율"
    print(
        f"캡처(capture 실행 시): 약 {estimate.capture_count:,}건 · "
        f"약 {_duration(estimate.capture_seconds)} "
        f"(캡처 대상 비율 {estimate.capture_rate:.1%}, {rate_note})"
    )

    leftovers = []
    if estimate.over_budget:
        leftovers.append(
            "예상 할당량이 오늘 남은 예산을 넘습니다. 수집이 중간에 멈추고 다음 실행이 이어서 합니다."
        )
    if plan.deferred:
        leftovers.append(
            f"영상 수 상한({limits.max_videos_per_run}개, 댓글 많은 순)에 들지 못해 빠지는 영상: "
            f"{len(plan.deferred)}개 (--max-videos로 늘릴 수 있습니다)"
        )
    if estimate.over_cap_comments:
        leftovers.append(
            f"영상당 댓글 상한({limits.max_comments_per_video:,}건)으로 가져오지 않는 댓글: "
            f"약 {estimate.over_cap_comments:,}건"
        )
    if estimate.classify_left:
        leftovers.append(f"분류 상한으로 미분류로 남는 댓글: 약 {estimate.classify_left:,}건")
    if estimate.capture_left:
        leftovers.append(f"캡처 상한으로 다음 실행에 넘기는 캡처: 약 {estimate.capture_left:,}건")
    if leftovers:
        print()
        print("[이번에 처리되지 않는 것]")
        for line in leftovers:
            print(f"- {line}")
        for video in plan.deferred:
            print(f"    {_fit(source_label(video.source), 4)}  {video.info.comment_count:>9,}  "
                  f"{_fit(video.info.title, 44)}  {video.info.video_id}")

    others = [
        *(f"댓글이 막힌 영상: {info.title} ({info.video_id})" for info in plan.disabled),
        *(f"조회되지 않는 영상(삭제 또는 비공개): {video_id}" for video_id in plan.unavailable),
        *(f"제외 목록에 있어 뺀 영상: {video_id}" for video_id in plan.excluded),
        *plan.notes,
    ]
    if others:
        print()
        print("[참고]")
        for line in others:
            print(f"- {line}")


def _confirm(question: str, *, yes: bool) -> bool:
    if yes:
        return True
    try:
        answer = input(f"{question} [y/N] ")
    except EOFError:
        return False
    return answer.strip().lower() in ("y", "yes", "ㅛ")


_OUTCOME_NOTES = {CAPPED: "상한 도달", STOPPED: "중단", SKIPPED: "건너뜀", ERROR: "오류"}


def _result_line(index: int, total: int, result: VideoResult) -> str:
    line = (
        f"[{index + 1}/{total}] {_fit(result.title, 44)}  새 댓글 {result.new:,} · "
        f"수정 {result.edited:,} · 조회 불가 {result.missing:,}"
    )
    if result.outcome in _OUTCOME_NOTES:
        line += f"  ({_OUTCOME_NOTES[result.outcome]}: {result.note})"
    return line


def _print_summary(summary: CollectionSummary, plan: Plan, quota_used: int) -> None:
    print()
    print("[수집 결과]")
    print(
        f"새 댓글 {summary.new:,}건 · 수정 감지 {summary.edited:,}건 · "
        f"조회 불가 감지 {summary.missing:,}건"
    )
    if summary.recheck.checked:
        print(f"부정 판정 댓글 재조회: {summary.recheck.checked:,}건")
    print(f"이번 실행의 할당량 사용: {quota_used:,} units")
    if summary.stopped_reason:
        print(f"수집을 멈췄습니다: {summary.stopped_reason}")
        print(f"- 처리하지 못한 영상 {len(summary.not_reached)}개는 다음 실행이 이어서 합니다.")
    if plan.deferred:
        print(
            f"- 댓글 수 순위가 영상 수 상한 밖인 영상 {len(plan.deferred)}개는 처리하지 않았습니다. "
            "--max-videos로 상한을 늘릴 수 있습니다."
        )


def _collect(config: Config, db: Database, args: argparse.Namespace, *, plan_only: bool) -> int:
    transport = make_transport()
    two_pass = getattr(args, "two_pass", False)
    options = {
        key: value for key, value in vars(args).items()
        if key not in ("command", "config") and value not in (None, False)
    }
    run_id = db.start_run(args.command, options)
    client = YouTubeClient(
        transport, db, daily_budget=config.youtube.daily_quota_budget, run_id=run_id
    )
    collector_platform = YouTubeCollector.platform
    limits_hit: list[str] = []
    counts: dict[str, int] = {}
    error = None
    try:
        plan = build_plan(
            config, db, client, full=args.full, quick=args.quick,
            refresh_search=args.refresh_search, limit=args.limit, two_pass=two_pass,
        )
        _print_plan(plan, config, two_pass=two_pass)
        if plan_only:
            return 0
        if not plan.videos and not db.negative_comments(collector_platform):
            print()
            print("수집할 영상이 없습니다. config.yaml의 youtube 항목을 확인해 주세요.")
            return 0
        print()
        if not _confirm("이대로 수집을 진행할까요?", yes=args.yes):
            print("취소했습니다. 아무것도 수집하지 않았습니다.")
            return CANCELLED

        register_plan(db, plan, config)
        collector = YouTubeCollector(
            db, client, max_comments=config.limits.max_comments_per_video
        )
        summary = run_collection(
            collector, plan.videos,
            on_result=lambda index, result: print(
                _result_line(index, len(plan.videos), result), flush=True
            ),
        )
        counts = {
            "videos": len(summary.results),
            "new": summary.new,
            "edited": summary.edited,
            "missing": summary.missing,
            "rechecked": summary.recheck.checked,
        }
        if plan.deferred:
            limits_hit.append("max_videos_per_run")
        if any(result.outcome == CAPPED for result in summary.results):
            limits_hit.append("max_comments_per_video")
        if summary.stopped_reason:
            limits_hit.append("daily_quota_budget")
        used = db.scalar(
            "SELECT COALESCE(SUM(units), 0) FROM quota_usage WHERE run_id = ?", (run_id,)
        )
        _print_summary(summary, plan, used)
        return 0
    except QuotaStop as exc:
        limits_hit.append("daily_quota_budget")
        print(f"대상 영상을 확정하는 중에 멈췄습니다: {exc}", file=sys.stderr)
        return 1
    except CollectorError as exc:
        error = str(exc)
        print(f"오류: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        error = "사용자가 중단함"
        print("\n중단했습니다. 저장된 데까지는 남아 있고 다음 실행이 이어서 합니다.", file=sys.stderr)
        return 130
    finally:
        db.finish_run(run_id, counts=counts, limits_hit=limits_hit, error=error)


def _breakdown(counts: dict[str, int], order: Sequence[str]) -> str:
    keys = [key for key in order if key in counts] + [key for key in counts if key not in order]
    return " · ".join(f"{key} {counts[key]:,}" for key in keys)


def _print_classify_summary(
    summary: ClassifySummary, config: Config, db: Database, run_id: int, left: int
) -> None:
    print()
    print("[분류 결과]")
    line = f"분류 {summary.classified:,}건"
    if summary.sentiments:
        line += f" ({_breakdown(summary.sentiments, ('긍정', '중립', '부정'))})"
    print(f"{line} · 분류 실패 {summary.failed:,}건")
    if summary.negative_targets:
        print(f"부정 댓글의 대상: {_breakdown(summary.negative_targets, config.target_values)}")
    notes = [f"요청 {summary.requests:,}회"]
    if summary.rerequested:
        notes.append(f"응답에서 빠져 다시 요청한 댓글 {summary.rerequested:,}건")
    if summary.refusals:
        notes.append(f"거부된 요청 {summary.refusals:,}회")
    if summary.fallback_requests:
        notes.append(f"대체 모델이 응답한 요청 {summary.fallback_requests:,}회")
    print(" · ".join(notes))
    usage = db.token_usage_by_model(run_id=run_id)
    if usage:
        tokens_in = sum(
            row["input_tokens"] + row["cache_creation_input_tokens"]
            + row["cache_read_input_tokens"] for row in usage
        )
        cached = sum(row["cache_read_input_tokens"] for row in usage)
        tokens_out = sum(row["output_tokens"] for row in usage)
        cost = spent_krw(db, run_id=run_id)
        per_comment = f" · 댓글 한 건당 약 {cost / summary.done:,.1f}원" if summary.done else ""
        print(
            f"토큰: 입력 {tokens_in:,} (캐시 적중 {cached:,}) · 출력 {tokens_out:,} · "
            f"실제 비용 약 {cost:,.0f}원{per_comment}"
        )
    if summary.failed:
        print("- 분류 실패 건은 `reclassify`로 다시 시도할 수 있습니다.")
    if left:
        print(f"- 아직 분류하지 않은 댓글 {left:,}건은 다음 실행에서 이어서 분류합니다.")


def _classify(config: Config, db: Database, args: argparse.Namespace, *, again: bool) -> int:
    """미분류 댓글을 분류한다. again이면 실패했거나 기준이 바뀐 판정도 다시 분류한다."""
    settings = config.classifier
    if getattr(args, "compare", None):
        return _compare(config, db, args)
    two_pass = getattr(args, "two_pass", False)

    restale = (settings.prompt_version, config.target_values) if again else None
    pending = db.count_to_classify(restale=restale)
    if not pending:
        print(
            "다시 분류할 댓글이 없습니다. 기준을 바꿨다면 classifier.prompt_version을 올려 주세요."
            if again else "분류할 댓글이 없습니다."
        )
        return 0
    limits = config.limits
    count = min(pending, limits.max_classify_per_run, args.limit or pending)
    cost = classify_cost_krw(config, count, db, two_pass=two_pass)
    basis = "지금까지의 실제 사용량 기준" if db.token_usage_by_model() else "추정치"
    print(
        f"분류 대상: {pending:,}건 중 {count:,}건 (최신 댓글부터) · "
        f"모델 {_models_label(config, two_pass)} · 프롬프트 {settings.prompt_version}"
    )
    print(f"예상 비용: 약 {cost:,}원 ({basis})")
    if cost > limits.confirm_cost_over_krw and not _confirm(
        f"예상 비용이 확인 기준({limits.confirm_cost_over_krw:,}원)을 넘습니다. 진행할까요?",
        yes=args.yes,
    ):
        print("취소했습니다. 아무것도 분류하지 않았습니다.")
        return CANCELLED

    client = make_classifier_client()
    options = {
        key: value for key, value in vars(args).items()
        if key not in ("command", "config") and value not in (None, False)
    }
    run_id = db.start_run(args.command, options)
    limits_hit = []
    if count < pending:
        limits_hit.append(
            "limit" if args.limit and args.limit <= limits.max_classify_per_run
            else "max_classify_per_run"
        )
    worker: Classifier | TwoPassClassifier | None = None
    error = None
    try:
        worker = (
            TwoPassClassifier(config, db, client, run_id=run_id) if two_pass
            else Classifier(config, db, client, run_id=run_id)
        )
        rows = db.comments_to_classify(limit=count, restale=restale)
        worker.run(
            rows,
            on_progress=lambda s: print(
                f"[{s.done:,}/{s.total:,}] 분류 {s.classified:,} · 실패 {s.failed:,} · "
                f"부정 {s.sentiments.get('부정', 0):,}",
                flush=True,
            ),
        )
        return 0
    except ClassifierError as exc:
        error = str(exc)
        print(f"오류: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        error = "사용자가 중단함"
        print("\n중단했습니다. 저장된 판정은 남아 있고 다음 실행이 이어서 합니다.", file=sys.stderr)
        return 130
    finally:
        summary = worker.summary if worker is not None else ClassifySummary()
        counts = {
            "classified": summary.classified, "failed": summary.failed,
            "requests": summary.requests, "refusals": summary.refusals,
            "rerequested": summary.rerequested,
            "fallback_requests": summary.fallback_requests,
        }
        if isinstance(worker, TwoPassClassifier):
            counts["escalated"] = worker.escalated
        db.finish_run(run_id, counts=counts, limits_hit=limits_hit, error=error)
        if summary.requests:
            _print_classify_summary(
                summary, config, db, run_id, db.count_to_classify(restale=restale)
            )
            if isinstance(worker, TwoPassClassifier):
                print(
                    f"2단 분류: 1차({settings.first_pass_model})에서 부정이거나 1차 분류에 실패한 "
                    f"{worker.escalated:,}건을 {settings.model}이 최종 판정했습니다."
                )


def _percent(part: int, whole: int) -> str:
    return f" ({part / whole:.1%})" if whole else ""


def _print_model_usage(run: ModelRun) -> None:
    line = (
        f"- {run.model}: 요청 {run.requests:,}회 · 입력 {run.input_total:,} "
        f"(캐시 적중 {run.tokens['cache_read_input_tokens']:,}) · "
        f"출력 {run.tokens['output_tokens']:,} · 실제 비용 약 {run.cost_krw:,.1f}원"
    )
    if run.krw_per_comment is not None:
        line += f" · 댓글 한 건당 약 {run.krw_per_comment:,.2f}원"
    print(line)
    notes = []
    if run.failed:
        notes.append(f"분류 실패 {run.failed:,}건")
    if run.refusals:
        notes.append(f"거부된 요청 {run.refusals:,}회")
    if run.fallback_requests:
        others = ", ".join(name for name in run.served_by if name != run.model)
        notes.append(f"대체 모델이 응답한 요청 {run.fallback_requests:,}회 ({others})")
    if notes:
        print("  " + " · ".join(notes))


def _print_comparison(comparison: Comparison) -> None:
    reference, candidate = comparison.reference, comparison.candidate
    both, different = len(comparison.both), len(comparison.different)
    print()
    print("[모델 비교 결과]")
    print(f"기준 {reference.model} · 비교 {candidate.model}")
    line = f"비교한 댓글 {len(comparison.items):,}건 · 두 모델 모두 판정 {both:,}건"
    if comparison.one_sided:
        line += f" · 한쪽이라도 분류 실패 {len(comparison.one_sided):,}건"
    print(line)
    print(
        f"네 항목이 모두 같은 댓글 {both - different:,}건{_percent(both - different, both)} · "
        f"갈린 댓글 {different:,}건{_percent(different, both)}"
    )
    print("항목별 갈린 건수: " + _breakdown(comparison.field_differences(), ()))
    negative, missed = len(comparison.reference_negative), len(comparison.missed)
    print(
        f"기준 모델이 부정으로 본 {negative:,}건 중 비교 모델이 놓친 댓글 "
        f"{missed:,}건{_percent(missed, negative)}, 그중 심각도 3 이상 "
        f"{len(comparison.missed_serious):,}건 · 비교 모델만 부정으로 본 댓글 "
        f"{len(comparison.extra):,}건"
    )
    close, both_negative = comparison.severity_close, len(comparison.both_negative)
    if both_negative:
        print(
            f"둘 다 부정으로 본 {both_negative:,}건 중 심각도 차이가 1 이하 "
            f"{close:,}건{_percent(close, both_negative)}"
        )
    print("모델별 실제 사용량:")
    _print_model_usage(reference)
    _print_model_usage(candidate)
    if reference.cost_krw and candidate.cost_krw:
        print(f"  비용은 기준 모델이 비교 모델의 약 {reference.cost_krw / candidate.cost_krw:,.0f}배")


def _compare(config: Config, db: Database, args: argparse.Namespace) -> int:
    """최신 댓글 N건을 두 모델로 각각 분류해 갈린 판정을 엑셀로 내보낸다."""
    settings, limits = config.classifier, config.limits
    if settings.model == settings.first_pass_model:
        print(
            "classifier.model과 classifier.first_pass_model이 같아 비교할 수 없습니다.",
            file=sys.stderr,
        )
        return 1
    total = db.scalar("SELECT COUNT(*) FROM comments")
    if not total:
        print("비교할 댓글이 없습니다. 먼저 collect로 댓글을 수집해 주세요.")
        return 0
    count = min(args.compare, total, limits.max_classify_per_run)
    cost = compare_cost_krw(config, count, db)
    print(
        f"모델 비교: 최신 댓글 {count:,}건 · 기준 {settings.model} · "
        f"비교 {settings.first_pass_model} · 프롬프트 {settings.prompt_version}"
    )
    if count < args.compare:
        reason = "수집된 댓글이 그만큼 없어서" if count == total else "max_classify_per_run 상한 때문에"
        print(f"- {reason} {args.compare:,}건이 아니라 {count:,}건만 비교합니다.")
    print(f"예상 비용: 약 {cost:,}원 (두 모델 합계)")
    print("- 여기서 나온 판정은 저장하지 않습니다. 기존 판정과 증거 엑셀은 바뀌지 않습니다.")
    if cost > limits.confirm_cost_over_krw and not _confirm(
        f"예상 비용이 확인 기준({limits.confirm_cost_over_krw:,}원)을 넘습니다. 진행할까요?",
        yes=args.yes,
    ):
        print("취소했습니다. 아무것도 분류하지 않았습니다.")
        return 0

    client = make_classifier_client()
    run_id = db.start_run(args.command, {"compare": args.compare})
    comparer = None
    error = None
    code = 0
    try:
        comparer = Comparer(config, db, client, run_id=run_id)
        comparer.run(
            db.comments_to_compare(count),
            on_progress=lambda c: print(
                f"[{len(c.items):,}/{c.requested:,}] 갈린 댓글 {len(c.different):,} · "
                f"놓친 부정 {len(c.missed):,}",
                flush=True,
            ),
        )
    except ClassifierError as exc:
        error, code = str(exc), 1
        print(f"오류: {exc}", file=sys.stderr)
    except KeyboardInterrupt:
        error, code = "사용자가 중단함", 130
        print("\n중단했습니다. 여기까지 비교한 결과만 정리합니다.", file=sys.stderr)
    comparison = comparer.result() if comparer else None
    counts = {}
    if comparison:
        counts = {
            "compared": len(comparison.items), "different": len(comparison.different),
            "missed_negative": len(comparison.missed),
            "requests": comparison.reference.requests + comparison.candidate.requests,
        }
    db.finish_run(run_id, counts=counts, error=error)
    if not comparison or not comparison.items:
        if comparison and (comparison.reference.requests or comparison.candidate.requests):
            print("모델별 실제 사용량:")
            _print_model_usage(comparison.reference)
            _print_model_usage(comparison.candidate)
        return code
    _print_comparison(comparison)
    result = export_comparison(db, compare_path(config.export.path), comparison)
    if result.renamed:
        print("기존 비교 파일이 열려 있어 다른 이름으로 저장했습니다.")
    print(f"판정이 갈린 댓글 {len(comparison.different):,}건을 저장: {result.path}")
    return code


def _capture(config: Config, db: Database, args: argparse.Namespace) -> int:
    settings, limits = config.capture, config.limits
    scope = {"min_severity": settings.min_severity, "targets": settings.targets}
    pending = db.count_to_capture(**scope)
    if not pending:
        print("캡처할 댓글이 없습니다.")
        return 0
    count = min(pending, limits.max_captures_per_run, args.limit or pending)
    headless = settings.headless and not args.headed
    folder = config.export.path.parent / CAPTURE_DIR
    print(
        f"캡처 대상: {pending:,}건 중 {count:,}건 (심각도가 높은 댓글부터) · "
        f"심각도 {settings.min_severity} 이상 · 대상 {', '.join(settings.targets)}"
    )
    print(f"브라우저: {'창 없이 실행' if headless else '창을 띄워 실행'} · 저장 위치: {folder}")

    summary, code = _shoot(
        config, db, args, db.comments_to_capture(**scope, limit=count),
        headless=headless, limits_hit=_capture_limits_hit(config, args, count, pending),
    )

    left = db.count_to_capture(**scope)
    print(f"캡처 완료: {summary.captured:,}건 · 실패 {summary.failed:,}건 · 남은 미캡처 {left:,}건")
    for evidence_no, reason in summary.failures:
        print(f"- 증거 {evidence_no:06d}: {reason}")
    if summary.failed:
        print("실패한 댓글은 다음 실행에서 다시 시도합니다.")
    if summary.captured:
        print("엑셀의 캡처 칸은 `export`를 다시 실행하면 채워집니다.")
    return code


def _capture_limits_hit(
    config: Config, args: argparse.Namespace, count: int, pending: int
) -> list[str]:
    if count >= pending:
        return []
    limit = config.limits.max_captures_per_run
    return ["limit" if args.limit and args.limit <= limit else "max_captures_per_run"]


def _shoot(
    config: Config, db: Database, args: argparse.Namespace, rows: Sequence[Any],
    *, headless: bool, limits_hit: list[str],
) -> tuple[CaptureSummary, int]:
    """댓글들을 브라우저로 찍고 실행 기록을 남긴다. (결과, 종료 코드)를 돌려준다."""
    run_id = db.start_run(args.command, {"limit": args.limit, "headed": args.headed})
    summary = CaptureSummary()
    error = None
    code = 0

    def progress(s: CaptureSummary, row, failure: str | None) -> None:
        result = f"실패: {failure}" if failure else "저장"
        # 찍은 뒤에 판정이 바뀐 댓글은 심각도가 없다.
        severity = f"심각도 {row['severity']} · " if row["severity"] is not None else ""
        print(
            f"[{s.done:,}/{s.total:,}] 증거 {row['evidence_no']:06d} · {severity}{result}",
            flush=True,
        )

    try:
        with make_camera(headless=headless) as camera:
            worker = Capturer(config, db, camera, run_id=run_id)
            summary = worker.summary
            worker.run(rows, on_progress=progress)
    except CaptureError as exc:
        error, code = str(exc), 1
        print(f"오류: {exc}", file=sys.stderr)
    except KeyboardInterrupt:
        error, code = "사용자가 중단함", 130
        print("\n중단했습니다. 여기까지 찍은 캡처는 저장되어 있습니다.", file=sys.stderr)
    if summary.blocked:
        error, code = f"차단 화면: {summary.blocked}", 1
        print(
            f"차단 화면이 나와 멈췄습니다. {summary.blocked}\n"
            "시간을 두고 다시 실행하면 남은 댓글부터 이어서 찍습니다.",
            file=sys.stderr,
        )
    db.finish_run(
        run_id,
        counts={"captured": summary.captured, "failed": summary.failed},
        limits_hit=limits_hit, error=error,
    )
    return summary, code


def _recapture(config: Config, db: Database, args: argparse.Namespace) -> int:
    """캡처 기록을 파일과 비교하고, 합성본과 원본이 다 없는 캡처를 다시 찍는다.

    기록은 지우지 않는다. 새 기록이 덧붙고, 엑셀은 댓글마다 가장 최근의 성공 기록을 쓴다.
    """
    output_dir = config.export.path.parent
    check = check_captures(db, output_dir)
    print(f"캡처 기록 {check.checked:,}건을 파일과 비교했습니다. ({output_dir / CAPTURE_DIR})")
    if not (check.missing or check.partial or check.altered):
        print("파일이 모두 기록과 일치합니다.")
        return 0
    if check.missing:
        print(f"파일이 없는 캡처: {len(check.missing):,}건")
    if check.partial:
        print(f"한쪽 파일만 남은 캡처: {len(check.partial):,}건 (남은 파일을 덮어쓰지 않도록 다시 찍지 않습니다)")
        for evidence_no, absent in check.partial:
            print(f"- 증거 {evidence_no:06d}: {absent} 없음")
    if check.altered:
        print(f"기록한 해시와 다른 캡처: {len(check.altered):,}건 (파일이 바뀌었습니다. 다시 찍지 않습니다)")
        for evidence_no, changed in check.altered:
            print(f"- 증거 {evidence_no:06d}: {changed}")
    if not check.missing:
        print("다시 찍을 댓글이 없습니다.")
        return 0

    pending = len(check.missing)
    count = min(pending, config.limits.max_captures_per_run, args.limit or pending)
    headless = config.capture.headless and not args.headed
    print(f"다시 찍을 댓글: {pending:,}건 중 {count:,}건 (심각도가 높은 댓글부터)")
    print(f"브라우저: {'창 없이 실행' if headless else '창을 띄워 실행'}")
    summary, code = _shoot(
        config, db, args, check.missing[:count],
        headless=headless, limits_hit=_capture_limits_hit(config, args, count, pending),
    )

    left = len(check_captures(db, output_dir).missing)
    print(
        f"다시 캡처 완료: {summary.captured:,}건 · 실패 {summary.failed:,}건 · "
        f"아직 파일이 없는 캡처 {left:,}건"
    )
    for evidence_no, reason in summary.failures:
        print(f"- 증거 {evidence_no:06d}: {reason}")
    if summary.failed:
        print("댓글이 지워졌거나 고쳐졌으면 다시 찍을 수 없습니다. 예전 기록은 DB에 그대로 남아 있습니다.")
    if summary.captured:
        print("엑셀의 캡처 해시는 `export`를 다시 실행하면 새 파일 기준으로 바뀝니다.")
    return code


def _import_reviews(config: Config, db: Database) -> bool:
    """사람이 엑셀에 적은 검토 상태와 비고를 DB로 옮긴다. 읽지 못하면 False."""
    try:
        imported = import_reviews(db, config.export.path)
    except ExportError as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return False
    if imported.saved:
        print(f"기존 엑셀에서 검토 결과 {imported.saved:,}건을 읽어 왔습니다.")
    for bad in imported.invalid:
        print(f"- 선택지에 없는 검토 상태라 반영하지 않았습니다 ({bad})")
    return True


def _export(config: Config, db: Database) -> int:
    path = config.export.path
    # 엑셀을 덮어쓰기 전에 검토 결과부터 읽는다. 엑셀을 읽지 못하면 덮어쓰지 않는다.
    if not _import_reviews(config, db):
        return 1
    result = export_excel(db, path, sheets=build_sheets(config, db))
    if result.renamed:
        print(
            f"{path.name} 파일이 열려 있어 덮어쓸 수 없습니다. "
            "다른 이름으로 저장했습니다."
        )
    for title, rows in result.rows.items():
        print(f"{title}: {rows:,}{result.units.get(title, '건')}")
    print(f"저장: {result.path}")
    return 0


def _folders(config: Config, db: Database) -> int:
    """랭킹 탭의 "증거 폴더" 칸이 가리키는 폴더를 만든다. DB는 읽기만 하고 엑셀은 건드리지 않는다."""
    try:
        folders = export_author_folders(db, config.export.path.parent, config.target_names)
    except OSError as exc:
        print(
            f"오류: {AUTHORS_DIR} 폴더를 다시 만들지 못했습니다 ({exc}). 폴더 안의 파일을 "
            "닫은 뒤 다시 실행해 주세요. 원본 캡처와 엑셀은 그대로입니다.",
            file=sys.stderr,
        )
        return 1
    if folders.folders:
        print(f"작성자별 증거 폴더: {folders.folders:,}명 · 캡처 {folders.files:,}장")
        print(f"저장: {folders.path}")
        print("폴더 이름의 순위는 지금 DB 기준입니다. 엑셀의 순위와 맞추려면 `export`도 실행해 주세요.")
    else:
        print("복사할 캡처가 없습니다.")
    if folders.missing:
        print(f"- 캡처 파일을 찾지 못해 복사하지 못한 댓글 {folders.missing:,}건")
    return 0


_LIMIT_LABELS = {
    "max_videos_per_run": "영상 수 상한 (limits.max_videos_per_run)",
    "max_comments_per_video": "영상당 댓글 상한 (limits.max_comments_per_video)",
    "daily_quota_budget": "하루 할당량 예산 (youtube.daily_quota_budget)",
    "max_classify_per_run": "분류 건수 상한 (limits.max_classify_per_run)",
    "max_captures_per_run": "캡처 건수 상한 (limits.max_captures_per_run)",
    "limit": "--limit",
}


def _run_summary(config: Config, db: Database, after_run_id: int, failed: list[str]) -> None:
    """이번 run이 만든 실행 기록(run:collect, run:classify, run:capture)을 모아 요약한다."""
    runs = db.conn.execute(
        "SELECT * FROM runs WHERE id > ? ORDER BY id", (after_run_id,)
    ).fetchall()
    ids = [row["id"] for row in runs]
    marks = ", ".join("?" for _ in ids) or "NULL"
    counts: dict[str, int] = {}
    limits: list[str] = []
    for row in runs:
        for key, value in json.loads(row["counts"] or "{}").items():
            counts[f"{row['command']}.{key}"] = counts.get(f"{row['command']}.{key}", 0) + value
        for name in json.loads(row["limits_hit"] or "[]"):
            if name not in limits:
                limits.append(name)
    negatives = db.scalar(
        f"SELECT COUNT(*) FROM classifications WHERE run_id IN ({marks}) AND is_current = 1 "
        "AND stage = 'final' AND status = 'ok' AND sentiment = '부정'", ids,
    )
    quota = sum(row["quota_used"] or 0 for row in runs)
    cost = sum(spent_krw(db, run_id=run_id) for run_id in ids)
    capture = config.capture
    left_classify = db.count_to_classify()
    left_capture = db.count_to_capture(min_severity=capture.min_severity, targets=capture.targets)

    def count(name: str) -> int:
        return counts.get(name, 0)

    print()
    print("=== 실행 요약 ===")
    print(
        f"수집: 새 댓글 {count('run:collect.new'):,}건 · 수정된 댓글 {count('run:collect.edited'):,}건"
        f" · 새로 조회 불가가 된 댓글 {count('run:collect.missing'):,}건"
    )
    line = f"분류: {count('run:classify.classified'):,}건 · 이 가운데 부정 {negatives:,}건"
    if count("run:classify.failed"):
        line += f" · 분류 실패 {count('run:classify.failed'):,}건"
    if count("run:classify.escalated"):
        line += f" · 최종 판정으로 넘긴 댓글 {count('run:classify.escalated'):,}건"
    print(line)
    print(f"캡처: {count('run:capture.captured'):,}건 · 실패 {count('run:capture.failed'):,}건")
    print(f"API 사용: 유튜브 할당량 {quota:,} units · 분류 비용 약 {cost:,.0f}원")
    if limits:
        print("상한에 걸린 항목: " + ", ".join(_LIMIT_LABELS.get(name, name) for name in limits))
    if left_classify or left_capture:
        print(
            f"남은 일: 미분류 {left_classify:,}건 · 미캡처 {left_capture:,}건 "
            "(다음 run이 이어서 처리합니다)"
        )
    if failed:
        print("문제가 있었던 단계: " + ", ".join(failed) + " (위의 오류 메시지를 확인해 주세요)")


def _run(config: Config, db: Database, args: argparse.Namespace) -> int:
    """DESIGN.md 4장의 흐름. 수집부터 캡처까지 한 번에 하는 것은 악성 댓글이 빨리 지워지기 때문이다.

    한 단계가 오류로 끝나도 다음 단계는 진행한다(수집이 할당량에서 멈췄어도 이미 모은 댓글은
    분류하고 찍어야 한다). 확인 질문에 아니오라고 답하거나 Ctrl+C로 멈추면 거기서 끝낸다.
    """
    after_run_id = db.scalar("SELECT COALESCE(MAX(id), 0) FROM runs")
    failed: list[str] = []

    def heading(index: int, title: str) -> None:
        print()
        print(f"=== {index}/5 {title} ===", flush=True)

    def as_step(name: str) -> argparse.Namespace:
        return argparse.Namespace(**{**vars(args), "command": f"run:{name}"})

    heading(1, "검토 결과 읽기")
    if not _import_reviews(config, db):
        print("엑셀을 읽지 못해 실행을 멈췄습니다. 검토 결과를 덮어쓰지 않으려는 것입니다.",
              file=sys.stderr)
        return 1
    print("완료")

    steps = (
        ("대상 영상 확정과 수집", "collect",
         lambda step: _collect(config, db, step, plan_only=False)),
        ("AI 분류", "classify", lambda step: _classify(config, db, step, again=False)),
        ("캡처", "capture", lambda step: _capture(config, db, step)),
    )
    for index, (title, name, action) in enumerate(steps, start=2):
        heading(index, title)
        code = action(as_step(name))
        if code == CANCELLED:
            print("실행을 멈췄습니다. 다음 단계는 진행하지 않았습니다.")
            _run_summary(config, db, after_run_id, failed)
            return CANCELLED
        if code == 130:
            _run_summary(config, db, after_run_id, failed)
            return 130
        if code != 0:
            failed.append(title)

    heading(5, "엑셀 출력")
    if _export(config, db) != 0:
        failed.append("엑셀 출력")
    _run_summary(config, db, after_run_id, failed)
    return 1 if failed else 0


def _status(config: Config, db: Database) -> int:
    s = db.status_summary(
        capture_min_severity=config.capture.min_severity,
        capture_targets=config.capture.targets,
    )
    videos = s["videos_full"] + s["videos_partial"] + s["videos_none"]
    last = s["last_run"]
    if last is None:
        last_text = "없음"
    else:
        outcome = (
            "진행 중이거나 중단됨" if last["ended_at"] is None
            else f"오류: {last['error']}" if last["error"]
            else "정상 종료"
        )
        last_text = f"{last['command']} · {to_kst(last['started_at'])} · {outcome}"
    lines = [
        ("대상", ", ".join(config.target_names)),
        ("DB", str(config.db_path)),
        (
            "영상",
            f"{videos:,}개 (전체 수집 {s['videos_full']:,} · 부분 수집 {s['videos_partial']:,} · "
            f"미수집 {s['videos_none']:,}) · 이어하기 위치 저장 {s['videos_resumable']:,}개 · "
            f"건너뜀 {s['videos_skipped']:,}개 · 제외 {s['videos_excluded']:,}개",
        ),
        ("댓글", f"{s['comments']:,}건 (조회 불가 {s['comments_missing']:,})"),
        ("미분류", f"{s['unclassified']:,}건 · 분류 실패 {s['classify_failed']:,}건"),
        ("부정 댓글", f"{s['negative']:,}건"),
        ("캡처", f"완료 {s['captures_ok']:,}건 · 미캡처 {s['captures_pending']:,}건"),
        (
            "오늘 할당량",
            f"{s['quota_used_today']:,} / {config.youtube.daily_quota_budget:,} units "
            f"(태평양 시간 {s['quota_day']} 기준)",
        ),
        (
            "분류 토큰",
            f"입력 {s['input_tokens']:,} · 출력 {s['output_tokens']:,} (누적) · "
            f"실제 비용 약 {spent_krw(db):,.0f}원",
        ),
        ("마지막 실행", last_text),
    ]
    for label, value in lines:
        print(f"{label}: {value}")
    return 0


def _dispatch(config: Config, db: Database, args: argparse.Namespace) -> int:
    command = args.command
    if command == "run":
        return _run(config, db, args)
    if command == "status":
        return _status(config, db)
    if command in ("plan", "collect"):
        return _collect(config, db, args, plan_only=command == "plan")
    if command in ("classify", "reclassify"):
        return _classify(config, db, args, again=command == "reclassify")
    if command == "capture":
        return _capture(config, db, args)
    if command == "recapture":
        return _recapture(config, db, args)
    if command == "folders":
        return _folders(config, db)
    return _export(config, db)


def main(argv: Sequence[str] | None = None) -> int:
    # 윈도우 콘솔의 기본 인코딩에서도 한글이 깨지지 않게 한다.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    args = build_parser().parse_args(argv)
    try:
        config = apply_overrides(
            load_config(args.config),
            videos=args.video,
            no_search=args.no_search,
            search_top=args.search_top,
            max_comments=args.max_comments,
            max_videos=args.max_videos,
        )
        with Database(config.db_path) as db:
            code = _dispatch(config, db, args)
        return 0 if code == CANCELLED else code
    except (ConfigError, DatabaseError) as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
