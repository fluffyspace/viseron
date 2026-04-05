"""Run Viseron once as a test harness.

Usage:

    python -m viseron.components.test_runner

Starts Viseron using the configuration in ``$VISERON_CONFIG_DIR``
(default ``/config``), waits for the ``test_runner`` component to finish
its run, logs a summary, and exits with status ``0`` if every case
passed or ``1`` otherwise.

The underlying ``config.yaml`` must contain a ``test_runner`` block plus
a camera component (e.g. ``ffmpeg``) whose cameras are configured with
``test_mode: true`` and a ``file_source`` path.
"""
from __future__ import annotations

import logging
import signal
import sys
import threading

from viseron import Viseron, setup_viseron

from .const import COMPONENT

LOGGER = logging.getLogger("viseron.test_runner.cli")


def main() -> int:
    """Run a test run and return an exit code."""
    vis: Viseron | None = None

    def signal_term(*_):
        if vis:
            vis.shutdown()

    signal.signal(signal.SIGTERM, signal_term)
    signal.signal(signal.SIGINT, signal_term)

    vis = Viseron()
    setup_viseron(vis)

    component = vis.data.get(COMPONENT)
    if component is None:
        LOGGER.error(
            "test_runner component is not configured — add a 'test_runner' "
            "block to your config.yaml"
        )
        vis.shutdown()
        return 2

    # The component may or may not have already triggered a run during setup
    # depending on auto_start. Trigger one here if nothing is running yet.
    if component.current_runner is None:
        runner = component.trigger_run()
    else:
        runner = component.current_runner

    LOGGER.info("test_runner: waiting for run to complete")
    # Wake periodically so Ctrl-C still reaches the main thread.
    while not runner.completion_event.wait(timeout=1.0):
        if not threading.main_thread().is_alive():
            break

    summary = runner.summary
    if summary is None:
        LOGGER.error("test_runner: runner finished without a summary")
        vis.shutdown()
        return 2

    LOGGER.info(
        "test_runner: run %s — total=%d passed=%d failed=%d status=%s",
        summary.run_id,
        summary.total,
        summary.passed,
        summary.failed,
        summary.status,
    )

    exit_code = vis.exit_code
    vis.shutdown()
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
