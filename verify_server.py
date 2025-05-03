
# -*- coding: utf-8 -*-

# verify_server.py (Wersja: Rozdzielone Osobowości + Pełne Przełączanie Kontekstu + Sprawdzanie Arkusza w get_free_time_ranges + Dwufazowy Zapis + Poprawki)

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
PAGE_ACCESS_TOKEN = os.environ.get("FB_PAGE_ACCESS_TOKEN", "EACNAHFzEhkUBO5sicIUMoIwuZCZC1ZAduL8gb5sZAjWX2oErT4esklQALmstq2bkZAnWq3CVNF0IO3gZB44ip3XCXG40revvmpFKOLlC9jBStCNAwbIXZBWfawg0z0YH6GLGZCE1gFfgEF5A6DEIKbu5FYZB6XKXHECTeW6PNZAUQrPiKxrPCjbz7QFiBtGROvZCPR4rAZDZD")
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

# --- Znaczniki i Stany ---
INTENT_SCHEDULE_MARKER = "[INTENT_SCHEDULE]"
SLOT_ISO_MARKER_PREFIX = "[SLOT_ISO:"
SLOT_ISO_MARKER_SUFFIX = "]"
INFO_GATHERED_MARKER = "[INFO_GATHERED]"
SWITCH_TO_GENERAL = "[SWITCH_TO_GENERAL]"
RETURN_TO_PREVIOUS = "[RETURN_TO_PREVIOUS]"

STATE_GENERAL = "general"
STATE_SCHEDULING_ACTIVE = "scheduling_active"
STATE_GATHERING_INFO = "gathering_info"

# --- Ustawienia Modelu Gemini ---
GENERATION_CONFIG_SCHEDULING = GenerationConfig(
    temperature=0.5, top_p=0.95, top_k=40, max_output_tokens=512,
)
GENERATION_CONFIG_GATHERING = GenerationConfig(
    temperature=0.4, top_p=0.95, top_k=40, max_output_tokens=350,
)
GENERATION_CONFIG_DEFAULT = GenerationConfig(
    temperature=0.7, top_p=0.95, top_k=40, max_output_tokens=1024,
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
    valid_states = [STATE_GENERAL, STATE_SCHEDULING_ACTIVE, STATE_GATHERING_INFO]
    if not os.path.exists(filepath):
        logging.info(f"[{user_psid}] Plik historii nie istnieje, zwracam stan domyślny {STATE_GENERAL}.")
        return history, {'type': STATE_GENERAL}
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            history_data = json.load(f)
            if isinstance(history_data, list):
                last_system_message_index = -1
                # Szukamy ostatniego kontekstu (nadal może być przydatny do przechowywania danych)
                for i, msg_data in enumerate(reversed(history_data)):
                    if isinstance(msg_data, dict) and msg_data.get('role') == 'system': # Sprawdzamy tylko rolę
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
                            # Sprawdź typ stanu, jeśli istnieje
                            state_type = msg_data.get('type')
                            if state_type and state_type in valid_states:
                                context = msg_data
                                logging.debug(f"[{user_psid}] Odczytano AKTYWNY kontekst: {context}")
                            elif state_type: # Nieprawidłowy typ
                                logging.warning(f"[{user_psid}] Znaleziono ostatni kontekst, ale z nieprawidłowym typem: {msg_data}. Ignorowanie typu, zachowując dane.")
                                context = msg_data # Zachowaj dane, ale typ będzie domyślny
                                context['type'] = STATE_GENERAL # Ustaw domyślny typ
                            else: # Brak typu w ostatnim wpisie systemowym
                                logging.warning(f"[{user_psid}] Znaleziono ostatni kontekst systemowy, ale bez typu: {msg_data}. Ustawiam stan {STATE_GENERAL}.")
                                context = msg_data
                                context['type'] = STATE_GENERAL
                        else:
                            logging.debug(f"[{user_psid}] Pominięto stary kontekst systemowy (idx {i}): {msg_data}")
                    else:
                        logging.warning(f"Ostrz. [{user_psid}]: Pominięto niepoprawną wiadomość/kontekst (idx {i}): {msg_data}")

                # Upewnij się, że kontekst ma poprawny typ na końcu
                if not context or context.get('type') not in valid_states:
                    if not context:
                        logging.debug(f"[{user_psid}] Nie znaleziono kontekstu systemowego na końcu pliku. Ustawiam stan {STATE_GENERAL}.")
                    # Typ mógł zostać zresetowany powyżej, jeśli był nieprawidłowy
                    context['type'] = STATE_GENERAL

                logging.info(f"[{user_psid}] Wczytano historię: {len(history)} wiad. Stan: {context.get('type', STATE_GENERAL)}")
                context.pop('role', None) # Usuń rolę z kontekstu
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
        current_state_to_save = context_to_save.get('type', STATE_GENERAL) if context_to_save else STATE_GENERAL
        # Zapisujemy kontekst tylko jeśli stan jest inny niż general lub zawiera informacje o powrocie
        if context_to_save and isinstance(context_to_save, dict) and (current_state_to_save != STATE_GENERAL or 'return_to_state' in context_to_save):
             context_copy = context_to_save.copy()
             context_copy['role'] = 'system' # Dodaj rolę systemową do zapisu
             history_data.append(context_copy)
             logging.debug(f"[{user_psid}] Dodano kontekst {current_state_to_save} do zapisu: {context_copy}")
        else:
             logging.debug(f"[{user_psid}] Zapis bez kontekstu (stan general bez powrotu).")
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

def get_sheet_booked_slots(spreadsheet_id, sheet_name, start_datetime, end_datetime):
    """Pobiera listę 'zajętych' slotów z arkusza Google w danym zakresie czasowym."""
    service = get_sheets_service()
    sheet_busy_slots = []
    if not service:
        logging.error("Błąd: Usługa arkuszy niedostępna do pobrania zajętych slotów.")
        return sheet_busy_slots # Zwróć pustą listę w razie błędu

    tz = _get_sheet_timezone()
    # Upewnij się, że zakresy są świadome strefy czasowej arkusza
    if start_datetime.tzinfo is None: start_datetime_aware = tz.localize(start_datetime)
    else: start_datetime_aware = start_datetime.astimezone(tz)
    if end_datetime.tzinfo is None: end_datetime_aware = tz.localize(end_datetime)
    else: end_datetime_aware = end_datetime.astimezone(tz)

    try:
        # Określ zakres do odczytu (Data i Czas)
        date_col_letter = chr(ord('A') + SHEET_DATE_COLUMN_INDEX - 1)
        time_col_letter = chr(ord('A') + SHEET_TIME_COLUMN_INDEX - 1)
        read_range = f"{sheet_name}!{date_col_letter}2:{time_col_letter}" # Od wiersza 2 do końca

        logging.debug(f"Odczytywanie arkusza '{sheet_name}' w zakresie '{read_range}' w celu znalezienia zajętych slotów.")
        result = service.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id, range=read_range
        ).execute()
        values = result.get('values', [])

        if not values:
            logging.debug("Arkusz jest pusty lub nie zawiera danych w sprawdzanym zakresie (dla zajętych slotów).")
            return sheet_busy_slots

        duration_delta = datetime.timedelta(minutes=APPOINTMENT_DURATION_MINUTES)

        for row in values:
            if len(row) >= 2: # Potrzebujemy daty i czasu
                date_str = row[0].strip()
                time_str = row[1].strip()
                try:
                    # Spróbuj sparsować datę i czas
                    naive_date = datetime.datetime.strptime(date_str, '%Y-%m-%d').date()
                    naive_time = datetime.datetime.strptime(time_str, '%H:%M').time()
                    naive_dt = datetime.datetime.combine(naive_date, naive_time)
                    # Ustaw strefę czasową arkusza
                    slot_start = tz.localize(naive_dt)

                    # Sprawdź, czy slot wpada w nasz zakres zainteresowania
                    if start_datetime_aware <= slot_start < end_datetime_aware:
                        slot_end = slot_start + duration_delta
                        sheet_busy_slots.append({'start': slot_start, 'end': slot_end})
                        logging.debug(f"  Znaleziono zajęty slot w arkuszu: {slot_start.strftime('%Y-%m-%d %H:%M')} - {slot_end.strftime('%H:%M')}")

                except ValueError:
                    logging.warning(f"  Pominięto wiersz w arkuszu z powodu błędu parsowania daty/czasu: Data='{date_str}', Czas='{time_str}'")
                except Exception as parse_err:
                     logging.warning(f"  Pominięto wiersz w arkuszu z powodu nieoczekiwanego błędu parsowania: {parse_err} (Data='{date_str}', Czas='{time_str}')")
            else:
                logging.debug(f"  Pominięto zbyt krótki wiersz w arkuszu: {row}")

    except HttpError as error:
        logging.error(f"Błąd HTTP API podczas odczytu arkusza dla zajętych slotów: {error.resp.status} {error.resp.reason}", exc_info=True)
        # Nie zwracamy błędu, po prostu lista będzie niekompletna
    except Exception as e:
        logging.error(f"Nieoczekiwany błąd podczas odczytu arkusza dla zajętych slotów: {e}", exc_info=True)
        # Nie zwracamy błędu

    logging.info(f"Znaleziono {len(sheet_busy_slots)} potencjalnie zajętych slotów w arkuszu.")
    return sheet_busy_slots


