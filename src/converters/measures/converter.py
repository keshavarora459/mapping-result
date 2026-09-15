"""LLM-assisted Qlik measure -> DAX conversion.

This converter did not exist: `src/converters/measures/` held only `rules.py`,
so MEASURES_CORE_RULES and MEASURES_ADVANCED_RULES were never sent anywhere
and every measure was produced by DAXConverter's regex pipeline alone.

The design is deliberately baseline-first:

  1. DAXConverter produces a deterministic draft (never skipped).
  2. The draft, the Qlik source and the resolved schema go to the model,
     which is asked to *correct* the draft rather than translate from
     scratch.
  3. dax_guard validates the answer. On any problem the draft is kept.

So turning USE_LLM_MEASURES on can raise quality but cannot fall below the
regex floor, which matters because a bad DAX expression produces a wrong
number rather than an obviously broken chart.
"""

import asyncio
import logging
from typing import Any, Dict, List, Optional

from config import Config
from services import dax_guard, llm_usage
from services.prompt_builder import build_measure_prompts
from services.schema_context import build_schema_context

logger = logging.getLogger(__name__)

STAGE = "measures"


class MeasureConverter:
    """Refines deterministic measure conversions with the LLM."""

    def __init__(self, llm_client=None):
        self._llm_client = llm_client

    @property
    def llm_client(self):
        # Constructed lazily so importing this module never requires a
        # configured GROQ_API_KEY (tests, offline runs).
        if self._llm_client is None:
            from src.converters.llm_client import GroqLLMClient

            self._llm_client = GroqLLMClient()
        return self._llm_client

    @staticmethod
    def _table_hint(measure: Dict[str, Any], tables: List[Dict[str, Any]]) -> str:
        declared = measure.get("tables") or []
        if declared and str(declared[0]).strip():
            return str(declared[0])
        return str((tables[0].get("name") if tables else "") or "")

    async def refine_one(
        self,
        measure: Dict[str, Any],
        tables: List[Dict[str, Any]],
        schema_context: str,
    ) -> Dict[str, Any]:
        """Return `measure` with its DAX upgraded when the model improves it.

        The measure dict is mutated in place and returned; the caller already
        holds the list produced by DAXConverter.
        """
        usage = llm_usage.current()
        fabric = measure.get("fabric") if isinstance(measure.get("fabric"), dict) else {}
        baseline = str(fabric.get("dax_expression") or measure.get("dax_expression") or "").strip()
        qlik_expr = str(measure.get("qlik_expression") or "").strip()
        name = str(measure.get("name") or "Measure")

        # Nothing to improve on: no source expression means the baseline is
        # a stub, and the model has no material to work from either.
        if not qlik_expr:
            return measure

        system, user = build_measure_prompts(
            name=name,
            qlik_expression=qlik_expr,
            schema_context=schema_context,
            baseline_dax=baseline,
            table_hint=self._table_hint(measure, tables),
        )

        usage.record_attempt(STAGE)
        try:
            answer = await self.llm_client.generate_text(system, user)
            usage.record_success(STAGE)
        except Exception as exc:  # noqa: BLE001
            usage.record_failure(STAGE, str(exc))
            logger.warning(
                "LLM measure conversion failed for '%s' (%s); keeping regex draft.",
                name, exc,
            )
            return measure

        chosen, used_llm, problems = dax_guard.choose(
            baseline, answer, tables, is_measure=True
        )

        if not used_llm:
            if problems:
                usage.record_rejected(STAGE, "; ".join(problems[:3]))
                logger.info(
                    "Rejected LLM DAX for measure '%s' (%s); kept regex draft.",
                    name, "; ".join(problems[:3]),
                )
            return measure

        if chosen.strip() == baseline.strip():
            # The model agreed with the draft. Worth recording as a
            # confirmation, but nothing changes.
            return measure

        usage.record_accepted(STAGE)
        measure["dax_expression"] = chosen
        measure["baseline_dax_expression"] = baseline
        measure["conversion_method"] = "llm_refined"
        if isinstance(fabric, dict):
            fabric["dax_expression"] = chosen
            tmdl = fabric.get("tmdl")
            if isinstance(tmdl, str) and baseline and baseline in tmdl:
                fabric["tmdl"] = tmdl.replace(baseline, chosen)
            else:
                fabric["tmdl"] = "measure '{}' = {}".format(name, chosen)

        confidence = measure.get("confidence")
        if isinstance(confidence, dict):
            confidence["rationale"] = (
                (confidence.get("rationale") or "").strip()
                + " Refined by LLM against the resolved schema and Qlik conversion rules."
            ).strip()
            confidence["llm_refined"] = True
        return measure

    async def refine_all(
        self,
        measures: List[Dict[str, Any]],
        tables: List[Dict[str, Any]],
        relationships: Optional[List[Dict[str, Any]]] = None,
    ) -> List[Dict[str, Any]]:
        """Refine every measure that has a Qlik expression to work from."""
        if not Config.USE_LLM_MEASURES or not measures:
            return measures

        schema_context = build_schema_context(
            tables, measures=measures, relationships=relationships
        )

        def needs_refinement(m: Dict[str, Any]) -> bool:
            if not isinstance(m, dict):
                return False
            qlik = str(m.get("qlik_expression") or m.get("expression") or "").strip()
            if not qlik:
                return False
            dax = str(m.get("dax_expression") or m.get("fabric", {}).get("dax_expression") or "").strip()
            if not dax or dax == "BLANK()":
                return True
            val = m.get("validation")
            if isinstance(val, dict) and not val.get("passed", True):
                return True
            conf = m.get("confidence")
            if isinstance(conf, dict):
                score = conf.get("score") or conf.get("confidence_score") or 1.0
                if score < 0.85 or conf.get("requires_review"):
                    return True
            if dax.startswith("=") or "num(" in dax.lower() or "aggr(" in dax.lower() or "match(" in dax.lower() or "applymap(" in dax.lower() or "above(" in dax.lower() or "below(" in dax.lower():
                return True
            return False

        candidates = [m for m in measures if needs_refinement(m)]
        if not candidates:
            logger.info("All %d measure(s) already have high confidence DAX; skipping LLM call.", len(measures))
            return measures

        logger.info("Refining %d / %d measure(s) with the LLM", len(candidates), len(measures))
        await asyncio.gather(
            *(self.refine_one(m, tables, schema_context) for m in candidates),
            return_exceptions=True,
        )
        return measures
