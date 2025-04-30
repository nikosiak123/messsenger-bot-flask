# -*- coding: utf-8 -*-

# verify_server.py (połączony kod z iteracyjnym szukaniem wg preferencji - AI decyduje o slocie - Wersja ze wzmocnioną instrukcją i niską temp.)

from flask import Flask, request, Response
import os
import json
import requests
import time
import vertexai
# Dodano import 'random' dla exponential backoff w _call_gemini
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
# Użyj testowego tokena, ale pamiętaj o błędach 400!
PAGE_ACCESS_TOKEN = os.environ.get("FB_PAGE_ACCESS_TOKEN", "EACNAHFzEhkUBOxSDMfOZCYbQAFKfVzJWowJpX8mcX0BvBGaWFRiUwNHjojZBcRXIPFszKzzRZBEqFI7AFD0DpI5sOeiN7HKLBGxBZB7tAgCkFdipRNQKevuP3F4kvSTIZCqqkrBaq7rPRM7FIqNQjP2Ju9UdZB5FNcvndzdZBZBGxTyyw9hkWmBndNr2A0VwO2Gf8QZDZD")
PROJECT_ID = os.environ.get("GCP_PROJECT_ID", "linear-booth-450221-k1")
LOCATION = os.environ.get("GCP_LOCATION", "us-central1")
MODEL_ID = os.environ.get("VERTEX_MODEL_ID", "gemini-2.0-flash-001") # Można zmienić na gemini-1.5-flash-001 lub inny

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
PREFERRED_WEEKDAY_START_HOUR = 16  # Godzina "popołudniowa" do podpowiedzi AI
PREFERRED_WEEKEND_START_HOUR = 10 # Godzina "weekendowa" do podpowiedzi AI
MAX_SEARCH_DAYS = 14  # Jak daleko w przyszłość szukać terminów

# --- Znaczniki dla komunikacji z AI ---
INTENT_SCHEDULE_MARKER = "[INTENT_SCHEDULE]"
SLOT_ISO_MARKER_PREFIX = "[SLOT_ISO:"
SLOT_ISO_MARKER_SUFFIX = "]"

# --- Ustawienia Modelu Gemini ---
# ZMIANA: Obniżona temperatura dla propozycji terminu
GENERATION_CONFIG_PROPOSAL = GenerationConfig(
    temperature=0.05, # Bardzo niska temperatura dla większej determinizmu
    top_p=0.95,
    top_k=40,
    max_output_tokens=512,
)

# Konfiguracja dla interpretacji feedbacku (deterministyczna)
GENERATION_CONFIG_FEEDBACK = GenerationConfig(
    temperature=0.0,
    top_p=0.95,
    top_k=40,
    max_output_tokens=128,
)

