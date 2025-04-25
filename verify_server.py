# -*- coding: utf-8 -*-

# verify_server.py (połączony kod z AI wybierającym i proponującym termin)

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

# --- Importy Google Calendar ---
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
# ------------------------------

app = Flask(__name__)

# --- Konfiguracja Ogólna ---
VERIFY_TOKEN = os.environ.get("FB_VERIFY_TOKEN", "KOLAGEN")
PAGE_ACCESS_TOKEN = os.environ.get("FB_PAGE_ACCESS_TOKEN", "EACNAHFzEhkUBO7nbFAtYvfPWbEht1B3chQqWLx76Ljg2ekdbJYoOrnpjATqhS0EZC8S0q8a49hEZBaZByZCaj5gr1z62dAaMgcZA1BqFOruHfFo86EWTbI3S9KL59oxFWfZCfCjwbQra9lY5of1JVnj2c9uFJDhIpWlXxLLao9Cv8JKssgs3rEDxIJBRr26HgUewZDZD") # WAŻNE: Podaj swój prawdziwy token!
PROJECT_ID = os.environ.get("GCP_PROJECT_ID", "linear-booth-450221-k1")
LOCATION = os.environ.get("GCP_LOCATION", "us-central1")
MODEL_ID = os.environ.get("VERTEX_MODEL_ID", "gemini-2.0-flash-001") # Przywrócony model

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
MAX_SLOTS_FOR_AI = 15 # Ile max slotów przekazać AI do wyboru

# --- Inicjalizacja Zmiennych Globalnych dla Kalendarza ---
_calendar_service = None
_tz = None

# --- Lista Polskich Dni Tygodnia ---
POLISH_WEEKDAYS = ["Poniedziałek", "Wtorek", "Środa", "Czwartek", "Piątek", "Sobota", "Niedziela"]

# --- Ustawienia Lokalizacji ---
try: locale.setlocale(locale.LC_TIME, 'pl_PL.UTF-8')
except locale.Error:
    try: locale.setlocale(locale.LC_TIME, 'Polish_Poland.1250')
    except locale.Error: print("Ostrzeżenie: Nie można ustawić polskiej lokalizacji.")

# =====================================================================
# === FUNKCJE POMOCNICZE ==============================================
# =====================================================================
# (Funkcje ensure_dir, get_user_profile, load_history, save_history, _get_timezone,
#  get_calendar_service, parse_event_time, get_free_slots, book_appointment,
#  format_slot_for_user - BEZ ZMIAN od ostatniej wersji)
# Dodano funkcję do formatowania listy slotów dla AI
# Usunięto find_next_reasonable_slot, bo AI będzie wybierać

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

# ZMIANA: Ta funkcja nie jest już potrzebna, AI będzie wybierać
# def find_next_reasonable_slot(...)

# ZMIANA: Funkcja formatująca sloty dla AI
def format_slots_for_ai(slots):
    """Formatuje listę slotów datetime na tekst dla AI, zawierający ISO string."""
    if not slots: return "Brak dostępnych terminów."
    formatted_list = ["Dostępne terminy (wybierz jeden i zaproponuj, dołączając jego ISO string w formacie [SLOT_ISO:...]):"]
    for slot in slots:
        iso_str = slot.isoformat()
        day_name = POLISH_WEEKDAYS[slot.weekday()]
        hour_str = str(slot.hour)
        readable_part = f"{day_name}, {slot.strftime(f'%d.%m.%Y o {hour_str}:%M')}"
        formatted_list.append(f"- [SLOT_ISO:{iso_str}] {readable_part}")
    return "\n".join(formatted_list)

def format_slot_for_user(slot_start): # Ta funkcja zostaje
    if not isinstance(slot_start, datetime.datetime): return ""
    day_index = slot_start.weekday(); day_name = POLISH_WEEKDAYS[day_index]
    hour_str = str(slot_start.hour)
    return f"{day_name}, {slot_start.strftime(f'%d.%m.%Y o {hour_str}:%M')}"

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

