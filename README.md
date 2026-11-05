# FieldFlow - AI Form Assistant

AI-powered conversational form-filling assistant built with LangGraph, Ollama, and Starlette.

---

## 🛠️ Setup & Running

### 1. Prerequisites
- Python 3.10+
- [Ollama](https://ollama.com/) installed and running

### 2. Environment Setup

```bash
# Create and activate virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

### 3. Configuration

Create `.env` from the template:

```bash
cp .env.example .env
```

Ensure your `.env` has:
```env
OLLAMA_BASE_URL="http://127.0.0.1:11434"
MODEL="llama3.2:3b"
PORT=8000
```

Download the model in Ollama:
```bash
ollama pull llama3.2:3b
```

### 4. Run the Application

Start the backend server:

```bash
python server.py
```

Open in browser:
- **Web UI**: [http://localhost:8000](http://localhost:8000)
- **API**: [http://localhost:8000/api/forms](http://localhost:8000/api/forms)
