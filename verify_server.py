# -*- coding: utf-8 -*-

# verify_server.py (Wersja: Wiele Stron FB + Statystyki + Poprawki)

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
from collections import defaultdict # Import defaultdict
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# --- Konfiguracja Stron Facebook ---
# Zamiast pojedynczego PAGE_ACCESS_TOKEN, używamy słownika
# Klucz: Page ID (ID strony odbiorcy), Wartość: Słownik {'token': '...', 'subject': '...'}
PAGE_CONFIG = {
    # Polski: Zakrzeczone Korepetycje - Polski (ID: 661857023673365)
    "661857023673365": {
        "token": "EACNAHFzEhkUBO5y1aIKOaYcZCKRz2fS6PpPUwPrdqaYgrJGL8KmAVJtXtwbZAzfzSZAREL67A0Go2xcnYgXy4rwZBwmlrraoQZASwZBZAJFEzzZCwL2vsX8lgodNtr2TiAAN4obiESu4TqLU3OwRbCnHgdDX8dRpaUl1lmO9ZAB8TTfkJ0OVQ9QaQJG7njwhugnHMrgZDZD",
        "subject": "Polski",
        "name": "Zakrzeczone Korepetycje - Polski",
        "link": "https://tiny.pl/0xnsgbt2" # Link do strony z Polskim
    },
    # Matematyka: ZakrzeczoneKorepetycje - MATEMATYKA... (ID: 638454406015018)
    "638454406015018": {
        "token": "EACNAHFzEhkUBO3hU8CvarZBMqnOeXFZC8v0haCt1fcWIwhiXGQpx98ZBEEGBmoZBwQZADqSSmeb9Py45ie7gXrh5yPCtvVi4aTZBwbggPgjaZCPzRo8dNndqfavc2ZCBZCCtkvVOkPOTs6c9lxmCIdvN1TCzeBszpr3i7n8uUoFme81bZBfm7y5LmwCDYZBy8xMllIy8AZDZD",
        "subject": "Matematyka",
        "name": "Zakrzeczone Korepetycje - MATEMATYKA",
        "link": "https://tiny.pl/f7xz5n0g" # Link do strony z Matematyką
    },
     # Angielski: English Zone: Zakrzeczone Korepetycje (ID: 653018101222547)
    "653018101222547": {
        "token": "EACNAHFzEhkUBOZC2RxQhFkPJOh4H9vMKZCt0KOCFPBscZCGurYZANYFEOkPyaKcsr88PeP36idt6UiXN2fzBKFdqWSxnaqF1WeZAJU3g6wYOVPTHLNjNl6HcW9GHRgTxEdjQdAMDRnnIgkCtGJCe4pSVEIk7yYRXrcfEam5XY6mXabBvqrlDzZBCLHonZCFRyIuuAZDZD",
        "subject": "Angielski",
        "name": "English Zone: Zakrzeczone Korepetycje",
        "link": "https://tiny.pl/prrr7qf1" # Link do strony z Angielskim
    },
    # Możesz dodać więcej stron tutaj, jeśli będzie potrzeba
}
# Utwórz listę linków do innych przedmiotów
ALL_SUBJECT_LINKS = {
    page_data["subject"]: page_data["link"]
    for page_data in PAGE_CONFIG.values() if "subject" in page_data and "link" in page_data
}

app = Flask(__name__)

# --- Konfiguracja Ogólna ---
VERIFY_TOKEN = os.environ.get("FB_VERIFY_TOKEN", "KOLAGEN") # Używane tylko do weryfikacji webhooka
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

# --- NOWOŚĆ: Lista przedmiotów (automatycznie z PAGE_CONFIG) ---
AVAILABLE_SUBJECTS = sorted(list(ALL_SUBJECT_LINKS.keys()))

# Lista kalendarzy do sprawdzania Z PRZEDMIOTAMI
CALENDARS = [
    {
        'id': 'f19e189826b9d6e36950da347ac84d5501ecbd6bed0d76c8641be61a67749c67@group.calendar.google.com',
        'name': 'Kalendarz Główny Polski', # Zaktualizuj nazwę dla jasności
        'subject': 'Polski' # Przypisanie przedmiotu
    },
    {
        'id': '3762cdf9ca674ed1e5dd87ff406dc92f365121aab827cea4d9a02085d31d15fb@group.calendar.google.com',
        'name': 'Kalendarz Dodatkowy Matematyka', # Zaktualizuj nazwę
        'subject': 'Matematyka' # Przypisanie przedmiotu
    },
    # DODAJ TUTAJ Kalendarz dla Angielskiego, jeśli istnieje
    # Przykład:
    # {
    #     'id': 'TWOJ_KALENDARZ_ID_ANGIELSKI@group.calendar.google.com',
    #     'name': 'Kalendarz Angielski',
    #     'subject': 'Angielski'
    # },
]
# --- Mapowanie Przedmiot -> Lista Kalendarzy ---
SUBJECT_TO_CALENDARS = defaultdict(list)
for cal_config in CALENDARS:
    if 'subject' in cal_config and cal_config['subject'] in AVAILABLE_SUBJECTS:
        SUBJECT_TO_CALENDARS[cal_config['subject'].lower()].append(cal_config)
    else:
        logging.warning(f"Kalendarz '{cal_config['name']}' nie ma przypisanego poprawnego przedmiotu lub brak klucza 'subject'. Pomijanie.")

# Stare zmienne - zachowane dla kompatybilności tam, gdzie nie potrzeba filtrowania po przedmiocie
ALL_CALENDAR_IDS = [cal['id'] for cal in CALENDARS]
ALL_CALENDAR_ID_TO_NAME = {cal['id']: cal['name'] for cal in CALENDARS}

MAX_SEARCH_DAYS = 14
MIN_BOOKING_LEAD_HOURS = 24

# --- Konfiguracja Google Sheets (ZAPIS + ODCZYT) ---
SHEETS_SERVICE_ACCOUNT_FILE = 'arkuszklucz.json'
SHEET_SCOPES = ['https://www.googleapis.com/auth/spreadsheets']
SPREADSHEET_ID = os.environ.get("GOOGLE_SHEET_ID", "1vpsIAEkqtY3ZJ5Mr67Dda45aZ55V1O-Ux9ODjwk13qw")
MAIN_SHEET_NAME = os.environ.get("GOOGLE_SHEET_NAME", 'Arkusz1') # Główne rezerwacje
STATS_SHEET_NAME = 'Arkusz2' # Nazwa arkusza statystyk
SHEET_TIMEZONE = 'Europe/Warsaw'

# Definicja kolumn (zaczynając od 1) dla Arkusz1
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
SHEET_READ_RANGE_FOR_PSID_SEARCH = f"{MAIN_SHEET_NAME}!A2:A"
SHEET_READ_RANGE_FOR_BUSY_SLOTS = f"{MAIN_SHEET_NAME}!F2:K" # Odczyt od F do K, zaczynając od wiersza 2

# Definicje dla Arkusz2 (Statystyki)
STATS_DATE_HEADER_ROW = 1 # Wiersz z datami
STATS_NEW_CONTACT_ROW_LABEL = "Nowe kontakty" # Etykieta w kolumnie A
STATS_BOOKING_ROW_LABEL = "Umówione terminy" # Etykieta w kolumnie A
STATS_DATA_START_COLUMN = 'B' # Pierwsza kolumna z danymi (np. 5.5.2025)


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

def get_user_profile(psid, page_access_token): # Dodano page_access_token
    """Pobiera podstawowe dane profilu użytkownika z Facebook Graph API."""
    if not page_access_token or len(page_access_token) < 50:
        logging.warning(f"[{psid}] Brak/nieprawidłowy page_access_token do pobrania profilu.")
        return None
    user_profile_api_url_template = "https://graph.facebook.com/v19.0/{psid}?fields=first_name,last_name,profile_pic&access_token={token}"
    url = user_profile_api_url_template.format(psid=psid, token=page_access_token) # Użyj przekazanego tokenu
    logging.debug(f"--- [{psid}] Pobieranie profilu użytkownika z FB API...")
    profile_data = {}
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        data = r.json()
        if 'error' in data:
            logging.error(f"BŁĄD FB API (pobieranie profilu) dla PSID {psid}: {data['error']}")
            if data['error'].get('code') == 190:
                logging.error(f"!!! Wygląda na to, że token strony (dla PSID {psid}) jest nieprawidłowy lub wygasł !!!")
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
    default_context = {'type': STATE_GENERAL} # Domyślny kontekst

    if not os.path.exists(filepath):
        logging.info(f"[{user_psid}] Plik historii nie istnieje, zwracam stan domyślny {STATE_GENERAL}.")
        return history, default_context.copy(), True # Zwróć True dla nowego kontaktu

    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            history_data = json.load(f)
            if isinstance(history_data, list):
                last_system_message_index = -1
                system_context_found = False
                # Iteruj od końca, aby znaleźć najnowszy kontekst systemowy
                for i, msg_data in enumerate(reversed(history_data)):
                    if isinstance(msg_data, dict) and msg_data.get('role') == 'system':
                        state_type = msg_data.get('type')
                        if state_type and state_type in valid_states:
                            context = msg_data.copy() # Kopiuj znaleziony kontekst
                            context.pop('role', None) # Usuń klucz 'role' z kontekstu
                            logging.debug(f"[{user_psid}] Odczytano AKTYWNY kontekst: {context}")
                            system_context_found = True
                        elif state_type:
                            logging.warning(f"[{user_psid}] Znaleziono kontekst w pliku {filepath}, ale z nieprawidłowym typem: {msg_data}. Używam domyślnego {STATE_GENERAL}.")
                            context = default_context.copy()
                        else:
                            logging.warning(f"[{user_psid}] Znaleziono kontekst systemowy w pliku {filepath}, ale bez typu: {msg_data}. Używam domyślnego {STATE_GENERAL}.")
                            context = default_context.copy()
                        last_system_message_index = len(history_data) - 1 - i
                        break # Znaleziono ostatni kontekst systemowy, przerwij

                if not system_context_found:
                    logging.debug(f"[{user_psid}] Nie znaleziono poprawnego kontekstu systemowego na końcu pliku {filepath}. Ustawiam stan {STATE_GENERAL}.")
                    context = default_context.copy()

                # Wczytaj historię wiadomości (wszystkie przed ostatnim systemowym lub wszystkie jeśli nie ma systemowego)
                limit_index = last_system_message_index if system_context_found else len(history_data)
                for i, msg_data in enumerate(history_data[:limit_index]):
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
                    else:
                        # Ignoruj stare konteksty systemowe lub niepoprawne wpisy
                        logging.debug(f"Ostrz. [{user_psid}]: Pominięto niepoprawną/starą wiadomość/kontekst (idx {i}) w pliku {filepath}: {msg_data}")


                logging.info(f"[{user_psid}] Wczytano historię z {filepath}: {len(history)} wiad. Stan: {context.get('type', STATE_GENERAL)}")
                return history, context, False # Zwróć False dla istniejącego kontaktu

            else:
                logging.error(f"BŁĄD [{user_psid}]: Plik historii {filepath} nie jest listą.")
                return [], default_context.copy(), False # Załóż, że nie jest nowy
    except FileNotFoundError:
        logging.info(f"[{user_psid}] Plik historii {filepath} nie istnieje.")
        return [], default_context.copy(), True # Zwróć True dla nowego kontaktu
    except (json.JSONDecodeError, KeyError, IndexError, TypeError) as e:
        logging.error(f"BŁĄD [{user_psid}] parsowania historii z {filepath}: {e}.")
        try:
            os.rename(filepath, f"{filepath}.error_{int(time.time())}")
            logging.warning("    Zmieniono nazwę uszkodzonego pliku historii.")
        except OSError as rename_err:
            logging.error(f"    Nie udało się zmienić nazwy: {rename_err}")
        return [], default_context.copy(), False # Załóż, że nie jest nowy
    except Exception as e:
        logging.error(f"BŁĄD [{user_psid}] wczytywania historii z {filepath}: {e}", exc_info=True)
        return [], default_context.copy(), False # Załóż, że nie jest nowy


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

        # Najpierw dodaj wiadomości do listy
        for msg in history_to_process:
            if isinstance(msg, Content) and hasattr(msg, 'role') and msg.role in ('user', 'model') and hasattr(msg, 'parts') and isinstance(msg.parts, list):
                parts_data = [{'text': part.text} for part in msg.parts if isinstance(part, Part) and hasattr(part, 'text')]
                if parts_data:
                    history_data.append({'role': msg.role, 'parts': parts_data})
            else:
                logging.warning(f"Ostrz. [{user_psid}]: Pomijanie nieprawidłowego obiektu historii podczas zapisu: {type(msg)}")

        # Następnie dodaj kontekst systemowy NA KOŃCU listy
        current_state_to_save = STATE_GENERAL
        if context_to_save and isinstance(context_to_save, dict):
            context_copy = context_to_save.copy()
            current_state_to_save = context_copy.get('type', STATE_GENERAL)
            context_copy['role'] = 'system' # Dodaj rolę 'system'
            # Zapisuj kontekst zawsze, chyba że jest to domyślny {'type': 'general'} bez dodatkowych pól
            is_default_general = (current_state_to_save == STATE_GENERAL and
                                  len(context_copy) == 2 and # tylko 'type' i 'role'
                                  'type' in context_copy and 'role' in context_copy)
            if not is_default_general:
                 history_data.append(context_copy)
                 logging.debug(f"[{user_psid}] Dodano kontekst {current_state_to_save} do zapisu: {context_copy}")
            else:
                 logging.debug(f"[{user_psid}] Pominięto zapis domyślnego kontekstu 'general'.")

        else:
            logging.debug(f"[{user_psid}] Brak kontekstu do zapisu lub niepoprawny typ kontekstu.")


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
                # Próba usunięcia słowa kluczowego typu szkoły z opisu
                # Używamy \b dla granic słów, aby uniknąć częściowych dopasowań
                # Dodajemy opcjonalne 'klasa', 'klasy' przed/po
                pattern_to_remove = r'(?i)(\bklas[ay]?\s+)?\b' + re.escape(match.group(0)) + r'\b(\s+\bklas[ay]?\b)?\s*'
                # Zastąp znaleziony wzorzec spacją, a następnie usuń nadmiarowe spacje
                cleaned_desc_candidate = re.sub(pattern_to_remove, ' ', class_desc, count=1).strip()
                # Usuń również "klasa" jeśli pozostało na początku/końcu
                cleaned_desc_candidate = re.sub(r'^\bklas[ay]?\b\s*|\s*\bklas[ay]?\b$', '', cleaned_desc_candidate, flags=re.IGNORECASE).strip()

                # Aktualizuj opis klasy tylko jeśli czyszczenie coś zmieniło i nie jest puste
                if cleaned_desc_candidate and cleaned_desc_candidate != class_desc:
                    class_desc = cleaned_desc_candidate
                # Jeśli po czyszczeniu nic nie zostało, spróbuj znaleźć sam numer
                elif not cleaned_desc_candidate:
                    num_match_inner = re.search(r'\b(\d+)\b', grade_lower)
                    class_desc = num_match_inner.group(1) if num_match_inner else ""

                found_type = True
                break
        if found_type:
            break

    # Jeśli typ szkoły nie został znaleziony przez słowa kluczowe, ale jest numer
    if school_type == "Nieokreślona":
        num_match_outer = re.search(r'\b\d+\b', grade_lower)
        if num_match_outer:
            school_type = "Inna (z numerem klasy)"
            # Jeśli opis klasy jest nadal oryginalnym ciągiem, zastąp go numerem
            if class_desc == grade_string.strip():
                class_desc = num_match_outer.group(0)

    # Ostateczne wyodrębnienie numeru klasy, niezależnie od typu szkoły
    num_match_final = re.search(r'\b(\d+)\b', grade_string)
    if num_match_final:
        numerical_grade = num_match_final.group(1)

    # Ostateczne czyszczenie słowa "klasa" z opisu, jeśli nadal tam jest
    class_desc = re.sub(r'\bklas[ay]?\b', '', class_desc, flags=re.IGNORECASE).strip()
    # Jeśli opis jest pusty po czyszczeniu, wróć do oryginalnego stringu (lub numeru jeśli jest)
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
            # Wydarzenia całodniowe mogą blokować cały dzień, ale nasz system szuka slotów godzinowych.
            # Decyzja: Ignorujemy wydarzenia całodniowe w kontekście szukania slotów godzinowych.
            # Jeśli polityka ma być inna (np. blokować cały dzień roboczy), trzeba to zmienić.
            logging.debug(f"Ignorowanie wydarzenia całodniowego: {dt_str}")
            return None # Zwracamy None, aby nie brać go pod uwagę jako zajętego slotu czasowego
        else:
            # Obsługa formatu ISO 8601 z 'Z' lub offsetem
            if dt_str.endswith('Z'):
                 # Zamień 'Z' na +00:00 dla `fromisoformat`
                 dt_str = dt_str[:-1] + '+00:00'

            dt = datetime.datetime.fromisoformat(dt_str)

            # Sprawdzenie, czy datetime jest świadomy strefy czasowej
            if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
                # Jeśli czas jest naiwny (brak informacji o strefie), lokalizuj go używając domyślnej strefy
                logging.warning(f"Ostrz.: dateTime '{event_time_data.get('dateTime', dt_str)}' jako naiwny. Zakładam strefę {default_tz.zone}.")
                dt_aware = default_tz.localize(dt)
            else:
                # Jeśli czas jest świadomy, przekonwertuj go do domyślnej strefy czasowej
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

    # Zapewnij, że daty graniczne są świadome strefy czasowej kalendarza
    if start_datetime.tzinfo is None:
        start_datetime = tz.localize(start_datetime)
    else:
        start_datetime = start_datetime.astimezone(tz)
    if end_datetime.tzinfo is None:
        end_datetime = tz.localize(end_datetime)
    else:
        end_datetime = end_datetime.astimezone(tz)


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
            # Użyj ALL_CALENDAR_ID_TO_NAME do pobrania nazwy
            cal_name = ALL_CALENDAR_ID_TO_NAME.get(cal_id, cal_id)
            if 'errors' in calendar_data:
                for error in calendar_data['errors']:
                    logging.error(f"Błąd API Freebusy dla '{cal_name}': {error.get('reason')} - {error.get('message')}")
                continue

            busy_times_raw = calendar_data.get('busy', [])
            logging.debug(f"Kalendarz '{cal_name}': {len(busy_times_raw)} surowych zajętych.")
            for busy_slot in busy_times_raw:
                # Używamy parse_event_time, przekazując słownik jak z API
                busy_start = parse_event_time({'dateTime': busy_slot.get('start')}, tz)
                busy_end = parse_event_time({'dateTime': busy_slot.get('end')}, tz)

                if busy_start and busy_end and busy_start < busy_end:
                    # Ogranicz zajęty slot do naszego zakresu zapytania [start_datetime, end_datetime]
                    busy_start_clipped = max(busy_start, start_datetime)
                    busy_end_clipped = min(busy_end, end_datetime)

                    # Dodaj tylko jeśli po przycięciu nadal jest to prawidłowy zakres
                    if busy_start_clipped < busy_end_clipped:
                        busy_times_calendar.append({
                            'start': busy_start_clipped,
                            'end': busy_end_clipped,
                            'calendar_id': cal_id
                        })
                    # else: # Debug log for clipping result
                    #     logging.debug(f"  Zajęty slot {busy_start:%H:%M}-{busy_end:%H:%M} z '{cal_name}' po przycięciu do [{start_datetime:%H:%M}-{end_datetime:%H:%M}] stał się nieprawidłowy/pusty.")

                # else: # Debug log for parsing failure
                #     logging.debug(f"  Pominięto nieparsowalny/nieprawidłowy zajęty slot z '{cal_name}': {busy_slot}")

    except HttpError as error:
        # Logowanie błędu HTTP z API
        error_content = "Brak szczegółów"
        try:
            if error.resp and error.content:
                error_content = json.loads(error.content.decode('utf-8'))
        except Exception:
            pass # Ignoruj błędy dekodowania/parsowania treści błędu
        logging.error(f'Błąd HTTP {error.resp.status} API Freebusy: {error.resp.reason}. Szczegóły: {error_content}', exc_info=False) # Zmieniono exc_info na False dla zwięzłości
    except Exception as e:
        logging.error(f"Nieoczekiwany błąd podczas freeBusy: {e}", exc_info=True)

    logging.info(f"Pobrano {len(busy_times_calendar)} zajętych slotów z kalendarzy Google: {calendar_ids_to_check}.")
    return busy_times_calendar

