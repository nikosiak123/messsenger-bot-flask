# -*- coding: utf-8 -*-

# verify_server.py (Poprawiona inicjalizacja gemini_model, bez zbędnych średników)

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

# --- Znaczniki dla komunikacji z AI ---
INTENT_SCHEDULE_MARKER = "[INTENT_SCHEDULE]"
SLOT_ISO_MARKER_PREFIX = "[SLOT_ISO:"
SLOT_ISO_MARKER_SUFFIX = "]"

# --- Ustawienia Modelu Gemini ---
GENERATION_CONFIG_PROPOSAL = GenerationConfig(
    temperature=0.05, # Nadal niska dla determinizmu propozycji
    top_p=0.95, top_k=40, max_output_tokens=512,
)
GENERATION_CONFIG_FEEDBACK_SIMPLE = GenerationConfig(
    temperature=0.0, # Maksymalnie deterministyczny
    top_p=0.95, top_k=40, max_output_tokens=32, # Wystarczy na krótki znacznik
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
gemini_model = None # Zainicjuj jako None na początku
try:
    # Logowanie może nie być jeszcze skonfigurowane, używamy print
    print(f"--- Inicjalizowanie Vertex AI: Projekt={PROJECT_ID}, Lokalizacja={LOCATION}")
    vertexai.init(project=PROJECT_ID, location=LOCATION)
    print("--- Inicjalizacja Vertex AI OK.")
    print(f"--- Ładowanie modelu: {MODEL_ID}")
    gemini_model = GenerativeModel(MODEL_ID) # Przypisanie obiektu modelu
    print(f"--- Model {MODEL_ID} załadowany OK.")
except Exception as e:
    print(f"!!! KRYTYCZNY BŁĄD inicjalizacji Vertex AI lub ładowania modelu: {e}", flush=True)
    import traceback
    traceback.print_exc()
    print("!!! Funkcjonalność AI będzie niedostępna !!!", flush=True)
    # W tym miejscu gemini_model pozostanie None

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
        logging.warning(f"[{psid}] Brak/nieprawidłowy PAGE_ACCESS_TOKEN. Profil niepobrany.")
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
    """Wczytuje historię konwersacji i ostatni kontekst systemowy z pliku JSON."""
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
                                logging.warning(f"Ostrz. [{user_psid}]: Niepoprawna część wiadomości w historii (idx {i})")
                                valid_parts = False
                                break
                        if valid_parts and text_parts:
                            history.append(Content(role=msg_data['role'], parts=text_parts))
                    elif (isinstance(msg_data, dict) and msg_data.get('role') == 'system' and
                          msg_data.get('type') == 'last_proposal' and 'slot_iso' in msg_data):
                        if i == last_system_message_index:
                            context['type'] = 'last_proposal'
                            context['slot_iso'] = msg_data['slot_iso']
                            logging.debug(f"[{user_psid}] Odczytano AKTYWNY kontekst: last_proposed_slot_iso (idx {i})")
                        else:
                            logging.debug(f"[{user_psid}] Pominięto stary kontekst systemowy na indeksie {i}")
                    else:
                        logging.warning(f"Ostrz. [{user_psid}]: Pominięto niepoprawną wiadomość w historii (idx {i}): {msg_data}")

                logging.info(f"[{user_psid}] Wczytano historię: {len(history)} wiadomości użytkownika/modelu.")
                return history, context
            else:
                logging.error(f"BŁĄD [{user_psid}]: Plik historii nie zawiera listy.")
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
             logging.error(f"    Nie udało się zmienić nazwy uszkodzonego pliku historii: {rename_err}")
        return [], {}
    except Exception as e:
        logging.error(f"BŁĄD [{user_psid}] wczytywania historii: {e}", exc_info=True)
        return [], {}

def save_history(user_psid, history, context_to_save=None):
    """Zapisuje historię konwersacji i opcjonalny kontekst systemowy do pliku JSON."""
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
                logging.warning(f"Ostrz. [{user_psid}]: Pomijanie nieprawidłowego obiektu historii podczas zapisu: {msg}")

        if context_to_save and isinstance(context_to_save, dict):
             history_data.append(context_to_save)
             logging.debug(f"[{user_psid}] Dodano kontekst do zapisu: {context_to_save.get('type')}")

        with open(temp_filepath, 'w', encoding='utf-8') as f:
            json.dump(history_data, f, ensure_ascii=False, indent=2)
        os.replace(temp_filepath, filepath)
        logging.info(f"[{user_psid}] Zapisano historię/kontekst ({len(history_data)} wpisów) do: {filepath}")

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
            logging.error(f"BŁĄD: Strefa czasowa '{CALENDAR_TIMEZONE}' nieznana. Używam UTC.")
            _tz = pytz.utc
    return _tz

def get_calendar_service():
    """Inicjalizuje (i cachuje) usługę Google Calendar API."""
    global _calendar_service
    if _calendar_service:
        return _calendar_service
    if not os.path.exists(SERVICE_ACCOUNT_FILE):
        logging.error(f"KRYTYCZNY BŁĄD: Brak pliku klucza usługi Google Calendar: '{SERVICE_ACCOUNT_FILE}'")
        return None
    try:
        creds = service_account.Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=CALENDAR_SCOPES)
        service = build('calendar', 'v3', credentials=creds, cache_discovery=False)
        logging.info("Utworzono połączenie z Google Calendar API.")
        _calendar_service = service
        return service
    except HttpError as error:
        logging.error(f"Błąd HTTP podczas tworzenia usługi Google Calendar API: {error}", exc_info=True)
        return None
    except Exception as e:
        logging.error(f"Nieoczekiwany błąd podczas tworzenia usługi Google Calendar API: {e}", exc_info=True)
        return None

