from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from aiogram import Bot, Dispatcher, types
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.filters import Command
from aiogram.types import FSInputFile, ContentType, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timedelta
from supabase import create_client, Client
import asyncio
import aiohttp
import os
import fitz
from io import BytesIO
import qrcode

# ------------ CONFIG ------------
BOT_TOKEN         = os.getenv("BOT_TOKEN", "")
SUPABASE_URL      = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY      = os.getenv("SUPABASE_KEY", "")
BASE_URL          = os.getenv("BASE_URL", "").rstrip("/")
URL_CONSULTA_BASE = "https://tlapadecomonfortexpediciondepermisosgob2-k6u7.onrender.com"
OUTPUT_DIR        = "documentos"
PLANTILLA_PDF     = "Guerrero.pdf"
PLANTILLA_FLASK   = "recibo_permiso_guerrero_img.pdf"

COSTO_FIJO     = "250"
RFC_FIJO       = "XAXX010101000"
DOMICILIO_FIJO = "MEXICO"

# ── Mismo bucket que usa el Flask de renovación — fuente única de PDFs ──
BUCKET_NAME = "permisos-guerrero"

os.makedirs(OUTPUT_DIR,    exist_ok=True)
os.makedirs("static/pdfs", exist_ok=True)

# ------------ SUPABASE ------------
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ------------ BOT con timeout 300s — evita HTTP timeout error ------------
_bot_session = AiohttpSession(timeout=aiohttp.ClientTimeout(total=300))
bot     = Bot(token=BOT_TOKEN, session=_bot_session)
storage = MemoryStorage()
dp      = Dispatcher(storage=storage)

# ------------ TIMERS ------------
timers_activos       = {}
user_folios          = {}
pending_comprobantes = {}
TOTAL_MINUTOS_TIMER  = 36 * 60

async def eliminar_folio_automatico(folio: str):
    try:
        uid = timers_activos[folio]["user_id"] if folio in timers_activos else None
        await asyncio.to_thread(lambda:
            supabase.table("folios_registrados").delete().eq("folio", folio).execute()
        )
        if uid:
            await bot.send_message(uid,
                f"⏰ TIEMPO AGOTADO - GUERRERO\n\nEl folio {folio} fue eliminado por no "
                f"completar el pago en 36 horas.\n\n📋 Use /banamex para generar otro permiso.")
        limpiar_timer_folio(folio)
    except Exception as e:
        print(f"Error eliminando folio {folio}: {e}")

async def enviar_recordatorio(folio: str, mins: int):
    try:
        if folio not in timers_activos: return
        uid = timers_activos[folio]["user_id"]
        await bot.send_message(uid,
            f"⚡ RECORDATORIO - GUERRERO\nFolio: {folio}\n"
            f"Tiempo restante: {mins} minutos\nMonto: ${COSTO_FIJO}\n\n"
            f"📸 Envía tu comprobante (foto).\n\n📋 /banamex para nuevo permiso")
    except Exception as e:
        print(f"Error recordatorio {folio}: {e}")

async def iniciar_timer_eliminacion(user_id: int, folio: str):
    async def _task():
        await asyncio.sleep(34.5 * 3600)
        for mins, sleep in [(90, 1800), (60, 1800), (30, 1200), (10, 600)]:
            if folio not in timers_activos: return
            await enviar_recordatorio(folio, mins)
            await asyncio.sleep(sleep)
        if folio in timers_activos:
            await eliminar_folio_automatico(folio)
    task = asyncio.create_task(_task())
    timers_activos[folio] = {"task": task, "user_id": user_id, "start_time": datetime.now()}
    user_folios.setdefault(user_id, []).append(folio)
    print(f"[TIMER] Iniciado folio {folio}")

def cancelar_timer_folio(folio: str):
    if folio not in timers_activos: return
    timers_activos[folio]["task"].cancel()
    uid = timers_activos[folio]["user_id"]
    del timers_activos[folio]
    if uid in user_folios and folio in user_folios[uid]:
        user_folios[uid].remove(folio)
        if not user_folios[uid]: del user_folios[uid]

def limpiar_timer_folio(folio: str):
    if folio not in timers_activos: return
    uid = timers_activos[folio]["user_id"]
    del timers_activos[folio]
    if uid in user_folios and folio in user_folios[uid]:
        user_folios[uid].remove(folio)
        if not user_folios[uid]: del user_folios[uid]

def obtener_folios_usuario(uid: int) -> list:
    return user_folios.get(uid, [])

# ============ FOLIOS GUERRERO — WATERMARK =====================================
FOLIO_PREFIJO_GUE = "GUE"
FOLIO_LETRAS      = "ZY"
FOLIO_NUM_INICIO  = 4917
_folio_counter    = {"siguiente": FOLIO_NUM_INICIO}
_folio_lock       = asyncio.Lock()

