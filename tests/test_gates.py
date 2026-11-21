"""The safety properties, tested against a pretend printer.

Each test tries to get a print started (or the bed moved) in a situation
where that must not happen, calling the tools the way a confused or
adversarial agent could.
"""
import json
import threading
import time

import pytest

import fake_printer
from printq import bedcheck, config, daemon, jobs, slicer, tools


@pytest.fixture
def shop(tmp_path, monkeypatch):
    """A fresh data directory, one fake printer, and one job sitting in 'queued'."""
    fake_printer.STATE.update(state="standby", file="last-part.gcode", position=[202.0, 264.0, 169.7], homed="",
                              uploads=[], started=[], gcode=[], object_height=20.0)
    server = fake_printer.serve(0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    home = tmp_path / "home"
    home.mkdir()
    printer = {"url": f"http://127.0.0.1:{server.server_address[1]}", "start_mode": "auto", "inspect": {
        "mode": "raise_bed", "park_xy": [202, 264], "step": 10, "min_z": 95, "object_margin": 20, "settle_sec": 0}}
    (home / "printers.json").write_text(json.dumps({"p1": printer}))
    for name, value in (("HOME", home), ("DATA", tmp_path / "data"), ("JOBS", tmp_path / "data" / "jobs"),
                        ("DATASET", tmp_path / "data" / "dataset"), ("VISION_MODEL", "fixed:bed_clear"),
                        ("SHADOW_VISION_MODEL", ""), ("SPOOLMAN_URL", "")):
        monkeypatch.setattr(config, name, value)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    job = config.JOBS / "job-1"
    job.mkdir(parents=True)
    (job / "plate_1.gcode").write_text("; test\n")
    (job / "ticket.json").write_text(json.dumps({"id": "job-1", "title": "Test part", "printer": "p1", "material": ""}))
    (job / "state.json").write_text(json.dumps({"state": "queued", "held": False, "updated": time.time(), "slice_report": {
        "gcode": str(job / "plate_1.gcode"),
        "estimate": {"filament_g": 10.0, "filament_mm": 4000.0, "time": "30m", "layers": 50, "height_mm": 10}}}))

    def reconfigure(**changes):
        printer.update(changes)
        (home / "printers.json").write_text(json.dumps({"p1": printer}))

    yield reconfigure
    server.shutdown()


def started():
    return fake_printer.STATE["started"]


def test_clear_bed_auto_mode_starts(shop):
    assert tools.check_bed("job-1")["verdict"] == "clear"
    assert "uploaded" in tools.upload_job("job-1")
    assert tools.start_job("job-1")["started"] is True
    assert started() == ["printq_job-1.gcode"]
    assert jobs.state("job-1")["state"] == "printing"


def test_steps_cannot_be_skipped(shop):
    assert "refused" in tools.start_job("job-1")
    assert "refused" in tools.upload_job("job-1")
    assert started() == [] and fake_printer.STATE["uploads"] == []


@pytest.mark.parametrize("view", ["object", "unsure"])
def test_no_start_unless_the_bed_is_seen_clear(shop, monkeypatch, view):
    monkeypatch.setattr(config, "VISION_MODEL", f"fixed:{view}")
    assert tools.check_bed("job-1")["verdict"] in ("occupied", "cannot_tell")
    assert "refused" in tools.upload_job("job-1")
    assert "refused" in tools.start_job("job-1")
    assert started() == []


def test_a_garbage_verdict_is_not_clear(shop, monkeypatch):
    monkeypatch.setattr(config, "VISION_MODEL", "fixed:looks fine to me")
    assert tools.check_bed("job-1")["verdict"] == "cannot_tell"


def test_stale_verdict_does_not_start(shop, monkeypatch):
    tools.check_bed("job-1")
    tools.upload_job("job-1")
    monkeypatch.setattr(config, "BED_OK_TTL", 0)
    assert "refused" in tools.start_job("job-1")
    assert started() == [] and jobs.state("job-1")["state"] == "queued"


def test_busy_printer_does_not_start(shop):
    tools.check_bed("job-1")
    tools.upload_job("job-1")
    fake_printer.STATE["state"] = "printing"
    assert "refused" in tools.start_job("job-1")
    assert started() == []


def test_hold_blocks_check_and_start(shop):
    tools.check_bed("job-1")
    tools.upload_job("job-1")
    daemon.command("hold", "job-1")
    assert "refused" in tools.start_job("job-1")
    assert started() == []


def test_confirm_mode_waits_for_go(shop):
    shop(start_mode="confirm")
    tools.check_bed("job-1")
    tools.upload_job("job-1")
    assert tools.start_job("job-1")["started"] is False
    assert started() == [] and jobs.state("job-1")["state"] == "awaiting_go"
    daemon.command("go", "job-1")
    assert started() == ["printq_job-1.gcode"]


def test_veto_mode_hold_during_countdown(shop):
    shop(start_mode="veto", veto_sec=4)
    tools.check_bed("job-1")
    tools.upload_job("job-1")
    threading.Timer(1, daemon.command, ("hold", "job-1")).start()
    assert "refused" in tools.start_job("job-1")
    assert started() == []


def test_go_after_occupied_verdict_lets_operator_override(shop, monkeypatch):
    monkeypatch.setattr(config, "VISION_MODEL", "fixed:object")
    tools.check_bed("job-1")
    tools.escalate("job-1", "bed occupied")
    daemon.command("go", "job-1")
    assert tools.check_bed("job-1")["verdict"] == "clear"  # vouched, no camera needed
    tools.upload_job("job-1")
    assert tools.start_job("job-1")["started"] is True


def zs():
    return fake_printer.STATE["position"][2]


def test_bed_is_raised_in_steps_but_never_past_the_floor(shop, monkeypatch):
    monkeypatch.setattr(config, "VISION_MODEL", "fixed:bed_not_visible")
    out = bedcheck.check(config.printer("p1"))
    assert out["verdict"] == "cannot_tell"
    assert zs() >= 95 and out["steps"] == 7  # 169.7 -> 99.7, the next step would cross 95
    assert not any("G28" in g for g in fake_printer.STATE["gcode"])  # never homes at a known park


def test_tall_last_object_keeps_the_bed_where_it_is(shop, monkeypatch):
    monkeypatch.setattr(config, "VISION_MODEL", "fixed:bed_not_visible")
    fake_printer.STATE["object_height"] = 150.0
    assert bedcheck.check(config.printer("p1"))["steps"] == 0
    assert zs() == 169.7 and fake_printer.STATE["gcode"] == []


def test_unknown_position_means_no_motion(shop, monkeypatch):
    monkeypatch.setattr(config, "VISION_MODEL", "fixed:bed_not_visible")
    fake_printer.STATE.update(position=[0.0, 0.0, 0.0])
    out = bedcheck.check(config.printer("p1"))
    assert out["verdict"] == "cannot_tell" and "park" in out["reason"]
    fake_printer.STATE.update(position=[120.0, 120.0, 169.7], homed="xy")  # left over the bed
    assert bedcheck.check(config.printer("p1"))["verdict"] == "cannot_tell"
    assert fake_printer.STATE["gcode"] == []


def test_unknown_object_height_still_raises_from_the_park(shop, monkeypatch):
    monkeypatch.setattr(config, "VISION_MODEL", "fixed:bed_not_visible")
    fake_printer.STATE["object_height"] = None
    out = bedcheck.check(config.printer("p1"))
    assert out["steps"] == 7 and zs() >= 95


def test_after_a_restart_xy_is_homed_to_prove_the_park_when_configured(shop, monkeypatch):
    views = iter(["bed_not_visible", "bed_not_visible", "bed_not_visible", "bed_clear"])
    monkeypatch.setattr(bedcheck, "judge", lambda *a: {"view": next(views), "description": "seen"})
    fake_printer.STATE.update(position=[0.0, 0.0, 0.0], file="", object_height=None)
    cfg = config.printer("p1")
    cfg["inspect"].update(unknown_position="home_xy", rest_z=170)
    out = bedcheck.check(cfg)
    sent = fake_printer.STATE["gcode"]
    assert out["verdict"] == "clear" and out["steps"] == 3
    assert sent[0] == "G28 X Y" and "X202 Y264" in sent[1] and sent[2].startswith("SET_KINEMATIC_POSITION")
    assert not any("G28" in g and "Z" in g[3:] for g in sent)  # Z is never homed
    assert sent[-1] == "M84" and fake_printer.STATE["homed"] == ""  # the assumed Z is forgotten again


def test_restart_recovery_is_bounded_by_the_assumed_height(shop, monkeypatch):
    monkeypatch.setattr(config, "VISION_MODEL", "fixed:bed_not_visible")
    fake_printer.STATE.update(position=[0.0, 0.0, 0.0], file="", object_height=None)
    cfg = config.printer("p1")
    cfg["inspect"].update(unknown_position="home_xy", rest_z=170)
    out = bedcheck.check(cfg)
    assert out["verdict"] == "cannot_tell" and out["steps"] == 7  # 170 -> 100, never past min_z 95
    cfg["inspect"]["rest_z"] = 100  # nothing to gain: no homing, no motion
    fake_printer.STATE.update(position=[0.0, 0.0, 0.0], homed="", gcode=[])
    assert bedcheck.check(cfg)["steps"] == 0 and fake_printer.STATE["gcode"] == []


def test_object_seen_while_raising_stops_the_motion(shop, monkeypatch):
    views = iter(["bed_not_visible", "bed_not_visible", "object"])
    monkeypatch.setattr(bedcheck, "judge", lambda *a: {"view": next(views), "description": "part rising into view"})
    out = bedcheck.check(config.printer("p1"))
    assert out["verdict"] == "occupied" and out["steps"] == 2


def test_state_machine_rejects_illegal_moves(shop):
    with pytest.raises(jobs.JobError):
        jobs.transition("job-1", "printing")


def test_override_of_unknown_setting_is_an_error(tmp_path):
    for part in slicer.PARTS:
        (tmp_path / f"{part}.json").write_text(json.dumps({"layer_height": "0.2", "nozzle_temperature": ["250"]}))
    with pytest.raises(jobs.JobError):
        slicer.merge_overrides(tmp_path, {"no_such_setting": 1}, tmp_path)
    out = slicer.merge_overrides(tmp_path, {"nozzle_temperature": 260}, tmp_path)
    assert json.loads(out["filament"].read_text())["nozzle_temperature"] == ["260"]
