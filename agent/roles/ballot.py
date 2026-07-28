"""Ballot canonicalization: fold + cluster near-duplicate free-text subjects.

The 2026-07-02 live audit run showed the headline decision bug: «Лунный рассвет»
and «Лунный рассвет: гора из изумрудного овала…» sat on the ballot as SEPARATE
lines, split 51.4 combined points, and lost to «Ледяная сфера» (30.1) — the
group's actual preference lost to vote fragmentation.

Fix: candidates are clustered BEFORE the ballot is published (deterministic
string fold + token containment, then one optional facilitator LLM merge), and
every vote is resolved against the clusters at tally time — an off-cluster vote
is an explicit abstain, never a write-in.
"""
from __future__ import annotations

import re

_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)
_WS = re.compile(r"\s+")


def fold(s: str) -> str:
    """Case/punctuation/whitespace-insensitive form of a subject."""
    t = _PUNCT.sub(" ", str(s or "").lower().replace("ё", "е"))
    return _WS.sub(" ", t).strip()


def _stem(tok: str) -> str:
    # Crude RU/EN stem — enough to make «тишине»/«тишины» compare equal.
    return tok[:5] if len(tok) > 5 else tok


def _tokens(s: str) -> set[str]:
    return {_stem(t) for t in fold(s).split() if len(t) > 1}


def similar(a: str, b: str) -> bool:
    """True when two free-text subjects are the same painting idea."""
    fa, fb = fold(a), fold(b)
    if not fa or not fb:
        return False
    if fa == fb or fa.startswith(fb) or fb.startswith(fa):
        return True  # «title» vs «title: elaboration»
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return False
    inter = len(ta & tb)
    if inter and (inter == len(ta) or inter == len(tb)):
        return True  # one token set contains the other
    return inter / len(ta | tb) >= 0.6


def cluster(subjects: list[str]) -> list[dict]:
    """Greedy first-seen clustering -> [{"label", "variants"}].
    Label = the shortest variant (usually the bare title)."""
    clusters: list[dict] = []
    for s in subjects:
        s = str(s or "").strip()
        if not s:
            continue
        home = None
        for c in clusters:
            if any(similar(s, v) for v in c["variants"]):
                home = c
                break
        if home is None:
            clusters.append({"label": s, "variants": [s]})
        elif s not in home["variants"]:
            home["variants"].append(s)
            home["label"] = min(home["variants"], key=len)
    return clusters


def llm_merge(brain, clusters: list[dict], log_context: str = "ballot_cluster") -> list[dict]:
    """One facilitator LLM call to merge near-duplicates the string pass missed.
    Fail-safe: any invalid answer leaves the deterministic clusters untouched."""
    if getattr(brain, "is_mock", True) or len(clusters) < 2:
        return clusters
    labels = [c["label"] for c in clusters]
    listing = "\n".join(f"  {i}: «{s}»" for i, s in enumerate(labels))
    system = ("Ты — фасилитатор голосования художников. Твоя задача — склеить "
              "ДУБЛИРУЮЩИЕСЯ формулировки одного и того же сюжета, чтобы голоса "
              "не расщеплялись между вариантами одной идеи.")
    user = (
        f"Кандидаты на голосование:\n{listing}\n\n"
        "Сгруппируй индексы, обозначающие ОДИН И ТОТ ЖЕ сюжет картины "
        "(разные сюжеты НЕ склеивай; сомневаешься — оставь раздельно).\n"
        'Ответ — только JSON: {"groups": [[0,2],[1],[3]]} — каждый индекс ровно один раз.')
    data = brain.think_json(system, user, max_tokens=300)
    groups = data.get("groups") if isinstance(data, dict) else None
    if not isinstance(groups, list):
        return clusters
    seen: set[int] = set()
    flat: list[list[int]] = []
    for g in groups:
        if not isinstance(g, list) or not g:
            return clusters
        idx = []
        for i in g:
            if not isinstance(i, int) or i < 0 or i >= len(clusters) or i in seen:
                return clusters
            seen.add(i)
            idx.append(i)
        flat.append(idx)
    if seen != set(range(len(clusters))):
        return clusters
    merged = []
    for g in sorted(flat, key=min):
        variants: list[str] = []
        for i in sorted(g):
            variants.extend(v for v in clusters[i]["variants"] if v not in variants)
        merged.append({"label": min(variants, key=len), "variants": variants})
    return merged


def resolve(vote: str, clusters: list[dict]) -> str:
    """Free-text vote -> canonical cluster label, or "" (explicit abstain).
    Exact folded match wins over fuzzy so «рассвет»≠«закат» ties don't flip.
    Last resort: the vote names a token that belongs to exactly ONE candidate
    («Intentional Hybrid» → «Hybrid (2-3 days in office)» — a real sverk ballot
    that the substring matcher discarded as an abstain, flipping the outcome)."""
    v = str(vote or "").strip()
    if not v:
        return ""
    fv = fold(v)
    for c in clusters:
        if any(fv == fold(x) for x in c["variants"]):
            return c["label"]
    for c in clusters:
        if any(similar(v, x) for x in c["variants"]):
            return c["label"]
    owners: dict[str, set[int]] = {}
    for i, c in enumerate(clusters):
        for x in c["variants"]:
            for t in _tokens(x):
                owners.setdefault(t, set()).add(i)
    referenced: set[int] = set()
    for t in _tokens(v):
        referenced |= owners.get(t, set())
    if len(referenced) == 1:
        return clusters[next(iter(referenced))]["label"]
    return ""  # touches zero or several candidates → genuine abstain


def clusters_from_state(candidates: list[str], variants_map: dict | None) -> list[dict]:
    """Rebuild clusters from collaborative.json (candidates + candidate_variants)."""
    vm = variants_map or {}
    out = []
    for c in candidates:
        vs = vm.get(c) or [c]
        if c not in vs:
            vs = [c, *vs]
        out.append({"label": c, "variants": list(vs)})
    return out
