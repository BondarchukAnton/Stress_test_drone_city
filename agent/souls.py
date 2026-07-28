"""Minimal SOUL.md frontmatter reader (§2).

Parses the small YAML subset used by the soul files (scalars, inline lists,
and block lists of scalars) with the stdlib only -- no PyYAML dependency, so
the agent image needs no package index at build time. The markdown body after
the frontmatter is returned as the agent's system context.
"""
from __future__ import annotations

from pathlib import Path


def _coerce(val: str):
    val = val.strip()
    if val.startswith("[") and val.endswith("]"):
        inner = val[1:-1].strip()
        return [_coerce(x) for x in inner.split(",")] if inner else []
    if val in ("true", "false"):
        return val == "true"
    if val in ("null", "~", ""):
        return None
    try:
        return int(val)
    except ValueError:
        pass
    try:
        return float(val)
    except ValueError:
        pass
    return val.strip("\"'")


def load_soul(path: str | Path) -> tuple[dict, str]:
    text = Path(path).read_text(encoding="utf-8")
    meta: dict = {}
    body = text
    if text.startswith("---"):
        _, fm, body = text.split("---", 2)
        cur_key = None
        for raw in fm.splitlines():
            if not raw.strip() or raw.strip().startswith("#"):
                continue
            if raw.lstrip().startswith("- ") and cur_key is not None:
                meta.setdefault(cur_key, [])
                if isinstance(meta[cur_key], list):
                    meta[cur_key].append(_coerce(raw.lstrip()[2:]))
                continue
            if ":" in raw and not raw.startswith(" "):
                key, _, val = raw.partition(":")
                key = key.strip()
                if val.strip() == "":
                    meta[key] = []  # block list / mapping follows
                    cur_key = key
                else:
                    meta[key] = _coerce(val)
                    cur_key = None
            # nested mapping lines (e.g. style:) are ignored for this scenario
    return meta, body.strip()


def save_soul(path: str | Path, meta: dict, body: str) -> None:
    """Записать SOUL.md обратно: YAML frontmatter + тело."""
    lines = ["---"]
    for k, v in meta.items():
        if isinstance(v, list):
            lines.append(f"{k}:")
            for item in v:
                lines.append(f"  - {item}")
        elif isinstance(v, bool):
            lines.append(f"{k}: {'true' if v else 'false'}")
        elif v is None:
            lines.append(f"{k}: null")
        elif isinstance(v, (int, float)):
            lines.append(f"{k}: {v}")
        else:
            lines.append(f"{k}: \"{v}\"")
    lines.append("---")
    lines.append(body.strip())
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")
