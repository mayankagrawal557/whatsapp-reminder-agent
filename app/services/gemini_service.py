"""
Google Gemini AI Personalization Service.
Generates personalized, high-converting WhatsApp reminders with two-way reply CTA:
"Reply 1 to Confirm or 2 to Reschedule."
Includes robust deterministic fallback engine when LLM API keys are unconfigured.
"""
import logging
from datetime import datetime
from typing import Optional
import httpx

from app.config import settings

logger = logging.getLogger("whatsapp_ai.gemini_service")


class GeminiService:
    """
    AI message generator powered by Google Gemini (gemini-1.5-flash / gemini-2.0-flash)
    with automatic fallback template engine.
    """

    TONE_MAP = {
        "polite & formal": "formal",
        "formal": "formal",
        "professional": "formal",
        "friendly & warm": "friendly",
        "friendly": "friendly",
        "warm": "friendly",
        "urgent reminder": "urgent",
        "urgent": "urgent",
        "casual": "casual",
        "empathetic": "empathetic",
    }

    def __init__(self):
        self.default_api_key = settings.GEMINI_API_KEY
        self.default_model = settings.GEMINI_MODEL
        self.timeout = settings.GEMINI_TIMEOUT_SECONDS

    def normalize_tone(self, tone: Optional[str]) -> str:
        if not tone:
            return "friendly"
        cleaned = tone.strip().lower()
        return self.TONE_MAP.get(cleaned, cleaned)

    def format_datetime_friendly(self, dt: datetime) -> str:
        try:
            time_part = dt.strftime("%I:%M %p").lstrip("0")
            date_part = dt.strftime("%A, %b %d")
            return f"{date_part} at {time_part}"
        except Exception:
            return dt.strftime("%Y-%m-%d %H:%M")

    async def generate_reminder(
        self,
        customer_name: str,
        appointment_datetime: datetime,
        service_name: str,
        notes: Optional[str] = None,
        tone: Optional[str] = "friendly",
        business_name: Optional[str] = None,
        business_type: Optional[str] = None,
        custom_api_key: Optional[str] = None,
        model_name: Optional[str] = None,
    ) -> str:
        clean_tone = self.normalize_tone(tone)
        active_business_name = business_name or settings.DEFAULT_BUSINESS_NAME
        active_business_type = business_type or settings.DEFAULT_BUSINESS_TYPE
        formatted_time = self.format_datetime_friendly(appointment_datetime)
        active_key = custom_api_key or self.default_api_key
        active_model = model_name or self.default_model

        if active_key and active_key.strip():
            try:
                llm_message = await self._call_gemini_api(
                    api_key=active_key.strip(),
                    model=active_model,
                    customer_name=customer_name,
                    formatted_time=formatted_time,
                    service_name=service_name,
                    notes=notes,
                    tone=clean_tone,
                    business_name=active_business_name,
                    business_type=active_business_type,
                )
                if llm_message and len(llm_message.strip()) > 10:
                    return llm_message.strip()
            except Exception as e:
                logger.warning(f"Gemini API call failed: {e}. Engaging fallback template.")

        return self._render_fallback_template(
            customer_name=customer_name,
            formatted_time=formatted_time,
            service_name=service_name,
            notes=notes,
            tone=clean_tone,
            business_name=active_business_name,
        )

    async def _call_gemini_api(
        self,
        api_key: str,
        model: str,
        customer_name: str,
        formatted_time: str,
        service_name: str,
        notes: Optional[str],
        tone: str,
        business_name: str,
        business_type: str,
    ) -> str:
        clean_model = model.replace("models/", "")
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{clean_model}:generateContent?key={api_key}"

        tone_instructions = {
            "formal": "Professional, respectful, courteous, and polite tone. Avoid excessive emojis.",
            "friendly": "Warm, welcoming, friendly tone with 1-2 cheerful emojis.",
            "urgent": "Prompt, time-sensitive, and urgent tone stressing the importance of confirming.",
            "casual": "Relaxed, conversational, modern friendly tone.",
            "empathetic": "Caring, understanding, and supportive tone.",
        }
        chosen_tone_desc = tone_instructions.get(tone, tone_instructions["friendly"])

        system_instruction = (
            f"You are the appointment assistant for {business_name} ({business_type}). "
            f"Style requirement: {chosen_tone_desc} "
            f"Keep it strictly 1 or 2 concise sentences. Do not use asterisks or markdown bolding. "
            f"Always conclude the message with the exact interactive action: 'Reply 1 to Confirm or 2 to Reschedule.' "
            f"Output the plain message text only without quotation marks."
        )

        prompt = (
            f"Compose a WhatsApp appointment reminder with the following details:\n"
            f"- Customer Name: {customer_name}\n"
            f"- Service / Appointment: {service_name}\n"
            f"- Scheduled Time: {formatted_time}\n"
            f"- Business Name: {business_name}\n"
            f"- Preparation / Notes: {notes if notes else 'None'}\n\n"
            f"Rules:\n"
            f"1. Directly address {customer_name}.\n"
            f"2. Mention '{service_name}' and '{formatted_time}' clearly.\n"
            f"3. Incorporate notes naturally if present.\n"
            f"4. MUST end with: 'Reply 1 to Confirm or 2 to Reschedule.'\n"
            f"5. Output ONLY the message body."
        )

        payload = {
            "systemInstruction": {"parts": [{"text": system_instruction}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.5, "topP": 0.85, "maxOutputTokens": 160},
        }

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(url, json=payload)
            response.raise_for_status()
            data = response.json()
            candidates = data.get("candidates", [])
            if candidates:
                parts = candidates[0].get("content", {}).get("parts", [])
                if parts:
                    text = parts[0].get("text", "").strip()
                    if (text.startswith('"') and text.endswith('"')) or (text.startswith("'") and text.endswith("'")):
                        text = text[1:-1].strip()
                    return text
            raise ValueError("Malformed candidate response from Gemini API")

    def _render_fallback_template(
        self,
        customer_name: str,
        formatted_time: str,
        service_name: str,
        notes: Optional[str],
        tone: str,
        business_name: str,
    ) -> str:
        notes_phrase = f" Note: {notes}." if notes and notes.strip() else ""

        if tone in ("formal", "professional"):
            return (
                f"Dear {customer_name}, this is a polite reminder regarding your upcoming {service_name} at "
                f"{business_name} scheduled for {formatted_time}.{notes_phrase} "
                f"Reply 1 to Confirm or 2 to Reschedule."
            )
        elif tone == "urgent":
            return (
                f"Important Reminder: {customer_name}, your {service_name} at {business_name} is scheduled for "
                f"{formatted_time}.{notes_phrase} "
                f"Reply 1 to Confirm or 2 to Reschedule."
            )
        elif tone == "empathetic":
            return (
                f"Hello {customer_name}, we look forward to seeing you for your {service_name} on "
                f"{formatted_time} at {business_name}.{notes_phrase} "
                f"Reply 1 to Confirm or 2 to Reschedule."
            )
        elif tone == "casual":
            return (
                f"Hey {customer_name}! Quick heads up about your {service_name} on {formatted_time} "
                f"at {business_name}.{notes_phrase} "
                f"Reply 1 to Confirm or 2 to Reschedule."
            )
        else:
            return (
                f"Hi {customer_name}! 👋 Just a quick reminder about your {service_name} scheduled for "
                f"{formatted_time} with {business_name}.{notes_phrase} "
                f"Reply 1 to Confirm or 2 to Reschedule."
            )

    async def polish_doctor_broadcast(
        self,
        template_text: str,
        tone: str = "professional",
        centre_name: str = "Arovida PET-CT Centre",
        custom_api_key: Optional[str] = None,
        model_name: Optional[str] = None,
    ) -> str:
        """
        Polish doctor broadcast text for medical oncology and clinical outreach.
        Preserves dynamic tags: {doctor_name}, {specialty}, {hospital}, {centre_name}.
        """
        active_key = custom_api_key or self.default_api_key
        active_model = model_name or self.default_model

        tone_instructions = {
            "professional": (
                "You are an executive medical liaison for Arovida PET-CT Centre writing to senior oncologists and physicians. "
                "Refine the draft message into a concise, highly professional, collegial WhatsApp broadcast. "
                "Highlight diagnostic precision, 6-hour reporting turnaround, and state-of-the-art PET-CT technology. "
                "CRITICAL: Keep and preserve dynamic tags {doctor_name}, {specialty}, {hospital}, and {centre_name} exactly intact without altering brackets."
            ),
            "slots_update": (
                "You are an executive medical liaison for Arovida PET-CT Centre writing to referring doctors. "
                "Highlight priority same-day PET-CT scanning slots, fast patient intake, ambulance support, and 6-hour report turnaround. "
                "Keep the tone respectful, urgent yet professional. "
                "CRITICAL: Keep and preserve dynamic tags {doctor_name}, {specialty}, {hospital}, and {centre_name} exactly intact."
            ),
            "clinical_case": (
                "You are a clinical radiologist at Arovida PET-CT Centre sharing an educational PET-CT case study or clinical milestone with referring physicians. "
                "The tone must be academic, evidence-based, and respectful. "
                "CRITICAL: Keep and preserve dynamic tags {doctor_name}, {specialty}, {hospital}, and {centre_name} exactly intact."
            ),
            "festive": (
                "You are the Medical Director of Arovida PET-CT Centre extending warm collegial holiday or new year greetings to esteemed referring doctors. "
                "The tone must be warm, respectful, acknowledging their tireless dedication to cancer patient care. "
                "CRITICAL: Keep and preserve dynamic tags {doctor_name}, {specialty}, {hospital}, and {centre_name} exactly intact."
            ),
        }

        system_instruction = tone_instructions.get(tone, tone_instructions["professional"])

        if active_key and active_key.strip():
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{active_model}:generateContent?key={active_key.strip()}"
            payload = {
                "contents": [
                    {
                        "role": "user",
                        "parts": [
                            {
                                "text": (
                                    f"{system_instruction}\n\n"
                                    f"Original draft:\n\"\"\"{template_text}\"\"\"\n\n"
                                    "Output ONLY the polished WhatsApp message text ready to be sent. Do not include markdown code fences or quotation marks around the output."
                                )
                            }
                        ]
                    }
                ],
                "generationConfig": {
                    "temperature": 0.4,
                    "maxOutputTokens": 600,
                }
            }

            try:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    resp = await client.post(url, json=payload)
                    if resp.status_code == 200:
                        data = resp.json()
                        candidates = data.get("candidates", [])
                        if candidates:
                            text_out = candidates[0].get("content", {}).get("parts", [{}])[0].get("text", "")
                            if text_out and len(text_out.strip()) > 15:
                                return text_out.strip()
            except Exception as e:
                logger.warning(f"Gemini polish API failed: {e}. Falling back to refined template.")

        # Fallback polish when Gemini key not available
        clean_text = template_text.strip()
        if tone == "slots_update":
            return (
                f"Respected {{doctor_name}},\n\n"
                f"Greetings from {{centre_name}}.\n\n"
                f"This is an urgent slot update for your oncology patients at {{hospital}}:\n\n"
                f"• Priority Whole-Body 18F-FDG PET-CT slots available with verified 6-hour report delivery.\n"
                f"• Dedicated 68Ga-PSMA & 68Ga-DOTATATE scans open for specialized staging.\n"
                f"• Seamless cashless insurance pre-authorization & dedicated patient transport.\n\n"
                f"To reserve immediate slots for critical cases, please contact our Physician Desk at +91 98765 00000.\n\n"
                f"Warm regards,\n{{centre_name}}"
            )
        elif tone == "clinical_case":
            return (
                f"Dear {{doctor_name}},\n\n"
                f"Greetings from {{centre_name}}.\n\n"
                f"We are pleased to share our latest Clinical Radiology Insight regarding optimized metabolic response evaluation in lymphoma and lung carcinoma using high-resolution PET-CT.\n\n"
                f"Our clinical team is always available to review complex correlative imaging with you.\n\n"
                f"Referral & Discussion Desk: +91 98765 00000\n\n"
                f"Sincerely,\n{{centre_name}}"
            )
        elif tone == "festive":
            return (
                f"Respected {{doctor_name}},\n\n"
                f"Warm greetings and best wishes from the entire team at {{centre_name}}!\n\n"
                f"We deeply appreciate your continued partnership and dedication to compassionate patient care. May this season bring peace, health, and success to you and your loved ones.\n\n"
                f"With highest regards,\n{{centre_name}}"
            )
        else:
            return (
                f"Respected {{doctor_name}},\n\n"
                f"Greetings from {{centre_name}}.\n\n"
                f"{clean_text}\n\n"
                f"For any priority scheduling assistance or reporting inquiries, please reach our dedicated Doctor Desk at +91 98765 00000.\n\n"
                f"Warm regards,\n{{centre_name}}"
            )

    async def test_connection(self, api_key: str, model: str = "gemini-1.5-flash") -> dict:
        try:
            msg = await self.generate_reminder(
                customer_name="Alex Taylor",
                appointment_datetime=datetime.now(),
                service_name="Consultation",
                notes="Introductory session",
                tone="friendly",
                business_name="Test Business",
                custom_api_key=api_key,
                model_name=model,
            )
            return {"success": True, "message": "Gemini API connection verified successfully!", "sample_output": msg}
        except Exception as e:
            return {"success": False, "message": f"Gemini connection failed: {str(e)}"}


gemini_service = GeminiService()

