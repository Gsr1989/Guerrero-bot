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
import fitz  # PyMuPDF
import pytz
import pdf417gen
from PIL import Image
import random

# Importaciones adicionales
from io import BytesIO
import base64
from pdf417gen import encode, render_image
import qrcode
import string
import csv
import json
import io
import time
import re  # para filtrar folios no numéricos

# ------------ CONFIG ------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")
BASE_URL = os.getenv("BASE_URL", "").rstrip("/")
URL_CONSULTA_BASE = "https://tlapadecomonfortexpediciondepermisosgob2.onrender.com"  # CAMBIAR POR TU URL
OUTPUT_DIR = "documentos"
PLANTILLA_PDF = "Guerrero.pdf"
PLANTILLA_BUENO = "elbueno.pdf"
PLANTILLA_FLASK = "recibo_permiso_guerrero_img.pdf"

os.makedirs(OUTPUT_DIR, exist_ok=True)
os.makedirs("static/pdfs", exist_ok=True)

# ------------ SUPABASE ------------
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ------------ BOT ------------
bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# ------------ TIMER MANAGEMENT - AUTOELIMINACIÓN A LAS 12 HORAS ------------
timers_activos = {}
user_folios = {}
pending_comprobantes = {}

async def eliminar_folio_automatico(folio: str):
    """Elimina folio automáticamente después de 12 horas"""
    try:
        user_id = None
        if folio in timers_activos:
            user_id = timers_activos[folio]["user_id"]
        
        supabase.table("folios_registrados").delete().eq("folio", folio).execute()
        
        if user_id:
            await bot.send_message(
                user_id,
                f"**TIEMPO AGOTADO**\n\n"
                f"**El folio {folio} ha sido eliminado del sistema por falta de pago.**\n\n"
                f"Para tramitar un nuevo permiso utilize **/permiso**",
                parse_mode="Markdown"
            )
        
        limpiar_timer_folio(folio)
            
    except Exception as e:
        print(f"Error eliminando folio {folio}: {e}")

async def iniciar_timer_eliminacion(user_id: int, folio: str):
    """Inicia el timer de 12 horas para eliminación automática"""
    async def timer_task():
        print(f"[TIMER] Iniciado para folio {folio}, usuario {user_id}")
        await asyncio.sleep(43200)  # 12 horas
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
    
    print(f"[SISTEMA] Timer iniciado para folio {folio}, total timers activos: {len(timers_activos)}")

def cancelar_timer_folio(folio: str):
    """Cancela el timer de un folio específico cuando el usuario paga"""
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
    """Limpia todas las referencias de un folio tras expirar"""
    if folio in timers_activos:
        user_id = timers_activos[folio]["user_id"]
        del timers_activos[folio]
        
        if user_id in user_folios and folio in user_folios[user_id]:
            user_folios[user_id].remove(folio)
            if not user_folios[user_id]:
                del user_folios[user_id]

def obtener_folios_usuario(user_id: int) -> list:
    """Obtiene todos los folios activos de un usuario"""
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

# ------------ FUNCIÓN GENERAR FOLIO GUERRERO (MEJORADA - SALTA FOLIOS OCUPADOS) ------------
def generar_folio_guerrero():
    letras = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    prefijo = "SR"
    inicio_num = 4060

    try:
        existentes = supabase.table("folios_registrados").select("folio").eq("entidad", "Guerrero").execute().data
        usados = set([r["folio"] for r in existentes if r["folio"] and len(r["folio"]) == 6 and r["folio"][:2] == prefijo])
    except Exception as e:
        print(f"Error consultando folios: {e}")
        usados = set()

    for num in range(inicio_num, 10000):
        folio_candidato = f"{prefijo}{str(num).zfill(4)}"
        if folio_candidato not in usados:
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

# ------------ FUNCIÓN GENERAR QR DINÁMICO ------------
def generar_qr_dinamico_guerrero(folio):
    """Genera QR dinámico para Guerrero"""
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

# ------------ FUNCIÓN GENERAR PDF FLASK (TIPO RECIBO) ------------
def generar_pdf_flask(folio, fecha_expedicion, fecha_vencimiento, contribuyente):
    """Genera el PDF tipo recibo como en el Flask"""
    try:
        ruta_pdf = f"{OUTPUT_DIR}/{folio}_recibo.pdf"
        
        doc = fitz.open(PLANTILLA_FLASK)
        page = doc[0]
        
        page.insert_text((700, 1750), folio, fontsize=100, fontname="helv")
        page.insert_text((2200, 1750), fecha_expedicion.strftime('%d/%m/%Y'), fontsize=100, fontname="helv")
        page.insert_text((4000, 1750), fecha_vencimiento.strftime('%d/%m/%Y'), fontsize=100, fontname="helv")
        page.insert_text((950, 1930), contribuyente.upper(), fontsize=100, fontname="helv")
        
        doc.save(ruta_pdf)
        doc.close()
        return ruta_pdf
    except Exception as e:
        print(f"ERROR al generar PDF Flask: {e}")
        return None

