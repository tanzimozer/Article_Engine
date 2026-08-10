"""Pipeline stages.

Stages 1 and 2 (``gather``, ``clean``) are batch operations that run once per
invocation. Stages 3-9 run per article and share one signature::

    run(conn, cfg, article_id) -> {"ok": bool, "reason": str, "retry_from": str}

They signal the two non-content outcomes by raising:

- ``errors.Held`` — needs a human, stop this article, keep everything
- ``errors.InfraFailure`` — transport problem, requeue for the next run
"""
