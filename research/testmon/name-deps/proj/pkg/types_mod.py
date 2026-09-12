"""Type aliases: old-style Annotated and PEP 695 `type` statement."""
from typing import Annotated

from pydantic import Field

UserId = Annotated[int, Field(gt=0)]

type Score = Annotated[float, Field(ge=0, le=100)]
