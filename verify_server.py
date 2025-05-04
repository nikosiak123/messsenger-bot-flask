# -*- coding: utf-8 -*-

# verify_server.py (Wersja: Multi-Strona + Statystyki w Arkusz2 + Pełna Funkcjonalność + Formatowanie)

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
VERIFY_TOKEN = os.environ.get("FB_VERIFY_TOKEN", "KOLAGEN") # Token do weryfikacji webhooka

# --- Konfiguracja Stron Facebook ---
# Przechowuj tokeny w zmiennych środowiskowych w systemie produkcyjnym!
PAGE_CONFIG = [
    {
        'id': '661857023673365', # ID strony z Polskiego (ze screena)
        'name': 'Zakręcone Korepetycje - j. Polski',
        'subject': 'Polski',
        'token': os.environ.get("FB_TOKEN_POLSKI", "EACNAHFzEhkUBO5y1aIKOaYcZCKRz2fS6PpPUwPrdqaYgrJGL8KmAVJtXtwbZAzfzSZAREL67A0Go2xcnYgXy4rwZBwmlrraoQZASwZBZAJFEzzZCwL2vsX8lgodNtr2TiAAN4obiESu4TqLU3OwRbCnHgdDX8dRpaUl1lmO9ZAB8TTfkJ0OVQ9QaQJG7njwhugnHMrgZDZD"),
        'link': 'https://tiny.pl/0xnsgbt2'
    },
    {
        'id': '653018101222547', # ID strony z Angielskiego (ze screena)
        'name': 'English Zone - Zakręcone Korepetycje',
        'subject': 'Angielski',
        'token': os.environ.get("FB_TOKEN_ANGIELSKI", "EACNAHFzEhkUBOZC2RxQhFkPJOh4H9vMKZCt0KOCFPBscZCGurYZANYFEOkPyaKcsr88PeP36idt6UiXN2fzBKFdqWSxnaqF1WeZAJU3g6wYOVPTHLNjNl6HcW9GHRgTxEdjQdAMDRnnIgkCtGJCe4pSVEIk7yYRXrcfEam5XY6mXabBvqrlDzZBCLHonZCFRyIuuAZDZD"),
        'link': 'https://tiny.pl/prrr7qf1'
    },
    {
        'id': '638454406015018', # ID strony z Matematyki (ze screena)
        'name': 'Zakręcone Korepetycje - MATEMATYKA',
        'subject': 'Matematyka',
        'token': os.environ.get("FB_TOKEN_MATEMATYKA", "EACNAHFzEhkUBO3hU8CvarZBMqnOeXFZC8v0haCt1fcWIwhiXGQpx98ZBEEGBmoZBwQZADqSSmeb9Py45ie7gXrh5yPCtvVi4aTZBwbggPgjaZCPzRo8dNndqfavc2ZCBZCCtkvVOkPOTs6c9lxmCIdvN1TCzeBszpr3i7n8uUoFme81bZBfm7y5LmwCDYZBy8xMllIy8AZDZD"),
        'link': 'https://tiny.pl/f7xz5n0g'
    }
]
PAGE_ID_TO_CONFIG = {page['id']: page for page in PAGE_CONFIG}
PAGE_ID_TO_SUBJECT = {page['id']: page['subject'] for page in PAGE_CONFIG}
PAGE_ID_TO_TOKEN = {page['id']: page['token'] for page in PAGE_CONFIG}
SUBJECT_TO_LINK = {page['subject'].lower(): page['link'] for page in PAGE_CONFIG}
OTHER_SUBJECT_LINKS_TEXT = "\n".join([f"  - {page['subject']}: {page['link']}" for page in PAGE_CONFIG])

# --- Konfiguracja Vertex AI ---
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

# --- Konfiguracja Kalendarza ---
CALENDAR_SERVICE_ACCOUNT_FILE = 'kalendarzklucz.json'
CALENDAR_SCOPES = ['https://www.googleapis.com/auth/calendar.readonly']
CALENDAR_TIMEZONE = 'Europe/Warsaw'
APPOINTMENT_DURATION_MINUTES = 60
WORK_START_HOUR = 7
WORK_END_HOUR = 22
AVAILABLE_SUBJECTS = list(set(page['subject'] for page in PAGE_CONFIG))
CALENDARS = [
    {'id': 'f19e189826b9d6e36950da347ac84d5501ecbd6bed0d76c8641be61a67749c67@group.calendar.google.com', 'name': 'Kalendarz Polski', 'subject': 'Polski'},
    {'id': '3762cdf9ca674ed1e5dd87ff406dc92f365121aab827cea4d9a02085d31d15fb@group.calendar.google.com', 'name': 'Kalendarz Matematyka', 'subject': 'Matematyka'},
]
SUBJECT_TO_CALENDARS = defaultdict(list)
for cal_config in CALENDARS:
    if 'subject' in cal_config and cal_config['subject'] in AVAILABLE_SUBJECTS:
        SUBJECT_TO_CALENDARS[cal_config['subject'].lower()].append(cal_config)
    else:
        logging.warning(f"Kalendarz '{cal_config['name']}' bez poprawnego przedmiotu.")
ALL_CALENDAR_ID_TO_NAME = {cal['id']: cal['name'] for cal in CALENDARS}
MAX_SEARCH_DAYS = 14
MIN_BOOKING_LEAD_HOURS = 24

# --- Konfiguracja Google Sheets ---
SHEETS_SERVICE_ACCOUNT_FILE = 'arkuszklucz.json'
SHEET_SCOPES = ['https://www.googleapis.com/auth/spreadsheets']
SPREADSHEET_ID = os.environ.get("GOOGLE_SHEET_ID", "1vpsIAEkqtY3ZJ5Mr67Dda45aZ55V1O-Ux9ODjwk13qw")
SHEET_NAME = os.environ.get("GOOGLE_SHEET_NAME", 'Arkusz1') # Główny arkusz rezerwacji
SHEET_TIMEZONE = 'Europe/Warsaw'
SHEET_PSID_COLUMN_INDEX = 1
SHEET_PARENT_FN_COLUMN_INDEX = 2
SHEET_PARENT_LN_COLUMN_INDEX = 3
SHEET_STUDENT_FN_COLUMN_INDEX = 4
SHEET_STUDENT_LN_COLUMN_INDEX = 5
SHEET_DATE_COLUMN_INDEX = 6
SHEET_TIME_COLUMN_INDEX = 7
SHEET_GRADE_COLUMN_INDEX = 8
SHEET_SCHOOL_TYPE_COLUMN_INDEX = 9
SHEET_LEVEL_COLUMN_INDEX = 10
SHEET_CALENDAR_NAME_COLUMN_INDEX = 11
SHEET_READ_RANGE_FOR_PSID_SEARCH = f"{SHEET_NAME}!A2:A"
SHEET_READ_RANGE_FOR_BUSY_SLOTS = f"{SHEET_NAME}!F2:K"

# --- Konfiguracja Arkusza Statystyk ---
STATS_SHEET_NAME = 'Arkusz2'
STATS_HEADER_ROW = 1
STATS_START_COLUMN = 'B'
STATS_ROW_MAP = {
    "Nowe kontakty": 3,
    "Umówione terminy": 4
}
STATS_IGNORE_ROW_NAME = "BRAK"
STATS_IGNORE_ROW_NUMBER = 2

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
    """Tworzy katalog, jeśli nie istnieje (rekursywnie)."""
    try:
        os.makedirs(directory, exist_ok=True)
    except OSError as e:
        if e.errno != errno.EEXIST:
            logging.error(f"Błąd tworzenia katalogu {directory}: {e}", exc_info=True)
            raise

def get_user_profile(psid, page_access_token):
    """Pobiera podstawowe dane profilu użytkownika z FB Graph API używając podanego tokenu."""
    if not page_access_token or len(page_access_token) < 50:
        logging.warning(f"[{psid}] Brak/nieprawidłowy page_access_token do pobrania profilu.")
        return None
    user_profile_api_url_template = "https://graph.facebook.com/v19.0/{psid}?fields=first_name,last_name,profile_pic&access_token={token}"
    url = user_profile_api_url_template.format(psid=psid, token=page_access_token)
    logging.debug(f"--- [{psid}] Pobieranie profilu użytkownika z FB API...")
    profile_data = {}
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        data = r.json()
        if 'error' in data:
            error_code = data['error'].get('code')
            error_msg = data['error'].get('message', 'Brak wiadomości')
            logging.error(f"BŁĄD FB API (profil) dla PSID {psid}: Kod={error_code}, Msg='{error_msg}', Full={data['error']}")
            if error_code == 190:
                logging.error(f"!!! Token PAGE_ACCESS_TOKEN jest nieprawidłowy/wygasł !!!")
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

def load_history(user_psid, page_id):
    """Wczytuje historię i kontekst z pliku strony/użytkownika, aktualizuje statystyki."""
    page_history_dir = os.path.join(HISTORY_DIR, page_id)
    filepath = os.path.join(page_history_dir, f"{user_psid}.json")
    history = []
    valid_states = [STATE_GENERAL, STATE_SCHEDULING_ACTIVE, STATE_GATHERING_INFO]
    default_subject = PAGE_ID_TO_SUBJECT.get(page_id, 'Nieznany')
    default_context = {'type': STATE_GENERAL, 'required_subject': default_subject}
    context = default_context.copy() # Zacznij od domyślnego

    is_new_contact = not os.path.exists(filepath)

    if is_new_contact:
        logging.info(f"[{page_id}/{user_psid}] Plik historii nie istnieje - nowy kontakt.")
        ensure_dir(page_history_dir)
        # --- STATYSTYKA: Nowy Kontakt ---
        try:
            today = datetime.datetime.now(_get_sheet_timezone()).date()
            logging.info(f"[{page_id}/{user_psid}] Aktualizacja statystyk 'Nowe kontakty' dla {today.strftime('%d.%m.%Y')}...")
            if not update_statistics_sheet("Nowe kontakty", today):
                 logging.error(f"[{page_id}/{user_psid}] Nie udało się zaktualizować statystyki 'Nowe kontakty'.")
        except Exception as stat_err:
            logging.error(f"[{page_id}/{user_psid}] Błąd aktualizacji statystyk 'Nowe kontakty': {stat_err}", exc_info=True)
        return [], context # Zwróć pustą historię i domyślny kontekst
    # Jeśli plik istnieje, kontynuuj wczytywanie
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            history_data = json.load(f)
            if isinstance(history_data, list):
                last_system_message_index = -1
                system_context_found = False
                for i, msg_data in enumerate(reversed(history_data)):
                    if isinstance(msg_data, dict) and msg_data.get('role') == 'system':
                        state_type = msg_data.get('type')
                        if state_type and state_type in valid_states:
                            context = msg_data.copy()
                            context.pop('role', None)
                            if 'required_subject' not in context:
                                 context['required_subject'] = default_subject
                                 logging.warning(f"Dodano 'required_subject' do wczytanego kontekstu.")
                            logging.debug(f"[{page_id}/{user_psid}] Odczytano kontekst: {context}")
                            system_context_found = True
                        else:
                             logging.warning(f"Nieprawidłowy kontekst w pliku, używam domyślnego.")
                             context = default_context.copy()
                        last_system_message_index = len(history_data) - 1 - i
                        break

                if not system_context_found:
                    logging.debug(f"Brak kontekstu systemowego, używam domyślnego.")
                    context = default_context.copy()

                limit_index = last_system_message_index if system_context_found else len(history_data)
                for i, msg_data in enumerate(history_data[:limit_index]):
                    if (isinstance(msg_data, dict) and 'role' in msg_data and
                            msg_data['role'] in ('user', 'model') and 'parts' in msg_data and
                            isinstance(msg_data['parts'], list) and msg_data['parts']):
                        text_parts = [Part.from_text(part['text']) for part in msg_data['parts'] if isinstance(part, dict) and 'text' in part and isinstance(part['text'], str)]
                        if text_parts:
                            history.append(Content(role=msg_data['role'], parts=text_parts))

                logging.info(f"[{page_id}/{user_psid}] Wczytano historię: {len(history)} wiad. Stan: {context.get('type')}. Przedmiot: '{context.get('required_subject')}'")
                return history, context
            else:
                logging.error(f"BŁĄD [{page_id}/{user_psid}]: Plik historii {filepath} nie jest listą.")
                return [], default_context.copy()
    except FileNotFoundError:
        # Teoretycznie nie powinno się zdarzyć po is_new_contact, ale dla bezpieczeństwa
        logging.error(f"Błąd: Plik {filepath} zniknął między sprawdzeniem a otwarciem?")
        return [], default_context.copy()
    except (json.JSONDecodeError, KeyError, IndexError, TypeError) as e:
        logging.error(f"BŁĄD [{page_id}/{user_psid}] parsowania historii z {filepath}: {e}.")
        try:
            corrupted_filepath = f"{filepath}.error_{int(time.time())}"
            os.rename(filepath, corrupted_filepath)
            logging.warning(f"Zmieniono nazwę uszkodzonego pliku na: {corrupted_filepath}")
        except OSError as rename_err:
            logging.error(f"Nie udało się zmienić nazwy: {rename_err}")
        return [], default_context.copy()
    except Exception as e:
        logging.error(f"BŁĄD [{page_id}/{user_psid}] wczytywania historii z {filepath}: {e}", exc_info=True)
        return [], default_context.copy()

