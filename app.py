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
from PIL import Image
from io import BytesIO
import qrcode

# ------------ CONFIG ------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")
BASE_URL = os.getenv("BASE_URL", "").rstrip("/")
URL_CONSULTA_BASE = "https://tlapadecomonfortexpediciondepermisosgob2.onrender.com"
OUTPUT_DIR = "documentos"
PLANTILLA_PDF = "Guerrero.pdf"
PLANTILLA_FLASK = "recibo_permiso_guerrero_img.pdf"

os.makedirs(OUTPUT_DIR, exist_ok=True)
os.makedirs("static/pdfs", exist_ok=True)

# ------------ SUPABASE ------------
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ------------ BOT ------------
bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# ------------ TIMER MANAGEMENT - 36 HORAS ------------
timers_activos = {}
user_folios = {}
pending_comprobantes = {}
TOTAL_MINUTOS_TIMER = 36 * 60

async def eliminar_folio_automatico(folio: str):
    try:
        user_id = None
        if folio in timers_activos:
            user_id = timers_activos[folio]["user_id"]
        supabase.table("folios_registrados").delete().eq("folio", folio).execute()
        if user_id:
            await bot.send_message(
                user_id,
                f"⏰ TIEMPO AGOTADO - GUERRERO\n\n"
                f"El folio {folio} ha sido eliminado por no completar el pago en 36 horas.\n\n"
                f"📋 Para generar otro permiso use /chuleta"
            )
        limpiar_timer_folio(folio)
    except Exception as e:
        print(f"Error eliminando folio {folio}: {e}")

async def enviar_recordatorio(folio: str, minutos_restantes: int):
    try:
        if folio not in timers_activos:
            return
        user_id = timers_activos[folio]["user_id"]
        costo_txt = ""
        try:
            res = supabase.table("folios_registrados").select("costo").eq("folio", folio).execute()
            if res.data:
                costo_txt = f"\nMonto: ${res.data[0].get('costo', 'N/D')}"
        except:
            pass
        await bot.send_message(
            user_id,
            f"⚡ RECORDATORIO DE PAGO - GUERRERO\n\n"
            f"Folio: {folio}\n"
            f"Tiempo restante: {minutos_restantes} minutos{costo_txt}\n\n"
            f"📸 Envíe su comprobante (imagen) para validar.\n\n"
            f"📋 Para generar otro permiso use /chuleta"
        )
    except Exception as e:
        print(f"Error enviando recordatorio {folio}: {e}")

async def iniciar_timer_eliminacion(user_id: int, folio: str):
    async def timer_task():
        print(f"[TIMER] Iniciado para folio {folio} (36h)")
        await asyncio.sleep(34.5 * 3600)
        if folio not in timers_activos: return
        await enviar_recordatorio(folio, 90)
        await asyncio.sleep(30 * 60)
        if folio not in timers_activos: return
        await enviar_recordatorio(folio, 60)
        await asyncio.sleep(30 * 60)
        if folio not in timers_activos: return
        await enviar_recordatorio(folio, 30)
        await asyncio.sleep(20 * 60)
        if folio not in timers_activos: return
        await enviar_recordatorio(folio, 10)
        await asyncio.sleep(10 * 60)
        if folio in timers_activos:
            print(f"[TIMER] Expirado folio {folio} - eliminando")
            await eliminar_folio_automatico(folio)
    task = asyncio.create_task(timer_task())
    timers_activos[folio] = {"task": task, "user_id": user_id, "start_time": datetime.now()}
    if user_id not in user_folios:
        user_folios[user_id] = []
    user_folios[user_id].append(folio)
    print(f"[SISTEMA] Timer 36h iniciado folio {folio}, total: {len(timers_activos)}")

def cancelar_timer_folio(folio: str):
    if folio in timers_activos:
        timers_activos[folio]["task"].cancel()
        user_id = timers_activos[folio]["user_id"]
        del timers_activos[folio]
        if user_id in user_folios and folio in user_folios[user_id]:
            user_folios[user_id].remove(folio)
            if not user_folios[user_id]:
                del user_folios[user_id]
        print(f"[SISTEMA] Timer cancelado folio {folio}")

def limpiar_timer_folio(folio: str):
    if folio in timers_activos:
        user_id = timers_activos[folio]["user_id"]
        del timers_activos[folio]
        if user_id in user_folios and folio in user_folios[user_id]:
            user_folios[user_id].remove(folio)
            if not user_folios[user_id]:
                del user_folios[user_id]

def obtener_folios_usuario(user_id: int) -> list:
    return user_folios.get(user_id, [])

