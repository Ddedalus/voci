"""Pydantic v2 models exercising: type-alias fields, Field constraints,
model_config, a nested model referenced as a type, a string forward
reference, and a @field_validator (a def, traced separately)."""
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .types_mod import Score, UserId


class Address(BaseModel):
    model_config = ConfigDict(frozen=True)

    city: str
    zip_code: str = Field(min_length=3)


class Manager(BaseModel):
    level: int = 1


class User(BaseModel):
    id: UserId
    score: Score
    address: Address
    manager: "Manager | None" = None  # forward reference by string

    @field_validator("score")
    @classmethod
    def check_score(cls, v):
        return v


def make_default_user() -> User:
    return User(id=1, score=0.0, address=Address(city="x", zip_code="000"))
