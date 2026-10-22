"""Measure column-tone amplitudes for equal spot brightness along all seven rows."""

import copy
import json
from datetime import datetime, timezone
import numpy as np
import tifffile
from qm import QuantumMachinesManager
from qm.qua import IO1, IO2, amp, infinite_loop_, play, program
from camera import Camera
from configuration import config, qop_ip, camera_settings, results_path

row_frequencies = np.arange(95, 102) * 1_000_000
column_frequencies = np.arange(95, 101.01, 0.25) * 1_000_000
roi = (504, 324, 160, 160)
row_amplitude = 0.00145
reference_column_amplitude = 0.0013
base_amplitude = 0.002

calibration_config = copy.deepcopy(config)
for waveform in calibration_config["waveforms"].values():
    waveform["sample"] = base_amplitude

# IO registers let Python adjust amplitude between camera measurements.
with program() as calibration_program:
    with infinite_loop_():
        play("constant" * amp(IO1), "aod_1", duration=250_000)
    with infinite_loop_():
        play("constant" * amp(IO2), "aod_2", duration=250_000)


def brightness(frame):
    """Background-subtracted counts in a six-pixel-radius aperture."""
    y, x = np.unravel_index(np.argmax(frame), frame.shape)
    yy, xx = np.indices(frame.shape)
    signal = frame.astype(float) - np.median(frame)
    aperture = (xx-x)**2 + (yy-y)**2 <= 6**2
    return float(signal[aperture].sum()), [int(x), int(y)]


if __name__ == "__main__":
    folder = results_path / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ_brightness_calibration")
    folder.mkdir(parents=True)
    qmm = QuantumMachinesManager(host=qop_ip)
    qm = qmm.open_qm(calibration_config, close_other_machines=False)
    job = None
    result = {"roi":roi, "row_frequencies_hz":row_frequencies.tolist(),
              "column_frequencies_hz":column_frequencies.tolist(), "row_amplitude":row_amplitude, "points":[]}
    try:
        job = qm.execute(calibration_program)
        job.set_io_values(float(reference_column_amplitude/base_amplitude), float(row_amplitude/base_amplitude))
        with Camera(camera_settings, roi=roi, fps=45, latest=True) as camera, tifffile.TiffWriter(folder / "frames.tiff") as images:
            reference, _ = camera.frames(9, discard=4)
            target = float(np.mean([brightness(frame)[0] for frame in reference]))
            result["target_counts"] = target
            print(f"Target integrated brightness: {target:.1f} counts", flush=True)
            for row, row_frequency in enumerate(row_frequencies):
                job.set_intermediate_frequency("aod_2", float(row_frequency))
                for column, frequency in enumerate(column_frequencies):
                    job.set_intermediate_frequency("aod_1", float(frequency))
                    voltage = reference_column_amplitude
                    for attempt in range(4):
                        job.set_io1_value(float(voltage/base_amplitude))
                        frames, _ = camera.frames(5, discard=3)
                        measured = float(np.mean([brightness(frame)[0] for frame in frames]))
                        error = measured/target-1
                        if abs(error) < 0.025 or attempt == 3:
                            break
                        voltage = float(np.clip(voltage*np.sqrt(target/max(measured,1)),0.0008,0.0025))
                    images.write(frames, photometric="minisblack")
                    result["points"].append({"row":row,"column":column,"column_vpeak":voltage,
                        "counts":measured,"relative_error":error,"xy":brightness(frames.mean(axis=0))[1],
                        "peak":int(frames.max()),"attempts":attempt+1})
                (folder / "calibration.json").write_text(json.dumps(result,indent=2))
                print(f"Row {row+1}/7 calibrated; worst error {max(abs(p['relative_error']) for p in result['points'] if p['row']==row):.1%}",flush=True)
        result["camera"] = camera.metadata
    finally:
        try:
            if job is not None:
                job.cancel()
        finally:
            qm.close()
        (folder / "calibration.json").write_text(json.dumps(result,indent=2))
    print(folder)
