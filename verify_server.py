# -*- coding: utf-8 -*-

# verify_server.py (Architektura stanów, rozbudowana instrukcja AI Propozycji, bez średników)

from flask import Flask, request, Response
import os
import json
import requests
import time
import vertexai
import random # Potrzebne dla exponential backoff
from vertexai.generative_models import (
    GenerativeModel, Part, Content, GenerationConfig,
    SafetySetting, HarmCategory, HarmBlockThreshold
)
import errno
import logging
import datetime
import pytz
import locale
import re
from collections import defaultdict

# --- Importy Google Calendar ---
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
# ------------------------------

app = Flask(__name__)

# --- Konfiguracja Ogólna ---
VERIFY_TOKEN = os.environ.get("FB_VERIFY_TOKEN", "KOLAGEN")
PAGE_ACCESS_TOKEN = os.environ.get("FB_PAGE_ACCESS_TOKEN", "EACNAHFzEhkUBOxSDMfOZCYbQAFKfVzJWowJpX8mcX0BvBGaWFRiUwNHjojZBcRXIPFszKzzRZBEqFI7AFD0DpI5sOeiN7HKLBGxBZB7tAgCkFdipRNQKevuP3F4kvSTIZCqqkrBaq7rPRM7FIqNQjP2Ju9UdZB5FNcvndzdZBZBGxTyyw9hkWmBndNr2A0VwO2Gf8QZDZD") # Testowy token
PROJECT_ID = os.environ.get("GCP_PROJECT_ID", "linear-booth-450221-k1")
LOCATION = os.environ.get("GCP_LOCATION", "us-central1")
MODEL_ID = os.environ.get("VERTEX_MODEL_ID", "gemini-2.0-flash-001") # Model Flash 2.0

FACEBOOK_GRAPH_API_URL = f"https://graph.facebook.com/v19.0/me/messages"

HISTORY_DIR = "conversation_store"
MAX_HISTORY_TURNS = 15
MESSAGE_CHAR_LIMIT = 1990
MESSAGE_DELAY_SECONDS = 1.5

ENABLE_TYPING_DELAY = True
MIN_TYPING_DELAY_SECONDS = 0.8
MAX_TYPING_DELAY_SECONDS = 3.5
TYPING_CHARS_PER_SECOND = 30

# --- Konfiguracja Kalendarza ---
SERVICE_ACCOUNT_FILE = 'kalendarzklucz.json'
CALENDAR_SCOPES = ['https://www.googleapis.com/auth/calendar.readonly', 'https://www.googleapis.com/auth/calendar.events']
CALENDAR_TIMEZONE = 'Europe/Warsaw'
APPOINTMENT_DURATION_MINUTES = 60
WORK_START_HOUR = 7
WORK_END_HOUR = 22
TARGET_CALENDAR_ID = 'f19e189826b9d6e36950da347ac84d5501ecbd6bed0d76c8641be61a67749c67@group.calendar.google.com'
PREFERRED_WEEKDAY_START_HOUR = 16
PREFERRED_WEEKEND_START_HOUR = 10
MAX_SEARCH_DAYS = 14

# --- Znaczniki i Stany ---
INTENT_SCHEDULE_MARKER = "[INTENT_SCHEDULE]"
SLOT_ISO_MARKER_PREFIX = "[SLOT_ISO:"
SLOT_ISO_MARKER_SUFFIX = "]"
STATE_GENERAL = "general"
STATE_SCHEDULING_ACTIVE = "scheduling_active"
STATE_WAITING_FOR_FEEDBACK = "waiting_for_feedback"

# --- Ustawienia Modelu Gemini ---
GENERATION_CONFIG_PROPOSAL = GenerationConfig(
    temperature=0.0, # Maksymalnie deterministyczny
    top_p=0.95, top_k=40, max_output_tokens=512,
)
GENERATION_CONFIG_FEEDBACK_SIMPLE = GenerationConfig(
    temperature=0.0, top_p=0.95, top_k=40, max_output_tokens=32,
)
GENERATION_CONFIG_DEFAULT = GenerationConfig(
    temperature=0.7, top_p=0.95, top_k=40, max_output_tokens=1024,
)

# --- Bezpieczeństwo AI ---
SAFETY_SETTINGS = [
    SafetySetting(category=HarmCategory.HARM_CATEGORY_HARASSMENT, threshold=HarmBlockThreshold.BLOCK_MEDIUM_AND_ABOVE),
    SafetySetting(category=HarmCategory.HARM_CATEGORY_HATE_SPEECH, threshold=HarmBlockThreshold.BLOCK_MEDIUM_AND_ABOVE),
    SafetySetting(category=HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT, threshold=HarmBlockThreshold.BLOCK_MEDIUM_AND_ABOVE),
    SafetySetting(category=HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT, threshold=HarmBlockThreshold.BLOCK_MEDIUM_AND_ABOVE),
]

# --- Inicjalizacja Zmiennych Globalnych ---
_calendar_service = None
_tz = None
POLISH_WEEKDAYS = ["Poniedziałek", "Wtorek", "Środa", "Czwartek", "Piątek", "Sobota", "Niedziela"]

# --- Ustawienia Lokalizacji ---
try:
    locale.setlocale(locale.LC_TIME, 'pl_PL.UTF-8')
except locale.Error:
    try:
        locale.setlocale(locale.LC_TIME, 'Polish_Poland.1250')
    except locale.Error:
        logging.warning("Nie można ustawić polskiej lokalizacji dla formatowania dat.")

# =====================================================================
# === INICJALIZACJA AI - WE WŁAŚCIWYM MIEJSCU =========================
# =====================================================================
gemini_model = None
try:
    if not logging.getLogger().hasHandlers():
        logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
    print(f"--- Inicjalizowanie Vertex AI: Projekt={PROJECT_ID}, Lokalizacja={LOCATION}")
    vertexai.init(project=PROJECT_ID, location=LOCATION)
    print("--- Inicjalizacja Vertex AI OK.")
    print(f"--- Ładowanie modelu: {MODEL_ID}")
    gemini_model = GenerativeModel(MODEL_ID)
    print(f"--- Model {MODEL_ID} załadowany OK.")
except Exception as e:
    print(f"!!! KRYTYCZNY BŁĄD inicjalizacji Vertex AI lub ładowania modelu: {e}", flush=True)
    import traceback
    traceback.print_exc()
    print("!!! Funkcjonalność AI będzie niedostępna !!!", flush=True)

# =====================================================================
# === FUNKCJE POMOCNICZE ==============================================
# =====================================================================

def ensure_dir(directory):
    """Tworzy katalog, jeśli nie istnieje."""
    try:
        os.makedirs(directory)
        logging.info(f"Utworzono katalog: {directory}")
    except OSError as e:
        if e.errno != errno.EEXIST:
            logging.error(f"Błąd tworzenia katalogu {directory}: {e}", exc_info=True)
            raise

def get_user_profile(psid):
    """Pobiera podstawowe dane profilu użytkownika z Facebook Graph API."""
    if not PAGE_ACCESS_TOKEN or len(PAGE_ACCESS_TOKEN) < 50:
        logging.warning(f"[{psid}] Brak/nieprawidłowy PAGE_ACCESS_TOKEN.")
        return None
    USER_PROFILE_API_URL_TEMPLATE = "https://graph.facebook.com/v19.0/{psid}?fields=first_name,last_name,profile_pic&access_token={token}"
    url = USER_PROFILE_API_URL_TEMPLATE.format(psid=psid, token=PAGE_ACCESS_TOKEN)
    logging.debug(f"--- [{psid}] Pobieranie profilu...")
    profile_data = {}
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        data = r.json()
        if 'error' in data:
            logging.error(f"BŁĄD FB API (profil) {psid}: {data['error']}")
            if data['error'].get('code') == 190:
                 logging.error("!!! Wygląda na to, że FB_PAGE_ACCESS_TOKEN jest nieprawidłowy lub wygasł !!!")
            return None
        profile_data['first_name'] = data.get('first_name')
        profile_data['last_name'] = data.get('last_name')
        profile_data['profile_pic'] = data.get('profile_pic')
        profile_data['id'] = data.get('id')
        return profile_data
    except requests.exceptions.Timeout:
        logging.error(f"BŁĄD TIMEOUT podczas pobierania profilu {psid}")
        return None
    except requests.exceptions.HTTPError as http_err:
         logging.error(f"BŁĄD HTTP {http_err.response.status_code} podczas pobierania profilu {psid}: {http_err}")
         if http_err.response is not None:
            try:
                logging.error(f"Odpowiedź FB (błąd HTTP): {http_err.response.json()}")
            except json.JSONDecodeError:
                logging.error(f"Odpowiedź FB (błąd HTTP, nie JSON): {http_err.response.text}")
         return None
    except requests.exceptions.RequestException as req_err:
        logging.error(f"BŁĄD RequestException podczas pobierania profilu {psid}: {req_err}")
        return None
    except Exception as e:
        logging.error(f"Niespodziewany BŁĄD podczas pobierania profilu {psid}: {e}", exc_info=True)
        return None

