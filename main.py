import os
import re
import datetime
import logging
from typing import Dict, Any, Optional

import requests
from fastapi import FastAPI, Form, HTTPException
from pydantic import BaseModel
from supabase import create_client, Client as SupabaseClient
from twilio.rest import Client as TwilioClient

# ── ENV ────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO)

OPENAI_API_KEY      = os.getenv("OPENAI_API_KEY")
SUPABASE_URL        = os.getenv("SUPABASE_URL")
SUPABASE_KEY        = os.getenv("SUPABASE_KEY")
TWILIO_ACCOUNT_SID  = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN   = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_WHATSAPP     = os.getenv("TWILIO_WHATSAPP_NUMBER")
SYSTEM_PROMPT       = os.getenv("SYSTEM_PROMPT")
AEROAPI_KEY         = os.getenv("AEROAPI_KEY")

required = [
    OPENAI_API_KEY,
    SUPABASE_URL,
    SUPABASE_KEY,
    TWILIO_ACCOUNT_SID,
    TWILIO_AUTH_TOKEN,
    TWILIO_WHATSAPP,
    SYSTEM_PROMPT
]
if not all(required):
    raise RuntimeError("Faltan variables de entorno obligatorias")

HEADERS = {
    "Authorization": f"Bearer {OPENAI_API_KEY}",
    "Content-Type": "application/json"
}

supabase: SupabaseClient = create_client(SUPABASE_URL, SUPABASE_KEY)
twilio_client: TwilioClient = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

# ── Pydantic models ────────────────────────────────────────────────────
class ResearchRequest(BaseModel):
    question: str

class ResearchResponse(BaseModel):
    answer: str

class OpenAIResponse(BaseModel):
    reply: str

# ── UTILIDADES ─────────────────────────────────────────────────────────
def normalise_phone(raw: str) -> str:
    return raw.split(":", 1)[1] if raw.startswith("whatsapp:") else raw

def validate_phone(phone: str) -> bool:
    pattern = re.compile(r"^\+\d{9,15}$")
    return bool(pattern.match(phone))

def detect_flight_pattern(text: str) -> Optional[str]:
    lo = text.strip().upper()
    match = re.fullmatch(r"^([A-Z]{2}\d{3,4})$", lo)
    return match.group(1) if match else None

def is_research_query(text: str) -> bool:
    lo = text.strip().lower()
    if detect_flight_pattern(text):
        return False
    if "vuelo" in lo:
        return False
    palabras_inicio = ("qué", "cómo", "dónde", "cuándo", "por qué", "cual")
    return ("?" in lo) or lo.startswith(palabras_inicio)

def fetch_flight_status_from_aeroapi(flight_number: str) -> str:
    hoy_utc = datetime.datetime.utcnow().date()
    manana_utc = hoy_utc + datetime.timedelta(days=1)
    start_iso = f"{hoy_utc.isoformat()}T00:00:00Z"
    end_iso   = f"{manana_utc.isoformat()}T00:00:00Z"
    if not AEROAPI_KEY:
        return f"No pude conectarme a AeroAPI. Estado estimado del vuelo {flight_number}: (desconocido)."
    url = f"https://aeroapi.flightaware.com/aeroapi/flights/{flight_number}?start={start_iso}&end={end_iso}"
    headers = {"x-api-key": AEROAPI_KEY, "Accept": "application/json"}
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        vuelos = data.get("flights", [])
        if not vuelos:
            return f"No hay datos disponibles para el vuelo {flight_number} en las últimas 24 horas."
        vuelo = vuelos[0]
        estado = vuelo.get("status", "desconocido")
        dep_sched = vuelo.get("departure", {}).get("scheduled", "")
        if dep_sched:
            try:
                dt = datetime.datetime.fromisoformat(dep_sched.rstrip("Z"))
                dep_sched = dt.strftime("%Y-%m-%d %H:%M UTC")
            except:
                pass
        return f"Estado del vuelo {flight_number}: {estado}.\nHora prevista de salida (UTC): {dep_sched}."
    except Exception as e:
        logging.error(f"AeroAPI error: {e}")
        return f"Error al consultar AeroAPI: {e}"

