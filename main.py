from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import os
import requests

app = FastAPI()

class ResearchRequest(BaseModel):
    question: str

class ResearchResponse(BaseModel):
    answer: str

@app.post("/research", response_model=ResearchResponse)
def research(req: ResearchRequest):
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise HTTPException(500, "Falta OPENAI_API_KEY en las variables de entorno")

    payload = {
        "model": "gpt-4o-mini",
        "messages": [
            {"role": "system", "content": "Eres un asistente de viajes experto."},
            {"role": "user", "content": req.question}
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
    answer = data["choices"][0]["message"]["content"]
    return {"answer": answer}