def load_history(user_psid):
    """Wczytuje historię i ostatni kontekst/stan z pliku."""
    filepath = os.path.join(HISTORY_DIR, f"{user_psid}.json")
    history = []
    context = {}
    if not os.path.exists(filepath):
        return history, context
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            history_data = json.load(f)
            if isinstance(history_data, list):
                last_system_message_index = -1
                for i, msg_data in enumerate(reversed(history_data)):
                    if isinstance(msg_data, dict) and msg_data.get('role') == 'system':
                        last_system_message_index = len(history_data) - 1 - i
                        break

                for i, msg_data in enumerate(history_data):
                    if (isinstance(msg_data, dict) and 'role' in msg_data and
                            msg_data['role'] in ('user', 'model') and 'parts' in msg_data and
                            isinstance(msg_data['parts'], list) and msg_data['parts']):
                        text_parts = []
                        valid_parts = True
                        for part_data in msg_data['parts']:
                            if isinstance(part_data, dict) and 'text' in part_data and isinstance(part_data['text'], str):
                                text_parts.append(Part.from_text(part_data['text']))
                            else:
                                logging.warning(f"Ostrz. [{user_psid}]: Niepoprawna część wiadomości (idx {i})")
                                valid_parts = False
                                break
                        if valid_parts and text_parts:
                            history.append(Content(role=msg_data['role'], parts=text_parts))
                    elif isinstance(msg_data, dict) and msg_data.get('role') == 'system':
                        if i == last_system_message_index:
                            context = msg_data
                            logging.debug(f"[{user_psid}] Odczytano AKTYWNY kontekst: {context}")
                        else:
                            logging.debug(f"[{user_psid}] Pominięto stary kontekst (idx {i}): {msg_data}")
                    else:
                        logging.warning(f"Ostrz. [{user_psid}]: Pominięto niepoprawną wiadomość (idx {i}): {msg_data}")

                logging.info(f"[{user_psid}] Wczytano historię: {len(history)} wiad. Stan: {context.get('type', STATE_GENERAL)}")
                return history, context
            else:
                logging.error(f"BŁĄD [{user_psid}]: Plik historii nie jest listą.")
                return [], {}
    except FileNotFoundError:
        logging.info(f"[{user_psid}] Plik historii nie istnieje.")
        return [], {}
    except (json.JSONDecodeError, KeyError, IndexError, TypeError) as e:
        logging.error(f"BŁĄD [{user_psid}] parsowania historii: {e}.")
        try:
            os.rename(filepath, f"{filepath}.error_{int(time.time())}")
            logging.warning(f"    Zmieniono nazwę uszkodzonego pliku historii.")
        except OSError as rename_err:
             logging.error(f"    Nie udało się zmienić nazwy: {rename_err}")
        return [], {}
    except Exception as e:
        logging.error(f"BŁĄD [{user_psid}] wczytywania historii: {e}", exc_info=True)
        return [], {}

def save_history(user_psid, history, context_to_save=None):
    """Zapisuje historię i aktualny kontekst/stan."""
    ensure_dir(HISTORY_DIR)
    filepath = os.path.join(HISTORY_DIR, f"{user_psid}.json")
    temp_filepath = f"{filepath}.tmp"
    history_data = []
    try:
        history_to_process = [m for m in history if isinstance(m, Content) and m.role in ('user', 'model')]
        max_messages_to_save = MAX_HISTORY_TURNS * 2
        if len(history_to_process) > max_messages_to_save:
            history_to_process = history_to_process[-max_messages_to_save:]
        for msg in history_to_process:
             if isinstance(msg, Content) and hasattr(msg, 'role') and msg.role in ('user', 'model') and hasattr(msg, 'parts') and isinstance(msg.parts, list):
                parts_data = [{'text': part.text} for part in msg.parts if isinstance(part, Part) and hasattr(part, 'text')]
                if parts_data:
                    history_data.append({'role': msg.role, 'parts': parts_data})
             else:
                logging.warning(f"Ostrz. [{user_psid}]: Pomijanie nieprawidłowego obiektu historii: {msg}")

        if context_to_save and isinstance(context_to_save, dict) and context_to_save.get('type'):
             context_to_save['role'] = 'system'
             history_data.append(context_to_save)
             logging.debug(f"[{user_psid}] Dodano kontekst do zapisu: {context_to_save}")
        else:
             logging.debug(f"[{user_psid}] Zapis bez kontekstu (stan general).")

        with open(temp_filepath, 'w', encoding='utf-8') as f:
            json.dump(history_data, f, ensure_ascii=False, indent=2)
        os.replace(temp_filepath, filepath)
        logging.info(f"[{user_psid}] Zapisano historię/kontekst ({len(history_data)} wpisów)")
    except Exception as e:
        logging.error(f"BŁĄD [{user_psid}] zapisu historii/kontekstu: {e}", exc_info=True)
        if os.path.exists(temp_filepath):
            try:
                os.remove(temp_filepath)
                logging.info(f"    Usunięto plik tymczasowy.")
            except OSError as remove_e:
                logging.error(f"    Nie można usunąć pliku tymczasowego {temp_filepath}: {remove_e}")

def _get_timezone():
    """Pobiera (i cachuje) obiekt strefy czasowej."""
    global _tz
    if _tz is None:
        try:
            _tz = pytz.timezone(CALENDAR_TIMEZONE)
        except pytz.exceptions.UnknownTimeZoneError:
            logging.error(f"BŁĄD: Strefa '{CALENDAR_TIMEZONE}' nieznana. Używam UTC.")
            _tz = pytz.utc
    return _tz

def get_calendar_service():
    """Inicjalizuje (i cachuje) usługę Google Calendar API."""
    global _calendar_service
    if _calendar_service:
        return _calendar_service
    if not os.path.exists(SERVICE_ACCOUNT_FILE):
        logging.error(f"KRYTYCZNY BŁĄD: Brak pliku klucza: '{SERVICE_ACCOUNT_FILE}'")
        return None
    try:
        creds = service_account.Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=CALENDAR_SCOPES)
        service = build('calendar', 'v3', credentials=creds, cache_discovery=False)
        logging.info("Utworzono połączenie z Google Calendar API.")
        _calendar_service = service
        return service
    except Exception as e:
        logging.error(f"Błąd tworzenia usługi Calendar: {e}", exc_info=True)
        return None

def parse_event_time(event_time_data, default_tz):
    """Parsuje dane czasu wydarzenia z API Kalendarza."""
    if 'dateTime' in event_time_data:
        dt_str = event_time_data['dateTime']
        try:
            dt = datetime.datetime.fromisoformat(dt_str)
        except ValueError:
            if dt_str.endswith('Z'):
                try:
                    dt = datetime.datetime.strptime(dt_str, '%Y-%m-%dT%H:%M:%S.%fZ')
                    dt = pytz.utc.localize(dt)
                except ValueError:
                     try:
                         dt = datetime.datetime.strptime(dt_str, '%Y-%m-%dT%H:%M:%SZ')
                         dt = pytz.utc.localize(dt)
                     except ValueError:
                         logging.warning(f"Ostrz.: Nie sparsowano dateTime (Z): {dt_str}")
                         return None
            else:
                try:
                    if ':' in dt_str[-6:]:
                       dt_str = dt_str[:-3] + dt_str[-2:]
                    dt = datetime.datetime.strptime(dt_str, '%Y-%m-%dT%H:%M:%S%z')
                except ValueError:
                    logging.warning(f"Ostrz.: Nie sparsowano dateTime: {dt_str}")
                    return None
        if dt.tzinfo is None:
            dt = default_tz.localize(dt)
        else:
            dt = dt.astimezone(default_tz)
        return dt
    elif 'date' in event_time_data:
        try:
            return datetime.date.fromisoformat(event_time_data['date'])
        except ValueError:
            logging.warning(f"Ostrz.: Nie sparsowano date: {event_time_data['date']}")
            return None
    return None

def get_free_time_ranges(calendar_id, start_datetime, end_datetime):
    """Pobiera listę wolnych zakresów czasowych z kalendarza."""
    service = get_calendar_service()
    tz = _get_timezone()
    if not service:
        logging.error("Błąd: Usługa kalendarza niedostępna.")
        return []

    if start_datetime.tzinfo is None: start_datetime = tz.localize(start_datetime)
    else: start_datetime = start_datetime.astimezone(tz)
    if end_datetime.tzinfo is None: end_datetime = tz.localize(end_datetime)
    else: end_datetime = end_datetime.astimezone(tz)

    now = datetime.datetime.now(tz)
    start_datetime = max(start_datetime, now)
    if start_datetime >= end_datetime:
        logging.info("Zakres wyszukiwania nieprawidłowy.")
        return []

    logging.info(f"Szukanie wolnych zakresów w '{calendar_id}' od {start_datetime:%Y-%m-%d %H:%M} do {end_datetime:%Y-%m-%d %H:%M}")
    try:
        body = {"timeMin": start_datetime.isoformat(), "timeMax": end_datetime.isoformat(), "timeZone": CALENDAR_TIMEZONE, "items": [{"id": calendar_id}]}
        freebusy_result = service.freebusy().query(body=body).execute()
        busy_times_raw = freebusy_result.get('calendars', {}).get(calendar_id, {}).get('busy', [])
        busy_times = []
        for busy_slot in busy_times_raw:
            try:
                busy_start = parse_event_time({'dateTime': busy_slot['start']}, tz)
                busy_end = parse_event_time({'dateTime': busy_slot['end']}, tz)
                if busy_start and busy_end:
                    busy_start_clipped = max(busy_start, start_datetime)
                    busy_end_clipped = min(busy_end, end_datetime)
                    if busy_start_clipped < busy_end_clipped:
                        busy_times.append({'start': busy_start_clipped, 'end': busy_end_clipped})
            except Exception as e:
                logging.warning(f"Ostrz.: Nie sparsowano zajętego czasu: {busy_slot}, błąd: {e}")
    except HttpError as error:
        logging.error(f'Błąd API Freebusy: {error}', exc_info=True)
        return []
    except Exception as e:
        logging.error(f"Nieoczekiwany błąd Freebusy: {e}", exc_info=True)
        return []

    busy_times.sort(key=lambda x: x['start'])
    merged_busy_times = []
    for busy in busy_times:
        if not merged_busy_times or busy['start'] > merged_busy_times[-1]['end']:
            merged_busy_times.append(busy)
        else:
            merged_busy_times[-1]['end'] = max(merged_busy_times[-1]['end'], busy['end'])

    free_ranges = []
    current_time = start_datetime
    for busy_slot in merged_busy_times:
        if current_time < busy_slot['start']:
            free_ranges.append({'start': current_time, 'end': busy_slot['start']})
        current_time = max(current_time, busy_slot['end'])
    if current_time < end_datetime:
        free_ranges.append({'start': current_time, 'end': end_datetime})

    final_free_slots = []
    min_duration_delta = datetime.timedelta(minutes=APPOINTMENT_DURATION_MINUTES)
    for free_range in free_ranges:
        range_start = free_range['start']
        range_end = free_range['end']
        current_day_start = range_start
        while current_day_start < range_end:
            day_date = current_day_start.date()
            work_day_start = tz.localize(datetime.datetime.combine(day_date, datetime.time(WORK_START_HOUR, 0)))
            work_day_end = tz.localize(datetime.datetime.combine(day_date, datetime.time(WORK_END_HOUR, 0)))
            intersect_start = max(current_day_start, work_day_start)
            intersect_end = min(range_end, work_day_end)

            if intersect_start < intersect_end and (intersect_end - intersect_start) >= min_duration_delta:
                if intersect_start.minute % 10 != 0 or intersect_start.second > 0 or intersect_start.microsecond > 0:
                    minutes_to_add = 10 - (intersect_start.minute % 10)
                    rounded_start = intersect_start + datetime.timedelta(minutes=minutes_to_add)
                    rounded_start = rounded_start.replace(second=0, microsecond=0)
                else:
                    rounded_start = intersect_start
                if rounded_start < intersect_end and (intersect_end - rounded_start) >= min_duration_delta:
                    final_free_slots.append({'start': rounded_start, 'end': intersect_end})

            next_day_date = day_date + datetime.timedelta(days=1)
            current_day_start = tz.localize(datetime.datetime.combine(next_day_date, datetime.time(0, 0)))
            current_day_start = max(current_day_start, range_start) # Uważaj na pętlę

    logging.info(f"Znaleziono {len(final_free_slots)} wolnych zakresów czasowych.")
    return final_free_slots

