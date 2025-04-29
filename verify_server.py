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

# --- Importy Google Calendar ---
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
# ------------------------------

app = Flask(__name__)

# --- Konfiguracja Ogólna ---
VERIFY_TOKEN = os.environ.get("FB_VERIFY_TOKEN", "KOLAGEN") # Zmień na swój token weryfikacyjny
PAGE_ACCESS_TOKEN = os.environ.get("FB_PAGE_ACCESS_TOKEN", "YOUR_FACEBOOK_PAGE_ACCESS_TOKEN") # WAŻNE: Podaj swój prawdziwy token!
PROJECT_ID = os.environ.get("GCP_PROJECT_ID", "linear-booth-450221-k1") # Zmień na swój Project ID
LOCATION = os.environ.get("GCP_LOCATION", "us-central1") # Zmień na swoją lokalizację
MODEL_ID = os.environ.get("VERTEX_MODEL_ID", "gemini-1.5-flash-001") # Lub inny wspierany model Gemini

FACEBOOK_GRAPH_API_URL = f"https://graph.facebook.com/v19.0/me/messages"

HISTORY_DIR = "conversation_store"
MAX_HISTORY_TURNS = 15 # Liczba tur (user+model) do trzymania w pamięci
MESSAGE_CHAR_LIMIT = 1990 # Limit znaków FB Messengera
MESSAGE_DELAY_SECONDS = 1.5 # Opóźnienie między fragmentami długiej wiadomości

ENABLE_TYPING_DELAY = True
MIN_TYPING_DELAY_SECONDS = 0.8
MAX_TYPING_DELAY_SECONDS = 3.5
TYPING_CHARS_PER_SECOND = 30

# --- Konfiguracja Kalendarza ---
SERVICE_ACCOUNT_FILE = 'kalendarzklucz.json' # Upewnij się, że ten plik istnieje i jest dostępny
CALENDAR_SCOPES = ['https://www.googleapis.com/auth/calendar.readonly', 'https://www.googleapis.com/auth/calendar.events']
CALENDAR_TIMEZONE = 'Europe/Warsaw'
APPOINTMENT_DURATION_MINUTES = 60
WORK_START_HOUR = 7
WORK_END_HOUR = 22
TARGET_CALENDAR_ID = 'f19e189826b9d6e36950da347ac84d5501ecbd6bed0d76c8641be61a67749c67@group.calendar.google.com' # Zmień na ID Twojego kalendarza
PREFERRED_WEEKDAY_START_HOUR = 16 # Preferowana godzina startu w dni robocze (pomocne dla AI przy wyborze "rozsądnego" slotu)
PREFERRED_WEEKEND_START_HOUR = 10 # Preferowana godzina startu w weekendy
MAX_SEARCH_DAYS = 14 # Jak daleko w przyszłość szukać slotów
MAX_SLOTS_FOR_AI = 15 # Ile max slotów przekazać AI do wyboru

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

# Konfiguracja logowania
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
    if not PAGE_ACCESS_TOKEN or len(PAGE_ACCESS_TOKEN) < 50:
        logging.warning(f"[{psid}] Brak/nieprawidłowy PAGE_ACCESS_TOKEN. Profil niepobrany.")
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
            try: logging.error(f"Odpowiedź FB (błąd HTTP): {http_err.response.json()}")
            except json.JSONDecodeError: logging.error(f"Odpowiedź FB (błąd HTTP, nie JSON): {http_err.response.text}")
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
            logging.error(f"BŁĄD [{user_psid}]: Plik historii nie zawiera listy.")
            return [], {}

        # Znajdź ostatni wpis systemowy (kontekst)
        last_system_entry_index = -1
        for i in range(len(history_data) - 1, -1, -1):
            entry = history_data[i]
            if isinstance(entry, dict) and entry.get('role') == 'system' and entry.get('type') == 'last_proposal':
                last_system_entry_index = i
                break

        # Przetwarzaj wiadomości użytkownika i modelu
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
            elif i == last_system_entry_index: # Jeśli to ostatni wpis systemowy
                if 'slot_iso' in msg_data:
                    context['last_proposed_slot_iso'] = msg_data['slot_iso']
                    context['message_index'] = i # Zapisz indeks, żeby sprawdzić, czy jest aktualny
                    logging.info(f"[{user_psid}] Odczytano AKTUALNY kontekst: last_proposed_slot_iso (na końcu historii)")
                else:
                    logging.warning(f"Ostrz. [{user_psid}]: Poprawny wpis systemowy, ale brak 'slot_iso' (idx {i})")
            elif isinstance(msg_data, dict) and msg_data.get('role') == 'system':
                 logging.info(f"[{user_psid}] Pominięto stary kontekst systemowy na indeksie {i}")
            else:
                logging.warning(f"Ostrz. [{user_psid}]: Pominięto niepoprawny wpis w historii (idx {i}): {msg_data}")

        logging.info(f"[{user_psid}] Wczytano historię: {len(history)} wiadomości (user/model).")
        return history, context

    except (json.JSONDecodeError, KeyError, IndexError, TypeError) as e:
        logging.error(f"BŁĄD [{user_psid}] parsowania historii: {e}.", exc_info=True)
        # Próba odzyskania części historii lub zwrócenie pustej
        # W tym przypadku po prostu zwracamy pustą
        return [], {}
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
        # Przycinanie historii do zapisu
        max_messages_to_save = MAX_HISTORY_TURNS * 2 # user + model
        start_index = max(0, len(history) - max_messages_to_save)
        history_to_save = history[start_index:]
        if len(history) > max_messages_to_save:
            logging.info(f"[{user_psid}] Historia przycięta DO ZAPISU: {len(history_to_save)} wiadomości (z {len(history)}).")

        # Konwersja obiektów Content do formatu JSON
        for msg in history_to_save:
             if isinstance(msg, Content) and hasattr(msg, 'role') and msg.role in ('user', 'model') and hasattr(msg, 'parts') and isinstance(msg.parts, list):
                parts_data = [{'text': part.text} for part in msg.parts if isinstance(part, Part) and hasattr(part, 'text')]
                if parts_data:
                    history_data.append({'role': msg.role, 'parts': parts_data})
             else:
                 logging.warning(f"Ostrz. [{user_psid}]: Pomijanie nieprawidłowego obiektu Content podczas zapisu: {type(msg)}")

        # Dodanie kontekstu na końcu, jeśli jest
        if context_to_save and isinstance(context_to_save, dict):
             history_data.append(context_to_save)
             logging.info(f"[{user_psid}] Dodano kontekst {context_to_save.get('type')} do zapisu.")

        # Atomowy zapis do pliku tymczasowego, a następnie zamiana
        with open(temp_filepath, 'w', encoding='utf-8') as f:
            json.dump(history_data, f, ensure_ascii=False, indent=2)
        os.replace(temp_filepath, filepath)
        logging.info(f"[{user_psid}] Zapisano historię/kontekst ({len(history_data)} wpisów) do: {filepath}")

    except Exception as e:
        logging.error(f"BŁĄD [{user_psid}] zapisu historii/kontekstu: {e}", exc_info=True)
        # Próba usunięcia pliku tymczasowego, jeśli istnieje
        if os.path.exists(temp_filepath):
            try:
                os.remove(temp_filepath)
                logging.info(f"    Usunięto plik tymczasowy {temp_filepath} po błędzie zapisu.")
            except OSError as remove_e:
                logging.error(f"    Nie można usunąć pliku tymczasowego {temp_filepath} po błędzie zapisu: {remove_e}")

def _get_timezone():
    """Pobiera i cachuje obiekt strefy czasowej."""
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
    """Pobiera i cachuje obiekt usługi Google Calendar API."""
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
    """Parsuje datę/czas z danych wydarzenia Google Calendar, uwzględniając strefę czasową."""
    if not event_time_data:
        return None
    if 'dateTime' in event_time_data:
        dt_str = event_time_data['dateTime']
        try:
            # Próba sparsowania z różnymi formatami offsetu
            dt = datetime.datetime.fromisoformat(dt_str.replace('Z', '+00:00'))
        except ValueError:
            logging.warning(f"Ostrz.: Nie udało się sparsować dateTime: {dt_str}")
            return None

        # Upewnienie się, że obiekt ma świadomość strefy czasowej i konwersja do domyślnej
        if dt.tzinfo is None:
            logging.warning(f"Ostrz.: dateTime {dt_str} nie ma informacji o strefie. Zakładam UTC.")
            dt = pytz.utc.localize(dt)
        return dt.astimezone(default_tz)

    elif 'date' in event_time_data:
        # Dla wydarzeń całodniowych zwracamy obiekt date
        try:
            return datetime.date.fromisoformat(event_time_data['date'])
        except ValueError:
            logging.warning(f"Ostrz.: Nie udało się sparsować date: {event_time_data['date']}")
            return None
    return None