# Konfiguracja dla ogólnej rozmowy
GENERATION_CONFIG_DEFAULT = GenerationConfig(
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

# --- Inicjalizacja Zmiennych Globalnych dla Kalendarza ---
_calendar_service = None
_tz = None

# --- Lista Polskich Dni Tygodnia ---
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
# === FUNKCJE POMOCNICZE ==============================================
# =====================================================================

# Funkcje ensure_dir, get_user_profile, load_history, save_history,
# _get_timezone, get_calendar_service, parse_event_time,
# get_free_time_ranges, is_slot_actually_free, book_appointment,
# format_ranges_for_ai, format_slot_for_user
# (Te funkcje pozostają bez zmian w stosunku do poprzedniej pełnej wersji kodu)
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
            # Dodatkowe logowanie błędu tokena
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
            logging.warning(f"    Zmieniono nazwę uszkodzonego pliku historii na: {filepath}.error_*")
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
                logging.info(f"    Usunięto plik tymczasowy: {temp_filepath}.")
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
    """
    Pobiera listę wolnych zakresów czasowych z kalendarza, uwzględniając godziny pracy.
    Zwraca listę słowników: [{'start': datetime, 'end': datetime}]
    """
    service = get_calendar_service()
    tz = _get_timezone()
    if not service:
        logging.error("Błąd: Usługa kalendarza niedostępna w get_free_time_ranges.")
        return []

    if start_datetime.tzinfo is None: start_datetime = tz.localize(start_datetime)
    else: start_datetime = start_datetime.astimezone(tz)
    if end_datetime.tzinfo is None: end_datetime = tz.localize(end_datetime)
    else: end_datetime = end_datetime.astimezone(tz)

    now = datetime.datetime.now(tz)
    start_datetime = max(start_datetime, now)
    if start_datetime >= end_datetime:
        logging.info("Zakres wyszukiwania wolnych terminów jest nieprawidłowy (start >= end).")
        return []

    logging.info(f"Szukanie wolnych zakresów w kalendarzu '{calendar_id}'")
    logging.info(f"Zakres: od {start_datetime:%Y-%m-%d %H:%M %Z} do {end_datetime:%Y-%m-%d %H:%M %Z}")

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
                logging.warning(f"Ostrz.: Nie udało się sparsować zajętego czasu: {busy_slot}, błąd: {e}")

    except HttpError as error:
        logging.error(f'Błąd API Google Calendar (Freebusy): {error}', exc_info=True)
        return []
    except Exception as e:
        logging.error(f"Nieoczekiwany błąd podczas zapytania Freebusy: {e}", exc_info=True)
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

    logging.info(f"Znaleziono {len(final_free_slots)} wolnych zakresów czasowych (po filtrze godzin pracy i zaokrągleniu).")
    return final_free_slots

def is_slot_actually_free(start_time, calendar_id):
    """Weryfikuje w czasie rzeczywistym, czy konkretny slot wizyty jest nadal wolny."""
    service = get_calendar_service(); tz = _get_timezone()
    if not service:
        logging.error("Błąd: Usługa kalendarza niedostępna do weryfikacji slotu.")
        return False

    if start_time.tzinfo is None: start_time = tz.localize(start_time)
    else: start_time = start_time.astimezone(tz)
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
                if max(start_time, busy_start) < min(end_time, busy_end):
                    logging.warning(f"Weryfikacja: Slot {start_time:%Y-%m-%d %H:%M} jest ZAJĘTY (kolizja z {busy_start:%H:%M}-{busy_end:%H:%M}).")
                    return False
            logging.info(f"Weryfikacja: Slot {start_time:%Y-%m-%d %H:%M} jest POTWIERDZONY jako wolny (zajętości nie kolidują).")
            return True

    except HttpError as error:
        logging.error(f'Błąd API Google Calendar (Freebusy) podczas weryfikacji slotu: {error}', exc_info=True)
        return False
    except Exception as e:
        logging.error(f"Nieoczekiwany błąd podczas weryfikacji slotu przez Freebusy: {e}", exc_info=True)
        return False

def book_appointment(calendar_id, start_time, end_time, summary="Rezerwacja FB", description="", user_name=""):
    """Rezerwuje termin w Kalendarzu Google."""
    service = get_calendar_service(); tz = _get_timezone()
    if not service:
        return False, "Błąd: Brak połączenia z usługą kalendarza. Nie można zarezerwować terminu."

    if start_time.tzinfo is None: start_time = tz.localize(start_time)
    else: start_time = start_time.astimezone(tz)
    if end_time.tzinfo is None: end_time = tz.localize(end_time)
    else: end_time = end_time.astimezone(tz)

    event_summary = summary
    if user_name: event_summary += f" - {user_name}"

    event = {
        'summary': event_summary, 'description': description,
        'start': {'dateTime': start_time.isoformat(), 'timeZone': CALENDAR_TIMEZONE,},
        'end': {'dateTime': end_time.isoformat(), 'timeZone': CALENDAR_TIMEZONE,},
        'reminders': {'useDefault': False, 'overrides': [{'method': 'popup', 'minutes': 60},],},
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
            if error_message: error_details += f" - {error_message}"
        except: pass
        logging.error(f"Błąd API Google Calendar podczas rezerwacji: {error}, Szczegóły: {error_details}", exc_info=True)
        if error.resp.status == 409: return False, "Niestety, wygląda na to, że ten termin został właśnie zajęty. Spróbujmy znaleźć inny."
        elif error.resp.status == 403: return False, "Wystąpił problem z uprawnieniami do zapisu w kalendarzu. Skontaktuj się z administratorem."
        elif error.resp.status == 404: return False, f"Nie znaleziono kalendarza docelowego ('{calendar_id}'). Skontaktuj się z administratorem."
        elif error.resp.status == 400: return False, f"Wystąpił błąd danych podczas próby rezerwacji. Sprawdź poprawność konfiguracji. ({error_details})"
        else: return False, f"Wystąpił nieoczekiwany błąd ({error.resp.status}) podczas rezerwacji terminu w kalendarzu."
    except Exception as e:
        logging.error(f"Nieoczekiwany błąd Python podczas rezerwacji: {e}", exc_info=True)
        return False, "Wystąpił wewnętrzny błąd systemu podczas próby rezerwacji terminu. Przepraszam za kłopot."

def format_ranges_for_ai(ranges):
    """Formatuje listę zakresów czasowych na czytelny tekst dla AI."""
    if not ranges: return "Brak dostępnych zakresów czasowych do zaproponowania."
    ranges_by_date = defaultdict(list); tz = _get_timezone()
    for r in ranges:
        range_date = r['start'].date()
        start_time = r['start'].astimezone(tz); end_time = r['end'].astimezone(tz)
        ranges_by_date[range_date].append({'start_time': start_time.strftime('%H:%M'), 'end_time': end_time.strftime('%H:%M')})
    formatted_lines = [
        f"Poniżej znajdują się dostępne ZAKRESY czasowe, w których można umówić wizytę (czas trwania: {APPOINTMENT_DURATION_MINUTES} minut).",
        "Twoim zadaniem jest wybrać JEDEN zakres, a następnie wygenerować z niego DOKŁADNY czas rozpoczęcia wizyty (np. 16:00, 17:30), biorąc pod uwagę preferencje z historii rozmowy.",
        "Pamiętaj, aby wygenerowany czas + czas trwania wizyty mieścił się w wybranym zakresie.",
        "Dołącz wygenerowany czas w formacie ISO w znaczniku [SLOT_ISO:...].",
        "--- Dostępne Zakresy ---"]
    dates_added = 0; max_dates_to_show = 7
    for d in sorted(ranges_by_date.keys()):
        day_name = POLISH_WEEKDAYS[d.weekday()]; date_str = d.strftime('%d.%m.%Y')
        time_ranges_str = '; '.join(f"{tr['start_time']}-{tr['end_time']}" for tr in sorted(ranges_by_date[d], key=lambda x: x['start_time']))
        if time_ranges_str:
            formatted_lines.append(f"- {day_name}, {date_str}: {time_ranges_str}")
            dates_added += 1
            if dates_added >= max_dates_to_show:
                formatted_lines.append("- ... (i potencjalnie więcej w kolejnych dniach)")
                break
    if dates_added == 0: return "Brak dostępnych zakresów czasowych w godzinach pracy."
    return "\n".join(formatted_lines)

def format_slot_for_user(slot_start):
    """Formatuje pojedynczy slot (datetime) na czytelny tekst dla użytkownika."""
    if not isinstance(slot_start, datetime.datetime):
        logging.warning(f"Próba formatowania niepoprawnego typu slotu: {type(slot_start)}")
        return "[Błąd formatowania daty]"
    try:
        tz = _get_timezone()
        if slot_start.tzinfo is None: slot_start = tz.localize(slot_start)
        else: slot_start = slot_start.astimezone(tz)
        day_index = slot_start.weekday(); day_name = POLISH_WEEKDAYS[day_index]; hour_str = str(slot_start.hour)
        return f"{day_name}, {slot_start.strftime(f'%d.%m.%Y o {hour_str}:%M')}"
    except Exception as e:
        logging.error(f"Błąd formatowania slotu {slot_start}: {e}", exc_info=True)
        return slot_start.isoformat()

# --- Inicjalizacja Vertex AI ---
gemini_model = None
try:
    logging.info(f"Inicjalizowanie Vertex AI: Projekt={PROJECT_ID}, Lokalizacja={LOCATION}")
    vertexai.init(project=PROJECT_ID, location=LOCATION)
    logging.info("Inicjalizacja Vertex AI zakończona pomyślnie.")
    logging.info(f"Ładowanie modelu: {MODEL_ID}")
    gemini_model = GenerativeModel(MODEL_ID)
    logging.info("Model Vertex AI załadowany pomyślnie.")
except Exception as e:
    logging.critical(f"KRYTYCZNY BŁĄD inicjalizacji Vertex AI lub ładowania modelu: {e}", exc_info=True)

# --- Funkcje wysyłania wiadomości FB ---
def _send_typing_on(recipient_id):
    """Wysyła wskaźnik 'pisania' do użytkownika."""
    if not PAGE_ACCESS_TOKEN or len(PAGE_ACCESS_TOKEN) < 50 or not ENABLE_TYPING_DELAY: return
    logging.debug(f"[{recipient_id}] Wysyłanie 'typing_on'")
    params = {"access_token": PAGE_ACCESS_TOKEN}
    payload = {"recipient": {"id": recipient_id}, "sender_action": "typing_on"}
    try: requests.post(FACEBOOK_GRAPH_API_URL, params=params, json=payload, timeout=5)
    except requests.exceptions.RequestException as e:
        logging.warning(f"[{recipient_id}] Błąd podczas wysyłania 'typing_on': {e}")

def _send_single_message(recipient_id, message_text):
    """Wysyła pojedynczy fragment wiadomości przez Facebook Graph API."""
    logging.info(f"--- Wysyłanie fragmentu do {recipient_id} (długość: {len(message_text)}) ---")
    if not PAGE_ACCESS_TOKEN or len(PAGE_ACCESS_TOKEN) < 50:
        logging.error(f"!!! [{recipient_id}] Brak lub nieprawidłowy PAGE_ACCESS_TOKEN. Wiadomość NIE WYSŁANA.")
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
            # Dodatkowe logowanie błędu tokena
            if fb_error.get('code') == 190:
                 logging.error("!!! POTWIERDZONO BŁĄD TOKENA DOSTĘPU (code 190). Sprawdź FB_PAGE_ACCESS_TOKEN! !!!")
            return False
        logging.debug(f"[{recipient_id}] Fragment wysłany pomyślnie. Response: {response_json}")
        return True
    except requests.exceptions.Timeout:
        logging.error(f"!!! BŁĄD TIMEOUT podczas wysyłania do {recipient_id} !!!")
        return False
    except requests.exceptions.HTTPError as e:
        logging.error(f"!!! BŁĄD HTTP {e.response.status_code} podczas wysyłania do {recipient_id}: {e} !!!")
        if e.response is not None:
            try: logging.error(f"Odpowiedź FB (błąd HTTP): {e.response.json()}")
            except json.JSONDecodeError: logging.error(f"Odpowiedź FB (błąd HTTP, nie JSON): {e.response.text}")
        return False
    except requests.exceptions.RequestException as e:
        logging.error(f"!!! BŁĄD sieciowy podczas wysyłania do {recipient_id}: {e} !!!")
        return False
    except Exception as e:
        logging.error(f"!!! Nieoczekiwany BŁĄD podczas wysyłania do {recipient_id}: {e} !!!", exc_info=True)
        return False

def send_message(recipient_id, full_message_text):
    """Wysyła wiadomość do użytkownika, dzieląc ją na fragmenty, jeśli jest za długa."""
    if not full_message_text or not isinstance(full_message_text, str) or not full_message_text.strip():
        logging.warning(f"[{recipient_id}] Pominięto próbę wysłania pustej wiadomości.")
        return
    message_len = len(full_message_text)
    logging.info(f"[{recipient_id}] Przygotowanie wiadomości do wysłania (długość: {message_len}).")
    if ENABLE_TYPING_DELAY:
        estimated_typing_duration = min(MAX_TYPING_DELAY_SECONDS, max(MIN_TYPING_DELAY_SECONDS, message_len / TYPING_CHARS_PER_SECOND))
        logging.debug(f"[{recipient_id}] Szacowany czas pisania: {estimated_typing_duration:.2f}s")
        _send_typing_on(recipient_id)
        time.sleep(estimated_typing_duration)
    chunks = []
    if message_len <= MESSAGE_CHAR_LIMIT: chunks.append(full_message_text)
    else:
        logging.info(f"[{recipient_id}] Wiadomość za długa ({message_len} > {MESSAGE_CHAR_LIMIT}). Dzielenie na fragmenty...")
        remaining_text = full_message_text
        while remaining_text:
            if len(remaining_text) <= MESSAGE_CHAR_LIMIT: chunks.append(remaining_text.strip()); break
            split_index = -1
            possible_delimiters = ['\n\n', '\n', '. ', '! ', '? ', ' ']
            for delimiter in possible_delimiters:
                search_limit = MESSAGE_CHAR_LIMIT - len(delimiter) + 1
                temp_index = remaining_text.rfind(delimiter, 0, search_limit)
                if temp_index != -1: split_index = temp_index + len(delimiter); break
            if split_index == -1: split_index = MESSAGE_CHAR_LIMIT
            chunk = remaining_text[:split_index].strip()
            if chunk: chunks.append(chunk)
            remaining_text = remaining_text[split_index:].strip()
        logging.info(f"[{recipient_id}] Podzielono wiadomość na {len(chunks)} fragmentów.")
    num_chunks = len(chunks); send_success_count = 0
    for i, chunk in enumerate(chunks):
        logging.debug(f"[{recipient_id}] Wysyłanie fragmentu {i+1}/{num_chunks} (długość: {len(chunk)})...")
        if not _send_single_message(recipient_id, chunk):
            logging.error(f"!!! [{recipient_id}] Błąd podczas wysyłania fragmentu {i+1}. Anulowano wysyłanie reszty. !!!")
            break
        send_success_count += 1
        if num_chunks > 1 and i < num_chunks - 1:
            logging.debug(f"[{recipient_id}] Oczekiwanie {MESSAGE_DELAY_SECONDS}s przed kolejnym fragmentem...")
            if ENABLE_TYPING_DELAY:
                estimated_typing_duration = min(MAX_TYPING_DELAY_SECONDS, max(MIN_TYPING_DELAY_SECONDS, len(chunks[i+1]) / TYPING_CHARS_PER_SECOND)) * 0.5
                _send_typing_on(recipient_id)
                time.sleep(estimated_typing_duration + MESSAGE_DELAY_SECONDS * 0.5)
                time.sleep(MESSAGE_DELAY_SECONDS * 0.5)
            else: time.sleep(MESSAGE_DELAY_SECONDS)
    logging.info(f"--- [{recipient_id}] Zakończono wysyłanie wiadomości. Wysłano {send_success_count}/{num_chunks} fragmentów. ---")

def _simulate_typing(recipient_id, duration_seconds):
    """Wysyła 'typing_on' i czeka przez określony czas."""
    if ENABLE_TYPING_DELAY and duration_seconds > 0:
        _send_typing_on(recipient_id)
        time.sleep(min(duration_seconds, MAX_TYPING_DELAY_SECONDS))

# --- Ogólna funkcja do wywoływania API Gemini ---
def _call_gemini(user_psid, prompt_history, generation_config, task_name, max_retries=3):
    """Wywołuje API Gemini z podaną historią i konfiguracją."""
    if not gemini_model:
        logging.error(f"!!! [{user_psid}] Model Gemini ({task_name}) niezaładowany. Nie można wywołać API.")
        return None
    if not isinstance(prompt_history, list) or not all(isinstance(item, Content) for item in prompt_history):
        logging.error(f"!!! [{user_psid}] Nieprawidłowy format promptu dla Gemini ({task_name}). Oczekiwano listy Content.")
        return None
    logging.info(f"[{user_psid}] Wywołanie Gemini dla zadania: {task_name} (Prompt: {len(prompt_history)} wiadomości)")
    attempt = 0
    while attempt < max_retries:
        attempt += 1
        logging.debug(f"    Próba {attempt}/{max_retries} wywołania Gemini ({task_name})...")
        try:
            _simulate_typing(user_psid, MIN_TYPING_DELAY_SECONDS)
            response = gemini_model.generate_content(prompt_history, generation_config=generation_config, safety_settings=SAFETY_SETTINGS)
            if response and response.candidates:
                finish_reason = response.candidates[0].finish_reason
                if finish_reason != 1: # 1 = STOP
                    safety_ratings = response.candidates[0].safety_ratings
                    logging.warning(f"[{user_psid}] Odpowiedź Gemini ({task_name}) ZABLOKOWANA lub NIEDOKOŃCZONA! Powód: {finish_reason}. Oceny bezpieczeństwa: {safety_ratings}")
                    if attempt < max_retries:
                        logging.warning(f"    Ponawianie próby ({attempt}/{max_retries}) po blokadzie bezpieczeństwa...")
                        time.sleep(1 * attempt); continue
                    else:
                         logging.error(f"!!! [{user_psid}] Gemini ({task_name}) - nie udało się po blokadzie bezpieczeństwa po {max_retries} próbach.")
                         return "Przepraszam, wystąpił problem z przetworzeniem Twojej wiadomości z powodu zasad bezpieczeństwa."
                if response.candidates[0].content and response.candidates[0].content.parts:
                    generated_text = "".join(part.text for part in response.candidates[0].content.parts if hasattr(part, 'text'))
                    logging.info(f"[{user_psid}] Gemini ({task_name}) zwróciło odpowiedź (dł: {len(generated_text)}).")
                    return generated_text.strip()
                else:
                    logging.warning(f"[{user_psid}] Gemini ({task_name}) zwróciło kandydata, ale bez treści (puste 'parts'). Response: {response}")
            else:
                 prompt_feedback = response.prompt_feedback if hasattr(response, 'prompt_feedback') else 'Brak prompt_feedback'
                 logging.error(f"!!! BŁĄD [{user_psid}] Gemini ({task_name}) - Brak kandydatów w odpowiedzi. Prompt feedback: {prompt_feedback}. Pełna odpowiedź: {response}")
        except HttpError as http_err:
             logging.error(f"!!! BŁĄD HTTP ({http_err.resp.status}) [{user_psid}] Gemini ({task_name}) - Próba {attempt}/{max_retries}: {http_err}", exc_info=True)
             if http_err.resp.status in [429, 500, 503] and attempt < max_retries:
                  sleep_time = (2 ** attempt) + (random.random() * 0.1)
                  logging.warning(f"    Oczekiwanie {sleep_time:.2f}s przed ponowieniem próby...")
                  time.sleep(sleep_time); continue
             else: break
        except Exception as e:
             logging.error(f"!!! BŁĄD [{user_psid}] Gemini ({task_name}) - Nieoczekiwany błąd API (Próba {attempt}/{max_retries}): {e}", exc_info=True)
        if attempt < max_retries:
             logging.warning(f"    Oczekiwanie przed ponowieniem próby ({attempt+1}/{max_retries})...")
             time.sleep(1.5 * attempt)
    logging.error(f"!!! KRYTYCZNY BŁĄD [{user_psid}] Gemini ({task_name}) - Nie udało się uzyskać poprawnej odpowiedzi po {max_retries} próbach.")
    return None


# --- INSTRUKCJA SYSTEMOWA (dla AI proponującego termin) ---
# ZMIANA: Wzmocniona instrukcja, aby NIE zadawać pytań
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
# ---------------------------------------------------------------------


# --- INSTRUKCJA SYSTEMOWA (dla AI interpretującego feedback) ---
# Bez zmian
SYSTEM_INSTRUCTION_TEXT_FEEDBACK = """Jesteś asystentem AI analizującym odpowiedź użytkownika na propozycję terminu.

**Kontekst:** Zaproponowano użytkownikowi termin wizyty.
**Ostatnia propozycja:** "{last_proposal_text}"
**Odpowiedź użytkownika:** "{user_feedback}"

**Twoje zadanie:** Przeanalizuj odpowiedź użytkownika i zwróć **DOKŁADNIE JEDEN** z poniższych znaczników, który najlepiej opisuje intencję użytkownika:

*   `[ACCEPT]`: Użytkownik akceptuje zaproponowany termin (np. "tak", "ok", "pasuje", "zgoda", "może być").
*   `[REJECT_FIND_NEXT PREFERENCE='any']`: Użytkownik odrzuca termin i nie podaje konkretnych preferencji co do następnego (np. "nie pasuje", "inny termin proszę", "daj coś innego", "nie mogę").
*   `[REJECT_FIND_NEXT PREFERENCE='later']`: Użytkownik odrzuca termin, sugerując, że jest za wcześnie lub woli późniejszą godzinę/dzień (np. "za wcześnie", "wolę później", "czy jest coś po 18?", "dopiero wieczorem mogę").
*   `[REJECT_FIND_NEXT PREFERENCE='earlier']`: Użytkownik odrzuca termin, sugerując, że jest za późno lub woli wcześniejszą godzinę (np. "za późno", "coś wcześniej?", "czy jest coś przed 16?").
*   `[REJECT_FIND_NEXT PREFERENCE='afternoon']`: Użytkownik odrzuca termin, preferując godziny popołudniowe (np. "rano mi nie pasuje", "tylko po południu", "popołudniu bym wolał").
*   `[REJECT_FIND_NEXT PREFERENCE='next_day']`: Użytkownik odrzuca termin, prosząc o termin w innym, niesprecyzowanym dniu (np. "nie dzisiaj", "jutro?", "w inny dzień").
*   `[REJECT_FIND_NEXT PREFERENCE='specific_day' DAY='NAZWA_DNIA']`: Użytkownik odrzuca termin, wskazując na preferowany konkretny dzień tygodnia (np. "pasuje mi tylko środa", "w piątek mogę"). Wyodrębnij **polską nazwę dnia** (np. 'Środa', 'Piątek').
*   `[REJECT_FIND_NEXT PREFERENCE='specific_hour' HOUR='GODZINA']`: Użytkownik odrzuca termin, wskazując na preferowaną konkretną godzinę (np. "tylko o 18:00", "czy jest coś na 17?"). Wyodrębnij **tylko pełną godzinę** jako cyfrę (np. '18', '17').
*   `[REJECT_FIND_NEXT PREFERENCE='specific_datetime' DAY='NAZWA_DNIA' HOUR='GODZINA']`: Użytkownik odrzuca termin, podając zarówno preferowany dzień, jak i godzinę (np. "środa o 17", "czy w piątek o 16 jest wolne?"). Wyodrębnij **polską nazwę dnia** i **pełną godzinę**.
*   `[REJECT_FIND_NEXT PREFERENCE='specific_day_later' DAY='NAZWA_DNIA']`: Użytkownik odrzuca termin, wskazując preferowany dzień, ale sugerując późniejszą porę (np. "środa, ale później", "w piątek, ale dopiero wieczorem"). Wyodrębnij **polską nazwę dnia**.
*   `[REJECT_FIND_NEXT PREFERENCE='specific_day_earlier' DAY='NAZWA_DNIA']`: Użytkownik odrzuca termin, wskazując preferowany dzień, ale sugerując wcześniejszą porę (np. "środa, ale wcześniej", "w piątek, ale rano"). Wyodrębnij **polską nazwę dnia**.
*   `[CLARIFY]`: Odpowiedź użytkownika jest niejasna, niejednoznaczna w kontekście propozycji terminu, lub jest pytaniem niezwiązanym bezpośrednio z akceptacją/odrzuceniem (np. "ile to kosztuje?", "a co będziemy robić?", "nie wiem jeszcze").

**Ważne:**
*   Zwróć **tylko jeden** znacznik.
*   Jeśli użytkownik podaje więcej informacji, wybierz najbardziej szczegółowy pasujący znacznik (np. `specific_datetime` jest lepszy niż `specific_day`).
*   Dokładnie wyodrębnij nazwy dni (zgodne z `POLISH_WEEKDAYS`) i godziny (tylko cyfra).
*   Jeśli odpowiedź jest całkowicie niezwiązana z terminem, użyj `[CLARIFY]`.
"""
# ---------------------------------------------------------------------


# --- INSTRUKCJA SYSTEMOWA (dla AI prowadzącego ogólną rozmowę) ---
# Bez zmian
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
# ---------------------------------------------------------------------


# --- Funkcja interakcji z Gemini (proponowanie slotu) ---
def get_gemini_slot_proposal(user_psid, history_for_proposal_ai, available_ranges):
    """
    Pobiera od AI propozycję konkretnego terminu na podstawie historii i dostępnych zakresów.
    Używa wzmocnionej instrukcji i niskiej temperatury.
    """
    if not gemini_model:
        logging.error(f"!!! [{user_psid}] Model Gemini niezaładowany! Nie można wygenerować propozycji slotu.")
        return None, None
    if not available_ranges:
        logging.warning(f"[{user_psid}]: Brak dostępnych zakresów do przekazania AI do propozycji.")
        return "Niestety, w tym momencie nie widzę żadnych dostępnych zakresów czasowych.", None

    ranges_text = format_ranges_for_ai(available_ranges)
    # Użyj wzmocnionej instrukcji
    system_instruction = SYSTEM_INSTRUCTION_TEXT_PROPOSE.format(available_ranges_text=ranges_text)
    initial_prompt = [
        Content(role="user", parts=[Part.from_text(system_instruction)]),
        Content(role="model", parts=[Part.from_text("Rozumiem. Moim zadaniem jest teraz wybrać JEDEN termin z podanych zakresów, zaproponować go i MUSZĘ dodać znacznik [SLOT_ISO:...]. Nie będę zadawać pytań.")]) # Wzmocniona odpowiedź modelu
    ]
    full_prompt = initial_prompt + history_for_proposal_ai

    max_prompt_messages = (MAX_HISTORY_TURNS * 2) + 2
    while len(full_prompt) > max_prompt_messages:
        full_prompt.pop(2)
        if len(full_prompt) > 2: full_prompt.pop(2)

    # Użyj konfiguracji z niską temperaturą
    generated_text = _call_gemini(user_psid, full_prompt, GENERATION_CONFIG_PROPOSAL, "Slot Proposal Strict")

    if not generated_text:
        logging.error(f"!!! [{user_psid}] Nie udało się uzyskać odpowiedzi od Gemini dla propozycji slotu (Strict).")
        return "Przepraszam, mam chwilowy problem z systemem proponowania terminów.", None

    iso_match = re.search(rf"{re.escape(SLOT_ISO_MARKER_PREFIX)}(.*?){re.escape(SLOT_ISO_MARKER_SUFFIX)}", generated_text)
    if iso_match:
        extracted_iso = iso_match.group(1).strip()
        text_for_user = re.sub(rf"{re.escape(SLOT_ISO_MARKER_PREFIX)}.*?{re.escape(SLOT_ISO_MARKER_SUFFIX)}", "", generated_text).strip()
        text_for_user = re.sub(r'\s+', ' ', text_for_user).strip()
        logging.info(f"[{user_psid}] AI (Strict) wygenerowało propozycję. ISO: {extracted_iso}. Tekst: '{text_for_user}'")
        try:
            tz = _get_timezone()
            proposed_start = datetime.datetime.fromisoformat(extracted_iso).astimezone(tz)
            proposed_end = proposed_start + datetime.timedelta(minutes=APPOINTMENT_DURATION_MINUTES)
            is_within_provided_ranges = False
            for r in available_ranges:
                if r['start'] <= proposed_start and proposed_end <= r['end']:
                    is_within_provided_ranges = True; break
            if not is_within_provided_ranges:
                 logging.error(f"!!! BŁĄD Walidacji AI (Strict) [{user_psid}]: Wygenerowany ISO '{extracted_iso}' (start: {proposed_start:%H:%M}) nie mieści się w żadnym z dostępnych zakresów!")
                 return "Przepraszam, wystąpił błąd podczas wybierania terminu z dostępnych opcji. Spróbujmy jeszcze raz.", None
            if is_slot_actually_free(proposed_start, TARGET_CALENDAR_ID):
                 return text_for_user, extracted_iso
            else:
                 logging.warning(f"!!! [{user_psid}]: Wygenerowany przez AI (Strict) slot {extracted_iso} okazał się ZAJĘTY po weryfikacji! Szukanie nowego.")
                 return "Wygląda na to, że proponowany przed chwilą termin właśnie się zajął. Szukam kolejnej opcji...", None
        except ValueError:
            logging.error(f"!!! BŁĄD AI (Strict) [{user_psid}]: Wygenerowany ciąg '{extracted_iso}' nie jest poprawnym formatem ISO 8601!")
            return "Przepraszam, wystąpił błąd podczas przetwarzania proponowanego terminu. Spróbujmy ponownie.", None
        except Exception as val_err:
            logging.error(f"!!! BŁĄD Walidacji AI (Strict) [{user_psid}]: Nieoczekiwany błąd podczas walidacji slotu {extracted_iso}: {val_err}", exc_info=True)
            return "Przepraszam, wystąpił wewnętrzny błąd systemu podczas weryfikacji terminu.", None
    else:
        # ZMIANA: Jeśli AI nadal nie dało znacznika, zwracamy tylko tekst (jeśli jest) i logujemy błąd krytyczny
        logging.critical(f"!!! KRYTYCZNY BŁĄD AI (Strict) [{user_psid}]: Brak znacznika ISO mimo wzmocnionej instrukcji! Odpowiedź: '{generated_text}'")
        # Zwróć sam tekst, jeśli jest, ale bez ISO, aby przerwać pętlę propozycji
        clean_text = generated_text.strip()
        if clean_text:
             return clean_text, None
        else:
             # Jeśli odpowiedź jest kompletnie pusta
             return "Przepraszam, wystąpił nieoczekiwany błąd i nie mogę teraz zaproponować terminu.", None


# --- Funkcja interakcji z Gemini (interpretacja feedbacku) ---
# Bez zmian
def get_gemini_feedback_decision(user_psid, user_feedback_text, history_for_feedback_ai, last_proposed_slot_text):
     """
     Prosi AI o zinterpretowanie odpowiedzi użytkownika na propozycję terminu i zwrócenie znacznika decyzji.
     """
     if not gemini_model:
        logging.error(f"!!! [{user_psid}] Model Gemini niezaładowany! Nie można zinterpretować feedbacku.")
        return "[CLARIFY]"
     instruction = SYSTEM_INSTRUCTION_TEXT_FEEDBACK.format(last_proposal_text=last_proposed_slot_text, user_feedback=user_feedback_text)
     prompt = [Content(role="user", parts=[Part.from_text(instruction)])]
     max_hist_messages = (MAX_HISTORY_TURNS - 1) * 2
     if len(history_for_feedback_ai) > max_hist_messages: prompt.extend(history_for_feedback_ai[-max_hist_messages:])
     else: prompt.extend(history_for_feedback_ai)
     prompt.append(Content(role="user", parts=[Part.from_text(user_feedback_text)]))
     decision_tag = _call_gemini(user_psid, prompt, GENERATION_CONFIG_FEEDBACK, "Feedback Interpretation")
     if not decision_tag:
         logging.error(f"!!! [{user_psid}] Nie udało się uzyskać odpowiedzi od Gemini dla interpretacji feedbacku.")
         return "[CLARIFY]"
     if not (decision_tag.startswith("[") and decision_tag.endswith("]")):
          logging.warning(f"Ostrz. [{user_psid}]: AI (Feedback) nie zwróciło poprawnego formatu znacznika: '{decision_tag}'. Traktuję jako CLARIFY.")
          return "[CLARIFY]"
     try:
         tag_parts = decision_tag[1:-1].split(' ')
         tag_name = tag_parts[0]
         if tag_name in ['ACCEPT', 'CLARIFY']: pass
         elif tag_name == 'REJECT_FIND_NEXT':
              params = {}
              for part in tag_parts[1:]:
                   if '=' in part: key, value = part.split('=', 1); params[key.upper()] = value.strip("'")
              preference = params.get('PREFERENCE')
              if not preference: raise ValueError("Brak PREFERENCE")
              if preference in ['specific_day', 'specific_datetime', 'specific_day_later', 'specific_day_earlier']:
                  day = params.get('DAY')
                  if not day or day.capitalize() not in POLISH_WEEKDAYS: raise ValueError(f"Nieprawidłowy DAY='{day}' dla {preference}")
                  decision_tag = decision_tag.replace(f"DAY='{day}'", f"DAY='{day.capitalize()}'")
              if preference in ['specific_hour', 'specific_datetime']:
                  hour_str = params.get('HOUR')
                  if not hour_str or not hour_str.isdigit() or not (0 <= int(hour_str) <= 23): raise ValueError(f"Nieprawidłowy HOUR='{hour_str}' dla {preference}")
         else: raise ValueError(f"Nieznany typ znacznika: {tag_name}")
         logging.info(f"[{user_psid}] Zwalidowana decyzja AI (Feedback): {decision_tag}")
         return decision_tag
     except ValueError as e:
         logging.warning(f"Ostrz. [{user_psid}]: Błąd walidacji znacznika feedbacku '{decision_tag}': {e}. Traktuję jako CLARIFY.")
         return "[CLARIFY]"
     except Exception as e_val:
          logging.error(f"!!! [{user_psid}]: Nieoczekiwany błąd podczas walidacji znacznika feedbacku '{decision_tag}': {e_val}", exc_info=True)
          return "[CLARIFY]"

# --- Funkcja interakcji z Gemini (ogólna rozmowa) ---
# Bez zmian
def get_gemini_general_response(user_psid, current_user_message_text, history_for_general_ai):
    """
    Prowadzi ogólną rozmowę z AI, zwraca odpowiedź tekstową.
    """
    if not gemini_model:
        logging.error(f"!!! [{user_psid}] Model Gemini niezaładowany! Nie można prowadzić ogólnej rozmowy.")
        return "Przepraszam, mam chwilowy problem z systemem i nie mogę teraz odpowiedzieć."
    initial_prompt = [
        Content(role="user", parts=[Part.from_text(SYSTEM_INSTRUCTION_GENERAL)]),
        Content(role="model", parts=[Part.from_text("Rozumiem. Jestem gotów do rozmowy i wykrywania intencji umówienia wizyty.")])]
    full_prompt = initial_prompt + history_for_general_ai
    full_prompt.append(Content(role="user", parts=[Part.from_text(current_user_message_text)]))
    max_prompt_messages = (MAX_HISTORY_TURNS * 2) + 2
    while len(full_prompt) > max_prompt_messages:
        full_prompt.pop(2);
        if len(full_prompt) > 2: full_prompt.pop(2)
    response_text = _call_gemini(user_psid, full_prompt, GENERATION_CONFIG_DEFAULT, "General Conversation")
    if response_text:
        if SLOT_ISO_MARKER_PREFIX in response_text:
             logging.warning(f"[{user_psid}] AI (General) błędnie dodało znacznik ISO. Usuwanie znacznika.")
             response_text = re.sub(rf"{re.escape(SLOT_ISO_MARKER_PREFIX)}.*?{re.escape(SLOT_ISO_MARKER_SUFFIX)}", "", response_text).strip()
        return response_text
    else:
        logging.error(f"!!! [{user_psid}] Nie udało się uzyskać odpowiedzi od Gemini dla ogólnej rozmowy.")
        return "Przepraszam, wystąpił błąd podczas przetwarzania Twojej wiadomości. Czy możesz spróbować ponownie?"


# =====================================================================
# === WEBHOOK HANDLERS ================================================
# =====================================================================

# --- Obsługa Weryfikacji Webhooka (GET) ---
@app.route('/webhook', methods=['GET'])
def webhook_verification():
    """Obsługuje żądanie weryfikacyjne od Facebooka."""
    logging.info("--- Otrzymano żądanie GET /webhook (Weryfikacja) ---")
    hub_mode = request.args.get('hub.mode'); hub_token = request.args.get('hub.verify_token'); hub_challenge = request.args.get('hub.challenge')
    logging.debug(f"Mode: {hub_mode}"); logging.debug(f"Challenge: {hub_challenge}")
    if hub_mode == 'subscribe' and hub_token == VERIFY_TOKEN:
        logging.info("Weryfikacja Webhooka GET zakończona pomyślnie!")
        return Response(hub_challenge, status=200)
    else:
        logging.warning(f"Weryfikacja Webhooka GET NIEUDANA. Otrzymany token: '{hub_token}' (oczekiwano: '{VERIFY_TOKEN}')")
        return Response("Verification failed", status=403)

# --- Główna Obsługa Webhooka (POST) ---
@app.route('/webhook', methods=['POST'])
def webhook_handle():
    """Obsługuje przychodzące zdarzenia (wiadomości, postbacki) od Facebooka."""
    logging.info(f"\n{'='*30} {datetime.datetime.now(_get_timezone()):%Y-%m-%d %H:%M:%S %Z} Otrzymano POST /webhook {'='*30}")
    raw_data = request.data; data = None
    try:
        decoded_data = raw_data.decode('utf-8'); data = json.loads(decoded_data)
        if data and data.get("object") == "page":
            for entry in data.get("entry", []):
                page_id = entry.get("id"); timestamp = entry.get("time"); logging.debug(f"Przetwarzanie wpisu dla strony: {page_id}, czas: {timestamp}")
                for event in entry.get("messaging", []):
                    sender_id = event.get("sender", {}).get("id"); recipient_id = event.get("recipient", {}).get("id")
                    if not sender_id: logging.warning("Pominięto zdarzenie bez ID nadawcy (sender.id)."); continue
                    logging.info(f"--- Przetwarzanie zdarzenia dla PSID: {sender_id} ---")
                    history, context = load_history(sender_id)
                    history_for_gemini = [h for h in history if isinstance(h, Content) and h.role in ('user', 'model')]
                    logging.debug(f"Wczytano {len(history_for_gemini)} wiadomości user/model z historii.")
                    is_context_active = False; last_iso_from_context = None; last_proposal_text_for_feedback = "poprzednio zaproponowany termin"
                    if context.get('type') == 'last_proposal' and context.get('slot_iso'):
                        temp_hist_check, temp_ctx_check = load_history(sender_id)
                        if temp_ctx_check.get('slot_iso') == context.get('slot_iso'):
                            is_context_active = True; last_iso_from_context = context['slot_iso']
                            try: last_dt = datetime.datetime.fromisoformat(last_iso_from_context).astimezone(_get_timezone()); last_proposal_text_for_feedback = format_slot_for_user(last_dt)
                            except Exception as fmt_err: logging.warning(f"Nie udało się sformatować ostatniego ISO '{last_iso_from_context}' na potrzeby promptu feedback: {fmt_err}")
                            logging.info(f"    Aktywny kontekst propozycji: {last_iso_from_context} ({last_proposal_text_for_feedback})")
                        else: logging.info(f"    Wczytany kontekst '{context.get('slot_iso')}' nie jest już aktywny. Reset."); context = {}
                    action = None; msg_result = None; ctx_save = context; model_resp_content = None; user_content = None

                    # === Obsługa wiadomości tekstowych ===
                    if message_data := event.get("message"):
                        if message_data.get("is_echo"): logging.debug(f"    Pominięto echo wiadomości."); continue
                        user_input_text = message_data.get("text", "").strip()
                        if user_input_text:
                            user_content = Content(role="user", parts=[Part.from_text(user_input_text)])
                            logging.info(f"    Otrzymano wiadomość tekstową: '{user_input_text[:100]}{'...' if len(user_input_text)>100 else ''}'")
                            if ENABLE_TYPING_DELAY: time.sleep(MIN_TYPING_DELAY_SECONDS * 0.5)
                            if is_context_active:
                                logging.info("      -> Kontekst aktywny. Pytanie AI (Feedback) o interpretację...")
                                try:
                                    decision = get_gemini_feedback_decision(sender_id, user_input_text, history_for_gemini, last_proposal_text_for_feedback)
                                    if decision == "[ACCEPT]": action = 'book'; logging.info(f"      Decyzja AI: {decision} -> Akcja: Rezerwacja"); ctx_save = None
                                    elif decision.startswith("[REJECT_FIND_NEXT"): action = 'find_and_propose'; logging.info(f"      Decyzja AI: {decision} -> Akcja: Odrzucenie i szukanie"); msg_result = "Rozumiem. W takim razie poszukam innego terminu..."; ctx_save = None
                                    elif decision == "[CLARIFY]": action = 'send_clarification'; logging.info(f"      Decyzja AI: {decision} -> Akcja: Prośba o doprecyzowanie"); msg_result = "Nie jestem pewien, co masz na myśli w kontekście zaproponowanego terminu. Czy możesz doprecyzować, czy go akceptujesz, czy wolisz inny?"; ctx_save = context
                                    else: action = 'send_error'; logging.warning(f"      Niespodziewana decyzja AI (Feedback): {decision}."); msg_result = "Przepraszam, mam problem ze zrozumieniem Twojej odpowiedzi dotyczącej terminu."; ctx_save = None
                                except Exception as feedback_err: logging.error(f"!!! BŁĄD podczas przetwarzania feedbacku przez AI: {feedback_err}", exc_info=True); action = 'send_error'; msg_result = "Wystąpił błąd podczas interpretacji Twojej odpowiedzi."; ctx_save = None
                            else:
                                logging.info("      -> Kontekst nieaktywny. Pytanie AI (General) o odpowiedź...")
                                response = get_gemini_general_response(sender_id, user_input_text, history_for_gemini)
                                if response:
                                    if INTENT_SCHEDULE_MARKER in response: logging.info(f"      AI wykryło intencję umówienia [{INTENT_SCHEDULE_MARKER}]."); action = 'find_and_propose'; initial_resp_text = response.split(INTENT_SCHEDULE_MARKER, 1)[0].strip(); msg_result = initial_resp_text if initial_resp_text else "Dobrze, w takim razie sprawdzę dostępne terminy."; ctx_save = None
                                    else: action = 'send_gemini_response'; msg_result = response; ctx_save = None
                                else: action = 'send_error'; msg_result = "Przepraszam, wystąpił błąd podczas przetwarzania Twojej wiadomości."; ctx_save = None
                        elif attachments := message_data.get("attachments"):
                            att_type = attachments[0].get('type','nieznany'); logging.info(f"      Otrzymano załącznik typu: {att_type}.")
                            user_content = Content(role="user", parts=[Part.from_text(f"[Otrzymano załącznik: {att_type}]")])
                            if att_type == 'image': msg_result = "Dziękuję za obrazek! Niestety, nie potrafię go jeszcze analizować."
                            elif att_type == 'audio': msg_result = "Otrzymałem nagranie głosowe, ale niestety nie mogę go jeszcze odsłuchać."
                            elif att_type == 'sticker': msg_result = "Fajna naklejka! 😉"
                            else: msg_result = "Dziękuję za przesłanie pliku. Obecnie nie obsługuję tego typu załączników."
                            action = 'send_info'; ctx_save = context
                        else:
                            logging.info("      Otrzymano pustą wiadomość.")
                            if is_context_active: action = 'send_clarification'; msg_result = "Przepraszam, nie otrzymałem odpowiedzi. Czy zaproponowany termin pasuje?"; ctx_save = context
                            else: action = None; ctx_save = None

                    # === Obsługa Postback ===
                    elif postback := event.get("postback"):
                        payload = postback.get("payload"); title = postback.get("title", ""); logging.info(f"    Otrzymano postback. Payload: '{payload}', Tytuł: '{title}'")
                        user_content = Content(role="user", parts=[Part.from_text(f"[Kliknięto przycisk: {title} ({payload})]")])
                        if payload == "ACCEPT_SLOT":
                            if is_context_active and last_iso_from_context: logging.info("      Postback: Akceptacja -> Akcja: Rezerwacja"); action = 'book'; msg_result = None; ctx_save = None
                            else: logging.warning("      Postback 'ACCEPT_SLOT', ale brak aktywnego kontekstu."); action = 'send_info'; msg_result = "Wygląda na to, że propozycja terminu wygasła."; ctx_save = None
                        elif payload == "REJECT_SLOT":
                            if is_context_active and last_iso_from_context: logging.info("      Postback: Odrzucenie -> Akcja: Szukanie nowego"); action = 'find_and_propose'; msg_result = "Rozumiem, ten termin nie pasuje. Sprawdzam inne opcje..."; ctx_save = None
                            else: logging.warning("      Postback 'REJECT_SLOT', ale brak aktywnego kontekstu."); action = 'send_info'; msg_result = "Nie widzę aktywnej propozycji do odrzucenia."; ctx_save = None
                        else:
                            logging.warning(f"      Nieznany payload postback: '{payload}'."); simulated_input = f"Użytkownik kliknął przycisk '{title}' (payload: {payload})."
                            response = get_gemini_general_response(sender_id, simulated_input, history_for_gemini)
                            if response:
                                if INTENT_SCHEDULE_MARKER in response: action = 'find_and_propose'; initial_resp_text = response.split(INTENT_SCHEDULE_MARKER, 1)[0].strip(); msg_result = initial_resp_text if initial_resp_text else "Dobrze, sprawdzę terminy."; ctx_save = None
                                else: action = 'send_gemini_response'; msg_result = response; ctx_save = None
                            else: action = 'send_error'; msg_result = "Przepraszam, wystąpił błąd."; ctx_save = None

                    # === Inne zdarzenia ===
                    elif event.get("read"): logging.debug(f"    Potwierdzenie odczytania."); continue
                    elif event.get("delivery"): logging.debug(f"    Potwierdzenie dostarczenia."); continue
                    else: logging.warning(f"    Nieobsługiwany typ zdarzenia: {json.dumps(event)}"); continue

                    # --- WYKONANIE AKCJI ---
                    history_saved_in_this_cycle = False
                    if action == 'book':
                        if last_iso_from_context:
                            try:
                                tz = _get_timezone(); start = datetime.datetime.fromisoformat(last_iso_from_context).astimezone(tz); end = start + datetime.timedelta(minutes=APPOINTMENT_DURATION_MINUTES); prof = get_user_profile(sender_id); name = prof.get('first_name', '') if prof else f"User_{sender_id[-4:]}"
                                desc = f"Rezerwacja przez Bota FB\nPSID: {sender_id}" + (f"\nNazwisko: {prof.get('last_name')}" if prof and prof.get('last_name') else "")
                                ok, booking_msg = book_appointment(TARGET_CALENDAR_ID, start, end, f"Lekcja FB: {name}", desc, name)
                                msg_result = booking_msg;
                                if not ok: ctx_save = None # Reset jeśli błąd rezerwacji
                            except Exception as e: logging.error(f"!!! BŁĄD rezerwacji ISO {last_iso_from_context}: {e}", exc_info=True); msg_result = "Wystąpił krytyczny błąd rezerwacji."; ctx_save = None
                        else: logging.error("!!! KRYTYCZNY BŁĄD LOGIKI: 'book' bez 'last_iso_from_context' !!!"); msg_result = "Błąd systemu rezerwacji."; ctx_save = None
                    elif action == 'find_and_propose':
                        try:
                            tz = _get_timezone(); now = datetime.datetime.now(tz); search_start = now; search_end_date = (search_start + datetime.timedelta(days=MAX_SEARCH_DAYS)).date()
                            search_end = tz.localize(datetime.datetime.combine(search_end_date, datetime.time(WORK_END_HOUR, 0)))
                            logging.info(f"      -> Rozpoczęcie szukania od {search_start:%Y-%m-%d %H:%M} do {search_end:%Y-%m-%d %H:%M}")
                            _simulate_typing(sender_id, MAX_TYPING_DELAY_SECONDS * 0.8)
                            free_ranges = get_free_time_ranges(TARGET_CALENDAR_ID, search_start, search_end)
                            if free_ranges:
                                logging.info(f"      Znaleziono {len(free_ranges)} zakresów. Przekazanie do AI (Proposal Strict)...")
                                history_for_proposal_ai = history_for_gemini + ([user_content] if user_content else [])
                                proposal_text, proposed_iso = get_gemini_slot_proposal(sender_id, history_for_proposal_ai, free_ranges) # Używa wzmocnionej wersji
                                if proposal_text and proposed_iso:
                                    final_proposal_msg = (msg_result + "\n\n" + proposal_text) if msg_result else proposal_text
                                    msg_result = final_proposal_msg
                                    ctx_save = {'role': 'system', 'type': 'last_proposal', 'slot_iso': proposed_iso}
                                # ZMIANA: Obsługa sytuacji, gdy AI zwróciło tekst, ale bez ISO (po wzmocnieniu instrukcji)
                                elif proposal_text and not proposed_iso:
                                     logging.warning(f"      AI (Strict) zwróciło tekst '{proposal_text[:50]}...' ale bez znacznika ISO. Wysyłanie samego tekstu.")
                                     final_proposal_msg = (msg_result + "\n\n" + proposal_text) if msg_result else proposal_text
                                     msg_result = final_proposal_msg
                                     ctx_save = None # Reset kontekstu, bo nie ma czego rezerwować
                                else: # AI nie zwróciło ani tekstu, ani ISO
                                    fail_msg = proposal_text if proposal_text else "Niestety, mam problem ze znalezieniem i zaproponowaniem dogodnego terminu w tym momencie."
                                    msg_result = (msg_result + "\n\n" + fail_msg) if msg_result else fail_msg
                                    ctx_save = None
                            else:
                                logging.warning(f"      Nie znaleziono żadnych wolnych zakresów w kalendarzu.")
                                no_slots_msg = f"Niestety, wygląda na to, że w najbliższym czasie ({MAX_SEARCH_DAYS} dni) nie mam już wolnych terminów w godzinach pracy. Spróbuj ponownie później lub skontaktuj się bezpośrednio."
                                msg_result = (msg_result + "\n\n" + no_slots_msg) if msg_result else no_slots_msg
                                ctx_save = None
                        except Exception as find_err:
                            logging.error(f"!!! BŁĄD ogólny podczas szukania/proponowania terminu: {find_err}", exc_info=True)
                            error_msg = "Wystąpił nieoczekiwany problem podczas wyszukiwania dostępnych terminów."
                            msg_result = (msg_result + "\n\n" + error_msg) if msg_result else error_msg
                            ctx_save = None
                    elif action in ['send_gemini_response', 'send_clarification', 'send_error', 'send_info']:
                        logging.debug(f"      Akcja: {action}. Wiadomość gotowa.")
                        pass

                    # --- WYSYŁANIE I ZAPIS ---
                    if msg_result:
                        send_message(sender_id, msg_result)
                        model_resp_content = Content(role="model", parts=[Part.from_text(msg_result)])
                    elif action: logging.warning(f"    Akcja '{action}' zakończona bez wiadomości do wysłania.")

                    original_context_iso = context.get('slot_iso'); new_context_iso = ctx_save.get('slot_iso') if isinstance(ctx_save, dict) else None
                    should_save = bool(user_content) or bool(model_resp_content) or (original_context_iso != new_context_iso)
                    if should_save:
                        history_to_save = list(history)
                        if user_content: history_to_save.append(user_content)
                        if model_resp_content: history_to_save.append(model_resp_content)
                        logging.debug(f"Przygotowanie do zapisu historii. Nowy kontekst: {ctx_save}")
                        save_history(sender_id, history_to_save, context_to_save=ctx_save)
                        history_saved_in_this_cycle = True
                    else: logging.debug("    Brak zmian - pomijanie zapisu historii.")

            logging.info(f"--- Zakończono przetwarzanie POST batch ---")
            return Response("EVENT_RECEIVED", status=200)
        else:
            logging.warning(f"Otrzymano POST, ale obiekt nie jest 'page' (typ: {data.get('object') if data else 'Brak'}).")
            return Response("OK", status=200)
    except json.JSONDecodeError as e:
        logging.error(f"!!! KRYTYCZNY BŁĄD: Nie udało się sparsować JSON z POST: {e}", exc_info=True)
        logging.error(f"    Początek surowych danych: {raw_data[:500]}...")
        return Response("Invalid JSON payload", status=400)
    except Exception as e:
        logging.critical(f"!!! KRYTYCZNY BŁĄD serwera podczas przetwarzania POST: {e}", exc_info=True)
        return Response("Internal Server Error", status=200)


# =====================================================================
# === URUCHOMIENIE SERWERA ============================================
# =====================================================================

if __name__ == '__main__':
    ensure_dir(HISTORY_DIR)
    log_level = logging.DEBUG if os.environ.get("FLASK_DEBUG", "False").lower() in ("true", "1", "yes") else logging.INFO
    logging.basicConfig(level=log_level, format='%(asctime)s - %(levelname)s - [%(funcName)s:%(lineno)d] - %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
    logging.getLogger('googleapiclient.discovery_cache').setLevel(logging.ERROR)
    logging.getLogger('urllib3.connectionpool').setLevel(logging.WARNING)
    logging.getLogger('werkzeug').setLevel(logging.WARNING)

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
    print(f"    Model AI: {MODEL_ID}")
    if not gemini_model: print("!!! OSTRZEŻENIE: Model Gemini AI NIE został załadowany poprawnie! !!!")
    else: print(f"    Model Gemini AI ({MODEL_ID}): Załadowany (OK)")
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
    