def is_slot_actually_free(start_time, calendar_id):
    """Weryfikuje w czasie rzeczywistym, czy slot jest wolny."""
    service = get_calendar_service()
    tz = _get_timezone()
    if not service:
        logging.error("Błąd: Usługa kalendarza niedostępna.")
        return False

    if start_time.tzinfo is None: start_time = tz.localize(start_time)
    else: start_time = start_time.astimezone(tz)
    end_time = start_time + datetime.timedelta(minutes=APPOINTMENT_DURATION_MINUTES)

    body = {"timeMin": start_time.isoformat(), "timeMax": end_time.isoformat(), "timeZone": CALENDAR_TIMEZONE, "items": [{"id": calendar_id}]}
    try:
        logging.debug(f"Weryfikacja freebusy dla: {start_time:%Y-%m-%d %H:%M} - {end_time:%Y-%m-%d %H:%M}")
        freebusy_result = service.freebusy().query(body=body).execute()
        busy_times = freebusy_result.get('calendars', {}).get(calendar_id, {}).get('busy', [])
        if not busy_times:
            logging.info(f"Weryfikacja: Slot {start_time:%Y-%m-%d %H:%M} jest wolny.")
            return True
        else:
            for busy in busy_times:
                busy_start = parse_event_time({'dateTime': busy['start']}, tz)
                busy_end = parse_event_time({'dateTime': busy['end']}, tz)
                if busy_start and busy_end and max(start_time, busy_start) < min(end_time, busy_end):
                    logging.warning(f"Weryfikacja: Slot {start_time:%Y-%m-%d %H:%M} jest ZAJĘTY.")
                    return False
            logging.info(f"Weryfikacja: Slot {start_time:%Y-%m-%d %H:%M} jest wolny (brak kolizji).")
            return True
    except Exception as e:
        logging.error(f"Nieoczekiwany błąd weryfikacji Freebusy: {e}", exc_info=True)
        return False

def book_appointment(calendar_id, start_time, end_time, summary="Rezerwacja FB", description="", user_name=""):
    """Rezerwuje termin w Kalendarzu Google."""
    service = get_calendar_service()
    tz = _get_timezone()
    if not service:
        return False, "Błąd: Brak połączenia z usługą kalendarza."

    if start_time.tzinfo is None: start_time = tz.localize(start_time)
    else: start_time = start_time.astimezone(tz)
    if end_time.tzinfo is None: end_time = tz.localize(end_time)
    else: end_time = end_time.astimezone(tz)

    event_summary = summary + (f" - {user_name}" if user_name else "")
    event = {'summary': event_summary, 'description': description, 'start': {'dateTime': start_time.isoformat(), 'timeZone': CALENDAR_TIMEZONE}, 'end': {'dateTime': end_time.isoformat(), 'timeZone': CALENDAR_TIMEZONE}, 'reminders': {'useDefault': False, 'overrides': [{'method': 'popup', 'minutes': 60}]}, 'status': 'confirmed'}
    try:
        logging.info(f"Próba rezerwacji: '{event_summary}' od {start_time:%Y-%m-%d %H:%M} do {end_time:%Y-%m-%d %H:%M}")
        created_event = service.events().insert(calendarId=calendar_id, body=event).execute()
        event_id = created_event.get('id')
        logging.info(f"Termin zarezerwowany pomyślnie. ID wydarzenia: {event_id}")
        day_index = start_time.weekday(); locale_day_name = POLISH_WEEKDAYS[day_index]; hour_str = str(start_time.hour)
        confirm_message = f"Świetnie! Termin na {locale_day_name}, {start_time.strftime(f'%d.%m.%Y o {hour_str}:%M')} został zarezerwowany."
        return True, confirm_message
    except HttpError as error:
        error_details = f"Kod: {error.resp.status}, Powód: {error.resp.reason}"; logging.error(f"Błąd API Google Calendar rezerwacji: {error}, Szczegóły: {error_details}", exc_info=True)
        if error.resp.status == 409: return False, "Niestety, ten termin został właśnie zajęty."
        else: return False, "Problem z rezerwacją terminu."
    except Exception as e:
        logging.error(f"Nieoczekiwany błąd Python rezerwacji: {e}", exc_info=True)
        return False, "Wewnętrzny błąd systemu rezerwacji."

def format_ranges_for_ai(ranges):
    """Formatuje listę zakresów czasowych na bardziej techniczny tekst dla AI."""
    if not ranges: return "Brak dostępnych zakresów czasowych."
    tz = _get_timezone(); formatted_lines = [f"Dostępne ZAKRESY czasowe (wizyta trwa {APPOINTMENT_DURATION_MINUTES} minut). Wybierz JEDEN zakres i wygeneruj DOKŁADNY termin startu (np. 16:00), dołączając go w znaczniku [SLOT_ISO:...]. Pamiętaj, aby termin mieścił się w zakresie.", "--- Dostępne Zakresy (Data YYYY-MM-DD, Dzień, Od Godziny HH:MM, Do Godziny HH:MM) ---"]
    slots_added = 0; max_slots_to_show = 15; sorted_ranges = sorted(ranges, key=lambda r: r['start'])
    for r in sorted_ranges:
        start_dt = r['start'].astimezone(tz); end_dt = r['end'].astimezone(tz); day_name = POLISH_WEEKDAYS[start_dt.weekday()]
        date_str = start_dt.strftime('%Y-%m-%d'); start_time_str = start_dt.strftime('%H:%M'); end_time_str = end_dt.strftime('%H:%M')
        formatted_lines.append(f"- {date_str}, {day_name}, od {start_time_str}, do {end_time_str}"); slots_added += 1
        if slots_added >= max_slots_to_show: formatted_lines.append("- ... (i potencjalnie więcej)"); break
    if slots_added == 0: return "Brak dostępnych zakresów czasowych w godzinach pracy."
    formatted_output = "\n".join(formatted_lines); logging.debug(f"--- Zakresy sformatowane dla AI ---\n{formatted_output}\n---------------------------------"); return formatted_output

def format_slot_for_user(slot_start):
    """Formatuje pojedynczy slot (datetime) na czytelny tekst dla użytkownika."""
    if not isinstance(slot_start, datetime.datetime): logging.warning(f"Błąd formatowania slotu: {type(slot_start)}"); return "[Błąd daty]"
    try:
        tz = _get_timezone();
        if slot_start.tzinfo is None: slot_start = tz.localize(slot_start)
        else: slot_start = slot_start.astimezone(tz)
        day_index = slot_start.weekday(); day_name = POLISH_WEEKDAYS[day_index]; hour_str = str(slot_start.hour)
        return f"{day_name}, {slot_start.strftime(f'%d.%m.%Y o {hour_str}:%M')}"
    except Exception as e: logging.error(f"Błąd formatowania slotu {slot_start}: {e}", exc_info=True); return slot_start.isoformat()

def _send_typing_on(recipient_id):
    """Wysyła wskaźnik 'pisania' do użytkownika."""
    if not PAGE_ACCESS_TOKEN or len(PAGE_ACCESS_TOKEN) < 50 or not ENABLE_TYPING_DELAY: return
    logging.debug(f"[{recipient_id}] Wysyłanie 'typing_on'")
    params = {"access_token": PAGE_ACCESS_TOKEN}; payload = {"recipient": {"id": recipient_id}, "sender_action": "typing_on"}
    try: requests.post(FACEBOOK_GRAPH_API_URL, params=params, json=payload, timeout=5)
    except requests.exceptions.RequestException as e: logging.warning(f"[{recipient_id}] Błąd 'typing_on': {e}")

def _send_single_message(recipient_id, message_text):
    """Wysyła pojedynczy fragment wiadomości przez Facebook Graph API."""
    logging.info(f"--- Wysyłanie fragmentu do {recipient_id} (dł: {len(message_text)}) ---")
    if not PAGE_ACCESS_TOKEN or len(PAGE_ACCESS_TOKEN) < 50: logging.error(f"!!! [{recipient_id}] Brak tokena. NIE WYSŁANO."); return False
    params = {"access_token": PAGE_ACCESS_TOKEN}; payload = {"recipient": {"id": recipient_id}, "message": {"text": message_text}, "messaging_type": "RESPONSE"}
    try:
        r = requests.post(FACEBOOK_GRAPH_API_URL, params=params, json=payload, timeout=30); r.raise_for_status()
        response_json = r.json()
        if response_json.get('error'): fb_error = response_json['error']; logging.error(f"!!! BŁĄD FB API wysyłania: {fb_error} !!!"); return False
        logging.debug(f"[{recipient_id}] Fragment wysłany."); return True
    except Exception as e: logging.error(f"!!! BŁĄD wysyłania do {recipient_id}: {e} !!!", exc_info=True); return False