def get_sheet_booked_slots(spreadsheet_id, sheet_name, start_datetime, end_datetime):
    """
    Pobiera zajęte sloty z arkusza Google (Arkusz1), włącznie z nazwą kalendarza.
    Zwraca listę słowników: {'start': dt_aware_cal_tz, 'end': dt_aware_cal_tz, 'calendar_name': str}.
    Daty zwracane są w strefie czasowej KALENDARZA.
    """
    service = get_sheets_service()
    sheet_busy_slots = []
    if not service:
        logging.error("Błąd: Usługa arkuszy niedostępna.")
        return sheet_busy_slots

    tz_sheet = _get_sheet_timezone()
    tz_cal = _get_calendar_timezone() # Potrzebne do konwersji na końcu

    # Konwersja granic do strefy czasowej KALENDARZA (bo dane z freeBusy są w tej strefie)
    if start_datetime.tzinfo is None:
        start_datetime_aware_cal = tz_cal.localize(start_datetime)
    else:
        start_datetime_aware_cal = start_datetime.astimezone(tz_cal)
    if end_datetime.tzinfo is None:
        end_datetime_aware_cal = tz_cal.localize(end_datetime)
    else:
        end_datetime_aware_cal = end_datetime.astimezone(tz_cal)

    try:
        # Używamy teraz poprawionego zakresu SHEET_READ_RANGE_FOR_BUSY_SLOTS
        read_range = SHEET_READ_RANGE_FOR_BUSY_SLOTS # np. Arkusz1!F2:K
        logging.debug(f"Odczyt arkusza '{sheet_name}' zakres '{read_range}' dla zajętych slotów.")
        result = service.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id, range=read_range
        ).execute()
        values = result.get('values', [])
        if not values:
            logging.debug(f"Arkusz '{sheet_name}' pusty/brak danych w zakresie F2:K.")
            return sheet_busy_slots

        duration_delta = datetime.timedelta(minutes=APPOINTMENT_DURATION_MINUTES)

        # Indeksy względem początku zakresu odczytu (F=0, G=1, ..., K=5)
        date_idx = SHEET_DATE_COLUMN_INDEX - SHEET_DATE_COLUMN_INDEX # Zawsze 0
        time_idx = SHEET_TIME_COLUMN_INDEX - SHEET_DATE_COLUMN_INDEX # G - F = 7 - 6 = 1
        cal_name_idx = SHEET_CALENDAR_NAME_COLUMN_INDEX - SHEET_DATE_COLUMN_INDEX # K - F = 11 - 6 = 5
        expected_row_length = cal_name_idx + 1 # Oczekujemy co najmniej 6 kolumn (F do K)

        for i, row in enumerate(values):
            row_num = i + 2 # Numer wiersza w arkuszu (zaczynamy odczyt od 2)
            if len(row) >= expected_row_length:
                date_str = row[date_idx].strip() if date_idx < len(row) else ""
                time_str = row[time_idx].strip() if time_idx < len(row) else ""
                calendar_name_str = row[cal_name_idx].strip() if cal_name_idx < len(row) else ""

                if not date_str or not time_str:
                    # logging.debug(f"Pominięto wiersz {row_num} z arkusza: brak daty lub czasu.")
                    continue
                # Nazwa kalendarza jest teraz kluczowa dla filtrowania per kalendarz
                if not calendar_name_str:
                    logging.warning(f"Wiersz {row_num} w arkuszu '{sheet_name}' nie ma nazwy kalendarza w kol. K. Pomijanie tego wpisu z arkusza.")
                    continue

                try:
                    # Parsuj jako datę i czas w strefie czasowej arkusza (SHEET_TIMEZONE)
                    naive_date = datetime.datetime.strptime(date_str, '%Y-%m-%d').date()
                    naive_time = datetime.datetime.strptime(time_str, '%H:%M').time()
                    naive_dt = datetime.datetime.combine(naive_date, naive_time)
                    # Ustaw strefę czasową arkusza
                    slot_start_sheet_tz = tz_sheet.localize(naive_dt)
                    # Przekonwertuj na strefę czasową kalendarza dla porównań
                    slot_start_cal_tz = slot_start_sheet_tz.astimezone(tz_cal)

                    # Porównuj daty w tej samej strefie (kalendarza)
                    if start_datetime_aware_cal <= slot_start_cal_tz < end_datetime_aware_cal:
                        slot_end_cal_tz = slot_start_cal_tz + duration_delta
                        sheet_busy_slots.append({
                            'start': slot_start_cal_tz, # Czas w strefie kalendarza
                            'end': slot_end_cal_tz,   # Czas w strefie kalendarza
                            'calendar_name': calendar_name_str
                        })
                        logging.debug(f"  Zajęty slot w arkuszu '{sheet_name}' (wiersz {row_num}): {slot_start_cal_tz:%Y-%m-%d %H:%M %Z} - {slot_end_cal_tz:%H:%M %Z} (Kalendarz: '{calendar_name_str}')")
                except ValueError:
                    logging.warning(f"  Pominięto wiersz {row_num} w arkuszu '{sheet_name}' (błąd parsowania daty/czasu): Data='{date_str}', Czas='{time_str}'")
                except pytz.exceptions.AmbiguousTimeError or pytz.exceptions.NonExistentTimeError:
                     logging.warning(f"  Pominięto wiersz {row_num} w arkuszu '{sheet_name}' (problem ze strefą czasową przy zmianie czasu): Data='{date_str}', Czas='{time_str}'")
                except Exception as parse_err:
                    logging.warning(f"  Pominięto wiersz {row_num} w arkuszu '{sheet_name}' (inny błąd): {parse_err} (Data='{date_str}', Czas='{time_str}')")
            else:
                logging.debug(f"Pominięto zbyt krótki wiersz {row_num} w arkuszu '{sheet_name}' (oczekiwano {expected_row_length} kolumn od F): {row}")

    except HttpError as error:
        logging.error(f"Błąd HTTP API odczytu arkusza '{sheet_name}': {error.resp.status} {error.resp.reason}", exc_info=True)
    except Exception as e:
        logging.error(f"Nieoczekiwany błąd odczytu arkusza '{sheet_name}': {e}", exc_info=True)

    logging.info(f"Znaleziono {len(sheet_busy_slots)} potencjalnie zajętych slotów w arkuszu '{sheet_name}' (w zakresie F:K) z przypisanymi kalendarzami.")
    return sheet_busy_slots


def get_free_time_ranges(calendar_config_list, start_datetime, end_datetime):
    """
    Pobiera listę wolnych zakresów czasowych, które są dostępne w CO NAJMNIEJ JEDNYM
    kalendarzu z podanej listy (`calendar_config_list`) PO odfiltrowaniu przez przypisane do niego
    rezerwacje z arkusza (Arkusz1).

    Args:
        calendar_config_list: Lista słowników konfiguracji kalendarzy do sprawdzenia
                              (każdy słownik powinien zawierać 'id' i 'name').
        start_datetime: Początek okresu wyszukiwania (może być naive lub aware).
        end_datetime: Koniec okresu wyszukiwania (może być naive lub aware).

    Returns:
        Lista słowników {'start': dt_aware, 'end': dt_aware} reprezentujących
        wolne zakresy czasowe (w strefie CALENDAR_TIMEZONE).
    """
    service_cal = get_calendar_service()
    tz = _get_calendar_timezone()
    if not service_cal:
        logging.error("Błąd: Usługa kalendarza niedostępna dla get_free_time_ranges.")
        return []
    if not calendar_config_list:
        logging.warning("Brak kalendarzy do sprawdzenia w get_free_time_ranges.")
        return []

    # Upewnij się, że daty graniczne są świadome strefy czasowej KALENDARZA
    if start_datetime.tzinfo is None:
        start_datetime = tz.localize(start_datetime)
    else:
        start_datetime = start_datetime.astimezone(tz)
    if end_datetime.tzinfo is None:
        end_datetime = tz.localize(end_datetime)
    else:
        end_datetime = end_datetime.astimezone(tz)

    now = datetime.datetime.now(tz)
    # Ustal efektywny początek wyszukiwania (nie wcześniej niż teraz)
    search_start_unfiltered = max(start_datetime, now)

    if search_start_unfiltered >= end_datetime:
        logging.info(f"Zakres [{search_start_unfiltered:%Y-%m-%d %H:%M %Z} - {end_datetime:%Y-%m-%d %H:%M %Z}] jest nieprawidłowy lub całkowicie w przeszłości.")
        return []

    calendar_names = [c.get('name', c.get('id', 'Nieznany')) for c in calendar_config_list]
    logging.info(f"Szukanie wolnych zakresów (Logika OR, Filtr Arkusza Per Kalendarz) w kalendarzach: {calendar_names} od {search_start_unfiltered:%Y-%m-%d %H:%M %Z} do {end_datetime:%Y-%m-%d %H:%M %Z}")

    min_duration_delta = datetime.timedelta(minutes=APPOINTMENT_DURATION_MINUTES)

    # --- Krok 1: Pobierz WSZYSTKIE zajęte sloty z Arkusza1 w danym zakresie ---
    # Pobieramy raz, aby uniknąć wielokrotnych zapytań do API Sheets
    all_sheet_bookings = get_sheet_booked_slots(SPREADSHEET_ID, MAIN_SHEET_NAME, search_start_unfiltered, end_datetime)
    all_sheet_bookings.sort(key=lambda x: x['start'])
    logging.debug(f"--- Zajęte sloty z Arkusza '{MAIN_SHEET_NAME}' (łącznie {len(all_sheet_bookings)} w zakresie) ---")
    # (Opcjonalne logowanie pierwszych slotów z arkusza)
    # if all_sheet_bookings:
    #     for i, s in enumerate(all_sheet_bookings[:5]):
    #         logging.debug(f"  Arkusz Slot {i+1}: {s['start']:%H:%M}-{s['end']:%H:%M} (Kal: '{s.get('calendar_name', 'Brak')}')")
    #     if len(all_sheet_bookings) > 5: logging.debug("  ...")

    # --- Krok 2: Dla każdego kalendarza z listy wejściowej, oblicz jego wolne sloty po filtracji ---
    all_individually_filtered_free_ranges = []
    calendar_ids_to_check_gcal = [c['id'] for c in calendar_config_list if 'id' in c]

    # Pobierz zajęte sloty z Google Calendar dla WSZYSTKICH kalendarzy z listy za jednym razem
    busy_times_gcal_all = get_calendar_busy_slots(calendar_ids_to_check_gcal, search_start_unfiltered, end_datetime)
    busy_times_gcal_by_id = defaultdict(list)
    for busy_slot in busy_times_gcal_all:
        busy_times_gcal_by_id[busy_slot['calendar_id']].append(busy_slot)

    for cal_config in calendar_config_list:
        cal_id = cal_config.get('id')
        cal_name = cal_config.get('name', cal_id or 'Nieznany')
        if not cal_id:
            logging.warning(f"Pominięto konfigurację kalendarza bez ID: {cal_config}")
            continue

        logging.debug(f"--- Przetwarzanie kalendarza: '{cal_name}' ({cal_id}) ---")

        # 2a: Użyj już pobranych zajętych z Google Calendar dla TEGO kalendarza
        busy_times_cal = sorted(busy_times_gcal_by_id.get(cal_id, []), key=lambda x: x['start'])

        # Scal nakładające się lub stykające zajęte sloty TYLKO z Google Calendar dla tego kalendarza
        merged_busy_cal = []
        for busy in busy_times_cal:
            if not merged_busy_cal or busy['start'] > merged_busy_cal[-1]['end']:
                 # Dodaj nowy, jeśli lista jest pusta lub jest przerwa
                 merged_busy_cal.append(busy.copy())
            else:
                 # Scal, jeśli się stykają lub nachodzą
                 merged_busy_cal[-1]['end'] = max(merged_busy_cal[-1]['end'], busy['end'])


        # 2b: Oblicz "surowe" wolne dla TEGO kalendarza (na podstawie jego zajętości w GCal)
        raw_calendar_free_ranges = []
        current_time = search_start_unfiltered # Zacznij od początku efektywnego zakresu
        for busy_slot in merged_busy_cal:
            if current_time < busy_slot['start']:
                # Jest przerwa między obecnym czasem a początkiem zajętości -> to jest wolny zakres
                raw_calendar_free_ranges.append({'start': current_time, 'end': busy_slot['start']})
            # Przesuń obecny czas na koniec bieżącego zajętego slotu (lub dalej, jeśli już tam był)
            current_time = max(current_time, busy_slot['end'])

        # Sprawdź, czy jest wolny zakres od końca ostatniego zajętego slotu do końca zakresu wyszukiwania
        if current_time < end_datetime:
            raw_calendar_free_ranges.append({'start': current_time, 'end': end_datetime})


        # Zastosuj filtr godzin pracy do "surowych" wolnych zakresów
        raw_calendar_free_ranges_workhours = []
        work_start_time = datetime.time(WORK_START_HOUR, 0)
        work_end_time = datetime.time(WORK_END_HOUR, 0)

        for free_range in raw_calendar_free_ranges:
            range_start = free_range['start']
            range_end = free_range['end']

            # Iteruj przez dni w zakresie wolnego czasu
            current_day_start = range_start
            while current_day_start < range_end:
                day_date = current_day_start.date()
                # Początek i koniec dnia pracy w strefie czasowej kalendarza
                work_day_start_dt = tz.localize(datetime.datetime.combine(day_date, work_start_time))
                work_day_end_dt = tz.localize(datetime.datetime.combine(day_date, work_end_time))

                # Znajdź część wspólną [range_start, range_end) oraz [work_day_start_dt, work_day_end_dt)
                effective_start = max(current_day_start, work_day_start_dt)
                effective_end = min(range_end, work_day_end_dt)

                # Dodaj tylko jeśli jest to prawidłowy zakres i wystarczająco długi
                if effective_start < effective_end and (effective_end - effective_start) >= min_duration_delta:
                    raw_calendar_free_ranges_workhours.append({'start': effective_start, 'end': effective_end})

                # Przejdź do następnego dnia LUB do końca obecnego zakresu, jeśli był krótszy niż dzień pracy
                # Ważne: Następny punkt startowy to albo koniec pracy tego dnia, albo początek następnego dnia
                next_day_start_dt = tz.localize(datetime.datetime.combine(day_date + datetime.timedelta(days=1), datetime.time.min))
                # Przesuń wskaźnik na początek następnego dnia lub na koniec obecnego przedziału pracy, cokolwiek jest pierwsze po effective_end
                current_day_start = min(range_end, max(effective_end, next_day_start_dt))
                # Upewnij się, że nie cofamy się przed początek oryginalnego zakresu free_range
                current_day_start = max(current_day_start, range_start)


        logging.debug(f"    Surowe wolne dla '{cal_name}' (po filtrze GCal i godzin pracy): {len(raw_calendar_free_ranges_workhours)}")
        # (Opcjonalne logowanie surowych slotów)
        # if raw_calendar_free_ranges_workhours:
        #     for i, s in enumerate(raw_calendar_free_ranges_workhours[:3]): logging.debug(f"      Surowy Slot {i+1}: {s['start']:%H:%M}-{s['end']:%H:%M}")
        #     if len(raw_calendar_free_ranges_workhours) > 3: logging.debug("      ...")


        # 2c: Odfiltruj surowe wolne używając TYLKO rezerwacji z arkusza przypisanych do TEGO kalendarza
        # Porównujemy nazwy bez uwzględniania wielkości liter i białych znaków
        cal_name_normalized = cal_name.strip().lower()
        sheet_bookings_for_this_cal = [
            b for b in all_sheet_bookings
            if b.get('calendar_name', '').strip().lower() == cal_name_normalized
        ]
        logging.debug(f"    Znaleziono {len(sheet_bookings_for_this_cal)} pasujących rezerwacji w arkuszu '{MAIN_SHEET_NAME}' dla '{cal_name_normalized}'.")

        # Lista kandydatów na wolne sloty = początkowo wszystkie wolne po filtrze GCal i godzin pracy
        candidate_ranges = raw_calendar_free_ranges_workhours
        if sheet_bookings_for_this_cal:
            logging.debug(f"    Filtrowanie wg {len(sheet_bookings_for_this_cal)} rezerwacji z arkusza...")
            # Iterujemy przez KAŻDĄ rezerwację z arkusza dla tego kalendarza
            for sheet_busy in sheet_bookings_for_this_cal:
                # logging.debug(f"      Filtr arkusza: {sheet_busy['start']:%H:%M}-{sheet_busy['end']:%H:%M}")
                next_candidate_ranges = [] # Nowa lista po zastosowaniu bieżącego filtra arkusza
                # Iterujemy przez aktualną listę kandydatów na wolne sloty
                for calendar_free in candidate_ranges:
                    # Sprawdź nakładanie się: [cal_start, cal_end) vs [sheet_start, sheet_end)
                    overlap_start = max(calendar_free['start'], sheet_busy['start'])
                    overlap_end = min(calendar_free['end'], sheet_busy['end'])

                    if overlap_start < overlap_end: # Jest nakładanie
                        # logging.debug(f"        Nakładanie z {calendar_free['start']:%H:%M}-{calendar_free['end']:%H:%M}")
                        # Podziel wolny zakres na części przed i po zajętym slocie z arkusza
                        # Część przed: [cal_start, sheet_start)
                        if calendar_free['start'] < sheet_busy['start'] and (sheet_busy['start'] - calendar_free['start']) >= min_duration_delta:
                            next_candidate_ranges.append({'start': calendar_free['start'], 'end': sheet_busy['start']})
                            # logging.debug(f"          -> Zachowano PRZED: {calendar_free['start']:%H:%M}-{sheet_busy['start']:%H:%M}")
                        # Część po: [sheet_end, cal_end)
                        if calendar_free['end'] > sheet_busy['end'] and (calendar_free['end'] - sheet_busy['end']) >= min_duration_delta:
                            next_candidate_ranges.append({'start': sheet_busy['end'], 'end': calendar_free['end']})
                            # logging.debug(f"          -> Zachowano PO: {sheet_busy['end']:%H:%M}-{calendar_free['end']:%H:%M}")
                    else: # Brak nakładania
                        # Zachowaj ten wolny zakres bez zmian
                        next_candidate_ranges.append(calendar_free)
                # Zaktualizuj listę kandydatów na wolne sloty przed kolejnym filtrem z arkusza
                candidate_ranges = sorted(next_candidate_ranges, key=lambda x: x['start'])

            filtered_calendar_free_ranges = candidate_ranges
            logging.debug(f"    Sloty dla '{cal_name}' PO filtracji arkuszem: {len(filtered_calendar_free_ranges)}")
        else: # Brak rezerwacji w arkuszu dla tego kalendarza
            filtered_calendar_free_ranges = raw_calendar_free_ranges_workhours
            logging.debug(f"    Brak rezerwacji w arkuszu '{MAIN_SHEET_NAME}' do filtrowania dla '{cal_name}'. Używam slotów po filtrze GCal/godzin.")

        # Dodaj przefiltrowane wolne zakresy dla tego kalendarza do łącznej listy
        all_individually_filtered_free_ranges.extend(filtered_calendar_free_ranges)
        # (Opcjonalne logowanie wynikowych slotów dla tego kalendarza)
        # if filtered_calendar_free_ranges:
        #     for i, s in enumerate(filtered_calendar_free_ranges[:3]): logging.debug(f"      Wynikowy slot dla {cal_name} {i+1}: {s['start']:%H:%M}-{s['end']:%H:%M}")
        #     if len(filtered_calendar_free_ranges) > 3: logging.debug("      ...")


    # --- Krok 3: Połącz wszystkie indywidualnie przefiltrowane wolne zakresy ---
    if not all_individually_filtered_free_ranges:
        logging.info("Brak wolnych zakresów w żadnym z wybranych kalendarzy po indywidualnej filtracji.")
        return []
    # Sortuj wszystkie znalezione wolne zakresy (z różnych kalendarzy) po czasie rozpoczęcia
    sorted_filtered_free = sorted(all_individually_filtered_free_ranges, key=lambda x: x['start'])
    logging.debug(f"--- Łączenie {len(sorted_filtered_free)} indywidualnie przefiltrowanych wolnych slotów (Logika 'OR') ---")

    # --- Krok 4: Scal połączone zakresy (Logika 'OR') ---
    # Scal nakładające się lub stykające wolne zakresy z RÓŻNYCH kalendarzy
    merged_all_free_ranges = []
    if sorted_filtered_free:
        # Zacznij od pierwszego zakresu
        current_merged_slot = sorted_filtered_free[0].copy()

        for next_slot in sorted_filtered_free[1:]:
            # Jeśli następny slot zaczyna się przed końcem bieżącego scalonego slotu (lub dokładnie na końcu)
            if next_slot['start'] <= current_merged_slot['end']:
                # Rozszerz koniec bieżącego scalonego slotu, jeśli następny kończy się później
                current_merged_slot['end'] = max(current_merged_slot['end'], next_slot['end'])
            else:
                # Jest przerwa, zakończ bieżący scalony slot i zacznij nowy
                # Dodaj tylko jeśli jest wystarczająco długi
                if (current_merged_slot['end'] - current_merged_slot['start']) >= min_duration_delta:
                     merged_all_free_ranges.append(current_merged_slot)
                current_merged_slot = next_slot.copy()

        # Dodaj ostatni scalony slot (jeśli jest wystarczająco długi)
        if (current_merged_slot['end'] - current_merged_slot['start']) >= min_duration_delta:
            merged_all_free_ranges.append(current_merged_slot)

    logging.debug(f"--- Scalone wolne zakresy ('OR') PRZED filtrem wyprzedzenia ({len(merged_all_free_ranges)}) ---")
    # (Opcjonalne logowanie scalonych slotów)
    # if merged_all_free_ranges:
    #     for i, s in enumerate(merged_all_free_ranges[:5]): logging.debug(f"  Scalony Slot {i+1}: {s['start']:%H:%M}-{s['end']:%H:%M}")
    #     if len(merged_all_free_ranges) > 5: logging.debug("  ...")

    # --- Krok 5: Zastosuj filtr MIN_BOOKING_LEAD_HOURS ---
    final_filtered_slots = []
    min_start_time = now + datetime.timedelta(hours=MIN_BOOKING_LEAD_HOURS)
    logging.debug(f"Minimalny czas startu (filtr {MIN_BOOKING_LEAD_HOURS}h): {min_start_time:%Y-%m-%d %H:%M %Z}")

    for slot in merged_all_free_ranges:
        # Efektywny początek to późniejszy z: początku slotu LUB minimalnego czasu rezerwacji
        effective_start = max(slot['start'], min_start_time)
        # Koniec slotu pozostaje bez zmian
        effective_end = slot['end']

        # Dodaj slot tylko jeśli po zastosowaniu filtra wyprzedzenia nadal jest poprawny i wystarczająco długi
        if effective_start < effective_end and (effective_end - effective_start) >= min_duration_delta:
            final_filtered_slots.append({'start': effective_start, 'end': effective_end})
            # if effective_start != slot['start']: # Loguj jeśli filtr wyprzedzenia zmienił start
            #     logging.debug(f"  Zmodyfikowano start slotu {slot['start']:%H:%M}-{slot['end']:%H:%M} na {effective_start:%H:%M} (filtr {MIN_BOOKING_LEAD_HOURS}h)")

    logging.info(f"Znaleziono {len(final_filtered_slots)} ostatecznych wolnych zakresów (Logika 'OR', Filtr Arkusza Per Kalendarz, po wszystkich filtrach).")
    # (Opcjonalne logowanie finalnych slotów)
    # if final_filtered_slots:
    #     for i, slot in enumerate(final_filtered_slots[:5]): logging.debug(f"  Finalny Slot {i+1}: {slot['start']:%Y-%m-%d %H:%M} - {slot['end']:%Y-%m-%d %H:%M}")
    #     if len(final_filtered_slots) > 5: logging.debug("  ...")

    return final_filtered_slots


