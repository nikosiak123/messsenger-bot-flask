# -*- coding: utf-8 -*-

# verify_server.py (połączony kod z AI wybierającym, proponującym i interpretującym potrzebę umówienia terminu)

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
from collections import defaultdict # Potrzebne do grupowania zakresów

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
# MAX_SLOTS_FOR_AI nie jest już potrzebne w tym podejściu

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
    try:
        os.makedirs(directory)
        logging.info(f"Utworzono katalog: {directory}")
    except OSError as e:
        if e.errno != errno.EEXIST:
            logging.error(f"Błąd tworzenia katalogu {directory}: {e}", exc_info=True)
            raise

def get_user_profile(psid):
    if not PAGE_ACCESS_TOKEN:
        logging.warning(f"[{psid}] Brak skonfigurowanego PAGE_ACCESS_TOKEN. Pobieranie profilu niemożliwe.")
        return None
    elif len(PAGE_ACCESS_TOKEN) < 50:
        logging.warning(f"[{psid}] PAGE_ACCESS_TOKEN wydaje się za krótki. Profil niepobrany.")
        return None

    USER_PROFILE_API_URL_TEMPLATE = f"https://graph.facebook.com/v19.0/{psid}?fields=first_name,last_name,profile_pic&access_token={PAGE_ACCESS_TOKEN}"
    logging.info(f"--- [{psid}] Pobieranie profilu...")
    try:
        r = requests.get(USER_PROFILE_API_URL_TEMPLATE, timeout=10)
        r.raise_for_status()
        data = r.json()
        if 'error' in data:
            logging.error(f"BŁĄD FB API (profil) {psid}: {data['error']}")
            if "access token" in data['error'].get('message', '').lower():
                 logging.error(f"[{psid}] Błąd tokena dostępu przy pobieraniu profilu. Sprawdź poprawność PAGE_ACCESS_TOKEN.")
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
                response_json = http_err.response.json()
                logging.error(f"Odpowiedź FB (błąd HTTP): {response_json}")
                if "access token" in response_json.get('error',{}).get('message', '').lower():
                     logging.error(f"[{psid}] Błąd tokena dostępu (HTTP {http_err.response.status_code}) przy pobieraniu profilu. Sprawdź poprawność PAGE_ACCESS_TOKEN.")
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
    filepath = os.path.join(HISTORY_DIR, f"{user_psid}.json")
    history = []
    context = {}
    if not os.path.exists(filepath):
        return history, context
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            history_data = json.load(f)

        if not isinstance(history_data, list):
            logging.error(f"BŁĄD [{user_psid}]: Plik historii nie zawiera listy.")
            return [], {}

        last_system_entry_index = -1
        for i in range(len(history_data) - 1, -1, -1):
            entry = history_data[i]
            if isinstance(entry, dict) and entry.get('role') == 'system' and entry.get('type') == 'last_proposal':
                last_system_entry_index = i
                break

        for i, msg_data in enumerate(history_data):
            if isinstance(msg_data, dict) and 'role' in msg_data and msg_data['role'] in ('user', 'model') and \
               'parts' in msg_data and isinstance(msg_data['parts'], list) and msg_data['parts']:
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
            elif i == last_system_entry_index:
                if 'slot_iso' in msg_data:
                    context['last_proposed_slot_iso'] = msg_data['slot_iso']
                    context['message_index_in_file'] = i
                    logging.info(f"[{user_psid}] Odczytano AKTUALNY kontekst: last_proposed_slot_iso (na pozycji {i} w pliku)")
                else:
                    logging.warning(f"Ostrz. [{user_psid}]: Poprawny wpis systemowy, ale brak 'slot_iso' (idx {i})")
            elif isinstance(msg_data, dict) and msg_data.get('role') == 'system':
                 logging.info(f"[{user_psid}] Pominięto stary kontekst systemowy na indeksie {i}")
            else:
                logging.warning(f"Ostrz. [{user_psid}]: Pominięto niepoprawny wpis w historii (idx {i}): {msg_data}")

        logging.info(f"[{user_psid}] Wczytano historię: {len(history)} wiadomości (user/model).")
        if 'message_index_in_file' in context and context['message_index_in_file'] != len(history_data) - 1:
            logging.info(f"[{user_psid}] Kontekst 'last_proposed_slot_iso' jest nieaktualny (nie na końcu pliku). Resetowanie.")
            context = {}

        return history, context

    except (json.JSONDecodeError, KeyError, IndexError, TypeError) as e:
        logging.error(f"BŁĄD [{user_psid}] parsowania historii: {e}.", exc_info=True)
        return [], {}
    except Exception as e:
        logging.error(f"BŁĄD [{user_psid}] wczytywania historii: {e}", exc_info=True)
        return [], {}


def save_history(user_psid, history, context_to_save=None):
    ensure_dir(HISTORY_DIR)
    filepath = os.path.join(HISTORY_DIR, f"{user_psid}.json")
    temp_filepath = f"{filepath}.tmp"
    history_data = []

    try:
        max_messages_to_save = MAX_HISTORY_TURNS * 2
        start_index = max(0, len(history) - max_messages_to_save)
        history_to_save = history[start_index:]
        if len(history) > max_messages_to_save:
            logging.info(f"[{user_psid}] Historia przycięta DO ZAPISU: {len(history_to_save)} wiadomości (z {len(history)}).")

        for msg in history_to_save:
             if isinstance(msg, Content) and hasattr(msg, 'role') and msg.role in ('user', 'model') and hasattr(msg, 'parts') and isinstance(msg.parts, list):
                parts_data = [{'text': part.text} for part in msg.parts if isinstance(part, Part) and hasattr(part, 'text')]
                if parts_data:
                    history_data.append({'role': msg.role, 'parts': parts_data})
             else:
                 logging.warning(f"Ostrz. [{user_psid}]: Pomijanie nieprawidłowego obiektu Content podczas zapisu: {type(msg)}")

        if context_to_save and isinstance(context_to_save, dict):
             history_data.append(context_to_save)
             logging.info(f"[{user_psid}] Dodano kontekst {context_to_save.get('type')} do zapisu.")

        with open(temp_filepath, 'w', encoding='utf-8') as f:
            json.dump(history_data, f, ensure_ascii=False, indent=2)
        os.replace(temp_filepath, filepath)
        logging.info(f"[{user_psid}] Zapisano historię/kontekst ({len(history_data)} wpisów) do: {filepath}")

    except Exception as e:
        logging.error(f"BŁĄD [{user_psid}] zapisu historii/kontekstu: {e}", exc_info=True)
        if os.path.exists(temp_filepath):
            try:
                os.remove(temp_filepath)
                logging.info(f"    Usunięto plik tymczasowy {temp_filepath} po błędzie zapisu.")
            except OSError as remove_e:
                logging.error(f"    Nie można usunąć pliku tymczasowego {temp_filepath} po błędzie zapisu: {remove_e}")

def _get_timezone():
    global _tz
    if _tz is None:
        try:
            _tz = pytz.timezone(CALENDAR_TIMEZONE)
            logging.info(f"Ustawiono strefę czasową: {CALENDAR_TIMEZONE}")
        except pytz.exceptions.UnknownTimeZoneError:
            logging.error(f"BŁĄD: Strefa czasowa '{CALENDAR_TIMEZONE}' jest nieznana. Używam UTC.")
            _tz = pytz.utc
    return _tz

def get_calendar_service():
    global _calendar_service
    if _calendar_service:
        return _calendar_service
    if not os.path.exists(SERVICE_ACCOUNT_FILE):
        logging.error(f"BŁĄD KRYTYCZNY: Brak pliku klucza konta usługi: '{SERVICE_ACCOUNT_FILE}'")
        return None
    try:
        creds = service_account.Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=CALENDAR_SCOPES)
        service = build('calendar', 'v3', credentials=creds)
        logging.info("Utworzono połączenie z Google Calendar API.")
        _calendar_service = service
        return service
    except HttpError as error:
        logging.error(f"Błąd API podczas tworzenia usługi Google Calendar: {error}", exc_info=True)
        return None
    except Exception as e:
        logging.error(f"Nieoczekiwany błąd podczas tworzenia usługi Google Calendar: {e}", exc_info=True)
        return None

def parse_event_time(event_time_data, default_tz):
    if not event_time_data:
        return None
    if 'dateTime' in event_time_data:
        dt_str = event_time_data['dateTime']
        try:
            dt = datetime.datetime.fromisoformat(dt_str.replace('Z', '+00:00'))
        except ValueError:
            logging.warning(f"Ostrz.: Nie udało się sparsować dateTime: {dt_str}")
            return None

        if dt.tzinfo is None:
            logging.warning(f"Ostrz.: dateTime {dt_str} nie ma informacji o strefie. Zakładam UTC.")
            dt = pytz.utc.localize(dt)
        return dt.astimezone(default_tz)

    elif 'date' in event_time_data:
        try:
            return datetime.date.fromisoformat(event_time_data['date'])
        except ValueError:
            logging.warning(f"Ostrz.: Nie udało się sparsować date: {event_time_data['date']}")
            return None
    return None

