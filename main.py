"""
Main Application Entrypoint for FieldFlow AI Form Assistant.
Initializes Starlette application, configures middleware, defines routes,
and starts the Uvicorn web server.
"""

import logging
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.routing import Route, Mount
from starlette.staticfiles import StaticFiles
import uvicorn

from config import FRONTEND_DIR, PORT
from api import (
    NoCacheMiddleware,
    list_forms_endpoint,
    get_form_schema_endpoint,
    list_chats_endpoint,
    create_chat_endpoint,
    get_chat_endpoint,
    update_chat_endpoint,
    delete_chat_endpoint,
    chat_endpoint,
    submit_form_endpoint,
    submit_batch_endpoint,
    list_submissions_endpoint,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("main")

routes = [
    Route("/api/forms", list_forms_endpoint, methods=["GET"]),
    Route("/api/forms/{key}/schema", get_form_schema_endpoint, methods=["GET"]),
    Route("/api/chats", list_chats_endpoint, methods=["GET"]),
    Route("/api/chats", create_chat_endpoint, methods=["POST"]),
    Route("/api/chats/{id}", get_chat_endpoint, methods=["GET"]),
    Route("/api/chats/{id}", update_chat_endpoint, methods=["PUT"]),
    Route("/api/chats/{id}", delete_chat_endpoint, methods=["DELETE"]),
    Route("/api/chat", chat_endpoint, methods=["POST"]),
    Route("/api/forms/{key}/submit", submit_form_endpoint, methods=["POST"]),
    Route("/api/forms/submit-batch", submit_batch_endpoint, methods=["POST"]),
    Route("/api/submissions", list_submissions_endpoint, methods=["GET"]),
    Mount("/", app=StaticFiles(directory=str(FRONTEND_DIR), html=True)),
]

middleware = [
    Middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    ),
    Middleware(NoCacheMiddleware),
]

app = Starlette(debug=True, routes=routes, middleware=middleware)

if __name__ == "__main__":
    log.info("Starting FieldFlow API Server on http://0.0.0.0:%s", PORT)
    uvicorn.run("main:app", host="0.0.0.0", port=PORT, reload=True)

