import os
import re
import json
import html
import logging
import calendar
import threading
import unicodedata
from collections import deque
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import gspread
import httpx
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from google.oauth2.service_account import Credentials

logging.basicConfig(level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)  # evita imprimir el token en logs
log = logging.getLogger("finanzas-bot")

# ======================================================================
# Configuración (variables de entorno)
# ======================================================================
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
GROQ_API_KEY = os.environ["GROQ_API_KEY"]
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "cambia-este-secreto")
ALLOWED_USER_ID = os.environ.get("ALLOWED_USER_ID")
GROQ_MODEL = os.environ.get("GROQ_MODEL", "openai/gpt-oss-20b")
NOMBRE = os.environ.get("NOMBRE", "Luis")
BASE_URL = os.environ.get("RENDER_EXTERNAL_URL") or os.environ.get("BASE_URL")
TZ = ZoneInfo("America/Bogota")

TG_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

SHEET_TAB = "Movimientos"
HEADERS = ["Fecha", "Hora", "Tipo", "Monto", "Descripción", "Categoría", "Medio de pago", "ID mensaje"]

CATS_GASTO = ["Comida", "Transporte", "Mercado", "Servicios", "Salud",
              "Entretenimiento", "Ropa", "Hogar", "Educación", "Otros"]
CATS_INGRESO = ["Salario", "Ventas", "Regalo", "Otros ingresos"]
EMOJI = {
    "Comida": "🍽", "Transporte": "🚌", "Mercado": "🛒", "Servicios": "💡",
    "Salud": "🩺", "Entretenimiento": "🎬", "Ropa": "👕", "Hogar": "🏠",
    "Educación": "📚", "Otros": "🧾", "Salario": "💼", "Ventas": "🛍",
    "Regalo": "🎁", "Otros ingresos": "💰",
}

MESES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
         "agosto", "septiembre", "octubre", "noviembre", "diciembre"]
MESES_CORTO = ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"]
DIAS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
DIAS_CORTO = ["lun", "mar", "mié", "jue", "vie", "sáb", "dom"]

esc = html.escape

# ======================================================================
# Google Sheets
# ======================================================================
_ws = None


def get_ws():
    """Conecta (una sola vez) y devuelve la pestaña 'Movimientos'."""
    global _ws
    if _ws is None:
        creds = Credentials.from_service_account_info(
            json.loads(os.environ["GOOGLE_CREDS_JSON"]),
            scopes=["https://www.googleapis.com/auth/spreadsheets"],
        )
        ss = gspread.authorize(creds).open_by_key(os.environ["SHEET_ID"])
        try:
            ws = ss.worksheet(SHEET_TAB)
        except gspread.WorksheetNotFound:
            ws = ss.add_worksheet(title=SHEET_TAB, rows=1000, cols=len(HEADERS))
        if not ws.row_values(1):
            ws.append_row(HEADERS)
        _ws = ws
    return _ws


def parse_rows(values):
    """Convierte las filas crudas de la hoja en diccionarios (ignora encabezado y filas dañadas)."""
    out = []
    for i, v in enumerate(values[1:], start=2):
        v = list(v) + [""] * (len(HEADERS) - len(v))
        try:
            fecha = date.fromisoformat(v[0].strip())
            monto = int(re.sub(r"[^\d]", "", str(v[3])))
        except Exception:
            continue
        out.append({
            "row": i, "fecha": fecha, "hora": v[1].strip(),
            "tipo": "Ingreso" if v[2].strip().lower().startswith("ing") else "Gasto",
            "monto": monto, "desc": v[4].strip(), "cat": v[5].strip() or "Otros",
            "medio": v[6].strip() or "No especificado", "msgid": v[7].strip(),
        })
    return out


def load_rows():
    return parse_rows(get_ws().get_all_values())


# ======================================================================
# Utilidades
# ======================================================================
def norm(s) -> str:
    s = unicodedata.normalize("NFD", str(s or "").lower())
    return "".join(c for c in s if unicodedata.category(c) != "Mn").strip()


def fmt_cop(n) -> str:
    return "$" + f"{int(n):,}".replace(",", ".")


def fmt_signed(n) -> str:
    return ("+" if n >= 0 else "-") + fmt_cop(abs(n))


def fmt_fecha(d: date, hora: str = "") -> str:
    s = f"{DIAS_CORTO[d.weekday()]} {d.day} {MESES_CORTO[d.month - 1]}"
    return f"{s} · {hora}" if hora else s


def to_int(v):
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return int(v)
    s = str(v or "").strip()
    if re.fullmatch(r"\d{1,3}([.,]\d{3})+", s):
        s = re.sub(r"[.,]", "", s)
    else:
        s = s.replace(",", ".")
    try:
        return int(float(re.sub(r"[^\d.]", "", s)))
    except Exception:
        return None



# ======================================================================
# Interpretación colombiana: montos, medios de pago y categorías
# ======================================================================

BANCOS = {
    "bancolombia": "Bancolombia",
    "davivienda": "Davivienda",
    "bbva": "BBVA",
    "banco de bogota": "Banco de Bogotá",
    "banco caja social": "Banco Caja Social",
    "banco occidental": "Banco Occidental",
    "av villas": "AV Villas",
    "banco popular": "Banco Popular",
    "scotiabank": "Scotiabank",
    "colpatria": "Banco Colpatria",
    "banco falabella": "Banco Falabella",
    "itau": "Itaú",
    "itáu": "Itaú",
}

# Se prueban primero las frases largas para evitar coincidencias parciales.
BANCOS_ORDENADOS = sorted(BANCOS.items(), key=lambda x: len(x[0]), reverse=True)

