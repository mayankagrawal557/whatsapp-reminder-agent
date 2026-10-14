"""
Database engine, models, and session management using SQLAlchemy async with SQLite.
Includes multi-tenant schema with User/Clinic, AppSetting, Reminder, and MessageAuditLog.
"""
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncGenerator, Dict, Optional

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    event,
    select,
    text,
)
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import declarative_base, relationship

from app.config import settings

Base = declarative_base()


class User(Base):
    """
    User/Clinic tenant model.
    Represents an onboarded clinic, salon, or consultant with isolated appointments and credentials.
    """
    __tablename__ = "users"

    id = Column(String(64), primary_key=True, index=True)
    business_name = Column(String(128), nullable=False)
    email = Column(String(128), unique=True, index=True, nullable=False)
    password_hash = Column(String(255), nullable=False)
    business_type = Column(String(64), nullable=False, default="PET-CT Diagnostic Imaging Centre")
    phone = Column(String(32), nullable=True)
    logo_url = Column(String(512), nullable=True)
    brand_color = Column(String(32), nullable=True, default="#0d9488")
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)

    reminders = relationship("Reminder", back_populates="user", cascade="all, delete-orphan")
    settings = relationship("AppSetting", back_populates="user", cascade="all, delete-orphan")
    doctors = relationship("DoctorContact", back_populates="user", cascade="all, delete-orphan")
    campaigns = relationship("BroadcastCampaign", back_populates="user", cascade="all, delete-orphan")


class AppSetting(Base):
    """
    Dynamic configuration key-value storage isolated per User/Clinic.
    Enables non-technical clinics to manage WhatsApp credentials and Gemini keys independently.
    """
    __tablename__ = "app_settings"

    id = Column(String(128), primary_key=True, index=True)
    user_id = Column(String(64), ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True)
    key = Column(String(64), index=True, nullable=False)
    value = Column(Text, nullable=True)
    description = Column(String(255), nullable=True)
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    user = relationship("User", back_populates="settings")


class Client(Base):
    """
    Legacy client model kept for backward-compatibility with existing tests.
    """
    __tablename__ = "clients"

    id = Column(String(64), primary_key=True, index=True)
    name = Column(String(128), nullable=False)
    business_type = Column(String(64), nullable=False, default="Service Provider")
    default_tone = Column(String(32), nullable=False, default="friendly")
    phone_number_id = Column(String(128), nullable=True)
    whatsapp_token = Column(String(512), nullable=True)
    api_key = Column(String(128), nullable=True, unique=True)
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)


class Reminder(Base):
    """
    Individual customer appointment reminder record with two-way reply tracking.
    """
    __tablename__ = "reminders"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()), index=True)
    user_id = Column(String(64), ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True)
    client_id = Column(String(64), nullable=True, index=True)
    customer_name = Column(String(128), nullable=False)
    phone = Column(String(32), nullable=False, index=True)
    service_name = Column(String(128), nullable=False)
    appointment_datetime = Column(DateTime(timezone=True), nullable=False, index=True)
    reminder_time = Column(DateTime(timezone=True), nullable=False, index=True)
    notes = Column(Text, nullable=True)
    tone = Column(String(32), nullable=True)
    status = Column(String(32), default="PENDING", index=True, nullable=False)
    # Two-way reply tracking
    customer_reply = Column(Text, nullable=True)
    replied_at = Column(DateTime(timezone=True), nullable=True)
    # Dispatch tracking
    generated_message = Column(Text, nullable=True)
    whatsapp_message_id = Column(String(128), nullable=True)
    attempts = Column(Integer, default=0, nullable=False)
    max_retries = Column(Integer, default=3, nullable=False)
    error_log = Column(Text, nullable=True)
    sent_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    user = relationship("User", back_populates="reminders")


class MessageAuditLog(Base):
    """
    Audit log for sent messages, delivery receipts, and inbound WhatsApp webhook callbacks.
    """
    __tablename__ = "message_audit_logs"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String(64), nullable=True, index=True)
    client_id = Column(String(64), nullable=True, index=True)
    reminder_id = Column(String(36), nullable=True, index=True)
    direction = Column(String(16), nullable=False)  # OUTBOUND or INBOUND
    status = Column(String(32), nullable=True)
    payload = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)


class DoctorContact(Base):
    """
    Referring Doctor Directory Contact.
    Stores medical oncologists, surgeons, and physicians for Arovida PET-CT broadcasts.
    """
    __tablename__ = "doctor_contacts"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()), index=True)
    user_id = Column(String(64), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    doctor_name = Column(String(128), nullable=False, index=True)
    phone_number = Column(String(32), nullable=False, index=True)
    specialty = Column(String(128), nullable=True)  # e.g., Medical Oncologist, Surgical Oncologist
    hospital = Column(String(128), nullable=True)   # e.g., Apex Hospital, Apollo, Max Healthcare
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)

    user = relationship("User", back_populates="doctors")


