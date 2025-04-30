# -*- coding: utf-8 -*-

# verify_server.py (wersja z AI GENERUJĄCYM termin z ZAKRESÓW - poprawiona składnia)

from flask import Flask, request, Response
import os
import json
import requests
import time
import vertexai
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
PAGE_ACCESS_TOKEN = os.environ.get("FB_PAGE_ACCESS_TOKEN", "EACNAHFzEhkUBO4ypcoyQfWIgNc0YLZA1aCr9n3BzpvSJLoBTJnv5rWZBmc7HlqF6uUWt1uAp6aDZB8ZAb0RRT45qVIfGnciQX6wBKrZColGARfVLXP5Ic6Ptrj5AUvom4Rt12hyBxcjIJGes76fvdvBhiBZCJ0ZCVfkQMZBZCBatJshSZA8hFuRyKd58b50wkhVCMZCuwZDZD")
PROJECT_ID = os.environ.get("GCP_PROJECT_ID", "linear-booth-450221-k1")
LOCATION = os.environ.get("GCP_LOCATION", "us-central1")
MODEL_ID = os.environ.get("VERTEX_MODEL_ID", "gemini-2.0-flash-001")

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
        print("Ostrzeżenie: Nie można ustawić polskiej lokalizacji dla formatowania dat.")

# --- Znaczniki specjalne dla AI ---
INTENT_SCHEDULE_MARKER = "[INTENT:SCHEDULE]"
SLOT_ISO_MARKER_PREFIX = "[SLOT_ISO:"
SLOT_ISO_MARKER_SUFFIX = "]"

# =====================================================================
# === FUNKCJE POMOCNICZE (Logowanie, Profil, Historia, Kalendarz) =====
# =====================================================================

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

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
    """Pobiera podstawowe informacje o profilu użytkownika z Facebooka."""
    if not PAGE_ACCESS_TOKEN:
        logging.warning(f"[{psid}] Brak tokena. Profil niepobrany.")
        return None
    elif len(PAGE_ACCESS_TOKEN) < 50:
        logging.warning(f"[{psid}] Token za krótki. Profil niepobrany.")
        return None

    USER_PROFILE_API_URL_TEMPLATE = f"https://graph.facebook.com/v19.0/{psid}?fields=first_name,last_name,profile_pic&access_token={PAGE_ACCESS_TOKEN}"
    logging.info(f"--- [{psid}] Pobieranie profilu...")
    try:
        r = requests.get(USER_PROFILE_API_URL_TEMPLATE, timeout=10)
        r.raise_for_status()
        data = r.json()
        if 'error' in data:
            logging.error(f"BŁĄD FB API (profil) {psid}: {data['error']}")
            return None
        profile_data = {
            'first_name': data.get('first_name'),
            'last_name': data.get('last_name'),
            'profile_pic': data.get('profile_pic'),
            'id': data.get('id')
        }
        logging.info(f"--- [{psid}] Pobrany profil: {profile_data.get('first_name')}")
        return profile_data
    except requests.exceptions.Timeout:
        logging.error(f"BŁĄD TIMEOUT profilu {psid}")
        return None
    except requests.exceptions.HTTPError as http_err:
         logging.error(f"BŁĄD HTTP {http_err.response.status_code} profilu {psid}: {http_err}")
         if http_err.response is not None:
            try:
                logging.error(f"Odpowiedź FB (błąd HTTP): {http_err.response.json()}")
            except json.JSONDecodeError:
                logging.error(f"Odpowiedź FB (błąd HTTP, nie JSON): {http_err.response.text}")
         return None
    except requests.exceptions.RequestException as req_err:
        logging.error(f"BŁĄD RequestException profilu {psid}: {req_err}")
        return None
    except Exception as e:
        logging.error(f"Niespodziewany BŁĄD profilu {psid}: {e}", exc_info=True)
        return None

def load_history(user_psid):
    """Wczytuje historię konwersacji i ostatni kontekst systemowy (jeśli istnieje)."""
    filepath = os.path.join(HISTORY_DIR, f"{user_psid}.json")
    history = []
    context = {}
    if not os.path.exists(filepath):
        return history, context
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            history_data = json.load(f)

        if not isinstance(history_data, list):
            logging.error(f"BŁĄD [{user_psid}]: Plik historii nie jest listą.")
            return [], {}

        last_system_entry_index = -1
        for i in range(len(history_data) - 1, -1, -1):
            entry = history_data[i]
            if isinstance(entry, dict) and entry.get('role') == 'system' and entry.get('type') == 'last_proposal':
                last_system_entry_index = i
                break

        for i, msg_data in enumerate(history_data):
            if isinstance(msg_data, dict) and msg_data.get('role') in ('user', 'model') and 'parts' in msg_data:
                text_parts = []
                valid_parts = True
                for part_data in msg_data['parts']:
                    if isinstance(part_data, dict) and 'text' in part_data and isinstance(part_data['text'], str):
                        text_parts.append(Part.from_text(part_data['text']))
                    else:
                        logging.warning(f"Ostrz. [{user_psid}]: Niepoprawna część w historii (idx {i})")
                        valid_parts = False
                        break
                if valid_parts and text_parts:
                    history.append(Content(role=msg_data['role'], parts=text_parts))
                elif valid_parts:
                    logging.warning(f"Ostrz. [{user_psid}]: Puste 'parts' w historii (idx {i})")

            elif i == last_system_entry_index:
                if 'slot_iso' in msg_data:
                    context['last_proposed_slot_iso'] = msg_data['slot_iso']
                    context['message_index_in_file'] = i
                    logging.info(f"[{user_psid}] Odczytano kontekst: {context['last_proposed_slot_iso']} (idx {i})")
                else:
                    logging.warning(f"Ostrz. [{user_psid}]: Wpis systemowy bez 'slot_iso' (idx {i})")
            elif isinstance(msg_data, dict) and msg_data.get('role') == 'system':
                pass # Ignoruj stare konteksty
            else:
                logging.warning(f"Ostrz. [{user_psid}]: Pominięto wpis w historii (idx {i}): {msg_data}")

        logging.info(f"[{user_psid}] Wczytano {len(history)} wiad. (user/model).")
        if 'message_index_in_file' in context and context['message_index_in_file'] != len(history_data) - 1:
            logging.info(f"[{user_psid}] Kontekst nieaktualny. Reset.")
            context = {}
        return history, context

    except Exception as e:
        logging.error(f"BŁĄD [{user_psid}] wczytywania historii: {e}", exc_info=True)
        return [], {}

def save_history(user_psid, history, context_to_save=None):
    """Zapisuje historię konwersacji i opcjonalny kontekst systemowy."""
    ensure_dir(HISTORY_DIR)
    filepath = os.path.join(HISTORY_DIR, f"{user_psid}.json")
    temp_filepath = f"{filepath}.tmp"
    history_data = []
    try:
        max_messages = MAX_HISTORY_TURNS * 2
        start_index = max(0, len(history) - max_messages)
        history_to_save = history[start_index:]
        if len(history) > max_messages:
            logging.info(f"[{user_psid}] Historia przycięta do zapisu: {len(history_to_save)} wiad.")

        for msg in history_to_save:
             if isinstance(msg, Content) and msg.role in ('user', 'model') and msg.parts:
                parts = [{'text': p.text} for p in msg.parts if hasattr(p, 'text')]
                if parts:
                    history_data.append({'role': msg.role, 'parts': parts})
             else:
                 logging.warning(f"Ostrz. [{user_psid}]: Pomijanie obiektu w zapisie historii: {type(msg)}")

        if context_to_save:
             history_data.append(context_to_save)
             logging.info(f"[{user_psid}] Dodano kontekst {context_to_save.get('type')} do zapisu.")

        with open(temp_filepath, 'w', encoding='utf-8') as f:
            json.dump(history_data, f, ensure_ascii=False, indent=2)
        os.replace(temp_filepath, filepath)
        logging.info(f"[{user_psid}] Zapisano historię/kontekst ({len(history_data)} wpisów).")
    except Exception as e:
        logging.error(f"BŁĄD [{user_psid}] zapisu historii: {e}", exc_info=True)
        if os.path.exists(temp_filepath):
            try:
                os.remove(temp_filepath)
                logging.info(f"Usunięto {temp_filepath} po błędzie.")
            except OSError as rem_e:
                logging.error(f"Nie można usunąć {temp_filepath}: {rem_e}")

