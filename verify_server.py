# -*- coding: utf-8 -*-

# verify_server.py (połączony kod z iteracyjnym szukaniem wg preferencji - AI decyduje o slocie)

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
# EARLY_HOUR_LIMIT nie jest już potrzebny w logice Pythona

# --- Znaczniki dla komunikacji z AI ---
INTENT_SCHEDULE_MARKER = "[INTENT_SCHEDULE]"
SLOT_ISO_MARKER_PREFIX = "[SLOT_ISO:"
SLOT_ISO_MARKER_SUFFIX = "]"

# --- Ustawienia Modelu Gemini ---
# Konfiguracja dla propozycji terminu (AI ma przeanalizować historię)
GENERATION_CONFIG_PROPOSAL = GenerationConfig(
    temperature=0.2, # Trochę więcej kreatywności, aby uwzględnić historię
    top_p=0.95,
    top_k=40,
    max_output_tokens=512,
)

# Konfiguracja dla interpretacji feedbacku (deterministyczna)
GENERATION_CONFIG_FEEDBACK = GenerationConfig(
    temperature=0.0,
    top_p=0.95,
    top_k=40,
    max_output_tokens=128, # Zwiększono lekko, na wypadek dłuższych znaczników
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
                # Znajdź indeks ostatniego komunikatu systemowego (kontekstu)
                for i, msg_data in enumerate(reversed(history_data)):
                    if isinstance(msg_data, dict) and msg_data.get('role') == 'system':
                        last_system_message_index = len(history_data) - 1 - i
                        break

                for i, msg_data in enumerate(history_data):
                    if (isinstance(msg_data, dict) and 'role' in msg_data and
                            msg_data['role'] in ('user', 'model') and 'parts' in msg_data and
                            isinstance(msg_data['parts'], list) and msg_data['parts']):
                        # Przetwarzanie wiadomości użytkownika/modelu
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
                        # Przetwarzanie kontekstu systemowego - tylko ostatni jest aktywny
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
        # W przypadku błędu parsowania, lepiej usunąć/zmienić nazwę pliku, aby uniknąć pętli błędów
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
        # Filtruj tylko wiadomości user/model do zapisu (bez kontekstu systemowego z `history`)
        history_to_process = [m for m in history if isinstance(m, Content) and m.role in ('user', 'model')]

        # Zachowaj MAX_HISTORY_TURNS konwersacyjnych par
        max_messages_to_save = MAX_HISTORY_TURNS * 2
        if len(history_to_process) > max_messages_to_save:
            history_to_process = history_to_process[-max_messages_to_save:]

        # Konwertuj wiadomości na format JSON
        for msg in history_to_process:
             if isinstance(msg, Content) and hasattr(msg, 'role') and msg.role in ('user', 'model') and hasattr(msg, 'parts') and isinstance(msg.parts, list):
                parts_data = [{'text': part.text} for part in msg.parts if isinstance(part, Part) and hasattr(part, 'text')]
                if parts_data:
                    history_data.append({'role': msg.role, 'parts': parts_data})
             else:
                logging.warning(f"Ostrz. [{user_psid}]: Pomijanie nieprawidłowego obiektu historii podczas zapisu: {msg}")

        # Dodaj nowy kontekst systemowy na końcu, jeśli jest
        if context_to_save and isinstance(context_to_save, dict):
             history_data.append(context_to_save)
             logging.debug(f"[{user_psid}] Dodano kontekst do zapisu: {context_to_save.get('type')}")

        # Zapisz do pliku tymczasowego, a następnie zamień
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
        service = build('calendar', 'v3', credentials=creds, cache_discovery=False) # cache_discovery=False może pomóc przy problemach z cachem
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
            # Próba sparsowania z pełnym offsetem strefy czasowej
            dt = datetime.datetime.fromisoformat(dt_str)
        except ValueError:
            # Próba sparsowania formatu z 'Z' (UTC)
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
                # Próba sparsowania starszych formatów z offsetem (+HH:MM lub +HHMM)
                try:
                    # Usunięcie dwukropka w offsecie, jeśli jest
                    if ':' in dt_str[-6:]:
                       dt_str = dt_str[:-3] + dt_str[-2:]
                    dt = datetime.datetime.strptime(dt_str, '%Y-%m-%dT%H:%M:%S%z')
                except ValueError:
                    logging.warning(f"Ostrz.: Nie udało się sparsować dateTime: {dt_str}")
                    return None

        # Konwersja do docelowej strefy czasowej
        if dt.tzinfo is None:
             # Jeśli brak informacji o strefie, zakładamy, że jest w domyślnej strefie kalendarza
            dt = default_tz.localize(dt)
        else:
            dt = dt.astimezone(default_tz)
        return dt
    elif 'date' in event_time_data:
        # Wydarzenia całodniowe
        try:
            return datetime.date.fromisoformat(event_time_data['date'])
        except ValueError:
            logging.warning(f"Ostrz.: Nie sparsowano date: {event_time_data['date']}")
            return None
    return None # Jeśli ani 'dateTime' ani 'date' nie pasuje

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

    # Upewnij się, że daty/czasy są świadome strefy czasowej
    if start_datetime.tzinfo is None:
        start_datetime = tz.localize(start_datetime)
    else:
        start_datetime = start_datetime.astimezone(tz)

    if end_datetime.tzinfo is None:
        end_datetime = tz.localize(end_datetime)
    else:
        end_datetime = end_datetime.astimezone(tz)

    # Upewnij się, że start nie jest w przeszłości
    now = datetime.datetime.now(tz)
    start_datetime = max(start_datetime, now)

    # Jeśli start jest po końcu, zwróć pustą listę
    if start_datetime >= end_datetime:
        logging.info("Zakres wyszukiwania wolnych terminów jest nieprawidłowy (start >= end).")
        return []

    logging.info(f"Szukanie wolnych zakresów w kalendarzu '{calendar_id}'")
    logging.info(f"Zakres: od {start_datetime:%Y-%m-%d %H:%M %Z} do {end_datetime:%Y-%m-%d %H:%M %Z}")

    try:
        # Pobierz zajęte sloty używając freebusy API
        body = {
            "timeMin": start_datetime.isoformat(),
            "timeMax": end_datetime.isoformat(),
            "timeZone": CALENDAR_TIMEZONE,
            "items": [{"id": calendar_id}]
        }
        freebusy_result = service.freebusy().query(body=body).execute()
        busy_times_raw = freebusy_result.get('calendars', {}).get(calendar_id, {}).get('busy', [])

        # Konwertuj zajęte czasy na obiekty datetime ze strefą czasową
        busy_times = []
        for busy_slot in busy_times_raw:
            try:
                busy_start = datetime.datetime.fromisoformat(busy_slot['start']).astimezone(tz)
                busy_end = datetime.datetime.fromisoformat(busy_slot['end']).astimezone(tz)
                # Ogranicz zajęte czasy do faktycznego zakresu zapytania
                busy_start_clipped = max(busy_start, start_datetime)
                busy_end_clipped = min(busy_end, end_datetime)
                # Dodaj tylko, jeśli zakres jest poprawny po obcięciu
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

    # Sortuj i połącz nakładające się zajęte przedziały
    if not busy_times:
        merged_busy_times = []
    else:
        busy_times.sort(key=lambda x: x['start'])
        merged_busy_times = [busy_times[0]]
        for current_busy in busy_times[1:]:
            last_merged = merged_busy_times[-1]
            # Jeśli aktualny zajęty slot zaczyna się przed końcem poprzedniego (lub w tym samym momencie) - połącz
            if current_busy['start'] <= last_merged['end']:
                last_merged['end'] = max(last_merged['end'], current_busy['end'])
            else:
                merged_busy_times.append(current_busy)

    # Wyznacz wolne zakresy na podstawie zajętych
    free_ranges = []
    current_time = start_datetime

    for busy_slot in merged_busy_times:
        # Wolny czas od `current_time` do początku bieżącego zajętego slotu
        if current_time < busy_slot['start']:
            free_ranges.append({'start': current_time, 'end': busy_slot['start']})
        # Przesuń `current_time` na koniec bieżącego zajętego slotu
        current_time = max(current_time, busy_slot['end'])

    # Wolny czas od końca ostatniego zajętego slotu do końca zakresu wyszukiwania
    if current_time < end_datetime:
        free_ranges.append({'start': current_time, 'end': end_datetime})

    # Przefiltruj wolne zakresy przez godziny pracy i minimalną długość wizyty
    final_free_slots = []
    min_duration_delta = datetime.timedelta(minutes=APPOINTMENT_DURATION_MINUTES)

    for free_range in free_ranges:
        range_start = free_range['start']
        range_end = free_range['end']

        # Iteruj przez dni w obrębie wolnego zakresu
        current_day_start = range_start
        while current_day_start < range_end:
            day_date = current_day_start.date()
            work_day_start = tz.localize(datetime.datetime.combine(day_date, datetime.time(WORK_START_HOUR, 0)))
            work_day_end = tz.localize(datetime.datetime.combine(day_date, datetime.time(WORK_END_HOUR, 0)))

            # Oblicz przecięcie wolnego zakresu z godzinami pracy w danym dniu
            intersect_start = max(current_day_start, work_day_start)
            intersect_end = min(range_end, work_day_end)

            # Upewnij się, że przecięcie jest poprawne i wystarczająco długie
            if intersect_start < intersect_end and (intersect_end - intersect_start) >= min_duration_delta:
                # Zaokrąglanie początku slotu do najbliższych 10 minut W GÓRĘ
                if intersect_start.minute % 10 != 0 or intersect_start.second > 0 or intersect_start.microsecond > 0:
                    minutes_to_add = 10 - (intersect_start.minute % 10)
                    rounded_start = intersect_start + datetime.timedelta(minutes=minutes_to_add)
                    rounded_start = rounded_start.replace(second=0, microsecond=0)
                else:
                    rounded_start = intersect_start # Już jest na granicy 10 minut

                # Sprawdź, czy po zaokrągleniu slot jest nadal wystarczająco długi
                if rounded_start < intersect_end and (intersect_end - rounded_start) >= min_duration_delta:
                    # Dodaj ten prawidłowy, zaokrąglony slot do wyników
                     final_free_slots.append({'start': rounded_start, 'end': intersect_end})


            # Przejdź do następnego dnia (początek następnego dnia)
            next_day_date = day_date + datetime.timedelta(days=1)
            current_day_start = tz.localize(datetime.datetime.combine(next_day_date, datetime.time(0, 0)))
            # Upewnij się, że nie wyjdziemy poza koniec oryginalnego wolnego zakresu
            current_day_start = max(current_day_start, range_start) # Na wypadek dziwnych przejść czasowych


    logging.info(f"Znaleziono {len(final_free_slots)} wolnych zakresów czasowych (po filtrze godzin pracy i zaokrągleniu).")
    # Można dodać logowanie zakresów dla debugowania:
    # for slot in final_free_slots:
    #    logging.debug(f"  - Wolny zakres: {slot['start']:%Y-%m-%d %H:%M} do {slot['end']:%Y-%m-%d %H:%M}")

    return final_free_slots


def is_slot_actually_free(start_time, calendar_id):
    """Weryfikuje w czasie rzeczywistym, czy konkretny slot wizyty jest nadal wolny."""
    service = get_calendar_service()
    tz = _get_timezone()
    if not service:
        logging.error("Błąd: Usługa kalendarza niedostępna do weryfikacji slotu.")
        return False # Zakładamy, że nie jest wolny, jeśli nie możemy sprawdzić

    # Upewnij się, że czas startu jest świadomy strefy czasowej
    if start_time.tzinfo is None:
        start_time = tz.localize(start_time)
    else:
        start_time = start_time.astimezone(tz)

    end_time = start_time + datetime.timedelta(minutes=APPOINTMENT_DURATION_MINUTES)

    # Użyj Freebusy API do sprawdzenia zajętości dokładnie w tym przedziale
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
            # Sprawdź, czy zajętość faktycznie koliduje (freebusy może zwrócić sloty stykające się)
            for busy in busy_times:
                busy_start = datetime.datetime.fromisoformat(busy['start']).astimezone(tz)
                busy_end = datetime.datetime.fromisoformat(busy['end']).astimezone(tz)
                # Sprawdzenie, czy jest jakakolwiek część wspólna
                if max(start_time, busy_start) < min(end_time, busy_end):
                    logging.warning(f"Weryfikacja: Slot {start_time:%Y-%m-%d %H:%M} jest ZAJĘTY (kolizja z {busy_start:%H:%M}-{busy_end:%H:%M}).")
                    return False
            # Jeśli pętla się zakończyła, żadna zajętość nie kolidowała (np. tylko stykały się końcami)
            logging.info(f"Weryfikacja: Slot {start_time:%Y-%m-%d %H:%M} jest POTWIERDZONY jako wolny (zajętości nie kolidują).")
            return True

    except HttpError as error:
        logging.error(f'Błąd API Google Calendar (Freebusy) podczas weryfikacji slotu: {error}', exc_info=True)
        return False # Bezpieczniej założyć, że zajęty
    except Exception as e:
        logging.error(f"Nieoczekiwany błąd podczas weryfikacji slotu przez Freebusy: {e}", exc_info=True)
        return False # Bezpieczniej założyć, że zajęty


def book_appointment(calendar_id, start_time, end_time, summary="Rezerwacja FB", description="", user_name=""):
    """Rezerwuje termin w Kalendarzu Google."""
    service = get_calendar_service()
    tz = _get_timezone()
    if not service:
        return False, "Błąd: Brak połączenia z usługą kalendarza. Nie można zarezerwować terminu."

    # Upewnij się, że czasy są świadome strefy czasowej
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
                {'method': 'popup', 'minutes': 60}, # Przypomnienie na godzinę przed
            ],
        },
        # 'conferenceData': { # Opcjonalnie: Dodaj link do Google Meet
        #     'createRequest': {
        #         'requestId': f"meet_{user_psid}_{int(time.time())}", # Unikalne ID żądania
        #         'conferenceSolutionKey': {'type': 'hangoutsMeet'}
        #     }
        # },
        'status': 'confirmed', # Oznacz jako potwierdzone
    }

    try:
        logging.info(f"Próba rezerwacji: '{event_summary}' od {start_time:%Y-%m-%d %H:%M} do {end_time:%Y-%m-%d %H:%M}")
        # Wstawienie wydarzenia
        created_event = service.events().insert(calendarId=calendar_id, body=event, conferenceDataVersion=1).execute() # conferenceDataVersion=1 jeśli używasz Meet
        event_id = created_event.get('id')
        logging.info(f"Termin zarezerwowany pomyślnie. ID wydarzenia: {event_id}")

        # Przygotowanie wiadomości potwierdzającej dla użytkownika
        day_index = start_time.weekday()
        locale_day_name = POLISH_WEEKDAYS[day_index]
        hour_str = str(start_time.hour) # Godzina bez wiodącego zera
        confirm_message = f"Świetnie! Termin na {locale_day_name}, {start_time.strftime(f'%d.%m.%Y o {hour_str}:%M')} został zarezerwowany."
        # Można dodać link do Meet, jeśli został wygenerowany:
        # meet_link = created_event.get('hangoutLink')
        # if meet_link:
        #    confirm_message += f"\nLink do spotkania: {meet_link}"

        return True, confirm_message

    except HttpError as error:
        error_details = f"Kod: {error.resp.status}, Powód: {error.resp.reason}"
        try:
            error_content = json.loads(error.content.decode('utf-8'))
            error_message = error_content.get('error', {}).get('message', '')
            if error_message:
                 error_details += f" - {error_message}"
        except:
            pass # Błąd dekodowania lub brak szczegółów błędu

        logging.error(f"Błąd API Google Calendar podczas rezerwacji: {error}, Szczegóły: {error_details}", exc_info=True)

        # Zwracanie bardziej szczegółowych komunikatów o błędach
        if error.resp.status == 409: # Konflikt - termin już zajęty
            return False, "Niestety, wygląda na to, że ten termin został właśnie zajęty. Spróbujmy znaleźć inny."
        elif error.resp.status == 403: # Brak uprawnień
            return False, "Wystąpił problem z uprawnieniami do zapisu w kalendarzu. Skontaktuj się z administratorem."
        elif error.resp.status == 404: # Nie znaleziono kalendarza
            return False, f"Nie znaleziono kalendarza docelowego ('{calendar_id}'). Skontaktuj się z administratorem."
        elif error.resp.status == 400: # Złe żądanie (np. nieprawidłowe dane)
            return False, f"Wystąpił błąd danych podczas próby rezerwacji. Sprawdź poprawność konfiguracji. ({error_details})"
        else: # Inny błąd API
            return False, f"Wystąpił nieoczekiwany błąd ({error.resp.status}) podczas rezerwacji terminu w kalendarzu."

    except Exception as e:
        logging.error(f"Nieoczekiwany błąd Python podczas rezerwacji: {e}", exc_info=True)
        return False, "Wystąpił wewnętrzny błąd systemu podczas próby rezerwacji terminu. Przepraszam za kłopot."