def send_message(recipient_id, full_message_text):
    """Wysyła wiadomość do użytkownika, dzieląc ją na fragmenty."""
    if not full_message_text or not isinstance(full_message_text, str) or not full_message_text.strip(): logging.warning(f"[{recipient_id}] Pominięto wysłanie pustej wiadomości."); return
    message_len = len(full_message_text); logging.info(f"[{recipient_id}] Przygotowanie wiadomości (dł: {message_len}).")
    if ENABLE_TYPING_DELAY: est_dur = min(MAX_TYPING_DELAY_SECONDS, max(MIN_TYPING_DELAY_SECONDS, message_len / TYPING_CHARS_PER_SECOND)); logging.debug(f"[{recipient_id}] Czas pisania: {est_dur:.2f}s"); _send_typing_on(recipient_id); time.sleep(est_dur)
    chunks = [];
    if message_len <= MESSAGE_CHAR_LIMIT: chunks.append(full_message_text)
    else:
        logging.info(f"[{recipient_id}] Dzielenie wiadomości..."); remaining_text = full_message_text
        while remaining_text:
            if len(remaining_text) <= MESSAGE_CHAR_LIMIT: chunks.append(remaining_text.strip()); break
            split_index = -1; delimiters = ['\n\n', '\n', '. ', '! ', '? ', ' ']
            for d in delimiters:
                s_lim = MESSAGE_CHAR_LIMIT - len(d) + 1
                t_idx = remaining_text.rfind(d, 0, s_lim);
                if t_idx != -1: 
                    split_index = t_idx + len(d)
                    break
            if split_index == -1: split_index = MESSAGE_CHAR_LIMIT
            chunk = remaining_text[:split_index].strip()
            if chunk: chunks.append(chunk)
            remaining_text = remaining_text[split_index:].strip()
        logging.info(f"[{recipient_id}] Podzielono na {len(chunks)} fragmentów.")
    num_chunks = len(chunks); send_success_count = 0
    for i, chunk in enumerate(chunks):
        logging.debug(f"[{recipient_id}] Wysyłanie fragm. {i+1}/{num_chunks}...")
        if not _send_single_message(recipient_id, chunk): logging.error(f"!!! [{recipient_id}] Błąd wysyłania fragm. {i+1}. Anulowano."); break
        send_success_count += 1
        if num_chunks > 1 and i < num_chunks - 1:
            logging.debug(f"[{recipient_id}] Oczekiwanie {MESSAGE_DELAY_SECONDS}s...");
            if ENABLE_TYPING_DELAY: est_dur = min(MAX_TYPING_DELAY_SECONDS, max(MIN_TYPING_DELAY_SECONDS, len(chunks[i+1]) / TYPING_CHARS_PER_SECOND)) * 0.5; _send_typing_on(recipient_id); time.sleep(est_dur + MESSAGE_DELAY_SECONDS * 0.5); time.sleep(MESSAGE_DELAY_SECONDS * 0.5)
            else: time.sleep(MESSAGE_DELAY_SECONDS)
    logging.info(f"--- [{recipient_id}] Zakończono wysyłanie. Wysłano {send_success_count}/{num_chunks} fragm. ---")

def _simulate_typing(recipient_id, duration_seconds):
    """Wysyła 'typing_on' i czeka."""
    if ENABLE_TYPING_DELAY and duration_seconds > 0: _send_typing_on(recipient_id); time.sleep(min(duration_seconds, MAX_TYPING_DELAY_SECONDS))

def _call_gemini(user_psid, prompt_history, generation_config, task_name, max_retries=3):
    """Wywołuje API Gemini z obsługą błędów i logowaniem."""
    if not gemini_model: logging.error(f"!!! [{user_psid}] Model Gemini ({task_name}) jest None!"); return None
    if not isinstance(prompt_history, list) or not all(isinstance(item, Content) for item in prompt_history): logging.error(f"!!! [{user_psid}] Nieprawidłowy prompt ({task_name})."); return None
    logging.info(f"[{user_psid}] Wywołanie Gemini: {task_name} (Prompt: {len(prompt_history)} wiad.)")
    if prompt_history and prompt_history[-1].role == 'user' and prompt_history[-1].parts: logging.debug(f"    Ostatnia wiadomość usera w prompcie ({task_name}): '{prompt_history[-1].parts[0].text[:200]}...'")
    elif prompt_history and len(prompt_history) > 1 and prompt_history[-1].role == 'model' and prompt_history[-2].role == 'user' and prompt_history[-2].parts: logging.debug(f"    Ostatnia wiadomość usera w prompcie ({task_name}) (przed ostatnią modelu): '{prompt_history[-2].parts[0].text[:200]}...'")

    attempt = 0
    while attempt < max_retries:
        attempt += 1; logging.debug(f"    Próba {attempt}/{max_retries} Gemini ({task_name})...")
        try:
            _simulate_typing(user_psid, MIN_TYPING_DELAY_SECONDS)
            response = gemini_model.generate_content(prompt_history, generation_config=generation_config, safety_settings=SAFETY_SETTINGS)
            if response and response.candidates:
                finish_reason = response.candidates[0].finish_reason
                if finish_reason != 1: # 1 = STOP
                    safety_ratings = response.candidates[0].safety_ratings; logging.warning(f"[{user_psid}] Gemini ({task_name}) ZABLOKOWANE/NIEDOKOŃCZONE! Powód: {finish_reason}. Safety: {safety_ratings}")
                    if attempt < max_retries: logging.warning(f"    Ponawianie ({attempt}/{max_retries})..."); time.sleep(1 * attempt); continue
                    else: logging.error(f"!!! [{user_psid}] Gemini ({task_name}) nieudane po blokadzie."); return "Problem z zasadami bezpieczeństwa."
                if response.candidates[0].content and response.candidates[0].content.parts:
                    generated_text = "".join(part.text for part in response.candidates[0].content.parts if hasattr(part, 'text'))
                    logging.info(f"[{user_psid}] Gemini ({task_name}) zwróciło odp. (dł: {len(generated_text)}).")
                    logging.debug(f"    Pełna odpowiedź Gemini ({task_name}): '{generated_text}'")
                    return generated_text.strip()
                else: logging.warning(f"[{user_psid}] Gemini ({task_name}) zwróciło kandydata bez treści.")
            else: prompt_feedback = response.prompt_feedback if hasattr(response, 'prompt_feedback') else 'Brak'; logging.error(f"!!! BŁĄD [{user_psid}] Gemini ({task_name}) - Brak kandydatów. Feedback: {prompt_feedback}.")
        except HttpError as http_err:
             logging.error(f"!!! BŁĄD HTTP ({http_err.resp.status}) [{user_psid}] Gemini ({task_name}) - Próba {attempt}/{max_retries}: {http_err.resp.reason}")
             if http_err.resp.status in [429, 500, 503] and attempt < max_retries: sleep_time = (2 ** attempt) + (random.random() * 0.1); logging.warning(f"    Oczekiwanie {sleep_time:.2f}s..."); time.sleep(sleep_time); continue
             else: break
        except Exception as e:
             if isinstance(e, NameError) and 'gemini_model' in str(e): logging.critical(f"!!! KRYTYCZNY NameError [{user_psid}] w _call_gemini: {e}. gemini_model jest None!", exc_info=True); return None
             else: logging.error(f"!!! BŁĄD [{user_psid}] Gemini ({task_name}) - Nieoczekiwany błąd API (Próba {attempt}/{max_retries}): {e}", exc_info=True)
        if attempt < max_retries: logging.warning(f"    Oczekiwanie przed ponowieniem ({attempt+1}/{max_retries})..."); time.sleep(1.5 * attempt)
    logging.error(f"!!! KRYTYCZNY BŁĄD [{user_psid}] Gemini ({task_name}) - Nie udało się po {max_retries} próbach."); return None

# =====================================================================
# === INSTRUKCJE SYSTEMOWE I GŁÓWNE FUNKCJE AI ========================
# =====================================================================

