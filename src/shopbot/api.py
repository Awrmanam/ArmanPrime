from fastapi import FastAPI

app = FastAPI(title="Telegram commerce core", docs_url=None, redoc_url=None)


@app.get("/health/live")
async def live() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/health/ready")
async def ready() -> dict[str, str]:
    return {"status": "ready"}
