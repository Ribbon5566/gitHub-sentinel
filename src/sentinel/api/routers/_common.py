from __future__ import annotations

from collections.abc import Sequence
from typing import TypeVar

from pydantic import BaseModel

from ..schemas import PageOut

T = TypeVar("T", bound=BaseModel)


def to_page(
    items: Sequence[object], total: int, limit: int, offset: int, schema: type[T]
) -> PageOut[T]:
    return PageOut[schema](  # type: ignore[index]
        items=[schema.model_validate(item) for item in items],
        total=total,
        limit=limit,
        offset=offset,
    )