def get_user_trip(phone_number: str) -> Dict[str, Any]:
    phone = normalise_phone(phone_number)
    try:
        resp = supabase.table("trips") \
            .select(
                "id, client_name, flight_number, origin_iata, destination_iata,"
                " departure_date, status, metadata, passenger_description"
            ).eq("whatsapp", phone).single().execute()
    except Exception as e:
        logging.error(f"Supabase get_user_trip error: {e}")
        return {"error": f"Error Supabase: {e}"}
    data = resp.data or {}
    if not data:
        return {"error": "No se encontró ningún viaje para tu número."}
    dep = data.get("departure_date")
    if isinstance(dep, (datetime.date, datetime.datetime)):
        data["departure_date"] = dep.isoformat()
    return data

def find_today_trip_by_flight(flight_number: str) -> Optional[Dict[str, Any]]:
    hoy = datetime.datetime.utcnow().date().isoformat()
    try:
        resp = supabase.table("trips") \
            .select(
                "id, client_name, flight_number, origin_iata, destination_iata,"
                " departure_date, status, metadata, whatsapp"
            ) \
            .eq("flight_number", flight_number) \
            .eq("departure_date", hoy) \
            .single().execute()
    except Exception as e:
        logging.error(f"Supabase find_today_trip_by_flight error: {e}")
        return None
    data = resp.data or {}
    return data if data else None

def associate_phone_to_trip(trip_id: str, new_phone: str) -> bool:
    try:
        supabase.table("trips").update({"whatsapp": new_phone}).eq("id", trip_id).execute()
        return True
    except Exception as e:
        logging.error(f"Supabase associate_phone_to_trip error: {e}")
        return False

def insert_conversation_record(whatsapp: str, role: str, message: str, trip_id: Optional[str]) -> None:
    try:
        supabase.table("conversations").insert({
            "whatsapp": whatsapp,
            "role": role,
            "message": message,
            "trip_id": trip_id
        }).execute()
    except Exception as e:
        logging.error(f"Supabase insert_conversation_record error: {e}")

def openai_chat(messages: list) -> str:
    try:
        logging.info("Llamando a OpenAI con payload: %s", messages)
        resp = requests.post(
            "https://api.openai.com/v1/chat/completions",
            json={"model": "gpt-4o-mini", "messages": messages},
            headers=HEADERS,
            timeout=15
        )
        resp.raise_for_status()
        text = resp.json()["choices"][0]["message"]["content"]
        logging.info("Respuesta de OpenAI: %s", text)
        return text
    except Exception as e:
        logging.error(f"OpenAI error: {e}")
        return "Lo siento, algo falló al conectar con OpenAI."

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
    resp = requests.post(
        "https://api.openai.com/v1/chat/completions",
        json=payload,
        headers=HEADERS,
        timeout=15
    )
    if resp.status_code != 200:
        raise HTTPException(resp.status_code, resp.text)
    return {"answer": resp.json()["choices"][0]["message"]["content"]}

@app.post("/webhook", response_model=OpenAIResponse)
def whatsapp_webhook(From: str = Form(...), Body: str = Form(...)):
    phone = normalise_phone(From)
    if not validate_phone(phone):
        twilio_client.messages.create(
            from_=TWILIO_WHATSAPP,
            to=From,
            body=(
                "Disculpas, el formato de tu número no es válido. "
                "Asegúrate de estar enviando desde tu WhatsApp con código de país "
                "(ej: +54911XXXXXXX)."
            )
        )
        return {"reply": "Número inválido"}

    # 1) Intentar obtener el viaje del usuario
    trip = get_user_trip(From)
    trip_id = trip.get("id") if "error" not in trip else None

    if trip_id:
    hist_resp = supabase.table("conversations") \
        .select("role, message") \
        .eq("trip_id", trip_id) \
        .order("created_at", {"ascending": True}) \
        .limit(15) \
        .execute()
    history = hist_resp.data or []
