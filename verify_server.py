# -*- coding: utf-8 -*-

# verify_server.py (połączony kod z iteracyjnym szukaniem wg preferencji)

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
PAGE_ACCESS_TOKEN = os.environ.get("FB_PAGE_ACCESS_TOKEN", "EACNAHFzEhkUBO4ypcoyQfWIgNc0YLZA1aCr9n3BzpvSJLoBTJnv5rWZBmc7HlqF6uUWt1uAp6aDZB8ZAb0RRT45qVIfGnciQX6wBKrZColGARfVLXP5Ic6Ptrj5AUvom4Rt12hyBxcjIJGes76fvdvBhiBZCJ0ZCVfkQMZBZCBatJshSZA8hFuRyKd58b50wkhVCMZCuwZDZD") # WAŻNE: Podaj swój prawdziwy token!
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
PREFERRED_WEEKDAY_START_HOUR = 16 # Godzina "popołudniowa"
PREFERRED_WEEKEND_START_HOUR = 10
MAX_SEARCH_DAYS = 14 # Jak daleko w przyszłość szukać kolejnych terminów
EARLY_HOUR_LIMIT = 12 # Górna granica dla preferencji "earlier"

# --- Inicjalizacja Zmiennych Globalnych dla Kalendarza ---
_calendar_service = None
_tz = None

# --- Lista Polskich Dni Tygodnia ---
POLISH_WEEKDAYS = ["Poniedziałek", "Wtorek", "Środa", "Czwartek", "Piątek", "Sobota", "Niedziela"]

# --- Ustawienia Lokalizacji ---
try: locale.setlocale(locale.LC_TIME, 'pl_PL.UTF-8')
except locale.Error:
    try: locale.setlocale(locale.LC_TIME, 'Polish_Poland.1250')
    except locale.Error: print("Ostrzeżenie: Nie można ustawić polskiej lokalizacji dla formatowania dat.")

# =====================================================================
# === FUNKCJE POMOCNICZE ==============================================
# =====================================================================
# (Funkcje ensure_dir, get_user_profile, load_history, save_history, _get_timezone,
#  get_calendar_service, parse_event_time, get_free_slots, book_appointment,
#  format_slot_for_user - BEZ ZMIAN od ostatniej wersji)
# (Funkcja find_next_reasonable_slot nie jest już potrzebna w tej logice)

def ensure_dir(directory):
    try: os.makedirs(directory); print(f"Utworzono katalog: {directory}")
    except OSError as e:
        if e.errno != errno.EEXIST: print(f"!!! Błąd tworzenia katalogu {directory}: {e} !!!"); raise

def get_user_profile(psid):
    if not PAGE_ACCESS_TOKEN or len(PAGE_ACCESS_TOKEN) < 50: print(f"!!! [{psid}] Brak/nieprawidłowy TOKEN. Profil niepobrany."); return None
    USER_PROFILE_API_URL_TEMPLATE = "https://graph.facebook.com/v19.0/{psid}?fields=first_name,last_name,profile_pic&access_token={token}"
    url = USER_PROFILE_API_URL_TEMPLATE.format(psid=psid, token=PAGE_ACCESS_TOKEN)
    print(f"--- [{psid}] Pobieranie profilu...")
    profile_data = {}
    try:
        r = requests.get(url, timeout=10); r.raise_for_status(); data = r.json()
        if 'error' in data: print(f"!!! BŁĄD FB API (profil) {psid}: {data['error']} !!!"); return None
        profile_data['first_name'] = data.get('first_name'); profile_data['last_name'] = data.get('last_name')
        profile_data['profile_pic'] = data.get('profile_pic'); profile_data['id'] = data.get('id')
        return profile_data
    except requests.exceptions.Timeout: print(f"!!! BŁĄD TIMEOUT profilu {psid} !!!"); return None
    except requests.exceptions.HTTPError as http_err:
         print(f"!!! BŁĄD HTTP {http_err.response.status_code} profilu {psid}: {http_err} !!!")
         if http_err.response is not None:
            try: print(f"Odpowiedź FB (błąd HTTP): {http_err.response.json()}")
            except json.JSONDecodeError: print(f"Odpowiedź FB (błąd HTTP, nie JSON): {http_err.response.text}")
         return None
    except requests.exceptions.RequestException as req_err: print(f"!!! BŁĄD RequestException profilu {psid}: {req_err} !!!"); return None
    except Exception as e: import traceback; print(f"!!! Niespodziewany BŁĄD profilu {psid}: {e} !!!"); traceback.print_exc(); return None

def load_history(user_psid):
    filepath = os.path.join(HISTORY_DIR, f"{user_psid}.json"); history = []; context = {}
    if not os.path.exists(filepath): return history, context
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            history_data = json.load(f)
            if isinstance(history_data, list):
                processed_indices = set()
                for i, msg_data in enumerate(history_data):
                    if i in processed_indices: continue
                    if (isinstance(msg_data, dict) and 'role' in msg_data and msg_data['role'] in ('user', 'model') and
                            'parts' in msg_data and isinstance(msg_data['parts'], list) and msg_data['parts']):
                        text_parts = []; valid_parts = True
                        for part_data in msg_data['parts']:
                            if isinstance(part_data, dict) and 'text' in part_data and isinstance(part_data['text'], str): text_parts.append(Part.from_text(part_data['text']))
                            else: print(f"Ostrz. [{user_psid}]: Niepoprawna część (idx {i})"); valid_parts = False; break
                        if valid_parts and text_parts: history.append(Content(role=msg_data['role'], parts=text_parts))
                    elif (isinstance(msg_data, dict) and 'role' in msg_data and msg_data['role'] == 'system' and
                          'type' in msg_data and msg_data['type'] == 'last_proposal' and 'slot_iso' in msg_data):
                        is_latest_context = all(not (isinstance(history_data[j], dict) and history_data[j].get('role') == 'system') for j in range(i + 1, len(history_data)))
                        if is_latest_context:
                            context['last_proposed_slot_iso'] = msg_data['slot_iso']
                            context['message_index'] = i
                            print(f"[{user_psid}] Odczytano AKTUALNY kontekst: last_proposed_slot_iso (idx {i})")
                        else: print(f"[{user_psid}] Pominięto stary kontekst systemowy na indeksie {i}")
                    else: print(f"Ostrz. [{user_psid}]: Pominięto niepoprawną wiadomość w historii (idx {i}): {msg_data}")
                print(f"[{user_psid}] Wczytano historię: {len(history)} wiadomości."); return history, context
            else: print(f"!!! BŁĄD [{user_psid}]: Plik historii nie zawiera listy."); return [], {}
    except FileNotFoundError: print(f"[{user_psid}] Plik historii nie istnieje."); return [], {}
    except (json.JSONDecodeError, KeyError, IndexError, TypeError) as e: print(f"!!! BŁĄD [{user_psid}] parsowania historii: {e}."); return [], {}
    except Exception as e: print(f"!!! BŁĄD [{user_psid}] wczytywania historii: {e} !!!"); return [], {}

