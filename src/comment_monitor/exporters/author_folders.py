"""작성자별 증거 폴더.

랭킹에 오른 작성자마다 폴더를 하나 만들고 그 작성자의 캡처 합성본을 복사해 넣는다.
엑셀과 같은 보기용 출력이다. 원본은 captures 폴더와 DB에 그대로 있고, 순위가 실행마다 바뀌므로
이 폴더는 만들 때마다 통째로 지우고 다시 만든다. `folders` 명령이 부른다.
"""

from __future__ import annotations

import re
import shutil
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from .. import ranking
from ..db import Database

AUTHORS_DIR = "작성자별"
_NAME_LIMIT = 40
# 윈도우에서 파일 이름에 쓸 수 없는 문자와 제어 문자
_UNSAFE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


@dataclass(frozen=True)
class FolderResult:
    path: Path
    folders: int = 0
    files: int = 0
    missing: int = 0      # 기록은 있는데 파일이 없어 복사하지 못한 캡처


def folder_name(author: ranking.AuthorRank) -> str:
    """순위_작성자명_채널ID. 순위가 앞에 있어 폴더가 랭킹 순서로 정렬된다."""
    name = _UNSAFE.sub("_", author.name or "")[:_NAME_LIMIT].strip(" .")
    return "_".join(part for part in (f"{author.rank:03d}", name, author.channel_id) if part)


def export_author_folders(
    db: Database, output_dir: Path, target_names: Sequence[str]
) -> FolderResult:
    """output_dir 아래의 작성자별 폴더를 다시 만든다. 캡처가 없는 작성자는 폴더를 만들지 않는다."""
    authors = ranking.rank_authors(db.judged_rows(), target_names)
    listed = ranking.comments_by_rank(db.export_rows(only_negative=True), authors)

    root = output_dir / AUTHORS_DIR
    if root.exists():
        shutil.rmtree(root)
    folders: set[Path] = set()
    files = missing = 0
    for author, row in listed:
        if not row["capture_path"]:
            continue
        # 엑셀의 증거 폴더 링크는 캡처 기록을 보고 만든다. 파일이 사라졌어도 폴더는 있어야 열린다.
        folder = root / folder_name(author)
        if folder not in folders:
            folder.mkdir(parents=True)
            folders.add(folder)
        source = output_dir / row["capture_path"]
        if not source.is_file():
            missing += 1
            continue
        shutil.copy2(source, folder / source.name)
        files += 1
    return FolderResult(root, len(folders), files, missing)
