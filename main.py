# main.py – Travel Copilot API
#
# Endpoints:
#   GET  /health                    → {"ok": true}
#   POST /supabase/trip_created     → envía WhatsApp “confirmation”
#   POST /supabase/poll_flight      → dispara polling de estado de vuelo
#
# Lee vars de entorno:
#   SUPABASE_URL, SUPABASE_KEY
#   TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_WHATSAPP_NUMBER
#   AEROAPI_KEY

from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
from supabase import create_client
from twilio.rest import Client
from dotenv import load_dotenv
from datetime import datetime, timedelta
import httpx
import os

# Carga variables de entorno
load_dotenv()

# Instancia clientes
sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])
tw = Client(os.environ["TWILIO_ACCOUNT_SID"], os.environ["TWILIO_AUTH_TOKEN"])

# Cliente AeroAPI
AEROAPI_KEY = os.environ.get("AEROAPI_KEY")
AEROAPI_BASE = "https://aeroapi.flightaware.com/aeroapi"
client = httpx.Client(
    base_url=AEROAPI_BASE,
    headers={"x-apikey": AEROAPI_KEY},
    timeout=10.0
)

app = FastAPI()


# ---------- ENDPOINTS ----------
@app.api_route("/health", methods=["GET", "HEAD"] )
def health(request: Request):
    return {"ok": True}


@app.post("/supabase/trip_created")
async def trip_created(req: Request):
    payload = await req.json()
    trip = payload.get("record")
    if not trip or "id" not in trip:
        raise HTTPException(400, "invalid payload")
    sent = send_confirmation(trip["id"])
    return {"sent": sent}


@app.post("/supabase/poll_flight")
async def poll_flight(background_tasks: BackgroundTasks):
    """
    Dispara un ciclo de chequeo de estado de vuelo desde n8n.
    """
    background_tasks.add_task(run_due_checks)
    return {"status": "scheduled"}


# ---------- Mensajería ----------
def send_confirmation(trip_id: int) -> int:
    """
    Envía mensaje 'confirmation' a cada traveler del viaje.
    Devuelve la cantidad de envíos realizados.
    """
    trip = sb.table("trips").select("*").eq("id", trip_id).single().execute().data
    if not trip:
        return 0
    rows = sb.table("trip_travelers").select(
        "is_captain, traveler:travelers(id,name,whatsapp_number)"
    ).eq("trip_id", trip_id).execute().data
    if not rows:
        return 0
    dep = datetime.fromisoformat(trip["departure_date"]).replace(tzinfo=None)
    dep_date = dep.strftime("%d %b %H:%M")
    base = (
        f"✈️ Hola {{name}}! Tu viaje *{trip['title']}* "
        f"({trip['flight_number']}) sale el {dep_date}. "
        "Te avisaremos cualquier cambio. ¡Buen vuelo!"
    )
    sent = 0
    for row in rows:
        t = row["traveler"]
        to = f"whatsapp:{t['whatsapp_number']}"
        msg = tw.messages.create(
            body=base.replace("{name}", t["name"] or "viajero"),
            from_=os.environ["TWILIO_WHATSAPP_NUMBER"],
            to=to
        )
        sb.table("message_logs").insert({
            "trip_id": trip_id,
            "traveler_id": t["id"],
            "template": "confirmation",
            "status": msg.status,
            "sid": msg.sid
        }).execute()
        sent += 1
    return sent


def send_update(trip_id: int, flight_info: dict) -> int:
    """
    Envía mensaje 'flight_update' a cada traveler si cambia el status.
    """
    status = flight_info.get("status")
    trip = sb.table("trips").select("*").eq("id", trip_id).single().execute().data
    rows = sb.table("trip_travelers").select(
        "is_captain, traveler:travelers(id,name,whatsapp_number)"
    ).eq("trip_id", trip_id).execute().data
    if not rows:
        return 0
    dep = datetime.fromisoformat(trip["departure_date"]).replace(tzinfo=None)
    dep_date = dep.strftime("%d %b %H:%M")
    base = (
        f"✈️ Actualización de tu vuelo *{trip['title']}* "
        f"({trip['flight_number']}) programado para {dep_date}: now status *{status}*."
    )
    sent = 0
    for row in rows:
        t = row["traveler"]
        to = f"whatsapp:{t['whatsapp_number']}"
        msg = tw.messages.create(
            body=base,
            from_=os.environ["TWILIO_WHATSAPP_NUMBER"],
            to=to
        )
        sb.table("message_logs").insert({
            "trip_id": trip_id,
            "traveler_id": t["id"],
            "template": "flight_update",
            "status": msg.status,
            "sid": msg.sid
        }).execute()
        sent += 1
    return sent


