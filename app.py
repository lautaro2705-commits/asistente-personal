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

# Almacena los números de WhatsApp registrados para recordatorios
registered_users = {}

# Zona horaria
TIMEZONE = pytz.timezone("America/Argentina/Buenos_Aires")

# Archivos de datos
DATA_DIR = os.path.dirname(os.path.abspath(__file__))
TASKS_FILE = os.path.join(DATA_DIR, "tasks.json")
NOTES_FILE = os.path.join(DATA_DIR, "notes.json")
CONVERSATIONS_FILE = os.path.join(DATA_DIR, "conversations.json")

# ==================== HISTORIAL DE CONVERSACIONES ====================

def load_conversations():
    """Carga el historial de conversaciones desde archivo"""
    if os.path.exists(CONVERSATIONS_FILE):
        try:
            with open(CONVERSATIONS_FILE, "r") as f:
                return json.load(f)
        except:
            return {}
    return {}

def save_conversations(conversations):
    """Guarda el historial de conversaciones"""
    with open(CONVERSATIONS_FILE, "w") as f:
        json.dump(conversations, f, ensure_ascii=False)

def get_conversation(user_id):
    """Obtiene la conversación de un usuario"""
    conversations = load_conversations()
    return conversations.get(user_id, [])

def add_to_conversation(user_id, role, content):
    """Agrega un mensaje a la conversación"""
    conversations = load_conversations()
    if user_id not in conversations:
        conversations[user_id] = []

    conversations[user_id].append({"role": role, "content": content})

    # Mantener los últimos 50 mensajes para buen contexto
    if len(conversations[user_id]) > 50:
        conversations[user_id] = conversations[user_id][-50:]

    save_conversations(conversations)

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

# ==================== MEDICAMENTOS ====================

MEDS_FILE = os.path.join(DATA_DIR, "medications.json")

def load_medications():
    """Carga los medicamentos desde el archivo JSON"""
    if os.path.exists(MEDS_FILE):
        try:
            with open(MEDS_FILE, "r") as f:
                return json.load(f)
        except:
            return {}
    return {}

def save_medications(meds):
    """Guarda los medicamentos en el archivo JSON"""
    with open(MEDS_FILE, "w") as f:
        json.dump(meds, f, indent=2, ensure_ascii=False)

def add_medication(user_id, med_name):
    """Agrega un medicamento para un usuario"""
    meds = load_medications()
    if user_id not in meds:
        meds[user_id] = {"medications": [], "log": []}

    if med_name not in meds[user_id]["medications"]:
        meds[user_id]["medications"].append(med_name)
        save_medications(meds)
        return True
    return False

def remove_medication(user_id, med_name):
    """Elimina un medicamento"""
    meds = load_medications()
    if user_id in meds and med_name in meds[user_id]["medications"]:
        meds[user_id]["medications"].remove(med_name)
        save_medications(meds)
        return True
    return False

def get_medications(user_id):
    """Obtiene los medicamentos de un usuario"""
    meds = load_medications()
    if user_id in meds:
        return meds[user_id].get("medications", [])
    return []

def log_medication_taken(user_id, period):
    """Registra que se tomaron los medicamentos"""
    meds = load_medications()
    if user_id not in meds:
        meds[user_id] = {"medications": [], "log": []}

    today = datetime.now(TIMEZONE).strftime("%Y-%m-%d")
    log_entry = {"date": today, "period": period, "taken": True}
    meds[user_id]["log"].append(log_entry)

    # Mantener solo los últimos 60 días de log
    meds[user_id]["log"] = meds[user_id]["log"][-120:]
    save_medications(meds)

def check_medication_taken_today(user_id, period):
    """Verifica si ya se registró la toma de medicamentos hoy"""
    meds = load_medications()
    if user_id not in meds:
        return False

    today = datetime.now(TIMEZONE).strftime("%Y-%m-%d")
    for entry in meds[user_id].get("log", []):
        if entry.get("date") == today and entry.get("period") == period:
            return True
    return False

def format_medications(user_id):
    """Formatea la lista de medicamentos"""
    meds = get_medications(user_id)
    if not meds:
        return "💊 No tienes medicamentos registrados."

    result = "💊 *Tus medicamentos:*\n"
    for i, med in enumerate(meds, 1):
        result += f"  {i}. {med}\n"
    return result