def get_free_slots(calendar_id, start_datetime, end_datetime):
    """Znajduje wolne sloty w kalendarzu Google w podanym zakresie."""
    service = get_calendar_service()
    tz = _get_timezone()
    if not service:
        logging.error("Błąd: Usługa kalendarza niedostępna w get_free_slots.")
        return []

    # Upewnij się, że daty początkowa i końcowa są świadome strefy czasowej
    if start_datetime.tzinfo is None: start_datetime = tz.localize(start_datetime)
    else: start_datetime = start_datetime.astimezone(tz)
    if end_datetime.tzinfo is None: end_datetime = tz.localize(end_datetime)
    else: end_datetime = end_datetime.astimezone(tz)

    logging.info(f"Szukanie wolnych slotów ({APPOINTMENT_DURATION_MINUTES} min) w '{calendar_id}'")
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

    free_slots_starts = []
    current_day = start_datetime.date()
    end_day = end_datetime.date()
    appointment_duration = datetime.timedelta(minutes=APPOINTMENT_DURATION_MINUTES)

    while current_day <= end_day:
        # Granice dnia roboczego w lokalnej strefie czasowej
        day_start_limit = tz.localize(datetime.datetime.combine(current_day, datetime.time(WORK_START_HOUR, 0)))
        day_end_limit = tz.localize(datetime.datetime.combine(current_day, datetime.time(WORK_END_HOUR, 0)))

        # Zakres sprawdzania dla bieżącego dnia (ograniczony przez globalny start/end i godziny pracy)
        check_start_time = max(start_datetime, day_start_limit)
        check_end_time = min(end_datetime, day_end_limit)

        # Jeśli zakres sprawdzania jest nieprawidłowy (np. start jest po końcu), przejdź do następnego dnia
        if check_start_time >= check_end_time:
            current_day += datetime.timedelta(days=1)
            continue

        # Wyodrębnij i posortuj zajęte przedziały czasowe dla bieżącego dnia
        busy_intervals = []
        for event in events:
            start = parse_event_time(event.get('start'), tz)
            end = parse_event_time(event.get('end'), tz)

            # Obsługa wydarzeń całodniowych - blokują cały dzień roboczy
            if isinstance(start, datetime.date):
                if start == current_day:
                    busy_intervals.append({'start': day_start_limit, 'end': day_end_limit})
                    logging.debug(f"  Dzień {current_day} zablokowany przez wydarzenie całodniowe: {event.get('summary', 'N/A')}")
            elif isinstance(start, datetime.datetime) and isinstance(end, datetime.datetime):
                # Interesują nas tylko wydarzenia, które nachodzą na sprawdzany zakres dnia roboczego
                if end > check_start_time and start < check_end_time:
                    # Ogranicz przedział zajętości do granic sprawdzanego dnia
                    effective_start = max(start, check_start_time)
                    effective_end = min(end, check_end_time)
                    if effective_start < effective_end: # Upewnij się, że przedział ma sens
                        busy_intervals.append({'start': effective_start, 'end': effective_end})
                        logging.debug(f"  Zajęty przedział: {effective_start.strftime('%H:%M')} - {effective_end.strftime('%H:%M')} ({event.get('summary', 'N/A')})")
            else:
                 logging.warning(f"Ostrz.: Wydarzenie '{event.get('summary','?')}' ma nieprawidłowe czasy rozpoczęcia/zakończenia ({type(start)}, {type(end)})")

        # Scal nachodzące na siebie zajęte przedziały
        if not busy_intervals:
            merged_busy_times = []
        else:
            busy_intervals.sort(key=lambda x: x['start'])
            merged_busy_times = [busy_intervals[0]]
            for current_busy in busy_intervals[1:]:
                last_merged = merged_busy_times[-1]
                # Jeśli bieżący przedział zaczyna się przed lub w momencie końca ostatniego scalonego, scal je
                if current_busy['start'] <= last_merged['end']:
                    last_merged['end'] = max(last_merged['end'], current_busy['end'])
                else:
                    merged_busy_times.append(current_busy)
            logging.debug(f"  Scalone zajęte przedziały dla {current_day}: {[{'s': t['start'].strftime('%H:%M'), 'e': t['end'].strftime('%H:%M')} for t in merged_busy_times]}")

        # Sprawdź wolne miejsca między zajętymi przedziałami (i przed pierwszym/po ostatnim)
        potential_slot_start = check_start_time
        for busy in merged_busy_times:
            busy_start = busy['start']
            busy_end = busy['end']
            # Sprawdź wolne miejsca przed bieżącym zajętym przedziałem
            while potential_slot_start + appointment_duration <= busy_start:
                # Dodajemy tylko sloty zaczynające się o pełnych 10 minutach
                if potential_slot_start.minute % 10 == 0:
                     # Upewnijmy się, że slot jest w granicach dnia roboczego
                    if potential_slot_start >= day_start_limit and potential_slot_start + appointment_duration <= day_end_limit:
                        free_slots_starts.append(potential_slot_start)
                        logging.debug(f"  + Wolny slot znaleziony (przed zajętym): {potential_slot_start.strftime('%Y-%m-%d %H:%M')}")

                # Przesuń potencjalny start do następnej wielokrotności 10 minut
                current_minute = potential_slot_start.minute
                minutes_to_add = 10 - (current_minute % 10) if current_minute % 10 != 0 else 10
                potential_slot_start += datetime.timedelta(minutes=minutes_to_add)
                potential_slot_start = potential_slot_start.replace(second=0, microsecond=0)

            # Przesuń potencjalny start za bieżący zajęty przedział
            potential_slot_start = max(potential_slot_start, busy_end)

        # Sprawdź wolne miejsca po ostatnim zajętym przedziale (lub przez cały dzień, jeśli nie było zajętych)
        while potential_slot_start + appointment_duration <= check_end_time:
             if potential_slot_start.minute % 10 == 0:
                # Upewnijmy się, że slot jest w granicach dnia roboczego
                if potential_slot_start >= day_start_limit and potential_slot_start + appointment_duration <= day_end_limit:
                    free_slots_starts.append(potential_slot_start)
                    logging.debug(f"  + Wolny slot znaleziony (po zajętych/cały dzień): {potential_slot_start.strftime('%Y-%m-%d %H:%M')}")

             current_minute = potential_slot_start.minute
             minutes_to_add = 10 - (current_minute % 10) if current_minute % 10 != 0 else 10
             potential_slot_start += datetime.timedelta(minutes=minutes_to_add)
             potential_slot_start = potential_slot_start.replace(second=0, microsecond=0)

        # Przejdź do następnego dnia
        current_day += datetime.timedelta(days=1)

    # Usuń duplikaty (choć logika powinna je minimalizować), posortuj i zwróć
    final_slots = sorted(list(set(slot for slot in free_slots_starts if start_datetime <= slot < end_datetime)))
    logging.info(f"Znaleziono {len(final_slots)} unikalnych wolnych slotów.")
    return final_slots


def book_appointment(calendar_id, start_time, end_time, summary="Rezerwacja wizyty", description="", user_name=""):
    """Rezerwuje wizytę w kalendarzu Google."""
    service = get_calendar_service()
    tz = _get_timezone()
    if not service:
        return False, "Błąd: Brak połączenia z usługą kalendarza."

    # Upewnij się, że czasy są świadome strefy czasowej
    if start_time.tzinfo is None: start_time = tz.localize(start_time)
    else: start_time = start_time.astimezone(tz)
    if end_time.tzinfo is None: end_time = tz.localize(end_time)
    else: end_time = end_time.astimezone(tz)

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
        'reminders': { # Opcjonalne: dodaj przypomnienie
            'useDefault': False,
            'overrides': [
                {'method': 'popup', 'minutes': 60}, # Przypomnienie 60 minut przed
            ],
        },
    }

    try:
        logging.info(f"Próba rezerwacji: '{event_summary}' od {start_time.strftime('%Y-%m-%d %H:%M')} do {end_time.strftime('%Y-%m-%d %H:%M')} w kalendarzu {calendar_id}")
        created_event = service.events().insert(calendarId=calendar_id, body=event).execute()
        event_id = created_event.get('id')
        logging.info(f"Rezerwacja zakończona sukcesem. ID wydarzenia: {event_id}")

        # Przygotuj wiadomość potwierdzającą dla użytkownika
        day_index = start_time.weekday()
        locale_day_name = POLISH_WEEKDAYS[day_index]
        # Użyj formatowania bez wiodącego zera dla godziny, jeśli to preferowane
        hour_str = start_time.strftime('%#H') if os.name != 'nt' else start_time.strftime('%#H') # %#H dla niektórych systemów
        try:
             hour_str = str(start_time.hour) # Bezpieczniejsza opcja
        except: pass # Ignoruj błąd formatowania godziny

        confirm_message = f"Świetnie! Termin na {locale_day_name}, {start_time.strftime(f'%d.%m.%Y o {hour_str}:%M')} został zarezerwowany."
        return True, confirm_message

    except HttpError as error:
        error_details = f"Kod: {error.resp.status}, Powód: {error.resp.reason}"
        try:
            error_json = json.loads(error.content.decode('utf-8'))
            error_message = error_json.get('error', {}).get('message', '')
            if error_message:
                error_details += f" - {error_message}"
        except (json.JSONDecodeError, AttributeError):
            pass # Ignoruj, jeśli nie można sparsować błędu JSON
        logging.error(f"Błąd API podczas rezerwacji: {error}, Szczegóły: {error_details}", exc_info=True)

        if error.resp.status == 409: # Conflict - termin prawdopodobnie zajęty
            return False, "Niestety, ten termin został właśnie zajęty. Czy chcesz spróbować znaleźć inny?"
        elif error.resp.status == 403: # Forbidden
            return False, f"Brak uprawnień do zapisu w kalendarzu '{calendar_id}'. Skontaktuj się z administratorem."
        elif error.resp.status == 404: # Not Found
            return False, f"Nie znaleziono kalendarza o ID '{calendar_id}'. Sprawdź konfigurację."
        else:
            return False, f"Wystąpił nieoczekiwany błąd podczas rezerwacji (kod: {error.resp.status}). Spróbuj ponownie później."
    except Exception as e:
        logging.error(f"Nieoczekiwany błąd Python podczas rezerwacji: {e}", exc_info=True)
        return False, "Wystąpił wewnętrzny błąd systemu rezerwacji. Przepraszamy za utrudnienia."


