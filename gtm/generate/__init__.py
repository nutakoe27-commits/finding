"""Письма: четвёртое звено конвейера.

Наружу нужны стадия, генерация одного письма и проверка результата —
плюс клиенты модели, чтобы вызывающий мог подсунуть шаблонный в dry-run.
"""

from gtm.generate.letter import (
    STAGE_NAME,
    Letter,
    LetterParseError,
    generate_letter,
    parse_letter,
    render_prompt,
    run_generate,
    validate_letter,
)
from gtm.generate.llm import (
    AnthropicClient,
    LLMClient,
    LLMError,
    LLMPermanentError,
    TemplateClient,
    estimate_cost_rub,
    get_client,
)

__all__ = [
    "STAGE_NAME",
    "AnthropicClient",
    "LLMClient",
    "LLMError",
    "LLMPermanentError",
    "Letter",
    "LetterParseError",
    "TemplateClient",
    "estimate_cost_rub",
    "generate_letter",
    "get_client",
    "parse_letter",
    "render_prompt",
    "run_generate",
    "validate_letter",
]