# "Luca/lucas/lukas" = miles; "palo/palos" = millones; "teja/tejas" = cien mil.
# Se incluyen expresiones muy usadas, pero no términos demasiado ambiguos como
# "barra", "baro" o "gamba", cuyo valor puede variar según la persona.
MONTO_MULT = re.compile(
    r"(\d+(?:[.,]\d+)?)\s*"
    r"(millones?|millon|mm|m|mil|k|lucas?|lukas?|tejas?|palo(?:s)?)\b",
    re.I,
)

NUMEROS_PALABRA = {
    "un": 1, "uno": 1, "una": 1,
    "dos": 2, "tres": 3, "cuatro": 4, "cinco": 5,
    "seis": 6, "siete": 7, "ocho": 8, "nueve": 9, "diez": 10,
    "once": 11, "doce": 12, "trece": 13, "catorce": 14, "quince": 15,
    "dieciseis": 16, "dieciséis": 16, "diecisiete": 17,
    "dieciocho": 18, "diecinueve": 19, "veinte": 20,
}
MONTO_PALABRA = re.compile(
    r"\b(una?|dos|tres|cuatro|cinco|seis|siete|ocho|nueve|diez|"
    r"once|doce|trece|catorce|quince|dieciseis|dieciséis|diecisiete|"
    r"dieciocho|diecinueve|veinte)\s+"
    r"(lucas?|lukas?|palos?|tejas?|millones?)\b",
    re.I,
)

BANCOS_RE = re.compile(
    r"\b(" + "|".join(re.escape(k) for k, _ in BANCOS_ORDENADOS) + r")\b",
    re.I,
)

def _banco(t: str):
    for k, v in BANCOS_ORDENADOS:
        if re.search(rf"\b{re.escape(k)}\b", t):
            return v
    return None


def preparse_monto(texto):
    """Extrae montos escritos de forma normal o coloquial en Colombia."""
    t = norm(texto)
    if not t:
        return None

    m = MONTO_MULT.search(t)
    if m:
        v = float(m.group(1).replace(",", "."))
        u = m.group(2).lower()
        if u in ("k", "mil", "luca", "lucas", "luka", "lukas"):
            return round(v * 1_000)
        if u in ("teja", "tejas"):
            return round(v * 100_000)
        if u in ("palo", "palos", "m", "mm", "millon", "millones"):
            return round(v * 1_000_000)

    m = MONTO_PALABRA.search(t)
    if m:
        v = NUMEROS_PALABRA.get(m.group(1).lower())
        if v is not None:
            u = m.group(2).lower()
            if u.startswith(("palo", "millon")):
                return v * 1_000_000
            if u.startswith("teja"):
                return v * 100_000
            return v * 1_000

    # "medio palo" / "media teja" son expresiones útiles y poco ambiguas.
    if re.search(r"\bmedio\s+palo\b", t):
        return 500_000
    if re.search(r"\bmedia\s+teja\b", t):
        return 50_000

    # Miles con separador colombiano: 10.000 / 10,000
    m = re.search(r"\b(\d{1,3}(?:[.,]\d{3})+)\b", t)
    if m:
        return int(re.sub(r"[.,]", "", m.group(1)))

    # Enteros de 3 o más dígitos. Evita confundir fechas como "el 15".
    m = re.search(r"\b(\d{3,})\b", t)
    return int(m.group(1)) if m else None


def norm_medio(texto):
    """
    Convierte formas humanas de pago a una etiqueta consistente.
    Ejemplos:
      "con la tarjeta de la banca" -> Tarjeta Bancolombia
      "la de crédito bancolombia" -> Tarjeta de crédito Bancolombia
      "por nequi" -> Nequi
      "en cash" -> Efectivo
    """
    t = norm(texto)
    if not t:
        return None

    if re.search(r"\b(no especificado|ningun[ao]?|n/?a|nose|no se)\b", t):
        return "No especificado"

    if re.search(r"\bnequi\b", t):
        return "Nequi"
    if re.search(r"\bdaviplata\b", t):
        return "Daviplata"
    if re.search(r"\bplin\b", t):
        return "Plin"
    if re.search(r"\befecty\b", t):
        return "Efecty"
    if re.search(r"\b(efectivo|cash|plata|billete|en mano)\b", t):
        return "Efectivo"

    banco = _banco(t)

    # La forma coloquial "la de crédito/débito" debe entenderse como tarjeta.
    credito = bool(re.search(
        r"\b(tarjeta\s+de\s+credito|tarjeta\s+credito|tc|credito)\b", t
    ))
    debito = bool(re.search(
        r"\b(tarjeta\s+de\s+debito|tarjeta\s+debito|td|debito)\b", t
    ))
    tarjeta = bool(re.search(
        r"\b(tarjeta|la\s+de\s+credito|la\s+de\s+debito)\b", t
    ))

    if banco and credito:
        return f"Tarjeta de crédito {banco}"
    if banco and debito:
        return f"Tarjeta de débito {banco}"
    if banco and tarjeta:
        return f"Tarjeta {banco}"
    if credito:
        return "Tarjeta de crédito"
    if debito:
        return "Tarjeta de débito"
    if tarjeta:
        return "Tarjeta"
    return banco


