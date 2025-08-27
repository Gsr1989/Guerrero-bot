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
import random

# ------------ CONFIG ------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")
BASE_URL = os.getenv("BASE_URL", "").rstrip("/")
OUTPUT_DIR = "documentos"
PLANTILLA_PDF = "Guerrero.pdf"  # PDF principal completo
PLANTILLA_BUENO = "elbueno.pdf"  # PDF simple (NO SE USA)
PLANTILLA_FLASK = "recibo_permiso_guerrero_img.pdf"  # Plantilla del recibo

os.makedirs(OUTPUT_DIR, exist_ok=True)
os.makedirs("static/pdfs", exist_ok=True)

# ------------ SUPABASE ------------
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ------------ BOT ------------
bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# ------------ TIMER MANAGEMENT - AUTOELIMINACIÓN A LAS 2 HORAS ------------
timers_activos = {}  # {folio: {"task": task, "user_id": user_id, "start_time": datetime}}
user_folios = {}     # {user_id: [lista_de_folios_activos]}
pending_comprobantes = {}  # {user_id: folio} para usuarios esperando especificar folio

async def eliminar_folio_automatico(folio: str):
    """Elimina folio automáticamente después de 2 horas"""
    try:
        # Obtener user_id del folio
        user_id = None
        if folio in timers_activos:
            user_id = timers_activos[folio]["user_id"]
        
        # Eliminar de base de datos
        supabase.table("folios_registrados").delete().eq("folio", folio).execute()
        
        # Notificar al usuario si está disponible
        if user_id:
            await bot.send_message(
                user_id,
                f"TIEMPO AGOTADO\n\n"
                f"El folio {folio} ha sido eliminado del sistema por falta de pago.\n\n"
                f"Para tramitar un nuevo permiso utilize /permiso"
            )
        
        # Limpiar timers
        limpiar_timer_folio(folio)
            
    except Exception as e:
        print(f"Error eliminando folio {folio}: {e}")

async def iniciar_timer_eliminacion(user_id: int, folio: str):
    """Inicia el timer de 2 horas para eliminación automática"""
    async def timer_task():
        print(f"[TIMER] Iniciado para folio {folio}, usuario {user_id}")
        
        # Esperar 2 horas (7200 segundos)
        await asyncio.sleep(7200)
        
        # Si llegamos aquí, se acabó el tiempo - eliminar
        if folio in timers_activos:
            print(f"[TIMER] Expirado para folio {folio} - eliminando")
            await eliminar_folio_automatico(folio)
    
    # Crear y guardar el task
    task = asyncio.create_task(timer_task())
    timers_activos[folio] = {
        "task": task,
        "user_id": user_id,
        "start_time": datetime.now()
    }
    
    # Agregar folio a la lista del usuario
    if user_id not in user_folios:
        user_folios[user_id] = []
    user_folios[user_id].append(folio)
    
    print(f"[SISTEMA] Timer iniciado para folio {folio}, total timers activos: {len(timers_activos)}")

def cancelar_timer_folio(folio: str):
    """Cancela el timer de un folio específico cuando el usuario paga"""
    if folio in timers_activos:
        timers_activos[folio]["task"].cancel()
        user_id = timers_activos[folio]["user_id"]
        
        # Remover de estructuras de datos
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
    "anio": (0,0,8,(0,0,0)),  # Agregar coordenadas para año si las necesitas
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

# ------------ FUNCIÓN GENERAR FOLIO GUERRERO (MEJORADA PARA EVITAR DUPLICADOS) ------------
def generar_folio_guerrero():
    letras = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    inicio_letras = "SR"
    inicio_num = 2000

    try:
        existentes = supabase.table("folios_registrados").select("folio").eq("entidad", "Guerrero").execute().data
        usados = set([r["folio"] for r in existentes if r["folio"] and len(r["folio"]) == 6 and r["folio"][:2].isalpha()])
    except Exception as e:
        print(f"Error consultando folios: {e}")
        usados = set()

    empezar = False
    for l1 in letras:
        for l2 in letras:
            par = l1 + l2
            for num in range(1, 10000):
                if not empezar:
                    if par == inicio_letras and num >= inicio_num:
                        empezar = True
                    else:
                        continue
                nuevo = f"{par}{str(num).zfill(4)}"
                if nuevo not in usados:
                    return nuevo
    return "ZZ9999"  # Fallback

# ------------ FSM STATES ------------
class PermisoForm(StatesGroup):
    marca = State()
    linea = State()
    anio = State()
    serie = State()
    motor = State()
    color = State()
    nombre = State()

# ------------ FUNCIÓN GENERAR PDF FLASK (TIPO RECIBO) ------------
def generar_pdf_flask(folio, fecha_expedicion, fecha_vencimiento, contribuyente):
    """Genera el PDF tipo recibo como en el Flask"""
    try:
        ruta_pdf = f"{OUTPUT_DIR}/{folio}_recibo.pdf"
        
        doc = fitz.open(PLANTILLA_FLASK)
        page = doc[0]
        
        # Insertar datos en coordenadas del Flask
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