def parse_event_time(event_time_data, default_tz):
    """Parsuje dane czasu wydarzenia z API Kalendarza na obiekt datetime lub date."""
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
                         logging.warning(f"Ostrz.: Nie udało się sparsować dateTime (z Z): {dt_str}")
                         return None
            else:
                try:
                    if ':' in dt_str[-6:]:
                       dt_str = dt_str[:-3] + dt_str[-2:]
                    dt = datetime.datetime.strptime(dt_str, '%Y-%m-%dT%H:%M:%S%z')
                except ValueError:
                    logging.warning(f"Ostrz.: Nie udało się sparsować dateTime: {dt_str}")
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
        logging.error("Błąd: Usługa kalendarza niedostępna w get_free_time_ranges.")
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
        logging.info("Zakres wyszukiwania wolnych terminów jest nieprawidłowy (start >= end).")
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

        busy_times = []
        for busy_slot in busy_times_raw:
            try:
                busy_start = datetime.datetime.fromisoformat(busy_slot['start']).astimezone(tz)
                busy_end = datetime.datetime.fromisoformat(busy_slot['end']).astimezone(tz)
                busy_start_clipped = max(busy_start, start_datetime)
                busy_end_clipped = min(busy_end, end_datetime)
                if busy_start_clipped < busy_end_clipped:
                   busy_times.append({'start': busy_start_clipped, 'end': busy_end_clipped})
            except ValueError as e:
                logging.warning(f"Ostrz.: Nie sparsowano zajętego czasu: {busy_slot}, błąd: {e}")

    except HttpError as error:
        logging.error(f'Błąd API Google Calendar (Freebusy): {error}', exc_info=True)
        return []
    except Exception as e:
        logging.error(f"Nieoczekiwany błąd Freebusy: {e}", exc_info=True)
        return []

    if not busy_times:
        merged_busy_times = []
    else:
        busy_times.sort(key=lambda x: x['start'])
        merged_busy_times = [busy_times[0]]
        for current_busy in busy_times[1:]:
            last_merged = merged_busy_times[-1]
            if current_busy['start'] <= last_merged['end']:
                last_merged['end'] = max(last_merged['end'], current_busy['end'])
            else:
                merged_busy_times.append(current_busy)

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
            current_day_start = max(current_day_start, range_start)

    logging.info(f"Znaleziono {len(final_free_slots)} wolnych zakresów czasowych.")
    return final_free_slots

def is_slot_actually_free(start_time, calendar_id):
    """Weryfikuje w czasie rzeczywistym, czy slot jest wolny."""
    service = get_calendar_service()
    tz = _get_timezone()
    if not service:
        logging.error("Błąd: Usługa kalendarza niedostępna do weryfikacji slotu.")
        return False

    if start_time.tzinfo is None:
        start_time = tz.localize(start_time)
    else:
        start_time = start_time.astimezone(tz)
    end_time = start_time + datetime.timedelta(minutes=APPOINTMENT_DURATION_MINUTES)

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
            logging.info(f"Weryfikacja: Slot {start_time:%Y-%m-%d %H:%M} jest POTWIERDZONY jako wolny.")
            return True
        else:
            for busy in busy_times:
                busy_start = datetime.datetime.fromisoformat(busy['start']).astimezone(tz)
                busy_end = datetime.datetime.fromisoformat(busy['end']).astimezone(tz)
                # Sprawdza czy jest jakakolwiek kolizja (overlap)
                if max(start_time, busy_start) < min(end_time, busy_end):
                    logging.warning(f"Weryfikacja: Slot {start_time:%Y-%m-%d %H:%M} jest ZAJĘTY (kolizja z {busy_start:%H:%M}-{busy_end:%H:%M}).")
                    return False
            logging.info(f"Weryfikacja: Slot {start_time:%Y-%m-%d %H:%M} jest POTWIERDZONY jako wolny (zajętości nie kolidują).")
            return True

    except HttpError as error:
        logging.error(f'Błąd API Google Calendar (Freebusy) podczas weryfikacji: {error}', exc_info=True)
        return False
    except Exception as e:
        logging.error(f"Nieoczekiwany błąd weryfikacji Freebusy: {e}", exc_info=True)
        return False

def book_appointment(calendar_id, start_time, end_time, summary="Rezerwacja FB", description="", user_name=""):
    """Rezerwuje termin w Kalendarzu Google."""
    service = get_calendar_service()
    tz = _get_timezone()
    if not service:
        return False, "Błąd: Brak połączenia z usługą kalendarza."

    if start_time.tzinfo is None:
        start_time = tz.localize(start_time)
    else:
        start_time = start_time.astimezone(tz)
    if end_time.tzinfo is None:
        end_time = tz.localize(end_time)
    else:
        end_time = end_time.astimezone(tz)

    event_summary = summary + (f" - {user_name}" if user_name else "")

    event = {
        'summary': event_summary,
        'description': description,
        'start': {'dateTime': start_time.isoformat(), 'timeZone': CALENDAR_TIMEZONE},
        'end': {'dateTime': end_time.isoformat(), 'timeZone': CALENDAR_TIMEZONE},
        'reminders': {'useDefault': False, 'overrides': [{'method': 'popup', 'minutes': 60}]},
        'status': 'confirmed',
    }

    try:
        logging.info(f"Próba rezerwacji: '{event_summary}' od {start_time:%Y-%m-%d %H:%M} do {end_time:%Y-%m-%d %H:%M}")
        created_event = service.events().insert(calendarId=calendar_id, body=event).execute()
        event_id = created_event.get('id')
        logging.info(f"Termin zarezerwowany pomyślnie. ID wydarzenia: {event_id}")

        day_index = start_time.weekday()
        locale_day_name = POLISH_WEEKDAYS[day_index]
        hour_str = str(start_time.hour)
        confirm_message = f"Świetnie! Termin na {locale_day_name}, {start_time.strftime(f'%d.%m.%Y o {hour_str}:%M')} został zarezerwowany."
        return True, confirm_message

    except HttpError as error:
        error_details = f"Kod: {error.resp.status}, Powód: {error.resp.reason}"
        try:
            error_content = json.loads(error.content.decode('utf-8'))
            error_message = error_content.get('error', {}).get('message', '')
            if error_message:
                 error_details += f" - {error_message}"
        except Exception:
            pass
        logging.error(f"Błąd API Google Calendar podczas rezerwacji: {error}, Szczegóły: {error_details}", exc_info=True)
        if error.resp.status == 409:
            return False, "Niestety, ten termin został właśnie zajęty."
        elif error.resp.status == 403:
            return False, "Problem z uprawnieniami do zapisu w kalendarzu."
        elif error.resp.status == 404:
            return False, f"Nie znaleziono kalendarza docelowego ('{calendar_id}')."
        elif error.resp.status == 400:
             return False, f"Błąd danych podczas próby rezerwacji. ({error_details})"
        else:
            return False, f"Nieoczekiwany błąd ({error.resp.status}) podczas rezerwacji."
    except Exception as e:
        logging.error(f"Nieoczekiwany błąd Python podczas rezerwacji: {e}", exc_info=True)
        return False, "Wewnętrzny błąd systemu rezerwacji."

