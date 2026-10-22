"""Two continuous AOD tones, following the QM qua-libs experiment layout."""

import json
import time
from datetime import datetime, timezone

from qm import QuantumMachinesManager, SimulationConfig
from qm.qua import align, infinite_loop_, play, program
from configuration import config, qop_ip, camera_settings, results_path

# Experiment settings
simulate = False
run_seconds = None  # None keeps output running until Ctrl+C; use 15 for a short test.
capture_camera = True

# QUA program: each element has its own gapless loop; both run in parallel.
with program() as continuous_tones:
    align("aod_1", "aod_2")
    with infinite_loop_():
        play("constant", "aod_1")
    with infinite_loop_():
        play("constant", "aod_2")

# Open communication, then simulate or execute the program.
if __name__ == "__main__":
    qmm = QuantumMachinesManager(host=qop_ip)
    if simulate:
        job = qmm.simulate(config, continuous_tones, SimulationConfig(duration=2500))
        job.wait_until("Done", timeout=30)
        samples = job.get_simulated_samples()
        print("Simulation complete. Samples are available in samples.con1.analog.")
    else:
        folder = results_path / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ_cw98")
        folder.mkdir(parents=True)
        qm = qmm.open_qm(config, close_other_machines=True)
        job = None
        try:
            job = qm.execute(continuous_tones)  # Compile, upload and start RF output.
            job.wait_until("Running", timeout=15)
            (folder / "run.json").write_text(json.dumps({"qm_id": qm.id, "job_id": job.id, "config": config}, indent=2))
            print(f"Output running. QM: {qm.id}\nResults: {folder}", flush=True)
            started = time.monotonic()
            if capture_camera:
                from camera import capture

                time.sleep(1)
                capture(camera_settings, folder)
                print(f"Camera image: {folder / 'frame.png'}", flush=True)
            while run_seconds is None or time.monotonic() - started < run_seconds:
                if not job.is_running():
                    break
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass
        finally:
            try:
                if job is not None:
                    job.cancel()
            finally:
                qm.close()
            (folder / "stopped.txt").write_text(datetime.now(timezone.utc).isoformat())
            print("Both outputs stopped.", flush=True)
