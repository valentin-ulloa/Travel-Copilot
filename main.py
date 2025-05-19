# main.py  – Travel Copilot API
#
# Endpoints:
#   GET  /health                    → {"ok": true}
#   POST /supabase/trip_created     → envía WhatsApp “confirmation”
#
# Lee vars de entorno:
#   SUPABASE_URL, SUPABASE_KEY
#   TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_WHATSAPP_NUMBER

from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
from supabase import create_client
from twilio.rest import Client
from dotenv import load_dotenv
from datetime import datetime, timedelta
import os

# Carga .env si está en local (no afecta a Railway)
load_dotenv()

# --- clientes globales ---
sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])
tw = Client(os.environ["TWILIO_ACCOUNT_SID"], os.environ["TWILIO_AUTH_TOKEN"])

app = FastAPI()


# ---------- ENDPOINTS ----------
@app.api_route("/health", methods=["GET", "HEAD"])
def health(request: Request):
    return {"ok": True}


def send_confirmation(trip_id: int) -> int:
    """
    Envía mensaje 'confirmation' a cada traveler del viaje.
    Devuelve la cantidad de envíos realizados.
    """
    # 1) Viaje
    trip = (
        sb.table("trips")
          .select("*")
          .eq("id", trip_id)
          .single()
          .execute()
          .data
    )
    if not trip:
        return 0

    # 2) Viajeros
    rows = (
        sb.table("trip_travelers")
          .select("is_captain, traveler:travelers(id, name, whatsapp_number)")
          .eq("trip_id", trip_id)
          .execute()
          .data
    )
    if not rows:
        return 0

    # 3) Texto
    dep_date = dep.strftime("%d %b %H:%M")
    base = (
        f"✈️ Hola {{name}}! Tu viaje *{trip['title']}* "
        f"({trip['flight_number']}) sale el {dep_date}. "
        "Te avisaremos cualquier cambio. ¡Buen vuelo!"
    )

    count = 0
    for row in rows:
        t   = row["traveler"]
        to  = f"whatsapp:{t['whatsapp_number']}"
        msg = tw.messages.create(
            body=base.replace("{name}", t["name"] or "viajero"),
            from_=os.environ["TWILIO_WHATSAPP_NUMBER"],
            to=to
        )
        count += 1

        # 4) Log
        sb.table("message_logs").insert({
            "trip_id":     trip_id,
            "traveler_id": t["id"],
            "template":    "confirmation",
            "status":      "queued",
            "sid":         msg.sid
        }).execute()
    return count


@app.post("/supabase/trip_created")
async def trip_created(req: Request):
    """
    Webhook que Supabase llama al hacer INSERT en trips.
    Payload ejemplo:
        { "type":"INSERT", "table":"trips",
          "record": { "id": 42, ... } }
    """
    payload = await req.json()
    trip = payload.get("record")
    if not trip or "id" not in trip:
        raise HTTPException(400, "invalid payload")

    sent = send_confirmation(trip["id"])
    return {"sent": sent}

# ─── Scheduler interno con APScheduler ───
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

        # 1) Viajes nuevos (next_check_at IS NULL)
        due_null = (
            sb.table("trips")
              .select("id,departure_date")
              .filter("next_check_at", "is", "null")    # <- aquí
              .execute()
              .data
        ) or []

        # 2) Viajes programados (next_check_at ≤ ahora)
        due_due = (
            sb.table("trips")
              .select("id,departure_date")
              .lte("next_check_at", now_iso)
              .execute()
              .data
        ) or []

        # 3) Unimos y quitamos duplicados
        todos = {t["id"]: t for t in (due_null + due_due)}.values()

        # 4) Reprogramamos each trip
        for trip in todos:
            dep_dt = datetime.fromisoformat(trip["departure_date"]).replace(tzinfo=None)
            next_time = compute_next_check(dep_dt, now)
            sb.table("trips") \
              .update({"next_check_at": next_time.isoformat()}) \
              .eq("id", trip["id"]) \
              .execute()

    except Exception as e:
        print("🔥 Error en run_due_checks():", e)

@app.post("/supabase/poll_flight")
async def poll_flight(background_tasks: BackgroundTasks):
    """
    Desencadena un ciclo de run_due_checks() en background.
    """
    background_tasks.add_task(run_due_checks)
    return {"status": "scheduled"}