def save_history(user_psid, page_id, history, context_to_save=None):
    """Zapisuje historię i kontekst do pliku strony/użytkownika."""
    page_history_dir = os.path.join(HISTORY_DIR, page_id)
    ensure_dir(page_history_dir)
    filepath = os.path.join(page_history_dir, f"{user_psid}.json")
    temp_filepath = f"{filepath}.tmp"
    history_data = []
    current_state_to_log = STATE_GENERAL

    try:
        history_to_process = [m for m in history if isinstance(m, Content) and m.role in ('user', 'model')]
        max_messages_to_save = MAX_HISTORY_TURNS * 2
        if len(history_to_process) > max_messages_to_save:
            logging.debug(f"[{page_id}/{user_psid}] Ograniczanie historii do zapisu: {len(history_to_process)} -> {max_messages_to_save}")
            history_to_process = history_to_process[-max_messages_to_save:]

        for msg in history_to_process:
            if isinstance(msg, Content) and hasattr(msg, 'role') and hasattr(msg, 'parts') and isinstance(msg.parts, list):
                parts_data = [{'text': part.text} for part in msg.parts if isinstance(part, Part) and hasattr(part, 'text')]
                if parts_data:
                    history_data.append({'role': msg.role, 'parts': parts_data})

        if context_to_save and isinstance(context_to_save, dict):
            context_copy = context_to_save.copy()
            current_state_to_log = context_copy.get('type', STATE_GENERAL)
            if 'required_subject' not in context_copy:
                 page_subject = PAGE_ID_TO_SUBJECT.get(page_id, 'Nieznany')
                 context_copy['required_subject'] = page_subject
                 logging.warning(f"Dodano 'required_subject' ({page_subject}) do kontekstu przed zapisem.")

            context_copy['role'] = 'system'
            history_data.append(context_copy)
            logging.debug(f"[{page_id}/{user_psid}] Dodano kontekst {current_state_to_log} do zapisu (klucze: {list(context_copy.keys())})")
        else:
            logging.debug(f"[{page_id}/{user_psid}] Zapis bez kontekstu systemowego.")

        with open(temp_filepath, 'w', encoding='utf-8') as f:
            json.dump(history_data, f, ensure_ascii=False, indent=2)
        os.replace(temp_filepath, filepath)
        logging.info(f"[{page_id}/{user_psid}] Zapisano historię/kontekst ({len(history_data)} wpisów, stan: {current_state_to_log}) do {filepath}")

    except Exception as e:
        logging.error(f"BŁĄD [{page_id}/{user_psid}] zapisu historii/kontekstu do {filepath}: {e}", exc_info=True)
        if os.path.exists(temp_filepath):
            try:
                os.remove(temp_filepath)
                logging.info(f"Usunięto plik tymczasowy {temp_filepath} po błędzie zapisu.")
            except OSError as remove_e:
                logging.error(f"Nie można usunąć pliku tymczasowego {temp_filepath}: {remove_e}")

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
            slot_aware = tz.localize(slot_start)
        else:
            slot_aware = slot_start.astimezone(tz)
        try:
            day_name = slot_aware.strftime('%A').capitalize()
            formatted_date = slot_aware.strftime('%d.%m.%Y')
            formatted_time = slot_aware.strftime('%H:%M')
            return f"{day_name}, {formatted_date} o {formatted_time}"
        except Exception as format_err:
            logging.warning(f"Błąd formatowania strftime (locale?): {format_err}. Fallback na ISO.")
            return slot_aware.strftime('%Y-%m-%d %H:%M')
    except Exception as e:
        logging.error(f"Błąd formatowania slotu {slot_start}: {e}", exc_info=True)
        return slot_start.isoformat()

def extract_school_type(grade_string):
    """Wyodrębnia numer klasy, opis i typ szkoły."""
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
        if found_type:
            break
        for pattern in patterns:
            match = re.search(pattern, grade_lower)
            if match:
                school_type = type_name
                pattern_to_remove = r'(?i)(\bklas[ay]?\s+)?\b' + re.escape(match.group(0)) + r'\b(\s+\bklas[ay]?\b)?\s*'
                cleaned_desc_candidate = re.sub(pattern_to_remove, ' ', class_desc, count=1).strip()
                cleaned_desc_candidate = re.sub(r'^\bklas[ay]?\b\s*|\s*\bklas[ay]?\b$', '', cleaned_desc_candidate, flags=re.IGNORECASE).strip()
                if cleaned_desc_candidate and cleaned_desc_candidate != class_desc:
                    class_desc = cleaned_desc_candidate
                elif not cleaned_desc_candidate:
                    num_match_inner = re.search(r'\b(\d+)\b', grade_lower)
                    class_desc = num_match_inner.group(1) if num_match_inner else ""
                found_type = True
                break

    if school_type == "Nieokreślona":
        num_match_outer = re.search(r'\b\d+\b', grade_lower)
        if num_match_outer:
            school_type = "Inna (z numerem klasy)"
            if class_desc == grade_string.strip():
                class_desc = num_match_outer.group(0)

    num_match_final = re.search(r'\b(\d+)\b', grade_string)
    if num_match_final:
        numerical_grade = num_match_final.group(1)

    class_desc = re.sub(r'\bklas[ay]?\b', '', class_desc, flags=re.IGNORECASE).strip()
    class_desc = class_desc if class_desc else (numerical_grade if numerical_grade else grade_string.strip())

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
        logging.error(f"KRYTYCZNY BŁĄD: Brak pliku klucza Google Calendar '{CALENDAR_SERVICE_ACCOUNT_FILE}'")
        return None
    try:
        creds = service_account.Credentials.from_service_account_file(
            CALENDAR_SERVICE_ACCOUNT_FILE, scopes=CALENDAR_SCOPES
        )
        _calendar_service = build('calendar', 'v3', credentials=creds, cache_discovery=False)
        logging.info(f"Utworzono połączenie z Google Calendar API (odczyt).")
        return _calendar_service
    except Exception as e:
        logging.error(f"Błąd tworzenia usługi Google Calendar: {e}", exc_info=True)
        return None

def parse_event_time(event_time_data, default_tz):
    """Parsuje dane czasu wydarzenia z API Kalendarza."""
    dt_str = None
    is_date_only = False
    if not isinstance(event_time_data, dict):
        logging.warning(f"parse_event_time: nieprawidłowy typ danych: {type(event_time_data)}")
        return None
    if 'dateTime' in event_time_data:
        dt_str = event_time_data['dateTime']
    elif 'date' in event_time_data:
        dt_str = event_time_data['date']
        is_date_only = True
    else:
        return None # Brak daty/czasu
    if not isinstance(dt_str, str):
        logging.warning(f"parse_event_time: oczekiwano stringa czasu, otrzymano {type(dt_str)}")
        return None
    try:
        if is_date_only:
            return None # Ignoruj wydarzenia całodniowe
        else:
            if dt_str.endswith('Z'):
                 dt_str = dt_str[:-1] + '+00:00'
            dt = datetime.datetime.fromisoformat(dt_str)
            if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
                dt_aware = default_tz.localize(dt)
            else:
                dt_aware = dt.astimezone(default_tz)
            return dt_aware
    except ValueError as e:
        logging.warning(f"Nie sparsowano czasu '{dt_str}': {e}")
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
        logging.error("get_calendar_busy_slots: Usługa kalendarza niedostępna.")
        return busy_times_calendar
    if not calendar_ids_to_check:
        logging.warning("get_calendar_busy_slots: Brak ID kalendarzy do sprawdzenia.")
        return busy_times_calendar

    if start_datetime.tzinfo is None:
        start_datetime = tz.localize(start_datetime)
    else:
        start_datetime = start_datetime.astimezone(tz)
    if end_datetime.tzinfo is None:
        end_datetime = tz.localize(end_datetime)
    else:
        end_datetime = end_datetime.astimezone(tz)

    items = [{"id": cal_id} for cal_id in calendar_ids_to_check]
    body = { "timeMin": start_datetime.isoformat(), "timeMax": end_datetime.isoformat(), "timeZone": CALENDAR_TIMEZONE, "items": items }
    try:
        logging.debug(f"Zapytanie freeBusy dla kalendarzy: {calendar_ids_to_check}")
        freebusy_result = service_cal.freebusy().query(body=body).execute()
        calendars_data = freebusy_result.get('calendars', {})
        for cal_id in calendar_ids_to_check:
            calendar_data = calendars_data.get(cal_id, {})
            cal_name = ALL_CALENDAR_ID_TO_NAME.get(cal_id, cal_id)
            if 'errors' in calendar_data:
                for error in calendar_data['errors']:
                    logging.error(f"Błąd API Freebusy dla '{cal_name}': {error.get('reason')}")
                continue
            busy_times_raw = calendar_data.get('busy', [])
            logging.debug(f"Kalendarz '{cal_name}': {len(busy_times_raw)} surowych zajętych.")
            for busy_slot in busy_times_raw:
                busy_start = parse_event_time({'dateTime': busy_slot.get('start')}, tz)
                busy_end = parse_event_time({'dateTime': busy_slot.get('end')}, tz)
                if busy_start and busy_end and busy_start < busy_end:
                    busy_start_clipped = max(busy_start, start_datetime)
                    busy_end_clipped = min(busy_end, end_datetime)
                    if busy_start_clipped < busy_end_clipped:
                        busy_times_calendar.append({'start': busy_start_clipped, 'end': busy_end_clipped, 'calendar_id': cal_id})
    except HttpError as error:
        error_content = "Brak szczegółów"
        try:
            if error.resp and error.content:
                error_content = json.loads(error.content.decode('utf-8'))
        except Exception:
            pass
        logging.error(f'Błąd HTTP {error.resp.status} API Freebusy: {error_content}', exc_info=False)
    except Exception as e:
        logging.error(f"Nieoczekiwany błąd podczas freeBusy: {e}", exc_info=True)
    logging.info(f"Pobrano {len(busy_times_calendar)} zajętych slotów z Google: {calendar_ids_to_check}.")
    return busy_times_calendar

def get_sheet_booked_slots(spreadsheet_id, sheet_name, start_datetime, end_datetime):
    """Pobiera zajęte sloty z arkusza Google (czasy w strefie kalendarza)."""
    service = get_sheets_service()
    sheet_busy_slots = []
    if not service:
        logging.error("get_sheet_booked_slots: Usługa arkuszy niedostępna.")
        return sheet_busy_slots
    tz_sheet = _get_sheet_timezone()
    tz_cal = _get_calendar_timezone()

    if start_datetime.tzinfo is None: start_datetime_aware_cal = tz_cal.localize(start_datetime)
    else: start_datetime_aware_cal = start_datetime.astimezone(tz_cal)
    if end_datetime.tzinfo is None: end_datetime_aware_cal = tz_cal.localize(end_datetime)
    else: end_datetime_aware_cal = end_datetime.astimezone(tz_cal)

    try:
        read_range = SHEET_READ_RANGE_FOR_BUSY_SLOTS
        logging.debug(f"Odczyt arkusza '{sheet_name}' zakres '{read_range}' dla zajętych slotów.")
        result = service.spreadsheets().values().get(spreadsheetId=spreadsheet_id, range=read_range).execute()
        values = result.get('values', [])
        if not values:
            return sheet_busy_slots

        duration_delta = datetime.timedelta(minutes=APPOINTMENT_DURATION_MINUTES)
        date_idx = SHEET_DATE_COLUMN_INDEX - SHEET_DATE_COLUMN_INDEX
        time_idx = SHEET_TIME_COLUMN_INDEX - SHEET_DATE_COLUMN_INDEX
        cal_name_idx = SHEET_CALENDAR_NAME_COLUMN_INDEX - SHEET_DATE_COLUMN_INDEX
        expected_row_length = cal_name_idx + 1

        for i, row in enumerate(values):
            row_num = i + 2
            if len(row) >= expected_row_length:
                date_str = row[date_idx].strip()
                time_str = row[time_idx].strip()
                calendar_name_str = row[cal_name_idx].strip()
                if not date_str or not time_str:
                    continue
                if not calendar_name_str:
                    logging.warning(f"Wiersz {row_num} w arkuszu bez nazwy kalendarza (K). Pomijanie.")
                    continue
                try:
                    naive_date = datetime.datetime.strptime(date_str, '%Y-%m-%d').date()
                    naive_time = datetime.datetime.strptime(time_str, '%H:%M').time()
                    naive_dt = datetime.datetime.combine(naive_date, naive_time)
                    slot_start_sheet_tz = tz_sheet.localize(naive_dt)
                    slot_start_cal_tz = slot_start_sheet_tz.astimezone(tz_cal)
                    if start_datetime_aware_cal <= slot_start_cal_tz < end_datetime_aware_cal:
                        slot_end_cal_tz = slot_start_cal_tz + duration_delta
                        sheet_busy_slots.append({'start': slot_start_cal_tz, 'end': slot_end_cal_tz, 'calendar_name': calendar_name_str})
                        logging.debug(f"  Zajęty slot w arkuszu (wiersz {row_num}): {slot_start_cal_tz:%Y-%m-%d %H:%M %Z} (Kal: '{calendar_name_str}')")
                except ValueError:
                    logging.warning(f"Pominięto wiersz {row_num} (błąd parsowania daty/czasu): Data='{date_str}', Czas='{time_str}'")
                except (pytz.exceptions.AmbiguousTimeError, pytz.exceptions.NonExistentTimeError):
                     logging.warning(f"Pominięto wiersz {row_num} (problem ze strefą czasową): Data='{date_str}', Czas='{time_str}'")
                except Exception as parse_err:
                    logging.warning(f"Pominięto wiersz {row_num} (inny błąd): {parse_err}")
    except HttpError as error:
        logging.error(f"Błąd HTTP API odczytu arkusza: {error.resp.status}", exc_info=False)
    except Exception as e:
        logging.error(f"Nieoczekiwany błąd odczytu arkusza: {e}", exc_info=True)
    logging.info(f"Znaleziono {len(sheet_busy_slots)} potencjalnie zajętych slotów w arkuszu.")
    return sheet_busy_slots

