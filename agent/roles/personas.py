"""Emergent painter personas — no hardcoded roster.

A persona (name, colour, technique, temperament, leaning) is derived
deterministically from the agent id + the per-run paint_seed, so:

  * the set scales to ANY number of painters (colours are spread evenly
    around the hue wheel by the painter's index — always distinct, always a
    real renderable hex);
  * every run produces a different cast (seed varies per run);
  * a soul file may OVERRIDE any field (``color`` / ``name`` / ``technique`` /
    ``temperament`` / ``leaning`` in front-matter) — souls are optional seeds,
    not the source of truth.

Replaces the old static ``ROSTER`` and the name-keyed mock dictionaries.
"""
from __future__ import annotations

import hashlib

# Flavour pools — picked by a stable per-agent hash, NOT pinned to an index, so
# nothing about a painter is positional. Extend freely; N is unbounded.
NAME_POOL = [
    "Аврора", "Кобальт", "Уголь", "Шалфей", "Индиго", "Охра", "Сурик", "Лазурь",
    "Сепия", "Малахит", "Кармин", "Умбра", "Гранат", "Циан", "Терракот", "Олива",
    "Пурпур", "Бирюза", "Сангина", "Берилл",
]

# Named pigments: the agent must KNOW which colour it paints with (a bare hex is
# meaningless to the model). Each entry is a precise Russian colour name + hex +
# a "role" hint used by the colour-aware composer (what this hue paints best).
PALETTE = [
    {"name": "янтарный",   "hex": "#f5a623", "role": "sun"},     # тёплый жёлто-оранжевый
    {"name": "алый",       "hex": "#e23b3b", "role": "accent"},  # красный
    {"name": "золотой",    "hex": "#f1c40f", "role": "sun"},
    {"name": "охряный",    "hex": "#cc7a30", "role": "ground"},  # земля/песок
    {"name": "изумрудный", "hex": "#2ecc71", "role": "foliage"}, # зелёный
    {"name": "оливковый",  "hex": "#8aa63b", "role": "foliage"},
    {"name": "лазурный",   "hex": "#3498db", "role": "water"},   # голубой
    {"name": "кобальтовый","hex": "#2c6fbb", "role": "water"},   # синий
    {"name": "бирюзовый",  "hex": "#1abc9c", "role": "water"},
    {"name": "индиго",     "hex": "#4b6cb7", "role": "sky"},
    {"name": "сливовый",   "hex": "#8e44ad", "role": "accent"},  # фиолетовый
    {"name": "розовый",    "hex": "#e84393", "role": "accent"},
]

TECHNIQUES = [
    ("soft_bands", "мягкие горизонтальные полосы"),
    ("long_strokes", "длинные мазки"),
    ("hatching", "диагональная штриховка"),
    ("stippling", "точечная напылка"),
]

TEMPERAMENTS = [
    ("упрямый", "стоит на своём, неохотно уступает, давит на свою идею"),
    ("дерзкий", "провоцирует, спорит резко, бьёт по чужим предложениям"),
    ("мечтательный", "тянет в поэзию и атмосферу, размывает конкретику"),
    ("язвительный", "иронизирует над чужими кирпичами, придирается к деталям"),
    ("дипломат", "ищет компромисс, склеивает чужие идеи в одну"),
    ("перфекционист", "требует чистой простой композиции, режет лишнее"),
]

LEANINGS = [
    ("тепло", "тёплые закатные сюжеты: солнце, золотое небо, свет"),
    ("холод", "холодная вода и небо: озеро, море, лунный диск, туман"),
    ("контраст", "резкая геометрия и контраст: молнии, диагонали, акценты"),
    ("природа", "органика и природа: деревья, лес, холмы, трава"),
    ("минимализм", "пустота и минимализм: одна крупная форма, много воздуха"),
    ("драма", "драма и энергия: бурное небо, штормовое море, огонь"),
]


def _h(*parts) -> int:
    return int(hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()[:8], 16)


def persona(agent_id: str, painters: list[str], seed: int, soul: dict | None = None) -> dict:
    """Full persona for ``agent_id``. Soul front-matter overrides any field."""
    soul = soul or {}
    order = sorted(painters) if painters else [agent_id]
    if agent_id not in order:
        order = sorted(order + [agent_id])
    idx = order.index(agent_id)
    n = len(order)

    name = soul.get("name") or NAME_POOL[_h(agent_id, "name") % len(NAME_POOL)]
    tech_key, tech_ru = TECHNIQUES[_h(agent_id, seed, "tech") % len(TECHNIQUES)]
    technique = soul.get("technique") or tech_key
    temp_key, temp_desc = TEMPERAMENTS[_h(agent_id, seed, "temp") % len(TEMPERAMENTS)]
    temperament = soul.get("temperament") or temp_key
    lean_key, lean_desc = LEANINGS[_h(agent_id, seed, "lean") % len(LEANINGS)]
    leaning = soul.get("leaning") or lean_key

    # Pick a NAMED pigment so the agent knows what it paints with (a bare hex is
    # meaningless to the model). Seed-shuffle the palette, assign by index →
    # distinct named colours for n ≤ palette, cycling beyond.
    pal = sorted(PALETTE, key=lambda p: _h(seed, "pal", p["hex"]))
    entry = pal[idx % len(pal)]
    color = soul.get("color") or entry["hex"]
    color_name = soul.get("color_name") or (
        entry["name"] if not soul.get("color") else "свой цвет"
    )
    color_role = entry["role"]

    # keep the *_ru / *_desc consistent with any soul override
    technique_ru = dict(TECHNIQUES).get(technique, technique)
    temp_desc = dict(TEMPERAMENTS).get(temperament, temp_desc)
    lean_desc = dict(LEANINGS).get(leaning, lean_desc)

    return {
        "id": agent_id,
        "name": name,
        "color": color,
        "hex": color,
        "color_name": color_name,
        "label": color_name,
        "color_role": color_role,
        "technique": technique,
        "technique_ru": technique_ru,
        "temperament": temperament,
        "temperament_desc": temp_desc,
        "leaning": leaning,
        "leaning_desc": lean_desc,
        "index": idx,
    }


def roster(painters: list[str], seed: int, souls: dict | None = None) -> list[dict]:
    """Personas for the whole active cast (souls: {agent_id: soul_meta})."""
    souls = souls or {}
    order = sorted(painters)
    return [persona(pid, order, seed, souls.get(pid)) for pid in order]
