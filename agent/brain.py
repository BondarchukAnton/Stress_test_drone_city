"""Parse reasoning-model output and run multi-turn chat with board context."""
from __future__ import annotations

import json
import os
import re
import time
import urllib.request
from typing import Any, Callable

from context_budget import RETRY_RAW_MAX_CHARS


# --- принудительный русский язык для всех нейронок -----------------------------
# Все роли/сценарии думают и говорят по-русски. Это касается ТОЛЬКО человеко-
# читаемого текста (thinking / line / message); ключи JSON, enum-значения,
# идентификаторы, координаты и код остаются как в схеме (иначе ломается протокол).
# Отключается LLM_LANG=en (или FORCE_RUSSIAN=0).
_RU_DIRECTIVE = (
    "\n\n=== ЯЗЫК ОТВЕТА ===\n"
    "Думай и пиши ВСЕ рассуждения (thinking) и все реплики/сообщения "
    "(line, message, говоримый текст) на РУССКОМ языке — всегда, независимо от "
    "языка вопроса. НЕ переводи и не меняй: ключи JSON, значения enum, "
    "идентификаторы (agent id, sector, phase), числа, координаты и код — они "
    "остаются точно как в схеме. По-русски — только то, что читает человек."
)


def _force_ru() -> bool:
    if os.environ.get("FORCE_RUSSIAN", "").strip() in ("0", "false", "no"):
        return False
    return os.environ.get("LLM_LANG", "ru").strip().lower() in ("ru", "rus", "russian", "")


def _ru(system: str) -> str:
    """Прицепить директиву русского языка к системному промпту (если включено)."""
    if not _force_ru() or not system:
        return system
    if "ЯЗЫК ОТВЕТА" in system:  # уже добавлено
        return system
    return system + _RU_DIRECTIVE


def schema_supported(brain) -> bool:
    """True when sampler-enforced JSON (response_format=json_schema) can be used.

    Verified on the sverk gateway 2026-07-02 (vllm-0.21.0 behind litellm):
    json_schema IS grammar-enforced (const-lock probe held, invalid schema
    400s). The older json_object mode is schema-less and soft — that is why
    LLM_GUIDED_JSON looked marginal. Kill switch: LLM_JSON_SCHEMA=0.
    """
    return (not getattr(brain, "is_mock", True)
            and getattr(brain, "provider", "") in ("sverk", "openai")
            and os.environ.get("LLM_JSON_SCHEMA", "1") not in ("0", "false", "no"))


def json_schema_format(name: str, schema: dict) -> dict:
    """OpenAI/vLLM response_format wrapper for a JSON schema."""
    safe = re.sub(r"[^a-zA-Z0-9_-]", "_", name or "out")[:48] or "out"
    return {"type": "json_schema", "json_schema": {"name": safe, "schema": schema}}


def _no_think_params() -> dict:
    """Disable the model's "think out loud" mode for structured/JSON calls.

    qwen35 (and other Qwen3-family reasoning models on the sverk/vLLM gateway)
    otherwise spend the whole token budget narrating an English chain-of-thought
    in `content` and never reach the JSON — every call hit `finish=length` with
    no answer and timed out. `enable_thinking:false` makes it emit the answer
    directly (~4-18s, valid JSON). Re-enable the CoT with LLM_THINKING=1.
    """
    if os.environ.get("LLM_THINKING", "0") in ("1", "true", "yes"):
        return {}
    return {"enable_thinking": False, "chat_template_kwargs": {"enable_thinking": False}}


def _parse_json(text: str):
    if not text:
        return None
    text = text.strip()
    for opener, closer in (("{", "}"), ("[", "]")):
        start = text.find(opener)
        end = text.rfind(closer)
        if start >= 0 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                continue
    return None


def _preview(text: str, limit: int = 220) -> str:
    one = " ".join((text or "").split())
    if len(one) <= limit:
        return one
    return one[: limit - 1] + "…"


