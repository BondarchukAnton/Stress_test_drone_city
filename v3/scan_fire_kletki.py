#!/usr/bin/env python3
"""
Поиск огня и расчёт клетки-источника.
Берёт координаты из имени файла (drone110_2_5.jpg → клетка 2,5),
применяет смещение по direction из VLM и выдаёт скорректированную клетку.
Использование: python3 scan_fire_kletki.py [путь_к_папке]
"""

import sys
import json
import base64
import urllib.request
import os
import re
from datetime import datetime, timezone

FOLDER = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "img"
)

_FIRE_PROMPT = """Ты — специализированный детектор пожара на бортовом компьютере дрона.
Перед тобой кадр с камеры дрона, направленной строго вниз на игровое поле, разделенное на квадратные клетки.
Дрон висит над центром одной клетки (Центральная клетка под дроном = область в центре кадра).
По краям кадра могут быть частично видны соседние клетки.
Твоя задача — проанализировать аэроснимок игрового поля сверху и найти целевые объекты, обозначающие огонь (очаги возгорания)

### 1. ВИЗУАЛЬНЫЕ ПРИЗНАКИ И ИСКЛЮЧЕНИЯ:
- ЦЕЛЕВОЙ ОБЪЕКТ "ОГОНЬ": Отдельная пластиковая фигурка ярко-красного или тёмно-красного/бордового цвета в форме языка пламени или капли, расположенная на поле, дорогах или возле зданий. Каплевидные (или похожие на язычки пламени) объекты, если они соответствуют какому либо оттенку красного (кроме чисто чёрных и чисто белых), должны вызывать у тебя высокие показатели уверености в том что это целевой объект. Активно пользуйся показателем уверености, если что то хоть отдалёно напоминает объект синаглизируй об этом с соответствующей степенью уверености, например, 0.56 если объект маленький, имеет оттенок тёмно крсный, но не наблюдается каплевидная форма. Объктов может быть в количестве большем чем 2.
- ИСКЛЮЧЕНИЯ (НЕ считай огнем!):
- Мелкие красные светодиоды/лампочки на платах машинок, дронов или роботов.
- Красные элементы декора или кубиков конструктора на границах кадра, если они не имеют формы язычка пламени.
- Красные линии и элементы разметки карты.

### 2. ПРАВИЛА ЛОКАЛИЗАЦИИ:
Огонь на кадре может находиться ТОЛЬКО В ОДНОЙ клетке (либо под дроном, либо в одной из соседних).
Определи направление клетки с огнем относительно центра кадра:
- "center" — огонь находится в центральной клетке прямо под дроном.
- "left", "right", "up", "down", "up-left", "up-right", "down-left", "down-right" — огонь находится в соответствующей соседней клетке.
- "none" — объектов огня на кадре не обнаружено.

### 3. ФОРМАТ ОТВЕТА:
Верни ответ СТРОГО в формате JSON без Markdown-разметки, вводных слов и пояснений вне JSON:
{
  "fire": true,
  "count": 1,
  "confidence": 0.95,
  "direction": "center",
  "summary": "краткое резюме об объектах под дроном или в соседней клетке"
}

Описание полей:
- fire (boolean): true, если найден хотя бы один объект огня, иначе false.
- count (integer): количество найденных объектов огня в этой клетке (0, если fire=false).
- confidence (float): степень уверенности в детекции от 0.0 до 1.0.
- direction (string): строго одно из значений: "center", "left", "right", "up", "down", "up-left", "up-right", "down-left", "down-right" или "none" (если fire=false).
- summary (string): краткое итоговое текстовое описание."""

DIRECTION_OFFSET = {
    "center":       ( 0,  0),
    "left":         (-1,  0),
    "right":        ( 1,  0),
    "up":           ( 0,  1),
    "down":         ( 0, -1),
    "up-left":      (-1,  1),
    "up-right":     ( 1,  1),
    "down-left":    (-1, -1),
    "down-right":   ( 1, -1),
}


