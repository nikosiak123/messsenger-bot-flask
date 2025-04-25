# -*- coding: utf-8 -*-

# verify_server.py (połączony kod app.py i calendar_utils.py)

from flask import Flask, request, Response
import os
import json
import requests # Do wysyłania wiadomości do FB API
import time     # Potrzebne do opóźnienia między wiadomościami ORAZ do symulacji pisania
import vertexai # Do komunikacji z Vertex AI
from vertexai.generative_models import (
    GenerativeModel, Part, Content, GenerationConfig,
    SafetySetting, HarmCategory, HarmBlockThreshold
)
import errno # Potrzebne do bezpiecznego tworzenia katalogu
import logging
import datetime # Potrzebne do pracy z datami/czasem
import pytz     # Potrzebne do stref czasowych
import locale   # Do formatowania dat po polsku

# --- Importy związane z Google Calendar ---
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
# ------------------------------------------

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
SERVICE_ACCOUNT_FILE = 'kalendarzklucz.json' # Nazwa pliku klucza konta usługi
CALENDAR_SCOPES = ['https://www.googleapis.com/auth/calendar.readonly', 'https://www.googleapis.com/auth/calendar.events']
CALENDAR_TIMEZONE = 'Europe/Warsaw'
# <<< POPRAWIONA DEFINICJA >>>
APPOINTMENT_DURATION_MINUTES = 60 # Długość wizyty w minutach
# ----------------------------
WORK_START_HOUR = 7
WORK_END_HOUR = 22
TARGET_CALENDAR_ID = 'f19e189826b9d6e36950da347ac84d5501ecbd6bed0d76c8641be61a67749c67@group.calendar.google.com'
MAX_SLOTS_TO_SHOW = 5
QUICK_REPLY_BOOK_PREFIX = "BOOK_SLOT_"
# --------------------------------------------------------------------

# --- Inicjalizacja Zmiennych Globalnych dla Kalendarza ---
_calendar_service = None
_tz = None
# ------------------------------------------------------

# --- Ustawienia Lokalizacji ---
try: locale.setlocale(locale.LC_TIME, 'pl_PL.UTF-8')
except locale.Error:
    try: locale.setlocale(locale.LC_TIME, 'Polish_Poland.1250') # Windows
    except locale.Error: print("Ostrzeżenie: Nie można ustawić polskiej lokalizacji.")
# ------------------------------------------------------------

# =====================================================================
# === FUNKCJE POMOCNICZE (Bot + Kalendarz) ============================
# =====================================================================

# --- Funkcja do bezpiecznego tworzenia katalogu ---
def ensure_dir(directory):
    try: os.makedirs(directory); print(f"Utworzono katalog: {directory}")
    except OSError as e:
        if e.errno != errno.EEXIST: print(f"!!! Błąd tworzenia katalogu {directory}: {e} !!!"); raise

# --- Funkcja do pobierania danych profilu użytkownika ---
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

# --- Funkcja do odczytu historii z pliku JSON ---
def load_history(user_psid):
    filepath = os.path.join(HISTORY_DIR, f"{user_psid}.json"); history = []
    if not os.path.exists(filepath): return history
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            history_data = json.load(f)
            if isinstance(history_data, list):
                for i, msg_data in enumerate(history_data):
                    if (isinstance(msg_data, dict) and 'role' in msg_data and msg_data['role'] in ('user', 'model') and
                            'parts' in msg_data and isinstance(msg_data['parts'], list) and msg_data['parts']):
                        text_parts = []; valid_parts = True
                        for part_data in msg_data['parts']:
                            if isinstance(part_data, dict) and 'text' in part_data and isinstance(part_data['text'], str): text_parts.append(Part.from_text(part_data['text']))
                            else: print(f"Ostrz. [{user_psid}]: Niepoprawna część w historii (idx {i}): {part_data}."); valid_parts = False; break
                        if valid_parts and text_parts: history.append(Content(role=msg_data['role'], parts=text_parts))
                    else: print(f"Ostrz. [{user_psid}]: Pominięto niepoprawną wiadomość w historii (idx {i}): {msg_data}")
                print(f"[{user_psid}] Wczytano historię: {len(history)} wiadomości."); return history
            else: print(f"!!! BŁĄD [{user_psid}]: Plik historii nie zawiera listy."); return []
    except FileNotFoundError: print(f"[{user_psid}] Plik historii nie istnieje."); return []
    except (json.JSONDecodeError, KeyError, IndexError, TypeError) as e: print(f"!!! BŁĄD [{user_psid}] parsowania historii: {e}. Plik: {filepath}."); return []
    except Exception as e: print(f"!!! BŁĄD [{user_psid}] wczytywania historii: {e} !!!"); return []

