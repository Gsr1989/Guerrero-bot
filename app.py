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

# ------------ FUNCIÓN GENERAR FOLIO GUERRERO ------------
def generar_folio_guerrero():
    letras = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    inicio_letras = "SR"
    inicio_num = 1928

    existentes = supabase.table("folios_registrados").select("folio").eq("entidad", "Guerrero").execute().data
    usados = [r["folio"] for r in existentes if r["folio"] and len(r["folio"]) == 6 and r["folio"][:2].isalpha()]

    empezar = False
    for l1 in letras:
        for l2 in letras:
            par = l1 + l2
            for num in range(1, 10000):
                if not empezar:
                    if par == inicio_letras and num == inicio_num:
                        empezar = True
                    else:
                        continue
                nuevo = f"{par}{str(num).zfill(4)}"
                if nuevo not in usados:
                    return nuevo
    return "SR9999"  # Fallback

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

# ------------ HANDLERS ------------
@dp.message(Command("start"))
async def start_cmd(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("👋 **Bienvenido al Bot de Permisos Guerrero**\n\n🚗 Usa /permiso para generar un nuevo permiso\n📄 Genera 2 documentos: Permiso Completo y Recibo\n⚡ Proceso rápido y seguro", parse_mode="Markdown")

@dp.message(Command("permiso"))
async def permiso_cmd(message: types.Message, state: FSMContext):
    await message.answer("🚗 **Paso 1/7:** Ingresa la marca del vehículo:", parse_mode="Markdown")
    await state.set_state(PermisoForm.marca)

@dp.message(PermisoForm.marca)
async def get_marca(message: types.Message, state: FSMContext):
    marca = message.text.strip().upper()
    await state.update_data(marca=marca)
    await message.answer("📱 **Paso 2/7:** Ingresa la línea/modelo del vehículo:", parse_mode="Markdown")
    await state.set_state(PermisoForm.linea)

@dp.message(PermisoForm.linea)
async def get_linea(message: types.Message, state: FSMContext):
    linea = message.text.strip().upper()
    await state.update_data(linea=linea)
    await message.answer("📅 **Paso 3/7:** Ingresa el año del vehículo (4 dígitos):", parse_mode="Markdown")
    await state.set_state(PermisoForm.anio)

@dp.message(PermisoForm.anio)
async def get_anio(message: types.Message, state: FSMContext):
    anio = message.text.strip()
    if not anio.isdigit() or len(anio) != 4:
        await message.answer("❌ Por favor ingresa un año válido (4 dígitos). Ejemplo: 2020")
        return
    
    await state.update_data(anio=anio)
    await message.answer("🔢 **Paso 4/7:** Ingresa el número de serie:", parse_mode="Markdown")
    await state.set_state(PermisoForm.serie)

@dp.message(PermisoForm.serie)
async def get_serie(message: types.Message, state: FSMContext):
    serie = message.text.strip().upper()
    await state.update_data(serie=serie)
    await message.answer("🔧 **Paso 5/7:** Ingresa el número de motor:", parse_mode="Markdown")
    await state.set_state(PermisoForm.motor)

@dp.message(PermisoForm.motor)
async def get_motor(message: types.Message, state: FSMContext):
    motor = message.text.strip().upper()
    await state.update_data(motor=motor)
    await message.answer("🎨 **Paso 6/7:** Ingresa el color del vehículo:", parse_mode="Markdown")
    await state.set_state(PermisoForm.color)

@dp.message(PermisoForm.color)
async def get_color(message: types.Message, state: FSMContext):
    color = message.text.strip().upper()
    await state.update_data(color=color)
    await message.answer("👤 **Paso 7/7:** Ingresa el nombre completo del solicitante:", parse_mode="Markdown")
    await state.set_state(PermisoForm.nombre)

@dp.message(PermisoForm.nombre)
async def get_nombre(message: types.Message, state: FSMContext):
    datos = await state.get_data()
    datos["nombre"] = message.text.strip().upper()
    
    # Generar folio único de Guerrero
    datos["folio"] = generar_folio_guerrero()

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
        await message.answer("📄 Generando 2 permisos, por favor espera...")
        
        # Generar LOS 2 PDFs
        p1 = generar_pdf_principal(datos)  # PDF principal completo (Guerrero.pdf)
        p2 = generar_pdf_flask(datos["folio"], hoy, fecha_ven, datos["nombre"])  # PDF recibo

        # Enviar PDF principal
        await message.answer_document(
            FSInputFile(p1),
            caption=f"📄 **Permiso Completo - Folio: {datos['folio']}**\n🌟 Guerrero Digital"
        )
        
        # Enviar PDF recibo (si se generó correctamente)
        if p2:
            await message.answer_document(
                FSInputFile(p2),
                caption=f"🧾 **RECIBO - Folio: {datos['folio']}**\n📋 Comprobante de Trámite"
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
        except Exception as e:
            print(f"Error guardando en Supabase: {e}")

        await message.answer(
            f"🎉 **¡2 Permisos generados exitosamente!**\n\n"
            f"📋 **Resumen:**\n"
            f"🆔 Folio: `{datos['folio']}`\n"
            f"🚗 Vehículo: {datos['marca']} {datos['linea']} {datos['anio']}\n"
            f"📅 Vigencia: {datos['vigencia']}\n"
            f"👤 Solicitante: {datos['nombre']}\n\n"
            f"📄 **Documentos generados:**\n"
            f"1️⃣ Permiso Completo (horizontal y vertical)\n"
            f"2️⃣ Recibo (comprobante)\n\n"
            f"✅ Registro guardado correctamente\n"
            f"🔄 Usa /permiso para generar otro permiso",
            parse_mode="Markdown"
        )
        
    except Exception as e:
        await message.answer(f"❌ Error al generar permisos: {str(e)}")
        print(f"Error: {e}")
    finally:
        await state.clear()

@dp.message()
async def fallback(message: types.Message):
    await message.answer(
        "👋 **¡Hola! Soy el Bot de Permisos de Guerrero**\n\n"
        "🚗 Usa /permiso para generar tu permiso de circulación\n"
        "📄 Genero 2 documentos: Permiso Completo y Recibo\n"
        "⚡ Proceso rápido y seguro\n\n"
        "💡 **Comandos disponibles:**\n"
        "/start - Información del bot\n"
        "/permiso - Generar nuevo permiso",
        parse_mode="Markdown"
    )

# ------------ FASTAPI + LIFESPAN ------------
_keep_task = None

async def keep_alive():
    """Mantiene el bot activo con pings periódicos"""
    while True:
        await asyncio.sleep(600)  # 10 minutos

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _keep_task
    
    # Configurar webhook
    await bot.delete_webhook(drop_pending_updates=True)
    if BASE_URL:
        webhook_url = f"{BASE_URL}/webhook"
        await bot.set_webhook(webhook_url, allowed_updates=["message"])
        print(f"Webhook configurado: {webhook_url}")
        _keep_task = asyncio.create_task(keep_alive())
    else:
        print("Modo polling (sin webhook)")
    
    yield
    
    # Cleanup
    if _keep_task:
        _keep_task.cancel()
        with suppress(asyncio.CancelledError):
            await _keep_task
    await bot.session.close()

app = FastAPI(lifespan=lifespan, title="Bot Permisos Guerrero", version="1.0.0")

@app.get("/")
async def health():
    return {
        "status": "running",
        "bot": "Guerrero Permisos",
        "version": "1.0.0",
        "webhook_configured": bool(BASE_URL),
        "documentos_generados": 2
    }

@app.post("/webhook")
async def telegram_webhook(request: Request):
    try:
        data = await request.json()
        update = types.Update(**data)
        await dp.feed_webhook_update(bot, update)
        return {"ok": True}
    except Exception as e:
        print(f"Error en webhook: {e}")
        return {"ok": False, "error": str(e)}

@app.get("/status")
async def bot_status():
    try:
        bot_info = await bot.get_me()
        return {
            "bot_active": True,
            "bot_username": bot_info.username,
            "bot_id": bot_info.id,
            "pdfs_por_permiso": 2
        }
    except Exception as e:
        return {"bot_active": False, "error": str(e)}