def parse_llm_json(raw: str) -> tuple[Any | None, str]:
    """Try to extract JSON from an LLM reply. Returns (data, error_message)."""
    if not raw or not str(raw).strip():
        return None, "Пустой ответ модели (возможен сбой API или таймаут)"
    text = raw.strip()
    data = _parse_json(text)
    if data is not None:
        return data, ""
    thinking, answer = parse_reasoning(text)
    if answer:
        data = _parse_json(answer)
        if data is not None:
            return data, ""
    if text.startswith("```"):
        return None, (
            "Ответ обёрнут в markdown (```), нужен чистый JSON без оформления. "
            f"Начало: «{_preview(text)}»"
        )
    if "{" not in text and "[" not in text:
        return None, (
            "В ответе нет JSON ({...}). Верни только один JSON-объект. "
            f"Начало: «{_preview(text)}»"
        )
    if thinking and not answer:
        return None, (
            "Модель выдала только рассуждение без JSON. "
            "Нужен объект {...} с требуемыми полями. "
            f"Начало: «{_preview(text)}»"
        )
    return None, (
        "JSON не распарсился (синтаксическая ошибка, обрезка или лишний текст). "
        f"Начало: «{_preview(text)}»"
    )


def chat_json_with_retry(
    brain: "Brain",
    system: str,
    messages: list[dict],
    *,
    validate: Callable[[Any], str | None] | None = None,
    max_tokens: int = 1400,
    max_retries: int | None = None,
    timeout: float | None = None,
    on_error: Callable[[int, str, str, str], None] | None = None,
    context: str = "json",
    schema: dict | None = None,
) -> tuple[Any | None, str, list[dict]]:
    """Multi-turn retry until JSON parses and passes validate().

    Two distinct failure classes, handled differently:
      * transport-empty (timeout/network/gateway) → backoff and resend the SAME
        prompt; no fake «ОШИБКА ФОРМАТА» turn is appended (the model never spoke);
      * parse/validate failure → echo the (trimmed) bad output + a fix instruction.
    Total wall-clock is capped (LLM_RETRY_WALL_SEC) so a slow endpoint can't eat
    a whole phase deadline across retries.
    """
    if brain.is_mock:
        return None, "", []
    attempts = max_retries
    if attempts is None:
        attempts = int(os.environ.get("LLM_JSON_RETRIES", "3"))
    attempts = max(1, attempts)
    wall_cap = float(os.environ.get("LLM_RETRY_WALL_SEC", "300"))
    started = time.monotonic()
    msgs = list(messages)
    last_raw = ""
    log: list[dict] = []
    # Sampler-enforced JSON when a schema is given (verified real on sverk);
    # legacy opt-in json_object otherwise. Self-heals: if a guided attempt comes
    # back empty (gateway rejected the format), drop it and retry plain.
    rf = None
    if schema is not None and schema_supported(brain):
        rf = json_schema_format(context, schema)
    elif (os.environ.get("LLM_GUIDED_JSON", "0") not in ("0", "", "false", "no")
          and getattr(brain, "provider", None) in ("sverk", "openai")):
        rf = {"type": "json_object"}
    for attempt in range(1, attempts + 1):
        last_raw = brain.chat(
            system, msgs, max_tokens=max_tokens, timeout=timeout, log_context=context,
            response_format=rf,
        ) or ""
        if not last_raw:
            # transport failure — the model never answered; don't blame its format
            err = "Пустой ответ модели (сбой API / таймаут / отклонённый формат)"
            log.append({"attempt": attempt, "error": err, "transport": True})
            try:
                from run_log import log_parse
                log_parse(context, attempt, False, err)
            except Exception:
                pass
            if on_error:
                on_error(attempt, err, "", context)
            if rf is not None:
                rf = None  # guided format may be unsupported → fall back to plain
            if attempt >= attempts or (time.monotonic() - started) > wall_cap:
                break
            time.sleep(min(8.0, 2.0 * attempt))
            continue
        parsed, parse_err = parse_llm_json(last_raw)
        err = parse_err
        if not err and validate:
            err = validate(parsed) or ""
        try:
            from run_log import log_parse
            log_parse(context, attempt, not err, err)
        except Exception:
            pass
        if not err:
            return parsed, last_raw, log
        log.append({"attempt": attempt, "error": err})
        if on_error:
            on_error(attempt, err, last_raw, context)
        if attempt >= attempts or (time.monotonic() - started) > wall_cap:
            break
        fix = (
            f"ОШИБКА ФОРМАТА (попытка {attempt}/{attempts}): {err}\n\n"
            "Исправь ответ: верни ТОЛЬКО один валидный JSON-объект "
            "{ ... } — без markdown, без ```, без нумерованных шагов "
            "и без текста до или после JSON."
        )
        msgs.append({"role": "assistant", "content": last_raw[:RETRY_RAW_MAX_CHARS]})
        msgs.append({"role": "user", "content": fix})
    return None, last_raw, log


