import pytest
from openpyxl import load_workbook

from comment_monitor import cli
from comment_monitor.classifier import FALLBACK_BETA
from comment_monitor.cli import main
from comment_monitor.compare import Comparer, compare_cost_krw
from comment_monitor.db import Database
from comment_monitor.exporters.compare import compare_path, export_comparison

from .conftest import VIDEO_ID
from .fake_claude import FakeClaude, judge, refusal, reply
from .test_classifier import add

OPUS, HAIKU = "claude-opus-5-5", "claude-haiku-5-5"


def two_models(items, kwargs):
    """haiku는 '은근'이 든 욕을 놓치고, '살짝'이 든 욕은 심각도를 낮게 본다."""
    model = kwargs["model"]
    results = []
    for item in items:
        result = judge(item)
        if model == HAIKU and "은근" in item["text"]:
            result = judge({**item, "text": "잡담"})
        if model == HAIKU and "살짝" in item["text"]:
            result.update(severity=1, legal_type="단순 비판")
        if model == HAIKU and "괜히" in item["text"]:
            result.update(sentiment="부정", target="불명", legal_type="단순 비판", severity=1)
        results.append(result)
    return reply(results, model=model)


@pytest.fixture
def sample(db):
    texts = ["욕 그대로", "은근한 욕", "살짝 욕", "좋아요", "괜히 좋아요", "인사"]
    for n, text in enumerate(texts, start=1):
        add(db, f"c{n}", text, published=f"2026-11-0{n}T00:00:00Z")
    return db


def test_both_models_judge_the_same_comments_and_nothing_is_saved(cfg, sample):
    config = cfg("classifier:\n  batch_size: 4\n")
    client = FakeClaude(two_models)
    comparison = Comparer(config, sample, client).run(sample.comments_to_compare(5))

    # 최신 5건을 묶음마다 두 모델에 같은 내용으로 보낸다. 싼 모델이 먼저다.
    assert [call["model"] for call in client.calls] == [HAIKU, OPUS, HAIKU, OPUS]
    assert client.batches == [[6, 5, 4, 3], [6, 5, 4, 3], [2], [2]]
    assert client.calls[0]["system"] == client.calls[1]["system"]
    assert client.calls[0]["messages"] == client.calls[1]["messages"]
    # haiku에는 서버 측 대체 모델 옵션을 보내지 않는다.
    for call in client.calls:
        if call["model"] == HAIKU:
            assert call["via"] == "messages" and "fallbacks" not in call and "betas" not in call
        else:
            assert call["via"] == "beta" and call["fallbacks"] == "default"
            assert call["betas"] == [FALLBACK_BETA]

    # 판정은 저장하지 않는다. 토큰 사용량만 남는다.
    assert sample.scalar("SELECT COUNT(*) FROM classifications") == 0
    assert sample.unclassified_count() == 6
    usage = {row["model"]: row for row in sample.token_usage_by_model()}
    assert usage[OPUS]["requests"] == usage[HAIKU]["requests"] == 2
    assert sample.scalar("SELECT DISTINCT stage FROM token_usage") == "compare"

    assert len(comparison.items) == len(comparison.both) == 5
    by_id = {item.row["comment_id"]: item for item in comparison.items}
    assert by_id["c6"].differs == [] and by_id["c4"].differs == []
    assert by_id["c2"].differs == ["감성", "대상", "법적 유형", "심각도"] and by_id["c2"].missed
    assert by_id["c3"].differs == ["법적 유형", "심각도"] and by_id["c3"].severity_gap == -2
    assert by_id["c5"].extra and not by_id["c5"].missed
    assert comparison.field_differences() == {"감성": 2, "대상": 1, "법적 유형": 3, "심각도": 3}
    assert [item.row["comment_id"] for item in comparison.missed_serious] == ["c2"]
    assert len(comparison.reference_negative) == 2 and comparison.severity_close == 0
    # 놓친 부정이 맨 앞에 온다.
    assert [item.row["comment_id"] for item in comparison.to_review()] == ["c2", "c3", "c5"]


def test_cost_is_priced_per_model(cfg, sample):
    config = cfg()
    assert compare_cost_krw(config, 100) == 410            # 4원 + 0.1원
    comparison = Comparer(config, sample, FakeClaude(two_models)).run(
        sample.comments_to_compare(6))
    opus, haiku = comparison.reference, comparison.candidate
    assert opus.tokens == haiku.tokens == {
        "input_tokens": 1000, "output_tokens": 200,
        "cache_creation_input_tokens": 0, "cache_read_input_tokens": 500,
    }
    # 입력 1000 + 캐시 읽기 500 + 출력 200 토큰, 1달러 1400원
    assert opus.cost_krw == pytest.approx((1000 * 4 + 500 * 4 * 0.05 + 200 * 20) / 1e6 * 1400)
    assert haiku.cost_krw == pytest.approx((1000 * 0.1 + 500 * 0.1 * 0.1 + 200 * 0.5) / 1e6 * 1400)
    assert opus.krw_per_comment == pytest.approx(opus.cost_krw / 6)
    # 실제 사용 기록이 생기면 견적도 모델별 실측값을 쓴다.
    assert compare_cost_krw(config, 6000, sample) == round((opus.cost_krw + haiku.cost_krw) * 1000)


