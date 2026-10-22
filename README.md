# OPX AOD test control

Python and QUA scripts for driving two AODs with an OPX1000, recording a FLIR camera, and testing tweezer rearrangement.

## Executive summary

The current experiment moves three spots in each row of a 7x7 grid into the rightmost three columns. It runs five random loadings, taking two seconds per loading, and records the motion for a split-screen GIF. Starting sites are generated in Python; the OPX executes the sequence using linear frequency chirps.

The project uses Python 3.12, uv, and qm-qua. Connection settings and output amplitudes are in `configuration.py`.

## Setup

From the repository folder:

```powershell
uv sync --inexact
```

Camera acquisition requires FLIR Spinnaker and its matching Python wheel. The tested installation uses Spinnaker 4.4.0.246 and Python 3.12 on Windows:

```powershell
uv pip install --python .venv/Scripts/python.exe C:/path/to/spinnaker_python-4.4.0.246-cp312-cp312-win_amd64.whl
```

Use `--inexact` with uv to retain the separately installed camera package.

In VS Code, select `.venv/Scripts/python.exe` with **Python: Select Interpreter**. Open an experiment and use **Run Python File**, or run the commands below in the terminal.

## Run

Check the OPX address, FEM slot, ports, and camera serial number in `configuration.py`. Close other camera acquisition software and coordinate use of the configured outputs.

For continuous 98 MHz tones:

```powershell
uv run --inexact python 00_cw.py
```

Set frequencies and voltages in `configuration.py`. In `00_cw.py`, `run_seconds` sets the run duration; `None` runs until Ctrl+C.

For rearrangement and camera recording:

```powershell
uv run --inexact python 03_rearrange.py
uv run --inexact python 04_make_gif.py
```

Settings are at the top of `03_rearrange.py`. Set `cycles = 1` for one two-second cycle. Changing `seed` changes the repeatable random loading. `simulate = False` runs the hardware; `True` compiles the sequence and simulates its first 10 microseconds on the QOP server.

Each recording goes into a timestamped folder under `results/`, containing `frames.tiff` and `run.json`. The GIF script selects the latest rearrangement folder and writes `rearrangement_split.gif`. To select a specific recording:

```powershell
uv run --inexact python 04_make_gif.py results/<recording-folder>
```

The experiment cancels its job and closes its quantum machine on completion or a normal Ctrl+C interruption. Camera settings are restored when acquisition ends.

## Files

| File | Purpose |
|---|---|
| `configuration.py` | OPX connection, ports, frequencies, amplitudes, and results directory |
| `00_cw.py` | Continuous tones and optional camera capture |
| `01_camera_rate.py` | Measure camera frame rate with a cropped image |
| `02_calibrate.py` | Measure brightness corrections across the array |
| `03_rearrange.py` | Run rearrangements and record camera frames |
| `04_make_gif.py` | Check recorded spots and make the split-screen GIF |
| `05_readout_benchmark.py` | Simulate serial and multitone occupancy readout |
| `camera.py` | Spinnaker acquisition and camera-setting restoration |
| `mj_test/` | Slow (1 s) 12×12 minimal-jerk camera-capture scripts (own config/ports) |
| `pyproject.toml`, `uv.lock` | Python dependencies |

## Hardware

| Item | Configuration |
|---|---|
| Controller | OPX1000, `con1`, LF-FEM slot 3 |
| Software used for testing | QOP 3.8.1; qm-qua 1.4.1 |
| Output 1 | X AOD; increasing frequency moves right in the camera |
| Output 2 | Y AOD; increasing frequency moves down in the camera |
| Frequency grid | 95-101 MHz in 1 MHz steps |
| Camera | FLIR Blackfly BFLY-PGE-50S5M |
| Camera crop | 160x160 pixels; requested rate 45 fps |

## Rearrangement sequence

For each row, one row tone and three column tones create three spots. The columns hold at their starting positions for 20 ms, move for 220 ms, and hold at their destinations for approximately 20 ms. One-microsecond half-Blackman envelopes turn the tones on and off.

The movement is divided into eleven 20 ms segments. Each segment uses the same chirp rate and a calibrated amplitude correction. Each row switches off after its move. After all seven rows, seven row tones and three column tones display the full 7x3 rectangle for 180 ms. The total is two seconds per cycle.

The camera acquires continuously while the OPX runs. The GIF aligns the recording to the sequence using measured spot positions. Its left panel shows the generated loading; its right panel shows the camera frames.

Frequency-grid changes require matching calibration data. Timing changes also require updating the holds, recording duration, and GIF timing model.

## Calibration

The repository includes the two calibration files selected by `03_rearrange.py`:

- [Brightness map](results/20261022T013354_095955Z_brightness_calibration/calibration.json): 25 horizontal positions in each of seven rows.
- [Rectangle corrections](results/20261022T014139_082938Z_rearrangement/rectangle_feedback.json): row and column amplitude factors for the simultaneous rectangle.

These were measured on the lab setup on 21 October 2026. Recalibrate after changing the optical alignment, then select the new files in `03_rearrange.py`. New recordings are ignored by Git; the two calibration JSON files are tracked.

## Occupancy readout simulation

```powershell
uv run --inexact python 05_readout_benchmark.py sliced --symbol-ns 20
uv run --inexact python 05_readout_benchmark.py multitone
```

These benchmarks use a virtual loopback on the QOP simulator. For 400 sites with 200 occupied, the calibrated simulation gave:

| Readout | Packet start to data-dependent ready pulse |
|---|---:|
| Sliced integration, 20 ns/site | 13.260 us |
| Sliced integration, 40 ns/site | 21.280 us |
| Sliced integration, 100 ns/site | 45.340 us |
| 400 tones, eight I/Q receivers per bank | 102.884 us |

All four cases recovered the occupancy pattern with zero errors in the noise-free simulation. Timing includes conversion to the occupancy array and counting occupied sites. Camera processing, external trigger acquisition, and sorting require separate measurements. The simulated receiver uses calibrated timing and thresholds; physical link testing is the next step.

## Next steps

- Measure RF power and spectrum through the amplifier chain.
- Check brightness repeatability after alignment changes.
- Test occupancy transfer with an external FPGA and measure latency and bit errors.

## References

The script layout follows [QM's Hello QUA](https://github.com/qua-platform/qua-libs/blob/main/Quantum-Control-Applications/Superconducting/Single-Fixed-Transmon/00_hello_qua.py). The row-by-row rearrangement follows the approach in [Saffman's atom-sorting example](https://github.com/qua-platform/qua-libs/blob/main/Quantum-Control-Applications/AMO/Use%20Case%201%20-%20Saffman%20Lab%20-%20Atom%20Sorting/array_sorting.py).

Design decisions: 21 October 2026 - use QUA loops, lookup tables, and hardware chirps for the full sequence. 22 October 2026 - keep the required calibration files and new results under the repository's `results/` directory so the project can run from a clone.
