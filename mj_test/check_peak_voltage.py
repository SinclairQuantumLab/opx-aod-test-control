"""Offline safety check: port amplitude guard + QOP simulations.

Verifies FEM2 port 4 and FEM3 port 4 stay below 10 mV peak and that no other
analog ports produce output.

Simulator notes (QOP 3.8 / qm-qua 1.3.1):
- ``timestamp_stream`` is stripped (known hang).
- Full 12-mover / 24-tone chirp programs are too slow to simulate end-to-end;
  we use an all-on coherent-sum probe plus a short gate+2-mover chirp probe.
- Full-length programs are checked with ``generate_qua_script``.
"""

from __future__ import annotations

import contextlib
import json
import sys
import time
from pathlib import Path

import numpy as np
from qm import QuantumMachinesManager, SimulationConfig
from qm.qua import play, program
from qm.qua import play as _real_play

from amplitude_calibration import load_xy_amp_calibration
from configuration import (
    CALIBRATION_ROOT,
    MAX_PORT_PEAK_V,
    TONE_AMPLITUDE_V,
    X_FEMS,
    Y_FEMS,
    all_tweezer_elements,
    assert_port_peak_safe,
    cluster_name,
    config,
    number_of_segments,
    qop_ip,
    qop_port,
    rampdown_length,
    rampup_length,
    segment_length,
    x_per_fem,
    x_tweezer_elements,
    y_per_fem,
    y_tweezer_elements,
)

SHORT_SEGMENTS = 4
ALL_ON_SIM_CC = 50_000
CHIRP_SIM_CC = 250_000
SIM_TIMEOUT_S = 180


def strip_timestamp_streams():
    import minimal_jerk as mj

    def play_no_ts(*args, **kwargs):
        kwargs.pop("timestamp_stream", None)
        return _real_play(*args, **kwargs)

    @contextlib.contextmanager
    def _ctx():
        old = mj.play
        mj.play = play_no_ts
        try:
            yield
        finally:
            mj.play = old

    return _ctx()


def static_guards() -> dict:
    assert_port_peak_safe(max(x_per_fem), TONE_AMPLITUDE_V)
    assert_port_peak_safe(max(y_per_fem), TONE_AMPLITUDE_V)
    x_peak = max(x_per_fem) * TONE_AMPLITUDE_V
    y_peak = max(y_per_fem) * TONE_AMPLITUDE_V
    return {
        "x_tones": max(x_per_fem),
        "y_tones": max(y_per_fem),
        "tone_v": TONE_AMPLITUDE_V,
        "x_coherent_peak_v": x_peak,
        "y_coherent_peak_v": y_peak,
        "limit_v": MAX_PORT_PEAK_V,
        "ok": x_peak < MAX_PORT_PEAK_V and y_peak < MAX_PORT_PEAK_V,
    }


def _analog_port_peaks(samples) -> dict[str, float]:
    peaks: dict[str, float] = {}
    analog = getattr(samples.con1, "analog", None)
    if analog is None:
        return peaks
    for key, wave in analog.items():
        arr = np.asarray(wave, dtype=float)
        peaks[str(key)] = float(np.max(np.abs(arr))) if arr.size else 0.0
    return peaks


def evaluate_peaks(peaks: dict[str, float]) -> dict:
    x_fem, x_port = X_FEMS[0][1], X_FEMS[0][2]
    y_fem, y_port = Y_FEMS[0][1], Y_FEMS[0][2]
    aod_peaks: dict[str, float] = {}
    other_peaks: dict[str, float] = {}
    for key, value in peaks.items():
        if key == f"{x_fem}-{x_port}":
            aod_peaks[f"x:{key}"] = value
        elif key == f"{y_fem}-{y_port}":
            aod_peaks[f"y:{key}"] = value
        else:
            other_peaks[key] = value
    max_aod = max(aod_peaks.values()) if aod_peaks else 0.0
    max_other = max(other_peaks.values()) if other_peaks else 0.0
    return {
        "aod_peaks_v": aod_peaks,
        "other_peaks_v": other_peaks,
        "max_aod_v": max_aod,
        "max_other_v": max_other,
        "ok": bool(max_aod < MAX_PORT_PEAK_V and max_other < 1e-6 and bool(aod_peaks)),
        "raw_peaks_v": peaks,
    }


