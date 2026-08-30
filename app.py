import os, re, json, time, base64, threading, asyncio, tempfile, uuid, traceback
import requests
from flask import Flask, request, jsonify
from google import genai
from google.genai import types as gtypes

app = Flask(__name__)

@app.errorhandler(Exception)
def manejar_error(e):
    tb = traceback.format_exc()
    fallo("EXC: " + tb[-400:])
    return jsonify({"error": str(e), "tb": tb[-600:]}), 500

GEMINI_KEY_A = os.getenv("GEMINI_KEY_A")
GEMINI_KEY_B = os.getenv("GEMINI_KEY_B")
GROQ_KEY = os.getenv("GROQ_KEY")
META_TOKEN = os.getenv("META_TOKEN")
META_PHONE_ID = os.getenv("META_PHONE_ID")
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "SaludMexicali2026")

def _mk_client(k):
    if not k: return None
    try: return genai.Client(api_key=k)
    except Exception: return None

cliente_gemini_a = _mk_client(GEMINI_KEY_A)
cliente_gemini_b = _mk_client(GEMINI_KEY_B)

MODELOS_GEMINI = ["gemini-3-flash", "gemini-3-flash-preview", "gemini-3-pro", "gemini-2.5-flash-lite", "gemini-2.0-flash", "gemini-2.5-flash"]
MODELOS_GROQ = ["llama-3.1-8b-instant", "llama-3.3-70b-versatile"]

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

SYSTEM = ("Responde SIEMPRE con frases completas (nunca cortadas a la mitad), maximo 3 frases cortas, separadas por renglones, con palabras sencillas para adultos mayores.\n"
"Eres 'Salud Mexicali', asistente calido de salud para adultos mayores con hipertension y diabetes.\n"
"IDIOMA: responde SIEMPRE en el idioma del paciente (espanol o ingles).\n"
"TRATO: si conoces el nombre del paciente (ver DATOS DEL PACIENTE), dirigete a el por su nombre con respeto y calidez (ej. 'don Antonio', 'senora Maria'); NUNCA uses 'corazon' ni 'carino' si ya sabes su nombre. Si no lo conoces, usa un trato amable neutro.\n"
"Habla con frases cortas, claras y carinosas.\n"
"Criterios (adulto mayor): normal: TA hasta 139/89 y glucosa 70-180; moderado: TA 140-159/90-99 o glucosa 181-250; critico: TA 160 o mas, o glucosa mayor a 250 o menor a 70, o sintomas como dolor de pecho, confusion o vision borrosa.\n"
"Si es critico: pide con carino que se vuelva a medir en 5 minutos sentado y avisa que notificaras a su familia.\n"
"Al final agrega SIEMPRE, en lineas separadas, exactamente:\n"
"TRIAGE:normal  (o TRIAGE:moderado o TRIAGE:critico)\n"
"y si detectas numeros: VALORES: TA=130/80 PULSO=76 GLUCOSA=110 (solo los que veas).")

def detectar_idioma(t):
    t = (t or "").lower()
    en = ["hello", "hi ", "thank", "my ", "i ", "the ", "doctor", "feel", "today", "blood pressure", "sugar", "good morning"]
    es = ["hola", "gracias", "mi ", "yo ", "el ", "doctor", "siento", "hoy", "presion", "glucosa", "buenos dias", "me "]
    ce = sum(1 for w in en if w in t)
    cs = sum(1 for w in es if w in t)
    return "en" if ce > cs else "es"

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
    txt = re.sub(r"VALORES:.*", "", txt or "")
    return txt.strip(), triage, valores

def datos_pac(raw):
    try:
        d = json.loads(raw or "{}")
        return d.get("n", ""), d.get("t", "")
    except Exception:
        return "", ""

def registrar(pid, nombre):
    if pid:
        p = PAC.setdefault(pid, {"nombre": nombre or "", "hist": []})
        if nombre: p["nombre"] = nombre
        return p
    return None

def contexto(p):
    if not p or not p.get("nombre"): return ""
    hist = " | ".join([h for h in p["hist"][-5:]])
    return ("\nDATOS DEL PACIENTE: nombre=" + p["nombre"] + ". Su historia reciente: " + hist +
            "\nUsa su nombre al hablarle.")

def recordar(p, linea):
    if p:
        p["hist"].append(linea)
        if len(p["hist"]) > 10: p["hist"] = p["hist"][-10:]

def sufijo_lang(lang):
    return " (Answer in English, short and warm.)" if lang == "en" else " (Responde en espanol, corto y carinoso.)"

