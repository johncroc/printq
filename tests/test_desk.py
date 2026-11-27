"""The print desk: drafts, the reprint question, the preview rule, and the slicer's way in."""
import json
import threading
import time
import urllib.error
import urllib.request

import pytest

from printq import config, desk, jobs, slicer
from test_gates import shop  # noqa: F401

GCODE = (b"; total filament used [g] = 3.5\n; filament used [mm] = 1200\n"
         b"; estimated printing time (normal mode) = 12m 0s\n; total layers count = 20\nG28\n"
         b"; CONFIG_BLOCK_START\n; printer_model = Bench Printer\n; filament_type = ASA\n; CONFIG_BLOCK_END\n")


@pytest.fixture
def site(shop, tmp_path, monkeypatch):  # noqa: F811
    presets = tmp_path / "presets" / "p1-asa"
    presets.mkdir(parents=True)
    (presets / "machine.json").write_text(json.dumps({"printer_model": "Bench Printer"}))
    (presets / "process.json").write_text(json.dumps({"layer_height": "0.2"}))
    (presets / "filament.json").write_text(json.dumps({"filament_type": ["ASA"], "nozzle_temperature": ["260"]}))
    shop(presets_root=str(tmp_path / "presets"))
    monkeypatch.setenv("PRINTQ_DESK_KEY", "k" * 32)
    monkeypatch.setenv("PRINTQ_DESK_OPEN", "0")
    desk._CACHE.clear()
    server = desk.serve("127.0.0.1", 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"

    def call(path, body=None, headers=None, raw=False):
        data = body if raw or body is None else json.dumps(body).encode()
        req = urllib.request.Request(base + path, data=data, headers={"X-Printq-Desk": "1", **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    call.presets = presets
    yield call
    server.shutdown()


def fake_slice(ticket, workdir):
    return {"ok": True, "problems": [], "warnings": ["floating region"], "gcode": "x", "settings": {},
            "estimate": {"filament_g": 9.0, "filament_mm": 3000.0, "time": "40m 0s", "layers": 60}}


def test_dropped_model_needs_a_preview_before_it_can_be_queued(site, monkeypatch):
    monkeypatch.setattr(slicer, "slice_ticket", fake_slice)
    code, draft = site("/api/upload?name=clip.stl", b"solid a\nendsolid a\n", raw=True)
    assert code == 200 and draft["kind"] == "print" and draft["source"] == "upload"
    choice = {"printer": "p1", "material": "ASA", "filament": {"id": 78, "label": "Black"},
              "presets": str(site.presets), "overrides": {}, "copies": 2}
    code, out = site(f"/api/draft/{draft['id']}/print", choice)
    assert code == 400 and "preview" in out["error"] and not jobs.active()[1:]
    code, seen = site(f"/api/draft/{draft['id']}/preview", choice)
    assert seen["ok"] and seen["estimate"]["time"] == "40m 0s" and seen["warnings"] == ["floating region"]
    # Changed settings after the preview: the estimate on screen is no longer this job's.
    code, out = site(f"/api/draft/{draft['id']}/print", {**choice, "overrides": {"brim_type": "outer_only"}})
    assert code == 400
    code, out = site(f"/api/draft/{draft['id']}/print", choice)
    ticket = jobs.ticket(out["job"])
    assert code == 200 and ticket["filament"] == {"id": 78, "label": "Black"} and ticket["copies"] == 2
    assert ticket["expect"] == {"layer_height": "0.2", "nozzle_temperature": ["260"]}
    assert site(f"/api/draft/{draft['id']}/print", choice)[0] == 400  # a draft is queued once


def test_reprint_is_recognised_and_offers_its_last_settings(site, monkeypatch):
    monkeypatch.setattr(slicer, "slice_ticket", fake_slice)
    _, first = site("/api/upload?name=bracket.stl", b"solid a\nendsolid a\n", raw=True)
    choice = {"printer": "p1", "material": "ASA", "filament": {"id": 78, "label": "Black"},
              "presets": str(site.presets), "overrides": {"wall_loops": "4"}, "copies": 1}
    site(f"/api/draft/{first['id']}/preview", choice)
    job = site(f"/api/draft/{first['id']}/print", choice)[1]["job"]
    site("/api/upload?name=bracket.stl", b"solid a\nendsolid a\n", raw=True)
    site("/api/upload?name=bracket.stl", b"solid b\nendsolid b\n", raw=True)
    _, state = site("/api/state")
    same, revised = state["drafts"]
    assert same["history"]["match"] == "same file" and same["history"]["job"] == job
    assert same["history"]["overrides"] == {"wall_loops": "4"} and same["history"]["filament"]["id"] == 78
    assert revised["history"]["match"] == "same name"
    assert state["printers"]["p1"]["last"]["ASA"]["overrides"] == {"wall_loops": "4"}
    assert [s["material"] for s in state["printers"]["p1"]["preset_sets"]] == ["ASA"]


def multipart(print_now):
    b = b"xYz"
    return (b"--" + b + b'\r\nContent-Disposition: form-data; name="print"\r\n\r\n' + print_now + b"\r\n--" + b +
            b'\r\nContent-Disposition: form-data; name="file"; filename="plate one.gcode"\r\n'
            b"Content-Type: application/octet-stream\r\n\r\n" + GCODE + b"\r\n--" + b + b"--\r\n",
            {"Content-Type": "multipart/form-data; boundary=xYz"})


def test_slicer_print_button_queues_and_upload_waits(site):
    body, head = multipart(b"true")
    assert site("/p/p1/api/version")[1]["text"].startswith("OctoPrint")
    assert site("/p/p1/api/files/local", body, head, raw=True)[0] == 403  # no key
    assert site("/p/p1/api/files/local", body, {**head, "X-Api-Key": "wrong"}, raw=True)[0] == 403
    code, out = site("/p/p1/api/files/local", body, {**head, "X-Api-Key": "k" * 32}, raw=True)
    assert code == 201 and out["done"] is True
    job = jobs.active()[-1]
    ticket = jobs.ticket(job["id"])
    assert ticket["kind"] == "gcode" and ticket["material"] == "ASA" and ticket["title"] == "plate one"
    assert open(ticket["file"], "rb").read() == GCODE  # the bytes the slicer sent, unchanged
    body, head = multipart(b"false")
    before = len(jobs.all_jobs())
    site("/p/p1/api/files/local", body, {**head, "X-Api-Key": "k" * 32}, raw=True)
    assert len(jobs.all_jobs()) == before
    waiting = site("/api/state")[1]["drafts"]
    assert [d["source"] for d in waiting] == ["slicer"] and waiting[0]["estimate"]["filament_g"] == 3.5
    assert site("/p/nowhere/api/files/local", body, {**head, "X-Api-Key": "k" * 32}, raw=True)[0] == 400


def test_other_sites_cannot_drive_the_desk(site):
    assert site("/api/upload?name=x.stl", b"solid", {"X-Printq-Desk": ""}, raw=True)[0] == 403
    assert site("/api/state", headers={"Host": "evil.example"})[0] == 403
    assert site("/api/upload?name=x.exe", b"MZ", raw=True)[0] == 400
    assert site("/api/upload?name=..%2F..%2Fx.stl", b"solid", raw=True)[1]["name"] == "x.stl"


def test_export_folder_makes_one_draft_per_finished_file(site, tmp_path):
    root = tmp_path / "exports"
    (root / "tools").mkdir(parents=True)
    (root / "old.stl").write_text("solid old\nendsolid old\n")
    stop = threading.Event()
    threading.Thread(target=desk.watch, args=(root, stop, 0.05), daemon=True).start()
    time.sleep(0.2)
    (root / "tools" / "tray.stl").write_text("solid a\nendsolid a\n")
    (root / "notes.txt").write_text("not a model")
    time.sleep(0.5)
    drafts = desk.all_drafts()
    assert [(d["name"], d["source"]) for d in drafts] == [("tray.stl", "export")]
    assert drafts[0]["path"] == str(root / "tools" / "tray.stl")  # used where it is, not copied
    (root / "tools" / "tray.stl").write_text("solid b\nendsolid b\n")  # exported again after an edit
    time.sleep(0.5)
    stop.set()
    assert [d["status"] for d in desk.all_drafts(None)] == ["replaced", "open"]