# -------- COORDENADAS GUERRERO --------
# Ajusta rfc/domicilio/costo/rot_rfc/rot_domicilio segun tu plantilla real
coords_guerrero = {
    # Seccion HORIZONTAL
    "folio":       (376, 769,  8, (1, 0, 0)),
    "fecha_exp":   (130, 755,  8, (0, 0, 0)),
    "fecha_ven":   (130, 768,  8, (0, 0, 0)),
    "serie":       (376, 742,  8, (0, 0, 0)),
    "motor":       (376, 729,  8, (0, 0, 0)),
    "marca":       (376, 700,  8, (0, 0, 0)),
    "linea":       (376, 714,  8, (0, 0, 0)),
    "color":       (376, 756,  8, (0, 0, 0)),
    "nombre":      (130, 700,  8, (0, 0, 0)),
    "rfc":         (130, 714,  8, (0, 0, 0)),  # abajo de nombre
    "domicilio":   (130, 728,  8, (0, 0, 0)),  # abajo de rfc
    "costo":       (130, 742,  8, (0, 0, 0)),  # abajo de domicilio (solo horizontal)
    "anio":        (  0,   0,  8, (0, 0, 0)),
    # Seccion VERTICAL (rotada 270 grados)
    "rot_folio":    (440, 200, 83, (0, 0, 0)),
    "rot_fecha_exp":( 77, 205,  8, (0, 0, 0)),
    "rot_fecha_ven":( 63, 205,  8, (0, 0, 0)),
    "rot_serie":    (168, 110, 18, (0, 0, 0)),
    "rot_motor":    (224, 110, 18, (0, 0, 0)),
    "rot_marca":    (280, 110, 18, (0, 0, 0)),
    "rot_linea":    (280, 300, 18, (0, 0, 0)),
    "rot_anio":     (305, 530, 18, (0, 0, 0)),
    "rot_color":    (224, 400, 18, (0, 0, 0)),
    "rot_nombre":   (115, 205,  8, (0, 0, 0)),
    "rot_rfc":      (130, 205,  8, (0, 0, 0)),  # ajustar segun plantilla
    "rot_domicilio":(145, 205,  8, (0, 0, 0)),  # ajustar segun plantilla
}

# ------------ FOLIO GUERRERO ------------
def generar_folio_guerrero():
    letras = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    prefijo = "ZY"
    inicio_num = 4917
    try:
        existentes = supabase.table("folios_registrados").select("folio").eq("entidad", "Guerrero").execute().data
        usados = set([r["folio"] for r in existentes if r["folio"] and len(r["folio"]) == 6 and r["folio"][:2] == prefijo])
    except Exception as e:
        print(f"Error consultando folios: {e}")
        usados = set()
    for intento in range(100000):
        num = inicio_num + intento
        if num >= 10000:
            break
        folio_candidato = f"{prefijo}{str(num).zfill(4)}"
        if folio_candidato not in usados:
            print(f"[FOLIO GUERRERO] Generado: {folio_candidato}")
            return folio_candidato
    for l1 in letras:
        for l2 in letras:
            par = l1 + l2
            if par == prefijo:
                continue
            for num in range(1, 10000):
                folio_candidato = f"{par}{str(num).zfill(4)}"
                if folio_candidato not in usados:
                    return folio_candidato
    return "ZZ9999"

# ------------ FSM STATES ------------
class PermisoForm(StatesGroup):
    marca     = State()
    linea     = State()
    anio      = State()
    serie     = State()
    motor     = State()
    color     = State()
    nombre    = State()
    costo     = State()    # NUEVO
    rfc       = State()    # NUEVO
    domicilio = State()    # NUEVO

# ------------ QR ------------
def generar_qr_dinamico_guerrero(folio):
    try:
        url = f"{URL_CONSULTA_BASE}/consulta/{folio}"
        qr = qrcode.QRCode(version=2, error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=4, border=1)
        qr.add_data(url)
        qr.make(fit=True)
        img_qr = qr.make_image(fill_color="black", back_color="white").convert("RGB")
        print(f"[QR] Generado para {folio} -> {url}")
        return img_qr, url
    except Exception as e:
        print(f"[ERROR QR] {e}")
        return None, None