def format_ranges_for_ai(ranges):
    """Formatuje listę zakresów czasowych na czytelny tekst dla AI."""
    if not ranges:
        return "Brak dostępnych zakresów czasowych."
    ranges_by_date = defaultdict(list)
    tz = _get_timezone()
    for r in ranges:
        range_date = r['start'].date()
        start_time = r['start'].astimezone(tz)
        end_time = r['end'].astimezone(tz)
        ranges_by_date[range_date].append({
            'start_time': start_time.strftime('%H:%M'),
            'end_time': end_time.strftime('%H:%M')
        })

    formatted_lines = [
        f"Dostępne ZAKRESY czasowe (wizyta trwa {APPOINTMENT_DURATION_MINUTES} minut). Wybierz JEDEN zakres i wygeneruj DOKŁADNY termin startu (preferuj pełne godziny), dołączając go w znaczniku [SLOT_ISO:...].",
        "--- Dostępne Zakresy ---"
        ]
    dates_added = 0
    max_dates_to_show = 7
    for d in sorted(ranges_by_date.keys()):
        day_name = POLISH_WEEKDAYS[d.weekday()]
        date_str = d.strftime('%d.%m.%Y')
        time_ranges_str = '; '.join(f"{tr['start_time']}-{tr['end_time']}"
                                  for tr in sorted(ranges_by_date[d], key=lambda x: x['start_time']))
        if time_ranges_str:
            formatted_lines.append(f"- {day_name}, {date_str}: {time_ranges_str}")
            dates_added += 1
            if dates_added >= max_dates_to_show:
                formatted_lines.append("- ... (i potencjalnie więcej)")
                break
    if dates_added == 0:
        return "Brak dostępnych zakresów czasowych w godzinach pracy."
    return "\n".join(formatted_lines)

def format_slot_for_user(slot_start):
    """Formatuje pojedynczy slot (datetime) na czytelny tekst dla użytkownika."""
    if not isinstance(slot_start, datetime.datetime):
        logging.warning(f"Próba formatowania niepoprawnego typu slotu: {type(slot_start)}")
        return "[Błąd formatowania daty]"
    try:
        tz = _get_timezone()
        if slot_start.tzinfo is None:
            slot_start = tz.localize(slot_start)
        else:
            slot_start = slot_start.astimezone(tz)
        day_index = slot_start.weekday()
        day_name = POLISH_WEEKDAYS[day_index]
        hour_str = str(slot_start.hour) # Godzina bez wiodącego zera
        return f"{day_name}, {slot_start.strftime(f'%d.%m.%Y o {hour_str}:%M')}"
    except Exception as e:
        logging.error(f"Błąd formatowania slotu {slot_start}: {e}", exc_info=True)
        return slot_start.isoformat() # Fallback

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
        logging.warning(f"[{recipient_id}] Błąd podczas wysyłania 'typing_on': {e}")

def _send_single_message(recipient_id, message_text):
    """Wysyła pojedynczy fragment wiadomości przez Facebook Graph API."""
    logging.info(f"--- Wysyłanie fragmentu do {recipient_id} (dł: {len(message_text)}) ---")
    if not PAGE_ACCESS_TOKEN or len(PAGE_ACCESS_TOKEN) < 50:
        logging.error(f"!!! [{recipient_id}] Brak/nieprawidłowy PAGE_ACCESS_TOKEN. NIE WYSŁANO.")
        return False
    params = {"access_token": PAGE_ACCESS_TOKEN}
    payload = {"recipient": {"id": recipient_id}, "message": {"text": message_text}, "messaging_type": "RESPONSE"}
    try:
        r = requests.post(FACEBOOK_GRAPH_API_URL, params=params, json=payload, timeout=30)
        r.raise_for_status()
        response_json = r.json()
        if response_json.get('error'):
            fb_error = response_json['error']
            logging.error(f"!!! BŁĄD FB API podczas wysyłania do {recipient_id}: {fb_error} !!!")
            if fb_error.get('code') == 190:
                 logging.error("!!! POTWIERDZONO BŁĄD TOKENA (code 190) !!!")
            return False
        logging.debug(f"[{recipient_id}] Fragment wysłany pomyślnie.")
        return True
    except requests.exceptions.Timeout:
        logging.error(f"!!! BŁĄD TIMEOUT wysyłania do {recipient_id} !!!")
        return False
    except requests.exceptions.HTTPError as e:
        logging.error(f"!!! BŁĄD HTTP {e.response.status_code} wysyłania do {recipient_id}: {e} !!!")
        if e.response is not None:
            try:
                logging.error(f"Odpowiedź FB (błąd HTTP): {e.response.json()}")
            except json.JSONDecodeError:
                logging.error(f"Odpowiedź FB (błąd HTTP, nie JSON): {e.response.text}")
        return False
    except requests.exceptions.RequestException as e:
        logging.error(f"!!! BŁĄD sieciowy wysyłania do {recipient_id}: {e} !!!")
        return False
    except Exception as e:
        logging.error(f"!!! Nieoczekiwany BŁĄD wysyłania do {recipient_id}: {e} !!!", exc_info=True)
        return False

def send_message(recipient_id, full_message_text):
    """Wysyła wiadomość do użytkownika, dzieląc ją na fragmenty."""
    if not full_message_text or not isinstance(full_message_text, str) or not full_message_text.strip():
        logging.warning(f"[{recipient_id}] Pominięto wysłanie pustej wiadomości.")
        return
    message_len = len(full_message_text)
    logging.info(f"[{recipient_id}] Przygotowanie wiadomości (dł: {message_len}).")
    if ENABLE_TYPING_DELAY:
        est_dur = min(MAX_TYPING_DELAY_SECONDS, max(MIN_TYPING_DELAY_SECONDS, message_len / TYPING_CHARS_PER_SECOND))
        logging.debug(f"[{recipient_id}] Szacowany czas pisania: {est_dur:.2f}s")
        _send_typing_on(recipient_id)
        time.sleep(est_dur)
    chunks = []
    if message_len <= MESSAGE_CHAR_LIMIT:
        chunks.append(full_message_text)
    else:
        logging.info(f"[{recipient_id}] Dzielenie wiadomości (limit: {MESSAGE_CHAR_LIMIT})...")
        remaining_text = full_message_text
        while remaining_text:
            if len(remaining_text) <= MESSAGE_CHAR_LIMIT:
                chunks.append(remaining_text.strip())
                break
            split_index = -1
            delimiters = ['\n\n', '\n', '. ', '! ', '? ', ' ']
            for d in delimiters:
                s_lim = MESSAGE_CHAR_LIMIT - len(d) + 1
                t_idx = remaining_text.rfind(d, 0, s_lim)
                if t_idx != -1:
                    split_index = t_idx + len(d)
                    break
            if split_index == -1:
                split_index = MESSAGE_CHAR_LIMIT
            chunk = remaining_text[:split_index].strip()
            if chunk:
                chunks.append(chunk)
            remaining_text = remaining_text[split_index:].strip()
        logging.info(f"[{recipient_id}] Podzielono na {len(chunks)} fragmentów.")
    num_chunks = len(chunks)
    send_success_count = 0
    for i, chunk in enumerate(chunks):
        logging.debug(f"[{recipient_id}] Wysyłanie fragm. {i+1}/{num_chunks} (dł: {len(chunk)})...")
        if not _send_single_message(recipient_id, chunk):
            logging.error(f"!!! [{recipient_id}] Błąd wysyłania fragm. {i+1}. Anulowano resztę. !!!")
            break
        send_success_count += 1
        if num_chunks > 1 and i < num_chunks - 1:
            logging.debug(f"[{recipient_id}] Oczekiwanie {MESSAGE_DELAY_SECONDS}s...")
            if ENABLE_TYPING_DELAY:
                est_dur = min(MAX_TYPING_DELAY_SECONDS, max(MIN_TYPING_DELAY_SECONDS, len(chunks[i+1]) / TYPING_CHARS_PER_SECOND)) * 0.5
                _send_typing_on(recipient_id)
                time.sleep(est_dur + MESSAGE_DELAY_SECONDS * 0.5)
                time.sleep(MESSAGE_DELAY_SECONDS * 0.5)
            else:
                time.sleep(MESSAGE_DELAY_SECONDS)
    logging.info(f"--- [{recipient_id}] Zakończono wysyłanie. Wysłano {send_success_count}/{num_chunks} fragm. ---")

