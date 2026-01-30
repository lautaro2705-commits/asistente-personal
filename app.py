import os
import re
import json
import tempfile
import requests
import locale
from datetime import datetime, timedelta
from flask import Flask, render_template, request, jsonify
from dotenv import load_dotenv
import anthropic
from twilio.rest import Client
from twilio.twiml.messaging_response import MessagingResponse
# import whisper  # Deshabilitado para deploy en la nube
import caldav
from icalendar import Calendar, Event
from apscheduler.schedulers.background import BackgroundScheduler
import pytz

load_dotenv(override=True)

# Configurar locale en español
try:
    locale.setlocale(locale.LC_TIME, 'es_ES.UTF-8')
except:
    try:
        locale.setlocale(locale.LC_TIME, 'es_AR.UTF-8')
    except:
        pass


# Leer API keys
def get_env_var(name):
    value = os.getenv(name)
    if not value:
        try:
            with open(".env") as f:
                for line in f:
                    if line.startswith(f"{name}="):
                        value = line.strip().split("=", 1)[1]
                        break
        except:
            pass
    return value


app = Flask(__name__)

# Configuración de clientes
anthropic_client = anthropic.Anthropic(api_key=get_env_var("ANTHROPIC_API_KEY"))
twilio_client = Client(
    get_env_var("TWILIO_ACCOUNT_SID"), get_env_var("TWILIO_AUTH_TOKEN")
)
TWILIO_WHATSAPP_NUMBER = get_env_var("TWILIO_WHATSAPP_NUMBER")

# Configuración iCloud
ICLOUD_EMAIL = get_env_var("ICLOUD_EMAIL")
ICLOUD_APP_PASSWORD = get_env_var("ICLOUD_APP_PASSWORD")
CALDAV_URL = "https://caldav.icloud.com"

# OpenAI API para transcripción de audio (Whisper API)
OPENAI_API_KEY = get_env_var("OPENAI_API_KEY")

# Historial de conversaciones por usuario
conversations = {}

# Almacena los números de WhatsApp registrados para recordatorios
registered_users = {}

# Zona horaria
TIMEZONE = pytz.timezone("America/Argentina/Buenos_Aires")

# Archivos de datos
DATA_DIR = os.path.dirname(os.path.abspath(__file__))
TASKS_FILE = os.path.join(DATA_DIR, "tasks.json")
NOTES_FILE = os.path.join(DATA_DIR, "notes.json")

# ==================== SISTEMA DE TAREAS ====================

def load_tasks():
    """Carga las tareas desde el archivo JSON"""
    if os.path.exists(TASKS_FILE):
        with open(TASKS_FILE, "r") as f:
            return json.load(f)
    return {}

def save_tasks(tasks):
    """Guarda las tareas en el archivo JSON"""
    with open(TASKS_FILE, "w") as f:
        json.dump(tasks, f, indent=2, ensure_ascii=False)

def add_task(user_id, task_text):
    """Agrega una tarea para un usuario"""
    tasks = load_tasks()
    if user_id not in tasks:
        tasks[user_id] = []

    task = {
        "id": len(tasks[user_id]) + 1,
        "text": task_text,
        "done": False,
        "created": datetime.now(TIMEZONE).isoformat()
    }
    tasks[user_id].append(task)
    save_tasks(tasks)
    return task

def complete_task(user_id, task_id):
    """Marca una tarea como completada"""
    tasks = load_tasks()
    if user_id in tasks:
        for task in tasks[user_id]:
            if task["id"] == task_id:
                task["done"] = True
                save_tasks(tasks)
                return True
    return False

def delete_task(user_id, task_id):
    """Elimina una tarea"""
    tasks = load_tasks()
    if user_id in tasks:
        tasks[user_id] = [t for t in tasks[user_id] if t["id"] != task_id]
        # Reordenar IDs
        for i, task in enumerate(tasks[user_id]):
            task["id"] = i + 1
        save_tasks(tasks)
        return True
    return False

def get_tasks(user_id, include_done=False):
    """Obtiene las tareas de un usuario"""
    tasks = load_tasks()
    user_tasks = tasks.get(user_id, [])
    if not include_done:
        user_tasks = [t for t in user_tasks if not t["done"]]
    return user_tasks

def format_tasks(user_id):
    """Formatea las tareas para mostrar"""
    tasks = get_tasks(user_id)
    if not tasks:
        return "No tienes tareas pendientes."

    result = "📋 *Tus tareas pendientes:*\n"
    for task in tasks:
        result += f"{task['id']}. {task['text']}\n"
    return result

# ==================== SISTEMA DE NOTAS ====================

def load_notes():
    """Carga las notas desde el archivo JSON"""
    if os.path.exists(NOTES_FILE):
        with open(NOTES_FILE, "r") as f:
            return json.load(f)
    return {}

def save_notes(notes):
    """Guarda las notas en el archivo JSON"""
    with open(NOTES_FILE, "w") as f:
        json.dump(notes, f, indent=2, ensure_ascii=False)

def add_note(user_id, note_text):
    """Agrega una nota para un usuario"""
    notes = load_notes()
    if user_id not in notes:
        notes[user_id] = []

    note = {
        "id": len(notes[user_id]) + 1,
        "text": note_text,
        "created": datetime.now(TIMEZONE).strftime("%Y-%m-%d %H:%M")
    }
    notes[user_id].append(note)
    save_notes(notes)
    return note

