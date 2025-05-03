# -*- coding: utf-8 -*-

# verify_server.py (Wersja: AI-Driven State + Two-Phase Sheet Write - EKSPERYMENTALNA - Poprawka JSON + PEP8)

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

# --- Importy Google Sheets (ZAPIS + ODCZYT) ---
# (Biblioteki już zaimportowane powyżej)
# --------------------------------------

app = Flask(__name__)

# --- Konfiguracja Ogólna ---
VERIFY_TOKEN = os.environ.get("FB_VERIFY_TOKEN", "KOLAGEN")
# Zaktualizowany domyślny token dostępu strony
PAGE_ACCESS_TOKEN = os.environ.get("FB_PAGE_ACCESS_TOKEN", "EACNAHFzEhkUBO5sicIUMoIwuZCZC1ZAduL8gb5sZAjWX2oErT4esklQALmstq2bkZAnWq3CVNF0IO3gZB44ip3XCXG40revvmpFKOLlC9jBStCNAwbIXZBWfawg0z0YH6GLGZCE1gFfgEF5A6DEIKbu5FYZB6XKXHECTeW6PNZAUQrPiKxrPCjbz7QFiBtGROvZCPR4rAZDZD")
PROJECT_ID = os.environ.get("GCP_PROJECT_ID", "linear-booth-450221-k1")
LOCATION = os.environ.get("GCP_LOCATION", "us-central1")
MODEL_ID = os.environ.get("VERTEX_MODEL_ID", "gemini-2.0-flash-001") # Użyjmy szybszego modelu dla testów

FACEBOOK_GRAPH_API_URL = f"https://graph.facebook.com/v19.0/me/messages"

HISTORY_DIR = "conversation_store"
MAX_HISTORY_TURNS = 10 # Zmniejszmy historię dla tak złożonego promptu
MESSAGE_CHAR_LIMIT = 1990
MESSAGE_DELAY_SECONDS = 1.2

ENABLE_TYPING_DELAY = True
MIN_TYPING_DELAY_SECONDS = 0.7
MAX_TYPING_DELAY_SECONDS = 3.0
TYPING_CHARS_PER_SECOND = 35

# --- Konfiguracja Kalendarza (ODCZYT/WERYFIKACJA) ---
CALENDAR_SERVICE_ACCOUNT_FILE = 'kalendarzklucz.json'
CALENDAR_SCOPES = ['https://www.googleapis.com/auth/calendar.readonly']
CALENDAR_TIMEZONE = 'Europe/Warsaw'
APPOINTMENT_DURATION_MINUTES = 60
WORK_START_HOUR = 7
WORK_END_HOUR = 22
TARGET_CALENDAR_ID = 'f19e189826b9d6e36950da347ac84d5501ecbd6bed0d76c8641be61a67749c67@group.calendar.google.com'
MAX_SEARCH_DAYS = 14
MIN_BOOKING_LEAD_HOURS = 24

# --- Konfiguracja Google Sheets (ZAPIS + ODCZYT) ---
SHEETS_SERVICE_ACCOUNT_FILE = 'arkuszklucz.json'
SHEET_SCOPES = ['https://www.googleapis.com/auth/spreadsheets'] # Pełny dostęp
SPREADSHEET_ID = os.environ.get("GOOGLE_SHEET_ID", "1vpsIAEkqtY3ZJ5Mr67Dda45aZ55V1O-Ux9ODjwk13qw")
SHEET_NAME = os.environ.get("GOOGLE_SHEET_NAME", 'Arkusz1')
SHEET_TIMEZONE = 'Europe/Warsaw'
# Definicja kolumn (zaczynając od 1)
SHEET_PSID_COLUMN_INDEX = 1      # A
SHEET_PARENT_FN_COLUMN_INDEX = 2 # B
SHEET_PARENT_LN_COLUMN_INDEX = 3 # C
SHEET_STUDENT_FN_COLUMN_INDEX = 4# D
SHEET_STUDENT_LN_COLUMN_INDEX = 5# E
SHEET_DATE_COLUMN_INDEX = 6      # F
SHEET_TIME_COLUMN_INDEX = 7      # G
SHEET_GRADE_COLUMN_INDEX = 8     # H
SHEET_SCHOOL_TYPE_COLUMN_INDEX = 9 # I
SHEET_LEVEL_COLUMN_INDEX = 10    # J
# Zakres do odczytu przy szukaniu wiersza (PSID, Data, Czas)
SHEET_READ_RANGE_FOR_UPDATE = f"{SHEET_NAME}!A2:G" # Od A2 do G do końca

# --- Znaczniki Akcji AI ---
ACTION_CHECK_AVAILABILITY = "[ACTION: CHECK_AVAILABILITY]"
ACTION_VERIFY_SLOT = "[ACTION: VERIFY_SLOT_CALENDAR_SHEET:" # + ISO + ]
ACTION_WRITE_PHASE1 = "[ACTION: WRITE_SHEET_PHASE1:"      # + ISO + ]
ACTION_GATHER_INFO = "[ACTION: GATHER_STUDENT_INFO]"
ACTION_FINALIZE = "[ACTION: FINALIZE_AND_UPDATE_SHEET:" # + Imię, Nazwisko, KlasaInfo, Poziom + ]"

# --- Ustawienia Modelu Gemini ---
# Użyjemy jednej konfiguracji dla uproszczenia, ale można by dostosować
GENERATION_CONFIG_UNIFIED = GenerationConfig(
    temperature=0.6, top_p=0.95, top_k=40, max_output_tokens=1024, # Dłuższe odpowiedzi mogą być potrzebne
)

# --- Bezpieczeństwo AI ---
SAFETY_SETTINGS = [
    SafetySetting(category=HarmCategory.HARM_CATEGORY_HARASSMENT, threshold=HarmBlockThreshold.BLOCK_ONLY_HIGH),
    SafetySetting(category=HarmCategory.HARM_CATEGORY_HATE_SPEECH, threshold=HarmBlockThreshold.BLOCK_MEDIUM_AND_ABOVE),
    SafetySetting(category=HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT, threshold=HarmBlockThreshold.BLOCK_ONLY_HIGH),
    SafetySetting(category=HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT, threshold=HarmBlockThreshold.BLOCK_MEDIUM_AND_ABOVE),
]

# --- Inicjalizacja Zmiennych Globalnych ---
_calendar_service = None
_sheets_service = None
_cal_tz = None
_sheet_tz = None
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
# === INICJALIZACJA AI ================================================
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
# === FUNKCJE POMOCNICZE (Ogólne) =====================================
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
            try:
                logging.error(f"Odpowiedź FB (błąd HTTP): {http_err.response.json()}")
            except json.JSONDecodeError:
                logging.error(f"Odpowiedź FB (błąd HTTP, nie JSON): {http_err.response.text}")
         return None
    except requests.exceptions.RequestException as req_err:
        logging.error(f"BŁĄD RequestException podczas pobierania profilu FB dla {psid}: {req_err}")
        return None
    except Exception as e:
        logging.error(f"Niespodziewany BŁĄD podczas pobierania profilu FB dla {psid}: {e}", exc_info=True)
        return None

def load_history(user_psid):
    """Wczytuje historię i ostatni kontekst/stan z pliku."""
    filepath = os.path.join(HISTORY_DIR, f"{user_psid}.json")
    history = []
    context = {}
    # W tym modelu nie ma już stanów, ale kontekst może przechowywać tymczasowe dane
    if not os.path.exists(filepath):
        logging.info(f"[{user_psid}] Plik historii nie istnieje.")
        return history, context
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            history_data = json.load(f)
            if isinstance(history_data, list):
                last_system_message_index = -1
                # Szukamy ostatniego kontekstu (nadal może być przydatny do przechowywania danych)
                for i, msg_data in enumerate(reversed(history_data)):
                    if isinstance(msg_data, dict) and msg_data.get('role') == 'system': # Nie sprawdzamy już 'type'
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
                            context = msg_data # Wczytaj ostatni kontekst systemowy
                            logging.debug(f"[{user_psid}] Odczytano ostatni kontekst systemowy: {context}")
                        else:
                            logging.debug(f"[{user_psid}] Pominięto stary kontekst systemowy (idx {i}): {msg_data}")
                    else:
                        logging.warning(f"Ostrz. [{user_psid}]: Pominięto niepoprawną wiadomość/kontekst (idx {i}): {msg_data}")
                logging.info(f"[{user_psid}] Wczytano historię: {len(history)} wiad.")
                context.pop('role', None) # Usuń rolę z kontekstu
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
            logging.debug(f"[{user_psid}] Ograniczanie historii do zapisu z {len(history_to_process)} do {max_messages_to_save} wiadomości.")
            history_to_process = history_to_process[-max_messages_to_save:]
        for msg in history_to_process:
             if isinstance(msg, Content) and hasattr(msg, 'role') and msg.role in ('user', 'model') and hasattr(msg, 'parts') and isinstance(msg.parts, list):
                parts_data = [{'text': part.text} for part in msg.parts if isinstance(part, Part) and hasattr(part, 'text')]
                if parts_data:
                    history_data.append({'role': msg.role, 'parts': parts_data})
             else:
                logging.warning(f"Ostrz. [{user_psid}]: Pomijanie nieprawidłowego obiektu historii podczas zapisu: {type(msg)}")
        # Zapisujemy kontekst tylko jeśli nie jest pusty
        if context_to_save and isinstance(context_to_save, dict):
             context_copy = context_to_save.copy()
             context_copy['role'] = 'system' # Dodaj rolę systemową do zapisu
             history_data.append(context_copy)
             logging.debug(f"[{user_psid}] Dodano kontekst do zapisu: {context_copy}")
        else:
             logging.debug(f"[{user_psid}] Zapis bez kontekstu.")
        with open(temp_filepath, 'w', encoding='utf-8') as f:
            json.dump(history_data, f, ensure_ascii=False, indent=2)
        os.replace(temp_filepath, filepath)
        logging.info(f"[{user_psid}] Zapisano historię/kontekst ({len(history_data)} wpisów)")
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

