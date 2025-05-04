# -*- coding: utf-8 -*-

# verify_server.py (Wersja: Wiele Kalendarzy + Logika 'OR' Wolnych + Poprawiony Filtr Arkusza Per Kalendarz + Nazwa Kal. w Arkuszu + Tylko Numer Klasy w Kol. H + Poprawione Formatowanie + Przywrócone Instrukcje AI)

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

# --- Importy Google Sheets (ZAPIS + ODCZYT) ---
# (Biblioteki już zaimportowane powyżej)

app = Flask(__name__)

# --- Konfiguracja Ogólna ---
VERIFY_TOKEN = os.environ.get("FB_VERIFY_TOKEN", "KOLAGEN")
PAGE_ACCESS_TOKEN = os.environ.get(
    "FB_PAGE_ACCESS_TOKEN",
    "EACNAHFzEhkUBO5sicIUMoIwuZCZC1ZAduL8gb5sZAjWX2oErT4esklQALmstq2bkZAnWq3CVNF0IO3gZB44ip3XCXG40revvmpFKOLlC9jBStCNAwbIXZBWfawg0z0YH6GLGZCE1gFfgEF5A6DEIKbu5FYZB6XKXHECTeW6PNZAUQrPiKxrPCjbz7QFiBtGROvZCPR4rAZDZD"
)
PROJECT_ID = os.environ.get("GCP_PROJECT_ID", "linear-booth-450221-k1")
LOCATION = os.environ.get("GCP_LOCATION", "us-central1")
MODEL_ID = os.environ.get("VERTEX_MODEL_ID", "gemini-2.0-flash-001")

FACEBOOK_GRAPH_API_URL = "https://graph.facebook.com/v19.0/me/messages"

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
# Lista kalendarzy do sprawdzania
CALENDARS = [
    {
        'id': 'f19e189826b9d6e36950da347ac84d5501ecbd6bed0d76c8641be61a67749c67@group.calendar.google.com',
        'name': 'Kalendarz Główny'
    },
    {
        'id': '3762cdf9ca674ed1e5dd87ff406dc92f365121aab827cea4d9a02085d31d15fb@group.calendar.google.com',
        'name': 'Kalendarz Dodatkowy'
    },
]
CALENDAR_IDS = [cal['id'] for cal in CALENDARS]
CALENDAR_ID_TO_NAME = {cal['id']: cal['name'] for cal in CALENDARS}

MAX_SEARCH_DAYS = 14
MIN_BOOKING_LEAD_HOURS = 24

# --- Konfiguracja Google Sheets (ZAPIS + ODCZYT) ---
SHEETS_SERVICE_ACCOUNT_FILE = 'arkuszklucz.json'
SHEET_SCOPES = ['https://www.googleapis.com/auth/spreadsheets']
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
SHEET_GRADE_COLUMN_INDEX = 8     # H - Numer Klasy (TYLKO LICZBA)
SHEET_SCHOOL_TYPE_COLUMN_INDEX = 9 # I - Typ szkoły
SHEET_LEVEL_COLUMN_INDEX = 10    # J - Poziom
SHEET_CALENDAR_NAME_COLUMN_INDEX = 11 # K - Nazwa Kalendarza
SHEET_READ_RANGE_FOR_PSID_SEARCH = f"{SHEET_NAME}!A2:A"
SHEET_READ_RANGE_FOR_BUSY_SLOTS = f"{SHEET_NAME}!{chr(ord('A') + SHEET_DATE_COLUMN_INDEX - 1)}2:{chr(ord('A') + SHEET_CALENDAR_NAME_COLUMN_INDEX - 1)}"

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
GENERATION_CONFIG_SCHEDULING = GenerationConfig(temperature=0.5, top_p=0.95, top_k=40, max_output_tokens=512)
GENERATION_CONFIG_GATHERING = GenerationConfig(temperature=0.4, top_p=0.95, top_k=40, max_output_tokens=350)
GENERATION_CONFIG_DEFAULT = GenerationConfig(temperature=0.7, top_p=0.95, top_k=40, max_output_tokens=1024)

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
        log_format = '%(asctime)s - %(levelname)s - [%(filename)s:%(lineno)d - %(funcName)s] - %(message)s'
        log_datefmt = '%Y-%m-%d %H:%M:%S'
        logging.basicConfig(level=logging.INFO, format=log_format, datefmt=log_datefmt)
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
    user_profile_api_url_template = "https://graph.facebook.com/v19.0/{psid}?fields=first_name,last_name,profile_pic&access_token={token}"
    url = user_profile_api_url_template.format(psid=psid, token=PAGE_ACCESS_TOKEN)
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
                                logging.warning(f"Ostrz. [{user_psid}]: Niepoprawna część wiadomości (idx {i}) w pliku {filepath}")
                                valid_parts = False
                                break
                        if valid_parts and text_parts:
                            history.append(Content(role=msg_data['role'], parts=text_parts))
                    elif isinstance(msg_data, dict) and msg_data.get('role') == 'system':
                        if i == last_system_message_index:
                            state_type = msg_data.get('type')
                            if state_type and state_type in valid_states:
                                context = msg_data
                                logging.debug(f"[{user_psid}] Odczytano AKTYWNY kontekst: {context}")
                            elif state_type:
                                logging.warning(f"[{user_psid}] Znaleziono ostatni kontekst w pliku {filepath}, ale z nieprawidłowym typem: {msg_data}. Ignorowanie typu, zachowując dane.")
                                context = msg_data
                                context['type'] = STATE_GENERAL
                            else:
                                logging.warning(f"[{user_psid}] Znaleziono ostatni kontekst systemowy w pliku {filepath}, ale bez typu: {msg_data}. Ustawiam stan {STATE_GENERAL}.")
                                context = msg_data
                                context['type'] = STATE_GENERAL
                        else:
                            logging.debug(f"[{user_psid}] Pominięto stary kontekst systemowy (idx {i}) w pliku {filepath}: {msg_data}")
                    else:
                        logging.warning(f"Ostrz. [{user_psid}]: Pominięto niepoprawną wiadomość/kontekst (idx {i}) w pliku {filepath}: {msg_data}")

                if not context or context.get('type') not in valid_states:
                    if not context:
                        logging.debug(f"[{user_psid}] Nie znaleziono kontekstu systemowego na końcu pliku {filepath}. Ustawiam stan {STATE_GENERAL}.")
                        context = {}
                    context['type'] = STATE_GENERAL

                logging.info(f"[{user_psid}] Wczytano historię z {filepath}: {len(history)} wiad. Stan: {context.get('type', STATE_GENERAL)}")
                context.pop('role', None)
                return history, context
            else:
                logging.error(f"BŁĄD [{user_psid}]: Plik historii {filepath} nie jest listą.")
                return [], {'type': STATE_GENERAL}
    except FileNotFoundError:
        logging.info(f"[{user_psid}] Plik historii {filepath} nie istnieje.")
        return [], {'type': STATE_GENERAL}
    except (json.JSONDecodeError, KeyError, IndexError, TypeError) as e:
        logging.error(f"BŁĄD [{user_psid}] parsowania historii z {filepath}: {e}.")
        try:
            os.rename(filepath, f"{filepath}.error_{int(time.time())}")
            logging.warning("    Zmieniono nazwę uszkodzonego pliku historii.")
        except OSError as rename_err:
            logging.error(f"    Nie udało się zmienić nazwy: {rename_err}")
        return [], {'type': STATE_GENERAL}
    except Exception as e:
        logging.error(f"BŁĄD [{user_psid}] wczytywania historii z {filepath}: {e}", exc_info=True)
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
        if context_to_save and isinstance(context_to_save, dict) and (current_state_to_save != STATE_GENERAL or 'return_to_state' in context_to_save):
            context_copy = context_to_save.copy()
            context_copy['role'] = 'system'
            history_data.append(context_copy)
            logging.debug(f"[{user_psid}] Dodano kontekst {current_state_to_save} do zapisu: {context_copy}")
        else:
            logging.debug(f"[{user_psid}] Zapis bez dodatkowego kontekstu systemowego (stan general bez powrotu lub brak kontekstu).")

        with open(temp_filepath, 'w', encoding='utf-8') as f:
            json.dump(history_data, f, ensure_ascii=False, indent=2)
        os.replace(temp_filepath, filepath)
        logging.info(f"[{user_psid}] Zapisano historię/kontekst ({len(history_data)} wpisów, stan: {current_state_to_save}) do {filepath}")

    except Exception as e:
        logging.error(f"BŁĄD [{user_psid}] zapisu historii/kontekstu do {filepath}: {e}", exc_info=True)
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
        tz = _get_calendar_timezone()
        if slot_start.tzinfo is None:
            slot_start = tz.localize(slot_start)
        else:
            slot_start = slot_start.astimezone(tz)
        try:
            day_name = slot_start.strftime('%A').capitalize()
        except Exception:
            day_name = POLISH_WEEKDAYS[slot_start.weekday()]
        hour_str = f"{slot_start.hour}"
        try:
            formatted_date = slot_start.strftime('%d.%m.%Y')
            formatted_time = slot_start.strftime(f'{hour_str}:%M')
            return f"{day_name}, {formatted_date} o {formatted_time}"
        except Exception as format_err:
            logging.warning(f"Błąd formatowania daty/czasu przez strftime: {format_err}. Używam formatu ISO.")
            return slot_start.strftime('%Y-%m-%d %H:%M')
    except Exception as e:
        logging.error(f"Błąd formatowania slotu {slot_start}: {e}", exc_info=True)
        return slot_start.isoformat()

def extract_school_type(grade_string):
    """
    Próbuje wyodrębnić numer klasy, opis klasy i typ szkoły z ciągu.
    Zwraca krotkę: (numerical_grade, class_desc, school_type).
    """
    if not grade_string or not isinstance(grade_string, str):
        return "", "Nieokreślona", "Nieokreślona"

    grade_lower = grade_string.lower().strip()
    class_desc = grade_string.strip()
    school_type = "Nieokreślona"
    numerical_grade = ""

    type_mapping = {
        "Liceum": [r'liceum', r'\blo\b'],
        "Technikum": [r'technikum', r'\btech\b'],
        "Szkoła Podstawowa": [r'podstaw', r'\bsp\b'],
        "Szkoła Branżowa/Zawodowa": [r'zawodowa', r'branżowa', r'zasadnicza']
    }
    found_type = False
    for type_name, patterns in type_mapping.items():
        for pattern in patterns:
            match = re.search(pattern, grade_lower)
            if match:
                school_type = type_name
                pattern_to_remove = r'(?i)(\bklas[ay]\s+)?' + re.escape(match.group(0)) + r'(\s+\bklas[ay]\b)?\s*'
                cleaned_desc_candidate = re.sub(pattern_to_remove, ' ', class_desc).strip()
                if cleaned_desc_candidate and len(cleaned_desc_candidate) < len(class_desc) and not cleaned_desc_candidate.isdigit():
                    class_desc = cleaned_desc_candidate
                elif not cleaned_desc_candidate:
                    num_match_inner = re.search(r'\b(\d+)\b', grade_lower)
                    class_desc = num_match_inner.group(1) if num_match_inner else ""
                found_type = True
                break
        if found_type:
            break

    if school_type == "Nieokreślona":
        num_match_outer = re.search(r'\b\d+\b', grade_lower)
        if num_match_outer:
            school_type = "Inna (z numerem klasy)"
            if not class_desc or class_desc == grade_string.strip():
                class_desc = num_match_outer.group(0)

    num_match_final = re.search(r'\b(\d+)\b', grade_string)
    if num_match_final:
        numerical_grade = num_match_final.group(1)

    class_desc = re.sub(r'\bklasa\b', '', class_desc, flags=re.IGNORECASE).strip()
    class_desc = class_desc if class_desc else grade_string.strip()

    logging.debug(f"extract_school_type('{grade_string}') -> num: '{numerical_grade}', desc: '{class_desc}', type: '{school_type}'")
    return numerical_grade, class_desc, school_type

# =====================================================================
# === FUNKCJE GOOGLE CALENDAR (ODCZYT/WERYFIKACJA) ====================
# =====================================================================

def get_calendar_service():
    """Inicjalizuje (i cachuje) usługę Google Calendar API."""
    global _calendar_service
    if _calendar_service:
        return _calendar_service
    if not os.path.exists(CALENDAR_SERVICE_ACCOUNT_FILE):
        logging.error(f"KRYTYCZNY BŁĄD: Brak pliku klucza '{CALENDAR_SERVICE_ACCOUNT_FILE}'")
        return None
    try:
        creds = service_account.Credentials.from_service_account_file(
            CALENDAR_SERVICE_ACCOUNT_FILE, scopes=CALENDAR_SCOPES
        )
        _calendar_service = build('calendar', 'v3', credentials=creds, cache_discovery=False)
        logging.info(f"Utworzono połączenie z Google Calendar API (odczyt) używając '{CALENDAR_SERVICE_ACCOUNT_FILE}'.")
        return _calendar_service
    except Exception as e:
        logging.error(f"Błąd tworzenia usługi Google Calendar: {e}", exc_info=True)
        return None

def parse_event_time(event_time_data, default_tz):
    """Parsuje dane czasu wydarzenia z API Kalendarza."""
    dt_str = None
    is_date_only = False
    if not isinstance(event_time_data, dict):
        logging.warning(f"Ostrz.: parse_event_time typ danych: {type(event_time_data)}")
        return None
    if 'dateTime' in event_time_data:
        dt_str = event_time_data['dateTime']
    elif 'date' in event_time_data:
        dt_str = event_time_data['date']
        is_date_only = True
    else:
        logging.debug(f"Brak 'dateTime'/'date' w event_time_data: {event_time_data}")
        return None
    if not isinstance(dt_str, str):
        logging.warning(f"Ostrz.: Oczekiwano stringa czasu, otrzymano {type(dt_str)} w {event_time_data}")
        return None
    try:
        if is_date_only:
            logging.debug(f"Ignorowanie wydarzenia całodniowego: {dt_str}")
            return None
        else:
            if dt_str.endswith('Z'):
                dt_str = dt_str[:-1] + '+00:00'
            dt = datetime.datetime.fromisoformat(dt_str)
            if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
                logging.warning(f"Ostrz.: dateTime '{event_time_data['dateTime']}' jako naiwny. Zakładam {default_tz.zone}.")
                dt_aware = default_tz.localize(dt)
            else:
                dt_aware = dt.astimezone(default_tz)
            return dt_aware
    except ValueError as e:
        logging.warning(f"Ostrz.: Nie sparsowano czasu '{dt_str}': {e}")
        return None
    except Exception as e:
        logging.error(f"Nieoczekiwany błąd parsowania czasu '{dt_str}': {e}", exc_info=True)
        return None

