from comment_monitor.db import CommentData
from comment_monitor.exporters.author_folders import export_author_folders, folder_name
from comment_monitor.ranking import AuthorRank

from .conftest import VIDEO_ID
from .helpers import mark_negative

NAMES = ("홍길동",)


def add(db, comment_id, author, *, severity=3, name="작성자", target="홍길동"):
    db.upsert_comment(
        CommentData(
            platform="youtube", comment_id=comment_id, video_id=VIDEO_ID, text="욕",
            author_name=name, author_channel_id=author, published_at="2026-11-01T00:00:00Z",
        ),
        now="2026-11-01T12:00:00Z",
    )
    mark_negative(db, comment_id, severity=severity, target=target)


def capture(db, out, comment_id, content=b"png"):
    """캡처가 끝난 것처럼 합성본 파일과 기록을 만든다."""
    no = db.scalar("SELECT evidence_no FROM comments WHERE comment_id = ?", (comment_id,))
    rel = f"captures/{no:06d}_{comment_id}.png"
    (out / "captures").mkdir(parents=True, exist_ok=True)
    (out / rel).write_bytes(content)
    db.save_capture("youtube", comment_id, captured_at="2026-11-03T00:00:00Z",
                    composite_path=rel, composite_sha256="hash")
    return rel


def tree(folder):
    return sorted(p.relative_to(folder).as_posix() for p in folder.rglob("*") if p.is_file())


def test_captures_are_copied_into_one_folder_per_ranked_author(db, tmp_path):
    out = tmp_path / "output"
    add(db, "a1", "UCa", severity=2)
    add(db, "b1", "UCb", severity=5)
    add(db, "b2", "UCb", severity=4)
    add(db, "c1", "UCc", severity=1)                       # 캡처가 없는 작성자
    for comment_id in ("a1", "b1", "b2"):
        capture(db, out, comment_id, content=comment_id.encode())

    result = export_author_folders(db, out, NAMES)

    assert (result.folders, result.files, result.missing) == (2, 3, 0)
    assert result.path == out / "작성자별"
    assert tree(out / "작성자별") == [
        "001_작성자_UCb/000002_b1.png", "001_작성자_UCb/000003_b2.png",
        "002_작성자_UCa/000001_a1.png",
    ]
    assert (out / "작성자별/001_작성자_UCb/000002_b1.png").read_bytes() == b"b1"
    # 원본은 그대로 남는다.
    assert tree(out / "captures") == ["000001_a1.png", "000002_b1.png", "000003_b2.png"]


def test_folders_are_rebuilt_when_the_ranking_changes(db, tmp_path):
    out = tmp_path / "output"
    add(db, "a1", "UCa", severity=2)
    add(db, "b1", "UCb", severity=5)
    capture(db, out, "a1")
    capture(db, out, "b1")
    export_author_folders(db, out, NAMES)

    add(db, "a2", "UCa", severity=5)                       # UCa가 1위로 올라선다
    db.save_reviews([("youtube", "b1", "제외", None)])     # UCb는 랭킹에서 빠진다
    result = export_author_folders(db, out, NAMES)

    assert (result.folders, result.files) == (1, 1)
    assert tree(out / "작성자별") == ["001_작성자_UCa/000001_a1.png"]


def test_comments_the_ranking_does_not_count_are_left_out(db, tmp_path):
    out = tmp_path / "output"
    add(db, "a1", "UCa")
    add(db, "a2", "UCa", severity=5, target="타인")
    capture(db, out, "a1")
    capture(db, out, "a2")
    export_author_folders(db, out, NAMES)
    assert tree(out / "작성자별") == ["001_작성자_UCa/000001_a1.png"]


def test_a_capture_file_that_is_gone_is_reported_not_fatal(db, tmp_path):
    out = tmp_path / "output"
    add(db, "a1", "UCa")
    add(db, "a2", "UCa")
    capture(db, out, "a1")
    (out / capture(db, out, "a2")).unlink()
    result = export_author_folders(db, out, NAMES)
    assert (result.folders, result.files, result.missing) == (1, 1, 1)


def test_an_author_whose_files_are_all_gone_still_gets_the_linked_folder(db, tmp_path):
    out = tmp_path / "output"
    add(db, "a1", "UCa")
    (out / capture(db, out, "a1")).unlink()
    result = export_author_folders(db, out, NAMES)
    # 엑셀의 증거 폴더 링크는 캡처 기록을 보고 만들므로, 파일이 없어도 폴더는 열려야 한다.
    assert (result.folders, result.files, result.missing) == (1, 0, 1)
    assert (out / "작성자별" / "001_작성자_UCa").is_dir()


def test_nothing_captured_leaves_no_folder_behind(db, tmp_path):
    out = tmp_path / "output"
    add(db, "a1", "UCa")
    result = export_author_folders(db, out, NAMES)
    assert (result.folders, result.files) == (0, 0) and not (out / "작성자별").exists()


def test_folder_names_are_safe_on_every_filesystem():
    def name(author_name, channel_id="UCx", rank=7):
        return folder_name(AuthorRank("youtube", channel_id, name=author_name, rank=rank))

    assert name("@user-1") == "007_@user-1_UCx"
    assert name('a/b\\c:d*e?f"g<h>i|j') == "007_a_b_c_d_e_f_g_h_i_j_UCx"
    assert name("  끝에 점. ") == "007_끝에 점_UCx"
    assert name(None) == "007_UCx"
    assert name("이름만", channel_id=None) == "007_이름만"
    assert name("가" * 100) == "007_" + "가" * 40 + "_UCx"
    assert name("천등", rank=1234) == "1234_천등_UCx"
