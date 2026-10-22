# MJ camera capture (12×12 AOD, compressed grid)

Self-contained minimal-jerk variants that record a FLIR camera while the OPX
runs slow (1 s) moves. Separate from the top-level 7×7 rearrangement scripts in
this repo.

## Hardware assumptions

| Item | Value |
|---|---|
| X AOD | con1 FEM 2 port 4 |
| Y AOD | con1 FEM 3 port 4 |
| Grid | 12×12 at 95–101 MHz (~0.545 MHz spacing) |
| Amplitude | 0.8 mV peak per tone (≤ 9.6 mV coherent sum / port) |
| Move timing | 50 µs segments × 20 000 = 1 s; 50 µs up/down ramps |
| Camera | Blackfly BFLY-PGE-50S5M, ROI 160×160 @ 45 fps |
| Sync | Software `pause()` / resume (no trigger cable) |

## Setup (Windows)

From this directory (uses its own `pyproject.toml`; or inherit the parent uv env
with Spinnaker already installed):

```powershell
cd mj_test
uv sync --inexact
uv pip install --python .venv\Scripts\python.exe C:\path\to\spinnaker_python-*-cp312-*-win_amd64.whl
```

Edit `configuration.py` if the OPX IP, ROI, or exposure need changing. Spot
brightness at 0.8 mV is ~13 % of the bench CW tones; raise
`camera_exposure_us` if frames are dark.

## Run order

```powershell
uv run --inexact python make_plans.py
uv run --inexact python 01_static_image.py          # one grid image + pixel_map.json
uv run --inexact python 02_row_sort_capture.py       # all 12 rows (~12 s)
uv run --inexact python make_gif.py                  # latest recording → split GIF
uv run --inexact python 03_streamed_move_capture.py  # one streamed step
uv run --inexact python make_gif.py
uv run --inexact python 04_move_set_capture.py        # one move set
uv run --inexact python make_gif.py
```

Recordings land under `results/<timestamp>_<label>/` with `frames.tiff`,
`run.json`, and (after `make_gif.py`) `capture_split.gif` + `verification.json`.

## Safety check (simulator, no RF)

```bash
python check_peak_voltage.py
```

Confirms the <10 mV coherent-sum guard, an all-on port probe, a short gate+2-mover
chirp probe, serialization of the full 20 000-segment programs, and a `pause()`
serialize check. Does not cancel other users' quantum machines. Full 12-mover
programs are too slow for this QOP simulator; see `results/peak_voltage_check.json`.

## Plans

- `02`: neighbor-pull at 0.3 × spacing on all 12 rows
- `03`: one live-grid step, first three X tones move by 0.5 × spacing
- `04`: one packed move set (same X movers + Y₁ static), `flag=0`