def get_calendar_busy_slots(calendar_ids_to_check, start_datetime, end_datetime):
    """Pobiera zajęte sloty z podanych kalendarzy Google."""
    service_cal = get_calendar_service()
    tz = _get_calendar_timezone()
    busy_times_calendar = []
    if not service_cal:
        logging.error("Błąd: Usługa kalendarza niedostępna.")
        return busy_times_calendar
    if not calendar_ids_to_check:
        logging.warning("Brak ID kalendarzy do sprawdzenia.")
        return busy_times_calendar

    items = [{"id": cal_id} for cal_id in calendar_ids_to_check]
    body = {
        "timeMin": start_datetime.isoformat(),
        "timeMax": end_datetime.isoformat(),
        "timeZone": CALENDAR_TIMEZONE,
        "items": items
    }
    try:
        logging.debug(f"Wykonywanie zapytania freeBusy dla kalendarzy: {calendar_ids_to_check}")
        freebusy_result = service_cal.freebusy().query(body=body).execute()
        calendars_data = freebusy_result.get('calendars', {})

        for cal_id in calendar_ids_to_check:
            calendar_data = calendars_data.get(cal_id, {})
            cal_name = CALENDAR_ID_TO_NAME.get(cal_id, cal_id)
            if 'errors' in calendar_data:
                for error in calendar_data['errors']:
                    logging.error(f"Błąd API Freebusy dla '{cal_name}': {error.get('reason')} - {error.get('message')}")
                continue

            busy_times_raw = calendar_data.get('busy', [])
            logging.debug(f"Kalendarz '{cal_name}': {len(busy_times_raw)} surowych zajętych.")
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
                            busy_times_calendar.append({
                                'start': busy_start_clipped,
                                'end': busy_end_clipped,
                                'calendar_id': cal_id
                            })
                else:
                    logging.warning(f"Ostrz.: Pominięto zajęty slot z '{cal_name}': {busy_slot}")
    except HttpError as error:
        logging.error(f'Błąd HTTP API Freebusy: {error.resp.status} {error.resp.reason}', exc_info=True)
    except Exception as e:
        logging.error(f"Nieoczekiwany błąd podczas freeBusy: {e}", exc_info=True)

    logging.info(f"Znaleziono {len(busy_times_calendar)} zajętych slotów w kalendarzach: {calendar_ids_to_check}.")
    return busy_times_calendar

def get_sheet_booked_slots(spreadsheet_id, sheet_name, start_datetime, end_datetime):
    """
    Pobiera zajęte sloty z arkusza Google, włącznie z nazwą kalendarza.
    Zwraca listę słowników: {'start': dt, 'end': dt, 'calendar_name': str}.
    """
    service = get_sheets_service()
    sheet_busy_slots = []
    if not service:
        logging.error("Błąd: Usługa arkuszy niedostępna.")
        return sheet_busy_slots

    tz_sheet = _get_sheet_timezone()
    tz_cal = _get_calendar_timezone()
    # Konwersja granic do strefy czasowej kalendarza
    if start_datetime.tzinfo is None:
        start_datetime_aware = tz_cal.localize(start_datetime)
    else:
        start_datetime_aware = start_datetime.astimezone(tz_cal)
    if end_datetime.tzinfo is None:
        end_datetime_aware = tz_cal.localize(end_datetime)
    else:
        end_datetime_aware = end_datetime.astimezone(tz_cal)

    try:
        read_range = SHEET_READ_RANGE_FOR_BUSY_SLOTS
        logging.debug(f"Odczyt arkusza '{sheet_name}' zakres '{read_range}' dla zajętych slotów.")
        result = service.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id, range=read_range
        ).execute()
        values = result.get('values', [])
        if not values:
            logging.debug("Arkusz pusty/brak danych w zakresie.")
            return sheet_busy_slots

        duration_delta = datetime.timedelta(minutes=APPOINTMENT_DURATION_MINUTES)
        date_idx = 0
        time_idx = SHEET_TIME_COLUMN_INDEX - SHEET_DATE_COLUMN_INDEX
        cal_name_idx = SHEET_CALENDAR_NAME_COLUMN_INDEX - SHEET_DATE_COLUMN_INDEX

        for i, row in enumerate(values):
            row_num = i + 2
            if len(row) > cal_name_idx:
                date_str = row[date_idx].strip()
                time_str = row[time_idx].strip()
                calendar_name_str = row[cal_name_idx].strip()

                if not date_str or not time_str:
                    logging.debug(f"Pominięto wiersz {row_num} z brakującą datą lub czasem.")
                    continue
                if not calendar_name_str:
                    logging.warning(f"Wiersz {row_num} w arkuszu nie ma nazwy kalendarza. Pomijanie w filtrze 'per kalendarz'.")
                    continue

                try:
                    naive_date = datetime.datetime.strptime(date_str, '%Y-%m-%d').date()
                    naive_time = datetime.datetime.strptime(time_str, '%H:%M').time()
                    naive_dt = datetime.datetime.combine(naive_date, naive_time)
                    slot_start_sheet_tz = tz_sheet.localize(naive_dt)
                    slot_start = slot_start_sheet_tz.astimezone(tz_cal)

                    if start_datetime_aware <= slot_start < end_datetime_aware:
                        slot_end = slot_start + duration_delta
                        sheet_busy_slots.append({
                            'start': slot_start,
                            'end': slot_end,
                            'calendar_name': calendar_name_str
                        })
                        logging.debug(f"  Zajęty slot w arkuszu (wiersz {row_num}): {slot_start:%Y-%m-%d %H:%M %Z} - {slot_end:%H:%M %Z} (Kalendarz: '{calendar_name_str}')")
                except ValueError:
                    logging.warning(f"  Pominięto wiersz {row_num} (błąd parsowania): Data='{date_str}', Czas='{time_str}'")
                except Exception as parse_err:
                    logging.warning(f"  Pominięto wiersz {row_num} (błąd): {parse_err} (Data='{date_str}', Czas='{time_str}')")
            else:
                logging.debug(f"Pominięto zbyt krótki wiersz {row_num} w arkuszu: {row}")

    except HttpError as error:
        logging.error(f"Błąd HTTP API odczytu arkusza: {error.resp.status} {error.resp.reason}", exc_info=True)
    except Exception as e:
        logging.error(f"Nieoczekiwany błąd odczytu arkusza: {e}", exc_info=True)

    logging.info(f"Znaleziono {len(sheet_busy_slots)} zajętych slotów w arkuszu z przypisanymi kalendarzami.")
    return sheet_busy_slots

def get_free_time_ranges(calendar_config_list, start_datetime, end_datetime):
    """
    Pobiera listę wolnych zakresów czasowych, które są dostępne w CO NAJMNIEJ JEDNYM
    kalendarzu PO odfiltrowaniu przez przypisane do niego rezerwacje z arkusza.
    """
    service_cal = get_calendar_service()
    tz = _get_calendar_timezone()
    if not service_cal:
        logging.error("Błąd: Usługa kalendarza niedostępna.")
        return []
    if not calendar_config_list:
        logging.warning("Brak konfiguracji kalendarzy do sprawdzenia.")
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
    search_start_unfiltered = max(start_datetime, now)

    if search_start_unfiltered >= end_datetime:
        logging.info(f"Zakres [{search_start_unfiltered:%Y-%m-%d %H:%M} - {end_datetime:%Y-%m-%d %H:%M}] nieprawidłowy/przeszły.")
        return []

    calendar_names = [c['name'] for c in calendar_config_list]
    logging.info(f"Szukanie wolnych zakresów (Logika OR, Filtr Arkusza Per Kalendarz) w {calendar_names} od {search_start_unfiltered:%Y-%m-%d %H:%M %Z} do {end_datetime:%Y-%m-%d %H:%M %Z}")

    min_duration_delta = datetime.timedelta(minutes=APPOINTMENT_DURATION_MINUTES)

    # --- Krok 1: Pobierz WSZYSTKIE zajęte sloty z Arkusza ---
    all_sheet_bookings = get_sheet_booked_slots(SPREADSHEET_ID, SHEET_NAME, search_start_unfiltered, end_datetime)
    all_sheet_bookings.sort(key=lambda x: x['start'])
    logging.debug(f"--- Zajęte sloty z Arkusza (łącznie {len(all_sheet_bookings)}) ---")
    if all_sheet_bookings:
        for i, s in enumerate(all_sheet_bookings[:15]):
            logging.debug(f"  Arkusz Slot {i+1}: {s['start']:%H:%M}-{s['end']:%H:%M} (Kal: '{s.get('calendar_name', 'Brak')}')")
        if len(all_sheet_bookings) > 15:
            logging.debug("  ...")

    # --- Krok 2: Dla każdego kalendarza, oblicz jego wolne sloty po filtracji ---
    all_individually_filtered_free_ranges = []
    for cal_config in calendar_config_list:
        cal_id = cal_config['id']
        cal_name = cal_config['name']
        logging.debug(f"--- Przetwarzanie kalendarza: '{cal_name}' ({cal_id}) ---")

        # 2a: Pobierz zajęte z Google Calendar dla TEGO kalendarza
        busy_times_cal = get_calendar_busy_slots([cal_id], search_start_unfiltered, end_datetime)
        busy_times_cal.sort(key=lambda x: x['start'])
        merged_busy_cal = []
        for busy in busy_times_cal:
            if not merged_busy_cal or busy['start'] >= merged_busy_cal[-1]['end']:
                merged_busy_cal.append(busy.copy())
            else:
                merged_busy_cal[-1]['end'] = max(merged_busy_cal[-1]['end'], busy['end'])

        # 2b: Oblicz "surowe" wolne dla TEGO kalendarza (na podstawie jego zajętości)
        raw_calendar_free_ranges = []
        current_time = search_start_unfiltered
        for busy_slot in merged_busy_cal:
            if current_time < busy_slot['start']:
                raw_calendar_free_ranges.append({'start': current_time, 'end': busy_slot['start']})
            current_time = max(current_time, busy_slot['end'])
        if current_time < end_datetime:
            raw_calendar_free_ranges.append({'start': current_time, 'end': end_datetime})

        # Zastosuj filtr godzin pracy do "surowych" wolnych
        raw_calendar_free_ranges_workhours = []
        for free_range in raw_calendar_free_ranges:
            range_start = free_range['start']
            range_end = free_range['end']
            current_day_start = range_start
            while current_day_start < range_end:
                day_date = current_day_start.date()
                work_day_start_dt = tz.localize(datetime.datetime.combine(day_date, datetime.time(WORK_START_HOUR, 0)))
                work_day_end_dt = tz.localize(datetime.datetime.combine(day_date, datetime.time(WORK_END_HOUR, 0)))
                effective_start = max(current_day_start, work_day_start_dt)
                effective_end = min(range_end, work_day_end_dt)
                if effective_start < effective_end and (effective_end - effective_start) >= min_duration_delta:
                    raw_calendar_free_ranges_workhours.append({'start': effective_start, 'end': effective_end})
                next_day_start_dt = tz.localize(datetime.datetime.combine(day_date + datetime.timedelta(days=1), datetime.time.min))
                current_day_start = max(effective_end, next_day_start_dt)
                current_day_start = max(current_day_start, range_start)

        logging.debug(f"    Surowe wolne dla '{cal_name}' (po filtrze godzin): {len(raw_calendar_free_ranges_workhours)}")
        if raw_calendar_free_ranges_workhours:
            for i, s in enumerate(raw_calendar_free_ranges_workhours[:5]):
                logging.debug(f"      Surowy Slot {i+1}: {s['start']:%H:%M}-{s['end']:%H:%M}")
            if len(raw_calendar_free_ranges_workhours) > 5:
                logging.debug("      ...")

        # 2c: Odfiltruj surowe wolne używając TYLKO rezerwacji z arkusza dla TEGO kalendarza
        logging.debug(f"    Próba znalezienia rezerwacji w arkuszu dla kalendarza: '{cal_name}' (case-insensitive, stripped)")
        # Poprawione, odporne porównanie nazw
        sheet_bookings_for_this_cal = [
            b for b in all_sheet_bookings
            if b.get('calendar_name', '').strip().lower() == cal_name.strip().lower()
            and b.get('calendar_name', '').strip() # Upewnij się, że nazwa nie jest pusta po strip()
        ]
        logging.debug(f"    Znaleziono {len(sheet_bookings_for_this_cal)} pasujących rezerwacji w arkuszu dla '{cal_name}'.")
        if not sheet_bookings_for_this_cal and any(b.get('calendar_name') for b in all_sheet_bookings):
            logging.debug(f"      DEBUG: Nie znaleziono dopasowań dla '{cal_name.strip().lower()}'. Nazwy w arkuszu (lower/strip):")
            sheet_names_lower = set(b.get('calendar_name', 'BRAK').strip().lower() for b in all_sheet_bookings if b.get('calendar_name'))
            for name in sheet_names_lower:
                logging.debug(f"        - '{name}'")

        candidate_ranges = raw_calendar_free_ranges_workhours
        if sheet_bookings_for_this_cal:
            logging.debug(f"    Filtrowanie wg {len(sheet_bookings_for_this_cal)} rezerwacji z arkusza...")
            for sheet_busy in sheet_bookings_for_this_cal:
                logging.debug(f"      Filtr arkusza: {sheet_busy['start']:%H:%M}-{sheet_busy['end']:%H:%M}")
                next_candidate_ranges = []
                for calendar_free in candidate_ranges:
                    overlap_start = max(calendar_free['start'], sheet_busy['start'])
                    overlap_end = min(calendar_free['end'], sheet_busy['end'])
                    if overlap_start < overlap_end: # Jest nakładanie
                        logging.debug(f"        Nakładanie z {calendar_free['start']:%H:%M}-{calendar_free['end']:%H:%M}")
                        if calendar_free['start'] < overlap_start and (overlap_start - calendar_free['start']) >= min_duration_delta:
                            next_candidate_ranges.append({'start': calendar_free['start'], 'end': overlap_start})
                            logging.debug(f"          -> Zachowano przed: {calendar_free['start']:%H:%M}-{overlap_start:%H:%M}")
                        if calendar_free['end'] > overlap_end and (calendar_free['end'] - overlap_end) >= min_duration_delta:
                            next_candidate_ranges.append({'start': overlap_end, 'end': calendar_free['end']})
                            logging.debug(f"          -> Zachowano po: {overlap_end:%H:%M}-{calendar_free['end']:%H:%M}")
                    else: # Brak nakładania
                        next_candidate_ranges.append(calendar_free)
                candidate_ranges = sorted(next_candidate_ranges, key=lambda x: x['start'])
            filtered_calendar_free_ranges = candidate_ranges
            logging.debug(f"    Sloty dla '{cal_name}' PO filtracji arkuszem: {len(filtered_calendar_free_ranges)}")
        else: # Brak rezerwacji w arkuszu dla tego kalendarza
            filtered_calendar_free_ranges = raw_calendar_free_ranges_workhours
            logging.debug(f"    Brak rezerwacji w arkuszu do filtrowania dla '{cal_name}'.")

        all_individually_filtered_free_ranges.extend(filtered_calendar_free_ranges)
        if filtered_calendar_free_ranges:
            for i, s in enumerate(filtered_calendar_free_ranges[:5]):
                logging.debug(f"      Wynikowy slot dla {cal_name} {i+1}: {s['start']:%H:%M}-{s['end']:%H:%M}")
            if len(filtered_calendar_free_ranges) > 5:
                logging.debug("      ...")

    # --- Krok 3: Połącz wszystkie indywidualnie przefiltrowane wolne zakresy ---
    if not all_individually_filtered_free_ranges:
        logging.info("Brak wolnych zakresów w żadnym kalendarzu po indywidualnej filtracji.")
        return []
    sorted_filtered_free = sorted(all_individually_filtered_free_ranges, key=lambda x: x['start'])
    logging.debug(f"--- Łączenie {len(sorted_filtered_free)} indywidualnie przefiltrowanych slotów (Logika 'OR') ---")

    # --- Krok 4: Scal połączone zakresy (Logika 'OR') ---
    merged_all_free_ranges = []
    if sorted_filtered_free:
        current_merged_slot = sorted_filtered_free[0].copy()
        for next_slot in sorted_filtered_free[1:]:
            if next_slot['start'] <= current_merged_slot['end']:
                current_merged_slot['end'] = max(current_merged_slot['end'], next_slot['end'])
            else:
                merged_all_free_ranges.append(current_merged_slot)
                current_merged_slot = next_slot.copy()
        merged_all_free_ranges.append(current_merged_slot)

    logging.debug(f"--- Scalone wolne zakresy ('OR') PRZED filtrem wyprzedzenia ({len(merged_all_free_ranges)}) ---")
    if merged_all_free_ranges:
        for i, s in enumerate(merged_all_free_ranges[:15]):
            logging.debug(f"  Scalony Slot {i+1}: {s['start']:%H:%M}-{s['end']:%H:%M}")
        if len(merged_all_free_ranges) > 15:
            logging.debug("  ...")

    # --- Krok 5: Zastosuj filtr MIN_BOOKING_LEAD_HOURS ---
    final_filtered_slots = []
    min_start_time = now + datetime.timedelta(hours=MIN_BOOKING_LEAD_HOURS)
    logging.debug(f"Minimalny czas startu (filtr {MIN_BOOKING_LEAD_HOURS}h): {min_start_time:%Y-%m-%d %H:%M %Z}")
    for slot in merged_all_free_ranges:
        effective_start = max(slot['start'], min_start_time)
        if effective_start < slot['end'] and (slot['end'] - effective_start) >= min_duration_delta:
            final_filtered_slots.append({'start': effective_start, 'end': slot['end']})
            if effective_start != slot['start']:
                logging.debug(f"  Zmodyfikowano {slot['start']:%H:%M}-{slot['end']:%H:%M} na {effective_start:%H:%M}-{slot['end']:%H:%M} (filtr {MIN_BOOKING_LEAD_HOURS}h)")

    logging.info(f"Znaleziono {len(final_filtered_slots)} wolnych zakresów (Logika 'OR', Filtr Arkusza Per Kalendarz, po wszystkich filtrach).")
    if final_filtered_slots:
        for i, slot in enumerate(final_filtered_slots[:10]):
            logging.debug(f"  Finalny Slot {i+1}: {slot['start']:%Y-%m-%d %H:%M %Z} - {slot['end']:%Y-%m-%d %H:%M %Z}")
        if len(final_filtered_slots) > 10:
            logging.debug("  ...")

    return final_filtered_slots

