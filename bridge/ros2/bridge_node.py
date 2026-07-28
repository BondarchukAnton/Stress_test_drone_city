#!/usr/bin/env python3
"""Единая ROS2 bridge-нода дрона (sverk-ros2), два режима: city | painter.

Тот же HTTP-контракт, что у bridge/mock.py (§6) — агент/PicoClaw не меняются
при переходе сим → железо. Реализация поверх фасада `sverk_interfaces` из
репы sverk-ros2 (main, включает коммит 8628ff4 с ros2_mcp/VLM):

    drone.control.navigate(x,y,z,yaw,speed,frame_id,auto_arm)   полёт
    drone.control.navigate_wait(...)                            полёт с ожиданием
    drone.control.land(timeout)                                 посадка (Trigger)
    drone.control.get_telemetry(frame_id)                       позиция (x,y,z)
    drone.led.set_effect(effect, r,g,b)                         LED-лента
    drone.image.take_picture(timeout)                           кадр с камеры
    бортовой VLM: ros2_mcp (порт 9092, /vlm/query)              анализ снимка

ВСЁ движение — в кадре ARUCO (BRIDGE_FRAME, по умолчанию aruco_map): на поле
стоят маркеры позиционирования, и перелёты/взлёт/удержание считаются в их
системе координат, не по body. Body используется ТОЛЬКО как аварийный подскок
на взлёте, если маркеры с земли ещё не видны (TAKEOFF_BODY_FALLBACK=1), и
сразу после набора высоты дрон перехватывается в aruco-кадр.

Режимы (BRIDGE_MODE, по умолчанию из FLEET):
  city    — /photograph {cell}, /analyze, /move, /pose, /land, /takeoff, /led
  painter — /move, /spray (серво краскопульта), /pose, /land, /takeoff, /led
Чужой для режима эндпоинт отвечает 409 — дрон не того флота виден сразу.

Сетка ↔ метры (city): клетка [cx,cy] → (FIELD_ORIGIN_X + cx*CELL_SIZE_M,
FIELD_ORIGIN_Y + cy*CELL_SIZE_M) в кадре BRIDGE_FRAME (обычно aruco_map).
После взлёта /pose отдаёт РЕАЛЬНУЮ телеметрию — на этом держится продолжение
миссии с фактического места после замены аккумулятора.

ПРОВЕРИТЬ НА СТЕНДЕ (написано против фасада, без живого полигона):
имена кадров (aruco_map/map), знаки осей, серво-углы краскопульта.
"""
from __future__ import annotations

import json
import math
import os
import re
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# --- конфиг (env, профиль проекта пишется на все дроны) ---------------------
MODE = (os.environ.get("BRIDGE_MODE") or os.environ.get("FLEET") or "city").lower()
if MODE in ("painters", "художники"):
    MODE = "painter"
PORT = int(os.environ.get("PORT", "9000"))
FRAME = os.environ.get("BRIDGE_FRAME", "aruco_map")
CELL_SIZE = float(os.environ.get("CELL_SIZE_M", "0.8"))
ORIGIN_X = float(os.environ.get("FIELD_ORIGIN_X", "0.0"))
ORIGIN_Y = float(os.environ.get("FIELD_ORIGIN_Y", "0.0"))
FLIGHT_ALT = float(os.environ.get("FLIGHT_ALT_M", "1.5"))
NAV_SPEED = float(os.environ.get("NAV_SPEED_MS", "0.5"))
NAV_TOL = float(os.environ.get("NAV_TOLERANCE_M", "0.3"))
NAV_TIMEOUT = float(os.environ.get("NAV_TIMEOUT_S", "30"))
# аварийный body-подскок на взлёте, когда aruco-маркеры с земли не видны
TAKEOFF_BODY_FALLBACK = os.environ.get("TAKEOFF_BODY_FALLBACK", "1") \
    not in ("0", "", "false", "no")
ARTIFACTS = Path(os.environ.get("ARTIFACTS_DIR", "/tmp/artifacts"))
AGENT = os.environ.get("AGENT_ID", "drone")
ROS2_MCP_URL = os.environ.get("ROS2_MCP_URL", "http://localhost:9092")
VLM_CARGO_PROMPT = os.environ.get(
    "VLM_CARGO_PROMPT",
    "На снимке сверху клетка поля. Есть ли на ней груз (коробка/контейнер)? "
    'Ответь ТОЛЬКО JSON: {"cargo": true|false, "confidence": 0..1, '
    '"label": "короткая метка"}')
