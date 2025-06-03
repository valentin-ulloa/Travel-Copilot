import os
import json
import datetime
from typing import Dict, Any

import requests
from fastapi import FastAPI, Form, HTTPException
from pydantic import BaseModel
from supabase import create_client, Client as SupabaseClient
from twilio.rest import Client as TwilioClient

# ── ENV ────────────────────────────────────────────────────────────────
OPENAI_API_KEY      = os.getenv("OPENAI_API_KEY")
SUPABASE_URL        = os.getenv("SUPABASE_URL")
SUPABASE_KEY        = os.getenv("SUPABASE_KEY")
TWILIO_ACCOUNT_SID  = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN   = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_WHATSAPP     = os.getenv("TWILIO_WHATSAPP_NUMBER")
SYSTEM_PROMPT       = os.getenv("SYSTEM_PROMPT")  # ← añade en Railway

required = [
    OPENAI_API_KEY, SUPABASE_URL, SUPABASE_KEY,
    TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_WHATSAPP, SYSTEM_PROMPT
]
if not all(required):
    raise RuntimeError("Faltan variables de entorno obligatorias")

HEADERS = {
    "Authorization": f"Bearer {OPENAI_API_KEY}",
    "Content-Type": "application/json"
}

supabase: SupabaseClient = create_client(SUPABASE_URL, SUPABASE_KEY)
twilio_client: TwilioClient = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

# ── Pydantic models (solo para docs) ───────────────────────────────────
class ResearchRequest(BaseModel):
    question: str
class ResearchResponse(BaseModel):
    answer: str
class OpenAIResponse(BaseModel):
    reply: str

# ── Utilidades ─────────────────────────────────────────────────────────
def normalise_phone(raw: str) -> str:
    """Convierte 'whatsapp:+549...' → '+549...'."""
    return raw.split(":", 1)[1] if raw.startswith("whatsapp:") else raw

def get_user_trip(phone_number: str) -> Dict[str, Any]:
    """Trae la fila de trips para el número o devuelve {'error':...}."""
    phone = normalise_phone(phone_number)
    try:
        resp = supabase.table("trips") \
            .select(
                "client_name, flight_number, origin_iata, destination_iata,"
                " departure_date, status, metadata"
            ).eq("whatsapp", phone).single().execute()
    except Exception as e:
        return {"error": f"Error Supabase: {e}"}
    data = resp.data or {}
    if not data:
        return {"error": "No se encontró ningún viaje para tu número."}
    dep = data.get("departure_date")
    if isinstance(dep, (datetime.date, datetime.datetime)):
        data["departure_date"] = dep.isoformat()
    return data

def is_research_query(text: str) -> bool:
    lo = text.strip().lower()
    if "vuelo" in lo:            # cualquier mención a “vuelo” → rama flight-status
        return False
    return "?" in lo or lo.startswith(("qué", "cómo", "dónde", "cuándo", "por qué", "cual"))

# ── FASTAPI ────────────────────────────────────────────────────────────
app = FastAPI()

@app.post("/research", response_model=ResearchResponse)
def research(req: ResearchRequest):
    payload = {
        "model": "gpt-4o-mini",
        "messages": [
            {"role": "system", "content": "Eres un asistente de viajes experto."},
            {"role": "user",   "content": req.question}
        ]
    }
    resp = requests.post("https://api.openai.com/v1/chat/completions",
                         json=payload, headers=HEADERS)
    if resp.status_code != 200:
        raise HTTPException(resp.status_code, resp.text)
    return {"answer": resp.json()["choices"][0]["message"]["content"]}

@app.post("/webhook", response_model=OpenAIResponse)
def whatsapp_webhook(From: str = Form(...), Body: str = Form(...)):
    """Webhook de Twilio WhatsApp."""
    # ── 1) Branch research vs flight-status
    if is_research_query(Body):
        r = requests.post(f"https://{os.getenv('RAILWAY_STATIC_URL')}/research",
                          json={"question": Body},
                          headers={"Content-Type": "application/json"})
        answer = r.json().get("answer", "Lo siento, no pude obtener respuesta.") \
                 if r.status_code == 200 else \
                 "Lo siento, hubo un problema al buscar la información."
    else:
        # ── 2) Consulta Supabase directamente
        trip = get_user_trip(From)
        if "error" in trip:
            answer = ("¡Hola! No encuentro tu reserva. "
                      "¿Me compartes tu número de vuelo o localizador?")
        else:
            # ── 3) Construye prompt con contexto + pregunta del usuario
            user_ctx = (
                f"Eres el asistente de {trip['client_name']}. "
                f"Vuelo {trip['flight_number']} de {trip['origin_iata']} "
                f"a {trip['destination_iata']}, programado {trip['departure_date']}. "
                f"Estado actual: {trip['status']}."
            )
            messages = [
                {"role": "system", "content": SYSTEM_PROMPT + "\n" + user_ctx},
                {"role": "user", "content": Body}
            ]
            resp = requests.post(
                "https://api.openai.com/v1/chat/completions",
                json={"model": "gpt-4o-mini", "messages": messages},
                headers=HEADERS
            ).json()
            answer = resp["choices"][0]["message"]["content"]

    # ── 4) Envía la respuesta por WhatsApp
    try:
        twilio_client.messages.create(from_=TWILIO_WHATSAPP, to=From, body=answer)
    except Exception as e:
        raise HTTPException(500, f"Error al enviar WhatsApp: {e}")

    return {"reply": answer}

# ── Run local ──────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
