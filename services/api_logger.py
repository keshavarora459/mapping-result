import logging
try:
    import aiohttp
except ImportError:
    aiohttp = None
from config import Config, HEADERS
from auth import request_token_ctx
from common.llm import redact_sensitive_data

logger = logging.getLogger(__name__)

async def log_action_to_api(
    action_desc: str,
    app_id: str = None,
    details: str = None,
    run_id: str = None,
    workspace_id: str = None,
    project_name: str = None
) -> None:
    """Log an agent action to the external monitoring API."""
    safe_action = str(redact_sensitive_data(action_desc))
    safe_details = str(redact_sensitive_data(details or (f"Processing app {app_id}" if app_id else "General action")))
    logger.info(f"Action: {safe_action}, app_id='{app_id}', run_id='{run_id}'")
    base_api = (Config.AGENT_ACTIONS_API_URL or Config.BASE_API_URL or "").rstrip('/')
    if not base_api:
        return
    url = f"{base_api}/agent-actions"
    data = {
        "agent_name": "Mapping Agent",
        "activity_summary": safe_action,
        "action": safe_action,
        "details": safe_details,
        "correlation_id": run_id or app_id or "unknown",
        "run_id": run_id or "",
        "run_no": run_id or "",
        "app_id": app_id or "",
        "workbook_id": app_id or "",
        "workspace_id": workspace_id or "personal",
        "project_id": workspace_id or "personal",
        "project_name": project_name or "Unknown",
        "type": "agent_activity",
        "status": "success",
    }
    token = request_token_ctx.get()
    headers = HEADERS.copy()
    if token:
        headers['Authorization'] = f"Bearer {token}"
        
    async with aiohttp.ClientSession(headers=headers) as session:
        try:
            async with session.post(url, json=data, timeout=aiohttp.ClientTimeout(total=1.5)) as response:
                if response.status not in [200, 201]:
                    logger.debug(f"Action logger response status={response.status} from {url}")
        except Exception as e:  # noqa: BLE001 - a monitoring-endpoint hiccup must never break the mapping run
            logger.debug(f"API log action unavailable: {redact_sensitive_data(str(e)[:100])}")


async def log_error_to_api(
    error_message: str, 
    app_id: str = None, 
    endpoint: str = None, 
    status_code: int = None
) -> None:
    """Log an API error to the external monitoring API."""
    base_api = (Config.BASE_API_URL or "").rstrip('/')
    if not base_api:
        return
    url = f"{base_api}/api-error-logs"
    safe_err = str(redact_sensitive_data(error_message))
    data = {
        "service_name": "Mapping",
        "endpoint": endpoint or "unknown",
        "status_code": status_code or 0,
        "error_message": safe_err[:150],
        "correlation_id": app_id if app_id else "unknown"
    }
    token = request_token_ctx.get()
    headers = HEADERS.copy()
    if token:
        headers['Authorization'] = f"Bearer {token}"
        
    async with aiohttp.ClientSession(headers=headers) as session:
        try:
            async with session.post(url, json=data, timeout=aiohttp.ClientTimeout(total=1.5)) as response:
                if response.status not in [200, 201]:
                    logger.debug(f"Error logger response status={response.status} from {url}")
        except Exception as e:  # noqa: BLE001 - a monitoring-endpoint hiccup must never break the mapping run
            logger.debug(f"API log error unavailable: {str(e)[:100]}")
