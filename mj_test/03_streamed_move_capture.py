"""Live 12x12 grid: stream one move step and record the camera."""

from __future__ import annotations

import json
from pathlib import Path

from qm import QuantumMachinesManager
from qm.qua import *
from qm.qua import advance_input_stream, declare_input_stream

from amplitude_calibration import (
    check_tweezer_computation_latency,
    load_xy_amp_calibration,
    validate_xy_compensation_range,
)
from configuration import (
    CALIBRATION_ROOT,
    NUM_X_TWEEZERS,
    NUM_Y_TWEEZERS,
    PLANS_ROOT,
    all_tweezer_elements,
    cluster_name,
    config,
    number_of_segments,
    qop_ip,
    qop_port,
    rampdown_length,
    rampup_length,
    segment_length,
    x_tweezer_IFs,
    x_tweezer_elements,
    y_tweezer_IFs,
    y_tweezer_elements,
)
from minimal_jerk import emit_live_chirp_if_active
from static_grid import initial_amp_values, ramp_down_grid, ramp_up_grid

SCRIPT_NAME = "03_streamed_move_capture"
PLAN_PATH = PLANS_ROOT / "stream_moves.json"


def load_stream_plan(path: Path = PLAN_PATH) -> list[dict]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    steps = payload["steps"]
    for step_index, step in enumerate(steps):
        for key, n in (
            ("x_freq", NUM_X_TWEEZERS),
            ("x_det", NUM_X_TWEEZERS),
            ("y_freq", NUM_Y_TWEEZERS),
            ("y_det", NUM_Y_TWEEZERS),
        ):
            if len(step[key]) != n:
                raise ValueError(f"step {step_index} {key} length {len(step[key])} != {n}")
    return steps


def validate_stream_continuity(steps: list[dict]) -> None:
    cur_x = x_tweezer_IFs.astype(int).tolist()
    cur_y = y_tweezer_IFs.astype(int).tolist()
    for step_index, step in enumerate(steps):
        for i, (freq, expected) in enumerate(zip(step["x_freq"], cur_x)):
            if freq != 0 and freq != expected:
                raise ValueError(
                    f"step {step_index} x_freq[{i}]={freq} != current {expected}"
                )
        for i, (freq, expected) in enumerate(zip(step["y_freq"], cur_y)):
            if freq != 0 and freq != expected:
                raise ValueError(
                    f"step {step_index} y_freq[{i}]={freq} != current {expected}"
                )
        for i, (freq, det) in enumerate(zip(step["x_freq"], step["x_det"])):
            if freq != 0 and det != 0:
                cur_x[i] = freq + det
        for i, (freq, det) in enumerate(zip(step["y_freq"], step["y_det"])):
            if freq != 0 and det != 0:
                cur_y[i] = freq + det
    print(f"Stream continuity OK ({len(steps)} steps)")


def _declare_live_amplitudes(x_amps_py, y_amps_py):
    x_amp = [declare(fixed, value=float(v)) for v in x_amps_py]
    y_amp = [declare(fixed, value=float(v)) for v in y_amps_py]
    return x_amp, y_amp


def _play_one_step(
    x_freq,
    x_det,
    y_freq,
    y_det,
    x_amp,
    y_amp,
    x_amp_calibration,
    y_amp_calibration,
    chirp_segments: int,
) -> None:
    for index, element in enumerate(x_tweezer_elements):
        emit_live_chirp_if_active(
            element,
            x_freq[index],
            x_det[index],
            x_amp[index],
            segment_length,
            chirp_segments,
            x_amp_calibration,
        )
    for index, element in enumerate(y_tweezer_elements):
        emit_live_chirp_if_active(
            element,
            y_freq[index],
            y_det[index],
            y_amp[index],
            segment_length,
            chirp_segments,
            y_amp_calibration,
        )
    align(*all_tweezer_elements)


def build_program_streamed(
    x_amp_calibration, y_amp_calibration, chirp_segments: int, *, with_pause: bool = True
):
    x_amps_py, y_amps_py = initial_amp_values(x_amp_calibration, y_amp_calibration)

    with program() as prog:
        step_flag = declare_input_stream(int, name="step_flag", size=1)
        x_freq = declare_input_stream(int, size=NUM_X_TWEEZERS, name="x_freq")
        x_det = declare_input_stream(int, size=NUM_X_TWEEZERS, name="x_det")
        y_freq = declare_input_stream(int, size=NUM_Y_TWEEZERS, name="y_freq")
        y_det = declare_input_stream(int, size=NUM_Y_TWEEZERS, name="y_det")
        x_amp, y_amp = _declare_live_amplitudes(x_amps_py, y_amps_py)
        flag = declare(int)
        done = declare(bool, value=False)

        if with_pause:
            pause()

        ramp_up_grid(x_amp_calibration, y_amp_calibration, x_amp=x_amp, y_amp=y_amp)

        with while_(~done):
            advance_input_stream(step_flag)
            assign(flag, step_flag[0])
            with if_(flag == 0):
                ramp_down_grid(x_amp=x_amp, y_amp=y_amp)
                assign(done, True)
            with else_():
                advance_input_stream(x_freq)
                advance_input_stream(x_det)
                advance_input_stream(y_freq)
                advance_input_stream(y_det)
                play("trigger", "scope_trigger")
                _play_one_step(
                    x_freq,
                    x_det,
                    y_freq,
                    y_det,
                    x_amp,
                    y_amp,
                    x_amp_calibration,
                    y_amp_calibration,
                    chirp_segments,
                )

    return prog