_BAD_LINE_RE = re.compile(
    r"^(?:chat\s*line(?:\s*for\s*the\s*board)?|your\s*chat\s*message|"
    r"chat\s*message|line|\.\.\.|n/?a|…)$",
    re.IGNORECASE,
)

_REASONING_LEAK_RE = re.compile(
    r"(?:thinking process|analyze user input|the user wants me to|"
    r"deconstruct constraints|deconstruct requirements|formulate strategy|"
    r"brainstorming scene|mental refinement|identify rivals|"
    r"determine response strategy|draft\s*-\s*mental|private reasoning|"
    r"`thinking`|`\s*line\s*`|role:\s*\w+.*painter drone|"
    r"key constraints|output strategy|json output|here's a thinking process|"
    r"analyze user input.*role:|"
    r"мы на раунде|мои оппоненты|мне нужно вступить|"
    r"constraints on the final|evaluate proposals)",
    re.IGNORECASE,
)


def is_reasoning_leak(text: str) -> bool:
    """True when text is chain-of-thought / meta, not a board message."""
    if not text or len(text.strip()) < 4:
        return True
    t = text.strip()
    low = t.lower()
    if _REASONING_LEAK_RE.search(t):
        return True
    if low.startswith("here's a thinking"):
        return True
    if re.match(r"^\d+\.\s+(\*\*)?", t):
        return True
    if t.count("**") >= 3:
        return True
    if re.match(r"^\d+\.\s+\*\*", t):
        return True
    if low.startswith("the user wants"):
        return True
    if re.search(r"^\s*[-*]\s+\*\*", t, re.M):
        return True
    return False


def extract_board_line(text: str) -> str:
    """Pull a spoken chat line out of prose when JSON parsing failed."""
    if not text:
        return ""
    for pat in (
        r"(?:^|\n)((?:Предлагаю|Голосую|Я предлагаю|I'm pitching|I propose|Vote:)\s*[^\n]{10,260})",
        r"(?:^|\n)((?:painter-\d+|[А-ЯЁA-Z][\wа-яё]+),[^\n]{15,260})",  # «Имя, …» — любое имя персоны
        r"\*\*(?:Line|Сообщение|Строка):\*\*\s*(.+?)(?:\n\*\*|$)",
        r"(?:^|\n)(?:line|сообщение|строка):\s*(.+?)(?:\n|$)",
    ):
        m = re.search(pat, text, re.IGNORECASE | re.DOTALL)
        if m:
            cand = m.group(1).strip().strip('"').strip("'")
            if not is_reasoning_leak(cand):
                return cand[:280]
    data = _parse_json(text)
    if isinstance(data, dict):
        cand = str(data.get("line") or "").strip()
        if cand and not is_reasoning_leak(cand):
            return cand[:280]
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    for p in reversed(paras):
        if 15 <= len(p) <= 280 and not is_reasoning_leak(p):
            if re.search(r"[.!?]$|pitching|holding out|Vote:|Предлагаю|Голосую|держусь", p, re.I):
                return p[:280]
    return ""


def extract_quoted_subject(text: str) -> str:
    """Scene title from a pitch line or quoted string."""
    if not text:
        return ""
    for pat in (
        r"предлагаю\s+настроение\s+[«\"']([^«»\"']{3,80})[»\"']",
        r"предлагаю\s+[«\"']([^«»\"']{3,80})[»\"']",
        r"pitching\s+['\"]([^'\"]{3,80})['\"]",
        r"propose[sd]?\s+['\"]([^'\"]{3,80})['\"]",
        r"держусь за\s+[«\"']([^«»\"']{3,80})[»\"']",
        r"holding out for\s+['\"]([^'\"]{3,80})['\"]",
        r"голосую за\s+[«\"']([^«»\"']{3,80})[»\"']",
        r"Vote:\s*['\"]([^'\"]{3,80})['\"]",
        r"настроение\s+[«\"']([^«»\"']{3,80})[»\"']",
    ):
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            return m.group(1).strip()
    return ""


