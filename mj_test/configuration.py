"""OPX1000 configuration for camera-capture AOD scripts (12 X + 12 Y sticky tones).

Self-contained copy for the camera_capture subdirectory. Ports, IFs, amplitudes,
and timing differ from the top-level distribution scripts.
"""

from __future__ import annotations

import itertools
from pathlib import Path

import numpy as np

######################
# Network parameters #
######################

qop_ip = "192.168.50.252"
cluster_name = "Cluster_1"
qop_port = None

######################
# Tweezer geometry   #
######################

MAX_TWEEZERS_PER_FEM = 16

X_TWEEZERS = 12
Y_TWEEZERS = 12

# X-axis (column) AOD -- camera-capture wiring
X_FEMS = [("con1", 2, 4)]
# Y-axis (row) AOD -- camera-capture wiring
Y_FEMS = [("con1", 3, 4)]

SCOPE_TRIGGER = ("con1", 2, 1)

AOD_ANALOG_OUTPUTS = X_FEMS + Y_FEMS

# Compressed grid into the bench optical window (95–101 MHz).
IF_LOW_HZ = 95_000_000
IF_HIGH_HZ = 101_000_000

# Commissioning limit: summed peak per port must stay below 10 mV.
MAX_PORT_PEAK_V = 0.010
# Flat amplitude: 12 tones in phase → 9.6 mV < 10 mV.
TONE_AMPLITUDE_V = 0.0008


def _distribute_tweezers(count: int, num_fems: int, max_per_fem: int) -> list[int]:
    """Spread *count* tweezers evenly across *num_fems* FEMs."""
    assert count <= num_fems * max_per_fem, (
        f"Cannot fit {count} tweezers across {num_fems} FEMs "
        f"(max {max_per_fem} each, capacity {num_fems * max_per_fem})"
    )
    base, remainder = divmod(count, num_fems)
    return [base + (1 if index < remainder else 0) for index in range(num_fems)]


x_per_fem = _distribute_tweezers(X_TWEEZERS, len(X_FEMS), MAX_TWEEZERS_PER_FEM)
y_per_fem = _distribute_tweezers(Y_TWEEZERS, len(Y_FEMS), MAX_TWEEZERS_PER_FEM)

NUM_X_TWEEZERS = X_TWEEZERS
NUM_Y_TWEEZERS = Y_TWEEZERS
newman_phases_2pi = lambda N: np.arange(N) ** 2 / (2 * N)
x_tweezer_elements = [f"x_tweezer_{index + 1}" for index in range(NUM_X_TWEEZERS)]
x_tweezer_IFs = np.linspace(IF_LOW_HZ, IF_HIGH_HZ, NUM_X_TWEEZERS).astype(int)
x_tweezer_phases = newman_phases_2pi(NUM_X_TWEEZERS)
y_tweezer_elements = [f"y_tweezer_{index + 1}" for index in range(NUM_Y_TWEEZERS)]
y_tweezer_IFs = np.linspace(IF_LOW_HZ, IF_HIGH_HZ, NUM_Y_TWEEZERS).astype(int)
y_tweezer_phases = newman_phases_2pi(NUM_Y_TWEEZERS)
all_tweezer_elements = x_tweezer_elements + y_tweezer_elements

IF_SPACING_HZ = int(x_tweezer_IFs[1] - x_tweezer_IFs[0]) if NUM_X_TWEEZERS > 1 else 0

# Flat amplitude: every tone uses the same sample; multipliers stay at 1.0.
x_tweezer_max_amplitude = float(TONE_AMPLITUDE_V)
y_tweezer_max_amplitude = float(TONE_AMPLITUDE_V)
x_tweezer_amplitudes = [float(TONE_AMPLITUDE_V)] * NUM_X_TWEEZERS
y_tweezer_amplitudes = [float(TONE_AMPLITUDE_V)] * NUM_Y_TWEEZERS
chirp_pulse_amplitude = float(TONE_AMPLITUDE_V)