def limpia_desc(raw, monto=None, medio=None):
    """Deja solo el concepto de la compra/ingreso, sin jerga de pago o fecha."""
    t = norm(raw)

    # Montos: primero expresiones con unidad ("10 lucas", "2 palos", etc.).
    t = MONTO_MULT.sub(" ", t)
    t = MONTO_PALABRA.sub(" ", t)
    t = re.sub(r"\bmedio\s+palo\b|\bmedia\s+teja\b", " ", t)
    t = re.sub(r"\b\d{1,3}(?:[.,]\d{3})+\b", " ", t)
    t = re.sub(r"\b\d{3,}\b", " ", t)

    # Bancos y métodos de pago.
    t = BANCOS_RE.sub(" ", t)
    t = re.sub(
        r"\b(tarjeta\s+de\s+credito|tarjeta\s+de\s+debito|tarjeta\s+credito|"
        r"tarjeta\s+debito|tarjeta|tc|td|credito|debito|nequi|daviplata|"
        r"plin|efecty|efectivo|cash|billete|en\s+mano|la\s+de)\b",
        " ", t
    )

    # Fechas y conectores conversacionales muy comunes.
    t = re.sub(
        r"\b(hoy|ayer|antier|anteayer|anoche|esta\s+manana|esta\s+mañana|"
        r"hace|el|la|los|las|de|del|en|y|un|una|unos|unas|por|con|para|pa|"
        r"que|me|yo|mi|mis|compr[eé]|pagu[eé]|gast[eé]|gaste|"
        r"consum[ií]|cog[ií]|lleve|llevar|comprar|pagar|gastar|"
        r"me\s+compre|me\s+gaste|me\s+fui|fue|salio|salió)\b",
        " ", t
    )

    palabras = [p for p in re.split(r"\s+", t.strip()) if p][:5]
    return " ".join(palabras).strip().lower() or "sin descripción"


CAT_CLAVES = [
    ("Comida", """
        pan cafe tinto desayuno almuerzo cena onces arepa empanada buñuelo
        corrientazo mecato comida pizza domicilio domicilios pollo hamburguesa
        perro hotdog restaurante comida rappi
    """),
    ("Mercado", """
        mercado supermercado tienda minimercado fruver verduleria
        d1 ara olimpica carulla exito exito jumbo canasta mercado plaza
    """),
    ("Transporte", """
        uber didi indrive taxi bus buseta transmilenio sitp tm metro
        gasolina tanqueada combustible peaje parqueadero parqueo moto
        pasaje vuelo avion transporte
    """),
    ("Servicios", """
        arriendo luz agua internet wifi celular plan telefono gas
        administracion recibo factura netflix claro movistar tigo wom
        servicios
    """),
    ("Entretenimiento", """
        cine concierto rumba bar baile juego steam spotify fiesta parche
        salida evento
    """),
    ("Ropa", "ropa zapatos zapato camisa pantalon chaqueta vestido tenis gorra"),
    ("Hogar", "mueble aseo lampara casa hogar reparacion arreglo cocina"),
    ("Educación", "curso libro matricula universidad colegio clases estudio"),
    ("Salud", "medico medicina farmacia consulta examen dentista laboratorio salud"),
]


def adivina_cat(desc, ingreso=False, texto_original=None):
    t = norm(f"{desc} {texto_original or ''}")

    if ingreso:
        if re.search(r"\b(salario|nomina|sueldo|quincena|pago\s+nomina)\b", t):
            return "Salario"
        if re.search(r"\b(venta|vendi|vendí|vendi[oó]|cliente|pedido)\b", t):
            return "Ventas"
        if re.search(r"\b(regalo|premio|bono)\b", t):
            return "Regalo"
        return "Otros ingresos"

    for cat, claves in CAT_CLAVES:
        for c in norm(claves).split():
            if re.search(rf"\b{re.escape(c)}\b", t):
                return cat
    return "Otros"


def post_valida(data: dict, texto_original: str) -> dict:
    """
    Segunda capa determinista: corrige errores típicos de la IA sin depender de
    que el modelo haya entendido perfecto la jerga colombiana.
    """
    if not isinstance(data, dict):
        return {"intent": "otro", "respuesta": "No pude interpretar eso. Prueba con: 10 lucas pan con Nequi."}

    intent = str(data.get("intent") or "").lower().strip()

    # El modelo puede devolver varios movimientos. La app los procesa en el handler.
    if intent != "registro":
        if intent == "consulta":
            tipo = str(data.get("tipo") or "Gasto").strip()
            data["tipo"] = tipo if tipo in ("Gasto", "Ingreso", "Todos") else "Gasto"
        return data

    ingreso = str(data.get("tipo", "")).lower().startswith("ing")
    data["tipo"] = "Ingreso" if ingreso else "Gasto"

    # El monto determinista gana cuando el texto contiene una forma colombiana
    # inequívoca (10k, 10 lucas, 2 palos, 50.000, etc.).
    monto_ia = to_int(data.get("monto"))
    monto_texto = preparse_monto(texto_original)
    if monto_texto is not None:
        data["monto"] = monto_texto
    else:
        data["monto"] = monto_ia

    medio_ia = norm_medio(str(data.get("medio_pago") or ""))
    medio_texto = norm_medio(texto_original)
    data["medio_pago"] = medio_texto or medio_ia or "No especificado"

    desc_ia = str(data.get("descripcion") or "").strip()
    desc = limpia_desc(desc_ia or texto_original, data.get("monto"), data["medio_pago"])

    # Si el modelo dejó una descripción vacía, larga o contaminada, se reconstruye
    # desde el mensaje original.
    contaminada = (
        not desc_ia
        or len(desc_ia) > 42
        or len(desc_ia.split()) > 5
        or re.search(r"\d", desc_ia)
        or re.search(
            r"\b(tarjeta|nequi|daviplata|efectivo|cash|credito|debito|"
            r"bancolombia|davivienda|bbva|plin|efecty)\b",
            norm(desc_ia),
        )
    )
    if contaminada or desc == "sin descripción":
        desc = limpia_desc(texto_original, data.get("monto"), data["medio_pago"])

    # Para ingresos, "nómina" debe sobrevivir como concepto.
    if ingreso and re.search(r"\b(nomina|salario|sueldo|quincena)\b", norm(texto_original)):
        desc = "nómina"

    data["descripcion"] = desc[:80] or "sin descripción"
    data["categoria"] = norm_cat(
        data.get("categoria"),
        ingreso,
    )

    # Si la IA dejó "Otros", intenta inferir por el texto completo.
    if data["categoria"] == ("Otros ingresos" if ingreso else "Otros"):
        data["categoria"] = adivina_cat(
            data["descripcion"], ingreso, texto_original
        )

    return data

