"""Offline field-calibrated speed profile for the lightweight single-axis bench."""
from dataclasses import dataclass
from random import Random

from device import Bench, integer


MAX_RPM = 4000
MAX_TRIP_RPM = 4005
STOP_WINDOW_RPM = 0.2
REPLAY_BRAKING_RATE = 1657.0
REPLAY_STATUS_AT_RPM = 54.5
CSV_RX = {(0x6040, 0), (0x6060, 0), (0x60FF, 0)}
CSV_TX = {(0x6041, 0), (0x6061, 0), (0x606C, 0)}


@dataclass(frozen=True)
class SpeedSetup:
    path: str = "position"
    trip_rpm: int = MAX_RPM
    ramp_seconds: int = 5

    @classmethod
    def compile(cls, cfg):
        speed = cfg.get("field_speed", {})
        if type(speed) is not dict or not speed.keys() <= {"path", "trip_rpm", "ramp_seconds"}:
            raise ValueError("速度组态只接受 path、trip_rpm 和 ramp_seconds")
        path = speed.get("path", "position")
        if path not in ("position", "csv"):
            raise ValueError("速度路径应为 position 或 csv")
        return cls(path, integer(speed.get("trip_rpm", MAX_RPM), 15, MAX_TRIP_RPM),
                   integer(speed.get("ramp_seconds", 5), 1, 20))


def approach(current, target, step):
    return current + max(-step, min(step, target - current))


