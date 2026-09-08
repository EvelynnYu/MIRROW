"""Environment-only structured-model transport defaults for standalone hosts."""
import asyncio
import os
import httpx

DEEPSEEK_API_KEY = os.getenv('DEEPSEEK_API_KEY', '')
DEEPSEEK_API_URL = os.getenv('DEEPSEEK_API_URL', '')
DEEPSEEK_FLASH_API_KEY = os.getenv('DEEPSEEK_FLASH_API_KEY', '')
DEEPSEEK_FLASH_API_URL = os.getenv('DEEPSEEK_FLASH_API_URL', '')
DEEPSEEK_FLASH_MODEL = os.getenv('DEEPSEEK_FLASH_MODEL', '')
http_client = None
llm_semaphore = asyncio.Semaphore(4)
LLM_TIMEOUT = httpx.Timeout(180.0, connect=20.0)
LLM_LIMITS = httpx.Limits(max_connections=10, max_keepalive_connections=5)
