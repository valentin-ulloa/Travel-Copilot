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


from fastapi.responses import JSONResponse

@app.post("/supabase/poll_flight")
async def poll_flight():
    """
    Desencadena run_due_checks() y devuelve cualquier error para debugging.
    """
    try:
        # Ejecutar de forma síncrona para capturar errores
        run_due_checks()
        return {"status": "completed"}
    except Exception as e:
        # Devolver error JSON para facilitar debugging
        return JSONResponse(status_code=500, content={"error": str(e)})