def format_slots_for_ai(slots):
    """Formatuje listę slotów (datetime) na czytelny tekst dla AI, zawierający ISO string."""
    if not slots:
        return "Brak dostępnych terminów w najbliższym czasie."

    formatted_list = ["Oto kilka dostępnych terminów (każdy w formacie [SLOT_ISO:ISODATA] Dzień, DD.MM.RRRR o GG:MM):"]
    for slot in slots:
        iso_str = slot.isoformat()
        day_name = POLISH_WEEKDAYS[slot.weekday()]
        hour_str = str(slot.hour) # Formatowanie godziny bez wiodącego zera
        readable_part = f"{day_name}, {slot.strftime(f'%d.%m.%Y o {hour_str}:%M')}"
        formatted_list.append(f"- {SLOT_ISO_MARKER_PREFIX}{iso_str}{SLOT_ISO_MARKER_SUFFIX} {readable_part}")

    return "\n".join(formatted_list)

def format_slot_for_user(slot_start):
    """Formatuje pojedynczy slot (datetime) na czytelny tekst dla użytkownika."""
    if not isinstance(slot_start, datetime.datetime):
        return ""
    try:
        day_index = slot_start.weekday()
        day_name = POLISH_WEEKDAYS[day_index]
        hour_str = str(slot_start.hour) # Formatowanie godziny bez wiodącego zera
        return f"{day_name}, {slot_start.strftime(f'%d.%m.%Y o {hour_str}:%M')}"
    except Exception as e:
        logging.error(f"Błąd formatowania slotu dla użytkownika: {e}", exc_info=True)
        # Fallback do ISO formatu
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
    logging.critical(f"KRYTYCZNY BŁĄD podczas inicjalizacji Vertex AI lub ładowania modelu: {e}", exc_info=True)
    # Aplikacja może nadal działać, ale funkcje AI nie będą dostępne
    # Można rozważyć zakończenie działania aplikacji tutaj: raise SystemExit(...)