class BroadcastCampaign(Base):
    """
    Doctor Broadcast Campaign model.
    Supports 1-click blasts or recurring schedules (every Monday, 1st & 16th, etc.).
    """
    __tablename__ = "broadcast_campaigns"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()), index=True)
    user_id = Column(String(64), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    campaign_title = Column(String(255), nullable=False)
    template_message = Column(Text, nullable=False)  # dynamic tags: {doctor_name}, {specialty}, {hospital}, {centre_name}
    schedule_type = Column(String(32), default="ONE_TIME", nullable=False)  # "ONE_TIME" or "RECURRING"
    cron_schedule = Column(String(64), nullable=True, default="immediate")  # "immediate", "every_monday", "dates_1_and_16", "every_friday", "daily"
    scheduled_time = Column(String(16), default="10:00", nullable=False)
    next_run_at = Column(DateTime(timezone=True), nullable=True, index=True)
    last_run_at = Column(DateTime(timezone=True), nullable=True)
    status = Column(String(32), default="ACTIVE", index=True, nullable=False)  # "DRAFT", "ACTIVE", "PAUSED", "COMPLETED"
    total_recipients = Column(Integer, default=0, nullable=False)
    total_sent = Column(Integer, default=0, nullable=False)
    total_failed = Column(Integer, default=0, nullable=False)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    user = relationship("User", back_populates="campaigns")
    logs = relationship("BroadcastLog", back_populates="campaign", cascade="all, delete-orphan")


class BroadcastLog(Base):
    """
    Detailed dispatch log entry per recipient doctor for campaign execution audits.
    """
    __tablename__ = "broadcast_logs"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()), index=True)
    user_id = Column(String(64), index=True, nullable=False)
    campaign_id = Column(String(36), ForeignKey("broadcast_campaigns.id", ondelete="CASCADE"), nullable=True, index=True)
    doctor_id = Column(String(36), index=True, nullable=True)
    doctor_name = Column(String(128), nullable=False)
    phone_number = Column(String(32), nullable=False)
    message_snippet = Column(Text, nullable=True)
    sent_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
    status = Column(String(32), default="SENT", nullable=False)  # "SENT" or "FAILED"
    error_message = Column(Text, nullable=True)
    whatsapp_message_id = Column(String(128), nullable=True)

    campaign = relationship("BroadcastCampaign", back_populates="logs")


# Resolve database URL and ensure parent directory exists for SQLite
def _resolve_and_prepare_db_url(url: str) -> str:
    if "sqlite" in url:
        parts = url.split("///")
        if len(parts) > 1 and parts[-1] and parts[-1] != ":memory:":
            db_file_path = Path(parts[-1]).resolve()
            db_file_path.parent.mkdir(parents=True, exist_ok=True)
    return url

prepared_db_url = _resolve_and_prepare_db_url(settings.DATABASE_URL)

# Initialize SQLite engine with async driver
engine = create_async_engine(
    prepared_db_url,
    echo=settings.DEBUG,
    connect_args={"check_same_thread": False} if "sqlite" in prepared_db_url else {},
)


@event.listens_for(engine.sync_engine, "connect")
def set_sqlite_pragma(dbapi_connection, connection_record):
    if "sqlite" in prepared_db_url:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL;")
        cursor.execute("PRAGMA synchronous=NORMAL;")
        cursor.execute("PRAGMA busy_timeout=5000;")
        cursor.close()


AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Dependency injection helper for FastAPI routes to obtain an async DB session."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()


async def get_user_setting(session: AsyncSession, user_id: str, key: str, default: Optional[str] = None) -> Optional[str]:
    """Retrieve an application setting value for a specific user, with global fallback."""
    setting_id = f"{user_id}:{key}"
    stmt = select(AppSetting).where(AppSetting.id == setting_id)
    res = await session.execute(stmt)
    setting = res.scalar_one_or_none()
    if setting and setting.value is not None:
        return setting.value

    # Fallback to global setting (without user_id prefix)
    global_stmt = select(AppSetting).where(AppSetting.key == key, AppSetting.user_id.is_(None))
    g_res = await session.execute(global_stmt)
    g_setting = g_res.scalar_one_or_none()
    if g_setting and g_setting.value is not None:
        return g_setting.value

    return default


