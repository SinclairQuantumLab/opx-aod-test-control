"""AOD amplitude calibration helpers for frequency-dependent compensation.

Calibration CSVs live under ``calibration_data/`` and use key/value rows.
Latency is keyed by script name, e.g. ``computation_latency_cc_02_row_sort_fixed_plan``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, Literal, NamedTuple

import numpy as np
from qm.qua import *  # noqa: F403

MIN_WAIT = 4
FIXED_DECIMAL_BIT = 28
ADC_BITSHIFT = FIXED_DECIMAL_BIT - 16


class Calibration(NamedTuple):
    center: float
    coefs: list
    computation_latency_cc: int = MIN_WAIT
    latency_only: bool = False


def computation_latency_key(module_name: str) -> str:
    """CSV row name for a script's processor gap."""
    return f"computation_latency_cc_{module_name}"


def _latency_from_fields(fields: dict[str, str], module_name: str | None) -> int:
    if module_name:
        value = fields.get(computation_latency_key(module_name), "").strip()
        return int(float(value)) if value else MIN_WAIT
    value = fields.get("computation_latency_cc", "").strip()
    return int(float(value)) if value else MIN_WAIT


def _parse_calibration_csv(csv_path: Path) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in csv_path.read_text(encoding="utf-8").strip().splitlines():
        if not line.strip() or "," not in line:
            continue
        key, rest = line.split(",", 1)
        fields[key.strip()] = rest.strip()
    return fields


def _as_pow2_float(coef: float) -> tuple[int, float]:
    """Return ``(n, f)`` such that ``coef == 2**n * f`` and ``1 <= abs(f) < 2``."""
    coef = float(coef)
    if coef == 0.0:
        return 0, 0.0
    n = int(np.floor(np.log2(abs(coef))))
    return n, coef / (2.0**n)


def _unpack_pow2_float(coef) -> tuple[int, float]:
    if isinstance(coef, (list, tuple, np.ndarray)):
        values = np.asarray(coef, dtype=float).reshape(-1)
        if values.size == 2:
            return int(values[0]), float(values[1])
        return 0, float(np.prod(values))
    return 0, float(coef)


def _coef_value(coef) -> float:
    n, f = _unpack_pow2_float(coef)
    return f * (2.0**n)


def _align_xy_coefs(x_coefs: list, y_coefs: list) -> tuple[list, list]:
    n_terms = max(len(x_coefs), len(y_coefs))
    sample = x_coefs[0] if x_coefs else (y_coefs[0] if y_coefs else 0.0)
    pad = (0, 0.0) if isinstance(sample, tuple) else 0.0
    x_coefs = [pad] * (n_terms - len(x_coefs)) + list(x_coefs)
    y_coefs = [pad] * (n_terms - len(y_coefs)) + list(y_coefs)
    return x_coefs, y_coefs


def load_calibration_coefs(
    csv_path: Path,
    unit_in_data: Literal["MHz", "GHz"] = "MHz",
    fixed_compatible: bool = True,
    module_name: str | None = None,
) -> Calibration:
    """Load polynomial coefficients from ``*_amplitude_calibration_coefs.csv``."""
    fields = _parse_calibration_csv(csv_path)
    if "center" not in fields or "coefs" not in fields:
        raise ValueError(f"{csv_path} must contain 'center' and 'coefs' rows")
    center = float(fields["center"])
    coefs = np.array([float(v) for v in fields["coefs"].split(",")])
    computation_latency_cc = _latency_from_fields(fields, module_name)

    if unit_in_data == "MHz":
        center /= 1e3
        coefs = coefs * (1e3 ** np.arange(len(coefs) - 1, -1, -1))

    if fixed_compatible:
        return Calibration(center, [_as_pow2_float(c) for c in coefs], computation_latency_cc)

    return Calibration(center, coefs.tolist(), computation_latency_cc)


def load_xy_amp_calibration(
    directory: Path | None = None,
    latency_only: bool = False,
    unit_in_data: Literal["MHz", "GHz"] = "MHz",
    fixed_compatible: bool = True,
    module_name: str | None = None,
) -> tuple[Calibration, Calibration]:
    """Load per-axis amplitude calibration CSVs if present."""
    directory = Path(__file__).resolve().parent / "calibration_data" if directory is None else Path(directory)
    latency_key = computation_latency_key(module_name) if module_name else "computation_latency_cc"
    calibrations: list[Calibration] = []
    for axis in ("x", "y"):
        csv_path = directory / f"{axis}_amplitude_calibration_coefs.csv"
        if csv_path.exists() and not latency_only:
            calibration = load_calibration_coefs(
                csv_path,
                unit_in_data=unit_in_data,
                fixed_compatible=fixed_compatible,
                module_name=module_name,
            )
            print(
                f"Loaded {axis.upper()} calibration "
                f"(center={calibration.center:.4f} GHz, "
                f"{latency_key}={calibration.computation_latency_cc})"
            )
        elif csv_path.exists():
            fields = _parse_calibration_csv(csv_path)
            latency = _latency_from_fields(fields, module_name)
            calibration = Calibration(0.0, [], latency, latency_only=True)
            print(f"Loaded {axis.upper()} {latency_key}={latency} (latency only)")
        else:
            print(f"No {csv_path.name} -- {axis.upper()} {latency_key}={MIN_WAIT}")
            calibration = Calibration(0.0, [], latency_only=True)
        calibrations.append(calibration)
    x_cal, y_cal = calibrations[0], calibrations[1]
    if not x_cal.latency_only and not y_cal.latency_only:
        x_coefs, y_coefs = _align_xy_coefs(x_cal.coefs, y_cal.coefs)
        x_cal = x_cal._replace(coefs=x_coefs)
        y_cal = y_cal._replace(coefs=y_coefs)
    return x_cal, y_cal


