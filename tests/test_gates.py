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
from printq import bedcheck, bedlevel, config, daemon, jobs, macro, moonraker, slicer, tools


class FakeGauge:
    """Bed height by camera, played by the fake printer's own Z: the bed enters the frame 160 mm down
    and its far edge is at the reference row when the bed is at the top."""
    ok, target = True, 0.25

    def __init__(self, references):
        pass

    def row(self, jpeg):
        z = fake_printer.STATE["true_z"]
        return None if z > 160 else round(0.25 + 0.75 * z / 160, 4)


@pytest.fixture
def shop(tmp_path, monkeypatch):
    """A fresh data directory, one fake printer, and one job sitting in 'queued'."""
    fake_printer.STATE.update(state="standby", file="last-part.gcode", position=[202.0, 264.0, 169.7], homed="",
                              uploads=[], started=[], gcode=[], object_height=20.0, true_z=200.0,
                              cooldown=None, history="completed")
    server = fake_printer.serve(0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    home = tmp_path / "home"
    home.mkdir()
    printer = {"url": f"http://127.0.0.1:{server.server_address[1]}", "start_mode": "auto", "inspect": {
        "mode": "raise_bed", "park_xy": [202, 264], "step": 10, "object_margin": 20, "settle_sec": 0}}
    (home / "printers.json").write_text(json.dumps({"p1": printer}))
    for name, value in (("HOME", home), ("DATA", tmp_path / "data"), ("JOBS", tmp_path / "data" / "jobs"),
                        ("DATASET", tmp_path / "data" / "dataset"), ("VISION_MODEL", "fixed:bed_clear"),
                        ("SHADOW_VISION_MODEL", ""), ("SPOOLMAN_URL", "")):
        monkeypatch.setattr(config, name, value)
    monkeypatch.setattr(bedlevel, "Gauge", FakeGauge)
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


class NoGauge(FakeGauge):
    """A printer without reference frames: the camera cannot measure bed height."""
    ok, target = False, None

    def row(self, jpeg):
        return None


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
    assert "refused" in tools.escalate("job-1", "bed occupied")  # the queue is already waiting for the bed
    daemon.command("go", "job-1")
    assert tools.check_bed("job-1")["verdict"] == "clear"  # vouched, no camera needed
    tools.upload_job("job-1")
    assert tools.start_job("job-1")["started"] is True


def zs():
    return fake_printer.STATE["position"][2]


def true_z():
    return fake_printer.STATE["true_z"]


def test_bed_rises_to_inspection_height_and_never_past_it(shop, monkeypatch):
    out = bedcheck.check(config.printer("p1"))  # the judge calls every frame clear, from far down already
    assert out["verdict"] == "clear" and out["steps"] == 19  # 200 -> 10: within one raise of the top
    assert 0 < true_z() <= 15
    sent = fake_printer.STATE["gcode"]
    assert not any("G28" in g for g in sent)  # never homes at a known park
    assert "Z=250" in sent[0]  # Klipper's own stale 169.7 is not used for anything
    assert sent[-1] == "M84"  # and the placeholder is forgotten again


@pytest.mark.parametrize("start", [258.0, 120.0, 14.0, 3.0])
def test_wherever_the_bed_really_is_it_stops_short_of_the_top(shop, monkeypatch, start):
    # Klipper says 169.7 throughout; the bed has sunk, or was moved up by hand, since the motors went off.
    monkeypatch.setattr(config, "VISION_MODEL", "fixed:bed_not_visible")  # a judge that never says stop
    fake_printer.STATE["true_z"] = start
    out = bedcheck.check(config.printer("p1"))
    assert out["verdict"] == "cannot_tell" and "inspection height" in out["reason"]
    assert 0 < true_z() <= 15


def test_number_of_raises_is_bounded(shop, monkeypatch):
    monkeypatch.setattr(config, "VISION_MODEL", "fixed:bed_not_visible")
    cfg = config.printer("p1")
    cfg["inspect"]["max_steps"] = 4
    assert bedcheck.check(cfg)["steps"] == 4


def test_unknown_height_without_reference_frames_means_no_motion(shop, monkeypatch):
    monkeypatch.setattr(bedlevel, "Gauge", NoGauge)
    monkeypatch.setattr(config, "VISION_MODEL", "fixed:bed_not_visible")
    out = bedcheck.check(config.printer("p1"))
    assert out["verdict"] == "cannot_tell" and "reference" in out["reason"] and fake_printer.STATE["gcode"] == []


def test_toolhead_must_be_seen_at_the_park_when_a_reference_is_configured(shop, monkeypatch, tmp_path):
    ref = tmp_path / "park.jpg"
    ref.write_bytes(b"jpeg")
    cfg = config.printer("p1")
    cfg["inspect"]["park_reference"] = str(ref)
    for seen in ("elsewhere", "not_visible"):
        monkeypatch.setattr(config, "VISION_MODEL", f"fixed:bed_not_visible:{seen}")
        out = bedcheck.check(cfg)
        assert out["verdict"] == "cannot_tell" and "park" in out["reason"] and fake_printer.STATE["gcode"] == []
    monkeypatch.setattr(config, "VISION_MODEL", "fixed:bed_not_visible:parked")
    assert bedcheck.check(cfg)["steps"] == 19
    cfg["inspect"]["park_reference"] = str(tmp_path / "missing.jpg")  # unreadable reference: no motion
    fake_printer.STATE.update(position=[202.0, 264.0, 169.7], homed="", gcode=[], true_z=200.0)
    assert bedcheck.check(cfg)["verdict"] == "cannot_tell" and fake_printer.STATE["gcode"] == []


def test_tall_last_object_keeps_the_bed_where_it_is_when_z_is_real(shop, monkeypatch):
    monkeypatch.setattr(config, "VISION_MODEL", "fixed:bed_not_visible")
    fake_printer.STATE.update(object_height=150.0, homed="xyz")  # motors held since the print: Z is real
    assert bedcheck.check(config.printer("p1"))["steps"] == 0
    assert fake_printer.STATE["gcode"] == []


def test_real_z_goes_no_higher_than_stop_z(shop, monkeypatch):
    monkeypatch.setattr(bedlevel, "Gauge", NoGauge)  # a Z that Klipper really knows is enough to raise by
    monkeypatch.setattr(config, "VISION_MODEL", "fixed:bed_not_visible")
    fake_printer.STATE.update(position=[202.0, 264.0, 60.0], homed="xyz", object_height=None)
    out = bedcheck.check(config.printer("p1"))
    assert out["steps"] == 5 and fake_printer.STATE["position"][2] == 10.0  # the next step would cross Z=1


def test_unknown_position_means_no_motion(shop, monkeypatch):
    monkeypatch.setattr(config, "VISION_MODEL", "fixed:bed_not_visible")
    fake_printer.STATE.update(position=[0.0, 0.0, 0.0])
    out = bedcheck.check(config.printer("p1"))
    assert out["verdict"] == "cannot_tell" and "park" in out["reason"]
    fake_printer.STATE.update(position=[120.0, 120.0, 169.7], homed="xy")  # left over the bed
    assert bedcheck.check(config.printer("p1"))["verdict"] == "cannot_tell"
    assert fake_printer.STATE["gcode"] == []


def test_after_a_restart_xy_is_homed_to_prove_the_park_when_configured(shop, monkeypatch):
    fake_printer.STATE.update(position=[0.0, 0.0, 0.0], file="", object_height=None)
    cfg = config.printer("p1")
    cfg["inspect"].update(unknown_position="home_xy")
    out = bedcheck.check(cfg)
    sent = fake_printer.STATE["gcode"]
    assert out["verdict"] == "clear" and out["steps"] == 19
    assert sent[0] == "G28 X Y" and "X202 Y264" in sent[1] and sent[2].startswith("SET_KINEMATIC_POSITION")
    assert not any("G28" in g and "Z" in g[3:] for g in sent)  # Z is never homed
    assert sent[-1] == "M84" and fake_printer.STATE["homed"] == ""  # the placeholder Z is forgotten again


def test_toolhead_left_off_the_park_is_parked_first_and_then_has_to_be_seen_there(shop, monkeypatch, tmp_path):
    ref = tmp_path / "park.jpg"
    ref.write_bytes(b"jpeg")
    cfg = config.printer("p1")
    cfg["inspect"].update(unknown_position="home_xy", park_reference=str(ref))
    fake_printer.STATE.update(position=[0.0, 0.0, 0.0], file="", object_height=None)  # Klipper restarted

    def judged(parks):
        def judge(*a):
            moved = any("X202 Y264" in g for g in fake_printer.STATE["gcode"])
            return {"view": "bed_not_visible", "description": "seen",
                    "toolhead": "parked" if moved and parks else "elsewhere"}
        return judge

    monkeypatch.setattr(bedcheck, "judge", judged(parks=False))  # parked by Klipper, the camera disagrees
    out = bedcheck.check(cfg)
    sent = fake_printer.STATE["gcode"]
    assert out["verdict"] == "cannot_tell" and "park" in out["reason"] and out["steps"] == 0
    assert sent[0] == "G28 X Y" and not any("G1 Z" in g for g in sent)  # the bed never moved

    fake_printer.STATE.update(position=[0.0, 0.0, 0.0], homed="", gcode=[])
    monkeypatch.setattr(bedcheck, "judge", judged(parks=True))
    assert bedcheck.check(cfg)["steps"] > 0  # seen parked after the move: the raise goes ahead


def test_object_seen_while_raising_stops_the_motion(shop, monkeypatch):
    views = iter(["bed_not_visible", "bed_not_visible", "object"])
    monkeypatch.setattr(bedcheck, "judge", lambda *a: {"view": next(views), "description": "part rising into view"})
    out = bedcheck.check(config.printer("p1"))
    assert out["verdict"] == "occupied" and out["steps"] == 2


def test_object_entering_at_the_bottom_edge_gets_one_confirming_raise(shop, monkeypatch):
    def judged(*seq):
        answers = iter(seq)
        monkeypatch.setattr(bedcheck, "judge", lambda *a: dict(zip(("view", "object_at"), next(answers)),
                                                                description="seen", toolhead="parked"))
        fake_printer.STATE.update(position=[202.0, 264.0, 169.7], homed="", gcode=[], true_z=200.0)
        return bedcheck.check(config.printer("p1"))

    edge, nothing = ("object", "bottom_edge"), ("bed_not_visible", "none")
    out = judged(nothing, edge, ("object", "higher"))
    assert out["verdict"] == "occupied" and out["steps"] == 2  # one raise to find it, one to confirm
    out = judged(nothing, edge, edge, edge)
    assert out["verdict"] == "occupied" and out["steps"] == 2  # still at the edge: no third raise
    out = judged(nothing, edge, nothing)
    assert out["verdict"] == "occupied" and out["steps"] == 2  # a sighting is not forgotten
    out = judged(nothing, edge, ("unsure", "none"))
    assert out["verdict"] == "occupied"
    out = judged(nothing, edge, ("bed_clear", "none"))
    assert out["verdict"] == "occupied"  # nor overruled by a later "clear"
    out = judged(("object", "higher"))
    assert out["verdict"] == "occupied" and out["steps"] == 0  # anywhere else: no motion at all


def test_confirming_raise_still_needs_the_parked_toolhead(shop, monkeypatch, tmp_path):
    ref = tmp_path / "park.jpg"
    ref.write_bytes(b"jpeg")
    cfg = config.printer("p1")
    cfg["inspect"]["park_reference"] = str(ref)
    monkeypatch.setattr(config, "VISION_MODEL", "fixed:object:elsewhere:bottom_edge")
    out = bedcheck.check(cfg)
    assert out["verdict"] == "occupied" and fake_printer.STATE["gcode"] == []


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


def test_a_blank_ticket_filled_in_is_a_valid_ticket(shop, tmp_path):
    sets = tmp_path / "sets"
    for part in slicer.PARTS:
        (sets / "asa").mkdir(parents=True, exist_ok=True)
        (sets / "asa" / f"{part}.json").write_text("{}")
    (sets / "not-a-set").mkdir()
    shop(presets_root=str(sets))
    model = tmp_path / "part.stl"
    model.write_text("solid")

    t = jobs.blank("p1")
    assert t["printer"] == "p1" and t["_help"]["required"] == ["title", "printer", "model", "presets"]
    assert t["_help"]["printers"]["p1"]["preset_sets"] == ["asa"]
    with pytest.raises(jobs.JobError, match="missing 'title'"):
        jobs.validate_ticket(t)

    t.update(title="Part", model=str(model), presets="asa")
    job_id = jobs.submit(t)
    saved = jobs.ticket(job_id)
    assert "_help" not in saved and saved["copies"] == 1 and saved["presets"] == str(sets / "asa")
    assert jobs.blank("p1")["_help"]["printers"]["p1"]["presets_of_last_job"] == str(sets / "asa")


def test_a_misspelt_ticket_field_is_an_error(shop, tmp_path):
    with pytest.raises(jobs.JobError, match="unknown field 'override'"):
        jobs.validate_ticket({"title": "x", "printer": "p1", "model": "m", "presets": "p", "override": {}})


def test_a_held_job_does_not_block_the_next_one(shop, monkeypatch):
    """Jobs run in order of submission, and a held job steps aside."""
    for job_id, when in (("j10", "2026-01-01 10:00:00"), ("j9", "2026-01-01 09:00:00")):
        path = config.JOBS / job_id
        path.mkdir()
        (path / "ticket.json").write_text(json.dumps({"id": job_id, "title": job_id, "printer": "p1", "submitted": when}))
        (path / "state.json").write_text(json.dumps({"state": "queued", "held": False, "updated": time.time()}))
    jobs.transition("job-1", "dropped")
    assert [j["id"] for j in jobs.active()] == ["j9", "j10"]

    launched = []
    monkeypatch.setattr(daemon.agent, "run", lambda phase, job_id: launched.append(job_id) or {"state": "queued", "ok": True, "text": ""})
    jobs.update("j9", held=True)
    daemon.tick()
    assert launched == ["j10"]


def test_one_stalled_camera_frame_does_not_end_the_check(shop, monkeypatch):
    real, calls = moonraker._call, []

    def flaky(url, *args, **kwargs):
        if url.endswith("/snapshot"):
            calls.append(url)
            if len(calls) == 1:
                raise moonraker.PrinterError("timed out")
        return real(url, *args, **kwargs)

    monkeypatch.setattr(moonraker, "_call", flaky)
    monkeypatch.setattr(moonraker, "SNAPSHOT_PAUSE", 0)
    assert moonraker.snapshot(config.printer("p1"))[:2] == b"\xff\xd8" and len(calls) == 2


def macro_job(**fields):
    job_id = jobs.submit({"kind": "macro", "title": "PID tune", "printer": "p1",
                          "gcode": ["PID_CALIBRATE HEATER=extruder TARGET=260", "SAVE_CONFIG"], **fields})
    jobs.transition("job-1", "dropped")
    assert tools.slice_job(job_id)["sliced"] is True
    assert tools.announce_job(job_id)["announced"] is True
    return job_id


def wait_for(job_id, state):
    for _ in range(100):
        daemon.tick()
        if jobs.state(job_id)["state"] == state:
            return True
        time.sleep(0.05)
    return False


@pytest.fixture
def quick_restart(monkeypatch):
    monkeypatch.setattr(macro, "READY_POLL", 0)
    monkeypatch.setattr(daemon.agent, "run", lambda phase, job_id: {"state": "queued", "ok": True, "text": ""})


def test_macro_job_runs_its_commands_in_order_after_the_bed_check(shop, quick_restart):
    job_id = macro_job()
    assert "refused" in tools.start_job(job_id) and "refused" in tools.upload_job(job_id)
    assert tools.check_bed(job_id)["verdict"] == "clear"
    assert "uploaded" in tools.upload_job(job_id) and fake_printer.STATE["uploads"] == []
    before = len(fake_printer.STATE["gcode"])
    assert tools.start_job(job_id)["started"] is True
    assert started() == [] and len(fake_printer.STATE["gcode"]) == before  # nothing sent by the tool itself
    assert wait_for(job_id, "done")
    assert fake_printer.STATE["gcode"][before:] == ["PID_CALIBRATE HEATER=extruder TARGET=260", "SAVE_CONFIG",
                                                    "TURN_OFF_HEATERS"]


def test_macro_job_does_not_run_on_an_occupied_bed(shop, monkeypatch):
    monkeypatch.setattr(config, "VISION_MODEL", "fixed:object")
    job_id = macro_job()
    assert tools.check_bed(job_id)["verdict"] == "occupied"
    assert "refused" in tools.upload_job(job_id) and "refused" in tools.start_job(job_id)


def test_macro_job_that_does_not_need_the_bed_skips_the_camera_but_not_the_idle_check(shop, monkeypatch, quick_restart):
    monkeypatch.setattr(config, "VISION_MODEL", "fixed:object")
    job_id = macro_job(bed="any", heaters_off=False)
    fake_printer.STATE["state"] = "printing"
    assert "refused" in tools.check_bed(job_id)
    fake_printer.STATE["state"] = "complete"
    before = len(fake_printer.STATE["gcode"])
    assert tools.check_bed(job_id)["verdict"] == "not_needed"
    tools.upload_job(job_id)
    assert tools.start_job(job_id)["started"] is True
    assert wait_for(job_id, "done")
    assert fake_printer.STATE["gcode"][before:] == ["PID_CALIBRATE HEATER=extruder TARGET=260", "SAVE_CONFIG"]


def test_macro_job_stops_at_the_first_failed_command_and_turns_heaters_off(shop, quick_restart):
    job_id = macro_job(gcode=["M140 S100", "FAIL NOW", "SAVE_CONFIG"], bed="any")
    tools.check_bed(job_id)
    tools.upload_job(job_id)
    before = len(fake_printer.STATE["gcode"])
    tools.start_job(job_id)
    assert wait_for(job_id, "needs_human")
    assert fake_printer.STATE["gcode"][before:] == ["M140 S100", "FAIL NOW", "TURN_OFF_HEATERS"]
    assert "Unknown command: FAIL" in jobs.state(job_id)["reason"]
    assert "run again" in daemon.command("retry", job_id) and jobs.state(job_id)["state"] == "queued"


def test_macro_job_interrupted_by_a_queue_restart_is_not_run_twice(shop, quick_restart):
    job_id = macro_job(bed="any")
    tools.check_bed(job_id)
    tools.upload_job(job_id)
    tools.start_job(job_id)
    jobs.update(job_id, macro_started=time.time())  # as a previous queue process would have left it
    before = len(fake_printer.STATE["gcode"])
    daemon.tick()
    assert jobs.state(job_id)["state"] == "needs_human" and len(fake_printer.STATE["gcode"]) == before


def test_macro_tickets_are_checked(shop):
    base = {"kind": "macro", "title": "x", "printer": "p1"}
    for bad, word in (({}, "missing 'gcode'"), ({"gcode": "G28"}, "list of commands"),
                      ({"gcode": ["G28\nM84"]}, "list of commands"), ({"gcode": ["G28"], "bed": "empty"}, "'bed'"),
                      ({"gcode": ["G28"], "copies": 3}, "does not belong"), ({"gcode": ["G28"], "kind": "magic"}, "kind")):
        with pytest.raises(jobs.JobError, match=word):
            jobs.validate_ticket({**base, **bad})
    t = jobs.blank("p1", "macro")
    assert t["_help"]["required"] == ["title", "printer", "gcode"] and "model" not in t
    t.update(title="Home", gcode=["G28"])
    assert jobs.validate_ticket(t)["bed"] == "clear"


COOLING = {"active": 1, "stage": 2, "stages": [{"bed": 90, "hold": 600}, {"bed": 80, "hold": 600},
                                               {"bed": 70, "hold": 600}, {"bed": 60, "hold": 600}]}


def test_a_print_is_not_done_and_the_printer_not_free_until_the_cool_down_ends(shop, monkeypatch, capsys):
    shop(cooldown_macro="STAGED_COOLDOWN")
    tools.check_bed("job-1")
    tools.upload_job("job-1")
    tools.start_job("job-1")
    jobs.update("job-1", started=time.time() - 600)
    fake_printer.STATE.update(state="complete", cooldown=COOLING)
    daemon.tick()
    daemon.tick()
    assert jobs.state("job-1")["state"] == "printing"
    assert capsys.readouterr().err.count("Entering cooldown phase, about 20 min") == 1
    assert moonraker.status(config.printer("p1"))["idle"] is False
    fake_printer.STATE["cooldown"] = {**COOLING, "active": 0}
    daemon.tick()
    assert jobs.state("job-1")["state"] == "done"
    assert "ready to be cleared from the build plate" in capsys.readouterr().err


def test_nothing_launches_on_a_printer_that_is_cooling_down(shop):
    shop(cooldown_macro="STAGED_COOLDOWN")
    fake_printer.STATE.update(state="complete", cooldown=COOLING)
    assert "not idle" in tools.check_bed("job-1")["refused"]
    fake_printer.STATE["cooldown"] = None
    assert tools.check_bed("job-1")["verdict"] == "clear"


def test_a_printer_that_ends_in_standby_is_done_when_its_history_says_completed(shop):
    tools.check_bed("job-1")
    tools.upload_job("job-1")
    tools.start_job("job-1")
    jobs.update("job-1", started=time.time() - 600)
    fake_printer.STATE.update(state="standby")
    daemon.tick()
    assert jobs.state("job-1")["state"] == "done"


def test_a_prepare_run_that_does_nothing_is_tried_again_before_asking_the_operator(shop, monkeypatch, tmp_path):
    jobs.transition("job-1", "needs_human")
    jobs.transition("job-1", "submitted")
    runs = []
    monkeypatch.setattr(daemon.agent, "run", lambda phase, job_id: runs.append(phase) or
                        {"state": "submitted", "ok": False, "text": "I called the tools."})
    daemon.tick()
    daemon.tick()
    assert runs == ["prepare", "prepare"] and jobs.state("job-1")["state"] == "submitted"
    daemon.tick()
    assert len(runs) == 3 and jobs.state("job-1")["state"] == "needs_human"


def test_an_occupied_bed_is_looked_at_again_until_it_is_clear_then_the_job_goes(shop, monkeypatch, capsys):
    shop(recheck_sec=300)
    monkeypatch.setattr(config, "VISION_MODEL", "fixed:object")
    launches = []

    def launch(phase, job_id):  # what the agent does in the launch phase, without the model
        launches.append(job_id)
        if tools.check_bed(job_id).get("verdict") == "clear":
            tools.upload_job(job_id)
            tools.start_job(job_id)
        return {"state": jobs.state(job_id)["state"], "ok": True, "text": ""}

    monkeypatch.setattr(daemon.agent, "run", launch)
    daemon.tick()
    st = jobs.state("job-1")
    assert st["state"] == "queued" and 290 < st["next_try"] - time.time() <= 300
    daemon.tick()
    assert len(launches) == 1                      # not before the five minutes are up
    jobs.update("job-1", next_try=0)
    daemon.tick()
    assert len(launches) == 2 and capsys.readouterr().err.count("Waiting [job-1]") == 1  # told once
    monkeypatch.setattr(config, "VISION_MODEL", "fixed:bed_clear")
    jobs.update("job-1", next_try=0)
    daemon.tick()
    assert started() == ["printq_job-1.gcode"] and jobs.state("job-1")["state"] == "printing"


def test_the_timeline_says_what_a_printer_was_doing_and_why_it_was_not_printing(shop, monkeypatch):
    from printq import utilization
    clock = [1_000_000.0]
    monkeypatch.setattr(utilization.time, "time", lambda: clock[0])
    last, seen = {}, []

    def look(minutes):
        utilization.sample(last)
        seen.append(last["p1"][0])
        clock[0] += minutes * 60

    look(10)                                              # a queued job, bed not yet looked at
    jobs.update("job-1", waiting_bed="occupied")
    look(30)                                              # waiting for the operator to clear the bed
    fake_printer.STATE["state"] = "printing"
    look(60)
    fake_printer.STATE.update(state="complete", cooldown=COOLING)
    shop(cooldown_macro="STAGED_COOLDOWN")
    look(20)
    fake_printer.STATE["cooldown"] = None
    jobs.transition("job-1", "dropped")
    look(0)
    assert seen == ["launching", "bed_not_clear", "printing", "cooldown", "no_work"]
    t = utilization.totals("p1", 0, clock[0])
    assert (t["launching"], t["bed_not_clear"], t["printing"], t["cooldown"]) == (600, 1800, 3600, 1200)
    text = utilization.report(days=1)
    assert "p1: 50.0% active over 02:00:00" in text and "waiting for the bed to be cleared: 00:30:00" in text