def is_slot_actually_free(start_time, calendar_id):
    """Weryfikuje w czasie rzeczywistym, czy slot jest wolny w danym Kalendarzu Google."""
    service = get_calendar_service()
    tz = _get_calendar_timezone()
    if not service:
        logging.error(f"Błąd: Usługa kalendarza niedostępna (weryfikacja {calendar_id}).")
        return False
    if not isinstance(start_time, datetime.datetime):
        logging.error(f"Błąd weryfikacji {calendar_id}: start_time typ {type(start_time)}")
        return False

    if start_time.tzinfo is None:
        start_time = tz.localize(start_time)
    else:
        start_time = start_time.astimezone(tz)
    end_time = start_time + datetime.timedelta(minutes=APPOINTMENT_DURATION_MINUTES)
    query_start_time = start_time + datetime.timedelta(seconds=1)
    query_end_time = end_time - datetime.timedelta(seconds=1)

    if query_start_time >= query_end_time:
        logging.warning(f"Weryfikacja {calendar_id}: Slot {start_time:%H:%M}-{end_time:%H:%M} za krótki po buforze.")
        return False

    body = {
        "timeMin": query_start_time.isoformat(),
        "timeMax": query_end_time.isoformat(),
        "timeZone": CALENDAR_TIMEZONE,
        "items": [{"id": calendar_id}]
    }
    try:
        cal_name = CALENDAR_ID_TO_NAME.get(calendar_id, calendar_id)
        logging.debug(f"Weryfikacja free/busy dla '{cal_name}': {start_time:%Y-%m-%d %H:%M} - {end_time:%Y-%m-%d %H:%M}")
        freebusy_result = service.freebusy().query(body=body).execute()
        calendar_data = freebusy_result.get('calendars', {}).get(calendar_id, {})

        if 'errors' in calendar_data:
            for error in calendar_data['errors']:
                logging.error(f"Błąd API Freebusy (weryfikacja) dla '{cal_name}': {error.get('reason')} - {error.get('message')}")
            return False

        busy_times = calendar_data.get('busy', [])
        if not busy_times:
            logging.info(f"Weryfikacja '{cal_name}': Slot {start_time:%Y-%m-%d %H:%M} JEST wolny.")
            return True
        else:
            for busy in busy_times:
                busy_start = parse_event_time({'dateTime': busy['start']}, tz)
                busy_end = parse_event_time({'dateTime': busy['end']}, tz)
                if busy_start and busy_end and max(start_time, busy_start) < min(end_time, busy_end):
                    logging.warning(f"Weryfikacja '{cal_name}': Slot {start_time:%Y-%m-%d %H:%M} jest ZAJĘTY przez: {busy_start:%H:%M} - {busy_end:%H:%M}")
                    return False
            logging.info(f"Weryfikacja '{cal_name}': Slot {start_time:%Y-%m-%d %H:%M} JEST wolny (zwrócone busy nie kolidowały: {busy_times}).")
            return True
    except HttpError as error:
        logging.error(f"Błąd HTTP API Freebusy (weryfikacja) dla '{calendar_id}': {error.resp.status} {error.resp.reason}", exc_info=True)
        return False
    except Exception as e:
        logging.error(f"Nieoczekiwany błąd weryfikacji Freebusy dla '{calendar_id}': {e}", exc_info=True)
        return False

def format_ranges_for_ai(ranges):
    """Formatuje listę zakresów czasowych dla AI."""
    if not ranges:
        return "Brak dostępnych zakresów czasowych w podanym okresie."

    tz = _get_calendar_timezone()
    formatted_lines = [
        f"Dostępne ZAKRESY (wizyta trwa {APPOINTMENT_DURATION_MINUTES} minut).",
        "--- Dostępne Zakresy (Data YYYY-MM-DD, Dzień, Od HH:MM, Do HH:MM) ---"
    ]
    slots_added = 0
    max_slots_to_show = 15
    sorted_ranges = sorted(ranges, key=lambda r: r['start'])
    min_duration = datetime.timedelta(minutes=APPOINTMENT_DURATION_MINUTES)

    for r in sorted_ranges:
        start_dt = r['start'].astimezone(tz)
        end_dt = r['end'].astimezone(tz)
        if (end_dt - start_dt) >= min_duration:
            try:
                day_name = start_dt.strftime('%A').capitalize()
            except Exception:
                day_name = POLISH_WEEKDAYS[start_dt.weekday()]
            date_str = start_dt.strftime('%Y-%m-%d')
            start_time_str = start_dt.strftime('%H:%M')
            end_time_str = end_dt.strftime('%H:%M')
            formatted_lines.append(f"- {date_str}, {day_name}, od {start_time_str}, do {end_time_str}")
            slots_added += 1
            if slots_added >= max_slots_to_show:
                formatted_lines.append("- ... (i potencjalnie więcej)")
                break

    if slots_added == 0:
        return "Brak dostępnych zakresów czasowych (mieszczących wizytę) w podanym okresie."

    formatted_output = "\n".join(formatted_lines)
    logging.debug(f"--- Zakresy sformatowane dla AI ({slots_added} pokazanych) ---\n{formatted_output}\n---------------------------------")
    return formatted_output

# =====================================================================
# === FUNKCJE GOOGLE SHEETS (ZAPIS + ODCZYT) ==========================
# =====================================================================

def get_sheets_service():
    """Inicjalizuje (i cachuje) usługę Google Sheets API."""
    global _sheets_service
    if _sheets_service:
        return _sheets_service
    if not os.path.exists(SHEETS_SERVICE_ACCOUNT_FILE):
        logging.error(f"KRYTYCZNY BŁĄD: Brak pliku klucza '{SHEETS_SERVICE_ACCOUNT_FILE}'")
        return None
    try:
        creds = service_account.Credentials.from_service_account_file(
            SHEETS_SERVICE_ACCOUNT_FILE, scopes=SHEET_SCOPES
        )
        _sheets_service = build('sheets', 'v4', credentials=creds, cache_discovery=False)
        logging.info(f"Utworzono połączenie z Google Sheets API (odczyt/zapis) używając '{SHEETS_SERVICE_ACCOUNT_FILE}'.")
        return _sheets_service
    except Exception as e:
        logging.error(f"Błąd tworzenia usługi Google Sheets: {e}", exc_info=True)
        return None

def find_row_by_psid(psid):
    """Szuka wiersza w arkuszu na podstawie PSID."""
    service = get_sheets_service()
    if not service:
        logging.error("Błąd: Usługa arkuszy niedostępna (szukanie PSID).")
        return None
    try:
        read_range = SHEET_READ_RANGE_FOR_PSID_SEARCH
        logging.debug(f"Szukanie PSID {psid} w '{SHEET_NAME}' zakres '{read_range}'")
        result = service.spreadsheets().values().get(
            spreadsheetId=SPREADSHEET_ID, range=read_range
        ).execute()
        values = result.get('values', [])
        if not values:
            logging.debug(f"Arkusz '{SHEET_NAME}' pusty/brak PSID.")
            return None
        for i, row in enumerate(values):
            if row and row[0].strip() == psid:
                row_number = i + 2
                logging.info(f"Znaleziono PSID {psid} w wierszu {row_number}.")
                return row_number
        logging.info(f"Nie znaleziono PSID {psid} w arkuszu.")
        return None
    except HttpError as error:
        logging.error(f"Błąd HTTP API szukania PSID: {error.resp.status} {error.resp.reason}", exc_info=True)
        return None
    except Exception as e:
        logging.error(f"Nieoczekiwany błąd szukania PSID: {e}", exc_info=True)
        return None

def write_to_sheet_phase1(psid, start_time, calendar_name):
    """Zapisuje dane Fazy 1 (PSID, Data, Czas, Nazwa Kalendarza) do arkusza (APPEND)."""
    service = get_sheets_service()
    if not service:
        return False, "Błąd połączenia z Google Sheets (Faza 1 - Append)."
    tz = _get_sheet_timezone()
    if start_time.tzinfo is None:
        start_time = tz.localize(start_time)
    else:
        start_time = start_time.astimezone(tz)
    date_str = start_time.strftime('%Y-%m-%d')
    time_str = start_time.strftime('%H:%M')
    data_row = [""] * SHEET_CALENDAR_NAME_COLUMN_INDEX
    data_row[SHEET_PSID_COLUMN_INDEX - 1] = psid
    data_row[SHEET_DATE_COLUMN_INDEX - 1] = date_str
    data_row[SHEET_TIME_COLUMN_INDEX - 1] = time_str
    data_row[SHEET_CALENDAR_NAME_COLUMN_INDEX - 1] = calendar_name
    try:
        range_name = f"{SHEET_NAME}!A1"
        body = {'values': [data_row]}
        logging.info(f"Próba zapisu Fazy 1 (Append) do '{SHEET_NAME}': {data_row}")
        result = service.spreadsheets().values().append(
            spreadsheetId=SPREADSHEET_ID, range=range_name,
            valueInputOption='USER_ENTERED', insertDataOption='INSERT_ROWS', body=body
        ).execute()
        updated_range = result.get('updates', {}).get('updatedRange', '')
        logging.info(f"Zapisano Faza 1 (Append) pomyślnie w zakresie {updated_range}")
        match = re.search(rf"{re.escape(SHEET_NAME)}!A(\d+):", updated_range)
        row_index = int(match.group(1)) if match else None
        if row_index:
            logging.info(f"Zapisano Faza 1 (Append) w wierszu: {row_index}")
            return True, row_index
        else:
            logging.warning(f"Nie udało się wyodrębnić numeru wiersza z: {updated_range}")
            return True, None
    except HttpError as error:
        error_details = f"{error.resp.status} {error.resp.reason}"
        logging.error(f"Błąd API Fazy 1 (Append): {error}, Szczegóły: {error_details}", exc_info=True)
        return False, f"Błąd zapisu Fazy 1 ({error_details})."
    except Exception as e:
        logging.error(f"Błąd Python Fazy 1 (Append): {e}", exc_info=True)
        return False, "Wewnętrzny błąd systemu Fazy 1."