# ---------- AeroAPI ----------
def fetch_flight_status(flight_number: str, departure_iso: str) -> dict:
    """
    Llama a AeroAPI usando ident_type=designator y start date (YYYY-MM-DD).
    Devuelve el primer objeto de 'flights'.
    """
    # Convertir departure_iso a fecha YYYY-MM-DD
    dep_dt = datetime.fromisoformat(departure_iso).replace(tzinfo=None)
    start_date = dep_dt.strftime("%Y-%m-%d")

    # Construir path sin end date
    url = f"/flights/{flight_number}?ident_type=designator&startDate={start_date}"

    resp = client.get(url)
    resp.raise_for_status()
    data = resp.json()
    flights = data.get("flights") or []
    if not flights:
        raise RuntimeError(f"No flights data for {flight_number} on {start_date}")
    return flights[0]

def log_flight_event(trip_id: int, event_type: str, metadata: dict) -> None:(trip_id: int, event_type: str, metadata: dict) -> None:
    sb.table("flight_events").insert({
        "trip_id": trip_id,
        "event": event_type,
        "metadata": metadata
    }).execute()


# ---------- Orquestación de polling ----------

def compute_next_check(dep: datetime, now: datetime) -> datetime:
    """
    Decide cuándo debe ser el siguiente chequeo dinámico.
    """
    rem = dep - now
    if rem > timedelta(hours=40):
        return dep - timedelta(hours=40)
    if rem > timedelta(hours=12):
        return now + timedelta(hours=10)
    if rem > timedelta(hours=3):
        return now + timedelta(hours=3)
    if rem > timedelta(hours=1):
        return now + timedelta(minutes=15)
    return now + timedelta(minutes=5)

async def run_due_checks():
    """
    Chequea todos los vuelos nuevos o programados, obtiene estado de AeroAPI,
    notifica si hay cambios y reprograma next_check_at.
    """
    try:
        now = datetime.utcnow()
        now_iso = now.isoformat()

        # 1) Viajes nuevos (next_check_at IS NULL)
        due_null = (
            sb.table("trips")
              .select("id,departure_date,flight_number")
              .filter("next_check_at", "is", "null")
              .execute()
              .data
        ) or []

        # 2) Viajes programados (next_check_at ≤ ahora)
        due_due = (
            sb.table("trips")
              .select("id,departure_date,flight_number")
              .lte("next_check_at", now_iso)
              .execute()
              .data
        ) or []

        # 3) Unión sin duplicados
        todos = {t["id"]: t for t in (due_null + due_due)}.values()

        # 4) Procesar cada viaje
        for trip in todos:
            dep_dt = datetime.fromisoformat(trip["departure_date"]).replace(tzinfo=None)

            # Llamada a AeroAPI con start y end
            try:
                start_ts = int(dep_dt.timestamp()) - 3600  # 1h antes
                end_ts = int(dep_dt.timestamp()) + 3600   # 1h después
                resp = client.get(
                    f"/flights/{trip['flight_number']}",
                    params={"ident_type": "designator", "start": start_ts, "end": end_ts}
                )
                resp.raise_for_status()
                data = resp.json().get("flights") or []
                if not data:
                    raise ValueError(f"No flights data for {trip['flight_number']}")
                flight_info = data[0]
            except Exception as e:
                print(f"⚠️ AeroAPI fetch failed for {trip['flight_number']}: {e}")
                # Reprogramar sencillo +15m y continuar
                next_time = now + timedelta(minutes=15)
                sb.table("trips").update({"next_check_at": next_time.isoformat()})\
                  .eq("id", trip["id"])\
                  .execute()
                continue

            # 5) Comparar con último estado
            last = sb.table("flight_events") \
                     .select("metadata") \
                     .eq("trip_id", trip["id"]) \
                     .order("created_at", desc=True) \
                     .limit(1) \
                     .execute().data
            prev_status = last[0]["metadata"].get("status") if last else None
            status = flight_info.get("status")
            if status != prev_status:
                send_update(trip["id"], flight_info)
                log_flight_event(trip["id"], "status_change", {"status": status})

            # 6) Reprogramar siguiente chequeo dinámico
            next_time = compute_next_check(dep_dt, now)
            sb.table("trips") \
              .update({"next_check_at": next_time.isoformat()}) \
              .eq("id", trip["id"]) \
              .execute()

    except Exception as e:
        print("🔥 Error en run_due_checks():", e)