def get_free_time_ranges(calendar_config_list, start_datetime, end_datetime):
    """Pobiera wolne zakresy (Logika OR, Filtr Arkusza Per Kalendarz)."""
    service_cal = get_calendar_service()
    tz = _get_calendar_timezone()
    if not service_cal:
        logging.error("get_free_time_ranges: Usługa kalendarza niedostępna.")
        return []
    if not calendar_config_list:
        logging.warning("get_free_time_ranges: Brak kalendarzy do sprawdzenia.")
        return []

    if start_datetime.tzinfo is None: start_datetime = tz.localize(start_datetime)
    else: start_datetime = start_datetime.astimezone(tz)
    if end_datetime.tzinfo is None: end_datetime = tz.localize(end_datetime)
    else: end_datetime = end_datetime.astimezone(tz)
    now = datetime.datetime.now(tz)
    search_start_unfiltered = max(start_datetime, now)
    if search_start_unfiltered >= end_datetime:
        return []

    calendar_names = [c.get('name', c.get('id', '?')) for c in calendar_config_list]
    logging.info(f"Szukanie wolnych zakresów dla {calendar_names} od {search_start_unfiltered:%Y-%m-%d %H:%M} do {end_datetime:%Y-%m-%d %H:%M}")
    min_duration_delta = datetime.timedelta(minutes=APPOINTMENT_DURATION_MINUTES)

    all_sheet_bookings = get_sheet_booked_slots(SPREADSHEET_ID, SHEET_NAME, search_start_unfiltered, end_datetime)
    all_sheet_bookings.sort(key=lambda x: x['start'])
    logging.debug(f"Pobrano {len(all_sheet_bookings)} rezerwacji z arkusza do filtrowania.")

    all_individually_filtered_free_ranges = []
    calendar_ids_to_check_gcal = [c['id'] for c in calendar_config_list if 'id' in c]
    busy_times_gcal_all = get_calendar_busy_slots(calendar_ids_to_check_gcal, search_start_unfiltered, end_datetime)
    busy_times_gcal_by_id = defaultdict(list)
    for busy_slot in busy_times_gcal_all:
        busy_times_gcal_by_id[busy_slot['calendar_id']].append(busy_slot)

    for cal_config in calendar_config_list:
        cal_id = cal_config.get('id')
        cal_name = cal_config.get('name', cal_id or '?')
        if not cal_id:
            continue
        logging.debug(f"--- Przetwarzanie kalendarza: '{cal_name}' ({cal_id}) ---")
        busy_times_cal = sorted(busy_times_gcal_by_id.get(cal_id, []), key=lambda x: x['start'])
        merged_busy_cal = []
        for busy in busy_times_cal:
            if not merged_busy_cal or busy['start'] > merged_busy_cal[-1]['end']:
                merged_busy_cal.append(busy.copy())
            else:
                merged_busy_cal[-1]['end'] = max(merged_busy_cal[-1]['end'], busy['end'])

        raw_calendar_free_ranges = []
        current_time = search_start_unfiltered
        for busy_slot in merged_busy_cal:
            if current_time < busy_slot['start']:
                raw_calendar_free_ranges.append({'start': current_time, 'end': busy_slot['start']})
            current_time = max(current_time, busy_slot['end'])
        if current_time < end_datetime:
            raw_calendar_free_ranges.append({'start': current_time, 'end': end_datetime})

        raw_calendar_free_ranges_workhours = []
        work_start_time = datetime.time(WORK_START_HOUR, 0)
        work_end_time = datetime.time(WORK_END_HOUR, 0)
        for free_range in raw_calendar_free_ranges:
            range_start = free_range['start']
            range_end = free_range['end']
            current_day_start = range_start
            while current_day_start < range_end:
                day_date = current_day_start.date()
                work_day_start_dt = tz.localize(datetime.datetime.combine(day_date, work_start_time))
                work_day_end_dt = tz.localize(datetime.datetime.combine(day_date, work_end_time))
                effective_start = max(current_day_start, work_day_start_dt)
                effective_end = min(range_end, work_day_end_dt)
                if effective_start < effective_end and (effective_end - effective_start) >= min_duration_delta:
                    raw_calendar_free_ranges_workhours.append({'start': effective_start, 'end': effective_end})
                next_day_start_dt = tz.localize(datetime.datetime.combine(day_date + datetime.timedelta(days=1), datetime.time.min))
                current_day_start = min(range_end, max(effective_end, next_day_start_dt))
                current_day_start = max(current_day_start, range_start)
        logging.debug(f"    Surowe wolne dla '{cal_name}' (po GCal i godz.): {len(raw_calendar_free_ranges_workhours)}")

        cal_name_normalized = cal_name.strip().lower()
        sheet_bookings_for_this_cal = [b for b in all_sheet_bookings if b.get('calendar_name', '').strip().lower() == cal_name_normalized]
        logging.debug(f"    Znaleziono {len(sheet_bookings_for_this_cal)} rezerwacji w arkuszu dla '{cal_name_normalized}'.")
        candidate_ranges = raw_calendar_free_ranges_workhours
        if sheet_bookings_for_this_cal:
            logging.debug(f"    Filtrowanie wg {len(sheet_bookings_for_this_cal)} rezerwacji z arkusza...")
            for sheet_busy in sheet_bookings_for_this_cal:
                next_candidate_ranges = []
                for calendar_free in candidate_ranges:
                    overlap_start = max(calendar_free['start'], sheet_busy['start'])
                    overlap_end = min(calendar_free['end'], sheet_busy['end'])
                    if overlap_start < overlap_end:
                        if calendar_free['start'] < sheet_busy['start'] and (sheet_busy['start'] - calendar_free['start']) >= min_duration_delta:
                            next_candidate_ranges.append({'start': calendar_free['start'], 'end': sheet_busy['start']})
                        if calendar_free['end'] > sheet_busy['end'] and (calendar_free['end'] - sheet_busy['end']) >= min_duration_delta:
                            next_candidate_ranges.append({'start': sheet_busy['end'], 'end': calendar_free['end']})
                    else:
                        next_candidate_ranges.append(calendar_free)
                candidate_ranges = sorted(next_candidate_ranges, key=lambda x: x['start'])
            filtered_calendar_free_ranges = candidate_ranges
            logging.debug(f"    Sloty dla '{cal_name}' PO filtracji arkuszem: {len(filtered_calendar_free_ranges)}")
        else:
            filtered_calendar_free_ranges = raw_calendar_free_ranges_workhours
        all_individually_filtered_free_ranges.extend(filtered_calendar_free_ranges)

    if not all_individually_filtered_free_ranges:
        return []
    sorted_filtered_free = sorted(all_individually_filtered_free_ranges, key=lambda x: x['start'])
    logging.debug(f"Łączenie {len(sorted_filtered_free)} indywidualnych slotów ('OR')...")
    merged_all_free_ranges = []
    if sorted_filtered_free:
        current_merged_slot = sorted_filtered_free[0].copy()
        for next_slot in sorted_filtered_free[1:]:
            if next_slot['start'] <= current_merged_slot['end']:
                current_merged_slot['end'] = max(current_merged_slot['end'], next_slot['end'])
            else:
                if (current_merged_slot['end'] - current_merged_slot['start']) >= min_duration_delta:
                     merged_all_free_ranges.append(current_merged_slot)
                current_merged_slot = next_slot.copy()
        if (current_merged_slot['end'] - current_merged_slot['start']) >= min_duration_delta:
            merged_all_free_ranges.append(current_merged_slot)
    logging.debug(f"Scalone wolne zakresy ('OR') PRZED filtrem wyprzedzenia: {len(merged_all_free_ranges)}")

    final_filtered_slots = []
    min_start_time = now + datetime.timedelta(hours=MIN_BOOKING_LEAD_HOURS)
    logging.debug(f"Minimalny czas startu (filtr {MIN_BOOKING_LEAD_HOURS}h): {min_start_time:%Y-%m-%d %H:%M %Z}")
    for slot in merged_all_free_ranges:
        effective_start = max(slot['start'], min_start_time)
        effective_end = slot['end']
        if effective_start < effective_end and (effective_end - effective_start) >= min_duration_delta:
            final_filtered_slots.append({'start': effective_start, 'end': effective_end})
    logging.info(f"Znaleziono {len(final_filtered_slots)} ostatecznych wolnych zakresów.")
    return final_filtered_slots

def is_slot_actually_free(start_time, calendar_id):
    """Weryfikuje w czasie rzeczywistym, czy DOKŁADNY slot jest wolny w GCal."""
    service = get_calendar_service()
    tz = _get_calendar_timezone()
    if not service:
        logging.error(f"is_slot_free: Usługa kalendarza niedostępna (weryfikacja {calendar_id}).")
        return False
    if not isinstance(start_time, datetime.datetime):
        logging.error(f"is_slot_free: Błąd typu start_time ({type(start_time)}) dla {calendar_id}")
        return False

    if start_time.tzinfo is None:
        start_time = tz.localize(start_time)
    else:
        start_time = start_time.astimezone(tz)
    end_time = start_time + datetime.timedelta(minutes=APPOINTMENT_DURATION_MINUTES)
    query_start_time = start_time + datetime.timedelta(seconds=1)
    query_end_time = end_time - datetime.timedelta(seconds=1)
    if query_start_time >= query_end_time:
        logging.warning(f"is_slot_free: Slot za krótki dla {calendar_id}.")
        return True # Zbyt krótki, zakładamy OK

    body = {"timeMin": query_start_time.isoformat(), "timeMax": query_end_time.isoformat(), "timeZone": CALENDAR_TIMEZONE, "items": [{"id": calendar_id}]}
    try:
        cal_name = ALL_CALENDAR_ID_TO_NAME.get(calendar_id, calendar_id)
        logging.debug(f"Weryfikacja free/busy dla '{cal_name}': {start_time:%H:%M} - {end_time:%H:%M}")
        freebusy_result = service.freebusy().query(body=body).execute()
        calendar_data = freebusy_result.get('calendars', {}).get(calendar_id, {})
        if 'errors' in calendar_data:
            for error in calendar_data['errors']:
                logging.error(f"Błąd API Freebusy (weryfikacja) dla '{cal_name}': {error.get('reason')}")
            return False
        busy_times = calendar_data.get('busy', [])
        if not busy_times:
            logging.info(f"Weryfikacja '{cal_name}': Slot {start_time:%H:%M} POTWIERDZONY wolny.")
            return True
        else:
            logging.warning(f"Weryfikacja '{cal_name}': Slot {start_time:%H:%M} ZAJĘTY (busy: {busy_times}).")
            return False
    except HttpError as error:
        error_content="Brak szczegółów"
        try:
             if error.resp and error.content: error_content = json.loads(error.content.decode('utf-8'))
        except Exception:
            pass
        logging.error(f"Błąd HTTP {error.resp.status} API Freebusy (weryfikacja) '{calendar_id}': {error_content}", exc_info=False)
        return False
    except Exception as e:
        logging.error(f"Nieoczekiwany błąd weryfikacji Freebusy '{calendar_id}': {e}", exc_info=True)
        return False

def format_ranges_for_ai(ranges, subject=None):
    """Formatuje listę zakresów czasowych dla AI."""
    if not ranges:
        subject_info = f" dla przedmiotu {subject}" if subject else ""
        return f"Brak dostępnych terminów{subject_info} w podanym okresie."
    tz = _get_calendar_timezone()
    formatted_lines = []
    subject_text = f" dla przedmiotu **{subject}**" if subject else ""
    formatted_lines.append(f"Dostępne ZAKRESY{subject_text} (wizyta trwa {APPOINTMENT_DURATION_MINUTES} minut):")
    formatted_lines.append("--- Dostępne Zakresy (Data YYYY-MM-DD, Dzień, Od HH:MM, Do HH:MM) ---")
    slots_added = 0
    max_slots_to_show = 15
    sorted_ranges = sorted(ranges, key=lambda r: r['start'])
    min_duration = datetime.timedelta(minutes=APPOINTMENT_DURATION_MINUTES)
    for r in sorted_ranges:
        if (r['end'] - r['start']) >= min_duration:
            start_dt = r['start'].astimezone(tz)
            end_dt = r['end'].astimezone(tz)
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
        subject_info = f" dla przedmiotu {subject}" if subject else ""
        return f"Brak dostępnych terminów{subject_info} (mieszczących wizytę) w podanym okresie."
    formatted_output = "\n".join(formatted_lines)
    logging.debug(f"--- Zakresy sformatowane dla AI ({slots_added} pokazanych, Przedmiot: {subject or 'brak'}) ---")
    return formatted_output

# =====================================================================
# === FUNKCJE GOOGLE SHEETS (ZAPIS + ODCZYT + STATYSTYKI) =============
# =====================================================================

def get_sheets_service():
    """Inicjalizuje (i cachuje) usługę Google Sheets API."""
    global _sheets_service
    if _sheets_service:
        return _sheets_service
    if not os.path.exists(SHEETS_SERVICE_ACCOUNT_FILE):
        logging.error(f"KRYTYCZNY BŁĄD: Brak pliku klucza Google Sheets '{SHEETS_SERVICE_ACCOUNT_FILE}'")
        return None
    try:
        creds = service_account.Credentials.from_service_account_file(SHEETS_SERVICE_ACCOUNT_FILE, scopes=SHEET_SCOPES)
        _sheets_service = build('sheets', 'v4', credentials=creds, cache_discovery=False)
        logging.info(f"Utworzono połączenie z Google Sheets API.")
        return _sheets_service
    except Exception as e:
        logging.error(f"Błąd tworzenia usługi Google Sheets: {e}", exc_info=True)
        return None

def find_row_by_psid(psid):
    """Szuka wiersza w arkuszu na podstawie PSID (od wiersza 2)."""
    service = get_sheets_service()
    if not service:
        logging.error("find_row_by_psid: Usługa arkuszy niedostępna.")
        return None
    try:
        read_range = SHEET_READ_RANGE_FOR_PSID_SEARCH
        logging.debug(f"Szukanie PSID {psid} w '{SHEET_NAME}' zakres '{read_range}'")
        result = service.spreadsheets().values().get(spreadsheetId=SPREADSHEET_ID, range=read_range).execute()
        values = result.get('values', [])
        if not values:
            logging.debug(f"Arkusz '{SHEET_NAME}' pusty lub brak PSID w zakresie {read_range}.")
            return None
        for i, row in enumerate(values):
            if row and len(row) > 0 and row[0].strip() == psid:
                row_number = i + 2
                logging.info(f"Znaleziono PSID {psid} w wierszu {row_number}.")
                return row_number
        logging.info(f"Nie znaleziono PSID {psid} w arkuszu ({read_range}).")
        return None
    except HttpError as error:
        error_content="Brak szczegółów"
        try:
            if error.resp and error.content:
                error_content = json.loads(error.content.decode('utf-8'))
        except Exception:
            pass
        logging.error(f"Błąd HTTP {error.resp.status} API szukania PSID: {error_content}", exc_info=False)
        return None
    except Exception as e:
        logging.error(f"Nieoczekiwany błąd szukania PSID: {e}", exc_info=True)
        return None