def is_slot_actually_free(start_time, calendar_id):
    """
    Weryfikuje w czasie rzeczywistym, czy DOKŁADNY slot czasowy jest wolny
    w danym Kalendarzu Google, sprawdzając free/busy.
    """
    service = get_calendar_service()
    tz = _get_calendar_timezone()
    if not service:
        logging.error(f"Błąd: Usługa kalendarza niedostępna (weryfikacja {calendar_id}).")
        return False # Nie można zweryfikować -> załóż, że zajęty

    if not isinstance(start_time, datetime.datetime):
        logging.error(f"Błąd weryfikacji {calendar_id}: start_time nie jest datetime (typ: {type(start_time)})")
        return False # Błąd danych -> załóż, że zajęty

    # Upewnij się, że czas jest świadomy strefy czasowej kalendarza
    if start_time.tzinfo is None:
        start_time = tz.localize(start_time)
    else:
        start_time = start_time.astimezone(tz)

    # Koniec slotu do sprawdzenia
    end_time = start_time + datetime.timedelta(minutes=APPOINTMENT_DURATION_MINUTES)

    # Zapytanie free/busy musi być minimalnie węższe niż slot, aby uniknąć problemów
    # z wydarzeniami zaczynającymi/kończącymi się DOKŁADNIE na granicy slotu.
    # Sprawdzamy okres (start_time + 1 sekunda, end_time - 1 sekunda)
    query_start_time = start_time + datetime.timedelta(seconds=1)
    query_end_time = end_time - datetime.timedelta(seconds=1)

    # Jeśli slot jest krótszy niż 2 sekundy (mało prawdopodobne), pomiń weryfikację
    if query_start_time >= query_end_time:
        logging.warning(f"Weryfikacja {calendar_id}: Slot {start_time:%H:%M}-{end_time:%H:%M} za krótki do precyzyjnej weryfikacji free/busy.")
        # W takim przypadku możemy polegać na wcześniejszym wyniku `get_free_time_ranges`
        # lub zachowawczo zwrócić False. Wybieramy True, zakładając, że `get_free_time_ranges` był dokładny.
        return True # Załóżmy, że jest OK, jeśli był w liście wolnych

    body = {
        "timeMin": query_start_time.isoformat(),
        "timeMax": query_end_time.isoformat(),
        "timeZone": CALENDAR_TIMEZONE,
        "items": [{"id": calendar_id}]
    }
    try:
        # Użyj ALL_CALENDAR_ID_TO_NAME do pobrania nazwy
        cal_name = ALL_CALENDAR_ID_TO_NAME.get(calendar_id, calendar_id)
        logging.debug(f"Weryfikacja free/busy dla '{cal_name}': {start_time:%Y-%m-%d %H:%M} - {end_time:%Y-%m-%d %H:%M} (Zapytanie: {query_start_time:%H:%M:%S} - {query_end_time:%H:%M:%S})")
        freebusy_result = service.freebusy().query(body=body).execute()
        calendar_data = freebusy_result.get('calendars', {}).get(calendar_id, {})

        if 'errors' in calendar_data:
            for error in calendar_data['errors']:
                logging.error(f"Błąd API Freebusy (weryfikacja) dla '{cal_name}': {error.get('reason')} - {error.get('message')}")
            return False # Błąd API -> załóż, że zajęty

        busy_times = calendar_data.get('busy', [])
        if not busy_times:
            # Brak zajętych slotów w WĄSKIM zakresie zapytania -> slot jest wolny
            logging.info(f"Weryfikacja '{cal_name}': Slot {start_time:%Y-%m-%d %H:%M} POTWIERDZONY jako wolny (brak busy w zakresie).")
            return True
        else:
            # Jeśli API zwróciło JAKIKOLWIEK zajęty slot w tym wąskim zakresie, oznacza to, że
            # nasz proponowany slot JEST ZAJĘTY. Nie musimy nawet sprawdzać dokładnych czasów.
            logging.warning(f"Weryfikacja '{cal_name}': Slot {start_time:%Y-%m-%d %H:%M} jest ZAJĘTY (API zwróciło busy w zakresie: {busy_times}).")
            return False

    except HttpError as error:
        error_content = "Brak szczegółów"
        try:
            if error.resp and error.content:
                error_content = json.loads(error.content.decode('utf-8'))
        except Exception: pass
        logging.error(f"Błąd HTTP {error.resp.status} API Freebusy (weryfikacja) dla '{calendar_id}': {error.resp.reason}. Szczegóły: {error_content}", exc_info=False)
        return False # Błąd HTTP -> załóż, że zajęty
    except Exception as e:
        logging.error(f"Nieoczekiwany błąd weryfikacji Freebusy dla '{calendar_id}': {e}", exc_info=True)
        return False # Inny błąd -> załóż, że zajęty


def format_ranges_for_ai(ranges, subject=None):
    """
    Formatuje listę zakresów czasowych dla AI, opcjonalnie dodając kontekst przedmiotu.
    """
    if not ranges:
        subject_info = f" dla przedmiotu {subject}" if subject else ""
        return f"Brak dostępnych terminów{subject_info} w podanym okresie."

    tz = _get_calendar_timezone()
    formatted_lines = []
    if subject:
         formatted_lines.append(f"Dostępne ZAKRESY dla przedmiotu **{subject}** (wizyta trwa {APPOINTMENT_DURATION_MINUTES} minut):")
    else:
         formatted_lines.append(f"Dostępne ZAKRESY (wizyta trwa {APPOINTMENT_DURATION_MINUTES} minut):")

    formatted_lines.append("--- Dostępne Zakresy (Data YYYY-MM-DD, Dzień, Od HH:MM, Do HH:MM) ---")

    slots_added = 0
    max_slots_to_show = 15 # Ograniczenie liczby pokazywanych zakresów AI
    sorted_ranges = sorted(ranges, key=lambda r: r['start'])
    min_duration = datetime.timedelta(minutes=APPOINTMENT_DURATION_MINUTES)

    for r in sorted_ranges:
        # Upewnij się, że zakres jest wystarczająco długi
        if (r['end'] - r['start']) >= min_duration:
            start_dt = r['start'].astimezone(tz)
            end_dt = r['end'].astimezone(tz)
            try:
                # Użyj locale jeśli ustawione, inaczej fallback
                day_name = start_dt.strftime('%A').capitalize()
            except Exception:
                day_name = POLISH_WEEKDAYS[start_dt.weekday()] # Fallback

            date_str = start_dt.strftime('%Y-%m-%d')
            start_time_str = start_dt.strftime('%H:%M')
            end_time_str = end_dt.strftime('%H:%M')
            formatted_lines.append(f"- {date_str}, {day_name}, od {start_time_str}, do {end_time_str}")
            slots_added += 1
            if slots_added >= max_slots_to_show:
                formatted_lines.append("- ... (i potencjalnie więcej)")
                break
        # else: # Opcjonalnie loguj pominięte za krótkie zakresy
        #     logging.debug(f"Pominięto za krótki zakres dla AI: {r['start']:%H:%M}-{r['end']:%H:%M}")


    if slots_added == 0:
        subject_info = f" dla przedmiotu {subject}" if subject else ""
        return f"Brak dostępnych terminów{subject_info} (mieszczących wizytę {APPOINTMENT_DURATION_MINUTES} min) w podanym okresie."

    formatted_output = "\n".join(formatted_lines)
    logging.debug(f"--- Zakresy sformatowane dla AI ({slots_added} pokazanych, Przedmiot: {subject or 'brak'}) ---\n{formatted_output}\n---------------------------------")
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
    """Szuka wiersza w arkuszu Arkusz1 na podstawie PSID."""
    service = get_sheets_service()
    if not service:
        logging.error("Błąd: Usługa arkuszy niedostępna (szukanie PSID).")
        return None
    try:
        read_range = SHEET_READ_RANGE_FOR_PSID_SEARCH # Np. Arkusz1!A2:A
        logging.debug(f"Szukanie PSID {psid} w '{MAIN_SHEET_NAME}' zakres '{read_range}'")
        result = service.spreadsheets().values().get(
            spreadsheetId=SPREADSHEET_ID, range=read_range
        ).execute()
        values = result.get('values', [])
        if not values:
            logging.debug(f"Arkusz '{MAIN_SHEET_NAME}' pusty lub brak PSID w zakresie A2:A.")
            return None
        for i, row in enumerate(values):
            # Sprawdź czy wiersz nie jest pusty i czy pierwszy element (PSID) pasuje
            if row and len(row) > 0 and row[0].strip() == psid:
                row_number = i + 2 # +2 bo zakres zaczyna się od A2, a indeksy są 0-based
                logging.info(f"Znaleziono PSID {psid} w wierszu {row_number} arkusza '{MAIN_SHEET_NAME}'.")
                return row_number
        logging.info(f"Nie znaleziono PSID {psid} w arkuszu '{MAIN_SHEET_NAME}' (zakres {read_range}).")
        return None
    except HttpError as error:
        error_content = "Brak szczegółów"
        try:
            if error.resp and error.content:
                error_content = json.loads(error.content.decode('utf-8'))
        except Exception: pass
        logging.error(f"Błąd HTTP {error.resp.status} API szukania PSID w '{MAIN_SHEET_NAME}': {error.resp.reason}. Szczegóły: {error_content}", exc_info=False)
        return None
    except Exception as e:
        logging.error(f"Nieoczekiwany błąd szukania PSID w '{MAIN_SHEET_NAME}': {e}", exc_info=True)
        return None


def write_to_sheet_phase1(psid, start_time, calendar_name):
    """Zapisuje dane Fazy 1 (PSID, Data, Czas, Nazwa Kalendarza) do Arkusz1 (APPEND)."""
    service = get_sheets_service()
    if not service:
        return False, "Błąd połączenia z Google Sheets (Faza 1 - Append)."

    tz_sheet = _get_sheet_timezone() # Użyj strefy czasowej arkusza do zapisu
    # Upewnij się, że start_time jest świadome i w strefie arkusza
    if start_time.tzinfo is None:
        # Jeśli naive, załóż, że jest w strefie kalendarza i przekonwertuj do strefy arkusza
        tz_cal = _get_calendar_timezone()
        try:
            start_time_aware = tz_cal.localize(start_time).astimezone(tz_sheet)
        except (pytz.exceptions.AmbiguousTimeError, pytz.exceptions.NonExistentTimeError):
             logging.error(f"Błąd konwersji czasu Fazy 1 (zmiana czasu?) dla {start_time}. Używam UTC offsetu.")
             # Fallback: użyj offsetu UTC jeśli lokalizacja zawiedzie
             start_time_aware = start_time.astimezone(tz_sheet) # Próba konwersji z istniejącego (może być UTC)
    else:
        # Jeśli aware, po prostu przekonwertuj do strefy arkusza
        start_time_aware = start_time.astimezone(tz_sheet)

    date_str = start_time_aware.strftime('%Y-%m-%d')
    time_str = start_time_aware.strftime('%H:%M')

    # Przygotuj wiersz z odpowiednią liczbą pustych komórek
    # Znajdź maksymalny indeks kolumny używany w zapisie
    max_col_index = max(SHEET_PSID_COLUMN_INDEX, SHEET_DATE_COLUMN_INDEX, SHEET_TIME_COLUMN_INDEX, SHEET_CALENDAR_NAME_COLUMN_INDEX)
    data_row = [""] * max_col_index # Utwórz listę pustych stringów o odpowiedniej długości

    # Wypełnij dane w odpowiednich indeksach (0-based)
    data_row[SHEET_PSID_COLUMN_INDEX - 1] = psid
    data_row[SHEET_DATE_COLUMN_INDEX - 1] = date_str
    data_row[SHEET_TIME_COLUMN_INDEX - 1] = time_str
    data_row[SHEET_CALENDAR_NAME_COLUMN_INDEX - 1] = calendar_name # Zapisz nazwę kalendarza

    try:
        # Zakres A1 jest tylko po to, by append wiedziało, do którego arkusza dodać
        # Dane zostaną dodane w pierwszym wolnym wierszu
        range_name = f"{MAIN_SHEET_NAME}!A1"
        body = {'values': [data_row]}
        logging.info(f"Próba zapisu Fazy 1 (Append) do '{MAIN_SHEET_NAME}': PSID={psid}, Data={date_str}, Czas={time_str}, Kalendarz='{calendar_name}'")

        result = service.spreadsheets().values().append(
            spreadsheetId=SPREADSHEET_ID,
            range=range_name, # Gdzie zacząć szukać miejsca do dodania
            valueInputOption='USER_ENTERED', # Interpretuj dane jakby wpisał je użytkownik
            insertDataOption='INSERT_ROWS', # Wstaw jako nowy wiersz
            body=body
        ).execute()

        updated_range = result.get('updates', {}).get('updatedRange', '')
        logging.info(f"Zapisano Faza 1 (Append) pomyślnie. Zaktualizowany zakres: {updated_range}")

        # Spróbuj wyodrębnić numer wiersza z 'updatedRange' (np. "Arkusz1!A10:K10")
        match = re.search(rf"{re.escape(MAIN_SHEET_NAME)}!A(\d+):", updated_range)
        row_index = int(match.group(1)) if match else None

        if row_index:
            logging.info(f"Wyodrębniono numer wiersza zapisu Fazy 1: {row_index}")
            # Loguj statystykę rezerwacji PO udanym zapisie Fazy 1
            log_statistic("booking")
            return True, row_index # Zwróć sukces i numer wiersza
        else:
            logging.warning(f"Nie udało się wyodrębnić numeru wiersza z odpowiedzi API append: {updated_range}. Zwracam sukces bez numeru wiersza.")
            # Loguj statystykę rezerwacji PO udanym zapisie Fazy 1 (nawet bez numeru wiersza)
            log_statistic("booking")
            # Zwracamy sukces, ale bez numeru wiersza - Faza 2 będzie musiała go znaleźć po PSID.
            return True, None

    except HttpError as error:
        error_content = "Brak szczegółów"
        try:
            if error.resp and error.content:
                error_content = json.loads(error.content.decode('utf-8'))
        except Exception: pass
        error_details = f"{error.resp.status} {error.resp.reason}"
        logging.error(f"Błąd API Fazy 1 (Append) do '{MAIN_SHEET_NAME}': {error_details}. Szczegóły: {error_content}", exc_info=False)
        # Zwróć bardziej szczegółowy błąd, jeśli to możliwe
        api_message = error_content.get('error', {}).get('message', error_details) if isinstance(error_content, dict) else error_details
        return False, f"Błąd zapisu Fazy 1 ({api_message})."
    except Exception as e:
        logging.error(f"Błąd Python Fazy 1 (Append) do '{MAIN_SHEET_NAME}': {e}", exc_info=True)
        return False, "Wewnętrzny błąd systemu podczas zapisu Fazy 1."

def update_sheet_phase2(student_data, sheet_row_index):
    """Aktualizuje wiersz w Arkusz1 danymi Fazy 2 (używając tylko numeru klasy dla kol. H)."""
    service = get_sheets_service()
    if not service:
        return False, "Błąd połączenia z Google Sheets (Faza 2)."
    if sheet_row_index is None or not isinstance(sheet_row_index, int) or sheet_row_index < 2:
        logging.error(f"Nieprawidłowy indeks wiersza ({sheet_row_index}) do aktualizacji Fazy 2 w '{MAIN_SHEET_NAME}'.")
        return False, "Brak/nieprawidłowy numer wiersza do aktualizacji."
    try:
        # Pobierz dane z wejściowego słownika, zapewniając domyślne wartości ''
        parent_fn = student_data.get('parent_first_name', '')
        parent_ln = student_data.get('parent_last_name', '')
        student_fn = student_data.get('student_first_name', '')
        student_ln = student_data.get('student_last_name', '')
        grade_info = student_data.get('grade_info', '') # Pełna informacja, np. "3 klasa liceum"
        level_info = student_data.get('level_info', '') # Np. "Podstawowy", "Rozszerzony", "Brak"

        # Wyodrębnij numer klasy, pełny opis i typ szkoły
        numerical_grade, _, school_type = extract_school_type(grade_info) # Ignorujemy drugi element (class_desc)

        logging.info(f"Przygotowanie danych do Fazy 2 (Arkusz1, wiersz {sheet_row_index}): Rodzic='{parent_fn} {parent_ln}', Uczeń='{student_fn} {student_ln}', NrKlasy(H)='{numerical_grade}', TypSzkoły(I)='{school_type}', Poziom(J)='{level_info}'")

        # Przygotuj listę wartości do zaktualizowania dla każdej grupy kolumn
        # Grupa 1: Dane rodzica i ucznia (Kolumny B, C, D, E)
        update_data_group1 = [parent_fn, parent_ln, student_fn, student_ln]
        # Grupa 2: Dane o edukacji (Kolumny H, I, J)
        update_data_group2 = [numerical_grade, school_type, level_info]

        # Zdefiniuj zakresy aktualizacji
        start_col_g1 = chr(ord('A') + SHEET_PARENT_FN_COLUMN_INDEX - 1) # B
        end_col_g1 = chr(ord('A') + SHEET_STUDENT_LN_COLUMN_INDEX - 1)   # E
        range_group1 = f"{MAIN_SHEET_NAME}!{start_col_g1}{sheet_row_index}:{end_col_g1}{sheet_row_index}"

        start_col_g2 = chr(ord('A') + SHEET_GRADE_COLUMN_INDEX - 1)     # H
        end_col_g2 = chr(ord('A') + SHEET_LEVEL_COLUMN_INDEX - 1)       # J
        range_group2 = f"{MAIN_SHEET_NAME}!{start_col_g2}{sheet_row_index}:{end_col_g2}{sheet_row_index}"

        # Przygotuj ciała zapytań
        body1 = {'values': [update_data_group1]}
        body2 = {'values': [update_data_group2]}

        # Wykonaj aktualizację grupy 1
        logging.info(f"Aktualizacja Fazy 2 (Grupa 1) wiersz {sheet_row_index} arkusz '{MAIN_SHEET_NAME}' zakres {range_group1} danymi: {update_data_group1}")
        result1 = service.spreadsheets().values().update(
            spreadsheetId=SPREADSHEET_ID,
            range=range_group1,
            valueInputOption='USER_ENTERED',
            body=body1
        ).execute()
        logging.info(f"Zaktualizowano Faza 2 (Grupa 1): {result1.get('updatedCells')} komórek.")

        # Wykonaj aktualizację grupy 2
        logging.info(f"Aktualizacja Fazy 2 (Grupa 2) wiersz {sheet_row_index} arkusz '{MAIN_SHEET_NAME}' zakres {range_group2} danymi: {update_data_group2}")
        result2 = service.spreadsheets().values().update(
            spreadsheetId=SPREADSHEET_ID,
            range=range_group2,
            valueInputOption='USER_ENTERED',
            body=body2
        ).execute()
        logging.info(f"Zaktualizowano Faza 2 (Grupa 2): {result2.get('updatedCells')} komórek.")

        return True, None # Sukces

    except HttpError as error:
        error_content = "Brak szczegółów"
        try:
            if error.resp and error.content:
                error_content = json.loads(error.content.decode('utf-8'))
        except Exception: pass
        error_details = f"{error.resp.status} {error.resp.reason}"
        logging.error(f"Błąd API Fazy 2 (Update) w '{MAIN_SHEET_NAME}': {error_details}. Szczegóły: {error_content}", exc_info=False)
        api_message = error_content.get('error', {}).get('message', error_details) if isinstance(error_content, dict) else error_details
        return False, f"Błąd aktualizacji Fazy 2 ({api_message})."
    except Exception as e:
        logging.error(f"Błąd Python Fazy 2 (Update) w '{MAIN_SHEET_NAME}': {e}", exc_info=True)
        return False, "Wewnętrzny błąd systemu podczas aktualizacji Fazy 2."

# --- NOWE FUNKCJE DLA STATYSTYK (Arkusz2) ---

def find_column_by_date(service, sheet_id, sheet_name, row_index, target_date):
    """Znajduje indeks kolumny (1-based) w danym wierszu na podstawie daty."""
    tz_sheet = _get_sheet_timezone()
    # Upewnij się, że target_date jest obiektem date
    if isinstance(target_date, datetime.datetime):
        target_date = target_date.astimezone(tz_sheet).date()
    elif not isinstance(target_date, datetime.date):
        logging.error(f"[Stats] Nieprawidłowy typ daty docelowej: {type(target_date)}")
        return None

    try:
        # Odczytaj cały wiersz nagłówka z datami
        range_name = f"{sheet_name}!{row_index}:{row_index}"
        logging.debug(f"[Stats] Odczyt wiersza nagłówka dat: {range_name}")
        result = service.spreadsheets().values().get(spreadsheetId=sheet_id, range=range_name).execute()
        values = result.get('values', [[]])[0] # Pobierz pierwszy (i jedyny) wiersz

        if not values:
            logging.warning(f"[Stats] Wiersz nagłówka dat ({row_index}) w '{sheet_name}' jest pusty.")
            return None

        # Iteruj przez komórki wiersza, szukając pasującej daty
        for col_idx, cell_value in enumerate(values):
            if not cell_value: # Pomiń puste komórki
                continue
            try:
                # Spróbuj sparsować datę z komórki arkusza (oczekiwany format D.M.YYYY)
                cell_date = datetime.datetime.strptime(cell_value.strip(), '%d.%m.%Y').date()
                if cell_date == target_date:
                    column_index_1_based = col_idx + 1 # Indeks kolumny jest 1-based
                    logging.info(f"[Stats] Znaleziono kolumnę dla daty {target_date.strftime('%d.%m.%Y')} -> Kolumna {column_index_1_based} ({chr(ord('A') + col_idx)})")
                    return column_index_1_based
            except ValueError:
                # Ignoruj komórki, których nie można sparsować jako daty w oczekiwanym formacie
                # Loguj tylko przy debugowaniu, aby uniknąć spamu
                # logging.debug(f"[Stats] Nie udało się sparsować daty w komórce {chr(ord('A') + col_idx)}{row_index}: '{cell_value}'")
                pass
            except Exception as parse_err:
                 logging.warning(f"[Stats] Błąd parsowania daty w komórce {chr(ord('A') + col_idx)}{row_index}: '{cell_value}', Błąd: {parse_err}")


        logging.warning(f"[Stats] Nie znaleziono kolumny dla daty {target_date.strftime('%d.%m.%Y')} w wierszu {row_index} arkusza '{sheet_name}'.")
        return None

    except HttpError as error:
        logging.error(f"[Stats] Błąd HTTP API podczas szukania kolumny daty w '{sheet_name}': {error}", exc_info=True)
        return None
    except Exception as e:
        logging.error(f"[Stats] Nieoczekiwany błąd podczas szukania kolumny daty w '{sheet_name}': {e}", exc_info=True)
        return None