def gemini_gen(parts, cliente, etiqueta, lang):
    if not cliente: return None
    for mod in MODELOS_GEMINI:
        try:
            r = cliente.models.generate_content(
                model=mod,
                contents=[{"role": "user", "parts": parts}],
                config=gtypes.GenerateContentConfig(system_instruction=SYSTEM, temperature=0.4, max_output_tokens=1024),
            )
            t = (r.text or "").strip()
            if t:
                contar(etiqueta)
                return t
        except Exception as e:
            fallo(f"{etiqueta}/{mod}: {str(e)[:60]}")
    return None

def generar_texto(prompt, lang):
    t = gemini_gen([{"text": prompt}], cliente_gemini_a, "gemini_a", lang)
    if t: return t
    t = gemini_gen([{"text": prompt}], cliente_gemini_b, "gemini_b", lang)
    if t: return t
    if GROQ_KEY:
        for mod in MODELOS_GROQ:
            try:
                r = requests.post("https://api.groq.com/openai/v1/chat/completions",
                    headers={"Authorization": "Bearer " + GROQ_KEY},
                    json={"model": mod, "messages": [
                        {"role": "system", "content": SYSTEM},
                        {"role": "user", "content": prompt}]}, timeout=30)
                r.raise_for_status()
                contar("groq")
                return r.json()["choices"][0]["message"]["content"]
            except Exception as e:
                fallo(f"groq/{mod}: {str(e)[:60]}")
    return None

def generar_foto(b64, mime, lang):
    t = gemini_gen([{"text": "El paciente manda una FOTO de su aparato de medicion. Lee con cuidado los numeros y acompana." + sufijo_lang(lang)},
                    gtypes.Part.from_bytes(data=base64.b64decode(b64), mime_type=mime)], cliente_gemini_a, "gemini_a", lang)
    if t: return t
    t = gemini_gen([{"text": "El paciente manda una FOTO de su aparato de medicion. Lee con cuidado los numeros y acompana." + sufijo_lang(lang)},
                    gtypes.Part.from_bytes(data=base64.b64decode(b64), mime_type=mime)], cliente_gemini_b, "gemini_b", lang)
    if t: return t
    if GROQ_KEY:
        for mod in ["llama-3.2-90b-vision-preview", "llama-3.2-11b-vision-preview"]:
            try:
                r = requests.post("https://api.groq.com/openai/v1/chat/completions",
                    headers={"Authorization": "Bearer " + GROQ_KEY},
                    json={"model": mod, "messages": [
                        {"role": "system", "content": SYSTEM},
                        {"role": "user", "content": [
                            {"type": "text", "text": "Lee los numeros de esta foto de un tensiometro o glucometro y acompana."},
                            {"type": "image_url", "image_url": {"url": "data:" + mime + ";base64," + b64}}]}]}, timeout=30)
                r.raise_for_status()
                contar("groq")
                return r.json()["choices"][0]["message"]["content"]
            except Exception as e:
                fallo(f"groq vision/{mod}: {str(e)[:60]}")
    return None

def generar_voz(audio, mime, lang):
    t = gemini_gen([{"text": "El paciente manda una NOTA DE VOZ. Escuchala, entiende sus numeros o su duda y acompana." + sufijo_lang(lang)},
                    gtypes.Part.from_bytes(data=audio, mime_type=mime)], cliente_gemini_a, "gemini_a", lang)
    if t: return t
    t = gemini_gen([{"text": "El paciente manda una NOTA DE VOZ. Escuchala, entiende sus numeros o su duda y acompana." + sufijo_lang(lang)},
                    gtypes.Part.from_bytes(data=audio, mime_type=mime)], cliente_gemini_b, "gemini_b", lang)
    if t: return t
    if GROQ_KEY:
        try:
            r = requests.post("https://api.groq.com/openai/v1/audio/transcriptions",
                headers={"Authorization": "Bearer " + GROQ_KEY},
                files={"file": ("voz.webm", audio, mime)},
                data={"model": "whisper-large-v3"}, timeout=30)
            r.raise_for_status()
            contar("groq")
            return generar_texto("El paciente dijo por voz: " + (r.json().get("text") or ""), lang)
        except Exception as e:
            fallo(f"groq whisper: {str(e)[:60]}")
    return None

