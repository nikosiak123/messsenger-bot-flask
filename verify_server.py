# -*- coding: utf-8 -*-

# verify_server.py (Wersja: Autonomiczne AI + Odczyt Kalendarza + Weryfikacja + Zbieranie Info + Zapis Sheets + Parent API)

from flask import Flask, request, Response
import os
import json
import requests
import time
import vertexai
import random
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

# --- Importy Google Calendar (ODCZYT/WERYFIKACJA) ---
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
# ----------------------------------------------------

# --- Importy Google Sheets (ZAPIS) ---
# (Zakładamy, że te same biblioteki są używane, ale dla innego API)
# from google.oauth2 import service_account  # Już zaimportowane
# from googleapiclient.discovery import build # Już zaimportowane
# from googleapiclient.errors import HttpError # Już zaimportowane
# --------------------------------------

app = Flask(__name__)

# --- Konfiguracja Ogólna ---
VERIFY_TOKEN = os.environ.get("FB_VERIFY_TOKEN", "KOLAGEN")
PAGE_ACCESS_TOKEN = os.environ.get("FB_PAGE_ACCESS_TOKEN", "EACNAHFzEhkUBOxSDMfOZCYbQAFKfVzJWowJpX8mcX0BvBGaWFRiUwNHjojZBcRXIPFszKzzRZBEqFI7AFD0DpI5sOeiN7HKLBGxBZB7tAgCkFdipRNQKevuP3F4kvSTIZCqqkrBaq7rPRM7FIqNQjP2Ju9UdZB5FNcvndzdZBZBGxTyyw9hkWmBndNr2A0VwO2Gf8QZDZD") # Testowy token
PROJECT_ID = os.environ.get("GCP_PROJECT_ID", "linear-booth-450221-k1")
LOCATION = os.environ.get("GCP_LOCATION", "us-central1")
MODEL_ID = os.environ.get("VERTEX_MODEL_ID", "gemini-2.0-flash-001")

FACEBOOK_GRAPH_API_URL = f"https://graph.facebook.com/v19.0/me/messages"

HISTORY_DIR = "conversation_store"
MAX_HISTORY_TURNS = 15
MESSAGE_CHAR_LIMIT = 1990
MESSAGE_DELAY_SECONDS = 1.2

ENABLE_TYPING_DELAY = True
MIN_TYPING_DELAY_SECONDS = 0.7
MAX_TYPING_DELAY_SECONDS = 3.0
TYPING_CHARS_PER_SECOND = 35

# --- Konfiguracja Kalendarza (ODCZYT/WERYFIKACJA) ---
SERVICE_ACCOUNT_FILE = 'kalendarzklucz.json' # Ten sam plik klucza dla obu API
CALENDAR_SCOPES = ['https://www.googleapis.com/auth/calendar.readonly'] # Tylko odczyt potrzebny
CALENDAR_TIMEZONE = 'Europe/Warsaw'
APPOINTMENT_DURATION_MINUTES = 60 # Nadal potrzebne do obliczania zakresów
WORK_START_HOUR = 7
WORK_END_HOUR = 22
TARGET_CALENDAR_ID = 'f19e189826b9d6e36950da347ac84d5501ecbd6bed0d76c8641be61a67749c67@group.calendar.google.com' # Kalendarz do sprawdzania dostępności
MAX_SEARCH_DAYS = 14
MIN_BOOKING_LEAD_HOURS = 24 # Filtr 24h nadal aktywny przy szukaniu

# --- Konfiguracja Google Sheets (ZAPIS) ---
SHEET_SCOPES = ['https://www.googleapis.com/auth/spreadsheets'] # Zakres zapisu do arkuszy
SPREADSHEET_ID = os.environ.get("GOOGLE_SHEET_ID", "1vpsIAEkqtY3ZJ5Mr67Dda45aZ55V1O-Ux9ODjwk13qw") # Arkusz do zapisu danych
SHEET_NAME = os.environ.get("GOOGLE_SHEET_NAME", 'Arkusz1')
SHEET_TIMEZONE = 'Europe/Warsaw' # Strefa czasowa dla formatowania w arkuszu

# --- Znaczniki i Stany ---
INTENT_SCHEDULE_MARKER = "[INTENT_SCHEDULE]"
SLOT_ISO_MARKER_PREFIX = "[SLOT_ISO:"
SLOT_ISO_MARKER_SUFFIX = "]"
INFO_GATHERED_MARKER = "[INFO_GATHERED]"
STATE_GENERAL = "general"
STATE_SCHEDULING_ACTIVE = "scheduling_active"
STATE_GATHERING_INFO = "gathering_info"

# --- Ustawienia Modelu Gemini ---
GENERATION_CONFIG_SCHEDULING = GenerationConfig(
    temperature=0.5, top_p=0.95, top_k=40, max_output_tokens=512, # Może potrzebować więcej tokenów z listą zakresów
)
GENERATION_CONFIG_GATHERING = GenerationConfig(
    temperature=0.4, top_p=0.95, top_k=40, max_output_tokens=350,
)
GENERATION_CONFIG_DEFAULT = GenerationConfig(
    temperature=0.7, top_p=0.95, top_k=40, max_output_tokens=1024,
)

# --- Bezpieczeństwo AI ---
SAFETY_SETTINGS = [
    SafetySetting(category=HarmCategory.HARM_CATEGORY_HARASSMENT, threshold=HarmBlockThreshold.BLOCK_MEDIUM_AND_ABOVE),
    # ... (reszta bez zmian) ...
    SafetySetting(category=HarmCategory.HARM_CATEGORY_HATE_SPEECH, threshold=HarmBlockThreshold.BLOCK_MEDIUM_AND_ABOVE),
    SafetySetting(category=HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT, threshold=HarmBlockThreshold.BLOCK_MEDIUM_AND_ABOVE),
    SafetySetting(category=HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT, threshold=HarmBlockThreshold.BLOCK_MEDIUM_AND_ABOVE),
]

# --- Inicjalizacja Zmiennych Globalnych ---
_calendar_service = None
_sheets_service = None
_cal_tz = None
_sheet_tz = None
POLISH_WEEKDAYS = ["Poniedziałek", "Wtorek", "Środa", "Czwartek", "Piątek", "Sobota", "Niedziela"] # Przywrócone

# --- Ustawienia Lokalizacji ---
try: locale.setlocale(locale.LC_TIME, 'pl_PL.UTF-8')
except locale.Error:
    try: locale.setlocale(locale.LC_TIME, 'Polish_Poland.1250')
    except locale.Error: logging.warning("Nie można ustawić polskiej lokalizacji dla formatowania dat.")

# =====================================================================
# === INICJALIZACJA AI (bez zmian) ====================================
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
# === FUNKCJE POMOCNICZE (Ogólne - bez zmian) =========================
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
    # ... (bez zmian od poprzedniej wersji - funkcja jest poprawna) ...
    if not PAGE_ACCESS_TOKEN or len(PAGE_ACCESS_TOKEN) < 50:
        logging.warning(f"[{psid}] Brak/nieprawidłowy PAGE_ACCESS_TOKEN do pobrania profilu.")
        return None
    USER_PROFILE_API_URL_TEMPLATE = "https://graph.facebook.com/v19.0/{psid}?fields=first_name,last_name,profile_pic&access_token={token}"
    url = USER_PROFILE_API_URL_TEMPLATE.format(psid=psid, token=PAGE_ACCESS_TOKEN)
    logging.debug(f"--- [{psid}] Pobieranie profilu użytkownika z FB API...")
    profile_data = {}
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        data = r.json()
        if 'error' in data:
            logging.error(f"BŁĄD FB API (pobieranie profilu) dla PSID {psid}: {data['error']}")
            if data['error'].get('code') == 190:
                 logging.error("!!! Wygląda na to, że FB_PAGE_ACCESS_TOKEN jest nieprawidłowy lub wygasł !!!")
            return None
        profile_data['first_name'] = data.get('first_name')
        profile_data['last_name'] = data.get('last_name')
        profile_data['profile_pic'] = data.get('profile_pic')
        profile_data['id'] = data.get('id')
        if profile_data.get('first_name') or profile_data.get('last_name'):
            logging.info(f"[{psid}] Pomyślnie pobrano profil: Imię='{profile_data.get('first_name', 'Brak')}', Nazwisko='{profile_data.get('last_name', 'Brak')}'")
        else:
            logging.warning(f"[{psid}] Pobrano profil, ale brak imienia/nazwiska w odpowiedzi API.")
        return profile_data
    except requests.exceptions.Timeout:
        logging.error(f"BŁĄD TIMEOUT podczas pobierania profilu FB dla {psid}")
        return None
    except requests.exceptions.HTTPError as http_err:
         logging.error(f"BŁĄD HTTP {http_err.response.status_code} podczas pobierania profilu FB dla {psid}: {http_err}")
         if http_err.response is not None:
            try: logging.error(f"Odpowiedź FB (błąd HTTP): {http_err.response.json()}")
            except json.JSONDecodeError: logging.error(f"Odpowiedź FB (błąd HTTP, nie JSON): {http_err.response.text}")
         return None
    except requests.exceptions.RequestException as req_err:
        logging.error(f"BŁĄD RequestException podczas pobierania profilu FB dla {psid}: {req_err}")
        return None
    except Exception as e:
        logging.error(f"Niespodziewany BŁĄD podczas pobierania profilu FB dla {psid}: {e}", exc_info=True)
        return None

# --- load_history (bez zmian od poprzedniej wersji) ---
def load_history(user_psid):
    """Wczytuje historię i ostatni kontekst/stan z pliku."""
    # ... (bez zmian) ...
    filepath = os.path.join(HISTORY_DIR, f"{user_psid}.json")
    history = []
    context = {}
    valid_states = [STATE_GENERAL, STATE_SCHEDULING_ACTIVE, STATE_GATHERING_INFO]
    if not os.path.exists(filepath):
        logging.info(f"[{user_psid}] Plik historii nie istnieje, zwracam stan domyślny {STATE_GENERAL}.")
        return history, {'type': STATE_GENERAL}
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            history_data = json.load(f)
            if isinstance(history_data, list):
                last_system_message_index = -1
                for i, msg_data in enumerate(reversed(history_data)):
                    if isinstance(msg_data, dict) and msg_data.get('role') == 'system' and 'type' in msg_data:
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
                    elif isinstance(msg_data, dict) and msg_data.get('role') == 'system' and 'type' in msg_data:
                        if i == last_system_message_index:
                            if msg_data.get('type') in valid_states:
                                context = msg_data
                                logging.debug(f"[{user_psid}] Odczytano AKTYWNY kontekst: {context}")
                            else:
                                logging.warning(f"[{user_psid}] Znaleziono ostatni kontekst, ale z nieprawidłowym typem: {msg_data}. Ignorowanie.")
                        else:
                            logging.debug(f"[{user_psid}] Pominięto stary kontekst (idx {i}): {msg_data}")
                    else:
                        logging.warning(f"Ostrz. [{user_psid}]: Pominięto niepoprawną wiadomość/kontekst (idx {i}): {msg_data}")
                if not context or context.get('type') not in valid_states:
                    if not context: logging.debug(f"[{user_psid}] Nie znaleziono kontekstu systemowego na końcu pliku. Ustawiam stan {STATE_GENERAL}.")
                    else: logging.warning(f"[{user_psid}] Wczytany kontekst ma nieprawidłowy typ '{context.get('type')}'. Reset do {STATE_GENERAL}.")
                    context = {'type': STATE_GENERAL}
                logging.info(f"[{user_psid}] Wczytano historię: {len(history)} wiad. Stan: {context.get('type', STATE_GENERAL)}")
                context.pop('role', None)
                return history, context
            else:
                logging.error(f"BŁĄD [{user_psid}]: Plik historii nie jest listą.")
                return [], {'type': STATE_GENERAL}
    except FileNotFoundError:
        logging.info(f"[{user_psid}] Plik historii nie istnieje.")
        return [], {'type': STATE_GENERAL}
    except (json.JSONDecodeError, KeyError, IndexError, TypeError) as e:
        logging.error(f"BŁĄD [{user_psid}] parsowania historii: {e}.")
        try:
            os.rename(filepath, f"{filepath}.error_{int(time.time())}")
            logging.warning(f"    Zmieniono nazwę uszkodzonego pliku historii.")
        except OSError as rename_err:
             logging.error(f"    Nie udało się zmienić nazwy: {rename_err}")
        return [], {'type': STATE_GENERAL}
    except Exception as e:
        logging.error(f"BŁĄD [{user_psid}] wczytywania historii: {e}", exc_info=True)
        return [], {'type': STATE_GENERAL}