else:
    history = []

    # 2) Guardar mensaje de usuario en conversations
    insert_conversation_record(phone, "user", Body, trip_id)

    # 3) Si es consulta de research
    if is_research_query(Body):
        try:
            r = requests.post(
                f"https://{os.getenv('RAILWAY_STATIC_URL')}/research",
                json={"question": Body},
                headers={"Content-Type": "application/json"},
                timeout=15
            )
            if r.status_code == 200:
                answer = r.json().get("answer", "Lo siento, no pude obtener respuesta.")
            else:
                answer = "Lo siento, hubo un problema al buscar la información."
        except Exception as e:
            logging.error(f"Research endpoint error: {e}")
            answer = "Lo siento, hubo un problema al buscar la información."

        insert_conversation_record(phone, "assistant", answer, trip_id)
        twilio_client.messages.create(from_=TWILIO_WHATSAPP, to=From, body=answer)
        return {"reply": answer}

    # 4) Si el usuario no tiene viaje asociado, manejamos flight_number
    if not trip_id:
        posible_flight = detect_flight_pattern(Body)
        if posible_flight:
            found = find_today_trip_by_flight(posible_flight)
            if found:
                exito = associate_phone_to_trip(found["id"], phone)
                if exito:
                    trip = {
                        "id": found["id"],
                        "client_name": found["client_name"],
                        "flight_number": found["flight_number"],
                        "origin_iata": found["origin_iata"],
                        "destination_iata": found["destination_iata"],
                        "departure_date": found["departure_date"],
                        "status": found["status"],
                        "passenger_description": found.get("passenger_description", "")
                    }
                    trip_id = trip["id"]
                else:
                    answer = (
                        "¡Ups! Hubo un problema al asociar tu número con el vuelo. "
                        "Por favor, inténtalo de nuevo más tarde."
                    )
                    insert_conversation_record(phone, "assistant", answer, None)
                    twilio_client.messages.create(from_=TWILIO_WHATSAPP, to=From, body=answer)
                    return {"reply": answer}
            else:
                answer = fetch_flight_status_from_aeroapi(posible_flight)
                insert_conversation_record(phone, "assistant", answer, None)
                twilio_client.messages.create(from_=TWILIO_WHATSAPP, to=From, body=answer)
                return {"reply": answer}
        else:
            answer = (
                "¡Hola! No encuentro tu reserva. "
                "Por favor, compárteme tu número de vuelo (por ejemplo: 'AR1234') "
                "o tu localizador para poder ayudarte."
            )
            insert_conversation_record(phone, "assistant", answer, None)
            twilio_client.messages.create(from_=TWILIO_WHATSAPP, to=From, body=answer)
            return {"reply": answer}

    # 5) Usuario ya registrado (o recién asociado): llamamos a OpenAI
    descripcion = trip.get("passenger_description", "")
    user_ctx = f"Eres el asistente de {trip['client_name']}.\n"
    if descripcion:
        user_ctx += f"Perfil: {descripcion}\n"
    user_ctx += (
        f"Vuelo {trip['flight_number']} de {trip['origin_iata']} "
        f"a {trip['destination_iata']}, programado {trip['departure_date']}. "
        f"Estado actual: {trip['status']}."
    )

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT + "\n" + user_ctx},
        {"role": "user", "content": Body}
    ]
    answer = openai_chat(messages)

    insert_conversation_record(phone, "assistant", answer, trip_id)
    try:
        twilio_client.messages.create(from_=TWILIO_WHATSAPP, to=From, body=answer)
    except Exception as e:
        logging.error(f"Twilio send error: {e}")
        raise HTTPException(500, f"Error al enviar WhatsApp: {e}")

    return {"reply": answer}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