def build_all_on_probe():
    """All 24 tones ramp up — worst-case coherent sum on each AOD port."""
    with program() as prog:
        for element in x_tweezer_elements + y_tweezer_elements:
            play("rampup", element)
    return prog


def build_chirp_probe(x_cal, y_cal):
    """One gate + two X movers (short segments); no align-all (sim speed)."""
    from minimal_jerk import play_gate_hold, play_minimal_jerk_chirp

    with strip_timestamp_streams():
        with program() as prog:
            play_gate_hold(
                y_tweezer_elements[0],
                SHORT_SEGMENTS * segment_length,
                int(95_000_000),
                y_cal,
            )
            play_minimal_jerk_chirp(
                x_tweezer_elements[0],
                95_000_000,
                163_636,
                segment_length,
                SHORT_SEGMENTS,
                x_cal,
            )
            play_minimal_jerk_chirp(
                x_tweezer_elements[1],
                95_545_454,
                -163_636,
                segment_length,
                SHORT_SEGMENTS,
                x_cal,
            )
    return prog


def build_full_programs():
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    x_cal, y_cal = load_xy_amp_calibration(
        CALIBRATION_ROOT, latency_only=True, module_name="02_row_sort_capture"
    )
    import importlib as _il

    plans = Path(__file__).resolve().parent / "plans"
    if not (plans / "row_sort_plan.json").exists():
        from make_plans import main as make_plans_main

        make_plans_main()

    row_mod = _il.import_module("02_row_sort_capture")
    stream_mod = _il.import_module("03_streamed_move_capture")
    set_mod = _il.import_module("04_move_set_capture")
    static_mod = _il.import_module("01_static_image")

    plan = row_mod.load_plan()
    steps = stream_mod.load_stream_plan()
    sets = set_mod.load_move_sets()

    with strip_timestamp_streams():
        return {
            "01_static_image": static_mod.build_program(
                x_cal, y_cal, with_pause=False, hold_seconds=200e-6
            ),
            "02_row_sort_capture": row_mod.build_program(
                plan,
                num_rows=1,
                x_amp_calibration=x_cal,
                y_amp_calibration=y_cal,
                with_pause=False,
            ),
            "03_streamed_move_capture": stream_mod.build_program_compile_time(
                steps, x_cal, y_cal, chirp_segments=number_of_segments, with_pause=False
            ),
            "04_move_set_capture": set_mod.build_program_compile_time(
                sets, x_cal, y_cal, chirp_segments=number_of_segments, with_pause=False
            ),
        }


def simulate_program(qmm, name: str, prog, duration_cc: int) -> dict:
    print(f"Simulating {name} (duration_cc={duration_cc})...")
    t0 = time.time()
    job = qmm.simulate(config, prog, SimulationConfig(duration=duration_cc))
    job.wait_until("Done", timeout=SIM_TIMEOUT_S)
    peaks = _analog_port_peaks(job.get_simulated_samples())
    evaluation = evaluate_peaks(peaks)
    evaluation["script"] = name
    evaluation["sim_seconds"] = time.time() - t0
    print(
        f"  {evaluation['sim_seconds']:.1f}s  "
        f"max_aod={evaluation['max_aod_v']*1e3:.4f} mV  "
        f"max_other={evaluation['max_other_v']*1e3:.4f} mV  "
        f"ok={evaluation['ok']}  keys={list(peaks.keys())}"
    )
    return evaluation


def compile_program(name: str, prog) -> dict:
    print(f"Serializing full-length {name}...")
    try:
        from qm import generate_qua_script

        script = generate_qua_script(prog, config)
        text = script if isinstance(script, str) else str(script)
        n_chars = len(text)
        has_nhz = "nHz/nsec" in text or "nHzPerNanoSec" in text
        print(f"  ok  chars={n_chars}  nHz_units_mentioned={has_nhz}")
        return {
            "script": name,
            "status": "ok",
            "chars": n_chars,
            "nHz_units_mentioned": has_nhz,
        }
    except Exception as exc:
        print(f"  error: {exc}")
        return {"script": name, "status": "error", "error": str(exc)}


