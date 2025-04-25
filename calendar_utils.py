# calendar_utils.py
import datetime
import os.path
import pytz
import json # Potrzebne do parsowania błędów API
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
import locale

# --- Konfiguracja ---
SERVICE_ACCOUNT_FILE = 'kalendarzklucz.json' # Upewnij się, że ta nazwa pliku jest poprawna
# Zakresy uprawnień (odczyt i zapis wydarzeń)
SCOPES = ['https://www.googleapis.com/auth/calendar.readonly', 'https://www.googleapis.com/auth/calendar.events']
TIMEZONE = 'Europe/Warsaw'
# Domyślna długość spotkania w minutach
DEFAULT_APPOINTMENT_DURATION = 60
# Godziny, w których MOŻNA umawiać wizyty
WORK_START_HOUR = 7
WORK_END_HOUR = 22

# Ustawienie polskiej lokalizacji dla nazw dni tygodnia
try:
    locale.setlocale(locale.LC_TIME, 'pl_PL.UTF-8')
except locale.Error:
    try:
        locale.setlocale(locale.LC_TIME, 'Polish_Poland.1250') # Windows
    except locale.Error:
        print("Ostrzeżenie: Nie można ustawić polskiej lokalizacji dla nazw dni.")

# Globalne zmienne cache'ujące
_calendar_service = None
_tz = None

def _get_timezone():
    """Zwraca (i cachuje) obiekt strefy czasowej."""
    global _tz
    if _tz is None:
        try:
            _tz = pytz.timezone(TIMEZONE)
        except pytz.exceptions.UnknownTimeZoneError:
            print(f"BŁĄD: Nieznana strefa czasowa '{TIMEZONE}'. Używam UTC.")
            _tz = pytz.utc
    return _tz

def get_calendar_service():
    """Tworzy lub zwraca (cachowany) obiekt usługi API używając konta usługi."""
    global _calendar_service
    if _calendar_service:
        return _calendar_service

    creds = None
    if not os.path.exists(SERVICE_ACCOUNT_FILE):
        print(f"BŁĄD: Nie znaleziono pliku klucza konta usługi: '{SERVICE_ACCOUNT_FILE}'")
        return None
    try:
        creds = service_account.Credentials.from_service_account_file(
            SERVICE_ACCOUNT_FILE, scopes=SCOPES)
        service = build('calendar', 'v3', credentials=creds)
        print("Pomyślnie utworzono usługę Calendar API przy użyciu konta usługi.")
        _calendar_service = service # Zapisz w cache
        return service
    except HttpError as error:
         print(f"Błąd API podczas tworzenia usługi Calendar: {error}")
         if error.resp.status == 403:
              print("   >>> Wygląda na problem z uprawnieniami konta usługi LUB włączonym API Kalendarza w GCP.")
         return None
    except Exception as e:
        print(f"Nieoczekiwany błąd podczas tworzenia usługi Calendar API: {e}")
        return None

def parse_event_time(event_time_data, default_tz):
    """Parsuje czas start/end z danych wydarzenia, zwracając obiekt datetime lub date."""
    if 'dateTime' in event_time_data:
        dt_str = event_time_data['dateTime']
        try: dt = datetime.datetime.fromisoformat(dt_str)
        except ValueError:
            try: dt = datetime.datetime.strptime(dt_str, '%Y-%m-%dT%H:%M:%S%z')
            except ValueError:
                try: dt = datetime.datetime.strptime(dt_str, '%Y-%m-%dT%H:%M%z')
                except ValueError: print(f"Ostrzeżenie: Nie można sparsować daty/czasu: {dt_str}"); return None
        if dt.tzinfo is None: dt = default_tz.localize(dt)
        else: dt = dt.astimezone(default_tz)
        return dt
    elif 'date' in event_time_data:
        try: return datetime.date.fromisoformat(event_time_data['date'])
        except ValueError: print(f"Ostrzeżenie: Nie można sparsować daty: {event_time_data['date']}"); return None
    return None

