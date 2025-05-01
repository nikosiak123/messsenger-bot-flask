# -*- coding: utf-8 -*-

# verify_server.py (Wersja z autonomicznym AI do planowania)

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
MODEL_ID = os.environ.get("VERTEX_MODEL_ID", "gemini-2.0-flash-001") # Użyjemy tego samego modelu dla obu ról

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
# Usunięto PREFERRED_WEEKDAY/WEEKEND_START_HOUR - AI ma decydować
MAX_SEARCH_DAYS = 14

# --- Znaczniki i Stany ---
INTENT_SCHEDULE_MARKER = "[INTENT_SCHEDULE]"
SLOT_ISO_MARKER_PREFIX = "[SLOT_ISO:"
SLOT_ISO_MARKER_SUFFIX = "]"
STATE_GENERAL = "general"
STATE_SCHEDULING_ACTIVE = "scheduling_active"
# STATE_WAITING_FOR_FEEDBACK usunięty

# --- Ustawienia Modelu Gemini ---
# Usunięto GENERATION_CONFIG_PROPOSAL i GENERATION_CONFIG_FEEDBACK_SIMPLE
GENERATION_CONFIG_SCHEDULING = GenerationConfig( # Konfiguracja dla AI planującego
    temperature=0.5, # Pozwalamy na trochę więcej kreatywności w rozmowie
    top_p=0.95,
    top_k=40,
    max_output_tokens=512, # Może potrzebować więcej tokenów na rozmowę
)
GENERATION_CONFIG_DEFAULT = GenerationConfig( # Dla ogólnej rozmowy
    temperature=0.7,
    top_p=0.95,
    top_k=40,
    max_output_tokens=1024,
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
try: locale.setlocale(locale.LC_TIME, 'pl_PL.UTF-8')
except locale.Error:
    try: locale.setlocale(locale.LC_TIME, 'Polish_Poland.1250')
    except locale.Error: logging.warning("Nie można ustawić polskiej lokalizacji dla formatowania dat.")

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
# === FUNKCJE POMOCNICZE (Bez większych zmian) ========================
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
                    # Szukamy ostatniego wpisu systemowego, który jest słownikiem i ma klucz 'type'
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
                    # Sprawdzamy, czy to jest *ten* ostatni wpis systemowy
                    elif isinstance(msg_data, dict) and msg_data.get('role') == 'system' and 'type' in msg_data:
                        if i == last_system_message_index:
                            context = msg_data
                            logging.debug(f"[{user_psid}] Odczytano AKTYWNY kontekst: {context}")
                        else:
                            logging.debug(f"[{user_psid}] Pominięto stary kontekst (idx {i}): {msg_data}")
                    else:
                        logging.warning(f"Ostrz. [{user_psid}]: Pominięto niepoprawną wiadomość/kontekst (idx {i}): {msg_data}")

                # Upewnij się, że stan jest poprawny, domyślnie general
                if 'type' not in context or context['type'] not in [STATE_GENERAL, STATE_SCHEDULING_ACTIVE]:
                    context = {'type': STATE_GENERAL} # Reset do stanu generalnego jeśli coś jest nie tak

                logging.info(f"[{user_psid}] Wczytano historię: {len(history)} wiad. Stan: {context.get('type', STATE_GENERAL)}")
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
            history_to_process = history_to_process[-max_messages_to_save:]
        for msg in history_to_process:
             if isinstance(msg, Content) and hasattr(msg, 'role') and msg.role in ('user', 'model') and hasattr(msg, 'parts') and isinstance(msg.parts, list):
                parts_data = [{'text': part.text} for part in msg.parts if isinstance(part, Part) and hasattr(part, 'text')]
                if parts_data:
                    history_data.append({'role': msg.role, 'parts': parts_data})
             else:
                logging.warning(f"Ostrz. [{user_psid}]: Pomijanie nieprawidłowego obiektu historii: {msg}")

        # Zapisujemy kontekst tylko jeśli nie jest to stan generalny
        if context_to_save and isinstance(context_to_save, dict) and context_to_save.get('type') and context_to_save['type'] != STATE_GENERAL:
             context_to_save['role'] = 'system' # Dodajemy rolę dla spójności
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
            # Próba bezpośredniego parsowania ISO 8601
            dt = datetime.datetime.fromisoformat(dt_str)
        except ValueError:
            # Obsługa formatów z 'Z' lub offsetem, które fromisoformat może nie łapać
            if dt_str.endswith('Z'):
                try:
                    # Spróbuj z mikrosekundami
                    dt = datetime.datetime.strptime(dt_str, '%Y-%m-%dT%H:%M:%S.%fZ')
                    dt = pytz.utc.localize(dt)
                except ValueError:
                     try:
                         # Spróbuj bez mikrosekund
                         dt = datetime.datetime.strptime(dt_str, '%Y-%m-%dT%H:%M:%SZ')
                         dt = pytz.utc.localize(dt)
                     except ValueError:
                         logging.warning(f"Ostrz.: Nie sparsowano dateTime (Z): {dt_str}")
                         return None
            else:
                # Spróbuj sparsować z offsetem
                try:
                    # Usuń dwukropek w offsecie, jeśli jest (np. +01:00 -> +0100)
                    if ':' in dt_str[-6:]:
                       dt_str_no_colon = dt_str[:-3] + dt_str[-2:]
                    else:
                       dt_str_no_colon = dt_str
                    dt = datetime.datetime.strptime(dt_str_no_colon, '%Y-%m-%dT%H:%M:%S%z')
                except ValueError:
                    logging.warning(f"Ostrz.: Nie sparsowano dateTime (offset): {dt_str}")
                    return None

        # Upewnij się, że mamy świadomy obiekt datetime w docelowej strefie
        if dt.tzinfo is None:
            # Jeśli naiwny, załóż, że jest w domyślnej strefie (choć API zwykle zwraca świadome)
            dt = default_tz.localize(dt)
            logging.warning(f"Ostrz.: Parsowany czas był naiwny, zlokalizowano do {default_tz.zone}: {dt_str}")
        else:
            # Jeśli świadomy, skonwertuj do docelowej strefy
            dt = dt.astimezone(default_tz)
        return dt
    elif 'date' in event_time_data: # Obsługa wydarzeń całodniowych (mniej istotne dla freebusy)
        try:
            return datetime.date.fromisoformat(event_time_data['date'])
        except ValueError:
            logging.warning(f"Ostrz.: Nie sparsowano date: {event_time_data['date']}")
            return None
    return None # Jeśli ani dateTime ani date nie pasuje

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
    start_datetime = max(start_datetime, now) # Zawsze zaczynaj od teraz lub później
    if start_datetime >= end_datetime:
        logging.info(f"Zakres wyszukiwania [{start_datetime} - {end_datetime}] nieprawidłowy lub w przeszłości.")
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
        busy_times_raw = freebusy_result.get('calendars', {}).get(calendar_id, {}).get('busy', [])

        # Przetwarzanie zajętych slotów
        busy_times = []
        for busy_slot in busy_times_raw:
            try:
                # Używamy parse_event_time do poprawnego sparsowania i konwersji stref
                busy_start = parse_event_time({'dateTime': busy_slot['start']}, tz)
                busy_end = parse_event_time({'dateTime': busy_slot['end']}, tz)

                if busy_start and busy_end:
                    # Przycinamy zajęty czas do naszego okna wyszukiwania
                    busy_start_clipped = max(busy_start, start_datetime)
                    busy_end_clipped = min(busy_end, end_datetime)
                    # Dodajemy tylko jeśli jest jakaś część wspólna w naszym oknie
                    if busy_start_clipped < busy_end_clipped:
                        busy_times.append({'start': busy_start_clipped, 'end': busy_end_clipped})
                else:
                    logging.warning(f"Ostrz.: Nie udało się sparsować zajętego czasu: start={busy_slot.get('start')}, end={busy_slot.get('end')}")

            except Exception as e:
                logging.warning(f"Ostrz.: Błąd przetwarzania zajętego czasu: {busy_slot}, błąd: {e}")

    except HttpError as error:
        logging.error(f'Błąd API Freebusy: {error}', exc_info=True)
        return []
    except Exception as e:
        logging.error(f"Nieoczekiwany błąd Freebusy: {e}", exc_info=True)
        return []

    # Sortowanie i scalanie nakładających się zajętych przedziałów
    busy_times.sort(key=lambda x: x['start'])
    merged_busy_times = []
    for busy in busy_times:
        if not merged_busy_times or busy['start'] > merged_busy_times[-1]['end']:
            merged_busy_times.append(busy)
        else:
            # Scalanie - rozszerzamy koniec poprzedniego przedziału
            merged_busy_times[-1]['end'] = max(merged_busy_times[-1]['end'], busy['end'])

    # Generowanie wolnych zakresów na podstawie scalonych zajętych
    free_ranges = []
    current_time = start_datetime
    for busy_slot in merged_busy_times:
        if current_time < busy_slot['start']:
            free_ranges.append({'start': current_time, 'end': busy_slot['start']})
        current_time = max(current_time, busy_slot['end'])

    # Dodanie ostatniego wolnego zakresu (od końca ostatniego zajętego do końca okna)
    if current_time < end_datetime:
        free_ranges.append({'start': current_time, 'end': end_datetime})

    # Filtrowanie wolnych zakresów: godziny pracy i minimalny czas trwania
    final_free_slots = []
    min_duration_delta = datetime.timedelta(minutes=APPOINTMENT_DURATION_MINUTES)

    for free_range in free_ranges:
        range_start = free_range['start']
        range_end = free_range['end']

        # Iterujemy dzień po dniu w ramach wolnego zakresu
        current_day_start = range_start
        while current_day_start < range_end:
            day_date = current_day_start.date()

            # Definiujemy godziny pracy dla danego dnia
            work_day_start_time = datetime.time(WORK_START_HOUR, 0)
            work_day_end_time = datetime.time(WORK_END_HOUR, 0)
            work_day_start = tz.localize(datetime.datetime.combine(day_date, work_day_start_time))
            work_day_end = tz.localize(datetime.datetime.combine(day_date, work_day_end_time))

            # Znajdujemy część wspólną wolnego zakresu i godzin pracy w danym dniu
            intersect_start = max(current_day_start, work_day_start)
            intersect_end = min(range_end, work_day_end)

            # Sprawdzamy, czy przecięcie jest wystarczająco długie
            if intersect_start < intersect_end and (intersect_end - intersect_start) >= min_duration_delta:
                # Zaokrąglamy początek do najbliższych 10 minut w górę (opcjonalne, ale może uprościć)
                if intersect_start.minute % 10 != 0 or intersect_start.second > 0 or intersect_start.microsecond > 0:
                    minutes_to_add = 10 - (intersect_start.minute % 10)
                    rounded_start = intersect_start + datetime.timedelta(minutes=minutes_to_add)
                    rounded_start = rounded_start.replace(second=0, microsecond=0)
                else:
                    rounded_start = intersect_start

                # Upewniamy się, że po zaokrągleniu nadal mieścimy się w zakresie i jest dość czasu
                if rounded_start < intersect_end and (intersect_end - rounded_start) >= min_duration_delta:
                    final_free_slots.append({'start': rounded_start, 'end': intersect_end})

            # Przechodzimy do początku następnego dnia (lub końca zakresu, jeśli wcześniej)
            next_day_date = day_date + datetime.timedelta(days=1)
            next_day_start_naive = datetime.datetime.combine(next_day_date, datetime.time(0, 0))
            current_day_start = tz.localize(next_day_start_naive)
            # Zapobiegamy cofaniu się, jeśli range_start był w środku dnia
            current_day_start = max(current_day_start, range_start)


    logging.info(f"Znaleziono {len(final_free_slots)} wolnych zakresów czasowych w godz. pracy.")
    # Logowanie pierwszych kilku dla debugowania
    for i, slot in enumerate(final_free_slots[:5]):
         logging.debug(f"  Slot {i+1}: {slot['start']:%Y-%m-%d %H:%M} - {slot['end']:%Y-%m-%d %H:%M}")
    if len(final_free_slots) > 5:
         logging.debug("  ...")

    return final_free_slots


def is_slot_actually_free(start_time, calendar_id):
    """Weryfikuje w czasie rzeczywistym, czy slot jest wolny."""
    service = get_calendar_service()
    tz = _get_timezone()
    if not service:
        logging.error("Błąd: Usługa kalendarza niedostępna do weryfikacji.")
        return False # Bezpieczniej założyć, że zajęty

    if not isinstance(start_time, datetime.datetime):
        logging.error(f"Błąd weryfikacji: start_time nie jest obiektem datetime ({type(start_time)})")
        return False

    # Upewnij się, że czas jest świadomy i w odpowiedniej strefie
    if start_time.tzinfo is None: start_time = tz.localize(start_time)
    else: start_time = start_time.astimezone(tz)
    end_time = start_time + datetime.timedelta(minutes=APPOINTMENT_DURATION_MINUTES)

    # Sprawdzamy bardzo wąski zakres czasu
    body = {
        "timeMin": start_time.isoformat(),
        "timeMax": end_time.isoformat(),
        "timeZone": CALENDAR_TIMEZONE,
        "items": [{"id": calendar_id}]
    }
    try:
        logging.debug(f"Weryfikacja freebusy dla: {start_time:%Y-%m-%d %H:%M} - {end_time:%Y-%m-%d %H:%M}")
        freebusy_result = service.freebusy().query(body=body).execute()
        busy_times = freebusy_result.get('calendars', {}).get(calendar_id, {}).get('busy', [])

        if not busy_times:
            logging.info(f"Weryfikacja: Slot {start_time:%Y-%m-%d %H:%M} JEST wolny.")
            return True
        else:
            # Sprawdzamy, czy *jakikolwiek* zwrócony zajęty slot koliduje
            # (Teoretycznie freebusy powinno zwrócić tylko kolidujące, ale dla pewności)
            for busy in busy_times:
                busy_start = parse_event_time({'dateTime': busy['start']}, tz)
                busy_end = parse_event_time({'dateTime': busy['end']}, tz)
                # Sprawdzamy, czy przedziały [start_time, end_time) i [busy_start, busy_end) mają część wspólną
                if busy_start and busy_end and max(start_time, busy_start) < min(end_time, busy_end):
                    logging.warning(f"Weryfikacja: Slot {start_time:%Y-%m-%d %H:%M} jest ZAJĘTY (kolizja z {busy_start:%H:%M}-{busy_end:%H:%M}).")
                    return False
            # Jeśli przeszliśmy pętlę i nie znaleźliśmy kolizji (np. freebusy zwróciło coś tuż obok)
            logging.info(f"Weryfikacja: Slot {start_time:%Y-%m-%d %H:%M} jest wolny (brak bezpośredniej kolizji).")
            return True

    except HttpError as error:
         logging.error(f"Błąd API Freebusy podczas weryfikacji: {error}", exc_info=True)
         return False # Błąd API -> zakładamy, że zajęty
    except Exception as e:
        logging.error(f"Nieoczekiwany błąd weryfikacji Freebusy: {e}", exc_info=True)
        return False # Inny błąd -> zakładamy, że zajęty

def book_appointment(calendar_id, start_time, end_time, summary="Rezerwacja FB", description="", user_name=""):
    """Rezerwuje termin w Kalendarzu Google."""
    service = get_calendar_service()
    tz = _get_timezone()
    if not service:
        return False, "Błąd: Brak połączenia z usługą kalendarza."

    if not isinstance(start_time, datetime.datetime) or not isinstance(end_time, datetime.datetime):
         logging.error(f"Błąd rezerwacji: Nieprawidłowe typy dat ({type(start_time)}, {type(end_time)})")
         return False, "Wewnętrzny błąd daty rezerwacji."

    # Upewnij się, że czasy są świadome i w odpowiedniej strefie
    if start_time.tzinfo is None: start_time = tz.localize(start_time)
    else: start_time = start_time.astimezone(tz)
    if end_time.tzinfo is None: end_time = tz.localize(end_time)
    else: end_time = end_time.astimezone(tz)

    event_summary = summary + (f" - {user_name}" if user_name else "")
    event = {
        'summary': event_summary,
        'description': description,
        'start': {'dateTime': start_time.isoformat(), 'timeZone': CALENDAR_TIMEZONE},
        'end': {'dateTime': end_time.isoformat(), 'timeZone': CALENDAR_TIMEZONE},
        'reminders': { # Dodajemy przypomnienie
            'useDefault': False,
            'overrides': [
                {'method': 'popup', 'minutes': 60}, # Popup na godzinę przed
                {'method': 'email', 'minutes': 120} # Email na 2 godziny przed
            ],
        },
        'status': 'confirmed', # Potwierdzone od razu
    }
    try:
        logging.info(f"Próba rezerwacji: '{event_summary}' od {start_time:%Y-%m-%d %H:%M} do {end_time:%Y-%m-%d %H:%M}")
        created_event = service.events().insert(calendarId=calendar_id, body=event).execute()
        event_id = created_event.get('id')
        logging.info(f"Termin zarezerwowany pomyślnie. ID wydarzenia: {event_id}")
        # Formatowanie potwierdzenia dla użytkownika
        day_index = start_time.weekday()
        locale_day_name = POLISH_WEEKDAYS[day_index]
        hour_str = str(start_time.hour) # Godzina bez wiodącego zera
        confirm_message = f"Świetnie! Termin na {locale_day_name}, {start_time.strftime(f'%d.%m.%Y o {hour_str}:%M')} został zarezerwowany. Do zobaczenia!"
        return True, confirm_message
    except HttpError as error:
        error_details = f"Kod: {error.resp.status}, Powód: {error.resp.reason}"
        logging.error(f"Błąd API Google Calendar rezerwacji: {error}, Szczegóły: {error_details}", exc_info=True)
        if error.resp.status == 409: # Konflikt - ktoś inny zarezerwował w międzyczasie
            return False, "Niestety, ten termin został właśnie zajęty przez kogoś innego. Spróbujmy znaleźć inny."
        elif error.resp.status == 403: # Problem z uprawnieniami
             return False, "Problem z uprawnieniami do kalendarza. Skontaktuj się z administratorem."
        else: # Inny błąd API
            return False, "Wystąpił nieoczekiwany problem z systemem rezerwacji. Spróbuj ponownie później."
    except Exception as e:
        logging.error(f"Nieoczekiwany błąd Python podczas rezerwacji: {e}", exc_info=True)
        return False, "Wewnętrzny błąd systemu rezerwacji."

def format_ranges_for_ai(ranges):
    """Formatuje listę zakresów czasowych na bardziej techniczny tekst dla AI."""
    if not ranges:
        return "Brak dostępnych zakresów czasowych w podanym okresie."

    tz = _get_timezone()
    formatted_lines = [
        f"Dostępne ZAKRESY czasowe (wizyta trwa {APPOINTMENT_DURATION_MINUTES} minut). Porozmawiaj z użytkownikiem, aby znaleźć pasujący termin. Pamiętaj, że dokładny czas rozpoczęcia musi mieścić się w jednym z podanych zakresów.",
        "--- Dostępne Zakresy (Data YYYY-MM-DD, Dzień, Od Godziny HH:MM, Do Godziny HH:MM) ---"
    ]
    slots_added = 0
    max_slots_to_show = 20 # Pokażmy trochę więcej, AI może potrzebować kontekstu
    # Sortowanie dla czytelności
    sorted_ranges = sorted(ranges, key=lambda r: r['start'])

    for r in sorted_ranges:
        # Upewnijmy się, że czasy są w odpowiedniej strefie
        start_dt = r['start'].astimezone(tz)
        end_dt = r['end'].astimezone(tz)
        day_name = POLISH_WEEKDAYS[start_dt.weekday()]
        date_str = start_dt.strftime('%Y-%m-%d')
        start_time_str = start_dt.strftime('%H:%M')
        end_time_str = end_dt.strftime('%H:%M')

        # Dodajemy tylko jeśli zakres jest sensowny (np. start < end)
        if start_dt < end_dt:
            formatted_lines.append(f"- {date_str}, {day_name}, od {start_time_str}, do {end_time_str}")
            slots_added += 1
            if slots_added >= max_slots_to_show:
                formatted_lines.append("- ... (i potencjalnie więcej)")
                break

    if slots_added == 0:
        return "Brak dostępnych zakresów czasowych w godzinach pracy w podanym okresie."

    formatted_output = "\n".join(formatted_lines)
    logging.debug(f"--- Zakresy sformatowane dla AI ---\n{formatted_output}\n---------------------------------")
    return formatted_output

def format_slot_for_user(slot_start):
    """Formatuje pojedynczy slot (datetime) na czytelny tekst dla użytkownika."""
    if not isinstance(slot_start, datetime.datetime):
        logging.warning(f"Błąd formatowania slotu: oczekiwano datetime, otrzymano {type(slot_start)}")
        return "[Błąd daty]"
    try:
        tz = _get_timezone()
        # Upewnij się, że czas jest świadomy i w odpowiedniej strefie
        if slot_start.tzinfo is None: slot_start = tz.localize(slot_start)
        else: slot_start = slot_start.astimezone(tz)

        day_index = slot_start.weekday()
        day_name = POLISH_WEEKDAYS[day_index]
        hour_str = str(slot_start.hour) # Godzina bez wiodącego zera
        # Użyj formatowania z locale, jeśli dostępne
        try:
            formatted_date = slot_start.strftime(f'%d.%m.%Y') # Standardowy format daty
            formatted_time = slot_start.strftime(f'{hour_str}:%M') # Standardowy format czasu
            return f"{day_name}, {formatted_date} o {formatted_time}"
        except Exception as format_err:
             logging.warning(f"Błąd formatowania daty/czasu przez strftime: {format_err}. Używam ISO.")
             # Fallback do bardziej standardowego formatu
             return slot_start.strftime(f'%Y-%m-%d %H:%M %Z')

    except Exception as e:
        logging.error(f"Błąd formatowania slotu {slot_start}: {e}", exc_info=True)
        # Fallback do ISO w razie problemów
        return slot_start.isoformat()


def _send_typing_on(recipient_id):
    """Wysyła wskaźnik 'pisania' do użytkownika."""
    if not PAGE_ACCESS_TOKEN or len(PAGE_ACCESS_TOKEN) < 50 or not ENABLE_TYPING_DELAY:
        return
    logging.debug(f"[{recipient_id}] Wysyłanie 'typing_on'")
    params = {"access_token": PAGE_ACCESS_TOKEN}
    payload = {"recipient": {"id": recipient_id}, "sender_action": "typing_on"}
    try:
        requests.post(FACEBOOK_GRAPH_API_URL, params=params, json=payload, timeout=5)
    except requests.exceptions.RequestException as e:
        logging.warning(f"[{recipient_id}] Błąd wysyłania 'typing_on': {e}")

def _send_single_message(recipient_id, message_text):
    """Wysyła pojedynczy fragment wiadomości przez Facebook Graph API."""
    logging.info(f"--- Wysyłanie fragmentu do {recipient_id} (dł: {len(message_text)}) ---")
    if not PAGE_ACCESS_TOKEN or len(PAGE_ACCESS_TOKEN) < 50:
        logging.error(f"!!! [{recipient_id}] Brak tokena dostępu strony. NIE WYSŁANO wiadomości.")
        return False
    params = {"access_token": PAGE_ACCESS_TOKEN}
    payload = {
        "recipient": {"id": recipient_id},
        "message": {"text": message_text},
        "messaging_type": "RESPONSE" # Standardowa odpowiedź na wiadomość użytkownika
    }
    try:
        r = requests.post(FACEBOOK_GRAPH_API_URL, params=params, json=payload, timeout=30)
        r.raise_for_status() # Rzuci wyjątkiem dla błędów HTTP 4xx/5xx
        response_json = r.json()
        # Sprawdzanie błędu w odpowiedzi JSON (dodatkowe zabezpieczenie)
        if response_json.get('error'):
            fb_error = response_json['error']
            logging.error(f"!!! BŁĄD FB API podczas wysyłania wiadomości: {fb_error} !!!")
            # Można dodać specyficzną obsługę kodów błędów, np. 190 dla tokena
            if fb_error.get('code') == 190:
                 logging.error("!!! Wygląda na to, że FB_PAGE_ACCESS_TOKEN jest nieprawidłowy lub wygasł !!!")
            return False
        logging.debug(f"[{recipient_id}] Fragment wiadomości wysłany pomyślnie.")
        return True
    except requests.exceptions.Timeout:
         logging.error(f"!!! BŁĄD TIMEOUT podczas wysyłania wiadomości do {recipient_id} !!!")
         return False
    except requests.exceptions.HTTPError as http_err:
         logging.error(f"!!! BŁĄD HTTP {http_err.response.status_code} podczas wysyłania wiadomości do {recipient_id}: {http_err} !!!")
         # Logowanie odpowiedzi błędu, jeśli dostępna
         if http_err.response is not None:
            try: logging.error(f"Odpowiedź FB (błąd HTTP): {http_err.response.json()}")
            except json.JSONDecodeError: logging.error(f"Odpowiedź FB (błąd HTTP, nie JSON): {http_err.response.text}")
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

    # Symulacja pisania przed wysłaniem pierwszej części
    if ENABLE_TYPING_DELAY:
        # Szacowany czas pisania, ograniczony
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

            # Szukamy najlepszego miejsca do podziału od końca dozwolonego limitu
            split_index = -1
            # Preferowane separatory w kolejności
            delimiters = ['\n\n', '\n', '. ', '! ', '? ', ' ']
            for delimiter in delimiters:
                # Gdzie szukać separatora (nieco przed limitem)
                search_limit = MESSAGE_CHAR_LIMIT - len(delimiter) + 1
                temp_index = remaining_text.rfind(delimiter, 0, search_limit)
                if temp_index != -1:
                    # Znaleziono preferowany separator w dozwolonym zakresie
                    split_index = temp_index + len(delimiter) # Podział *po* separatorze
                    break

            if split_index == -1:
                # Nie znaleziono dobrego separatora, tniemy "na twardo" na limicie
                split_index = MESSAGE_CHAR_LIMIT

            chunk = remaining_text[:split_index].strip()
            if chunk: # Dodajemy tylko niepuste fragmenty
                chunks.append(chunk)
            remaining_text = remaining_text[split_index:].strip()

        logging.info(f"[{recipient_id}] Podzielono wiadomość na {len(chunks)} fragmentów.")

    num_chunks = len(chunks)
    send_success_count = 0
    for i, chunk in enumerate(chunks):
        logging.debug(f"[{recipient_id}] Wysyłanie fragmentu {i+1}/{num_chunks}...")
        if not _send_single_message(recipient_id, chunk):
            logging.error(f"!!! [{recipient_id}] Błąd wysyłania fragmentu {i+1}. Anulowano wysyłanie reszty.")
            break # Przerywamy wysyłanie kolejnych części po błędzie
        send_success_count += 1

        # Opóźnienie i symulacja pisania między fragmentami (jeśli jest więcej niż 1)
        if num_chunks > 1 and i < num_chunks - 1:
            logging.debug(f"[{recipient_id}] Oczekiwanie {MESSAGE_DELAY_SECONDS}s przed kolejnym fragmentem...")
            if ENABLE_TYPING_DELAY:
                # Krótsza symulacja pisania dla kolejnych części
                next_chunk_len = len(chunks[i+1])
                est_next_typing_duration = min(MAX_TYPING_DELAY_SECONDS * 0.7, max(MIN_TYPING_DELAY_SECONDS * 0.5, next_chunk_len / TYPING_CHARS_PER_SECOND))
                _send_typing_on(recipient_id)
                # Podzielenie opóźnienia na czas pisania i stałe opóźnienie
                time.sleep(min(est_next_typing_duration, MESSAGE_DELAY_SECONDS * 0.6))
                remaining_delay = max(0, MESSAGE_DELAY_SECONDS - est_next_typing_duration)
                if remaining_delay > 0:
                    time.sleep(remaining_delay)
            else:
                # Tylko stałe opóźnienie, jeśli symulacja wyłączona
                time.sleep(MESSAGE_DELAY_SECONDS)

    logging.info(f"--- [{recipient_id}] Zakończono proces wysyłania. Wysłano {send_success_count}/{num_chunks} fragmentów. ---")


def _simulate_typing(recipient_id, duration_seconds):
    """Wysyła 'typing_on' i czeka przez określony czas."""
    if ENABLE_TYPING_DELAY and duration_seconds > 0:
        _send_typing_on(recipient_id)
        # Ograniczamy maksymalny czas oczekiwania
        time.sleep(min(duration_seconds, MAX_TYPING_DELAY_SECONDS * 1.2)) # Pozwólmy na nieco dłuższe oczekiwanie niż max delay


def _call_gemini(user_psid, prompt_history, generation_config, task_name, max_retries=3):
    """Wywołuje API Gemini z obsługą błędów, logowaniem i ponowieniami."""
    if not gemini_model:
        logging.error(f"!!! [{user_psid}] KRYTYCZNY BŁĄD: Model Gemini ({task_name}) jest niedostępny (None)!")
        return None # Zwracamy None, aby wskazać błąd

    # Walidacja promptu
    if not isinstance(prompt_history, list) or not all(isinstance(item, Content) for item in prompt_history):
        logging.error(f"!!! [{user_psid}] Nieprawidłowy format promptu ({task_name}). Oczekiwano listy obiektów Content.")
        return None

    logging.info(f"[{user_psid}] Wywołanie Gemini: {task_name} (Prompt: {len(prompt_history)} wiadomości)")
    # Logowanie ostatniej wiadomości użytkownika dla kontekstu
    last_user_msg = next((msg.parts[0].text for msg in reversed(prompt_history) if msg.role == 'user' and msg.parts), None)
    if last_user_msg:
         logging.debug(f"    Ostatnia wiadomość usera ({task_name}): '{last_user_msg[:200]}{'...' if len(last_user_msg)>200 else ''}'")
    else:
         logging.debug(f"    Brak wiadomości użytkownika w bezpośrednim prompcie ({task_name}).")


    attempt = 0
    while attempt < max_retries:
        attempt += 1
        logging.debug(f"    Próba {attempt}/{max_retries} wywołania Gemini ({task_name})...")
        try:
            # Krótka symulacja pisania przed wywołaniem API
            _simulate_typing(user_psid, MIN_TYPING_DELAY_SECONDS * 0.8)

            # Wywołanie API Gemini
            response = gemini_model.generate_content(
                prompt_history,
                generation_config=generation_config,
                safety_settings=SAFETY_SETTINGS
            )

            # Analiza odpowiedzi
            if response and response.candidates:
                candidate = response.candidates[0]
                finish_reason = candidate.finish_reason

                # Sprawdzenie powodu zakończenia generowania
                if finish_reason != 1: # 1 = STOP (sukces)
                    safety_ratings = candidate.safety_ratings
                    logging.warning(f"[{user_psid}] Gemini ({task_name}) ZAKOŃCZONE NIEPRAWIDŁOWO! Powód: {finish_reason.name} ({finish_reason.value}). Safety Ratings: {safety_ratings}")
                    # Jeśli zablokowane z powodu bezpieczeństwa lub innego błędu, spróbuj ponownie (jeśli są próby)
                    if finish_reason in [3, 4] and attempt < max_retries: # 3=SAFETY, 4=RECITATION
                        logging.warning(f"    Ponawianie ({attempt}/{max_retries}) z powodu blokady...")
                        time.sleep(1.5 * attempt) # Dłuższe oczekiwanie po blokadzie
                        continue
                    else:
                        # Zwracamy komunikat błędu dla użytkownika w przypadku blokady
                        logging.error(f"!!! [{user_psid}] Gemini ({task_name}) nieudane po blokadzie lub innym błędzie.")
                        # Można zwrócić bardziej konkretny komunikat w zależności od finish_reason
                        if finish_reason == 3: return "Przepraszam, nie mogę przetworzyć tej prośby ze względu na zasady bezpieczeństwa."
                        else: return "Wystąpił problem z generowaniem odpowiedzi."

                # Sprawdzenie, czy jest treść
                if candidate.content and candidate.content.parts:
                    generated_text = "".join(part.text for part in candidate.content.parts if hasattr(part, 'text'))
                    generated_text = generated_text.strip() # Usuń białe znaki z początku/końca
                    if generated_text:
                        logging.info(f"[{user_psid}] Gemini ({task_name}) zwróciło odpowiedź (długość: {len(generated_text)}).")
                        logging.debug(f"    Pełna odpowiedź Gemini ({task_name}): '{generated_text}'")
                        return generated_text # Sukces - zwracamy tekst
                    else:
                        logging.warning(f"[{user_psid}] Gemini ({task_name}) zwróciło kandydata z pustą treścią.")
                        # Traktujemy to jako błąd i próbujemy ponownie, jeśli są próby
                else:
                    logging.warning(f"[{user_psid}] Gemini ({task_name}) zwróciło kandydata bez treści (content/parts).")
                    # Traktujemy to jako błąd i próbujemy ponownie

            else:
                # Brak kandydatów w odpowiedzi - poważny problem
                prompt_feedback = response.prompt_feedback if hasattr(response, 'prompt_feedback') else 'Brak informacji zwrotnej'
                logging.error(f"!!! BŁĄD [{user_psid}] Gemini ({task_name}) - Brak kandydatów w odpowiedzi. Feedback: {prompt_feedback}.")

        # Obsługa błędów HTTP i innych wyjątków
        except HttpError as http_err:
             status_code = http_err.resp.status if http_err.resp else 'Nieznany'
             reason = http_err.resp.reason if http_err.resp else 'Nieznany'
             logging.error(f"!!! BŁĄD HTTP ({status_code}) [{user_psid}] Gemini ({task_name}) - Próba {attempt}/{max_retries}: {reason}")
             # Ponowienie dla błędów serwera lub przekroczenia limitu
             if status_code in [429, 500, 503] and attempt < max_retries:
                 sleep_time = (2 ** attempt) + (random.random() * 0.5) # Exponential backoff
                 logging.warning(f"    Oczekiwanie {sleep_time:.2f}s przed ponowieniem...")
                 time.sleep(sleep_time)
                 continue # Kontynuuj pętlę while
             else:
                 break # Zakończ pętlę dla innych błędów HTTP lub po ostatniej próbie
        except Exception as e:
             # Obsługa krytycznego błędu, gdy model nie jest zainicjowany
             if isinstance(e, NameError) and 'gemini_model' in str(e):
                 logging.critical(f"!!! KRYTYCZNY NameError [{user_psid}] w _call_gemini: {e}. gemini_model jest None!", exc_info=True)
                 return None # Zwróć None, aby zasygnalizować krytyczny błąd
             else:
                 # Inne nieoczekiwane błędy
                 logging.error(f"!!! BŁĄD [{user_psid}] Gemini ({task_name}) - Nieoczekiwany błąd Python (Próba {attempt}/{max_retries}): {e}", exc_info=True)
                 # Można spróbować ponowić dla niektórych błędów, ale ostrożnie
                 # break # Bezpieczniej jest przerwać po nieznanym błędzie

        # Jeśli doszło do błędu (innego niż HTTP 429/50x/503) lub braku treści i są jeszcze próby
        if attempt < max_retries:
            logging.warning(f"    Problem z odpowiedzią Gemini ({task_name}). Oczekiwanie przed ponowieniem ({attempt+1}/{max_retries})...")
            time.sleep(1.5 * attempt) # Krótkie oczekiwanie przed kolejną próbą

    # Jeśli pętla zakończyła się bez sukcesu
    logging.error(f"!!! KRYTYCZNY BŁĄD [{user_psid}] Gemini ({task_name}) - Nie udało się uzyskać poprawnej odpowiedzi po {max_retries} próbach.")
    return None # Zwracamy None, aby wskazać ostateczny błąd


# =====================================================================
# === INSTRUKCJE SYSTEMOWE I GŁÓWNE FUNKCJE AI ========================
# =====================================================================

# NOWA Instrukcja dla AI Planującego (bardziej autonomiczna)
SYSTEM_INSTRUCTION_SCHEDULING = """Jesteś pomocnym asystentem AI specjalizującym się w umawianiu terminów korepetycji online. Twoim zadaniem jest znalezienie pasującego terminu dla użytkownika na podstawie jego preferencji oraz dostarczonej listy dostępnych zakresów czasowych.

**Kontekst:**
*   Rozmawiasz z użytkownikiem, który wyraził chęć umówienia się na lekcję.
*   Poniżej znajduje się lista AKTUALNIE dostępnych ZAKRESÓW czasowych, w których można umówić wizytę (każda trwa {duration} minut).
*   Masz dostęp do historii poprzedniej rozmowy.

**Dostępne zakresy czasowe:**
{available_ranges_text}

**Twoje zadanie:**
1. ** Sam jako pierwszy zaproponuj termin, następnie na podstawie historii konwersacji i zakresów terminów ustal pasujący klientowi termin. Najlepiej, gdyby był on najwcześniej jak się da, ale nie krócej niż za 24h.
2. ** Zwróć znacznik TYLKO po ZGODZIE:** Kiedy użytkownik **wyraźnie potwierdzi i zaakceptuje** konkretny, zaproponowany przez Ciebie termin (np. powie "Tak, pasuje", "OK, ten termin jest dobry", "Zapiszmy ten"), zakończ swoją odpowiedź potwierdzającą **DOKŁADNIE** znacznikiem `{slot_marker_prefix}YYYY-MM-DDTHH:MM:SS{slot_marker_suffix}`, gdzie YYYY-MM-DDTHH:MM:SS to **dokładny czas rozpoczęcia** zaakceptowanego terminu w formacie ISO 8601 (np. 2024-07-25T17:00:00).
3. ** NIE dodawaj znacznika**, jeśli:
    *   Użytkownik jeszcze się zastanawia.
    *   Użytkownik zadaje pytania.
    *   Użytkownik proponuje inny termin, którego nie ma na liście.
    *   Nie udało się znaleźć pasującego terminu.
    *   Lista dostępnych zakresów jest pusta.
4. **Brak terminów:** Jeśli lista zakresów jest pusta lub po rozmowie okaże się, że żaden termin nie pasuje, poinformuj o tym użytkownika uprzejmie. Nie dodawaj znacznika.

**Pamiętaj:**
1. Proponuj termin tak żeby do jego rozpoczęcia było przynajmniej 24 godziny, tak abyśmy mogli się przygotować się do zajęć. 
2. Najczęściej nasi klienci to osoby uczące się, więc w większości przypadków w tygodniu pomiędzy 8 i 14 będą niedostępne.
3. Jesteśmy w polsce więc posługuj się jezykiem polskim i używaj polskiego czasu.
4. Raczej nie informuj o wewnętrznych zasadach typu min 24h do terminu itd., no chyba, że prośba użytkownika będzie kolidowała z jakąś zasadą to wtedy wytłumacz, że nei możesz tego zrobić.
5. Bądź cierpliwy i pomocny. Znacznik `{slot_marker_prefix}...{slot_marker_suffix}` jest sygnałem dla systemu, że **osiągnięto finalne porozumienie** co do terminu. Używaj go tylko w tym jednym, konkretnym przypadku.
""".format(
    duration=APPOINTMENT_DURATION_MINUTES,
    available_ranges_text="{available_ranges_text}", # Zostanie wstrzyknięte później
    slot_marker_prefix=SLOT_ISO_MARKER_PREFIX,
    slot_marker_suffix=SLOT_ISO_MARKER_SUFFIX
)


# Instrukcja ogólna (bez zmian)
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


# --- Funkcja AI: Planowanie terminu (NOWA) ---
def get_gemini_scheduling_response(user_psid, history_for_scheduling_ai, current_user_message_text, available_ranges):
    """Prowadzi rozmowę planującą z AI, zwraca odpowiedź AI (może zawierać znacznik ISO)."""
    if not gemini_model:
        logging.error(f"!!! [{user_psid}] Model Gemini niezaładowany (Scheduling)!")
        return "Przepraszam, mam problem z systemem planowania."

    ranges_text = format_ranges_for_ai(available_ranges)
    system_instruction = SYSTEM_INSTRUCTION_SCHEDULING.format(available_ranges_text=ranges_text)

    # Budowanie promptu: instrukcja systemowa + historia + nowa wiadomość użytkownika
    initial_prompt = [
        Content(role="user", parts=[Part.from_text(system_instruction)]),
        Content(role="model", parts=[Part.from_text("Rozumiem. Będę rozmawiać z użytkownikiem, aby znaleźć pasujący termin. Znacznik [SLOT_ISO:...] dodam tylko po uzyskaniu ostatecznej zgody.")]) # Potwierdzenie zrozumienia instrukcji przez model
    ]
    full_prompt = initial_prompt + history_for_scheduling_ai
    if current_user_message_text: # Dodaj bieżącą wiadomość użytkownika, jeśli istnieje
        full_prompt.append(Content(role="user", parts=[Part.from_text(current_user_message_text)]))

    # Ograniczenie długości promptu (usuwanie najstarszych par user/model z historii)
    max_prompt_messages = (MAX_HISTORY_TURNS * 2) + 2 # +2 dla instrukcji systemowej i odpowiedzi modelu
    # Usuwamy od indeksu 2, aby zachować instrukcję systemową i pierwszą odpowiedź modelu
    while len(full_prompt) > max_prompt_messages:
        full_prompt.pop(2) # Usuń najstarszą wiadomość użytkownika (jeśli istnieje)
        if len(full_prompt) > 2:
            full_prompt.pop(2) # Usuń odpowiadającą jej wiadomość modelu (jeśli istnieje)

    # Wywołanie Gemini z konfiguracją dla planowania
    response_text = _call_gemini(user_psid, full_prompt, GENERATION_CONFIG_SCHEDULING, "Scheduling Conversation")

    if response_text:
        # Sprawdzamy, czy AI nie dodało przypadkiem znacznika INTENT_SCHEDULE_MARKER
        if INTENT_SCHEDULE_MARKER in response_text:
             logging.warning(f"[{user_psid}] AI (Scheduling) błędnie dodało znacznik {INTENT_SCHEDULE_MARKER}. Usuwam.")
             response_text = response_text.replace(INTENT_SCHEDULE_MARKER, "").strip()
        return response_text # Zwracamy pełną odpowiedź, może zawierać SLOT_ISO
    else:
        logging.error(f"!!! [{user_psid}] Nie uzyskano odpowiedzi Gemini (Scheduling).")
        # Zwracamy generyczny komunikat błędu
        return "Przepraszam, wystąpił błąd podczas sprawdzania terminów. Spróbujmy ponownie za chwilę."


# --- Funkcja AI: Ogólna rozmowa (bez zmian) ---
def get_gemini_general_response(user_psid, current_user_message_text, history_for_general_ai):
    """Prowadzi ogólną rozmowę z AI."""
    if not gemini_model:
        logging.error(f"!!! [{user_psid}] Model Gemini niezaładowany (General)!")
        return "Przepraszam, mam chwilowy problem z systemem."

    initial_prompt = [
        Content(role="user", parts=[Part.from_text(SYSTEM_INSTRUCTION_GENERAL)]),
        Content(role="model", parts=[Part.from_text("Rozumiem. Będę pomocnym asystentem klienta i dodam znacznik [INTENT_SCHEDULE], gdy użytkownik wyrazi chęć umówienia się.")]) # Potwierdzenie modelu
    ]
    full_prompt = initial_prompt + history_for_general_ai
    if current_user_message_text: # Dodaj bieżącą wiadomość tylko jeśli nie jest pusta
        full_prompt.append(Content(role="user", parts=[Part.from_text(current_user_message_text)]))

    # Ograniczenie długości promptu
    max_prompt_messages = (MAX_HISTORY_TURNS * 2) + 2
    while len(full_prompt) > max_prompt_messages:
        full_prompt.pop(2) # Usuń user
        if len(full_prompt) > 2:
            full_prompt.pop(2) # Usuń model

    # Wywołanie Gemini z domyślną konfiguracją
    response_text = _call_gemini(user_psid, full_prompt, GENERATION_CONFIG_DEFAULT, "General Conversation")

    if response_text:
        # Sprawdzamy, czy AI (General) nie dodało przypadkiem znacznika SLOT_ISO
        if SLOT_ISO_MARKER_PREFIX in response_text:
             logging.warning(f"[{user_psid}] AI (General) błędnie dodało znacznik ISO. Usuwam.")
             response_text = re.sub(rf"{re.escape(SLOT_ISO_MARKER_PREFIX)}.*?{re.escape(SLOT_ISO_MARKER_SUFFIX)}", "", response_text).strip()
        return response_text # Zwracamy odpowiedź (może zawierać INTENT_SCHEDULE_MARKER)
    else:
        logging.error(f"!!! [{user_psid}] Nie uzyskano odpowiedzi Gemini (General).")
        return "Przepraszam, wystąpił błąd przetwarzania Twojej wiadomości."

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
    logging.info(f"\n{'='*30} {datetime.datetime.now(_get_timezone()):%Y-%m-%d %H:%M:%S %Z} POST /webhook {'='*30}")
    raw_data = request.data
    data = None
    try:
        decoded_data = raw_data.decode('utf-8')
        # Logowanie surowych danych (ostrożnie w produkcji - może zawierać dane wrażliwe)
        # logging.debug(f"Surowe dane POST: {decoded_data[:1000]}{'...' if len(decoded_data)>1000 else ''}")
        data = json.loads(decoded_data)

        if data and data.get("object") == "page":
            for entry in data.get("entry", []):
                for event in entry.get("messaging", []):
                    sender_id = event.get("sender", {}).get("id")
                    if not sender_id:
                        logging.warning("Pominięto zdarzenie bez identyfikatora nadawcy (sender.id).")
                        continue

                    logging.info(f"--- Przetwarzanie zdarzenia dla PSID: {sender_id} ---")

                    # Wczytanie historii i stanu
                    history, context = load_history(sender_id)
                    # Filtrujemy historię tylko do wiadomości user/model dla AI
                    history_for_gemini = [h for h in history if isinstance(h, Content) and h.role in ('user', 'model')]
                    current_state = context.get('type', STATE_GENERAL)
                    logging.info(f"    Aktualny stan konwersacji: {current_state}")

                    # Zmienne do zarządzania przepływem
                    action = None # Co zrobić po przetworzeniu wejścia
                    msg_result = None # Wiadomość do wysłania użytkownikowi
                    next_state = current_state # Stan po zakończeniu cyklu
                    model_resp_content = None # Odpowiedź AI do zapisania w historii
                    user_content = None # Wiadomość użytkownika do zapisania w historii
                    extracted_iso_slot = None # Wyekstrahowany slot ISO, jeśli AI go zwróci
                    slot_verification_failed = False # Flaga oznaczająca nieudaną weryfikację slotu

                    # === Obsługa wiadomości tekstowych ===
                    if message_data := event.get("message"):
                        if message_data.get("is_echo"):
                            logging.debug(f"    Pominięto echo wiadomości bota.")
                            continue # Ignoruj echa

                        user_input_text = message_data.get("text", "").strip()
                        if user_input_text:
                            user_content = Content(role="user", parts=[Part.from_text(user_input_text)])
                            logging.info(f"    Otrzymano wiadomość tekstową (stan={current_state}): '{user_input_text[:100]}{'...' if len(user_input_text)>100 else ''}'")
                            if ENABLE_TYPING_DELAY: time.sleep(MIN_TYPING_DELAY_SECONDS * 0.5) # Krótka pauza po otrzymaniu

                            if current_state == STATE_SCHEDULING_ACTIVE:
                                logging.info("      -> Stan: Aktywne Planowanie. Wywołanie AI Planującego...")
                                action = 'handle_scheduling' # Przekaż do logiki planowania
                            else: # Stan GENERAL
                                logging.info("      -> Stan: Ogólny. Wywołanie AI Ogólnego...")
                                action = 'handle_general' # Przekaż do logiki ogólnej

                        elif attachments := message_data.get("attachments"):
                             att_type = attachments[0].get('type','nieznany')
                             logging.info(f"      Otrzymano załącznik typu: {att_type}.")
                             # Traktujemy załącznik jako wiadomość tekstową dla historii
                             user_content = Content(role="user", parts=[Part.from_text(f"[Użytkownik wysłał załącznik typu: {att_type}]")])
                             # Odpowiadamy generycznie
                             msg_result = "Dziękuję, ale obecnie mogę przetwarzać tylko wiadomości tekstowe." if att_type not in ['sticker', 'image', 'audio', 'video', 'file'] else "Dzięki!"
                             action = 'send_info' # Tylko wyślij informację
                             next_state = current_state # Stan się nie zmienia
                        else: # Pusta wiadomość lub nieobsługiwany typ
                            logging.info("      Otrzymano pustą wiadomość lub nieobsługiwany typ.")
                            action = None # Nic nie rób

                    # === Obsługa Postback (np. z przycisków) ===
                    elif postback := event.get("postback"):
                        payload = postback.get("payload")
                        title = postback.get("title", "") # Tytuł przycisku
                        logging.info(f"    Otrzymano postback: Payload='{payload}', Tytuł='{title}', Stan={current_state}")
                        # Traktujemy postback jak wiadomość tekstową dla AI
                        user_input_text = f"Użytkownik kliknął przycisk: '{title}' (Payload: {payload})"
                        user_content = Content(role="user", parts=[Part.from_text(user_input_text)])

                        # Można dodać specyficzną logikę dla payload, np. szybkie anulowanie
                        if payload == "CANCEL_SCHEDULING":
                             logging.info("      Postback anulowania planowania.")
                             msg_result = "Rozumiem, anulowano proces umawiania terminu. W czymś jeszcze mogę pomóc?"
                             action = 'send_info'
                             next_state = STATE_GENERAL
                        # Domyślnie przekazujemy informację o kliknięciu do odpowiedniego AI
                        elif current_state == STATE_SCHEDULING_ACTIVE:
                            logging.info("      -> Stan: Aktywne Planowanie. Przekazanie postback do AI Planującego...")
                            action = 'handle_scheduling'
                        else: # Stan GENERAL
                            logging.info("      -> Stan: Ogólny. Przekazanie postback do AI Ogólnego...")
                            action = 'handle_general'

                    # === Inne zdarzenia (np. odczytanie, dostarczenie) ===
                    elif event.get("read"):
                        logging.debug(f"    Otrzymano potwierdzenie odczytania.")
                        continue # Ignoruj
                    elif event.get("delivery"):
                        logging.debug(f"    Otrzymano potwierdzenie dostarczenia.")
                        continue # Ignoruj
                    else:
                        logging.warning(f"    Otrzymano nieobsługiwany typ zdarzenia: {json.dumps(event)}")
                        continue # Ignoruj nieznane zdarzenia


                    # --- WYKONANIE ZAPLANOWANEJ AKCJI ---
                    if action == 'handle_general':
                        logging.debug("  >> Akcja: handle_general")
                        if user_content and user_content.parts:
                            response = get_gemini_general_response(sender_id, user_content.parts[0].text, history_for_gemini)
                            if response:
                                if INTENT_SCHEDULE_MARKER in response:
                                    logging.info(f"      AI Ogólne wykryło intencję [{INTENT_SCHEDULE_MARKER}]. Przejście do planowania.")
                                    # Usuń znacznik z wiadomości dla użytkownika
                                    initial_resp_text = response.split(INTENT_SCHEDULE_MARKER, 1)[0].strip()
                                    # Wyślij odpowiedź inicjującą i od razu przejdź do logiki planowania
                                    if initial_resp_text:
                                        send_message(sender_id, initial_resp_text)
                                        model_resp_content = Content(role="model", parts=[Part.from_text(initial_resp_text)])
                                        # Dodaj odpowiedź do historii *przed* wywołaniem AI planującego
                                        history_for_gemini.append(user_content)
                                        history_for_gemini.append(model_resp_content)
                                    else: # Jeśli AI nie dało tekstu przed znacznikiem
                                        history_for_gemini.append(user_content) # Dodaj tylko wiadomość usera

                                    next_state = STATE_SCHEDULING_ACTIVE
                                    action = 'handle_scheduling' # Ustaw akcję na planowanie w tym samym cyklu
                                    user_content = None # Wiadomość usera już przetworzona przez AI General
                                    model_resp_content = None # Odpowiedź modelu już wysłana/dodana
                                    logging.debug("      Przekierowanie do handle_scheduling...")
                                    # Kontynuuj do następnego bloku 'if action == ...'
                                else:
                                    # Zwykła odpowiedź AI Ogólnego
                                    msg_result = response
                                    model_resp_content = Content(role="model", parts=[Part.from_text(msg_result)])
                                    next_state = STATE_GENERAL
                                    # action = None (zakończ cykl po wysłaniu)
                            else:
                                # Błąd AI Ogólnego
                                msg_result = "Przepraszam, mam problem z przetworzeniem Twojej wiadomości."
                                model_resp_content = Content(role="model", parts=[Part.from_text(msg_result)])
                                next_state = STATE_GENERAL
                        else:
                             logging.warning("handle_general wywołane bez user_content.")
                             action = None # Nic nie rób

                    if action == 'handle_scheduling':
                        logging.debug("  >> Akcja: handle_scheduling")
                        try:
                            # Zawsze pobieraj świeże zakresy przed wywołaniem AI planującego
                            tz = _get_timezone()
                            now = datetime.datetime.now(tz)
                            search_start = now
                            search_end_date = (search_start + datetime.timedelta(days=MAX_SEARCH_DAYS)).date()
                            # Koniec dnia na koniec okresu wyszukiwania
                            search_end = tz.localize(datetime.datetime.combine(search_end_date, datetime.time(WORK_END_HOUR, 0)))

                            logging.info(f"      Pobieranie wolnych zakresów od {search_start:%Y-%m-%d %H:%M} do {search_end:%Y-%m-%d %H:%M}")
                            _simulate_typing(sender_id, MAX_TYPING_DELAY_SECONDS * 0.6) # Symulacja myślenia
                            free_ranges = get_free_time_ranges(TARGET_CALENDAR_ID, search_start, search_end)

                            if free_ranges:
                                logging.info(f"      Znaleziono {len(free_ranges)} zakresów. Wywołanie AI Planującego...")
                                # Przekaż aktualną wiadomość użytkownika (jeśli jest)
                                current_input_text = user_content.parts[0].text if user_content and user_content.parts else None
                                ai_response_text = get_gemini_scheduling_response(sender_id, history_for_gemini, current_input_text, free_ranges)

                                if ai_response_text:
                                    # Sprawdź, czy AI zwróciło znacznik finalnej zgody
                                    iso_match = re.search(rf"{re.escape(SLOT_ISO_MARKER_PREFIX)}(.*?){re.escape(SLOT_ISO_MARKER_SUFFIX)}", ai_response_text)
                                    if iso_match:
                                        extracted_iso = iso_match.group(1).strip()
                                        logging.info(f"      AI Planujące zwróciło potencjalny finalny slot: {extracted_iso}")
                                        # Usuń znacznik z wiadomości dla użytkownika
                                        text_for_user = re.sub(rf"{re.escape(SLOT_ISO_MARKER_PREFIX)}.*?{re.escape(SLOT_ISO_MARKER_SUFFIX)}", "", ai_response_text).strip()
                                        text_for_user = re.sub(r'\s+', ' ', text_for_user).strip() # Normalizuj białe znaki

                                        # --- WERYFIKACJA SLOTU ---
                                        try:
                                            proposed_start = datetime.datetime.fromisoformat(extracted_iso)
                                            # Upewnij się, że jest świadomy i w dobrej strefie
                                            if proposed_start.tzinfo is None: proposed_start = tz.localize(proposed_start)
                                            else: proposed_start = proposed_start.astimezone(tz)

                                            logging.info(f"      Weryfikacja dostępności slotu: {format_slot_for_user(proposed_start)}")
                                            if is_slot_actually_free(proposed_start, TARGET_CALENDAR_ID):
                                                logging.info("      Weryfikacja OK! Slot jest wolny. Przystępowanie do rezerwacji.")
                                                # 1. Wyślij wiadomość potwierdzającą *przed* rezerwacją
                                                confirm_msg = text_for_user if text_for_user else f"Dobrze, potwierdzam termin {format_slot_for_user(proposed_start)}. Zapisuję..."
                                                send_message(sender_id, confirm_msg)
                                                model_resp_content = Content(role="model", parts=[Part.from_text(confirm_msg)]) # Zapisz tę wiadomość
                                                # Dodaj do historii *przed* rezerwacją
                                                if user_content: history_for_gemini.append(user_content)
                                                history_for_gemini.append(model_resp_content)
                                                save_history(sender_id, history_for_gemini, context_to_save={'type': STATE_GENERAL}) # Zapisz stan jako generalny

                                                # 2. Wykonaj rezerwację
                                                action = 'book' # Ustaw akcję na rezerwację
                                                extracted_iso_slot = proposed_start # Przekaż sparsowaną datę do akcji 'book'
                                                next_state = STATE_GENERAL # Po rezerwacji wracamy do stanu ogólnego
                                                # Przerwij dalsze przetwarzanie w tym bloku, przejdź do akcji 'book'
                                                logging.debug("      Przekierowanie do akcji 'book'...")
                                                # Resetuj user_content i model_resp_content, bo zostały już zapisane
                                                user_content = None
                                                model_resp_content = None
                                                # Kontynuuj do bloku 'if action == 'book'...'
                                            else:
                                                # Slot ZAJĘTY podczas weryfikacji
                                                logging.warning(f"      Weryfikacja NIEUDANA! Slot {extracted_iso} został zajęty.")
                                                slot_verification_failed = True
                                                # Poinformuj użytkownika i AI w następnej turze
                                                fail_msg = f"Ojej, wygląda na to, że termin {format_slot_for_user(proposed_start)} został właśnie zajęty! Przepraszam za zamieszanie. Spróbujmy znaleźć inny."
                                                msg_result = fail_msg
                                                model_resp_content = Content(role="model", parts=[Part.from_text(fail_msg)])
                                                next_state = STATE_SCHEDULING_ACTIVE # Pozostań w trybie planowania
                                                # action = None (zakończ cykl po wysłaniu wiadomości o błędzie)
                                        except ValueError:
                                            logging.error(f"!!! BŁĄD: AI zwróciło nieprawidłowy format ISO w znaczniku: '{extracted_iso}'")
                                            msg_result = "Przepraszam, wystąpił błąd techniczny przy przetwarzaniu zaproponowanego terminu. Spróbujmy jeszcze raz."
                                            model_resp_content = Content(role="model", parts=[Part.from_text(msg_result)])
                                            next_state = STATE_SCHEDULING_ACTIVE # Pozostań w trybie planowania
                                        except Exception as verif_err:
                                             logging.error(f"!!! BŁĄD podczas weryfikacji slotu {extracted_iso}: {verif_err}", exc_info=True)
                                             msg_result = "Przepraszam, wystąpił nieoczekiwany błąd podczas sprawdzania dostępności terminu."
                                             model_resp_content = Content(role="model", parts=[Part.from_text(msg_result)])
                                             next_state = STATE_SCHEDULING_ACTIVE
                                    else:
                                        # AI odpowiedziało, ale bez znacznika - kontynuacja rozmowy
                                        logging.info("      AI Planujące kontynuuje rozmowę (brak znacznika ISO).")
                                        msg_result = ai_response_text
                                        model_resp_content = Content(role="model", parts=[Part.from_text(msg_result)])
                                        next_state = STATE_SCHEDULING_ACTIVE # Pozostań w trybie planowania
                                        # action = None (zakończ cykl po wysłaniu)
                                else:
                                    # Błąd AI Planującego
                                    logging.error("!!! BŁĄD: AI Planujące nie zwróciło odpowiedzi.")
                                    msg_result = "Przepraszam, mam problem z systemem planowania. Spróbuj ponownie za chwilę."
                                    model_resp_content = Content(role="model", parts=[Part.from_text(msg_result)])
                                    next_state = STATE_GENERAL # Wróć do stanu ogólnego w razie błędu AI
                            else:
                                # Brak wolnych zakresów
                                logging.warning(f"      Brak wolnych zakresów w ciągu najbliższych {MAX_SEARCH_DAYS} dni.")
                                no_slots_msg = f"Niestety, wygląda na to, że nie mam żadnych wolnych terminów w ciągu najbliższych {MAX_SEARCH_DAYS} dni. Spróbuj ponownie później lub skontaktuj się z nami w inny sposób."
                                msg_result = no_slots_msg
                                model_resp_content = Content(role="model", parts=[Part.from_text(msg_result)])
                                next_state = STATE_GENERAL # Koniec planowania, brak terminów
                        except Exception as schedule_err:
                            logging.error(f"!!! KRYTYCZNY BŁĄD w bloku 'handle_scheduling': {schedule_err}", exc_info=True)
                            msg_result = "Wystąpił nieoczekiwany błąd systemu podczas planowania. Przepraszam za problem."
                            model_resp_content = Content(role="model", parts=[Part.from_text(msg_result)])
                            next_state = STATE_GENERAL # Wróć do stanu ogólnego

                    if action == 'book':
                        logging.debug("  >> Akcja: book")
                        if extracted_iso_slot and isinstance(extracted_iso_slot, datetime.datetime):
                            try:
                                start_dt_obj = extracted_iso_slot # Mamy już sparsowaną datę
                                end_dt_obj = start_dt_obj + datetime.timedelta(minutes=APPOINTMENT_DURATION_MINUTES)

                                # Pobierz dane użytkownika do opisu wydarzenia
                                prof = get_user_profile(sender_id)
                                user_name = prof.get('first_name', '') if prof else f"User_{sender_id[-4:]}"
                                user_last_name = prof.get('last_name', '') if prof else ''
                                event_desc = f"Rezerwacja przez Bota Facebook Messenger\nPSID: {sender_id}"
                                if user_last_name: event_desc += f"\nNazwisko: {user_last_name}"

                                # Wywołaj funkcję rezerwacji
                                ok, booking_msg = book_appointment(
                                    TARGET_CALENDAR_ID,
                                    start_dt_obj,
                                    end_dt_obj,
                                    summary=f"Korepetycje: {user_name}", # Tytuł wydarzenia
                                    description=event_desc,
                                    user_name=user_name # Przekazanie imienia do funkcji
                                )

                                # Wyślij wynik rezerwacji do użytkownika
                                msg_result = booking_msg # Wiadomość zwrotna z book_appointment
                                model_resp_content = Content(role="model", parts=[Part.from_text(msg_result)])
                                next_state = STATE_GENERAL # Stan zawsze wraca do general po próbie rezerwacji
                                if not ok:
                                    logging.warning(f"      Rezerwacja terminu {start_dt_obj} nie powiodła się (zwrócono False).")
                                    # Wiadomość o błędzie jest już w booking_msg
                            except Exception as e:
                                logging.error(f"!!! BŁĄD podczas wykonywania akcji 'book': {e}", exc_info=True)
                                msg_result = "Wystąpił krytyczny błąd podczas finalizowania rezerwacji. Skontaktuj się z nami bezpośrednio."
                                model_resp_content = Content(role="model", parts=[Part.from_text(msg_result)])
                                next_state = STATE_GENERAL
                        else:
                            logging.error("!!! BŁĄD LOGIKI: Akcja 'book' wywołana bez prawidłowego 'extracted_iso_slot'!")
                            msg_result = "Wystąpił wewnętrzny błąd systemu rezerwacji (brak daty)."
                            model_resp_content = Content(role="model", parts=[Part.from_text(msg_result)])
                            next_state = STATE_GENERAL

                    if action == 'send_info':
                         logging.debug("  >> Akcja: send_info")
                         # msg_result i next_state powinny być już ustawione
                         if msg_result:
                              model_resp_content = Content(role="model", parts=[Part.from_text(msg_result)])
                         else:
                              logging.warning("Akcja 'send_info' bez wiadomości do wysłania.")


                    # --- WYSYŁANIE ODPOWIEDZI I ZAPIS STANU (po zakończeniu akcji) ---
                    final_context_to_save = {'type': next_state} # Zawsze zapisujemy nowy stan

                    # Wysyłanie wiadomości, jeśli JEST jakaś (msg_result mogło być ustawione w blokach akcji)
                    if msg_result:
                        send_message(sender_id, msg_result)
                        # model_resp_content powinien być już ustawiony, jeśli wysyłamy wiadomość
                        if not model_resp_content:
                             logging.warning(f"Wiadomość '{msg_result[:50]}...' została wysłana, ale nie ustawiono model_resp_content do zapisu!")
                             model_resp_content = Content(role="model", parts=[Part.from_text(msg_result)])
                    elif action and action not in ['book']: # Jeśli była akcja, ale nie było wiadomości (i nie była to rezerwacja, która ma własną logikę zapisu)
                        logging.warning(f"    Akcja '{action}' zakończona bez wiadomości do wysłania użytkownikowi.")

                    # Zapis historii i stanu, jeśli coś się zmieniło
                    # Sprawdzamy, czy dodano wiadomość użytkownika, wiadomość modelu, lub zmienił się stan
                    should_save = bool(user_content) or bool(model_resp_content) or (current_state != next_state)

                    if should_save:
                        history_to_save = list(history) # Kopia oryginalnej historii
                        if user_content: history_to_save.append(user_content)
                        if model_resp_content: history_to_save.append(model_resp_content)

                        # Ograniczenie historii przed zapisem
                        max_hist_len = MAX_HISTORY_TURNS * 2
                        if len(history_to_save) > max_hist_len:
                             history_to_save = history_to_save[-max_hist_len:]

                        logging.info(f"Zapisywanie historii ({len(history_to_save)} wiad.). Nowy stan: {final_context_to_save}")
                        save_history(sender_id, history_to_save, context_to_save=final_context_to_save)
                    else:
                        logging.debug("    Brak zmian w historii lub stanie - pomijanie zapisu.")

            logging.info(f"--- Zakończono przetwarzanie batcha zdarzeń ---")
            return Response("EVENT_RECEIVED", status=200)
        else:
            # Otrzymano dane, ale nie są to zdarzenia strony Facebooka
            logging.warning(f"Otrzymano POST, ale obiekt nie jest 'page' (typ: {data.get('object') if data else 'Brak danych'}). Ignorowanie.")
            return Response("OK", status=200) # Odpowiadamy OK, żeby FB nie próbował ponownie
    except json.JSONDecodeError as e:
        logging.error(f"!!! BŁĄD podczas dekodowania JSON z danych POST: {e}", exc_info=True)
        logging.error(f"    Pierwsze 500 znaków surowych danych: {raw_data[:500]}...")
        return Response("Invalid JSON payload", status=400) # Zły request od FB
    except Exception as e:
        # Złapanie wszelkich innych nieoczekiwanych błędów
        logging.critical(f"!!! KRYTYCZNY NIEOCZEKIWANY BŁĄD w głównym handlerze POST /webhook: {e}", exc_info=True)
        # Odpowiadamy 200 OK, aby uniknąć pętli ponowień od Facebooka, ale logujemy błąd krytyczny
        return Response("Internal Server Error during processing", status=200)


# =====================================================================
# === URUCHOMIENIE SERWERA ============================================
# =====================================================================
if __name__ == '__main__':
    ensure_dir(HISTORY_DIR)
    log_level = logging.DEBUG # Ustaw logowanie na DEBUG dla rozwoju
    # Konfiguracja logowania, jeśli jeszcze nie ustawiona
    if not logging.getLogger().hasHandlers():
        logging.basicConfig(
            level=log_level,
            format='%(asctime)s - %(levelname)s - [%(filename)s:%(lineno)d - %(funcName)s] - %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )

    # Wyciszenie zbyt gadatliwych loggerów zewnętrznych bibliotek
    logging.getLogger('googleapiclient.discovery_cache').setLevel(logging.ERROR)
    logging.getLogger('urllib3.connectionpool').setLevel(logging.WARNING)
    logging.getLogger('werkzeug').setLevel(logging.WARNING) # Logi żądań Flask

    print("\n" + "="*60 + "\n--- START KONFIGURACJI BOTA (Tryb Autonomicznego Planowania) ---")
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
    print("  Konfiguracja Kalendarza Google:")
    print(f"    ID Kalendarza: {TARGET_CALENDAR_ID}")
    print(f"    Strefa czasowa: {CALENDAR_TIMEZONE} (Obiekt TZ: {_get_timezone()})")
    print(f"    Czas trwania wizyty: {APPOINTMENT_DURATION_MINUTES} min")
    print(f"    Godziny pracy: {WORK_START_HOUR}:00 - {WORK_END_HOUR}:00")
    print(f"    Maks. zakres szukania: {MAX_SEARCH_DAYS} dni")
    print(f"    Plik klucza API: {SERVICE_ACCOUNT_FILE} ({'Znaleziono' if os.path.exists(SERVICE_ACCOUNT_FILE) else 'BRAK!!! Funkcjonalność kalendarza niedostępna.'})")
    cal_service = get_calendar_service()
    if not cal_service and os.path.exists(SERVICE_ACCOUNT_FILE): print("!!! OSTRZ.: Usługa Google Calendar NIE zainicjowana mimo obecności pliku klucza.")
    elif not os.path.exists(SERVICE_ACCOUNT_FILE): print("!!! OSTRZ.: Brak pliku klucza Google Calendar.")
    elif cal_service: print("    Usługa Google Calendar: Zainicjowana (OK)")
    print("--- KONIEC KONFIGURACJI BOTA ---"); print("="*60 + "\n")

    # Uruchomienie serwera
    port = int(os.environ.get("PORT", 8080))
    # Uruchom w trybie debug Flask tylko jeśli logowanie jest na DEBUG
    run_flask_in_debug = (log_level == logging.DEBUG)

    print(f"Uruchamianie serwera Flask na porcie {port}...")
    if not run_flask_in_debug:
        # Tryb produkcyjny z Waitress
        try:
            from waitress import serve
            print(">>> Serwer produkcyjny Waitress START <<<")
            serve(app, host='0.0.0.0', port=port, threads=8) # Użyj Waitress dla produkcji
        except ImportError:
            print("!!! Ostrzeżenie: 'waitress' nie znaleziono. Uruchamianie wbudowanego serwera deweloperskiego Flask (niezalecane w produkcji).")
            print(">>> Serwer deweloperski Flask START <<<")
            app.run(host='0.0.0.0', port=port, debug=False) # Uruchom bez debuggera Flask
    else:
        # Tryb deweloperski z włączonym debuggerem Flask (przydatne do śledzenia błędów)
        print(">>> Serwer deweloperski Flask (Tryb DEBUG dla logowania i debuggera) START <<<")
        # Użyj use_reloader=False, aby uniknąć podwójnej inicjalizacji AI przy debugowaniu
        app.run(host='0.0.0.0', port=port, debug=True, use_reloader=False)