def upsert_computation_latency_cc(csv_path: Path, computation_latency_cc: int, module_name: str) -> None:
    """Insert or replace ``computation_latency_cc_<module_name>``, leaving other rows intact."""
    csv_path = Path(csv_path)
    if not csv_path.exists():
        raise FileNotFoundError(csv_path)
    key = computation_latency_key(module_name)
    lines = csv_path.read_text(encoding="utf-8").strip().splitlines()
    row = f"{key},{int(computation_latency_cc)}"
    updated = False
    out = []
    for line in lines:
        line_key = line.split(",", 1)[0].strip() if "," in line else line.strip()
        if line_key == key:
            out.append(row)
            updated = True
        else:
            out.append(line)
    if not updated:
        out.append(row)
    csv_path.write_text("\n".join(out) + "\n", encoding="utf-8")


def _fetch_timestamp_values(job, stream_name: str) -> np.ndarray:
    try:
        handle = job.result_handles.get(stream_name)
    except Exception:
        return np.array([], dtype=np.int64)
    if handle is None:
        return np.array([], dtype=np.int64)
    try:
        data = handle.fetch_all()
    except Exception:
        return np.array([], dtype=np.int64)
    if data is None:
        return np.array([], dtype=np.int64)
    if isinstance(data, dict):
        data = data.get("value", data.get("data", []))
    elif getattr(data, "dtype", None) is not None and getattr(data.dtype, "names", None):
        if "value" in data.dtype.names:
            data = data["value"]
    values = np.asarray(data).reshape(-1)
    if values.size and isinstance(values.flat[0], dict):
        values = np.array([v.get("value", v) for v in values.tolist()])
    if values.size == 0:
        return np.array([], dtype=np.int64)
    return np.asarray(values, dtype=np.int64).reshape(-1)


def _mode_diff_cc(timestamps: np.ndarray) -> int | None:
    if timestamps.size < 2:
        return None
    diffs = np.diff(timestamps)
    diffs = diffs[diffs > 0]
    if diffs.size == 0:
        return None
    values, counts = np.unique(diffs, return_counts=True)
    return int(values[np.argmax(counts)])


def _element_axis(element: str) -> str | None:
    if element.startswith("x"):
        return "x"
    if element.startswith("y"):
        return "y"
    return None


def check_tweezer_computation_latency(
    job,
    elements: Iterable[str],
    segment_length_ns: int,
    x_calibration: Calibration | None = None,
    y_calibration: Calibration | None = None,
    directory: Path | None = None,
    module_name: str | None = None,
) -> bool:
    """Verify per-tweezer chirp timing and update each axis's per-script latency."""
    GREEN = "\033[92m"
    RED = "\033[91m"
    RESET = "\033[0m"

    directory = (
        Path(__file__).resolve().parent / "calibration_data"
        if directory is None
        else Path(directory)
    )
    segment_cc = int(segment_length_ns) // 4
    calibration_by_axis = {"x": x_calibration, "y": y_calibration}

    timestamps_by_axis: dict[str, dict[str, np.ndarray]] = {"x": {}, "y": {}}
    for element in elements:
        axis = _element_axis(element)
        if axis is None:
            continue
        values = _fetch_timestamp_values(job, f"t_{element}")
        if values.size == 0:
            continue
        timestamps_by_axis[axis][element] = values

    corrected_by_axis: dict[str, int] = {}
    mismatches: list[str] = []
    last_delta = 0

    for axis in ("x", "y"):
        streams = timestamps_by_axis[axis]
        if not streams:
            continue

        names = list(streams)
        reference_name = names[0]
        reference = streams[reference_name]
        reference_mode = _mode_diff_cc(reference)
        for name, values in streams.items():
            mode_dt = _mode_diff_cc(values)
            # Cores may start at different absolute clock times (constant offset);
            # require matching segment spacing (mode of diffs), not identical arrays.
            if mode_dt != reference_mode:
                mismatches.append(
                    f"{name} mode(diff)={mode_dt} != {reference_name} mode(diff)={reference_mode}"
                )
            elif values.size == reference.size and not np.array_equal(values, reference):
                offset = int(values[0] - reference[0])
                print(
                    f"{name}: same mode(diff)={mode_dt} as {reference_name}, "
                    f"constant t0 offset={offset} cc"
                )

        if reference_mode is None:
            print(f"{axis.upper()} axis: too few timestamps to measure computation_latency_cc")
            continue

        calibration = calibration_by_axis[axis]
        if calibration is None:
            print(f"{axis.upper()} axis: no loaded calibration -- skipping")
            continue
        loaded_latency = int(calibration.computation_latency_cc)

        csv_path = directory / f"{axis}_amplitude_calibration_coefs.csv"
        delta = reference_mode - segment_cc
        last_delta = delta
        corrected_latency = loaded_latency + delta
        corrected_by_axis[axis] = corrected_latency
        if corrected_latency < 0:
            mismatches.append(
                f"{axis} axis negative computation_latency: "
                f"loaded={loaded_latency}, mode(diff)={reference_mode}, "
                f"segment_cc={segment_cc}, delta={delta}, corrected={corrected_latency}"
            )

        latency_key = (
            computation_latency_key(module_name) if module_name else "computation_latency_cc"
        )
        if csv_path.exists() and module_name and corrected_latency >= 0:
            upsert_computation_latency_cc(csv_path, corrected_latency, module_name)
            print(f"Updated {csv_path} with {latency_key}={corrected_latency}")

        color = GREEN if delta == 0 else RED
        status = "correct" if delta == 0 else "needs reload"
        print(
            f"{color}{axis.upper()} axis {latency_key} {status}: "
            f"loaded={loaded_latency}, mode(diff)={reference_mode}, segment_cc={segment_cc}, "
            f"delta={delta}, corrected={corrected_latency}{RESET}"
        )

    if not corrected_by_axis and not mismatches:
        print("No tweezer timestamp streams -- skipping computation_latency_cc check")

    if mismatches:
        raise RuntimeError("Computation time is not calibrated: " + "; ".join(mismatches))

    return last_delta == 0