def get_free_slots(calendar_id, start_datetime, end_datetime, duration_minutes=DEFAULT_APPOINTMENT_DURATION):
    """
    Znajduje wolne przedziały czasowe o określonej długości w podanym zakresie.
    Zwraca listę dostępnych początków slotów (datetime objects, świadome strefy czasowej).
    """
    service = get_calendar_service()
    tz = _get_timezone()
    if not service: print("Błąd: Usługa kalendarza niedostępna w get_free_slots."); return []

    if start_datetime.tzinfo is None: start_datetime = tz.localize(start_datetime)
    else: start_datetime = start_datetime.astimezone(tz)
    if end_datetime.tzinfo is None: end_datetime = tz.localize(end_datetime)
    else: end_datetime = end_datetime.astimezone(tz)

    print(f"Szukanie wolnych slotów ({duration_minutes} min) w kalendarzu '{calendar_id}'")
    print(f"Zakres: od {start_datetime.strftime('%Y-%m-%d %H:%M %Z')} do {end_datetime.strftime('%Y-%m-%d %H:%M %Z')}")

    try:
        events_result = service.events().list(
            calendarId=calendar_id, timeMin=start_datetime.isoformat(), timeMax=end_datetime.isoformat(),
            singleEvents=True, orderBy='startTime').execute()
        events = events_result.get('items', [])
    except HttpError as error:
        print(f'Błąd API pobierania wydarzeń: {error}')
        if error.resp.status == 403: print(f"   >>> Brak uprawnień odczytu kalendarza '{calendar_id}'.")
        return []
    except Exception as e: print(f"Nieoczekiwany błąd pobierania wydarzeń: {e}"); return []

    free_slots_starts = []
    current_day = start_datetime.date(); end_day = end_datetime.date()
    appointment_duration = datetime.timedelta(minutes=duration_minutes)

    while current_day <= end_day:
        day_start_limit = tz.localize(datetime.datetime.combine(current_day, datetime.time(WORK_START_HOUR, 0)))
        day_end_limit = tz.localize(datetime.datetime.combine(current_day, datetime.time(WORK_END_HOUR, 0)))
        check_start_time = max(start_datetime, day_start_limit); check_end_time = min(end_datetime, day_end_limit)

        if check_start_time >= check_end_time: current_day += datetime.timedelta(days=1); continue

        potential_slot_start = check_start_time; busy_times = []
        for event in events:
            start = parse_event_time(event['start'], tz); end = parse_event_time(event['end'], tz)

            # <<< --- PONOWNA POPRAWKA LOGIKI DLA WYDARZEŃ CAŁODNIOWYCH --- >>>
            if isinstance(start, datetime.date): # Wydarzenie całodniowe
                # Wydarzenia całodniowe zwracane przez API mają datę końca dnia *następnego*.
                # Jeśli API zwróciło 'end' jako datę, użyj jej.
                # Jeśli nie (np. end jest None lub datetime), standardowo trwa do końca dnia 'start'.
                # Data końca (exclusive) - dzień, w którym wydarzenie już NIE obowiązuje.
                event_exclusive_end_date = None
                if isinstance(end, datetime.date):
                    event_exclusive_end_date = end
                else:
                    # Domyślnie wydarzenie całodniowe kończy się o północy *po* dniu startowym.
                    event_exclusive_end_date = start + datetime.timedelta(days=1)

                # Sprawdź, czy current_day (sprawdzany dzień) zawiera się w zakresie [start, event_exclusive_end_date)
                # Porównujemy tylko obiekty 'date'
                if start <= current_day < event_exclusive_end_date:
                    # Jeśli tak, to cały dzień pracy jest zajęty
                    busy_times.append({'start': day_start_limit, 'end': day_end_limit})
                    # print(f"   - Uwzględniono wydarzenie całodniowe '{event.get('summary','?')}' w dniu {current_day}")
            # <<< --- KONIEC PONOWNEJ POPRAWKI --- >>>

            elif isinstance(start, datetime.datetime) and isinstance(end, datetime.datetime): # Normalne wydarzenie
                 if end > day_start_limit and start < day_end_limit: # Czy nakłada się na godziny pracy?
                     effective_start = max(start, day_start_limit)
                     effective_end = min(end, day_end_limit)
                     if effective_start < effective_end:
                        busy_times.append({'start': effective_start, 'end': effective_end})

        # Łączenie zajętych okresów
        if not busy_times: merged_busy_times = []
        else:
             busy_times.sort(key=lambda x: x['start']); merged_busy_times = [busy_times[0]]
             for current_busy in busy_times[1:]:
                 last_merged = merged_busy_times[-1]
                 if current_busy['start'] <= last_merged['end']: last_merged['end'] = max(last_merged['end'], current_busy['end'])
                 else: merged_busy_times.append(current_busy)

        # Szukanie luk
        for busy in merged_busy_times:
            busy_start = busy['start']; busy_end = busy['end']
            while potential_slot_start + appointment_duration <= busy_start:
                 if potential_slot_start >= check_start_time and potential_slot_start + appointment_duration <= check_end_time:
                     free_slots_starts.append(potential_slot_start)
                 potential_slot_start += appointment_duration
            potential_slot_start = max(potential_slot_start, busy_end)

        while potential_slot_start + appointment_duration <= check_end_time:
             if potential_slot_start >= check_start_time: free_slots_starts.append(potential_slot_start)
             potential_slot_start += appointment_duration
        current_day += datetime.timedelta(days=1)

    final_slots = sorted(list(set(slot for slot in free_slots_starts if start_datetime <= slot < end_datetime)))
    print(f"Znaleziono {len(final_slots)} unikalnych wolnych slotów.")
    return final_slots


