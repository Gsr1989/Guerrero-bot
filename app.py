from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from aiogram import Bot, Dispatcher, types
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.filters import Command
from aiogram.types import FSInputFile, ContentType, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timedelta
from supabase import create_client, Client
import asyncio
import os
import fitz
from io import BytesIO
import qrcode

# ------------ CONFIG ------------
BOT_TOKEN        = os.getenv("BOT_TOKEN", "")
SUPABASE_URL     = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY     = os.getenv("SUPABASE_KEY", "")
BASE_URL         = os.getenv("BASE_URL", "").rstrip("/")
URL_CONSULTA_BASE= "https://tlapadecomonfortexpediciondepermisosgob2-k6u7.onrender.com"
OUTPUT_DIR       = "documentos"
PLANTILLA_PDF    = "Guerrero.pdf"
PLANTILLA_FLASK  = "recibo_permiso_guerrero_img.pdf"

os.makedirs(OUTPUT_DIR,   exist_ok=True)
os.makedirs("static/pdfs", exist_ok=True)

# ------------ SUPABASE ------------
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ------------ BOT ------------
bot     = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp      = Dispatcher(storage=storage)

# ------------ TIMERS ------------
timers_activos     = {}
user_folios        = {}
pending_comprobantes = {}
TOTAL_MINUTOS_TIMER  = 36 * 60

async def eliminar_folio_automatico(folio: str):
    try:
        uid = timers_activos[folio]["user_id"] if folio in timers_activos else None
        supabase.table("folios_registrados").delete().eq("folio", folio).execute()
        if uid:
            await bot.send_message(uid,
                f"⏰ TIEMPO AGOTADO - GUERRERO\n\nEl folio {folio} fue eliminado por no "
                f"completar el pago en 36 horas.\n\n📋 Use /chuleta para generar otro permiso.")
        limpiar_timer_folio(folio)
    except Exception as e:
        print(f"Error eliminando folio {folio}: {e}")

async def enviar_recordatorio(folio: str, mins: int):
    try:
        if folio not in timers_activos: return
        uid = timers_activos[folio]["user_id"]
        costo = ""
        try:
            r = supabase.table("folios_registrados").select("costo").eq("folio", folio).execute()
            if r.data: costo = f"\nMonto: ${r.data[0].get('costo','N/D')}"
        except: pass
        await bot.send_message(uid,
            f"⚡ RECORDATORIO - GUERRERO\nFolio: {folio}\n"
            f"Tiempo restante: {mins} minutos{costo}\n\n"
            f"📸 Envía tu comprobante (foto).\n\n📋 /chuleta para nuevo permiso")
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

# ------------ COORDENADAS PERMISO (Guerrero.pdf) ------------
# ⚠️  Ajusta rfc / domicilio / costo / rot_rfc / rot_domicilio segun tu plantilla
coords_guerrero = {
    "folio":        (376, 769,  8, (1,0,0)),
    "fecha_exp":    (130, 755,  8, (0,0,0)),
    "fecha_ven":    (130, 768,  8, (0,0,0)),
    "serie":        (376, 742,  8, (0,0,0)),
    "motor":        (376, 729,  8, (0,0,0)),
    "marca":        (376, 700,  8, (0,0,0)),
    "linea":        (376, 714,  8, (0,0,0)),
    "color":        (376, 756,  8, (0,0,0)),
    "nombre":       (130, 700,  8, (0,0,0)),
    "rfc":          (130, 713,  8, (0,0,0)),   # ajustar
    "domicilio":    (130, 726,  8, (0,0,0)),   # ajustar
    "costo":        (130, 742,  8, (0,0,0)),   # solo horizontal
    "anio":         (  0,   0,  8, (0,0,0)),
    "rot_folio":    (440, 200, 83, (0,0,0)),
    "rot_fecha_exp":( 77, 205,  8, (0,0,0)),
    "rot_fecha_ven":( 63, 205,  8, (0,0,0)),
    "rot_serie":    (168, 110, 18, (0,0,0)),
    "rot_motor":    (224, 110, 18, (0,0,0)),
    "rot_marca":    (280, 110, 18, (0,0,0)),
    "rot_linea":    (280, 300, 18, (0,0,0)),
    "rot_anio":     (305, 530, 18, (0,0,0)),
    "rot_color":    (224, 400, 18, (0,0,0)),
    "rot_nombre":   (115, 205,  8, (0,0,0)),
    "rot_rfc":      (102, 205,  8, (0,0,0)),   # ajustar
    "rot_domicilio":(89, 205,  8, (0,0,0)),   # ajustar
}

