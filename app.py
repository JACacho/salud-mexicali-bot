import os, re, json, time, base64, struct, threading
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

GEMINI_KEYS = [k for k in [os.getenv("GEMINI_KEY_A"), os.getenv("GEMINI_KEY_B")] if k]
GROQ_KEY = os.getenv("GROQ_KEY")
META_TOKEN = os.getenv("META_TOKEN")
META_PHONE_ID = os.getenv("META_PHONE_ID")
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "SaludMexicali2026")

MODELOS_GEMINI = ["gemini-2.5-flash", "gemini-2.0-flash", "gemini-flash-latest"]
GEMINI_TTS = "gemini-2.5-flash-preview-tts"
GROQ_TEXT = "llama-3.3-70b-versatile"
GROQ_VISION = "llama-4-maverick-17b-128e"

USO = {"gemini_a":0,"gemini_b":0,"groq":0,"web":0,"whatsapp":0,"fotos":0,"voces":0,"audios":0}
ERRORES = []
BITACORA = []
PAC = {}
LOCK = threading.Lock()

def contar(k):
    with LOCK: USO[k] = USO.get(k, 0) + 1

def fallo(msg):
    ERRORES.append(time.strftime("%H:%M") + " " + str(msg)[:200])
    if len(ERRORES) > 20: ERRORES.pop(0)

SYSTEM = ("Eres 'Salud Mexicali', asistente calido de salud para adultos mayores con hipertension y diabetes, en espanol de Mexico.\n"
"Habla con frases cortas, carinosas y claras. Si conoces el nombre del paciente, usalo con carino.\n"
"Criterios por defecto (adulto mayor): normal: TA hasta 139/89 y glucosa 70-180; moderado: TA 140-159/90-99 o glucosa 181-250; critico: TA 160 o mas, o glucosa mayor a 250 o menor a 70, o sintomas como dolor de pecho, confusion o vision borrosa.\n"
"Si es critico: pide con carino que se vuelva a medir en 5 minutos sentado y avisa que notificaras a su familia.\n"
"Al final agrega SIEMPRE, en lineas separadas, exactamente:\n"
"TRIAGE:normal  (o TRIAGE:moderado o TRIAGE:critico)\n"
"y si detectas numeros: VALORES: TA=130/80 PULSO=76 GLUCOSA=110 (solo los que veas).")

def limpiar(txt):
    triage = "normal"
    m = re.search(r"TRIAGE:\s*(normal|moderado|critico)", txt or "", re.I)
    if m: triage = m.group(1).lower()
    valores = {}
    mv = re.search(r"VALORES:(.+)", txt or "")
    if mv:
        s = mv.group(1)
        ta = re.search(r"TA\s*=?\s*(\d{2,3})\s*/\s*(\d{2,3})", s)
        if ta: valores["ta"] = ta.group(1) + "/" + ta.group(2)
        pu = re.search(r"PULSO\s*=?\s*(\d{2,3})", s)
        if pu: valores["pulso"] = pu.group(1)
        gl = re.search(r"GLUCOSA\s*=?\s*(\d{2,3})", s)
        if gl: valores["glucosa"] = gl.group(1)
    txt = re.sub(r"TRIAGE:\s*(normal|moderado|critico)", "", txt or "", flags=re.I)
    txt = re.sub(r"VALORES:.+", "", txt or "")
    return txt.strip(), triage, valores

def pcm_to_wav(pcm, rate=24000):
    return (b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVEfmt " +
            struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16) +
            b"data" + struct.pack("<I", len(pcm)) + pcm)

def datos_pac(raw):
    try:
        d = json.loads(raw or "{}")
        return d.get("n", ""), d.get("t", "")
    except Exception:
        return "", ""

def registrar(pid, nombre):
    if pid:
        p = PAC.setdefault(pid, {"nombre": nombre or "cariño", "hist": []})
        if nombre: p["nombre"] = nombre
        return p
    return None

