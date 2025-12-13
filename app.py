from fastapi import FastAPI, Request
from aiogram import Bot, Dispatcher, types
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.filters import Command
from aiogram.types import FSInputFile, ContentType
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timedelta
from supabase import create_client, Client
import asyncio
import os
import fitz
import pytz
from PIL import Image
import random
from io import BytesIO
import qrcode
import re

# ------------ CONFIG ------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")
BASE_URL = os.getenv("BASE_URL", "").rstrip("/")
URL_CONSULTA_BASE = "https://tlapadecomonfortexpediciondepermisosgob2.onrender.com"
OUTPUT_DIR = "documentos"
PLANTILLA_PDF = "Guerrero.pdf"
PLANTILLA_FLASK = "recibo_permiso_guerrero_img.pdf"

PRECIO_PERMISO = 50

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
                f"El folio {folio} ha sido eliminado del sistema por no completar el pago en 36 horas.\n\n"
                f"Para iniciar un nuevo trámite use /chuleta"
            )
        
        limpiar_timer_folio(folio)
            
    except Exception as e:
        print(f"Error eliminando folio {folio}: {e}")

async def enviar_recordatorio(folio: str, minutos_restantes: int):
    try:
        if folio not in timers_activos:
            return
            
        user_id = timers_activos[folio]["user_id"]
        
        await bot.send_message(
            user_id,
            f"⚡ RECORDATORIO DE PAGO - GUERRERO\n\n"
            f"Folio: {folio}\n"
            f"Tiempo restante: {minutos_restantes} minutos\n"
            f"Monto: ${PRECIO_PERMISO}\n\n"
            f"📸 Envíe su comprobante de pago (imagen) para validar el trámite."
        )
    except Exception as e:
        print(f"Error enviando recordatorio para folio {folio}: {e}")

async def iniciar_timer_eliminacion(user_id: int, folio: str):
    async def timer_task():
        print(f"[TIMER] Iniciado para folio {folio}, usuario {user_id} (36 horas)")
        
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
            print(f"[TIMER] Expirado para folio {folio} - eliminando")
            await eliminar_folio_automatico(folio)
    
    task = asyncio.create_task(timer_task())
    timers_activos[folio] = {
        "task": task,
        "user_id": user_id,
        "start_time": datetime.now()
    }
    
    if user_id not in user_folios:
        user_folios[user_id] = []
    user_folios[user_id].append(folio)
    
    print(f"[SISTEMA] Timer 36h iniciado para folio {folio}, total timers: {len(timers_activos)}")

def cancelar_timer_folio(folio: str):
    if folio in timers_activos:
        timers_activos[folio]["task"].cancel()
        user_id = timers_activos[folio]["user_id"]
        
        del timers_activos[folio]
        
        if user_id in user_folios and folio in user_folios[user_id]:
            user_folios[user_id].remove(folio)
            if not user_folios[user_id]:
                del user_folios[user_id]
        
        print(f"[SISTEMA] Timer cancelado para folio {folio}")

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

# ---------------- COORDENADAS GUERRERO ----------------
coords_guerrero = {
    "folio": (376,769,8,(1,0,0)),
    "fecha_exp": (122,755,8,(0,0,0)),
    "fecha_ven": (122,768,8,(0,0,0)),
    "serie": (376,742,8,(0,0,0)),
    "motor": (376,729,8,(0,0,0)),
    "marca": (376,700,8,(0,0,0)),
    "linea": (376,714,8,(0,0,0)),
    "color": (376,756,8,(0,0,0)),
    "nombre": (122,700,8,(0,0,0)),
    "anio": (0,0,8,(0,0,0)),
    "rot_folio": (440,200,83,(0,0,0)),
    "rot_fecha_exp": (77,205,8,(0,0,0)),
    "rot_fecha_ven": (63,205,8,(0,0,0)),
    "rot_serie": (168,110,18,(0,0,0)),
    "rot_motor": (224,110,18,(0,0,0)),
    "rot_marca": (280,110,18,(0,0,0)),
    "rot_linea": (280,340,18,(0,0,0)),
    "rot_anio": (305,530,18,(0,0,0)),
    "rot_color": (224,410,18,(0,0,0)),
    "rot_nombre": (115,205,8,(0,0,0))
}

