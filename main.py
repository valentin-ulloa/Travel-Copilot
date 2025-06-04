import os
import re
import json
import datetime
from typing import Dict, Any, Optional

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


# ── Pydantic models (solo para docs) ───────────────────────────────────
class ResearchRequest(BaseModel):
    question: str

class ResearchResponse(BaseModel):
    answer: str

class OpenAIResponse(BaseModel):
    reply: str


# ── UTILIDADES ─────────────────────────────────────────────────────────

def normalise_phone(raw: str) -> str:
    """
    Convierte 'whatsapp:+54911XXXXXXX' → '+54911XXXXXXX'.
    Si no empieza con 'whatsapp:', devuelve raw tal cual.
    """
    return raw.split(":", 1)[1] if raw.startswith("whatsapp:") else raw


def validate_phone(phone: str) -> bool:
    """
    Verifica que phone empiece con '+' seguido de entre 9 y 15 dígitos.
    Ejemplos válidos: '+54911XXXXXXX', '+12025550123'.
    """
    pattern = re.compile(r"^\+\d{9,15}$")
    return bool(pattern.match(phone))


def detect_flight_pattern(text: str) -> Optional[str]:
    """
    Detecta si el texto coincide con un patrón de número de vuelo:
    - Dos letras (A–Z) seguidas de 3 o 4 dígitos, p. ej., 'AR1234', 'LA567'.
    - Devuelve el número de vuelo en mayúsculas sin espacios, o None.
    """
    lo = text.strip().upper()
    match = re.fullmatch(r"^([A-Z]{2}\d{3,4})$", lo)
    if match:
        return match.group(1)
    return None


def is_research_query(text: str) -> bool:
    """
    Si detecta un patrón de vuelo, devuelve False (no es consulta de investigación).
    Si menciona la palabra 'vuelo', también devuelve False.
    En otro caso, si contiene '?' o empieza con 'qué', 'cómo', etc., lo considera research.
    """
    lo = text.strip().lower()

    # 1) Si coincide con número de vuelo: NO es research.
    if detect_flight_pattern(text):
        return False

    # 2) Si menciona explícitamente la palabra 'vuelo', asumimos que no es research:
    if "vuelo" in lo:
        return False

    # 3) Resto de casos, si contiene '?' o empieza con palabras de investigación:
    palabras_inicio = ("qué", "cómo", "dónde", "cuándo", "por qué", "cual")
    return ("?" in lo) or lo.startswith(palabras_inicio)


def get_user_trip(phone_number: str) -> Dict[str, Any]:
    """
    Trae la fila de trips para el número o devuelve {'error': ...}.
    """
    phone = normalise_phone(phone_number)
    try:
        resp = supabase.table("trips") \
            .select(
                "id, client_name, flight_number, origin_iata, destination_iata,"
                " departure_date, status, metadata"
            ).eq("whatsapp", phone).single().execute()
    except Exception as e:
        return {"error": f"Error Supabase: {e}"}

    data = resp.data or {}
    if not data:
        return {"error": "No se encontró ningún viaje para tu número."}

    # Aseguramos que departure_date sea ISO string (YYYY-MM-DD o con hora)
    dep = data.get("departure_date")
    if isinstance(dep, (datetime.date, datetime.datetime)):
        data["departure_date"] = dep.isoformat()
    return data


def find_today_trip_by_flight(flight_number: str) -> Optional[Dict[str, Any]]:
    """
    Busca en trips un viaje cuyo flight_number == flight_number
    y cuya departure_date sea 'hoy' (fecha UTC).
    Si lo encuentra, devuelve la fila completa (incluyendo id y whatsapp).
    """
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
    except Exception:
        return None

    data = resp.data or {}
    return data if data else None


def associate_phone_to_trip(trip_id: str, new_phone: str) -> bool:
    """
    Actualiza la fila trips con id == trip_id, poniendo whatsapp = new_phone.
    Devuelve True si tuvo éxito, False si hubo error.
    """
    try:
        supabase.table("trips") \
            .update({"whatsapp": new_phone}) \
            .eq("id", trip_id) \
            .execute()
        return True
    except Exception:
        return False