def write_to_sheet_phase1(psid, start_time, calendar_name):
    """Zapisuje dane Fazy 1 (PSID, Data, Czas, Nazwa Kalendarza) do arkusza."""
    service = get_sheets_service()
    if not service:
        return False, "Błąd połączenia z Google Sheets (Faza 1)."
    tz_sheet = _get_sheet_timezone()
    tz_cal = _get_calendar_timezone()
    try:
        if start_time.tzinfo is None:
            start_time_aware = tz_cal.localize(start_time).astimezone(tz_sheet)
        else:
            start_time_aware = start_time.astimezone(tz_sheet)
    except (pytz.exceptions.AmbiguousTimeError, pytz.exceptions.NonExistentTimeError):
         logging.error(f"Błąd konwersji czasu Fazy 1 dla {start_time} (zmiana czasu?).")
         return False, "Błąd strefy czasowej przy zapisie."

    date_str = start_time_aware.strftime('%Y-%m-%d')
    time_str = start_time_aware.strftime('%H:%M')
    max_col_index = max(SHEET_PSID_COLUMN_INDEX, SHEET_DATE_COLUMN_INDEX, SHEET_TIME_COLUMN_INDEX, SHEET_CALENDAR_NAME_COLUMN_INDEX)
    data_row = [""] * max_col_index
    data_row[SHEET_PSID_COLUMN_INDEX - 1] = psid
    data_row[SHEET_DATE_COLUMN_INDEX - 1] = date_str
    data_row[SHEET_TIME_COLUMN_INDEX - 1] = time_str
    data_row[SHEET_CALENDAR_NAME_COLUMN_INDEX - 1] = calendar_name
    try:
        range_name = f"{SHEET_NAME}!A1"
        body = {'values': [data_row]}
        logging.info(f"Zapis Fazy 1 (Append) do '{SHEET_NAME}': PSID={psid}, Data={date_str}, Czas={time_str}, Kalendarz='{calendar_name}'")
        result = service.spreadsheets().values().append(spreadsheetId=SPREADSHEET_ID, range=range_name, valueInputOption='USER_ENTERED', insertDataOption='INSERT_ROWS', body=body).execute()
        updated_range = result.get('updates', {}).get('updatedRange', '')
        logging.info(f"Zapisano Faza 1. Zakres: {updated_range}")
        match = re.search(rf"{re.escape(SHEET_NAME)}!A(\d+):", updated_range)
        row_index = int(match.group(1)) if match else None
        if row_index:
            logging.info(f"Wyodrębniono numer wiersza Fazy 1: {row_index}")
            return True, row_index
        else:
            logging.warning(f"Nie udało się wyodrębnić numeru wiersza z: {updated_range}.")
            return True, None
    except HttpError as error:
        error_content="Brak szczegółów"
        try:
            if error.resp and error.content:
                error_content = json.loads(error.content.decode('utf-8'))
        except Exception:
            pass
        error_details = f"{error.resp.status} {error.resp.reason}"
        logging.error(f"Błąd API Fazy 1 (Append): {error_details}. Szczegóły: {error_content}", exc_info=False)
        api_message = error_content.get('error', {}).get('message', error_details) if isinstance(error_content, dict) else error_details
        return False, f"Błąd zapisu Fazy 1 ({api_message})."
    except Exception as e:
        logging.error(f"Błąd Python Fazy 1 (Append): {e}", exc_info=True)
        return False, "Wewnętrzny błąd systemu Fazy 1."

def update_sheet_phase2(student_data, sheet_row_index):
    """Aktualizuje wiersz danymi Fazy 2."""
    service = get_sheets_service()
    if not service:
        return False, "Błąd połączenia z Google Sheets (Faza 2)."
    if sheet_row_index is None or not isinstance(sheet_row_index, int) or sheet_row_index < 2:
        logging.error(f"Nieprawidłowy indeks wiersza ({sheet_row_index}) do aktualizacji Fazy 2.")
        return False, "Brak/nieprawidłowy numer wiersza."
    try:
        parent_fn = student_data.get('parent_first_name', '')
        parent_ln = student_data.get('parent_last_name', '')
        student_fn = student_data.get('student_first_name', '')
        student_ln = student_data.get('student_last_name', '')
        grade_info = student_data.get('grade_info', '')
        level_info = student_data.get('level_info', '')
        numerical_grade, _, school_type = extract_school_type(grade_info)

        logging.info(f"Aktualizacja Fazy 2 (wiersz {sheet_row_index}): NrKlasy(H)='{numerical_grade}', TypSzkoły(I)='{school_type}', Poziom(J)='{level_info}'")
        update_data_group1 = [parent_fn, parent_ln, student_fn, student_ln]
        update_data_group2 = [numerical_grade, school_type, level_info]
        start_col_g1 = chr(ord('A')+SHEET_PARENT_FN_COLUMN_INDEX-1)
        end_col_g1 = chr(ord('A')+SHEET_STUDENT_LN_COLUMN_INDEX-1)
        range_group1 = f"{SHEET_NAME}!{start_col_g1}{sheet_row_index}:{end_col_g1}{sheet_row_index}"
        start_col_g2 = chr(ord('A')+SHEET_GRADE_COLUMN_INDEX-1)
        end_col_g2 = chr(ord('A')+SHEET_LEVEL_COLUMN_INDEX-1)
        range_group2 = f"{SHEET_NAME}!{start_col_g2}{sheet_row_index}:{end_col_g2}{sheet_row_index}"
        body1 = {'values': [update_data_group1]}
        body2 = {'values': [update_data_group2]}

        logging.info(f"Aktualizacja Fazy 2 (Grupa 1) wiersz {sheet_row_index} zakres {range_group1}")
        result1 = service.spreadsheets().values().update(spreadsheetId=SPREADSHEET_ID, range=range_group1, valueInputOption='USER_ENTERED', body=body1).execute()
        logging.info(f"Zaktualizowano Faza 2 (Grupa 1): {result1.get('updatedCells')} komórek.")
        logging.info(f"Aktualizacja Fazy 2 (Grupa 2) wiersz {sheet_row_index} zakres {range_group2}")
        result2 = service.spreadsheets().values().update(spreadsheetId=SPREADSHEET_ID, range=range_group2, valueInputOption='USER_ENTERED', body=body2).execute()
        logging.info(f"Zaktualizowano Faza 2 (Grupa 2): {result2.get('updatedCells')} komórek.")
        return True, None
    except HttpError as error:
        error_content="Brak szczegółów"
        try:
            if error.resp and error.content:
                error_content = json.loads(error.content.decode('utf-8'))
        except Exception:
            pass
        error_details = f"{error.resp.status} {error.resp.reason}"
        logging.error(f"Błąd API Fazy 2 (Update): {error_details}. Szczegóły: {error_content}", exc_info=False)
        api_message = error_content.get('error', {}).get('message', error_details) if isinstance(error_content, dict) else error_details
        return False, f"Błąd aktualizacji Fazy 2 ({api_message})."
    except Exception as e:
        logging.error(f"Błąd Python Fazy 2 (Update): {e}", exc_info=True)
        return False, "Wewnętrzny błąd systemu Fazy 2."

def update_statistics_sheet(stat_type, event_date):
    """Aktualizuje licznik statystyk w Arkusz2."""
    service = get_sheets_service()
    if not service:
        logging.error(f"Statystyki: Błąd połączenia z Sheets.")
        return False

    target_row_index = STATS_ROW_MAP.get(stat_type)
    if not target_row_index:
        logging.error(f"Statystyki: Nieznany typ '{stat_type}'.")
        return False
    if target_row_index == STATS_IGNORE_ROW_NUMBER:
         logging.warning(f"Statystyki: Próba zapisu do ignorowanego wiersza {STATS_IGNORE_ROW_NUMBER}. Pomijanie.")
         return False

    logging.debug(f"Statystyki: Aktualizacja '{stat_type}' (wiersz {target_row_index}) dla {event_date.strftime('%Y-%m-%d')}")

    try:
        header_range = f"{STATS_SHEET_NAME}!{STATS_START_COLUMN}{STATS_HEADER_ROW}:{STATS_HEADER_ROW}"
        logging.debug(f"Statystyki: Odczyt nagłówka {header_range}")
        result = service.spreadsheets().values().get(spreadsheetId=SPREADSHEET_ID, range=header_range).execute()
        header_values = result.get('values', [[]])[0]

        target_col_index = -1
        date_str_to_find = event_date.strftime('%#d.%#m.%Y') # D.M.YYYY (Win)
        date_str_to_find_alt = event_date.strftime('%-d.%-m.%Y') # D.M.YYYY (Unix)
        date_str_to_find_padded = event_date.strftime('%d.%m.%Y') # DD.MM.YYYY

        logging.debug(f"Statystyki: Szukam daty '{date_str_to_find}'/'{date_str_to_find_alt}'/'{date_str_to_find_padded}' w {header_values}")
        for i, cell_value in enumerate(header_values):
            if isinstance(cell_value, str):
                cell_value_stripped = cell_value.strip()
                if cell_value_stripped in [date_str_to_find, date_str_to_find_alt, date_str_to_find_padded]:
                    target_col_index = i
                    break
                try: # Próba parsowania
                     if datetime.datetime.strptime(cell_value_stripped, '%d.%m.%Y').date() == event_date:
                          target_col_index = i
                          break
                except ValueError:
                     pass

        if target_col_index == -1:
            logging.error(f"Statystyki: Nie znaleziono kolumny dla daty {event_date.strftime('%d.%m.%Y')} w '{STATS_SHEET_NAME}'.")
            return False

        start_col_ord = ord(STATS_START_COLUMN.upper())
        target_col_letter = chr(start_col_ord + target_col_index)
        target_cell_a1 = f"{STATS_SHEET_NAME}!{target_col_letter}{target_row_index}"
        logging.debug(f"Statystyki: Komórka docelowa: {target_cell_a1}")

        current_value = 0
        try:
            cell_result = service.spreadsheets().values().get(spreadsheetId=SPREADSHEET_ID, range=target_cell_a1).execute()
            cell_values = cell_result.get('values', [[]])
            if cell_values and cell_values[0]:
                current_value_str = cell_values[0][0]
                try:
                    current_value = int(current_value_str)
                except ValueError:
                    logging.warning(f"Statystyki: Wartość w {target_cell_a1} ('{current_value_str}') nie jest liczbą. Używam 0.")
        except HttpError as get_err:
             logging.warning(f"Statystyki: Błąd odczytu {target_cell_a1}: {get_err}. Używam 0.")

        new_value = current_value + 1
        logging.info(f"Statystyki: Zwiększanie wartości w {target_cell_a1}: {current_value} -> {new_value}.")
        update_body = {'values': [[new_value]]}
        service.spreadsheets().values().update(spreadsheetId=SPREADSHEET_ID, range=target_cell_a1, valueInputOption='USER_ENTERED', body=update_body).execute()
        logging.info(f"Statystyki: Zaktualizowano {target_cell_a1} na {new_value}.")
        return True

    except HttpError as error:
        error_content="?"
        try:
             if error.resp and error.content: error_content = json.loads(error.content.decode('utf-8'))
        except Exception:
            pass
        logging.error(f"Statystyki: Błąd HTTP API ({error.resp.status}): {error_content}", exc_info=False)
        return False
    except Exception as e:
        logging.error(f"Statystyki: Nieoczekiwany błąd dla '{stat_type}': {e}", exc_info=True)
        return False

# =====================================================================
# === FUNKCJE KOMUNIKACJI FB ==========================================
# =====================================================================

def _send_typing_on(recipient_id, page_access_token):
    """Wysyła 'typing_on' używając podanego tokenu."""
    if not page_access_token or len(page_access_token) < 50 or not ENABLE_TYPING_DELAY:
        return
    logging.debug(f"[{recipient_id}] Wysyłanie 'typing_on'")
    params = {"access_token": page_access_token}
    payload = {"recipient": {"id": recipient_id}, "sender_action": "typing_on"}
    try:
        requests.post(FACEBOOK_GRAPH_API_URL, params=params, json=payload, timeout=3)
    except requests.exceptions.RequestException as e:
        logging.warning(f"[{recipient_id}] Błąd wysyłania 'typing_on': {e}")

def _send_single_message(recipient_id, message_text, page_access_token):
    """Wysyła pojedynczy fragment wiadomości używając podanego tokenu."""
    page_id = [pid for pid, tok in PAGE_ID_TO_TOKEN.items() if tok == page_access_token]
    page_id_log = page_id[0] if page_id else 'UNKNOWN'
    logging.info(f"--- Wysyłanie do PSID:{recipient_id} na PAGE:{page_id_log} (dł: {len(message_text)}) ---")

    if not recipient_id or not message_text:
        logging.error("Błąd wysyłania: Brak ID/treści.")
        return False
    if not page_access_token or len(page_access_token) < 50:
        logging.error(f"!!! [{recipient_id}/{page_id_log}] Brak/błędny token. NIE WYSŁANO.")
        return False

    params = {"access_token": page_access_token}
    payload = {"recipient": {"id": recipient_id}, "message": {"text": message_text}, "messaging_type": "RESPONSE"}
    try:
        r = requests.post(FACEBOOK_GRAPH_API_URL, params=params, json=payload, timeout=30)
        r.raise_for_status()
        response_json = r.json()
        if fb_error := response_json.get('error'):
            error_code = fb_error.get('code')
            error_msg = fb_error.get('message', '?')
            logging.error(f"!!! BŁĄD FB API [{recipient_id}/{page_id_log}]: Kod={error_code}, Msg='{error_msg}' !!!")
            if error_code == 190:
                logging.critical(f"!!! Token dla strony {page_id_log} nieprawidłowy/wygasł !!!")
            return False
        logging.debug(f"[{recipient_id}/{page_id_log}] Fragment wysłany (Msg ID: {response_json.get('message_id')}).")
        return True
    except requests.exceptions.Timeout:
        logging.error(f"!!! TIMEOUT wysyłania do [{recipient_id}/{page_id_log}] !!!")
        return False
    except requests.exceptions.HTTPError as http_err:
        logging.error(f"!!! BŁĄD HTTP {http_err.response.status_code} wysyłania do [{recipient_id}/{page_id_log}] !!!")
        if http_err.response is not None:
            try:
                logging.error(f"Odpowiedź FB: {http_err.response.json()}")
            except json.JSONDecodeError:
                logging.error(f"Odpowiedź FB (nie JSON): {http_err.response.text}")
        return False
    except requests.exceptions.RequestException as req_err:
        logging.error(f"!!! BŁĄD RequestException wysyłania do [{recipient_id}/{page_id_log}]: {req_err} !!!")
        return False
    except Exception as e:
        logging.error(f"!!! Nieoczekiwany BŁĄD wysyłania do [{recipient_id}/{page_id_log}]: {e} !!!", exc_info=True)
        return False

