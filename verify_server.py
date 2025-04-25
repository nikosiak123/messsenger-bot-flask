# -*- coding: utf-8 -*-

# verify_server.py (połączony kod z poprawkami języka, obsługi odrzucenia i kolejnych terminów)

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
import locale # Nadal potrzebne do ogólnych ustawień, ale nie dla dni tygodnia
import re

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

app = Flask(__name__)

# --- Konfiguracja Ogólna ---
VERIFY_TOKEN = os.environ.get("FB_VERIFY_TOKEN", "KOLAGEN")
PAGE_ACCESS_TOKEN = os.environ.get("FB_PAGE_ACCESS_TOKEN", "EACNAHFzEhkUBO7nbFAtYvfPWbEht1B3chQqWLx76Ljg2ekdbJYoOrnpjATqhS0EZC8S0q8a49hEZBaZByZCaj5gr1z62dAaMgcZA1BqFOruHfFo86EWTbI3S9KL59oxFWfZCfCjwbQra9lY5of1JVnj2c9uFJDhIpWlXxLLao9Cv8JKssgs3rEDxIJBRr26HgUewZDZD")
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
MAX_SLOTS_TO_SHOW = 3 # Pokazujemy 3 terminy naraz
QUICK_REPLY_BOOK_PREFIX = "BOOK_SLOT_" # Nadal nieużywany do wysyłania, ale może być do logiki

# --- Inicjalizacja Zmiennych Globalnych dla Kalendarza ---
_calendar_service = None
_tz = None

# --- Lista Polskich Dni Tygodnia ---
POLISH_WEEKDAYS = ["Poniedziałek", "Wtorek", "Środa", "Czwartek", "Piątek", "Sobota", "Niedziela"]

# --- Ustawienia Lokalizacji (nadal próba, ale nie polegamy na niej dla dni) ---
try: locale.setlocale(locale.LC_TIME, 'pl_PL.UTF-8')
except locale.Error:
    try: locale.setlocale(locale.LC_TIME, 'Polish_Poland.1250')
    except locale.Error: print("Ostrzeżenie: Nie można ustawić polskiej lokalizacji.")

# =====================================================================
# === FUNKCJE POMOCNICZE ==============================================
# =====================================================================

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

# ZMIANA: load_history teraz odczytuje również 'next_search_start'
def load_history(user_psid):
    filepath = os.path.join(HISTORY_DIR, f"{user_psid}.json"); history = []; context = {}
    if not os.path.exists(filepath): return history, context
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            history_data = json.load(f)
            if isinstance(history_data, list):
                processed_indices = set() # Aby uniknąć podwójnego przetwarzania kontekstu
                for i, msg_data in enumerate(history_data):
                    if i in processed_indices: continue # Już przetworzone jako część kontekstu
                    if (isinstance(msg_data, dict) and 'role' in msg_data and msg_data['role'] in ('user', 'model') and
                            'parts' in msg_data and isinstance(msg_data['parts'], list) and msg_data['parts']):
                        text_parts = []; valid_parts = True
                        for part_data in msg_data['parts']:
                            if isinstance(part_data, dict) and 'text' in part_data and isinstance(part_data['text'], str): text_parts.append(Part.from_text(part_data['text']))
                            else: print(f"Ostrz. [{user_psid}]: Niepoprawna część (idx {i})"); valid_parts = False; break
                        if valid_parts and text_parts: history.append(Content(role=msg_data['role'], parts=text_parts))
                    elif (isinstance(msg_data, dict) and 'role' in msg_data and msg_data['role'] == 'system' and
                          'type' in msg_data and msg_data['type'] == 'presented_slots' and 'slots' in msg_data):
                        # Sprawdź czy to najnowszy kontekst systemowy
                        is_latest_context = True
                        for j in range(i + 1, len(history_data)):
                             if isinstance(history_data[j], dict) and history_data[j].get('role') == 'system':
                                 is_latest_context = False
                                 break
                        if is_latest_context:
                            context['presented_slots'] = msg_data['slots']
                            context['next_search_start'] = msg_data.get('next_search_start') # Odczytaj nowy klucz
                            context['message_index'] = i # Zapisz indeks, aby wiedzieć, czy jest aktualny
                            print(f"[{user_psid}] Odczytano AKTUALNY kontekst: {len(context['presented_slots'])} slotów (idx {i}), next_start: {context['next_search_start']}")
                        else:
                             print(f"[{user_psid}] Pominięto stary kontekst systemowy na indeksie {i}")

                    else: print(f"Ostrz. [{user_psid}]: Pominięto niepoprawną wiadomość w historii (idx {i}): {msg_data}")
                print(f"[{user_psid}] Wczytano historię: {len(history)} wiadomości."); return history, context
            else: print(f"!!! BŁĄD [{user_psid}]: Plik historii nie zawiera listy."); return [], {}
    except FileNotFoundError: print(f"[{user_psid}] Plik historii nie istnieje."); return [], {}
    except (json.JSONDecodeError, KeyError, IndexError, TypeError) as e: print(f"!!! BŁĄD [{user_psid}] parsowania historii: {e}."); return [], {}
    except Exception as e: print(f"!!! BŁĄD [{user_psid}] wczytywania historii: {e} !!!"); return [], {}