def get_notes(user_id):
    """Obtiene las notas de un usuario"""
    notes = load_notes()
    return notes.get(user_id, [])

def delete_note(user_id, note_id):
    """Elimina una nota"""
    notes = load_notes()
    if user_id in notes:
        notes[user_id] = [n for n in notes[user_id] if n["id"] != note_id]
        for i, note in enumerate(notes[user_id]):
            note["id"] = i + 1
        save_notes(notes)
        return True
    return False

def format_notes(user_id):
    """Formatea las notas para mostrar"""
    notes = get_notes(user_id)
    if not notes:
        return "No tienes notas guardadas."

    result = "📝 *Tus notas:*\n"
    for note in notes:
        result += f"{note['id']}. {note['text']} _({note['created']})_\n"
    return result

# ==================== CLIMA ====================

def get_weather(city="Cordoba,Argentina"):
    """Obtiene el clima usando wttr.in (gratis, sin API key)"""
    try:
        url = f"https://wttr.in/{city}?format=j1"
        response = requests.get(url, timeout=10)
        data = response.json()

        current = data["current_condition"][0]
        temp = current["temp_C"]
        feels_like = current["FeelsLikeC"]
        humidity = current["humidity"]
        desc = current["lang_es"][0]["value"] if "lang_es" in current else current["weatherDesc"][0]["value"]

        # Pronóstico de hoy
        today = data["weather"][0]
        max_temp = today["maxtempC"]
        min_temp = today["mintempC"]

        weather_info = f"""🌤 *Clima en {city}:*
🌡 Temperatura: {temp}°C (sensación {feels_like}°C)
📊 Máx: {max_temp}°C / Mín: {min_temp}°C
💧 Humedad: {humidity}%
📝 {desc}"""

        return weather_info
    except Exception as e:
        print(f"Error obteniendo clima: {e}")
        return "No pude obtener el clima en este momento."

# ==================== DÓLAR ====================

def get_dolar():
    """Obtiene cotización del dólar en Argentina"""
    try:
        response = requests.get("https://dolarapi.com/v1/dolares", timeout=10)
        data = response.json()

        result = "💵 *Cotización del Dólar:*\n"

        for d in data:
            nombre = d.get("nombre", "")
            compra = d.get("compra", 0)
            venta = d.get("venta", 0)

            if nombre == "Oficial":
                result += f"  • Oficial: ${compra:.0f} / ${venta:.0f}\n"
            elif nombre == "Blue":
                result += f"  • Blue: ${compra:.0f} / ${venta:.0f}\n"
            elif nombre == "MEP" or nombre == "Bolsa":
                result += f"  • MEP: ${compra:.0f} / ${venta:.0f}\n"

        return result
    except Exception as e:
        print(f"Error obteniendo dólar: {e}")
        return "💵 No pude obtener la cotización del dólar."

# ==================== GASTOS ====================

EXPENSES_FILE = os.path.join(DATA_DIR, "expenses.json")

def load_expenses():
    """Carga los gastos desde el archivo JSON"""
    if os.path.exists(EXPENSES_FILE):
        with open(EXPENSES_FILE, "r") as f:
            return json.load(f)
    return {}

def save_expenses(expenses):
    """Guarda los gastos en el archivo JSON"""
    with open(EXPENSES_FILE, "w") as f:
        json.dump(expenses, f, indent=2, ensure_ascii=False)

def add_expense(user_id, amount, description, category="General"):
    """Agrega un gasto"""
    expenses = load_expenses()
    if user_id not in expenses:
        expenses[user_id] = []

    expense = {
        "id": len(expenses[user_id]) + 1,
        "amount": amount,
        "description": description,
        "category": category,
        "date": datetime.now(TIMEZONE).strftime("%Y-%m-%d %H:%M")
    }
    expenses[user_id].append(expense)
    save_expenses(expenses)
    return expense

def get_expenses_summary(user_id, days=30):
    """Obtiene resumen de gastos del mes"""
    expenses = load_expenses()
    user_expenses = expenses.get(user_id, [])

    # Filtrar por fecha
    cutoff = datetime.now(TIMEZONE) - timedelta(days=days)
    recent = []
    for e in user_expenses:
        try:
            exp_date = datetime.strptime(e["date"], "%Y-%m-%d %H:%M")
            if exp_date >= cutoff.replace(tzinfo=None):
                recent.append(e)
        except:
            recent.append(e)

    if not recent:
        return "No tienes gastos registrados en los últimos 30 días."

    total = sum(e["amount"] for e in recent)

    # Agrupar por categoría
    by_category = {}
    for e in recent:
        cat = e.get("category", "General")
        by_category[cat] = by_category.get(cat, 0) + e["amount"]

    result = f"💰 *Gastos del mes:*\n"
    result += f"📊 Total: ${total:,.0f}\n\n"
    result += "*Por categoría:*\n"
    for cat, amount in sorted(by_category.items(), key=lambda x: -x[1]):
        result += f"  • {cat}: ${amount:,.0f}\n"

    result += f"\n*Últimos gastos:*\n"
    for e in recent[-5:]:
        result += f"  • ${e['amount']:,.0f} - {e['description']}\n"

    return result