# краскопульт (painter): серво из sverk_interfaces.init(servo_*)
SPRAY_ON_DEG = float(os.environ.get("SPRAY_ON_DEG", "60"))
SPRAY_OFF_DEG = float(os.environ.get("SPRAY_OFF_DEG", "0"))
CANVAS_SCALE = float(os.environ.get("CANVAS_SCALE_M", "0.025"))  # м на единицу холста
CANVAS_ORIGIN_X = float(os.environ.get("CANVAS_ORIGIN_X", "0.0"))
CANVAS_ORIGIN_Y = float(os.environ.get("CANVAS_ORIGIN_Y", "0.0"))
PAINT_ALT = float(os.environ.get("PAINT_ALT_M", "1.0"))

_drone = None
_lock = threading.Lock()          # sverk_interfaces — один вызов за раз
_landed = {"v": True}             # на земле до первого /takeoff


def drone():
    """Ленивая инициализация фасада (нода поднимается до полётного стека)."""
    global _drone
    if _drone is None:
        import sverk_interfaces
        _drone = sverk_interfaces.init(Nodename=f"openclaw_bridge_{re.sub(r'[^a-z0-9_]', '_', AGENT.lower())}")
    return _drone


# --- координаты --------------------------------------------------------------
def cell_to_m(cell) -> tuple[float, float]:
    return (ORIGIN_X + float(cell[0]) * CELL_SIZE,
            ORIGIN_Y + float(cell[1]) * CELL_SIZE)


def m_to_cell(x: float, y: float) -> list[int]:
    return [int(round((x - ORIGIN_X) / CELL_SIZE)),
            int(round((y - ORIGIN_Y) / CELL_SIZE))]


def canvas_to_m(p) -> tuple[float, float]:
    return (CANVAS_ORIGIN_X + float(p[0]) * CANVAS_SCALE,
            CANVAS_ORIGIN_Y + float(p[1]) * CANVAS_SCALE)


# --- полётные операции --------------------------------------------------------
def _aruco_telemetry(d, timeout: float = 2.0):
    """Телеметрия в aruco-кадре или None (маркеры не видны / нет локализации)."""
    try:
        t = d.control.get_telemetry(frame_id=FRAME, timeout=timeout)
        if t is not None and all(math.isfinite(float(getattr(t, k)))
                                 for k in ("x", "y", "z")):
            return t
    except Exception:  # noqa: BLE001
        pass
    return None


def do_takeoff() -> dict:
    """Взлёт В КАДРЕ ARUCO: держим текущие x,y по маркерам поля и поднимаемся
    на FLIGHT_ALT — движение с первого метра считается в системе координат
    поля, не по body. Если маркеры с земли не видны, разрешён короткий
    body-подскок (TAKEOFF_BODY_FALLBACK=1), после набора высоты дрон сразу
    перехватывается в aruco-кадр (hold текущей aruco-точки). /pose после
    взлёта отдаёт фактическую позицию по маркерам."""
    with _lock:
        d = drone()
        t0 = _aruco_telemetry(d)
        if t0 is not None:
            d.control.navigate(x=float(t0.x), y=float(t0.y), z=FLIGHT_ALT,
                               yaw=0.0, speed=NAV_SPEED, frame_id=FRAME,
                               auto_arm=True)
        elif TAKEOFF_BODY_FALLBACK:
            d.control.navigate(x=0.0, y=0.0, z=FLIGHT_ALT, yaw=0.0,
                               speed=NAV_SPEED, frame_id="body", auto_arm=True)
        else:
            raise RuntimeError(
                f"нет aruco-телеметрии в кадре {FRAME} — взлёт отменён "
                "(TAKEOFF_BODY_FALLBACK=0)")
        deadline = time.monotonic() + NAV_TIMEOUT
        t = None
        while time.monotonic() < deadline:
            t = _aruco_telemetry(d)
            if t is not None and abs(float(t.z)) > FLIGHT_ALT * 0.6:
                break
            time.sleep(0.5)
        if t is None:
            raise RuntimeError(
                f"после взлёта нет aruco-телеметрии в кадре {FRAME} — "
                "проверьте маркеры/локализацию")
        if t0 is None:
            # взлетали по body — закрепляемся в aruco-кадре (hold точки поля),
            # чтобы дальше не накапливать дрейф
            d.control.navigate(x=float(t.x), y=float(t.y), z=FLIGHT_ALT,
                               yaw=0.0, speed=NAV_SPEED, frame_id=FRAME,
                               auto_arm=False)
        _landed["v"] = False
        return {"ok": True, "landed": False, "frame": FRAME,
                "aruco_from_ground": t0 is not None,
                "pose": m_to_cell(float(t.x), float(t.y)),
                "m": [round(float(t.x), 2), round(float(t.y), 2), round(float(t.z), 2)]}


