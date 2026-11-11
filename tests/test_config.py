from datetime import date
from pathlib import Path

import pytest

from comment_monitor.config import (
    ConfigError,
    apply_overrides,
    load_config,
    parse_video_id,
    require_env,
)

ROOT = Path(__file__).resolve().parents[1]


def test_example_config_loads():
    config = load_config(ROOT / "config.example.yaml")
    assert config.target_names == ("가수 이름", "그룹명")
    assert config.targets[0].note == "그룹명의 보컬"
    assert config.youtube.videos == ("xxxxxxxxxxx",)
    assert config.youtube.search.enabled is False
    assert config.youtube.search.published_after == date(2026, 1, 1)
    assert config.limits.max_classify_per_run == 5000
    assert config.classifier.model == "claude-opus-5-5"
    assert config.capture.targets == ("가수 이름", "그룹명", "불명")
    assert config.export.path == ROOT / "output" / "댓글증거.xlsx"
    assert config.db_path == ROOT / "data" / "monitor.db"


def test_defaults_when_only_target_given(write_config):
    config = load_config(write_config())
    assert config.targets[0].aliases == ()
    assert config.targets[0].note == ""
    assert config.capture.targets == ("홍길동", "불명")
    assert config.youtube.videos == ()
    assert config.youtube.daily_quota_budget == 8000
    assert config.youtube.search.keywords == ()
    assert config.limits.full_rescan_max_comments == 5000
    assert config.capture.min_severity == 2


def test_keywords_take_defaults_unless_overridden(write_config):
    config = load_config(write_config("""
        youtube:
          search:
            default_top_n: 20
            order: date
            keywords:
              - "홍길동"
              - { q: "홍길동 논란", top_n: 10, order: viewCount }
    """))
    plain, custom = config.youtube.search.keywords
    assert (plain.q, plain.top_n, plain.order) == ("홍길동", 20, "date")
    assert (custom.q, custom.top_n, custom.order) == ("홍길동 논란", 10, "viewCount")


def test_search_top_overrides_every_keyword(write_config):
    config = load_config(write_config("""
        youtube:
          search:
            enabled: true
            keywords: ["홍길동", { q: "홍길동 논란", top_n: 10 }]
    """))
    overridden = apply_overrides(config, search_top=3)
    assert [k.top_n for k in overridden.youtube.search.keywords] == [3, 3]
    assert overridden.youtube.search.enabled is True
    # 원래 설정은 그대로다.
    assert [k.top_n for k in config.youtube.search.keywords] == [20, 10]


def test_video_override_processes_only_that_video(write_config):
    config = load_config(write_config("""
        youtube:
          videos: ["https://www.youtube.com/watch?v=aaaaaaaaaaa"]
          channels: ["UCaaaaaaaaaaaaaaaaaaaaaa"]
          search: { enabled: true, keywords: ["홍길동"] }
    """))
    overridden = apply_overrides(config, videos=["https://youtu.be/bbbbbbbbbbb"])
    assert overridden.youtube.videos == ("bbbbbbbbbbb",)
    assert overridden.youtube.channels == ()
    assert overridden.youtube.search.enabled is False


def test_no_search_and_max_comments_overrides(write_config):
    config = load_config(write_config("""
        youtube:
          search: { enabled: true, keywords: ["홍길동"] }
    """))
    overridden = apply_overrides(config, no_search=True, max_comments=500)
    assert overridden.youtube.search.enabled is False
    assert overridden.limits.max_comments_per_video == 500


def test_max_videos_defaults_to_50_and_can_be_overridden(write_config):
    config = load_config(write_config(""))
    assert config.limits.max_videos_per_run == 50
    assert apply_overrides(config).limits.max_videos_per_run == 50
    assert apply_overrides(config, max_videos=10).limits.max_videos_per_run == 10


@pytest.mark.parametrize(
    "value",
    [
        "dQw4w9WgXcQ",
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ&lc=Ugxyz&t=10s",
        "https://m.youtube.com/watch?feature=share&v=dQw4w9WgXcQ",
        "https://youtu.be/dQw4w9WgXcQ?si=abc",
        "https://www.youtube.com/shorts/dQw4w9WgXcQ",
        "https://www.youtube.com/live/dQw4w9WgXcQ",
        "youtube.com/watch?v=dQw4w9WgXcQ",
    ],
)
def test_parse_video_id(value):
    assert parse_video_id(value) == "dQw4w9WgXcQ"


@pytest.mark.parametrize(
    "value",
    ["", "https://example.com/watch?v=dQw4w9WgXcQ", "https://www.youtube.com/@channel", "짧음"],
)
def test_parse_video_id_rejects(value):
    with pytest.raises(ConfigError):
        parse_video_id(value)


