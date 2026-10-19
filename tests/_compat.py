import inspect

import fastapi.dependencies.utils as _dep_utils
import pytest
from fastapi import APIRouter, Depends

HAS_FLAT_DEPENDANT = hasattr(_dep_utils, "get_flat_dependant")

requires_flat_dependant = pytest.mark.skipif(
    not HAS_FLAT_DEPENDANT,
    reason="get_flat_dependant was removed in FastAPI 0.141",
)

HAS_DEPENDENCY_SCOPE = "scope" in inspect.signature(Depends).parameters

requires_dependency_scope = pytest.mark.skipif(
    not HAS_DEPENDENCY_SCOPE,
    reason="Depends(scope=...) arrived in FastAPI 0.121",
)

requires_route_groups = pytest.mark.skipif(
    not hasattr(APIRouter, "frontend"),
    reason="route groups (APIRouter.frontend) are not in this FastAPI",
)

requires_no_flat_dependant = pytest.mark.skipif(
    HAS_FLAT_DEPENDANT,
    reason="get_flat_dependant exists up to FastAPI 0.140",
)
