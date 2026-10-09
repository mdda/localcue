# /// script
# dependencies = [
#   "flask>=3.0",
#   "flask-socketio>=5.3",
#   "qrcode>=7.4",
#   "Pillow>=10.0",
#   "vosk>=0.3.45",
#   "metaphone>=0.6",
#   "pasimple>=0.0.3",
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

from voice import VoiceFollower, DEFAULT_MODEL

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

# pos = position in the script as a (fractional) character offset — the text on the
# reading line of every device. playing/speed = auto-scroll state (run on the phone).
state = {"text": "", "fontSize": 36, "marginPct": 15, "flipped": True, "brightness": 25,
         "pos": 0.0, "playing": False, "speed": 30, "voice": {"state": "off"}}

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
    emit("play_control", {"action": "play" if state["playing"] else "pause", "speed": state["speed"]})
    emit("voice_status", state["voice"])
    # Last connect-time push; "initial" lets the desktop tell it apart from live moves.
    emit("position", {"pos": state["pos"], "initial": True})


@socketio.on("position")
def handle_position(data):
    state["pos"] = data.get("pos", 0.0)
    if voice:
        voice.reset()  # moved by hand (or by auto-scroll): forget the words heard so far
    emit("position", data, broadcast=True, include_self=False)


# ---- Voice-follow (voice.py) ----
voice = None  # VoiceFollower, created in __main__ (needs the --mic / --vosk-model options)


def voice_position(pos):
    # Speech matched the script: everyone moves there (the phone glides rather than jumps).
    state["pos"] = pos
    socketio.emit("position", {"pos": pos, "glide": True})


def voice_status(status):
    state["voice"] = status
    socketio.emit("voice_status", status)


@socketio.on("voice_toggle")
def handle_voice_toggle(data):
    if data.get("on"):
        voice.start()
    else:
        voice.stop()


@socketio.on("play_control")
def handle_play_control(data):
    # action: play | pause | skip (with seconds) | speed — all carry the current speed.
    state["speed"] = data.get("speed", state["speed"])
    if data.get("action") in ("play", "pause"):
        state["playing"] = data["action"] == "play"
    emit("play_control", data, broadcast=True, include_self=False)


@socketio.on("play_state")
def handle_play_state(data):
    # Phone reports it stopped by itself (reached the end of the script).
    state["playing"] = data.get("playing", False)
    emit("play_state", data, broadcast=True, include_self=False)


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
    parser.add_argument("--mic", metavar="SOURCE",
                        help="PulseAudio/PipeWire source for voice-follow (default: the default source; "
                             "list with `pactl list short sources`)")
    parser.add_argument("--vosk-model", default=DEFAULT_MODEL, metavar="NAME_OR_DIR",
                        help=f"Vosk model name (downloaded to ~/.cache/vosk on first use) or directory "
                             f"(default: {DEFAULT_MODEL})")
    args = parser.parse_args()
    voice = VoiceFollower(get_text=lambda: state["text"], get_pos=lambda: state["pos"],
                          on_position=voice_position, on_status=voice_status,
                          model=args.vosk_model, device=args.mic)
    if args.script:
        load_script(args.script)
        print(f"Script:  {script_path}" + ("" if os.path.exists(script_path) else "  (new file, created on Save)"))

    print(f"Desktop: http://localhost:{PORT}/")
    print(f"Mobile:  {MOBILE_URL}")
    socketio.run(app, host="0.0.0.0", port=PORT, debug=False, allow_unsafe_werkzeug=True)