def _get_timezone():
    """Pobiera i cachuje obiekt strefy czasowej."""
    global _tz
    if _tz is None:
        try:
            _tz = pytz.timezone(CALENDAR_TIMEZONE)
            logging.info(f"Ustawiono strefę czasową: {CALENDAR_TIMEZONE}")
        except pytz.exceptions.UnknownTimeZoneError:
            logging.error(f"BŁĄD: Strefa '{CALENDAR_TIMEZONE}' nieznana. Używam UTC.")
            _tz = pytz.utc
    return _tz

def get_calendar_service():
    """Pobiera i cachuje obiekt usługi Google Calendar API."""
    global _calendar_service
    if _calendar_service:
        return _calendar_service
    if not os.path.exists(SERVICE_ACCOUNT_FILE):
        logging.error(f"KRYTYCZNY: Brak pliku klucza: '{SERVICE_ACCOUNT_FILE}'")
        return None
    try:
        creds = service_account.Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=CALENDAR_SCOPES)
        service = build('calendar', 'v3', credentials=creds)
        logging.info("Połączono z Google Calendar API.")
        _calendar_service = service
        return service
    except Exception as e:
        logging.error(f"Błąd tworzenia usługi Calendar: {e}", exc_info=True)
        return None

def parse_event_time(event_time_data, default_tz):
    """Parsuje datę/czas z danych wydarzenia Google Calendar."""
    if not event_time_data: return None
    if 'dateTime' in event_time_data:
        dt_str = event_time_data['dateTime']
        try:
            dt = datetime.datetime.fromisoformat(dt_str.replace('Z', '+00:00'))
        except ValueError:
            logging.warning(f"Ostrz.: Nie sparsowano dateTime: {dt_str}")
            return None
        if dt.tzinfo is None:
            dt = pytz.utc.localize(dt) # Assume UTC if no timezone info
        return dt.astimezone(default_tz)
    elif 'date' in event_time_data:
        try:
            return datetime.date.fromisoformat(event_time_data['date'])
        except ValueError:
            logging.warning(f"Ostrz.: Nie sparsowano date: {event_time_data['date']}")
            return None
    return None

def get_free_time_ranges(calendar_id, start_datetime, end_datetime):
    """Znajduje ciągłe zakresy wolnego czasu w kalendarzu Google."""
    service = get_calendar_service()
    tz = _get_timezone()
    if not service:
        logging.error("Błąd: Usługa kalendarza niedostępna w get_free_time_ranges.")
        return []
    if start_datetime.tzinfo is None: start_datetime = tz.localize(start_datetime)
    else: start_datetime = start_datetime.astimezone(tz)
    if end_datetime.tzinfo is None: end_datetime = tz.localize(end_datetime)
    else: end_datetime = end_datetime.astimezone(tz)

    logging.info(f"Szukanie zakresów (min. {APPOINTMENT_DURATION_MINUTES} min) w '{calendar_id}'")
    logging.info(f"Zakres: {start_datetime:%Y-%m-%d %H:%M %Z} - {end_datetime:%Y-%m-%d %H:%M %Z}")
    try:
        events_result = service.events().list(
            calendarId=calendar_id, timeMin=start_datetime.isoformat(),
            timeMax=end_datetime.isoformat(), singleEvents=True, orderBy='startTime'
        ).execute()
        events = events_result.get('items', [])
        logging.info(f"Pobrano {len(events)} wydarzeń.")
    except Exception as e:
        logging.error(f'Błąd API pobierania wydarzeń: {e}', exc_info=True)
        return []

    free_ranges = []
    current_time = start_datetime
    appointment_duration_td = datetime.timedelta(minutes=APPOINTMENT_DURATION_MINUTES)

    busy_times = []
    for event in events:
        start = parse_event_time(event.get('start'), tz)
        end = parse_event_time(event.get('end'), tz)
        if isinstance(start, datetime.datetime) and isinstance(end, datetime.datetime):
            if end > start_datetime and start < end_datetime:
                 effective_start = max(start, start_datetime)
                 effective_end = min(end, end_datetime)
                 if effective_start < effective_end:
                     busy_times.append({'start': effective_start, 'end': effective_end})
        elif isinstance(start, datetime.date):
            event_date = start
            day_start = tz.localize(datetime.datetime.combine(event_date, datetime.time(0,0)))
            day_end = tz.localize(datetime.datetime.combine(event_date, datetime.time(23,59,59)))
            if day_end > start_datetime and day_start < end_datetime:
                effective_start = max(day_start, start_datetime)
                effective_end = min(day_end, end_datetime)
                if effective_start < effective_end:
                    busy_times.append({'start': effective_start, 'end': effective_end})

    busy_times.sort(key=lambda x: x['start'])
    merged_busy_times = []
    if busy_times:
        merged_busy_times.append(busy_times[0])
        for current in busy_times[1:]:
            last = merged_busy_times[-1]
            if current['start'] <= last['end']:
                last['end'] = max(last['end'], current['end'])
            else:
                merged_busy_times.append(current)

    for busy in merged_busy_times:
        free_start = current_time
        free_end = busy['start']
        current_check_date = free_start.date()
        while True:
             day_start_work = tz.localize(datetime.datetime.combine(current_check_date, datetime.time(WORK_START_HOUR, 0)))
             day_end_work = tz.localize(datetime.datetime.combine(current_check_date, datetime.time(WORK_END_HOUR, 0)))
             eff_start = max(free_start, day_start_work)
             eff_end = min(free_end, day_end_work)

             if eff_end > eff_start and eff_end - eff_start >= appointment_duration_td:
                 free_ranges.append({'start': eff_start, 'end': eff_end})
                 logging.debug(f"  + Zakres (między): {eff_start:%Y-%m-%d %H:%M} - {eff_end:%Y-%m-%d %H:%M}")

             if free_end.date() > current_check_date:
                 current_check_date += datetime.timedelta(days=1)
                 free_start = tz.localize(datetime.datetime.combine(current_check_date, datetime.time(WORK_START_HOUR, 0))) # Start from work start next day
             else:
                 break
        current_time = max(current_time, busy['end'])

    free_start = current_time
    free_end = end_datetime
    current_check_date = free_start.date()
    while True:
        day_start_work = tz.localize(datetime.datetime.combine(current_check_date, datetime.time(WORK_START_HOUR, 0)))
        day_end_work = tz.localize(datetime.datetime.combine(current_check_date, datetime.time(WORK_END_HOUR, 0)))
        eff_start = max(free_start, day_start_work)
        eff_end = min(free_end, day_end_work)
        if eff_end > eff_start and eff_end - eff_start >= appointment_duration_td:
            free_ranges.append({'start': eff_start, 'end': eff_end})
            logging.debug(f"  + Zakres (po): {eff_start:%Y-%m-%d %H:%M} - {eff_end:%Y-%m-%d %H:%M}")
        if free_end.date() > current_check_date:
            current_check_date += datetime.timedelta(days=1)
            free_start = tz.localize(datetime.datetime.combine(current_check_date, datetime.time(WORK_START_HOUR, 0)))
        else:
            break

    logging.info(f"Znaleziono {len(free_ranges)} zakresów wolnego czasu.")
    return free_ranges

def format_ranges_for_ai(ranges):
    """Formatuje listę zakresów na czytelny tekst dla AI."""
    if not ranges:
        return "Brak dostępnych zakresów czasowych."
    ranges_by_date = defaultdict(list)
    for r in ranges:
        range_date = r['start'].date()
        ranges_by_date[range_date].append({
            'start_time': r['start'].strftime('%H:%M'),
            'end_time': r['end'].strftime('%H:%M')
        })
    formatted = ["Oto dostępne zakresy czasowe (wizyta trwa 60 minut). Wybierz zakres i wygeneruj termin (preferuj pełne godziny):"]
    for d in sorted(ranges_by_date.keys()):
        day_name = POLISH_WEEKDAYS[d.weekday()]
        date_str = d.strftime('%d.%m.%Y')
        times = [f"{tr['start_time']}-{tr['end_time']}" for tr in ranges_by_date[d]]
        formatted.append(f"- {day_name}, {date_str}: {'; '.join(times)}")
    return "\n".join(formatted)