# Rozbudowana instrukcja dla AI Proponującego
SYSTEM_INSTRUCTION_TEXT_PROPOSE = """Twoje zadanie: Jesteś systemem wybierającym termin spotkania. Masz przeanalizować historię rozmowy pod kątem preferencji użytkownika oraz sprawdzić poniższą listę dostępnych ZAKRESÓW czasowych. Twoim celem jest zaproponowanie JEDNEGO konkretnego terminu rozpoczęcia wizyty (trwa {duration} minut) i zwrócenie go wraz ze znacznikiem ISO.

**Dostępne zakresy czasowe (Data YYYY-MM-DD, Dzień, Od Godziny HH:MM, Do Godziny HH:MM):**
{available_ranges_text}

**Algorytm postępowania:**

1.  **Analiza Historii:** Sprawdź ostatnie wiadomości użytkownika w historii. Czy zawiera ona konkretną prośbę o dzień tygodnia, porę dnia (np. rano, popołudnie, wieczór) lub konkretną godzinę?

2.  **Wyszukiwanie Preferowanego Terminu:**
    a.  Jeśli użytkownik podał preferencje (np. "piątek wieczorem", "środa o 17"): Spróbuj znaleźć **pierwszy dostępny** termin w podanych "Dostępnych zakresach czasowych", który **pasuje** do tych preferencji. Pamiętaj, że termin musi się mieścić w zakresie (`wybrany_czas + {duration} minut <= koniec_zakresu`). Preferuj pełne godziny, jeśli to możliwe.
    b.  Jeśli użytkownik **nie podał** konkretnych preferencji: Wybierz **najbliższy dostępny**, "rozsądny" termin z listy zakresów (preferuj popołudnia w dni robocze od {pref_weekday}h lub weekendy od {pref_weekend}h, jeśli są dostępne; w przeciwnym razie wybierz po prostu pierwszy dostępny).

3.  **Generowanie Odpowiedzi:**
    a.  **Jeśli znalazłeś termin pasujący do preferencji użytkownika (krok 2a):** Sformułuj propozycję tego terminu (np. "Znalazłem termin zgodny z Twoją prośbą: [dzień], [data] o [godzina]. Czy pasuje?"). Dołącz na końcu znacznik `{slot_marker_prefix}TERMIN_ISO{slot_marker_suffix}` dla tego terminu.
    b.  **Jeśli NIE znalazłeś terminu pasującego DOKŁADNIE do preferencji użytkownika (np. prosił o piątek wieczór, a nie ma już miejsc), ALE znalazłeś inne dostępne terminy (krok 2b lub alternatywa):**
        i.  **Poinformuj** krótko o braku dostępności preferowanego terminu (np. "Niestety, w piątek wieczorem nie mam już wolnych miejsc.").
        ii. **Zaproponuj NAJBLIŻSZĄ dostępną alternatywę**, którą znalazłeś w kroku 2b lub wybierając po prostu pierwszy dostępny slot z listy (np. "Najbliższy wolny termin, jaki mogę zaproponować, to [dzień_alt], [data_alt] o [godzina_alt]. Czy taka opcja by Ci odpowiadała?").
        iii.**Dołącz na końcu znacznik `{slot_marker_prefix}TERMIN_ALT_ISO{slot_marker_suffix}` dla tej ALTERNATYWNEJ propozycji.**
    c.  **Jeśli użytkownik nie miał preferencji i znalazłeś termin (krok 2b):** Sformułuj prostą propozycję (np. "Proponuję termin: [dzień], [data] o [godzina]. Pasuje?"). Dołącz na końcu znacznik `{slot_marker_prefix}TERMIN_ISO{slot_marker_suffix}`.
    d.  **Jeśli lista "Dostępne zakresy czasowe" była PUSTA:** Odpowiedz "Niestety, w tej chwili nie widzę żadnych wolnych terminów.". NIE dodawaj znacznika ISO.

**BARDZO WAŻNE ZASADY:**
*   Twoja odpowiedź (oprócz przypadku 3d) **MUSI** kończyć się znacznikiem `{slot_marker_prefix}TERMIN_ISO{slot_marker_suffix}` zawierającym **konkretny, dostępny** termin (nawet jeśli to alternatywa).
*   Termin w ISO **MUSI** pochodzić z listy "Dostępnych zakresów czasowych".
*   **NIE ZADAWAJ ŻADNYCH PYTAŃ** w swojej odpowiedzi. Twoim zadaniem jest zaproponować termin.
*   Generuj tylko **JEDNĄ** propozycję terminu w tekście (tę, której ISO dodajesz na końcu).

""".format(
    available_ranges_text="{available_ranges_text}",
    duration=APPOINTMENT_DURATION_MINUTES,
    pref_weekday=PREFERRED_WEEKDAY_START_HOUR,
    pref_weekend=PREFERRED_WEEKEND_START_HOUR,
    slot_marker_prefix=SLOT_ISO_MARKER_PREFIX,
    slot_marker_suffix=SLOT_ISO_MARKER_SUFFIX
)

# Uproszczona instrukcja feedbacku
SYSTEM_INSTRUCTION_TEXT_FEEDBACK_SIMPLE = """Jesteś asystentem AI analizującym odpowiedź użytkownika na propozycję terminu.

**Kontekst:** Zaproponowano użytkownikowi termin wizyty.
**Ostatnia propozycja:** "{last_proposal_text}"
**Odpowiedź użytkownika:** "{user_feedback}"

**Twoje zadanie:** Zwróć **DOKŁADNIE JEDEN** z poniższych trzech znaczników: `[ACCEPT]`, `[REJECT]` lub `[CLARIFY]`.
*   `[ACCEPT]`: Jeśli użytkownik akceptuje (np. "tak", "ok", "pasuje").
*   `[REJECT]`: Jeśli użytkownik odrzuca (np. "nie pasuje", "inny", "za wcześnie", "wolę środę").
*   `[CLARIFY]`: Jeśli odpowiedź jest niejasna lub niezwiązana z terminem (np. "ile?", "nie wiem", "może").

Zwróć tylko jeden znacznik.
"""

# Instrukcja ogólna bez zmian
SYSTEM_INSTRUCTION_GENERAL = """Jesteś przyjaznym i pomocnym asystentem klienta w 'Zakręcone Korepetycje'. Prowadzisz rozmowę na czacie dotyczącą korepetycji online.

**Twoje główne zadania:**
1.  Odpowiadaj rzeczowo i uprzejmie na pytania użytkownika dotyczące oferty, metodyki, dostępności korepetycji.
2.  Utrzymuj konwersacyjny, pomocny ton. Odpowiadaj po polsku.
3.  **Nie podawaj samodzielnie informacji o cenach ani dokładnych metodach płatności.** Jeśli użytkownik o to zapyta, możesz odpowiedzieć ogólnie, np. "Szczegóły dotyczące płatności omawiamy po umówieniu pierwszej lekcji próbnej." lub "Informacje o cenach prześlemy po rezerwacji terminu.".
4.  **Kluczowy cel:** Jeśli w wypowiedzi użytkownika **wyraźnie pojawi się intencja umówienia się na lekcję** (próbną lub zwykłą), rezerwacji terminu, zapytanie o wolne terminy lub chęć rozpoczęcia współpracy, **dodaj na samym końcu swojej odpowiedzi specjalny znacznik:** `{intent_marker}`.

**Przykłady wypowiedzi użytkownika, które powinny skutkować dodaniem znacznika `{intent_marker}`:**
*   "Chciałbym się umówić na lekcję próbną."
*   "Kiedy moglibyśmy zacząć?"
*   "Proszę zaproponować jakiś termin."
*   "Czy macie jakieś wolne godziny w przyszłym tygodniu?"
*   "Jak mogę zarezerwować korepetycje?"
*   "Interesuje mnie ta oferta, jak się umówić?"
*   Pytanie typu: "Ile trwa lekcja i kiedy można ją umówić?" -> Odpowiedz na pierwszą część pytania i dodaj znacznik.

**Przykłady wypowiedzi, po których NIE dodawać znacznika:**
*   "Ile kosztują korepetycje?" (Odpowiedz ogólnie o cenach, bez znacznika).
*   "Jakie przedmioty oferujecie?" (Odpowiedz na pytanie, bez znacznika).
*   "Dziękuję za informacje." (Podziękuj, bez znacznika).

**Zasady:** Zawsze odpowiadaj na bieżące pytanie lub stwierdzenie użytkownika. Znacznik `{intent_marker}` dodawaj **tylko wtedy**, gdy intencja umówienia się jest jasna i bezpośrednia, i **zawsze na samym końcu** odpowiedzi. Nie inicjuj samodzielnie procesu umawiania.
""".format(intent_marker=INTENT_SCHEDULE_MARKER)


# --- Funkcja AI: Propozycja slotu (używa ROZBUDOWANEJ instrukcji) ---
def get_gemini_slot_proposal(user_psid, history_for_proposal_ai, available_ranges):
    """Pobiera propozycję terminu od AI (rozbudowana instrukcja)."""
    if not gemini_model:
        logging.error(f"!!! [{user_psid}] Model Gemini niezaładowany!")
        return None, None
    if not available_ranges:
        logging.warning(f"[{user_psid}]: Brak zakresów do przekazania AI.")
        return None, None # Zwraca None, None

    ranges_text = format_ranges_for_ai(available_ranges)
    system_instruction = SYSTEM_INSTRUCTION_TEXT_PROPOSE.format(available_ranges_text=ranges_text)
    initial_prompt = [
        Content(role="user", parts=[Part.from_text(system_instruction)]),
        Content(role="model", parts=[Part.from_text("Rozumiem. Wybieram termin i zwracam propozycję z [SLOT_ISO:...].")])
    ]
    full_prompt = initial_prompt + history_for_proposal_ai

    max_prompt_messages = (MAX_HISTORY_TURNS * 2) + 2
    while len(full_prompt) > max_prompt_messages:
        full_prompt.pop(2)
        if len(full_prompt) > 2:
            full_prompt.pop(2)

    generated_text = _call_gemini(user_psid, full_prompt, GENERATION_CONFIG_PROPOSAL, "Slot Proposal Conditional")

    if not generated_text:
        logging.error(f"!!! [{user_psid}] Nie uzyskano odpowiedzi Gemini (Slot Proposal Conditional).")
        return "Problem z systemem proponowania terminów.", None

    iso_match = re.search(rf"{re.escape(SLOT_ISO_MARKER_PREFIX)}(.*?){re.escape(SLOT_ISO_MARKER_SUFFIX)}", generated_text)
    if iso_match:
        extracted_iso = iso_match.group(1).strip()
        text_for_user = re.sub(rf"{re.escape(SLOT_ISO_MARKER_PREFIX)}.*?{re.escape(SLOT_ISO_MARKER_SUFFIX)}", "", generated_text).strip()
        text_for_user = re.sub(r'\s+', ' ', text_for_user).strip()

        logging.info(f"[{user_psid}] AI (Conditional) propozycja: ISO={extracted_iso}, Tekst='{text_for_user}'")
        try:
            tz = _get_timezone()
            logging.debug(f"    Surowe ISO od AI: {extracted_iso}")
            try:
                 proposed_start_naive = datetime.datetime.fromisoformat(extracted_iso)
                 if proposed_start_naive.tzinfo is None:
                     proposed_start = tz.localize(proposed_start_naive)
                     logging.debug(f"    ISO sparsowane jako 'naiwne', zlokalizowano do {CALENDAR_TIMEZONE}: {proposed_start}")
                 else:
                     proposed_start = proposed_start_naive.astimezone(tz)
                     logging.debug(f"    ISO sparsowane ze strefą, skonwertowano do {CALENDAR_TIMEZONE}: {proposed_start}")
            except ValueError:
                 logging.error(f"!!! BŁĄD AI (Conditional) [{user_psid}]: '{extracted_iso}' nie jest ISO!")
                 raise

            proposed_end = proposed_start + datetime.timedelta(minutes=APPOINTMENT_DURATION_MINUTES)
            logging.debug(f"    Proponowany przedział: {proposed_start} - {proposed_end}")
            logging.debug(f"    Dostępne zakresy (przed walidacją):")
            for r_idx, r in enumerate(available_ranges):
                logging.debug(f"      Zakres {r_idx}: {r['start']} - {r['end']}")

            is_within = False
            for r_idx, r in enumerate(available_ranges):
                logging.debug(f"      Walidacja z zakresem {r_idx}: Czy {r['start']} <= {proposed_start}? {r['start'] <= proposed_start}. Czy {proposed_end} <= {r['end']}? {proposed_end <= r['end']}")
                if r['start'] <= proposed_start and proposed_end <= r['end']:
                    is_within = True
                    logging.debug(f"        => PASUJE!")
                    break
                else:
                     logging.debug(f"        => NIE PASUJE.")

            if not is_within:
                 logging.error(f"!!! BŁĄD Walidacji AI (Conditional) [{user_psid}]: ISO '{extracted_iso}' poza zakresami!")
                 return None, None # Zwracamy None, None

            if is_slot_actually_free(proposed_start, TARGET_CALENDAR_ID):
                 if not text_for_user: # Generowanie tekstu jeśli AI dało tylko ISO
                      formatted_slot = format_slot_for_user(proposed_start)
                      text_for_user = f"Proponuję termin: {formatted_slot}. Pasuje?"
                      logging.info(f"    AI zwróciło tylko ISO, wygenerowano tekst: '{text_for_user}'")
                 return text_for_user, extracted_iso
            else:
                 logging.warning(f"!!! [{user_psid}]: Slot {extracted_iso} ZAJĘTY (weryfikacja)!")
                 return None, None # Zwracamy None, None
        except ValueError:
             return "Błąd przetwarzania terminu.", None
        except Exception as val_err:
             logging.error(f"!!! BŁĄD Walidacji AI (Conditional) [{user_psid}]: {val_err}", exc_info=True)
             return "Błąd weryfikacji terminu.", None
    else:
        logging.critical(f"!!! KRYTYCZNY BŁĄD AI (Conditional) [{user_psid}]: Brak ISO! Odp: '{generated_text}'")
        clean_text = generated_text.strip()
        if clean_text:
             return clean_text, None
        else:
             return "Błąd generowania propozycji.", None

