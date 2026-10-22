"""Build a split-screen GIF from a camera_capture recording."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import tifffile
from PIL import Image, ImageDraw

from configuration import (
    IF_HIGH_HZ,
    IF_LOW_HZ,
    NUM_X_TWEEZERS,
    NUM_Y_TWEEZERS,
    number_of_segments,
    rampdown_length,
    rampup_length,
    results_path,
    roi,
    segment_length,
    x_tweezer_IFs,
    y_tweezer_IFs,
)

# Bench fallback: ~18 px/MHz from brightness calibration, centered in the ROI.
BENCH_PX_PER_MHZ = 18.0


def minimal_jerk_fraction(tau: float) -> float:
    """f(τ) = 10τ³ − 15τ⁴ + 6τ⁵ for τ in [0, 1]."""
    tau = float(np.clip(tau, 0.0, 1.0))
    return 10 * tau**3 - 15 * tau**4 + 6 * tau**5


def default_pixel_map() -> dict:
    """Affine map approximating the bench optics (no 01 fit yet)."""
    w, h = roi[2], roi[3]
    fx0 = IF_LOW_HZ / 1e6
    fy0 = IF_LOW_HZ / 1e6
    # Center the 95–101 MHz grid in the ROI.
    mid_f = 0.5 * (IF_LOW_HZ + IF_HIGH_HZ) / 1e6
    return {
        "pixel_x": {
            "offset": w / 2 - BENCH_PX_PER_MHZ * mid_f,
            "mhz_x": BENCH_PX_PER_MHZ,
            "mhz_y": 0.0,
        },
        "pixel_y": {
            "offset": h / 2 - BENCH_PX_PER_MHZ * mid_f,
            "mhz_x": 0.0,
            "mhz_y": BENCH_PX_PER_MHZ,
        },
        "source": "bench_fallback",
        "fit_rms_px": None,
    }


def load_pixel_map() -> dict:
    path = results_path / "pixel_map.json"
    if path.exists():
        data = json.loads(path.read_text())
        data.setdefault("source", "01_static_image")
        return data
    return default_pixel_map()


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


def latest_recording(*labels: str) -> Path:
    folders = []
    for label in labels:
        folders.extend(results_path.glob(f"*_{label}"))
    if not folders:
        raise FileNotFoundError(f"No recordings matching {labels} under {results_path}")
    return sorted(folders)[-1]


def chirp_window_s(n_segments: int | None = None) -> float:
    n = number_of_segments if n_segments is None else n_segments
    return (rampup_length + n * segment_length + rampdown_length) * 1e-9


def tone_freq_during_move(f0: float, det: float, t_local: float, move_s: float) -> float:
    """Frequency during rampup / MJ chirp / rampdown for one tone."""
    ramp_up_s = rampup_length * 1e-9
    ramp_down_s = rampdown_length * 1e-9
    chirp_s = move_s - ramp_up_s - ramp_down_s
    if t_local < 0:
        return f0
    if t_local < ramp_up_s:
        return f0
    if t_local < ramp_up_s + chirp_s:
        tau = (t_local - ramp_up_s) / max(chirp_s, 1e-15)
        return f0 + det * minimal_jerk_fraction(tau)
    return f0 + det


def expected_spots_02(record: dict, t: float, pixel_map: dict) -> list[tuple[float, float]]:
    plan = record["plan"]
    cycle = float(record.get("cycle_seconds", chirp_window_s()))
    num_rows = int(record.get("num_rows", NUM_Y_TWEEZERS))
    if t < 0 or t >= num_rows * cycle:
        return []
    row = int(t // cycle)
    local = t - row * cycle
    gate = float(plan["gate_freq"][row])
    spots = []
    for col in range(NUM_X_TWEEZERS):
        f0 = float(plan["tweezer_freq"][row][col])
        det = float(plan["tweezer_detuning"][row][col])
        if f0 <= 0:
            continue
        if det == 0:
            fx = f0
        else:
            fx = tone_freq_during_move(f0, det, local, cycle)
        spots.append(freq_to_pixel(fx, gate, pixel_map))
    return spots


def expected_spots_03(record: dict, t: float, pixel_map: dict) -> list[tuple[float, float]]:
    steps = record["steps"]
    cycle = float(record.get("cycle_seconds", chirp_window_s()))
    # After ramp-up of the grid, one step runs for `cycle` seconds.
    # Approximate: ignore host ramp-up visual (tones already at IF), step starts at t=0.
    if not (0 <= t < cycle):
        # Still show static grid outside the move window.
        if t < 0:
            return []
        spots = []
        cur_x = x_tweezer_IFs.astype(float).copy()
        cur_y = y_tweezer_IFs.astype(float).copy()
        for step in steps:
            for i, (f, d) in enumerate(zip(step["x_freq"], step["x_det"])):
                if f > 0 and d != 0:
                    cur_x[i] = f + d
            for i, (f, d) in enumerate(zip(step["y_freq"], step["y_det"])):
                if f > 0 and d != 0:
                    cur_y[i] = f + d
        for fx in cur_x:
            for fy in cur_y:
                spots.append(freq_to_pixel(fx, fy, pixel_map))
        return spots

    step = steps[0]
    spots = []
    for ix, (fx0, dx) in enumerate(zip(step["x_freq"], step["x_det"])):
        fx = tone_freq_during_move(float(fx0), float(dx), t, cycle) if fx0 > 0 else float(x_tweezer_IFs[ix])
        for iy, (fy0, dy) in enumerate(zip(step["y_freq"], step["y_det"])):
            fy = (
                tone_freq_during_move(float(fy0), float(dy), t, cycle)
                if fy0 > 0
                else float(y_tweezer_IFs[iy])
            )
            # Live grid: all 12x12 stay on.
            spots.append(freq_to_pixel(fx if fx0 > 0 else float(x_tweezer_IFs[ix]), fy if fy0 > 0 else float(y_tweezer_IFs[iy]), pixel_map))
    # Deduplicate by rendering full grid with updated freqs
    cur_x = x_tweezer_IFs.astype(float).copy()
    cur_y = y_tweezer_IFs.astype(float).copy()
    for i, (f, d) in enumerate(zip(step["x_freq"], step["x_det"])):
        if f > 0:
            cur_x[i] = tone_freq_during_move(float(f), float(d), t, cycle)
    for i, (f, d) in enumerate(zip(step["y_freq"], step["y_det"])):
        if f > 0:
            cur_y[i] = tone_freq_during_move(float(f), float(d), t, cycle)
    return [freq_to_pixel(fx, fy, pixel_map) for fx in cur_x for fy in cur_y]


def expected_spots_04(record: dict, t: float, pixel_map: dict) -> list[tuple[float, float]]:
    sets = record["sets"]
    cycle = float(record.get("cycle_seconds", chirp_window_s()))
    if not (0 <= t < cycle * len(sets)):
        return []
    set_index = min(len(sets) - 1, int(t // cycle))
    local = t - set_index * cycle
    move_set = sets[set_index]
    spots = []
    active_x = []
    active_y = []
    for i, (f, d) in enumerate(zip(move_set["x_freq"], move_set["x_det"])):
        if f > 0:
            active_x.append(tone_freq_during_move(float(f), float(d), local, cycle))
    for i, (f, d) in enumerate(zip(move_set["y_freq"], move_set["y_det"])):
        if f > 0:
            active_y.append(tone_freq_during_move(float(f), float(d), local, cycle))
    for fx in active_x:
        for fy in active_y:
            spots.append(freq_to_pixel(fx, fy, pixel_map))
    return spots


def expected_spots(record: dict, t: float, pixel_map: dict) -> list[tuple[float, float]]:
    script = record.get("script", "")
    if "02" in script or "row_sort" in script:
        return expected_spots_02(record, t, pixel_map)
    if "03" in script or "streamed" in script:
        return expected_spots_03(record, t, pixel_map)
    if "04" in script or "move_set" in script:
        return expected_spots_04(record, t, pixel_map)
    return []


def render_expected(shape, spots, scale: float, panel_size: int) -> Image.Image:
    h, w = shape
    panel = Image.new("RGB", (panel_size, panel_size), "#101010")
    draw = ImageDraw.Draw(panel)
    for x, y in spots:
        px, py = x * scale, y * scale
        r = 4
        draw.ellipse((px - r, py - r, px + r, py + r), fill="#6edce8")
    return panel


def synthetic_frame(shape, spots) -> np.ndarray:
    """Soft spots for correlation against camera frames."""
    h, w = shape
    yy, xx = np.mgrid[0:h, 0:w]
    frame = np.zeros((h, w), dtype=float)
    for x, y in spots:
        frame += np.exp(-((xx - x) ** 2 + (yy - y) ** 2) / (2 * 3.0**2))
    return frame


def correlation_score(camera: np.ndarray, expected: np.ndarray) -> float:
    a = camera.astype(float) - np.median(camera)
    b = expected - expected.mean()
    if a.std() < 1e-9 or b.std() < 1e-9:
        return 0.0
    a = (a - a.mean()) / a.std()
    b = b / b.std()
    return float(np.mean(a * b))


def fit_offset(frames, times, record, pixel_map, duration: float) -> tuple[float, float]:
    """Search ±30 ms around first bright frame for best correlation."""
    peaks = np.array([float(f.max()) for f in frames])
    background = float(np.median(peaks[: min(5, len(peaks))])) if len(peaks) else 0.0
    bright = np.where(peaks > background + 10)[0]
    if len(bright) == 0:
        return float(times[0]), 0.0
    t0_guess = float(times[bright[0]])
    best_start, best_score = t0_guess, -1.0
    for offset in np.arange(t0_guess - 0.030, t0_guess + 0.030, 0.001):
        scores = []
        for i in range(0, len(frames), max(1, len(frames) // 40)):
            elapsed = times[i] - offset
            if not (0 <= elapsed < duration):
                continue
            spots = expected_spots(record, elapsed, pixel_map)
            if not spots:
                continue
            syn = synthetic_frame(frames[i].shape, spots)
            scores.append(correlation_score(frames[i], syn))
        score = float(np.mean(scores)) if scores else -1.0
        if score > best_score:
            best_score, best_start = score, float(offset)
    return best_start, best_score


def duration_of(record: dict) -> float:
    script = record.get("script", "")
    cycle = float(record.get("cycle_seconds", chirp_window_s()))
    if "02" in script or "row_sort" in script:
        return int(record.get("num_rows", NUM_Y_TWEEZERS)) * cycle
    if "04" in script or "move_set" in script:
        return len(record.get("sets", [{}])) * cycle
    return cycle


def make_gif(folder: Path) -> Path:
    record = json.loads((folder / "run.json").read_text())
    frames = tifffile.imread(folder / "frames.tiff")
    if frames.ndim == 2:
        frames = frames[None, ...]
    timestamps = np.array([p["timestamp_ns"] for p in record["timestamps"]], dtype=np.int64)
    times = (timestamps - timestamps[0]) / 1e9
    pixel_map = load_pixel_map()
    duration = duration_of(record)

    start, score = fit_offset(frames, times, record, pixel_map, duration)
    chosen = np.where((times >= start) & (times < start + duration))[0]
    if len(chosen) == 0:
        chosen = np.arange(len(frames))

    panel_size = 480
    divider = 4
    scale = panel_size / frames.shape[2]
    gif_frames = []
    for i in chosen:
        elapsed = max(0.0, float(times[i] - start))
        spots = expected_spots(record, elapsed, pixel_map)
        left = render_expected(frames[i].shape, spots, scale, panel_size)
        right = Image.fromarray(frames[i]).resize(
            (panel_size, panel_size), Image.Resampling.NEAREST
        ).convert("RGB")
        image = Image.new("RGB", (2 * panel_size + divider, panel_size), "#444444")
        image.paste(left, (0, 0))
        image.paste(right, (panel_size + divider, 0))
        gif_frames.append(image)

    if not gif_frames:
        raise RuntimeError("No frames selected for GIF")

    edges = np.round(np.linspace(0, max(duration, 0.1) * 1000, len(gif_frames) + 1) / 10) * 10
    durations = np.diff(edges).astype(int).tolist()
    out = folder / "capture_split.gif"
    gif_frames[0].save(
        out,
        save_all=True,
        append_images=gif_frames[1:],
        duration=durations,
        loop=0,
        optimize=False,
    )

    frame_ids = [p["frame_id"] for p in record["timestamps"]]
    missing = int(np.diff(frame_ids).sum() - (len(frames) - 1)) if len(frame_ids) > 1 else 0
    measured_fps = float(1e9 / np.median(np.diff(timestamps))) if len(timestamps) > 1 else 0.0
    verification = {
        "measured_fps": measured_fps,
        "missing_frame_ids": missing,
        "camera_opx_offset_s": start,
        "correlation_score": score,
        "peak": int(frames.max()),
        "saturated_pixels": int((frames == 255).sum()),
        "pixel_map_source": pixel_map.get("source"),
        "n_gif_frames": len(gif_frames),
        "duration_s": duration,
    }
    (folder / "verification.json").write_text(json.dumps(verification, indent=2))
    print(json.dumps(verification, indent=2))
    print(out)
    return out


def main() -> None:
    if len(sys.argv) > 1:
        folder = Path(sys.argv[1])
    else:
        folder = latest_recording("row_sort", "streamed_move", "move_set", "static_image")
    make_gif(folder)


if __name__ == "__main__":
    main()