def get_free_time_ranges(calendar_id, start_datetime, end_datetime):
    """Pobiera listę wolnych zakresów czasowych z kalendarza ORAZ arkusza, filtrując je."""
    service_cal = get_calendar_service() # Usługa kalendarza
    tz = _get_calendar_timezone()
    if not service_cal:
        logging.error("Błąd: Usługa kalendarza niedostępna do pobrania wolnych terminów.")
        return [] # Nie możemy kontynuować bez kalendarza

    # Upewnij się, że daty są świadome strefy czasowej kalendarza
    if start_datetime.tzinfo is None: start_datetime = tz.localize(start_datetime)
    else: start_datetime = start_datetime.astimezone(tz)
    if end_datetime.tzinfo is None: end_datetime = tz.localize(end_datetime)
    else: end_datetime = end_datetime.astimezone(tz)

    now = datetime.datetime.now(tz)
    start_datetime = max(start_datetime, now)
    if start_datetime >= end_datetime:
        logging.info(f"Zakres wyszukiwania [{start_datetime:%Y-%m-%d %H:%M} - {end_datetime:%Y-%m-%d %H:%M}] jest nieprawidłowy lub całkowicie w przeszłości.")
        return []

    logging.info(f"Szukanie wolnych zakresów w '{calendar_id}' ORAZ arkuszu od {start_datetime:%Y-%m-%d %H:%M %Z} do {end_datetime:%Y-%m-%d %H:%M %Z}")

    # --- Krok 1: Pobierz zajęte sloty z Kalendarza Google ---
    busy_times_calendar = []
    try:
        body = {"timeMin": start_datetime.isoformat(), "timeMax": end_datetime.isoformat(), "timeZone": CALENDAR_TIMEZONE, "items": [{"id": calendar_id}]}
        freebusy_result = service_cal.freebusy().query(body=body).execute()
        calendar_data = freebusy_result.get('calendars', {}).get(calendar_id, {})
        if 'errors' in calendar_data:
             for error in calendar_data['errors']: logging.error(f"Błąd API Freebusy dla kalendarza {calendar_id}: {error.get('reason')} - {error.get('message')}")
             if any(e.get('reason') == 'notFound' or e.get('reason') == 'forbidden' for e in calendar_data['errors']): return [] # Krytyczny błąd kalendarza
        busy_times_raw = calendar_data.get('busy', [])
        for busy_slot in busy_times_raw:
            start_str = busy_slot.get('start'); end_str = busy_slot.get('end')
            if isinstance(start_str, str) and isinstance(end_str, str):
                busy_start = parse_event_time({'dateTime': start_str}, tz); busy_end = parse_event_time({'dateTime': end_str}, tz)
                if busy_start and busy_end and busy_start < busy_end:
                    busy_start_clipped = max(busy_start, start_datetime); busy_end_clipped = min(busy_end, end_datetime)
                    if busy_start_clipped < busy_end_clipped: busy_times_calendar.append({'start': busy_start_clipped, 'end': busy_end_clipped})
                else: logging.warning(f"Ostrz.: Pominięto nieprawidłowy lub niesparsowany zajęty czas z kalendarza: start={start_str}, end={end_str}")
            else: logging.warning(f"Ostrz.: Pominięto zajęty slot z kalendarza o nieoczekiwanej strukturze danych: {busy_slot}")
    except HttpError as error: logging.error(f'Błąd HTTP API Freebusy: {error.resp.status} {error.resp.reason}', exc_info=True); return []
    except Exception as e: logging.error(f"Nieoczekiwany błąd podczas zapytania Freebusy: {e}", exc_info=True); return []
    logging.info(f"Znaleziono {len(busy_times_calendar)} zajętych slotów w Kalendarzu Google.")

    # --- Krok 2: Pobierz zajęte sloty z Arkusza Google ---
    busy_times_sheet = get_sheet_booked_slots(SPREADSHEET_ID, SHEET_NAME, start_datetime, end_datetime)
    # Upewnij się, że sloty z arkusza są w tej samej strefie czasowej co kalendarzowe (na potrzeby sortowania/łączenia)
    busy_times_sheet_cal_tz = []
    for slot in busy_times_sheet:
        try:
            start_cal_tz = slot['start'].astimezone(tz)
            end_cal_tz = slot['end'].astimezone(tz)
            busy_times_sheet_cal_tz.append({'start': start_cal_tz, 'end': end_cal_tz})
        except Exception as tz_err:
            logging.warning(f"Błąd konwersji strefy czasowej dla slotu z arkusza {slot}: {tz_err}")


    # --- Krok 3: Połącz, posortuj i złącz wszystkie zajęte sloty ---
    all_busy_times = busy_times_calendar + busy_times_sheet_cal_tz
    if not all_busy_times:
        logging.info("Brak zajętych slotów w kalendarzu i arkuszu. Cały zakres jest potencjalnie wolny (z uwzględnieniem filtrów).")
    else:
        all_busy_times.sort(key=lambda x: x['start'])
        logging.debug(f"Łączna liczba zajętych slotów (Kalendarz + Arkusz) przed złączeniem: {len(all_busy_times)}")

    merged_busy_times = []
    for busy in all_busy_times:
        # Sprawdź poprawność typu przed dostępem do kluczy
        if not isinstance(busy, dict) or 'start' not in busy or 'end' not in busy:
            logging.warning(f"Pominięto nieprawidłowy wpis w all_busy_times: {busy}")
            continue
        if not merged_busy_times or busy['start'] > merged_busy_times[-1]['end']:
            merged_busy_times.append(busy)
        else:
            merged_busy_times[-1]['end'] = max(merged_busy_times[-1]['end'], busy['end'])
    logging.info(f"Liczba zajętych slotów po złączeniu: {len(merged_busy_times)}")

    # --- Krok 4: Oblicz wolne zakresy (odwrócenie zajętych) ---
    free_ranges = []
    current_time = start_datetime
    for busy_slot in merged_busy_times:
        if current_time < busy_slot['start']:
            free_ranges.append({'start': current_time, 'end': busy_slot['start']})
        current_time = max(current_time, busy_slot['end'])
    if current_time < end_datetime:
        free_ranges.append({'start': current_time, 'end': end_datetime})

    # --- Krok 5 i 6: Filtruj wg godzin pracy i wyprzedzenia (bez zmian) ---
    intermediate_free_slots = []
    min_duration_delta = datetime.timedelta(minutes=APPOINTMENT_DURATION_MINUTES)
    for free_range in free_ranges:
        range_start = free_range['start']; range_end = free_range['end']; current_segment_start = range_start
        while current_segment_start < range_end:
            day_date = current_segment_start.date()
            work_day_start = tz.localize(datetime.datetime.combine(day_date, datetime.time(WORK_START_HOUR, 0)))
            work_day_end = tz.localize(datetime.datetime.combine(day_date, datetime.time(WORK_END_HOUR, 0)))
            effective_start = max(current_segment_start, work_day_start); effective_end = min(range_end, work_day_end)
            if effective_start < effective_end and (effective_end - effective_start) >= min_duration_delta:
                rounded_start = effective_start
                if rounded_start < effective_end and (effective_end - rounded_start) >= min_duration_delta: intermediate_free_slots.append({'start': rounded_start, 'end': effective_end})
            next_day_start = tz.localize(datetime.datetime.combine(day_date + datetime.timedelta(days=1), datetime.time(0,0)))
            current_segment_start = max(work_day_end, next_day_start); current_segment_start = max(current_segment_start, range_start)

    final_filtered_slots = []; min_start_time = now + datetime.timedelta(hours=MIN_BOOKING_LEAD_HOURS)
    logging.debug(f"Minimalny czas startu po filtrze {MIN_BOOKING_LEAD_HOURS}h: {min_start_time:%Y-%m-%d %H:%M %Z}")
    for slot in intermediate_free_slots:
        original_start = slot['start']; original_end = slot['end']
        if original_start >= min_start_time:
            if (original_end - original_start) >= min_duration_delta: final_filtered_slots.append(slot)
        elif original_end > min_start_time:
            adjusted_start = min_start_time
            if (original_end - adjusted_start) >= min_duration_delta:
                final_filtered_slots.append({'start': adjusted_start, 'end': original_end})
                logging.debug(f"Zmodyfikowano slot {original_start:%H:%M}-{original_end:%H:%M} na {adjusted_start:%H:%M}-{original_end:%H:%M} z powodu reguły {MIN_BOOKING_LEAD_HOURS}h.")

    logging.info(f"Znaleziono {len(final_filtered_slots)} wolnych zakresów (po filtrach Kalendarza, Arkusza, godzin pracy i {MIN_BOOKING_LEAD_HOURS}h wyprzedzenia).")
    for i, slot in enumerate(final_filtered_slots[:5]): logging.debug(f"  Finalny Slot {i+1}: {slot['start']:%Y-%m-%d %H:%M %Z} - {slot['end']:%Y-%m-%d %H:%M %Z}")
    if len(final_filtered_slots) > 5: logging.debug("  ...")

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
    """Formatuje listę zakresów czasowych na bardziej techniczny tekst dla AI, ograniczając liczbę."""
    if not ranges:
        return "Brak dostępnych zakresów czasowych w podanym okresie."

    tz = _get_calendar_timezone()
    formatted_lines = [
        f"Dostępne ZAKRESY czasowe (wizyta trwa {APPOINTMENT_DURATION_MINUTES} minut).", # Uproszczono nagłówek
        "--- Dostępne Zakresy (Data YYYY-MM-DD, Dzień, Od Godziny HH:MM, Do Godziny HH:MM) ---"
    ]
    slots_added = 0
    # Zmniejszamy limit, aby prompt był krótszy
    max_slots_to_show = 15 # <<< ZMNIEJSZONO LIMIT
    sorted_ranges = sorted(ranges, key=lambda r: r['start'])

    for r in sorted_ranges:
        start_dt = r['start'].astimezone(tz)
        end_dt = r['end'].astimezone(tz)
        try: day_name = start_dt.strftime('%A').capitalize()
        except Exception: day_name = POLISH_WEEKDAYS[start_dt.weekday()]
        date_str = start_dt.strftime('%Y-%m-%d')
        start_time_str = start_dt.strftime('%H:%M')
        end_time_str = end_dt.strftime('%H:%M')

        if start_dt < end_dt:
            formatted_lines.append(f"- {date_str}, {day_name}, od {start_time_str}, do {end_time_str}")
            slots_added += 1
            if slots_added >= max_slots_to_show:
                formatted_lines.append("- ... (i potencjalnie więcej w dalszych dniach)") # Zmieniono tekst
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
    response = None # Zmienna do przechowywania ostatniej odpowiedzi
    candidate = None # Zmienna do przechowywania ostatniego kandydata

    while attempt < max_retries:
        attempt += 1
        logging.debug(f"    Próba {attempt}/{max_retries} wywołania Gemini ({task_name})...")
        try:
            _simulate_typing(user_psid, MIN_TYPING_DELAY_SECONDS * 0.8)
            # Dodano stream=False
            response = gemini_model.generate_content(
                prompt_history,
                generation_config=generation_config,
                safety_settings=SAFETY_SETTINGS,
                stream=False
            )
            if response and response.candidates:
                # Sprawdź, czy jest co najmniej jeden kandydat
                if not response.candidates:
                    logging.warning(f"[{user_psid}] Gemini ({task_name}) zwróciło odpowiedź bez kandydatów.")
                    if attempt < max_retries: time.sleep(1.5 * attempt); continue
                    else: return "Przepraszam, wystąpił problem z generowaniem odpowiedzi (brak kandydatów)."

                candidate = response.candidates[0]
                finish_reason = candidate.finish_reason # Zapisz ostatni powód

                # Sprawdź finish_reason
                if finish_reason != 1: # 1 = STOP
                    safety_ratings = candidate.safety_ratings
                    logging.warning(f"[{user_psid}] Gemini ({task_name}) ZAKOŃCZONE NIEPRAWIDŁOWO! Powód: {finish_reason.name} ({finish_reason.value}). Safety Ratings: {safety_ratings}")
                    if finish_reason in [3, 4] and attempt < max_retries: # SAFETY lub RECITATION
                        logging.warning(f"    Ponawianie ({attempt}/{max_retries}) z powodu blokady...")
                        time.sleep(1.5 * attempt)
                        continue
                    else:
                        logging.error(f"!!! [{user_psid}] Gemini ({task_name}) nieudane po blokadzie lub innym błędzie.")
                        if finish_reason == 3: return "Przepraszam, nie mogę przetworzyć tej prośby ze względu na zasady bezpieczeństwa."
                        else: return "Przepraszam, wystąpił nieoczekiwany problem z generowaniem odpowiedzi."

                # Sprawdź content (nawet jeśli finish_reason=STOP)
                if candidate.content and candidate.content.parts:
                    generated_text = "".join(part.text for part in candidate.content.parts if hasattr(part, 'text')).strip()
                    if generated_text:
                        logging.info(f"[{user_psid}] Gemini ({task_name}) zwróciło odpowiedź (długość: {len(generated_text)}).")
                        logging.debug(f"    Pełna odpowiedź Gemini ({task_name}): '{generated_text}'")
                        return generated_text # Sukces
                    else:
                        # Pusty content mimo finish_reason=STOP
                        logging.warning(f"[{user_psid}] Gemini ({task_name}) zwróciło kandydata z pustą treścią (Finish Reason: {finish_reason.name}).")
                        if attempt < max_retries: time.sleep(1.5 * attempt); continue
                        else: logging.error(f"!!! [{user_psid}] Gemini ({task_name}) zwróciło pustą treść po {max_retries} próbach."); return "Przepraszam, wystąpił problem z wygenerowaniem odpowiedzi (pusta treść)."
                else:
                    # Brak contentu w kandydacie
                    logging.warning(f"[{user_psid}] Gemini ({task_name}) zwróciło kandydata bez treści (content/parts). Finish Reason: {finish_reason.name}")
                    if attempt < max_retries: time.sleep(1.5 * attempt); continue
                    else: logging.error(f"!!! [{user_psid}] Gemini ({task_name}) zwróciło kandydata bez treści po {max_retries} próbach."); return "Przepraszam, wystąpił problem z wygenerowaniem odpowiedzi (brak treści)."
            else:
                # Odpowiedź bez kandydatów
                prompt_feedback = response.prompt_feedback if hasattr(response, 'prompt_feedback') else 'Brak informacji zwrotnej'
                logging.error(f"!!! BŁĄD [{user_psid}] Gemini ({task_name}) - Brak kandydatów w odpowiedzi (ponownie). Feedback: {prompt_feedback}.")
                if attempt < max_retries: time.sleep(1.5 * attempt); continue
                else: return "Przepraszam, wystąpił problem z generowaniem odpowiedzi (brak kandydatów)."

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
                 break # Nie ponawiaj innych błędów HTTP
        except Exception as e:
             if isinstance(e, NameError) and 'gemini_model' in str(e):
                 logging.critical(f"!!! KRYTYCZNY NameError [{user_psid}] w _call_gemini: {e}. gemini_model jest None!", exc_info=True)
                 return None
             else:
                 logging.error(f"!!! BŁĄD [{user_psid}] Gemini ({task_name}) - Nieoczekiwany błąd Python (Próba {attempt}/{max_retries}): {e}", exc_info=True)
                 break # Nie ponawiaj nieznanych błędów Pythona

        # Jeśli doszło tutaj, oznacza to błąd inny niż HTTP lub brak poprawnej odpowiedzi
        if attempt < max_retries:
            logging.warning(f"    Problem z odpowiedzią Gemini ({task_name}). Oczekiwanie przed ponowieniem ({attempt+1}/{max_retries})...")
            time.sleep(1.5 * attempt)

    # Po wszystkich próbach
    logging.error(f"!!! KRYTYCZNY BŁĄD [{user_psid}] Gemini ({task_name}) - Nie udało się uzyskać poprawnej odpowiedzi po {max_retries} próbach.")
    # Zwróć None lub specyficzną wiadomość błędu, jeśli ostatni błąd to SAFETY
    if finish_reason == 3:
        return "Przepraszam, nie mogę przetworzyć tej prośby ze względu na zasady bezpieczeństwa."
    # Jeśli ostatni błąd to pusty content, zwróć generyczny błąd
    # Sprawdzenie response i candidate jest potrzebne, bo mogły nie zostać przypisane przy błędzie HTTP/Exception
    if finish_reason == 1 and not (response and response.candidates and candidate and candidate.content and candidate.content.parts):
         return "Przepraszam, wystąpił problem z wygenerowaniem odpowiedzi."
    return None # Ogólny błąd po wszystkich próbach

