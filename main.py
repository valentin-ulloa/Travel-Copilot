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

# Variables para AeroAPI (FlightAware)
AEROAPI_KEY         = os.getenv("AEROAPI_KEY")
# No almacenamos AEROAPI_URL completo con placeholders; lo generaremos dinámicamente.
# Formato base: "https://aeroapi.flightaware.com/aeroapi/flights/{flight_number}?start={start_iso}&end={end_iso}"

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


def fetch_flight_status_from_aeroapi(flight_number: str) -> str:
    """
    Llama a AeroAPI (FlightAware) para traer estado de un vuelo en ventana de 24hs:
    - start = hoy a las 00:00:00 UTC
    - end   = mañana a las 00:00:00 UTC

    URL resultante:
    https://aeroapi.flightaware.com/aeroapi/flights/{flight_number}?start={start_iso}&end={end_iso}

    Si no hay AEROAPI_KEY, devuelve un texto genérico.
    """
    # 1) Obtenemos fecha de hoy y de mañana en UTC, formateadas como ISO 'YYYY-MM-DDT00:00:00Z'
    hoy_utc = datetime.datetime.utcnow().date()
    manana_utc = hoy_utc + datetime.timedelta(days=1)

    start_iso = f"{hoy_utc.isoformat()}T00:00:00Z"
    end_iso   = f"{manana_utc.isoformat()}T00:00:00Z"

    # 2) Si no tenemos credencial AEROAPI, devolvemos un stub
    if not AEROAPI_KEY:
        return (
            f"No pude conectarme a AeroAPI. Estado estimado del vuelo {flight_number}: (desconocido).\n"
            f"(Intenta configurar AEROAPI_KEY para detalles reales.)"
        )

    # 3) Construimos la URL
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
        # Estructura típica de FlightAware AeroAPI:
        #   data["flights"] es una lista de vuelos; tomamos el primero si existe.
        vuelos = data.get("flights", [])
        if not vuelos:
            return f"No hay datos disponibles para el vuelo {flight_number} en las últimas 24 horas."

        vuelo = vuelos[0]
        estado = vuelo.get("status", "desconocido")

        # Intentamos obtener hora prevista de salida en UTC
        dep_info = vuelo.get("faFlightID", {})
        # Dependiendo del endpoint exacto, puede venir en otra parte; 
        # como ejemplo, se asume que 'departure' tiene 'scheduled' en ISO
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

    # Aseguramos que departure_date sea ISO string
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
    hoy = datetime.datetime.utcnow().date().isoformat()  # 'YYYY-MM-DD'
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
    1) Normalizamos y validamos el número.
    2) Detectamos si es consulta research o estado de vuelo.
    3) Si es research, redirigimos a /research.
    4) Si es vuelo:
       a) Si el número ya está en trips, construimos contexto y vamos a OpenAI.
       b) Si no, y Body coincide con un flight_number HOY, asociamos el teléfono y luego a OpenAI.
       c) Si no está en DB HOY, llamamos a AeroAPI para obtener estado del vuelo del día.
       d) Si Body no es vuelo, pedimos número de vuelo o localizador.
    """

    # ── 0) Normalizar y validar el número de teléfono que viene de Twilio:
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

    # ── 1) Branch: ¿consulta de research o estado de vuelo?
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

    else:
        # ── 2) Estado de vuelo / conversación con usuario
        trip = get_user_trip(From)
        if "error" in trip:
            # 2.1) El teléfono no está registrado → detectamos número de vuelo
            posible_flight = detect_flight_pattern(Body)
            if posible_flight:
                # 2.1.a) Buscamos ese vuelo en trips para el día de hoy
                found = find_today_trip_by_flight(posible_flight)
                if found:
                    # Asociamos el teléfono a esta fila
                    exito = associate_phone_to_trip(found["id"], phone)
                    if exito:
                        # Reconstruimos trip como si ya estuviera registrado
                        trip = {
                            "client_name": found["client_name"],
                            "flight_number": found["flight_number"],
                            "origin_iata": found["origin_iata"],
                            "destination_iata": found["destination_iata"],
                            "departure_date": found["departure_date"],
                            "status": found["status"]
                        }
                    else:
                        answer = (
                            "¡Ups! Hubo un problema al asociar tu número con el vuelo. "
                            "Por favor, inténtalo de nuevo más tarde."
                        )
                        twilio_client.messages.create(from_=TWILIO_WHATSAPP, to=From, body=answer)
                        return {"reply": answer}

                else:
                    # 2.1.b) No está en la DB para hoy → llamamos a AeroAPI
                    answer = fetch_flight_status_from_aeroapi(posible_flight)
                    twilio_client.messages.create(from_=TWILIO_WHATSAPP, to=From, body=answer)
                    return {"reply": answer}

            else:
                # 2.1.c) Ni vuelo ni research: pedimos registro o localizador
                answer = (
                    "¡Hola! No encuentro tu reserva. "
                    "Por favor, compárteme tu número de vuelo (por ejemplo: 'AR1234') "
                    "o tu localizador para poder ayudarte."
                )
                twilio_client.messages.create(from_=TWILIO_WHATSAPP, to=From, body=answer)
                return {"reply": answer}

        # ── 2.2) Si llegamos acá, 'trip' tiene datos válidos (teléfono registrado o recién asociado)
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

    # ── 3) Enviamos la respuesta final por WhatsApp (Twilio)
    try:
        twilio_client.messages.create(from_=TWILIO_WHATSAPP, to=From, body=answer)
    except Exception as e:
        raise HTTPException(500, f"Error al enviar WhatsApp: {e}")

    return {"reply": answer}


# ── Run local ──────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