def get_valid_start_times(calendar_id, check_start, check_end):
    """Zwraca listę DOKŁADNYCH dozwolonych czasów rozpoczęcia wizyty (co 10 min)."""
    service = get_calendar_service()
    tz = _get_timezone()
    if not service: return []
    if check_start.tzinfo is None: check_start = tz.localize(check_start)
    else: check_start = check_start.astimezone(tz)
    if check_end.tzinfo is None: check_end = tz.localize(check_end)
    else: check_end = check_end.astimezone(tz)

    logging.debug(f"Pobieranie dokładnych slotów w zakresie: {check_start.isoformat()} - {check_end.isoformat()}")
    try:
        events_result = service.events().list(calendarId=calendar_id, timeMin=check_start.isoformat(), timeMax=check_end.isoformat(), singleEvents=True, orderBy='startTime').execute()
        events = events_result.get('items', [])
    except Exception as e:
        logging.error(f"Błąd API przy pobieraniu wydarzeń dla weryfikacji: {e}")
        return []

    valid_starts = []
    current_day = check_start.date()
    end_day = check_end.date()
    appointment_duration = datetime.timedelta(minutes=APPOINTMENT_DURATION_MINUTES)

    while current_day <= end_day:
        day_start_limit = tz.localize(datetime.datetime.combine(current_day, datetime.time(WORK_START_HOUR, 0)))
        day_end_limit = tz.localize(datetime.datetime.combine(current_day, datetime.time(WORK_END_HOUR, 0)))
        loop_check_start = max(check_start, day_start_limit)
        loop_check_end = min(check_end, day_end_limit)
        if loop_check_start >= loop_check_end:
            current_day += datetime.timedelta(days=1)
            continue

        busy_intervals = []
        for event in events:
            start = parse_event_time(event.get('start'), tz)
            end = parse_event_time(event.get('end'), tz)
            if isinstance(start, datetime.date):
                if start == current_day: busy_intervals.append({'start': day_start_limit, 'end': day_end_limit})
            elif isinstance(start, datetime.datetime) and isinstance(end, datetime.datetime):
                if end > loop_check_start and start < loop_check_end:
                    eff_start = max(start, loop_check_start)
                    eff_end = min(end, loop_check_end)
                    if eff_start < eff_end: busy_intervals.append({'start': eff_start, 'end': eff_end})

        merged_busy_times = []
        if busy_intervals:
             busy_intervals.sort(key=lambda x: x['start'])
             merged_busy_times.append(busy_intervals[0])
             for current in busy_intervals[1:]:
                 last = merged_busy_times[-1]
                 if current['start'] <= last['end']: last['end'] = max(last['end'], current['end'])
                 else: merged_busy_times.append(current)

        potential_start = loop_check_start
        for busy in merged_busy_times:
            while potential_start + appointment_duration <= busy['start']:
                if potential_start.minute % 10 == 0:
                    valid_starts.append(potential_start)
                minutes_to_add = 10 - (potential_start.minute % 10) if potential_start.minute % 10 != 0 else 10
                potential_start += datetime.timedelta(minutes=minutes_to_add)
                potential_start = potential_start.replace(second=0, microsecond=0)
            potential_start = max(potential_start, busy['end'])

        while potential_start + appointment_duration <= loop_check_end:
             if potential_start.minute % 10 == 0:
                 valid_starts.append(potential_start)
             minutes_to_add = 10 - (potential_start.minute % 10) if potential_start.minute % 10 != 0 else 10
             potential_start += datetime.timedelta(minutes=minutes_to_add)
             potential_start = potential_start.replace(second=0, microsecond=0)

        current_day += datetime.timedelta(days=1)

    final_starts = sorted(list(set(s for s in valid_starts if check_start <= s < check_end)))
    logging.debug(f"Znaleziono {len(final_starts)} dokładnych slotów w wąskim zakresie.")
    return final_starts

def is_slot_actually_free(proposed_start_dt, calendar_id):
    """Sprawdza, czy DOKŁADNIE proponowany slot jest możliwy do zarezerwowania."""
    try:
        tz = _get_timezone()
        if proposed_start_dt.tzinfo is None:
            proposed_start_dt = tz.localize(proposed_start_dt)
        else:
            proposed_start_dt = proposed_start_dt.astimezone(tz)

        if not (WORK_START_HOUR <= proposed_start_dt.hour < WORK_END_HOUR):
            logging.info(f"Weryfikacja slotu {proposed_start_dt.isoformat()}: INVALID (Poza godzinami pracy)")
            return False
        if proposed_start_dt.minute % 10 != 0:
             logging.info(f"Weryfikacja slotu {proposed_start_dt.isoformat()}: INVALID (Nie co 10 minut)")
             return False

        check_start = proposed_start_dt
        check_end = proposed_start_dt + datetime.timedelta(minutes=APPOINTMENT_DURATION_MINUTES)
        service = get_calendar_service()
        if not service: return False

        events_result = service.events().list(
            calendarId=calendar_id, timeMin=check_start.isoformat(),
            timeMax=check_end.isoformat(), maxResults=1
        ).execute()
        items = events_result.get('items', [])

        is_free = not items
        logging.info(f"Weryfikacja slotu {proposed_start_dt.isoformat()}: {'FREE' if is_free else 'BUSY'}")
        return is_free
    except Exception as e:
        logging.error(f"Błąd weryfikacji slotu {proposed_start_dt.isoformat()}: {e}", exc_info=True)
        return False

def format_slot_for_user(slot_start):
    """Formatuje pojedynczy slot (datetime) na czytelny tekst dla użytkownika."""
    if not isinstance(slot_start, datetime.datetime): return ""
    try:
        day_index = slot_start.weekday(); day_name = POLISH_WEEKDAYS[day_index]
        hour_str = slot_start.strftime('%#H') if os.name != 'nt' else slot_start.strftime('%H')
        try: hour_str = str(slot_start.hour)
        except Exception: pass
        return f"{day_name}, {slot_start.strftime(f'%d.%m.%Y o {hour_str}:%M')}"
    except Exception as e:
        logging.error(f"Błąd formatowania slotu: {e}", exc_info=True)
        return slot_start.isoformat()

def book_appointment(calendar_id, start_time, end_time, summary="Rezerwacja wizyty", description="", user_name=""):
    """Rezerwuje wizytę w kalendarzu Google."""
    service = get_calendar_service(); tz = _get_timezone()
    if not service: return False, "Błąd: Brak połączenia z usługą kalendarza."
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
        'reminders': {'useDefault': False, 'overrides': [{'method': 'popup', 'minutes': 60},], },}
    try:
        logging.info(f"Próba rezerwacji: '{event_summary}' od {start_time:%Y-%m-%d %H:%M} do {end_time:%Y-%m-%d %H:%M}")
        created_event = service.events().insert(calendarId=calendar_id, body=event).execute()
        event_id = created_event.get('id'); logging.info(f"Rezerwacja OK. ID: {event_id}")
        day_index = start_time.weekday(); locale_day_name = POLISH_WEEKDAYS[day_index]
        hour_str = start_time.strftime('%#H') if os.name != 'nt' else start_time.strftime('%H')
        try: hour_str = str(start_time.hour)
        except Exception: pass
        confirm_message = f"Świetnie! Termin na {locale_day_name}, {start_time.strftime(f'%d.%m.%Y o {hour_str}:%M')} został zarezerwowany."
        return True, confirm_message
    except HttpError as error:
        error_details = f"Kod: {error.resp.status}, Powód: {error.resp.reason}"
        try:
            error_json = json.loads(error.content.decode('utf-8')); msg = error_json.get('error', {}).get('message', '')
            if msg: error_details += f" - {msg}"
        except: pass
        logging.error(f"Błąd API rezerwacji: {error}, Szczegóły: {error_details}", exc_info=True)
        if error.resp.status == 409: return False, "Niestety, ten termin został właśnie zajęty. Czy chcesz spróbować znaleźć inny?"
        elif error.resp.status == 403: return False, f"Brak uprawnień do zapisu w '{calendar_id}'."
        elif error.resp.status == 404: return False, f"Nie znaleziono kalendarza '{calendar_id}'."
        else: return False, f"Błąd API ({error.resp.status}) rezerwacji."
    except Exception as e:
        logging.error(f"Nieoczekiwany błąd Python rezerwacji: {e}", exc_info=True)
        return False, "Błąd systemu rezerwacji."

