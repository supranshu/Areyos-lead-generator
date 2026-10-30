import logging
import os
import socket
import time

from areyos import queue
from areyos.config import get_settings
from areyos.db import session_scope
from areyos.engine import process_job

log = logging.getLogger(__name__)


def work_once(worker_id: str) -> bool:
    """Process one job if one is ready. Returns False when the queue is empty."""
    with session_scope() as session:
        job = queue.claim(session, worker_id)
        if job is None:
            return False
        log.info("job %s: %s %s", job.id, job.kind, job.payload.get("industry", ""))
        process_job(session, job)
        return True


def drain(worker_id: str = "inline") -> int:
    """Run jobs until none are ready (used by `areyos discover --wait` and tests)."""
    count = 0
    while work_once(worker_id):
        count += 1
    return count


def run_forever() -> None:
    worker_id = f"{socket.gethostname()}:{os.getpid()}"
    poll = get_settings().worker_poll_seconds
    log.info("worker %s started", worker_id)
    while True:
        try:
            if not work_once(worker_id):
                time.sleep(poll)
        except KeyboardInterrupt:
            break
        except Exception:  # noqa: BLE001 - keep the worker alive on DB hiccups
            log.exception("worker loop error")
            time.sleep(poll)
