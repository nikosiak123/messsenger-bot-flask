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
# ZASADA 1: UŻYTO DOMYŚLNEGO TOKENU PODANEGO PRZEZ UŻYTKOWNIKA
PAGE_ACCESS_TOKEN = os.environ.get("FB_PAGE_ACCESS_TOKEN", "EACNAHFzEhkUBO7nbFAtYvfPWbEht1B3chQqWLx76Ljg2ekdbJYoOrnpjATqhS0EZC8S0q8a49hEZBaZByZCaj5gr1z62dAaMgcZA1B")
PROJECT_ID = os.environ.get("GCP_PROJECT_ID", "linear-booth-450221-k1") # Zmień na swój Project ID
LOCATION = os.environ.get("GCP_LOCATION", "us-central1") # Zmień na swoją lokalizację
# ZASADA 2: UŻYTO MODELU PODANEGO PRZEZ UŻYTKOWNIKA
MODEL_ID = os.environ.get("VERTEX_MODEL_ID", "gemini-2.0-flash-001")

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
    # Zmieniono warunek, aby poprawnie obsługiwał domyślny token
    if not PAGE_ACCESS_TOKEN or PAGE_ACCESS_TOKEN == "EACNAHFzEhkUBO7nbFAtYvfPWbEht1B3chQqWLx76Ljg2ekdbJYoOrnpjATqhS0EZC8S0q8a49hEZBaZByZCaj5gr1z62dAaMgcZA1B":
        logging.warning(f"[{psid}] Używany jest domyślny lub pusty PAGE_ACCESS_TOKEN. Realne pobieranie profilu może nie działać. Profil niepobrany.")
        # Zwracamy None, ale logujemy ostrzeżenie zamiast błędu krytycznego
        return None
    elif len(PAGE_ACCESS_TOKEN) < 50: # Dodatkowe sprawdzenie minimalnej długości
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
            # Dodatkowe logowanie w przypadku błędów związanych z tokenem
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
                    # Zapisz indeks W CAŁEJ historii_data, a nie w przefiltrowanej `history`
                    context['message_index_in_file'] = i
                    logging.info(f"[{user_psid}] Odczytano AKTUALNY kontekst: last_proposed_slot_iso (na pozycji {i} w pliku)")
                else:
                    logging.warning(f"Ostrz. [{user_psid}]: Poprawny wpis systemowy, ale brak 'slot_iso' (idx {i})")
            elif isinstance(msg_data, dict) and msg_data.get('role') == 'system':
                 logging.info(f"[{user_psid}] Pominięto stary kontekst systemowy na indeksie {i}")
            else:
                logging.warning(f"Ostrz. [{user_psid}]: Pominięto niepoprawny wpis w historii (idx {i}): {msg_data}")

        logging.info(f"[{user_psid}] Wczytano historię: {len(history)} wiadomości (user/model).")
        # Sprawdź aktualność kontekstu porównując zapisany indeks z *całkowitą* liczbą wpisów w pliku
        if 'message_index_in_file' in context and context['message_index_in_file'] != len(history_data) - 1:
            logging.info(f"[{user_psid}] Kontekst 'last_proposed_slot_iso' jest nieaktualny (nie na końcu pliku). Resetowanie.")
            context = {} # Zresetuj kontekst, jeśli nie był ostatnim wpisem

        return history, context

    except (json.JSONDecodeError, KeyError, IndexError, TypeError) as e:
        logging.error(f"BŁĄD [{user_psid}] parsowania historii: {e}.", exc_info=True)
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
        hour_str = start_time.strftime('%#H') if os.name != 'nt' else start_time.strftime('%H') # %#H dla niektórych systemów, %H dla Windows
        try:
             # Spróbuj alternatywnie uzyskać godzinę jako liczbę
             hour_str = str(start_time.hour)
        except Exception:
            logging.warning("Nie udało się sformatować godziny bez wiodącego zera, używam standardowego formatu.")
            hour_str = start_time.strftime('%H') # Fallback do standardowego formatu

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
        # Formatowanie godziny bez wiodącego zera
        hour_str = slot_start.strftime('%#H') if os.name != 'nt' else slot_start.strftime('%H') # %#H dla niektórych systemów, %H dla Windows
        try:
             # Spróbuj alternatywnie uzyskać godzinę jako liczbę
             hour_str = str(slot_start.hour)
        except Exception:
             logging.warning("Nie udało się sformatować godziny bez wiodącego zera dla użytkownika, używam standardowego formatu.")
             hour_str = slot_start.strftime('%H') # Fallback

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
    logging.info(f"Ładowanie modelu: {MODEL_ID}") # Używa MODEL_ID z góry
    gemini_model = GenerativeModel(MODEL_ID)
    logging.info(f"Model {MODEL_ID} załadowany pomyślnie.")