# --- Funkcja AI: Interpretacja feedbacku (UPROSZCZONA) ---
def get_gemini_simple_feedback_decision(user_psid, user_feedback_text, history_for_feedback_ai, last_proposed_slot_text):
     """Prosi AI o zwrócenie [ACCEPT], [REJECT] lub [CLARIFY]."""
     if not gemini_model:
         logging.error(f"!!! [{user_psid}] Model Gemini niezaładowany!")
         return "[CLARIFY]"

     instruction = SYSTEM_INSTRUCTION_TEXT_FEEDBACK_SIMPLE.format(
         last_proposal_text=last_proposed_slot_text, user_feedback=user_feedback_text
     )
     prompt = [Content(role="user", parts=[Part.from_text(instruction)])]
     max_hist_messages = (MAX_HISTORY_TURNS - 2) * 2
     if len(history_for_feedback_ai) > max_hist_messages:
         prompt.extend(history_for_feedback_ai[-max_hist_messages:])
     else:
         prompt.extend(history_for_feedback_ai)
     prompt.append(Content(role="user", parts=[Part.from_text(user_feedback_text)]))

     decision_tag = _call_gemini(user_psid, prompt, GENERATION_CONFIG_FEEDBACK_SIMPLE, "Simple Feedback Interpretation")

     if not decision_tag:
         logging.error(f"!!! [{user_psid}] Nie uzyskano odpowiedzi Gemini (Simple Feedback).")
         return "[CLARIFY]"

     valid_tags = ["[ACCEPT]", "[REJECT]", "[CLARIFY]"]
     if decision_tag in valid_tags:
          logging.info(f"[{user_psid}] Decyzja AI (Simple Feedback): {decision_tag}")
          return decision_tag
     else:
          logging.warning(f"Ostrz. [{user_psid}]: AI (Simple Feedback) zwróciło '{decision_tag}'. Traktuję jako CLARIFY.")
          return "[CLARIFY]"

# --- Funkcja AI: Ogólna rozmowa (bez zmian) ---
def get_gemini_general_response(user_psid, current_user_message_text, history_for_general_ai):
    """Prowadzi ogólną rozmowę z AI."""
    if not gemini_model:
        logging.error(f"!!! [{user_psid}] Model Gemini niezaładowany!")
        return "Przepraszam, problem z systemem."
    initial_prompt = [
        Content(role="user", parts=[Part.from_text(SYSTEM_INSTRUCTION_GENERAL)]),
        Content(role="model", parts=[Part.from_text("Rozumiem.")])
        ]
    full_prompt = initial_prompt + history_for_general_ai
    full_prompt.append(Content(role="user", parts=[Part.from_text(current_user_message_text)]))
    max_prompt_messages = (MAX_HISTORY_TURNS * 2) + 2
    while len(full_prompt) > max_prompt_messages:
        full_prompt.pop(2) # Usuń user
        if len(full_prompt) > 2:
            full_prompt.pop(2) # Usuń model
    response_text = _call_gemini(user_psid, full_prompt, GENERATION_CONFIG_DEFAULT, "General Conversation")
    if response_text:
        if SLOT_ISO_MARKER_PREFIX in response_text:
             logging.warning(f"[{user_psid}] AI (General) błędnie dodało znacznik ISO.")
             response_text = re.sub(rf"{re.escape(SLOT_ISO_MARKER_PREFIX)}.*?{re.escape(SLOT_ISO_MARKER_SUFFIX)}", "", response_text).strip()
        return response_text
    else:
        logging.error(f"!!! [{user_psid}] Nie uzyskano odpowiedzi Gemini (General).")
        return "Przepraszam, błąd przetwarzania."

# =====================================================================
# === WEBHOOK HANDLERS ================================================
# =====================================================================

@app.route('/webhook', methods=['GET'])
def webhook_verification():
    """Obsługuje weryfikację webhooka Facebooka."""
    logging.info("--- GET /webhook (Weryfikacja) ---")
    hub_mode = request.args.get('hub.mode')
    hub_token = request.args.get('hub.verify_token')
    hub_challenge = request.args.get('hub.challenge')
    logging.debug(f"Mode: {hub_mode}, Challenge: {hub_challenge}")
    if hub_mode == 'subscribe' and hub_token == VERIFY_TOKEN:
        logging.info("Weryfikacja GET OK!")
        return Response(hub_challenge, status=200)
    else:
        logging.warning(f"Weryfikacja GET NIEUDANA. Token: '{hub_token}' vs '{VERIFY_TOKEN}'")
        return Response("Verification failed", status=403)

