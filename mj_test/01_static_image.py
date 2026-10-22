"""Ramp up 12x12 static AOD tones, capture one camera image, fit frequency→pixel map."""

from __future__ import annotations

import json

import numpy as np
from qm import QuantumMachinesManager
from qm.qua import *

from amplitude_calibration import load_xy_amp_calibration, validate_xy_compensation_range
from configuration import (
    CALIBRATION_ROOT,
    X_FEMS,
    Y_FEMS,
    camera_exposure_us,
    camera_gain,
    camera_settings,
    cluster_name,
    config,
    qop_ip,
    qop_port,
    results_path,
    roi,
    x_tweezer_IFs,
    y_tweezer_IFs,
)
from static_grid import initial_amp_values, ramp_down_grid, ramp_up_grid

SCRIPT_NAME = "01_static_image"
HOLD_SECONDS = 2.0


def build_program(
    x_amp_calibration,
    y_amp_calibration,
    *,
    with_pause: bool = True,
    hold_seconds: float | None = None,
):
    x_amps_py, y_amps_py = initial_amp_values(x_amp_calibration, y_amp_calibration)
    hold_s = HOLD_SECONDS if hold_seconds is None else hold_seconds

    with program() as prog:
        if with_pause:
            pause()
        play("trigger", "scope_trigger")
        ramp_up_grid(x_amp_calibration, y_amp_calibration)
        # Hold long enough for a single exposure; host cancels after capture.
        wait(max(4, int(hold_s * 1e9 / 4)))
        ramp_down_grid(x_amps_py=x_amps_py, y_amps_py=y_amps_py)

    return prog


def detect_spots(frame: np.ndarray, n_expected: int = 144) -> list[dict]:
    """Greedy peak picking with local suppression."""
    background = float(np.median(frame))
    remaining = frame.astype(float).copy()
    yy, xx = np.indices(frame.shape)
    found = []
    for _ in range(n_expected + 10):
        y, x = np.unravel_index(np.argmax(remaining), remaining.shape)
        if remaining[y, x] < background + 15:
            break
        radius_squared = (xx - x) ** 2 + (yy - y) ** 2
        weight = np.maximum(frame.astype(float) - background, 0) * (radius_squared <= 16)
        if weight.sum() <= 0:
            remaining[radius_squared <= 36] = background
            continue
        found.append(
            {
                "x": float((weight * xx).sum() / weight.sum()),
                "y": float((weight * yy).sum() / weight.sum()),
                "peak": float(remaining[y, x]),
            }
        )
        remaining[radius_squared <= 49] = background
    return found


def fit_pixel_map(spots: list[dict], x_ifs, y_ifs) -> dict:
    """Fit affine map: pixel ≈ a0 + ax*fx_MHz + ay*fy_MHz (separate for x,y).

    Expects a roughly rectangular 12x12 lattice. Assigns spots to a grid by
    sorting y then x, then fits linear maps from IF indices.
    """
    if len(spots) < 4:
        raise ValueError(f"Need at least 4 spots to fit a map; found {len(spots)}")

    pts = np.array([[s["x"], s["y"]] for s in spots])
    # Cluster into rows by y, then sort x within rows.
    order = np.argsort(pts[:, 1])
    pts = pts[order]
    n = int(np.sqrt(len(pts)))
    if n * n != len(pts):
        # Fall back: use nearest-neighbour grid assignment on expected count.
        n_x, n_y = len(x_ifs), len(y_ifs)
        # Sort all by y into n_y bands.
        ys = pts[:, 1]
        row_edges = np.linspace(ys.min() - 1e-6, ys.max() + 1e-6, n_y + 1)
        rows = []
        for r in range(n_y):
            mask = (ys >= row_edges[r]) & (ys < row_edges[r + 1])
            row_pts = pts[mask]
            row_pts = row_pts[np.argsort(row_pts[:, 0])] if len(row_pts) else row_pts
            rows.append(row_pts)
    else:
        n_x = n_y = n
        rows = [pts[r * n : (r + 1) * n] for r in range(n)]
        rows = [row[np.argsort(row[:, 0])] for row in rows]

    xs, ys, fxs, fys = [], [], [], []
    for r, row in enumerate(rows):
        for c, (px, py) in enumerate(row[: len(x_ifs)]):
            if r >= len(y_ifs):
                break
            xs.append(px)
            ys.append(py)
            fxs.append(float(x_ifs[c]) / 1e6)
            fys.append(float(y_ifs[r]) / 1e6)

    xs, ys = np.asarray(xs), np.asarray(ys)
    fxs, fys = np.asarray(fxs), np.asarray(fys)
    # px = a0 + ax * fx + ay * fy ; same for py
    A = np.column_stack([np.ones(len(fxs)), fxs, fys])
    cx, *_ = np.linalg.lstsq(A, xs, rcond=None)
    cy, *_ = np.linalg.lstsq(A, ys, rcond=None)
    pred_x = A @ cx
    pred_y = A @ cy
    rms = float(np.sqrt(np.mean((pred_x - xs) ** 2 + (pred_y - ys) ** 2)))
    return {
        "pixel_x": {"offset": float(cx[0]), "mhz_x": float(cx[1]), "mhz_y": float(cx[2])},
        "pixel_y": {"offset": float(cy[0]), "mhz_x": float(cy[1]), "mhz_y": float(cy[2])},
        "n_spots_used": int(len(xs)),
        "n_spots_detected": int(len(spots)),
        "fit_rms_px": rms,
        "x_ifs_mhz": (np.asarray(x_ifs) / 1e6).tolist(),
        "y_ifs_mhz": (np.asarray(y_ifs) / 1e6).tolist(),
        "roi": list(roi),
    }