def format_slot_for_user(slot_start):
    """Formatuje pojedynczy slot (datetime) na czytelny tekst dla użytkownika."""
    if not isinstance(slot_start, datetime.datetime):
        logging.warning(f"Błąd formatowania slotu: oczekiwano datetime, otrzymano {type(slot_start)}")
        return "[Błąd daty]"
    try:
        tz = _get_calendar_timezone() # Użyj strefy kalendarza do wyświetlania użytkownikowi
        if slot_start.tzinfo is None:
            slot_start = tz.localize(slot_start)
        else:
            slot_start = slot_start.astimezone(tz)
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

def extract_school_type(grade_string):
    """Próbuje wyodrębnić typ szkoły i opis klasy z ciągu."""
    if not grade_string or not isinstance(grade_string, str):
        return "Nieokreślona", "Nieokreślona" # Zwróć krotkę

    grade_lower = grade_string.lower().strip()
    class_desc = grade_string # Domyślnie cała informacja to opis klasy
    school_type = "Nieokreślona" # Domyślny typ szkoły

    # Szukaj słów kluczowych dla typu szkoły
    type_mapping = {
        "Liceum": [r'liceum', r' lo ', r'\blo\b'],
        "Technikum": [r'technikum', r' tech ', r'\btech\b'],
        "Szkoła Podstawowa": [r'podstaw', r' sp ', r'\bsp\b'],
        "Szkoła Branżowa/Zawodowa": [r'zawodowa', r'branżowa', r'zasadnicza']
    }

    found_type = False
    for type_name, patterns in type_mapping.items():
        for pattern in patterns:
            if re.search(pattern, grade_lower):
                school_type = type_name
                # Usuń znaleziony wzorzec (i ewentualne otaczające spacje) z opisu klasy
                # Używamy \b dla słów, ale spacje dla ' lo ', ' tech ', ' sp '
                cleaned_desc = re.sub(r'\s*' + pattern.replace(r'\b', '') + r'\b?\s*', ' ', grade_string, flags=re.IGNORECASE).strip()
                # Sprawdź, czy coś zostało po czyszczeniu
                if cleaned_desc:
                    class_desc = cleaned_desc
                found_type = True
                break # Znaleziono typ, przejdź do następnego
        if found_type:
            break

    # Jeśli typ szkoły nadal nieokreślony, ale jest numer klasy
    if school_type == "Nieokreślona" and re.search(r'\d', grade_lower): # Szukaj cyfry
         school_type = "Inna (z numerem klasy)"

    # Dodatkowe czyszczenie opisu klasy (np. usunięcie słowa "klasa")
    class_desc = re.sub(r'\bklasa\b', '', class_desc, flags=re.IGNORECASE).strip()
    # Jeśli opis klasy jest pusty po czyszczeniu, wróć do oryginalnego stringu
    if not class_desc:
        class_desc = grade_string.strip()

    # Zwróć krotkę: (opis klasy, typ szkoły)
    return class_desc, school_type

# =====================================================================
# === FUNKCJE GOOGLE CALENDAR (ODCZYT/WERYFIKACJA) ====================
# =====================================================================

def get_calendar_service():
    """Inicjalizuje (i cachuje) usługę Google Calendar API używając dedykowanego klucza."""
    global _calendar_service
    if _calendar_service:
        return _calendar_service
    # Użyj dedykowanego pliku klucza dla Kalendarza
    if not os.path.exists(CALENDAR_SERVICE_ACCOUNT_FILE):
        logging.error(f"KRYTYCZNY BŁĄD: Brak pliku klucza usługi Google Calendar: '{CALENDAR_SERVICE_ACCOUNT_FILE}'")
        return None
    try:
        # Użyj dedykowanego pliku klucza i zakresów dla Kalendarza
        creds = service_account.Credentials.from_service_account_file(CALENDAR_SERVICE_ACCOUNT_FILE, scopes=CALENDAR_SCOPES)
        service = build('calendar', 'v3', credentials=creds, cache_discovery=False)
        logging.info(f"Utworzono połączenie z Google Calendar API (odczyt) używając '{CALENDAR_SERVICE_ACCOUNT_FILE}'.")
        _calendar_service = service
        return service
    except Exception as e:
        logging.error(f"Błąd tworzenia usługi Google Calendar używając '{CALENDAR_SERVICE_ACCOUNT_FILE}': {e}", exc_info=True)
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
            dt_naive = datetime.datetime.strptime(dt_str, '%Y-%m-%d')
            dt_aware = default_tz.localize(dt_naive)
            return dt_aware
        else:
            if dt_str.endswith('Z'):
                dt_str = dt_str[:-1] + '+00:00'
            dt = datetime.datetime.fromisoformat(dt_str)
            if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
                logging.warning(f"Ostrz.: Parsowany dateTime '{event_time_data['dateTime']}' jako naiwny. Zakładam strefę {default_tz.zone}.")
                dt_aware = default_tz.localize(dt)
            else:
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
    if start_datetime.tzinfo is None:
        start_datetime = tz.localize(start_datetime)
    else:
        start_datetime = start_datetime.astimezone(tz)
    if end_datetime.tzinfo is None:
        end_datetime = tz.localize(end_datetime)
    else:
        end_datetime = end_datetime.astimezone(tz)
    now = datetime.datetime.now(tz)
    start_datetime = max(start_datetime, now)
    if start_datetime >= end_datetime:
        logging.info(f"Zakres wyszukiwania [{start_datetime:%Y-%m-%d %H:%M} - {end_datetime:%Y-%m-%d %H:%M}] jest nieprawidłowy lub całkowicie w przeszłości.")
        return []
    logging.info(f"Szukanie wolnych zakresów w '{calendar_id}' od {start_datetime:%Y-%m-%d %H:%M %Z} do {end_datetime:%Y-%m-%d %H:%M %Z}")
    try:
        body = {"timeMin": start_datetime.isoformat(), "timeMax": end_datetime.isoformat(), "timeZone": CALENDAR_TIMEZONE, "items": [{"id": calendar_id}]}
        freebusy_result = service.freebusy().query(body=body).execute()
        calendar_data = freebusy_result.get('calendars', {}).get(calendar_id, {})
        if 'errors' in calendar_data:
             for error in calendar_data['errors']:
                 logging.error(f"Błąd API Freebusy dla kalendarza {calendar_id}: {error.get('reason')} - {error.get('message')}")
             if any(e.get('reason') == 'notFound' or e.get('reason') == 'forbidden' for e in calendar_data['errors']):
                 return []
        busy_times_raw = calendar_data.get('busy', [])
        busy_times = []
        for busy_slot in busy_times_raw:
            start_str = busy_slot.get('start')
            end_str = busy_slot.get('end')
            if isinstance(start_str, str) and isinstance(end_str, str):
                busy_start = parse_event_time({'dateTime': start_str}, tz)
                busy_end = parse_event_time({'dateTime': end_str}, tz)
                if busy_start and busy_end and busy_start < busy_end:
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
    intermediate_free_slots = []
    min_duration_delta = datetime.timedelta(minutes=APPOINTMENT_DURATION_MINUTES)
    for free_range in free_ranges:
        range_start = free_range['start']
        range_end = free_range['end']
        current_segment_start = range_start
        while current_segment_start < range_end:
            day_date = current_segment_start.date()
            work_day_start = tz.localize(datetime.datetime.combine(day_date, datetime.time(WORK_START_HOUR, 0)))
            work_day_end = tz.localize(datetime.datetime.combine(day_date, datetime.time(WORK_END_HOUR, 0)))
            effective_start = max(current_segment_start, work_day_start)
            effective_end = min(range_end, work_day_end)
            if effective_start < effective_end and (effective_end - effective_start) >= min_duration_delta:
                rounded_start = effective_start
                if rounded_start < effective_end and (effective_end - rounded_start) >= min_duration_delta:
                    intermediate_free_slots.append({'start': rounded_start, 'end': effective_end})
            next_day_start = tz.localize(datetime.datetime.combine(day_date + datetime.timedelta(days=1), datetime.time(0,0)))
            current_segment_start = max(work_day_end, next_day_start)
            current_segment_start = max(current_segment_start, range_start)
    final_filtered_slots = []
    min_start_time = now + datetime.timedelta(hours=MIN_BOOKING_LEAD_HOURS)
    logging.debug(f"Minimalny czas startu po filtrze {MIN_BOOKING_LEAD_HOURS}h: {min_start_time:%Y-%m-%d %H:%M %Z}")
    for slot in intermediate_free_slots:
        original_start = slot['start']
        original_end = slot['end']
        if original_start >= min_start_time:
            if (original_end - original_start) >= min_duration_delta:
                final_filtered_slots.append(slot)
        elif original_end > min_start_time:
            adjusted_start = min_start_time
            if (original_end - adjusted_start) >= min_duration_delta:
                final_filtered_slots.append({'start': adjusted_start, 'end': original_end})
                logging.debug(f"Zmodyfikowano slot {original_start:%H:%M}-{original_end:%H:%M} na {adjusted_start:%H:%M}-{original_end:%H:%M} z powodu reguły {MIN_BOOKING_LEAD_HOURS}h.")
    logging.info(f"Znaleziono {len(final_filtered_slots)} wolnych zakresów po filtrze godzin pracy i {MIN_BOOKING_LEAD_HOURS}h wyprzedzenia.")
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
        return False
    if not isinstance(start_time, datetime.datetime):
        logging.error(f"Błąd weryfikacji: start_time nie jest obiektem datetime ({type(start_time)})")
        return False
    if start_time.tzinfo is None:
        start_time = tz.localize(start_time)
    else:
        start_time = start_time.astimezone(tz)
    end_time = start_time + datetime.timedelta(minutes=APPOINTMENT_DURATION_MINUTES)
    body = {"timeMin": start_time.isoformat(), "timeMax": end_time.isoformat(), "timeZone": CALENDAR_TIMEZONE, "items": [{"id": calendar_id}]}
    try:
        logging.debug(f"Weryfikacja free/busy dla slotu: {start_time:%Y-%m-%d %H:%M} - {end_time:%Y-%m-%d %H:%M}")
        freebusy_result = service.freebusy().query(body=body).execute()
        calendar_data = freebusy_result.get('calendars', {}).get(calendar_id, {})
        if 'errors' in calendar_data:
             for error in calendar_data['errors']:
                 logging.error(f"Błąd API Freebusy (weryfikacja) dla kalendarza {calendar_id}: {error.get('reason')} - {error.get('message')}")
             return False
        busy_times = calendar_data.get('busy', [])
        if not busy_times:
            logging.info(f"Weryfikacja: Slot {start_time:%Y-%m-%d %H:%M} JEST wolny.")
            return True
        else:
            logging.warning(f"Weryfikacja: Slot {start_time:%Y-%m-%d %H:%M} jest ZAJĘTY. Zwrócone zajęte sloty: {busy_times}")
            return False
    except HttpError as error:
        logging.error(f"Błąd HTTP API Freebusy podczas weryfikacji: {error.resp.status} {error.resp.reason}", exc_info=True)
        return False
    except Exception as e:
        logging.error(f"Nieoczekiwany błąd podczas weryfikacji Freebusy: {e}", exc_info=True)
        return False