def contexto(p):
    if not p: return ""
    hist = " | ".join([h for h in p["hist"][-5:]])
    return ("\nDATOS DEL PACIENTE: nombre=" + p["nombre"] + ". Su historia reciente: " + hist +
            "\nLlamalo por su nombre y recuerda su historia.")

def recordar(p, linea):
    if p:
        p["hist"].append(linea)
        if len(p["hist"]) > 10: p["hist"] = p["hist"][-10:]

def gemini_gen(parts, key, modelo):
    r = requests.post(
        "https://generativelanguage.googleapis.com/v1beta/models/" + modelo + ":generateContent",
        headers={"x-goog-api-key": key},
        json={"system_instruction": {"parts": [{"text": SYSTEM}]},
              "contents": [{"parts": parts}],
              "generationConfig": {"temperature": 0.4}}, timeout=90)
    r.raise_for_status()
    return r.json()["candidates"][0]["content"]["parts"][0]["text"]

def generar_texto(prompt):
    for i, key in enumerate(GEMINI_KEYS):
        for mod in MODELOS_GEMINI:
            try:
                t = gemini_gen([{"text": prompt}], key, mod)
                contar("gemini_a" if i == 0 else "gemini_b")
                return t
            except Exception as e:
                fallo("gemini" + ("A" if i == 0 else "B") + "/" + mod + ": " + str(e))
    if GROQ_KEY:
        try:
            r = requests.post("https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": "Bearer " + GROQ_KEY},
                json={"model": GROQ_TEXT, "messages": [
                    {"role": "system", "content": SYSTEM},
                    {"role": "user", "content": prompt}]}, timeout=90)
            r.raise_for_status()
            contar("groq")
            return r.json()["choices"][0]["message"]["content"]
        except Exception as e:
            fallo("groq: " + str(e))
    return None

def generar_foto(b64, mime):
    for i, key in enumerate(GEMINI_KEYS):
        for mod in MODELOS_GEMINI:
            try:
                t = gemini_gen([{"text": "El paciente manda una FOTO de su aparato. Lee con cuidado los numeros y acompana."},
                                {"inline_data": {"mime_type": mime, "data": b64}}], key, mod)
                contar("gemini_a" if i == 0 else "gemini_b")
                return t
            except Exception as e:
                fallo("foto gemini" + ("A" if i == 0 else "B") + "/" + mod + ": " + str(e))
    if GROQ_KEY:
        try:
            r = requests.post("https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": "Bearer " + GROQ_KEY},
                json={"model": GROQ_VISION, "messages": [
                    {"role": "system", "content": SYSTEM},
                    {"role": "user", "content": [
                        {"type": "text", "text": "Lee los numeros de esta foto de un tensiometro o glucometro y acompana."},
                        {"type": "image_url", "image_url": {"url": "data:" + mime + ";base64," + b64}}]}]}, timeout=90)
            r.raise_for_status()
            contar("groq")
            return r.json()["choices"][0]["message"]["content"]
        except Exception as e:
            fallo("foto groq: " + str(e))
    return None

def generar_voz(audio, mime):
    for i, key in enumerate(GEMINI_KEYS):
        for mod in MODELOS_GEMINI:
            try:
                t = gemini_gen([{"text": "El paciente manda una NOTA DE VOZ. Escuchala, entiende sus numeros o su duda y acompana."},
                                {"inline_data": {"mime_type": mime, "data": base64.b64encode(audio).decode()}}], key, mod)
                contar("gemini_a" if i == 0 else "gemini_b")
                return t
            except Exception as e:
                fallo("voz gemini" + ("A" if i == 0 else "B") + "/" + mod + ": " + str(e))
    if GROQ_KEY:
        try:
            r = requests.post("https://api.groq.com/openai/v1/audio/transcriptions",
                headers={"Authorization": "Bearer " + GROQ_KEY},
                files={"file": ("voz.webm", audio, mime)},
                data={"model": "whisper-large-v3"}, timeout=90)
            r.raise_for_status()
            contar("groq")
            return generar_texto("El paciente dijo por voz: " + r.json().get("text", ""))
        except Exception as e:
            fallo("voz groq: " + str(e))
    return None