# --- save_history (bez zmian od poprzedniej wersji) ---
def save_history(user_psid, history, context_to_save=None):
    """Zapisuje historię i aktualny kontekst/stan."""
    # ... (bez zmian) ...
    ensure_dir(HISTORY_DIR)
    filepath = os.path.join(HISTORY_DIR, f"{user_psid}.json")
    temp_filepath = f"{filepath}.tmp"
    history_data = []
    try:
        history_to_process = [m for m in history if isinstance(m, Content) and m.role in ('user', 'model')]
        max_messages_to_save = MAX_HISTORY_TURNS * 2
        if len(history_to_process) > max_messages_to_save:
            logging.debug(f"[{user_psid}] Ograniczanie historii do zapisu z {len(history_to_process)} do {max_messages_to_save} wiadomości.")
            history_to_process = history_to_process[-max_messages_to_save:]
        for msg in history_to_process:
             if isinstance(msg, Content) and hasattr(msg, 'role') and msg.role in ('user', 'model') and hasattr(msg, 'parts') and isinstance(msg.parts, list):
                parts_data = [{'text': part.text} for part in msg.parts if isinstance(part, Part) and hasattr(part, 'text')]
                if parts_data:
                    history_data.append({'role': msg.role, 'parts': parts_data})
             else:
                logging.warning(f"Ostrz. [{user_psid}]: Pomijanie nieprawidłowego obiektu historii podczas zapisu: {type(msg)}")
        current_state_to_save = context_to_save.get('type', STATE_GENERAL) if context_to_save else STATE_GENERAL
        if context_to_save and isinstance(context_to_save, dict) and current_state_to_save != STATE_GENERAL:
             context_copy = context_to_save.copy()
             context_copy['role'] = 'system'
             history_data.append(context_copy)
             logging.debug(f"[{user_psid}] Dodano kontekst {current_state_to_save} do zapisu: {context_copy}")
        else:
             logging.debug(f"[{user_psid}] Zapis bez kontekstu (stan general).")
        with open(temp_filepath, 'w', encoding='utf-8') as f:
            json.dump(history_data, f, ensure_ascii=False, indent=2)
        os.replace(temp_filepath, filepath)
        logging.info(f"[{user_psid}] Zapisano historię/kontekst ({len(history_data)} wpisów, stan: {current_state_to_save})")
    except Exception as e:
        logging.error(f"BŁĄD [{user_psid}] zapisu historii/kontekstu: {e}", exc_info=True)
        if os.path.exists(temp_filepath):
            try:
                os.remove(temp_filepath)
                logging.info(f"    Usunięto plik tymczasowy {temp_filepath} po błędzie zapisu.")
            except OSError as remove_e:
                logging.error(f"    Nie można usunąć pliku tymczasowego {temp_filepath} po błędzie zapisu: {remove_e}")

def _get_calendar_timezone():
    """Pobiera (i cachuje) obiekt strefy czasowej dla Kalendarza."""
    global _cal_tz
    if _cal_tz is None:
        try:
            _cal_tz = pytz.timezone(CALENDAR_TIMEZONE)
        except pytz.exceptions.UnknownTimeZoneError:
            logging.error(f"BŁĄD: Strefa kalendarza '{CALENDAR_TIMEZONE}' nieznana. Używam UTC.")
            _cal_tz = pytz.utc
    return _cal_tz

def _get_sheet_timezone():
    """Pobiera (i cachuje) obiekt strefy czasowej dla Arkusza."""
    global _sheet_tz
    if _sheet_tz is None:
        try:
            _sheet_tz = pytz.timezone(SHEET_TIMEZONE)
        except pytz.exceptions.UnknownTimeZoneError:
            logging.error(f"BŁĄD: Strefa arkusza '{SHEET_TIMEZONE}' nieznana. Używam UTC.")
            _sheet_tz = pytz.utc
    return _sheet_tz

# --- format_slot_for_user (używa teraz strefy kalendarza do wyświetlania) ---
def format_slot_for_user(slot_start):
    """Formatuje pojedynczy slot (datetime) na czytelny tekst dla użytkownika."""
    if not isinstance(slot_start, datetime.datetime):
        logging.warning(f"Błąd formatowania slotu: oczekiwano datetime, otrzymano {type(slot_start)}")
        return "[Błąd daty]"
    try:
        tz = _get_calendar_timezone() # Użyj strefy kalendarza do wyświetlania użytkownikowi
        if slot_start.tzinfo is None: slot_start = tz.localize(slot_start)
        else: slot_start = slot_start.astimezone(tz)

        try:
            day_name = slot_start.strftime('%A').capitalize()
        except Exception:
             day_name = POLISH_WEEKDAYS[slot_start.weekday()]

        hour_str = str(slot_start.hour)
        try:
            formatted_date = slot_start.strftime('%d.%m.%Y')
            formatted_time = slot_start.strftime(f'{hour_str}:%M')
            return f"{day_name}, {formatted_date} o {formatted_time}"
        except Exception as format_err:
             logging.warning(f"Błąd formatowania daty/czasu przez strftime: {format_err}. Używam formatu ISO.")
             return slot_start.strftime('%Y-%m-%d %H:%M %Z')

    except Exception as e:
        logging.error(f"Błąd formatowania slotu {slot_start}: {e}", exc_info=True)
        return slot_start.isoformat()

# =====================================================================
# === FUNKCJE GOOGLE CALENDAR (ODCZYT/WERYFIKACJA) ====================
# =====================================================================

def get_calendar_service():
    """Inicjalizuje (i cachuje) usługę Google Calendar API."""
    global _calendar_service
    if _calendar_service:
        return _calendar_service
    if not os.path.exists(SERVICE_ACCOUNT_FILE):
        logging.error(f"KRYTYCZNY BŁĄD: Brak pliku klucza usługi Google: '{SERVICE_ACCOUNT_FILE}'")
        return None
    try:
        # Użyj zakresu tylko do odczytu dla kalendarza
        creds = service_account.Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=CALENDAR_SCOPES)
        service = build('calendar', 'v3', credentials=creds, cache_discovery=False)
        logging.info("Utworzono połączenie z Google Calendar API (odczyt).")
        _calendar_service = service
        return service
    except Exception as e:
        logging.error(f"Błąd tworzenia usługi Google Calendar: {e}", exc_info=True)
        return None

def parse_event_time(event_time_data, default_tz):
    """Parsuje dane czasu wydarzenia z API Kalendarza, zwracając świadomy obiekt datetime."""
    dt_str = None
    is_date_only = False

    if not isinstance(event_time_data, dict):
        logging.warning(f"Ostrz.: parse_event_time otrzymało nieprawidłowy typ danych: {type(event_time_data)}")
        return None

    if 'dateTime' in event_time_data:
        dt_str = event_time_data['dateTime']
    elif 'date' in event_time_data:
        dt_str = event_time_data['date']
        is_date_only = True
    else:
        logging.debug(f"Brak klucza 'dateTime' lub 'date' w event_time_data: {event_time_data}")
        return None

    if not isinstance(dt_str, str):
        logging.warning(f"Ostrz.: Oczekiwano stringa czasu, otrzymano {type(dt_str)} w {event_time_data}")
        return None

    try:
        if is_date_only:
            # Handle all-day events - assume they start at the beginning of the day in the default timezone
            dt_naive = datetime.datetime.strptime(dt_str, '%Y-%m-%d')
            dt_aware = default_tz.localize(dt_naive)
            return dt_aware
        else:
            # Handle specific dateTime
            # Handle 'Z' for UTC explicitly
            if dt_str.endswith('Z'):
                dt_str = dt_str[:-1] + '+00:00'
            dt = datetime.datetime.fromisoformat(dt_str)
            # If timezone is naive, assume the default timezone
            if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
                 logging.warning(f"Ostrz.: Parsowany dateTime '{event_time_data['dateTime']}' jako naiwny. Zakładam strefę {default_tz.zone}.")
                 dt_aware = default_tz.localize(dt)
            else:
                 # Convert to the default timezone
                 dt_aware = dt.astimezone(default_tz)
            return dt_aware
    except ValueError as e:
        logging.warning(f"Ostrz.: Nie udało się sparsować czasu '{dt_str}': {e}")
        return None
    except Exception as e:
        logging.error(f"Nieoczekiwany błąd podczas parsowania czasu '{dt_str}': {e}", exc_info=True)
        return None