def norm_cat(c, ingreso: bool) -> str:
    lst = CATS_INGRESO if ingreso else CATS_GASTO
    for x in lst:
        if norm(x) == norm(c):
            return x
    return lst[-1]


def emoji_cat(c: str) -> str:
    return EMOJI.get(c, "🏷")


# ======================================================================
# Fechas y períodos (lo calcula Python, no la IA)
# ======================================================================
def resolve_fecha(data: dict, today: date) -> date:
    f = data.get("fecha")
    if f:
        try:
            d = date.fromisoformat(str(f)[:10])
            if d > today:  # fecha futura: probablemente era del año pasado
                d = d.replace(year=d.year - 1)
            if d <= today:
                return d
        except Exception:
            pass
    dw = data.get("dia_semana")
    if isinstance(dw, int) and not isinstance(dw, bool) and 0 <= dw <= 6:
        return today - timedelta(days=(today.weekday() - dw) % 7)
    da = data.get("dias_atras")
    if isinstance(da, (int, float)) and not isinstance(da, bool) and 0 <= da <= 365:
        return today - timedelta(days=int(da))
    return today


def resolve_period(q: dict, today: date):
    """Devuelve (desde, hasta, etiqueta)."""
    p = str(q.get("periodo") or "mes").lower()
    if p == "hoy":
        return today, today, "hoy"
    if p == "ayer":
        d = today - timedelta(days=1)
        return d, d, "ayer"
    if p == "semana":
        return today - timedelta(days=today.weekday()), today, "esta semana"
    if p == "semana_pasada":
        fin = today - timedelta(days=today.weekday() + 1)
        return fin - timedelta(days=6), fin, "semana pasada"
    if p == "ultimos_7":
        return today - timedelta(days=6), today, "últimos 7 días"
    if p == "ultimos_30":
        return today - timedelta(days=29), today, "últimos 30 días"
    if p == "anio":
        return date(today.year, 1, 1), today, str(today.year)
    if p == "todo":
        return date(2000, 1, 1), today, "todo el historial"
    if p == "mes_pasado":
        fin = today.replace(day=1) - timedelta(days=1)
        return fin.replace(day=1), fin, MESES[fin.month - 1]
    if p == "mes_especifico":
        mes = q.get("mes")
        if isinstance(mes, int) and 1 <= mes <= 12:
            anio = q.get("anio") if isinstance(q.get("anio"), int) else None
            if anio is None:
                anio = today.year if mes <= today.month else today.year - 1
            ini = date(anio, mes, 1)
            fin = date(anio, mes, calendar.monthrange(anio, mes)[1])
            label = MESES[mes - 1] + ("" if anio == today.year else f" {anio}")
            return ini, min(fin, today), label
    return today.replace(day=1), today, MESES[today.month - 1]


def filter_rows(rows, desde, hasta, tipo, filtro):
    out = []
    f = norm(filtro) if filtro else ""
    for r in rows:
        if not (desde <= r["fecha"] <= hasta):
            continue
        if tipo in ("Gasto", "Ingreso") and r["tipo"] != tipo:
            continue
        if f and f not in norm(f"{r['desc']} {r['cat']} {r['medio']}"):
            continue
        out.append(r)
    return out


# ======================================================================
# Telegram
# ======================================================================
def tg(method: str, **payload):
    try:
        r = httpx.post(f"{TG_API}/{method}", json=payload, timeout=30)
        if r.status_code != 200:
            log.warning("Telegram %s -> %s %s", method, r.status_code, r.text[:200])
        return r
    except Exception as e:
        log.warning("Telegram %s falló: %s", method, type(e).__name__)


def send(chat_id, text, keyboard=None):
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if keyboard:
        payload["reply_markup"] = {"inline_keyboard": keyboard}
    tg("sendMessage", **payload)


def edit(chat_id, message_id, text):
    tg("editMessageText", chat_id=chat_id, message_id=message_id, text=text, parse_mode="HTML")


# ======================================================================
# Groq (IA solo para entender el mensaje y redactar una frase)
# ======================================================================
def groq_chat(messages, json_mode=False, temperature=0.0) -> str:
    payload = {"model": GROQ_MODEL, "messages": messages, "temperature": temperature}
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    r = httpx.post(
        GROQ_URL,
        headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
        json=payload,
        timeout=30,
    )
    if r.status_code != 200:
        log.error("Groq %s: %s", r.status_code, r.text[:300])
        r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