@app.route('/webhook', methods=['POST'])
def webhook_handle():
    """Główny handler dla przychodzących zdarzeń z Messengera."""
    logging.info(f"\n{'='*30} {datetime.datetime.now(_get_timezone()):%Y-%m-%d %H:%M:%S %Z} POST /webhook {'='*30}")
    raw_data = request.data
    data = None
    try:
        decoded_data = raw_data.decode('utf-8')
        data = json.loads(decoded_data)
        if data and data.get("object") == "page":
            for entry in data.get("entry", []):
                for event in entry.get("messaging", []):
                    sender_id = event.get("sender", {}).get("id")
                    if not sender_id:
                        logging.warning("Pominięto zdarzenie bez sender.id.")
                        continue
                    logging.info(f"--- Zdarzenie dla PSID: {sender_id} ---")
                    history, context = load_history(sender_id)
                    history_for_gemini = [h for h in history if isinstance(h, Content) and h.role in ('user', 'model')]
                    current_state = context.get('type', STATE_GENERAL)
                    last_iso_from_context = context.get('slot_iso') if current_state == STATE_WAITING_FOR_FEEDBACK else None
                    last_proposal_text_for_feedback = "poprzedni termin"
                    if last_iso_from_context:
                         try:
                             last_dt = datetime.datetime.fromisoformat(last_iso_from_context).astimezone(_get_timezone())
                             last_proposal_text_for_feedback = format_slot_for_user(last_dt)
                         except Exception as fmt_err:
                             logging.warning(f"Nie sformatowano ISO '{last_iso_from_context}' dla feedbacku: {fmt_err}")
                         logging.info(f"    Aktywny stan: {current_state}, Ostatnie ISO: {last_iso_from_context}")
                    else:
                         logging.info(f"    Aktywny stan: {current_state}")

                    action = None
                    msg_result = None
                    next_state = current_state
                    ctx_save_payload = {}
                    if current_state == STATE_WAITING_FOR_FEEDBACK and last_iso_from_context:
                         ctx_save_payload['slot_iso'] = last_iso_from_context

                    model_resp_content = None
                    user_content = None

                    # === Obsługa wiadomości tekstowych ===
                    if message_data := event.get("message"):
                        if message_data.get("is_echo"):
                            logging.debug(f"    Pominięto echo.")
                            continue
                        user_input_text = message_data.get("text", "").strip()
                        if user_input_text:
                            user_content = Content(role="user", parts=[Part.from_text(user_input_text)])
                            logging.info(f"    Otrzymano tekst (stan={current_state}): '{user_input_text[:100]}{'...' if len(user_input_text)>100 else ''}'")
                            if ENABLE_TYPING_DELAY: time.sleep(MIN_TYPING_DELAY_SECONDS * 0.5)

                            if current_state == STATE_WAITING_FOR_FEEDBACK:
                                logging.info("      -> Stan: Oczekiwanie na Feedback. Pytanie AI (Simple Feedback)...")
                                try:
                                    decision = get_gemini_simple_feedback_decision(
                                        sender_id, user_input_text, history_for_gemini, last_proposal_text_for_feedback
                                    )
                                    if decision == "[ACCEPT]":
                                        action = 'book'; logging.info(f"      Decyzja: {decision} -> Akcja: Rezerwacja, Stan: General")
                                        next_state = STATE_GENERAL; ctx_save_payload = {}
                                    elif decision == "[REJECT]":
                                        action = 'find_and_propose'; logging.info(f"      Decyzja: {decision} -> Akcja: Odrzucenie i szukanie, Stan: SchedulingActive")
                                        msg_result = "Rozumiem. W takim razie poszukam innego terminu..."
                                        next_state = STATE_SCHEDULING_ACTIVE; ctx_save_payload = {}
                                    elif decision == "[CLARIFY]":
                                        logging.info(f"      Decyzja: {decision} -> Akcja: Odpowiedź przez AI General, Stan: General")
                                        action = 'send_general_ai_response'
                                        next_state = STATE_GENERAL; ctx_save_payload = {}
                                    else:
                                        action = 'send_error'; logging.warning(f"      Niespodziewana decyzja AI (Simple Feedback): {decision}.")
                                        msg_result = "Problem ze zrozumieniem odpowiedzi."; next_state = STATE_GENERAL; ctx_save_payload = {}
                                except Exception as feedback_err:
                                    logging.error(f"!!! BŁĄD AI (Simple Feedback): {feedback_err}", exc_info=True)
                                    action = 'send_error'; msg_result = "Błąd interpretacji odpowiedzi."
                                    next_state = STATE_GENERAL; ctx_save_payload = {}
                            else: # Stan GENERAL lub SCHEDULING_ACTIVE
                                logging.info(f"      -> Stan: {current_state}. Pytanie AI (General)...")
                                response = get_gemini_general_response(sender_id, user_input_text, history_for_gemini)
                                if response:
                                    if INTENT_SCHEDULE_MARKER in response:
                                        logging.info(f"      AI wykryło intencję [{INTENT_SCHEDULE_MARKER}].")
                                        action = 'find_and_propose'
                                        initial_resp_text = response.split(INTENT_SCHEDULE_MARKER, 1)[0].strip()
                                        msg_result = initial_resp_text if initial_resp_text else "Sprawdzę terminy."
                                        next_state = STATE_SCHEDULING_ACTIVE; ctx_save_payload = {}
                                    else:
                                        action = 'send_gemini_response'
                                        msg_result = response
                                        next_state = STATE_GENERAL; ctx_save_payload = {}
                                else:
                                    action = 'send_error'; msg_result = "Błąd przetwarzania."
                                    next_state = STATE_GENERAL; ctx_save_payload = {}

                        elif attachments := message_data.get("attachments"):
                             att_type = attachments[0].get('type','nieznany')
                             logging.info(f"      Otrzymano załącznik: {att_type}.")
                             user_content = Content(role="user", parts=[Part.from_text(f"[Załącznik: {att_type}]")])
                             msg_result = "Nie obsługuję załączników." if att_type != 'sticker' else "👍"
                             action = 'send_info'; next_state = current_state
                        else: # Pusta wiadomość
                            logging.info("      Otrzymano pustą wiadomość.")
                            if current_state == STATE_WAITING_FOR_FEEDBACK:
                                action = 'send_clarification'
                                msg_result = "Nie otrzymałem odpowiedzi. Czy termin pasuje?"
                                next_state = STATE_WAITING_FOR_FEEDBACK
                            else:
                                action = None; next_state = current_state; ctx_save_payload = {}

                    # === Obsługa Postback ===
                    elif postback := event.get("postback"):
                        payload = postback.get("payload"); title = postback.get("title", "")
                        logging.info(f"    Postback: '{payload}', Tytuł: '{title}', Stan: {current_state}")
                        user_content = Content(role="user", parts=[Part.from_text(f"[Przycisk: {title} ({payload})]")])
                        if payload == "ACCEPT_SLOT":
                            if current_state == STATE_WAITING_FOR_FEEDBACK and last_iso_from_context:
                                action = 'book'; msg_result = None; next_state = STATE_GENERAL; ctx_save_payload = {}
                            else: action = 'send_info'; msg_result = "Propozycja wygasła."; next_state = STATE_GENERAL; ctx_save_payload = {}
                        elif payload == "REJECT_SLOT":
                            if current_state == STATE_WAITING_FOR_FEEDBACK and last_iso_from_context:
                                action = 'find_and_propose'; msg_result = "OK, szukam innego terminu..."; next_state = STATE_SCHEDULING_ACTIVE; ctx_save_payload = {}
                            else: action = 'send_info'; msg_result = "Brak propozycji do odrzucenia."; next_state = STATE_GENERAL; ctx_save_payload = {}
                        else: # Inne postbacki
                            action = 'send_general_ai_response'; next_state = STATE_GENERAL; ctx_save_payload = {}

                    # === Inne zdarzenia ===
                    elif event.get("read"): logging.debug(f"    Potw. odczytania."); continue
                    elif event.get("delivery"): logging.debug(f"    Potw. dostarczenia."); continue
                    else: logging.warning(f"    Nieobsługiwany typ zdarzenia: {json.dumps(event)}"); continue


                    # --- WYKONANIE ZAPLANOWANEJ AKCJI (z pętlą) ---
                    history_saved_in_this_cycle = False
                    loop_guard = 0
                    while action and loop_guard < 3:
                        loop_guard += 1
                        logging.info(f"  >> Pętla akcji {loop_guard}/3 | Akcja: {action} | Stan: {next_state}")
                        current_action = action
                        action = None # Reset

                        if current_action == 'book':
                            if last_iso_from_context:
                                try:
                                    tz= _get_timezone(); start = datetime.datetime.fromisoformat(last_iso_from_context).astimezone(tz); end = start + datetime.timedelta(minutes=APPOINTMENT_DURATION_MINUTES); prof = get_user_profile(sender_id); name = prof.get('first_name', '') if prof else f"U_{sender_id[-4:]}"
                                    desc = f"FB Bot\nPSID: {sender_id}" + (f"\nNazwisko: {prof.get('last_name')}" if prof and prof.get('last_name') else "")
                                    ok, booking_msg = book_appointment(TARGET_CALENDAR_ID, start, end, f"FB: {name}", desc, name)
                                    msg_result = booking_msg
                                    next_state = STATE_GENERAL; ctx_save_payload = {} # Zawsze resetuj po próbie rezerwacji
                                    if not ok: logging.warning("Rezerwacja nie powiodła się.")
                                except Exception as e: logging.error(f"!!! BŁĄD rezerwacji {last_iso_from_context}: {e}", exc_info=True); msg_result = "Błąd rezerwacji."; next_state = STATE_GENERAL; ctx_save_payload = {}
                            else: logging.error("!!! BŁĄD LOGIKI: 'book' bez 'last_iso' !!!"); msg_result = "Błąd systemu."; next_state = STATE_GENERAL; ctx_save_payload = {}
                        elif current_action == 'find_and_propose':
                            try:
                                tz = _get_timezone(); now = datetime.datetime.now(tz); search_start = now; search_end_date = (search_start + datetime.timedelta(days=MAX_SEARCH_DAYS)).date(); search_end = tz.localize(datetime.datetime.combine(search_end_date, datetime.time(WORK_END_HOUR, 0)))
                                logging.info(f"      -> Szukanie od {search_start:%Y-%m-%d %H:%M} do {search_end:%Y-%m-%d %H:%M}")
                                _simulate_typing(sender_id, MAX_TYPING_DELAY_SECONDS * 0.8)
                                free_ranges = get_free_time_ranges(TARGET_CALENDAR_ID, search_start, search_end)
                                if free_ranges:
                                    logging.info(f"      Znaleziono {len(free_ranges)} zakresów. AI (Proposal Conditional)...")
                                    history_for_proposal_ai = history_for_gemini + ([user_content] if user_content else [])
                                    proposal_text, proposed_iso = get_gemini_slot_proposal(sender_id, history_for_proposal_ai, free_ranges)
                                    if proposal_text and proposed_iso:
                                        final_proposal_msg = (msg_result + "\n\n" + proposal_text) if msg_result else proposal_text
                                        msg_result = final_proposal_msg
                                        next_state = STATE_WAITING_FOR_FEEDBACK
                                        ctx_save_payload = {'slot_iso': proposed_iso}
                                    elif proposal_text and not proposed_iso:
                                         logging.warning(f"      AI (Conditional) dało tekst '{proposal_text[:50]}...' bez ISO.")
                                         final_proposal_msg = (msg_result + "\n\n" + proposal_text) if msg_result else proposal_text
                                         msg_result = final_proposal_msg
                                         next_state = STATE_GENERAL; ctx_save_payload = {}
                                    else: # Błąd AI lub walidacji/zajęty
                                        fail_msg = proposal_text if proposal_text else "Problem ze znalezieniem terminu."
                                        if proposal_text is None and proposed_iso is None: fail_msg = f"Niestety, brak wolnych terminów w ciągu {MAX_SEARCH_DAYS} dni."
                                        msg_result = (msg_result + "\n\n" + fail_msg) if msg_result else fail_msg
                                        next_state = STATE_GENERAL; ctx_save_payload = {}
                                else: # Brak wolnych zakresów
                                    logging.warning(f"      Brak wolnych zakresów.")
                                    no_slots_msg = f"Niestety, brak wolnych terminów w ciągu {MAX_SEARCH_DAYS} dni."
                                    msg_result = (msg_result + "\n\n" + no_slots_msg) if msg_result else no_slots_msg
                                    next_state = STATE_GENERAL; ctx_save_payload = {}
                            except Exception as find_err:
                                logging.error(f"!!! BŁĄD szukania/proponowania: {find_err}", exc_info=True)
                                error_msg = "Błąd wyszukiwania terminów."
                                msg_result = (msg_result + "\n\n" + error_msg) if msg_result else error_msg
                                next_state = STATE_GENERAL; ctx_save_payload = {}
                        elif current_action == 'send_general_ai_response':
                             logging.info(f"      -> Akcja: Przekazanie do AI General...")
                             if user_content and user_content.parts:
                                 input_text_for_general = user_content.parts[0].text
                                 response = get_gemini_general_response(sender_id, input_text_for_general, history_for_gemini)
                                 if response:
                                     if INTENT_SCHEDULE_MARKER in response:
                                         logging.info(f"      AI General odpowiedziało i wykryło intencję. Ustawianie akcji 'find_and_propose'.")
                                         action = 'find_and_propose' # Ustaw akcję na następną iterację
                                         initial_resp_text = response.split(INTENT_SCHEDULE_MARKER, 1)[0].strip()
                                         msg_result = initial_resp_text if initial_resp_text else "Sprawdzę terminy."
                                         next_state = STATE_SCHEDULING_ACTIVE; ctx_save_payload = {}
                                         continue # Przejdź do następnej iteracji
                                     else: # Zwykła odpowiedź
                                         msg_result = response
                                         next_state = STATE_GENERAL; ctx_save_payload = {}
                                         # action jest None, pętla zakończy się
                                 else: # Błąd AI General
                                     msg_result = "Błąd przetwarzania."
                                     next_state = STATE_GENERAL; ctx_save_payload = {}
                                     # action jest None
                             else:
                                 logging.error("!!! Błąd logiki: 'send_general_ai_response' bez user_content !!!")
                                 msg_result = "Wewnętrzny błąd."; next_state = STATE_GENERAL; ctx_save_payload = {}
                                 # action jest None
                        elif current_action in ['send_gemini_response', 'send_clarification', 'send_error', 'send_info']:
                            logging.debug(f"      Akcja: {current_action}. Wiadomość gotowa.")
                            # action jest None, pętla zakończy się
                            pass
                        else:
                             logging.warning(f"   Brak lub nieznana akcja '{current_action}'. Zakończenie pętli.")
                             break

                    # --- WYSYŁANIE ODPOWIEDZI I ZAPIS STANU ---
                    final_context_to_save = {}
                    if next_state != STATE_GENERAL:
                        final_context_to_save['type'] = next_state
                        if isinstance(ctx_save_payload, dict):
                            payload_data = ctx_save_payload.copy()
                            payload_data.pop('type', None)
                            final_context_to_save.update(payload_data)
                        final_context_to_save['type'] = next_state # Upewnij się

                    if msg_result:
                        send_message(sender_id, msg_result)
                        model_resp_content = Content(role="model", parts=[Part.from_text(msg_result)])
                    elif current_action: # Używamy current_action, bo action może być None po pętli
                        logging.warning(f"    Akcja '{current_action}' zakończona bez wiadomości do wysłania.")

                    context_for_comparison = context.copy(); context_for_comparison.pop('role', None)
                    final_context_for_comparison = final_context_to_save.copy(); final_context_for_comparison.pop('role', None)
                    should_save = bool(user_content) or bool(model_resp_content) or (context_for_comparison != final_context_for_comparison)

                    if should_save:
                        history_to_save = list(history)
                        if user_content: history_to_save.append(user_content)
                        if model_resp_content: history_to_save.append(model_resp_content)
                        logging.info(f"Zapis historii. Nowy kontekst/stan: {final_context_to_save}")
                        save_history(sender_id, history_to_save, context_to_save=final_context_to_save)
                        history_saved_in_this_cycle = True
                    else:
                        logging.debug("    Brak zmian w historii lub stanie - pomijanie zapisu.")

            logging.info(f"--- Koniec POST batch ---")
            return Response("EVENT_RECEIVED", status=200)
        else:
            logging.warning(f"POST nie 'page' (typ: {data.get('object') if data else 'Brak'}).")
            return Response("OK", status=200)
    except json.JSONDecodeError as e:
        logging.error(f"!!! BŁĄD JSON POST: {e}", exc_info=True)
        logging.error(f"    Dane: {raw_data[:500]}...")
        return Response("Invalid JSON", status=400)
    except Exception as e:
        logging.critical(f"!!! KRYTYCZNY BŁĄD POST: {e}", exc_info=True)
        return Response("ERROR", status=200)