# ------------ PDF PRINCIPAL GUERRERO (COMPLETO CON QR) ------------
def generar_pdf_principal(datos: dict) -> str:
    """Genera el PDF principal de Guerrero con todos los datos y QR dinámico"""
    fol = datos["folio"]
    
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    out = os.path.join(OUTPUT_DIR, f"{fol}_guerrero.pdf")
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

    # AGREGAR QR DINÁMICO
    img_qr, url_qr = generar_qr_dinamico_guerrero(datos["folio"])
    
    if img_qr:
        buf = BytesIO()
        img_qr.save(buf, format="PNG")
        buf.seek(0)
        qr_pix = fitz.Pixmap(buf.read())

        x_qr = 80  # sumar arriba restar abajo 
        y_qr = 430  # sumar derecha restar izquierda 
        ancho_qr = 130
        alto_qr = 130

        pg.insert_image(
            fitz.Rect(x_qr, y_qr, x_qr + ancho_qr, y_qr + alto_qr),
            pixmap=qr_pix,
            overlay=True
        )
        print(f"[QR GUERRERO] Insertado en PDF: {url_qr}")

    doc.save(out)
    doc.close()
    
    return out

def generar_pdf_bueno(serie: str, fecha: datetime, folio: str) -> str:
    """Genera el PDF simple con fecha+hora y serie"""
    doc = fitz.open(PLANTILLA_BUENO)
    page = doc[0]
    
    fecha_hora_str = fecha.strftime("%d/%m/%Y %H:%M")
    
    page.insert_text((135.02, 193.88), fecha_hora_str, fontsize=6)
    page.insert_text((190, 324), serie, fontsize=6)

    filename = f"{OUTPUT_DIR}/{folio}_bueno.pdf"
    doc.save(filename)
    doc.close()
    
    return filename

# ------------ HANDLERS CON DIÁLOGOS PROFESIONALES ------------
@dp.message(Command("start"))
async def start_cmd(message: types.Message, state: FSMContext):
    await state.clear()
    frases_start = [
        "**🏛️ SISTEMA DIGITAL DEL ESTADO DE GUERRERO**\n"
        "Plataforma oficial para la gestión de trámites vehiculares\n\n"
        "**💰 Inversión del servicio:** $50 pesos\n"
        "**⏰ Tiempo límite para efectuar el pago:** 12 horas\n"
        "**💳 Modalidades de pago:** Transferencia SPIN OXXO\n\n"
        "**📋 Para iniciar su trámite, utilice el comando /permiso**\n"
        "**⚠️ IMPORTANTE:** Su folio será eliminado automáticamente del sistema si no realiza el pago dentro del tiempo establecido"
    ]
    await message.answer(random.choice(frases_start), parse_mode="Markdown")

@dp.message(Command("permiso"))
async def permiso_cmd(message: types.Message, state: FSMContext):
    folios_activos = obtener_folios_usuario(message.from_user.id)
    
    mensaje_folios = ""
    if folios_activos:
        mensaje_folios = f"\n\n**📋 FOLIOS ACTIVOS:** {', '.join(folios_activos)}\n(Cada folio tiene su propio timer independiente de 12 horas)"
    
    frases_inicio = [
        f"**🚗 SOLICITUD DE PERMISO DE CIRCULACIÓN - GUERRERO**\n\n"
        f"**📋 Inversión:** $50 pesos mexicanos\n"
        f"**⏰ Plazo para el pago:** 12 horas\n"
        f"**💼 Concepto de pago:** Número de folio asignado\n\n"
        f"Al proceder, usted acepta que el folio será eliminado si no efectúa el pago en el tiempo estipulado."
        f"{mensaje_folios}\n\n"
        f"Para comenzar, por favor indique la **MARCA** de su vehículo:"
    ]
    await message.answer(random.choice(frases_inicio), parse_mode="Markdown")
    await state.set_state(PermisoForm.marca)

@dp.message(PermisoForm.marca)
async def get_marca(message: types.Message, state: FSMContext):
    marca = message.text.strip().upper()
    
    if not marca or len(marca) < 2:
        frases_error = [
            "**⚠️ MARCA INVÁLIDA**\n\n"
            "Por favor, ingrese una marca válida de al menos 2 caracteres.\n"
            "**Ejemplos:** NISSAN, TOYOTA, HONDA, VOLKSWAGEN\n\n"
            "Intente nuevamente:"
        ]
        await message.answer(random.choice(frases_error), parse_mode="Markdown")
        return
    
    await state.update_data(marca=marca)
    
    frases_marca = [
        f"**✅ MARCA REGISTRADA:** {marca}\n\n"
        f"Excelente. Ahora proporcione la **LÍNEA** o **MODELO** del vehículo:"
    ]
    await message.answer(random.choice(frases_marca), parse_mode="Markdown")
    await state.set_state(PermisoForm.linea)