SYSTEM_PROMPT = """Eres el cerebro de interpretación de un bot personal de finanzas en Colombia, usando pesos colombianos (COP).

Tu trabajo principal es ENTENDER lo que la persona quiso decir aunque escriba rápido,
con errores, abreviaturas, sin tildes, con jerga colombiana o en lenguaje de chat.
NO le exijas una sintaxis exacta. NO corrijas al usuario. EXTRAe la intención y los datos.

Hoy es {HOY}. Días: lunes=0, martes=1, miércoles=2, jueves=3, viernes=4,
sábado=5, domingo=6.

REGLA GENERAL DE LENGUAJE:
- El usuario puede escribir como habla: "10 lucas de pan con la bancolombia",
  "ayer me gasté 25 lukas en uber", "pagué 30 mil del internet",
  "me entraron 3 palos de nómina", "¿cuánta plata me he gastado este mes?",
  "¿en qué se me fue la plata?", "borra ese gasto de ayer".
- Entiende "de una", "listo", "qué más", "cómo voy", "me gasté", "se me fueron",
  "me salió", "compré", "pagué", "me consignaron", "me entraron", "me llegó".
- La jerga es para COMPRENDER. No inventes datos solo porque una palabra podría significar algo.
- Puedes entender "lucas/lukas", "palo/palos", "teja/tejas" y "k/mil/m/millón".
- "10 lucas" = 10000. "25 lukas" = 25000. "2 palos" = 2000000.
  "50 lucas" = 50000. "una luca" = 1000. "una teja" = 100000.
  "medio palo" = 500000. No uses valores inventados para otras jergas ambiguas.

RESPONDE SIEMPRE CON UN ÚNICO OBJETO JSON VÁLIDO. Nunca pongas markdown,
fences, comentarios ni texto fuera del JSON.

## 1) REGISTRO DE GASTO O INGRESO

Formato de un movimiento:
{"intent":"registro","tipo":"Gasto"|"Ingreso","monto":<entero COP>,"descripcion":"<concepto, max 5 palabras>",
"categoria":"<categoría>","medio_pago":"<medio>","dias_atras":<entero|null>,
"dia_semana":<0-6|null>,"fecha":<"YYYY-MM-DD"|null>}

MONTOS:
- SIEMPRE devuelve monto como entero JSON, nunca cadena.
- "10.000" = 10000; "10,000" = 10000; "25k" = 25000; "25 mil" = 25000.
- "10 lucas" / "10 lukas" = 10000; "2 lucas" = 2000; "una luca" = 1000.
- "2 palos" = 2000000; "medio palo" = 500000.
- "1,5 millones" = 1500000; "3 m" = 3000000.
- "una teja" = 100000.
- "10k" NUNCA es 10. "3 m" NUNCA es 3.
- Si no hay monto entendible, NO inventes uno. Devuelve intent "otro" y pide el monto.

DESCRIPCIÓN:
- Solo el concepto, máximo 5 palabras, preferiblemente minúsculas.
- Nunca metas monto, fecha, banco, método de pago, "tarjeta", "nequi", etc.
- "10 lucas de pan con la tarjeta de Bancolombia" -> "pan"
- "ayer me gasté 25 lukas en Uber por Nequi" -> "uber"
- "pagué 30 mil del internet" -> "internet"
- "me entraron 3 palos de nómina" -> "nómina"
- "me gasté 18 lucas en un corrientazo" -> "corrientazo"

MEDIO DE PAGO:
- "tarjeta de crédito Bancolombia", "tc Bancolombia", "la de crédito de Bancolombia"
  -> "Tarjeta de crédito Bancolombia"
- "tarjeta de débito Davivienda", "td Davivienda"
  -> "Tarjeta de débito Davivienda"
- "tarjeta Bancolombia" sin decir crédito/débito -> "Tarjeta Bancolombia"
- "con la tarjeta" -> "Tarjeta"
- "Bancolombia" solo -> "Bancolombia"
- "por Nequi", "con Nequi" -> "Nequi"
- "Daviplata" -> "Daviplata"
- "efectivo", "cash", "plata", "billete" -> "Efectivo"
- El banco NO debe quedarse en la descripción.

CATEGORÍAS EXACTAS:

Gasto:
Comida, Transporte, Mercado, Servicios, Salud, Entretenimiento, Ropa, Hogar, Educación, Otros

Ingreso:
Salario, Ventas, Regalo, Otros ingresos

PISTAS DE CATEGORÍA:
- Comida: pan, café, tinto, arepa, empanada, desayuno, almuerzo, cena,
  onces, corrientazo, comida, domicilio, pizza, pollo, restaurante.
- Mercado: mercado, tienda, supermercado, D1, Ara, Olímpica, Carulla, Éxito.
- Transporte: Uber, DiDi, taxi, bus, TransMilenio, SITP, metro, gasolina,
  tanqueada, peaje, parqueadero, vuelo.
- Servicios: arriendo, luz, agua, internet, wifi, celular, gas, administración,
  recibo, factura.
- Entretenimiento: cine, concierto, rumba, bar, baile, juego, Spotify, Steam, fiesta.
- Ropa: ropa, zapatos, tenis, camisa, pantalón, chaqueta, vestido, gorra.
- Hogar: mueble, aseo, lámpara, casa, reparación.
- Educación: curso, libro, matrícula, universidad, colegio, clase.
- Salud: médico, medicina, farmacia, dentista, consulta, examen.
- Ingreso "salario/nómina/sueldo/quincena" -> Salario.
- "vendí/venta/pedido de cliente" -> Ventas.
- "regalo/premio/bono" -> Regalo.

TIPO:
Es Ingreso si dice o claramente implica: "me pagaron", "me consignaron",
"me transfirieron", "me entraron", "me llegó", "recibí", "salario",
"nómina", "sueldo", "vendí", "cobré", "me pagaron una plata".
En otro caso es Gasto.

FECHA:
- Sin fecha, "hoy", "ahora", "esta mañana", "anoche" -> dias_atras/dia_semana/fecha = null.
- "ayer" -> dias_atras=1.
- "antier" o "anteayer" -> dias_atras=2.
- "hace 3 días" -> dias_atras=3.
- "el lunes" -> dia_semana=0; martes=1 ... domingo=6.
  Usa el último día de la semana que ya pasó.
- "el 15 de septiembre" -> fecha "YYYY-MM-DD" usando el año actual salvo que diga otro.

VARIOS MOVIMIENTOS:
Si el mensaje contiene dos o más movimientos claramente separados, usa:
{"intent":"registro","registros":[{movimiento1},{movimiento2}]}
No mezcles los montos o medios entre movimientos.

## 2) CONSULTA

Formato:
{"intent":"consulta","periodo":"hoy"|"ayer"|"semana"|"semana_pasada"|"mes"|"mes_pasado"|
"ultimos_7"|"ultimos_30"|"anio"|"mes_especifico"|"todo","mes":<1-12|null>,
"anio":<entero|null>,"tipo":"Gasto"|"Ingreso"|"Todos","filtro":<texto|null>}

Entiende preguntas naturales como:
- "¿cuánto me he gastado este mes?"
- "¿cuánta plata he gastado?"
- "¿en qué se me fue la plata?"
- "¿qué fue lo que más gasté esta semana?"
- "¿cómo voy de plata?"
- "¿cómo voy de balance?"
- "¿cuánto me entró?"
- "muéstrame lo de ayer"
- "¿cuánto llevo gastando en comida?"

Reglas:
- Sin período -> "mes".
- Tipo "Gasto" por defecto.
- Usa "Todos" si pregunta por balance, ingresos y gastos juntos, "cómo voy",
  "qué me queda", "cuánta plata tengo disponible", etc.
- filtro solo cuando pregunta por algo concreto: categoría, lugar, concepto
  o medio de pago, en una o dos palabras.
- "comida", "transporte", "nequi", "bancolombia", "uber", "arriendo" pueden ser filtros.
- Para "¿en qué gasté más?" no pongas un filtro; deja filtro=null.

## 3) BORRAR

Formato:
{"intent":"borrar","filtro":<texto|null>,"monto":<entero|null>,"periodo":"hoy"|"ayer"|"semana"|"mes"|"todo"}

Entiende:
"borra el cine de ayer", "elimina el gasto de 20 lucas", "quita el uber de hoy",
"borra ese movimiento".

## 4) OTRA COSA

Formato:
{"intent":"otro","respuesta":"<1 o 2 frases breves>"}

Para charla, usa español natural de Colombia, sin caricaturizar ni abusar de la jerga.
Puedes decir "de una", "listo", "claro", "te ayudo", "qué más" de vez en cuando.
Ejemplo:
"hola" -> {"intent":"otro","respuesta":"¡Qué más! De una, dime qué gastaste o qué quieres consultar."}
"""