# =====================================================================
# === URUCHOMIENIE SERWERA ============================================
# =====================================================================
if __name__ == '__main__':
    ensure_dir(HISTORY_DIR)
    log_level = logging.DEBUG # Ustaw logowanie na DEBUG
    if not logging.getLogger().hasHandlers():
        logging.basicConfig(level=log_level, format='%(asctime)s - %(levelname)s - [%(funcName)s:%(lineno)d] - %(message)s', datefmt='%Y-%m-%d %H:%M:%S')

    logging.getLogger('googleapiclient.discovery_cache').setLevel(logging.ERROR)
    logging.getLogger('urllib3.connectionpool').setLevel(logging.WARNING)
    logging.getLogger('werkzeug').setLevel(logging.WARNING)

    print("\n" + "="*60 + "\n--- START KONFIGURACJI BOTA ---")
    print(f"  * Poziom logowania: {logging.getLevelName(log_level)}")
    print("-" * 60)
    print("  Konfiguracja Facebook:")
    print(f"    FB_VERIFY_TOKEN: {'OK' if VERIFY_TOKEN != 'KOLAGEN' else 'Użyto domyślny (KOLAGEN!)'}")
    if not PAGE_ACCESS_TOKEN or len(PAGE_ACCESS_TOKEN) < 50: print("!!! KRYTYCZNE: FB_PAGE_ACCESS_TOKEN PUSTY lub ZBYT KRÓTKI !!!")
    elif PAGE_ACCESS_TOKEN == "EACNAHFzEhkUBOxSDMfOZCYbQAFKfVzJWowJpX8mcX0BvBGaWFRiUwNHjojZBcRXIPFszKzzRZBEqFI7AFD0DpI5sOeiN7HKLBGxBZB7tAgCkFdipRNQKevuP3F4kvSTIZCqqkrBaq7rPRM7FIqNQjP2Ju9UdZB5FNcvndzdZBZBGxTyyw9hkWmBndNr2A0VwO2Gf8QZDZD": print("!!! UWAGA: Używany jest TESTOWY/DOMYŚLNY FB_PAGE_ACCESS_TOKEN - NIE zadziała w produkcji! !!!")
    else: print("    FB_PAGE_ACCESS_TOKEN: Ustawiony (OK)")
    print("-" * 60)
    print("  Konfiguracja Ogólna:")
    print(f"    Katalog historii: {HISTORY_DIR}")
    print(f"    Maks. tur historii AI: {MAX_HISTORY_TURNS}")
    print(f"    Limit znaków wiad. FB: {MESSAGE_CHAR_LIMIT}")
    print(f"    Opóźnienie między fragm.: {MESSAGE_DELAY_SECONDS}s")
    print(f"    Symulacja pisania: {'Włączona' if ENABLE_TYPING_DELAY else 'Wyłączona'}")
    if ENABLE_TYPING_DELAY: print(f"      Min/Max czas pisania: {MIN_TYPING_DELAY_SECONDS}s / {MAX_TYPING_DELAY_SECONDS}s; Prędkość: {TYPING_CHARS_PER_SECOND} zn/s")
    print("-" * 60)
    print("  Konfiguracja Vertex AI:")
    print(f"    Projekt GCP: {PROJECT_ID}")
    print(f"    Lokalizacja GCP: {LOCATION}")
    print(f"    Model AI: {MODEL_ID}")
    if not gemini_model: print("!!! OSTRZEŻENIE: Model Gemini AI NIE załadowany poprawnie! !!!")
    else: print(f"    Model Gemini AI ({MODEL_ID}): Załadowany (OK)")
    print("-" * 60)
    print("  Konfiguracja Kalendarza Google:")
    print(f"    ID Kalendarza: {TARGET_CALENDAR_ID}")
    print(f"    Strefa czasowa: {CALENDAR_TIMEZONE} (Obiekt TZ: {_get_timezone()})")
    print(f"    Czas trwania wizyty: {APPOINTMENT_DURATION_MINUTES} min")
    print(f"    Godziny pracy: {WORK_START_HOUR}:00 - {WORK_END_HOUR}:00")
    print(f"    Preferowane godz. AI: W tyg >= {PREFERRED_WEEKDAY_START_HOUR}:00, Weekend >= {PREFERRED_WEEKEND_START_HOUR}:00")
    print(f"    Maks. zakres szukania: {MAX_SEARCH_DAYS} dni")
    print(f"    Plik klucza API: {SERVICE_ACCOUNT_FILE} ({'Znaleziono' if os.path.exists(SERVICE_ACCOUNT_FILE) else 'BRAK!!!'})")
    cal_service = get_calendar_service()
    if not cal_service and os.path.exists(SERVICE_ACCOUNT_FILE): print("!!! OSTRZ.: Usługa Google Calendar NIE zainicjowana.")
    elif not os.path.exists(SERVICE_ACCOUNT_FILE): print("!!! OSTRZ.: Brak pliku klucza Google Calendar.")
    elif cal_service: print("    Usługa Google Calendar: Zainicjowana (OK)")
    print("--- KONIEC KONFIGURACJI BOTA ---"); print("="*60 + "\n")

    port = int(os.environ.get("PORT", 8080)); flask_debug_mode = os.environ.get("FLASK_DEBUG", "False").lower() in ("true", "1", "yes")
    run_flask_in_debug = (log_level == logging.DEBUG)

    print(f"Uruchamianie serwera Flask na porcie {port}...")
    if not run_flask_in_debug:
        try: from waitress import serve; print(">>> Serwer produkcyjny Waitress START <<<"); serve(app, host='0.0.0.0', port=port, threads=8)
        except ImportError: print("!!! Ostrz.: 'waitress' nie znaleziono. Uruchamianie serwera dev Flask."); print(">>> Serwer deweloperski Flask START <<<"); app.run(host='0.0.0.0', port=port, debug=False)
    else:
        print(">>> Serwer deweloperski Flask (DEBUG MODE for Logging) START <<<")
        app.run(host='0.0.0.0', port=port, debug=True) # Uruchom z debug=True dla logów DEBUG