# ==================== FRASE MOTIVACIONAL ====================

def get_motivational_quote():
    """Obtiene una frase motivacional"""
    quotes = [
        "El único modo de hacer un gran trabajo es amar lo que haces. - Steve Jobs",
        "El éxito es la suma de pequeños esfuerzos repetidos día tras día. - Robert Collier",
        "No esperes el momento perfecto, toma el momento y hazlo perfecto.",
        "Cada día es una nueva oportunidad para cambiar tu vida.",
        "La disciplina es el puente entre las metas y los logros. - Jim Rohn",
        "El fracaso es simplemente la oportunidad de comenzar de nuevo, esta vez de forma más inteligente. - Henry Ford",
        "Cree en ti mismo y todo será posible.",
        "La mejor manera de predecir el futuro es crearlo. - Peter Drucker",
        "No cuentes los días, haz que los días cuenten. - Muhammad Ali",
        "El éxito no es definitivo, el fracaso no es fatal: lo que cuenta es el coraje para continuar. - Winston Churchill",
        "Tu actitud determina tu dirección.",
        "Los grandes logros requieren tiempo y perseverancia.",
        "Hoy es un buen día para ser increíble.",
        "La única limitación es la que te pones a ti mismo.",
        "Convierte tus heridas en sabiduría. - Oprah Winfrey"
    ]
    import random
    return f"💫 _{random.choice(quotes)}_"

# ==================== NOTICIAS ====================

def get_news_argentina():
    """Obtiene las 5 noticias más importantes de Argentina"""
    try:
        # Usar Google News RSS para Argentina
        url = "https://news.google.com/rss/search?q=argentina&hl=es-419&gl=AR&ceid=AR:es-419"
        response = requests.get(url, timeout=10)

        import xml.etree.ElementTree as ET
        root = ET.fromstring(response.content)

        news = []
        for item in root.findall(".//item")[:3]:
            title = item.find("title").text
            # Limpiar el título (quitar la fuente)
            if " - " in title:
                title = title.rsplit(" - ", 1)[0]
            news.append(title)

        return news
    except Exception as e:
        print(f"Error obteniendo noticias Argentina: {e}")
        return []

def get_news_world():
    """Obtiene las 3 noticias más importantes del mundo (excluyendo Argentina)"""
    try:
        # Usar sección de noticias internacionales
        url = "https://news.google.com/rss/topics/CAAqJggKIiBDQkFTRWdvSUwyMHZNRFp1ZEdvU0FtVnpHZ0pCVWlnQVAB?hl=es-419&gl=AR&ceid=AR:es-419"
        response = requests.get(url, timeout=10)

        import xml.etree.ElementTree as ET
        root = ET.fromstring(response.content)

        news = []
        keywords_argentina = ['argentina', 'argentino', 'argentinos', 'milei', 'buenos aires', 'peso argentino', 'afa', 'boca', 'river', 'racing', 'independiente', 'san lorenzo', 'estudiantes', 'contte', 'apertura', 'superliga']

        for item in root.findall(".//item"):
            if len(news) >= 3:
                break
            title = item.find("title").text
            # Filtrar noticias de Argentina
            if any(kw in title.lower() for kw in keywords_argentina):
                continue
            if " - " in title:
                title = title.rsplit(" - ", 1)[0]
            news.append(title)

        return news
    except Exception as e:
        print(f"Error obteniendo noticias mundo: {e}")
        return []

# ==================== FÚTBOL ====================

EQUIPOS_FAVORITOS = ["Boca Juniors", "Inter Miami"]

def get_football_news():
    """Obtiene noticias de los equipos favoritos"""
    try:
        result = "⚽ *Fútbol:*\n"

        for equipo in EQUIPOS_FAVORITOS:
            # Buscar noticias del equipo
            search_term = equipo.replace(" ", "+")
            url = f"https://news.google.com/rss/search?q={search_term}+futbol&hl=es-419&gl=AR&ceid=AR:es-419"
            response = requests.get(url, timeout=10)

            import xml.etree.ElementTree as ET
            root = ET.fromstring(response.content)

            items = root.findall(".//item")[:2]  # 2 noticias por equipo
            if items:
                result += f"\n*{equipo}:*\n"
                for item in items:
                    title = item.find("title").text
                    if " - " in title:
                        title = title.rsplit(" - ", 1)[0]
                    result += f"  • {title}\n"

        return result
    except Exception as e:
        print(f"Error obteniendo noticias de fútbol: {e}")
        return "⚽ No pude obtener info de fútbol."

# ==================== CINE/STREAMING ====================

def get_entertainment_news():
    """Obtiene estrenos y noticias de cine/streaming"""
    try:
        url = "https://news.google.com/rss/search?q=estrenos+netflix+cine+peliculas&hl=es-419&gl=AR&ceid=AR:es-419"
        response = requests.get(url, timeout=10)

        import xml.etree.ElementTree as ET
        root = ET.fromstring(response.content)

        news = []
        for item in root.findall(".//item")[:3]:
            title = item.find("title").text
            if " - " in title:
                title = title.rsplit(" - ", 1)[0]
            news.append(title)

        if news:
            result = "🎬 *Cine y Streaming:*\n"
            for n in news:
                result += f"  • {n}\n"
            return result
        return ""
    except Exception as e:
        print(f"Error obteniendo noticias de entretenimiento: {e}")
        return ""

