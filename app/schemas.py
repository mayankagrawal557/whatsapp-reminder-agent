"""
Pydantic schemas and data validation models for API requests and responses.
Includes schemas for Reminders, AI Live Preview, Settings, Clients, and Health.
"""
from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional
from pydantic import BaseModel, Field, field_validator
import re

ToneType = Literal["friendly", "professional", "formal", "urgent", "empathetic", "casual"]
ReminderStatusType = Literal[
    "PENDING",
    "PROCESSING",
    "SENT",
    "CONFIRMED",
    "NEEDS_RESCHEDULE",
    "CANCELLED",
    "FAILED",
]


def normalize_phone_number(phone: str, default_country_code: str = "") -> str:
    """
    Sanitize phone number to E.164-compatible format.
    Removes whitespace, hyphens, parentheses, etc.
    """
    cleaned = re.sub(r"[^\d+]", "", phone.strip())
    if not cleaned:
        raise ValueError("Phone number cannot be empty.")
    if not cleaned.startswith("+"):
        if default_country_code:
            code = default_country_code.strip()
            if not code.startswith("+"):
                code = "+" + code
            cleaned = f"{code}{cleaned}"
        else:
            cleaned = f"+{cleaned}"
    return cleaned


class ClientCreate(BaseModel):
    id: str = Field(..., description="Unique client identifier (e.g. clinic_alpha, salon_101)")
    name: str = Field(..., description="Business display name (e.g. Sunset Dental Care)")
    business_type: str = Field(default="Clinic / Service", description="Type of business (e.g., Dental Clinic, Hair Salon)")
    default_tone: str = Field(default="friendly", description="Default reminder phrasing tone")
    phone_number_id: Optional[str] = Field(None, description="Dedicated WhatsApp Phone Number ID (optional)")
    whatsapp_token: Optional[str] = Field(None, description="Dedicated WhatsApp Access Token (optional)")
    api_key: Optional[str] = Field(None, description="API key to protect client-specific endpoints")


class ClientResponse(BaseModel):
    id: str
    name: str
    business_type: str
    default_tone: str
    phone_number_id: Optional[str] = None
    is_active: bool
    created_at: datetime
    total_reminders: Optional[int] = 0

    class Config:
        from_attributes = True


class ReminderCreate(BaseModel):
    customer_name: str = Field(..., min_length=1, max_length=128, description="Customer or patient full name")
    phone: str = Field(..., description="Recipient phone number with country code (e.g. +14155552671)")
    service_name: str = Field(..., min_length=1, max_length=128, description="Name of scheduled service or consultation")
    appointment_datetime: datetime = Field(..., description="ISO 8601 appointment datetime")
    reminder_time: Optional[datetime] = Field(
        None,
        description="Optional exact datetime to dispatch reminder. If null, computed using lead_hours."
    )
    lead_hours: Optional[int] = Field(
        24,
        ge=0,
        description="Hours before appointment to trigger reminder (used if reminder_time is not provided)"
    )
    notes: Optional[str] = Field(None, max_length=1000, description="Special instructions or prep notes for the customer")
    tone: Optional[str] = Field(None, description="Tone override: friendly, professional, formal, urgent, etc.")
    client_id: Optional[str] = Field(None, description="Tenant ID. Falls back to default tenant if omitted.")
    user_id: Optional[str] = Field(None, description="User/Clinic ID")

    @field_validator("phone")
    @classmethod
    def validate_phone(cls, v: str) -> str:
        return normalize_phone_number(v)

    @field_validator("appointment_datetime", mode="before")
    @classmethod
    def parse_appointment_datetime(cls, v: Any) -> datetime:
        if isinstance(v, datetime):
            if v.tzinfo is None:
                return v.replace(tzinfo=timezone.utc)
            return v
        if isinstance(v, str):
            try:
                dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    return dt.replace(tzinfo=timezone.utc)
                return dt
            except ValueError:
                for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%d/%m/%Y %H:%M", "%m/%d/%Y %H:%M"):
                    try:
                        return datetime.strptime(v, fmt).replace(tzinfo=timezone.utc)
                    except ValueError:
                        continue
                raise ValueError(f"Could not parse appointment datetime: '{v}'. Use YYYY-MM-DD HH:MM or ISO 8601.")
        raise ValueError("Invalid datetime format")


class BulkUploadRowError(BaseModel):
    row_number: int
    data: Dict[str, Any]
    error: str


class BulkUploadResponse(BaseModel):
    total_received: int
    inserted: int
    failed: int
    errors: List[BulkUploadRowError] = []
    reminder_ids: List[str] = []


class ReminderResponse(BaseModel):
    id: str
    user_id: Optional[str] = None
    client_id: Optional[str] = None
    customer_name: str
    phone: str
    service_name: str
    appointment_datetime: datetime
    reminder_time: datetime
    notes: Optional[str] = None
    tone: Optional[str] = None
    status: str
    customer_reply: Optional[str] = None
    replied_at: Optional[datetime] = None
    generated_message: Optional[str] = None
    whatsapp_message_id: Optional[str] = None
    attempts: int
    error_log: Optional[str] = None
    sent_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class AIPreviewRequest(BaseModel):
    customer_name: str = Field("Sarah Jenkins", description="Target customer name")
    service_name: str = Field("Teeth Whitening", description="Service or consultation name")
    appointment_datetime: Optional[datetime] = Field(None, description="Appointment date & time")
    tone: Optional[str] = Field("friendly", description="Selected message tone")
    notes: Optional[str] = Field(None, description="Optional preparation notes")
    business_name: Optional[str] = Field(None, description="Business name")
    business_type: Optional[str] = Field(None, description="Business industry or category")


class AIPreviewResponse(BaseModel):
    message: str
    tone: str
    customer_name: str
    formatted_time: str


class SettingsPayload(BaseModel):
    meta_phone_number_id: Optional[str] = None
    meta_whatsapp_token: Optional[str] = None
    gemini_api_key: Optional[str] = None
    gemini_model: Optional[str] = "gemini-1.5-flash"
    business_name: Optional[str] = "Appointments Desk"
    business_type: Optional[str] = "Clinic / Service"
    default_tone: Optional[str] = "friendly"
    whatsapp_gateway_type: Optional[str] = "meta"


class SettingsResponse(BaseModel):
    meta_phone_number_id: str = ""
    meta_whatsapp_token_masked: str = ""
    gemini_api_key_masked: str = ""
    gemini_model: str = "gemini-1.5-flash"
    business_name: str = "Appointments Desk"
    business_type: str = "Clinic / Service"
    default_tone: str = "friendly"
    whatsapp_gateway_type: str = "meta"
    is_meta_configured: bool = False
    is_gemini_configured: bool = False


class HealthResponse(BaseModel):
    status: str
    app_name: str
    version: str
    database_connected: bool
    scheduler_running: bool
    gateway_type: str
    gemini_configured: bool
    stats: Dict[str, int]