def format_ranges_for_ai(ranges):
    """Formatuje listę zakresów czasowych na czytelny tekst dla AI."""
    if not ranges:
        return "Brak dostępnych zakresów czasowych do zaproponowania."

    ranges_by_date = defaultdict(list)
    tz = _get_timezone()
    for r in ranges:
        range_date = r['start'].date()
        # Upewnij się, że czasy są w poprawnej strefie czasowej
        start_time = r['start'].astimezone(tz)
        end_time = r['end'].astimezone(tz)
        ranges_by_date[range_date].append({
            'start_time': start_time.strftime('%H:%M'),
            'end_time': end_time.strftime('%H:%M')
        })

    formatted_lines = [
        f"Poniżej znajdują się dostępne ZAKRESY czasowe, w których można umówić wizytę (czas trwania: {APPOINTMENT_DURATION_MINUTES} minut).",
        "Twoim zadaniem jest wybrać JEDEN zakres, a następnie wygenerować z niego DOKŁADNY czas rozpoczęcia wizyty (np. 16:00, 17:30), biorąc pod uwagę preferencje z historii rozmowy.",
        "Pamiętaj, aby wygenerowany czas + czas trwania wizyty mieścił się w wybranym zakresie.",
        "Dołącz wygenerowany czas w formacie ISO w znaczniku [SLOT_ISO:...].",
        "--- Dostępne Zakresy ---"
    ]
    dates_added = 0
    max_dates_to_show = 7 # Ograniczenie liczby dni pokazywanych AI

    for d in sorted(ranges_by_date.keys()):
        day_name = POLISH_WEEKDAYS[d.weekday()]
        date_str = d.strftime('%d.%m.%Y') # Format DD.MM.YYYY
        # Sortuj przedziały czasowe dla danego dnia
        time_ranges_str = '; '.join(
            f"{tr['start_time']}-{tr['end_time']}"
            for tr in sorted(ranges_by_date[d], key=lambda x: x['start_time'])
        )
        if time_ranges_str:
            formatted_lines.append(f"- {day_name}, {date_str}: {time_ranges_str}")
            dates_added += 1
            if dates_added >= max_dates_to_show:
                formatted_lines.append("- ... (i potencjalnie więcej w kolejnych dniach)")
                break

    if dates_added == 0:
        return "Brak dostępnych zakresów czasowych w godzinach pracy."

    return "\n".join(formatted_lines)


def format_slot_for_user(slot_start):
    """Formatuje pojedynczy slot (datetime) na czytelny tekst dla użytkownika."""
    if not isinstance(slot_start, datetime.datetime):
        logging.warning(f"Próba formatowania niepoprawnego typu slotu: {type(slot_start)}")
        return "[Błąd formatowania daty]"
    try:
        tz = _get_timezone()
        # Upewnij się, że slot jest w odpowiedniej strefie czasowej
        if slot_start.tzinfo is None:
            slot_start = tz.localize(slot_start)
        else:
            slot_start = slot_start.astimezone(tz)

        day_index = slot_start.weekday()
        day_name = POLISH_WEEKDAYS[day_index]
        hour_str = str(slot_start.hour)  # Godzina bez wiodącego zera
        return f"{day_name}, {slot_start.strftime(f'%d.%m.%Y o {hour_str}:%M')}"
    except Exception as e:
        logging.error(f"Błąd formatowania slotu {slot_start}: {e}", exc_info=True)
        # Zwróć format ISO jako fallback
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
    # Aplikacja może działać dalej, ale bez AI. Zgłoszenie błędu jest kluczowe.