# =====================================================================
# === Inicjalizacja Vertex AI =========================================
# =====================================================================

gemini_model = None
try:
    logging.info(f"Inicjalizowanie Vertex AI: Projekt={PROJECT_ID}, Lokalizacja={LOCATION}")
    vertexai.init(project=PROJECT_ID, location=LOCATION); logging.info("Inicjalizacja Vertex AI OK.")
    logging.info(f"Ładowanie modelu: {MODEL_ID}"); gemini_model = GenerativeModel(MODEL_ID); logging.info(f"Model {MODEL_ID} OK.")
except Exception as e: logging.critical(f"KRYTYCZNY BŁĄD inicjalizacji Vertex AI: {e}", exc_info=True)

GENERATION_CONFIG_DEFAULT = GenerationConfig(temperature=0.7, top_p=0.95, top_k=40, max_output_tokens=1024)
GENERATION_CONFIG_PROPOSAL = GenerationConfig(temperature=0.5, top_p=0.95, top_k=40, max_output_tokens=1024)
GENERATION_CONFIG_FEEDBACK = GenerationConfig(temperature=0.1, top_p=0.95, top_k=40, max_output_tokens=100)
SAFETY_SETTINGS = {HarmCategory.HARM_CATEGORY_HARASSMENT: HarmBlockThreshold.BLOCK_MEDIUM_AND_ABOVE, HarmCategory.HARM_CATEGORY_HATE_SPEECH: HarmBlockThreshold.BLOCK_MEDIUM_AND_ABOVE, HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT: HarmBlockThreshold.BLOCK_MEDIUM_AND_ABOVE, HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT: HarmBlockThreshold.BLOCK_MEDIUM_AND_ABOVE,}

# =====================================================================
# === FUNKCJE WYSYŁANIA WIADOMOŚCI FB ================================
# =====================================================================

def _send_single_message(recipient_id, message_text):
    """Wysyła pojedynczy fragment wiadomości przez Facebook Graph API."""
    logging.info(f"--- Wysyłanie fragm. do {recipient_id} (dł: {len(message_text)}) ---")
    params = {"access_token": PAGE_ACCESS_TOKEN}
    payload = {"recipient": {"id": recipient_id}, "message": {"text": message_text}, "messaging_type": "RESPONSE"}
    if not PAGE_ACCESS_TOKEN: logging.error(f"!!! [{recipient_id}] Brak tokena!"); return False
    elif len(PAGE_ACCESS_TOKEN) < 50: logging.error(f"!!! [{recipient_id}] Token za krótki!"); return False
    try:
        r = requests.post(FACEBOOK_GRAPH_API_URL, params=params, json=payload, timeout=30)
        r.raise_for_status()
        response_json = r.json()
        if 'error' in response_json: logging.error(f"!!! BŁĄD FB API wysyłania: {response_json['error']}"); return False
        logging.info(f"--- Fragment wysłany OK ---"); return True
    except requests.exceptions.Timeout: logging.error(f"!!! BŁĄD TIMEOUT wysyłania"); return False
    except requests.exceptions.HTTPError as err: logging.error(f"!!! BŁĄD HTTP {err.response.status_code}: {err}"); return False
    except Exception as e: logging.error(f"!!! BŁĄD wysyłania: {e}", exc_info=True); return False

def send_message(recipient_id, full_message_text):
    """Wysyła wiadomość do użytkownika, dzieląc ją na fragmenty w razie potrzeby."""
    if not full_message_text or not isinstance(full_message_text, str) or not full_message_text.strip():
        logging.warning(f"[{recipient_id}] Pusta wiadomość."); return
    logging.info(f"[{recipient_id}] Przygotowanie wiad. (dł: {len(full_message_text)}).")
    if len(full_message_text) <= MESSAGE_CHAR_LIMIT:
        _send_single_message(recipient_id, full_message_text)
    else:
        chunks = []
        remaining = full_message_text
        logging.info(f"[{recipient_id}] Dzielenie wiad...")
        while remaining:
            if len(remaining) <= MESSAGE_CHAR_LIMIT:
                chunks.append(remaining.strip())
                break
            idx = -1
            for d in ['\n\n', '\n', '. ', '! ', '? ', ' ']:
                lim = MESSAGE_CHAR_LIMIT - (len(d)-1) if len(d)>1 else MESSAGE_CHAR_LIMIT
                t_idx = remaining.rfind(d, 0, lim + len(d))
                if t_idx != -1 and t_idx <= MESSAGE_CHAR_LIMIT :
                    idx = t_idx + len(d)
                    break
            if idx == -1:
                idx = MESSAGE_CHAR_LIMIT
                logging.warning(f"[{recipient_id}] Cięcie na limicie.")
            chunk = remaining[:idx].strip()
            if chunk:
                chunks.append(chunk)
            remaining = remaining[idx:].strip()
        logging.info(f"[{recipient_id}] Podzielono na {len(chunks)} fragm.")
        ok_count = 0
        for i, ch in enumerate(chunks):
            logging.info(f"[{recipient_id}] Wysyłanie {i+1}/{len(chunks)}...")
            if not _send_single_message(recipient_id, ch):
                logging.error(f"!!! [{recipient_id}] Anulowano resztę.")
                break
            ok_count += 1
            if i < len(chunks) - 1:
                time.sleep(MESSAGE_DELAY_SECONDS)
        logging.info(f"--- [{recipient_id}] Wysłano {ok_count}/{len(chunks)} fragm. ---")

# =====================================================================
# === INSTRUKCJE SYSTEMOWE DLA AI =====================================
# =====================================================================

# Instrukcje SYSTEM_INSTRUCTION_GENERAL, SYSTEM_INSTRUCTION_PROPOSE (dla zakresów), SYSTEM_INSTRUCTION_FEEDBACK (z obsługą datetime)
# ... (treść instrukcji jak w poprzedniej odpowiedzi) ...
SYSTEM_INSTRUCTION_GENERAL = f"""Jesteś profesjonalnym i przyjaznym asystentem klienta 'Zakręcone Korepetycje'. Pomagasz w sprawach związanych z korepetycjami online.

**Twoje Główne Zadania:**
1.  Odpowiadaj na pytania dotyczące: przedmiotów (matematyka, j. polski, j. angielski), poziomów (klasy 4 SP - matura), cennika (poniżej), formy zajęć (online, 60 min), pierwszej lekcji próbnej (płatna wg cennika).
2.  Prowadź naturalną, uprzejmą rozmowę po polsku.
3.  **Analizuj intencje:** Czy użytkownik chce się umówić lub pyta o terminy?
4.  **Jeśli intencja umówienia:** Odpowiedź MUSI zawierać znacznik `{INTENT_SCHEDULE_MARKER}` oraz krótkie potwierdzenie (np. "Jasne, sprawdzę terminy."). Przykład: "OK, sprawdzę wolne terminy dla 8 klasy. {INTENT_SCHEDULE_MARKER}"
5.  **Jeśli BRAK intencji umówienia:** Odpowiedz normalnie, BEZ znacznika `{INTENT_SCHEDULE_MARKER}`.
6.  Pamiętaj o historii rozmowy.

**Cennik (60 min):** 4-8 SP: 60 zł; 1-3 LO/Tech(P): 65 zł; 1-3 LO/Tech(R): 70 zł; 4 LO/Tech(P): 70 zł; 4 LO/Tech(R): 75 zł.

**Ważne:** Znacznik `{INTENT_SCHEDULE_MARKER}` jest kluczowy. Używaj go **tylko** gdy użytkownik chce się umówić.
"""