# ------------ FUNCIÓN GENERAR FOLIO GUERRERO ------------
def generar_folio_guerrero():
    letras = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    prefijo = "ZA"
    inicio_num = 1245
    max_intentos = 100000

    try:
        existentes = supabase.table("folios_registrados").select("folio").eq("entidad", "Guerrero").execute().data
        usados = set([r["folio"] for r in existentes if r["folio"] and len(r["folio"]) == 6 and r["folio"][:2] == prefijo])
    except Exception as e:
        print(f"Error consultando folios: {e}")
        usados = set()

    for intento in range(max_intentos):
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
    marca = State()
    linea = State()
    anio = State()
    serie = State()
    motor = State()
    color = State()
    nombre = State()

def generar_qr_dinamico_guerrero(folio):
    try:
        url_directa = f"{URL_CONSULTA_BASE}/consulta/{folio}"
        
        qr = qrcode.QRCode(
            version=2,
            error_correction=qrcode.constants.ERROR_CORRECT_M,
            box_size=4,
            border=1
        )
        qr.add_data(url_directa)
        qr.make(fit=True)

        img_qr = qr.make_image(fill_color="black", back_color="white").convert("RGB")
        print(f"[QR GUERRERO] Generado para folio {folio} -> {url_directa}")
        return img_qr, url_directa
        
    except Exception as e:
        print(f"[ERROR QR GUERRERO] {e}")
        return None, None

# ============ GENERACIÓN DE 2 PDFs SEPARADOS ============
def generar_pdf_principal(datos: dict) -> str:
    """Genera el PDF principal de Guerrero (Guerrero.pdf)"""
    fol = datos["folio"]
    
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    filename = f"{OUTPUT_DIR}/{fol}_guerrero.pdf"
    
    try:
        doc = fitz.open(PLANTILLA_PDF)
        pg = doc[0]

        for campo in ["folio", "fecha_exp", "fecha_ven", "serie", "motor", "marca", "linea", "color", "nombre"]:
            if campo in coords_guerrero and campo in datos:
                x, y, s, col = coords_guerrero[campo]
                texto = datos.get(campo, "")
                pg.insert_text((x, y), str(texto), fontsize=s, color=col)

        pg.insert_text(coords_guerrero["rot_folio"][:2], fol, fontsize=coords_guerrero["rot_folio"][2], rotate=270)
        pg.insert_text(coords_guerrero["rot_fecha_exp"][:2], datos["fecha_exp"], fontsize=coords_guerrero["rot_fecha_exp"][2], rotate=270)
        pg.insert_text(coords_guerrero["rot_fecha_ven"][:2], datos["fecha_ven"], fontsize=coords_guerrero["rot_fecha_ven"][2], rotate=270)
        pg.insert_text(coords_guerrero["rot_serie"][:2], datos["serie"], fontsize=coords_guerrero["rot_serie"][2], rotate=270)
        pg.insert_text(coords_guerrero["rot_motor"][:2], datos["motor"], fontsize=coords_guerrero["rot_motor"][2], rotate=270)
        pg.insert_text(coords_guerrero["rot_marca"][:2], datos["marca"], fontsize=coords_guerrero["rot_marca"][2], rotate=270)
        pg.insert_text(coords_guerrero["rot_linea"][:2], datos["linea"], fontsize=coords_guerrero["rot_linea"][2], rotate=270)
        pg.insert_text(coords_guerrero["rot_anio"][:2], datos["anio"], fontsize=coords_guerrero["rot_anio"][2], rotate=270)
        pg.insert_text(coords_guerrero["rot_color"][:2], datos["color"], fontsize=coords_guerrero["rot_color"][2], rotate=270)
        pg.insert_text(coords_guerrero["rot_nombre"][:2], datos["nombre"], fontsize=coords_guerrero["rot_nombre"][2], rotate=270)

        # QR dinámico
        img_qr, url_qr = generar_qr_dinamico_guerrero(fol)
        
        if img_qr:
            buf = BytesIO()
            img_qr.save(buf, format="PNG")
            buf.seek(0)
            qr_pix = fitz.Pixmap(buf.read())

            x_qr = 80
            y_qr = 430
            ancho_qr = 130
            alto_qr = 130

            pg.insert_image(
                fitz.Rect(x_qr, y_qr, x_qr + ancho_qr, y_qr + alto_qr),
                pixmap=qr_pix,
                overlay=True
            )
            print(f"[QR GUERRERO] Insertado en PDF principal")

        doc.save(filename)
        doc.close()
        
        print(f"[PDF PRINCIPAL GUERRERO] ✅ Generado: {filename}")
        return filename
        
    except Exception as e:
        print(f"[ERROR] Generando PDF principal GUERRERO: {e}")
        return ""