def _sb_leer_watermark_gue() -> int | None:
    try:
        r = supabase.table("folio_watermark") \
            .select("ultimo_asignado").eq("prefijo", FOLIO_PREFIJO_GUE).execute()
        if r.data:
            return r.data[0]["ultimo_asignado"]
        return None
    except Exception as e:
        print(f"[ERROR] leer_watermark GUE: {e}")
        return None

def _sb_guardar_watermark_gue(numero: int):
    try:
        supabase.table("folio_watermark").upsert({
            "prefijo":         FOLIO_PREFIJO_GUE,
            "ultimo_asignado": numero
        }).execute()
        print(f"[WATERMARK GUE] Guardado: {FOLIO_LETRAS}{str(numero).zfill(4)}")
    except Exception as e:
        print(f"[ERROR] guardar_watermark GUE: {e}")

def _sb_inicializar_folio_gue():
    watermark = _sb_leer_watermark_gue()
    if watermark is not None:
        _folio_counter["siguiente"] = watermark + 1
        print(f"[FOLIO GUE] Desde watermark: {FOLIO_LETRAS}{str(watermark).zfill(4)} "
              f"-> siguiente: {_folio_counter['siguiente']}")
        return
    try:
        resp = supabase.table("folios_registrados") \
            .select("folio").eq("entidad", "Guerrero").execute()
        numeros = []
        for row in resp.data or []:
            f = row.get("folio", "")
            if isinstance(f, str) and f.startswith(FOLIO_LETRAS) and len(f) == 6:
                sufijo = f[len(FOLIO_LETRAS):]
                if sufijo.isdigit():
                    numeros.append(int(sufijo))
        if numeros:
            maximo = max(numeros)
            _folio_counter["siguiente"] = maximo + 1
            _sb_guardar_watermark_gue(maximo)
            print(f"[FOLIO GUE] Desde DB (primera vez): {FOLIO_LETRAS}{str(maximo).zfill(4)} "
                  f"-> siguiente: {_folio_counter['siguiente']}")
        else:
            _folio_counter["siguiente"] = FOLIO_NUM_INICIO
            print(f"[FOLIO GUE] Sin folios previos, empezando desde "
                  f"{FOLIO_LETRAS}{str(FOLIO_NUM_INICIO).zfill(4)}")
    except Exception as e:
        print(f"[ERROR] inicializar_folio GUE: {e}")
        _folio_counter["siguiente"] = FOLIO_NUM_INICIO

def _sb_folio_existe(folio: str) -> bool:
    try:
        r = supabase.table("folios_registrados").select("folio").eq("folio", folio).execute()
        return len(r.data) > 0
    except Exception as e:
        print(f"[ERROR] verificar folio {folio}: {e}")
        return False

def _generar_folio_guerrero_sync() -> str:
    candidato = _folio_counter["siguiente"]
    for _ in range(100_000):
        if candidato > 9999:
            print("[FOLIO GUE] Límite 9999 alcanzado")
            break
        folio = f"{FOLIO_LETRAS}{str(candidato).zfill(4)}"
        if not _sb_folio_existe(folio):
            _folio_counter["siguiente"] = candidato + 1
            _sb_guardar_watermark_gue(candidato)
            print(f"[FOLIO GUE] Asignado: {folio} (siguiente: {_folio_counter['siguiente']})")
            return folio
        print(f"[FOLIO GUE] {folio} ocupado -> probando siguiente")
        candidato += 1
    return f"{FOLIO_LETRAS}9999"

async def _generar_folio_guerrero() -> str:
    async with _folio_lock:
        return await asyncio.to_thread(_generar_folio_guerrero_sync)

# ------------ COORDENADAS PDF ------------
coords_guerrero = {
    "folio":         (360, 769,  8, (1,0,0)),
    "fecha_exp":     (135, 755,  8, (0,0,0)),
    "fecha_ven":     (135, 768,  8, (0,0,0)),
    "serie":         (360, 742,  8, (0,0,0)),
    "motor":         (360, 729,  8, (0,0,0)),
    "marca":         (360, 700,  8, (0,0,0)),
    "linea":         (360, 714,  8, (0,0,0)),
    "color":         (360, 756,  8, (0,0,0)),
    "nombre":        (135, 700,  8, (0,0,0)),
    "rfc":           (135, 713,  8, (0,0,0)),
    "domicilio":     (135, 726,  8, (0,0,0)),
    "costo":         (135, 742,  8, (0,0,0)),
    "rot_folio":     (440, 200, 83, (0,0,0)),
    "rot_fecha_exp": ( 77, 205,  8, (0,0,0)),
    "rot_fecha_ven": ( 63, 205,  8, (0,0,0)),
    "rot_serie":     (168, 110, 18, (0,0,0)),
    "rot_motor":     (224, 110, 18, (0,0,0)),
    "rot_marca":     (280, 110, 18, (0,0,0)),
    "rot_linea":     (280, 290, 18, (0,0,0)),
    "rot_anio":      (305, 520, 18, (0,0,0)),
    "rot_color":     (224, 420, 18, (0,0,0)),
    "rot_nombre":    (115, 205,  8, (0,0,0)),
    "rot_rfc":       (102, 205,  8, (0,0,0)),
    "rot_domicilio": ( 89, 205,  8, (0,0,0)),
}

