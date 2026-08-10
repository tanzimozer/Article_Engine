"""Control-flow exceptions shared by the orchestrator and the stages.

These live in their own module so a stage can raise them without importing
``main``, which imports every stage.
"""

from __future__ import annotations


class Held(Exception):
    """A stage decided this article needs a human.

    Not an error. The article stops where it is, keeps everything it has, and
    waits in the Drive hold queue until someone clears it.
    """

    def __init__(self, stage: str, reason: str):
        super().__init__(reason)
        self.stage = stage
        self.reason = reason


class InfraFailure(Exception):
    """Transport, API, or scrape failure.

    Deliberately distinct from a content failure. Content problems get the
    revise loop; retrying a draft against an HTTP 500 accomplishes nothing, so
    these get backoff and a requeue for the next run instead.
    """
