"""
Compatibility alias module for WhatsApp gateway service.
Points to app.services.whatsapp_service.
"""
from app.services.whatsapp_service import (
    BaseWhatsAppGateway,
    MetaCloudWhatsAppGateway,
    MockWhatsAppGateway,
    WhatsAppSendResult,
    WhatsAppWebGateway,
    get_whatsapp_gateway,
    whatsapp_gateway,
)

__all__ = [
    "BaseWhatsAppGateway",
    "MetaCloudWhatsAppGateway",
    "MockWhatsAppGateway",
    "WhatsAppSendResult",
    "WhatsAppWebGateway",
    "get_whatsapp_gateway",
    "whatsapp_gateway",
]
