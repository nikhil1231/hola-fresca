from fastapi import FastAPI

app = FastAPI(title="HolaFresca")


@app.get("/api/health")
def health_check() -> dict[str, str]:
    return {"status": "ok", "service": "HolaFresca"}
