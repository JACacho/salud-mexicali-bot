import os
import requests
from flask import Flask, request

app = Flask(__name__)

META_TOKEN = os.getenv("META_TOKEN")
META_PHONE_ID = os.getenv("META_PHONE_ID")
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "SaludMexicali2026")

def enviar_texto(destino, texto):
    url = f"https://graph.facebook.com/v21.0/{META_PHONE_ID}/messages"
    headers = {"Authorization": f"Bearer {META_TOKEN}"}
    data = {"messaging_product": "whatsapp", "to": destino,
            "type": "text", "text": {"body": texto}}
    r = requests.post(url, json=data, headers=headers)
    print("Meta respondió:", r.status_code)
    return r.status_code

@app.route("/")
def inicio():
    return "Salud Mexicali bot vivo ❤️ v1.0"

@app.route("/webhook", methods=["GET"])
def verificar():
    if request.args.get("hub.verify_token") == VERIFY_TOKEN:
        return request.args.get("hub.challenge"), 200
    return "no autorizado", 403

@app.route("/webhook", methods=["POST"])
def recibir():
    data = request.get_json(force=True)
    try:
        valor = data["entry"][0]["changes"][0]["value"]
        mensaje = valor["messages"][0]
        de = mensaje["from"]
        if mensaje["type"] == "text":
            enviar_texto(de, "❤️ Salud Mexicali recibió tu mensaje. (v1.0 en prueba)")
        else:
            enviar_texto(de, "❤️ Recibí tu archivo. En v2.0 leeré fotos y audios.")
    except Exception as e:
        print("Aviso:", e)
    return "OK", 200