def build_program_compile_time(
    steps: list[dict],
    x_amp_calibration,
    y_amp_calibration,
    chirp_segments: int,
    *,
    with_pause: bool = True,
):
    """Compile-time equivalent for simulator (no input streams)."""
    x_amps_py, y_amps_py = initial_amp_values(x_amp_calibration, y_amp_calibration)
    n_steps = len(steps)

    x_freq_flat = [int(v) for step in steps for v in step["x_freq"]]
    x_det_flat = [int(v) for step in steps for v in step["x_det"]]
    y_freq_flat = [int(v) for step in steps for v in step["y_freq"]]
    y_det_flat = [int(v) for step in steps for v in step["y_det"]]

    with program() as prog:
        x_freq_all = declare(int, value=x_freq_flat)
        x_det_all = declare(int, value=x_det_flat)
        y_freq_all = declare(int, value=y_freq_flat)
        y_det_all = declare(int, value=y_det_flat)
        x_amp, y_amp = _declare_live_amplitudes(x_amps_py, y_amps_py)
        step_index = declare(int)
        x_freq = declare(int, size=NUM_X_TWEEZERS)
        x_det = declare(int, size=NUM_X_TWEEZERS)
        y_freq = declare(int, size=NUM_Y_TWEEZERS)
        y_det = declare(int, size=NUM_Y_TWEEZERS)
        slot = declare(int)

        if with_pause:
            pause()

        ramp_up_grid(x_amp_calibration, y_amp_calibration, x_amp=x_amp, y_amp=y_amp)

        with for_(step_index, 0, step_index < n_steps, step_index + 1):
            for i in range(NUM_X_TWEEZERS):
                assign(slot, step_index * NUM_X_TWEEZERS + i)
                assign(x_freq[i], x_freq_all[slot])
                assign(x_det[i], x_det_all[slot])
            for i in range(NUM_Y_TWEEZERS):
                assign(slot, step_index * NUM_Y_TWEEZERS + i)
                assign(y_freq[i], y_freq_all[slot])
                assign(y_det[i], y_det_all[slot])
            play("trigger", "scope_trigger")
            _play_one_step(
                x_freq,
                x_det,
                y_freq,
                y_det,
                x_amp,
                y_amp,
                x_amp_calibration,
                y_amp_calibration,
                chirp_segments,
            )

        ramp_down_grid(x_amp=x_amp, y_amp=y_amp)

    return prog


def move_duration_s(chirp_segments: int | None = None) -> float:
    n = number_of_segments if chirp_segments is None else chirp_segments
    # rampup (grid) + chirp window + rampdown
    return (rampup_length + n * segment_length + rampdown_length) * 1e-9


def _push_steps(job, steps: list[dict]) -> None:
    for step in steps:
        job.push_to_input_stream("step_flag", [1])
        job.push_to_input_stream("x_freq", step["x_freq"])
        job.push_to_input_stream("x_det", step["x_det"])
        job.push_to_input_stream("y_freq", step["y_freq"])
        job.push_to_input_stream("y_det", step["y_det"])
    job.push_to_input_stream("step_flag", [0])


def main() -> None:
    from capture import run_with_camera

    steps = load_stream_plan()
    validate_stream_continuity(steps)

    x_amp_calibration, y_amp_calibration = load_xy_amp_calibration(
        CALIBRATION_ROOT,
        latency_only=True,
        module_name=SCRIPT_NAME,
    )
    validate_xy_compensation_range(
        x_amp_calibration, y_amp_calibration, x_tweezer_IFs, y_tweezer_IFs
    )

    chirp_segments = number_of_segments
    prog = build_program_streamed(
        x_amp_calibration, y_amp_calibration, chirp_segments=chirp_segments
    )

    duration_s = move_duration_s(chirp_segments) + 0.5
    print(f"Executing input-stream program  steps={len(steps)}  duration_s≈{duration_s:.1f}")

    qmm_kwargs = {"host": qop_ip, "cluster_name": cluster_name, "timeout": 600}
    if qop_port is not None:
        qmm_kwargs["port"] = qop_port
    qmm = QuantumMachinesManager(**qmm_kwargs)

    record = {
        "script": SCRIPT_NAME,
        "steps": steps,
        "segment_length": segment_length,
        "number_of_segments": chirp_segments,
        "cycle_seconds": move_duration_s(chirp_segments),
    }

    def before_resume(job):
        _push_steps(job, steps)

    def after_recording(job, folder):
        try:
            check_tweezer_computation_latency(
                job,
                all_tweezer_elements,
                segment_length,
                x_amp_calibration,
                y_amp_calibration,
                directory=CALIBRATION_ROOT,
                module_name=SCRIPT_NAME,
            )
        except Exception as exc:
            print(f"Latency check skipped/failed: {exc}")

    qm = qmm.open_qm(config, close_other_machines=False)
    run_with_camera(
        qm=qm,
        prog=prog,
        label="streamed_move",
        duration_s=duration_s,
        record=record,
        before_resume=before_resume,
        after_recording=after_recording,
    )


if __name__ == "__main__":
    main()