def amp_at_frequency_py(
    freq: float | np.ndarray,
    center: float,
    coefs: Iterable,
) -> float | np.ndarray:
    """Evaluate the compensation polynomial in Python. *freq* and *center* in GHz."""
    x = freq - center
    result = 0.0
    coefs = list(coefs)
    for i, coef in enumerate(coefs):
        power = len(coefs) - 1 - i
        result = result + _coef_value(coef) * x**power
    return result


def amp_at_frequency_qua(freq, center, coefs, x_terms=None):
    """Generate QUA code that evaluates the compensation polynomial."""
    x = declare(fixed)
    assign(x, freq - center)

    if x_terms is None:
        x_terms = [declare(fixed) for _ in coefs]

    for i, (coef, x_term) in enumerate(zip(coefs[::-1], x_terms)):
        n, f = _unpack_pow2_float(coef)
        factors = ["x"] * i + [f"{f:.15f}"]
        op_str = ") * ".join(factors)
        op_str = "(" * (len(factors) - 1) + op_str
        assign(x_term, eval(op_str))
        if n >= 0:
            assign(x_term, x_term << n)
        elif n < 0:
            assign(x_term, x_term >> -n)

    op_str = " + ".join(f"x_terms[{i}]" for i in range(len(x_terms)))
    return eval(op_str)


def validate_xy_compensation_range(
    x_calibration: Calibration,
    y_calibration: Calibration,
    x_ifs_hz: Iterable[float],
    y_ifs_hz: Iterable[float],
    lo: float = 0.0,
    hi: float = 2.0,
) -> None:
    """Raise if either axis's compensation multiplier leaves [*lo*, *hi*] over its IF grid."""
    for axis, calibration, ifs in (
        ("x", x_calibration, x_ifs_hz),
        ("y", y_calibration, y_ifs_hz),
    ):
        if calibration.latency_only or not calibration.coefs:
            continue
        freqs_ghz = np.asarray(list(ifs), dtype=float) / 1e9
        multipliers = amp_at_frequency_py(freqs_ghz, calibration.center, calibration.coefs)
        multipliers = np.asarray(multipliers, dtype=float)
        amin, amax = float(np.min(multipliers)), float(np.max(multipliers))
        if amin < lo or amax > hi:
            raise ValueError(
                f"{axis.upper()} compensation multiplier range [{amin:.4f}, {amax:.4f}] "
                f"leaves [{lo}, {hi}] over the configured IF grid"
            )
        print(f"{axis.upper()} compensation range OK: [{amin:.4f}, {amax:.4f}]")


def quantized_multiplier(freq_hz: float, calibration: Calibration) -> float:
    """Python-side DAC-quantized compensation multiplier at *freq_hz*."""
    if calibration.latency_only or not calibration.coefs:
        return 1.0
    freq_ghz = float(freq_hz) / 1e9
    amp_mod = float(amp_at_frequency_py(freq_ghz, calibration.center, calibration.coefs))
    # Match fixed_to_adc_bit truncation used in the compensated chirp path.
    scale = 2**ADC_BITSHIFT
    return (int(amp_mod * (2**FIXED_DECIMAL_BIT)) >> ADC_BITSHIFT << ADC_BITSHIFT) / (
        2**FIXED_DECIMAL_BIT
    )