def _simulate_typing(recipient_id, duration_seconds):
    """Wysyła 'typing_on' i czeka."""
    if ENABLE_TYPING_DELAY and duration_seconds > 0:
        _send_typing_on(recipient_id)
        time.sleep(min(duration_seconds, MAX_TYPING_DELAY_SECONDS))

def _call_gemini(user_psid, prompt_history, generation_config, task_name, max_retries=3):
    """Wywołuje API Gemini z obsługą błędów i logowaniem."""
    # Sprawdzenie globalnej zmiennej gemini_model
    if not gemini_model:
        logging.error(f"!!! [{user_psid}] Model Gemini ({task_name}) niezaładowany (None). Nie można wywołać API.")
        return None
    if not isinstance(prompt_history, list) or not all(isinstance(item, Content) for item in prompt_history):
        logging.error(f"!!! [{user_psid}] Nieprawidłowy format promptu dla Gemini ({task_name}).")
        return None
    logging.info(f"[{user_psid}] Wywołanie Gemini: {task_name} (Prompt: {len(prompt_history)} wiad.)")
    attempt = 0
    while attempt < max_retries:
        attempt += 1
        logging.debug(f"    Próba {attempt}/{max_retries} Gemini ({task_name})...")
        try:
            _simulate_typing(user_psid, MIN_TYPING_DELAY_SECONDS)
            # Użycie globalnej zmiennej gemini_model
            response = gemini_model.generate_content(
                prompt_history,
                generation_config=generation_config,
                safety_settings=SAFETY_SETTINGS
            )
            if response and response.candidates:
                finish_reason = response.candidates[0].finish_reason
                if finish_reason != 1: # 1 = STOP
                    safety_ratings = response.candidates[0].safety_ratings
                    logging.warning(f"[{user_psid}] Gemini ({task_name}) ZABLOKOWANE/NIEDOKOŃCZONE! Powód: {finish_reason}. Safety: {safety_ratings}")
                    if attempt < max_retries:
                        logging.warning(f"    Ponawianie ({attempt}/{max_retries}) po blokadzie...")
                        time.sleep(1 * attempt)
                        continue
                    else:
                        logging.error(f"!!! [{user_psid}] Gemini ({task_name}) nieudane po blokadzie po {max_retries} próbach.")
                        return "Przepraszam, problem z zasadami bezpieczeństwa."
                if response.candidates[0].content and response.candidates[0].content.parts:
                    generated_text = "".join(part.text for part in response.candidates[0].content.parts if hasattr(part, 'text'))
                    logging.info(f"[{user_psid}] Gemini ({task_name}) zwróciło odp. (dł: {len(generated_text)}).")
                    return generated_text.strip()
                else:
                    logging.warning(f"[{user_psid}] Gemini ({task_name}) zwróciło kandydata bez treści. Response: {response}")
            else:
                 prompt_feedback = response.prompt_feedback if hasattr(response, 'prompt_feedback') else 'Brak'
                 logging.error(f"!!! BŁĄD [{user_psid}] Gemini ({task_name}) - Brak kandydatów. Feedback: {prompt_feedback}. Odp: {response}")

        except HttpError as http_err:
             logging.error(f"!!! BŁĄD HTTP ({http_err.resp.status}) [{user_psid}] Gemini ({task_name}) - Próba {attempt}/{max_retries}: {http_err.resp.reason}")
             if http_err.resp.status in [429, 500, 503] and attempt < max_retries:
                  sleep_time = (2 ** attempt) + (random.random() * 0.1)
                  logging.warning(f"    Oczekiwanie {sleep_time:.2f}s przed ponowieniem...")
                  time.sleep(sleep_time)
                  continue
             else:
                  break
        except Exception as e:
             if isinstance(e, NameError) and 'gemini_model' in str(e):
                 logging.critical(f"!!! KRYTYCZNY BŁĄD NameError [{user_psid}] w _call_gemini: {e}. gemini_model jest None!", exc_info=True)
                 # W tej sytuacji nie ma sensu ponawiać, bo model nie istnieje
                 return None # Zwróć None, aby funkcja nadrzędna wiedziała o problemie
             else:
                 logging.error(f"!!! BŁĄD [{user_psid}] Gemini ({task_name}) - Nieoczekiwany błąd API (Próba {attempt}/{max_retries}): {e}", exc_info=True)

        if attempt < max_retries:
             logging.warning(f"    Oczekiwanie przed ponowieniem ({attempt+1}/{max_retries})...")
             time.sleep(1.5 * attempt)

    logging.error(f"!!! KRYTYCZNY BŁĄD [{user_psid}] Gemini ({task_name}) - Nie udało się po {max_retries} próbach.")
    return None

# =====================================================================
# === INSTRUKCJE SYSTEMOWE I GŁÓWNE FUNKCJE AI ========================
# =====================================================================

