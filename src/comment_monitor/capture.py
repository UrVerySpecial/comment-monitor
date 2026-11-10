"""캡처 (DESIGN.md 11장).

캡처 대상 댓글의 고정 주소를 브라우저로 열고, 화면에 강조된 댓글이 DB의 본문과 같은지 확인한 뒤 찍는다.
가공하지 않은 원본과, 그 위에 머리띠를 붙인 합성본을 저장하고 두 파일의 해시를 DB에 남긴다.

브라우저를 다루는 부분은 YouTubeCamera에만 있다. Capturer는 Camera 규약만 알기 때문에
시험에서는 브라우저 없이 가짜 카메라로 돌린다.
"""

from __future__ import annotations

import hashlib
import io
import re
import time
import unicodedata
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import parse_qs, urlparse

from PIL import Image, ImageDraw, ImageFont

from .collectors.youtube import comment_url
from .config import Config
from .db import Database, to_kst, utc_now

CAPTURE_DIR = "captures"
RAW_DIR = "raw"
VIEWPORT_WIDTH, VIEWPORT_HEIGHT = 1280, 900
# 댓글 사이에 쉬는 시간(초). 천천히 열어야 차단되지 않는다.
PAUSE_SECONDS = 4.0
# 찍힌 화면에서 강조 댓글 아래에 남기는 여백
BOTTOM_MARGIN = 24


class CaptureError(Exception):
    """캡처를 아예 진행할 수 없을 때. 브라우저가 없거나 실행 도중 닫힌 경우."""


class CaptureFailed(Exception):
    """이 댓글만 찍지 못했을 때. 사유를 기록하고 다음 댓글로 넘어간다."""


class Blocked(Exception):
    """차단 화면이 나왔을 때. 더 열지 않고 멈춘다."""


@dataclass(frozen=True)
class Seen:
    """화면에서 읽은 강조 댓글과 영상 정보."""

    text: str
    author: str
    title: str
    channel: str


class Camera(Protocol):
    def view(self, url: str, comment_id: str) -> Seen:
        """댓글 고정 주소를 열고 강조된 댓글을 읽는다."""

    def shoot(self) -> bytes:
        """방금 연 화면을 PNG로 찍는다. 영상 제목부터 강조 댓글까지 한 장에 담는다."""


# ---- 본문 대조 ----

# 화면과 수집본 사이에서 달라질 수 있지만 내용과 무관한 문자: 폭 없는 문자, 이모지 변형 선택자
_INVISIBLE = re.compile("[\u200b-\u200d\u2060\ufe0e\ufe0f\ufeff]")


def normalize_text(text: str | None) -> str:
    """대조용. 공백과 줄바꿈, 보이지 않는 문자의 차이는 무시한다."""
    text = unicodedata.normalize("NFC", text or "")
    return "".join(_INVISIBLE.sub("", text).split())


def same_text(shown: str | None, stored: str | None) -> bool:
    return normalize_text(shown) == normalize_text(stored)


# ---- 머리띠 합성 ----

_FONT_CANDIDATES = (
    "/System/Library/Fonts/AppleSDGothicNeo.ttc",              # macOS
    "/System/Library/Fonts/Supplemental/AppleGothic.ttf",
    "C:/Windows/Fonts/malgun.ttf",                             # Windows
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",  # Linux
    "/usr/share/fonts/truetype/nanum/NanumGothic.ttf",
)
_BAND_COLOR, _LABEL_COLOR, _VALUE_COLOR = "#111827", "#9CA3AF", "#F9FAFB"
_PADDING, _LABEL_WIDTH, _FONT_SIZE, _LINE_GAP = 20, 150, 18, 8


def _font(size: int) -> ImageFont.ImageFont:
    for path in _FONT_CANDIDATES:
        if Path(path).is_file():
            return ImageFont.truetype(path, size)
    # 한글 글꼴을 찾지 못하면 Pillow 기본 글꼴. 한글은 네모로 나온다.
    return ImageFont.load_default(size)


def _wrap(text: str, font: ImageFont.ImageFont, width: int) -> list[str]:
    """주소처럼 띄어쓰기가 없는 값도 넘치지 않게 글자 단위로 줄을 나눈다."""
    lines, current = [], ""
    for char in " ".join(text.split()):
        if current and font.getlength(current + char) > width:
            lines.append(current)
            current = char.lstrip()
        else:
            current += char
    return [*lines, current] if current or not lines else lines