# --- Funkcje wysyłania wiadomości FB ---
def _send_typing_on(recipient_id):
    """Wysyła wskaźnik 'pisania' do użytkownika."""
    if not PAGE_ACCESS_TOKEN or len(PAGE_ACCESS_TOKEN) < 50:
        return # Cicho ignoruj, jeśli brak tokena
    if not ENABLE_TYPING_DELAY:
        return # Nie wysyłaj, jeśli funkcja wyłączona

    logging.debug(f"[{recipient_id}] Wysyłanie 'typing_on'")
    params = {"access_token": PAGE_ACCESS_TOKEN}
    payload = {"recipient": {"id": recipient_id}, "sender_action": "typing_on"}
    try:
        requests.post(FACEBOOK_GRAPH_API_URL, params=params, json=payload, timeout=5)
    except requests.exceptions.RequestException as e:
        # Błędy 'typing_on' są niskiego priorytetu, loguj jako warning
        logging.warning(f"[{recipient_id}] Błąd podczas wysyłania 'typing_on': {e}")

def _send_single_message(recipient_id, message_text):
    """Wysyła pojedynczy fragment wiadomości przez Facebook Graph API."""
    logging.info(f"--- Wysyłanie fragmentu do {recipient_id} (długość: {len(message_text)}) ---")
    # logging.debug(f"Pełna treść fragmentu: {message_text}") # Opcjonalnie do debugowania

    if not PAGE_ACCESS_TOKEN or len(PAGE_ACCESS_TOKEN) < 50:
        logging.error(f"!!! [{recipient_id}] Brak lub nieprawidłowy PAGE_ACCESS_TOKEN. Wiadomość NIE WYSŁANA.")
        return False

    params = {"access_token": PAGE_ACCESS_TOKEN}
    payload = {
        "recipient": {"id": recipient_id},
        "message": {"text": message_text},
        "messaging_type": "RESPONSE" # Standardowy typ odpowiedzi
    }

    try:
        r = requests.post(FACEBOOK_GRAPH_API_URL, params=params, json=payload, timeout=30)
        r.raise_for_status() # Zgłosi błąd dla statusów 4xx/5xx
        response_json = r.json()
        if response_json.get('error'):
            # Obsługa błędów specyficznych dla API Facebooka
            fb_error = response_json['error']
            logging.error(f"!!! BŁĄD FB API podczas wysyłania do {recipient_id}: {fb_error} !!!")
            # Można dodać logikę specyficzną dla kodów błędów FB, np. 100 (invalid parameter), 10 (permission denied)
            return False
        logging.debug(f"[{recipient_id}] Fragment wysłany pomyślnie. Response: {response_json}")
        return True
    except requests.exceptions.Timeout:
        logging.error(f"!!! BŁĄD TIMEOUT podczas wysyłania do {recipient_id} !!!")
        return False
    except requests.exceptions.HTTPError as e:
        # Błędy HTTP (np. 400, 403, 500)
        logging.error(f"!!! BŁĄD HTTP {e.response.status_code} podczas wysyłania do {recipient_id}: {e} !!!")
        if e.response is not None:
            try:
                logging.error(f"Odpowiedź FB (błąd HTTP): {e.response.json()}")
            except json.JSONDecodeError:
                logging.error(f"Odpowiedź FB (błąd HTTP, nie JSON): {e.response.text}")
        return False
    except requests.exceptions.RequestException as e:
        # Inne błędy sieciowe (np. problem z połączeniem)
        logging.error(f"!!! BŁĄD sieciowy podczas wysyłania do {recipient_id}: {e} !!!")
        return False
    except Exception as e:
        # Inne nieoczekiwane błędy
        logging.error(f"!!! Nieoczekiwany BŁĄD podczas wysyłania do {recipient_id}: {e} !!!", exc_info=True)
        return False

def send_message(recipient_id, full_message_text):
    """Wysyła wiadomość do użytkownika, dzieląc ją na fragmenty, jeśli jest za długa."""
    if not full_message_text or not isinstance(full_message_text, str) or not full_message_text.strip():
        logging.warning(f"[{recipient_id}] Pominięto próbę wysłania pustej wiadomości.")
        return

    message_len = len(full_message_text)
    logging.info(f"[{recipient_id}] Przygotowanie wiadomości do wysłania (długość: {message_len}).")

    # Symulacja pisania przed wysłaniem pierwszej części
    if ENABLE_TYPING_DELAY:
        # Oblicz szacowany czas pisania
        estimated_typing_duration = min(
            MAX_TYPING_DELAY_SECONDS,
            max(MIN_TYPING_DELAY_SECONDS, message_len / TYPING_CHARS_PER_SECOND)
        )
        logging.debug(f"[{recipient_id}] Szacowany czas pisania: {estimated_typing_duration:.2f}s")
        _send_typing_on(recipient_id)
        time.sleep(estimated_typing_duration)

    # Podział wiadomości na fragmenty
    chunks = []
    if message_len <= MESSAGE_CHAR_LIMIT:
        chunks.append(full_message_text)
    else:
        logging.info(f"[{recipient_id}] Wiadomość za długa ({message_len} > {MESSAGE_CHAR_LIMIT}). Dzielenie na fragmenty...")
        remaining_text = full_message_text
        while remaining_text:
            if len(remaining_text) <= MESSAGE_CHAR_LIMIT:
                chunks.append(remaining_text.strip())
                break

            split_index = -1
            # Szukaj miejsca podziału w preferowanej kolejności (najpierw nowe linie, potem zdania, potem spacje)
            possible_delimiters = ['\n\n', '\n', '. ', '! ', '? ', ' ']# Można dodać '; ', ', '
            for delimiter in possible_delimiters:
                # Szukaj od końca w dozwolonym zakresie
                search_limit = MESSAGE_CHAR_LIMIT - len(delimiter) + 1
                temp_index = remaining_text.rfind(delimiter, 0, search_limit)
                if temp_index != -1:
                    # Znaleziono dobry punkt podziału
                    split_index = temp_index + len(delimiter)
                    break

            # Jeśli nie znaleziono naturalnego miejsca podziału, tnij na siłę
            if split_index == -1:
                split_index = MESSAGE_CHAR_LIMIT

            chunk = remaining_text[:split_index].strip()
            if chunk: # Dodaj tylko niepuste fragmenty
                chunks.append(chunk)
            remaining_text = remaining_text[split_index:].strip()

        logging.info(f"[{recipient_id}] Podzielono wiadomość na {len(chunks)} fragmentów.")

    # Wysyłanie fragmentów
    num_chunks = len(chunks)
    send_success_count = 0
    for i, chunk in enumerate(chunks):
        logging.debug(f"[{recipient_id}] Wysyłanie fragmentu {i+1}/{num_chunks} (długość: {len(chunk)})...")
        if not _send_single_message(recipient_id, chunk):
            logging.error(f"!!! [{recipient_id}] Błąd podczas wysyłania fragmentu {i+1}. Anulowano wysyłanie reszty. !!!")
            break # Przerwij wysyłanie kolejnych fragmentów po błędzie
        send_success_count += 1

        # Opóźnienie między fragmentami (jeśli jest więcej niż jeden i nie jest to ostatni)
        if num_chunks > 1 and i < num_chunks - 1:
            logging.debug(f"[{recipient_id}] Oczekiwanie {MESSAGE_DELAY_SECONDS}s przed kolejnym fragmentem...")
            # Symulacja pisania przed kolejnym fragmentem
            if ENABLE_TYPING_DELAY:
                 # Krótsza symulacja pisania między fragmentami
                estimated_typing_duration = min(MAX_TYPING_DELAY_SECONDS, max(MIN_TYPING_DELAY_SECONDS, len(chunks[i+1]) / TYPING_CHARS_PER_SECOND)) * 0.5
                _send_typing_on(recipient_id)
                time.sleep(estimated_typing_duration + MESSAGE_DELAY_SECONDS * 0.5) # Dodaj część opóźnienia do pisania
                time.sleep(MESSAGE_DELAY_SECONDS * 0.5) # Pozostała część opóźnienia
            else:
                 time.sleep(MESSAGE_DELAY_SECONDS)


    logging.info(f"--- [{recipient_id}] Zakończono wysyłanie wiadomości. Wysłano {send_success_count}/{num_chunks} fragmentów. ---")

# --- Funkcja do symulowania pisania (używana np. podczas dłuższych operacji) ---
def _simulate_typing(recipient_id, duration_seconds):
    """Wysyła 'typing_on' i czeka przez określony czas."""
    if ENABLE_TYPING_DELAY and duration_seconds > 0:
        _send_typing_on(recipient_id)
        time.sleep(min(duration_seconds, MAX_TYPING_DELAY_SECONDS)) # Ogranicz maksymalny czas symulacji


