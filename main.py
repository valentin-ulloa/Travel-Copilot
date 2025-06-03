import os
import json
import datetime

import requests
from fastapi import FastAPI, Form, HTTPException
from pydantic import BaseModel

from supabase import create_client, Client as SupabaseClient
from twilio.rest import Client as TwilioClient

OPENAI_API_KEY      = os.getenv("OPENAI_API_KEY")
SUPABASE_URL        = os.getenv("SUPABASE_URL")
SUPABASE_KEY        = os.getenv("SUPABASE_KEY")
TWILIO_ACCOUNT_SID  = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN   = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_WHATSAPP     = os.getenv("TWILIO_WHATSAPP_NUMBER")

if not (OPENAI_API_KEY and SUPABASE_URL and SUPABASE_KEY and TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and TWILIO_WHATSAPP):
    raise RuntimeError("Faltan variables de entorno. Revisa tu .env o tu configuración en Railway.")


supabase: SupabaseClient = create_client(SUPABASE_URL, SUPABASE_KEY)
twilio_client: TwilioClient = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)


class ResearchRequest(BaseModel):
    question: str

class ResearchResponse(BaseModel):
    answer: str

class FunctionCall(BaseModel):
    name: str
    arguments: dict

class OpenAIResponse(BaseModel):
    reply: str

# ------------------------------
# UTIL: ¿Es query de “research” o de “flight-status”?
# ------------------------------

def is_research_query(text: str) -> bool:
    lo = text.strip().lower()
    return "?" in lo or any(lo.startswith(w) for w in ["qué","cómo","dónde","cuándo","por qué","cual"])

# ------------------------------
# TOOL: obtener el viaje del usuario desde Supabase
# ------------------------------

def get_user_trip(phone_number: str) -> dict:
    """
    Busca en Supabase la fila de 'trips' donde whatsapp = phone_number.
    Retorna un dict con datos del viaje o con 'error' si no hay.
    """
    try:
        resp = supabase.table("trips") \
                       .select("client_name, flight_number, origin_iata, destination_iata, departure_date, status") \
                       .eq("whatsapp", phone_number) \
                       .single() \
                       .execute()
    except Exception as e:
        return {"error": f"Error al consultar Supabase: {str(e)}"}

    data = resp.data
    if data is None:
        return {"error": "No se encontró ningún viaje registrado para tu número."}

    # Convertir departure_date a string ISO si viene como datetime
    dep = data.get("departure_date")
    if isinstance(dep, (datetime.date, datetime.datetime)):
        data["departure_date"] = dep.isoformat()
    return data

# ------------------------------
# DEFINICIÓN DE FUNCIONES PARA LLAMADAS “FUNCTION CALLING”
# ------------------------------

FUNCTIONS = [
    {
        "name": "get_user_trip",
        "description": "Obtiene la información del viaje (vuelo) de un usuario dado su número de WhatsApp.",
        "parameters": {
            "type": "object",
            "properties": {
                "phone_number": {
                    "type": "string",
                    "description": "Número de WhatsApp en formato 'whatsapp:+549XXXXXXX'"
                }
            },
            "required": ["phone_number"]
        }
    }
]

# ------------------------------
# FASTAPI INSTANCE
# ------------------------------

app = FastAPI()

# ------------------------------
# ENDPOINT DE “RESEARCH” YA EXISTENTE
# ------------------------------

