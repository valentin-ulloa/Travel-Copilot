import os
import json
import datetime
from typing import Dict, Any

import requests
from fastapi import FastAPI, Form, HTTPException
from pydantic import BaseModel

from supabase import create_client, Client as SupabaseClient
from twilio.rest import Client as TwilioClient

# ────────────────────
#  CREDENCIALES / ENV
# ────────────────────
OPENAI_API_KEY      = os.getenv("OPENAI_API_KEY")
SUPABASE_URL        = os.getenv("SUPABASE_URL")
SUPABASE_KEY        = os.getenv("SUPABASE_KEY")
TWILIO_ACCOUNT_SID  = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN   = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_WHATSAPP     = os.getenv("TWILIO_WHATSAPP_NUMBER")
SYSTEM_PROMPT       = os.getenv("SYSTEM_PROMPT")  # ← nuevo

required_env = [
    OPENAI_API_KEY, SUPABASE_URL, SUPABASE_KEY,
    TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_WHATSAPP, SYSTEM_PROMPT
]
if not all(required_env):
    raise RuntimeError("Faltan variables de entorno obligatorias.")

supabase: SupabaseClient = create_client(SUPABASE_URL, SUPABASE_KEY)
twilio_client: TwilioClient = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

# ────────────────
#  SCHEMAS
# ────────────────
class ResearchRequest(BaseModel):
    question: str
class ResearchResponse(BaseModel):
    answer: str
class FunctionCall(BaseModel):
    name: str
    arguments: Dict[str, Any]
class OpenAIResponse(BaseModel):
    reply: str

# ────────────────
#  HEURÍSTICA
# ────────────────
def is_research_query(text: str) -> bool:
    lo = text.strip().lower()
    if "vuelo" in lo:
        return False
    return "?" in lo or lo.startswith(("qué", "cómo", "dónde", "cuándo", "por qué", "cual"))

# ────────────────
#  HELPERS
# ────────────────
def normalise_phone(raw: str) -> str:
    return raw.split(":", 1)[1] if raw.startswith("whatsapp:") else raw

def get_user_trip(phone_number: str) -> Dict[str, Any]:
    phone_number = normalise_phone(phone_number)
    try:
        resp = supabase.table("trips") \
                       .select("client_name, flight_number, origin_iata, destination_iata, departure_date, status, metadata") \
                       .eq("whatsapp", phone_number) \
                       .single() \
                       .execute()
    except Exception as e:
        return {"error": f"Error Supabase: {e}"}

    data = resp.data or {}
    if not data:
        return {"error": "No se encontró ningún viaje registrado para tu número."}

    dep = data.get("departure_date")
    if isinstance(dep, (datetime.date, datetime.datetime)):
        data["departure_date"] = dep.isoformat()
    return data

FUNCTIONS = [{
    "name": "get_user_trip",
    "description": "Devuelve la información del viaje de un usuario.",
    "parameters": {
        "type": "object",
        "properties": {
            "phone_number": {"type": "string"}
        },
        "required": ["phone_number"]
    }
}]

HEADERS = {
    "Authorization": f"Bearer {OPENAI_API_KEY}",
    "Content-Type": "application/json"
}

app = FastAPI()

# ────────────────
#  /research (igual)
# ────────────────
@app.post("/research", response_model=ResearchResponse)
def research(req: ResearchRequest):
    payload = {
        "model": "gpt-4o-mini",
        "messages": [
            {"role": "system", "content": "Eres un asistente de viajes experto."},
            {"role": "user", "content": req.question}
        ]
    }
    resp = requests.post("https://api.openai.com/v1/chat/completions", json=payload, headers=HEADERS)
    if resp.status_code != 200:
        raise HTTPException(resp.status_code, resp.text)
    data = resp.json()
    return {"answer": data["choices"][0]["message"]["content"]}

# ────────────────
#  /webhook
# ────────────────
@app.post("/webhook", response_model=OpenAIResponse)
def whatsapp_webhook(From: str = Form(...), Body: str = Form(...)):
    if is_research_query(Body):
        r = requests.post(
            f"https://{os.getenv('RAILWAY_STATIC_URL')}/research",
            json={"question": Body},
            headers={"Content-Type": "application/json"}
        )
        answer = r.json().get("answer", "Lo siento, sin respuesta.") if r.status_code == 200 else \
                 "Lo siento, hubo un problema al buscar la información."
    else:
        system_msg = {"role": "system", "content": SYSTEM_PROMPT}
        user_msg   = {"role": "user", "content": Body}

        initial = requests.post(
            "https://api.openai.com/v1/chat/completions",
            json={
                "model": "gpt-4o-mini",
                "messages": [system_msg, user_msg],
                "functions": FUNCTIONS,
                "function_call": "auto"
            },
            headers=HEADERS
        ).json()

        msg0 = initial["choices"][0]["message"]

        if "function_call" in msg0:
            call = FunctionCall.parse_obj(msg0["function_call"])
            phone = normalise_phone(call.arguments.get("phone_number") or From)
            trip  = get_user_trip(phone)

            second = requests.post(
                "https://api.openai.com/v1/chat/completions",
                json={
                    "model": "gpt-4o-mini",
                    "messages": [
                        system_msg,
                        user_msg,
                        {"role": "assistant", "content": None, "function_call": msg0["function_call"]},
                        {"role": "function", "name": "get_user_trip", "content": json.dumps(trip)}
                    ]
                },
                headers=HEADERS
            ).json()
            answer = second["choices"][0]["message"]["content"]
        else:
            answer = msg0.get("content", "Lo siento, no entendí tu solicitud.")

    # enviar por Twilio
    twilio_client.messages.create(from_=TWILIO_WHATSAPP, to=From, body=answer)
    return {"reply": answer}

# ────────────────
#  MAIN
# ────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