def find_row_by_label(service, sheet_id, sheet_name, column_letter, target_label):
    """Znajduje indeks wiersza (1-based) w danej kolumnie na podstawie etykiety."""
    try:
        range_name = f"{sheet_name}!{column_letter}:{column_letter}" # Cała kolumna A
        logging.debug(f"[Stats] Szukanie etykiety '{target_label}' w zakresie {range_name}")
        result = service.spreadsheets().values().get(spreadsheetId=sheet_id, range=range_name).execute()
        values = result.get('values', [])

        if not values:
            logging.warning(f"[Stats] Kolumna etykiet ({column_letter}) w '{sheet_name}' jest pusta.")
            return None

        for row_idx, row in enumerate(values):
            if row and row[0].strip() == target_label:
                row_index_1_based = row_idx + 1 # Indeks wiersza jest 1-based
                logging.info(f"[Stats] Znaleziono wiersz dla etykiety '{target_label}' -> Wiersz {row_index_1_based}")
                return row_index_1_based

        logging.warning(f"[Stats] Nie znaleziono wiersza dla etykiety '{target_label}' w kolumnie {column_letter} arkusza '{sheet_name}'.")
        return None

    except HttpError as error:
        logging.error(f"[Stats] Błąd HTTP API podczas szukania wiersza etykiety w '{sheet_name}': {error}", exc_info=True)
        return None
    except Exception as e:
        logging.error(f"[Stats] Nieoczekiwany błąd podczas szukania wiersza etykiety w '{sheet_name}': {e}", exc_info=True)
        return None

def increment_cell_value(service, sheet_id, sheet_name, row_index, col_index):
    """Odczytuje, inkrementuje i zapisuje wartość w danej komórce."""
    if not row_index or not col_index:
        logging.error(f"[Stats] Nieprawidłowe indeksy wiersza/kolumny ({row_index}, {col_index}) do inkrementacji.")
        return False

    # Konwertuj col_index (1-based) na literę kolumny
    col_letter = chr(ord('A') + col_index - 1)
    cell_a1_notation = f"{sheet_name}!{col_letter}{row_index}"

    try:
        # 1. Odczytaj obecną wartość
        logging.debug(f"[Stats] Odczyt wartości z komórki {cell_a1_notation}")
        result = service.spreadsheets().values().get(
            spreadsheetId=sheet_id,
            range=cell_a1_notation,
            valueRenderOption='UNFORMATTED_VALUE' # Odczytaj jako liczbę, jeśli to możliwe
        ).execute()
        values = result.get('values', [[]])
        current_value_raw = values[0][0] if values and values[0] else 0
        current_value = 0
        if isinstance(current_value_raw, (int, float)):
            current_value = int(current_value_raw)
        elif isinstance(current_value_raw, str) and current_value_raw.isdigit():
            current_value = int(current_value_raw)
        else:
            # Jeśli wartość nie jest numeryczna lub pusta, zacznij od 0
            logging.debug(f"[Stats] Wartość w {cell_a1_notation} nie jest liczbą ('{current_value_raw}'). Zaczynam od 0.")
            current_value = 0

        # 2. Inkrementuj wartość
        new_value = current_value + 1
        logging.info(f"[Stats] Inkrementacja wartości w {cell_a1_notation} z {current_value} do {new_value}")

        # 3. Zapisz nową wartość
        body = {'values': [[new_value]]}
        update_result = service.spreadsheets().values().update(
            spreadsheetId=sheet_id,
            range=cell_a1_notation,
            valueInputOption='USER_ENTERED', # Zapisz jako liczbę
            body=body
        ).execute()
        logging.debug(f"[Stats] Zapisano nową wartość w {cell_a1_notation}. Wynik: {update_result.get('updatedCells')} komórek.")
        return True

    except HttpError as error:
        logging.error(f"[Stats] Błąd HTTP API podczas inkrementacji komórki {cell_a1_notation}: {error}", exc_info=True)
        return False
    except Exception as e:
        logging.error(f"[Stats] Nieoczekiwany błąd podczas inkrementacji komórki {cell_a1_notation}: {e}", exc_info=True)
        return False

def log_statistic(event_type):
    """Loguje statystykę (new_contact lub booking) w Arkusz2."""
    service = get_sheets_service()
    if not service:
        logging.error("[Stats] Nie można zalogować statystyki - usługa arkuszy niedostępna.")
        return

    try:
        # Określ etykietę wiersza na podstawie typu zdarzenia
        target_label = None
        if event_type == "new_contact":
            target_label = STATS_NEW_CONTACT_ROW_LABEL
        elif event_type == "booking":
            target_label = STATS_BOOKING_ROW_LABEL
        else:
            logging.error(f"[Stats] Nieznany typ zdarzenia statystycznego: {event_type}")
            return

        # Znajdź wiersz dla tej etykiety (w kolumnie A)
        target_row_index = find_row_by_label(service, SPREADSHEET_ID, STATS_SHEET_NAME, 'A', target_label)
        if not target_row_index:
            logging.error(f"[Stats] Nie znaleziono wiersza dla '{target_label}' w {STATS_SHEET_NAME}!A:A.")
            return

        # Pobierz dzisiejszą datę w strefie czasowej arkusza
        today = datetime.datetime.now(_get_sheet_timezone()).date()

        # Znajdź kolumnę dla dzisiejszej daty (w wierszu nagłówka)
        target_col_index = find_column_by_date(service, SPREADSHEET_ID, STATS_SHEET_NAME, STATS_DATE_HEADER_ROW, today)
        if not target_col_index:
            logging.error(f"[Stats] Nie znaleziono kolumny dla daty {today.strftime('%d.%m.%Y')} w {STATS_SHEET_NAME} wiersz {STATS_DATE_HEADER_ROW}.")
            return

        # Inkrementuj wartość w znalezionej komórce
        increment_cell_value(service, SPREADSHEET_ID, STATS_SHEET_NAME, target_row_index, target_col_index)

    except Exception as e:
        logging.error(f"[Stats] Ogólny błąd podczas logowania statystyki '{event_type}': {e}", exc_info=True)

# =====================================================================
# === FUNKCJE KOMUNIKACJI FB ==========================================
# =====================================================================

def _send_typing_on(recipient_id, page_access_token): # Dodano page_access_token
    """Wysyła wskaźnik 'pisania' do użytkownika."""
    if not page_access_token or len(page_access_token) < 50 or not ENABLE_TYPING_DELAY:
        return
    logging.debug(f"[{recipient_id}] Wysyłanie 'typing_on'")
    params = {"access_token": page_access_token} # Użyj przekazanego tokenu
    payload = {"recipient": {"id": recipient_id}, "sender_action": "typing_on"}
    try:
        # Używamy krótkiego timeoutu dla akcji niekrytycznej
        requests.post(FACEBOOK_GRAPH_API_URL, params=params, json=payload, timeout=3)
    except requests.exceptions.RequestException as e:
        logging.warning(f"[{recipient_id}] Błąd wysyłania 'typing_on': {e}")

def _send_single_message(recipient_id, message_text, page_access_token): # Dodano page_access_token
    """Wysyła pojedynczy fragment wiadomości przez Facebook Graph API."""
    logging.info(f"--- Wysyłanie fragmentu do {recipient_id} (dł: {len(message_text)}) ---")
    # Podstawowa walidacja
    if not recipient_id or not message_text:
        logging.error(f"Błąd wysyłania: Brak ID odbiorcy lub treści wiadomości.")
        return False
    if not page_access_token or len(page_access_token) < 50:
        logging.error(f"!!! [{recipient_id}] Brak lub nieprawidłowy token strony FB dla tej wiadomości. NIE WYSŁANO.")
        return False

    params = {"access_token": page_access_token} # Użyj przekazanego tokenu
    payload = {
        "recipient": {"id": recipient_id},
        "message": {"text": message_text},
        "messaging_type": "RESPONSE" # Standardowy typ odpowiedzi na wiadomość użytkownika
    }
    try:
        # Zwiększony timeout dla głównej operacji wysyłania
        r = requests.post(FACEBOOK_GRAPH_API_URL, params=params, json=payload, timeout=30)
        r.raise_for_status() # Rzuci wyjątkiem dla statusów 4xx/5xx
        response_json = r.json()

        # Sprawdzenie błędu w odpowiedzi JSON od Facebooka
        if fb_error := response_json.get('error'):
            error_code = fb_error.get('code')
            error_msg = fb_error.get('message', 'Brak wiadomości o błędzie FB.')
            logging.error(f"!!! BŁĄD FB API (wysyłanie) dla {recipient_id}: Kod={error_code}, Wiadomość='{error_msg}' Pełny błąd: {fb_error} !!!")
            if error_code == 190: # Błąd autentykacji tokena
                logging.critical(f"!!! Token strony FB użyty dla {recipient_id} jest nieprawidłowy, wygasł lub ma niewystarczające uprawnienia (pages_messaging) !!!")
            elif error_code == 2018001: # Np. użytkownik zablokował bota
                 logging.warning(f"Użytkownik {recipient_id} prawdopodobnie zablokował bota lub nie można do niego wysłać wiadomości.")
            # Inne kody błędów: https://developers.facebook.com/docs/messenger-platform/reference/errors/
            return False # Błąd zwrócony przez API

        # Sukces
        logging.debug(f"[{recipient_id}] Fragment wysłany pomyślnie (FB Msg ID: {response_json.get('message_id')}).")
        return True

    except requests.exceptions.Timeout:
        logging.error(f"!!! BŁĄD TIMEOUT podczas wysyłania wiadomości do {recipient_id} !!!")
        return False
    except requests.exceptions.HTTPError as http_err:
        logging.error(f"!!! BŁĄD HTTP {http_err.response.status_code} podczas wysyłania do {recipient_id}: {http_err} !!!")
        # Logowanie treści odpowiedzi błędu, jeśli dostępna
        if http_err.response is not None:
            try:
                logging.error(f"    Odpowiedź FB (HTTP Err): {http_err.response.json()}")
            except json.JSONDecodeError:
                logging.error(f"    Odpowiedź FB (HTTP Err, nie JSON): {http_err.response.text}")
        return False
    except requests.exceptions.RequestException as req_err:
        # Inne błędy związane z requestem (np. DNS, połączenie)
        logging.error(f"!!! BŁĄD RequestException podczas wysyłania do {recipient_id}: {req_err} !!!")
        return False
    except Exception as e:
        # Nieoczekiwane inne błędy
        logging.error(f"!!! Nieoczekiwany BŁĄD podczas wysyłania wiadomości do {recipient_id}: {e} !!!", exc_info=True)
        return False


def send_message(recipient_id, full_message_text, page_access_token): # Dodano page_access_token
    """Wysyła wiadomość, dzieląc ją w razie potrzeby i dodając opóźnienia."""
    if not full_message_text or not isinstance(full_message_text, str) or not full_message_text.strip():
        logging.warning(f"[{recipient_id}] Pominięto wysłanie pustej lub nieprawidłowej wiadomości.")
        return

    message_len = len(full_message_text)
    logging.info(f"[{recipient_id}] Przygotowanie do wysłania wiadomości (długość: {message_len}).")

    # --- Obliczanie i symulacja pisania (jeśli włączone) ---
    if ENABLE_TYPING_DELAY:
        # Szacowany czas pisania - bardziej realistyczny, z ograniczeniami
        estimated_typing_duration = min(MAX_TYPING_DELAY_SECONDS, max(MIN_TYPING_DELAY_SECONDS, message_len / TYPING_CHARS_PER_SECOND))
        logging.debug(f"[{recipient_id}] Szacowany czas pisania: {estimated_typing_duration:.2f}s")
        _send_typing_on(recipient_id, page_access_token) # Użyj właściwego tokenu
        time.sleep(estimated_typing_duration) # Zaczekaj oszacowany czas

    # --- Dzielenie wiadomości na fragmenty ---
    chunks = []
    if message_len <= MESSAGE_CHAR_LIMIT:
        chunks.append(full_message_text)
    else:
        logging.info(f"[{recipient_id}] Wiadomość za długa ({message_len} > {MESSAGE_CHAR_LIMIT}). Dzielenie na fragmenty...")
        remaining_text = full_message_text
        while remaining_text:
            # Jeśli pozostałość mieści się w limicie, dodaj ją jako ostatni fragment
            if len(remaining_text) <= MESSAGE_CHAR_LIMIT:
                chunks.append(remaining_text.strip())
                break

            # Znajdź najlepsze miejsce do podziału (od końca do początku w limicie)
            split_index = -1
            # Priorytety podziału: 1. Podwójny newline 2. Newline 3. Kropka+spacja 4. Wykrzyknik+spacja 5. Pytajnik+spacja 6. Spacja
            delimiters = ['\n\n', '\n', '. ', '! ', '? ', ' ']
            # Szukaj od końca limitu (MESSAGE_CHAR_LIMIT) wstecz
            search_end_pos = MESSAGE_CHAR_LIMIT
            for delim in delimiters:
                # rfind szuka od prawej do lewej
                found_index = remaining_text.rfind(delim, 0, search_end_pos)
                if found_index != -1:
                    # Znaleziono separator, miejsce podziału to koniec separatora
                    split_index = found_index + len(delim)
                    break # Znaleziono najlepszy separator, przerwij

            # Jeśli nie znaleziono żadnego separatora, tnij "na twardo" po limicie
            if split_index == -1:
                split_index = MESSAGE_CHAR_LIMIT

            # Wydziel fragment i usuń białe znaki z początku/końca
            chunk = remaining_text[:split_index].strip()
            if chunk: # Dodaj tylko jeśli fragment nie jest pusty
                chunks.append(chunk)
            # Zaktualizuj pozostały tekst
            remaining_text = remaining_text[split_index:].strip()

        logging.info(f"[{recipient_id}] Podzielono wiadomość na {len(chunks)} fragmentów.")

    # --- Wysyłanie fragmentów z opóźnieniami ---
    num_chunks = len(chunks)
    successful_sends = 0
    for i, chunk_text in enumerate(chunks):
        logging.debug(f"[{recipient_id}] Wysyłanie fragmentu {i+1}/{num_chunks}...")
        if not _send_single_message(recipient_id, chunk_text, page_access_token): # Użyj właściwego tokenu
            # Jeśli wysłanie fragmentu się nie powiedzie, loguj błąd i przerwij wysyłanie reszty
            logging.error(f"!!! [{recipient_id}] Błąd wysyłania fragmentu {i+1}/{num_chunks}. Anulowanie wysyłania pozostałych.")
            break # Przerwij pętlę
        successful_sends += 1

        # Jeśli jest więcej niż jeden fragment i to nie jest ostatni, poczekaj
        if num_chunks > 1 and i < num_chunks - 1:
            logging.debug(f"[{recipient_id}] Oczekiwanie {MESSAGE_DELAY_SECONDS}s przed następnym fragmentem...")
            # Opcjonalnie: Można dodać ponowne wysłanie "typing_on" tutaj dla dłuższych przerw
            if ENABLE_TYPING_DELAY:
                 _send_typing_on(recipient_id, page_access_token) # Wyślij typing przed pauzą
            time.sleep(MESSAGE_DELAY_SECONDS)

    logging.info(f"--- [{recipient_id}] Zakończono proces wysyłania. Wysłano {successful_sends}/{num_chunks} fragmentów. ---")

def _simulate_typing(recipient_id, duration_seconds, page_access_token): # Dodano page_access_token
    """Wysyła 'typing_on' i czeka określoną liczbę sekund (jeśli włączone)."""
    if ENABLE_TYPING_DELAY and duration_seconds > 0:
        _send_typing_on(recipient_id, page_access_token) # Użyj właściwego tokenu
        # Czekaj nie dłużej niż maksymalny sensowny czas pisania
        wait_time = min(duration_seconds, MAX_TYPING_DELAY_SECONDS * 1.1)
        time.sleep(wait_time)

# =====================================================================
# === FUNKCJE WYWOŁANIA AI ============================================
# =====================================================================

def _call_gemini(user_psid, prompt_history, generation_config, task_name, page_access_token, max_retries=3): # Dodano page_access_token
    """Wywołuje API Gemini z obsługą błędów i ponowień."""
    if not gemini_model:
        logging.error(f"!!! [{user_psid}] KRYTYCZNY BŁĄD: Model Gemini ({task_name}) niedostępny (gemini_model is None)!")
        # Zwróć generyczny błąd, aby uniknąć crasha
        return "Przepraszam, wystąpił wewnętrzny błąd systemu. Spróbuj ponownie później."

    # Walidacja promptu
    if not isinstance(prompt_history, list) or not all(isinstance(item, Content) for item in prompt_history):
        logging.error(f"!!! [{user_psid}] Nieprawidłowy format historii promptu przekazany do _call_gemini ({task_name}). Typ: {type(prompt_history)}")
        return "Przepraszam, wystąpił błąd przetwarzania wewnętrznego."

    logging.info(f"[{user_psid}] Wywołanie Gemini: {task_name} (Prompt: {len(prompt_history)} wiadomości)")
    # Logowanie ostatniej wiadomości użytkownika dla kontekstu
    last_user_msg_part = next((msg.parts[0] for msg in reversed(prompt_history) if msg.role == 'user' and msg.parts), None)
    if last_user_msg_part and hasattr(last_user_msg_part, 'text'):
        last_user_msg = last_user_msg_part.text
        log_msg = f"'{last_user_msg[:200]}{'...' if len(last_user_msg)>200 else ''}'"
        logging.debug(f"    Ostatnia wiad. usera ({task_name}): {log_msg}")
    # else:
    #     logging.debug(f"    Brak wiadomości użytkownika w historii promptu ({task_name}).")

    attempt = 0
    while attempt < max_retries:
        attempt += 1
        logging.debug(f"    Próba wywołania Gemini {attempt}/{max_retries} ({task_name})...")
        try:
            # Symulacja pisania przed wywołaniem AI
            _simulate_typing(user_psid, MIN_TYPING_DELAY_SECONDS * 0.8, page_access_token) # Przekaż token

            # Wywołanie API Gemini
            response = gemini_model.generate_content(
                prompt_history,
                generation_config=generation_config,
                safety_settings=SAFETY_SETTINGS,
                stream=False # Oczekujemy pełnej odpowiedzi
            )

            # --- Analiza odpowiedzi ---
            if not response:
                 logging.warning(f"[{user_psid}] Gemini ({task_name}) zwróciło pustą odpowiedź (None).")
                 if attempt < max_retries:
                     time.sleep(1 + random.random()) # Krótka pauza przed ponowieniem
                     continue
                 else:
                     return "Przepraszam, nie udało się uzyskać odpowiedzi od AI."

            # Sprawdzenie kandydatów
            if not response.candidates:
                # Sprawdzenie prompt_feedback na wypadek blokady na wejściu
                prompt_feedback = response.prompt_feedback if hasattr(response, 'prompt_feedback') else None
                if prompt_feedback and hasattr(prompt_feedback, 'block_reason') and prompt_feedback.block_reason != 0: # 0 = BLOCK_REASON_UNSPECIFIED
                     block_reason_name = prompt_feedback.block_reason.name if hasattr(prompt_feedback.block_reason, 'name') else str(prompt_feedback.block_reason)
                     logging.error(f"!!! BŁĄD [{user_psid}] Gemini ({task_name}) - PROMPT ZABLOKOWANY! Powód: {block_reason_name}. Feedback: {prompt_feedback}")
                     # Zwróć użytkownikowi informację o blokadzie promptu
                     return "Przepraszam, Twoja wiadomość nie mogła zostać przetworzona ze względu na zasady bezpieczeństwa."
                else:
                    logging.warning(f"[{user_psid}] Gemini ({task_name}) brak kandydatów w odpowiedzi. Feedback promptu: {prompt_feedback}")
                    if attempt < max_retries:
                        time.sleep(1.5 * attempt * random.uniform(0.8, 1.2))
                        continue
                    else:
                        return "Przepraszam, problem z generowaniem odpowiedzi (brak kandydatów)."

            # Przetwarzanie pierwszego kandydata
            candidate = response.candidates[0]
            finish_reason = candidate.finish_reason if hasattr(candidate, 'finish_reason') else None
            finish_reason_val = finish_reason.value if finish_reason else 0 # 1 = STOP

            # Sprawdzenie powodu zakończenia
            if finish_reason_val != 1: # Jeśli powód inny niż STOP (1)
                finish_reason_name = finish_reason.name if hasattr(finish_reason, 'name') else str(finish_reason_val or 'UNKNOWN')
                safety_ratings = candidate.safety_ratings if hasattr(candidate, 'safety_ratings') else "Brak danych safety"
                logging.warning(f"[{user_psid}] Gemini ({task_name}) ZAKOŃCZONE NIEPRAWIDŁOWO! Powód: {finish_reason_name} ({finish_reason_val}). Safety: {safety_ratings}")

                # Obsługa blokad bezpieczeństwa (RECITATION=4, SAFETY=3)
                if finish_reason_val in [3, 4]: # SAFETY or RECITATION
                    if attempt < max_retries:
                        logging.warning(f"    Ponawianie ({attempt}/{max_retries}) z powodu blokady ({finish_reason_name})...")
                        time.sleep(1.5 * attempt * random.uniform(0.8, 1.2))
                        continue
                    else: # Ostatnia próba nieudana z powodu blokady
                        logging.error(f"!!! [{user_psid}] Gemini ({task_name}) nieudane po {attempt} próbach z powodu blokady ({finish_reason_name}).")
                        if finish_reason_val == 3: return "Przepraszam, nie mogę wygenerować odpowiedzi ze względu na zasady bezpieczeństwa."
                        if finish_reason_val == 4: return "Przepraszam, nie mogę wygenerować odpowiedzi, ponieważ naruszałaby zasady cytowania." # Recitation

                # Obsługa przekroczenia limitu tokenów (MAX_TOKENS=2)
                elif finish_reason_val == 2: # MAX_TOKENS
                     logging.warning(f"    Odpowiedź ucięta (MAX_TOKENS).")
                     # Czy ponawiać? Może zwrócić częściową odpowiedź? Na razie zwracamy błąd.
                     # Jeśli jest jakaś treść, można by ją zwrócić z ostrzeżeniem.
                     partial_text = "".join(part.text for part in candidate.content.parts if hasattr(candidate.content, 'parts') and hasattr(part, 'text')).strip()
                     if partial_text:
                         logging.warning(f"    Zwracam częściową odpowiedź ({len(partial_text)} znaków) z powodu MAX_TOKENS.")
                         return partial_text + "..." # Dodaj "..." dla oznaczenia ucięcia
                     else:
                         if attempt < max_retries:
                             logging.warning(f"    Brak treści przy MAX_TOKENS. Ponawianie ({attempt}/{max_retries})...")
                             time.sleep(1.5 * attempt * random.uniform(0.8, 1.2))
                             continue
                         else:
                             return "Przepraszam, wygenerowana odpowiedź była zbyt długa."
                # Inne powody zakończenia (np. OTHER=5)
                else:
                    if attempt < max_retries:
                        logging.warning(f"    Nieoczekiwany powód zakończenia ({finish_reason_name}). Ponawianie ({attempt}/{max_retries})...")
                        time.sleep(1.5 * attempt * random.uniform(0.8, 1.2))
                        continue
                    else:
                         logging.error(f"!!! [{user_psid}] Gemini ({task_name}) nieudane po {attempt} próbach ({finish_reason_name}).")
                         return f"Przepraszam, wystąpił problem z generowaniem odpowiedzi (kod: {finish_reason_name})."


            # --- Ekstrakcja tekstu z poprawnej odpowiedzi (finish_reason == STOP) ---
            if hasattr(candidate, 'content') and candidate.content and hasattr(candidate.content, 'parts') and candidate.content.parts:
                generated_text = "".join(part.text for part in candidate.content.parts if hasattr(part, 'text')).strip()
                if generated_text:
                    logging.info(f"[{user_psid}] Gemini ({task_name}) zwróciło poprawną odpowiedź (długość: {len(generated_text)}).")
                    logging.debug(f"    Odpowiedź Gemini ({task_name}): '{generated_text[:300]}{'...' if len(generated_text)>300 else ''}'")
                    return generated_text # Sukces - zwracamy tekst
                else:
                    # Rzadki przypadek: FinishReason=STOP, ale brak treści
                    logging.warning(f"[{user_psid}] Gemini ({task_name}) zwróciło pustą treść mimo FinishReason=STOP.")
                    if attempt < max_retries:
                         time.sleep(1 + random.random())
                         continue
                    else:
                         return "Przepraszam, problem z wygenerowaniem odpowiedzi (pusta treść)."
            else:
                # Rzadki przypadek: FinishReason=STOP, ale brak obiektu content/parts
                logging.warning(f"[{user_psid}] Gemini ({task_name}) zwróciło kandydata bez treści/części mimo FinishReason=STOP.")
                if attempt < max_retries:
                    time.sleep(1 + random.random())
                    continue
                else:
                    return "Przepraszam, problem z wygenerowaniem odpowiedzi (brak struktury treści)."

        # --- Obsługa wyjątków HTTP i innych ---
        except HttpError as http_err:
            status_code = http_err.resp.status if hasattr(http_err, 'resp') and hasattr(http_err.resp, 'status') else 'Nieznany'
            reason = http_err.resp.reason if hasattr(http_err, 'resp') and hasattr(http_err.resp, 'reason') else 'Nieznany'
            logging.error(f"!!! BŁĄD HTTP ({status_code} {reason}) [{user_psid}] Gemini ({task_name}) - Próba {attempt}/{max_retries}.")
            # Ponawianie dla błędów serwera (5xx) lub rate limiting (429)
            if status_code in [429, 500, 503] and attempt < max_retries:
                # Exponential backoff z jitterem
                sleep_time = (2 ** attempt) + random.uniform(0, 1)
                logging.warning(f"    Oczekiwanie {sleep_time:.2f}s przed ponowieniem z powodu błędu {status_code}...")
                time.sleep(sleep_time)
                continue
            else:
                logging.error(f"    Nie ponawiam błędu HTTP {status_code}.")
                return f"Przepraszam, wystąpił błąd komunikacji z systemem AI (HTTP {status_code}). Spróbuj ponownie za chwilę."
        except Exception as e:
            # Sprawdzenie krytycznego błędu braku modelu
            if isinstance(e, NameError) and 'gemini_model' in str(e):
                 logging.critical(f"!!! KRYTYCZNY NameError [{user_psid}]: {e}. 'gemini_model' nie jest zdefiniowany!", exc_info=True)
                 return "Przepraszam, krytyczny błąd wewnętrzny systemu AI (brak modelu)."
            # Inne błędy Python
            else:
                 logging.error(f"!!! BŁĄD Python [{user_psid}] podczas wywołania Gemini ({task_name}) - Próba {attempt}/{max_retries}: {e}", exc_info=True)
                 if attempt < max_retries:
                     # Exponential backoff z jitterem
                     sleep_time = (2 ** attempt) + random.uniform(0, 1)
                     logging.warning(f"    Nieoczekiwany błąd Python. Oczekiwanie {sleep_time:.2f}s przed ponowieniem...")
                     time.sleep(sleep_time)
                     continue
                 else:
                     logging.error(f"    Nie ponawiam błędu Python po {max_retries} próbach.")
                     return "Przepraszam, wystąpił nieoczekiwany błąd przetwarzania."

    # Jeśli pętla zakończyła się bez zwrócenia odpowiedzi
    logging.error(f"!!! KRYTYCZNY BŁĄD [{user_psid}] Gemini ({task_name}) - Nie udało się uzyskać poprawnej odpowiedzi po {max_retries} próbach.")
    # Zwróć najbardziej prawdopodobny błąd, jeśli był (np. z ostatniej iteracji)
    # Tutaj można by dodać logikę zwracania ostatniego znanego błędu, ale dla uproszczenia:
    return "Przepraszam, nie udało się przetworzyć Twojej wiadomości po kilku próbach. Spróbuj ponownie później."


