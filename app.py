import os, re, json, time, base64, threading, asyncio, tempfile, uuid, traceback
import requests
from flask import Flask, request, jsonify
from google import genai
from google.genai import types as gtypes
from google.genai import types

app = Flask(__name__)

@app.errorhandler(Exception)
def manejar_error(e):
    tb = traceback.format_exc()
    fallo("EXC: " + tb[-400:])
    return jsonify({"error": str(e), "tb": tb[-600:]}), 500

GEMINI_KEY_A = os.getenv("GEMINI_KEY_A")
GEMINI_KEY_B = os.getenv("GEMINI_KEY_B")
GROQ_KEY = os.getenv("GROQ_KEY")
OR_KEY = os.getenv("OPENROUTER_KEY")
TR_KEY = os.getenv("TOKENROUTER_KEY")
TR_URL = "https://api.to.tokenrouter.com/v1/chat/completions"
TR_MODELOS = ["z-ai/glm-5.3-free", "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free"]
HF_KEY = os.getenv("HUGGINGFACE_KEY")
HF_URL = "https://router.huggingface.co/v1/chat/completions"
HF_MODELOS = ["Qwen/Qwen3-8B", "meta-llama/Llama-3.1-8B-Instruct"]
PROV = {}
USU = {}

def prov_stats(nombre, ok):
    p = PROV.setdefault(nombre, {"ok":0,"fail":0})
    if ok: p["ok"] += 1
    else: p["fail"] += 1

def orden_proveedores():
    base = ["gemini_a","gemini_b","groq","openrouter","tokenrouter","huggingface"]
    return sorted(base, key=lambda n: (PROV.get(n,{}).get("ok",0) - PROV.get(n,{}).get("fail",0)), reverse=True)

META_TOKEN = os.getenv("META_TOKEN")
META_PHONE_ID = os.getenv("META_PHONE_ID")
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "SaludMexicali2026")
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")

def _sb_headers():
    return {"apikey": SUPABASE_KEY, "Authorization": "Bearer " + SUPABASE_KEY,
            "Content-Type": "application/json", "Prefer": "resolution=merge-duplicates,return=minimal"}

def sb_guardar_paciente(pid, nombre):
    if not (SUPABASE_URL and SUPABASE_KEY and pid): return
    try:
        requests.post(SUPABASE_URL + "/rest/v1/pacientes", headers=_sb_headers(),
                      json={"id": pid, "nombre": nombre or ""}, timeout=8)
    except Exception as e:
        fallo(f"supabase paciente: {str(e)[:60]}")

def sb_guardar_lectura(pid, tipo, valores, triage, nota, canal, momento=""):
    if not (SUPABASE_URL and SUPABASE_KEY and pid): return
    try:
        requests.post(SUPABASE_URL + "/rest/v1/lecturas", headers=_sb_headers(),
                      json={"pac_id": pid, "tipo": tipo, "ta": valores.get("ta", ""),
                            "pulso": valores.get("pulso", ""), "glucosa": valores.get("glucosa", ""),
                            "triage": triage, "nota": (nota or "")[:200], "canal": canal, "momento": momento}, timeout=8)
    except Exception as e:
        fallo(f"supabase lectura: {str(e)[:60]}")

PEND = {}

def sb_tomas_pendientes(pid, solo_medicinas=False):
    out = []
    if not (SUPABASE_URL and SUPABASE_KEY and pid): return out
    try:
        r = requests.get(SUPABASE_URL + "/rest/v1/tomas", headers=_sb_headers(),
                         params={"pac_id": "eq." + pid, "activo": "eq.true", "select": "id,medicamento,hora"}, timeout=6)
        tomas = r.json() if r.ok else []
        hoy = time.strftime("%Y-%m-%d")
        ok = requests.get(SUPABASE_URL + "/rest/v1/tomas_ok", headers=_sb_headers(),
                          params={"pac_id": "eq." + pid, "fecha": "eq." + hoy, "select": "toma_id"}, timeout=6)
        done = set(str(x["toma_id"]) for x in (ok.json() if ok.ok else []))
        for t in tomas:
            if str(t["id"]) in done: continue
            esmed = not any(k in t["medicamento"].lower() for k in ["presion", "presión", "glucosa", "chequeo", "medicion", "medición"])
            if solo_medicinas and not esmed: continue
            if not solo_medicinas and esmed: continue
            out.append(t)
    except Exception as e:
        fallo(f"supabase pendientes: {str(e)[:60]}")
    return out

def sb_confirmar_toma_id(pid, tid):
    if not (SUPABASE_URL and SUPABASE_KEY and pid): return
    try:
        requests.post(SUPABASE_URL + "/rest/v1/tomas_ok", headers=_sb_headers(),
                      json={"pac_id": pid, "toma_id": tid, "fecha": time.strftime("%Y-%m-%d")}, timeout=6)
    except Exception as e:
        fallo(f"supabase confirmar id: {str(e)[:60]}")

def sb_guardar_tomas(pid, meds):
    if not (SUPABASE_URL and SUPABASE_KEY and pid and meds): return
    try:
        for nom, hors in meds.items():
            requests.delete(SUPABASE_URL + "/rest/v1/tomas", headers=_sb_headers(),
                            params={"pac_id": "eq." + pid, "medicamento": "eq." + nom}, timeout=8)
            for h in hors:
                requests.post(SUPABASE_URL + "/rest/v1/tomas", headers=_sb_headers(),
                              json={"pac_id": pid, "medicamento": nom, "hora": h}, timeout=8)
    except Exception as e:
        fallo(f"supabase tomas: {str(e)[:60]}")

def sb_guardar_rutina(pid, rut):
    if not (SUPABASE_URL and SUPABASE_KEY and pid and rut): return
    try:
        requests.patch(SUPABASE_URL + "/rest/v1/pacientes", headers=_sb_headers(),
                       params={"id": "eq." + pid}, json={"rutina": rut}, timeout=8)
    except Exception as e:
        fallo(f"supabase rutina: {str(e)[:60]}")

def sb_confirmar_toma(pid, texto):
    if not (SUPABASE_URL and SUPABASE_KEY and pid): return
    try:
        r = requests.get(SUPABASE_URL + "/rest/v1/tomas", headers=_sb_headers(),
                         params={"pac_id": "eq." + pid, "activo": "eq.true", "select": "id,medicamento,hora"}, timeout=6)
        tomas = r.json() if r.ok else []
        if not tomas: return
        low = (texto or "").lower()
        cand = [t for t in tomas if t["medicamento"].split()[0].lower() in low] or tomas
        hoy = time.strftime("%Y-%m-%d")
        requests.post(SUPABASE_URL + "/rest/v1/tomas_ok", headers=_sb_headers(),
                      json={"pac_id": pid, "toma_id": cand[0]["id"], "fecha": hoy}, timeout=6)
    except Exception as e:
        fallo(f"supabase confirmar: {str(e)[:60]}")

def sb_confirmar_medicion(pid):
    if not (SUPABASE_URL and SUPABASE_KEY and pid): return
    try:
        r = requests.get(SUPABASE_URL + "/rest/v1/tomas", headers=_sb_headers(),
                         params={"pac_id": "eq." + pid, "activo": "eq.true", "select": "id,medicamento"}, timeout=6)
        tomas = r.json() if r.ok else []
        hoy = time.strftime("%Y-%m-%d")
        for t in tomas:
            if any(k in t["medicamento"].lower() for k in ["presion", "presión", "glucosa", "chequeo", "medicion", "medición"]):
                requests.post(SUPABASE_URL + "/rest/v1/tomas_ok", headers=_sb_headers(),
                              json={"pac_id": pid, "toma_id": t["id"], "fecha": hoy}, timeout=6)
    except Exception as e:
        fallo(f"supabase confirmar medicion: {str(e)[:60]}")