# Konfiguracja generowania i bezpieczeństwa dla AI
# Można dostosować wartości w zależności od potrzeb
GENERATION_CONFIG_DEFAULT = GenerationConfig(
    temperature=0.7, # Większa kreatywność w rozmowie
    top_p=0.95,
    top_k=40,
    max_output_tokens=1024 # Limit tokenów odpowiedzi
)
GENERATION_CONFIG_PROPOSAL = GenerationConfig(
    temperature=0.4, # Mniejsza losowość przy wyborze slotu
    top_p=0.95,
    top_k=40,
    max_output_tokens=512
)
GENERATION_CONFIG_FEEDBACK = GenerationConfig(
    temperature=0.1, # Bardzo niska losowość dla precyzyjnej klasyfikacji
    top_p=0.95,
    top_k=40,
    max_output_tokens=100 # Krótka odpowiedź (znacznik)
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
    """Wysyła pojedynczy fragment wiadomości przez Facebook Graph API."""
    logging.info(f"--- Wysyłanie fragm. do {recipient_id} (dł: {len(message_text)}) ---")
    params = {"access_token": PAGE_ACCESS_TOKEN}
    payload = {
        "recipient": {"id": recipient_id},
        "message": {"text": message_text},
        "messaging_type": "RESPONSE" # Ważne dla odpowiedzi na wiadomości użytkownika
    }

    if not PAGE_ACCESS_TOKEN or len(PAGE_ACCESS_TOKEN) < 50:
        logging.error(f"!!! [{recipient_id}] Brak lub nieprawidłowy PAGE_ACCESS_TOKEN. Wiadomość NIE wysłana.")
        return False

    try:
        r = requests.post(FACEBOOK_GRAPH_API_URL, params=params, json=payload, timeout=30)
        r.raise_for_status() # Rzuci wyjątek dla odpowiedzi 4xx/5xx
        response_json = r.json()
        if 'error' in response_json:
            logging.error(f"!!! BŁĄD FB API podczas wysyłania do {recipient_id}: {response_json['error']}")
            return False
        logging.info(f"--- Fragment wysłany pomyślnie do {recipient_id} ---")
        return True
    except requests.exceptions.Timeout:
        logging.error(f"!!! BŁĄD TIMEOUT podczas wysyłania do {recipient_id}")
        return False
    except requests.exceptions.HTTPError as http_err:
        logging.error(f"!!! BŁĄD HTTP {http_err.response.status_code} podczas wysyłania do {recipient_id}: {http_err}")
        if http_err.response is not None:
            try: logging.error(f"Odpowiedź FB (błąd HTTP): {http_err.response.json()}")
            except json.JSONDecodeError: logging.error(f"Odpowiedź FB (błąd HTTP, nie JSON): {http_err.response.text}")
        return False
    except requests.exceptions.RequestException as req_err:
        logging.error(f"!!! BŁĄD RequestException podczas wysyłania do {recipient_id}: {req_err}", exc_info=True)
        return False
    except Exception as e:
        logging.error(f"!!! Niespodziewany BŁĄD podczas wysyłania do {recipient_id}: {e}", exc_info=True)
        return False

def send_message(recipient_id, full_message_text):
    """Wysyła wiadomość do użytkownika, dzieląc ją na fragmenty w razie potrzeby."""
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
                chunks.append(remaining_text.strip())
                break

            split_index = -1
            # Szukaj najlepszego miejsca do podziału od tyłu
            # Priorytet: podwójny enter > pojedynczy enter > kropka > wykrzyknik > znak zapytania > spacja
            for delimiter in ['\n\n', '\n', '. ', '! ', '? ', ' ']:
                 # Wyszukaj w zakresie do limitu znaków (+ margines na delimiter)
                search_limit = MESSAGE_CHAR_LIMIT - (len(delimiter) - 1) if len(delimiter) > 1 else MESSAGE_CHAR_LIMIT
                temp_index = remaining_text.rfind(delimiter, 0, search_limit + len(delimiter))

                # Sprawdź czy znaleziony indeks jest w dozwolonym zakresie podziału
                if temp_index != -1 and temp_index <= MESSAGE_CHAR_LIMIT :
                    split_index = temp_index + len(delimiter) # Podziel ZA delimiterem
                    break

            if split_index == -1:
                # Jeśli nie znaleziono dobrego miejsca, twardo utnij po limicie
                split_index = MESSAGE_CHAR_LIMIT
                logging.warning(f"[{recipient_id}] Nie znaleziono naturalnego miejsca podziału, cięcie na {MESSAGE_CHAR_LIMIT} znakach.")

            chunk = remaining_text[:split_index].strip()
            if chunk: # Upewnij się, że fragment nie jest pusty
                chunks.append(chunk)
            remaining_text = remaining_text[split_index:].strip()

        num_chunks = len(chunks)
        logging.info(f"[{recipient_id}] Podzielono na {num_chunks} fragmentów.")
        send_success_count = 0
        for i, chunk in enumerate(chunks):
            logging.info(f"[{recipient_id}] Wysyłanie fragm. {i+1}/{num_chunks} (dł: {len(chunk)})...")
            if not _send_single_message(recipient_id, chunk):
                logging.error(f"!!! [{recipient_id}] Anulowano wysyłanie reszty wiadomości po błędzie na fragmencie {i+1}.")
                break # Przestań wysyłać kolejne fragmenty, jeśli jeden się nie udał
            send_success_count += 1
            if i < num_chunks - 1:
                logging.info(f"[{recipient_id}] Oczekiwanie {MESSAGE_DELAY_SECONDS}s przed następnym fragmentem...")
                time.sleep(MESSAGE_DELAY_SECONDS)

        logging.info(f"--- [{recipient_id}] Zakończono wysyłanie {send_success_count}/{num_chunks} fragmentów. ---")


# =====================================================================
# === INSTRUKCJE SYSTEMOWE DLA AI =====================================
# =====================================================================

# --- INSTRUKCJA DLA GŁÓWNEGO AI (ROZMOWA + WYKRYWANIE INTENCJI) ---
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

# --- INSTRUKCJA DLA AI WYBIERAJĄCEGO I PROPONUJĄCEGO TERMIN ---
SYSTEM_INSTRUCTION_PROPOSE = f"""Jesteś asystentem AI specjalizującym się w proponowaniu terminów spotkań dla 'Zakręcone Korepetycje'. Twoim zadaniem jest wybranie **jednego**, najbardziej odpowiedniego terminu z dostarczonej listy i zaproponowanie go użytkownikowi.

**Kontekst:** Użytkownik wyraził chęć umówienia pierwszej lekcji próbnej (płatnej). Otrzymałeś listę dostępnych terminów.

**Dostępne terminy:**
{{available_slots_text}}

**Twoje zadanie:**
1.  Przeanalizuj historię rozmowy (jeśli dostępna) pod kątem ewentualnych preferencji użytkownika (np. "popołudniu", "wtorek", "po 16"). Uwzględnij te preferencje przy wyborze.
2.  Jeśli brak wyraźnych preferencji w historii, wybierz termin, który wydaje się "rozsądny":
    *   W dni robocze (Pon-Pt): preferuj godziny popołudniowe (od {PREFERRED_WEEKDAY_START_HOUR}:00).
    *   W weekendy (Sob-Nd): preferuj godziny od {PREFERRED_WEEKEND_START_HOUR}:00.
    *   Jeśli to możliwe, wybierz termin nie w najbliższych kilku godzinach, dając użytkownikowi czas na przygotowanie.
3.  Wybierz **tylko jeden** termin z powyższej listy "Dostępne terminy".
4.  Sformułuj **krótką, uprzejmą i naturalną propozycję** wybranego terminu, pytając użytkownika o akceptację. Użyj polskiego formatu daty i dnia tygodnia.
5.  **ABSOLUTNIE KLUCZOWE:** W swojej odpowiedzi **musisz** zawrzeć identyfikator ISO wybranego terminu w specjalnym znaczniku `{SLOT_ISO_MARKER_PREFIX}TWOJ_WYBRANY_ISO_STRING{SLOT_ISO_MARKER_SUFFIX}`. Znacznik ten musi być częścią odpowiedzi.

**Przykład dobrej odpowiedzi (jeśli wybrałeś termin z ISO '2025-05-06T16:00:00+02:00'):**
"Znalazłem dla Pana/Pani taki termin: Wtorek, 06.05.2025 o 16:00. Czy taki termin by odpowiadał? {SLOT_ISO_MARKER_PREFIX}2025-05-06T16:00:00+02:00{SLOT_ISO_MARKER_SUFFIX}"
Lub:
"Proponuję termin: Piątek, 09.05.2025 o 17:30. {SLOT_ISO_MARKER_PREFIX}2025-05-09T17:30:00+02:00{SLOT_ISO_MARKER_SUFFIX} Pasuje?"

**Zasady:**
*   Odpowiadaj po polsku.
*   Bądź zwięzły i profesjonalny.
*   **Nie proponuj** terminów spoza dostarczonej listy.
*   **Zawsze** dołączaj znacznik `{SLOT_ISO_MARKER_PREFIX}...{SLOT_ISO_MARKER_SUFFIX}` z poprawnym ISO stringiem wybranego terminu.
*   Nie dodawaj żadnych innych informacji (np. o cenniku), skup się tylko na propozycji terminu.
"""

# --- INSTRUKCJA DLA AI INTERPRETUJĄCEGO ODPOWIEDŹ NA PROPOZYCJĘ ---
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

def _call_gemini(user_psid, prompt_content, generation_config, model_purpose=""):
    """Wewnętrzna funkcja do wywoływania modelu Gemini."""
    if not gemini_model:
        logging.error(f"!!! [{user_psid}] Model Gemini ({MODEL_ID}) nie jest załadowany! Nie można wykonać wywołania ({model_purpose}).")
        return None
    if not prompt_content:
        logging.warning(f"[{user_psid}] Pusty prompt przekazany do Gemini ({model_purpose}).")
        return None

    logging.info(f"\n--- [{user_psid}] Wywołanie Gemini ({MODEL_ID}) - Cel: {model_purpose} ---")
    # Opcjonalne logowanie pełnego promptu (może być bardzo długie)
    # logging.debug(f"Pełny prompt dla Gemini:\n{prompt_content}")
    logging.info(f"--- Koniec zawartości dla Gemini {user_psid} ---\n")

    try:
        response = gemini_model.generate_content(
            prompt_content,
            generation_config=generation_config,
            safety_settings=SAFETY_SETTINGS,
            stream=False # Używamy trybu bez strumieniowania dla prostoty
        )

        # Logowanie informacji o bezpieczeństwie i zakończeniu
        if response.prompt_feedback and response.prompt_feedback.block_reason:
             logging.warning(f"[{user_psid}] Prompt zablokowany przez Gemini. Powód: {response.prompt_feedback.block_reason_message}")
             return None
        if not response.candidates:
             logging.warning(f"[{user_psid}] Odpowiedź Gemini nie zawiera kandydatów.")
             # Sprawdź, czy odpowiedź została zablokowana
             if response.candidates[0].finish_reason.name != "STOP":
                 logging.warning(f"    Powód zakończenia: {response.candidates[0].finish_reason.name}")
                 if response.candidates[0].safety_ratings:
                    logging.warning(f"    Oceny bezpieczeństwa: {response.candidates[0].safety_ratings}")
             return None

        # Sprawdzenie, czy kandydat ma zawartość
        candidate = response.candidates[0]
        if not candidate.content or not candidate.content.parts:
            logging.warning(f"[{user_psid}] Kandydat w odpowiedzi Gemini nie ma zawartości (content/parts).")
            logging.warning(f"    Powód zakończenia: {candidate.finish_reason.name}")
            if candidate.safety_ratings: logging.warning(f"    Oceny bezpieczeństwa: {candidate.safety_ratings}")
            return None

        # Pobranie tekstu odpowiedzi
        generated_text = candidate.content.parts[0].text.strip()
        logging.info(f"[{user_psid}] Gemini ({model_purpose}) odpowiedziało (raw): '{generated_text[:200]}...'") # Loguj tylko początek długiej odpowiedzi
        return generated_text

    except Exception as e:
        logging.error(f"!!! BŁĄD podczas wywołania Gemini ({MODEL_ID}) dla {user_psid} ({model_purpose}): {e}", exc_info=True)
        return None


def get_gemini_general_response(user_psid, user_input, history):
    """Wywołuje AI do prowadzenia rozmowy i wykrywania intencji umówienia."""
    if not user_input: return None # Nie ma sensu pytać AI o pusty input

    # Przygotowanie historii dla AI (bez wpisów systemowych)
    history_for_ai = [msg for msg in history if msg.role in ('user', 'model')]
    user_content = Content(role="user", parts=[Part.from_text(user_input)])

    # Przygotowanie promptu z instrukcją systemową
    # Ważne: Instrukcja systemowa może być przekazana jako pierwszy element listy `contents`
    # lub jako osobny parametr `system_instruction` w nowszych wersjach API/SDK.
    # Użyjemy tutaj metody z pierwszą wiadomością od 'user' zawierającą instrukcję,
    # a następnie wiadomością od 'model' potwierdzającą zrozumienie.
    prompt_content = [
        Content(role="user", parts=[Part.from_text(SYSTEM_INSTRUCTION_GENERAL)]),
        Content(role="model", parts=[Part.from_text("Rozumiem. Będę asystentem 'Zakręcone Korepetycje'. Będę odpowiadał na pytania, prowadził rozmowę i informował o intencji umówienia spotkania za pomocą znacznika " + INTENT_SCHEDULE_MARKER + ".")]),
    ]
    # Dodaj historię i ostatnią wiadomość użytkownika
    prompt_content.extend(history_for_ai)
    prompt_content.append(user_content)

    # Przycinanie promptu, jeśli jest za długi (prosta metoda - usuwanie najstarszych tur)
    # Bardziej zaawansowane metody mogłyby liczyć tokeny
    while len(prompt_content) > (MAX_HISTORY_TURNS * 2 + 3) and len(prompt_content) > 3: # +3 dla instrukcji, potwierdzenia i ostatniej wiad. usera
        logging.warning(f"[{user_psid}] Prompt dla General AI za długi ({len(prompt_content)} wiad.). Usuwam najstarszą turę (user+model).")
        prompt_content.pop(2) # Usuń najstarszą wiadomość użytkownika
        if len(prompt_content) > 3:
             prompt_content.pop(2) # Usuń odpowiadającą jej wiadomość modelu

    response_text = _call_gemini(user_psid, prompt_content, GENERATION_CONFIG_DEFAULT, model_purpose="General Conversation & Intent Detection")

    return response_text # Może zawierać INTENT_SCHEDULE_MARKER lub być None w razie błędu


def get_gemini_slot_proposal(user_psid, history, available_slots):
    """Wywołuje AI, aby wybrało jeden slot z listy i sformułowało propozycję."""
    if not available_slots:
        logging.warning(f"[{user_psid}]: Brak slotów do przekazania AI do propozycji.")
        return None, None # Zwróć None dla tekstu i ISO

    # Przygotuj listę slotów dla AI (ograniczona liczba)
    slots_text_for_ai = format_slots_for_ai(available_slots[:MAX_SLOTS_FOR_AI])
    logging.info(f"[{user_psid}] Przekazuję {min(len(available_slots), MAX_SLOTS_FOR_AI)} slotów do AI w celu wyboru.")

    # Przygotuj historię dla AI (bez wpisów systemowych)
    history_for_ai = [msg for msg in history if msg.role in ('user', 'model')]

    # Przygotuj prompt z instrukcją systemową dla AI proponującego
    current_instruction = SYSTEM_INSTRUCTION_PROPOSE.format(available_slots_text=slots_text_for_ai)
    prompt_content = [
        Content(role="user", parts=[Part.from_text(current_instruction)]),
        Content(role="model", parts=[Part.from_text(f"Rozumiem. Wybiorę jeden najlepszy termin z dostarczonej listy, sformułuję propozycję i dołączę znacznik {SLOT_ISO_MARKER_PREFIX}ISO_TERMINU{SLOT_ISO_MARKER_SUFFIX}.")])
    ]
    # Dodaj historię (może zawierać preferencje użytkownika)
    prompt_content.extend(history_for_ai)

    # Przycinanie promptu (jeśli konieczne)
    while len(prompt_content) > (MAX_HISTORY_TURNS * 2 + 2) and len(prompt_content) > 2:
         logging.warning(f"[{user_psid}] Prompt dla Slot Proposal AI za długi ({len(prompt_content)} wiad.). Usuwam najstarszą turę (user+model).")
         prompt_content.pop(2)
         if len(prompt_content) > 2:
             prompt_content.pop(2)

    generated_text = _call_gemini(user_psid, prompt_content, GENERATION_CONFIG_PROPOSAL, model_purpose="Slot Proposal")

    if not generated_text:
        logging.error(f"!!! BŁĄD [{user_psid}]: AI nie zwróciło odpowiedzi przy propozycji slotu.")
        return None, None

    # --- Walidacja odpowiedzi AI ---
    iso_match = re.search(rf"\{SLOT_ISO_MARKER_PREFIX}(.*?)\{SLOT_ISO_MARKER_SUFFIX}", generated_text)
    if iso_match:
        extracted_iso = iso_match.group(1)
        # Sprawdź, czy wyekstrahowany ISO jest na liście dostępnych slotów (tych przekazanych AI)
        slot_exists = any(slot.isoformat() == extracted_iso for slot in available_slots[:MAX_SLOTS_FOR_AI])
        if slot_exists:
            # Usuń znacznik z tekstu dla użytkownika
            text_for_user = re.sub(rf"\{SLOT_ISO_MARKER_PREFIX}.*?\{SLOT_ISO_MARKER_SUFFIX}", "", generated_text).strip()
            # Dodatkowe czyszczenie (np. usunięcie ewentualnych podwójnych spacji)
            text_for_user = re.sub(r'\s+', ' ', text_for_user).strip()
            logging.info(f"[{user_psid}] AI wybrało poprawny slot: {extracted_iso}. Tekst dla użytkownika: '{text_for_user}'")
            return text_for_user, extracted_iso # Zwróć tekst i ISO
        else:
            logging.error(f"!!! BŁĄD KRYTYCZNY AI [{user_psid}]: Zaproponowany ISO '{extracted_iso}' nie znajduje się na liście dostępnych slotów przekazanych do AI!")
            # To nie powinno się zdarzyć, jeśli AI działa zgodnie z instrukcją
            # Zwracamy błąd, aby uniknąć proponowania nieistniejącego terminu
            return None, None # Błąd - AI wymyśliło slot lub błąd walidacji
    else:
        logging.error(f"!!! BŁĄD KRYTYCZNY AI [{user_psid}]: Brak znacznika {SLOT_ISO_MARKER_PREFIX}...{SLOT_ISO_MARKER_SUFFIX} w odpowiedzi AI proponującej slot!")
        logging.error(f"    Odpowiedź AI: '{generated_text}'")
        # To jest poważny błąd, AI nie zastosowało się do kluczowej instrukcji
        return None, None # Błąd - AI nie dołączyło wymaganego znacznika

def get_gemini_feedback_decision(user_psid, user_feedback, history, last_proposal_text):
     """Wywołuje AI do interpretacji odpowiedzi użytkownika na propozycję terminu."""
     if not user_feedback: return "[CLARIFY]" # Traktuj pustą odpowiedź jako niejasną

     # Przygotuj historię dla AI (bez wpisów systemowych)
     history_for_ai = [msg for msg in history if msg.role in ('user', 'model')]
     user_content = Content(role="user", parts=[Part.from_text(user_feedback)])

     # Przygotuj prompt z instrukcją systemową dla AI interpretującego feedback
     current_instruction = SYSTEM_INSTRUCTION_FEEDBACK.format(
         last_proposal_text=last_proposal_text, # Tekst ostatniej propozycji BOTA
         user_feedback=user_feedback           # Odpowiedź UŻYTKOWNIKA na tę propozycję
     )
     prompt_content = [
         Content(role="user", parts=[Part.from_text(current_instruction)]),
         Content(role="model", parts=[Part.from_text("Rozumiem. Przeanalizuję odpowiedź użytkownika i zwrócę dokładnie jeden znacznik akcji: [ACCEPT], [REJECT_FIND_NEXT PREFERENCE=...], lub [CLARIFY].")])
     ]
     # Dodaj historię (może dawać kontekst np. poprzednich odrzuceń)
     # ORAZ ostatnią wiadomość użytkownika (tę, którą interpretujemy)
     prompt_content.extend(history_for_ai)
     prompt_content.append(user_content) # Dodaj feedback usera jako ostatnią wiadomość

     # Przycinanie promptu (jeśli konieczne)
     while len(prompt_content) > (MAX_HISTORY_TURNS * 2 + 3) and len(prompt_content) > 3:
         logging.warning(f"[{user_psid}] Prompt dla Feedback AI za długi ({len(prompt_content)} wiad.). Usuwam najstarszą turę (user+model).")
         prompt_content.pop(2)
         if len(prompt_content) > 3:
             prompt_content.pop(2)

     decision = _call_gemini(user_psid, prompt_content, GENERATION_CONFIG_FEEDBACK, model_purpose="Feedback Interpretation")

     if not decision:
         logging.error(f"!!! BŁĄD [{user_psid}]: AI nie zwróciło decyzji przy interpretacji feedbacku. Domyślnie CLARIFY.")
         return "[CLARIFY]" # Fallback w razie błędu AI

     # Podstawowa walidacja formatu znacznika
     if decision.startswith("[") and decision.endswith("]"):
         logging.info(f"[{user_psid}] AI zinterpretowało feedback jako: {decision}")
         # Można dodać bardziej szczegółową walidację, czy znacznik jest jednym z oczekiwanych
         # np. czy część REJECT zawiera poprawną PREFERENCE
         # Na razie zakładamy, że AI zwróci poprawny format, jeśli zwróci znacznik.
         return decision
     else:
         # Jeśli AI zwróciło zwykły tekst zamiast znacznika, potraktuj to jako potrzebę wyjaśnienia
         logging.warning(f"Ostrz. [{user_psid}]: AI nie zwróciło poprawnego znacznika akcji, tylko tekst: '{decision}'. Traktuję jako CLARIFY.")
         return "[CLARIFY]"

# =====================================================================
# === OBSŁUGA WEBHOOKA FACEBOOKA =====================================
# =====================================================================

@app.route('/webhook', methods=['GET'])
def webhook_verification():
    """Obsługuje weryfikację webhooka przez Facebooka (GET request)."""
    logging.info("--- Otrzymano żądanie GET (weryfikacja webhooka) ---")
    hub_mode = request.args.get('hub.mode')
    hub_token = request.args.get('hub.verify_token')
    hub_challenge = request.args.get('hub.challenge')

    logging.info(f"Mode: {hub_mode}")
    logging.info(f"Token Provided: {'OK' if hub_token == VERIFY_TOKEN else 'BŁĘDNY!'}")
    # Nie loguj tokena w produkcji! logging.info(f"Expected Token: {VERIFY_TOKEN}")
    logging.info(f"Challenge: {'Present' if hub_challenge else 'Missing'}")

    if hub_mode == 'subscribe' and hub_token == VERIFY_TOKEN:
        logging.info("Weryfikacja GET zakończona pomyślnie!")
        return Response(hub_challenge, status=200)
    else:
        logging.warning("Weryfikacja GET nie powiodła się. Nieprawidłowy mode lub token.")
        return Response("Verification failed", status=403)

@app.route('/webhook', methods=['POST'])
def webhook_handle():
    """Obsługuje przychodzące zdarzenia z Facebooka (POST request)."""
    logging.info("\n" + "="*30 + f" {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')} OTRZYMANO POST " + "="*30)
    raw_data = request.data.decode('utf-8')
    data = None

    try:
        data = json.loads(raw_data)
        # Logowanie tylko struktury, nie pełnej treści w produkcji
        logging.debug(f"Odebrane dane (struktura): {json.dumps(data, indent=2)}")

        if data and data.get("object") == "page":
            for entry in data.get("entry", []):
                # page_id = entry.get("id") # ID strony FB
                # timestamp = entry.get("time") # Czas zdarzenia
                for messaging_event in entry.get("messaging", []):

                    # Sprawdź, czy zdarzenie ma nadawcę i odbiorcę (bota)
                    if "sender" not in messaging_event or "id" not in messaging_event["sender"] or \
                       "recipient" not in messaging_event or "id" not in messaging_event["recipient"]:
                        logging.warning("Pominięto zdarzenie bez sender.id lub recipient.id")
                        continue

                    sender_id = messaging_event["sender"]["id"]
                    recipient_id = messaging_event["recipient"]["id"] # ID strony, która otrzymała wiadomość

                    # Ignoruj wiadomości wysłane przez samą stronę (echa)
                    if sender_id == recipient_id:
                         logging.info(f"[{sender_id}] Pominięto echo wiadomości od strony.")
                         continue # Zwykle nie chcemy przetwarzać ech

                    logging.info(f"--- Przetwarzanie zdarzenia dla PSID: {sender_id} ---")

                    # Wczytaj historię i kontekst
                    history, context = load_history(sender_id)
                    last_proposed_slot_iso = context.get('last_proposed_slot_iso')
                    # Sprawdź, czy kontekst jest 'aktualny' (tj. czy ostatni zapisany wpis to kontekst)
                    # Porównujemy indeks zapisany w kontekście z długością wczytanej historii (user/model)
                    is_context_current = last_proposed_slot_iso and context.get('message_index') == len(history)

                    if is_context_current:
                        logging.info(f"    Aktywny kontekst: Oczekiwano na odpowiedź dot. slotu {last_proposed_slot_iso}")
                    elif last_proposed_slot_iso:
                        # Jeśli kontekst istnieje, ale nie jest na końcu, oznacza to, że użytkownik napisał coś innego po propozycji.
                        logging.info(f"    Kontekst 'last_proposed_slot_iso' ({last_proposed_slot_iso}) jest nieaktualny (użytkownik napisał coś nowego). Resetowanie kontekstu.")
                        last_proposed_slot_iso = None # Zresetuj kontekst dla dalszej logiki

                    # -----------------------------------------
                    # --- Główna Logika Przetwarzania Wiadomości ---
                    # -----------------------------------------
                    if messaging_event.get("message"):
                        message_data = messaging_event["message"]
                        message_id = message_data.get("mid")
                        logging.info(f"    Odebrano wiadomość (ID: {message_id})")

                        # Ignoruj wiadomości typu "echo" wysłane przez bota
                        if message_data.get("is_echo"):
                            logging.info("      Wiadomość jest echem. Ignorowanie.")
                            continue

                        user_input_text = None
                        user_content = None # Obiekt Content dla wiadomości użytkownika

                        # --- Obsługa wiadomości tekstowej ---
                        if "text" in message_data:
                            user_input_text = message_data["text"].strip()
                            logging.info(f"      Tekst użytkownika: '{user_input_text}'")
                            if not user_input_text:
                                logging.info("      Pusta wiadomość tekstowa. Ignorowanie.")
                                continue # Ignoruj puste wiadomości
                            user_content = Content(role="user", parts=[Part.from_text(user_input_text)])

                        # --- Obsługa załączników (na razie tylko informacyjnie) ---
                        elif "attachments" in message_data:
                            attachment_type = message_data['attachments'][0].get('type', 'nieznany')
                            logging.info(f"      Odebrano załącznik typu: {attachment_type}.")
                            # Można dodać logikę obsługi konkretnych typów (np. lokalizacji)
                            # Na razie informujemy użytkownika i AI
                            user_input_text = f"[Użytkownik wysłał załącznik typu: {attachment_type}]"
                            user_content = Content(role="user", parts=[Part.from_text(user_input_text)])
                            # Od razu wyślij odpowiedź o braku obsługi i zakończ przetwarzanie tego zdarzenia
                            no_attachment_message = "Przepraszam, obecnie nie potrafię przetwarzać załączników."
                            send_message(sender_id, no_attachment_message)
                            model_content = Content(role="model", parts=[Part.from_text(no_attachment_message)])
                            save_history(sender_id, history + [user_content, model_content], context_to_save=None) # Zapisz bez kontekstu
                            continue # Przejdź do następnego zdarzenia

                        # --- Jeśli nie ma ani tekstu ani znanego załącznika ---
                        else:
                            logging.warning(f"      Nieznany typ wiadomości: {message_data}")
                            user_input_text = "[Odebrano nieznany typ wiadomości]"
                            user_content = Content(role="user", parts=[Part.from_text(user_input_text)])
                            # Można wysłać wiadomość "Nie rozumiem"
                            unknown_message_reply = "Przepraszam, nie rozumiem tej wiadomości."
                            send_message(sender_id, unknown_message_reply)
                            model_content = Content(role="model", parts=[Part.from_text(unknown_message_reply)])
                            save_history(sender_id, history + [user_content, model_content], context_to_save=None) # Zapisz bez kontekstu
                            continue # Przejdź do następnego zdarzenia

                        # === GŁÓWNA LOGIKA DECYZYJNA ===
                        action_to_perform = None
                        text_to_send_immediately = None # Tekst do wysłania przed akcją (np. "Sprawdzam...")
                        text_to_send_as_result = None # Tekst wynikowy akcji (np. potwierdzenie rezerwacji)
                        context_to_save = None # Kontekst do zapisania po akcji
                        model_response_content = None # Obiekt Content odpowiedzi modelu do zapisu w historii
                        error_occurred = False

                        # Symulacja pisania (jeśli włączona) - robimy to raz na początku
                        if ENABLE_TYPING_DELAY and user_input_text:
                            # Można obliczyć delay na podstawie długości odpowiedzi, ale nie mamy jej jeszcze
                            # Użyjmy stałego minimalnego opóźnienia lub losowego
                            delay = max(MIN_TYPING_DELAY_SECONDS, min(MAX_TYPING_DELAY_SECONDS, len(user_input_text) / TYPING_CHARS_PER_SECOND))
                            logging.info(f"      Symulacja pisania... ({delay:.2f}s)")
                            time.sleep(delay)


                        # --- SCENARIUSZ 1: Oczekiwano na odpowiedź dot. zaproponowanego terminu ---
                        if is_context_current and last_proposed_slot_iso:
                            logging.info(f"      SCENARIUSZ: Analiza odpowiedzi na propozycję slotu {last_proposed_slot_iso}")
                            try:
                                # Odzyskaj tekst ostatniej propozycji z historii
                                last_bot_message_text = history[-1].parts[0].text if history and history[-1].role == 'model' else "Proponowany termin."
                                gemini_decision = get_gemini_feedback_decision(sender_id, user_input_text, history, last_bot_message_text)
                            except Exception as feedback_err:
                                logging.error(f"!!! BŁĄD podczas interpretacji feedbacku przez AI: {feedback_err}", exc_info=True)
                                gemini_decision = "[CLARIFY]" # Fallback
                                text_to_send_as_result = "Przepraszam, mam chwilowy problem ze zrozumieniem Twojej odpowiedzi. Czy możesz powtórzyć?"
                                error_occurred = True

                            if gemini_decision == "[ACCEPT]":
                                action_to_perform = 'book'
                            elif isinstance(gemini_decision, str) and gemini_decision.startswith("[REJECT_FIND_NEXT"):
                                action_to_perform = 'find_and_propose' # Akcja: znajdź nowy slot i niech AI go zaproponuje
                                # Parsuj preferencje z decyzji AI
                                preference = 'any' # Domyślnie szukaj jakiegokolwiek innego
                                requested_day_str = None
                                requested_hour_int = None
                                pref_match = re.search(r"PREFERENCE='([^']*)'", gemini_decision)
                                if pref_match: preference = pref_match.group(1)
                                day_match = re.search(r"DAY='([^']*)'", gemini_decision)
                                if day_match: requested_day_str = day_match.group(1)
                                hour_match = re.search(r"HOUR='(\d+)'", gemini_decision)
                                if hour_match:
                                    try: requested_hour_int = int(hour_match.group(1))
                                    except ValueError: logging.warning(f"Nie udało się sparsować godziny z {gemini_decision}")
                                logging.info(f"      Użytkownik odrzucił. Preferencje dla nowego szukania: {preference}, Dzień: {requested_day_str}, Godzina: {requested_hour_int}")
                                # Opcjonalnie wyślij potwierdzenie przyjęcia preferencji
                                text_to_send_immediately = "Rozumiem. Poszukam innego terminu zgodnie z Twoimi wskazówkami."
                            elif gemini_decision == "[CLARIFY]" or error_occurred:
                                action_to_perform = 'send_clarification'
                                if not error_occurred:
                                     text_to_send_as_result = "Nie jestem pewien, co masz na myśli w kontekście zaproponowanego terminu. Czy mógłbyś/mogłabyś doprecyzować, czy termin pasuje, czy szukamy innego?"
                                # Utrzymaj kontekst, bo nadal pytamy o TEN sam slot
                                context_to_save = {'role': 'system', 'type': 'last_proposal', 'slot_iso': last_proposed_slot_iso}
                            else: # Nieoczekiwany znacznik lub błąd
                                logging.error(f"!!! Nieoczekiwana decyzja z AI (feedback): {gemini_decision}")
                                action_to_perform = 'send_error'
                                text_to_send_as_result = "Wystąpił nieoczekiwany problem podczas przetwarzania Twojej odpowiedzi."
                                error_occurred = True
                            # Po obsłudze feedbacku, kontekst slotu jest już nieaktualny (chyba że CLARIFY)
                            if action_to_perform != 'send_clarification':
                                context_to_save = None # Resetuj kontekst

                        # --- SCENARIUSZ 2: Normalna rozmowa, brak oczekiwania na feedback ---
                        else:
                            logging.info(f"      SCENARIUSZ: Normalna rozmowa lub nieaktualny kontekst.")
                            try:
                                gemini_response = get_gemini_general_response(sender_id, user_input_text, history)
                            except Exception as general_err:
                                logging.error(f"!!! BŁĄD podczas generowania odpowiedzi przez AI: {general_err}", exc_info=True)
                                gemini_response = None # Traktuj jak błąd AI
                                text_to_send_as_result = "Przepraszam, mam chwilowy problem z przetworzeniem Twojej wiadomości. Spróbuj ponownie za chwilę."
                                error_occurred = True

                            if gemini_response:
                                # Sprawdź, czy AI wykryło intencję umówienia
                                if INTENT_SCHEDULE_MARKER in gemini_response:
                                    logging.info(f"      AI wykryło intencję umówienia [{INTENT_SCHEDULE_MARKER}].")
                                    action_to_perform = 'find_and_propose'
                                    # Wyślij część odpowiedzi AI przed znacznikiem (jeśli jest)
                                    text_before_marker = gemini_response.split(INTENT_SCHEDULE_MARKER, 1)[0].strip()
                                    if text_before_marker:
                                        text_to_send_immediately = text_before_marker
                                    else: # Jeśli AI zwróciło tylko marker, użyj domyślnego tekstu
                                        text_to_send_immediately = "Dobrze, sprawdzę dostępne terminy."
                                    # Domyślne preferencje dla pierwszego szukania
                                    preference = 'any'
                                    requested_day_str = None
                                    requested_hour_int = None
                                    # Odpowiedź modelu do zapisu to będzie ta część przed markerem
                                    model_response_content = Content(role="model", parts=[Part.from_text(text_to_send_immediately)])
                                else:
                                    # Normalna odpowiedź AI, bez intencji umówienia
                                    action_to_perform = 'send_gemini_response'
                                    text_to_send_as_result = gemini_response
                            elif not error_occurred: # Jeśli AI zwróciło None, ale nie było błędu (np. zablokowane)
                                logging.warning(f"[{sender_id}] AI nie zwróciło odpowiedzi (prawdopodobnie zablokowana lub pusty wynik).")
                                action_to_perform = 'send_error'
                                text_to_send_as_result = "Nie mogę wygenerować odpowiedzi na tę wiadomość. Spróbuj sformułować ją inaczej."
                                error_occurred = True
                            # Jeśli error_occurred jest True, text_to_send_as_result jest już ustawiony

                        # === WYKONANIE AKCJI ===
                        logging.info(f"      Akcja do wykonania: {action_to_perform}")

                        # Najpierw wyślij ewentualną wiadomość przygotowawczą
                        if text_to_send_immediately:
                            send_message(sender_id, text_to_send_immediately)
                            # Jeśli ta wiadomość była odpowiedzią AI, zapisz ją od razu (jeśli nie została zapisana wcześniej)
                            if not model_response_content and action_to_perform == 'find_and_propose' and INTENT_SCHEDULE_MARKER in gemini_response:
                                model_response_content = Content(role="model", parts=[Part.from_text(text_to_send_immediately)])
                                # Zapisz historię już teraz, zanim zacznie się długie szukanie slotów
                                save_history(sender_id, history + [user_content, model_response_content], context_to_save=None)
                                history.append(user_content) # Dodaj do bieżącej historii
                                history.append(model_response_content)

                        # Wykonaj główną akcję
                        if action_to_perform == 'book':
                            try:
                                tz = _get_timezone()
                                start_time = datetime.datetime.fromisoformat(last_proposed_slot_iso).astimezone(tz)
                                end_time = start_time + datetime.timedelta(minutes=APPOINTMENT_DURATION_MINUTES)
                                user_profile = get_user_profile(sender_id) # Pobierz profil do rezerwacji
                                user_name = user_profile.get('first_name', '') if user_profile else 'Użytkownik FB'
                                logging.info(f"      Wywołanie book_appointment dla {start_time}")
                                success, message_to_user = book_appointment(
                                    TARGET_CALENDAR_ID,
                                    start_time,
                                    end_time,
                                    summary=f"Korepetycje (FB)",
                                    description=f"Rezerwacja przez bota FB.\nPSID: {sender_id}\nImię: {user_name}",
                                    user_name=user_name
                                )
                                text_to_send_as_result = message_to_user
                                if not success: error_occurred = True # Zaznacz błąd, jeśli rezerwacja się nie udała
                                context_to_save = None # Zresetuj kontekst po próbie rezerwacji
                            except Exception as book_err:
                                logging.error(f"!!! BŁĄD KRYTYCZNY podczas próby rezerwacji: {book_err}", exc_info=True)
                                text_to_send_as_result = "Wystąpił poważny błąd podczas próby rezerwacji terminu. Skontaktuj się z nami bezpośrednio."
                                error_occurred = True
                                context_to_save = None # Zresetuj kontekst

                        elif action_to_perform == 'find_and_propose':
                            try:
                                tz = _get_timezone()
                                now = datetime.datetime.now(tz)
                                search_start = now # Domyślnie szukaj od teraz

                                # Jeśli szukamy po odrzuceniu, dostosuj search_start na podstawie preferencji
                                if last_proposed_slot_iso and preference != 'any': # preference jest ustawiane w logice REJECT
                                    try:
                                        last_proposed_dt = datetime.datetime.fromisoformat(last_proposed_slot_iso).astimezone(tz)
                                        base_start = last_proposed_dt + datetime.timedelta(minutes=10) # Zacznij szukać chwilę po ostatniej propozycji

                                        if preference == 'later':
                                             search_start = base_start + datetime.timedelta(hours=1) # Przeskocz o godzinę
                                             # Jeśli proponowano rano w dzień roboczy, spróbuj zacząć od popołudnia
                                             if last_proposed_dt.weekday() < 5 and last_proposed_dt.hour < PREFERRED_WEEKDAY_START_HOUR:
                                                 afternoon_start = tz.localize(datetime.datetime.combine(last_proposed_dt.date(), datetime.time(PREFERRED_WEEKDAY_START_HOUR, 0)))
                                                 search_start = max(search_start, afternoon_start)
                                        elif preference == 'earlier':
                                            # Szukanie wcześniejszego terminu jest trudne, bo musimy zacząć od 'now', ale filtrować wyniki
                                            # Na razie prostsze: szukaj od 'now', AI powinno wybrać coś sensownego
                                             search_start = now # Zacznij od teraz, ale AI ma preferencje z historii
                                        elif preference == 'next_day':
                                             search_start = tz.localize(datetime.datetime.combine(last_proposed_dt.date() + datetime.timedelta(days=1), datetime.time(WORK_START_HOUR, 0)))
                                        elif preference == 'specific_day' and requested_day_str:
                                             try:
                                                 target_weekday = POLISH_WEEKDAYS.index(requested_day_str)
                                                 days_ahead = (target_weekday - now.weekday() + 7) % 7
                                                 if days_ahead == 0 and now.time() >= datetime.time(WORK_END_HOUR, 0): # Jeśli to dzisiaj, ale już po godzinach pracy
                                                      days_ahead = 7
                                                 target_date = now.date() + datetime.timedelta(days=days_ahead)
                                                 search_start = tz.localize(datetime.datetime.combine(target_date, datetime.time(WORK_START_HOUR, 0)))
                                             except ValueError:
                                                 logging.warning(f"Nieznana nazwa dnia: {requested_day_str}. Szukam od teraz.")
                                                 search_start = now
                                        # Dla specific_hour i any zostawiamy search_start = now (lub base_start jeśli było odrzucenie), AI przefiltruje

                                        search_start = max(search_start, now) # Nigdy nie szukaj w przeszłości

                                    except Exception as date_err:
                                        logging.error(f"Błąd przy ustalaniu search_start na podstawie preferencji: {date_err}", exc_info=True)
                                        search_start = now # W razie błędu szukaj od teraz
                                else: # Pierwsze szukanie
                                    search_start = now

                                logging.info(f"      Rozpoczynanie szukania slotów od: {search_start.strftime('%Y-%m-%d %H:%M:%S %Z')}")
                                search_end_date = (search_start + datetime.timedelta(days=MAX_SEARCH_DAYS)).date()
                                search_end = tz.localize(datetime.datetime.combine(search_end_date, datetime.time(WORK_END_HOUR, 0)))

                                free_slots = get_free_slots(TARGET_CALENDAR_ID, search_start, search_end)

                                if free_slots:
                                    # Opcjonalne wstępne filtrowanie dla AI na podstawie preferencji godzinowych
                                    filtered_slots = free_slots
                                    if preference == 'specific_hour' and requested_hour_int is not None:
                                        filtered_slots = [s for s in free_slots if s.hour == requested_hour_int]
                                        logging.info(f"Wstępnie przefiltrowano sloty do godziny {requested_hour_int}. Liczba: {len(filtered_slots)}")
                                    elif preference == 'later': # Preferuj popołudnia
                                        afternoon_slots = [s for s in free_slots if s.hour >= PREFERRED_WEEKDAY_START_HOUR]
                                        if afternoon_slots: filtered_slots = afternoon_slots
                                        logging.info(f"Wstępnie przefiltrowano sloty do popołudniowych. Liczba: {len(filtered_slots)}")
                                    # Jeśli filtrowanie nic nie dało, użyj wszystkich
                                    if not filtered_slots: filtered_slots = free_slots

                                    if not filtered_slots:
                                         logging.info("      Brak wolnych slotów po wstępnym filtrowaniu.")
                                         text_to_send_as_result = "Niestety, nie znalazłem żadnych wolnych terminów pasujących do Twoich preferencji w najbliższym czasie."
                                         context_to_save = None
                                    else:
                                        # Wywołaj AI, aby wybrało i zaproponowało slot
                                        logging.info(f"      Przekazanie {len(filtered_slots)} slotów do AI w celu wyboru propozycji...")
                                        proposal_text, proposed_iso = get_gemini_slot_proposal(sender_id, history, filtered_slots)

                                        if proposal_text and proposed_iso:
                                            text_to_send_as_result = proposal_text
                                            # Zapisz kontekst z zaproponowanym slotem ISO
                                            context_to_save = {'role': 'system', 'type': 'last_proposal', 'slot_iso': proposed_iso}
                                            logging.info(f"      AI zaproponowało: {proposal_text} (ISO: {proposed_iso})")
                                        else: # Błąd AI przy propozycji
                                            logging.error("!!! BŁĄD: AI nie udało się wybrać i sformułować propozycji slotu.")
                                            text_to_send_as_result = "Przepraszam, mam problem z wybraniem konkretnego terminu w tej chwili. Spróbujmy ponownie za chwilę."
                                            error_occurred = True
                                            context_to_save = None # Usuń kontekst, bo propozycja się nie udała
                                else: # Brak jakichkolwiek wolnych slotów
                                    logging.info("      Nie znaleziono żadnych wolnych slotów w kalendarzu.")
                                    text_to_send_as_result = "Niestety, aktualnie brak wolnych terminów w kalendarzu."
                                    context_to_save = None
                            except Exception as find_err:
                                logging.error(f"!!! BŁĄD KRYTYCZNY podczas szukania/proponowania slotów: {find_err}", exc_info=True)
                                text_to_send_as_result = "Wystąpił nieoczekiwany błąd podczas sprawdzania dostępności terminów."
                                error_occurred = True
                                context_to_save = None # Resetuj kontekst

                        elif action_to_perform == 'send_gemini_response' or \
                             action_to_perform == 'send_clarification' or \
                             action_to_perform == 'send_error':
                            # Wiadomość do wysłania jest już w text_to_send_as_result
                            # Kontekst (jeśli potrzebny, np. dla clarification) jest w context_to_save
                            pass # Akcja to tylko wysłanie wiadomości na końcu

                        else:
                            logging.error(f"!!! Nierozpoznana akcja do wykonania: {action_to_perform} dla PSID {sender_id}")
                            text_to_send_as_result = "Wystąpił wewnętrzny błąd bota."
                            error_occurred = True
                            context_to_save = None


                        # === WYSŁANIE WIADOMOŚCI WYNIKOWEJ I ZAPIS HISTORII ===

                        # Jeśli akcja wygenerowała tekst do wysłania
                        if text_to_send_as_result:
                             send_message(sender_id, text_to_send_as_result)
                             # Utwórz obiekt Content dla odpowiedzi modelu (jeśli jeszcze nie istnieje)
                             if not model_response_content:
                                model_response_content = Content(role="model", parts=[Part.from_text(text_to_send_as_result)])

                        # Zapisz historię na końcu, uwzględniając wiadomość użytkownika,
                        # odpowiedź modelu (jeśli była) i nowy kontekst (jeśli jest)
                        if user_content: # Upewnij się, że mamy co dodać od użytkownika
                             history_to_save = history + [user_content]
                             if model_response_content:
                                 history_to_save.append(model_response_content)

                             logging.info(f"      Zapisywanie historii. Nowy kontekst: {context_to_save}")
                             save_history(sender_id, history_to_save, context_to_save=context_to_save)
                        else:
                             logging.warning(f"[{sender_id}] Brak user_content do zapisania w historii.")


                    # -----------------------------------------
                    # --- Obsługa innych typów zdarzeń (Postback, Read, Delivery) ---
                    # -----------------------------------------
                    elif messaging_event.get("postback"):
                         # UWAGA: Logika Postback nie została dostosowana do nowego przepływu AI!
                         # Może wymagać refaktoryzacji, jeśli przyciski mają inicjować szukanie/rezerwację.
                         # Obecnie traktuje postback jak zwykłą wiadomość tekstową.
                         postback_data = messaging_event["postback"]
                         payload = postback_data.get("payload")
                         title = postback_data.get("title", payload) # Użyj tytułu przycisku jako input
                         logging.info(f"    Odebrano Postback: Tytuł='{title}', Payload='{payload}'")

                         postback_as_text = f"Użytkownik kliknął przycisk: '{title}' (payload: {payload})"
                         user_content = Content(role="user", parts=[Part.from_text(postback_as_text)])

                         # Wywołaj ogólne AI, aby zareagowało na kliknięcie
                         # Zakładamy, że przyciski nie inicjują od razu umawiania (wymagałoby to zmiany)
                         gemini_response = get_gemini_general_response(sender_id, postback_as_text, history)

                         if gemini_response and INTENT_SCHEDULE_MARKER not in gemini_response: # Jeśli AI chce pogadać, a nie umawiać
                              send_message(sender_id, gemini_response)
                              model_content = Content(role="model", parts=[Part.from_text(gemini_response)])
                              save_history(sender_id, history + [user_content, model_content], context_to_save=None)
                         elif gemini_response and INTENT_SCHEDULE_MARKER in gemini_response:
                              # TODO: Obsłużyć przypadek, gdy kliknięcie przycisku ma prowadzić do umówienia
                              # Obecnie wysyłamy tylko info i prosimy o potwierdzenie
                              text_before_marker = gemini_response.split(INTENT_SCHEDULE_MARKER, 1)[0].strip()
                              clarification_msg = text_before_marker + "\nCzy chcesz, żebym poszukał terminu?" if text_before_marker else "Chcesz umówić termin?"
                              send_message(sender_id, clarification_msg)
                              model_content = Content(role="model", parts=[Part.from_text(clarification_msg)])
                              save_history(sender_id, history + [user_content, model_content], context_to_save=None)
                         else: # Błąd AI
                              error_msg = "Mam problem z przetworzeniem tej akcji."
                              send_message(sender_id, error_msg)
                              model_content = Content(role="model", parts=[Part.from_text(error_msg)])
                              save_history(sender_id, history + [user_content, model_content], context_to_save=None)

                    elif messaging_event.get("read"):
                        # Użytkownik odczytał wiadomość
                        watermark = messaging_event["read"]["watermark"]
                        logging.info(f"    Wiadomości odczytane przez użytkownika do czasu: {datetime.datetime.fromtimestamp(watermark/1000).strftime('%Y-%m-%d %H:%M:%S')}")
                        # Zwykle nie wymaga akcji

                    elif messaging_event.get("delivery"):
                        # Wiadomość została dostarczona do użytkownika
                        # mids = messaging_event["delivery"].get("mids", [])
                        # watermark = messaging_event["delivery"]["watermark"]
                        # logging.debug(f"    Wiadomości dostarczone: {len(mids)} (do {datetime.datetime.fromtimestamp(watermark/1000).strftime('%H:%M:%S')})")
                        pass # Zwykle nie wymaga akcji

                    else:
                        logging.warning(f"    Otrzymano nieobsługiwany typ zdarzenia messaging: {json.dumps(messaging_event)}")

            # Zwróć odpowiedź 200 OK do Facebooka, aby potwierdzić odbiór zdarzenia
            return Response("EVENT_RECEIVED", status=200)
        else:
            # Otrzymano dane, ale nie są to zdarzenia strony ('page')
            logging.warning(f"Otrzymano POST z obiektem innym niż 'page': {data.get('object') if data else 'Brak danych'}")
            return Response("Non-page object received", status=200) # Zwracamy OK, ale nic nie robimy

    except json.JSONDecodeError as json_err:
        logging.error(f"!!! KRYTYCZNY BŁĄD: Nie można sparsować JSON z requestu: {json_err}", exc_info=True)
        logging.error(f"   Pierwsze 500 znaków danych: {raw_data[:500]}")
        return Response("Invalid JSON format", status=400) # Zwróć błąd 400 Bad Request
    except Exception as e:
        logging.error(f"!!! KRYTYCZNY BŁĄD podczas przetwarzania POST webhooka: {e}", exc_info=True)
        # Zwróć 200 OK, aby Facebook nie próbował wysyłać ponownie tego samego zdarzenia
        # Błędy są logowane po stronie serwera
        return Response("Internal server error processing event", status=200)


# =====================================================================
# === URUCHOMIENIE SERWERA APLIKACJI ==================================
# =====================================================================

if __name__ == '__main__':
    ensure_dir(HISTORY_DIR) # Utwórz katalog historii, jeśli nie istnieje
    port = int(os.environ.get("PORT", 8080))
    # Odczytaj tryb debug z zmiennej środowiskowej
    debug_mode = os.environ.get("FLASK_DEBUG", "False").lower() in ("true", "1", "yes")

    # --- Logowanie konfiguracji startowej ---
    print("\n" + "="*50)
    print("--- START KONFIGURACJI BOTA ---")
    if not VERIFY_TOKEN or VERIFY_TOKEN == "KOLAGEN": # Sprawdź czy domyślny token nie został zmieniony
        print("!!! OSTRZEŻENIE: FB_VERIFY_TOKEN jest pusty lub używa wartości domyślnej 'KOLAGEN'. Ustaw bezpieczny token!")
    else:
        print("  FB_VERIFY_TOKEN: Ustawiony (OK)")

    if not PAGE_ACCESS_TOKEN or len(PAGE_ACCESS_TOKEN) < 50 or PAGE_ACCESS_TOKEN == "YOUR_FACEBOOK_PAGE_ACCESS_TOKEN":
        print("\n!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
        print("!!! KRYTYCZNE OSTRZEŻENIE: FB_PAGE_ACCESS_TOKEN jest PUSTY lub NIEPOPRAWNY !!!")
        print("!!! Bot NIE BĘDZIE MÓGŁ WYSYŁAĆ WIADOMOŚCI!                       !!!")
        print("!!! Ustaw poprawny token dostępu do strony w zmiennej środowiskowej   !!!")
        print("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!\n")
    else:
        print("  FB_PAGE_ACCESS_TOKEN: Ustawiony (OK)")

    print(f"  Katalog historii konwersacji: {HISTORY_DIR}")
    print(f"  Projekt Google Cloud (Vertex AI): {PROJECT_ID}")
    print(f"  Lokalizacja Vertex AI: {LOCATION}")
    print(f"  Model Vertex AI: {MODEL_ID}")
    print(f"  Plik klucza Google Calendar: {SERVICE_ACCOUNT_FILE} {'(OK)' if os.path.exists(SERVICE_ACCOUNT_FILE) else '(BRAK PLIKU!)'}")
    if not os.path.exists(SERVICE_ACCOUNT_FILE):
        print("  !!! OSTRZEŻENIE: Funkcje kalendarza nie będą działać bez pliku klucza!")
    print(f"  Docelowy Kalendarz Google ID: {TARGET_CALENDAR_ID}")
    print(f"  Strefa czasowa kalendarza: {CALENDAR_TIMEZONE}")
    print(f"  Symulacja pisania: {'Włączona' if ENABLE_TYPING_DELAY else 'Wyłączona'}")

    if gemini_model is None:
        print("\n!!! OSTRZEŻENIE: Model Gemini AI ({MODEL_ID}) NIE został załadowany poprawnie!")
        print("!!! Funkcje AI nie będą działać. Sprawdź logi błędów inicjalizacji.\n")
    else:
        print("  Model Gemini AI: Załadowany (OK)")

    if _calendar_service is None:
         # Spróbuj zainicjować teraz, żeby sprawdzić czy działa
         get_calendar_service()
         if _calendar_service is None and os.path.exists(SERVICE_ACCOUNT_FILE):
              print("\n!!! OSTRZEŻENIE: Nie udało się zainicjować usługi Google Calendar, mimo że plik klucza istnieje.")
              print("!!! Sprawdź uprawnienia konta usługi i logi błędów.\n")

    print("--- KONIEC KONFIGURACJI BOTA ---")
    print("="*50 + "\n")


    # --- Uruchomienie serwera ---
    print(f"Uruchamianie serwera Flask na porcie {port}")
    print(f"Tryb debug: {debug_mode}")

    # Użyj Waitress jako serwera produkcyjnego, jeśli jest zainstalowany i nie jest w trybie debug
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
        # Uruchom w trybie debug Flask (automatyczne przeładowywanie)
        print("Uruchamianie wbudowanego serwera Flask w trybie DEBUG...")
        app.run(host='0.0.0.0', port=port, debug=True)