@dp.message(PermisoForm.linea)
async def get_linea(message: types.Message, state: FSMContext):
    linea = message.text.strip().upper()
    
    if not linea or len(linea) < 1:
        frases_error = [
            "**⚠️ LÍNEA/MODELO INVÁLIDO**\n\n"
            "Por favor, ingrese una línea o modelo válido.\n"
            "**Ejemplos:** SENTRA, TSURU, AVEO, JETTA\n\n"
            "Intente nuevamente:"
        ]
        await message.answer(random.choice(frases_error), parse_mode="Markdown")
        return
    
    await state.update_data(linea=linea)
    
    frases_linea = [
        f"**✅ LÍNEA CONFIRMADA:** {linea}\n\n"
        f"Perfecto. Indique el **AÑO** de fabricación del vehículo (formato de 4 dígitos):"
    ]
    await message.answer(random.choice(frases_linea), parse_mode="Markdown")
    await state.set_state(PermisoForm.anio)

@dp.message(PermisoForm.anio)
async def get_anio(message: types.Message, state: FSMContext):
    anio = message.text.strip()
    
    if not anio.isdigit() or len(anio) != 4:
        frases_error = [
            "**⚠️ AÑO INVÁLIDO**\n\n"
            "Por favor, ingrese un año válido de 4 dígitos.\n"
            "**Ejemplo correcto:** 2020, 2015, 2023\n\n"
            "Favor de intentarlo nuevamente:"
        ]
        await message.answer(random.choice(frases_error), parse_mode="Markdown")
        return
    
    anio_num = int(anio)
    if anio_num < 1980 or anio_num > datetime.now().year + 1:
        frases_error_rango = [
            f"**⚠️ AÑO FUERA DE RANGO**\n\n"
            f"El año debe estar entre 1980 y {datetime.now().year + 1}.\n"
            f"**Año ingresado:** {anio}\n\n"
            f"Por favor, verifique e intente nuevamente:"
        ]
        await message.answer(random.choice(frases_error_rango), parse_mode="Markdown")
        return
    
    await state.update_data(anio=anio)
    
    frases_anio = [
        f"**✅ AÑO VERIFICADO:** {anio}\n\n"
        f"Muy bien. Proporcione el **NÚMERO DE SERIE** del vehículo:"
    ]
    await message.answer(random.choice(frases_anio), parse_mode="Markdown")
    await state.set_state(PermisoForm.serie)

@dp.message(PermisoForm.serie)
async def get_serie(message: types.Message, state: FSMContext):
    serie = message.text.strip().upper()
    
    if len(serie) < 5:
        frases_error = [
            "**⚠️ NÚMERO DE SERIE INCOMPLETO**\n\n"
            "El número de serie debe tener al menos 5 caracteres.\n"
            "Por favor, verifique que haya ingresado la información completa.\n\n"
            "Intente nuevamente:"
        ]
        await message.answer(random.choice(frases_error), parse_mode="Markdown")
        return
    
    if len(serie) > 25:
        frases_error_largo = [
            "**⚠️ NÚMERO DE SERIE DEMASIADO LARGO**\n\n"
            "El número de serie no puede exceder 25 caracteres.\n"
            "Por favor, verifique la información ingresada.\n\n"
            "Intente nuevamente:"
        ]
        await message.answer(random.choice(frases_error_largo), parse_mode="Markdown")
        return
    
    await state.update_data(serie=serie)
    
    frases_serie = [
        f"**✅ SERIE CAPTURADA:** {serie}\n\n"
        f"Correcto. Ahora indique el **NÚMERO DE MOTOR**:"
    ]
    await message.answer(random.choice(frases_serie), parse_mode="Markdown")
    await state.set_state(PermisoForm.motor)

@dp.message(PermisoForm.motor)
async def get_motor(message: types.Message, state: FSMContext):
    motor = message.text.strip().upper()
    
    if len(motor) < 5:
        frases_error = [
            "**⚠️ NÚMERO DE MOTOR INCOMPLETO**\n\n"
            "El número de motor debe tener al menos 5 caracteres.\n"
            "Por favor, verifique que haya ingresado la información completa.\n\n"
            "Intente nuevamente:"
        ]
        await message.answer(random.choice(frases_error), parse_mode="Markdown")
        return
    
    if len(motor) > 25:
        frases_error_largo = [
            "**⚠️ NÚMERO DE MOTOR DEMASIADO LARGO**\n\n"
            "El número de motor no puede exceder 25 caracteres.\n"
            "Por favor, verifique la información ingresada.\n\n"
            "Intente nuevamente:"
        ]
        await message.answer(random.choice(frases_error_largo), parse_mode="Markdown")
        return
    
    await state.update_data(motor=motor)
    
    frases_motor = [
        f"**✅ MOTOR REGISTRADO:** {motor}\n\n"
        f"Excelente. Ahora especifique el **COLOR** del vehículo:"
    ]
    await message.answer(random.choice(frases_motor), parse_mode="Markdown")
    await state.set_state(PermisoForm.color)