# ------------ FOLIO GUERRERO ------------
def generar_folio_guerrero():
    letras  = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    prefijo = "ZY"
    try:
        ex = supabase.table("folios_registrados").select("folio").eq("entidad","Guerrero").execute().data
        usados = {r["folio"] for r in ex if r["folio"] and r["folio"][:2]==prefijo and len(r["folio"])==6}
    except Exception as e:
        print(f"Error folios: {e}"); usados = set()
    for i in range(100000):
        num = 4917 + i
        if num >= 10000: break
        f = f"{prefijo}{str(num).zfill(4)}"
        if f not in usados: return f
    for a in letras:
        for b in letras:
            par = a+b
            if par == prefijo: continue
            for n in range(1,10000):
                f = f"{par}{str(n).zfill(4)}"
                if f not in usados: return f
    return "ZZ9999"

# ------------ FSM ------------
class PermisoForm(StatesGroup):
    marca     = State()
    linea     = State()
    anio      = State()
    serie     = State()
    motor     = State()
    color     = State()
    nombre    = State()
    costo     = State()
    rfc       = State()
    domicilio = State()

# ------------ QR ------------
def make_qr(folio: str):
    url = f"{URL_CONSULTA_BASE}/consulta/{folio}"
    qr  = qrcode.QRCode(version=2,
                         error_correction=qrcode.constants.ERROR_CORRECT_M,
                         box_size=4, border=1)
    qr.add_data(url); qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
    return img, url

def qr_pixmap(folio: str):
    img, url = make_qr(folio)
    buf = BytesIO(); img.save(buf, format="PNG"); buf.seek(0)
    return fitz.Pixmap(buf.read()), url

# ======================================================
#  PDF PERMISO  (Guerrero.pdf)
# ======================================================
def generar_pdf_permiso(datos: dict) -> str:
    fol  = datos["folio"]
    path = f"{OUTPUT_DIR}/{fol}_permiso_tmp.pdf"
    try:
        doc = fitz.open(PLANTILLA_PDF); pg = doc[0]

        # Horizontales base
        for c in ["folio","fecha_exp","fecha_ven","serie","motor","marca","linea","color","nombre"]:
            if c in coords_guerrero and c in datos:
                x,y,s,col = coords_guerrero[c]
                pg.insert_text((x,y), str(datos[c]), fontsize=s, color=col)

        # Nuevos campos horizontales
        for c in ["rfc","domicilio"]:
            if datos.get(c):
                x,y,s,col = coords_guerrero[c]
                pg.insert_text((x,y), datos[c], fontsize=s, color=col)
        if datos.get("costo"):
            x,y,s,col = coords_guerrero["costo"]
            pg.insert_text((x,y), f"${datos['costo']}", fontsize=s, color=col)

        # Verticales base
        for k,val in [("rot_folio",fol),("rot_fecha_exp",datos["fecha_exp"]),
                       ("rot_fecha_ven",datos["fecha_ven"]),("rot_serie",datos["serie"]),
                       ("rot_motor",datos["motor"]),("rot_marca",datos["marca"]),
                       ("rot_linea",datos["linea"]),("rot_anio",datos["anio"]),
                       ("rot_color",datos["color"]),("rot_nombre",datos["nombre"])]:
            pg.insert_text(coords_guerrero[k][:2], val,
                           fontsize=coords_guerrero[k][2], rotate=270)
        if datos.get("rfc"):
            pg.insert_text(coords_guerrero["rot_rfc"][:2], datos["rfc"],
                           fontsize=coords_guerrero["rot_rfc"][2], rotate=270)
        if datos.get("domicilio"):
            pg.insert_text(coords_guerrero["rot_domicilio"][:2], datos["domicilio"],
                           fontsize=coords_guerrero["rot_domicilio"][2], rotate=270)

        # QR reducido 25% → 97×97, coordenadas originales
        qr_pix, _ = qr_pixmap(fol)
        pg.insert_image(fitz.Rect(80, 430, 80+97, 430+97), pixmap=qr_pix, overlay=True)

        doc.save(path); doc.close()
        print(f"[PERMISO] OK: {path}")
        return path
    except Exception as e:
        print(f"[ERROR PERMISO] {e}"); return ""