# ZMIANA: Funkcja generująca ZAKRESY wolnego czasu
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

    logging.info(f"Szukanie zakresów wolnego czasu (min. {APPOINTMENT_DURATION_MINUTES} min) w '{calendar_id}'")
    logging.info(f"Zakres: od {start_datetime.strftime('%Y-%m-%d %H:%M:%S %Z')} do {end_datetime.strftime('%Y-%m-%d %H:%M:%S %Z')}")

    try:
        events_result = service.events().list(
            calendarId=calendar_id,
            timeMin=start_datetime.isoformat(),
            timeMax=end_datetime.isoformat(),
            singleEvents=True,
            orderBy='startTime'
        ).execute()
        events = events_result.get('items', [])
        logging.info(f"Pobrano {len(events)} wydarzeń z kalendarza w podanym zakresie.")
    except HttpError as error:
        logging.error(f'Błąd API podczas pobierania wydarzeń: {error}', exc_info=True)
        return []
    except Exception as e:
        logging.error(f"Nieoczekiwany błąd podczas pobierania wydarzeń: {e}", exc_info=True)
        return []

    free_ranges = []
    current_time = start_datetime
    appointment_duration_td = datetime.timedelta(minutes=APPOINTMENT_DURATION_MINUTES)

    # Posortowane czasy rozpoczęcia i zakończenia zajętych wydarzeń
    busy_times = []
    for event in events:
        start = parse_event_time(event.get('start'), tz)
        end = parse_event_time(event.get('end'), tz)
        if isinstance(start, datetime.datetime) and isinstance(end, datetime.datetime):
            # Interesują nas tylko wydarzenia w głównym zakresie
            if end > start_datetime and start < end_datetime:
                 # Ograniczamy do zakresu start/end dla pewności
                effective_start = max(start, start_datetime)
                effective_end = min(end, end_datetime)
                if effective_start < effective_end:
                    busy_times.append({'start': effective_start, 'end': effective_end})
        elif isinstance(start, datetime.date): # Obsługa wydarzeń całodniowych
            event_date = start
            day_start_limit = tz.localize(datetime.datetime.combine(event_date, datetime.time(0, 0)))
            day_end_limit = tz.localize(datetime.datetime.combine(event_date, datetime.time(23, 59, 59)))
            # Sprawdź, czy dzień wydarzenia całodniowego pokrywa się z zakresem
            if day_end_limit > start_datetime and day_start_limit < end_datetime:
                effective_start = max(day_start_limit, start_datetime)
                effective_end = min(day_end_limit, end_datetime)
                if effective_start < effective_end:
                     busy_times.append({'start': effective_start, 'end': effective_end})


    busy_times.sort(key=lambda x: x['start'])

    # Scal nachodzące na siebie zajęte przedziały
    merged_busy_times = []
    if busy_times:
        merged_busy_times.append(busy_times[0])
        for current_busy in busy_times[1:]:
            last_merged = merged_busy_times[-1]
            if current_busy['start'] <= last_merged['end']:
                last_merged['end'] = max(last_merged['end'], current_busy['end'])
            else:
                merged_busy_times.append(current_busy)

    # Znajdź luki między zajętymi przedziałami
    for busy in merged_busy_times:
        free_start = current_time
        free_end = busy['start']
        # Sprawdź, czy zakres jest w dozwolonych godzinach pracy i czy jest wystarczająco długi
        range_start = max(free_start, tz.localize(datetime.datetime.combine(free_start.date(), datetime.time(WORK_START_HOUR, 0))))
        range_end = min(free_end, tz.localize(datetime.datetime.combine(free_end.date(), datetime.time(WORK_END_HOUR, 0))))

        # Dodaj zakresy dla każdego dnia osobno, jeśli luka obejmuje zmianę dnia
        current_check_date = range_start.date()
        final_end_date = range_end.date()

        while current_check_date <= final_end_date:
             day_start_work = tz.localize(datetime.datetime.combine(current_check_date, datetime.time(WORK_START_HOUR, 0)))
             day_end_work = tz.localize(datetime.datetime.combine(current_check_date, datetime.time(WORK_END_HOUR, 0)))

             effective_range_start = max(range_start, day_start_work)
             effective_range_end = min(range_end, day_end_work)

             if effective_range_end > effective_range_start and effective_range_end - effective_range_start >= appointment_duration_td:
                  free_ranges.append({'start': effective_range_start, 'end': effective_range_end})
                  logging.debug(f"  + Znaleziono zakres wolny (między zajętymi): {effective_range_start.strftime('%Y-%m-%d %H:%M')} - {effective_range_end.strftime('%Y-%m-%d %H:%M')}")

             current_check_date += datetime.timedelta(days=1)
             # Ustaw range_start na początek następnego dnia, aby kontynuować sprawdzanie
             range_start = tz.localize(datetime.datetime.combine(current_check_date, datetime.time(0, 0)))


        current_time = max(current_time, busy['end']) # Przesuń wskaźnik za zajęty okres

    # Sprawdź zakres po ostatnim zajętym wydarzeniu do końca globalnego zakresu
    free_start = current_time
    free_end = end_datetime
    range_start = max(free_start, tz.localize(datetime.datetime.combine(free_start.date(), datetime.time(WORK_START_HOUR, 0))))
    range_end = min(free_end, tz.localize(datetime.datetime.combine(free_end.date(), datetime.time(WORK_END_HOUR, 0))))

    current_check_date = range_start.date()
    final_end_date = range_end.date()

    while current_check_date <= final_end_date:
        day_start_work = tz.localize(datetime.datetime.combine(current_check_date, datetime.time(WORK_START_HOUR, 0)))
        day_end_work = tz.localize(datetime.datetime.combine(current_check_date, datetime.time(WORK_END_HOUR, 0)))

        effective_range_start = max(range_start, day_start_work)
        effective_range_end = min(range_end, day_end_work)

        if effective_range_end > effective_range_start and effective_range_end - effective_range_start >= appointment_duration_td:
            free_ranges.append({'start': effective_range_start, 'end': effective_range_end})
            logging.debug(f"  + Znaleziono zakres wolny (po ostatnim zajętym): {effective_range_start.strftime('%Y-%m-%d %H:%M')} - {effective_range_end.strftime('%Y-%m-%d %H:%M')}")

        current_check_date += datetime.timedelta(days=1)
        range_start = tz.localize(datetime.datetime.combine(current_check_date, datetime.time(0, 0)))


    logging.info(f"Znaleziono {len(free_ranges)} zakresów wolnego czasu.")
    return free_ranges

# NOWA funkcja do formatowania ZAKRESÓW dla AI
def format_ranges_for_ai(ranges):
    """Formatuje listę zakresów na czytelny tekst dla AI."""
    if not ranges:
        return "Brak dostępnych zakresów czasowych w najbliższym czasie."

    # Grupuj zakresy po dacie
    ranges_by_date = defaultdict(list)
    for r in ranges:
        # Klucz to obiekt date, wartość to lista {'start_time': 'HH:MM', 'end_time': 'HH:MM'}
        range_date = r['start'].date()
        ranges_by_date[range_date].append({
            'start_time': r['start'].strftime('%H:%M'),
            'end_time': r['end'].strftime('%H:%M')
        })

    formatted_list = ["Oto dostępne zakresy czasowe, w których można umówić wizytę (trwającą 60 minut). Wybierz jeden zakres i wygeneruj w nim konkretny termin, preferując pełne godziny:"]

    # Sortuj daty
    sorted_dates = sorted(ranges_by_date.keys())

    for d in sorted_dates:
        day_name = POLISH_WEEKDAYS[d.weekday()]
        date_str = d.strftime('%d.%m.%Y')
        time_parts = []
        for time_range in ranges_by_date[d]:
            time_parts.append(f"{time_range['start_time']}-{time_range['end_time']}")
        formatted_list.append(f"- {day_name}, {date_str}: {'; '.join(time_parts)}")

    return "\n".join(formatted_list)

# NOWA funkcja do weryfikacji, czy dany slot jest FAKTYCZNIE wolny
# Używa oryginalnej logiki get_free_slots (przemianowanej)
def get_valid_start_times(calendar_id, check_start, check_end):
    """Zwraca listę DOKŁADNYCH dozwolonych czasów rozpoczęcia wizyty."""
    # Ta funkcja zawiera logikę z poprzedniej wersji get_free_slots,
    # która generuje konkretne 10-minutowe sloty.
    service = get_calendar_service()
    tz = _get_timezone()
    if not service: return []

    if check_start.tzinfo is None: check_start = tz.localize(check_start)
    else: check_start = check_start.astimezone(tz)
    if check_end.tzinfo is None: check_end = tz.localize(check_end)
    else: check_end = check_end.astimezone(tz)

    logging.debug(f"Pobieranie dokładnych slotów startowych w zakresie: {check_start.isoformat()} - {check_end.isoformat()}")

    try:
        events_result = service.events().list(
            calendarId=calendar_id, timeMin=check_start.isoformat(),
            timeMax=check_end.isoformat(), singleEvents=True,
            orderBy='startTime').execute()
        events = events_result.get('items', [])
    except Exception as e:
        logging.error(f"Błąd API przy pobieraniu wydarzeń dla weryfikacji slotu: {e}")
        return []

    valid_starts = []
    current_day = check_start.date()
    end_day = check_end.date()
    appointment_duration = datetime.timedelta(minutes=APPOINTMENT_DURATION_MINUTES)

    while current_day <= end_day:
        day_start_limit = tz.localize(datetime.datetime.combine(current_day, datetime.time(WORK_START_HOUR, 0)))
        day_end_limit = tz.localize(datetime.datetime.combine(current_day, datetime.time(WORK_END_HOUR, 0)))
        loop_check_start_time = max(check_start, day_start_limit)
        loop_check_end_time = min(check_end, day_end_limit)

        if loop_check_start_time >= loop_check_end_time:
            current_day += datetime.timedelta(days=1)
            continue

        busy_intervals = []
        for event in events:
            start = parse_event_time(event.get('start'), tz)
            end = parse_event_time(event.get('end'), tz)
            if isinstance(start, datetime.date):
                if start == current_day: busy_intervals.append({'start': day_start_limit, 'end': day_end_limit})
            elif isinstance(start, datetime.datetime) and isinstance(end, datetime.datetime):
                if end > loop_check_start_time and start < loop_check_end_time:
                    effective_start = max(start, loop_check_start_time)
                    effective_end = min(end, loop_check_end_time)
                    if effective_start < effective_end: busy_intervals.append({'start': effective_start, 'end': effective_end})

        if not busy_intervals: merged_busy_times = []
        else:
             busy_intervals.sort(key=lambda x: x['start'])
             merged_busy_times = [busy_intervals[0]]
             for current_busy in busy_intervals[1:]:
                 last_merged = merged_busy_times[-1]
                 if current_busy['start'] <= last_merged['end']: last_merged['end'] = max(last_merged['end'], current_busy['end'])
                 else: merged_busy_times.append(current_busy)

        potential_slot_start = loop_check_start_time
        for busy in merged_busy_times:
            busy_start = busy['start']
            busy_end = busy['end']
            while potential_slot_start + appointment_duration <= busy_start:
                 if potential_slot_start.minute % 10 == 0:
                    if potential_slot_start >= day_start_limit and potential_slot_start + appointment_duration <= day_end_limit:
                        valid_starts.append(potential_slot_start)
                 current_minute = potential_slot_start.minute
                 minutes_to_add = 10 - (current_minute % 10) if current_minute % 10 != 0 else 10
                 potential_slot_start += datetime.timedelta(minutes=minutes_to_add)
                 potential_slot_start = potential_slot_start.replace(second=0, microsecond=0)
            potential_slot_start = max(potential_slot_start, busy_end)

        while potential_slot_start + appointment_duration <= loop_check_end_time:
             if potential_slot_start.minute % 10 == 0:
                if potential_slot_start >= day_start_limit and potential_slot_start + appointment_duration <= day_end_limit:
                    valid_starts.append(potential_slot_start)
             current_minute = potential_slot_start.minute
             minutes_to_add = 10 - (current_minute % 10) if current_minute % 10 != 0 else 10
             potential_slot_start += datetime.timedelta(minutes=minutes_to_add)
             potential_slot_start = potential_slot_start.replace(second=0, microsecond=0)

        current_day += datetime.timedelta(days=1)

    # Zwróć posortowaną listę unikalnych datetime
    final_valid_starts = sorted(list(set(slot for slot in valid_starts if check_start <= slot < check_end)))
    logging.debug(f"Znaleziono {len(final_valid_starts)} dokładnych slotów startowych w podanym wąskim zakresie.")
    return final_valid_starts

