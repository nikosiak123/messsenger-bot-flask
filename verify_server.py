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
PAGE_ACCESS_TOKEN = os.environ.get("FB_PAGE_ACCESS_TOKEN", "EACNAHFzEhkUBO4ypcoyQfWIgNc0YLZA1aCr9n3BzpvSJLoBTJnv5rWZBmc7HlqF6uUW1uAp6aDZB8ZAb0RRT45lVIfGnciQX6wBKrZColGARfVLXP5Ic6Ptrj5AUvom4Rt12hyBxcjIJGes76fvdvBhiBZCJ0ZCVfkQMZBZCBatJshSZA8hFuRyKd58b50wkhVCMZCuwZDZD")  # WAŻNE: Podaj swój prawdziwy token!
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
PREFERRED_WEEKDAY_START_HOUR = 16  # Godzina "popołudniowa"
PREFERRED_WEEKEND_START_HOUR = 10
MAX_SEARCH_DAYS = 14  # Jak daleko w przyszłość szukać kolejnych terminów
EARLY_HOUR_LIMIT = 12  # Górna granica dla preferencji "earlier"

# --- Znaczniki dla komunikacji z AI ---
INTENT_SCHEDULE_MARKER = "[INTENT_SCHEDULE]"
SLOT_ISO_MARKER_PREFIX = "[SLOT_ISO:"
SLOT_ISO_MARKER_SUFFIX = "]"

# --- Ustawienia Modelu Gemini ---
# Konfiguracja dla propozycji terminu
GENERATION_CONFIG_PROPOSAL = GenerationConfig(
    temperature=0.1,
    top_p=0.95,
    top_k=40,
    max_output_tokens=512,
)

# Konfiguracja dla interpretacji feedbacku
GENERATION_CONFIG_FEEDBACK = GenerationConfig(
    temperature=0.0,  # Niska temperatura dla deterministycznych odpowiedzi
    top_p=0.95,
    top_k=40,
    max_output_tokens=64,
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
        print("Ostrzeżenie: Nie można ustawić polskiej lokalizacji dla formatowania dat.")

# =====================================================================
# === FUNKCJE POMOCNICZE ==============================================
# =====================================================================
# (Funkcje ensure_dir, get_user_profile, load_history, save_history, _get_timezone,
#  get_calendar_service, parse_event_time, get_free_slots, book_appointment,
#  format_slot_for_user - BEZ ZMIAN od ostatniej wersji)
# (Funkcja find_next_reasonable_slot nie jest już potrzebna w tej logice)

def ensure_dir(directory):
    try:
        os.makedirs(directory)
        print(f"Utworzono katalog: {directory}")
    except OSError as e:
        if e.errno != errno.EEXIST:
            print(f"!!! Błąd tworzenia katalogu {directory}: {e} !!!")
            raise

def get_user_profile(psid):
    if not PAGE_ACCESS_TOKEN or len(PAGE_ACCESS_TOKEN) < 50:
        print(f"!!! [{psid}] Brak/nieprawidłowy TOKEN. Profil niepobrany.")
        return None
    USER_PROFILE_API_URL_TEMPLATE = "https://graph.facebook.com/v19.0/{psid}?fields=first_name,last_name,profile_pic&access_token={token}"
    url = USER_PROFILE_API_URL_TEMPLATE.format(psid=psid, token=PAGE_ACCESS_TOKEN)
    print(f"--- [{psid}] Pobieranie profilu...")
    profile_data = {}
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        data = r.json()
        if 'error' in data:
            print(f"!!! BŁĄD FB API (profil) {psid}: {data['error']} !!!")
            return None
        profile_data['first_name'] = data.get('first_name')
        profile_data['last_name'] = data.get('last_name')
        profile_data['profile_pic'] = data.get('profile_pic')
        profile_data['id'] = data.get('id')
        return profile_data
    except requests.exceptions.Timeout:
        print(f"!!! BŁĄD TIMEOUT profilu {psid} !!!")
        return None
    except requests.exceptions.HTTPError as http_err:
         print(f"!!! BŁĄD HTTP {http_err.response.status_code} profilu {psid}: {http_err} !!!")
         if http_err.response is not None:
            try:
                print(f"Odpowiedź FB (błąd HTTP): {http_err.response.json()}")
            except json.JSONDecodeError:
                print(f"Odpowiedź FB (błąd HTTP, nie JSON): {http_err.response.text}")
         return None
    except requests.exceptions.RequestException as req_err:
        print(f"!!! BŁĄD RequestException profilu {psid}: {req_err} !!!")
        return None
    except Exception as e:
        import traceback
        print(f"!!! Niespodziewany BŁĄD profilu {psid}: {e} !!!")
        traceback.print_exc()
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
            if isinstance(history_data, list):
                processed_indices = set()
                for i, msg_data in enumerate(history_data):
                    if i in processed_indices:
                        continue
                    if (isinstance(msg_data, dict) and 'role' in msg_data and msg_data['role'] in ('user', 'model') and
                            'parts' in msg_data and isinstance(msg_data['parts'], list) and msg_data['parts']):
                        text_parts = []
                        valid_parts = True
                        for part_data in msg_data['parts']:
                            if isinstance(part_data, dict) and 'text' in part_data and isinstance(part_data['text'], str):
                                text_parts.append(Part.from_text(part_data['text']))
                            else:
                                print(f"Ostrz. [{user_psid}]: Niepoprawna część (idx {i})")
                                valid_parts = False
                                break
                        if valid_parts and text_parts:
                            history.append(Content(role=msg_data['role'], parts=text_parts))
                    elif (isinstance(msg_data, dict) and 'role' in msg_data and msg_data['role'] == 'system' and
                          'type' in msg_data and msg_data['type'] == 'last_proposal' and 'slot_iso' in msg_data):
                        # Znajdź ostatni kontekst systemowy
                        is_latest_context = all(not (isinstance(history_data[j], dict) and history_data[j].get('role') == 'system') for j in range(i + 1, len(history_data)))
                        if is_latest_context:
                            context['last_proposed_slot_iso'] = msg_data['slot_iso']
                            context['message_index_in_file'] = i # Zapisz indeks w pliku
                            print(f"[{user_psid}] Odczytano AKTUALNY kontekst: last_proposed_slot_iso (idx {i})")
                        else:
                            print(f"[{user_psid}] Pominięto stary kontekst systemowy na indeksie {i}")
                    else:
                        print(f"Ostrz. [{user_psid}]: Pominięto niepoprawną wiadomość w historii (idx {i}): {msg_data}")
                print(f"[{user_psid}] Wczytano historię: {len(history)} wiadomości.")
                return history, context
            else:
                print(f"!!! BŁĄD [{user_psid}]: Plik historii nie zawiera listy.")
                return [], {}
    except FileNotFoundError:
        print(f"[{user_psid}] Plik historii nie istnieje.")
        return [], {}
    except (json.JSONDecodeError, KeyError, IndexError, TypeError) as e:
        print(f"!!! BŁĄD [{user_psid}] parsowania historii: {e}.")
        return [], {}
    except Exception as e:
        print(f"!!! BŁĄD [{user_psid}] wczytywania historii: {e} !!!")
        import traceback
        traceback.print_exc()
        return [], {}


def save_history(user_psid, history, context_to_save=None):
    ensure_dir(HISTORY_DIR)
    filepath = os.path.join(HISTORY_DIR, f"{user_psid}.json")
    temp_filepath = f"{filepath}.tmp"
    history_data = []
    try:
        # Zachowaj MAX_HISTORY_TURNS konwersacyjnych par + ostatni kontekst
        max_messages_to_save = MAX_HISTORY_TURNS * 2
        history_to_process = [m for m in history if isinstance(m, Content) and m.role in ('user', 'model')]
        if len(history_to_process) > max_messages_to_save:
            history_to_process = history_to_process[-max_messages_to_save:]

        for msg in history_to_process:
             if isinstance(msg, Content) and hasattr(msg, 'role') and msg.role in ('user', 'model') and hasattr(msg, 'parts') and isinstance(msg.parts, list):
                parts_data = [{'text': part.text} for part in msg.parts if isinstance(part, Part) and hasattr(part, 'text')]
                if parts_data:
                    history_data.append({'role': msg.role, 'parts': parts_data})
             else:
                print(f"Ostrz. [{user_psid}]: Pomijanie nieprawidłowego obiektu (zapis): {msg}")

        if context_to_save and isinstance(context_to_save, dict):
             history_data.append(context_to_save)
             print(f"[{user_psid}] Dodano kontekst do zapisu: {context_to_save.get('type')}")

        with open(temp_filepath, 'w', encoding='utf-8') as f:
            json.dump(history_data, f, ensure_ascii=False, indent=2)
        os.replace(temp_filepath, filepath)
        print(f"[{user_psid}] Zapisano historię/kontekst ({len(history_data)} wpisów) do: {filepath}")
    except Exception as e:
        print(f"!!! BŁĄD [{user_psid}] zapisu historii/kontekstu: {e} !!! Plik: {filepath}")
        if os.path.exists(temp_filepath):
            try:
                os.remove(temp_filepath)
                print(f"    Usunięto {temp_filepath}.")
            except OSError as remove_e:
                print(f"    Nie można usunąć {temp_filepath}: {remove_e}")

def _get_timezone():
    global _tz
    if _tz is None:
        try:
            _tz = pytz.timezone(CALENDAR_TIMEZONE)
        except pytz.exceptions.UnknownTimeZoneError:
            print(f"BŁĄD: Strefa '{CALENDAR_TIMEZONE}' nieznana. Używam UTC.")
            _tz = pytz.utc
    return _tz

def get_calendar_service():
    global _calendar_service
    if _calendar_service:
        return _calendar_service
    if not os.path.exists(SERVICE_ACCOUNT_FILE):
        print(f"BŁĄD: Brak pliku klucza: '{SERVICE_ACCOUNT_FILE}'")
        return None
    try:
        creds = service_account.Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=CALENDAR_SCOPES)
        service = build('calendar', 'v3', credentials=creds)
        print("Utworzono usługę Calendar API.")
        _calendar_service = service
        return service
    except HttpError as error:
        print(f"Błąd API tworzenia usługi Calendar: {error}")
        return None
    except Exception as e:
        print(f"Błąd tworzenia usługi Calendar: {e}")
        import traceback
        traceback.print_exc()
        return None

def parse_event_time(event_time_data, default_tz):
    if 'dateTime' in event_time_data:
        dt_str = event_time_data['dateTime']
        try:
            dt = datetime.datetime.fromisoformat(dt_str)
        except ValueError:
            try:
                dt = datetime.datetime.strptime(dt_str, '%Y-%m-%dT%H:%M:%S%z')
            except ValueError:
                try:
                    dt = datetime.datetime.strptime(dt_str, '%Y-%m-%dT%H:%M%z')
                except ValueError:
                    print(f"Ostrz.: Nie sparsowano dateTime: {dt_str}")
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
            print(f"Ostrz.: Nie sparsowano date: {event_time_data['date']}")
            return None
    return None

