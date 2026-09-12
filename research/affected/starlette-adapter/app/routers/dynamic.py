from fastapi import APIRouter

router = APIRouter()

_segment = "".join(["from", "-", "variable"])
DYNAMIC_PATH = "/" + _segment  # not a literal string at the decorator call site


@router.get(DYNAMIC_PATH)
def dyn():
    return {"dynamic": True}