@dp.message(PermisoForm.color)
async def get_color(message: types.Message, state: FSMContext):
    color = message.text.strip().upper()
    
    if not color or len(color) < 2:
        frases_error = [
            "**⚠️ COLOR INVÁLIDO**\n\n"
            "Por favor, ingrese un color válido del vehículo.\n"
            "**Ejemplos:** BLANCO, AZUL, ROJO, NEGRO, GRIS\n\n"
            "Intente nuevamente:"
        ]
        await message.answer(random.choice(frases_error), parse_mode="Markdown")
        return
    
    if len(color) > 20:
        frases_error_largo = [
            "**⚠️ COLOR DEMASIADO LARGO**\n\n"
            "El color no puede exceder 20 caracteres.\n"
            "Por favor, simplifique la descripción.\n\n"
            "Intente nuevamente:"
        ]
        await message.answer(random.choice(frases_error_largo), parse_mode="Markdown")
        return
    
    await state.update_data(color=color)
    
    frases_color = [
        f"**✅ COLOR CONFIRMADO:** {color}\n\n"
        f"Finalmente, proporcione el **NOMBRE COMPLETO** del propietario del vehículo:"
    ]
    await message.answer(random.choice(frases_color), parse_mode="Markdown")
    await state.set_state(PermisoForm.nombre)

@dp.message(PermisoForm.nombre)
async def get_nombre(message: types.Message, state: FSMContext):
    datos = await state.get_data()
    nombre = message.text.strip().upper()
    
    if len(nombre) < 5:
        frases_error = [
            "**⚠️ NOMBRE INCOMPLETO**\n\n"
            "Por favor, ingrese el nombre completo del titular.\n"
            "Debe incluir nombre(s) y apellido(s).\n\n"
            "**Ejemplo:** JUAN PÉREZ GARCÍA\n\n"
            "Intente nuevamente:"
        ]
        await message.answer(random.choice(frases_error), parse_mode="Markdown")
        return
    
    if len(nombre) > 60:
        frases_error_largo = [
            "**⚠️ NOMBRE DEMASIADO LARGO**\n\n"
            "El nombre no puede exceder 60 caracteres.\n"
            "Por favor, verifique la información.\n\n"
            "Intente nuevamente:"
        ]
        await message.answer(random.choice(frases_error_largo), parse_mode="Markdown")
        return
    
    palabras = nombre.split()
    if len(palabras) < 2:
        frases_error_palabras = [
            "**⚠️ NOMBRE INCOMPLETO**\n\n"
            "Por favor, proporcione al menos nombre y apellido.\n"
            "**Ejemplo:** MARÍA GONZÁLEZ\n\n"
            "Intente nuevamente:"
        ]
        await message.answer(random.choice(frases_error_palabras), parse_mode="Markdown")
        return
    
    datos["nombre"] = nombre
    datos["folio"] = generar_folio_guerrero()

    hoy = datetime.now()
    vigencia_dias = 30
    fecha_ven = hoy + timedelta(days=vigencia_dias)

    datos["fecha_exp"] = hoy.strftime("%d/%m/%Y")
    datos["fecha_ven"] = fecha_ven.strftime("%d/%m/%Y")
    datos["vigencia"] = fecha_ven.strftime("%d/%m/%Y")

    try:
        frases_procesando = [
            f"**🔄 PROCESANDO DOCUMENTACIÓN OFICIAL...**\n\n"
            f"**📄 Folio asignado:** {datos['folio']}\n"
            f"**🚗 Vehículo:** {datos['marca']} {datos['linea']} {datos['anio']}\n"
            f"**👤 Titular:** {nombre}\n\n"
            f"El sistema está generando su documentación. Por favor espere..."
        ]
        await message.answer(random.choice(frases_procesando), parse_mode="Markdown")
        
        p1 = generar_pdf_principal(datos)
        p2 = generar_pdf_flask(datos["folio"], hoy, fecha_ven, datos["nombre"])

        await message.answer_document(
            FSInputFile(p1),
            caption=f"**📋 PERMISO OFICIAL DE CIRCULACIÓN - GUERRERO**\n"
                   f"**Folio:** {datos['folio']}\n"
                   f"**Vigencia:** 30 días\n"
                   f"**🏛️ Documento con QR dinámico para consulta**"
        )
        
        if p2:
            await message.answer_document(
                FSInputFile(p2),
                caption=f"**🧾 COMPROBANTE DE VERIFICACIÓN**\n"
                       f"**Folio:** {datos['folio']}\n"
                       f"**📋 Documento complementario**"
            )

        try:
            supabase.table("folios_registrados").insert({
                "folio": datos["folio"],
                "marca": datos["marca"],
                "linea": datos["linea"],
                "anio": datos["anio"],
                "numero_serie": datos["serie"],
                "numero_motor": datos["motor"],
                "color": datos["color"],
                "nombre": datos["nombre"],
                "fecha_expedicion": hoy.date().isoformat(),
                "fecha_vencimiento": fecha_ven.date().isoformat(),
                "entidad": "Guerrero",
                "estado": "PENDIENTE",
                "user_id": message.from_user.id,
                "username": message.from_user.username or "Sin username"
            }).execute()
            
            await iniciar_timer_eliminacion(message.from_user.id, datos['folio'])
            
            await message.answer(
                f"**💰 INSTRUCCIONES PARA EL PAGO**\n\n"
                f"**📄 Folio:** {datos['folio']}\n"
                f"**💵 Monto:** $50 pesos mexicanos\n"
                f"**⏰ Tiempo límite:** 12 horas\n\n"
                
                "**🏪 TRANSFERENCIA SPIN BY OXXO:**\n"
                "• **Titular:** GUILLERMO S.R.J\n"
                "• **Número:** 7289690000484424454\n\n"
                
                "**💳 DEPÓSITO DIRECTO EN CAJA OXXO:**\n"
                "• **SOLO OXXO GUILLERMO. S.R.J**\n"
                "• **Referencia:** 2242170180214090\n"
                "• **Cantidad exacta:** $50.00\n\n"
                
                f"**📸 IMPORTANTE:** Una vez efectuado el pago, envíe la fotografía de su comprobante para la validación correspondiente.\n\n"
                f"**⚠️ ADVERTENCIA:** Si no completa el pago en las próximas 12 horas, el folio {datos['folio']} será eliminado automáticamente del sistema.",
                parse_mode="Markdown"
            )
            
        except Exception as e:
            print(f"Error guardando en Supabase: {e}")
            await message.answer(f"**⚠️ ADVERTENCIA:** PDFs generados pero error en registro: {str(e)}", parse_mode="Markdown")
        
    except Exception as e:
        await message.answer(f"**❌ ERROR EN EL SISTEMA:** {str(e)}", parse_mode="Markdown")
        print(f"Error: {e}")
    finally:
        await state.clear()

