# DGEN Chatbot

Python service that connects DGEN chat widget to Vertex AI Agent Engine. It handles session management, rate limiting, and easy deployment to Google Cloud.

## Quick Install
1. Install Dependencies
```bash
python -m venv .venv
source .venv/bin/activate
uv sync
```

2. Configure environment:
```bash
cp .env.example .env
# Edit .env with your configuration
```

3. Run the agent locally
```bash
cd src
adk web
```