@app.post("/research", response_model=ResearchResponse)
def research(req: ResearchRequest):
    api_key = OPENAI_API_KEY
    if not api_key:
        raise HTTPException(500, "Falta OPENAI_API_KEY")

    payload = {
        "model": "gpt-4o-mini",
        "messages": [
            {"role": "system", "content": "Eres un asistente de viajes experto."},
            {"role": "user",   "content": req.question}
        ]
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    resp = requests.post("https://api.openai.com/v1/chat/completions", json=payload, headers=headers)
    if resp.status_code != 200:
        raise HTTPException(resp.status_code, resp.text)
    data = resp.json()
    return {"answer": data["choices"][0]["message"]["content"]}

# ------------------------------
# ENDPOINT DEL WEBHOOK PARA WHATSAPP (Twilio)
# ------------------------------

@app.post("/webhook", response_model=OpenAIResponse)
def whatsapp_webhook(
    From: str = Form(...),
    Body: str = Form(...),
):
    """
    Recibe el webhook de Twilio (POST x-www-form-urlencoded).
    1) Si is_research_query(Body) → redirige internamente a /research.
    2) Si no, ejecuta un Agent con Function Calling para obtener el estado del vuelo:
       a) ChatCompletion primero: ¿debo llamar a get_user_trip?(auto)
       b) Si el modelo invoca get_user_trip, ejecutamos la función y re‐llamamos al modelo.
       c) Enviamos la respuesta por Twilio.
    """

    # 1️⃣ Si es pregunta genérica → proxy a /research (mismo código que antes)
    if is_research_query(Body):
        try:
            r = requests.post(
                f"https://{os.getenv('RAILWAY_STATIC_URL')}/research",
                json={"question": Body},
                headers={"Content-Type": "application/json"}
            )
        except Exception:
            answer = "Lo siento, hubo un problema al buscar la información. Inténtalo más tarde."
        else:
            if r.status_code == 200:
                answer = r.json().get("answer", "").strip() or "Lo siento, no pude obtener la información ahora."
            else:
                answer = "Lo siento, hubo un problema al buscar la información. Inténtalo más tarde."

    # 2️⃣ Si NO es pregunta de research → voy por el “flight‐status”
    else:
        # Mensajes iniciales para ChatCompletion
        system_message = {
            "role": "system",
            "content": (
                "Eres Travel Copilot, un asistente de viajes que ayuda a los usuarios "
                "a conocer el estado de su vuelo. Puedes consultar el viaje del usuario "
                "llamando a la función get_user_trip con su número de WhatsApp."
            )
        }
        user_message = {"role": "user", "content": Body}

        # Primer llamado al modelo con posibilidad de function_call
        try:
            initial_resp = requests.post(
                "https://api.openai.com/v1/chat/completions",
                json={
                    "model": "gpt-4o-mini",
                    "messages": [system_message, user_message],
                    "functions": FUNCTIONS,
                    "function_call": "auto"
                },
                headers={
                    "Authorization": f"Bearer {OPENAI_API_KEY}",
                    "Content-Type": "application/json"
                }
            ).json()
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"OpenAI error: {e}")

        message_0 = initial_resp["choices"][0]["message"]

        # Si el modelo decide invocar a la función get_user_trip
        if "function_call" in message_0:
            call = FunctionCall.parse_obj(message_0["function_call"])
            if call.name == "get_user_trip":
                phone_arg = call.arguments.get("phone_number")
                trip_info = get_user_trip(phone_arg)

                # Construir segundo turno para que el modelo genere la respuesta final
                messages_for_second_call = [
                    system_message,
                    user_message,
                    {
                        "role": "assistant",
                        "content": None,
                        "function_call": {
                            "name": "get_user_trip",
                            "arguments": json.dumps({"phone_number": phone_arg})
                        }
                    },
                    {
                        "role": "function",
                        "name": "get_user_trip",
                        "content": json.dumps(trip_info)
                    }
                ]
                try:
                    second_resp = requests.post(
                        "https://api.openai.com/v1/chat/completions",
                        json={
                            "model": "gpt-4o-mini",
                            "messages": messages_for_second_call
                        },
                        headers={
                            "Authorization": f"Bearer {OPENAI_API_KEY}",
                            "Content-Type": "application/json"
                        }
                    ).json()
                except Exception as e:
                    raise HTTPException(status_code=500, detail=f"OpenAI error: {e}")

                answer = second_resp["choices"][0]["message"]["content"]
            else:
                answer = "Lo siento, no puedo procesar esa solicitud."
        else:
            # Si no hubo función pedida, devolvemos lo que el modelo generó
            answer = message_0.get("content", "Lo siento, no entendí tu solicitud.")

    # 3️⃣ Enviar respuesta por WhatsApp via Twilio
    try:
        twilio_client.messages.create(
            from_=TWILIO_WHATSAPP,
            to=From,
            body=answer
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error al enviar WhatsApp: {e}")

    return {"reply": answer}

# ------------------------------
# EJECUTABLE CON UVICORN
# ------------------------------

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