# ====== PDF PRINCIPAL (permiso) ======
def generar_pdf_principal(datos: dict) -> str:
    """Genera el permiso con campos nuevos y QR reducido 25% (97x97)."""
    fol = datos["folio"]
    filename = f"{OUTPUT_DIR}/{fol}_principal_tmp.pdf"
    try:
        doc = fitz.open(PLANTILLA_PDF)
        pg  = doc[0]

        # Campos horizontales base
        for campo in ["folio","fecha_exp","fecha_ven","serie","motor","marca","linea","color","nombre"]:
            if campo in coords_guerrero and campo in datos:
                x, y, s, col = coords_guerrero[campo]
                pg.insert_text((x, y), str(datos.get(campo, "")), fontsize=s, color=col)

        # Nuevos campos horizontales
        for campo in ["rfc", "domicilio"]:
            if datos.get(campo):
                x, y, s, col = coords_guerrero[campo]
                pg.insert_text((x, y), datos[campo], fontsize=s, color=col)
        if datos.get("costo"):
            x, y, s, col = coords_guerrero["costo"]
            pg.insert_text((x, y), f"${datos['costo']}", fontsize=s, color=col)

        # Campos verticales base
        pg.insert_text(coords_guerrero["rot_folio"][:2],     fol,              fontsize=coords_guerrero["rot_folio"][2],    rotate=270)
        pg.insert_text(coords_guerrero["rot_fecha_exp"][:2], datos["fecha_exp"],fontsize=coords_guerrero["rot_fecha_exp"][2],rotate=270)
        pg.insert_text(coords_guerrero["rot_fecha_ven"][:2], datos["fecha_ven"],fontsize=coords_guerrero["rot_fecha_ven"][2],rotate=270)
        pg.insert_text(coords_guerrero["rot_serie"][:2],     datos["serie"],   fontsize=coords_guerrero["rot_serie"][2],    rotate=270)
        pg.insert_text(coords_guerrero["rot_motor"][:2],     datos["motor"],   fontsize=coords_guerrero["rot_motor"][2],    rotate=270)
        pg.insert_text(coords_guerrero["rot_marca"][:2],     datos["marca"],   fontsize=coords_guerrero["rot_marca"][2],    rotate=270)
        pg.insert_text(coords_guerrero["rot_linea"][:2],     datos["linea"],   fontsize=coords_guerrero["rot_linea"][2],    rotate=270)
        pg.insert_text(coords_guerrero["rot_anio"][:2],      datos["anio"],    fontsize=coords_guerrero["rot_anio"][2],     rotate=270)
        pg.insert_text(coords_guerrero["rot_color"][:2],     datos["color"],   fontsize=coords_guerrero["rot_color"][2],    rotate=270)
        pg.insert_text(coords_guerrero["rot_nombre"][:2],    datos["nombre"],  fontsize=coords_guerrero["rot_nombre"][2],   rotate=270)

        # Nuevos campos verticales
        if datos.get("rfc"):
            pg.insert_text(coords_guerrero["rot_rfc"][:2], datos["rfc"],
                           fontsize=coords_guerrero["rot_rfc"][2], rotate=270)
        if datos.get("domicilio"):
            pg.insert_text(coords_guerrero["rot_domicilio"][:2], datos["domicilio"],
                           fontsize=coords_guerrero["rot_domicilio"][2], rotate=270)

        # QR reducido 25%: 130 -> 97, mismas coordenadas de origen
        img_qr, _ = generar_qr_dinamico_guerrero(fol)
        if img_qr:
            buf = BytesIO()
            img_qr.save(buf, format="PNG")
            buf.seek(0)
            qr_pix = fitz.Pixmap(buf.read())
            x_qr, y_qr, tam = 80, 430, 97  # tam original 130 * 0.75 = 97
            pg.insert_image(fitz.Rect(x_qr, y_qr, x_qr + tam, y_qr + tam), pixmap=qr_pix, overlay=True)
            print("[QR PERMISO] Insertado 97x97")

        doc.save(filename)
        doc.close()
        print(f"[PDF PRINCIPAL] OK: {filename}")
        return filename
    except Exception as e:
        print(f"[ERROR PDF PRINCIPAL] {e}")
        return ""

# ====== PDF RECIBO ======
def generar_pdf_recibo(datos: dict) -> str:
    """Recibo con QR (97x97 mismo tamanio que permiso), nombre, domicilio e importe."""
    fol = datos["folio"]
    filename = f"{OUTPUT_DIR}/{fol}_recibo_tmp.pdf"
    try:
        doc  = fitz.open(PLANTILLA_FLASK)
        page = doc[0]
        exp_dt = datos["fecha_exp_obj"]
        ven_dt = datos["fecha_ven_obj"]

        # Campos base del recibo
        page.insert_text((700,  1750), fol,                               fontsize=100, fontname="helv")
        page.insert_text((2200, 1750), exp_dt.strftime('%d/%m/%Y'),       fontsize=100, fontname="helv")
        page.insert_text((4000, 1750), ven_dt.strftime('%d/%m/%Y'),       fontsize=100, fontname="helv")
        page.insert_text((950,  1930), datos["nombre"].upper(),            fontsize=100, fontname="helv")

        # Nuevos campos
        page.insert_text((950, 2110), datos.get("domicilio","").upper(),  fontsize=100, fontname="helv")
        page.insert_text((950, 2290), f'${datos.get("costo","")}',        fontsize=100, fontname="helv")

        # QR mismo tamanio que en el permiso (97x97)
        # Ajusta x_qr_r / y_qr_r segun tu plantilla de recibo
        img_qr, _ = generar_qr_dinamico_guerrero(fol)
        if img_qr:
            buf = BytesIO()
            img_qr.save(buf, format="PNG")
            buf.seek(0)
            qr_pix = fitz.Pixmap(buf.read())
            x_qr_r, y_qr_r, tam_r = 100, 1750, 97
            page.insert_image(fitz.Rect(x_qr_r, y_qr_r, x_qr_r + tam_r, y_qr_r + tam_r),
                               pixmap=qr_pix, overlay=True)
            print("[QR RECIBO] Insertado 97x97")

        doc.save(filename)
        doc.close()
        print(f"[PDF RECIBO] OK: {filename}")
        return filename
    except Exception as e:
        print(f"[ERROR PDF RECIBO] {e}")
        return ""