def parse_coords(filename):
    m = re.search(r"_(\d+)_(\d+)(?:_\d+s)?\.", filename)
    if m:
        return int(m.group(1)), int(m.group(2))
    return None


def analyze_image(filepath):
    with open(filepath, "rb") as f:
        png = f.read()
    b64 = base64.b64encode(png).decode()

    key = os.environ.get("SVERK_API_KEY") or "sk-jkx31e2PLKxCpjOynEwyxA"
    base = os.environ.get("SVERK_API_BASE") or "https://ai.sverk.tech/v1"

    payload = {
        "model": "gemma4-vlm",
        "max_tokens": 200,
        "messages": [
            {"role": "system", "content": _FIRE_PROMPT},
            {"role": "user", "content": [
                {"type": "text", "text": "Проанализируй этот кадр и найди объекты огня."},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}}
            ]}
        ]
    }

    req = urllib.request.Request(
        f"{base}/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"content-type": "application/json", "authorization": f"Bearer {key}"},
        method="POST"
    )

    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode())
        content = data["choices"][0]["message"].get("content", "")
    return content


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def scan_folder(folder):
    """Анализирует все изображения в папке, возвращает список найденных пожаров.
    Каждый элемент: {cell, count, confidence, direction, summary, drone_cell, image, ts}"""
    images = sorted(
        f for f in os.listdir(folder)
        if f.lower().endswith((".png", ".jpg", ".jpeg"))
    )
    results = []
    for name in images:
        path = os.path.join(folder, name)
        coords = parse_coords(name)
        if coords is None:
            print(f"[{name}] ПРОПУЩЕНО: не удалось извлечь координаты из имени")
            continue
        cx, cy = coords
        try:
            raw = analyze_image(path)
            raw = raw.strip()
            if raw.startswith("```"):
                raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
            result = json.loads(raw)
            if not result.get("fire"):
                continue
            direction = result.get("direction", "center")
            offset = DIRECTION_OFFSET.get(direction, (0, 0))
            tx, ty = cx + offset[0], cy + offset[1]
            entry = {
                "cell": [tx, ty],
                "count": result.get("count", 1),
                "confidence": result.get("confidence"),
                "direction": direction,
                "summary": result.get("summary"),
                "drone_cell": [cx, cy],
                "image": name,
                "ts": now_iso(),
            }
            results.append(entry)
            print(f"[{name}] огонь в ({tx};{ty})  count={entry['count']}  "
                  f"confidence={entry['confidence']}  direction={direction}")
        except Exception as e:
            print(f"[{name}] ОШИБКА: {e}")
    return results


if __name__ == "__main__":
    if not os.path.isdir(FOLDER):
        print(f"Папка не найдена: {FOLDER}")
        sys.exit(1)

    IMAGES = sorted(
        f for f in os.listdir(FOLDER)
        if f.lower().endswith((".png", ".jpg", ".jpeg"))
    )

    if not IMAGES:
        print(f"Нет изображений в {FOLDER}")
        sys.exit(0)

    print(f"Папка: {FOLDER}")
    print(f"Изображений: {len(IMAGES)}\n")

    results = scan_folder(FOLDER)

    fired = len(results)
    total = len(IMAGES)
    for r in results:
        print(f"\n[{r['image']}]")
        print(f"  дрон над:    ({r['drone_cell'][0]};{r['drone_cell'][1]})")
        print(f"  direction:    {r['direction']}  →  смещение "
              f"({r['cell'][0]-r['drone_cell'][0]:+d};{r['cell'][1]-r['drone_cell'][1]:+d})")
        print(f"  confidence:   {r['confidence']}")
        print(f"  count:        {r['count']}")
        print(f"  summary:      {r['summary']}")
        print(f"  >>> КЛЕТКА С ОГНЁМ: ({r['cell'][0]};{r['cell'][1]})")

    if fired == 0:
        print("\nОГОНЬ НЕ ОБНАРУЖЕН НИ НА ОДНОМ КАДРЕ.")
    else:
        print(f"\nГотово: {fired}/{total} кадров с огнём.")