# =====================================================================
# === INSTRUKCJE SYSTEMOWE I GŁÓWNE FUNKCJE AI ========================
# =====================================================================

# --- SYSTEM_INSTRUCTION_SCHEDULING (Wersja z pytaniem o preferencje i obsługą pytań ogólnych) ---
SYSTEM_INSTRUCTION_SCHEDULING = """Jesteś pomocnym asystentem AI specjalizującym się w umawianiu terminów korepetycji online. Twoim zadaniem jest znalezienie pasującego terminu dla użytkownika na podstawie jego preferencji oraz dostarczonej listy dostępnych zakresów czasowych z kalendarza.

**Kontekst:**
*   Rozmawiasz z użytkownikiem, który wyraził chęć umówienia się na lekcję.
*   Poniżej znajduje się lista AKTUALNIE dostępnych ZAKRESÓW czasowych z kalendarza, w których można umówić wizytę (każda trwa {duration} minut). **Wszystkie podane zakresy są już odpowiednio odsunięte w czasie (filtr {min_lead_hours}h) i gotowe do zaproponowania.**
*   Masz dostęp do historii poprzedniej rozmowy. Czasami rozmowa mogła zostać przerwana pytaniem ogólnym i teraz do niej wracamy.

**Styl pisania:**
*   Używaj zwrotów typu "Państwo".
*   Unikaj zbyt entuzjastycznych wiadomości i wykrzykników.
*   Zwracaj uwagę na ortografię i interpunkcję.
*   Proponuj terminy w formie pytania, np. "Czy odpowiadałby Państwu termin w najbliższy wtorek o 17:00?".

**Dostępne zakresy czasowe z kalendarza:**
{available_ranges_text}

**Twoje zadanie:**
1. **Zacznij rozmowę od zaproponowania terminu** Zaproponuj konretny termin i spytaj czy odpowiada.
2.  **Negocjuj:** Na podstawie odpowiedzi użytkownika **dotyczącej preferencji terminu**, historii konwersacji i **wyłącznie dostępnych zakresów z listy**, kontynuuj rozmowę, aby znaleźć termin pasujący obu stronom. Gdy użytkownik poda preferencje, **zaproponuj konkretny termin z listy**, który im odpowiada (np. "W takim razie, może środa o 17:00?"). Jeśli ostatnia wiadomość użytkownika nie była odpowiedzią na pytanie o termin, wróć do kroku 1.
3.  **Potwierdź i dodaj znacznik:** Kiedy wspólnie ustalicie **dokładny termin** (np. "Środa, 15 maja o 18:30"), który **znajduje się na liście dostępnych zakresów**, potwierdź go w swojej odpowiedzi (np. "Świetnie, w takim razie proponowany termin to środa, 15 maja o 18:30.") i **zakończ swoją odpowiedź potwierdzającą DOKŁADNIE znacznikiem** `{slot_marker_prefix}YYYY-MM-DDTHH:MM:SS{slot_marker_suffix}`. Użyj formatu ISO 8601 dla ustalonego czasu rozpoczęcia (np. 2024-05-15T18:30:00). Upewnij się, że data i godzina w znaczniku są poprawne, zgodne z ustaleniami i **pochodzą z listy dostępnych zakresów**.
4.  **NIE dodawaj znacznika**, jeśli:
    *   Użytkownik jeszcze się zastanawia lub prosi o więcej opcji.
    *   Użytkownik proponuje termin, którego nie ma na liście dostępnych zakresów.
    *   Nie udało się znaleźć pasującego terminu.
    *   Lista dostępnych zakresów jest pusta.
5.  **Brak terminów:** Jeśli lista zakresów jest pusta lub po rozmowie okaże się, że żaden termin nie pasuje, poinformuj o tym użytkownika uprzejmie. Nie dodawaj znacznika.
6.  **Pytania poza tematem:** Jeśli użytkownik zada pytanie niezwiązane bezpośrednio z ustalaniem terminu (np. o cenę, metodykę, dostępne przedmioty), **NIE ODPOWIADAJ na nie**. Zamiast tego, Twoja odpowiedź musi zawierać **TYLKO I WYŁĄCZNIE** znacznik: `{switch_marker}`. System przełączy się wtedy do trybu ogólnych odpowiedzi.

**Pamiętaj:**
*   Trzymaj się **wyłącznie** terminów i godzin wynikających z "Dostępnych zakresów czasowych".
*   Bądź elastyczny w rozmowie, ale propozycje muszą pochodzić z listy.
*   Używaj języka polskiego i polskiej strefy czasowej ({calendar_timezone}).
*   Znacznik `{slot_marker_prefix}...{slot_marker_suffix}` jest sygnałem dla systemu, że **osiągnięto porozumienie co do terminu z dostępnej listy**. Używaj go tylko w tym jednym, konkretnym przypadku.
*   Znacznik `{switch_marker}` służy do przekazania obsługi pytania ogólnego.
*   Wybieraj terminy w najbliższych dniach im szybciej tym lepiej
*   Kieruj się tez preferencjami ludzi, większość osób w tygodniu woli godziny po 14:00 a w weekend po 9:00
""".format(
    duration=APPOINTMENT_DURATION_MINUTES, min_lead_hours=MIN_BOOKING_LEAD_HOURS,
    available_ranges_text="{available_ranges_text}", calendar_timezone=CALENDAR_TIMEZONE,
    slot_marker_prefix=SLOT_ISO_MARKER_PREFIX, slot_marker_suffix=SLOT_ISO_MARKER_SUFFIX,
    switch_marker=SWITCH_TO_GENERAL
)

