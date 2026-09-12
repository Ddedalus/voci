from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field

router = APIRouter(prefix="/validated")


class Order(BaseModel):
    quantity: int = Field(gt=0)  # edit target: gt=0 -> gt=5


@router.post("/orders")
def create_order(order: Order):
    return {"quantity": order.quantity}


@router.get("/search")
def search(limit: int = 10):  # edit target: type/default of `limit`
    return {"limit": limit}


def require_token(x_token: str = Header(...)) -> str:
    if x_token != "secret":  # edit target: the raising condition
        raise HTTPException(status_code=401, detail="bad token")
    return x_token


@router.get("/protected", dependencies=[Depends(require_token)])
def protected():
    return {"ok": True}