coords_recibo = {
    "folio":     ( 85, 210, 12, (0,0,0)),
    "nombre":    (117, 231, 12, (0,0,0)),
    "domicilio": (117, 255, 12, (0,0,0)),
    "costo":     (432, 352, 12, (0,0,0)),
    "fecha_exp": (265, 210, 12, (0,0,0)),
    "fecha_ven": (480, 210, 12, (0,0,0)),
    "qr":        ( 55, 307, 110, None),
}

# ------------ FSM ------------
class PermisoForm(StatesGroup):
    marca  = State()
    linea  = State()
    anio   = State()
    serie  = State()
    motor  = State()
    color  = State()
    nombre = State()

# ------------ QR ------------
def _make_qr_pixmap(folio: str):
    url = f"{URL_CONSULTA_BASE}/consulta/{folio}"
    qr  = qrcode.QRCode(version=2, error_correction=qrcode.constants.ERROR_CORRECT_M,
                         box_size=4, border=1)
    qr.add_data(url); qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
    buf = BytesIO(); img.save(buf, format="PNG"); buf.seek(0)
    return fitz.Pixmap(buf.read())

# ------------ PDF PERMISO ------------
def _generar_pdf_permiso(datos: dict) -> str:
    fol  = datos["folio"]
    path = f"{OUTPUT_DIR}/{fol}_permiso_tmp.pdf"
    try:
        doc = fitz.open(PLANTILLA_PDF); pg = doc[0]
        for c in ["folio","fecha_exp","fecha_ven","serie","motor","marca","linea","color","nombre"]:
            if c in coords_guerrero and c in datos:
                x,y,s,col = coords_guerrero[c]
                pg.insert_text((x,y), str(datos[c]), fontsize=s, color=col)
        for c in ["rfc","domicilio"]:
            x,y,s,col = coords_guerrero[c]
            pg.insert_text((x,y), datos[c], fontsize=s, color=col)
        x,y,s,col = coords_guerrero["costo"]
        pg.insert_text((x,y), f"${datos['costo']}", fontsize=s, color=col)
        for k, val in [
            ("rot_folio",    fol),
            ("rot_fecha_exp",datos["fecha_exp"]),
            ("rot_fecha_ven",datos["fecha_ven"]),
            ("rot_serie",    datos["serie"]),
            ("rot_motor",    datos["motor"]),
            ("rot_marca",    datos["marca"]),
            ("rot_linea",    datos["linea"]),
            ("rot_anio",     datos["anio"]),
            ("rot_color",    datos["color"]),
            ("rot_nombre",   datos["nombre"]),
            ("rot_rfc",      datos["rfc"]),
            ("rot_domicilio",datos["domicilio"]),
        ]:
            pg.insert_text(coords_guerrero[k][:2], val,
                           fontsize=coords_guerrero[k][2], rotate=270)
        qr_pix = _make_qr_pixmap(fol)
        pg.insert_image(fitz.Rect(80, 460, 80+97, 460+97), pixmap=qr_pix, overlay=True)
        doc.save(path); doc.close()
        print(f"[PERMISO] OK: {path}")
        return path
    except Exception as e:
        print(f"[ERROR PERMISO] {e}"); return ""

# ------------ PDF RECIBO ------------
def _generar_pdf_recibo(datos: dict) -> str:
    fol  = datos["folio"]
    path = f"{OUTPUT_DIR}/{fol}_recibo_tmp.pdf"
    try:
        doc  = fitz.open(PLANTILLA_FLASK)
        page = doc[0]
        page.wrap_contents()
        for campo in ["folio","nombre","rfc","domicilio","costo","fecha_exp","fecha_ven"]:
            if campo in coords_recibo:
                x, y, size, color = coords_recibo[campo]
                valor = datos.get(campo, "")
                if campo == "costo":
                    valor = f"${valor}"
                page.insert_text((x,y), str(valor), fontsize=size,
                                 color=color, fontname="helv")
        x, y, size, _ = coords_recibo["qr"]
        qr_pix = _make_qr_pixmap(fol)
        page.insert_image(fitz.Rect(x, y, x+size, y+size), pixmap=qr_pix)
        doc.save(path); doc.close()
        print(f"[RECIBO OK] {path}")
        return path
    except Exception as e:
        print(f"[ERROR RECIBO] {e}"); return ""