def main() -> int:
    report = {
        "segment_length_ns": segment_length,
        "number_of_segments_full": number_of_segments,
        "short_segments": SHORT_SEGMENTS,
        "rampup_ns": rampup_length,
        "rampdown_ns": rampdown_length,
        "ports": {"x": list(X_FEMS), "y": list(Y_FEMS)},
        "row_hold_pulse_length_ns": config["pulses"]["row_hold_pulse"]["length"],
    }

    report["static_guard"] = static_guards()
    print("Static guard:", json.dumps(report["static_guard"], indent=2))
    if not report["static_guard"]["ok"]:
        print("FAIL: static port peak guard")
        return 1

    qmm_kwargs = {"host": qop_ip, "cluster_name": cluster_name, "timeout": 300}
    if qop_port is not None:
        qmm_kwargs["port"] = qop_port
    qmm = QuantumMachinesManager(**qmm_kwargs)
    # Do not close other users' QMs; only proceed with simulate.

    x_cal, y_cal = load_xy_amp_calibration(
        CALIBRATION_ROOT, latency_only=True, module_name="02_row_sort_capture"
    )

    sim_results = [
        simulate_program(qmm, "all_on_probe", build_all_on_probe(), ALL_ON_SIM_CC),
        simulate_program(qmm, "chirp_gate_2movers", build_chirp_probe(x_cal, y_cal), CHIRP_SIM_CC),
    ]
    report["simulations"] = sim_results

    full = build_full_programs()
    compile_results = [compile_program(name, prog) for name, prog in full.items()]

    # Explicit pause() coverage (full builders used with_pause=False for sim speed).
    import importlib as _il

    x_cal2, y_cal2 = load_xy_amp_calibration(
        CALIBRATION_ROOT, latency_only=True, module_name="02_row_sort_capture"
    )
    row_mod = _il.import_module("02_row_sort_capture")
    with strip_timestamp_streams():
        prog_pause = row_mod.build_program(
            row_mod.load_plan(),
            num_rows=1,
            x_amp_calibration=x_cal2,
            y_amp_calibration=y_cal2,
            chirp_segments=SHORT_SEGMENTS,
            hold_length_ns=SHORT_SEGMENTS * segment_length,
            move_elements=x_tweezer_elements[:2],
            with_pause=True,
        )
    pause_result = compile_program("02_with_pause", prog_pause)
    try:
        from qm import generate_qua_script

        pause_text = generate_qua_script(prog_pause, config)
        pause_result["pause_keyword_present"] = "pause" in pause_text.lower()
    except Exception as exc:
        pause_result["pause_keyword_present"] = False
        pause_result["pause_check_error"] = str(exc)
    compile_results.append(pause_result)

    report["compiles"] = compile_results
    report["serialized_diff_note"] = (
        "assert_programs_are_equal unavailable on qm-qua 1.3.1; used generate_qua_script. "
        "Expected diffs vs top-level: ports FEM2p4/FEM3p4, IFs 95-101 MHz, 0.8 mV, "
        "50 us x 20000, 50 us ramps, nHz/nsec, integer segment counter, pause(). "
        "Pause coverage: see compile entry 02_with_pause."
    )
    report["sim_coverage_note"] = (
        "Full 12-mover / 24-tone chirp programs are too slow for this QOP simulator; "
        "all_on_probe covers worst-case coherent sum; chirp_gate_2movers covers the "
        "MJ + gate path at short segment count."
    )

    sim_ok = all(r["ok"] for r in sim_results)
    compile_ok = all(r["status"] == "ok" for r in compile_results)
    report["ok"] = bool(report["static_guard"]["ok"] and sim_ok and compile_ok)

    out = Path(__file__).resolve().parent / "results" / "peak_voltage_check.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(report, indent=2))
    print(json.dumps({"ok": report["ok"], "wrote": str(out)}, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