def _json_from_llm(raw: str) -> dict:
    """Hace el parseo un poco más resistente sin relajar la salida esperada."""
    raw = (raw or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.I)
        raw = re.sub(r"\s*```$", "", raw)
    return json.loads(raw)


def parse_with_llm(text: str, now: datetime) -> dict:
    hoy = f"{DIAS[now.weekday()]} {now.day} de {MESES[now.month - 1]} de {now.year}"

    # Le damos a la IA una pista determinista, pero dejamos que ella resuelva
    # intención, fecha y contexto.
    monto_hint = preparse_monto(text)
    medio_hint = norm_medio(text)
    hints = (
        f"Pista técnica (no inventar): monto_detectado={monto_hint!r}; "
        f"medio_detectado={medio_hint!r}"
    )

    raw = groq_chat(
        [
            {"role": "system", "content": SYSTEM_PROMPT.replace("{HOY}", hoy)},
            {"role": "user", "content": f"{text}\n\n{hints}"},
        ],
        json_mode=True,
        temperature=0.0,
    )
    data = _json_from_llm(raw)
    return post_valida(data, text)


def llm_intro(pregunta: str, resumen_html: str) -> str:
    """Una o dos frases que responden la pregunta con cifras ya calculadas por Python."""
    plano = re.sub(r"<[^>]+>", "", html.unescape(resumen_html))
    try:
        out = groq_chat(
            [
                {
                    "role": "system",
                    "content": (
                        "Eres un asistente personal de finanzas en Colombia. Hablas natural, cercano y "
                        "claro, como una conversación por Telegram. Puedes usar de vez en cuando expresiones "
                        "como 'de una', 'listo', 'vas bien' o 'se te fue', pero sin forzar la jerga ni decir "
                        "'parce/parcero' en cada respuesta. Responde en 1 o 2 frases usando EXCLUSIVAMENTE "
                        "las cifras y hechos del resumen. No inventes ni recalcules números: copia los montos "
                        "tal cual. Sin listas ni títulos. Máximo un emoji."
                    ),
                },
                {"role": "user", "content": f"Pregunta: {pregunta}\n\nResumen:\n{plano}"},
            ],
            temperature=0.3,
        )
        return out.strip()
    except Exception as e:
        log.warning("llm_intro falló: %s", type(e).__name__)
        return ""


# ======================================================================
# Mensajes bonitos
# ======================================================================
def describe(r: dict) -> str:
    icon = "💰" if r["tipo"] == "Ingreso" else "💸"
    hora = r["hora"]
    return (
        f"{icon} <b>{fmt_cop(r['monto'])}</b> · {esc(r['desc'])}\n"
        f"{emoji_cat(r['cat'])} {esc(r['cat'])} · 💳 {esc(r['medio'])}\n"
        f"📅 {fmt_fecha(r['fecha'], hora)}"
    )


def describe_short(r: dict) -> str:
    icon = "💰" if r["tipo"] == "Ingreso" else "💸"
    return f"{icon} {fmt_cop(r['monto'])} · {esc(r['desc'])} · {esc(r['medio'])} · {fmt_fecha(r['fecha'])}"


def group_sum(rows, key):
    acc = {}
    for r in rows:
        acc[r[key]] = acc.get(r[key], 0) + r["monto"]
    return sorted(acc.items(), key=lambda kv: -kv[1])