except Exception as e:
    logging.critical(f"KRYTYCZNY BŁĄD podczas inicjalizacji Vertex AI lub ładowania modelu {MODEL_ID}: {e}", exc_info=True)

# Konfiguracja generowania i bezpieczeństwa dla AI
GENERATION_CONFIG_DEFAULT = GenerationConfig(
    temperature=0.7,
    top_p=0.95,
    top_k=40,
    max_output_tokens=1024
)
GENERATION_CONFIG_PROPOSAL = GenerationConfig(
    temperature=0.4,
    top_p=0.95,
    top_k=40,
    max_output_tokens=512
)
GENERATION_CONFIG_FEEDBACK = GenerationConfig(
    temperature=0.1,
    top_p=0.95,
    top_k=40,
    max_output_tokens=100
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
        "messaging_type": "RESPONSE"
    }

    # Sprawdzenie czy token nie jest pusty LUB nie jest domyślnym tokenem z przykładu
    if not PAGE_ACCESS_TOKEN or PAGE_ACCESS_TOKEN == "EACNAHFzEhkUBO7nbFAtYvfPWbEht1B3chQqWLx76Ljg2ekdbJYoOrnpjATqhS0EZC8S0q8a49hEZBaZByZCaj5gr1z62dAaMgcZA1B":
        logging.error(f"!!! [{recipient_id}] Próba wysłania wiadomości z PUSTYM lub DOMYŚLNYM tokenem! Wiadomość NIE wysłana.")
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
            for delimiter in ['\n\n', '\n', '. ', '! ', '? ', ' ']:
                search_limit = MESSAGE_CHAR_LIMIT - (len(delimiter) - 1) if len(delimiter) > 1 else MESSAGE_CHAR_LIMIT
                temp_index = remaining_text.rfind(delimiter, 0, search_limit + len(delimiter))

                if temp_index != -1 and temp_index <= MESSAGE_CHAR_LIMIT :
                    split_index = temp_index + len(delimiter)
                    break

            if split_index == -1:
                split_index = MESSAGE_CHAR_LIMIT
                logging.warning(f"[{recipient_id}] Nie znaleziono naturalnego miejsca podziału, cięcie na {MESSAGE_CHAR_LIMIT} znakach.")

            chunk = remaining_text[:split_index].strip()
            if chunk:
                chunks.append(chunk)
            remaining_text = remaining_text[split_index:].strip()

        num_chunks = len(chunks)
        logging.info(f"[{recipient_id}] Podzielono na {num_chunks} fragmentów.")
        send_success_count = 0
        for i, chunk in enumerate(chunks):
            logging.info(f"[{recipient_id}] Wysyłanie fragm. {i+1}/{num_chunks} (dł: {len(chunk)})...")
            if not _send_single_message(recipient_id, chunk):
                logging.error(f"!!! [{recipient_id}] Anulowano wysyłanie reszty wiadomości po błędzie na fragmencie {i+1}.")
                break
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
        # Używamy MODEL_ID w logu
        logging.error(f"!!! [{user_psid}] Model Gemini ({MODEL_ID}) nie jest załadowany! Nie można wykonać wywołania ({model_purpose}).")
        return None
    if not prompt_content:
        logging.warning(f"[{user_psid}] Pusty prompt przekazany do Gemini ({model_purpose}).")
        return None

    # Używamy MODEL_ID w logu
    logging.info(f"\n--- [{user_psid}] Wywołanie Gemini ({MODEL_ID}) - Cel: {model_purpose} ---")
    # Opcjonalne logowanie pełnego promptu (może być bardzo długie)
    # logging.debug(f"Pełny prompt dla Gemini ({MODEL_ID}):\n{prompt_content}")
    logging.info(f"--- Koniec zawartości dla Gemini ({MODEL_ID}) {user_psid} ---\n")

    try:
        response = gemini_model.generate_content(
            prompt_content,
            generation_config=generation_config,
            safety_settings=SAFETY_SETTINGS,
            stream=False
        )

        # Logowanie informacji o bezpieczeństwie i zakończeniu
        # Sprawdzanie obiektu `prompt_feedback`
        if hasattr(response, 'prompt_feedback') and response.prompt_feedback and response.prompt_feedback.block_reason:
             logging.warning(f"[{user_psid}] Prompt zablokowany przez Gemini ({MODEL_ID}). Powód: {response.prompt_feedback.block_reason} ({response.prompt_feedback.block_reason_message})")
             # Logowanie ocen bezpieczeństwa promptu, jeśli dostępne
             if response.prompt_feedback.safety_ratings:
                 logging.warning(f"    Oceny bezpieczeństwa promptu: {response.prompt_feedback.safety_ratings}")
             return None # Zwróć None, jeśli prompt został zablokowany

        # Sprawdzanie kandydatów i ich zawartości
        if not response.candidates:
             logging.warning(f"[{user_psid}] Odpowiedź Gemini ({MODEL_ID}) nie zawiera kandydatów.")
             # Sprawdź powód zakończenia, jeśli kandydatów nie ma (może być informacja w prompt_feedback)
             if hasattr(response, 'prompt_feedback') and response.prompt_feedback:
                  logging.warning(f"    Prompt Feedback (brak kandydatów): {response.prompt_feedback}")
             return None

        # Sprawdzenie, czy kandydat ma zawartość i nie został zablokowany
        candidate = response.candidates[0]
        if candidate.finish_reason.name != "STOP" and candidate.finish_reason.name != "MAX_TOKENS":
             logging.warning(f"[{user_psid}] Odpowiedź Gemini ({MODEL_ID}) zakończona z powodu innego niż STOP/MAX_TOKENS. Powód: {candidate.finish_reason.name}")
             if candidate.safety_ratings:
                 logging.warning(f"    Oceny bezpieczeństwa odpowiedzi: {candidate.safety_ratings}")
             # Jeśli odpowiedź została zablokowana (np. SAFETY), zwróć None
             if candidate.finish_reason.name == "SAFETY":
                 return None

        if not candidate.content or not candidate.content.parts:
            logging.warning(f"[{user_psid}] Kandydat w odpowiedzi Gemini ({MODEL_ID}) nie ma zawartości (content/parts).")
            logging.warning(f"    Powód zakończenia: {candidate.finish_reason.name}")
            if candidate.safety_ratings: logging.warning(f"    Oceny bezpieczeństwa: {candidate.safety_ratings}")
            return None

        # Pobranie tekstu odpowiedzi
        generated_text = candidate.content.parts[0].text.strip()
        logging.info(f"[{user_psid}] Gemini ({MODEL_ID}, {model_purpose}) odpowiedziało (raw): '{generated_text[:200]}...'")
        return generated_text

    except Exception as e:
        # Używamy MODEL_ID w logu błędu
        logging.error(f"!!! BŁĄD podczas wywołania Gemini ({MODEL_ID}) dla {user_psid} ({model_purpose}): {e}", exc_info=True)
        return None


