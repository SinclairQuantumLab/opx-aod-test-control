"""Measure the cropped camera rate without changing RF output."""

import json
from datetime import datetime, timezone
import numpy as np
import tifffile
from camera import Camera
from configuration import camera_settings, results_path

roi = (504, 324, 160, 160)  # x, y, width, height around the measured 98 MHz spot
requested_fps = 60

if __name__ == "__main__":
    folder = results_path / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ_camera_rate")
    folder.mkdir(parents=True)
    with Camera(camera_settings, roi=roi, fps=requested_fps) as camera:
        frames, timestamps = camera.frames(120)
    intervals = np.diff([item["timestamp_ns"] for item in timestamps]) / 1e9
    result = dict(camera.metadata, measured_fps=float(1 / np.median(intervals)),
                  missing_frame_ids=int(np.diff([item["frame_id"] for item in timestamps]).sum() - 119),
                  timestamps=timestamps)
    tifffile.imwrite(folder / "frames.tiff", frames, photometric="minisblack")
    (folder / "camera.json").write_text(json.dumps(result, indent=2))
    print({key: value for key, value in result.items() if key != "timestamps"})
    print(folder)