def send_message(recipient_id, page_access_token, full_message_text):
    """Wysyła wiadomość, dzieląc ją i używając podanego tokenu."""
    page_id = [pid for pid, tok in PAGE_ID_TO_TOKEN.items() if tok == page_access_token]
    page_id_log = page_id[0] if page_id else 'UNKNOWN'

    if not full_message_text or not isinstance(full_message_text, str) or not full_message_text.strip():
        logging.warning(f"[{recipient_id}/{page_id_log}] Pominięto wysłanie pustej wiadomości.")
        return
    if not page_access_token or len(page_access_token) < 50:
         logging.error(f"!!! [{recipient_id}/{page_id_log}] Nie można wysłać - brak/błędny token.")
         return

    message_len = len(full_message_text)
    logging.info(f"[{recipient_id}/{page_id_log}] Przygotowanie wiadomości (dł: {message_len}).")

    if ENABLE_TYPING_DELAY:
        duration = min(MAX_TYPING_DELAY_SECONDS, max(MIN_TYPING_DELAY_SECONDS, message_len / TYPING_CHARS_PER_SECOND))
        logging.debug(f"[{recipient_id}/{page_id_log}] Symulacja pisania: {duration:.2f}s")
        _simulate_typing(recipient_id, page_access_token, duration)

    chunks = []
    if message_len <= MESSAGE_CHAR_LIMIT:
        chunks.append(full_message_text)
    else:
        logging.info(f"[{recipient_id}/{page_id_log}] Dzielenie wiadomości ({message_len} > {MESSAGE_CHAR_LIMIT})...")
        remaining_text = full_message_text
        while remaining_text:
            if len(remaining_text) <= MESSAGE_CHAR_LIMIT:
                chunks.append(remaining_text.strip())
                break
            split_index = -1
            delimiters = ['\n\n', '\n', '. ', '! ', '? ', ' ']
            search_end_pos = MESSAGE_CHAR_LIMIT
            for delim in delimiters:
                found_index = remaining_text.rfind(delim, 0, search_end_pos)
                if found_index != -1:
                    split_index = found_index + len(delim)
                    break
            if split_index == -1:
                split_index = MESSAGE_CHAR_LIMIT
            chunk = remaining_text[:split_index].strip()
            if chunk:
                chunks.append(chunk)
            remaining_text = remaining_text[split_index:].strip()
        logging.info(f"[{recipient_id}/{page_id_log}] Podzielono na {len(chunks)} fragmentów.")

    num_chunks = len(chunks)
    successful_sends = 0
    for i, chunk_text in enumerate(chunks):
        logging.debug(f"[{recipient_id}/{page_id_log}] Wysyłanie fragmentu {i+1}/{num_chunks}...")
        if not _send_single_message(recipient_id, chunk_text, page_access_token):
            logging.error(f"!!! [{recipient_id}/{page_id_log}] Błąd wysyłania fragmentu {i+1}. Anulowanie reszty.")
            break
        successful_sends += 1
        if num_chunks > 1 and i < num_chunks - 1:
            logging.debug(f"[{recipient_id}/{page_id_log}] Oczekiwanie {MESSAGE_DELAY_SECONDS}s...")
            if ENABLE_TYPING_DELAY:
                _send_typing_on(recipient_id, page_access_token)
            time.sleep(MESSAGE_DELAY_SECONDS)

    logging.info(f"--- [{recipient_id}/{page_id_log}] Zakończono wysyłanie. Wysłano {successful_sends}/{num_chunks} fragmentów. ---")

def _simulate_typing(recipient_id, page_access_token, duration_seconds):
    """Wysyła 'typing_on' i czeka używając podanego tokenu."""
    if ENABLE_TYPING_DELAY and duration_seconds > 0 and page_access_token:
        _send_typing_on(recipient_id, page_access_token)
        wait_time = min(duration_seconds, MAX_TYPING_DELAY_SECONDS * 1.1)
        time.sleep(wait_time)

# =====================================================================
# === FUNKCJE WYWOŁANIA AI ============================================
# =====================================================================

def _call_gemini(user_psid, prompt_history, generation_config, task_name, max_retries=3):
    """Wywołuje API Gemini z obsługą błędów i ponowień."""
    if not gemini_model:
        logging.error(f"!!! [{user_psid}] Model Gemini ({task_name}) niedostępny!")
        return "Przepraszam, błąd systemu AI."
    if not isinstance(prompt_history, list) or not all(isinstance(item, Content) for item in prompt_history):
        logging.error(f"!!! [{user_psid}] Nieprawidłowy format promptu ({task_name}).")
        return "Przepraszam, błąd przetwarzania."

    logging.info(f"[{user_psid}] Wywołanie Gemini: {task_name} (Prompt: {len(prompt_history)} wiad.)")
    # Logowanie ostatniej wiadomości dla kontekstu
    last_user_msg_part = next((msg.parts[0] for msg in reversed(prompt_history) if msg.role == 'user' and msg.parts), None)
    if last_user_msg_part and hasattr(last_user_msg_part, 'text'):
        logging.debug(f"    Ostatnia wiad. usera ({task_name}): '{last_user_msg_part.text[:200]}...'")

    attempt = 0
    while attempt < max_retries:
        attempt += 1
        logging.debug(f"    Próba {attempt}/{max_retries} ({task_name})...")
        try:
            response = gemini_model.generate_content(prompt_history, generation_config=generation_config, safety_settings=SAFETY_SETTINGS, stream=False)

            if not response:
                 logging.warning(f"[{user_psid}] Gemini ({task_name}) zwróciło None.")
                 if attempt < max_retries: time.sleep(1); continue
                 else: return "Brak odpowiedzi od AI."

            if not response.candidates:
                prompt_feedback = response.prompt_feedback if hasattr(response, 'prompt_feedback') else None
                if prompt_feedback and hasattr(prompt_feedback, 'block_reason') and prompt_feedback.block_reason != 0:
                     block_reason_name = prompt_feedback.block_reason.name
                     logging.error(f"!!! [{user_psid}] Gemini ({task_name}) - PROMPT ZABLOKOWANY! Powód: {block_reason_name}.")
                     return "Wiadomość narusza zasady bezpieczeństwa."
                else:
                    logging.warning(f"[{user_psid}] Gemini ({task_name}) brak kandydatów. Feedback: {prompt_feedback}")
                    if attempt < max_retries: time.sleep(1.5 * attempt); continue
                    else: return "Brak kandydatów odpowiedzi."

            candidate = response.candidates[0]
            finish_reason = candidate.finish_reason if hasattr(candidate, 'finish_reason') else None
            finish_reason_val = finish_reason.value if finish_reason else 0

            if finish_reason_val != 1: # Nie jest STOP
                finish_reason_name = finish_reason.name if hasattr(finish_reason, 'name') else str(finish_reason_val or '?')
                safety_ratings = candidate.safety_ratings if hasattr(candidate, 'safety_ratings') else "Brak"
                logging.warning(f"[{user_psid}] Gemini ({task_name}) ZAKOŃCZONE NIEPRAWIDŁOWO! Powód: {finish_reason_name}({finish_reason_val}). Safety: {safety_ratings}")
                if finish_reason_val in [3, 4]: # Safety / Recitation
                    if attempt < max_retries: time.sleep(1.5 * attempt); continue
                    else: return "Nie mogę wygenerować odpowiedzi ze względu na zasady bezpieczeństwa."
                elif finish_reason_val == 2: # Max Tokens
                     partial_text = "".join(part.text for part in candidate.content.parts if hasattr(candidate.content, 'parts') and hasattr(part, 'text')).strip()
                     if partial_text: return partial_text + "..." # Zwróć część
                     else:
                         if attempt < max_retries: time.sleep(1.5 * attempt); continue
                         else: return "Odpowiedź zbyt długa."
                else: # Inny powód
                    if attempt < max_retries: time.sleep(1.5 * attempt); continue
                    else: return f"Problem z generowaniem odpowiedzi (kod: {finish_reason_name})."

            # Sukces (finish_reason == STOP)
            if hasattr(candidate, 'content') and candidate.content and hasattr(candidate.content, 'parts') and candidate.content.parts:
                generated_text = "".join(part.text for part in candidate.content.parts if hasattr(part, 'text')).strip()
                if generated_text:
                    logging.info(f"[{user_psid}] Gemini ({task_name}) zwróciło odpowiedź (dł: {len(generated_text)}).")
                    return generated_text
                else:
                    logging.warning(f"[{user_psid}] Gemini ({task_name}) zwróciło pustą treść (Finish: STOP).")
                    if attempt < max_retries: time.sleep(1); continue
                    else: return "Problem z generowaniem odpowiedzi (pusta treść)."
            else:
                logging.warning(f"[{user_psid}] Gemini ({task_name}) brak treści (Finish: STOP).")
                if attempt < max_retries: time.sleep(1); continue
                else: return "Problem z generowaniem odpowiedzi (brak treści)."

        except HttpError as http_err:
            status_code = http_err.resp.status if hasattr(http_err, 'resp') else '?'; reason = http_err.resp.reason if hasattr(http_err, 'resp') else '?'
            logging.error(f"!!! BŁĄD HTTP ({status_code} {reason}) [{user_psid}] Gemini ({task_name}) - Próba {attempt}/{max_retries}.")
            if status_code in [429, 500, 503] and attempt < max_retries:
                sleep_time = (2 ** attempt) + random.random(); logging.warning(f"Oczekiwanie {sleep_time:.2f}s..."); time.sleep(sleep_time); continue
            else:
                logging.error(f"Nie ponawiam błędu HTTP {status_code}.")
                return f"Błąd komunikacji z AI (HTTP {status_code})."
        except Exception as e:
            if isinstance(e, NameError) and 'gemini_model' in str(e):
                 logging.critical(f"!!! KRYTYCZNY NameError [{user_psid}]: {e}!", exc_info=True); return "Krytyczny błąd AI."
            else:
                 logging.error(f"!!! BŁĄD Python [{user_psid}] Gemini ({task_name}) - Próba {attempt}/{max_retries}: {e}", exc_info=True)
                 if attempt < max_retries:
                     sleep_time = (2 ** attempt) + random.random(); logging.warning(f"Oczekiwanie {sleep_time:.2f}s..."); time.sleep(sleep_time); continue
                 else:
                     logging.error(f"Nie ponawiam błędu Python.")
                     return "Nieoczekiwany błąd przetwarzania."

    logging.error(f"!!! KRYTYCZNY BŁĄD [{user_psid}] Gemini ({task_name}) - Nie uzyskano odpowiedzi po {max_retries} próbach.")
    return "Nie udało się przetworzyć wiadomości. Spróbuj ponownie."

# =====================================================================
# === INSTRUKCJE SYSTEMOWE I GŁÓWNE FUNKCJE AI ========================
# =====================================================================

# (Instrukcje SYSTEM_INSTRUCTION_SCHEDULING, SYSTEM_INSTRUCTION_GATHERING, SYSTEM_INSTRUCTION_GENERAL
#  oraz funkcje get_gemini_scheduling_response, get_gemini_gathering_response, get_gemini_general_response
#  - logika wewnętrzna bez zmian, ale formatowanie i wywołania w handlerze są dostosowane)
# Definicje instrukcji (skrócone dla zwięzłości)
SYSTEM_INSTRUCTION_SCHEDULING = """Jesteś asystentem AI umawiającym korepetycje online.
Kontekst: Rozmawiasz z userem o lekcji z **{subject}**. Lista dostępnych zakresów dla {subject} ({duration} min, filtr {min_lead_hours}h) poniżej.
Styl: Naturalny, profesjonalny, bez emotek, po polsku, "Państwo".
Dostępne zakresy dla {subject}:
{available_ranges_text}
Zadanie:
1. Rozpocznij/Wznów: Potwierdź terminy dla {subject}, zapytaj o preferencje (dzień/pora). Nie proponuj konkretów.
2. Negocjuj: Na podstawie preferencji, zaproponuj konkretny termin z listy dla {subject}.
3. Potwierdź: Gdy user wybierze termin z listy dla {subject}, potwierdź go (np. "Termin na {subject} to...") i zakończ odpowiedź znacznikiem `{slot_marker_prefix}YYYY-MM-DDTHH:MM:SS{slot_marker_suffix}`.
4. NIE dodawaj znacznika: jeśli user się waha, proponuje spoza listy, brak terminów.
5. Brak terminów: Poinformuj o braku terminów dla {subject}.
6. Pytania poza tematem: Odpowiedź TYLKO znacznikiem: `{switch_marker}`.
Pamiętaj: Tylko terminy z listy dla {subject}. Znacznik ISO tylko po ustaleniu. `{switch_marker}` do pytań ogólnych."""

SYSTEM_INSTRUCTION_GATHERING = """Zbierasz dane UCZNIA po ustaleniu terminu: {proposed_slot_formatted}. Przedmiot ustalony.
Kontekst: Znane dane: Imię: {known_student_first_name}, Nazwisko: {known_student_last_name}, Klasa: {known_grade}, Poziom: {known_level}.
Styl: Naturalny, profesjonalny, bez emotek, po polsku, "Państwo".
Zadania:
1. Analiza: Sprawdź znane dane.
2. Zapytaj o brakujące: Imię+Nazwisko UCZNIA, Klasa+Typ Szkoły, Poziom (tylko liceum/tech.).
3. IGNORUJ rodzica.
4. Prowadź rozmowę.
5. Zakończ po zebraniu danych: Ostatnia odpowiedź MUSI zawierać:
   a) Linijka: `ZEBRANE_DANE_UCZNIA: [Imię: <imię>, Nazwisko: <nazwisko>, KlasaInfo: <klasa i szkoła>, Poziom: <Podst./Rozsz./Brak>]`
   b) Wiadomość: "Dobrze, dziękujemy za informacje... prosimy o potwierdzenie... https://www.facebook.com/profile.php?id=61576135251276"
   c) Znacznik na końcu: `{info_gathered_marker}`
6. NIE dodawaj znacznika/ZEBRANE_DANE, jeśli brakuje danych.
7. Pytania poza tematem: Odpowiedź TYLKO znacznikiem: `{switch_marker}`."""

SYSTEM_INSTRUCTION_GENERAL = """Jesteś asystentem klienta dla korepetycji z **{subject}**.
Styl: Naturalny, prof., bez emotek, PL, "Państwo".
Kontekst: Strona dla **{subject}**. Dostępne ogólnie: {available_subjects_list}. Cennik: SP:60, L/T Podst(1-2):65/(3-5):70, L/T Rozsz(1):65/(2):70/(3-5):75. Format: Online (Teams).
Przepływ Pracy:
1. Powitanie: Potwierdź kontekst {subject}. Od razu przejdź do kroku 2. NIE PYTAJ O PRZEDMIOT.
2. Info o Uczniu (dla {subject}): Zapytaj o Klasę+Typ Szkoły. Jeśli L/T, zapytaj o Poziom dla {subject}.
3. Cena i Format: Podaj cenę dla {subject} i format. Możesz wspomnieć o innych przedmiotach.
4. Zachęta (dla {subject}): Zapytaj o chęć umówienia lekcji z {subject}.
5. Odpowiedź na Zachętę:
   - TAK: Nic nie rób (System przejmie).
   - NIE/Wahanie: Zapytaj o powód. Wyjaśnij Online. Zaproponuj próbną z {subject}. Jeśli się zgodzi - nic nie rób. Jeśli odmówi - podziękuj.
   - Inne pytanie: Odpowiedz. Jeśli o inny przedmiot, podaj linki: "Ta strona jest dla {subject}. Inne:\n{other_subject_links}"
6. Powrót (tryb tymczasowy): Odpowiedz na pytanie. Jeśli user nie pyta dalej, dodaj `{return_marker}` na końcu.
Pamiętaj: Zakładaj {subject}. System sam przejdzie do planowania."""

