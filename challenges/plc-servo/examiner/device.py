"""One low-speed commissioning axis, advanced by an explicit 4 ms simulation clock."""
import math
from dataclasses import dataclass
from xml.etree import ElementTree


def number(value):
    return int(str(value).strip().lower().replace("#x", "0x"), 0)


def integer(value, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"需要 {low} 至 {high} 的整数")
    return value


@dataclass(frozen=True)
class Project:
    build: int
    mode: str
    address: int
    identity: tuple
    rx: int
    tx: int
    feedback: tuple | None
    counts: int
    units: float

    @classmethod
    def compile(cls, cfg):
        station, pdo, axis = cfg["station"], cfg["pdo"], cfg["axis"]
        if station["mode"] not in ("alias", "ring"):
            raise ValueError("寻址模式使用 alias 或 ring（首站为 0）")
        feedback = axis["feedback"]
        if feedback is not None:
            index, sub = feedback.split(":")
            feedback = (number(index), number(sub))
        units = axis["units_per_rev"]
        if type(units) not in (int, float) or not math.isfinite(units) or not 0 < units <= 1e6:
            raise ValueError("每转工程单位必须为有限正数")
        return cls(integer(cfg["build"], 0, 65535), station["mode"],
                   integer(station["address"], 0, 65535),
                   tuple(number(station[k]) for k in ("vendor", "product", "revision")),
                   number(pdo["rx"]), number(pdo["tx"]), feedback,
                   integer(axis["counts_per_rev"], 1, 2**31-1), units)


class Bench:
    dt = 0.004
    encoder_lines = 262144
    speed_ramp = 50.0  # rpm/s, assumed for low-speed exercises

    def __init__(self, esi, startup):
        root = ElementTree.parse(esi)
        dev = root.find(".//Descriptions/Devices/Device")
        kind = dev.find("Type")
        self.identity = (number(root.findtext("./Vendor/Id")), number(kind.get("ProductCode")),
                         number(kind.get("RevisionNo")))
        self.pdos = {direction: {number(p.findtext("Index")): {
            (number(e.findtext("Index")), number(e.findtext("SubIndex") or 0))
            for e in p.findall("Entry") if e.findtext("Index")} for p in dev.findall(direction)}
            for direction in ("RxPdo", "TxPdo")}
        self.startup = startup
        self.reset()

    def reset(self):
        self.scan = 0
        self.angle = 0.0
        self.rpm = 0.0
        self.operator_rpm = 0
        self.operator_stop = False
        self.operator_start = False
        self.operator_mode = 0
        self.operator_position = self.operator_torque = self.operator_duration = self.request_id = 0
        self.download(self.startup)

    def download(self, project):
        if not isinstance(project, Project):
            raise ValueError("需要已编译的组态")
        self.project = project
        self.command = (False, 0)
        self.heartbeat = None
        self.last_heartbeat = self.scan
        self.program_marker = 0
        self.active = False
        self.timed_out = False
        self.drive_mode = None
        self.command_mode = 0
        self.position_command = self.torque_command = 0
        self.position_goal = None
        self.applied_torque = 0.0  # 0.1 percent of rated torque, measured on the virtual drive

    def status(self):
        project = self.project
        rx = self.pdos["RxPdo"].get(project.rx, set())
        tx = self.pdos["TxPdo"].get(project.tx, set())
        addressed = project.identity == self.identity and project.address == 0
        # The synthetic drive uses alias zero at ring position zero.
        if not addressed or not {(0x6040, 0), (0x607A, 0)} <= rx or (0x6041, 0) not in tx:
            return 2, None
        if project.feedback != (0x6064, 0) or project.feedback not in tx:
            return 8, 9126
        return 8, 0

    def write(self, enabled, rpm100, heartbeat, marker, mode=0, position=0, torque=0):
        integer(enabled, 0, 1)
        integer(rpm100, -1000, 1000)
        integer(heartbeat, 0, 65535)
        integer(marker, 0, 65535)
        integer(mode, 0, 3)
        integer(position, -10000, 10000)
        integer(torque, -50, 50)
        if heartbeat != self.heartbeat:
            self.heartbeat = heartbeat
            self.last_heartbeat = self.scan
            self.timed_out = False
        self.command = bool(enabled), rpm100
        self.command_mode, self.position_command, self.torque_command = mode, position, torque
        self.program_marker = marker

    def step(self, scans=1):
        integer(scans, 1, 25000)
        bus, alarm = self.status()
        for _ in range(scans):
            self.scan += 1
            self.timed_out = self.scan - self.last_heartbeat >= 250
            self.active = self.command[0] and bus == 8 and alarm == 0 and not self.timed_out
            mode = self.command_mode if self.active else None
            if mode == 1 and self.drive_mode != 1:
                self.position_goal = self.angle + self.position_command / 1000
            elif mode != 1:
                self.position_goal = None
            self.drive_mode = mode
            old = self.rpm
            if mode == 3:
                # Simplified unloaded shaft: torque slew, breakaway friction and viscous drag.
                slew = 20 * self.dt
                self.applied_torque += max(-slew, min(slew, self.torque_command - self.applied_torque))
                effort = math.copysign(max(0, abs(self.applied_torque) - 22), self.applied_torque)
                self.rpm += (0.24 * effort - 2 * old) * self.dt
            else:
                self.applied_torque = 0.0
                target = self.command[1] / 100 if mode in (0, 2) else 0.0
                if mode == 1:
                    # Drive closes its position loop; one relative move per enable edge.
                    target = max(-2.0, min(2.0, (self.position_goal - self.angle) * 1.5))
                delta = self.speed_ramp * self.dt
                self.rpm = old + max(-delta, min(delta, target - old))
            movement = (old + self.rpm) * 3 * self.dt
            if mode == 1 and abs(self.position_goal - self.angle) < 0.0005 and abs(self.rpm) < 0.002:
                self.angle, self.rpm = self.position_goal, 0.0
            else:
                self.angle += movement

    @property
    def encoder(self):
        return round(self.angle * self.encoder_lines / 360)

    def panel(self):
        bus, alarm = self.status()
        position = self.encoder * self.project.units / self.project.counts if alarm == 0 else None
        return {"build": self.project.build, "scan": self.scan, "al_state": bus, "axis_alarm": alarm,
                "axis_position": position, "enabled": self.active, "watchdog": self.timed_out,
                "program_marker": self.program_marker, "operator_rpm100": self.operator_rpm,
                "operator_stop": self.operator_stop, "operator_start": self.operator_start, "drive_mode": self.drive_mode,
                "operator_mode": self.operator_mode, "operator_position_mdeg": self.operator_position,
                "operator_torque_permille": self.operator_torque, "duration_scans": self.operator_duration,
                "request_id": self.request_id, "torque_feedback_permille": round(self.applied_torque, 2)}

    def observer(self):
        return {"encoder_counts": self.encoder, "shaft_degrees": self.encoder * 360 / self.encoder_lines,
                "shaft_rpm": self.rpm, "applied_torque_permille": round(self.applied_torque, 2), "scan": self.scan}
