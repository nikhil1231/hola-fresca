from fastapi import FastAPI

from app.api.recipes import router as recipes_router

app = FastAPI(title="HolaFresca")

app.include_router(recipes_router)


@app.get("/api/health")
def health_check() -> dict[str, str]:
    return {"status": "ok", "service": "HolaFresca"}