def format_ranges_for_ai(ranges):
    """Formatuje listę zakresów czasowych na bardziej techniczny tekst dla AI."""
    if not ranges:
        return "Brak dostępnych zakresów czasowych w podanym okresie."
    tz = _get_calendar_timezone()
    formatted_lines = [
        f"Dostępne ZAKRESY czasowe (wizyta trwa {APPOINTMENT_DURATION_MINUTES} minut). Porozmawiaj z użytkownikiem, aby znaleźć pasujący termin. Pamiętaj, że dokładny czas rozpoczęcia musi mieścić się w jednym z podanych zakresów.",
        "--- Dostępne Zakresy (Data YYYY-MM-DD, Dzień, Od Godziny HH:MM, Do Godziny HH:MM) ---"
    ]
    slots_added = 0
    max_slots_to_show = 25
    sorted_ranges = sorted(ranges, key=lambda r: r['start'])
    for r in sorted_ranges:
        start_dt = r['start'].astimezone(tz)
        end_dt = r['end'].astimezone(tz)
        try:
            day_name = start_dt.strftime('%A').capitalize()
        except Exception:
            day_name = POLISH_WEEKDAYS[start_dt.weekday()]
        date_str = start_dt.strftime('%Y-%m-%d')
        start_time_str = start_dt.strftime('%H:%M')
        end_time_str = end_dt.strftime('%H:%M')
        if start_dt < end_dt:
            formatted_lines.append(f"- {date_str}, {day_name}, od {start_time_str}, do {end_time_str}")
            slots_added += 1
            if slots_added >= max_slots_to_show:
                formatted_lines.append("- ... (i potencjalnie więcej)")
                break
    if slots_added == 0:
        return "Brak dostępnych zakresów czasowych w godzinach pracy w podanym okresie."
    formatted_output = "\n".join(formatted_lines)
    logging.debug(f"--- Zakresy sformatowane dla AI ({slots_added} pokazanych) ---\n{formatted_output}\n---------------------------------")
    return formatted_output

# =====================================================================
# === FUNKCJE GOOGLE SHEETS (ZAPIS + ODCZYT) ==========================
# =====================================================================

def get_sheets_service():
    """Inicjalizuje (i cachuje) usługę Google Sheets API używając dedykowanego klucza."""
    global _sheets_service
    if _sheets_service:
        return _sheets_service
    # Użyj dedykowanego pliku klucza dla Arkuszy
    if not os.path.exists(SHEETS_SERVICE_ACCOUNT_FILE):
        logging.error(f"KRYTYCZNY BŁĄD: Brak pliku klucza usługi Google Sheets: '{SHEETS_SERVICE_ACCOUNT_FILE}'")
        return None
    try:
        # Użyj dedykowanego pliku klucza i zakresów dla Arkuszy (odczyt/zapis)
        creds = service_account.Credentials.from_service_account_file(SHEETS_SERVICE_ACCOUNT_FILE, scopes=SHEET_SCOPES)
        service = build('sheets', 'v4', credentials=creds, cache_discovery=False)
        logging.info(f"Utworzono połączenie z Google Sheets API (odczyt/zapis) używając '{SHEETS_SERVICE_ACCOUNT_FILE}'.")
        _sheets_service = service
        return service
    except Exception as e:
        logging.error(f"Błąd tworzenia usługi Google Sheets używając '{SHEETS_SERVICE_ACCOUNT_FILE}': {e}", exc_info=True)
        return None

def write_to_sheet_phase1(psid, start_time):
    """Zapisuje dane Fazy 1 (PSID, Data, Czas) do arkusza."""
    service = get_sheets_service()
    if not service:
        return False, "Błąd połączenia z Google Sheets (Faza 1)."

    tz = _get_sheet_timezone()
    if start_time.tzinfo is None:
        start_time = tz.localize(start_time)
    else:
        start_time = start_time.astimezone(tz)
    date_str = start_time.strftime('%Y-%m-%d')
    time_str = start_time.strftime('%H:%M')

    # Przygotuj wiersz z pustymi miejscami na przyszłe dane
    # Kolejność zgodna z definicją kolumn
    data_row = [
        psid,          # 1. ID konta
        "",            # 2. Imie rodzica (później)
        "",            # 3. Nazwisko rodzica (później)
        "",            # 4. Imie ucznia (później)
        "",            # 5. Nazwisko ucznia (później)
        date_str,      # 6. Data
        time_str,      # 7. Godzina
        "",            # 8. Klasa (później)
        "",            # 9. Jaka szkoła (później)
        ""             # 10. Poziom (później)
    ]

    try:
        range_name = f"{SHEET_NAME}!A1" # Zakres do dopisywania
        body = {'values': [data_row]}
        logging.info(f"Próba zapisu Fazy 1 do arkusza '{SPREADSHEET_ID}' -> '{SHEET_NAME}': {data_row}")
        result = service.spreadsheets().values().append(
            spreadsheetId=SPREADSHEET_ID, range=range_name,
            valueInputOption='USER_ENTERED', insertDataOption='INSERT_ROWS', body=body
        ).execute()
        updated_range = result.get('updates', {}).get('updatedRange', '')
        logging.info(f"Zapisano Faza 1 pomyślnie w zakresie {updated_range}")
        # Spróbuj wyciągnąć numer wiersza z zakresu (np. 'Arkusz1!A5:J5')
        match = re.search(r'!A(\d+):', updated_range)
        row_index = int(match.group(1)) if match else None
        if row_index:
            logging.info(f"Zapisano Faza 1 w wierszu: {row_index}")
            return True, row_index # Zwróć sukces i numer wiersza
        else:
            logging.warning(f"Nie udało się wyodrębnić numeru wiersza z zakresu: {updated_range}")
            return True, None # Sukces zapisu, ale brak numeru wiersza (mniej idealne)

    except HttpError as error:
        error_details = f"Kod: {error.resp.status}, Powód: {error.resp.reason}"
        logging.error(f"Błąd API Google Sheets podczas zapisu Fazy 1: {error}, Szczegóły: {error_details}", exc_info=True)
        return False, f"Błąd zapisu Fazy 1 ({error_details})."
    except Exception as e:
        logging.error(f"Nieoczekiwany błąd Python podczas zapisu Fazy 1: {e}", exc_info=True)
        return False, "Wewnętrzny błąd systemu podczas zapisu Fazy 1."

def find_row_and_update_sheet(psid, start_time, student_data, sheet_row_index=None):
    """Znajduje wiersz (używając indeksu jeśli dostępny, inaczej szuka) i aktualizuje go danymi Fazy 2."""
    service = get_sheets_service()
    if not service:
        return False, "Błąd połączenia z Google Sheets (Faza 2)."

    tz = _get_sheet_timezone()
    if start_time.tzinfo is None:
        start_time = tz.localize(start_time)
    else:
        start_time = start_time.astimezone(tz)
    target_date_str = start_time.strftime('%Y-%m-%d')
    target_time_str = start_time.strftime('%H:%M')

    target_row_number = sheet_row_index # Użyj zapisanego indeksu, jeśli jest

    # Jeśli nie mamy zapisanego indeksu wiersza, spróbuj go znaleźć
    if target_row_number is None:
        logging.warning(f"Brak zapisanego indeksu wiersza dla PSID {psid} i terminu {target_date_str} {target_time_str}. Próba znalezienia...")
        try:
            read_range = SHEET_READ_RANGE_FOR_UPDATE
            result = service.spreadsheets().values().get(
                spreadsheetId=SPREADSHEET_ID, range=read_range
            ).execute()
            values = result.get('values', [])
            found_row_index = -1
            # Pamiętaj, że indeksy API są 0-based, a numery wierszy w arkuszu od 1
            # Dane zaczynają się od wiersza 2, więc pierwszy wiersz danych ma indeks 0 w `values`
            # i odpowiada wierszowi 2 w arkuszu.
            for i, row in enumerate(values):
                # Sprawdź, czy wiersz ma wystarczającą liczbę kolumn do porównania
                if len(row) >= max(SHEET_PSID_COLUMN_INDEX, SHEET_DATE_COLUMN_INDEX, SHEET_TIME_COLUMN_INDEX):
                    row_psid = row[SHEET_PSID_COLUMN_INDEX - 1].strip()
                    row_date = row[SHEET_DATE_COLUMN_INDEX - 1].strip()
                    row_time = row[SHEET_TIME_COLUMN_INDEX - 1].strip()

                    if row_psid == psid and row_date == target_date_str and row_time == target_time_str:
                        target_row_number = i + 2 # +2 bo indeksy API są 0-based, a dane zaczynają się od wiersza 2
                        logging.info(f"Znaleziono pasujący wiersz Fazy 1 w arkuszu: {target_row_number}")
                        break
                else:
                    logging.warning(f"Pominięto zbyt krótki wiersz ({len(row)} kolumn) podczas szukania wiersza Fazy 1.")

            if target_row_number is None: # Jeśli nadal nie znaleziono
                 logging.error(f"Nie znaleziono wiersza Fazy 1 dla PSID {psid} i terminu {target_date_str} {target_time_str}. Nie można zaktualizować.")
                 return False, "Nie znaleziono pierwotnego zapisu terminu."
        except HttpError as error:
            logging.error(f"Błąd API Google Sheets podczas szukania wiersza Fazy 1: {error}", exc_info=True)
            return False, "Błąd odczytu arkusza przy szukaniu wiersza."
        except Exception as e:
            logging.error(f"Nieoczekiwany błąd Python podczas szukania wiersza Fazy 1: {e}", exc_info=True)
            return False, "Wewnętrzny błąd systemu podczas szukania wiersza."

    # Mamy numer wiersza (target_row_number), przygotuj aktualizację
    try:
        parent_fn = student_data.get('parent_first_name', '') # Pobierz z danych przekazanych
        parent_ln = student_data.get('parent_last_name', '')
        student_fn = student_data.get('student_first_name', '')
        student_ln = student_data.get('student_last_name', '')
        grade_info = student_data.get('grade_info', '')
        level_info = student_data.get('level_info', '')
        class_desc, school_type = extract_school_type(grade_info)

        # Przygotuj dane do aktualizacji - tylko kolumny od Rodzica do Poziomu
        # Kolejność musi odpowiadać kolumnom od B do J
        update_data = [
            parent_fn, parent_ln, student_fn, student_ln,
            # Nie aktualizujemy daty i czasu tutaj, tylko dane ucznia/rodzica
            # Puste stringi dla kolumn F i G, aby nie nadpisać przypadkowo
            # Jeśli chcesz nadpisać, wstaw tu target_date_str, target_time_str
            "", "",
            class_desc, school_type, level_info
        ]

        # Określ zakres do aktualizacji (od kolumny B do J w znalezionym wierszu)
        update_range = f"{SHEET_NAME}!B{target_row_number}:J{target_row_number}"
        body = {'values': [update_data]}

        logging.info(f"Próba aktualizacji Fazy 2 wiersza {target_row_number} w zakresie {update_range} danymi: {update_data}")
        update_result = service.spreadsheets().values().update(
            spreadsheetId=SPREADSHEET_ID, range=update_range,
            valueInputOption='USER_ENTERED', body=body
        ).execute()
        logging.info(f"Zaktualizowano Faza 2 pomyślnie: {update_result.get('updatedCells')} komórek.")
        return True, None

    except HttpError as error:
        error_details = f"Kod: {error.resp.status}, Powód: {error.resp.reason}"
        logging.error(f"Błąd API Google Sheets podczas aktualizacji Fazy 2: {error}, Szczegóły: {error_details}", exc_info=True)
        return False, f"Błąd aktualizacji Fazy 2 ({error_details})."
    except Exception as e:
        logging.error(f"Nieoczekiwany błąd Python podczas aktualizacji Fazy 2: {e}", exc_info=True)
        return False, "Wewnętrzny błąd systemu podczas aktualizacji Fazy 2."