def parse_agent_turn(raw: str, fallback: dict | None = None) -> dict:
    """Normalize one LLM response into {thinking, line, subject?, ...}."""
    out = dict(fallback or {})
    if not raw:
        return out

    thinking_parts: list[str] = []
    data = _parse_json(raw)

    if isinstance(data, dict):
        for k in ("thinking", "reasoning"):
            if data.get(k):
                thinking_parts.append(str(data[k]).strip())
        line = str(data.get("line") or "").strip()
        if line and not is_reasoning_leak(line):
            out["line"] = line
        elif line:
            thinking_parts.append(line)
        for k in ("subject", "endorse", "reply_to", "scores"):
            if data.get(k) is not None:
                val = data[k]
                if k in ("subject", "endorse") and isinstance(val, str):
                    if is_reasoning_leak(val):
                        continue
                out[k] = val
    else:
        thinking, answer = parse_reasoning(raw)
        if thinking:
            thinking_parts.append(thinking)
        if answer and not is_reasoning_leak(answer):
            out["line"] = answer
        elif answer:
            thinking_parts.append(answer)

    if not out.get("line") or is_reasoning_leak(str(out.get("line", ""))):
        extracted = extract_board_line(raw)
        if extracted:
            out["line"] = extracted

    if thinking_parts:
        out["thinking"] = "\n\n".join(thinking_parts)[:2400]
    elif is_reasoning_leak(raw) and not out.get("thinking"):
        out["thinking"] = raw[:2400]

    subj = str(out.get("subject") or "").strip()
    if not subj or is_reasoning_leak(subj):
        subj = extract_quoted_subject(str(out.get("line", "")))
        if subj:
            out["subject"] = subj
        elif "subject" in out:
            del out["subject"]

    return out


def parse_reasoning(text: str) -> tuple[str, str]:
    """Split chain-of-thought from the final answer / chat line."""
    if not text:
        return "", ""
    t = text.strip()

    # qwen35 markdown: **Thinking:** ... **Line:** ... (и русские варианты)
    if re.search(r"\*\*(?:Thinking|Мышление|Рассуждение):\*\*", t, re.I) or re.search(
        r"\*\*(?:Line|Сообщение|Строка):\*\*", t, re.I
    ):
        m_th = re.search(
            r"\*\*(?:Thinking|Мышление|Рассуждение):\*\*\s*(.*?)(?=\*\*(?:Line|Сообщение|Строка):\*\*|$)",
            t, re.IGNORECASE | re.DOTALL,
        )
        m_ln = re.search(
            r"\*\*(?:Line|Сообщение|Строка):\*\*\s*(.+)", t, re.IGNORECASE | re.DOTALL
        )
        thinking = (m_th.group(1).strip() if m_th else "")
        answer = (m_ln.group(1).strip() if m_ln else "")
        if answer and not is_reasoning_leak(answer):
            return thinking, answer
        if thinking:
            return thinking, ""
        return (t if is_reasoning_leak(t) else ""), ""

    think_open, think_close = "<" + "think" + ">", "</" + "think" + ">"
    if think_open in t.lower():
        try:
            i = t.lower().index(think_open) + len(think_open)
            j = t.lower().index(think_close, i)
            thinking = t[i:j].strip()
            answer = t[j + len(think_close):].strip()
            return thinking, answer or thinking
        except ValueError:
            pass
    if "<thinking>" in t.lower():
        try:
            i = t.lower().index("<thinking>") + len("<thinking>")
            j = t.lower().index("</thinking>", i)
            thinking = t[i:j].strip()
            answer = t[j + len("</thinking>"):].strip()
            return thinking, answer or thinking
        except ValueError:
            pass

    # JSON payload embedded after prose
    data = _parse_json(t)
    if isinstance(data, dict):
        thinking = str(data.get("thinking") or data.get("reasoning") or "").strip()
        answer = str(
            data.get("line")
            or data.get("answer")
            or data.get("message")
            or data.get("decision")
            or ""
        ).strip()
        if answer or thinking:
            if is_reasoning_leak(answer):
                return (thinking or answer).strip(), ""
            if answer:
                return thinking, answer
            return thinking, ""

    # "Here's a thinking process" style (qwen35)
    if re.search(r"thinking process", t, re.IGNORECASE) or is_reasoning_leak(t):
        markers = (
            r"\*\*(?:Decision|Answer|Conclusion|Final|Vote|Proposal|Response|Line|"
            r"Решение|Ответ|Вывод|Голос|Предложение|Отклик|Сообщение|Строка)\*\*[:\s]*",
            r"(?:^|\n)(?:Decision|Answer|Conclusion|Final vote|I propose|My vote|Posted line|"
            r"I'm pitching|Предлагаю|Голосую|Мой голос|Сообщение)[:\s]+",
        )
        for pat in markers:
            m2 = re.search(pat, t, re.IGNORECASE | re.MULTILINE)
            if m2:
                thinking = t[: m2.start()].strip()
                answer = t[m2.start() :].strip()
                if is_reasoning_leak(answer):
                    return thinking or t, ""
                return thinking, answer
        extracted = extract_board_line(t)
        if extracted:
            return t, extracted
        return t, ""

    if is_reasoning_leak(t):
        extracted = extract_board_line(t)
        return t if not extracted else t, extracted

    return "", t