# ======================================================
#  PDF RECIBO  (recibo_permiso_guerrero_img.pdf)
#
#  El recibo tiene DOS secciones:
#  ① RECIBO DE PAGO (parte superior) ← había que llenar esto
#  ② PERMISO PROVISIONAL (tabla inferior)
#
#  COORDENADAS — ⚠️ ajusta según donde cae el texto en TU plantilla.
#  Las variables R_* son para la sección del recibo (arriba).
#  Las variables P_* son para la tabla del permiso (abajo).
# ======================================================

# ── Sección RECIBO (superior) ────────────────────────
#  Col izquierda: Nombre, RFC, Dirección, Importe, Expedición, Vencimiento
R_X_IZQ   = 780   # x donde empieza el VALOR en columna izquierda
R_X_DER   = 3000  # x donde empieza el VALOR en columna derecha
R_Y_ROW1  = 550   # Nombre / Marca
R_Y_ROW2  = 730   # RFC    / Línea
R_Y_ROW3  = 910   # Dir    / Motor
R_Y_ROW4  = 1090  # Importe/ Serie
R_Y_ROW5  = 1270  # Expedi / Color
R_Y_ROW6  = 1450  # Vencim / Folio
R_FS      = 100   # fontsize para el recibo

# ── Sección TABLA PERMISO (inferior) ─────────────────
P_X_FOLIO   = 700   # col1 folio
P_X_FEXP    = 2200  # col2 fecha exp
P_X_FVEN    = 4000  # col3 fecha ven
P_Y_ROW1    = 1750  # fila Folio/Fechas
P_Y_SOL     = 1930  # fila Solicitante
P_Y_DOM     = 2110  # fila Domicilio
P_X_TEXT    = 950   # x para textos de filas 2,3
P_FS        = 100   # fontsize tabla permiso

# ── QR en la sección de "Código QR Datos" ────────────
QR_X        = 950   # esquina superior-izq del QR
QR_Y        = 2280  # esquina superior-izq del QR
QR_SIZE     = 600   # tamaño en puntos (más grande para que sea visible en PDF grande)

# ── Importe en esquina inferior derecha del área QR ──
P_X_IMPORTE = 3800
P_Y_IMPORTE = 2850
P_FS_IMP    = 100

# ======================================================
#  COORDENADAS RECIBO (IGUAL QUE LA HOJA 1)
# ======================================================

coords_recibo = {
    "folio":     (100, 100, 12, (0,0,0)),
    "nombre":    (130, 115, 12, (0,0,0)),
    "rfc":       (300, 460, 12, (0,0,0)),
    "domicilio": (130, 130, 12, (0,0,0)),
    "costo":     (380, 420, 12, (0,0,0)),
    "fecha_exp": (200, 550, 12, (0,0,0)),
    "fecha_ven": (300, 580, 12, (0,0,0)),
    "qr":        (100, 420, 120, None)
}


# ======================================================
#  PDF RECIBO (SIN MAMADAS, SOLO COORDENADAS)
# ======================================================

def generar_pdf_recibo(datos: dict) -> str:
    fol  = datos["folio"]
    path = f"{OUTPUT_DIR}/{fol}_recibo_tmp.pdf"

    try:
        doc  = fitz.open(PLANTILLA_FLASK)
        page = doc[0]

        # IMPORTANTE PARA QUE SE VEA
        page.wrap_contents()

        # TEXTO (SOLO VALORES)
        for campo in ["folio","nombre","rfc","domicilio","costo","fecha_exp","fecha_ven"]:
            if campo in coords_recibo:

                x, y, size, color = coords_recibo[campo]

                valor = datos.get(campo, "")

                if campo == "costo":
                    valor = f"${valor}"

                page.insert_text(
                    (x, y),
                    str(valor),
                    fontsize=size,
                    color=color,
                    fontname="helv"
                )

        # QR
        qr_pix, _ = qr_pixmap(fol)
        x, y, size, _ = coords_recibo["qr"]

        page.insert_image(
            fitz.Rect(x, y, x + size, y + size),
            pixmap=qr_pix
        )

        doc.save(path)
        doc.close()

        print(f"[RECIBO OK] {path}")
        return path

    except Exception as e:
        print(f"[ERROR RECIBO] {e}")
        return ""

# ======================================================
#  PDF UNIFICADO  (permiso pág 1  +  recibo pág 2)
# ======================================================
def generar_pdf_unificado(datos: dict) -> str:
    fol   = datos["folio"]
    final = f"{OUTPUT_DIR}/{fol}_guerrero_completo.pdf"
    try:
        p1 = generar_pdf_permiso(datos)
        p2 = generar_pdf_recibo(datos)
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