def is_slot_actually_free(proposed_start_dt, calendar_id):
    """Sprawdza, czy DOKŁADNIE proponowany czas rozpoczęcia jest na liście ważnych slotów."""
    try:
        # Sprawdź w bardzo wąskim zakresie czasu (np. +/- 1 minuta) wokół proponowanego startu
        check_start = proposed_start_dt - datetime.timedelta(minutes=1)
        check_end = proposed_start_dt + datetime.timedelta(minutes=1)
        valid_starts_nearby = get_valid_start_times(calendar_id, check_start, check_end)
        # Sprawdź, czy proponowany czas startu jest DOKŁADNIE na liście
        is_valid = proposed_start_dt in valid_starts_nearby
        logging.info(f"Weryfikacja slotu {proposed_start_dt.isoformat()}: {'VALID' if is_valid else 'INVALID'}")
        return is_valid
    except Exception as e:
        logging.error(f"Błąd podczas weryfikacji slotu {proposed_start_dt.isoformat()}: {e}", exc_info=True)
        return False # Bezpieczniej założyć, że jest niepoprawny w razie błędu

def format_slot_for_user(slot_start):
    if not isinstance(slot_start, datetime.datetime):
        return ""
    try:
        day_index = slot_start.weekday()
        day_name = POLISH_WEEKDAYS[day_index]
        hour_str = slot_start.strftime('%#H') if os.name != 'nt' else slot_start.strftime('%H')
        try: hour_str = str(slot_start.hour)
        except Exception: pass # Ignoruj błąd formatowania godziny
        return f"{day_name}, {slot_start.strftime(f'%d.%m.%Y o {hour_str}:%M')}"
    except Exception as e:
        logging.error(f"Błąd formatowania slotu dla użytkownika: {e}", exc_info=True)
        return slot_start.isoformat()

# =====================================================================
# === Inicjalizacja Vertex AI =========================================
# =====================================================================

gemini_model = None
try:
    logging.info(f"Inicjalizowanie Vertex AI: Projekt={PROJECT_ID}, Lokalizacja={LOCATION}")
    vertexai.init(project=PROJECT_ID, location=LOCATION)
    logging.info("Inicjalizacja Vertex AI zakończona.")
    logging.info(f"Ładowanie modelu: {MODEL_ID}")
    gemini_model = GenerativeModel(MODEL_ID)
    logging.info(f"Model {MODEL_ID} załadowany pomyślnie.")
except Exception as e:
    logging.critical(f"KRYTYCZNY BŁĄD podczas inicjalizacji Vertex AI lub ładowania modelu {MODEL_ID}: {e}", exc_info=True)

GENERATION_CONFIG_DEFAULT = GenerationConfig(
    temperature=0.7, top_p=0.95, top_k=40, max_output_tokens=1024
)
# ZMIANA: Konfiguracja dla AI generującego termin z zakresu
GENERATION_CONFIG_PROPOSAL = GenerationConfig(
    temperature=0.5, # Nieco więcej kreatywności niż przy wyborze, ale nie za dużo
    top_p=0.95,
    top_k=40,
    max_output_tokens=1024 # Dajmy więcej miejsca na odpowiedź
)
GENERATION_CONFIG_FEEDBACK = GenerationConfig(
    temperature=0.1, top_p=0.95, top_k=40, max_output_tokens=100
)

SAFETY_SETTINGS = {
    HarmCategory.HARM_CATEGORY_HARASSMENT: HarmBlockThreshold.BLOCK_MEDIUM_AND_ABOVE,
    HarmCategory.HARM_CATEGORY_HATE_SPEECH: HarmBlockThreshold.BLOCK_MEDIUM_AND_ABOVE,
    HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT: HarmBlockThreshold.BLOCK_MEDIUM_AND_ABOVE,
    HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT: HarmBlockThreshold.BLOCK_MEDIUM_AND_ABOVE,
}


# =====================================================================
# === FUNKCJE WYSYŁANIA WIADOMOŚCI FB ================================
# =====================================================================

def _send_single_message(recipient_id, message_text):
    logging.info(f"--- Wysyłanie fragm. do {recipient_id} (dł: {len(message_text)}) ---")
    params = {"access_token": PAGE_ACCESS_TOKEN}
    payload = {"recipient": {"id": recipient_id}, "message": {"text": message_text}, "messaging_type": "RESPONSE"}
    if not PAGE_ACCESS_TOKEN:
         logging.error(f"!!! [{recipient_id}] Brak skonfigurowanego PAGE_ACCESS_TOKEN! Wiadomość NIE wysłana.")
         return False
    elif len(PAGE_ACCESS_TOKEN) < 50:
         logging.error(f"!!! [{recipient_id}] PAGE_ACCESS_TOKEN wydaje się za krótki! Wiadomość NIE wysłana.")
         return False
    try:
        r = requests.post(FACEBOOK_GRAPH_API_URL, params=params, json=payload, timeout=30)
        r.raise_for_status()
        response_json = r.json()
        if 'error' in response_json:
            logging.error(f"!!! BŁĄD FB API podczas wysyłania do {recipient_id}: {response_json['error']}")
            if "access token" in response_json['error'].get('message', '').lower():
                 logging.error(f"!!! [{recipient_id}] Wygląda na błąd tokena dostępu przy wysyłaniu. Sprawdź poprawność PAGE_ACCESS_TOKEN.")
            return False
        logging.info(f"--- Fragment wysłany pomyślnie do {recipient_id} ---")
        return True
    except requests.exceptions.Timeout:
        logging.error(f"!!! BŁĄD TIMEOUT podczas wysyłania do {recipient_id}")
        return False
    except requests.exceptions.HTTPError as http_err:
        logging.error(f"!!! BŁĄD HTTP {http_err.response.status_code} podczas wysyłania do {recipient_id}: {http_err}")
        if http_err.response is not None:
            try:
                response_json = http_err.response.json()
                logging.error(f"Odpowiedź FB (błąd HTTP): {response_json}")
                if "access token" in response_json.get('error',{}).get('message', '').lower():
                     logging.error(f"!!! [{recipient_id}] Wygląda na błąd tokena dostępu (HTTP {http_err.response.status_code}) przy wysyłaniu.")
            except json.JSONDecodeError:
                logging.error(f"Odpowiedź FB (błąd HTTP, nie JSON): {http_err.response.text}")
        return False
    except requests.exceptions.RequestException as req_err:
        logging.error(f"!!! BŁĄD RequestException podczas wysyłania do {recipient_id}: {req_err}", exc_info=True)
        return False
    except Exception as e:
        logging.error(f"!!! Niespodziewany BŁĄD podczas wysyłania do {recipient_id}: {e}", exc_info=True)
        return False

def send_message(recipient_id, full_message_text):
    if not full_message_text or not isinstance(full_message_text, str) or not full_message_text.strip():
        logging.warning(f"[{recipient_id}] Próba wysłania pustej lub nieprawidłowej wiadomości. Pominięto.")
        return
    message_len = len(full_message_text)
    logging.info(f"[{recipient_id}] Przygotowanie wiadomości (dł: {message_len}).")
    if message_len <= MESSAGE_CHAR_LIMIT:
        _send_single_message(recipient_id, full_message_text)
    else:
        chunks = []
        remaining_text = full_message_text
        logging.info(f"[{recipient_id}] Dzielenie wiadomości na fragmenty (limit: {MESSAGE_CHAR_LIMIT})...")
        while remaining_text:
            if len(remaining_text) <= MESSAGE_CHAR_LIMIT:
                chunks.append(remaining_text.strip()); break
            split_index = -1
            for delimiter in ['\n\n', '\n', '. ', '! ', '? ', ' ']:
                search_limit = MESSAGE_CHAR_LIMIT - (len(delimiter) - 1) if len(delimiter) > 1 else MESSAGE_CHAR_LIMIT
                temp_index = remaining_text.rfind(delimiter, 0, search_limit + len(delimiter))
                if temp_index != -1 and temp_index <= MESSAGE_CHAR_LIMIT :
                    split_index = temp_index + len(delimiter); break
            if split_index == -1:
                split_index = MESSAGE_CHAR_LIMIT
                logging.warning(f"[{recipient_id}] Nie znaleziono naturalnego miejsca podziału, cięcie na {MESSAGE_CHAR_LIMIT} znakach.")
            chunk = remaining_text[:split_index].strip()
            if chunk: chunks.append(chunk)
            remaining_text = remaining_text[split_index:].strip()
        num_chunks = len(chunks); logging.info(f"[{recipient_id}] Podzielono na {num_chunks} fragmentów.")
        send_success_count = 0
        for i, chunk in enumerate(chunks):
            logging.info(f"[{recipient_id}] Wysyłanie fragm. {i+1}/{num_chunks} (dł: {len(chunk)})...")
            if not _send_single_message(recipient_id, chunk):
                logging.error(f"!!! [{recipient_id}] Anulowano wysyłanie reszty wiadomości po błędzie na fragmencie {i+1}."); break
            send_success_count += 1
            if i < num_chunks - 1:
                logging.info(f"[{recipient_id}] Oczekiwanie {MESSAGE_DELAY_SECONDS}s przed następnym fragmentem..."); time.sleep(MESSAGE_DELAY_SECONDS)
        logging.info(f"--- [{recipient_id}] Zakończono wysyłanie {send_success_count}/{num_chunks} fragmentów. ---")

# =====================================================================
# === INSTRUKCJE SYSTEMOWE DLA AI =====================================
# =====================================================================

