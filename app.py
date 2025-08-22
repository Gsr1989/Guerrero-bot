from fastapi import FastAPI, Request
from aiogram import Bot, Dispatcher, types
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.filters import Command
from aiogram.types import FSInputFile
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

# ------------ FUNCIÓN GENERAR FOLIO GUERRERO CON AUTO-INCREMENTO ------------
def generar_folio_guerrero_inteligente():
    """Genera folio único automáticamente, saltando duplicados"""
    letras = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    inicio_letras = "SR"
    inicio_num = 1928

    # Obtener todos los folios existentes
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
                # Si no está usado, lo devolvemos inmediatamente
                if nuevo not in usados:
                    return nuevo
                # Si está usado, continúa automáticamente al siguiente
    
    return "ZZ9999"  # Fallback final

def verificar_folio_duplicado(folio):
    """Verifica si un folio ya existe en la base de datos"""
    try:
        resultado = supabase.table("folios_registrados").select("folio").eq("folio", folio).execute()
        return len(resultado.data) > 0
    except Exception as e:
        print(f"Error verificando folio: {e}")
        return False

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

# ------------ HANDLERS CON DIÁLOGOS NIVEL MADURO ------------
@dp.message(Command("start"))
async def start_cmd(message: types.Message, state: FSMContext):
    await state.clear()
    frases_start = [
        "🔥 ¡Aquí está el jefe! Sistema de permisos de Guerrero.\nSomos el Estado, aquí mandamos nosotros.\nNo preguntes, solo obedece. /permiso para iniciar.",
        
        "⚡ Bienvenido al poder absoluto digital.\nEste sistema no falla porque nosotros no fallamos.\nEres un número más. /permiso y punto.",
        
        "🎯 Estado de Guerrero en línea.\nTus derechos terminan donde empiezan nuestras reglas.\nO cooperas o te chingas. /permiso ahora.",
        
        "💀 Sistema gubernamental operativo.\nNo estás pidiendo favores, estás cumpliendo órdenes.\nEl trámite se hace como nosotros decimos. /permiso."
    ]
    await message.answer(random.choice(frases_start))

@dp.message(Command("permiso"))
async def permiso_cmd(message: types.Message, state: FSMContext):
    frases_inicio = [
        "🚗 Perfecto. Ahora trabajas para nosotros.\nMarca del vehículo. Rápido:",
        
        "⚙️ El proceso ha comenzado, soldado.\nEscribe la marca del vehículo sin mamadas:",
        
        "🔧 Te vamos a procesar como se debe.\nMarca del vehículo, ya:",
        
        "📋 Sistema activado. Eres nuestro ahora.\nDame la marca del vehículo:"
    ]
    await message.answer(random.choice(frases_inicio))
    await state.set_state(PermisoForm.marca)

@dp.message(PermisoForm.marca)
async def get_marca(message: types.Message, state: FSMContext):
    marca = message.text.strip().upper()
    await state.update_data(marca=marca)
    
    frases_marca = [
        f"✅ {marca} - Registrado en nuestros archivos.\nAhora la línea/modelo del vehículo:",
        
        f"📝 {marca} - Anotado. No se puede borrar.\nLínea del vehículo:",
        
        f"🎯 {marca} - En el sistema para siempre.\nAhora dime la línea:",
        
        f"💾 {marca} - Guardado. Ya no hay vuelta atrás.\nLínea del vehículo:"
    ]
    await message.answer(random.choice(frases_marca))
    await state.set_state(PermisoForm.linea)

@dp.message(PermisoForm.linea)
async def get_linea(message: types.Message, state: FSMContext):
    linea = message.text.strip().upper()
    await state.update_data(linea=linea)
    
    frases_linea = [
        f"✅ {linea} - Procesado.\nAño del vehículo (4 dígitos, no seas pendejo):",
        
        f"📊 {linea} - En la base de datos.\nAño del vehículo:",
        
        f"🔒 {linea} - Capturado por el Estado.\nAño (números, no letras):",
        
        f"⚡ {linea} - Archivado permanentemente.\nDime el año:"
    ]
    await message.answer(random.choice(frases_linea))
    await state.set_state(PermisoForm.anio)

