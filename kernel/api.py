#!/usr/bin/env python3
# =============================================================================
# api.py — CoPES / Phoenix DevOps OS
# FastAPI bridge: PHP → Frank → Ollama (Mistral) → response
# Port: 8000
# jwl247 / United Systems / GPL v3
# =============================================================================

import sys, os, logging
import httpx
from fastapi import FastAPI, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SRC_DIR = os.path.abspath(os.path.join(SCRIPT_DIR, ".."))
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

logging.basicConfig(level=logging.INFO, format="[API] %(asctime)s — %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("api")

OLLAMA_URL   = os.environ.get("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "mistral")
API_SECRET   = os.environ.get("API_SECRET", "lifefirst_copes_secret_change_me")

SYSTEM_PROMPTS = {
    "schedule":     "You are a schedule assistant for LifeFirst. Help manage calendar and availability. Be concise.",
    "messenger":    "You are a communication assistant for LifeFirst. Help compose and understand messages.",
    "memory":       "You are a memory assistant for LifeFirst. Help recall preferences and important context.",
    "notification": "You are a notification assistant for LifeFirst. Be brief and actionable.",
    "voice":        "You are a helpful general assistant for LifeFirst. Be friendly and practical.",
}

class AIRequest(BaseModel):
    user_id: int
    username: str
    display_name: str
    message: str
    intent: str
    action: Optional[str] = "query"

class AIResponse(BaseModel):
    status: str
    message: str
    intent: str
    model: str

app = FastAPI(title="CoPES AI Bridge", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["POST","GET"], allow_headers=["*"])

frank = None

@app.on_event("startup")
async def startup():
    global frank
    try:
        from kernel.frank import build_ring_chain
        frank = build_ring_chain()
        log.info("Frank-0 online. Stationary. Sovereign.")
    except Exception as e:
        log.warning(f"Kernel init warning: {e}")

async def call_ollama(intent: str, message: str, display_name: str) -> str:
    prompt = SYSTEM_PROMPTS.get(intent, SYSTEM_PROMPTS["voice"])
    prompt += f"\n\nYou are speaking with {display_name}."
    payload = {
        "model": OLLAMA_MODEL,
        "messages": [{"role": "system", "content": prompt}, {"role": "user", "content": message}],
        "stream": False
    }
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            r = await client.post(f"{OLLAMA_URL}/api/chat", json=payload)
            r.raise_for_status()
            return r.json()["message"]["content"]
    except httpx.ConnectError:
        raise HTTPException(status_code=503, detail="AI service unavailable")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/health")
async def health():
    ollama_ok = False
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(f"{OLLAMA_URL}/api/tags")
            ollama_ok = r.status_code == 200
    except Exception:
        pass
    return {"status": "online", "ollama": "reachable" if ollama_ok else "unreachable", "model": OLLAMA_MODEL}

@app.post("/ai", response_model=AIResponse)
async def ai_endpoint(request: AIRequest, authorization: Optional[str] = Header(None)):
    token = (authorization or "").replace("Bearer ", "").strip()
    if token != API_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")
    log.info(f"user={request.username} intent={request.intent}")
    response_text = await call_ollama(request.intent, request.message, request.display_name)
    return AIResponse(status="success", message=response_text, intent=request.intent, model=OLLAMA_MODEL)

if __name__ == "__main__":
    import uvicorn
    log.info("CoPES AI Bridge — Frank receives. Mistral responds.")
    uvicorn.run("api:app", host="0.0.0.0", port=8000, reload=False)