def clean_chat_line(raw, fallback: str = "", *, thinking: str = "") -> str:
    """Board-ready chat line: strip reasoning leaks and reject placeholder text."""
    t = str(raw or "").strip()
    if not t:
        t = ""

    if "**Thinking:**" in t or "**Line:**" in t or re.search(
        r"\*\*(?:Мышление|Рассуждение|Сообщение|Строка):\*\*", t, re.I
    ):
        _, parsed = parse_reasoning(t)
        if parsed and parsed != t:
            t = parsed

    m = re.search(
        r"(?:^|\n)(?:\*\*)?(?:Line|Сообщение|Строка)(?:\*\*)?:\s*(.+)",
        t, re.IGNORECASE | re.DOTALL,
    )
    if m:
        t = m.group(1).strip()

    embedded = _parse_json(t)
    if isinstance(embedded, dict) and embedded.get("line"):
        t = str(embedded["line"]).strip()

    _, answer = parse_reasoning(t)
    if answer and answer != t:
        t = answer

    low = t.lower().strip()
    if not t or len(t) < 4 or _BAD_LINE_RE.match(low):
        return fallback
    if low.startswith("step-by-step reasoning"):
        return fallback
    if "reason step by step" in low and len(t) < 100:
        return fallback
    if thinking and t.strip() == thinking.strip():
        return fallback
    if "**Thinking:**" in t:
        return fallback
    if is_reasoning_leak(t):
        return fallback
    return t[:280]