# ------------ PDF PRINCIPAL GUERRERO (COMPLETO) ------------
def generar_pdf_principal(datos: dict) -> str:
    """Genera el PDF principal de Guerrero con todos los datos"""
    fol = datos["folio"]
    fecha_exp = datos["fecha_exp"]
    fecha_ven = datos["fecha_ven"]
    
    # Crear carpeta de salida
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    out = os.path.join(OUTPUT_DIR, f"{fol}_guerrero.pdf")
    doc = fitz.open(PLANTILLA_PDF)
    pg = doc[0]

    # --- Insertar campos normales del formulario ---
    for campo in ["folio", "fecha_exp", "fecha_ven", "serie", "motor", "marca", "linea", "color", "nombre"]:
        if campo in coords_guerrero and campo in datos:
            x, y, s, col = coords_guerrero[campo]
            texto = datos.get(campo, "")
            pg.insert_text((x, y), str(texto), fontsize=s, color=col)

    # --- Insertar campos rotados ---
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

    doc.save(out)
    doc.close()
    
    return out

def generar_pdf_bueno(serie: str, fecha: datetime, folio: str) -> str:
    """Genera el PDF simple con fecha+hora y serie"""
    doc = fitz.open(PLANTILLA_BUENO)
    page = doc[0]
    
    # Crear fecha y hora string
    fecha_hora_str = fecha.strftime("%d/%m/%Y %H:%M")
    
    # Imprimir fecha+hora y serie
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
        "🏛️ SISTEMA DIGITAL DEL ESTADO DE GUERRERO\n"
        "Plataforma oficial para la gestión de trámites vehiculares\n\n"
        "💰 Inversión del servicio: $50 pesos\n"
        "⏰ Tiempo límite para efectuar el pago: 2 horas\n"
        "💳 Modalidades de pago: Transferencia SPIN OXXO\n\n"
        "📋 Para iniciar su trámite, utilice el comando /permiso\n"
        "⚠️ IMPORTANTE: Su folio será eliminado automáticamente del sistema si no realiza el pago dentro del tiempo establecido",
        
        "🏆 BIENVENIDO AL SISTEMA GUBERNAMENTAL DE GUERRERO\n"
        "Servicio digital de excelencia para ciudadanos\n\n"
        "💼 Tarifa establecida: $50 pesos mexicanos\n"
        "🕐 Plazo para liquidación: 120 minutos\n"
        "🏪 Punto de pago: Establecimientos OXXO (Transferencia SPIN)\n\n"
        "🚀 Comando de inicio: /permiso\n"
        "📢 AVISO: Los folios no pagados se eliminan automáticamente tras el vencimiento",
        
        "⚡ PORTAL DIGITAL - ESTADO DE GUERRERO\n"
        "Su aliado confiable en trámites vehiculares\n\n"
        "💵 Inversión requerida: Cincuenta pesos ($50.00)\n"
        "⌛ Ventana de pago: 2 horas exactas\n"
        "🔄 Método: Transferencia SPIN en tiendas OXXO\n\n"
        "📝 Inicie con: /permiso\n"
        "🎯 COMPROMISO: Sistema automático, sin excepciones en tiempos"
    ]
    await message.answer(random.choice(frases_start))

@dp.message(Command("permiso"))
async def permiso_cmd(message: types.Message, state: FSMContext):
    # Verificar folios activos del usuario
    folios_activos = obtener_folios_usuario(message.from_user.id)
    
    mensaje_folios = ""
    if folios_activos:
        mensaje_folios = f"\n\n📋 FOLIOS ACTIVOS: {', '.join(folios_activos)}\n(Cada folio tiene su propio timer independiente de 2 horas)"
    
    frases_inicio = [
        f"🚗 SOLICITUD DE PERMISO DE CIRCULACIÓN - GUERRERO\n\n"
        f"📋 Inversión: $50 pesos mexicanos\n"
        f"⏰ Plazo para el pago: 2 horas\n"
        f"💼 Concepto de pago: Número de folio asignado\n\n"
        f"Al proceder, usted acepta que el folio será eliminado si no efectúa el pago en el tiempo estipulado."
        f"{mensaje_folios}\n\n"
        f"Para comenzar, por favor indique la MARCA de su vehículo:",
        
        f"🏛️ TRÁMITE DE PERMISO VEHICULAR - ESTADO DE GUERRERO\n\n"
        f"💰 Tarifa oficial: $50.00 MXN\n"
        f"🕐 Tiempo límite: 120 minutos\n"
        f"📋 Modalidad: Pago contra folio único\n\n"
        f"Acepta los términos de eliminación automática por falta de pago."
        f"{mensaje_folios}\n\n"
        f"Proporcione la MARCA del vehículo a registrar:",
        
        f"⚡ GESTIÓN DE PERMISO - GUERRERO DIGITAL\n\n"
        f"🎯 Costo: Cincuenta pesos mexicanos\n"
        f"⌛ Ventana de pago: 2 horas exactas\n"
        f"🔒 Sistema: Folio único intransferible\n\n"
        f"Confirma aceptación de políticas de eliminación automática."
        f"{mensaje_folios}\n\n"
        f"Ingrese la MARCA de su vehículo:"
    ]
    await message.answer(random.choice(frases_inicio))
    await state.set_state(PermisoForm.marca)