def is_slot_in_sheet(start_time):
    """Sprawdza, czy dany termin (data i godzina) już istnieje w arkuszu."""
    service = get_sheets_service()
    if not service:
        logging.error("Błąd: Usługa arkuszy niedostępna do weryfikacji slotu w arkuszu.")
        return True # Bezpieczniej założyć, że jest zajęty

    if not isinstance(start_time, datetime.datetime):
        logging.error(f"Błąd weryfikacji w arkuszu: start_time nie jest obiektem datetime ({type(start_time)})")
        return True

    tz = _get_sheet_timezone()
    if start_time.tzinfo is None:
        start_time = tz.localize(start_time)
    else:
        start_time = start_time.astimezone(tz)

    target_date_str = start_time.strftime('%Y-%m-%d')
    target_time_str = start_time.strftime('%H:%M')

    try:
        date_col_letter = chr(ord('A') + SHEET_DATE_COLUMN_INDEX - 1)
        time_col_letter = chr(ord('A') + SHEET_TIME_COLUMN_INDEX - 1)
        # Czytaj tylko potrzebne kolumny
        read_range = f"{SHEET_NAME}!{date_col_letter}2:{time_col_letter}"
    except Exception as e:
        logging.error(f"Błąd konwersji indeksów kolumn na litery: {e}")
        return True

    logging.debug(f"Sprawdzanie arkusza '{SHEET_NAME}' w zakresie '{read_range}' dla terminu: {target_date_str} {target_time_str}")

    try:
        result = service.spreadsheets().values().get(
            spreadsheetId=SPREADSHEET_ID,
            range=read_range
        ).execute()
        values = result.get('values', [])

        if not values:
            logging.debug("Arkusz jest pusty lub nie zawiera danych w sprawdzanym zakresie.")
            return False

        for row in values:
            if len(row) >= 2: # Oczekujemy co najmniej daty i czasu
                sheet_date_str = row[0].strip()
                sheet_time_str = row[1].strip()
                if sheet_date_str == target_date_str and sheet_time_str == target_time_str:
                    logging.warning(f"Weryfikacja arkusza: Slot {target_date_str} {target_time_str} ZNALEZIONY w arkuszu.")
                    return True

        logging.info(f"Weryfikacja arkusza: Slot {target_date_str} {target_time_str} NIE ZNALEZIONY w arkuszu.")
        return False

    except HttpError as error:
        logging.error(f"Błąd HTTP API podczas odczytu arkusza do weryfikacji: {error.resp.status} {error.resp.reason}", exc_info=True)
        return True # Bezpieczniej założyć, że zajęty
    except Exception as e:
        logging.error(f"Nieoczekiwany błąd podczas odczytu arkusza do weryfikacji: {e}", exc_info=True)
        return True

# =====================================================================
# === FUNKCJE KOMUNIKACJI FB ==========================================
# =====================================================================
def _send_typing_on(recipient_id):
    """Wysyła wskaźnik 'pisania' do użytkownika."""
    if not PAGE_ACCESS_TOKEN or len(PAGE_ACCESS_TOKEN) < 50 or not ENABLE_TYPING_DELAY:
        return
    logging.debug(f"[{recipient_id}] Wysyłanie 'typing_on'")
    params = {"access_token": PAGE_ACCESS_TOKEN}
    payload = {"recipient": {"id": recipient_id}, "sender_action": "typing_on"}
    try:
        requests.post(FACEBOOK_GRAPH_API_URL, params=params, json=payload, timeout=3)
    except requests.exceptions.RequestException as e:
        logging.warning(f"[{recipient_id}] Błąd wysyłania 'typing_on': {e}")

def _send_single_message(recipient_id, message_text):
    """Wysyła pojedynczy fragment wiadomości przez Facebook Graph API."""
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
        # Poprawiona obsługa błędu API
        if fb_error := response_json.get('error'): # Użycie "walrus operator" (:=) dla zwięzłości
            logging.error(f"!!! BŁĄD FB API podczas wysyłania wiadomości: {fb_error} !!!")
            # Sprawdź kod błędu w osobnym if
            if fb_error.get('code') == 190:
                logging.error("!!! Wygląda na to, że FB_PAGE_ACCESS_TOKEN jest nieprawidłowy lub wygasł !!!")
            return False # Zwróć False w przypadku jakiegokolwiek błędu API
        # Koniec poprawionej obsługi błędu
        logging.debug(f"[{recipient_id}] Fragment wiadomości wysłany pomyślnie (Message ID: {response_json.get('message_id')}).")
        return True
    except requests.exceptions.Timeout:
        logging.error(f"!!! BŁĄD TIMEOUT podczas wysyłania wiadomości do {recipient_id} !!!")
        return False
    except requests.exceptions.HTTPError as http_err:
         logging.error(f"!!! BŁĄD HTTP {http_err.response.status_code} podczas wysyłania wiadomości do {recipient_id}: {http_err} !!!")
         if http_err.response is not None:
            try:
                logging.error(f"Odpowiedź FB (błąd HTTP): {http_err.response.json()}")
            except json.JSONDecodeError:
                logging.error(f"Odpowiedź FB (błąd HTTP, nie JSON): {http_err.response.text}")
         return False
    except requests.exceptions.RequestException as req_err:
        logging.error(f"!!! BŁĄD RequestException podczas wysyłania wiadomości do {recipient_id}: {req_err} !!!")
        return False
    except Exception as e:
        logging.error(f"!!! Nieoczekiwany BŁĄD podczas wysyłania wiadomości do {recipient_id}: {e} !!!", exc_info=True)
        return False

def send_message(recipient_id, full_message_text):
    """Wysyła wiadomość do użytkownika, dzieląc ją na fragmenty, jeśli jest za długa."""
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
    if message_len <= MESSAGE_CHAR_LIMIT:
        chunks.append(full_message_text)
    else:
        logging.info(f"[{recipient_id}] Wiadomość za długa ({message_len} > {MESSAGE_CHAR_LIMIT}). Dzielenie na fragmenty...")
        remaining_text = full_message_text
        while remaining_text:
            if len(remaining_text) <= MESSAGE_CHAR_LIMIT:
                chunks.append(remaining_text.strip())
                break
            split_index = -1
            delimiters = ['\n\n', '\n', '. ', '! ', '? ', ' ']
            for delimiter in delimiters:
                search_limit = MESSAGE_CHAR_LIMIT - len(delimiter) + 1
                temp_index = remaining_text.rfind(delimiter, 0, search_limit)
                if temp_index != -1:
                    split_index = temp_index + len(delimiter)
                    break
            if split_index == -1:
                split_index = MESSAGE_CHAR_LIMIT
            chunk = remaining_text[:split_index].strip()
            if chunk:
                chunks.append(chunk)
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
                if remaining_delay > 0:
                    time.sleep(remaining_delay)
            else:
                time.sleep(MESSAGE_DELAY_SECONDS)
    logging.info(f"--- [{recipient_id}] Zakończono proces wysyłania. Wysłano {send_success_count}/{num_chunks} fragmentów. ---")

def _simulate_typing(recipient_id, duration_seconds):
    """Wysyła 'typing_on' i czeka przez określony czas."""
    if ENABLE_TYPING_DELAY and duration_seconds > 0:
        _send_typing_on(recipient_id)
        time.sleep(min(duration_seconds, MAX_TYPING_DELAY_SECONDS * 1.1))