# --- ZMODYFIKOWANA INSTRUKCJA GATHERING (AI potwierdza dane w strukturze i obsługa pytań ogólnych) ---
SYSTEM_INSTRUCTION_GATHERING = """Twoim zadaniem jest zebranie informacji wyłącznie o UCZNIU, potrzebnych do zapisu na korepetycje, po tym jak wstępnie ustalono termin. Dane rodzica zostaną pobrane automatycznie przez system.

**Kontekst:**
*   Wstępnie ustalony termin lekcji to: {proposed_slot_formatted}
*   Masz dostęp do historii rozmowy.
*   Informacje o UCZNIU już znane (mogą być puste):
    *   Imię ucznia: {known_student_first_name}
    *   Nazwisko ucznia: {known_student_last_name}
    *   Klasa/Szkoła: {known_grade} # Pełna informacja, np. "3 klasa liceum"
    *   Poziom (dla liceum/technikum): {known_level} # Np. "Podstawowy", "Rozszerzony" lub "Brak"

**Twoje zadania:**
1.  **Przeanalizuj znane informacje o UCZNIU:** Sprawdź powyższe "Informacje o UCZNIU już znane" oraz historię rozmowy.
2.  **Zapytaj o BRAKUJĄCE informacje dotyczące WYŁĄCZNIE UCZNIA:** Uprzejmie poproś użytkownika o podanie **tylko tych informacji o uczniu, których jeszcze brakuje**. Wymagane informacje to:
    *   **Pełne Imię i Nazwisko UCZNIA**.
    *   **Klasa**, do której uczęszcza uczeń ORAZ **typ szkoły** (np. "7 klasa podstawówki", "1 klasa liceum", "3 klasa technikum"). Poproś o podanie obu informacji, jeśli brakuje.
    *   **Poziom nauczania** (podstawowy czy rozszerzony) - **zapytaj o to TYLKO jeśli z podanej klasy/szkoły wynika, że jest to liceum lub technikum**.
3.  **IGNORUJ i NIE PYTAJ o dane rodzica.** System zajmie się tym automatycznie.
4.  **Prowadź rozmowę:** Zadawaj pytania dotyczące ucznia pojedynczo lub połącz kilka, jeśli brakuje więcej danych. Bądź miły i konwersacyjny. Potwierdzaj zrozumienie odpowiedzi użytkownika.
5.  **Zakończ po zebraniu danych UCZNIA:** Kiedy uznasz, że masz już **wszystkie wymagane informacje o UCZNIU** (Imię, Nazwisko, Klasa+Szkoła, ewentualnie Poziom), Twoja ostatnia odpowiedź **MUSI** mieć następującą strukturę:
    a)  **DOKŁADNIE** linijka w formacie: `ZEBRANE_DANE_UCZNIA: [Imię: <imię>, Nazwisko: <nazwisko>, KlasaInfo: <pełna informacja o klasie i szkole np. 3 klasa liceum>, Poziom: <Podstawowy/Rozszerzony/brak>]` (Zastąp <...> zebranymi danymi. Jeśli poziom nie dotyczy lub nie został podany, wpisz "brak").
    b)  **PO TEJ LINIJCE**, w nowej linii, standardowa wiadomość dla użytkownika: "Dziękuję za wszystkie informacje. Dane zostały zapisane. Wkrótce skontaktujemy się w celu potwierdzenia szczegółów. Proszę również oczekiwać na wiadomość dotyczącą płatności i dostępu do materiałów na profilu dedykowanym do komunikacji: https://www.facebook.com/profile.php?id=61576135251276"
    c)  **NA SAMYM KOŃCU** całej odpowiedzi dodaj **DOKŁADNIE** znacznik: `{info_gathered_marker}`.
6.  **NIE dodawaj znacznika ani struktury `ZEBRANE_DANE_UCZNIA:`**, jeśli nadal brakuje którejś z wymaganych informacji o uczniu. Kontynuuj zadawanie pytań.
7.  **Pytania poza tematem:** Jeśli użytkownik zada pytanie niezwiązane bezpośrednio ze zbieraniem danych ucznia (np. o cenę, metodykę), **NIE ODPOWIADAJ na nie**. Zamiast tego, Twoja odpowiedź musi zawierać **TYLKO I WYŁĄCZNIE** znacznik: `{switch_marker}`. System przełączy się wtedy do trybu ogólnych odpowiedzi.

**Przykład poprawnej odpowiedzi końcowej:**
```
ZEBRANE_DANE_UCZNIA: [Imię: Jan, Nazwisko: Kowalski, KlasaInfo: 2 klasa liceum, Poziom: Rozszerzony]
Dziękuję za wszystkie informacje. Dane zostały zapisane. Wkrótce skontaktujemy się w celu potwierdzenia szczegółów. Proszę również oczekiwać na wiadomość dotyczącą płatności i dostępu do materiałów na profilu dedykowanym do komunikacji: https://www.facebook.com/profile.php?id=61576135251276[INFO_GATHERED]
```

**Pamiętaj:** Kluczowe jest dokładne przestrzeganie formatu `ZEBRANE_DANE_UCZNIA: [...]` w przedostatniej linijce odpowiedzi końcowej. Znacznik `{switch_marker}` służy do przekazania obsługi pytania ogólnego.
""".format(
    proposed_slot_formatted="{proposed_slot_formatted}",
    known_student_first_name="{known_student_first_name}",
    known_student_last_name="{known_student_last_name}",
    known_grade="{known_grade}",
    known_level="{known_level}",
    info_gathered_marker=INFO_GATHERED_MARKER,
    switch_marker=SWITCH_TO_GENERAL
)

# --- ZMODYFIKOWANA INSTRUKCJA GENERAL (z obsługą powrotu) ---
SYSTEM_INSTRUCTION_GENERAL = """Jesteś przyjaznym i pomocnym asystentem klienta w 'Zakręcone Korepetycje'. Prowadzisz rozmowę na czacie dotyczącą korepetycji online.
**Twoje główne zadania:**
1.  Odpowiadaj rzeczowo i uprzejmie na pytania użytkownika dotyczące oferty, metodyki, dostępności korepetycji.
2.  Utrzymuj konwersacyjny, pomocny ton. Odpowiadaj po polsku.
3.  **Kluczowy cel:** Jeśli w wypowiedzi użytkownika **wyraźnie pojawi się intencja umówienia się na lekcję** (próbną lub zwykłą), rezerwacji terminu, zapytanie o wolne terminy lub chęć rozpoczęcia współpracy, **dodaj na samym końcu swojej odpowiedzi specjalny znacznik:** `{intent_marker}`.
4.  **Obsługa powrotu:** Jeśli zostałeś aktywowany, aby odpowiedzieć na pytanie ogólne podczas innego procesu (np. umawiania terminu), a odpowiedź użytkownika na Twoją odpowiedź wydaje się satysfakcjonująca (np. zawiera "ok", "dziękuję", "rozumiem") i **nie zawiera kolejnego pytania ogólnego**, dodaj na **samym końcu** swojej odpowiedzi (po ewentualnym podziękowaniu) **DOKŁADNIE** znacznik: `{return_marker}`. Jeśli użytkownik zada kolejne pytanie ogólne, odpowiedz na nie normalnie, bez tego znacznika.
5. Koszt zajęć to 60zł dla podstawówki i 75 dla szkoły średniej
**Przykłady wypowiedzi użytkownika, które powinny skutkować dodaniem znacznika `{intent_marker}`:**
*   "Chciałbym się umówić na lekcję próbną."
*   "Kiedy moglibyśmy zacząć?"
*   "Proszę zaproponować jakiś termin."
*   "Czy macie jakieś wolne godziny w przyszłym tygodniu?"
*   "Jak mogę zarezerwować korepetycje?"
*   "Interesuje mnie ta oferta, jak się umówić?"
*   Pytanie typu: "Ile trwa lekcja i kiedy można ją umówić?" -> Odpowiedz na pierwszą część pytania i dodaj znacznik.
**Przykłady wypowiedzi, po których NIE dodawać znacznika `{intent_marker}`:**
*   "Ile kosztują korepetycje?" (Odpowiedz zgodnie z punktem 5, bez znacznika).
*   "Jakie przedmioty oferujecie?" (Odpowiedz na pytanie, bez znacznika).
*   "Dziękuję za informacje." (Podziękuj, bez znacznika).
**Przykład odpowiedzi ze znacznikiem powrotu `{return_marker}`:**
    *   User: "Dziękuję za wyjaśnienie ceny." -> Model: "Cieszę się, że mogłem pomóc.{return_marker}"
    *   User: "Ok, rozumiem." -> Model: "Świetnie.{return_marker}"
    *   User: "Super." -> Model: "W porządku.{return_marker}"

**Zasady:** Zawsze odpowiadaj na bieżące pytanie lub stwierdzenie użytkownika. Znacznik `{intent_marker}` dodawaj **tylko wtedy**, gdy intencja umówienia się jest jasna i bezpośrednia, i **zawsze na samym końcu** odpowiedzi. Nie inicjuj samodzielnie procesu umawiania. Znacznik `{return_marker}` dodawaj tylko w sytuacji opisanej w punkcie 4.
""".format(
    intent_marker=INTENT_SCHEDULE_MARKER,
    return_marker=RETURN_TO_PREVIOUS
)