def header_fields(row: Any, captured_at: str) -> list[tuple[str, str]]:
    """머리띠에 적는 항목. 유튜브 화면은 상대 시각만 보여주므로 정확한 시각은 여기에 담는다."""
    author = row["author_name"] or "(이름 없음)"
    if row["author_channel_id"]:
        author += f"  ·  채널 ID {row['author_channel_id']}"
    return [
        ("증거 번호", f"{row['evidence_no']:06d}"),
        ("댓글 주소", comment_url(row["video_id"], row["comment_id"])),
        ("영상 제목", row["video_title"] or ""),
        ("작성자", author),
        ("작성 시각", f"{to_kst(row['published_at'])} (KST)" if row["published_at"] else "알 수 없음"),
        ("캡처 시각", f"{to_kst(captured_at)} (KST)"),
    ]


def compose(raw_png: bytes, fields: Sequence[tuple[str, str]]) -> bytes:
    """원본 스크린샷 위에 머리띠를 붙인 합성본(PNG)."""
    shot = Image.open(io.BytesIO(raw_png)).convert("RGB")
    font = _font(_FONT_SIZE)
    value_width = max(shot.width - 2 * _PADDING - _LABEL_WIDTH, 100)
    wrapped = [(label, _wrap(value, font, value_width)) for label, value in fields]
    line_height = _FONT_SIZE + _LINE_GAP
    band_height = 2 * _PADDING + line_height * sum(len(lines) for _, lines in wrapped) - _LINE_GAP

    canvas = Image.new("RGB", (shot.width, band_height + shot.height), _BAND_COLOR)
    draw = ImageDraw.Draw(canvas)
    y = _PADDING
    for label, lines in wrapped:
        draw.text((_PADDING, y), label, font=font, fill=_LABEL_COLOR)
        for line in lines:
            draw.text((_PADDING + _LABEL_WIDTH, y), line, font=font, fill=_VALUE_COLOR)
            y += line_height
    canvas.paste(shot, (0, band_height))
    out = io.BytesIO()
    canvas.save(out, format="PNG")
    return out.getvalue()


# ---- 실행 ----


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def capture_name(row: Any) -> str:
    """파일 이름. 증거 번호_댓글ID.png"""
    safe_id = re.sub(r"[^A-Za-z0-9._-]", "_", row["comment_id"])
    return f"{row['evidence_no']:06d}_{safe_id}.png"


@dataclass
class CaptureSummary:
    total: int = 0
    done: int = 0
    captured: int = 0
    failures: list[tuple[int, str]] = field(default_factory=list)  # (증거 번호, 사유)
    blocked: str | None = None                                     # 차단되어 멈춘 사유
    saved: list[Path] = field(default_factory=list)                # 이번에 만든 합성본

    @property
    def failed(self) -> int:
        return len(self.failures)


class Capturer:
    """댓글을 한 건씩 열어 확인하고 찍어서 저장한다."""

    def __init__(
        self,
        config: Config,
        db: Database,
        camera: Camera,
        *,
        run_id: int | None = None,
        pause: float = PAUSE_SECONDS,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], str] = utc_now,
    ) -> None:
        self.db = db
        self.camera = camera
        self.run_id = run_id
        self.pause = pause
        self.sleep = sleep
        self.now = now
        # 엑셀 파일이 있는 폴더. DB에는 이 폴더 기준 상대 경로를 적어야 폴더째 옮겨도 링크가 열린다.
        self.output_dir = config.export.path.parent
        self.summary = CaptureSummary()

    def run(
        self,
        rows: Iterable[Any],
        on_progress: Callable[[CaptureSummary, Any, str | None], None] | None = None,
    ) -> CaptureSummary:
        rows = list(rows)
        summary = self.summary
        summary.total += len(rows)
        for index, row in enumerate(rows):
            if index:
                self.sleep(self.pause)
            error = None
            try:
                self._capture(row)
                summary.captured += 1
            except Blocked as exc:
                summary.blocked = str(exc)
                break
            except CaptureFailed as exc:
                error = str(exc)
                summary.failures.append((row["evidence_no"], error))
                self.db.save_capture(
                    row["platform"], row["comment_id"], error=error,
                    captured_at=self.now(), run_id=self.run_id,
                )
            summary.done += 1
            if on_progress:
                on_progress(summary, row, error)
        return summary

    def _capture(self, row: Any) -> None:
        seen = self.camera.view(comment_url(row["video_id"], row["comment_id"]), row["comment_id"])
        if not same_text(seen.text, row["text"]):
            raise CaptureFailed("화면의 댓글 본문이 수집한 본문과 다름 (수집 뒤 수정되었을 수 있음)")
        missing = [
            name for name, value in
            (("영상 제목", seen.title), ("영상 채널명", seen.channel), ("작성자 표시명", seen.author))
            if not value.strip()
        ]
        if missing:
            raise CaptureFailed(f"화면에 {', '.join(missing)}이(가) 보이지 않음")

        raw = self.camera.shoot()
        captured_at = self.now()
        composite = compose(raw, header_fields(row, captured_at))

        name = capture_name(row)
        composite_rel = Path(CAPTURE_DIR) / name
        raw_rel = Path(CAPTURE_DIR) / RAW_DIR / name
        (self.output_dir / raw_rel).parent.mkdir(parents=True, exist_ok=True)
        (self.output_dir / raw_rel).write_bytes(raw)
        (self.output_dir / composite_rel).write_bytes(composite)
        # 파일을 다 쓴 뒤에 기록한다. 도중에 끊기면 기록이 없으므로 다음 실행에서 다시 찍는다.
        self.db.save_capture(
            row["platform"], row["comment_id"], captured_at=captured_at, run_id=self.run_id,
            composite_path=composite_rel.as_posix(), composite_sha256=sha256(composite),
            raw_path=raw_rel.as_posix(), raw_sha256=sha256(raw),
        )
        self.summary.saved.append(self.output_dir / composite_rel)


