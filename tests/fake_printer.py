"""A pretend Moonraker for tests and dry runs: one printer, one camera, no hardware.

    python tests/fake_printer.py 7199 [frame.jpg]

Serves the handful of endpoints printq uses and remembers what it was told,
so a test can assert that a print was (or was not) started.
GET /_state returns that memory; POST /_set?state=printing etc. changes it.
"""
import json
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# smallest valid JPEG, used when no frame file is given
TINY = bytes.fromhex("ffd8ffe000104a46494600010100000100010000ffdb004300" + "01" * 64 +
                     "ffc0000b080001000101011100ffc40014000100000000000000000000000000000009ffc40014100100000000"
                     "000000000000000000000000ffda0008010100003f0054ffd9")
STATE = {"state": "standby", "file": "last-part.gcode", "position": [202.0, 264.0, 169.7], "homed": "",
         "uploads": [], "started": [], "gcode": [], "object_height": 20.0, "frame": TINY}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, obj, code=200, ctype="application/json"):
        body = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urllib.parse.urlsplit(self.path)
        if url.path == "/printer/objects/query":
            self.reply({"result": {"status": {
                "print_stats": {"state": STATE["state"], "filename": STATE["file"]},
                "toolhead": {"position": STATE["position"] + [0.0], "homed_axes": STATE["homed"]},
                "heater_bed": {"temperature": 24.0, "target": 0.0},
                "extruder": {"temperature": 24.0, "target": 0.0},
                "display_status": {"progress": 0.0}}}})
        elif url.path == "/server/files/metadata":
            self.reply({"result": {"object_height": STATE["object_height"]}})
        elif url.path == "/server/webcams/list":
            self.reply({"result": {"webcams": [{"name": "cam", "snapshot_url": f"http://{self.headers['Host']}/snapshot"}]}})
        elif url.path == "/snapshot":
            self.reply(STATE["frame"], ctype="image/jpeg")
        elif url.path == "/server/spoolman/spool_id":
            self.reply({"result": {"spool_id": None}})
        elif url.path == "/_state":
            self.reply({k: v for k, v in STATE.items() if k != "frame"})
        else:
            self.reply({"error": "not found"}, 404)

    def do_POST(self):
        url = urllib.parse.urlsplit(self.path)
        query = dict(urllib.parse.parse_qsl(url.query))
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if url.path == "/server/files/upload":
            name = body.split(b'filename="', 1)[1].split(b'"', 1)[0].decode()
            STATE["uploads"].append(name)
            self.reply({"result": {"item": {"path": name}}})
        elif url.path == "/printer/print/start":
            STATE["started"].append(query["filename"])
            STATE.update(state="printing", file=query["filename"])
            self.reply({"result": "ok"})
        elif url.path == "/printer/gcode/script":
            script = query["script"]
            STATE["gcode"].append(script)
            for line in script.split("\n"):
                words = {w[0]: w[1:].lstrip("=") for w in line.split()[1:]}
                if line.startswith("G1 Z"):  # relative move, as the bed check sends it
                    STATE["position"][2] += float(line.split()[1][1:])
                elif line.startswith("G28"):  # X/Y homing as the bed check sends it; Z would be a bug
                    STATE["homed"] = "".join(sorted(set(STATE["homed"]) | {a.lower() for a in "XYZ" if a in line[3:]}))
                elif line.startswith("G1 X"):
                    STATE["position"][:2] = [float(words["X"]), float(words["Y"])]
                elif line.startswith("SET_KINEMATIC_POSITION"):
                    STATE["position"] = [float(words[a]) for a in "XYZ"]
                    STATE["homed"] = "xyz"
                elif line.startswith("M84"):
                    STATE["homed"] = ""
            self.reply({"result": "ok"})
        elif url.path == "/_set":
            for k, v in query.items():
                STATE[k] = json.loads(v) if v[:1] in "[{0123456789-" else v
            self.reply({"result": "ok"})
        else:
            self.reply({"error": "not found"}, 404)


def serve(port: int, frame: bytes | None = None) -> ThreadingHTTPServer:
    if frame:
        STATE["frame"] = frame
    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


if __name__ == "__main__":
    serve(int(sys.argv[1]), open(sys.argv[2], "rb").read() if len(sys.argv) > 2 else None).serve_forever()