def get_free_time_ranges(calendar_id, start_datetime, end_datetime):
    """
    Pobiera listę wolnych zakresów czasowych z kalendarza.
    Zwraca listę słowników: [{'start': datetime, 'end': datetime}]
    """
    service = get_calendar_service()
    tz = _get_timezone()
    if not service:
        print("Błąd: Usługa kalendarza niedostępna w get_free_time_ranges.")
        return []

    # Ensure datetimes are timezone-aware and in the correct timezone
    if start_datetime.tzinfo is None:
        start_datetime = tz.localize(start_datetime)
    else:
        start_datetime = start_datetime.astimezone(tz)

    if end_datetime.tzinfo is None:
        end_datetime = tz.localize(end_datetime)
    else:
        end_datetime = end_datetime.astimezone(tz)

    # Ensure start_datetime is not in the past
    now = datetime.datetime.now(tz)
    start_datetime = max(start_datetime, now)

    print(f"Szukanie wolnych zakresów w '{calendar_id}'")
    print(f"Zakres: {start_datetime:%Y-%m-%d %H:%M %Z} do {end_datetime:%Y-%m-%d %H:%M %Z}")

    try:
        # Używamy metody freebusy, aby uzyskać zajęte czasy
        body = {
            "timeMin": start_datetime.isoformat(),
            "timeMax": end_datetime.isoformat(),
            "timeZone": CALENDAR_TIMEZONE,
            "items": [{"id": calendar_id}]
        }
        freebusy_result = service.freebusy().query(body=body).execute()
        busy_times_raw = freebusy_result.get('calendars', {}).get(calendar_id, {}).get('busy', [])

        # Konwertuj zajęte czasy na timezone-aware datetime
        busy_times = []
        for busy_slot in busy_times_raw:
            try:
                busy_start = datetime.datetime.fromisoformat(busy_slot['start']).astimezone(tz)
                busy_end = datetime.datetime.fromisoformat(busy_slot['end']).astimezone(tz)
                # Ogranicz zajęte czasy do zakresu zapytania i godzin pracy
                busy_times.append({'start': max(busy_start, start_datetime), 'end': min(busy_end, end_datetime)})
            except ValueError as e:
                print(f"Ostrz.: Nie sparsowano busy time: {busy_slot}, błąd: {e}")

    except HttpError as error:
        print(f'Błąd API Freebusy: {error}')
        return []
    except Exception as e:
        print(f"Błąd Freebusy: {e}")
        import traceback
        traceback.print_exc()
        return []

    # Sortuj i połącz zajęte czasy
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

    # Określ wolne zakresy na podstawie zajętych
    free_ranges = []
    current_time = start_datetime

    # Dodaj przedział od początku szukania do pierwszej zajętej pory
    if merged_busy_times:
        if current_time < merged_busy_times[0]['start']:
            free_ranges.append({'start': current_time, 'end': merged_busy_times[0]['start']})
        current_time = merged_busy_times[0]['end']
    else:
        # Jeśli brak zajętych, cały okres jest wolny
        free_ranges.append({'start': current_time, 'end': end_datetime})

    # Dodaj przedziały między zajętymi porami
    for i in range(len(merged_busy_times) - 1):
        gap_start = merged_busy_times[i]['end']
        gap_end = merged_busy_times[i+1]['start']
        if gap_start < gap_end:
            free_ranges.append({'start': gap_start, 'end': gap_end})
        current_time = merged_busy_times[i+1]['end']

    # Dodaj przedział od ostatniej zajętej pory do końca szukania
    if merged_busy_times and current_time < end_datetime:
        free_ranges.append({'start': current_time, 'end': end_datetime})

    # Ogranicz wolne zakresy do godzin pracy każdego dnia
    final_free_ranges = []
    one_day = datetime.timedelta(days=1)
    current_day = start_datetime.date()

    while current_day <= end_datetime.date():
        day_start_limit = tz.localize(datetime.datetime.combine(current_day, datetime.time(WORK_START_HOUR, 0)))
        day_end_limit = tz.localize(datetime.datetime.combine(current_day, datetime.time(WORK_END_HOUR, 0)))

        day_range_start = max(day_start_limit, start_datetime)
        day_range_end = min(day_end_limit, end_datetime)

        if day_range_start < day_range_end: # Upewnij się, że zakres dnia ma sens
            for free_range in free_ranges:
                 # Znajdź przecięcie zakresu wolnego z zakresem godzin pracy w danym dniu
                 intersect_start = max(free_range['start'], day_range_start)
                 intersect_end = min(free_range['end'], day_range_end)

                 # Upewnij się, że przecięcie jest wystarczająco długie na wizytę
                 if intersect_start < intersect_end and (intersect_end - intersect_start) >= datetime.timedelta(minutes=APPOINTMENT_DURATION_MINUTES):
                    # Zadbaj o to, by początek wolnego slotu był wielokrotnością 10 minut
                    # Jeśli początek przedziału nie jest wielokrotnością 10, zaokrąglaj W GÓRĘ
                    if intersect_start.minute % 10 != 0:
                        minutes_to_add = 10 - (intersect_start.minute % 10)
                        intersect_start += datetime.timedelta(minutes=minutes_to_add)
                        intersect_start = intersect_start.replace(second=0, microsecond=0)

                    # Dodaj zakres tylko jeśli po zaokrągleniu nadal jest wystarczająco długi
                    if intersect_start < intersect_end and (intersect_end - intersect_start) >= datetime.timedelta(minutes=APPOINTMENT_DURATION_MINUTES):
                        final_free_ranges.append({'start': intersect_start, 'end': intersect_end})

        current_day += one_day

    # Upewnij się, że zakresy są unikalne i posortowane (choć pętla po dniach i tak to zapewnia)
    # Można opcjonalnie dodać unikalność, ale przy takiej logice chyba niepotrzebne
    # unique_free_ranges = list({(r['start'], r['end']): r for r in final_free_ranges}.values())
    # final_free_ranges = sorted(unique_free_ranges, key=lambda x: x['start'])

    # Filtruj zakresy, które są za krótkie na wizytę
    final_free_ranges = [r for r in final_free_ranges if (r['end'] - r['start']) >= datetime.timedelta(minutes=APPOINTMENT_DURATION_MINUTES)]


    print(f"Znaleziono {len(final_free_ranges)} wolnych zakresów czasowych (z ograniczeniami i zaokrągleniem startu).")
    # print("Zakresy:", final_free_ranges) # Debug
    return final_free_ranges


def is_slot_actually_free(start_time, calendar_id):
    """Weryfikuje w czasie rzeczywistym, czy dany slot jest na pewno wolny."""
    service = get_calendar_service(); tz = _get_timezone()
    if not service: print("Błąd: Usługa kalendarza niedostępna do weryfikacji."); return False

    if start_time.tzinfo is None: start_time = tz.localize(start_time)
    else: start_time = start_time.astimezone(tz)

    end_time = start_time + datetime.timedelta(minutes=APPOINTMENT_DURATION_MINUTES)

    # Użyj Freebusy do sprawdzenia zajętości w dokładnie tym przedziale
    body = {
        "timeMin": start_time.isoformat(),
        "timeMax": end_time.isoformat(),
        "timeZone": CALENDAR_TIMEZONE,
        "items": [{"id": calendar_id}]
    }
    try:
        freebusy_result = service.freebusy().query(body=body).execute()
        busy_times = freebusy_result.get('calendars', {}).get(calendar_id, {}).get('busy', [])
        if not busy_times:
            print(f"Weryfikacja: Slot {start_time:%Y-%m-%d %H:%M} jest wolny.")
            return True
        else:
            print(f"Weryfikacja: Slot {start_time:%Y-%m-%d %H:%M} jest ZAJĘTY.")
            return False
    except HttpError as error:
        print(f'Błąd API Freebusy podczas weryfikacji: {error}')
        return False
    except Exception as e:
        print(f"Błąd Freebusy podczas weryfikacji: {e}")
        import traceback
        traceback.print_exc()
        return False


def book_appointment(calendar_id, start_time, end_time, summary="Rezerwacja wizyty", description="", user_name=""):
    service = get_calendar_service(); tz = _get_timezone()
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

    event_summary = summary
    if user_name:
        event_summary += f" - {user_name}"

    event = {
        'summary': event_summary,
        'description': description,
        'start': {
            'dateTime': start_time.isoformat(),
            'timeZone': CALENDAR_TIMEZONE,
        },
        'end': {
            'dateTime': end_time.isoformat(),
            'timeZone': CALENDAR_TIMEZONE,
        },
        'reminders': {
            'useDefault': False,
            'overrides': [
                {'method': 'popup', 'minutes': 60},
            ],
        },
    }

    try:
        print(f"Rezerwacja: {event_summary} od {start_time:%Y-%m-%d %H:%M} do {end_time:%Y-%m-%d %H:%M}")
        created_event = service.events().insert(calendarId=calendar_id, body=event).execute()
        print(f"Zarezerwowano. ID: {created_event.get('id')}")
        day_index = start_time.weekday()
        locale_day_name = POLISH_WEEKDAYS[day_index]
        hour_str = str(start_time.hour)  # Godzina bez zera
        confirm_message = f"Świetnie! Termin na {locale_day_name}, {start_time.strftime(f'%d.%m.%Y o {hour_str}:%M')} został zarezerwowany."
        return True, confirm_message
    except HttpError as error:
        error_details = f"Kod: {error.resp.status}, Powód: {error.resp.reason}"
        try:
            error_json = json.loads(error.content.decode('utf-8'))
            error_details += f" - {error_json.get('error', {}).get('message', '')}"
        except:
            pass
        print(f"Błąd API rezerwacji: {error}, Szczegóły: {error_details}")
        if error.resp.status == 409:
            return False, "Niestety, ten termin jest już zajęty."
        elif error.resp.status == 403:
            return False, f"Brak uprawnień do zapisu w kalendarzu."
        elif error.resp.status == 404:
            return False, f"Nie znaleziono kalendarza '{calendar_id}'."
        else:
            return False, f"Błąd API ({error.resp.status}) rezerwacji."
    except Exception as e:
        import traceback
        print(f"Nieoczekiwany błąd Python rezerwacji: {e}")
        traceback.print_exc()
        return False, "Błąd systemu rezerwacji."


# ZMIANA: Usunięto find_next_reasonable_slot, AI wybiera
# def find_next_reasonable_slot(...)