def generar_pdf_recibo(datos: dict) -> str:
    """Genera el PDF tipo recibo (recibo_permiso_guerrero_img.pdf)"""
    fol = datos["folio"]
    
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    filename = f"{OUTPUT_DIR}/{fol}_recibo.pdf"
    
    try:
        doc = fitz.open(PLANTILLA_FLASK)
        page = doc[0]
        
        fecha_exp_dt = datos["fecha_exp_obj"]
        fecha_ven_dt = datos["fecha_ven_obj"]
        
        page.insert_text((700, 1750), fol, fontsize=100, fontname="helv")
        page.insert_text((2200, 1750), fecha_exp_dt.strftime('%d/%m/%Y'), fontsize=100, fontname="helv")
        page.insert_text((4000, 1750), fecha_ven_dt.strftime('%d/%m/%Y'), fontsize=100, fontname="helv")
        page.insert_text((950, 1930), datos["nombre"].upper(), fontsize=100, fontname="helv")
        
        doc.save(filename)
        doc.close()
        
        print(f"[PDF RECIBO GUERRERO] ✅ Generado: {filename}")
        return filename
        
    except Exception as e:
        print(f"[ERROR] Generando PDF recibo GUERRERO: {e}")
        return ""

# ------------ HANDLERS ------------
@dp.message(Command("start"))
async def start_cmd(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "🏛️ SISTEMA DIGITAL DEL ESTADO DE GUERRERO\n\n"
        f"💰 Costo: ${PRECIO_PERMISO}\n"
        "⏰ Tiempo límite: 36 horas\n\n"
        "⚠️ IMPORTANTE: Su folio será eliminado automáticamente si no realiza el pago dentro del tiempo límite"
    )

@dp.message(Command("chuleta"))
async def chuleta_cmd(message: types.Message, state: FSMContext):
    folios_activos = obtener_folios_usuario(message.from_user.id)
    mensaje_folios = ""
    if folios_activos:
        mensaje_folios = f"\n\n📋 FOLIOS ACTIVOS: {', '.join(folios_activos)}\n(Cada folio tiene su propio timer de 36 horas)"
    
    await message.answer(
        f"🚗 NUEVO PERMISO - GUERRERO\n\n"
        f"💰 Costo: ${PRECIO_PERMISO}\n"
        f"⏰ Plazo de pago: 36 horas"
        f"{mensaje_folios}\n\n"
        f"Primer paso: MARCA del vehículo:"
    )
    await state.set_state(PermisoForm.marca)

@dp.message(PermisoForm.marca)
async def get_marca(message: types.Message, state: FSMContext):
    marca = message.text.strip().upper()
    if not marca or len(marca) < 2:
        await message.answer("⚠️ Proporcione una MARCA válida (mínimo 2 caracteres):")
        return
    await state.update_data(marca=marca)
    await message.answer("LÍNEA/MODELO del vehículo:")
    await state.set_state(PermisoForm.linea)