def tts(texto):
    for key in GEMINI_KEYS:
        try:
            r = requests.post(
                "https://generativelanguage.googleapis.com/v1beta/models/" + GEMINI_TTS + ":generateContent",
                headers={"x-goog-api-key": key},
                json={"contents": [{"parts": [{"text": texto}]}],
                      "generationConfig": {"response_modalities": ["AUDIO"],
                                           "speech_config": {"voice_config": {"prebuilt_voice_config": {"voice_name": "Leda"}}}}},
                timeout=90)
            r.raise_for_status()
            part = r.json()["candidates"][0]["content"]["parts"][0]
            d = part.get("inline_data", {})
            pcm = base64.b64decode(d.get("data", ""))
            rate = 24000
            mr = re.search(r"rate=(\d+)", d.get("mime_type", ""))
            if mr: rate = int(mr.group(1))
            contar("audios")
            return base64.b64encode(pcm_to_wav(pcm, rate)).decode()
        except Exception as e:
            fallo("tts: " + str(e))
    return None

def finalizar(txt_crudo, canal, tipo, usuario, pid, nombre):
    p = registrar(pid, nombre)
    if not txt_crudo:
        return jsonify({"texto": "Ups, carino, no te escuche bien. Intenta otra vez, por favor.", "audio": None})
    texto, triage, valores = limpiar(txt_crudo)
    recordar(p, tipo + " " + usuario + " -> " + triage + " " + json.dumps(valores))
    BITACORA.append({"ts": time.strftime("%Y-%m-%d %H:%M"), "canal": canal, "pac": pid,
                     "usuario": usuario, "bot": texto, "triage": triage, "valores": valores})
    audio = tts(texto)
    return jsonify({"texto": texto, "audio": audio, "triage": triage, "valores": valores})

@app.route("/")
def inicio():
    return HTML

@app.route("/manifest.webmanifest")
def manifest():
    return jsonify({"name": "Salud Mexicali", "short_name": "SaludMex", "start_url": "/",
                    "display": "standalone", "background_color": "#0f274d", "theme_color": "#0f274d",
                    "icons": [{"src": "/icon.svg", "sizes": "any", "type": "image/svg+xml"}]})

@app.route("/icon.svg")
def icon():
    return '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><rect width="100" height="100" rx="20" fill="#0f274d"/><text x="50" y="68" font-size="55" text-anchor="middle">❤️</text></svg>', 200, {"Content-Type": "image/svg+xml"}

@app.route("/sw.js")
def sw():
    return "self.addEventListener('install',e=>self.skipWaiting());self.addEventListener('fetch',e=>{});", 200, {"Content-Type": "text/javascript"}

@app.route("/api/text", methods=["POST"])
def api_text():
    contar("web")
    d = request.get_json(force=True)
    t = d.get("texto", "")
    n, tel = datos_pac(d.get("pac", ""))
    return finalizar(generar_texto(contexto(PAC.get(tel or n)) + "\nEl paciente escribe: " + t), "web", "texto", t, tel or n, n)

@app.route("/api/foto", methods=["POST"])
def api_foto():
    contar("web"); contar("fotos")
    f = request.files.get("foto")
    n, tel = datos_pac(request.form.get("pac", ""))
    b64 = base64.b64encode(f.read()).decode()
    return finalizar(generar_foto(b64, f.mimetype or "image/jpeg"), "web", "foto", "(foto)", tel or n, n)

@app.route("/api/voz", methods=["POST"])
def api_voz():
    contar("web"); contar("voces")
    f = request.files.get("audio")
    n, tel = datos_pac(request.form.get("pac", ""))
    return finalizar(generar_voz(f.read(), f.mimetype or "audio/webm"), "web", "voz", "(voz)", tel or n, n)