SYSTEM_INSTRUCTION_GENERAL = f"""Jesteś profesjonalnym i przyjaznym asystentem klienta 'Zakręcone Korepetycje'. Pomagasz w sprawach związanych z korepetycjami online.

**Twoje Główne Zadania:**
1.  Odpowiadaj na pytania dotyczące:
    *   Oferowanych przedmiotów (matematyka, j. polski, j. angielski).
    *   Poziomów nauczania (klasy 4 SP - matura).
    *   Cennika (podany poniżej).
    *   Formy zajęć (online, 60 minut).
    *   Pierwszej lekcji próbnej (jest płatna zgodnie z cennikiem).
2.  Prowadź naturalną, uprzejmą rozmowę w języku polskim.
3.  **Analizuj intencje użytkownika:** Na podstawie historii rozmowy i ostatniej wiadomości zdecyduj, czy użytkownik wyraża chęć umówienia się na lekcję lub pyta o dostępne terminy.
4.  **Jeśli wykryjesz intencję umówienia:**
    *   Twoja odpowiedź MUSI zawierać specjalny znacznik: `{INTENT_SCHEDULE_MARKER}`.
    *   Oprócz znacznika, sformułuj krótkie potwierdzenie, np. "Jasne, sprawdzę dostępne terminy.", "Dobrze, poszukam wolnego miejsca.", "Chętnie znajdę dla Ciebie pasujący termin."
    *   **Przykład odpowiedzi z intencją:** "Oczywiście, mogę sprawdzić dostępne terminy na matematykę dla 8 klasy. {INTENT_SCHEDULE_MARKER}"
5.  **Jeśli NIE wykryjesz intencji umówienia:** Odpowiedz normalnie na pytanie lub kontynuuj rozmowę, NIE dodając znacznika `{INTENT_SCHEDULE_MARKER}`.
6.  Pamiętaj o historii rozmowy, aby unikać powtórzeń i odpowiadać kontekstowo.

**Cennik (lekcja 60 min):**
*   Klasy 4-8 Szkoły Podstawowej: 60 zł
*   Klasy 1-3 Liceum/Technikum (poziom podstawowy): 65 zł
*   Klasy 1-3 Liceum/Technikum (poziom rozszerzony): 70 zł
*   Klasa 4 Liceum/Technikum (poziom podstawowy): 70 zł
*   Klasa 4 Liceum/Technikum (poziom rozszerzony): 75 zł

**Ważne:** Znacznik `{INTENT_SCHEDULE_MARKER}` jest kluczowy do uruchomienia procesu szukania terminów. Używaj go **tylko i wyłącznie**, gdy jesteś pewien, że użytkownik chce się umówić lub bezpośrednio o to pyta. W innych przypadkach prowadź normalną rozmowę.
"""

# ZMIANA: Nowa instrukcja dla AI generującego termin z zakresu
SYSTEM_INSTRUCTION_PROPOSE = f"""Jesteś asystentem AI specjalizującym się w proponowaniu terminów spotkań dla 'Zakręcone Korepetycje'. Twoim zadaniem jest wybranie **jednego zakresu** z dostarczonej listy, wygenerowanie w nim **jednego konkretnego terminu** i zaproponowanie go użytkownikowi. Czas trwania wizyty to **{APPOINTMENT_DURATION_MINUTES} minut**.

**Kontekst:** Użytkownik wyraził chęć umówienia pierwszej lekcji próbnej (płatnej). Otrzymałeś listę dostępnych ZAKRESÓW czasowych.

**Dostępne zakresy czasowe:**
{{available_ranges_text}}

**Twoje zadanie:**
1.  Przeanalizuj historię rozmowy (jeśli dostępna) pod kątem ewentualnych preferencji użytkownika (np. "popołudniu", "wtorek", "po 16").
2.  Wybierz **jeden** zakres z powyższej listy, który najlepiej pasuje do preferencji użytkownika LUB jeśli brak preferencji, wybierz zakres "rozsądny" (np. popołudnie w tygodniu, okolice południa w weekend).
3.  Wewnątrz wybranego zakresu **wygeneruj DOKŁADNY czas rozpoczęcia** wizyty. **Preferuj PEŁNE GODZINY** (np. 16:00, 10:00), jeśli to możliwe w ramach zakresu.
4.  **BARDZO WAŻNE:** Upewnij się, że wygenerowany czas rozpoczęcia pozwala na odbycie całej wizyty (czyli `wygenerowany_czas + {APPOINTMENT_DURATION_MINUTES} minut`) **w całości w ramach wybranego zakresu czasowego**. Np. jeśli zakres to 16:00-16:50, a wizyta trwa 60 minut, to 16:00 NIE jest poprawnym czasem rozpoczęcia.
5.  Sformułuj **krótką, uprzejmą i naturalną propozycję** wygenerowanego terminu, pytając użytkownika o akceptację. Użyj polskiego formatu daty i dnia tygodnia.
6.  **ABSOLUTNIE KLUCZOWE:** W swojej odpowiedzi **musisz** zawrzeć **identyfikator ISO wygenerowanego przez Ciebie terminu rozpoczęcia** w specjalnym znaczniku `{SLOT_ISO_MARKER_PREFIX}WYGENEROWANY_ISO_STRING{SLOT_ISO_MARKER_SUFFIX}`. Znacznik ten musi być częścią odpowiedzi i zawierać poprawny format ISO 8601 ze strefą czasową (np. `2025-05-07T17:00:00+02:00`).

**Przykład (jeśli zakres to "Środa, 07.05.2025: 16:00-18:30"):**
*   Dobry wybór (pełna godzina, mieści się w zakresie): "Proponuję termin: Środa, 07.05.2025 o 17:00. Czy ten termin by odpowiadał? {SLOT_ISO_MARKER_PREFIX}2025-05-07T17:00:00+02:00{SLOT_ISO_MARKER_SUFFIX}"
*   Zły wybór (nie mieści się w zakresie): 18:00 (bo 18:00 + 60 min = 19:00, a zakres kończy się o 18:30)
*   Zły wybór (niepełna godzina, jeśli pełna była możliwa): 16:30

**Zasady:**
*   Odpowiadaj po polsku.
*   Bądź zwięzły i profesjonalny.
*   **Wygeneruj tylko JEDEN termin** w ramach JEDNEGO wybranego zakresu.
*   **Zawsze** preferuj pełne godziny, jeśli to możliwe.
*   **Zawsze** sprawdzaj, czy cała wizyta zmieści się w zakresie.
*   **Zawsze** dołączaj znacznik `{SLOT_ISO_MARKER_PREFIX}...{SLOT_ISO_MARKER_SUFFIX}` z poprawnym ISO stringiem **wygenerowanego** terminu.
*   Nie dodawaj żadnych innych informacji (np. o cenniku), skup się tylko na propozycji terminu.
"""

SYSTEM_INSTRUCTION_FEEDBACK = f"""Jesteś asystentem AI analizującym odpowiedzi użytkowników na propozycje terminów spotkań dla 'Zakręcone Korepetycje'.

**Kontekst:** System właśnie zaproponował użytkownikowi konkretny termin lekcji próbnej. Propozycja zawierała datę i godzinę.

**Ostatnia propozycja systemu (zawierająca datę i godzinę):**
"{{last_proposal_text}}"

**Odpowiedź użytkownika na tę propozycję:**
"{{user_feedback}}"

**Twoje zadanie:**
Przeanalizuj odpowiedź użytkownika i zdecyduj, jaka jest jego intencja. **Odpowiedz TYLKO I WYŁĄCZNIE jednym z poniższych znaczników akcji:**

*   `[ACCEPT]`: Jeśli użytkownik akceptuje proponowany termin (np. "tak", "pasuje", "ok", "super", "zgadzam się", "dobrze", "może być", "rezerwuję").
*   `[REJECT_FIND_NEXT PREFERENCE='any']`: Jeśli użytkownik odrzuca termin i chce po prostu inny, bez sprecyzowanych preferencji (np. "nie pasuje", "inny termin", "nie mogę", "coś innego?", "daj następny").
*   `[REJECT_FIND_NEXT PREFERENCE='later']`: Jeśli użytkownik odrzuca i sugeruje, że termin jest za wcześnie lub woli coś później (np. "za wcześnie", "później", "popołudniu", "wieczorem").
*   `[REJECT_FIND_NEXT PREFERENCE='earlier']`: Jeśli użytkownik odrzuca i sugeruje, że termin jest za późno lub woli coś wcześniej (np. "za późno", "wcześniej", "rano", "przed południem").
*   `[REJECT_FIND_NEXT PREFERENCE='next_day']`: Jeśli użytkownik odrzuca i prosi o inny dzień, następny dzień, jutro itp.
*   `[REJECT_FIND_NEXT PREFERENCE='specific_day' DAY='NAZWA_DNIA']`: Jeśli użytkownik odrzuca i prosi o konkretny dzień tygodnia (np. "wolałbym wtorek", "może środa?"). Zastąp NAZWA_DNIA **pełną polską nazwą dnia tygodnia z dużej litery** (np. Poniedziałek, Wtorek, Środa, Czwartek, Piątek, Sobota, Niedziela).
*   `[REJECT_FIND_NEXT PREFERENCE='specific_hour' HOUR='GODZINA']`: Jeśli użytkownik odrzuca i prosi o konkretną godzinę lub porę dnia (np. "może o 14?", "czy jest coś koło 18?", "a o 10?"). Zastąp GODZINA **liczbą** reprezentującą godzinę (np. 14, 18, 10).
*   `[CLARIFY]`: Jeśli odpowiedź użytkownika jest niejasna, niejednoznaczna, zadaje pytanie niezwiązane z terminem, lub nie da się jednoznacznie określić jego intencji co do zaproponowanego terminu (np. "ile to kosztuje?", "a dla kogo to?", "nie wiem", "zastanowię się", "?").

**Ważne:**
*   Twoja odpowiedź musi być *dokładnie* jednym z powyższych znaczników, bez żadnego dodatkowego tekstu.
*   Analizuj tylko odpowiedź użytkownika w kontekście ostatniej propozycji terminu.
*   Jeśli użytkownik podaje kilka preferencji (np. "nie w środę, może piątek po 17"), wybierz najbardziej konkretną preferencję (`[REJECT_FIND_NEXT PREFERENCE='specific_day' DAY='Piątek']` lub `[REJECT_FIND_NEXT PREFERENCE='specific_hour' HOUR='17']` - wybierz jedną, np. dzień).
"""


# =====================================================================
# === FUNKCJE INTERAKCJI Z GEMINI AI ==================================
# =====================================================================