# ==================== CUARTETO CÓRDOBA ====================

def get_cuarteto_events():
    """Obtiene información de bailes de cuarteto en Córdoba"""
    try:
        url = "https://news.google.com/rss/search?q=cuarteto+cordoba+baile+show&hl=es-419&gl=AR&ceid=AR:es-419"
        response = requests.get(url, timeout=10)

        import xml.etree.ElementTree as ET
        root = ET.fromstring(response.content)

        news = []
        keywords = ['cuarteto', 'la mona', 'jimenez', 'cachumba', 'trulala', 'rodrigo', 'ulises bueno', 'la konga', 'baile', 'show']

        for item in root.findall(".//item"):
            if len(news) >= 3:
                break
            title = item.find("title").text.lower()
            if any(kw in title for kw in keywords):
                title_orig = item.find("title").text
                if " - " in title_orig:
                    title_orig = title_orig.rsplit(" - ", 1)[0]
                news.append(title_orig)

        if news:
            result = "🎺 *Cuarteto en Córdoba:*\n"
            for n in news:
                result += f"  • {n}\n"
            return result
        return "🎺 *Cuarteto:* No encontré eventos esta semana."
    except Exception as e:
        print(f"Error obteniendo info de cuarteto: {e}")
        return ""

def format_news():
    """Formatea las noticias para mostrar"""
    result = ""

    # Noticias Argentina
    news_ar = get_news_argentina()
    if news_ar:
        result += "🇦🇷 *Noticias de Argentina:*\n"
        for i, news in enumerate(news_ar, 1):
            result += f"  {i}. {news}\n"
        result += "\n"

    # Noticias del mundo
    news_world = get_news_world()
    if news_world:
        result += "🌍 *Noticias del Mundo:*\n"
        for i, news in enumerate(news_world, 1):
            result += f"  {i}. {news}\n"

    if not result:
        result = "No pude obtener las noticias en este momento."

    return result

# ==================== RESUMEN DEL DÍA ====================

def generate_daily_summary(user_id):
    """Genera el resumen del día"""
    now = datetime.now(TIMEZONE)

    # Saludo según la hora
    hour = now.hour
    if hour < 12:
        greeting = "¡Buenos días! ☀️"
    elif hour < 19:
        greeting = "¡Buenas tardes! 🌤"
    else:
        greeting = "¡Buenas noches! 🌙"

    summary = f"{greeting}\n\n"
    summary += f"📅 *{now.strftime('%A %d de %B, %Y')}*\n\n"

    # Frase motivacional
    summary += get_motivational_quote() + "\n\n"

    # Clima
    weather = get_weather()
    summary += weather + "\n\n"

    # Cotización del dólar
    summary += get_dolar() + "\n"

    # Eventos del día
    events = get_todays_events()
    if events:
        summary += "📆 *Eventos de hoy:*\n"
        for event in events:
            try:
                ical = Calendar.from_ical(event.data)
                for component in ical.walk():
                    if component.name == "VEVENT":
                        title = str(component.get("summary", "Sin título"))
                        dtstart = component.get("dtstart")
                        if dtstart and hasattr(dtstart.dt, "hour"):
                            time_str = dtstart.dt.strftime("%H:%M")
                            summary += f"  • {time_str} - {title}\n"
                        else:
                            summary += f"  • {title}\n"
            except:
                pass
    else:
        summary += "📆 No tienes eventos programados para hoy.\n"

    summary += "\n"

    # Tareas pendientes
    tasks = get_tasks(user_id)
    if tasks:
        summary += "📋 *Tareas pendientes:*\n"
        for task in tasks[:5]:
            summary += f"  • {task['text']}\n"
        if len(tasks) > 5:
            summary += f"  _...y {len(tasks) - 5} más_\n"
    else:
        summary += "📋 No tienes tareas pendientes. ¡Buen trabajo!\n"

    summary += "\n"

    # Noticias
    summary += format_news()

    return summary

def send_morning_summary():
    """Envía el resumen matutino a todos los usuarios registrados"""
    print(f"[{datetime.now()}] Enviando resumen matutino...")

    for user_number in registered_users:
        try:
            summary = generate_daily_summary(user_number)
            send_whatsapp_message(user_number, summary)
            print(f"Resumen enviado a {user_number}")
        except Exception as e:
            print(f"Error enviando resumen a {user_number}: {e}")

# ==================== PROMPT DEL SISTEMA ====================

