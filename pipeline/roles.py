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
import subprocess
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"

# Roles that must reach the network to do their job. Everything else runs with
# no tools at all, which keeps them from wandering off their contract.
ROLE_TOOLS: dict[str, list[str]] = {
    "cross_check_validator": ["WebFetch"],
    "dedupe_agent": [],
    "relevance_scorer": [],
    "writer": [],
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


def _invoke(prompt: str, tools: list[str], model: str | None,
            timeout_s: int) -> str:
    cmd = ["claude", "-p", prompt, "--output-format", "json"]
    if model:
        cmd += ["--model", model]
    if tools:
        cmd += ["--allowed-tools", ",".join(tools)]
    else:
        cmd += ["--allowed-tools", ""]

    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout_s,
        env=_clean_env(),
        encoding="utf-8",
        errors="replace",
    )
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