# ZMIANA: Formatowanie listy zakresów dla AI
def format_ranges_for_ai(ranges):
    """Formatuje listę zakresów na czytelny tekst dla AI."""
    if not ranges:
        return "Brak dostępnych zakresów czasowych."

    ranges_by_date = defaultdict(list)
    for r in ranges:
        range_date = r['start'].date()
        # Pokaż tylko zakresy w godzinach pracy
        eff_start = max(r['start'], _get_timezone().localize(datetime.datetime.combine(range_date, datetime.time(WORK_START_HOUR, 0))))
        eff_end = min(r['end'], _get_timezone().localize(datetime.datetime.combine(range_date, datetime.time(WORK_END_HOUR, 0))))

        # Upewnij się, że zakres ma co najmniej długość wizyty
        if eff_end - eff_start >= datetime.timedelta(minutes=APPOINTMENT_DURATION_MINUTES):
             # Pamiętaj, że AI ma wybrać dokładny start w tym zakresie
            ranges_by_date[range_date].append({'start_time': eff_start.strftime('%H:%M'), 'end_time': eff_end.strftime('%H:%M')})

    formatted = [f"Dostępne ZAKRESY czasowe (wizyta trwa {APPOINTMENT_DURATION_MINUTES} minut). Wybierz jeden zakres i wygeneruj z niego DOKŁADNY termin startu (preferuj pełne godziny), dołączając go w znaczniku [SLOT_ISO:...]"]
    dates_added = 0
    for d in sorted(ranges_by_date.keys()):
        day_name = POLISH_WEEKDAYS[d.weekday()]
        date_str = d.strftime('%d.%m.%Y')
        times = [f"{tr['start_time']}-{tr['end_time']}" for tr in ranges_by_date[d]]
        if times:  # Dodaj tylko jeśli są jakieś przedziały w godzinach pracy
            formatted.append(f"- {day_name}, {date_str}: {'; '.join(times)}")
            dates_added += 1
            if dates_added >= 7:  # Ogranicz liczbę dni pokazywanych AI
                break

    if dates_added == 0:
        return "Brak dostępnych zakresów czasowych w godzinach pracy."

    return "\n".join(formatted)


def format_slot_for_user(slot_start):
    """Formatuje pojedynczy slot (datetime) na czytelny tekst dla użytkownika."""
    if not isinstance(slot_start, datetime.datetime):
        return ""
    try:
        day_index = slot_start.weekday()
        day_name = POLISH_WEEKDAYS[day_index]
        hour_str = str(slot_start.hour)  # Godzina bez zera
        return f"{day_name}, {slot_start.strftime(f'%d.%m.%Y o {hour_str}:%M')}"
    except Exception as e:
        logging.error(f"Błąd formatowania slotu: {e}", exc_info=True)
        return slot_start.isoformat()

# --- Inicjalizacja Vertex AI ---
gemini_model = None
try:
    print(f"Inicjalizowanie Vertex AI: Projekt={PROJECT_ID}, Lokalizacja={LOCATION}")
    vertexai.init(project=PROJECT_ID, location=LOCATION)
    print("Inicjalizacja Vertex AI OK.")
    print(f"Ładowanie modelu: {MODEL_ID}")
    gemini_model = GenerativeModel(MODEL_ID)
    print("Model załadowany OK.")
except Exception as e:
    print(f"!!! KRYTYCZNY BŁĄD inicjalizacji Vertex AI: {e} !!!")


# --- Funkcje wysyłania wiadomości FB ---
def _send_typing_on(recipient_id):
     if not PAGE_ACCESS_TOKEN or len(PAGE_ACCESS_TOKEN) < 50: return # Brak tokena
     params = {"access_token": PAGE_ACCESS_TOKEN}
     payload = {"recipient": {"id": recipient_id}, "sender_action": "typing_on"}
     try: requests.post(FACEBOOK_GRAPH_API_URL, params=params, json=payload, timeout=5)
     except requests.exceptions.RequestException: pass # Ignoruj błędy typingu

def _send_single_message(recipient_id, message_text):
    print(f"--- Wysyłanie fragm. do {recipient_id} (dł: {len(message_text)}) ---")
    params = {"access_token": PAGE_ACCESS_TOKEN}
    payload = {"recipient": {"id": recipient_id}, "message": {"text": message_text}, "messaging_type": "RESPONSE"}

    if not PAGE_ACCESS_TOKEN or len(PAGE_ACCESS_TOKEN) < 50:
        print(f"!!! [{recipient_id}] Brak TOKENA. Nie wysłano.")
        return False

    try:
        r = requests.post(FACEBOOK_GRAPH_API_URL, params=params, json=payload, timeout=30)
        r.raise_for_status()
        response_json = r.json()
        if response_json.get('error'):
            print(f"!!! BŁĄD FB API: {response_json['error']} !!!")
            return False
        return True
    except requests.exceptions.Timeout:
        print(f"!!! BŁĄD TIMEOUT wysyłania do {recipient_id} !!!")
        return False
    except requests.exceptions.RequestException as e:
        print(f"!!! BŁĄD wysyłania do {recipient_id}: {e} !!!")
        if hasattr(e, 'response') and e.response is not None:
            try:
                print(f"Odpowiedź FB (błąd): {e.response.json()}")
            except json.JSONDecodeError:
                print(f"Odpowiedź FB (błąd, nie JSON): {e.response.text}")
        return False

def send_message(recipient_id, full_message_text):
    if not full_message_text or not isinstance(full_message_text, str) or not full_message_text.strip():
        print(f"[{recipient_id}] Pominięto pustą wiadomość.")
        return

    message_len = len(full_message_text)
    print(f"[{recipient_id}] Przygotowanie wiad. (dł: {message_len}).")

    if ENABLE_TYPING_DELAY:
        estimated_delay = max(MIN_TYPING_DELAY_SECONDS, message_len / TYPING_CHARS_PER_SECOND)
        logging.debug(f"[{recipient_id}] Szacowany delay pisania: {estimated_delay:.2f}s")
        _send_typing_on(recipient_id)
        # Opóźnienie przed rozpoczęciem wysyłki
        time.sleep(min(estimated_delay, MAX_TYPING_DELAY_SECONDS))


    if message_len <= MESSAGE_CHAR_LIMIT:
        _send_single_message(recipient_id, full_message_text)
    else:
        chunks = []
        remaining_text = full_message_text
        print(f"[{recipient_id}] Dzielenie wiad. (limit: {MESSAGE_CHAR_LIMIT})...")

        while remaining_text:
            if len(remaining_text) <= MESSAGE_CHAR_LIMIT:
                chunks.append(remaining_text.strip())
                break

            split_index = -1
            # Szukaj podziału w priorytetowej kolejności
            for delimiter in ['\n\n', '\n', '. ', '! ', '? ', ' ']:
                search_limit = MESSAGE_CHAR_LIMIT - (len(delimiter) - 1) if len(delimiter) > 1 else MESSAGE_CHAR_LIMIT
                temp_index = remaining_text.rfind(delimiter, 0, search_limit)
                if temp_index != -1:
                    split_index = temp_index + len(delimiter)
                    break

            # Jeśli nie znaleziono sensownego miejsca, podziel na siłę
            if split_index == -1:
                split_index = MESSAGE_CHAR_LIMIT

            chunk = remaining_text[:split_index].strip()
            if chunk:
                chunks.append(chunk)

            remaining_text = remaining_text[split_index:].strip()

        num_chunks = len(chunks)
        print(f"[{recipient_id}] Podzielono na {num_chunks} fragmentów.")
        send_success_count = 0
        for i, chunk in enumerate(chunks):
            print(f"[{recipient_id}] Wysyłanie fragm. {i+1}/{num_chunks} (dł: {len(chunk)})...")
            if not _send_single_message(recipient_id, chunk):
                print(f"!!! [{recipient_id}] Anulowano resztę po błędzie fragm. {i+1} !!!")
                break
            send_success_count += 1
            # Opóźnienie między fragmentami, ale nie po ostatnim
            if i < num_chunks - 1:
                print(f"[{recipient_id}] Oczekiwanie {MESSAGE_DELAY_SECONDS}s...")
                time.sleep(MESSAGE_DELAY_SECONDS)

        print(f"--- [{recipient_id}] Zakończono wysyłanie {send_success_count}/{num_chunks} fragm. ---")

# --- Funkcja do symulowania pisania (wywoływana podczas myślenia AI) ---
def _simulate_typing(recipient_id, duration_seconds):
    if ENABLE_TYPING_DELAY:
        _send_typing_on(recipient_id)
        time.sleep(min(duration_seconds, MAX_TYPING_DELAY_SECONDS))


# --- Generic function to call Gemini API ---
def _call_gemini(user_psid, prompt, generation_config, task_name, max_retries=3):
    if not gemini_model:
        logging.error(f"!!! [{user_psid}] Model Gemini ({task_name}) niezaładowany. Nie mogę dzwonić do API.")
        return None

    history_debug = [{"role": m.role, "parts": [{"text": p.text} for p in m.parts]} if isinstance(m, Content) else m for m in prompt]
    logging.debug(f"[{user_psid}] Prompt dla Gemini ({task_name}):\n{json.dumps(history_debug, indent=2, ensure_ascii=False)}")

    attempt = 0
    while attempt < max_retries:
        try:
            _simulate_typing(user_psid, MIN_TYPING_DELAY_SECONDS + (len(prompt[-1].parts[0].text if prompt and prompt[-1].parts and prompt[-1].parts[0].text else "") / TYPING_CHARS_PER_SECOND) * 0.5)
            response = gemini_model.generate_content(
                prompt,
                generation_config=generation_config,
                safety_settings=SAFETY_SETTINGS
            )

            if response and response.candidates and response.candidates[0].content.parts:
                generated_text = "".join(part.text for part in response.candidates[0].content.parts)
                logging.info(f"[{user_psid}] Gemini ({task_name}) odp: '{generated_text.strip()[:100]}...'")
                return generated_text.strip()
            elif response and response.prompt_feedback and response.prompt_feedback.block_reason:
                 logging.warning(f"[{user_psid}] Gemini ({task_name}) zablokowane! Powód: {response.prompt_feedback.block_reason}. Odp: {response.text}")
                 return "Przepraszam, Twoja wiadomość naruszyła zasady bezpieczeństwa. Czy możesz sformułować ją inaczej?" # Odpowiedź w przypadku blokady
            else:
                 logging.error(f"!!! BŁĄD [{user_psid}] Gemini ({task_name}) - Pusta odpowiedź lub brak kandydata. Odp: {response}")
                 attempt += 1
                 time.sleep(1 * attempt) # Czekaj dłużej przy kolejnych próbach
        except Exception as e:
            logging.error(f"!!! BŁĄD [{user_psid}] Gemini ({task_name}) - API Error ({attempt+1}/{max_retries}): {e}", exc_info=True)
            attempt += 1
            time.sleep(2 * attempt) # Czekaj dłużej przy kolejnych próbach

    logging.error(f"!!! BŁĄD [{user_psid}] Gemini ({task_name}) - Nie udało się po {max_retries} próbach.")
    return None # Zwróć None po nieudanych próbach


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
*   `[REJECT_FIND_NEXT PREFERENCE='any']`: Odrzucenie, brak preferencji (nie pasuje, inny, daj inny).
*   `[REJECT_FIND_NEXT PREFERENCE='later']`: Chce później tego samego dnia lub ogólnie później (np. "później", "za wcześnie" jeśli było popołudniu).
*   `[REJECT_FIND_NEXT PREFERENCE='earlier']`: Chce wcześniej tego samego dnia (np. "za późno").
*   `[REJECT_FIND_NEXT PREFERENCE='afternoon']`: Za wcześnie (np. "rano mi nie pasuje", "chcę popołudniu").
*   `[REJECT_FIND_NEXT PREFERENCE='next_day']`: Chce inny dzień, jutro (np. "jutro", "nie dzisiaj").
*   `[REJECT_FIND_NEXT PREFERENCE='specific_day' DAY='NAZWA_DNIA']`: Chce konkretny dzień (np. "pasuje mi środa"). Użyj polskiej nazwy dnia z POLISH_WEEKDAYS.
*   `[REJECT_FIND_NEXT PREFERENCE='specific_hour' HOUR='GODZINA']`: Chce konkretną godzinę (np. "tylko o 18:00"). Użyj tylko cyfry godziny (np. '18').
*   `[REJECT_FIND_NEXT PREFERENCE='specific_datetime' DAY='NAZWA_DNIA' HOUR='GODZINA']`: Chce konkretny dzień i godzinę (np. "środa o 17"). Użyj polskiej nazwy dnia i cyfry godziny.
*   `[REJECT_FIND_NEXT PREFERENCE='specific_day_later' DAY='NAZWA_DNIA']`: Chce konkretny dzień, ale później (np. "środa, ale później"). Użyj polskiej nazwy dnia.
*   `[REJECT_FIND_NEXT PREFERENCE='specific_day_earlier' DAY='NAZWA_DNIA']`: Chce konkretny dzień, ale wcześniej (np. "środa, ale wcześniej"). Użyj polskiej nazwy dnia.
*   `[CLARIFY]`: Niejasna odpowiedź, pytanie niezwiązane z terminem (np. "ile kosztuje?", "co to za korepetycje?").

