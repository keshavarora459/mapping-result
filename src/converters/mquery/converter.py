"""LLM-assisted repair of generated Power Query M.

services/connection_mapper.py builds each table's M expression from a
per-connector template. That is the right default - a connector signature is
a fact, not a judgement - but a template cannot express the parts of a Qlik
LOAD that genuinely require interpretation: preceding loads, resident chains,
CROSSTABLE unpivots, ApplyMap lookups and inline tables.

This stage reviews the generated M against the original Qlik statement.

`USE_LLM_MQUERY` defaults to **false**. A wrong DAX measure produces a wrong
number in one visual; a wrong M query means the table does not load at all
and the whole report is empty. This should stay off until the golden-file
regression harness exists.
"""

import asyncio
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from config import Config
from services import llm_usage
from services.prompt_builder import build_mquery_prompts

logger = logging.getLogger(__name__)

STAGE = "mquery"

# Connector calls that mean the query actually reaches a real source rather
# than the `let Source = TableName in Source` placeholder.
SOURCE_MARKERS = (
    ".Database(", ".Databases(", ".Files(", ".Catalogs(", ".Contents(",
    "NativeQuery(", "Table.FromRows(", "Json.Document(", "#table(",
    "Csv.Document(", "Excel.Workbook(", "Parquet.Document(",
)

CODE_FENCE = re.compile(r"```[a-zA-Z]*\s*(.*?)```", re.DOTALL)


def strip_fences(text: str) -> str:
    if not text:
        return ""
    cleaned = text.strip()
    if "```" in cleaned:
        blocks = CODE_FENCE.findall(cleaned)
        if blocks:
            cleaned = max(blocks, key=len)
    return cleaned.strip()


def _balanced(expr: str) -> bool:
    """Parens/brackets/braces balanced outside string literals.

    M strings are double-quoted with `""` as the escape, so a quote is a
    toggle unless it is immediately doubled.
    """
    depth = 0
    in_string = False
    index = 0
    while index < len(expr):
        char = expr[index]
        if char == '"':
            if in_string and index + 1 < len(expr) and expr[index + 1] == '"':
                index += 2
                continue
            in_string = not in_string
        elif not in_string:
            if char in "([{":
                depth += 1
            elif char in ")]}":
                depth -= 1
                if depth < 0:
                    return False
        index += 1
    return depth == 0 and not in_string


def validate_mquery(
    candidate: str,
    table_name: str,
    known_queries: Optional[List[str]] = None,
) -> Tuple[bool, List[str]]:
    """Structural checks on a candidate M expression."""
    problems: List[str] = []
    if not candidate or not candidate.strip():
        return False, ["expression is empty"]

    text = candidate.strip()

    if not re.match(r"^\s*let\b", text, re.IGNORECASE):
        problems.append("does not start with 'let'")
    if not re.search(r"\bin\b", text):
        problems.append("has no 'in' clause")
    if not _balanced(text):
        problems.append("unbalanced parentheses, brackets or quotes")
    if "$(" in text:
        problems.append(
            "contains an unexpanded Qlik dollar-sign expansion, which is not valid M"
        )
    if not any(marker in text for marker in SOURCE_MARKERS):
        problems.append("no real connector call - looks like an unresolved placeholder")

    # Qlik syntax that should have been translated away.
    for leftover in ("RESIDENT ", "AUTOGENERATE", "ApplyMap(", "CROSSTABLE(", "INLINE ["):
        if re.search(re.escape(leftover), text, re.IGNORECASE):
            problems.append(f"still contains untranslated Qlik syntax: {leftover.strip()}")

    # Local file paths never resolve in the Fabric service.
    if re.search(r'"[A-Za-z]:\\\\', text) or "lib://" in text:
        problems.append("references a local or lib:// path that will not resolve in Fabric")

    # M is case-sensitive; these are the common mis-cased forms.
    for wrong in ("TEXT.", "TABLE.", "LIST.", "DATE.", "NUMBER."):
        if wrong in text:
            problems.append(f"mis-cased M function prefix '{wrong}' (M is case-sensitive)")
            break

    return (not problems), problems


class MQueryConverter:
    """Reviews and repairs generated Power Query M against the Qlik source."""

    def __init__(self, llm_client=None):
        self._llm_client = llm_client

    @property
    def llm_client(self):
        if self._llm_client is None:
            from src.converters.llm_client import GroqLLMClient

            self._llm_client = GroqLLMClient()
        return self._llm_client

    @staticmethod
    def _connection_context(table: Dict[str, Any]) -> str:
        connection = table.get("connection")
        if not isinstance(connection, dict):
            return "(no connection metadata supplied)"
        keep = (
            "name", "lib_name", "connection_id", "type", "provider", "driver",
            "server", "host", "database", "schema", "path", "url", "catalog",
        )
        lines = [f"- {k}: {connection[k]}" for k in keep if connection.get(k)]
        return "\n".join(lines) or "(no connection metadata supplied)"

    async def refine_one(
        self,
        table: Dict[str, Any],
        baseline: str,
        known_queries: List[str],
    ) -> Dict[str, Any]:
        usage = llm_usage.current()
        name = str(table.get("name") or table.get("table_name") or "")

        usage.record_attempt(STAGE)
        try:
            system, user = build_mquery_prompts(
                table=table,
                baseline_mquery=baseline,
                connection_context=self._connection_context(table),
                upstream_tables=known_queries,
            )
            answer = await self.llm_client.generate_text(system, user)
            usage.record_success(STAGE)
        except Exception as exc:  # noqa: BLE001
            usage.record_failure(STAGE, str(exc))
            logger.warning(
                "LLM M-query review failed for '%s' (%s); keeping generated query.",
                name, exc,
            )
            return table

        candidate = strip_fences(answer)
        if not candidate:
            usage.record_rejected(STAGE, "model returned nothing usable")
            return table

        ok, problems = validate_mquery(candidate, name, known_queries)
        if not ok:
            usage.record_rejected(STAGE, "; ".join(problems[:3]))
            logger.info(
                "Rejected LLM M-query for '%s' (%s); kept generated query.",
                name, "; ".join(problems[:3]),
            )
            return table

        if candidate.strip() == str(baseline).strip():
            return table

        usage.record_accepted(STAGE)
        table["baseline_m_expression"] = baseline
        table["m_expression"] = candidate
        from services.connection_mapper import ConnectionMapper
        table["m_query"] = ConnectionMapper().parse_mquery_to_steps(candidate)
        table["conversion_method"] = "llm_refined"
        return table

    async def refine_all(self, tables: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        if not Config.USE_LLM_MQUERY or not tables:
            return tables

        known = [
            str(t.get("name") or t.get("table_name") or "")
            for t in tables if isinstance(t, dict)
        ]
        targets = []
        for table in tables:
            if not isinstance(table, dict):
                continue
            raw_m = table.get("m_query") or table.get("m_expression") or table.get("mquery")
            if isinstance(raw_m, list):
                steps = []
                for s in raw_m:
                    if isinstance(s, dict) and "content" in s:
                        steps.append(str(s["content"]))
                    else:
                        steps.append(str(s))
                baseline = "\n".join(steps)
            else:
                baseline = str(raw_m or "")
            if baseline and str(baseline).strip():
                targets.append((table, str(baseline)))

        if not targets:
            return tables

        logger.info("Reviewing %d M-query expression(s) with the LLM", len(targets))
        await asyncio.gather(
            *(self.refine_one(table, baseline, known) for table, baseline in targets),
            return_exceptions=True,
        )
        return tables