def send_medication_reminder(period):
    """Envía recordatorio de medicamentos a todos los usuarios"""
    print(f"[{datetime.now()}] Enviando recordatorio de medicamentos ({period})...")

    meds = load_medications()

    for user_id in meds:
        if meds[user_id].get("medications"):
            # Verificar si ya tomó los medicamentos
            if not check_medication_taken_today(user_id, period):
                med_list = ", ".join(meds[user_id]["medications"])
                if period == "mañana":
                    message = f"💊 *Recordatorio de medicamentos (mañana)*\n\n¿Ya tomaste tus medicamentos?\n\n📋 {med_list}\n\nResponde 'tomé mis medicamentos' o 'ya tomé' cuando los hayas tomado."
                else:
                    message = f"💊 *Recordatorio de medicamentos (noche)*\n\n¿Ya tomaste tus medicamentos de la noche?\n\n📋 {med_list}\n\nResponde 'tomé mis medicamentos' o 'ya tomé' cuando los hayas tomado."

                try:
                    send_whatsapp_message(user_id, message)
                    print(f"Recordatorio de medicamentos enviado a {user_id}")
                except Exception as e:
                    print(f"Error enviando recordatorio a {user_id}: {e}")

# ==================== RECORDATORIOS PERSONALIZADOS ====================

REMINDERS_FILE = os.path.join(DATA_DIR, "reminders.json")

def load_reminders():
    """Carga los recordatorios desde el archivo JSON"""
    if os.path.exists(REMINDERS_FILE):
        try:
            with open(REMINDERS_FILE, "r") as f:
                return json.load(f)
        except:
            return {}
    return {}

def save_reminders(reminders):
    """Guarda los recordatorios"""
    with open(REMINDERS_FILE, "w") as f:
        json.dump(reminders, f, indent=2, ensure_ascii=False)

def add_reminder(user_id, message, remind_at):
    """Agrega un recordatorio"""
    reminders = load_reminders()
    if user_id not in reminders:
        reminders[user_id] = []

    reminder = {
        "id": len(reminders[user_id]) + 1,
        "message": message,
        "remind_at": remind_at,
        "created": datetime.now(TIMEZONE).isoformat(),
        "sent": False
    }
    reminders[user_id].append(reminder)
    save_reminders(reminders)
    return reminder

def get_pending_reminders(user_id):
    """Obtiene recordatorios pendientes"""
    reminders = load_reminders()
    if user_id not in reminders:
        return []
    return [r for r in reminders[user_id] if not r.get("sent", False)]

def mark_reminder_sent(user_id, reminder_id):
    """Marca un recordatorio como enviado"""
    reminders = load_reminders()
    if user_id in reminders:
        for r in reminders[user_id]:
            if r["id"] == reminder_id:
                r["sent"] = True
                save_reminders(reminders)
                return True
    return False

def delete_reminder(user_id, reminder_id):
    """Elimina un recordatorio"""
    reminders = load_reminders()
    if user_id in reminders:
        reminders[user_id] = [r for r in reminders[user_id] if r["id"] != reminder_id]
        save_reminders(reminders)
        return True
    return False

def format_reminders(user_id):
    """Formatea los recordatorios pendientes"""
    pending = get_pending_reminders(user_id)
    if not pending:
        return "⏰ No tienes recordatorios pendientes."

    result = "⏰ *Tus recordatorios:*\n"
    for r in pending:
        try:
            remind_time = datetime.fromisoformat(r["remind_at"])
            time_str = remind_time.strftime("%d/%m %H:%M")
            result += f"  {r['id']}. {r['message']} - {time_str}\n"
        except:
            result += f"  {r['id']}. {r['message']}\n"
    return result

def check_and_send_custom_reminders():
    """Revisa y envía recordatorios personalizados"""
    reminders = load_reminders()
    now = datetime.now(TIMEZONE)

    for user_id in reminders:
        for reminder in reminders[user_id]:
            if reminder.get("sent", False):
                continue

            try:
                remind_at = datetime.fromisoformat(reminder["remind_at"])
                if remind_at.tzinfo is None:
                    remind_at = TIMEZONE.localize(remind_at)

                if now >= remind_at:
                    message = f"⏰ *Recordatorio:*\n\n{reminder['message']}"
                    send_whatsapp_message(user_id, message)
                    mark_reminder_sent(user_id, reminder["id"])
                    print(f"Recordatorio enviado a {user_id}: {reminder['message']}")
            except Exception as e:
                print(f"Error procesando recordatorio: {e}")