def update_sheet_phase2(student_data, sheet_row_index):
    """Aktualizuje wiersz danymi Fazy 2 (używając tylko numeru klasy dla kol. H)."""
    service = get_sheets_service()
    if not service:
        return False, "Błąd połączenia z Google Sheets (Faza 2)."
    if sheet_row_index is None:
        logging.error("Brak indeksu wiersza do aktualizacji Fazy 2.")
        return False, "Brak info o wierszu."
    try:
        parent_fn = student_data.get('parent_first_name', '')
        parent_ln = student_data.get('parent_last_name', '')
        student_fn = student_data.get('student_first_name', '')
        student_ln = student_data.get('student_last_name', '')
        grade_info = student_data.get('grade_info', '')
        level_info = student_data.get('level_info', '')
        numerical_grade, _, school_type = extract_school_type(grade_info)
        logging.info(f"Dane do Fazy 2: NumerKlasy='{numerical_grade}', TypSzkoły='{school_type}', Poziom='{level_info}'")
        update_data_p1 = [parent_fn, parent_ln, student_fn, student_ln]
        update_data_p2 = [numerical_grade, school_type, level_info]
        range_p1 = f"{SHEET_NAME}!{chr(ord('A')+SHEET_PARENT_FN_COLUMN_INDEX-1)}{sheet_row_index}:{chr(ord('A')+SHEET_STUDENT_LN_COLUMN_INDEX-1)}{sheet_row_index}"
        range_p2 = f"{SHEET_NAME}!{chr(ord('A')+SHEET_GRADE_COLUMN_INDEX-1)}{sheet_row_index}:{chr(ord('A')+SHEET_LEVEL_COLUMN_INDEX-1)}{sheet_row_index}"
        body1 = {'values': [update_data_p1]}
        body2 = {'values': [update_data_p2]}
        logging.info(f"Aktualizacja Fazy 2 (cz. 1) wiersz {sheet_row_index} zakres {range_p1} danymi: {update_data_p1}")
        result1 = service.spreadsheets().values().update(spreadsheetId=SPREADSHEET_ID, range=range_p1, valueInputOption='USER_ENTERED', body=body1).execute()
        logging.info(f"Zaktualizowano Faza 2 (cz. 1): {result1.get('updatedCells')} komórek.")
        logging.info(f"Aktualizacja Fazy 2 (cz. 2) wiersz {sheet_row_index} zakres {range_p2} danymi: {update_data_p2}")
        result2 = service.spreadsheets().values().update(spreadsheetId=SPREADSHEET_ID, range=range_p2, valueInputOption='USER_ENTERED', body=body2).execute()
        logging.info(f"Zaktualizowano Faza 2 (cz. 2): {result2.get('updatedCells')} komórek.")
        return True, None
    except HttpError as error:
        error_details = f"{error.resp.status} {error.resp.reason}"
        logging.error(f"Błąd API Fazy 2: {error}, Szczegóły: {error_details}", exc_info=True)
        return False, f"Błąd aktualizacji Fazy 2 ({error_details})."
    except Exception as e:
        logging.error(f"Błąd Python Fazy 2: {e}", exc_info=True)
        return False, "Wewnętrzny błąd systemu Fazy 2."

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
        logging.error(f"!!! [{recipient_id}] Brak tokena strony. NIE WYSŁANO.")
        return False
    params = {"access_token": PAGE_ACCESS_TOKEN}
    payload = {"recipient": {"id": recipient_id}, "message": {"text": message_text}, "messaging_type": "RESPONSE"}
    try:
        r = requests.post(FACEBOOK_GRAPH_API_URL, params=params, json=payload, timeout=30)
        r.raise_for_status()
        response_json = r.json()
        if fb_error := response_json.get('error'):
            logging.error(f"!!! BŁĄD FB API wysyłania: {fb_error} !!!")
            if fb_error.get('code') == 190:
                logging.error("!!! Token FB_PAGE_ACCESS_TOKEN nieprawidłowy/wygasł !!!")
            return False
        logging.debug(f"[{recipient_id}] Fragment wysłany (Msg ID: {response_json.get('message_id')}).")
        return True
    except requests.exceptions.Timeout:
        logging.error(f"!!! BŁĄD TIMEOUT wysyłania do {recipient_id} !!!")
        return False
    except requests.exceptions.HTTPError as http_err:
        logging.error(f"!!! BŁĄD HTTP {http_err.response.status_code} wysyłania do {recipient_id}: {http_err} !!!")
        if http_err.response is not None:
            try:
                logging.error(f"Odpowiedź FB (HTTP Err): {http_err.response.json()}")
            except json.JSONDecodeError:
                logging.error(f"Odpowiedź FB (HTTP Err, !JSON): {http_err.response.text}")
        return False
    except requests.exceptions.RequestException as req_err:
        logging.error(f"!!! BŁĄD RequestException wysyłania do {recipient_id}: {req_err} !!!")
        return False
    except Exception as e:
        logging.error(f"!!! Nieoczekiwany BŁĄD wysyłania do {recipient_id}: {e} !!!", exc_info=True)
        return False

def send_message(recipient_id, full_message_text):
    """Wysyła wiadomość, dzieląc ją w razie potrzeby."""
    if not full_message_text or not isinstance(full_message_text, str) or not full_message_text.strip():
        logging.warning(f"[{recipient_id}] Pominięto wysłanie pustej wiadomości.")
        return

    message_len = len(full_message_text)
    logging.info(f"[{recipient_id}] Przygotowanie wiadomości (dł: {message_len}).")

    if ENABLE_TYPING_DELAY:
        est_typing = min(MAX_TYPING_DELAY_SECONDS, max(MIN_TYPING_DELAY_SECONDS, message_len / TYPING_CHARS_PER_SECOND))
        logging.debug(f"[{recipient_id}] Szacowany czas pisania: {est_typing:.2f}s")
        _send_typing_on(recipient_id)
        time.sleep(est_typing)

    chunks = []
    if message_len <= MESSAGE_CHAR_LIMIT:
        chunks.append(full_message_text)
    else:
        logging.info(f"[{recipient_id}] Dzielenie wiadomości ({message_len} > {MESSAGE_CHAR_LIMIT})...")
        remaining = full_message_text
        while remaining:
            if len(remaining) <= MESSAGE_CHAR_LIMIT:
                chunks.append(remaining.strip())
                break
            split_idx = -1
            search_limit = MESSAGE_CHAR_LIMIT
            for delim in ['\n\n', '\n', '. ', '! ', '? ', ' ']:
                idx = remaining.rfind(delim, 0, search_limit)
                if idx != -1:
                    split_idx = idx + len(delim)
                    break
            if split_idx == -1:
                split_idx = MESSAGE_CHAR_LIMIT

            chunk = remaining[:split_idx].strip()
            if chunk:
                chunks.append(chunk)
            remaining = remaining[split_idx:].strip()
        logging.info(f"[{recipient_id}] Podzielono na {len(chunks)} fragmentów.")

    num_chunks = len(chunks)
    send_ok_count = 0
    for i, chunk in enumerate(chunks):
        logging.debug(f"[{recipient_id}] Wysyłanie fragmentu {i+1}/{num_chunks}...")
        if not _send_single_message(recipient_id, chunk):
            logging.error(f"!!! [{recipient_id}] Błąd wysyłania fragmentu {i+1}. Anulowanie reszty.")
            break
        send_ok_count += 1

        if num_chunks > 1 and i < num_chunks - 1:
            logging.debug(f"[{recipient_id}] Oczekiwanie {MESSAGE_DELAY_SECONDS}s...")
            if ENABLE_TYPING_DELAY:
                next_len = len(chunks[i+1])
                est_next = min(MAX_TYPING_DELAY_SECONDS * 0.7, max(MIN_TYPING_DELAY_SECONDS * 0.5, next_len / TYPING_CHARS_PER_SECOND))
                _send_typing_on(recipient_id)
                wait = min(est_next, MESSAGE_DELAY_SECONDS * 0.8)
                time.sleep(wait)
                remain_delay = max(0, MESSAGE_DELAY_SECONDS - wait)
                if remain_delay > 0:
                    time.sleep(remain_delay)
            else:
                time.sleep(MESSAGE_DELAY_SECONDS)

    logging.info(f"--- [{recipient_id}] Zakończono wysyłanie. Wysłano {send_ok_count}/{num_chunks} fragmentów. ---")

def _simulate_typing(recipient_id, duration_seconds):
    """Wysyła 'typing_on' i czeka."""
    if ENABLE_TYPING_DELAY and duration_seconds > 0:
        _send_typing_on(recipient_id)
        time.sleep(min(duration_seconds, MAX_TYPING_DELAY_SECONDS * 1.1))

# =====================================================================
# === FUNKCJE WYWOŁANIA AI ============================================
# =====================================================================

def _call_gemini(user_psid, prompt_history, generation_config, task_name, max_retries=3):
    """Wywołuje API Gemini z obsługą błędów i ponowień."""
    if not gemini_model:
        logging.error(f"!!! [{user_psid}] KRYTYCZNY BŁĄD: Model Gemini ({task_name}) niedostępny!")
        return None
    if not isinstance(prompt_history, list) or not all(isinstance(item, Content) for item in prompt_history):
        logging.error(f"!!! [{user_psid}] Nieprawidłowy format promptu ({task_name}).")
        return None

    logging.info(f"[{user_psid}] Wywołanie Gemini: {task_name} (Prompt: {len(prompt_history)} wiad.)")
    last_user_msg = next((msg.parts[0].text for msg in reversed(prompt_history) if msg.role == 'user' and msg.parts), None)
    if last_user_msg:
        log_msg = f"'{last_user_msg[:200]}{'...' if len(last_user_msg)>200 else ''}'"
        logging.debug(f"    Ostatnia wiad. usera ({task_name}): {log_msg}")
    else:
        logging.debug(f"    Brak wiadomości użytkownika w prompcie ({task_name}).")

    attempt = 0
    finish_reason = None
    response = None
    candidate = None

    while attempt < max_retries:
        attempt += 1
        logging.debug(f"    Próba {attempt}/{max_retries} ({task_name})...")
        try:
            _simulate_typing(user_psid, MIN_TYPING_DELAY_SECONDS * 0.8)
            response = gemini_model.generate_content(
                prompt_history,
                generation_config=generation_config,
                safety_settings=SAFETY_SETTINGS,
                stream=False
            )

            if response and response.candidates:
                if not response.candidates:
                    logging.warning(f"[{user_psid}] Gemini ({task_name}) brak kandydatów.")
                    if attempt < max_retries:
                        time.sleep(1.5 * attempt)
                        continue
                    else:
                        return "Przepraszam, problem z generowaniem odpowiedzi (brak kandydatów)."

                candidate = response.candidates[0]
                finish_reason = candidate.finish_reason if hasattr(candidate, 'finish_reason') else None

                if finish_reason != 1: # 1 = FINISH_REASON_STOP
                    safety_ratings = candidate.safety_ratings if hasattr(candidate, 'safety_ratings') else "Brak"
                    finish_reason_name = finish_reason.name if hasattr(finish_reason, 'name') else str(finish_reason)
                    logging.warning(f"[{user_psid}] Gemini ({task_name}) ZAKOŃCZONE NIEPRAWIDŁOWO! Powód: {finish_reason_name} ({finish_reason}). Safety: {safety_ratings}")

                    if finish_reason in [3, 4] and attempt < max_retries:
                        logging.warning(f"    Ponawianie ({attempt}/{max_retries}) z powodu blokady ({finish_reason_name})...")
                        time.sleep(1.5 * attempt)
                        continue
                    elif finish_reason == 2 and attempt < max_retries:
                        logging.warning(f"    Odpowiedź ucięta (MAX_TOKENS). Ponawianie ({attempt}/{max_retries})...")
                        time.sleep(1.5 * attempt)
                        continue
                    else:
                        logging.error(f"!!! [{user_psid}] Gemini ({task_name}) nieudane po {attempt} próbach ({finish_reason_name}).")
                        if finish_reason == 3: return "Przepraszam, nie mogę przetworzyć tej prośby ze względu na zasady bezpieczeństwa."
                        if finish_reason == 4: return "Przepraszam, nie mogę wygenerować odpowiedzi z powodu zasad cytowania."
                        if finish_reason == 2: return "Przepraszam, wygenerowana odpowiedź była zbyt długa."
                        return f"Przepraszam, wystąpił problem z generowaniem odpowiedzi (kod: {finish_reason_name})."

                if hasattr(candidate, 'content') and candidate.content and hasattr(candidate.content, 'parts') and candidate.content.parts:
                    generated_text = "".join(part.text for part in candidate.content.parts if hasattr(part, 'text')).strip()
                    if generated_text:
                        logging.info(f"[{user_psid}] Gemini ({task_name}) zwróciło odpowiedź (dł: {len(generated_text)}).")
                        logging.debug(f"    Odpowiedź Gemini ({task_name}): '{generated_text}'")
                        return generated_text
                    else:
                        logging.warning(f"[{user_psid}] Gemini ({task_name}) zwróciło pustą treść (Finish: STOP).")
                        if attempt < max_retries:
                            time.sleep(1.5 * attempt)
                            continue
                        else:
                            return "Przepraszam, problem z wygenerowaniem odpowiedzi (pusta treść)."
                else:
                    finish_reason_name = finish_reason.name if hasattr(finish_reason, 'name') else str(finish_reason)
                    logging.warning(f"[{user_psid}] Gemini ({task_name}) zwróciło kandydata bez treści (Finish: {finish_reason_name}).")
                    if attempt < max_retries:
                        time.sleep(1.5 * attempt)
                        continue
                    else:
                        return "Przepraszam, problem z wygenerowaniem odpowiedzi (brak treści)."
            else:
                prompt_feedback = response.prompt_feedback if hasattr(response, 'prompt_feedback') else 'Brak'
                logging.error(f"!!! BŁĄD [{user_psid}] Gemini ({task_name}) - Brak kandydatów. Feedback: {prompt_feedback}.")
                if attempt < max_retries:
                    time.sleep(1.5 * attempt)
                    continue
                else:
                    return "Przepraszam, problem z generowaniem odpowiedzi (brak kandydatów)."

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
                logging.error(f"    Nie ponawiam błędu HTTP {status_code}.")
                return f"Przepraszam, błąd komunikacji z AI (HTTP {status_code}). Spróbuj później."
        except Exception as e:
            if isinstance(e, NameError) and 'gemini_model' in str(e):
                logging.critical(f"!!! KRYTYCZNY NameError [{user_psid}]: {e}. gemini_model jest None!", exc_info=True)
                return "Przepraszam, krytyczny błąd wewnętrzny (brak modelu AI)."
            else:
                logging.error(f"!!! BŁĄD [{user_psid}] Gemini ({task_name}) - Python (Próba {attempt}/{max_retries}): {e}", exc_info=True)
                if attempt < max_retries:
                    sleep_time = (2 ** attempt) + (random.random() * 0.5)
                    logging.warning(f"    Nieoczekiwany błąd Python. Oczekiwanie {sleep_time:.2f}s...")
                    time.sleep(sleep_time)
                    continue
                else:
                    logging.error(f"    Nie ponawiam błędu Python po {max_retries} próbach.")
                    return "Przepraszam, nieoczekiwany błąd przetwarzania."

        logging.error(f"!!! [{user_psid}] Niespodziewanie osiągnięto koniec pętli _call_gemini ({task_name}) (Próba {attempt}/{max_retries}).")
        if attempt < max_retries:
            time.sleep(1.5 * attempt)
            continue

    logging.error(f"!!! KRYTYCZNY BŁĄD [{user_psid}] Gemini ({task_name}) - Nie udało się uzyskać odpowiedzi po {max_retries} próbach.")
    if finish_reason == 3: return "Przepraszam, nie mogę przetworzyć tej prośby ze względu na zasady bezpieczeństwa."
    if finish_reason == 4: return "Przepraszam, nie mogę wygenerować odpowiedzi z powodu zasad cytowania."
    if finish_reason == 2: return "Przepraszam, wygenerowana odpowiedź była zbyt długa."
    return "Przepraszam, nie udało się przetworzyć wiadomości po kilku próbach. Spróbuj ponownie później."

# =====================================================================
# === INSTRUKCJE SYSTEMOWE I GŁÓWNE FUNKCJE AI ========================
# =====================================================================
# Przywrócone instrukcje systemowe

