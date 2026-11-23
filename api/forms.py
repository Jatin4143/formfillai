"""
Forms catalog & schema API endpoints.
"""
import logging
from starlette.responses import JSONResponse
from core import db

log = logging.getLogger("api.forms")


async def list_forms_endpoint(request):
    """GET /api/forms - returns all available forms."""
    forms = db.list_forms()
    return JSONResponse(forms)


async def get_form_schema_endpoint(request):
    """GET /api/forms/{key}/schema - returns schema and fields for a form."""
    form_key = request.path_params.get("key")
    schema = db.fetch_form_schema(form_key)
    if not schema:
        return JSONResponse({"error": "Form not found"}, status_code=404)
    return JSONResponse(schema)