# Extra truncation of amplitude deltas before computing ramp rates (DAC-aware).
# Unused on the flat (latency_only) path, kept for API parity with top-level.
RAMP_SCALING_BITSHIFT = 10

##################
# Pulse settings #
##################

# 50 us segments × 20_000 = 1 s move; 50 us up/down ramps.
segment_length = 50_000
number_of_segments = 20_000
chirp_pulse_length = segment_length * number_of_segments
rampup_length = 50_000
rampdown_length = rampup_length
# 50_000 samples at 1 GS/s is large; use 100 MS/s → 5_000 samples per ramp.
ramp_sampling_rate = 100_000_000

assert chirp_pulse_length % segment_length == 0
assert number_of_segments == chirp_pulse_length // segment_length

CALIBRATION_ROOT = Path(__file__).resolve().parent / "calibration_data"
CALIBRATION_ROOT.mkdir(exist_ok=True)

PLANS_ROOT = Path(__file__).resolve().parent / "plans"
PLANS_ROOT.mkdir(exist_ok=True)

results_path = Path(__file__).resolve().parent / "results"
results_path.mkdir(exist_ok=True)

##################
# Camera         #
##################

camera_settings = {"serial": "25370031", "model": "Blackfly BFLY-PGE-50S5M"}
# Bench crop around 98 MHz; may need re-centering after new wiring.
roi = (504, 324, 160, 160)
fps = 45
# Spots are ~13% of bench brightness at 0.8 mV; raise exposure if needed (us).
camera_exposure_us = None  # None = keep camera's current ExposureTime
camera_gain = None  # None = keep camera's current Gain

#################
# OPX config    #
#################

_port_cfg = {
    "offset": 0.0,
    "sampling_rate": 1e9,
    "output_mode": "direct",
    "upsampling_mode": "mw",
}


def _blackman(duration: int, v_start: float, v_end: float, sampling_rate: float) -> np.ndarray:
    """Blackman ramp sampled at *sampling_rate* (waveform sample count = duration * rate / 1e9)."""
    n_samples = max(1, int(round(duration * sampling_rate / 1e9)))
    time_vector = np.arange(n_samples, dtype=float)
    return v_start + (
        time_vector / n_samples
        - (25 / (42 * np.pi)) * np.sin(2 * np.pi * time_vector / n_samples)
        + (1 / (21 * np.pi)) * np.sin(4 * np.pi * time_vector / n_samples)
    ) * (v_end - v_start)


def _fem_for_index(index: int, per_fem: list[int]) -> int:
    cumulative = 0
    for fem_idx, count in enumerate(per_fem):
        if index < cumulative + count:
            return fem_idx
        cumulative += count
    return len(per_fem) - 1


def _build_controller_config(fem_lists: list[list[tuple[str, int, int]]]) -> dict:
    controllers: dict = {}
    for con, slot, port in itertools.chain(*fem_lists):
        controllers.setdefault(con, {})
        controllers[con].setdefault(slot, {"type": "LF", "analog_outputs": {}})
        controllers[con][slot]["analog_outputs"][port] = dict(_port_cfg)
    for con, slot, port in (SCOPE_TRIGGER,):
        controllers.setdefault(con, {})
        fem_cfg = controllers[con].setdefault(
            slot, {"type": "LF", "analog_outputs": {}, "digital_outputs": {}}
        )
        fem_cfg.setdefault("digital_outputs", {})[port] = {}
    return {
        "controllers": {
            controller: {"type": "opx1000", "fems": fems}
            for controller, fems in controllers.items()
        }
    }


