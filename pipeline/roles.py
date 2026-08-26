"""Runs the ten LLM roles through headless Claude Code.

Roles are invoked as ``claude -p``, not as direct Anthropic API calls. That is
a billing decision: subscription auth bills against Claude Max, an API key
bills pay-as-you-go.

The guard in :func:`_clean_env` matters more than it looks. Claude Code
prioritises ``ANTHROPIC_API_KEY`` over subscription auth, so if that variable
is ever present in the environment every role call silently switches to API
rates. This strips it.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import time
from functools import lru_cache
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"

# Roles that must reach the network to do their job. Everything else runs with
# no tools at all, which keeps them from wandering off their contract.
ROLE_TOOLS: dict[str, list[str]] = {
    "cross_check_validator": ["WebFetch"],
    # The only role that searches. The validator is handed the URLs it must
    # check; the researcher has to find the subject's own pages first, then
    # read them. Everything an article knows beyond its original listing
    # arrives through these two tools.
    "fact_researcher": ["WebFetch", "WebSearch"],
    "dedupe_agent": [],
    "relevance_scorer": [],
    "writer": [],
    "voice_editor": [],
    "fact_checker": [],
    "judge_seo": [],
    "slot_scheduler": [],
}

DEFAULT_TIMEOUT_S = 300


class RoleError(RuntimeError):
    """A role failed to produce usable output after its retries."""


def _clean_env() -> dict[str, str]:
    """Environment for the subprocess, with API-key auth removed.

    Leaving ``ANTHROPIC_API_KEY`` in place would override the OAuth token and
    move every call onto pay-as-you-go billing without any visible signal.
    """
    env = dict(os.environ)
    removed = [k for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN") if k in env]
    for key in removed:
        env.pop(key)
    if removed:
        log.warning("stripped %s from role environment to protect Max billing",
                    ", ".join(removed))
    return env


def load_prompt(role: str) -> str:
    path = PROMPTS_DIR / f"{role}.md"
    if not path.exists():
        raise RoleError(f"no prompt file for role '{role}' at {path}")
    return path.read_text(encoding="utf-8")


def _extract_json(text: str) -> Any:
    """Pull a JSON object out of model output.

    Handles a bare object, a ```json fenced block, and an object with prose
    wrapped around it. Raises if none of those yield valid JSON.
    """
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    fenced = re.search(r"```(?:json)?\s*(\{.*?\}|\[.*?\])\s*```", text, re.DOTALL)
    if fenced:
        try:
            return json.loads(fenced.group(1))
        except json.JSONDecodeError:
            pass

    # Last resort: widest brace span in the output.
    for opener, closer in (("{", "}"), ("[", "]")):
        start, end = text.find(opener), text.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                continue

    raise RoleError(f"no parsable JSON in role output: {text[:400]}")


@lru_cache(maxsize=1)
def _claude_executable() -> str:
    """Absolute path to the Claude Code CLI.

    On Windows the npm install is a ``claude.CMD`` shim, and ``subprocess``
    resolves a bare command name against ``.exe`` only -- so ``["claude", ...]``
    raises ``WinError 2`` even with the CLI plainly on PATH. ``shutil.which``
    honours PATHEXT and returns the real target on every platform, which
    avoids reaching for ``shell=True`` with a prompt in the argument list.
    """
    found = shutil.which("claude")
    if not found:
        raise RoleError(
            "claude CLI not found on PATH. Install Claude Code, or expose it "
            "to this process, before running any role."
        )
    return found


def _kill_tree(proc: "subprocess.Popen[str]") -> None:
    """Kill *proc* and everything it spawned.

    ``subprocess`` only ever kills the process it started. On Windows the npm
    entry point is ``claude.CMD``, so that process is a shell and the node
    process doing the actual work is its child -- which survives the kill still
    holding the stdout pipe, leaving the parent blocked on a read that never
    ends. A 600-second writer timeout was observed surfacing 35 minutes late
    for exactly this reason. ``taskkill /T`` takes the tree down together.
    """
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            capture_output=True, check=False,
        )
    else:
        proc.kill()


def _invoke(prompt: str, tools: list[str], model: str | None,
            timeout_s: int) -> str:
    # The prompt goes in on stdin, never in argv. Passing it as an argument
    # capped every role at the cmd.exe 8191-character command line, and five
    # of the seven prompt files exceed that before any event data is added
    # (writer.md alone is 19k). stdin also removes every quoting and escaping
    # hazard that comes with putting model-authored text on a command line.
    cmd = [_claude_executable(), "-p", "--output-format", "json"]
    if model:
        cmd += ["--model", model]
    if tools:
        cmd += ["--allowed-tools", ",".join(tools)]
    else:
        cmd += ["--allowed-tools", ""]

    # Popen rather than run(), so a timeout can take the whole process tree
    # down instead of orphaning the worker and blocking on its pipes.
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=_clean_env(),
        encoding="utf-8",
        errors="replace",
    )
    try:
        stdout, stderr = proc.communicate(prompt, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        try:
            proc.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            log.warning("role process %s did not exit after kill", proc.pid)
        raise

    proc = subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)
    if proc.returncode != 0:
        raise RoleError(
            f"claude exited {proc.returncode}: {(proc.stderr or '').strip()[:400]}"
        )

    # --output-format json wraps the answer in an envelope; older versions
    # return the answer directly.
    try:
        envelope = json.loads(proc.stdout)
        if isinstance(envelope, dict) and "result" in envelope:
            return str(envelope["result"])
    except json.JSONDecodeError:
        pass
    return proc.stdout


def run_role(role: str, payload: dict[str, Any], *, model: str | None = None,
             attempts: int = 3, backoff_s: int = 5,
             timeout_s: int = DEFAULT_TIMEOUT_S,
             extra_context: str = "",
             system_override: str | None = None) -> dict[str, Any]:
    """Run one role and return its parsed JSON output.

    Args:
        role: filename stem in ``prompts/``, e.g. ``"relevance_scorer"``.
        payload: the role's input, serialised into the prompt as JSON.
        model: optional model override.
        attempts: retries on transport failure or unparsable output.
        extra_context: appended verbatim, for things like inherited voice rules.
        system_override: use this text instead of reading a prompt file. The
            six inherited judge dimensions share one prompt built at run time
            from the rubric, so they have no file of their own.

    Raises:
        RoleError: every attempt failed.
    """
    system = system_override if system_override is not None else load_prompt(role)
    tools = ROLE_TOOLS.get(role, [])

    prompt = (
        f"{system}\n\n"
        f"{extra_context}\n\n"
        "---\n\nINPUT:\n\n"
        f"```json\n{json.dumps(payload, indent=2, default=str)}\n```\n\n"
        "Respond with JSON only, matching the OUTPUT schema above. "
        "No preamble, no commentary, no code fence."
    )

    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            raw = _invoke(prompt, tools, model, timeout_s)
            result = _extract_json(raw)
            if not isinstance(result, dict):
                raise RoleError(f"{role} returned {type(result).__name__}, expected object")
            log.info("role %s ok (attempt %d)", role, attempt)
            return result
        except (RoleError, subprocess.TimeoutExpired, OSError) as exc:
            last = exc
            log.warning("role %s attempt %d/%d failed: %s", role, attempt, attempts, exc)
            if attempt < attempts:
                time.sleep(backoff_s * (2 ** (attempt - 1)))

    raise RoleError(f"role {role} failed after {attempts} attempts: {last}")
