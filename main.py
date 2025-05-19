# main.py – Travel Copilot API
#
# Endpoints:
#   GET  /health                  → {"ok": true}
#   POST /supabase/trip_created   → envía WhatsApp “confirmation”
#   POST /supabase/poll_flight    → dispara polling y actualiza estados

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
from supabase import create_client
from twilio.rest import Client
from dotenv import load_dotenv
from datetime import datetime, timedelta
import httpx
import os

# Carga variables de entorno
load_dotenv()

# Clientes globales
sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])
tw = Client(os.environ["TWILIO_ACCOUNT_SID"], os.environ["TWILIO_AUTH_TOKEN"])

# Cliente AeroAPI
AEROAPI_KEY = os.environ.get("AEROAPI_KEY")
client = httpx.Client(
    base_url="https://aeroapi.flightaware.com/aeroapi",
    headers={"x-apikey": AEROAPI_KEY},
    timeout=10.0
)

app = FastAPI()

# ---------- Funciones de negocio ----------

def send_confirmation(trip_id: int) -> int:
    trip = sb.table("trips").select("*").eq("id", trip_id).single().execute().data
    if not trip:
        return 0
    rows = sb.table("trip_travelers").select(
        "is_captain, traveler:travelers(id,name,whatsapp_number)"
    ).eq("trip_id", trip_id).execute().data
    if not rows:
        return 0
    dep_dt = datetime.fromisoformat(trip["departure_date"]).replace(tzinfo=None)
    dep_str = dep_dt.strftime("%d %b %H:%M")
    template = (
        f"✈️ Hola {{name}}! Tu viaje *{trip['title']}* "
        f"({trip['flight_number']}) sale el {dep_str}. "
        "Te avisaremos cualquier cambio. ¡Buen vuelo!"
    )
    sent = 0
    for row in rows:
        t = row["traveler"]
        body = template.replace("{name}", t["name"] or "viajero")
        msg = tw.messages.create(
            body=body,
            from_=os.environ["TWILIO_WHATSAPP_NUMBER"],
            to=f"whatsapp:{t['whatsapp_number']}"
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
    trip = sb.table("trips").select("*").eq("id", trip_id).single().execute().data
    if not trip:
        return 0
    rows = sb.table("trip_travelers").select(
        "is_captain, traveler:travelers(id,name,whatsapp_number)"
    ).eq("trip_id", trip_id).execute().data
    if not rows:
        return 0
    dep_dt = datetime.fromisoformat(trip["departure_date"]).replace(tzinfo=None)
    dep_str = dep_dt.strftime("%d %b %H:%M")
    status = flight_info.get("status")
    template = (
        f"✈️ Actualización: tu vuelo *{trip['title']}* "
        f"({trip['flight_number']}) programado para {dep_str} "
        f"tiene nuevo estado *{status}*."
    )
    sent = 0
    for row in rows:
        t = row["traveler"]
        msg = tw.messages.create(
            body=template,
            from_=os.environ["TWILIO_WHATSAPP_NUMBER"],
            to=f"whatsapp:{t['whatsapp_number']}"
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


def fetch_flight_status(flight_number: str, departure_iso: str) -> dict:
    dep_dt = datetime.fromisoformat(departure_iso).replace(tzinfo=None)
    start_date = dep_dt.strftime("%Y-%m-%d")
    resp = client.get(
        f"/flights/{flight_number}?ident_type=designator&start={start_date}"
    )
    resp.raise_for_status()
    data = resp.json().get("flights") or []
    if not data:
        raise RuntimeError(f"No flights data for {flight_number} on {start_date}")
    return data[0]


def log_flight_event(trip_id: int, event_type: str, metadata: dict) -> None:
    sb.table("flight_events").insert({
        "trip_id": trip_id,
        "event": event_type,
        "metadata": metadata
    }).execute()


def compute_next_check(dep: datetime, now: datetime) -> datetime:
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


def run_due_checks():
    try:
        now = datetime.utcnow()
        now_iso = now.isoformat()
        due_null = sb.table("trips").select("id,departure_date,flight_number").filter("next_check_at","is","null").execute().data or []
        due_due = sb.table("trips").select("id,departure_date,flight_number").lte("next_check_at", now_iso).execute().data or []
        todos = {t["id"]: t for t in (due_null + due_due)}.values()
        for trip in todos:
            dep_dt = datetime.fromisoformat(trip["departure_date"]).replace(tzinfo=None)
            try:
                flight = fetch_flight_status(trip["flight_number"], trip["departure_date"])
            except Exception as e:
                print(f"⚠️ AeroAPI fetch failed for {trip['flight_number']}: {e}")
                next_time = now + timedelta(minutes=15)
                sb.table("trips").update({"next_check_at": next_time.isoformat()}).eq("id", trip["id"]).execute()
                continue
            last = sb.table("flight_events").select("metadata").eq("trip_id", trip["id"]).order("created_at", desc=True).limit(1).execute().data
            prev_status = last[0]["metadata"].get("status") if last else None
            status = flight.get("status")
            if status != prev_status:
                send_update(trip["id"], flight)
                log_flight_event(trip["id"], "status_change", {"status": status})
            next_time = compute_next_check(dep_dt, now)
            sb.table("trips").update({"next_check_at": next_time.isoformat()}).eq("id", trip["id"]).execute()
    except Exception as e:
        print("🔥 Error en run_due_checks():", e)

# ---------- Endpoints ----------
@app.api_route("/health", methods=["GET", "HEAD"] )
async def health(request: Request):
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
async def poll_flight():
    try:
        run_due_checks()
        return {"status": "completed"}
    except Exception as e:
        print(f"🔥 Error en run_due_checks(): {e}")
        raise HTTPException(status_code=500, detail=str(e))
