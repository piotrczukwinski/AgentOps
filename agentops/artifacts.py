from __future__ import annotations

import hashlib
from pathlib import Path


def safe_name(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "._-" else "-" for ch in value).strip("-")


class ArtifactStore:
    def __init__(self, root: Path):
        self.root = root.expanduser().resolve()

    def attempt_dir(self, roadmap_id: str, task_id: str, attempt_no: int) -> Path:
        path = self.root / "runs" / safe_name(roadmap_id) / safe_name(task_id) / str(attempt_no)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def write_text(self, directory: Path, name: str, text: str) -> Path:
        path = directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def sha256(self, path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
