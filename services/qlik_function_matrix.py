"""Qlik Function to Power Query M and DAX Semantic Translation Matrix.

Handles Qlik date/time parsers (Date#, Time#, Timestamp, AddMonths, etc.),
string functions, summarize_by heuristics, and semantic data type mappings.
"""

import re
from typing import Any, Dict, List, Optional, Tuple


class QlikFunctionMatrix:
    """Translates Qlik scalar functions to Power Query M or DAX equivalents."""

    # -------------------------------------------------------------
    # 1. Summarize-By Semantic Heuristics
    # -------------------------------------------------------------
    @staticmethod
    def infer_summarize_by(column_name: str, datatype: str) -> str:
        """Infer summarize_by property based on column name and datatype."""
        col_clean = (column_name or "").lower().strip()
        dtype = (datatype or "").lower().strip()

        # Non-numeric or boolean -> none
        if dtype in ("string", "text", "date", "datetime", "boolean", "logical"):
            return "none"

        # Identifiers, codes, keys, year, month numbers, dates, flags -> none
        if any(kw in col_clean for kw in [
            "id", "_id", "key", "_key", "code", "_cd", "num", "number", "account",
            "postal", "zip", "phone", "year", "month", "day", "quarter", "rank",
            "sequence", "seq", "flag", "status", "version"
        ]):
            # Exception for count of items or actual amounts containing num
            if not any(amt_kw in col_clean for amt_kw in ["amount", "revenue", "cost", "sales", "qty", "quantity"]):
                return "none"

        # Rates, percentages, ratios, prices, averages -> average or none
        if any(kw in col_clean for kw in ["rate", "percent", "pct", "ratio", "margin", "price", "unit_price", "discount"]):
            return "average"

        # Metrics, financial amounts, counts -> sum
        if any(kw in col_clean for kw in [
            "amount", "amt", "revenue", "rev", "cost", "sales", "qty", "quantity",
            "volume", "pnl", "profit", "loss", "balance", "total", "fee", "tax", "load"
        ]) or dtype in ("double", "int64", "decimal", "number", "numeric"):
            return "sum"

        return "none"

    # -------------------------------------------------------------
    # 2. Semantic Data Type Mapping
    # -------------------------------------------------------------
    @staticmethod
    def map_datatype(qlik_type: str, sample_val: Optional[Any] = None) -> Tuple[str, str]:
        """Map Qlik datatype to (fabric_datatype, m_type_literal)."""
        raw = str(qlik_type or "").upper().strip()

        if "INT" in raw or raw in ("INTEGER", "LONG", "BIGINT"):
            return "int64", "Int64.Type"
        elif any(kw in raw for kw in ["NUM", "DOUBLE", "FLOAT", "DECIMAL", "REAL", "CURRENCY", "MONEY"]):
            return "double", "type number"
        elif "DATETIME" in raw or "TIMESTAMP" in raw:
            return "dateTime", "type datetime"
        elif "DATE" in raw:
            return "dateTime", "type date"
        elif "TIME" in raw:
            return "string", "type time"
        elif any(kw in raw for kw in ["BOOL", "BOOLEAN", "BIT"]):
            return "boolean", "type logical"
        else:
            return "string", "type text"

    # -------------------------------------------------------------
    # 3. Date / Time / String Function M Translations
    # -------------------------------------------------------------
    @staticmethod
    def translate_qlik_expression_to_m(expr: str) -> str:
        """Convert scalar Qlik date/string expressions to Power Query M."""
        if not expr:
            return ""

        res = expr.strip()

        # Date#([col], 'YYYY-MM-DD') -> Date.FromText([col])
        res = re.sub(
            r"Date#\s*\(\s*(\[[^\]]+\]|[A-Za-z0-9_]+)\s*(?:,\s*'[^']*')?\s*\)",
            r"Date.FromText(\1)",
            res,
            flags=re.IGNORECASE,
        )

        # Time#([col], 'hh:mm TT') -> Time.FromText([col])
        res = re.sub(
            r"Time#\s*\(\s*(\[[^\]]+\]|[A-Za-z0-9_]+)\s*(?:,\s*'[^']*')?\s*\)",
            r"Time.FromText(\1)",
            res,
            flags=re.IGNORECASE,
        )

        # AddMonths([col], n) -> Date.AddMonths([col], n)
        res = re.sub(
            r"AddMonths\s*\(",
            r"Date.AddMonths(",
            res,
            flags=re.IGNORECASE,
        )

        # Month([col]) -> Date.Month([col])
        res = re.sub(
            r"\bMonth\s*\(",
            r"Date.Month(",
            res,
            flags=re.IGNORECASE,
        )

        # Year([col]) -> Date.Year([col])
        res = re.sub(
            r"\bYear\s*\(",
            r"Date.Year(",
            res,
            flags=re.IGNORECASE,
        )

        # Lower([col]) -> Text.Lower([col]), Upper([col]) -> Text.Upper([col])
        res = re.sub(r"\bLower\s*\(", r"Text.Lower(", res, flags=re.IGNORECASE)
        res = re.sub(r"\bUpper\s*\(", r"Text.Upper(", res, flags=re.IGNORECASE)
        res = re.sub(r"\bTrim\s*\(", r"Text.Trim(", res, flags=re.IGNORECASE)
        res = re.sub(r"\bLen\s*\(", r"Text.Length(", res, flags=re.IGNORECASE)

        # If(cond, then, else) -> if cond then val1 else val2
        if_match = re.match(r"^\s*If\s*\((.*)\)\s*$", res, re.IGNORECASE | re.DOTALL)
        if if_match:
            args = if_match.group(1).split(",")
            if len(args) == 3:
                cond = args[0].strip()
                then_b = args[1].strip()
                else_b = args[2].strip()
                return f"if {cond} then {then_b} else {else_b}"

        return res

    @staticmethod
    def translate_qlik_expression_to_dax(expr: str) -> str:
        """Convert scalar Qlik date/string expressions to DAX."""
        if not expr:
            return ""
        res = expr.strip()
        # Date#([col], 'YYYY-MM-DD') -> DATEVALUE([col])
        res = re.sub(
            r"Date#\s*\(\s*(\[[^\]]+\]|[A-Za-z0-9_]+)\s*(?:,\s*'[^']*')?\s*\)",
            r"DATEVALUE(\1)",
            res,
            flags=re.IGNORECASE,
        )
        # Time#([col], 'hh:mm TT') -> TIMEVALUE([col])
        res = re.sub(
            r"Time#\s*\(\s*(\[[^\]]+\]|[A-Za-z0-9_]+)\s*(?:,\s*'[^']*')?\s*\)",
            r"TIMEVALUE(\1)",
            res,
            flags=re.IGNORECASE,
        )
        # AddMonths([col], n) -> EDATE([col], n)
        res = re.sub(
            r"AddMonths\s*\(",
            r"EDATE(",
            res,
            flags=re.IGNORECASE,
        )
        # Month([col]) -> MONTH([col])
        res = re.sub(r"\bMonth\s*\(", r"MONTH(", res, flags=re.IGNORECASE)
        # Year([col]) -> YEAR([col])
        res = re.sub(r"\bYear\s*\(", r"YEAR(", res, flags=re.IGNORECASE)
        # Len([col]) -> LEN([col])
        res = re.sub(r"\bLen\s*\(", r"LEN(", res, flags=re.IGNORECASE)
        return res

    @classmethod
    def qlik_date_to_m(cls, expr: str) -> str:
        return cls.translate_qlik_expression_to_m(expr)

    @classmethod
    def qlik_date_to_dax(cls, expr: str) -> str:
        return cls.translate_qlik_expression_to_dax(expr)