**Ważne:** Zwróć DOKŁADNIE JEDEN znacznik. Preferuj bardziej szczegółowe znaczniki (np. `specific_datetime` nad `specific_day`). Nazwy dni i godziny w znacznikach muszą być poprawnie wyodrębnione z odpowiedzi użytkownika.
"""
# ---------------------------------------------------------------------

# --- INSTRUKCJA SYSTEMOWA (dla AI prowadzącego ogólną rozmowę) ---
SYSTEM_INSTRUCTION_GENERAL = """Jesteś przyjaznym i pomocnym asystentem klienta w 'Zakręcone Korepetycje'. Prowadzisz rozmowę o korepetycjach online.

**Twoje cele:**
1.  Odpowiadaj na pytania użytkownika dotyczące korepetycji.
2.  Utrzymuj przyjazny ton.
3.  Nie podawaj cennika ani szczegółów rozliczeń (kwestie płatności są omawiane po umówieniu terminu).
4.  **KLUCZOWE:** Jeśli użytkownik wyrazi chęć umówienia lekcji próbnej (lub jakiejkolwiek wizyty), dodaj na końcu swojej odpowiedzi znacznik `{intent_marker}`, aby zasygnalizować systemowi, że należy przejść do proponowania terminu.

**Przykłady wywołania intencji umówienia:**
*   "Chciałbym/Chciałabym się umówić."
*   "Kiedy można przyjść/zacząć?"
*   "Proszę o termin."
*   "Czy są wolne miejsca/godziny?"
*   "Ile trwa lekcja próbna i kiedy ją umówić?" (odpowiedz na część pytania, dodaj znacznik)