# ------------ PDF UNIFICADO ------------
def _generar_pdf_unificado(datos: dict) -> str:
    fol   = datos["folio"]
    final = f"{OUTPUT_DIR}/{fol}_guerrero_completo.pdf"
    try:
        p1 = _generar_pdf_permiso(datos)
        p2 = _generar_pdf_recibo(datos)
        if not p1 or not p2:
            print("[ERROR] Falló un PDF individual"); return p1 or p2 or ""
        d1 = fitz.open(p1); d2 = fitz.open(p2)
        d1.insert_pdf(d2); d1.save(final)
        d1.close(); d2.close()
        for tmp in [p1, p2]:
            try: os.remove(tmp)
            except: pass
        print(f"[UNIFICADO] OK: {final}")
        return final
    except Exception as e:
        print(f"[ERROR UNIFICADO] {e}"); return ""

# ═══════════════════════════════════════════════════════════════════════════
# SUPABASE STORAGE — mismo bucket que usa el Flask de renovación
# ═══════════════════════════════════════════════════════════════════════════

def _subir_pdf_a_storage_sync(ruta_local: str, folio: str) -> str:
    """
    Síncrono — se llama con asyncio.to_thread. Sube el PDF al bucket
    'permisos-guerrero' y devuelve la URL pública, o "" si falla.
    """
    try:
        with open(ruta_local, "rb") as f:
            contenido = f.read()

        nombre_archivo = f"{folio}.pdf"

        supabase.storage.from_(BUCKET_NAME).upload(
            path=nombre_archivo,
            file=contenido,
            file_options={"content-type": "application/pdf", "upsert": "true"}
        )

        url = supabase.storage.from_(BUCKET_NAME).get_public_url(nombre_archivo)
        print(f"[STORAGE] Subido: {url}")
        return url

    except Exception as e:
        print(f"[ERROR STORAGE] No se pudo subir {folio}: {e}")
        return ""


async def _subir_pdf_y_actualizar(ruta_pdf: str, folio: str):
    """
    Corre como tarea independiente (asyncio.create_task) — no bloquea
    el envío del documento al usuario. Sube a Storage y guarda pdf_url.
    """
    url = await asyncio.to_thread(_subir_pdf_a_storage_sync, ruta_pdf, folio)
    if url:
        try:
            await asyncio.to_thread(lambda:
                supabase.table("folios_registrados")
                    .update({"pdf_url": url}).eq("folio", folio).execute()
            )
            print(f"[STORAGE] pdf_url guardado para {folio}")
        except Exception as e:
            print(f"[WARN] No se pudo guardar pdf_url en BD para {folio}: {e}")

# ------------ Supabase insert ------------
def _sb_insertar(datos: dict, user_id: int, username: str):
    hoy = datos["fecha_exp_obj"]; ven = datos["fecha_ven_obj"]
    supabase.table("folios_registrados").insert({
        "folio":             datos["folio"],
        "marca":             datos["marca"],
        "linea":             datos["linea"],
        "anio":              datos["anio"],
        "numero_serie":      datos["serie"],
        "numero_motor":      datos["motor"],
        "color":             datos["color"],
        "nombre":            datos["nombre"],
        "rfc":               datos["rfc"],
        "domicilio":         datos["domicilio"],
        "costo":             datos["costo"],
        "fecha_expedicion":  hoy.date().isoformat(),
        "fecha_vencimiento": ven.date().isoformat(),
        "entidad":           "Guerrero",
        "estado":            "PENDIENTE",
        "user_id":           user_id,
        "username":          username or "Sin username",
    }).execute()

# ------------ BACKGROUND TASK ------------------------------------------------
async def _generar_y_enviar_background(chat_id: int, datos: dict, user_id: int, username: str):
    folio = datos["folio"]
    try:
        pdf = await asyncio.to_thread(_generar_pdf_unificado, datos)

        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🔑 Validar Admin",  callback_data=f"validar_{folio}"),
            InlineKeyboardButton(text="⏹️ Detener Timer", callback_data=f"detener_{folio}")
        ]])

        if pdf:
            await bot.send_document(
                chat_id, FSInputFile(pdf),
                caption=(
                    f"📋 PERMISO DE CIRCULACIÓN — GUERRERO\n"
                    f"Folio: {folio}\n"
                    f"Titular: {datos['nombre']}\n"
                    f"Vigencia: 30 días | Costo: ${COSTO_FIJO}\n\n"
                    f"✅ Permiso + Recibo (PDF unificado)\n\n"
                    f"⏰ TIMER ACTIVO — 36 horas"
                ),
                reply_markup=kb
            )
        else:
            await bot.send_message(user_id,
                "❌ Error al generar PDF. Intenta de nuevo con /banamex")
            return

        await asyncio.to_thread(_sb_insertar, datos, user_id, username)

        # ── Sube a Storage en paralelo — no bloquea, ya se mandó el documento ──
        asyncio.create_task(_subir_pdf_y_actualizar(pdf, folio))

        await iniciar_timer_eliminacion(user_id, folio)

        await bot.send_message(user_id,
            f"💰 INSTRUCCIONES DE PAGO\n\n"
            f"📄 Folio: {folio}\n💵 Monto: ${COSTO_FIJO}\n⏰ Límite: 36 horas\n\n"
            f"🏦 TRANSFERENCIA:\n• Titular: GUILLERMO S.R.J\n"
            f"• Número: 7289690000484424454\n\n"
            f"🏪 OXXO:\n• Referencia: 2242170180214090\n• Monto: ${COSTO_FIJO}\n\n"
            f"📸 Envía foto del comprobante para validar.\n\n📋 /banamex para nuevo permiso")

    except Exception as e:
        print(f"[ERROR background] folio {folio}: {e}")
        try:
            await bot.send_message(user_id,
                f"❌ Error al generar el documento: {e}\n\nUse /banamex para reintentar.")
        except Exception:
            pass