SYSTEM_INSTRUCTION_PROPOSE = f"""Jesteś asystentem AI proponującym terminy dla 'Zakręcone Korepetycje'. Wybierz **jeden zakres**, wygeneruj w nim **jeden konkretny termin** ({APPOINTMENT_DURATION_MINUTES} min) i zaproponuj go.

**Kontekst:** Użytkownik chce umówić lekcję próbną. Masz listę ZAKRESÓW wolnego czasu.

**Dostępne zakresy czasowe:**
{{available_ranges_text}}

**Twoje zadanie:**
1.  Analizuj historię rozmowy pod kątem preferencji (np. "popołudniu", "wtorek").
2.  Wybierz **jeden** zakres pasujący do preferencji lub "rozsądny" (popołudnie w tyg., od {PREFERRED_WEEKEND_START_HOUR} w weekend).
3.  W wybranym zakresie **wygeneruj DOKŁADNY czas startu**. **Preferuj PEŁNE GODZINY** (np. 16:00).
4.  **BARDZO WAŻNE:** Upewnij się, że `wygenerowany_czas + {APPOINTMENT_DURATION_MINUTES} minut` **mieści się w wybranym zakresie**.
5.  Sformułuj krótką, uprzejmą propozycję wygenerowanego terminu (polski format daty/dnia).
6.  **KLUCZOWE:** Odpowiedź **MUSI** zawierać znacznik `{SLOT_ISO_MARKER_PREFIX}WYGENEROWANY_ISO_STRING{SLOT_ISO_MARKER_SUFFIX}` z poprawnym ISO 8601 **wygenerowanego** terminu.

**Przykład (zakres "Środa, 07.05.2025: 16:00-18:30"):**
*   Dobry: "Proponuję: Środa, 07.05.2025 o 17:00. Pasuje? {SLOT_ISO_MARKER_PREFIX}2025-05-07T17:00:00+02:00{SLOT_ISO_MARKER_SUFFIX}"
*   Zły (nie mieści się): 18:00

**Zasady:** Generuj JEDEN termin. Preferuj pełne godziny. Sprawdź zakres. ZAWSZE dołączaj znacznik ISO. Bez cennika itp.
"""

SYSTEM_INSTRUCTION_FEEDBACK = f"""Jesteś asystentem AI analizującym odpowiedź użytkownika na propozycję terminu.

**Kontekst:** Zaproponowano użytkownikowi termin.
**Ostatnia propozycja:** "{{last_proposal_text}}"
**Odpowiedź użytkownika:** "{{user_feedback}}"

**Zadanie:** Zwróć **TYLKO JEDEN** z poniższych znaczników:
*   `[ACCEPT]`: Akceptacja (tak, ok, pasuje, rezerwuję).
*   `[REJECT_FIND_NEXT PREFERENCE='any']`: Odrzucenie, brak preferencji (nie pasuje, inny).
*   `[REJECT_FIND_NEXT PREFERENCE='later']`: Odrzucenie, preferencja późniejszego (za wcześnie, popołudniu).
*   `[REJECT_FIND_NEXT PREFERENCE='earlier']`: Odrzucenie, preferencja wcześniejszego (za późno, rano).
*   `[REJECT_FIND_NEXT PREFERENCE='next_day']`: Odrzucenie, prośba o inny dzień.
*   `[REJECT_FIND_NEXT PREFERENCE='specific_day' DAY='NAZWA_DNIA']`: Odrzucenie, prośba o **tylko** konkretny dzień (bez godziny). NAZWA_DNIA = pełna polska nazwa z dużej litery.
*   `[REJECT_FIND_NEXT PREFERENCE='specific_hour' HOUR='GODZINA']`: Odrzucenie, prośba o **tylko** konkretną godzinę (bez dnia). GODZINA = liczba.
*   **`[REJECT_FIND_NEXT PREFERENCE='specific_datetime' DAY='NAZWA_DNIA' HOUR='GODZINA']`**: Odrzucenie, podany **dzień I godzina** (np. piątek 18).
*   `[CLARIFY]`: Niejasna odpowiedź, pytanie niezwiązane.

**Ważne:** Dokładnie jeden znacznik. `specific_datetime` ma pierwszeństwo.
"""

# =====================================================================
# === FUNKCJE INTERAKCJI Z GEMINI AI ==================================
# =====================================================================

def _call_gemini(user_psid, prompt_content, generation_config, model_purpose="", max_retries=1):
    """Wewnętrzna funkcja do wywoływania modelu Gemini z opcją ponowienia."""
    if not gemini_model:
        logging.error(f"!!! [{user_psid}] Model {MODEL_ID} niezaładowany! ({model_purpose}).")
        return None
    if not prompt_content:
        logging.warning(f"[{user_psid}] Pusty prompt dla {model_purpose}.")
        return None

    attempt = 0
    while attempt <= max_retries:
        attempt += 1
        logging.info(f"\n--- [{user_psid}] Wywołanie Gemini ({MODEL_ID}) - {model_purpose} (Próba: {attempt}/{max_retries + 1}) ---")
        if logging.getLogger().isEnabledFor(logging.DEBUG):
            try: # Logowanie JSON promptu
                prompt_dict=[{'role':c.role,'parts':[{'text':p.text} for p in c.parts if hasattr(p,'text')]} if isinstance(c,Content) else repr(c) for c in prompt_content]
                logging.debug(f"--- Prompt dla {user_psid} ---\n{json.dumps(prompt_dict, indent=2, ensure_ascii=False)}\n--- Koniec Promptu ---")
            except Exception as log_err:
                logging.error(f"Błąd logowania promptu: {log_err}")
        try:
            response = gemini_model.generate_content(prompt_content, generation_config=generation_config, safety_settings=SAFETY_SETTINGS, stream=False)
            if hasattr(response, 'prompt_feedback') and response.prompt_feedback.block_reason:
                 logging.warning(f"[{user_psid}] Prompt zablokowany (Próba {attempt}): {response.prompt_feedback.block_reason_message}")
                 return None
            if not response.candidates:
                 logging.warning(f"[{user_psid}] Brak kandydatów (Próba {attempt}).")
                 return None
            candidate = response.candidates[0]
            finish_reason = candidate.finish_reason.name
            if finish_reason != "STOP" and finish_reason != "MAX_TOKENS":
                 logging.warning(f"[{user_psid}] Zakończono: {finish_reason} (Próba {attempt})")
                 return None # Nie ponawiaj jeśli zablokowane lub inny błąd
            if finish_reason == "STOP" and (not candidate.content or not candidate.content.parts):
                logging.warning(f"[{user_psid}] Brak treści mimo STOP (Próba {attempt}).")
                if attempt <= max_retries:
                    logging.warning(f"    Ponawiam próbę {attempt + 1}...")
                    time.sleep(1)
                    continue
                else:
                    logging.error(f"!!! [{user_psid}] Max prób ({max_retries + 1}) dla pustej odpowiedzi. Zwracam None.")
                    return None
            generated_text = candidate.content.parts[0].text.strip()
            logging.info(f"[{user_psid}] Gemini ({model_purpose}) OK (Próba {attempt}): '{generated_text[:200]}...'")
            return generated_text
        except Exception as e:
            logging.error(f"!!! BŁĄD Gemini ({model_purpose}, Próba {attempt}): {e}", exc_info=True)
            return None
    logging.error(f"!!! [{user_psid}] Pętla _call_gemini zakończona.")
    return None

def get_gemini_general_response(user_psid, user_input, history):
    """Wywołuje AI do prowadzenia rozmowy i wykrywania intencji umówienia."""
    if not user_input: return None
    history_for_ai = [m for m in history if m.role in ('user','model')]
    user_c = Content(role="user", parts=[Part.from_text(user_input)])
    prompt = [
        Content(role="user", parts=[Part.from_text(SYSTEM_INSTRUCTION_GENERAL)]),
        Content(role="model", parts=[Part.from_text("Rozumiem.")])
    ] + history_for_ai + [user_c]

    # Poprawione przycinanie promptu (bez średników)
    while len(prompt) > (MAX_HISTORY_TURNS * 2 + 3) and len(prompt) > 3:
        logging.warning(f"[{user_psid}] Prompt General za długi ({len(prompt)}). Usuwam turę.")
        prompt.pop(2) # Usuń najstarszą wiadomość użytkownika (po instrukcjach)
        if len(prompt) > 3: # Upewnij się, że jest co usunąć (odpowiedź modelu)
             prompt.pop(2) # Usuń odpowiadającą jej wiadomość modelu

    response_text = _call_gemini(user_psid, prompt, GENERATION_CONFIG_DEFAULT, "General Conversation & Intent Detection", 1)
    return response_text

