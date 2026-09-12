from fastapi import FastAPI

subapp = FastAPI()


@subapp.get("/ping")
def ping():
    return {"ping": "pong"}