def _call_gemini(user_psid, prompt_content, generation_config, model_purpose="", max_retries=1):
    if not gemini_model:
        logging.error(f"!!! [{user_psid}] Model Gemini ({MODEL_ID}) nie jest załadowany! Nie można wykonać wywołania ({model_purpose}).")
        return None
    if not prompt_content:
        logging.warning(f"[{user_psid}] Pusty prompt przekazany do Gemini ({model_purpose}).")
        return None

    attempt = 0
    while attempt <= max_retries:
        attempt += 1
        logging.info(f"\n--- [{user_psid}] Wywołanie Gemini ({MODEL_ID}) - Cel: {model_purpose} (Próba: {attempt}/{max_retries + 1}) ---")
        if logging.getLogger().isEnabledFor(logging.DEBUG):
            try:
                prompt_dict = []
                for content_obj in prompt_content:
                    if isinstance(content_obj, Content):
                         parts_list = []
                         for part_obj in content_obj.parts:
                             if isinstance(part_obj, Part) and hasattr(part_obj, 'text'):
                                  parts_list.append({'text': part_obj.text})
                             else: parts_list.append(repr(part_obj))
                         prompt_dict.append({'role': content_obj.role, 'parts': parts_list})
                    else: prompt_dict.append(repr(content_obj))
                logging.debug(f"--- [{user_psid}] Treść promptu dla Gemini ({MODEL_ID}, {model_purpose}, Próba {attempt}): ---")
                logging.debug(json.dumps(prompt_dict, indent=2, ensure_ascii=False))
                logging.debug(f"--- Koniec treści promptu {user_psid} ---")
            except Exception as log_err:
                logging.error(f"Błąd podczas logowania promptu dla {user_psid}: {log_err}")

        try:
            response = gemini_model.generate_content(
                prompt_content,
                generation_config=generation_config,
                safety_settings=SAFETY_SETTINGS,
                stream=False
            )

            if hasattr(response, 'prompt_feedback') and response.prompt_feedback and response.prompt_feedback.block_reason:
                 logging.warning(f"[{user_psid}] Prompt zablokowany przez Gemini ({MODEL_ID}) (Próba {attempt}). Powód: {response.prompt_feedback.block_reason} ({response.prompt_feedback.block_reason_message})")
                 if response.prompt_feedback.safety_ratings:
                     logging.warning(f"    Oceny bezpieczeństwa promptu: {response.prompt_feedback.safety_ratings}")
                 return None

            if not response.candidates:
                 logging.warning(f"[{user_psid}] Odpowiedź Gemini ({MODEL_ID}) nie zawiera kandydatów (Próba {attempt}).")
                 if hasattr(response, 'prompt_feedback') and response.prompt_feedback:
                      logging.warning(f"    Prompt Feedback (brak kandydatów): {response.prompt_feedback}")
                 return None

            candidate = response.candidates[0]
            finish_reason_name = candidate.finish_reason.name

            if finish_reason_name != "STOP" and finish_reason_name != "MAX_TOKENS":
                 logging.warning(f"[{user_psid}] Odpowiedź Gemini ({MODEL_ID}) zakończona z powodu innego niż STOP/MAX_TOKENS (Próba {attempt}). Powód: {finish_reason_name}")
                 if candidate.safety_ratings:
                     logging.warning(f"    Oceny bezpieczeństwa odpowiedzi: {candidate.safety_ratings}")
                 if finish_reason_name == "SAFETY":
                     return None
                 return None

            if finish_reason_name == "STOP" and (not candidate.content or not candidate.content.parts):
                logging.warning(f"[{user_psid}] Kandydat w odpowiedzi Gemini ({MODEL_ID}) nie ma zawartości (content/parts), mimo zakończenia STOP (Próba {attempt}).")
                if candidate.safety_ratings: logging.warning(f"    Oceny bezpieczeństwa: {candidate.safety_ratings}")
                if attempt <= max_retries:
                    logging.warning(f"    Ponawianie próby ({attempt + 1}/{max_retries + 1})...")
                    time.sleep(1)
                    continue
                else:
                    logging.error(f"!!! [{user_psid}] Osiągnięto maksymalną liczbę prób ({max_retries + 1}) dla pustej odpowiedzi przy STOP. Zwracam None.")
                    return None

            generated_text = candidate.content.parts[0].text.strip()
            logging.info(f"[{user_psid}] Gemini ({MODEL_ID}, {model_purpose}) odpowiedziało (raw, Próba {attempt}): '{generated_text[:200]}...'")
            return generated_text

        except Exception as e:
            logging.error(f"!!! BŁĄD podczas wywołania Gemini ({MODEL_ID}) dla {user_psid} ({model_purpose}) (Próba {attempt}): {e}", exc_info=True)
            return None

    logging.error(f"!!! [{user_psid}] Pętla _call_gemini zakończyła się nieoczekiwanie po {attempt} próbach.")
    return None


def get_gemini_general_response(user_psid, user_input, history):
    if not user_input: return None
    history_for_ai = [msg for msg in history if msg.role in ('user', 'model')]
    user_content = Content(role="user", parts=[Part.from_text(user_input)])
    prompt_content = [
        Content(role="user", parts=[Part.from_text(SYSTEM_INSTRUCTION_GENERAL)]),
        Content(role="model", parts=[Part.from_text("Rozumiem. Będę asystentem 'Zakręcone Korepetycje'. Będę odpowiadał na pytania, prowadził rozmowę i informował o intencji umówienia spotkania za pomocą znacznika " + INTENT_SCHEDULE_MARKER + ".")]),
    ]
    prompt_content.extend(history_for_ai)
    prompt_content.append(user_content)
    while len(prompt_content) > (MAX_HISTORY_TURNS * 2 + 3) and len(prompt_content) > 3:
        logging.warning(f"[{user_psid}] Prompt dla General AI ({MODEL_ID}) za długi ({len(prompt_content)} wiad.). Usuwam najstarszą turę.")
        prompt_content.pop(2)
        if len(prompt_content) > 3: prompt_content.pop(2)
    response_text = _call_gemini(user_psid, prompt_content, GENERATION_CONFIG_DEFAULT, model_purpose="General Conversation & Intent Detection", max_retries=1)
    return response_text

# ZMIANA: Funkcja wywołująca AI do GENEROWANIA terminu z ZAKRESÓW
def get_gemini_slot_proposal(user_psid, history, available_ranges):
    """Wywołuje AI, aby wygenerowało jeden slot z podanych zakresów i sformułowało propozycję."""
    if not available_ranges:
        logging.warning(f"[{user_psid}]: Brak zakresów do przekazania AI ({MODEL_ID}) do propozycji.")
        return None, None

    ranges_text_for_ai = format_ranges_for_ai(available_ranges)
    logging.info(f"[{user_psid}] Przekazuję {len(available_ranges)} zakresów do AI ({MODEL_ID}) w celu wygenerowania terminu.")

    history_for_ai = [msg for msg in history if msg.role in ('user', 'model')]
    # Używamy nowej instrukcji systemowej
    current_instruction = SYSTEM_INSTRUCTION_PROPOSE.format(available_ranges_text=ranges_text_for_ai)
    prompt_content = [
        Content(role="user", parts=[Part.from_text(current_instruction)]),
        # Zaktualizowana odpowiedź modela dla nowej instrukcji
        Content(role="model", parts=[Part.from_text(f"Rozumiem. Wybiorę jeden zakres, wygeneruję w nim konkretny termin ({APPOINTMENT_DURATION_MINUTES} min), preferując pełne godziny i upewniając się, że mieści się w zakresie. Następnie sformułuję propozycję i dołączę znacznik {SLOT_ISO_MARKER_PREFIX}WYGENEROWANY_ISO_STRING{SLOT_ISO_MARKER_SUFFIX}.")])
    ]
    prompt_content.extend(history_for_ai)

    while len(prompt_content) > (MAX_HISTORY_TURNS * 2 + 2) and len(prompt_content) > 2:
         logging.warning(f"[{user_psid}] Prompt dla Slot Proposal AI ({MODEL_ID}) za długi ({len(prompt_content)} wiad.). Usuwam najstarszą turę.")
         prompt_content.pop(2)
         if len(prompt_content) > 2: prompt_content.pop(2)

    # Wywołanie AI
    generated_text = _call_gemini(user_psid, prompt_content, GENERATION_CONFIG_PROPOSAL, model_purpose="Slot Proposal from Ranges", max_retries=1)

    if not generated_text:
        return None, None # Błąd lub pusta odpowiedź

    # Walidacja odpowiedzi AI - tylko szukanie znacznika i ekstrakcja ISO
    iso_match = re.search(rf"\{SLOT_ISO_MARKER_PREFIX}(.*?)\{SLOT_ISO_MARKER_SUFFIX}", generated_text)
    if iso_match:
        extracted_iso = iso_match.group(1)
        # Na tym etapie NIE weryfikujemy, czy ISO jest poprawne/wolne. To zrobi webhook_handle.
        text_for_user = re.sub(rf"\{SLOT_ISO_MARKER_PREFIX}.*?\{SLOT_ISO_MARKER_SUFFIX}", "", generated_text).strip()
        text_for_user = re.sub(r'\s+', ' ', text_for_user).strip()
        logging.info(f"[{user_psid}] AI ({MODEL_ID}) wygenerowało potencjalny slot: {extracted_iso}. Tekst dla użytkownika: '{text_for_user}'")
        # Spróbujmy sparsować ISO, aby upewnić się, że ma poprawny format
        try:
            datetime.datetime.fromisoformat(extracted_iso)
            return text_for_user, extracted_iso
        except ValueError:
             logging.error(f"!!! BŁĄD KRYTYCZNY AI [{user_psid}, {MODEL_ID}]: Wygenerowany ISO '{extracted_iso}' ma nieprawidłowy format!")
             return None, None # Zwróć błąd, jeśli format ISO jest zły
    else:
        # To jest nadal krytyczny błąd, bo AI nie zastosowało się do instrukcji
        logging.error(f"!!! BŁĄD KRYTYCZNY AI [{user_psid}, {MODEL_ID}]: Brak znacznika {SLOT_ISO_MARKER_PREFIX}...{SLOT_ISO_MARKER_SUFFIX} w odpowiedzi AI generującej slot!")
        logging.error(f"    Odpowiedź AI ({MODEL_ID}): '{generated_text}'")
        return None, None