# --- INSTRUKCJA SYSTEMOWA (dla AI wybierającego termin) ---
SYSTEM_INSTRUCTION_TEXT_PROPOSE = """Jesteś profesjonalnym asystentem klienta 'Zakręcone Korepetycje'. Twoim zadaniem jest przeanalizowanie historii rozmowy i listy dostępnych terminów, a następnie wybranie **jednego**, najbardziej odpowiedniego terminu i zaproponowanie go użytkownikowi.

**Kontekst:** Rozmawiasz o korepetycjach online (matematyka, j. polski, j. angielski, kl. 4 SP - matura). Użytkownik wyraził zainteresowanie umówieniem pierwszej lekcji próbnej (płatnej wg cennika).

**Cennik (60 min):** 4-8 SP: 60 zł; 1-3 LO/Tech(P): 65 zł; 1-3 LO/Tech(R): 70 zł; 4 LO/Tech(P): 70 zł; 4 LO/Tech(R): 75 zł.

**Dostępne terminy:**
{available_slots_text}

**Twoje zadanie:**
1.  Przeanalizuj historię rozmowy pod kątem ewentualnych preferencji użytkownika (np. "popołudniu", "wtorek", "po 16").
2.  Wybierz **jeden** termin z powyższej listy "Dostępne terminy", który najlepiej pasuje do preferencji użytkownika LUB jeśli brak preferencji, wybierz termin "rozsądny" (np. popołudnie w tygodniu, okolice południa w weekend).
3.  Sformułuj **krótką, naturalną propozycję** tego terminu, pytając użytkownika o akceptację.
4.  **Absolutnie kluczowe:** W swojej odpowiedzi **musisz** zawrzeć identyfikator ISO wybranego terminu w specjalnym znaczniku `[SLOT_ISO:TWOJ_WYBRANY_ISO_STRING]`. Znacznik ten musi być częścią odpowiedzi, ale może być na końcu lub wpleciony w zdanie.

**Przykład dobrej odpowiedzi (jeśli wybrałeś termin z ISO '2025-05-06T16:00:00+02:00'):**
"Znalazłem dla Państwa termin: Wtorek, 06.05.2025 o 16:00. [SLOT_ISO:2025-05-06T16:00:00+02:00] Czy taki termin by odpowiadał?"

**Zasady:**
*   Odpowiadaj po polsku.
*   Bądź uprzejmy i profesjonalny.
*   **Nie proponuj** terminów spoza dostarczonej listy.
*   **Zawsze** dołączaj znacznik `[SLOT_ISO:...]` z poprawnym ISO stringiem wybranego terminu.
"""
# ---------------------------------------------------------------------

# --- INSTRUKCJA SYSTEMOWA (dla AI interpretującego feedback) ---
SYSTEM_INSTRUCTION_TEXT_FEEDBACK = """Jesteś profesjonalnym asystentem klienta 'Zakręcone Korepetycje'. Twoim zadaniem jest zinterpretowanie odpowiedzi użytkownika na propozycję terminu lekcji próbnej.

**Kontekst:** System właśnie zaproponował użytkownikowi termin. Ostatnia propozycja systemu brzmiała:
"{last_proposal_text}"

**Odpowiedź użytkownika:**
"{user_feedback}"

**Twoje zadanie:**
Przeanalizuj odpowiedź użytkownika i zdecyduj, co należy zrobić dalej. **Odpowiedz TYLKO I WYŁĄCZNIE jednym z poniższych znaczników akcji:**

*   `[ACCEPT]`: Jeśli użytkownik akceptuje proponowany termin (słowa kluczowe: tak, pasuje, ok, super, zgadzam się, dobrze, może być itp.).
*   `[REJECT_FIND_NEXT PREFERENCE='later']`: Jeśli użytkownik odrzuca termin i chce po prostu inny/późniejszy (słowa kluczowe: nie, nie pasuje, inny, później, następny, dalej, może inny, za późno itp.).
*   `[REJECT_FIND_NEXT PREFERENCE='afternoon']`: Jeśli użytkownik mówi, że proponowany termin jest "za wcześnie" lub prosi o termin popołudniowy.
*   `[REJECT_FIND_NEXT PREFERENCE='next_day']`: Jeśli użytkownik prosi o inny dzień, jutro, w tygodniu (jeśli propozycja była w weekend).
*   `[REJECT_FIND_NEXT PREFERENCE='specific_day' DAY='NAZWA_DNIA']`: Jeśli użytkownik prosi o konkretny dzień tygodnia (np. "wolałbym wtorek"). Zastąp NAZWA_DNIA polską nazwą dnia.
*   `[REJECT_FIND_NEXT PREFERENCE='specific_hour' HOUR='GODZINA']`: Jeśli użytkownik prosi o konkretną godzinę (np. "może o 14?"). Zastąp GODZINA liczbą.
*   `[CLARIFY]`: Jeśli odpowiedź użytkownika jest niejasna, zadaje pytanie niezwiązane z terminem lub nie da się jednoznacznie określić jego intencji.

**Ważne:** Twoja odpowiedź musi być *dokładnie* jednym z powyższych znaczników, bez żadnego dodatkowego tekstu.
"""
# ---------------------------------------------------------------------

