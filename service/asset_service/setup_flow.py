"""The setup flow behind /setup: pick a folder, survey it, correct the map,
index. The server serves these; `pharos.py setup` opens the page.

Nothing here runs an engine. The two long steps (`init`, which surveys and
writes the first map, and `ingest`, which runs the engine-free chains) run
as `pharos.py` subprocesses in a background thread, their output kept for
the page's progress log; one job at a time.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import config, library_map

REPO = Path(__file__).resolve().parents[2]
_LOOPBACK = {"127.0.0.1", "localhost", "::1", "0:0:0:0:0:0:0:1"}
_HIDDEN = {"$recycle.bin", "system volume information", "$windows.~bt",
           "recovery", "config.msi", "msocache"}
_STATE: dict = {"state": "idle", "job": "", "lines": [], "error": "",
                "started": 0.0, "finished": 0.0, "result": {}}
_LOCK = threading.Lock()


# --------------------------------------------------------------- folders
def fs_allowed(bind_host: str) -> bool:
    """Folder browsing exists for the person at the machine: the server
    must be bound to loopback."""
    return (bind_host or "").strip("[]").lower() in _LOOPBACK


def api_fs_drives() -> dict:
    drives = []
    if hasattr(os, "listdrives"):
        try:
            drives = [{"path": d, "label": d.rstrip("\\/")} for d in os.listdrives()]
        except OSError:
            drives = []
    if not drives:
        drives = [{"path": "/", "label": "/"}]
    home = str(Path.home())
    return {"drives": drives, "home": home,
            "suggested": config._CFG.get("library_root") or home}


def api_fs_list(path: str) -> tuple:
    """Sub-folders of `path` (never files), for the folder browser."""
    raw = (path or "").strip()
    if not raw:
        return {"error": "path missing"}, 400
    if "\x00" in raw or raw.startswith(("\\\\", "//")):
        return {"error": "not a local folder path"}, 400
    p = Path(raw).expanduser()
    if not p.is_absolute():
        return {"error": "path must be absolute"}, 400
    try:
        p = p.resolve()
    except OSError:
        return {"error": "unreadable path"}, 400
    if not p.exists():
        return {"error": "no such folder"}, 404
    if not p.is_dir():
        return {"error": "not a folder"}, 400
    dirs = []
    try:
        with os.scandir(p) as it:
            for e in it:
                try:
                    if not e.is_dir(follow_symlinks=False):
                        continue
                except OSError:
                    continue
                if e.name.startswith((".", "$")) or e.name.lower() in _HIDDEN:
                    continue
                has_children = False
                try:
                    with os.scandir(e.path) as sub:
                        has_children = any(s.is_dir(follow_symlinks=False) for s in sub)
                except OSError:
                    pass
                dirs.append({"name": e.name, "path": e.path, "has_children": has_children})
    except PermissionError:
        return {"error": "no permission to read this folder"}, 403
    except OSError as exc:
        return {"error": str(exc)}, 400
    dirs.sort(key=lambda d: d["name"].lower())
    parent = str(p.parent) if p.parent != p else ""
    return {"path": str(p), "parent": parent, "dirs": dirs}, 200


# ------------------------------------------------------------------ state
def _public_progress() -> dict:
    with _LOCK:
        return {"state": _STATE["state"], "job": _STATE["job"],
                "lines": _STATE["lines"][-400:], "error": _STATE["error"],
                "seconds": round((_STATE["finished"] or time.time())
                                 - _STATE["started"], 1) if _STATE["started"] else 0,
                "result": dict(_STATE["result"])}


def next_steps(rows: dict) -> list:
    """What the owner's decisions ask for after indexing: the engine
    chains, each with its command."""
    out = []
    kinds = {}
    for r in rows.values():
        if not library_map.included(r, rows):
            continue
        act = library_map.effective_action(r, rows)
        if r["kind"] == "unreal" and r["state"] in ("raw", "project") and act == "export":
            kinds.setdefault("convert", []).append(r["folder"])
        elif r["kind"] == "blender" and r["state"] in ("kit", "files") and act == "extract":
            kinds.setdefault("extract", []).append(r["folder"])
        elif r["kind"] == "animation" and act == "previews":
            kinds.setdefault("previews", []).append(r["folder"])
    text = {"convert": "export the approved Unreal packs to FBX (needs the Unreal Editor)",
            "extract": "extract the approved Blender kits and .blend files (needs Blender)",
            "previews": "record the animation previews in a browser tab"}
    for cmd, folders in kinds.items():
        out.append({"command": f"python pharos.py {cmd}", "why": text[cmd],
                    "folders": folders[:8], "more": max(0, len(folders) - 8)})
    return out


def api_state(server_mode: str) -> dict:
    """What the page needs on load: configured?, the roots, the map's
    summary, what is pending, the running job."""
    configured = config.is_configured()
    out = {"configured": configured, "mode": server_mode,
           "library_root": config._CFG.get("library_root") or "",
           "registry_dir": config._CFG.get("registry_dir") or "",
           "default_registry_dir": str(Path.home() / ".pharos" / "registry"),
           "map_file": str(library_map.PATH) if configured else "",
           "rows": 0, "pending": [], "unconfirmed": 0, "summary": [],
           "next": [], "progress": _public_progress()}
    if configured:
        try:
            rows = library_map.load()
        except OSError:
            rows = {}
        out["rows"] = len(rows)
        out["pending"] = [r["folder"] for r in library_map.pending(rows)]
        out["unconfirmed"] = sum(1 for r in rows.values()
                                 if r["include"] and not r["confirmed"] and r["folder"] != ".")
        out["summary"] = library_map.summary(rows)
        out["next"] = next_steps(rows)
    return out


# ------------------------------------------------------------------- jobs
def _set(**kw) -> None:
    with _LOCK:
        _STATE.update(kw)


def _line(text: str) -> None:
    with _LOCK:
        _STATE["lines"].append(text.rstrip("\r\n"))
        if len(_STATE["lines"]) > 4000:
            del _STATE["lines"][:1000]


def _stream(cmd: list) -> int:
    """Run `cmd`, keep every output line, return its exit code."""
    _line("> " + " ".join(f'"{c}"' if " " in str(c) else str(c) for c in cmd[1:]))
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
    try:
        proc = subprocess.Popen([str(c) for c in cmd], cwd=str(REPO), env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, encoding="utf-8", errors="replace", bufsize=1)
    except (OSError, ValueError) as exc:
        _line(f"could not start: {exc}")
        return 1
    assert proc.stdout is not None
    for text in proc.stdout:
        _line(text)
    return proc.wait()


def begin(job: str, body: dict, on_configured=None, on_indexed=None) -> tuple:
    """Start `survey` (init: the first map and config) or `ingest` in a
    background thread. (answer, HTTP status). One job at a time."""
    with _LOCK:
        if _STATE["state"] == "running":
            return {"error": f"a {_STATE['job']} is still running", "job": _STATE["job"]}, 409
    cmd: list = [sys.executable, str(REPO / "pharos.py")]
    if job == "survey":
        raw = str(body.get("root") or "").strip()
        if not raw:
            return {"error": "root missing"}, 400
        if "\x00" in raw or raw.startswith(("\\\\", "//")):
            return {"error": "not a local folder path"}, 400
        root = Path(raw).expanduser()
        if not root.is_absolute() or not root.is_dir():
            return {"error": f"not a folder: {raw}"}, 400
        root = root.resolve()
        if root.parent == root and not body.get("whole_drive"):      # a whole drive
            return {"error": "that is a whole drive; pick the folder that holds your assets"}, 400
        reg = str(body.get("registry_dir") or "").strip()
        if reg and ("\x00" in reg or not Path(reg).expanduser().is_absolute()):
            return {"error": "registry folder must be an absolute local path"}, 400
        if not reg:
            cur = config._CFG.get("registry_dir") or ""
            same = (config._CFG.get("library_root") or "").replace("\\", "/").lower() \
                == str(root).replace("\\", "/").lower()
            reg = cur if (cur and same) else str(Path.home() / ".pharos" / "registry")
        # a registry belongs to one library: another library's registry is
        # refused when it was named, and replaced by its own when it was
        # only the default (init would write a config that cannot start)
        from . import db
        owner = db.registry_foreign(Path(reg).expanduser().resolve() / "assets.sqlite", root)
        if owner and body.get("registry_dir"):
            return {"error": db.foreign_registry_message(
                Path(reg) / "assets.sqlite", owner, root), "step": "survey"}, 409
        if owner:
            reg = str(Path.home() / ".pharos" / f"registry-{root.name}")
        cmd += ["init", str(root), "--registry-dir", reg, "--force"]
        after = on_configured
    elif job == "ingest":
        if not config.is_configured():
            return {"error": "no library chosen yet", "step": "survey"}, 400
        pend = [r["folder"] for r in library_map.pending(library_map.load())]
        if pend and not body.get("force"):
            return {"error": "decisions are still pending", "pending": pend}, 400
        cmd += ["ingest"]
        after = on_indexed
    else:
        return {"error": "unknown job"}, 400
    # one job at a time: the check and the claim are one step
    with _LOCK:
        if _STATE["state"] == "running":
            return {"error": f"a {_STATE['job']} is still running", "job": _STATE["job"]}, 409
        _STATE.update(state="running", job=job, lines=[], error="", started=time.time(),
                      finished=0.0, result={})

    def run() -> None:
        try:
            rc = _stream(cmd)
            if rc != 0:
                _set(state="failed", finished=time.time(),
                     error=f"{job} exited with {rc}; the log above says why")
                return
            config.reload()
            result = (after() or {}) if after else {}
        except Exception as exc:                          # noqa: BLE001
            _set(state="failed", finished=time.time(), error=f"{job}: {exc}")
            return
        _set(state="done", finished=time.time(), result=result)

    threading.Thread(target=run, name=f"pharos-setup-{job}", daemon=True).start()
    return {"ok": True, "job": job, "state": "running"}, 202


def progress() -> dict:
    return _public_progress()


def doctor_verdict() -> dict:
    """`pharos.py doctor --json`, reduced to what the page shows."""
    import json
    try:
        r = subprocess.run([sys.executable, str(REPO / "pharos.py"), "doctor", "--json"],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", cwd=str(REPO), timeout=120)
        d = json.loads(r.stdout[r.stdout.find("{"):])
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        return {"verdict": "unknown", "error": str(exc), "gaps": []}
    gaps = [{"name": c["name"], "status": c["status"], "detail": c["detail"], "fix": c["fix"]}
            for c in d.get("checks", [])
            if c["status"] not in ("ok", "found", "running", "absent")]
    return {"verdict": d.get("verdict", "unknown"), "gaps": gaps}