def get_gemini_slot_proposal(user_psid, history, available_ranges):
    """Wywołuje AI, aby wygenerowało jeden slot z podanych zakresów."""
    if not available_ranges:
        logging.warning(f"[{user_psid}]: Brak zakresów dla AI ({MODEL_ID}).")
        return None, None
    ranges_text = format_ranges_for_ai(available_ranges)
    logging.info(f"[{user_psid}] Przekazuję {len(available_ranges)} zakresów do AI ({MODEL_ID}).")
    history_for_ai = [m for m in history if m.role in ('user','model')]
    instr = SYSTEM_INSTRUCTION_PROPOSE.format(available_ranges_text=ranges_text)
    prompt = [
        Content(role="user", parts=[Part.from_text(instr)]),
        Content(role="model", parts=[Part.from_text(f"OK. Wygeneruję termin ({APPOINTMENT_DURATION_MINUTES} min) w zakresie, preferując pełne godziny i dodam [{SLOT_ISO_MARKER_PREFIX}ISO].")])
    ] + history_for_ai

    # Poprawione przycinanie promptu (bez średników)
    while len(prompt) > (MAX_HISTORY_TURNS * 2 + 2) and len(prompt) > 2:
         logging.warning(f"[{user_psid}] Prompt Proposal za długi ({len(prompt)}). Usuwam turę.")
         prompt.pop(2) # Usuń najstarszą wiadomość użytkownika (po instrukcjach)
         if len(prompt) > 2: # Upewnij się, że jest co usunąć (odpowiedź modelu)
             prompt.pop(2) # Usuń odpowiadającą jej wiadomość modelu

    generated_text = _call_gemini(user_psid, prompt, GENERATION_CONFIG_PROPOSAL, "Slot Proposal from Ranges", 1)
    if not generated_text: return None, None

    iso_match = re.search(rf"\{SLOT_ISO_MARKER_PREFIX}(.*?)\{SLOT_ISO_MARKER_SUFFIX}", generated_text)
    if iso_match:
        extracted_iso = iso_match.group(1)
        text_for_user = re.sub(rf"\{SLOT_ISO_MARKER_PREFIX}.*?\{SLOT_ISO_MARKER_SUFFIX}", "", generated_text).strip()
        text_for_user = re.sub(r'\s+', ' ', text_for_user).strip()
        logging.info(f"[{user_psid}] AI wygenerowało: {extracted_iso}. Text: '{text_for_user}'")
        try:
            datetime.datetime.fromisoformat(extracted_iso) # Walidacja formatu
            return text_for_user, extracted_iso
        except ValueError:
            logging.error(f"!!! AI Error [{user_psid}]: Zły format ISO '{extracted_iso}'!")
            return None, None
    else:
        logging.error(f"!!! AI Error [{user_psid}]: Brak znacznika ISO w odpowiedzi! Odp: '{generated_text}'")
        return None, None

def get_gemini_feedback_decision(user_psid, user_feedback, history, last_proposal_text):
    """Wywołuje AI do interpretacji odpowiedzi użytkownika na propozycję terminu."""
    if not user_feedback: return "[CLARIFY]"
    history_for_ai=[m for m in history if m.role in ('user','model')]
    user_c=Content(role="user", parts=[Part.from_text(user_feedback)])
    instr=SYSTEM_INSTRUCTION_FEEDBACK.format(last_proposal_text=last_proposal_text, user_feedback=user_feedback)
    prompt = [
        Content(role="user", parts=[Part.from_text(instr)]),
        Content(role="model", parts=[Part.from_text("OK. Zwrócę jeden znacznik.")])
    ] + history_for_ai + [user_c]

    # Poprawione przycinanie promptu (bez średników)
    while len(prompt) > (MAX_HISTORY_TURNS * 2 + 3) and len(prompt) > 3:
        logging.warning(f"[{user_psid}] Prompt Feedback za długi ({len(prompt)}). Usuwam turę.")
        prompt.pop(2) # Usuń najstarszą wiadomość użytkownika (po instrukcjach)
        if len(prompt) > 3: # Upewnij się, że jest co usunąć (odpowiedź modelu)
             prompt.pop(2) # Usuń odpowiadającą jej wiadomość modelu

    decision = _call_gemini(user_psid, prompt, GENERATION_CONFIG_FEEDBACK, "Feedback Interpretation", 1)
    if not decision: return "[CLARIFY]"
    if decision.startswith("[") and decision.endswith("]"):
        logging.info(f"[{user_psid}] AI feedback: {decision}")
        return decision
    else:
        logging.warning(f"Ostrz. [{user_psid}]: AI nie zwróciło znacznika: '{decision}'. CLARIFY.")
        return "[CLARIFY]"

# =====================================================================
# === OBSŁUGA WEBHOOKA FACEBOOKA =====================================
# =====================================================================

@app.route('/webhook', methods=['GET'])
def webhook_verification():
    """Obsługuje weryfikację webhooka przez Facebooka (GET request)."""
    logging.info("--- GET weryfikacja ---")
    hub_mode=request.args.get('hub.mode')
    hub_token=request.args.get('hub.verify_token')
    hub_challenge=request.args.get('hub.challenge')
    if hub_mode=='subscribe' and hub_token==VERIFY_TOKEN:
        logging.info("Weryfikacja GET OK!")
        return Response(hub_challenge, status=200)
    else:
        logging.warning("Weryfikacja GET FAILED.")
        return Response("Verification failed", status=403)

