# -*- coding: utf-8 -*-

# verify_server.py (Wersja: Wiele Stron FB + Statystyki + Poprawki)
from pyairtable import Api 
from flask import Flask, request, Response
import threading # <--- DODAJ TEN IMPORT
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
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
import errno
import logging
import datetime
import pytz
import locale
import re
from collections import defaultdict # Import defaultdict


# --- Konfiguracja Stron Facebook ---
# Zamiast pojedynczego PAGE_ACCESS_TOKEN, używamy słownika
# Klucz: Page ID (ID strony odbiorcy), Wartość: Słownik {'token': '...', 'subject': '...'}
PAGE_CONFIG = {
    # Polski: Zakrzeczone Korepetycje - Polski (ID: 661857023673365)
    "661857023673365": {
        "token": "EAAJUltDBrJQBPOBUTGeEQusJwEZA2CwhD63gy7TXOVcQpnDW2gKtNZASCZBnUZBvhlh5OAe9iaOR8Cn0hrG34wacQHlUCYXBxZBzDg1sHuAjV0IBZAHvc9RT9ZAXbFgn0CMQq00ZCS52RFVwlgM4rDKVwSJrzGKJqvaJOCmTeuE9ZBY6IXYB1rR2iXb3beNo4lGZBF8h0nvRoCmQZDZD",
        "subject": "Polski",
        "name": "Zakręcone Korepetycje - Polski",
        "link": "https://tiny.pl/0xnsgbt2" # Link do strony z Polskim
    },
    # Matematyka: ZakrzeczoneKorepetycje - MATEMATYKA... (ID: 638454406015018)
    "638454406015018": {
        "token": "EAAJUltDBrJQBPAZAylC5nVzVG4NjbhPZCtvI40aWuk4y8jm6wLdQqjuhsMMxHATytTF6Awh0LPKI9Rljckk9yC31JKtzgWMGhRoCsah1IRDqV0TBYs9XZAZBvWYBW0rSt95NFOz6nRw5GZAXlT7yIbKUO3tkZCTuZB3eTDX0kaTjRl9I8ueLsZCa9ZBVZCHNb7f5pj8F8ZC9yyehQZDZD",
        "subject": "Matematyka",
        "name": "Zakręcone Korepetycje - MATEMATYKA",
        "link": "https://tiny.pl/f7xz5n0g" # Link do strony z Matematyką
    },
     # Angielski: English Zone: Zakrzeczone Korepetycje (ID: 653018101222547)
    "653018101222547": {
        "token": "EAAJUltDBrJQBPOkydccFPcQ1SDZBhYFBFZCMTohk1hgtLbHNmwdA0ylZCZBMTDeDG2OOmbhYaN5KJTJV5N4pX1LLR60G9ye8btGM1hfCfXoLsz1qSw7YUqZCrzeLQqrKQ5uEOn19VGGg7zfEDuLy6TZAPBtf5kQK7ZBKGnrZBaWcmfGofVoAQ5R2stUyG6bGCnPWpx1CZBzzlZAgZDZD",
        "subject": "Angielski",
        "name": "English Zone: Zakręcone Korepetycje",
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
PROJECT_ID = os.environ.get("GCP_PROJECT_ID", "singular-carver-459118-g5")
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
CALENDAR_SERVICE_ACCOUNT_FILE = 'KALENDARZ_KLUCZ.json'
CALENDAR_SCOPES = ['https://www.googleapis.com/auth/calendar.readonly']
CALENDAR_TIMEZONE = 'Europe/Warsaw'
APPOINTMENT_DURATION_MINUTES = 60
WORK_START_HOUR = 8
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

# --- Konfiguracja Airtable ---
AIRTABLE_API_KEY = os.environ.get("AIRTABLE_API_KEY", "patcSdupvwJebjFDo.7e15a93930d15261989844687bcb15ac5c08c84a29920c7646760bc6f416146d")
AIRTABLE_BASE_ID = os.environ.get("AIRTABLE_BASE_ID", "appTjrMTVhYBZDPw9")
AIRTABLE_BOOKINGS_TABLE_NAME = "Rezerwacje"  # Nazwa Twojej tabeli rezerwacji
AIRTABLE_STATS_TABLE_NAME = "Statystyki"      # Nazwa Twojej tabeli statystyk
AIRTABLE_TIMEZONE = 'Europe/Warsaw'          # Ważne dla spójności dat

# Inicjalizacja klienta Airtable (zastępuje get_sheets_service)
try:
    airtable_api = Api(AIRTABLE_API_KEY)
    logging.info("Utworzono połączenie z Airtable API.")
except Exception as e:
    airtable_api = None
    logging.critical(f"KRYTYCZNY BŁĄD inicjalizacji Airtable API: {e}", exc_info=True)


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
_cal_tz = None
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



# =====================================================================
# === FUNKCJA PRZETWARZANIA POJEDYNCZEGO ZDARZENIA W TLE ==============
# =====================================================================
# =====================================================================
# === FUNKCJA PRZETWARZANIA POJEDYNCZEGO ZDARZENIA W TLE ==============
# =====================================================================
def process_single_event(event_payload, page_id_from_entry_info): # page_id_from_entry_info to ID strony, która FAKTYCZNIE otrzymała webhook
    """
    Przetwarza pojedyncze zdarzenie 'messaging' od Facebooka.
    Ta funkcja będzie uruchamiana w osobnym wątku.
    """
    try:
        logging.info(f"(Wątek) RAW EVENT PAYLOAD: {json.dumps(event_payload)}")

        # --- KROK 1: Ostrożna identyfikacja ról i konfiguracji strony ---
        actual_user_psid = None
        page_config_for_event = None # Konfiguracja strony, z którą użytkownik rozmawia

        event_sender_id = event_payload.get("sender", {}).get("id")
        event_recipient_id = event_payload.get("recipient", {}).get("id")

        if not event_sender_id or not event_recipient_id:
            logging.warning(f"(Wątek) Zdarzenie bez sender.id lub recipient.id. Event: {event_payload}")
            return

        message_data_for_echo_check = event_payload.get("message")
        is_echo = message_data_for_echo_check and message_data_for_echo_check.get("is_echo")

        if is_echo:
            echoing_page_config = PAGE_CONFIG.get(event_sender_id)
            echoing_page_name_for_log = echoing_page_config.get('name', event_sender_id) if echoing_page_config else event_sender_id
            logging.debug(f"    (Wątek) Pominięto echo. Strona wysyłająca: '{echoing_page_name_for_log}' ({event_sender_id}). Odbiorca echa (user): {event_recipient_id}.")
            return
        
        actual_user_psid = event_sender_id
        page_being_contacted_id = event_recipient_id
        page_config_for_event = PAGE_CONFIG.get(page_being_contacted_id)

        if not page_config_for_event:
            logging.error(f"!!! (Wątek) Otrzymano zdarzenie dla nieskonfigurowanej strony ID: {page_being_contacted_id} (Użytkownik PSID: {actual_user_psid}). Pomijam.")
            return

        if actual_user_psid in PAGE_CONFIG:
            logging.warning(f"!!! (Wątek) Potencjalny problem: actual_user_psid ('{actual_user_psid}') jest taki sam jak ID jednej ze skonfigurowanych stron. Strona kontaktu: {page_being_contacted_id}. Pomijam to zdarzenie dla bezpieczeństwa.")
            return 

        current_page_token = page_config_for_event['token']
        current_subject = page_config_for_event.get('subject', "nieznany przedmiot") # Bezpieczne pobieranie
        current_page_name = page_config_for_event['name']

        logging.info(f"--- (Wątek) Przetwarzanie zdarzenia dla Strony: '{current_page_name}' ({page_being_contacted_id}) | Przedmiot Główny Strony: {current_subject} | User PSID: {actual_user_psid} ---")

        if not current_page_token or len(current_page_token) < 50:
            logging.error(f"!!! KRYTYCZNY BŁĄD (Wątek): Brak lub nieprawidłowy token dostępu dla strony '{current_page_name}' ({page_being_contacted_id}).")
            return

        # --- KROK 2: Ładowanie historii i kontekstu ---
        history, context, is_new_contact = load_history(actual_user_psid)
        history_for_gemini = [h for h in history if isinstance(h, Content) and h.role in ('user', 'model')]
        current_state = context.get('type', STATE_GENERAL)

        if is_new_contact:
            logging.info(f"[{actual_user_psid}] (Wątek) Wykryto nowy kontakt dla strony '{current_page_name}'. Logowanie statystyki.")
            log_statistic("new_contact")

        logging.info(f"    (Wątek) [{actual_user_psid}] Aktualny stan: {current_state}")
        logging.debug(f"    (Wątek) [{actual_user_psid}] Kontekst wejściowy (klucze): {list(context.keys())}")

        action = None
        msg_result = None
        ai_response_text_raw = None
        next_state = current_state
        model_resp_content = None 
        user_content = None 
        context_data_to_save = context.copy() 
        
        context_data_to_save.pop('return_to_state', None)
        context_data_to_save.pop('return_to_context', None)

        if context_data_to_save.get('required_subject') != current_subject or 'required_subject' not in context_data_to_save:
            if current_state == STATE_GENERAL or context_data_to_save.get('_just_reset', False):
                context_data_to_save['required_subject'] = current_subject
                logging.debug(f"    (Wątek) [{actual_user_psid}] Ustawiono/zaktualizowano 'required_subject' w kontekście na domyślny przedmiot strony: {current_subject}")
            elif 'required_subject' not in context_data_to_save or not context_data_to_save.get('required_subject'):
                 context_data_to_save['required_subject'] = current_subject 
                 logging.warning(f"    (Wątek) [{actual_user_psid}] 'required_subject' był pusty w stanie {current_state}. Ustawiono na domyślny przedmiot strony: {current_subject}.")

        trigger_gathering_ai_immediately = False
        slot_verification_failed = False 
        is_temporary_general_state = 'return_to_state' in context 

        if message_data := event_payload.get("message"):
            user_input_text = message_data.get("text", "").strip()
            if user_input_text:
                user_content = Content(role="user", parts=[Part.from_text(user_input_text)])
                logging.info(f"    (Wątek) [{actual_user_psid}] Odebrano wiadomość (stan={current_state}): '{user_input_text[:100]}{'...' if len(user_input_text)>100 else ''}'")
                if ENABLE_TYPING_DELAY: time.sleep(MIN_TYPING_DELAY_SECONDS * 0.3)
                if current_state == STATE_SCHEDULING_ACTIVE: action = 'handle_scheduling'
                elif current_state == STATE_GATHERING_INFO: action = 'handle_gathering'
                else: action = 'handle_general' 
            elif attachments := message_data.get("attachments"):
                att_type = attachments[0].get('type', 'nieznany')
                user_content = Content(role="user", parts=[Part.from_text(f"[Załącznik: {att_type}]")])
                msg_result = "Mogę przetwarzać tylko wiadomości tekstowe."
                action = 'send_info' 
                next_state = current_state 
            else:
                logging.info(f"      (Wątek) [{actual_user_psid}] Odebrano pustą wiadomość lub nieobsługiwany typ komunikatu. Kończenie.")
                return 
        elif postback := event_payload.get("postback"):
            payload = postback.get("payload")
            title = postback.get("title", "")
            user_input_text = f"Kliknięto: '{title}' (Payload: {payload})" 
            user_content = Content(role="user", parts=[Part.from_text(user_input_text)])
            logging.info(f"    (Wątek) [{actual_user_psid}] Odebrano postback: Payload='{payload}', Tytuł='{title}' (stan={current_state})")
            if payload == "CANCEL_SCHEDULING":
                msg_result = "Proces umawiania został anulowany."
                action = 'send_info'
                next_state = STATE_GENERAL
                context_data_to_save = {'type': STATE_GENERAL, 'required_subject': current_subject, '_just_reset': True} 
            elif current_state == STATE_SCHEDULING_ACTIVE: action = 'handle_scheduling'
            elif current_state == STATE_GATHERING_INFO: action = 'handle_gathering'
            else: action = 'handle_general'
        elif event_payload.get("read"):
            logging.debug(f"    (Wątek) Potwierdzenie odczytania wiadomości przez użytkownika {actual_user_psid}.")
            return
        elif event_payload.get("delivery"):
            logging.debug(f"    (Wątek) Potwierdzenie dostarczenia wiadomości do użytkownika {actual_user_psid}.")
            return
        else:
            logging.warning(f"    (Wątek) Otrzymano nieobsługiwany typ zdarzenia dla PSID {actual_user_psid}. Event: {json.dumps(event_payload)}")
            return

        if not action and not msg_result: 
            logging.debug(f"    (Wątek) [{actual_user_psid}] Brak akcji lub wiadomości do przetworzenia po analizie typu zdarzenia. Kończenie.")
            return

        loop_guard = 0
        max_loops = 3
        while (action or msg_result) and loop_guard < max_loops:
            loop_guard += 1
            effective_subject_for_action = context_data_to_save.get('required_subject', current_subject)
            logging.debug(f"  >> (Wątek) [{actual_user_psid}] Pętla {loop_guard}/{max_loops} | Akcja: {action} | Stan: {current_state} -> {next_state} | Efektywny Przedmiot: {effective_subject_for_action}")
            current_action_in_loop = action
            action = None

            # --- handle_general ---
            if current_action_in_loop == 'handle_general':
                is_initial_general_entry = (current_state != STATE_GENERAL) or \
                                        (not history_for_gemini and not user_content) or \
                                        (context_data_to_save.get('_just_reset', False))
                context_data_to_save.pop('_just_reset', None)
                user_message_text_for_ai = user_content.parts[0].text if user_content and user_content.parts else None
                
                if is_initial_general_entry and not user_message_text_for_ai: 
                    logging.debug(f"    (Wątek) [{actual_user_psid}] Generowanie wiadomości powitalnej. Bieżący przedmiot strony: '{current_subject}'")
                    other_subjects_links_parts = []
                    
                    if not PAGE_CONFIG:
                        logging.warning(f"    (Wątek) [{actual_user_psid}] PAGE_CONFIG jest pusty! Nie można wygenerować linków.")
                    
                    for page_id_iter, page_data_entry in PAGE_CONFIG.items(): 
                        subj_name = page_data_entry.get("subject")
                        subj_link = page_data_entry.get("link")
                        page_entry_name_for_log = page_data_entry.get("name", f"ID: {page_id_iter}")

                        logging.debug(f"      Iteracja PAGE_CONFIG dla '{page_entry_name_for_log}': Przedmiot='{subj_name}', Link='{subj_link}'")

                        if subj_name and subj_link:
                            if current_subject and subj_name.lower() != current_subject.lower(): # Upewnij się, że current_subject nie jest None
                                other_subjects_links_parts.append(f"- {subj_name}: {subj_link}")
                                logging.debug(f"        Dodano link: - {subj_name}: {subj_link}")
                            elif not current_subject: # Jeśli current_subject jest None, to nie ma z czym porównywać, więc dodaj link
                                other_subjects_links_parts.append(f"- {subj_name}: {subj_link}")
                                logging.warning(f"        Dodano link (current_subject był pusty/None, więc nie można porównać): - {subj_name}: {subj_link}")
                            # else: # current_subject istnieje i jest taki sam jak subj_name - nie rób nic (nie dodawaj)
                        else:
                            logging.warning(f"        Pominięto wpis dla '{page_entry_name_for_log}' z PAGE_CONFIG - brak 'subject' lub 'link'.")
                    
                    links_text_for_user = ""
                    if other_subjects_links_parts:
                        links_text_for_user = "\n\nUdzielamy również korepetycji z:\n" + "\n".join(other_subjects_links_parts)
                        logging.debug(f"    (Wątek) [{actual_user_psid}] Sformatowany tekst linków: {links_text_for_user}")
                    else:
                        logging.debug(f"    (Wątek) [{actual_user_psid}] Brak linków do innych przedmiotów do wyświetlenia.")

                    display_subject = current_subject if current_subject else "korepetycji"
                    msg_result = f"Dzień dobry! Dziękujemy za kontakt w sprawie korepetycji z przedmiotu **{display_subject}**. W czym mogę pomóc? Jeśli chcą Państwo umówić termin, proszę dać znać, a ja sprawdzę dostępne opcje." + links_text_for_user
                    logging.info(f"    (Wątek) [{actual_user_psid}] Wiadomość powitalna wygenerowana: '{msg_result[:200]}...'")
                    
                    model_resp_content = Content(role="model", parts=[Part.from_text(msg_result)])
                    next_state = STATE_GENERAL
                    context_data_to_save.update({'type': STATE_GENERAL, 'required_subject': current_subject})
                
                elif user_message_text_for_ai: 
                    was_temporary = 'return_to_state' in context 
                    
                    # Wywołanie get_gemini_general_response - upewnij się, że ta funkcja poprawnie
                    # używa/formatuje SYSTEM_INSTRUCTION_GENERAL z linkami
                    # (zgodnie z jednym z dwóch podejść, które omawialiśmy)
                    ai_response_text_raw = get_gemini_general_response(
                        actual_user_psid, 
                        user_message_text_for_ai, 
                        history_for_gemini,
                        is_temporary_general_state, 
                        current_page_token,
                        current_subject_for_context=current_subject # Przekazujemy przedmiot bieżącej strony
                    )
                    
                    if ai_response_text_raw:
                        model_resp_content = Content(role="model", parts=[Part.from_text(ai_response_text_raw)])
                        if RETURN_TO_PREVIOUS in ai_response_text_raw and was_temporary:
                            msg_result = ai_response_text_raw.split(RETURN_TO_PREVIOUS, 1)[0].strip()
                            next_state = context.get('return_to_state', STATE_GENERAL) 
                            context_data_to_save = context.get('return_to_context', {}).copy() 
                            context_data_to_save['type'] = next_state 
                            if next_state == STATE_SCHEDULING_ACTIVE: action = 'handle_scheduling' 
                            elif next_state == STATE_GATHERING_INFO: action = 'handle_gathering'; trigger_gathering_ai_immediately = True
                            else: action = 'handle_general' 
                            current_state = next_state 
                        elif INTENT_SCHEDULE_MARKER in ai_response_text_raw:
                            # 1. Najpierw wyciągnij dane o uczniu, jeśli AI je podało
                            student_data_match = re.search(
                                r"\[DANE_UCZNIA_OGOLNE:\s*KlasaInfo:\s*(.*?),?\s*Poziom:\s*(.*?)\]",
                                ai_response_text_raw, re.IGNORECASE | re.DOTALL
                            )
                            
                            grade_info_from_ai = ""
                            level_info_from_ai = ""
                            
                            if student_data_match:
                                grade_info_from_ai = student_data_match.group(1).strip()
                                level_info_from_ai = student_data_match.group(2).strip()
                                logging.info(f"    (Wątek) [{actual_user_psid}] AI (General) zidentyfikowało dane ucznia: Klasa='{grade_info_from_ai}', Poziom='{level_info_from_ai}'")
                                # Usuń znacznik z odpowiedzi, żeby użytkownik go nie widział
                                ai_response_text_raw = re.sub(r"\[DANE_UCZNIA_OGOLNE:.*?\]", "", ai_response_text_raw).strip()

                            # 2. Teraz przetwórz intencję umówienia wizyty
                            msg_result = ai_response_text_raw.split(INTENT_SCHEDULE_MARKER, 1)[0].strip()
                            confirmed_subject_by_ai = effective_subject_for_action 
                            
                            if confirmed_subject_by_ai and confirmed_subject_by_ai in AVAILABLE_SUBJECTS:
                                next_state = STATE_SCHEDULING_ACTIVE
                                # Tworzymy nowy kontekst i od razu wstawiamy dane o uczniu!
                                context_data_to_save = {
                                    'type': STATE_SCHEDULING_ACTIVE, 
                                    'required_subject': confirmed_subject_by_ai,
                                    'known_grade': grade_info_from_ai,
                                    'known_level': level_info_from_ai
                                }
                                action = 'handle_scheduling' 
                                current_state = next_state
                            else:
                                msg_result = (msg_result + "\n\n" if msg_result else "") + f"Nie jestem pewien, dla którego przedmiotu chcą Państwo umówić termin. Dostępne przedmioty to: {', '.join(AVAILABLE_SUBJECTS)}. Proszę sprecyzować."
                                model_resp_content = Content(role="model", parts=[Part.from_text(msg_result)]) 
                                next_state = STATE_GENERAL
                                context_data_to_save.update({'type': STATE_GENERAL, 'required_subject': current_subject})
                        else: 
                            msg_result = ai_response_text_raw
                            next_state = STATE_GENERAL
                            context_data_to_save.update({'type': STATE_GENERAL, 'required_subject': effective_subject_for_action})
                            if was_temporary: 
                                context_data_to_save.update({
                                    'return_to_state': context.get('return_to_state'),
                                    'return_to_context': context.get('return_to_context', {})
                                })
                    else: 
                        msg_result = "Przepraszam, mam chwilowy problem z przetworzeniem Twojej wiadomości. Spróbuj ponownie za chwilę."
                        model_resp_content = Content(role="model", parts=[Part.from_text(msg_result)])
                        next_state = STATE_GENERAL
                        context_data_to_save = {'type': STATE_GENERAL, 'required_subject': current_subject, '_just_reset': True} 

            # --- handle_scheduling ---
            elif current_action_in_loop == 'handle_scheduling':
                if not effective_subject_for_action or effective_subject_for_action not in AVAILABLE_SUBJECTS:
                    msg_result = f"Przepraszam, wystąpił błąd. Nie wiem, dla jakiego przedmiotu ('{effective_subject_for_action}') próbujemy umówić termin. Proszę zacząć od nowa, np. pisząc 'Chcę umówić {current_subject if current_subject else 'korepetycje'}'."
                    model_resp_content = Content(role="model", parts=[Part.from_text(msg_result)])
                    next_state = STATE_GENERAL
                    context_data_to_save = {'type': STATE_GENERAL, 'required_subject': current_subject, '_just_reset': True}
                else:
                    subject_calendars_config = SUBJECT_TO_CALENDARS.get(effective_subject_for_action.lower(), [])
                    if not subject_calendars_config:
                        msg_result = f"Przepraszam, obecnie nie mam skonfigurowanych kalendarzy dla przedmiotu '{effective_subject_for_action}'. Skontaktuj się z nami inną drogą w sprawie tego przedmiotu."
                        model_resp_content = Content(role="model", parts=[Part.from_text(msg_result)])
                        next_state = STATE_GENERAL
                        context_data_to_save = {'type': STATE_GENERAL, 'required_subject': current_subject, '_just_reset': True}
                    else:
                        try:
                            tz_cal = _get_calendar_timezone()
                            now_cal_tz = datetime.datetime.now(tz_cal)
                            search_start_dt = now_cal_tz 
                            search_end_dt = tz_cal.localize(datetime.datetime.combine(
                                (now_cal_tz + datetime.timedelta(days=MAX_SEARCH_DAYS)).date(),
                                datetime.time(WORK_END_HOUR, 0) 
                            ))
                            _simulate_typing(actual_user_psid, MAX_TYPING_DELAY_SECONDS * 0.7, current_page_token) 
                            free_ranges = get_free_time_ranges(subject_calendars_config, search_start_dt, search_end_dt)
                            user_msg_for_ai = user_content.parts[0].text if user_content and user_content.parts else None
                            if slot_verification_failed:
                                user_msg_for_ai = (user_msg_for_ai or "") + \
                                                  f"\n[Informacja systemowa: Poprzednio proponowany termin okazał się niedostępny. Zaproponuj proszę inny termin dla przedmiotu {effective_subject_for_action}, biorąc pod uwagę preferencje użytkownika i dostępne zakresy.]"
                                slot_verification_failed = False 
                            ai_response_text_raw = get_gemini_scheduling_response(
                                actual_user_psid, history_for_gemini, user_msg_for_ai, 
                                free_ranges, effective_subject_for_action, current_page_token
                            )
                            if ai_response_text_raw:
                                model_resp_content = Content(role="model", parts=[Part.from_text(ai_response_text_raw)]) 
                                if ai_response_text_raw.strip() == SWITCH_TO_GENERAL:
                                    context_data_to_save.update({
                                        'return_to_state': STATE_SCHEDULING_ACTIVE, 
                                        'return_to_context': {'type': STATE_SCHEDULING_ACTIVE, 'required_subject': effective_subject_for_action}, 
                                        'type': STATE_GENERAL 
                                    })
                                    next_state = STATE_GENERAL
                                    action = 'handle_general' 
                                    current_state = next_state
                                    msg_result = None 
                                else:
                                    iso_match = re.search(rf"{re.escape(SLOT_ISO_MARKER_PREFIX)}(.*?){re.escape(SLOT_ISO_MARKER_SUFFIX)}", ai_response_text_raw)
                                    if iso_match: 
                                        extracted_iso = iso_match.group(1).strip()
                                        text_for_user = re.sub(r'\s+', ' ', re.sub(rf"{re.escape(SLOT_ISO_MARKER_PREFIX)}.*?{re.escape(SLOT_ISO_MARKER_SUFFIX)}", "", ai_response_text_raw).strip()).strip()
                                        try:
                                            proposed_start_dt = datetime.datetime.fromisoformat(extracted_iso)
                                            proposed_start_dt = proposed_start_dt.astimezone(tz_cal) if proposed_start_dt.tzinfo else tz_cal.localize(proposed_start_dt)
                                            proposed_slot_formatted = format_slot_for_user(proposed_start_dt)
                                            _simulate_typing(actual_user_psid, MIN_TYPING_DELAY_SECONDS, current_page_token) 
                                            chosen_calendar_id = None
                                            chosen_calendar_name = None
                                            # Weryfikujemy ostatecznie dostępność w kalendarzach Google
                                            for cal_conf_iter in subject_calendars_config:
                                                if is_slot_actually_free(proposed_start_dt, cal_conf_iter['id']):
                                                    chosen_calendar_id = cal_conf_iter['id']
                                                    chosen_calendar_name = cal_conf_iter['name']
                                                    logging.info(f"    (Wątek) [{actual_user_psid}] Slot {proposed_slot_formatted} jest WOLNY w kalendarzu Google '{chosen_calendar_name}'.")
                                                    break
                                            
                                            if chosen_calendar_id and chosen_calendar_name: 
                                                # Faza 1: Tworzenie rekordu w Airtable
                                                write_ok, record_id_or_error_msg = create_airtable_record_phase1(actual_user_psid, proposed_start_dt, chosen_calendar_name, effective_subject_for_action)
                                                if write_ok:
                                                    record_id = record_id_or_error_msg
                                                    user_profile_fb = get_user_profile(actual_user_psid, current_page_token)
                                                    parent_fn = user_profile_fb.get('first_name', '') if user_profile_fb else ''
                                                    parent_ln = user_profile_fb.get('last_name', '') if user_profile_fb else ''
                                                    msg_result_scheduling_confirmation = (text_for_user if text_for_user else f"Świetnie, proponowany termin na {effective_subject_for_action} to {proposed_slot_formatted}.")
                                                    msg_result = msg_result_scheduling_confirmation
                                                    model_confirmation_content_for_history = Content(role="model", parts=[Part.from_text(msg_result_scheduling_confirmation)])
                                                    next_state = STATE_GATHERING_INFO
                                                    context_data_to_save = {
                                                        'type': STATE_GATHERING_INFO,
                                                        'proposed_slot_iso': proposed_start_dt.isoformat(),
                                                        'proposed_slot_formatted': proposed_slot_formatted,
                                                        'chosen_calendar_id': chosen_calendar_id,
                                                        'chosen_calendar_name': chosen_calendar_name,
                                                        'required_subject': effective_subject_for_action,
                                                        'known_parent_first_name': parent_fn,
                                                        'known_parent_last_name': parent_ln,
                                                        'known_student_first_name': '',
                                                        'known_student_last_name': '',
                                                        'known_grade': '', 'known_level': '',
                                                        'airtable_record_id': record_id,  # Zapisujemy ID z Airtable
                                                        'last_model_message_before_gathering': model_confirmation_content_for_history
                                                    }
                                                    action = 'handle_gathering'
                                                    trigger_gathering_ai_immediately = True
                                                    current_state = next_state
                                                else:
                                                    error_message = record_id_or_error_msg
                                                    msg_result = f"Przepraszam, wystąpił błąd podczas wstępnej rezerwacji terminu ({error_message}). Proszę wybrać termin ponownie."
                                                    next_state = STATE_SCHEDULING_ACTIVE
                                                    context_data_to_save.update({'type': STATE_SCHEDULING_ACTIVE, 'required_subject': effective_subject_for_action})
                                                    slot_verification_failed = True
                                    
                                            else: 
                                                reason_for_failure = "właśnie został zajęty lub jest zablokowany" if not chosen_calendar_id else "jest już zajęty w naszym systemie rezerwacji"
                                                msg_result = (text_for_user if text_for_user else "") + \
                                                             (("\n" if text_for_user else "") + f"Niestety, termin {proposed_slot_formatted} {reason_for_failure}. Proszę, wybierzmy inny.")
                                                next_state = STATE_SCHEDULING_ACTIVE
                                                context_data_to_save.update({'type': STATE_SCHEDULING_ACTIVE, 'required_subject': effective_subject_for_action})
                                                slot_verification_failed = True 
                                        except ValueError as ve: 
                                            logging.error(f"(Wątek) [{actual_user_psid}] Błąd parsowania daty ISO '{extracted_iso}' z AI: {ve}")
                                            msg_result = "Wystąpił błąd z formatem proponowanego terminu. Spróbujmy wybrać ponownie."
                                            next_state = STATE_SCHEDULING_ACTIVE
                                            context_data_to_save.update({'type': STATE_SCHEDULING_ACTIVE, 'required_subject': effective_subject_for_action})
                                        except Exception as verif_err: 
                                            logging.error(f"(Wątek) [{actual_user_psid}] Krytyczny błąd weryfikacji/zapisu slotu: {verif_err}", exc_info=True)
                                            msg_result = "Przepraszam, wystąpił wewnętrzny błąd systemu podczas weryfikacji terminu. Spróbuj ponownie później."
                                            next_state = STATE_GENERAL 
                                            context_data_to_save = {'type': STATE_GENERAL, 'required_subject': current_subject, '_just_reset': True}
                                    else: 
                                        msg_result = ai_response_text_raw
                                        next_state = STATE_SCHEDULING_ACTIVE
                                        context_data_to_save.update({'type': STATE_SCHEDULING_ACTIVE, 'required_subject': effective_subject_for_action})
                            else: 
                                msg_result = ai_response_text_raw or f"Przepraszam, mam problem z systemem planowania dla przedmiotu {effective_subject_for_action}. Spróbujmy za chwilę."
                                if "Brak dostępnych terminów" in msg_result or (not free_ranges and not ai_response_text_raw) : 
                                    msg_result = f"Przepraszam, ale obecnie nie mam dostępnych wolnych terminów dla przedmiotu {effective_subject_for_action} w najbliższym czasie. Proszę spróbować później lub skontaktować się z nami inną drogą."
                                model_resp_content = Content(role="model", parts=[Part.from_text(msg_result)]) if msg_result else None
                                next_state = STATE_GENERAL 
                                context_data_to_save = {'type': STATE_GENERAL, 'required_subject': current_subject, '_just_reset': True}
                        except Exception as schedule_err_final:
                            logging.error(f"(Wątek) [{actual_user_psid}] Krytyczny błąd w logice 'handle_scheduling': {schedule_err_final}", exc_info=True)
                            msg_result = "Przepraszam, wystąpił poważny błąd systemu planowania. Spróbuj ponownie później."
                            model_resp_content = Content(role="model", parts=[Part.from_text(msg_result)]) if msg_result else None
                            next_state = STATE_GENERAL
                            context_data_to_save = {'type': STATE_GENERAL, 'required_subject': current_subject, '_just_reset': True}
            
            # --- handle_gathering ---
            elif current_action_in_loop == 'handle_gathering':
                try:
                    user_msg_for_ai = user_content.parts[0].text if user_content and user_content.parts else None
                    current_history_for_gathering_ai = list(history_for_gemini) 
                    if trigger_gathering_ai_immediately:
                        logging.info(f"    (Wątek) [{actual_user_psid}] Inicjuję AI zbierające dane ucznia (trigger_gathering_ai_immediately=True).")
                        last_model_msg_content_from_context = context_data_to_save.pop('last_model_message_before_gathering', None) 
                        if last_model_msg_content_from_context and isinstance(last_model_msg_content_from_context, Content):
                            current_history_for_gathering_ai.append(last_model_msg_content_from_context)
                            if last_model_msg_content_from_context.parts and last_model_msg_content_from_context.parts[0].text:
                                logging.debug(f"    (Wątek) Dodano ostatnią wiadomość modelu do historii AI zbierającego: '{last_model_msg_content_from_context.parts[0].text[:70]}...'")
                        else:
                            logging.debug(f"    (Wątek) Brak 'last_model_message_before_gathering' w kontekście dla AI zbierającego.")
                        user_msg_for_ai = "Dobrze, jestem gotów/gotowa podać dane ucznia."
                        trigger_gathering_ai_immediately = False
                    context_for_gathering_ai = context_data_to_save.copy()
                    ai_response_text_raw = get_gemini_gathering_response(
                        actual_user_psid, current_history_for_gathering_ai, user_msg_for_ai,
                        context_for_gathering_ai, current_page_token
                    )
                    if ai_response_text_raw:
                        model_resp_content = Content(role="model", parts=[Part.from_text(ai_response_text_raw)])
                        if ai_response_text_raw.strip() == SWITCH_TO_GENERAL:
                            context_data_to_save.update({
                                'return_to_state': STATE_GATHERING_INFO,
                                'return_to_context': context_for_gathering_ai,
                                'type': STATE_GENERAL
                            })
                            next_state = STATE_GENERAL; action = 'handle_general'
                            current_state = next_state; msg_result = None
                        elif INFO_GATHERED_MARKER in ai_response_text_raw:
                            response_parts = ai_response_text_raw.split(INFO_GATHERED_MARKER, 1)
                            ai_response_before_marker = response_parts[0].strip()
                            final_message_to_user = ""
                            data_match = re.search(
                                r"ZEBRANE_DANE_UCZNIA:\s*\[Imię:\s*(.*?),?\s*Nazwisko:\s*(.*?),?\s*KlasaInfo:\s*(.*?),?\s*Poziom:\s*(.*?)\]",
                                ai_response_before_marker, re.IGNORECASE | re.DOTALL
                            )
                            parsed_student_data = {}
                            if data_match:
                                parsed_student_data['student_first_name'] = data_match.group(1).strip()
                                parsed_student_data['student_last_name'] = data_match.group(2).strip()
                                parsed_student_data['grade_info'] = data_match.group(3).strip()
                                parsed_student_data['level_info'] = data_match.group(4).strip()
                                final_message_to_user = ai_response_before_marker[data_match.end():].strip()
                                logging.info(f"    (Wątek) [{actual_user_psid}] Parsowane dane ucznia z AI: {parsed_student_data}")
                            else:
                                logging.warning(f"    (Wątek) [{actual_user_psid}] Nie znaleziono ZEBRANE_DANE_UCZNIA w: '{ai_response_before_marker[:100]}...'. Używam kontekstu.")
                                final_message_to_user = ai_response_before_marker
                                parsed_student_data['student_first_name'] = context_data_to_save.get('known_student_first_name', 'Brak')
                                parsed_student_data['student_last_name'] = context_data_to_save.get('known_student_last_name', 'Brak')
                                parsed_student_data['grade_info'] = context_data_to_save.get('known_grade', 'Brak')
                                parsed_student_data['level_info'] = context_data_to_save.get('known_level', 'Brak')
                            if not final_message_to_user:
                                final_message_to_user = "Dziękujemy za podanie informacji. Rezerwacja została wstępnie przyjęta. Prosimy o ostateczne potwierdzenie zajęć poprzez wysłanie wiadomości \"POTWIERDZAM\" na profil Facebook: https://www.facebook.com/profile.php?id=61576135251276. Ten profil służy również do dalszego kontaktu w sprawie zajęć."
                            msg_result = final_message_to_user
                            parsed_student_data['parent_first_name'] = context_data_to_save.get('known_parent_first_name', '')
                            parsed_student_data['parent_last_name'] = context_data_to_save.get('known_parent_last_name', '')
                            record_id_for_update = context_data_to_save.get('airtable_record_id')
                            update_ok, update_message = update_airtable_record_phase2(record_id_for_update, parsed_student_data)
                            if not update_ok: 
                                logging.error(f"    (Wątek) [{actual_user_psid}] Błąd Fazy 2 w arkuszu: {update_message}")
                            next_state = STATE_GENERAL
                            context_data_to_save = {'type': STATE_GENERAL, 'required_subject': current_subject, '_just_reset': True}
                        else:
                            msg_result = ai_response_text_raw
                            next_state = STATE_GATHERING_INFO
                            context_data_to_save['type'] = STATE_GATHERING_INFO 
                    else:
                        msg_result = "Przepraszam, mam chwilowy problem z systemem zbierania informacji. Spróbujmy jeszcze raz za chwilę."
                        model_resp_content = Content(role="model", parts=[Part.from_text(msg_result)])
                        next_state = STATE_GATHERING_INFO
                        context_data_to_save['type'] = STATE_GATHERING_INFO
                except Exception as gather_err:
                    logging.error(f"(Wątek) [{actual_user_psid}] Krytyczny błąd w logice 'handle_gathering': {gather_err}", exc_info=True)
                    msg_result = "Przepraszam, wystąpił poważny błąd systemu zbierania danych. Spróbuj ponownie później."
                    model_resp_content = Content(role="model", parts=[Part.from_text(msg_result)])
                    next_state = STATE_GENERAL
                    context_data_to_save = {'type': STATE_GENERAL, 'required_subject': current_subject, '_just_reset': True}

            elif current_action_in_loop == 'send_info':
                if msg_result and not model_resp_content: 
                    model_resp_content = Content(role="model", parts=[Part.from_text(msg_result)])
                if 'type' not in context_data_to_save: context_data_to_save['type'] = next_state
                if 'required_subject' not in context_data_to_save: context_data_to_save['required_subject'] = current_subject
            else: 
                if current_action_in_loop:
                    logging.error(f"    (Wątek) [{actual_user_psid}] Nieznana akcja '{current_action_in_loop}' w pętli.")
                pass 

        final_context_to_save_dict = context_data_to_save.copy()
        final_context_to_save_dict['type'] = next_state 
        if 'required_subject' not in final_context_to_save_dict: 
            final_context_to_save_dict['required_subject'] = current_subject
        
        if next_state != STATE_GENERAL or 'return_to_state' not in final_context_to_save_dict:
                final_context_to_save_dict.pop('return_to_state', None)
                final_context_to_save_dict.pop('return_to_context', None)

        if msg_result:
            send_message(actual_user_psid, msg_result, current_page_token)
        elif current_action_in_loop and not action: 
            logging.debug(f"    (Wątek) [{actual_user_psid}] Ostatnia akcja '{current_action_in_loop}' zakończona bez bezpośredniej wiadomości do wysłania.")

        original_context_no_return = context.copy()
        original_context_no_return.pop('return_to_state', None) 
        original_context_no_return.pop('return_to_context', None)

        should_save = (bool(user_content) or bool(model_resp_content) or
                       (original_context_no_return != final_context_to_save_dict))
        
        if should_save:
            history_to_save_final = [h for h in history_for_gemini if isinstance(h, Content) and h.role in ('user', 'model')]
            if user_content: 
                history_to_save_final.append(user_content)
            if model_resp_content: 
                history_to_save_final.append(model_resp_content)
            history_to_save_final = history_to_save_final[-(MAX_HISTORY_TURNS * 2):] 
            logging.info(f"    (Wątek) [{actual_user_psid}] Zapisywanie historii ({len(history_to_save_final)} wiad.). Stan: {final_context_to_save_dict.get('type')}, Przedmiot: {final_context_to_save_dict.get('required_subject')}")
            save_history(actual_user_psid, history_to_save_final, context_to_save=final_context_to_save_dict)
        else:
            logging.debug(f"    (Wątek) [{actual_user_psid}] Brak zmian w historii lub kontekście (poza kluczami powrotu) - pomijanie zapisu.")

        logging.info(f"--- (Wątek) Zakończono przetwarzanie eventu dla Strony: '{current_page_name}', User PSID: {actual_user_psid} ---")

    except Exception as e_thread:
        event_mid = event_payload.get('message', {}).get('mid', 'N/A') if isinstance(event_payload, dict) else 'N/A_event_payload_not_dict'
        logging.critical(f"KRYTYCZNY BŁĄD W WĄTKU PRZETWARZANIA ZDARZENIA (event MID: {event_mid}): {e_thread}", exc_info=True)

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

def get_airtable_booked_slots(start_datetime, end_datetime):
    """Pobiera zajęte sloty z tabeli Rezerwacje w Airtable."""
    if not airtable_api:
        logging.error("Airtable API jest niedostępne. Nie można pobrać zajętych slotów.")
        return []
        
    try:
        table = airtable_api.table(AIRTABLE_BASE_ID, AIRTABLE_BOOKINGS_TABLE_NAME)
        tz_cal = _get_calendar_timezone()
        tz_airtable = pytz.timezone(AIRTABLE_TIMEZONE)

        start_dt_airtable_tz = start_datetime.astimezone(tz_airtable)
        end_dt_airtable_tz = end_datetime.astimezone(tz_airtable)
        
        # POPRAWKA: Użyj nazwy kolumny 'Date' i prawidłowej formuły dla Airtable
        formula = f"AND(IS_AFTER({{Date}}, DATETIME_PARSE('{start_dt_airtable_tz.isoformat()}')), IS_BEFORE({{Date}}, DATETIME_PARSE('{end_dt_airtable_tz.isoformat()}')))"
        
        logging.debug(f"Airtable: Odczyt z '{AIRTABLE_BOOKINGS_TABLE_NAME}' z formułą: {formula}")
        # POPRAWKA: nazwy pól muszą odpowiadać Airtable
        records = table.all(formula=formula, fields=['Date', 'Nazwa Kalendarza'])
        
        airtable_busy_slots = []
        duration_delta = datetime.timedelta(minutes=APPOINTMENT_DURATION_MINUTES)

        for record in records:
            fields = record.get('fields', {})
            datetime_str = fields.get('Date') # POPRAWKA
            calendar_name_str = fields.get('Nazwa Kalendarza', '')

            if not datetime_str or not calendar_name_str:
                continue

            try:
                slot_start_utc = datetime.datetime.fromisoformat(datetime_str.replace('Z', '+00:00'))
                slot_start_cal_tz = slot_start_utc.astimezone(tz_cal)
                slot_end_cal_tz = slot_start_cal_tz + duration_delta
                
                airtable_busy_slots.append({
                    'start': slot_start_cal_tz,
                    'end': slot_end_cal_tz,
                    'calendar_name': calendar_name_str
                })
            except (ValueError, TypeError) as e:
                logging.warning(f"Airtable: Błąd parsowania daty z rekordu {record['id']}: '{datetime_str}'. Błąd: {e}")

        logging.info(f"Airtable: Znaleziono {len(airtable_busy_slots)} zajętych slotów.")
        return airtable_busy_slots

    except Exception as e:
        logging.error(f"Błąd podczas pobierania danych z Airtable: {e}", exc_info=True)
        return []


def get_free_time_ranges(calendar_config_list, start_datetime, end_datetime):
    """
    Pobiera listę wolnych zakresów czasowych, łącząc zajęte sloty
    z Kalendarza Google i bazy danych Airtable.
    """
    tz = _get_calendar_timezone()

    # Krok 1: Upewnij się, że daty graniczne są świadome strefy czasowej
    if start_datetime.tzinfo is None: start_datetime = tz.localize(start_datetime)
    else: start_datetime = start_datetime.astimezone(tz)
    
    if end_datetime.tzinfo is None: end_datetime = tz.localize(end_datetime)
    else: end_datetime = end_datetime.astimezone(tz)

    now = datetime.datetime.now(tz)
    search_start = max(start_datetime, now)

    if search_start >= end_datetime:
        logging.info(f"Zakres [{search_start:%Y-%m-%d %H:%M}]-[{end_datetime:%Y-%m-%d %H:%M}] jest nieprawidłowy/w przeszłości.")
        return []

    calendar_ids_to_check = [c['id'] for c in calendar_config_list if 'id' in c]
    logging.info(f"Szukanie wolnych zakresów w kalendarzach: {[c['name'] for c in calendar_config_list]}")

    # Krok 2: Pobierz zajęte sloty z OBU źródeł
    busy_from_gcal = get_calendar_busy_slots(calendar_ids_to_check, search_start, end_datetime)
    busy_from_airtable = get_airtable_booked_slots(search_start, end_datetime)

    # Krok 3: Połącz i posortuj wszystkie zajęte sloty
    all_busy_slots = sorted(busy_from_gcal + busy_from_airtable, key=lambda x: x['start'])

    # Krok 4: Scal nakładające się zajęte sloty w jeden ciągły blok
    merged_busy = []
    if all_busy_slots:
        current_busy = all_busy_slots[0].copy()
        for next_busy in all_busy_slots[1:]:
            if next_busy['start'] < current_busy['end']:
                current_busy['end'] = max(current_busy['end'], next_busy['end'])
            else:
                merged_busy.append(current_busy)
                current_busy = next_busy.copy()
        merged_busy.append(current_busy)

    # Krok 5: Wygeneruj wolne zakresy na podstawie scalonych zajętych
    free_ranges = []
    current_time = search_start
    for busy in merged_busy:
        if current_time < busy['start']:
            free_ranges.append({'start': current_time, 'end': busy['start']})
        current_time = max(current_time, busy['end'])
    
    if current_time < end_datetime:
        free_ranges.append({'start': current_time, 'end': end_datetime})

    # Krok 6: Zastosuj filtry godzin pracy i minimalnego czasu rezerwacji
    final_free_slots = []
    min_duration = datetime.timedelta(minutes=APPOINTMENT_DURATION_MINUTES)
    min_booking_time = now + datetime.timedelta(hours=MIN_BOOKING_LEAD_HOURS)
    work_start_time = datetime.time(WORK_START_HOUR, 0)
    work_end_time = datetime.time(WORK_END_HOUR, 0)

    for free_range in free_ranges:
        current_day_start = free_range['start']
        while current_day_start < free_range['end']:
            day = current_day_start.date()
            day_work_start = tz.localize(datetime.datetime.combine(day, work_start_time))
            day_work_end = tz.localize(datetime.datetime.combine(day, work_end_time))
            
            # Znajdź część wspólną wolnego zakresu i godzin pracy
            slot_start = max(current_day_start, day_work_start)
            slot_end = min(free_range['end'], day_work_end)
            
            # Zastosuj filtr minimalnego czasu rezerwacji
            slot_start = max(slot_start, min_booking_time)

            if slot_start < slot_end and (slot_end - slot_start) >= min_duration:
                final_free_slots.append({'start': slot_start, 'end': slot_end})
            
            # Przejdź do następnego dnia
            next_day_date = day + datetime.timedelta(days=1)
            current_day_start = tz.localize(datetime.datetime.combine(next_day_date, datetime.time.min))

    logging.info(f"Znaleziono {len(final_free_slots)} ostatecznych wolnych zakresów po wszystkich filtrach.")
    return final_free_slots

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
# === FUNKCJE AIRTABLE (ZAPIS + ODCZYT) ===============================
# =====================================================================

def create_airtable_record_phase1(psid, start_time, calendar_name, subject):
    """Zapisuje dane Fazy 1 do tabeli 'Rezerwacje' w Airtable."""
    if not airtable_api:
        return False, "Błąd połączenia z Airtable (Faza 1)."
    try:
        table = airtable_api.table(AIRTABLE_BASE_ID, AIRTABLE_BOOKINGS_TABLE_NAME)
        
        # Airtable preferuje daty w formacie ISO 8601 w strefie UTC (z 'Z' na końcu)
        start_time_utc = start_time.astimezone(pytz.utc)
        
        # Upewnij się, że nazwy pól odpowiadają DOKŁADNIE nazwom kolumn w Airtable
        # Na podstawie Twoich zrzutów ekranu!
        record_data = {
            'PSID': psid,
            'Date': start_time_utc.isoformat(), # Używam 'Date' jak na zrzucie ekranu
            'Nazwa Kalendarza': calendar_name,
            'Przedmiot': subject, # Upewnij się, że masz taką kolumnę!
            'Status': 'Oczekiwanie na dane ucznia' # Dobra praktyka, by śledzić status
        }
        
        logging.info(f"Próba zapisu Fazy 1 (Airtable) do '{AIRTABLE_BOOKINGS_TABLE_NAME}': {record_data}")
        created_record = table.create(record_data)
        
        record_id = created_record['id']
        logging.info(f"Zapisano Faza 1 (Airtable) pomyślnie. Record ID: {record_id}")
        
        # Logowanie statystyki po udanym zapisie
        log_statistic("booking")
        
        return True, record_id
        
    except Exception as e:
        logging.error(f"Błąd podczas zapisu Fazy 1 do Airtable: {e}", exc_info=True)
        return False, "Błąd systemu podczas zapisu do bazy danych."

def update_airtable_record_phase2(record_id, student_data):
    """Aktualizuje rekord w Airtable danymi Fazy 2."""
    if not airtable_api:
        return False, "Błąd połączenia z Airtable (Faza 2)."
    if not record_id:
        return False, "Brak ID rekordu do aktualizacji."
        
    try:
        table = airtable_api.table(AIRTABLE_BASE_ID, AIRTABLE_BOOKINGS_TABLE_NAME)
        
        parent_fn = student_data.get('parent_first_name', '')
        parent_ln = student_data.get('parent_last_name', '')
        student_fn = student_data.get('student_first_name', '')
        student_ln = student_data.get('student_last_name', '')
        grade_info = student_data.get('grade_info', '')
        level_info = student_data.get('level_info', '')
        
        # Użyj swojej istniejącej funkcji do ekstrakcji danych
        numerical_grade, _, school_type = extract_school_type(grade_info)
        
        # Upewnij się, że nazwy pól odpowiadają DOKŁADNIE nazwom kolumn w Airtable
        update_data = {
            'Imię Rodzica': parent_fn,
            'Nazwisko Rodzica': parent_ln,
            'Imię Ucznia': student_fn,
            'Nazwisko Ucznia': student_ln,
            'Klasa': numerical_grade, # Zapisujemy tylko numer
            'Typ Szkoły': school_type,
            'Poziom': level_info if level_info != 'Brak' else '',
            'Status': 'Dane zebrane - oczekiwanie na potwierdzenie'
        }
        
        logging.info(f"Aktualizacja Fazy 2 (Airtable) rekordu {record_id} danymi: {update_data}")
        table.update(record_id, update_data)
        logging.info(f"Zaktualizowano rekord {record_id} pomyślnie.")
        
        return True, None # Sukces
        
    except Exception as e:
        logging.error(f"Błąd podczas aktualizacji Fazy 2 w Airtable: {e}", exc_info=True)
        return False, "Błąd systemu podczas aktualizacji w bazie danych."

def log_statistic(event_type):
    """Loguje statystykę (new_contact lub booking) w tabeli 'Statystyki' w Airtable."""
    if not airtable_api:
        logging.error("[Stats] Nie można zalogować statystyki - usługa Airtable niedostępna.")
        return

    try:
        table = airtable_api.table(AIRTABLE_BASE_ID, AIRTABLE_STATS_TABLE_NAME)
        
        # Nazwa pola do inkrementacji na podstawie typu zdarzenia
        # Zgodnie z Twoim zrzutem ekranu: 'Nowe Kontakty' i 'Rezerwacje'
        field_to_increment = None
        if event_type == "new_contact":
            field_to_increment = "Nowe Kontakty"
        elif event_type == "booking":
            field_to_increment = "Rezerwacje" # Nazwa kolumny zliczającej rezerwacje
        else:
            logging.error(f"[Stats] Nieznany typ zdarzenia statystycznego: {event_type}")
            return
            
        # Pobierz dzisiejszą datę w formacie YYYY-MM-DD
        today_str = datetime.datetime.now(pytz.timezone(AIRTABLE_TIMEZONE)).strftime('%Y-%m-%d')
        
        # Sprawdź, czy istnieje już rekord dla dzisiejszej daty
        # W Airtable pole typu "Date" jest przechowywane w formacie ISO, musimy odpowiednio sformatować formułę
        formula = f"IS_SAME({{Data}}, '{today_str}', 'day')"
        existing_record = table.first(formula=formula)
        
        if existing_record:
            # Rekord istnieje - odczytaj i zaktualizuj
            record_id = existing_record['id']
            current_value = existing_record.get('fields', {}).get(field_to_increment, 0)
            new_value = (current_value or 0) + 1
            
            logging.info(f"[Stats] Aktualizacja statystyk dla {today_str}: {field_to_increment} z {current_value} na {new_value}")
            table.update(record_id, {field_to_increment: new_value})
        else:
            # Rekord nie istnieje - utwórz nowy
            new_record_data = {
                "Data": today_str,
                field_to_increment: 1
            }
            logging.info(f"[Stats] Tworzenie nowego rekordu statystyk dla {today_str}: {new_record_data}")
            table.create(new_record_data)

    except Exception as e:
        logging.error(f"[Stats] Ogólny błąd podczas logowania statystyki '{event_type}' w Airtable: {e}", exc_info=True)


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

def _call_gemini(user_psid, prompt_history, generation_config, task_name, page_access_token, max_retries=3, user_message=None):
    """Wywołuje API Gemini z obsługą błędów i ponowień."""
    if not gemini_model:
        logging.error(f"!!! [{user_psid}] KRYTYCZNY BŁĄD: Model Gemini ({task_name}) niedostępny (gemini_model is None)!")
        return "Przepraszam, wystąpił wewnętrzny błąd systemu. Spróbuj ponownie później."

    if not isinstance(prompt_history, list) or not all(isinstance(item, Content) for item in prompt_history):
        logging.error(f"!!! [{user_psid}] Nieprawidłowy format historii promptu przekazany do _call_gemini ({task_name}). Typ: {type(prompt_history)}")
        return "Przepraszam, wystąpił błąd przetwarzania wewnętrznego."

    # Przygotuj finalny prompt dla API
    # prompt_history tutaj powinno już zawierać instrukcję systemową jako pierwsze elementy
    final_prompt_for_api = list(prompt_history) # Skopiuj historię
    if user_message: # Jeśli jest nowa wiadomość użytkownika, dodaj ją
        if not isinstance(user_message, str):
            logging.warning(f"[{user_psid}] _call_gemini otrzymało user_message, które nie jest stringiem: {type(user_message)}. Konwertuję na string.")
            user_message = str(user_message)
        # Dodaj tylko jeśli user_message nie jest pustym stringiem po konwersji i strip()
        if user_message.strip():
            final_prompt_for_api.append(Content(role="user", parts=[Part.from_text(user_message.strip())]))
        else:
            logging.debug(f"[{user_psid}] _call_gemini otrzymało pusty user_message po strip(). Nie dodaję do promptu.")


    logging.info(f"[{user_psid}] Wywołanie Gemini: {task_name} (Prompt: {len(final_prompt_for_api)} wiadomości)")

    # Logowanie ostatniej wiadomości użytkownika dla kontekstu (tej, która została właśnie dodana, jeśli była)
    last_user_msg_to_log = None
    if final_prompt_for_api and final_prompt_for_api[-1].role == 'user':
        last_user_msg_to_log = final_prompt_for_api[-1].parts[0].text if final_prompt_for_api[-1].parts else "[Brak treści w ostatniej wiadomości użytkownika]"

    if last_user_msg_to_log:
        log_msg_content = f"'{last_user_msg_to_log[:200]}{'...' if len(last_user_msg_to_log)>200 else ''}'"
        logging.debug(f"    Ostatnia wiad. usera przekazana do AI ({task_name}): {log_msg_content}")
    else:
        logging.debug(f"    Brak wiadomości użytkownika na końcu promptu przekazanego do AI ({task_name}).")


    attempt = 0
    while attempt < max_retries:
        attempt += 1
        logging.debug(f"    Próba wywołania Gemini {attempt}/{max_retries} ({task_name})...")
        try:
            _simulate_typing(user_psid, MIN_TYPING_DELAY_SECONDS * 0.8, page_access_token)

            response = gemini_model.generate_content(
                final_prompt_for_api, # Użyj final_prompt_for_api
                generation_config=generation_config,
                safety_settings=SAFETY_SETTINGS,
                stream=False
            )

            if not response:
                 logging.warning(f"[{user_psid}] Gemini ({task_name}) zwróciło pustą odpowiedź (None).")
                 if attempt < max_retries:
                     time.sleep(1 + random.random())
                     continue
                 else:
                     return "Przepraszam, nie udało się uzyskać odpowiedzi od AI."

            if not response.candidates:
                prompt_feedback = response.prompt_feedback if hasattr(response, 'prompt_feedback') else None
                if prompt_feedback and hasattr(prompt_feedback, 'block_reason') and prompt_feedback.block_reason != 0:
                     block_reason_name = prompt_feedback.block_reason.name if hasattr(prompt_feedback.block_reason, 'name') else str(prompt_feedback.block_reason)
                     logging.error(f"!!! BŁĄD [{user_psid}] Gemini ({task_name}) - PROMPT ZABLOKOWANY! Powód: {block_reason_name}. Feedback: {prompt_feedback}")
                     return "Przepraszam, Twoja wiadomość nie mogła zostać przetworzona ze względu na zasady bezpieczeństwa."
                else:
                    logging.warning(f"[{user_psid}] Gemini ({task_name}) brak kandydatów w odpowiedzi. Feedback promptu: {prompt_feedback}")
                    if attempt < max_retries: time.sleep(1.5 * attempt * random.uniform(0.8, 1.2)); continue
                    else: return "Przepraszam, problem z generowaniem odpowiedzi (brak kandydatów)."

            candidate = response.candidates[0]
            finish_reason = candidate.finish_reason if hasattr(candidate, 'finish_reason') else None
            finish_reason_val = finish_reason.value if finish_reason else 0

            if finish_reason_val != 1: # STOP
                finish_reason_name = finish_reason.name if hasattr(finish_reason, 'name') else str(finish_reason_val or 'UNKNOWN')
                safety_ratings = candidate.safety_ratings if hasattr(candidate, 'safety_ratings') else "Brak danych safety"
                logging.warning(f"[{user_psid}] Gemini ({task_name}) ZAKOŃCZONE NIEPRAWIDŁOWO! Powód: {finish_reason_name}. Safety: {safety_ratings}")
                if finish_reason_val in [3, 4]: # SAFETY or RECITATION
                    if attempt < max_retries: time.sleep(1.5 * attempt * random.uniform(0.8, 1.2)); continue
                    else:
                        if finish_reason_val == 3: return "Przepraszam, nie mogę wygenerować odpowiedzi ze względu na zasady bezpieczeństwa."
                        if finish_reason_val == 4: return "Przepraszam, nie mogę wygenerować odpowiedzi, ponieważ naruszałaby zasady cytowania."
                elif finish_reason_val == 2: # MAX_TOKENS
                     partial_text = "".join(part.text for part in candidate.content.parts if hasattr(candidate.content, 'parts') and hasattr(part, 'text')).strip()
                     if partial_text: return partial_text + "..."
                     else:
                         if attempt < max_retries: time.sleep(1.5 * attempt * random.uniform(0.8, 1.2)); continue
                         else: return "Przepraszam, wygenerowana odpowiedź była zbyt długa."
                else: # OTHER
                    if attempt < max_retries: time.sleep(1.5 * attempt * random.uniform(0.8, 1.2)); continue
                    else: return f"Przepraszam, problem z generowaniem odpowiedzi (kod: {finish_reason_name})."

            if hasattr(candidate, 'content') and candidate.content and hasattr(candidate.content, 'parts') and candidate.content.parts:
                generated_text = "".join(part.text for part in candidate.content.parts if hasattr(part, 'text')).strip()
                if generated_text:
                    logging.info(f"[{user_psid}] Gemini ({task_name}) zwróciło poprawną odpowiedź (długość: {len(generated_text)}).")
                    logging.debug(f"    Odpowiedź Gemini ({task_name}): '{generated_text[:300]}{'...' if len(generated_text)>300 else ''}'")
                    return generated_text
                else:
                    logging.warning(f"[{user_psid}] Gemini ({task_name}) zwróciło pustą treść mimo FinishReason=STOP.")
                    if attempt < max_retries: time.sleep(1 + random.random()); continue
                    else: return "Przepraszam, problem z wygenerowaniem odpowiedzi (pusta treść)."
            else:
                logging.warning(f"[{user_psid}] Gemini ({task_name}) zwróciło kandydata bez treści mimo FinishReason=STOP.")
                if attempt < max_retries: time.sleep(1 + random.random()); continue
                else: return "Przepraszam, problem z wygenerowaniem odpowiedzi (brak struktury treści)."

        except HttpError as http_err:
            status_code = http_err.resp.status if hasattr(http_err, 'resp') and hasattr(http_err.resp, 'status') else 'Nieznany'
            reason = http_err.resp.reason if hasattr(http_err, 'resp') and hasattr(http_err.resp, 'reason') else 'Nieznany'
            logging.error(f"!!! BŁĄD HTTP ({status_code} {reason}) [{user_psid}] Gemini ({task_name}) - Próba {attempt}/{max_retries}.")
            if status_code in [429, 500, 503] and attempt < max_retries:
                sleep_time = (2 ** attempt) + random.uniform(0, 1)
                logging.warning(f"    Oczekiwanie {sleep_time:.2f}s przed ponowieniem z powodu błędu {status_code}...")
                time.sleep(sleep_time); continue
            else: return f"Przepraszam, błąd komunikacji z AI (HTTP {status_code})."
        except Exception as e:
            if isinstance(e, NameError) and 'gemini_model' in str(e):
                 logging.critical(f"!!! KRYTYCZNY NameError [{user_psid}]: {e}. 'gemini_model' nie jest zdefiniowany!", exc_info=True)
                 return "Przepraszam, krytyczny błąd wewnętrzny systemu AI."
            else:
                 logging.error(f"!!! BŁĄD Python [{user_psid}] podczas wywołania Gemini ({task_name}) - Próba {attempt}/{max_retries}: {e}", exc_info=True)
                 if attempt < max_retries:
                     sleep_time = (2 ** attempt) + random.uniform(0, 1)
                     logging.warning(f"    Nieoczekiwany błąd Python. Oczekiwanie {sleep_time:.2f}s przed ponowieniem...")
                     time.sleep(sleep_time); continue
                 else: return "Przepraszam, wystąpił nieoczekiwany błąd przetwarzania."

    logging.error(f"!!! KRYTYCZNY BŁĄD [{user_psid}] Gemini ({task_name}) - Nie udało się uzyskać poprawnej odpowiedzi po {max_retries} próbach.")
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
*   ZAWSZE jeśli proponujesz godzinę ma zaokrąglać się do 10 minut np 16:20, 17:40. NIGDY nie proponuj godzin z jakąś liczbą na końcu np 14:49
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
5.  **Zakończ NATYCHMIAST BEZ ŻADNYCH DODATKOWYCH PYTAŃ CZY KOMENTARZY PRZED LINIĄ ZEBRANE_DANE_UCZNIA po zebraniu danych UCZNIA:** Kiedy uznasz, że masz już **wszystkie wymagane informacje o UCZNIU** (Nie pytaj klienta czy to już wsystkie informacje, sam masz to wywnioskować) (Imię, Nazwisko, Klasa+Szkoła, ewentualnie Poziom), Twoja ostatnia odpowiedź **MUSI** mieć następującą strukturę:
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
AVAILABLE_SUBJECTS = sorted(list(ALL_SUBJECT_LINKS.keys())) # Musi być zdefiniowane przed użyciem

# ZASTĄP CAŁĄ TĘ ZMIENNĄ
SYSTEM_INSTRUCTION_GENERAL_RAW = """Jesteś przyjaznym, proaktywnym i profesjonalnym asystentem klienta centrum korepetycji. Twoim głównym celem jest przeprowadzenie klienta przez proces zapoznania się z ofertą i zachęcenie go do umówienia pierwszej lekcji.

**Styl Komunikacji:**
*   **Naturalność:** Zachowuj się jak człowiek, unikaj schematycznych i powtarzalnych odpowiedzi.
*   **Umiar:** Nie używaj nadmiernie wykrzykników ani entuzjastycznych sformułowań typu "Super!", "Jasne!".
*   **Bez Emotek:** Nie używaj emotikon.
*   **Profesjonalizm:** Bądź uprzejmy, rzeczowy i pomocny. Używaj zwrotów "Państwo".
*   **Język:** Odpowiadaj płynnie po polsku.

**Dostępne Przedmioty (ogólnie):** {available_subjects_list}

{dynamic_subject_link_info}

**Cennik (za 60 minut):**
*   Szkoła Podstawowa: 60 zł
*   Liceum/Technikum (Poziom Podstawowy, klasa 1-3): 65 zł
*   Liceum/Technikum (Poziom Podstawowy, klasa 4/5): 70 zł
*   Liceum/Technikum (Poziom Rozszerzony, klasa 1-3): 70 zł
*   Liceum/Technikum (Poziom Rozszerzony, klasa 4/5): 75 zł

**Format Lekcji:** Online, przez platformę Microsoft Teams (bez konieczności instalacji, wystarczy link).

**Format Odpowiedzi (NOWA ZASADA):**
*   Gdy po raz pierwszy zbierzesz informacje o klasie i poziomie, **TWOJA ODPOWIEDŹ MUSI ZAWIERAĆ** specjalny znacznik z tymi danymi. Wygląda on tak: `[DANE_UCZNIA_OGOLNE: KlasaInfo: <pełna informacja o klasie i szkole>, Poziom: <Podstawowy/Rozszerzony/Brak>]`. Ten znacznik umieść **przed** swoją normalną wiadomością do klienta o cenie. Jeśli poziom nie dotyczy (np. szkoła podstawowa), wpisz "Brak".

**Twój Przepływ Pracy:**

1.  **Identyfikacja Potrzeby (PRZEDMIOT):**
    *   Jeśli ZNASZ już przedmiot (np. z kontekstu strony, który jest: {current_subject_from_page}), przywitaj się uprzejmie, potwierdź przedmiot.

2.  **Szybka informacja:**
    *   Poinformuj o innych przedmiotach i podaj linki.

3.  **Zbieranie Informacji o Uczniu:**
    *   Zapytaj o klasę i typ szkoły (podstawowa, liceum/technikum). Jeśli to szkoła średnia, zapytaj też o poziom (podstawowy/rozszerzony).
    *   Jeśli poziom szkoły podstawowej jest poniżej 4 klasy, poinformuj, że nie udzielacie korepetycji dla takiego poziomu.

4.  **Prezentacja Ceny i Formatu:**
    *   Na podstawie zebranych informacji ustal właściwą cenę.
    *   **Zbuduj odpowiedź:** Najpierw wstaw znacznik `[DANE_UCZNIA_OGOLNE: ...]`, a potem w nowej linii napisz wiadomość dla klienta, informując go o cenie i formacie lekcji.
    *   Przykład:
        `[DANE_UCZNIA_OGOLNE: KlasaInfo: 8 klasa podstawówki, Poziom: Brak]`
        `Dla ucznia w 8 klasie szkoły podstawowej koszt zajęć wynosi 60 zł za 60 minut. Wszystkie zajęcia odbywają się online...`

5.  **Zachęta do Umówienia Lekcji:**
    *   Po podaniu ceny, bezpośrednio zapytaj, czy klient jest zainteresowany umówieniem lekcji.

6.  **Obsługa Odpowiedzi na Propozycję Lekcji:**
    *   **Jeśli TAK:** Twoja odpowiedź musi zawierać **TYLKO I WYŁĄCZNIE** znacznik: `{intent_marker}`.
    *   **Jeśli NIE (lub wahanie):** Postępuj jak dotychczas.

7.  **Obsługa Powrotu (tryb tymczasowy):**
    *   Postępuj jak dotychczas.

**Zasady Dodatkowe:**
*   Pamiętaj o dodaniu znacznika `[DANE_UCZNIA_OGOLNE: ...]` gdy pierwszy raz informujesz o cenie.
*   Reszta zasad pozostaje bez zmian.
"""

SYSTEM_INSTRUCTION_GENERAL = SYSTEM_INSTRUCTION_GENERAL_RAW.format(
    available_subjects_list=", ".join(AVAILABLE_SUBJECTS),
    intent_marker=INTENT_SCHEDULE_MARKER,
    return_marker=RETURN_TO_PREVIOUS,
    dynamic_subject_link_info="{dynamic_subject_link_info}",  # Zachowaj ten placeholder
    current_subject_from_page="{current_subject_from_page}"    # Zachowaj ten placeholder
)

# Ten print jest nadal użyteczny do weryfikacji
print("--- Wartość SYSTEM_INSTRUCTION_GENERAL po globalnym formacie ---")
start_idx = SYSTEM_INSTRUCTION_GENERAL.find("**Dostępne Przedmioty (ogólnie):**")
end_idx = SYSTEM_INSTRUCTION_GENERAL.find("**Cennik (za 60 minut):**")
if start_idx != -1 and end_idx != -1:
    # Wydrukuj fragment zawierający miejsce, gdzie powinien być {dynamic_subject_link_info}
    print(SYSTEM_INSTRUCTION_GENERAL[start_idx : end_idx])
else:
    print(SYSTEM_INSTRUCTION_GENERAL[:1000])
print("----------------------------------------------------------------")

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
    print(f"DEBUG PRINT GATHERING AI [{user_psid}]: History: {history_for_gathering_ai}, Current User Msg: '{current_user_message_text}', Context Info: {context_info}")
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
# Zmień definicję funkcji:
def get_gemini_general_response(user_psid, current_user_message_text, history_for_general_ai, is_temporary_general_state, page_access_token, current_subject_for_context=None): # <--- ZMIENIONA NAZWA
    """Prowadzi ogólną rozmowę z AI."""
    if not gemini_model:
        logging.error(f"!!! [{user_psid}] Model Gemini niedostępny (General)!")
        return None

    # Przygotuj tekst z informacjami o linkach dla AI
    link_data_for_ai_parts = []
    if PAGE_CONFIG:
        for page_id, page_data in PAGE_CONFIG.items():
            subj_name = page_data.get("subject")
            subj_link = page_data.get("link")
            if subj_name and subj_link:
                link_data_for_ai_parts.append(f"Strona dla '{subj_name}': {subj_link}")
    
    link_info_text_for_prompt = ""
    if link_data_for_ai_parts:
        link_info_text_for_prompt = "**Informacje o stronach i linkach do poszczególnych przedmiotów:**\n" + "\n".join(link_data_for_ai_parts)
        logging.debug(f"[{user_psid}] Przygotowano link_info_text_for_prompt dla AI (General):\n{link_info_text_for_prompt}")
    else:
        link_info_text_for_prompt = "**Informacje o stronach i linkach do poszczególnych przedmiotów:** Brak dostępnych danych."
        logging.warning(f"[{user_psid}] link_data_for_ai_parts jest pusta. AI (General) otrzyma informację o braku danych o linkach.")

    try:
        system_instruction_formatted_dynamically = SYSTEM_INSTRUCTION_GENERAL.format(
            dynamic_subject_link_info=link_info_text_for_prompt,
            current_subject_from_page=(current_subject_for_context  # <--- UŻYJ NOWEJ NAZWY TUTAJ
                                       if current_subject_for_context 
                                       else "nieokreślonego przedmiotu")
        )
    except KeyError as e:
        logging.error(f"!!! [{user_psid}] Błąd dynamicznego formatowania SYSTEM_INSTRUCTION_GENERAL: Brak klucza {e}. Sprawdź, czy wszystkie placeholdery są obsługiwane.")
        return "Przepraszam, wystąpił błąd konfiguracji asystenta."
    except Exception as format_err:
        logging.error(f"!!! [{user_psid}] Inny błąd formatowania SYSTEM_INSTRUCTION_GENERAL: {format_err}", exc_info=True)
        return "Przepraszam, wystąpił wewnętrzny błąd konfiguracji asystenta."

    model_ack_base = "Rozumiem. Będę asystentem klienta."
    if current_subject_for_context: # <--- UŻYJ NOWEJ NAZWY TUTAJ
        model_ack_base += f" Przedmiot '{current_subject_for_context}' jest już znany. Przejdę do pytania o klasę/szkołę." # <--- UŻYJ NOWEJ NAZWY TUTAJ
    else:
        model_ack_base += f" Najpierw ustalę przedmiot." 
    
    model_ack_base += f" Wykorzystam dostarczone informacje o linkach do stron, aby odpowiednio informować użytkownika."
    model_ack = model_ack_base + f" Po zebraniu danych i potwierdzeniu zainteresowania, zwrócę {INTENT_SCHEDULE_MARKER}."

    if is_temporary_general_state:
        model_ack += f" Będąc w trybie tymczasowym, po odpowiedzi na pytanie ogólne, jeśli użytkownik nie pyta dalej, dodam {RETURN_TO_PREVIOUS}."

    initial_prompt = [
        Content(role="user", parts=[Part.from_text(system_instruction_formatted_dynamically)]),
        Content(role="model", parts=[Part.from_text(model_ack)])
    ]
    
    full_prompt = initial_prompt + history_for_general_ai
    if current_user_message_text:
        if not isinstance(current_user_message_text, str):
            logging.warning(f"[{user_psid}] Wiadomość użytkownika nie jest stringiem (typ: {type(current_user_message_text)}), konwertuję na string.")
            current_user_message_text = str(current_user_message_text)
        
        if current_user_message_text.strip():
            full_prompt.append(Content(role="user", parts=[Part.from_text(current_user_message_text.strip())]))
        else:
            logging.debug(f"[{user_psid}] Pusta wiadomość użytkownika po strip(), nie dodaję do promptu AI (General).")

    max_prompt_messages = (MAX_HISTORY_TURNS * 2) + 2
    while len(full_prompt) > max_prompt_messages:
        if len(full_prompt) > 3:
             full_prompt.pop(2)
             if len(full_prompt) > 2:
                 full_prompt.pop(2)
        else:
             break

    response_text = _call_gemini(
        user_psid, 
        full_prompt, 
        GENERATION_CONFIG_DEFAULT, 
        "General Conversation", 
        page_access_token
    )

    if response_text:
        response_text = re.sub(rf"{re.escape(SLOT_ISO_MARKER_PREFIX)}.*?{re.escape(SLOT_ISO_MARKER_SUFFIX)}", "", response_text).strip()
        response_text = response_text.replace(INFO_GATHERED_MARKER, "").strip()
        response_text = response_text.replace(SWITCH_TO_GENERAL, "").strip()
        return response_text
    else:
        logging.error(f"!!! [{user_psid}] Nie uzyskano poprawnej odpowiedzi od Gemini (General). _call_gemini zwróciło None lub pusty string.")
        return "Przepraszam, mam chwilowy problem z przetworzeniem Twojej prośby. Spróbuj ponownie za moment."


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


@app.route('/webhook', methods=['POST'])
def webhook_handle():
    """Główny handler dla przychodzących zdarzeń z Messengera."""
    now_str = datetime.datetime.now(_get_calendar_timezone()).strftime('%Y-%m-%d %H:%M:%S %Z')
    logging.info(f"\n{'='*30} {now_str} POST /webhook {'='*30}")
    raw_data = request.data

    # 1. Zdekoduj dane
    data = None
    try:
        decoded_data = raw_data.decode('utf-8')
        data = json.loads(decoded_data)
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        logging.error(f"BŁĄD dekodowania danych przychodzących z FB: {e}", exc_info=True)
        logging.error(f"Surowe dane (fragment): {raw_data[:500]}...")
        # Mimo błędu, Facebook oczekuje odpowiedzi 200 OK, że coś odebraliśmy
        return Response("ERROR_DECODING_DATA_BUT_OK", status=200)

    # 2. Sprawdź podstawową strukturę (object: page)
    if not (data and data.get("object") == "page"):
        logging.warning(f"POST na /webhook, ale obiekt != 'page' lub brak danych. Typ: {type(data)}. Dane: {raw_data[:200]}...")
        return Response("INVALID_OBJECT_TYPE_BUT_OK", status=200) # FB oczekuje 200

    # 3. Jeśli dane są poprawne, przygotuj wątki do przetwarzania
    threads_to_run = []
    if data.get("entry"): # Upewnij się, że 'entry' istnieje
        for entry in data.get("entry", []):
            page_id_from_entry = entry.get("id") # ID strony, do której przyszło zdarzenie
            if entry.get("messaging"): # Upewnij się, że 'messaging' istnieje
                for event_item in entry.get("messaging", []): # event_item to pojedyncze zdarzenie
                    # Utwórz wątek dla każdego zdarzenia
                    # Ważne: event_item.copy() aby każdy wątek miał swoją kopię słownika zdarzenia
                    thread = threading.Thread(target=process_single_event, args=(event_item.copy(), page_id_from_entry))
                    threads_to_run.append(thread)
                    thread.start() # Uruchom wątek
            else:
                logging.debug(f"Brak klucza 'messaging' w entry dla page_id: {page_id_from_entry}. Dane entry: {entry}")
        
        if threads_to_run:
            logging.info(f"Uruchomiono {len(threads_to_run)} wątków do przetworzenia zdarzeń. Wysyłanie 200 OK do Facebooka.")
        else:
            logging.info("Brak zdarzeń 'messaging' do przetworzenia w otrzymanym requeście. Wysyłanie 200 OK do Facebooka.")
    else:
        logging.debug(f"Brak klucza 'entry' w danych od Facebooka. Dane: {data}")
        # Mimo to wyślij 200 OK
    
    # 4. Wyślij odpowiedź 200 OK do Facebooka JAK NAJSZYBCIEJ
    # Ta odpowiedź idzie teraz zanim wątki zakończą swoją pracę.
    return Response("EVENT_RECEIVED_QUEUED_FOR_PROCESSING", status=200)

    # Cała poprzednia logika pętli `for entry... for event...` została przeniesiona
    # do funkcji `process_single_event` i jest uruchamiana w wątkach.


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
# ======================== POCZĄTEK BLOKU DO WKLEJENIA ========================
    print("  Konfiguracja Airtable:")
    print(f"    ID Bazy: {AIRTABLE_BASE_ID}")
    print(f"    Tabela Rezerwacji: '{AIRTABLE_BOOKINGS_TABLE_NAME}'")
    print(f"    Tabela Statystyk: '{AIRTABLE_STATS_TABLE_NAME}'")
    print(f"    Klucz API: {'OK (załadowany)' if AIRTABLE_API_KEY and AIRTABLE_API_KEY != 'TU_WKLEJ_SWOJ_KLUCZ_API' else 'BRAK lub DOMYŚLNY!!!'}")
    print(f"    Usługa Airtable API: {'OK' if airtable_api else 'BŁĄD INICJALIZACJI!'}")
# ========================= KONIEC BLOKU DO WKLEJENIA =========================
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