SYSTEM_PROMPT = """Eres un asistente personal inteligente que ayuda a gestionar calendario, tareas, notas, gastos y más.

FUNCIONALIDADES DISPONIBLES:
1. CALENDARIO: Agendar eventos
2. TAREAS: Agregar, listar, completar y eliminar tareas
3. NOTAS: Guardar y consultar notas rápidas
4. CLIMA: Consultar el clima
5. RESUMEN: Obtener resumen del día (incluye clima, dólar, noticias, fútbol, cine)
6. GASTOS: Registrar y ver resumen de gastos
7. DÓLAR: Consultar cotización del dólar
8. FÚTBOL: Noticias de Boca Juniors e Inter Miami
9. CUARTETO: Bailes de cuarteto en Córdoba

IMPORTANTE sobre horarios:
- La hora actual es: {current_time}
- Si el usuario dice una hora como "2:30" o "3:00" sin especificar AM/PM, asume que es una hora FUTURA del mismo día
- Si la hora mencionada ya pasó hoy, pregunta si se refiere a mañana
- Usa formato 24 horas internamente (ej: 14:30 para 2:30 PM)

FORMATOS DE ACCIÓN (usa estos formatos exactos cuando corresponda):

Para crear EVENTOS en el calendario:
[EVENTO]
titulo: <título>
fecha: <YYYY-MM-DD>
hora: <HH:MM>
duracion: <minutos>
[/EVENTO]

Para agregar TAREAS:
[TAREA_AGREGAR]<texto de la tarea>[/TAREA_AGREGAR]

Para completar TAREAS:
[TAREA_COMPLETAR]<número>[/TAREA_COMPLETAR]

Para eliminar TAREAS:
[TAREA_ELIMINAR]<número>[/TAREA_ELIMINAR]

Para listar TAREAS:
[TAREAS_LISTAR][/TAREAS_LISTAR]

Para agregar NOTAS:
[NOTA_AGREGAR]<texto de la nota>[/NOTA_AGREGAR]

Para listar NOTAS:
[NOTAS_LISTAR][/NOTAS_LISTAR]

Para eliminar NOTAS:
[NOTA_ELIMINAR]<número>[/NOTA_ELIMINAR]

Para consultar CLIMA:
[CLIMA]<ciudad opcional>[/CLIMA]

Para generar RESUMEN del día:
[RESUMEN][/RESUMEN]

Para registrar GASTOS:
[GASTO_AGREGAR]monto|descripción|categoría[/GASTO_AGREGAR]
Categorías: Comida, Transporte, Entretenimiento, Servicios, Compras, Salud, Otros

Para ver resumen de GASTOS:
[GASTOS_RESUMEN][/GASTOS_RESUMEN]

Para consultar DÓLAR:
[DOLAR][/DOLAR]

Para ver noticias de FÚTBOL:
[FUTBOL][/FUTBOL]

Para ver bailes de CUARTETO:
[CUARTETO][/CUARTETO]

Para ver noticias de CINE/STREAMING:
[CINE][/CINE]

INSTRUCCIONES:
- Responde de forma breve y amable
- Cuando el usuario pida algo, ejecuta la acción directamente sin pedir confirmación
- Si dice "buenos días", "buen día", etc., genera automáticamente el resumen del día
- Si dice "agregar tarea: X" o "nueva tarea: X", agrega la tarea
- Si dice "mis tareas" o "lista de tareas", muestra las tareas
- Si dice "completar tarea 1" o "marcar tarea 1", complétala
- Si dice "guardar nota: X" o "anotar: X", guarda la nota
- Si dice "mis notas", muestra las notas
- Si dice "clima" o "cómo está el clima", muestra el clima
- Si dice "gasté X en Y" o "gasto: X", registra el gasto
- Si dice "mis gastos" o "resumen de gastos", muestra el resumen
- Si dice "dólar" o "cotización", muestra la cotización del dólar
- Si dice "fútbol" o "noticias de boca/inter miami", muestra noticias de fútbol
- Si dice "cuarteto", "bailes" o "qué bailes hay en la semana", muestra info de cuarteto
- Si dice "cine", "películas", "estrenos", "netflix" o "streaming", muestra info de cine/streaming

Hoy es: {today}
"""

# ==================== FUNCIONES DE CALENDARIO ====================

def get_caldav_client():
    """Conecta con el servidor CalDAV de iCloud"""
    try:
        client = caldav.DAVClient(
            url=CALDAV_URL, username=ICLOUD_EMAIL, password=ICLOUD_APP_PASSWORD
        )
        return client
    except Exception as e:
        print(f"Error conectando a iCloud: {e}")
        return None


def get_calendar():
    """Obtiene el calendario principal de iCloud"""
    client = get_caldav_client()
    if not client:
        return None
    try:
        principal = client.principal()
        calendars = principal.calendars()
        if calendars:
            return calendars[0]
        return None
    except Exception as e:
        print(f"Error obteniendo calendario: {e}")
        return None


def create_calendar_event(title, date_str, time_str, duration_minutes=60):
    """Crea un evento en el calendario de iCloud"""
    calendar = get_calendar()
    if not calendar:
        return False, "No se pudo conectar al calendario"

    try:
        dt_start = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
        dt_start = TIMEZONE.localize(dt_start)
        dt_end = dt_start + timedelta(minutes=int(duration_minutes))

        cal = Calendar()
        cal.add("prodid", "-//Asistente Personal//")
        cal.add("version", "2.0")

        event = Event()
        event.add("summary", title)
        event.add("dtstart", dt_start)
        event.add("dtend", dt_end)
        event.add("dtstamp", datetime.now(TIMEZONE))

        cal.add_component(event)
        calendar.save_event(cal.to_ical().decode("utf-8"))

        return True, f"Evento '{title}' creado para {date_str} a las {time_str}"
    except Exception as e:
        print(f"Error creando evento: {e}")
        return False, f"Error: {str(e)}"


