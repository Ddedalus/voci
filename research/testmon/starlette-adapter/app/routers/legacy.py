from fastapi import APIRouter

router = APIRouter(prefix="/legacy")


# registered first: a catch-all path param that will shadow /legacy/health
# below, since Starlette matches routes in registration order.
@router.get("/{name}")
def catch_all(name: str):
    return {"name": name}


@router.get("/health")
def health():
    return {"status": "ok"}