@dp.message(PermisoForm.marca)
async def get_marca(message: types.Message, state: FSMContext):
    marca = message.text.strip().upper()
    
    if not marca or len(marca) < 2:
        frases_error = [
            "⚠️ MARCA INVÁLIDA\n\n"
            "Por favor, ingrese una marca válida de al menos 2 caracteres.\n"
            "Ejemplos: NISSAN, TOYOTA, HONDA, VOLKSWAGEN\n\n"
            "Intente nuevamente:",
            
            "❌ FORMATO INCORRECTO\n\n"
            "La marca del vehículo debe contener mínimo 2 caracteres.\n"
            "Ejemplos válidos: FORD, BMW, AUDI, CHEVROLET\n\n"
            "Favor de reintentar:",
            
            "🔍 DATO INSUFICIENTE\n\n"
            "Requiere especificar marca con al menos 2 caracteres.\n"
            "Referencias: MAZDA, KIA, HYUNDAI, JEEP\n\n"
            "Ingrese nuevamente:"
        ]
        await message.answer(random.choice(frases_error))
        return
    
    await state.update_data(marca=marca)
    
    frases_marca = [
        f"✅ MARCA REGISTRADA: {marca}\n\n"
        f"Excelente. Ahora proporcione la LÍNEA o MODELO del vehículo:",
        
        f"📝 MARCA CONFIRMADA: {marca}\n\n"
        f"Perfecto. Continúe con la LÍNEA/MODELO del vehículo:",
        
        f"🎯 MARCA VALIDADA: {marca}\n\n"
        f"Correcto. Especifique la LÍNEA o MODELO del vehículo:",
        
        f"💾 MARCA CAPTURADA: {marca}\n\n"
        f"Muy bien. Proporcione la LÍNEA/MODELO del vehículo:"
    ]
    await message.answer(random.choice(frases_marca))
    await state.set_state(PermisoForm.linea)

@dp.message(PermisoForm.linea)
async def get_linea(message: types.Message, state: FSMContext):
    linea = message.text.strip().upper()
    
    if not linea or len(linea) < 1:
        frases_error = [
            "⚠️ LÍNEA/MODELO INVÁLIDO\n\n"
            "Por favor, ingrese una línea o modelo válido.\n"
            "Ejemplos: SENTRA, TSURU, AVEO, JETTA\n\n"
            "Intente nuevamente:",
            
            "❌ MODELO INCOMPLETO\n\n"
            "Debe especificar la línea o modelo del vehículo.\n"
            "Ejemplos: CIVIC, COROLLA, FOCUS, CRUZE\n\n"
            "Favor de corregir:",
            
            "🔍 INFORMACIÓN FALTANTE\n\n"
            "Requiere línea o modelo del vehículo.\n"
            "Referencias: ALTIMA, VERSA, MARCH, TIIDA\n\n"
            "Proporcione el dato:"
        ]
        await message.answer(random.choice(frases_error))
        return
    
    await state.update_data(linea=linea)
    
    frases_linea = [
        f"✅ LÍNEA CONFIRMADA: {linea}\n\n"
        f"Perfecto. Indique el AÑO de fabricación del vehículo (formato de 4 dígitos):",
        
        f"📋 MODELO REGISTRADO: {linea}\n\n"
        f"Excelente. Especifique el AÑO del vehículo (4 dígitos):",
        
        f"🎯 LÍNEA VALIDADA: {linea}\n\n"
        f"Correcto. Proporcione el AÑO de fabricación (YYYY):",
        
        f"💾 MODELO CAPTURADO: {linea}\n\n"
        f"Muy bien. Ingrese el AÑO del vehículo (4 dígitos):"
    ]
    await message.answer(random.choice(frases_linea))
    await state.set_state(PermisoForm.anio)

@dp.message(PermisoForm.anio)
async def get_anio(message: types.Message, state: FSMContext):
    anio = message.text.strip()
    
    if not anio.isdigit() or len(anio) != 4:
        frases_error = [
            "⚠️ AÑO INVÁLIDO\n\n"
            "Por favor, ingrese un año válido de 4 dígitos.\n"
            "Ejemplo correcto: 2020, 2015, 2023\n\n"
            "Favor de intentarlo nuevamente:",
            
            "❌ FORMATO INCORRECTO\n\n"
            "El año debe contener exactamente 4 dígitos numéricos.\n"
            "Ejemplos válidos: 2018, 2019, 2024\n\n"
            "Intente de nuevo:",
            
            "🔍 DATO INVÁLIDO\n\n"
            "Requiere año en formato YYYY (4 dígitos).\n"
            "Referencias: 2016, 2017, 2022\n\n"
            "Corrija el formato:"
        ]
        await message.answer(random.choice(frases_error))
        return
    
    anio_num = int(anio)
    if anio_num < 1980 or anio_num > datetime.now().year + 1:
        frases_error_rango = [
            f"⚠️ AÑO FUERA DE RANGO\n\n"
            f"El año debe estar entre 1980 y {datetime.now().year + 1}.\n"
            f"Año ingresado: {anio}\n\n"
            f"Por favor, verifique e intente nuevamente:",
            
            f"❌ RANGO INVÁLIDO\n\n"
            f"Años aceptados: 1980 - {datetime.now().year + 1}\n"
            f"Su entrada: {anio} (no válida)\n\n"
            f"Favor de corregir:",
            
            f"🔍 FUERA DE LÍMITES\n\n"
            f"Rango permitido: 1980 a {datetime.now().year + 1}\n"
            f"Valor ingresado: {anio}\n\n"
            f"Ajuste su entrada:"
        ]
        await message.answer(random.choice(frases_error_rango))
        return
    
    await state.update_data(anio=anio)
    
    frases_anio = [
        f"✅ AÑO VERIFICADO: {anio}\n\n"
        f"Muy bien. Proporcione el NÚMERO DE SERIE del vehículo:",
        
        f"📅 AÑO CONFIRMADO: {anio}\n\n"
        f"Excelente. Especifique el NÚMERO DE SERIE del vehículo:",
        
        f"🎯 AÑO VALIDADO: {anio}\n\n"
        f"Correcto. Ingrese el NÚMERO DE SERIE del vehículo:",
        
        f"💾 AÑO REGISTRADO: {anio}\n\n"
        f"Perfecto. Proporcione el NÚMERO DE SERIE:"
    ]
    await message.answer(random.choice(frases_anio))
    await state.set_state(PermisoForm.serie)

