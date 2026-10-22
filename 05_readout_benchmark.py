"""Simulator-only readout benchmark. Never opens a QM or drives physical outputs."""

import argparse
import json
from pathlib import Path
from datetime import datetime, timezone
import numpy as np
from qm import QuantumMachinesManager, SimulationConfig, LoopbackInterface
from qm.qua import (program, declare, fixed, measure, integration, demod, assign,
                    for_, align, play, save, update_frequency, Cast, infinite_loop_)
from configuration import qop_ip, results_path


def configuration(channels, length_ns, frequencies=None):
    cfg = {
        "controllers": {"con1": {"type": "opx1000", "fems": {3: {
            "type": "LF",
            "analog_outputs": {1: {"offset": 0., "sampling_rate": 1e9, "output_mode": "direct"}},
            "analog_inputs": {1: {"offset": 0., "sampling_rate": 1e9}},
        }}}},
        "elements": {},
        "pulses": {
            "read": {"operation": "measurement", "length": length_ns,
                     "waveforms": {"single": "zero"}, "integration_weights": {"cos": "cos", "sin": "sin"}},
            "ready": {"operation": "control", "length": 32, "waveforms": {"single": "marker"}},
        },
        "waveforms": {"zero": {"type": "constant", "sample": 0.},
                      "marker": {"type": "constant", "sample": 0.1}},
        "integration_weights": {
            "cos": {"cosine": [(1., length_ns)], "sine": [(0., length_ns)]},
            "sin": {"cosine": [(0., length_ns)], "sine": [(1., length_ns)]},
        },
    }
    for channel in range(channels):
        cfg["elements"][f"rx{channel}"] = {
            "singleInput": {"port": ("con1", 3, 1)},
            "outputs": {"out1": ("con1", 3, 1)}, "time_of_flight": 92,
            "intermediate_frequency": int(frequencies[channel]) if frequencies is not None else 0,
            "operations": {"read": "read", "ready": "ready"},
        }
    return cfg


def add_source(cfg, waveform):
    cfg["controllers"]["con1"]["fems"][2] = {"type":"LF", "analog_outputs":{
        1:{"offset":0.,"sampling_rate":1e9,"output_mode":"direct","upsampling_mode":"mw"}}}
    cfg["elements"]["source"] = {"singleInput":{"port":("con1",2,1)},"intermediate_frequency":0,
                                 "operations":{"packet":"packet"}}
    cfg["waveforms"]["packet"] = {"type":"arbitrary","samples":waveform.tolist(),"max_allowed_error":1e-7}
    cfg["pulses"]["packet"] = {"operation":"control","length":len(waveform),"waveforms":{"single":"packet"}}


