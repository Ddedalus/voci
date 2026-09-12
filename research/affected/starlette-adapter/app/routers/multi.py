from fastapi import APIRouter

router = APIRouter()


@router.api_route("/multi", methods=["GET", "POST"])
def multi_handler():
    return {"ok": True}


def manual_handler():
    return {"manual": True}


# add_api_route call, not a decorator
router.add_api_route("/manual", manual_handler, methods=["GET"])