def get_todays_events():
    """Obtiene los eventos de hoy"""
    calendar = get_calendar()
    if not calendar:
        return []

    today = datetime.now(TIMEZONE).date()
    tomorrow = today + timedelta(days=1)

    try:
        events = calendar.date_search(
            start=datetime.combine(today, datetime.min.time()),
            end=datetime.combine(tomorrow, datetime.min.time()),
        )
        return events
    except Exception as e:
        print(f"Error obteniendo eventos: {e}")
        return []


def get_upcoming_events(hours=24):
    """Obtiene eventos en las próximas horas"""
    calendar = get_calendar()
    if not calendar:
        return []

    now = datetime.now(TIMEZONE)
    end_time = now + timedelta(hours=hours)

    try:
        events = calendar.date_search(start=now, end=end_time)
        result = []
        for event in events:
            try:
                ical = Calendar.from_ical(event.data)
                for component in ical.walk():
                    if component.name == "VEVENT":
                        summary = str(component.get("summary", "Sin título"))
                        dtstart = component.get("dtstart")
                        if dtstart:
                            dt = dtstart.dt
                            if hasattr(dt, "hour"):
                                result.append(
                                    {"title": summary, "datetime": dt, "event": event}
                                )
            except:
                pass
        return result
    except Exception as e:
        print(f"Error obteniendo eventos próximos: {e}")
        return []


# ==================== PARSEO Y PROCESAMIENTO ====================

def parse_event_from_response(response_text):
    """Extrae datos del evento de la respuesta del AI"""
    pattern = r"\[EVENTO\](.*?)\[/EVENTO\]"
    match = re.search(pattern, response_text, re.DOTALL)

    if not match:
        return None

    event_text = match.group(1)
    event_data = {}

    for line in event_text.strip().split("\n"):
        if ":" in line:
            key, value = line.split(":", 1)
            event_data[key.strip().lower()] = value.strip()

    if "titulo" in event_data and "fecha" in event_data and "hora" in event_data:
        return event_data
    return None


def process_actions(response_text, user_id):
    """Procesa todas las acciones en la respuesta"""
    result = response_text

    # Procesar evento
    event_data = parse_event_from_response(response_text)
    if event_data:
        success, msg = create_calendar_event(
            event_data["titulo"],
            event_data["fecha"],
            event_data["hora"],
            event_data.get("duracion", 60),
        )
        result = re.sub(r"\[EVENTO\].*?\[/EVENTO\]", "", result, flags=re.DOTALL)
        result += f"\n\n{'✅' if success else '❌'} {msg}"

    # Procesar agregar tarea
    task_match = re.search(r"\[TAREA_AGREGAR\](.*?)\[/TAREA_AGREGAR\]", result, re.DOTALL)
    if task_match:
        task_text = task_match.group(1).strip()
        task = add_task(user_id, task_text)
        result = re.sub(r"\[TAREA_AGREGAR\].*?\[/TAREA_AGREGAR\]", "", result, flags=re.DOTALL)
        result += f"\n\n✅ Tarea agregada: {task_text}"

    # Procesar completar tarea
    complete_match = re.search(r"\[TAREA_COMPLETAR\](\d+)\[/TAREA_COMPLETAR\]", result)
    if complete_match:
        task_id = int(complete_match.group(1))
        if complete_task(user_id, task_id):
            result = re.sub(r"\[TAREA_COMPLETAR\]\d+\[/TAREA_COMPLETAR\]", "", result)
            result += f"\n\n✅ Tarea {task_id} completada"
        else:
            result += f"\n\n❌ No encontré la tarea {task_id}"

    # Procesar eliminar tarea
    delete_task_match = re.search(r"\[TAREA_ELIMINAR\](\d+)\[/TAREA_ELIMINAR\]", result)
    if delete_task_match:
        task_id = int(delete_task_match.group(1))
        if delete_task(user_id, task_id):
            result = re.sub(r"\[TAREA_ELIMINAR\]\d+\[/TAREA_ELIMINAR\]", "", result)
            result += f"\n\n✅ Tarea {task_id} eliminada"

    # Procesar listar tareas
    if "[TAREAS_LISTAR][/TAREAS_LISTAR]" in result:
        result = result.replace("[TAREAS_LISTAR][/TAREAS_LISTAR]", "")
        result += f"\n\n{format_tasks(user_id)}"

    # Procesar agregar nota
    note_match = re.search(r"\[NOTA_AGREGAR\](.*?)\[/NOTA_AGREGAR\]", result, re.DOTALL)
    if note_match:
        note_text = note_match.group(1).strip()
        note = add_note(user_id, note_text)
        result = re.sub(r"\[NOTA_AGREGAR\].*?\[/NOTA_AGREGAR\]", "", result, flags=re.DOTALL)
        result += f"\n\n✅ Nota guardada: {note_text}"

    # Procesar listar notas
    if "[NOTAS_LISTAR][/NOTAS_LISTAR]" in result:
        result = result.replace("[NOTAS_LISTAR][/NOTAS_LISTAR]", "")
        result += f"\n\n{format_notes(user_id)}"

    # Procesar eliminar nota
    delete_note_match = re.search(r"\[NOTA_ELIMINAR\](\d+)\[/NOTA_ELIMINAR\]", result)
    if delete_note_match:
        note_id = int(delete_note_match.group(1))
        if delete_note(user_id, note_id):
            result = re.sub(r"\[NOTA_ELIMINAR\]\d+\[/NOTA_ELIMINAR\]", "", result)
            result += f"\n\n✅ Nota {note_id} eliminada"

    # Procesar clima
    clima_match = re.search(r"\[CLIMA\](.*?)\[/CLIMA\]", result)
    if clima_match:
        city = clima_match.group(1).strip() or "Cordoba,Argentina"
        weather = get_weather(city)
        result = re.sub(r"\[CLIMA\].*?\[/CLIMA\]", "", result)
        result += f"\n\n{weather}"

    # Procesar resumen
    if "[RESUMEN][/RESUMEN]" in result:
        result = result.replace("[RESUMEN][/RESUMEN]", "")
        result += f"\n\n{generate_daily_summary(user_id)}"

    # Procesar agregar gasto
    gasto_match = re.search(r"\[GASTO_AGREGAR\](.*?)\[/GASTO_AGREGAR\]", result)
    if gasto_match:
        gasto_data = gasto_match.group(1).strip().split("|")
        if len(gasto_data) >= 2:
            try:
                monto = float(gasto_data[0].replace("$", "").replace(",", "").strip())
                descripcion = gasto_data[1].strip()
                categoria = gasto_data[2].strip() if len(gasto_data) > 2 else "General"
                expense = add_expense(user_id, monto, descripcion, categoria)
                result = re.sub(r"\[GASTO_AGREGAR\].*?\[/GASTO_AGREGAR\]", "", result)
                result += f"\n\n✅ Gasto registrado: ${monto:,.0f} - {descripcion} ({categoria})"
            except:
                result += "\n\n❌ No pude registrar el gasto. Formato: monto|descripción|categoría"
        else:
            result += "\n\n❌ Formato incorrecto. Usa: monto|descripción|categoría"

    # Procesar resumen de gastos
    if "[GASTOS_RESUMEN][/GASTOS_RESUMEN]" in result:
        result = result.replace("[GASTOS_RESUMEN][/GASTOS_RESUMEN]", "")
        result += f"\n\n{get_expenses_summary(user_id)}"

    # Procesar dólar
    if "[DOLAR][/DOLAR]" in result:
        result = result.replace("[DOLAR][/DOLAR]", "")
        result += f"\n\n{get_dolar()}"

    # Procesar fútbol
    if "[FUTBOL][/FUTBOL]" in result:
        result = result.replace("[FUTBOL][/FUTBOL]", "")
        result += f"\n\n{get_football_news()}"

    # Procesar cuarteto
    if "[CUARTETO][/CUARTETO]" in result:
        result = result.replace("[CUARTETO][/CUARTETO]", "")
        result += f"\n\n{get_cuarteto_events()}"

    # Procesar cine/streaming
    if "[CINE][/CINE]" in result:
        result = result.replace("[CINE][/CINE]", "")
        result += f"\n\n{get_entertainment_news()}"

    return result.strip()