# --- Funkcja interakcji z Gemini (proponowanie slotu) ---
def get_gemini_slot_proposal(user_psid, history, available_slots):
    if not gemini_model: print(f"!!! BŁĄD [{user_psid}]: Model Gemini niezaładowany!"); return None
    if not available_slots: print(f"[{user_psid}]: Brak slotów do zaproponowania."); return None # Zwróć None jeśli lista pusta

    # Przygotuj listę slotów dla AI
    slots_text_for_ai = format_slots_for_ai(available_slots[:MAX_SLOTS_FOR_AI]) # Przekaż ograniczoną liczbę

    # Przygotuj prompt
    current_instruction = SYSTEM_INSTRUCTION_TEXT_PROPOSE.format(available_slots_text=slots_text_for_ai)
    max_messages_to_send = MAX_HISTORY_TURNS * 2
    history_to_send = history[-max_messages_to_send:] if len(history) > max_messages_to_send else history
    prompt_content = [
        Content(role="user", parts=[Part.from_text(current_instruction)]),
        Content(role="model", parts=[Part.from_text("Rozumiem. Wybiorę najlepszy termin z listy i zaproponuję go, dołączając znacznik [SLOT_ISO:...].")])
    ] + history_to_send

    print(f"\n--- [{user_psid}] Zawartość do Gemini ({MODEL_ID}) - Propozycja Slotu ---");
    # Logowanie promptu...
    print(f"--- Koniec zawartości {user_psid} ---\n")
    try:
        generation_config = GenerationConfig(temperature=0.5, top_p=0.95, top_k=40) # Może niższa temperatura dla wyboru?
        safety_settings = {HarmCategory.HARM_CATEGORY_HARASSMENT: HarmBlockThreshold.BLOCK_MEDIUM_AND_ABOVE, #... itd ...
                           HarmCategory.HARM_CATEGORY_HATE_SPEECH: HarmBlockThreshold.BLOCK_MEDIUM_AND_ABOVE,
                           HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT: HarmBlockThreshold.BLOCK_MEDIUM_AND_ABOVE,
                           HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT: HarmBlockThreshold.BLOCK_MEDIUM_AND_ABOVE,}
        response = gemini_model.generate_content(prompt_content, generation_config=generation_config, safety_settings=safety_settings, stream=False)
        if response.candidates and response.candidates[0].content and response.candidates[0].content.parts:
            generated_text = response.candidates[0].content.parts[0].text.strip()
            print(f"[{user_psid}] Gemini zaproponował: '{generated_text}'")
            # --- Walidacja odpowiedzi AI ---
            iso_match = re.search(r"\[SLOT_ISO:([^\]]+)\]", generated_text)
            if iso_match:
                extracted_iso = iso_match.group(1)
                # Sprawdź, czy wyekstrahowany ISO jest na liście dostępnych slotów
                slot_exists = any(slot.isoformat() == extracted_iso for slot in available_slots[:MAX_SLOTS_FOR_AI])
                if slot_exists:
                    # Usuń znacznik z tekstu dla użytkownika
                    text_for_user = re.sub(r"\[SLOT_ISO:[^\]]+\]", "", generated_text).strip()
                    return text_for_user, extracted_iso # Zwróć tekst i ISO
                else:
                    print(f"!!! BŁĄD AI [{user_psid}]: Zaproponowany ISO '{extracted_iso}' nie ma na liście dostępnych!")
                    return None, None # Błąd - AI wymyśliło slot
            else:
                print(f"!!! BŁĄD AI [{user_psid}]: Brak znacznika [SLOT_ISO:...] w odpowiedzi!")
                return None, None # Błąd - AI nie dołączyło znacznika
        else: print(f"!!! [{user_psid}] Odp. Gemini pusta/zablokowana przy propozycji."); return None, None
    except Exception as e: print(f"!!! BŁĄD Gemini ({MODEL_ID}) propozycji: {e} !!!"); return None, None