def sb_expediente(pid):
    if not (SUPABASE_URL and SUPABASE_KEY and pid): return ""
    try:
        r = requests.get(SUPABASE_URL + "/rest/v1/pacientes", headers=_sb_headers(),
                         params={"id": "eq." + pid, "select": "nombre,medicamentos,rutina"}, timeout=6)
        p = (r.json() or [{}])[0] if r.ok else {}
        r2 = requests.get(SUPABASE_URL + "/rest/v1/lecturas", headers=_sb_headers(),
                          params={"pac_id": "eq." + pid, "order": "ts.desc", "limit": 3,
                                  "select": "ta,glucosa,triage"}, timeout=6)
        lect = r2.json() if r2.ok else []
        r3 = requests.get(SUPABASE_URL + "/rest/v1/citas", headers=_sb_headers(),
                          params={"pac_id": "eq." + pid, "recordado": "eq.false",
                                  "order": "fecha.asc", "limit": 1,
                                  "select": "fecha,hora,lugar,doctor,notas"}, timeout=6)
        cit = (r.json() or [])[:1] if False else (r3.json() or [])[:1] if r3.ok else []
        r4 = requests.get(SUPABASE_URL + "/rest/v1/tomas", headers=_sb_headers(),
                  params={"pac_id": "eq." + pid, "activo": "eq.true",
                      "select": "medicamento,hora", "order": "hora.asc"}, timeout=6)
        tomas = r4.json() if r4.ok else []
        partes = []
        if p.get("nombre"): partes.append("nombre=" + p["nombre"])
        if p.get("medicamentos"): partes.append("medicamentos=" + p["medicamentos"])
        if p.get("rutina"): partes.append("rutina=" + p["rutina"])
        if lect:
            ls = []
            for l in lect:
                ped = []
                if l.get("ta"): ped.append("TA " + l["ta"])
                if l.get("glucosa"): ped.append("glucosa " + l["glucosa"])
                if l.get("triage"): ped.append(l["triage"])
                ls.append(" ".join(ped))
            partes.append("ultimas lecturas: " + "; ".join(ls))
        if cit:
            c = cit[0]
            partes.append("PROXIMA CITA: " + str(c.get("fecha", "")) + " " + c.get("hora", "") +
                          " en " + c.get("lugar", "") + " con " + c.get("doctor", "") +
                          ((" llevar: " + c.get("notas", "")) if c.get("notas") else ""))
        if tomas:
            partes.append("tomas programadas: " + "; ".join(t["medicamento"] + " " + t["hora"] for t in tomas))
        return ("\nEXPEDIENTE DEL PACIENTE: " + " | ".join(partes)) if partes else ""
    except Exception as e:
        fallo(f"supabase expediente: {str(e)[:60]}")
        return ""

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
"Cuando leas una foto del aparato o recibas valores, la PRIMERA frase de tu respuesta debe ser el valor mismo, por ejemplo: 'Su presión fue 152/76 con pulso de 52.' o 'Su glucosa fue 95.' Después continúa con el acompañamiento cariñoso.\n"
"Si el paciente menciona medicamentos, dosis u horarios, agrega al final una linea: MEDS: nombre=HH:MM,HH:MM; nombre2=HH:MM. Convierte los momentos a horas: manana=08:00, mediodia=14:00, tarde=17:00, noche=21:00, antes de dormir=22:00. Si el paciente describe rutinas nuevas o cambios, actualiza MEDS: con todas sus medicinas conocidas.\n"
"Si menciona a que hora se mide la presion o la glucosa, agrega: RUTINA: presion=HH:MM; glucosa=HH:MM\n"
"Si el EXPEDIENTE aparece vacio (paciente nuevo), presentate con cariño y preguntale que medicamentos toma con sus horarios y a que hora se mide la presion.\n"
"Eres 'Salud Mexicali', asistente calido de salud para adultos mayores con hipertension y diabetes.\n"
"IDIOMA: responde SIEMPRE en el idioma del paciente (espanol o ingles).\n"
"TRATO: si conoces el nombre del paciente (ver DATOS DEL PACIENTE), dirigete a el por su nombre con respeto y calidez (ej. 'don Antonio', 'senora Maria'); NUNCA uses 'corazon' ni 'cariño' si ya sabes su nombre. Si no lo conoces, usa un trato amable neutro.\n"
"Habla con frases cortas, claras y carinosas.\n"
"Criterios (adulto mayor): normal: TA hasta 139/89 y glucosa 70-180; moderado: TA 140-159/90-99 o glucosa 181-250; critico: TA 160 o mas, o glucosa mayor a 250 o menor a 70, o sintomas como dolor de pecho, confusion o vision borrosa.\n"
"Si es critico: pide con carino que se vuelva a medir en 5 minutos sentado y avisa que notificaras a su familia.\n"
"Al final agrega SIEMPRE, en lineas separadas, exactamente:\n"
"TRIAGE:normal  (o TRIAGE:moderado o TRIAGE:critico)\n"
"VALORES: ta=SIST/DIAST, pulso=P, glucosa=G, hora=HH:MM (solo los que aparezcan; si el paciente dice a qué hora se midió, pon esa hora en hora=). Al responder, menciona con cariño el pulso si lo hay, y di si es por la mañana, por la tarde o por la noche según la hora actual que aparece en el contexto.\n"
"SIEMPRE menciona TODOS los valores que el paciente envió: presión arterial (sistólica/diastólica), pulso y glucosa. Nunca omitas la presión ni la glucosa si aparecen en VALORES. Ejemplo: Su presión salió en 152/76 con pulso de 68, eso es...\n")

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
        hr = re.search(r"HORA\s*=?\s*(\d{1,2})(?::(\d{2}))?", s, re.I)
        if hr: valores["hora"] = str(int(hr.group(1))).zfill(2) + ":" + (hr.group(2) or "00")
    meds = {}
    mm = re.search(r"MEDS:(.+)", txt)
    if mm:
        for par in mm.group(1).split(";"):
            if "=" in par:
                nom, hors = par.split("=", 1)
                meds[nom.strip()] = [h.strip() for h in hors.split(",") if h.strip()]
        txt = re.sub(r"MEDS:.+", "", txt)
    rut = ""
    mr = re.search(r"RUTINA:(.+)", txt)
    if mr:
        rut = mr.group(1).strip()
        txt = re.sub(r"RUTINA:.+", "", txt)
    txt = re.sub(r"TRIAGE:\s*(normal|moderado|critico)", "", txt or "", flags=re.I)
    txt = re.sub(r"VALORES:.*", "", txt or "")
    return txt.strip(), triage, valores, meds, rut

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
        sb_guardar_paciente(pid, nombre or p["nombre"])
        return p
    return None

