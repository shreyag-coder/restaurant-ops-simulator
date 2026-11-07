"""Local restaurant dashboard. Run: python3 dashboard.py (http://127.0.0.1:8765).

The browser reads committed recordings. Controls launch the existing CLI one
tick at a time: the orchestrator remains the only restaurant-state writer.
No third-party packages are needed. Bind only to loopback; this is a local app.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import recording
import replay
import scenario

ROOT = Path(__file__).resolve().parent
WEB = ROOT / "dashboard" / "web"
VARIANTS = {"baseline", "disruption", "blocker"}
KITCHENS = {"classic": ("config", "personas"), "chipotle": ("config_chipotle", "personas_chipotle")}
RUN_ID = re.compile(r"^[a-f0-9]{32}$")
STATIC_NAME = re.compile(r"^[a-z][a-z0-9_-]*\.(html|js|css)$")


def ui_version():
    """Fingerprint of the dashboard's own files; the page reloads itself when it changes."""
    import hashlib
    digest = hashlib.sha256()
    for name in sorted(static_files()):
        stat = (WEB / name).stat()
        digest.update(("%s:%d:%d;" % (name, stat.st_mtime_ns, stat.st_size)).encode())
    return digest.hexdigest()[:16]


def static_files():
    """The dashboard's own assets: plain .html/.js/.css files directly in dashboard/web."""
    return {p.name for p in WEB.iterdir() if p.is_file() and not p.is_symlink() and STATIC_NAME.match(p.name)}
WORKER_EDIT_FIELDS = ("name", "role", "description", "skills", "profile")
MAX_PERSONA_CHARS = 6000