def save_history(user_psid, history, context_to_save=None):
    ensure_dir(HISTORY_DIR); filepath = os.path.join(HISTORY_DIR, f"{user_psid}.json"); temp_filepath = f"{filepath}.tmp"
    history_data = []
    try:
        max_messages_to_save = MAX_HISTORY_TURNS * 2
        history_to_process = history[-max_messages_to_save:] if len(history) > max_messages_to_save else history
        if len(history) > max_messages_to_save: print(f"[{user_psid}] Historia przycięta DO ZAPISU: {len(history_to_process)} wiad.")
        for msg in history_to_process:
             if isinstance(msg, Content) and hasattr(msg, 'role') and msg.role in ('user', 'model') and hasattr(msg, 'parts') and isinstance(msg.parts, list):
                parts_data = [{'text': part.text} for part in msg.parts if isinstance(part, Part) and hasattr(part, 'text')]
                if parts_data: history_data.append({'role': msg.role, 'parts': parts_data})
             else: print(f"Ostrz. [{user_psid}]: Pomijanie nieprawidłowego obiektu (zapis): {msg}")
        if context_to_save and isinstance(context_to_save, dict):
             history_data.append(context_to_save)
             print(f"[{user_psid}] Dodano kontekst do zapisu: {context_to_save.get('type')}")
        with open(temp_filepath, 'w', encoding='utf-8') as f: json.dump(history_data, f, ensure_ascii=False, indent=2)
        os.replace(temp_filepath, filepath)
        print(f"[{user_psid}] Zapisano historię/kontekst ({len(history_data)} wpisów) do: {filepath}")
    except Exception as e:
        print(f"!!! BŁĄD [{user_psid}] zapisu historii/kontekstu: {e} !!! Plik: {filepath}")
        if os.path.exists(temp_filepath):
            try: os.remove(temp_filepath); print(f"    Usunięto {temp_filepath}.")
            except OSError as remove_e: print(f"    Nie można usunąć {temp_filepath}: {remove_e}")

def _get_timezone():
    global _tz;
    if _tz is None:
        try: _tz = pytz.timezone(CALENDAR_TIMEZONE)
        except pytz.exceptions.UnknownTimeZoneError: print(f"BŁĄD: Strefa '{CALENDAR_TIMEZONE}' nieznana. Używam UTC."); _tz = pytz.utc
    return _tz

def get_calendar_service():
    global _calendar_service;
    if _calendar_service: return _calendar_service
    if not os.path.exists(SERVICE_ACCOUNT_FILE): print(f"BŁĄD: Brak pliku klucza: '{SERVICE_ACCOUNT_FILE}'"); return None
    try:
        creds = service_account.Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=CALENDAR_SCOPES)
        service = build('calendar', 'v3', credentials=creds); print("Utworzono usługę Calendar API."); _calendar_service = service; return service
    except HttpError as error: print(f"Błąd API tworzenia usługi Calendar: {error}"); return None
    except Exception as e: print(f"Błąd tworzenia usługi Calendar: {e}"); return None

def parse_event_time(event_time_data, default_tz):
    if 'dateTime' in event_time_data:
        dt_str = event_time_data['dateTime'];
        try: dt = datetime.datetime.fromisoformat(dt_str)
        except ValueError:
            try: dt = datetime.datetime.strptime(dt_str, '%Y-%m-%dT%H:%M:%S%z')
            except ValueError:
                try: dt = datetime.datetime.strptime(dt_str, '%Y-%m-%dT%H:%M%z')
                except ValueError: print(f"Ostrz.: Nie sparsowano dateTime: {dt_str}"); return None
        if dt.tzinfo is None: dt = default_tz.localize(dt)
        else: dt = dt.astimezone(default_tz)
        return dt
    elif 'date' in event_time_data:
        try: return datetime.date.fromisoformat(event_time_data['date'])
        except ValueError: print(f"Ostrz.: Nie sparsowano date: {event_time_data['date']}"); return None
    return None