# ============================================================
#  HANDLERS
# ============================================================
@dp.message(Command("start"))
async def start_cmd(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "🏛️ SISTEMA DIGITAL DEL ESTADO DE GUERRERO\n\n"
        "⏰ Tiempo límite de pago: 36 horas\n\n"
        "📋 Use /chuleta para generar un permiso."
    )

@dp.message(Command("chuleta"))
async def chuleta_cmd(message: types.Message, state: FSMContext):
    fa = obtener_folios_usuario(message.from_user.id)
    ex = f"\n\n📋 FOLIOS ACTIVOS: {', '.join(fa)}" if fa else ""
    await message.answer(f"🚗 NUEVO PERMISO - GUERRERO{ex}\n\nPaso 1: MARCA del vehículo:")
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
    await state.update_data(nombre=message.text.strip().upper())
    await message.answer("COSTO del permiso (ej: 200):")
    await state.set_state(PermisoForm.costo)

@dp.message(PermisoForm.costo)
async def get_costo(message: types.Message, state: FSMContext):
    raw = message.text.strip().replace("$","").replace(",","")
    try: float(raw)
    except ValueError:
        await message.answer("⚠️ Monto inválido. Ej: 200"); return
    await state.update_data(costo=raw)
    await message.answer("RFC del propietario (o N/A):")
    await state.set_state(PermisoForm.rfc)

@dp.message(PermisoForm.rfc)
async def get_rfc(message: types.Message, state: FSMContext):
    await state.update_data(rfc=message.text.strip().upper())
    await message.answer("DOMICILIO del propietario:")
    await state.set_state(PermisoForm.domicilio)

@dp.message(PermisoForm.domicilio)
async def get_domicilio(message: types.Message, state: FSMContext):
    datos     = await state.get_data()
    domicilio = message.text.strip().upper()
    folio     = generar_folio_guerrero()
    hoy       = datetime.now()
    ven       = hoy + timedelta(days=30)

    datos_pdf = {
        "folio":         folio,
        "marca":         datos["marca"],
        "linea":         datos["linea"],
        "anio":          datos["anio"],
        "serie":         datos["serie"],
        "motor":         datos["motor"],
        "color":         datos["color"],
        "nombre":        datos["nombre"],
        "costo":         datos["costo"],
        "rfc":           datos["rfc"],
        "domicilio":     domicilio,
        "fecha_exp":     hoy.strftime("%d/%m/%Y"),
        "fecha_ven":     ven.strftime("%d/%m/%Y"),
        "fecha_exp_obj": hoy,
        "fecha_ven_obj": ven,
    }

    try:
        await message.answer(
            f"🔄 Generando...\n<b>Folio:</b> {folio}\n<b>Titular:</b> {datos['nombre']}",
            parse_mode="HTML")

        pdf = generar_pdf_unificado(datos_pdf)

        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🔑 Validar Admin",  callback_data=f"validar_{folio}"),
            InlineKeyboardButton(text="⏹️ Detener Timer", callback_data=f"detener_{folio}")
        ]])

        if pdf:
            await message.answer_document(
                FSInputFile(pdf),
                caption=(
                    f"📋 PERMISO DE CIRCULACIÓN — GUERRERO\n"
                    f"Folio: {folio}\n"
                    f"Titular: {datos['nombre']}\n"
                    f"RFC: {datos['rfc']}\n"
                    f"Domicilio: {domicilio}\n"
                    f"Vigencia: 30 días | Costo: ${datos['costo']}\n\n"
                    f"✅ Permiso + Recibo (PDF unificado)\n\n"
                    f"⏰ TIMER ACTIVO — 36 horas"
                ),
                reply_markup=kb)
        else:
            await message.answer("❌ Error al generar PDF. Intenta de nuevo con /chuleta")
            await state.clear(); return

        supabase.table("folios_registrados").insert({
            "folio":             folio,
            "marca":             datos["marca"],
            "linea":             datos["linea"],
            "anio":              datos["anio"],
            "numero_serie":      datos["serie"],
            "numero_motor":      datos["motor"],
            "color":             datos["color"],
            "nombre":            datos["nombre"],
            "rfc":               datos["rfc"],
            "domicilio":         domicilio,
            "costo":             datos["costo"],
            "fecha_expedicion":  hoy.date().isoformat(),
            "fecha_vencimiento": ven.date().isoformat(),
            "entidad":           "Guerrero",
            "estado":            "PENDIENTE",
            "user_id":           message.from_user.id,
            "username":          message.from_user.username or "Sin username"
        }).execute()

        await iniciar_timer_eliminacion(message.from_user.id, folio)

        await message.answer(
            f"💰 INSTRUCCIONES DE PAGO\n\n"
            f"📄 Folio: {folio}\n💵 Monto: ${datos['costo']}\n⏰ Límite: 36 horas\n\n"
            f"🏦 TRANSFERENCIA:\n• Titular: GUILLERMO S.R.J\n• Número: 7289690000484424454\n\n"
            f"🏪 OXXO:\n• Referencia: 2242170180214090\n• Monto: ${datos['costo']}\n\n"
            f"📸 Envía foto del comprobante para validar.\n\n📋 /chuleta para nuevo permiso")

    except Exception as e:
        await message.answer(f"❌ Error: {e}\n\n📋 /chuleta")
        print(f"Error get_domicilio: {e}")
    finally:
        await state.clear()


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
            supabase.table("folios_registrados").update(
                {"estado":"VALIDADO_ADMIN","fecha_comprobante":datetime.now().isoformat()}
            ).eq("folio",folio).execute()
        except: pass
        await callback.answer("✅ Validado", show_alert=True)
        await callback.message.edit_reply_markup(reply_markup=None)
        try:
            await bot.send_message(uid,
                f"✅ PAGO VALIDADO — GUERRERO\nFolio: {folio}\n"
                f"Tu permiso está activo.\n\n📋 /chuleta para nuevo permiso")
        except: pass
    else:
        await callback.answer("❌ Timer no activo", show_alert=True)