def test_failures_on_one_side_are_reported_not_compared(cfg, sample):
    def handler(items, kwargs):
        if kwargs["model"] == HAIKU and any("인사" in item["text"] for item in items):
            return refusal(model=HAIKU)
        return two_models(items, kwargs)

    comparison = Comparer(cfg(), sample, FakeClaude(handler)).run(sample.comments_to_compare(2))
    failed = comparison.one_sided
    assert [item.row["comment_id"] for item in failed] == ["c6"]
    assert failed[0].candidate is None and failed[0].candidate_failure and failed[0].reference
    assert failed[0].differs == [] and len(comparison.both) == 1
    assert (comparison.candidate.failed, comparison.candidate.refusals) == (1, 2)
    assert comparison.reference.failed == 0


def test_fallback_served_requests_are_priced_at_the_serving_model(cfg, sample):
    from types import SimpleNamespace

    def handler(items, kwargs):
        if kwargs["model"] == OPUS:
            return reply([judge(i) for i in items], model="claude-sonnet-5-5",
                         iterations=[SimpleNamespace(type="fallback_message")])
        return two_models(items, kwargs)

    opus = Comparer(cfg(), sample, FakeClaude(handler)).run(
        sample.comments_to_compare(3)).reference
    assert (opus.model, opus.served_by, opus.fallback_requests) == (
        OPUS, ("claude-sonnet-5-5",), 1)
    assert opus.cost_krw == pytest.approx((1000 * 2 + 500 * 2 * 0.05 + 200 * 10) / 1e6 * 1400)


def test_workbook_lists_only_the_comments_that_differ(cfg, sample, tmp_path):
    comparison = Comparer(cfg(), sample, FakeClaude(two_models)).run(
        sample.comments_to_compare(6))
    path = compare_path(tmp_path / "output" / "댓글증거.xlsx")
    result = export_comparison(sample, path, comparison)
    assert result.path == tmp_path / "output" / "모델비교.xlsx"
    assert result.rows == {"요약": 6, "판정 차이": 3}

    book = load_workbook(path)
    lines = list(book["판정 차이"].iter_rows(values_only=True))
    headers = list(lines[0])
    rows = [dict(zip(headers, line)) for line in lines[1:]]
    assert [row["댓글 ID"] for row in rows] == ["c2", "c3", "c5"]
    first = rows[0]
    assert first["갈린 항목"] == "감성, 대상, 법적 유형, 심각도" and first["심각도 차이"] == -3
    assert first["구분"] == f"{HAIKU}가 부정을 놓침" and rows[2]["구분"] == f"{HAIKU}만 부정으로 봄"
    assert (first[f"{OPUS}\n감성"], first[f"{HAIKU}\n감성"]) == ("부정", "중립")
    assert (first[f"{OPUS}\n심각도"], first[f"{HAIKU}\n심각도"]) == (3, 0)
    assert first[f"{OPUS}\n근거"] == "욕설" and first["댓글 본문"] == "은근한 욕"
    assert first["댓글 주소"].endswith(f"v={VIDEO_ID}&lc=c2")

    cells = [list(line) for line in book["요약"].iter_rows(values_only=True)]
    lookup = {line[0]: line for line in cells if line and line[0]}
    assert lookup["비교한 댓글 수"][1] == 6
    assert lookup["  한 항목이라도 갈린 댓글"][1:3] == [3, 0.5]
    assert lookup["감성"][1:3] == [2, pytest.approx(4 / 6)]
    assert lookup["기준 모델이 부정으로 본 댓글"][1] == 3
    assert lookup["  비교 모델이 놓친 댓글"][1] == 1
    assert lookup["비교 모델만 부정으로 본 댓글"][1] == 1
    usage_head = next(line for line in cells if line[0] == "모델")
    opus = dict(zip(usage_head, lookup[OPUS]))
    haiku = dict(zip(usage_head, lookup[HAIKU]))
    assert (opus["요청 수"], opus["입력 토큰"], opus["출력 토큰"], opus["판정 건수"]) == (1, 1000, 200, 6)
    assert opus["실제 비용 (원)"] == 11.3 and haiku["실제 비용 (원)"] == 0.3
    assert lookup["비용 비율 (기준 ÷ 비교)"][1] == 39.5


# ---- 명령 ----