def get_free_slots(calendar_id, start_datetime, end_datetime):
    service = get_calendar_service(); tz = _get_timezone()
    if not service: print("Błąd: Usługa kalendarza niedostępna w get_free_slots."); return []
    if start_datetime.tzinfo is None: start_datetime = tz.localize(start_datetime)
    else: start_datetime = start_datetime.astimezone(tz)
    if end_datetime.tzinfo is None: end_datetime = tz.localize(end_datetime)
    else: end_datetime = end_datetime.astimezone(tz)
    print(f"Szukanie wolnych slotów ({APPOINTMENT_DURATION_MINUTES} min) w '{calendar_id}'"); print(f"Zakres: {start_datetime:%Y-%m-%d %H:%M %Z} do {end_datetime:%Y-%m-%d %H:%M %Z}")
    try:
        events_result = service.events().list(calendarId=calendar_id, timeMin=start_datetime.isoformat(), timeMax=end_datetime.isoformat(), singleEvents=True, orderBy='startTime').execute()
        events = events_result.get('items', [])
    except HttpError as error: print(f'Błąd API pobierania wydarzeń: {error}'); return []
    except Exception as e: print(f"Błąd pobierania wydarzeń: {e}"); return []

    free_slots_starts = []; current_day = start_datetime.date(); end_day = end_datetime.date()
    appointment_duration = datetime.timedelta(minutes=APPOINTMENT_DURATION_MINUTES)
    while current_day <= end_day:
        day_start_limit = tz.localize(datetime.datetime.combine(current_day, datetime.time(WORK_START_HOUR, 0)))
        day_end_limit = tz.localize(datetime.datetime.combine(current_day, datetime.time(WORK_END_HOUR, 0)))
        check_start_time = max(start_datetime, day_start_limit); check_end_time = min(end_datetime, day_end_limit)
        if check_start_time >= check_end_time: current_day += datetime.timedelta(days=1); continue
        potential_slot_start = check_start_time; busy_times = []
        for event in events:
            start = parse_event_time(event['start'], tz)
            if isinstance(start, datetime.date):
                if start == current_day: busy_times.append({'start': day_start_limit, 'end': day_end_limit})
            elif isinstance(start, datetime.datetime):
                end = parse_event_time(event['end'], tz)
                if isinstance(end, datetime.datetime):
                    if end > day_start_limit and start < day_end_limit:
                        effective_start = max(start, day_start_limit); effective_end = min(end, day_end_limit)
                        if effective_start < effective_end: busy_times.append({'start': effective_start, 'end': effective_end})
                else: print(f"Ostrz.: Wydarzenie '{event.get('summary','?')}' ma nieprawidłowy koniec ({type(end)})")
        if not busy_times: merged_busy_times = []
        else:
             busy_times.sort(key=lambda x: x['start']); merged_busy_times = [busy_times[0]]
             for current_busy in busy_times[1:]:
                 last_merged = merged_busy_times[-1]
                 if current_busy['start'] <= last_merged['end']: last_merged['end'] = max(last_merged['end'], current_busy['end'])
                 else: merged_busy_times.append(current_busy)
        for busy in merged_busy_times:
            busy_start = busy['start']; busy_end = busy['end']
            while potential_slot_start + appointment_duration <= busy_start:
                 if potential_slot_start >= check_start_time and potential_slot_start + appointment_duration <= check_end_time:
                     if potential_slot_start.minute % 10 == 0: free_slots_starts.append(potential_slot_start)
                 current_minute = potential_slot_start.minute; minutes_to_add = 10 - (current_minute % 10);
                 if minutes_to_add == 0: minutes_to_add = 10
                 potential_slot_start += datetime.timedelta(minutes=minutes_to_add)
                 potential_slot_start = potential_slot_start.replace(second=0, microsecond=0)
            potential_slot_start = max(potential_slot_start, busy_end)
        while potential_slot_start + appointment_duration <= check_end_time:
             if potential_slot_start >= check_start_time:
                 if potential_slot_start.minute % 10 == 0: free_slots_starts.append(potential_slot_start)
             current_minute = potential_slot_start.minute; minutes_to_add = 10 - (current_minute % 10);
             if minutes_to_add == 0: minutes_to_add = 10
             potential_slot_start += datetime.timedelta(minutes=minutes_to_add)
             potential_slot_start = potential_slot_start.replace(second=0, microsecond=0)
        current_day += datetime.timedelta(days=1)
    final_slots = sorted(list(set(slot for slot in free_slots_starts if start_datetime <= slot < end_datetime)))
    print(f"Znaleziono {len(final_slots)} unikalnych 'ładnych' wolnych slotów."); return final_slots

def book_appointment(calendar_id, start_time, end_time, summary="Rezerwacja wizyty", description="", user_name=""):
    service = get_calendar_service(); tz = _get_timezone()
    if not service: return False, "Błąd: Brak połączenia z usługą kalendarza."
    if start_time.tzinfo is None: start_time = tz.localize(start_time)
    else: start_time = start_time.astimezone(tz)
    if end_time.tzinfo is None: end_time = tz.localize(end_time)
    else: end_time = end_time.astimezone(tz)
    event_summary = summary;
    if user_name: event_summary += f" - {user_name}"
    event = {'summary': event_summary, 'description': description, 'start': {'dateTime': start_time.isoformat(), 'timeZone': CALENDAR_TIMEZONE,}, 'end': {'dateTime': end_time.isoformat(), 'timeZone': CALENDAR_TIMEZONE,}, 'reminders': {'useDefault': False, 'overrides': [{'method': 'popup', 'minutes': 60},],},}
    try:
        print(f"Rezerwacja: {event_summary} od {start_time:%Y-%m-%d %H:%M} do {end_time:%Y-%m-%d %H:%M}")
        created_event = service.events().insert(calendarId=calendar_id, body=event).execute()
        print(f"Zarezerwowano. ID: {created_event.get('id')}")
        day_index = start_time.weekday(); locale_day_name = POLISH_WEEKDAYS[day_index]
        hour_str = str(start_time.hour); confirm_message = f"Świetnie! Termin na {locale_day_name}, {start_time.strftime(f'%d.%m.%Y o {hour_str}:%M')} został zarezerwowany."
        return True, confirm_message
    except HttpError as error:
        error_details = f"Kod: {error.resp.status}, Powód: {error.resp.reason}"
        try: error_json = json.loads(error.content.decode('utf-8')); error_details += f" - {error_json.get('error', {}).get('message', '')}"
        except: pass
        print(f"Błąd API rezerwacji: {error}, Szczegóły: {error_details}")
        if error.resp.status == 409: return False, "Niestety, ten termin jest już zajęty."
        elif error.resp.status == 403: return False, f"Brak uprawnień do zapisu w kalendarzu."
        elif error.resp.status == 404: return False, f"Nie znaleziono kalendarza '{calendar_id}'."
        else: return False, f"Błąd API ({error.resp.status}) rezerwacji."
    except Exception as e: import traceback; print(f"Nieoczekiwany błąd Python rezerwacji: {e}"); traceback.print_exc(); return False, "Błąd systemu rezerwacji."

# ZMIANA: Usunięto find_next_reasonable_slot, AI wybiera
# def find_next_reasonable_slot(...)