# --- Ogólna funkcja do wywoływania API Gemini ---
def _call_gemini(user_psid, prompt_history, generation_config, task_name, max_retries=3):
    """Wywołuje API Gemini z podaną historią i konfiguracją."""
    if not gemini_model:
        logging.error(f"!!! [{user_psid}] Model Gemini ({task_name}) niezaładowany. Nie można wywołać API.")
        return None # Zwróć None, jeśli model jest niedostępny

    # Przygotowanie promptu - upewnij się, że to lista obiektów Content
    if not isinstance(prompt_history, list) or not all(isinstance(item, Content) for item in prompt_history):
        logging.error(f"!!! [{user_psid}] Nieprawidłowy format promptu dla Gemini ({task_name}). Oczekiwano listy Content.")
        return None

    # Logowanie promptu (opcjonalnie, może zawierać dane wrażliwe)
    # history_debug = [{"role": m.role, "parts": [{"text": p.text} for p in m.parts]} for m in prompt_history]
    # logging.debug(f"[{user_psid}] Prompt dla Gemini ({task_name}):\n{json.dumps(history_debug, indent=2, ensure_ascii=False)}")
    logging.info(f"[{user_psid}] Wywołanie Gemini dla zadania: {task_name} (Prompt: {len(prompt_history)} wiadomości)")


    attempt = 0
    while attempt < max_retries:
        attempt += 1
        logging.debug(f"    Próba {attempt}/{max_retries} wywołania Gemini ({task_name})...")
        try:
            # Symulacja pisania przed wywołaniem API
            _simulate_typing(user_psid, MIN_TYPING_DELAY_SECONDS)

            # Wywołanie API Vertex AI Gemini
            response = gemini_model.generate_content(
                prompt_history,
                generation_config=generation_config,
                safety_settings=SAFETY_SETTINGS
                # stream=False # Używamy trybu bez strumieniowania
            )

            # Analiza odpowiedzi
            if response and response.candidates:
                # Sprawdzenie powodu zablokowania przez filtry bezpieczeństwa
                finish_reason = response.candidates[0].finish_reason
                if finish_reason != 1: # 1 oznacza STOP (normalne zakończenie)
                    safety_ratings = response.candidates[0].safety_ratings
                    logging.warning(f"[{user_psid}] Odpowiedź Gemini ({task_name}) ZABLOKOWANA lub NIEDOKOŃCZONA! Powód: {finish_reason}. Oceny bezpieczeństwa: {safety_ratings}")
                    # Można zwrócić generyczną odpowiedź o problemie z bezpieczeństwem
                    # return "Przepraszam, Twoja prośba lub nasza odpowiedź mogła naruszyć zasady bezpieczeństwa. Spróbuj sformułować to inaczej."
                    # Lub próbować ponownie (choć jeśli prompt narusza zasady, ponowna próba może nie pomóc)
                    if attempt < max_retries:
                        logging.warning(f"    Ponawianie próby ({attempt}/{max_retries}) po blokadzie bezpieczeństwa...")
                        time.sleep(1 * attempt)
                        continue
                    else:
                         logging.error(f"!!! [{user_psid}] Gemini ({task_name}) - nie udało się po blokadzie bezpieczeństwa po {max_retries} próbach.")
                         return "Przepraszam, wystąpił problem z przetworzeniem Twojej wiadomości z powodu zasad bezpieczeństwa."


                # Pomyślna odpowiedź
                if response.candidates[0].content and response.candidates[0].content.parts:
                    generated_text = "".join(part.text for part in response.candidates[0].content.parts if hasattr(part, 'text'))
                    logging.info(f"[{user_psid}] Gemini ({task_name}) zwróciło odpowiedź (dł: {len(generated_text)}).")
                    # logging.debug(f"Pełna odp. Gemini: {generated_text}")
                    return generated_text.strip()
                else:
                    logging.warning(f"[{user_psid}] Gemini ({task_name}) zwróciło kandydata, ale bez treści (puste 'parts'). Response: {response}")
                    # Traktuj to jako błąd i spróbuj ponownie
            else:
                 # Brak kandydatów w odpowiedzi - nietypowa sytuacja
                 prompt_feedback = response.prompt_feedback if hasattr(response, 'prompt_feedback') else 'Brak prompt_feedback'
                 logging.error(f"!!! BŁĄD [{user_psid}] Gemini ({task_name}) - Brak kandydatów w odpowiedzi. Prompt feedback: {prompt_feedback}. Pełna odpowiedź: {response}")


        except HttpError as http_err:
             # Błędy specyficzne dla Google API (np. quota, auth)
             logging.error(f"!!! BŁĄD HTTP ({http_err.resp.status}) [{user_psid}] Gemini ({task_name}) - Próba {attempt}/{max_retries}: {http_err}", exc_info=True)
             # Można dodać logikę retry dla błędów 429 (Quota exceeded) lub 5xx (Server error)
             if http_err.resp.status in [429, 500, 503] and attempt < max_retries:
                  sleep_time = (2 ** attempt) + (random.random() * 0.1) # Exponential backoff
                  logging.warning(f"    Oczekiwanie {sleep_time:.2f}s przed ponowieniem próby...")
                  time.sleep(sleep_time)
                  continue # Ponów próbę
             else:
                  break # Nie ponawiaj dla innych błędów HTTP lub po osiągnięciu limitu prób
        except Exception as e:
             # Inne nieoczekiwane błędy podczas wywołania API
             logging.error(f"!!! BŁĄD [{user_psid}] Gemini ({task_name}) - Nieoczekiwany błąd API (Próba {attempt}/{max_retries}): {e}", exc_info=True)

        # Jeśli pętla dotarła tutaj bez zwrócenia odpowiedzi, oznacza to błąd lub osiągnięcie limitu prób
        if attempt < max_retries:
             logging.warning(f"    Oczekiwanie przed ponowieniem próby ({attempt+1}/{max_retries})...")
             time.sleep(1.5 * attempt) # Krótkie oczekiwanie przed kolejną próbą

    # Jeśli wszystkie próby zawiodły
    logging.error(f"!!! KRYTYCZNY BŁĄD [{user_psid}] Gemini ({task_name}) - Nie udało się uzyskać poprawnej odpowiedzi po {max_retries} próbach.")
    return None # Zwróć None po nieudanych wszystkich próbach


# --- INSTRUKCJA SYSTEMOWA (dla AI proponującego termin) ---
# ZMIANA: Podkreślenie analizy całej historii i feedbacku
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

**Przykład (dostępny zakres "Środa, 07.05.2025: 16:00-18:30", historia zawiera feedback "wolałbym coś koło 17"):**
*   Dobry wynik: "Ok, może w takim razie pasowałaby Środa, 07.05.2025 o 17:00? {slot_marker_prefix}2025-05-07T17:00:00+02:00{slot_marker_suffix}"

**Zasady:** Zawsze generuj tylko JEDEN termin. Zawsze sprawdzaj, czy mieści się w zakresie. Zawsze dołączaj znacznik ISO na końcu. Opieraj wybór na historii rozmowy. Bądź uprzejmy.
""".format(
    available_ranges_text="{available_ranges_text}", # Zostaw jako placeholder
    pref_weekday=PREFERRED_WEEKDAY_START_HOUR,
    pref_weekend=PREFERRED_WEEKEND_START_HOUR,
    duration=APPOINTMENT_DURATION_MINUTES,
    slot_marker_prefix=SLOT_ISO_MARKER_PREFIX,
    slot_marker_suffix=SLOT_ISO_MARKER_SUFFIX
)
# ---------------------------------------------------------------------


# --- INSTRUKCJA SYSTEMOWA (dla AI interpretującego feedback) ---
# Bez zmian, ta instrukcja działała dobrze
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
# Bez zmian, ta instrukcja działała dobrze
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
    """
    if not gemini_model:
        logging.error(f"!!! [{user_psid}] Model Gemini niezaładowany! Nie można wygenerować propozycji slotu.")
        return None, None # Błąd krytyczny

    if not available_ranges:
        logging.warning(f"[{user_psid}]: Brak dostępnych zakresów do przekazania AI do propozycji.")
        # Zwracamy tekst błędu, ale bez ISO
        return "Niestety, w tym momencie nie widzę żadnych dostępnych zakresów czasowych.", None

    # Sformatuj dostępne zakresy dla AI
    ranges_text = format_ranges_for_ai(available_ranges)

    # Przygotuj prompt: Instrukcja systemowa + Historia (user/model)
    # Instrukcja jest formatowana z aktualnymi zakresami
    system_instruction = SYSTEM_INSTRUCTION_TEXT_PROPOSE.format(available_ranges_text=ranges_text)
    initial_prompt = [
        Content(role="user", parts=[Part.from_text(system_instruction)]),
        Content(role="model", parts=[Part.from_text("Rozumiem. Przeanalizuję historię i dostępne zakresy, aby zaproponować jeden konkretny termin i dołączę go w wymaganym formacie.")])
    ]

    # Połącz instrukcję z faktyczną historią rozmowy
    full_prompt = initial_prompt + history_for_proposal_ai

    # Ogranicz długość historii przekazywanej do AI (opcjonalnie, ale zalecane)
    # Usuwa najstarsze wiadomości (poza instrukcją) jeśli przekroczono limit
    max_prompt_messages = (MAX_HISTORY_TURNS * 2) + 2 # +2 dla instrukcji user/model
    while len(full_prompt) > max_prompt_messages:
        full_prompt.pop(2) # Usuń najstarszą wiadomość użytkownika (indeks 2)
        if len(full_prompt) > 2:
            full_prompt.pop(2) # Usuń odpowiadającą jej wiadomość modelu (teraz na indeksie 2)


    # Wywołaj Gemini, aby wygenerowało propozycję
    generated_text = _call_gemini(user_psid, full_prompt, GENERATION_CONFIG_PROPOSAL, "Slot Proposal")

    if not generated_text:
        logging.error(f"!!! [{user_psid}] Nie udało się uzyskać odpowiedzi od Gemini dla propozycji slotu.")
        # Można zwrócić generyczny błąd lub próbować ponownie z innym promptem
        return "Przepraszam, mam chwilowy problem z systemem proponowania terminów.", None

    # Parsowanie odpowiedzi AI w poszukiwaniu znacznika ISO
    iso_match = re.search(rf"{re.escape(SLOT_ISO_MARKER_PREFIX)}(.*?){re.escape(SLOT_ISO_MARKER_SUFFIX)}", generated_text)

    if iso_match:
        extracted_iso = iso_match.group(1).strip()
        # Usuń znacznik ISO z tekstu, który zobaczy użytkownik
        text_for_user = re.sub(rf"{re.escape(SLOT_ISO_MARKER_PREFIX)}.*?{re.escape(SLOT_ISO_MARKER_SUFFIX)}", "", generated_text).strip()
        # Usuń ewentualne wielokrotne spacje
        text_for_user = re.sub(r'\s+', ' ', text_for_user).strip()

        logging.info(f"[{user_psid}] AI wygenerowało propozycję. ISO: {extracted_iso}. Tekst: '{text_for_user}'")

        # --- Walidacja wygenerowanego slotu ---
        try:
            tz = _get_timezone()
            proposed_start = datetime.datetime.fromisoformat(extracted_iso).astimezone(tz)
            proposed_end = proposed_start + datetime.timedelta(minutes=APPOINTMENT_DURATION_MINUTES)

            # 1. Sprawdź, czy mieści się w jednym z DOSTARCZONYCH zakresów
            is_within_provided_ranges = False
            for r in available_ranges:
                if r['start'] <= proposed_start and proposed_end <= r['end']:
                    is_within_provided_ranges = True
                    break

            if not is_within_provided_ranges:
                 logging.error(f"!!! BŁĄD Walidacji AI [{user_psid}]: Wygenerowany ISO '{extracted_iso}' (start: {proposed_start:%H:%M}) nie mieści się w żadnym z dostępnych zakresów przekazanych AI!")
                 # To jest błąd logiki AI, zwracamy błąd do użytkownika
                 return "Przepraszam, wystąpił błąd podczas wybierania terminu z dostępnych opcji. Spróbujmy jeszcze raz.", None

            # 2. Dodatkowa weryfikacja w czasie rzeczywistym z Google Calendar API (minimalizuje ryzyko konfliktu)
            if is_slot_actually_free(proposed_start, TARGET_CALENDAR_ID):
                 # Slot jest poprawny i wolny
                 return text_for_user, extracted_iso
            else:
                 logging.warning(f"!!! [{user_psid}]: Wygenerowany przez AI slot {extracted_iso} okazał się ZAJĘTY po weryfikacji w czasie rzeczywistym! Szukanie nowego terminu.")
                 # Jeśli slot jest zajęty, nie zwracamy go, co spowoduje ponowne wyszukanie
                 # Można też od razu wywołać ponowne szukanie tutaj, ale obecna logika webhooka to obsłuży
                 return "Wygląda na to, że proponowany przed chwilą termin właśnie się zajął. Szukam kolejnej opcji...", None # Zwracamy info i None ISO

        except ValueError:
            logging.error(f"!!! BŁĄD AI [{user_psid}]: Wygenerowany ciąg '{extracted_iso}' nie jest poprawnym formatem ISO 8601!")
            return "Przepraszam, wystąpił błąd podczas przetwarzania proponowanego terminu. Spróbujmy ponownie.", None
        except Exception as val_err:
            logging.error(f"!!! BŁĄD Walidacji AI [{user_psid}]: Nieoczekiwany błąd podczas walidacji slotu {extracted_iso}: {val_err}", exc_info=True)
            return "Przepraszam, wystąpił wewnętrzny błąd systemu podczas weryfikacji terminu.", None
    else:
        # AI nie zwróciło znacznika ISO
        logging.error(f"!!! BŁĄD AI [{user_psid}]: Brak znacznika ISO w odpowiedzi AI dla propozycji slotu! Odpowiedź: '{generated_text}'")
        # Zwróć sam tekst odpowiedzi AI, jeśli jest sensowny, ale bez możliwości rezerwacji
        # Lub zwróć generyczny błąd
        return "Przepraszam, mam problem z wygenerowaniem propozycji terminu w odpowiednim formacie.", None


