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

from apscheduler.schedulers.background import BackgroundScheduler
import pytz

# ── ENV ────────────────────────────────────────────────────────────────
OPENAI_API_KEY      = os.getenv("OPENAI_API_KEY")
SUPABASE_URL        = os.getenv("SUPABASE_URL")
SUPABASE_KEY        = os.getenv("SUPABASE_KEY")
TWILIO_ACCOUNT_SID  = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN   = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_WHATSAPP     = os.getenv("TWILIO_WHATSAPP_NUMBER")  # e.g., "whatsapp:+54911XXXXXXX"
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

timezone = pytz.timezone("UTC")  # Usamos UTC para agendar las tareas


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


# ── Función para guardar cada mensaje en la tabla `conversations` ────────

def insert_conversation_record(whatsapp: str, role: str, message: str) -> None:
    """
    Inserta un registro en la tabla 'conversations' de Supabase.
    - whatsapp: teléfono en formato +54911XXXXXXX
    - role: 'user' o 'assistant'
    - message: contenido textual
    """
    try:
        supabase.table("conversations").insert({
            "whatsapp": whatsapp,
            "role": role,
            "message": message
        }).execute()
    except Exception:
        # En caso de error, no bloqueamos el flujo
        pass


# ── Estado “pendiente de fecha” en memoria ───────────────────────────────
# Cuando un usuario NO está en trips y envía un flight_number,
# pedimos la fecha y guardamos el estado aquí. (No persiste tras reinicio).
pending_date_requests: Dict[str, Dict[str, Any]] = {}


# ── APScheduler: envío de notificaciones automáticas ─────────────────────

def check_and_send_reminders():
    """
    Función que corre periódicamente:
    1) Busca en 'trips' los viajes cuyo 'next_check_at' <= now_utc.
    2) Envía recordatorio por WhatsApp.
    3) Actualiza 'next_check_at' al siguiente hito (3h antes) o lo pone a null.
    """
    now_utc = datetime.datetime.utcnow().replace(tzinfo=pytz.UTC)
    try:
        resp = supabase.table("trips") \
            .select(
                "id, client_name, flight_number, origin_iata, destination_iata,"
                " departure_date, status, whatsapp, next_check_at"
            ) \
            .lte("next_check_at", now_utc.isoformat()) \
            .execute()
    except Exception:
        return

    trips_due = resp.data or []
    for trip in trips_due:
        phone = trip.get("whatsapp")
        if not phone:
            continue

        # Armar mensaje de recordatorio
        departure = trip.get("departure_date")
        msg = (
            f"¡Hola {trip['client_name']}! Tu vuelo {trip['flight_number']} "
            f"de {trip['origin_iata']} a {trip['destination_iata']} "
            f"sale el {departure} UTC. "
            "Te avisamos para que organices tu viaje. ¡Buen viaje! 🚀"
        )

        # Enviar WhatsApp
        try:
            twilio_client.messages.create(
                from_=TWILIO_WHATSAPP,
                to=f"whatsapp:{phone}",
                body=msg
            )
        except Exception:
            pass

        # Calcular siguiente hito:
        try:
            dep_dt = datetime.datetime.fromisoformat(trip["departure_date"])
            dep_dt = dep_dt.replace(tzinfo=pytz.UTC)
        except Exception:
            continue

        delta = dep_dt - now_utc
        if delta > datetime.timedelta(hours=3):
            # Pasar a alerta de 3h antes
            next_hito = dep_dt - datetime.timedelta(hours=3)
        else:
            # No hay más notificaciones
            next_hito = None

        update_payload = {"next_check_at": next_hito.isoformat() if next_hito else None}
        try:
            supabase.table("trips") \
                .update(update_payload) \
                .eq("id", trip["id"]) \
                .execute()
        except Exception:
            pass