def book_appointment(calendar_id, start_time, end_time, summary="Rezerwacja wizyty", description="", user_name=""):
    """Dodaje wydarzenie (rezerwację) do kalendarza Google."""
    service = get_calendar_service()
    tz = _get_timezone()
    if not service: return False, "Nie udało się połączyć z usługą kalendarza."

    if start_time.tzinfo is None: start_time = tz.localize(start_time)
    else: start_time = start_time.astimezone(tz)
    if end_time.tzinfo is None: end_time = tz.localize(end_time)
    else: end_time = end_time.astimezone(tz)

    event_summary = summary
    if user_name: event_summary += f" - {user_name}"

    event = {
        'summary': event_summary, 'description': description,
        'start': {'dateTime': start_time.isoformat(), 'timeZone': TIMEZONE,},
        'end': {'dateTime': end_time.isoformat(), 'timeZone': TIMEZONE,},
        'reminders': {'useDefault': False, 'overrides': [{'method': 'popup', 'minutes': 60},],},
    }

    try:
        print(f"Rezerwacja: {event_summary} od {start_time.strftime('%Y-%m-%d %H:%M')} do {end_time.strftime('%Y-%m-%d %H:%M')}")
        created_event = service.events().insert(calendarId=calendar_id, body=event).execute()
        print(f"Zarezerwowano. ID: {created_event.get('id')}")
        locale_day_name = ""
        try: locale_day_name = start_time.strftime("%A")
        except: day_names = ["Poniedziałek", "Wtorek", "Środa", "Czwartek", "Piątek", "Sobota", "Niedziela"]; locale_day_name = day_names[start_time.weekday()]
        confirm_message = f"Świetnie! Twój termin na {locale_day_name}, {start_time.strftime('%d.%m.%Y o %H:%M')} został pomyślnie zarezerwowany."
        return True, confirm_message
    except HttpError as error:
        error_details = f"Kod: {error.resp.status}, Powód: {error.resp.reason}"
        try: error_json = json.loads(error.content.decode('utf-8')); error_details += f" - {error_json.get('error', {}).get('message', '')}"
        except: pass
        print(f"Błąd API rezerwacji: {error}, Szczegóły: {error_details}")
        if error.resp.status == 409: return False, "Niestety, ten termin został właśnie zajęty. Wybierz inny."
        elif error.resp.status == 403: return False, f"Brak uprawnień do zapisu w kalendarzu '{calendar_id}'. Skontaktuj się z adminem."
        elif error.resp.status == 404: return False, f"Nie znaleziono kalendarza '{calendar_id}'."
        else: return False, f"Błąd API ({error.resp.status}) podczas rezerwacji."
    except Exception as e:
        import traceback; print(f"Nieoczekiwany błąd Python rezerwacji: {e}"); traceback.print_exc()
        return False, "Niespodziewany błąd systemu rezerwacji."