# --- Funkcja interakcji z Gemini (interpretacja feedbacku) ---
def get_gemini_feedback_decision(user_psid, user_feedback_text, history_for_feedback_ai, last_proposed_slot_text):
     """
     Prosi AI o zinterpretowanie odpowiedzi użytkownika na propozycję terminu i zwrócenie znacznika decyzji.
     """
     if not gemini_model:
        logging.error(f"!!! [{user_psid}] Model Gemini niezaładowany! Nie można zinterpretować feedbacku.")
        return "[CLARIFY]" # Domyślnie niejasna odpowiedź w razie błędu

     # Formatowanie instrukcji dla AI z aktualnymi danymi
     instruction = SYSTEM_INSTRUCTION_TEXT_FEEDBACK.format(
         last_proposal_text=last_proposed_slot_text,
         user_feedback=user_feedback_text
     )

     # Przygotowanie promptu: Instrukcja + Historia (bez ostatniego kontekstu) + Aktualna odpowiedź usera
     prompt = [Content(role="user", parts=[Part.from_text(instruction)])]

     # Dodaj historię (user/model), ograniczając jej długość
     max_hist_messages = (MAX_HISTORY_TURNS - 1) * 2 # Mniej historii, bo prompt zawiera instrukcję i feedback
     if len(history_for_feedback_ai) > max_hist_messages:
         prompt.extend(history_for_feedback_ai[-max_hist_messages:])
     else:
         prompt.extend(history_for_feedback_ai)

     # Dodaj aktualną odpowiedź użytkownika na końcu
     prompt.append(Content(role="user", parts=[Part.from_text(user_feedback_text)]))

     # Wywołaj Gemini
     decision_tag = _call_gemini(user_psid, prompt, GENERATION_CONFIG_FEEDBACK, "Feedback Interpretation")

     if not decision_tag:
         logging.error(f"!!! [{user_psid}] Nie udało się uzyskać odpowiedzi od Gemini dla interpretacji feedbacku.")
         return "[CLARIFY]" # Błąd API - załóż, że niejasne

     # Podstawowa walidacja formatu znacznika
     if not (decision_tag.startswith("[") and decision_tag.endswith("]")):
          logging.warning(f"Ostrz. [{user_psid}]: AI (Feedback) nie zwróciło poprawnego formatu znacznika: '{decision_tag}'. Traktuję jako CLARIFY.")
          return "[CLARIFY]"

     # Bardziej szczegółowa walidacja zawartości znacznika
     # (Sprawdzenie poprawności nazw dni, godzin itp.)
     try:
         tag_parts = decision_tag[1:-1].split(' ') # Podziel np. "[REJECT_FIND_NEXT PREFERENCE='specific_day' DAY='Środa']"
         tag_name = tag_parts[0]

         # Sprawdź podstawowe typy
         if tag_name in ['ACCEPT', 'CLARIFY']:
              pass # Te są proste
         elif tag_name == 'REJECT_FIND_NEXT':
              params = {}
              for part in tag_parts[1:]:
                   if '=' in part:
                       key, value = part.split('=', 1)
                       params[key.upper()] = value.strip("'") # Klucze dużymi literami, wartość bez apostrofów

              preference = params.get('PREFERENCE')
              if not preference:
                   raise ValueError("Brak parametru PREFERENCE w znaczniku REJECT_FIND_NEXT")

              # Walidacja dla preferencji wymagających dnia
              if preference in ['specific_day', 'specific_datetime', 'specific_day_later', 'specific_day_earlier']:
                  day = params.get('DAY')
                  if not day or day.capitalize() not in POLISH_WEEKDAYS:
                       raise ValueError(f"Nieprawidłowy lub brakujący DAY='{day}' dla preferencji {preference}")
                  # Popraw wielkość liter w zwróconym znaczniku dla spójności
                  decision_tag = decision_tag.replace(f"DAY='{day}'", f"DAY='{day.capitalize()}'")


              # Walidacja dla preferencji wymagających godziny
              if preference in ['specific_hour', 'specific_datetime']:
                  hour_str = params.get('HOUR')
                  if not hour_str or not hour_str.isdigit() or not (0 <= int(hour_str) <= 23):
                       raise ValueError(f"Nieprawidłowy lub brakujący HOUR='{hour_str}' dla preferencji {preference}")

              # Można dodać walidację dla innych preferencji, jeśli powstaną

         else:
              raise ValueError(f"Nieznany główny typ znacznika: {tag_name}")

         # Jeśli walidacja przeszła pomyślnie
         logging.info(f"[{user_psid}] Zwalidowana decyzja AI (Feedback): {decision_tag}")
         return decision_tag

     except ValueError as e:
         logging.warning(f"Ostrz. [{user_psid}]: Błąd walidacji znacznika feedbacku '{decision_tag}': {e}. Traktuję jako CLARIFY.")
         return "[CLARIFY]"
     except Exception as e_val:
          logging.error(f"!!! [{user_psid}]: Nieoczekiwany błąd podczas walidacji znacznika feedbacku '{decision_tag}': {e_val}", exc_info=True)
          return "[CLARIFY]"


