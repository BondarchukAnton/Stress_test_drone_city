"""VLM-критик (жюри) для задачи painting: смотрит на холст каждые пару секунд
и оценивает красоту картины 1..100; в конце выносит финальный вердикт.

«Камера» в симуляции — рендер холста из shape/stroke-событий доски
(agent/raster.py); на железе достаточно подменить источник кадра (env
CRITIC_CAMERA_URL: любой URL, отдающий JPEG/PNG кадр) — протокол не меняется.
Оценка: настоящий VLM, если доступен (brain.see: anthropic/openai из коробки,
sverk/ollama при MODEL_VISION), иначе детерминированная эвристика по метрикам
холста (покрытие, разнообразие цветов, баланс) — mock-демо работает без
ключей.

Пишет (single-writer): state/critic.json {score, comment, final, history[]}.
События: {"kind":"critic", score, comment, final} — их рисует экран жюри
viz/critic.html (:8080/critic). Финальный вердикт постится и сообщением
REPORT, чтобы остаться в протоколе прогона.

Критик включён в painting-лаунчеры (CRITIC=0 отключает); loop.py даёт ему
один дополнительный шаг на phase=DONE (STEP_AT_DONE) — там и рождается
финальный вердикт.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.request

from brain import parse_llm_json
from raster import canvas_stats, png_bytes, render_events

from . import make_msg

_T = {"last": 0.0, "final_done": False, "last_band": -1, "ticks": 0}


def _interval() -> float:
    return max(0.5, float(os.environ.get("CRITIC_INTERVAL", "2")))


def _read_events(bb) -> list[dict]:
    """Холст живёт в events.jsonl (shape/stroke). Критик со-локализован с
    доской (FileBoard); на HttpBoard событий не читать — критик тогда молчит."""
    path = getattr(bb, "events_path", None)
    if path is None or not path.exists():
        return []
    out = []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if e.get("kind") in ("shape", "stroke", "canvas_clear"):
                    out.append(e)
    except OSError:
        return []
    # canvas_clear = новый прогон: берём только события после последней очистки
    for i in range(len(out) - 1, -1, -1):
        if out[i].get("kind") == "canvas_clear":
            return out[i + 1:]
    return out


def _camera_frame(ctx) -> tuple[bytes | None, dict]:
    """(png_bytes | None, stats). CRITIC_CAMERA_URL — реальная камера (кадр по
    HTTP); иначе рендерим холст из событий доски (сим-камера)."""
    url = os.environ.get("CRITIC_CAMERA_URL")
    if url:
        try:
            with urllib.request.urlopen(url, timeout=5) as r:
                return r.read(), {}
        except Exception:  # noqa: BLE001 — камера моргнула, вернёмся к рендеру
            pass
    events = _read_events(ctx.bb)
    if not events:
        return None, {"coverage": 0.0, "colors": 0, "balance": 0.0}
    cv = (ctx.scenario_map or {}).get("canvas") or {"w": 120, "h": 120}
    img = render_events(events, int(cv.get("w", 120)), int(cv.get("h", 120)),
                        px=int(os.environ.get("CRITIC_PX", "240")))
    return png_bytes(img), canvas_stats(img)


def _heuristic(ctx, stats: dict, final: bool) -> tuple[int, str]:
    """Детерминированная оценка без VLM: растёт с покрытием/разнообразием/
    балансом, слегка «дышит» от тика к тику (по сиду — воспроизводимо)."""
    cov = float(stats.get("coverage", 0.0))
    colors = min(4, int(stats.get("colors", 0)))
    balance = float(stats.get("balance", 0.0))
    base = 22 + 48 * min(1.0, cov * 1.6) + 12 * colors / 4 + 10 * balance
    seed = f"{ctx.agent_id}|{_T['ticks'] if not final else 'final'}"
    wobble = int(hashlib.sha256(seed.encode()).hexdigest()[:2], 16) % 9 - 4
    score = max(3, min(97, int(base) + (wobble if not final else 2)))
    subject = (ctx.bb.read_decision() or {}).get("subject") or "картина"
    if final:
        comment = (f"«{subject}»: композиция сложилась — покрытие "
                   f"{cov:.0%}, цветов {colors}, баланс {balance:.0%}.")
    elif cov < 0.05:
        comment = "холст почти пуст — жду первых мазков"
    elif cov < 0.25:
        comment = "эскиз проступает, цвет ещё робкий"
    elif cov < 0.55:
        comment = "слои ложатся, появляется глубина"
    else:
        comment = "плотная фактура, картина почти собралась"
    return score, comment


def _judge(ctx, final: bool) -> tuple[int, str, dict]:
    frame, stats = _camera_frame(ctx)
    if frame is not None and ctx.brain.vision_available():
        system = (ctx.soul_body or "") or (
            "Ты — художественный критик на выставке дронов-художников.")
        user = (
            ("Картина ЗАВЕРШЕНА — вынеси финальный вердикт. " if final else
             "Картина ещё пишется — промежуточная оценка. ")
            + "Оцени, насколько она красива, ЧИСЛОМ от 1 до 100, и дай короткий "
              'комментарий по-русски. Ответ — один JSON: {"score": 1..100, '
              '"comment": "1-2 предложения"}')
        raw = ctx.brain.see(system, user, frame, log_context="critic")
        data, _err = parse_llm_json(raw or "")
        if isinstance(data, dict):
            try:
                score = max(1, min(100, int(float(data.get("score")))))
                comment = str(data.get("comment") or "").strip()[:300]
                if comment:
                    return score, comment, stats
            except (TypeError, ValueError):
                pass
        ctx.emit({"kind": "llm_error", "from": ctx.agent_id, "phase": "EXECUTE",
                  "context": "critic", "attempt": 1, "max_attempts": 1,
                  "error": "VLM не дал score/comment — эвристика", "exhausted": True})
    score, comment = _heuristic(ctx, stats, final)
    return score, comment, stats


def _write_state(ctx, score: int, comment: str, final: bool, stats: dict) -> None:
    path = ctx.bb.state / "critic.json" if hasattr(ctx.bb, "state") else None
    if path is None:
        return
    cur = ctx.bb.read_json(path, {}) or {}
    hist = cur.get("history") or []
    hist.append({"t": time.time(), "score": score})
    cur.update({
        "score": score, "comment": comment, "final": final,
        "stats": stats, "history": hist[-300:],
        "subject": (ctx.bb.read_decision() or {}).get("subject"),
    })
    ctx.bb.write_json(path, cur)


def step(ctx):
    if ctx.config.get("task") != "painting":
        return {"thought": "Жюри работает только на выставке (task=painting).",
                "messages": [], "idle": True}
    if not hasattr(ctx.bb, "events_path"):
        return {"thought": "Нет доступа к ленте событий (HttpBoard) — молчу.",
                "messages": [], "idle": True}

    phase = ctx.phase.get("phase")
    dec = ctx.bb.read_decision() or {}
    finished = phase == "DONE" or bool(str(dec.get("result") or "").strip())

    if phase in ("INIT", "CHAT", "PROPOSE", "BUILD", "CONVERGE") and not finished:
        return {"thought": "Жду, пока художники возьмутся за холст.",
                "messages": [], "idle": True}

    # финальный вердикт — один раз, когда картина готова
    if finished:
        if _T["final_done"]:
            return {"thought": "Вердикт вынесен.", "messages": [], "idle": True}
        _T["final_done"] = True
        score, comment, stats = _judge(ctx, final=True)
        _write_state(ctx, score, comment, True, stats)
        ctx.emit({"kind": "critic", "from": ctx.agent_id, "score": score,
                  "comment": comment, "final": True})
        msg = make_msg(ctx, "REPORT", "all", "REPORT",
                       body=f"Финальный вердикт жюри: {score}/100 — {comment}",
                       payload={"score": score, "final": True,
                                "name": ctx.soul.get("name", "Жюри")})
        # thinking="" -> loop берёт precooked-путь (не жжёт LLM-вызов на озвучку)
        return {"thought": f"ФИНАЛЬНЫЙ ВЕРДИКТ: {score}/100. {comment}",
                "thinking": "", "messages": [msg], "idle": False}

    # промежуточные оценки: каждые CRITIC_INTERVAL секунд
    now = time.monotonic()
    if now - _T["last"] < _interval():
        return {"thought": "Смотрю на холст…", "messages": [], "idle": True}
    _T["last"] = now
    _T["ticks"] += 1
    score, comment, stats = _judge(ctx, final=False)
    _write_state(ctx, score, comment, False, stats)
    ctx.emit({"kind": "critic", "from": ctx.agent_id, "score": score,
              "comment": comment, "final": False})
    band = score // 10
    if band != _T["last_band"]:
        _T["last_band"] = band
        return {"thought": f"Оценка сейчас: {score}/100 — {comment}",
                "thinking": "", "messages": [], "idle": False}
    return {"thought": "Смотрю на холст…", "messages": [], "idle": True}