def split_message(message, max_length=1500):
    """Divide un mensaje largo en partes"""
    if len(message) <= max_length:
        return [message]

    parts = []
    current = ""

    for line in message.split("\n"):
        if len(current) + len(line) + 1 <= max_length:
            current += line + "\n"
        else:
            if current:
                parts.append(current.strip())
            current = line + "\n"

    if current:
        parts.append(current.strip())

    return parts

def send_whatsapp_message(to_number, message):
    """Envía un mensaje de WhatsApp (divide si es muy largo)"""
    try:
        parts = split_message(message)
        for part in parts:
            twilio_client.messages.create(
                body=part, from_=TWILIO_WHATSAPP_NUMBER, to=to_number
            )
        return True
    except Exception as e:
        print(f"Error enviando WhatsApp: {e}")
        return False


def check_and_send_reminders():
    """Revisa eventos próximos y envía recordatorios"""
    print(f"[{datetime.now()}] Verificando recordatorios...")

    if not registered_users:
        return

    events = get_upcoming_events(hours=1)
    now = datetime.now(TIMEZONE)

    for event_data in events:
        event_time = event_data["datetime"]
        if hasattr(event_time, "tzinfo") and event_time.tzinfo is None:
            event_time = TIMEZONE.localize(event_time)

        time_diff = (event_time - now).total_seconds() / 60

        if 25 <= time_diff <= 35:
            title = event_data["title"]
            time_str = event_time.strftime("%H:%M")

            for user_number in registered_users:
                message = f"⏰ Recordatorio: '{title}' comienza a las {time_str} (en ~30 minutos)"
                send_whatsapp_message(user_number, message)
                print(f"Recordatorio enviado a {user_number}: {title}")


def get_ai_response(user_message, user_id):
    """Obtiene respuesta de Claude"""
    if user_id not in conversations:
        conversations[user_id] = []

    conversations[user_id].append({"role": "user", "content": user_message})

    if len(conversations[user_id]) > 20:
        conversations[user_id] = conversations[user_id][-20:]

    now = datetime.now(TIMEZONE)
    today = now.strftime("%Y-%m-%d %A")
    current_time = now.strftime("%H:%M")

    response = anthropic_client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1024,
        system=SYSTEM_PROMPT.format(today=today, current_time=current_time),
        messages=conversations[user_id],
    )

    assistant_message = response.content[0].text
    conversations[user_id].append({"role": "assistant", "content": assistant_message})

    # Procesar todas las acciones
    final_response = process_actions(assistant_message, user_id)

    return final_response