def get_gemini_feedback_decision(user_psid, user_feedback, history, last_proposal_text):
     if not user_feedback: return "[CLARIFY]"
     history_for_ai = [msg for msg in history if msg.role in ('user', 'model')]
     user_content = Content(role="user", parts=[Part.from_text(user_feedback)])
     current_instruction = SYSTEM_INSTRUCTION_FEEDBACK.format(last_proposal_text=last_proposal_text, user_feedback=user_feedback)
     prompt_content = [
         Content(role="user", parts=[Part.from_text(current_instruction)]),
         Content(role="model", parts=[Part.from_text("Rozumiem. Przeanalizuję odpowiedź użytkownika i zwrócę dokładnie jeden znacznik akcji: [ACCEPT], [REJECT_FIND_NEXT PREFERENCE=...], lub [CLARIFY].")])
     ]
     prompt_content.extend(history_for_ai)
     prompt_content.append(user_content)
     while len(prompt_content) > (MAX_HISTORY_TURNS * 2 + 3) and len(prompt_content) > 3:
         logging.warning(f"[{user_psid}] Prompt dla Feedback AI ({MODEL_ID}) za długi ({len(prompt_content)} wiad.). Usuwam najstarszą turę.")
         prompt_content.pop(2)
         if len(prompt_content) > 3: prompt_content.pop(2)
     decision = _call_gemini(user_psid, prompt_content, GENERATION_CONFIG_FEEDBACK, model_purpose="Feedback Interpretation", max_retries=1)
     if not decision: return "[CLARIFY]"
     if decision.startswith("[") and decision.endswith("]"):
         logging.info(f"[{user_psid}] AI ({MODEL_ID}) zinterpretowało feedback jako: {decision}")
         return decision
     else:
         logging.warning(f"Ostrz. [{user_psid}, {MODEL_ID}]: AI nie zwróciło poprawnego znacznika akcji, tylko tekst: '{decision}'. Traktuję jako CLARIFY.")
         return "[CLARIFY]"

# =====================================================================
# === OBSŁUGA WEBHOOKA FACEBOOKA =====================================
# =====================================================================

@app.route('/webhook', methods=['GET'])
def webhook_verification():
    logging.info("--- Otrzymano żądanie GET (weryfikacja webhooka) ---")
    hub_mode = request.args.get('hub.mode')
    hub_token = request.args.get('hub.verify_token')
    hub_challenge = request.args.get('hub.challenge')
    logging.info(f"Mode: {hub_mode}")
    logging.info(f"Token Provided: {'OK' if hub_token == VERIFY_TOKEN else 'BŁĘDNY!'}")
    logging.info(f"Challenge: {'Present' if hub_challenge else 'Missing'}")
    if hub_mode == 'subscribe' and hub_token == VERIFY_TOKEN:
        logging.info("Weryfikacja GET zakończona pomyślnie!")
        return Response(hub_challenge, status=200)
    else:
        logging.warning("Weryfikacja GET nie powiodła się. Nieprawidłowy mode lub token.")
        return Response("Verification failed", status=403)