def get_gemini_scheduling_response(user_psid, history_for_scheduling_ai, current_user_message_text, available_ranges, required_subject):
    """Prowadzi rozmowę planującą z AI dla konkretnego przedmiotu."""
    if not gemini_model:
        logging.error(f"[{user_psid}] Gemini niedostępny (Scheduling {required_subject})!")
        return None
    if not required_subject:
        logging.error(f"[{user_psid}] Brak przedmiotu dla Scheduling!")
        return "Błąd: Brak przedmiotu."

    ranges_text = format_ranges_for_ai(available_ranges, subject=required_subject)
    try:
        system_instruction = SYSTEM_INSTRUCTION_SCHEDULING.format(
            subject=required_subject, duration=APPOINTMENT_DURATION_MINUTES, min_lead_hours=MIN_BOOKING_LEAD_HOURS,
            available_ranges_text=ranges_text, calendar_timezone=CALENDAR_TIMEZONE,
            slot_marker_prefix=SLOT_ISO_MARKER_PREFIX, slot_marker_suffix=SLOT_ISO_MARKER_SUFFIX,
            switch_marker=SWITCH_TO_GENERAL )
    except Exception as e:
        logging.error(f"Błąd formatowania instrukcji Scheduling ({required_subject}): {e}")
        return "Błąd konfiguracji AI."

    initial_prompt = [
        Content(role="user", parts=[Part.from_text(system_instruction)]),
        Content(role="model", parts=[Part.from_text(f"OK. Ustalam termin dla **{required_subject}**.")]) ]
    full_prompt = initial_prompt + history_for_scheduling_ai
    if current_user_message_text:
        full_prompt.append(Content(role="user", parts=[Part.from_text(current_user_message_text)]))

    # Ograniczenie historii
    max_prompt_messages = (MAX_HISTORY_TURNS * 2) + 2
    while len(full_prompt) > max_prompt_messages:
        if len(full_prompt) > 3:
            full_prompt.pop(2) # user
            if len(full_prompt) > 2:
                 full_prompt.pop(2) # model
        else:
            break

    response_text = _call_gemini(user_psid, full_prompt, GENERATION_CONFIG_SCHEDULING, f"Scheduling ({required_subject})")
    if response_text:
        response_text = response_text.replace(INFO_GATHERED_MARKER, "").replace(RETURN_TO_PREVIOUS, "").strip()
        return response_text
    else:
        logging.error(f"[{user_psid}] Brak odpowiedzi Gemini (Scheduling {required_subject}).")
        return None

def get_gemini_gathering_response(user_psid, history_for_gathering_ai, current_user_message_text, context_info):
    """Prowadzi rozmowę zbierającą informacje o uczniu."""
    if not gemini_model:
        logging.error(f"[{user_psid}] Gemini niedostępny (Gathering)!")
        return None
    # Pobierz dane z kontekstu
    proposed_slot_str = context_info.get("proposed_slot_formatted", "?")
    sfn = context_info.get("known_student_first_name", "")
    sln = context_info.get("known_student_last_name", "")
    grd = context_info.get("known_grade", "")
    lvl = context_info.get("known_level", "")
    try:
        system_instruction = SYSTEM_INSTRUCTION_GATHERING.format(
            proposed_slot_formatted=proposed_slot_str, known_student_first_name=sfn, known_student_last_name=sln,
            known_grade=grd, known_level=lvl, info_gathered_marker=INFO_GATHERED_MARKER, switch_marker=SWITCH_TO_GENERAL )
    except Exception as e:
        logging.error(f"Błąd formatowania instrukcji Gathering: {e}")
        return "Błąd konfiguracji AI."

    initial_prompt = [
        Content(role="user", parts=[Part.from_text(system_instruction)]),
        Content(role="model", parts=[Part.from_text(f"OK. Sprawdzam dane (I:{sfn or '?'} N:{sln or '?'} K:{grd or '?'} P:{lvl or '?'}). Pytam o brakujące.")]) ]
    full_prompt = initial_prompt + history_for_gathering_ai
    if current_user_message_text:
        full_prompt.append(Content(role="user", parts=[Part.from_text(current_user_message_text)]))

    max_prompt_messages = (MAX_HISTORY_TURNS * 2) + 2
    while len(full_prompt) > max_prompt_messages:
        if len(full_prompt) > 3:
            full_prompt.pop(2)
            if len(full_prompt) > 2:
                 full_prompt.pop(2)
        else:
            break

    response_text = _call_gemini(user_psid, full_prompt, GENERATION_CONFIG_GATHERING, "Info Gathering")
    if response_text:
        response_text = response_text.replace(INTENT_SCHEDULE_MARKER, "").strip()
        response_text = re.sub(rf"{re.escape(SLOT_ISO_MARKER_PREFIX)}.*?{re.escape(SLOT_ISO_MARKER_SUFFIX)}", "", response_text).strip()
        response_text = response_text.replace(RETURN_TO_PREVIOUS, "").strip()
        return response_text
    else:
        logging.error(f"[{user_psid}] Brak odpowiedzi Gemini (Gathering).")
        return None

def get_gemini_general_response(user_psid, current_user_message_text, history_for_general_ai, subject, is_temporary_general_state=False):
    """Prowadzi ogólną rozmowę, zakładając przedmiot."""
    if not gemini_model:
        logging.error(f"!!! [{user_psid}] Model Gemini niedostępny (General dla {subject})!")
        return None
    if not subject:
        logging.error(f"!!! [{user_psid}] Brak przedmiotu dla General!")
        return "Błąd: Brak przedmiotu."

    try:
        system_instruction = SYSTEM_INSTRUCTION_GENERAL.format(
            subject=subject,
            available_subjects_list=", ".join(AVAILABLE_SUBJECTS),
            other_subject_links=OTHER_SUBJECT_LINKS_TEXT,
            return_marker=RETURN_TO_PREVIOUS )
    except Exception as e:
        logging.error(f"Błąd formatowania instrukcji General ({subject}): {e}")
        return "Błąd konfiguracji AI."

    model_ack = f"OK. Jestem asystentem dla **{subject}**. Potwierdzę, zbiorę dane, podam cenę dla {subject} i zapytam o lekcję."
    if is_temporary_general_state:
        model_ack += f" W trybie tymczasowym dodam {RETURN_TO_PREVIOUS} po odpowiedzi."

    initial_prompt = [
        Content(role="user", parts=[Part.from_text(system_instruction)]),
        Content(role="model", parts=[Part.from_text(model_ack)]) ]
    full_prompt = initial_prompt + history_for_general_ai
    if current_user_message_text:
        full_prompt.append(Content(role="user", parts=[Part.from_text(current_user_message_text)]))

    max_prompt_messages = (MAX_HISTORY_TURNS * 2) + 2
    while len(full_prompt) > max_prompt_messages:
        if len(full_prompt) > 3:
            full_prompt.pop(2)
            if len(full_prompt) > 2:
                 full_prompt.pop(2)
        else:
            break

    response_text = _call_gemini(user_psid, full_prompt, GENERATION_CONFIG_DEFAULT, f"General Conversation ({subject})")
    if response_text:
        response_text = re.sub(rf"{re.escape(SLOT_ISO_MARKER_PREFIX)}.*?{re.escape(SLOT_ISO_MARKER_SUFFIX)}", "", response_text).strip()
        response_text = response_text.replace(INFO_GATHERED_MARKER, "").replace(SWITCH_TO_GENERAL, "").strip()
        return response_text
    else:
        logging.error(f"[{user_psid}] Brak odpowiedzi Gemini (General {subject}).")
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
    logging.debug(f"GET Data: Mode={hub_mode}, Token={hub_token}, Challenge={hub_challenge}")
    if hub_mode == 'subscribe' and hub_token == VERIFY_TOKEN:
        logging.info("Weryfikacja GET OK!")
        return Response(hub_challenge, status=200)
    else:
        logging.warning(f"Weryfikacja GET NIEUDANA. Oczekiwany token: '{VERIFY_TOKEN}', Otrzymany: '{hub_token}'")
        return Response("Verification failed", status=403)

