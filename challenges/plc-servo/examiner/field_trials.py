"""Examiner-side field-profile checks using the candidate's frozen program."""
from statistics import mean

from field_device import MAX_RPM, MAX_TRIP_RPM, STOP_WINDOW_RPM
from motion_trials import advance, active, input_motion, linked, stopped as shaft_stopped, trial


def speed_input(room, rpm, *, start=False, stop=False):
    return input_motion(room, "speed", rpm100=rpm * 100, start=start, stop=stop)


def launch(room, rpm):
    speed_input(room, rpm)
    idle = advance(room, 50)
    speed_input(room, rpm, start=True)
    advance(room, 6)
    speed_input(room, rpm)
    return idle


def dwell(room, target_rpm, seconds=10):
    ticks = round(seconds / room.bench.dt)
    initial = room.operate("observe")
    window = {}
    kept_running = True
    for start in range(0, ticks, 5):
        end = advance(room, min(5, ticks - start))
        drive = end["speed"]["drive"]
        if drive["fresh"] and drive["sample_scan"] > initial["panel"]["scan"]:
            window[drive["sample_scan"]] = drive["rpm"]
        kept_running &= active(end, 2) and end["speed"]["run"] == initial["speed"]["run"]
    delta = end["shaft"]["shaft_degrees"] - initial["shaft"]["shaft_degrees"]
    rpm = delta / (6 * ticks * room.bench.dt)
    band = target_rpm * 0.01
    in_band = sum(abs(value - target_rpm) <= band for value in window.values())
    timestamps = [initial["panel"]["scan"], *window, end["panel"]["scan"]]
    span_ok = max(b - a for a, b in zip(timestamps, timestamps[1:])) <= 18
    return {
        "same_run_enabled": kept_running, "encoder_near_target": abs(rpm - target_rpm) <= band,
        "drive_near_target": bool(window) and in_band >= 0.95 * len(window)
                             and abs(mean(window.values()) - target_rpm) <= band,
        "drive_continuous": span_ok and len(window) >= 28 * seconds,
    }, {"dwell_s": seconds, "shaft_rpm": round(rpm, 6), "drive_samples": len(window), "drive_in_band": in_band,
         "drive_rpm": round(mean(window.values()), 6) if window else None}, end


def stopped(state):
    plc = state["speed"]["plc"]
    return (not state["panel"]["enabled"] and abs(state["shaft"]["shaft_rpm"]) <= STOP_WINDOW_RPM
            and plc["fresh"] and abs(plc["rpm"]) <= STOP_WINDOW_RPM)


def run_early_position_stop(room, marker, rng):
    args = {"position_mdeg": 8000}
    start = input_motion(room, "position", start=True, **args)
    moving = advance(room, rng.randint(20, 40))
    input_motion(room, "position", stop=True, **args)
    advance(room, 8)
    input_motion(room, "position", start=False, **args)
    halted = advance(room, 80)
    travel = halted["shaft"]["shaft_degrees"] - start["shaft"]["shaft_degrees"]
    return trial({
        "moving_before_stop": active(moving, 1) and moving["shaft"]["shaft_rpm"] > 0,
        "stopped_before_target": shaft_stopped(halted) and 0 < travel < 4,
        "feedback": linked(halted, marker),
    }, case="position_early_stop", measured_travel_deg=travel)


def run_early_speed_stop(room, rng):
    rpm = MAX_RPM
    launch(room, rpm)
    moving = advance(room, rng.randint(70, 110))
    speed_input(room, rpm, stop=True)
    advance(room, 8)
    speed_input(room, rpm)
    settle = round((room.bench.setup.ramp_seconds + 0.7) / room.bench.dt)
    halted = advance(room, settle)
    first_stop = halted["speed"]["first_stop"]
    checks = {
        "moving_during_ramp": active(moving, 2) and 0 < moving["shaft"]["shaft_rpm"] < rpm * 0.85,
        "operator_first_stop": first_stop is not None and first_stop["reason"] == "operator"
                               and first_stop["run"] == moving["speed"]["run"],
        "stopped_after_release": stopped(halted) and not room.bench.command[0],
    }
    # Leave the operator at an explicit stop before the separate plateau trial.
    speed_input(room, rpm, stop=True)
    advance(room, 50)
    return trial(checks, case="speed_early_stop",
                 rpm_before_stop=round(moving["shaft"]["shaft_rpm"], 3), first_stop=first_stop)


def run_field_speed(room, marker):
    rpm = MAX_RPM
    idle = launch(room, rpm)
    steps = round((room.bench.setup.ramp_seconds + 0.35) / room.bench.dt)
    advance(room, steps)
    checks, readings, plateau = dwell(room, rpm)
    speed_input(room, rpm, start=True, stop=True)
    settle = round((room.bench.setup.ramp_seconds + 0.7) / room.bench.dt)
    after = advance(room, settle)
    after_again = advance(room, 50)
    first_stop = after_again["speed"]["first_stop"]
    checks.update(
        preset_idle=not idle["panel"]["enabled"] and idle["shaft"]["shaft_rpm"] == 0,
        linked_feedback=linked(plateau, marker),
        drive_csv=plateau["speed"]["drive"]["mode_actual"] == 9,
        normal_stop=first_stop is not None and first_stop["reason"] == "operator"
                    and abs(first_stop["feedback_rpm"] - rpm) <= 0.01 * rpm,
        stopped_disarmed=stopped(after) and stopped(after_again) and not room.bench.command[0]
                         and after_again["speed"]["plc"]["sample_scan"] > after["speed"]["plc"]["sample_scan"]
                         and after_again["shaft"]["encoder_counts"] == after["shaft"]["encoder_counts"],
    )
    return trial(checks, case="near_max_speed", target_rpm=rpm, first_stop=first_stop, **readings)


def run_field_protection(room):
    # Re-arm through the operator interface and inject a bounded, examiner-only
    # shaft excursion. The examiner does not edit the submission's threshold.
    launch(room, 1000)
    moving = advance(room, 250)
    room.bench.inject_overspeed(MAX_TRIP_RPM + 4)
    tripped = advance(room, 1)
    final = advance(room, 1300)
    first_stop = tripped["speed"]["first_stop"]
    return trial({
        "motion_before_excursion": active(moving, 2),
        "overspeed_latched": tripped["speed"]["protection_latched"] and first_stop is not None
                             and first_stop["reason"] == "overspeed"
                             and first_stop["feedback_rpm"] > first_stop["trip_rpm"],
        "stop_state_retained": stopped(final) and final["speed"]["protection_latched"]
                               and final["speed"]["first_stop"] == first_stop,
    }, case="field_protection", first_stop=first_stop)