async def set_user_setting(session: AsyncSession, user_id: str, key: str, value: Optional[str], description: Optional[str] = None) -> None:
    """Upsert an application setting for a specific user."""
    setting_id = f"{user_id}:{key}"
    stmt = select(AppSetting).where(AppSetting.id == setting_id)
    res = await session.execute(stmt)
    setting = res.scalar_one_or_none()
    if setting:
        setting.value = value
        if description:
            setting.description = description
    else:
        setting = AppSetting(id=setting_id, user_id=user_id, key=key, value=value, description=description)
        session.add(setting)
    await session.commit()


async def get_user_settings_dict(session: AsyncSession, user_id: str) -> Dict[str, str]:
    """Retrieve all stored application settings for a specific user as a dictionary."""
    stmt = select(AppSetting).where((AppSetting.user_id == user_id) | (AppSetting.user_id.is_(None)))
    res = await session.execute(stmt)
    records = res.scalars().all()
    # User-specific settings override global ones
    result = {}
    for rec in records:
        if rec.user_id is None and rec.key not in result:
            result[rec.key] = rec.value or ""
        elif rec.user_id == user_id:
            result[rec.key] = rec.value or ""
    return result


# Compatibility helpers for global settings
async def get_all_settings_dict(session: AsyncSession) -> Dict[str, str]:
    stmt = select(AppSetting)
    res = await session.execute(stmt)
    records = res.scalars().all()
    return {rec.key: rec.value for rec in records if rec.value is not None}


async def set_setting(session: AsyncSession, key: str, value: Optional[str], description: Optional[str] = None) -> None:
    stmt = select(AppSetting).where(AppSetting.id == key)
    res = await session.execute(stmt)
    setting = res.scalar_one_or_none()
    if setting:
        setting.value = value
    else:
        setting = AppSetting(id=key, user_id=None, key=key, value=value, description=description)
        session.add(setting)
    await session.commit()