def test_video_urls_are_normalized_and_deduplicated(write_config):
    config = load_config(write_config("""
        youtube:
          videos:
            - "https://www.youtube.com/watch?v=aaaaaaaaaaa"
            - "https://youtu.be/aaaaaaaaaaa"
          exclude_videos: ["bbbbbbbbbbb"]
    """))
    assert config.youtube.videos == ("aaaaaaaaaaa",)
    assert config.youtube.exclude_videos == ("bbbbbbbbbbb",)


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("limits:\n  max_clasify_per_run: 10\n", "limits.max_clasify_per_run"),
        ("limit:\n  max_classify_per_run: 10\n", "limit"),
        ("limits:\n  max_classify_per_run: 0\n", "limits.max_classify_per_run"),
        ("limits:\n  max_classify_per_run: '많이'\n", "limits.max_classify_per_run"),
        ("limits:\n  max_classify_per_run: true\n", "limits.max_classify_per_run"),
        ("youtube:\n  search:\n    order: popular\n", "youtube.search.order"),
        ("youtube:\n  search:\n    published_after: '작년'\n", "youtube.search.published_after"),
        ("youtube:\n  search:\n    keywords: [{ top_n: 3 }]\n", "youtube.search.keywords[0].q"),
        ("youtube:\n  videos: ['https://example.com/x']\n", "youtube.videos"),
        ("youtube:\n  channels: ['@handle']\n", "youtube.channels"),
        ("classifier:\n  mode: triple\n", "classifier.mode"),
        ("capture:\n  min_severity: 6\n", "capture.min_severity"),
        ("capture:\n  targets: ['모두']\n", "capture.targets"),
        ("capture:\n  headless: 'yes'\n", "capture.headless"),
    ],
)
def test_invalid_config_names_the_offending_key(write_config, body, expected):
    with pytest.raises(ConfigError, match=expected.replace("[", r"\[").replace("]", r"\]")):
        load_config(write_config(body))


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("targets:\n  - aliases: []\n", r"targets\[0\].name"),
        ("targets: []\n", "targets"),
        ("youtube: {}\n", "targets"),
        ("targets:\n  - name: 타인\n", r"targets\[0\].name"),
        ("targets:\n  - name: 가\n  - name: 가\n", r"targets\[1\].name"),
        ("targets:\n  - name: 가\n    memo: x\n", r"targets\[0\].memo"),
        ("targets:\n  - name: 가\n    note: 3\n", r"targets\[0\].note"),
        ("target:\n  name: 가\n", "targets 목록으로"),
    ],
)
def test_invalid_targets(tmp_path, text, expected):
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError, match=expected):
        load_config(path)


def test_multiple_targets_and_capture_default(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        "targets:\n"
        "  - { name: 멤버, aliases: [별명], note: 그룹의 보컬 }\n"
        "  - { name: 그룹 }\n",
        encoding="utf-8",
    )
    config = load_config(path)
    assert config.target_names == ("멤버", "그룹")
    assert config.targets[0].aliases == ("별명",)
    assert config.target_values == ("멤버", "그룹", "타인", "불명")
    # 적지 않으면 타인을 제외한 전부
    assert config.capture.targets == ("멤버", "그룹", "불명")


def test_capture_targets_can_be_narrowed(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        "targets:\n  - name: 멤버\n  - name: 그룹\ncapture:\n  targets: [멤버]\n",
        encoding="utf-8",
    )
    assert load_config(path).capture.targets == ("멤버",)


def test_missing_config_file_points_to_example(tmp_path):
    with pytest.raises(ConfigError, match="config.example.yaml"):
        load_config(tmp_path / "config.yaml")


def test_env_file_next_to_config_is_loaded(write_config, tmp_path, monkeypatch):
    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    (tmp_path / ".env").write_text("YOUTUBE_API_KEY=abc123\n", encoding="utf-8")
    load_config(write_config())
    try:
        assert require_env("YOUTUBE_API_KEY") == "abc123"
        with pytest.raises(ConfigError, match="ANTHROPIC_API_KEY"):
            require_env("ANTHROPIC_API_KEY")
    finally:
        import os

        os.environ.pop("YOUTUBE_API_KEY", None)


def test_old_two_pass_mode_points_to_the_run_option(write_config):
    # 예전 설정의 mode: single은 그대로 읽히고, two_pass는 새 옵션을 알려준다.
    assert load_config(write_config("classifier:\n  mode: single\n")).classifier.batch_size == 20
    with pytest.raises(ConfigError, match="--two-pass"):
        load_config(write_config("classifier:\n  mode: two_pass\n"))