**Zasady:** Odpowiadaj na bieżące pytanie/stwierdzenie, ale jeśli tylko pojawi się intencja umówienia terminu, zakończ odpowiedź znacznikiem `{intent_marker}`. Nie dodawaj znacznika bez wyraźnej intencji ze strony użytkownika.
""".format(intent_marker=INTENT_SCHEDULE_MARKER)
# ---------------------------------------------------------------------

# --- Funkcja interakcji z Gemini (proponowanie slotu) ---
def get_gemini_slot_proposal(user_psid, history_for_gemini, available_ranges):
    """
    Pobiera od AI propozycję terminu z listy dostępnych zakresów.
    history_for_gemini to lista obiektów Content (user/model).
    available_ranges to lista słowników {'start': dt, 'end': dt}.
    """
    if not gemini_model:
        logging.error(f"!!! [{user_psid}] Model niezaładowany! get_gemini_slot_proposal nie działa.")
        return None, None

    if not available_ranges:
        logging.warning(f"[{user_psid}]: Brak zakresów dla AI do proponowania.")
        return "Niestety, brak wolnych terminów.", None

    ranges_text = format_ranges_for_ai(available_ranges)  # Użyj nowej funkcji formatującej

    # Przygotuj prompt: Instrukcja + historia (user/model)
    # Dodaj początkową wymianę, aby AI zrozumiało swoją rolę
    instr = SYSTEM_INSTRUCTION_TEXT_PROPOSE.format(available_ranges_text=ranges_text)
    prompt = [
        Content(role="user", parts=[Part.from_text(instr)]),
        Content(role="model", parts=[Part.from_text(f"OK. Wygeneruję termin {APPOINTMENT_DURATION_MINUTES} min z podanych zakresów.")])
    ] + history_for_gemini # Dodaj prawdziwą historię rozmowy

    # Usuń najstarsze wiadomości z promptu, jeśli przekracza limit (z zachowaniem instrukcji startowej)
    while len(prompt) > (MAX_HISTORY_TURNS * 2 + 2) and len(prompt) > 2:
        prompt.pop(2) # Usuń najstarszą wiadomość (po instrukcjach startowych)
        if len(prompt) > 2:
             prompt.pop(2) # Usuń kolejną (parę)


    generated_text = _call_gemini(user_psid, prompt, GENERATION_CONFIG_PROPOSAL, "Slot Proposal from Ranges")

    if not generated_text:
        return None, None # Błąd API

    # Parsowanie znacznika ISO
    iso_match = re.search(rf"\{re.escape(SLOT_ISO_MARKER_PREFIX)}(.*?)\{re.escape(SLOT_ISO_MARKER_SUFFIX)}", generated_text)

    if iso_match:
        extracted_iso = iso_match.group(1)
        # Usuń znacznik z tekstu wyświetlanego użytkownikowi
        text_for_user = re.sub(rf"\{re.escape(SLOT_ISO_MARKER_PREFIX)}.*?\{re.escape(SLOT_ISO_MARKER_SUFFIX)}", "", generated_text).strip()
        text_for_user = re.sub(r'\s+', ' ', text_for_user).strip() # Znormalizuj spacje
        logging.info(f"[{user_psid}] AI wygenerowało ISO: {extracted_iso}. Tekst dla użytkownika: '{text_for_user}'")

        try:
            # Sprawdź poprawność formatu ISO 8601 i strefy czasowej
            proposed_start = datetime.datetime.fromisoformat(extracted_iso).astimezone(_get_timezone())

            # Sprawdź, czy wygenerowany termin mieści się w jednym z dostępnych zakresów
            is_possible_in_ranges = False
            for r in available_ranges:
                if r['start'] <= proposed_start and proposed_start + datetime.timedelta(minutes=APPOINTMENT_DURATION_MINUTES) <= r['end']:
                    is_possible_in_ranges = True
                    break

            if is_possible_in_ranges:
                 # Dodatkowa weryfikacja z Google API dla pewności (minimalizuje szansę na 409)
                 if is_slot_actually_free(proposed_start, TARGET_CALENDAR_ID):
                     return text_for_user, extracted_iso
                 else:
                     logging.warning(f"!!! AI Error [{user_psid}]: Wygenerowany slot {extracted_iso} okazał się ZAJĘTY po weryfikacji API!")
                     return None, None # Nie proponuj zajętego terminu

            else:
                 logging.error(f"!!! AI Error [{user_psid}]: Wygenerowany ISO '{extracted_iso}' poza dostępnymi zakresami! (start: {proposed_start:%Y-%m-%d %H:%M})")
                 # W takim przypadku poproś AI o inną próbę (zwracając None, None), lub zwróć generyczny błąd.
                 # W tej implementacji zwracamy None, None, co spowoduje błąd szukania i powrót do ogólnej rozmowy.
                 return None, None

        except ValueError:
            logging.error(f"!!! AI Error: Zły format ISO '{extracted_iso}'!")
            return None, None
    else:
        logging.error(f"!!! AI Error [{user_psid}]: Brak znacznika ISO w odpowiedzi AI! Odpowiedź: '{generated_text}'")
        return None, None # Brak znacznika - błąd

# --- Funkcja interakcji z Gemini (interpretacja feedbacku) ---
def get_gemini_feedback_decision(user_psid, user_feedback, history_for_gemini, last_proposal_text):
     """
     Pyta AI o interpretację odpowiedzi użytkownika na zaproponowany termin.
     history_for_gemini to lista obiektów Content (user/model).
     """
     if not gemini_model:
        logging.error(f"!!! [{user_psid}] Model Gemini niezaładowany! get_gemini_feedback_decision nie działa.")
        return "[CLARIFY]" # Domyślnie niejasna odpowiedź

     user_content = Content(role="user", parts=[Part.from_text(user_feedback)])

     # Przygotuj prompt: Instrukcja + historia (user/model) + aktualna wiadomość
     # Pamiętaj, że historia przekazana do tej funkcji już nie zawiera kontekstu systemowego
     max_messages = MAX_HISTORY_TURNS * 2
     history_to_send = history_for_gemini[-max_messages:] if len(history_for_gemini) > max_messages else history_for_gemini


     instr = SYSTEM_INSTRUCTION_TEXT_FEEDBACK.format(last_proposal_text=last_proposal_text, user_feedback=user_feedback)
     prompt = [Content(role="user", parts=[Part.from_text(instr)])] + history_to_send + [user_content]

     # Usuń najstarsze wiadomości z promptu, jeśli przekracza limit
     while len(prompt) > (MAX_HISTORY_TURNS * 2 + 2) and len(prompt) > 1: # Zostaw instrukcję i ostatnie user_content
         prompt.pop(1) # Usuń najstarszą wiadomość (po instrukcji startowej)
         if len(prompt) > 1:
             prompt.pop(1) # Usuń kolejną (parę)

     decision = _call_gemini(user_psid, prompt, GENERATION_CONFIG_FEEDBACK, "Feedback Interpretation")

     if not decision:
         return "[CLARIFY]" # Błąd API - domyślnie niejasne

     # Weryfikuj, czy odpowiedź jest poprawnym znacznikiem
     if decision.startswith("[") and decision.endswith("]"):
         # Dodatkowa walidacja dla znacznika specific_day / specific_datetime
         if decision.startswith("[REJECT_FIND_NEXT PREFERENCE='specific_day'"):
             match = re.search(r"DAY='([^']*)'", decision)
             day = match.group(1) if match else None
             if not day or day.capitalize() not in POLISH_WEEKDAYS:
                 logging.warning(f"Ostrz. [{user_psid}]: AI zwróciło specific_day z nieznanym dniem '{day}'. CLARIFY.")
                 return "[CLARIFY]"
         elif decision.startswith("[REJECT_FIND_NEXT PREFERENCE='specific_datetime'"):
              match = re.search(r"DAY='([^']*)' HOUR='(\d+)'", decision)
              day = match.group(1) if match else None
              hour_str = match.group(2) if match else None
              if not day or day.capitalize() not in POLISH_WEEKDAYS or not hour_str or not (0 <= int(hour_str) <= 23):
                  logging.warning(f"Ostrz. [{user_psid}]: AI zwróciło specific_datetime z nieznanym dniem '{day}' lub godziną '{hour_str}'. CLARIFY.")
                  return "[CLARIFY]"
         elif decision.startswith("[REJECT_FIND_NEXT PREFERENCE='specific_hour'"):
             match = re.search(r"HOUR='(\d+)'", decision)
             hour_str = match.group(1) if match else None
             if not hour_str or not (0 <= int(hour_str) <= 23):
                 logging.warning(f"Ostrz. [{user_psid}]: AI zwróciło specific_hour z nieznaną godziną '{hour_str}'. CLARIFY.")
                 return "[CLARIFY]"
         elif decision.startswith("[REJECT_FIND_NEXT PREFERENCE='specific_day_later'"):
             match = re.search(r"DAY='([^']*)'", decision)
             day = match.group(1) if match else None
             if not day or day.capitalize() not in POLISH_WEEKDAYS:
                 logging.warning(f"Ostrz. [{user_psid}]: AI zwróciło specific_day_later z nieznanym dniem '{day}'. CLARIFY.")
                 return "[CLARIFY]"
         elif decision.startswith("[REJECT_FIND_NEXT PREFERENCE='specific_day_earlier'"):
             match = re.search(r"DAY='([^']*)'", decision)
             day = match.group(1) if match else None
             if not day or day.capitalize() not in POLISH_WEEKDAYS:
                 logging.warning(f"Ostrz. [{user_psid}]: AI zwróciło specific_day_earlier z nieznanym dniem '{day}'. CLARIFY.")
                 return "[CLARIFY]"

         logging.info(f"[{user_psid}] AI feedback: {decision}")
         return decision
     else:
         logging.warning(f"Ostrz. [{user_psid}]: AI nie zwróciło oczekiwanego znacznika feedbacku: '{decision}'. Zwracam CLARIFY.")
         return "[CLARIFY]" # Odpowiedź AI nie jest znacznikiem

# --- Funkcja interakcji z Gemini (ogólna rozmowa) ---
def get_gemini_general_response(user_psid, current_user_message, history_for_gemini):
    """
    Prowadzi ogólną rozmowę z AI.
    history_for_gemini to lista obiektów Content (user/model).
    """
    if not gemini_model:
        logging.error(f"!!! [{user_psid}] Model Gemini niezaładowany! get_gemini_general_response nie działa.")
        return "Przepraszam, wystąpił błąd."

    user_content = Content(role="user", parts=[Part.from_text(current_user_message)])

    # Przygotuj prompt: Instrukcja + początkowa wymiana + historia (user/model) + aktualna wiadomość
    max_messages = MAX_HISTORY_TURNS * 2
    history_to_send = history_for_gemini[-max_messages:] if len(history_for_gemini) > max_messages else history_for_gemini


    prompt = [
        Content(role="user", parts=[Part.from_text(SYSTEM_INSTRUCTION_GENERAL)]),
        Content(role="model", parts=[Part.from_text("Rozumiem.")])
    ] + history_to_send + [user_content]

    # Usuń najstarsze wiadomości z promptu, jeśli przekracza limit
    while len(prompt) > (MAX_HISTORY_TURNS * 2 + 2) and len(prompt) > 2:
        prompt.pop(2) # Usuń najstarszą wiadomość (po instrukcjach startowych)
        if len(prompt) > 2:
             prompt.pop(2) # Usuń kolejną (parę)


    response_text = _call_gemini(user_psid, prompt, GENERATION_CONFIG_DEFAULT, "General Conversation")

    if response_text:
        return response_text
    else:
        return "Przepraszam, wystąpił błąd podczas przetwarzania Twojej wiadomości." # Generyczna odpowiedź po nieudanych próbach API


# --- Obsługa Weryfikacji Webhooka (GET) ---
@app.route('/webhook', methods=['GET'])
def webhook_verification():
    logging.info("--- GET weryfikacja ---")
    hub_mode = request.args.get('hub.mode')
    hub_token = request.args.get('hub.verify_token')
    hub_challenge = request.args.get('hub.challenge')

    logging.info(f"Mode:{hub_mode},Token:{'OK' if hub_token==VERIFY_TOKEN else 'BŁĄD'},Challenge:{'Jest' if hub_challenge else 'Brak'}")

    if hub_mode == 'subscribe' and hub_token == VERIFY_TOKEN:
        logging.info("Weryfikacja GET OK!")
        return Response(hub_challenge, status=200)
    else:
        logging.warning("Weryfikacja GET FAILED.")
        return Response("Verification failed", status=403)

# --- Główna Obsługa Webhooka (POST) ---
@app.route('/webhook', methods=['POST'])
def webhook_handle():
    logging.info(f"\n{'='*30} {datetime.datetime.now():%Y-%m-%d %H:%M:%S} POST {'='*30}")
    raw_data = request.data.decode('utf-8')
    data = None
    try:
        data = json.loads(raw_data)

        if data and data.get("object") == "page":
            for entry in data.get("entry", []):
                page_id = entry.get("id")
                timestamp = entry.get("time")
                for event in entry.get("messaging", []):
                    sender_id = event.get("sender", {}).get("id")
                    if not sender_id:
                        continue # Pomiń zdarzenia bez sender ID

                    logging.info(f"--- Zdarzenie dla PSID: {sender_id} ---")

                    # Wczytaj historię i kontekst
                    history, context = load_history(sender_id)
                    # Filter out system messages for AI processing
                    history_for_gemini = [h for h in history if isinstance(h, Content) and h.role in ('user', 'model')]

                    # Sprawdź, czy ostatni wpis w pliku historii to kontekst rezerwacji
                    is_context_active = False
                    last_iso = None
                    if context.get('type') == 'last_proposal' and context.get('slot_iso'):
                         # Sprawdź, czy indeks kontekstu jest ostatni w pliku
                         # UWAGA: Może być problematyczne przy jednoczesnym pisaniu.
                         # Lepsze rozwiązanie to Timestamp lub ID wiadomości, ale prostsze to index.
                         # Używamy prostej weryfikacji, czy kontekst jest ostatnim wpisem.
                         # Ponowne wczytanie pliku jest mało wydajne, ale proste do weryfikacji.
                         temp_history, temp_context = load_history(sender_id)
                         if temp_context.get('type') == 'last_proposal' and temp_context.get('slot_iso') == context.get('slot_iso'):
                             # Prosta weryfikacja: czy ostatni wpis w pliku to ten kontekst?
                             try:
                                 with open(os.path.join(HISTORY_DIR, f"{sender_id}.json"), 'r', encoding='utf-8') as f_check:
                                     full_file_data = json.load(f_check)
                                     if full_file_data and isinstance(full_file_data[-1], dict) and full_file_data[-1].get('type') == 'last_proposal' and full_file_data[-1].get('slot_iso') == context.get('slot_iso'):
                                          is_context_active = True
                                          last_iso = context['slot_iso']
                                          logging.info(f"    Aktywny kontekst: {last_iso}")
                                     else:
                                         logging.info(f"    Kontekst '{context.get('slot_iso')}' stary/nieostatni w pliku. Reset.")
                             except Exception as e_check:
                                  logging.warning(f"Ostrz. [{sender_id}] błąd weryfikacji kontekstu w pliku: {e_check}")
                                  # Na wszelki wypadek zakładamy, że kontekst jest nieaktywny
                                  pass
                         else:
                             logging.info(f"    Kontekst '{context.get('slot_iso')}' nieaktualny (nowy plik). Reset.")


                    action = None
                    msg_result = None
                    ctx_save = None  # Kontekst do zapisania po przetworzeniu
                    model_resp = None # Odpowiedź modelu (Content) do dodania do historii

                    # --- Obsługa wiadomości tekstowych ---
                    if message_data := event.get("message"):
                        if message_data.get("is_echo"):
                            continue  # Pomijaj wiadomości wysłane przez bota

                        user_input = message_data.get("text", "").strip()
                        user_content = Content(role="user", parts=[Part.from_text(user_input)]) if user_input else None

                        if ENABLE_TYPING_DELAY and user_input:
                            # Krótki delay po otrzymaniu wiadomości przed odpowiedzią
                            time.sleep(MIN_TYPING_DELAY_SECONDS)

                        if is_context_active and user_input:
                            # --- Użytkownik odpowiada na zaproponowany termin ---
                            logging.info("      Oczekiwano na feedback. Pytanie AI o decyzję...")
                            try:
                                last_dt = datetime.datetime.fromisoformat(last_iso).astimezone(_get_timezone())
                                decision = get_gemini_feedback_decision(sender_id, user_input, history_for_gemini, format_slot_for_user(last_dt))

                                if decision == "[ACCEPT]":
                                    action = 'book'
                                elif decision.startswith("[REJECT_FIND_NEXT"):
                                    action = 'find_and_propose'
                                    # Parsuj preferencje
                                    pref = 'any'
                                    day = None
                                    hour = None

                                    pref_match = re.search(r"PREFERENCE='([^']*)'", decision)
                                    if pref_match:
                                         pref = pref_match.group(1)

                                    if pref in ['specific_day', 'specific_datetime', 'specific_day_later', 'specific_day_earlier']:
                                        m_day = re.search(r"DAY='([^']*)'", decision)
                                        day = m_day.group(1) if m_day else None # Pobierz nazwę dnia

                                    if pref in ['specific_hour', 'specific_datetime']:
                                        m_hour = re.search(r"HOUR='(\d+)'", decision)
                                        hour_str = m_hour.group(1) if m_hour else None
                                        try: hour = int(hour_str) if hour_str else None
                                        except ValueError: hour = None # Na wypadek gdyby AI dało coś nie-cyfrowego

                                    logging.info(f"      Feedback Decyzja AI: {decision} -> Action: find_and_propose, Pref: {pref}, Day: {day}, Hour: {hour}")

                                elif decision == "[CLARIFY]":
                                    action = 'send_clarification'
                                    msg_result = "Nie jestem pewien, czy ten termin Ci odpowiada? Czy możemy go zarezerwować, czy wolisz poszukać innego?"
                                elif decision.startswith("[REJECT"): # Ogólne odrzucenie bez specyficznych preferencji
                                     action = 'find_and_propose'
                                     pref = 'any'
                                     logging.info(f"      Feedback Decyzja AI: {decision} -> Action: find_and_propose, Pref: {pref}")
                                else:
                                     # Powinno być obsłużone przez [CLARIFY], ale na wszelki wypadek
                                     action = 'send_error'
                                     msg_result = "Problem z przetworzeniem Twojej odpowiedzi na termin. Spróbujmy jeszcze raz – czy zaproponowany termin pasuje?"

                            except Exception as feedback_err:
                                logging.error(f"BŁĄD podczas przetwarzania feedbacku na termin: {feedback_err}", exc_info=True)
                                action = 'send_error'
                                msg_result = "Wystąpił błąd podczas interpretacji Twojej odpowiedzi. Przepraszam za kłopot."
                                # W przypadku błędu, resetujemy kontekst rezerwacji, aby nie utknąć w pętli
                                ctx_save = None


                        elif user_input:
                            # --- Normalna rozmowa z użytkownikiem ---
                            logging.info("      -> Gemini (normalna rozmowa)...")
                            response = get_gemini_general_response(sender_id, user_input, history_for_gemini)

                            if response:
                                # Sprawdź, czy AI zasygnalizowało intencję umówienia terminu
                                if INTENT_SCHEDULE_MARKER in response:
                                    logging.info(f"      AI wykryło intencję [{INTENT_SCHEDULE_MARKER}].")
                                    action = 'find_and_propose' # Pierwsze szukanie terminu
                                    # Usuń znacznik intencji z tekstu odpowiedzi
                                    msg_result = response.split(INTENT_SCHEDULE_MARKER, 1)[0].strip()
                                    if not msg_result:
                                        msg_result = "Dobrze, sprawdzę dostępne terminy." # Domyślny tekst, jeśli AI nie dało żadnego przed znacznikiem
                                    # Przy pierwszym szukaniu użyjemy domyślnych preferencji
                                    pref = 'any'
                                    day = None
                                    hour = None
                                else:
                                    action = 'send_gemini_response'
                                    msg_result = response
                            else:
                                # Błąd API Gemini w ogólnej rozmowie
                                action = 'send_error'
                                msg_result = "Przepraszam, wystąpił błąd podczas przetwarzania Twojej wiadomości."


                        elif attachments := message_data.get("attachments"):
                            # --- Obsługa załączników ---
                            att_type = attachments[0].get('type','?')
                            logging.info(f"      Otrzymano załącznik: {att_type}.")
                            # Dodaj do historii jako wiadomość od użytkownika (z informacją o załączniku)
                            user_content = Content(role="user", parts=[Part.from_text(f"[Załącznik:{att_type}]")])
                            # Odpowiedz użytkownikowi, że nie obsługujesz załączników
                            msg_result = "Przepraszam, nie obsługuję załączników takich jak ten."
                            action = 'send_info' # Informacja, nie błąd krytyczny
                        else:
                             # --- Nieznany typ wiadomości ---
                            logging.warning(f"      Nieznany typ wiadomości lub pusta wiadomość: {message_data}")
                            msg_result="Nie rozumiem." # Domyślna odpowiedź na nieznane
                            action = 'send_info'

                        # --- Wykonanie akcji na podstawie 'action' ---
                        history_saved = False # Flaga, czy historia została już zapisana w tym cyklu

                        if action == 'book':
                            # Potwierdzenie i rezerwacja terminu z Calendar API
                            # Użyjemy ostatniego zaproponowanego slotu zapisanego w kontekście
                            if last_iso:
                                try:
                                    tz = _get_timezone()
                                    start = datetime.datetime.fromisoformat(last_iso).astimezone(tz)
                                    end = start + datetime.timedelta(minutes=APPOINTMENT_DURATION_MINUTES)

                                    # Pobierz profil użytkownika, jeśli dostępny, do nazwy wydarzenia
                                    prof = get_user_profile(sender_id)
                                    name = prof.get('first_name', '') if prof else ''

                                    ok, msg = book_appointment(TARGET_CALENDAR_ID, start, end, "Rezerwacja FB", f"PSID:{sender_id}\nImię:{name}", name)
                                    msg_result = msg
                                    ctx_save = None # Po rezerwacji resetujemy kontekst oczekiwania na akceptację

                                except Exception as e:
                                    logging.error(f"BŁĄD podczas próby rezerwacji terminu ISO {last_iso}: {e}", exc_info=True)
                                    msg_result = "Wystąpił błąd podczas rezerwacji terminu. Spróbuj ponownie lub skontaktuj się z administratorem."
                                    ctx_save = None # Reset kontekstu po błędzie rezerwacji
                            else:
                                # To nie powinno się zdarzyć, jeśli is_context_active było True
                                logging.error("!!! PRÓBA REZERWACJI BEZ AKTYWNEGO KONTEKSTU ISO !!!")
                                msg_result = "Nie mogę teraz zarezerwować. Problem z systemem."
                                ctx_save = None


                        elif action == 'find_and_propose':
                            # Znajdź wolne zakresy i poproś AI o propozycję
                            try:
                                tz = _get_timezone()
                                now = datetime.datetime.now(tz)

                                # Określ punkt startowy wyszukiwania na podstawie preferencji
                                search_start = now # Domyślnie szukaj od teraz

                                # Jeśli to kolejne szukanie po odrzuceniu terminu
                                if last_iso and is_context_active: # Używamy ostatniego ISO jako punktu odniesienia
                                    last_dt = datetime.datetime.fromisoformat(last_iso).astimezone(tz)

                                    # Dostosuj search_start na podstawie preferencji z feedbacku
                                    if pref == 'later':
                                         # Szukaj co najmniej od końca ostatnio zaproponowanego slotu + margines
                                         base_start = last_dt + datetime.timedelta(minutes=APPOINTMENT_DURATION_MINUTES)
                                         # Plus dodatkowy czas (np. kilka godzin), jeśli użytkownik chce "później"
                                         later_start_same_day = tz.localize(datetime.datetime.combine(last_dt.date(), datetime.time(last_dt.hour + 3, 0))) if last_dt.hour + 3 <= WORK_END_HOUR else last_dt + datetime.timedelta(days=1)
                                         search_start = max(base_start, later_start_same_day) # Szukaj od końca ostatniego slotu LUB 3h później tego samego dnia (jeśli możliwe)
                                         search_start = max(search_start, now) # Upewnij się, że nie cofamy się w czasie
                                         logging.info(f"      -> Szukanie 'later' od {search_start}")

                                    elif pref == 'earlier':
                                         # Szukaj wcześniej tego samego dnia, ale nie przed godziną pracy lub early_hour_limit
                                         earlier_limit = tz.localize(datetime.datetime.combine(last_dt.date(), datetime.time(EARLY_HOUR_LIMIT, 0)))
                                         day_start_limit = tz.localize(datetime.datetime.combine(last_dt.date(), datetime.time(WORK_START_HOUR, 0)))
                                         search_start = max(day_start_limit, earlier_limit, now)
                                         # Szukamy PRZED ostatnim terminem, więc zakres wyszukiwania będzie do last_dt
                                         search_end = last_dt - datetime.timedelta(minutes=1) # Szukaj do minuty przed ostatnim terminem
                                         if search_start >= search_end:
                                             # Jeśli brak sensownego zakresu wcześniej dzisiaj, szukaj normalnie od teraz
                                             search_start = now
                                             search_end = tz.localize(datetime.datetime.combine((now + datetime.timedelta(days=MAX_SEARCH_DAYS)).date(), datetime.time(WORK_END_HOUR, 0)))
                                             msg_result = (msg_result + " Niestety, nie znalazłem nic sensownego wcześniej dzisiaj. Szukam dalej...") if msg_result else "Niestety, nie znalazłem nic sensownego wcześniej dzisiaj. Szukam dalej..."
                                             logging.info(f"      -> Brak zakresu 'earlier', szukanie 'any' od {search_start}")

                                         else: logging.info(f"      -> Szukanie 'earlier' od {search_start} do {search_end}")


                                    elif pref == 'afternoon':
                                         # Szukaj popołudniu TEGO SAMEGO DNIA, od preferowanej godziny popołudniowej
                                         afternoon_start_today = tz.localize(datetime.datetime.combine(last_dt.date(), datetime.time(PREFERRED_WEEKDAY_START_HOUR, 0)))
                                         search_start = max(afternoon_start_today, now)
                                         logging.info(f"      -> Szukanie 'afternoon' od {search_start}")


                                    elif pref == 'next_day':
                                         # Szukaj od początku następnego dnia pracy
                                         next_day = last_dt.date() + datetime.timedelta(days=1)
                                         search_start = tz.localize(datetime.datetime.combine(next_day, datetime.time(WORK_START_HOUR, 0)))
                                         search_start = max(search_start, now)
                                         logging.info(f"      -> Szukanie 'next_day' od {search_start}")


                                    elif pref in ['specific_day', 'specific_datetime', 'specific_day_later', 'specific_day_earlier'] and day:
                                         # Szukaj od początku lub konkretnej godziny w wybranym dniu
                                         try:
                                             # Znajdź datę wybranego dnia w przyszłości (najbliższa, włącznie z dzisiaj jeśli to ten sam dzień)
                                             today = now.date()
                                             day_index = POLISH_WEEKDAYS.index(day.capitalize())
                                             days_ahead = (day_index - today.weekday() + 7) % 7
                                             # Jeśli dzień jest dziś, ale już po godzinach pracy, przejdź do następnego tygodnia
                                             if days_ahead == 0 and now.time() >= datetime.time(WORK_END_HOUR, 0):
                                                  days_ahead = 7

                                             target_date = today + datetime.timedelta(days=days_ahead)
                                             target_datetime = tz.localize(datetime.datetime.combine(target_date, datetime.time(WORK_START_HOUR, 0))) # Domyślny start dnia

                                             if pref in ['specific_datetime', 'specific_hour'] and hour is not None:
                                                 try:
                                                     target_time_hour = hour
                                                     # Ustaw konkretną godzinę, ale nie wcześniej niż początek dnia pracy
                                                     target_datetime = tz.localize(datetime.datetime.combine(target_date, datetime.time(target_time_hour, 0)))
                                                     # Jeśli wybrano konkretną godzinę, szukaj tylko w przedziale tej godziny
                                                     # (Można zmodyfikować, by szukać od tej godziny do końca dnia)
                                                     # W tej logice szukamy OD tej godziny
                                                     logging.info(f"      -> Szukanie 'specific_datetime/hour' od {target_datetime}")
                                                     search_start = max(target_datetime, now) # Upewnij się, że nie w przeszłości
                                                 except ValueError:
                                                      logging.warning(f"      Niepoprawna godzina z feedbacku: {hour}. Szukanie od początku dnia.")
                                                      search_start = max(target_datetime, now) # Szukaj od początku dnia
                                             elif pref == 'specific_day_later':
                                                  # Szukaj od preferowanej godziny popołudniowej w wybranym dniu
                                                  later_start_day = tz.localize(datetime.datetime.combine(target_date, datetime.time(PREFERRED_WEEKDAY_START_HOUR, 0)))
                                                  search_start = max(later_start_day, now)
                                                  logging.info(f"      -> Szukanie 'specific_day_later' od {search_start}")

                                             elif pref == 'specific_day_earlier':
                                                 # Szukaj od początku dnia, ale nie później niż early_hour_limit
                                                 earlier_limit_day = tz.localize(datetime.datetime.combine(target_date, datetime.time(EARLY_HOUR_LIMIT, 0)))
                                                 search_start = max(target_datetime, now)
                                                 search_end = min(earlier_limit_day, tz.localize(datetime.datetime.combine(target_date, datetime.time(WORK_END_HOUR, 0))))
                                                 # Sprawdź czy zakres earlier ma sens
                                                 if search_start >= search_end or (search_end - search_start) < datetime.timedelta(minutes=APPOINTMENT_DURATION_MINUTES):
                                                      # Jeśli brak sensownego zakresu wcześniej tego dnia, szukaj normalnie od teraz
                                                      search_start = now
                                                      search_end = tz.localize(datetime.datetime.combine((now + datetime.timedelta(days=MAX_SEARCH_DAYS)).date(), datetime.time(WORK_END_HOUR, 0)))
                                                      msg_result = (msg_result + " Niestety, nie znalazłem nic sensownego wcześniej w tym dniu. Szukam dalej...") if msg_result else "Niestety, nie znalazłem nic sensownego wcześniej w tym dniu. Szukam dalej..."
                                                      logging.info(f"      -> Brak zakresu 'specific_day_earlier', szukanie 'any' od {search_start}")
                                                 else:
                                                    logging.info(f"      -> Szukanie 'specific_day_earlier' od {search_start} do {search_end}")


                                             else: # pref == 'specific_day'
                                                 # Szukaj od początku dnia pracy w wybranym dniu
                                                 search_start = max(target_datetime, now)
                                                 logging.info(f"      -> Szukanie 'specific_day' od {search_start}")

                                         except ValueError: # Nie udało się sparsować nazwy dnia
                                              logging.warning(f"      Nieznana nazwa dnia z feedbacku: {day}. Szukanie od teraz.")
                                              search_start = now # Powrót do szukania od teraz

                                    elif pref == 'any':
                                         # Szukaj od teraz (domyślnie)
                                         search_start = now
                                         logging.info(f"      -> Szukanie 'any' od {search_start}")

                                    else:
                                         logging.warning(f"      Nieznana preferencja z feedbacku: {pref}. Szukanie od teraz.")
                                         search_start = now # Domyślna akcja przy nieznanej preferencji


                                else: # Pierwsze szukanie terminu (po INTENT_SCHEDULE)
                                    logging.info(f"      -> Pierwsze szukanie terminu. Start od {search_start}.")
                                    search_start = now # Zawsze szukaj od teraz

                                # Określ koniec zakresu wyszukiwania (do MAX_SEARCH_DAYS w przyszłość, do końca dnia pracy)
                                # Jeśli już ustawiliśmy search_end w bloku 'earlier', używamy tego zakresu
                                if 'search_end' not in locals() or search_end is None:
                                    search_end = tz.localize(datetime.datetime.combine((search_start + datetime.timedelta(days=MAX_SEARCH_DAYS)).date(), datetime.time(WORK_END_HOUR, 0)))

                                # Pobierz wolne zakresy
                                if ENABLE_TYPING_DELAY:
                                    # Symuluj dłuższe myślenie AI podczas szukania w kalendarzu
                                    _simulate_typing(sender_id, MAX_TYPING_DELAY_SECONDS)

                                free_ranges = get_free_time_ranges(TARGET_CALENDAR_ID, search_start, search_end)

                                if free_ranges:
                                    logging.info(f"      Przekazanie {len(free_ranges)} zakresów do AI w celu wyboru slotu...")
                                    # Poproś AI o wybranie JEDNEGO terminu z dostępnych zakresów
                                    # Dodaj aktualną wiadomość użytkownika (feedback) do historii dla AI
                                    proposal_text, proposed_iso = get_gemini_slot_proposal(sender_id, history_for_gemini + ([user_content] if user_content else []), free_ranges)

                                    if proposal_text and proposed_iso:
                                        msg_result = (msg_result + "\n\n" + proposal_text) if msg_result else proposal_text
                                        # Zapisz zaproponowany slot w kontekście
                                        ctx_save = {'role': 'system', 'type': 'last_proposal', 'slot_iso': proposed_iso}
                                    else:
                                        # AI nie wygenerowało poprawnego slotu lub API zwróciło błąd
                                        msg_result = (msg_result + "\n\n" + (proposal_text if proposal_text else "Niestety, mam problem ze znalezieniem dogodnego terminu teraz.")) if msg_result else (proposal_text if proposal_text else "Niestety, mam problem ze znalezieniem dogodnego terminu teraz.")
                                        ctx_save = None # Reset kontekstu po błędzie AI

                                else:
                                    # Brak wolnych zakresów w danym okresie
                                    msg_result = (msg_result + "\n\n" + "Przepraszam, ale w najbliższych dniach nie mam wolnych terminów w godzinach pracy.") if msg_result else "Przepraszam, ale w najbliższych dniach nie mam wolnych terminów w godzinach pracy."
                                    ctx_save = None # Reset kontekstu po braku terminów

                            except Exception as find_err:
                                logging.error(f"BŁĄD ogólny podczas szukania/proponowania terminu: {find_err}", exc_info=True)
                                msg_result = (msg_result + "\n\n" + "Wystąpił problem podczas wyszukiwania terminów. Spróbuj ponownie później.") if msg_result else "Wystąpił problem podczas wyszukiwania terminów. Spróbuj ponownie później."
                                ctx_save = None # Reset kontekstu po błędzie szukania

                        elif action == 'send_gemini_response' or action == 'send_clarification' or action == 'send_error' or action == 'send_info':
                            # Wysyłanie odpowiedzi wygenerowanej przez AI lub domyślnej
                             pass # msg_result już ustawiony

                        # --- Wysyłanie wiadomości do użytkownika ---
                        if msg_result:
                             send_message(sender_id, msg_result)
                             model_resp = Content(role="model", parts=[Part.from_text(msg_result)])

                        # --- Zapis historii ---
                        # Zawsze dodajemy wiadomość użytkownika, jeśli była
                        # Dodajemy odpowiedź modelu, jeśli była
                        # Zapisujemy nowy kontekst, jeśli został wygenerowany
                        if user_content:
                            history_to_save = history + [user_content]
                            if model_resp:
                                history_to_save.append(model_resp)
                            save_history(sender_id, history_to_save, context_to_save=ctx_save)
                            history_saved = True # Zaznacz, że historia została już zapisana
                        elif ctx_save:
                             # Jeśli nie było user_content (np. postback), ale jest nowy kontekst do zapisania
                             # Wczytaj najnowszą historię i dodaj tylko nowy kontekst
                             latest_hist, _ = load_history(sender_id)
                             save_history(sender_id, latest_hist, context_to_save=ctx_save)
                             history_saved = True # Zaznacz, że historia została już zapisana


                    # --- Obsługa Postback (np. kliknięcie przycisku) ---
                    elif postback := event.get("postback"):
                         payload = postback.get("payload")
                         logging.info(f"    Otrzymano postback: {payload}")
                         user_content = Content(role="user", parts=[Part.from_text(f"[POSTBACK:{payload}]")]) # Zapisz w historii

                         # Przetwarzanie payloadu (np. przycisk akceptacji terminu)
                         if payload == "ACCEPT_SLOT":
                              # Akcja akceptacji, tylko jeśli jest aktywny kontekst z terminem
                              if is_context_active and last_iso:
                                  logging.info("      Postback: Akceptacja terminu.")
                                  action = 'book'
                                  msg_result = None # Komunikat zostanie wygenerowany przez funkcję book_appointment
                                  ctx_save = None # Zarezerwowano, więc reset kontekstu
                              else:
                                  # Nie ma aktywnego terminu do akceptacji
                                  logging.warning("      Postback: Akceptacja terminu, ale brak aktywnego kontekstu ISO.")
                                  msg_result = "Nie widzę aktywnej propozycji terminu do zaakceptowania."
                                  action = 'send_info'
                                  ctx_save = None
                         elif payload == "REJECT_SLOT":
                              # Akcja odrzucenia terminu, tylko jeśli jest aktywny kontekst
                              if is_context_active and last_iso:
                                   logging.info("      Postback: Odrzucenie terminu. Szukam dalej.")
                                   action = 'find_and_propose' # Odrzucenie bez specyficznych preferencji
                                   pref = 'any' # Domyślna preferencja przy odrzuceniu przyciskiem
                                   day = None
                                   hour = None
                                   msg_result = "Rozumiem, ten termin nie pasuje. Sprawdzam inne dostępne opcje." # Komunikat przed szukaniem
                                   ctx_save = None # Reset kontekstu starej propozycji
                              else:
                                   logging.warning("      Postback: Odrzucenie terminu, ale brak aktywnego kontekstu ISO.")
                                   msg_result = "Nie widzę aktywnej propozycji terminu do odrzucenia."
                                   action = 'send_info'
                                   ctx_save = None
                         else:
                              # Inny nieznany postback - potraktuj jak ogólne pytanie
                              logging.warning(f"      Nieznany payload postback: {payload}")
                              # Można przekazać do AI jako "[POSTBACK: Payload]"
                              user_input_simulated = f"[POSTBACK:{payload}]"
                              logging.info("      -> Gemini (normalna rozmowa - postback)...")
                              response = get_gemini_general_response(sender_id, user_input_simulated, history_for_gemini)
                              if response:
                                  if INTENT_SCHEDULE_MARKER in response:
                                      logging.info(f"      AI wykryło intencję [{INTENT_SCHEDULE_MARKER}] po postbacku.")
                                      action = 'find_and_propose'
                                      msg_result = response.split(INTENT_SCHEDULE_MARKER, 1)[0].strip()
                                      if not msg_result: msg_result = "Dobrze, sprawdzę dostępne terminy."
                                      pref = 'any'; day = None; hour = None
                                  else:
                                       action = 'send_gemini_response'
                                       msg_result = response
                              else:
                                   action = 'send_error'
                                   msg_result = "Przepraszam, wystąpił błąd podczas przetwarzania Twojego żądania."


                         # Wykonanie akcji dla postback
                         if action == 'book':
                             if last_iso:
                                 try: tz = _get_timezone(); start = datetime.datetime.fromisoformat(last_iso).astimezone(tz); end = start + datetime.timedelta(minutes=APPOINTMENT_DURATION_MINUTES); prof=get_user_profile(sender_id); name=prof.get('first_name','') if prof else ''; ok, msg=book_appointment(TARGET_CALENDAR_ID, start, end, "Rezerwacja FB", f"PSID:{sender_id}\nImię:{name}", name); msg_result=msg; ctx_save=None
                                 except Exception as e: logging.error(f"BŁĄD book (postback): {e}"); msg_result="Błąd rezerwacji."; ctx_save=None
                             else: logging.error("!!! PRÓBA REZERWACJI (postback) BEZ AKTYWNEGO KONTEKSTU ISO !!!"); msg_result="Nie mogę teraz zarezerwować. Problem z systemem."; ctx_save=None
                         elif action == 'find_and_propose':
                            # Logika szukania i proponowania terminu (taka sama jak przy feedbacku tekstowym)
                            try:
                                tz = _get_timezone()
                                now = datetime.datetime.now(tz)
                                search_start = now # Domyślny start (od teraz)

                                # Jeśli szukanie po odrzuceniu przyciskiem - szukaj od teraz (lub od końca ostatniego terminu)
                                if last_iso and is_context_active:
                                     last_dt = datetime.datetime.fromisoformat(last_iso).astimezone(tz)
                                     search_start = max(now, last_dt + datetime.timedelta(minutes=APPOINTMENT_DURATION_MINUTES))
                                     logging.info(f"      -> Szukanie 'any' (po odrzuceniu postback) od {search_start}")
                                else:
                                     # Jeśli szukanie po intencji wykrytej z postbacku - szukaj od teraz
                                     logging.info(f"      -> Pierwsze szukanie terminu (po postbacku). Start od {search_start}.")
                                     search_start = now

                                search_end = tz.localize(datetime.datetime.combine((search_start + datetime.timedelta(days=MAX_SEARCH_DAYS)).date(), datetime.time(WORK_END_HOUR, 0)))

                                if ENABLE_TYPING_DELAY:
                                     _simulate_typing(sender_id, MAX_TYPING_DELAY_SECONDS)

                                free_ranges = get_free_time_ranges(TARGET_CALENDAR_ID, search_start, search_end)

                                if free_ranges:
                                     logging.info(f"      Przekazanie {len(free_ranges)} zakresów do AI w celu wyboru slotu (postback)...")
                                     # Poproś AI o wybranie JEDNEGO terminu z dostępnych zakresów
                                     proposal_text, proposed_iso = get_gemini_slot_proposal(sender_id, history_for_gemini + ([user_content] if user_content else []), free_ranges)

                                     if proposal_text and proposed_iso:
                                         # Jeśli był już wstępny komunikat (np. "Szukam dalej...") dodaj propozycję po nim
                                         msg_result = (msg_result + "\n\n" + proposal_text) if msg_result else proposal_text
                                         ctx_save = {'role': 'system', 'type': 'last_proposal', 'slot_iso': proposed_iso}
                                     else:
                                          msg_result = (msg_result + "\n\n" + (proposal_text if proposal_text else "Niestety, mam problem ze znalezieniem dogodnego terminu teraz.")) if msg_result else (proposal_text if proposal_text else "Niestety, mam problem ze znalezieniem dogodnego terminu teraz.")
                                          ctx_save = None # Reset kontekstu po błędzie AI
                                else:
                                     msg_result = (msg_result + "\n\n" + "Przepraszam, ale w najbliższych dniach nie mam wolnych terminów w godzinach pracy.") if msg_result else "Przepraszam, ale w najbliższych dniach nie mam wolnych terminów w godzinach pracy."
                                     ctx_save = None # Reset kontekstu po braku terminów

                            except Exception as find_err_pb:
                                logging.error(f"BŁĄD ogólny podczas szukania/proponowania terminu (postback): {find_err_pb}", exc_info=True)
                                msg_result = (msg_result + "\n\n" + "Wystąpił problem podczas wyszukiwania terminów. Spróbuj ponownie później.") if msg_result else "Wystąpił problem podczas wyszukiwania terminów. Spróbuj ponownie później."
                                ctx_save = None # Reset kontekstu po błędzie szukania

                         elif action == 'send_gemini_response' or action == 'send_clarification' or action == 'send_error' or action == 'send_info':
                             pass # msg_result już ustawiony

                         # Wysyłanie wiadomości wynikowej (po postbacku)
                         if msg_result:
                             send_message(sender_id, msg_result)
                             model_resp = Content(role="model", parts=[Part.from_text(msg_result)])

                         # Zapis historii po postbacku
                         if user_content: # Zawsze zapisz postback w historii
                              history_to_save = history + [user_content]
                              if model_resp:
                                  history_to_save.append(model_resp)
                              save_history(sender_id, history_to_save, context_to_save=ctx_save)
                              history_saved = True
                         elif ctx_save: # Jeśli nie było user_content (tylko systemowa zmiana stanu)
                              latest_hist, _ = load_history(sender_id)
                              save_history(sender_id, latest_hist, context_to_save=ctx_save)
                              history_saved = True


                    # --- Obsługa Read i Delivery Receipts ---
                    elif event.get("read"):
                        logging.info(f"    Wiadomość odczytana przez użytkownika.")
                        pass # Można dodać logikę np. usuwania kontekstu, ale ostrożnie

                    elif event.get("delivery"):
                        pass # Potwierdzenie dostarczenia, zazwyczaj ignorowane

                    else:
                        # --- Nieobsługiwany typ zdarzenia ---
                        logging.warning(f"    Nieobsługiwany typ zdarzenia w 'messaging': {json.dumps(event)}")


            return Response("EVENT_RECEIVED", status=200)

        else:
            # Nie jest to zdarzenie typu "page"
            logging.warning(f"POST nie 'page': {data.get('object') if data else 'Brak'}")
            return Response("OK", status=200) # Zawsze zwracaj 200 OK dla FB

    except json.JSONDecodeError as e:
        logging.error(f"!!! BŁĄD JSON podczas parsowania POST: {e}\nDane: {raw_data[:500]}...")
        return Response("Invalid JSON", status=400) # Zwróć 400 na błędny JSON

    except Exception as e:
        # Ogólny błąd serwera podczas przetwarzania POST
        logging.error(f"!!! KRYTYCZNY BŁĄD podczas przetwarzania POST: {e}", exc_info=True)
        # Mimo błędu, warto zwrócić 200 OK, aby FB nie próbował wielokrotnie wysyłać tego samego zdarzenia.
        return Response("ERROR", status=200)


# --- Uruchomienie Serwera ---
if __name__ == '__main__':
    ensure_dir(HISTORY_DIR)

    port = int(os.environ.get("PORT", 8080))
    debug = os.environ.get("FLASK_DEBUG", "False").lower() in ("true", "1", "yes")

    print("\n" + "="*50 + "\n--- START KONFIGURACJI BOTA ---")
    print(f"  Weryfikacja TOKENA FB: {'OK' if VERIFY_TOKEN != 'KOLAGEN' else 'Użyty domyślny (KOLAGEN)'}")
    if not PAGE_ACCESS_TOKEN:
        print("\n!!! KRYTYCZNE: FB_PAGE_ACCESS_TOKEN PUSTY!\n")
    elif len(PAGE_ACCESS_TOKEN) < 50:
        print("\n!!! KRYTYCZNE: FB_PAGE_ACCESS_TOKEN ZBYT KRÓTKI!\n")
    else:
        print("  FB_PAGE_ACCESS_TOKEN: Ustawiony (OK)")
        if PAGE_ACCESS_TOKEN=="EACNAHFzEhkUBO4ypcoyQfWIgNc0YLZA1aCr9n3BzpvSJLoBTJnv5rWZBmc7HlqF6uUW1uAp6aDZB8ZAb0RRT45lVIfGnciQX6wBKrZColGARfVLXP5Ic6Ptrj5AUvom4Rt12hyBxcjIJGes76fvdvBhiBZCJ0ZCVfkQMZBZCBatJshSZA8hFuRyKd58b50wkhVCMZCuwZDZD":
             print("\n!!! UWAGA: Używany DOMYŚLNY PAGE_ACCESS_TOKEN!\n")


    print(f"  Katalog historii: {HISTORY_DIR}")
    print(f"  Maksymalna historia AI: {MAX_HISTORY_TURNS} par wiad.")
    print(f"  Limit znaków wiadomości FB: {MESSAGE_CHAR_LIMIT}")
    print(f"  Opóźnienie między fragm. wiad.: {MESSAGE_DELAY_SECONDS}s")
    print(f"  Symulacja pisania: {'On' if ENABLE_TYPING_DELAY else 'Off'}")
    if ENABLE_TYPING_DELAY:
        print(f"    Min. opóźnienie pisania: {MIN_TYPING_DELAY_SECONDS}s")
        print(f"    Max. opóźnienie pisania: {MAX_TYPING_DELAY_SECONDS}s")
        print(f"    Znaki/sek. dla pisania: {TYPING_CHARS_PER_SECOND}")

    print(f"  Projekt Vertex AI: {PROJECT_ID}")
    print(f"  Lokalizacja Vertex AI: {LOCATION}")
    print(f"  Model Vertex AI: {MODEL_ID}")
    if not gemini_model:
        print("\n!!! OSTRZ.: Model Gemini NIE załadowany podczas startu!\n")
    else:
        print(f"  Model Gemini AI ({MODEL_ID}): OK")

    print(f"  Identyfikator kalendarza: {TARGET_CALENDAR_ID}")
    print(f"  Strefa czasowa kalendarza: {CALENDAR_TIMEZONE}")
    print(f"  Czas trwania wizyty: {APPOINTMENT_DURATION_MINUTES} min")
    print(f"  Godziny pracy: {WORK_START_HOUR}:00 - {WORK_END_HOUR}:00")
    print(f"  Preferowana godzina popołudniowa: {PREFERRED_WEEKDAY_START_HOUR}:00")
    print(f"  Preferowana godzina weekend: {PREFERRED_WEEKEND_START_HOUR}:00")
    print(f"  Maksymalny zakres szukania: {MAX_SEARCH_DAYS} dni")
    print(f"  Górna granica dla 'wcześniej': {EARLY_HOUR_LIMIT}:00")
    print(f"  Plik klucza Calendar API: {SERVICE_ACCOUNT_FILE} ({'Znaleziono' if os.path.exists(SERVICE_ACCOUNT_FILE) else 'BRAK!!!'})")

    cal_service = get_calendar_service()
    if not cal_service and os.path.exists(SERVICE_ACCOUNT_FILE):
         print("\n!!! OSTRZ.: Usługa Google Calendar NIE zainicjowana poprawnie podczas startu.\n")
    elif cal_service:
         print("  Usługa Google Calendar: OK")


    print("--- KONIEC KONFIGURACJI BOTA ---\n" + "="*50 + "\n")

    print(f"Start serwera Flask: port={port}, debug={debug}...")

    # Ustaw poziom logowania dla głośnych bibliotek
    logging.getLogger('googleapiclient.discovery_cache').setLevel(logging.ERROR)
    # Ustaw poziom logowania dla własnych logów
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
    if debug:
        logging.getLogger().setLevel(logging.DEBUG)
        print("Logowanie DEBUG włączone.")

    if not debug:
        try:
            from waitress import serve
            print("Start Waitress...")
            serve(app, host='0.0.0.0', port=port)
        except ImportError:
            print("Waitress brak. Start serwera developmentowego.")
            app.run(host='0.0.0.0', port=port, debug=False)
    else:
        print("Start serwera developmentowego w trybie DEBUG...")
        app.run(host='0.0.0.0', port=port, debug=True)
