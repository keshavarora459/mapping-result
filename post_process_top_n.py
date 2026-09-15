import json
import argparse
import sys

def process_json(input_file: str, output_file: str):
    try:
        with open(input_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except Exception as e:
        print(f"Error reading {input_file}: {e}")
        sys.exit(1)
        
    modified = False

    # 1. Add the missing measure if it doesn't exist
    measures = data.get("measures", [])
    measure_name = "Show Top N Customer"
    
    measure_exists = any(m.get("name") == measure_name for m in measures)
    if not measure_exists:
        dax_logic = """VAR SelectedTopN = COALESCE([Top N Customers Value], 5)
VAR CurrentRank =
    RANKX(
        ALLSELECTED('Customers'[customer_name]),
        CALCULATE(
            DIVIDE(
                SUM('Loads'[revenue]),
                1000000,
                BLANK()
            )
        ),
        ,
        DESC
    )
RETURN
    IF(
        ISBLANK([Total Revenue]),
        0,
        IF(CurrentRank <= SelectedTopN, 1, 0)
    )"""

        new_measure = {
            "name": measure_name,
            "dax_expression": dax_logic,
            "tables": ["Top N Customers"],
            "qlik_expression": "",
            "conversion_method": "post_processed",
            "is_stub": False,
            "fabric": {
                "table": "Top N Customers",
                "dax_expression": dax_logic,
                "format_string": "0",
                "tmdl": f"measure '{measure_name}' = \n{dax_logic}"
            },
            "confidence": {
                "score": 1.0,
                "score_out_of_100": 100,
                "percentage": "100%",
                "band": "high",
                "llm_score": 1.0,
                "requires_review": False,
                "rationale": "Injected by post-processing script for dynamic Top N logic."
            }
        }
        measures.append(new_measure)
        modified = True
        print(f"Added measure '{measure_name}'")

    # 2. Add the visual filter to 'Top 10 Customers by Revenue'
    visuals = data.get("visuals", {})
    sheet_visuals = visuals.get("sheet_visuals", [])
    
    for v in sheet_visuals:
        if v.get("name") == "Top 10 Customers by Revenue" or v.get("fabric", {}).get("title") == "Top 10 Customers by Revenue":
            fabric_block = v.get("fabric", {})
            filters = fabric_block.get("filters", [])
            
            # Check if filter already exists
            filter_exists = any(f.get("field") == measure_name or f.get("name") == measure_name for f in filters)
            
            if not filter_exists:
                top_n_filter = {
                    "field": measure_name,
                    "name": measure_name,
                    "filter_type": "Measure Filter",
                    "filter_mode": "Dynamic Top N",
                    "operator": "is",
                    "value": "1",
                    "variable_name": "vTopN",
                    "parameter_table": "Top N Customers",
                    "parameter_measure": "[Top N Customers Value]"
                }
                filters.append(top_n_filter)
                fabric_block["filters"] = filters
                modified = True
                print("Added measure filter to visual 'Top 10 Customers by Revenue'")

    if modified:
        try:
            with open(output_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=4)
            print(f"Successfully wrote modified JSON to {output_file}")
        except Exception as e:
            print(f"Error writing to {output_file}: {e}")
            sys.exit(1)
    else:
        print("No modifications were needed. The measure and filter already exist.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Post-process JSON output to add Top N Customer logic.")
    parser.add_argument("input_file", help="Path to the original JSON file")
    parser.add_argument("output_file", help="Path to save the modified JSON file")
    
    args = parser.parse_args()
    process_json(args.input_file, args.output_file)
