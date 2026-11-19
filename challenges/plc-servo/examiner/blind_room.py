"""Candidate-visible data and instrument operations. Host pathnames never enter the tool dispatcher."""
import base64
import hashlib
import io
import json
import re
import secrets
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType

from device import Bench, Project
from st_control import Program

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = Path(__file__).with_name("blind_manifest.json")
STORE = ROOT / "work" / "blind-submissions.sqlite3"
MODES = ("jog", "position", "speed", "torque")
EXAM_INPUTS = {f"D{i}" for i in range(20)}
MAX_FILE = 65536
MAX_WORK = 256 * 1024


def virtual_name(name):
    if not isinstance(name, str) or len(name) > 180:
        raise ValueError("无效的考场文件名")
    for part in name.split("/"):
        if not re.fullmatch(r"[\w\-（）][\w .\-（）]{0,88}", part) or part.endswith((" ", ".")):
            raise ValueError("请输入工位列表中的相对文件名")
    return name


def load_packet(candidate_root=None):
    files = {}
    root = (candidate_root or ROOT / "candidate").resolve()
    for name, digest in json.loads(MANIFEST.read_text(encoding="utf-8")).items():
        virtual_name(name)
        path = root / name.removeprefix("work/")
        if path.resolve() != path or not path.is_file():
            raise RuntimeError("开工材料的位置已变化")
        content = path.read_bytes()
        if hashlib.sha256(content).hexdigest() != digest:
            raise RuntimeError("开工材料与封存清单不符")
        files[name] = content
    return MappingProxyType(files)


def num(value, lo, hi):
    if type(value) is not int or not lo <= value <= hi:
        raise ValueError(f"整数应在 {lo} 至 {hi} 范围内")
    return value


def persist(payload, store=STORE):
    """Only trusted callers choose the database. Candidate file names remain JSON keys."""
    data = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    receipt = uuid.uuid4().hex
    digest = hashlib.sha256(data).hexdigest()
    store.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(store, timeout=5)
    try:
        with db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("CREATE TABLE IF NOT EXISTS submissions (id TEXT PRIMARY KEY, utc TEXT, sha256 TEXT, data BLOB)")
            count, size = db.execute("SELECT COUNT(*), COALESCE(SUM(length(data)),0) FROM submissions").fetchone()
            if count >= 32 or size + len(data) > 16 * 1024 * 1024:
                raise ValueError("本地交卷库已达保留上限，请考官处理")
            db.execute("INSERT INTO submissions VALUES(?,?,?,?)",
                       (receipt, datetime.now(timezone.utc).isoformat(), digest, data))
    finally:
        db.close()
    return {"receipt": receipt, "sha256": digest}