# Handler para recibir comprobantes de pago
@dp.message(lambda message: message.content_type == ContentType.PHOTO)
async def recibir_comprobante(message: types.Message):
    try:
        user_id = message.from_user.id
        folios_usuario = obtener_folios_usuario(user_id)
        
        if not folios_usuario:
            frases_sin_folios = [
                "**ℹ️ NO HAY PERMISOS PENDIENTES DE PAGO**\n\n"
                "No se encontró ningún permiso pendiente de pago para su cuenta.\n\n"
                "Si desea tramitar un nuevo permiso, utilice **/permiso**"
            ]
            await message.answer(random.choice(frases_sin_folios), parse_mode="Markdown")
            return
        
        if len(folios_usuario) > 1:
            lista_folios = '\n'.join([f"• **{folio}**" for folio in folios_usuario])
            pending_comprobantes[user_id] = "waiting_folio"
            await message.answer(
                f"**📄 MÚLTIPLES FOLIOS ACTIVOS**\n\n"
                f"Tiene {len(folios_usuario)} folios pendientes de pago:\n\n"
                f"{lista_folios}\n\n"
                f"Por favor, responda con el **NÚMERO DE FOLIO** al que corresponde este comprobante.\n"
                f"**Ejemplo:** {folios_usuario[0]}",
                parse_mode="Markdown"
            )
            return
        
        folio = folios_usuario[0]
        cancelar_timer_folio(folio)
        
        try:
            supabase.table("folios_registrados").update({
                "estado": "COMPROBANTE_ENVIADO",
                "fecha_comprobante": datetime.now().isoformat()
            }).eq("folio", folio).execute()
            
            frases_comprobante_recibido = [
                f"**✅ COMPROBANTE RECIBIDO CORRECTAMENTE**\n\n"
                f"**📄 Folio:** {folio}\n"
                f"**📸 Su comprobante de pago ha sido recibido exitosamente**\n"
                f"**⏰ Timer de eliminación automática detenido**\n\n"
                f"**🔍 Su comprobante está siendo verificado por nuestro equipo.**\n"
                f"Una vez validado el pago, su permiso quedará completamente activo.\n\n"
                f"**Gracias por utilizar el Sistema Digital del Estado de Guerrero.**"
            ]
            await message.answer(random.choice(frases_comprobante_recibido), parse_mode="Markdown")
            
        except Exception as e:
            print(f"Error actualizando estado comprobante: {e}")
            await message.answer(
                f"**✅ COMPROBANTE RECIBIDO**\n\n"
                f"**📄 Folio:** {folio}\n"
                f"**📸 Su comprobante fue recibido y el timer se detuvo.**\n\n"
                f"**⚠️ Hubo un problema menor actualizando el estado en el sistema, pero su comprobante está guardado.**",
                parse_mode="Markdown"
            )
            
    except Exception as e:
        print(f"[ERROR] recibir_comprobante: {e}")
        await message.answer(
            "**❌ ERROR PROCESANDO COMPROBANTE**\n\n"
            "Ocurrió un problema al procesar su imagen.\n"
            "Por favor, intente enviar nuevamente la fotografía de su comprobante.",
            parse_mode="Markdown"
        )

