"""Shared helpers to ramp the full 12x12 AOD grid up and down."""

from __future__ import annotations

from qm.qua import align, amp, assign, frame_rotation_2pi, play, ramp_to_zero, update_frequency

from amplitude_calibration import quantized_multiplier
from configuration import (
    all_tweezer_elements,
    x_tweezer_IFs,
    x_tweezer_elements,
    x_tweezer_phases,
    y_tweezer_IFs,
    y_tweezer_elements,
    y_tweezer_phases,
)


def apply_phase_setup() -> None:
    """Apply compile-time Newman frame rotations before playing tones."""
    for index, element in enumerate(x_tweezer_elements):
        frame_rotation_2pi(float(x_tweezer_phases[index]), element)
    for index, element in enumerate(y_tweezer_elements):
        frame_rotation_2pi(float(y_tweezer_phases[index]), element)


def initial_amp_values(x_amp_calibration, y_amp_calibration) -> tuple[list[float], list[float]]:
    """Python-side starting multipliers for each tone at its configured IF."""
    x_amps = [quantized_multiplier(float(f), x_amp_calibration) for f in x_tweezer_IFs]
    y_amps = [quantized_multiplier(float(f), y_amp_calibration) for f in y_tweezer_IFs]
    return x_amps, y_amps


def ramp_up_grid(x_amp_calibration, y_amp_calibration, x_amp=None, y_amp=None) -> None:
    """Ramp up all 24 tones at their configured IFs with compensated amplitude.

    If *x_amp* / *y_amp* QUA arrays are provided, they are filled with the
    starting multipliers used for ramp-up (for later live-chirp tracking).
    """
    apply_phase_setup()

    x_amps_py, y_amps_py = initial_amp_values(x_amp_calibration, y_amp_calibration)

    for index, element in enumerate(x_tweezer_elements):
        update_frequency(element, int(x_tweezer_IFs[index]))
        play("rampup" * amp(float(x_amps_py[index])), element)
        if x_amp is not None:
            assign(x_amp[index], float(x_amps_py[index]))

    for index, element in enumerate(y_tweezer_elements):
        update_frequency(element, int(y_tweezer_IFs[index]))
        play("rampup" * amp(float(y_amps_py[index])), element)
        if y_amp is not None:
            assign(y_amp[index], float(y_amps_py[index]))

    align(*all_tweezer_elements)


def ramp_down_grid(x_amp=None, y_amp=None, x_amps_py=None, y_amps_py=None) -> None:
    """Ramp down all 24 tones using tracked or Python-side amplitudes."""
    if x_amp is not None:
        for index, element in enumerate(x_tweezer_elements):
            play("rampdown" * amp(x_amp[index]), element)
            ramp_to_zero(element)
    else:
        assert x_amps_py is not None
        for index, element in enumerate(x_tweezer_elements):
            play("rampdown" * amp(float(x_amps_py[index])), element)
            ramp_to_zero(element)

    if y_amp is not None:
        for index, element in enumerate(y_tweezer_elements):
            play("rampdown" * amp(y_amp[index]), element)
            ramp_to_zero(element)
    else:
        assert y_amps_py is not None
        for index, element in enumerate(y_tweezer_elements):
            play("rampdown" * amp(float(y_amps_py[index])), element)
            ramp_to_zero(element)

    align(*all_tweezer_elements)