# ====== PDF UNIFICADO (permiso pag1 + recibo pag2) ======
def generar_pdf_unificado(datos: dict) -> str:
    """Un solo PDF: permiso en pagina 1, recibo en pagina 2."""
    fol = datos["folio"]
    filename_final = f"{OUTPUT_DIR}/{fol}_guerrero_completo.pdf"
    try:
        pdf_p = generar_pdf_principal(datos)
        pdf_r = generar_pdf_recibo(datos)
        if not pdf_p or not pdf_r:
            print("[ERROR] Fallo generando PDF individual")
            return pdf_p or pdf_r or ""
        doc1 = fitz.open(pdf_p)
        doc2 = fitz.open(pdf_r)
        doc1.insert_pdf(doc2)
        doc1.save(filename_final)
        doc1.close()
        doc2.close()
        for tmp in [pdf_p, pdf_r]:
            try: os.remove(tmp)
            except: pass
        print(f"[PDF UNIFICADO] OK: {filename_final}")
        return filename_final
    except Exception as e:
        print(f"[ERROR PDF UNIFICADO] {e}")
        return ""

# ============ HANDLERS ============

@dp.message(Command("start"))
async def start_cmd(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "🏛️ SISTEMA DIGITAL DEL ESTADO DE GUERRERO\n\n"
        "⏰ Tiempo límite de pago: 36 horas\n\n"
        "⚠️ Su folio se elimina si no paga a tiempo.\n\n"
        "📋 Use /chuleta para generar un permiso."
    )

@dp.message(Command("chuleta"))
async def chuleta_cmd(message: types.Message, state: FSMContext):
    folios_activos = obtener_folios_usuario(message.from_user.id)
    msg_folios = ""
    if folios_activos:
        msg_folios = f"\n\n📋 FOLIOS ACTIVOS: {', '.join(folios_activos)}"
    await message.answer(
        f"🚗 NUEVO PERMISO - GUERRERO{msg_folios}\n\n"
        f"Paso 1: MARCA del vehículo:"
    )
    await state.set_state(PermisoForm.marca)

@dp.message(PermisoForm.marca)
async def get_marca(message: types.Message, state: FSMContext):
    await state.update_data(marca=message.text.strip().upper())
    await message.answer("LÍNEA/MODELO del vehículo:")
    await state.set_state(PermisoForm.linea)

@dp.message(PermisoForm.linea)
async def get_linea(message: types.Message, state: FSMContext):
    await state.update_data(linea=message.text.strip().upper())
    await message.answer("AÑO del vehículo (4 dígitos):")
    await state.set_state(PermisoForm.anio)

@dp.message(PermisoForm.anio)
async def get_anio(message: types.Message, state: FSMContext):
    anio = message.text.strip()
    if not anio.isdigit() or len(anio) != 4:
        await message.answer("⚠️ Año inválido. Use 4 dígitos (ej. 2021):")
        return
    await state.update_data(anio=anio)
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
    await message.answer("COLOR del vehículo:")
    await state.set_state(PermisoForm.color)

@dp.message(PermisoForm.color)
async def get_color(message: types.Message, state: FSMContext):
    await state.update_data(color=message.text.strip().upper())
    await message.answer("NOMBRE COMPLETO del propietario:")
    await state.set_state(PermisoForm.nombre)

@dp.message(PermisoForm.nombre)
async def get_nombre(message: types.Message, state: FSMContext):
    await state.update_data(nombre=message.text.strip().upper())
    await message.answer("COSTO del permiso (solo número, ej: 50):")
    await state.set_state(PermisoForm.costo)

@dp.message(PermisoForm.costo)
async def get_costo(message: types.Message, state: FSMContext):
    raw = message.text.strip().replace("$", "").replace(",", "")
    try:
        float(raw)
    except ValueError:
        await message.answer("⚠️ Monto inválido. Solo el número (ej: 50 o 150.00):")
        return
    await state.update_data(costo=raw)
    await message.answer("RFC del propietario (o N/A si no aplica):")
    await state.set_state(PermisoForm.rfc)

@dp.message(PermisoForm.rfc)
async def get_rfc(message: types.Message, state: FSMContext):
    await state.update_data(rfc=message.text.strip().upper())
    await message.answer("DOMICILIO del propietario:")
    await state.set_state(PermisoForm.domicilio)