# ZMIANA: Formatowanie listy zakresów dla AI
def format_ranges_for_ai(ranges):
    """Formatuje listę zakresów na czytelny tekst dla AI."""
    if not ranges: return "Brak dostępnych zakresów czasowych."
    ranges_by_date = defaultdict(list)
    for r in ranges:
        range_date = r['start'].date()
        # Pokaż tylko zakresy w godzinach pracy
        eff_start = max(r['start'], _get_timezone().localize(datetime.datetime.combine(range_date, datetime.time(WORK_START_HOUR, 0))))
        eff_end = min(r['end'], _get_timezone().localize(datetime.datetime.combine(range_date, datetime.time(WORK_END_HOUR, 0))))
        if eff_end - eff_start >= datetime.timedelta(minutes=APPOINTMENT_DURATION_MINUTES):
            ranges_by_date[range_date].append({'start_time': eff_start.strftime('%H:%M'), 'end_time': eff_end.strftime('%H:%M')})
    formatted = ["Dostępne ZAKRESY czasowe (wizyta trwa 60 minut). Wybierz jeden zakres i wygeneruj z niego DOKŁADNY termin startu (preferuj pełne godziny), dołączając go w znaczniku [SLOT_ISO:...]"]
    dates_added = 0
    for d in sorted(ranges_by_date.keys()):
        day_name = POLISH_WEEKDAYS[d.weekday()]; date_str = d.strftime('%d.%m.%Y')
        times = [f"{tr['start_time']}-{tr['end_time']}" for tr in ranges_by_date[d]]
        if times: # Dodaj tylko jeśli są jakieś przedziały w godzinach pracy
            formatted.append(f"- {day_name}, {date_str}: {'; '.join(times)}")
            dates_added += 1
            if dates_added >= 7: break # Ogranicz liczbę dni pokazywanych AI
    if dates_added == 0: return "Brak dostępnych zakresów czasowych w godzinach pracy."
    return "\n".join(formatted)

def format_slot_for_user(slot_start):
    """Formatuje pojedynczy slot (datetime) na czytelny tekst dla użytkownika."""
    if not isinstance(slot_start, datetime.datetime): return ""
    try:
        day_index = slot_start.weekday(); day_name = POLISH_WEEKDAYS[day_index]
        hour_str = str(slot_start.hour) # Godzina bez zera
        return f"{day_name}, {slot_start.strftime(f'%d.%m.%Y o {hour_str}:%M')}"
    except Exception as e: logging.error(f"Błąd formatowania slotu: {e}", exc_info=True); return slot_start.isoformat()

# --- Inicjalizacja Vertex AI ---
gemini_model = None
try:
    print(f"Inicjalizowanie Vertex AI: Projekt={PROJECT_ID}, Lokalizacja={LOCATION}")
    vertexai.init(project=PROJECT_ID, location=LOCATION); print("Inicjalizacja Vertex AI OK.")
    print(f"Ładowanie modelu: {MODEL_ID}")
    gemini_model = GenerativeModel(MODEL_ID); print("Model załadowany OK.")
except Exception as e: print(f"!!! KRYTYCZNY BŁĄD inicjalizacji Vertex AI: {e} !!!")

# --- Funkcje wysyłania wiadomości FB ---
def _send_single_message(recipient_id, message_text):
    print(f"--- Wysyłanie fragm. do {recipient_id} (dł: {len(message_text)}) ---"); params = {"access_token": PAGE_ACCESS_TOKEN}
    payload = {"recipient": {"id": recipient_id}, "message": {"text": message_text}, "messaging_type": "RESPONSE"}
    if not PAGE_ACCESS_TOKEN or len(PAGE_ACCESS_TOKEN) < 50: print(f"!!! [{recipient_id}] Brak TOKENA. Nie wysłano."); return False
    try:
        r = requests.post(FACEBOOK_GRAPH_API_URL, params=params, json=payload, timeout=30); r.raise_for_status(); response_json = r.json()
        if response_json.get('error'): print(f"!!! BŁĄD FB API: {response_json['error']} !!!"); return False
        return True
    except requests.exceptions.Timeout: print(f"!!! BŁĄD TIMEOUT wysyłania do {recipient_id} !!!"); return False
    except requests.exceptions.RequestException as e:
        print(f"!!! BŁĄD wysyłania do {recipient_id}: {e} !!!")
        if hasattr(e, 'response') and e.response is not None:
            try: print(f"Odpowiedź FB (błąd): {e.response.json()}")
            except json.JSONDecodeError: print(f"Odpowiedź FB (błąd, nie JSON): {e.response.text}")
        return False

def send_message(recipient_id, full_message_text):
    if not full_message_text or not isinstance(full_message_text, str) or not full_message_text.strip(): print(f"[{recipient_id}] Pominięto pustą wiadomość."); return
    message_len = len(full_message_text); print(f"[{recipient_id}] Przygotowanie wiad. (dł: {message_len}).")
    if message_len <= MESSAGE_CHAR_LIMIT: _send_single_message(recipient_id, full_message_text)
    else:
        chunks = []; remaining_text = full_message_text; print(f"[{recipient_id}] Dzielenie wiad. (limit: {MESSAGE_CHAR_LIMIT})...")
        while remaining_text:
            if len(remaining_text) <= MESSAGE_CHAR_LIMIT: chunks.append(remaining_text.strip()); break
            split_index = -1
            for delimiter in ['\n\n', '\n', '. ', '! ', '? ', ' ']:
                search_limit = MESSAGE_CHAR_LIMIT - (len(delimiter) -1) if len(delimiter) > 1 else MESSAGE_CHAR_LIMIT
                temp_index = remaining_text.rfind(delimiter, 0, search_limit)
                if temp_index != -1: split_index = temp_index + len(delimiter); break
            if split_index == -1: split_index = MESSAGE_CHAR_LIMIT
            chunk = remaining_text[:split_index].strip()
            if chunk: chunks.append(chunk)
            remaining_text = remaining_text[split_index:].strip()
        num_chunks = len(chunks); print(f"[{recipient_id}] Podzielono na {num_chunks} fragmentów.")
        send_success_count = 0
        for i, chunk in enumerate(chunks):
            print(f"[{recipient_id}] Wysyłanie fragm. {i+1}/{num_chunks} (dł: {len(chunk)})...")
            if not _send_single_message(recipient_id, chunk): print(f"!!! [{recipient_id}] Anulowano resztę po błędzie fragm. {i+1} !!!"); break
            send_success_count += 1
            if i < num_chunks - 1: print(f"[{recipient_id}] Oczekiwanie {MESSAGE_DELAY_SECONDS}s..."); time.sleep(MESSAGE_DELAY_SECONDS)
        print(f"--- [{recipient_id}] Zakończono wysyłanie {send_success_count}/{num_chunks} fragm. ---")