async def init_db() -> None:
    """
    Create tables, apply safe migrations for existing SQLite databases,
    and seed default demo clinic user account.
    """
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

        # Safe schema migration for SQLite if existing database lacks new columns
        # Check reminders table columns
        reminders_info = await conn.execute(text("PRAGMA table_info(reminders);"))
        rem_cols = {row[1] for row in reminders_info.fetchall()}
        if "user_id" not in rem_cols:
            await conn.execute(text("ALTER TABLE reminders ADD COLUMN user_id VARCHAR(64);"))
        if "customer_reply" not in rem_cols:
            await conn.execute(text("ALTER TABLE reminders ADD COLUMN customer_reply TEXT;"))
        if "replied_at" not in rem_cols:
            await conn.execute(text("ALTER TABLE reminders ADD COLUMN replied_at DATETIME;"))

        # Check app_settings table columns - recreate if old table had key as primary key without id
        settings_info = await conn.execute(text("PRAGMA table_info(app_settings);"))
        set_cols = {row[1] for row in settings_info.fetchall()}
        if "id" not in set_cols and len(set_cols) > 0:
            await conn.execute(text("DROP TABLE IF EXISTS app_settings;"))
            await conn.run_sync(Base.metadata.create_all)

        # Check message_audit_logs table columns
        audit_info = await conn.execute(text("PRAGMA table_info(message_audit_logs);"))
        audit_cols = {row[1] for row in audit_info.fetchall()}
        if "user_id" not in audit_cols and len(audit_cols) > 0:
            await conn.execute(text("ALTER TABLE message_audit_logs ADD COLUMN user_id VARCHAR(64);"))

        # Check users table columns for white-label branding
        users_info = await conn.execute(text("PRAGMA table_info(users);"))
        user_cols = {row[1] for row in users_info.fetchall()}
        if "logo_url" not in user_cols and len(user_cols) > 0:
            await conn.execute(text("ALTER TABLE users ADD COLUMN logo_url VARCHAR(512);"))
        if "brand_color" not in user_cols and len(user_cols) > 0:
            await conn.execute(text("ALTER TABLE users ADD COLUMN brand_color VARCHAR(32) DEFAULT '#0d9488';"))

    # Seed default clinic user tenant & sample doctor contacts
    async with AsyncSessionLocal() as session:
        # Avoid circular import by hashing locally with bcrypt
        import bcrypt
        demo_user_id = "demo_clinic"
        stmt = select(User).where(User.id == demo_user_id)
        result = await session.execute(stmt)
        demo_user = result.scalar_one_or_none()

        if not demo_user:
            email_stmt = select(User).where(User.email == "demo@clinic.com")
            email_res = await session.execute(email_stmt)
            demo_user = email_res.scalar_one_or_none()

        if not demo_user:
            hashed_pw = bcrypt.hashpw("demo123".encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
            demo_user = User(
                id=demo_user_id,
                business_name="Arovida PET-CT Centre",
                email="demo@clinic.com",
                password_hash=hashed_pw,
                business_type="PET-CT Diagnostic Imaging Centre",
                brand_color="#0d9488",
                is_active=True,
            )
            session.add(demo_user)
            await session.commit()
        else:
            demo_user.business_name = "Arovida PET-CT Centre"
            demo_user.business_type = "PET-CT Diagnostic Imaging Centre"
            demo_user.brand_color = "#0d9488"
            await session.commit()

        # Update any legacy clinic names across all tables
        await session.execute(
            text("UPDATE users SET business_name = 'Arovida PET-CT Centre' WHERE business_name LIKE '%Apex%' OR business_name = 'Arovida Clinic';")
        )
        await session.execute(
            text("UPDATE app_settings SET value = 'Arovida PET-CT Centre' WHERE key = 'business_name' AND (value LIKE '%Apex%' OR value = 'Arovida Clinic');")
        )
        await session.commit()

        # Seed sample referring doctors if table is empty
        doc_count_res = await session.execute(select(DoctorContact).where(DoctorContact.user_id == demo_user.id))
        existing_docs = doc_count_res.scalars().all()
        if not existing_docs:
            sample_doctors = [
                DoctorContact(
                    id=str(uuid.uuid4()),
                    user_id=demo_user.id,
                    doctor_name="Dr. Rajesh Sharma",
                    phone_number="+919876543210",
                    specialty="Medical Oncology",
                    hospital="Tata Memorial Hospital",
                    is_active=True,
                ),
                DoctorContact(
                    id=str(uuid.uuid4()),
                    user_id=demo_user.id,
                    doctor_name="Dr. Ananya Iyer",
                    phone_number="+919812345678",
                    specialty="Surgical Oncology",
                    hospital="Apollo Cancer Centre",
                    is_active=True,
                ),
                DoctorContact(
                    id=str(uuid.uuid4()),
                    user_id=demo_user.id,
                    doctor_name="Dr. Vikram Malhotra",
                    phone_number="+919988776655",
                    specialty="Pulmonology & Chest Medicine",
                    hospital="Fortis Memorial Research Institute",
                    is_active=True,
                ),
                DoctorContact(
                    id=str(uuid.uuid4()),
                    user_id=demo_user.id,
                    doctor_name="Dr. Sneha Patel",
                    phone_number="+919765432109",
                    specialty="Radiation Oncology",
                    hospital="Max Super Speciality Hospital",
                    is_active=True,
                ),
                DoctorContact(
                    id=str(uuid.uuid4()),
                    user_id=demo_user.id,
                    doctor_name="Dr. Amit Verma",
                    phone_number="+919823456789",
                    specialty="Internal Medicine & Oncology",
                    hospital="City Care Hospital",
                    is_active=True,
                ),
                DoctorContact(
                    id=str(uuid.uuid4()),
                    user_id=demo_user.id,
                    doctor_name="Dr. Meera Nambiar",
                    phone_number="+919834567890",
                    specialty="Hemato-Oncology",
                    hospital="AIIMS Oncology Unit",
                    is_active=True,
                ),
            ]
            session.add_all(sample_doctors)
            await session.commit()

        # Seed sample Monday campaign if none exists
        camp_res = await session.execute(select(BroadcastCampaign).where(BroadcastCampaign.user_id == demo_user.id))
        existing_camps = camp_res.scalars().all()
        if not existing_camps:
            sample_campaign = BroadcastCampaign(
                id=str(uuid.uuid4()),
                user_id=demo_user.id,
                campaign_title="Monday PET-CT Slots Update",
                template_message=(
                    "Respected {doctor_name},\n\n"
                    "Greetings from {centre_name}.\n\n"
                    "This is our weekly update on prioritized PET-CT scanning slots available for your patients at {hospital}:\n\n"
                    "• Same-Day Whole Body 18F-FDG PET-CT with guaranteed 6-Hour reporting turnaround.\n"
                    "• High-resolution 68Ga-PSMA PET-CT (Prostate) & 68Ga-DOTATATE (Neuroendocrine) appointments open.\n"
                    "• Patient transport assistance & insurance cashless pre-authorization available.\n\n"
                    "For direct priority bookings, please contact our Referral Desk at +91 98765 00000.\n\n"
                    "Warm regards,\n"
                    "{centre_name}"
                ),
                schedule_type="RECURRING",
                cron_schedule="every_monday",
                scheduled_time="10:00",
                status="ACTIVE",
                total_recipients=6,
                total_sent=0,
                total_failed=0,
            )
            session.add(sample_campaign)
            await session.commit()