def get_free_time_ranges(calendar_id, start_datetime, end_datetime):
    """Pobiera listę wolnych zakresów czasowych z kalendarza, filtrując je wg 24h wyprzedzenia."""
    service = get_calendar_service()
    tz = _get_calendar_timezone()
    if not service:
        logging.error("Błąd: Usługa kalendarza niedostępna do pobrania wolnych terminów.")
        return []

    # Ensure start and end datetimes are timezone-aware
    if start_datetime.tzinfo is None: start_datetime = tz.localize(start_datetime)
    else: start_datetime = start_datetime.astimezone(tz)
    if end_datetime.tzinfo is None: end_datetime = tz.localize(end_datetime)
    else: end_datetime = end_datetime.astimezone(tz)

    now = datetime.datetime.now(tz)
    # Ensure search starts from now if start_datetime is in the past
    start_datetime = max(start_datetime, now)
    if start_datetime >= end_datetime:
        logging.info(f"Zakres wyszukiwania [{start_datetime:%Y-%m-%d %H:%M} - {end_datetime:%Y-%m-%d %H:%M}] jest nieprawidłowy lub całkowicie w przeszłości.")
        return []

    logging.info(f"Szukanie wolnych zakresów w '{calendar_id}' od {start_datetime:%Y-%m-%d %H:%M %Z} do {end_datetime:%Y-%m-%d %H:%M %Z}")
    try:
        body = {
            "timeMin": start_datetime.isoformat(),
            "timeMax": end_datetime.isoformat(),
            "timeZone": CALENDAR_TIMEZONE,
            "items": [{"id": calendar_id}]
        }
        freebusy_result = service.freebusy().query(body=body).execute()
        calendar_data = freebusy_result.get('calendars', {}).get(calendar_id, {})

        # Handle potential errors in the free/busy response
        if 'errors' in calendar_data:
             for error in calendar_data['errors']:
                 logging.error(f"Błąd API Freebusy dla kalendarza {calendar_id}: {error.get('reason')} - {error.get('message')}")
             # If calendar not found or forbidden, return empty list
             if any(e.get('reason') == 'notFound' or e.get('reason') == 'forbidden' for e in calendar_data['errors']):
                 return []
             # Otherwise, log the error but proceed assuming maybe some data is available

        busy_times_raw = calendar_data.get('busy', [])
        busy_times = []
        for busy_slot in busy_times_raw:
            start_str = busy_slot.get('start')
            end_str = busy_slot.get('end')
            # Ensure start and end are strings before parsing
            if isinstance(start_str, str) and isinstance(end_str, str):
                # Use the parser function
                busy_start = parse_event_time({'dateTime': start_str}, tz)
                busy_end = parse_event_time({'dateTime': end_str}, tz)
                if busy_start and busy_end and busy_start < busy_end:
                    # Clip busy times to the requested search range
                    busy_start_clipped = max(busy_start, start_datetime)
                    busy_end_clipped = min(busy_end, end_datetime)
                    if busy_start_clipped < busy_end_clipped:
                        busy_times.append({'start': busy_start_clipped, 'end': busy_end_clipped})
                else:
                    logging.warning(f"Ostrz.: Pominięto nieprawidłowy lub niesparsowany zajęty czas: start={start_str}, end={end_str}")
            else:
                 logging.warning(f"Ostrz.: Pominięto zajęty slot o nieoczekiwanej strukturze danych: {busy_slot}")

    except HttpError as error:
        logging.error(f'Błąd HTTP API Freebusy: {error.resp.status} {error.resp.reason}', exc_info=True)
        return []
    except Exception as e:
        logging.error(f"Nieoczekiwany błąd podczas zapytania Freebusy: {e}", exc_info=True)
        return []

    # Sort and merge overlapping/adjacent busy times
    busy_times.sort(key=lambda x: x['start'])
    merged_busy_times = []
    for busy in busy_times:
        if not merged_busy_times or busy['start'] > merged_busy_times[-1]['end']:
            merged_busy_times.append(busy)
        else:
            # Merge overlapping or adjacent intervals
            merged_busy_times[-1]['end'] = max(merged_busy_times[-1]['end'], busy['end'])

    # Calculate free ranges by inverting busy times within the search window
    free_ranges = []
    current_time = start_datetime
    for busy_slot in merged_busy_times:
        if current_time < busy_slot['start']:
            free_ranges.append({'start': current_time, 'end': busy_slot['start']})
        current_time = max(current_time, busy_slot['end'])
    # Add the final free range if any time is left until the end of the search window
    if current_time < end_datetime:
        free_ranges.append({'start': current_time, 'end': end_datetime})

    # Filter free ranges by working hours and minimum duration
    intermediate_free_slots = []
    min_duration_delta = datetime.timedelta(minutes=APPOINTMENT_DURATION_MINUTES)
    for free_range in free_ranges:
        range_start = free_range['start']
        range_end = free_range['end']

        # Iterate through days within the free range
        current_segment_start = range_start
        while current_segment_start < range_end:
            day_date = current_segment_start.date()
            # Define working hours for the current day
            work_day_start = tz.localize(datetime.datetime.combine(day_date, datetime.time(WORK_START_HOUR, 0)))
            work_day_end = tz.localize(datetime.datetime.combine(day_date, datetime.time(WORK_END_HOUR, 0)))

            # Calculate the effective free time within working hours for this day
            effective_start = max(current_segment_start, work_day_start)
            effective_end = min(range_end, work_day_end)

            # Check if the effective slot is long enough
            if effective_start < effective_end and (effective_end - effective_start) >= min_duration_delta:
                # Optional: Round start time up to the nearest 10 minutes (or other interval)
                # if effective_start.minute % 10 != 0 or effective_start.second > 0 or effective_start.microsecond > 0:
                #     minutes_to_add = 10 - (effective_start.minute % 10)
                #     rounded_start = effective_start + datetime.timedelta(minutes=minutes_to_add)
                #     rounded_start = rounded_start.replace(second=0, microsecond=0)
                # else:
                #     rounded_start = effective_start
                rounded_start = effective_start # Keep original start for now

                # Check again if rounded slot is still valid and long enough
                if rounded_start < effective_end and (effective_end - rounded_start) >= min_duration_delta:
                    intermediate_free_slots.append({'start': rounded_start, 'end': effective_end})

            # Move to the next potential start time (either end of work day or start of next day)
            # This logic ensures we check each day within the range correctly
            next_day_start = tz.localize(datetime.datetime.combine(day_date + datetime.timedelta(days=1), datetime.time(0,0)))
            current_segment_start = max(work_day_end, next_day_start) # Start checking after work hours or on the next day
            current_segment_start = max(current_segment_start, range_start) # Ensure we don't go back before the original range start

    # --- Apply the MIN_BOOKING_LEAD_HOURS filter ---
    final_filtered_slots = []
    min_start_time = now + datetime.timedelta(hours=MIN_BOOKING_LEAD_HOURS)
    logging.debug(f"Minimalny czas startu po filtrze {MIN_BOOKING_LEAD_HOURS}h: {min_start_time:%Y-%m-%d %H:%M %Z}")

    for slot in intermediate_free_slots:
        original_start = slot['start']
        original_end = slot['end']

        # If the entire slot starts after the minimum lead time, keep it if long enough
        if original_start >= min_start_time:
            if (original_end - original_start) >= min_duration_delta:
                final_filtered_slots.append(slot)
        # If the slot starts before but ends after the minimum lead time, adjust the start
        elif original_end > min_start_time:
            # Adjust start time to meet the lead time requirement
            adjusted_start = min_start_time
            # Check if the *remaining* part of the slot is long enough
            if (original_end - adjusted_start) >= min_duration_delta:
                final_filtered_slots.append({'start': adjusted_start, 'end': original_end})
                logging.debug(f"Zmodyfikowano slot {original_start:%H:%M}-{original_end:%H:%M} na {adjusted_start:%H:%M}-{original_end:%H:%M} z powodu reguły {MIN_BOOKING_LEAD_HOURS}h.")
        # Otherwise, the slot is entirely too soon, discard it

    logging.info(f"Znaleziono {len(final_filtered_slots)} wolnych zakresów po filtrze godzin pracy i {MIN_BOOKING_LEAD_HOURS}h wyprzedzenia.")
    # Log first few slots for debugging
    for i, slot in enumerate(final_filtered_slots[:5]):
         logging.debug(f"  Finalny Slot {i+1}: {slot['start']:%Y-%m-%d %H:%M %Z} - {slot['end']:%Y-%m-%d %H:%M %Z}")
    if len(final_filtered_slots) > 5:
         logging.debug("  ...")

    return final_filtered_slots

def is_slot_actually_free(start_time, calendar_id):
    """Weryfikuje w czasie rzeczywistym, czy slot jest wolny w Kalendarzu Google."""
    service = get_calendar_service()
    tz = _get_calendar_timezone()
    if not service:
        logging.error("Błąd: Usługa kalendarza niedostępna do weryfikacji slotu.")
        # Fail safe - assume it might be busy if calendar is down
        return False

    if not isinstance(start_time, datetime.datetime):
        logging.error(f"Błąd weryfikacji: start_time nie jest obiektem datetime ({type(start_time)})")
        return False # Invalid input

    # Ensure timezone awareness
    if start_time.tzinfo is None: start_time = tz.localize(start_time)
    else: start_time = start_time.astimezone(tz)
    # Calculate end time based on duration
    end_time = start_time + datetime.timedelta(minutes=APPOINTMENT_DURATION_MINUTES)

    # Prepare free/busy query for the specific slot
    body = {
        "timeMin": start_time.isoformat(),
        "timeMax": end_time.isoformat(),
        "timeZone": CALENDAR_TIMEZONE,
        "items": [{"id": calendar_id}]
    }
    try:
        logging.debug(f"Weryfikacja free/busy dla slotu: {start_time:%Y-%m-%d %H:%M} - {end_time:%Y-%m-%d %H:%M}")
        freebusy_result = service.freebusy().query(body=body).execute()
        calendar_data = freebusy_result.get('calendars', {}).get(calendar_id, {})

        # Handle errors during verification
        if 'errors' in calendar_data:
             for error in calendar_data['errors']:
                 logging.error(f"Błąd API Freebusy (weryfikacja) dla kalendarza {calendar_id}: {error.get('reason')} - {error.get('message')}")
             return False # Assume busy on error

        # Check the 'busy' list for the target calendar
        busy_times = calendar_data.get('busy', [])

        if not busy_times:
            # No busy intervals found within the slot duration
            logging.info(f"Weryfikacja: Slot {start_time:%Y-%m-%d %H:%M} JEST wolny.")
            return True
        else:
            # Found overlapping busy intervals
            logging.warning(f"Weryfikacja: Slot {start_time:%Y-%m-%d %H:%M} jest ZAJĘTY. Zwrócone zajęte sloty: {busy_times}")
            return False

    except HttpError as error:
         logging.error(f"Błąd HTTP API Freebusy podczas weryfikacji: {error.resp.status} {error.resp.reason}", exc_info=True)
         return False # Assume busy on HTTP error
    except Exception as e:
        logging.error(f"Nieoczekiwany błąd podczas weryfikacji Freebusy: {e}", exc_info=True)
        return False # Assume busy on other errors

def format_ranges_for_ai(ranges):
    """Formatuje listę zakresów czasowych na bardziej techniczny tekst dla AI."""
    if not ranges:
        return "Brak dostępnych zakresów czasowych w podanym okresie."

    tz = _get_calendar_timezone() # Use calendar timezone for consistency
    formatted_lines = [
        f"Dostępne ZAKRESY czasowe (wizyta trwa {APPOINTMENT_DURATION_MINUTES} minut). Porozmawiaj z użytkownikiem, aby znaleźć pasujący termin. Pamiętaj, że dokładny czas rozpoczęcia musi mieścić się w jednym z podanych zakresów.",
        "--- Dostępne Zakresy (Data YYYY-MM-DD, Dzień, Od Godziny HH:MM, Do Godziny HH:MM) ---"
    ]
    slots_added = 0
    max_slots_to_show = 25 # Limit the number shown to AI to avoid huge prompts
    # Sort ranges chronologically before formatting
    sorted_ranges = sorted(ranges, key=lambda r: r['start'])

    for r in sorted_ranges:
        start_dt = r['start'].astimezone(tz)
        end_dt = r['end'].astimezone(tz)
        # Get Polish day name
        try:
            day_name = start_dt.strftime('%A').capitalize()
        except Exception: # Fallback
            day_name = POLISH_WEEKDAYS[start_dt.weekday()]

        date_str = start_dt.strftime('%Y-%m-%d')
        start_time_str = start_dt.strftime('%H:%M')
        end_time_str = end_dt.strftime('%H:%M')

        # Ensure start is before end before adding
        if start_dt < end_dt:
            formatted_lines.append(f"- {date_str}, {day_name}, od {start_time_str}, do {end_time_str}")
            slots_added += 1
            if slots_added >= max_slots_to_show:
                formatted_lines.append("- ... (i potencjalnie więcej)")
                break

    if slots_added == 0: # Should not happen if initial check passed, but good failsafe
        return "Brak dostępnych zakresów czasowych w godzinach pracy w podanym okresie."

    formatted_output = "\n".join(formatted_lines)
    logging.debug(f"--- Zakresy sformatowane dla AI ({slots_added} pokazanych) ---\n{formatted_output}\n---------------------------------")
    return formatted_output

# =====================================================================
# === FUNKCJE GOOGLE SHEETS (bez zmian od poprzedniej wersji) =========
# =====================================================================

def get_sheets_service():
    """Inicjalizuje (i cachuje) usługę Google Sheets API."""
    global _sheets_service
    if _sheets_service:
        return _sheets_service
    if not os.path.exists(SERVICE_ACCOUNT_FILE):
        logging.error(f"KRYTYCZNY BŁĄD: Brak pliku klucza usługi Google: '{SERVICE_ACCOUNT_FILE}'")
        return None
    try:
        # Użyj zakresu zapisu dla arkuszy
        creds = service_account.Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=SHEET_SCOPES)
        service = build('sheets', 'v4', credentials=creds, cache_discovery=False)
        logging.info("Utworzono połączenie z Google Sheets API (zapis).")
        _sheets_service = service
        return service
    except Exception as e:
        logging.error(f"Błąd tworzenia usługi Google Sheets: {e}", exc_info=True)
        return None

def write_to_sheet(spreadsheet_id, sheet_name, data_row):
    """Zapisuje pojedynczy wiersz danych do określonego arkusza Google Sheets."""
    # ... (bez zmian od poprzedniej wersji) ...
    service = get_sheets_service()
    if not service: return False, "Błąd: Brak połączenia z usługą Google Sheets."
    if not spreadsheet_id: return False, "Błąd konfiguracji: Brak ID arkusza Google."
    if not sheet_name: return False, "Błąd konfiguracji: Brak nazwy arkusza Google."
    if not isinstance(data_row, list): return False, "Błąd wewnętrzny: Dane do zapisu nie są listą."
    try:
        range_name = f"{sheet_name}!A1"
        body = {'values': [data_row]}
        logging.info(f"Próba zapisu do arkusza '{spreadsheet_id}' -> '{sheet_name}': {data_row}")
        result = service.spreadsheets().values().append(
            spreadsheetId=spreadsheet_id, range=range_name,
            valueInputOption='USER_ENTERED', insertDataOption='INSERT_ROWS', body=body
        ).execute()
        logging.info(f"Zapisano pomyślnie {result.get('updates').get('updatedRows')} wiersz(y) w zakresie {result.get('updates').get('updatedRange')}")
        return True, None
    except HttpError as error:
        error_details = f"Kod: {error.resp.status}, Powód: {error.resp.reason}"
        logging.error(f"Błąd API Google Sheets podczas zapisu: {error}, Szczegóły: {error_details}", exc_info=True)
        if error.resp.status == 403: return False, "Problem z uprawnieniami do zapisu w arkuszu."
        elif error.resp.status == 404: return False, f"Nie znaleziono arkusza o ID '{spreadsheet_id}' lub nazwie '{sheet_name}'."
        elif error.resp.status == 400: return False, f"Błąd zapytania do arkusza (np. zły zakres/format danych)."
        else: return False, f"Nieoczekiwany problem z Google Sheets ({error_details})."
    except Exception as e:
        logging.error(f"Nieoczekiwany błąd Python podczas zapisu do arkusza: {e}", exc_info=True)
        return False, "Wewnętrzny błąd systemu podczas próby zapisu danych."