@dp.message(PermisoForm.linea)
async def get_linea(message: types.Message, state: FSMContext):
    linea = message.text.strip().upper()
    if not linea:
        await message.answer("⚠️ Proporcione la LÍNEA/MODELO:")
        return
    await state.update_data(linea=linea)
    await message.answer("AÑO del vehículo (4 dígitos):")
    await state.set_state(PermisoForm.anio)

@dp.message(PermisoForm.anio)
async def get_anio(message: types.Message, state: FSMContext):
    anio = message.text.strip()
    if not anio.isdigit() or len(anio) != 4:
        await message.answer("⚠️ Formato inválido. Use 4 dígitos (ej. 2021):")
        return
    await state.update_data(anio=anio)
    await message.answer("NÚMERO DE SERIE:")
    await state.set_state(PermisoForm.serie)

@dp.message(PermisoForm.serie)
async def get_serie(message: types.Message, state: FSMContext):
    serie = message.text.strip().upper()
    if len(serie) < 5 or len(serie) > 25:
        await message.answer("⚠️ Serie inválida (5 a 25 caracteres):")
        return
    await state.update_data(serie=serie)
    await message.answer("NÚMERO DE MOTOR:")
    await state.set_state(PermisoForm.motor)

@dp.message(PermisoForm.motor)
async def get_motor(message: types.Message, state: FSMContext):
    motor = message.text.strip().upper()
    if len(motor) < 5 or len(motor) > 25:
        await message.answer("⚠️ Motor inválido (5 a 25 caracteres):")
        return
    await state.update_data(motor=motor)
    await message.answer("COLOR del vehículo:")
    await state.set_state(PermisoForm.color)

@dp.message(PermisoForm.color)
async def get_color(message: types.Message, state: FSMContext):
    color = message.text.strip().upper()
    if not color or len(color) > 20:
        await message.answer("⚠️ Color inválido (máx. 20 caracteres):")
        return
    await state.update_data(color=color)
    await message.answer("NOMBRE COMPLETO del propietario:")
    await state.set_state(PermisoForm.nombre)

@dp.message(PermisoForm.nombre)
async def get_nombre(message: types.Message, state: FSMContext):
    datos = await state.get_data()
    nombre = message.text.strip().upper()

    if len(nombre) < 5 or len(nombre) > 60 or len(nombre.split()) < 2:
        await message.answer("⚠️ Nombre completo inválido (mínimo nombre y apellido, máx. 60 caracteres):")
        return

    folio = generar_folio_guerrero()
    
    hoy = datetime.now()
    vigencia_dias = 30
    fecha_ven = hoy + timedelta(days=vigencia_dias)

    datos_pdf = {
        "folio": folio,
        "marca": datos["marca"],
        "linea": datos["linea"],
        "anio": datos["anio"],
        "serie": datos["serie"],
        "motor": datos["motor"],
        "color": datos["color"],
        "nombre": nombre,
        "fecha_exp": hoy.strftime("%d/%m/%Y"),
        "fecha_ven": fecha_ven.strftime("%d/%m/%Y"),
        "fecha_exp_obj": hoy,
        "fecha_ven_obj": fecha_ven
    }

    try:
        await message.answer(
            f"🔄 Generando documentación...\n"
            f"<b>Folio:</b> {folio}\n"
            f"<b>Titular:</b> {nombre}",
            parse_mode="HTML"
        )

        # Generar 2 PDFs SEPARADOS
        pdf_principal = generar_pdf_principal(datos_pdf)
        pdf_recibo = generar_pdf_recibo(datos_pdf)

        if pdf_principal:
            await message.answer_document(
                FSInputFile(pdf_principal),
                caption=f"📋 PERMISO DE CIRCULACIÓN - GUERRERO\nFolio: {folio}\nVigencia: 30 días\n\n✅ Documento principal con QR"
            )

        if pdf_recibo:
            await message.answer_document(
                FSInputFile(pdf_recibo),
                caption=f"🧾 COMPROBANTE DE VERIFICACIÓN\nFolio: {folio}\n\n📋 Recibo oficial"
            )

        supabase.table("folios_registrados").insert({
            "folio": folio,
            "marca": datos["marca"],
            "linea": datos["linea"],
            "anio": datos["anio"],
            "numero_serie": datos["serie"],
            "numero_motor": datos["motor"],
            "color": datos["color"],
            "nombre": nombre,
            "fecha_expedicion": hoy.date().isoformat(),
            "fecha_vencimiento": fecha_ven.date().isoformat(),
            "entidad": "Guerrero",
            "estado": "PENDIENTE",
            "user_id": message.from_user.id,
            "username": message.from_user.username or "Sin username"
        }).execute()
        
        await iniciar_timer_eliminacion(message.from_user.id, folio)

        await message.answer(
            "💰 INSTRUCCIONES DE PAGO\n\n"
            f"📄 Folio: {folio}\n"
            f"💵 Monto: ${PRECIO_PERMISO}\n"
            "⏰ Tiempo límite: 36 horas\n\n"
            "🏦 TRANSFERENCIA:\n"
            "• Titular: GUILLERMO S.R.J\n"
            "• Número: 7289690000484424454\n\n"
            "🏪 OXXO:\n"
            "• Referencia: 2242170180214090\n"
            f"• Monto: ${PRECIO_PERMISO}\n\n"
            "📸 Envía la foto del comprobante para validar.\n"
            "⚠️ Si no pagas en 36 horas, el folio se elimina automáticamente.\n\n"
            "📋 Para generar otro permiso use /chuleta"
        )

    except Exception as e:
        await message.answer(f"❌ Error generando documentación: {str(e)}")
        print(f"Error: {e}")
    finally:
        await state.clear()

