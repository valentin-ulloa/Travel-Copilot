import os
import requests
from fastapi import FastAPI, Form, HTTPException
from pydantic import BaseModel
from twilio.rest import Client

app = FastAPI()

# Inicializo Twilio
twilio_client = Client(
    os.getenv("TWILIO_ACCOUNT_SID"),
    os.getenv("TWILIO_AUTH_TOKEN")
)
TWILIO_WHATSAPP = os.getenv("TWILIO_WHATSAPP_NUMBER")

# Modelo para /research
class ResearchRequest(BaseModel):
    question: str

class ResearchResponse(BaseModel):
    answer: str

@app.post("/research", response_model=ResearchResponse)
def research(req: ResearchRequest):
    api_key = os.getenv("OPENAI_API_KEY")
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
    resp = requests.post(
        "https://api.openai.com/v1/chat/completions",
        json=payload,
        headers=headers
    )
    if resp.status_code != 200:
        raise HTTPException(resp.status_code, resp.text)
    data = resp.json()
    return {"answer": data["choices"][0]["message"]["content"]}

# Heurística sencilla para detectar preguntas
def is_research_query(text: str) -> bool:
    lo = text.strip().lower()
    return "?" in lo or any(lo.startswith(w) for w in ["qué","cómo","dónde","cuándo","por qué","cual"])

# Endpoint que Twilio llamará por cada mensaje de WhatsApp
@app.post("/webhook")
def whatsapp_webhook(
    From: str = Form(...),
    Body: str = Form(...),
):
    print(f"----✅ Webhook recibido: From={From!r} Body={Body!r}")

    # Decide ruta
    if is_research_query(Body):
        print("   ↳ Ruta: research")

        r = requests.post(
            f"https://{os.getenv('RAILWAY_STATIC_URL')}/research",
            json={"question": Body},
            headers={"Content-Type": "application/json"}
        )
        print("   ↳ /research status:", r.status_code, "body:", r.text[:200])

        # ⬇️  Fallback por si OpenAI falla
        if r.status_code == 200:
            answer = r.json().get("answer", "").strip()
            if not answer:
                answer = "Lo siento, no pude obtener la información ahora. Inténtalo más tarde."
        else:
            answer = "Lo siento, hubo un problema al buscar la información. Inténtalo más tarde."

    else:
        print("   ↳ Ruta: flight-status (placeholder)")
        answer = "Lo siento, aún no manejo preguntas de vuelo aquí."

    # Intenta enviar con Twilio
    try:
        print(f"----✉️ Enviando WhatsApp a {From}: {answer!r}")
        msg = twilio_client.messages.create(
            from_=TWILIO_WHATSAPP,
            to=From,
            body=answer
        )
        print(f"----✅ Outbound Twilio SID: {msg.sid}")
    except Exception as e:
        print(f"----❌ Error al enviar con Twilio: {type(e).__name__} – {e}")

    return {"status": "ok"}