# --- INSTRUKCJA SYSTEMOWA (dla AI proponującego termin) ---
# ZMIANA: Usunięto wzmiankę o MAX_SLOTS_FOR_AI z promptu
SYSTEM_INSTRUCTION_TEXT_PROPOSE = """Jesteś profesjonalnym asystentem klienta 'Zakręcone Korepetycje'. Twoim zadaniem jest przeanalizowanie historii rozmowy i listy dostępnych zakresów czasowych, a następnie wybranie **jednego**, najbardziej odpowiedniego terminu i zaproponowanie go użytkownikowi.

**Kontekst:** Rozmawiasz o korepetycjach online. Użytkownik chce umówić lekcję próbną (płatną).

**Dostępne zakresy czasowe:**
{available_ranges_text}

**Twoje zadanie:**
1.  Analizuj historię rozmowy pod kątem preferencji (dzień, pora dnia, godzina).
2.  Wybierz **jeden** zakres z listy "Dostępne zakresy czasowe", który pasuje do preferencji lub jest "rozsądny" (popołudnie w tyg. >= {pref_weekday}h, weekend >= {pref_weekend}h).
3.  W wybranym zakresie **wygeneruj DOKŁADNY czas startu** ({duration} min wizyta). **Preferuj PEŁNE GODZINY** (np. 16:00, 17:00) jeśli to możliwe w danym zakresie.
4.  **BARDZO WAŻNE:** Upewnij się, że `wygenerowany_czas + {duration} minut` **mieści się w wybranym zakresie**.
5.  Sformułuj krótką, uprzejmą propozycję wygenerowanego terminu (polski format daty/dnia).
6.  **KLUCZOWE:** Odpowiedź **MUSI** zawierać znacznik `{slot_marker_prefix}WYGENEROWANY_ISO_STRING{slot_marker_suffix}` z poprawnym ISO 8601 **wygenerowanego** terminu.

**Przykład (zakres "Środa, 07.05.2025: 16:00-18:30"):**
*   Dobry: "Proponuję: Środa, 07.05.2025 o 17:00. Pasuje? {slot_marker_prefix}2025-05-07T17:00:00+02:00{slot_marker_suffix}"
*   Zły (nie mieści się): 18:00

**Zasady:** Generuj JEDEN termin. Preferuj pełne godziny. Sprawdź zakres. ZAWSZE dołączaj znacznik ISO. Bez cennika itp.
""".format(
    pref_weekday=PREFERRED_WEEKDAY_START_HOUR,
    pref_weekend=PREFERRED_WEEKEND_START_HOUR,
    duration=APPOINTMENT_DURATION_MINUTES,
    slot_marker_prefix=SLOT_ISO_MARKER_PREFIX,
    slot_marker_suffix=SLOT_ISO_MARKER_SUFFIX
    )
# ---------------------------------------------------------------------

# --- INSTRUKCJA SYSTEMOWA (dla AI interpretującego feedback) ---
SYSTEM_INSTRUCTION_TEXT_FEEDBACK = """Jesteś asystentem AI analizującym odpowiedź użytkownika na propozycję terminu.

**Kontekst:** Zaproponowano użytkownikowi termin.
**Ostatnia propozycja:** "{last_proposal_text}"
**Odpowiedź użytkownika:** "{user_feedback}"

**Zadanie:** Zwróć **TYLKO JEDEN** z poniższych znaczników:
*   `[ACCEPT]`: Akceptacja (tak, ok, pasuje).
*   `[REJECT PREFERENCE='any']`: Odrzucenie, brak preferencji (nie pasuje, inny).
*   `[REJECT PREFERENCE='later']`: Chce później tego samego dnia lub ogólnie później.
*   `[REJECT PREFERENCE='earlier']`: Chce wcześniej tego samego dnia (za późno).
*   `[REJECT PREFERENCE='afternoon']`: Za wcześnie, chce popołudniu.
*   `[REJECT PREFERENCE='next_day']`: Chce inny dzień, jutro.
*   `[REJECT PREFERENCE='specific_day' DAY='NAZWA_DNIA']`: Chce konkretny dzień.
*   `[REJECT PREFERENCE='specific_hour' HOUR='GODZINA']`: Chce konkretną godzinę.
*   `[REJECT PREFERENCE='specific_datetime' DAY='NAZWA_DNIA' HOUR='GODZINA']`: Chce konkretny dzień i godzinę.
*   `[REJECT PREFERENCE='specific_day_later' DAY='NAZWA_DNIA']`: Chce konkretny dzień, ale później.
*   `[REJECT PREFERENCE='specific_day_earlier' DAY='NAZWA_DNIA']`: Chce konkretny dzień, ale wcześniej.
*   `[CLARIFY]`: Niejasna odpowiedź, pytanie niezwiązane.

**Ważne:** Dokładnie jeden znacznik. Preferuj bardziej szczegółowe znaczniki (np. `specific_datetime` nad `specific_day`).
"""
# ---------------------------------------------------------------------