# --- Funkcja do zapisu historii do pliku JSON ---
def save_history(user_psid, history):
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
                else: print(f"Ostrz. [{user_psid}]: Pomijanie wiad. bez części (zapis, Rola: {msg.role})")
            else: print(f"Ostrz. [{user_psid}]: Pomijanie nieprawidłowego obiektu (zapis): {msg}")
        with open(temp_filepath, 'w', encoding='utf-8') as f: json.dump(history_data, f, ensure_ascii=False, indent=2)
        os.replace(temp_filepath, filepath)
        print(f"[{user_psid}] Zapisano historię ({len(history_data)} wiad.) do: {filepath}")
    except Exception as e:
        print(f"!!! BŁĄD [{user_psid}] zapisu historii: {e} !!! Plik: {filepath}")
        if os.path.exists(temp_filepath):
            try: os.remove(temp_filepath); print(f"    Usunięto {temp_filepath}.")
            except OSError as remove_e: print(f"    Nie można usunąć {temp_filepath}: {remove_e}")

# --- Funkcje Kalendarza (wcześniej w calendar_utils.py) ---

def _get_timezone():
    global _tz
    if _tz is None:
        try: _tz = pytz.timezone(CALENDAR_TIMEZONE)
        except pytz.exceptions.UnknownTimeZoneError: print(f"BŁĄD: Strefa '{CALENDAR_TIMEZONE}' nieznana. Używam UTC."); _tz = pytz.utc
    return _tz

def get_calendar_service():
    global _calendar_service
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

