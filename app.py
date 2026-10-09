# /// script
# dependencies = [
#   "flask>=3.0",
#   "flask-socketio>=5.3",
#   "qrcode>=7.4",
#   "Pillow>=10.0",
# ]
# ///

import io
import os
import base64
import argparse
import socket as _socket

import qrcode
from flask import Flask, render_template
from flask_socketio import SocketIO, emit

PORT = 5858

app = Flask(__name__)
app.config['SECRET_KEY'] = 'teleprompt-local'
socketio = SocketIO(app, async_mode='threading', cors_allowed_origins='*')


def get_local_ip():
    s = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def make_qr_datauri(url):
    img = qrcode.make(url)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode()
    return f"data:image/png;base64,{b64}"


LOCAL_IP = get_local_ip()
MOBILE_URL = f"http://{LOCAL_IP}:{PORT}/view"
QR_DATA_URI = make_qr_datauri(MOBILE_URL)

state = {"text": "", "fontSize": 36, "marginPct": 15, "scrollFraction": 0, "flipped": True, "brightness": 25, "scrollableHeight": 0}

# Script file given on the command line (None = no file; Save is disabled).
script_path = None
saved_text = ""  # text as last loaded from / saved to script_path, for the unsaved-changes marker


def load_script(path):
    global script_path
    script_path = os.path.abspath(path)
    if os.path.exists(script_path):
        read_script()
    # else: start empty; the file is created on first Save


def read_script():
    global saved_text
    with open(script_path, encoding="utf-8") as f:
        saved_text = f.read()
    state["text"] = saved_text


def save_script(text):
    global saved_text
    # Write to a temp file then rename, so a failed write can't truncate the script.
    tmp = script_path + ".localcue-tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, script_path)
    saved_text = text


def file_info():
    return {"name": os.path.basename(script_path) if script_path else None, "savedText": saved_text}


@app.route("/")
def desktop():
    return render_template("desktop.html", qr_data_uri=QR_DATA_URI, mobile_url=MOBILE_URL)


@app.route("/view")
def mobile():
    return render_template("mobile.html")


@socketio.on("connect")
def handle_connect():
    emit("file_info", file_info())
    emit("text_broadcast", {"text": state["text"]})
    emit("settings_broadcast", {
        "fontSize": state["fontSize"],
        "marginPct": state["marginPct"],
        "flipped": state["flipped"],
        "brightness": state["brightness"],
    })
    emit("scroll_update", {"fraction": state["scrollFraction"]})
    emit("view_metrics", {"scrollableHeight": state["scrollableHeight"]})


@socketio.on("scroll_sync")
def handle_scroll(data):
    state["scrollFraction"] = data.get("fraction", 0)
    emit("scroll_update", data, broadcast=True, include_self=False)


@socketio.on("text_update")
def handle_text(data):
    state["text"] = data.get("text", "")
    emit("text_broadcast", data, broadcast=True, include_self=False)


@socketio.on("save_script")
def handle_save(data):
    if not script_path:
        emit("save_result", {"ok": False, "error": "no file given on the command line"})
        return
    text = data.get("text", "")
    try:
        save_script(text)
    except OSError as e:
        emit("save_result", {"ok": False, "error": str(e)})
        return
    # Save carries the latest text, so make sure server state and the phone match it.
    state["text"] = text
    emit("text_broadcast", {"text": text}, broadcast=True, include_self=False)
    emit("save_result", {"ok": True, **file_info()})


@socketio.on("reload_script")
def handle_reload():
    if not script_path:
        emit("reload_result", {"ok": False, "error": "no file given on the command line"})
        return
    try:
        read_script()
    except OSError as e:
        emit("reload_result", {"ok": False, "error": e.strerror or str(e)})
        return
    emit("text_broadcast", {"text": state["text"]}, broadcast=True, include_self=False)
    emit("reload_result", {"ok": True, **file_info()})


@socketio.on("view_metrics")
def handle_view_metrics(data):
    state["scrollableHeight"] = data.get("scrollableHeight", 0)
    emit("view_metrics", data, broadcast=True, include_self=False)


@socketio.on("seek_to_char")
def handle_seek(data):
    emit("seek_to_char", data, broadcast=True, include_self=False)


@socketio.on("seek_ack")
def handle_seek_ack(data):
    state["scrollFraction"] = data.get("fraction", state["scrollFraction"])
    emit("seek_ack", data, broadcast=True, include_self=False)


@socketio.on("settings_update")
def handle_settings(data):
    state["fontSize"] = data.get("fontSize", state["fontSize"])
    state["marginPct"] = data.get("marginPct", state["marginPct"])
    state["flipped"] = data.get("flipped", state["flipped"])
    state["brightness"] = data.get("brightness", state["brightness"])
    emit("settings_broadcast", data, broadcast=True, include_self=False)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="LocalCue local-network teleprompter")
    parser.add_argument("script", nargs="?", help="text file to load (and Save back to); created on first Save if missing")
    args = parser.parse_args()
    if args.script:
        load_script(args.script)
        print(f"Script:  {script_path}" + ("" if os.path.exists(script_path) else "  (new file, created on Save)"))

    print(f"Desktop: http://localhost:{PORT}/")
    print(f"Mobile:  {MOBILE_URL}")
    socketio.run(app, host="0.0.0.0", port=PORT, debug=False, allow_unsafe_werkzeug=True)