def freq_to_pixel(fx_hz: float, fy_hz: float, pixel_map: dict) -> tuple[float, float]:
    fx, fy = fx_hz / 1e6, fy_hz / 1e6
    px = (
        pixel_map["pixel_x"]["offset"]
        + pixel_map["pixel_x"]["mhz_x"] * fx
        + pixel_map["pixel_x"]["mhz_y"] * fy
    )
    py = (
        pixel_map["pixel_y"]["offset"]
        + pixel_map["pixel_y"]["mhz_x"] * fx
        + pixel_map["pixel_y"]["mhz_y"] * fy
    )
    return px, py


def main() -> None:
    from camera import capture_one
    from capture import new_results_folder, wait_until_paused

    x_amp_calibration, y_amp_calibration = load_xy_amp_calibration(
        CALIBRATION_ROOT, latency_only=True, module_name=SCRIPT_NAME
    )
    validate_xy_compensation_range(
        x_amp_calibration, y_amp_calibration, x_tweezer_IFs, y_tweezer_IFs
    )

    prog = build_program(x_amp_calibration, y_amp_calibration)

    qmm_kwargs = {"host": qop_ip, "cluster_name": cluster_name, "timeout": 300}
    if qop_port is not None:
        qmm_kwargs["port"] = qop_port
    qmm = QuantumMachinesManager(**qmm_kwargs)

    from camera import Camera
    import tifffile
    from PIL import Image

    folder = new_results_folder("static_image")
    record = {
        "script": SCRIPT_NAME,
        "roi": list(roi),
        "x_ifs": x_tweezer_IFs.astype(int).tolist(),
        "y_ifs": y_tweezer_IFs.astype(int).tolist(),
        "config_ports": {
            "x": [list(p) for p in X_FEMS],
            "y": [list(p) for p in Y_FEMS],
        },
    }
    qm = qmm.open_qm(config, close_other_machines=True)
    job = None
    try:
        job = qm.execute(prog)
        record.update(qm_id=qm.id, job_id=job.id)
        wait_until_paused(job)
        # Warm camera before resume (same order as capture.run_with_camera).
        with Camera(
            camera_settings,
            roi=roi,
            fps=3,
            exposure_us=camera_exposure_us,
            gain=camera_gain,
        ) as camera:
            camera.frames(3, discard=2)
            job.resume()
            frames, timestamps = camera.frames(1, discard=0)
            pixels = frames[0]
            record["camera"] = dict(camera.metadata, **timestamps[0])
        tifffile.imwrite(folder / "frame.tiff", pixels)
        Image.fromarray(pixels).save(folder / "frame.png")
        record["camera"]["peak"] = int(pixels.max())
        record["camera"]["saturated_pixels"] = int((pixels == 255).sum())
        (folder / "camera.json").write_text(json.dumps(record["camera"], indent=2))
        spots = detect_spots(pixels)
        record["n_spots"] = len(spots)
        record["spots"] = spots
        try:
            pixel_map = fit_pixel_map(spots, x_tweezer_IFs, y_tweezer_IFs)
            (folder / "pixel_map.json").write_text(json.dumps(pixel_map, indent=2))
            # Also publish as the latest map for make_gif.py
            (results_path / "pixel_map.json").write_text(json.dumps(pixel_map, indent=2))
            record["pixel_map"] = pixel_map
            print(f"Pixel map RMS={pixel_map['fit_rms_px']:.2f} px from {pixel_map['n_spots_used']} spots")
        except ValueError as exc:
            print(f"Pixel map fit skipped: {exc}")
            record["pixel_map_error"] = str(exc)
    finally:
        try:
            if job is not None:
                job.cancel()
        finally:
            qm.close()
        (folder / "run.json").write_text(json.dumps(record, indent=2))
    print(f"Outputs stopped. Recording: {folder}")


if __name__ == "__main__":
    main()
