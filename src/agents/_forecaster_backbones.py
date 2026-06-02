"""Backbone-agnostic forecaster pipeline.

The Time Series Forecaster analyst can run on either:

* **Chronos-2** (Amazon, 120M params) — the original v1 backbone. Available
  by default; weights download lazily from HuggingFace into the standard cache.
* **Toto-2.0** (Datadog, 313M params) — optional second backbone. Installed
  via ``scripts/install_toto.sh`` because the package isn't on PyPI and pulls
  in heavy deps. Apache 2.0 license, observability-metrics pretraining.

Both backbones produce the same output shape — ``BackboneResult`` — so the
agent and the chart code don't care which model ran. Add a new backbone by
implementing one function that returns this dataclass.

Output contract:

    BackboneResult(
        q10, q25, q50, q75, q90,  # per-step forecast quantiles, len = prediction_len
        confidence,                # per-step confidence (0-100), derived from fan width
        model_name,                # human-readable backbone label for the trace
    )

Failure semantics: every entry point returns ``None`` on any failure (model
not available, weights missing, predict raised). The agent treats ``None``
as "skip this ticker" rather than propagating the error — keeps the
pipeline fail-open per the persona's "we don't override the model" rule.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from threading import Lock
from typing import Sequence

import numpy as np

logger = logging.getLogger(__name__)


# --- shared shape -----------------------------------------------------------

@dataclass(frozen=True)
class BackboneResult:
    """Per-ticker forecast in the shape the agent + chart expect.

    All trajectories are length ``prediction_len`` (the requested horizon).
    Quantile levels are fixed at q10/q25/q50/q75/q90 regardless of backbone —
    if the underlying model offers more, we project to these five; if it
    offers fewer, we interpolate (degenerate models that can't supply five
    levels return ``None`` instead of a BackboneResult)."""
    q10: list[float]
    q25: list[float]
    q50: list[float]
    q75: list[float]
    q90: list[float]
    confidence: list[int]   # 0-100 per step, fan-width derived
    model_name: str         # human-readable, e.g. "Amazon Chronos-2"


# --- Chronos-2 backbone -----------------------------------------------------

# Singleton load — Chronos's pipeline.from_pretrained downloads ~480 MB on
# first call and warms a process-wide cache. Lock guards the lazy init.
_chronos_pipeline = None
_chronos_lock = Lock()

# Levels we always emit. Chronos can be asked for arbitrary levels; we pick
# these five so the trace and the inline chart share a single contract.
_QUANTILES = [0.1, 0.25, 0.5, 0.75, 0.9]


def _load_chronos():
    """Lazy, process-wide singleton load of Chronos-2.

    Local import so a missing chronos / torch install doesn't break the
    rest of the app at module load. Returns None on any failure; callers
    treat that as "Chronos isn't available, skip the ticker"."""
    global _chronos_pipeline
    if _chronos_pipeline is not None:
        return _chronos_pipeline
    with _chronos_lock:
        if _chronos_pipeline is not None:
            return _chronos_pipeline
        try:
            from chronos import Chronos2Pipeline
        except Exception as e:
            logger.warning("chronos-forecasting not importable: %s", e)
            return None
        try:
            # device_map="auto" picks CUDA → MPS → CPU. Apple Silicon
            # gets MPS for free; CPU-only still works (slower).
            _chronos_pipeline = Chronos2Pipeline.from_pretrained(
                "amazon/chronos-2", device_map="auto",
            )
        except Exception as e:
            logger.warning("Chronos-2 load failed: %s", e)
            return None
    return _chronos_pipeline


def chronos_forecast(closes: np.ndarray, *, prediction_len: int) -> BackboneResult | None:
    """Run Chronos-2 on a single ticker's close series. Returns None on
    any failure (load error, predict error, unparseable output)."""
    pipeline = _load_chronos()
    if pipeline is None:
        return None
    try:
        qt, _means = pipeline.predict_quantiles(
            [closes], prediction_length=prediction_len, quantile_levels=_QUANTILES,
        )
    except Exception as e:
        logger.warning("Chronos-2 predict failed: %s", e)
        return None
    try:
        # qt shape: (1, prediction_length, num_quantiles); the leading 1
        # is Chronos's per-series batch axis.
        arr = qt[0].detach().cpu().numpy() if hasattr(qt[0], "detach") else np.asarray(qt[0])
        if arr.ndim == 3:
            arr = np.squeeze(arr, axis=0)
        q10 = arr[:, 0].astype(float).tolist()
        q25 = arr[:, 1].astype(float).tolist()
        q50 = arr[:, 2].astype(float).tolist()
        q75 = arr[:, 3].astype(float).tolist()
        q90 = arr[:, 4].astype(float).tolist()
    except Exception as e:
        logger.warning("Chronos-2 unparseable output: %s", e)
        return None
    last = float(closes[-1])
    return BackboneResult(
        q10=q10, q25=q25, q50=q50, q75=q75, q90=q90,
        confidence=_per_step_confidence(last, q10, q90),
        model_name="Amazon Chronos-2",
    )


# --- Toto-2.0 backbone ------------------------------------------------------

# Singleton load + lock, same pattern as Chronos. Weights are ~1.2 GB the
# first time.
_toto_model = None
_toto_lock = Lock()
_toto_device = None  # cached torch.device after the first load


def _load_toto():
    """Lazy, process-wide singleton load of Toto-2.0-313m.

    Tries CUDA first, then CPU. **MPS is explicitly NOT tried** — Toto's
    forecast call uses a 4D sort that Apple's MetalPerformanceShaders
    rejects (``MPSNDArraySort.mm:252`` assertion: "Axis = 4. This class
    only supports axis = 0, 1, 2, 3"). Reproduced 2026-06 on M-series.
    Going CPU-only on Apple Silicon is fine: ~90ms per forecast at 256
    context → 10-step horizon.

    Returns None on any failure (toto2 not installed, weights missing,
    load error)."""
    global _toto_model, _toto_device
    if _toto_model is not None:
        return _toto_model, _toto_device
    with _toto_lock:
        if _toto_model is not None:
            return _toto_model, _toto_device
        try:
            import torch
            from toto2 import Toto2Model
        except Exception as e:
            logger.warning(
                "toto2 not importable — install via scripts/install_toto.sh "
                "(error: %s)", e,
            )
            return None, None
        try:
            model = Toto2Model.from_pretrained("Datadog/Toto-2.0-313m")
            # CUDA → CPU. No MPS attempt — see docstring.
            device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
            model = model.to(device).eval()
            _toto_model = model
            _toto_device = device
            logger.info("Toto-2.0-313m loaded on %s", device)
        except Exception as e:
            logger.warning("Toto-2.0 load failed: %s", e)
            return None, None
    return _toto_model, _toto_device


# Toto's forecast call returns quantiles at fixed levels — 9 evenly-spaced
# from 0.1 to 0.9, step 0.1. Indices we care about for the BackboneResult.
_TOTO_Q10_IDX = 0  # 0.1
_TOTO_Q25_IDX = 1  # 0.2  — closest to 0.25; see _interpolate_q25_q75 below
_TOTO_Q50_IDX = 4  # 0.5
_TOTO_Q75_IDX = 7  # 0.8  — closest to 0.75
_TOTO_Q90_IDX = 8  # 0.9


def _interpolate_q25_q75(arr: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Toto emits q20 + q30 (not q25) and q70 + q80 (not q75). Linear-
    interpolate to the standard q25/q75 levels so the result matches
    Chronos's contract. Half-way between q20 (idx 1) and q30 (idx 2);
    same for q70/q80."""
    q25 = 0.5 * (arr[1, ...] + arr[2, ...])
    q75 = 0.5 * (arr[6, ...] + arr[7, ...])
    return q25, q75


def toto_forecast(closes: np.ndarray, *, prediction_len: int) -> BackboneResult | None:
    """Run Toto-2.0 on a single ticker's close series. Returns None when
    Toto isn't installed, weights missing, or the forecast call raised."""
    pair = _load_toto()
    if pair is None or pair[0] is None:
        return None
    model, device = pair
    try:
        import torch
    except Exception:
        return None
    try:
        # (batch=1, n_variates=1, time_steps). Toto wants float32.
        target = torch.tensor(closes, dtype=torch.float32).unsqueeze(0).unsqueeze(0).to(device)
        target_mask = torch.ones_like(target, dtype=torch.bool)
        series_ids = torch.zeros(1, 1, dtype=torch.long, device=device)
        # decode_block_size=768 mirrors Datadog's example; horizon ≤ this
        # is fine, larger horizons would need chunking (out of scope for
        # the forecaster's 1-1024 bar range).
        quantiles = model.forecast(
            {"target": target, "target_mask": target_mask, "series_ids": series_ids},
            horizon=int(prediction_len),
            decode_block_size=768,
            has_missing_values=False,
        )
    except Exception as e:
        logger.warning("Toto-2.0 forecast call failed: %s", e)
        return None
    try:
        # quantiles shape: (9, batch=1, n_variates=1, horizon)
        arr = quantiles.detach().cpu().numpy()
        # Reduce to (9, horizon).
        arr = arr[:, 0, 0, :]
        q10 = arr[_TOTO_Q10_IDX].astype(float).tolist()
        q50 = arr[_TOTO_Q50_IDX].astype(float).tolist()
        q90 = arr[_TOTO_Q90_IDX].astype(float).tolist()
        q25_arr, q75_arr = _interpolate_q25_q75(arr)
        q25 = q25_arr.astype(float).tolist()
        q75 = q75_arr.astype(float).tolist()
    except Exception as e:
        logger.warning("Toto-2.0 unparseable output: %s", e)
        return None
    last = float(closes[-1])
    return BackboneResult(
        q10=q10, q25=q25, q50=q50, q75=q75, q90=q90,
        confidence=_per_step_confidence(last, q10, q90),
        model_name="Datadog Toto-2.0",
    )


# --- registry ---------------------------------------------------------------

_BACKBONES = {
    "chronos2": chronos_forecast,
    "toto2": toto_forecast,
}


def run(
    backbone: str,
    closes: np.ndarray,
    *,
    prediction_len: int,
) -> BackboneResult | None:
    """Dispatch a forecast to the named backbone. Unknown name → None.

    Single entry point keeps the agent module agnostic to which backbone
    actually runs; adding a third backbone (Moirai, TimesFM, …) is one
    new function + one dict entry below."""
    fn = _BACKBONES.get(backbone)
    if fn is None:
        logger.warning("Unknown forecaster backbone: %s", backbone)
        return None
    return fn(closes, prediction_len=prediction_len)


# --- shared confidence formula ---------------------------------------------

def _per_step_confidence(last: float, q10: Sequence[float], q90: Sequence[float]) -> list[int]:
    """Confidence at each forecast step, derived from the 80% prediction
    interval's width as a fraction of the last close.

    Mapped through a smooth decay so common values land in 0-100:
      - 2%  width → 83 (very tight; rare past a few days)
      - 5%  width → 67 (typical day-1 to day-5 for large-caps)
      - 10% width → 50 (typical end-of-horizon)
      - 20% width → 33 (volatile names)
      - 30% width → 25 (low-quality forecast territory)

    Decays monotonically with horizon — that's the property that makes
    the curve worth showing. Distinct from the agent's signal-level
    confidence (magnitude × agreement at horizon end). Shared across
    backbones so the chart's confidence trace renders identically."""
    out: list[int] = []
    base = max(last, 1e-9)
    for lo, hi in zip(q10, q90):
        width_pct = max(0.0, (hi - lo) / base * 100.0)
        conf = 100.0 / (1.0 + width_pct / 10.0)
        out.append(int(round(max(0.0, min(100.0, conf)))))
    return out
