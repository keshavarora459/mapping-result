"""Production Readiness Gate & Migration Status Engine for Enterprise Qlik -> Power BI Migration."""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional
from services.validators.dax_validators import validate_dax_schema_binding


class ProductionGateStatus(str, Enum):
    PRODUCTION_READY = "PRODUCTION_READY"
    PRODUCTION_READY_WITH_REVIEW = "PRODUCTION_READY_WITH_REVIEW"
    NOT_PRODUCTION_READY = "NOT_PRODUCTION_READY"


@dataclass
class GateEvaluation:
    status: ProductionGateStatus
    score: float
    blocking_reasons: List[str] = field(default_factory=list)
    review_items: List[str] = field(default_factory=list)
    checks: Dict[str, bool] = field(default_factory=dict)
    migration_status: Dict[str, Any] = field(default_factory=dict)


class ProductionGate:
    """Evaluates whether an end-to-end migration artifact is production ready according to strict Quality Gates."""

    @classmethod
    def evaluate(cls, mapping_payload: Dict[str, Any]) -> GateEvaluation:
        blocking_reasons: List[str] = []
        review_items: List[str] = []
        checks: Dict[str, bool] = {}

        tables = mapping_payload.get("tables", [])
        measures = mapping_payload.get("measures", [])
        relationships = mapping_payload.get("relationships", [])
        visuals = (mapping_payload.get("visuals") or {}).get("sheet_visuals", [])

        # 1. Power Query M Validation
        m_passed = True
        has_tables = len(tables) > 0 and all(len(t.get("columns", [])) > 0 for t in tables)
        if not has_tables:
            m_passed = False
            blocking_reasons.append("No valid tables with columns mapped in model")

        for t in tables:
            m_q = str(t.get("m_query") or "")
            t_name = t.get("name") or "Unknown"

            # Raw lib:// paths
            if "lib://" in m_q or "Folder.Files(\"'lib:" in m_q:
                m_passed = False
                blocking_reasons.append(f"Table '{t_name}' contains unresolved Qlik lib:// connection path in M query")

            # QVD treated as CSV
            if ".qvd" in m_q.lower() and "csv.document" in m_q.lower():
                m_passed = False
                blocking_reasons.append(f"Table '{t_name}' attempts to parse QVD file with Csv.Document")

            # Unresolved placeholder table
            if "#table({\"*\"}" in m_q or "#table({}, {})" in m_q:
                m_passed = False
                blocking_reasons.append(f"Table '{t_name}' contains unresolved empty table placeholder")

            # Plaintext credentials
            if any(kw in m_q.lower() for kw in ["password=", "pwd=", "secret=", "apikey="]):
                m_passed = False
                blocking_reasons.append(f"Table '{t_name}' contains exposed plaintext credentials in M query")

            # Check if table confidence requires review
            if t.get("confidence", {}).get("requires_review"):
                rat = t.get("confidence", {}).get("rationale") or "Requires manual review"
                review_items.append(f"Table '{t_name}': {rat}")

        checks["m_validation"] = m_passed

        # 2. Model & Relationship Validation
        model_passed = True
        if not tables:
            model_passed = False
        table_names = {str(t.get("name", "")).lower() for t in tables}

        for r in relationships:
            src_t = str(r.get("source_table") or r.get("from_table") or "").lower()
            tgt_t = str(r.get("target_table") or r.get("to_table") or "").lower()
            if src_t and src_t not in table_names:
                model_passed = False
                blocking_reasons.append(f"Relationship source table '{src_t}' does not exist in model")
            if tgt_t and tgt_t not in table_names:
                model_passed = False
                blocking_reasons.append(f"Relationship target table '{tgt_t}' does not exist in model")

        checks["model_validation"] = model_passed

        # 3. DAX & Schema Binding Validation
        dax_passed = True
        dax_val_result = validate_dax_schema_binding(measures, tables)
        if not dax_val_result["valid"]:
            dax_passed = False
            for err in dax_val_result.get("errors", []):
                blocking_reasons.append(f"DAX Schema Binding Error: {err.get('error')}")

        unresolved_measures = [
            m.get("name") for m in measures
            if m.get("dax_expression") == "BLANK()" or m.get("conversion_method") == "unresolved"
        ]
        if unresolved_measures:
            review_items.append(f"{len(unresolved_measures)} measure(s) unresolved: {unresolved_measures[:3]}")

        checks["dax_validation"] = dax_passed

        # 4. Visual Validation
        visual_passed = True
        unsupported_visuals = [
            v.get("name") for v in visuals
            if not v.get("fabric", {}).get("supported", True)
        ]
        if unsupported_visuals:
            review_items.append(f"{len(unsupported_visuals)} visual(s) without direct Fabric equivalent: {unsupported_visuals[:3]}")

        unbound_visuals = [
            v.get("name") for v in visuals
            if not v.get("fabric", {}).get("field_roles") and v.get("fabric", {}).get("supported", True)
        ]
        if unbound_visuals:
            review_items.append(f"{len(unbound_visuals)} visual(s) have unpopulated field roles")

        checks["visual_validation"] = visual_passed

        # Overall Status Determination
        requires_review = bool(blocking_reasons or review_items)
        publish_ready = bool(
            m_passed
            and model_passed
            and dax_passed
            and visual_passed
            and not blocking_reasons
        )

        if not publish_ready and blocking_reasons:
            status = ProductionGateStatus.NOT_PRODUCTION_READY
            score = 0.4
            conversion_status = "partial" if (tables or measures) else "failed"
        elif requires_review:
            status = ProductionGateStatus.PRODUCTION_READY_WITH_REVIEW
            score = 0.85
            conversion_status = "converted"
        else:
            status = ProductionGateStatus.PRODUCTION_READY
            score = 1.0
            conversion_status = "converted"

        # Construct Requirement 30 compliant migration_status schema
        migration_status = {
            "conversion_status": conversion_status,
            "conversion_method": "hybrid" if mapping_payload.get("llm_status", {}).get("used") else "deterministic",
            "m_validation": "passed" if m_passed else "failed",
            "model_validation": "passed" if model_passed else "failed",
            "dax_validation": "passed" if dax_passed else "failed",
            "visual_validation": "passed" if visual_passed else "failed",
            "runtime_desktop": mapping_payload.get("runtime_desktop", "not_tested"),
            "runtime_service": mapping_payload.get("runtime_service", "not_tested"),
            "reconciliation": mapping_payload.get("reconciliation", "not_tested"),
            "requires_review": requires_review,
            "publish_ready": publish_ready,
            "blocking_reasons": blocking_reasons,
            "review_items": review_items,
        }

        return GateEvaluation(
            status=status,
            score=score,
            blocking_reasons=blocking_reasons,
            review_items=review_items,
            checks=checks,
            migration_status=migration_status,
        )