@app.route('/webhook', methods=['POST'])
def webhook_handle():
    """Obsługuje przychodzące zdarzenia z Facebooka (POST request)."""
    logging.info(f"\n{'='*30} {datetime.datetime.now():%Y-%m-%d %H:%M:%S} POST {'='*30}")
    raw_data = request.data.decode('utf-8')
    data = None
    try:
        data = json.loads(raw_data)
        logging.debug(f"Odebrane dane: {json.dumps(data, indent=2)}")
        if data and data.get("object") == "page":
            for entry in data.get("entry", []):
                for event in entry.get("messaging", []):
                    sender_id = event.get("sender", {}).get("id")
                    recipient_id = event.get("recipient", {}).get("id")
                    if not sender_id or not recipient_id or sender_id == recipient_id:
                        continue # Skip invalid or echo events

                    logging.info(f"--- Zdarzenie dla PSID: {sender_id} ---")
                    history, context = load_history(sender_id)
                    last_iso = context.get('last_proposed_slot_iso')
                    is_context = bool(last_iso)
                    if is_context:
                        logging.info(f"    Aktywny kontekst: {last_iso}")

                    if message_data := event.get("message"):
                        if message_data.get("is_echo"): continue

                        user_input = None
                        user_content = None
                        history_saved = False # Flaga zapisu historii po intencji

                        if text := message_data.get("text"):
                            user_input = text.strip()
                            logging.info(f"      Txt: '{user_input}'")
                            if not user_input: continue
                            user_content = Content(role="user", parts=[Part.from_text(user_input)])
                        elif attachments := message_data.get("attachments"):
                            att_type = attachments[0].get('type','?')
                            logging.info(f"      Załącznik: {att_type}.")
                            user_content = Content(role="user", parts=[Part.from_text(f"[Załącznik:{att_type}]")])
                            msg = "Nie obsługuję załączników."
                            send_message(sender_id, msg)
                            model_resp = Content(role="model", parts=[Part.from_text(msg)])
                            save_history(sender_id, history + [user_content, model_resp])
                            continue
                        else:
                            logging.warning(f"      Nieznany typ msg: {message_data}")
                            user_content = Content(role="user", parts=[Part.from_text("[Nieznany typ]")])
                            msg = "Nie rozumiem."
                            send_message(sender_id, msg)
                            model_resp = Content(role="model", parts=[Part.from_text(msg)])
                            save_history(sender_id, history + [user_content, model_resp])
                            continue

                        # Inicjalizacja zmiennych dla cyklu request/response
                        action = None
                        msg_now = None
                        msg_result = None
                        ctx_save = None
                        model_resp = None
                        pref = 'any'
                        day = None
                        hour = None

                        if ENABLE_TYPING_DELAY and user_input:
                            delay = max(MIN_TYPING_DELAY_SECONDS, min(MAX_TYPING_DELAY_SECONDS, len(user_input)/TYPING_CHARS_PER_SECOND))
                            logging.info(f"      Typing... ({delay:.2f}s)")
                            time.sleep(delay)

                        # Główna logika decyzyjna
                        if is_context and last_iso: # SCENARIUSZ 1: Feedback
                            logging.info(f"      SCENARIUSZ: Feedback dla {last_iso}")
                            last_proposal = history[-1].parts[0].text if history and history[-1].role=='model' else "?"
                            decision = get_gemini_feedback_decision(sender_id, user_input, history, last_proposal)

                            if decision == "[ACCEPT]": action = 'book'
                            elif decision and decision.startswith("[REJECT_FIND_NEXT"):
                                action = 'find_and_propose'
                                m_pref = re.search(r"PREFERENCE='([^']*)'", decision)
                                pref = m_pref.group(1) if m_pref else 'any'
                                # Poprawione parsowanie dla DAY i HOUR
                                if pref in ['specific_day', 'specific_datetime']:
                                    m_day=re.search(r"DAY='([^']*)'",decision)
                                    day=m_day.group(1) if m_day else None
                                if pref in ['specific_hour', 'specific_datetime']:
                                    m_hour=re.search(r"HOUR='(\d+)'",decision)
                                    hour=int(m_hour.group(1)) if m_hour and m_hour.group(1).isdigit() else None
                                logging.info(f"      Odrzucono. Pref: {pref}, Dzień: {day}, Godz: {hour}")
                                msg_now = "Rozumiem. Poszukam innego terminu."
                            elif decision == "[CLARIFY]":
                                action='send_clarification'
                                msg_result="Nie jestem pewien. Czy termin pasuje?"
                                ctx_save={'role':'system','type':'last_proposal','slot_iso':last_iso}
                            else:
                                action='send_error'; msg_result="Problem z przetworzeniem."
                            if action != 'send_clarification': ctx_save = None
                        else: # SCENARIUSZ 2: Normalna rozmowa
                            logging.info(f"      SCENARIUSZ: Normalna rozmowa.")
                            response = get_gemini_general_response(sender_id, user_input, history)
                            if response:
                                if INTENT_SCHEDULE_MARKER in response:
                                    logging.info(f"      AI wykryło intencję [{INTENT_SCHEDULE_MARKER}].")
                                    action = 'find_and_propose'
                                    text_before = response.split(INTENT_SCHEDULE_MARKER,1)[0].strip()
                                    msg_now = text_before or "Sprawdzę terminy."
                                    pref = 'any'; day = None; hour = None # Reset preferencji
                                    model_resp = Content(role="model", parts=[Part.from_text(msg_now)])
                                else:
                                    action = 'send_gemini_response'
                                    msg_result = response
                            else:
                                action = 'send_error'
                                msg_result = "Nie mogę wygenerować odpowiedzi."

                        logging.info(f"      Akcja: {action}")
                        if msg_now:
                            send_message(sender_id, msg_now)
                            if action == 'find_and_propose' and model_resp:
                                save_history(sender_id, history + [user_content, model_resp])
                                history.extend([user_content, model_resp]) # Aktualizuj historię w pamięci
                                history_saved = True

                        # Wykonanie akcji
                        if action == 'book':
                            try:
                                tz = _get_timezone()
                                start = datetime.datetime.fromisoformat(last_iso).astimezone(tz)
                                if is_slot_actually_free(start, TARGET_CALENDAR_ID): # Weryfikacja przed rezerwacją
                                    end = start + datetime.timedelta(minutes=APPOINTMENT_DURATION_MINUTES)
                                    profile = get_user_profile(sender_id); name = profile.get('first_name', 'FB User') if profile else 'FB User'
                                    success, msg = book_appointment(TARGET_CALENDAR_ID, start, end, "Korepetycje (FB)", f"PSID:{sender_id}\nImię:{name}", name)
                                    msg_result = msg
                                    if not success: logging.warning(f"Rezerwacja API nieudana: {msg}")
                                else:
                                    logging.warning(f"[{sender_id}] Slot {last_iso} zajęty!")
                                    msg_result="Niestety, ten termin został właśnie zajęty. Szukamy innego?"
                                ctx_save = None
                            except Exception as book_err:
                                logging.error(f"!!! BŁĄD rezerwacji: {book_err}", exc_info=True)
                                msg_result="Błąd systemu rezerwacji."; ctx_save = None

                        elif action == 'find_and_propose':
                            try:
                                tz = _get_timezone(); now = datetime.datetime.now(tz); search_start = now
                                if last_iso and pref != 'any':
                                    try: # Ustalanie search_start wg preferencji
                                        last_dt = datetime.datetime.fromisoformat(last_iso).astimezone(tz); base_start = last_dt + datetime.timedelta(minutes=10)
                                        if pref == 'later': search_start = base_start + datetime.timedelta(hours=1); if last_dt.weekday()<5 and last_dt.hour<PREFERRED_WEEKDAY_START_HOUR: search_start = max(search_start, tz.localize(datetime.datetime.combine(last_dt.date(), datetime.time(PREFERRED_WEEKDAY_START_HOUR,0))))
                                        elif pref == 'earlier': search_start = now
                                        elif pref == 'next_day': search_start = tz.localize(datetime.datetime.combine(last_dt.date()+datetime.timedelta(days=1), datetime.time(WORK_START_HOUR,0)))
                                        elif pref in ['specific_day','specific_datetime'] and day:
                                            try: wd=POLISH_WEEKDAYS.index(day); ahead=(wd-now.weekday()+7)%7; if ahead==0 and now.time()>=datetime.time(WORK_END_HOUR,0): ahead=7
                                            search_start = tz.localize(datetime.datetime.combine(now.date()+datetime.timedelta(days=ahead), datetime.time(WORK_START_HOUR,0)))
                                            except ValueError: logging.warning(f"Zły dzień: {day}."); search_start = now
                                        elif pref == 'specific_hour': search_start = now
                                        search_start = max(search_start, now)
                                    except Exception as date_err: logging.error(f"Błąd search_start: {date_err}", exc_info=True); search_start = now
                                else: search_start = now

                                logging.info(f"      Szukanie zakresów od: {search_start:%Y-%m-%d %H:%M:%S %Z}")
                                search_end = tz.localize(datetime.datetime.combine((search_start + datetime.timedelta(days=MAX_SEARCH_DAYS)).date(), datetime.time(WORK_END_HOUR, 0)))

                                free_ranges = get_free_time_ranges(TARGET_CALENDAR_ID, search_start, search_end)
                                if free_ranges:
                                    relevant_ranges = free_ranges
                                    if pref in ['specific_hour','specific_datetime'] and hour is not None:
                                        potential_ranges = [r for r in free_ranges if r['start'].hour <= hour < r['end'].hour or (hour == r['start'].hour and r['start'].minute == 0 and hour < r['end'].hour)]
                                        if potential_ranges: relevant_ranges = potential_ranges; logging.info(f"Przefiltrowano zakresy do godz. {hour}. Liczba: {len(relevant_ranges)}")
                                        else: logging.info(f"Brak zakresów o godz. {hour}.")
                                    # Dodatkowe filtrowanie jeśli podano tylko dzień (w pref=specific_day lub specific_datetime)
                                    elif pref in ['specific_day', 'specific_datetime'] and day:
                                         # Już uwzględnione w search_start, ale można dodać warunek na weekday dla pewności
                                         try:
                                             target_wd_idx = POLISH_WEEKDAYS.index(day)
                                             relevant_ranges = [r for r in relevant_ranges if r['start'].weekday() == target_wd_idx]
                                             logging.info(f"Przefiltrowano zakresy do dnia: {day}. Liczba: {len(relevant_ranges)}")
                                         except ValueError:
                                             logging.warning(f"Ignoruję nieznany dzień '{day}' przy filtrowaniu zakresów.")


                                    if not relevant_ranges:
                                        logging.info("      Brak zakresów po filtrowaniu."); msg_result = "Niestety, brak terminów pasujących do preferencji."; ctx_save = None
                                    else:
                                        logging.info(f"      Przekazanie {len(relevant_ranges)} zakresów do AI...")
                                        proposal_text, proposed_iso = get_gemini_slot_proposal(sender_id, history, relevant_ranges)
                                        verified_iso = None
                                        if proposal_text and proposed_iso:
                                            try:
                                                prop_start = datetime.datetime.fromisoformat(proposed_iso).astimezone(tz)
                                                if is_slot_actually_free(prop_start, TARGET_CALENDAR_ID):
                                                    verified_iso = proposed_iso; msg_result = proposal_text; ctx_save = {'role':'system','type':'last_proposal','slot_iso':verified_iso}
                                                    logging.info(f"      AI wygenerowało poprawny slot: {verified_iso}")
                                                else: logging.warning(f"[{sender_id}] AI wygenerowało zajęty ({proposed_iso})! Fallback.")
                                            except ValueError: logging.error(f"!!! AI Error: Zły format ISO '{proposed_iso}'. Fallback.");
                                        else: logging.warning(f"[{sender_id}] AI nie zwróciło propozycji. Fallback.")

                                        if not verified_iso: # Fallback
                                            logging.info(f"      Fallback: szukanie...")
                                            fallback_slot = None
                                            for r in relevant_ranges: # Przeszukaj relevant_ranges
                                                 possible_slots = get_valid_start_times(TARGET_CALENDAR_ID, r['start'], r['end'])
                                                 # Zastosuj filtr godziny również w fallbacku
                                                 if pref in ['specific_hour','specific_datetime'] and hour is not None:
                                                     possible_slots = [s for s in possible_slots if s.hour == hour]
                                                 if possible_slots:
                                                     fallback_slot = possible_slots[0]; break
                                            if fallback_slot:
                                                fallback_iso = fallback_slot.isoformat()
                                                msg_result = f"Proponuję najbliższy termin: {format_slot_for_user(fallback_slot)}. Pasuje?"
                                                ctx_save = {'role': 'system', 'type': 'last_proposal', 'slot_iso': fallback_iso}; logging.info(f"      Fallback wybrał: {fallback_iso}")
                                            else: logging.error(f"[{sender_id}] Fallback nie znalazł slotów!"); msg_result = "Problem ze znalezieniem terminu."; ctx_save = None
                                else: logging.info("      Brak zakresów."); msg_result = "Brak wolnych terminów."; ctx_save = None
                            except Exception as find_err: logging.error(f"!!! BŁĄD find/propose: {find_err}", exc_info=True); msg_result = "Błąd sprawdzania dostępności."; ctx_save = None
                        elif action == 'send_gemini_response' or action == 'send_clarification' or action == 'send_error': pass
                        else: logging.error(f"!!! Nierozpoznana akcja: {action}"); msg_result = "Błąd bota."; ctx_save = None

                        if msg_result:
                             send_message(sender_id, msg_result)
                             if not model_resp: model_resp = Content(role="model", parts=[Part.from_text(msg_result)])
                        if user_content and not history_saved:
                             history_to_save = history + [user_content]
                             if model_resp: history_to_save.append(model_resp)
                             logging.info(f"      Zapisuję historię. Kontekst: {ctx_save}"); save_history(sender_id, history_to_save, context_to_save=ctx_save)
                        elif not user_content: logging.warning(f"[{sender_id}] Brak user_content do zapisu.")
                        elif history_saved:
                             logging.info(f"      Historia zapisana. Kontekst dla nast. kroku: {ctx_save}")
                             if ctx_save: latest_hist, _ = load_history(sender_id); save_history(sender_id, latest_hist, context_to_save=ctx_save)
                    elif event.get("postback"):
                         data = event["postback"]; payload = data.get("payload"); title = data.get("title", payload); logging.info(f"    Postback: T:'{title}', P:'{payload}'")
                         text = f"Kliknięto: '{title}' ({payload})."; user_c = Content(role="user", parts=[Part.from_text(text)])
                         resp = get_gemini_general_response(sender_id, text, history)
                         if resp and INTENT_SCHEDULE_MARKER not in resp: send_message(sender_id, resp); model_c = Content(role="model", parts=[Part.from_text(resp)]); save_history(sender_id, history + [user_c, model_c])
                         elif resp and INTENT_SCHEDULE_MARKER in resp: txt_before = resp.split(INTENT_SCHEDULE_MARKER,1)[0].strip(); msg = txt_before + "\nChcesz szukać terminu?" if txt_before else "Chcesz umówić?"; send_message(sender_id, msg); model_c = Content(role="model", parts=[Part.from_text(msg)]); save_history(sender_id, history + [user_c, model_c])
                         else: msg = "Problem."; send_message(sender_id, msg); model_c = Content(role="model", parts=[Part.from_text(msg)]); save_history(sender_id, history + [user_c, model_c])
                    elif event.get("read"): logging.info(f"    Odczytane.")
                    elif event.get("delivery"): pass
                    else: logging.warning(f"    Nieobsługiwany event: {json.dumps(event)}")
            return Response("EVENT_RECEIVED", status=200)
        else:
            logging.warning(f"Otrzymano POST nie 'page': {data.get('object') if data else 'Brak'}")
            return Response("OK", status=200)
    except json.JSONDecodeError as e:
        logging.error(f"!!! BŁĄD JSON: {e}\nDane: {raw_data[:500]}")
        return Response("Invalid JSON", status=400)
    except Exception as e:
        logging.error(f"!!! KRYTYCZNY BŁĄD POST: {e}", exc_info=True)
        return Response("ERROR", status=200)

