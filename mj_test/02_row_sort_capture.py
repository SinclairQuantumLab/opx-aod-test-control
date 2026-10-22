"""Row-by-row sort from a fixed plan with camera recording across all 12 rows."""

from __future__ import annotations

import json
from pathlib import Path

from qm import QuantumMachinesManager
from qm.qua import *

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
    chirp_pulse_length,
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
    x_tweezer_phases,
    y_tweezer_IFs,
    y_tweezer_elements,
    y_tweezer_phases,
)
from minimal_jerk import emit_move_chirp_if_active, play_gate_hold

SCRIPT_NAME = "02_row_sort_capture"
NUM_ROWS = 12
PLAN_PATH = PLANS_ROOT / "row_sort_plan.json"


def load_plan(path: Path = PLAN_PATH) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    n = NUM_X_TWEEZERS
    if len(payload["gate_freq"]) != NUM_Y_TWEEZERS:
        raise ValueError("gate_freq length mismatch")
    if len(payload["tweezer_freq"]) != n or len(payload["tweezer_detuning"]) != n:
        raise ValueError("tweezer grid row count mismatch")
    for row in payload["tweezer_freq"]:
        if len(row) != n:
            raise ValueError("tweezer_freq row width mismatch")
    for row in payload["tweezer_detuning"]:
        if len(row) != n:
            raise ValueError("tweezer_detuning row width mismatch")
    return payload


def row_duration_s(
    *,
    chirp_segments: int | None = None,
    hold_length_ns: int | None = None,
) -> float:
    n_segments = number_of_segments if chirp_segments is None else chirp_segments
    hold_ns = chirp_pulse_length if hold_length_ns is None else hold_length_ns
    # Gate hold covers the full chirp window; movers: rampup + segments + rampdown.
    move_ns = rampup_length + n_segments * segment_length + rampdown_length
    return max(hold_ns, move_ns) * 1e-9


def build_program(
    plan: dict,
    *,
    num_rows: int,
    x_amp_calibration,
    y_amp_calibration,
    chirp_segments: int | None = None,
    hold_length_ns: int | None = None,
    move_elements: list[str] | None = None,
    with_pause: bool = True,
):
    gate_freq_py = [int(v) for v in plan["gate_freq"][:num_rows]]
    tweezer_freq_flat = [int(v) for row in plan["tweezer_freq"][:num_rows] for v in row]
    tweezer_det_flat = [int(v) for row in plan["tweezer_detuning"][:num_rows] for v in row]
    n_segments = number_of_segments if chirp_segments is None else chirp_segments
    hold_ns = chirp_pulse_length if hold_length_ns is None else hold_length_ns
    movers = list(x_tweezer_elements if move_elements is None else move_elements)

    with program() as prog:
        gate_freq = declare(int, value=gate_freq_py)
        tweezer_freq = declare(int, value=tweezer_freq_flat)
        tweezer_detuning = declare(int, value=tweezer_det_flat)
        row_index = declare(int)

        if with_pause:
            pause()

        for index, element in enumerate(x_tweezer_elements):
            frame_rotation_2pi(float(x_tweezer_phases[index]), element)
        for index, element in enumerate(y_tweezer_elements):
            frame_rotation_2pi(float(y_tweezer_phases[index]), element)

        with for_(row_index, 0, row_index < num_rows, row_index + 1):
            play("trigger", "scope_trigger")
            play_gate_hold(
                y_tweezer_elements[0],
                hold_ns,
                gate_freq[row_index],
                y_amp_calibration,
            )
            for move_index, element in enumerate(movers):
                emit_move_chirp_if_active(
                    element,
                    tweezer_freq[row_index * NUM_X_TWEEZERS + move_index],
                    tweezer_detuning[row_index * NUM_X_TWEEZERS + move_index],
                    segment_length,
                    n_segments,
                    x_amp_calibration,
                )
            align(*all_tweezer_elements)

    return prog


def main() -> None:
    from capture import run_with_camera

    plan = load_plan()
    num_rows = NUM_ROWS

    x_amp_calibration, y_amp_calibration = load_xy_amp_calibration(
        CALIBRATION_ROOT,
        latency_only=True,
        module_name=SCRIPT_NAME,
    )
    validate_xy_compensation_range(
        x_amp_calibration, y_amp_calibration, x_tweezer_IFs, y_tweezer_IFs
    )

    chirp_segments = number_of_segments
    hold_ns = chirp_pulse_length
    movers = x_tweezer_elements

    prog = build_program(
        plan,
        num_rows=num_rows,
        x_amp_calibration=x_amp_calibration,
        y_amp_calibration=y_amp_calibration,
        chirp_segments=chirp_segments,
        hold_length_ns=hold_ns,
        move_elements=movers,
    )

    duration_s = num_rows * row_duration_s(chirp_segments=chirp_segments, hold_length_ns=hold_ns)
    print(
        f"Mode=hardware  rows={num_rows}  movers={len(movers)}  "
        f"segments={chirp_segments}  duration_s≈{duration_s:.1f}"
    )

    qmm_kwargs = {"host": qop_ip, "cluster_name": cluster_name, "timeout": 600}
    if qop_port is not None:
        qmm_kwargs["port"] = qop_port
    qmm = QuantumMachinesManager(**qmm_kwargs)

    record = {
        "script": SCRIPT_NAME,
        "plan": plan,
        "num_rows": num_rows,
        "segment_length": segment_length,
        "number_of_segments": chirp_segments,
        "cycle_seconds": row_duration_s(chirp_segments=chirp_segments, hold_length_ns=hold_ns),
    }

    def after_recording(job, folder):
        try:
            check_tweezer_computation_latency(
                job,
                movers,
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
        label="row_sort",
        duration_s=duration_s + 0.5,
        record=record,
        after_recording=after_recording,
    )


if __name__ == "__main__":
    main()
