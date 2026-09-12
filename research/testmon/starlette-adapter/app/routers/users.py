from fastapi import APIRouter

from .items import router as items_router

router = APIRouter(prefix="/users")


@router.get("/{id:int}")
def get_user(id: int):
    return {"id": id}


@router.get("/me")
def get_me():
    return {"id": "me"}


# nested router included with its own prefix (which itself contains a path
# param) -- exercises multi-level prefix composition
router.include_router(items_router, prefix="/{user_id}/items", tags=["items"])
