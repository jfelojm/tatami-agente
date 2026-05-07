import os

from fastapi import FastAPI, Form, Request
from fastapi.responses import PlainTextResponse, Response
from twilio.twiml.messaging_response import MessagingResponse

from agente_chat import responder

app = FastAPI(title="Tatami WhatsApp Webhook")


@app.get("/health", response_class=PlainTextResponse)
def health():
    return "ok"


@app.post("/whatsapp")
async def whatsapp_webhook(
    request: Request,
    Body: str = Form(default=""),
    From: str = Form(default=""),
):
    text_in = (Body or "").strip() or "(mensaje vacio)"

    try:
        text_out = responder(text_in)
    except Exception as e:
        text_out = f"Error procesando mensaje: {e}"

    max_len = int(os.getenv("WHATSAPP_MAX_CHARS", "1500") or "1500")
    if len(text_out) > max_len:
        text_out = text_out[: max_len - 3] + "..."

    tw = MessagingResponse()
    tw.message(text_out)
    return Response(content=str(tw), media_type="application/xml")

