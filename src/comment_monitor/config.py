"""config.yaml과 .env를 읽어 검증된 설정 객체로 만든다.

설정 오류는 돈과 시간이 드는 작업을 시작하기 전에 잡아야 하므로,
알 수 없는 항목과 잘못된 값은 조용히 넘기지 않고 ConfigError로 알린다.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import yaml
from dotenv import load_dotenv

SEARCH_ORDERS = ("relevance", "viewCount", "date")
# AI 판정의 "대상" 값은 targets의 각 name과 아래 두 값이다.
TARGET_OTHER = "타인"      # 설정한 대상이 아닌 사람을 향한 댓글
TARGET_UNKNOWN = "불명"    # 누구를 향한 것인지 알 수 없는 댓글
RESERVED_TARGET_VALUES = (TARGET_OTHER, TARGET_UNKNOWN)

DEFAULT_CONFIG_NAME = "config.yaml"
DEFAULT_DB_PATH = "data/monitor.db"

_VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}")
_CHANNEL_ID = re.compile(r"UC[A-Za-z0-9_-]{22}")
_VIDEO_PATH_PREFIXES = ("shorts", "live", "embed", "v")


class ConfigError(Exception):
    """설정 파일이나 환경 변수에 문제가 있을 때."""


@dataclass(frozen=True)
class Target:
    name: str  # AI 판정의 "대상" 값으로 그대로 저장된다
    aliases: tuple[str, ...]
    note: str  # 그룹인지 멤버인지 같은, AI에게 주는 설명


@dataclass(frozen=True)
class SearchKeyword:
    q: str
    top_n: int
    order: str


@dataclass(frozen=True)
class SearchConfig:
    enabled: bool
    default_top_n: int
    order: str
    published_after: date | None
    cache_days: int
    keywords: tuple[SearchKeyword, ...]


@dataclass(frozen=True)
class YoutubeConfig:
    videos: tuple[str, ...]  # 영상 ID
    channels: tuple[str, ...]  # 채널 ID
    channel_max_videos: int
    search: SearchConfig
    exclude_videos: tuple[str, ...]  # 영상 ID
    daily_quota_budget: int


@dataclass(frozen=True)
class Limits:
    max_videos_per_run: int
    max_comments_per_video: int
    full_rescan_max_comments: int
    max_classify_per_run: int
    max_captures_per_run: int
    confirm_cost_over_krw: int


@dataclass(frozen=True)
class ClassifierConfig:
    model: str
    first_pass_model: str
    batch_size: int
    prompt_version: str


@dataclass(frozen=True)
class CaptureConfig:
    min_severity: int
    targets: tuple[str, ...]  # 캡처할 "대상" 값. 기본은 타인을 제외한 전부
    headless: bool


@dataclass(frozen=True)
class ExportConfig:
    path: Path
    include_all_comments: bool


@dataclass(frozen=True)
class Config:
    targets: tuple[Target, ...]
    youtube: YoutubeConfig
    limits: Limits
    classifier: ClassifierConfig
    capture: CaptureConfig
    export: ExportConfig
    base_dir: Path
    db_path: Path

    @property
    def target_names(self) -> tuple[str, ...]:
        return tuple(target.name for target in self.targets)

    @property
    def target_values(self) -> tuple[str, ...]:
        """AI 판정의 "대상"에 올 수 있는 값 전부."""
        return (*self.target_names, *RESERVED_TARGET_VALUES)


def parse_video_id(value: str) -> str:
    """영상 주소나 ID에서 11자리 영상 ID를 꺼낸다."""
    text = value.strip()
    if _VIDEO_ID.fullmatch(text):
        return text
    parsed = urlparse(text if "://" in text else f"https://{text}")
    host = (parsed.hostname or "").lower().removeprefix("www.").removeprefix("m.")
    candidate = ""
    if host == "youtu.be":
        candidate = parsed.path.strip("/").split("/")[0]
    elif host in ("youtube.com", "music.youtube.com"):
        parts = [p for p in parsed.path.split("/") if p]
        if parts and parts[0] == "watch":
            candidate = parse_qs(parsed.query).get("v", [""])[0]
        elif len(parts) >= 2 and parts[0] in _VIDEO_PATH_PREFIXES:
            candidate = parts[1]
    if not _VIDEO_ID.fullmatch(candidate):
        raise ConfigError(f"유튜브 영상 주소나 영상 ID로 읽을 수 없습니다: {value!r}")
    return candidate


_REQUIRED: Any = object()


class _Section:
    """설정의 한 묶음. 읽은 항목을 지워 가다가 끝에 남은 것을 오타로 보고 알린다."""

    def __init__(self, data: Any, path: str) -> None:
        if data is None:
            data = {}
        if not isinstance(data, dict):
            raise ConfigError(f"{path or '설정 파일'}: 하위 항목을 가진 묶음이어야 합니다.")
        self._data = dict(data)
        self._path = path

    def name(self, key: str) -> str:
        return f"{self._path}.{key}" if self._path else key

    def _take(self, key: str, default: Any) -> Any:
        value = self._data.pop(key, None)
        if value is None:
            if default is _REQUIRED:
                raise ConfigError(f"{self.name(key)}: 값이 필요합니다.")
            return default
        return value

    def section(self, key: str) -> _Section:
        return _Section(self._data.pop(key, None), self.name(key))

    def string(self, key: str, default: Any = _REQUIRED) -> str:
        value = self._take(key, default)
        if not isinstance(value, str) or not value.strip():
            raise ConfigError(f"{self.name(key)}: 비어 있지 않은 문자열이어야 합니다.")
        return value.strip()

    def integer(self, key: str, default: int, *, minimum: int = 1, maximum: int | None = None) -> int:
        value = self._take(key, default)
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigError(f"{self.name(key)}: 정수여야 합니다. (현재 값: {value!r})")
        if value < minimum or (maximum is not None and value > maximum):
            bound = f"{minimum} 이상" if maximum is None else f"{minimum}~{maximum}"
            raise ConfigError(f"{self.name(key)}: {bound}이어야 합니다. (현재 값: {value})")
        return value

    def boolean(self, key: str, default: bool) -> bool:
        value = self._take(key, default)
        if not isinstance(value, bool):
            raise ConfigError(f"{self.name(key)}: true 또는 false여야 합니다. (현재 값: {value!r})")
        return value

    def choice(self, key: str, default: str, choices: tuple[str, ...]) -> str:
        value = self._take(key, default)
        if value not in choices:
            raise ConfigError(
                f"{self.name(key)}: {', '.join(choices)} 중 하나여야 합니다. (현재 값: {value!r})"
            )
        return value

    def list_(self, key: str) -> list[Any]:
        value = self._take(key, [])
        if not isinstance(value, list):
            raise ConfigError(f"{self.name(key)}: 목록이어야 합니다.")
        return value

    def strings(self, key: str) -> tuple[str, ...]:
        items = self.list_(key)
        for item in items:
            if not isinstance(item, str) or not item.strip():
                raise ConfigError(f"{self.name(key)}: 비어 있지 않은 문자열의 목록이어야 합니다.")
        return tuple(item.strip() for item in items)

    def finish(self) -> None:
        if self._data:
            unknown = ", ".join(self.name(str(k)) for k in self._data)
            raise ConfigError(f"알 수 없는 설정 항목입니다: {unknown}")


def _unique(items: list[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(items))


def _video_ids(section: _Section, key: str) -> tuple[str, ...]:
    ids = []
    for value in section.strings(key):
        try:
            ids.append(parse_video_id(value))
        except ConfigError as exc:
            raise ConfigError(f"{section.name(key)}: {exc}") from None
    return _unique(ids)


def _published_after(section: _Section) -> date | None:
    key = "published_after"
    value = section._take(key, None)
    if value is None:
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        raise ConfigError(
            f"{section.name(key)}: 2026-01-01 형식의 날짜여야 합니다. (현재 값: {value!r})"
        ) from None


def _search(section: _Section) -> SearchConfig:
    enabled = section.boolean("enabled", False)
    default_top_n = section.integer("default_top_n", 20)
    order = section.choice("order", "relevance", SEARCH_ORDERS)
    published_after = _published_after(section)
    cache_days = section.integer("cache_days", 7, minimum=0)

    keywords = []
    for index, raw in enumerate(section.list_("keywords")):
        path = f"{section.name('keywords')}[{index}]"
        if isinstance(raw, str):
            raw = {"q": raw}
        item = _Section(raw, path)
        keywords.append(
            SearchKeyword(
                q=item.string("q"),
                top_n=item.integer("top_n", default_top_n),
                order=item.choice("order", order, SEARCH_ORDERS),
            )
        )
        item.finish()
    section.finish()
    return SearchConfig(
        enabled=enabled,
        default_top_n=default_top_n,
        order=order,
        published_after=published_after,
        cache_days=cache_days,
        keywords=tuple(keywords),
    )


def _youtube(section: _Section) -> YoutubeConfig:
    channels = section.strings("channels")
    for channel in channels:
        if not _CHANNEL_ID.fullmatch(channel):
            raise ConfigError(
                f"{section.name('channels')}: UC로 시작하는 24자리 채널 ID여야 합니다. "
                f"(현재 값: {channel!r})"
            )
    result = YoutubeConfig(
        videos=_video_ids(section, "videos"),
        channels=_unique(list(channels)),
        channel_max_videos=section.integer("channel_max_videos", 100),
        search=_search(section.section("search")),
        exclude_videos=_video_ids(section, "exclude_videos"),
        daily_quota_budget=section.integer("daily_quota_budget", 8000),
    )
    section.finish()
    return result


def _limits(section: _Section) -> Limits:
    result = Limits(
        max_videos_per_run=section.integer("max_videos_per_run", 50),
        max_comments_per_video=section.integer("max_comments_per_video", 20000),
        full_rescan_max_comments=section.integer("full_rescan_max_comments", 5000, minimum=0),
        max_classify_per_run=section.integer("max_classify_per_run", 5000),
        max_captures_per_run=section.integer("max_captures_per_run", 300),
        confirm_cost_over_krw=section.integer("confirm_cost_over_krw", 10000, minimum=0),
    )
    section.finish()
    return result


def _classifier(section: _Section) -> ClassifierConfig:
    # 예전 설정의 mode 항목. 2단 분류는 이제 `run --two-pass`로 켠다.
    if section.choice("mode", "single", ("single", "two_pass")) == "two_pass":
        raise ConfigError(
            "classifier.mode 설정은 없어졌습니다. 2단 분류는 `run --two-pass` 옵션으로 켭니다. "
            "config.yaml에서 mode 줄을 지워 주세요."
        )
    result = ClassifierConfig(
        model=section.string("model", "claude-opus-5-5"),
        first_pass_model=section.string("first_pass_model", "claude-haiku-5-5"),
        batch_size=section.integer("batch_size", 20),
        prompt_version=section.string("prompt_version", "v1"),
    )
    section.finish()
    return result


def _targets(root: _Section) -> tuple[Target, ...]:
    if "target" in root._data:
        raise ConfigError(
            "target: 이 항목은 targets 목록으로 바뀌었습니다. "
            "config.example.yaml의 targets를 참고해 고쳐 주세요."
        )
    key = "targets"
    targets: list[Target] = []
    for index, raw in enumerate(root.list_(key)):
        item = _Section(raw, f"{key}[{index}]")
        name = item.string("name")
        if name in RESERVED_TARGET_VALUES:
            raise ConfigError(
                f"{item.name('name')}: {name!r}은 판정 값으로 따로 쓰이므로 대상 이름으로 쓸 수 없습니다."
            )
        if any(name == other.name for other in targets):
            raise ConfigError(f"{item.name('name')}: 대상 이름이 겹칩니다. (현재 값: {name!r})")
        note = item._take("note", "")
        if not isinstance(note, str):
            raise ConfigError(f"{item.name('note')}: 문자열이어야 합니다. (현재 값: {note!r})")
        targets.append(Target(name=name, aliases=item.strings("aliases"), note=note.strip()))
        item.finish()
    if not targets:
        raise ConfigError(f"{key}: 대상을 한 명 이상 적어야 합니다.")
    return tuple(targets)


def _capture(section: _Section, target_names: tuple[str, ...]) -> CaptureConfig:
    min_severity = section.integer("min_severity", 2, minimum=1, maximum=5)
    allowed = (*target_names, *RESERVED_TARGET_VALUES)
    # 적지 않으면 타인을 제외한 전부: 설정한 대상 전원과 불명.
    targets = _unique(list(section.strings("targets"))) or (*target_names, TARGET_UNKNOWN)
    for target in targets:
        if target not in allowed:
            raise ConfigError(
                f"{section.name('targets')}: {', '.join(allowed)} 중에서 골라야 합니다. "
                f"(현재 값: {target!r})"
            )
    result = CaptureConfig(
        min_severity=min_severity,
        targets=targets,
        headless=section.boolean("headless", True),
    )
    section.finish()
    return result


def load_config(path: str | Path = DEFAULT_CONFIG_NAME) -> Config:
    """설정 파일을 읽는다. 상대 경로는 설정 파일이 있는 폴더를 기준으로 푼다."""
    config_path = Path(path).expanduser().resolve()
    if not config_path.is_file():
        raise ConfigError(
            f"설정 파일이 없습니다: {config_path}\n"
            "config.example.yaml을 config.yaml로 복사한 뒤 값을 채워 주세요."
        )
    try:
        data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"설정 파일을 읽을 수 없습니다: {config_path}\n{exc}") from None

    base_dir = config_path.parent
    load_dotenv(base_dir / ".env")

    root = _Section(data, "")
    targets = _targets(root)
    export = root.section("export")
    config = Config(
        targets=targets,
        youtube=_youtube(root.section("youtube")),
        limits=_limits(root.section("limits")),
        classifier=_classifier(root.section("classifier")),
        capture=_capture(root.section("capture"), tuple(t.name for t in targets)),
        export=ExportConfig(
            path=base_dir / export.string("path", "output/댓글증거.xlsx"),
            include_all_comments=export.boolean("include_all_comments", False),
        ),
        base_dir=base_dir,
        db_path=base_dir / DEFAULT_DB_PATH,
    )
    export.finish()
    root.finish()
    return config


def apply_overrides(
    config: Config,
    *,
    videos: list[str] | None = None,
    no_search: bool = False,
    search_top: int | None = None,
    max_comments: int | None = None,
    max_videos: int | None = None,
) -> Config:
    """명령줄 옵션을 이번 실행에 한해 설정 위에 덮어쓴다.

    우선순위는 --search-top, 키워드별 top_n, default_top_n 순이다.
    --video를 주면 그 영상만 처리하므로 채널과 검색은 끈다.
    """
    youtube = config.youtube
    search = youtube.search
    if search_top is not None:
        search = replace(
            search,
            default_top_n=search_top,
            keywords=tuple(replace(k, top_n=search_top) for k in search.keywords),
        )
    if no_search or videos:
        search = replace(search, enabled=False)
    youtube = replace(youtube, search=search)
    if videos:
        youtube = replace(
            youtube, videos=_unique([parse_video_id(v) for v in videos]), channels=()
        )
    limits = config.limits
    if max_comments is not None:
        limits = replace(limits, max_comments_per_video=max_comments)
    if max_videos is not None:
        limits = replace(limits, max_videos_per_run=max_videos)
    return replace(config, youtube=youtube, limits=limits)


def require_env(name: str) -> str:
    """API 키처럼 .env에서 오는 값을 꺼낸다. 없으면 어디에 적어야 하는지 알린다."""
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(
            f"{name}가 설정되지 않았습니다. .env.example을 .env로 복사한 뒤 값을 채워 주세요."
        )
    return value