def _tweezer_element(index, fem_list, per_fem, if_array, element_names):
    con, slot, port = fem_list[_fem_for_index(index, per_fem)]
    return {
        "singleInput": {"port": (con, slot, port)},
        "intermediate_frequency": int(if_array[index]),
        "operations": {
            "hold": "zero_pulse",
            "const": "const_pulse",
            "row_hold": "row_hold_pulse",
            "rampup": "rampup_pulse",
            "rampdown": "rampdown_pulse",
        },
        "sticky": {"analog": True},
        "core": element_names[index],
    }


def assert_port_peak_safe(
    tones_per_port: int,
    sample_v: float,
    max_multiplier: float = 1.0,
    limit_v: float = MAX_PORT_PEAK_V,
) -> None:
    """Raise if coherent sum of tones on one port can exceed *limit_v* peak."""
    peak = tones_per_port * abs(sample_v) * abs(max_multiplier)
    if peak >= limit_v:
        raise ValueError(
            f"Port peak {peak * 1e3:.3f} mV "
            f"({tones_per_port} tones × {abs(sample_v) * 1e3:.3f} mV × {max_multiplier}) "
            f"must stay below {limit_v * 1e3:.1f} mV"
        )


def build_config() -> dict:
    """Return the QM configuration dict."""
    # Safety: coherent sum of all tones on each AOD port.
    assert_port_peak_safe(max(x_per_fem), TONE_AMPLITUDE_V)
    assert_port_peak_safe(max(y_per_fem), TONE_AMPLITUDE_V)

    elements: dict = {}
    for index, name in enumerate(x_tweezer_elements):
        elements[name] = _tweezer_element(
            index, X_FEMS, x_per_fem, x_tweezer_IFs, x_tweezer_elements
        )
    for index, name in enumerate(y_tweezer_elements):
        elements[name] = _tweezer_element(
            index, Y_FEMS, y_per_fem, y_tweezer_IFs, y_tweezer_elements
        )

    elements["scope_trigger"] = {
        "operations": {"trigger": "trigger_pulse"},
        "digitalInputs": {
            "marker": {
                "port": SCOPE_TRIGGER,
                "buffer": 0,
                "delay": 0,
            }
        },
    }

    wf_amplitude = float(TONE_AMPLITUDE_V)
    rampup_samples = _blackman(rampup_length, 0.0, wf_amplitude, ramp_sampling_rate)
    rampdown_samples = _blackman(rampdown_length, 0.0, -wf_amplitude, ramp_sampling_rate)

    return _build_controller_config([X_FEMS, Y_FEMS]) | {
        "elements": elements,
        "pulses": {
            "const_pulse": {
                "operation": "control",
                "length": segment_length,
                "waveforms": {"single": "const_wf"},
            },
            "rampup_pulse": {
                "operation": "control",
                "length": rampup_length,
                "waveforms": {"single": "rampup_wf"},
            },
            "rampdown_pulse": {
                "operation": "control",
                "length": rampdown_length,
                "waveforms": {"single": "rampdown_wf"},
            },
            "zero_pulse": {
                "operation": "control",
                "length": segment_length,
                "waveforms": {"single": "zero_wf"},
            },
            # Length is a template only; play_gate_hold always passes an explicit
            # duration. Keep this short so the simulator does not allocate a 1 s pulse.
            "row_hold_pulse": {
                "operation": "control",
                "length": segment_length,
                "waveforms": {"single": "const_wf"},
            },
            "trigger_pulse": {
                "operation": "control",
                "length": 1_000,
                "digital_marker": "ON",
            },
        },
        "waveforms": {
            "const_wf": {"type": "constant", "sample": wf_amplitude},
            "zero_wf": {"type": "constant", "sample": 0.0},
            "rampup_wf": {
                "type": "arbitrary",
                "samples": list(rampup_samples),
                "sampling_rate": ramp_sampling_rate,
            },
            "rampdown_wf": {
                "type": "arbitrary",
                "samples": list(rampdown_samples),
                "sampling_rate": ramp_sampling_rate,
            },
        },
        "digital_waveforms": {
            "ON": {"samples": [(1, 0)]},
        },
    }


config = build_config()
