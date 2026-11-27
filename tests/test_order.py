"""Queue order and the spool gate: jobs are grouped by filament, and a job waits for its spool."""
import json
import time

import fake_printer
from printq import config, daemon, jobs, moonraker, order, tools
from test_gates import shop  # noqa: F401  (the fixture: one fake printer 'p1' in auto mode)

BLACK = {"id": 7, "filament_id": 78, "material": "ASA", "name": "PolyLite ASA Black", "vendor": "Polymaker"}
RED = {"id": 9, "filament_id": 40, "material": "ASA", "name": "ASA Red", "vendor": "Elegoo"}


def queued(job_id, filament_id=None, material="", minute=0, **state):
    """Put a sliced job straight into 'queued', as the prepare step would leave it."""
    path = config.JOBS / job_id
    path.mkdir(parents=True)
    (path / "plate_1.gcode").write_text("; test\n")
    ticket = {"id": job_id, "title": job_id, "printer": "p1", "material": material, "kind": "print",
              "filament": {"id": filament_id, "label": f"filament {filament_id}"} if filament_id else {},
              "submitted": f"2026-01-01 00:{minute:02d}:00"}
    (path / "ticket.json").write_text(json.dumps(ticket))
    (path / "state.json").write_text(json.dumps({"state": "queued", "held": False, "updated": time.time(), **state,
        "slice_report": {"gcode": str(path / "plate_1.gcode"), "estimate": {
            "filament_g": 1.0, "filament_mm": 400.0, "time": "5m", "layers": 5, "height_mm": 1}}}))


def ids(rows):
    return [j["id"] for j in rows]


def test_jobs_are_grouped_by_filament_loaded_spool_first():
    rows = [{"id": "a", "filament": {"id": 40}, "material": "ASA"}, {"id": "b", "filament": {"id": 78}, "material": "ASA"},
            {"id": "c", "filament": {"id": 40}, "material": "ASA"}, {"id": "d", "filament": {"id": 78}, "material": "ASA"},
            {"id": "e", "filament": {"id": 5}, "material": "PETG"}, {"id": "f", "filament": {}, "material": ""}]
    assert ids(order.arrange(rows, BLACK)) == ["b", "d", "f", "a", "c", "e"]
    assert ids(order.arrange(rows, RED)) == ["a", "c", "f", "b", "d", "e"]
    assert order.spool_changes(rows) == 4 and order.spool_changes(order.arrange(rows, BLACK)) == 2


def test_unknown_spool_takes_the_last_print_as_loaded():
    rows = [{"id": "a", "filament": {"id": 40}}, {"id": "b", "filament": {"id": 78}}, {"id": "c", "filament": {"id": 40}}]
    assert ids(order.arrange(rows, None, last={"filament": {"id": 78}})) == ["b", "a", "c"]
    assert ids(order.arrange(rows, None)) == ["a", "c", "b"]  # nothing known: oldest filament first


def test_material_only_job_suits_any_spool_of_that_material():
    assert order.suits({"material": "asa"}, BLACK) and not order.suits({"material": "PETG"}, BLACK)
    assert order.suits({"filament": {"id": 78}, "material": "ASA"}, BLACK)
    assert not order.suits({"filament": {"id": 78}, "material": "ASA"}, RED)


def test_queue_runs_the_job_that_matches_the_loaded_spool_first(shop, monkeypatch):
    jobs.update("job-1", held=True)  # the fixture's own job steps aside
    queued("red-1", 40, "ASA", minute=1)
    queued("black-1", 78, "ASA", minute=2)
    monkeypatch.setattr(moonraker, "spool", lambda cfg: BLACK)
    daemon.tick()
    assert jobs.state("black-1")["state"] == "printing" and jobs.state("red-1")["state"] == "queued"
    assert fake_printer.STATE["started"] == ["printq_black-1.gcode"]