def schedule_jobs():
    """
    Inicia el scheduler en background que ejecuta 'check_and_send_reminders'
    cada minuto.
    """
    scheduler = BackgroundScheduler(timezone=timezone)
    scheduler.add_job(
        check_and_send_reminders,
        "interval",
        minutes=1,
        id="send_reminders_job",
        replace_existing=True
    )
    scheduler.start()


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
    0) Normalizamos y validamos número.
    1) Si había “pendiente de fecha” para este número, procesamos la fecha y llamamos a AeroAPI.
    2) Si es research, vamos a /research.
    3) Si el usuario está en trips, vamos directo a OpenAI.
    4) Si no está en trips y envía flight_number, le pedimos fecha y guardamos estado “pendiente”.
    5) Si no está en trips y no envía flight_number, pedimos vuelo/localizador.
    6) Guardamos cada mensaje en la tabla 'conversations'.
    """

    # ── 0) Normalizar y validar teléfono:
    phone = normalise_phone(From)  # 'whatsapp:+54911XXX' → '+54911XXX'
    if not validate_phone(phone):
        error_text = (
            "Disculpas, el formato de tu número no es válido. "
            "Asegúrate de estar enviando desde tu WhatsApp con código de país "
            "(ej: +54911XXXXXXX)."
        )
        insert_conversation_record(phone, "user", Body)
        insert_conversation_record(phone, "assistant", error_text)
        twilio_client.messages.create(
            from_=TWILIO_WHATSAPP,
            to=From,
            body=error_text
        )
        return {"reply": "Número inválido"}

    # Guardamos el mensaje del usuario
    insert_conversation_record(phone, "user", Body)

    # ── 1) ¿Tenía pendiente “esperando fecha”?
    if phone in pending_date_requests:
        pend = pending_date_requests[phone]
        flight_num = pend["flight"]
        try:
            user_date = datetime.date.fromisoformat(Body.strip())
        except ValueError:
            respuesta = (
                "No entendí la fecha. Por favor, enviá tu fecha de vuelo "
                "en formato YYYY-MM-DD. Por ejemplo: '2025-06-10'."
            )
            insert_conversation_record(phone, "assistant", respuesta)
            twilio_client.messages.create(from_=TWILIO_WHATSAPP, to=From, body=respuesta)
            return {"reply": respuesta}

        start_iso = user_date.isoformat() + "T00:00:00Z"
        next_day = user_date + datetime.timedelta(days=1)
        end_iso   = next_day.isoformat() + "T00:00:00Z"

        answer = fetch_flight_status_from_aeroapi_given_dates(flight_num, start_iso, end_iso)

        insert_conversation_record(phone, "assistant", answer)
        del pending_date_requests[phone]

        twilio_client.messages.create(from_=TWILIO_WHATSAPP, to=From, body=answer)
        return {"reply": answer}

    # ── 2) ¿Es consulta de research?
    if is_research_query(Body):
        try:
            r = requests.post(
                f"https://{os.getenv('RAILWAY_STATIC_URL')}/research",
                json={"question": Body},
                headers={"Content-Type": "application/json"},
                timeout=10
            )
            if r.status_code == 200:
                answer = r.json().get("answer", "Lo siento, no pude obtener respuesta.")
            else:
                answer = "Lo siento, hubo un problema al buscar la información."
        except Exception:
            answer = "Lo siento, hubo un problema al buscar la información."

        insert_conversation_record(phone, "assistant", answer)
        twilio_client.messages.create(from_=TWILIO_WHATSAPP, to=From, body=answer)
        return {"reply": answer}

    # ── 3) Intentamos traer el viaje del usuario (registrado) por teléfono
    trip = get_user_trip(From)
    if "error" not in trip:
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
        try:
            resp = requests.post(
                "https://api.openai.com/v1/chat/completions",
                json={"model": "gpt-4o-mini", "messages": messages},
                headers=HEADERS,
                timeout=10
            ).json()
            answer = resp["choices"][0]["message"]["content"]
        except Exception:
            answer = "Lo siento, algo falló al conectar con OpenAI."

        insert_conversation_record(phone, "assistant", answer)
        twilio_client.messages.create(from_=TWILIO_WHATSAPP, to=From, body=answer)
        return {"reply": answer}

    # ── 4) Si no está en trips: detectamos flight_number
    posible_flight = detect_flight_pattern(Body)
    if posible_flight:
        texto = (
            f"Entendido, tu vuelo es {posible_flight}. "
            "Para poder buscar información, "
            "¿me podés decir la fecha de tu vuelo en formato YYYY-MM-DD?"
        )
        insert_conversation_record(phone, "assistant", texto)
        twilio_client.messages.create(from_=TWILIO_WHATSAPP, to=From, body=texto)

        pending_date_requests[phone] = {
            "flight": posible_flight,
            "timestamp": datetime.datetime.utcnow()
        }
        return {"reply": texto}

    # ── 5) Ni registrado ni flight_pattern → pedimos registro o localizador
    respuesta = (
        "¡Hola! No encuentro tu reserva. "
        "Por favor, compárteme tu número de vuelo (por ejemplo: 'AR1234') "
        "o tu localizador para poder ayudarte."
    )
    insert_conversation_record(phone, "assistant", respuesta)
    twilio_client.messages.create(from_=TWILIO_WHATSAPP, to=From, body=respuesta)
    return {"reply": respuesta}


# ── Run local ──────────────────────────────────────────────────────────
if __name__ == "__main__":
    # Iniciar el scheduler antes de arrancar FastAPI
    schedule_jobs()
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