@dp.message(lambda message: message.from_user.id in pending_comprobantes and pending_comprobantes[message.from_user.id] == "waiting_folio")
async def especificar_folio_comprobante(message: types.Message):
    try:
        user_id = message.from_user.id
        folio_especificado = message.text.strip().upper()
        
        folios_usuario = obtener_folios_usuario(user_id)
        
        if folio_especificado not in folios_usuario:
            lista_folios = '\n'.join([f"• **{f}**" for f in folios_usuario])
            await message.answer(
                f"**❌ FOLIO NO ENCONTRADO**\n\n"
                f"El folio **'{folio_especificado}'** no está en sus folios activos.\n\n"
                f"Sus folios activos son:\n{lista_folios}\n\n"
                f"Por favor, verifique e ingrese un folio válido:",
                parse_mode="Markdown"
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
                f"**✅ FOLIO CONFIRMADO Y COMPROBANTE PROCESADO**\n\n"
                f"**📄 Folio:** {folio_especificado}\n"
                f"**📸 Su comprobante ha sido asociado correctamente**\n"
                f"**⏰ Timer de eliminación automática detenido**\n\n"
                f"**🔍 Su comprobante está siendo verificado.**\n"
                f"Una vez validado el pago, su permiso quedará activo.\n\n"
                f"Ahora puede enviar el comprobante de pago como imagen.",
                parse_mode="Markdown"
            )
            
        except Exception as e:
            print(f"Error actualizando estado: {e}")
            await message.answer(
                f"**✅ FOLIO CONFIRMADO**\n\n"
                f"**📄 Folio:** {folio_especificado}\n"
                f"**⏰ Timer detenido**\n\n"
                f"Ahora envíe la imagen del comprobante de pago.",
                parse_mode="Markdown"
            )
            
    except Exception as e:
        print(f"[ERROR] especificar_folio_comprobante: {e}")
        if user_id in pending_comprobantes:
            del pending_comprobantes[user_id]
        await message.answer("**❌ Error procesando el folio. Intente nuevamente.**", parse_mode="Markdown")

@dp.message(Command("folios"))
async def ver_folios_activos(message: types.Message):
    try:
        user_id = message.from_user.id
        folios_usuario = obtener_folios_usuario(user_id)
        
        if not folios_usuario:
            frases_sin_folios = [
                "**ℹ️ NO HAY FOLIOS ACTIVOS**\n\n"
                "No tiene folios pendientes de pago en este momento.\n\n"
                "Para crear un nuevo permiso utilice **/permiso**"
            ]
            await message.answer(random.choice(frases_sin_folios), parse_mode="Markdown")
            return
        
        lista_folios = []
        for folio in folios_usuario:
            if folio in timers_activos:
                tiempo_transcurrido = int((datetime.now() - timers_activos[folio]["start_time"]).total_seconds() / 60)
                tiempo_restante = max(0, 720 - tiempo_transcurrido)
                lista_folios.append(f"• **{folio}** ({tiempo_restante} min restantes)")
            else:
                lista_folios.append(f"• **{folio}** (timer detenido)")
        
        await message.answer(
            f"**📋 SUS FOLIOS ACTIVOS ({len(folios_usuario)})**\n\n"
            + '\n'.join(lista_folios) +
            f"\n\n**⏰ Cada folio tiene timer independiente de 12 horas.**\n"
            f"**📸 Para enviar comprobante, use una imagen.**\n"
            f"**💰 Costo por permiso:** $50 pesos",
            parse_mode="Markdown"
        )
        
    except Exception as e:
        print(f"[ERROR] ver_folios_activos: {e}")
        await message.answer("**❌ Error consultando folios activos.**", parse_mode="Markdown")

# Agrega este handler ANTES del handler fallback (@dp.message() al final)