# =====================================================================
# === FUNKCJE KOMUNIKACJI FB (bez zmian od poprzedniej wersji) ========
# =====================================================================
# _send_typing_on, _send_single_message, send_message, _simulate_typing
# ... (bez zmian) ...
def _send_typing_on(recipient_id):
    if not PAGE_ACCESS_TOKEN or len(PAGE_ACCESS_TOKEN) < 50 or not ENABLE_TYPING_DELAY: return
    logging.debug(f"[{recipient_id}] Wysyłanie 'typing_on'")
    params = {"access_token": PAGE_ACCESS_TOKEN}
    payload = {"recipient": {"id": recipient_id}, "sender_action": "typing_on"}
    try: requests.post(FACEBOOK_GRAPH_API_URL, params=params, json=payload, timeout=3)
    except requests.exceptions.RequestException as e: logging.warning(f"[{recipient_id}] Błąd wysyłania 'typing_on': {e}")

def _send_single_message(recipient_id, message_text):
    logging.info(f"--- Wysyłanie fragmentu do {recipient_id} (dł: {len(message_text)}) ---")
    if not PAGE_ACCESS_TOKEN or len(PAGE_ACCESS_TOKEN) < 50:
        logging.error(f"!!! [{recipient_id}] Brak tokena dostępu strony. NIE WYSŁANO wiadomości.")
        return False
    params = {"access_token": PAGE_ACCESS_TOKEN}
    payload = {"recipient": {"id": recipient_id}, "message": {"text": message_text}, "messaging_type": "RESPONSE"}
    try:
        r = requests.post(FACEBOOK_GRAPH_API_URL, params=params, json=payload, timeout=30)
        r.raise_for_status()
        response_json = r.json()
        if response_json.get('error'):
            fb_error = response_json['error']
            logging.error(f"!!! BŁĄD FB API podczas wysyłania wiadomości: {fb_error} !!!")
            if fb_error.get('code') == 190: logging.error("!!! Wygląda na to, że FB_PAGE_ACCESS_TOKEN jest nieprawidłowy lub wygasł !!!")
            return False
        logging.debug(f"[{recipient_id}] Fragment wiadomości wysłany pomyślnie (Message ID: {response_json.get('message_id')}).")
        return True
    except requests.exceptions.Timeout: logging.error(f"!!! BŁĄD TIMEOUT podczas wysyłania wiadomości do {recipient_id} !!!"); return False
    except requests.exceptions.HTTPError as http_err:
         logging.error(f"!!! BŁĄD HTTP {http_err.response.status_code} podczas wysyłania wiadomości do {recipient_id}: {http_err} !!!")
         if http_err.response is not None:
            try: logging.error(f"Odpowiedź FB (błąd HTTP): {http_err.response.json()}")
            except json.JSONDecodeError: logging.error(f"Odpowiedź FB (błąd HTTP, nie JSON): {http_err.response.text}")
         return False
    except requests.exceptions.RequestException as req_err: logging.error(f"!!! BŁĄD RequestException podczas wysyłania wiadomości do {recipient_id}: {req_err} !!!"); return False
    except Exception as e: logging.error(f"!!! Nieoczekiwany BŁĄD podczas wysyłania wiadomości do {recipient_id}: {e} !!!", exc_info=True); return False

def send_message(recipient_id, full_message_text):
    if not full_message_text or not isinstance(full_message_text, str) or not full_message_text.strip():
        logging.warning(f"[{recipient_id}] Pominięto wysłanie pustej lub nieprawidłowej wiadomości.")
        return
    message_len = len(full_message_text)
    logging.info(f"[{recipient_id}] Przygotowanie wiadomości do wysłania (długość: {message_len}).")
    if ENABLE_TYPING_DELAY:
        est_typing_duration = min(MAX_TYPING_DELAY_SECONDS, max(MIN_TYPING_DELAY_SECONDS, message_len / TYPING_CHARS_PER_SECOND))
        logging.debug(f"[{recipient_id}] Szacowany czas pisania: {est_typing_duration:.2f}s")
        _send_typing_on(recipient_id)
        time.sleep(est_typing_duration)
    chunks = []
    if message_len <= MESSAGE_CHAR_LIMIT: chunks.append(full_message_text)
    else:
        logging.info(f"[{recipient_id}] Wiadomość za długa ({message_len} > {MESSAGE_CHAR_LIMIT}). Dzielenie na fragmenty...")
        remaining_text = full_message_text
        while remaining_text:
            if len(remaining_text) <= MESSAGE_CHAR_LIMIT: chunks.append(remaining_text.strip()); break
            split_index = -1
            delimiters = ['\n\n', '\n', '. ', '! ', '? ', ' ']
            for delimiter in delimiters:
                search_limit = MESSAGE_CHAR_LIMIT - len(delimiter) + 1
                temp_index = remaining_text.rfind(delimiter, 0, search_limit)
                if temp_index != -1: split_index = temp_index + len(delimiter); break
            if split_index == -1: split_index = MESSAGE_CHAR_LIMIT
            chunk = remaining_text[:split_index].strip()
            if chunk: chunks.append(chunk)
            remaining_text = remaining_text[split_index:].strip()
        logging.info(f"[{recipient_id}] Podzielono wiadomość na {len(chunks)} fragmentów.")
    num_chunks = len(chunks)
    send_success_count = 0
    for i, chunk in enumerate(chunks):
        logging.debug(f"[{recipient_id}] Wysyłanie fragmentu {i+1}/{num_chunks}...")
        if not _send_single_message(recipient_id, chunk):
            logging.error(f"!!! [{recipient_id}] Błąd wysyłania fragmentu {i+1}. Anulowano wysyłanie reszty.")
            break
        send_success_count += 1
        if num_chunks > 1 and i < num_chunks - 1:
            logging.debug(f"[{recipient_id}] Oczekiwanie {MESSAGE_DELAY_SECONDS}s przed kolejnym fragmentem...")
            if ENABLE_TYPING_DELAY:
                next_chunk_len = len(chunks[i+1])
                est_next_typing_duration = min(MAX_TYPING_DELAY_SECONDS * 0.7, max(MIN_TYPING_DELAY_SECONDS * 0.5, next_chunk_len / TYPING_CHARS_PER_SECOND))
                _send_typing_on(recipient_id)
                time.sleep(min(est_next_typing_duration, MESSAGE_DELAY_SECONDS * 0.8))
                remaining_delay = max(0, MESSAGE_DELAY_SECONDS - est_next_typing_duration)
                if remaining_delay > 0: time.sleep(remaining_delay)
            else: time.sleep(MESSAGE_DELAY_SECONDS)
    logging.info(f"--- [{recipient_id}] Zakończono proces wysyłania. Wysłano {send_success_count}/{num_chunks} fragmentów. ---")

def _simulate_typing(recipient_id, duration_seconds):
    if ENABLE_TYPING_DELAY and duration_seconds > 0:
        _send_typing_on(recipient_id)
        time.sleep(min(duration_seconds, MAX_TYPING_DELAY_SECONDS * 1.1))

# =====================================================================
# === FUNKCJE WYWOŁANIA AI (bez zmian od poprzedniej wersji) =========
# =====================================================================
def _call_gemini(user_psid, prompt_history, generation_config, task_name, max_retries=3):
    """Wywołuje API Gemini z obsługą błędów, logowaniem i ponowieniami."""
    # ... (bez zmian) ...
    if not gemini_model: logging.error(f"!!! [{user_psid}] KRYTYCZNY BŁĄD: Model Gemini ({task_name}) jest niedostępny (None)!"); return None
    if not isinstance(prompt_history, list) or not all(isinstance(item, Content) for item in prompt_history): logging.error(f"!!! [{user_psid}] Nieprawidłowy format promptu ({task_name})."); return None
    logging.info(f"[{user_psid}] Wywołanie Gemini: {task_name} (Prompt: {len(prompt_history)} wiadomości)")
    last_user_msg = next((msg.parts[0].text for msg in reversed(prompt_history) if msg.role == 'user' and msg.parts), None)
    if last_user_msg: logging.debug(f"    Ostatnia wiadomość usera ({task_name}): '{last_user_msg[:200]}{'...' if len(last_user_msg)>200 else ''}'")
    else: logging.debug(f"    Brak wiadomości użytkownika w bezpośrednim prompcie ({task_name}).")
    attempt = 0
    while attempt < max_retries:
        attempt += 1
        logging.debug(f"    Próba {attempt}/{max_retries} wywołania Gemini ({task_name})...")
        try:
            _simulate_typing(user_psid, MIN_TYPING_DELAY_SECONDS * 0.8)
            response = gemini_model.generate_content(prompt_history, generation_config=generation_config, safety_settings=SAFETY_SETTINGS)
            if response and response.candidates:
                candidate = response.candidates[0]
                finish_reason = candidate.finish_reason
                if finish_reason != 1:
                    safety_ratings = candidate.safety_ratings
                    logging.warning(f"[{user_psid}] Gemini ({task_name}) ZAKOŃCZONE NIEPRAWIDŁOWO! Powód: {finish_reason.name} ({finish_reason.value}). Safety Ratings: {safety_ratings}")
                    if finish_reason in [3, 4] and attempt < max_retries:
                        logging.warning(f"    Ponawianie ({attempt}/{max_retries}) z powodu blokady...")
                        time.sleep(1.5 * attempt); continue
                    else:
                        logging.error(f"!!! [{user_psid}] Gemini ({task_name}) nieudane po blokadzie lub innym błędzie.")
                        if finish_reason == 3: return "Przepraszam, nie mogę przetworzyć tej prośby ze względu na zasady bezpieczeństwa."
                        else: return "Wystąpił problem z generowaniem odpowiedzi."
                if candidate.content and candidate.content.parts:
                    generated_text = "".join(part.text for part in candidate.content.parts if hasattr(part, 'text')).strip()
                    if generated_text:
                        logging.info(f"[{user_psid}] Gemini ({task_name}) zwróciło odpowiedź (długość: {len(generated_text)}).")
                        logging.debug(f"    Pełna odpowiedź Gemini ({task_name}): '{generated_text}'")
                        return generated_text
                    else: logging.warning(f"[{user_psid}] Gemini ({task_name}) zwróciło kandydata z pustą treścią.")
                else: logging.warning(f"[{user_psid}] Gemini ({task_name}) zwróciło kandydata bez treści (content/parts).")
            else:
                prompt_feedback = response.prompt_feedback if hasattr(response, 'prompt_feedback') else 'Brak informacji zwrotnej'
                logging.error(f"!!! BŁĄD [{user_psid}] Gemini ({task_name}) - Brak kandydatów w odpowiedzi. Feedback: {prompt_feedback}.")
        except HttpError as http_err:
             status_code = http_err.resp.status if http_err.resp else 'Nieznany'
             reason = http_err.resp.reason if http_err.resp else 'Nieznany'
             logging.error(f"!!! BŁĄD HTTP ({status_code}) [{user_psid}] Gemini ({task_name}) - Próba {attempt}/{max_retries}: {reason}")
             if status_code in [429, 500, 503] and attempt < max_retries:
                 sleep_time = (2 ** attempt) + (random.random() * 0.5)
                 logging.warning(f"    Oczekiwanie {sleep_time:.2f}s przed ponowieniem...")
                 time.sleep(sleep_time); continue
             else: break
        except Exception as e:
             if isinstance(e, NameError) and 'gemini_model' in str(e): logging.critical(f"!!! KRYTYCZNY NameError [{user_psid}] w _call_gemini: {e}. gemini_model jest None!", exc_info=True); return None
             else: logging.error(f"!!! BŁĄD [{user_psid}] Gemini ({task_name}) - Nieoczekiwany błąd Python (Próba {attempt}/{max_retries}): {e}", exc_info=True)
        if attempt < max_retries:
            logging.warning(f"    Problem z odpowiedzią Gemini ({task_name}). Oczekiwanie przed ponowieniem ({attempt+1}/{max_retries})...")
            time.sleep(1.5 * attempt)
    logging.error(f"!!! KRYTYCZNY BŁĄD [{user_psid}] Gemini ({task_name}) - Nie udało się uzyskać poprawnej odpowiedzi po {max_retries} próbach.")
    return None