# --- Funkcja interakcji z Gemini (interpretacja feedbacku) ---
def get_gemini_feedback_decision(user_psid, user_feedback, history, last_proposal_text):
     if not gemini_model: print(f"!!! BŁĄD [{user_psid}]: Model Gemini niezaładowany!"); return "[CLARIFY]" # Fallback
     user_content = Content(role="user", parts=[Part.from_text(user_feedback)])
     max_messages_to_send = MAX_HISTORY_TURNS * 2
     history_to_send = history[-max_messages_to_send:] if len(history) > max_messages_to_send else history
     current_instruction = SYSTEM_INSTRUCTION_TEXT_FEEDBACK.format(last_proposal_text=last_proposal_text, user_feedback=user_feedback)
     prompt_content = [
         Content(role="user", parts=[Part.from_text(current_instruction)]) # Tylko instrukcja i pytanie
     ] + history_to_send + [user_content] # Dodaj historię i feedback usera

     print(f"\n--- [{user_psid}] Zawartość do Gemini ({MODEL_ID}) - Interpretacja Feedbacku ---");
     # Logowanie...
     print(f"--- Koniec zawartości {user_psid} ---\n")
     try:
         generation_config = GenerationConfig(temperature=0.2, top_p=0.95, top_k=40) # Niska temp dla znaczników
         safety_settings = {HarmCategory.HARM_CATEGORY_HARASSMENT: HarmBlockThreshold.BLOCK_MEDIUM_AND_ABOVE, #... itd ...
                           HarmCategory.HARM_CATEGORY_HATE_SPEECH: HarmBlockThreshold.BLOCK_MEDIUM_AND_ABOVE,
                           HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT: HarmBlockThreshold.BLOCK_MEDIUM_AND_ABOVE,
                           HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT: HarmBlockThreshold.BLOCK_MEDIUM_AND_ABOVE,}
         response = gemini_model.generate_content(prompt_content, generation_config=generation_config, safety_settings=safety_settings, stream=False)
         if response.candidates and response.candidates[0].content and response.candidates[0].content.parts:
             decision = response.candidates[0].content.parts[0].text.strip()
             print(f"[{user_psid}] Gemini zdecydował (feedback): '{decision}'")
             # Podstawowa walidacja znacznika
             if decision.startswith("[") and decision.endswith("]"):
                 return decision
             else:
                 print(f"Ostrz. [{user_psid}]: Gemini nie zwrócił poprawnego znacznika, zwrócił tekst: '{decision}'. Traktuję jako CLARIFY.")
                 return "[CLARIFY]" # Jeśli nie jest to znacznik, poproś o wyjaśnienie
         else: print(f"!!! [{user_psid}] Odp. Gemini pusta/zablokowana przy feedbacku."); return "[CLARIFY]"
     except Exception as e: print(f"!!! BŁĄD Gemini ({MODEL_ID}) feedbacku: {e} !!!"); return "[CLARIFY]" # Fallback

# --- Obsługa Weryfikacji Webhooka (GET) ---
@app.route('/webhook', methods=['GET'])
def webhook_verification():
    print("--- GET weryfikacja ---"); hub_mode = request.args.get('hub.mode'); hub_token = request.args.get('hub.verify_token'); hub_challenge = request.args.get('hub.challenge')
    print(f"Mode:{hub_mode},Token:{'OK' if hub_token==VERIFY_TOKEN else 'BŁĄD'},Challenge:{'Jest' if hub_challenge else 'Brak'}")
    if hub_mode == 'subscribe' and hub_token == VERIFY_TOKEN: print("Weryfikacja GET OK!"); return Response(hub_challenge, status=200)
    else: print("Weryfikacja GET FAILED."); return Response("Verification failed", status=403)