@dp.message(lambda message: message.text and message.text.upper().startswith("SERO"))
async def cancelar_timer_sero(message: types.Message):
    """Handler para cancelar timer usando palabra clave SERO + folio"""
    try:
        texto = message.text.upper().strip()
        
        # Extraer el folio después de SERO
        if len(texto) > 4:  # SERO + al menos 1 carácter
            folio = texto[4:]  # Quita "SERO" y toma el resto
            
            # Verificar que el folio existe en los timers activos
            if folio not in timers_activos:
                await message.answer(
                    f"**❌ FOLIO NO ENCONTRADO**\n\n"
                    f"El folio **{folio}** no está en los timers activos.\n\n"
                    f"**Timers activos:** {list(timers_activos.keys()) if timers_activos else 'Ninguno'}\n\n"
                    f"**Formato correcto:** SERO + folio (ejemplo: SEROSR2001)",
                    parse_mode="Markdown"
                )
                return
            
            # Verificar que el folio pertenezca al usuario (opcional - para seguridad)
            user_id = message.from_user.id
            if timers_activos[folio]["user_id"] != user_id:
                await message.answer(
                    f"**🔒 ACCESO DENEGADO**\n\n"
                    f"El folio **{folio}** no pertenece a su cuenta.\n"
                    f"Solo puede cancelar timers de sus propios folios.",
                    parse_mode="Markdown"
                )
                return
            
            # Cancelar el timer
            cancelar_timer_folio(folio)
            
            # Actualizar estado en base de datos
            try:
                supabase.table("folios_registrados").update({
                    "estado": "TIMER_CANCELADO_MANUAL",
                    "fecha_cancelacion": datetime.now().isoformat(),
                    "metodo_cancelacion": "COMANDO_SERO"
                }).eq("folio", folio).execute()
            except Exception as e:
                print(f"Error actualizando BD para folio {folio}: {e}")
            
            await message.answer(
                f"**✅ TIMER CANCELADO EXITOSAMENTE**\n\n"
                f"**📄 Folio:** {folio}\n"
                f"**⏰ El timer de eliminación automática ha sido detenido**\n"
                f"**🛡️ El folio ya no será eliminado automáticamente**\n\n"
                f"**Estado actualizado:** Timer cancelado manualmente\n"
                f"**Método:** Comando SERO",
                parse_mode="Markdown"
            )
            
        else:
            await message.answer(
                f"**❌ FORMATO INCORRECTO**\n\n"
                f"**Uso correcto:** SERO + número de folio\n"
                f"**Ejemplo:** SEROSR2001\n\n"
                f"**Su mensaje:** {texto}\n"
                f"**Longitud:** {len(texto)} caracteres",
                parse_mode="Markdown"
            )
            
    except Exception as e:
        print(f"[ERROR] cancelar_timer_sero: {e}")
        await message.answer(
            f"**❌ ERROR PROCESANDO COMANDO**\n\n"
            f"Ocurrió un error al procesar el comando SERO.\n"
            f"**Error:** {str(e)}\n\n"
            f"Por favor, intente nuevamente con el formato: SERO + folio",
            parse_mode="Markdown"
        )

# También puedes agregar un comando de ayuda para SERO
@dp.message(Command("sero"))
async def ayuda_sero(message: types.Message):
    """Comando de ayuda para SERO"""
    try:
        user_id = message.from_user.id
        folios_usuario = obtener_folios_usuario(user_id)
        
        mensaje_folios = ""
        if folios_usuario:
            lista_ejemplos = []
            for folio in folios_usuario[:3]:  # Máximo 3 ejemplos
                lista_ejemplos.append(f"• **SERO{folio}**")
            mensaje_folios = f"\n\n**📋 Sus folios activos:**\n" + '\n'.join(lista_ejemplos)
        
        await message.answer(
            f"**🛠️ COMANDO SERO - CANCELAR TIMER**\n\n"
            f"**📝 Función:** Cancela el timer de eliminación automática de un folio\n"
            f"**⏰ Uso:** Evita que el folio sea eliminado a las 12 horas\n\n"
            f"**🔧 Formato correcto:**\n"
            f"• Escriba: **SERO** + **número de folio**\n"
            f"• Ejemplo: **SEROSR2001**\n"
            f"• Sin espacios entre SERO y el folio\n\n"
            f"**⚠️ Importante:**\n"
            f"• Solo funciona con folios de su propiedad\n"
            f"• El folio debe tener timer activo\n"
            f"• Una vez cancelado, no se puede reactivar"
            f"{mensaje_folios}",
            parse_mode="Markdown"
        )
        
    except Exception as e:
        print(f"[ERROR] ayuda_sero: {e}")
        await message.answer(
            f"**🛠️ COMANDO SERO**\n\n"
            f"Cancela timer de folio: **SERO + folio**\n"
            f"Ejemplo: **SEROSR2001**",
            parse_mode="Markdown"
        )