# --- SYSTEM_INSTRUCTION_SCHEDULING ---
SYSTEM_INSTRUCTION_SCHEDULING = """Jesteś pomocnym asystentem AI specjalizującym się w umawianiu terminów korepetycji online. Twoim zadaniem jest znalezienie pasującego terminu dla użytkownika na podstawie jego preferencji oraz dostarczonej listy dostępnych zakresów czasowych z kalendarza.

**Kontekst:**
*   Rozmawiasz z użytkownikiem, który wyraził chęć umówienia się na lekcję.
*   Poniżej znajduje się lista AKTUALNIE dostępnych ZAKRESÓW czasowych z kalendarza, w których można umówić wizytę (każda trwa {duration} minut). **Wszystkie podane zakresy są już odpowiednio odsunięte w czasie (filtr {min_lead_hours}h) i gotowe do zaproponowania.**
*   Masz dostęp do historii poprzedniej rozmowy. Czasami rozmowa mogła zostać przerwana pytaniem ogólnym i teraz do niej wracamy.

**Styl Komunikacji:**
*   **Naturalność:** Zachowuj się jak człowiek, unikaj schematycznych i powtarzalnych odpowiedzi.
*   **Umiar:** Nie używaj nadmiernie wykrzykników ani entuzjastycznych sformułowań typu "Super!", "Jasne!".
*   **Bez Emotek:** Nie używaj emotikon.
*   **Profesjonalizm:** Bądź uprzejmy, rzeczowy i pomocny. Używaj zwrotów "Państwo".
*   **Język:** Odpowiadaj płynnie po polsku.

**Dostępne zakresy czasowe z kalendarza:**
{available_ranges_text}

**Twoje zadanie:**
1.  **Rozpocznij rozmowę LUB WZNÓW:** Jeśli to początek umawiania lub jeśli ostatnia wiadomość użytkownika nie dotyczyła preferencji terminu (np. było to podziękowanie po odpowiedzi na pytanie ogólne), potwierdź, że widzisz dostępne terminy i zapytaj użytkownika o jego **ogólne preferencje** dotyczące dnia tygodnia lub pory dnia (np. "Mamy kilka wolnych terminów. Czy preferują Państwo jakiś konkretny dzień tygodnia lub porę dnia - rano, popołudnie, wieczór?"). **Nie proponuj jeszcze konkretnej daty i godziny.** Odpowiadaj na ewentualne pytania użytkownika dotyczące dostępności lub procesu umawiania.
2.  **Negocjuj:** Na podstawie odpowiedzi użytkownika **dotyczącej preferencji terminu**, historii konwersacji i **wyłącznie dostępnych zakresów z listy**, kontynuuj rozmowę, aby znaleźć termin pasujący obu stronom. Gdy użytkownik poda preferencje, **zaproponuj konkretny termin z listy**, który im odpowiada (np. "W takim razie, może środa o 17:00?"). Jeśli ostatnia wiadomość użytkownika nie była odpowiedzią na pytanie o termin, wróć do kroku 1. Odpowiadaj na pytania dotyczące proponowanych terminów.
3.  **Potwierdź i dodaj znacznik:** Kiedy wspólnie ustalicie **dokładny termin** (np. "Środa, 15 maja o 18:30"), który **znajduje się na liście dostępnych zakresów**, potwierdź go w swojej odpowiedzi (np. "Świetnie, w takim razie proponowany termin to środa, 15 maja o 18:30.") i **zakończ swoją odpowiedź potwierdzającą DOKŁADNIE znacznikiem** `{slot_marker_prefix}YYYY-MM-DDTHH:MM:SS{slot_marker_suffix}`. Użyj formatu ISO 8601 dla ustalonego czasu rozpoczęcia (np. 2024-05-15T18:30:00). Upewnij się, że data i godzina w znaczniku są poprawne, zgodne z ustaleniami i **pochodzą z listy dostępnych zakresów**.
4.  **NIE dodawaj znacznika**, jeśli:
    *   Użytkownik jeszcze się zastanawia lub prosi o więcej opcji.
    *   Użytkownik proponuje termin, którego nie ma na liście dostępnych zakresów.
    *   Nie udało się znaleźć pasującego terminu.
    *   Lista dostępnych zakresów jest pusta.
5.  **Brak terminów:** Jeśli lista zakresów jest pusta lub po rozmowie okaże się, że żaden termin nie pasuje, poinformuj o tym użytkownika uprzejmie. Nie dodawaj znacznika.
6.  **Pytania poza tematem:** Jeśli użytkownik zada pytanie **niezwiązane bezpośrednio z ustalaniem terminu z listy** (np. o cenę, metodykę, dostępne przedmioty), **NIE ODPOWIADAJ na nie**. Zamiast tego, Twoja odpowiedź musi zawierać **TYLKO I WYŁĄCZNIE** znacznik: `{switch_marker}`. System przełączy się wtedy do trybu ogólnych odpowiedzi.

**Pamiętaj:**
*   Trzymaj się **wyłącznie** terminów i godzin wynikających z "Dostępnych zakresów czasowych".
*   Bądź elastyczny w rozmowie, ale propozycje muszą pochodzić z listy.
*   Używaj języka polskiego i polskiej strefy czasowej ({calendar_timezone}).
*   Znacznik `{slot_marker_prefix}...{slot_marker_suffix}` jest sygnałem dla systemu, że **osiągnięto porozumienie co do terminu z dostępnej listy**. Używaj go tylko w tym jednym, konkretnym przypadku.
*   Znacznik `{switch_marker}` służy do przekazania obsługi pytania ogólnego.
*   Nie podawaj zakresów wolnych terminów staraj się pytać raczej o preferencje i dawać konkretne daty
""".format(
    duration=APPOINTMENT_DURATION_MINUTES, min_lead_hours=MIN_BOOKING_LEAD_HOURS,
    available_ranges_text="{available_ranges_text}", calendar_timezone=CALENDAR_TIMEZONE,
    slot_marker_prefix=SLOT_ISO_MARKER_PREFIX, slot_marker_suffix=SLOT_ISO_MARKER_SUFFIX,
    switch_marker=SWITCH_TO_GENERAL
)

# --- SYSTEM_INSTRUCTION_GATHERING ---
SYSTEM_INSTRUCTION_GATHERING = """Rozmawiasz z klientem. Twoim zadaniem jest zebranie informacji wyłącznie o UCZNIU, potrzebnych do zapisu na korepetycje, po tym jak wstępnie ustalono termin. Dane rodzica zostaną pobrane automatycznie przez system.

**Kontekst:**
*   Wstępnie ustalony termin lekcji to: {proposed_slot_formatted}
*   Masz dostęp do historii rozmowy.
*   Informacje o UCZNIU już znane (mogą być puste):
    *   Imię ucznia: {known_student_first_name}
    *   Nazwisko ucznia: {known_student_last_name}
    *   Klasa/Szkoła: {known_grade} # Pełna informacja, np. "3 klasa liceum"
    *   Poziom (dla liceum/technikum): {known_level} # Np. "Podstawowy", "Rozszerzony" lub "Brak"
*  Zbieraj dane ucznia i ogranicz się do tego, ewent. możesz wytłumaczyc do czego są potrzebne. Gdy zbierzesz informację wymagane postępuj zgodnie z instrukcją

**Styl Komunikacji:**
*   **Naturalność:** Zachowuj się jak człowiek, unikaj schematycznych i powtarzalnych odpowiedzi.
*   **Umiar:** Nie używaj nadmiernie wykrzykników ani entuzjastycznych sformułowań typu "Super!", "Jasne!".
*   **Bez Emotek:** Nie używaj emotikon.
*   **Profesjonalizm:** Bądź uprzejmy, rzeczowy i pomocny. Używaj zwrotów "Państwo".
*   **Język:** Odpowiadaj płynnie po polsku.

**Twoje zadania:**
1.  **Przeanalizuj znane informacje o UCZNIU:** Sprawdź powyższe "Informacje o UCZNIU już znane" oraz historię rozmowy.
2.  **ZDOBĄDŹ INFORMACJE OD KLIENTA:** Uprzejmie poproś użytkownika o podanie **tylko tych informacji o uczniu, których jeszcze brakuje**. Wymagane informacje to:
    *   **Pełne Imię i Nazwisko UCZNIA**.
    *   **Klasa**, do której uczęszcza uczeń ORAZ **typ szkoły** (np. "7 klasa podstawówki", "1 klasa liceum", "3 klasa technikum"). Poproś o podanie obu informacji, jeśli brakuje.
    *   **Poziom nauczania** (podstawowy czy rozszerzony) - **zapytaj o to TYLKO jeśli z podanej klasy/szkoły wynika, że jest to liceum lub technikum**. Odpowiadaj na pytania użytkownika dotyczące zbieranych danych.
3.  **IGNORUJ i NIE PYTAJ o dane rodzica.** System zajmie się tym automatycznie.
4.  **Prowadź rozmowę:** Zadawaj pytania dotyczące ucznia pojedynczo lub połącz kilka, jeśli brakuje więcej danych. Bądź miły i konwersacyjny. Potwierdzaj zrozumienie odpowiedzi użytkownika.
5.  **Zakończ po zebraniu danych UCZNIA:** Kiedy uznasz, że masz już **wszystkie wymagane informacje o UCZNIU** (Imię, Nazwisko, Klasa+Szkoła, ewentualnie Poziom), Twoja ostatnia odpowiedź **MUSI** mieć następującą strukturę:
    a)  **DOKŁADNIE** linijka w formacie: `ZEBRANE_DANE_UCZNIA: [Imię: <imię>, Nazwisko: <nazwisko>, KlasaInfo: <pełna informacja o klasie i szkole np. 3 klasa liceum>, Poziom: <Podstawowy/Rozszerzony/brak>]` (Zastąp <...> zebranymi danymi. Jeśli poziom nie dotyczy lub nie został podany, wpisz "brak").
    b)  **PO TEJ LINIJCE**, w nowej linii, standardowa wiadomość dla użytkownika: "Dobrze, dziękujemy za wszystkie informacje. Aby lekcja się odbyła prosimy jeszcze o potwierdzenie zajęć wysyłając wiadomość "POTWIERDZAM" na podany profil. Jest to profil także to dalszego kontaktu w sprawie zajęć: https://www.facebook.com/profile.php?id=61576135251276"
    c)  **NA SAMYM KOŃCU** całej odpowiedzi dodaj **DOKŁADNIE** znacznik: `{info_gathered_marker}`.
6.  **NIE dodawaj znacznika ani struktury `ZEBRANE_DANE_UCZNIA:`**, jeśli nadal brakuje którejś z wymaganych informacji o uczniu. Kontynuuj zadawanie pytań.
7.  **Pytania poza tematem:** Jeśli użytkownik zada pytanie **niezwiązane bezpośrednio ze zbieraniem danych ucznia** (np. o cenę, metodykę), **NIE ODPOWIADAJ na nie**. Zamiast tego, Twoja odpowiedź musi zawierać **TYLKO I WYŁĄCZNIE** znacznik: `{switch_marker}`. System przełączy się wtedy do trybu ogólnych odpowiedzi.

**Przykład poprawnej odpowiedzi końcowej:**
```
ZEBRANE_DANE_UCZNIA: [Imię: Jan, Nazwisko: Kowalski, KlasaInfo: 2 klasa liceum, Poziom: Rozszerzony]
Dobrze, dziękujemy za wszystkie informacje. Aby lekcja się odbyła prosimy jeszcze o potwierdzenie zajęć wysyłając wiadomość "POTWIERDZAM" na podany profil. Jest to profil także to dalszego kontaktu w sprawie zajęć: https://www.facebook.com/profile.php?id=61576135251276[INFO_GATHERED]
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

# --- SYSTEM_INSTRUCTION_GENERAL ---
SYSTEM_INSTRUCTION_GENERAL = """Jesteś przyjaznym, proaktywnym i profesjonalnym asystentem klienta centrum korepetycji. Twoim głównym celem jest przeprowadzenie klienta przez proces zapoznania się z ofertą i zachęcenie go do umówienia pierwszej lekcji (próbnej lub standardowej).

**Styl Komunikacji:**
*   **Naturalność:** Zachowuj się jak człowiek, unikaj schematycznych i powtarzalnych odpowiedzi.
*   **Umiar:** Nie używaj nadmiernie wykrzykników ani entuzjastycznych sformułowań typu "Super!", "Jasne!".
*   **Bez Emotek:** Nie używaj emotikon.
*   **Profesjonalizm:** Bądź uprzejmy, rzeczowy i pomocny. Używaj zwrotów "Państwo".
*   **Język:** Odpowiadaj płynnie po polsku.

**Dostępne Przedmioty:** Polski, Matematyka, Angielski.

**Cennik (za 60 minut):**
*   Szkoła Podstawowa: 60 zł
*   Liceum/Technikum (Poziom Podstawowy, klasa 1-2): 65 zł
*   Liceum/Technikum (Poziom Podstawowy, klasa 3-4/5): 70 zł
*   Liceum/Technikum (Poziom Rozszerzony, klasa 1): 65 zł
*   Liceum/Technikum (Poziom Rozszerzony, klasa 2): 70 zł
*   Liceum/Technikum (Poziom Rozszerzony, klasa 3-4/5): 75 zł

**Format Lekcji:** Online, przez platformę Microsoft Teams (bez konieczności instalacji, wystarczy link).

**Twój Przepływ Pracy:**

1.  **Powitanie i Identyfikacja Potrzeby:**
    *   Przywitaj się uprzejmie.
    *   Zapytaj, z jakiego przedmiotu uczeń potrzebuje korepetycji. Jeśli użytkownik nie podał przedmiotu, poinformuj o dostępnych (Polski, Matematyka, Angielski) i zapytaj ponownie. Odpowiadaj na ewentualne pytania użytkownika w tym zakresie.

2.  **Zbieranie Informacji o Uczniu:**
    *   Gdy znasz przedmiot, zapytaj o **klasę** ucznia oraz **typ szkoły** (podstawowa czy średnia - liceum/technikum). Staraj się uzyskać obie informacje.
    *   **Tylko jeśli** szkoła to liceum lub technikum, zapytaj o **poziom nauczania** (podstawowy czy rozszerzony).

3.  **Prezentacja Ceny i Formatu:**
    *   Na podstawie zebranych informacji (klasa, typ szkoły, poziom), **ustal właściwą cenę** z cennika.
    *   **Poinformuj klienta o cenie** za 60 minut lekcji dla danego poziomu, np. "Dla ucznia w [klasa] [typ szkoły] na poziomie [poziom] koszt zajęć wynosi [cena] zł za 60 minut.".
    *   **Dodaj informację o formacie:** "Wszystkie zajęcia odbywają się wygodnie online przez platformę Microsoft Teams - wystarczy kliknąć w link, nie trzeba nic instalować."

4.  **Zachęta do Umówienia Lekcji:**
    *   Po podaniu ceny i informacji o formacie, **bezpośrednio zapytaj**, czy klient jest zainteresowany umówieniem pierwszej lekcji (może być próbna), np. "Czy byliby Państwo zainteresowani umówieniem pierwszej lekcji, aby zobaczyć, jak pracujemy?".