@dp.message(PermisoForm.serie)
async def get_serie(message: types.Message, state: FSMContext):
    serie = message.text.strip().upper()
    
    if len(serie) < 5:
        frases_error = [
            "⚠️ NÚMERO DE SERIE INCOMPLETO\n\n"
            "El número de serie debe tener al menos 5 caracteres.\n"
            "Por favor, verifique que haya ingresado la información completa.\n\n"
            "Intente nuevamente:",
            
            "❌ SERIE INSUFICIENTE\n\n"
            "Mínimo requerido: 5 caracteres para el número de serie.\n"
            "Verifique la información en su documentación.\n\n"
            "Favor de corregir:",
            
            "🔍 DATO INCOMPLETO\n\n"
            "El número de serie requiere mínimo 5 caracteres.\n"
            "Consulte la tarjeta de circulación para el dato completo.\n\n"
            "Proporcione información completa:"
        ]
        await message.answer(random.choice(frases_error))
        return
    
    if len(serie) > 25:
        frases_error_largo = [
            "⚠️ NÚMERO DE SERIE DEMASIADO LARGO\n\n"
            "El número de serie no puede exceder 25 caracteres.\n"
            "Por favor, verifique la información ingresada.\n\n"
            "Intente nuevamente:",
            
            "❌ SERIE EXCESIVA\n\n"
            "Máximo permitido: 25 caracteres para el número de serie.\n"
            "Revise que no haya incluido información adicional.\n\n"
            "Favor de ajustar:",
            
            "🔍 LÍMITE EXCEDIDO\n\n"
            "El número de serie no debe superar 25 caracteres.\n"
            "Verifique que sea únicamente el número de serie.\n\n"
            "Corrija la entrada:"
        ]
        await message.answer(random.choice(frases_error_largo))
        return
    
    await state.update_data(serie=serie)
    
    frases_serie = [
        f"✅ SERIE CAPTURADA: {serie}\n\n"
        f"Correcto. Ahora indique el NÚMERO DE MOTOR:",
        
        f"📝 SERIE REGISTRADA: {serie}\n\n"
        f"Perfecto. Especifique el NÚMERO DE MOTOR del vehículo:",
        
        f"🎯 SERIE VALIDADA: {serie}\n\n"
        f"Excelente. Proporcione el NÚMERO DE MOTOR:",
        
        f"💾 SERIE ALMACENADA: {serie}\n\n"
        f"Muy bien. Ingrese el NÚMERO DE MOTOR del vehículo:"
    ]
    await message.answer(random.choice(frases_serie))
    await state.set_state(PermisoForm.motor)

@dp.message(PermisoForm.motor)
async def get_motor(message: types.Message, state: FSMContext):
    motor = message.text.strip().upper()
    
    if len(motor) < 5:
        frases_error = [
            "⚠️ NÚMERO DE MOTOR INCOMPLETO\n\n"
            "El número de motor debe tener al menos 5 caracteres.\n"
            "Por favor, verifique que haya ingresado la información completa.\n\n"
            "Intente nuevamente:",
            
            "❌ MOTOR INSUFICIENTE\n\n"
            "Mínimo requerido: 5 caracteres para el número de motor.\n"
            "Verifique la información en su documentación.\n\n"
            "Favor de corregir:",
            
            "🔍 DATO INCOMPLETO\n\n"
            "El número de motor requiere mínimo 5 caracteres.\n"
            "Consulte la tarjeta de circulación para el dato completo.\n\n"
            "Proporcione información completa:"
        ]
        await message.answer(random.choice(frases_error))
        return
    
    if len(motor) > 25:
        frases_error_largo = [
            "⚠️ NÚMERO DE MOTOR DEMASIADO LARGO\n\n"
            "El número de motor no puede exceder 25 caracteres.\n"
            "Por favor, verifique la información ingresada.\n\n"
            "Intente nuevamente:",
            
            "❌ MOTOR EXCESIVO\n\n"
            "Máximo permitido: 25 caracteres para el número de motor.\n"
            "Revise que no haya incluido información adicional.\n\n"
            "Favor de ajustar:",
            
            "🔍 LÍMITE EXCEDIDO\n\n"
            "El número de motor no debe superar 25 caracteres.\n"
            "Verifique que sea únicamente el número de motor.\n\n"
            "Corrija la entrada:"
        ]
        await message.answer(random.choice(frases_error_largo))
        return
    
    await state.update_data(motor=motor)
    
    frases_motor = [
        f"✅ MOTOR REGISTRADO: {motor}\n\n"
        f"Excelente. Ahora especifique el COLOR del vehículo:",
        
        f"📝 MOTOR CAPTURADO: {motor}\n\n"
        f"Perfecto. Indique el COLOR del vehículo:",
        
        f"🎯 MOTOR VALIDADO: {motor}\n\n"
        f"Correcto. Proporcione el COLOR del vehículo:",
        
        f"💾 MOTOR ALMACENADO: {motor}\n\n"
        f"Muy bien. Especifique el COLOR del vehículo:"
    ]
    await message.answer(random.choice(frases_motor))
    await state.set_state(PermisoForm.color)