# --- Funkcja interakcji z Gemini (ogólna rozmowa) ---
def get_gemini_general_response(user_psid, current_user_message_text, history_for_general_ai):
    """
    Prowadzi ogólną rozmowę z AI, zwraca odpowiedź tekstową.
    """
    if not gemini_model:
        logging.error(f"!!! [{user_psid}] Model Gemini niezaładowany! Nie można prowadzić ogólnej rozmowy.")
        return "Przepraszam, mam chwilowy problem z systemem i nie mogę teraz odpowiedzieć."

    # Przygotowanie promptu: Instrukcja + Historia + Aktualna wiadomość
    initial_prompt = [
        Content(role="user", parts=[Part.from_text(SYSTEM_INSTRUCTION_GENERAL)]),
        Content(role="model", parts=[Part.from_text("Rozumiem. Jestem gotów do rozmowy i wykrywania intencji umówienia wizyty.")])
    ]
    full_prompt = initial_prompt + history_for_general_ai
    full_prompt.append(Content(role="user", parts=[Part.from_text(current_user_message_text)]))

    # Ogranicz długość promptu
    max_prompt_messages = (MAX_HISTORY_TURNS * 2) + 2 # +2 dla instrukcji
    while len(full_prompt) > max_prompt_messages:
        full_prompt.pop(2) # Usuń najstarszą wiadomość użytkownika
        if len(full_prompt) > 2:
            full_prompt.pop(2) # Usuń odpowiadającą wiadomość modelu


    # Wywołaj Gemini
    response_text = _call_gemini(user_psid, full_prompt, GENERATION_CONFIG_DEFAULT, "General Conversation")

    if response_text:
        # Sprawdź, czy AI przypadkiem nie dodało znacznika ISO (nie powinno w tym trybie)
        if SLOT_ISO_MARKER_PREFIX in response_text:
             logging.warning(f"[{user_psid}] AI (General) błędnie dodało znacznik ISO. Usuwanie znacznika.")
             response_text = re.sub(rf"{re.escape(SLOT_ISO_MARKER_PREFIX)}.*?{re.escape(SLOT_ISO_MARKER_SUFFIX)}", "", response_text).strip()

        return response_text
    else:
        # Błąd API Gemini w ogólnej rozmowie
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
    hub_mode = request.args.get('hub.mode')
    hub_token = request.args.get('hub.verify_token')
    hub_challenge = request.args.get('hub.challenge')

    # Logowanie otrzymanych parametrów (ostrożnie z tokenem w produkcji)
    logging.debug(f"Mode: {hub_mode}")
    # logging.debug(f"Token: {hub_token}") # Lepiej nie logować tokena
    logging.debug(f"Challenge: {hub_challenge}")

    # Sprawdzenie poprawności tokena
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
    raw_data = request.data # Pobierz surowe dane
    data = None
    try:
        # Dekoduj i sparsuj JSON
        decoded_data = raw_data.decode('utf-8')
        # logging.debug(f"Surowe dane POST:\n{decoded_data[:1000]}...") # Loguj początek dla debugowania
        data = json.loads(decoded_data)

        # Sprawdź, czy to zdarzenie typu "page"
        if data and data.get("object") == "page":
            # Iteruj przez wpisy (może być ich wiele w jednym batchu)
            for entry in data.get("entry", []):
                page_id = entry.get("id")
                timestamp = entry.get("time")
                logging.debug(f"Przetwarzanie wpisu dla strony: {page_id}, czas: {timestamp}")

                # Iteruj przez zdarzenia 'messaging' w danym wpisie
                for event in entry.get("messaging", []):
                    sender_id = event.get("sender", {}).get("id")
                    recipient_id = event.get("recipient", {}).get("id") # ID strony

                    if not sender_id:
                        logging.warning("Pominięto zdarzenie bez ID nadawcy (sender.id).")
                        continue # Pomiń zdarzenia bez ID nadawcy

                    logging.info(f"--- Przetwarzanie zdarzenia dla PSID: {sender_id} ---")
                    # logging.debug(f"Pełne zdarzenie: {json.dumps(event, indent=2)}")

                    # --- Wczytanie historii i kontekstu ---
                    history, context = load_history(sender_id)
                    # Historia tylko user/model dla AI
                    history_for_gemini = [h for h in history if isinstance(h, Content) and h.role in ('user', 'model')]
                    logging.debug(f"Wczytano {len(history_for_gemini)} wiadomości user/model z historii.")

                    # --- Sprawdzenie aktywnego kontekstu propozycji ---
                    is_context_active = False
                    last_iso_from_context = None
                    last_proposal_text_for_feedback = "poprzednio zaproponowany termin" # Domyślny tekst
                    if context.get('type') == 'last_proposal' and context.get('slot_iso'):
                        # Weryfikacja, czy wczytany kontekst jest rzeczywiście ostatnim wpisem w pliku
                        # (Prosta weryfikacja, można ulepszyć o timestampy lub ID)
                        temp_hist_check, temp_ctx_check = load_history(sender_id) # Wczytaj ponownie, aby mieć pewność
                        if temp_ctx_check.get('slot_iso') == context.get('slot_iso'):
                            is_context_active = True
                            last_iso_from_context = context['slot_iso']
                            try:
                                last_dt = datetime.datetime.fromisoformat(last_iso_from_context).astimezone(_get_timezone())
                                last_proposal_text_for_feedback = format_slot_for_user(last_dt)
                            except Exception as fmt_err:
                                logging.warning(f"Nie udało się sformatować ostatniego ISO '{last_iso_from_context}' na potrzeby promptu feedback: {fmt_err}")
                            logging.info(f"    Aktywny kontekst propozycji: {last_iso_from_context} ({last_proposal_text_for_feedback})")
                        else:
                            logging.info(f"    Wczytany kontekst '{context.get('slot_iso')}' nie jest już aktywny (nadpisany). Reset.")
                            context = {} # Zresetuj wczytany kontekst, bo jest nieaktualny

                    # --- Inicjalizacja zmiennych dla cyklu przetwarzania zdarzenia ---
                    action = None         # Planowana akcja do wykonania
                    msg_result = None     # Tekst odpowiedzi do wysłania użytkownikowi
                    ctx_save = context    # Kontekst do zapisania (domyślnie zachowaj obecny)
                    model_resp_content = None # Obiekt Content odpowiedzi modelu do zapisu w historii
                    user_content = None   # Obiekt Content wiadomości użytkownika

                    # -----------------------------------------
                    # --- GŁÓWNA LOGIKA PRZETWARZANIA ZDARZEŃ ---
                    # -----------------------------------------

                    # === Obsługa wiadomości tekstowych ===
                    if message_data := event.get("message"):
                        if message_data.get("is_echo"):
                            logging.debug(f"    Pominięto echo wiadomości wysłanej przez stronę.")
                            continue # Ignoruj wiadomości wysłane przez bota

                        user_input_text = message_data.get("text", "").strip()

                        if user_input_text:
                            user_content = Content(role="user", parts=[Part.from_text(user_input_text)])
                            logging.info(f"    Otrzymano wiadomość tekstową: '{user_input_text[:100]}{'...' if len(user_input_text)>100 else ''}'")

                            # Symulacja "czytania"
                            if ENABLE_TYPING_DELAY:
                                time.sleep(MIN_TYPING_DELAY_SECONDS * 0.5)

                            # --- Przypadek 1: Użytkownik odpowiada na aktywną propozycję terminu ---
                            if is_context_active:
                                logging.info("      -> Kontekst aktywny. Pytanie AI (Feedback) o interpretację odpowiedzi...")
                                try:
                                    decision = get_gemini_feedback_decision(
                                        sender_id,
                                        user_input_text,
                                        history_for_gemini, # Przekaż historię user/model
                                        last_proposal_text_for_feedback # Przekaż sformatowany ostatni termin
                                    )

                                    if decision == "[ACCEPT]":
                                        action = 'book'
                                        logging.info(f"      Decyzja AI: {decision} -> Akcja: Rezerwacja")
                                        # Wiadomość zostanie wygenerowana przez funkcję book_appointment
                                        ctx_save = None # Reset kontekstu po akceptacji

                                    elif decision.startswith("[REJECT_FIND_NEXT"):
                                        action = 'find_and_propose'
                                        logging.info(f"      Decyzja AI: {decision} -> Akcja: Odrzucenie i szukanie nowego")
                                        # Wiadomość potwierdzająca odrzucenie przed szukaniem
                                        msg_result = "Rozumiem. W takim razie poszukam innego terminu..."
                                        ctx_save = None # Reset starego kontekstu propozycji

                                    elif decision == "[CLARIFY]":
                                        action = 'send_clarification'
                                        logging.info(f"      Decyzja AI: {decision} -> Akcja: Prośba o doprecyzowanie")
                                        msg_result = "Nie jestem pewien, co masz na myśli w kontekście zaproponowanego terminu. Czy możesz doprecyzować, czy go akceptujesz, czy wolisz inny?"
                                        # Zachowaj kontekst, aby użytkownik mógł się do niego odnieść
                                        ctx_save = context # Użyj oryginalnego kontekstu wczytanego na początku

                                    else: # Nieoczekiwany znacznik - potraktuj jako błąd/niejasność
                                        action = 'send_error'
                                        logging.warning(f"      Niespodziewana decyzja AI (Feedback): {decision}. Traktuję jako błąd.")
                                        msg_result = "Przepraszam, mam problem ze zrozumieniem Twojej odpowiedzi dotyczącej terminu. Czy możesz spróbować inaczej?"
                                        ctx_save = None # Reset kontekstu w razie błędu

                                except Exception as feedback_err:
                                    logging.error(f"!!! BŁĄD podczas przetwarzania feedbacku przez AI: {feedback_err}", exc_info=True)
                                    action = 'send_error'
                                    msg_result = "Wystąpił błąd podczas interpretacji Twojej odpowiedzi. Przepraszam za kłopot."
                                    ctx_save = None # Reset kontekstu

                            # --- Przypadek 2: Normalna rozmowa (brak aktywnego kontekstu) ---
                            else:
                                logging.info("      -> Kontekst nieaktywny. Pytanie AI (General) o odpowiedź...")
                                response = get_gemini_general_response(sender_id, user_input_text, history_for_gemini)

                                if response:
                                    # Sprawdź, czy AI zasygnalizowało intencję umówienia terminu
                                    if INTENT_SCHEDULE_MARKER in response:
                                        logging.info(f"      AI wykryło intencję umówienia [{INTENT_SCHEDULE_MARKER}].")
                                        action = 'find_and_propose'
                                        # Usuń znacznik z odpowiedzi i przygotuj wiadomość wstępną
                                        initial_resp_text = response.split(INTENT_SCHEDULE_MARKER, 1)[0].strip()
                                        if not initial_resp_text:
                                             initial_resp_text = "Dobrze, w takim razie sprawdzę dostępne terminy."
                                        msg_result = initial_resp_text # Wyślij najpierw odpowiedź, potem szukaj
                                        ctx_save = None # Upewnij się, że resetujemy kontekst przed szukaniem

                                    else: # Zwykła odpowiedź AI
                                        action = 'send_gemini_response'
                                        msg_result = response
                                        ctx_save = None # Ogólna rozmowa resetuje kontekst propozycji
                                else:
                                    # Błąd API Gemini w ogólnej rozmowie
                                    action = 'send_error'
                                    msg_result = "Przepraszam, wystąpił błąd podczas przetwarzania Twojej wiadomości. Spróbuj ponownie."
                                    ctx_save = None # Reset kontekstu

                        # --- Przypadek 3: Wiadomość bez tekstu (np. tylko załącznik, kciuk w górę itp.) ---
                        elif attachments := message_data.get("attachments"):
                            att_type = attachments[0].get('type','nieznany')
                            payload_url = attachments[0].get('payload', {}).get('url', '') # Dla obrazków itp.
                            logging.info(f"      Otrzymano załącznik typu: {att_type}.")
                            # Zapisz informację o załączniku w historii jako wiadomość użytkownika
                            user_content = Content(role="user", parts=[Part.from_text(f"[Otrzymano załącznik: {att_type}]")])
                            # Odpowiedz użytkownikowi (można dostosować)
                            if att_type == 'image':
                                msg_result = "Dziękuję za obrazek! Niestety, nie potrafię go jeszcze analizować."
                            elif att_type == 'audio':
                                msg_result = "Otrzymałem nagranie głosowe, ale niestety nie mogę go jeszcze odsłuchać."
                            elif att_type == 'sticker':
                                msg_result = "Fajna naklejka! 😉" # Prosta reakcja
                            else:
                                msg_result = "Dziękuję za przesłanie pliku. Obecnie nie obsługuję tego typu załączników."
                            action = 'send_info'
                            ctx_save = context # Zachowaj kontekst, jeśli był aktywny

                        else: # Pusta wiadomość (brak tekstu i załączników)
                            logging.info("      Otrzymano pustą wiadomość.")
                            if is_context_active:
                                # Jeśli czekamy na odpowiedź, zapytaj ponownie
                                action = 'send_clarification'
                                msg_result = "Przepraszam, nie otrzymałem odpowiedzi. Czy zaproponowany termin pasuje?"
                                ctx_save = context # Zachowaj kontekst
                            else:
                                # Jeśli nie ma kontekstu, można zignorować lub wysłać "?"
                                action = None # Ignoruj pustą wiadomość
                                ctx_save = None


                    # === Obsługa Postback (np. kliknięcie przycisku) ===
                    elif postback := event.get("postback"):
                        payload = postback.get("payload")
                        title = postback.get("title", "") # Tytuł klikniętego przycisku
                        logging.info(f"    Otrzymano postback. Payload: '{payload}', Tytuł: '{title}'")
                        # Zapisz w historii jako akcję użytkownika
                        user_content = Content(role="user", parts=[Part.from_text(f"[Kliknięto przycisk: {title} ({payload})]")])

                        # --- Przetwarzanie payloadu ---
                        if payload == "ACCEPT_SLOT":
                            if is_context_active and last_iso_from_context:
                                logging.info("      Postback: Akceptacja terminu -> Akcja: Rezerwacja")
                                action = 'book'
                                msg_result = None # Wiadomość generowana przez book_appointment
                                ctx_save = None # Reset kontekstu
                            else:
                                logging.warning("      Postback 'ACCEPT_SLOT', ale brak aktywnego kontekstu ISO.")
                                action = 'send_info'
                                msg_result = "Wygląda na to, że propozycja terminu wygasła lub wystąpił błąd. Nie mogę teraz zaakceptować."
                                ctx_save = None

                        elif payload == "REJECT_SLOT":
                            if is_context_active and last_iso_from_context:
                                logging.info("      Postback: Odrzucenie terminu -> Akcja: Szukanie nowego")
                                action = 'find_and_propose'
                                msg_result = "Rozumiem, ten termin nie pasuje. Sprawdzam inne dostępne opcje..."
                                ctx_save = None # Reset starego kontekstu
                            else:
                                logging.warning("      Postback 'REJECT_SLOT', ale brak aktywnego kontekstu ISO.")
                                action = 'send_info'
                                msg_result = "Nie widzę aktywnej propozycji terminu do odrzucenia."
                                ctx_save = None

                        # --- Można dodać obsługę innych payloadów/przycisków ---
                        # elif payload == "SHOW_PRICE_LIST":
                        #    action = 'send_info'
                        #    msg_result = "Cennik wysyłamy po umówieniu pierwszej lekcji."
                        #    ctx_save = context # Zachowaj kontekst, jeśli był

                        else: # Nieznany payload postback
                            logging.warning(f"      Nieznany payload postback: '{payload}'. Traktuję jak ogólne zapytanie.")
                            # Przekaż do AI jako tekst
                            simulated_input = f"Użytkownik kliknął przycisk '{title}' (payload: {payload})."
                            response = get_gemini_general_response(sender_id, simulated_input, history_for_gemini)
                            if response:
                                if INTENT_SCHEDULE_MARKER in response:
                                    action = 'find_and_propose'
                                    initial_resp_text = response.split(INTENT_SCHEDULE_MARKER, 1)[0].strip()
                                    if not initial_resp_text: initial_resp_text = "Dobrze, sprawdzę terminy."
                                    msg_result = initial_resp_text
                                    ctx_save = None
                                else:
                                    action = 'send_gemini_response'
                                    msg_result = response
                                    ctx_save = None # Reset kontekstu
                            else:
                                action = 'send_error'
                                msg_result = "Przepraszam, wystąpił błąd podczas przetwarzania Twojego żądania."
                                ctx_save = None

                    # === Obsługa innych zdarzeń (Read, Delivery) ===
                    elif event.get("read"):
                        logging.debug(f"    Potwierdzenie odczytania wiadomości przez użytkownika.")
                        # Zazwyczaj nie wymaga akcji
                        continue # Przejdź do następnego zdarzenia bez wysyłania/zapisu

                    elif event.get("delivery"):
                        logging.debug(f"    Potwierdzenie dostarczenia wiadomości.")
                        # Zazwyczaj nie wymaga akcji
                        continue # Przejdź do następnego zdarzenia

                    # === Nieobsługiwany typ zdarzenia ===
                    else:
                        logging.warning(f"    Nieobsługiwany typ zdarzenia w 'messaging': {json.dumps(event)}")
                        continue # Przejdź do następnego zdarzenia


                    # -----------------------------------------
                    # --- WYKONANIE ZAPLANOWANEJ AKCJI --------
                    # -----------------------------------------
                    history_saved_in_this_cycle = False # Flaga kontrolna

                    if action == 'book':
                        if last_iso_from_context:
                            try:
                                tz = _get_timezone()
                                start = datetime.datetime.fromisoformat(last_iso_from_context).astimezone(tz)
                                end = start + datetime.timedelta(minutes=APPOINTMENT_DURATION_MINUTES)
                                prof = get_user_profile(sender_id)
                                name = prof.get('first_name', '') if prof else f"User_{sender_id[-4:]}"
                                desc = f"Rezerwacja przez Bota FB\nPSID: {sender_id}"
                                if prof and prof.get('last_name'):
                                    desc += f"\nNazwisko: {prof.get('last_name')}"

                                ok, booking_msg = book_appointment(TARGET_CALENDAR_ID, start, end, f"Lekcja FB: {name}", desc, name)
                                msg_result = booking_msg # Wiadomość zwrotna z funkcji rezerwacji
                                if not ok:
                                     ctx_save = None # Reset kontekstu, jeśli rezerwacja się nie udała

                            except Exception as e:
                                logging.error(f"!!! BŁĄD podczas próby rezerwacji ISO {last_iso_from_context}: {e}", exc_info=True)
                                msg_result = "Wystąpił krytyczny błąd podczas próby rezerwacji terminu. Skontaktuj się z administratorem."
                                ctx_save = None # Reset kontekstu
                        else:
                            logging.error("!!! KRYTYCZNY BŁĄD LOGIKI: Próba wykonania akcji 'book' bez aktywnego 'last_iso_from_context' !!!")
                            msg_result = "Wystąpił wewnętrzny błąd systemu. Nie można teraz zarezerwować terminu."
                            ctx_save = None


                    elif action == 'find_and_propose':
                        # Zawsze szukaj od teraz
                        try:
                            tz = _get_timezone()
                            now = datetime.datetime.now(tz)
                            search_start = now
                            search_end_date = (search_start + datetime.timedelta(days=MAX_SEARCH_DAYS)).date()
                            search_end = tz.localize(datetime.datetime.combine(search_end_date, datetime.time(WORK_END_HOUR, 0)))

                            logging.info(f"      -> Rozpoczęcie szukania wolnych zakresów od {search_start:%Y-%m-%d %H:%M} do {search_end:%Y-%m-%d %H:%M}")

                            # Dłuższa symulacja pisania podczas szukania w kalendarzu
                            _simulate_typing(sender_id, MAX_TYPING_DELAY_SECONDS * 0.8)

                            free_ranges = get_free_time_ranges(TARGET_CALENDAR_ID, search_start, search_end)

                            if free_ranges:
                                logging.info(f"      Znaleziono {len(free_ranges)} wolnych zakresów. Przekazanie do AI (Proposal)...")
                                # Przygotuj historię dla AI proponującego - zawiera już feedback usera (jeśli był)
                                history_for_proposal_ai = history_for_gemini + ([user_content] if user_content else [])
                                proposal_text, proposed_iso = get_gemini_slot_proposal(sender_id, history_for_proposal_ai, free_ranges)

                                if proposal_text and proposed_iso:
                                    # Połącz wiadomość potwierdzającą odrzucenie (jeśli była) z nową propozycją
                                    final_proposal_msg = (msg_result + "\n\n" + proposal_text) if msg_result else proposal_text
                                    msg_result = final_proposal_msg # Ustaw finalną wiadomość
                                    ctx_save = {'role': 'system', 'type': 'last_proposal', 'slot_iso': proposed_iso} # Zapisz nowy kontekst
                                else:
                                    # AI nie wygenerowało poprawnego slotu lub wystąpił błąd weryfikacji
                                    fail_msg = proposal_text if proposal_text else "Niestety, mam problem ze znalezieniem i zaproponowaniem dogodnego terminu w tym momencie."
                                    msg_result = (msg_result + "\n\n" + fail_msg) if msg_result else fail_msg
                                    ctx_save = None # Reset kontekstu po błędzie propozycji

                            else: # Brak wolnych zakresów w kalendarzu
                                logging.warning(f"      Nie znaleziono żadnych wolnych zakresów w kalendarzu w podanym okresie.")
                                no_slots_msg = "Niestety, wygląda na to, że w najbliższym czasie ({MAX_SEARCH_DAYS} dni) nie mam już wolnych terminów w godzinach pracy. Spróbuj ponownie później lub skontaktuj się bezpośrednio."
                                msg_result = (msg_result + "\n\n" + no_slots_msg) if msg_result else no_slots_msg
                                ctx_save = None # Reset kontekstu

                        except Exception as find_err:
                            logging.error(f"!!! BŁĄD ogólny podczas szukania/proponowania terminu: {find_err}", exc_info=True)
                            error_msg = "Wystąpił nieoczekiwany problem podczas wyszukiwania dostępnych terminów. Spróbuj ponownie za chwilę."
                            msg_result = (msg_result + "\n\n" + error_msg) if msg_result else error_msg
                            ctx_save = None # Reset kontekstu


                    elif action in ['send_gemini_response', 'send_clarification', 'send_error', 'send_info']:
                        # Wiadomość `msg_result` została już ustawiona wcześniej w logice
                        # Kontekst (`ctx_save`) również powinien być już ustawiony (None lub zachowany)
                        logging.debug(f"      Akcja: {action}. Wiadomość gotowa do wysłania.")
                        pass


                    # -----------------------------------------
                    # --- WYSYŁANIE ODPOWIEDZI I ZAPIS -------
                    # -----------------------------------------

                    # --- Wysyłanie wiadomości do użytkownika ---
                    if msg_result:
                        send_message(sender_id, msg_result)
                        # Przygotuj obiekt Content odpowiedzi modelu do zapisu w historii
                        model_resp_content = Content(role="model", parts=[Part.from_text(msg_result)])
                    elif action: # Jeśli była akcja, ale nie wygenerowała wiadomości (rzadkie)
                        logging.warning(f"    Akcja '{action}' została wykonana, ale nie wygenerowano żadnej wiadomości do wysłania.")


                    # --- Zapis historii i kontekstu ---
                    # Zapisuj tylko, jeśli coś się wydarzyło (otrzymano wiadomość/postback LUB wysłano odpowiedź LUB zmieniono kontekst)
                    original_context_iso = context.get('slot_iso') # Zapamiętaj ISO z pierwotnie wczytanego kontekstu
                    new_context_iso = ctx_save.get('slot_iso') if isinstance(ctx_save, dict) else None # ISO z nowego kontekstu

                    # Sprawdź, czy nastąpiła zmiana w konwersacji lub kontekście
                    should_save = bool(user_content) or bool(model_resp_content) or (original_context_iso != new_context_iso)

                    if should_save:
                        history_to_save = list(history) # Stwórz kopię oryginalnej historii
                        if user_content:
                            history_to_save.append(user_content)
                        if model_resp_content:
                            history_to_save.append(model_resp_content)

                        logging.debug(f"Przygotowanie do zapisu historii. Nowy kontekst: {ctx_save}")
                        save_history(sender_id, history_to_save, context_to_save=ctx_save)
                        history_saved_in_this_cycle = True
                    else:
                        logging.debug("    Brak zmian w konwersacji lub kontekście - pomijanie zapisu historii.")


            # Zakończono przetwarzanie wszystkich zdarzeń w batchu
            logging.info(f"--- Zakończono przetwarzanie POST batch ---")
            return Response("EVENT_RECEIVED", status=200) # Zawsze zwracaj 200 OK dla Facebooka

        else:
            # Otrzymano dane POST, ale nie są to zdarzenia strony ('page')
            logging.warning(f"Otrzymano POST, ale obiekt nie jest typu 'page' (typ: {data.get('object') if data else 'Brak danych'}).")
            return Response("OK", status=200) # Mimo to odpowiedz OK

    except json.JSONDecodeError as e:
        # Błąd podczas parsowania JSON z żądania POST
        logging.error(f"!!! KRYTYCZNY BŁĄD: Nie udało się sparsować JSON z żądania POST: {e}", exc_info=True)
        logging.error(f"    Początek surowych danych: {raw_data[:500]}...")
        # Zwróć błąd 400 Bad Request
        return Response("Invalid JSON payload", status=400)

    except Exception as e:
        # Ogólny, nieoczekiwany błąd serwera podczas przetwarzania POST
        logging.critical(f"!!! KRYTYCZNY BŁĄD serwera podczas przetwarzania żądania POST: {e}", exc_info=True)
        # Zwróć 200 OK, aby Facebook nie próbował ponawiać tego samego błędnego żądania
        # W środowisku produkcyjnym warto monitorować te błędy.
        return Response("Internal Server Error", status=200)


