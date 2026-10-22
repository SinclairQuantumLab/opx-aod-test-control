#!/usr/bin/env python3
"""Generate camera-capture plans for the compressed 95–101 MHz 12x12 grid."""

from __future__ import annotations

import json

from configuration import (
    IF_SPACING_HZ,
    NUM_X_TWEEZERS,
    NUM_Y_TWEEZERS,
    PLANS_ROOT,
    x_tweezer_IFs,
    y_tweezer_IFs,
)

NEIGHBOR_PULL_FRACTION = 0.3
# Half-spacing moves so a tone never lands on a neighbour frequency.
STREAM_DETUNING_HZ = int(round(0.5 * IF_SPACING_HZ))


def make_row_sort_plan(num_tweezers: int = NUM_X_TWEEZERS) -> dict:
    """Neighbor-pull pattern: even columns close toward the right neighbour."""
    assert num_tweezers == NUM_Y_TWEEZERS
    col_ifs = x_tweezer_IFs.astype(int)
    row_ifs = y_tweezer_IFs.astype(int)
    spacing = int(col_ifs[1] - col_ifs[0]) if num_tweezers > 1 else 0
    pull = int(round(NEIGHBOR_PULL_FRACTION * spacing))

    gate_freq = [int(f) for f in row_ifs]
    tweezer_freq: list[list[int]] = []
    tweezer_detuning: list[list[int]] = []

    for row in range(num_tweezers):
        freqs = [int(f) for f in col_ifs]
        dets = [0] * num_tweezers
        offset = row % 2
        for col in range(offset, num_tweezers - 1, 2):
            dets[col] = pull
            dets[col + 1] = -pull
        tweezer_freq.append(freqs)
        tweezer_detuning.append(dets)

    return {
        "gate_freq": gate_freq,
        "tweezer_freq": tweezer_freq,
        "tweezer_detuning": tweezer_detuning,
        "spacing_hz": spacing,
        "pull_hz": pull,
    }


def make_stream_moves_plan() -> dict:
    """Single live-grid step: chirp first three X tones by half spacing."""
    x0 = x_tweezer_IFs.astype(int).tolist()
    y0 = y_tweezer_IFs.astype(int).tolist()
    n_x, n_y = NUM_X_TWEEZERS, NUM_Y_TWEEZERS

    x_det = [0] * n_x
    for i in (0, 1, 2):
        x_det[i] = STREAM_DETUNING_HZ

    steps = [
        {
            "x_freq": x0,
            "x_det": x_det,
            "y_freq": y0,
            "y_det": [0] * n_y,
        }
    ]
    return {
        "steps": steps,
        "spacing_hz": IF_SPACING_HZ,
        "detuning_hz": STREAM_DETUNING_HZ,
    }


def make_move_sets_plan() -> dict:
    """Single packed move set (flag=0 exits after the set)."""
    x0 = x_tweezer_IFs.astype(int).tolist()
    y0 = y_tweezer_IFs.astype(int).tolist()
    n_x, n_y = NUM_X_TWEEZERS, NUM_Y_TWEEZERS

    x_freq = [0] * n_x
    x_det = [0] * n_x
    y_freq = [0] * n_y
    y_det = [0] * n_y
    for i in (0, 1, 2):
        x_freq[i] = x0[i]
        x_det[i] = STREAM_DETUNING_HZ
    y_freq[0] = y0[0]
    y_det[0] = 0

    return {
        "sets": [
            {
                "flag": 0,
                "x_freq": x_freq,
                "x_det": x_det,
                "y_freq": y_freq,
                "y_det": y_det,
            }
        ],
        "spacing_hz": IF_SPACING_HZ,
        "detuning_hz": STREAM_DETUNING_HZ,
    }


def main() -> None:
    PLANS_ROOT.mkdir(parents=True, exist_ok=True)

    row_plan = make_row_sort_plan()
    row_path = PLANS_ROOT / "row_sort_plan.json"
    row_path.write_text(json.dumps(row_plan, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {row_path} (pull={row_plan['pull_hz']} Hz, spacing={row_plan['spacing_hz']} Hz)")

    stream_plan = make_stream_moves_plan()
    stream_path = PLANS_ROOT / "stream_moves.json"
    stream_path.write_text(json.dumps(stream_plan, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {stream_path} ({len(stream_plan['steps'])} step, det={STREAM_DETUNING_HZ} Hz)")

    move_sets_plan = make_move_sets_plan()
    move_sets_path = PLANS_ROOT / "move_sets.json"
    move_sets_path.write_text(json.dumps(move_sets_plan, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {move_sets_path} ({len(move_sets_plan['sets'])} set, det={STREAM_DETUNING_HZ} Hz)")


if __name__ == "__main__":
    main()