# ==================== LISTA DE COMPRAS ====================

SHOPPING_FILE = os.path.join(DATA_DIR, "shopping.json")

def load_shopping():
    """Carga la lista de compras"""
    if os.path.exists(SHOPPING_FILE):
        try:
            with open(SHOPPING_FILE, "r") as f:
                return json.load(f)
        except:
            return {}
    return {}

def save_shopping(shopping):
    """Guarda la lista de compras"""
    with open(SHOPPING_FILE, "w") as f:
        json.dump(shopping, f, indent=2, ensure_ascii=False)

def add_shopping_item(user_id, item):
    """Agrega un item a la lista de compras"""
    shopping = load_shopping()
    if user_id not in shopping:
        shopping[user_id] = []

    shopping_item = {
        "id": len(shopping[user_id]) + 1,
        "item": item,
        "bought": False,
        "added": datetime.now(TIMEZONE).strftime("%Y-%m-%d")
    }
    shopping[user_id].append(shopping_item)
    save_shopping(shopping)
    return shopping_item

def mark_item_bought(user_id, item_id):
    """Marca un item como comprado"""
    shopping = load_shopping()
    if user_id in shopping:
        for item in shopping[user_id]:
            if item["id"] == item_id:
                item["bought"] = True
                save_shopping(shopping)
                return True
    return False

def delete_shopping_item(user_id, item_id):
    """Elimina un item de la lista"""
    shopping = load_shopping()
    if user_id in shopping:
        shopping[user_id] = [i for i in shopping[user_id] if i["id"] != item_id]
        # Reordenar IDs
        for idx, item in enumerate(shopping[user_id]):
            item["id"] = idx + 1
        save_shopping(shopping)
        return True
    return False

def clear_bought_items(user_id):
    """Elimina todos los items comprados"""
    shopping = load_shopping()
    if user_id in shopping:
        shopping[user_id] = [i for i in shopping[user_id] if not i.get("bought", False)]
        # Reordenar IDs
        for idx, item in enumerate(shopping[user_id]):
            item["id"] = idx + 1
        save_shopping(shopping)
        return True
    return False

def format_shopping_list(user_id):
    """Formatea la lista de compras"""
    shopping = load_shopping()
    items = shopping.get(user_id, [])

    if not items:
        return "🛒 Tu lista de compras está vacía."

    pending = [i for i in items if not i.get("bought", False)]
    bought = [i for i in items if i.get("bought", False)]

    result = "🛒 *Lista de compras:*\n"

    if pending:
        result += "\n*Pendientes:*\n"
        for item in pending:
            result += f"  {item['id']}. {item['item']}\n"

    if bought:
        result += "\n*Comprados:* ✓\n"
        for item in bought:
            result += f"  ~{item['item']}~\n"

    return result

# ==================== ANÁLISIS DE GASTOS ====================