@dp.message(PermisoForm.domicilio)
async def get_domicilio(message: types.Message, state: FSMContext):
    """Handler final: genera PDF unificado y guarda en DB."""
    datos     = await state.get_data()
    domicilio = message.text.strip().upper()
    folio     = generar_folio_guerrero()
    hoy       = datetime.now()
    fecha_ven = hoy + timedelta(days=30)

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
        "fecha_ven":     fecha_ven.strftime("%d/%m/%Y"),
        "fecha_exp_obj": hoy,
        "fecha_ven_obj": fecha_ven,
    }

    try:
        await message.answer(
            f"🔄 Generando documentación...\n"
            f"<b>Folio:</b> {folio}\n"
            f"<b>Titular:</b> {datos['nombre']}",
            parse_mode="HTML"
        )

        pdf_unificado = generar_pdf_unificado(datos_pdf)

        keyboard = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🔑 Validar Admin",  callback_data=f"validar_{folio}"),
            InlineKeyboardButton(text="⏹️ Detener Timer", callback_data=f"detener_{folio}")
        ]])

        if pdf_unificado:
            await message.answer_document(
                FSInputFile(pdf_unificado),
                caption=(
                    f"📋 PERMISO - GUERRERO\n"
                    f"Folio: {folio}\n"
                    f"Titular: {datos['nombre']}\n"
                    f"RFC: {datos['rfc']}\n"
                    f"Domicilio: {domicilio}\n"
                    f"Vigencia: 30 días | Costo: ${datos['costo']}\n\n"
                    f"✅ Permiso + Comprobante (PDF unificado)\n\n"
                    f"⏰ TIMER ACTIVO — 36 horas"
                ),
                reply_markup=keyboard
            )
        else:
            await message.answer("❌ Error al generar el PDF. Intenta de nuevo con /chuleta")
            await state.clear()
            return

        # Guardar en Supabase
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
            "fecha_vencimiento": fecha_ven.date().isoformat(),
            "entidad":           "Guerrero",
            "estado":            "PENDIENTE",
            "user_id":           message.from_user.id,
            "username":          message.from_user.username or "Sin username"
        }).execute()

        await iniciar_timer_eliminacion(message.from_user.id, folio)

        await message.answer(
            f"💰 INSTRUCCIONES DE PAGO\n\n"
            f"📄 Folio: {folio}\n"
            f"💵 Monto: ${datos['costo']}\n"
            f"⏰ Tiempo límite: 36 horas\n\n"
            f"🏦 TRANSFERENCIA:\n"
            f"• Titular: GUILLERMO S.R.J\n"
            f"• Número: 7289690000484424454\n\n"
            f"🏪 OXXO:\n"
            f"• Referencia: 2242170180214090\n"
            f"• Monto: ${datos['costo']}\n\n"
            f"📸 Envía la foto del comprobante para validar.\n"
            f"⚠️ Si no pagas en 36 horas, el folio se elimina.\n\n"
            f"📋 Para generar otro permiso use /chuleta"
        )

    except Exception as e:
        await message.answer(f"❌ Error: {str(e)}\n\n📋 Para generar otro permiso use /chuleta")
        print(f"Error en get_domicilio: {e}")
    finally:
        await state.clear()

# ------------ CALLBACKS ------------
@dp.callback_query(lambda c: c.data and c.data.startswith("validar_"))
async def callback_validar_admin(callback: CallbackQuery):
    folio = callback.data.replace("validar_", "")
    if not folio.startswith("Z"):
        await callback.answer("❌ Folio inválido para Guerrero", show_alert=True)
        return
    if folio in timers_activos:
        user_con_folio = timers_activos[folio]["user_id"]
        cancelar_timer_folio(folio)
        try:
            supabase.table("folios_registrados").update({
                "estado": "VALIDADO_ADMIN",
                "fecha_comprobante": datetime.now().isoformat()
            }).eq("folio", folio).execute()
        except Exception as e:
            print(f"Error BD validar: {e}")
        await callback.answer("✅ Folio validado por administración", show_alert=True)
        await callback.message.edit_reply_markup(reply_markup=None)
        try:
            await bot.send_message(
                user_con_folio,
                f"✅ PAGO VALIDADO - GUERRERO\nFolio: {folio}\n"
                f"Tu permiso está activo.\n\n📋 Para generar otro permiso use /chuleta"
            )
        except Exception as e:
            print(f"Error notif usuario: {e}")
    else:
        await callback.answer("❌ Folio no encontrado en timers activos", show_alert=True)

@dp.callback_query(lambda c: c.data and c.data.startswith("detener_"))
async def callback_detener_timer(callback: CallbackQuery):
    folio = callback.data.replace("detener_", "")
    if folio in timers_activos:
        cancelar_timer_folio(folio)
        try:
            supabase.table("folios_registrados").update({
                "estado": "TIMER_DETENIDO",
                "fecha_detencion": datetime.now().isoformat()
            }).eq("folio", folio).execute()
        except Exception as e:
            print(f"Error BD detener: {e}")
        await callback.answer("⏹️ Timer detenido", show_alert=True)
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.answer(
            f"⏹️ TIMER DETENIDO\nFolio: {folio}\n\n📋 Para generar otro permiso use /chuleta"
        )
    else:
        await callback.answer("❌ Timer ya no está activo", show_alert=True)

