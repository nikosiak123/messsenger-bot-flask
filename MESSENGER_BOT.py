# -*- coding: utf-8 -*-

# verify_server.py (Wersja: Wiele Stron FB + Statystyki + Zapis do Google Calendar)
from pyairtable import Api
from flask import Flask, request, Response
import threading
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
from collections import defaultdict

# --- Konfiguracja Stron Facebook ---

app = Flask(__name__)

# --- Konfiguracja Ogólna ---
VERIFY_TOKEN = os.environ.get("FB_VERIFY_TOKEN", "KOLAGEN")
PROJECT_ID = os.environ.get("GCP_PROJECT_ID", "singular-carver-459118-g5")
LOCATION = os.environ.get("GCP_LOCATION", "us-central1")
MODEL_ID = os.environ.get("VERTEX_MODEL_ID", "gemini-2.0-flash-001") # POPRAWKA: Używam 1.5 flash, jest nowszy i lepszy
FACEBOOK_GRAPH_API_URL = "https://graph.facebook.com/v19.0/me/messages"
HISTORY_DIR = "conversation_store"

# DODANO: Brakujące stałe konfiguracyjne
MAX_HISTORY_TURNS = 15  # Liczba tur (user+model) do przechowywania w historii
MESSAGE_CHAR_LIMIT = 1900  # Maksymalna długość pojedynczej wiadomości na FB
MESSAGE_DELAY_SECONDS = 2  # Opóźnienie między kolejnymi fragmentami długiej wiadomości
ENABLE_TYPING_DELAY = True  # Czy symulować pisanie
MIN_TYPING_DELAY_SECONDS = 1.5  # Minimalny czas symulacji pisania
MAX_TYPING_DELAY_SECONDS = 5.0  # Maksymalny czas symulacji pisania
TYPING_CHARS_PER_SECOND = 25 # Szacowana prędkość pisania (znaki/sekundę)

# --- Konfiguracja Kalendarza ---
CALENDAR_SERVICE_ACCOUNT_FILE = 'KALENDARZ_KLUCZ.json'
CALENDAR_SCOPES = ['https://www.googleapis.com/auth/calendar']
CALENDAR_TIMEZONE = 'Europe/Warsaw'
APPOINTMENT_DURATION_MINUTES = 60
MIN_BOOKING_LEAD_HOURS = 2    # Nie można rezerwować terminów z mniejszym niż 2h wyprzedzeniem
MAX_SEARCH_DAYS = 30          # Sprawdzaj dostępność na 30 dni w przód
WORK_START_HOUR = 8           # Godzina rozpoczęcia pracy
WORK_END_HOUR = 22            # Godzina zakończenia pracy

# --- Konfiguracja Airtable ---
AIRTABLE_API_KEY = os.environ.get("AIRTABLE_API_KEY", "patcSdupvwJebjFDo.7e15a93930d15261989844687bcb15ac5c08c84a29920c7646760bc6f416146d")
AIRTABLE_BASE_ID = os.environ.get("AIRTABLE_BASE_ID", "appTjrMTVhYBZDPw9")
AIRTABLE_BOOKINGS_TABLE_NAME = "Rezerwacje"
AIRTABLE_STATS_TABLE_NAME = "Statystyki"
AIRTABLE_TIMEZONE = 'Europe/Warsaw' # DODANO: Strefa czasowa dla Airtable

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