# --- INSTRUKCJA SYSTEMOWA (Propozycja terminu - WZMOCNIONA) ---
SYSTEM_INSTRUCTION_TEXT_PROPOSE = """Jesteś profesjonalnym asystentem klienta 'Zakręcone Korepetycje'. Twoim zadaniem jest przeanalizowanie historii rozmowy i listy dostępnych zakresów czasowych, a następnie wybranie **jednego**, najbardziej odpowiedniego terminu i zaproponowanie go użytkownikowi.

**Kontekst:** Rozmawiasz o korepetycjach online. Użytkownik chce umówić lekcję próbną (płatną). W historii rozmowy może znajdować się feedback dotyczący poprzednio proponowanych terminów.

**Dostępne zakresy czasowe:**
{available_ranges_text}

**Twoje zadanie:**
1.  Analizuj **całą** historię rozmowy, **w tym ostatnią odpowiedź użytkownika**, pod kątem preferencji (dzień, pora dnia, godzina, feedback na poprzednie propozycje np. "za wcześnie", "wolę piątek", "pasuje mi tylko o 18").
2.  Wybierz **jeden** zakres z listy "Dostępne zakresy czasowe", który **najlepiej pasuje do preferencji wywnioskowanych z historii** lub jest "rozsądny" (np. popołudnie w dni robocze >= {pref_weekday}h, weekend >= {pref_weekend}h), jeśli brak wyraźnych preferencji.
3.  W wybranym zakresie **wygeneruj DOKŁADNY czas startu** wizyty (trwa {duration} minut). **Preferuj PEŁNE GODZINY** (np. 16:00, 17:00) jeśli to możliwe i zgodne z preferencjami w danym zakresie. Jeśli użytkownik podał konkretną godzinę (np. "tylko 18:00"), spróbuj ją zaproponować, jeśli jest dostępna.
4.  **BARDZO WAŻNE:** Upewnij się, że `wygenerowany_czas_startu + {duration} minut` **mieści się w wybranym przez Ciebie zakresie czasowym** z listy.
5.  Sformułuj krótką, uprzejmą propozycję wygenerowanego terminu (użyj polskiego formatu daty i dnia tygodnia). Jeśli użytkownik wcześniej odrzucił termin, możesz krótko nawiązać, np. "Rozumiem, w takim razie może..." lub "Sprawdziłem inne opcje, proponuję...".
6.  **KLUCZOWE:** Twoja odpowiedź **MUSI** zawierać na końcu znacznik `{slot_marker_prefix}WYGENEROWANY_ISO_STRING{slot_marker_suffix}` z poprawnym czasem startu w formacie ISO 8601 (np. `2024-07-28T17:00:00+02:00`).
7.  **NAJWAŻNIEJSZE:** **Twoim GŁÓWNYM zadaniem w tej chwili jest ZAPROPONOWANIE konkretnego terminu z listy i dodanie znacznika ISO.** Nawet jeśli żaden zakres nie pasuje idealnie do preferencji z historii, wybierz najbardziej rozsądny dostępny termin (np. popołudniowy w tygodniu) i zaproponuj go. **NIE ZADAWAJ dodatkowych pytań w tej odpowiedzi.** Po prostu zaproponuj termin i dodaj znacznik.

**Przykład (dostępny zakres "Środa, 07.05.2025: 16:00-18:30", historia zawiera feedback "wolałbym coś koło 17"):**
*   Dobry wynik: "Ok, może w takim razie pasowałaby Środa, 07.05.2025 o 17:00? {slot_marker_prefix}2025-05-07T17:00:00+02:00{slot_marker_suffix}"

**Zasady:** Zawsze generuj tylko JEDEN termin. Zawsze sprawdzaj, czy mieści się w zakresie. Zawsze dołączaj znacznik ISO na końcu. Opieraj wybór na historii rozmowy. Bądź uprzejmy. **Nie zadawaj pytań, tylko proponuj.**
""".format(
    available_ranges_text="{available_ranges_text}", # Placeholder
    pref_weekday=PREFERRED_WEEKDAY_START_HOUR,
    pref_weekend=PREFERRED_WEEKEND_START_HOUR,
    duration=APPOINTMENT_DURATION_MINUTES,
    slot_marker_prefix=SLOT_ISO_MARKER_PREFIX,
    slot_marker_suffix=SLOT_ISO_MARKER_SUFFIX
)

# --- INSTRUKCJA SYSTEMOWA (Interpretacja feedbacku - UPROSZCZONA) ---
SYSTEM_INSTRUCTION_TEXT_FEEDBACK_SIMPLE = """Jesteś asystentem AI analizującym odpowiedź użytkownika na propozycję terminu.

**Kontekst:** Zaproponowano użytkownikowi termin wizyty.
**Ostatnia propozycja:** "{last_proposal_text}"
**Odpowiedź użytkownika:** "{user_feedback}"

**Twoje zadanie:** Przeanalizuj odpowiedź użytkownika i zwróć **DOKŁADNIE JEDEN** z poniższych trzech znaczników:

*   `[ACCEPT]`: Jeśli użytkownik akceptuje zaproponowany termin (np. "tak", "ok", "pasuje", "zgoda", "może być").
*   `[REJECT]`: Jeśli użytkownik odrzuca zaproponowany termin, niezależnie od tego, czy podaje preferencje co do następnego, czy nie (np. "nie pasuje", "inny termin proszę", "za wcześnie", "wolę środę", "tylko o 18"). Po prostu odrzuca obecną propozycję.
*   `[CLARIFY]`: Jeśli odpowiedź użytkownika jest niejasna w kontekście propozycji terminu, niejednoznaczna, lub jest pytaniem niezwiązanym bezpośrednio z akceptacją/odrzuceniem (np. "ile to kosztuje?", "a co będziemy robić?", "nie wiem jeszcze", "może").

**Ważne:** Zwróć **tylko jeden** znacznik: `[ACCEPT]`, `[REJECT]` lub `[CLARIFY]`.
"""

# --- INSTRUKCJA SYSTEMOWA (Ogólna rozmowa - bez zmian) ---
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


