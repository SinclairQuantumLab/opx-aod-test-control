"""Minimal-jerk tweezer chirp macros for camera-capture timing.

Uses ``nHz/nsec`` chirp units (1 Hz/s per integer) so ~0.27 MHz moves over 1 s
quantize cleanly. Segment index is an integer; ``tau`` is derived each step so
fixed-point drift does not accumulate over 20_000 segments.
"""

from qm.qua import *  # noqa: F403

from amplitude_calibration import amp_at_frequency_qua
from configuration import RAMP_SCALING_BITSHIFT, chirp_pulse_amplitude

FIXED_DECIMAL_BIT = 28
ADC_BITSHIFT = FIXED_DECIMAL_BIT - 16

# 1 nHz/nsec = 1 Hz/s. Peak MJ rate ≈ 1.875 * Δf / T → integer rates stay finite.
CHIRP_UNITS = "nHz/nsec"


def play_gate_hold(
    element,
    hold_length_ns,
    gate_frequency_hz,
    amp_calibration,
    hold_operation="row_hold",
):
    """Hold a gate tone at *gate_frequency_hz* with AOD-compensated amplitude."""

    qua_Hz_to_GHz = lambda f: (1 / (2**-FIXED_DECIMAL_BIT * 1e9)) * Cast.unsafe_cast_fixed(f)

    if amp_calibration.latency_only or not amp_calibration.coefs:
        update_frequency(element, gate_frequency_hz)
        play("rampup", element)
        play(hold_operation * amp(0), element, duration=hold_length_ns // 4)
        play("rampdown", element)
        ramp_to_zero(element)
        return

    center_ghz, coefs = amp_calibration.center, amp_calibration.coefs
    freq_ghz = declare(fixed)
    assign(freq_ghz, qua_Hz_to_GHz(gate_frequency_hz))
    effective_amp = declare(fixed)
    assign(effective_amp, amp_at_frequency_qua(freq_ghz, center_ghz, coefs))

    update_frequency(element, gate_frequency_hz)
    play("rampup" * amp(effective_amp), element)
    play(hold_operation * amp(0), element, duration=hold_length_ns // 4)
    play("rampdown" * amp(effective_amp), element)
    ramp_to_zero(element)


def play_minimal_jerk_chirp(
    element,
    start_frequency,
    detuning,
    segment_length,
    number_of_segments,
    amp_calibration,
):
    """Play a minimal-jerk chirp on a single element (rampup / chirp / rampdown)."""
    assert amp_calibration is not None
    if not amp_calibration.latency_only and amp_calibration.coefs:
        _play_compensated(
            element,
            start_frequency,
            detuning,
            segment_length,
            number_of_segments,
            amp_calibration,
        )
    else:
        _play_constant(
            element,
            start_frequency,
            detuning,
            segment_length,
            number_of_segments,
            amp_calibration.computation_latency_cc,
        )


def chirp_live_tone(
    element,
    start_frequency,
    detuning,
    cur_amp,
    segment_length,
    number_of_segments,
    amp_calibration,
):
    """Chirp a tone that is already on; update *cur_amp* to the final multiplier.

    No ``update_frequency``, rampup, or rampdown. The last segment uses
    ``continue_chirp=False`` so the NCO parks at the new frequency.
    """
    assert amp_calibration is not None
    if not amp_calibration.latency_only and amp_calibration.coefs:
        _chirp_live_compensated(
            element,
            start_frequency,
            detuning,
            cur_amp,
            segment_length,
            number_of_segments,
            amp_calibration,
        )
    else:
        _chirp_live_constant(
            element,
            detuning,
            segment_length,
            number_of_segments,
            amp_calibration.computation_latency_cc,
        )


def _hz_to_ghz(frequency_hz):
    return (1 / (2**-FIXED_DECIMAL_BIT * 1e9)) * Cast.unsafe_cast_fixed(frequency_hz)


def _chirp_rate_to_ghz(chirp_rate, segment_length):
    # nHz/nsec = Hz/s → Δf over one segment (ns) = rate * segment_length * 1e-9 Hz
    # In GHz: rate * segment_length * 1e-18
    return (segment_length / (2**-FIXED_DECIMAL_BIT * 1e18)) * Cast.unsafe_cast_fixed(chirp_rate)


def _to_dac_bits(value, additional_shift=0):
    return (value >> (ADC_BITSHIFT + additional_shift)) << (ADC_BITSHIFT + additional_shift)


def _constant_segments(
    element,
    detuning,
    segment_length,
    number_of_segments,
    computation_latency_cc,
    lead_in=None,
):
    """Constant-amplitude minimal-jerk segments with integer segment indexing.

    Each segment's play is issued before the next segment's math so the math
    runs while the play is on the timeline. *lead_in* runs after segment 1 is
    computed, so whatever it plays is followed directly by segment 1.
    """
    number_of_steps = number_of_segments + 1
    inv_number_of_steps = 1 / number_of_steps
    # rate [nHz/nsec] = detuning * 30e9 / (N * segment_ns) * τ²(1-2τ+τ²)
    # 30e9/(N*seg) ≈ 30 overflows fixed(±8), so factor as:
    #   mul_int_by_fixed(detuning, poly) * mul_int_by_fixed(..., seg_scale) * 30
    # with seg_scale = 1e9/(N*seg) ≈ 1 and poly ≤ 0.0625.
    segment_scale = 1e9 / (number_of_steps * segment_length)

    seg = declare(int)
    tau_next = declare(fixed)
    tau2 = declare(fixed)
    poly = declare(fixed)
    chirp_rate = declare(int)

    def _compute() -> None:
        assign(tau2, tau_next * tau_next)
        assign(poly, tau2 * (1 - 2 * tau_next + tau2))
        assign(chirp_rate, Cast.mul_int_by_fixed(detuning, poly))
        assign(chirp_rate, Cast.mul_int_by_fixed(chirp_rate, segment_scale))
        assign(chirp_rate, chirp_rate * 30)

    def _play(continue_chirp: bool) -> None:
        play(
            "hold",
            element,
            duration=segment_length // 4 - computation_latency_cc,
            chirp=(chirp_rate, CHIRP_UNITS),
            continue_chirp=continue_chirp,
            timestamp_stream=f"t_{element}",
        )

    # Segment 1 at τ = 1/N
    assign(tau_next, inv_number_of_steps)
    _compute()
    if lead_in is not None:
        lead_in()

    # Play segments 1 .. N-1; after play k, compute k+1 from integer index.
    with for_(seg, 1, seg < number_of_segments, seg + 1):
        _play(continue_chirp=True)
        assign(tau_next, Cast.mul_fixed_by_int(inv_number_of_steps, seg + 1))
        _compute()

    _play(continue_chirp=False)


def _compensated_segments(
    element,
    start_frequency,
    detuning,
    cumsum_amp_mod,
    segment_length,
    number_of_segments,
    amp_calibration,
    lead_in=None,
):
    """AOD-compensated minimal-jerk segments; *cumsum_amp_mod* tracks the DAC level.

    Same play-then-compute ordering and integer segment index as
    :func:`_constant_segments`. On return, *cumsum_amp_mod* holds the multiplier
    reached at the end of the last segment.
    """
    center_ghz, coefs = amp_calibration.center, amp_calibration.coefs
    computation_latency_cc = amp_calibration.computation_latency_cc

    number_of_steps = number_of_segments + 1
    inv_number_of_steps = 1 / number_of_steps
    segment_scale = 1e9 / (number_of_steps * segment_length)
    play_window_ns = segment_length - (4 * computation_latency_cc)
    ramp_scaling_V_ns = chirp_pulse_amplitude / play_window_ns

    seg = declare(int)
    tau_next = declare(fixed)
    tau2 = declare(fixed)
    poly = declare(fixed)
    chirp_rate = declare(int)

    amp_mod = declare(fixed)
    diff_amp_mod = declare(fixed)
    ramp_rate = declare(fixed)

    cur_freq_ghz = declare(fixed)
    x_terms = [declare(fixed) for _ in coefs]

    def _compute() -> None:
        assign(tau2, tau_next * tau_next)
        assign(poly, tau2 * (1 - 2 * tau_next + tau2))
        assign(chirp_rate, Cast.mul_int_by_fixed(detuning, poly))
        assign(chirp_rate, Cast.mul_int_by_fixed(chirp_rate, segment_scale))
        assign(chirp_rate, chirp_rate * 30)

        assign(cur_freq_ghz, cur_freq_ghz + _chirp_rate_to_ghz(chirp_rate, segment_length))
        assign(amp_mod, amp_at_frequency_qua(cur_freq_ghz, center_ghz, coefs, x_terms=x_terms))
        assign(
            diff_amp_mod,
            _to_dac_bits(amp_mod - cumsum_amp_mod, additional_shift=RAMP_SCALING_BITSHIFT),
        )
        assign(ramp_rate, diff_amp_mod * ramp_scaling_V_ns)
        assign(cumsum_amp_mod, cumsum_amp_mod + diff_amp_mod)

    def _play(continue_chirp: bool) -> None:
        play(
            ramp(ramp_rate),
            element,
            duration=segment_length // 4 - computation_latency_cc,
            chirp=(chirp_rate, CHIRP_UNITS),
            continue_chirp=continue_chirp,
            timestamp_stream=f"t_{element}",
        )

    assign(cur_freq_ghz, _hz_to_ghz(start_frequency))
    assign(tau_next, inv_number_of_steps)
    _compute()
    if lead_in is not None:
        lead_in()

    with for_(seg, 1, seg < number_of_segments, seg + 1):
        _play(continue_chirp=True)
        assign(tau_next, Cast.mul_fixed_by_int(inv_number_of_steps, seg + 1))
        _compute()

    _play(continue_chirp=False)


def _play_constant(
    element,
    start_frequency,
    detuning,
    segment_length,
    number_of_segments,
    computation_latency_cc,
):
    def _lead_in() -> None:
        update_frequency(element, start_frequency)
        play("rampup", element)

    _constant_segments(
        element,
        detuning,
        segment_length,
        number_of_segments,
        computation_latency_cc,
        lead_in=_lead_in,
    )

    play("rampdown", element)
    ramp_to_zero(element, 1)


def _play_compensated(
    element,
    start_frequency,
    detuning,
    segment_length,
    number_of_segments,
    amp_calibration,
):
    center_ghz, coefs = amp_calibration.center, amp_calibration.coefs

    start_freq_ghz = declare(fixed)
    start_amp = declare(fixed)
    cumsum_amp_mod = declare(fixed)

    assign(start_freq_ghz, _hz_to_ghz(start_frequency))
    assign(start_amp, amp_at_frequency_qua(start_freq_ghz, center_ghz, coefs))
    assign(cumsum_amp_mod, _to_dac_bits(start_amp))

    def _lead_in() -> None:
        update_frequency(element, start_frequency)
        play("rampup" * amp(start_amp), element)

    _compensated_segments(
        element,
        start_frequency,
        detuning,
        cumsum_amp_mod,
        segment_length,
        number_of_segments,
        amp_calibration,
        lead_in=_lead_in,
    )

    play("rampdown" * amp(cumsum_amp_mod), element)
    ramp_to_zero(element, 1)


def _chirp_live_constant(
    element,
    detuning,
    segment_length,
    number_of_segments,
    computation_latency_cc,
):
    _constant_segments(
        element,
        detuning,
        segment_length,
        number_of_segments,
        computation_latency_cc,
    )


def _chirp_live_compensated(
    element,
    start_frequency,
    detuning,
    cur_amp,
    segment_length,
    number_of_segments,
    amp_calibration,
):
    cumsum_amp_mod = declare(fixed)
    assign(cumsum_amp_mod, cur_amp)

    _compensated_segments(
        element,
        start_frequency,
        detuning,
        cumsum_amp_mod,
        segment_length,
        number_of_segments,
        amp_calibration,
    )

    assign(cur_amp, cumsum_amp_mod)


def emit_requested_tone(
    element,
    freq,
    detuning,
    segment_length,
    number_of_segments,
    amp_calibration,
) -> None:
    """Rampup / MJ chirp / rampdown when *freq* > 0; *detuning* 0 holds the tone static.

    Tones with ``freq == 0`` stay off. Unlike :func:`emit_move_chirp_if_active`, a
    requested tone with zero detuning still plays (zero chirp rate across the
    same segment window), so it starts and stops with the movers.
    """
    with if_(freq > 0):
        play_minimal_jerk_chirp(
            element,
            freq,
            detuning,
            segment_length,
            number_of_segments,
            amp_calibration,
        )


def emit_move_chirp_if_active(
    element,
    freq,
    detuning,
    segment_length,
    number_of_segments,
    amp_calibration,
) -> None:
    """Play a full rampup/chirp/rampdown only when freq is live and detuning is nonzero."""
    with if_(freq > 0):
        with if_(~(detuning == 0)):
            play_minimal_jerk_chirp(
                element,
                freq,
                detuning,
                segment_length,
                number_of_segments,
                amp_calibration,
            )


def emit_live_chirp_if_active(
    element,
    freq,
    detuning,
    cur_amp,
    segment_length,
    number_of_segments,
    amp_calibration,
) -> None:
    """Chirp a live tone only when freq is live and detuning is nonzero."""
    with if_(freq > 0):
        with if_(~(detuning == 0)):
            chirp_live_tone(
                element,
                freq,
                detuning,
                cur_amp,
                segment_length,
                number_of_segments,
                amp_calibration,
            )