# --- Funkcja interakcji z Gemini (proponowanie slotu) ---
def get_gemini_slot_proposal(user_psid, history, available_ranges):
    if not gemini_model: logging.error(f"!!! [{user_psid}] Model niezaładowany!"); return None, None
    if not available_ranges: logging.warning(f"[{user_psid}]: Brak zakresów dla AI."); return "Niestety, brak wolnych terminów.", None

    ranges_text = format_ranges_for_ai(available_ranges) # Użyj nowej funkcji formatującej
    logging.info(f"[{user_psid}] Przekazuję {len(available_ranges)} zakresów do AI.")
    history_for_ai = [m for m in history if m.role in ('user','model')]
    instr = SYSTEM_INSTRUCTION_TEXT_PROPOSE.format(available_ranges_text=ranges_text) # Wypełnij szablon
    prompt = [Content(role="user", parts=[Part.from_text(instr)]), Content(role="model", parts=[Part.from_text(f"OK. Wygeneruję termin {APPOINTMENT_DURATION_MINUTES} min.")])] + history_for_ai
    # Usuwanie starych wiadomości z promptu (jak poprzednio)...
    while len(prompt) > (MAX_HISTORY_TURNS * 2 + 2) and len(prompt) > 2: prompt.pop(2); if len(prompt) > 2: prompt.pop(2)

    generated_text = _call_gemini(user_psid, prompt, GENERATION_CONFIG_PROPOSAL, "Slot Proposal from Ranges", 1)
    if not generated_text: return None, None

    iso_match = re.search(rf"\{SLOT_ISO_MARKER_PREFIX}(.*?)\{SLOT_ISO_MARKER_SUFFIX}", generated_text)
    if iso_match:
        extracted_iso = iso_match.group(1)
        text_for_user = re.sub(rf"\{SLOT_ISO_MARKER_PREFIX}.*?\{SLOT_ISO_MARKER_SUFFIX}", "", generated_text).strip()
        text_for_user = re.sub(r'\s+', ' ', text_for_user).strip()
        logging.info(f"[{user_psid}] AI wygenerowało: {extracted_iso}. Text: '{text_for_user}'")
        try: # Sprawdź poprawność ISO i czy termin jest możliwy w zakresie
            proposed_start = datetime.datetime.fromisoformat(extracted_iso).astimezone(_get_timezone())
            is_possible = False
            for r in available_ranges:
                if r['start'] <= proposed_start < r['end'] and proposed_start + datetime.timedelta(minutes=APPOINTMENT_DURATION_MINUTES) <= r['end']:
                    is_possible = True; break
            if is_possible:
                 # Dodatkowa weryfikacja z Google API dla pewności
                 if is_slot_actually_free(proposed_start, TARGET_CALENDAR_ID):
                     return text_for_user, extracted_iso
                 else:
                     logging.warning(f"!!! AI Error [{user_psid}]: Wygenerowany slot {extracted_iso} okazał się zajęty po weryfikacji API!")
                     return None, None # Nie proponuj zajętego
            else:
                 logging.error(f"!!! AI Error [{user_psid}]: Wygenerowany ISO '{extracted_iso}' poza dostępnymi zakresami!")
                 return None, None
        except ValueError: logging.error(f"!!! AI Error: Zły format ISO '{extracted_iso}'!"); return None, None
    else: logging.error(f"!!! AI Error [{user_psid}]: Brak znacznika ISO! Odp: '{generated_text}'"); return None, None

# --- Funkcja interakcji z Gemini (interpretacja feedbacku) ---
def get_gemini_feedback_decision(user_psid, user_feedback, history, last_proposal_text):
     if not gemini_model: logging.error(f"!!! [{user_psid}] Model niezaładowany!"); return "[CLARIFY]"
     user_content = Content(role="user", parts=[Part.from_text(user_feedback)])
     history_to_send = [h for h in history if not (isinstance(h, dict) and h.get('role') == 'system')] # Wyślij bez kontekstu
     max_messages = MAX_HISTORY_TURNS * 2
     if len(history_to_send) > max_messages: history_to_send = history_to_send[-max_messages:]
     instr = SYSTEM_INSTRUCTION_TEXT_FEEDBACK.format(last_proposal_text=last_proposal_text, user_feedback=user_feedback)
     prompt = [Content(role="user", parts=[Part.from_text(instr)])] + history_to_send + [user_content]
     # Usuwanie starych wiadomości z promptu (jak poprzednio)...
     while len(prompt) > (MAX_HISTORY_TURNS * 2 + 3) and len(prompt) > 3: prompt.pop(1); if len(prompt) > 3: prompt.pop(1)

     decision = _call_gemini(user_psid, prompt, GENERATION_CONFIG_FEEDBACK, "Feedback Interpretation", 1)
     if not decision: return "[CLARIFY]"
     if decision.startswith("[") and decision.endswith("]"):
         logging.info(f"[{user_psid}] AI feedback: {decision}"); return decision
     else: logging.warning(f"Ostrz. [{user_psid}]: AI nie zwróciło znacznika: '{decision}'. CLARIFY."); return "[CLARIFY]"

# --- Funkcja interakcji z Gemini (ogólna rozmowa) ---
def get_gemini_general_response(user_psid, current_user_message, history):
    if not gemini_model: logging.error(f"!!! [{user_psid}] Model niezaładowany!"); return "Przepraszam, błąd AI."
    user_content = Content(role="user", parts=[Part.from_text(current_user_message)])
    history_to_send = [h for h in history if not (isinstance(h, dict) and h.get('role') == 'system')]
    max_messages = MAX_HISTORY_TURNS * 2
    if len(history_to_send) > max_messages: history_to_send = history_to_send[-max_messages:]
    prompt = [Content(role="user", parts=[Part.from_text(SYSTEM_INSTRUCTION_GENERAL)]), Content(role="model", parts=[Part.from_text("Rozumiem.")])] + history_to_send + [user_content]
    # Usuwanie starych wiadomości z promptu...
    while len(prompt) > (MAX_HISTORY_TURNS * 2 + 2) and len(prompt) > 2: prompt.pop(2); if len(prompt) > 2: prompt.pop(2)
    response_text = _call_gemini(user_psid, prompt, GENERATION_CONFIG_DEFAULT, "General Conversation", 1)
    if response_text: return response_text
    else: return "Przepraszam, wystąpił błąd." # Generyczna odpowiedź