# =====================================================================
# === INSTRUKCJE SYSTEMOWE I GŁÓWNE FUNKCJE AI ========================
# =====================================================================

# --- ZMODYFIKOWANA INSTRUKCJA SCHEDULING (używa zakresów z kalendarza) ---
SYSTEM_INSTRUCTION_SCHEDULING = """Jesteś pomocnym asystentem AI specjalizującym się w umawianiu terminów korepetycji online. Twoim zadaniem jest znalezienie pasującego terminu dla użytkownika na podstawie jego preferencji oraz dostarczonej listy dostępnych zakresów czasowych z kalendarza.

**Kontekst:**
*   Rozmawiasz z użytkownikiem, który wyraził chęć umówienia się na lekcję.
*   Poniżej znajduje się lista AKTUALNIE dostępnych ZAKRESÓW czasowych z kalendarza, w których można umówić wizytę (każda trwa {duration} minut). **Wszystkie podane zakresy są już odpowiednio odsunięte w czasie (filtr {min_lead_hours}h) i gotowe do zaproponowania.**
*   Masz dostęp do historii poprzedniej rozmowy.

**Styl pisania:**
*   Używaj zwrotów typu "Państwo".
*   Unikaj zbyt entuzjastycznych wiadomości i wykrzykników.
*   Zwracaj uwagę na ortografię i interpunkcję.
*   Proponuj terminy w formie pytania, np. "Czy odpowiadałby Państwu termin w najbliższy wtorek o 17:00?".

**Dostępne zakresy czasowe z kalendarza:**
{available_ranges_text}

**Twoje zadanie:**
1.  **Zaproponuj pierwszy termin:** Rozpocznij od zaproponowania **konkretnego terminu** z podanej listy "Dostępne zakresy czasowe". Wybierz termin w miarę możliwości w najbliższych dniach, biorąc pod uwagę potencjalne preferencje ucznia (np. popołudnia w tygodniu).
2.  **Negocjuj:** Na podstawie odpowiedzi użytkownika, historii konwersacji i **wyłącznie dostępnych zakresów z listy**, kontynuuj rozmowę, aby znaleźć termin pasujący obu stronom. Proponuj konkretne godziny rozpoczęcia (np. "Może w takim razie czwartek o 16:00?").
3.  **Potwierdź i dodaj znacznik:** Kiedy wspólnie ustalicie **dokładny termin** (np. "Środa, 15 maja o 18:30"), który **znajduje się na liście dostępnych zakresów**, potwierdź go w swojej odpowiedzi (np. "Świetnie, w takim razie proponowany termin to środa, 15 maja o 18:30.") i **zakończ swoją odpowiedź potwierdzającą DOKŁADNIE znacznikiem** `{slot_marker_prefix}YYYY-MM-DDTHH:MM:SS{slot_marker_suffix}`. Użyj formatu ISO 8601 dla ustalonego czasu rozpoczęcia (np. 2024-05-15T18:30:00). Upewnij się, że data i godzina w znaczniku są poprawne, zgodne z ustaleniami i **pochodzą z listy dostępnych zakresów**.
4.  **NIE dodawaj znacznika**, jeśli:
    *   Użytkownik jeszcze się zastanawia lub prosi o więcej opcji.
    *   Użytkownik proponuje termin, którego nie ma na liście dostępnych zakresów.
    *   Nie udało się znaleźć pasującego terminu.
    *   Lista dostępnych zakresów jest pusta.
5.  **Brak terminów:** Jeśli lista zakresów jest pusta lub po rozmowie okaże się, że żaden termin nie pasuje, poinformuj o tym użytkownika uprzejmie. Nie dodawaj znacznika.

**Pamiętaj:**
*   Trzymaj się **wyłącznie** terminów i godzin wynikających z "Dostępnych zakresów czasowych".
*   Bądź elastyczny w rozmowie, ale propozycje muszą pochodzić z listy.
*   Używaj języka polskiego i polskiej strefy czasowej ({calendar_timezone}).
*   Znacznik `{slot_marker_prefix}...{slot_marker_suffix}` jest sygnałem dla systemu, że **osiągnięto porozumienie co do terminu z dostępnej listy**. Używaj go tylko w tym jednym, konkretnym przypadku.
""".format(
    duration=APPOINTMENT_DURATION_MINUTES,
    min_lead_hours=MIN_BOOKING_LEAD_HOURS,
    available_ranges_text="{available_ranges_text}", # Placeholder zostanie wypełniony
    calendar_timezone=CALENDAR_TIMEZONE,
    slot_marker_prefix=SLOT_ISO_MARKER_PREFIX,
    slot_marker_suffix=SLOT_ISO_MARKER_SUFFIX
)
# --- KONIEC ZMODYFIKOWANEJ INSTRUKCJI SCHEDULING ---

# --- SYSTEM_INSTRUCTION_GENERAL (bez zmian od poprzedniej wersji) ---
SYSTEM_INSTRUCTION_GENERAL = """Jesteś przyjaznym i pomocnym asystentem klienta w 'Zakręcone Korepetycje'. Prowadzisz rozmowę na czacie dotyczącą korepetycji online.
# ... (bez zmian) ...
**Twoje główne zadania:**
1.  Odpowiadaj rzeczowo i uprzejmie na pytania użytkownika dotyczące oferty, metodyki, dostępności korepetycji.
2.  Utrzymuj konwersacyjny, pomocny ton. Odpowiadaj po polsku.
3.  **Nie podawaj samodzielnie informacji o cenach ani dokładnych metodach płatności.** Jeśli użytkownik o to zapyta, możesz odpowiedzieć ogólnie, np. "Szczegóły dotyczące płatności omawiamy indywidualnie." lub "Informacje o cenach prześlemy po ustaleniu terminu.".
4.  **Kluczowy cel:** Jeśli w wypowiedzi użytkownika **wyraźnie pojawi się intencja umówienia się na lekcję** (próbną lub zwykłą), rezerwacji terminu, zapytanie o wolne terminy lub chęć rozpoczęcia współpracy, **dodaj na samym końcu swojej odpowiedzi specjalny znacznik:** `{intent_marker}`.
# ... (reszta bez zmian) ...
""".format(intent_marker=INTENT_SCHEDULE_MARKER)


# --- SYSTEM_INSTRUCTION_GATHERING (bez zmian od poprzedniej wersji - już uwzględniała pobrane imię rodzica) ---
SYSTEM_INSTRUCTION_GATHERING = """Twoim zadaniem jest zebranie dodatkowych informacji o UCZNIU potrzebnych do zapisu na korepetycje, po tym jak wstępnie ustalono termin.
# ... (bez zmian od poprzedniej wersji) ...
**Kontekst:**
*   Wstępnie ustalony termin lekcji to: {proposed_slot_formatted}
*   Rozmawiasz prawdopodobnie z rodzicem/opiekunem. Jego/Jej imię i nazwisko z profilu to: {known_parent_first_name} {known_parent_last_name}. (Jeśli te pola są puste, API nie zwróciło danych).
*   Masz dostęp do historii rozmowy.
*   Informacje o UCZNIU już znane (mogą być puste):
    *   Imię ucznia: {known_student_first_name}
    *   Nazwisko ucznia: {known_student_last_name}
    *   Klasa/Szkoła: {known_grade}
    *   Poziom (dla liceum/technikum): {known_level}
# ... (reszta bez zmian) ...
5.  **Zakończ po zebraniu danych:** Kiedy uznasz, że masz już **wszystkie wymagane informacje** (Imię/Nazwisko Ucznia, Klasa, ewentualnie Poziom dla szkół średnich, oraz imię/nazwisko rodzica - jeśli było pobrane z API lub zapytane), Twoja ostatnia odpowiedź **MUSI** zawierać **DOKŁADNIE** następujący tekst (bez żadnych dodatków przed lub po, oprócz znacznika na końcu):
    "Dziękuję za wszystkie informacje. Dane zostały zapisane. Wkrótce skontaktujemy się w celu potwierdzenia szczegółów. Proszę również oczekiwać na wiadomość dotyczącą płatności i dostępu do materiałów na profilu dedykowanym do komunikacji: https://www.facebook.com/profile.php?id=61576135251276"
    Na samym końcu tej wiadomości dodaj **DOKŁADNIE** znacznik: `{info_gathered_marker}`.
# ... (reszta bez zmian) ...
""".format(
    proposed_slot_formatted="{proposed_slot_formatted}",
    known_parent_first_name="{known_parent_first_name}",
    known_parent_last_name="{known_parent_last_name}",
    known_student_first_name="{known_student_first_name}",
    known_student_last_name="{known_student_last_name}",
    known_grade="{known_grade}",
    known_level="{known_level}",
    info_gathered_marker=INFO_GATHERED_MARKER
)


# --- Funkcja AI: Planowanie terminu (przyjmuje zakresy) ---
def get_gemini_scheduling_response(user_psid, history_for_scheduling_ai, current_user_message_text, available_ranges):
    """Prowadzi rozmowę planującą z AI, używając dostępnych zakresów, zwraca odpowiedź AI."""
    if not gemini_model:
        logging.error(f"!!! [{user_psid}] Model Gemini niezaładowany (Scheduling)!")
        return "Przepraszam, mam problem z systemem planowania."

    # Formatuj zakresy dla AI
    ranges_text = format_ranges_for_ai(available_ranges)
    try:
        # Wypełnij placeholder w instrukcji
        system_instruction = SYSTEM_INSTRUCTION_SCHEDULING.format(
            available_ranges_text=ranges_text
        )
    except KeyError as e:
         logging.error(f"!!! BŁĄD formatowania instrukcji AI (Scheduling): Brak klucza {e}")
         return "Błąd konfiguracji asystenta planowania."
    except Exception as format_e:
         logging.error(f"!!! BŁĄD nieoczekiwany formatowania instrukcji AI (Scheduling): {format_e}")
         return "Błąd wewnętrzny konfiguracji asystenta planowania."


    initial_prompt = [
        Content(role="user", parts=[Part.from_text(system_instruction)]),
        Content(role="model", parts=[Part.from_text(f"Rozumiem. Zaproponuję pierwszy dostępny termin z podanej listy i będę negocjować z użytkownikiem na podstawie dostępnych zakresów. Znacznik {SLOT_ISO_MARKER_PREFIX}...{SLOT_ISO_MARKER_SUFFIX} dodam tylko po uzyskaniu ostatecznej zgody na termin z listy.")])
    ]
    full_prompt = initial_prompt + history_for_scheduling_ai
    if current_user_message_text:
        full_prompt.append(Content(role="user", parts=[Part.from_text(current_user_message_text)]))

    max_prompt_messages = (MAX_HISTORY_TURNS * 2) + 2
    while len(full_prompt) > max_prompt_messages:
        full_prompt.pop(2)
        if len(full_prompt) > 2:
            full_prompt.pop(2)

    # Użyj konfiguracji dla planowania
    response_text = _call_gemini(user_psid, full_prompt, GENERATION_CONFIG_SCHEDULING, "Scheduling Conversation")

    if response_text:
        if INTENT_SCHEDULE_MARKER in response_text:
             logging.warning(f"[{user_psid}] AI (Scheduling) błędnie dodało znacznik {INTENT_SCHEDULE_MARKER}. Usuwam.")
             response_text = response_text.replace(INTENT_SCHEDULE_MARKER, "").strip()
        if INFO_GATHERED_MARKER in response_text:
             logging.warning(f"[{user_psid}] AI (Scheduling) błędnie dodało znacznik {INFO_GATHERED_MARKER}. Usuwam.")
             response_text = response_text.replace(INFO_GATHERED_MARKER, "").strip()
        return response_text
    else:
        logging.error(f"!!! [{user_psid}] Nie uzyskano odpowiedzi Gemini (Scheduling).")
        return "Przepraszam, wystąpił błąd podczas sprawdzania terminów. Spróbujmy ponownie za chwilę."