@app.route('/webhook', methods=['POST'])
def webhook_handle():
    logging.info("\n" + "="*30 + f" {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')} OTRZYMANO POST " + "="*30)
    raw_data = request.data.decode('utf-8')
    data = None
    try:
        data = json.loads(raw_data)
        logging.debug(f"Odebrane dane (struktura): {json.dumps(data, indent=2)}")
        if data and data.get("object") == "page":
            for entry in data.get("entry", []):
                for messaging_event in entry.get("messaging", []):
                    if "sender" not in messaging_event or "id" not in messaging_event["sender"] or \
                       "recipient" not in messaging_event or "id" not in messaging_event["recipient"]:
                        logging.warning("Pominięto zdarzenie bez sender.id lub recipient.id")
                        continue
                    sender_id = messaging_event["sender"]["id"]
                    recipient_id = messaging_event["recipient"]["id"]
                    if sender_id == recipient_id:
                         logging.info(f"[{sender_id}] Pominięto echo wiadomości od strony.")
                         continue
                    logging.info(f"--- Przetwarzanie zdarzenia dla PSID: {sender_id} ---")
                    history, context = load_history(sender_id)
                    last_proposed_slot_iso = context.get('last_proposed_slot_iso')
                    is_context_current = bool(last_proposed_slot_iso)
                    if is_context_current:
                        logging.info(f"    Aktywny kontekst: Oczekiwano na odpowiedź dot. slotu {last_proposed_slot_iso}")
                    if messaging_event.get("message"):
                        message_data = messaging_event["message"]
                        message_id = message_data.get("mid")
                        logging.info(f"    Odebrano wiadomość (ID: {message_id})")
                        if message_data.get("is_echo"):
                            logging.info("      Wiadomość jest echem. Ignorowanie.")
                            continue
                        user_input_text = None
                        user_content = None
                        history_saved_after_intent = False
                        if "text" in message_data:
                            user_input_text = message_data["text"].strip()
                            logging.info(f"      Tekst użytkownika: '{user_input_text}'")
                            if not user_input_text:
                                logging.info("      Pusta wiadomość tekstowa. Ignorowanie.")
                                continue
                            user_content = Content(role="user", parts=[Part.from_text(user_input_text)])
                        elif "attachments" in message_data:
                            attachment_type = message_data['attachments'][0].get('type', 'nieznany')
                            logging.info(f"      Odebrano załącznik typu: {attachment_type}.")
                            user_input_text = f"[Użytkownik wysłał załącznik typu: {attachment_type}]"
                            user_content = Content(role="user", parts=[Part.from_text(user_input_text)])
                            no_attachment_message = "Przepraszam, obecnie nie potrafię przetwarzać załączników."
                            send_message(sender_id, no_attachment_message)
                            model_content = Content(role="model", parts=[Part.from_text(no_attachment_message)])
                            save_history(sender_id, history + [user_content, model_content], context_to_save=None)
                            continue
                        else:
                            logging.warning(f"      Nieznany typ wiadomości: {message_data}")
                            user_input_text = "[Odebrano nieznany typ wiadomości]"
                            user_content = Content(role="user", parts=[Part.from_text(user_input_text)])
                            unknown_message_reply = "Przepraszam, nie rozumiem tej wiadomości."
                            send_message(sender_id, unknown_message_reply)
                            model_content = Content(role="model", parts=[Part.from_text(unknown_message_reply)])
                            save_history(sender_id, history + [user_content, model_content], context_to_save=None)
                            continue
                        action_to_perform = None
                        text_to_send_immediately = None
                        text_to_send_as_result = None
                        context_to_save = None
                        model_response_content = None
                        error_occurred = False
                        preference = 'any'
                        requested_day_str = None
                        requested_hour_int = None
                        if ENABLE_TYPING_DELAY and user_input_text:
                            delay = max(MIN_TYPING_DELAY_SECONDS, min(MAX_TYPING_DELAY_SECONDS, len(user_input_text) / TYPING_CHARS_PER_SECOND))
                            logging.info(f"      Symulacja pisania... ({delay:.2f}s)")
                            time.sleep(delay)
                        if is_context_current and last_proposed_slot_iso:
                            logging.info(f"      SCENARIUSZ: Analiza odpowiedzi na propozycję slotu {last_proposed_slot_iso}")
                            try:
                                last_bot_message_text = history[-1].parts[0].text if history and history[-1].role == 'model' else "Proponowany termin."
                                gemini_decision = get_gemini_feedback_decision(sender_id, user_input_text, history, last_bot_message_text)
                            except Exception as feedback_err:
                                logging.error(f"!!! BŁĄD podczas interpretacji feedbacku przez AI ({MODEL_ID}): {feedback_err}", exc_info=True)
                                gemini_decision = "[CLARIFY]"
                                text_to_send_as_result = "Przepraszam, mam chwilowy problem ze zrozumieniem Twojej odpowiedzi. Czy możesz powtórzyć?"
                                error_occurred = True
                            if gemini_decision == "[ACCEPT]":
                                action_to_perform = 'book'
                            elif isinstance(gemini_decision, str) and gemini_decision.startswith("[REJECT_FIND_NEXT"):
                                action_to_perform = 'find_and_propose'
                                pref_match = re.search(r"PREFERENCE='([^']*)'", gemini_decision)
                                if pref_match: preference = pref_match.group(1)
                                day_match = re.search(r"DAY='([^']*)'", gemini_decision)
                                if day_match: requested_day_str = day_match.group(1)
                                hour_match = re.search(r"HOUR='(\d+)'", gemini_decision)
                                if hour_match:
                                    try: requested_hour_int = int(hour_match.group(1))
                                    except ValueError: logging.warning(f"Nie udało się sparsować godziny z {gemini_decision}")
                                logging.info(f"      Użytkownik odrzucił. Preferencje dla nowego szukania: {preference}, Dzień: {requested_day_str}, Godzina: {requested_hour_int}")
                                text_to_send_immediately = "Rozumiem. Poszukam innego terminu zgodnie z Twoimi wskazówkami."
                            elif gemini_decision == "[CLARIFY]" or error_occurred:
                                action_to_perform = 'send_clarification'
                                if not error_occurred:
                                     text_to_send_as_result = "Nie jestem pewien, co masz na myśli w kontekście zaproponowanego terminu. Czy mógłbyś/mogłabyś doprecyzować, czy termin pasuje, czy szukamy innego?"
                                context_to_save = {'role': 'system', 'type': 'last_proposal', 'slot_iso': last_proposed_slot_iso}
                            else:
                                logging.error(f"!!! Nieoczekiwana decyzja z AI ({MODEL_ID}, feedback): {gemini_decision}")
                                action_to_perform = 'send_error'
                                text_to_send_as_result = "Wystąpił nieoczekiwany problem podczas przetwarzania Twojej odpowiedzi."
                                error_occurred = True
                            if action_to_perform != 'send_clarification':
                                context_to_save = None
                        else:
                            logging.info(f"      SCENARIUSZ: Normalna rozmowa lub nieaktualny kontekst.")
                            try:
                                gemini_response = get_gemini_general_response(sender_id, user_input_text, history)
                            except Exception as general_err:
                                logging.error(f"!!! BŁĄD podczas generowania odpowiedzi przez AI ({MODEL_ID}): {general_err}", exc_info=True)
                                gemini_response = None
                                text_to_send_as_result = "Przepraszam, mam chwilowy problem z przetworzeniem Twojej wiadomości. Spróbuj ponownie za chwilę."
                                error_occurred = True
                            if gemini_response:
                                if INTENT_SCHEDULE_MARKER in gemini_response:
                                    logging.info(f"      AI ({MODEL_ID}) wykryło intencję umówienia [{INTENT_SCHEDULE_MARKER}].")
                                    action_to_perform = 'find_and_propose'
                                    text_before_marker = gemini_response.split(INTENT_SCHEDULE_MARKER, 1)[0].strip()
                                    if text_before_marker:
                                        text_to_send_immediately = text_before_marker
                                    else:
                                        text_to_send_immediately = "Dobrze, sprawdzę dostępne terminy."
                                    preference = 'any' # Reset preferencji przy nowym szukaniu
                                    requested_day_str = None
                                    requested_hour_int = None
                                    model_response_content = Content(role="model", parts=[Part.from_text(text_to_send_immediately)])
                                else:
                                    action_to_perform = 'send_gemini_response'
                                    text_to_send_as_result = gemini_response
                            elif not error_occurred:
                                logging.warning(f"[{sender_id}] AI ({MODEL_ID}) nie zwróciło odpowiedzi (prawdopodobnie zablokowana lub pusty wynik).")
                                action_to_perform = 'send_error'
                                text_to_send_as_result = "Nie mogę wygenerować odpowiedzi na tę wiadomość. Spróbuj sformułować ją inaczej."
                                error_occurred = True
                        logging.info(f"      Akcja do wykonania: {action_to_perform}")

                        if text_to_send_immediately:
                            send_message(sender_id, text_to_send_immediately)
                            # Zapisz historię tylko jeśli wiadomość pochodziła z wykrycia intencji
                            if action_to_perform == 'find_and_propose' and model_response_content:
                                save_history(sender_id, history + [user_content, model_response_content], context_to_save=None)
                                history.append(user_content)
                                history.append(model_response_content)
                                history_saved_after_intent = True

                        if action_to_perform == 'book':
                            try:
                                tz = _get_timezone()
                                # Weryfikacja przed rezerwacją - czy slot nadal jest ważny?
                                proposed_start_dt_verify = datetime.datetime.fromisoformat(last_proposed_slot_iso).astimezone(tz)
                                if is_slot_actually_free(proposed_start_dt_verify, TARGET_CALENDAR_ID):
                                    start_time = proposed_start_dt_verify
                                    end_time = start_time + datetime.timedelta(minutes=APPOINTMENT_DURATION_MINUTES)
                                    user_profile = get_user_profile(sender_id)
                                    user_name = user_profile.get('first_name', 'Użytkownik FB') if user_profile else 'Użytkownik FB'
                                    logging.info(f"      Wywołanie book_appointment dla {start_time}")
                                    success, message_to_user = book_appointment(
                                        TARGET_CALENDAR_ID, start_time, end_time,
                                        summary=f"Korepetycje (FB)",
                                        description=f"Rezerwacja przez bota FB.\nPSID: {sender_id}\nImię: {user_name}",
                                        user_name=user_name)
                                    text_to_send_as_result = message_to_user
                                    if not success: error_occurred = True
                                else:
                                    logging.warning(f"[{sender_id}] Slot {last_proposed_slot_iso} nie jest już dostępny! Informowanie użytkownika.")
                                    text_to_send_as_result = "Niestety, wybrany przez Ciebie termin został w międzyczasie zajęty. Czy chcesz poszukać innego?"
                                    # Nie ustawiamy błędu, ale resetujemy kontekst, żeby mógł szukać dalej
                                context_to_save = None # Zawsze resetuj kontekst po próbie rezerwacji
                            except ValueError: # Błąd parsowania ISO
                                logging.error(f"!!! BŁĄD: Nie można sparsować ISO '{last_proposed_slot_iso}' z kontekstu przy rezerwacji.")
                                text_to_send_as_result = "Wystąpił błąd podczas próby rezerwacji terminu. Spróbuj ponownie."
                                error_occurred = True
                                context_to_save = None
                            except Exception as book_err:
                                logging.error(f"!!! BŁĄD KRYTYCZNY podczas próby rezerwacji: {book_err}", exc_info=True)
                                text_to_send_as_result = "Wystąpił poważny błąd podczas próby rezerwacji terminu. Skontaktuj się z nami bezpośrednio."
                                error_occurred = True
                                context_to_save = None

                        elif action_to_perform == 'find_and_propose':
                            try:
                                tz = _get_timezone()
                                now = datetime.datetime.now(tz)
                                search_start = now
                                if last_proposed_slot_iso and preference != 'any':
                                    # ... (logika ustalania search_start bez zmian) ...
                                    try:
                                        last_proposed_dt = datetime.datetime.fromisoformat(last_proposed_slot_iso).astimezone(tz)
                                        base_start = last_proposed_dt + datetime.timedelta(minutes=10)
                                        if preference == 'later':
                                             search_start = base_start + datetime.timedelta(hours=1)
                                             if last_proposed_dt.weekday() < 5 and last_proposed_dt.hour < PREFERRED_WEEKDAY_START_HOUR:
                                                 afternoon_start = tz.localize(datetime.datetime.combine(last_proposed_dt.date(), datetime.time(PREFERRED_WEEKDAY_START_HOUR, 0)))
                                                 search_start = max(search_start, afternoon_start)
                                        elif preference == 'earlier': search_start = now
                                        elif preference == 'next_day': search_start = tz.localize(datetime.datetime.combine(last_proposed_dt.date() + datetime.timedelta(days=1), datetime.time(WORK_START_HOUR, 0)))
                                        elif preference == 'specific_day' and requested_day_str:
                                             try:
                                                 target_weekday = POLISH_WEEKDAYS.index(requested_day_str)
                                                 current_weekday = now.weekday()
                                                 days_ahead = (target_weekday - current_weekday + 7) % 7
                                                 if days_ahead == 0 and now.time() >= datetime.time(WORK_END_HOUR, 0): days_ahead = 7
                                                 target_date = now.date() + datetime.timedelta(days=days_ahead)
                                                 search_start = tz.localize(datetime.datetime.combine(target_date, datetime.time(WORK_START_HOUR, 0)))
                                             except ValueError:
                                                 logging.warning(f"Nieznana nazwa dnia: {requested_day_str}. Szukam od teraz.")
                                                 search_start = now
                                        elif preference == 'specific_hour': search_start = now
                                        search_start = max(search_start, now)
                                    except Exception as date_err:
                                        logging.error(f"Błąd przy ustalaniu search_start na podstawie preferencji: {date_err}", exc_info=True)
                                        search_start = now
                                else: search_start = now

                                logging.info(f"      Rozpoczynanie szukania zakresów od: {search_start.strftime('%Y-%m-%d %H:%M:%S %Z')}")
                                search_end_date = (search_start + datetime.timedelta(days=MAX_SEARCH_DAYS)).date()
                                search_end = tz.localize(datetime.datetime.combine(search_end_date, datetime.time(WORK_END_HOUR, 0)))

                                # Krok 1: Znajdź zakresy wolnego czasu
                                free_ranges = get_free_time_ranges(TARGET_CALENDAR_ID, search_start, search_end)

                                if free_ranges:
                                    # Krok 2: Poproś AI o wygenerowanie slotu z tych zakresów
                                    logging.info(f"      Przekazanie {len(free_ranges)} zakresów do AI ({MODEL_ID}) w celu wygenerowania propozycji...")
                                    proposal_text, proposed_iso = get_gemini_slot_proposal(sender_id, history, free_ranges)

                                    verified_iso = None # Zmienna na zweryfikowany ISO

                                    if proposal_text and proposed_iso:
                                        # Krok 3: WERYFIKACJA wygenerowanego slotu
                                        try:
                                            proposed_start_dt_verify = datetime.datetime.fromisoformat(proposed_iso).astimezone(tz)
                                            if is_slot_actually_free(proposed_start_dt_verify, TARGET_CALENDAR_ID):
                                                verified_iso = proposed_iso # Slot jest poprawny i wolny
                                                text_to_send_as_result = proposal_text
                                                context_to_save = {'role': 'system', 'type': 'last_proposal', 'slot_iso': verified_iso}
                                                logging.info(f"      AI ({MODEL_ID}) wygenerowało poprawny i wolny slot: {verified_iso}")
                                            else:
                                                logging.warning(f"[{sender_id}] AI ({MODEL_ID}) wygenerowało termin ({proposed_iso}), który NIE JEST faktycznie wolny! Używam fallbacku.")
                                                # Nie ustawiaj text_to_send_as_result ani context_to_save, przejdź do fallbacku
                                        except ValueError:
                                             logging.error(f"!!! BŁĄD [{sender_id}, {MODEL_ID}]: AI zwróciło nieprawidłowy format ISO '{proposed_iso}'. Używam fallbacku.")
                                             # Nie ustawiaj text_to_send_as_result ani context_to_save, przejdź do fallbacku
                                    else:
                                        logging.warning(f"[{sender_id}] AI ({MODEL_ID}) nie zwróciło propozycji (błąd lub pusta odpowiedź). Używam fallbacku.")
                                        # Przejdź do fallbacku

                                    # Krok 4: Logika awaryjna (Fallback), jeśli weryfikacja zawiodła lub AI nie odpowiedziało
                                    if not verified_iso:
                                        logging.info(f"      Uruchamianie logiki awaryjnej (Fallback)...")
                                        # Pobierz listę *dokładnych* slotów, aby mieć z czego wybrać
                                        all_possible_slots = get_valid_start_times(TARGET_CALENDAR_ID, search_start, search_end)
                                        if all_possible_slots:
                                            fallback_slot = all_possible_slots[0] # Wybierz pierwszy dostępny
                                            fallback_iso = fallback_slot.isoformat()
                                            fallback_text = f"Mam pewien problem z wyszukaniem idealnego terminu, ale proponuję najbliższy dostępny: {format_slot_for_user(fallback_slot)}. Czy ten może być?"
                                            text_to_send_as_result = fallback_text
                                            context_to_save = {'role': 'system', 'type': 'last_proposal', 'slot_iso': fallback_iso}
                                            logging.info(f"      Logika awaryjna wybrała: {fallback_text} (ISO: {fallback_iso})")
                                        else:
                                             logging.error(f"[{sender_id}] Brak jakichkolwiek ważnych slotów startowych dla logiki awaryjnej!")
                                             text_to_send_as_result = "Niestety, nie udało mi się znaleźć żadnych dostępnych terminów w tej chwili."
                                             error_occurred = True
                                             context_to_save = None
                                else: # Brak jakichkolwiek zakresów
                                    logging.info("      Nie znaleziono żadnych zakresów wolnego czasu w kalendarzu.")
                                    text_to_send_as_result = "Niestety, aktualnie brak wolnych terminów w kalendarzu pasujących do Twoich kryteriów."
                                    context_to_save = None
                            except Exception as find_err:
                                logging.error(f"!!! BŁĄD KRYTYCZNY podczas szukania/proponowania slotów: {find_err}", exc_info=True)
                                text_to_send_as_result = "Wystąpił nieoczekiwany błąd podczas sprawdzania dostępności terminów."
                                error_occurred = True
                                context_to_save = None
                        elif action_to_perform == 'send_gemini_response' or \
                             action_to_perform == 'send_clarification' or \
                             action_to_perform == 'send_error':
                            pass
                        else:
                            logging.error(f"!!! Nierozpoznana akcja do wykonania: {action_to_perform} dla PSID {sender_id}")
                            text_to_send_as_result = "Wystąpił wewnętrzny błąd bota."
                            error_occurred = True
                            context_to_save = None
                        if text_to_send_as_result:
                             send_message(sender_id, text_to_send_as_result)
                             if not model_response_content:
                                model_response_content = Content(role="model", parts=[Part.from_text(text_to_send_as_result)])
                        if user_content and not history_saved_after_intent:
                             history_to_save = history + [user_content]
                             if model_response_content:
                                 history_to_save.append(model_response_content)
                             logging.info(f"      Zapisywanie historii. Nowy kontekst: {context_to_save}")
                             save_history(sender_id, history_to_save, context_to_save=context_to_save)
                        elif not user_content:
                             logging.warning(f"[{sender_id}] Brak user_content do zapisania w historii (prawdopodobnie błąd przetwarzania).")
                        elif history_saved_after_intent:
                             logging.info(f"      Historia została już zapisana po wykryciu intencji. Nowy kontekst dla następnego kroku: {context_to_save}")
                             if context_to_save:
                                  latest_history, _ = load_history(sender_id)
                                  save_history(sender_id, latest_history, context_to_save=context_to_save)
                    elif messaging_event.get("postback"):
                         postback_data = messaging_event["postback"]
                         payload = postback_data.get("payload")
                         title = postback_data.get("title", payload)
                         logging.info(f"    Odebrano Postback: Tytuł='{title}', Payload='{payload}'")
                         postback_as_text = f"Użytkownik kliknął przycisk: '{title}' (payload: {payload})"
                         user_content = Content(role="user", parts=[Part.from_text(postback_as_text)])
                         gemini_response = get_gemini_general_response(sender_id, postback_as_text, history)
                         if gemini_response and INTENT_SCHEDULE_MARKER not in gemini_response:
                              send_message(sender_id, gemini_response)
                              model_content = Content(role="model", parts=[Part.from_text(gemini_response)])
                              save_history(sender_id, history + [user_content, model_content], context_to_save=None)
                         elif gemini_response and INTENT_SCHEDULE_MARKER in gemini_response:
                              text_before_marker = gemini_response.split(INTENT_SCHEDULE_MARKER, 1)[0].strip()
                              clarification_msg = text_before_marker + "\nCzy chcesz, żebym poszukał terminu?" if text_before_marker else "Chcesz umówić termin?"
                              send_message(sender_id, clarification_msg)
                              model_content = Content(role="model", parts=[Part.from_text(clarification_msg)])
                              save_history(sender_id, history + [user_content, model_content], context_to_save=None)
                         else:
                              error_msg = "Mam problem z przetworzeniem tej akcji."
                              send_message(sender_id, error_msg)
                              model_content = Content(role="model", parts=[Part.from_text(error_msg)])
                              save_history(sender_id, history + [user_content, model_content], context_to_save=None)
                    elif messaging_event.get("read"):
                        watermark = messaging_event["read"]["watermark"]
                        logging.info(f"    Wiadomości odczytane przez użytkownika do czasu: {datetime.datetime.fromtimestamp(watermark/1000).strftime('%Y-%m-%d %H:%M:%S')}")
                    elif messaging_event.get("delivery"):
                        pass
                    else:
                        logging.warning(f"    Otrzymano nieobsługiwany typ zdarzenia messaging: {json.dumps(messaging_event)}")
            return Response("EVENT_RECEIVED", status=200)
        else:
            logging.warning(f"Otrzymano POST z obiektem innym niż 'page': {data.get('object') if data else 'Brak danych'}")
            return Response("Non-page object received", status=200)
    except json.JSONDecodeError as json_err:
        logging.error(f"!!! KRYTYCZNY BŁĄD: Nie można sparsować JSON z requestu: {json_err}", exc_info=True)
        logging.error(f"   Pierwsze 500 znaków danych: {raw_data[:500]}")
        return Response("Invalid JSON format", status=400)
    except Exception as e:
        logging.error(f"!!! KRYTYCZNY BŁĄD podczas przetwarzania POST webhooka: {e}", exc_info=True)
        return Response("Internal server error processing event", status=200)

