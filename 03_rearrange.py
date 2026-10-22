"""Five two-second, row-by-row rearrangements; record the actual camera frames."""

import copy
import json
import time
from datetime import datetime, timezone
import numpy as np
import tifffile
from qm import QuantumMachinesManager, SimulationConfig
from qm.qua import align, amp, assign, declare, fixed, for_, frame_rotation_2pi, pause, play, program, update_frequency
from camera import Camera
from configuration import config, qop_ip, camera_settings, results_path

# Experiment settings
simulate = False
cycles = 5
seed = 42
fps = 45
roi = (504, 324, 160, 160)
calibration_file = results_path / "20261022T013354_095955Z_brightness_calibration/calibration.json"
rectangle_feedback_file = results_path / "20261022T014139_082938Z_rearrangement/rectangle_feedback.json"
grid_hz = np.arange(95, 102) * 1_000_000
target_sites = [4, 5, 6]
move_ns = 220_000_000
segment_ns = 20_000_000  # Eleven hardware chirps with calibrated amplitude updates.
row_ns = 260_000_000
rectangle_ns = 180_000_000  # 7 * 260 ms + 180 ms = 2 seconds.

calibration = json.loads(calibration_file.read_text())
if len(calibration["points"]) != 175:
    raise ValueError("A complete 7 × 25 brightness calibration is required")
voltages = np.array([p["column_vpeak"] for p in calibration["points"]]).reshape(7,25)
sample_frequencies = np.array(calibration["column_frequencies_hz"])
row_voltage = calibration["row_amplitude"]
rng = np.random.default_rng(seed)
if not 1 <= cycles <= 35:  # There are 35 ways to choose three sites from seven.
    raise ValueError("Use 1–35 cycles for distinct loadings in every row")
loadings = []
for cycle in range(cycles):
    loading = []
    for row in range(7):
        occupied = sorted(int(i) for i in rng.choice(7,3,replace=False))
        while any(occupied == previous[row] for previous in loadings):
            occupied = sorted(int(i) for i in rng.choice(7,3,replace=False))
        loading.append(occupied)
    loadings.append(loading)

# Seven row oscillators and three column oscillators, sharing two physical ports.
sorting_config = copy.deepcopy(config)
sorting_config["elements"] = {}
sorting_config["pulses"] = {}
sorting_config["waveforms"] = {}
elements = [f"row_{r}" for r in range(7)] + [f"column_{c}" for c in range(3)]
for r in range(7):
    sorting_config["elements"][f"row_{r}"] = {"singleInput":copy.deepcopy(config["elements"]["aod_2"]["singleInput"]),
        "intermediate_frequency":int(grid_hz[r]), "operations":{}}
for c in range(3):
    sorting_config["elements"][f"column_{c}"] = {"singleInput":copy.deepcopy(config["elements"]["aod_1"]["singleInput"]),
        "intermediate_frequency":int(grid_hz[target_sites[c]]), "operations":{}}


def add_pulse(name, samples, length_ns, element_names):
    waveform = ({"type":"constant", "sample":float(samples)} if np.isscalar(samples) else
                {"type":"arbitrary", "samples":list(samples), "max_allowed_error":1e-7})
    sorting_config["waveforms"][name] = waveform
    sorting_config["pulses"][name] = {"operation":"control", "length":length_ns, "waveforms":{"single":name}}
    for element in element_names:
        sorting_config["elements"][element]["operations"][name] = name


base_voltage = 0.002
blackman = np.maximum(np.blackman(2000),0) * base_voltage
add_pulse("constant", base_voltage, 1000, elements)
add_pulse("up", blackman[:1000], 1000, elements)
add_pulse("down", blackman[1000:], 1000, elements)

# Best separable row/column correction for the final simultaneous rectangle.
destination_voltages = voltages[:,[16,20,24]]
log_voltages = np.log(destination_voltages)
rectangle_columns = np.exp(log_voltages.mean(axis=0))
rectangle_rows = row_voltage * np.exp(log_voltages.mean(axis=1)-log_voltages.mean())
rectangle_feedback = json.loads(rectangle_feedback_file.read_text())
rectangle_rows *= np.array(rectangle_feedback["row_scale"])
rectangle_columns *= np.array(rectangle_feedback["column_scale"])

