import json

import pytest
from openpyxl import load_workbook

from comment_monitor import cli
from comment_monitor.cli import build_parser, main
from comment_monitor.db import Database

from .helpers import V1, V2, mark_negative


def test_status_creates_database_and_prints_summary(write_config, tmp_path, capsys):
    path = write_config()
    assert main(["status", "--config", str(path)]) == 0
    out = capsys.readouterr().out
    assert "대상: 홍길동" in out
    assert "댓글: 0건" in out
    assert "0 / 8,000 units" in out
    assert "마지막 실행: 없음" in out
    assert (tmp_path / "data" / "monitor.db").is_file()


def test_missing_config_is_reported_without_traceback(tmp_path, capsys):
    assert main(["status", "--config", str(tmp_path / "config.yaml")]) == 2
    assert "설정 파일이 없습니다" in capsys.readouterr().err


def test_invalid_video_option_is_reported(write_config, capsys):
    assert main(["status", "--config", str(write_config()), "--video", "https://example.com"]) == 2
    assert "오류:" in capsys.readouterr().err


def test_all_design_commands_and_options_parse():
    parser = build_parser()
    for command in ("plan", "run", "collect", "classify", "capture", "export", "status",
                    "reclassify", "folders", "recapture"):
        args = parser.parse_args([
            command, "--limit", "200", "--video", "a", "--video", "b", "--no-search",
            "--search-top", "10", "--max-comments", "500", "--full", "--yes",
            "--refresh-search",
        ])
        assert (args.command, args.limit, args.video) == (command, 200, ["a", "b"])
        assert args.no_search and args.full and args.yes and args.refresh_search
        assert (args.search_top, args.max_comments) == (10, 500)
    assert parser.parse_args(["classify", "--compare", "300"]).compare == 300
    run = parser.parse_args(["run"])
    assert run.two_pass is False and run.headed is False        # 2단 분류는 기본값이 끔
    assert parser.parse_args(["run", "--two-pass", "--headed"]).two_pass is True
    assert parser.parse_args(["recapture", "--headed"]).headed is True
    with pytest.raises(SystemExit):
        parser.parse_args(["collect", "--two-pass"])


@pytest.mark.parametrize(
    "argv",
    [["run", "--full", "--quick"], ["run", "--limit", "0"], ["run", "--limit", "많이"], ["unknown"]],
)
def test_bad_arguments_are_rejected(argv):
    with pytest.raises(SystemExit):
        build_parser().parse_args(argv)


@pytest.fixture
def site(write_config, fake, monkeypatch, tmp_path):
    """가짜 API에 영상 두 개를 두고 그 영상을 대상으로 하는 설정 파일 경로를 돌려준다."""
    fake.add_video(V1, title="첫 번째 영상 제목", comments=3)
    fake.add_video(V2, title="두 번째 영상", comments=2)
    monkeypatch.setattr(cli, "make_transport", lambda: fake)
    return str(write_config(f'youtube:\n  videos: ["{V1}", "{V2}"]\n'))


def stored(tmp_path, sql):
    with Database(tmp_path / "data" / "monitor.db") as db:
        return db.conn.execute(sql).fetchall()


def test_plan_shows_targets_and_estimate_without_collecting(site, fake, tmp_path, capsys):
    assert main(["plan", "--config", site]) == 0
    out = capsys.readouterr().out
    assert "이번에 처리할 영상: 2개 (지정 2)" in out
    assert "첫 번째 영상 제목" in out and "첫 수집" in out
    assert "새로 수집될 댓글: 약 5건" in out
    assert "약 20원" in out
    assert not fake.called("commentThreads")
    assert stored(tmp_path, "SELECT COUNT(*) FROM comments")[0][0] == 0
    assert stored(tmp_path, "SELECT COUNT(*) FROM videos")[0][0] == 0


def test_collect_asks_first_and_does_nothing_on_no(site, fake, tmp_path, capsys, monkeypatch):
    monkeypatch.setattr("builtins.input", lambda prompt: "n")
    assert main(["collect", "--config", site]) == 0
    assert "취소했습니다" in capsys.readouterr().out
    assert not fake.called("commentThreads")

    def closed(prompt):
        raise EOFError

    monkeypatch.setattr("builtins.input", closed)           # 입력을 받을 수 없으면 진행하지 않는다
    assert main(["collect", "--config", site]) == 0
    assert stored(tmp_path, "SELECT COUNT(*) FROM comments")[0][0] == 0


