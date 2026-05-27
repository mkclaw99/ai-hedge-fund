"""Research-area materials: turn an uploaded PDF into injectable grounding.

WikiLLM approach (no RAG): extract the PDF text, run one Gemini "librarian" pass
to distill it into a compact investment brief, and store both in the flow's wiki —
`brief.md` (injected into the analysts) and `source.txt` (full text, kept for audit
/ future retrieval, not injected). Fail-open: if distillation fails we fall back to
a truncated excerpt so the upload still yields usable grounding.
"""

import io
import logging
import os
from pathlib import Path

from src.memory import flow_root

logger = logging.getLogger(__name__)

# Cap how much source text we feed the distiller (cost/safety) and how long the
# resulting brief can be (it's injected into every agent prompt).
_MAX_SOURCE_CHARS = 40_000
_FALLBACK_BRIEF_CHARS = 2_000
_DISTILL_MODEL = "gemini-3.1-pro-preview"
_DISTILL_PROVIDER = "Google"

_DISTILL_PROMPT = (
    "You are a research librarian for an investment team. Distill the source document "
    "below into a concise investment brief (~400-600 words max) that analysts will read "
    "as background when researching this area. Use only these sections, omitting any with "
    "no material:\n"
    "## Thesis\n## Key companies / value chain (include tickers if stated)\n"
    "## Catalysts\n## Risks\n## Notable facts & figures\n\n"
    "Be specific and factual. Do NOT invent companies, tickers, or numbers that aren't in "
    "the source. Source document:\n---\n{text}\n---"
)


def _materials_dir(flow_id: int) -> Path | None:
    root = flow_root(f"flow-{flow_id}")
    if not root:
        return None
    d = Path(root) / "materials"
    d.mkdir(parents=True, exist_ok=True)
    return d


def extract_pdf_text(data: bytes) -> str:
    """Extract text from a PDF's bytes. Raises ValueError on an unreadable file."""
    from pypdf import PdfReader

    try:
        reader = PdfReader(io.BytesIO(data))
        parts = [(page.extract_text() or "") for page in reader.pages]
    except Exception as e:
        raise ValueError(f"Could not read PDF: {e}")
    text = "\n\n".join(p.strip() for p in parts if p.strip())
    if not text.strip():
        raise ValueError("No extractable text in PDF (is it a scan/image-only file?)")
    return text


def distill_brief(text: str, *, api_keys: dict | None = None) -> str:
    """Distill source text into a compact investment brief via Gemini (fail-open)."""
    excerpt = text[:_MAX_SOURCE_CHARS]
    try:
        from src.llm.models import get_model

        model = get_model(_DISTILL_MODEL, _DISTILL_PROVIDER, api_keys)
        if model is None:
            raise RuntimeError("distill model unavailable")
        result = model.invoke(_DISTILL_PROMPT.format(text=excerpt))
        brief = getattr(result, "content", None) or str(result)
        brief = brief.strip()
        if brief:
            return brief
    except Exception as e:  # fall back to a plain excerpt so upload still works
        logger.warning("Materials distillation failed, using excerpt: %s", e)
    fallback = excerpt[:_FALLBACK_BRIEF_CHARS].strip()
    suffix = "\n\n[…excerpt truncated; distillation unavailable]" if len(text) > _FALLBACK_BRIEF_CHARS else ""
    return f"## Source excerpt\n\n{fallback}{suffix}"


def store_materials(flow_id: int, *, source_text: str, brief: str, filename: str) -> None:
    """Persist the full source text and the distilled brief in the flow's wiki."""
    d = _materials_dir(flow_id)
    if d is None:
        return
    header = f"<!-- source: {filename} -->\n\n"
    (d / "source.txt").write_text(source_text, encoding="utf-8")
    (d / "brief.md").write_text(header + brief, encoding="utf-8")
    (d / "source_name.txt").write_text(filename, encoding="utf-8")


def load_brief(flow_id: int | None) -> str:
    """Return the distilled brief for a flow (or '' if none / unavailable)."""
    if flow_id is None:
        return ""
    try:
        d = _materials_dir(flow_id)
        if d is None:
            return ""
        f = d / "brief.md"
        return f.read_text(encoding="utf-8") if f.exists() else ""
    except Exception:
        return ""


def materials_status(flow_id: int | None) -> dict:
    """Lightweight status for the UI: whether a brief exists + the source filename."""
    if flow_id is None:
        return {"has_brief": False}
    d = _materials_dir(flow_id)
    if d is None:
        return {"has_brief": False}
    brief = d / "brief.md"
    name = d / "source_name.txt"
    return {
        "has_brief": brief.exists(),
        "filename": name.read_text(encoding="utf-8").strip() if name.exists() else None,
        "brief": brief.read_text(encoding="utf-8") if brief.exists() else None,
    }