def test_job_waits_for_its_spool_and_asks_once(shop, monkeypatch):
    jobs.update("job-1", held=True)
    queued("red-1", 40, "ASA", minute=1)
    said = []
    monkeypatch.setattr(daemon.notify, "send", lambda text, photo=None: said.append(text))
    monkeypatch.setattr(moonraker, "spool", lambda cfg: BLACK)
    daemon.tick()
    jobs.update("red-1", next_try=0)
    daemon.tick()
    assert jobs.state("red-1")["state"] == "queued" and fake_printer.STATE["started"] == []
    assert fake_printer.STATE["gcode"] == []  # the bed was not moved for a job that cannot print yet
    assert len([s for s in said if "Spool change" in s]) == 1 and "filament 40" in said[0]
    monkeypatch.setattr(moonraker, "spool", lambda cfg: RED)  # the operator loads the spool
    jobs.update("red-1", next_try=0)
    daemon.tick()
    assert jobs.state("red-1")["state"] == "printing"


def test_go_prints_with_the_loaded_colour_but_never_the_wrong_material(shop, monkeypatch):
    jobs.update("job-1", held=True)
    queued("red-1", 40, "ASA", minute=1, vouched_until=time.time() + 600)
    queued("petg-1", 5, "PETG", minute=2, vouched_until=time.time() + 600)
    assert tools.spool_problem("red-1", BLACK) is None
    assert "PETG" in tools.spool_problem("petg-1", BLACK)


def test_no_spool_tracking_asks_only_when_the_filament_changes(shop, monkeypatch):
    jobs.update("job-1", held=True)
    queued("red-1", 40, "ASA", minute=1)
    said = []
    monkeypatch.setattr(daemon.notify, "send", lambda text, photo=None: said.append(text))
    daemon.tick()  # SPOOLMAN_URL is empty in the fixture: no spool is known
    assert jobs.state("red-1")["state"] == "queued" and any("no spool is recorded" in s for s in said)
    daemon.command("go", "red-1")
    daemon.tick()
    assert jobs.state("red-1")["state"] == "printing"
    queued("red-2", 40, "ASA", minute=3)  # the same filament again: taken as still loaded
    jobs.transition("red-1", "done")
    fake_printer.STATE.update(state="standby")
    daemon.tick()
    assert jobs.state("red-2")["state"] == "printing"


def test_gcode_job_is_checked_not_sliced(shop, tmp_path):
    src = tmp_path / "part.gcode"
    src.write_text("; total filament used [g] = 12.5\n; filament used [mm] = 4100\n"
                   "; estimated printing time (normal mode) = 1h 2m 3s\n; total layers count = 80\nG28\n"
                   "; CONFIG_BLOCK_START\n; printer_model = Bench Printer\n; filament_type = ASA\n; CONFIG_BLOCK_END\n")
    job_id = jobs.submit({"kind": "gcode", "title": "Sent from the slicer", "printer": "p1", "file": str(src),
                          "material": "ASA"})
    out = tools.slice_job(job_id)
    assert out["sliced"] is True and out["estimate"]["filament_g"] == 12.5
    assert jobs.state(job_id)["slice_report"]["presliced"] is True
    shop(gcode_printer_model="Other Printer")
    other = jobs.submit({"kind": "gcode", "title": "Wrong machine", "printer": "p1", "file": str(src)})
    assert "sliced for 'Bench Printer'" in tools.slice_job(other)["problems"][0]
    src.write_text("; changed\n")
    late = jobs.submit({"kind": "gcode", "title": "x", "printer": "p1", "file": str(src)})
    src.write_text("; changed again\n")
    assert "changed" in tools.slice_job(late)["refused"]


def test_reprint_is_recognised_by_contents_then_by_path(shop, tmp_path):
    presets = tmp_path / "set"
    presets.mkdir()
    for part in ("machine", "process", "filament"):
        (presets / f"{part}.json").write_text("{}")
    model = tmp_path / "part.stl"
    model.write_text("solid a\nendsolid a\n")
    first = jobs.submit({"title": "Part", "printer": "p1", "model": str(model), "presets": str(presets),
                         "overrides": {"brim_type": "outer_only"}, "filament": {"id": 78, "label": "Black"}})
    same = jobs.last_print_of(sha=jobs.sha256(model), path=str(model))
    assert same["match"] == "same file" and same["ticket"]["id"] == first
    assert same["ticket"]["overrides"] == {"brim_type": "outer_only"}
    assert jobs.last_print_of(sha="0" * 64, path=str(model))["match"] == "same path"
    assert jobs.last_print_of(sha="0" * 64, path="/elsewhere/part.stl", name="part.stl")["match"] == "same name"
    assert jobs.last_print_of(sha="0" * 64, path="/elsewhere/other.stl", name="other.stl") is None