# =====================================================================
# === FUNKCJE WYWOŁANIA AI ============================================
# =====================================================================
def _call_gemini(user_psid, prompt_history, generation_config, task_name, max_retries=3):
    """Wywołuje API Gemini z obsługą błędów, logowaniem i ponowieniami."""
    if not gemini_model:
        logging.error(f"!!! [{user_psid}] KRYTYCZNY BŁĄD: Model Gemini ({task_name}) jest niedostępny (None)!")
        return None
    if not isinstance(prompt_history, list) or not all(isinstance(item, Content) for item in prompt_history):
        logging.error(f"!!! [{user_psid}] Nieprawidłowy format promptu ({task_name}).")
        return None
    logging.info(f"[{user_psid}] Wywołanie Gemini: {task_name} (Prompt: {len(prompt_history)} wiadomości)")
    last_user_msg = next((msg.parts[0].text for msg in reversed(prompt_history) if msg.role == 'user' and msg.parts), None)
    if last_user_msg:
        logging.debug(f"    Ostatnia wiadomość usera ({task_name}): '{last_user_msg[:200]}{'...' if len(last_user_msg)>200 else ''}'")
    else:
        logging.debug(f"    Brak wiadomości użytkownika w bezpośrednim prompcie ({task_name}).")
    attempt = 0
    finish_reason = None # Zmienna do przechowywania ostatniego powodu zakończenia
    while attempt < max_retries:
        attempt += 1
        logging.debug(f"    Próba {attempt}/{max_retries} wywołania Gemini ({task_name})...")
        try:
            _simulate_typing(user_psid, MIN_TYPING_DELAY_SECONDS * 0.8)
            response = gemini_model.generate_content(prompt_history, generation_config=generation_config, safety_settings=SAFETY_SETTINGS) # Używa globalnych SAFETY_SETTINGS
            if response and response.candidates:
                candidate = response.candidates[0]
                finish_reason = candidate.finish_reason # Zapisz ostatni powód
                if finish_reason != 1:
                    safety_ratings = candidate.safety_ratings
                    logging.warning(f"[{user_psid}] Gemini ({task_name}) ZAKOŃCZONE NIEPRAWIDŁOWO! Powód: {finish_reason.name} ({finish_reason.value}). Safety Ratings: {safety_ratings}")
                    if finish_reason in [3, 4] and attempt < max_retries: # SAFETY lub RECITATION
                        logging.warning(f"    Ponawianie ({attempt}/{max_retries}) z powodu blokady...")
                        time.sleep(1.5 * attempt)
                        continue
                    else:
                        logging.error(f"!!! [{user_psid}] Gemini ({task_name}) nieudane po blokadzie lub innym błędzie.")
                        # Zwróć specyficzną wiadomość tylko w przypadku błędu SAFETY
                        if finish_reason == 3:
                            return "Przepraszam, nie mogę przetworzyć tej prośby ze względu na zasady bezpieczeństwa."
                        else:
                            return "Wystąpił problem z generowaniem odpowiedzi." # Inny błąd
                if candidate.content and candidate.content.parts:
                    generated_text = "".join(part.text for part in candidate.content.parts if hasattr(part, 'text')).strip()
                    if generated_text:
                        logging.info(f"[{user_psid}] Gemini ({task_name}) zwróciło odpowiedź (długość: {len(generated_text)}).")
                        logging.debug(f"    Pełna odpowiedź Gemini ({task_name}): '{generated_text}'")
                        return generated_text # Sukces
                    else:
                        logging.warning(f"[{user_psid}] Gemini ({task_name}) zwróciło kandydata z pustą treścią.")
                else:
                    logging.warning(f"[{user_psid}] Gemini ({task_name}) zwróciło kandydata bez treści (content/parts).")
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
                 time.sleep(sleep_time)
                 continue
             else:
                 break
        except Exception as e:
             if isinstance(e, NameError) and 'gemini_model' in str(e):
                 logging.critical(f"!!! KRYTYCZNY NameError [{user_psid}] w _call_gemini: {e}. gemini_model jest None!", exc_info=True)
                 return None
             else:
                 logging.error(f"!!! BŁĄD [{user_psid}] Gemini ({task_name}) - Nieoczekiwany błąd Python (Próba {attempt}/{max_retries}): {e}", exc_info=True)
        # Jeśli doszło tutaj, oznacza to błąd inny niż HTTP lub brak poprawnej odpowiedzi
        if attempt < max_retries:
            logging.warning(f"    Problem z odpowiedzią Gemini ({task_name}). Oczekiwanie przed ponowieniem ({attempt+1}/{max_retries})...")
            time.sleep(1.5 * attempt)

    # Po wszystkich próbach
    logging.error(f"!!! KRYTYCZNY BŁĄD [{user_psid}] Gemini ({task_name}) - Nie udało się uzyskać poprawnej odpowiedzi po {max_retries} próbach.")
    # Zwróć None lub specyficzną wiadomość błędu, jeśli ostatni błąd to SAFETY
    if finish_reason == 3:
        return "Przepraszam, nie mogę przetworzyć tej prośby ze względu na zasady bezpieczeństwa."
    return None # Ogólny błąd po wszystkich próbach

# =====================================================================
# === INSTRUKCJE SYSTEMOWE I GŁÓWNE FUNKCJE AI ========================
# =====================================================================

# --- JEDEN WIELKI PROMPT (EKSPERYMENTALNY) ---
SYSTEM_INSTRUCTION_UNIFIED = """Jesteś wielozadaniowym asystentem AI dla 'Zakręcone Korepetycje'. Twoim celem jest obsługa klienta od pierwszego kontaktu aż po zapisanie danych do systemu. Musisz płynnie przechodzić między różnymi zadaniami: odpowiadaniem na ogólne pytania, umawianiem terminu i zbieraniem danych ucznia.

**Ogólne Zasady:**
*   Odpowiadaj uprzejmie, profesjonalnie i po polsku. Używaj zwrotów "Państwo".
*   Koszt zajęć to 60zł dla szkoły podstawowej i 75zł dla szkoły średniej za 60 minut. Podawaj tę informację tylko na wyraźne pytanie.
*   Nie podawaj szczegółów płatności innych niż cena.
*   Masz dostęp do historii konwersacji.

**Przepływ Pracy:**

1.  **Tryb Ogólny (Domyślny):**
    *   Odpowiadaj na pytania o ofertę, metodykę, przedmioty itp.
    *   Jeśli użytkownik **wyraźnie zasygnalizuje chęć umówienia się** (np. "chcę umówić lekcję", "kiedy macie wolne?", "jak zacząć?"), Twoja odpowiedź musi zawierać **TYLKO I WYŁĄCZNIE** znacznik: `{action_check_availability}`.

2.  **Tryb Umawiania Terminu (Aktywowany po `{action_check_availability}` lub gdy kontekst zawiera `available_ranges`):**
    *   System (kod Pythona) dostarczy Ci listę dostępnych zakresów czasowych w formacie:
        ```
        --- Dostępne Zakresy (Data YYYY-MM-DD, Dzień, Od Godziny HH:MM, Do Godziny HH:MM) ---
        - 2024-05-20, Poniedziałek, od 16:00, do 18:00
        - 2024-05-21, Wtorek, od 17:30, do 19:30
        - ...
        ```
        (Jeśli lista jest pusta, poinformuj o tym użytkownika i zakończ proces umawiania).
    *   Twoim zadaniem jest **zaproponowanie konkretnego terminu** z tej listy (np. "Czy odpowiadałby Państwu termin w najbliższy poniedziałek o 16:00?") i negocjowanie z użytkownikiem, aż dojdziecie do porozumienia co do **konkretnego terminu z listy**.
    *   Gdy użytkownik **zaakceptuje konkretny termin z listy**, Twoja odpowiedź musi zawierać **TYLKO I WYŁĄCZNIE** znacznik w formacie: `{action_verify_slot}YYYY-MM-DDTHH:MM:SS]` (gdzie YYYY-MM-DDTHH:MM:SS to zaakceptowany termin w ISO 8601).
    *   Jeśli podczas umawiania użytkownik zada pytanie ogólne (np. o cenę), odpowiedz na nie krótko i **kontynuuj proces umawiania terminu** (nie używaj specjalnych znaczników do przełączania).

3.  **Tryb Weryfikacji Terminu (Aktywowany po `{action_verify_slot}`):**
    *   System (kod Pythona) sprawdzi dostępność terminu w Kalendarzu i Arkuszu. Otrzymasz informację zwrotną w historii jako wiadomość systemową, np.:
        *   `[SYSTEM_INFO: Slot YYYY-MM-DDTHH:MM:SS jest DOSTĘPNY]`
        *   `[SYSTEM_INFO: Slot YYYY-MM-DDTHH:MM:SS jest ZAJĘTY (Kalendarz)]`
        *   `[SYSTEM_INFO: Slot YYYY-MM-DDTHH:MM:SS jest ZAJĘTY (Arkusz)]`
    *   **Jeśli slot jest DOSTĘPNY:** Twoja odpowiedź musi zawierać **TYLKO I WYŁĄCZNIE** znacznik: `{action_write_phase1}YYYY-MM-DDTHH:MM:SS]` (użyj tego samego ISO co w `{action_verify_slot}`).
    *   **Jeśli slot jest ZAJĘTY:** Poinformuj o tym użytkownika (np. "Niestety, ten termin został właśnie zajęty...") i **wróć do Trybu Umawiania Terminu** - zaproponuj inny termin z listy dostępnych zakresów (które nadal powinieneś mieć w kontekście z poprzedniego kroku).

4.  **Tryb Zapisu Fazy 1 (Aktywowany po `{action_write_phase1}`):**
    *   System (kod Pythona) zapisze PSID, Datę i Godzinę do arkusza. Otrzymasz informację zwrotną, np.:
        *   `[SYSTEM_INFO: Zapis Fazy 1 dla YYYY-MM-DDTHH:MM:SS zakończony pomyślnie.]`
        *   `[SYSTEM_INFO: Błąd zapisu Fazy 1 dla YYYY-MM-DDTHH:MM:SS.]`
    *   **Jeśli zapis się powiódł:** Twoja odpowiedź musi zawierać **TYLKO I WYŁĄCZNIE** znacznik: `{action_gather_info}`.
    *   **Jeśli wystąpił błąd zapisu:** Poinformuj użytkownika o błędzie technicznym i zakończ proces.

5.  **Tryb Zbierania Danych Ucznia (Aktywowany po `{action_gather_info}`):**
    *   Twoim zadaniem jest zebranie **wyłącznie** informacji o uczniu:
        *   Pełne Imię i Nazwisko UCZNIA.
        *   Klasa ORAZ typ szkoły (np. "3 klasa liceum", "8 klasa podstawówki").
        *   Poziom nauczania (Podstawowy/Rozszerzony) - **tylko** dla liceum/technikum.
    *   Prowadź naturalną rozmowę, zadając pytania o brakujące dane.
    *   Jeśli użytkownik zada pytanie ogólne (np. o cenę), odpowiedz na nie krótko i **kontynuuj proces zbierania danych**.
    *   Gdy zbierzesz **wszystkie** wymagane informacje o uczniu, Twoja odpowiedź musi zawierać **TYLKO I WYŁĄCZNIE** znacznik w formacie: `{action_finalize}<imię_ucznia>, <nazwisko_ucznia>, <klasa_info>, <poziom>]` (np. `[ACTION: FINALIZE_AND_UPDATE_SHEET: Jan, Kowalski, 3 klasa liceum, Podstawowy]`). Użyj "brak" dla poziomu, jeśli nie dotyczy.

6.  **Tryb Finalizacji (Aktywowany po `{action_finalize}`):**
    *   System (kod Pythona) zaktualizuje arkusz i pobierze dane rodzica. Otrzymasz informację zwrotną, np.:
        *   `[SYSTEM_INFO: Aktualizacja Fazy 2 zakończona pomyślnie.]`
        *   `[SYSTEM_INFO: Błąd aktualizacji Fazy 2.]`
    *   **Jeśli aktualizacja się powiodła:** Wyślij użytkownikowi finalną wiadomość potwierdzającą: "Dziękuję za wszystkie informacje. Dane zostały zapisane. Wkrótce skontaktujemy się w celu potwierdzenia szczegółów. Proszę również oczekiwać na wiadomość dotyczącą płatności i dostępu do materiałów na profilu dedykowanym do komunikacji: https://www.facebook.com/profile.php?id=61576135251276"
    *   **Jeśli wystąpił błąd aktualizacji:** Poinformuj użytkownika o błędzie technicznym.

**Ważne:** Zawsze analizuj ostatnią wiadomość systemową `[SYSTEM_INFO: ...]` (jeśli istnieje), aby wiedzieć, co się stało w poprzednim kroku i jak kontynuować. Precyzyjnie generuj znaczniki `[ACTION: ...]`, ponieważ od nich zależy działanie systemu.
""".format(
    duration=APPOINTMENT_DURATION_MINUTES, min_lead_hours=MIN_BOOKING_LEAD_HOURS,
    calendar_timezone=CALENDAR_TIMEZONE,
    action_check_availability=ACTION_CHECK_AVAILABILITY,
    action_verify_slot=ACTION_VERIFY_SLOT, # Zwróć pełny znacznik z ':'
    action_write_phase1=ACTION_WRITE_PHASE1, # Zwróć pełny znacznik z ':'
    action_gather_info=ACTION_GATHER_INFO,
    action_finalize=ACTION_FINALIZE # Zwróć pełny znacznik z ':'
)