def get_gemini_general_response(user_psid, user_input, history):
    """Wywołuje AI do prowadzenia rozmowy i wykrywania intencji umówienia."""
    if not user_input: return None

    history_for_ai = [msg for msg in history if msg.role in ('user', 'model')]
    user_content = Content(role="user", parts=[Part.from_text(user_input)])

    prompt_content = [
        Content(role="user", parts=[Part.from_text(SYSTEM_INSTRUCTION_GENERAL)]),
        Content(role="model", parts=[Part.from_text("Rozumiem. Będę asystentem 'Zakręcone Korepetycje'. Będę odpowiadał na pytania, prowadził rozmowę i informował o intencji umówienia spotkania za pomocą znacznika " + INTENT_SCHEDULE_MARKER + ".")]),
    ]
    prompt_content.extend(history_for_ai)
    prompt_content.append(user_content)

    # Przycinanie promptu
    while len(prompt_content) > (MAX_HISTORY_TURNS * 2 + 3) and len(prompt_content) > 3:
        logging.warning(f"[{user_psid}] Prompt dla General AI ({MODEL_ID}) za długi ({len(prompt_content)} wiad.). Usuwam najstarszą turę.")
        prompt_content.pop(2)
        if len(prompt_content) > 3:
             prompt_content.pop(2)

    response_text = _call_gemini(user_psid, prompt_content, GENERATION_CONFIG_DEFAULT, model_purpose="General Conversation & Intent Detection")

    return response_text