@app.route("/stats")
def stats():
    return jsonify({"uso": USO, "errores": ERRORES, "pacientes": list(PAC.keys()), "bitacora": BITACORA[-50:]})

@app.route("/webhook", methods=["GET"])
def verificar():
    if request.args.get("hub.verify_token") == VERIFY_TOKEN:
        return request.args.get("hub.challenge"), 200
    return "no autorizado", 403

def enviar_wa(destino, texto):
    contar("whatsapp")
    requests.post("https://graph.facebook.com/v21.0/" + str(META_PHONE_ID) + "/messages",
        headers={"Authorization": "Bearer " + str(META_TOKEN)},
        json={"messaging_product": "whatsapp", "to": destino, "type": "text", "text": {"body": texto}})

@app.route("/webhook", methods=["POST"])
def recibir():
    data = request.get_json(force=True)
    try:
        msg = data["entry"][0]["changes"][0]["value"]["messages"][0]
        de = msg["from"]
        p = registrar(de, "")
        if msg["type"] == "text":
            crudo = generar_texto(contexto(p) + "\nEl paciente escribe por WhatsApp: " + msg["text"]["body"])
        elif msg["type"] == "image":
            mid = msg["image"]["id"]
            r1 = requests.get("https://graph.facebook.com/v21.0/" + mid, headers={"Authorization": "Bearer " + str(META_TOKEN)})
            r2 = requests.get(r1.json()["url"], headers={"Authorization": "Bearer " + str(META_TOKEN)})
            crudo = generar_foto(base64.b64encode(r2.content).decode(), "image/jpeg")
        else:
            enviar_wa(de, "Recibi tu mensaje, carino. En esta version leo fotos y texto.")
            return "OK", 200
        texto, triage, valores = limpiar(crudo or "")
        recordar(p, msg["type"] + " -> " + triage + " " + json.dumps(valores))
        BITACORA.append({"ts": time.strftime("%Y-%m-%d %H:%M"), "canal": "whatsapp",
                         "usuario": msg.get("text", {}).get("body", "(foto)"), "bot": texto, "triage": triage, "valores": valores})
        enviar_wa(de, texto)
    except Exception as e:
        fallo("webhook: " + str(e))
    return "OK", 200