# --- get_gemini_gathering_response (bez zmian od poprzedniej wersji) ---
def get_gemini_gathering_response(user_psid, history_for_gathering_ai, current_user_message_text, context_info):
    """Prowadzi rozmowę zbierającą informacje o uczniu."""
    # ... (bez zmian) ...
    if not gemini_model: logging.error(f"!!! [{user_psid}] Model Gemini niezaładowany (Gathering Info)!"); return "Przepraszam, mam problem z systemem."
    proposed_slot_str = context_info.get("proposed_slot_formatted", "nie ustalono")
    parent_first_name = context_info.get("known_parent_first_name", "")
    parent_last_name = context_info.get("known_parent_last_name", "")
    student_first_name = context_info.get("known_student_first_name", "")
    student_last_name = context_info.get("known_student_last_name", "")
    grade = context_info.get("known_grade", "")
    level = context_info.get("known_level", "")
    try:
        system_instruction = SYSTEM_INSTRUCTION_GATHERING.format(
            proposed_slot_formatted=proposed_slot_str,
            known_parent_first_name=parent_first_name, known_parent_last_name=parent_last_name,
            known_student_first_name=student_first_name, known_student_last_name=student_last_name,
            known_grade=grade, known_level=level
        )
    except KeyError as e: logging.error(f"!!! BŁĄD formatowania instrukcji AI (Gathering): Brak klucza {e}"); return "Błąd konfiguracji asystenta zbierania informacji."
    initial_prompt = [
        Content(role="user", parts=[Part.from_text(system_instruction)]),
        Content(role="model", parts=[Part.from_text(f"Rozumiem. Sprawdzę znane informacje (w tym imię rodzica z profilu) i zapytam o brakujące dane UCZNIA (Imię/Nazwisko, Klasa, Poziom dla liceum/technikum). Po zebraniu kompletu informacji dodam znacznik {INFO_GATHERED_MARKER}.")])
    ]
    full_prompt = initial_prompt + history_for_gathering_ai
    if current_user_message_text: full_prompt.append(Content(role="user", parts=[Part.from_text(current_user_message_text)]))
    max_prompt_messages = (MAX_HISTORY_TURNS * 2) + 2
    while len(full_prompt) > max_prompt_messages: full_prompt.pop(2); full_prompt.pop(2)
    response_text = _call_gemini(user_psid, full_prompt, GENERATION_CONFIG_GATHERING, "Info Gathering")
    if response_text:
        if INTENT_SCHEDULE_MARKER in response_text: response_text = response_text.replace(INTENT_SCHEDULE_MARKER, "").strip()
        if SLOT_ISO_MARKER_PREFIX in response_text: response_text = re.sub(rf"{re.escape(SLOT_ISO_MARKER_PREFIX)}.*?{re.escape(SLOT_ISO_MARKER_SUFFIX)}", "", response_text).strip()
        return response_text
    else: logging.error(f"!!! [{user_psid}] Nie uzyskano odpowiedzi Gemini (Gathering Info)."); return "Przepraszam, wystąpił błąd systemowy."

# --- get_gemini_general_response (bez zmian od poprzedniej wersji) ---
def get_gemini_general_response(user_psid, current_user_message_text, history_for_general_ai):
    """Prowadzi ogólną rozmowę z AI."""
    # ... (bez zmian) ...
    if not gemini_model: logging.error(f"!!! [{user_psid}] Model Gemini niezaładowany (General)!"); return "Przepraszam, mam chwilowy problem z systemem."
    initial_prompt = [
        Content(role="user", parts=[Part.from_text(SYSTEM_INSTRUCTION_GENERAL)]),
        Content(role="model", parts=[Part.from_text(f"Rozumiem. Będę pomocnym asystentem klienta i dodam znacznik {INTENT_SCHEDULE_MARKER}, gdy użytkownik wyrazi chęć umówienia się.")])
    ]
    full_prompt = initial_prompt + history_for_general_ai
    if current_user_message_text: full_prompt.append(Content(role="user", parts=[Part.from_text(current_user_message_text)]))
    max_prompt_messages = (MAX_HISTORY_TURNS * 2) + 2
    while len(full_prompt) > max_prompt_messages: full_prompt.pop(2); full_prompt.pop(2)
    response_text = _call_gemini(user_psid, full_prompt, GENERATION_CONFIG_DEFAULT, "General Conversation")
    if response_text:
        if SLOT_ISO_MARKER_PREFIX in response_text: response_text = re.sub(rf"{re.escape(SLOT_ISO_MARKER_PREFIX)}.*?{re.escape(SLOT_ISO_MARKER_SUFFIX)}", "", response_text).strip()
        if INFO_GATHERED_MARKER in response_text: response_text = response_text.replace(INFO_GATHERED_MARKER, "").strip()
        return response_text
    else: logging.error(f"!!! [{user_psid}] Nie uzyskano odpowiedzi Gemini (General)."); return "Przepraszam, wystąpił błąd przetwarzania Twojej wiadomości."

# =====================================================================
# === WEBHOOK HANDLERS ================================================
# =====================================================================

@app.route('/webhook', methods=['GET'])
def webhook_verification():
    # ... (bez zmian) ...
    logging.info("--- GET /webhook (Weryfikacja) ---")
    hub_mode = request.args.get('hub.mode')
    hub_token = request.args.get('hub.verify_token')
    hub_challenge = request.args.get('hub.challenge')
    logging.debug(f"Otrzymano GET: Mode={hub_mode}, Token={hub_token}, Challenge={hub_challenge}")
    if hub_mode == 'subscribe' and hub_token == VERIFY_TOKEN:
        logging.info("Weryfikacja GET zakończona pomyślnie!")
        return Response(hub_challenge, status=200)
    else:
        logging.warning(f"Weryfikacja GET NIEUDANA. Oczekiwany token: '{VERIFY_TOKEN}', Otrzymany: '{hub_token}'")
        return Response("Verification failed", status=403)