def get_free_slots(calendar_id, start_datetime, end_datetime): # Usunięto domyślny duration
    """Znajduje wolne sloty używając APPOINTMENT_DURATION_MINUTES z konfiguracji."""
    service = get_calendar_service(); tz = _get_timezone()
    if not service: print("Błąd: Usługa kalendarza niedostępna w get_free_slots."); return []
    if start_datetime.tzinfo is None: start_datetime = tz.localize(start_datetime)
    else: start_datetime = start_datetime.astimezone(tz)
    if end_datetime.tzinfo is None: end_datetime = tz.localize(end_datetime)
    else: end_datetime = end_datetime.astimezone(tz)
    print(f"Szukanie wolnych slotów ({APPOINTMENT_DURATION_MINUTES} min) w '{calendar_id}'"); print(f"Zakres: {start_datetime:%Y-%m-%d %H:%M %Z} do {end_datetime:%Y-%m-%d %H:%M %Z}")
    try:
        events_result = service.events().list(
            calendarId=calendar_id, timeMin=start_datetime.isoformat(), timeMax=end_datetime.isoformat(),
            singleEvents=True, orderBy='startTime').execute()
        events = events_result.get('items', [])
    except HttpError as error: print(f'Błąd API pobierania wydarzeń: {error}'); return []
    except Exception as e: print(f"Błąd pobierania wydarzeń: {e}"); return []

    free_slots_starts = []; current_day = start_datetime.date(); end_day = end_datetime.date()
    appointment_duration = datetime.timedelta(minutes=APPOINTMENT_DURATION_MINUTES) # Użyj wartości z konfiguracji

    while current_day <= end_day:
        day_start_limit = tz.localize(datetime.datetime.combine(current_day, datetime.time(WORK_START_HOUR, 0)))
        day_end_limit = tz.localize(datetime.datetime.combine(current_day, datetime.time(WORK_END_HOUR, 0)))
        check_start_time = max(start_datetime, day_start_limit); check_end_time = min(end_datetime, day_end_limit)
        if check_start_time >= check_end_time: current_day += datetime.timedelta(days=1); continue

        potential_slot_start = check_start_time; busy_times = []
        for event in events:
            start = parse_event_time(event['start'], tz)
            # <<< --- UPROSZCZONA LOGIKA ALL-DAY --- >>>
            if isinstance(start, datetime.date):
                if start == current_day: busy_times.append({'start': day_start_limit, 'end': day_end_limit})
            # <<< --- KONIEC LOGIKI ALL-DAY --- >>>
            elif isinstance(start, datetime.datetime):
                end = parse_event_time(event['end'], tz)
                if isinstance(end, datetime.datetime):
                    if end > day_start_limit and start < day_end_limit:
                        effective_start = max(start, day_start_limit); effective_end = min(end, day_end_limit)
                        if effective_start < effective_end: busy_times.append({'start': effective_start, 'end': effective_end})
                else: print(f"Ostrz.: Wydarzenie czasowe '{event.get('summary','?')}' ma nieprawidłowy koniec ({type(end)})")

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
                 if potential_slot_start >= check_start_time and potential_slot_start + appointment_duration <= check_end_time: free_slots_starts.append(potential_slot_start)
                 potential_slot_start += appointment_duration
            potential_slot_start = max(potential_slot_start, busy_end)
        while potential_slot_start + appointment_duration <= check_end_time:
             if potential_slot_start >= check_start_time: free_slots_starts.append(potential_slot_start)
             potential_slot_start += appointment_duration
        current_day += datetime.timedelta(days=1)
    final_slots = sorted(list(set(slot for slot in free_slots_starts if start_datetime <= slot < end_datetime)))
    print(f"Znaleziono {len(final_slots)} unikalnych wolnych slotów."); return final_slots

def book_appointment(calendar_id, start_time, end_time, summary="Rezerwacja wizyty", description="", user_name=""):
    service = get_calendar_service(); tz = _get_timezone()
    if not service: return False, "Błąd: Brak połączenia z usługą kalendarza."
    if start_time.tzinfo is None: start_time = tz.localize(start_time)
    else: start_time = start_time.astimezone(tz)
    if end_time.tzinfo is None: end_time = tz.localize(end_time)
    else: end_time = end_time.astimezone(tz)
    event_summary = summary;
    if user_name: event_summary += f" - {user_name}"
    event = {
        'summary': event_summary, 'description': description,
        'start': {'dateTime': start_time.isoformat(), 'timeZone': CALENDAR_TIMEZONE,},
        'end': {'dateTime': end_time.isoformat(), 'timeZone': CALENDAR_TIMEZONE,},
        'reminders': {'useDefault': False, 'overrides': [{'method': 'popup', 'minutes': 60},],},
    }
    try:
        print(f"Rezerwacja: {event_summary} od {start_time:%Y-%m-%d %H:%M} do {end_time:%Y-%m-%d %H:%M}")
        created_event = service.events().insert(calendarId=calendar_id, body=event).execute()
        print(f"Zarezerwowano. ID: {created_event.get('id')}")
        locale_day_name = "";
        try: locale_day_name = start_time.strftime("%A")
        except: day_names = ["Pn", "Wt", "Śr", "Cz", "Pt", "So", "Nd"]; locale_day_name = day_names[start_time.weekday()]
        confirm_message = f"Świetnie! Termin na {locale_day_name}, {start_time:%d.%m.%Y o %H:%M} został zarezerwowany."
        return True, confirm_message
    except HttpError as error:
        error_details = f"Kod: {error.resp.status}, Powód: {error.resp.reason}"
        try: error_json = json.loads(error.content.decode('utf-8')); error_details += f" - {error_json.get('error', {}).get('message', '')}"
        except: pass
        print(f"Błąd API rezerwacji: {error}, Szczegóły: {error_details}")
        if error.resp.status == 409: return False, "Niestety, ten termin został właśnie zajęty. Proszę, wybierz inny."
        elif error.resp.status == 403: return False, f"Brak uprawnień do zapisu w kalendarzu. Skontaktuj się z adminem."
        elif error.resp.status == 404: return False, f"Nie znaleziono kalendarza '{calendar_id}'."
        else: return False, f"Błąd API ({error.resp.status}) podczas rezerwacji."
    except Exception as e: import traceback; print(f"Nieoczekiwany błąd Python rezerwacji: {e}"); traceback.print_exc(); return False, "Niespodziewany błąd systemu rezerwacji."

