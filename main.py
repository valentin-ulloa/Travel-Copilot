# main.py  – Travel Copilot API
#
# Endpoints:
#   GET  /health                    → {"ok": true}
#   POST /supabase/trip_created     → envía WhatsApp “confirmation”
#
# Lee vars de entorno:
#   SUPABASE_URL, SUPABASE_KEY
#   TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_WHATSAPP_NUMBER

from fastapi import FastAPI, Request, HTTPException
from datetime import datetime
from supabase import create_client
from twilio.rest import Client
from dotenv import load_dotenv
import os

# Carga .env si está en local (no afecta a Railway)
load_dotenv()

# --- clientes globales ---
sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])
tw = Client(os.environ["TWILIO_ACCOUNT_SID"], os.environ["TWILIO_AUTH_TOKEN"])

app = FastAPI()


# ---------- ENDPOINTS ----------
@app.get("/health")
def health():
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
    dep_date = datetime.fromisoformat(trip["departure_date"]).strftime("%d %b %H:%M")
    base = (f"✈️ Hola {{name}}! Tu viaje *{trip['title']}* "
            f"({trip['flight_number']}) sale el {dep_date}. "
            "Te avisaremos cualquier cambio. ¡Buen vuelo!")

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
