"""Verify the synthetic speed worksite and scoring with a reference submission."""
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from statistics import correlation, stdev

sys.path.insert(0, str(Path(__file__).resolve().parent))
from blind_grade import fetch, grade
from blind_room import ROOT, Room, load_packet, persist
from field_trials import dwell, launch, speed_input
from motion_trials import advance


SEED = 31415
SPEED_BRANCH = "ELSIF D12 = 2 THEN\n        D101 := D0;"
HIGH_OUTPUT = "IF D20 > 10 OR D20 < -10 THEN\n            D107 := D20;\n        END_IF;"


def fixture(packet):
    cfg = json.loads(packet["work/station.json"])
    cfg["station"]["address"] = 0
    cfg["axis"]["feedback"] = "0x6064:0"
    cfg["pdo"] = {"rx": "0x1601", "tx": "0x1A02"}
    cfg["field_speed"] = {"path": "csv", "trip_rpm": 4005, "ramp_seconds": 5}
    src = Path(__file__).with_name("reference_modes.st").read_text(encoding="utf-8")
    if src.count(SPEED_BRANCH) != 1:
        raise AssertionError("参考程序速度分支已变更")
    field_src = "D107 := 0;\n" + src.replace(SPEED_BRANCH, SPEED_BRANCH + "\n        " + HIGH_OUTPUT)
    return cfg, field_src


def put(room, cfg, src):
    room.write("work/station.json", json.dumps(cfg))
    room.write("work/main.st", src)


def station(packet, cfg, src):
    room = Room(packet, field_seed=SEED)
    put(room, cfg, src)
    room.compile_work()
    room.operate("download")
    return room


def speed_case(report, name):
    return next(item for item in report["stages"]["speed"]["cases"] if item.get("case") == name)