# --------------------------------------------------------------------

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

def send_quick_replies(recipient_id, text, quick_replies_list):
    if not PAGE_ACCESS_TOKEN or len(PAGE_ACCESS_TOKEN) < 50: print(f"!!! [{recipient_id}] Brak TOKENA. Nie można wysłać QR."); return False
    if not quick_replies_list: print(f"[{recipient_id}] Brak QR. Wysyłanie tekstu."); return _send_single_message(recipient_id, text)
    print(f"--- Wysyłanie QR do {recipient_id} ({len(quick_replies_list)} przycisków) ---"); params = {"access_token": PAGE_ACCESS_TOKEN}; headers = {'Content-Type': 'application/json'}
    fb_quick_replies = []
    for qr in quick_replies_list[:13]:
         if isinstance(qr, dict) and "title" in qr and "payload" in qr: fb_quick_replies.append({"content_type": "text", "title": qr["title"][:20], "payload": qr["payload"][:1000]})
         else: print(f"Ostrz. [{recipient_id}]: Pominięto nieprawidłowy QR: {qr}")
    if not fb_quick_replies: print(f"!!! [{recipient_id}] Żaden QR niepoprawny. Wysyłanie tekstu."); return _send_single_message(recipient_id, text)
    if len(text) > MESSAGE_CHAR_LIMIT: print(f"Ostrz. [{recipient_id}]: Tekst QR ({len(text)}) > limit {MESSAGE_CHAR_LIMIT}. Skracanie."); text = text[:MESSAGE_CHAR_LIMIT-3] + "..."
    payload = {"recipient": {"id": recipient_id}, "messaging_type": "RESPONSE", "message": {"text": text, "quick_replies": fb_quick_replies}}
    try:
        r = requests.post(FACEBOOK_GRAPH_API_URL, params=params, headers=headers, json=payload, timeout=30); r.raise_for_status(); response_json = r.json()
        if response_json.get('error'): print(f"!!! BŁĄD FB API (QR): {response_json['error']} !!!"); return False
        return True
    except requests.exceptions.Timeout: print(f"!!! BŁĄD TIMEOUT wysyłania QR do {recipient_id} !!!"); return False
    except requests.exceptions.RequestException as e:
        print(f"!!! BŁĄD wysyłania QR do {recipient_id}: {e} !!!")
        if hasattr(e, 'response') and e.response is not None:
            try: print(f"Odpowiedź serwera FB (błąd QR): {e.response.json()}")
            except json.JSONDecodeError: print(f"Odpowiedź serwera FB (błąd QR, nie JSON): {e.response.text}")
        return False
    except Exception as e: print(f"!!! Niespodziewany BŁĄD wysyłania QR do {recipient_id}: {e} !!!"); return False