@app.route('/webhook', methods=['POST'])
def webhook_handle():
    """Główny handler dla przychodzących zdarzeń z Messengera."""
    logging.info(f"\n{'='*30} {datetime.datetime.now(_get_calendar_timezone()):%Y-%m-%d %H:%M:%S %Z} POST /webhook {'='*30}") # Użyj strefy kalendarza w logach
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
                        logging.warning("Pominięto zdarzenie bez identyfikatora nadawcy (sender.id).")
                        continue

                    logging.info(f"--- Przetwarzanie zdarzenia dla PSID: {sender_id} ---")

                    history, context = load_history(sender_id)
                    history_for_gemini = [h for h in history if isinstance(h, Content) and h.role in ('user', 'model')]
                    current_state = context.get('type', STATE_GENERAL)
                    logging.info(f"    Aktualny stan konwersacji: {current_state}")
                    logging.debug(f"    Aktualny kontekst przed przetworzeniem: {context}")

                    action = None
                    msg_result = None
                    next_state = current_state
                    model_resp_content = None
                    user_content = None
                    context_data_to_save = context.copy()
                    trigger_gathering_ai_immediately = False
                    slot_verification_failed = False # Flaga do obsługi zajętego slotu

                    # === Obsługa wiadomości tekstowych ===
                    if message_data := event.get("message"):
                        if message_data.get("is_echo"): logging.debug(f"    Pominięto echo wiadomości bota."); continue

                        user_input_text = message_data.get("text", "").strip()
                        if user_input_text:
                            user_content = Content(role="user", parts=[Part.from_text(user_input_text)])
                            logging.info(f"    Otrzymano wiadomość tekstową (stan={current_state}): '{user_input_text[:100]}{'...' if len(user_input_text)>100 else ''}'")
                            if ENABLE_TYPING_DELAY: time.sleep(MIN_TYPING_DELAY_SECONDS * 0.5)

                            if current_state == STATE_SCHEDULING_ACTIVE:
                                action = 'handle_scheduling'
                            elif current_state == STATE_GATHERING_INFO:
                                action = 'handle_gathering'
                                # --- Heurystyka aktualizacji kontekstu (bez zmian) ---
                                try:
                                    if not context_data_to_save.get('known_student_first_name') and not context_data_to_save.get('known_student_last_name'):
                                        name_match = re.findall(r'\b[A-ZĄĆĘŁŃÓŚŹŻ][a-ząćęłńóśźż]+\s+[A-ZĄĆĘŁŃÓŚŹŻ][a-ząćęłńóśźż]+\b', user_input_text)
                                        if name_match:
                                            potential_name = name_match[0].split()
                                            context_data_to_save['known_student_first_name'] = potential_name[0]
                                            context_data_to_save['known_student_last_name'] = potential_name[1]
                                            logging.debug(f"      Heurystyka: Potencjalne imię studenta z inputu: {potential_name[0]} {potential_name[1]}")
                                    if not context_data_to_save.get('known_grade'):
                                        grade_match = re.search(r'(?:klasa\s+(\d+)|(\d+)\s+klasa|(\d+)\s*(?:lo|liceum|tech|technikum))', user_input_text, re.IGNORECASE)
                                        if grade_match:
                                            num = grade_match.group(1) or grade_match.group(2) or grade_match.group(3)
                                            context_data_to_save['known_grade'] = f"{num} klasa"
                                            logging.debug(f"      Heurystyka: Potencjalna klasa z inputu: {context_data_to_save['known_grade']}")
                                    if not context_data_to_save.get('known_level') and ('liceum' in context_data_to_save.get('known_grade','').lower() or 'technikum' in context_data_to_save.get('known_grade','').lower()):
                                         if re.search(r'\b(rozszerzon[ya]|rozszerzenie)\b', user_input_text, re.IGNORECASE): context_data_to_save['known_level'] = 'Rozszerzony'; logging.debug(f"      Heurystyka: Potencjalny poziom z inputu: Rozszerzony")
                                         elif re.search(r'\b(podstawow[ya]|podstawa)\b', user_input_text, re.IGNORECASE): context_data_to_save['known_level'] = 'Podstawowy'; logging.debug(f"      Heurystyka: Potencjalny poziom z inputu: Podstawowy")
                                except Exception as parse_ex: logging.warning(f"      Błąd podczas heurystycznego parsowania inputu w stanie GATHERING: {parse_ex}")
                            else: # Stan GENERAL
                                action = 'handle_general'

                        elif attachments := message_data.get("attachments"):
                             att_type = attachments[0].get('type','nieznany')
                             logging.info(f"      Otrzymano załącznik typu: {att_type}.")
                             user_content = Content(role="user", parts=[Part.from_text(f"[Użytkownik wysłał załącznik typu: {att_type}]")])
                             msg_result = "Dziękuję, ale obecnie mogę przetwarzać tylko wiadomości tekstowe." if att_type not in ['sticker', 'image', 'audio', 'video', 'file'] else "Dzięki!"
                             action = 'send_info'
                             next_state = current_state
                        else: logging.info("      Otrzymano pustą wiadomość lub nieobsługiwany typ."); action = None

                    # === Obsługa Postback ===
                    elif postback := event.get("postback"):
                        payload = postback.get("payload")
                        title = postback.get("title", "")
                        logging.info(f"    Otrzymano postback: Payload='{payload}', Tytuł='{title}', Stan={current_state}")
                        user_input_text = f"Użytkownik kliknął przycisk: '{title}' (Payload: {payload})"
                        user_content = Content(role="user", parts=[Part.from_text(user_input_text)])
                        if payload == "CANCEL_SCHEDULING":
                             msg_result = "Rozumiem, anulowano proces umawiania terminu. W czymś jeszcze mogę pomóc?"
                             action = 'send_info'; next_state = STATE_GENERAL; context_data_to_save = {}
                        elif current_state == STATE_SCHEDULING_ACTIVE: action = 'handle_scheduling'
                        elif current_state == STATE_GATHERING_INFO: action = 'handle_gathering'
                        else: action = 'handle_general'

                    # === Inne zdarzenia ===
                    elif event.get("read"): logging.debug(f"    Otrzymano potwierdzenie odczytania."); continue
                    elif event.get("delivery"): logging.debug(f"    Otrzymano potwierdzenie dostarczenia."); continue
                    else: logging.warning(f"    Otrzymano nieobsługiwany typ zdarzenia: {json.dumps(event)}"); continue


                    # --- Pętla przetwarzania akcji ---
                    loop_guard = 0
                    while action and loop_guard < 3:
                        loop_guard += 1
                        logging.debug(f"  >> Pętla akcji {loop_guard}/3 | Akcja: {action} | Stan wejściowy: {current_state} | Kontekst wej.: {context_data_to_save}")
                        current_action = action
                        action = None # Reset

                        if current_action == 'handle_general':
                            logging.debug("  >> Wykonanie: handle_general")
                            # ... (bez zmian od poprzedniej wersji) ...
                            if user_content and user_content.parts:
                                response = get_gemini_general_response(sender_id, user_content.parts[0].text, history_for_gemini)
                                if response:
                                    if INTENT_SCHEDULE_MARKER in response:
                                        logging.info(f"      AI Ogólne wykryło intencję [{INTENT_SCHEDULE_MARKER}]. Przejście do planowania.")
                                        initial_resp_text = response.split(INTENT_SCHEDULE_MARKER, 1)[0].strip()
                                        if initial_resp_text:
                                            send_message(sender_id, initial_resp_text)
                                            model_resp_content = Content(role="model", parts=[Part.from_text(initial_resp_text)])
                                            history_for_gemini.append(user_content); history_for_gemini.append(model_resp_content)
                                        else: history_for_gemini.append(user_content)
                                        user_content = None; model_resp_content = None
                                        next_state = STATE_SCHEDULING_ACTIVE; action = 'handle_scheduling'; context_data_to_save = {}
                                        logging.debug("      Przekierowanie do handle_scheduling...")
                                        continue
                                    else:
                                        msg_result = response; model_resp_content = Content(role="model", parts=[Part.from_text(msg_result)])
                                        next_state = STATE_GENERAL; context_data_to_save = {}
                                else:
                                    msg_result = "Przepraszam, mam problem z przetworzeniem Twojej wiadomości."
                                    model_resp_content = Content(role="model", parts=[Part.from_text(msg_result)])
                                    next_state = STATE_GENERAL; context_data_to_save = {}
                            else: logging.warning("handle_general wywołane bez user_content.")

                        elif current_action == 'handle_scheduling':
                            logging.debug("  >> Wykonanie: handle_scheduling")
                            try:
                                # --- Odczyt z Kalendarza ---
                                tz = _get_calendar_timezone()
                                now = datetime.datetime.now(tz)
                                search_start = now
                                search_end_date = (search_start + datetime.timedelta(days=MAX_SEARCH_DAYS)).date()
                                search_end = tz.localize(datetime.datetime.combine(search_end_date, datetime.time(WORK_END_HOUR, 0)))
                                logging.info(f"      Pobieranie wolnych zakresów (z filtrem {MIN_BOOKING_LEAD_HOURS}h) od {search_start:%Y-%m-%d %H:%M} do {search_end:%Y-%m-%d %H:%M}")
                                _simulate_typing(sender_id, MAX_TYPING_DELAY_SECONDS * 0.6) # Symulacja podczas szukania
                                free_ranges = get_free_time_ranges(TARGET_CALENDAR_ID, search_start, search_end)
                                # ---------------------------

                                if free_ranges:
                                    logging.info(f"      Znaleziono {len(free_ranges)} zakresów. Wywołanie AI Planującego...")
                                    current_input_text = user_content.parts[0].text if user_content and user_content.parts else None
                                    # Przekaż zakresy do AI
                                    ai_response_text = get_gemini_scheduling_response(
                                        sender_id, history_for_gemini, current_input_text, free_ranges
                                    )

                                    if ai_response_text:
                                        iso_match = re.search(rf"{re.escape(SLOT_ISO_MARKER_PREFIX)}(.*?){re.escape(SLOT_ISO_MARKER_SUFFIX)}", ai_response_text)
                                        if iso_match:
                                            extracted_iso = iso_match.group(1).strip()
                                            logging.info(f"      AI Planujące zwróciło potencjalny finalny slot: {extracted_iso}")
                                            text_for_user = re.sub(rf"{re.escape(SLOT_ISO_MARKER_PREFIX)}.*?{re.escape(SLOT_ISO_MARKER_SUFFIX)}", "", ai_response_text).strip()
                                            text_for_user = re.sub(r'\s+', ' ', text_for_user).strip()

                                            try:
                                                proposed_start = datetime.datetime.fromisoformat(extracted_iso)
                                                if proposed_start.tzinfo is None: proposed_start = tz.localize(proposed_start)
                                                else: proposed_start = proposed_start.astimezone(tz)
                                                proposed_slot_formatted = format_slot_for_user(proposed_start)

                                                # --- WERYFIKACJA SLOTU W KALENDARZU ---
                                                logging.info(f"      Weryfikacja dostępności slotu w kalendarzu: {proposed_slot_formatted}")
                                                _simulate_typing(sender_id, MIN_TYPING_DELAY_SECONDS) # Symulacja podczas weryfikacji
                                                if is_slot_actually_free(proposed_start, TARGET_CALENDAR_ID):
                                                    logging.info("      Weryfikacja OK! Slot jest wolny. Przejście do zbierania danych.")
                                                    # --- Pobierz profil rodzica ---
                                                    parent_profile = get_user_profile(sender_id)
                                                    parent_first_name_api = parent_profile.get('first_name', '') if parent_profile else ''
                                                    parent_last_name_api = parent_profile.get('last_name', '') if parent_profile else ''
                                                    # -----------------------------

                                                    confirm_msg = text_for_user if text_for_user else f"Dobrze, potwierdzam termin {proposed_slot_formatted}."
                                                    confirm_msg += " Teraz poproszę o kilka dodatkowych informacji dotyczących ucznia."
                                                    send_message(sender_id, confirm_msg)

                                                    if user_content: history_for_gemini.append(user_content)
                                                    model_resp_content_confirm = Content(role="model", parts=[Part.from_text(confirm_msg)])
                                                    history_for_gemini.append(model_resp_content_confirm)
                                                    user_content = None; model_resp_content = None

                                                    context_data_to_save = {
                                                        'proposed_slot_iso': proposed_start.isoformat(),
                                                        'proposed_slot_formatted': proposed_slot_formatted,
                                                        'known_parent_first_name': parent_first_name_api,
                                                        'known_parent_last_name': parent_last_name_api,
                                                        'known_student_first_name': '', 'known_student_last_name': '',
                                                        'known_grade': '', 'known_level': ''
                                                    }
                                                    next_state = STATE_GATHERING_INFO
                                                    action = 'handle_gathering'
                                                    trigger_gathering_ai_immediately = True
                                                    logging.debug(f"      Ustawiono stan '{next_state}', akcję '{action}', trigger={trigger_gathering_ai_immediately}. Kontekst: {context_data_to_save}")
                                                    continue # Przejdź do pętli z akcją 'handle_gathering'
                                                else:
                                                    # --- Slot ZAJĘTY podczas weryfikacji ---
                                                    logging.warning(f"      Weryfikacja KALENDARZA NIEUDANA! Slot {extracted_iso} ({proposed_slot_formatted}) został zajęty.")
                                                    fail_msg = f"Ojej, wygląda na to, że termin {proposed_slot_formatted} został właśnie zajęty! Przepraszam za zamieszanie. Spróbujmy znaleźć inny."
                                                    # Dodaj informację dla AI, że musi wybrać inny termin z listy
                                                    fail_msg_for_ai = f"\n[SYSTEM: Termin {proposed_slot_formatted} okazał się zajęty. Zaproponuj inny termin z dostępnej listy.]"
                                                    msg_result = fail_msg
                                                    # Dodajemy wiadomość użytkownika (jeśli była) i wiadomość o błędzie do historii
                                                    if user_content: history_for_gemini.append(user_content)
                                                    model_resp_content = Content(role="model", parts=[Part.from_text(fail_msg + fail_msg_for_ai)]) # Dodaj info dla AI
                                                    user_content = None # Już dodane
                                                    next_state = STATE_SCHEDULING_ACTIVE # Pozostań w planowaniu
                                                    slot_verification_failed = True # Zapisz historię mimo braku zmiany stanu
                                                    context_data_to_save = {} # Wyczyść kontekst, bo termin nieudany
                                                    # Pętla zakończy się, wyśle wiadomość i zapisze historię

                                            except ValueError:
                                                logging.error(f"!!! BŁĄD: AI zwróciło nieprawidłowy format ISO w znaczniku: '{extracted_iso}'")
                                                msg_result = "Przepraszam, wystąpił błąd techniczny przy przetwarzaniu zaproponowanego terminu. Spróbujmy jeszcze raz."
                                                model_resp_content = Content(role="model", parts=[Part.from_text(msg_result)])
                                                next_state = STATE_SCHEDULING_ACTIVE; context_data_to_save = {}
                                            except Exception as verif_err:
                                                 logging.error(f"!!! BŁĄD podczas weryfikacji slotu {extracted_iso}: {verif_err}", exc_info=True)
                                                 msg_result = "Przepraszam, wystąpił nieoczekiwany błąd podczas sprawdzania dostępności terminu."
                                                 model_resp_content = Content(role="model", parts=[Part.from_text(msg_result)])
                                                 next_state = STATE_SCHEDULING_ACTIVE; context_data_to_save = {}
                                        else:
                                            # AI kontynuuje rozmowę planującą
                                            logging.info("      AI Planujące kontynuuje rozmowę (brak znacznika ISO).")
                                            msg_result = ai_response_text
                                            model_resp_content = Content(role="model", parts=[Part.from_text(msg_result)])
                                            next_state = STATE_SCHEDULING_ACTIVE
                                    else:
                                        logging.error("!!! BŁĄD: AI Planujące nie zwróciło odpowiedzi.")
                                        msg_result = "Przepraszam, mam problem z systemem planowania. Spróbuj ponownie za chwilę."
                                        model_resp_content = Content(role="model", parts=[Part.from_text(msg_result)])
                                        next_state = STATE_GENERAL; context_data_to_save = {}
                                else:
                                    # Brak wolnych zakresów
                                    logging.warning(f"      Brak wolnych zakresów spełniających kryteria (w tym {MIN_BOOKING_LEAD_HOURS}h wyprzedzenia).")
                                    no_slots_msg = f"Niestety, wygląda na to, że nie mam żadnych wolnych terminów w ciągu najbliższych {MAX_SEARCH_DAYS} dni, które można zarezerwować z odpowiednim wyprzedzeniem ({MIN_BOOKING_LEAD_HOURS}h). Spróbuj ponownie później lub skontaktuj się z nami w inny sposób."
                                    msg_result = no_slots_msg
                                    model_resp_content = Content(role="model", parts=[Part.from_text(no_slots_msg)])
                                    next_state = STATE_GENERAL; context_data_to_save = {}
                            except Exception as schedule_err:
                                logging.error(f"!!! KRYTYCZNY BŁĄD w bloku 'handle_scheduling': {schedule_err}", exc_info=True)
                                msg_result = "Wystąpił nieoczekiwany błąd systemu podczas planowania. Przepraszam za problem."
                                model_resp_content = Content(role="model", parts=[Part.from_text(msg_result)])
                                next_state = STATE_GENERAL; context_data_to_save = {}

                        elif current_action == 'handle_gathering':
                            logging.debug("  >> Wykonanie: handle_gathering")
                            # ... (Logika zbierania i zapisu do Sheets - bez zmian od poprzedniej wersji) ...
                            try:
                                known_info_for_ai = context_data_to_save.copy()
                                logging.debug(f"    Kontekst przekazywany do AI (Gathering): {known_info_for_ai}")
                                current_input_text = user_content.parts[0].text if user_content and user_content.parts else None
                                if trigger_gathering_ai_immediately:
                                     logging.info("      Pierwsze wywołanie AI zbierającego (po ustaleniu terminu).")
                                     current_input_text = None; trigger_gathering_ai_immediately = False
                                ai_response_text = get_gemini_gathering_response(sender_id, history_for_gemini, current_input_text, known_info_for_ai)
                                if ai_response_text:
                                    if INFO_GATHERED_MARKER in ai_response_text:
                                        logging.info(f"      AI Zbierające zasygnalizowało koniec [{INFO_GATHERED_MARKER}]. Próba zapisu do arkusza.")
                                        final_gathering_msg = ai_response_text.split(INFO_GATHERED_MARKER, 1)[0].strip()
                                        if not final_gathering_msg: final_gathering_msg = "Dziękuję za informacje. Dane zapisane."
                                        try:
                                            proposed_iso = context_data_to_save.get('proposed_slot_iso')
                                            dt_object = None; lesson_date_str = "Brak danych"; lesson_time_str = "Brak danych"
                                            if proposed_iso:
                                                try:
                                                    dt_object = datetime.datetime.fromisoformat(proposed_iso)
                                                    tz_sheet = _get_sheet_timezone() # Użyj strefy arkusza do zapisu
                                                    if dt_object.tzinfo is None: dt_object = tz_sheet.localize(dt_object)
                                                    else: dt_object = dt_object.astimezone(tz_sheet)
                                                    lesson_date_str = dt_object.strftime('%Y-%m-%d')
                                                    lesson_time_str = dt_object.strftime('%H:%M')
                                                except Exception as dt_err: logging.error(f"Błąd formatowania daty {proposed_iso} do arkusza: {dt_err}")
                                            data_to_write = [
                                                lesson_date_str, lesson_time_str,
                                                context_data_to_save.get('known_student_first_name', 'Brak'), context_data_to_save.get('known_student_last_name', 'Brak'),
                                                context_data_to_save.get('known_parent_first_name', 'Brak (API?)'), context_data_to_save.get('known_parent_last_name', 'Brak (API?)'),
                                                context_data_to_save.get('known_grade', 'Brak'), context_data_to_save.get('known_level', 'Brak')
                                            ]
                                            write_ok, write_error_msg = write_to_sheet(SPREADSHEET_ID, SHEET_NAME, data_to_write)
                                            if write_ok:
                                                logging.info("      Zapis do Google Sheet zakończony sukcesem.")
                                                msg_result = final_gathering_msg
                                                model_resp_content = Content(role="model", parts=[Part.from_text(final_gathering_msg)])
                                                next_state = STATE_GENERAL; context_data_to_save = {}
                                            else:
                                                logging.error(f"!!! BŁĄD zapisu do Google Sheet: {write_error_msg}")
                                                error_msg_user = f"Przepraszam, wystąpił problem podczas zapisywania danych ({write_error_msg}). Czy możemy spróbować ponownie uzupełnić informacje?"
                                                msg_result = error_msg_user
                                                model_resp_content = Content(role="model", parts=[Part.from_text(error_msg_user)])
                                                next_state = STATE_GATHERING_INFO # Pozostań by spróbować ponownie
                                        except Exception as sheet_write_err:
                                            logging.error(f"!!! KRYTYCZNY BŁĄD podczas przygotowania/zapisu do arkusza: {sheet_write_err}", exc_info=True)
                                            msg_result = "Wystąpił krytyczny błąd podczas zapisywania danych. Proszę skontaktować się z nami bezpośrednio."
                                            model_resp_content = Content(role="model", parts=[Part.from_text(msg_result)])
                                            next_state = STATE_GENERAL; context_data_to_save = {}
                                    else:
                                        logging.info("      AI Zbierające kontynuuje rozmowę.")
                                        msg_result = ai_response_text
                                        model_resp_content = Content(role="model", parts=[Part.from_text(msg_result)])
                                        next_state = STATE_GATHERING_INFO
                                else:
                                    logging.error("!!! BŁĄD: AI Zbierające nie zwróciło odpowiedzi.")
                                    msg_result = "Przepraszam, wystąpił błąd systemowy. Spróbuj odpowiedzieć jeszcze raz."
                                    model_resp_content = Content(role="model", parts=[Part.from_text(msg_result)])
                                    next_state = STATE_GATHERING_INFO
                            except Exception as gather_err:
                                logging.error(f"!!! KRYTYCZNY BŁĄD w bloku 'handle_gathering': {gather_err}", exc_info=True)
                                msg_result = "Wystąpił nieoczekiwany błąd systemu podczas zbierania informacji. Przepraszam za problem."
                                model_resp_content = Content(role="model", parts=[Part.from_text(msg_result)])
                                next_state = STATE_GENERAL; context_data_to_save = {}

                        elif current_action == 'send_info':
                             logging.debug("  >> Wykonanie: send_info")
                             # ... (bez zmian) ...
                             if msg_result: model_resp_content = Content(role="model", parts=[Part.from_text(msg_result)])
                             else: logging.warning("Akcja 'send_info' bez wiadomości do wysłania.")

                        else:
                             logging.warning(f"   Nieznana lub nieobsługiwana akcja '{current_action}'. Zakończenie pętli.")
                             break


                    # --- WYSYŁANIE ODPOWIEDZI I ZAPIS STANU ---
                    final_context_to_save_dict = {'type': next_state, **context_data_to_save}
                    final_context_to_save_dict.pop('role', None)

                    if msg_result:
                        send_message(sender_id, msg_result)
                        if not model_resp_content:
                             logging.warning(f"Wiadomość '{msg_result[:50]}...' została wysłana, ale nie ustawiono model_resp_content! Tworzenie domyślnego.")
                             model_resp_content = Content(role="model", parts=[Part.from_text(msg_result)])
                    elif current_action:
                        logging.debug(f"    Akcja '{current_action}' zakończona bez wiadomości do wysłania użytkownikowi (może być OK).")

                    # Zapisz jeśli była wiadomość LUB zmienił się kontekst LUB weryfikacja slotu się nie udała
                    should_save = bool(user_content) or bool(model_resp_content) or (context != final_context_to_save_dict) or slot_verification_failed

                    if should_save:
                        history_to_save = list(history_for_gemini)
                        if user_content: history_to_save.append(user_content)
                        if model_resp_content: history_to_save.append(model_resp_content)

                        max_hist_len = MAX_HISTORY_TURNS * 2
                        if len(history_to_save) > max_hist_len:
                             history_to_save = history_to_save[-max_hist_len:]

                        logging.info(f"Zapisywanie historii ({len(history_to_save)} wiad.). Nowy stan: {final_context_to_save_dict.get('type')}")
                        logging.debug(f"   Kontekst do zapisu: {final_context_to_save_dict}")
                        save_history(sender_id, history_to_save, context_to_save=final_context_to_save_dict)
                    else:
                        logging.debug("    Brak zmian w historii lub stanie - pomijanie zapisu.")

            logging.info(f"--- Zakończono przetwarzanie batcha zdarzeń ---")
            return Response("EVENT_RECEIVED", status=200)
        else:
            logging.warning(f"Otrzymano POST, ale obiekt nie jest 'page' (typ: {data.get('object') if data else 'Brak danych'}). Ignorowanie.")
            return Response("OK", status=200)
    except json.JSONDecodeError as e:
        logging.error(f"!!! BŁĄD podczas dekodowania JSON z danych POST: {e}", exc_info=True)
        logging.error(f"    Pierwsze 500 znaków surowych danych: {raw_data[:500]}...")
        return Response("Invalid JSON payload", status=400)
    except Exception as e:
        logging.critical(f"!!! KRYTYCZNY NIEOCZEKIWANY BŁĄD w głównym handlerze POST /webhook: {e}", exc_info=True)
        return Response("Internal Server Error during processing", status=200)