# --- Jedna funkcja AI ---
def get_unified_ai_response(user_psid, history, current_user_message_text, context, available_ranges=None):
    """Wywołuje AI z ujednoliconym promptem, potencjalnie wstrzykując dostępne zakresy."""
    if not gemini_model:
        logging.error(f"!!! [{user_psid}] Model Gemini niezaładowany (Unified)!")
        return "Przepraszam, mam problem z systemem."

    # Przygotuj prompt systemowy
    system_instruction = SYSTEM_INSTRUCTION_UNIFIED
    # Zawsze formatuj, nawet jeśli zakresy są puste (None)
    ranges_text = format_ranges_for_ai(available_ranges) if available_ranges is not None else "[Nie dotyczy - tryb ogólny lub oczekiwanie na sprawdzenie dostępności]"
    try:
        system_instruction = system_instruction.format(available_ranges_text=ranges_text)
    except KeyError as e:
        logging.error(f"Błąd formatowania Unified Prompt: Brak klucza {e}")
        return "Błąd konfiguracji AI."
    except Exception as format_e:
        logging.error(f"Błąd nieoczekiwany formatowania Unified Prompt: {format_e}")
        return "Błąd wewnętrzny konfiguracji AI."


    # Zbuduj pełny prompt - JUŻ BEZ DODAWANIA KONTEKSTU JAKO OSOBNEJ WIADOMOŚCI
    initial_prompt = [
        Content(role="user", parts=[Part.from_text(system_instruction)]),
        Content(role="model", parts=[Part.from_text("Rozumiem. Będę postępować zgodnie z przepływem pracy, analizując historię i generując odpowiednie znaczniki akcji dla systemu lub odpowiedzi dla użytkownika.")])
    ]
    full_prompt = initial_prompt + history # Łączymy instrukcję i historię

    if current_user_message_text:
        full_prompt.append(Content(role="user", parts=[Part.from_text(current_user_message_text)]))

    # Ogranicz historię
    max_prompt_messages = (MAX_HISTORY_TURNS * 2) + 2 # +2 dla initial_prompt
    while len(full_prompt) > max_prompt_messages:
        # Usuwaj najstarszą parę user/model (pomijając instrukcję)
        start_index_to_remove = 2
        if len(full_prompt) > start_index_to_remove + 1:
            full_prompt.pop(start_index_to_remove + 1) # model
            full_prompt.pop(start_index_to_remove)     # user
        else:
            break # Nie usuwaj instrukcji

    # Wywołaj Gemini
    response_text = _call_gemini(user_psid, full_prompt, GENERATION_CONFIG_UNIFIED, "Unified Conversation")

    return response_text # Zwróć pełną odpowiedź AI (ze znacznikami akcji)


# =====================================================================
# === WEBHOOK HANDLERS ================================================
# =====================================================================

