"""Shared host-side pause / camera / resume recording helpers."""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import tifffile

from camera import Camera
from configuration import (
    camera_exposure_us,
    camera_gain,
    camera_settings,
    fps,
    results_path,
    roi,
)


def wait_until_paused(job, timeout_s: float = 30.0) -> None:
    deadline = time.monotonic() + timeout_s
    while not job.is_paused():
        if time.monotonic() > deadline:
            raise TimeoutError("OPX did not reach the camera-start pause")
        time.sleep(0.01)


def new_results_folder(label: str) -> Path:
    folder = results_path / datetime.now(timezone.utc).strftime(f"%Y%m%dT%H%M%S_%fZ_{label}")
    folder.mkdir(parents=True)
    return folder


def run_with_camera(
    *,
    qm,
    prog,
    label: str,
    duration_s: float,
    record: dict[str, Any],
    before_resume: Callable[[Any], None] | None = None,
    after_recording: Callable[[Any, Path], None] | None = None,
) -> Path:
    """Execute *prog*, capture camera frames around resume, always cancel and close.

    Sequence: execute → wait for ``pause()`` → optional *before_resume* (e.g. push
    input streams) → warm up camera → resume → record → optional *after_recording*
    → cancel job → close QM → write ``run.json``.
    """
    folder = new_results_folder(label)
    job = None
    try:
        job = qm.execute(prog)
        record.update(qm_id=qm.id, job_id=job.id)
        wait_until_paused(job)
        if before_resume is not None:
            before_resume(job)
        with Camera(
            camera_settings,
            roi=roi,
            fps=fps,
            exposure_us=camera_exposure_us,
            gain=camera_gain,
        ) as camera:
            camera.frames(3, discard=2)
            job.resume()
            n_frames = max(1, round((duration_s + 0.5) * fps))
            frames, timestamps = camera.frames(n_frames, discard=0)
            record["camera"] = camera.metadata
        record["timestamps"] = timestamps
        record["duration_s"] = duration_s
        record["n_frames"] = int(frames.shape[0])
        tifffile.imwrite(folder / "frames.tiff", frames, photometric="minisblack")
        if after_recording is not None:
            after_recording(job, folder)
    finally:
        try:
            if job is not None:
                job.cancel()
        finally:
            qm.close()
        (folder / "run.json").write_text(json.dumps(record, indent=2))
    print(f"Outputs stopped. Recording: {folder}", flush=True)
    return folder