# --- Obsługa Weryfikacji Webhooka (GET) ---
@app.route('/webhook', methods=['GET'])
def webhook_verification():
    logging.info("--- GET weryfikacja ---"); hub_mode = request.args.get('hub.mode'); hub_token = request.args.get('hub.verify_token'); hub_challenge = request.args.get('hub.challenge')
    logging.info(f"Mode:{hub_mode},Token:{'OK' if hub_token==VERIFY_TOKEN else 'BŁĄD'},Challenge:{'Jest' if hub_challenge else 'Brak'}")
    if hub_mode == 'subscribe' and hub_token == VERIFY_TOKEN: logging.info("Weryfikacja GET OK!"); return Response(hub_challenge, status=200)
    else: logging.warning("Weryfikacja GET FAILED."); return Response("Verification failed", status=403)

# --- Główna Obsługa Webhooka (POST) ---
@app.route('/webhook', methods=['POST'])
def webhook_handle():
    logging.info(f"\n{'='*30} {datetime.datetime.now():%Y-%m-%d %H:%M:%S} POST {'='*30}")
    raw_data = request.data.decode('utf-8'); data = None
    try:
        data = json.loads(raw_data)
        if data and data.get("object") == "page":
            for entry in data.get("entry", []):
                page_id = entry.get("id"); timestamp = entry.get("time");
                for event in entry.get("messaging", []):
                    sender_id = event.get("sender", {}).get("id")
                    if not sender_id: continue
                    logging.info(f"--- Zdarzenie dla PSID: {sender_id} ---")
                    history, context = load_history(sender_id)
                    last_iso = context.get('last_proposed_slot_iso')
                    is_context = last_iso and context.get('message_index_in_file', -1) == len(load_history(sender_id)[0]) # Sprawdź index vs długość historii bez kontekstu

                    if is_context: logging.info(f"    Aktywny kontekst: {last_iso}")
                    elif last_iso: logging.info(f"    Kontekst stary. Reset."); last_iso = None

                    if message_data := event.get("message"):
                        if message_data.get("is_echo"): continue
                        user_input = message_data.get("text", "").strip()
                        user_content = Content(role="user", parts=[Part.from_text(user_input)]) if user_input else None
                        history_for_ai = [h for h in history if not (isinstance(h, dict) and h.get('role') == 'system')]

                        action = None; msg_result = None; ctx_save = None; model_resp = None
                        pref = 'any'; day = None; hour = None # Domyślne preferencje
                        proposal_text = None; proposed_iso = None # Dla nowej propozycji

                        if ENABLE_TYPING_DELAY and user_input: time.sleep(MIN_TYPING_DELAY_SECONDS)

                        if is_context and user_input: # Feedback do terminu
                            logging.info("      Oczekiwano na feedback. Pytanie AI...")
                            last_dt = datetime.datetime.fromisoformat(last_iso)
                            decision = get_gemini_feedback_decision(sender_id, user_input, history_for_ai, format_slot_for_user(last_dt))
                            if decision == "[ACCEPT]": action = 'book'
                            elif decision and decision.startswith("[REJECT_FIND_NEXT"):
                                action = 'find_and_propose'
                                pref_match = re.search(r"PREFERENCE='([^']*)'", decision); pref = pref_match.group(1) if pref_match else 'any'
                                if pref in ['specific_day', 'specific_datetime', 'specific_day_later', 'specific_day_earlier']:
                                    m_day=re.search(r"DAY='([^']*)'",decision); day=m_day.group(1) if m_day else None
                                if pref in ['specific_hour', 'specific_datetime']:
                                    m_hour=re.search(r"HOUR='(\d+)'",decision); hour=int(m_hour.group(1)) if m_hour and m_hour.group(1).isdigit() else None
                            elif decision == "[CLARIFY]": action = 'send_clarification'; msg_result = "Nie jestem pewien. Czy termin pasuje?"
                            else: action = 'send_error'; msg_result = "Problem z przetworzeniem."
                        elif user_input: # Normalna rozmowa
                            logging.info("      -> Gemini (normalna rozmowa)...")
                            response = get_gemini_general_response(sender_id, user_input, history_for_ai)
                            if response and INTENT_SCHEDULE_MARKER in response:
                                logging.info(f"      AI wykryło intencję [{INTENT_SCHEDULE_MARKER}].")
                                action = 'find_and_propose' # Pierwsze szukanie
                                msg_result = response.split(INTENT_SCHEDULE_MARKER,1)[0].strip() # Tekst przed znacznikiem
                                if not msg_result: msg_result = "Dobrze, sprawdzę dostępne terminy." # Domyślny tekst
                                pref = 'any'; day = None; hour = None # Użyj domyślnych preferencji
                            elif response: action = 'send_gemini_response'; msg_result = response
                            else: action = 'send_error'; msg_result = "Błąd AI."
                        elif attachments := message_data.get("attachments"):
                            att_type = attachments[0].get('type','?'); logging.info(f"      Załącznik: {att_type}.")
                            user_content = Content(role="user", parts=[Part.from_text(f"[Załącznik:{att_type}]")])
                            msg_result = "Nie obsługuję załączników."; action = 'send_error'
                        else: logging.warning(f"      Nieznany msg: {message_data}"); action = 'send_error'; msg_result="Nie rozumiem."

                        # Wykonanie akcji
                        logging.info(f"      Akcja: {action}")
                        if action == 'book':
                            # ... (logika book_appointment jak poprzednio) ...
                            try: tz = _get_timezone(); start = datetime.datetime.fromisoformat(last_iso).astimezone(tz); end = start + datetime.timedelta(minutes=APPOINTMENT_DURATION_MINUTES); prof=get_user_profile(sender_id); name=prof.get('first_name','') if prof else ''; ok, msg=book_appointment(TARGET_CALENDAR_ID, start, end, "Rezerwacja FB", f"PSID:{sender_id}\nImię:{name}", name); msg_result=msg; ctx_save=None
                            except Exception as e: logging.error(f"BŁĄD book: {e}"); msg_result="Błąd rezerwacji."; ctx_save=None
                        elif action == 'find_and_propose':
                            try: # Blok szukania i proponowania przez AI
                                tz = _get_timezone(); now = datetime.datetime.now(tz)
                                search_start = now; after_dt = now
                                if last_iso: # Jeśli to kolejne szukanie
                                    last_dt = datetime.datetime.fromisoformat(last_iso).astimezone(tz)
                                    after_dt = last_dt # Szukaj PO ostatnim
                                    search_start = last_dt + datetime.timedelta(minutes=1) # Domyślny start
                                    # Skoryguj search_start wg preferencji... (jak poprzednio)
                                    if pref in ['specific_day', 'specific_datetime', 'specific_day_later', 'specific_day_earlier'] and day:
                                        try: wd=POLISH_WEEKDAYS.index(day.capitalize()); ahead=(wd-now.weekday()+7)%7; if ahead==0 and now.time()>=datetime.time(WORK_END_HOUR): ahead=7; search_start = tz.localize(datetime.datetime.combine(now.date()+datetime.timedelta(days=ahead), datetime.time(WORK_START_HOUR,0)))
                                        except ValueError: pass
                                    elif pref == 'next_day': search_start = tz.localize(datetime.datetime.combine(last_dt.date()+datetime.timedelta(days=1),datetime.time(WORK_START_HOUR,0)))
                                    elif pref == 'later': base_start = last_dt+datetime.timedelta(minutes=APPOINTMENT_DURATION_MINUTES); later_start = last_dt+datetime.timedelta(hours=3); search_start=max(base_start,later_start);
                                    elif pref == 'afternoon': afternoon_start = tz.localize(datetime.datetime.combine(last_dt.date(),datetime.time(PREFERRED_WEEKDAY_START_HOUR,0))); search_start=max(search_start, afternoon_start)
                                    search_start = max(search_start, now) # Nie w przeszłości

                                search_end = tz.localize(datetime.datetime.combine((search_start + datetime.timedelta(days=MAX_SEARCH_DAYS)).date(), datetime.time(WORK_END_HOUR, 0)))
                                if ENABLE_TYPING_DELAY: time.sleep(MIN_TYPING_DELAY_SECONDS)
                                free_ranges = get_free_time_ranges(TARGET_CALENDAR_ID, search_start, search_end)
                                if free_ranges:
                                    logging.info(f"      Przekazanie {len(free_ranges)} zakresów do AI...")
                                    proposal_text, proposed_iso = get_gemini_slot_proposal(sender_id, history_for_gemini + ([user_content] if user_content else []), free_ranges)
                                    if proposal_text and proposed_iso:
                                        msg_result = proposal_text
                                        ctx_save = {'role': 'system', 'type': 'last_proposal', 'slot_iso': proposed_iso}
                                    else: msg_result = proposal_text or "Problem z wyborem terminu."; ctx_save = None # Błąd AI lub brak slotu
                                else: msg_result = "Brak wolnych terminów."; ctx_save = None
                            except Exception as find_err: logging.error(f"BŁĄD find/propose: {find_err}", exc_info=True); msg_result = "Błąd szukania."; ctx_save = None
                        # Wysyłanie wiadomości, jeśli jest coś do wysłania
                        if msg_result: send_message(sender_id, msg_result); model_resp = Content(role="model", parts=[Part.from_text(msg_result)])
                        # Zapis historii
                        if user_content and not history_saved:
                            hist_to_save = history + [user_content]
                            if model_resp: hist_to_save.append(model_resp)
                            save_history(sender_id, hist_to_save, context_to_save=ctx_save)
                        elif history_saved and ctx_save: # Zapisz tylko nowy kontekst
                             latest_hist, _ = load_history(sender_id); save_history(sender_id, latest_hist, context_to_save=ctx_save)
                    # ... (postback, read, delivery - jak poprzednio) ...
                    elif event.get("postback"): pass # Logika postback jak poprzednio
                    elif event.get("read"): logging.info(f"    Odczytane.")
                    elif event.get("delivery"): pass
                    else: logging.warning(f"    Nieobsługiwany event: {json.dumps(event)}")
            return Response("EVENT_RECEIVED", status=200)
        else: logging.warning(f"POST nie 'page': {data.get('object') if data else 'Brak'}"); return Response("OK", status=200)
    except json.JSONDecodeError as e: logging.error(f"!!! BŁĄD JSON: {e}\nDane: {raw_data[:500]}"); return Response("Invalid JSON", status=400)
    except Exception as e: logging.error(f"!!! KRYTYCZNY BŁĄD POST: {e}", exc_info=True); return Response("ERROR", status=200)