def get_gemini_slot_proposal(user_psid, history, available_slots):
    """Wywołuje AI, aby wybrało jeden slot z listy i sformułowało propozycję."""
    if not available_slots:
        logging.warning(f"[{user_psid}]: Brak slotów do przekazania AI ({MODEL_ID}) do propozycji.")
        return None, None

    slots_text_for_ai = format_slots_for_ai(available_slots[:MAX_SLOTS_FOR_AI])
    logging.info(f"[{user_psid}] Przekazuję {min(len(available_slots), MAX_SLOTS_FOR_AI)} slotów do AI ({MODEL_ID}) w celu wyboru.")

    history_for_ai = [msg for msg in history if msg.role in ('user', 'model')]

    current_instruction = SYSTEM_INSTRUCTION_PROPOSE.format(available_slots_text=slots_text_for_ai)
    prompt_content = [
        Content(role="user", parts=[Part.from_text(current_instruction)]),
        Content(role="model", parts=[Part.from_text(f"Rozumiem. Wybiorę jeden najlepszy termin z dostarczonej listy, sformułuję propozycję i dołączę znacznik {SLOT_ISO_MARKER_PREFIX}ISO_TERMINU{SLOT_ISO_MARKER_SUFFIX}.")])
    ]
    prompt_content.extend(history_for_ai)

    # Przycinanie promptu
    while len(prompt_content) > (MAX_HISTORY_TURNS * 2 + 2) and len(prompt_content) > 2:
         logging.warning(f"[{user_psid}] Prompt dla Slot Proposal AI ({MODEL_ID}) za długi ({len(prompt_content)} wiad.). Usuwam najstarszą turę.")
         prompt_content.pop(2)
         if len(prompt_content) > 2:
             prompt_content.pop(2)

    generated_text = _call_gemini(user_psid, prompt_content, GENERATION_CONFIG_PROPOSAL, model_purpose="Slot Proposal")

    if not generated_text:
        logging.error(f"!!! BŁĄD [{user_psid}]: AI ({MODEL_ID}) nie zwróciło odpowiedzi przy propozycji slotu.")
        return None, None

    # --- Walidacja odpowiedzi AI ---
    iso_match = re.search(rf"\{SLOT_ISO_MARKER_PREFIX}(.*?)\{SLOT_ISO_MARKER_SUFFIX}", generated_text)
    if iso_match:
        extracted_iso = iso_match.group(1)
        slot_exists = any(slot.isoformat() == extracted_iso for slot in available_slots[:MAX_SLOTS_FOR_AI])
        if slot_exists:
            text_for_user = re.sub(rf"\{SLOT_ISO_MARKER_PREFIX}.*?\{SLOT_ISO_MARKER_SUFFIX}", "", generated_text).strip()
            text_for_user = re.sub(r'\s+', ' ', text_for_user).strip()
            logging.info(f"[{user_psid}] AI ({MODEL_ID}) wybrało poprawny slot: {extracted_iso}. Tekst dla użytkownika: '{text_for_user}'")
            return text_for_user, extracted_iso
        else:
            logging.error(f"!!! BŁĄD KRYTYCZNY AI [{user_psid}, {MODEL_ID}]: Zaproponowany ISO '{extracted_iso}' nie znajduje się na liście dostępnych slotów przekazanych do AI!")
            return None, None
    else:
        logging.error(f"!!! BŁĄD KRYTYCZNY AI [{user_psid}, {MODEL_ID}]: Brak znacznika {SLOT_ISO_MARKER_PREFIX}...{SLOT_ISO_MARKER_SUFFIX} w odpowiedzi AI proponującej slot!")
        logging.error(f"    Odpowiedź AI ({MODEL_ID}): '{generated_text}'")
        return None, None

