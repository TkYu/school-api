"""Background worker: takes jobs from the `jobs` table (FOR UPDATE SKIP LOCKED, so several workers never take the
same job) and runs the handler for their kind. Kinds without a handler yet stay queued for a later version.
Run: `python -m app.worker`."""
from __future__ import annotations

import logging
import signal
import time
import traceback
from typing import Any, Callable

from .config import conninfo, settings
from .db import close_pool, open_pool, tx

log = logging.getLogger("worker")
# kind -> handler(payload); filled in by the processing modules (parse, chunk, embed)
HANDLERS: dict[str, Callable[[dict[str, Any]], None]] = {}
MAX_ATTEMPTS = 5


def take_job() -> dict[str, Any] | None:
    if not HANDLERS:
        return None
    with tx() as conn:
        job = conn.execute(
            """SELECT id, kind, payload, attempts FROM jobs
               WHERE status = 'queued' AND run_after <= now() AND kind = ANY(%s)
               ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED""", (list(HANDLERS),)).fetchone()
        if job:
            conn.execute("UPDATE jobs SET status = 'running', locked_at = now(), attempts = attempts + 1 WHERE id = %s", (job["id"],))
        return job


def run_job(job: dict[str, Any]) -> None:
    try:
        HANDLERS[job["kind"]](job["payload"])
        with tx() as conn:
            conn.execute("UPDATE jobs SET status = 'done', error = NULL WHERE id = %s", (job["id"],))
    except Exception:
        err = traceback.format_exc(limit=5)
        failed = job["attempts"] + 1 >= MAX_ATTEMPTS
        with tx() as conn:  # retry later with back-off, give up after MAX_ATTEMPTS
            conn.execute(
                "UPDATE jobs SET status = %s, error = %s, run_after = now() + make_interval(secs => %s) WHERE id = %s",
                ("failed" if failed else "queued", err, 30 * 2 ** job["attempts"], job["id"]))
        log.warning("job %s (%s) failed%s", job["id"], job["kind"], " for good" if failed else "")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    stop = False

    def _stop(*_: Any) -> None:
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    open_pool(conninfo(settings()))
    # one worker: a job still "running" now was cut off by a restart (deploy, crash); put it back in the queue
    with tx() as conn:
        n = conn.execute("UPDATE jobs SET status = 'queued', locked_at = NULL WHERE status = 'running'").rowcount
    if n:
        log.info("requeued %d interrupted job(s)", n)
    from . import mail, processing
    processing.register(HANDLERS)
    mail.register(HANDLERS)
    from .embed import embedder
    embedder()  # loads (and the first time downloads) the model before taking jobs; the API reads it from /models
    log.info("worker started; handlers: %s", ", ".join(HANDLERS) or "none yet")
    try:
        while not stop:
            job = take_job()
            if job:
                run_job(job)
            else:
                time.sleep(2)
    finally:
        close_pool()


if __name__ == "__main__":
    main()