def find_row_and_update_sheet(psid, start_time, student_data, sheet_row_index=None):
    """Znajduje wiersz (jeśli nie podano) i aktualizuje dane Fazy 2."""
    if sheet_row_index is None:
        logging.warning(f"[{psid}] Aktualizacja Fazy 2 bez indeksu wiersza. Szukam...")
        sheet_row_index = find_row_by_psid(psid)
        if sheet_row_index is None:
            logging.error(f"[{psid}] Nie znaleziono wiersza dla PSID do aktualizacji Fazy 2.")
            return False, "Nie znaleziono powiązanego wpisu."
        else:
            logging.info(f"[{psid}] Znaleziono wiersz {sheet_row_index} dla PSID.")
    if sheet_row_index is None or not isinstance(sheet_row_index, int) or sheet_row_index < 2:
         logging.error(f"[{psid}] Nieprawidłowy indeks wiersza ({sheet_row_index}) do update_sheet_phase2.")
         return False, f"Nieprawidłowy numer wiersza ({sheet_row_index})."
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
                page_id = entry.get("id")
                if not page_id:
                    logging.warning("Pominięto wpis 'entry' bez ID strony.")
                    continue

                page_config = PAGE_ID_TO_CONFIG.get(page_id)
                if not page_config:
                    logging.error(f"!!! Otrzymano zdarzenie dla NIEZNANEJ strony PAGE_ID: {page_id}. Ignorowanie.")
                    continue

                page_name = page_config['name']
                page_subject = page_config['subject']
                page_token = page_config['token']
                logging.info(f"--- Przetwarzanie dla strony: '{page_name}' (ID: {page_id}, Przedmiot: {page_subject}) ---")

                if not page_token or len(page_token) < 50:
                     logging.critical(f"!!! BRAK/BŁĘDNY TOKEN dla strony '{page_name}'! Wysyłanie niemożliwe.")

                for event in entry.get("messaging", []):
                    sender_id = event.get("sender", {}).get("id")
                    recipient_id = event.get("recipient", {}).get("id")

                    if not sender_id or not recipient_id or recipient_id != page_id:
                        logging.warning(f"Pominięto zdarzenie: brak/niezgodne ID. Event: {event}")
                        continue

                    logging.info(f"  > Zdarzenie dla PSID: {sender_id} na Stronie: '{page_name}'")

                    history, context = load_history(sender_id, page_id) # load_history aktualizuje statystyki nowego kontaktu
                    history_for_gemini = [h for h in history if isinstance(h, Content) and h.role in ('user', 'model')]
                    current_state = context.get('type', STATE_GENERAL)
                    if 'required_subject' not in context:
                         context['required_subject'] = page_subject
                    logging.info(f"    Stan: {current_state}, Przedmiot: '{context.get('required_subject', 'Brak')}'")

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
                            continue
                        user_input_text = message_data.get("text", "").strip()
                        if user_input_text:
                            user_content = Content(role="user", parts=[Part.from_text(user_input_text)])
                            logging.info(f"    Tekst (stan={current_state}): '{user_input_text[:100]}...'")
                            if ENABLE_TYPING_DELAY:
                                time.sleep(MIN_TYPING_DELAY_SECONDS * 0.3)
                            if current_state == STATE_SCHEDULING_ACTIVE:
                                action = 'handle_scheduling'
                            elif current_state == STATE_GATHERING_INFO:
                                action = 'handle_gathering'
                            else: # General/Nieznany -> Uruchom planowanie
                                logging.info(f"    Wiadomość w stanie {current_state}. Uruchamiam Scheduling dla {page_subject}.")
                                next_state = STATE_SCHEDULING_ACTIVE
                                context_data_to_save = {'type': STATE_SCHEDULING_ACTIVE, 'required_subject': page_subject}
                                action = 'handle_scheduling'
                        elif attachments := message_data.get("attachments"):
                            att_type = attachments[0].get('type', '?')
                            logging.info(f"      Otrzymano załącznik: {att_type}.")
                            user_content = Content(role="user", parts=[Part.from_text(f"[User sent attachment: {att_type}]")])
                            msg_result = "Mogę przetwarzać tylko tekst."
                            action = 'send_info'
                            next_state = current_state
                        else:
                            action = None

                    elif postback := event.get("postback"):
                        payload = postback.get("payload")
                        title = postback.get("title", "")
                        logging.info(f"    Postback: Payload='{payload}', Tytuł='{title}' (stan={current_state})")
                        user_input_text = f"User clicked: '{title}' ({payload})"
                        user_content = Content(role="user", parts=[Part.from_text(user_input_text)])
                        if payload == "CANCEL_SCHEDULING":
                             msg_result = "Anulowano proces."
                             action = 'send_info'
                             next_state = STATE_GENERAL
                             context_data_to_save = {'type': STATE_GENERAL, 'required_subject': page_subject}
                        elif current_state == STATE_SCHEDULING_ACTIVE:
                            action = 'handle_scheduling'
                        elif current_state == STATE_GATHERING_INFO:
                            action = 'handle_gathering'
                        else: # General/Nieznany -> Uruchom planowanie
                              logging.info(f"    Postback w stanie {current_state}. Uruchamiam Scheduling dla {page_subject}.")
                              next_state = STATE_SCHEDULING_ACTIVE
                              context_data_to_save = {'type': STATE_SCHEDULING_ACTIVE, 'required_subject': page_subject}
                              action = 'handle_scheduling'
                    elif event.get("read"):
                        continue
                    elif event.get("delivery"):
                        continue
                    else:
                        logging.warning(f"    Nieobsługiwany typ zdarzenia: {event}")
                        continue

                    # --- Pętla przetwarzania akcji ---
                    loop_guard = 0
                    max_loops = 3
                    while action and loop_guard < max_loops:
                        loop_guard += 1
                        logging.debug(f"  >> Pętla {loop_guard}/{max_loops} | Akcja: {action} | Stan wej.: {current_state}")
                        current_action = action
                        action = None # Reset

                        # --- Stan Generalny (tylko pytania ogólne) ---
                        if current_action == 'handle_general':
                            logging.debug(f"  >> Wykonanie: handle_general (dla {page_subject})")
                            if user_content and user_content.parts:
                                user_message_text = user_content.parts[0].text
                                was_temporary = 'return_to_state' in context
                                current_subject_context = context_data_to_save.get('required_subject', page_subject)
                                ai_response_text_raw = get_gemini_general_response(sender_id, user_message_text, history_for_gemini, current_subject_context, was_temporary)
                                if ai_response_text_raw:
                                    model_resp_content = Content(role="model", parts=[Part.from_text(ai_response_text_raw)])
                                    if RETURN_TO_PREVIOUS in ai_response_text_raw and was_temporary:
                                        logging.info(f"      AI Ogólne -> Powrót [{RETURN_TO_PREVIOUS}].")
                                        msg_result = ai_response_text_raw.split(RETURN_TO_PREVIOUS, 1)[0].strip()
                                        next_state = context.get('return_to_state', STATE_GENERAL)
                                        context_data_to_save = context.get('return_to_context', {}).copy()
                                        context_data_to_save['type'] = next_state
                                        logging.info(f"      Przywracam stan: {next_state}.")
                                        if next_state == STATE_SCHEDULING_ACTIVE:
                                            action = 'handle_scheduling'
                                        elif next_state == STATE_GATHERING_INFO:
                                            action = 'handle_gathering'
                                            trigger_gathering_ai_immediately = True
                                        else:
                                             next_state = STATE_GENERAL
                                             context_data_to_save = {'type': STATE_GENERAL, 'required_subject': page_subject}
                                             action = None
                                        if action:
                                             current_state = next_state
                                             continue
                                    # Sprawdź, czy AI sugeruje umówienie
                                    positive_intent_keywords = [r'chc(ą|ieliby)\s+państwo\s+(umówić|ustalić)', r'czy\s+(umawiamy|ustalamy)\s+termin', r'zainteresowani\s+umówieniem', r'mogę\s+zaproponować\s+termin']
                                    schedule_intent_detected = any(re.search(p, ai_response_text_raw, re.IGNORECASE) for p in positive_intent_keywords)
                                    if schedule_intent_detected:
                                         logging.info(f"      AI Ogólne -> Sugestia umówienia dla {current_subject_context}. Przechodzę do Scheduling.")
                                         msg_result = ai_response_text_raw
                                         next_state = STATE_SCHEDULING_ACTIVE
                                         context_data_to_save = {'type': STATE_SCHEDULING_ACTIVE, 'required_subject': current_subject_context}
                                         action = 'handle_scheduling'
                                         current_state = next_state
                                         continue
                                    else: # Normalna odpowiedź
                                        logging.info(f"      AI Ogólne -> Standardowa odpowiedź.")
                                        msg_result = ai_response_text_raw
                                        next_state = STATE_GENERAL
                                        if was_temporary:
                                             context_data_to_save['return_to_state'] = context['return_to_state']
                                             context_data_to_save['return_to_context'] = context.get('return_to_context', {})
                                             context_data_to_save['type'] = STATE_GENERAL
                                        else:
                                             context_data_to_save = {'type': STATE_GENERAL, 'required_subject': current_subject_context}
                                        action = None
                                else: # Błąd AI General
                                    msg_result = "Przepraszam, problem z przetworzeniem."
                                    model_resp_content = Content(role="model", parts=[Part.from_text(msg_result)])
                                    next_state = STATE_GENERAL
                                    context_data_to_save = {'type': STATE_GENERAL, 'required_subject': page_subject}
                                    action = None
                            else:
                                logging.warning("handle_general bez user_content.")
                                action = None

                        # --- Stan Planowania ---
                        elif current_action == 'handle_scheduling':
                            logging.debug(f"  >> Wykonanie: handle_scheduling (dla {context_data_to_save.get('required_subject', '?')})")
                            required_subject = context_data_to_save.get('required_subject')
                            if not required_subject:
                                logging.error(f"!!! KRYTYCZNY BŁĄD: SCHEDULING bez 'required_subject'! PSID: {sender_id} na {page_id}")
                                msg_result = "Błąd - brak przedmiotu."; model_resp_content = Content(role="model", parts=[Part.from_text(msg_result)])
                                next_state = STATE_GENERAL; context_data_to_save = {'type': STATE_GENERAL, 'required_subject': page_subject}; action = 'send_info'
                                current_state = next_state; continue

                            subject_calendars_config = SUBJECT_TO_CALENDARS.get(required_subject.lower(), [])
                            if not subject_calendars_config:
                                logging.error(f"!!! BŁĄD KONFIGURACJI: Brak kalendarzy dla '{required_subject}'!")
                                msg_result = f"Przepraszam, brak kalendarzy dla {required_subject}."
                                other_links = [f"- {p['subject']}: {p['link']}" for p in PAGE_CONFIG if p['subject'].lower() != required_subject.lower()]
                                if other_links:
                                    msg_result += "\nProwadzimy zajęcia z:\n" + "\n".join(other_links)
                                model_resp_content = Content(role="model", parts=[Part.from_text(msg_result)])
                                next_state = STATE_GENERAL; context_data_to_save = {'type': STATE_GENERAL, 'required_subject': page_subject}; action = 'send_info'
                                current_state = next_state; continue

                            try:
                                tz = _get_calendar_timezone(); now = datetime.datetime.now(tz)
                                search_start_base = now; search_end_date = (now + datetime.timedelta(days=MAX_SEARCH_DAYS)).date()
                                search_end = tz.localize(datetime.datetime.combine(search_end_date, datetime.time(WORK_END_HOUR, 0)))
                                logging.info(f"      Pobieranie wolnych zakresów dla '{required_subject}'...")
                                _simulate_typing(sender_id, page_token, random.uniform(MIN_TYPING_DELAY_SECONDS, MAX_TYPING_DELAY_SECONDS * 0.8))
                                free_ranges = get_free_time_ranges(subject_calendars_config, search_start_base, search_end)

                                if free_ranges:
                                    logging.info(f"      Znaleziono {len(free_ranges)} zakresów dla '{required_subject}'.")
                                    current_input_text = user_content.parts[0].text if user_content and user_content.parts else None
                                    if slot_verification_failed:
                                        fail_info = f"\n[System: Poprzedni termin zajęty. Zaproponuj inny dla {required_subject}.]"
                                        current_input_text = (current_input_text or "") + fail_info; slot_verification_failed = False
                                    ai_response_text_raw = get_gemini_scheduling_response(sender_id, history_for_gemini, current_input_text, free_ranges, required_subject)

                                    if ai_response_text_raw:
                                        model_resp_content = Content(role="model", parts=[Part.from_text(ai_response_text_raw)])
                                        if ai_response_text_raw.strip() == SWITCH_TO_GENERAL:
                                            logging.info(f"      AI Planujące ({required_subject}) -> Pytanie Ogólne.")
                                            context_data_to_save['return_to_state'] = STATE_SCHEDULING_ACTIVE
                                            context_data_to_save['return_to_context'] = {'required_subject': required_subject}
                                            context_data_to_save['type'] = STATE_GENERAL; next_state = STATE_GENERAL; action = 'handle_general'
                                            msg_result = None; current_state = next_state; continue
                                        iso_match = re.search(rf"{re.escape(SLOT_ISO_MARKER_PREFIX)}(.*?){re.escape(SLOT_ISO_MARKER_SUFFIX)}", ai_response_text_raw)
                                        if iso_match: # AI ustaliło termin
                                            extracted_iso = iso_match.group(1).strip(); logging.info(f"      AI ({required_subject}) zwróciło slot: {extracted_iso}")
                                            text_for_user = re.sub(r'\s+', ' ', re.sub(rf"{re.escape(SLOT_ISO_MARKER_PREFIX)}.*?{re.escape(SLOT_ISO_MARKER_SUFFIX)}", "", ai_response_text_raw).strip())
                                            try:
                                                proposed_start = datetime.datetime.fromisoformat(extracted_iso)
                                                tz_cal = _get_calendar_timezone()
                                                if proposed_start.tzinfo is None: proposed_start = tz_cal.localize(proposed_start)
                                                else: proposed_start = proposed_start.astimezone(tz_cal)
                                                proposed_slot_formatted = format_slot_for_user(proposed_start)
                                                logging.info(f"      Weryfikacja dostępności {proposed_slot_formatted} ({required_subject})...")
                                                _simulate_typing(sender_id, page_token, MIN_TYPING_DELAY_SECONDS)

                                                chosen_calendar_id = None; chosen_calendar_name = None; sheet_blocks_slot = False
                                                min_duration_delta = datetime.timedelta(minutes=APPOINTMENT_DURATION_MINUTES); proposed_end = proposed_start + min_duration_delta
                                                potential_sheet_blockers = get_sheet_booked_slots(SPREADSHEET_ID, SHEET_NAME, proposed_start, proposed_end)
                                                for blocker in potential_sheet_blockers:
                                                    if max(proposed_start, blocker['start']) < min(proposed_end, blocker['end']):
                                                         logging.warning(f"      Weryfikacja: Slot {proposed_slot_formatted} ZAJĘTY w ARKUSZU.")
                                                         sheet_blocks_slot = True; break
                                                if not sheet_blocks_slot:
                                                    logging.debug(f"      Weryfikacja w GCal dla {required_subject}...")
                                                    for cal_config in subject_calendars_config:
                                                        cal_id = cal_config['id']; cal_name = cal_config['name']
                                                        if is_slot_actually_free(proposed_start, cal_id):
                                                            chosen_calendar_id = cal_id; chosen_calendar_name = cal_name
                                                            logging.info(f"      Slot {proposed_slot_formatted} wolny w '{cal_name}'.")
                                                            break
                                                if chosen_calendar_id: # Sukces
                                                    logging.info(f"      Wybrano '{chosen_calendar_name}'. Zapis Fazy 1...")
                                                    write_ok, write_msg_or_row = write_to_sheet_phase1(sender_id, proposed_start, chosen_calendar_name)
                                                    if write_ok:
                                                        # STATYSTYKA: Umówiony Termin
                                                        try:
                                                            appointment_date = proposed_start.date()
                                                            logging.info(f"Aktualizacja statystyk 'Umówione terminy' dla {appointment_date.strftime('%d.%m.%Y')}...")
                                                            if not update_statistics_sheet("Umówione terminy", appointment_date):
                                                                 logging.error(f"Nie udało się zaktualizować statystyki 'Umówione terminy'.")
                                                        except Exception as stat_err:
                                                            logging.error(f"Błąd aktualizacji statystyk 'Umówione terminy': {stat_err}", exc_info=True)
                                                        # Kontynuuj...
                                                        sheet_row_idx = write_msg_or_row if isinstance(write_msg_or_row, int) else None
                                                        parent_profile = get_user_profile(sender_id, page_token)
                                                        parent_fn = parent_profile.get('first_name', '') if parent_profile else ''; parent_ln = parent_profile.get('last_name', '') if parent_profile else ''
                                                        confirm_msg = text_for_user if text_for_user else f"Potwierdzam termin {proposed_slot_formatted} na {required_subject}."
                                                        confirm_msg += " Poproszę teraz o dane ucznia."
                                                        msg_result = confirm_msg; model_resp_content = Content(role="model", parts=[Part.from_text(confirm_msg)])
                                                        next_state = STATE_GATHERING_INFO
                                                        context_data_to_save = {
                                                            'type': STATE_GATHERING_INFO, 'proposed_slot_iso': proposed_start.isoformat(), 'proposed_slot_formatted': proposed_slot_formatted,
                                                            'chosen_calendar_id': chosen_calendar_id, 'chosen_calendar_name': chosen_calendar_name, 'required_subject': required_subject,
                                                            'known_parent_first_name': parent_fn, 'known_parent_last_name': parent_ln, 'known_student_first_name': '', 'known_student_last_name': '',
                                                            'known_grade': '', 'known_level': '', 'sheet_row_index': sheet_row_idx }
                                                        action = 'handle_gathering'; trigger_gathering_ai_immediately = True
                                                        current_state = next_state; continue
                                                    else: # Błąd zapisu Fazy 1
                                                        logging.error(f"Błąd zapisu Fazy 1: {write_msg_or_row}")
                                                        msg_result = f"Błąd rezerwacji ({write_msg_or_row})."; model_resp_content = Content(role="model", parts=[Part.from_text(msg_result)])
                                                        next_state = STATE_GENERAL; context_data_to_save = {'type': STATE_GENERAL, 'required_subject': page_subject}; action = None
                                                else: # Slot zajęty
                                                    blocker = 'arkuszu' if sheet_blocks_slot else f'kalendarzu dla {required_subject}'
                                                    logging.warning(f"      Weryfikacja NIEUDANA! Slot {extracted_iso} zajęty w {blocker}.")
                                                    fail_msg = f"Ojej, termin {proposed_slot_formatted} został zajęty. Spróbujmy inny dla {required_subject}."
                                                    msg_result = fail_msg; fail_info_for_ai = f"\n[System: Termin {proposed_slot_formatted} zajęty. Zaproponuj inny.]"
                                                    model_resp_content = Content(role="model", parts=[Part.from_text(ai_response_text_raw + fail_info_for_ai)])
                                                    next_state = STATE_SCHEDULING_ACTIVE; slot_verification_failed = True
                                                    context_data_to_save['type'] = STATE_SCHEDULING_ACTIVE; action = None
                                            except ValueError:
                                                logging.error(f"BŁĄD: AI ({required_subject}) zwróciło nieprawidłowy ISO: '{extracted_iso}'.")
                                                msg_result = "Błąd terminu. Wybierzmy jeszcze raz."; model_resp_content = Content(role="model", parts=[Part.from_text(msg_result)])
                                                next_state = STATE_SCHEDULING_ACTIVE; context_data_to_save['type'] = STATE_SCHEDULING_ACTIVE; action = None
                                            except Exception as verif_err:
                                                logging.error(f"BŁĄD weryfikacji/zapisu {extracted_iso} ({required_subject}): {verif_err}", exc_info=True)
                                                msg_result = "Błąd rezerwacji."; model_resp_content = Content(role="model", parts=[Part.from_text(msg_result)])
                                                next_state = STATE_GENERAL; context_data_to_save = {'type': STATE_GENERAL, 'required_subject': page_subject}; action = None
                                        else: # AI kontynuuje rozmowę
                                            logging.info(f"AI Planujące ({required_subject}) kontynuuje rozmowę.")
                                            msg_result = ai_response_text_raw
                                            next_state = STATE_SCHEDULING_ACTIVE; context_data_to_save['type'] = STATE_SCHEDULING_ACTIVE; action = None
                                    else: # Błąd AI Scheduling
                                        logging.error(f"BŁĄD: AI Planujące ({required_subject}) nie zwróciło odpowiedzi.")
                                        msg_result = f"Problem z planowaniem dla {required_subject}."
                                        model_resp_content = Content(role="model", parts=[Part.from_text(msg_result)])
                                        next_state = STATE_GENERAL; context_data_to_save = {'type': STATE_GENERAL, 'required_subject': page_subject}; action = None
                                else: # Brak wolnych zakresów
                                    logging.warning(f"Brak wolnych zakresów dla '{required_subject}'.")
                                    no_slots_msg = f"Niestety, brak wolnych terminów na **{required_subject}** w najbliższym czasie."
                                    other_links = [f"- {p['subject']}: {p['link']}" for p in PAGE_CONFIG if p['subject'].lower() != required_subject.lower()]
                                    if other_links:
                                        no_slots_msg += "\nMoże inny przedmiot?\n" + "\n".join(other_links)
                                    msg_result = no_slots_msg; model_resp_content = Content(role="model", parts=[Part.from_text(no_slots_msg)])
                                    next_state = STATE_GENERAL; context_data_to_save = {'type': STATE_GENERAL, 'required_subject': page_subject}; action = None
                            except Exception as schedule_err:
                                logging.error(f"KRYTYCZNY BŁĄD 'handle_scheduling' dla {required_subject}: {schedule_err}", exc_info=True)
                                msg_result = "Nieoczekiwany błąd planowania."
                                model_resp_content = Content(role="model", parts=[Part.from_text(msg_result)])
                                next_state = STATE_GENERAL; context_data_to_save = {'type': STATE_GENERAL, 'required_subject': page_subject}; action = None

                        # --- Stan Zbierania Informacji ---
                        elif current_action == 'handle_gathering':
                            logging.debug(f"  >> Wykonanie: handle_gathering (dla {context_data_to_save.get('required_subject', '?')})")
                            current_subject = context_data_to_save.get('required_subject', page_subject)
                            try:
                                known_info_for_ai = context_data_to_save.copy()
                                current_input_text = None
                                if trigger_gathering_ai_immediately:
                                    logging.info("Inicjuję AI zbierające (trigger).")
                                    trigger_gathering_ai_immediately = False
                                elif user_content and user_content.parts:
                                    current_input_text = user_content.parts[0].text
                                ai_response_text_raw = get_gemini_gathering_response(sender_id, history_for_gemini, current_input_text, known_info_for_ai)
                                if ai_response_text_raw:
                                    model_resp_content = Content(role="model", parts=[Part.from_text(ai_response_text_raw)])
                                    if ai_response_text_raw.strip() == SWITCH_TO_GENERAL:
                                        logging.info(f"AI Zbierające -> Pytanie Ogólne.")
                                        context_data_to_save['return_to_state'] = STATE_GATHERING_INFO
                                        context_data_to_save['return_to_context'] = context_data_to_save.copy()
                                        context_data_to_save['type'] = STATE_GENERAL; next_state = STATE_GENERAL; action = 'handle_general'
                                        msg_result = None; current_state = next_state; continue
                                    if INFO_GATHERED_MARKER in ai_response_text_raw:
                                        logging.info(f"AI Zbierające -> Koniec [{INFO_GATHERED_MARKER}]. Parsowanie i Faza 2.")
                                        response_parts = ai_response_text_raw.split(INFO_GATHERED_MARKER, 1); ai_full_resp_before_marker = response_parts[0].strip()
                                        final_msg_for_user = ""; parsed_student_data = {}; data_line_found = False
                                        data_regex = r"ZEBRANE_DANE_UCZNIA:\s*\[Imię:\s*(.*?),?\s*Nazwisko:\s*(.*?),?\s*KlasaInfo:\s*(.*?),?\s*Poziom:\s*(.*?)\]"
                                        match = re.search(data_regex, ai_full_resp_before_marker, re.IGNORECASE | re.DOTALL)
                                        if match:
                                            data_line_found = True; logging.debug("Znaleziono regex ZEBRANE_DANE_UCZNIA.")
                                            s_fn = match.group(1).strip() or "Brak"; s_ln = match.group(2).strip() or "Brak"
                                            g_info = match.group(3).strip() or "Brak"; l_info = match.group(4).strip() or "Brak"; l_info = "Brak" if l_info.lower() == 'brak' else l_info
                                            parsed_student_data = {'student_first_name': s_fn, 'student_last_name': s_ln, 'grade_info': g_info, 'level_info': l_info}
                                            logging.info(f"Sparsowano z AI: {parsed_student_data}"); final_msg_for_user = ai_full_resp_before_marker[match.end():].strip()
                                        else:
                                            logging.error(f"!!! BŁĄD: Nie znaleziono 'ZEBRANE_DANE_UCZNIA:'!"); final_msg_for_user = ai_full_resp_before_marker
                                            parsed_student_data = {'student_first_name': 'Błąd', 'student_last_name': 'Błąd', 'grade_info': 'Błąd', 'level_info': 'Błąd'}
                                        if not final_msg_for_user:
                                            final_msg_for_user = "Dziękujemy. Prosimy o potwierdzenie wysyłając \"POTWIERDZAM\" na https://www.facebook.com/profile.php?id=61576135251276"
                                            logging.warning("Użyto domyślnej wiadomości końcowej (Gathering).")
                                        try: # Aktualizacja Fazy 2
                                            p_fn = context_data_to_save.get('known_parent_first_name', '?'); p_ln = context_data_to_save.get('known_parent_last_name', '?'); sheet_row_idx = context_data_to_save.get('sheet_row_index')
                                            full_data_for_update = {'parent_first_name': p_fn, 'parent_last_name': p_ln, **parsed_student_data}
                                            update_ok, update_msg = find_row_and_update_sheet(sender_id, None, full_data_for_update, sheet_row_idx)
                                            if update_ok:
                                                logging.info(f"Aktualizacja Fazy 2 OK."); msg_result = final_msg_for_user
                                                next_state = STATE_GENERAL; context_data_to_save = {'type': STATE_GENERAL, 'required_subject': page_subject}; action = None
                                            else:
                                                logging.error(f"BŁĄD aktualizacji Fazy 2: {update_msg}"); msg_result = f"Problem z zapisem ({update_msg})."
                                                model_resp_content = Content(role="model", parts=[Part.from_text(ai_full_resp_before_marker + f"\n[System Error: {update_msg}]")])
                                                next_state = STATE_GENERAL; context_data_to_save = {'type': STATE_GENERAL, 'required_subject': page_subject}; action = None
                                        except Exception as sheet_update_err:
                                            logging.error(f"KRYTYCZNY BŁĄD Fazy 2: {sheet_update_err}", exc_info=True); msg_result = "Krytyczny błąd zapisu."
                                            model_resp_content = Content(role="model", parts=[Part.from_text(msg_result)]); next_state = STATE_GENERAL; context_data_to_save = {'type': STATE_GENERAL, 'required_subject': page_subject}; action = None
                                    else: # AI kontynuuje zbieranie
                                        logging.info("AI Zbierające kontynuuje rozmowę."); msg_result = ai_response_text_raw
                                        next_state = STATE_GATHERING_INFO; context_data_to_save['type'] = STATE_GATHERING_INFO; action = None
                                else: # Błąd AI Gathering
                                    logging.error(f"BŁĄD: AI Zbierające nie zwróciło odpowiedzi."); msg_result = "Błąd systemu zbierania informacji."
                                    model_resp_content = Content(role="model", parts=[Part.from_text(msg_result)]); next_state = STATE_GATHERING_INFO; context_data_to_save['type'] = STATE_GATHERING_INFO; action = None
                            except Exception as gather_err:
                                logging.error(f"KRYTYCZNY BŁĄD 'handle_gathering': {gather_err}", exc_info=True); msg_result = "Nieoczekiwany błąd zbierania informacji."
                                model_resp_content = Content(role="model", parts=[Part.from_text(msg_result)]); next_state = STATE_GENERAL; context_data_to_save = {'type': STATE_GENERAL, 'required_subject': page_subject}; action = None

                        # --- Akcja Wysyłania Informacji ---
                        elif current_action == 'send_info':
                            logging.debug("  >> Wykonanie: send_info")
                            if msg_result and not model_resp_content:
                                model_resp_content = Content(role="model", parts=[Part.from_text(msg_result)])
                            elif not msg_result:
                                logging.warning(f"send_info bez wiadomości.")
                            if 'type' not in context_data_to_save:
                                context_data_to_save['type'] = next_state
                            action = None
                        else:
                            logging.error(f"   Nieznana akcja '{current_action}'."); action = None

                    # --- Koniec pętli ---
                    logging.debug(f"  << Koniec pętli. Finalny stan: {next_state}")

                    # --- Zapis Stanu i Historii ---
                    final_context_to_save_dict = context_data_to_save.copy()
                    final_context_to_save_dict['type'] = next_state
                    if next_state != STATE_GENERAL or 'return_to_state' not in final_context_to_save_dict:
                         final_context_to_save_dict.pop('return_to_state', None)
                         final_context_to_save_dict.pop('return_to_context', None)

                    if msg_result:
                        # Wysyłaj tylko jeśli jest token
                        if page_token and len(page_token) > 50:
                            send_message(sender_id, page_token, msg_result)
                        else:
                            logging.error(f"Nie wysłano wiadomości do {sender_id} na {page_name} z powodu braku/błędnego tokena.")
                    elif current_action:
                        logging.debug(f"Akcja '{current_action}' zakończona bez wiadomości.")

                    original_context_no_return = context.copy()
                    original_context_no_return.pop('return_to_state', None)
                    original_context_no_return.pop('return_to_context', None)
                    should_save = bool(user_content) or bool(model_resp_content) or (original_context_no_return != final_context_to_save_dict)

                    if should_save:
                        history_to_save = list(history_for_gemini)
                        if user_content:
                            history_to_save.append(user_content)
                        if model_resp_content:
                            history_to_save.append(model_resp_content)
                        max_hist_len = MAX_HISTORY_TURNS * 2
                        history_to_save = history_to_save[-max_hist_len:]
                        logging.info(f"Zapisywanie historii ({len(history_to_save)}) dla [{page_id}/{sender_id}]. Stan: {final_context_to_save_dict.get('type')}")
                        save_history(sender_id, page_id, history_to_save, context_to_save=final_context_to_save_dict)
                    else:
                        logging.debug("Brak zmian - pomijanie zapisu.")

            logging.info(f"--- Zakończono przetwarzanie batcha dla strony: '{page_name}' ---")
            return Response("EVENT_RECEIVED", status=200)
        else:
            logging.warning(f"POST /webhook, ale obiekt != 'page'. Typ: {type(data)}.")
            return Response("OK", status=200)
    except json.JSONDecodeError as e:
        logging.error(f"BŁĄD dekodowania JSON: {e}", exc_info=True)
        logging.error(f"Dane: {raw_data[:500]}...")
        return Response("Invalid JSON", status=400)
    except Exception as e:
        logging.critical(f"KRYTYCZNY BŁĄD POST /webhook: {e}", exc_info=True)
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
    logging.getLogger('googleapiclient._helpers').setLevel(logging.WARNING)
    logging.getLogger('urllib3.connectionpool').setLevel(logging.WARNING)
    logging.getLogger('werkzeug').setLevel(logging.WARNING)

    print("\n" + "="*60)
    print("--- START BOTA (Wersja Multi-Strona / Multi-Przedmiot + Statystyki) ---")
    print(f"  * Poziom logowania: {logging.getLevelName(log_level)}")
    print("-" * 60)
    print("  Konfiguracja Facebook:")
    print(f"    FB_VERIFY_TOKEN: {'OK' if VERIFY_TOKEN and VERIFY_TOKEN != 'KOLAGEN' else 'DOMYŚLNY/BRAK!'}")
    print("    Obsługiwane strony:")
    for cfg in PAGE_CONFIG:
         token_status = "OK" if cfg.get('token') and len(cfg['token']) > 50 else "BRAK/BŁĘDNY!"
         print(f"      - '{cfg.get('name','?')}' (ID: {cfg.get('id','?')}, Przedmiot: {cfg.get('subject','?')}, Token: {token_status}, Link: {cfg.get('link','?')})")
         if token_status != "OK":
             print(f"!!! KRYTYCZNE: Sprawdź token dla strony '{cfg.get('name','?')}' !!!")
    print("-" * 60)
    print("  Konfiguracja Vertex AI:")
    print(f"    Projekt: {PROJECT_ID}, Lokalizacja: {LOCATION}, Model: {MODEL_ID}")
    print(f"    Model Gemini: {'Załadowany (OK)' if gemini_model else 'BŁĄD ŁADOWANIA!'}")
    print("-" * 60)
    print("  Konfiguracja Google Calendar:")
    print(f"    Dostępne przedmioty (z kalendarzy): {', '.join(SUBJECT_TO_CALENDARS.keys())}")
    print("    Przypisanie Kalendarzy do Przedmiotów:")
    if SUBJECT_TO_CALENDARS:
        for subject, cal_list in SUBJECT_TO_CALENDARS.items():
            print(f"      - {subject.capitalize()}: {', '.join([f'{c["name"]}' for c in cal_list])}")
    else:
        print("      !!! BRAK kalendarzy przypisanych do przedmiotów !!!")
    if 'angielski' not in SUBJECT_TO_CALENDARS:
        print("      !!! OSTRZEŻENIE: Brak kalendarza dla Angielskiego w konfiguracji CALENDARS!")
    print(f"    Strefa: {CALENDAR_TIMEZONE}")
    print(f"    Filtry: Godz. {WORK_START_HOUR}-{WORK_END_HOUR}, Wyprz. {MIN_BOOKING_LEAD_HOURS}h, Zakres {MAX_SEARCH_DAYS}dni")
    cal_key_status = 'OK' if os.path.exists(CALENDAR_SERVICE_ACCOUNT_FILE) else 'BRAK!!!'
    print(f"    Plik klucza: {CALENDAR_SERVICE_ACCOUNT_FILE} ({cal_key_status})")
    cal_service = get_calendar_service()
    print(f"    Usługa Calendar API: {'OK' if cal_service else 'BŁĄD!'}")
    print("-" * 60)
    print("  Konfiguracja Google Sheets:")
    print(f"    Główny arkusz: '{SHEET_NAME}' (ID: {SPREADSHEET_ID})")
    print(f"    Arkusz statystyk: '{STATS_SHEET_NAME}'")
    print(f"    Strefa: {SHEET_TIMEZONE}")
    print(f"    Klucze statystyk (Wiersz): {STATS_ROW_MAP}")
    sheet_key_status = 'OK' if os.path.exists(SHEETS_SERVICE_ACCOUNT_FILE) else 'BRAK!!!'
    print(f"    Plik klucza: {SHEETS_SERVICE_ACCOUNT_FILE} ({sheet_key_status})")
    sheets_service = get_sheets_service()
    print(f"    Usługa Sheets API: {'OK' if sheets_service else 'BŁĄD!'}")
    print("--- KONIEC KONFIGURACJI ---")
    print("="*60 + "\n")

    port = int(os.environ.get("PORT", 8080))
    run_flask_in_debug = (log_level == logging.DEBUG)

    print(f"Uruchamianie serwera Flask na porcie {port}...")
    if not run_flask_in_debug:
        try:
            from waitress import serve
            print(">>> Uruchamianie serwera produkcyjnego Waitress <<<")
            serve(app, host='0.0.0.0', port=port, threads=16)
        except ImportError:
            print("!!! Ostrz.: 'waitress' nie znaleziono. Uruchamiam Flask dev. !!!")
            app.run(host='0.0.0.0', port=port, debug=False)
    else:
        print(">>> Uruchamianie serwera deweloperskiego Flask (DEBUG) <<<")
        app.run(host='0.0.0.0', port=port, debug=True, use_reloader=False)