# --- Funkcja AI: Propozycja slotu (używa wzmocnionej instrukcji) ---
def get_gemini_slot_proposal(user_psid, history_for_proposal_ai, available_ranges):
    """Pobiera propozycję terminu od AI."""
    if not gemini_model:
        logging.error(f"!!! [{user_psid}] Model Gemini niezaładowany!")
        return None, None
    if not available_ranges:
        logging.warning(f"[{user_psid}]: Brak zakresów do przekazania AI.")
        return "Niestety, brak dostępnych zakresów.", None

    ranges_text = format_ranges_for_ai(available_ranges)
    system_instruction = SYSTEM_INSTRUCTION_TEXT_PROPOSE.format(available_ranges_text=ranges_text)
    initial_prompt = [
        Content(role="user", parts=[Part.from_text(system_instruction)]),
        Content(role="model", parts=[Part.from_text("Rozumiem. Wybieram JEDEN termin z listy i ZAWSZE dodaję znacznik [SLOT_ISO:...].")])
    ]
    full_prompt = initial_prompt + history_for_proposal_ai

    max_prompt_messages = (MAX_HISTORY_TURNS * 2) + 2
    while len(full_prompt) > max_prompt_messages:
        full_prompt.pop(2)
        if len(full_prompt) > 2:
            full_prompt.pop(2)

    generated_text = _call_gemini(user_psid, full_prompt, GENERATION_CONFIG_PROPOSAL, "Slot Proposal Strict")

    if not generated_text:
        logging.error(f"!!! [{user_psid}] Nie uzyskano odpowiedzi Gemini (Slot Proposal Strict).")
        return "Problem z systemem proponowania terminów.", None

    iso_match = re.search(rf"{re.escape(SLOT_ISO_MARKER_PREFIX)}(.*?){re.escape(SLOT_ISO_MARKER_SUFFIX)}", generated_text)
    if iso_match:
        extracted_iso = iso_match.group(1).strip()
        text_for_user = re.sub(rf"{re.escape(SLOT_ISO_MARKER_PREFIX)}.*?{re.escape(SLOT_ISO_MARKER_SUFFIX)}", "", generated_text).strip()
        text_for_user = re.sub(r'\s+', ' ', text_for_user).strip()
        logging.info(f"[{user_psid}] AI (Strict) propozycja: ISO={extracted_iso}, Tekst='{text_for_user}'")
        try:
            tz = _get_timezone()
            proposed_start = datetime.datetime.fromisoformat(extracted_iso).astimezone(tz)
            proposed_end = proposed_start + datetime.timedelta(minutes=APPOINTMENT_DURATION_MINUTES)
            is_within = any(r['start'] <= proposed_start and proposed_end <= r['end'] for r in available_ranges)
            if not is_within:
                 logging.error(f"!!! BŁĄD Walidacji AI (Strict) [{user_psid}]: ISO '{extracted_iso}' poza zakresami!")
                 return "Błąd wybierania terminu.", None
            if is_slot_actually_free(proposed_start, TARGET_CALENDAR_ID):
                 return text_for_user, extracted_iso
            else:
                 logging.warning(f"!!! [{user_psid}]: Slot {extracted_iso} ZAJĘTY (weryfikacja)!")
                 return "Ten termin właśnie się zajął. Szukam innego...", None
        except ValueError:
            logging.error(f"!!! BŁĄD AI (Strict) [{user_psid}]: '{extracted_iso}' nie jest ISO!")
            return "Błąd przetwarzania terminu.", None
        except Exception as val_err:
            logging.error(f"!!! BŁĄD Walidacji AI (Strict) [{user_psid}]: {val_err}", exc_info=True)
            return "Błąd weryfikacji terminu.", None
    else:
        logging.critical(f"!!! KRYTYCZNY BŁĄD AI (Strict) [{user_psid}]: Brak ISO! Odp: '{generated_text}'")
        clean_text = generated_text.strip()
        if clean_text:
             return clean_text, None # Zwróć sam tekst bez ISO
        else:
             return "Błąd generowania propozycji.", None