# ZMIANA: save_history teraz zapisuje też 'next_search_start' w kontekście
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
             # Nie zapisujemy starych wpisów systemowych, tylko najnowszy na końcu
             # elif isinstance(msg, dict) and msg.get('role') == 'system': history_data.append(msg) # Usuwamy to
             else: print(f"Ostrz. [{user_psid}]: Pomijanie nieprawidłowego obiektu (zapis): {msg}")
        if context_to_save and isinstance(context_to_save, dict):
             history_data.append(context_to_save) # Dodaj nowy kontekst na końcu
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

# ZMIANA: get_free_slots używa teraz duration_minutes z konfiguracji
def get_free_slots(calendar_id, start_datetime, end_datetime):
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
                 if potential_slot_start >= check_start_time and potential_slot_start + appointment_duration <= check_end_time: free_slots_starts.append(potential_slot_start)
                 potential_slot_start += appointment_duration
            potential_slot_start = max(potential_slot_start, busy_end)
        while potential_slot_start + appointment_duration <= check_end_time:
             if potential_slot_start >= check_start_time: free_slots_starts.append(potential_slot_start)
             potential_slot_start += appointment_duration
        current_day += datetime.timedelta(days=1)
    final_slots = sorted(list(set(slot for slot in free_slots_starts if start_datetime <= slot < end_datetime)))
    print(f"Znaleziono {len(final_slots)} unikalnych wolnych slotów."); return final_slots

# ZMIANA: book_appointment używa formatowania godziny bez zera
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
        day_index = start_time.weekday()
        locale_day_name = POLISH_WEEKDAYS[day_index] # Użyj polskiej nazwy z listy
        hour_str = str(start_time.hour) # Godzina bez zera
        confirm_message = f"Świetnie! Termin na {locale_day_name}, {start_time.strftime(f'%d.%m.%Y o {hour_str}:%M')} został zarezerwowany."
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

# Funkcja send_quick_replies nie jest używana w tej logice