@dp.message(lambda m: m.text and m.text.upper().startswith("SERO") and len(m.text) > 4)
async def comando_admin_sero(message: types.Message):
    texto = message.text.upper()
    folio_admin = texto[4:].strip()
    
    if not folio_admin.startswith("Z"):
        await message.answer(
            f"❌ FOLIO INVÁLIDO\n"
            f"El folio {folio_admin} no es GUERRERO.\n"
            f"Debe comenzar con Z (ej: ZA1245)"
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
            print(f"Error actualizando BD para folio {folio_admin}: {e}")
        
        await message.answer(
            f"✅ VALIDACIÓN ADMINISTRATIVA OK\n"
            f"Folio: {folio_admin}\n"
            f"Timer cancelado y estado actualizado."
        )
        
        try:
            await bot.send_message(
                user_con_folio,
                f"✅ PAGO VALIDADO POR ADMINISTRACIÓN - GUERRERO\n"
                f"Folio: {folio_admin}\n"
                f"Tu permiso está activo para circular."
            )
        except Exception as e:
            print(f"Error notificando al usuario {user_con_folio}: {e}")
    else:
        await message.answer(
            f"❌ FOLIO NO LOCALIZADO EN TIMERS ACTIVOS\n"
            f"Folio consultado: {folio_admin}"
        )

@dp.message(lambda m: m.content_type == ContentType.PHOTO)
async def recibir_comprobante(message: types.Message):
    try:
        user_id = message.from_user.id
        folios_usuario = obtener_folios_usuario(user_id)
        
        if not folios_usuario:
            await message.answer(
                "ℹ️ No hay trámites pendientes de pago.\n"
                "Para iniciar uno nuevo usa /chuleta"
            )
            return
        
        if len(folios_usuario) > 1:
            lista_folios = '\n'.join([f"• {folio}" for folio in folios_usuario])
            pending_comprobantes[user_id] = "waiting_folio"
            await message.answer(
                f"📄 Tienes varios folios activos:\n\n{lista_folios}\n\n"
                f"Responde con el NÚMERO DE FOLIO al que corresponde este comprobante."
            )
            return
        
        folio = folios_usuario[0]
        cancelar_timer_folio(folio)
        
        try:
            supabase.table("folios_registrados").update({
                "estado": "COMPROBANTE_ENVIADO",
                "fecha_comprobante": datetime.now().isoformat()
            }).eq("folio", folio).execute()
            await message.answer(
                f"✅ Comprobante recibido.\n"
                f"📄 Folio: {folio}\n"
                f"⏹️ Timer detenido.\n\n"
                f"📋 Para generar otro permiso use /chuleta"
            )
        except Exception as e:
            print(f"Error actualizando estado comprobante: {e}")
            await message.answer(
                f"✅ Comprobante recibido.\n"
                f"📄 Folio: {folio}\n"
                f"⏹️ Timer detenido.\n\n"
                f"📋 Para generar otro permiso use /chuleta"
            )
            
    except Exception as e:
        print(f"[ERROR] recibir_comprobante: {e}")
        await message.answer("❌ Error procesando el comprobante. Intenta enviar la foto nuevamente.")

@dp.message(lambda message: message.from_user.id in pending_comprobantes and pending_comprobantes[message.from_user.id] == "waiting_folio")
async def especificar_folio_comprobante(message: types.Message):
    try:
        user_id = message.from_user.id
        folio_especificado = message.text.strip().upper()
        folios_usuario = obtener_folios_usuario(user_id)
        
        if folio_especificado not in folios_usuario:
            await message.answer(
                "❌ Ese folio no está entre tus expedientes activos.\n"
                "Responde con uno de tu lista actual."
            )
            return
        
        cancelar_timer_folio(folio_especificado)
        del pending_comprobantes[user_id]
        
        try:
            supabase.table("folios_registrados").update({
                "estado": "COMPROBANTE_ENVIADO",
                "fecha_comprobante": datetime.now().isoformat()
            }).eq("folio", folio_especificado).execute()
            await message.answer(
                f"✅ Comprobante asociado.\n"
                f"📄 Folio: {folio_especificado}\n"
                f"⏹️ Timer detenido.\n\n"
                f"📋 Para generar otro permiso use /chuleta"
            )
        except Exception as e:
            print(f"Error actualizando estado: {e}")
            await message.answer(
                f"✅ Folio confirmado: {folio_especificado}\n"
                f"⏹️ Timer detenido.\n\n"
                f"📋 Para generar otro permiso use /chuleta"
            )
    except Exception as e:
        print(f"[ERROR] especificar_folio_comprobante: {e}")
        if user_id in pending_comprobantes:
            del pending_comprobantes[user_id]
        await message.answer("❌ Error procesando el folio especificado. Intenta de nuevo.")

@dp.message(Command("folios"))
async def ver_folios_activos(message: types.Message):
    try:
        user_id = message.from_user.id
        folios_usuario = obtener_folios_usuario(user_id)
        
        if not folios_usuario:
            await message.answer(
                "ℹ️ NO HAY FOLIOS ACTIVOS\n\n"
                "No tienes folios pendientes de pago.\n"
                "Para nuevo permiso use /chuleta"
            )
            return
        
        lista_folios = []
        for folio in folios_usuario:
            if folio in timers_activos:
                tiempo_restante = 2160 - int((datetime.now() - timers_activos[folio]["start_time"]).total_seconds() / 60)
                tiempo_restante = max(0, tiempo_restante)
                horas = tiempo_restante // 60
                minutos = tiempo_restante % 60
                lista_folios.append(f"• {folio} ({horas}h {minutos}min restantes)")
            else:
                lista_folios.append(f"• {folio} (sin timer)")
        
        await message.answer(
            f"📋 FOLIOS GUERRERO ACTIVOS ({len(folios_usuario)})\n\n"
            + '\n'.join(lista_folios) +
            f"\n\n⏰ Cada folio tiene timer de 36 horas.\n"
            f"📸 Para enviar comprobante, use imagen."
        )
    except Exception as e:
        print(f"[ERROR] ver_folios_activos: {e}")
        await message.answer("❌ Error consultando expedientes activos.")

@dp.message(lambda message: message.text and any(palabra in message.text.lower() for palabra in [
    'costo', 'precio', 'cuanto', 'cuánto', 'deposito', 'depósito', 'pago', 'valor', 'monto'
]))
async def responder_costo(message: types.Message):
    await message.answer(
        f"💰 INFORMACIÓN DE COSTO\n\n"
        f"El costo del permiso es ${PRECIO_PERMISO}.\n\n"
        "Para iniciar su trámite use /chuleta"
    )

@dp.message()
async def fallback(message: types.Message):
    await message.answer("🏛️ Sistema Digital Guerrero.")

# ------------ FASTAPI + LIFESPAN ------------
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
            await bot.set_webhook(webhook_url, allowed_updates=["message"])
            print(f"[WEBHOOK] Configurado: {webhook_url}")
            _keep_task = asyncio.create_task(keep_alive())
        else:
            print("[POLLING] Modo sin webhook")
        print("[SISTEMA] ¡Sistema Digital Guerrero iniciado correctamente!")
        yield
    except Exception as e:
        print(f"[ERROR CRÍTICO] Iniciando sistema: {e}")
        yield
    finally:
        print("[CIERRE] Cerrando sistema...")
        if _keep_task:
            _keep_task.cancel()
            with suppress(asyncio.CancelledError):
                await _keep_task
        await bot.session.close()

app = FastAPI(lifespan=lifespan, title="Sistema Guerrero Digital", version="4.0")

@app.post("/webhook")
async def telegram_webhook(request: Request):
    try:
        data = await request.json()
        update = types.Update(**data)
        await dp.feed_webhook_update(bot, update)
        return {"ok": True}
    except Exception as e:
        print(f"[ERROR] webhook: {e}")
        return {"ok": False, "error": str(e)}

@app.get("/")
async def health():
    return {
        "ok": True,
        "bot": "Guerrero Permisos Sistema",
        "status": "running",
        "version": "4.0 - 2 PDFs Separados + Timer 36h + SERO + /chuleta",
        "entidad": "Guerrero",
        "vigencia": "30 días",
        "timer_eliminacion": "36 horas",
        "active_timers": len(timers_activos),
        "prefijo_folio": "ZA",
        "comando_secreto": "/chuleta (invisible)",
        "caracteristicas": [
            "2 PDFs separados (principal + recibo)",
            "Folios con prefijo ZA consecutivos",
            "Timer 36 horas con avisos 90/60/30/10",
            "Reintentos automáticos ante duplicados (100000 intentos)",
            "Comando admin: SERO[folio]",
            "Timers independientes por folio"
        ]
    }

@app.get("/status")
async def status_detail():
    return {
        "sistema": "Guerrero Digital v4.0 - 2 PDFs Separados",
        "entidad": "Guerrero",
        "vigencia_dias": 30,
        "tiempo_eliminacion": "36 horas con avisos 90/60/30/10",
        "total_timers_activos": len(timers_activos),
        "folios_con_timer": list(timers_activos.keys()),
        "usuarios_con_folios": len(user_folios),
        "prefijo_folio": "ZA",
        "pdf_output": "2 archivos separados (Guerrero.pdf + recibo)",
        "comando_secreto": "/chuleta (invisible)",
        "timestamp": datetime.now().isoformat(),
        "status": "Operacional"
    }

if __name__ == '__main__':
    try:
        import uvicorn
        port = int(os.getenv("PORT", 8000))
        print(f"[ARRANQUE] Iniciando servidor en puerto {port}")
        print(f"[SISTEMA] Guerrero v4.0 - 2 PDFs SEPARADOS + Timer 36h + SERO")
        print(f"[COMANDO SECRETO] /chuleta")
        print(f"[PREFIJO] ZA")
        print(f"[PDF OUTPUT] 2 archivos separados (NO unificados)")
        uvicorn.run(app, host="0.0.0.0", port=port)
    except Exception as e:
        print(f"[ERROR FATAL] No se pudo iniciar el servidor: {e}")