# =====================================================================
# === INSTRUKCJE SYSTEMOWE I GŁÓWNE FUNKCJE AI ========================
# =====================================================================

# --- SYSTEM_INSTRUCTION_SCHEDULING ---
# Bez zmian - już przyjmuje {subject}
SYSTEM_INSTRUCTION_SCHEDULING = """Jesteś pomocnym asystentem AI specjalizującym się w umawianiu terminów korepetycji online. Twoim zadaniem jest znalezienie pasującego terminu dla użytkownika na podstawie jego preferencji oraz dostarczonej listy dostępnych zakresów czasowych.

**Kontekst:**
*   Rozmawiasz z użytkownikiem, który wyraził chęć umówienia się na lekcję z przedmiotu: **{subject}**.
*   Poniżej znajduje się lista AKTUALNIE dostępnych ZAKRESÓW czasowych **dla przedmiotu {subject}**, w których można umówić wizytę (każda trwa {duration} minut). **Wszystkie podane zakresy są już odpowiednio odsunięte w czasie (filtr {min_lead_hours}h) i dotyczą wyłącznie tego przedmiotu.**
*   Masz dostęp do historii poprzedniej rozmowy. Czasami rozmowa mogła zostać przerwana pytaniem ogólnym i teraz do niej wracamy.

**Styl Komunikacji:**
*   **Naturalność:** Zachowuj się jak człowiek, unikaj schematycznych i powtarzalnych odpowiedzi.
*   **Umiar:** Nie używaj nadmiernie wykrzykników ani entuzjastycznych sformułowań typu "Super!", "Jasne!".
*   **Bez Emotek:** Nie używaj emotikon.
*   **Profesjonalizm:** Bądź uprzejmy, rzeczowy i pomocny. Używaj zwrotów "Państwo".
*   **Język:** Odpowiadaj płynnie po polsku.

**Dostępne zakresy czasowe dla {subject}:**
{available_ranges_text}

**Twoje zadanie:**
1.  **Rozpocznij rozmowę LUB WZNÓW:** Jeśli to początek umawiania (dla {subject}) lub jeśli ostatnia wiadomość użytkownika nie dotyczyła preferencji terminu, potwierdź, że widzisz dostępne terminy dla **{subject}** i zapytaj użytkownika o jego **ogólne preferencje** dotyczące dnia tygodnia lub pory dnia (np. "Mamy kilka wolnych terminów z {subject}. Czy preferują Państwo jakiś konkretny dzień tygodnia lub porę dnia - rano, popołudnie, wieczór?"). **Nie proponuj jeszcze konkretnej daty i godziny.** Odpowiadaj na ewentualne pytania użytkownika dotyczące dostępności lub procesu umawiania dla {subject}.
2.  **Negocjuj:** Na podstawie odpowiedzi użytkownika **dotyczącej preferencji terminu**, historii konwersacji i **wyłącznie dostępnych zakresów z listy dla {subject}**, kontynuuj rozmowę, aby znaleźć termin pasujący obu stronom. Gdy użytkownik poda preferencje, **zaproponuj konkretny termin z listy**, który im odpowiada (np. "W takim razie, z {subject} może pasowałaby środa o 17:00?"). Jeśli ostatnia wiadomość użytkownika nie była odpowiedzią na pytanie o termin, wróć do kroku 1. Odpowiadaj na pytania dotyczące proponowanych terminów.
3.  **Potwierdź i dodaj znacznik:** Kiedy wspólnie ustalicie **dokładny termin** (np. "Środa, 15 maja o 18:30") dla **{subject}**, który **znajduje się na liście dostępnych zakresów**, potwierdź go w swojej odpowiedzi (np. "Świetnie, w takim razie proponowany termin na {subject} to środa, 15 maja o 18:30.") i **zakończ swoją odpowiedź potwierdzającą DOKŁADNIE znacznikiem** `{slot_marker_prefix}YYYY-MM-DDTHH:MM:SS{slot_marker_suffix}`. Użyj formatu ISO 8601 dla ustalonego czasu rozpoczęcia (np. 2024-05-15T18:30:00). Upewnij się, że data i godzina w znaczniku są poprawne, zgodne z ustaleniami i **pochodzą z listy dostępnych zakresów dla {subject}**.
4.  **NIE dodawaj znacznika**, jeśli:
    *   Użytkownik jeszcze się zastanawia lub prosi o więcej opcji dla {subject}.
    *   Użytkownik proponuje termin, którego nie ma na liście dostępnych zakresów dla {subject}.
    *   Nie udało się znaleźć pasującego terminu dla {subject}.
    *   Lista dostępnych zakresów dla {subject} jest pusta.
5.  **Brak terminów:** Jeśli lista zakresów dla {subject} jest pusta lub po rozmowie okaże się, że żaden termin nie pasuje, poinformuj o tym użytkownika uprzejmie, wspominając o przedmiocie {subject}. Nie dodawaj znacznika.
6.  **Pytania poza tematem:** Jeśli użytkownik zada pytanie **niezwiązane bezpośrednio z ustalaniem terminu z listy dla {subject}** (np. o cenę innego przedmiotu, metodykę ogólną), **NIE ODPOWIADAJ na nie**. Zamiast tego, Twoja odpowiedź musi zawierać **TYLKO I WYŁĄCZNIE** znacznik: `{switch_marker}`. System przełączy się wtedy do trybu ogólnych odpowiedzi.

**Pamiętaj:**
*   Trzymaj się **wyłącznie** terminów i godzin wynikających z "Dostępnych zakresów czasowych dla {subject}".
*   Bądź elastyczny w rozmowie, ale propozycje muszą pochodzić z listy dla {subject}.
*   Używaj języka polskiego i polskiej strefy czasowej ({calendar_timezone}).
*   Znacznik `{slot_marker_prefix}...{slot_marker_suffix}` jest sygnałem dla systemu, że **osiągnięto porozumienie co do terminu dla {subject} z dostępnej listy**. Używaj go tylko w tym jednym, konkretnym przypadku.
*   Znacznik `{switch_marker}` służy do przekazania obsługi pytania ogólnego.
*   Nie podawaj pełnej listy zakresów wolnych terminów, staraj się pytać raczej o preferencje i dawać konkretne propozycje z listy dla {subject}.
""" # .format() zostanie użyte w funkcji wywołującej

# --- SYSTEM_INSTRUCTION_GATHERING --- (bez zmian)
SYSTEM_INSTRUCTION_GATHERING = """Rozmawiasz z klientem. Twoim zadaniem jest zebranie informacji wyłącznie o UCZNIU, potrzebnych do zapisu na korepetycje, po tym jak wstępnie ustalono termin. Dane rodzica zostaną pobrane automatycznie przez system.

**Kontekst:**
*   Wstępnie ustalony termin lekcji to: {proposed_slot_formatted} (Przedmiot został już ustalony wcześniej).
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
    a)  **DOKŁADNIE** linijka w formacie: `ZEBRANE_DANE_UCZNIA: [Imię: <imię>, Nazwisko: <nazwisko>, KlasaInfo: <pełna informacja o klasie i szkole np. 3 klasa liceum>, Poziom: <Podstawowy/Rozszerzony/brak>]` (Zastąp <...> zebranymi danymi. Jeśli poziom nie dotyczy lub nie został podany, wpisz "Brak").
    b)  **PO TEJ LINIJCE**, w nowej linii, standardowa wiadomość dla użytkownika: "Dobrze, dziękujemy za wszystkie informacje. Aby lekcja się odbyła prosimy jeszcze o potwierdzenie zajęć wysyłając wiadomość "POTWIERDZAM" na podany profil. Jest to profil także to dalszego kontaktu w sprawie zajęć: https://www.facebook.com/profile.php?id=61576135251276"
    c)  **NA SAMYM KOŃCU** całej odpowiedzi dodaj **DOKŁADNIE** znacznik: `{info_gathered_marker}`.
6.  **NIE dodawaj znacznika ani struktury `ZEBRANE_DANE_UCZNIA:`**, jeśli nadal brakuje którejś z wymaganych informacji o uczniu. Kontynuuj zadawanie pytań.
7.  **Pytania poza tematem:** Jeśli użytkownik zada pytanie **niezwiązane bezpośrednio ze zbieraniem danych ucznia** (np. o cenę, metodykę), **NIE ODPOWIADAJ na nie**. Zamiast tego, Twoja odpowiedź musi zawierać **TYLKO I WYŁĄCZNIE** znacznik: `{switch_marker}`. System przełączy się wtedy do trybu ogólnych odpowiedzi.

**Przykład poprawnej odpowiedzi końcowej:**
```
ZEBRANE_DANE_UCZNIA: [Imię: Jan, Nazwisko: Kowalski, KlasaInfo: 2 klasa liceum, Poziom: Rozszerzony]
Dobrze, dziękujemy za wszystkie informacje. Aby lekcja się odbyła prosimy jeszcze o potwierdzenie zajęć wysyłając wiadomość "POTWIERDZAM" na podany profil. Jest to profil także to dalszego kontaktu w sprawie zajęć: https://www.facebook.com/profile.php?id=61576135251276{info_gathered_marker}
```

**Pamiętaj:** Kluczowe jest dokładne przestrzeganie formatu `ZEBRANE_DANE_UCZNIA: [...]` w przedostatniej linijce odpowiedzi końcowej. Znacznik `{switch_marker}` służy do przekazania obsługi pytania ogólnego.
""".format(
    proposed_slot_formatted="{proposed_slot_formatted}", # Będzie formatowane dynamicznie
    known_student_first_name="{known_student_first_name}", # Będzie formatowane dynamicznie
    known_student_last_name="{known_student_last_name}", # Będzie formatowane dynamicznie
    known_grade="{known_grade}", # Będzie formatowane dynamicznie
    known_level="{known_level}", # Będzie formatowane dynamicznie
    info_gathered_marker=INFO_GATHERED_MARKER,
    switch_marker=SWITCH_TO_GENERAL
)


# --- SYSTEM_INSTRUCTION_GENERAL ---
# Zmodyfikowany, aby uwzględnić, że przedmiot może być już znany z kontekstu strony
SYSTEM_INSTRUCTION_GENERAL = """Jesteś przyjaznym, proaktywnym i profesjonalnym asystentem klienta centrum korepetycji. Twoim głównym celem jest przeprowadzenie klienta przez proces zapoznania się z ofertą i zachęcenie go do umówienia pierwszej lekcji.

**Styl Komunikacji:**
*   **Naturalność:** Zachowuj się jak człowiek, unikaj schematycznych i powtarzalnych odpowiedzi.
*   **Umiar:** Nie używaj nadmiernie wykrzykników ani entuzjastycznych sformułowań typu "Super!", "Jasne!".
*   **Bez Emotek:** Nie używaj emotikon.
*   **Profesjonalizm:** Bądź uprzejmy, rzeczowy i pomocny. Używaj zwrotów "Państwo".
*   **Język:** Odpowiadaj płynnie po polsku.

**Dostępne Przedmioty:** {available_subjects_list}

**Cennik (za 60 minut):**
*   Szkoła Podstawowa: 60 zł
*   Liceum/Technikum (Poziom Podstawowy, klasa 1-2): 65 zł
*   Liceum/Technikum (Poziom Podstawowy, klasa 3-4/5): 70 zł
*   Liceum/Technikum (Poziom Rozszerzony, klasa 1): 65 zł
*   Liceum/Technikum (Poziom Rozszerzony, klasa 2): 70 zł
*   Liceum/Technikum (Poziom Rozszerzony, klasa 3-4/5): 75 zł

**Format Lekcji:** Online, przez platformę Microsoft Teams (bez konieczności instalacji, wystarczy link).

**Twój Przepływ Pracy:**

1.  **Identyfikacja Potrzeby (PRZEDMIOT):**
    *   **Jeśli ZNASZ już przedmiot (np. z kontekstu strony):** Przywitaj się uprzejmie, potwierdź przedmiot (np. "Dzień dobry! Widzę, że kontaktują się Państwo w sprawie korepetycji z przedmiotu [Przedmiot].")

2. **Szybka informacja**
    *  Pinformuj, że udzielacie korepetycji również z innych przedmiotów i podaj linki do odpowiednich stron, korzystając z informacji z sekcji "Dostępne Przedmioty i Linki do Stron".** Twoja informacja powinna być sformułowana np. tak: "Gdyby byli Państwo zainteresowani to udzielamy również korepetycji z [Inny Przedmiot 1] (kontakt: [Link do Innego Przedmiotu 1]) oraz [Inny Przedmiot 2] (kontakt: [Link do Innego Przedmiotu 2])." **Nie wymieniaj tutaj przedmiotu, z którego właśnie toczy się rozmowa (jeśli jest znany jako {current_subject_from_page}).**


3.  **Zbieranie Informacji o Uczniu:**
    *   Zapytaj o **klasę** ucznia oraz **typ szkoły** (podstawowa czy średnia - liceum/technikum). Staraj się uzyskać obie informacje. Jeśli jest to poziom szkoły podstawowej poniżej 4 klasy poinformuj, że nie udzielamy korepetycji dla takiego poziomu.
    *   **Tylko jeśli** szkoła to liceum lub technikum, zapytaj o **poziom nauczania** (podstawowy czy rozszerzony).

4.  **Prezentacja Ceny i Formatu:**
    *   Na podstawie zebranych informacji (przedmiot, klasa, typ szkoły, poziom), **ustal właściwą cenę** z cennika.
    *   **Poinformuj klienta o cenie** za 60 minut lekcji dla danego poziomu i przedmiotu, np. "Dla ucznia w [klasa] [typ szkoły] na poziomie [poziom] z przedmiotu [przedmiot] koszt zajęć wynosi [cena] zł za 60 minut.".
    *   **Dodaj informację o formacie:** "Wszystkie zajęcia odbywają się wygodnie online przez platformę Microsoft Teams - wystarczy kliknąć w link, nie trzeba nic instalować."

5.  **Zachęta do Umówienia Lekcji:**
    *   Po podaniu ceny i informacji o formacie, **bezpośrednio zapytaj**, czy klient jest zainteresowany umówieniem pierwszej lekcji (może być próbna), np. "Czy byliby Państwo zainteresowani umówieniem pierwszej lekcji z [przedmiot], aby zobaczyć, jak pracujemy?".

6.  **Obsługa Odpowiedzi na Propozycję Lekcji:**
    *   **Jeśli TAK (lub podobna pozytywna odpowiedź):** Twoja odpowiedź musi zawierać **TYLKO I WYŁĄCZNIE** znacznik: `{intent_marker}`. System przejmie wtedy proces umawiania terminu dla ustalonego przedmiotu. **Upewnij się, że przedmiot został wcześniej jasno ustalony.**
    *   **Jeśli NIE (lub wahanie):**
        *   Zapytaj delikatnie o powód odmowy/wątpliwości.
        *   **Jeśli powodem jest forma ONLINE:** Wyjaśnij zalety: "Jeśli chodzi o formę online, chciałbym zapewnić, że nasi korepetytorzy to profesjonaliści z doświadczeniem w prowadzeniu zajęć zdalnych. Używamy interaktywnych narzędzi na platformie Teams, co sprawia, że lekcje są angażujące i efektywne – zupełnie inaczej niż mogło to wyglądać podczas nauki zdalnej w pandemii. Wszystko odbywa się przez przeglądarkę po kliknięciu w link."
        *   **Po wyjaśnieniu (lub jeśli powód był inny):** Zaproponuj lekcję próbną (płatną jak standardowa, bez zobowiązań) dla ustalonego przedmiotu.
        *   **Jeśli klient zgodzi się na lekcję próbną po perswazji:** Twoja odpowiedź musi zawierać **TYLKO I WYŁĄCZNIE** znacznik: `{intent_marker}`.
        *   **Jeśli klient nadal odmawia:** Podziękuj za rozmowę i zakończ uprzejmie. (Bez znacznika).
    *   **Jeśli użytkownik zada inne pytanie:** Odpowiedz na nie zgodnie z ogólnymi zasadami i **ponownie spróbuj zachęcić** do umówienia lekcji z ustalonego przedmiotu (wróć do kroku 4 lub 5). **Jeśli pytanie dotyczy innego przedmiotu, potwierdź zmianę przedmiotu i wróć do kroku 2.**

7.  **Obsługa Powrotu (jeśli aktywowano Cię w trybie tymczasowym):**
    *   Odpowiedz na pytanie ogólne użytkownika.
    *   Jeśli odpowiedź użytkownika na Twoją odpowiedź wydaje się satysfakcjonująca (np. "ok", "dziękuję") i **nie zawiera kolejnego pytania ogólnego**, dodaj na **samym końcu** swojej odpowiedzi (po ewentualnym podziękowaniu) **DOKŁADNIE** znacznik: `{return_marker}`.
    *   Jeśli użytkownik zada kolejne pytanie ogólne, odpowiedz na nie normalnie, bez znacznika powrotu.

**Zasady Dodatkowe:**
*   Prowadź rozmowę płynnie.
*   Bądź cierpliwy i empatyczny.
*   **Jeśli przedmiot nie jest znany, nie przechodź do kroku 2, dopóki go nie ustalisz.**
*   Znacznik `{intent_marker}` jest sygnałem dla systemu, że użytkownik jest gotowy na ustalanie terminu **dla konkretnego, ustalonego przedmiotu**.
*   Znacznik `{return_marker}` służy tylko do powrotu z trybu odpowiedzi na pytanie ogólne zadane podczas innego procesu.
""".format(
    all_subject_links_formatted_for_ai="{all_subject_links_formatted_for_ai}",
    current_subject_from_page="{current_subject_from_page}",
    available_subjects_list=", ".join(AVAILABLE_SUBJECTS),
    intent_marker=INTENT_SCHEDULE_MARKER,
    return_marker=RETURN_TO_PREVIOUS
)