def test_collect_then_status_and_export(site, tmp_path, capsys):
    assert main(["collect", "--config", site, "--yes"]) == 0
    out = capsys.readouterr().out
    assert "[1/2]" in out and "[2/2]" in out
    assert "새 댓글 5건 · 수정 감지 0건 · 조회 불가 감지 0건" in out
    assert "이번 실행의 할당량 사용: 3 units" in out

    run = stored(tmp_path, "SELECT command, counts, limits_hit, quota_used, ended_at FROM runs")[0]
    assert run["command"] == "collect" and run["quota_used"] == 3 and run["ended_at"]
    assert json.loads(run["counts"])["new"] == 5 and json.loads(run["limits_hit"]) == []

    assert main(["status", "--config", site]) == 0
    out = capsys.readouterr().out
    assert "댓글: 5건" in out and "전체 수집 2" in out

    assert main(["export", "--config", site]) == 0
    out = capsys.readouterr().out
    assert "부정 댓글: 0건" in out and "작성자 랭킹: 0명" in out and "통계 요약: 2개 영상" in out
    book = load_workbook(tmp_path / "output" / "댓글증거.xlsx")
    assert book.sheetnames == ["부정 댓글", "작성자 랭킹", "작성자별 댓글", "통계 요약", "영상 목록"]
    assert not (tmp_path / "output" / "작성자별").exists()

    # 캡처가 없으면 작성자별 폴더는 만들 것이 없다.
    assert main(["folders", "--config", site]) == 0
    assert "복사할 캡처가 없습니다" in capsys.readouterr().out
    assert not (tmp_path / "output" / "작성자별").exists()

    with open(site, "a", encoding="utf-8") as handle:
        handle.write("export:\n  include_all_comments: true\n")
    assert main(["export", "--config", site]) == 0
    assert "전체 댓글: 5건" in capsys.readouterr().out
    sheet = load_workbook(tmp_path / "output" / "댓글증거.xlsx")["전체 댓글"]
    assert sheet.max_row == 6


def test_collect_reports_limits_and_quota_stop(write_config, fake, monkeypatch, tmp_path, capsys):
    fake.add_video(V1, comments=3)
    fake.add_video(V2, comments=2)
    monkeypatch.setattr(cli, "make_transport", lambda: fake)
    path = str(write_config(f"""
    youtube:
      videos: ["{V1}", "{V2}"]
      daily_quota_budget: 2
    limits:
      max_comments_per_video: 2
    """))
    assert main(["collect", "--config", path, "--yes"]) == 0
    out = capsys.readouterr().out
    assert "예상 할당량이 오늘 남은 예산을 넘습니다" in out
    assert "영상당 댓글 상한(2건)" in out
    assert "수집을 멈췄습니다" in out
    limits_hit = json.loads(stored(tmp_path, "SELECT limits_hit FROM runs")[0][0])
    assert "daily_quota_budget" in limits_hit


def test_video_option_limits_the_run_to_that_video(site, fake, capsys):
    assert main(["collect", "--config", site, "--yes", "--video", V2]) == 0
    assert "이번에 처리할 영상: 1개" in capsys.readouterr().out
    assert [call["videoId"] for call in fake.called("commentThreads")] == [V2]


def test_missing_api_key_is_explained(write_config, monkeypatch, capsys):
    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)
    assert main(["plan", "--config", str(write_config())]) == 2
    assert "YOUTUBE_API_KEY" in capsys.readouterr().err


def test_export_with_no_comments_still_writes_the_file(write_config, tmp_path, capsys):
    assert main(["export", "--config", str(write_config())]) == 0
    assert "부정 댓글: 0건" in capsys.readouterr().out
    assert (tmp_path / "output" / "댓글증거.xlsx").is_file()


def test_wide_characters_are_fitted_by_display_width():
    assert cli._width("가a") == 3
    fitted = cli._fit("가나다라마바사", 8)
    assert cli._width(fitted) == 8 and fitted.rstrip().endswith("…")
    assert cli._fit("ab", 4) == "ab  "


class RunCamera:
    """브라우저 대신 가짜 YouTube에 단 댓글 본문을 그대로 보여 준다."""

    def __init__(self, fake):
        self.fake = fake
        self.opened = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass

    def view(self, url, comment_id):
        from comment_monitor.capture import Seen

        self.opened.append(comment_id)
        return Seen(self.fake.find(comment_id)["text"], "@작성자", "첫 번째 영상 제목", "채널")

    def shoot(self):
        import io

        from PIL import Image

        out = io.BytesIO()
        Image.new("RGB", (400, 300), "white").save(out, format="PNG")
        return out.getvalue()