class Room:
    def __init__(self, packet, save=persist):
        self.packet = packet
        self.jog_checkpoint = None
        self.files = dict(packet)
        self.save = save
        self.closed = False
        self.compiled = None
        self.program = None
        self.marker = 0
        self.calls = 0
        self.bench = Bench(io.BytesIO(self.files["usb/drive.xml"]),
                           Project.compile(json.loads(self.files["work/station.json"])))

    def opening(self):
        return [{"role":"system", "content":self.files["AGENTS.md"].decode("utf-8")},
                {"role":"user", "content":self.files["任务.md"].decode("utf-8")}]

    def content(self, name):
        name = virtual_name(name)
        if name not in self.files:
            raise ValueError("本工位没有该文件")
        return self.files[name]

    def read(self, path, offset=0, limit=3200, format="text"):
        raw = self.content(path)
        start, count = num(offset,0,2**24), num(limit,1,8192)
        if format == "base64":
            part = base64.b64encode(raw[start:start+count]).decode("ascii")
            total = len(raw)
        elif format == "text":
            try:
                text = raw.decode("utf-8-sig")
                if "\0" in text:
                    raise UnicodeError
            except UnicodeError:
                raise ValueError("二进制材料请使用 base64 分页读取") from None
            part, total = text[start:start+count], len(text)
        else:
            raise ValueError("format 可取 text 或 base64")
        return {"data":part,"total":total,"next":start+count if start+count<total else None}

    def search(self, path, text, offset=0):
        raw = self.content(path)
        if not isinstance(text,str) or not 1 <= len(text) <= 128:
            raise ValueError("查找字符串应为 1 至 128 字符")
        content = raw.decode("utf-8-sig")
        pos = content.find(text,num(offset,0,2**24))
        return {"offset":pos,"excerpt":content[max(0,pos-80):pos+len(text)+160] if pos>=0 else ""}

    def write(self, path, text):
        path = virtual_name(path)
        if not path.startswith("work/") or not isinstance(text,str):
            raise ValueError("请将文本写到 work/ 中")
        data = text.encode("utf-8")
        sizes = [len(v) for k,v in self.files.items() if k.startswith("work/") and k!=path]
        if len(data)>MAX_FILE or len(sizes)>=48 or sum(sizes)+len(data)>MAX_WORK:
            raise ValueError("工作副本达到存储上限")
        self.files[path]=data
        return {"bytes":len(data)}

    def compile_work(self):
        self.compiled=None
        cfg=json.loads(self.files["work/station.json"])
        project=Project.compile(cfg)
        src=self.files["work/main.st"].decode("utf-8-sig")
        program=Program(src, EXAM_INPUTS)
        if len(program.code)>256:
            raise ValueError("单次扫描最多支持 256 条语句")
        self.compiled=(project,src)
        return {"compiled_build":project.build,"instructions":len(program.code)}

    def set_input(self, mode, stop, rpm100=None, position_mdeg=None, torque_permille=None, duration_ms=None, start=None):
        if mode not in MODES or type(stop) is not bool:
            raise ValueError("请选择有效模式并给出布尔停止请求")
        if start is not None and type(start) is not bool:
            raise ValueError("启动按钮需要布尔值")
        pressed = (not stop) if start is None else start
        values = {"rpm100": rpm100, "position_mdeg": position_mdeg,
                  "torque_permille": torque_permille, "duration_ms": duration_ms}
        allowed = {"jog": {"rpm100"}, "speed": {"rpm100"}, "position": {"position_mdeg"},
                   "torque": {"torque_permille", "duration_ms"}}[mode]
        if any(value is not None and key not in allowed for key, value in values.items()):
            raise ValueError("给定与所选模式不符")
        speed = num(rpm100 if rpm100 is not None else 0, -1000, 1000)
        position = num(position_mdeg if position_mdeg is not None else 0, -10000, 10000)
        torque = num(torque_permille if torque_permille is not None else 0, -50, 50)
        duration = num(duration_ms if duration_ms is not None else 3000, 500, 6000)
        b = self.bench
        b.operator_rpm = 0 if mode == "jog" and not pressed and not stop else speed
        b.operator_stop = stop
        b.operator_mode = MODES.index(mode)
        b.operator_position, b.operator_torque = position, torque
        b.operator_duration = (duration + 3) // 4 if mode == "torque" else 0
        b.operator_start = pressed
        if pressed and not stop:
            b.request_id = b.request_id % 32767 + 1

    def check_jog(self):
        from motion_trials import run_jog
        other = Room(self.packet)
        for name in ("work/main.st", "work/station.json"):
            other.files[name] = self.files[name]
        other.compile_work()
        marker = other.operate("download")["requested_marker"]
        result = run_jog(other, marker, secrets.SystemRandom())
        if result["passed"]:
            self.jog_checkpoint = {name: hashlib.sha256(other.files[name]).hexdigest()
                                   for name in ("work/main.st", "work/station.json")}
        return result

    def operate(self, action, rpm100=None, stop=None, scans=None, mode=None, position_mdeg=None,
                torque_permille=None, duration_ms=None, start=None):
        fields = (rpm100, stop, mode, position_mdeg, torque_permille, duration_ms, start)
        if action=="input":
            if scans is not None:
                raise ValueError("操作员输入不接收扫描次数")
            self.set_input("jog" if mode is None else mode, stop, rpm100, position_mdeg, torque_permille, duration_ms, start)
        elif action=="advance":
            if any(value is not None for value in fields):
                raise ValueError("advance 只接收 scans")
            self.advance(num(scans,1,250))
        else:
            if any(x is not None for x in (*fields, scans)):
                raise ValueError("该动作不接收额外参数")
            if action=="discover":
                vendor,product,rev=self.bench.identity
                return {"device":"LAB-SERVO-1","ring":0,"actual_alias":0,"vendor":vendor,"product":product,"revision":rev}
            if action=="compile":
                return self.compile_work()
            if action=="checkpoint":
                return self.check_jog()
            if action=="download":
                if self.compiled is None:
                    raise ValueError("先成功编译工作副本")
                cfg,src=self.compiled
                self.bench.download(cfg)
                self.program=Program(src, EXAM_INPUTS)
                self.marker=secrets.randbelow(32000)+1
                return {"loaded_build":cfg.build,"requested_marker":self.marker}
            if action=="reset":
                self.compiled=self.program=None
                self.marker=0
                self.bench.reset()
            elif action!="observe":
                raise ValueError("未知设备动作")
        return {"panel":self.bench.panel(),"shaft":self.bench.observer()}

    def advance(self, count):
        for _ in range(count):
            if self.program is not None:
                b = self.bench
                panel = b.panel()
                pos = round((panel["axis_position"] or 0) * 1000) & 0xffffffff
                alarm = panel["axis_alarm"]
                registers = [
                    b.operator_rpm, int(b.operator_stop), panel["al_state"], 65535 if alarm is None else alarm,
                    pos >> 16, pos & 65535, int(b.active), int(b.timed_out), b.project.build, b.scan & 65535,
                    b.program_marker, self.marker, b.operator_mode, b.operator_position, b.operator_torque,
                    b.operator_duration, b.request_id, round(b.applied_torque),
                    -1 if b.drive_mode is None else b.drive_mode, int(b.operator_start),
                ]
                enable,speed,heart,marker=self.program.scan(registers,self.marker)
                mem = self.program.mem
                try:
                    self.bench.write(enable, speed, heart & 65535, marker & 65535, mem.get("D104", 0),
                                     mem.get("D105", 0), mem.get("D106", 0))
                except ValueError:
                    self.program=None
                    self.bench.write(0,0,0,0)
                    self.bench.step()
                    raise ValueError("程序输出超过虚拟轴接口范围，已停止程序") from None
            self.bench.step()

    def submit(self, note=""):
        if not isinstance(note,str) or len(note)>4000:
            raise ValueError("交付说明最长 4000 字符")
        work={k:v.decode("utf-8") for k,v in self.files.items() if k.startswith("work/")}
        result=self.save({"work":work,"note":note,"exam_version":2,"jog_checkpoint":self.jog_checkpoint})
        self.closed=True
        return {"submitted":True,**result}

    def call(self, tool, arguments):
        if self.closed:
            return {"ok":False,"error":"本轮已封卷"}
        self.calls+=1
        if self.calls>1500:
            self.closed=True
            return {"ok":False,"error":"本轮工具调用已达上限"}
        routes={"list_files":lambda:{"files":sorted(self.files)}, "read_file":self.read,
                "find_text":self.search,"write_file":self.write,"station":self.operate,"submit":self.submit}
        if type(tool) is not str or tool not in routes or type(arguments) is not dict:
            return {"ok":False,"error":"本场没有这项工具或参数无效"}
        try:
            return {"ok":True,"result":routes[tool](**arguments)}
        except (ValueError,UnicodeError,SyntaxError,KeyError,TypeError,AttributeError,OverflowError,RecursionError) as exc:
            # Python errors can identify implementation files or functions; return only
            # approved operational diagnostics and never echo tracebacks to the candidate.
            if type(exc) is ValueError and str(exc) and str(exc)[0] >= "\u3400":
                message=str(exc)[:200]
            else:
                message="输入格式、组态或 ST 语法不受支持"
            return {"ok":False,"error":message}
