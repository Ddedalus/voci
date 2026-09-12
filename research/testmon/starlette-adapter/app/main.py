from fastapi import FastAPI

from .routers import admin, dynamic, legacy, multi, users, validated
from .routers import ws as ws_router
from .sub import subapp

app = FastAPI()

app.include_router(users.router)
app.include_router(legacy.router)
app.include_router(admin.router, prefix="/api")
app.include_router(multi.router)
app.include_router(ws_router.router)
app.include_router(dynamic.router)
app.include_router(validated.router)

app.mount("/sub", subapp)


@app.get("/")
def root():
    return {"hello": "world"}


@app.get("/items", tags=["items"])
def top_items():
    return {"top": True}