# --- Funkcja AI: Interpretacja feedbacku (UPROSZCZONA) ---
def get_gemini_simple_feedback_decision(user_psid, user_feedback_text, history_for_feedback_ai, last_proposed_slot_text):
     """Prosi AI o zwrócenie [ACCEPT], [REJECT] lub [CLARIFY]."""
     if not gemini_model:
         logging.error(f"!!! [{user_psid}] Model Gemini niezaładowany!"); return "[CLARIFY]"

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
         logging.error(f"!!! [{user_psid}] Nie uzyskano odpowiedzi Gemini (Simple Feedback)."); return "[CLARIFY]"

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
        logging.error(f"!!! [{user_psid}] Model Gemini niezaładowany!"); return "Przepraszam, problem z systemem."
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
                    logging.debug(f"Wczytano {len(history_for_gemini)} wiadomości user/model.")

                    is_context_active = False
                    last_iso_from_context = None
                    last_proposal_text_for_feedback = "poprzedni termin"
                    if context.get('type') == 'last_proposal' and context.get('slot_iso'):
                        temp_hist_check, temp_ctx_check = load_history(sender_id) # Sprawdzenie aktualności kontekstu
                        if temp_ctx_check.get('slot_iso') == context.get('slot_iso'):
                            is_context_active = True
                            last_iso_from_context = context['slot_iso']
                            try:
                                last_dt = datetime.datetime.fromisoformat(last_iso_from_context).astimezone(_get_timezone())
                                last_proposal_text_for_feedback = format_slot_for_user(last_dt)
                            except Exception as fmt_err:
                                logging.warning(f"Nie sformatowano ISO '{last_iso_from_context}' dla feedbacku: {fmt_err}")
                            logging.info(f"    Aktywny kontekst: {last_iso_from_context} ({last_proposal_text_for_feedback})")
                        else:
                            logging.info(f"    Kontekst '{context.get('slot_iso')}' nieaktualny.")
                            context = {} # Reset

                    action = None
                    msg_result = None
                    ctx_save = context # Domyślnie zachowaj kontekst
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
                            logging.info(f"    Otrzymano tekst: '{user_input_text[:100]}{'...' if len(user_input_text)>100 else ''}'")
                            if ENABLE_TYPING_DELAY: time.sleep(MIN_TYPING_DELAY_SECONDS * 0.5)

                            # Przypadek 1: Odpowiedź na aktywną propozycję
                            if is_context_active:
                                logging.info("      -> Kontekst aktywny. Pytanie AI (Simple Feedback)...")
                                try:
                                    # Użycie uproszczonej funkcji feedbacku
                                    decision = get_gemini_simple_feedback_decision(
                                        sender_id,
                                        user_input_text,
                                        history_for_gemini,
                                        last_proposal_text_for_feedback
                                    )
                                    # Uproszczona logika reakcji
                                    if decision == "[ACCEPT]":
                                        action = 'book'
                                        logging.info(f"      Decyzja AI (Simple): {decision} -> Akcja: Rezerwacja")
                                        ctx_save = None # Reset po akceptacji
                                    elif decision == "[REJECT]":
                                        action = 'find_and_propose'
                                        logging.info(f"      Decyzja AI (Simple): {decision} -> Akcja: Odrzucenie i szukanie")
                                        msg_result = "Rozumiem. W takim razie poszukam innego terminu..."
                                        ctx_save = None # Reset starego kontekstu
                                    elif decision == "[CLARIFY]":
                                        action = 'send_clarification'
                                        logging.info(f"      Decyzja AI (Simple): {decision} -> Akcja: Prośba o doprecyzowanie")
                                        msg_result = "Nie jestem pewien, co masz na myśli w kontekście terminu. Czy możesz wyjaśnić?"
                                        ctx_save = context # Zachowaj kontekst
                                    else: # Nieoczekiwany wynik
                                        action = 'send_error'
                                        logging.warning(f"      Niespodziewana decyzja AI (Simple Feedback): {decision}.")
                                        msg_result = "Problem ze zrozumieniem odpowiedzi."
                                        ctx_save = None
                                except Exception as feedback_err:
                                    logging.error(f"!!! BŁĄD AI (Simple Feedback): {feedback_err}", exc_info=True)
                                    action = 'send_error'
                                    msg_result = "Błąd interpretacji odpowiedzi."
                                    ctx_save = None

                            # Przypadek 2: Normalna rozmowa
                            else:
                                logging.info("      -> Kontekst nieaktywny. Pytanie AI (General)...")
                                response = get_gemini_general_response(sender_id, user_input_text, history_for_gemini)
                                if response:
                                    if INTENT_SCHEDULE_MARKER in response:
                                        logging.info(f"      AI wykryło intencję [{INTENT_SCHEDULE_MARKER}].")
                                        action = 'find_and_propose'
                                        initial_resp_text = response.split(INTENT_SCHEDULE_MARKER, 1)[0].strip()
                                        msg_result = initial_resp_text if initial_resp_text else "Sprawdzę terminy."
                                        ctx_save = None # Reset przed szukaniem
                                    else:
                                        action = 'send_gemini_response'
                                        msg_result = response
                                        ctx_save = None # Reset kontekstu
                                else:
                                    action = 'send_error'
                                    msg_result = "Błąd przetwarzania."
                                    ctx_save = None

                        # Przypadek 3: Załączniki / Puste wiadomości
                        elif attachments := message_data.get("attachments"):
                             att_type = attachments[0].get('type','nieznany')
                             logging.info(f"      Otrzymano załącznik: {att_type}.")
                             user_content = Content(role="user", parts=[Part.from_text(f"[Załącznik: {att_type}]")])
                             msg_result = "Nie obsługuję załączników." if att_type != 'sticker' else "👍"
                             action = 'send_info'
                             ctx_save = context # Zachowaj kontekst, jeśli był aktywny
                        else:
                            logging.info("      Otrzymano pustą wiadomość.")
                            if is_context_active:
                                action = 'send_clarification'
                                msg_result = "Nie otrzymałem odpowiedzi. Czy termin pasuje?"
                                ctx_save = context
                            else:
                                action = None # Ignoruj
                                ctx_save = None

                    # === Obsługa Postback ===
                    elif postback := event.get("postback"):
                        payload = postback.get("payload")
                        title = postback.get("title", "")
                        logging.info(f"    Postback: '{payload}', Tytuł: '{title}'")
                        user_content = Content(role="user", parts=[Part.from_text(f"[Przycisk: {title} ({payload})]")])
                        if payload == "ACCEPT_SLOT":
                            if is_context_active and last_iso_from_context:
                                logging.info("      Postback: Akceptacja -> Akcja: Rezerwacja")
                                action = 'book'
                                msg_result = None
                                ctx_save = None
                            else:
                                logging.warning("      Postback 'ACCEPT_SLOT' bez kontekstu.")
                                action = 'send_info'
                                msg_result = "Propozycja wygasła."
                                ctx_save = None
                        elif payload == "REJECT_SLOT":
                            if is_context_active and last_iso_from_context:
                                logging.info("      Postback: Odrzucenie -> Akcja: Szukanie")
                                action = 'find_and_propose'
                                msg_result = "OK, szukam innego terminu..."
                                ctx_save = None
                            else:
                                logging.warning("      Postback 'REJECT_SLOT' bez kontekstu.")
                                action = 'send_info'
                                msg_result = "Brak propozycji do odrzucenia."
                                ctx_save = None
                        else: # Inne postbacki
                            logging.warning(f"      Nieznany postback: '{payload}'.")
                            simulated_input = f"[Przycisk: {title}]"
                            response = get_gemini_general_response(sender_id, simulated_input, history_for_gemini)
                            if response:
                                if INTENT_SCHEDULE_MARKER in response:
                                    action = 'find_and_propose'
                                    initial_resp_text = response.split(INTENT_SCHEDULE_MARKER, 1)[0].strip()
                                    msg_result = initial_resp_text if initial_resp_text else "Sprawdzę."
                                    ctx_save = None
                                else:
                                    action = 'send_gemini_response'
                                    msg_result = response
                                    ctx_save = None
                            else:
                                action = 'send_error'
                                msg_result = "Błąd przetwarzania."
                                ctx_save = None

                    # === Inne zdarzenia ===
                    elif event.get("read"):
                        logging.debug(f"    Potw. odczytania.")
                        continue
                    elif event.get("delivery"):
                        logging.debug(f"    Potw. dostarczenia.")
                        continue
                    else:
                        logging.warning(f"    Nieobsługiwany typ zdarzenia: {json.dumps(event)}")
                        continue


                    # --- WYKONANIE ZAPLANOWANEJ AKCJI ---
                    history_saved_in_this_cycle = False
                    if action == 'book':
                        if last_iso_from_context:
                            try:
                                tz = _get_timezone()
                                start = datetime.datetime.fromisoformat(last_iso_from_context).astimezone(tz)
                                end = start + datetime.timedelta(minutes=APPOINTMENT_DURATION_MINUTES)
                                prof = get_user_profile(sender_id)
                                name = prof.get('first_name', '') if prof else f"U_{sender_id[-4:]}"
                                desc = f"FB Bot\nPSID: {sender_id}" + (f"\nNazwisko: {prof.get('last_name')}" if prof and prof.get('last_name') else "")
                                ok, booking_msg = book_appointment(TARGET_CALENDAR_ID, start, end, f"FB: {name}", desc, name)
                                msg_result = booking_msg
                                if not ok:
                                     ctx_save = None # Reset jeśli błąd rezerwacji
                            except Exception as e:
                                logging.error(f"!!! BŁĄD rezerwacji {last_iso_from_context}: {e}", exc_info=True)
                                msg_result = "Błąd rezerwacji."
                                ctx_save = None
                        else:
                            logging.error("!!! BŁĄD LOGIKI: 'book' bez 'last_iso' !!!")
                            msg_result = "Błąd systemu."
                            ctx_save = None
                    elif action == 'find_and_propose':
                        try:
                            tz = _get_timezone()
                            now = datetime.datetime.now(tz)
                            search_start = now
                            search_end_date = (search_start + datetime.timedelta(days=MAX_SEARCH_DAYS)).date()
                            search_end = tz.localize(datetime.datetime.combine(search_end_date, datetime.time(WORK_END_HOUR, 0)))
                            logging.info(f"      -> Szukanie od {search_start:%Y-%m-%d %H:%M} do {search_end:%Y-%m-%d %H:%M}")
                            _simulate_typing(sender_id, MAX_TYPING_DELAY_SECONDS * 0.8)
                            free_ranges = get_free_time_ranges(TARGET_CALENDAR_ID, search_start, search_end)
                            if free_ranges:
                                logging.info(f"      Znaleziono {len(free_ranges)} zakresów. AI (Proposal Strict)...")
                                history_for_proposal_ai = history_for_gemini + ([user_content] if user_content else [])
                                proposal_text, proposed_iso = get_gemini_slot_proposal(sender_id, history_for_proposal_ai, free_ranges) # Używa wzmocnionej wersji
                                if proposal_text and proposed_iso:
                                    final_proposal_msg = (msg_result + "\n\n" + proposal_text) if msg_result else proposal_text
                                    msg_result = final_proposal_msg
                                    ctx_save = {'role': 'system', 'type': 'last_proposal', 'slot_iso': proposed_iso}
                                elif proposal_text and not proposed_iso: # AI dało tekst, ale bez ISO
                                     logging.warning(f"      AI (Strict) dało tekst '{proposal_text[:50]}...' bez ISO.")
                                     final_proposal_msg = (msg_result + "\n\n" + proposal_text) if msg_result else proposal_text
                                     msg_result = final_proposal_msg
                                     ctx_save = None # Reset kontekstu
                                else: # AI nie dało nic sensownego
                                    fail_msg = proposal_text if proposal_text else "Problem ze znalezieniem terminu."
                                    msg_result = (msg_result + "\n\n" + fail_msg) if msg_result else fail_msg
                                    ctx_save = None
                            else: # Brak wolnych zakresów
                                logging.warning(f"      Brak wolnych zakresów.")
                                no_slots_msg = f"Niestety, brak wolnych terminów w ciągu {MAX_SEARCH_DAYS} dni."
                                msg_result = (msg_result + "\n\n" + no_slots_msg) if msg_result else no_slots_msg
                                ctx_save = None
                        except Exception as find_err:
                            logging.error(f"!!! BŁĄD szukania/proponowania: {find_err}", exc_info=True)
                            error_msg = "Błąd wyszukiwania terminów."
                            msg_result = (msg_result + "\n\n" + error_msg) if msg_result else error_msg
                            ctx_save = None
                    elif action in ['send_gemini_response', 'send_clarification', 'send_error', 'send_info']:
                        logging.debug(f"      Akcja: {action}. Wiadomość gotowa.")
                        pass # msg_result i ctx_save już ustawione


                    # --- WYSYŁANIE ODPOWIEDZI I ZAPIS ---
                    if msg_result:
                        send_message(sender_id, msg_result)
                        model_resp_content = Content(role="model", parts=[Part.from_text(msg_result)])
                    elif action:
                        logging.warning(f"    Akcja '{action}' bez wiadomości.")

                    original_context_iso = context.get('slot_iso')
                    new_context_iso = ctx_save.get('slot_iso') if isinstance(ctx_save, dict) else None
                    should_save = bool(user_content) or bool(model_resp_content) or (original_context_iso != new_context_iso)

                    if should_save:
                        history_to_save = list(history)
                        if user_content:
                            history_to_save.append(user_content)
                        if model_resp_content:
                            history_to_save.append(model_resp_content)
                        logging.debug(f"Zapis historii. Nowy kontekst: {ctx_save}")
                        save_history(sender_id, history_to_save, context_to_save=ctx_save)
                        history_saved_in_this_cycle = True
                    else:
                        logging.debug("    Brak zmian - pomijanie zapisu.")

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
    log_level = logging.DEBUG if os.environ.get("FLASK_DEBUG", "False").lower() in ("true", "1", "yes") else logging.INFO
    # Konfiguruj logowanie tylko raz
    if not logging.getLogger().hasHandlers():
        logging.basicConfig(level=log_level, format='%(asctime)s - %(levelname)s - [%(funcName)s:%(lineno)d] - %(message)s', datefmt='%Y-%m-%d %H:%M:%S')

    # Wyciszenie loggerów
    logging.getLogger('googleapiclient.discovery_cache').setLevel(logging.ERROR)
    logging.getLogger('urllib3.connectionpool').setLevel(logging.WARNING)
    logging.getLogger('werkzeug').setLevel(logging.WARNING) # Mniej logów z serwera dev

    # Wypisanie konfiguracji startowej
    print("\n" + "="*60 + "\n--- START KONFIGURACJI BOTA ---")
    print(f"  * Tryb debugowania Flask: {'Włączony' if log_level == logging.DEBUG else 'Wyłączony'}")
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
    print(f"    Model AI: {MODEL_ID}") # Nadal gemini-2.0-flash-001
    # Sprawdzenie, czy model został załadowany (zmienna globalna)
    if not gemini_model:
        print("!!! OSTRZEŻENIE: Model Gemini AI NIE został załadowany poprawnie podczas startu! (Sprawdź logi krytyczne) !!!")
    else:
        print(f"    Model Gemini AI ({MODEL_ID}): Załadowany (OK)")
    print("-" * 60)
    print("  Konfiguracja Kalendarza Google:")
    print(f"    ID Kalendarza: {TARGET_CALENDAR_ID}")
    print(f"    Strefa czasowa: {CALENDAR_TIMEZONE} (Obiekt TZ: {_get_timezone()})")
    print(f"    Czas trwania wizyty: {APPOINTMENT_DURATION_MINUTES} min")
    print(f"    Godziny pracy: {WORK_START_HOUR}:00 - {WORK_END_HOUR}:00")
    print(f"    Preferowane godz. AI: W tygodniu >= {PREFERRED_WEEKDAY_START_HOUR}:00, Weekend >= {PREFERRED_WEEKEND_START_HOUR}:00")
    print(f"    Maks. zakres szukania: {MAX_SEARCH_DAYS} dni")
    print(f"    Plik klucza API: {SERVICE_ACCOUNT_FILE} ({'Znaleziono' if os.path.exists(SERVICE_ACCOUNT_FILE) else 'BRAK PLIKU!!!'})")
    cal_service = get_calendar_service()
    if not cal_service and os.path.exists(SERVICE_ACCOUNT_FILE): print("!!! OSTRZEŻENIE: Usługa Google Calendar NIE zainicjowana poprawnie. Sprawdź uprawnienia/API. !!!")
    elif not os.path.exists(SERVICE_ACCOUNT_FILE): print("!!! OSTRZEŻENIE: Brak pliku klucza Google Calendar - funkcje kalendarza nie będą działać. !!!")
    elif cal_service: print("    Usługa Google Calendar: Zainicjowana (OK)")
    print("--- KONIEC KONFIGURACJI BOTA ---"); print("="*60 + "\n")

    port = int(os.environ.get("PORT", 8080)); debug_mode = os.environ.get("FLASK_DEBUG", "False").lower() in ("true", "1", "yes")
    print(f"Uruchamianie serwera Flask na porcie {port}...")
    if not debug_mode:
        try: from waitress import serve; print(">>> Serwer produkcyjny Waitress START <<<"); serve(app, host='0.0.0.0', port=port, threads=8)
        except ImportError: print("!!! Ostrzeżenie: 'waitress' nie znaleziono. Uruchamianie serwera deweloperskiego Flask."); print(">>> Serwer deweloperski Flask START <<<"); app.run(host='0.0.0.0', port=port, debug=False)
    else: print(">>> Serwer deweloperski Flask (DEBUG MODE) START <<<"); app.run(host='0.0.0.0', port=port, debug=True)