async def _edge_async(texto, lang):
    try:
        import edge_tts
        voz = "en-US-AriaNeural" if lang == "en" else "es-MX-DaliaNeural"
        ruta = os.path.join(tempfile.gettempdir(), "salud_" + str(uuid.uuid4()) + ".mp3")
        c = edge_tts.Communicate(texto, voz)
        await c.save(ruta)
        with open(ruta, "rb") as f:
            data = f.read()
        try: os.remove(ruta)
        except Exception: pass
        contar("audios")
        return base64.b64encode(data).decode()
    except Exception as e:
        fallo(f"edge_tts: {str(e)[:60]}")
        return None

def tts(texto, lang):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(_edge_async(texto, lang))
    finally:
        loop.close()

def finalizar(txt_crudo, canal, tipo, usuario, pid, nombre, lang):
    p = registrar(pid, nombre)
    if not txt_crudo:
        return jsonify({"texto": "No te escuche bien, intentalo otra vez por favor. / I didn't hear you well, please try again.", "triage": "normal", "valores": {}, "lang": lang})
    texto, triage, valores = limpiar(txt_crudo)
    recordar(p, tipo + " " + usuario + " -> " + triage + " " + json.dumps(valores))
    BITACORA.append({"ts": time.strftime("%Y-%m-%d %H:%M"), "canal": canal, "pac": pid,
                     "usuario": usuario, "bot": texto, "triage": triage, "valores": valores})
    return jsonify({"texto": texto, "triage": triage, "valores": valores, "lang": lang})

@app.route("/")
def inicio():
    return HTML

@app.route("/api/tts", methods=["POST"])
def api_tts():
    d = request.get_json(force=True)
    a = tts(d.get("texto", "")[:600], d.get("lang", "es"))
    return jsonify({"audio": a, "mime": "audio/mpeg"})

@app.route("/test")
def test():
    out = {"gemini_a": "SIN_LLAVE", "gemini_b": "SIN_LLAVE", "groq": "SIN_LLAVE", "edge_tts": "PENDIENTE"}
    if cliente_gemini_a:
        out["gemini_a"] = "OK" if generar_texto("responde solo: ok", "es") else "FALLO"
    if cliente_gemini_b:
        t = gemini_gen([{"text": "responde solo: ok"}], cliente_gemini_b, "gemini_b", "es")
        out["gemini_b"] = "OK" if t else "FALLO"
    if GROQ_KEY:
        try:
            r = requests.post("https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": "Bearer " + GROQ_KEY},
                json={"model": MODELOS_GROQ[0], "messages": [{"role": "user", "content": "responde solo: ok"}]}, timeout=15)
            r.raise_for_status()
            out["groq"] = "OK"
        except Exception as e:
            out["groq"] = "ERROR: " + str(e)[:200]
    out["edge_tts"] = "OK" if tts("prueba", "es") else "ERROR"
    return jsonify(out)

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
    lp = d.get("lang", "auto")
    lang = lp if lp in ("es", "en") else detectar_idioma(t)
    return finalizar(generar_texto(contexto(PAC.get(tel or n)) + "\nEl paciente escribe: " + t + sufijo_lang(lang), lang), "web", "texto", t, tel or n, n, lang)


@app.route("/api/foto", methods=["POST"])
def api_foto():
    contar("web"); contar("fotos")
    f = request.files.get("foto")
    n, tel = datos_pac(request.form.get("pac", ""))
    lang = request.form.get("lang", "es")
    b64 = base64.b64encode(f.read()).decode()
    return finalizar(generar_foto(b64, f.mimetype or "image/jpeg", lang), "web", "foto", "(foto)", tel or n, n, lang)