# Opcional: Handler más flexible que acepta variaciones
@dp.message(lambda message: message.text and re.match(r'^SERO\s*[A-Z]{2}\d{4}$', message.text.upper().replace(' ', '')))
async def cancelar_timer_sero_flexible(message: types.Message):
    """Handler más flexible que acepta espacios y variaciones"""
    try:
        texto_limpio = message.text.upper().replace(' ', '').strip()
        folio = texto_limpio[4:]  # Quita "SERO"
        
        # Reutilizar la lógica del handler principal
        # (Aquí va la misma lógica que en cancelar_timer_sero)
        
        print(f"[SERO FLEXIBLE] Procesando: {texto_limpio} -> Folio: {folio}")
        
        # Copiar aquí toda la lógica del handler principal...
        
    except Exception as e:
        print(f"[ERROR] sero_flexible: {e}")
        
@dp.message(lambda message: message.text and any(palabra in message.text.lower() for palabra in [
    'costo', 'precio', 'cuanto', 'cuánto', 'deposito', 'depósito', 'pago', 'valor', 'monto'
]))
async def responder_costo(message: types.Message):
    try:
        frases_costo = [
            "**💰 INFORMACIÓN SOBRE LA INVERSIÓN**\n\n"
            "El costo del permiso es de **$50 pesos mexicanos**.\n"
            "**Vigencia:** 30 días\n"
            "**Pago:** OXXO (Transferencia SPIN o depósito directo)\n\n"
            "Para iniciar su trámite utilice **/permiso**"
        ]
        await message.answer(random.choice(frases_costo), parse_mode="Markdown")
    except Exception as e:
        print(f"[ERROR] responder_costo: {e}")
        await message.answer("**💰 Costo del permiso:** $50 pesos. Use **/permiso** para tramitar.", parse_mode="Markdown")

@dp.message()
async def fallback(message: types.Message):
    respuestas_random = [
        "**🏛️ Sistema Digital del Estado de Guerrero.** Para tramitar su permiso utilice **/permiso**",
        "**📋 Plataforma gubernamental de servicios.** Comando disponible: **/permiso**",
        "**⚡ Sistema en línea activo.** Use **/permiso** para generar su documento oficial"
    ]
    await message.answer(random.choice(respuestas_random), parse_mode="Markdown")

_keep_task = None

async def keep_alive():
    while True:
        await asyncio.sleep(600)
        print("[HEARTBEAT] Sistema activo")

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
        
        print("[SISTEMA] ¡Guerrero Sistema Digital iniciado correctamente!")
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

app = FastAPI(lifespan=lifespan, title="Sistema Guerrero Digital", version="2.0")

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
    try:
        return {
            "ok": True, 
            "bot": "Guerrero Permisos Sistema", 
            "status": "running",
            "version": "2.0",
            "costo_permiso": "$50 MXN",
            "vigencia": "30 días",
            "timer_eliminacion": "12 horas",
            "active_timers": len(timers_activos),
            "independent_timers": True,
            "qr_dinamico": True,
            "texto_negritas": True
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}

@app.get("/status")
async def status_detail():
    try:
        return {
            "sistema": "Guerrero Digital v2.0 - Timers Independientes 12h",
            "entidad": "Guerrero",
            "costo": "$50 pesos mexicanos",
            "vigencia_dias": 30,
            "tiempo_eliminacion": "12 horas",
            "total_timers_activos": len(timers_activos),
            "folios_con_timer": list(timers_activos.keys()),
            "usuarios_con_folios": len(user_folios),
            "detalle_usuarios": {str(uid): folios for uid, folios in user_folios.items()},
            "timestamp": datetime.now().isoformat(),
            "status": "Operacional - Eliminación automática activa",
            "prefijo_folio": "SR respetado",
            "qr_dinamico_activo": True,
            "texto_negritas_activo": True
        }
    except Exception as e:
        return {"error": str(e), "status": "Error"}

if __name__ == '__main__':
    try:
        import uvicorn
        port = int(os.getenv("PORT", 8000))
        print(f"[ARRANQUE] Iniciando servidor en puerto {port}")
        print(f"[SISTEMA] Timers de eliminación independientes habilitados - 12 HORAS")
        print(f"[CONFIG] Costo: $50 MXN - Vigencia: 30 días - Auto-eliminación: 12 horas")
        print(f"[FUNCIONALIDADES] QR dinámico: ✅ | Texto negritas: ✅ | Prefijo SR: ✅")
        uvicorn.run(app, host="0.0.0.0", port=port)
    except Exception as e:
        print(f"[ERROR FATAL] No se pudo iniciar el servidor: {e}")
