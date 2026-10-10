"""
Root entrypoint for running the application.
Re-exports `app` from `app.main` so that both:
    uvicorn main:app --reload
and
    uvicorn app.main:app --reload
work interchangeably and seamlessly.
"""
from app.main import app

if __name__ == "__main__":
    import uvicorn
    from app.config import settings
    uvicorn.run(
        "main:app",
        host=settings.HOST,
        port=settings.PORT,
        reload=True,
    )