class Brain:
    def __init__(self):
        self.provider = os.environ.get("MODEL_PROVIDER", "mock").lower()
        _defaults = {"sverk": "qwen35", "anthropic": "claude-opus-4-8",
                     "openai": "gpt-4o-mini", "ollama": "qwen2.5:3b"}
        self.model = os.environ.get("MODEL") or _defaults.get(self.provider, "qwen35")
        self.timeout = float(os.environ.get("MODEL_TIMEOUT", "45"))
        self.max_tokens = int(os.environ.get("MODEL_MAX_TOKENS", "256"))

    @property
    def is_mock(self) -> bool:
        return self.provider == "mock"

    def think(self, system: str, user: str, fallback: str = "",
              *, max_tokens: int | None = None) -> str:
        if self.provider == "mock":
            return fallback
        system = _ru(system)
        try:
            if self.provider == "anthropic":
                return self._anthropic(system, user, max_tokens=max_tokens)
            if self.provider == "openai":
                return self._openai(system, user, max_tokens=max_tokens)
            if self.provider == "sverk":
                return self._sverk(system, user, max_tokens=max_tokens)
            if self.provider == "ollama":
                return self._ollama(system, user, max_tokens=max_tokens)
        except Exception as exc:  # noqa: BLE001
            return f"{fallback} (llm error: {type(exc).__name__})"
        return fallback

    def think_json(self, system: str, user: str, *, max_tokens: int = 400):
        if self.provider == "mock":
            return None
        text = self.think(system, user, max_tokens=max_tokens)
        if "(llm error:" in text:
            return None
        data = _parse_json(text)
        if data is not None:
            return data
        _, answer = parse_reasoning(text)
        return _parse_json(answer)

    def chat(self, system: str, messages: list[dict], *, max_tokens: int | None = None,
             timeout: float | None = None, log_context: str = "chat",
             response_format: dict | None = None) -> str:
        """Multi-turn chat. messages = [{role, content}, ...] after system.

        response_format (e.g. {"type":"json_object"}) is a vLLM/OpenAI guided-
        decoding hint; only the OpenAI-compatible path (sverk/openai) forwards it,
        others ignore it. On this Qwen deployment it is accepted but marginal (the
        no-think params already do the heavy lifting), so it is opt-in."""
        if self.provider == "mock":
            return ""
        system = _ru(system)
        t0 = time.monotonic()
        err = ""
        result = ""
        try:
            if self.provider in ("sverk", "openai"):
                base = (os.environ.get("SVERK_API_BASE", "https://ai.sverk.tech/v1")
                        if self.provider == "sverk"
                        else os.environ.get("OPENAI_API_BASE", "https://api.openai.com/v1"))
                key = (os.environ.get("SVERK_API_KEY") or os.environ.get("OPENAI_API_KEY", "")
                       if self.provider == "sverk" else os.environ.get("OPENAI_API_KEY", ""))
                result = self._chat_completions_msgs(
                    base, key, system, messages, max_tokens=max_tokens, timeout=timeout,
                    response_format=response_format,
                )
            elif self.provider == "anthropic":
                user = "\n\n".join(
                    f"{m['role'].upper()}: {m['content']}" for m in messages if m.get("content")
                )
                result = self._anthropic(system, user, max_tokens=max_tokens, timeout=timeout)
            elif self.provider == "ollama":
                result = self._ollama_msgs(system, messages, max_tokens=max_tokens, timeout=timeout)
        except Exception as exc:  # noqa: BLE001
            err = type(exc).__name__
            result = ""
        finally:
            try:
                from run_log import log_llm
                log_llm(
                    context=log_context,
                    system=system,
                    messages=messages,
                    response=result or "",
                    error=err,
                    max_tokens=max_tokens,
                    duration_ms=(time.monotonic() - t0) * 1000,
                )
            except Exception:
                pass
        return result

    def agent_json(self, system: str, messages: list[dict], *,
                   max_tokens: int = 500) -> dict | None:
        """Context-aware turn -> parsed JSON dict or None."""
        if self.provider == "mock":
            return None
        text = self.chat(system, messages, max_tokens=max_tokens)
        if not text:
            return None
        data = _parse_json(text)
        if isinstance(data, dict):
            if not data.get("thinking"):
                th, _ = parse_reasoning(text)
                if th:
                    data["thinking"] = th
            return data
        thinking, answer = parse_reasoning(text)
        data = _parse_json(answer)
        if isinstance(data, dict):
            if thinking and not data.get("thinking"):
                data["thinking"] = thinking
            return data
        line = clean_chat_line(answer or text, thinking=thinking)
        return {"thinking": thinking, "line": line or answer or text}

    # ---- vision (VLM): картинка + вопрос -> текст --------------------------
    def vision_available(self) -> bool:
        """Есть ли настоящий VLM-канал. Провайдер зрения задаётся VISION_PROVIDER
        (по умолчанию — основной провайдер); модель — MODEL_VISION. Для
        anthropic/openai зрение есть у дефолтных моделей; для sverk/ollama VLM
        нужно указать явно (гейтвей qwen35 — text-only, аудит 2026-07)."""
        vp = os.environ.get("VISION_PROVIDER", self.provider).lower()
        if vp in ("anthropic",) and os.environ.get("ANTHROPIC_API_KEY"):
            return True
        if vp == "openai" and os.environ.get("OPENAI_API_KEY"):
            return True
        if vp in ("sverk", "ollama") and os.environ.get("MODEL_VISION"):
            return True
        return False

    def see(self, system: str, user: str, image_png: bytes, *,
            max_tokens: int = 300, log_context: str = "vision") -> str:
        """Один VLM-вызов: PNG + инструкция -> ответ модели ('' при ошибке —
        вызывающий падает на эвристику)."""
        import base64
        system = _ru(system)
        vp = os.environ.get("VISION_PROVIDER", self.provider).lower()
        model = os.environ.get("MODEL_VISION") or self.model
        b64 = base64.b64encode(image_png).decode()
        t0 = time.monotonic()
        err, result = "", ""
        try:
            if vp == "anthropic":
                key = os.environ["ANTHROPIC_API_KEY"]
                data = self._post(
                    "https://api.anthropic.com/v1/messages",
                    {"content-type": "application/json", "x-api-key": key,
                     "anthropic-version": "2023-06-01"},
                    {"model": os.environ.get("MODEL_VISION") or "claude-haiku-4-5-20251001",
                     "max_tokens": max_tokens, "system": system,
                     "messages": [{"role": "user", "content": [
                         {"type": "image", "source": {"type": "base64",
                          "media_type": "image/png", "data": b64}},
                         {"type": "text", "text": user}]}]})
                result = data["content"][0]["text"].strip()
            elif vp in ("openai", "sverk"):
                base = (os.environ.get("SVERK_API_BASE", "https://ai.sverk.tech/v1")
                        if vp == "sverk"
                        else os.environ.get("OPENAI_API_BASE", "https://api.openai.com/v1"))
                key = (os.environ.get("SVERK_API_KEY") or os.environ.get("OPENAI_API_KEY", "")
                       if vp == "sverk" else os.environ.get("OPENAI_API_KEY", ""))
                headers = {"content-type": "application/json"}
                if key:
                    headers["authorization"] = f"Bearer {key}"
                payload = {"model": model, "max_tokens": max_tokens, "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": [
                        {"type": "text", "text": user},
                        {"type": "image_url",
                         "image_url": {"url": f"data:image/png;base64,{b64}"}}]}]}
                if "api.openai.com" not in base:
                    payload.update(_no_think_params())
                data = self._post(f"{base.rstrip('/')}/chat/completions",
                                  headers, payload)
                msg = data["choices"][0]["message"]
                result = (msg.get("content") or "").strip()
            elif vp == "ollama":
                base = os.environ.get("OLLAMA_BASE", "http://localhost:11434")
                data = self._post(
                    f"{base}/api/chat", {"content-type": "application/json"},
                    {"model": model, "stream": False,
                     "options": {"num_predict": max_tokens},
                     "messages": [{"role": "system", "content": system},
                                  {"role": "user", "content": user,
                                   "images": [b64]}]})
                result = data["message"]["content"].strip()
        except Exception as exc:  # noqa: BLE001 — критик переживает любой сбой
            err = type(exc).__name__
            result = ""
        finally:
            try:
                from run_log import log_llm
                log_llm(context=log_context, system=system,
                        messages=[{"role": "user", "content": f"[image {len(image_png)}B] {user}"}],
                        response=result or "", error=err, max_tokens=max_tokens,
                        duration_ms=(time.monotonic() - t0) * 1000)
            except Exception:  # noqa: BLE001
                pass
        return result

    def think_stream(self, system: str, user: str):
        try:
            if self.provider in ("sverk", "openai"):
                if self.provider == "sverk":
                    base = os.environ.get("SVERK_API_BASE", "https://ai.sverk.tech/v1")
                    key = os.environ.get("SVERK_API_KEY") or os.environ.get("OPENAI_API_KEY", "")
                else:
                    base = os.environ.get("OPENAI_API_BASE", "https://api.openai.com/v1")
                    key = os.environ.get("OPENAI_API_KEY", "")
                yield from self._stream_chat(base, key, system, user)
                return
        except Exception as exc:  # noqa: BLE001
            yield f" (llm error: {type(exc).__name__})"
            return
        yield self.think(system, user, "")

    def _stream_chat(self, base: str, key: str, system: str, user: str):
        headers = {"content-type": "application/json"}
        if key:
            headers["authorization"] = f"Bearer {key}"
        payload = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "stream": True,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        req = urllib.request.Request(
            f"{base.rstrip('/')}/chat/completions",
            data=json.dumps(payload).encode(), headers=headers, method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            for raw in resp:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    delta = json.loads(data)["choices"][0]["delta"]
                except (json.JSONDecodeError, KeyError, IndexError):
                    continue
                chunk = delta.get("reasoning_content") or delta.get("content")
                if chunk:
                    yield chunk

    def _post(self, url: str, headers: dict, payload: dict, *, timeout: float | None = None) -> dict:
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode(), headers=headers, method="POST"
        )
        with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
            return json.loads(resp.read().decode())

    def _chat_completions_msgs(self, base: str, key: str, system: str,
                               messages: list[dict], *, max_tokens: int | None = None,
                               timeout: float | None = None,
                               response_format: dict | None = None) -> str:
        headers = {"content-type": "application/json"}
        if key:
            headers["authorization"] = f"Bearer {key}"
        msgs = [{"role": "system", "content": system}]
        msgs.extend({"role": m["role"], "content": m["content"]}
                    for m in messages if m.get("content"))
        payload = {"model": self.model, "max_tokens": max_tokens or self.max_tokens,
                   "messages": msgs}
        if response_format:
            payload["response_format"] = response_format
        # enable_thinking/chat_template_kwargs are vLLM-only — api.openai.com
        # rejects unknown params with a 400 (which chat() swallowed to ""), so
        # the openai fallback provider was silently broken
        if "api.openai.com" not in base:
            payload.update(_no_think_params())
        data = self._post(
            f"{base.rstrip('/')}/chat/completions", headers, payload, timeout=timeout,
        )
        msg = data["choices"][0]["message"]
        return (msg.get("content") or msg.get("reasoning_content") or "").strip()

    def _anthropic(self, system: str, user: str, *, max_tokens: int | None = None,
                   timeout: float | None = None) -> str:
        key = os.environ["ANTHROPIC_API_KEY"]
        data = self._post(
            "https://api.anthropic.com/v1/messages",
            {"content-type": "application/json", "x-api-key": key,
             "anthropic-version": "2023-06-01"},
            {"model": self.model, "max_tokens": max_tokens or self.max_tokens,
             "system": system, "messages": [{"role": "user", "content": user}]},
            timeout=timeout,
        )
        return data["content"][0]["text"].strip()

    def _openai(self, system: str, user: str, *, max_tokens: int | None = None) -> str:
        return self._chat_completions_msgs(
            os.environ.get("OPENAI_API_BASE", "https://api.openai.com/v1"),
            os.environ["OPENAI_API_KEY"], system,
            [{"role": "user", "content": user}], max_tokens=max_tokens)

    def _sverk(self, system: str, user: str, *, max_tokens: int | None = None) -> str:
        base = os.environ.get("SVERK_API_BASE", "https://ai.sverk.tech/v1")
        key = os.environ.get("SVERK_API_KEY") or os.environ.get("OPENAI_API_KEY", "")
        return self._chat_completions_msgs(
            base, key, system, [{"role": "user", "content": user}], max_tokens=max_tokens)

    def _ollama(self, system: str, user: str, *, max_tokens: int | None = None) -> str:
        return self._ollama_msgs(system, [{"role": "user", "content": user}], max_tokens=max_tokens)

    def _ollama_msgs(self, system: str, messages: list[dict], *, max_tokens: int | None = None,
                     timeout: float | None = None) -> str:
        base = os.environ.get("OLLAMA_BASE", "http://localhost:11434")
        msgs = [{"role": "system", "content": system}]
        msgs.extend({"role": m["role"], "content": m["content"]} for m in messages)
        data = self._post(
            f"{base}/api/chat",
            {"content-type": "application/json"},
            {"model": self.model, "stream": False,
             "options": {"num_predict": max_tokens or self.max_tokens}, "messages": msgs},
            timeout=timeout,
        )
        return data["message"]["content"].strip()