@dp.message(PermisoForm.color)
async def get_color(message: types.Message, state: FSMContext):
    color = message.text.strip().upper()
    
    if not color or len(color) < 2:
        frases_error = [
            "⚠️ COLOR INVÁLIDO\n\n"
            "Por favor, ingrese un color válido del vehículo.\n"
            "Ejemplos: BLANCO, AZUL, ROJO, NEGRO, GRIS\n\n"
            "Intente nuevamente:",
            
            "❌ COLOR INCOMPLETO\n\n"
            "Debe especificar el color del vehículo.\n"
            "Ejemplos válidos: VERDE, AMARILLO, PLATA\n\n"
            "Favor de corregir:",
            
            "🔍 INFORMACIÓN FALTANTE\n\n"
            "Requiere color válido del vehículo.\n"
            "Referencias: CAFÉ, NARANJA, MORADO\n\n"
            "Proporcione el dato:"
        ]
        await message.answer(random.choice(frases_error))
        return
    
    if len(color) > 20:
        frases_error_largo = [
            "⚠️ COLOR DEMASIADO LARGO\n\n"
            "El color no puede exceder 20 caracteres.\n"
            "Por favor, simplifique la descripción.\n\n"
            "Intente nuevamente:",
            
            "❌ DESCRIPCIÓN EXCESIVA\n\n"
            "Máximo 20 caracteres para el color.\n"
            "Use descripciones simples como AZUL MARINO.\n\n"
            "Favor de ajustar:",
            
            "🔍 LÍMITE EXCEDIDO\n\n"
            "El color no debe superar 20 caracteres.\n"
            "Ejemplos: ROJO, BLANCO PERLA, GRIS OXFORD\n\n"
            "Corrija la entrada:"
        ]
        await message.answer(random.choice(frases_error_largo))
        return
    
    await state.update_data(color=color)
    
    frases_color = [
        f"✅ COLOR CONFIRMADO: {color}\n\n"
        f"Finalmente, proporcione el NOMBRE COMPLETO del propietario del vehículo:",
        
        f"🎨 COLOR REGISTRADO: {color}\n\n"
        f"Perfecto. Ahora ingrese el NOMBRE COMPLETO del titular:",
        
        f"🎯 COLOR VALIDADO: {color}\n\n"
        f"Excelente. Especifique el NOMBRE COMPLETO del propietario:",
        
        f"💾 COLOR CAPTURADO: {color}\n\n"
        f"Muy bien. Proporcione el NOMBRE COMPLETO del titular del vehículo:"
    ]
    await message.answer(random.choice(frases_color))
    await state.set_state(PermisoForm.nombre)

