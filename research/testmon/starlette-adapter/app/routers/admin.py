from fastapi import APIRouter

router = APIRouter(prefix="/admin")
reports_router = APIRouter(prefix="/reports")


@reports_router.get("/summary")
def summary():
    return {"summary": True}


@reports_router.get("/detail/{report_id:int}")
def detail(report_id: int):
    return {"report_id": report_id}


# nested router-in-router, itself prefixed again when included into app
router.include_router(reports_router)