def build_sliced(sites, symbol_ns, bits):
    # The loopback requires timing/threshold calibration. Keep a trailing guard slice.
    cfg = configuration(1, (sites+1) * symbol_ns)
    add_source(cfg,np.concatenate([np.repeat(bits*0.2,symbol_ns),np.full(400,bits[-1]*0.2)]))
    with program() as sequence:
        values = declare(fixed, size=sites+1)
        occupancy = declare(int, size=sites)
        i = declare(int)
        total = declare(int, value=0)
        align("source","rx0")
        play("packet","source")
        measure("read", "rx0", integration.sliced("cos", values, symbol_ns//4, "out1"))
        with for_(i, 0, i < sites, i+1):
            assign(occupancy[i], Cast.to_int(values[i] < -0.0004))
            assign(total, total + occupancy[i])
        # The marker depends on all decoded sites; fetching is outside the timed path.
        play("ready", "rx0", condition=total > 0)
        with for_(i, 0, i < sites, i+1):
            save(occupancy[i], "occupancy")
            save(values[i], "values")
        save(total, "total")
    return cfg, sequence


def build_multitone(sites, channels, window_ns, bits, iq=True):
    # Up to 400 distinct carriers, spaced by 1 MHz; evaluate successive banks.
    cfg = configuration(channels, window_ns, np.arange(1, channels+1)*1_000_000)
    elements = list(cfg["elements"])
    t=np.arange(1000)*1e-9
    waveform=np.sum([np.cos(2*np.pi*(k+1)*1e6*t) for k in np.flatnonzero(bits)],axis=0)
    amplitude=0.3/max(abs(waveform))
    add_source(cfg,waveform*amplitude)
    threshold=(128*amplitude*window_ns/8192*.4)**2
    with program() as sequence:
        I = declare(fixed, size=sites)
        Q = declare(fixed, size=sites)
        occupancy = declare(int, size=sites)
        batch = declare(int)
        index = declare(int)
        total = declare(int, value=0)
        scaled_i = declare(fixed)
        scaled_q = declare(fixed)
        align("source",*elements)
        with infinite_loop_():
            play("packet","source")
        with for_(batch, 0, batch < sites//channels, batch+1):
            for channel, element in enumerate(elements):
                update_frequency(element, (batch*channels+channel+1)*1_000_000)
            align(*elements)
            for channel, element in enumerate(elements):
                if iq:
                    measure("read", element,
                            demod.full("cos", I[batch*channels+channel], "out1"),
                            demod.full("sin", Q[batch*channels+channel], "out1"))
                else:
                    measure("read", element, demod.full("cos", I[batch*channels+channel], "out1"))
            align(*elements)
        # Normalize before squaring to preserve QUA fixed-point precision.
        with for_(index, 0, index < sites, index+1):
            assign(scaled_i,Cast.mul_fixed_by_int(I[index],128))
            assign(scaled_q,Cast.mul_fixed_by_int(Q[index],128))
            if iq:
                assign(occupancy[index], Cast.to_int(scaled_i*scaled_i+scaled_q*scaled_q > threshold))
            else:
                assign(occupancy[index], Cast.to_int(scaled_i*scaled_i > threshold))
            assign(total, total+occupancy[index])
        play("ready", "rx0", condition=total > 0)
        with for_(index, 0, index < sites, index+1):
            save(occupancy[index], "occupancy")
            save(I[index], "I")
            save(Q[index], "Q")
        save(total, "total")
    return cfg, sequence


def fetched(job, name):
    data = job.result_handles.get(name).fetch_all()
    if getattr(data.dtype, "names", None) and "value" in data.dtype.names:
        data = data["value"]
    return np.asarray(data).tolist()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("method", choices=["sliced", "multitone"])
    parser.add_argument("--sites", type=int, default=400)
    parser.add_argument("--channels", type=int, default=8)
    parser.add_argument("--symbol-ns", type=int, default=20)
    parser.add_argument("--window-ns", type=int, default=1000)
    parser.add_argument("--one-quadrature", action="store_true")
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()
    if args.sites < 2 or args.symbol_ns < 20 or args.symbol_ns % 4:
        parser.error("Use at least two sites and a symbol length >=20 ns divisible by four")
    if args.method == "multitone" and (args.sites % args.channels or args.window_ns % 1000 or args.sites > 400):
        parser.error("Multitone requires sites divisible by channels, <=400 sites, and whole-microsecond windows")
    folder = results_path / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ_readout_simulation")
    folder.mkdir(parents=True)
    result = {"kind": "OPX simulation, not a physical measurement", "parameters": vars(args)}
    result["assumptions"] = {"sample_rate":1e9,"loopback_latency_ns":28,"time_of_flight_ns":92,
        "added_noise_power":0,"sliced_threshold":-0.0004,"sliced_trailing_guard_symbols":1,
        "multitone_amplitude_threshold_fraction":0.4,
        "endpoint":"conditional output after occupancy array conversion and occupied-site count; excludes sorting"}
    bits = np.zeros(args.sites,dtype=int)
    bits[np.random.default_rng(args.seed).choice(args.sites,args.sites//2,replace=False)] = 1
    result["expected_occupancy"] = bits.tolist()
    (folder / "result.json").write_text(json.dumps(result, indent=2))
    qmm = QuantumMachinesManager(host=qop_ip)
    job = None
    try:
        result["open_qms_before"] = qmm.list_open_qms()
        if args.method == "sliced":
            cfg, sequence = build_sliced(args.sites, args.symbol_ns,bits)
        else:
            cfg, sequence = build_multitone(args.sites,args.channels,args.window_ns,bits,not args.one_quadrature)
        (folder / "configuration.json").write_text(json.dumps(cfg,indent=2))
        (folder / "benchmark_source.py").write_text(Path(__file__).read_text())
        # All source/output ports below are virtual: qmm.simulate never reserves a QM.
        acquisition_ns = (args.sites+1)*args.symbol_ns if args.method=="sliced" else (args.sites//args.channels)*args.window_ns
        simulation_ns = acquisition_ns + args.sites*160 + 10000
        interface = LoopbackInterface([("con1",2,1,"con1",3,1)],latency=28)
        job = qmm.simulate(cfg,sequence,SimulationConfig(duration=simulation_ns//4,simulation_interface=interface))
        result["job_id"] = job.id
        job.wait_until("Done",timeout=60)
        report = job.get_simulated_waveform_report().to_dict()
        (folder / "waveform_report.json").write_text(json.dumps(report, indent=2))
        result["ready_waveforms"] = [w for w in report["analog_waveforms"] if w["pulse_name"] == "ready"]
        result["total"] = fetched(job,"total")
        result["occupancy"] = fetched(job,"occupancy")
        result["bit_errors"] = int(np.sum(np.array(result["occupancy"])!=bits))
        source_start = min(w["timestamp"] for w in report["analog_waveforms"] if w["element"]=="source")
        read_end = max(w["timestamp"]+w["length"] for w in report["analog_waveforms"] if w["pulse_name"]=="read")
        result["packet_to_ready_ns"] = result["ready_waveforms"][0]["timestamp"]-source_start if result["ready_waveforms"] else None
        result["packet_to_last_read_end_ns"] = read_end-source_start
        result["timing_validated"] = result["bit_errors"]==0 and result["packet_to_ready_ns"] is not None
        for name in (["values"] if args.method=="sliced" else ["I","Q"]):
            result[name] = fetched(job,name)
        result["open_qms_after"] = qmm.list_open_qms()
    except Exception as error:
        result["error"] = str(error)
        raise
    finally:
        if job is not None and job.get_status() == "Running":
            job.cancel()
        qmm.close()
        (folder / "result.json").write_text(json.dumps(result,indent=2))
        print(json.dumps({key:result.get(key) for key in ("bit_errors","packet_to_ready_ns","timing_validated","error")}),flush=True)
        print(folder,flush=True)