# =====================================================================
# === URUCHOMIENIE SERWERA ============================================
# =====================================================================
if __name__ == '__main__':
    ensure_dir(HISTORY_DIR)
    log_level = logging.DEBUG # Ustaw na INFO w produkcji
    if not logging.getLogger().hasHandlers():
        logging.basicConfig(
            level=log_level,
            format='%(asctime)s - %(levelname)s - [%(filename)s:%(lineno)d - %(funcName)s] - %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )

    logging.getLogger('googleapiclient.discovery_cache').setLevel(logging.ERROR)
    logging.getLogger('urllib3.connectionpool').setLevel(logging.WARNING)
    logging.getLogger('werkzeug').setLevel(logging.WARNING)

    print("\n" + "="*60 + "\n--- START KONFIGURACJI BOTA (Odczyt Kalendarza + Weryfikacja + Info + Zapis Sheets + Parent API) ---")
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
    if not gemini_model: print("!!! OSTRZEŻENIE: Model Gemini AI NIE załadowany poprawnie! Funkcjonalność AI niedostępna. !!!")
    else: print(f"    Model Gemini AI ({MODEL_ID}): Załadowany (OK)")
    print("-" * 60)
    print("  Konfiguracja Google Calendar (Odczyt/Weryfikacja):") # Zaktualizowany nagłówek
    print(f"    ID Kalendarza (odczyt): {TARGET_CALENDAR_ID}")
    print(f"    Strefa czasowa kalendarza: {CALENDAR_TIMEZONE} (Obiekt TZ: {_get_calendar_timezone()})")
    print(f"    Czas trwania wizyty (do obliczeń): {APPOINTMENT_DURATION_MINUTES} min")
    print(f"    Godziny pracy (filtr): {WORK_START_HOUR}:00 - {WORK_END_HOUR}:00")
    print(f"    Min. wyprzedzenie (filtr): {MIN_BOOKING_LEAD_HOURS} godz.")
    print(f"    Maks. zakres szukania: {MAX_SEARCH_DAYS} dni")
    print(f"    Plik klucza API: {SERVICE_ACCOUNT_FILE} ({'Znaleziono' if os.path.exists(SERVICE_ACCOUNT_FILE) else 'BRAK!!!'})")
    cal_service = get_calendar_service() # Inicjalizacja usługi kalendarza
    if not cal_service and os.path.exists(SERVICE_ACCOUNT_FILE): print("!!! OSTRZ.: Usługa Google Calendar NIE zainicjowana mimo obecności pliku klucza.")
    elif not os.path.exists(SERVICE_ACCOUNT_FILE): print("!!! OSTRZ.: Brak pliku klucza Google.")
    elif cal_service: print("    Usługa Google Calendar (odczyt): Zainicjowana (OK)")
    print("-" * 60)
    print("  Konfiguracja Google Sheets (Zapis):") # Zaktualizowany nagłówek
    print(f"    ID Arkusza (zapis): {SPREADSHEET_ID}")
    print(f"    Nazwa Arkusza (zapis): {SHEET_NAME}")
    print(f"    Strefa czasowa arkusza: {SHEET_TIMEZONE} (Obiekt TZ: {_get_sheet_timezone()})")
    # Plik klucza jest ten sam, ale sprawdzamy inicjalizację usługi Sheets
    sheets_service = get_sheets_service() # Inicjalizacja usługi arkuszy
    if not sheets_service and os.path.exists(SERVICE_ACCOUNT_FILE): print("!!! OSTRZ.: Usługa Google Sheets NIE zainicjowana mimo obecności pliku klucza (sprawdź uprawnienia API/klucza!).")
    elif not os.path.exists(SERVICE_ACCOUNT_FILE): print("!!! OSTRZ.: Brak pliku klucza Google.")
    elif sheets_service: print("    Usługa Google Sheets (zapis): Zainicjowana (OK)")
    print("--- KONIEC KONFIGURACJI BOTA ---"); print("="*60 + "\n")

    port = int(os.environ.get("PORT", 8080))
    run_flask_in_debug = (log_level == logging.DEBUG)

    print(f"Uruchamianie serwera Flask na porcie {port}...")
    if not run_flask_in_debug:
        try:
            from waitress import serve
            print(">>> Serwer produkcyjny Waitress START <<<")
            serve(app, host='0.0.0.0', port=port, threads=8)
        except ImportError:
            print("!!! Ostrzeżenie: 'waitress' nie znaleziono. Uruchamianie wbudowanego serwera deweloperskiego Flask (niezalecane w produkcji).")
            print(">>> Serwer deweloperski Flask START <<<")
            app.run(host='0.0.0.0', port=port, debug=False)
    else:
        print(">>> Serwer deweloperski Flask (Tryb DEBUG) START <<<")
        app.run(host='0.0.0.0', port=port, debug=True, use_reloader=False)