# ------------ ADMIN SERO ------------
@dp.message(lambda m: m.text and m.text.upper().startswith("SERO") and len(m.text) > 4)
async def comando_admin_sero(message: types.Message):
    folio_admin = message.text.upper()[4:].strip()
    if not folio_admin.startswith("Z"):
        await message.answer(
            f"❌ El folio {folio_admin} no es GUERRERO (debe comenzar con Z)\n\n"
            f"📋 Para generar otro permiso use /chuleta"
        )
        return
    if folio_admin in timers_activos:
        user_con_folio = timers_activos[folio_admin]["user_id"]
        cancelar_timer_folio(folio_admin)
        try:
            supabase.table("folios_registrados").update({
                "estado": "VALIDADO_ADMIN",
                "fecha_comprobante": datetime.now().isoformat()
            }).eq("folio", folio_admin).execute()
        except Exception as e:
            print(f"Error BD SERO: {e}")
        await message.answer(
            f"✅ VALIDACIÓN OK\nFolio: {folio_admin}\n\n📋 Para generar otro permiso use /chuleta"
        )
        try:
            await bot.send_message(
                user_con_folio,
                f"✅ PAGO VALIDADO - GUERRERO\nFolio: {folio_admin}\n"
                f"Tu permiso está activo.\n\n📋 Para generar otro permiso use /chuleta"
            )
        except Exception as e:
            print(f"Error notif SERO: {e}")
    else:
        await message.answer(
            f"❌ FOLIO NO ENCONTRADO EN TIMERS\nFolio: {folio_admin}\n\n"
            f"📋 Para generar otro permiso use /chuleta"
        )

# ------------ COMPROBANTE ------------
@dp.message(lambda m: m.content_type == ContentType.PHOTO)
async def recibir_comprobante(message: types.Message):
    try:
        user_id = message.from_user.id
        folios_usuario = obtener_folios_usuario(user_id)
        if not folios_usuario:
            await message.answer(
                "ℹ️ No hay trámites pendientes.\n\n📋 Para generar otro permiso use /chuleta"
            )
            return
        if len(folios_usuario) > 1:
            lista = "\n".join([f"• {f}" for f in folios_usuario])
            pending_comprobantes[user_id] = "waiting_folio"
            await message.answer(
                f"📄 Tienes varios folios activos:\n\n{lista}\n\n"
                f"Responde con el NÚMERO DE FOLIO al que corresponde este comprobante.\n\n"
                f"📋 Para generar otro permiso use /chuleta"
            )
            return
        folio = folios_usuario[0]
        cancelar_timer_folio(folio)
        try:
            supabase.table("folios_registrados").update({
                "estado": "COMPROBANTE_ENVIADO",
                "fecha_comprobante": datetime.now().isoformat()
            }).eq("folio", folio).execute()
        except Exception as e:
            print(f"Error BD comprobante: {e}")
        await message.answer(
            f"✅ Comprobante recibido.\n📄 Folio: {folio}\n⏹️ Timer detenido.\n\n"
            f"📋 Para generar otro permiso use /chuleta"
        )
    except Exception as e:
        print(f"[ERROR] recibir_comprobante: {e}")
        await message.answer("❌ Error procesando comprobante. Intenta de nuevo.\n\n📋 /chuleta")

@dp.message(lambda m: m.from_user.id in pending_comprobantes and pending_comprobantes[m.from_user.id] == "waiting_folio")
async def especificar_folio_comprobante(message: types.Message):
    try:
        user_id  = message.from_user.id
        folio_e  = message.text.strip().upper()
        folios_u = obtener_folios_usuario(user_id)
        if folio_e not in folios_u:
            await message.answer(
                "❌ Ese folio no está en tu lista activa.\n\n📋 Para generar otro permiso use /chuleta"
            )
            return
        cancelar_timer_folio(folio_e)
        del pending_comprobantes[user_id]
        try:
            supabase.table("folios_registrados").update({
                "estado": "COMPROBANTE_ENVIADO",
                "fecha_comprobante": datetime.now().isoformat()
            }).eq("folio", folio_e).execute()
        except Exception as e:
            print(f"Error BD especificar: {e}")
        await message.answer(
            f"✅ Comprobante asociado.\n📄 Folio: {folio_e}\n⏹️ Timer detenido.\n\n"
            f"📋 Para generar otro permiso use /chuleta"
        )
    except Exception as e:
        print(f"[ERROR] especificar_folio: {e}")
        if message.from_user.id in pending_comprobantes:
            del pending_comprobantes[message.from_user.id]
        await message.answer("❌ Error. Intenta de nuevo.\n\n📋 /chuleta")