# ---- 브라우저 ----

# 강조된 댓글을 찾는다. 답글은 같은 요소가 숨은 채로 하나 더 있어서 화면에 보이는 것만 본다.
# 본문은 이모지가 그림(img)으로 들어가므로 alt까지 이어 붙여 읽는다.
_FIND_JS = """
(commentId) => {
  const read = (node) => {
    if (!node) return '';
    let out = '';
    const walk = (n) => {
      if (n.nodeType === Node.TEXT_NODE) out += n.nodeValue;
      else if (n.nodeName === 'IMG') out += n.getAttribute('alt') || '';
      else if (n.nodeName === 'BR') out += '\\n';
      else n.childNodes.forEach(walk);
    };
    walk(node);
    return out;
  };
  const lcOf = (view) => {
    const href = view.querySelector('#published-time-text a')?.getAttribute('href') || '';
    return new URL(href, location.href).searchParams.get('lc');
  };
  document.querySelectorAll('[data-capture-target]')
    .forEach((el) => el.removeAttribute('data-capture-target'));
  const views = [...document.querySelectorAll('ytd-comment-view-model')];
  const visible = views.filter((v) => v.getBoundingClientRect().height > 0);
  const target = visible.find((v) => v.hasAttribute('linked') && lcOf(v) === commentId);
  if (!target) return {found: false, loaded: visible.length};
  target.setAttribute('data-capture-target', '');
  const more = target.querySelector('#more');
  if (more && !more.hidden && more.offsetParent) more.click();
  target.scrollIntoView({block: 'center'});
  const meta = document.querySelector('ytd-watch-metadata');
  return {
    found: true,
    text: read(target.querySelector('#content-text')),
    author: (target.querySelector('#author-text')?.textContent || '').trim(),
    title: (meta?.querySelector('h1')?.textContent || '').trim(),
    channel: (meta?.querySelector('ytd-channel-name #text')?.textContent || '').trim(),
  };
}
"""

_BOTTOM_JS = """
() => {
  window.scrollTo(0, 0);
  const target = document.querySelector('[data-capture-target]');
  return target ? target.getBoundingClientRect().bottom + window.scrollY : null;
}
"""

_BLOCK_MARKS = ("google.com/sorry", "consent.youtube.com", "consent.google.com", "accounts.google.com")


