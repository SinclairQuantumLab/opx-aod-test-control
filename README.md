# OPX AOD control

Standalone Python 3.12 / uv project for the OPX1000 and two AODs. No labscript dependency. The configuration-plus-numbered-experiment layout follows [QM's Hello QUA](https://github.com/qua-platform/qua-libs/blob/main/Quantum-Control-Applications/Superconducting/Single-Fixed-Transmon/00_hello_qua.py) and [Saffman's sorting example](https://github.com/qua-platform/qua-libs/blob/main/Quantum-Control-Applications/AMO/Use%20Case%201%20-%20Saffman%20Lab%20-%20Atom%20Sorting/array_sorting.py).

## Executive summary

**Readout benchmark, 21 October 2026:** Simulator-only tests for 400 sites / 200 occupied sites recover all bits with 13.26 µs packet-to-ready latency using 20 ns/site sliced integration, 21.28 µs at 40 ns/site, and 45.34 µs at 100 ns/site. A 400-tone comb decoded in 50 banks of eight I/Q receivers takes 102.884 µs. Timing includes occupancy-array conversion and occupied-site counting, but excludes camera processing, trigger acquisition and the sorting algorithm. These are calibrated, noise-free loopback simulations, not physical link-speed measurements. No hardware QM was opened. See the benchmark section below.

**21 October 2026:** Five different random loadings ran autonomously on the OPX, two seconds per cycle. Each cycle selects three sites in each of seven rows and moves them to the rightmost three columns, then displays the full 7×3 rectangle. The 10-second [camera GIF](../../figures/opx_control/rearrangement_5x2s_split.gif) contains real camera frames at 45.08 fps, with no dropped frames or unexpected spot counts. **RF outputs are now off; camera settings were restored and the camera released.**

Brightness variation relative to each group's mean was **2.03% RMS / 5.79% worst** for the moving rows and **3.99% RMS / 10.35% worst** for the final rectangles. Peak pixel value was 191/255, with no saturation. Correction improves uniformity but does not make every spot equal. These are optical spot measurements with software-generated occupancy, not trapped-atom transport measurements.

## Files

| File | Purpose |
|---|---|
| `configuration.py` | Connection, physical ports and CW configuration |
| `00_cw.py` | Continuous tones; explicit QUA program and execution |
| `01_camera_rate.py` | Measure cropped-camera throughput |
| `02_calibrate.py` | Measure brightness correction along seven rows |
| `03_rearrange.py` | Five autonomous rearrangements plus camera recording |
| `04_make_gif.py` | Verify frames and render their GIF |
| `05_readout_benchmark.py` | Simulator-only occupancy-link timing and bit verification |
| `camera.py` | Spinnaker acquisition and camera-setting restoration |
| `pyproject.toml`, `uv.lock` | Dependencies and locked versions |

## Run

Open a terminal in this folder. Close other camera acquisition software first.

```powershell
uv sync --inexact
uv run --inexact python 03_rearrange.py
uv run --inexact python 04_make_gif.py ../../results/opx_control/<recording-folder>
```

The experiment prints its recording folder and stops its outputs on completion. The renderer accepts no argument to select the most recent rearrangement folder; pass an explicit successful folder if a later run failed.

Settings at the top of `03_rearrange.py` include `cycles`, `seed`, camera ROI, timing and calibration paths. Changing `seed` produces different repeatable loadings. This experiment is specifically a seven-row, seven-site grid from **95 to 101 MHz**, centered on 98 MHz. Frequency changes require a matching calibration. Timing changes must also update the holds, recording duration and renderer timing model; the tested configuration is two seconds per cycle.

For a single continuous spot:

```powershell
uv run --inexact python 00_cw.py
```

Edit its frequencies and voltages in `configuration.py`. `00_cw.py` exposes `simulate`, `capture_camera` and `run_seconds`: `None` runs until Ctrl+C, while a number gives a finite run. Its `finally` block cancels its job and closes its QM. Stop an existing run before starting another on the same ports. Scripts do not close unrelated QMs.

## How the rearrangement runs

1. Python generates five loadings with three distinct occupied columns per row. Sorted source sites map to sorted destinations, so trajectories do not cross.
2. `with program() as rearrangement:` builds QUA instructions. `qm.execute(rearrangement)` compiles and uploads them to the OPX.
3. The OPX pauses while Python arms the camera. `job.resume()` starts all five cycles. The OPX generates and chirps the RF locally without host updates during motion.
4. One row tone and three column tones illuminate three spots. Each row gets 20 ms at its sources, 220 ms of linear frequency chirping, and approximately 20 ms at its destinations. One-microsecond half-Blackman edges bring the tones up and down.
5. The seven rows take 1.82 seconds. Seven row tones and three column tones then display the complete rectangle for 180 ms. Completed rows are not held illuminated while other rows move.
6. After five cycles, Python saves the camera stack and closes the QM. The header-free GIF shows the simulated 7×7 loading on the left, held fixed for each two-second shot, and the camera recording on the right with a fixed intensity scale. Filled cyan circles mark occupied sites; gray rings mark empty sites. The output is `rearrangement_split.gif`.

The motion uses small QUA lookup tables and loops, 2,000 arbitrary envelope samples and hardware oscillators/chirps; a long RF sample buffer is unnecessary. The full ten-second sequence compiled and ran successfully. Fully unrolled trials timed out during compilation; the compact loop version resolved that problem.

## Wiring and calibration

| Setting | Verified value |
|---|---|
| OPX address / software | `192.168.50.252`; QOP 3.8.1; `qm-qua 1.4.1` |
| Controller / FEM | `con1`, LF-FEM slot 3 |
| Output 1 | X; increasing frequency moves right in the camera |
| Output 2 | Y; increasing frequency moves down in the camera |
| Camera displacement | Approximately 17.94 X / 17.76 Y recorded pixels per MHz |
| Camera | Blackfly BFLY-PGE-50S5M, serial 25370031 |
| Recording | 160×160 ROI, 181.615 µs exposure, 0 dB gain, requested 45 fps |

The cropped camera reports a maximum of 45.84 fps in its current mode; measured recording rate was 45.08 fps. Acquisition is free-running. The GIF start offset is fitted from measured spot positions, not a hardware trigger.

`02_calibrate.py` measures 25 column frequencies along each of seven rows, holding the row tone at 1.45 mV peak. All 175 sampled points reached a 2.5% feedback tolerance; calibrated column tones span approximately 1.238–1.506 mV peak. Motion interpolates this map and changes amplitude every 20 ms. The final rectangle uses separate row/column factors plus feedback from the first recording. These ten amplitudes cannot independently control all 21 spot powers.

Calibration paths are pinned in `03_rearrange.py` so a new measurement does not silently change the experiment. Recalibrate after moving optics and explicitly select the new file. The calibration's absolute brightness reference drifted relative to the recordings; reported uniformity is relative to the measured group mean. Delivered RF power remains unmeasured.

## Camera installation and evidence

Spinnaker 4.4.0.246 and its matching Python 3.12 wheel are installed locally. On another Windows computer, install the native SDK, then its vendor wheel:

```powershell
uv pip install --python .venv/Scripts/python.exe C:/path/to/spinnaker_python-4.4.0.246-cp312-cp312-win_amd64.whl
```

Use `--inexact` so uv preserves this separately installed package. Wi-Fi and Ethernet sharing a subnet caused Spinnaker error −1023; disabling Wi-Fi resolved it on this computer.

Raw evidence is preserved under `../../results/opx_control/`:

- [Final recording](../../results/opx_control/20261022T015318_035554Z_rearrangement): raw `frames.tiff`, configuration/loadings/timestamps in `run.json`, `verification.json`, detections and GIF. There are 464 captured frames; 451 form the exactly 10,000 ms GIF. Brightness checks exclude exposures within 1 ms of row/rectangle transitions.
- [Brightness calibration](../../results/opx_control/20261022T013354_095955Z_brightness_calibration/calibration.json): 175 locations and raw calibration frames in the same folder.
- [First recording and rectangle feedback](../../results/opx_control/20261022T014139_082938Z_rearrangement): preserved separately from the corrected final capture.
- [Axis mapping](../../results/opx_control/20261022T013013_762983Z_axis_map/mapping.json).
- [Earlier 98 MHz first light](../../results/opx_control/20261022T012001_694961Z_cw98): 1.45 mV peak/channel produced 207/255 without saturation. That CW session has been stopped.

If this folder becomes a standalone repository, relocate the selected calibration files with it and change `results_path` in `configuration.py` to the desired data directory.

## Next steps

Prototype a framed serial occupancy link and validate trigger alignment, voltage levels, timing jitter and bit-error rate before selecting an external FPGA. A 50 Mbit/s payload is sufficient for the 20 ns/site candidate; the FPGA's camera interface and image-processing requirements must also guide selection.

Measure direct RF-chain power/spectrum and brightness repeatability. Improve residual rectangle nonuniformity before claiming ±5% across all spots. Faster atom-transport experiments need a separate trajectory and trapping test; this two-second sequence verifies visible optical motion.

**Design decisions — 21 October 2026:** Keep the flat QM configuration/experiment layout for readability. Use QUA loops and hardware chirps to execute all five loadings autonomously. Preserve calibration and recording evidence separately.

## Occupancy readout benchmark

```powershell
uv run --inexact python 05_readout_benchmark.py sliced --symbol-ns 20
uv run --inexact python 05_readout_benchmark.py multitone
```

These commands call `qmm.simulate()` only. A virtual source on FEM 2 feeds a virtual receiver on FEM 3; physical sessions and outputs are untouched. `--seed` selects a repeatable half-occupied pattern. Results, decoded bits and simulator waveform reports are timestamped under `results/opx_control/*_readout_simulation`.

| Method, 400 occupancy bits | Reception including gaps/guard | Conversion and ready output | Total |
|---|---:|---:|---:|
| Sliced, 20 ns/site | 8.020 µs | 5.240 µs | **13.260 µs** |
| Sliced, 40 ns/site | 16.040 µs | 5.240 µs | **21.280 µs** |
| Sliced, 100 ns/site | 40.100 µs | 5.240 µs | **45.340 µs** |
| 400 tones, eight I/Q receivers, 1 µs/window | 76.068 µs | 26.816 µs | **102.884 µs** |

All table rows recovered the same 400-bit pattern with exactly 200 occupied sites and zero errors. The endpoint is a conditional pulse depending on the count computed from the decoded array. Python fetches results afterward. A second seed independently verifies both the fastest sliced case and the I/Q multitone case. This demonstrates the direct QUA feedback path, not the runtime of a complete sorting algorithm.

The compiler accepts eight simultaneous I/Q frequencies and rejects nine; it accepts sixteen single-quadrature frequencies and rejects seventeen. These consume all 16 receiver FEM cores at 1 GS/s. The single-quadrature resource probe had bit errors without phase calibration and is **not** a validated decoder timing result. Output tones and readout share the core budget. A separate readout FEM may be appropriate if trapping tones must continue during reception. [QM core-allocation rules](https://docs.quantum-machines.co/latest/docs/Guides/features/#cores-and-oscillators).

The multitone signal uses one frequency per site at 1–400 MHz, a 0.3 V peak aggregate limit, and a continuous repeated comb while the receiver changes banks. Its fifty 1 µs observations take 50 µs alone; frequency updates, synchronization and result processing add time. The serial packet uses 0/0.2 V symbols and one trailing guard slice. Both use a 28 ns virtual loopback and calibrated 92 ns `time_of_flight`. Initial raw-input simulations stalled; loopback simulations completed. The simulator's final slice and amplitude scaling differed from the naive integral estimate, so the sliced receiver uses a guard and a calibrated threshold. These behaviors require bench verification; no simulator-specific calibration is a measured physical specification.

The comparison concerns these implementations, not a fundamental speed limit. It does not test equal bit-error rates under noise, source-FPGA encoding latency, oscillator drift, or an optimized hybrid frequency/time packet. For purchasing, favor a deterministic camera-processing FPGA and a simple framed serial output first; the tested 400-tone design would require a much wider-band DAC without giving lower latency.

[Benchmark comparison and raw-run references](../../results/opx_control/20261022T024317_287621Z_readout_comparison/comparison.json).