5.  **Obsługa Odpowiedzi na Propozycję Lekcji:**
    *   **Jeśli TAK (lub podobna pozytywna odpowiedź):** Twoja odpowiedź musi zawierać **TYLKO I WYŁĄCZNIE** znacznik: `{intent_marker}`. System przejmie wtedy proces umawiania terminu.
    *   **Jeśli NIE (lub wahanie):**
        *   Zapytaj delikatnie o powód odmowy/wątpliwości.
        *   **Jeśli powodem jest forma ONLINE:** Wyjaśnij zalety: "Jeśli chodzi o formę online, chciałbym zapewnić, że nasi korepetytorzy to profesjonaliści z doświadczeniem w prowadzeniu zajęć zdalnych. Używamy interaktywnych narzędzi na platformie Teams, co sprawia, że lekcje są angażujące i efektywne – zupełnie inaczej niż mogło to wyglądać podczas nauki zdalnej w pandemii. Wszystko odbywa się przez przeglądarkę po kliknięciu w link."
        *   **Po wyjaśnieniu (lub jeśli powód był inny):** Zaproponuj lekcję próbną (płatną jak standardowa, bez zobowiązań).
        *   **Jeśli klient zgodzi się na lekcję próbną po perswazji:** Twoja odpowiedź musi zawierać **TYLKO I WYŁĄCZNIE** znacznik: `{intent_marker}`.
        *   **Jeśli klient nadal odmawia:** Podziękuj za rozmowę i zakończ uprzejmie. (Bez znacznika).
    *   **Jeśli użytkownik zada inne pytanie:** Odpowiedz na nie zgodnie z ogólnymi zasadami i **ponownie spróbuj zachęcić** do umówienia lekcji (wróć do kroku 4 lub 5).

6.  **Obsługa Powrotu (jeśli aktywowano Cię w trybie tymczasowym):**
    *   Odpowiedz na pytanie ogólne użytkownika.
    *   Jeśli odpowiedź użytkownika na Twoją odpowiedź wydaje się satysfakcjonująca (np. "ok", "dziękuję") i **nie zawiera kolejnego pytania ogólnego**, dodaj na **samym końcu** swojej odpowiedzi (po ewentualnym podziękowaniu) **DOKŁADNIE** znacznik: `{return_marker}`.
    *   Jeśli użytkownik zada kolejne pytanie ogólne, odpowiedz na nie normalnie, bez znacznika powrotu.