class YouTubeCamera:
    """Playwright(Chromium)로 유튜브 댓글 화면을 연다. 언어 한국어, 시간대 서울, 로그인하지 않은 상태."""

    def __init__(self, *, headless: bool = True, wait_seconds: float = 25.0) -> None:
        self.headless = headless
        self.wait_seconds = wait_seconds
        self._playwright = self._browser = self._page = None

    def __enter__(self) -> YouTubeCamera:
        from playwright.sync_api import Error, sync_playwright

        self._error = Error
        self._playwright = sync_playwright().start()
        try:
            self._browser = self._playwright.chromium.launch(
                headless=self.headless, args=["--mute-audio"]
            )
        except Error as exc:
            self._playwright.stop()
            if "Executable doesn't exist" in str(exc):
                raise CaptureError(
                    "캡처용 브라우저(Chromium)가 설치되어 있지 않습니다. "
                    "`uv run playwright install chromium`을 한 번 실행해 주세요."
                ) from None
            raise CaptureError(f"브라우저를 열 수 없습니다: {_first_line(exc)}") from None
        context = self._browser.new_context(
            locale="ko-KR", timezone_id="Asia/Seoul",
            viewport={"width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT},
        )
        self._page = context.new_page()
        return self

    def __exit__(self, *exc: object) -> None:
        try:
            if self._browser:
                self._browser.close()
        except Exception:
            pass  # 사용자가 창을 먼저 닫은 경우
        if self._playwright:
            self._playwright.stop()

    def _guard(self, action: Callable[[], Any]) -> Any:
        """브라우저 오류를 댓글 한 건의 실패로 바꾼다. 창이 닫혔으면 전체를 멈춘다."""
        try:
            return action()
        except self._error as exc:
            if self._page.is_closed() or not self._browser.is_connected():
                raise CaptureError("브라우저 창이 닫혀 캡처를 멈췄습니다.") from None
            raise CaptureFailed(f"브라우저 오류: {_first_line(exc)}") from None

    def _check_blocked(self) -> None:
        url = self._page.url
        if any(mark in url for mark in _BLOCK_MARKS):
            raise Blocked(f"유튜브 대신 다른 화면으로 넘어갔습니다: {urlparse(url).netloc}")

    def view(self, url: str, comment_id: str) -> Seen:
        return self._guard(lambda: self._view(url, comment_id))

    def _view(self, url: str, comment_id: str) -> Seen:
        page = self._page
        page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        self._check_blocked()
        if parse_qs(urlparse(page.url).query).get("lc") != [comment_id]:
            raise CaptureFailed("댓글 주소가 다른 주소로 바뀜")
        try:
            page.wait_for_selector("ytd-watch-metadata h1", timeout=self.wait_seconds * 1000)
        except self._error:
            if page.is_closed():
                raise  # 창이 닫힌 것은 댓글의 실패가 아니다. _guard가 전체를 멈춘다.
            self._check_blocked()
            raise CaptureFailed("영상 화면이 열리지 않음 (영상이 삭제되었거나 비공개일 수 있음)") from None

        # 댓글란은 화면에 들어와야 불러온다. 내려가서 강조 댓글이 뜰 때까지 기다린다.
        deadline = time.monotonic() + self.wait_seconds
        loaded_since = None
        while True:
            page.evaluate("document.querySelector('ytd-comments#comments')?.scrollIntoView()")
            page.wait_for_timeout(700)
            found = page.evaluate(_FIND_JS, comment_id)
            if found["found"]:
                break
            now = time.monotonic()
            if found["loaded"] and loaded_since is None:
                loaded_since = now
            # 다른 댓글은 다 떴는데 강조 댓글만 없으면 더 기다려도 나오지 않는다.
            if loaded_since is not None and now - loaded_since > 5:
                raise CaptureFailed("강조된 댓글이 보이지 않음 (삭제되었거나 숨겨졌을 수 있음)")
            if now > deadline:
                self._check_blocked()
                raise CaptureFailed("댓글란이 열리지 않음 (댓글이 막혔거나 불러오지 못함)")
        # "자세히 보기"를 펼친 뒤 자리를 잡을 시간을 주고 다시 읽는다.
        page.wait_for_timeout(1200)
        found = page.evaluate(_FIND_JS, comment_id)
        if not found["found"]:
            raise CaptureFailed("강조된 댓글이 화면에서 사라짐")
        return Seen(found["text"], found["author"], found["title"], found["channel"])

    def shoot(self) -> bytes:
        return self._guard(self._shoot)

    def _shoot(self) -> bytes:
        page = self._page
        bottom = page.evaluate(_BOTTOM_JS)
        if bottom is None:
            raise CaptureFailed("강조된 댓글이 화면에서 사라짐")
        page.wait_for_timeout(600)
        # 화면 맨 위(영상, 제목, 채널명)부터 강조 댓글 아래까지를 한 장으로 찍는다.
        return page.screenshot(
            type="png", full_page=True, animations="disabled",
            clip={"x": 0, "y": 0, "width": VIEWPORT_WIDTH, "height": int(bottom) + BOTTOM_MARGIN},
        )


def _first_line(exc: Exception) -> str:
    return (str(exc).strip().splitlines() or [type(exc).__name__])[0]