def do_land() -> dict:
    with _lock:
        d = drone()
        resp = d.control.land(timeout=15.0)
        _landed["v"] = True
        ok = bool(getattr(resp, "success", True))
        return {"ok": ok, "landed": True,
                "message": str(getattr(resp, "message", ""))}


def do_move_cell(cell) -> dict:
    x, y = cell_to_m(cell)
    with _lock:
        d = drone()
        d.control.navigate_wait(x=x, y=y, z=FLIGHT_ALT, yaw=0.0, speed=NAV_SPEED,
                                frame_id=FRAME, auto_arm=False,
                                timeout=NAV_TIMEOUT, tolerance=NAV_TOL)
    return {"ok": True, "pose": list(cell)}


def do_pose() -> dict:
    with _lock:
        t = drone().control.get_telemetry(frame_id=FRAME, timeout=2.0)
    ok = t is not None and all(math.isfinite(float(getattr(t, k, float("nan"))))
                               for k in ("x", "y"))
    if not ok:                     # aruco lost -> fall back to the operator's cell
        if _manual_cell["v"] is not None:
            return {"xy": list(_manual_cell["v"]), "m": None, "heading": 0.0,
                    "landed": _landed["v"], "source": "manual"}
        raise RuntimeError(f"нет aruco-телеметрии в кадре {FRAME} и клетка не "
                           f"задана — вызови /set_cell")
    return {"xy": m_to_cell(float(t.x), float(t.y)),
            "m": [round(float(t.x), 2), round(float(t.y), 2), round(float(t.z), 2)],
            "heading": round(float(getattr(t, "yaw", 0.0)), 3),
            "landed": _landed["v"]}


def do_led(body: dict) -> dict:
    effect = str(body.get("effect") or "fill")
    r, g, b = (int(body.get(k, 0)) & 255 for k in ("r", "g", "b"))
    with _lock:
        d = drone()
        if not getattr(d, "led", None):
            raise RuntimeError("led_control недоступен (drone.led is None)")
        d.led.set_effect(effect, r=r, g=g, b=b)
    secs = float(body.get("seconds") or 0)
    if secs > 0:
        def _off():
            time.sleep(secs)
            try:
                with _lock:
                    drone().led.set_effect("fill", r=0, g=0, b=0)
            except Exception:  # noqa: BLE001
                pass
        threading.Thread(target=_off, daemon=True).start()
    return {"ok": True, "effect": effect, "r": r, "g": g, "b": b}


def do_photograph_cell(cell) -> dict:
    """Кадр вниз с текущей позиции (агент уже прилетел в клетку)."""
    with _lock:
        d = drone()
        frame = d.image.take_picture(timeout=3.0)
        img = d.image.to_cv2(frame)
    import cv2
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    dst = ARTIFACTS / f"{AGENT}-cell-{int(cell[0])}-{int(cell[1])}.jpg"
    cv2.imwrite(str(dst), img)
    return {"image_path": str(dst), "cell": [int(cell[0]), int(cell[1])],
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}


def do_analyze(body: dict) -> dict:
    """«Сделать фото и сразу его проанализировать»: кадр -> бортовой VLM
    через ros2_mcp: POST /call {"name":"vlm_query","arguments":{image_path,
    query,use_npu}} -> {"content":[{"text": json({response, inference_time_ms})}]}.
    close_look — верификационный заход (кадр ниже обеспечивает сам полёт)."""
    cell = body.get("cell") or [0, 0]
    shot = do_photograph_cell(cell)
    payload = json.dumps({"name": "vlm_query",
                          "arguments": {"image_path": shot["image_path"],
                                        "query": VLM_CARGO_PROMPT,
                                        "use_npu": True}}).encode()
    req = urllib.request.Request(f"{ROS2_MCP_URL.rstrip('/')}/call",
                                 data=payload,
                                 headers={"content-type": "application/json"},
                                 method="POST")
    with urllib.request.urlopen(req, timeout=150) as resp:
        out = json.loads(resp.read().decode())
    inner = json.loads(out["content"][0]["text"])
    text = str(inner.get("response") or "")
    m = re.search(r"\{.*\}", text, re.DOTALL)
    data = json.loads(m.group(0)) if m else {}
    return {"cargo": bool(data.get("cargo")),
            "confidence": float(data.get("confidence", 0.0) or 0.0),
            "label": str(data.get("label") or "unknown")[:64],
            "cell": [int(cell[0]), int(cell[1])],
            "close_look": bool(body.get("close_look")),
            "image_path": shot["image_path"],
            "vlm_ms": inner.get("inference_time_ms")}


