"""
Central Configuration for FieldFlow AI.

All modules (multi_form_planner.py, demo_paragraph.py, server.py, and future flows)
import their LLM settings, URLs, database paths, and shared clients from here.
"""

import os
from pathlib import Path
import ollama

# Base Directories
BASE_DIR = Path(__file__).resolve().parent
FRONTEND_DIR = BASE_DIR / "frontend"
DB_PATH = BASE_DIR / "forms.db"
ENV_FILE = BASE_DIR / ".env"

# Load .env file if present
if ENV_FILE.exists():
    with open(ENV_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip("'\""))

# LLM & Ollama Configuration
MODEL = os.getenv("MODEL", "llama3.2:1b")
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434")
PORT = int(os.getenv("PORT", "8000"))

# Shared Ollama Client Instance
ollama_client = ollama.Client(host=OLLAMA_BASE_URL)

# Application Branding
APP_NAME = os.getenv("APP_NAME", "FieldFlow")
APP_TAGLINE = os.getenv("APP_TAGLINE", "Fill any form from chat")