# =====================================================================
# === URUCHOMIENIE SERWERA ============================================
# =====================================================================

if __name__ == '__main__':
    ensure_dir(HISTORY_DIR) # Upewnij się, że katalog na historię istnieje

    # --- Konfiguracja logowania ---
    log_level = logging.DEBUG if os.environ.get("FLASK_DEBUG", "False").lower() in ("true", "1", "yes") else logging.INFO
    logging.basicConfig(level=log_level,
                        format='%(asctime)s - %(levelname)s - [%(funcName)s:%(lineno)d] - %(message)s',
                        datefmt='%Y-%m-%d %H:%M:%S')

    # Wyciszenie zbyt gadatliwych logerów bibliotek zewnętrznych
    logging.getLogger('googleapiclient.discovery_cache').setLevel(logging.ERROR)
    logging.getLogger('urllib3.connectionpool').setLevel(logging.WARNING)
    logging.getLogger('werkzeug').setLevel(logging.WARNING) # Mniej logów z serwera deweloperskiego Flask

    # --- Wypisanie konfiguracji startowej ---
    print("\n" + "="*60 + "\n--- START KONFIGURACJI BOTA ---")
    print(f"  * Tryb debugowania Flask: {'Włączony' if log_level == logging.DEBUG else 'Wyłączony'}")
    print("-" * 60)
    print("  Konfiguracja Facebook:")
    print(f"    FB_VERIFY_TOKEN: {'OK' if VERIFY_TOKEN != 'KOLAGEN' else 'Użyto domyślny (KOLAGEN!)'}")
    if not PAGE_ACCESS_TOKEN or len(PAGE_ACCESS_TOKEN) < 50:
        print("!!! KRYTYCZNE: FB_PAGE_ACCESS_TOKEN jest PUSTY lub ZBYT KRÓTKI !!!")
    elif PAGE_ACCESS_TOKEN == "EACNAHFzEhkUBO4ypcoyQfWIgNc0YLZA1aCr9n3BzpvSJLoBTJnv5rWZBmc7HlqF6uUW1uAp6aDZB8ZAb0RRT45lVIfGnciQX6wBKrZColGARfVLXP5Ic6Ptrj5AUvom4Rt12hyBxcjIJGes76fvdvBhiBZCJ0ZCVfkQMZBZCBatJshSZA8hFuRyKd58b50wkhVCMZCuwZDZD":
        print("!!! UWAGA: Używany jest DOMYŚLNY FB_PAGE_ACCESS_TOKEN - zmień go! !!!")
    else:
        print("    FB_PAGE_ACCESS_TOKEN: Ustawiony (OK)")
    print("-" * 60)
    print("  Konfiguracja Ogólna:")
    print(f"    Katalog historii: {HISTORY_DIR}")
    print(f"    Maks. tur historii AI: {MAX_HISTORY_TURNS}")
    print(f"    Limit znaków wiad. FB: {MESSAGE_CHAR_LIMIT}")
    print(f"    Opóźnienie między fragm.: {MESSAGE_DELAY_SECONDS}s")
    print(f"    Symulacja pisania: {'Włączona' if ENABLE_TYPING_DELAY else 'Wyłączona'}")
    if ENABLE_TYPING_DELAY:
        print(f"      Min/Max czas pisania: {MIN_TYPING_DELAY_SECONDS}s / {MAX_TYPING_DELAY_SECONDS}s")
        print(f"      Prędkość pisania: {TYPING_CHARS_PER_SECOND} znaków/s")
    print("-" * 60)
    print("  Konfiguracja Vertex AI:")
    print(f"    Projekt GCP: {PROJECT_ID}")
    print(f"    Lokalizacja GCP: {LOCATION}")
    print(f"    Model AI: {MODEL_ID}")
    if not gemini_model:
        print("!!! OSTRZEŻENIE: Model Gemini AI NIE został załadowany poprawnie! !!!")
    else:
        print(f"    Model Gemini AI ({MODEL_ID}): Załadowany (OK)")
    print("-" * 60)
    print("  Konfiguracja Kalendarza Google:")
    print(f"    ID Kalendarza: {TARGET_CALENDAR_ID}")
    print(f"    Strefa czasowa: {CALENDAR_TIMEZONE} (Obiekt TZ: {_get_timezone()})")
    print(f"    Czas trwania wizyty: {APPOINTMENT_DURATION_MINUTES} min")
    print(f"    Godziny pracy: {WORK_START_HOUR}:00 - {WORK_END_HOUR}:00")
    print(f"    Preferowane godz. AI: W tygodniu >= {PREFERRED_WEEKDAY_START_HOUR}:00, Weekend >= {PREFERRED_WEEKEND_START_HOUR}:00")
    print(f"    Maks. zakres szukania: {MAX_SEARCH_DAYS} dni")
    print(f"    Plik klucza API: {SERVICE_ACCOUNT_FILE} ({'Znaleziono' if os.path.exists(SERVICE_ACCOUNT_FILE) else 'BRAK PLIKU!!!'})")
    # Sprawdzenie połączenia z API Kalendarza
    cal_service = get_calendar_service()
    if not cal_service and os.path.exists(SERVICE_ACCOUNT_FILE):
         print("!!! OSTRZEŻENIE: Usługa Google Calendar NIE zainicjowana poprawnie mimo istnienia pliku klucza. Sprawdź uprawnienia i konfigurację API. !!!")
    elif not os.path.exists(SERVICE_ACCOUNT_FILE):
         print("!!! OSTRZEŻENIE: Brak pliku klucza Google Calendar - funkcje kalendarza nie będą działać. !!!")
    elif cal_service:
         print("    Usługa Google Calendar: Zainicjowana (OK)")
    print("--- KONIEC KONFIGURACJI BOTA ---")
    print("="*60 + "\n")

    # --- Uruchomienie serwera ---
    port = int(os.environ.get("PORT", 8080))
    debug_mode = os.environ.get("FLASK_DEBUG", "False").lower() in ("true", "1", "yes")

    print(f"Uruchamianie serwera Flask na porcie {port}...")
    if not debug_mode:
        try:
            from waitress import serve
            print(">>> Serwer produkcyjny Waitress START <<<")
            serve(app, host='0.0.0.0', port=port, threads=8) # Można dostosować liczbę wątków
        except ImportError:
            print("!!! Ostrzeżenie: Biblioteka 'waitress' nie znaleziona. Uruchamianie wbudowanego serwera Flask (niezalecane w produkcji!).")
            print(">>> Serwer deweloperski Flask START <<<")
            app.run(host='0.0.0.0', port=port, debug=False)
    else:
        print(">>> Serwer deweloperski Flask (DEBUG MODE) START <<<")
        # Użyj wbudowanego serwera Flask z debugowaniem i automatycznym przeładowaniem
        app.run(host='0.0.0.0', port=port, debug=True)