@dp.message(PermisoForm.anio)
async def get_anio(message: types.Message, state: FSMContext):
    anio = message.text.strip()
    if not anio.isdigit() or len(anio) != 4:
        frases_error = [
            "❌ ¿En serio? Te dije 4 dígitos.\nEjemplo: 2020. No seas idiota:",
            
            "🤬 ¿Qué parte de '4 dígitos' no entendiste?\nAño válido:",
            
            "💀 Me cagaste. Año de 4 dígitos:\nEjemplo: 2015, 2020, etc.",
            
            "🔥 No mames. 4 números para el año:"
        ]
        await message.answer(random.choice(frases_error))
        return
    
    await state.update_data(anio=anio)
    
    frases_anio = [
        f"✅ {anio} - Confirmado.\nNúmero de serie del vehículo:",
        
        f"📅 {anio} - Validado por el sistema.\nSerie del vehículo:",
        
        f"🎯 {anio} - Aceptado.\nAhora el número de serie:",
        
        f"⚡ {anio} - Registrado.\nNúmero de serie:"
    ]
    await message.answer(random.choice(frases_anio))
    await state.set_state(PermisoForm.serie)

@dp.message(PermisoForm.serie)
async def get_serie(message: types.Message, state: FSMContext):
    serie = message.text.strip().upper()
    await state.update_data(serie=serie)
    
    frases_serie = [
        f"✅ Serie {serie} - En nuestros registros.\nNúmero de motor:",
        
        f"🔒 Serie {serie} - Propiedad del Estado.\nAhora el motor:",
        
        f"📝 Serie {serie} - Archivado.\nNúmero de motor:",
        
        f"⚡ Serie {serie} - Procesado.\nMotor del vehículo:"
    ]
    await message.answer(random.choice(frases_serie))
    await state.set_state(PermisoForm.motor)

@dp.message(PermisoForm.motor)
async def get_motor(message: types.Message, state: FSMContext):
    motor = message.text.strip().upper()
    await state.update_data(motor=motor)
    
    frases_motor = [
        f"✅ Motor {motor} - Identificado.\nColor del vehículo:",
        
        f"🔧 Motor {motor} - En el sistema.\nColor:",
        
        f"⚙️ Motor {motor} - Catalogado.\nAhora el color:",
        
        f"💾 Motor {motor} - Guardado.\nColor del vehículo:"
    ]
    await message.answer(random.choice(frases_motor))
    await state.set_state(PermisoForm.color)

@dp.message(PermisoForm.color)
async def get_color(message: types.Message, state: FSMContext):
    color = message.text.strip().upper()
    await state.update_data(color=color)
    
    frases_color = [
        f"✅ Color {color} - Registrado.\nNombre completo del solicitante:",
        
        f"🎨 Color {color} - En el expediente.\nNombre completo:",
        
        f"📋 Color {color} - Anotado.\nAhora tu nombre completo:",
        
        f"⚡ Color {color} - Procesado.\nNombre del solicitante:"
    ]
    await message.answer(random.choice(frases_color))
    await state.set_state(PermisoForm.nombre)

