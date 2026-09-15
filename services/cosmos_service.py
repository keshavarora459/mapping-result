import logging
import aiohttp
from typing import Optional, Dict, Any
from config import COSMOS_DB_API
from auth import request_token_ctx

logger = logging.getLogger(__name__)


def _is_not_found_body(data: Dict[str, Any]) -> bool:
    """The MongoDB API answers a miss with HTTP 200 and a body like
    `{"message": "No record found for folder '...'"}` instead of a 404.
    Treating that as real data made /api/mapping silently return an empty
    'success' Contract 2.0 result for an app/run that was never parsed,
    instead of the 400 the caller needs to know nothing was found."""
    return isinstance(data, dict) and "message" in data and not (
        data.get("parsing_result") or data.get("mapping_result") or data.get("tables") or data.get("fields")
    )


import os

def _get_base_apis() -> list:
    """Collect configured MongoDB API endpoints from environment variables."""
    apis = []
    for env_var in ["COSMOS_BASE_API", "MONGO_API_URL", "QLIK_MONGO_API_URL", "COSMOS_DB_API", "BASE_API_URL"]:
        val = os.getenv(env_var)
        if val and val.strip():
            clean_val = val.strip().rstrip("/")
            if clean_val not in apis:
                apis.append(clean_val)
    if not apis and COSMOS_DB_API:
        apis.append(COSMOS_DB_API)
    return apis


async def fetch_parsing_from_cosmos(app_id: str, run_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Fetch parsed metadata directly from MongoDB API via run_id or app_id."""
    token = request_token_ctx.get()
    headers = {'Authorization': f"Bearer {token}"} if token else {}

    urls_to_try = []
    for base in _get_base_apis():
        if run_id:
            urls_to_try.append(f"{base}/parsing/by-run/{run_id}")
            urls_to_try.append(f"{base}/parsing/{run_id}")
        if app_id:
            urls_to_try.append(f"{base}/parsing/by-app/{app_id}")
            urls_to_try.append(f"{base}/parsing/{app_id}")

    async with aiohttp.ClientSession(headers=headers) as session:
        for url in urls_to_try:
            try:
                async with session.get(url, timeout=15) as response:
                    if response.status == 200:
                        data = await response.json()
                        if isinstance(data, list) and data:
                            parsing_result = data[0].get("parsing_result")
                            if isinstance(parsing_result, dict) and parsing_result:
                                return parsing_result
                            return data[0]
                        elif isinstance(data, dict) and not _is_not_found_body(data):
                            return data.get("parsing_result") or data
            except Exception as e:
                logger.warning(f"Failed to fetch parsing from {url}: {e}")

    return None


async def fetch_mapping_from_cosmos(app_id: str, run_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Fetch mapped result from MongoDB API via run_id or app_id."""
    token = request_token_ctx.get()
    headers = {'Authorization': f"Bearer {token}"} if token else {}

    urls_to_try = []
    for base in _get_base_apis():
        if run_id:
            urls_to_try.append(f"{base}/mapping/by-run/{run_id}")
            urls_to_try.append(f"{base}/mapping/{run_id}")
        if app_id:
            urls_to_try.append(f"{base}/mapping/by-app/{app_id}")
            urls_to_try.append(f"{base}/mapping/{app_id}")

    async with aiohttp.ClientSession(headers=headers) as session:
        for url in urls_to_try:
            try:
                async with session.get(url, timeout=15) as response:
                    if response.status == 200:
                        data = await response.json()
                        if isinstance(data, list) and data:
                            mapping_result = data[0].get("mapping_result")
                            if isinstance(mapping_result, dict) and mapping_result:
                                return mapping_result
                            return data[0]
                        elif isinstance(data, dict) and not _is_not_found_body(data):
                            return data.get("mapping_result") or data
            except Exception as e:
                logger.warning(f"Failed to fetch mapping from {url}: {e}")

    return None


async def save_mapping_to_cosmos(
    app_id: str, 
    space_id: str, 
    app_name: str, 
    run_id: str, 
    mapping_result: Dict[str, Any]
) -> Dict[str, str]:
    """Save converted mapping result back to MongoDB / Cosmos DB."""
    if not app_id and not run_id:
        return {"status": "skipped", "message": "No app_id or run_id provided"}
    
    last_error = None
    for base in _get_base_apis():
        try:
            url = f"{base}/mapping"
            payload = {
                "app_id": app_id or "",
                "space_id": space_id or "",
                "app_name": app_name or "",
                "run_id": run_id or "",
                "mapping_result": mapping_result
            }
            token = request_token_ctx.get()
            headers = {'Content-Type': 'application/json'}
            if token:
                headers['Authorization'] = f"Bearer {token}"
                
            async with aiohttp.ClientSession(headers=headers) as session:
                async with session.post(url, json=payload, timeout=30) as response:
                    response.raise_for_status()
                    return {"status": "success", "message": "Mapping result saved"}
        except Exception as e:
            last_error = e
            logger.error(f"Error saving mapping to {base}: {e}")
    return {"status": "error", "message": str(last_error)}