# =====================================================================
# === URUCHOMIENIE SERWERA APLIKACJI ==================================
# =====================================================================

if __name__ == '__main__':
    ensure_dir(HISTORY_DIR)
    port = int(os.environ.get("PORT", 8080))
    debug = os.environ.get("FLASK_DEBUG", "False").lower() in ("true", "1", "yes")
    print("\n" + "="*50 + "\n--- START KONFIGURACJI BOTA ---")
    if not VERIFY_TOKEN or VERIFY_TOKEN == "KOLAGEN": print("!!! OSTRZ.: FB_VERIFY_TOKEN domyślny!")
    else: print("  FB_VERIFY_TOKEN: OK")
    if not PAGE_ACCESS_TOKEN: print("\n!!! KRYTYCZNE: FB_PAGE_ACCESS_TOKEN PUSTY!\n");
    elif len(PAGE_ACCESS_TOKEN) < 50: print("\n!!! KRYTYCZNE: FB_PAGE_ACCESS_TOKEN ZBYT KRÓTKI!\n")
    else:
        print("  FB_PAGE_ACCESS_TOKEN: Ustawiony (OK)")
        if PAGE_ACCESS_TOKEN=="EACNAHFzEhkUBO4ypcoyQfWIgNc0YLZA1aCr9n3BzpvSJLoBTJnv5rWZBmc7HlqF6uUWt1uAp6aDZB8ZAb0RRT45qVIfGnciQX6wBKrZColGARfVLXP5Ic6Ptrj5AUvom4Rt12hyBxcjIJGes76fvdvBhiBZCJ0ZCVfkQMZBZCBatJshSZA8hFuRyKd58b50wkhVCMZCuwZDZD":
             print("\n!!! UWAGA: Używany jest DOMYŚLNY PAGE_ACCESS_TOKEN!\n")
    print(f"  Historia: {HISTORY_DIR}"); print(f"  Projekt Vertex: {PROJECT_ID}"); print(f"  Lokalizacja Vertex: {LOCATION}")
    print(f"  Model Vertex: {MODEL_ID}"); print(f"  Kalendarz ID: {TARGET_CALENDAR_ID}")
    print(f"  Symulacja pisania: {'On' if ENABLE_TYPING_DELAY else 'Off'}")
    if not gemini_model: print("\n!!! OSTRZ.: Model Gemini NIE załadowany!\n")
    else: print(f"  Model Gemini AI ({MODEL_ID}): OK")
    cal_service = get_calendar_service()
    if not cal_service and os.path.exists(SERVICE_ACCOUNT_FILE): print("\n!!! OSTRZ.: Google Calendar NIE zainicjowany.\n")
    elif cal_service: print("  Usługa Google Calendar: OK")
    elif not os.path.exists(SERVICE_ACCOUNT_FILE): print("  Plik klucza Calendar: BRAK")
    print("--- KONIEC KONFIGURACJI BOTA ---\n" + "="*50 + "\n")
    print(f"Start serwera Flask: port={port}, debug={debug}...")
    logging.getLogger('googleapiclient.discovery_cache').setLevel(logging.ERROR)
    if not debug:
        try: from waitress import serve; print("Start Waitress..."); serve(app, host='0.0.0.0', port=port)
        except ImportError: print("Waitress brak. Start serwera dev."); app.run(host='0.0.0.0', port=port, debug=False)
    else:
        print("Start serwera dev w trybie DEBUG...")
        logging.getLogger().setLevel(logging.DEBUG); print("Logowanie DEBUG włączone.")
        app.run(host='0.0.0.0', port=port, debug=True)