class FieldBench(Bench):
    speed_limit_rpm100 = MAX_RPM * 100

    def __init__(self, esi, startup, setup, seed):
        self.startup_setup = setup
        self.seed = seed
        super().__init__(esi, startup)

    def reset(self):
        self.rng = Random(self.seed)
        self.operator_field_rpm = 0
        super().reset()

    def download(self, project, setup=None):
        # Preserve shaft position across downloads. A fresh controller starts
        # with no speed request and no feedback snapshot from a prior run.
        was_fast = getattr(self, "field_motion", False)
        old_stop_rate = getattr(self, "stop_rate", 200.0)
        super().download(project)
        self.setup = self.startup_setup if setup is None else setup
        self.run = 0
        self.first_stop = None
        self.stop_confirmed_scan = None
        self.field_motion = was_fast and abs(self.rpm) > 0
        self.stop_rate = old_stop_rate
        self.setpoint = 0.0
        self.reference_rpm = self.rpm
        self.ramp_rate = 0.0
        self.axis_fault = self.stop_error = 0
        self.status_fault_pending = False
        self.axis_fault_due = None
        self.excursion = None
        self.protection_latched = False
        self.wait_release = self.stopping = self.field_motion
        self.drive_sample = self.plc_sample = None
        self.drive_gap_until = 0
        self.guard_noise = self.drive_noise = self.drive_bias = 0.0
        self.next_noise_scan = self.next_plc_scan = self.scan + 1
        self.csv_mapped = (CSV_RX <= self.pdos["RxPdo"].get(project.rx, set())
                           and CSV_TX <= self.pdos["TxPdo"].get(project.tx, set()))

    def status(self):
        bus, alarm = super().status()
        return bus, self.axis_fault if bus == 8 and alarm == 0 else alarm

    def replay_axis_status_fault(self):
        # Only the examiner can select the recorded incident's braking
        # prelude. This is not a threshold for generating a vendor fault.
        self.status_fault_pending = True

    def inject_overspeed(self, rpm):
        self.excursion = rpm

    def omit_drive_samples(self, scans):
        self.drive_gap_until = self.scan + integer(scans, 1, 25000)

    def _capture_stop(self, reason, feedback):
        if self.first_stop is None:
            self.first_stop = {
                "run": self.run, "scan": self.scan, "reason": reason,
                "plc_cause_code": {"operator": 0, "overspeed": 3, "axis_status": 6}.get(reason),
                "feedback_rpm": round(feedback, 3), "target_rpm": self.setpoint,
                "trip_rpm": self.setup.trip_rpm, "stop_error": self.stop_error,
                "axis_error": self.axis_fault,
            }
        if not self.stopping:
            self.stop_rate = max(50.0, abs(self.setpoint) / self.setup.ramp_seconds)
        self.status_fault_pending = False
        self.stopping = self.wait_release = True

    def _plc_feedback(self):
        amplitude = min(1.0, 0.075 + abs(self.rpm) / MAX_RPM)
        quantum = 0.11444
        return round((self.rpm + amplitude * self.guard_noise) / quantum) * quantum

    def _sample(self):
        # Bounded, correlated residue on a roughly 29 ms clock. A second,
        # slower state allows the CN5-like observation's center to move.
        if self.scan >= self.next_noise_scan:
            self.next_noise_scan = self.scan + self.rng.choice((7, 7, 7, 8))
            self.guard_noise = 0.40 * self.guard_noise + self.rng.uniform(-1.50, 1.50)
            self.drive_noise = 0.55 * self.drive_noise + self.rng.uniform(-1.85, 1.85)
            self.drive_bias = 0.985 * self.drive_bias + self.rng.uniform(-0.19, 0.19)
            if self.scan > self.drive_gap_until:
                amplitude = min(1.0, 0.70 + 0.30 * abs(self.rpm) / MAX_RPM)
                value = self.rpm + amplitude * (self.drive_noise + self.drive_bias) - 0.25
                self.drive_sample = (self.scan, round(value, 1))
        if self.scan >= self.next_plc_scan:
            self.next_plc_scan = self.scan + 50
            self.plc_sample = (self.scan, self._plc_feedback())

    def step(self, scans=1):
        integer(scans, 1, 25000)
        for _ in range(scans):
            enable, command = self.command
            request_speed = self.command_mode == 2 and command != 0 and enable
            if self.wait_release and not self.field_motion and not enable and not self.operator_start:
                self.wait_release = False
            if self.field_motion or (request_speed and abs(command) > 1000):
                self._speed_step(command / 100, request_speed)
            else:
                super().step()
                if self.first_stop or self.drive_sample is not None:
                    self._sample()

    def _speed_step(self, target, requested):
        self.scan += 1
        self.timed_out = self.scan - self.last_heartbeat >= 250
        if self.axis_fault_due == self.scan:
            self.axis_fault = 9510
        bus, alarm = self.status()
        valid = bus == 8 and alarm == 0 and not self.timed_out
        if not self.field_motion:
            if (not requested or not valid or self.wait_release or self.operator_stop
                    or self.protection_latched or (self.setup.path == "csv" and not self.csv_mapped)):
                self.active = False
                self.drive_mode = None
                previous = self.rpm
                self.rpm = approach(previous, 0.0, self.speed_ramp * self.dt)
                self.angle += (previous + self.rpm) * 3 * self.dt
                self._sample()
                return
            self.field_motion = True
            self.run += 1
            self.first_stop = None
            self.stop_confirmed_scan = None
            self.setpoint = self.reference_rpm = self.rpm
        if self.excursion is not None:
            self.rpm, self.excursion = self.excursion, None

        feedback = self._plc_feedback()
        if not self.stopping:
            if self.status_fault_pending and abs(self.rpm) <= REPLAY_STATUS_AT_RPM:
                self.stop_error = 9108
                self.axis_fault_due = self.scan + 1
                self._capture_stop("axis_status", feedback)
                self.stop_rate = REPLAY_BRAKING_RATE
            elif valid and abs(feedback) > self.setup.trip_rpm:
                self.protection_latched = True
                self._capture_stop("overspeed", feedback)
                self.stop_rate = max(self.stop_rate, 1000.0)
            elif self.operator_stop:
                self._capture_stop("operator", feedback)
            elif not valid:
                self._capture_stop("watchdog" if self.timed_out else "axis_state", feedback)
            elif not requested or target * self.setpoint < 0:
                self._capture_stop("request_withdrawn", feedback)
            elif target != self.setpoint:
                self.ramp_rate = abs(target - self.reference_rpm) / self.setup.ramp_seconds
                self.setpoint = target

        previous = self.rpm
        if self.stopping:
            self.rpm = approach(self.rpm, 0.0, self.stop_rate * self.dt)
            self.reference_rpm = self.rpm
            if self.rpm == 0:
                self.field_motion = self.stopping = False
                self.stop_confirmed_scan = self.scan
        elif self.status_fault_pending:
            self.rpm = approach(self.rpm, 0.0, REPLAY_BRAKING_RATE * self.dt)
        else:
            self.reference_rpm = approach(self.reference_rpm, self.setpoint, self.ramp_rate * self.dt)
            # Small plant lag chosen for the offline profile. The measured
            # plateau residual is modeled on the observation paths.
            self.rpm += 0.20 * (self.reference_rpm - self.rpm)
        self.active = self.field_motion
        self.drive_mode = 2 if self.active else None
        self.applied_torque = 0.0
        self.angle += (previous + self.rpm) * 3 * self.dt
        self._sample()

    def diagnostics(self):
        plc_scan, plc_rpm = self.plc_sample or (None, None)
        drive_scan, drive_rpm = self.drive_sample or (None, None)
        if self.status() != (8, 0) or (self.setup.path == "csv" and not self.csv_mapped):
            mode = None
        else:
            mode = 9 if self.setup.path == "csv" else 8
        return {
            "run": self.run, "requested_rpm": (self.operator_field_rpm or self.operator_rpm / 100)
            if self.operator_mode == 2 else 0,
            "effective_rpm": round(self.setpoint if self.field_motion and not self.stopping else 0.0, 3),
            "ramp_reference_rpm": round(self.reference_rpm, 3),
            "max_request_rpm": MAX_RPM, "feedback_trip_rpm": self.setup.trip_rpm,
            "protection_latched": self.protection_latched, "stop_error": self.stop_error,
            "first_stop": dict(self.first_stop) if self.first_stop else None,
            "stop_confirmed_scan": self.stop_confirmed_scan,
            "plc": {"rpm": plc_rpm, "sample_scan": plc_scan,
                    "fresh": plc_scan is not None and self.scan - plc_scan <= 51},
            "drive": {"rpm": drive_rpm, "sample_scan": drive_scan,
                      "fresh": drive_scan is not None and self.scan - drive_scan <= 10,
                      "mode_request": 9 if self.setup.path == "csv" else 8,
                      "mode_actual": mode,
                      "pdo_request_counts_s": round(self.reference_rpm * self.encoder_lines / 60) if mode == 9 else None,
                      "pdo_feedback_counts_s": round(self.rpm * self.encoder_lines / 60) if mode == 9 else None},
        }