# --- INSTRUKCJA SYSTEMOWA ---
SYSTEM_INSTRUCTION_TEXT = """Jesteś profesjonalnym i uprzejmym asystentem obsługi klienta reprezentującym centrum 'Zakręcone Korepetycje', specjalizujące się w korepetycjach online z matematyki, języka angielskiego i języka polskiego dla uczniów od 4 klasy SP do matury (poziom podstawowy i rozszerzony).

Twoim głównym celem jest zachęcanie do skorzystania z naszych usług i **umówienia się na pierwszą lekcję próbną** (płatną zgodnie z cennikiem).

Przebieg rozmowy (elastyczny):
1.  Przywitaj się i zapytaj, w czym możesz pomóc w kwestii korepetycji.
2.  Ustal przedmiot.
3.  Ustal klasę.
4.  Dla szkoły średniej ustal poziom (podst./rozsz.).
5.  Podaj cenę za 60 min lekcji (cennik poniżej).
6.  **Po podaniu ceny, jeśli rozmowa naturalnie zmierza ku umówieniu terminu lub użytkownik pyta o terminy, użyj akcji FIND_SLOTS.**
7.  Informuj o formie online (MS Teams, bez instalacji) na życzenie.

Cennik (60 min): 4-8 SP: 60 zł; 1-3 LO/Tech (podst.): 65 zł; 1-3 LO/Tech (rozsz.): 70 zł; 4 LO/Tech (podst.): 70 zł; 4 LO/Tech (rozsz.): 75 zł.

**Obsługa Umawiania Terminów:**
*   Jeśli użytkownik pyta o terminy, rezerwację, kalendarz, dostępność LUB rozmowa logicznie prowadzi do pytania o termin, **NIE pytaj o datę**, odpowiedz **TYLKO** znacznikiem: `[ACTION: FIND_SLOTS]`
*   Nawet jeśli poda preferencje, odpowiedz tylko `[ACTION: FIND_SLOTS]`.
*   Używaj znacznika **TYLKO** w tym kontekście.
*   Po tym jak system zaproponuje terminy (np. 1, 2, 3), a użytkownik odpowie wybierając numer, Twoim zadaniem będzie tylko potwierdzenie, czy rezerwacja się udała lub obsługa dalszych pytań.

**Ważne zasady:**
*   **Kontynuacja po przerwie:** **ZAWSZE** analizuj historię i kontynuuj od miejsca przerwania. **NIE ZACZYNAJ OD NOWA**.
*   Preferuj formy bezosobowe lub "Państwo".
*   Rozdzielaj wywiad na krótsze wiadomości.
*   Bądź perswazyjny, ale nie nachalny. Po odmowie zaproponuj zastanowienie się.
*   Jeśli nie znasz odpowiedzi, poinformuj i podaj kontakt: tel. [Twój Numer], email: [Twój Email].
*   Odpowiadaj zawsze po polsku.
"""
# ---------------------------------------------------------------------

