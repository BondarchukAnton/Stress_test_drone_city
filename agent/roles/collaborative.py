"""Collaborative idea building: bricks → shared draft → final candidates."""
from __future__ import annotations

import random
import re

from . import messages_of


def collab_path(bb):
    return bb.root / "state" / "collaborative.json"


def read_collab(bb) -> dict:
    return bb.read_json(collab_path(bb), {
        "bricks": [],
        "draft": "",
        "build_notes": [],
        "candidates": [],
    })


def write_collab(bb, obj: dict) -> None:
    bb.write_json(collab_path(bb), obj)


def bricks_from_messages(ctx) -> list[dict]:
    out = []
    for m in messages_of(ctx, type_="BRICK", phase="PROPOSE"):
        p = m.get("payload", {})
        piece = (p.get("piece") or p.get("subject") or "").strip()
        if piece:
            out.append({
                "from": m["from"],
                "name": p.get("name", m["from"]),
                "piece": piece,
                "color": p.get("color", ""),
                "shapes": p.get("shapes") or [],
            })
    return out


def build_notes_from_messages(ctx) -> list[dict]:
    notes = []
    for m in messages_of(ctx, type_="BUILD", phase="BUILD"):
        p = m.get("payload", {})
        notes.append({
            "from": m["from"],
            "name": p.get("name", m["from"]),
            "addition": (p.get("addition") or m.get("body") or "")[:300],
            "draft_line": (p.get("draft_line") or "")[:200],
            "round": p.get("round", 0),
        })
    return notes


def merge_draft(bricks: list[dict], notes: list[dict]) -> str:
    if not bricks:
        return "общий пейзаж из четырёх цветов"
    by_name = {b["name"]: b["piece"] for b in bricks}
    pieces = " + ".join(f"{b['name']}: {b['piece']}" for b in bricks)
    latest = ""
    for n in reversed(notes):
        if n.get("draft_line"):
            latest = n["draft_line"]
            break
    if latest:
        return latest
    if len(by_name) >= 2:
        names = list(by_name.keys())
        return f"{by_name[names[0]]} и {by_name[names[1]]} на одном холсте"
    return pieces[:120]


def synthesize_candidates(draft: str, bricks: list[dict], paint_seed: int) -> list[str]:
    """Финальные варианты для голосования = РАЗНЫЕ ЦЕЛЬНЫЕ сюжеты, по одному на
    выбор. НЕ склеиваем кирпичи в «А — Б — В» (это давало «франкенштейн-сюжет»,
    где каждый дрон рисовал свой фрагмент). Каждый кандидат — один чистый замысел,
    голосование выбирает ОДИН."""
    rng = random.Random(paint_seed)
    seen: list[str] = []
    for b in bricks:
        piece = re.sub(r"\s+", " ", (b.get("piece") or "")).strip()
        if piece and piece not in seen:
            seen.append(piece[:60])
    if not seen:
        base = re.sub(r"\s+", " ", draft or "").strip()[:60] or "пейзаж"
        seen = [base]
    rng.shuffle(seen)  # avoid ballot position bias
    return seen[:4]


def candidates_for_vote(ctx) -> list[str]:
    collab = read_collab(ctx.bb)
    cands = collab.get("candidates") or []
    return [c for c in cands if isinstance(c, str) and c.strip()]


def collab_summary_text(collab: dict) -> str:
    lines = ["ЧЕРНОВИК СОВМЕСТНОЙ ИДЕИ:"]
    if collab.get("draft"):
        lines.append(f"  «{collab['draft']}»")
    lines.append("Кирпичи:")
    for b in collab.get("bricks") or []:
        lines.append(f"  • {b.get('name', '?')}: {b.get('piece', '?')}")
    if collab.get("candidates"):
        lines.append("Финальные варианты на голос:")
        for i, c in enumerate(collab["candidates"], 1):
            lines.append(f"  {i}. «{c}»")
    return "\n".join(lines)