@dp.message(PermisoForm.nombre)
async def get_nombre(message: types.Message, state: FSMContext):
    datos = await state.get_data()
    nombre = message.text.strip().upper()
    
    # Validar nombre
    if len(nombre) < 5:
        frases_error = [
            "⚠️ NOMBRE INCOMPLETO\n\n"
            "Por favor, ingrese el nombre completo del titular.\n"
            "Debe incluir nombre(s) y apellido(s).\n\n"
            "Ejemplo: JUAN PÉREZ GARCÍA\n\n"
            "Intente nuevamente:",
            
            "❌ INFORMACIÓN INSUFICIENTE\n\n"
            "Requiere nombre completo del propietario.\n"
            "Incluya nombre y apellidos completos.\n\n"
            "Ejemplo: MARÍA GONZÁLEZ LÓPEZ\n\n"
            "Favor de completar:",
            
            "🔍 DATO INCOMPLETO\n\n"
            "El nombre debe incluir nombre(s) y apellido(s).\n"
            "Mínimo 5 caracteres requeridos.\n\n"
            "Ejemplo: CARLOS MÉNDEZ RUIZ\n\n"
            "Proporcione nombre completo:"
        ]
        await message.answer(random.choice(frases_error))
        return
    
    if len(nombre) > 60:
        frases_error_largo = [
            "⚠️ NOMBRE DEMASIADO LARGO\n\n"
            "El nombre no puede exceder 60 caracteres.\n"
            "Por favor, verifique la información.\n\n"
            "Intente nuevamente:",
            
            "❌ LÍMITE EXCEDIDO\n\n"
            "Máximo 60 caracteres para el nombre completo.\n"
            "Simplifique si es necesario.\n\n"
            "Favor de ajustar:",
            
            "🔍 INFORMACIÓN EXCESIVA\n\n"
            "El nombre no debe superar 60 caracteres.\n"
            "Verifique que sea únicamente el nombre.\n\n"
            "Corrija la entrada:"
        ]
        await message.answer(random.choice(frases_error_largo))
        return
    
    # Verificar que tenga al menos dos palabras
    palabras = nombre.split()
    if len(palabras) < 2:
        frases_error_palabras = [
            "⚠️ NOMBRE INCOMPLETO\n\n"
            "Por favor, proporcione al menos nombre y apellido.\n"
            "Ejemplo: MARÍA GONZÁLEZ\n\n"
            "Intente nuevamente:",
            
            "❌ INFORMACIÓN INSUFICIENTE\n\n"
            "Debe incluir mínimo nombre y un apellido.\n"
            "Ejemplo: JOSÉ MARTÍNEZ\n\n"
            "Favor de completar:",
            
            "🔍 DATOS FALTANTES\n\n"
            "Requiere al menos 2 palabras (nombre y apellido).\n"
            "Ejemplo: ANA LÓPEZ\n\n"
            "Proporcione información completa:"
        ]
        await message.answer(random.choice(frases_error_palabras))
        return
    
    datos["nombre"] = nombre
    
    # Generar folio único de Guerrero
    datos["folio"] = generar_folio_guerrero()

    # Fechas
    hoy = datetime.now()
    vigencia_dias = 30
    fecha_ven = hoy + timedelta(days=vigencia_dias)
    
    datos["fecha_exp"] = hoy.strftime("%d/%m/%Y")
    datos["fecha_ven"] = fecha_ven.strftime("%d/%m/%Y")
    datos["vigencia"] = fecha_ven.strftime("%d/%m/%Y")

    try:
        frases_procesando = [
            f"🔄 PROCESANDO DOCUMENTACIÓN OFICIAL...\n\n"
            f"📄 Folio asignado: {datos['folio']}\n"
            f"🚗 Vehículo: {datos['marca']} {datos['linea']} {datos['anio']}\n"
            f"👤 Titular: {nombre}\n\n"
            f"El sistema está generando su documentación. Por favor espere...",
            
            f"⚡ GENERANDO DOCUMENTOS ESTATALES...\n\n"
            f"🆔 Código único: {datos['folio']}\n"
            f"🚙 Unidad: {datos['marca']} {datos['linea']} ({datos['anio']})\n"
            f"👥 Propietario: {nombre}\n\n"
            f"Procesando información en el sistema gubernamental...",
            
            f"🎯 EJECUTANDO TRÁMITE OFICIAL...\n\n"
            f"📋 Expediente: {datos['folio']}\n"
            f"🔧 Vehículo: {datos['marca']} {datos['linea']} modelo {datos['anio']}\n"
            f"📝 Solicitante: {nombre}\n\n"
            f"Generando documentación oficial del Estado de Guerrero..."
        ]
        await message.answer(random.choice(frases_procesando))
        
        # Generar PDFs
        p1 = generar_pdf_principal(datos)
        p2 = generar_pdf_flask(datos["folio"], hoy, fecha_ven, datos["nombre"])

        # Enviar documentos
        await message.answer_document(
            FSInputFile(p1),
            caption=f"📋 PERMISO OFICIAL DE CIRCULACIÓN - GUERRERO\n"
                   f"Folio: {datos['folio']}\n"
                   f"Vigencia: 30 días\n"
                   f"🏛️ Documento con validez oficial"
        )
        
        if p2:
            await message.answer_document(
                FSInputFile(p2),
                caption=f"🧾 COMPROBANTE DE VERIFICACIÓN\n"
                       f"Folio: {datos['folio']}\n"
                       f"📋 Documento complementario"
            )

        # Guardar en base de datos
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
            
            # INICIAR TIMER DE ELIMINACIÓN AUTOMÁTICA (2 HORAS)
            await iniciar_timer_eliminacion(message.from_user.id, datos['folio'])
            
            # Mensaje con información de pago actualizada
            await message.answer(
                f"💰 INSTRUCCIONES PARA EL PAGO\n\n"
                f"📄 Folio: {datos['folio']}\n"
                f"💵 Monto: $50 pesos mexicanos\n"
                f"⏰ Tiempo límite: 2 horas\n\n"
                
                "🏪 TRANSFERENCIA SPIN BY OXXO:\n"
                "• Titular: GUILLERMO S.R.J\n"
                "• Número: 7289690000484424454\n\n"
                
                "💳 DEPÓSITO DIRECTO EN CAJA OXXO:\n"
                "• SOLO OXXO GUILLERMO. S.R.J\n"
                "• Referencia: 2242170180214090\n"
                "• Cantidad exacta: $50.00\n\n"
                
                f"📸 IMPORTANTE: Una vez efectuado el pago, envíe la fotografía de su comprobante para la validación correspondiente.\n\n"
                f"⚠️ ADVERTENCIA: Si no completa el pago en las próximas 2 horas, el folio {datos['folio']} será eliminado automáticamente del sistema."
            )
            
        except Exception as e:
            print(f"Error guardando en Supabase: {e}")
            await message.answer(f"⚠️ ADVERTENCIA: PDFs generados pero error en registro: {str(e)}")
        
    except Exception as e:
        await message.answer(f"❌ ERROR EN EL SISTEMA: {str(e)}")
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
                "ℹ️ NO HAY PERMISOS PENDIENTES DE PAGO\n\n"
                "No se encontró ningún permiso pendiente de pago para su cuenta.\n\n"
                "Si desea tramitar un nuevo permiso, utilice /permiso",
                
                "📄 SIN TRÁMITES ACTIVOS\n\n"
                "No tiene folios pendientes de validación de pago.\n\n"
                "Para iniciar un nuevo trámite: /permiso",
                
                "🔍 NO HAY FOLIOS VIGENTES\n\n"
                "No se localizaron permisos esperando comprobante de pago.\n\n"
                "Comando para nuevo permiso: /permiso"
            ]
            await message.answer(random.choice(frases_sin_folios))
            return
        
        # Si tiene varios folios, preguntar cuál
        if len(folios_usuario) > 1:
            lista_folios = '\n'.join([f"• {folio}" for folio in folios_usuario])
            pending_comprobantes[user_id] = "waiting_folio"
            await message.answer(
                f"📄 MÚLTIPLES FOLIOS ACTIVOS\n\n"
                f"Tiene {len(folios_usuario)} folios pendientes de pago:\n\n"
                f"{lista_folios}\n\n"
                f"Por favor, responda con el NÚMERO DE FOLIO al que corresponde este comprobante.\n"
                f"Ejemplo: {folios_usuario[0]}"
            )
            return
        
        # Solo un folio activo, procesar automáticamente
        folio = folios_usuario[0]
        
        # Cancelar timer de eliminación
        cancelar_timer_folio(folio)
        
        # Actualizar estado en base de datos
        try:
            supabase.table("folios_registrados").update({
                "estado": "COMPROBANTE_ENVIADO",
                "fecha_comprobante": datetime.now().isoformat()
            }).eq("folio", folio).execute()
            
            frases_comprobante_recibido = [
                f"✅ COMPROBANTE RECIBIDO CORRECTAMENTE\n\n"
                f"📄 Folio: {folio}\n"
                f"📸 Su comprobante de pago ha sido recibido exitosamente\n"
                f"⏰ Timer de eliminación automática detenido\n\n"
                f"🔍 Su comprobante está siendo verificado por nuestro equipo.\n"
                f"Una vez validado el pago, su permiso quedará completamente activo.\n\n"
                f"Gracias por utilizar el Sistema Digital del Estado de Guerrero.",
                
                f"💾 COMPROBANTE ALMACENADO EN EL SISTEMA\n\n"
                f"📋 Expediente: {folio}\n"
                f"📷 Imagen del comprobante registrada correctamente\n"
                f"🛑 Eliminación automática cancelada\n\n"
                f"⚡ Proceso de validación iniciado automáticamente.\n"
                f"Su permiso será activado una vez confirmado el pago.\n\n"
                f"Agradecemos su confianza en nuestro sistema.",
                
                f"🎯 COMPROBANTE PROCESADO EXITOSAMENTE\n\n"
                f"🆔 Código: {folio}\n"
                f"📊 Estado: Comprobante en verificación\n"
                f"⏹️ Timer de eliminación detenido\n\n"
                f"🔄 Su documentación será validada en breve.\n"
                f"El permiso estará disponible tras confirmar el pago.\n\n"
                f"Sistema Digital de Guerrero - Servicio de excelencia."
            ]
            await message.answer(random.choice(frases_comprobante_recibido))
            
        except Exception as e:
            print(f"Error actualizando estado comprobante: {e}")
            await message.answer(
                f"✅ COMPROBANTE RECIBIDO\n\n"
                f"📄 Folio: {folio}\n"
                f"📸 Su comprobante fue recibido y el timer se detuvo.\n\n"
                f"⚠️ Hubo un problema menor actualizando el estado en el sistema, pero su comprobante está guardado.\n\n"
                f"Si tiene dudas, mencione este folio: {folio}"
            )
            
    except Exception as e:
        print(f"[ERROR] recibir_comprobante: {e}")
        await message.answer(
            "❌ ERROR PROCESANDO COMPROBANTE\n\n"
            "Ocurrió un problema al procesar su imagen.\n"
            "Por favor, intente enviar nuevamente la fotografía de su comprobante.\n\n"
            "Si el problema persiste, contacte al soporte técnico."
        )