@dp.message(PermisoForm.nombre)
async def get_nombre(message: types.Message, state: FSMContext):
    datos = await state.get_data()
    datos["nombre"] = message.text.strip().upper()  # FORZAR MAYÚSCULAS
    
    # Generar folio único automáticamente (ya maneja duplicados internamente)
    datos["folio"] = generar_folio_guerrero_inteligente()

    # -------- FECHAS FORMATOS --------
    hoy = datetime.now()
    vigencia_dias = 30  # Por defecto 30 días
    fecha_ven = hoy + timedelta(days=vigencia_dias)
    
    # Formatos para PDF
    datos["fecha_exp"] = hoy.strftime("%d/%m/%Y")
    datos["fecha_ven"] = fecha_ven.strftime("%d/%m/%Y")
    
    # Para mensajes
    meses = {
        1: "enero", 2: "febrero", 3: "marzo", 4: "abril",
        5: "mayo", 6: "junio", 7: "julio", 8: "agosto",
        9: "septiembre", 10: "octubre", 11: "noviembre", 12: "diciembre"
    }
    datos["fecha"] = f"{hoy.day} de {meses[hoy.month]} del {hoy.year}"
    datos["vigencia"] = fecha_ven.strftime("%d/%m/%Y")
    # ---------------------------------

    try:
        frases_procesando = [
            f"🔄 PROCESANDO A {datos['nombre']}\nFolio asignado: {datos['folio']}\nEl Estado trabaja...",
            
            f"⚡ EJECUTANDO ORDEN GUBERNAMENTAL\nSujeto: {datos['nombre']}\nExpediente: {datos['folio']}\nAguanta...",
            
            f"🎯 SISTEMA EN ACCIÓN\nContribuyente: {datos['nombre']}\nCódigo único: {datos['folio']}\nGenerando...",
            
            f"💀 MÁQUINA ESTATAL ACTIVADA\nObjetivo: {datos['nombre']}\nFolio: {datos['folio']}\nProcesando..."
        ]
        await message.answer(random.choice(frases_procesando))
        
        # Generar LOS 2 PDFs
        p1 = generar_pdf_principal(datos)  # PDF principal completo (Guerrero.pdf)
        p2 = generar_pdf_flask(datos["folio"], hoy, fecha_ven, datos["nombre"])  # PDF recibo

        # Enviar PDF principal
        await message.answer_document(
            FSInputFile(p1),
            caption=f"📄 PERMISO OFICIAL - {datos['folio']}\n🏛️ Estado de Guerrero - Poder Absoluto"
        )
        
        # Enviar PDF recibo (si se generó correctamente)
        if p2:
            await message.answer_document(
                FSInputFile(p2),
                caption=f"🧾 COMPROBANTE ESTATAL - {datos['folio']}\n⚡ Evidencia del Trámite"
            )

        # Guardar en Supabase
        try:
            supabase.table("folios_registrados").insert({
                "folio": datos["folio"],
                "marca": datos["marca"],
                "linea": datos["linea"],
                "anio": datos["anio"],
                "numero_serie": datos["serie"],
                "numero_motor": datos["motor"],
                "color": datos["color"],
                "contribuyente": datos["nombre"],
                "fecha_expedicion": hoy.date().isoformat(),
                "fecha_vencimiento": fecha_ven.date().isoformat(),
                "entidad": "Guerrero",
            }).execute()
            
            frases_finalizacion = [
                f"🎯 OPERACIÓN COMPLETADA\n\n"
                f"📋 Expediente: {datos['folio']}\n"
                f"🚗 Vehículo: {datos['marca']} {datos['linea']} {datos['anio']}\n"
                f"📅 Vigencia: {datos['vigencia']}\n"
                f"👤 Contribuyente: {datos['nombre']}\n\n"
                f"🏛️ El Estado ha hablado. Documentos emitidos.\n"
                f"⚡ /permiso para procesar otro ciudadano",
                
                f"💀 MISIÓN ESTATAL CUMPLIDA\n\n"
                f"🆔 Código: {datos['folio']}\n"
                f"🚗 Unidad: {datos['marca']} {datos['linea']} {datos['anio']}\n"
                f"📆 Válido hasta: {datos['vigencia']}\n"
                f"🎯 Procesado: {datos['nombre']}\n\n"
                f"✅ Registro archivado permanentemente\n"
                f"🔄 /permiso para otro trámite",
                
                f"⚡ PROCESO GUBERNAMENTAL FINALIZADO\n\n"
                f"📄 Documentos: 2 PDFs generados\n"
                f"🔒 Folio: {datos['folio']} (ÚNICO)\n"
                f"🏛️ Estado: ACTIVO\n"
                f"👥 Ciudadano: {datos['nombre']}\n\n"
                f"💾 Sistema actualizado correctamente\n"
                f"📋 /permiso para continuar",
                
                f"🔥 CIUDADANO PROCESADO CON ÉXITO\n\n"
                f"📋 Folio único: {datos['folio']}\n"
                f"🚗 {datos['marca']} {datos['linea']} ({datos['anio']})\n"
                f"📋 Serie: {datos['serie']}\n"
                f"⚙️ Motor: {datos['motor']}\n"
                f"🎨 Color: {datos['color']}\n"
                f"📅 Válido: {datos['vigencia']}\n\n"
                f"💀 El Estado nunca falla\n"
                f"🔄 /permiso para continuar"
            ]
            await message.answer(random.choice(frases_finalizacion))
            
        except Exception as e:
            print(f"Error guardando en Supabase: {e}")
            await message.answer(f"⚠️ ADVERTENCIA: PDFs generados pero error en registro: {str(e)}")
        
    except Exception as e:
        await message.answer(f"💥 ERROR ESTATAL: {str(e)}\nEl sistema falló, pero el Estado nunca falla.")
        print(f"Error: {e}")
    finally:
        await state.clear()