# --- Funkcja AI: Planowanie terminu ---
def get_gemini_scheduling_response(user_psid, history_for_scheduling_ai, current_user_message_text, available_ranges):
    """Prowadzi rozmowę planującą z AI, używając dostępnych zakresów, zwraca odpowiedź AI."""
    if not gemini_model:
        logging.error(f"!!! [{user_psid}] Model Gemini niezaładowany (Scheduling)!")
        return "Przepraszam, mam problem z systemem planowania."
    ranges_text = format_ranges_for_ai(available_ranges)
    try:
        # Używamy przywróconej instrukcji
        system_instruction = SYSTEM_INSTRUCTION_SCHEDULING.format(available_ranges_text=ranges_text)
    except KeyError as e:
        logging.error(f"!!! BŁĄD formatowania instrukcji AI (Scheduling): Brak klucza {e}")
        return "Błąd konfiguracji asystenta planowania."
    except Exception as format_e:
        logging.error(f"!!! BŁĄD nieoczekiwany formatowania instrukcji AI (Scheduling): {format_e}")
        return "Błąd wewnętrzny konfiguracji asystenta planowania."
    initial_prompt = [
        Content(role="user", parts=[Part.from_text(system_instruction)]),
        # Przywrócona odpowiedź modelu
        Content(role="model", parts=[Part.from_text(f"Rozumiem. Zapytam o preferencje, a następnie zaproponuję konkretny termin z listy i będę negocjować. Znacznik {SLOT_ISO_MARKER_PREFIX}...{SLOT_ISO_MARKER_SUFFIX} dodam tylko po uzyskaniu ostatecznej zgody na termin z listy. Jeśli użytkownik zada pytanie ogólne, odpowiem tylko znacznikiem {SWITCH_TO_GENERAL}.")])
    ]
    full_prompt = initial_prompt + history_for_scheduling_ai
    if current_user_message_text:
        full_prompt.append(Content(role="user", parts=[Part.from_text(current_user_message_text)]))
    max_prompt_messages = (MAX_HISTORY_TURNS * 2) + 2
    while len(full_prompt) > max_prompt_messages:
        full_prompt.pop(2)
        if len(full_prompt) > 2:
            full_prompt.pop(2)
    # Wywołanie _call_gemini pozostaje bez zmian tutaj
    response_text = _call_gemini(user_psid, full_prompt, GENERATION_CONFIG_SCHEDULING, "Scheduling Conversation")
    if response_text:
        # Nie usuwamy już tutaj SWITCH_TO_GENERAL, bo jest potrzebny w logice webhooka
        if INTENT_SCHEDULE_MARKER in response_text:
            response_text = response_text.replace(INTENT_SCHEDULE_MARKER, "").strip()
        if INFO_GATHERED_MARKER in response_text:
            response_text = response_text.replace(INFO_GATHERED_MARKER, "").strip()
        return response_text
    else:
        logging.error(f"!!! [{user_psid}] Nie uzyskano odpowiedzi Gemini (Scheduling).")
        # Zwróć wiadomość o błędzie, jeśli AI nie odpowiedziało
        return "Przepraszam, wystąpił błąd podczas sprawdzania terminów. Spróbujmy ponownie za chwilę."

# --- Funkcja AI: Zbieranie informacji (AI ignoruje dane rodzica) ---
def get_gemini_gathering_response(user_psid, history_for_gathering_ai, current_user_message_text, context_info):
    """Prowadzi rozmowę zbierającą informacje WYŁĄCZNIE o uczniu."""
    if not gemini_model:
        logging.error(f"!!! [{user_psid}] Model Gemini niezaładowany (Gathering Info)!")
        return "Przepraszam, mam problem z systemem."
    proposed_slot_str = context_info.get("proposed_slot_formatted", "nie ustalono")
    student_first_name = context_info.get("known_student_first_name", "")
    student_last_name = context_info.get("known_student_last_name", "")
    grade = context_info.get("known_grade", "")
    level = context_info.get("known_level", "")
    try:
        system_instruction = SYSTEM_INSTRUCTION_GATHERING.format(
            proposed_slot_formatted=proposed_slot_str,
            known_student_first_name=student_first_name,
            known_student_last_name=student_last_name,
            known_grade=grade,
            known_level=level
        )
    except KeyError as e:
        logging.error(f"!!! BŁĄD formatowania instrukcji AI (Gathering): Brak klucza {e}")
        return "Błąd konfiguracji asystenta zbierania informacji."
    initial_prompt = [
        Content(role="user", parts=[Part.from_text(system_instruction)]),
        Content(role="model", parts=[Part.from_text(f"Rozumiem. Sprawdzę znane informacje o uczniu i zapytam o brakujące dane: Imię/Nazwisko Ucznia, Klasa, Poziom (dla liceum/technikum). Zignoruję dane rodzica. Po zebraniu kompletu informacji o uczniu zwrócę strukturę ZEBRANE_DANE_UCZNIA i znacznik {INFO_GATHERED_MARKER}. Jeśli użytkownik zada pytanie ogólne, odpowiem tylko znacznikiem {SWITCH_TO_GENERAL}.")])
    ]
    full_prompt = initial_prompt + history_for_gathering_ai
    if current_user_message_text:
        full_prompt.append(Content(role="user", parts=[Part.from_text(current_user_message_text)]))
    max_prompt_messages = (MAX_HISTORY_TURNS * 2) + 2
    while len(full_prompt) > max_prompt_messages:
        full_prompt.pop(2)
        if len(full_prompt) > 2:
            full_prompt.pop(2)
    response_text = _call_gemini(user_psid, full_prompt, GENERATION_CONFIG_GATHERING, "Info Gathering (Student Only)")
    if response_text:
        # Nie usuwamy już tutaj SWITCH_TO_GENERAL ani INFO_GATHERED_MARKER
        if INTENT_SCHEDULE_MARKER in response_text:
            response_text = response_text.replace(INTENT_SCHEDULE_MARKER, "").strip()
        if SLOT_ISO_MARKER_PREFIX in response_text:
            response_text = re.sub(rf"{re.escape(SLOT_ISO_MARKER_PREFIX)}.*?{re.escape(SLOT_ISO_MARKER_SUFFIX)}", "", response_text).strip()
        return response_text
    else:
        logging.error(f"!!! [{user_psid}] Nie uzyskano odpowiedzi Gemini (Gathering Info - Student Only).")
        # Zwróć wiadomość o błędzie, jeśli AI nie odpowiedziało
        return "Przepraszam, wystąpił błąd systemowy."

