# main.py – Travel Copilot API
#
# Endpoints:
#   GET  /health                  → {"ok": true}
#   POST /supabase/trip_created   → envía WhatsApp “confirmation”
#   POST /supabase/poll_flight    → dispara polling y actualiza estados

from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
from fastapi.responses import JSONResponse
from supabase import create_client
from twilio.rest import Client
from dotenv import load_dotenv
from datetime import datetime, timedelta
import httpx
import os

# ─── Carga variables de entorno ──────────────────────────────────────────────────
load_dotenv()
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
TW_SID = os.environ["TWILIO_ACCOUNT_SID"]
TW_TOKEN = os.environ["TWILIO_AUTH_TOKEN"]
TW_WHATSAPP = os.environ["TWILIO_WHATSAPP_NUMBER"]
AEROAPI_KEY = os.environ.get("AEROAPI_KEY")

# ─── Clientes globales ───────────────────────────────────────────────────────────
sb = create_client(SUPABASE_URL, SUPABASE_KEY)
tw = Client(TW_SID, TW_TOKEN)
aero = httpx.Client(
    base_url="https://aeroapi.flightaware.com/aeroapi",
    headers={"x-apikey": AEROAPI_KEY},
    timeout=10.0
)

app = FastAPI()


# ─── Funciones de negocio ────────────────────────────────────────────────────────
def send_confirmation(trip_id: int) -> int:
    trip = sb.table("trips").select("*").eq("id", trip_id).single().execute().data
    if not trip:
        return 0

    rows = sb.table("trip_travelers")\
             .select("traveler:travelers(id,name,whatsapp_number)")\
             .eq("trip_id", trip_id).execute().data
    if not rows:
        return 0

    dep_dt = datetime.fromisoformat(trip["departure_date"]).replace(tzinfo=None)
    dep_str = dep_dt.strftime("%d %b %H:%M")
    template = (f"✈️ Hola {{name}}! Tu viaje *{trip['title']}* "
                f"({trip['flight_number']}) sale el {dep_str}. "
                "Te avisaremos cualquier cambio. ¡Buen vuelo!")

    sent = 0
    for row in rows:
        t = row["traveler"]
        body = template.replace("{name}", t["name"] or "viajero")
        msg = tw.messages.create(
            body=body,
            from_=TW_WHATSAPP,
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


def fetch_flight_status(flight_number: str, departure_iso: str) -> dict:
    # sólo date, sin hora, en formato YYYY-MM-DD
    dep_dt = datetime.fromisoformat(departure_iso).replace(tzinfo=None)
    day = dep_dt.strftime("%Y-%m-%d")
    url = f"/flights/{flight_number}?ident_type=designator&start={day}"
    resp = aero.get(url)
    resp.raise_for_status()
    flights = resp.json().get("flights") or []
    if not flights:
        raise RuntimeError(f"No data for {flight_number} on {day}")
    return flights[0]


def send_update(trip_id: int, status: str) -> None:
    trip = sb.table("trips").select("*").eq("id", trip_id).single().execute().data
    rows = sb.table("trip_travelers")\
             .select("traveler:travelers(id,name,whatsapp_number)")\
             .eq("trip_id", trip_id).execute().data
    dep_dt = datetime.fromisoformat(trip["departure_date"]).replace(tzinfo=None)
    dep_str = dep_dt.strftime("%d %b %H:%M")
    template = (f"✈️ Actualización: tu vuelo *{trip['title']}* "
                f"({trip['flight_number']}) programado para {dep_str} "
                f"tiene nuevo estado *{status}*.")
    for row in rows:
        t = row["traveler"]
        tw.messages.create(
            body=template,
            from_=TW_WHATSAPP,
            to=f"whatsapp:{t['whatsapp_number']}"
        )
    sb.table("message_logs").insert({
        "trip_id":      trip_id,
        "traveler_id":  rows[0]["traveler"]["id"],
        "template":     "flight_update",
        "status":       status,
        "sid":          None
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
    print(f"🔄 run_due_checks @ {datetime.utcnow().isoformat()}")
    now = datetime.utcnow()
    now_iso = now.isoformat()

    # 1) Viajes nuevos
    due_null = sb.table("trips")\
                 .select("id,departure_date,flight_number")\
                 .is_("next_check_at", None)\
                 .execute().data or []

    # 2) Viajes programados
    due_due = sb.table("trips")\
                .select("id,departure_date,flight_number")\
                .lte("next_check_at", now_iso)\
                .execute().data or []

    todos = {t["id"]: t for t in (due_null + due_due)}.values()
    print(f"👀 trips to check: {len(todos)} → {[t['id'] for t in todos]}")

    for trip in todos:
        try:
            info = fetch_flight_status(trip["flight_number"], trip["departure_date"])
            status = info.get("status")
            # Aquí podrías comparar con último event y decidir si enviar update
            send_update(trip["id"], status)
        except Exception as e:
            print(f"⚠️ AeroAPI fetch failed for {trip['flight_number']}: {e}")

        next_t = compute_next_check(
            datetime.fromisoformat(trip["departure_date"]).replace(tzinfo=None),
            now
        )
        sb.table("trips")\
          .update({"next_check_at": next_t.isoformat()})\
          .eq("id", trip["id"])\
          .execute()


# ─── Endpoints ─────────────────────────────────────────────────────────────────
@app.api_route("/health", methods=["GET", "HEAD"])
def health():
    return {"ok": True}


@app.post("/supabase/trip_created")
async def trip_created(req: Request):
    payload = await req.json()
    rec = payload.get("record", {})
    tid = rec.get("id")
    if not tid:
        raise HTTPException(400, "invalid payload")
    sent = send_confirmation(tid)
    return {"sent": sent}


@app.post("/supabase/poll_flight")
async def poll_flight(background: BackgroundTasks):
    background.add_task(run_due_checks)
    return {"status": "scheduled"}