# Small lookup tables; the OPX reuses one row-motion loop for all five loadings.
source_table, rate_table, initial_table, final_table, correction_table = [], [], [], [], []
segments = move_ns // segment_ns
for loading in loadings:
    for row, occupied in enumerate(loading):
        for column, source in enumerate(occupied):
            start, end = int(grid_hz[source]), int(grid_hz[target_sites[column]])
            source_table.append(start)
            rate_table.append(round((end-start)/(move_ns*1e-9)))
            initial_table.append(float(np.interp(start,sample_frequencies,voltages[row])/base_voltage))
            final_table.append(float(np.interp(end,sample_frequencies,voltages[row])/base_voltage))
            for segment in range(segments):
                midpoint = start+(end-start)*(segment+0.5)/segments
                correction_table.append(float(np.interp(midpoint,sample_frequencies,voltages[row])/base_voltage))

# The whole motion is compiled into the OPX; no host updates occur during playback.
with program() as rearrangement:
    cycle = declare(int)
    row = declare(int)
    segment = declare(int)
    index = declare(int)
    sources = declare(int,value=source_table)
    rates = declare(int,value=rate_table)
    initial = declare(fixed,value=initial_table)
    final = declare(fixed,value=final_table)
    corrections = declare(fixed,value=correction_table)
    if not simulate:
        pause()  # Arm the camera before starting.
    with for_(cycle,0,cycle<cycles,cycle+1):
        with for_(row,0,row<7,row+1):
            align(*elements)
            assign(index,(cycle*7+row)*3)
            update_frequency("row_0",95_000_000+row*1_000_000,keep_phase=True)
            for column in range(3):
                update_frequency(f"column_{column}",sources[index+column],keep_phase=True)
            align(*elements)
            play("up" * amp(row_voltage/base_voltage),"row_0")
            play("constant" * amp(row_voltage/base_voltage),"row_0",duration=(row_ns-2000)//4)
            play("down" * amp(row_voltage/base_voltage),"row_0")
            for column in range(3):
                play("up" * amp(initial[index+column]),f"column_{column}")
                play("constant" * amp(initial[index+column]),f"column_{column}",duration=20_000_000//4)
            with for_(segment,0,segment<segments,segment+1):
                for column in range(3):
                    play("constant" * amp(corrections[(index+column)*segments+segment]),f"column_{column}",
                         duration=segment_ns//4,chirp=(rates[index+column],"Hz/sec"))
            for column in range(3):
                play("constant" * amp(final[index+column]),f"column_{column}",duration=19_998_000//4)
                play("down" * amp(final[index+column]),f"column_{column}")
        align(*elements)
        update_frequency("row_0",int(grid_hz[0]),keep_phase=True)
        for column, site in enumerate(target_sites):
            update_frequency(f"column_{column}",int(grid_hz[site]),keep_phase=True)
        align(*elements)
        for row_index in range(7):
            frame_rotation_2pi(float(-row_index*(row_index-1)/14),f"row_{row_index}")
            play("constant" * amp(float(rectangle_rows[row_index]/base_voltage)),f"row_{row_index}",duration=rectangle_ns//4)
        for column in range(3):
            play("constant" * amp(float(rectangle_columns[column]/base_voltage)),f"column_{column}",duration=rectangle_ns//4)

if __name__ == "__main__":
    qmm = QuantumMachinesManager(host=qop_ip)
    if simulate:
        job = qmm.simulate(sorting_config,rearrangement,SimulationConfig(duration=2500))
        job.wait_until("Done",timeout=30)
        print("Full sequence compiled; first 10 microseconds simulated.")
    else:
        folder = results_path / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ_rearrangement")
        folder.mkdir(parents=True)
        qm = qmm.open_qm(sorting_config,close_other_machines=False)
        job = None
        record = {"loadings":loadings,"target_sites":target_sites,"cycle_seconds":2,"cycles":cycles,
                  "roi":roi,"calibration_file":str(calibration_file),"rectangle_feedback_file":str(rectangle_feedback_file),
                  "rectangle_row_vpeak":rectangle_rows.tolist(),"rectangle_column_vpeak":rectangle_columns.tolist(),"config":sorting_config}
        try:
            job = qm.execute(rearrangement)
            record.update(qm_id=qm.id,job_id=job.id)
            deadline = time.monotonic()+20
            while not job.is_paused():
                if time.monotonic()>deadline:
                    raise TimeoutError("OPX did not reach the camera-start pause")
                time.sleep(0.01)
            with Camera(camera_settings,roi=roi,fps=fps) as camera:
                camera.frames(3,discard=2)
                job.resume()
                frames,timestamps = camera.frames(round((cycles*2+0.3)*fps),discard=0)
            record["camera"] = camera.metadata
            record["timestamps"] = timestamps
            tifffile.imwrite(folder / "frames.tiff",frames,photometric="minisblack")
        finally:
            try:
                if job is not None:
                    job.cancel()
            finally:
                qm.close()
            (folder / "run.json").write_text(json.dumps(record,indent=2))
        print(f"Outputs stopped. Recording: {folder}",flush=True)