def analyze_expenses(user_id):
    """Analiza los gastos del usuario"""
    expenses = load_expenses()
    user_expenses = expenses.get(user_id, [])

    if not user_expenses:
        return "📊 No tienes gastos registrados para analizar."

    now = datetime.now(TIMEZONE)

    # Gastos del mes actual
    current_month = now.strftime("%Y-%m")
    month_expenses = []
    for e in user_expenses:
        if e.get("date", "").startswith(current_month):
            month_expenses.append(e)

    # Gastos del mes anterior
    last_month = (now.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
    last_month_expenses = []
    for e in user_expenses:
        if e.get("date", "").startswith(last_month):
            last_month_expenses.append(e)

    # Gastos de la semana
    week_start = now - timedelta(days=now.weekday())
    week_expenses = []
    for e in user_expenses:
        try:
            exp_date = datetime.strptime(e["date"].split()[0], "%Y-%m-%d")
            if exp_date >= week_start.replace(tzinfo=None):
                week_expenses.append(e)
        except:
            pass

    total_month = sum(e["amount"] for e in month_expenses)
    total_last_month = sum(e["amount"] for e in last_month_expenses)
    total_week = sum(e["amount"] for e in week_expenses)

    result = "📊 *Análisis de gastos:*\n\n"

    result += f"💰 *Esta semana:* ${total_week:,.0f}\n"
    result += f"💰 *Este mes:* ${total_month:,.0f}\n"

    if total_last_month > 0:
        diff = total_month - total_last_month
        percent = (diff / total_last_month) * 100
        if diff > 0:
            result += f"📈 Gastaste ${diff:,.0f} más que el mes pasado (+{percent:.0f}%)\n"
        elif diff < 0:
            result += f"📉 Gastaste ${abs(diff):,.0f} menos que el mes pasado ({percent:.0f}%)\n"
        else:
            result += "📊 Igual que el mes pasado\n"

    # Categoría con más gastos
    if month_expenses:
        by_category = {}
        for e in month_expenses:
            cat = e.get("category", "General")
            by_category[cat] = by_category.get(cat, 0) + e["amount"]

        top_category = max(by_category, key=by_category.get)
        top_amount = by_category[top_category]
        result += f"\n🏷 *Mayor gasto:* {top_category} (${top_amount:,.0f})\n"

        result += "\n*Por categoría este mes:*\n"
        for cat, amount in sorted(by_category.items(), key=lambda x: -x[1]):
            percent = (amount / total_month) * 100 if total_month > 0 else 0
            result += f"  • {cat}: ${amount:,.0f} ({percent:.0f}%)\n"

    # Promedio diario
    if month_expenses:
        days_in_month = now.day
        daily_avg = total_month / days_in_month
        result += f"\n📅 *Promedio diario:* ${daily_avg:,.0f}"

    return result

# ==================== UBICACIÓN ====================

USER_LOCATIONS_FILE = os.path.join(DATA_DIR, "locations.json")

def load_locations():
    """Carga las ubicaciones guardadas"""
    if os.path.exists(USER_LOCATIONS_FILE):
        try:
            with open(USER_LOCATIONS_FILE, "r") as f:
                return json.load(f)
        except:
            return {}
    return {}

def save_locations(locations):
    """Guarda las ubicaciones"""
    with open(USER_LOCATIONS_FILE, "w") as f:
        json.dump(locations, f, ensure_ascii=False)

def set_user_location(user_id, city):
    """Guarda la ubicación del usuario"""
    locations = load_locations()
    locations[user_id] = city
    save_locations(locations)

def get_user_location(user_id):
    """Obtiene la ubicación del usuario"""
    locations = load_locations()
    return locations.get(user_id, "Cordoba,Argentina")

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

Para agregar MEDICAMENTO:
[MED_AGREGAR]<nombre del medicamento>[/MED_AGREGAR]

Para eliminar MEDICAMENTO:
[MED_ELIMINAR]<nombre del medicamento>[/MED_ELIMINAR]

Para listar MEDICAMENTOS:
[MED_LISTAR][/MED_LISTAR]

Para registrar que TOMÓ los medicamentos:
[MED_TOMADO]<periodo: mañana o noche>[/MED_TOMADO]

Para agregar RECORDATORIO:
[RECORDATORIO]mensaje|YYYY-MM-DD HH:MM[/RECORDATORIO]

Para listar RECORDATORIOS:
[RECORDATORIOS_LISTAR][/RECORDATORIOS_LISTAR]

Para eliminar RECORDATORIO:
[RECORDATORIO_ELIMINAR]<número>[/RECORDATORIO_ELIMINAR]

Para agregar item a LISTA DE COMPRAS:
[COMPRA_AGREGAR]<item>[/COMPRA_AGREGAR]

Para ver LISTA DE COMPRAS:
[COMPRAS_LISTAR][/COMPRAS_LISTAR]

Para marcar item COMPRADO:
[COMPRA_MARCAR]<número>[/COMPRA_MARCAR]

Para eliminar item de COMPRAS:
[COMPRA_ELIMINAR]<número>[/COMPRA_ELIMINAR]

Para limpiar items COMPRADOS:
[COMPRAS_LIMPIAR][/COMPRAS_LIMPIAR]

Para ver ANÁLISIS de gastos:
[GASTOS_ANALISIS][/GASTOS_ANALISIS]

Para cambiar UBICACIÓN (para el clima):
[UBICACION]<ciudad>[/UBICACION]

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
- Si dice "agregar medicamento: X" o "tomo X", agrega el medicamento
- Si dice "mis medicamentos" o "qué medicamentos tomo", muestra la lista
- Si dice "tomé mis medicamentos", "ya tomé" o "medicamentos tomados", registra que los tomó (usa "mañana" si es antes de las 14:00, "noche" si es después)
- Si dice "eliminar medicamento X", elimina el medicamento
- Si dice "recordame en X horas/minutos que Y" o "avisame a las X que Y", crea un recordatorio con fecha y hora calculada
- Si dice "mis recordatorios" o "qué recordatorios tengo", muestra los recordatorios
- Si dice "eliminar recordatorio X", elimina el recordatorio
- Si dice "agregar a la lista de compras: X" o "comprar X", agrega a la lista
- Si dice "lista de compras" o "qué tengo que comprar", muestra la lista
- Si dice "compré X" o "ya compré el item X", marca como comprado
- Si dice "limpiar comprados", elimina los items ya comprados
- Si dice "análisis de gastos" o "cómo voy con los gastos", muestra análisis detallado
- Si dice "estoy en X" o "mi ubicación es X" o "cambiar ubicación a X", guarda la ubicación para el clima

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
        city = clima_match.group(1).strip() or get_user_location(user_id)
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

    # Procesar agregar medicamento
    med_add_match = re.search(r"\[MED_AGREGAR\](.*?)\[/MED_AGREGAR\]", result)
    if med_add_match:
        med_name = med_add_match.group(1).strip()
        if add_medication(user_id, med_name):
            result = re.sub(r"\[MED_AGREGAR\].*?\[/MED_AGREGAR\]", "", result)
            result += f"\n\n✅ Medicamento agregado: {med_name}"
        else:
            result = re.sub(r"\[MED_AGREGAR\].*?\[/MED_AGREGAR\]", "", result)
            result += f"\n\n⚠️ El medicamento '{med_name}' ya está en tu lista."

    # Procesar eliminar medicamento
    med_del_match = re.search(r"\[MED_ELIMINAR\](.*?)\[/MED_ELIMINAR\]", result)
    if med_del_match:
        med_name = med_del_match.group(1).strip()
        if remove_medication(user_id, med_name):
            result = re.sub(r"\[MED_ELIMINAR\].*?\[/MED_ELIMINAR\]", "", result)
            result += f"\n\n✅ Medicamento eliminado: {med_name}"
        else:
            result = re.sub(r"\[MED_ELIMINAR\].*?\[/MED_ELIMINAR\]", "", result)
            result += f"\n\n❌ No encontré el medicamento '{med_name}' en tu lista."

    # Procesar listar medicamentos
    if "[MED_LISTAR][/MED_LISTAR]" in result:
        result = result.replace("[MED_LISTAR][/MED_LISTAR]", "")
        result += f"\n\n{format_medications(user_id)}"

    # Procesar medicamentos tomados
    med_taken_match = re.search(r"\[MED_TOMADO\](.*?)\[/MED_TOMADO\]", result)
    if med_taken_match:
        period = med_taken_match.group(1).strip().lower()
        if period not in ["mañana", "noche"]:
            # Determinar automáticamente según la hora
            hour = datetime.now(TIMEZONE).hour
            period = "mañana" if hour < 14 else "noche"

        log_medication_taken(user_id, period)
        result = re.sub(r"\[MED_TOMADO\].*?\[/MED_TOMADO\]", "", result)
        result += f"\n\n✅ Registrado: medicamentos de la {period} tomados. ¡Bien hecho! 💪"

    # Procesar agregar recordatorio
    reminder_match = re.search(r"\[RECORDATORIO\](.*?)\[/RECORDATORIO\]", result)
    if reminder_match:
        reminder_data = reminder_match.group(1).strip().split("|")
        if len(reminder_data) >= 2:
            try:
                message_text = reminder_data[0].strip()
                remind_at_str = reminder_data[1].strip()
                remind_at = datetime.strptime(remind_at_str, "%Y-%m-%d %H:%M")
                remind_at = TIMEZONE.localize(remind_at)
                reminder = add_reminder(user_id, message_text, remind_at.isoformat())
                result = re.sub(r"\[RECORDATORIO\].*?\[/RECORDATORIO\]", "", result)
                result += f"\n\n✅ Recordatorio creado: '{message_text}' para el {remind_at.strftime('%d/%m/%Y a las %H:%M')}"
            except Exception as e:
                print(f"Error creando recordatorio: {e}")
                result = re.sub(r"\[RECORDATORIO\].*?\[/RECORDATORIO\]", "", result)
                result += "\n\n❌ No pude crear el recordatorio. Formato: mensaje|YYYY-MM-DD HH:MM"
        else:
            result = re.sub(r"\[RECORDATORIO\].*?\[/RECORDATORIO\]", "", result)
            result += "\n\n❌ Formato incorrecto. Usa: mensaje|YYYY-MM-DD HH:MM"

    # Procesar listar recordatorios
    if "[RECORDATORIOS_LISTAR][/RECORDATORIOS_LISTAR]" in result:
        result = result.replace("[RECORDATORIOS_LISTAR][/RECORDATORIOS_LISTAR]", "")
        result += f"\n\n{format_reminders(user_id)}"

    # Procesar eliminar recordatorio
    reminder_del_match = re.search(r"\[RECORDATORIO_ELIMINAR\](\d+)\[/RECORDATORIO_ELIMINAR\]", result)
    if reminder_del_match:
        reminder_id = int(reminder_del_match.group(1))
        if delete_reminder(user_id, reminder_id):
            result = re.sub(r"\[RECORDATORIO_ELIMINAR\]\d+\[/RECORDATORIO_ELIMINAR\]", "", result)
            result += f"\n\n✅ Recordatorio {reminder_id} eliminado"
        else:
            result = re.sub(r"\[RECORDATORIO_ELIMINAR\]\d+\[/RECORDATORIO_ELIMINAR\]", "", result)
            result += f"\n\n❌ No encontré el recordatorio {reminder_id}"

    # Procesar agregar a lista de compras
    shopping_add_match = re.search(r"\[COMPRA_AGREGAR\](.*?)\[/COMPRA_AGREGAR\]", result)
    if shopping_add_match:
        item = shopping_add_match.group(1).strip()
        add_shopping_item(user_id, item)
        result = re.sub(r"\[COMPRA_AGREGAR\].*?\[/COMPRA_AGREGAR\]", "", result)
        result += f"\n\n✅ Agregado a la lista: {item}"

    # Procesar listar compras
    if "[COMPRAS_LISTAR][/COMPRAS_LISTAR]" in result:
        result = result.replace("[COMPRAS_LISTAR][/COMPRAS_LISTAR]", "")
        result += f"\n\n{format_shopping_list(user_id)}"

    # Procesar marcar comprado
    shopping_mark_match = re.search(r"\[COMPRA_MARCAR\](\d+)\[/COMPRA_MARCAR\]", result)
    if shopping_mark_match:
        item_id = int(shopping_mark_match.group(1))
        if mark_item_bought(user_id, item_id):
            result = re.sub(r"\[COMPRA_MARCAR\]\d+\[/COMPRA_MARCAR\]", "", result)
            result += f"\n\n✅ Item {item_id} marcado como comprado"
        else:
            result = re.sub(r"\[COMPRA_MARCAR\]\d+\[/COMPRA_MARCAR\]", "", result)
            result += f"\n\n❌ No encontré el item {item_id}"

    # Procesar eliminar de compras
    shopping_del_match = re.search(r"\[COMPRA_ELIMINAR\](\d+)\[/COMPRA_ELIMINAR\]", result)
    if shopping_del_match:
        item_id = int(shopping_del_match.group(1))
        if delete_shopping_item(user_id, item_id):
            result = re.sub(r"\[COMPRA_ELIMINAR\]\d+\[/COMPRA_ELIMINAR\]", "", result)
            result += f"\n\n✅ Item {item_id} eliminado de la lista"

    # Procesar limpiar comprados
    if "[COMPRAS_LIMPIAR][/COMPRAS_LIMPIAR]" in result:
        clear_bought_items(user_id)
        result = result.replace("[COMPRAS_LIMPIAR][/COMPRAS_LIMPIAR]", "")
        result += "\n\n✅ Items comprados eliminados de la lista"

    # Procesar análisis de gastos
    if "[GASTOS_ANALISIS][/GASTOS_ANALISIS]" in result:
        result = result.replace("[GASTOS_ANALISIS][/GASTOS_ANALISIS]", "")
        result += f"\n\n{analyze_expenses(user_id)}"

    # Procesar cambio de ubicación
    location_match = re.search(r"\[UBICACION\](.*?)\[/UBICACION\]", result)
    if location_match:
        city = location_match.group(1).strip()
        set_user_location(user_id, city)
        result = re.sub(r"\[UBICACION\].*?\[/UBICACION\]", "", result)
        result += f"\n\n✅ Ubicación guardada: {city}. El clima ahora será de esta ciudad."

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


def is_new_user(user_id):
    """Verifica si es un usuario nuevo (sin conversaciones previas)"""
    conversation = get_conversation(user_id)
    return len(conversation) == 0

def get_welcome_message():
    """Mensaje de bienvenida para usuarios nuevos"""
    return """¡Hola! 👋 Soy tu *Asistente Personal*.

Estoy acá para ayudarte a organizar tu día a día. Esto es lo que puedo hacer:

📋 *TAREAS*
• "agregar tarea: comprar leche"
• "mis tareas"
• "completar tarea 1"

📝 *NOTAS*
• "guardar nota: cumple de mamá 15/3"
• "mis notas"

💰 *GASTOS*
• "gasté 5000 en supermercado"
• "mis gastos"
• "análisis de gastos"

🛒 *LISTA DE COMPRAS*
• "agregar a compras: pan, leche"
• "lista de compras"
• "compré el 1"

⏰ *RECORDATORIOS*
• "recordame en 2 horas sacar la ropa"
• "recordame mañana a las 10 llamar al médico"

💊 *MEDICAMENTOS*
• "tomo ibuprofeno"
• "mis medicamentos"
• "ya tomé mis medicamentos"

🌤 *INFO ÚTIL*
• "clima" - pronóstico del tiempo
• "dólar" - cotización actual
• "noticias" - titulares del día
• "buen día" - resumen completo del día

🎤 *También podés enviarme audios* y los entiendo perfectamente.

¿En qué te puedo ayudar?"""

def get_ai_response(user_message, user_id):
    """Obtiene respuesta de Claude"""
    # Verificar si es usuario nuevo
    is_first_message = is_new_user(user_id)

    # Cargar conversación desde archivo (persistente)
    conversation = get_conversation(user_id)

    # Agregar mensaje del usuario
    add_to_conversation(user_id, "user", user_message)
    conversation.append({"role": "user", "content": user_message})

    # Si es usuario nuevo y dice hola/buen día, mostrar bienvenida
    greeting_words = ["hola", "buenas", "buen dia", "buen día", "buenos dias", "buenos días", "hey", "hello", "hi", "que tal", "qué tal"]
    if is_first_message and any(word in user_message.lower() for word in greeting_words):
        welcome = get_welcome_message()
        add_to_conversation(user_id, "assistant", welcome)
        return welcome

    now = datetime.now(TIMEZONE)
    today = now.strftime("%Y-%m-%d %A")
    current_time = now.strftime("%H:%M")

    response = anthropic_client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1024,
        system=SYSTEM_PROMPT.format(today=today, current_time=current_time),
        messages=conversation,
    )

    assistant_message = response.content[0].text

    # Guardar respuesta del asistente
    add_to_conversation(user_id, "assistant", assistant_message)

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
        print(f"Error en get_ai_response: {e}")

    # Siempre enviar usando la API de Twilio (más confiable con WhatsApp Business)
    try:
        send_whatsapp_message(from_number, ai_response)
        print(f"Respuesta enviada a {from_number}")
    except Exception as e:
        print(f"Error enviando respuesta: {e}")

    # Responder inmediatamente a Twilio con 200 OK
    return "", 200


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
# Recordatorios personalizados cada minuto
scheduler.add_job(check_and_send_custom_reminders, "interval", minutes=1)
# Resumen matutino a las 8:00 AM
scheduler.add_job(send_morning_summary, "cron", hour=8, minute=45)
# Recordatorio de medicamentos a las 10:00 AM
scheduler.add_job(lambda: send_medication_reminder("mañana"), "cron", hour=10, minute=0)
# Recordatorio de medicamentos a las 9:00 PM
scheduler.add_job(lambda: send_medication_reminder("noche"), "cron", hour=21, minute=0)
scheduler.start()

if __name__ == "__main__":
    print("=" * 50)
    print("🤖 Asistente Personal iniciado")
    print(f"⏰ Zona horaria: {TIMEZONE}")
    print("📋 Funciones: Calendario, Tareas, Notas, Clima, Resumen")
    print("=" * 50)
    app.run(debug=True, port=5001, use_reloader=False)