@pytest.fixture
def site(write_config, tmp_path, monkeypatch):
    path = write_config("classifier:\n  batch_size: 4\n")
    with Database(tmp_path / "data" / "monitor.db") as db:
        db.conn.execute(
            "INSERT INTO videos (platform, video_id, title, source, first_seen_at) "
            "VALUES ('youtube', ?, '시험 영상', 'direct', '2026-11-01T00:00:00Z')", (VIDEO_ID,),
        )
        texts = ["욕 그대로", "은근한 욕", "살짝 욕", "좋아요", "괜히 좋아요", "인사"]
        for n, text in enumerate(texts, start=1):
            add(db, f"c{n}", text, published=f"2026-11-0{n}T00:00:00Z")
    client = FakeClaude(two_models)
    monkeypatch.setattr(cli, "make_classifier_client", lambda: client)
    return str(path), client


def test_compare_command(site, tmp_path, capsys):
    path, client = site
    assert main(["classify", "--config", path, "--compare", "200"]) == 0
    out = capsys.readouterr().out
    assert f"모델 비교: 최신 댓글 6건 · 기준 {OPUS} · 비교 {HAIKU}" in out
    assert "수집된 댓글이 그만큼 없어서 200건이 아니라 6건만 비교합니다" in out
    assert "예상 비용: 약 25원 (두 모델 합계)" in out
    assert "[6/6] 갈린 댓글 3 · 놓친 부정 1" in out
    assert "네 항목이 모두 같은 댓글 3건 (50.0%) · 갈린 댓글 3건 (50.0%)" in out
    assert "항목별 갈린 건수: 감성 2 · 대상 1 · 법적 유형 3 · 심각도 3" in out
    assert "기준 모델이 부정으로 본 3건 중 비교 모델이 놓친 댓글 1건 (33.3%), 그중 심각도 3 이상 1건" in out
    assert f"- {OPUS}: 요청 2회 · 입력 3,000 (캐시 적중 1,000) · 출력 400 · 실제 비용 약 22.7원" in out
    assert f"- {HAIKU}: 요청 2회 · 입력 3,000 (캐시 적중 1,000) · 출력 400 · 실제 비용 약 0.6원" in out
    assert "판정이 갈린 댓글 3건을 저장" in out
    assert (tmp_path / "output" / "모델비교.xlsx").is_file()
    assert not (tmp_path / "output" / "댓글증거.xlsx").exists()

    with Database(tmp_path / "data" / "monitor.db") as db:
        assert db.scalar("SELECT COUNT(*) FROM classifications") == 0
        run = db.conn.execute("SELECT * FROM runs").fetchone()
        assert run["command"] == "classify" and '"compare": 200' in run["args"]
        assert '"missed_negative": 1' in run["counts"] and run["ended_at"]

    # 비교 뒤에도 일반 분류는 전부를 대상으로 한다.
    assert main(["classify", "--config", path]) == 0
    assert "분류 대상: 6건 중 6건" in capsys.readouterr().out


def test_compare_asks_before_an_expensive_run(site, capsys, monkeypatch):
    path, client = site
    with open(path, "a", encoding="utf-8") as handle:
        handle.write("limits:\n  confirm_cost_over_krw: 10\n  max_classify_per_run: 4\n")
    monkeypatch.setattr("builtins.input", lambda prompt: "n")
    assert main(["classify", "--config", path, "--compare", "5"]) == 0
    out = capsys.readouterr().out
    assert "max_classify_per_run 상한 때문에 5건이 아니라 4건만 비교합니다" in out
    assert "취소했습니다" in out and not client.calls
    assert main(["classify", "--config", path, "--compare", "5", "--yes"]) == 0
    assert len(client.calls) == 2


def test_compare_keeps_what_was_done_when_the_api_fails(site, tmp_path, capsys):
    import anthropic
    import httpx2 as httpx

    path, client = site
    inner = client.handler

    def handler(items, kwargs):
        if len(client.calls) > 2:
            request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
            raise anthropic.RateLimitError(
                "slow down", response=httpx.Response(429, request=request), body=None)
        return inner(items, kwargs)

    client.handler = handler
    assert main(["classify", "--config", path, "--compare", "6"]) == 1
    captured = capsys.readouterr()
    assert "요청 한도" in captured.err
    assert "비교한 댓글 4건" in captured.out and "판정이 갈린 댓글" in captured.out
    summary = load_workbook(tmp_path / "output" / "모델비교.xlsx")["요약"]
    notes = [line[0] for line in summary.iter_rows(values_only=True) if line and line[0]]
    assert "6건을 비교하려다 중간에 멈춰 4건만 비교했다." in notes


def test_compare_needs_two_different_models(site, capsys):
    path, client = site
    text = open(path, encoding="utf-8").read().replace(
        "classifier:\n", f'classifier:\n  first_pass_model: "{OPUS}"\n')
    open(path, "w", encoding="utf-8").write(text)
    assert main(["classify", "--config", path, "--compare", "3"]) == 1
    assert "같아 비교할 수 없습니다" in capsys.readouterr().err and not client.calls
