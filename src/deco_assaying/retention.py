"""Background sweeper that purges aged-out job dirs under OUTPUT_ROOT.

Runs as an asyncio task started in the FastAPI lifespan. Every
`SWEEP_INTERVAL_SECONDS` it walks `OUTPUT_ROOT` one level deep and
removes any job dir whose mtime is older than `OUTPUT_EXPIRY_DAYS`,
unless the job is still active in the in-memory table.

Set `OUTPUT_EXPIRY_DAYS=0` to disable the sweeper entirely (for
ops who want manual control via DELETE /outputs/{id}).
"""

from __future__ import annotations

import asyncio
import logging
import time

from deco_assaying import config, jobs, outputs

log = logging.getLogger(__name__)

# How often the sweeper wakes up. One hour is dense enough that purges
# happen reasonably soon after expiry without burning CPU on a quiet
# server.
SWEEP_INTERVAL_SECONDS: float = 60 * 60


def sweep_once(*, now: float | None = None) -> list[str]:
    """Run one pass and return the job_ids that were removed.

    Public so tests can drive it directly without spinning up the
    background task.
    """
    if config.OUTPUT_EXPIRY_DAYS <= 0:
        return []
    root = config.OUTPUT_ROOT
    if not root.is_dir():
        return []

    cutoff = (now if now is not None else time.time()) - (config.OUTPUT_EXPIRY_DAYS * 86400)
    removed: list[str] = []
    for child in root.iterdir():
        if not child.is_dir():
            continue
        job_id = child.name
        if jobs.is_active(job_id):
            continue
        try:
            mtime = child.stat().st_mtime
        except OSError:
            continue
        if mtime > cutoff:
            continue
        try:
            outputs.remove_job_dir(child)
        except OSError as e:
            log.warning("retention: failed to remove %s: %s", child, e)
            continue
        # Drop the table entry too so /admin/jobs doesn't keep pointing
        # at a vanished output_path.
        jobs.drop(job_id)
        removed.append(job_id)
    if removed:
        log.info("retention: removed %d expired job dirs", len(removed))
    return removed


async def run_forever() -> None:
    """Lifespan-driven background loop. Cancelled when the app stops."""
    if config.OUTPUT_EXPIRY_DAYS <= 0:
        log.info("retention: disabled (OUTPUT_EXPIRY_DAYS=0)")
        return
    log.info(
        "retention: sweeper starting (every %.0fs, cutoff=%dd)",
        SWEEP_INTERVAL_SECONDS,
        config.OUTPUT_EXPIRY_DAYS,
    )
    # Run an initial sweep on startup so a long-stopped server doesn't
    # keep stale dirs around for an extra hour after restart.
    try:
        await asyncio.to_thread(sweep_once)
    except Exception:
        log.exception("retention: initial sweep failed")
    while True:
        try:
            await asyncio.sleep(SWEEP_INTERVAL_SECONDS)
            await asyncio.to_thread(sweep_once)
        except asyncio.CancelledError:
            log.info("retention: sweeper stopping")
            raise
        except Exception:
            log.exception("retention: sweep failed; will retry next cycle")
