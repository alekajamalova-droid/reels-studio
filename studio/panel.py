#!/usr/bin/env python3
"""Веб-панель студии Reels: http://localhost:8765"""
import json, sys, threading, time, webbrowser
from pathlib import Path
from flask import Flask, jsonify, request, send_file, abort

sys.path.insert(0, str(Path(__file__).parent))
import reels  # noqa: E402

app = Flask(__name__)
JOBS = {}  # проект -> имя задачи
VIDEO_EXT = {".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi"}


def bg(name, label, fn, *a):
    if JOBS.get(name):
        return False

    def go():
        try:
            fn(*a)
        except Exception as e:
            print("ошибка:", e, flush=True)
        finally:
            JOBS.pop(name, None)
    JOBS[name] = label
    threading.Thread(target=go, daemon=True).start()
    return True


@app.get("/")
def index():
    return send_file(Path(__file__).parent / "panel.html")


@app.get("/api/list")
def api_list():
    reels.SOURCES.mkdir(exist_ok=True)
    srcs = sorted(p.name for p in reels.SOURCES.iterdir() if p.suffix.lower() in VIDEO_EXT)
    projects = []
    for d in sorted(reels.WORK.glob("*/state.json"), key=lambda p: -p.stat().st_mtime):
        st = json.loads(d.read_text(encoding="utf-8"))
        projects.append({"name": st.get("name", d.parent.name), "source": Path(st.get("source", "")).name})
    return jsonify(sources=srcs, projects=projects)


@app.post("/api/upload")
def api_upload():
    f = request.files.get("file")
    if not f:
        abort(400)
    dst = reels.SOURCES / Path(f.filename).name
    f.save(dst)
    return jsonify(ok=True, name=dst.name)


@app.post("/api/start")
def api_start():
    src = reels.SOURCES / Path(request.json["source"]).name
    name = reels.new_project(src)
    bg(name, "Расшифровка", reels.prepare, str(src))
    return jsonify(ok=True, name=name)


@app.get("/api/state/<name>")
def api_state(name):
    d = reels.pdir(name)
    if not (d / "state.json").exists():
        abort(404)
    st = reels.load_state(name)
    st["busy"] = JOBS.get(name)
    st["has_preview"] = (d / "preview.mp4").exists()
    st["preview_t"] = int((d / "preview.mp4").stat().st_mtime) if st["has_preview"] else 0
    st["has_final"] = bool(st.get("final")) and Path(st["final"]).exists()
    st["final_name"] = Path(st["final"]).name if st.get("final") else ""
    st["has_transcript"] = (d / "transcript.txt").exists()
    return jsonify(st)


@app.route("/api/transcript/<name>", methods=["GET", "POST"])
def api_transcript(name):
    f = reels.pdir(name) / "transcript.txt"
    if request.method == "POST":
        f.write_text(request.json["text"], encoding="utf-8")
        return jsonify(ok=True)
    return jsonify(text=f.read_text(encoding="utf-8") if f.exists() else "")


@app.post("/api/preview/<name>")
def api_preview(name):
    return jsonify(ok=bg(name, "Превью", reels.safe, reels.render, name, True))


@app.post("/api/render/<name>")
def api_render(name):
    return jsonify(ok=bg(name, "Рендер", reels.safe, reels.render, name, False))


@app.get("/media/<name>/<kind>")
def media(name, kind):
    st = reels.load_state(name)
    path = {"source": st.get("source"),
            "preview": reels.pdir(name) / "preview.mp4",
            "final": st.get("final")}.get(kind)
    if not path or not Path(path).exists():
        abort(404)
    return send_file(path, conditional=True, download_name=Path(path).name,
                     as_attachment=request.args.get("dl") == "1")


if __name__ == "__main__":
    port = 8765
    if "--open" in sys.argv:
        threading.Timer(1.2, lambda: webbrowser.open(f"http://localhost:{port}")).start()
    print(f"Панель: http://localhost:{port}", flush=True)
    app.run(host="127.0.0.1", port=port, threaded=True)