def fetch_flight_status_from_aeroapi_given_dates(
    flight_number: str, start_iso: str, end_iso: str
) -> str:
    """
    Llama a AeroAPI (FlightAware) para traer estado de un vuelo en la ventana dada.
    start_iso y end_iso deben tener el formato 'YYYY-MM-DDTHH:MM:SSZ'.
    Si no hay AEROAPI_KEY, devuelve un stub.
    """
    if not AEROAPI_KEY:
        return (
            f"No pude conectarme a AeroAPI. Estado estimado del vuelo {flight_number}: (desconocido).\n"
            f"(Configura AEROAPI_KEY para detalles reales.)"
        )

    url = (
        f"https://aeroapi.flightaware.com/aeroapi/flights/"
        f"{flight_number}"
        f"?start={start_iso}&end={end_iso}"
    )
    headers = {
        "x-api-key": AEROAPI_KEY,
        "Accept": "application/json"
    }

    try:
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code != 200:
            return f"AeroAPI devolvió error (código {resp.status_code})."
        data = resp.json()
        vuelos = data.get("flights", [])
        if not vuelos:
            return f"No hay datos disponibles para el vuelo {flight_number} en ese rango."
        vuelo = vuelos[0]
        estado = vuelo.get("status", "desconocido")
        dep_sched = vuelo.get("departure", {}).get("scheduled", "")
        if dep_sched:
            try:
                dt = datetime.datetime.fromisoformat(dep_sched.rstrip("Z"))
                dep_sched = dt.strftime("%Y-%m-%d %H:%M UTC")
            except:
                pass
        return (
            f"Estado del vuelo {flight_number}: {estado}.\n"
            f"Hora prevista de salida (UTC): {dep_sched}."
        )
    except Exception as e:
        return f"Error al consultar AeroAPI: {e}"


# ── Estado “pendiente de fecha” en memoria ───────────────────────────────
# Cuando un usuario NO está en trips y envía un flight_number,
# pedimos la fecha y guardamos el estado aquí. (Ojo: no persiste más allá de reinicio).
pending_date_requests: Dict[str, Dict[str, Any]] = {}


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
        timeout=10
    )
    if resp.status_code != 200:
        raise HTTPException(resp.status_code, resp.text)
    return {"answer": resp.json()["choices"][0]["message"]["content"]}


@app.post("/webhook", response_model=OpenAIResponse)
def whatsapp_webhook(From: str = Form(...), Body: str = Form(...)):
    """
    Webhook de Twilio WhatsApp.
    1) Normalizamos y validamos número.
    2) Si había “pendiente de fecha” para este número, procesamos la fecha y llamamos a AeroAPI.
    3) Si es research, vamos a /research.
    4) Si el usuario está en trips, vamos directo a OpenAI (sin llamar a AeroAPI).
    5) Si no está en trips y envía solo flight_number, le pedimos fecha y guardamos estado “pendiente”.
    6) Si no está en trips y no envía flight_number, le pedimos numero de vuelo o localizador.
    """

    # ── 0) Normalizar y validar teléfono:
    phone = normalise_phone(From)  # 'whatsapp:+54911XXX' → '+54911XXX'
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

    # ── 1) ¿Tenía pendiente “esperando fecha” para este número?
    if phone in pending_date_requests:
        pend = pending_date_requests[phone]
        flight_num = pend["flight"]
        # Body ahora debe ser la fecha en YYYY-MM-DD.
        try:
            user_date = datetime.date.fromisoformat(Body.strip())
        except ValueError:
            respuesta = (
                "No entendí la fecha. Por favor, enviá tu fecha de vuelo "
                "en formato YYYY-MM-DD. Por ejemplo: '2025-06-10'."
            )
            twilio_client.messages.create(from_=TWILIO_WHATSAPP, to=From, body=respuesta)
            return {"reply": respuesta}

        # Construimos start/end en UTC usando la fecha que dio el usuario:
        start_iso = user_date.isoformat() + "T00:00:00Z"
        next_day = user_date + datetime.timedelta(days=1)
        end_iso_
