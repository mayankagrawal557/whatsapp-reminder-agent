"""
FastAPI Application Entry Point.
Coordinates database initialization, APScheduler background loops,
Jinja2/HTML template rendering for the Web Dashboard, static assets, and REST routes.
"""
import logging
import os
from datetime import datetime, timezone
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import Reminder, get_all_settings_dict, get_db, init_db
from app.routes import auth, clients, reminders, settings as settings_route, webhooks
from app.schemas import HealthResponse
from app.scheduler import is_scheduler_running, start_scheduler, stop_scheduler

# Configure root logger
logging.basicConfig(
    level=logging.DEBUG if settings.DEBUG else logging.INFO,
    format="%(asctime)s [%(levelname)s] [%(name)s]: %(message)s",
)
logger = logging.getLogger("whatsapp_ai.main")

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Application lifecycle manager.
    Initializes SQLite tables and starts background APScheduler on startup.
    Gracefully shuts down scheduler on termination.
    """
    logger.info("Initializing SQLite database tables and default tenant...")
    await init_db()

    logger.info("Starting APScheduler background dispatch worker...")
    start_scheduler()

    yield

    logger.info("Shutting down APScheduler...")
    stop_scheduler()


app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    description=(
        "Production-ready, sellable WhatsApp AI Reminder Automation SaaS. "
        "Powered by FastAPI, Tailwind CSS, SQLite, APScheduler, and Google Gemini API."
    ),
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)

# Enable CORS for cross-origin client dashboard integrations
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Ensure static and template directories exist
STATIC_DIR.mkdir(parents=True, exist_ok=True)
TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)

# Mount static files directory
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# Mount Route Handlers
app.include_router(auth.router)
app.include_router(reminders.router)
app.include_router(settings_route.router)
app.include_router(clients.router)
app.include_router(webhooks.router)


@app.get("/", response_class=HTMLResponse, tags=["Dashboard"], summary="Serve Web Dashboard")
async def serve_dashboard():
    """
    Serve the clean, modern, responsive single-page Web Dashboard.
    """
    index_file = TEMPLATES_DIR / "index.html"
    if index_file.exists():
        return FileResponse(index_file, media_type="text/html")
    return HTMLResponse(
        content="<h1>Dashboard index.html not found. Please ensure app/templates/index.html is created.</h1>",
        status_code=404,
    )


@app.get(
    "/health",
    tags=["System"],
    summary="Lightweight UptimeRobot ping & keep-alive monitor",
)
async def health_check():
    """
    Lightweight keep-alive health check returning HTTP 200 OK.
    Designed for uptime monitors (e.g. UptimeRobot) pinging every 10 minutes
    to prevent Render free instances from going to sleep.
    """
    return {
        "status": "ok",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "app_name": settings.APP_NAME,
        "version": settings.APP_VERSION,
        "scheduler_running": is_scheduler_running(),
    }


@app.get(
    "/health/full",
    response_model=HealthResponse,
    tags=["System"],
    summary="Comprehensive system health with DB diagnostics",
)
async def full_health_check(db: AsyncSession = Depends(get_db)):
    """
    Comprehensive health check verifying database connection, scheduler loop,
    Gemini LLM configuration, and aggregate reminder counts.
    """
    db_connected = False
    stats = {"pending": 0, "sent": 0, "failed": 0, "processing": 0, "total": 0}

    try:
        stmt = select(Reminder.status, func.count(Reminder.id)).group_by(Reminder.status)
        res = await db.execute(stmt)
        for row_status, count in res.all():
            key = row_status.lower()
            if key in stats:
                stats[key] = count
            stats["total"] += count
        db_connected = True
    except Exception as exc:
        logger.error(f"Database health check failed: {exc}")
        db_connected = False

    stored = await get_all_settings_dict(db)
    active_gateway = stored.get("whatsapp_gateway_type") or settings.WHATSAPP_GATEWAY_TYPE
    has_gemini = bool(stored.get("gemini_api_key") or settings.GEMINI_API_KEY)

    return HealthResponse(
        status="healthy" if db_connected else "degraded",
        app_name=settings.APP_NAME,
        version=settings.APP_VERSION,
        database_connected=db_connected,
        scheduler_running=is_scheduler_running(),
        gateway_type=active_gateway,
        gemini_configured=has_gemini,
        stats=stats,
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "app.main:app",
        host=settings.HOST,
        port=settings.PORT,
        reload=settings.DEBUG,
    )