@dp.callback_query(lambda c: c.data and c.data.startswith("detener_"))
async def cb_detener(callback: CallbackQuery):
    folio = callback.data.replace("detener_", "")
    if folio in timers_activos:
        cancelar_timer_folio(folio)
        try:
            supabase.table("folios_registrados").update(
                {"estado":"TIMER_DETENIDO","fecha_detencion":datetime.now().isoformat()}
            ).eq("folio",folio).execute()
        except: pass
        await callback.answer("⏹️ Timer detenido", show_alert=True)
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.answer(f"⏹️ Timer detenido\nFolio: {folio}\n\n📋 /chuleta")
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
            supabase.table("folios_registrados").update(
                {"estado":"VALIDADO_ADMIN","fecha_comprobante":datetime.now().isoformat()}
            ).eq("folio",fa).execute()
        except: pass
        await message.answer(f"✅ VALIDADO\nFolio: {fa}")
        try:
            await bot.send_message(uid,
                f"✅ PAGO VALIDADO — GUERRERO\nFolio: {fa}\n"
                f"Tu permiso está activo.\n\n📋 /chuleta para nuevo permiso")
        except: pass
    else:
        await message.answer(f"❌ Folio {fa} no encontrado en timers activos")

@dp.message(lambda m: m.content_type == ContentType.PHOTO)
async def recibir_comprobante(message: types.Message):
    uid = message.from_user.id
    fl  = obtener_folios_usuario(uid)
    if not fl:
        await message.answer("ℹ️ No hay trámites pendientes.\n\n📋 /chuleta"); return
    if len(fl) > 1:
        lista = "\n".join(f"• {f}" for f in fl)
        pending_comprobantes[uid] = "waiting_folio"
        await message.answer(
            f"📄 Varios folios activos:\n\n{lista}\n\n"
            f"Responde con el FOLIO al que corresponde este comprobante.\n\n📋 /chuleta")
        return
    folio = fl[0]; cancelar_timer_folio(folio)
    try:
        supabase.table("folios_registrados").update(
            {"estado":"COMPROBANTE_ENVIADO","fecha_comprobante":datetime.now().isoformat()}
        ).eq("folio",folio).execute()
    except: pass
    await message.answer(f"✅ Comprobante recibido.\n📄 Folio: {folio}\n⏹️ Timer detenido.\n\n📋 /chuleta")

@dp.message(lambda m: m.from_user.id in pending_comprobantes
            and pending_comprobantes[m.from_user.id] == "waiting_folio")