# Handler para cuando el usuario especifica el folio para el comprobante
@dp.message(lambda message: message.from_user.id in pending_comprobantes and pending_comprobantes[message.from_user.id] == "waiting_folio")
async def especificar_folio_comprobante(message: types.Message):
    try:
        user_id = message.from_user.id
        folio_especificado = message.text.strip().upper()
        
        folios_usuario = obtener_folios_usuario(user_id)
        
        if folio_especificado not in folios_usuario:
            await message.answer(
                f"❌ FOLIO NO ENCONTRADO\n\n"
                f"El folio '{folio_especificado}' no está en sus folios activos.\n\n"
                f"Sus folios activos son:\n" + 
                '\n'.join([f"• {f}" for f in folios_usuario]) +
                f"\n\nPor favor, verifique e ingrese un folio válido:"
            )
            return
        
        # Folio válido - cancelar timer
        cancelar_timer_folio(folio_especificado)
        
        # Limpiar estado pending
        del pending_comprobantes[user_id]
        
        # Actualizar en base de datos
        try:
            supabase.table("folios_registrados").update({
                "estado": "COMPROBANTE_ENVIADO",
                "fecha_comprobante": datetime.now().isoformat()
            }).eq("folio", folio_especificado).execute()
            
            await message.answer(
                f"✅ FOLIO CONFIRMADO Y COMPROBANTE PROCESADO\n\n"
                f"📄 Folio: {folio_especificado}\n"
                f"📸 Su comprobante ha sido asociado correctamente\n"
                f"⏰ Timer de eliminación automática detenido\n\n"
                f"🔍 Su comprobante está siendo verificado.\n"
                f"Una vez validado el pago, su permiso quedará activo.\n\n"
                f"Ahora puede enviar el comprobante de pago como imagen."
            )
            
        except Exception as e:
            print(f"Error actualizando estado: {e}")
            await message.answer(
                f"✅ FOLIO CONFIRMADO\n\n"
                f"📄 Folio: {folio_especificado}\n"
                f"⏰ Timer detenido\n\n"
                f"Ahora envíe la imagen del comprobante de pago."
            )
            
    except Exception as e:
        print(f"[ERROR] especificar_folio_comprobante: {e}")
        if user_id in pending_comprobantes:
            del pending_comprobantes[user_id]
        await message.answer("❌ Error procesando el folio. Intente nuevamente.")