def contexto(pid):
    partes = []
    exp = sb_expediente(pid)
    if exp: partes.append(exp)
    p = PAC.get(pid)
    if p and p.get("hist"):
        partes.append("Historia de hoy: " + " | ".join(p["hist"][-3:]))
    partes.append("hora actual: " + time.strftime("%H:%M"))
    return ("\n" + "\n".join(partes) + "\nUsa su nombre al hablarle.") if partes else ""

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
    def g_a(): return gemini_gen([{"text": prompt}], cliente_gemini_a, "gemini_a", lang)
    def g_b(): return gemini_gen([{"text": prompt}], cliente_gemini_b, "gemini_b", lang)
    def g_groq():
        if not GROQ_KEY: return None
        for mod in MODELOS_GROQ:
            try:
                r = requests.post("https://api.groq.com/openai/v1/chat/completions",
                    headers={"Authorization": "Bearer " + GROQ_KEY},
                    json={"model": mod, "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}]}, timeout=30)
                r.raise_for_status()
                return r.json()["choices"][0]["message"]["content"]
            except Exception as e:
                fallo(f"groq/{mod}: {str(e)[:60]}")
        return None
    def g_or():
        if not OR_KEY: return None
        for mod in ["deepseek/deepseek-v4-flash", "meta-llama/llama-3.3-70b-instruct:free", "google/gemini-2.0-flash-001"]:
            try:
                r = requests.post("https://openrouter.ai/api/v1/chat/completions",
                    headers={"Authorization": "Bearer " + OR_KEY},
                    json={"model": mod, "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}]}, timeout=30)
                r.raise_for_status()
                return r.json()["choices"][0]["message"]["content"]
            except Exception as e:
                fallo(f"openrouter/{mod}: {str(e)[:60]}")
        return None
    def g_tr():
        if not TR_KEY: return None
        for mod in TR_MODELOS:
            try:
                r = requests.post(TR_URL,
                    headers={"Authorization": "Bearer " + TR_KEY},
                    json={"model": mod, "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}]}, timeout=30)
                r.raise_for_status()
                return r.json()["choices"][0]["message"]["content"]
            except Exception as e:
                fallo(f"tokenrouter/{mod}: {str(e)[:60]}")
        return None
    def g_hf():
        if not HF_KEY: return None
        for mod in HF_MODELOS:
            try:
                r = requests.post(HF_URL,
                    headers={"Authorization": "Bearer " + HF_KEY},
                    json={"model": mod, "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}]}, timeout=30)
                r.raise_for_status()
                return r.json()["choices"][0]["message"]["content"]
            except Exception as e:
                fallo(f"huggingface/{mod}: {str(e)[:60]}")
        return None
    funcs = {"gemini_a": g_a, "gemini_b": g_b, "groq": g_groq, "openrouter": g_or, "tokenrouter": g_tr, "huggingface": g_hf}
    for nombre in orden_proveedores():
        t = funcs[nombre]()
        prov_stats(nombre, bool(t))
        if t: return t
    return None

import io as _io
from PIL import Image

def comprimir_img(datos, maxw=900, q=72):
    try:
        im = Image.open(_io.BytesIO(datos))
        if im.width > maxw:
            im = im.resize((maxw, int(im.height * maxw / im.width)))
        buf = _io.BytesIO()
        im.convert("RGB").save(buf, "JPEG", quality=q)
        return buf.getvalue()
    except Exception:
        return datos

def parseo_monitor(t):
    t = t or ""
    ta = re.search(r"ta\s*=\s*(\d{2,3})\s*/\s*(\d{2,3})", t, re.I)
    pu = re.search(r"pulso\s*=\s*(\d{2,3})", t, re.I)
    gl = re.search(r"glucosa\s*=\s*(\d{2,3})", t, re.I)
    if not ta and not gl:
        nums = re.findall(r"\b(\d{2,3})\b", t)
        if len(nums) >= 3:
            ta = re.match("", "")
            return "VALORES: ta=" + nums[0] + "/" + nums[1] + ", pulso=" + nums[2]
        if len(nums) == 1:
            return "VALORES: glucosa=" + nums[0]
    out = "VALORES:"
    if ta: out += " ta=" + ta.group(1) + "/" + ta.group(2)
    if pu: out += ", pulso=" + pu.group(1)
    if gl: out += ", glucosa=" + gl.group(1)
    return out if out != "VALORES:" else t

def generar_foto(b64, mime, lang):
    datos = base64.b64decode(b64)
    pregunta = "Lee este monitor de salud y responde SOLO con la linea: VALORES: ta=SIST/DIAST, pulso=P, glucosa=G (solo los que veas)."
    parte_img = gtypes.Part.from_bytes(data=datos, mime_type=mime or "image/jpeg")
    for cli, nom in ((cliente_gemini_a, "gemini_a"), (cliente_gemini_b, "gemini_b")):
        t = gemini_gen([{"text": pregunta}, parte_img], cli, nom, lang)
        if t: return parseo_monitor(t)
    url_img = "data:" + (mime or "image/jpeg") + ";base64," + b64
    msgs = [{"role": "user", "content": [{"type": "text", "text": pregunta}, {"type": "image_url", "image_url": {"url": url_img}}]}]
    for nom, key, url, mods in (("tokenrouter", TR_KEY, TR_URL, ["z-ai/glm-4.6v"]),
                                ("huggingface", HF_KEY, HF_URL, ["Qwen/Qwen2.5-VL-7B-Instruct", "meta-llama/Llama-3.2-11B-Vision-Instruct"]),
                                ("openrouter", OR_KEY, "https://openrouter.ai/api/v1/chat/completions", ["openai/gpt-5.4-mini", "google/gemini-2.5-flash"])):
        if not key: continue
        for mod in mods:
            try:
                r = requests.post(url, headers={"Authorization": "Bearer " + key}, json={"model": mod, "messages": msgs, "max_tokens": 300}, timeout=25)
                r.raise_for_status()
                t = (r.json()["choices"][0]["message"]["content"] or "").strip()
                if t: return parseo_monitor(t)
            except Exception as e:
                fallo(f"{nom}/{mod} foto: {str(e)[:60]}")
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

EMO = re.compile("[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U00002B00-\U00002BFF\U0001F1E6-\U0001F1FF❤️♥✅]", flags=re.UNICODE)
def texto_voz(t):
    t = EMO.sub("", t or "")
    t = t.replace("•", " ").replace("\n", ". ")
    t = re.sub(r"\s+", " ", t)
    return t.strip()[:600]

async def _edge_async(texto, lang):
    try:
        import edge_tts
        voz = "es-MX-DaliaNeural"
        ruta = os.path.join(tempfile.gettempdir(), "salud_" + str(uuid.uuid4()) + ".mp3")
        c = edge_tts.Communicate(texto_voz(texto), voz, rate="-4%")
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

def tts(texto, lang="es"):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(_edge_async(texto, lang))
    finally:
        loop.close()

def finalizar(txt_crudo, canal, tipo, usuario, pid, nombre, lang, extra="", botones=None):
    p = registrar(pid, nombre)
    if not txt_crudo:
        if tipo == "voz":
            msg = "No te escuche bien, intentalo otra vez por favor. / I didn't hear you well, please try again."
        else:
            msg = "No pude procesar su mensaje esta vez. Intente de nuevo, por favor."
        return jsonify({"texto": msg, "audio": tts(texto_voz(msg)) or "", "triage": "normal", "valores": {}, "lang": lang, "botones": botones or []})
    texto, triage, valores, meds, rut = limpiar(txt_crudo)
    texto += extra
    h = int(time.strftime("%H"))
    hora_lectura = valores.get("hora")
    if hora_lectura:
        mh = re.search(r"(\d{1,2})(?::(\d{2}))?", str(hora_lectura))
        if mh:
            h = int(mh.group(1))
            valores["hora"] = str(h).zfill(2) + ":" + (mh.group(2) or "00")
    momento = "manana" if h < 12 else ("tarde" if h < 19 else "noche")
    recordar(p, tipo + " " + usuario + " -> " + triage + " " + json.dumps(valores))
    sb_guardar_lectura(pid, tipo, valores, triage, usuario, canal, momento=momento)
    if valores.get("ta") or valores.get("glucosa"):
        sb_confirmar_medicion(pid)
        PEND.pop(pid, None)
    sb_guardar_tomas(pid, meds)
    sb_guardar_rutina(pid, rut)
    BITACORA.append({"ts": time.strftime("%Y-%m-%d %H:%M"), "canal": canal, "pac": pid,
                     "usuario": usuario, "bot": texto, "triage": triage, "valores": valores})
    return jsonify({"texto": texto, "audio": tts(texto_voz(texto)) or "", "triage": triage, "valores": valores, "lang": lang, "botones": botones or []})

@app.route("/")
def inicio():
    return HTML

@app.route("/api/tts", methods=["POST"])
def api_tts():
    d = request.get_json(force=True)
    a = tts(d.get("texto", "")[:600], d.get("lang", "es"))
    return jsonify({"audio": a, "mime": "audio/mpeg"})

BIENV = {"audio": None}
@app.route("/api/bienvenida")
def bienvenida():
    txt = "Hola, soy su asistente de salud. Yo le puedo ayudar si me manda su presión arterial, su glucosa, una foto de su aparato, o una nota de voz. ¿Cómo se siente hoy?"
    if BIENV["audio"] is None:
        BIENV["audio"] = tts(txt) or ""
    return jsonify({"texto": txt, "audio": BIENV["audio"]})

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
    u = USU.setdefault(tel or n or "anon", {"msgs":0,"fotos":0,"voces":0}); u["msgs"] += 1
    extra_n = ""
    bot_n = None
    pid0 = tel or n or "anon"
    st = PEND.get(pid0)
    if st and st["tipo"] == "cual_med":
        lowt = (t or "").lower()
        if any(w in lowt for w in ["todas", "ambas", "las dos", "los dos", "todos"]):
            for o in st["opts"]:
                sb_confirmar_toma_id(pid0, o["id"])
            PEND.pop(pid0, None)
            msg = "¡Excelente, " + (n or "don Antonio") + "! Anoto con cariño todas sus medicinas de hoy como tomadas. 💙"
            return jsonify({"texto": msg, "audio": tts(texto_voz(msg)) or "", "triage": "normal", "valores": {}, "botones": []})
        eleg = [o for o in st["opts"] if o["medicamento"].split()[0].lower() in lowt]
        if not eleg:
            nums = re.findall(r"\d+", t or "")
            if nums:
                eleg = [st["opts"][int(x) - 1] for x in nums if 1 <= int(x) <= len(st["opts"])]
        if eleg:
            for o in eleg:
                sb_confirmar_toma_id(pid0, o["id"])
            PEND.pop(pid0, None)
            msg = "¡Qué bien, " + (n or "don Antonio") + "! Anoto con cariño como tomado: " + ", ".join(o["medicamento"] + " (" + o["hora"] + ")" for o in eleg) + ". 💙"
            return jsonify({"texto": msg, "audio": tts(texto_voz(msg)) or "", "triage": "normal", "valores": {}, "botones": []})
    if st and st["tipo"] == "numeros":
        PEND.pop(pid0, None)
        pm = sb_tomas_pendientes(pid0, solo_medicinas=True)
        if pm:
            extra_n = "\n\nPor cierto, " + (n or "don Antonio") + ": aún me falta saber de su medicina: " + ", ".join(o["medicamento"] + " (" + o["hora"] + ")" for o in pm) + ". ¿Ya la tomó?"
            bot_n = ["tomé " + o["medicamento"] + " (" + o["hora"] + ")" for o in pm] + ["tomé todas mis medicinas"]
    if re.search(r"(tom[eé]|pastilla|medicamento)", t, re.I):
        pend = sb_tomas_pendientes(pid0, solo_medicinas=True)
        if len(pend) == 1:
            sb_confirmar_toma_id(pid0, pend[0]["id"])
            msg = "¡Qué bien, " + (n or "don Antonio") + "! Anoto con cariño su " + pend[0]["medicamento"] + " de las " + pend[0]["hora"] + " como tomado. 💙"
            return jsonify({"texto": msg, "audio": tts(texto_voz(msg)) or "", "triage": "normal", "valores": {}, "botones": []})
        if len(pend) > 1:
            PEND[pid0] = {"tipo": "cual_med", "opts": pend}
            msg = "¡Me da gusto! ¿Cuál de sus medicinas tomó? Dígame el nombre o el número:\n" + "\n".join(str(i + 1) + ". " + o["medicamento"] + " (" + o["hora"] + ")" for i, o in enumerate(pend))
            return jsonify({"texto": msg + "\nSi tomó varias, puede decirme los números o tocar: tomé todas mis medicinas.", "audio": tts(texto_voz(msg)) or "", "triage": "normal", "valores": {}, "botones": ["tomé " + o["medicamento"] + " (" + o["hora"] + ")" for o in pend] + ["tomé todas mis medicinas"]})
    if re.search(r"(me med[ií]|me chequ[eé])", t, re.I):
        PEND[pid0] = {"tipo": "numeros"}
        msg = "¡Muy bien! Dígame su numerito, por favor. Si fue presión, algo como 120/80; si fue glucosa, algo como 95. También puede mandarme la foto de su aparato con el botón de camarita."
        return jsonify({"texto": msg, "audio": tts(texto_voz(msg)) or "", "triage": "normal", "valores": {}, "botones": []})
    lp = d.get("lang", "auto")
    lang = lp if lp in ("es", "en") else detectar_idioma(t)
    return finalizar(generar_texto(contexto(tel or n) + "\nEl paciente escribe: " + t + sufijo_lang(lang), lang), "web", "texto", t, tel or n, n, lang, extra=extra_n, botones=bot_n)


@app.route("/api/foto", methods=["POST"])
def api_foto():
    try:
        contar("web"); contar("fotos")
        f = request.files.get("foto")
        n, tel = datos_pac(request.form.get("pac", ""))
        extra_n = ""
        bot_n = None
        pm = sb_tomas_pendientes(tel or n, solo_medicinas=True)
        if pm:
            extra_n = "\n\nPor cierto, " + (n or "don Antonio") + ": aún me falta saber de su medicina: " + ", ".join(o["medicamento"] + " (" + o["hora"] + ")" for o in pm) + ". ¿Ya la tomó?"
            bot_n = ["tomé " + o["medicamento"] + " (" + o["hora"] + ")" for o in pm] + ["tomé todas mis medicinas"]
        u = USU.setdefault(tel or n or "anon", {"msgs":0,"fotos":0,"voces":0}); u["fotos"] += 1
        lang = request.form.get("lang", "es")
        datos = comprimir_img(f.read())
        b64 = base64.b64encode(datos).decode()
        crudo = generar_foto(b64, f.mimetype or "image/jpeg", lang)
        if not crudo:
            raise RuntimeError("vision sin resultado")
        return finalizar(crudo, "web", "foto", "(foto)", tel or n, n, lang, extra=extra_n, botones=bot_n)
    except Exception as e:
        fallo(f"foto: {str(e)[:60]}")
        msg = "No pude leer su foto esta vez. Intente de nuevo, o escriba su numerito con confianza."
        return jsonify({"texto": msg, "audio": tts(texto_voz(msg)) or "", "triage": "normal", "valores": {}, "botones": []})

@app.route("/api/voz", methods=["POST"])
def api_voz():
    contar("web"); contar("voces")
    f = request.files.get("audio")
    n, tel = datos_pac(request.form.get("pac", ""))
    u = USU.setdefault(tel or n or "anon", {"msgs":0,"fotos":0,"voces":0}); u["voces"] += 1
    lang = request.form.get("lang", "es")
    return finalizar(generar_voz(f.read(), f.mimetype or "audio/webm", lang), "web", "voz", "(voz)", tel or n, n, lang)

@app.route("/api/registro", methods=["POST"])
def api_registro():
    d = request.get_json(force=True)
    pid = (d.get("tel") or d.get("nombre") or "").strip()
    if not pid: return jsonify({"ok": False})
    try:
        requests.post(SUPABASE_URL + "/rest/v1/pacientes", headers=_sb_headers(),
                      json={"id": pid, "nombre": d.get("nombre", ""), "edad": d.get("edad") or None,
                            "sexo": d.get("sexo", ""), "diagnostico": d.get("diagnostico", ""),
                            "medicamentos": d.get("medicamentos", ""), "medico": d.get("medico", ""),
                            "medico_tel": d.get("medico_tel", ""), "medico_mail": d.get("medico_mail", ""),
                            "toma_presion": bool(d.get("toma_presion", True)),
                            "mide_glucosa": bool(d.get("mide_glucosa", True)),
                            "cuidador_nombre": d.get("cuidador_nombre", ""),
                            "cuidador_tel": d.get("cuidador_tel", ""),
                            "cuidador_parentesco": d.get("cuidador_parentesco", "")}, timeout=8)
        tomas = d.get("tomas") or []
        if tomas:
            requests.delete(SUPABASE_URL + "/rest/v1/tomas", headers=_sb_headers(),
                            params={"pac_id": "eq." + pid}, timeout=8)
            for t in tomas:
                for h in t.get("horas", []):
                    requests.post(SUPABASE_URL + "/rest/v1/tomas", headers=_sb_headers(),
                                  json={"pac_id": pid, "medicamento": t.get("nombre", ""), "hora": h}, timeout=8)
        registrar(pid, d.get("nombre", ""))
        return jsonify({"ok": True})
    except Exception as e:
        fallo(f"registro: {str(e)[:60]}")
        return jsonify({"ok": False})

@app.route("/api/expediente")
def api_expediente():
    pid = request.args.get("pac", "")
    out = {"paciente": {}, "tomas": []}
    if SUPABASE_URL and SUPABASE_KEY and pid:
        try:
            r = requests.get(SUPABASE_URL + "/rest/v1/pacientes", headers=_sb_headers(),
                             params={"id": "eq." + pid}, timeout=6)
            out["paciente"] = (r.json() or [{}])[0] if r.ok else {}
            r2 = requests.get(SUPABASE_URL + "/rest/v1/tomas", headers=_sb_headers(),
                              params={"pac_id": "eq." + pid, "activo": "eq.true", "select": "medicamento,hora", "order": "hora.asc"}, timeout=6)
            out["tomas"] = r2.json() if r2.ok else []
        except Exception as e:
            fallo(f"expediente: {str(e)[:60]}")
    return jsonify(out)

@app.route("/stats")
def stats():
    return jsonify({"uso": USO, "proveedores": PROV, "usuarios": USU, "errores": ERRORES, "pacientes": list(PAC.keys()), "bitacora": BITACORA[-50:]})

@app.route("/api/citas")
def api_citas():
    pid = request.args.get("pac", "")
    mes = request.args.get("mes", "")
    items = []
    if SUPABASE_URL and SUPABASE_KEY and pid and mes:
        try:
            y, m, _ = mes.split("-")
            nm = int(m) + 1 if int(m) < 12 else 1
            ny = int(y) if int(m) < 12 else int(y) + 1
            sig = f"{ny}-{nm:02d}-01"
            r = requests.get(SUPABASE_URL + "/rest/v1/citas", headers=_sb_headers(),
                             params=[("pac_id", "eq." + pid), ("fecha", "gte." + mes),
                                     ("fecha", "lt." + sig), ("select", "fecha,hora,lugar,doctor,notas")], timeout=6)
            items = r.json() if r.ok else []
        except Exception as e:
            fallo(f"supabase citas cal: {str(e)[:60]}")
    return jsonify({"items": items})

@app.route("/api/recordatorios")
def recordatorios():
    pid = request.args.get("pac", "")
    out = []
    nombre = ""
    if SUPABASE_URL and SUPABASE_KEY and pid:
        try:
            rp = requests.get(SUPABASE_URL + "/rest/v1/pacientes", headers=_sb_headers(),
                              params={"id": "eq." + pid, "select": "nombre"}, timeout=6)
            nombre = ((rp.json() or [{}])[0].get("nombre", "") if rp.ok else "")
            hoy = time.strftime("%Y-%m-%d")
            ayer = time.strftime("%Y-%m-%d", time.localtime(time.time() - 86400))
            rt = requests.get(SUPABASE_URL + "/rest/v1/tomas", headers=_sb_headers(),
                              params={"pac_id": "eq." + pid, "activo": "eq.true", "select": "id,medicamento,hora"}, timeout=6)
            tomas = rt.json() if rt.ok else []
            ok = requests.get(SUPABASE_URL + "/rest/v1/tomas_ok", headers=_sb_headers(),
                              params={"pac_id": "eq." + pid, "select": "toma_id,fecha"}, timeout=6)
            done = set((str(x["toma_id"]), x["fecha"]) for x in (ok.json() if ok.ok else []))
            def es_med(m): return not any(k in m.lower() for k in ["presion", "presión", "glucosa", "chequeo", "medicion", "medición"])
            def frase(t):
                if es_med(t["medicamento"]):
                    return "su " + t["medicamento"] + " de las " + t["hora"] + ". ¿Me cuenta si ya lo tomo? Lo anoto con cariño."
                return "su chequeo de las " + t["hora"] + ". ¿Ya se midio? Mandeme el numerito y lo guardo en su bitacora."
            lineas = []
            g_ayer = [t for t in tomas if (str(t["id"]), ayer) not in done]
            g_man = [t for t in tomas if t["hora"] < "12:00" and (str(t["id"]), hoy) not in done]
            g_tar = [t for t in tomas if t["hora"] >= "12:00" and (str(t["id"]), hoy) not in done]
            if g_ayer:
                lineas.append("• Ayer quedo pendiente: " + "; ".join(t["medicamento"] + " (" + t["hora"] + ")" for t in g_ayer) + ". ¿Me cuenta si lo tomo?")
            if g_man:
                lineas.append("• Hoy por la manana: " + "; ".join(frase(t) for t in g_man))
            if g_tar:
                lineas.append("• Hoy por la tarde: " + "; ".join(frase(t) for t in g_tar))
            rc = requests.get(SUPABASE_URL + "/rest/v1/citas", headers=_sb_headers(),
                              params=[("pac_id", "eq." + pid), ("recordado", "eq.false"), ("fecha", "gte." + hoy), ("order", "fecha.asc"), ("limit", "1"), ("select", "id,fecha,hora,lugar,doctor,notas")], timeout=6)
            cit = (rc.json() or [])[:1] if rc.ok else []
            if cit:
                c = cit[0]
                lineas.append("• 📅 Su proxima cita: " + str(c.get("fecha", "")) + " a las " + c.get("hora", "") + " en " + c.get("lugar", "") + " con " + c.get("doctor", "") + ". " + c.get("notas", ""))
                requests.patch(SUPABASE_URL + "/rest/v1/citas?id=eq." + str(c["id"]), headers=_sb_headers(), json={"recordado": True}, timeout=6)
            if lineas:
                aviso = "🌞 Hola" + ((" " + nombre) if nombre else "") + ". Le comparto su guia con carino:\n" + "\n".join(lineas) + "\nCuando guste me cuenta y lo anoto en su bitacora. 💙"
                out.append({"id": -1, "texto": aviso, "audio": tts(texto_voz(aviso)) or ""})
        except Exception as e:
            fallo(f"supabase recordatorios: {str(e)[:60]}")
    texto_audio = out[0]["texto"] if out else ""
    return jsonify({"items": out, "nombre": nombre, "audio": tts(texto_voz(texto_audio)) or ""})

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
            crudo = generar_texto(contexto(de) + "\nEl paciente escribe por WhatsApp: " + msg["text"]["body"] + sufijo_lang(lang), lang)
        elif msg["type"] == "image":
            mid = msg["image"]["id"]
            r1 = requests.get("https://graph.facebook.com/v21.0/" + mid, headers={"Authorization": "Bearer " + str(META_TOKEN)})
            r2 = requests.get(r1.json()["url"], headers={"Authorization": "Bearer " + str(META_TOKEN)})
            crudo = generar_foto(base64.b64encode(r2.content).decode(), "image/jpeg", lang)
        else:
            enviar_wa(de, "Recibi tu mensaje. En esta version leo fotos y texto.")
            return "OK", 200
        texto, triage, valores, meds, rut = limpiar(crudo or "")
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
 .typing span{display:inline-block;width:9px;height:9px;border-radius:50%;background:#0f274d;margin:0 2px;animation:tp 1s infinite}
 .typing span:nth-child(2){animation-delay:.2s}
 .typing span:nth-child(3){animation-delay:.4s}
 @keyframes tp{0%,100%{opacity:.2;transform:translateY(0)}50%{opacity:1;transform:translateY(-4px)}}
 #bar{display:flex;gap:8px;padding:10px;background:#fff;border-top:2px solid #dde}
 #bar input{flex:1;font-size:1em;padding:12px;border-radius:12px;border:2px solid #bbc}
 #bar button{font-size:1.2em;border:none;border-radius:12px;background:#0f274d;color:#fff;padding:0 16px}
 #inst{display:none;margin:8px auto;background:#e8f5e9;border:2px solid #4c8;padding:8px 16px;font-size:.9em;border-radius:12px}
 #ficha{margin:10px auto;background:#fff;padding:14px;border-radius:14px;box-shadow:0 1px 6px rgba(0,0,0,.2);text-align:center}
 #ficha input{font-size:1em;margin:6px;padding:10px;border-radius:10px;border:2px solid #bbc;display:block;width:80%;margin-left:auto;margin-right:auto}
 #calpanel{position:fixed;right:10px;bottom:90px;width:250px;background:#fff;border:1px solid #bbb;border-radius:12px;padding:8px;z-index:60;font-size:.85em;box-shadow:0 2px 10px rgba(0,0,0,.25)}
 @media(max-width:700px){#calpanel{position:static;width:auto;margin:8px auto}}
body.alto{background:#000}
body.alto header{background:#000;border-bottom:2px solid #ffeb3b}
body.alto .msg.bot{background:#111;color:#ffeb3b;border:1px solid #ffeb3b}
body.alto .msg.user{background:#333;color:#fff}
body.alto #chat{background:#000}
</style>
</head>
<body>
<header>❤️ Salud Mexicali <span id="quien" style="position:absolute;right:12px;top:10px;font-size:.75em"></span>
 <div id="hdr2">
  <button id="Lauto" class="on">AUTO</button><button id="Les">ES</button><button id="Len">EN</button>
  <button id="fmas">A+</button><button id="fmenos">A−</button>
 <button onclick="document.body.classList.toggle('alto')" style="margin-left:6px;padding:2px 10px;border-radius:12px;border:1px solid #fff;background:transparent;color:#fff;font-size:.8em">🔲</button>
 <button onclick="abreFicha(true)" style="margin-left:6px;padding:2px 10px;border-radius:12px;border:1px solid #fff;background:transparent;color:#fff;font-size:.8em">✏️ Mis datos</button>
 </div>
</header>
<button id="inst">📲 Instalar como app</button>
<div id="chat"></div>
<div id="ficha" style="display:none;max-width:460px;margin:10px auto;padding:14px;background:#fff;border-radius:12px;max-height:70vh;overflow:auto">
<h3 style="margin:0 0 8px">📋 Hoja de ingreso</h3>
<label>Nombre completo*<br><input id="fnom" style="width:100%;padding:8px;font-size:1em"></label><br>
<label>Teléfono*<br><input id="ftel" style="width:100%;padding:8px;font-size:1em"></label><br>
<label>Edad<br><input id="fedad" type="number" style="width:100%;padding:8px;font-size:1em"></label><br>
<label>Sexo<br><select id="fsexo" style="width:100%;padding:8px;font-size:1em"><option value="">—</option><option>Femenino</option><option>Masculino</option></select></label><br>
<label>Diagnóstico (ej. hipertensión, diabetes)<br><input id="fdiag" style="width:100%;padding:8px;font-size:1em"></label><br>
<label>¿Se checa la presión? <input id="ftapres" type="checkbox" checked>  ¿Se mide la glucosa? <input id="fgluc" type="checkbox" checked></label><br>
<label>Médico(s)<br><input id="fmed" style="width:100%;padding:8px;font-size:1em"></label><br>
<label>Teléfono del médico<br><input id="fmedtel" style="width:100%;padding:8px;font-size:1em"></label><br>
<label>Correo del médico<br><input id="fmedmail" type="email" style="width:100%;padding:8px;font-size:1em"></label><br>
<b>Medicamentos y horarios</b>
<div id="ftomas"></div>
<button onclick="agregaToma('','')" style="margin:4px 0;padding:6px 12px;border-radius:8px;border:1px solid #0f274d;background:#fff">➕ Agregar medicamento</button><br>
<label>👤 Cuidador: nombre<br><input id="fcuinom" style="width:100%;padding:8px;font-size:1em"></label><br>
<label>Teléfono del cuidador<br><input id="fcuitel" style="width:100%;padding:8px;font-size:1em"></label><br>
<label>Parentesco<br><input id="fcuipar" style="width:100%;padding:8px;font-size:1em"></label><br>
<button id="fok" style="margin-top:8px;padding:10px 20px;font-size:1.1em;border:none;border-radius:10px;background:#0f274d;color:#fff">💾 Guardar mi expediente</button>
</div>
<div id="calpanel"><div style="text-align:center"><button onclick="calMes(-1)">⬅️</button> <b id="caltit"></b> <button onclick="calMes(1)">➡️</button></div><div id="calbody"></div><div id="caldet" style="margin-top:6px;font-size:.95em"></div></div>
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
function pinta(q,t,cls){const aud=cls&&cls.length>100?cls:'';const d=document.createElement('div');d.className='b '+(q?'yo':'bot')+(aud?'':(cls||''));d.innerHTML=t;if(aud){const au=document.createElement('audio');au.controls=true;au.src='data:audio/mpeg;base64,'+aud;d.appendChild(au)}chat.appendChild(d);const au=d.querySelector('audio');if(au){if(window._yaToco){au.play().catch(()=>{});}else{window._audPend=au;}}chat.scrollTop=chat.scrollHeight;return d}
function textoVozJS(t){return (t||'').replace(/[\\u{1F300}-\\u{1FAFF}\\u{2600}-\\u{27BF}\\u{2B00}-\\u{2BFF}\\u{1F1E6}-\\u{1F1FF}\\u{2764}\\u{2665}\\u{2705}]/gu,'').replace(/\\s+/g,' ').trim();}
function vozFem(){try{const vs=speechSynthesis.getVoices();return vs.find(v=>/Dalia|Mónica|Monica|Paulina|Sabina|Elvira|female/i.test(v.name))||vs.find(v=>(v.lang||'').toLowerCase().startsWith('es'))||null;}catch(e){return null;}}
function leer(t){try{const u=new SpeechSynthesisUtterance(textoVozJS(t));const v=vozFem();if(v)u.voice=v;u.lang=v?v.lang:'es-MX';u.rate=0.95;speechSynthesis.cancel();speechSynthesis.speak(u);}catch(e){}}
window._avisoPend=null;window._audPend=null;window._yaToco=false;
function leerAuto(t){window._avisoPend=t;try{const u=new SpeechSynthesisUtterance(textoVozJS(t));const v=vozFem();if(v)u.voice=v;u.lang=v?v.lang:'es-MX';u.rate=0.95;speechSynthesis.cancel();speechSynthesis.speak(u);window._avisoPend=null;}catch(e){}}
['pointerdown','keydown','touchstart'].forEach(ev=>window.addEventListener(ev,function(){window._yaToco=true;if(window._avisoPend){leer(window._avisoPend);window._avisoPend=null;}if(window._audPend){window._audPend.play().catch(()=>{});window._audPend=null;}}));
function pintaAviso(t,aud){pinta(false,t+`<br><button onclick="mandar('ya tomé mi medicina')" style="margin:4px;padding:8px 14px;border-radius:10px;border:none;background:#1b5e20;color:#fff;font-size:1em">✔ Ya tomé mi medicina</button><button onclick="mandar('ya me medí')" style="margin:4px;padding:8px 14px;border-radius:10px;border:none;background:#0f274d;color:#fff;font-size:1em">✔ Ya me medí</button>`,aud);}
function typingOn(){typingOff();const d=document.createElement('div');d.className='msg bot typing';d.id='typing';d.innerHTML='<span></span><span></span><span></span> <small id="crono">0s</small>';document.getElementById('chat').appendChild(d);d.scrollIntoView({behavior:'smooth'});
 window._crono=0;window._cronoI=setInterval(()=>{window._crono++;const s=document.getElementById('crono');if(s)s.textContent=window._crono+'s';},1000);
 try{const u=new SpeechSynthesisUtterance('Recibí su información. La estoy revisando con calma, un momento por favor.');const v=vozFem();if(v)u.voice=v;u.lang=v?v.lang:'es-MX';u.rate=0.95;speechSynthesis.cancel();speechSynthesis.speak(u);}catch(e){}}
function typingOff(){const d=document.getElementById('typing');if(d)d.remove();if(window._cronoI){clearInterval(window._cronoI);window._cronoI=null;}}
let calY=0,calM=0;
function abreCal(){const p=document.getElementById('calpanel');p.style.display=p.style.display==='none'?'block':'none';if(p.style.display==='block'&&!calY){const h=new Date();calY=h.getFullYear();calM=h.getMonth();}pintaCal();}
function calMes(d){calM+=d;if(calM<0){calM=11;calY--}if(calM>11){calM=0;calY++}pintaCal();}
function initCal(){if(!calY){const h=new Date();calY=h.getFullYear();calM=h.getMonth();}pintaCal();}
function pintaCal(){const d0=JSON.parse(pac()||'{}');const id=d0.t||d0.n||'';const mes=calY+'-'+String(calM+1).padStart(2,'0')+'-01';
 fetch('/api/citas?pac='+encodeURIComponent(id)+'&mes='+mes).then(r=>r.json()).then(d=>{
    window.diasDet={};const dias={};
    (d.items||[]).forEach(c=>{const dd=Number(c.fecha.slice(8,10));dias[dd]=(dias[dd]||'')+'🩺';window.diasDet[dd]=(window.diasDet[dd]||[]).concat([c]);});
    document.getElementById('caltit').textContent=['enero','febrero','marzo','abril','mayo','junio','julio','agosto','septiembre','octubre','noviembre','diciembre'][calM]+' '+calY;
    const prim=new Date(calY,calM,1);const nd=new Date(calY,calM+1,0).getDate();let h='<table style="width:100%;text-align:center;font-size:1.15em;border-collapse:collapse"><tr>';
    ['D','L','M','M','J','V','S'].forEach(x=>h+='<th>'+x+'</th>');h+='</tr><tr>';
    for(let i=0;i<prim.getDay();i++)h+='<td></td>';
    for(let dd=1;dd<=nd;dd++){const mk=dias[dd];h+='<td onclick="verDia('+dd+')" style="cursor:pointer;padding:8px;border:1px solid #ccc;'+(mk?'background:#ffd6d6;font-weight:bold':'')+'">'+dd+(mk||'')+'</td>';if((prim.getDay()+dd)%7===0)h+='</tr><tr>';}
    h+='</tr></table>';document.getElementById('calbody').innerHTML=h;const det=document.getElementById('caldet');if(det)det.innerHTML='';}).catch(()=>{});}
function verDia(d){const cs=window.diasDet&&window.diasDet[d]||[];document.getElementById('caldet').innerHTML=cs.length?cs.map(c=>'📅 Día '+d+': '+c.hora+' en '+c.lugar+' con '+c.doctor+'. '+(c.notas||'')).join('<br>'):'Sin citas ese día.';}
function pensando(){quitando();pinta(false,'<span class="dots"><i></i><i></i><i></i></span> Trabajando en tu respuesta… <span id="tsec">0</span> s');thinkS=0;thinkT=setInterval(()=>{thinkS++;const e=document.getElementById('tsec');if(e)e.textContent=thinkS},1000)}
function quitando(){if(thinkT){clearInterval(thinkT);thinkT=null}}
function agregaAudio(el,texto,lang){fetch('/api/tts',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({texto:texto,lang:lang})}).then(r=>r.json()).then(a=>{if(a.audio){const au=document.createElement('audio');au.controls=true;au.src='data:'+(a.mime||'audio/mpeg')+';base64,'+a.audio;el.appendChild(au);chat.scrollTop=chat.scrollHeight}}).catch(()=>{})}
function botMsg(d){const t=(d.texto||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/\\n/g,'<br>');
 const el=pinta(false,t,d.triage==='critico'?' crit':'');
 if(d.texto)agregaAudio(el,d.texto,d.lang||'es');
 if(d.botones&&d.botones.length){pinta(false,d.botones.map(b=>`<button onclick="mandar('${b}')" style="margin:4px;padding:8px 14px;border-radius:10px;border:none;background:#0f274d;color:#fff;font-size:1em">${b}</button>`).join(''))}}
async function api(url,body){pensando();
 typingOn();try{const r=await fetch(url,{method:'POST',body});
 if(!r.ok){typingOff();quitando();chat.lastChild.remove();pinta(false,'⚠️ Error '+r.status+'. Abre /test para ver por que.');return}
     const d=await r.json();typingOff();quitando();chat.lastChild.remove();botMsg(d)}
 catch(e){typingOff();quitando();chat.lastChild.remove();pinta(false,'⚠️ Sin conexion con el servidor: '+e)}}
function agregaToma(nom, hors){const d=document.createElement('div');d.style.margin='4px 0';d.innerHTML='<input placeholder="Medicamento (ej. Losartán 50mg)" style="width:60%;padding:6px" value="'+nom+'"> <input placeholder="Horas o momento: 08:00,20:00 / manana y noche / antes de dormir" style="width:30%;padding:6px" value="'+hors+'">';document.getElementById('ftomas').appendChild(d);}
function normHoras(s){const map=[["antes de dormir","22:00"],["dormir","22:00"],["manana","08:00"],["mañana","08:00"],["mediodia","14:00"],["mediodía","14:00"],["tarde","17:00"],["noche","21:00"]];
 return s.split(/[,+&]/i).map(x=>x.trim().toLowerCase()).filter(x=>x).map(x=>{
     if(/^\d{1,2}(:\d{2})?$/.test(x)){return x.length<=2?x.padStart(2,"0")+":00":(x.length===4?x.slice(0,2)+":"+x.slice(2):x);}
     for(const p of map){if(x.includes(p[0]))return p[1];}
     return "";}).filter(x=>x);}
function abreFicha(pref){const f=document.getElementById('ficha');f.style.display='block';if(!pref)return;const d0=JSON.parse(pac()||'{}');const id=d0.t||d0.n||'';if(!id)return;
 fetch('/api/expediente?pac='+encodeURIComponent(id)).then(r=>r.json()).then(d=>{const p=d.paciente||{};
        document.getElementById('fnom').value=p.nombre||'';document.getElementById('ftel').value=id;document.getElementById('fedad').value=p.edad||'';document.getElementById('fsexo').value=p.sexo||'';document.getElementById('fdiag').value=p.diagnostico||'';document.getElementById('ftapres').checked=p.toma_presion!==false;document.getElementById('fgluc').checked=p.mide_glucosa!==false;document.getElementById('fmed').value=p.medico||'';document.getElementById('fmedtel').value=p.medico_tel||'';document.getElementById('fmedmail').value=p.medico_mail||'';document.getElementById('fcuinom').value=p.cuidador_nombre||'';document.getElementById('fcuitel').value=p.cuidador_tel||'';document.getElementById('fcuipar').value=p.cuidador_parentesco||'';
    document.getElementById('ftomas').innerHTML='';const g={};(d.tomas||[]).forEach(t=>{g[t.medicamento]=g[t.medicamento]||[];g[t.medicamento].push(t.hora);});Object.keys(g).forEach(k=>agregaToma(k,g[k].join(',')));});}
if(!pac()){document.getElementById('ficha').style.display='block'}
document.getElementById('fok').onclick=()=>{const nom=document.getElementById('fnom').value.trim();const tel=document.getElementById('ftel').value.trim();if(!nom||!tel){alert('Por favor nombre y telefono, gracias.');return;}
 const tomas=[];document.querySelectorAll('#ftomas div').forEach(d=>{const i=d.querySelectorAll('input');const n=i[0].value.trim();const hs=normHoras(i[1].value);if(n&&hs.length)tomas.push({nombre:n,horas:hs});});
 const body={nombre:nom,tel:tel,edad:document.getElementById('fedad').value,sexo:document.getElementById('fsexo').value,diagnostico:document.getElementById('fdiag').value,toma_presion:document.getElementById('ftapres').checked,mide_glucosa:document.getElementById('fgluc').checked,medico:document.getElementById('fmed').value,medico_tel:document.getElementById('fmedtel').value,medico_mail:document.getElementById('fmedmail').value,cuidador_nombre:document.getElementById('fcuinom').value,cuidador_tel:document.getElementById('fcuitel').value,cuidador_parentesco:document.getElementById('fcuipar').value,tomas:tomas,medicamentos:tomas.map(t=>t.nombre+' '+t.horas.join(',')).join('; ')};
 fetch('/api/registro',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}).then(()=>{localStorage.setItem('pac',JSON.stringify({n:nom,t:tel}));document.getElementById('ficha').style.display='none';pintaNombre();initCal();pinta(false,'Gracias, '+nom+'. Su expediente queda guardado con carino. 💙');});};
function enviarTexto(t){t=(t||'').trim();if(!t)return;document.getElementById('txt').value='';pinta(true,t);api('/api/text',JSON.stringify({texto:t,pac:pac(),lang:langPref}));}
function mandar(t){enviarTexto(t);}
document.getElementById('txt').onkeydown=e=>{if(e.key==='Enter')enviarTexto(e.target.value)};
document.getElementById('benv').onclick=()=>enviarTexto(document.getElementById('txt').value);
document.getElementById('bfoto').onclick=()=>document.getElementById('ffoto').click();
function mandaFoto(f){if(!f)return;pinta(false,'📷 Recibí su foto. La estoy leyendo con calma, un momento por favor...');
 const fd=new FormData();fd.append('foto',f);fd.append('pac',pac());fd.append('lang',langPref==='auto'?'es':langPref);
 typingOn();fetch('/api/foto',{method:'POST',body:fd}).then(r=>{typingOff();if(!r.ok)throw new Error('foto '+r.status);return r.json()}).then(d=>{if(chat.lastChild)chat.lastChild.remove();if(d&&d.texto)botMsg(d);else pinta(false,'No pude leer su foto esta vez. Intente de nuevo, o escriba su numerito con confianza.')}).catch(()=>{typingOff();if(chat.lastChild)chat.lastChild.remove();pinta(false,'No pude leer su foto esta vez. Intente de nuevo, o escriba su numerito con confianza.')});};
document.getElementById('ffoto').onchange=e=>mandaFoto(e.target.files[0]);
window.addEventListener('dragover',function(e){e.preventDefault();});
window.addEventListener('drop',function(e){e.preventDefault();const f=e.dataTransfer&&e.dataTransfer.files&&e.dataTransfer.files[0];if(f&&f.type.indexOf('image/')===0)mandaFoto(f);});
window.addEventListener('paste',function(e){const it=e.clipboardData&&e.clipboardData.items;for(let i=0;i<(it||[]).length;i++){if(it[i].type.indexOf('image/')===0){mandaFoto(it[i].getAsFile());break;}}});
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
(function(){const d0=JSON.parse(pac()||'{}');const id=d0.t||d0.n||'';if(id){fetch('/api/recordatorios?pac='+encodeURIComponent(id)).then(r=>r.json()).then(d=>{if(d.nombre){const q=document.getElementById('quien');if(q)q.textContent=d.nombre;} (d.items||[]).forEach(x=>pintaAviso(x.texto,x.audio));}).catch(()=>{});}})();
initCal();
fetch('/api/bienvenida').then(r=>r.json()).then(d=>pinta(false,d.texto,d.audio)).catch(()=>pinta(false,'Hola, soy su asistente de salud. ❤️'));
</script>
</body>
</html>"""

if __name__ == "__main__":
    app.run(port=10000)