def transcribe_audio(audio_url):
    """Descarga y transcribe audio usando OpenAI Whisper API"""
    if not OPENAI_API_KEY:
        print("OpenAI API key no configurada")
        return None

    try:
        print(f"Descargando audio desde: {audio_url}")
        auth = (get_env_var("TWILIO_ACCOUNT_SID"), get_env_var("TWILIO_AUTH_TOKEN"))
        response = requests.get(audio_url, auth=auth)
        print(f"Audio descargado: {len(response.content)} bytes, status: {response.status_code}")

        if response.status_code != 200:
            print(f"Error descargando audio: {response.status_code}")
            return None

        # Guardar con extensión .mp3 que OpenAI maneja mejor
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
            f.write(response.content)
            temp_path = f.name

        print(f"Transcribiendo audio con OpenAI Whisper API...")

        # Usar OpenAI Whisper API
        with open(temp_path, "rb") as audio_file:
            transcription_response = requests.post(
                "https://api.openai.com/v1/audio/transcriptions",
                headers={
                    "Authorization": f"Bearer {OPENAI_API_KEY}"
                },
                files={
                    "file": ("audio.mp3", audio_file)
                },
                data={
                    "model": "whisper-1",
                    "language": "es"
                },
                timeout=30
            )

        os.unlink(temp_path)

        print(f"Respuesta de OpenAI: {transcription_response.status_code}")

        if transcription_response.status_code == 200:
            text = transcription_response.json().get("text", "").strip()
            print(f"Transcripción completada: {text}")
            if text:
                return text
            else:
                print("Transcripción vacía")
                return None
        else:
            print(f"Error en API de OpenAI: {transcription_response.status_code} - {transcription_response.text}")
            return None

    except Exception as e:
        print(f"Error transcribiendo audio: {e}")
        import traceback
        traceback.print_exc()
        return None


# ==================== RUTAS ====================

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/chat", methods=["POST"])
def chat():
    """Endpoint para el chat web"""
    data = request.get_json()
    user_message = data.get("message", "")

    if not user_message:
        return jsonify({"error": "Mensaje vacío"}), 400

    try:
        response = get_ai_response(user_message, "web_user")
        return jsonify({"response": response})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/whatsapp", methods=["POST"])
def whatsapp_webhook():
    """Endpoint para recibir mensajes de WhatsApp"""
    from_number = request.values.get("From", "")
    message_body = request.values.get("Body", "")
    num_media = int(request.values.get("NumMedia", 0))

    print(f"Mensaje de {from_number}: {message_body} (Media: {num_media})")

    # Registrar usuario para recordatorios
    if from_number and from_number not in registered_users:
        registered_users[from_number] = True
        print(f"Usuario registrado para recordatorios: {from_number}")

    # Si hay audio, transcribirlo
    if num_media > 0:
        media_type = request.values.get("MediaContentType0", "")
        if "audio" in media_type:
            media_url = request.values.get("MediaUrl0", "")
            print(f"Transcribiendo audio: {media_url}")
            transcription = transcribe_audio(media_url)
            if transcription:
                message_body = transcription
                print(f"Transcripción: {transcription}")
            else:
                message_body = "[No pude entender el audio]"

    # Obtener respuesta de Claude
    try:
        ai_response = get_ai_response(message_body, from_number)
    except Exception as e:
        ai_response = f"Error: {str(e)}"

    # Si el mensaje es largo, enviar por partes usando la API
    if len(ai_response) > 1500:
        send_whatsapp_message(from_number, ai_response)
        return "", 200
    else:
        resp = MessagingResponse()
        resp.message(ai_response)
        return str(resp)


@app.route("/events", methods=["GET"])
def list_events():
    """Lista eventos de hoy"""
    events = get_todays_events()
    event_list = []

    for event in events:
        try:
            ical = Calendar.from_ical(event.data)
            for component in ical.walk():
                if component.name == "VEVENT":
                    event_list.append(
                        {
                            "title": str(component.get("summary", "Sin título")),
                            "start": str(component.get("dtstart").dt),
                        }
                    )
        except:
            pass

    return jsonify({"events": event_list})


@app.route("/send-reminder", methods=["POST"])
def send_reminder():
    """Endpoint para enviar recordatorios manualmente"""
    data = request.get_json()
    to_number = data.get("to")
    message = data.get("message")

    if not to_number or not message:
        return jsonify({"error": "Faltan parámetros"}), 400

    try:
        twilio_client.messages.create(
            body=message, from_=TWILIO_WHATSAPP_NUMBER, to=to_number
        )
        return jsonify({"status": "enviado"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ==================== SCHEDULER ====================

scheduler = BackgroundScheduler(timezone=TIMEZONE)
scheduler.add_job(check_and_send_reminders, "interval", minutes=5)
# Resumen matutino a las 8:00 AM
scheduler.add_job(send_morning_summary, "cron", hour=8, minute=45)
scheduler.start()

if __name__ == "__main__":
    print("=" * 50)
    print("🤖 Asistente Personal iniciado")
    print(f"⏰ Zona horaria: {TIMEZONE}")
    print("📋 Funciones: Calendario, Tareas, Notas, Clima, Resumen")
    print("=" * 50)
    app.run(debug=True, port=5001, use_reloader=False)
