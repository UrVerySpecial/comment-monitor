"""모델 비교 (classify --compare N).

같은 댓글을 기준 모델(classifier.model)과 비교 모델(classifier.first_pass_model)로 각각 분류해
판정이 어디서 갈리는지 본다. 비교 모델만으로 충분한지, 2단 분류를 켜도 되는지 판단하는 자료다.
여기서 나온 판정은 DB의 판정으로 저장하지 않는다. 토큰 사용량만 기록한다.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from .classifier import USAGE_FIELDS, Classifier, Judgment
from .config import Config
from .db import Database
from .planner import krw_per_comment, tokens_cost_krw

TOKEN_STAGE = "compare"
NEGATIVE = "부정"
SERIOUS = 3  # 놓치면 안 되는 심각도의 하한
# 비교하는 판정 항목: (표시 이름, Judgment의 속성)
FIELDS = (
    ("감성", "sentiment"), ("대상", "target"), ("법적 유형", "legal_type"), ("심각도", "severity"),
)


@dataclass
class ModelRun:
    """한 모델이 표본을 분류한 결과."""

    model: str
    requests: int = 0
    refusals: int = 0
    fallback_requests: int = 0
    judged: int = 0
    failed: int = 0
    tokens: dict[str, int] = field(default_factory=lambda: dict.fromkeys(USAGE_FIELDS, 0))
    cost_krw: float = 0.0
    served_by: tuple[str, ...] = ()   # 실제로 응답한 모델 (대체 모델이 섞일 수 있다)

    @property
    def input_total(self) -> int:
        return sum(self.tokens[name] for name in USAGE_FIELDS if name != "output_tokens")

    @property
    def krw_per_comment(self) -> float | None:
        return self.cost_krw / self.judged if self.judged else None


@dataclass
class Compared:
    """댓글 한 건에 대한 두 모델의 판정."""

    row: Any
    reference: Judgment | None
    candidate: Judgment | None
    reference_failure: str | None = None
    candidate_failure: str | None = None

    @property
    def both(self) -> bool:
        return self.reference is not None and self.candidate is not None

    @property
    def differs(self) -> list[str]:
        """판정이 갈린 항목의 이름."""
        if not self.both:
            return []
        return [
            label for label, name in FIELDS
            if getattr(self.reference, name) != getattr(self.candidate, name)
        ]

    @property
    def severity_gap(self) -> int | None:
        """비교 모델 심각도 - 기준 모델 심각도."""
        return self.candidate.severity - self.reference.severity if self.both else None

    @property
    def missed(self) -> bool:
        """기준 모델은 부정으로 봤는데 비교 모델은 부정으로 보지 않았다."""
        return (
            self.both and self.reference.sentiment == NEGATIVE
            and self.candidate.sentiment != NEGATIVE
        )

    @property
    def extra(self) -> bool:
        """비교 모델만 부정으로 봤다."""
        return (
            self.both and self.candidate.sentiment == NEGATIVE
            and self.reference.sentiment != NEGATIVE
        )


@dataclass
class Comparison:
    reference: ModelRun
    candidate: ModelRun
    items: list[Compared] = field(default_factory=list)
    requested: int = 0   # 비교하려던 댓글 수. 중간에 멈추면 items보다 많다.

    @property
    def both(self) -> list[Compared]:
        return [item for item in self.items if item.both]

    @property
    def different(self) -> list[Compared]:
        return [item for item in self.items if item.differs]

    @property
    def one_sided(self) -> list[Compared]:
        """한쪽이라도 분류에 실패해 견줄 수 없는 댓글."""
        return [item for item in self.items if not item.both]

    def field_differences(self) -> dict[str, int]:
        counts = {label: 0 for label, _ in FIELDS}
        for item in self.items:
            for label in item.differs:
                counts[label] += 1
        return counts

    @property
    def reference_negative(self) -> list[Compared]:
        return [item for item in self.both if item.reference.sentiment == NEGATIVE]

    @property
    def missed(self) -> list[Compared]:
        return [item for item in self.items if item.missed]

    @property
    def missed_serious(self) -> list[Compared]:
        return [item for item in self.missed if item.reference.severity >= SERIOUS]

    @property
    def extra(self) -> list[Compared]:
        return [item for item in self.items if item.extra]

    @property
    def both_negative(self) -> list[Compared]:
        return [
            item for item in self.reference_negative if item.candidate.sentiment == NEGATIVE
        ]

    @property
    def severity_close(self) -> int:
        """둘 다 부정으로 본 댓글 가운데 심각도 차이가 1 이하인 건수."""
        return sum(1 for item in self.both_negative if abs(item.severity_gap) <= 1)

    def to_review(self) -> list[Compared]:
        """엑셀에 내보낼 댓글: 판정이 갈린 것. 놓친 부정, 심각도 차이가 큰 것부터."""
        return sorted(
            self.different,
            key=lambda item: (
                not item.missed, -(item.reference.severity if item.missed else 0),
                -abs(item.severity_gap), -item.row["evidence_no"],
            ),
        )


def ratio(part: int, whole: int) -> float | None:
    return part / whole if whole else None


def compare_cost_krw(config: Config, count: int, db: Database | None = None) -> int:
    """표본 count건을 두 모델로 모두 분류하는 예상 비용."""
    settings = config.classifier
    per_comment = (
        krw_per_comment(settings.model, db) + krw_per_comment(settings.first_pass_model, db)
    )
    return math.ceil(count * per_comment)


def _model_run(worker: Classifier) -> ModelRun:
    summary = worker.summary
    run = ModelRun(
        model=worker.model, requests=summary.requests, refusals=summary.refusals,
        fallback_requests=summary.fallback_requests,
        judged=len(worker.results), failed=len(worker.failures),
        served_by=tuple(worker.usage),
    )
    for usage in worker.usage.values():
        for name in USAGE_FIELDS:
            run.tokens[name] += usage[name]
        run.cost_krw += tokens_cost_krw(usage)   # 값은 실제로 응답한 모델의 단가로 매긴다
    return run


class Comparer:
    """묶음마다 두 모델을 차례로 돌린다. 중간에 멈춰도 그때까지의 비교가 남는다."""

    def __init__(
        self, config: Config, db: Database, client: Any, *, run_id: int | None = None
    ) -> None:
        settings = config.classifier
        options = {"run_id": run_id, "store": False, "token_stage": TOKEN_STAGE}
        self.batch_size = settings.batch_size
        self.reference = Classifier(config, db, client, model=settings.model, **options)
        self.candidate = Classifier(
            config, db, client, model=settings.first_pass_model, **options
        )
        self.rows: list[Any] = []       # 두 모델이 모두 시도한 댓글
        self.requested = 0

    def run(
        self, rows: Sequence[Any], *, on_progress: Callable[[Comparison], None] | None = None
    ) -> Comparison:
        self.requested = len(rows)
        for start in range(0, len(rows), self.batch_size):
            batch = list(rows[start:start + self.batch_size])
            # 싼 모델을 먼저 돌린다. 여기서 오류가 나면 비싼 모델 요청은 나가지 않는다.
            self.candidate.run(batch)
            self.reference.run(batch)
            self.rows.extend(batch)
            if on_progress:
                on_progress(self.result())
        return self.result()

    def result(self) -> Comparison:
        reference, candidate = self.reference, self.candidate
        items = [
            Compared(
                row,
                reference.results.get(row["evidence_no"]),
                candidate.results.get(row["evidence_no"]),
                reference.failures.get(row["evidence_no"]),
                candidate.failures.get(row["evidence_no"]),
            )
            for row in self.rows
        ]
        return Comparison(
            _model_run(reference), _model_run(candidate), items, requested=self.requested
        )