def get_gemini_feedback_decision(user_psid, user_feedback, history, last_proposal_text):
     """Wywołuje AI do interpretacji odpowiedzi użytkownika na propozycję terminu."""
     if not user_feedback: return "[CLARIFY]"

     history_for_ai = [msg for msg in history if msg.role in ('user', 'model')]
     user_content = Content(role="user", parts=[Part.from_text(user_feedback)])

     current_instruction = SYSTEM_INSTRUCTION_FEEDBACK.format(
         last_proposal_text=last_proposal_text,
         user_feedback=user_feedback
     )
     prompt_content = [
         Content(role="user", parts=[Part.from_text(current_instruction)]),
         Content(role="model", parts=[Part.from_text("Rozumiem. Przeanalizuję odpowiedź użytkownika i zwrócę dokładnie jeden znacznik akcji: [ACCEPT], [REJECT_FIND_NEXT PREFERENCE=...], lub [CLARIFY].")])
     ]
     prompt_content.extend(history_for_ai)
     prompt_content.append(user_content)

     # Przycinanie promptu
     while len(prompt_content) > (MAX_HISTORY_TURNS * 2 + 3) and len(prompt_content) > 3:
         logging.warning(f"[{user_psid}] Prompt dla Feedback AI ({MODEL_ID}) za długi ({len(prompt_content)} wiad.). Usuwam najstarszą turę.")
         prompt_content.pop(2)
         if len(prompt_content) > 3:
             prompt_content.pop(2)

     decision = _call_gemini(user_psid, prompt_content, GENERATION_CONFIG_FEEDBACK, model_purpose="Feedback Interpretation")

     if not decision:
         logging.error(f"!!! BŁĄD [{user_psid}]: AI ({MODEL_ID}) nie zwróciło decyzji przy interpretacji feedbacku. Domyślnie CLARIFY.")
         return "[CLARIFY]"

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
    """Obsługuje weryfikację webhooka przez Facebooka (GET request)."""
    logging.info("--- Otrzymano żądanie GET (weryfikacja webhooka) ---")
    hub_mode = request.args.get('hub.mode')
    hub_token = request.args.get('hub.verify_token')
    hub_challenge = request.args.get('hub.challenge')

    logging.info(f"Mode: {hub_mode}")
    # Sprawdź czy token jest poprawny, ale nie loguj go
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
    """Obsługuje przychodzące zdarzenia z Facebooka (POST request)."""
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
                    # Aktualność kontekstu jest sprawdzana w load_history
                    is_context_current = bool(last_proposed_slot_iso) # Jeśli load_history zwróciło slot, to jest aktualny

                    if is_context_current:
                        logging.info(f"    Aktywny kontekst: Oczekiwano na odpowiedź dot. slotu {last_proposed_slot_iso}")
                    # Nie potrzebujemy else, bo load_history resetuje kontekst, jeśli jest nieaktualny

                    # -----------------------------------------
                    # --- Główna Logika Przetwarzania Wiadomości ---
                    # -----------------------------------------
                    if messaging_event.get("message"):
                        message_data = messaging_event["message"]
                        message_id = message_data.get("mid")
                        logging.info(f"    Odebrano wiadomość (ID: {message_id})")

                        if message_data.get("is_echo"):
                            logging.info("      Wiadomość jest echem. Ignorowanie.")
                            continue

                        user_input_text = None
                        user_content = None

                        # --- Obsługa wiadomości tekstowej ---
                        if "text" in message_data:
                            user_input_text = message_data["text"].strip()
                            logging.info(f"      Tekst użytkownika: '{user_input_text}'")
                            if not user_input_text:
                                logging.info("      Pusta wiadomość tekstowa. Ignorowanie.")
                                continue
                            user_content = Content(role="user", parts=[Part.from_text(user_input_text)])

                        # --- Obsługa załączników ---
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

                        # --- Jeśli nie ma ani tekstu ani znanego załącznika ---
                        else:
                            logging.warning(f"      Nieznany typ wiadomości: {message_data}")
                            user_input_text = "[Odebrano nieznany typ wiadomości]"
                            user_content = Content(role="user", parts=[Part.from_text(user_input_text)])
                            unknown_message_reply = "Przepraszam, nie rozumiem tej wiadomości."
                            send_message(sender_id, unknown_message_reply)
                            model_content = Content(role="model", parts=[Part.from_text(unknown_message_reply)])
                            save_history(sender_id, history + [user_content, model_content], context_to_save=None)
                            continue

                        # === GŁÓWNA LOGIKA DECYZYJNA ===
                        action_to_perform = None
                        text_to_send_immediately = None
                        text_to_send_as_result = None
                        context_to_save = None
                        model_response_content = None
                        error_occurred = False
                        # Zmienne do przekazania preferencji do find_and_propose
                        preference = 'any'
                        requested_day_str = None
                        requested_hour_int = None

                        if ENABLE_TYPING_DELAY and user_input_text:
                            delay = max(MIN_TYPING_DELAY_SECONDS, min(MAX_TYPING_DELAY_SECONDS, len(user_input_text) / TYPING_CHARS_PER_SECOND))
                            logging.info(f"      Symulacja pisania... ({delay:.2f}s)")
                            time.sleep(delay)


                        # --- SCENARIUSZ 1: Oczekiwano na odpowiedź dot. zaproponowanego terminu ---
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
                                # Parsuj preferencje z decyzji AI
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
                                # Utrzymaj kontekst przy CLARIFY
                                context_to_save = {'role': 'system', 'type': 'last_proposal', 'slot_iso': last_proposed_slot_iso}
                            else:
                                logging.error(f"!!! Nieoczekiwana decyzja z AI ({MODEL_ID}, feedback): {gemini_decision}")
                                action_to_perform = 'send_error'
                                text_to_send_as_result = "Wystąpił nieoczekiwany problem podczas przetwarzania Twojej odpowiedzi."
                                error_occurred = True
                            # Resetuj kontekst po obsłudze feedbacku (chyba że CLARIFY)
                            if action_to_perform != 'send_clarification':
                                context_to_save = None

                        # --- SCENARIUSZ 2: Normalna rozmowa, brak oczekiwania na feedback ---
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
                                    # Resetuj preferencje dla pierwszego szukania
                                    preference = 'any'
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

                        # === WYKONANIE AKCJI ===
                        logging.info(f"      Akcja do wykonania: {action_to_perform}")

                        if text_to_send_immediately:
                            send_message(sender_id, text_to_send_immediately)
                            if not model_response_content and action_to_perform == 'find_and_propose' and gemini_response and INTENT_SCHEDULE_MARKER in gemini_response:
                                model_response_content = Content(role="model", parts=[Part.from_text(text_to_send_immediately)])
                                save_history(sender_id, history + [user_content, model_response_content], context_to_save=None)
                                history.append(user_content)
                                history.append(model_response_content)

                        if action_to_perform == 'book':
                            try:
                                tz = _get_timezone()
                                start_time = datetime.datetime.fromisoformat(last_proposed_slot_iso).astimezone(tz)
                                end_time = start_time + datetime.timedelta(minutes=APPOINTMENT_DURATION_MINUTES)
                                user_profile = get_user_profile(sender_id)
                                user_name = user_profile.get('first_name', 'Użytkownik FB') if user_profile else 'Użytkownik FB'
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
                                if not success: error_occurred = True
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

                                # Użyj `preference`, `requested_day_str`, `requested_hour_int` ustawionych wcześniej
                                if is_context_current and last_proposed_slot_iso and preference != 'any': # Jeśli było odrzucenie z preferencjami
                                    try:
                                        last_proposed_dt = datetime.datetime.fromisoformat(last_proposed_slot_iso).astimezone(tz)
                                        base_start = last_proposed_dt + datetime.timedelta(minutes=10)

                                        if preference == 'later':
                                             search_start = base_start + datetime.timedelta(hours=1)
                                             if last_proposed_dt.weekday() < 5 and last_proposed_dt.hour < PREFERRED_WEEKDAY_START_HOUR:
                                                 afternoon_start = tz.localize(datetime.datetime.combine(last_proposed_dt.date(), datetime.time(PREFERRED_WEEKDAY_START_HOUR, 0)))
                                                 search_start = max(search_start, afternoon_start)
                                        elif preference == 'earlier':
                                             search_start = now # Szukaj od teraz, AI musi wybrać coś wcześniejszego
                                        elif preference == 'next_day':
                                             search_start = tz.localize(datetime.datetime.combine(last_proposed_dt.date() + datetime.timedelta(days=1), datetime.time(WORK_START_HOUR, 0)))
                                        elif preference == 'specific_day' and requested_day_str:
                                             try:
                                                 target_weekday = POLISH_WEEKDAYS.index(requested_day_str)
                                                 current_weekday = now.weekday()
                                                 days_ahead = (target_weekday - current_weekday + 7) % 7
                                                 # Jeśli to ten sam dzień tygodnia, a już minęła godzina pracy lub jest za późno, przesuń o tydzień
                                                 if days_ahead == 0 and now.time() >= datetime.time(WORK_END_HOUR, 0):
                                                      days_ahead = 7
                                                 target_date = now.date() + datetime.timedelta(days=days_ahead)
                                                 search_start = tz.localize(datetime.datetime.combine(target_date, datetime.time(WORK_START_HOUR, 0)))
                                             except ValueError:
                                                 logging.warning(f"Nieznana nazwa dnia: {requested_day_str}. Szukam od teraz.")
                                                 search_start = now
                                        elif preference == 'specific_hour':
                                             # Szukamy od teraz, ale przekażemy godzinę do filtrowania
                                             search_start = now

                                        search_start = max(search_start, now)

                                    except Exception as date_err:
                                        logging.error(f"Błąd przy ustalaniu search_start na podstawie preferencji: {date_err}", exc_info=True)
                                        search_start = now
                                else: # Pierwsze szukanie lub odrzucenie bez preferencji
                                    search_start = now

                                logging.info(f"      Rozpoczynanie szukania slotów od: {search_start.strftime('%Y-%m-%d %H:%M:%S %Z')}")
                                search_end_date = (search_start + datetime.timedelta(days=MAX_SEARCH_DAYS)).date()
                                search_end = tz.localize(datetime.datetime.combine(search_end_date, datetime.time(WORK_END_HOUR, 0)))

                                free_slots = get_free_slots(TARGET_CALENDAR_ID, search_start, search_end)

                                if free_slots:
                                    filtered_slots = free_slots
                                    if preference == 'specific_hour' and requested_hour_int is not None:
                                        potential_matches = [s for s in free_slots if s.hour == requested_hour_int]
                                        if potential_matches:
                                             filtered_slots = potential_matches
                                             logging.info(f"Wstępnie przefiltrowano sloty do godziny {requested_hour_int}. Liczba: {len(filtered_slots)}")
                                        else:
                                             logging.info(f"Brak slotów o godzinie {requested_hour_int}. AI wybierze z wszystkich {len(free_slots)}.")
                                    elif preference == 'later':
                                        potential_matches = [s for s in free_slots if s.hour >= PREFERRED_WEEKDAY_START_HOUR]
                                        if potential_matches:
                                             filtered_slots = potential_matches
                                             logging.info(f"Wstępnie przefiltrowano sloty do popołudniowych. Liczba: {len(filtered_slots)}")
                                        else:
                                             logging.info(f"Brak slotów popołudniowych. AI wybierze z wszystkich {len(free_slots)}.")
                                    elif preference == 'earlier':
                                         potential_matches = [s for s in free_slots if s.hour < 12] # Np. preferuj przed południem
                                         if potential_matches:
                                              filtered_slots = potential_matches
                                              logging.info(f"Wstępnie przefiltrowano sloty do przedpołudniowych. Liczba: {len(filtered_slots)}")
                                         else:
                                             logging.info(f"Brak slotów przedpołudniowych. AI wybierze z wszystkich {len(free_slots)}.")

                                    logging.info(f"      Przekazanie {len(filtered_slots)} slotów do AI ({MODEL_ID}) w celu wyboru propozycji...")
                                    proposal_text, proposed_iso = get_gemini_slot_proposal(sender_id, history, filtered_slots)

                                    if proposal_text and proposed_iso:
                                        text_to_send_as_result = proposal_text
                                        context_to_save = {'role': 'system', 'type': 'last_proposal', 'slot_iso': proposed_iso}
                                        logging.info(f"      AI ({MODEL_ID}) zaproponowało: {proposal_text} (ISO: {proposed_iso})")
                                    else:
                                        logging.error(f"!!! BŁĄD: AI ({MODEL_ID}) nie udało się wybrać i sformułować propozycji slotu.")
                                        text_to_send_as_result = "Przepraszam, mam problem z wybraniem konkretnego terminu w tej chwili. Spróbujmy ponownie za chwilę."
                                        error_occurred = True
                                        context_to_save = None
                                else:
                                    logging.info("      Nie znaleziono żadnych wolnych slotów w kalendarzu.")
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


                        # === WYSŁANIE WIADOMOŚCI WYNIKOWEJ I ZAPIS HISTORII ===
                        if text_to_send_as_result:
                             send_message(sender_id, text_to_send_as_result)
                             if not model_response_content:
                                model_response_content = Content(role="model", parts=[Part.from_text(text_to_send_as_result)])

                        if user_content:
                             history_to_save = history + [user_content]
                             if model_response_content:
                                 history_to_save.append(model_response_content)
                             logging.info(f"      Zapisywanie historii. Nowy kontekst: {context_to_save}")
                             save_history(sender_id, history_to_save, context_to_save=context_to_save)
                        else:
                             logging.warning(f"[{sender_id}] Brak user_content do zapisania w historii.")


                    # -----------------------------------------
                    # --- Obsługa innych typów zdarzeń ---
                    # -----------------------------------------
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
                        pass # Zwykle ignorujemy

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

    # ZASADA 1: Sprawdzenie zgodności z podanym tokenem
    if PAGE_ACCESS_TOKEN == "EACNAHFzEhkUBO7nbFAtYvfPWbEht1B3chQqWLx76Ljg2ekdbJYoOrnpjATqhS0EZC8S0q8a49hEZBaZByZCaj5gr1z62dAaMgcZA1B":
        print("\n!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
        print("!!! UWAGA: Używany jest DOMYŚLNY PAGE_ACCESS_TOKEN podany w instrukcji!   !!!")
        print("!!! Dla rzeczywistego działania bota, zastąp go PRAWDZIWYM tokenem     !!!")
        print("!!! dostępu do strony Facebook w zmiennej środowiskowej FB_PAGE_ACCESS_TOKEN !!!")
        print("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!\n")
    elif not PAGE_ACCESS_TOKEN or len(PAGE_ACCESS_TOKEN) < 50 :
         print("\n!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
         print("!!! KRYTYCZNE OSTRZEŻENIE: FB_PAGE_ACCESS_TOKEN PUSTY lub ZBYT KRÓTKI!    !!!")
         print("!!! Bot NIE BĘDZIE MÓGŁ WYSYŁAĆ WIADOMOŚCI!                       !!!")
         print("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!\n")
    else:
        print("  FB_PAGE_ACCESS_TOKEN: Ustawiony (wydaje się OK)")

    print(f"  Katalog historii konwersacji: {HISTORY_DIR}")
    print(f"  Projekt Google Cloud (Vertex AI): {PROJECT_ID}")
    print(f"  Lokalizacja Vertex AI: {LOCATION}")
    # ZASADA 2: Wypisanie używanego modelu
    print(f"  Model Vertex AI: {MODEL_ID} (zgodnie z wymaganiem)")
    print(f"  Plik klucza Google Calendar: {SERVICE_ACCOUNT_FILE} {'(OK)' if os.path.exists(SERVICE_ACCOUNT_FILE) else '(BRAK PLIKU!)'}")
    if not os.path.exists(SERVICE_ACCOUNT_FILE):
        print("  !!! OSTRZEŻENIE: Funkcje kalendarza nie będą działać bez pliku klucza!")
    print(f"  Docelowy Kalendarz Google ID: {TARGET_CALENDAR_ID}")
    print(f"  Strefa czasowa kalendarza: {CALENDAR_TIMEZONE}")
    print(f"  Symulacja pisania: {'Włączona' if ENABLE_TYPING_DELAY else 'Wyłączona'}")

    if gemini_model is None:
        print(f"\n!!! OSTRZEŻENIE: Model Gemini AI ({MODEL_ID}) NIE został załadowany poprawnie!")
        print("!!! Funkcje AI nie będą działać. Sprawdź logi błędów inicjalizacji.\n")
    else:
        print(f"  Model Gemini AI ({MODEL_ID}): Załadowany (OK)")

    # Sprawdzenie inicjalizacji kalendarza
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
        app.run(host='0.0.0.0', port=port, debug=True)
