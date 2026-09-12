from fastapi import APIRouter

router = APIRouter()


@router.get("/")
def list_items():
    return {"items": []}


@router.get("/{item_id:int}")
def get_item(item_id: int):
    return {"item_id": item_id}


@router.post("/")
def create_item():
    return {"created": True}