@app.route('/webhook', methods=['GET'])
def webhook_verification():
    """Obsługuje weryfikację webhooka przez Facebooka."""
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
    """Główny handler dla przychodzących zdarzeń z Messengera (Model AI-Driven State)."""
    logging.info(f"\n{'='*30} {datetime.datetime.now(_get_calendar_timezone()):%Y-%m-%d %H:%M:%S %Z} POST /webhook {'='*30}")
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
                    history, context = load_history(sender_id) # Kontekst przechowuje teraz dane tymczasowe
                    history_for_gemini = [h for h in history if isinstance(h, Content) and h.role in ('user', 'model')]
                    logging.debug(f"    Aktualny kontekst przed przetworzeniem: {context}")

                    user_content = None
                    current_user_message_text = None
                    ai_response_text = None
                    system_message_for_ai = None # Do przekazywania wyników akcji
                    available_ranges_for_ai = None # Do przekazania zakresów
                    context_data_to_save = context.copy() # Pracujemy na kopii kontekstu

                    # === Obsługa wiadomości / postbacków ===
                    if message_data := event.get("message"):
                        if message_data.get("is_echo"): continue
                        user_input_text = message_data.get("text", "").strip()
                        if user_input_text:
                            user_content = Content(role="user", parts=[Part.from_text(user_input_text)])
                            current_user_message_text = user_input_text
                            logging.info(f"    Otrzymano wiadomość tekstową: '{user_input_text[:100]}{'...' if len(user_input_text)>100 else ''}'")
                        elif attachments := message_data.get("attachments"):
                             att_type = attachments[0].get('type','nieznany')
                             logging.info(f"      Otrzymano załącznik typu: {att_type}.")
                             user_content = Content(role="user", parts=[Part.from_text(f"[Użytkownik wysłał załącznik typu: {att_type}]")])
                             current_user_message_text = f"[Użytkownik wysłał załącznik typu: {att_type}]" # Przekaż info do AI
                        else: continue # Ignoruj puste wiadomości
                    elif postback := event.get("postback"):
                        payload = postback.get("payload"); title = postback.get("title", "")
                        logging.info(f"    Otrzymano postback: Payload='{payload}', Tytuł='{title}'")
                        user_input_text = f"Użytkownik kliknął przycisk: '{title}' (Payload: {payload})"
                        user_content = Content(role="user", parts=[Part.from_text(user_input_text)])
                        current_user_message_text = user_input_text
                    elif event.get("read") or event.get("delivery"): continue # Ignoruj
                    else: logging.warning(f"    Otrzymano nieobsługiwany typ zdarzenia: {json.dumps(event)}"); continue

                    # --- Główna pętla sterowana przez AI ---
                    max_ai_calls = 3 # Ogranicznik pętli AI -> Akcja -> AI
                    ai_call_count = 0
                    last_ai_response = None # Do zapisania w historii, jeśli nie ma nowej odpowiedzi

                    while ai_call_count < max_ai_calls:
                        ai_call_count += 1
                        logging.debug(f"  >> Cykl AI {ai_call_count}/{max_ai_calls}")

                        # 1. Wywołaj AI
                        # Przekaż zakresy tylko jeśli są w kontekście (zostały pobrane wcześniej)
                        ranges_to_pass = context_data_to_save.get('available_ranges')
                        ai_response_text = get_unified_ai_response(sender_id, history_for_gemini, current_user_message_text, context_data_to_save, available_ranges=ranges_to_pass)
                        current_user_message_text = None # Wiadomość użytkownika przetworzona w tym wywołaniu

                        if not ai_response_text:
                            # Obsługa błędu AI
                            logging.error(f"Błąd krytyczny: Unified AI nie zwróciło odpowiedzi (Cykl {ai_call_count}).")
                            send_message(sender_id, "Przepraszam, wystąpił wewnętrzny błąd. Spróbuj ponownie później.")
                            last_ai_response = Content(role="model", parts=[Part.from_text("[Błąd wewnętrzny AI]")]) # Zapisz błąd do historii
                            break # Przerwij pętlę AI

                        # Zapisz pełną odpowiedź AI do potencjalnego zapisu w historii
                        last_ai_response = Content(role="model", parts=[Part.from_text(ai_response_text)])

                        # 2. Analiza odpowiedzi AI i wykonanie akcji
                        text_to_send_user = ai_response_text # Domyślnie cała odpowiedź
                        system_message_for_ai = None # Resetuj wiadomość systemową
                        available_ranges_for_ai = None # Resetuj zakresy
                        should_break_loop = True # Domyślnie zakończ pętlę po tym cyklu, chyba że akcja wymaga kolejnego wywołania AI
                        action_tag_found = False # Czy znaleziono jakikolwiek znacznik akcji?

                        # --- Sprawdzanie znaczników akcji ---

                        # Akcja: Sprawdź dostępność
                        if ACTION_CHECK_AVAILABILITY in ai_response_text:
                            logging.info(f"      AI zażądało sprawdzenia dostępności [{ACTION_CHECK_AVAILABILITY}]")
                            action_tag_found = True
                            should_break_loop = False # Potrzebne kolejne wywołanie AI
                            text_to_send_user = ai_response_text.replace(ACTION_CHECK_AVAILABILITY, "").strip()
                            tz = _get_calendar_timezone(); now = datetime.datetime.now(tz); search_start = now; search_end_date = (search_start + datetime.timedelta(days=MAX_SEARCH_DAYS)).date(); search_end = tz.localize(datetime.datetime.combine(search_end_date, datetime.time(WORK_END_HOUR, 0)))
                            _simulate_typing(sender_id, MAX_TYPING_DELAY_SECONDS * 0.6)
                            free_ranges = get_free_time_ranges(TARGET_CALENDAR_ID, search_start, search_end)
                            context_data_to_save['available_ranges'] = free_ranges # Zapisz zakresy w kontekście
                            if free_ranges:
                                system_message_for_ai = f"[SYSTEM_INFO: Dostępne zakresy zostały pobrane i zapisane w kontekście. Są one teraz dostępne w Twoim prompcie systemowym. Zaproponuj termin.]"
                            else:
                                system_message_for_ai = f"[SYSTEM_INFO: Brak dostępnych zakresów w kalendarzu w ciągu najbliższych {MAX_SEARCH_DAYS} dni z wymaganym wyprzedzeniem {MIN_BOOKING_LEAD_HOURS}h. Poinformuj użytkownika.]"

                        # Akcja: Weryfikuj Slot (Kalendarz + Arkusz)
                        verify_match = re.search(rf"{re.escape(ACTION_VERIFY_SLOT)}(.*?)]", ai_response_text)
                        if verify_match:
                            iso_to_verify = verify_match.group(1).strip()
                            logging.info(f"      AI zażądało weryfikacji slotu [{ACTION_VERIFY_SLOT}{iso_to_verify}]")
                            action_tag_found = True
                            should_break_loop = False # Potrzebne kolejne wywołanie AI
                            text_to_send_user = re.sub(rf"{re.escape(ACTION_VERIFY_SLOT)}.*?]", "", ai_response_text).strip() # Usuń znacznik z odpowiedzi
                            try:
                                proposed_start = datetime.datetime.fromisoformat(iso_to_verify)
                                tz_cal = _get_calendar_timezone()
                                if proposed_start.tzinfo is None: proposed_start = tz_cal.localize(proposed_start)
                                else: proposed_start = proposed_start.astimezone(tz_cal)
                                context_data_to_save['proposed_slot_iso'] = proposed_start.isoformat() # Zapisz proponowany slot
                                context_data_to_save['proposed_slot_formatted'] = format_slot_for_user(proposed_start)
                                _simulate_typing(sender_id, MIN_TYPING_DELAY_SECONDS * 1.5) # Symulacja weryfikacji
                                calendar_free = is_slot_actually_free(proposed_start, TARGET_CALENDAR_ID)
                                sheet_free = False
                                if calendar_free: sheet_free = not is_slot_in_sheet(proposed_start)
                                if calendar_free and sheet_free: system_message_for_ai = f"[SYSTEM_INFO: Slot {iso_to_verify} jest DOSTĘPNY. Wygeneruj akcję zapisu Fazy 1: {ACTION_WRITE_PHASE1}{iso_to_verify}]"
                                else: reason = "Kalendarz" if not calendar_free else "Arkusz"; system_message_for_ai = f"[SYSTEM_INFO: Slot {iso_to_verify} jest ZAJĘTY ({reason}). Poinformuj użytkownika i zaproponuj inny termin z dostępnych zakresów.]"; context_data_to_save.pop('proposed_slot_iso', None); context_data_to_save.pop('proposed_slot_formatted', None)
                            except Exception as e: logging.error(f"Błąd podczas weryfikacji slotu {iso_to_verify}: {e}", exc_info=True); system_message_for_ai = "[SYSTEM_INFO: Błąd wewnętrzny podczas weryfikacji terminu. Poinformuj użytkownika o problemie technicznym.]"

                        # Akcja: Zapisz Fazę 1
                        write1_match = re.search(rf"{re.escape(ACTION_WRITE_PHASE1)}(.*?)]", ai_response_text)
                        if write1_match:
                            iso_to_write = write1_match.group(1).strip()
                            logging.info(f"      AI zażądało zapisu Fazy 1 dla slotu [{ACTION_WRITE_PHASE1}{iso_to_write}]")
                            action_tag_found = True
                            should_break_loop = False # Potrzebne kolejne wywołanie AI
                            text_to_send_user = re.sub(rf"{re.escape(ACTION_WRITE_PHASE1)}.*?]", "", ai_response_text).strip()
                            try:
                                start_time_obj = datetime.datetime.fromisoformat(iso_to_write)
                                if context_data_to_save.get('proposed_slot_iso') != iso_to_write: logging.warning(f"Niezgodność ISO w akcji WRITE_PHASE1 ({iso_to_write}) z kontekstem ({context_data_to_save.get('proposed_slot_iso')}). Używam ISO z akcji."); context_data_to_save['proposed_slot_iso'] = iso_to_write; tz_cal = _get_calendar_timezone(); start_time_obj = start_time_obj.astimezone(tz_cal) if start_time_obj.tzinfo else tz_cal.localize(start_time_obj); context_data_to_save['proposed_slot_formatted'] = format_slot_for_user(start_time_obj)
                                write_ok, write_msg_or_row = write_to_sheet_phase1(sender_id, start_time_obj)
                                if write_ok:
                                    system_message_for_ai = f"[SYSTEM_INFO: Zapis Fazy 1 dla {iso_to_write} zakończony pomyślnie. Przejdź do zbierania danych ucznia generując akcję {ACTION_GATHER_INFO}.]"
                                    if isinstance(write_msg_or_row, int): context_data_to_save['sheet_row_index'] = write_msg_or_row # Zapisz indeks wiersza
                                else: system_message_for_ai = f"[SYSTEM_INFO: Błąd zapisu Fazy 1 dla {iso_to_write}: {write_msg_or_row}. Poinformuj użytkownika o problemie technicznym.]"; context_data_to_save.pop('proposed_slot_iso', None); context_data_to_save.pop('proposed_slot_formatted', None); context_data_to_save.pop('sheet_row_index', None)
                            except Exception as e: logging.error(f"Błąd podczas zapisu Fazy 1 dla {iso_to_write}: {e}", exc_info=True); system_message_for_ai = "[SYSTEM_INFO: Błąd wewnętrzny podczas zapisu Fazy 1. Poinformuj użytkownika.]"

                        # Akcja: Rozpocznij Zbieranie Danych
                        if ACTION_GATHER_INFO in ai_response_text:
                            logging.info(f"      AI zażądało rozpoczęcia zbierania danych [{ACTION_GATHER_INFO}]")
                            action_tag_found = True
                            should_break_loop = False # Potrzebne kolejne wywołanie AI
                            text_to_send_user = ai_response_text.replace(ACTION_GATHER_INFO, "").strip()
                            if 'known_parent_first_name' not in context_data_to_save: # Pobierz tylko jeśli brakuje
                                parent_profile = get_user_profile(sender_id)
                                context_data_to_save['known_parent_first_name'] = parent_profile.get('first_name', '') if parent_profile else ''
                                context_data_to_save['known_parent_last_name'] = parent_profile.get('last_name', '') if parent_profile else ''
                            system_message_for_ai = "[SYSTEM_INFO: Rozpocznij zbieranie danych ucznia (Imię, Nazwisko, KlasaInfo, Poziom). Dane rodzica zostały pobrane/sprawdzone.]"

                        # Akcja: Finalizuj i Zaktualizuj Arkusz
                        finalize_match = re.search(rf"{re.escape(ACTION_FINALIZE)}(.*?)\]", ai_response_text)
                        if finalize_match:
                            student_data_str = finalize_match.group(1).strip()
                            logging.info(f"      AI zażądało finalizacji i aktualizacji arkusza [{ACTION_FINALIZE}{student_data_str}]")
                            action_tag_found = True
                            should_break_loop = False # Potrzebne kolejne wywołanie AI
                            text_to_send_user = re.sub(rf"{re.escape(ACTION_FINALIZE)}.*?]", "", ai_response_text).strip()
                            parsed_student_data = {}
                            try:
                                parts = [p.strip() for p in student_data_str.split(',')]
                                if len(parts) == 4:
                                    parsed_student_data['student_first_name'] = parts[0]
                                    parsed_student_data['student_last_name'] = parts[1]
                                    parsed_student_data['grade_info'] = parts[2]
                                    parsed_student_data['level_info'] = parts[3] if parts[3].lower() != 'brak' else 'Brak'
                                    logging.info(f"      Dane ucznia sparsowane ze znacznika AI: {parsed_student_data}")
                                    psid_to_update = sender_id
                                    iso_to_update = context_data_to_save.get('proposed_slot_iso')
                                    parent_fn = context_data_to_save.get('known_parent_first_name', 'Brak (API?)')
                                    parent_ln = context_data_to_save.get('known_parent_last_name', 'Brak (API?)')
                                    sheet_row_idx = context_data_to_save.get('sheet_row_index') # Pobierz zapisany indeks

                                    if iso_to_update:
                                        start_time_obj = datetime.datetime.fromisoformat(iso_to_update)
                                        full_data_for_update = {'parent_first_name': parent_fn, 'parent_last_name': parent_ln, **parsed_student_data}
                                        update_ok, update_msg = find_row_and_update_sheet(psid_to_update, start_time_obj, full_data_for_update, sheet_row_index=sheet_row_idx)
                                        if update_ok: system_message_for_ai = "[SYSTEM_INFO: Aktualizacja Fazy 2 zakończona pomyślnie. Wyślij finalne potwierdzenie użytkownikowi.]"; context_data_to_save = {} # Wyczyść kontekst
                                        else: system_message_for_ai = f"[SYSTEM_INFO: Błąd aktualizacji Fazy 2: {update_msg}. Poinformuj użytkownika o problemie technicznym.]"
                                    else: logging.error("Brak 'proposed_slot_iso' w kontekście podczas próby aktualizacji Fazy 2."); system_message_for_ai = "[SYSTEM_INFO: Błąd wewnętrzny (brak terminu w kontekście). Poinformuj użytkownika o problemie.]"
                                else: raise ValueError(f"Nieprawidłowa liczba części w danych ucznia: {len(parts)}")
                            except Exception as parse_err: logging.error(f"Błąd parsowania danych ucznia ze znacznika '{student_data_str}': {parse_err}"); system_message_for_ai = "[SYSTEM_INFO: Błąd wewnętrzny podczas parsowania danych ucznia. Poinformuj użytkownika.]"

                        # 3. Jeśli wykonano akcję, przygotuj następne wywołanie AI
                        if system_message_for_ai:
                            logging.debug(f"      Przygotowano system_message_for_ai: {system_message_for_ai}")
                            # Dodaj wiadomość użytkownika (jeśli była w tym cyklu) i odpowiedź AI (bez znacznika akcji) do historii
                            if user_content:
                                history_for_gemini.append(user_content)
                                user_content = None # Zresetuj, bo dodane
                            if text_to_send_user: # Jeśli AI coś odpowiedziało oprócz znacznika akcji
                                 history_for_gemini.append(Content(role="model", parts=[Part.from_text(text_to_send_user)]))
                                 # Wyślij tę część odpowiedzi do użytkownika od razu
                                 send_message(sender_id, text_to_send_user)

                            # Dodaj wiadomość systemową jako input dla następnego kroku AI
                            current_user_message_text = system_message_for_ai # To będzie input dla kolejnego wywołania
                            history_for_gemini.append(Content(role="user", parts=[Part.from_text(system_message_for_ai)])) # Zapisz do historii jako user
                            # Kontynuuj pętlę, aby wywołać AI z wiadomością systemową
                            continue
                        else:
                            # Jeśli nie było akcji do wykonania przez Pythona, to odpowiedź AI jest finalna dla tego cyklu
                            should_break_loop = True

                        # 4. Jeśli pętla ma się zakończyć, wyślij ostatnią odpowiedź AI
                        if should_break_loop:
                            if text_to_send_user:
                                send_message(sender_id, text_to_send_user)
                            else:
                                if action_tag_found: logging.debug("Akcja wykonana, ale brak finalnej odpowiedzi tekstowej AI dla użytkownika.")
                                elif not action_tag_found: logging.error("AI nie zwróciło ani tekstu, ani znacznika akcji w finalnym kroku!")
                            break # Zakończ pętlę while

                    # --- Koniec pętli AI ---

                    # 5. Zapisz historię i kontekst po zakończeniu pętli
                    history_to_save = list(history_for_gemini)
                    if user_content: # Dodaj ostatnią wiadomość usera, jeśli nie została dodana w pętli
                        history_to_save.append(user_content)
                    if last_ai_response: # Dodaj ostatnią odpowiedź AI
                        history_to_save.append(last_ai_response)

                    max_hist_len = MAX_HISTORY_TURNS * 2
                    if len(history_to_save) > max_hist_len:
                        history_to_save = history_to_save[-max_hist_len:]

                    logging.info(f"Zapisywanie historii ({len(history_to_save)} wiad.)")
                    logging.debug(f"   Kontekst do zapisu: {context_data_to_save}")
                    save_history(sender_id, history_to_save, context_to_save=context_data_to_save)


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

    print("\n" + "="*60 + "\n--- START KONFIGURACJI BOTA (AI-Driven State + Two-Phase Write - EKSPERYMENTALNA) ---")
    print(f"  * Poziom logowania: {logging.getLevelName(log_level)}")
    print("-" * 60)
    print("  Konfiguracja Facebook:")
    print(f"    FB_VERIFY_TOKEN: {'OK' if VERIFY_TOKEN != 'KOLAGEN' else 'Użyto domyślny (KOLAGEN!)'}")
    if not PAGE_ACCESS_TOKEN or len(PAGE_ACCESS_TOKEN) < 50:
        print("!!! KRYTYCZNE: FB_PAGE_ACCESS_TOKEN PUSTY lub ZBYT KRÓTKI !!!")
    elif PAGE_ACCESS_TOKEN == "EACNAHFzEhkUBO5sicIUMoIwuZCZC1ZAduL8gb5sZAjWX2oErT4esklQALmstq2bkZAnWq3CVNF0IO3gZB44ip3XCXG40revvmpFKOLlC9jBStCNAwbIXZBWfawg0z0YH6GLGZCE1gFfgEF5A6DEIKbu5FYZB6XKXHECTeW6PNZAUQrPiKxrPCjbz7QFiBtGROvZCPR4rAZDZD":
        print("    FB_PAGE_ACCESS_TOKEN: Ustawiony (OK - Nowy)")
    else:
        print("    FB_PAGE_ACCESS_TOKEN: Ustawiony (OK - Inny niż domyślny)")
    print("-" * 60)
    print("  Konfiguracja Ogólna:")
    print(f"    Katalog historii: {HISTORY_DIR}")
    print(f"    Maks. tur historii AI: {MAX_HISTORY_TURNS}")
    print(f"    Limit znaków wiad. FB: {MESSAGE_CHAR_LIMIT}")
    print(f"    Opóźnienie między fragm.: {MESSAGE_DELAY_SECONDS}s")
    print(f"    Symulacja pisania: {'Włączona' if ENABLE_TYPING_DELAY else 'Wyłączona'}")
    if ENABLE_TYPING_DELAY:
        print(f"      Min/Max czas pisania: {MIN_TYPING_DELAY_SECONDS}s / {MAX_TYPING_DELAY_SECONDS}s; Prędkość: {TYPING_CHARS_PER_SECOND} zn/s")
    print("-" * 60)
    print("  Konfiguracja Vertex AI:")
    print(f"    Projekt GCP: {PROJECT_ID}")
    print(f"    Lokalizacja GCP: {LOCATION}")
    print(f"    Model AI: {MODEL_ID}")
    print(f"    Ustawienia bezpieczeństwa: {SAFETY_SETTINGS}")
    if not gemini_model:
        print("!!! OSTRZEŻENIE: Model Gemini AI NIE załadowany poprawnie! Funkcjonalność AI niedostępna. !!!")
    else:
        print(f"    Model Gemini AI ({MODEL_ID}): Załadowany (OK)")
    print("-" * 60)
    print("  Konfiguracja Google Calendar (Odczyt/Weryfikacja):")
    print(f"    ID Kalendarza (odczyt): {TARGET_CALENDAR_ID}")
    print(f"    Strefa czasowa kalendarza: {CALENDAR_TIMEZONE} (Obiekt TZ: {_get_calendar_timezone()})")
    print(f"    Czas trwania wizyty (do obliczeń): {APPOINTMENT_DURATION_MINUTES} min")
    print(f"    Godziny pracy (filtr): {WORK_START_HOUR}:00 - {WORK_END_HOUR}:00")
    print(f"    Min. wyprzedzenie (filtr): {MIN_BOOKING_LEAD_HOURS} godz.")
    print(f"    Maks. zakres szukania: {MAX_SEARCH_DAYS} dni")
    print(f"    Plik klucza Calendar API: {CALENDAR_SERVICE_ACCOUNT_FILE} ({'Znaleziono' if os.path.exists(CALENDAR_SERVICE_ACCOUNT_FILE) else 'BRAK!!!'})")
    cal_service = get_calendar_service()
    if not cal_service and os.path.exists(CALENDAR_SERVICE_ACCOUNT_FILE):
        print(f"!!! OSTRZ.: Usługa Google Calendar NIE zainicjowana mimo obecności pliku '{CALENDAR_SERVICE_ACCOUNT_FILE}'.")
    elif not os.path.exists(CALENDAR_SERVICE_ACCOUNT_FILE):
        print(f"!!! OSTRZ.: Brak pliku klucza Google Calendar '{CALENDAR_SERVICE_ACCOUNT_FILE}'.")
    elif cal_service:
        print("    Usługa Google Calendar (odczyt): Zainicjowana (OK)")
    print("-" * 60)
    print("  Konfiguracja Google Sheets (Zapis + Odczyt):")
    print(f"    ID Arkusza: {SPREADSHEET_ID}")
    print(f"    Nazwa Arkusza: {SHEET_NAME}")
    print(f"    Strefa czasowa arkusza: {SHEET_TIMEZONE} (Obiekt TZ: {_get_sheet_timezone()})")
    print(f"    Kolumny do sprawdzania/zapisu: Data={SHEET_DATE_COLUMN_INDEX}, Czas={SHEET_TIME_COLUMN_INDEX}, PSID={SHEET_PSID_COLUMN_INDEX}")
    print(f"    Plik klucza Sheets API: {SHEETS_SERVICE_ACCOUNT_FILE} ({'Znaleziono' if os.path.exists(SHEETS_SERVICE_ACCOUNT_FILE) else 'BRAK!!!'})")
    sheets_service = get_sheets_service()
    if not sheets_service and os.path.exists(SHEETS_SERVICE_ACCOUNT_FILE):
        print(f"!!! OSTRZ.: Usługa Google Sheets NIE zainicjowana mimo obecności pliku '{SHEETS_SERVICE_ACCOUNT_FILE}' (sprawdź uprawnienia API/klucza!).")
    elif not os.path.exists(SHEETS_SERVICE_ACCOUNT_FILE):
        print(f"!!! OSTRZ.: Brak pliku klucza Google Sheets '{SHEETS_SERVICE_ACCOUNT_FILE}'.")
    elif sheets_service:
        print("    Usługa Google Sheets (odczyt/zapis): Zainicjowana (OK)")
    print("--- KONIEC KONFIGURACJI BOTA ---")
    print("="*60 + "\n")

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