@dp.message(Command("folios"))
async def ver_folios_activos(message: types.Message):
    try:
        user_id = message.from_user.id
        folios_usuario = obtener_folios_usuario(user_id)
        if not folios_usuario:
            await message.answer(
                "ℹ️ No hay folios activos.\n\n📋 Para generar otro permiso use /chuleta"
            )
            return
        lista = []
        for f in folios_usuario:
            if f in timers_activos:
                seg = max(0, int(TOTAL_MINUTOS_TIMER * 60 -
                                 (datetime.now() - timers_activos[f]["start_time"]).total_seconds()))
                h, m = divmod(seg // 60, 60)
                lista.append(f"• {f} ({h}h {m}min restantes)")
            else:
                lista.append(f"• {f} (sin timer)")
        await message.answer(
            f"📋 FOLIOS GUERRERO ACTIVOS ({len(folios_usuario)})\n\n"
            + "\n".join(lista)
            + "\n\n⏰ Timer 36h | 📸 Comprobante: envía imagen\n\n"
            f"📋 Para generar otro permiso use /chuleta"
        )
    except Exception as e:
        print(f"[ERROR] ver_folios: {e}")
        await message.answer("❌ Error consultando folios.\n\n📋 /chuleta")

@dp.message(lambda m: m.text and any(p in m.text.lower() for p in
        ["costo","precio","cuanto","cuánto","deposito","depósito","pago","valor","monto"]))
async def responder_costo(message: types.Message):
    await message.answer(
        "💰 El costo se define al generar el permiso.\n\n📋 Use /chuleta para iniciar."
    )

@dp.message()
async def fallback(message: types.Message):
    await message.answer("🏛️ Sistema Digital Guerrero.")

# ============ FASTAPI ============
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
            webhook_url = f"{BASE_URL}/webhook"
            await bot.set_webhook(webhook_url, allowed_updates=["message", "callback_query"])
            print(f"[WEBHOOK] {webhook_url}")
            _keep_task = asyncio.create_task(keep_alive())
        else:
            print("[POLLING] Sin webhook")
        print("[SISTEMA] Guerrero v6.0 iniciado")
        yield
    except Exception as e:
        print(f"[ERROR CRITICO] {e}")
        yield
    finally:
        print("[CIERRE] Cerrando...")
        if _keep_task:
            _keep_task.cancel()
            with suppress(asyncio.CancelledError):
                await _keep_task
        await bot.session.close()

app = FastAPI(lifespan=lifespan, title="Sistema Guerrero Digital", version="6.0")

@app.post("/webhook")
async def telegram_webhook(request: Request):
    try:
        data   = await request.json()
        update = types.Update(**data)
        await dp.feed_webhook_update(bot, update)
        return {"ok": True}
    except Exception as e:
        print(f"[ERROR] webhook: {e}")
        return {"ok": False, "error": str(e)}

# ============ RUTA DE CONSULTA ============
@app.get("/consulta/{folio}", response_class=HTMLResponse)
async def consulta_permiso(folio: str):
    """Verificador de permiso — diseño LTM original."""

    BANNER = "https://direcciontransitotlapadecomonfort.gob.mx/img/transformando_guerrero.jpg"
    CSS    = "https://direcciontransitotlapadecomonfort.gob.mx/css/bootstrap.min.css"
    JQ     = "https://direcciontransitotlapadecomonfort.gob.mx/js/jquery-3.4.1.min.js"
    BS_JS  = "https://direcciontransitotlapadecomonfort.gob.mx/js/bootstrap.min.js"
    FAV    = "https://direcciontransitotlapadecomonfort.gob.mx/img/favicon-96x96.png?v2"

    def base_html(body_content: str) -> str:
        return f"""<!doctype html>
<html lang="es">
<head>
  <meta http-equiv="Content-Type" content="text/html; charset=UTF-8">
  <title>LTM | Licencias de Tránsito Municipal</title>
  <meta http-equiv="X-UA-Compatible" content="IE=edge">
  <meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1">
  <link rel="icon" href="{FAV}">
  <link href="https://fonts.googleapis.com/css?family=Nunito:200,600" rel="stylesheet">
  <link href="{CSS}" rel="stylesheet">
  <style>
    #ssp  {{ width:300px; margin:0 auto; border-radius:5px; }}
    #ltm  {{ color:#636b6f; font-family:'Nunito',sans-serif; font-weight:200; font-size:50px; text-align:center; }}
    body  {{ background:url(https://direcciontransitotlapadecomonfort.gob.mx/img/intersection.png); }}
    .etiqueta {{ margin:0; padding:3px 10px 3px 0; text-align:right; font-weight:bold; }}
    .valor    {{ margin:0; padding:3px 0;           text-align:left; }}
    #logo-y {{ width:110px; margin:-15px auto 0; border-radius:5px; }}
    #logo-x {{ width:70px;  margin:-15px auto 0; border-radius:5px; }}
  </style>
</head>
<body>
  <div class="container">
    <div class="col-md-8 col-md-offset-2 col-sm-10 col-sm-offset-1"
         style="padding:0; margin-top:3em;">
      <div class="row" style="margin-bottom:1em;">
        <div class="col-md-12">
          <img id="ssp" src="{BANNER}" class="img-responsive" alt="">
        </div>
      </div>
      {body_content}
      <div class="col-md-12 text-center text-right">
        <div class="col-md-6 col-xs-6" style="text-align:right;">
          <img id="logo-y" src="{BANNER}" class="img-responsive"
               title="Licencia de tránsito Municipal Tlapa de Comonfort, Guerrero.">
        </div>
        <div class="col-md-6 col-xs-6" style="text-align:left;">
          <img id="logo-x" src="{FAV}" class="img-responsive">
        </div>
      </div>
      <div class="clearfix"></div>
      <div id="ltm"
           onclick="window.location.assign('https://direcciontransitotlapadecomonfort.gob.mx');"
           style="cursor:pointer;">
        LTM | Tlapa de Comonfort Gro.
      </div>
    </div>
  </div>
  <script src="{JQ}"></script>
  <script src="{BS_JS}"></script>
</body>
</html>"""

    # ---- consulta DB ----
    try:
        result = supabase.table("folios_registrados").select("*").eq("folio", folio).execute()
    except Exception as e:
        return HTMLResponse(content=base_html(
            f'<div class="alert alert-danger">Error de base de datos: {e}</div>'
        ), status_code=500)

    if not result.data:
        not_found = f"""
<div class="panel panel-danger">
  <div class="panel-heading"><div class="panel-title">Verificador de Permiso</div></div>
  <div class="panel-body" style="padding:20px 10px;">
    <div class="alert alert-danger">
      El folio <strong>{folio}</strong> no fue encontrado en el sistema.
    </div>
  </div>
  <div class="panel-footer"><div class="text-center">Permiso de Tr&aacute;nsito Municipal</div></div>
</div>"""
        return HTMLResponse(content=base_html(not_found), status_code=404)

    d = result.data[0]

    def fmt_date(s):
        try:
            from datetime import datetime as _dt
            return _dt.strptime(s, "%Y-%m-%d").strftime("%d-%m-%Y")
        except:
            return s or "N/D"

    marca         = d.get("marca",         "N/D")
    linea         = d.get("linea",         "N/D")
    anio          = d.get("anio",          "N/D")
    serie         = d.get("numero_serie",  "N/D")
    motor         = d.get("numero_motor",  "N/D")
    color         = d.get("color",         "N/D")
    contribuyente = d.get("nombre",        "N/D")
    fecha_exp     = fmt_date(d.get("fecha_expedicion",  ""))
    fecha_ven     = fmt_date(d.get("fecha_vencimiento", ""))

    def row(label, value):
        return f"""
<div class="row">
  <div class="col-xs-5 etiqueta">{label}:</div>
  <div class="col-xs-7 valor">{value}</div>
</div>"""

    panel = f"""
<div class="panel panel-primary">
  <div class="panel-heading"><div class="panel-title">Verificador de Permiso</div></div>
  <div style="padding:20px 10px 20px 10px;" class="panel-body">
    <div class="col-lg-12 text-center" style="border:0; padding:0; margin:0;">
      <div class="row">
        <div class="col-xs-12" style="font-size:13px; margin-left:-25px;">
          {row("Marca", marca)}
          {row("L&iacute;nea", linea)}
          {row("Modelo", anio)}
          {row("Color", color)}
          {row("N&uacute;mero de Serie", serie)}
          {row("N&uacute;mero de Motor", motor)}
          {row("CONTRIBUYENTE", contribuyente)}
          {row("Fecha Expedici&oacute;n", fecha_exp)}
          {row("Fecha Vencimiento", fecha_ven)}
        </div>
      </div>
    </div>
  </div>
  <div class="panel-footer">
    <div class="text-center">Permiso de Tr&aacute;nsito Municipal</div>
  </div>
</div>"""

    return HTMLResponse(content=base_html(panel))

@app.get("/")
async def health():
    return {
        "ok": True,
        "version": "6.0 - PDF Unificado + Nuevos Campos",
        "entidad": "Guerrero",
        "active_timers": len(timers_activos),
        "prefijo_folio": "ZY",
        "nuevos_campos": ["costo", "rfc", "domicilio"],
        "pdf": "unificado (permiso pag1 + recibo pag2)",
        "qr_size": "97x97 px (reducido 25%)",
        "consulta": "/consulta/{folio}",
    }

@app.get("/status")
async def status():
    return {
        "sistema": "Guerrero v6.0",
        "timers_activos": len(timers_activos),
        "folios": list(timers_activos.keys()),
        "timestamp": datetime.now().isoformat(),
    }

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    print(f"[ARRANQUE] Puerto {port} | Guerrero v6.0")
    uvicorn.run(app, host="0.0.0.0", port=port)