async def especificar_folio(message: types.Message):
    uid = message.from_user.id
    fe  = message.text.strip().upper()
    fl  = obtener_folios_usuario(uid)
    if fe not in fl:
        await message.answer("❌ Folio no encontrado en tu lista.\n\n📋 /chuleta"); return
    cancelar_timer_folio(fe); del pending_comprobantes[uid]
    try:
        supabase.table("folios_registrados").update(
            {"estado":"COMPROBANTE_ENVIADO","fecha_comprobante":datetime.now().isoformat()}
        ).eq("folio",fe).execute()
    except: pass
    await message.answer(f"✅ Comprobante asociado.\n📄 Folio: {fe}\n\n📋 /chuleta")

@dp.message(Command("folios"))
async def ver_folios(message: types.Message):
    fl = obtener_folios_usuario(message.from_user.id)
    if not fl:
        await message.answer("ℹ️ No hay folios activos.\n\n📋 /chuleta"); return
    rows = []
    for f in fl:
        if f in timers_activos:
            seg = max(0, int(TOTAL_MINUTOS_TIMER*60 -
                             (datetime.now()-timers_activos[f]["start_time"]).total_seconds()))
            h,m = divmod(seg//60, 60)
            rows.append(f"• {f} ({h}h {m}min)")
        else:
            rows.append(f"• {f} (sin timer)")
    await message.answer(f"📋 FOLIOS ACTIVOS ({len(fl)})\n\n" + "\n".join(rows) + "\n\n📋 /chuleta")

@dp.message(lambda m: m.text and any(p in m.text.lower() for p in
    ["costo","precio","cuanto","cuánto","deposito","depósito","pago","valor","monto"]))
async def info_costo(message: types.Message):
    await message.answer("💰 El costo se define al generar el permiso.\n\n📋 Use /chuleta")

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
        await bot.delete_webhook(drop_pending_updates=True)
        if BASE_URL:
            wh = f"{BASE_URL}/webhook"
            await bot.set_webhook(wh, allowed_updates=["message","callback_query"])
            print(f"[WEBHOOK] {wh}")
            _keep_task = asyncio.create_task(keep_alive())
        else:
            print("[POLLING] Sin webhook")
        print("[SISTEMA] Guerrero v6.0 listo")
        yield
    except Exception as e:
        print(f"[ERROR] {e}"); yield
    finally:
        if _keep_task:
            _keep_task.cancel()
            with suppress(asyncio.CancelledError): await _keep_task
        await bot.session.close()

app = FastAPI(lifespan=lifespan, title="Sistema Guerrero", version="6.0")

@app.post("/webhook")
async def webhook(request: Request):
    try:
        data = await request.json()
        await dp.feed_webhook_update(bot, types.Update(**data))
        return {"ok": True}
    except Exception as e:
        print(f"[ERROR webhook] {e}"); return {"ok": False, "error": str(e)}


# ============================================================
#  PÁGINA DE VERIFICACIÓN  /consulta/{folio}
# ============================================================
@app.get("/consulta/{folio}", response_class=HTMLResponse)
async def consulta(folio: str):
    CSS   = "https://direcciontransitotlapadecomonfort.gob.mx/css/bootstrap.min.css"
    JQ    = "https://direcciontransitotlapadecomonfort.gob.mx/js/jquery-3.4.1.min.js"
    BSJS  = "https://direcciontransitotlapadecomonfort.gob.mx/js/bootstrap.min.js"
    LOGO  = "https://direcciontransitotlapadecomonfort.gob.mx/img/transformando_guerrero.jpg"
    FAV   = "https://direcciontransitotlapadecomonfort.gob.mx/img/favicon-96x96.png?v2"

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
        r = supabase.table("folios_registrados").select("*").eq("folio", folio).execute()
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
        try:
            return datetime.strptime(s, "%Y-%m-%d").strftime("%d-%m-%Y")
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
          {row("Marca",                     d.get("marca","N/D"))}
          {row("L&iacute;nea",              d.get("linea","N/D"))}
          {row("Modelo",                    d.get("anio","N/D"))}
          {row("Color",                     d.get("color","N/D"))}
          {row("N&uacute;mero de Serie",    d.get("numero_serie","N/D"))}
          {row("N&uacute;mero de Motor",    d.get("numero_motor","N/D"))}
          {row("CONTRIBUYENTE",             d.get("nombre","N/D"))}
          {row("Fecha Expedici&oacute;n",   fd(d.get("fecha_expedicion","")))}
          {row("Fecha Vencimiento",          fd(d.get("fecha_vencimiento","")))}
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
        "ok": True, "version": "6.0",
        "entidad": "Guerrero",
        "active_timers": len(timers_activos),
        "pdf": "unificado (permiso+recibo)",
        "consulta": "/consulta/{folio}",
    }

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
