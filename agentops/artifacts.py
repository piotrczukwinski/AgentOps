from __future__ import annotations

import hashlib
from pathlib import Path

# Path-traversal sentinel segments that ``safe_name`` must collapse.
# ``..`` would let a hostile ``roadmap_id``/``task_id`` escape the
# ``.agentops/runs/`` sandbox (e.g. ``attempt_dir("..", "..", 0)``
# would write to ``root/0`` instead of ``root/runs/..//0``). ``.``
# means "current directory" and silently collapses a path component.
# We replace both with a single dash so the resulting segment is never
# empty and never traverses.
_DOTDOT_SENTINELS = {"..", "."}


def safe_name(value: str) -> str:
    """Slugify ``value`` into a path-safe, non-traversing segment.

    The output:
    * contains only alphanumerics plus ``.`` ``_`` ``-``;
    * has no leading/trailing dashes or dots;
    * NEVER equals ``..`` or ``.`` (path-traversal sentinels are
      collapsed to ``-`` so a hostile ``roadmap_id`` cannot escape the
      ``.agentops/runs/`` sandbox);
    * is never empty for non-empty input (an all-symbol input collapses
      to ``-`` rather than the empty string).
    """
    slug = "".join(ch if ch.isalnum() or ch in "._-" else "-" for ch in value).strip("-").strip(".")
    # Collapse any run of 2+ dots so the segment cannot be confused
    # with ``..`` by a path joiner. Iterating until stable handles
    # ``...`` -> ``-.`` -> ``-`` and ``a..b`` -> ``a-b``.
    while ".." in slug:
        slug = slug.replace("..", "-")
    slug = slug.strip("-").strip(".")
    # After collapse the segment may have become ``.`` / ``..`` or
    # empty; replace those with a single harmless dash.
    if slug in _DOTDOT_SENTINELS or not slug:
        return "-"
    return slug


class ArtifactStore:
    def __init__(self, root: Path):
        self.root = root.expanduser().resolve()

    def attempt_dir(self, roadmap_id: str, task_id: str, attempt_no: int) -> Path:
        path = self.root / "runs" / safe_name(roadmap_id) / safe_name(task_id) / str(attempt_no)
        path.mkdir(parents=True, exist_ok=True)
        # Defence in depth: refuse to hand back a path that escaped the
        # sandbox. ``safe_name`` already prevents traversal, but if a
        # future caller passes a pre-built segment we still catch it.
        resolved = path.resolve()
        if not str(resolved).startswith(str(self.root)):
            raise ValueError(
                f"attempt_dir escaped the artifact sandbox: {path} resolves to {resolved}, "
                f"which is outside {self.root}. Refusing to write."
            )
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