def _write_atomic(path, text):
    """Replace a file in one step so a reader never sees half a file."""
    path = Path(path)
    handle, temp = tempfile.mkstemp(dir=path.parent, prefix="." + path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as out:
            out.write(text)
        os.replace(temp, path)
    except BaseException:
        if os.path.exists(temp):
            os.unlink(temp)
        raise


def summary(state):
    orders = list(state["orders"].values())
    return {"run_id": state["run_id"], "time": state["time"], "deadline": state["deadline"],
            "status": state["status"], "mode": state["mode"],
            "variant": state["config"].get("variant", "baseline"),
            "kitchen": "chipotle" if "burrito" in state["config"].get("menu", {}).get("recipes", {}) else "classic",
            "workers": len(state["workers"]),
            "correct": sum(o["correct"] is True for o in orders), "total": len(orders),
            "on_time": sum(o["on_time"] is True for o in orders)}


class Dashboard:
    def __init__(self, runs_dir=None, interval=0.7, sim_root=None):
        self.runs_dir = Path(runs_dir or ROOT / "sim" / "runs").resolve()
        # Where the kitchens' config and persona folders live (tests use a copy).
        self.sim_root = Path(sim_root or ROOT / "sim").resolve()
        self.interval = interval
        self.guard = threading.Lock()
        # Held while an orchestrator subprocess writes a run, so a kitchen
        # update typed mid-run waits for the current minute instead of failing.
        self.tick_lock = threading.Lock()
        self.profile_lock = threading.Lock()
        self.stop = threading.Event()
        self.active = False
        self.run_id = None
        self.error = None
        self.thread = None

    def path(self, run_id):
        if not RUN_ID.fullmatch(run_id):
            raise ValueError("Invalid run ID")
        path = self.runs_dir / run_id
        if not path.is_dir() or path.is_symlink():
            raise ValueError("Run not found")
        return path

    def control_status(self):
        with self.guard:
            return {"active": self.active, "run_id": self.run_id,
                    "pausing": self.active and self.stop.is_set(), "error": self.error}

    def runs(self):
        rows = []
        if self.runs_dir.exists():
            for folder in self.runs_dir.iterdir():
                if not folder.is_dir() or folder.is_symlink() or not RUN_ID.fullmatch(folder.name):
                    continue
                try:
                    state = recording.load_checkpoint(folder)
                    rows.append(dict(summary(state), updated=(folder / "state.json").stat().st_mtime))
                except (recording.CheckpointError, OSError, KeyError):
                    continue
        return sorted(rows, key=lambda row: row["updated"], reverse=True)

    def detail(self, run_id, at=None):
        folder = self.path(run_id)
        state, events = replay.replay_state(folder, at=at)
        committed = recording.load_checkpoint(folder)
        ticks = [t for t in recording.list_snapshot_ticks(folder) if t <= committed["time"]]
        return {"state": state, "events": events, "ticks": ticks,
                "latest_tick": committed["time"], "control": self.control_status()}

    def control(self, data):
        action = data.get("action")
        if action not in {"new", "start", "step", "pause"}:
            raise ValueError("Unknown control")
        if action == "pause":
            self.stop.set()
            return self.control_status()
        variant, mode = data.get("scenario", "baseline"), data.get("mode", "det")
        kitchen = data.get("kitchen", "classic")
        if variant not in VARIANTS or mode not in {"det", "ai"} or kitchen not in KITCHENS:
            raise ValueError("Invalid scenario or decision mode")
        run_id = None if action == "new" else data.get("run_id")
        if run_id:
            state = recording.load_checkpoint(self.path(run_id))
            if state["status"] != "paused":
                raise ValueError("This recording has ended. Create a new run to simulate again.")
            mode = state["mode"]
        if mode == "ai" and not (os.environ.get("OPENROUTER_API_KEY") and os.environ.get("OPENROUTER_MODEL")):
            raise ValueError("AI mode needs OPENROUTER_API_KEY and OPENROUTER_MODEL in the dashboard server environment.")
        with self.guard:
            if self.active:
                raise ValueError("A simulation is already progressing. Pause it before starting another.")
            self.active, self.run_id, self.error = True, run_id, None
            self.stop.clear()
            self.thread = threading.Thread(target=self._work,
                                           args=(run_id, variant, mode, action == "start", kitchen), daemon=True)
            self.thread.start()
        return self.control_status()

    def _work(self, run_id, variant, mode, continuous, kitchen="classic"):
        try:
            while True:
                command = [sys.executable, str(ROOT / "orchestrator.py"), "--runs-dir", str(self.runs_dir)]
                if run_id is None:
                    config_dir, personas_dir = self.kitchen_dirs(kitchen)
                    command += ["run", "--scenario", variant, "--mode", mode,
                                "--config-dir", str(config_dir), "--personas-dir", str(personas_dir)]
                else:
                    command += ["resume", "--run", run_id]
                command += ["--max-ticks", "1", "--quiet"]
                with self.tick_lock:
                    result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
                match = re.search(r"Run ([a-f0-9]{32}):", result.stdout)
                if match:
                    run_id = match.group(1)
                    with self.guard:
                        self.run_id = run_id
                if result.returncode:
                    message = result.stderr.strip() or "The orchestrator could not advance this run."
                    key = os.environ.get("OPENROUTER_API_KEY")
                    if key:
                        message = message.replace(key, "[redacted]")
                    raise ValueError(message[-600:])
                state = recording.load_checkpoint(self.path(run_id))
                if not continuous or state["status"] in {"completed", "timed_out"} or self.stop.wait(self.interval):
                    break
        except Exception as exc:
            with self.guard:
                self.error = str(exc)
        finally:
            with self.guard:
                self.active = False


    # ---------------------------------------------------------- kitchen updates

    def command(self, data):
        """Queue one line of manager text ("fryer is broken") on a paused run.

        The orchestrator parses, validates, and records it; the engine applies
        it at the start of the next simulated minute.
        """
        run_id, text = data.get("run_id"), data.get("text")
        if not isinstance(run_id, str) or not run_id:
            raise ValueError("Select a run before sending a kitchen update")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("Type a kitchen update first")
        self.path(run_id)
        argv = [sys.executable, str(ROOT / "orchestrator.py"), "--runs-dir", str(self.runs_dir),
                "command", "--run", run_id, "--text=" + text.strip()[:300]]
        with self.tick_lock:
            # The AI interpreter makes at most two 30-second attempts.
            result = subprocess.run(argv, cwd=ROOT, capture_output=True, text=True, timeout=90)
        if result.returncode:
            message = result.stderr.strip() or "The orchestrator could not queue that update."
            raise ValueError(message[len("Error: "):] if message.startswith("Error: ") else message)
        return json.loads(result.stdout.strip().splitlines()[-1])

    # ---------------------------------------------------------- team profiles

    def kitchen_dirs(self, kitchen):
        if kitchen not in KITCHENS:
            raise ValueError("Unknown kitchen")
        config, personas = KITCHENS[kitchen]
        return self.sim_root / config, self.sim_root / personas

    def profiles(self, kitchen):
        config_dir, personas_dir = self.kitchen_dirs(kitchen)
        workers = json.loads((config_dir / "workers.json").read_text(encoding="utf-8"))["workers"]
        menu = json.loads((config_dir / "menu.json").read_text(encoding="utf-8"))
        skills = sorted({step["skill"] for recipe in menu["recipes"].values() for step in recipe["steps"]}
                        | {step["skill"] for step in menu["order_steps"]})
        personas = {}
        for worker in workers:
            path = personas_dir / (worker["id"] + ".txt")
            personas[worker["id"]] = path.read_text(encoding="utf-8") if path.is_file() else ""

        def shown(path):
            try:
                return str(path.relative_to(ROOT))
            except ValueError:
                return path.name
        return {"kitchen": kitchen, "workers": workers, "personas": personas, "skills": skills,
                "stations": {sid: station.get("name", sid) for sid, station in menu["stations"].items()},
                "paths": {"workers": shown(config_dir / "workers.json"), "personas": shown(personas_dir)}}

    def save_profile(self, data):
        """Replace one worker's profile and persona after validating every variant."""
        kitchen, worker_id = data.get("kitchen"), data.get("worker_id")
        worker, persona = data.get("worker"), data.get("persona")
        config_dir, personas_dir = self.kitchen_dirs(kitchen)
        if not isinstance(worker, dict):
            raise ValueError("Expected the worker's fields")
        unknown = sorted(set(worker) - set(WORKER_EDIT_FIELDS))
        if unknown:
            raise ValueError("These fields can't be edited here: " + ", ".join(unknown))
        if not isinstance(persona, str) or not persona.strip():
            raise ValueError("The persona text can't be empty; it is what the AI worker reads")
        if len(persona) > MAX_PERSONA_CHARS:
            raise ValueError("Keep the persona under {} characters".format(MAX_PERSONA_CHARS))
        with self.profile_lock:
            doc = json.loads((config_dir / "workers.json").read_text(encoding="utf-8"))
            ids = [w.get("id") for w in doc["workers"]]
            if worker_id not in ids:
                raise ValueError("Unknown worker")
            updated = {"id": worker_id}
            for field in WORKER_EDIT_FIELDS:
                if field in worker and not (field == "profile" and not worker[field]):
                    updated[field] = worker[field]
            doc["workers"][ids.index(worker_id)] = updated
            text = json.dumps(doc, indent=2, ensure_ascii=False) + "\n"
            # Validate the edited kitchen exactly as a new run would load it.
            with tempfile.TemporaryDirectory() as temp:
                for name in ("menu.json", "scenario.json"):
                    shutil.copyfile(config_dir / name, Path(temp) / name)
                (Path(temp) / "workers.json").write_text(text, encoding="utf-8")
                variants = json.loads((config_dir / "scenario.json").read_text(encoding="utf-8"))["variants"]
                for variant in variants:
                    try:
                        scenario.load_config(temp, variant)
                    except ValueError as exc:
                        raise ValueError("Not saved. {} (checked against the {} scenario)".format(exc, variant)) from exc
            _write_atomic(config_dir / "workers.json", text)
            _write_atomic(personas_dir / (worker_id + ".txt"), persona.strip() + "\n")
        return self.profiles(kitchen)


class Handler(BaseHTTPRequestHandler):
    server_version = "RestaurantDashboard/1"

    def log_message(self, *_):
        pass

    def local_host(self):
        host = self.headers.get("Host", "")
        return host in {"127.0.0.1:{}".format(self.server.server_port),
                        "localhost:{}".format(self.server.server_port)}

    def send_json(self, value, code=200):
        body = json.dumps(value, allow_nan=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self.local_host():
            self.send_json({"error": "This dashboard accepts loopback hosts only"}, 403)
            return
        parsed = urlparse(self.path)
        app = self.server.app
        try:
            if parsed.path == "/api/meta":
                self.send_json({"static": False, "ai_available": bool(os.environ.get("OPENROUTER_API_KEY") and os.environ.get("OPENROUTER_MODEL")),
                                "model": os.environ.get("OPENROUTER_MODEL"), "control": app.control_status(),
                                "ui_version": ui_version()})
            elif parsed.path == "/api/runs":
                self.send_json({"runs": app.runs(), "control": app.control_status()})
            elif parsed.path == "/api/profiles":
                kitchen = parse_qs(parsed.query).get("kitchen", ["chipotle"])[0]
                self.send_json(app.profiles(kitchen))
            elif parsed.path.startswith("/api/runs/"):
                run_id = parsed.path.split("/")[3]
                query = parse_qs(parsed.query)
                tick = int(query["tick"][0]) if "tick" in query else None
                if tick is not None and tick < 0:
                    raise ValueError("Tick must be nonnegative")
                self.send_json(app.detail(run_id, tick))
            else:
                name = parsed.path.lstrip("/") or "index.html"
                if name not in static_files():
                    self.send_json({"error": "Not found"}, 404)
                    return
                path = WEB / name
                body = path.read_bytes()
                mime = {".html": "text/html", ".js": "text/javascript", ".css": "text/css"}[path.suffix]
                self.send_response(200)
                self.send_header("Content-Type", mime + "; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        except (ValueError, OSError, recording.CheckpointError, replay.ReplayError) as exc:
            self.send_json({"error": str(exc)}, 400)

    def do_POST(self):
        # Requests from another website must not launch this machine's simulator.
        origin = self.headers.get("Origin")
        host = self.headers.get("Host")
        if not self.local_host() or (origin and origin != "http://" + str(host)):
            self.send_json({"error": "Cross-origin control requests are not allowed"}, 403)
            return
        app = self.server.app
        routes = {"/api/control": (4096, app.control), "/api/command": (4096, app.command),
                  "/api/profiles": (65536, app.save_profile)}
        if self.path not in routes or self.headers.get_content_type() != "application/json":
            self.send_json({"error": "Expected a JSON control request"}, 400)
            return
        limit, handler = routes[self.path]
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length < limit:
                raise ValueError("Invalid request length")
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict):
                raise ValueError("Expected an object")
            self.send_json(handler(data))
        except (ValueError, OSError, recording.CheckpointError, subprocess.TimeoutExpired) as exc:
            self.send_json({"error": str(exc)}, 400)


def make_server(port=8765, runs_dir=None, interval=0.7, sim_root=None):
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.app = Dashboard(runs_dir, interval, sim_root)
    return server


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--runs-dir", type=Path)
    args = parser.parse_args()
    server = make_server(args.port, args.runs_dir)
    print("Restaurant dashboard: http://127.0.0.1:{}".format(server.server_port), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.app.stop.set()
        server.server_close()


if __name__ == "__main__":
    main()