# --- INSTRUKCJA SYSTEMOWA (Przywrócona wersja bez ograniczenia emotek) ---
# TODO: Zmień [Nazwa Firmy], [Twój Numer], [Twój Email]
SYSTEM_INSTRUCTION_TEXT = """Jesteś profesjonalnym i uprzejmym asystentem obsługi klienta reprezentującym centrum 'Zakręcone Korepetycje', specjalizujące się w korepetycjach online z matematyki, języka angielskiego i języka polskiego dla uczniów od 4 klasy SP do matury (poziom podstawowy i rozszerzony).

Twoim głównym celem jest zachęcanie do skorzystania z naszych usług i **umówienia się na pierwszą lekcję próbną** (płatną zgodnie z cennikiem).

Przebieg rozmowy (elastyczny):
1.  Przywitaj się i zapytaj, w czym możesz pomóc w kwestii korepetycji.
2.  Ustal przedmiot.
3.  Ustal klasę.
4.  Dla szkoły średniej ustal poziom (podst./rozsz.).
5.  Podaj cenę za 60 min lekcji (cennik poniżej).
6.  Aktywnie zachęcaj do umówienia pierwszej lekcji.
7.  Informuj o formie online (MS Teams, bez instalacji) na życzenie.

Cennik (60 min): 4-8 SP: 60 zł; 1-3 LO/Tech (podst.): 65 zł; 1-3 LO/Tech (rozsz.): 70 zł; 4 LO/Tech (podst.): 70 zł; 4 LO/Tech (rozsz.): 75 zł.

**Obsługa Umawiania Terminów:**
*   Jeśli użytkownik pyta o terminy, rezerwację, kalendarz (np. "Chcę umówić wizytę", "Kiedy macie wolne?"), **NIE pytaj o datę**, odpowiedz **TYLKO** znacznikiem: `[ACTION: FIND_SLOTS]`
*   Nawet jeśli poda preferencje, odpowiedz tylko `[ACTION: FIND_SLOTS]`.
*   Używaj znacznika **TYLKO** w tym kontekście.

**Ważne zasady:**
*   **Kontynuacja po przerwie:** **ZAWSZE** analizuj historię i kontynuuj od miejsca przerwania. **NIE ZACZYNAJ OD NOWA**.
*   Preferuj formy bezosobowe lub "Państwo" zamiast "Pan/Pani".
*   Rozdzielaj wywiad na krótsze wiadomości.
*   Bądź perswazyjny, ale nie nachalny. Po odmowie zaproponuj zastanowienie się.
*   Jeśli nie znasz odpowiedzi, poinformuj i podaj kontakt: tel. [Twój Numer], email: [Twój Email].
*   Odpowiadaj zawsze po polsku.
"""
# ---------------------------------------------------------------------