# --- Uruchomienie Serwera ---
if __name__ == '__main__':
    ensure_dir(HISTORY_DIR); port = int(os.environ.get("PORT", 8080)); debug = os.environ.get("FLASK_DEBUG", "False").lower() in ("true", "1", "yes")
    print("\n" + "="*50 + "\n--- START KONFIGURACJI BOTA ---")
    # ... (logi startowe jak poprzednio) ...
    if not VERIFY_TOKEN or VERIFY_TOKEN == "KOLAGEN": print("!!! OSTRZ.: FB_VERIFY_TOKEN domyślny!")
    else: print("  FB_VERIFY_TOKEN: OK")
    if not PAGE_ACCESS_TOKEN: print("\n!!! KRYTYCZNE: FB_PAGE_ACCESS_TOKEN PUSTY!\n");
    elif len(PAGE_ACCESS_TOKEN) < 50: print("\n!!! KRYTYCZNE: FB_PAGE_ACCESS_TOKEN ZBYT KRÓTKI!\n")
    else:
        print("  FB_PAGE_ACCESS_TOKEN: Ustawiony (OK)")
        if PAGE_ACCESS_TOKEN=="EACNAHFzEhkUBO4ypcoyQfWIgNc0YLZA1aCr9n3BzpvSJLoBTJnv5rWZBmc7HlqF6uUWt1uAp6aDZB8ZAb0RRT45qVIfGnciQX6wBKrZColGARfVLXP5Ic6Ptrj5AUvom4Rt12hyBxcjIJGes76fvdvBhiBZCJ0ZCVfkQMZBZCBatJshSZA8hFuRyKd58b50wkhVCMZCuwZDZD": print("\n!!! UWAGA: Używany DOMYŚLNY PAGE_ACCESS_TOKEN!\n")
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
    else: print("Start serwera dev w trybie DEBUG..."); logging.getLogger().setLevel(logging.DEBUG); print("Logowanie DEBUG włączone."); app.run(host='0.0.0.0', port=port, debug=True)
