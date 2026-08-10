"""Wix Blog API client.

Every mechanic here is one that has already broken in production and is
documented in ``skills/integrations/wix-api-operations/SKILL.md`` and Step 7 of
``skills/timbr/magazine-eic/SKILL.md``:

* ``Authorization: <API_KEY>`` with **no** ``Bearer`` prefix, plus a
  ``wix-site-id`` header on every site-scoped request.
* ``POST /blog/v3/posts`` returns 404. Posts are created through the draft-posts
  workflow: create at ``/blog/v3/draft-posts``, then publish at
  ``/blog/v3/draft-posts/{id}/publish``.
* Creating a draft post without ``memberId`` fails 400 INVALID_ARGUMENT.
* On update-publish, ``fieldMask`` is what stops ``richContent`` being wiped.
  Omit it and the body is lost.
* After a publish the CDN serves the old body for a minute or two. Re-poll.
  Never re-publish on a stale read -- that is how duplicate writes start.
* ``seoData`` meta text lives in ``props.content``, never in ``children``. Text
  in ``children`` renders an empty meta tag.

Credentials come from the environment (``WIX_API_KEY``, ``WIX_SITE_ID``,
``WIX_MEMBER_ID``); the site id and API base also come from
``config/settings.yaml``. Nothing about a site is hardcoded here except the
never-touch guardrail.

Public API:
    WixClient(api_key, site_id, member_id)
    WixClient.from_config(cfg) / WixClient.from_env()
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Any, Final

import requests

from pipeline import ricos

try:  # package import -- the normal path
    from pipeline.errors import InfraFailure
    from pipeline.ricos import extract_text
except ImportError:  # pragma: no cover - direct-script execution
    from errors import InfraFailure  # type: ignore[no-redef]
    from ricos import extract_text  # type: ignore[no-redef]

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

#: Fallback only. ``config/settings.yaml`` -> ``wix.api_base`` wins.
DEFAULT_API_BASE: Final[str] = "https://www.wixapis.com"

ENV_API_KEY: Final[str] = "WIX_API_KEY"
ENV_SITE_ID: Final[str] = "WIX_SITE_ID"
ENV_MEMBER_ID: Final[str] = "WIX_MEMBER_ID"

_CONFIG_PATH: Final[Path] = Path(__file__).resolve().parent.parent / "config" / "settings.yaml"

#: Sites this engine must never write to, even if something hands us their id.
#: SPEC.md §7: timbr.fit is never touched and TIMBR-3 is the product site.
#: ``config/settings.yaml`` -> ``wix.forbidden_site_ids`` is merged onto this at
#: construction; the constants below are the floor, so deleting the config entry
#: cannot disarm the guardrail.
FORBIDDEN_SITE_IDS: Final[frozenset[str]] = frozenset(
    {
        "f916c8b1-134a-4691-9241-5a14bf849078",  # timbr.fit -- never touch
        "ab465896-e5c3-4f5d-bc9d-7f495a6d6be1",  # TIMBR-3 product/store site
    }
)

#: Statuses worth a retry. Everything else is a bug in the request, not weather.
_RETRY_STATUSES: Final[frozenset[int]] = frozenset({429, 500, 502, 503, 504})

_DEFAULT_RETRY_ATTEMPTS: Final[int] = 3
_DEFAULT_RETRY_BACKOFF_S: Final[int] = 5
_DEFAULT_TIMEOUT_S: Final[int] = 30


class WixError(InfraFailure):
    """A Wix API call failed.

    Subclasses :class:`~pipeline.errors.InfraFailure` on purpose. SPEC.md §4
    classes "Wix rejecting a publish" as an infrastructure failure, so the
    orchestrator's existing backoff-and-requeue path catches this without any
    special handling -- retrying a *draft* against an HTTP 500 accomplishes
    nothing.

    Attributes:
        status_code: HTTP status, when the failure was a response rather than a
            transport error.
        body: First 500 chars of the response body, for diagnosis.
    """

    def __init__(self, message: str, status_code: int | None = None, body: str = "") -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = body


def load_wix_settings(config_path: Path | None = None) -> dict[str, Any]:
    """Read the ``wix:`` block out of ``config/settings.yaml``.

    Returns ``{}`` when the file is missing or unreadable, so a caller that has
    the values in the environment still works.
    """
    path = config_path or _CONFIG_PATH
    try:
        import yaml  # imported lazily: a caller passing cfg does not need it

        with path.open(encoding="utf-8") as handle:
            settings = yaml.safe_load(handle) or {}
    except (OSError, ImportError) as exc:
        logger.warning("Could not read wix settings from %s (%s)", path, exc)
        return {}
    except Exception:
        logger.warning("Could not parse %s", path, exc_info=True)
        return {}
    wix = settings.get("wix")
    return wix if isinstance(wix, dict) else {}


class WixClient:
    """Thin, explicit client over the Wix Blog v3 draft-posts workflow."""

    def __init__(
        self,
        api_key: str,
        site_id: str,
        member_id: str,
        *,
        api_base: str = DEFAULT_API_BASE,
        timeout: int = _DEFAULT_TIMEOUT_S,
        max_retries: int = _DEFAULT_RETRY_ATTEMPTS,
        retry_backoff_s: int = _DEFAULT_RETRY_BACKOFF_S,
        forbidden_site_ids: frozenset[str] | set[str] | None = None,
        session: requests.Session | None = None,
    ) -> None:
        if not api_key:
            raise WixError(f"api_key is required (set {ENV_API_KEY})")
        if not site_id:
            raise WixError(f"site_id is required (set {ENV_SITE_ID} or config wix.site_id)")
        if not member_id:
            # Wix answers 400 INVALID_ARGUMENT on a draft with no memberId.
            # Failing here puts the error where the cause is obvious.
            raise WixError(f"member_id is required (set {ENV_MEMBER_ID})")

        self._forbidden = FORBIDDEN_SITE_IDS | frozenset(forbidden_site_ids or ())
        if site_id in self._forbidden:
            raise WixError(
                f"refusing to build a client for forbidden site {site_id} -- "
                "this engine publishes to seattlefitnessmag.com only"
            )

        self.api_key = api_key
        self.site_id = site_id
        self.member_id = member_id
        self.api_base = (api_base or DEFAULT_API_BASE).rstrip("/")
        self.timeout = timeout
        self.max_retries = max(1, max_retries)
        self.retry_backoff_s = retry_backoff_s
        self._session = session or requests.Session()

    # ----------------------------------------------------------------- #
    # Construction
    # ----------------------------------------------------------------- #
    @classmethod
    def from_config(cls, cfg: dict[str, Any], **kwargs: Any) -> "WixClient":
        """Build a client from the orchestrator's ``cfg`` dict.

        Site id resolution: ``WIX_SITE_ID`` -> ``cfg["settings"]["wix"]["site_id"]``.
        The environment wins so a run can be pointed at a test site without
        editing a locked config file.
        """
        wix_cfg = ((cfg or {}).get("settings") or {}).get("wix") or {}
        return cls._build(wix_cfg, **kwargs)

    @classmethod
    def from_env(
        cls, wix_cfg: dict[str, Any] | Path | str | None = None, **kwargs: Any
    ) -> "WixClient":
        """Build a client from the environment, backfilled from the wix config.

        ``wix_cfg`` may be the ``wix:`` block itself (what
        ``cfg["settings"]["wix"]`` holds, which is how ``stages/publish.py``
        calls this), a path to a settings YAML file, or None to read
        ``config/settings.yaml``.
        """
        if isinstance(wix_cfg, dict):
            resolved = wix_cfg
        else:
            resolved = load_wix_settings(Path(wix_cfg) if wix_cfg else None)
        return cls._build(resolved, **kwargs)

    @classmethod
    def _build(cls, wix_cfg: dict[str, Any], **kwargs: Any) -> "WixClient":
        missing = [
            name for name in (ENV_API_KEY, ENV_MEMBER_ID) if not os.environ.get(name, "").strip()
        ]
        if missing:
            raise WixError(f"missing environment variable(s): {', '.join(missing)}")

        site_id = os.environ.get(ENV_SITE_ID, "").strip() or str(wix_cfg.get("site_id") or "")
        kwargs.setdefault("api_base", str(wix_cfg.get("api_base") or DEFAULT_API_BASE))
        kwargs.setdefault("forbidden_site_ids", frozenset(wix_cfg.get("forbidden_site_ids") or ()))
        return cls(
            api_key=os.environ[ENV_API_KEY].strip(),
            site_id=site_id,
            member_id=os.environ[ENV_MEMBER_ID].strip(),
            **kwargs,
        )

    # ----------------------------------------------------------------- #
    # HTTP
    # ----------------------------------------------------------------- #
    def _headers(self) -> dict[str, str]:
        # No "Bearer" prefix -- Wix wants the raw IST.eyJ... token. The
        # wix-site-id header is required on every site-scoped request.
        return {
            "Authorization": self.api_key,
            "wix-site-id": self.site_id,
            "Content-Type": "application/json",
        }

    def _assert_writable(self) -> None:
        """Re-check the never-touch list at the moment of a write."""
        if self.site_id in self._forbidden:
            raise WixError(f"refusing to write to forbidden site {self.site_id}")

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """One Wix call. Raises :class:`WixError` on any non-2xx response.

        Retries 429 and 5xx with exponential backoff, per SPEC.md §4. A 4xx that
        is not 429 is a malformed request and is raised immediately.
        """
        url = f"{self.api_base}{path}"
        last_error: WixError | None = None

        for attempt in range(1, self.max_retries + 1):
            try:
                response = self._session.request(
                    method,
                    url,
                    headers=self._headers(),
                    json=payload,
                    params=params,
                    timeout=self.timeout,
                )
            except requests.RequestException as exc:
                last_error = WixError(f"{method} {path} transport error: {exc}")
                logger.warning("%s %s failed (attempt %d): %s", method, path, attempt, exc)
            else:
                if response.ok:
                    if not response.content:
                        return {}
                    try:
                        return response.json()
                    except ValueError as exc:
                        raise WixError(
                            f"{method} {path} returned a non-JSON body: {exc}",
                            response.status_code,
                            response.text[:500],
                        ) from exc

                body = response.text[:500]
                last_error = WixError(
                    f"{method} {path} -> HTTP {response.status_code}: {body}",
                    response.status_code,
                    body,
                )
                if response.status_code not in _RETRY_STATUSES:
                    raise last_error
                logger.warning(
                    "%s %s -> HTTP %d (attempt %d/%d)",
                    method, path, response.status_code, attempt, self.max_retries,
                )

            if attempt < self.max_retries:
                time.sleep(self.retry_backoff_s * (2 ** (attempt - 1)))

        assert last_error is not None
        raise last_error

    # ----------------------------------------------------------------- #
    # Validation
    # ----------------------------------------------------------------- #
    @staticmethod
    def _check_seo_data(seo_data: dict[str, Any] | None) -> None:
        """Warn on the meta-tag shape that renders an empty tag.

        ``seoData`` meta text belongs in ``props.content``. Text in ``children``
        renders ``<meta name="description"/>`` empty and emits duplicate empty
        og: tags. ``title`` tags legitimately use ``children``, so only ``meta``
        is checked.
        """
        for tag in (seo_data or {}).get("tags", []) or []:
            if not isinstance(tag, dict) or tag.get("type") != "meta":
                continue
            if tag.get("children") and not (tag.get("props") or {}).get("content"):
                logger.warning(
                    "seoData meta tag carries its text in 'children' (%r) -- it must live "
                    "in props.content or the meta tag renders empty",
                    tag.get("children"),
                )

    # ----------------------------------------------------------------- #
    # Blog operations
    # ----------------------------------------------------------------- #
    def create_draft(
        self,
        title: str,
        rich_content: dict[str, Any],
        seo_data: dict[str, Any],
        category_ids: list[str],
        tag_ids: list[str],
        slug: str,
        excerpt: str,
    ) -> str:
        """Create a draft post. Returns the draft post id.

        Uses ``/blog/v3/draft-posts`` because ``POST /blog/v3/posts`` returns
        404. ``memberId`` is mandatory -- without it Wix answers 400
        INVALID_ARGUMENT.
        """
        self._assert_writable()
        self._check_seo_data(seo_data)

        draft_post: dict[str, Any] = {
            "title": title,
            "richContent": rich_content,
            "memberId": self.member_id,  # required -- else 400 INVALID_ARGUMENT
        }
        if seo_data:
            draft_post["seoData"] = seo_data
        if category_ids:
            draft_post["categoryIds"] = list(category_ids)
        if tag_ids:
            draft_post["tagIds"] = list(tag_ids)
        if slug:
            # Controlled slug with a date suffix; Wix's auto-slug collides on
            # recurring events (SPEC.md §2).
            draft_post["slug"] = slug
        if excerpt:
            draft_post["excerpt"] = excerpt

        response = self._request("POST", "/blog/v3/draft-posts", {"draftPost": draft_post})
        draft_id = (response.get("draftPost") or {}).get("id")
        if not draft_id:
            raise WixError(f"draft created but no id came back: {str(response)[:500]}")

        logger.info("Created draft %s (%r)", draft_id, title)
        return str(draft_id)

    def publish_draft(self, draft_id: str) -> dict[str, Any]:
        """Publish a draft post.

        ``/blog/v3/posts/{id}/publish`` returns 404; the draft-posts endpoint is
        the working one.
        """
        self._assert_writable()
        response = self._request("POST", f"/blog/v3/draft-posts/{draft_id}/publish", {})
        post_id = response.get("postId") or (response.get("draftPost") or {}).get("id") or draft_id
        logger.info("Published draft %s -> post %s", draft_id, post_id)
        return response

    def update_published(
        self,
        post_id: str,
        rich_content: dict[str, Any] | None = None,
        seo_data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Update an already-published post in place and re-publish it.

        ``fieldMask`` is what stops ``richContent`` being wiped, so only the
        fields actually supplied are listed in it -- and at least one must be.
        An empty mask is a request to lose the body, so it raises instead.
        """
        self._assert_writable()
        self._check_seo_data(seo_data)

        draft_post: dict[str, Any] = {"id": post_id}
        paths: list[str] = []
        if rich_content is not None:
            draft_post["richContent"] = rich_content
            paths.append("richContent")
        if seo_data is not None:
            draft_post["seoData"] = seo_data
            paths.append("seoData")

        if not paths:
            raise WixError(
                "update_published needs rich_content and/or seo_data -- publishing with "
                "an empty fieldMask wipes the body"
            )

        payload = {
            "action": "UPDATE_PUBLISH",
            "draftPosts": [{"fieldMask": {"paths": paths}, "draftPost": draft_post}],
        }
        response = self._request("PATCH", "/blog/v3/draft-posts/update", payload)
        logger.info("Update-published %s (fieldMask=%s)", post_id, ",".join(paths))
        return response

    def get_post(self, post_id: str) -> dict[str, Any]:
        """Fetch a published post, body included.

        Live copy is read from the API, never from the CDN or a summarizer -- a
        summarizer paraphrases the page and you end up reviewing prose the site
        does not contain. ``fieldsets`` is what pulls ``richContent`` down; some
        blog endpoints reject that param with a 400, so a rejection falls back
        to a bare GET rather than failing the read.
        """
        path = f"/blog/v3/posts/{post_id}"
        try:
            response = self._request("GET", path, params={"fieldsets": ["RICH_CONTENT", "URL"]})
        except WixError as exc:
            if exc.status_code != 400:
                raise
            logger.warning("fieldsets rejected on %s (HTTP 400); retrying bare", path)
            response = self._request("GET", path)
        return response.get("post") or response

    def prepend_banner(self, post_id: str, banner: str) -> dict[str, Any]:
        """Put a status banner at the top of an already-published post.

        Used by the post-publish sweep for events that ended, were cancelled,
        or sold out. Chosen over unpublishing so the article keeps its URL and
        whatever search authority it has built up.

        Idempotent by design: a post already carrying this banner is left
        alone. The sweep runs twice a day and would otherwise stack a fresh
        banner on every pass.
        """
        post = self.get_post(post_id)
        rich_content = dict(post.get("richContent") or {})
        nodes = list(rich_content.get("nodes") or [])

        if nodes and banner in ricos.extract_text({"nodes": nodes[:2]}):
            logger.info("%s already carries this banner; leaving it alone", post_id)
            return {"post": post, "skipped": True}

        nodes.insert(0, ricos.banner_node(banner))
        rich_content["nodes"] = nodes
        logger.info("Bannering %s: %s", post_id, banner)
        return self.update_published(post_id, rich_content=rich_content)

    def verify_published(
        self,
        post_id: str,
        expect_substring: str,
        attempts: int = 5,
        delay_s: int = 20,
    ) -> bool:
        """Poll the live post until its body contains ``expect_substring``.

        The CDN serves the old body for a minute or two after a successful
        publish, so a first read that misses the string is a stale read, not a
        failed publish. This re-polls and **never re-publishes** -- republishing
        on a stale read is how duplicate writes start.

        Comparison is whitespace-normalised, because the Ricos spacing rules
        split a paragraph across several TEXT nodes with ``\\n`` between them.

        Returns True as soon as the string is found, False if every attempt is
        stale. A False means "go look at the post", not "publish it again".
        """
        needle = " ".join((expect_substring or "").split())
        if not needle:
            raise WixError("verify_published needs a non-empty expect_substring")

        attempts = max(1, attempts)
        for attempt in range(1, attempts + 1):
            try:
                post = self.get_post(post_id)
            except WixError as exc:
                logger.warning(
                    "verify %s attempt %d/%d: read failed: %s", post_id, attempt, attempts, exc
                )
            else:
                body = extract_text(post.get("richContent")) or str(post.get("contentText") or "")
                if needle in " ".join(body.split()):
                    logger.info("Verified %s on attempt %d/%d", post_id, attempt, attempts)
                    return True
                logger.info(
                    "verify %s attempt %d/%d: stale read (%d chars, substring absent)",
                    post_id, attempt, attempts, len(body),
                )

            if attempt < attempts:
                time.sleep(delay_s)

        logger.error(
            "verify %s: substring never appeared after %d attempt(s). Inspect the post; "
            "do NOT re-publish on a stale read.",
            post_id, attempts,
        )
        return False
