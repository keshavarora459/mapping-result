import os
import logging
try:
    from cachetools import TTLCache
except ImportError:
    class TTLCache(dict):
        def __init__(self, maxsize=100, ttl=300):
            super().__init__()

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    def load_dotenv():
        pass

logger = logging.getLogger(__name__)

COSMOS_DB_API = (
    os.getenv("COSMOS_BASE_API")
    or os.getenv("MONGO_API_URL")
    or os.getenv("QLIK_MONGO_API_URL")
    or os.getenv("BASE_API_URL")
    or ""
).rstrip("/")
TARGET_API_URL = os.getenv("TARGET_API_URL", "")

TENANT_ID = os.getenv("AZURE_AD_TENANT_ID", os.getenv("TENANT_ID"))
EXPECTED_AUDIENCE = os.getenv("AZURE_AD_EXPECTED_AUDIENCE")
ISSUER = f"https://sts.windows.net/{TENANT_ID}/" if TENANT_ID else ""
JWKS_URL = f"https://login.microsoftonline.com/{TENANT_ID}/discovery/v2.0/keys" if TENANT_ID else ""
ENABLE_AUTH = os.getenv("ENABLE_AUTH", "true").lower() != "false"

HEADERS = {'Content-Type': 'application/json'}
get_results_cache = TTLCache(maxsize=100, ttl=3600)


class Config:
    MAX_RETRIES = int(os.getenv("LLM_MAX_RETRIES", "2"))
    GROQ_API_KEY = os.getenv("GROQ_API_KEY")
    GROQ_MODEL = os.getenv("GROQ_MODEL", "groq/compound-mini")
    USE_GROQ = True

    BASE_API_URL = COSMOS_DB_API
    AGENT_ACTIONS_API_URL = os.getenv("AGENT_ACTIONS_API_URL") or COSMOS_DB_API
    RETRY_DELAY = float(os.getenv("LLM_RETRY_DELAY", "1.0"))
    LLM_MAX_CONCURRENCY = int(os.getenv("LLM_MAX_CONCURRENCY", "2"))

    # --- LLM-assisted conversion configuration ---
    # Core measures and custom objects use LLM when needed;
    # Deterministic columns & M-queries are handled by connection_mapper without wasting rate limits
    USE_LLM_MEASURES = True
    USE_LLM_DIMENSIONS = False
    USE_LLM_VISUALS = True
    USE_LLM_VARIABLES = False
    USE_LLM_COLUMNS = False
    USE_LLM_MQUERY = False

    # Character budget for rules block injected into system prompt
    LLM_RULES_CHAR_BUDGET = int(os.getenv("LLM_RULES_CHAR_BUDGET", "12000"))
    LLM_SYSTEM_PROMPT_LIMIT = int(os.getenv("LLM_SYSTEM_PROMPT_LIMIT", "24000"))
    LLM_SCHEMA_COLUMN_LIMIT = int(os.getenv("LLM_SCHEMA_COLUMN_LIMIT", "40"))

    @staticmethod
    def validate() -> None:
        logger.info("Validating LLM configuration")
        if not Config.GROQ_API_KEY:
            logger.warning("GROQ_API_KEY is not set in environment. Deterministic mappings will run; LLM calls will require key.")
        if not COSMOS_DB_API:
            logger.warning("No MongoDB API base URL configured in environment (COSMOS_BASE_API / MONGO_API_URL / BASE_API_URL)")
        
        logger.info("Using Groq API provider with model %s", Config.GROQ_MODEL)