# --- Główna Obsługa Webhooka (POST) ---
@app.route('/webhook', methods=['POST'])
def webhook_handle():
    print("\n-----------------"); print(f"--- {datetime.datetime.now():%Y-%m-%d %H:%M:%S} POST ---")
    raw_data = request.data.decode('utf-8'); data = None
    try:
        data = json.loads(raw_data)
        if data and data.get("object") == "page":
            for entry in data.get("entry", []):
                page_id = entry.get("id"); timestamp = entry.get("time");
                for messaging_event in entry.get("messaging", []):
                    if "sender" not in messaging_event or "id" not in messaging_event["sender"]: continue
                    sender_id = messaging_event["sender"]["id"]; print(f"  -> PSID: {sender_id}")
                    history, context = load_history(sender_id)
                    last_proposed_slot_iso = context.get('last_proposed_slot_iso')
                    is_context_current = last_proposed_slot_iso and context.get('message_index', -1) == len(history)

                    if is_context_current: print(f"    Aktywny kontekst 'last_proposed_slot_iso'.")
                    elif last_proposed_slot_iso: print(f"    Kontekst 'last_proposal' stary. Reset."); last_proposed_slot_iso = None

                    if messaging_event.get("message"):
                        message_data = messaging_event["message"]; message_id = message_data.get("mid"); print(f"    Msg (ID:{message_id})")
                        if message_data.get("is_echo"): print("      Echo."); continue
                        user_input_text = None;
                        if "text" in message_data: user_input_text = message_data["text"]; print(f"      Txt: '{user_input_text}'")

                        # --- Nowa Logika Główna ---
                        if user_input_text:
                            user_content = Content(role="user", parts=[Part.from_text(user_input_text)])
                            action_to_perform = None; text_to_send = None; context_to_save = None
                            history_for_gemini = [h for h in history if not (isinstance(h, dict) and h.get('role') == 'system')]

                            # 1. Jeśli oczekiwano na feedback
                            if is_context_current:
                                print(f"      Oczekiwano na feedback. Pytanie Gemini o decyzję...")
                                last_proposal_dt = datetime.datetime.fromisoformat(last_proposed_slot_iso)
                                proposal_info_for_ai = f"Zaproponowano: {format_slot_for_user(last_proposal_dt)}."
                                gemini_decision = get_gemini_feedback_decision(sender_id, user_input_text, history_for_gemini, proposal_info_for_ai)

                                if gemini_decision == "[ACCEPT]": action_to_perform = 'book'
                                elif isinstance(gemini_decision, str) and gemini_decision.startswith("[FIND_NEXT_SLOT"):
                                    action_to_perform = 'find_and_propose' # Akcja: znajdź i niech AI zaproponuje
                                    # Parsuj preferencje (jak poprzednio)
                                    preference = 'later'; requested_day_str = None; requested_hour_int = None; force_afternoon = False
                                    pref_match = re.search(r"PREFERENCE='([^']*)'", gemini_decision); #... itd ...
                                    if pref_match: preference = pref_match.group(1)
                                    day_match = re.search(r"DAY='([^']*)'", gemini_decision)
                                    if day_match: requested_day_str = day_match.group(1)
                                    hour_match = re.search(r"HOUR='(\d+)'", gemini_decision)
                                    if hour_match: requested_hour_int = int(hour_match.group(1))
                                    if preference == 'afternoon': force_afternoon = True
                                elif gemini_decision == "[CLARIFY]" or (isinstance(gemini_decision, str) and not gemini_decision.startswith("[")):
                                     action_to_perform = 'send_clarification_from_ai'
                                     text_to_send = gemini_decision if gemini_decision != "[CLARIFY]" else "Nie jestem pewien, co masz na myśli. Czy termin pasuje?"
                                     context_to_save = {'role':'system', 'type':'last_proposal', 'slot_iso': last_proposed_slot_iso} # Utrzymaj kontekst
                                else: action_to_perform = 'send_error'; text_to_send = "Problem z interpretacją odpowiedzi."

                            # 2. Jeśli NIE oczekiwano na feedback
                            else:
                                print(f"      -> Gemini (normalna rozmowa)...");
                                # Wywołaj Gemini z normalnym promptem
                                gemini_response = get_gemini_response(sender_id, user_input_text, history_for_gemini) # Użyj starej funkcji do rozmowy
                                if isinstance(gemini_response, str) and gemini_response:
                                    action_to_perform = 'send_gemini_response'
                                    text_to_send = gemini_response
                                    trigger_keywords = ["umówić", "termin", "wolne", "kiedy", "zapisać", "kalendarz", "rezerw", "dostępn"]
                                    user_wants_to_schedule = any(keyword in user_input_text.lower() for keyword in trigger_keywords)
                                    confirm_keywords = ["oczywiście", "jasne", "proszę", "sprawdz", "termin", "chętnie", "znajdę"]
                                    gemini_confirms_scheduling = any(keyword in gemini_response.lower() for keyword in confirm_keywords)
                                    if user_wants_to_schedule or gemini_confirms_scheduling:
                                        action_to_perform = 'find_and_propose' # Zmień akcję na szukanie i proponowanie przez AI
                                        preference = 'first_reasonable' # Domyślna preferencja dla pierwszego szukania
                                        requested_day_str = None; requested_hour_int = None; force_afternoon = False
                                else: action_to_perform = 'send_error'; text_to_send = gemini_response or "Błąd AI."

                            # --- Wykonanie Akcji przez Pythona ---
                            print(f"      Wykonanie akcji: {action_to_perform}")
                            model_response_content = None # Content odpowiedzi modelu do zapisu

                            if action_to_perform == 'book':
                                try:
                                    tz = _get_timezone(); start_time = datetime.datetime.fromisoformat(last_proposed_slot_iso).astimezone(tz); end_time = start_time + datetime.timedelta(minutes=APPOINTMENT_DURATION_MINUTES)
                                    user_profile = get_user_profile(sender_id); user_name = user_profile.get('first_name', '') if user_profile else ''
                                    if ENABLE_TYPING_DELAY: time.sleep(MIN_TYPING_DELAY_SECONDS)
                                    success, message_to_user = book_appointment(TARGET_CALENDAR_ID, start_time, end_time, summary=f"Rezerwacja FB", description=f"PSID:{sender_id}\nImię:{user_name}", user_name=user_name)
                                    send_message(sender_id, message_to_user); model_response_content = Content(role="model", parts=[Part.from_text(message_to_user)])
                                except Exception as book_err: print(f"!!! BŁĄD rezerwacji: {book_err} !!!"); send_message(sender_id, "Błąd rezerwacji."); model_response_content = Content(role="model", parts=[Part.from_text("Błąd rezerwacji.")])
                                context_to_save = None # Usuń kontekst po próbie rezerwacji
                            elif action_to_perform == 'find_and_propose':
                                try:
                                    tz = _get_timezone(); now = datetime.datetime.now(tz)
                                    search_start = now; after_dt = now
                                    if last_proposed_slot_iso and action_to_perform == 'find_next': # Jeśli szukamy następnego
                                        last_proposed_dt = datetime.datetime.fromisoformat(last_proposed_slot_iso).astimezone(tz)
                                        after_dt = last_proposed_dt
                                        search_start = last_proposed_dt + datetime.timedelta(minutes=1)
                                        # ... (logika ustalania search_start wg preferencji jak poprzednio) ...
                                        target_weekday = -1
                                        if preference == 'specific_day' and requested_day_str:
                                            try: target_weekday = POLISH_WEEKDAYS.index(requested_day_str.capitalize())
                                            except ValueError: pass
                                        if target_weekday != -1:
                                            days_ahead = target_weekday - last_proposed_dt.weekday();
                                            if days_ahead <= 0: days_ahead += 7
                                            search_start = tz.localize(datetime.datetime.combine(last_proposed_dt.date() + datetime.timedelta(days=days_ahead), datetime.time(0,0)))
                                        elif preference == 'next_day': search_start = tz.localize(datetime.datetime.combine(last_proposed_dt.date() + datetime.timedelta(days=1), datetime.time(0,0)))
                                        elif preference == 'later':
                                             jump_hours = 3; base_start = last_proposed_dt + datetime.timedelta(minutes=APPOINTMENT_DURATION_MINUTES); later_start = last_proposed_dt + datetime.timedelta(hours=jump_hours)
                                             search_start = max(base_start, later_start)
                                             if last_proposed_dt.hour < PREFERRED_WEEKDAY_START_HOUR and last_proposed_dt.weekday() < 5: afternoon_start = tz.localize(datetime.datetime.combine(last_proposed_dt.date(), datetime.time(PREFERRED_WEEKDAY_START_HOUR, 0))); search_start = max(search_start, afternoon_start)
                                        elif preference == 'afternoon':
                                             afternoon_start = tz.localize(datetime.datetime.combine(last_proposed_dt.date(), datetime.time(PREFERRED_WEEKDAY_START_HOUR, 0))); search_start = max(search_start, afternoon_start)

                                    if search_start < now: search_start = now # Nie szukaj w przeszłości
                                    print(f"      Ustalono search_start: {search_start:%Y-%m-%d %H:%M}")

                                    search_end_date = (search_start + datetime.timedelta(days=MAX_SEARCH_DAYS)).date()
                                    search_end = tz.localize(datetime.datetime.combine(search_end_date, datetime.time(23, 59, 59)))
                                    if ENABLE_TYPING_DELAY: print(f"      Szukanie slotów..."); time.sleep(MIN_TYPING_DELAY_SECONDS)
                                    free_slots = get_free_slots(TARGET_CALENDAR_ID, search_start, search_end)

                                    # --- Wywołaj AI, aby wybrało i zaproponowało slot ---
                                    if free_slots:
                                        # Usuń 'after_time' z argumentów, bo AI dostaje całą listę
                                        # Możemy wstępnie odfiltrować listę wg preferencji GODZINOWYCH tutaj, aby pomóc AI
                                        filtered_slots = free_slots
                                        if requested_hour_int is not None: filtered_slots = [s for s in free_slots if s.hour == requested_hour_int]
                                        elif force_afternoon: filtered_slots = [s for s in free_slots if s.hour >= PREFERRED_WEEKDAY_START_HOUR]
                                        if not filtered_slots: filtered_slots = free_slots # Jeśli filtrowanie nic nie dało, użyj pełnej listy

                                        print(f"      Przekazanie {len(filtered_slots)} slotów do AI (max {MAX_SLOTS_FOR_AI})...")
                                        proposal_text, proposed_iso = get_gemini_slot_proposal(sender_id, history_for_gemini, filtered_slots)

                                        if proposal_text and proposed_iso:
                                            send_message(sender_id, proposal_text)
                                            model_response_content = Content(role="model", parts=[Part.from_text(proposal_text)])
                                            context_to_save = {'role': 'system', 'type': 'last_proposal', 'slot_iso': proposed_iso}
                                        else: # Błąd AI przy propozycji
                                            error_msg = "Przepraszam, mam problem z zaproponowaniem konkretnego terminu. Spróbujmy jeszcze raz."
                                            send_message(sender_id, error_msg)
                                            model_response_content = Content(role="model", parts=[Part.from_text(error_msg)])
                                            context_to_save = None # Usuń kontekst
                                    else: # Brak slotów
                                        no_slots_msg = "Niestety, nie znalazłem pasujących wolnych terminów."
                                        send_message(sender_id, no_slots_msg)
                                        model_response_content = Content(role="model", parts=[Part.from_text(no_slots_msg)])
                                        context_to_save = None
                                except Exception as find_err: print(f"!!! BŁĄD find_and_propose: {find_err} !!!"); send_message(sender_id, "Błąd szukania."); model_response_content = Content(role="model", parts=[Part.from_text("Błąd szukania.")])
                            elif action_to_perform == 'send_gemini_response' or action_to_perform == 'send_clarification' or action_to_perform == 'send_error':
                                 if ENABLE_TYPING_DELAY and text_to_send: time.sleep(MIN_TYPING_DELAY_SECONDS)
                                 send_message(sender_id, text_to_send); model_response_content = Content(role="model", parts=[Part.from_text(text_to_send)])
                                 # Context_to_save jest już ustawiony lub None
                            else: print(f"!!! Nie wykonano akcji dla: {user_input_text}")

                            # Zapisz historię na końcu, uwzględniając odpowiedź modelu (jeśli była) i kontekst
                            if model_response_content: save_history(sender_id, history + [user_content, model_response_content], context_to_save=context_to_save)
                            else: save_history(sender_id, history + [user_content], context_to_save=context_to_save) # Zapisz tylko usera jeśli nie było odp. modelu

                        # ... (obsługa załączników i nieznanych typów wiadomości) ...
                        elif "attachments" in message_data: attachment_type = message_data['attachments'][0].get('type', 'nieznany'); print(f"      Załącznik: {attachment_type}."); user_content = Content(role="user", parts=[Part.from_text(f"[Załącznik: {attachment_type}]")]); model_content = Content(role="model", parts=[Part.from_text("Nie obsługuję załączników.")]); save_history(sender_id, history + [user_content, model_content]); send_message(sender_id, "Przepraszam, nie obsługuję załączników.")
                        else: print(f"      Nieznany typ wiadomości: {message_data}"); user_content = Content(role="user", parts=[Part.from_text("[Nieznany typ wiadomości]")]); model_content = Content(role="model", parts=[Part.from_text("Nie rozumiem.")]); save_history(sender_id, history + [user_content, model_content]); send_message(sender_id, "Nie rozumiem.")

                    # ... (reszta obsługi postback, read, delivery) ...
                    elif messaging_event.get("postback"):
                         # UWAGA: Logika Postback nie została dostosowana do nowego przepływu AI!
                         # Może wymagać podobnej refaktoryzacji, jeśli przyciski mają inicjować szukanie/rezerwację.
                         postback_data = messaging_event["postback"]; payload = postback_data.get("payload"); title = postback_data.get("title", payload); print(f"    Postback: T:'{title}', P:'{payload}'")
                         prompt_for_button = f"Kliknięto: '{title}' ({payload})."
                         # Użyj ogólnej funkcji odpowiedzi, bez kontekstu slotów
                         response_text = get_gemini_response(sender_id, prompt_for_button, [h for h in history if not (isinstance(h, dict) and h.get('role') == 'system')])
                         if isinstance(response_text, str) and response_text:
                             if ENABLE_TYPING_DELAY:
                                 response_len = len(response_text); calculated_delay = response_len / TYPING_CHARS_PER_SECOND; final_delay = max(0, min(MAX_TYPING_DELAY_SECONDS, calculated_delay + MIN_TYPING_DELAY_SECONDS))
                                 if final_delay > 0: print(f"      Pisanie (postback)... {final_delay:.2f}s"); time.sleep(final_delay)
                             user_content = Content(role="user", parts=[Part.from_text(prompt_for_button)]); model_content = Content(role="model", parts=[Part.from_text(response_text)])
                             save_history(sender_id, history + [user_content, model_content]) # Zapisz normalnie
                             send_message(sender_id, response_text)
                         else: user_content = Content(role="user", parts=[Part.from_text(prompt_for_button)]); save_history(sender_id, history + [user_content]); send_message(sender_id, response_text or "Błąd.")
                    elif messaging_event.get("read"): print(f"    Typ: read.")
                    elif messaging_event.get("delivery"): print(f"    Typ: delivery.")
                    else: print(f"    Inne zdarzenie: {messaging_event}")
        else: print("Otrzymano POST nie 'page':", data.get("object"))
    except json.JSONDecodeError as json_err: print(f"!!! BŁĄD JSON: {json_err} !!!"); print(f"   Dane: {raw_data[:500]}"); return Response("Invalid JSON", status=400)
    except Exception as e: import traceback; print(f"!!! KRYTYCZNY BŁĄD POST: {e} !!!"); traceback.print_exc(); return Response("ERROR", status=200)
    return Response("OK", status=200)