# --- Funkcja AI: Ogólna rozmowa ---
def get_gemini_general_response(user_psid, current_user_message_text, history_for_general_ai, is_temporary_general_state=False):
    """Prowadzi ogólną rozmowę z AI, z obsługą powrotu do poprzedniego stanu."""
    if not gemini_model:
        logging.error(f"!!! [{user_psid}] Model Gemini niezaładowany (General)!")
        return "Przepraszam, mam chwilowy problem z systemem."

    # Dostosuj odpowiedź modelu w zależności od tego, czy jest to stan tymczasowy
    model_ack = f"Rozumiem. Będę pomocnym asystentem klienta i dodam znacznik {INTENT_SCHEDULE_MARKER}, gdy użytkownik wyrazi chęć umówienia się."
    if is_temporary_general_state:
        model_ack = f"Rozumiem. Odpowiem na pytanie ogólne użytkownika. Jeśli odpowiedź użytkownika będzie satysfakcjonująca i nie będzie zawierać dalszych pytań ogólnych, dodam znacznik {RETURN_TO_PREVIOUS}."

    initial_prompt = [
        Content(role="user", parts=[Part.from_text(SYSTEM_INSTRUCTION_GENERAL)]),
        Content(role="model", parts=[Part.from_text(model_ack)])
    ]
    full_prompt = initial_prompt + history_for_general_ai
    if current_user_message_text:
        full_prompt.append(Content(role="user", parts=[Part.from_text(current_user_message_text)]))
    max_prompt_messages = (MAX_HISTORY_TURNS * 2) + 2
    while len(full_prompt) > max_prompt_messages:
        full_prompt.pop(2)
        if len(full_prompt) > 2:
            full_prompt.pop(2)
    response_text = _call_gemini(user_psid, full_prompt, GENERATION_CONFIG_DEFAULT, "General Conversation")
    if response_text:
        # Nie usuwamy już tutaj RETURN_TO_PREVIOUS ani INTENT_SCHEDULE_MARKER
        if SLOT_ISO_MARKER_PREFIX in response_text:
            response_text = re.sub(rf"{re.escape(SLOT_ISO_MARKER_PREFIX)}.*?{re.escape(SLOT_ISO_MARKER_SUFFIX)}", "", response_text).strip()
        if INFO_GATHERED_MARKER in response_text:
            response_text = response_text.replace(INFO_GATHERED_MARKER, "").strip()
        if SWITCH_TO_GENERAL in response_text: # Usunięcie na wszelki wypadek
            response_text = response_text.replace(SWITCH_TO_GENERAL, "").strip()
        return response_text
    else:
        logging.error(f"!!! [{user_psid}] Nie uzyskano odpowiedzi Gemini (General).")
        # Zwróć wiadomość o błędzie, jeśli AI nie odpowiedziało
        return "Przepraszam, wystąpił błąd przetwarzania Twojej wiadomości."


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
    """Główny handler dla przychodzących zdarzeń z Messengera."""
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
                    # Kopiujemy kontekst, usuwając potencjalne klucze powrotu na start cyklu
                    context_data_to_save = context.copy()
                    context_data_to_save.pop('return_to_state', None)
                    context_data_to_save.pop('return_to_context', None)

                    trigger_gathering_ai_immediately = False
                    slot_verification_failed = False
                    is_temporary_general_state = 'return_to_state' in context # Sprawdź, czy byliśmy w stanie tymczasowym

                    # === Obsługa wiadomości tekstowych ===
                    if message_data := event.get("message"):
                        if message_data.get("is_echo"):
                            logging.debug(f"    Pominięto echo wiadomości bota.")
                            continue
                        user_input_text = message_data.get("text", "").strip()
                        if user_input_text:
                            user_content = Content(role="user", parts=[Part.from_text(user_input_text)])
                            logging.info(f"    Otrzymano wiadomość tekstową (stan={current_state}): '{user_input_text[:100]}{'...' if len(user_input_text)>100 else ''}'")
                            if ENABLE_TYPING_DELAY:
                                time.sleep(MIN_TYPING_DELAY_SECONDS * 0.5)
                            # Ustalenie akcji na podstawie stanu
                            if current_state == STATE_SCHEDULING_ACTIVE:
                                action = 'handle_scheduling'
                            elif current_state == STATE_GATHERING_INFO:
                                action = 'handle_gathering'
                            else: # STATE_GENERAL (może być normalny lub tymczasowy)
                                action = 'handle_general'
                        elif attachments := message_data.get("attachments"):
                             att_type = attachments[0].get('type','nieznany')
                             logging.info(f"      Otrzymano załącznik typu: {att_type}.")
                             user_content = Content(role="user", parts=[Part.from_text(f"[Użytkownik wysłał załącznik typu: {att_type}]")])
                             msg_result = "Dziękuję, ale obecnie mogę przetwarzać tylko wiadomości tekstowe." if att_type not in ['sticker', 'image', 'audio', 'video', 'file'] else "Dzięki!"
                             action = 'send_info'
                             next_state = current_state # Pozostań w tym samym stanie
                        else:
                            logging.info("      Otrzymano pustą wiadomość lub nieobsługiwany typ.")
                            action = None
                    # === Obsługa Postback ===
                    elif postback := event.get("postback"):
                        payload = postback.get("payload")
                        title = postback.get("title", "")
                        logging.info(f"    Otrzymano postback: Payload='{payload}', Tytuł='{title}', Stan={current_state}")
                        user_input_text = f"Użytkownik kliknął przycisk: '{title}' (Payload: {payload})"
                        user_content = Content(role="user", parts=[Part.from_text(user_input_text)])
                        if payload == "CANCEL_SCHEDULING":
                             msg_result = "Rozumiem, anulowano proces umawiania terminu. W czymś jeszcze mogę pomóc?"
                             action = 'send_info'
                             next_state = STATE_GENERAL
                             context_data_to_save = {} # Wyczyść kontekst
                        elif current_state == STATE_SCHEDULING_ACTIVE:
                            action = 'handle_scheduling'
                        elif current_state == STATE_GATHERING_INFO:
                            action = 'handle_gathering'
                        else:
                            action = 'handle_general'
                    # === Inne zdarzenia ===
                    elif event.get("read"):
                        logging.debug(f"    Otrzymano potwierdzenie odczytania.")
                        continue
                    elif event.get("delivery"):
                        logging.debug(f"    Otrzymano potwierdzenie dostarczenia.")
                        continue
                    else:
                        logging.warning(f"    Otrzymano nieobsługiwany typ zdarzenia: {json.dumps(event)}")
                        continue

                    # --- Pętla przetwarzania akcji ---
                    loop_guard = 0
                    while action and loop_guard < 3: # Zwiększono limit na wszelki wypadek
                        loop_guard += 1
                        logging.debug(f"  >> Pętla akcji {loop_guard}/3 | Akcja: {action} | Stan wejściowy: {current_state} | Kontekst wej.: {context_data_to_save}")
                        current_action = action
                        action = None # Reset

                        # --- Obsługa Stanu Generalnego (w tym powrotu) ---
                        if current_action == 'handle_general':
                            logging.debug("  >> Wykonanie: handle_general")
                            if user_content and user_content.parts:
                                was_temporary_general = 'return_to_state' in context
                                response = get_gemini_general_response(sender_id, user_content.parts[0].text, history_for_gemini, was_temporary_general)

                                if response:
                                    if RETURN_TO_PREVIOUS in response and was_temporary_general:
                                        logging.info(f"      AI Ogólne zasygnalizowało powrót [{RETURN_TO_PREVIOUS}]. Przywracanie stanu.")
                                        msg_result = response.split(RETURN_TO_PREVIOUS, 1)[0].strip()
                                        if msg_result:
                                            send_message(sender_id, msg_result)
                                            model_resp_content = Content(role="model", parts=[Part.from_text(msg_result)])
                                            history_for_gemini.append(user_content)
                                            history_for_gemini.append(model_resp_content)
                                        else:
                                             history_for_gemini.append(user_content)

                                        user_content = None; model_resp_content = None
                                        next_state = context.get('return_to_state', STATE_GENERAL)
                                        context_data_to_save = context.get('return_to_context', {})
                                        logging.info(f"      Przywrócono stan: {next_state}")
                                        logging.debug(f"      Przywrócony kontekst: {context_data_to_save}")

                                        if next_state == STATE_SCHEDULING_ACTIVE:
                                            action = 'handle_scheduling'
                                            # Nie wysyłamy "Wracając do...", AI samo wznowi
                                            msg_result = None
                                            model_resp_content = None
                                            trigger_gathering_ai_immediately = False # Nie triggerujemy od razu
                                        elif next_state == STATE_GATHERING_INFO:
                                            action = 'handle_gathering'
                                            trigger_gathering_ai_immediately = True
                                            logging.debug("      Ustawiono trigger_gathering_ai_immediately po powrocie.")
                                            msg_result = None
                                            model_resp_content = None
                                        else:
                                            logging.warning(f"      Nieoczekiwany stan powrotu: {next_state}. Przechodzę do STATE_GENERAL.")
                                            next_state = STATE_GENERAL; context_data_to_save = {}; action = None

                                        if action: continue # Kontynuuj pętlę

                                    elif INTENT_SCHEDULE_MARKER in response:
                                        logging.info(f"      AI Ogólne wykryło intencję [{INTENT_SCHEDULE_MARKER}]. Przejście do planowania.")
                                        initial_resp_text = response.split(INTENT_SCHEDULE_MARKER, 1)[0].strip()
                                        if initial_resp_text:
                                            send_message(sender_id, initial_resp_text)
                                            model_resp_content = Content(role="model", parts=[Part.from_text(initial_resp_text)])
                                            history_for_gemini.append(user_content)
                                            history_for_gemini.append(model_resp_content)
                                        else:
                                            history_for_gemini.append(user_content)
                                        user_content = None; model_resp_content = None
                                        next_state = STATE_SCHEDULING_ACTIVE; action = 'handle_scheduling'; context_data_to_save = {}
                                        logging.debug("      Przekierowanie do handle_scheduling...")
                                        continue
                                    else:
                                        msg_result = response; model_resp_content = Content(role="model", parts=[Part.from_text(msg_result)])
                                        next_state = STATE_GENERAL
                                        if was_temporary_general:
                                            context_data_to_save['return_to_state'] = context.get('return_to_state')
                                            context_data_to_save['return_to_context'] = context.get('return_to_context')
                                        else: context_data_to_save = {}
                                else:
                                    msg_result = "Przepraszam, mam problem z przetworzeniem Twojej wiadomości."; model_resp_content = Content(role="model", parts=[Part.from_text(msg_result)])
                                    next_state = STATE_GENERAL; context_data_to_save = {}
                            else: logging.warning("handle_general wywołane bez user_content.")

                        # --- Obsługa Stanu Planowania ---
                        elif current_action == 'handle_scheduling':
                            logging.debug("  >> Wykonanie: handle_scheduling")
                            try:
                                tz = _get_calendar_timezone(); now = datetime.datetime.now(tz); search_start = now; search_end_date = (search_start + datetime.timedelta(days=MAX_SEARCH_DAYS)).date(); search_end = tz.localize(datetime.datetime.combine(search_end_date, datetime.time(WORK_END_HOUR, 0)))
                                logging.info(f"      Pobieranie wolnych zakresów (z filtrem {MIN_BOOKING_LEAD_HOURS}h) od {search_start:%Y-%m-%d %H:%M} do {search_end:%Y-%m-%d %H:%M}")
                                _simulate_typing(sender_id, MAX_TYPING_DELAY_SECONDS * 0.6); free_ranges = get_free_time_ranges(TARGET_CALENDAR_ID, search_start, search_end)

                                if free_ranges:
                                    logging.info(f"      Znaleziono {len(free_ranges)} zakresów. Wywołanie AI Planującego...")
                                    current_input_text = user_content.parts[0].text if user_content and user_content.parts else None
                                    ai_response_text = get_gemini_scheduling_response(sender_id, history_for_gemini, current_input_text, free_ranges)

                                    if ai_response_text:
                                        # Sprawdź, czy AI chce przełączyć do General
                                        if ai_response_text.strip() == SWITCH_TO_GENERAL: # Przywrócono sprawdzanie
                                            logging.info(f"      AI Planujące zasygnalizowało pytanie ogólne [{SWITCH_TO_GENERAL}]. Przełączanie.")
                                            context_data_to_save['return_to_state'] = STATE_SCHEDULING_ACTIVE
                                            scheduling_context_minimal = {}
                                            context_data_to_save['return_to_context'] = scheduling_context_minimal
                                            context_data_to_save['type'] = STATE_GENERAL
                                            next_state = STATE_GENERAL
                                            action = 'handle_general'
                                            msg_result = None; model_resp_content = None
                                            logging.debug(f"      Zapisano stan powrotu. Nowy stan: {next_state}. Kontekst: {context_data_to_save}")
                                            continue

                                        # Sprawdź, czy AI ustaliło termin
                                        iso_match = re.search(rf"{re.escape(SLOT_ISO_MARKER_PREFIX)}(.*?){re.escape(SLOT_ISO_MARKER_SUFFIX)}", ai_response_text)
                                        if iso_match:
                                            extracted_iso = iso_match.group(1).strip(); logging.info(f"      AI Planujące zwróciło potencjalny finalny slot: {extracted_iso}"); text_for_user = re.sub(rf"{re.escape(SLOT_ISO_MARKER_PREFIX)}.*?{re.escape(SLOT_ISO_MARKER_SUFFIX)}", "", ai_response_text).strip(); text_for_user = re.sub(r'\s+', ' ', text_for_user).strip()
                                            try:
                                                proposed_start = datetime.datetime.fromisoformat(extracted_iso)
                                                tz_cal = _get_calendar_timezone()
                                                if proposed_start.tzinfo is None: proposed_start = tz_cal.localize(proposed_start)
                                                else: proposed_start = proposed_start.astimezone(tz_cal)
                                                proposed_slot_formatted = format_slot_for_user(proposed_start)
                                                logging.info(f"      Weryfikacja dostępności slotu w kalendarzu: {proposed_slot_formatted}")
                                                _simulate_typing(sender_id, MIN_TYPING_DELAY_SECONDS)
                                                calendar_free = is_slot_actually_free(proposed_start, TARGET_CALENDAR_ID)

                                                if calendar_free:
                                                    logging.info("      Weryfikacja Kalendarza OK! Zapis Fazy 1 i przejście do zbierania danych.")
                                                    write_ok, write_msg_or_row = write_to_sheet_phase1(sender_id, proposed_start)
                                                    if write_ok:
                                                        parent_profile = get_user_profile(sender_id); parent_first_name_api = parent_profile.get('first_name', '') if parent_profile else ''; parent_last_name_api = parent_profile.get('last_name', '') if parent_profile else ''
                                                        confirm_msg = text_for_user if text_for_user else f"Dobrze, potwierdzam termin {proposed_slot_formatted}."; confirm_msg += " Teraz poproszę o kilka dodatkowych informacji dotyczących ucznia."
                                                        send_message(sender_id, confirm_msg)
                                                        if user_content: history_for_gemini.append(user_content)
                                                        model_resp_content_confirm = Content(role="model", parts=[Part.from_text(confirm_msg)]); history_for_gemini.append(model_resp_content_confirm)
                                                        user_content = None; model_resp_content = None
                                                        context_data_to_save = {'proposed_slot_iso': proposed_start.isoformat(), 'proposed_slot_formatted': proposed_slot_formatted, 'known_parent_first_name': parent_first_name_api, 'known_parent_last_name': parent_last_name_api, 'known_student_first_name': '', 'known_student_last_name': '', 'known_grade': '', 'known_level': '', 'sheet_row_index': write_msg_or_row}
                                                        next_state = STATE_GATHERING_INFO; action = 'handle_gathering'; trigger_gathering_ai_immediately = True; logging.debug(f"      Ustawiono stan '{next_state}', akcję '{action}', trigger={trigger_gathering_ai_immediately}. Kontekst: {context_data_to_save}"); continue
                                                    else:
                                                        logging.error(f"Błąd zapisu Fazy 1 do arkusza: {write_msg_or_row}")
                                                        msg_result = f"Przepraszam, wystąpił błąd techniczny podczas wstępnej rezerwacji terminu ({write_msg_or_row}). Proszę spróbować ponownie później."; model_resp_content = Content(role="model", parts=[Part.from_text(msg_result)]); next_state = STATE_GENERAL; context_data_to_save = {}
                                                else:
                                                    logging.warning(f"      Weryfikacja KALENDARZA NIEUDANA! Slot {extracted_iso} ({proposed_slot_formatted}) został zajęty.")
                                                    fail_msg = f"Ojej, wygląda na to, że termin {proposed_slot_formatted} został właśnie zajęty w kalendarzu! Przepraszam za zamieszanie. Spróbujmy znaleźć inny."
                                                    fail_msg_for_ai = f"\n[SYSTEM: Termin {proposed_slot_formatted} okazał się zajęty (kalendarz). Zaproponuj inny termin z dostępnej listy.]"
                                                    msg_result = fail_msg
                                                    if user_content: history_for_gemini.append(user_content)
                                                    model_resp_content = Content(role="model", parts=[Part.from_text(fail_msg + fail_msg_for_ai)]); user_content = None
                                                    next_state = STATE_SCHEDULING_ACTIVE; slot_verification_failed = True; context_data_to_save = {}
                                            except ValueError: logging.error(f"!!! BŁĄD: AI zwróciło nieprawidłowy format ISO w znaczniku: '{extracted_iso}'"); msg_result = "Przepraszam, wystąpił błąd techniczny przy przetwarzaniu zaproponowanego terminu. Spróbujmy jeszcze raz."; model_resp_content = Content(role="model", parts=[Part.from_text(msg_result)]); next_state = STATE_SCHEDULING_ACTIVE; context_data_to_save = {}
                                            except Exception as verif_err: logging.error(f"!!! BŁĄD podczas weryfikacji slotu {extracted_iso}: {verif_err}", exc_info=True); msg_result = "Przepraszam, wystąpił nieoczekiwany błąd podczas sprawdzania dostępności terminu."; model_resp_content = Content(role="model", parts=[Part.from_text(msg_result)]); next_state = STATE_SCHEDULING_ACTIVE; context_data_to_save = {}
                                        else:
                                            logging.info("      AI Planujące kontynuuje rozmowę (brak znacznika ISO/SWITCH).")
                                            msg_result = ai_response_text
                                            model_resp_content = Content(role="model", parts=[Part.from_text(msg_result)])
                                            next_state = STATE_SCHEDULING_ACTIVE
                                    else:
                                        logging.error(f"!!! BŁĄD: AI Planujące nie zwróciło poprawnej odpowiedzi. Odpowiedź: {ai_response_text}")
                                        if ai_response_text and "zasady bezpieczeństwa" in ai_response_text: msg_result = ai_response_text
                                        else: msg_result = "Przepraszam, mam problem z systemem planowania. Spróbuj ponownie za chwilę."
                                        model_resp_content = Content(role="model", parts=[Part.from_text(msg_result)])
                                        next_state = STATE_GENERAL; context_data_to_save = {}
                                else:
                                    logging.warning(f"      Brak wolnych zakresów spełniających kryteria (w tym {MIN_BOOKING_LEAD_HOURS}h wyprzedzenia).")
                                    no_slots_msg = f"Niestety, wygląda na to, że nie mam żadnych wolnych terminów w ciągu najbliższych {MAX_SEARCH_DAYS} dni, które można zarezerwować z odpowiednim wyprzedzeniem ({MIN_BOOKING_LEAD_HOURS}h). Spróbuj ponownie później lub skontaktuj się z nami w inny sposób."
                                    msg_result = no_slots_msg; model_resp_content = Content(role="model", parts=[Part.from_text(no_slots_msg)]); next_state = STATE_GENERAL; context_data_to_save = {}
                            except Exception as schedule_err:
                                logging.error(f"!!! KRYTYCZNY BŁĄD w bloku 'handle_scheduling': {schedule_err}", exc_info=True)
                                msg_result = "Wystąpił nieoczekiwany błąd systemu podczas planowania. Przepraszam za problem."; model_resp_content = Content(role="model", parts=[Part.from_text(msg_result)]); next_state = STATE_GENERAL; context_data_to_save = {}

                        # --- Obsługa Stanu Zbierania Informacji ---
                        elif current_action == 'handle_gathering':
                            logging.debug("  >> Wykonanie: handle_gathering")
                            try:
                                known_info_for_ai = context_data_to_save.copy()
                                logging.debug(f"    Kontekst przekazywany do AI (Gathering): {known_info_for_ai}")
                                current_input_text = user_content.parts[0].text if user_content and user_content.parts else None
                                if trigger_gathering_ai_immediately:
                                    logging.info("      Pierwsze wywołanie AI zbierającego (po ustaleniu terminu lub powrocie).")
                                    current_input_text = None
                                    trigger_gathering_ai_immediately = False
                                ai_response_text = get_gemini_gathering_response(sender_id, history_for_gemini, current_input_text, known_info_for_ai)

                                if ai_response_text:
                                     # Sprawdź, czy AI chce przełączyć do General
                                    if ai_response_text.strip() == SWITCH_TO_GENERAL:
                                        logging.info(f"      AI Zbierające zasygnalizowało pytanie ogólne [{SWITCH_TO_GENERAL}]. Przełączanie.")
                                        context_data_to_save['return_to_state'] = STATE_GATHERING_INFO
                                        context_data_to_save['return_to_context'] = context_data_to_save.copy()
                                        context_data_to_save['type'] = STATE_GENERAL
                                        next_state = STATE_GENERAL
                                        action = 'handle_general'
                                        msg_result = None
                                        model_resp_content = None
                                        logging.debug(f"      Zapisano stan powrotu. Nowy stan: {next_state}. Kontekst: {context_data_to_save}")
                                        continue

                                    # Sprawdź, czy AI zakończyło zbieranie danych
                                    if INFO_GATHERED_MARKER in ai_response_text:
                                        logging.info(f"      AI Zbierające (Student Only) zasygnalizowało koniec [{INFO_GATHERED_MARKER}]. Próba parsowania danych i aktualizacji arkusza.")
                                        response_parts = ai_response_text.split(INFO_GATHERED_MARKER, 1)
                                        ai_full_response_before_marker = response_parts[0].strip()
                                        final_gathering_msg_for_user = ""
                                        data_line_index = ai_full_response_before_marker.find("ZEBRANE_DANE_UCZNIA:")
                                        if data_line_index != -1:
                                            end_of_data_line = ai_full_response_before_marker.find('\n', data_line_index)
                                            if end_of_data_line != -1: final_gathering_msg_for_user = ai_full_response_before_marker[end_of_data_line:].strip()
                                            else: logging.warning("      Format odpowiedzi AI (Gathering) nie zawierał nowej linii po ZEBRANE_DANE_UCZNIA."); final_gathering_msg_for_user = ""
                                        if not final_gathering_msg_for_user:
                                            final_gathering_msg_for_user = "Dziękuję za wszystkie informacje. Dane zostały zapisane. Wkrótce skontaktujemy się w celu potwierdzenia szczegółów. Proszę również oczekiwać na wiadomość dotyczącą płatności i dostępu do materiałów na profilu dedykowanym do komunikacji: https://www.facebook.com/profile.php?id=61576135251276"
                                            logging.warning("      Użyto domyślnej wiadomości końcowej dla użytkownika (Gathering).")

                                        # --- Parsowanie struktury ZEBRANE_DANE_UCZNIA ---
                                        student_first_name = "Brak (Parse)"; student_last_name = "Brak (Parse)"; grade_info = "Brak (Parse)"; level_info = "Brak (Parse)"
                                        data_regex = r"ZEBRANE_DANE_UCZNIA:\s*\[Imię:\s*(.*?),?\s*Nazwisko:\s*(.*?),?\s*KlasaInfo:\s*(.*?),?\s*Poziom:\s*(.*?)\]"; match = re.search(data_regex, ai_full_response_before_marker, re.IGNORECASE | re.DOTALL)
                                        if match:
                                            logging.debug("      Znaleziono dopasowanie regex dla ZEBRANE_DANE_UCZNIA.")
                                            student_first_name = match.group(1).strip() if match.group(1) else student_first_name; student_last_name = match.group(2).strip() if match.group(2) else student_last_name; grade_info = match.group(3).strip() if match.group(3) else grade_info; level_info = match.group(4).strip() if match.group(4) else level_info
                                            if level_info.lower() == 'brak': level_info = "Brak"
                                            logging.info(f"      Dane sparsowane z AI: Imię='{student_first_name}', Nazwisko='{student_last_name}', KlasaInfo='{grade_info}', Poziom='{level_info}'")
                                        else:
                                            logging.error("!!! BŁĄD: Nie udało się sparsować struktury ZEBRANE_DANE_UCZNIA z odpowiedzi AI! Używam danych z kontekstu jako fallback.")
                                            student_first_name = context_data_to_save.get('known_student_first_name', 'Brak (Fallback)'); student_last_name = context_data_to_save.get('known_student_last_name', 'Brak (Fallback)'); grade_info = context_data_to_save.get('known_grade', 'Brak (Fallback)'); level_info = context_data_to_save.get('known_level', 'Brak (Fallback)')
                                        # -------------------------------------------

                                        try:
                                            # --- Przygotowanie danych do aktualizacji Fazy 2 ---
                                            psid_to_update = sender_id
                                            iso_to_update = context_data_to_save.get('proposed_slot_iso')
                                            parent_fn = context_data_to_save.get('known_parent_first_name', 'Brak (API?)'); parent_ln = context_data_to_save.get('known_parent_last_name', 'Brak (API?)')
                                            sheet_row_idx = context_data_to_save.get('sheet_row_index') # Pobierz zapisany indeks

                                            if iso_to_update:
                                                start_time_obj = datetime.datetime.fromisoformat(iso_to_update)
                                                full_data_for_update = {'parent_first_name': parent_fn, 'parent_last_name': parent_ln, 'student_first_name': student_first_name, 'student_last_name': student_last_name, 'grade_info': grade_info, 'level_info': level_info}
                                                update_ok, update_msg = find_row_and_update_sheet(psid_to_update, start_time_obj, full_data_for_update, sheet_row_index=sheet_row_idx)
                                                if update_ok:
                                                    logging.info("      Aktualizacja Fazy 2 w Google Sheet zakończona sukcesem.")
                                                    msg_result = final_gathering_msg_for_user; model_resp_content = Content(role="model", parts=[Part.from_text(ai_full_response_before_marker)]); next_state = STATE_GENERAL; context_data_to_save = {}
                                                else:
                                                    logging.error(f"!!! BŁĄD aktualizacji Fazy 2 w Google Sheet: {update_msg}")
                                                    error_msg_user = f"Przepraszam, wystąpił problem podczas zapisywania pełnych danych ({update_msg}). Proszę spróbować ponownie lub skontaktować się z nami."; msg_result = error_msg_user; model_resp_content = Content(role="model", parts=[Part.from_text(error_msg_user)]); next_state = STATE_GATHERING_INFO
                                            else:
                                                logging.error("Brak 'proposed_slot_iso' w kontekście podczas próby aktualizacji Fazy 2."); msg_result = "Wystąpił błąd wewnętrzny (brak terminu w kontekście). Proszę skontaktować się z nami."; model_resp_content = Content(role="model", parts=[Part.from_text(msg_result)]); next_state = STATE_GENERAL; context_data_to_save = {}
                                        except Exception as sheet_write_err:
                                            logging.error(f"!!! KRYTYCZNY BŁĄD podczas przygotowania/aktualizacji Fazy 2: {sheet_write_err}", exc_info=True); msg_result = "Wystąpił krytyczny błąd podczas zapisywania danych. Proszę skontaktować się z nami bezpośrednio."; model_resp_content = Content(role="model", parts=[Part.from_text(msg_result)]); next_state = STATE_GENERAL; context_data_to_save = {}
                                    else:
                                        # AI kontynuuje zbieranie informacji o uczniu
                                        logging.info("      AI Zbierające (Student Only) kontynuuje rozmowę.")
                                        msg_result = ai_response_text
                                        model_resp_content = Content(role="model", parts=[Part.from_text(msg_result)])
                                        next_state = STATE_GATHERING_INFO
                                        # Aktualizuj kontekst na podstawie sparsowanej odpowiedzi AI, jeśli to możliwe
                                        temp_student_fn = "Brak"; temp_student_ln = "Brak"; temp_grade = "Brak"; temp_level = "Brak"
                                        temp_data_regex = r"\[Imię:\s*(.*?),?\s*Nazwisko:\s*(.*?),?\s*KlasaInfo:\s*(.*?),?\s*Poziom:\s*(.*?)\]"; temp_match = re.search(temp_data_regex, ai_response_text, re.IGNORECASE | re.DOTALL)
                                        if temp_match:
                                            temp_student_fn = temp_match.group(1).strip() if temp_match.group(1) else temp_student_fn; temp_student_ln = temp_match.group(2).strip() if temp_match.group(2) else temp_student_ln; temp_grade = temp_match.group(3).strip() if temp_match.group(3) else temp_grade; temp_level = temp_match.group(4).strip() if temp_match.group(4) else temp_level
                                            if temp_level.lower() == 'brak': temp_level = "Brak"
                                            if temp_student_fn != "Brak (Parse)": context_data_to_save['known_student_first_name'] = temp_student_fn
                                            if temp_student_ln != "Brak (Parse)": context_data_to_save['known_student_last_name'] = temp_student_ln
                                            if temp_grade != "Brak (Parse)": context_data_to_save['known_grade'] = temp_grade
                                            if temp_level != "Brak (Parse)": context_data_to_save['known_level'] = temp_level
                                            logging.debug(f"      Zaktualizowano kontekst na podstawie odpowiedzi AI: {context_data_to_save}")

                                else:
                                    logging.error(f"!!! BŁĄD: AI Zbierające (Student Only) nie zwróciło poprawnej odpowiedzi. Odpowiedź: {ai_response_text}")
                                    if ai_response_text and "zasady bezpieczeństwa" in ai_response_text: msg_result = ai_response_text
                                    else: msg_result = "Przepraszam, wystąpił błąd systemowy podczas zbierania informacji. Spróbuj odpowiedzieć jeszcze raz."
                                    model_resp_content = Content(role="model", parts=[Part.from_text(msg_result)])
                                    next_state = STATE_GATHERING_INFO
                            except Exception as gather_err:
                                logging.error(f"!!! KRYTYCZNY BŁĄD w bloku 'handle_gathering': {gather_err}", exc_info=True)
                                msg_result = "Wystąpił nieoczekiwany błąd systemu podczas zbierania informacji. Przepraszam za problem."; model_resp_content = Content(role="model", parts=[Part.from_text(msg_result)]); next_state = STATE_GENERAL; context_data_to_save = {}

                        elif current_action == 'send_info':
                            logging.debug("  >> Wykonanie: send_info")
                            if msg_result:
                                model_resp_content = Content(role="model", parts=[Part.from_text(msg_result)])
                            else:
                                logging.warning("Akcja 'send_info' bez wiadomości do wysłania.")
                        else:
                            logging.warning(f"   Nieznana lub nieobsługiwana akcja '{current_action}'. Zakończenie pętli.")
                            break

                    # --- WYSYŁANIE ODPOWIEDZI I ZAPIS STANU ---
                    final_context_to_save_dict = {'type': next_state, **context_data_to_save}
                    # Usunięto logikę zapisu flag powrotu
                    final_context_to_save_dict.pop('role', None)
                    context_for_comparison = context.copy()
                    context_for_comparison.pop('return_to_state', None)
                    context_for_comparison.pop('return_to_context', None)


                    if msg_result:
                        send_message(sender_id, msg_result)
                        if not model_resp_content:
                            logging.warning(f"Wiadomość '{msg_result[:50]}...' została wysłana, ale nie ustawiono model_resp_content! Tworzenie domyślnego.")
                            model_resp_content = Content(role="model", parts=[Part.from_text(msg_result)])
                    elif current_action:
                        logging.debug(f"    Akcja '{current_action}' zakończona bez wiadomości do wysłania użytkownikowi (może być OK).")

                    # Porównaj stary kontekst (bez flag powrotu) z nowym finalnym kontekstem
                    should_save = bool(user_content) or bool(model_resp_content) or (context_for_comparison != final_context_to_save_dict) or slot_verification_failed

                    if should_save:
                        history_to_save = list(history_for_gemini)
                        if user_content:
                            history_to_save.append(user_content)
                        if model_resp_content:
                            history_to_save.append(model_resp_content)
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

    print("\n" + "="*60 + "\n--- START KONFIGURACJI BOTA (Rozdzielone Osobowości + Pełne Przełączanie Kontekstu + Sprawdzanie Arkusza w get_free_time_ranges + Dwufazowy Zapis) ---") # Zaktualizowano tytuł
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