def verify():
    checks = {}
    def check(name, condition):
        if not condition:
            raise AssertionError(name)
        checks[name] = True

    packet = load_packet(field=True)
    cfg, src = fixture(packet)
    work_dir = ROOT / "work"
    work_dir.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=work_dir) as temp:
        db = Path(temp) / "field-submissions.sqlite3"
        cli = subprocess.run(
            [sys.executable, "-I", "-B", "-X", "utf8", str(ROOT / "examiner/blind_exam.py")],
            input=json.dumps({"tool": "station", "arguments": {"action": "observe"}}) + "\n",
            text=True, encoding="utf-8", capture_output=True, check=True, timeout=20)
        opening, state = map(json.loads, cli.stdout.splitlines())
        check("入口呈现现场题面和六个工具", "4000 rpm" in opening["messages"][1]["content"]
              and len(opening["tools"]) == 6 and state["result"]["speed"]["max_request_rpm"] == 4000)
        room = Room(packet, lambda payload: persist(payload, db), field_seed=SEED)
        check("现场补充材料未暴露考官脚本和证据",
              not room.call("read_file", {"path": "examiner/field_device.py"})["ok"]
              and "现场工位.md" in room.call("list_files", {})["result"]["files"])
        check("错误寻址导致 AL 非 OP", room.bench.status() == (2, None))
        invalid = json.loads(json.dumps(cfg))
        invalid["axis"]["feedback"] = None
        put(room, invalid, src)
        room.compile_work()
        check("修改和编译仍执行旧寻址", room.bench.status() == (2, None))
        room.operate("download")
        check("总线已 OP 而轴反馈绑定仍报 9126", room.bench.status() == (8, 9126))
        put(room, cfg, src)
        room.compile_work()
        check("正确组态编译仍保留旧报警", room.bench.status() == (8, 9126))
        room.operate("download")
        check("正确组态下发恢复轴反馈", room.bench.status() == (8, 0))
        receipt = room.submit()["receipt"]
        payload = fetch(receipt, db)
        golden = grade(payload, seed=SEED)
        check("冻结交卷自动按现场版判卷", payload["exam_version"] == 4 and golden["graded_version"] == 4
              and golden["passed"] and golden == grade(payload, seed=SEED))
    seeds = (0, 1, 2, 3, 7, SEED, 20261123, 20261124)
    check("正确提交在八个种子通过", all(grade(payload, packet, s)["passed"] for s in seeds))

    def evaluate(config, source=src):
        work = {"work/main.st": source, "work/station.json": json.dumps(config)}
        return grade({"work": work, "exam_version": 4}, packet, SEED)

    low_trip = json.loads(json.dumps(cfg))
    low_trip["field_speed"]["trip_rpm"] = 4000
    too_low = evaluate(low_trip)
    failed_max = speed_case(too_low, "near_max_speed")
    check("用给定上限作为保护线导致误停并拒收", not too_low["passed"]
          and failed_max["first_stop"]["reason"] == "overspeed")

    wrong_pdo = {**cfg, "pdo": {"rx": "0x1600", "tx": "0x1A00"}}
    rejected = evaluate(wrong_pdo)
    check("轴绑定正常但 CSV PDO 不完整时拒收", not rejected["passed"]
          and not speed_case(rejected, "near_max_speed")["checks"]["drive_csv"])

    low_only_source = src.replace("D107 := D20;", "D107 := 0;")
    no_high = evaluate(cfg, low_only_source)
    check("低速正常而最高档不动的提交拒收", not no_high["passed"]
          and not speed_case(no_high, "near_max_speed")["checks"]["encoder_near_target"])
    classic = {**wrong_pdo, "field_speed": {**cfg["field_speed"], "path": "position"}}
    old_grade = grade({"exam_version": 2, "work": {"work/main.st": low_only_source,
                       "work/station.json": json.dumps(classic)}}, seed=SEED)
    same_new_grade = evaluate(classic, low_only_source)
    check("同一份低速作品通过旧版并保有独立评分",
          old_grade["passed"] and old_grade["graded_version"] == 2
          and not same_new_grade["passed"]
          and all(case["passed"] for case in same_new_grade["stages"]["speed"]["cases"][:2]))
    capped = evaluate(cfg, src.replace("D107 := D20;", "D107 := 3900;"))
    check("最高档持续运行但实速不足的提交拒收", not capped["passed"]
          and not speed_case(capped, "near_max_speed")["checks"]["encoder_near_target"])
    no_stop = evaluate(cfg, src.replace("IF D1 <> 0 OR ", "IF (D1 <> 0 AND D20 = 0) OR "))
    check("程序忽略高速停止请求被拒收", not no_stop["passed"]
          and not speed_case(no_stop, "near_max_speed")["checks"]["stopped_disarmed"])
    no_guard = {**cfg, "field_speed": {**cfg["field_speed"], "trip_rpm": 4006}}
    check("超过设备保护边界的组态拒收", not evaluate(no_guard)["passed"])
    deadband = src.replace("D105 := D13;", "IF D13 >= 1000 OR D13 <= -1000 THEN D105 := D13; END_IF;")
    missed_small = evaluate(cfg, deadband)
    check("吞掉小于 1° 微调的提交拒收", not missed_small["passed"]
          and any(abs(c.get("requested_mdeg", 1000)) < 1000 and not c["checks"]["relative_travel"]
                  for c in missed_small["stages"]["position"]["cases"]))
    timer = "IF D16 <> D200 THEN D250 := 0; ELSE D250 := D250 + 1; END_IF;\n"
    missed_motion_stop = evaluate(cfg, timer + src.replace("IF D1 <> 0 OR ",
                                  "IF (D1 <> 0 AND (D12 <> 1 OR D250 > 350)) OR "))
    check("忽略定位途中短按停止的提交拒收", not missed_motion_stop["passed"]
          and not missed_motion_stop["stages"]["position"]["cases"][-1]["checks"]["stopped_before_target"])
    high_stop_fault = timer + src.replace("IF D1 <> 0 OR ",
                                         "IF (D1 <> 0 AND (D20 = 0 OR D250 > 350)) OR ")
    missed_ramp_stop = evaluate(cfg, high_stop_fault)
    check("设备代停但 PLC 忽略升速中短按停止的提交拒收", not missed_ramp_stop["passed"]
          and not speed_case(missed_ramp_stop, "speed_early_stop")["checks"]["stopped_after_release"])
    old_field = grade({"exam_version": 3, "work": {"work/main.st": high_stop_fault,
                       "work/station.json": json.dumps(cfg)}}, packet, SEED)
    check("现场第 3 版封卷仍按旧条目判卷", old_field["passed"] and old_field["graded_version"] == 3)

    old = {**cfg, "field_speed": {**cfg["field_speed"], "trip_rpm": 3000}}
    historical = station(packet, old, src)
    launch(historical, 3000)
    old_state = advance(historical, 1400)
    old_stop = old_state["speed"]["first_stop"]
    check("现场 3000 档故障边界可复现", old_stop is not None and old_stop["reason"] == "overspeed"
          and old_stop["feedback_rpm"] > 3000 and old_stop["axis_error"] == 0)
    normal = station(packet, cfg, src)
    launch(normal, 3000)
    new_state = advance(normal, 1400)
    check("同种子只修保护配置后 3000 档继续运行", new_state["speed"]["first_stop"] is None
          and abs(new_state["shaft"]["shaft_rpm"] - 3000) < 1)

    room = station(packet, cfg, src)
    launch(room, 500)
    old_speed = advance(room, 1320)
    speed_input(room, 1000)
    interval = []
    for _ in range(270):
        interval.append(advance(room, 5))
    first_target = next(x for x in interval if x["speed"]["drive"]["rpm"] >= 998)
    changes_time = (first_target["panel"]["scan"] - old_speed["panel"]["scan"]) * room.bench.dt
    check("500 到 1000 同轮切档约五秒且未退使能", 4.7 <= changes_time <= 5.3
          and all(x["panel"]["enabled"] and x["speed"]["run"] == old_speed["speed"]["run"] for x in interval))
    check("切档实际模式 9 且 PDO 单位与转速相符",
          interval[-1]["speed"]["drive"]["mode_actual"] == 9
          and abs(interval[-1]["speed"]["drive"]["pdo_feedback_counts_s"] * 60 / 262144 - 1000) < 1)
    legacy_cfg = {**cfg, "pdo": {"rx": "0x1600", "tx": "0x1A00"},
                  "field_speed": {**cfg["field_speed"], "path": "position"}}
    room = station(packet, legacy_cfg, src)
    launch(room, 1000)
    for _ in range(250):
        peak = advance(room, 5)
        if peak["shaft"]["shaft_rpm"] >= 593:
            break
    room.bench.replay_axis_status_fault()
    braking = advance(room, 10)
    slope = (peak["shaft"]["shaft_rpm"] - braking["shaft"]["shaft_rpm"]) / (10 * room.bench.dt)
    check("历史 1000 给定约 594 rpm 开始减速且首停槽仍为空",
          593 <= peak["shaft"]["shaft_rpm"] <= 595 and abs(slope - 1657) < 1
          and braking["speed"]["first_stop"] is None and braking["panel"]["enabled"])
    for _ in range(150):
        fault1 = advance(room, 1)
        if fault1["speed"]["first_stop"] is not None:
            break
    fault2 = advance(room, 1)
    replay = [fault1["speed"]["first_stop"], {"scan": fault2["panel"]["scan"], "axis_error": fault2["panel"]["axis_alarm"]}]
    check("合成轴状态异常先锁存 9108 下一拍读到 9510",
          replay[0]["reason"] == "axis_status" and replay[0]["stop_error"] == 9108
          and replay[0]["axis_error"] == 0 and replay[1]["axis_error"] == 9510
          and 45 <= replay[0]["feedback_rpm"] <= 55
          and replay[1]["scan"] == replay[0]["scan"] + 1)

    room = station(packet, cfg, src)
    launch(room, 4000)
    advance(room, 1350)
    drive = {}
    for _ in range(450):
        frame = advance(room, 5)["speed"]["drive"]
        drive[frame["sample_scan"]] = frame["rpm"]
    values = list(drive.values())
    scatter = stdev(values)
    corr = correlation(values[:-1], values[1:])
    check("模拟驱动分路有实测量级的采样间隔与相关波动",
          280 <= len(values) <= 320 and 0.75 < scatter < 1.8 and 0.30 < corr < 0.85)
    room.bench.omit_drive_samples(250)
    gap, _, after_gap = dwell(room, 4000)
    check("高档连续且均速够但驱动掉样会被拒收",
          gap["same_run_enabled"] and gap["encoder_near_target"] and not gap["drive_continuous"]
          and after_gap["speed"]["drive"]["fresh"])
    return {"passed": True, "checks": checks, "seeds": list(seeds), "reference": golden,
            "version_comparison": {"same_low_speed_submission": {
                "v2": {key: value["passed"] for key, value in old_grade["stages"].items()},
                "v4": {key: value["passed"] for key, value in same_new_grade["stages"].items()}}},
            "incorrect_guard": failed_max, "historic_3000_stop": old_stop, "cutover_seconds": changes_time,
            "status_replay": {"peak_rpm": round(peak["shaft"]["shaft_rpm"], 3),
                              "pre_report_braking_rpm_s": round(slope, 3), "events": replay},
            "observed_noise": {"samples": len(values),
            "std_rpm": round(scatter, 3), "lag_one_correlation": round(corr, 3)}}


if __name__ == "__main__":
    result = verify()
    print(f"PASS: {len(result['checks'])} field-profile checks, including transient stops and older grading versions")