# ============================================================
#  HANDLERS
# ============================================================

@dp.message(Command("start"))
async def start_cmd(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "🏛️ SISTEMA DIGITAL DEL ESTADO DE GUERRERO\n\n"
        f"💰 Costo fijo: ${COSTO_FIJO}\n"
        "⏰ Tiempo límite de pago: 36 horas\n\n"
        "📋 Use /banamex para generar un permiso."
    )

@dp.message(Command("banamex"))
async def banamex_cmd(message: types.Message, state: FSMContext):
    await state.clear()
    folios_activos = obtener_folios_usuario(message.from_user.id)

    if folios_activos:
        lineas = []
        for f in folios_activos:
            if f in timers_activos:
                seg  = max(0, int(TOTAL_MINUTOS_TIMER * 60 -
                                  (datetime.now() - timers_activos[f]["start_time"]).total_seconds()))
                h, m = divmod(seg // 60, 60)
                lineas.append(f"• {f}  ({h}h {m}min restantes)")
            else:
                lineas.append(f"• {f}  (sin timer)")
        botones = [
            [InlineKeyboardButton(text=f"⏹️ Detener {f}", callback_data=f"detener_{f}")]
            for f in folios_activos
        ]
        await message.answer(
            f"📋 FOLIOS GUERRERO ACTIVOS ({len(folios_activos)}):\n\n" +
            "\n".join(lineas) + "\n\nPuedes detener el timer de cualquier folio:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=botones)
        )

    await message.answer(
        f"🚗 NUEVO PERMISO - GUERRERO\n\n"
        f"💰 Costo: ${COSTO_FIJO} (fijo)\n"
        f"⏰ Plazo de pago: 36 horas\n\n"
        f"Paso 1: MARCA del vehículo:"
    )
    await state.set_state(PermisoForm.marca)

@dp.message(PermisoForm.marca)
async def get_marca(message: types.Message, state: FSMContext):
    await state.update_data(marca=message.text.strip().upper())
    await message.answer("LÍNEA/MODELO:")
    await state.set_state(PermisoForm.linea)

@dp.message(PermisoForm.linea)
async def get_linea(message: types.Message, state: FSMContext):
    await state.update_data(linea=message.text.strip().upper())
    await message.answer("AÑO (4 dígitos):")
    await state.set_state(PermisoForm.anio)

@dp.message(PermisoForm.anio)
async def get_anio(message: types.Message, state: FSMContext):
    a = message.text.strip()
    if not a.isdigit() or len(a) != 4:
        await message.answer("⚠️ Año inválido. Ej: 2021"); return
    await state.update_data(anio=a)
    await message.answer("NÚMERO DE SERIE / NIV:")
    await state.set_state(PermisoForm.serie)

@dp.message(PermisoForm.serie)
async def get_serie(message: types.Message, state: FSMContext):
    await state.update_data(serie=message.text.strip().upper())
    await message.answer("NÚMERO DE MOTOR:")
    await state.set_state(PermisoForm.motor)

@dp.message(PermisoForm.motor)
async def get_motor(message: types.Message, state: FSMContext):
    await state.update_data(motor=message.text.strip().upper())
    await message.answer("COLOR:")
    await state.set_state(PermisoForm.color)

@dp.message(PermisoForm.color)
async def get_color(message: types.Message, state: FSMContext):
    await state.update_data(color=message.text.strip().upper())
    await message.answer("NOMBRE COMPLETO del propietario:")
    await state.set_state(PermisoForm.nombre)

@dp.message(PermisoForm.nombre)
async def get_nombre(message: types.Message, state: FSMContext):
    datos  = await state.get_data()
    nombre = message.text.strip().upper()

    folio = await _generar_folio_guerrero()
    hoy   = datetime.now()
    ven   = hoy + timedelta(days=30)

    datos_pdf = {
        "folio":         folio,
        "marca":         datos["marca"],
        "linea":         datos["linea"],
        "anio":          datos["anio"],
        "serie":         datos["serie"],
        "motor":         datos["motor"],
        "color":         datos["color"],
        "nombre":        nombre,
        "costo":         COSTO_FIJO,
        "rfc":           RFC_FIJO,
        "domicilio":     DOMICILIO_FIJO,
        "fecha_exp":     hoy.strftime("%d/%m/%Y"),
        "fecha_ven":     ven.strftime("%d/%m/%Y"),
        "fecha_exp_obj": hoy,
        "fecha_ven_obj": ven,
    }

    # state.clear() ANTES del create_task — evita re-triggers
    await state.clear()

    await message.answer(
        f"🔄 Generando...\n<b>Folio:</b> {folio}\n<b>Titular:</b> {nombre}",
        parse_mode="HTML"
    )

    asyncio.create_task(
        _generar_y_enviar_background(
            message.chat.id, datos_pdf,
            message.from_user.id,
            message.from_user.username or "Sin username"
        )
    )

# ------------ CALLBACKS ------------
@dp.callback_query(lambda c: c.data and c.data.startswith("validar_"))
async def cb_validar(callback: CallbackQuery):
    folio = callback.data.replace("validar_", "")
    if not folio.startswith("Z"):
        await callback.answer("❌ Folio inválido", show_alert=True); return
    if folio in timers_activos:
        uid = timers_activos[folio]["user_id"]
        cancelar_timer_folio(folio)
        try:
            await asyncio.to_thread(lambda:
                supabase.table("folios_registrados").update(
                    {"estado": "VALIDADO_ADMIN", "fecha_comprobante": datetime.now().isoformat()}
                ).eq("folio", folio).execute()
            )
        except Exception as e:
            print(f"Error BD validar {folio}: {e}")
        await callback.answer("✅ Validado", show_alert=True)
        await callback.message.edit_reply_markup(reply_markup=None)
        try:
            await bot.send_message(uid,
                f"✅ PAGO VALIDADO — GUERRERO\nFolio: {folio}\n"
                f"Tu permiso está activo.\n\n📋 /banamex para nuevo permiso")
        except Exception: pass
    else:
        await callback.answer("❌ Timer no activo", show_alert=True)

@dp.callback_query(lambda c: c.data and c.data.startswith("detener_"))
async def cb_detener(callback: CallbackQuery):
    folio = callback.data.replace("detener_", "")
    if folio in timers_activos:
        cancelar_timer_folio(folio)
        try:
            await asyncio.to_thread(lambda:
                supabase.table("folios_registrados").update(
                    {"estado": "TIMER_DETENIDO", "fecha_detencion": datetime.now().isoformat()}
                ).eq("folio", folio).execute()
            )
        except Exception as e:
            print(f"Error BD detener {folio}: {e}")
        await callback.answer("⏹️ Timer detenido", show_alert=True)
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.answer(
            f"⏹️ Timer detenido\nFolio: {folio}\n\n📋 /banamex")
    else:
        await callback.answer("❌ Timer ya no activo", show_alert=True)

@dp.message(lambda m: m.text and m.text.upper().startswith("SERO") and len(m.text) > 4)
async def cmd_sero(message: types.Message):
    fa = message.text.upper()[4:].strip()
    if not fa.startswith("Z"):
        await message.answer(f"❌ Folio {fa} no es GUERRERO (debe empezar con Z)"); return
    if fa in timers_activos:
        uid = timers_activos[fa]["user_id"]
        cancelar_timer_folio(fa)
        try:
            await asyncio.to_thread(lambda:
                supabase.table("folios_registrados").update(
                    {"estado": "VALIDADO_ADMIN", "fecha_comprobante": datetime.now().isoformat()}
                ).eq("folio", fa).execute()
            )
        except Exception: pass
        await message.answer(f"✅ VALIDADO\nFolio: {fa}")
        try:
            await bot.send_message(uid,
                f"✅ PAGO VALIDADO — GUERRERO\nFolio: {fa}\n"
                f"Tu permiso está activo.\n\n📋 /banamex para nuevo permiso")
        except Exception: pass
    else:
        await message.answer(f"❌ Folio {fa} no encontrado en timers activos")

@dp.message(lambda m: m.content_type == ContentType.PHOTO)
async def recibir_comprobante(message: types.Message):
    uid = message.from_user.id
    fl  = obtener_folios_usuario(uid)
    if not fl:
        await message.answer("ℹ️ No hay trámites pendientes.\n\n📋 /banamex"); return
    if len(fl) > 1:
        lista = "\n".join(f"• {f}" for f in fl)
        pending_comprobantes[uid] = "waiting_folio"
        await message.answer(
            f"📄 Varios folios activos:\n\n{lista}\n\n"
            f"Responde con el FOLIO al que corresponde este comprobante.\n\n📋 /banamex")
        return
    folio = fl[0]; cancelar_timer_folio(folio)
    try:
        await asyncio.to_thread(lambda:
            supabase.table("folios_registrados").update(
                {"estado": "COMPROBANTE_ENVIADO", "fecha_comprobante": datetime.now().isoformat()}
            ).eq("folio", folio).execute()
        )
    except Exception: pass
    await message.answer(
        f"✅ Comprobante recibido.\n📄 Folio: {folio}\n⏹️ Timer detenido.\n\n📋 /banamex")

@dp.message(lambda m: m.from_user.id in pending_comprobantes
            and pending_comprobantes[m.from_user.id] == "waiting_folio")
async def especificar_folio(message: types.Message):
    uid = message.from_user.id
    fe  = message.text.strip().upper()
    fl  = obtener_folios_usuario(uid)
    if fe not in fl:
        await message.answer("❌ Folio no encontrado en tu lista.\n\n📋 /banamex"); return
    cancelar_timer_folio(fe); del pending_comprobantes[uid]
    try:
        await asyncio.to_thread(lambda:
            supabase.table("folios_registrados").update(
                {"estado": "COMPROBANTE_ENVIADO", "fecha_comprobante": datetime.now().isoformat()}
            ).eq("folio", fe).execute()
        )
    except Exception: pass
    await message.answer(f"✅ Comprobante asociado.\n📄 Folio: {fe}\n\n📋 /banamex")

@dp.message(Command("folios"))
async def ver_folios(message: types.Message):
    fl = obtener_folios_usuario(message.from_user.id)
    if not fl:
        await message.answer("ℹ️ No hay folios activos.\n\n📋 /banamex"); return
    rows    = []
    botones = []
    for f in fl:
        if f in timers_activos:
            seg  = max(0, int(TOTAL_MINUTOS_TIMER * 60 -
                               (datetime.now()-timers_activos[f]["start_time"]).total_seconds()))
            h, m = divmod(seg // 60, 60)
            rows.append(f"• {f} ({h}h {m}min)")
        else:
            rows.append(f"• {f} (sin timer)")
        botones.append([InlineKeyboardButton(
            text=f"⏹️ Detener {f}", callback_data=f"detener_{f}"
        )])
    await message.answer(
        f"📋 FOLIOS GUERRERO ACTIVOS ({len(fl)})\n\n" +
        "\n".join(rows) + "\n\n📋 /banamex",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=botones)
    )

@dp.message(lambda m: m.text and any(p in m.text.lower() for p in
    ["costo","precio","cuanto","cuánto","deposito","depósito","pago","valor","monto"]))
async def info_costo(message: types.Message):
    await message.answer(f"💰 Costo fijo del permiso: ${COSTO_FIJO}\n\n📋 Use /banamex")

@dp.message()
async def fallback(message: types.Message):
    await message.answer("🏛️ Sistema Digital Guerrero.")

# ============================================================
#  FASTAPI
# ============================================================
_keep_task = None

async def keep_alive():
    while True:
        await asyncio.sleep(600)
        print("[HEARTBEAT] Sistema Guerrero activo")

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _keep_task
    try:
        await asyncio.to_thread(_sb_inicializar_folio_gue)
        await bot.delete_webhook(drop_pending_updates=True)
        if BASE_URL:
            wh = f"{BASE_URL}/webhook"
            await bot.set_webhook(wh, allowed_updates=["message","callback_query"])
            print(f"[WEBHOOK] {wh}")
            _keep_task = asyncio.create_task(keep_alive())
        else:
            print("[POLLING] Sin webhook")
        print(f"[SISTEMA] Guerrero v7.3 listo — "
              f"siguiente folio: {FOLIO_LETRAS}{str(_folio_counter['siguiente']).zfill(4)}")
        yield
    except Exception as e:
        print(f"[ERROR] {e}"); yield
    finally:
        if _keep_task:
            _keep_task.cancel()
            with suppress(asyncio.CancelledError): await _keep_task
        await bot.session.close()

app = FastAPI(lifespan=lifespan, title="Sistema Guerrero", version="7.3")

@app.post("/webhook")
async def webhook(request: Request):
    try:
        data = await request.json()
        await dp.feed_webhook_update(bot, types.Update(**data))
        return {"ok": True}
    except Exception as e:
        print(f"[ERROR webhook] {e}"); return {"ok": False, "error": str(e)}

# ============================================================
#  CONSULTA PÚBLICA
# ============================================================
@app.get("/consulta/{folio}", response_class=HTMLResponse)
async def consulta(folio: str):
    CSS  = "https://direcciontransitotlapadecomonfort.gob.mx/css/bootstrap.min.css"
    JQ   = "https://direcciontransitotlapadecomonfort.gob.mx/js/jquery-3.4.1.min.js"
    BSJS = "https://direcciontransitotlapadecomonfort.gob.mx/js/bootstrap.min.js"
    LOGO = "https://direcciontransitotlapadecomonfort.gob.mx/img/transformando_guerrero.jpg"
    FAV  = "https://direcciontransitotlapadecomonfort.gob.mx/img/favicon-96x96.png?v2"

    STYLE = """
<style>
  #ssp{width:300px;margin:0 auto;border-radius:5px}
  #ltm{color:#636b6f;font-family:'Nunito',sans-serif;font-weight:200;font-size:50px;text-align:center}
  body{background:url(https://direcciontransitotlapadecomonfort.gob.mx/img/intersection.png)}
  .etiqueta{margin:0;padding:3px 10px 3px 0;text-align:right;font-weight:bold}
  .valor{margin:0;padding:3px 0;text-align:left}
  #logo-y{width:110px;margin:-15px auto 0;border-radius:5px}
  #logo-x{width:70px;margin:-15px auto 0;border-radius:5px}
</style>"""

    def wrap(body):
        return f"""<!doctype html><html lang="es"><head>
<meta charset="UTF-8"><title>LTM | Licencias de Tránsito Municipal</title>
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">
<link rel="icon" href="{FAV}">
<link href="https://fonts.googleapis.com/css?family=Nunito:200,600" rel="stylesheet">
<link href="{CSS}" rel="stylesheet">{STYLE}</head><body>
<div class="container">
<div class="col-md-8 col-md-offset-2 col-sm-10 col-sm-offset-1" style="padding:0;margin-top:3em">
<div class="row" style="margin-bottom:1em">
  <div class="col-md-12"><img id="ssp" src="{LOGO}" class="img-responsive" alt=""></div>
</div>
{body}
<div class="col-md-12 text-center">
  <div class="col-md-6 col-xs-6" style="text-align:right">
    <img id="logo-y" src="{LOGO}" class="img-responsive">
  </div>
  <div class="col-md-6 col-xs-6" style="text-align:left">
    <img id="logo-x" src="{FAV}" class="img-responsive">
  </div>
</div>
<div class="clearfix"></div>
<div id="ltm" onclick="window.location.assign('https://direcciontransitotlapadecomonfort.gob.mx');"
     style="cursor:pointer">LTM | Tlapa de Comonfort Gro.</div>
</div></div>
<script src="{JQ}"></script><script src="{BSJS}"></script>
</body></html>"""

    try:
        r = await asyncio.to_thread(lambda:
            supabase.table("folios_registrados").select("*").eq("folio", folio).execute()
        )
    except Exception as e:
        return HTMLResponse(wrap(f'<div class="alert alert-danger">Error DB: {e}</div>'), 500)

    if not r.data:
        return HTMLResponse(wrap(f"""
<div class="panel panel-danger">
  <div class="panel-heading"><div class="panel-title">Verificador de Permiso</div></div>
  <div class="panel-body" style="padding:20px 10px">
    <div class="alert alert-danger">
      El folio <strong>{folio}</strong> no fue encontrado en el sistema.
    </div>
  </div>
  <div class="panel-footer"><div class="text-center">Permiso de Tr&aacute;nsito Municipal</div></div>
</div>"""), 404)

    d = r.data[0]

    def fd(s):
        try: return datetime.strptime(s, "%Y-%m-%d").strftime("%d-%m-%Y")
        except: return s or "N/D"

    def row(lbl, val):
        return (f'<div class="row">'
                f'<div class="col-xs-5 etiqueta">{lbl}:</div>'
                f'<div class="col-xs-7 valor">{val}</div></div>')

    panel = f"""
<div class="panel panel-primary">
  <div class="panel-heading"><div class="panel-title">Verificador de Permiso</div></div>
  <div style="padding:20px 10px" class="panel-body">
    <div class="col-lg-12 text-center" style="padding:0;margin:0">
      <div class="row">
        <div class="col-xs-12" style="font-size:13px;margin-left:-25px">
          {row("Marca",                   d.get("marca","N/D"))}
          {row("L&iacute;nea",            d.get("linea","N/D"))}
          {row("Modelo",                  d.get("anio","N/D"))}
          {row("Color",                   d.get("color","N/D"))}
          {row("N&uacute;m. Serie",       d.get("numero_serie","N/D"))}
          {row("N&uacute;m. Motor",       d.get("numero_motor","N/D"))}
          {row("CONTRIBUYENTE",           d.get("nombre","N/D"))}
          {row("Fecha Expedici&oacute;n", fd(d.get("fecha_expedicion","")))}
          {row("Fecha Vencimiento",        fd(d.get("fecha_vencimiento","")))}
        </div>
      </div>
    </div>
  </div>
  <div class="panel-footer">
    <div class="text-center">Permiso de Tr&aacute;nsito Municipal</div>
  </div>
</div>"""

    return HTMLResponse(wrap(panel))

@app.get("/")
async def health():
    return {
        "ok":             True,
        "version":        "7.3",
        "entidad":        "Guerrero",
        "costo_fijo":     COSTO_FIJO,
        "siguiente_folio": f"{FOLIO_LETRAS}{str(_folio_counter['siguiente']).zfill(4)}",
        "active_timers":  len(timers_activos),
        "bucket_storage": BUCKET_NAME,
        "fixes_v7.3": [
            "PDF sube automático al bucket Storage 'permisos-guerrero' (mismo del Flask)",
            "Subida a Storage en background — no bloquea el envío del PDF al usuario",
            "Columna pdf_url se actualiza una vez subido",
        ]
    }

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