# --- Funkcja AI: Planowanie terminu ---
# Zmodyfikowana, aby przyjmować page_access_token
def get_gemini_scheduling_response(user_psid, history_for_scheduling_ai, current_user_message_text, available_ranges, required_subject, page_access_token):
    """Prowadzi rozmowę planującą z AI dla konkretnego przedmiotu."""
    if not gemini_model:
        logging.error(f"!!! [{user_psid}] Model Gemini niedostępny (Scheduling dla {required_subject})!")
        return None # Zwróć None w przypadku braku modelu
    if not required_subject:
         logging.error(f"!!! [{user_psid}] Wywołano get_gemini_scheduling_response bez wymaganego przedmiotu!")
         return "Przepraszam, wystąpił błąd - nie wiem, dla jakiego przedmiotu szukamy terminu."

    # Sformatuj zakresy dla AI, dodając informację o przedmiocie
    ranges_text = format_ranges_for_ai(available_ranges, subject=required_subject)

    try:
        # Sformatuj instrukcję systemową, wstawiając dynamicznie wymagany przedmiot
        system_instruction = SYSTEM_INSTRUCTION_SCHEDULING.format(
            subject=required_subject, # NOWOŚĆ: przekazanie przedmiotu
            available_ranges_text=ranges_text,
            duration=APPOINTMENT_DURATION_MINUTES,
            min_lead_hours=MIN_BOOKING_LEAD_HOURS,
            calendar_timezone=CALENDAR_TIMEZONE,
            slot_marker_prefix=SLOT_ISO_MARKER_PREFIX,
            slot_marker_suffix=SLOT_ISO_MARKER_SUFFIX,
            switch_marker=SWITCH_TO_GENERAL
        )
    except KeyError as e:
        logging.error(f"!!! BŁĄD formatowania instrukcji (Scheduling dla {required_subject}): Brak klucza {e}")
        return "Błąd konfiguracji asystenta planowania."
    except Exception as format_e:
        logging.error(f"!!! BŁĄD formatowania instrukcji (Scheduling dla {required_subject}): {format_e}")
        return "Błąd wewnętrzny asystenta planowania."

    # Zbuduj prompt dla AI
    initial_prompt = [
        Content(role="user", parts=[Part.from_text(system_instruction)]),
        Content(role="model", parts=[Part.from_text(f"Rozumiem. Będę ustalać termin dla przedmiotu **{required_subject}**. Zapytam o preferencje, zaproponuję termin z dostarczonej listy dla tego przedmiotu, dodam znacznik {SLOT_ISO_MARKER_PREFIX}...{SLOT_ISO_MARKER_SUFFIX} po zgodzie, lub {SWITCH_TO_GENERAL} przy pytaniu ogólnym.")])
    ]
    full_prompt = initial_prompt + history_for_scheduling_ai
    if current_user_message_text:
        # Dodaj bieżącą wiadomość użytkownika do promptu
        full_prompt.append(Content(role="user", parts=[Part.from_text(current_user_message_text)]))

    # Ogranicz długość historii promptu
    max_prompt_messages = (MAX_HISTORY_TURNS * 2) + 2 # +2 dla instrukcji systemowej i potwierdzenia modelu
    # Usuwaj najstarsze pary user/model, zachowując instrukcję systemową (indeksy 0 i 1)
    while len(full_prompt) > max_prompt_messages:
        if len(full_prompt) > 3: # Upewnij się, że są co najmniej 4 elementy (system, model, user, model)
             full_prompt.pop(2) # Usuń starą wiadomość użytkownika (indeks 2)
             if len(full_prompt) > 2: # Sprawdź ponownie, czy jest co usuwać
                 full_prompt.pop(2) # Usuń starą odpowiedź modelu (nowy indeks 2)
        else:
             break # Zabezpieczenie przed nieskończoną pętlą

    # Wywołaj Gemini, przekazując token strony
    response_text = _call_gemini(user_psid, full_prompt, GENERATION_CONFIG_SCHEDULING, f"Scheduling Conversation ({required_subject})", page_access_token)

    # Przetwarzanie odpowiedzi
    if response_text:
        # Oczyść odpowiedź z potencjalnych innych znaczników (chociaż nie powinno ich tu być)
        response_text = response_text.replace(INTENT_SCHEDULE_MARKER, "").strip()
        response_text = response_text.replace(INFO_GATHERED_MARKER, "").strip()
        response_text = response_text.replace(RETURN_TO_PREVIOUS, "").strip()
        return response_text
    else:
        # Jeśli _call_gemini zwróciło None lub pusty string
        logging.error(f"!!! [{user_psid}] Nie uzyskano poprawnej odpowiedzi od Gemini (Scheduling dla {required_subject}).")
        # Zwróć None, aby główna logika mogła obsłużyć błąd
        return None


# --- Funkcja AI: Zbieranie informacji ---
# Zmodyfikowana, aby przyjmować page_access_token
def get_gemini_gathering_response(user_psid, history_for_gathering_ai, current_user_message_text, context_info, page_access_token):
    """Prowadzi rozmowę zbierającą informacje WYŁĄCZNIE o uczniu."""
    if not gemini_model:
        logging.error(f"!!! [{user_psid}] Model Gemini niedostępny (Gathering)!")
        return None

    # Pobierz dane z kontekstu do sformatowania instrukcji
    proposed_slot_str = context_info.get("proposed_slot_formatted", "nie ustalono")
    student_first_name = context_info.get("known_student_first_name", "")
    student_last_name = context_info.get("known_student_last_name", "")
    grade = context_info.get("known_grade", "")
    level = context_info.get("known_level", "")

    try:
        # Użyj predefiniowanej instrukcji (bez zmian związanych z przedmiotem)
        system_instruction = SYSTEM_INSTRUCTION_GATHERING.format(
            proposed_slot_formatted=proposed_slot_str,
            known_student_first_name=student_first_name,
            known_student_last_name=student_last_name,
            known_grade=grade,
            known_level=level
            # info_gathered_marker i switch_marker są już w stringu
        )
    except KeyError as e:
        logging.error(f"!!! BŁĄD formatowania instrukcji (Gathering): Brak klucza {e}")
        return "Błąd konfiguracji asystenta zbierania danych."
    except Exception as format_e:
         logging.error(f"!!! BŁĄD formatowania instrukcji (Gathering): {format_e}")
         return "Błąd wewnętrzny asystenta zbierania danych."

    # Zbuduj prompt
    initial_prompt = [
        Content(role="user", parts=[Part.from_text(system_instruction)]),
        Content(role="model", parts=[Part.from_text(f"Rozumiem. Sprawdzę znane dane ucznia (Imię: {student_first_name or 'brak'}, Nazwisko: {student_last_name or 'brak'}, Klasa: {grade or 'brak'}, Poziom: {level or 'brak'}). Zapytam o brakujące. Zignoruję dane rodzica. Po zebraniu wszystkich danych zwrócę format ZEBRANE_DANE_UCZNIA i znacznik {INFO_GATHERED_MARKER}. Jeśli użytkownik zapyta o coś innego, zwrócę {SWITCH_TO_GENERAL}.")])
    ]
    full_prompt = initial_prompt + history_for_gathering_ai
    if current_user_message_text:
        full_prompt.append(Content(role="user", parts=[Part.from_text(current_user_message_text)]))

    # Ogranicz historię
    max_prompt_messages = (MAX_HISTORY_TURNS * 2) + 2
    while len(full_prompt) > max_prompt_messages:
        if len(full_prompt) > 3:
            full_prompt.pop(2)
            if len(full_prompt) > 2:
                 full_prompt.pop(2)
        else:
             break

    # Wywołaj Gemini, przekazując token strony
    response_text = _call_gemini(user_psid, full_prompt, GENERATION_CONFIG_GATHERING, "Info Gathering (Student Only)", page_access_token)

    # Przetwarzanie odpowiedzi
    if response_text:
        # Oczyść odpowiedź z potencjalnych innych znaczników
        response_text = response_text.replace(INTENT_SCHEDULE_MARKER, "").strip()
        # Usuń potencjalny znacznik ISO slotu, jeśli jakimś cudem się pojawił
        response_text = re.sub(rf"{re.escape(SLOT_ISO_MARKER_PREFIX)}.*?{re.escape(SLOT_ISO_MARKER_SUFFIX)}", "", response_text).strip()
        response_text = response_text.replace(RETURN_TO_PREVIOUS, "").strip()
        return response_text
    else:
        logging.error(f"!!! [{user_psid}] Nie uzyskano poprawnej odpowiedzi od Gemini (Gathering Info).")
        return None

# --- Funkcja AI: Ogólna rozmowa ---
# Zmodyfikowana, aby przyjmować page_access_token
def get_gemini_general_response(user_psid, current_user_message_text, history_for_general_ai, is_temporary_general_state, page_access_token, current_subject=None):
    """Prowadzi ogólną rozmowę z AI."""
    if not gemini_model:
        logging.error(f"!!! [{user_psid}] Model Gemini niedostępny (General)!")
        return None

    # Użyj predefiniowanej instrukcji (już sformatowanej z listą przedmiotów)
    system_instruction = SYSTEM_INSTRUCTION_GENERAL
    model_ack_base = "Rozumiem. Będę asystentem klienta."
    if current_subject:
         model_ack_base += f" Przedmiot '{current_subject}' jest już znany. Przejdę do pytania o klasę/szkołę."
    else:
        model_ack_base += f" Najpierw ustalę przedmiot ({', '.join(AVAILABLE_SUBJECTS)})."
    model_ack = model_ack_base + f" Po zebraniu danych i potwierdzeniu zainteresowania, zwrócę {INTENT_SCHEDULE_MARKER}."

    if is_temporary_general_state:
        model_ack += f" Będąc w trybie tymczasowym, po odpowiedzi na pytanie ogólne, jeśli user nie pyta dalej, dodam {RETURN_TO_PREVIOUS}."

    # Zbuduj prompt
    initial_prompt = [
        Content(role="user", parts=[Part.from_text(system_instruction)]),
        Content(role="model", parts=[Part.from_text(model_ack)])
    ]
    full_prompt = initial_prompt + history_for_general_ai
    if current_user_message_text:
        full_prompt.append(Content(role="user", parts=[Part.from_text(current_user_message_text)]))

    # Ogranicz historię
    max_prompt_messages = (MAX_HISTORY_TURNS * 2) + 2
    while len(full_prompt) > max_prompt_messages:
         if len(full_prompt) > 3:
             full_prompt.pop(2)
             if len(full_prompt) > 2:
                 full_prompt.pop(2)
         else:
             break

    # Wywołaj Gemini, przekazując token strony
    response_text = _call_gemini(user_psid, full_prompt, GENERATION_CONFIG_DEFAULT, "General Conversation", page_access_token)

    # Przetwarzanie odpowiedzi
    if response_text:
        # Oczyść z potencjalnych innych znaczników
        response_text = re.sub(rf"{re.escape(SLOT_ISO_MARKER_PREFIX)}.*?{re.escape(SLOT_ISO_MARKER_SUFFIX)}", "", response_text).strip()
        response_text = response_text.replace(INFO_GATHERED_MARKER, "").strip()
        response_text = response_text.replace(SWITCH_TO_GENERAL, "").strip() # SWITCH_TO_GENERAL nie powinien tu być, ale na wszelki wypadek
        return response_text
    else:
        logging.error(f"!!! [{user_psid}] Nie uzyskano poprawnej odpowiedzi od Gemini (General).")
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
    # TUTAJ WAŻNE: Weryfikacja może przyjść z dowolnej skonfigurowanej strony.
    # Sprawdź, czy otrzymany token pasuje do *dowolnego* tokenu weryfikacyjnego
    # (jeśli masz różne dla różnych stron, inaczej użyj jednego globalnego jak teraz)
    if hub_mode == 'subscribe' and hub_token == VERIFY_TOKEN:
        logging.info("Weryfikacja GET OK!")
        return Response(hub_challenge, status=200)
    else:
        logging.warning(f"Weryfikacja GET NIEUDANA. Oczekiwany token: '{VERIFY_TOKEN}', Otrzymany: '{hub_token}'")
        return Response("Verification failed", status=403)


