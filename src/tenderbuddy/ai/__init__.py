"""AI layer: OpenAI-compatible LLM client with a deterministic offline mode."""

from tenderbuddy.ai.features import AIResult, draft_bid, extract_requirements, summarize_tender
from tenderbuddy.ai.llm import LLMClient, LLMError, MockLLM, OpenAICompatibleLLM, get_llm

__all__ = [
    "AIResult",
    "LLMClient",
    "LLMError",
    "MockLLM",
    "OpenAICompatibleLLM",
    "draft_bid",
    "extract_requirements",
    "get_llm",
    "summarize_tender",
]
