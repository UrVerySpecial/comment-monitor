import textwrap

import pytest

from comment_monitor.config import load_config
from comment_monitor.db import Database

from .fake_youtube import FakeYouTube

VIDEO_ID = "dQw4w9WgXcQ"


@pytest.fixture
def db(tmp_path):
    with Database(tmp_path / "data" / "monitor.db") as database:
        database.conn.execute(
            "INSERT INTO videos (platform, video_id, title, source, first_seen_at) "
            "VALUES ('youtube', ?, '시험 영상', 'direct', '2026-11-01T00:00:00Z')",
            (VIDEO_ID,),
        )
        yield database


@pytest.fixture
def write_config(tmp_path):
    """YAML 본문을 받아 임시 폴더에 config.yaml로 쓰고 경로를 돌려준다."""

    def write(body: str = ""):
        path = tmp_path / "config.yaml"
        text = 'targets:\n  - name: "홍길동"\n' + textwrap.dedent(body)
        path.write_text(text, encoding="utf-8")
        return path

    return write


@pytest.fixture
def fake():
    return FakeYouTube()


@pytest.fixture
def store(tmp_path):
    """영상이 하나도 없는 빈 DB."""
    with Database(tmp_path / "data" / "monitor.db") as database:
        yield database


@pytest.fixture
def cfg(write_config):
    """YAML 본문을 받아 읽어들인 설정을 돌려준다."""
    return lambda body="": load_config(write_config(body))
