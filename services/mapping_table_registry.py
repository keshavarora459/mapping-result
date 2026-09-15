"""Generic Mapping Table Registry for Qlik ApplyMap() Semantics."""

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class MappingTableDefinition:
    mapping_name: str
    key_expression: str
    value_expression: str
    default_expression: Optional[str] = None
    source_table: Optional[str] = None
    dependencies: List[str] = field(default_factory=list)
    lineage: Dict[str, Any] = field(default_factory=dict)


class MappingTableRegistry:
    """Canonical registry for Qlik mapping tables and ApplyMap resolution."""

    def __init__(self):
        self._mappings: Dict[str, MappingTableDefinition] = {}

    def register(self, definition: MappingTableDefinition) -> None:
        self._mappings[definition.mapping_name.lower()] = definition

    def register_mapping(
        self,
        map_name: str,
        source_table: str,
        key_column: str,
        value_column: str,
        default_value: Optional[str] = None,
    ) -> None:
        """Helper to register a mapping table definition directly."""
        self.register(
            MappingTableDefinition(
                mapping_name=map_name,
                key_expression=key_column,
                value_expression=value_column,
                default_expression=default_value,
                source_table=source_table,
                dependencies=[key_column, value_column],
                lineage={"source": "manual_or_test", "name": map_name},
            )
        )

    def get(self, mapping_name: str) -> Optional[MappingTableDefinition]:
        return self._mappings.get((mapping_name or "").lower())

    def discover_from_script(self, qlik_script: str) -> None:
        """Parse MAPPING LOAD statements from Qlik script and register them dynamically."""
        if not qlik_script:
            return

        # Pattern: MapName:\s*MAPPING\s+LOAD\s+([A-Za-z0-9_#]+)\s*,\s*([A-Za-z0-9_#]+)(?:\s+RESIDENT\s+([A-Za-z0-9_#]+)|\s+FROM\s+[^\;]+)?\;
        mapping_blocks = re.finditer(
            r"([A-Za-z0-9_#]+)\s*:\s*MAPPING\s+LOAD\s+([A-Za-z0-9_#\[\]]+)\s*,\s*([A-Za-z0-9_#\[\]]+)(?:\s+RESIDENT\s+([A-Za-z0-9_#]+))?",
            qlik_script,
            re.IGNORECASE,
        )
        for match in mapping_blocks:
            map_name = match.group(1).strip()
            key_field = match.group(2).strip("[] ")
            val_field = match.group(3).strip("[] ")
            src_tbl = match.group(4).strip() if match.group(4) else None

            self.register(
                MappingTableDefinition(
                    mapping_name=map_name,
                    key_expression=key_field,
                    value_expression=val_field,
                    source_table=src_tbl,
                    dependencies=[key_field, val_field],
                    lineage={"source": "script_mapping_load", "name": map_name},
                )
            )

    def discover_from_tables(self, tables: List[Dict[str, Any]]) -> None:
        """Scan tables list for mapping tables or scripts."""
        for t in tables or []:
            if not isinstance(t, dict):
                continue
            t_name = t.get("name") or t.get("table_name") or ""
            q_script = t.get("qlik_query") or t.get("load_statement") or ""
            if q_script:
                self.discover_from_script(q_script)

            if t.get("is_mapping") or t.get("load_type") == "mapping":
                cols = t.get("columns") or t.get("fields") or []
                if len(cols) >= 2:
                    k_col = cols[0].get("fabric_column_name") or cols[0].get("qlik_column_name") or cols[0].get("name") if isinstance(cols[0], dict) else str(cols[0])
                    v_col = cols[1].get("fabric_column_name") or cols[1].get("qlik_column_name") or cols[1].get("name") if isinstance(cols[1], dict) else str(cols[1])
                    self.register(
                        MappingTableDefinition(
                            mapping_name=t_name,
                            key_expression=str(k_col),
                            value_expression=str(v_col),
                            source_table=t_name,
                            dependencies=[str(k_col), str(v_col)],
                            lineage={"source": "table_metadata", "name": t_name},
                        )
                    )

    def resolve_applymap_dax(
        self,
        map_name: str,
        key_expr: str,
        default_expr: Optional[str] = None,
        known_tables: Optional[List[Dict[str, Any]]] = None,
    ) -> Tuple[str, bool]:
        """Convert ApplyMap(map_name, key, default) to DAX LOOKUPVALUE / COALESCE.
        
        Returns: (dax_expression, success)
        """
        mapping_def = self.get(map_name)
        target_col = "Value"
        source_col = "Key"
        table_name = map_name
        is_found = False

        if mapping_def:
            source_col = mapping_def.key_expression
            target_col = mapping_def.value_expression
            table_name = mapping_def.source_table or map_name
            is_found = True
        elif known_tables:
            for tbl in known_tables:
                if (tbl.get("name") or tbl.get("table_name") or "").lower() == map_name.lower():
                    cols = tbl.get("columns", [])
                    if len(cols) >= 2:
                        source_col = cols[0].get("fabric_column_name") or cols[0].get("qlik_column_name") or "Key"
                        target_col = cols[1].get("fabric_column_name") or cols[1].get("qlik_column_name") or "Value"
                    elif len(cols) == 1:
                        target_col = cols[0].get("fabric_column_name") or cols[0].get("qlik_column_name") or "Value"
                    table_name = tbl.get("name") or map_name
                    is_found = True
                    break

        if not is_found:
            review_note = f"REVIEW_REQUIRED: Mapping table '{map_name}' is referenced by ApplyMap but does not exist in model or script."
            fallback = f"/* {review_note} */ LOOKUPVALUE('{table_name}'[{target_col}], '{table_name}'[{source_col}], {key_expr})"
            if default_expr:
                return f"COALESCE({fallback}, {default_expr})", False
            return fallback, False

        lookup = f"LOOKUPVALUE('{table_name}'[{target_col}], '{table_name}'[{source_col}], {key_expr})"
        if default_expr:
            return f"COALESCE({lookup}, {default_expr})", True
        return lookup, True

    def translate_applymap_expression(
        self,
        qlik_expr: str,
        known_tables: Optional[List[Dict[str, Any]]] = None,
    ) -> str:
        """Parse and translate single or nested ApplyMap expressions into DAX inside-out."""
        applymap_pattern = re.compile(
            r"ApplyMap\s*\(\s*'([^']+)'\s*,\s*([^,()]+|\([^)]+\)|ApplyMap\([^)]+\))\s*(?:,\s*('[^']*'|[^)]+?))?\s*\)",
            re.IGNORECASE,
        )
        current = qlik_expr
        for _ in range(5):
            match = applymap_pattern.search(current)
            if not match:
                break
            map_name = match.group(1)
            key_expr = match.group(2).strip()
            def_expr = match.group(3).strip() if match.group(3) else None
            dax_lookup, _ = self.resolve_applymap_dax(map_name, key_expr, def_expr, known_tables)
            current = current[:match.start()] + dax_lookup + current[match.end():]
        return current


    def build_mapping_table_m(self, mapping_name: str, key_col: str, val_col: str, rows: Optional[List[Tuple[Any, Any]]] = None) -> str:
        """Build executable Power Query M for a mapping lookup table with real rows."""
        if rows:
            row_items = []
            for k, v in rows:
                k_val = f'"{k}"' if isinstance(k, str) else str(k)
                v_val = f'"{v}"' if isinstance(v, str) else str(v)
                row_items.append(f"{{{k_val}, {v_val}}}")
            rows_str = "{\n        " + ",\n        ".join(row_items) + "\n    }"
            return (
                f'let\n'
                f'    Source = #table({{"{key_col}", "{val_col}"}}, {rows_str}),\n'
                f'    #"Changed Type" = Table.TransformColumnTypes(Source, {{ {{"{key_col}", type text}}, {{"{val_col}", type text}} }})\n'
                f'in\n'
                f'    #"Changed Type"'
            )
        return (
            f'let\n'
            f'    Source = #table({{"{key_col}", "{val_col}"}}, {{}}),\n'
            f'    #"Changed Type" = Table.TransformColumnTypes(Source, {{ {{"{key_col}", type text}}, {{"{val_col}", type text}} }})\n'
            f'in\n'
            f'    #"Changed Type"'
        )

