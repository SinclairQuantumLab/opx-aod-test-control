"""OPX1000 / LF-FEM settings for the two AODs. Amplitudes are volts peak into 50 ohm."""

from pathlib import Path

# Wiring verified on 21 October 2026: output 1 = X/right, output 2 = Y/down.
qop_ip = "192.168.50.252"
controller = "con1"
fem = 3
aod_1_port = 1
aod_2_port = 2

# Edit these four values to change the tones independently.
aod_1_frequency = 98_000_000  # Hz
aod_2_frequency = 98_000_000  # Hz
aod_1_amplitude = 0.00145  # V peak
aod_2_amplitude = 0.00145  # V peak
constant_length = 1000  # ns; repeated without gaps by the QUA program

# Keep the first bench tests below 10 mV peak per amplifier input.
if not all(0 < value <= 0.01 for value in (aod_1_amplitude, aod_2_amplitude)):
    raise ValueError("Initial commissioning range: 0 < amplitude <= 0.01 V peak")

camera_settings = {"serial": "25370031", "model": "Blackfly BFLY-PGE-50S5M"}
results_path = Path(__file__).resolve().parent / "results"

config = {
    "controllers": {
        controller: {
            "type": "opx1000",
            "fems": {
                fem: {
                    "type": "LF",
                    "analog_outputs": {
                        aod_1_port: {"offset": 0.0, "output_mode": "direct", "sampling_rate": 1e9, "upsampling_mode": "mw"},
                        aod_2_port: {"offset": 0.0, "output_mode": "direct", "sampling_rate": 1e9, "upsampling_mode": "mw"},
                    },
                },
            },
        },
    },
    "elements": {
        "aod_1": {
            "singleInput": {"port": (controller, fem, aod_1_port)},
            "intermediate_frequency": aod_1_frequency,
            "operations": {"constant": "aod_1_constant_pulse"},
        },
        "aod_2": {
            "singleInput": {"port": (controller, fem, aod_2_port)},
            "intermediate_frequency": aod_2_frequency,
            "operations": {"constant": "aod_2_constant_pulse"},
        },
    },
    "pulses": {
        "aod_1_constant_pulse": {
            "operation": "control", "length": constant_length,
            "waveforms": {"single": "aod_1_constant_wf"},
        },
        "aod_2_constant_pulse": {
            "operation": "control", "length": constant_length,
            "waveforms": {"single": "aod_2_constant_wf"},
        },
    },
    "waveforms": {
        "aod_1_constant_wf": {"type": "constant", "sample": aod_1_amplitude},
        "aod_2_constant_wf": {"type": "constant", "sample": aod_2_amplitude},
    },
}
