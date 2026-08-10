"""Reads the inherited voice and rubric specs from Skill-Cabinet.

Those files are referenced rather than forked so a voice fix propagates to
every TIMBR surface at once. Skill-Cabinet is public, so no token is needed.

The clone is shallow and cached for the life of the run. Actions runners are
ephemeral, so this costs one clone per job.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
from functools import lru_cache
from pathlib import Path

log = logging.getLogger(__name__)

_CACHE_DIR = Path(tempfile.gettempdir()) / "article_engine_skills"


class SkillsUnavailable(RuntimeError):
    """Skill-Cabinet could not be fetched or a referenced file is missing."""


def ensure_clone(repo_url: str) -> Path:
    """Shallow-clone Skill-Cabinet once, reusing it for later calls."""
    if (_CACHE_DIR / ".git").exists():
        return _CACHE_DIR

    if _CACHE_DIR.exists():
        shutil.rmtree(_CACHE_DIR, ignore_errors=True)

    log.info("cloning %s", repo_url)
    proc = subprocess.run(
        ["git", "clone", "--depth", "1", repo_url, str(_CACHE_DIR)],
        capture_output=True, text=True, timeout=180,
    )
    if proc.returncode != 0:
        raise SkillsUnavailable(
            f"clone failed: {(proc.stderr or '').strip()[:300]}"
        )
    return _CACHE_DIR


@lru_cache(maxsize=32)
def read(repo_url: str, relative_path: str) -> str:
    """Return the contents of one file from Skill-Cabinet.

    Raises:
        SkillsUnavailable: the repo is unreachable or the path does not exist.
            This is fatal by design — writing without the voice handbook would
            produce off-brand copy that the judges would reject anyway.
    """
    root = ensure_clone(repo_url)
    path = root / relative_path
    if not path.exists():
        raise SkillsUnavailable(f"{relative_path} not found in Skill-Cabinet")
    return path.read_text(encoding="utf-8")


def voice_handbook(cfg: dict) -> str:
    skills = cfg["settings"]["skills"]
    return read(skills["repo"], skills["house_voice"])


def eic_harness(cfg: dict) -> str:
    skills = cfg["settings"]["skills"]
    return read(skills["repo"], skills["eic_harness"])


def rubric(cfg: dict) -> str:
    skills = cfg["settings"]["skills"]
    return read(skills["repo"], skills["rubric"])
