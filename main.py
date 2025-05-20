import os
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from supabase import create_client

# —————————————————————————————————————————————
# 1) Inicialización del cliente Supabase
#    Levanta las vars directamente desde el entorno de Railway.
# —————————————————————————————————————————————
try:
    SUPABASE_URL = os.environ["SUPABASE_URL"]
    SUPABASE_KEY = os.environ["SUPABASE_KEY"]
except KeyError as e:
    raise RuntimeError(f"Falta la variable de entorno {e}") from e

sb = create_client(SUPABASE_URL, SUPABASE_KEY)

# —————————————————————————————————————————————
# 2) Creación de la app FastAPI
# —————————————————————————————————————————————
app = FastAPI(title="Travel Copilot v0")


# —————————————————————————————————————————————
# 3) Modelo de entrada
# —————————————————————————————————————————————
class TripIn(BaseModel):
    agency_id: str
    client_name: str
    whatsapp: str               # "+5491112345678"
    flight_number: str          # "AR1234"
    origin_iata: str            # "EZE"
    destination_iata: str       # "JFK"
    departure_date: str         # ISO 8601


# —————————————————————————————————————————————
# 4) Healthcheck
# —————————————————————————————————————————————
@app.get("/health")
async def health():
    return {"status": "ok"}


# —————————————————————————————————————————————
# 5) Endpoint POST /trips
#    Inserta un viaje en la tabla `trips`
# —————————————————————————————————————————————
@app.post("/trips", status_code=201)
async def create_trip(trip: TripIn):
    payload = trip.dict()
    try:
        res = sb.table("trips").insert(payload).execute()
    except Exception as e:
        # Error de conexión, permisos, etc.
        raise HTTPException(status_code=500, detail=f"Supabase insert error: {e}")
    # Asumimos que si no hay excepción, res.data existe
    trip_id = res.data[0]["id"]
    return {"id": trip_id, "status": "created"}