# --- Uruchomienie Serwera ---
if __name__ == '__main__':
    ensure_dir(HISTORY_DIR); port = int(os.environ.get("PORT", 8080)); debug_mode = os.environ.get("FLASK_DEBUG", "False").lower() in ("true", "1", "yes")
    print("="*50); print("--- Konfiguracja Bota ---")
    # ... (reszta logów startowych) ...
    if not VERIFY_TOKEN or VERIFY_TOKEN == "KOLAGEN": print("!!! OSTRZEŻENIE: FB_VERIFY_TOKEN domyślny/pusty!")
    else: print("  FB_VERIFY_TOKEN: Ustawiony")
    if not PAGE_ACCESS_TOKEN or len(PAGE_ACCESS_TOKEN) < 50: print("!!!!!!!!!!!!!!!!!!!!!!!!!! KRYTYCZNE OSTRZEŻENIE: FB_PAGE_ACCESS_TOKEN PUSTY/NIEPOPRAWNY?! Bot nie będzie działał! !!!!!!!!!!!!!!!!!!!!!!!!!!")
    else: print("  FB_PAGE_ACCESS_TOKEN: Ustawiony")
    print(f"  Katalog historii: {HISTORY_DIR}"); print(f"  Projekt Vertex AI: {PROJECT_ID}"); print(f"  Lokalizacja Vertex AI: {LOCATION}")
    print(f"  Model Vertex AI: {MODEL_ID}"); print(f"  Docelowy Kalendarz ID: {TARGET_CALENDAR_ID}")
    print(f"  Symulacja pisania: {'Włączona' if ENABLE_TYPING_DELAY else 'Wyłączona'}")
    if gemini_model is None: print("!!! OSTRZEŻENIE: Model Gemini NIE załadowany!")
    print("="*50)
    print(f"Uruchamianie serwera Flask na porcie {port} (debug={debug_mode})...")
    if debug_mode: app.run(host='0.0.0.0', port=port, debug=True)
    else:
        try: from waitress import serve; print("Uruchamianie serwera Waitress..."); serve(app, host='0.0.0.0', port=port)
        except ImportError: print("Waitress nie zainstalowany. Uruchamianie serwera Flask (dev)."); app.run(host='0.0.0.0', port=port, debug=False)