# =====================================================================
# === URUCHOMIENIE SERWERA APLIKACJI ==================================
# =====================================================================

if __name__ == '__main__':
    ensure_dir(HISTORY_DIR)
    port = int(os.environ.get("PORT", 8080))
    debug_mode = os.environ.get("FLASK_DEBUG", "False").lower() in ("true", "1", "yes")

    print("\n" + "="*50)
    print("--- START KONFIGURACJI BOTA ---")
    if not VERIFY_TOKEN or VERIFY_TOKEN == "KOLAGEN":
        print("!!! OSTRZEŻENIE: FB_VERIFY_TOKEN jest pusty lub używa wartości domyślnej 'KOLAGEN'. Ustaw bezpieczny token!")
    else:
        print("  FB_VERIFY_TOKEN: Ustawiony (OK)")

    if not PAGE_ACCESS_TOKEN:
         print("\n!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
         print("!!! KRYTYCZNE OSTRZEŻENIE: FB_PAGE_ACCESS_TOKEN JEST PUSTY!               !!!")
         print("!!! Bot NIE BĘDZIE MÓGŁ WYSYŁAĆ WIADOMOŚCI!                       !!!")
         print("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!\n")
    elif len(PAGE_ACCESS_TOKEN) < 50 :
         print("\n!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
         print("!!! KRYTYCZNE OSTRZEŻENIE: FB_PAGE_ACCESS_TOKEN ZBYT KRÓTKI!             !!!")
         print("!!! Bot NIE BĘDZIE MÓGŁ WYSYŁAĆ WIADOMOŚCI!                       !!!")
         print("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!\n")
    else:
        print("  FB_PAGE_ACCESS_TOKEN: Ustawiony (wydaje się OK)")
        if PAGE_ACCESS_TOKEN == "EACNAHFzEhkUBO4ypcoyQfWIgNc0YLZA1aCr9n3BzpvSJLoBTJnv5rWZBmc7HlqF6uUWt1uAp6aDZB8ZAb0RRT45qVIfGnciQX6wBKrZColGARfVLXP5Ic6Ptrj5AUvom4Rt12hyBxcjIJGes76fvdvBhiBZCJ0ZCVfkQMZBZCBatJshSZA8hFuRyKd58b50wkhVCMZCuwZDZD":
             print("\n!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
             print("!!! UWAGA: Używany jest DOMYŚLNY PAGE_ACCESS_TOKEN podany w instrukcji!   !!!")
             print("!!! Dla rzeczywistego działania bota, zastąp go PRAWDZIWYM tokenem     !!!")
             print("!!! dostępu do strony Facebook w zmiennej środowiskowej FB_PAGE_ACCESS_TOKEN !!!")
             print("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!\n")

    print(f"  Katalog historii konwersacji: {HISTORY_DIR}")
    print(f"  Projekt Google Cloud (Vertex AI): {PROJECT_ID}")
    print(f"  Lokalizacja Vertex AI: {LOCATION}")
    print(f"  Model Vertex AI: {MODEL_ID} (zgodnie z wymaganiem)")
    print(f"  Plik klucza Google Calendar: {SERVICE_ACCOUNT_FILE} {'(OK)' if os.path.exists(SERVICE_ACCOUNT_FILE) else '(BRAK PLIKU!)'}")
    if not os.path.exists(SERVICE_ACCOUNT_FILE):
        print("  !!! OSTRZEŻENIE: Funkcje kalendarza nie będą działać bez pliku klucza!")
    print(f"  Docelowy Kalendarz Google ID: {TARGET_CALENDAR_ID}")
    print(f"  Strefa czasowa kalendarza: {CALENDAR_TIMEZONE}")
    print(f"  Symulacja pisania: {'Włączona' if ENABLE_TYPING_DELAY else 'Wyłączona'}")
    # Usunięto MAX_SLOTS_FOR_AI z logów startowych, bo nie jest już bezpośrednio używane w ten sam sposób

    if gemini_model is None:
        print(f"\n!!! OSTRZEŻENIE: Model Gemini AI ({MODEL_ID}) NIE został załadowany poprawnie!")
        print("!!! Funkcje AI nie będą działać. Sprawdź logi błędów inicjalizacji.\n")
    else:
        print(f"  Model Gemini AI ({MODEL_ID}): Załadowany (OK)")

    calendar_service_check = get_calendar_service()
    if calendar_service_check is None and os.path.exists(SERVICE_ACCOUNT_FILE):
         print("\n!!! OSTRZEŻENIE: Nie udało się zainicjować usługi Google Calendar, mimo że plik klucza istnieje.")
         print("!!! Sprawdź uprawnienia konta usługi i logi błędów.\n")
    elif calendar_service_check:
         print("  Usługa Google Calendar: Zainicjowana (OK)")


    print("--- KONIEC KONFIGURACJI BOTA ---")
    print("="*50 + "\n")

    print(f"Uruchamianie serwera Flask na porcie {port}")
    print(f"Tryb debug: {debug_mode}")

    logging.getLogger('googleapiclient.discovery_cache').setLevel(logging.ERROR)

    if not debug_mode:
        try:
            from waitress import serve
            print("Uruchamianie serwera produkcyjnego Waitress...")
            serve(app, host='0.0.0.0', port=port)
        except ImportError:
            print("Waitress nie jest zainstalowany. Uruchamianie wbudowanego serwera Flask (niezalecane dla produkcji).")
            print("Aby zainstalować Waitress, uruchom: pip install waitress")
            app.run(host='0.0.0.0', port=port, debug=False)
    else:
        print("Uruchamianie wbudowanego serwera Flask w trybie DEBUG...")
        logging.getLogger().setLevel(logging.DEBUG)
        print("Ustawiono poziom logowania na DEBUG.")
        app.run(host='0.0.0.0', port=port, debug=True)
