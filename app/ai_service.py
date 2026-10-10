"""
Compatibility alias module for AI personalization service.
Points to app.services.gemini_service.
"""
from app.services.gemini_service import (
    GeminiService,
    gemini_service,
)

# Backward-compatibility alias
ai_agent = gemini_service
MessageCustomizationAgent = GeminiService

__all__ = ["GeminiService", "gemini_service", "ai_agent", "MessageCustomizationAgent"]