# Comando para ver folios activos
@dp.message(Command("folios"))
async def ver_folios_activos(message: types.Message):
    try:
        user_id = message.from_user.id
        folios_usuario = obtener_folios_usuario(user_id)
        
        if not folios_usuario:
            frases_sin_folios = [
                "ℹ️ NO HAY FOLIOS ACTIVOS\n\n"
                "No tiene folios pendientes de pago en este momento.\n\n"
                "Para crear un nuevo permiso utilice /permiso",
                
                "📄 SIN TRÁMITES VIGENTES\n\n"
                "No se encontraron folios activos para su cuenta.\n\n"
                "Comando para nuevo permiso: /permiso",
                
                "🔍 ESTADO: SIN FOLIOS PENDIENTES\n\n"
                "Actualmente no tiene permisos esperando pago.\n\n"
                "Inicie nuevo trámite con: /permiso"
            ]
            await message.answer(random.choice(frases_sin_folios))
            return
        
        lista_folios = []
        for folio in folios_usuario:
            if folio in timers_activos:
                tiempo_transcurrido = int((datetime.now() - timers_activos[folio]["start_time"]).total_seconds() / 60)
                tiempo_restante = max(0, 120 - tiempo_transcurrido)
                lista_folios.append(f"• {folio} ({tiempo_restante} min restantes)")
            else:
                lista_folios.append(f"• {folio} (timer detenido)")
        
        await message.answer(
            f"📋 SUS FOLIOS ACTIVOS ({len(folios_usuario)})\n\n"
            + '\n'.join(lista_folios) +
            f"\n\n⏰ Cada folio tiene timer independiente de 2 horas.\n"
            f"📸 Para enviar comprobante, use una imagen.\n"
            f"💰 Costo por permiso: $50 pesos"
        )
        
    except Exception as e:
        print(f"[ERROR] ver_folios_activos: {e}")
        await message.answer("❌ Error consultando folios activos.")

# Handler para preguntas sobre costo
@dp.message(lambda message: message.text and any(palabra in message.text.lower() for palabra in [
    'costo', 'precio', 'cuanto', 'cuánto', 'deposito', 'depósito', 'pago', 'valor', 'monto'
]))
async def responder_costo(message: types.Message):
    try:
        frases_costo = [
            "💰 INFORMACIÓN SOBRE LA INVERSIÓN\n\n"
            "El costo del permiso es de $50 pesos mexicanos.\n"
            "Vigencia: 30 días\n"
            "Pago: OXXO (Transferencia SPIN o depósito directo)\n\n"
            "Para iniciar su trámite utilice /permiso",
            
            "💵 TARIFA OFICIAL - ESTADO DE GUERRERO\n\n"
            "Inversión requerida: $50.00 MXN\n"
            "Periodo de validez: 30 días naturales\n"
            "Modalidad de pago: Establecimientos OXXO\n\n"
            "Comando de inicio: /permiso",
            
            "🏪 COSTO Y MODALIDADES DE PAGO\n\n"
            "Precio: Cincuenta pesos mexicanos ($50)\n"
            "Duración: Un mes (30 días)\n"
            "Pago disponible: Tiendas OXXO únicamente\n\n"
            "Inicie con: /permiso"
        ]
        await message.answer(random.choice(frases_costo))
    except Exception as e:
        print(f"[ERROR] responder_costo: {e}")
        await message.answer("💰 Costo del permiso: $50 pesos. Use /permiso para tramitar.")

@dp.message()
async def fallback(message: types.Message):
    respuestas_random = [
        "🏛️ Sistema Digital del Estado de Guerrero. Para tramitar su permiso utilice /permiso",
        "📋 Plataforma gubernamental de servicios. Comando disponible: /permiso",
        "⚡ Sistema en línea activo. Use /permiso para generar su documento oficial",
        "🚗 Servicio de permisos de Guerrero. Inicie su proceso con /permiso",
        "💰 Costo: $50 pesos. Vigencia: 30 días. Comando: /permiso",
        "🎯 Sistema automatizado. Para permisos vehiculares: /permiso"
    ]
    await message.answer(random.choice(respuestas_random))

# ------------ FASTAPI + LIFESPAN ------------
_keep_task = None

async def keep_alive():
    """Mantiene el bot activo con pings periódicos"""
    while True:
        await asyncio.sleep(600)  # 10 minutos
        print("[HEARTBEAT] Sistema activo")

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _keep_task
    
    try:
        # Configurar webhook
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
            "timer_eliminacion": "2 horas",
            "active_timers": len(timers_activos),
            "independent_timers": True
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}

@app.get("/status")
async def status_detail():
    """Endpoint de diagnóstico detallado"""
    try:
        return {
            "sistema": "Guerrero Digital v2.0 - Timers Independientes",
            "entidad": "Guerrero",
            "costo": "$50 pesos mexicanos",
            "vigencia_dias": 30,
            "tiempo_eliminacion": "2 horas",
            "total_timers_activos": len(timers_activos),
            "folios_con_timer": list(timers_activos.keys()),
            "usuarios_con_folios": len(user_folios),
            "detalle_usuarios": {str(uid): folios for uid, folios in user_folios.items()},
            "timestamp": datetime.now().isoformat(),
            "status": "Operacional - Eliminación automática activa"
        }
    except Exception as e:
        return {"error": str(e), "status": "Error"}

if __name__ == '__main__':
    try:
        import uvicorn
        port = int(os.getenv("PORT", 8000))
        print(f"[ARRANQUE] Iniciando servidor en puerto {port}")
        print(f"[SISTEMA] Timers de eliminación independientes habilitados")
        print(f"[CONFIG] Costo: $50 MXN - Vigencia: 30 días - Auto-eliminación: 2 horas")
        uvicorn.run(app, host="0.0.0.0", port=port)
    except Exception as e:
        print(f"[ERROR FATAL] No se pudo iniciar el servidor: {e}")
