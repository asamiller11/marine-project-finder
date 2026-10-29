"""Optional LLM step: a short English summary and a confidence label.

Safety rules built in:
- The rule score stays the primary signal. This step never changes the score.
- The model must quote the notice word for word. We check every quote against the
  original text and drop any that do not appear. If no quote survives, we drop the
  summary too, so nothing unsupported is shown.
- Quotes stay in the original language. The model says when it translated.
- The API key comes from the ANTHROPIC_API_KEY environment variable, never from files.
"""
from __future__ import annotations

import json
import os
import re

SYSTEM = """You summarize public environmental and permit notices for marine construction projects.
Rules:
- Use only facts stated in the notice text. Never add facts, numbers, dates or names that are not in it.
- Write a plain-English summary of 2 to 3 sentences. If the notice is not in English, translate for
  the summary and set "translated" to true.
- Give 1 to 4 short evidence quotes copied EXACTLY, character for character, from the notice in its
  original language. Every claim in the summary must be supported by one of these quotes.
- "confidence" is how clearly the notice shows the project may need marine habitat work
  (fish habitat offsetting, compensation, habitat enhancement, nature-inclusive design):
  "high", "medium" or "low". Low is normal and fine. Do not overstate.
- Never say mitigation or compensation "will be required" unless the notice says so.
Reply with only a JSON object with keys: summary_en, confidence, evidence_quotes, translated."""


def _norm(s: str) -> str:
    s = s.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    s = s.replace("–", "-").replace("—", "-")
    return re.sub(r"\s+", " ", s).strip().lower()


def verify_quotes(quotes: list, source_text: str) -> tuple[list[str], int]:
    """Keep only quotes that appear in the source text. Returns (kept, dropped_count)."""
    haystack = _norm(source_text)
    kept = []
    for q in quotes or []:
        if isinstance(q, str) and len(q.strip()) >= 8 and _norm(q) in haystack:
            kept.append(q.strip())
    return kept, len(quotes or []) - len(kept)


def parse_reply(text: str) -> dict:
    """Read the JSON object from the model reply (tolerates code fences)."""
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        raise ValueError("no JSON object in reply")
    return json.loads(m.group(0))


def check_result(data: dict, source_text: str) -> dict:
    kept, dropped = verify_quotes(data.get("evidence_quotes", []), source_text)
    confidence = str(data.get("confidence", "")).lower()
    if confidence not in ("high", "medium", "low"):
        confidence = "unknown"
    summary = str(data.get("summary_en", "")).strip()
    if not kept:
        # Nothing checkable: do not show an unsupported summary.
        return {"summary_en": None, "confidence": "unverified", "quotes": [], "dropped": dropped,
                "translated": bool(data.get("translated"))}
    return {"summary_en": summary, "confidence": confidence, "quotes": kept, "dropped": dropped,
            "translated": bool(data.get("translated"))}


def summarize(text: str, model: str, client=None) -> dict:
    """Call the Claude API for one notice. Raises if the SDK or key is missing."""
    if client is None:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise RuntimeError("Set ANTHROPIC_API_KEY to use the LLM step.")
        import anthropic          # optional dependency: pip install anthropic
        client = anthropic.Anthropic()
    # Server-side fallback: if the main model declines a request, the API retries it on
    # another model automatically. Notices are ordinary public text, so this is rarely used.
    response = client.beta.messages.create(
        model=model,
        max_tokens=2000,
        system=SYSTEM,
        messages=[{"role": "user", "content": f"<notice>\n{text[:60000]}\n</notice>"}],
        betas=["server-side-fallback-2026-07-01"],
        extra_body={"fallbacks": "default", "output_config": {"effort": "low"}},
    )
    if response.stop_reason == "refusal":
        raise RuntimeError("The model declined this notice.")
    reply = "".join(b.text for b in response.content if getattr(b, "type", "") == "text")
    result = check_result(parse_reply(reply), text)
    result["model"] = getattr(response, "model", model)
    return result