@app.route("/api/voz", methods=["POST"])
def api_voz():
    contar("web"); contar("voces")
    f = request.files.get("audio")
    n, tel = datos_pac(request.form.get("pac", ""))
    lang = request.form.get("lang", "es")
    return finalizar(generar_voz(f.read(), f.mimetype or "audio/webm", lang), "web", "voz", "(voz)", tel or n, n, lang)

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
        lang = "es"
        if msg["type"] == "text":
            crudo = generar_texto(contexto(p) + "\nEl paciente escribe por WhatsApp: " + msg["text"]["body"] + sufijo_lang(lang), lang)
        elif msg["type"] == "image":
            mid = msg["image"]["id"]
            r1 = requests.get("https://graph.facebook.com/v21.0/" + mid, headers={"Authorization": "Bearer " + str(META_TOKEN)})
            r2 = requests.get(r1.json()["url"], headers={"Authorization": "Bearer " + str(META_TOKEN)})
            crudo = generar_foto(base64.b64encode(r2.content).decode(), "image/jpeg", lang)
        else:
            enviar_wa(de, "Recibi tu mensaje. En esta version leo fotos y texto.")
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
 body{margin:0;font-family:Arial;font-size:var(--fs,20px);background:#f4f6fb;display:flex;flex-direction:column;height:100vh}
 header{background:#0f274d;color:#fff;padding:12px;text-align:center;font-size:1.2em;position:relative}
 #hdr2{display:flex;justify-content:center;gap:8px;margin-top:6px}
 #hdr2 button{font-size:.7em;padding:4px 10px;border-radius:999px;border:1px solid rgba(255,255,255,.5);background:transparent;color:#fff;cursor:pointer}
 #hdr2 button.on{background:#f7941d;border-color:#f7941d;font-weight:700}
 #chat{flex:1;overflow-y:auto;padding:14px;display:flex;flex-direction:column;gap:10px}
 .b{max-width:80%;padding:12px 16px;border-radius:18px;line-height:1.4}
 .yo{align-self:flex-end;background:#0f274d;color:#fff}
 .bot{align-self:flex-start;background:#fff;box-shadow:0 1px 4px rgba(0,0,0,.15)}
 .crit{border:3px solid #d32f2f}
 .b audio{width:100%;max-width:320px;margin-top:6px;display:block}
 .dots i{display:inline-block;width:8px;height:8px;border-radius:50%;background:#0f274d;margin:0 2px;animation:lat 1s infinite}
 .dots i:nth-child(2){animation-delay:.2s}.dots i:nth-child(3){animation-delay:.4s}
 @keyframes lat{0%,100%{transform:translateY(0);opacity:.4}50%{transform:translateY(-5px);opacity:1}}
 #bar{display:flex;gap:8px;padding:10px;background:#fff;border-top:2px solid #dde}
 #bar input{flex:1;font-size:1em;padding:12px;border-radius:12px;border:2px solid #bbc}
 #bar button{font-size:1.2em;border:none;border-radius:12px;background:#0f274d;color:#fff;padding:0 16px}
 #inst{display:none;margin:8px auto;background:#e8f5e9;border:2px solid #4c8;padding:8px 16px;font-size:.9em;border-radius:12px}
 #ficha{margin:10px auto;background:#fff;padding:14px;border-radius:14px;box-shadow:0 1px 6px rgba(0,0,0,.2);text-align:center}
 #ficha input{font-size:1em;margin:6px;padding:10px;border-radius:10px;border:2px solid #bbc;display:block;width:80%;margin-left:auto;margin-right:auto}
</style>
</head>
<body>
<header>❤️ Salud Mexicali
 <div id="hdr2">
  <button id="Lauto" class="on">AUTO</button><button id="Les">ES</button><button id="Len">EN</button>
  <button id="fmas">A+</button><button id="fmenos">A−</button>
 </div>
</header>
<button id="inst">📲 Instalar como app</button>
<div id="chat"></div>
<div id="ficha" style="display:none">
 <b>Presentate para que te recuerde:</b>
 <input id="fnom" placeholder="Tu nombre">
 <input id="ftel" placeholder="Tu telefono (opcional)">
 <button id="fok" style="font-size:1em;padding:8px 20px;border-radius:10px;border:none;background:#0f274d;color:#fff">Guardar</button>
</div>
<div id="bar">
 <button id="bfoto">📷</button>
 <button id="bvoz">🎤</button>
 <input id="txt" placeholder="Escribe aqui... (Enter envia)">
 <button id="benv">➤</button>
 <input type="file" id="ffoto" accept="image/*" hidden>
</div>
<script>
const chat=document.getElementById('chat');
const pac=()=>localStorage.getItem('pac')||'';
let langPref='auto',fontScale=1,thinkT=null,thinkS=0,rec=null,chunks=[];
function aplicarFuente(){document.documentElement.style.setProperty('--fs',(20*fontScale)+'px')}
function pinta(q,t,cls){const d=document.createElement('div');d.className='b '+(q?'yo':'bot')+(cls||'');d.innerHTML=t;chat.appendChild(d);chat.scrollTop=chat.scrollHeight;return d}
function pensando(){quitando();pinta(false,'<span class="dots"><i></i><i></i><i></i></span> Trabajando en tu respuesta… <span id="tsec">0</span> s');thinkS=0;thinkT=setInterval(()=>{thinkS++;const e=document.getElementById('tsec');if(e)e.textContent=thinkS},1000)}
function quitando(){if(thinkT){clearInterval(thinkT);thinkT=null}}
function agregaAudio(el,texto,lang){fetch('/api/tts',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({texto:texto,lang:lang})}).then(r=>r.json()).then(a=>{if(a.audio){const au=document.createElement('audio');au.controls=true;au.src='data:'+(a.mime||'audio/mpeg')+';base64,'+a.audio;el.appendChild(au);chat.scrollTop=chat.scrollHeight}}).catch(()=>{})}
function botMsg(d){const t=(d.texto||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/\\n/g,'<br>');
 const el=pinta(false,t,d.triage==='critico'?' crit':'');
 if(d.texto)agregaAudio(el,d.texto,d.lang||'es')}
async function api(url,body){pensando();
 try{const r=await fetch(url,{method:'POST',body});
  if(!r.ok){quitando();chat.lastChild.remove();pinta(false,'⚠️ Error '+r.status+'. Abre /test para ver por que.');return}
  const d=await r.json();quitando();chat.lastChild.remove();botMsg(d)}
 catch(e){quitando();chat.lastChild.remove();pinta(false,'⚠️ Sin conexion con el servidor: '+e)}}
if(!pac()){document.getElementById('ficha').style.display='block'}
document.getElementById('fok').onclick=()=>{const n=document.getElementById('fnom').value.trim()||'';
 localStorage.setItem('pac',JSON.stringify({n:n,t:document.getElementById('ftel').value.trim()}));
 document.getElementById('ficha').style.display='none';
 pinta(false,n?('Gracias, '+n+'. Ya me acuerdo de ti. ❤️'):'Listo. ❤️')}
document.getElementById('txt').onkeydown=e=>{if(e.key==='Enter')document.getElementById('benv').click()};
document.getElementById('benv').onclick=()=>{const t=document.getElementById('txt').value.trim();if(!t)return;
 document.getElementById('txt').value='';pinta(true,t);
 api('/api/text',JSON.stringify({texto:t,pac:pac(),lang:langPref}))};
document.getElementById('bfoto').onclick=()=>document.getElementById('ffoto').click();
document.getElementById('ffoto').onchange=e=>{const f=e.target.files[0];if(!f)return;pinta(true,'📷 (foto)');
 const fd=new FormData();fd.append('foto',f);fd.append('pac',pac());fd.append('lang',langPref==='auto'?'es':langPref);api('/api/foto',fd)};
document.getElementById('bvoz').onclick=async()=>{
 if(rec){rec.stop();rec=null;document.getElementById('bvoz').textContent='🎤';return}
 document.getElementById('bvoz').textContent='⏹';chunks=[];
 const st=await navigator.mediaDevices.getUserMedia({audio:true});
 rec=new MediaRecorder(st);
 rec.ondataavailable=e=>chunks.push(e.data);
 rec.onstop=()=>{st.getTracks().forEach(t=>t.stop());pinta(true,'🎤 (voz)');
  const fd=new FormData();fd.append('audio',new Blob(chunks,{type:'audio/webm'}),'voz.webm');fd.append('pac',pac());fd.append('lang',langPref==='auto'?'es':langPref);api('/api/voz',fd)};
 rec.start()};
[['Lauto','auto'],['Les','es'],['Len','en']].forEach(([id,v])=>{document.getElementById(id).onclick=e=>{langPref=v;
 document.querySelectorAll('#hdr2 button').forEach(x=>x.classList.remove('on'));e.target.classList.add('on')}});
document.getElementById('fmas').onclick=()=>{fontScale=Math.min(1.6,fontScale+0.1);aplicarFuente()};
document.getElementById('fmenos').onclick=()=>{fontScale=Math.max(0.8,fontScale-0.1);aplicarFuente()};
let evtI=null;
window.addEventListener('beforeinstallprompt',e=>{evtI=e;document.getElementById('inst').style.display='block'});
document.getElementById('inst').onclick=async()=>{if(evtI){evtI.prompt();document.getElementById('inst').style.display='none'}};
if('serviceWorker' in navigator)navigator.serviceWorker.register('/sw.js');
pinta(false,'Hola, soy su asistente de salud. ❤️<br><br>Yo le puedo ayudar si me manda:<br>• Su presión arterial<br>• Su glucosa<br>• Una foto de su aparato<br>• O una nota de voz<br><br>¿Cómo se siente hoy?');
</script>
</body>
</html>"""

if __name__ == "__main__":
    app.run(port=10000)