def do_spray(body: dict) -> dict:
    """Краскопульт художника: серво «нажимает» баллон (drone.gpio.servo_*
    из фасада: servo_enable / servo_set_angle / servo_center), дрон ведёт
    полилинию. Углы нажатия/отпускания — env SPRAY_ON_DEG / SPRAY_OFF_DEG."""
    points = body.get("points") or []
    if not points:
        return {"ok": False, "error": "points required"}
    with _lock:
        d = drone()
        first = canvas_to_m(points[0])
        d.control.navigate_wait(x=first[0], y=first[1], z=PAINT_ALT, yaw=0.0,
                                speed=NAV_SPEED, frame_id=FRAME, auto_arm=False,
                                timeout=NAV_TIMEOUT, tolerance=NAV_TOL)
        d.gpio.servo_enable()
        d.gpio.servo_set_angle(SPRAY_ON_DEG)
        try:
            for p in points[1:]:
                x, y = canvas_to_m(p)
                d.control.navigate_wait(x=x, y=y, z=PAINT_ALT, yaw=0.0,
                                        speed=NAV_SPEED, frame_id=FRAME,
                                        auto_arm=False, timeout=NAV_TIMEOUT,
                                        tolerance=NAV_TOL)
        finally:
            d.gpio.servo_set_angle(SPRAY_OFF_DEG)
    return {"ok": True, "points": points, "color": body.get("color"),
            "length": len(points)}


# --- HTTP-обвязка (тот же контракт, что mock) --------------------------------
CITY_ONLY = {"/photograph", "/analyze", "/detect_obstacle"}
PAINTER_ONLY = {"/spray"}


# ---- pause / arbitrary-start / cv2-aruco reality-check (added) ----------------
_paused = {"v": False}
_manual_cell = {"v": None}     # operator-set cell used if aruco telemetry is lost
ARUCO_CHECK = os.environ.get("ARUCO_CHECK", "1") not in ("0", "", "false", "no")
ARUCO_OFF = (int(os.environ.get("ARUCO_OFFX", "0")), int(os.environ.get("ARUCO_OFFY", "0")))
_ARUCO_MARKERS = os.environ.get(
    "ARUCO_MARKERS",
    "/home/sverk/sverk_ws/src/sverk_drone/odometry/aruco/aruco_map/config/markers.txt")
_ID2CELL = None


def _id2cell():
    global _ID2CELL
    if _ID2CELL is None:
        m = {}
        try:
            for ln in open(_ARUCO_MARKERS):
                if ln.startswith("#"):
                    continue
                p = ln.split()
                if len(p) >= 4:
                    m[int(p[0])] = [int(round((float(p[2]) - ORIGIN_X) / CELL_SIZE)),
                                    int(round((float(p[3]) - ORIGIN_Y) / CELL_SIZE))]
        except Exception:  # noqa: BLE001
            pass
        _ID2CELL = m
    return _ID2CELL


def _cv2_aruco_cell(img):
    """Independent cv2 reality-check: DICT_4X4_1000 marker nearest the frame
    centre -> field cell. Does NOT touch the (possibly unstable) ROS2 aruco->EKF
    flight path; it just reads what the camera sees to confirm/deny the pose."""
    try:
        import cv2, cv2.aruco as aruco
        ID = _id2cell()
        h, w = img.shape[:2]
        g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        dic = aruco.getPredefinedDictionary(aruco.DICT_4X4_1000)
        try:
            det = aruco.ArucoDetector(dic, aruco.DetectorParameters())
            corners, ids, _ = det.detectMarkers(g)
        except AttributeError:
            corners, ids, _ = aruco.detectMarkers(g, dic)
        if ids is None or len(ids) == 0:
            return {"cell": None, "on_field": False, "markers": 0}
        best, bestd = None, 1e9
        for c, i in zip(corners, ids.flatten()):
            pts = c.reshape(-1, 2)
            dist = math.hypot(pts[:, 0].mean() - w / 2, pts[:, 1].mean() - h / 2)
            if int(i) in ID and dist < bestd:
                bestd, best = dist, int(i)
        return {"cell": ID.get(best), "on_field": True, "markers": int(len(ids))}
    except Exception:  # noqa: BLE001
        return None


