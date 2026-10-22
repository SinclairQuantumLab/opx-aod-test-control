"""Input-stream move set: one packed set, tones off before/after, camera recording."""

from __future__ import annotations

import json
from pathlib import Path

from qm import QuantumMachinesManager
from qm.qua import *
from qm.qua import advance_input_stream, declare_input_stream

from amplitude_calibration import (
    amp_at_frequency_py,
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
from minimal_jerk import emit_requested_tone
from static_grid import apply_phase_setup

SCRIPT_NAME = "04_move_set_capture"
PLAN_PATH = PLANS_ROOT / "move_sets.json"

FLAG = 0
X_FREQ = 1
X_DET = X_FREQ + NUM_X_TWEEZERS
Y_FREQ = X_DET + NUM_X_TWEEZERS
Y_DET = Y_FREQ + NUM_Y_TWEEZERS
MOVE_SET_SIZE = Y_DET + NUM_Y_TWEEZERS  # 49


def load_move_sets(path: Path = PLAN_PATH) -> list[dict]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return list(payload["sets"])


def validate_move_sets(
    sets: list[dict],
    *,
    x_amp_calibration=None,
    y_amp_calibration=None,
) -> None:
    if not sets:
        raise ValueError("move_sets plan is empty")

    for set_index, move_set in enumerate(sets):
        flag = int(move_set["flag"])
        if flag not in (0, 1):
            raise ValueError(f"set {set_index} flag={flag} must be 0 or 1")
        if set_index < len(sets) - 1 and flag != 1:
            raise ValueError(f"set {set_index} flag={flag}; non-final sets must have flag=1")
        if set_index == len(sets) - 1 and flag != 0:
            raise ValueError(f"final set flag={flag}; must be 0 to exit the wait loop")

        for key, n in (
            ("x_freq", NUM_X_TWEEZERS),
            ("x_det", NUM_X_TWEEZERS),
            ("y_freq", NUM_Y_TWEEZERS),
            ("y_det", NUM_Y_TWEEZERS),
        ):
            if len(move_set[key]) != n:
                raise ValueError(f"set {set_index} {key} length {len(move_set[key])} != {n}")

        for axis, freqs, dets, calibration in (
            ("x", move_set["x_freq"], move_set["x_det"], x_amp_calibration),
            ("y", move_set["y_freq"], move_set["y_det"], y_amp_calibration),
        ):
            for tone_index, (freq, det) in enumerate(zip(freqs, dets)):
                if int(freq) <= 0:
                    continue
                start_hz = float(freq)
                end_hz = float(freq + det)
                if calibration is None or calibration.latency_only or not calibration.coefs:
                    continue
                for label, hz in (("start", start_hz), ("end", end_hz)):
                    mult = float(
                        amp_at_frequency_py(hz / 1e9, calibration.center, calibration.coefs)
                    )
                    if mult < 0.0 or mult > 2.0:
                        raise ValueError(
                            f"set {set_index} {axis}[{tone_index}] {label} "
                            f"{hz / 1e6:.3f} MHz → mult={mult:.4f} outside [0, 2]"
                        )

    print(f"Move-set plan OK ({len(sets)} sets, packed size={MOVE_SET_SIZE})")


def pack_move_set(move_set: dict) -> list[int]:
    packed = [0] * MOVE_SET_SIZE
    packed[FLAG] = int(move_set["flag"])
    packed[X_FREQ : X_FREQ + NUM_X_TWEEZERS] = [int(v) for v in move_set["x_freq"]]
    packed[X_DET : X_DET + NUM_X_TWEEZERS] = [int(v) for v in move_set["x_det"]]
    packed[Y_FREQ : Y_FREQ + NUM_Y_TWEEZERS] = [int(v) for v in move_set["y_freq"]]
    packed[Y_DET : Y_DET + NUM_Y_TWEEZERS] = [int(v) for v in move_set["y_det"]]
    return packed


def requested_elements_for_set(move_set: dict) -> list[str]:
    names: list[str] = []
    for index, freq in enumerate(move_set["x_freq"]):
        if int(freq) > 0:
            names.append(x_tweezer_elements[index])
    for index, freq in enumerate(move_set["y_freq"]):
        if int(freq) > 0:
            names.append(y_tweezer_elements[index])
    return names


def build_program(
    x_amp_calibration, y_amp_calibration, chirp_segments: int, *, with_pause: bool = True
):
    with program() as prog:
        move_set = declare_input_stream(int, name="move_set", size=MOVE_SET_SIZE)
        keep_waiting = declare(bool, value=True)

        if with_pause:
            pause()

        apply_phase_setup()

        with while_(keep_waiting):
            advance_input_stream(move_set)
            play("trigger", "scope_trigger")

            for index, element in enumerate(x_tweezer_elements):
                emit_requested_tone(
                    element,
                    move_set[X_FREQ + index],
                    move_set[X_DET + index],
                    segment_length,
                    chirp_segments,
                    x_amp_calibration,
                )
            for index, element in enumerate(y_tweezer_elements):
                emit_requested_tone(
                    element,
                    move_set[Y_FREQ + index],
                    move_set[Y_DET + index],
                    segment_length,
                    chirp_segments,
                    y_amp_calibration,
                )

            align(*all_tweezer_elements)
            assign(keep_waiting, ~(move_set[FLAG] == 0))

    return prog


def build_program_compile_time(
    sets: list[dict],
    x_amp_calibration,
    y_amp_calibration,
    chirp_segments: int,
    *,
    with_pause: bool = True,
):
    """Compile-time equivalent for simulator (no input streams)."""
    packed_all = [v for move_set in sets for v in pack_move_set(move_set)]
    n_sets = len(sets)

    with program() as prog:
        packed = declare(int, value=packed_all)
        set_index = declare(int)
        slot = declare(int)
        flag = declare(int)
        x_freq = declare(int, size=NUM_X_TWEEZERS)
        x_det = declare(int, size=NUM_X_TWEEZERS)
        y_freq = declare(int, size=NUM_Y_TWEEZERS)
        y_det = declare(int, size=NUM_Y_TWEEZERS)

        if with_pause:
            pause()

        apply_phase_setup()

        with for_(set_index, 0, set_index < n_sets, set_index + 1):
            assign(slot, set_index * MOVE_SET_SIZE)
            assign(flag, packed[slot + FLAG])
            for i in range(NUM_X_TWEEZERS):
                assign(x_freq[i], packed[slot + X_FREQ + i])
                assign(x_det[i], packed[slot + X_DET + i])
            for i in range(NUM_Y_TWEEZERS):
                assign(y_freq[i], packed[slot + Y_FREQ + i])
                assign(y_det[i], packed[slot + Y_DET + i])
            play("trigger", "scope_trigger")
            for index, element in enumerate(x_tweezer_elements):
                emit_requested_tone(
                    element,
                    x_freq[index],
                    x_det[index],
                    segment_length,
                    chirp_segments,
                    x_amp_calibration,
                )
            for index, element in enumerate(y_tweezer_elements):
                emit_requested_tone(
                    element,
                    y_freq[index],
                    y_det[index],
                    segment_length,
                    chirp_segments,
                    y_amp_calibration,
                )
            align(*all_tweezer_elements)

    return prog


def move_duration_s(chirp_segments: int | None = None) -> float:
    n = number_of_segments if chirp_segments is None else chirp_segments
    return (rampup_length + n * segment_length + rampdown_length) * 1e-9


def _push_move_sets(job, sets: list[dict]) -> None:
    for set_index, move_set in enumerate(sets):
        packed = pack_move_set(move_set)
        job.push_to_input_stream("move_set", packed)
        print(
            f"Pushed set {set_index}: flag={packed[FLAG]}  "
            f"requested={len(requested_elements_for_set(move_set))}"
        )


def main() -> None:
    from capture import run_with_camera

    sets = load_move_sets()
    x_amp_calibration, y_amp_calibration = load_xy_amp_calibration(
        CALIBRATION_ROOT, latency_only=True, module_name=SCRIPT_NAME
    )
    validate_xy_compensation_range(
        x_amp_calibration, y_amp_calibration, x_tweezer_IFs, y_tweezer_IFs
    )
    validate_move_sets(
        sets, x_amp_calibration=x_amp_calibration, y_amp_calibration=y_amp_calibration
    )

    chirp_segments = number_of_segments
    prog = build_program(x_amp_calibration, y_amp_calibration, chirp_segments)

    duration_s = len(sets) * move_duration_s(chirp_segments) + 0.5
    print(
        f"Mode=hardware  sets={len(sets)}  packed_size={MOVE_SET_SIZE}  "
        f"segments={chirp_segments}  duration_s≈{duration_s:.1f}"
    )

    qmm_kwargs = {"host": qop_ip, "cluster_name": cluster_name, "timeout": 600}
    if qop_port is not None:
        qmm_kwargs["port"] = qop_port
    qmm = QuantumMachinesManager(**qmm_kwargs)

    record = {
        "script": SCRIPT_NAME,
        "sets": sets,
        "segment_length": segment_length,
        "number_of_segments": chirp_segments,
        "cycle_seconds": move_duration_s(chirp_segments),
    }

    def before_resume(job):
        _push_move_sets(job, sets)

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

    qm = qmm.open_qm(config, close_other_machines=True)
    run_with_camera(
        qm=qm,
        prog=prog,
        label="move_set",
        duration_s=duration_s,
        record=record,
        before_resume=before_resume,
        after_recording=after_recording,
    )


if __name__ == "__main__":
    main()
