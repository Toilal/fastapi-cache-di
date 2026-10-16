import fastapi.dependencies.utils as _dep_utils
import pytest

HAS_FLAT_DEPENDANT = hasattr(_dep_utils, "get_flat_dependant")

requires_flat_dependant = pytest.mark.skipif(
    not HAS_FLAT_DEPENDANT,
    reason="get_flat_dependant was removed in FastAPI 0.141",
)

requires_no_flat_dependant = pytest.mark.skipif(
    HAS_FLAT_DEPENDANT,
    reason="get_flat_dependant exists up to FastAPI 0.140",
)
