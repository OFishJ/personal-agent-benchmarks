"""Exercise one loaded workpiece through operator inputs and independent instrument readings."""


def advance(room, scans):
    while scans > 250:
        room.operate("advance", scans=250)
        scans -= 250
    return room.operate("advance", scans=scans)


def input_motion(room, mode, **request):
    return room.operate("input", mode=mode, **{"stop": False, **request})


def stopped(state):
    return not state["panel"]["enabled"] and state["shaft"]["shaft_rpm"] == 0


def linked(state, marker):
    p, s = state["panel"], state["shaft"]
    return (p["program_marker"] == marker and p["axis_alarm"] == 0 and p["al_state"] == 8
            and p["axis_position"] is not None and abs(p["axis_position"] - s["shaft_degrees"]) < 0.002)


def active(state, mode):
    return state["panel"]["enabled"] and state["panel"]["drive_mode"] == mode


def trial(checks, **readings):
    return {"passed": all(checks.values()), "checks": checks, **readings}


def group(cases):
    return {"passed": all(case["passed"] for case in cases), "cases": cases}


def run_jog(room, marker, rng):
    cases = []
    for sign in (1, -1):
        rpm = sign * rng.randint(150, 550)
        before = input_motion(room, "jog", rpm100=rpm)["shaft"]["encoder_counts"]
        moving = advance(room, rng.randint(75, 110))
        input_motion(room, "jog", rpm100=rpm, start=False)
        released = advance(room, 60)
        input_motion(room, "jog", rpm100=rpm)
        advance(room, 60)
        input_motion(room, "jog", rpm100=rpm, stop=True, start=True)
        halted = advance(room, 80)
        shaft = moving["shaft"]
        cases.append(trial({
            "direction": (shaft["encoder_counts"] - before) * sign > 0,
            "speed": abs(shaft["shaft_rpm"] - rpm / 100) < 0.02 and active(moving, 0),
            "release": stopped(released), "stop_priority": stopped(halted),
            "feedback": linked(moving, marker) and linked(halted, marker)},
            rpm100=rpm, measured_rpm=shaft["shaft_rpm"], encoder_counts=shaft["encoder_counts"]))
    return group(cases)


def run_position(room, marker, rng):
    cases = []
    for sign in (1, -1):
        command = sign * rng.randint(1100, 9500)
        before = input_motion(room, "position", position_mdeg=command)["shaft"]["shaft_degrees"]
        moving = advance(room, 20)
        input_motion(room, "position", position_mdeg=command, start=False)
        settled = advance(room, 560)
        held = advance(room, 120)
        input_motion(room, "position", position_mdeg=command, stop=True)
        halted = advance(room, 80)
        actual = settled["shaft"]["shaft_degrees"] - before
        cases.append(trial({
            "position_mode": active(moving, 1),
            "encoder_direction": (moving["shaft"]["shaft_degrees"] - before) * sign > 0,
            "relative_travel": abs(actual - command / 1000) <= 0.025,
            "hold": abs(held["shaft"]["shaft_degrees"] - settled["shaft"]["shaft_degrees"]) <= 0.01
                    and abs(held["shaft"]["shaft_rpm"]) <= 0.01,
            "stop": stopped(halted), "feedback": linked(settled, marker) and linked(halted, marker)},
            requested_mdeg=command, initial_deg=before, measured_travel_deg=actual,
            held_deg=held["shaft"]["shaft_degrees"]))
    return group(cases)


def run_speed(room, marker, rng):
    cases = []
    for sign in (1, -1):
        rpm = sign * rng.randint(160, 480)
        initial = input_motion(room, "speed", rpm100=rpm, start=False)
        preset = advance(room, 80)
        input_motion(room, "speed", rpm100=rpm)
        advance(room, 6)
        input_motion(room, "speed", rpm100=rpm, start=False)
        steady = advance(room, 110)
        ticks = rng.randint(70, 145)
        later = advance(room, ticks)
        delta = later["shaft"]["shaft_degrees"] - steady["shaft"]["shaft_degrees"]
        sampled_rpm = delta / (6 * ticks * room.bench.dt)
        input_motion(room, "speed", rpm100=rpm, stop=True, start=True)
        halted = advance(room, 80)
        cases.append(trial({
            "preset_idle": stopped(preset) and preset["shaft"]["encoder_counts"] == initial["shaft"]["encoder_counts"],
            "continuous_after_release": active(steady, 2) and active(later, 2),
            "speed": abs(steady["shaft"]["shaft_rpm"] - rpm / 100) < 0.02,
            "encoder_speed": abs(sampled_rpm - rpm / 100) < 0.02,
            "stop_priority": stopped(halted), "feedback": linked(later, marker) and linked(halted, marker)},
            rpm100=rpm, measured_rpm=steady["shaft"]["shaft_rpm"], encoder_rpm=sampled_rpm))
    return group(cases)


def run_torque(room, marker, rng):
    cases = []
    # First apply a sub-breakaway load. Then check positive and negative torque above it.
    inputs = [(rng.randint(3, 12), rng.randint(220, 320)), (rng.randint(40, 50), rng.randint(700, 850)),
              (-rng.randint(40, 50), rng.randint(980, 1250))]
    for command, duration in inputs:
        args = {"torque_permille": command, "duration_ms": duration * 4}
        before = input_motion(room, "torque", **args)["shaft"]["shaft_degrees"]
        early = advance(room, 30)
        input_motion(room, "torque", start=False, **args)
        live = advance(room, duration - 40)
        deadline = advance(room, 12)
        expired = advance(room, 80)
        waiting = advance(room, 100)
        input_motion(room, "torque", stop=True, **args)
        advance(room, 60)
        shaft = live["shaft"]
        travel = shaft["shaft_degrees"] - before
        cases.append(trial({
            "torque_mode": active(early, 3) and active(live, 3),
            "ramp": 0 < early["shaft"]["applied_torque_permille"] / command < 1,
            "torque_feedback": abs(shaft["applied_torque_permille"] - command) < 0.6,
            "shaft_response": abs(travel) <= 0.01 if abs(command) < 22 else travel * (1 if command > 0 else -1) > 0.1,
            "timed_stop": not deadline["panel"]["enabled"] and stopped(expired) and expired["shaft"]["applied_torque_permille"] == 0,
            "stays_stopped": stopped(waiting) and waiting["shaft"]["encoder_counts"] == expired["shaft"]["encoder_counts"],
            "feedback": linked(live, marker) and linked(expired, marker)},
            requested_permille=command, duration_ms=duration*4, measured_permille=shaft["applied_torque_permille"],
            measured_travel_deg=travel, early_permille=early["shaft"]["applied_torque_permille"]))
    input_motion(room, "torque", torque_permille=45, duration_ms=6000)
    moving = advance(room, 560)
    input_motion(room, "torque", torque_permille=45, duration_ms=6000, stop=True, start=True)
    halted = advance(room, 80)
    cases.append(trial({"moving_before_stop": active(moving, 3) and moving["shaft"]["shaft_rpm"] > 0.1,
                        "manual_stop_before_expiry": stopped(halted) and halted["shaft"]["applied_torque_permille"] == 0},
                       duration_ms=6000, stop_at_ms=2240))
    return group(cases)