# Nie ma potrzeby zmiany tej funkcji
def find_row_and_update_sheet(psid, start_time, student_data, sheet_row_index=None):
    """Znajduje wiersz (jeśli nie podano) i aktualizuje dane Fazy 2 w Arkusz1."""
    if sheet_row_index is None:
        logging.warning(f"[{psid}] Aktualizacja Fazy 2 bez indeksu wiersza. Próba znalezienia w '{MAIN_SHEET_NAME}'...")
        sheet_row_index = find_row_by_psid(psid) # find_row_by_psid szuka od wiersza 2
        if sheet_row_index is None:
            logging.error(f"[{psid}] Nie znaleziono wiersza dla PSID w '{MAIN_SHEET_NAME}' do aktualizacji Fazy 2.")
            # Zwróć błąd, aby proces mógł odpowiednio zareagować
            return False, "Nie znaleziono powiązanego wpisu w arkuszu do aktualizacji."
        else:
            logging.info(f"[{psid}] Znaleziono wiersz {sheet_row_index} dla PSID w '{MAIN_SHEET_NAME}' do aktualizacji Fazy 2.")
    # Sprawdź ponownie, czy sheet_row_index jest prawidłowy po potencjalnym znalezieniu
    if sheet_row_index is None or not isinstance(sheet_row_index, int) or sheet_row_index < 2:
         logging.error(f"[{psid}] Nieprawidłowy indeks wiersza ({sheet_row_index}) przekazany do update_sheet_phase2.")
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
                page_id = entry.get("id") # ID strony, która otrzymała zdarzenie
                for event in entry.get("messaging", []):
                    sender_id = event.get("sender", {}).get("id")
                    recipient_id = event.get("recipient", {}).get("id") # ID strony, która otrzymała wiadomość

                    if not sender_id:
                        logging.warning("Pominięto zdarzenie bez sender.id.")
                        continue
                    if not recipient_id:
                        logging.warning(f"Pominięto zdarzenie dla sender {sender_id} bez recipient.id.")
                        continue

                    # --- Pobierz konfigurację strony ---
                    page_info = PAGE_CONFIG.get(recipient_id)
                    if not page_info:
                        logging.error(f"!!! Otrzymano wiadomość dla nieznanej/nieskonfigurowanej strony ID: {recipient_id} (Sender: {sender_id}). Pomijam.")
                        continue # Ignoruj wiadomości do stron, których nie ma w konfiguracji

                    current_page_token = page_info['token']
                    current_subject = page_info['subject']
                    current_page_name = page_info['name']
                    logging.info(f"--- Przetwarzanie zdarzenia dla Strony: '{current_page_name}' ({recipient_id}) | Przedmiot: {current_subject} | Sender PSID: {sender_id} ---")

                    # Sprawdź poprawność tokenu dla strony
                    if not current_page_token or len(current_page_token) < 50:
                         logging.error(f"!!! KRYTYCZNY BŁĄD: Brak lub nieprawidłowy token dostępu skonfigurowany dla strony '{current_page_name}' ({recipient_id}). Nie można kontynuować.")
                         continue # Nie można nic zrobić bez tokenu

                    history, context, is_new_contact = load_history(sender_id)
                    # Filtruj historię do formatu Content dla Gemini
                    history_for_gemini = [h for h in history if isinstance(h, Content) and h.role in ('user', 'model')]
                    current_state = context.get('type', STATE_GENERAL)

                    # *** Logika statystyk: Nowy kontakt ***
                    if is_new_contact:
                        logging.info(f"[{sender_id}] Wykryto nowy kontakt dla strony '{current_page_name}'. Logowanie statystyki.")
                        log_statistic("new_contact")

                    logging.info(f"    Aktualny stan: {current_state}")
                    # Loguj tylko klucze kontekstu dla zwięzłości
                    logging.debug(f"    Kontekst wejściowy (klucze): {list(context.keys())}")


                    action = None
                    msg_result = None
                    ai_response_text_raw = None
                    next_state = current_state
                    model_resp_content = None # Odpowiedź AI jako obiekt Content
                    user_content = None       # Wiadomość użytkownika jako obiekt Content
                    # Zawsze zaczynaj od kopii aktualnego kontekstu
                    context_data_to_save = context.copy()
                    # Usuń potencjalne tymczasowe flagi powrotu na początku przetwarzania
                    context_data_to_save.pop('return_to_state', None)
                    context_data_to_save.pop('return_to_context', None)

                    # Zapisz aktualny przedmiot w kontekście, jeśli go tam nie ma lub się różni
                    # (na wypadek zmiany kontekstu lub gdy stan jest General)
                    if context_data_to_save.get('required_subject') != current_subject:
                         context_data_to_save['required_subject'] = current_subject
                         logging.debug(f"    Zaktualizowano/ustawiono 'required_subject' w kontekście na: {current_subject}")


                    trigger_gathering_ai_immediately = False
                    slot_verification_failed = False # Flaga do obsługi zajętych slotów
                    is_temporary_general_state = 'return_to_state' in context # Sprawdź ORYGINALNY kontekst

                    # === Obsługa wiadomości / postbacków ===
                    if message_data := event.get("message"):
                        if message_data.get("is_echo"):
                            logging.debug(f"    Pominięto echo wiadomości (PSID: {sender_id}, Strona: {current_page_name}).")
                            continue # Ignoruj echa wysłane przez bota
                        user_input_text = message_data.get("text", "").strip()
                        if user_input_text:
                            user_content = Content(role="user", parts=[Part.from_text(user_input_text)])
                            log_msg = f"'{user_input_text[:100]}{'...' if len(user_input_text)>100 else ''}'"
                            logging.info(f"    Odebrano wiadomość tekstową (stan={current_state}): {log_msg}")
                            # Krótka pauza symulująca czytanie
                            if ENABLE_TYPING_DELAY:
                                time.sleep(MIN_TYPING_DELAY_SECONDS * 0.3)

                            # Przypisz akcję na podstawie bieżącego stanu
                            if current_state == STATE_SCHEDULING_ACTIVE:
                                action = 'handle_scheduling'
                            elif current_state == STATE_GATHERING_INFO:
                                action = 'handle_gathering'
                            else: # Domyślnie stan GENERAL
                                action = 'handle_general'
                        elif attachments := message_data.get("attachments"):
                            att_type = attachments[0].get('type', 'nieznany')
                            logging.info(f"      Otrzymano załącznik typu: {att_type}.")
                            # Traktuj załącznik jako wiadomość użytkownika dla historii
                            user_content = Content(role="user", parts=[Part.from_text(f"[Użytkownik wysłał załącznik typu: {att_type}]")])
                            # Prosta odpowiedź informacyjna
                            msg_result = "Dziękuję za przesłanie, ale obecnie mogę przetwarzać tylko wiadomości tekstowe."
                            action = 'send_info' # Wyślij tylko informację
                            next_state = current_state # Pozostań w bieżącym stanie
                        else:
                            logging.info("      Odebrano pustą wiadomość lub nieobsługiwany typ (np. udostępnienie lokalizacji).")
                            action = None # Ignoruj

                    elif postback := event.get("postback"):
                        payload = postback.get("payload")
                        title = postback.get("title", "") # Tytuł przycisku
                        logging.info(f"    Odebrano postback: Payload='{payload}', Tytuł='{title}' (stan={current_state})")
                        # Traktuj postback jako wiadomość użytkownika dla historii
                        user_input_text = f"Użytkownik kliknął przycisk: '{title}' (Payload: {payload})"
                        user_content = Content(role="user", parts=[Part.from_text(user_input_text)])

                        # Specjalna obsługa payloadów sterujących
                        if payload == "CANCEL_SCHEDULING":
                             msg_result = "Rozumiem, proces umawiania został anulowany. Czy mogę pomóc w czymś innym?"
                             action = 'send_info'
                             next_state = STATE_GENERAL # Wróć do stanu ogólnego
                             context_data_to_save = {'type': STATE_GENERAL, 'required_subject': current_subject} # Resetuj kontekst, zachowaj przedmiot
                        # Można dodać inne payloady np. "CHANGE_SUBJECT" itp.
                        # Domyślnie przekaż do obsługi stanu
                        elif current_state == STATE_SCHEDULING_ACTIVE:
                            action = 'handle_scheduling'
                        elif current_state == STATE_GATHERING_INFO:
                            action = 'handle_gathering'
                        else:
                            action = 'handle_general'

                    elif event.get("read"):
                        logging.debug(f"    Otrzymano potwierdzenie odczytania przez użytkownika {sender_id} (Strona: {current_page_name}).")
                        continue # Nic nie rób
                    elif event.get("delivery"):
                        logging.debug(f"    Otrzymano potwierdzenie dostarczenia wiadomości do {sender_id} (Strona: {current_page_name}).")
                        continue # Nic nie rób
                    else:
                        logging.warning(f"    Otrzymano nieobsługiwany typ zdarzenia od {sender_id} dla strony {current_page_name}: {json.dumps(event)}")
                        continue # Ignoruj nieznane zdarzenia

                    # --- Pętla przetwarzania akcji (głównie do obsługi przejść między stanami bez odpowiedzi użytkownika) ---
                    loop_guard = 0
                    max_loops = 3 # Zabezpieczenie przed nieskończoną pętlą stanów
                    while action and loop_guard < max_loops:
                        loop_guard += 1
                        logging.debug(f"  >> Pętla przetwarzania {loop_guard}/{max_loops} | Akcja: {action} | Stan wejściowy: {current_state} | Stan wyjściowy (oczekiwany): {next_state} | Przedmiot: {current_subject}")
                        current_action = action
                        action = None # Resetuj akcję na początku każdej iteracji pętli

                        # --- Stan Generalny ---
                        if current_action == 'handle_general':
                            logging.debug("  >> Wykonanie: handle_general")
                            # W tym stanie, przedmiot jest znany z kontekstu strony (current_subject)
                            # Modyfikujemy logikę: nie wywołujemy AI od razu, jeśli to początek rozmowy w General
                            # Wysyłamy wiadomość powitalną i czekamy na odpowiedź

                            # Czy to jest PIERWSZE wejście do stanu General dla tej rozmowy (lub powrót np. z anulowania)?
                            # Sprawdzamy, czy stan się właśnie zmienił na General LUB czy historia jest pusta
                            is_initial_general_entry = (current_state != STATE_GENERAL) or (not history_for_gemini) or (context_data_to_save.get('_just_reset', False))
                            context_data_to_save.pop('_just_reset', None) # Usuń flagę

                            # Czy użytkownik coś napisał TERAZ?
                            user_message_text = user_content.parts[0].text if user_content and user_content.parts else None

                            if is_initial_general_entry and not user_message_text:
                                # Pierwsze wejście bez wiadomości od użytkownika (np. kliknięcie 'Get Started' lub reset stanu)
                                logging.info(f"      Pierwsze wejście do General dla {current_subject} (bez wiadomości usera). Wysyłam powitanie.")
                                # Wygeneruj linki do innych przedmiotów
                                other_subjects_links = []
                                for subj, link in ALL_SUBJECT_LINKS.items():
                                    if subj != current_subject: # Nie linkuj do bieżącego
                                        other_subjects_links.append(f"{subj}: {link}")
                                links_text = "\n\nUdzielamy również korepetycji z innych przedmiotów:\n" + "\n".join(other_subjects_links) if other_subjects_links else ""

                                msg_result = f"Dzień dobry! Dziękujemy za kontakt w sprawie korepetycji z przedmiotu **{current_subject}**. W czym mogę pomóc? Jeśli chcą Państwo umówić termin, proszę dać znać - zapytam wtedy o klasę ucznia i poziom nauczania." + links_text
                                model_resp_content = Content(role="model", parts=[Part.from_text(msg_result)])
                                next_state = STATE_GENERAL
                                context_data_to_save['type'] = STATE_GENERAL
                                action = 'send_info' # Ustaw akcję wysłania wiadomości
                                current_state = next_state
                                continue # Kontynuuj pętlę, aby wykonać send_info
                            elif user_message_text:
                                # Użytkownik coś napisał w stanie General
                                logging.info(f"      Obsługa wiadomości usera w stanie General dla {current_subject}.")
                                was_temporary = 'return_to_state' in context # Sprawdź oryginalny kontekst

                                # Wywołaj AI ogólne (przekazując, że przedmiot jest już znany)
                                ai_response_text_raw = get_gemini_general_response(
                                     sender_id,
                                     user_message_text,
                                     history_for_gemini,
                                     was_temporary,
                                     current_page_token, # Przekaż token
                                     current_subject # Przekaż znany przedmiot
                                )

                                if ai_response_text_raw:
                                    # Zapisz pełną odpowiedź AI do historii
                                    model_resp_content = Content(role="model", parts=[Part.from_text(ai_response_text_raw)])

                                    # --- Analiza odpowiedzi AI General ---
                                    # Sprawdź, czy AI chce wrócić do poprzedniego stanu
                                    if RETURN_TO_PREVIOUS in ai_response_text_raw and was_temporary:
                                        logging.info(f"      AI Ogólne (tymczasowe) -> Wykryto znacznik powrotu [{RETURN_TO_PREVIOUS}].")
                                        msg_result = ai_response_text_raw.split(RETURN_TO_PREVIOUS, 1)[0].strip()
                                        next_state = context.get('return_to_state', STATE_GENERAL)
                                        context_data_to_save = context.get('return_to_context', {}).copy()
                                        context_data_to_save['type'] = next_state
                                        logging.info(f"      Przywracam stan: {next_state}. Kontekst przywrócony (klucze): {list(context_data_to_save.keys())}")

                                        # Ustaw kolejną akcję
                                        user_content_for_next_action = None # Powrót nie ma wiadomości usera
                                        if next_state == STATE_SCHEDULING_ACTIVE:
                                             action = 'handle_scheduling'
                                        elif next_state == STATE_GATHERING_INFO:
                                             action = 'handle_gathering'
                                             trigger_gathering_ai_immediately = True
                                        else:
                                             next_state = STATE_GENERAL
                                             context_data_to_save = {'type': STATE_GENERAL, 'required_subject': current_subject}
                                             action = None
                                        if action:
                                             logging.debug(f"      Ustawiono kolejną akcję po powrocie: {action}")
                                             current_state = next_state
                                             continue

                                    # Sprawdź, czy AI wykryło intencję umówienia wizyty
                                    elif INTENT_SCHEDULE_MARKER in ai_response_text_raw:
                                        logging.info(f"      AI Ogólne -> Wykryto intencję planowania [{INTENT_SCHEDULE_MARKER}] dla przedmiotu {current_subject}.")
                                        msg_result = ai_response_text_raw.split(INTENT_SCHEDULE_MARKER, 1)[0].strip()

                                        logging.info(f"      Przechodzę do planowania dla przedmiotu: {current_subject}.")
                                        next_state = STATE_SCHEDULING_ACTIVE
                                        action = 'handle_scheduling' # Ustaw kolejną akcję
                                        # Przygotuj nowy kontekst dla stanu Scheduling
                                        context_data_to_save = {'type': STATE_SCHEDULING_ACTIVE, 'required_subject': current_subject}
                                        logging.debug(f"      Nowy kontekst dla Scheduling: {context_data_to_save}")
                                        # Kontynuuj pętlę, aby wykonać handle_scheduling
                                        current_state = next_state # Aktualizuj stan wejściowy
                                        continue
                                    else: # Normalna odpowiedź AI General (bez znaczników)
                                        logging.info(f"      AI Ogólne -> Standardowa odpowiedź dla {current_subject}.")
                                        msg_result = ai_response_text_raw
                                        next_state = STATE_GENERAL # Pozostań w stanie General
                                        # Jeśli byliśmy w stanie tymczasowym, zachowaj flagi powrotu
                                        if was_temporary:
                                             context_data_to_save['return_to_state'] = context['return_to_state']
                                             context_data_to_save['return_to_context'] = context.get('return_to_context', {})
                                             context_data_to_save['type'] = STATE_GENERAL # Ustaw typ na General
                                             context_data_to_save['required_subject'] = current_subject # Upewnij się, że przedmiot jest
                                             logging.debug("      Zachowuję flagi powrotu w stanie General (tymczasowym).")
                                        else:
                                            # Zawsze zachowaj przedmiot w kontekście General
                                            context_data_to_save = {'type': STATE_GENERAL, 'required_subject': current_subject}

                                        action = None # Zakończ pętlę, wyślij odpowiedź
                                        # break # Przerwij pętlę while action
                                else: # Błąd wywołania AI General
                                    msg_result = "Przepraszam, mam chwilowy problem z przetworzeniem Twojej wiadomości. Spróbuj ponownie za moment."
                                    model_resp_content = Content(role="model", parts=[Part.from_text(msg_result)]) # Zapisz info o błędzie
                                    next_state = STATE_GENERAL # Pozostań w General
                                    context_data_to_save = {'type': STATE_GENERAL, 'required_subject': current_subject} # Zachowaj przedmiot
                                    action = None # Zakończ pętlę
                            else:
                                # To się może zdarzyć, jeśli użytkownik np. tylko polubił wiadomość
                                logging.debug("Wywołano 'handle_general' bez nowej treści wiadomości użytkownika (user_content). Pozostaję w stanie General.")
                                action = None # Zakończ pętlę, nic nie rób

                        # --- Stan Planowania ---
                        elif current_action == 'handle_scheduling':
                            logging.debug("  >> Wykonanie: handle_scheduling")
                            # Przedmiot powinien być już w `current_subject`
                            if not current_subject:
                                logging.error(f"!!! KRYTYCZNY BŁĄD: Wejście do stanu SCHEDULING bez 'current_subject'! PSID: {sender_id}, Strona: {current_page_name}")
                                msg_result = f"Przepraszam, wystąpił błąd wewnętrzny. Proszę spróbować ponownie."
                                model_resp_content = Content(role="model", parts=[Part.from_text(msg_result)])
                                next_state = STATE_GENERAL # Wróć do General
                                context_data_to_save = {'type': STATE_GENERAL, 'required_subject': PAGE_CONFIG.get(recipient_id, {}).get('subject', 'Nieznany')} # Spróbuj odzyskać
                                action = 'send_info'
                                current_state = next_state
                                continue

                            # Pobierz listę konfiguracji kalendarzy DLA wymaganego przedmiotu
                            subject_calendars_config = SUBJECT_TO_CALENDARS.get(current_subject.lower(), [])

                            if not subject_calendars_config:
                                logging.error(f"!!! BŁĄD KONFIGURACJI: Brak kalendarzy zdefiniowanych dla przedmiotu '{current_subject}'! PSID: {sender_id}, Strona: {current_page_name}")
                                msg_result = f"Przepraszam, wygląda na to, że obecnie nie mamy dostępnych kalendarzy dla przedmiotu '{current_subject}'. Skontaktuj się z nami bezpośrednio, aby omówić możliwości."
                                model_resp_content = Content(role="model", parts=[Part.from_text(msg_result)])
                                next_state = STATE_GENERAL
                                context_data_to_save = {'type': STATE_GENERAL, 'required_subject': current_subject}
                                action = 'send_info'
                                current_state = next_state
                                continue

                            try:
                                tz = _get_calendar_timezone()
                                now = datetime.datetime.now(tz)
                                search_start_base = now # Zawsze zaczynaj od teraz
                                search_end_date = (search_start_base + datetime.timedelta(days=MAX_SEARCH_DAYS)).date()
                                search_end = tz.localize(datetime.datetime.combine(search_end_date, datetime.time(WORK_END_HOUR, 0))) # Koniec ostatniego dnia roboczego

                                # Informacja dla użytkownika, że szukamy
                                logging.info(f"      Pobieranie wolnych zakresów dla przedmiotu '{current_subject}' (Kalendarze: {[c['name'] for c in subject_calendars_config]})")
                                # Dłuższa symulacja pisania przy szukaniu w kalendarzu
                                _simulate_typing(sender_id, random.uniform(MIN_TYPING_DELAY_SECONDS, MAX_TYPING_DELAY_SECONDS * 0.8), current_page_token)

                                # Pobierz wolne zakresy TYLKO z kalendarzy dla danego przedmiotu
                                free_ranges = get_free_time_ranges(subject_calendars_config, search_start_base, search_end)

                                if free_ranges:
                                    logging.info(f"      Znaleziono {len(free_ranges)} łącznych wolnych zakresów dla '{current_subject}'. Wywołanie AI Planującego...")

                                    current_input_text = None
                                    if user_content and user_content.parts: # Tylko jeśli user coś napisał w tej turze
                                         current_input_text = user_content.parts[0].text

                                    if slot_verification_failed:
                                        fail_info = f"\n[Informacja dla Ciebie: Poprzednio proponowany termin okazał się w międzyczasie zajęty. Zaproponuj proszę inny dostępny termin z listy dla {current_subject}.]"
                                        current_input_text = (current_input_text + fail_info) if current_input_text else fail_info
                                        slot_verification_failed = False # Resetuj flagę

                                    # Wywołaj AI planujące dla danego przedmiotu
                                    ai_response_text_raw = get_gemini_scheduling_response(
                                        user_psid=sender_id,
                                        history_for_scheduling_ai=history_for_gemini,
                                        current_user_message_text=current_input_text,
                                        available_ranges=free_ranges,
                                        required_subject=current_subject, # Przekaż przedmiot
                                        page_access_token=current_page_token # Przekaż token
                                    )

                                    if ai_response_text_raw:
                                        # Reszta logiki handle_scheduling (weryfikacja, zapis Fazy 1, przejście do Gathering)
                                        # pozostaje taka sama jak w poprzedniej wersji,
                                        # tylko używa `current_subject` i `current_page_token`
                                        # Wklejam tę część poniżej, upewniając się, że używa poprawnych zmiennych

                                        model_resp_content = Content(role="model", parts=[Part.from_text(ai_response_text_raw)])

                                        if ai_response_text_raw.strip() == SWITCH_TO_GENERAL:
                                            logging.info(f"      AI Planujące ({current_subject}) -> Wykryto prośbę o przełączenie na tryb ogólny [{SWITCH_TO_GENERAL}].")
                                            context_data_to_save['return_to_state'] = STATE_SCHEDULING_ACTIVE
                                            context_data_to_save['return_to_context'] = {'required_subject': current_subject} # Zapisz tylko przedmiot
                                            context_data_to_save['type'] = STATE_GENERAL
                                            next_state = STATE_GENERAL
                                            action = 'handle_general'
                                            msg_result = None
                                            logging.debug(f"      Zapisano stan powrotu. Nowy stan: {next_state}. Kontekst zapisu: {context_data_to_save}")
                                            current_state = next_state
                                            continue

                                        iso_match = re.search(rf"{re.escape(SLOT_ISO_MARKER_PREFIX)}(.*?){re.escape(SLOT_ISO_MARKER_SUFFIX)}", ai_response_text_raw)
                                        if iso_match:
                                            extracted_iso = iso_match.group(1).strip()
                                            logging.info(f"      AI Planujące ({current_subject}) zwróciło potwierdzony slot ISO: {extracted_iso}")
                                            text_for_user = re.sub(rf"{re.escape(SLOT_ISO_MARKER_PREFIX)}.*?{re.escape(SLOT_ISO_MARKER_SUFFIX)}", "", ai_response_text_raw).strip()
                                            text_for_user = re.sub(r'\s+', ' ', text_for_user).strip()

                                            try:
                                                proposed_start = datetime.datetime.fromisoformat(extracted_iso)
                                                tz_cal = _get_calendar_timezone()
                                                if proposed_start.tzinfo is None: proposed_start = tz_cal.localize(proposed_start)
                                                else: proposed_start = proposed_start.astimezone(tz_cal)

                                                proposed_slot_formatted = format_slot_for_user(proposed_start)
                                                logging.info(f"      Rozpoczynam weryfikację dostępności slotu: {proposed_slot_formatted} ({current_subject}) w kalendarzach i arkuszu '{MAIN_SHEET_NAME}'...")
                                                _simulate_typing(sender_id, MIN_TYPING_DELAY_SECONDS * 0.6, current_page_token)

                                                chosen_calendar_id = None
                                                chosen_calendar_name = None
                                                sheet_blocks_slot = False
                                                min_duration_delta = datetime.timedelta(minutes=APPOINTMENT_DURATION_MINUTES)
                                                proposed_end = proposed_start + min_duration_delta

                                                logging.debug(f"      Weryfikacja w arkuszu '{MAIN_SHEET_NAME}' dla slotu {proposed_start:%H:%M}-{proposed_end:%H:%M}")
                                                potential_sheet_blockers = get_sheet_booked_slots(SPREADSHEET_ID, MAIN_SHEET_NAME, proposed_start, proposed_end)
                                                for blocker in potential_sheet_blockers:
                                                    if max(proposed_start, blocker['start']) < min(proposed_end, blocker['end']):
                                                         logging.warning(f"      Weryfikacja: Slot {proposed_slot_formatted} ZAJĘTY w ARKUSZU '{MAIN_SHEET_NAME}' przez rezerwację '{blocker.get('calendar_name', '?')}' ({blocker['start']:%H:%M}-{blocker['end']:%H:%M}).")
                                                         sheet_blocks_slot = True
                                                         break

                                                if not sheet_blocks_slot:
                                                    logging.debug(f"      Slot nie zablokowany w arkuszu '{MAIN_SHEET_NAME}'. Weryfikacja w Google Calendar (dla {current_subject})...")
                                                    for cal_config in subject_calendars_config:
                                                        cal_id = cal_config['id']
                                                        cal_name = cal_config['name']
                                                        if is_slot_actually_free(proposed_start, cal_id):
                                                            chosen_calendar_id = cal_id
                                                            chosen_calendar_name = cal_name
                                                            logging.info(f"      Slot {proposed_slot_formatted} POTWIERDZONY jako wolny w kalendarzu '{cal_name}' ({cal_id}).")
                                                            break

                                                if chosen_calendar_id:
                                                    logging.info(f"      Wybrano kalendarz '{chosen_calendar_name}' ({chosen_calendar_id}) dla rezerwacji. Rozpoczynam zapis Fazy 1 do '{MAIN_SHEET_NAME}'...")
                                                    write_ok, write_msg_or_row = write_to_sheet_phase1(sender_id, proposed_start, chosen_calendar_name)

                                                    if write_ok:
                                                        sheet_row_idx = write_msg_or_row if isinstance(write_msg_or_row, int) else None
                                                        parent_profile = get_user_profile(sender_id, current_page_token) # Użyj tokenu strony
                                                        parent_fn = parent_profile.get('first_name', '') if parent_profile else ''
                                                        parent_ln = parent_profile.get('last_name', '') if parent_profile else ''

                                                        confirm_msg = text_for_user if text_for_user else f"Potwierdzam rezerwację terminu {proposed_slot_formatted} na {current_subject}."
                                                        confirm_msg += " Aby dokończyć rezerwację, poproszę teraz o kilka informacji dotyczących ucznia."
                                                        msg_result = confirm_msg
                                                        model_resp_content = Content(role="model", parts=[Part.from_text(confirm_msg)])

                                                        next_state = STATE_GATHERING_INFO
                                                        context_data_to_save = {
                                                            'type': STATE_GATHERING_INFO,
                                                            'proposed_slot_iso': proposed_start.isoformat(),
                                                            'proposed_slot_formatted': proposed_slot_formatted,
                                                            'chosen_calendar_id': chosen_calendar_id,
                                                            'chosen_calendar_name': chosen_calendar_name,
                                                            'required_subject': current_subject, # Zachowaj przedmiot
                                                            'known_parent_first_name': parent_fn,
                                                            'known_parent_last_name': parent_ln,
                                                            'known_student_first_name': '',
                                                            'known_student_last_name': '',
                                                            'known_grade': '',
                                                            'known_level': '',
                                                            'sheet_row_index': sheet_row_idx
                                                        }
                                                        action = 'handle_gathering'
                                                        trigger_gathering_ai_immediately = True
                                                        logging.debug(f"      Przejście do {next_state}. Akcja: {action}. Trigger AI: {trigger_gathering_ai_immediately}. Kontekst: {list(context_data_to_save.keys())}")
                                                        current_state = next_state
                                                        continue

                                                    else: # Błąd zapisu Fazy 1
                                                        logging.error(f"Błąd zapisu Fazy 1 do arkusza '{MAIN_SHEET_NAME}': {write_msg_or_row}. PSID: {sender_id}")
                                                        msg_result = f"Przepraszam, wystąpił błąd techniczny podczas próby rezerwacji terminu ({write_msg_or_row}). Proszę spróbować ponownie za chwilę lub skontaktować się z nami bezpośrednio."
                                                        model_resp_content = Content(role="model", parts=[Part.from_text(msg_result)])
                                                        next_state = STATE_GENERAL
                                                        context_data_to_save = {'type': STATE_GENERAL, 'required_subject': current_subject, '_just_reset': True}
                                                        action = None
                                                        # break

                                                else: # Slot zajęty
                                                    blocker_source = f"w arkuszu '{MAIN_SHEET_NAME}'" if sheet_blocks_slot else f'w kalendarzu Google dla {current_subject}'
                                                    logging.warning(f"      Weryfikacja NIEUDANA! Slot {extracted_iso} ({proposed_slot_formatted}) okazał się zajęty {blocker_source}. PSID: {sender_id}")
                                                    fail_msg = f"Ojej, wygląda na to, że termin {proposed_slot_formatted} został w międzyczasie zarezerwowany. Spróbujmy znaleźć inny pasujący termin dla {current_subject}."
                                                    msg_result = fail_msg
                                                    fail_info_for_ai = f"\n[System: Termin {proposed_slot_formatted} okazał się zajęty. Zaproponuj inny dostępny termin z listy dla {current_subject}.]"
                                                    model_resp_content = Content(role="model", parts=[Part.from_text(ai_response_text_raw + fail_info_for_ai)])
                                                    next_state = STATE_SCHEDULING_ACTIVE
                                                    slot_verification_failed = True
                                                    context_data_to_save['type'] = STATE_SCHEDULING_ACTIVE
                                                    context_data_to_save['required_subject'] = current_subject # Upewnij się, że jest
                                                    action = None
                                                    # break

                                            except ValueError:
                                                logging.error(f"!!! BŁĄD: AI ({current_subject}) zwróciło nieprawidłowy format ISO w znaczniku: '{extracted_iso}'. PSID: {sender_id}")
                                                msg_result = "Przepraszam, wystąpił błąd techniczny podczas przetwarzania wybranego terminu. Spróbujmy wybrać termin jeszcze raz."
                                                model_resp_content = Content(role="model", parts=[Part.from_text(msg_result)])
                                                next_state = STATE_SCHEDULING_ACTIVE
                                                context_data_to_save['type'] = STATE_SCHEDULING_ACTIVE
                                                context_data_to_save['required_subject'] = current_subject
                                                action = None
                                                # break
                                            except Exception as verif_err:
                                                logging.error(f"!!! KRYTYCZNY BŁĄD podczas weryfikacji/zapisu slotu {extracted_iso} ({current_subject}): {verif_err}", exc_info=True)
                                                msg_result = "Przepraszam, wystąpił nieoczekiwany błąd podczas finalizowania rezerwacji. Proszę spróbować ponownie później."
                                                model_resp_content = Content(role="model", parts=[Part.from_text(msg_result)])
                                                next_state = STATE_GENERAL
                                                context_data_to_save = {'type': STATE_GENERAL, 'required_subject': current_subject, '_just_reset': True}
                                                action = None
                                                # break
                                        else: # AI kontynuuje rozmowę
                                            logging.info(f"      AI Planujące ({current_subject}) kontynuuje rozmowę (brak znacznika ISO/SWITCH).")
                                            msg_result = ai_response_text_raw
                                            next_state = STATE_SCHEDULING_ACTIVE
                                            context_data_to_save['type'] = STATE_SCHEDULING_ACTIVE
                                            context_data_to_save['required_subject'] = current_subject # Upewnij się
                                            action = None
                                            # break

                                    else: # Błąd AI Scheduling
                                        logging.error(f"!!! BŁĄD: AI Planujące ({current_subject}) nie zwróciło odpowiedzi. PSID: {sender_id}")
                                        msg_result = f"Przepraszam, wystąpił problem z systemem planowania dla przedmiotu {current_subject}. Spróbuj ponownie za chwilę."
                                        model_resp_content = Content(role="model", parts=[Part.from_text(msg_result)])
                                        next_state = STATE_GENERAL
                                        context_data_to_save = {'type': STATE_GENERAL, 'required_subject': current_subject, '_just_reset': True}
                                        action = None
                                        # break
                                else: # Brak wolnych zakresów
                                    logging.warning(f"      Brak wolnych zakresów (po filtrach) dla przedmiotu '{current_subject}'. PSID: {sender_id}")
                                    no_slots_msg = f"Niestety, wygląda na to, że w tej chwili brak wolnych terminów na korepetycje z przedmiotu **{current_subject}** w najbliższym okresie ({MAX_SEARCH_DAYS} dni, z wyprzedzeniem {MIN_BOOKING_LEAD_HOURS}h). Proszę spróbować ponownie później lub zapytać o inny przedmiot."
                                    msg_result = no_slots_msg
                                    model_resp_content = Content(role="model", parts=[Part.from_text(no_slots_msg)])
                                    next_state = STATE_GENERAL
                                    context_data_to_save = {'type': STATE_GENERAL, 'required_subject': current_subject, '_just_reset': True}
                                    action = None
                                    # break
                            except Exception as schedule_err:
                                logging.error(f"!!! KRYTYCZNY BŁĄD w logice 'handle_scheduling' dla {current_subject}: {schedule_err}", exc_info=True)
                                msg_result = "Przepraszam, wystąpił nieoczekiwany błąd systemu podczas planowania terminu. Proszę spróbować ponownie później."
                                model_resp_content = Content(role="model", parts=[Part.from_text(msg_result)])
                                next_state = STATE_GENERAL
                                context_data_to_save = {'type': STATE_GENERAL, 'required_subject': current_subject, '_just_reset': True}
                                action = None
                                # break

                        # --- Stan Zbierania Informacji ---
                        elif current_action == 'handle_gathering':
                            logging.debug("  >> Wykonanie: handle_gathering")
                            try:
                                # Przedmiot powinien być w kontekście `context_data_to_save['required_subject']`
                                known_info_for_ai = context_data_to_save.copy()
                                logging.debug(f"    Kontekst przekazywany do AI (Gathering): {list(known_info_for_ai.keys())}")

                                current_input_text = None
                                if trigger_gathering_ai_immediately:
                                    logging.info("      Inicjuję AI zbierające dane (pierwsze wywołanie w tym stanie).")
                                    trigger_gathering_ai_immediately = False
                                elif user_content and user_content.parts:
                                    current_input_text = user_content.parts[0].text

                                # Wywołaj AI zbierające dane
                                ai_response_text_raw = get_gemini_gathering_response(
                                    user_psid=sender_id,
                                    history_for_gathering_ai=history_for_gemini,
                                    current_user_message_text=current_input_text,
                                    context_info=known_info_for_ai,
                                    page_access_token=current_page_token # Przekaż token
                                )

                                if ai_response_text_raw:
                                    # Reszta logiki handle_gathering (przełączenie na General, parsowanie danych, aktualizacja Fazy 2)
                                    # pozostaje taka sama jak w poprzedniej wersji.
                                    # Wklejam tę część poniżej.

                                    model_resp_content = Content(role="model", parts=[Part.from_text(ai_response_text_raw)])

                                    if ai_response_text_raw.strip() == SWITCH_TO_GENERAL:
                                        logging.info(f"      AI Zbierające -> Wykryto prośbę o przełączenie na tryb ogólny [{SWITCH_TO_GENERAL}].")
                                        context_data_to_save['return_to_state'] = STATE_GATHERING_INFO
                                        # Zapisz kopię CAŁEGO kontekstu zbierania danych do powrotu
                                        context_data_to_save['return_to_context'] = context_data_to_save.copy()
                                        context_data_to_save['type'] = STATE_GENERAL
                                        next_state = STATE_GENERAL
                                        action = 'handle_general'
                                        msg_result = None
                                        logging.debug(f"      Zapisano stan powrotu. Nowy stan: {next_state}. Kontekst zapisu (klucze): {list(context_data_to_save.keys())}")
                                        current_state = next_state
                                        continue

                                    if INFO_GATHERED_MARKER in ai_response_text_raw:
                                        logging.info(f"      AI Zbierające -> Wykryto koniec zbierania danych [{INFO_GATHERED_MARKER}]. Parsowanie i aktualizacja Fazy 2 w '{MAIN_SHEET_NAME}'.")
                                        response_parts = ai_response_text_raw.split(INFO_GATHERED_MARKER, 1)
                                        ai_full_resp_before_marker = response_parts[0].strip()

                                        final_msg_for_user = ""
                                        parsed_student_data = {}
                                        data_line_found = False
                                        data_regex = r"ZEBRANE_DANE_UCZNIA:\s*\[Imię:\s*(.*?),?\s*Nazwisko:\s*(.*?),?\s*KlasaInfo:\s*(.*?),?\s*Poziom:\s*(.*?)\]"
                                        match = re.search(data_regex, ai_full_resp_before_marker, re.IGNORECASE | re.DOTALL)

                                        if match:
                                            data_line_found = True
                                            logging.debug("      Znaleziono dopasowanie regex ZEBRANE_DANE_UCZNIA.")
                                            s_fn = match.group(1).strip() if match.group(1) else "Brak"
                                            s_ln = match.group(2).strip() if match.group(2) else "Brak"
                                            g_info = match.group(3).strip() if match.group(3) else "Brak"
                                            l_info = match.group(4).strip() if match.group(4) else "Brak"
                                            l_info = "Brak" if l_info.lower() == 'brak' else l_info

                                            parsed_student_data = {
                                                'student_first_name': s_fn, 'student_last_name': s_ln,
                                                'grade_info': g_info, 'level_info': l_info
                                            }
                                            logging.info(f"      Sparsowano dane ucznia z AI: {parsed_student_data}")
                                            final_msg_for_user = ai_full_resp_before_marker[match.end():].strip()
                                        else:
                                            logging.error(f"!!! BŁĄD: Nie znaleziono linii 'ZEBRANE_DANE_UCZNIA:' w odpowiedzi AI (Gathering)! PSID: {sender_id}")
                                            final_msg_for_user = ai_full_resp_before_marker
                                            parsed_student_data = {
                                                 'student_first_name': context_data_to_save.get('known_student_first_name', 'Błąd parsowania'),
                                                 'student_last_name': context_data_to_save.get('known_student_last_name', 'Błąd parsowania'),
                                                 'grade_info': context_data_to_save.get('known_grade', 'Błąd parsowania'),
                                                 'level_info': context_data_to_save.get('known_level', 'Błąd parsowania')
                                            }
                                            logging.warning(f"      Używam danych z kontekstu jako fallback: {parsed_student_data}")

                                        if not final_msg_for_user:
                                             final_msg_for_user = "Dziękujemy za podanie informacji. Aby lekcja doszła do skutku, prosimy o potwierdzenie rezerwacji wysyłając wiadomość \"POTWIERDZAM\" na profilu: https://www.facebook.com/profile.php?id=61576135251276 . Jest to również profil do dalszego kontaktu."
                                             logging.warning("      AI nie zwróciło wiadomości końcowej po zebraniu danych. Użyto domyślnej.")

                                        try:
                                            p_fn = context_data_to_save.get('known_parent_first_name', 'Nieznane')
                                            p_ln = context_data_to_save.get('known_parent_last_name', 'Nieznane')
                                            sheet_row_idx = context_data_to_save.get('sheet_row_index')

                                            full_data_for_update = {
                                                'parent_first_name': p_fn, 'parent_last_name': p_ln,
                                                **parsed_student_data
                                            }

                                            update_ok, update_msg = find_row_and_update_sheet(
                                                psid=sender_id, start_time=None,
                                                student_data=full_data_for_update,
                                                sheet_row_index=sheet_row_idx
                                            )

                                            if update_ok:
                                                logging.info(f"      Aktualizacja Fazy 2 w arkuszu '{MAIN_SHEET_NAME}' zakończona pomyślnie. PSID: {sender_id}")
                                                msg_result = final_msg_for_user
                                                next_state = STATE_GENERAL
                                                context_data_to_save = {'type': STATE_GENERAL, 'required_subject': current_subject, '_just_reset': True} # Resetuj kontekst, zachowaj przedmiot
                                                action = None
                                                # break
                                            else: # Błąd aktualizacji Fazy 2
                                                logging.error(f"!!! BŁĄD aktualizacji Fazy 2 w arkuszu '{MAIN_SHEET_NAME}': {update_msg}. PSID: {sender_id}")
                                                error_msg_user = f"Przepraszam, wystąpił błąd podczas zapisywania danych ucznia ({update_msg}). Proszę spróbować ponownie za chwilę lub skontaktować się z nami."
                                                msg_result = error_msg_user
                                                model_resp_content = Content(role="model", parts=[Part.from_text(ai_full_resp_before_marker + f"\n[System Error: {update_msg}]")])
                                                next_state = STATE_GENERAL
                                                context_data_to_save = {'type': STATE_GENERAL, 'required_subject': current_subject, '_just_reset': True}
                                                action = None
                                                # break

                                        except Exception as sheet_update_err:
                                            logging.error(f"!!! KRYTYCZNY BŁĄD podczas wywoływania aktualizacji Fazy 2: {sheet_update_err}", exc_info=True)
                                            msg_result = "Przepraszam, wystąpił krytyczny błąd systemu podczas zapisywania danych. Proszę skontaktować się z nami bezpośrednio."
                                            model_resp_content = Content(role="model", parts=[Part.from_text(msg_result)])
                                            next_state = STATE_GENERAL
                                            context_data_to_save = {'type': STATE_GENERAL, 'required_subject': current_subject, '_just_reset': True}
                                            action = None
                                            # break
                                    else: # AI kontynuuje zbieranie danych
                                        logging.info("      AI Zbierające kontynuuje rozmowę.")
                                        msg_result = ai_response_text_raw
                                        next_state = STATE_GATHERING_INFO
                                        context_data_to_save['type'] = STATE_GATHERING_INFO # Upewnij się
                                        # Aktualizuj kontekst na podstawie odpowiedzi AI
                                        # Prosta aktualizacja - jeśli AI pyta o coś, zakładamy, że reszta jest ok
                                        # Można to ulepszyć, parsując odpowiedź AI dokładniej
                                        if "imię" in ai_response_text_raw.lower(): context_data_to_save['known_student_first_name'] = context_data_to_save.get('known_student_first_name', '')
                                        else: context_data_to_save['known_student_first_name'] = context_data_to_save.get('known_student_first_name', '') or 'Zebrane?' # Lub odczytaj z historii
                                        if "nazwisko" in ai_response_text_raw.lower(): context_data_to_save['known_student_last_name'] = context_data_to_save.get('known_student_last_name', '')
                                        else: context_data_to_save['known_student_last_name'] = context_data_to_save.get('known_student_last_name', '') or 'Zebrane?'
                                        if "klas" in ai_response_text_raw.lower() or "szkoł" in ai_response_text_raw.lower() : context_data_to_save['known_grade'] = context_data_to_save.get('known_grade', '')
                                        else: context_data_to_save['known_grade'] = context_data_to_save.get('known_grade', '') or 'Zebrane?'
                                        if "poziom" in ai_response_text_raw.lower(): context_data_to_save['known_level'] = context_data_to_save.get('known_level', '')
                                        else: context_data_to_save['known_level'] = context_data_to_save.get('known_level', '') or 'Zebrane?' # Poziom może być 'Brak'

                                        action = None
                                        # break

                                else: # Błąd AI Gathering
                                    logging.error(f"!!! BŁĄD: AI Zbierające nie zwróciło odpowiedzi. PSID: {sender_id}")
                                    msg_result = "Przepraszam, wystąpił błąd systemu podczas zbierania informacji. Spróbuj podać dane jeszcze raz."
                                    model_resp_content = Content(role="model", parts=[Part.from_text(msg_result)])
                                    next_state = STATE_GATHERING_INFO # Pozostań
                                    context_data_to_save['type'] = STATE_GATHERING_INFO
                                    action = None
                                    # break
                            except Exception as gather_err:
                                logging.error(f"!!! KRYTYCZNY BŁĄD w logice 'handle_gathering': {gather_err}", exc_info=True)
                                msg_result = "Przepraszam, wystąpił nieoczekiwany błąd systemu podczas przetwarzania danych ucznia."
                                model_resp_content = Content(role="model", parts=[Part.from_text(msg_result)])
                                next_state = STATE_GENERAL
                                context_data_to_save = {'type': STATE_GENERAL, 'required_subject': current_subject, '_just_reset': True}
                                action = None
                                # break


                        # --- Akcja Wysyłania Informacji ---
                        elif current_action == 'send_info':
                            logging.debug("  >> Wykonanie: send_info")
                            if msg_result:
                                if not model_resp_content:
                                     model_resp_content = Content(role="model", parts=[Part.from_text(msg_result)])
                                if 'type' not in context_data_to_save:
                                     context_data_to_save['type'] = next_state
                                     context_data_to_save['required_subject'] = current_subject # Upewnij się
                            else:
                                logging.warning(f"Akcja 'send_info' wywołana bez ustawionej wiadomości (msg_result). Nic nie wysłano. PSID: {sender_id}")
                            action = None
                            # break
                        else:
                            logging.error(f"   Nieznana lub nieobsługiwana akcja '{current_action}'. Przerywam przetwarzanie. PSID: {sender_id}")
                            action = None
                            # break

                    # --- Koniec pętli przetwarzania akcji ---
                    logging.debug(f"  << Koniec pętli przetwarzania (po {loop_guard} iteracjach). Finalny stan: {next_state}")

                    # --- Zapis Stanu i Historii ---
                    final_context_to_save_dict = context_data_to_save.copy()
                    final_context_to_save_dict['type'] = next_state
                    # Upewnij się, że 'required_subject' jest w finalnym kontekście
                    if 'required_subject' not in final_context_to_save_dict:
                        final_context_to_save_dict['required_subject'] = current_subject

                    if next_state != STATE_GENERAL or 'return_to_state' not in final_context_to_save_dict:
                         final_context_to_save_dict.pop('return_to_state', None)
                         final_context_to_save_dict.pop('return_to_context', None)

                    if msg_result:
                        send_message(sender_id, msg_result, current_page_token) # Użyj tokenu strony
                    elif current_action:
                        logging.debug(f"    Akcja '{current_action}' zakończona bez wiadomości dla użytkownika.")

                    original_context_no_return = context.copy()
                    original_context_no_return.pop('return_to_state', None)
                    original_context_no_return.pop('return_to_context', None)

                    should_save = (
                        bool(user_content) or
                        bool(model_resp_content) or
                        (original_context_no_return != final_context_to_save_dict)
                    )

                    if should_save:
                        history_to_save = list(history_for_gemini)
                        if user_content: history_to_save.append(user_content)
                        if model_resp_content: history_to_save.append(model_resp_content)

                        max_hist_len = MAX_HISTORY_TURNS * 2
                        history_to_save = history_to_save[-max_hist_len:]

                        logging.info(f"Zapisywanie historii ({len(history_to_save)} wiad.). Stan: {final_context_to_save_dict.get('type')}, Przedmiot Kontekstu: {final_context_to_save_dict.get('required_subject')}")
                        logging.debug(f"   Kontekst do zapisu (klucze): {list(final_context_to_save_dict.keys())}")
                        save_history(sender_id, history_to_save, context_to_save=final_context_to_save_dict)
                    else:
                        logging.debug("    Brak zmian w historii lub kontekście - pomijanie zapisu.")

            logging.info(f"--- Zakończono przetwarzanie zdarzeń w batchu dla Strony: '{current_page_name}' ({recipient_id}) ---")
            # Zwracamy 200 OK do Facebooka po przetworzeniu wszystkich zdarzeń w wejściowym batchu
            return Response("EVENT_RECEIVED", status=200)
        else:
            # Otrzymano POST, ale nie jest to zdarzenie strony Facebooka
            logging.warning(f"Otrzymano POST na /webhook, ale obiekt != 'page'. Typ: {data.get('object') if isinstance(data, dict) else type(data)}. Dane: {raw_data[:200]}...")
            return Response("OK", status=200) # Odpowiedz OK, ale zaloguj ostrzeżenie
    except json.JSONDecodeError as e:
        logging.error(f"!!! BŁĄD dekodowania JSON w /webhook: {e}", exc_info=True)
        logging.error(f"    Surowe dane: {raw_data[:500]}...")
        # Zwracamy 400 Bad Request, bo dane wejściowe były nieprawidłowe
        return Response("Invalid JSON", status=400)
    except Exception as e:
        # Złap wszystkie inne nieoczekiwane błędy
        logging.critical(f"!!! KRYTYCZNY BŁĄD podczas obsługi POST /webhook: {e}", exc_info=True)
        # Zwróć 200 OK do Facebooka, aby uniknąć ponownych prób, ale zaloguj krytyczny błąd
        return Response("Internal Server Error Occurred", status=200)


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

    # Wycisz gadatliwe loggery bibliotek zewnętrznych
    logging.getLogger('googleapiclient.discovery_cache').setLevel(logging.ERROR)
    logging.getLogger('googleapiclient._helpers').setLevel(logging.WARNING) # Wycisz logi o cache
    logging.getLogger('urllib3.connectionpool').setLevel(logging.WARNING)
    logging.getLogger('werkzeug').setLevel(logging.WARNING) # Logi Flaska tylko na WARNING i wyżej

    print("\n" + "="*60)
    print("--- START BOTA (Wiele Stron FB + Statystyki) ---")
    print(f"  * Poziom logowania: {logging.getLevelName(log_level)}")
    print("-" * 60)
    print("  Konfiguracja Facebook:")
    print(f"    FB_VERIFY_TOKEN: {'OK' if VERIFY_TOKEN and VERIFY_TOKEN != 'KOLAGEN' else 'DOMYŚLNY lub BRAK!'}")
    print("    Skonfigurowane Strony:")
    if PAGE_CONFIG:
        for page_id, config in PAGE_CONFIG.items():
            token_status = "OK" if config.get("token") and len(config["token"]) > 50 else "BRAK lub ZBYT KRÓTKI!!!"
            subject = config.get("subject", "Brak")
            name = config.get("name", f"Strona {page_id}")
            print(f"      - ID: {page_id}, Nazwa: '{name}', Przedmiot: {subject}, Token: {token_status}")
            if token_status != "OK":
                 print(f"!!! KRYTYCZNE: Problem z tokenem dla strony {name} ({page_id}) !!!")
    else:
        print("!!! KRYTYCZNE: Brak skonfigurowanych stron w PAGE_CONFIG !!!")
    print("-" * 60)
    print("  Konfiguracja Vertex AI:")
    print(f"    Projekt: {PROJECT_ID}, Lokalizacja: {LOCATION}, Model: {MODEL_ID}")
    if not gemini_model:
        print("!!! KRYTYCZNE: Model Gemini NIE załadowany! AI będzie niedostępne. !!!")
    else:
        print(f"    Model Gemini ({MODEL_ID}): Załadowany (OK)")
    print("-" * 60)
    print("  Konfiguracja Google Calendar:")
    print(f"    Dostępne przedmioty: {', '.join(AVAILABLE_SUBJECTS)}")
    print("    Przypisanie Kalendarzy do Przedmiotów:")
    if SUBJECT_TO_CALENDARS:
        for subject, cal_list in SUBJECT_TO_CALENDARS.items():
            cal_names = [f"'{c['name']}' ({c['id'][-6:]}...)" for c in cal_list]
            print(f"      - {subject.capitalize()}: {', '.join(cal_names)}")
    else:
        print("      !!! BRAK skonfigurowanych kalendarzy dla przedmiotów !!!")
    print(f"    Strefa: {CALENDAR_TIMEZONE} (TZ: {_get_calendar_timezone()})")
    print(f"    Filtry: Godz. {WORK_START_HOUR}-{WORK_END_HOUR}, Wyprz. {MIN_BOOKING_LEAD_HOURS}h, Zakres {MAX_SEARCH_DAYS}dni")
    print(f"    Plik klucza: {CALENDAR_SERVICE_ACCOUNT_FILE} ({'OK' if os.path.exists(CALENDAR_SERVICE_ACCOUNT_FILE) else 'BRAK!!!'})")
    cal_service = get_calendar_service()
    print(f"    Usługa Calendar API: {'OK' if cal_service else 'BŁĄD INICJALIZACJI!'}")
    print("-" * 60)
    print("  Konfiguracja Google Sheets:")
    print(f"    ID Arkusza: {SPREADSHEET_ID}")
    print(f"    Główny Arkusz (Rezerwacje): '{MAIN_SHEET_NAME}'")
    print(f"    Arkusz Statystyk: '{STATS_SHEET_NAME}'")
    print(f"    Strefa: {SHEET_TIMEZONE} (TZ: {_get_sheet_timezone()})")
    print(f"    Kolumny Kluczowe (Arkusz1): Data={SHEET_DATE_COLUMN_INDEX}({chr(ord('A')+SHEET_DATE_COLUMN_INDEX-1)}), Czas={SHEET_TIME_COLUMN_INDEX}({chr(ord('A')+SHEET_TIME_COLUMN_INDEX-1)}), NumerKlasy(H)={SHEET_GRADE_COLUMN_INDEX}({chr(ord('A')+SHEET_GRADE_COLUMN_INDEX-1)}), Kalendarz={SHEET_CALENDAR_NAME_COLUMN_INDEX}({chr(ord('A')+SHEET_CALENDAR_NAME_COLUMN_INDEX-1)})")
    print(f"    Struktura Statystyk (Arkusz2): Daty w wierszu {STATS_DATE_HEADER_ROW}, Etykiety: '{STATS_NEW_CONTACT_ROW_LABEL}', '{STATS_BOOKING_ROW_LABEL}'")
    print(f"    Plik klucza: {SHEETS_SERVICE_ACCOUNT_FILE} ({'OK' if os.path.exists(SHEETS_SERVICE_ACCOUNT_FILE) else 'BRAK!!!'})")
    sheets_service = get_sheets_service()
    print(f"    Usługa Sheets API: {'OK' if sheets_service else 'BŁĄD INICJALIZACJI!'}")
    print("--- KONIEC KONFIGURACJI ---")
    print("="*60 + "\n")

    port = int(os.environ.get("PORT", 8080))
    # Uruchom w trybie debug Flask tylko jeśli logowanie jest na DEBUG
    run_flask_in_debug = (log_level == logging.DEBUG)

    print(f"Uruchamianie serwera Flask na porcie {port}...")
    if not run_flask_in_debug:
        try:
            from waitress import serve
            print(">>> Uruchamianie serwera produkcyjnego Waitress <<<")
            # Zwiększenie liczby wątków może pomóc w obsłudze wielu jednoczesnych zapytań
            serve(app, host='0.0.0.0', port=port, threads=16)
        except ImportError:
            print("!!! Ostrz.: Biblioteka 'waitress' nie została znaleziona. Uruchamiam wbudowany serwer deweloperski Flask (niezalecane na produkcji).")
            print(">>> Uruchamianie serwera deweloperskiego Flask <<<")
            # Uruchomienie serwera deweloperskiego bez trybu debug
            app.run(host='0.0.0.0', port=port, debug=False)
    else:
        # Uruchomienie serwera deweloperskiego z trybem debug Flask
        print(">>> Uruchamianie serwera deweloperskiego Flask w trybie DEBUG <<<")
        # use_reloader=False jest zalecane przy debugowaniu, aby uniknąć podwójnej inicjalizacji
        app.run(host='0.0.0.0', port=port, debug=True, use_reloader=False)