def build_summary(sel, tipo, label, filtro=None) -> str:
    if not sel:
        extra = f" con «{esc(filtro)}»" if filtro else ""
        return f"🤷 No encontré movimientos{extra} · {esc(label)}."
    gastos = [r for r in sel if r["tipo"] == "Gasto"]
    ingresos = [r for r in sel if r["tipo"] == "Ingreso"]
    tg_ = sum(r["monto"] for r in gastos)
    ti_ = sum(r["monto"] for r in ingresos)

    L = [f"📊 <b>Resumen · {esc(label)}</b>"]
    if filtro:
        L.append(f"🔎 <i>{esc(filtro)}</i>")
    L.append("")
    if tipo in ("Gasto", "Todos"):
        L.append(f"💸 Gastos: <b>{fmt_cop(tg_)}</b> · {len(gastos)} mov.")
    if tipo in ("Ingreso", "Todos"):
        L.append(f"💰 Ingresos: <b>{fmt_cop(ti_)}</b> · {len(ingresos)} mov.")
    if tipo == "Todos":
        L.append(f"⚖️ Balance: <b>{fmt_signed(ti_ - tg_)}</b>")

    base = ingresos if tipo == "Ingreso" else gastos
    total = sum(r["monto"] for r in base)
    if base and total > 0:
        L += ["", "🏷 <b>Por categoría</b>"]
        for k, v in group_sum(base, "cat")[:6]:
            L.append(f"{emoji_cat(k)} {esc(k)} — {fmt_cop(v)} ({round(v * 100 / total)}%)")
        L += ["", "💳 <b>Por medio de pago</b>"]
        for k, v in group_sum(base, "medio")[:4]:
            L.append(f"• {esc(k)} — {fmt_cop(v)}")
        L += ["", "🔝 <b>Más grandes</b>"]
        for r in sorted(base, key=lambda x: -x["monto"])[:3]:
            L.append(f"• {fmt_cop(r['monto'])} {esc(r['desc'])} ({fmt_fecha(r['fecha'])})")
    return "\n".join(L)


def welcome_text(nombre: str) -> str:
    return (
        f"👋 <b>¡Qué más, {esc(nombre)}!</b>\n"
        "Soy tu asistente de finanzas 💸\n\n"
        "✍️ <b>Registrar</b>\n"
        "<code>10 lucas de pan con Bancolombia</code>\n\n"
        "🔎 <b>Preguntar</b>\n"
        "<code>¿cuánto me he gastado este mes?</code>\n\n"
        "🗑 <b>Borrar</b>\n"
        "<code>borra el cine de ayer</code>\n\n"
        "⚡ <b>Comandos</b>\n"
        "<code>/hoy</code> · <code>/resumen</code> · <code>/deshacer</code> · <code>/ayuda</code>"
    )


# ======================================================================
# Anti-duplicados en memoria (además del ID guardado en la hoja)
# ======================================================================
_seen = deque(maxlen=500)
_seen_lock = threading.Lock()


def already_seen(message_id) -> bool:
    with _seen_lock:
        if message_id in _seen:
            return True
        _seen.append(message_id)
        return False


# ======================================================================
# Acciones
# ======================================================================
def do_registro(chat_id, message_id, data: dict, now: datetime):
    today = now.date()
    ingreso = str(data.get("tipo", "")).lower().startswith("ing")
    tipo = "Ingreso" if ingreso else "Gasto"
    monto = to_int(data.get("monto"))
    if not monto or monto <= 0:
        send(chat_id, "🤔 Me falta el valor. Dímelo como te salga: <code>10 lucas pan con Nequi</code> o <code>25.000 almuerzo</code>.")
        return
    desc = (str(data.get("descripcion") or "sin descripción").strip() or "sin descripción")[:80]
    cat = norm_cat(data.get("categoria"), ingreso)
    medio = norm_medio(str(data.get("medio_pago") or "")) or "No especificado"
    medio = medio[:60]
    fecha = resolve_fecha(data, today)
    hora = now.strftime("%H:%M") if fecha == today else ""

    rows = load_rows()
    if any(r["msgid"] == str(message_id) for r in rows):
        log.info("Mensaje %s ya estaba registrado, se ignora", message_id)
        return

    get_ws().append_row(
        [fecha.isoformat(), hora, tipo, monto, desc, cat, medio, str(message_id)],
        value_input_option="RAW",
    )
    nuevo = {"row": 0, "fecha": fecha, "hora": hora, "tipo": tipo, "monto": monto,
             "desc": desc, "cat": cat, "medio": medio, "msgid": str(message_id)}
    rows.append(nuevo)

    mes_total = sum(
        r["monto"] for r in rows
        if r["tipo"] == tipo and r["fecha"].year == fecha.year and r["fecha"].month == fecha.month
    )
    titulo = "Ingreso registrado" if ingreso else "Gasto registrado"
    etiqueta = "Ingresos" if ingreso else "Gastos"
    send(
        chat_id,
        f"✅ <b>{titulo}</b>\n\n{describe(nuevo)}\n\n"
        f"📊 {etiqueta} de {MESES[fecha.month - 1]}: <b>{fmt_cop(mes_total)}</b>\n"
        f"<i>¿Te equivocaste? /deshacer</i>",
    )


def do_consulta(chat_id, text: str, data: dict, today: date):
    desde, hasta, label = resolve_period(data, today)
    tipo = str(data.get("tipo") or "Gasto").capitalize()
    if tipo not in ("Gasto", "Ingreso", "Todos"):
        tipo = "Gasto"
    filtro = data.get("filtro") or None
    sel = filter_rows(load_rows(), desde, hasta, tipo, filtro)
    resumen = build_summary(sel, tipo, label, filtro)
    intro = llm_intro(text, resumen) if sel else ""
    send(chat_id, (esc(intro) + "\n\n" if intro else "") + resumen)


def do_resumen(chat_id, nombre: str, periodo: str, today: date):
    desde, hasta, label = resolve_period({"periodo": periodo}, today)
    sel = filter_rows(load_rows(), desde, hasta, "Todos", None)
    send(
        chat_id,
        f"👋 Hola, <b>{esc(nombre)}</b>, estas son tus finanzas personales\n\n"
        + build_summary(sel, "Todos", label),
    )


def do_deshacer(chat_id):
    rows = load_rows()
    if not rows:
        send(chat_id, "No hay nada para deshacer 🙂")
        return
    last = max(rows, key=lambda r: r["row"])
    get_ws().delete_rows(last["row"])
    send(chat_id, f"↩️ <b>Deshecho</b>\n\n{describe(last)}")