# --- Funkcja interakcji z Gemini (z obsługą akcji) ---
def get_gemini_response_or_action(user_psid, current_user_message):
    if not gemini_model: print(f"!!! BŁĄD [{user_psid}]: Model Gemini niezaładowany!"); return "Przepraszam, błąd AI."
    history = load_history(user_psid); user_content = Content(role="user", parts=[Part.from_text(current_user_message)])
    full_conversation_for_save = history + [user_content]
    max_messages_to_send = MAX_HISTORY_TURNS * 2
    history_to_send = full_conversation_for_save[-max_messages_to_send:] if len(full_conversation_for_save) > max_messages_to_send else full_conversation_for_save
    if len(full_conversation_for_save) > max_messages_to_send: print(f"[{user_psid}] Historia przycięta DO WYSLANIA: {len(history_to_send)} wiad.")
    prompt_content_with_instruction = [
        Content(role="user", parts=[Part.from_text(SYSTEM_INSTRUCTION_TEXT)]),
        Content(role="model", parts=[Part.from_text("Rozumiem. Pomogę zgodnie z wytycznymi, inicjując sprawdzanie terminów znacznikiem [ACTION: FIND_SLOTS].")])
    ] + history_to_send
    print(f"\n--- [{user_psid}] Zawartość do Gemini ({MODEL_ID}) ---"); # Logowanie (skrócone dla przejrzystości)
    # for i, content in enumerate(prompt_content_with_instruction): role = content.role; raw_text = content.parts[0].text; text_fragment = raw_text[:80].replace('\n', '\\n'); text_to_log = text_fragment + "..." if len(raw_text) > 80 else text_fragment; print(f"  [{i}] R:{role}, T:'{text_to_log}'")
    print(f"--- Koniec zawartości {user_psid} ---\n")
    try:
        generation_config = GenerationConfig(temperature=0.7, top_p=0.95, top_k=40)
        safety_settings = {HarmCategory.HARM_CATEGORY_HARASSMENT: HarmBlockThreshold.BLOCK_MEDIUM_AND_ABOVE, HarmCategory.HARM_CATEGORY_HATE_SPEECH: HarmBlockThreshold.BLOCK_MEDIUM_AND_ABOVE, HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT: HarmBlockThreshold.BLOCK_MEDIUM_AND_ABOVE, HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT: HarmBlockThreshold.BLOCK_MEDIUM_AND_ABOVE,}
        response = gemini_model.generate_content(prompt_content_with_instruction, generation_config=generation_config, safety_settings=safety_settings, stream=False)
        generated_text = ""
        if response.candidates and response.candidates[0].content and response.candidates[0].content.parts:
            generated_text = response.candidates[0].content.parts[0].text.strip()
            # <<< Sprawdzenie ZAWARTOSCI znacznika >>>
            if "[ACTION: FIND_SLOTS]" in generated_text:
                print(f"[{user_psid}] Gemini -> Tekst zawiera akcję FIND_SLOTS: '{generated_text}'"); save_history(user_psid, full_conversation_for_save); return "[ACTION: FIND_SLOTS]"
            print(f"[{user_psid}] Wygenerowany tekst (dł: {len(generated_text)})"); text_preview = generated_text[:150].replace('\n', '\\n'); print(f"   Fragment: '{text_preview}...'")
            model_content = Content(role="model", parts=[Part.from_text(generated_text)]); final_history_to_save = full_conversation_for_save + [model_content]
            max_messages_to_save = MAX_HISTORY_TURNS * 2
            if len(final_history_to_save) > max_messages_to_save: final_history_to_save = final_history_to_save[-max_messages_to_save:]; print(f"[{user_psid}] Historia przycięta DO ZAPISU: {len(final_history_to_save)} wiad.")
            save_history(user_psid, final_history_to_save); return generated_text
        else: # Obsługa błędów/blokad
            finish_reason = "UNKNOWN"; safety_ratings = [];
            if response.candidates: finish_reason_obj = response.candidates[0].finish_reason; finish_reason = finish_reason_obj.name if hasattr(finish_reason_obj, 'name') else str(finish_reason_obj); safety_ratings = response.candidates[0].safety_ratings if response.candidates[0].safety_ratings else []
            print(f"!!! [{user_psid}] Odp. Gemini pusta/zablokowana. Powód: {finish_reason}, Oceny: {safety_ratings} !!!"); save_history(user_psid, full_conversation_for_save)
            if finish_reason == 'SAFETY': return "Przepraszam, treść narusza zasady."
            elif finish_reason == 'RECITATION': return "Moje źródła są ograniczone."
            else: return "Hmm, błąd generowania odpowiedzi."
    except Exception as e: # Obsługa wyjątków
        import traceback; print(f"!!! KRYTYCZNY BŁĄD Gemini ({MODEL_ID}) dla PSID {user_psid}: {e} !!!"); traceback.print_exc(); save_history(user_psid, full_conversation_for_save)
        error_str = str(e).lower();
        if "permission denied" in error_str: return "Błąd: Brak uprawnień AI."
        elif "model" in error_str and "not found" in error_str: return f"Błąd: Model AI ('{MODEL_ID}') niedostępny."
        elif "deadline exceeded" in error_str: return "Błąd: AI nie odpowiedziało."
        elif "quota" in error_str: return "Błąd: Limit zapytań AI."
        elif "content" in error_str and "invalid" in error_str: return "Błąd: Wewnętrzny błąd AI."
        return "Wystąpił błąd techniczny."