# POPRAWKA: Funkcja została ulepszona, aby centralnie tworzyć wszystkie potrzebne struktury danych.
def load_and_process_config(config_file='config.json'):
    """Wczytuje, parsuje i przetwarza konfigurację z pliku JSON."""
    default_config = {
        "PAGE_CONFIG": {}, "CALENDARS": [], "ALL_SUBJECT_LINKS": {},
        "AVAILABLE_SUBJECTS": [], "SUBJECT_TO_CALENDARS": defaultdict(list),
        "ALL_CALENDAR_ID_TO_NAME": {}
    }
    try:
        with open(config_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
        
        page_config = data.get("PAGE_CONFIG", {})
        calendars = data.get("CALENDARS", [])
        
        all_subject_links = {
            page_data["subject"]: page_data["link"]
            for page_data in page_config.values() if "subject" in page_data and "link" in page_data
        }
        available_subjects = sorted(list(all_subject_links.keys()))
        
        subject_to_calendars = defaultdict(list)
        all_calendar_id_to_name = {}
        for cal_config in calendars:
            if 'subject' in cal_config and 'id' in cal_config and 'name' in cal_config:
                if cal_config['subject'] in available_subjects:
                    subject_to_calendars[cal_config['subject'].lower()].append(cal_config)
                all_calendar_id_to_name[cal_config['id']] = cal_config['name']

        logging.info(f"Pomyślnie wczytano i przetworzono konfigurację z {config_file}.")
        return {
            "PAGE_CONFIG": page_config,
            "CALENDARS": calendars,
            "ALL_SUBJECT_LINKS": all_subject_links,
            "AVAILABLE_SUBJECTS": available_subjects,
            "SUBJECT_TO_CALENDARS": subject_to_calendars,
            "ALL_CALENDAR_ID_TO_NAME": all_calendar_id_to_name # DODANO
        }
    except FileNotFoundError:
        logging.critical(f"!!! KRYTYCZNY BŁĄD: Brak pliku konfiguracyjnego '{config_file}'! Bot nie będzie działał poprawnie. !!!")
        return default_config
    except json.JSONDecodeError as e:
        logging.critical(f"!!! KRYTYCZNY BŁĄD: Błąd parsowania pliku JSON '{config_file}': {e}. Bot nie będzie działał poprawnie. !!!")
        return default_config
    except Exception as e:
        logging.critical(f"!!! KRYTYCZNY BŁĄD: Nieoczekiwany błąd podczas ładowania konfiguracji: {e} !!!", exc_info=True)
        return default_config

# POPRAWKA: Funkcja przyjmuje `subject_to_calendars` jako argument, aby uniknąć problemu z zasięgiem.
def create_google_event_from_airtable(calendar_service, airtable_record_fields, subject_to_calendars):
    """Tworzy wydarzenie w Google Calendar na podstawie danych z rekordu Airtable."""
    if not calendar_service:
        logging.error("[GCAL CREATE] Usługa kalendarza niedostępna.")
        return
    
    subject = airtable_record_fields.get('Przedmiot')
    start_time_iso = airtable_record_fields.get('Date')
    student_first_name = airtable_record_fields.get('Imię Ucznia', 'Nieznany')
    student_last_name = airtable_record_fields.get('Nazwisko Ucznia', 'Uczeń')
    parent_first_name = airtable_record_fields.get('Imię Rodzica', 'Brak')
    parent_last_name = airtable_record_fields.get('Nazwisko Rodzica', 'Brak')
    grade = airtable_record_fields.get('Klasa', 'Brak')
    level = airtable_record_fields.get('Poziom', 'Brak')

    if not subject or not start_time_iso:
        logging.error(f"[GCAL CREATE] Brak przedmiotu lub daty w danych z Airtable: {airtable_record_fields}")
        return

    # POPRAWKA: Użycie przekazanej mapy zamiast globalnej
    calendars_for_subject = subject_to_calendars.get(subject.lower())
    if not calendars_for_subject:
        logging.error(f"[GCAL CREATE] Nie znaleziono skonfigurowanego kalendarza dla przedmiotu: {subject}")
        return
    
    target_calendar_id = calendars_for_subject[0]['id']
    
    try:
        tz = pytz.timezone(CALENDAR_TIMEZONE)
        start_dt = datetime.datetime.fromisoformat(start_time_iso.replace('Z', '+00:00')).astimezone(tz)
        end_dt = start_dt + datetime.timedelta(minutes=APPOINTMENT_DURATION_MINUTES)

        summary = f"(NIEPOTWIERDZONE) {student_first_name} {student_last_name}"
        
        description = (
            f"Automatyczna rezerwacja z bota Messengera.\n\n"
            f"Przedmiot: {subject}\n"
            f"Uczeń: {student_first_name} {student_last_name}\n"
            f"Rodzic: {parent_first_name} {parent_last_name}\n"
            f"Klasa: {grade}\n"
            f"Poziom: {level}\n\n"
            f"Status: Oczekuje na ostateczne potwierdzenie."
        )
        
        event_body = {
            'summary': summary,
            'description': description,
            'start': {'dateTime': start_dt.isoformat(), 'timeZone': CALENDAR_TIMEZONE},
            'end': {'dateTime': end_dt.isoformat(), 'timeZone': CALENDAR_TIMEZONE},
        }

        created_event = calendar_service.events().insert(
            calendarId=target_calendar_id,
            body=event_body
        ).execute()
        
        logging.info(f"[GCAL CREATE] Pomyślnie utworzono wydarzenie w kalendarzu '{target_calendar_id}'. Link: {created_event.get('htmlLink')}")
        
    except Exception as e:
        logging.error(f"[GCAL CREATE] Wystąpił błąd podczas tworzenia wydarzenia w Google Calendar: {e}", exc_info=True)

def ensure_dir(directory):
    """Tworzy katalog, jeśli nie istnieje."""
    try:
        os.makedirs(directory)
        logging.info(f"Utworzono katalog: {directory}")
    except OSError as e:
        if e.errno != errno.EEXIST:
            logging.error(f"Błąd tworzenia katalogu {directory}: {e}", exc_info=True)
            raise

def get_user_profile(psid, page_access_token):
    """Pobiera podstawowe dane profilu użytkownika z Facebook Graph API."""
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
    default_context = {'type': STATE_GENERAL}

    if not os.path.exists(filepath):
        logging.info(f"[{user_psid}] Plik historii nie istnieje, zwracam stan domyślny {STATE_GENERAL}.")
        return history, default_context.copy(), True

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
                            logging.debug(f"[{user_psid}] Odczytano AKTYWNY kontekst: {context}")
                            system_context_found = True
                        elif state_type:
                            logging.warning(f"[{user_psid}] Znaleziono kontekst w pliku {filepath}, ale z nieprawidłowym typem: {msg_data}. Używam domyślnego {STATE_GENERAL}.")
                            context = default_context.copy()
                        else:
                            logging.warning(f"[{user_psid}] Znaleziono kontekst systemowy w pliku {filepath}, ale bez typu: {msg_data}. Używam domyślnego {STATE_GENERAL}.")
                            context = default_context.copy()
                        last_system_message_index = len(history_data) - 1 - i
                        break

                if not system_context_found:
                    logging.debug(f"[{user_psid}] Nie znaleziono poprawnego kontekstu systemowego na końcu pliku {filepath}. Ustawiam stan {STATE_GENERAL}.")
                    context = default_context.copy()

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
                        logging.debug(f"Ostrz. [{user_psid}]: Pominięto niepoprawną/starą wiadomość/kontekst (idx {i}) w pliku {filepath}: {msg_data}")

                logging.info(f"[{user_psid}] Wczytano historię z {filepath}: {len(history)} wiad. Stan: {context.get('type', STATE_GENERAL)}")
                return history, context, False

            else:
                logging.error(f"BŁĄD [{user_psid}]: Plik historii {filepath} nie jest listą.")
                return [], default_context.copy(), False
    except FileNotFoundError:
        logging.info(f"[{user_psid}] Plik historii {filepath} nie istnieje.")
        return [], default_context.copy(), True
    except (json.JSONDecodeError, KeyError, IndexError, TypeError) as e:
        logging.error(f"BŁĄD [{user_psid}] parsowania historii z {filepath}: {e}.")
        try:
            os.rename(filepath, f"{filepath}.error_{int(time.time())}")
            logging.warning("    Zmieniono nazwę uszkodzonego pliku historii.")
        except OSError as rename_err:
            logging.error(f"    Nie udało się zmienić nazwy: {rename_err}")
        return [], default_context.copy(), False
    except Exception as e:
        logging.error(f"BŁĄD [{user_psid}] wczytywania historii z {filepath}: {e}", exc_info=True)
        return [], default_context.copy(), False


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

        current_state_to_save = STATE_GENERAL
        if context_to_save and isinstance(context_to_save, dict):
            context_copy = context_to_save.copy()
            current_state_to_save = context_copy.get('type', STATE_GENERAL)
            context_copy['role'] = 'system'
            is_default_general = (current_state_to_save == STATE_GENERAL and
                                  len(context_copy) == 2 and
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
# POPRAWKA: Cała funkcja została zachowana, ale wywołania funkcji pomocniczych zostały zaktualizowane,
# aby przekazywać im wczytaną konfigurację.
def process_single_event(event_payload, page_id_from_entry_info):
    """
    Przetwarza pojedyncze zdarzenie 'messaging' od Facebooka.
    Ta funkcja będzie uruchamiana w osobnym wątku.
    """
    config = load_and_process_config() # Zawsze ładuj świeżą konfigurację

    # Użyj zmiennych z wczytanej konfiguracji
    PAGE_CONFIG = config['PAGE_CONFIG']
    SUBJECT_TO_CALENDARS = config['SUBJECT_TO_CALENDARS']
    AVAILABLE_SUBJECTS = config['AVAILABLE_SUBJECTS']
    ALL_CALENDAR_ID_TO_NAME = config['ALL_CALENDAR_ID_TO_NAME'] # POPRAWKA: Pobierz nową strukturę
    ALL_SUBJECT_LINKS = config['ALL_SUBJECT_LINKS'] # POPRAWKA: Pobierz nową strukturę

    try:
        logging.info(f"(Wątek) RAW EVENT PAYLOAD: {json.dumps(event_payload)}")

        actual_user_psid = None
        page_config_for_event = None
        event_sender_id = event_payload.get("sender", {}).get("id")
        event_recipient_id = event_payload.get("recipient", {}).get("id")

        if not event_sender_id or not event_recipient_id:
            logging.warning(f"(Wątek) Zdarzenie bez sender.id lub recipient.id. Event: {event_payload}")
            return

        is_echo = event_payload.get("message", {}).get("is_echo")
        if is_echo:
            echoing_page_name = PAGE_CONFIG.get(event_sender_id, {}).get('name', event_sender_id)
            logging.debug(f"    (Wątek) Pominięto echo. Strona wysyłająca: '{echoing_page_name}'.")
            return

        actual_user_psid = event_sender_id
        page_being_contacted_id = event_recipient_id
        page_config_for_event = PAGE_CONFIG.get(page_being_contacted_id)

        if not page_config_for_event:
            logging.error(f"!!! (Wątek) Otrzymano zdarzenie dla nieskonfigurowanej strony ID: {page_being_contacted_id}. Pomijam.")
            return

        current_page_token = page_config_for_event['token']
        current_subject = page_config_for_event.get('subject', "nieznany przedmiot")
        current_page_name = page_config_for_event['name']

        logging.info(f"--- (Wątek) Przetwarzanie dla Strony: '{current_page_name}' | Przedmiot: {current_subject} | User PSID: {actual_user_psid} ---")

        if not current_page_token or len(current_page_token) < 50:
            logging.error(f"!!! KRYTYCZNY BŁĄD (Wątek): Brak tokena dla strony '{current_page_name}'.")
            return

        history, context, is_new_contact = load_history(actual_user_psid)
        history_for_gemini = [h for h in history if isinstance(h, Content) and h.role in ('user', 'model')]
        current_state = context.get('type', STATE_GENERAL)

        if is_new_contact:
            logging.info(f"[{actual_user_psid}] (Wątek) Wykryto nowy kontakt.")
            log_statistic("new_contact")

        logging.info(f"    (Wątek) [{actual_user_psid}] Aktualny stan: {current_state}")

        action = None
        msg_result = None
        next_state = current_state # Domyślnie stan się nie zmienia
        context_data_to_save = context.copy()
        
        ai_response_text_raw = None
        model_resp_content = None
        user_content = None
        
        context_data_to_save.pop('return_to_state', None)
        context_data_to_save.pop('return_to_context', None)

        if context_data_to_save.get('required_subject') != current_subject or 'required_subject' not in context_data_to_save:
            if current_state == STATE_GENERAL or context_data_to_save.get('_just_reset', False):
                context_data_to_save['required_subject'] = current_subject
                logging.debug(f"    (Wątek) [{actual_user_psid}] Ustawiono 'required_subject' na domyślny: {current_subject}")
            elif 'required_subject' not in context_data_to_save or not context_data_to_save.get('required_subject'):
                 context_data_to_save['required_subject'] = current_subject
                 logging.warning(f"    (Wątek) [{actual_user_psid}] 'required_subject' był pusty. Ustawiono na: {current_subject}.")

        trigger_gathering_ai_immediately = False
        slot_verification_failed = False
        is_temporary_general_state = 'return_to_state' in context

        if message_data := event_payload.get("message"):
            user_input_text = message_data.get("text", "").strip()
            if user_input_text:
                user_content = Content(role="user", parts=[Part.from_text(user_input_text)])
                logging.info(f"    (Wątek) [{actual_user_psid}] Odebrano wiadomość (stan={current_state}): '{user_input_text[:100]}...'")
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
                logging.info(f"      (Wątek) [{actual_user_psid}] Odebrano pustą wiadomość.")
                return
        elif postback := event_payload.get("postback"):
            payload = postback.get("payload")
            title = postback.get("title", "")
            user_input_text = f"Kliknięto: '{title}' (Payload: {payload})"
            user_content = Content(role="user", parts=[Part.from_text(user_input_text)])
            logging.info(f"    (Wątek) [{actual_user_psid}] Odebrano postback: Payload='{payload}' (stan={current_state})")
            if payload == "CANCEL_SCHEDULING":
                msg_result = "Proces umawiania został anulowany."
                action = 'send_info'
                next_state = STATE_GENERAL
                context_data_to_save = {'type': STATE_GENERAL, 'required_subject': current_subject, '_just_reset': True}
            elif current_state == STATE_SCHEDULING_ACTIVE: action = 'handle_scheduling'
            elif current_state == STATE_GATHERING_INFO: action = 'handle_gathering'
            else: action = 'handle_general'
        elif event_payload.get("read") or event_payload.get("delivery"):
            logging.debug(f"    (Wątek) Potwierdzenie odczytu/dostarczenia dla {actual_user_psid}.")
            return
        else:
            logging.warning(f"    (Wątek) Nieobsługiwany typ zdarzenia dla PSID {actual_user_psid}: {json.dumps(event_payload)}")
            return

        if not action and not msg_result:
            logging.debug(f"    (Wątek) [{actual_user_psid}] Brak akcji. Kończenie.")
            return

        loop_guard = 0
        max_loops = 3
        while (action or msg_result) and loop_guard < max_loops:
            loop_guard += 1
            effective_subject_for_action = context_data_to_save.get('required_subject', current_subject)
            logging.debug(f"  >> (Wątek) [{actual_user_psid}] Pętla {loop_guard}/{max_loops} | Akcja: {action} | Stan: {current_state} -> {next_state} | Przedmiot: {effective_subject_for_action}")
            current_action_in_loop = action
            action = None

            if current_action_in_loop == 'handle_general':
                is_initial_general_entry = (current_state != STATE_GENERAL) or (not history_for_gemini and not user_content) or (context_data_to_save.get('_just_reset', False))
                context_data_to_save.pop('_just_reset', None)
                user_message_text_for_ai = user_content.parts[0].text if user_content and user_content.parts else None

                if is_initial_general_entry and not user_message_text_for_ai:
                    other_subjects_links_parts = []
                    for subj_name, subj_link in ALL_SUBJECT_LINKS.items():
                        if current_subject and subj_name.lower() != current_subject.lower():
                            other_subjects_links_parts.append(f"- {subj_name}: {subj_link}")

                    links_text_for_user = "\n\nUdzielamy również korepetycji z:\n" + "\n".join(other_subjects_links_parts) if other_subjects_links_parts else ""
                    display_subject = current_subject if current_subject else "korepetycji"
                    msg_result = f"Dzień dobry! Dziękujemy za kontakt w sprawie korepetycji z przedmiotu **{display_subject}**. W czym mogę pomóc? Jeśli chcą Państwo umówić termin, proszę dać znać." + links_text_for_user
                    model_resp_content = Content(role="model", parts=[Part.from_text(msg_result)])
                    next_state = STATE_GENERAL
                    context_data_to_save.update({'type': STATE_GENERAL, 'required_subject': current_subject})

                elif user_message_text_for_ai:
                    was_temporary = 'return_to_state' in context
                    # POPRAWKA: Przekazanie PAGE_CONFIG do funkcji AI
                    ai_response_text_raw = get_gemini_general_response(
                        actual_user_psid,
                        user_message_text_for_ai,
                        history_for_gemini,
                        is_temporary_general_state,
                        current_page_token,
                        current_subject_for_context=current_subject,
                        page_config=PAGE_CONFIG # POPRAWKA
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
                            student_data_match = re.search(r"\[DANE_UCZNIA_OGOLNE:\s*KlasaInfo:\s*(.*?),?\s*Poziom:\s*(.*?)\]", ai_response_text_raw, re.IGNORECASE | re.DOTALL)
                            grade_info_from_ai, level_info_from_ai = "", ""
                            if student_data_match:
                                grade_info_from_ai = student_data_match.group(1).strip()
                                level_info_from_ai = student_data_match.group(2).strip()
                                logging.info(f"    (Wątek) [{actual_user_psid}] AI (General) zidentyfikowało: Klasa='{grade_info_from_ai}', Poziom='{level_info_from_ai}'")
                                ai_response_text_raw = re.sub(r"\[DANE_UCZNIA_OGOLNE:.*?\]", "", ai_response_text_raw).strip()

                            msg_result = ai_response_text_raw.split(INTENT_SCHEDULE_MARKER, 1)[0].strip()
                            confirmed_subject_by_ai = effective_subject_for_action

                            if confirmed_subject_by_ai and confirmed_subject_by_ai in AVAILABLE_SUBJECTS:
                                next_state = STATE_SCHEDULING_ACTIVE
                                context_data_to_save = {
                                    'type': STATE_SCHEDULING_ACTIVE, 'required_subject': confirmed_subject_by_ai,
                                    'known_grade': grade_info_from_ai, 'known_level': level_info_from_ai
                                }
                                action = 'handle_scheduling'
                                current_state = next_state
                            else:
                                msg_result = (msg_result + "\n\n" if msg_result else "") + f"Dostępne przedmioty: {', '.join(AVAILABLE_SUBJECTS)}. Proszę sprecyzować."
                                model_resp_content = Content(role="model", parts=[Part.from_text(msg_result)])
                                next_state = STATE_GENERAL
                                context_data_to_save.update({'type': STATE_GENERAL, 'required_subject': current_subject})
                        else:
                            msg_result = ai_response_text_raw
                            next_state = STATE_GENERAL
                            context_data_to_save.update({'type': STATE_GENERAL, 'required_subject': effective_subject_for_action})
                            if was_temporary:
                                context_data_to_save.update({'return_to_state': context.get('return_to_state'), 'return_to_context': context.get('return_to_context', {})})
                    else:
                        msg_result = "Przepraszam, mam chwilowy problem. Spróbuj ponownie za chwilę."
                        model_resp_content = Content(role="model", parts=[Part.from_text(msg_result)])
                        next_state = STATE_GENERAL
                        context_data_to_save = {'type': STATE_GENERAL, 'required_subject': current_subject, '_just_reset': True}

            elif current_action_in_loop == 'handle_scheduling':
                if not effective_subject_for_action or effective_subject_for_action not in AVAILABLE_SUBJECTS:
                    msg_result = f"Przepraszam, wystąpił błąd. Nie wiem, dla jakiego przedmiotu ('{effective_subject_for_action}') umówić termin. Proszę zacząć od nowa."
                    next_state = STATE_GENERAL
                    context_data_to_save = {'type': STATE_GENERAL, 'required_subject': current_subject, '_just_reset': True}
                else:
                    subject_calendars_config = SUBJECT_TO_CALENDARS.get(effective_subject_for_action.lower(), [])
                    if not subject_calendars_config:
                        msg_result = f"Przepraszam, brak skonfigurowanych kalendarzy dla '{effective_subject_for_action}'."
                        next_state = STATE_GENERAL
                        context_data_to_save = {'type': STATE_GENERAL, 'required_subject': current_subject, '_just_reset': True}
                    else:
                        try:
                            tz_cal = _get_calendar_timezone()
                            now_cal_tz = datetime.datetime.now(tz_cal)
                            search_start_dt = now_cal_tz
                            search_end_dt = tz_cal.localize(datetime.datetime.combine((now_cal_tz + datetime.timedelta(days=MAX_SEARCH_DAYS)).date(), datetime.time(WORK_END_HOUR, 0)))
                            _simulate_typing(actual_user_psid, MAX_TYPING_DELAY_SECONDS * 0.7, current_page_token)
                            
                            # POPRAWKA: Przekazanie `all_calendar_id_to_name`
                            free_ranges = get_free_time_ranges(subject_calendars_config, search_start_dt, search_end_dt, ALL_CALENDAR_ID_TO_NAME)
                            
                            user_msg_for_ai = user_content.parts[0].text if user_content and user_content.parts else None
                            if slot_verification_failed:
                                user_msg_for_ai = (user_msg_for_ai or "") + "\n[Informacja systemowa: Poprzedni termin był niedostępny. Zaproponuj inny.]"
                                slot_verification_failed = False
                            
                            ai_response_text_raw = get_gemini_scheduling_response(
                                actual_user_psid, history_for_gemini, user_msg_for_ai,
                                free_ranges, effective_subject_for_action, current_page_token
                            )
                            if ai_response_text_raw:
                                model_resp_content = Content(role="model", parts=[Part.from_text(ai_response_text_raw)])
                                if ai_response_text_raw.strip() == SWITCH_TO_GENERAL:
                                    context_data_to_save.update({'return_to_state': STATE_SCHEDULING_ACTIVE, 'return_to_context': {'type': STATE_SCHEDULING_ACTIVE, 'required_subject': effective_subject_for_action}, 'type': STATE_GENERAL})
                                    next_state = STATE_GENERAL; action = 'handle_general'; current_state = next_state; msg_result = None
                                else:
                                    iso_match = re.search(rf"{re.escape(SLOT_ISO_MARKER_PREFIX)}(.*?){re.escape(SLOT_ISO_MARKER_SUFFIX)}", ai_response_text_raw)
                                    if iso_match:
                                        extracted_iso = iso_match.group(1).strip()
                                        text_for_user = re.sub(r'\s+', ' ', re.sub(rf"{re.escape(SLOT_ISO_MARKER_PREFIX)}.*?{re.escape(SLOT_ISO_MARKER_SUFFIX)}", "", ai_response_text_raw).strip()).strip()
                                        try:
                                            proposed_start_dt = datetime.datetime.fromisoformat(extracted_iso).astimezone(tz_cal)
                                            proposed_slot_formatted = format_slot_for_user(proposed_start_dt)
                                            _simulate_typing(actual_user_psid, MIN_TYPING_DELAY_SECONDS, current_page_token)
                                            
                                            chosen_calendar_id, chosen_calendar_name = None, None
                                            for cal_conf_iter in subject_calendars_config:
                                                # POPRAWKA: Przekazanie `all_calendar_id_to_name`
                                                if is_slot_actually_free(proposed_start_dt, cal_conf_iter['id'], ALL_CALENDAR_ID_TO_NAME):
                                                    chosen_calendar_id = cal_conf_iter['id']
                                                    chosen_calendar_name = cal_conf_iter['name']
                                                    logging.info(f"    (Wątek) [{actual_user_psid}] Slot {proposed_slot_formatted} jest WOLNY w '{chosen_calendar_name}'.")
                                                    break

                                            if chosen_calendar_id and chosen_calendar_name:
                                                write_ok, record_id_or_error_msg = create_airtable_record_phase1(actual_user_psid, proposed_start_dt, chosen_calendar_name, effective_subject_for_action)
                                                if write_ok:
                                                    record_id = record_id_or_error_msg
                                                    user_profile_fb = get_user_profile(actual_user_psid, current_page_token)
                                                    parent_fn, parent_ln = (user_profile_fb.get('first_name', ''), user_profile_fb.get('last_name', '')) if user_profile_fb else ('', '')
                                                    msg_result = text_for_user or f"Świetnie, proponowany termin na {effective_subject_for_action} to {proposed_slot_formatted}."
                                                    model_confirmation_content_for_history = Content(role="model", parts=[Part.from_text(msg_result)])
                                                    next_state = STATE_GATHERING_INFO
                                                    context_data_to_save = {
                                                        'type': STATE_GATHERING_INFO, 'proposed_slot_iso': proposed_start_dt.isoformat(),
                                                        'proposed_slot_formatted': proposed_slot_formatted, 'chosen_calendar_id': chosen_calendar_id,
                                                        'chosen_calendar_name': chosen_calendar_name, 'required_subject': effective_subject_for_action,
                                                        'known_parent_first_name': parent_fn, 'known_parent_last_name': parent_ln,
                                                        'known_student_first_name': '', 'known_student_last_name': '',
                                                        'known_grade': context.get('known_grade', ''), 'known_level': context.get('known_level', ''), # Przenieś dane z poprzedniego stanu
                                                        'airtable_record_id': record_id, 'last_model_message_before_gathering': model_confirmation_content_for_history
                                                    }
                                                    action = 'handle_gathering'; trigger_gathering_ai_immediately = True; current_state = next_state
                                                else:
                                                    msg_result = f"Przepraszam, wystąpił błąd rezerwacji ({record_id_or_error_msg}). Proszę wybrać termin ponownie."
                                                    next_state = STATE_SCHEDULING_ACTIVE; slot_verification_failed = True
                                            else:
                                                msg_result = (text_for_user + "\n" if text_for_user else "") + f"Niestety, termin {proposed_slot_formatted} został właśnie zajęty. Wybierzmy inny."
                                                next_state = STATE_SCHEDULING_ACTIVE; slot_verification_failed = True
                                        except ValueError as ve:
                                            logging.error(f"(Wątek) [{actual_user_psid}] Błąd parsowania daty ISO '{extracted_iso}': {ve}")
                                            msg_result = "Wystąpił błąd formatu terminu. Spróbujmy wybrać ponownie."
                                            next_state = STATE_SCHEDULING_ACTIVE
                                        except Exception as verif_err:
                                            logging.error(f"(Wątek) [{actual_user_psid}] Krytyczny błąd weryfikacji slotu: {verif_err}", exc_info=True)
                                            msg_result = "Wewnętrzny błąd systemu. Spróbuj ponownie później."
                                            next_state = STATE_GENERAL; context_data_to_save = {'type': STATE_GENERAL, 'required_subject': current_subject, '_just_reset': True}
                                    else:
                                        msg_result = ai_response_text_raw
                                        next_state = STATE_SCHEDULING_ACTIVE
                            else:
                                msg_result = ai_response_text_raw or f"Problem z systemem planowania dla {effective_subject_for_action}."
                                if "Brak dostępnych terminów" in msg_result or (not free_ranges and not ai_response_text_raw):
                                    msg_result = f"Przepraszam, brak wolnych terminów dla {effective_subject_for_action} w najbliższym czasie."
                                next_state = STATE_GENERAL; context_data_to_save = {'type': STATE_GENERAL, 'required_subject': current_subject, '_just_reset': True}
                        except Exception as schedule_err_final:
                            logging.error(f"(Wątek) [{actual_user_psid}] Krytyczny błąd w 'handle_scheduling': {schedule_err_final}", exc_info=True)
                            msg_result = "Poważny błąd systemu planowania. Spróbuj ponownie później."
                            next_state = STATE_GENERAL; context_data_to_save = {'type': STATE_GENERAL, 'required_subject': current_subject, '_just_reset': True}
            
            elif current_action_in_loop == 'handle_gathering':
                try:
                    user_msg_for_ai = user_content.parts[0].text if user_content and user_content.parts else None
                    current_history_for_gathering_ai = list(history_for_gemini)
                    if trigger_gathering_ai_immediately:
                        logging.info(f"    (Wątek) [{actual_user_psid}] Inicjuję AI zbierające dane.")
                        last_model_msg_content = context_data_to_save.pop('last_model_message_before_gathering', None)
                        if last_model_msg_content and isinstance(last_model_msg_content, Content):
                            current_history_for_gathering_ai.append(last_model_msg_content)
                        user_msg_for_ai = "Jestem gotów podać dane ucznia."
                        trigger_gathering_ai_immediately = False
                    
                    context_for_gathering_ai = context_data_to_save.copy()
                    ai_response_text_raw = get_gemini_gathering_response(
                        actual_user_psid, current_history_for_gathering_ai, user_msg_for_ai,
                        context_for_gathering_ai, current_page_token
                    )
                    if ai_response_text_raw:
                        model_resp_content = Content(role="model", parts=[Part.from_text(ai_response_text_raw)])
                        if ai_response_text_raw.strip() == SWITCH_TO_GENERAL:
                            context_data_to_save.update({'return_to_state': STATE_GATHERING_INFO, 'return_to_context': context_for_gathering_ai, 'type': STATE_GENERAL})
                            next_state = STATE_GENERAL; action = 'handle_general'; current_state = next_state; msg_result = None
                        elif INFO_GATHERED_MARKER in ai_response_text_raw:
                            response_parts = ai_response_text_raw.split(INFO_GATHERED_MARKER, 1)
                            ai_response_before_marker = response_parts[0].strip()
                            data_match = re.search(r"ZEBRANE_DANE_UCZNIA:\s*\[Imię:\s*(.*?),?\s*Nazwisko:\s*(.*?),?\s*KlasaInfo:\s*(.*?),?\s*Poziom:\s*(.*?)\]", ai_response_before_marker, re.IGNORECASE | re.DOTALL)
                            parsed_student_data = {}
                            if data_match:
                                parsed_student_data['student_first_name'] = data_match.group(1).strip()
                                parsed_student_data['student_last_name'] = data_match.group(2).strip()
                                parsed_student_data['grade_info'] = data_match.group(3).strip()
                                parsed_student_data['level_info'] = data_match.group(4).strip()
                                msg_result = ai_response_before_marker[data_match.end():].strip()
                                logging.info(f"    (Wątek) [{actual_user_psid}] Parsowane dane ucznia z AI: {parsed_student_data}")
                            else:
                                logging.warning(f"    (Wątek) [{actual_user_psid}] Nie znaleziono ZEBRANE_DANE_UCZNIA. Używam kontekstu.")
                                msg_result = ai_response_before_marker

                            if not msg_result:
                                msg_result = "Dziękujemy za podanie informacji. Rezerwacja została wstępnie przyjęta. Prosimy o ostateczne potwierdzenie zajęć, wysyłając wiadomość \"POTWIERDZAM\" na profilu Facebook: https://www.facebook.com/profile.php?id=61576135251276. Ten profil służy również do dalszego kontaktu."
                            
                            parsed_student_data['parent_first_name'] = context_data_to_save.get('known_parent_first_name', '')
                            parsed_student_data['parent_last_name'] = context_data_to_save.get('known_parent_last_name', '')
                            record_id_for_update = context_data_to_save.get('airtable_record_id')
                            
                            # POPRAWKA: Przekazanie `subject_to_calendars`
                            update_ok, update_message = update_airtable_record_phase2(record_id_for_update, parsed_student_data, SUBJECT_TO_CALENDARS)
                            
                            if not update_ok:
                                logging.error(f"    (Wątek) [{actual_user_psid}] Błąd Fazy 2 w arkuszu: {update_message}")
                            
                            next_state = STATE_GENERAL
                            context_data_to_save = {'type': STATE_GENERAL, 'required_subject': current_subject, '_just_reset': True}
                        else:
                            msg_result = ai_response_text_raw
                            next_state = STATE_GATHERING_INFO
                    else:
                        msg_result = "Chwilowy problem z systemem zbierania informacji. Spróbujmy jeszcze raz."
                        next_state = STATE_GATHERING_INFO
                except Exception as gather_err:
                    logging.error(f"(Wątek) [{actual_user_psid}] Krytyczny błąd w 'handle_gathering': {gather_err}", exc_info=True)
                    msg_result = "Poważny błąd systemu zbierania danych. Spróbuj ponownie później."
                    next_state = STATE_GENERAL
                    context_data_to_save = {'type': STATE_GENERAL, 'required_subject': current_subject, '_just_reset': True}

            elif current_action_in_loop == 'send_info':
                pass # Wiadomość zostanie wysłana na końcu
            else:
                if current_action_in_loop:
                    logging.error(f"    (Wątek) [{actual_user_psid}] Nieznana akcja '{current_action_in_loop}'.")

        final_context_to_save_dict = context_data_to_save.copy()
        final_context_to_save_dict['type'] = next_state
        if 'required_subject' not in final_context_to_save_dict:
            final_context_to_save_dict['required_subject'] = current_subject
        
        if next_state != STATE_GENERAL or 'return_to_state' not in final_context_to_save_dict:
            final_context_to_save_dict.pop('return_to_state', None)
            final_context_to_save_dict.pop('return_to_context', None)

        if msg_result:
            if not model_resp_content and not (current_action_in_loop == 'handle_gathering' and INFO_GATHERED_MARKER in (ai_response_text_raw or "")):
                 model_resp_content = Content(role="model", parts=[Part.from_text(msg_result)])
            send_message(actual_user_psid, msg_result, current_page_token)

        original_context_no_return = context.copy()
        original_context_no_return.pop('return_to_state', None)
        original_context_no_return.pop('return_to_context', None)
        should_save = bool(user_content) or bool(model_resp_content) or (original_context_no_return != final_context_to_save_dict)

        if should_save:
            history_to_save_final = [h for h in history_for_gemini if isinstance(h, Content)]
            if user_content: history_to_save_final.append(user_content)
            if model_resp_content: history_to_save_final.append(model_resp_content)
            history_to_save_final = history_to_save_final[-(MAX_HISTORY_TURNS * 2):]
            logging.info(f"    (Wątek) [{actual_user_psid}] Zapisywanie historii. Stan: {final_context_to_save_dict.get('type')}")
            save_history(actual_user_psid, history_to_save_final, context_to_save=final_context_to_save_dict)
        else:
            logging.debug(f"    (Wątek) [{actual_user_psid}] Brak zmian w historii/kontekście - pomijanie zapisu.")

        logging.info(f"--- (Wątek) Zakończono przetwarzanie dla Strony: '{current_page_name}', User PSID: {actual_user_psid} ---")

    except Exception as e_thread:
        event_mid = event_payload.get('message', {}).get('mid', 'N/A')
        logging.critical(f"KRYTYCZNY BŁĄD W WĄTKU (event MID: {event_mid}): {e_thread}", exc_info=True)


def _get_calendar_timezone():
    """Pobiera (i cachuje) obiekt strefy czasowej dla Kalendarza."""
    global _cal_tz
    if _cal_tz is None:
        try:
            _cal_tz = pytz.timezone(CALENDAR_TIMEZONE)
        except pytz.exceptions.UnknownTimeZoneError:
            logging.error(f"BŁĄD: Strefa '{CALENDAR_TIMEZONE}' nieznana. Używam UTC.")
            _cal_tz = pytz.utc
    return _cal_tz


def format_slot_for_user(slot_start):
    """Formatuje pojedynczy slot (datetime) na czytelny tekst dla użytkownika."""
    if not isinstance(slot_start, datetime.datetime):
        return "[Błąd daty]"
    try:
        tz = _get_calendar_timezone()
        slot_start = slot_start.astimezone(tz)
        day_name = POLISH_WEEKDAYS[slot_start.weekday()]
        return f"{day_name}, {slot_start.strftime('%d.%m.%Y o %H:%M')}"
    except Exception as e:
        logging.error(f"Błąd formatowania slotu {slot_start}: {e}", exc_info=True)
        return slot_start.isoformat()

def extract_school_type(grade_string):
    """Próbuje wyodrębnić numer klasy, opis klasy i typ szkoły z ciągu."""
    # Funkcja bez zmian, jest OK.
    if not grade_string or not isinstance(grade_string, str):
        return "", "Nieokreślona", "Nieokreślona"
    grade_lower = grade_string.lower().strip()
    class_desc = grade_string.strip()
    school_type = "Nieokreślona"
    numerical_grade = ""
    type_mapping = {"Liceum": [r'liceum', r'\blo\b'], "Technikum": [r'technikum', r'\btech\b'], "Szkoła Podstawowa": [r'podstaw', r'\bsp\b'], "Szkoła Branżowa/Zawodowa": [r'zawodowa', r'branżowa', r'zasadnicza']}
    for type_name, patterns in type_mapping.items():
        if any(re.search(p, grade_lower) for p in patterns):
            school_type = type_name
            break
    num_match = re.search(r'\b(\d+)\b', grade_string)
    if num_match: numerical_grade = num_match.group(1)
    class_desc = re.sub(r'(?i)\bklas[ay]?\b', '', class_desc).strip()
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
        creds = service_account.Credentials.from_service_account_file(CALENDAR_SERVICE_ACCOUNT_FILE, scopes=CALENDAR_SCOPES)
        _calendar_service = build('calendar', 'v3', credentials=creds, cache_discovery=False)
        logging.info("Utworzono połączenie z Google Calendar API.")
        return _calendar_service
    except Exception as e:
        logging.error(f"Błąd tworzenia usługi Google Calendar: {e}", exc_info=True)
        return None

def parse_event_time(event_time_data, default_tz):
    """Parsuje dane czasu wydarzenia z API Kalendarza."""
    dt_str = event_time_data.get('dateTime') or event_time_data.get('date')
    if not dt_str: return None
    is_date_only = 'date' in event_time_data and 'T' not in dt_str
    if is_date_only: return None # Ignoruj wydarzenia całodniowe
    try:
        dt = datetime.datetime.fromisoformat(dt_str.replace('Z', '+00:00'))
        return dt.astimezone(default_tz) if dt.tzinfo else default_tz.localize(dt)
    except (ValueError, TypeError) as e:
        logging.warning(f"Ostrz.: Nie sparsowano czasu '{dt_str}': {e}")
        return None

# POPRAWKA: Funkcja przyjmuje `all_calendar_id_to_name`, aby uniknąć problemu z zasięgiem.
def get_calendar_busy_slots(calendar_ids_to_check, start_datetime, end_datetime, all_calendar_id_to_name):
    """Pobiera zajęte sloty z podanych kalendarzy Google."""
    service_cal = get_calendar_service()
    tz = _get_calendar_timezone()
    busy_times_calendar = []
    if not service_cal or not calendar_ids_to_check:
        return busy_times_calendar

    start_datetime = start_datetime.astimezone(tz)
    end_datetime = end_datetime.astimezone(tz)

    body = {"timeMin": start_datetime.isoformat(), "timeMax": end_datetime.isoformat(), "timeZone": CALENDAR_TIMEZONE, "items": [{"id": cal_id} for cal_id in calendar_ids_to_check]}
    try:
        logging.debug(f"Wykonywanie zapytania freeBusy dla kalendarzy: {calendar_ids_to_check}")
        freebusy_result = service_cal.freebusy().query(body=body).execute()
        calendars_data = freebusy_result.get('calendars', {})

        for cal_id in calendar_ids_to_check:
            # POPRAWKA: Użycie przekazanej mapy
            cal_name = all_calendar_id_to_name.get(cal_id, cal_id)
            calendar_data = calendars_data.get(cal_id, {})
            if 'errors' in calendar_data:
                logging.error(f"Błąd API Freebusy dla '{cal_name}': {calendar_data['errors']}")
                continue

            for busy_slot in calendar_data.get('busy', []):
                busy_start = parse_event_time({'dateTime': busy_slot.get('start')}, tz)
                busy_end = parse_event_time({'dateTime': busy_slot.get('end')}, tz)
                if busy_start and busy_end and busy_start < busy_end:
                    busy_times_calendar.append({'start': max(busy_start, start_datetime), 'end': min(busy_end, end_datetime)})

    except HttpError as error:
        logging.error(f'Błąd HTTP API Freebusy: {error}', exc_info=True)
    except Exception as e:
        logging.error(f"Nieoczekiwany błąd podczas freeBusy: {e}", exc_info=True)

    logging.info(f"Pobrano {len(busy_times_calendar)} zajętych slotów z Google.")
    return busy_times_calendar

def get_airtable_booked_slots(start_datetime, end_datetime):
    """Pobiera zajęte sloty z tabeli Rezerwacje w Airtable."""
    if not airtable_api:
        return []
    try:
        table = airtable_api.table(AIRTABLE_BASE_ID, AIRTABLE_BOOKINGS_TABLE_NAME)
        tz_cal = _get_calendar_timezone()
        tz_airtable = pytz.timezone(AIRTABLE_TIMEZONE)

        start_dt_airtable_tz = start_datetime.astimezone(tz_airtable)
        end_dt_airtable_tz = end_datetime.astimezone(tz_airtable)
        
        formula = f"AND(IS_AFTER({{Date}}, DATETIME_PARSE('{start_dt_airtable_tz.isoformat()}')), IS_BEFORE({{Date}}, DATETIME_PARSE('{end_dt_airtable_tz.isoformat()}')))"
        
        records = table.all(formula=formula, fields=['Date', 'Nazwa Kalendarza'])
        
        airtable_busy_slots = []
        duration_delta = datetime.timedelta(minutes=APPOINTMENT_DURATION_MINUTES)
        for record in records:
            fields = record.get('fields', {})
            datetime_str = fields.get('Date')
            if not datetime_str: continue

            try:
                slot_start_utc = datetime.datetime.fromisoformat(datetime_str.replace('Z', '+00:00'))
                slot_start_cal_tz = slot_start_utc.astimezone(tz_cal)
                slot_end_cal_tz = slot_start_cal_tz + duration_delta
                airtable_busy_slots.append({'start': slot_start_cal_tz, 'end': slot_end_cal_tz})
            except (ValueError, TypeError) as e:
                logging.warning(f"Airtable: Błąd parsowania daty z rekordu {record['id']}: {e}")

        logging.info(f"Airtable: Znaleziono {len(airtable_busy_slots)} zajętych slotów.")
        return airtable_busy_slots

    except Exception as e:
        logging.error(f"Błąd podczas pobierania danych z Airtable: {e}", exc_info=True)
        return []

# POPRAWKA: Funkcja przyjmuje `all_calendar_id_to_name`, aby uniknąć problemu z zasięgiem.
def get_free_time_ranges(calendar_config_list, start_datetime, end_datetime, all_calendar_id_to_name):
    """Łączy zajęte sloty z GCal i Airtable, aby znaleźć wolne zakresy."""
    tz = _get_calendar_timezone()
    now = datetime.datetime.now(tz)
    search_start = max(start_datetime.astimezone(tz), now)
    end_datetime = end_datetime.astimezone(tz)

    if search_start >= end_datetime:
        return []

    calendar_ids_to_check = [c['id'] for c in calendar_config_list if 'id' in c]
    logging.info(f"Szukanie wolnych zakresów w: {[c['name'] for c in calendar_config_list]}")

    # POPRAWKA: Przekazanie `all_calendar_id_to_name` do funkcji pomocniczej
    busy_from_gcal = get_calendar_busy_slots(calendar_ids_to_check, search_start, end_datetime, all_calendar_id_to_name)
    busy_from_airtable = get_airtable_booked_slots(search_start, end_datetime)

    all_busy_slots = sorted(busy_from_gcal + busy_from_airtable, key=lambda x: x['start'])

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

    free_ranges = []
    current_time = search_start
    for busy in merged_busy:
        if current_time < busy['start']:
            free_ranges.append({'start': current_time, 'end': busy['start']})
        current_time = max(current_time, busy['end'])
    
    if current_time < end_datetime:
        free_ranges.append({'start': current_time, 'end': end_datetime})

    final_free_slots = []
    min_duration = datetime.timedelta(minutes=APPOINTMENT_DURATION_MINUTES)
    min_booking_time = now + datetime.timedelta(hours=MIN_BOOKING_LEAD_HOURS)
    work_start_time = datetime.time(WORK_START_HOUR, 0)
    work_end_time = datetime.time(WORK_END_HOUR, 0)

    for free_range in free_ranges:
        day_iterator = free_range['start'].date()
        while tz.localize(datetime.datetime.combine(day_iterator, datetime.time.min)) < free_range['end']:
            day_work_start = tz.localize(datetime.datetime.combine(day_iterator, work_start_time))
            day_work_end = tz.localize(datetime.datetime.combine(day_iterator, work_end_time))
            
            slot_start = max(free_range['start'], day_work_start, min_booking_time)
            slot_end = min(free_range['end'], day_work_end)

            if slot_start < slot_end and (slot_end - slot_start) >= min_duration:
                final_free_slots.append({'start': slot_start, 'end': slot_end})
            
            day_iterator += datetime.timedelta(days=1)

    logging.info(f"Znaleziono {len(final_free_slots)} ostatecznych wolnych zakresów.")
    return final_free_slots

# POPRAWKA: Funkcja przyjmuje `all_calendar_id_to_name`, aby uniknąć problemu z zasięgiem.
def is_slot_actually_free(start_time, calendar_id, all_calendar_id_to_name):
    """Weryfikuje w czasie rzeczywistym, czy DOKŁADNY slot jest wolny."""
    service = get_calendar_service()
    tz = _get_calendar_timezone()
    if not service: return False

    start_time = start_time.astimezone(tz)
    end_time = start_time + datetime.timedelta(minutes=APPOINTMENT_DURATION_MINUTES)
    query_start_time = start_time + datetime.timedelta(seconds=1)
    query_end_time = end_time - datetime.timedelta(seconds=1)

    if query_start_time >= query_end_time: return True

    body = {"timeMin": query_start_time.isoformat(), "timeMax": query_end_time.isoformat(), "timeZone": CALENDAR_TIMEZONE, "items": [{"id": calendar_id}]}
    try:
        # POPRAWKA: Użycie przekazanej mapy
        cal_name = all_calendar_id_to_name.get(calendar_id, calendar_id)
        logging.debug(f"Weryfikacja free/busy dla '{cal_name}': {start_time:%Y-%m-%d %H:%M}")
        freebusy_result = service.freebusy().query(body=body).execute()
        calendar_data = freebusy_result.get('calendars', {}).get(calendar_id, {})

        if 'errors' in calendar_data:
            logging.error(f"Błąd API Freebusy (weryfikacja) dla '{cal_name}': {calendar_data['errors']}")
            return False
        
        busy_times = calendar_data.get('busy', [])
        if not busy_times:
            logging.info(f"Weryfikacja '{cal_name}': Slot {start_time:%H:%M} POTWIERDZONY jako wolny.")
            return True
        else:
            logging.warning(f"Weryfikacja '{cal_name}': Slot {start_time:%H:%M} jest ZAJĘTY.")
            return False

    except HttpError as error:
        logging.error(f"Błąd HTTP API Freebusy (weryfikacja) dla '{calendar_id}': {error}", exc_info=True)
        return False
    except Exception as e:
        logging.error(f"Nieoczekiwany błąd weryfikacji Freebusy dla '{calendar_id}': {e}", exc_info=True)
        return False

def format_ranges_for_ai(ranges, subject=None):
    """Formatuje listę zakresów czasowych dla AI."""
    if not ranges:
        subject_info = f" dla {subject}" if subject else ""
        return f"Brak dostępnych terminów{subject_info}."

    tz = _get_calendar_timezone()
    formatted_lines = [f"Dostępne ZAKRESY dla **{subject}** (wizyta trwa {APPOINTMENT_DURATION_MINUTES} min):"] if subject else []
    
    slots_added = 0
    max_slots_to_show = 15
    sorted_ranges = sorted(ranges, key=lambda r: r['start'])
    min_duration = datetime.timedelta(minutes=APPOINTMENT_DURATION_MINUTES)

    for r in sorted_ranges:
        if (r['end'] - r['start']) >= min_duration:
            start_dt, end_dt = r['start'].astimezone(tz), r['end'].astimezone(tz)
            day_name = POLISH_WEEKDAYS[start_dt.weekday()]
            formatted_lines.append(f"- {start_dt.strftime('%Y-%m-%d')}, {day_name}, od {start_dt.strftime('%H:%M')}, do {end_dt.strftime('%H:%M')}")
            slots_added += 1
            if slots_added >= max_slots_to_show:
                formatted_lines.append("- ... (i więcej)")
                break

    if slots_added == 0:
        return f"Brak dostępnych terminów{f' dla {subject}' if subject else ''} (mieszczących wizytę {APPOINTMENT_DURATION_MINUTES} min)."
    
    return "\n".join(formatted_lines)


# =====================================================================
# === FUNKCJE AIRTABLE (ZAPIS + ODCZYT) ===============================
# =====================================================================

def create_airtable_record_phase1(psid, start_time, calendar_name, subject):
    """Zapisuje dane Fazy 1 do tabeli 'Rezerwacje' w Airtable."""
    if not airtable_api:
        return False, "Błąd połączenia z Airtable."
    try:
        table = airtable_api.table(AIRTABLE_BASE_ID, AIRTABLE_BOOKINGS_TABLE_NAME)
        start_time_utc = start_time.astimezone(pytz.utc)
        record_data = {
            'PSID': psid,
            'Date': start_time_utc.isoformat(),
            'Nazwa Kalendarza': calendar_name,
            'Przedmiot': subject,
            'Status': 'Oczekiwanie na dane ucznia'
        }
        created_record = table.create(record_data)
        record_id = created_record['id']
        logging.info(f"Zapisano Faza 1 (Airtable). ID: {record_id}")
        log_statistic("booking")
        return True, record_id
    except Exception as e:
        logging.error(f"Błąd zapisu Fazy 1 do Airtable: {e}", exc_info=True)
        return False, "Błąd systemu bazy danych."

# POPRAWKA: Funkcja przyjmuje `subject_to_calendars` jako argument, aby przekazać go dalej.
def update_airtable_record_phase2(record_id, student_data, subject_to_calendars):
    """Aktualizuje rekord w Airtable i jeśli się uda, tworzy wydarzenie w GCal."""
    if not airtable_api:
        return False, "Błąd połączenia z Airtable."
    if not record_id:
        return False, "Brak ID rekordu do aktualizacji."
    try:
        table = airtable_api.table(AIRTABLE_BASE_ID, AIRTABLE_BOOKINGS_TABLE_NAME)
        update_data = {
            'Imię Rodzica': student_data.get('parent_first_name', ''),
            'Nazwisko Rodzica': student_data.get('parent_last_name', ''),
            'Imię Ucznia': student_data.get('student_first_name', ''),
            'Nazwisko Ucznia': student_data.get('student_last_name', ''),
            'Klasa': student_data.get('grade_info', ''),
            'Poziom': student_data.get('level_info', ''),
            'Status': 'Dane zebrane - oczekiwanie na potwierdzenie'
        }
        table.update(record_id, update_data)
        logging.info(f"Zaktualizowano rekord {record_id} pomyślnie.")
        
        full_updated_record = table.get(record_id)
        if full_updated_record and 'fields' in full_updated_record:
            calendar_service = get_calendar_service()
            # POPRAWKA: Przekazanie `subject_to_calendars` do funkcji tworzącej wydarzenie
            create_google_event_from_airtable(calendar_service, full_updated_record['fields'], subject_to_calendars)
        else:
            logging.error(f"Nie udało się pobrać zaktualizowanych danych rekordu {record_id}.")
        return True, None
    except Exception as e:
        logging.error(f"Błąd aktualizacji Fazy 2 lub tworzenia w GCal: {e}", exc_info=True)
        return False, "Błąd systemu bazy danych."

def log_statistic(event_type):
    """Loguje statystykę w Airtable."""
    if not airtable_api:
        logging.error("[Stats] Usługa Airtable niedostępna.")
        return
    try:
        table = airtable_api.table(AIRTABLE_BASE_ID, AIRTABLE_STATS_TABLE_NAME)
        field_to_increment = "Nowe Kontakty" if event_type == "new_contact" else "Rezerwacje"
        today_str = datetime.datetime.now(pytz.timezone(AIRTABLE_TIMEZONE)).strftime('%Y-%m-%d')
        formula = f"IS_SAME({{Data}}, '{today_str}', 'day')"
        existing_record = table.first(formula=formula)
        
        if existing_record:
            record_id = existing_record['id']
            current_value = existing_record.get('fields', {}).get(field_to_increment, 0)
            new_value = (current_value or 0) + 1
            table.update(record_id, {field_to_increment: new_value})
        else:
            table.create({"Data": today_str, field_to_increment: 1})
        logging.info(f"[Stats] Zalogowano statystykę: {event_type}")

    except Exception as e:
        logging.error(f"[Stats] Błąd logowania '{event_type}': {e}", exc_info=True)


# =====================================================================
# === FUNKCJE KOMUNIKACJI FB ==========================================
# =====================================================================

def _send_typing_on(recipient_id, page_access_token):
    """Wysyła wskaźnik 'pisania'."""
    if not page_access_token or not ENABLE_TYPING_DELAY: return
    params = {"access_token": page_access_token}
    payload = {"recipient": {"id": recipient_id}, "sender_action": "typing_on"}
    try:
        requests.post(FACEBOOK_GRAPH_API_URL, params=params, json=payload, timeout=3)
    except requests.exceptions.RequestException as e:
        logging.warning(f"[{recipient_id}] Błąd wysyłania 'typing_on': {e}")

def _send_single_message(recipient_id, message_text, page_access_token):
    """Wysyła pojedynczy fragment wiadomości."""
    if not all([recipient_id, message_text, page_access_token]):
        logging.error("Błąd wysyłania: Brak ID, treści lub tokenu.")
        return False

    params = {"access_token": page_access_token}
    payload = {"recipient": {"id": recipient_id}, "message": {"text": message_text}, "messaging_type": "RESPONSE"}
    try:
        r = requests.post(FACEBOOK_GRAPH_API_URL, params=params, json=payload, timeout=30)
        r.raise_for_status()
        response_json = r.json()
        if fb_error := response_json.get('error'):
            logging.error(f"BŁĄD FB API (wysyłanie) do {recipient_id}: {fb_error}")
            return False
        return True
    except requests.exceptions.RequestException as e:
        logging.error(f"Błąd wysyłania do {recipient_id}: {e}", exc_info=True)
        return False

def send_message(recipient_id, full_message_text, page_access_token):
    """Wysyła wiadomość, dzieląc ją w razie potrzeby."""
    if not (full_message_text and isinstance(full_message_text, str) and full_message_text.strip()):
        return

    message_len = len(full_message_text)
    if ENABLE_TYPING_DELAY:
        estimated_typing_duration = min(MAX_TYPING_DELAY_SECONDS, max(MIN_TYPING_DELAY_SECONDS, message_len / TYPING_CHARS_PER_SECOND))
        _send_typing_on(recipient_id, page_access_token)
        time.sleep(estimated_typing_duration)

    chunks = []
    if message_len <= MESSAGE_CHAR_LIMIT:
        chunks.append(full_message_text)
    else:
        remaining_text = full_message_text
        while remaining_text:
            if len(remaining_text) <= MESSAGE_CHAR_LIMIT:
                chunks.append(remaining_text.strip())
                break
            split_index = remaining_text.rfind('\n\n', 0, MESSAGE_CHAR_LIMIT)
            if split_index == -1: split_index = remaining_text.rfind('\n', 0, MESSAGE_CHAR_LIMIT)
            if split_index == -1: split_index = remaining_text.rfind('. ', 0, MESSAGE_CHAR_LIMIT)
            if split_index == -1: split_index = MESSAGE_CHAR_LIMIT
            else: split_index += 1
            
            chunks.append(remaining_text[:split_index].strip())
            remaining_text = remaining_text[split_index:].strip()

    for i, chunk_text in enumerate(chunks):
        if not _send_single_message(recipient_id, chunk_text, page_access_token):
            logging.error(f"Błąd wysyłania fragmentu {i+1}/{len(chunks)} do {recipient_id}. Anulowanie.")
            break
        if len(chunks) > 1 and i < len(chunks) - 1:
            if ENABLE_TYPING_DELAY: _send_typing_on(recipient_id, page_access_token)
            time.sleep(MESSAGE_DELAY_SECONDS)

def _simulate_typing(recipient_id, duration_seconds, page_access_token):
    """Wysyła 'typing_on' i czeka."""
    if ENABLE_TYPING_DELAY and duration_seconds > 0:
        _send_typing_on(recipient_id, page_access_token)
        time.sleep(min(duration_seconds, MAX_TYPING_DELAY_SECONDS * 1.1))

# =====================================================================
# === FUNKCJE WYWOŁANIA AI ============================================
# =====================================================================

def _call_gemini(user_psid, prompt_history, generation_config, task_name, page_access_token, max_retries=3):
    """Wywołuje API Gemini z obsługą błędów."""
    if not gemini_model:
        logging.error(f"KRYTYCZNY BŁĄD: Model Gemini ({task_name}) niedostępny!")
        return "Przepraszam, błąd wewnętrzny systemu."

    logging.info(f"[{user_psid}] Wywołanie Gemini: {task_name} (Prompt: {len(prompt_history)} wiad.)")
    
    attempt = 0
    while attempt < max_retries:
        attempt += 1
        try:
            _simulate_typing(user_psid, MIN_TYPING_DELAY_SECONDS * 0.8, page_access_token)
            response = gemini_model.generate_content(prompt_history, generation_config=generation_config, safety_settings=SAFETY_SETTINGS, stream=False)

            if response and response.candidates:
                candidate = response.candidates[0]
                if candidate.finish_reason.name == "STOP":
                    generated_text = "".join(part.text for part in candidate.content.parts).strip()
                    if generated_text:
                        logging.info(f"[{user_psid}] Gemini ({task_name}) zwróciło odpowiedź.")
                        return generated_text
                logging.warning(f"[{user_psid}] Gemini ({task_name}) zakończone z powodem: {candidate.finish_reason.name}")
            else:
                block_reason = response.prompt_feedback.block_reason.name if response and response.prompt_feedback else "Nieznany"
                logging.error(f"BŁĄD [{user_psid}] Gemini ({task_name}) - PROMPT ZABLOKOWANY! Powód: {block_reason}")
                return "Twoja wiadomość nie mogła być przetworzona (zasady bezpieczeństwa)."

        except Exception as e:
            logging.error(f"BŁĄD [{user_psid}] Gemini ({task_name}) - Próba {attempt}/{max_retries}: {e}", exc_info=True)
        
        if attempt < max_retries:
            time.sleep((2 ** attempt) + random.random())
    
    logging.error(f"KRYTYCZNY BŁĄD [{user_psid}] Gemini ({task_name}) - Nie udało się uzyskać odpowiedzi po {max_retries} próbach.")
    return "Przepraszam, nie udało się przetworzyć Twojej wiadomości."

# =====================================================================
# === INSTRUKCJE SYSTEMOWE I GŁÓWNE FUNKCJE AI ========================
# =====================================================================

SYSTEM_INSTRUCTION_SCHEDULING = """Jesteś pomocnym asystentem AI specjalizującym się w umawianiu terminów korepetycji online. Twoim zadaniem jest znalezienie pasującego terminu dla użytkownika na podstawie jego preferencji oraz dostarczonej listy dostępnych zakresów czasowych.
Kontekst:
*   Rozmawiasz z użytkownikiem, który wyraził chęć umówienia się na lekcję z przedmiotu: **{subject}**.
*   Poniżej znajduje się lista AKTUALNIE dostępnych ZAKRESÓW czasowych **dla przedmiotu {subject}**, w których można umówić wizytę (każda trwa {duration} minut).
*   Masz dostęp do historii poprzedniej rozmowy.
Styl Komunikacji:
*   Bądź naturalny, profesjonalny, uprzejmy (zwroty "Państwo") i nie używaj emotikon.
Dostępne zakresy czasowe dla {subject}:
{available_ranges_text}
Twoje zadanie:
1.  **Rozpocznij/Wznów:** Jeśli to początek umawiania, zapytaj o **ogólne preferencje** (dzień, pora dnia). Nie proponuj od razu konkretnej daty.
2.  **Negocjuj:** Na podstawie odpowiedzi, **zaproponuj konkretny termin z listy**.
3.  **Potwierdź i dodaj znacznik:** Gdy ustalicie **dokładny termin z listy**, potwierdź go (np. "Świetnie, proponowany termin na {subject} to środa, 15 maja o 18:30.") i **zakończ swoją odpowiedź DOKŁADNIE znacznikiem** `{slot_marker_prefix}YYYY-MM-DDTHH:MM:SS{slot_marker_suffix}`. Użyj formatu ISO 8601 (np. 2024-05-15T18:30:00).
4.  **NIE dodawaj znacznika**, jeśli nie ma zgody lub termin jest spoza listy.
5.  **Brak terminów:** Jeśli lista jest pusta, poinformuj o tym.
6.  **Pytania poza tematem:** Jeśli pytanie jest niezwiązane z umawianiem, odpowiedz **TYLKO I WYŁĄCZNIE** znacznikiem: `{switch_marker}`.
Pamiętaj:
*   Proponuj godziny zaokrąglone do pełnych 10 minut (np. 16:20, 17:40).
*   Trzymaj się **wyłącznie** terminów z dostarczonej listy.
"""

SYSTEM_INSTRUCTION_GATHERING = """Twoim zadaniem jest zebranie informacji o UCZNIU, potrzebnych do zapisu na korepetycje. Dane rodzica zostaną pobrane automatycznie.
Kontekst:
*   Wstępnie ustalony termin: {proposed_slot_formatted}.
*   Informacje o UCZNIU już znane (mogą być puste): Imię: {known_student_first_name}, Nazwisko: {known_student_last_name}, Klasa/Szkoła: {known_grade}, Poziom: {known_level}.
Styl Komunikacji:
*   Bądź naturalny, profesjonalny i nie używaj emotikon.
Twoje zadania:
1.  **ZDOBĄDŹ INFORMACJE:** Uprzejmie poproś o brakujące dane ucznia: **Pełne Imię i Nazwisko**, **Klasa i typ szkoły** (np. "7 klasa podstawówki"), **Poziom** (podst./rozsz. - TYLKO dla liceum/technikum).
2.  **IGNORUJ dane rodzica.**
3.  **Zakończ po zebraniu danych:** Kiedy masz już wszystkie wymagane informacje o uczniu, Twoja ostatnia odpowiedź **MUSI** mieć strukturę:
    a)  Linijka: `ZEBRANE_DANE_UCZNIA: [Imię: <imię>, Nazwisko: <nazwisko>, KlasaInfo: <pełna info o klasie>, Poziom: <Podstawowy/Rozszerzony/Brak>]`
    b)  W nowej linii wiadomość: "Dobrze, dziękujemy za wszystkie informacje. Aby lekcja się odbyła prosimy jeszcze o potwierdzenie zajęć wysyłając wiadomość "POTWIERDZAM" na podany profil. Jest to profil także to dalszego kontaktu w sprawie zajęć: https://www.facebook.com/profile.php?id=61576135251276"
    c)  Na samym końcu znacznik: `{info_gathered_marker}`.
4.  **Pytania poza tematem:** Odpowiedz **TYLKO** znacznikiem: `{switch_marker}`.
"""

SYSTEM_INSTRUCTION_GENERAL_RAW = """Jesteś przyjaznym asystentem klienta centrum korepetycji. Twoim celem jest przeprowadzenie klienta przez ofertę i zachęcenie go do umówienia lekcji.
Styl Komunikacji:
*   Bądź naturalny, profesjonalny (zwroty "Państwo") i nie używaj emotikon.
Dostępne Przedmioty (ogólnie): {available_subjects_list}
{dynamic_subject_link_info}
Cennik (60 minut):
*   Szkoła Podstawowa: 60 zł
*   Liceum/Technikum (Podstawa, kl. 1-3): 65 zł; (Podstawa, kl. 4/5): 70 zł
*   Liceum/Technikum (Rozszerzenie, kl. 1-3): 70 zł; (Rozszerzenie, kl. 4/5): 75 zł
Format Lekcji: Online (Microsoft Teams, przez link).
Format Odpowiedzi:
*   Gdy po raz pierwszy podajesz cenę, **TWOJA ODPOWIEDŹ MUSI ZAWIERAĆ** znacznik: `[DANE_UCZNIA_OGOLNE: KlasaInfo: <info o klasie>, Poziom: <Podstawowy/Rozszerzony/Brak>]`.
Twój Przepływ Pracy:
1.  **Identyfikacja Potrzeby:** Jeśli znasz przedmiot (z kontekstu strony: {current_subject_from_page}), potwierdź go.
2.  **Informacja:** Poinformuj o innych przedmiotach i podaj linki, jeśli są dostępne.
3.  **Zbieranie Danych:** Zapytaj o klasę, typ szkoły i (dla szkoły średniej) poziom. Jeśli klasa < 4 SP, poinformuj o braku oferty.
4.  **Prezentacja Ceny:** Na podstawie danych, ustal cenę. Zbuduj odpowiedź: najpierw znacznik `[DANE_UCZNIA_OGOLNE: ...]`, potem wiadomość dla klienta.
5.  **Zachęta:** Po podaniu ceny, zapytaj o chęć umówienia lekcji.
6.  **Obsługa Odpowiedzi:**
    *   Jeśli **TAK**: Odpowiedz **TYLKO I WYŁĄCZNIE** znacznikiem: `{intent_marker}`.
    *   Jeśli **NIE**: Kontynuuj rozmowę.
    *   Jeśli jesteś w trybie tymczasowym (odpowiadasz na pytanie ogólne i użytkownik nie kontynuuje tematu), dodaj znacznik `{return_marker}` do swojej odpowiedzi.
"""

def get_gemini_scheduling_response(user_psid, history_for_scheduling_ai, current_user_message_text, available_ranges, required_subject, page_access_token):
    if not required_subject: return "Błąd: brak przedmiotu."
    ranges_text = format_ranges_for_ai(available_ranges, subject=required_subject)
    system_instruction = SYSTEM_INSTRUCTION_SCHEDULING.format(
        subject=required_subject, available_ranges_text=ranges_text, duration=APPOINTMENT_DURATION_MINUTES,
        slot_marker_prefix=SLOT_ISO_MARKER_PREFIX, slot_marker_suffix=SLOT_ISO_MARKER_SUFFIX, switch_marker=SWITCH_TO_GENERAL
    )
    initial_prompt = [
        Content(role="user", parts=[Part.from_text(system_instruction)]),
        Content(role="model", parts=[Part.from_text(f"Rozumiem. Ustalam termin dla **{required_subject}**.")])
    ]
    full_prompt = initial_prompt + history_for_scheduling_ai
    if current_user_message_text:
        full_prompt.append(Content(role="user", parts=[Part.from_text(current_user_message_text)]))
    
    return _call_gemini(user_psid, full_prompt, GENERATION_CONFIG_SCHEDULING, f"Scheduling ({required_subject})", page_access_token)

def get_gemini_gathering_response(user_psid, history_for_gathering_ai, current_user_message_text, context_info, page_access_token):
    system_instruction = SYSTEM_INSTRUCTION_GATHERING.format(
        proposed_slot_formatted=context_info.get("proposed_slot_formatted", "nie ustalono"),
        known_student_first_name=context_info.get("known_student_first_name", ""),
        known_student_last_name=context_info.get("known_student_last_name", ""),
        known_grade=context_info.get("known_grade", ""), known_level=context_info.get("known_level", ""),
        info_gathered_marker=INFO_GATHERED_MARKER, switch_marker=SWITCH_TO_GENERAL
    )
    initial_prompt = [
        Content(role="user", parts=[Part.from_text(system_instruction)]),
        Content(role="model", parts=[Part.from_text("Rozumiem. Zbiorę brakujące dane o uczniu.")])
    ]
    full_prompt = initial_prompt + history_for_gathering_ai
    if current_user_message_text:
        full_prompt.append(Content(role="user", parts=[Part.from_text(current_user_message_text)]))

    return _call_gemini(user_psid, full_prompt, GENERATION_CONFIG_GATHERING, "Info Gathering", page_access_token)

# POPRAWKA: Ujednolicono nazwę argumentu i dodano `page_config`.
def get_gemini_general_response(user_psid, current_user_message_text, history_for_general_ai, is_temporary_general_state, page_access_token, current_subject_for_context=None, page_config=None):
    if page_config is None: page_config = {}
    
    all_subject_links = {
        page_data["subject"]: page_data["link"]
        for page_data in page_config.values() if "subject" in page_data and "link" in page_data
    }
    available_subjects = sorted(list(all_subject_links.keys()))
    
    link_info_text_for_prompt = "**Informacje o stronach i linkach:**\n" + "\n".join([f"Strona dla '{name}': {link}" for name, link in all_subject_links.items()]) if all_subject_links else "Brak danych o linkach."

    system_instruction_formatted = SYSTEM_INSTRUCTION_GENERAL_RAW.format(
        available_subjects_list=", ".join(available_subjects),
        dynamic_subject_link_info=link_info_text_for_prompt,
        current_subject_from_page=(current_subject_for_context or "nieokreślony"),
        intent_marker=INTENT_SCHEDULE_MARKER,
        return_marker=RETURN_TO_PREVIOUS
    )
    
    model_ack = f"Rozumiem. Jestem asystentem klienta. Kontekstowy przedmiot: {current_subject_for_context or 'brak'}."
    if is_temporary_general_state: model_ack += f" Po odpowiedzi, jeśli trzeba, dodam {RETURN_TO_PREVIOUS}."

    initial_prompt = [
        Content(role="user", parts=[Part.from_text(system_instruction_formatted)]),
        Content(role="model", parts=[Part.from_text(model_ack)])
    ]
    
    full_prompt = initial_prompt + history_for_general_ai
    if current_user_message_text:
        full_prompt.append(Content(role="user", parts=[Part.from_text(current_user_message_text)]))

    return _call_gemini(user_psid, full_prompt, GENERATION_CONFIG_DEFAULT, "General Conversation", page_access_token)

# =====================================================================
# === WEBHOOK HANDLERS ================================================
# =====================================================================

@app.route('/webhook', methods=['GET'])
def webhook_verification():
    """Obsługuje weryfikację webhooka przez Facebooka."""
    if request.args.get('hub.mode') == 'subscribe' and request.args.get('hub.verify_token') == VERIFY_TOKEN:
        logging.info("Weryfikacja GET OK!")
        return Response(request.args.get('hub.challenge'), status=200)
    else:
        logging.warning("Weryfikacja GET NIEUDANA.")
        return Response("Verification failed", status=403)

@app.route('/webhook', methods=['POST'])
def webhook_handle():
    """Główny handler dla przychodzących zdarzeń."""
    data = request.json
    logging.info(f"\n{'='*20} POST /webhook {'='*20}")
    
    if data.get("object") == "page":
        for entry in data.get("entry", []):
            page_id = entry.get("id")
            for event in entry.get("messaging", []):
                thread = threading.Thread(target=process_single_event, args=(event.copy(), page_id))
                thread.start()
        return Response("EVENT_RECEIVED", status=200)
    else:
        return Response("NOT_PAGE_EVENT", status=404)

# =====================================================================
# === URUCHOMIENIE SERWERA ============================================
# =====================================================================
if __name__ == '__main__':
    ensure_dir(HISTORY_DIR)
    log_level = os.environ.get("LOG_LEVEL", "DEBUG").upper()
    logging.basicConfig(level=log_level, format='%(asctime)s - %(levelname)s - [%(filename)s:%(lineno)d] - %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
    logging.getLogger('googleapiclient').setLevel(logging.WARNING)
    logging.getLogger('urllib3').setLevel(logging.WARNING)
    logging.getLogger('werkzeug').setLevel(logging.WARNING)

    # POPRAWKA: Wczytanie konfiguracji na starcie do celów informacyjnych
    print("\n" + "="*60)
    print("--- START BOTA (Wiele Stron FB + Statystyki) ---")
    config = load_and_process_config()
    PAGE_CONFIG = config['PAGE_CONFIG']
    SUBJECT_TO_CALENDARS = config['SUBJECT_TO_CALENDARS']
    AVAILABLE_SUBJECTS = config['AVAILABLE_SUBJECTS']

    print(f"  * Poziom logowania: {log_level}")
    print("-" * 60)
    print("  Konfiguracja Facebook:")
    print(f"    FB_VERIFY_TOKEN: {'OK' if VERIFY_TOKEN and VERIFY_TOKEN != 'KOLAGEN' else 'DOMYŚLNY lub BRAK!'}")
    print("    Skonfigurowane Strony:")
    if PAGE_CONFIG:
        for page_id, cfg in PAGE_CONFIG.items():
            token_status = "OK" if cfg.get("token") and len(cfg["token"]) > 50 else "BRAK lub ZBYT KRÓTKI!!!"
            print(f"      - ID: {page_id}, Nazwa: '{cfg.get('name', 'Brak')}', Przedmiot: {cfg.get('subject', 'Brak')}, Token: {token_status}")
    else:
        print("!!! KRYTYCZNE: Brak skonfigurowanych stron w config.json !!!")
    
    print("-" * 60)
    print("  Konfiguracja Vertex AI:")
    print(f"    Projekt: {PROJECT_ID}, Lokalizacja: {LOCATION}, Model: {MODEL_ID}")
    print(f"    Model Gemini: {'Załadowany (OK)' if gemini_model else 'BŁĄD INICJALIZACJI!'}")
    
    print("-" * 60)
    print("  Konfiguracja Google Calendar:")
    print(f"    Dostępne przedmioty: {', '.join(AVAILABLE_SUBJECTS)}")
    print("    Przypisanie Kalendarzy do Przedmiotów:")
    if SUBJECT_TO_CALENDARS:
        for subject, cal_list in SUBJECT_TO_CALENDARS.items():
            print(f"      - {subject.capitalize()}: {[c['name'] for c in cal_list]}")
    else:
        print("      !!! BRAK skonfigurowanych kalendarzy dla przedmiotów !!!")
    print(f"    Strefa: {CALENDAR_TIMEZONE} (TZ: {_get_calendar_timezone()})")
    print(f"    Plik klucza: {CALENDAR_SERVICE_ACCOUNT_FILE} ({'OK' if os.path.exists(CALENDAR_SERVICE_ACCOUNT_FILE) else 'BRAK!!!'})")
    print(f"    Usługa Calendar API: {'OK' if get_calendar_service() else 'BŁĄD INICJALIZACJI!'}")

    print("-" * 60)
    print("  Konfiguracja Airtable:")
    print(f"    ID Bazy: {AIRTABLE_BASE_ID}")
    print(f"    Klucz API: {'OK (załadowany)' if AIRTABLE_API_KEY else 'BRAK!'}")
    print(f"    Usługa Airtable API: {'OK' if airtable_api else 'BŁĄD INICJALIZACJI!'}")
    print("="*60 + "\n")
    
    port = int(os.environ.get("PORT", 8080))
    run_in_debug_mode = (log_level == 'DEBUG')
    print(f"Uruchamianie serwera Flask na porcie {port} (tryb debug: {run_in_debug_mode})...")
    
    if not run_in_debug_mode:
        try:
            from waitress import serve
            serve(app, host='0.0.0.0', port=port, threads=16)
        except ImportError:
            print("!!! Ostrz.: 'waitress' nie znaleziono. Uruchamiam serwer deweloperski Flask (niezalecane na produkcji).")
            app.run(host='0.0.0.0', port=port, debug=False)
    else:
        app.run(host='0.0.0.0', port=port, debug=True, use_reloader=False)