def do_borrar(chat_id, data: dict, today: date):
    rows = [r for r in load_rows() if r["msgid"]]  # solo los que el bot puede identificar
    periodo = str(data.get("periodo") or "todo").lower()
    if periodo in ("hoy", "ayer", "semana", "mes"):
        desde, hasta, _ = resolve_period({"periodo": periodo}, today)
    else:
        desde, hasta = date(2000, 1, 1), today
    rows = filter_rows(rows, desde, hasta, "Todos", data.get("filtro") or None)
    monto = to_int(data.get("monto")) if data.get("monto") else None
    if monto:
        rows = [r for r in rows if r["monto"] == monto]
    cands = sorted(rows, key=lambda r: (r["fecha"], r["row"]), reverse=True)[:5]

    if not cands:
        send(chat_id, "🤷 No encontré ningún movimiento que coincida. Prueba con /deshacer para borrar el último.")
        return
    if len(cands) == 1:
        r = cands[0]
        send(
            chat_id,
            f"🗑 ¿Borro este movimiento?\n\n{describe(r)}",
            [[
                {"text": "✅ Sí, borrar", "callback_data": f"del:{r['msgid']}"},
                {"text": "❌ Cancelar", "callback_data": "cancel"},
            ]],
        )
        return
    lista = "\n".join(f"{i}. {describe_short(r)}" for i, r in enumerate(cands, 1))
    botones = [{"text": f"🗑 {i}", "callback_data": f"del:{r['msgid']}"} for i, r in enumerate(cands, 1)]
    send(
        chat_id,
        f"🗑 Encontré varios. ¿Cuál borro?\n\n{lista}",
        [botones, [{"text": "❌ Cancelar", "callback_data": "cancel"}]],
    )


# ======================================================================
# Manejo de updates
# ======================================================================
def authorized(user_id) -> bool:
    return not ALLOWED_USER_ID or str(user_id) == str(ALLOWED_USER_ID)


def handle_message(msg: dict):
    chat_id = msg["chat"]["id"]
    user_id = msg["from"]["id"]
    message_id = msg["message_id"]
    text = (msg.get("text") or "").strip()

    if not authorized(user_id):
        send(chat_id, f"No autorizado. Tu ID es {user_id}")
        return
    if already_seen(message_id):
        return

    now = datetime.now(TZ)
    today = now.date()
    try:
        if text.startswith("/"):
            cmd = text.split()[0].split("@")[0].lower()
            if cmd in ("/start", "/ayuda", "/help"):
                send(chat_id, welcome_text(NOMBRE))
            elif cmd == "/resumen":
                do_resumen(chat_id, NOMBRE, "mes", today)
            elif cmd == "/hoy":
                do_resumen(chat_id, NOMBRE, "hoy", today)
            elif cmd == "/deshacer":
                do_deshacer(chat_id)
            else:
                send(chat_id, welcome_text(NOMBRE))
            return

        tg("sendChatAction", chat_id=chat_id, action="typing")
        data = parse_with_llm(text, now)
        intent = data.get("intent")
        if intent == "registro":
            registros = data.get("registros")
            if isinstance(registros, list) and registros:
                for i, registro in enumerate(registros):
                    if isinstance(registro, dict):
                        registro = post_valida(
                            {**registro, "intent": "registro"},
                            text,
                        )
                        do_registro(chat_id, f"{message_id}-{i}", registro, now)
            else:
                do_registro(chat_id, message_id, data, now)
        elif intent == "consulta":
            do_consulta(chat_id, text, data, today)
        elif intent == "borrar":
            do_borrar(chat_id, data, today)
        else:
            send(chat_id, esc(str(data.get("respuesta") or "No te entendí, intenta de nuevo 🙂")))
    except Exception:
        log.exception("Error procesando mensaje")
        send(chat_id, "⚠️ Tuve un problema procesando eso. Intenta de nuevo en un momento.")


def handle_callback(cb: dict):
    if not authorized(cb["from"]["id"]):
        return
    tg("answerCallbackQuery", callback_query_id=cb["id"])
    chat_id = cb["message"]["chat"]["id"]
    mid = cb["message"]["message_id"]
    data = cb.get("data", "")
    try:
        if data == "cancel":
            edit(chat_id, mid, "Cancelado 👍")
        elif data.startswith("del:"):
            msgid = data[4:]
            target = next((r for r in load_rows() if r["msgid"] == msgid), None)
            if not target:
                edit(chat_id, mid, "Ese movimiento ya no existe 🙂")
                return
            get_ws().delete_rows(target["row"])
            edit(chat_id, mid, f"🗑 <b>Borrado</b>\n\n{describe(target)}")
    except Exception:
        log.exception("Error en callback")
        edit(chat_id, mid, "⚠️ No pude borrarlo. Intenta de nuevo.")


# ======================================================================
# FastAPI
# ======================================================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    get_ws()  # falla rápido si la hoja o las credenciales están mal
    if BASE_URL:
        async with httpx.AsyncClient() as c:
            r = await c.post(
                f"{TG_API}/setWebhook",
                json={
                    "url": f"{BASE_URL}/webhook",
                    "secret_token": WEBHOOK_SECRET,
                    "allowed_updates": ["message", "callback_query"],
                },
            )
            log.info("setWebhook: %s", r.text)
    yield


app = FastAPI(lifespan=lifespan)


@app.get("/")
def health():
    return {"status": "ok"}


@app.post("/webhook")
async def webhook(request: Request, background: BackgroundTasks):
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != WEBHOOK_SECRET:
        raise HTTPException(status_code=403)
    update = await request.json()
    if update.get("callback_query"):
        background.add_task(handle_callback, update["callback_query"])
    elif update.get("message") and update["message"].get("text"):
        background.add_task(handle_message, update["message"])
    return {"ok": True}