def do_pause() -> dict:
    """Battery-swap / kill-switch prep: land and freeze."""
    _paused["v"] = True
    do_land()
    return {"ok": True, "paused": True}


def do_set_cell(cell) -> dict:
    """Arbitrary start / kill-switch recovery: operator asserts which cell the
    drone is on. Used as the pose fallback when aruco telemetry is unavailable
    (markers not yet localised after replacement)."""
    _manual_cell["v"] = [int(cell[0]), int(cell[1])]
    return {"ok": True, "cell": _manual_cell["v"]}


def do_reality() -> dict:
    """On-demand cv2-aruco reality-check vs the ROS2 aruco pose."""
    with _lock:
        d = drone()
        frame = d.image.take_picture(timeout=3.0)
        img = d.image.to_cv2(frame)
        t = _aruco_telemetry(d)
    a = _cv2_aruco_cell(img) or {"cell": None, "on_field": False, "markers": 0}
    ac = ([a["cell"][0] - ARUCO_OFF[0], a["cell"][1] - ARUCO_OFF[1]]
          if a.get("cell") else None)
    ekf = m_to_cell(float(t.x), float(t.y)) if t is not None else _manual_cell["v"]
    return {"aruco_cell": ac, "ekf_cell": ekf, "on_field": a["on_field"],
            "markers": a["markers"], "reality_ok": bool(a["on_field"] and ac == ekf)}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # noqa: D102
        pass

    def _json(self, code, obj):
        raw = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path == "/healthz":
            return self._json(200, {"ok": True, "agent": AGENT, "mode": MODE,
                                    "frame": FRAME, "cell_size_m": CELL_SIZE,
                                    "landed": _landed["v"]})
        if self.path == "/reality":       # independent cv2-aruco reality-check
            try:
                return self._json(200, do_reality())
            except Exception as exc:  # noqa: BLE001
                return self._json(500, {"error": f"{type(exc).__name__}: {exc}"})
        if self.path == "/pose":
            try:
                return self._json(200, do_pose())
            except Exception as exc:  # noqa: BLE001
                return self._json(503, {"error": f"telemetry: {exc}"})
        return self._json(404, {"error": "not found"})

    def do_POST(self):
        try:
            n = int(self.headers.get("content-length", 0))
            body = json.loads(self.rfile.read(n).decode()) if n else {}
        except Exception as exc:  # noqa: BLE001
            return self._json(400, {"error": f"bad json: {exc}"})

        path = self.path
        if path in CITY_ONLY and MODE != "city":
            return self._json(409, {"error": f"bridge is in '{MODE}' mode; "
                                             f"{path} is city-only"})
        if path in PAINTER_ONLY and MODE != "painter":
            return self._json(409, {"error": f"bridge is in '{MODE}' mode; "
                                             f"{path} is painter-only"})
        if _landed["v"] and path in ("/photograph", "/analyze", "/move", "/spray"):
            return self._json(409, {"error": "landed (сначала /takeoff)"})

        try:
            if path == "/led":
                return self._json(200, do_led(body))
            if path == "/land":
                return self._json(200, do_land())
            if path == "/pause":
                return self._json(200, do_pause())
            if path == "/set_cell":
                c = body.get("cell")
                if not (isinstance(c, list) and len(c) == 2):
                    return self._json(400, {"error": "cell must be [x,y]"})
                return self._json(200, do_set_cell(c))
            if path == "/takeoff":
                _paused["v"] = False       # resume after pause/battery-swap
                return self._json(200, do_takeoff())
            if path == "/move":
                to = body.get("to")
                if not (isinstance(to, list) and len(to) == 2):
                    return self._json(400, {"error": "to must be [x,y]"})
                return self._json(200, do_move_cell(to))
            if path == "/photograph":
                cell = body.get("cell")
                if cell is None:
                    return self._json(400, {"error": "cell required (city mode)"})
                return self._json(200, do_photograph_cell(cell))
            if path == "/analyze":
                return self._json(200, do_analyze(body))
            if path == "/spray":
                return self._json(200, do_spray(body))
            if path == "/detect_obstacle":
                return self._json(501, {"error": "use /analyze (VLM) in city mode"})
            if path == "/navigate":
                return self._json(501, {"error": "rover-only endpoint"})
        except Exception as exc:  # noqa: BLE001 — строгая граница §11
            return self._json(500, {"error": f"{type(exc).__name__}: {exc}"})
        return self._json(404, {"error": "not found"})


def main():
    print(f"[bridge {AGENT}] sverk-ros2 bridge on :{PORT} mode={MODE} "
          f"frame={FRAME} cell={CELL_SIZE}m", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