**Zasady Dodatkowe:**
*   Prowadź rozmowę płynnie.
*   Bądź cierpliwy i empatyczny.
*   Nie przechodź do kolejnego kroku, dopóki nie uzyskasz potrzebnych informacji z poprzedniego.
*   Znacznik `{intent_marker}` jest sygnałem dla systemu, że użytkownik jest gotowy na ustalanie terminu.
*   Znacznik `{return_marker}` służy tylko do powrotu z trybu odpowiedzi na pytanie ogólne zadane podczas innego procesu.
""".format(
    intent_marker=INTENT_SCHEDULE_MARKER,
    return_marker=RETURN_TO_PREVIOUS
)

# --- Funkcja AI: Planowanie terminu ---
def get_gemini_scheduling_response(user_psid, history_for_scheduling_ai, current_user_message_text, available_ranges):
    """Prowadzi rozmowę planującą z AI."""
    if not gemini_model:
        logging.error(f"!!! [{user_psid}] Model Gemini niedostępny (Scheduling)!")
        return None
    ranges_text = format_ranges_for_ai(available_ranges)
    try:
        system_instruction = SYSTEM_INSTRUCTION_SCHEDULING.format(available_ranges_text=ranges_text)
    except KeyError as e:
        logging.error(f"!!! BŁĄD formatowania instrukcji (Scheduling): Brak klucza {e}")
        return "Błąd konfiguracji asystenta."
    except Exception as format_e:
        logging.error(f"!!! BŁĄD formatowania instrukcji (Scheduling): {format_e}")
        return "Błąd wewnętrzny asystenta."

    initial_prompt = [
        Content(role="user", parts=[Part.from_text(system_instruction)]),
        Content(role="model", parts=[Part.from_text(f"Rozumiem. Zapytam o preferencje, zaproponuję termin z listy, dodam znacznik {SLOT_ISO_MARKER_PREFIX}...{SLOT_ISO_MARKER_SUFFIX} po zgodzie, lub {SWITCH_TO_GENERAL} przy pytaniu ogólnym.")])
    ]
    full_prompt = initial_prompt + history_for_scheduling_ai
    if current_user_message_text:
        full_prompt.append(Content(role="user", parts=[Part.from_text(current_user_message_text)]))

    max_prompt_messages = (MAX_HISTORY_TURNS * 2) + 2
    while len(full_prompt) > max_prompt_messages:
        if len(full_prompt) > 2:
            full_prompt.pop(2)
        if len(full_prompt) > 2:
            full_prompt.pop(2)

    response_text = _call_gemini(user_psid, full_prompt, GENERATION_CONFIG_SCHEDULING, "Scheduling Conversation")

    if response_text:
        response_text = response_text.replace(INTENT_SCHEDULE_MARKER, "").strip()
        response_text = response_text.replace(INFO_GATHERED_MARKER, "").strip()
        response_text = response_text.replace(RETURN_TO_PREVIOUS, "").strip()
        return response_text
    else:
        logging.error(f"!!! [{user_psid}] Nie uzyskano odpowiedzi Gemini (Scheduling).")
        return None

# --- Funkcja AI: Zbieranie informacji ---
def get_gemini_gathering_response(user_psid, history_for_gathering_ai, current_user_message_text, context_info):
    """Prowadzi rozmowę zbierającą informacje WYŁĄCZNIE o uczniu."""
    if not gemini_model:
        logging.error(f"!!! [{user_psid}] Model Gemini niedostępny (Gathering)!")
        return None
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
        logging.error(f"!!! BŁĄD formatowania instrukcji (Gathering): Brak klucza {e}")
        return "Błąd konfiguracji asystenta."

    initial_prompt = [
        Content(role="user", parts=[Part.from_text(system_instruction)]),
        Content(role="model", parts=[Part.from_text(f"Rozumiem. Sprawdzę dane ucznia, zapytam o brakujące, zignoruję rodzica. Po zebraniu zwrócę ZEBRANE_DANE_UCZNIA i {INFO_GATHERED_MARKER}, lub {SWITCH_TO_GENERAL} przy pytaniu ogólnym.")])
    ]
    full_prompt = initial_prompt + history_for_gathering_ai
    if current_user_message_text:
        full_prompt.append(Content(role="user", parts=[Part.from_text(current_user_message_text)]))

    max_prompt_messages = (MAX_HISTORY_TURNS * 2) + 2
    while len(full_prompt) > max_prompt_messages:
        if len(full_prompt) > 2:
            full_prompt.pop(2)
        if len(full_prompt) > 2:
            full_prompt.pop(2)

    response_text = _call_gemini(user_psid, full_prompt, GENERATION_CONFIG_GATHERING, "Info Gathering (Student Only)")

    if response_text:
        response_text = response_text.replace(INTENT_SCHEDULE_MARKER, "").strip()
        response_text = re.sub(rf"{re.escape(SLOT_ISO_MARKER_PREFIX)}.*?{re.escape(SLOT_ISO_MARKER_SUFFIX)}", "", response_text).strip()
        response_text = response_text.replace(RETURN_TO_PREVIOUS, "").strip()
        return response_text
    else:
        logging.error(f"!!! [{user_psid}] Nie uzyskano odpowiedzi Gemini (Gathering Info).")
        return None

# --- Funkcja AI: Ogólna rozmowa ---
def get_gemini_general_response(user_psid, current_user_message_text, history_for_general_ai, is_temporary_general_state=False):
    """Prowadzi ogólną rozmowę z AI."""
    if not gemini_model:
        logging.error(f"!!! [{user_psid}] Model Gemini niedostępny (General)!")
        return None

    system_instruction = SYSTEM_INSTRUCTION_GENERAL
    model_ack = f"Rozumiem. Będę asystentem klienta. Dodam {INTENT_SCHEDULE_MARKER} gdy user chce się umówić."
    if is_temporary_general_state:
        model_ack += f" Będąc w trybie tymczasowym, po odpowiedzi na pytanie ogólne, jeśli user nie pyta dalej, dodam {RETURN_TO_PREVIOUS}."

    initial_prompt = [
        Content(role="user", parts=[Part.from_text(system_instruction)]),
        Content(role="model", parts=[Part.from_text(model_ack)])
    ]
    full_prompt = initial_prompt + history_for_general_ai
    if current_user_message_text:
        full_prompt.append(Content(role="user", parts=[Part.from_text(current_user_message_text)]))

    max_prompt_messages = (MAX_HISTORY_TURNS * 2) + 2
    while len(full_prompt) > max_prompt_messages:
        if len(full_prompt) > 2:
            full_prompt.pop(2)
        if len(full_prompt) > 2:
            full_prompt.pop(2)

    response_text = _call_gemini(user_psid, full_prompt, GENERATION_CONFIG_DEFAULT, "General Conversation")

    if response_text:
        response_text = re.sub(rf"{re.escape(SLOT_ISO_MARKER_PREFIX)}.*?{re.escape(SLOT_ISO_MARKER_SUFFIX)}", "", response_text).strip()
        response_text = response_text.replace(INFO_GATHERED_MARKER, "").strip()
        response_text = response_text.replace(SWITCH_TO_GENERAL, "").strip()
        return response_text
    else:
        logging.error(f"!!! [{user_psid}] Nie uzyskano odpowiedzi Gemini (General).")
        return None

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
        logging.info("Weryfikacja GET OK!")
        return Response(hub_challenge, status=200)
    else:
        logging.warning(f"Weryfikacja GET NIEUDANA. Oczekiwany token: '{VERIFY_TOKEN}', Otrzymany: '{hub_token}'")
        return Response("Verification failed", status=403)

def find_row_and_update_sheet(psid, start_time, student_data, sheet_row_index=None):
    """Znajduje wiersz (jeśli nie podano) i aktualizuje dane Fazy 2."""
    if sheet_row_index is None:
        logging.warning(f"[{psid}] Aktualizacja Fazy 2 bez indeksu wiersza. Próba znalezienia...")
        sheet_row_index = find_row_by_psid(psid)
        if sheet_row_index is None:
            logging.error(f"[{psid}] Nie znaleziono wiersza dla PSID do aktualizacji Fazy 2.")
            return False, "Nie znaleziono powiązanego wpisu."
        else:
            logging.info(f"[{psid}] Znaleziono wiersz {sheet_row_index} dla PSID do aktualizacji Fazy 2.")
    return update_sheet_phase2(student_data, sheet_row_index)

@app.route('/webhook', methods=['POST'])
def webhook_handle():
    """Główny handler dla przychodzących zdarzeń z Messengera."""
    now_str = datetime.datetime.now(_get_calendar_timezone()).strftime('%Y-%m-%d %H:%M:%S %Z')
    logging.info(f"\n{'='*30} {now_str} POST /webhook {'='*30}")
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

                    logging.info(f"--- Przetwarzanie zdarzenia dla PSID: {sender_id} ---")
                    history, context = load_history(sender_id)
                    history_for_gemini = [h for h in history if isinstance(h, Content) and h.role in ('user', 'model')]
                    current_state = context.get('type', STATE_GENERAL)
                    logging.info(f"    Aktualny stan: {current_state}")
                    logging.debug(f"    Kontekst wejściowy: {context}")

                    action = None
                    msg_result = None
                    ai_response_text_raw = None
                    next_state = current_state
                    model_resp_content = None
                    user_content = None
                    context_data_to_save = context.copy()
                    context_data_to_save.pop('return_to_state', None)
                    context_data_to_save.pop('return_to_context', None)

                    trigger_gathering_ai_immediately = False
                    slot_verification_failed = False
                    is_temporary_general_state = 'return_to_state' in context

                    # === Obsługa wiadomości / postbacków ===
                    if message_data := event.get("message"):
                        if message_data.get("is_echo"):
                            logging.debug("    Pominięto echo.")
                            continue
                        user_input_text = message_data.get("text", "").strip()
                        if user_input_text:
                            user_content = Content(role="user", parts=[Part.from_text(user_input_text)])
                            log_msg = f"'{user_input_text[:100]}{'...' if len(user_input_text)>100 else ''}'"
                            logging.info(f"    Wiadomość (stan={current_state}): {log_msg}")
                            if ENABLE_TYPING_DELAY:
                                time.sleep(MIN_TYPING_DELAY_SECONDS * 0.5)
                            if current_state == STATE_SCHEDULING_ACTIVE:
                                action = 'handle_scheduling'
                            elif current_state == STATE_GATHERING_INFO:
                                action = 'handle_gathering'
                            else:
                                action = 'handle_general'
                        elif attachments := message_data.get("attachments"):
                            att_type = attachments[0].get('type', 'nieznany')
                            logging.info(f"      Otrzymano załącznik: {att_type}.")
                            user_content = Content(role="user", parts=[Part.from_text(f"[User sent attachment: {att_type}]")])
                            msg_result = "Dziękuję, ale mogę przetwarzać tylko tekst." if att_type not in ['sticker', 'image'] else "Dzięki!"
                            action = 'send_info'
                            next_state = current_state
                        else:
                            logging.info("      Pusta wiadomość/nieobsługiwany typ.")
                            action = None

                    elif postback := event.get("postback"):
                        payload = postback.get("payload")
                        title = postback.get("title", "")
                        logging.info(f"    Postback: Payload='{payload}', Tytuł='{title}', Stan={current_state}")
                        user_input_text = f"User clicked: '{title}' (Payload: {payload})"
                        user_content = Content(role="user", parts=[Part.from_text(user_input_text)])
                        if payload == "CANCEL_SCHEDULING":
                            msg_result = "Anulowano proces umawiania. W czymś jeszcze mogę pomóc?"
                            action = 'send_info'
                            next_state = STATE_GENERAL
                            context_data_to_save = {}
                        elif current_state == STATE_SCHEDULING_ACTIVE:
                            action = 'handle_scheduling'
                        elif current_state == STATE_GATHERING_INFO:
                            action = 'handle_gathering'
                        else:
                            action = 'handle_general'

                    elif event.get("read"):
                        logging.debug("    Potwierdzenie odczytania.")
                        continue
                    elif event.get("delivery"):
                        logging.debug("    Potwierdzenie dostarczenia.")
                        continue
                    else:
                        logging.warning(f"    Nieobsługiwany typ zdarzenia: {json.dumps(event)}")
                        continue

                    # --- Pętla przetwarzania akcji ---
                    loop_guard = 0
                    while action and loop_guard < 3:
                        loop_guard += 1
                        logging.debug(f"  >> Pętla {loop_guard}/3 | Akcja: {action} | Stan: {current_state} | Kontekst: {context_data_to_save}")
                        current_action = action
                        action = None # Resetuj akcję

                        # --- Stan Generalny ---
                        if current_action == 'handle_general':
                            logging.debug("  >> Wykonanie: handle_general")
                            if user_content and user_content.parts:
                                was_temporary = 'return_to_state' in context
                                ai_response_text_raw = get_gemini_general_response(sender_id, user_content.parts[0].text, history_for_gemini, was_temporary)
                                if ai_response_text_raw:
                                    model_resp_content = Content(role="model", parts=[Part.from_text(ai_response_text_raw)])
                                    if RETURN_TO_PREVIOUS in ai_response_text_raw and was_temporary:
                                        logging.info(f"      AI Ogólne -> Powrót [{RETURN_TO_PREVIOUS}].")
                                        msg_result = ai_response_text_raw.split(RETURN_TO_PREVIOUS, 1)[0].strip()
                                        next_state = context.get('return_to_state', STATE_GENERAL)
                                        context_data_to_save = context.get('return_to_context', {})
                                        context_data_to_save['type'] = next_state
                                        logging.info(f"      Przywrócono stan: {next_state}, Kontekst: {context_data_to_save}")
                                        if next_state == STATE_SCHEDULING_ACTIVE:
                                            action = 'handle_scheduling'
                                        elif next_state == STATE_GATHERING_INFO:
                                            action = 'handle_gathering'
                                            trigger_gathering_ai_immediately = True
                                        else:
                                            logging.warning(f"      Nieoczekiwany stan powrotu: {next_state}. Reset do General.")
                                            next_state = STATE_GENERAL
                                            context_data_to_save = {'type': STATE_GENERAL}
                                            action = None
                                        if action:
                                            continue
                                    elif INTENT_SCHEDULE_MARKER in ai_response_text_raw:
                                        logging.info(f"      AI Ogólne -> Intencja Planowania [{INTENT_SCHEDULE_MARKER}].")
                                        msg_result = ai_response_text_raw.split(INTENT_SCHEDULE_MARKER, 1)[0].strip()
                                        next_state = STATE_SCHEDULING_ACTIVE
                                        action = 'handle_scheduling'
                                        context_data_to_save = {'type': STATE_SCHEDULING_ACTIVE}
                                        logging.debug("      Przekierowanie do handle_scheduling...")
                                        continue
                                    else: # Normalna odpowiedź General
                                        msg_result = ai_response_text_raw
                                        next_state = STATE_GENERAL
                                        if was_temporary:
                                            context_data_to_save['return_to_state'] = context['return_to_state']
                                            context_data_to_save['return_to_context'] = context.get('return_to_context', {})
                                            context_data_to_save['type'] = STATE_GENERAL
                                        else:
                                            context_data_to_save = {'type': STATE_GENERAL}
                                else: # Błąd AI General
                                    msg_result = "Przepraszam, mam problem z przetworzeniem wiadomości."
                                    model_resp_content = Content(role="model", parts=[Part.from_text(msg_result)])
                                    next_state = STATE_GENERAL
                                    context_data_to_save = {'type': STATE_GENERAL}
                            else:
                                logging.warning("handle_general bez user_content.")

                        # --- Stan Planowania ---
                        elif current_action == 'handle_scheduling':
                            logging.debug("  >> Wykonanie: handle_scheduling")
                            try:
                                tz = _get_calendar_timezone()
                                now = datetime.datetime.now(tz)
                                search_start_base = now
                                search_end_date = (search_start_base + datetime.timedelta(days=MAX_SEARCH_DAYS)).date()
                                search_end = tz.localize(datetime.datetime.combine(search_end_date, datetime.time(WORK_END_HOUR, 0)))

                                logging.info(f"      Pobieranie wolnych zakresów (Logika OR, Filtr Arkusza Per Kal.) dla {[c['name'] for c in CALENDARS]}")
                                _simulate_typing(sender_id, MAX_TYPING_DELAY_SECONDS * 0.6)
                                free_ranges = get_free_time_ranges(CALENDARS, search_start_base, search_end)

                                if free_ranges:
                                    logging.info(f"      Znaleziono {len(free_ranges)} łącznych wolnych zakresów. Wywołanie AI Planującego...")
                                    current_input_text = user_content.parts[0].text if user_content and user_content.parts else None
                                    if slot_verification_failed:
                                        fail_info = f"\n[SYSTEM: Poprzednio proponowany termin okazał się zajęty. Zaproponuj inny termin z listy.]"
                                        current_input_text = (current_input_text + fail_info) if current_input_text else fail_info
                                        slot_verification_failed = False

                                    ai_response_text_raw = get_gemini_scheduling_response(sender_id, history_for_gemini, current_input_text, free_ranges)

                                    if ai_response_text_raw:
                                        model_resp_content = Content(role="model", parts=[Part.from_text(ai_response_text_raw)])
                                        if ai_response_text_raw.strip() == SWITCH_TO_GENERAL:
                                            logging.info(f"      AI Planujące -> Pytanie Ogólne [{SWITCH_TO_GENERAL}].")
                                            context_data_to_save['return_to_state'] = STATE_SCHEDULING_ACTIVE
                                            context_data_to_save['return_to_context'] = {}
                                            context_data_to_save['type'] = STATE_GENERAL
                                            next_state = STATE_GENERAL
                                            action = 'handle_general'
                                            msg_result = None
                                            logging.debug(f"      Zapisano stan powrotu. Nowy stan: {next_state}. Kontekst: {context_data_to_save}")
                                            continue
                                        iso_match = re.search(rf"{re.escape(SLOT_ISO_MARKER_PREFIX)}(.*?){re.escape(SLOT_ISO_MARKER_SUFFIX)}", ai_response_text_raw)
                                        if iso_match: # AI ustaliło termin
                                            extracted_iso = iso_match.group(1).strip()
                                            logging.info(f"      AI Planujące zwróciło slot: {extracted_iso}")
                                            text_for_user = re.sub(rf"{re.escape(SLOT_ISO_MARKER_PREFIX)}.*?{re.escape(SLOT_ISO_MARKER_SUFFIX)}", "", ai_response_text_raw).strip()
                                            text_for_user = re.sub(r'\s+', ' ', text_for_user).strip()
                                            try:
                                                proposed_start = datetime.datetime.fromisoformat(extracted_iso)
                                                tz_cal = _get_calendar_timezone()
                                                if proposed_start.tzinfo is None:
                                                    proposed_start = tz_cal.localize(proposed_start)
                                                else:
                                                    proposed_start = proposed_start.astimezone(tz_cal)
                                                proposed_slot_formatted = format_slot_for_user(proposed_start)
                                                logging.info(f"      Weryfikacja dostępności {proposed_slot_formatted} w kalendarzach i arkuszu...")
                                                _simulate_typing(sender_id, MIN_TYPING_DELAY_SECONDS)

                                                chosen_calendar_id = None
                                                chosen_calendar_name = None
                                                sheet_blocks = False
                                                min_duration_delta = datetime.timedelta(minutes=APPOINTMENT_DURATION_MINUTES)

                                                potential_sheet_blockers = get_sheet_booked_slots(
                                                    SPREADSHEET_ID, SHEET_NAME,
                                                    proposed_start, proposed_start + min_duration_delta
                                                )
                                                if potential_sheet_blockers:
                                                    for blocker in potential_sheet_blockers:
                                                        if max(proposed_start, blocker['start']) < min(proposed_start + min_duration_delta, blocker['end']):
                                                            logging.warning(f"      Weryfikacja: Slot {proposed_slot_formatted} ZAJĘTY w ARKUSZU przez '{blocker.get('calendar_name', 'Brak')}' ({blocker['start']:%H:%M}-{blocker['end']:%H:%M}).")
                                                            sheet_blocks = True
                                                            break
                                                if not sheet_blocks:
                                                    for cal_config in CALENDARS:
                                                        cal_id = cal_config['id']
                                                        cal_name = cal_config['name']
                                                        if is_slot_actually_free(proposed_start, cal_id):
                                                            chosen_calendar_id = cal_id
                                                            chosen_calendar_name = cal_name
                                                            logging.info(f"      Slot {proposed_slot_formatted} JEST wolny w '{cal_name}' ({cal_id}).")
                                                            break
                                                        else:
                                                            logging.debug(f"      Slot {proposed_slot_formatted} zajęty w '{cal_name}'.")

                                                if chosen_calendar_id: # Sukces weryfikacji
                                                    logging.info(f"      Wybrano '{chosen_calendar_name}' ({chosen_calendar_id}). Zapis Fazy 1...")
                                                    write_ok, write_msg_or_row = write_to_sheet_phase1(sender_id, proposed_start, chosen_calendar_name)
                                                    if write_ok:
                                                        sheet_row_idx = write_msg_or_row if isinstance(write_msg_or_row, int) else None
                                                        parent_profile = get_user_profile(sender_id)
                                                        parent_fn = parent_profile.get('first_name', '') if parent_profile else ''
                                                        parent_ln = parent_profile.get('last_name', '') if parent_profile else ''
                                                        confirm_msg = text_for_user if text_for_user else f"Potwierdzam termin {proposed_slot_formatted}."
                                                        confirm_msg += " Poproszę teraz o dane ucznia."
                                                        msg_result = confirm_msg
                                                        model_resp_content = Content(role="model", parts=[Part.from_text(confirm_msg)])
                                                        context_data_to_save = {
                                                            'type': STATE_GATHERING_INFO,
                                                            'proposed_slot_iso': proposed_start.isoformat(),
                                                            'proposed_slot_formatted': proposed_slot_formatted,
                                                            'chosen_calendar_id': chosen_calendar_id,
                                                            'chosen_calendar_name': chosen_calendar_name,
                                                            'known_parent_first_name': parent_fn,
                                                            'known_parent_last_name': parent_ln,
                                                            'known_student_first_name': '', 'known_student_last_name': '',
                                                            'known_grade': '', 'known_level': '',
                                                            'sheet_row_index': sheet_row_idx
                                                        }
                                                        next_state = STATE_GATHERING_INFO
                                                        action = 'handle_gathering'
                                                        trigger_gathering_ai_immediately = True
                                                        logging.debug(f"      Ustawiono stan '{next_state}', akcję '{action}', trigger={trigger_gathering_ai_immediately}. Kontekst: {context_data_to_save}")
                                                        continue
                                                    else: # Błąd zapisu Fazy 1
                                                        logging.error(f"Błąd zapisu Fazy 1: {write_msg_or_row}")
                                                        msg_result = f"Błąd techniczny rezerwacji ({write_msg_or_row}). Spróbuj później."
                                                        model_resp_content = Content(role="model", parts=[Part.from_text(msg_result)])
                                                        next_state = STATE_GENERAL
                                                        context_data_to_save = {'type': STATE_GENERAL}
                                                else: # Slot zajęty
                                                    blocker_source = 'w arkuszu' if sheet_blocks else 'we wszystkich kalendarzach'
                                                    logging.warning(f"      Weryfikacja NIEUDANA! Slot {extracted_iso} ({proposed_slot_formatted}) zajęty {blocker_source}.")
                                                    fail_msg = f"Ojej, termin {proposed_slot_formatted} został właśnie zajęty! Spróbujmy znaleźć inny."
                                                    msg_result = fail_msg
                                                    fail_info_for_ai = f"\n[SYSTEM: Termin {proposed_slot_formatted} okazał się zajęty. Zaproponuj inny.]"
                                                    model_resp_content = Content(role="model", parts=[Part.from_text(ai_response_text_raw + fail_info_for_ai)])
                                                    next_state = STATE_SCHEDULING_ACTIVE
                                                    slot_verification_failed = True
                                                    context_data_to_save = {'type': STATE_SCHEDULING_ACTIVE}
                                            except ValueError:
                                                logging.error(f"!!! BŁĄD: AI zwróciło nieprawidłowy ISO: '{extracted_iso}'")
                                                msg_result = "Błąd techniczny przetwarzania terminu. Spróbujmy jeszcze raz."
                                                model_resp_content = Content(role="model", parts=[Part.from_text(msg_result)])
                                                next_state = STATE_SCHEDULING_ACTIVE
                                                context_data_to_save = {'type': STATE_SCHEDULING_ACTIVE}
                                            except Exception as verif_err:
                                                logging.error(f"!!! BŁĄD weryfikacji/zapisu {extracted_iso}: {verif_err}", exc_info=True)
                                                msg_result = "Błąd sprawdzania/rezerwacji terminu."
                                                model_resp_content = Content(role="model", parts=[Part.from_text(msg_result)])
                                                next_state = STATE_SCHEDULING_ACTIVE
                                                context_data_to_save = {'type': STATE_SCHEDULING_ACTIVE}
                                        else: # AI kontynuuje rozmowę
                                            logging.info("      AI Planujące kontynuuje rozmowę (brak ISO/SWITCH).")
                                            msg_result = ai_response_text_raw
                                            next_state = STATE_SCHEDULING_ACTIVE
                                            context_data_to_save['type'] = STATE_SCHEDULING_ACTIVE
                                    else: # Błąd AI Scheduling
                                        logging.error("!!! BŁĄD: AI Planujące nie zwróciło odpowiedzi.")
                                        msg_result = "Problem z systemem planowania. Spróbuj za chwilę."
                                        model_resp_content = Content(role="model", parts=[Part.from_text(msg_result)])
                                        next_state = STATE_GENERAL
                                        context_data_to_save = {'type': STATE_GENERAL}
                                else: # Brak wolnych zakresów
                                    logging.warning("      Brak wolnych zakresów (po filtrach).")
                                    no_slots_msg = f"Niestety, brak wolnych terminów w ciągu {MAX_SEARCH_DAYS} dni (z wyprzedzeniem {MIN_BOOKING_LEAD_HOURS}h). Spróbuj później."
                                    msg_result = no_slots_msg
                                    model_resp_content = Content(role="model", parts=[Part.from_text(no_slots_msg)])
                                    next_state = STATE_GENERAL
                                    context_data_to_save = {'type': STATE_GENERAL}
                            except Exception as schedule_err:
                                logging.error(f"!!! KRYTYCZNY BŁĄD 'handle_scheduling': {schedule_err}", exc_info=True)
                                msg_result = "Nieoczekiwany błąd planowania."
                                model_resp_content = Content(role="model", parts=[Part.from_text(msg_result)])
                                next_state = STATE_GENERAL
                                context_data_to_save = {'type': STATE_GENERAL}

                        # --- Stan Zbierania Informacji ---
                        elif current_action == 'handle_gathering':
                            logging.debug("  >> Wykonanie: handle_gathering")
                            try:
                                known_info_for_ai = context_data_to_save.copy()
                                logging.debug(f"    Kontekst dla AI (Gathering): {known_info_for_ai}")
                                current_input_text = user_content.parts[0].text if user_content and user_content.parts else None
                                if trigger_gathering_ai_immediately:
                                    logging.info("      Pierwsze wywołanie AI zbierającego.")
                                    current_input_text = None
                                    trigger_gathering_ai_immediately = False

                                ai_response_text_raw = get_gemini_gathering_response(sender_id, history_for_gemini, current_input_text, known_info_for_ai)

                                if ai_response_text_raw:
                                    model_resp_content = Content(role="model", parts=[Part.from_text(ai_response_text_raw)])
                                    if ai_response_text_raw.strip() == SWITCH_TO_GENERAL:
                                        logging.info(f"      AI Zbierające -> Pytanie Ogólne [{SWITCH_TO_GENERAL}].")
                                        context_data_to_save['return_to_state'] = STATE_GATHERING_INFO
                                        context_data_to_save['return_to_context'] = context_data_to_save.copy()
                                        context_data_to_save['type'] = STATE_GENERAL
                                        next_state = STATE_GENERAL
                                        action = 'handle_general'
                                        msg_result = None
                                        logging.debug(f"      Zapisano stan powrotu. Nowy stan: {next_state}. Kontekst: {context_data_to_save}")
                                        continue
                                    if INFO_GATHERED_MARKER in ai_response_text_raw:
                                        logging.info(f"      AI Zbierające -> Koniec [{INFO_GATHERED_MARKER}]. Parsowanie i aktualizacja Fazy 2.")
                                        response_parts = ai_response_text_raw.split(INFO_GATHERED_MARKER, 1)
                                        ai_full_resp_before_marker = response_parts[0].strip()
                                        final_msg_for_user = ""
                                        data_line_match = re.search(r"ZEBRANE_DANE_UCZNIA:.*", ai_full_resp_before_marker, re.IGNORECASE | re.DOTALL)
                                        if data_line_match:
                                            final_msg_for_user = ai_full_resp_before_marker[data_line_match.end():].strip()
                                        else:
                                            logging.warning("      Brak linii ZEBRANE_DANE_UCZNIA w odp. AI (Gathering).")
                                            final_msg_for_user = ai_full_resp_before_marker
                                        if not final_msg_for_user:
                                            final_msg_for_user = "Dziękujemy za informacje. Prosimy o potwierdzenie wysyłając \"POTWIERDZAM\" na https://www.facebook.com/profile.php?id=61576135251276"
                                            logging.warning("      Użyto domyślnej wiadomości końcowej (Gathering).")

                                        s_fn = "Brak"; s_ln = "Brak"; g_info = "Brak"; l_info = "Brak"
                                        data_regex = r"ZEBRANE_DANE_UCZNIA:\s*\[Imię:\s*(.*?),?\s*Nazwisko:\s*(.*?),?\s*KlasaInfo:\s*(.*?),?\s*Poziom:\s*(.*?)\]"
                                        match = re.search(data_regex, ai_full_resp_before_marker, re.IGNORECASE | re.DOTALL)
                                        if match:
                                            logging.debug("      Znaleziono dopasowanie regex ZEBRANE_DANE_UCZNIA.")
                                            s_fn = match.group(1).strip() if match.group(1) else s_fn
                                            s_ln = match.group(2).strip() if match.group(2) else s_ln
                                            g_info = match.group(3).strip() if match.group(3) else g_info
                                            l_info = match.group(4).strip() if match.group(4) else l_info
                                            l_info = "Brak" if l_info.lower() == 'brak' else l_info
                                            logging.info(f"      Sparsowano z AI: Imię='{s_fn}', Nazwisko='{s_ln}', Klasa='{g_info}', Poziom='{l_info}'")
                                        else:
                                            logging.error("!!! BŁĄD: Nie sparsowano ZEBRANE_DANE_UCZNIA! Używam fallback z kontekstu.")
                                            s_fn = context_data_to_save.get('known_student_first_name', 'Fallback')
                                            s_ln = context_data_to_save.get('known_student_last_name', 'Fallback')
                                            g_info = context_data_to_save.get('known_grade', 'Fallback')
                                            l_info = context_data_to_save.get('known_level', 'Fallback')

                                        try: # Aktualizacja Fazy 2
                                            p_fn = context_data_to_save.get('known_parent_first_name', 'API?')
                                            p_ln = context_data_to_save.get('known_parent_last_name', 'API?')
                                            sheet_row_idx = context_data_to_save.get('sheet_row_index')
                                            if sheet_row_idx is not None:
                                                full_data_update = {'parent_first_name': p_fn, 'parent_last_name': p_ln, 'student_first_name': s_fn, 'student_last_name': s_ln, 'grade_info': g_info, 'level_info': l_info}
                                                update_ok, update_msg = update_sheet_phase2(full_data_update, sheet_row_idx)
                                                if update_ok:
                                                    logging.info("      Aktualizacja Fazy 2 OK.")
                                                    msg_result = final_msg_for_user
                                                    next_state = STATE_GENERAL
                                                    context_data_to_save = {'type': STATE_GENERAL}
                                                else:
                                                    logging.error(f"!!! BŁĄD aktualizacji Fazy 2: {update_msg}")
                                                    error_msg = f"Problem z zapisem danych ({update_msg}). Spróbuj ponownie."
                                                    msg_result = error_msg
                                                    model_resp_content = Content(role="model", parts=[Part.from_text(ai_full_resp_before_marker)])
                                                    next_state = STATE_GATHERING_INFO
                                                    context_data_to_save['type'] = STATE_GATHERING_INFO
                                            else:
                                                logging.error("Brak indeksu wiersza do aktualizacji Fazy 2.")
                                                msg_result = "Błąd wewnętrzny (brak indeksu wiersza)."
                                                model_resp_content = Content(role="model", parts=[Part.from_text(msg_result)])
                                                next_state = STATE_GENERAL
                                                context_data_to_save = {'type': STATE_GENERAL}
                                        except Exception as sheet_err:
                                            logging.error(f"!!! KRYTYCZNY BŁĄD Fazy 2: {sheet_err}", exc_info=True)
                                            msg_result = "Krytyczny błąd zapisu danych."
                                            model_resp_content = Content(role="model", parts=[Part.from_text(msg_result)])
                                            next_state = STATE_GENERAL
                                            context_data_to_save = {'type': STATE_GENERAL}
                                    else: # AI kontynuuje zbieranie
                                        logging.info("      AI Zbierające kontynuuje rozmowę.")
                                        msg_result = ai_response_text_raw
                                        next_state = STATE_GATHERING_INFO
                                        context_data_to_save['type'] = STATE_GATHERING_INFO
                                else: # Błąd AI Gathering
                                    logging.error("!!! BŁĄD: AI Zbierające nie zwróciło odpowiedzi.")
                                    msg_result = "Błąd systemu zbierania informacji. Spróbuj ponownie."
                                    model_resp_content = Content(role="model", parts=[Part.from_text(msg_result)])
                                    next_state = STATE_GATHERING_INFO
                                    context_data_to_save['type'] = STATE_GATHERING_INFO
                            except Exception as gather_err:
                                logging.error(f"!!! KRYTYCZNY BŁĄD 'handle_gathering': {gather_err}", exc_info=True)
                                msg_result = "Nieoczekiwany błąd zbierania informacji."
                                model_resp_content = Content(role="model", parts=[Part.from_text(msg_result)])
                                next_state = STATE_GENERAL
                                context_data_to_save = {'type': STATE_GENERAL}

                        # --- Akcja Wysyłania Informacji ---
                        elif current_action == 'send_info':
                            logging.debug("  >> Wykonanie: send_info")
                            if msg_result:
                                model_resp_content = Content(role="model", parts=[Part.from_text(msg_result)])
                            else:
                                logging.warning("Akcja 'send_info' bez wiadomości.")
                        else:
                            logging.warning(f"   Nieznana akcja '{current_action}'.")
                            break # Przerwij pętlę

                    # --- Koniec pętli ---

                    # --- Zapis Stanu i Historii ---
                    final_context_to_save_dict = context_data_to_save
                    final_context_to_save_dict['type'] = next_state

                    if next_state == STATE_GENERAL and 'return_to_state' in context:
                        final_context_to_save_dict['return_to_state'] = context['return_to_state']
                        final_context_to_save_dict['return_to_context'] = context.get('return_to_context', {})
                    else:
                        final_context_to_save_dict.pop('return_to_state', None)
                        final_context_to_save_dict.pop('return_to_context', None)

                    if msg_result:
                        send_message(sender_id, msg_result)
                    elif current_action:
                        logging.debug(f"    Akcja '{current_action}' zakończona bez wiad. dla usera.")

                    context_for_comp = context.copy()
                    context_for_comp.pop('return_to_state', None)
                    context_for_comp.pop('return_to_context', None)
                    context_for_comp.pop('role', None)
                    final_ctx_for_comp = final_context_to_save_dict.copy()
                    final_ctx_for_comp.pop('role', None)

                    should_save = (
                        bool(user_content) or
                        bool(model_resp_content) or
                        (context_for_comp != final_ctx_for_comp) or
                        slot_verification_failed
                    )

                    if should_save:
                        history_to_save = list(history_for_gemini)
                        if user_content:
                            history_to_save.append(user_content)
                        if model_resp_content:
                            history_to_save.append(model_resp_content)
                        max_hist = MAX_HISTORY_TURNS * 2
                        history_to_save = history_to_save[-max_hist:]
                        logging.info(f"Zapisywanie historii ({len(history_to_save)} wiad.). Stan: {final_context_to_save_dict.get('type')}")
                        logging.debug(f"   Kontekst: {final_context_to_save_dict}")
                        save_history(sender_id, history_to_save, context_to_save=final_context_to_save_dict)
                    else:
                        logging.debug("    Brak zmian - pomijanie zapisu.")

            logging.info("--- Zakończono przetwarzanie batcha ---")
            return Response("EVENT_RECEIVED", status=200)
        else:
            logging.warning(f"POST, ale obiekt != 'page' (typ: {data.get('object') if data else 'Brak'}).")
            return Response("OK", status=200)
    except json.JSONDecodeError as e:
        logging.error(f"!!! BŁĄD dekodowania JSON: {e}", exc_info=True)
        logging.error(f"    Dane: {raw_data[:500]}...")
        return Response("Invalid JSON", status=400)
    except Exception as e:
        logging.critical(f"!!! KRYTYCZNY BŁĄD POST /webhook: {e}", exc_info=True)
        return Response("Internal Server Error", status=200)

# =====================================================================
# === URUCHOMIENIE SERWERA ============================================
# =====================================================================
if __name__ == '__main__':
    ensure_dir(HISTORY_DIR)
    log_level_name = os.environ.get("LOG_LEVEL", "DEBUG").upper()
    log_level = getattr(logging, log_level_name, logging.INFO)

    if not logging.getLogger().hasHandlers():
        log_format = '%(asctime)s - %(levelname)s - [%(filename)s:%(lineno)d - %(funcName)s] - %(message)s'
        log_datefmt = '%Y-%m-%d %H:%M:%S'
        logging.basicConfig(level=log_level, format=log_format, datefmt=log_datefmt)

    logging.getLogger('googleapiclient.discovery_cache').setLevel(logging.ERROR)
    logging.getLogger('urllib3.connectionpool').setLevel(logging.WARNING)
    logging.getLogger('werkzeug').setLevel(logging.WARNING)

    print("\n" + "="*60)
    print("--- START BOTA (Wiele Kal., Logika OR, Filtr Ark. Per Kal., Tylko Numer Klasy w H) ---")
    print(f"  * Poziom logowania: {logging.getLevelName(log_level)}")
    print("-" * 60)
    print("  Konfiguracja Facebook:")
    print(f"    FB_VERIFY_TOKEN: {'OK' if VERIFY_TOKEN != 'KOLAGEN' else 'DOMYŚLNY!'}")
    default_token_part = "EACNAHFzEhkUBO5sicIUMoIwuZCZC1ZAduL8gb5sZAjWX2oErT4esklQALmstq2bkZAnWq3CVNF0IO3gZB44ip3XCXG40revvmpFKOLlC9jBStCNAwbIXZBWfawg0z0YH6GLGZCE1gFfgEF5A6DEIKbu5FYZB6XKXHECTeW6PNZAUQrPiKxrPCjbz7QFiBtGROvZCPR4rAZDZD"
    if not PAGE_ACCESS_TOKEN or len(PAGE_ACCESS_TOKEN) < 50:
        print("!!! KRYTYCZNE: FB_PAGE_ACCESS_TOKEN PUSTY/ZBYT KRÓTKI !!!")
    elif PAGE_ACCESS_TOKEN == default_token_part:
        print("!!! OSTRZ.: FB_PAGE_ACCESS_TOKEN wygląda na domyślny z przykładu! Zmień go.")
    else:
        print("    FB_PAGE_ACCESS_TOKEN: Ustawiony (OK)")
    print("-" * 60)
    print("  Konfiguracja Vertex AI:")
    print(f"    Projekt: {PROJECT_ID}, Lokalizacja: {LOCATION}, Model: {MODEL_ID}")
    if not gemini_model:
        print("!!! OSTRZEŻENIE: Model Gemini NIE załadowany! AI niedostępne. !!!")
    else:
        print(f"    Model Gemini ({MODEL_ID}): Załadowany (OK)")
    print("-" * 60)
    print("  Konfiguracja Google Calendar:")
    print("    Sprawdzane kalendarze:")
    for cal in CALENDARS:
        print(f"      - ID: {cal['id']}, Nazwa: {cal['name']}")
    print(f"    Strefa: {CALENDAR_TIMEZONE} (TZ: {_get_calendar_timezone()})")
    print(f"    Filtry: Godz. {WORK_START_HOUR}-{WORK_END_HOUR}, Wyprz. {MIN_BOOKING_LEAD_HOURS}h, Zakres {MAX_SEARCH_DAYS}dni")
    print(f"    Plik klucza: {CALENDAR_SERVICE_ACCOUNT_FILE} ({'OK' if os.path.exists(CALENDAR_SERVICE_ACCOUNT_FILE) else 'BRAK!!!'})")
    cal_service = get_calendar_service()
    print(f"    Usługa Calendar API: {'OK' if cal_service else 'BŁĄD INICJALIZACJI!'}")
    print("-" * 60)
    print("  Konfiguracja Google Sheets:")
    print(f"    ID Arkusza: {SPREADSHEET_ID}, Nazwa: {SHEET_NAME}")
    print(f"    Strefa: {SHEET_TIMEZONE} (TZ: {_get_sheet_timezone()})")
    print(f"    Kolumny: Data={SHEET_DATE_COLUMN_INDEX}, Czas={SHEET_TIME_COLUMN_INDEX}, NumerKlasy(H)={SHEET_GRADE_COLUMN_INDEX}, Kalendarz={SHEET_CALENDAR_NAME_COLUMN_INDEX}")
    print(f"    Plik klucza: {SHEETS_SERVICE_ACCOUNT_FILE} ({'OK' if os.path.exists(SHEETS_SERVICE_ACCOUNT_FILE) else 'BRAK!!!'})")
    sheets_service = get_sheets_service()
    print(f"    Usługa Sheets API: {'OK' if sheets_service else 'BŁĄD INICJALIZACJI!'}")
    print("--- KONIEC KONFIGURACJI ---")
    print("="*60 + "\n")

    port = int(os.environ.get("PORT", 8080))
    run_flask_in_debug = (log_level == logging.DEBUG)

    print(f"Uruchamianie serwera Flask na porcie {port}...")
    if not run_flask_in_debug:
        try:
            from waitress import serve
            print(">>> Serwer produkcyjny Waitress START <<<")
            serve(app, host='0.0.0.0', port=port, threads=16)
        except ImportError:
            print("!!! Ostrz.: 'waitress' nie znaleziono. Uruchamiam serwer dev Flask.")
            print(">>> Serwer deweloperski Flask START <<<")
            app.run(host='0.0.0.0', port=port, debug=False)
    else:
        print(">>> Serwer deweloperski Flask (DEBUG) START <<<")
        app.run(host='0.0.0.0', port=port, debug=True, use_reloader=False)