# --- Funkcja interakcji z Gemini ---
def get_gemini_response_or_action(user_psid, current_user_message, history):
    if not gemini_model: print(f"!!! BŁĄD [{user_psid}]: Model Gemini niezaładowany!"); return "Przepraszam, błąd AI."
    user_content = Content(role="user", parts=[Part.from_text(current_user_message)])
    max_messages_to_send = MAX_HISTORY_TURNS * 2
    history_to_send = history[-max_messages_to_send:] if len(history) > max_messages_to_send else history
    if len(history) > max_messages_to_send: print(f"[{user_psid}] Historia przycięta DO WYSLANIA: {len(history_to_send)} wiad.")
    prompt_content_with_instruction = [
        Content(role="user", parts=[Part.from_text(SYSTEM_INSTRUCTION_TEXT)]),
        Content(role="model", parts=[Part.from_text("Rozumiem. Pomogę zgodnie z wytycznymi, inicjując sprawdzanie terminów znacznikiem [ACTION: FIND_SLOTS].")])
    ] + history_to_send + [user_content]
    print(f"\n--- [{user_psid}] Zawartość do Gemini ({MODEL_ID}) ---");
    # for i, content in enumerate(prompt_content_with_instruction): role = content.role; raw_text = content.parts[0].text; text_fragment = raw_text[:80].replace('\n', '\\n'); text_to_log = text_fragment + "..." if len(raw_text) > 80 else text_fragment; print(f"  [{i}] R:{role}, T:'{text_to_log}'")
    print(f"--- Koniec zawartości {user_psid} ---\n")
    try:
        generation_config = GenerationConfig(temperature=0.7, top_p=0.95, top_k=40)
        safety_settings = {HarmCategory.HARM_CATEGORY_HARASSMENT: HarmBlockThreshold.BLOCK_MEDIUM_AND_ABOVE, HarmCategory.HARM_CATEGORY_HATE_SPEECH: HarmBlockThreshold.BLOCK_MEDIUM_AND_ABOVE, HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT: HarmBlockThreshold.BLOCK_MEDIUM_AND_ABOVE, HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT: HarmBlockThreshold.BLOCK_MEDIUM_AND_ABOVE,}
        response = gemini_model.generate_content(prompt_content_with_instruction, generation_config=generation_config, safety_settings=safety_settings, stream=False)
        generated_text = ""
        if response.candidates and response.candidates[0].content and response.candidates[0].content.parts:
            generated_text = response.candidates[0].content.parts[0].text.strip()
            if "[ACTION: FIND_SLOTS]" in generated_text:
                print(f"[{user_psid}] Gemini -> Tekst zawiera akcję FIND_SLOTS: '{generated_text}'"); return "[ACTION: FIND_SLOTS]"
            print(f"[{user_psid}] Wygenerowany tekst (dł: {len(generated_text)})"); text_preview = generated_text[:150].replace('\n', '\\n'); print(f"   Fragment: '{text_preview}...'")
            return generated_text
        else:
            finish_reason = "UNKNOWN"; safety_ratings = [];
            if response.candidates: finish_reason_obj = response.candidates[0].finish_reason; finish_reason = finish_reason_obj.name if hasattr(finish_reason_obj, 'name') else str(finish_reason_obj); safety_ratings = response.candidates[0].safety_ratings if response.candidates[0].safety_ratings else []
            print(f"!!! [{user_psid}] Odp. Gemini pusta/zablokowana. Powód: {finish_reason}, Oceny: {safety_ratings} !!!")
            if finish_reason == 'SAFETY': return "Przepraszam, treść narusza zasady."
            elif finish_reason == 'RECITATION': return "Moje źródła są ograniczone."
            else: return "Hmm, błąd generowania odpowiedzi."
    except Exception as e:
        import traceback; print(f"!!! KRYTYCZNY BŁĄD Gemini ({MODEL_ID}) dla PSID {user_psid}: {e} !!!"); traceback.print_exc()
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
                    presented_slots_context = context.get('presented_slots'); context_message_index = context.get('message_index', -1)
                    last_search_start_iso = context.get('last_search_start') # Odczytaj czas ostatniego wyszukiwania

                    # Sprawdź czy kontekst jest aktualny (czy ostatnia wiadomość w historii to ta z kontekstem)
                    is_context_current = presented_slots_context and context_message_index == len(history) -1

                    if is_context_current:
                         print(f"    Aktywny kontekst 'presented_slots' (indeks {context_message_index}).")
                    elif presented_slots_context:
                         print(f"    Kontekst 'presented_slots' stary (indeks {context_message_index} vs {len(history)-1}). Reset.")
                         presented_slots_context = None # Zignoruj stary kontekst

                    if messaging_event.get("message"):
                        message_data = messaging_event["message"]; message_id = message_data.get("mid"); print(f"    Msg (ID:{message_id})")
                        if message_data.get("is_echo"): print("      Echo."); continue
                        user_input_text = None;
                        if "text" in message_data: user_input_text = message_data["text"]; print(f"      Txt: '{user_input_text}'")

                        # --- Logika Wyboru/Odrzucenia Terminu ---
                        if is_context_current and user_input_text: # Tylko jeśli kontekst jest aktualny
                            print(f"      Oczekiwano na wybór/odrzucenie. Analiza: '{user_input_text}'")
                            chosen_index = -1; match = re.search(r'\b([1-3])\b', user_input_text)
                            user_wants_more = False

                            if match: chosen_index = int(match.group(1)) - 1
                            else: # Sprawdź słowa kluczowe
                                lower_text = user_input_text.lower()
                                if "pierwszy" in lower_text or "jedynk" in lower_text: chosen_index = 0
                                elif "drugi" in lower_text or "dwójk" in lower_text: chosen_index = 1
                                elif "trzeci" in lower_text or "trójk" in lower_text: chosen_index = 2
                                elif any(keyword in lower_text for keyword in ["nie pasuje", "inny", "inne", "żaden", "dalej", "następne", "więcej"]):
                                    user_wants_more = True
                                    print("      Użytkownik chce inne terminy.")

                            if 0 <= chosen_index < len(presented_slots_context): # Użytkownik wybrał numer
                                selected_iso_slot = presented_slots_context[chosen_index]; print(f"      Wybrano nr {chosen_index + 1} ({selected_iso_slot})")
                                try:
                                    tz = _get_timezone(); start_time = datetime.datetime.fromisoformat(selected_iso_slot).astimezone(tz); end_time = start_time + datetime.timedelta(minutes=APPOINTMENT_DURATION_MINUTES)
                                    user_profile = get_user_profile(sender_id); user_name = user_profile.get('first_name', '') if user_profile else ''
                                    if ENABLE_TYPING_DELAY: time.sleep(MIN_TYPING_DELAY_SECONDS)
                                    success, message_to_user = book_appointment(TARGET_CALENDAR_ID, start_time, end_time, summary=f"Rezerwacja FB", description=f"PSID:{sender_id}\nImię:{user_name}", user_name=user_name)
                                    send_message(sender_id, message_to_user)
                                    user_content = Content(role="user", parts=[Part.from_text(user_input_text)]); model_content = Content(role="model", parts=[Part.from_text(message_to_user)])
                                    save_history(sender_id, history + [user_content, model_content]) # Zapisz bez kontekstu
                                except ValueError as ve: print(f"!!! BŁĄD parsowania ISO z kontekstu: {selected_iso_slot}. {ve} !!!"); send_message(sender_id, "Błąd terminu.")
                                except Exception as book_err: print(f"!!! KRYTYCZNY BŁĄD rezerwacji z kontekstu: {book_err} !!!"); import traceback; traceback.print_exc(); send_message(sender_id, "Błąd rezerwacji.")
                            elif user_wants_more: # Użytkownik chce inne terminy
                                print("      Logika pokazywania kolejnych terminów...")
                                next_search_start_dt = None
                                if presented_slots_context: # Powinno być, bo is_context_current = True
                                    try:
                                        # Rozpocznij szukanie od końca ostatniego zaproponowanego slotu + 1 minuta
                                        last_proposed_iso = presented_slots_context[-1]
                                        tz = _get_timezone()
                                        last_proposed_start = datetime.datetime.fromisoformat(last_proposed_iso).astimezone(tz)
                                        next_search_start_dt = last_proposed_start + datetime.timedelta(minutes=APPOINTMENT_DURATION_MINUTES)
                                    except Exception as e:
                                        print(f"!!! Błąd ustalania next_search_start_dt: {e}")
                                        # Fallback: zacznij od teraz + 1 godzina
                                        tz = _get_timezone(); now = datetime.datetime.now(tz)
                                        next_search_start_dt = now + datetime.timedelta(hours=1)

                                if next_search_start_dt:
                                    search_end_date = (next_search_start_dt + datetime.timedelta(days=7)).date()
                                    search_end = tz.localize(datetime.datetime.combine(search_end_date, datetime.time(23, 59, 59)))
                                    if ENABLE_TYPING_DELAY: print(f"      Szukanie kolejnych..."); time.sleep(MIN_TYPING_DELAY_SECONDS)
                                    free_slots = get_free_slots(TARGET_CALENDAR_ID, next_search_start_dt, search_end)

                                    if free_slots:
                                        proposed_slots = []; proposed_dates = set()
                                        for slot in free_slots:
                                            slot_date = slot.date()
                                            if slot_date not in proposed_dates: proposed_slots.append(slot); proposed_dates.add(slot_date)
                                            if len(proposed_slots) == 3: break
                                        if len(proposed_slots) < 3:
                                            remaining_needed = 3 - len(proposed_slots)
                                            for slot in free_slots:
                                                if slot not in proposed_slots: proposed_slots.append(slot); remaining_needed -= 1
                                                if remaining_needed == 0: break
                                            proposed_slots.sort()
                                        print(f"      Znaleziono {len(free_slots)}. Proponowanie kolejnych {len(proposed_slots)}.")
                                        message_parts = ["Rozumiem, oto kolejne propozycje terminów:"]
                                        proposed_iso_slots = []
                                        for i, slot_start in enumerate(proposed_slots):
                                            hour_str = str(slot_start.hour); day_index = slot_start.weekday(); day_name = POLISH_WEEKDAYS[day_index]
                                            slot_text = f"{day_name}, {slot_start.strftime(f'%d.%m.%Y o {hour_str}:%M')}"
                                            message_parts.append(f"{i+1}. {slot_text}")
                                            proposed_iso_slots.append(slot_start.isoformat())
                                        message_parts.append("\nProszę wybrać numer lub poprosić o jeszcze inne terminy.")
                                        final_message = "\n".join(message_parts); send_message(sender_id, final_message)
                                        user_content = Content(role="user", parts=[Part.from_text(user_input_text)])
                                        model_content = Content(role="model", parts=[Part.from_text(final_message)])
                                        context_to_save = {'role': 'system', 'type': 'presented_slots', 'slots': proposed_iso_slots, 'last_search_start': next_search_start_dt.isoformat()}
                                        save_history(sender_id, history + [user_content, model_content], context_to_save=context_to_save)
                                    else:
                                        print(f"      Brak kolejnych slotów."); send_message(sender_id, "Niestety, nie znalazłem więcej wolnych terminów w najbliższym czasie.")
                                        user_content = Content(role="user", parts=[Part.from_text(user_input_text)]); model_content = Content(role="model", parts=[Part.from_text("Brak kolejnych terminów.")])
                                        save_history(sender_id, history + [user_content, model_content]) # Zapisz bez kontekstu
                                else:
                                    send_message(sender_id, "Wystąpił problem przy szukaniu kolejnych terminów.")
                                    user_content = Content(role="user", parts=[Part.from_text(user_input_text)])
                                    save_history(sender_id, history + [user_content])

                            else: # Użytkownik nie wybrał numeru ani nie odrzucił
                                print(f"      Nie sparsowano wyboru/odrzucenia z: '{user_input_text}'. Przekazanie do Gemini.")
                                # Traktuj jako normalną wiadomość, ale wyczyść kontekst przed zapisem Gemini
                                gemini_output = get_gemini_response_or_action(sender_id, user_input_text, [h for h in history if not (isinstance(h, dict) and h.get('role') == 'system')])
                                if isinstance(gemini_output, str) and gemini_output and not gemini_output.startswith("[ACTION"):
                                    if ENABLE_TYPING_DELAY: time.sleep(MIN_TYPING_DELAY_SECONDS)
                                    send_message(sender_id, gemini_output)
                                    user_content = Content(role="user", parts=[Part.from_text(user_input_text)]); model_content = Content(role="model", parts=[Part.from_text(gemini_output)])
                                    save_history(sender_id, history + [user_content, model_content]) # Zapisz bez kontekstu
                                elif gemini_output == "[ACTION: FIND_SLOTS]": # Gemini znów chce szukać? Może być pętla, lepiej wysłać info
                                    send_message(sender_id, "Proszę wybrać jeden z podanych terminów (1, 2 lub 3) lub napisać, że żaden nie pasuje.")
                                    user_content = Content(role="user", parts=[Part.from_text(user_input_text)]); model_content = Content(role="model", parts=[Part.from_text("Proszę wybrać...")])
                                    save_history(sender_id, history + [user_content, model_content], context_to_save={'role':'system', 'type':'presented_slots', 'slots': presented_slots_context, 'last_search_start': last_search_start_iso}) # Utrzymaj kontekst
                                else: # Błąd Gemini
                                    send_message(sender_id, gemini_output or "Błąd.")
                                    user_content = Content(role="user", parts=[Part.from_text(user_input_text)])
                                    save_history(sender_id, history + [user_content])

                        # --- Jeśli NIE oczekiwano na wybór terminu -> Normalne przetwarzanie ---
                        elif user_input_text:
                             print(f"      -> Gemini...");
                             gemini_output = get_gemini_response_or_action(sender_id, user_input_text, [h for h in history if not (isinstance(h, dict) and h.get('role') == 'system')])

                             # <<< Logika Akcji FIND_SLOTS (Pierwsze wywołanie) >>>
                             if gemini_output == "[ACTION: FIND_SLOTS]":
                                 print(f"      Akcja: FIND_SLOTS"); tz = _get_timezone(); now = datetime.datetime.now(tz); search_start = now
                                 search_end_date = (now + datetime.timedelta(days=7)).date(); search_end = tz.localize(datetime.datetime.combine(search_end_date, datetime.time(23, 59, 59)))
                                 if ENABLE_TYPING_DELAY: print(f"      Szukanie..."); time.sleep(MIN_TYPING_DELAY_SECONDS)
                                 free_slots = get_free_slots(TARGET_CALENDAR_ID, search_start, search_end)
                                 if free_slots:
                                     proposed_slots = []; proposed_dates = set()
                                     for slot in free_slots:
                                         slot_date = slot.date()
                                         if slot_date not in proposed_dates: proposed_slots.append(slot); proposed_dates.add(slot_date)
                                         if len(proposed_slots) == 3: break
                                     if len(proposed_slots) < 3:
                                         remaining_needed = 3 - len(proposed_slots)
                                         for slot in free_slots:
                                             if slot not in proposed_slots: proposed_slots.append(slot); remaining_needed -= 1
                                             if remaining_needed == 0: break
                                         proposed_slots.sort()
                                     print(f"      Znaleziono {len(free_slots)}. Proponowanie {len(proposed_slots)}.")
                                     message_parts = ["Oto propozycje najbliższych wolnych terminów:"]
                                     proposed_iso_slots = []
                                     for i, slot_start in enumerate(proposed_slots):
                                         hour_str = str(slot_start.hour); day_index = slot_start.weekday(); day_name = POLISH_WEEKDAYS[day_index]
                                         slot_text = f"{day_name}, {slot_start.strftime(f'%d.%m.%Y o {hour_str}:%M')}" # Poprawiony format
                                         message_parts.append(f"{i+1}. {slot_text}")
                                         proposed_iso_slots.append(slot_start.isoformat())
                                     message_parts.append("\nProszę wybrać numer terminu (np. odpisując '1').")
                                     final_message = "\n".join(message_parts); send_message(sender_id, final_message)
                                     user_content = Content(role="user", parts=[Part.from_text(user_input_text)])
                                     model_content = Content(role="model", parts=[Part.from_text(final_message)])
                                     context_to_save = {'role': 'system', 'type': 'presented_slots', 'slots': proposed_iso_slots, 'last_search_start': search_start.isoformat()} # Zapisz też start wyszukiwania
                                     save_history(sender_id, history + [user_content, model_content], context_to_save=context_to_save)
                                 else:
                                     print(f"      Brak slotów."); send_message(sender_id, "Niestety, brak wolnych terminów w najbliższym tyg.")
                                     user_content = Content(role="user", parts=[Part.from_text(user_input_text)]); model_content = Content(role="model", parts=[Part.from_text("Niestety, brak wolnych terminów...")])
                                     save_history(sender_id, history + [user_content, model_content])
                             # <<< Koniec Logiki FIND_SLOTS >>>
                             elif isinstance(gemini_output, str) and gemini_output: # Normalna odpowiedź
                                 print(f"      <- Gemini Odp.");
                                 if ENABLE_TYPING_DELAY:
                                     response_len = len(gemini_output); calculated_delay = response_len / TYPING_CHARS_PER_SECOND; final_delay = max(0, min(MAX_TYPING_DELAY_SECONDS, calculated_delay + MIN_TYPING_DELAY_SECONDS))
                                     if final_delay > 0: print(f"      Pisanie... {final_delay:.2f}s"); time.sleep(final_delay)
                                 user_content = Content(role="user", parts=[Part.from_text(user_input_text)]); model_content = Content(role="model", parts=[Part.from_text(gemini_output)])
                                 save_history(sender_id, history + [user_content, model_content]) # Zapisz normalnie
                                 send_message(sender_id, gemini_output)
                             else: # Błąd z Gemini
                                  print(f"!!! [{sender_id}] Błąd z get_gemini_response_or_action."); user_content = Content(role="user", parts=[Part.from_text(user_input_text)])
                                  save_history(sender_id, history + [user_content]); send_message(sender_id, gemini_output or "Błąd.")
                        elif "attachments" in message_data: # Załączniki
                             attachment_type = message_data['attachments'][0].get('type', 'nieznany'); print(f"      Załącznik: {attachment_type}.");
                             user_content = Content(role="user", parts=[Part.from_text(f"[Załącznik: {attachment_type}]")]); model_content = Content(role="model", parts=[Part.from_text("Nie obsługuję załączników.")])
                             save_history(sender_id, history + [user_content, model_content]); send_message(sender_id, "Przepraszam, nie obsługuję załączników.")
                        else: # Inne
                            print(f"      Nieznany typ wiadomości: {message_data}")
                            user_content = Content(role="user", parts=[Part.from_text("[Nieznany typ wiadomości]")]); model_content = Content(role="model", parts=[Part.from_text("Nie rozumiem.")])
                            save_history(sender_id, history + [user_content, model_content]); send_message(sender_id, "Nie rozumiem.")

                    # ... (reszta obsługi postback, read, delivery bez zmian) ...
                    elif messaging_event.get("postback"):
                        postback_data = messaging_event["postback"]; payload = postback_data.get("payload"); title = postback_data.get("title", payload); print(f"    Postback: T:'{title}', P:'{payload}'")
                        prompt_for_button = f"Kliknięto: '{title}' ({payload})."
                        response_text = get_gemini_response_or_action(sender_id, prompt_for_button, [h for h in history if not (isinstance(h, dict) and h.get('role') == 'system')])
                        if isinstance(response_text, str) and not response_text.startswith("[ACTION"):
                            if ENABLE_TYPING_DELAY:
                                response_len = len(response_text); calculated_delay = response_len / TYPING_CHARS_PER_SECOND; final_delay = max(0, min(MAX_TYPING_DELAY_SECONDS, calculated_delay + MIN_TYPING_DELAY_SECONDS))
                                if final_delay > 0: print(f"      Pisanie (postback)... {final_delay:.2f}s"); time.sleep(final_delay)
                            user_content = Content(role="user", parts=[Part.from_text(prompt_for_button)]); model_content = Content(role="model", parts=[Part.from_text(response_text)])
                            save_history(sender_id, history + [user_content, model_content])
                            send_message(sender_id, response_text)
                        elif response_text == "[ACTION: FIND_SLOTS]": print("Ostrz.: FIND_SLOTS dla postback."); user_content = Content(role="user", parts=[Part.from_text(prompt_for_button)]); model_content = Content(role="model", parts=[Part.from_text("Akcja nieobsługiwana.")]) ; save_history(sender_id, history + [user_content, model_content]) ; send_message(sender_id, "Akcja nieobsługiwana.")
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