HTML = """<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="theme-color" content="#0f274d">
<link rel="manifest" href="/manifest.webmanifest">
<link rel="icon" href="/icon.svg">
<title>Salud Mexicali ❤️</title>
<style>
 body{margin:0;font-family:Arial;font-size:20px;background:#f4f6fb;display:flex;flex-direction:column;height:100vh}
 header{background:#0f274d;color:#fff;padding:14px;text-align:center;font-size:24px}
 #chat{flex:1;overflow-y:auto;padding:14px;display:flex;flex-direction:column;gap:10px}
 .b{max-width:80%;padding:12px 16px;border-radius:18px;line-height:1.4}
 .yo{align-self:flex-end;background:#0f274d;color:#fff}
 .bot{align-self:flex-start;background:#fff;box-shadow:0 1px 4px rgba(0,0,0,.15)}
 .oir{font-size:22px;margin-top:6px}
 #bar{display:flex;gap:8px;padding:10px;background:#fff;border-top:2px solid #dde}
 #bar input{flex:1;font-size:20px;padding:12px;border-radius:12px;border:2px solid #bbc}
 #bar button{font-size:24px;border:none;border-radius:12px;background:#0f274d;color:#fff;padding:0 16px}
 #inst{display:none;margin:8px auto;background:#e8f5e9;border:2px solid #4c8;padding:8px 16px;font-size:18px;border-radius:12px}
 #ficha{margin:10px auto;background:#fff;padding:14px;border-radius:14px;box-shadow:0 1px 6px rgba(0,0,0,.2);text-align:center}
 #ficha input{font-size:20px;margin:6px;padding:10px;border-radius:10px;border:2px solid #bbc;display:block}
</style>
</head>
<body>
<header>❤️ Salud Mexicali</header>
<button id="inst">📲 Instalar como app</button>
<div id="chat"></div>
<div id="ficha" style="display:none">
 <b>Presentate para que te recuerde:</b>
 <input id="fnom" placeholder="Tu nombre">
 <input id="ftel" placeholder="Tu telefono (opcional)">
 <button id="fok" style="font-size:20px;padding:8px 20px;border-radius:10px;border:none;background:#0f274d;color:#fff">Guardar</button>
</div>
<div id="bar">
 <button id="bfoto">📷</button>
 <button id="bvoz">🎤</button>
 <input id="txt" placeholder="Escribe aqui...">
 <button id="benv">➤</button>
 <input type="file" id="ffoto" accept="image/*" hidden>
</div>
<script>
const chat=document.getElementById('chat');
const pac=()=>localStorage.getItem('pac')||'';
function pinta(q,t){const d=document.createElement('div');d.className='b '+(q?'yo':'bot');d.innerHTML=t;chat.appendChild(d);chat.scrollTop=chat.scrollHeight;return d}
function suena(b){if(!b)return;new Audio('data:audio/wav;base64,'+b).play()}
function botMsg(d){const t=(d.texto||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/\\n/g,'<br>');
 pinta(false,t+(d.audio?'<br><button class="oir" data-a="'+d.audio+'">🔊 Oir</button>':''));suena(d.audio)}
async function api(url,body){pinta(false,'...');const r=await fetch(url,{method:'POST',body});const d=await r.json();chat.lastChild.remove();botMsg(d)}
if(!pac()){document.getElementById('ficha').style.display='block'}
document.getElementById('fok').onclick=()=>{const n=document.getElementById('fnom').value.trim()||'cariño';
 localStorage.setItem('pac',JSON.stringify({n:n,t:document.getElementById('ftel').value.trim()}));
 document.getElementById('ficha').style.display='none';
 pinta(false,'Gracias, '+n+'. Ya me acuerdo de ti. ❤️')};
document.getElementById('benv').onclick=()=>{const t=document.getElementById('txt').value.trim();if(!t)return;
 document.getElementById('txt').value='';pinta(true,t);
 api('/api/text',JSON.stringify({texto:t,pac:pac()}))};
document.getElementById('bfoto').onclick=()=>document.getElementById('ffoto').click();
document.getElementById('ffoto').onchange=e=>{const f=e.target.files[0];if(!f)return;pinta(true,'📷 (foto)');
 const fd=new FormData();fd.append('foto',f);fd.append('pac',pac());api('/api/foto',fd)};
let rec=null,chunks=[];
document.getElementById('bvoz').onclick=async()=>{
 if(rec){rec.stop();rec=null;document.getElementById('bvoz').textContent='🎤';return}
 document.getElementById('bvoz').textContent='⏹';chunks=[];
 const st=await navigator.mediaDevices.getUserMedia({audio:true});
 rec=new MediaRecorder(st);
 rec.ondataavailable=e=>chunks.push(e.data);
 rec.onstop=()=>{st.getTracks().forEach(t=>t.stop());pinta(true,'🎤 (voz)');
  const fd=new FormData();fd.append('audio',new Blob(chunks,{type:'audio/webm'}),'voz.webm');fd.append('pac',pac());api('/api/voz',fd)};
 rec.start()};
document.addEventListener('click',e=>{if(e.target.dataset&&e.target.dataset.a)suena(e.target.dataset.a)});
let evtI=null;
window.addEventListener('beforeinstallprompt',e=>{evtI=e;document.getElementById('inst').style.display='block'});
document.getElementById('inst').onclick=async()=>{if(evtI){evtI.prompt();document.getElementById('inst').style.display='none'}};
if('serviceWorker' in navigator)navigator.serviceWorker.register('/sw.js');
pinta(false,'Hola, soy tu asistente de salud. ❤️ Mandame tu presion, tu glucosa, una foto o una nota de voz, y yo te acompano.');
</script>
</body>
</html>"""

if __name__ == "__main__":
    app.run(port=10000)