# --- Obsługa Weryfikacji Webhooka (GET) ---
@app.route('/webhook', methods=['GET'])
def webhook_verification():
    print("--- GET weryfikacja ---"); hub_mode = request.args.get('hub.mode'); hub_token = request.args.get('hub.verify_token'); hub_challenge = request.args.get('hub.challenge')
    print(f"Mode:{hub_mode},Token:{'OK' if hub_token==VERIFY_TOKEN else 'BŁĄD'},Challenge:{'Jest' if hub_challenge else 'Brak'}")
    if hub_mode == 'subscribe' and hub_token == VERIFY_TOKEN: print("Weryfikacja OK!"); return Response(hub_challenge, status=200)
    else: print("Weryfikacja FAILED."); return Response("Verification failed", status=403)

# --- Główna Obsługa Webhooka (POST) ---
@app.route('/webhook', methods=['POST'])
def webhook_handle():
    print("\n-----------------"); print(f"--- {datetime.datetime.now():%Y-%m-%d %H:%M:%S} POST ---")
    raw_data = request.data.decode('utf-8'); data = None
    try:
        data = json.loads(raw_data)
        if data and data.get("object") == "page":
            for entry in data.get("entry", []):
                page_id = entry.get("id"); timestamp = entry.get("time"); #print(f" Entry: {page_id}")
                for messaging_event in entry.get("messaging", []):
                    if "sender" not in messaging_event or "id" not in messaging_event["sender"]: continue
                    sender_id = messaging_event["sender"]["id"]; print(f"  -> PSID: {sender_id}")
                    if messaging_event.get("message"): # Wiadomość
                        message_data = messaging_event["message"]; message_id = message_data.get("mid"); print(f"    Msg (ID:{message_id})")
                        if message_data.get("is_echo"): print("      Echo."); continue
                        user_input_text = None; quick_reply_payload = None
                        if "quick_reply" in message_data:
                            quick_reply_payload = message_data["quick_reply"].get("payload"); user_input_text = message_data.get("text", quick_reply_payload); print(f"      QR: P='{quick_reply_payload}', T='{user_input_text}'")
                        elif "text" in message_data: user_input_text = message_data["text"]; print(f"      Txt: '{user_input_text}'")

                        # <<< Logika Rezerwacji >>>
                        if quick_reply_payload and quick_reply_payload.startswith(QUICK_REPLY_BOOK_PREFIX):
                            slot_iso_string = quick_reply_payload[len(QUICK_REPLY_BOOK_PREFIX):]
                            try:
                                tz = _get_timezone(); start_time = datetime.datetime.fromisoformat(slot_iso_string).astimezone(tz); end_time = start_time + datetime.timedelta(minutes=APPOINTMENT_DURATION_MINUTES)
                                print(f"      Wybrano slot: {start_time:%Y-%m-%d %H:%M %Z}")
                                user_profile = get_user_profile(sender_id); user_name = user_profile.get('first_name', '') if user_profile else ''
                                if ENABLE_TYPING_DELAY: time.sleep(MIN_TYPING_DELAY_SECONDS)
                                success, message_to_user = book_appointment(TARGET_CALENDAR_ID, start_time, end_time, summary=f"Rezerwacja FB", description=f"PSID:{sender_id}\nImię:{user_name}", user_name=user_name)
                                send_message(sender_id, message_to_user)
                            except ValueError as ve: print(f"!!! BŁĄD parsowania ISO QR: {slot_iso_string}. {ve} !!!"); send_message(sender_id, "Błąd terminu.")
                            except Exception as book_err: print(f"!!! KRYTYCZNY BŁĄD rezerwacji: {book_err} !!!"); import traceback; traceback.print_exc(); send_message(sender_id, "Błąd rezerwacji.")
                        # <<< Koniec Logiki Rezerwacji >>>
                        elif user_input_text: # Inny tekst lub QR -> Gemini
                             print(f"      -> Gemini..."); gemini_output = get_gemini_response_or_action(sender_id, user_input_text)
                             # <<< Logika Akcji FIND_SLOTS >>>
                             if gemini_output == "[ACTION: FIND_SLOTS]":
                                 print(f"      Akcja: FIND_SLOTS"); tz = _get_timezone(); now = datetime.datetime.now(tz); search_start = now
                                 search_end_date = (now + datetime.timedelta(days=7)).date(); search_end = tz.localize(datetime.datetime.combine(search_end_date, datetime.time(23, 59, 59)))
                                 if ENABLE_TYPING_DELAY: print(f"      Szukanie..."); time.sleep(MIN_TYPING_DELAY_SECONDS)
                                 free_slots = get_free_slots(TARGET_CALENDAR_ID, search_start, search_end) # Używa domyślnej długości
                                 if free_slots:
                                     replies = []; print(f"      Znaleziono {len(free_slots)} slotów.")
                                     for slot_start in free_slots[:MAX_SLOTS_TO_SHOW]:
                                         try: slot_text = slot_start.strftime("%A, %d.%m %H:%M")
                                         except: day_names = ["Pn", "Wt", "Śr", "Cz", "Pt", "So", "Nd"]; slot_text = f"{day_names[slot_start.weekday()]}, {slot_start.strftime('%d.%m %H:%M')}"
                                         replies.append({"title": slot_text, "payload": f"{QUICK_REPLY_BOOK_PREFIX}{slot_start.isoformat()}"})
                                     send_quick_replies(sender_id, "Oto kilka wolnych terminów:", replies)
                                 else: print(f"      Brak slotów."); send_message(sender_id, "Niestety, brak wolnych terminów w najbliższym tyg.")
                             # <<< Koniec Logiki FIND_SLOTS >>>
                             elif isinstance(gemini_output, str) and gemini_output: # Normalna odpowiedź
                                 print(f"      <- Gemini Odp.");
                                 if ENABLE_TYPING_DELAY:
                                     response_len = len(gemini_output); calculated_delay = response_len / TYPING_CHARS_PER_SECOND; final_delay = max(0, min(MAX_TYPING_DELAY_SECONDS, calculated_delay + MIN_TYPING_DELAY_SECONDS))
                                     if final_delay > 0: print(f"      Pisanie... {final_delay:.2f}s"); time.sleep(final_delay)
                                 send_message(sender_id, gemini_output)
                             else: print(f"!!! Błąd Gemini."); send_message(sender_id, gemini_output or "Błąd.")
                        elif "attachments" in message_data: attachment_type = message_data['attachments'][0].get('type', 'nieznany'); print(f"      Załącznik: {attachment_type}."); send_message(sender_id, "Nie obsługuję załączników.")
                        else: print(f"      Nieznany typ wiadomości: {message_data}"); send_message(sender_id, "Nie rozumiem tej wiadomości.")
                    elif messaging_event.get("postback"): # Postback
                         postback_data = messaging_event["postback"]; payload = postback_data.get("payload"); title = postback_data.get("title", payload); print(f"    Postback: T:'{title}', P:'{payload}'")
                         prompt_for_button = f"Kliknięto: '{title}' ({payload})."
                         response_text = get_gemini_response_or_action(sender_id, prompt_for_button)
                         if isinstance(response_text, str) and not response_text.startswith("[ACTION"):
                             if ENABLE_TYPING_DELAY:
                                 response_len = len(response_text); calculated_delay = response_len / TYPING_CHARS_PER_SECOND; final_delay = max(0, min(MAX_TYPING_DELAY_SECONDS, calculated_delay + MIN_TYPING_DELAY_SECONDS))
                                 if final_delay > 0: print(f"      Pisanie (postback)... {final_delay:.2f}s"); time.sleep(final_delay)
                             send_message(sender_id, response_text)
                         elif response_text == "[ACTION: FIND_SLOTS]": print("Ostrz.: FIND_SLOTS dla postback."); send_message(sender_id, "Otrzymałem akcję.")
                         else: send_message(sender_id, response_text or "Błąd.")
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