@pytest.fixture
def run_site(site, fake, monkeypatch):
    from .fake_claude import FakeClaude, judge, reply

    fake.add_comment(V1, "bad1", "욕하는 댓글")
    client = FakeClaude(lambda items, kw: reply([judge(i) for i in items], model=kw["model"]))
    camera = RunCamera(fake)
    monkeypatch.setattr(cli, "make_classifier_client", lambda: client)
    monkeypatch.setattr(cli, "make_camera", lambda *, headless: camera)
    return site, client, camera


def test_run_does_every_step_and_summarizes(run_site, tmp_path, capsys):
    site, client, camera = run_site
    assert main(["run", "--config", site, "--yes"]) == 0
    out = capsys.readouterr().out
    for heading in ("1/5 검토 결과 읽기", "2/5 대상 영상 확정과 수집", "3/5 AI 분류",
                    "4/5 캡처", "5/5 엑셀 출력", "실행 요약"):
        assert f"=== {heading} ===" in out
    assert "수집: 새 댓글 6건" in out
    assert "분류: 6건 · 이 가운데 부정 1건" in out
    assert "캡처: 1건 · 실패 0건" in out
    assert "유튜브 할당량 3 units" in out
    assert {call["model"] for call in client.calls} == {"claude-opus-5-5"}   # 기본은 단일 분류
    assert camera.opened == ["bad1"]
    commands = [row[0] for row in stored(tmp_path, "SELECT command FROM runs ORDER BY id")]
    assert commands == ["run:collect", "run:classify", "run:capture"]
    # 엑셀에는 방금 찍은 캡처까지 들어 있다.
    sheet = load_workbook(tmp_path / "output" / "댓글증거.xlsx")["부정 댓글"]
    values = [cell.value for row in sheet.iter_rows() for cell in row]
    assert any(isinstance(v, str) and "HYPERLINK" in v and "captures/" in v for v in values)

    # 다시 돌리면 새로 할 일이 없다.
    assert main(["run", "--config", site, "--yes"]) == 0
    out = capsys.readouterr().out
    assert "분류할 댓글이 없습니다" in out and "캡처할 댓글이 없습니다" in out
    assert "수집: 새 댓글 0건" in out


def test_run_two_pass_option(run_site, tmp_path, capsys):
    site, client, _ = run_site
    assert main(["run", "--config", site, "--yes", "--two-pass"]) == 0
    out = capsys.readouterr().out
    assert "2단 분류 claude-haiku-5-5 → claude-opus-5-5" in out
    assert "최종 판정으로 넘긴 댓글 1건" in out
    models = [call["model"] for call in client.calls]
    assert models == ["claude-haiku-5-5", "claude-opus-5-5"]
    finals = stored(tmp_path, "SELECT COUNT(*) FROM classifications WHERE stage = 'final'")
    assert finals[0][0] == 6


def test_run_stops_when_the_user_says_no(run_site, tmp_path, monkeypatch, capsys):
    site, client, camera = run_site
    monkeypatch.setattr("builtins.input", lambda prompt: "n")
    assert main(["run", "--config", site]) == 0
    out = capsys.readouterr().out
    assert "다음 단계는 진행하지 않았습니다" in out and "=== 실행 요약 ===" in out
    assert "=== 3/5 AI 분류 ===" not in out
    assert not client.calls and not camera.opened
    assert not (tmp_path / "output" / "댓글증거.xlsx").exists()


def test_run_goes_on_to_capture_when_classification_fails(run_site, tmp_path, capsys):
    import anthropic
    import httpx2 as httpx

    site, client, camera = run_site
    assert main(["collect", "--config", site, "--yes"]) == 0
    with Database(tmp_path / "data" / "monitor.db") as db:
        mark_negative(db, "bad1", severity=4, target="홍길동")      # 앞선 실행에서 판정된 댓글
    capsys.readouterr()

    def overloaded(items, kwargs):
        request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
        raise anthropic.InternalServerError(
            "overloaded", response=httpx.Response(529, request=request), body=None
        )

    client.handler = overloaded
    assert main(["run", "--config", site, "--yes"]) == 1
    captured = capsys.readouterr()
    assert "분류 요청이 실패했습니다" in captured.err
    assert camera.opened == ["bad1"]                       # 이미 판정된 댓글은 그래도 찍는다
    assert "문제가 있었던 단계: AI 분류" in captured.out
    assert (tmp_path / "output" / "댓글증거.xlsx").exists()
