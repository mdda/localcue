# /// script
# dependencies = [
#   "flask>=3.0",
#   "flask-socketio>=5.3",
#   "qrcode>=7.4",
#   "Pillow>=10.0",
# ]
# ///

import io
import base64
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


@app.route("/")
def desktop():
    return render_template("desktop.html", qr_data_uri=QR_DATA_URI, mobile_url=MOBILE_URL)


@app.route("/view")
def mobile():
    return render_template("mobile.html")


@socketio.on("connect")
def handle_connect():
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
    print(f"Desktop: http://localhost:{PORT}/")
    print(f"Mobile:  {MOBILE_URL}")
    socketio.run(app, host="0.0.0.0", port=PORT, debug=False, allow_unsafe_werkzeug=True)
