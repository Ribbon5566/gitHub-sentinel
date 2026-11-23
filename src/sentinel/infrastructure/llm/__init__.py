"""LLM 适配器。"""

from .mock import MockLLM
from .openai_compat import OpenAICompatLLM
from .parsing import parse_summary
from .precompress import render_compact

__all__ = ["MockLLM", "OpenAICompatLLM", "parse_summary", "render_compact"]
