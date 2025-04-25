import datetime
import os.path
import pytz # Dla lepszej obsługi stref czasowych

# Używamy bibliotek dla konta usługi
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# --- Konfiguracja ---
# ID kalendarza do sprawdzenia
CALENDAR_ID = 'f19e189826b9d6e36950da347ac84d5501ecbd6bed0d76c8641be61a67749c67@group.calendar.google.com'
# Zakresy uprawnień - nadal potrzebne, by określić, co chcemy robić
SCOPES = ['https://www.googleapis.com/auth/calendar.readonly']
# Nazwa pliku klucza JSON pobranego z Google Cloud Console dla konta usługi
SERVICE_ACCOUNT_FILE = 'kalendarzklucz.json' # <<< ZAKTUALIZOWANA NAZWA PLIKU

# Strefa czasowa (ważne dla poprawnego porównywania czasów)
# Znajdź odpowiednią dla siebie, np. 'Europe/Warsaw', 'UTC', itp.
TIMEZONE = 'Europe/Warsaw'
# Definicja godzin pracy (do sprawdzania wolnych slotów)
WORK_START_HOUR = 9
WORK_END_HOUR = 17 # Koniec o 17:00 (sloty do 16:00-17:00)
# --- Koniec Konfiguracji ---

# --- Funkcja get_calendar_service używająca konta usługi ---
def get_calendar_service():
    """Tworzy obiekt usługi API używając danych uwierzytelniających konta usługi."""
    creds = None
    try:
        if not os.path.exists(SERVICE_ACCOUNT_FILE):
             print(f"BŁĄD: Nie znaleziono pliku klucza konta usługi: '{SERVICE_ACCOUNT_FILE}'")
             print("Upewnij się, że plik klucza JSON znajduje się w tym samym katalogu co skrypt.")
             print("Pobierz plik klucza JSON z Google Cloud Console (IAM i administracja -> Konta usług -> Klucze).")
             return None

        # Ładuj dane uwierzytelniające z pliku klucza konta usługi
        creds = service_account.Credentials.from_service_account_file(
            SERVICE_ACCOUNT_FILE, scopes=SCOPES)

    except Exception as e:
        print(f"Błąd podczas ładowania danych uwierzytelniających konta usługi z pliku '{SERVICE_ACCOUNT_FILE}': {e}")
        return None

    try:
        # Buduj usługę API kalendarza
        service = build('calendar', 'v3', credentials=creds)
        print("Pomyślnie utworzono usługę Calendar API przy użyciu konta usługi.")
        return service
    except HttpError as error:
        print(f'Wystąpił błąd podczas tworzenia usługi API: {error}')
        return None
    except Exception as e:
        print(f'Wystąpił nieoczekiwany błąd podczas tworzenia usługi: {e}')
        return None
# --- Koniec funkcji get_calendar_service ---


def get_week_range(start_date, tz):
    """Oblicza początek bieżącego tygodnia (poniedziałek) i koniec następnego (niedziela)."""
    # Początek bieżącego tygodnia (poniedziałek 00:00:00)
    start_of_current_week = start_date - datetime.timedelta(days=start_date.weekday())
    start_dt = tz.localize(datetime.datetime.combine(start_of_current_week, datetime.time.min))

    # Koniec następnego tygodnia (niedziela 23:59:59)
    # Przesuwamy się o 7 dni, aby być w następnym tygodniu, a potem do końca tego tygodnia (niedziela)
    end_of_next_week = start_of_current_week + datetime.timedelta(days=6 + 7) # 6 dni do niedzieli + 7 dni
    # Używamy początku dnia *następnego* po końcu zakresu dla API
    end_dt = tz.localize(datetime.datetime.combine(end_of_next_week + datetime.timedelta(days=1), datetime.time.min))

    return start_dt, end_dt

def parse_event_time(event_time_data, default_tz):
    """Parsuje czas start/end z danych wydarzenia, uwzględniając datę i datę/czas."""
    if 'dateTime' in event_time_data:
        # Wydarzenie ma określony czas
        dt_str = event_time_data['dateTime']
        # Spróbuj sparsować z różnymi formatami offsetu UTC (+HH:MM, -HH:MM, Z)
        try:
            dt = datetime.datetime.fromisoformat(dt_str)
        except ValueError:
            # Czasami brakuje sekund w formacie ISO lub inne drobne różnice
            try:
                dt = datetime.datetime.strptime(dt_str, '%Y-%m-%dT%H:%M:%S%z')
            except ValueError:
                 try:
                     dt = datetime.datetime.strptime(dt_str, '%Y-%m-%dT%H:%M%z')
                 except ValueError:
                     print(f"Nie można sparsować daty/czasu: {dt_str}")
                     return None


        # Upewnij się, że datetime jest świadomy strefy czasowej
        if dt.tzinfo is None:
             # Jeśli API nie zwróciło tzinfo, załóż domyślną strefę kalendarza (jeśli znana) lub UTC
             # Tutaj dla uproszczenia używamy zdefiniowanej TIMEZONE
             dt = default_tz.localize(dt)
        else:
             # Konwertuj do naszej lokalnej strefy czasowej dla spójności porównań
             dt = dt.astimezone(default_tz)
        return dt
    elif 'date' in event_time_data:
        # Wydarzenie całodniowe - API zwraca tylko datę
        date_str = event_time_data['date']
        date_obj = datetime.date.fromisoformat(date_str)
        # Zwracamy obiekt daty, funkcja wywołująca zdecyduje jak go interpretować
        return date_obj
    return None # Nieznany format

def find_free_slots(service, calendar_id, start_dt, end_dt, tz):
    """Pobiera wydarzenia i znajduje wolne przedziały czasowe."""
    print(f"Pobieranie wydarzeń z kalendarza '{calendar_id}'")
    print(f"Zakres czasu: od {start_dt.strftime('%Y-%m-%d %H:%M %Z')} do {end_dt.strftime('%Y-%m-%d %H:%M %Z')}")
    try:
        events_result = service.events().list(
            calendarId=calendar_id,
            timeMin=start_dt.isoformat(),
            timeMax=end_dt.isoformat(),
            singleEvents=True,  # Rozwija wydarzenia cykliczne
            orderBy='startTime'
        ).execute()
        events = events_result.get('items', [])
    except HttpError as error:
        print(f'Wystąpił błąd API podczas pobierania wydarzeń: {error}')
        if error.resp.status == 404:
             print(f"BŁĄD: Nie znaleziono kalendarza o ID: {calendar_id}")
        elif error.resp.status == 403:
             print(f"BŁĄD: Brak uprawnień do odczytu kalendarza: {calendar_id}")
             print(f"Upewnij się, że konto usługi (którego klucz jest w '{SERVICE_ACCOUNT_FILE}')")
             print("zostało dodane do ustawień udostępniania tego kalendarza")
             print("z uprawnieniem 'Widzi wszystkie szczegóły wydarzenia'.")
        return
    except Exception as e:
        print(f'Wystąpił nieoczekiwany błąd podczas pobierania wydarzeń: {e}')
        return

    if not events:
        print("Nie znaleziono żadnych nadchodzących wydarzeń w podanym zakresie.")
        # W tym przypadku wszystkie sloty w godzinach pracy są wolne
        # (kod poniżej to obsłuży)

    print(f"Znaleziono {len(events)} wydarzeń. Przetwarzanie wolnych slotów...")

    # Przetwarzanie dzień po dniu
    current_day = start_dt.date()
    end_day = end_dt.date() # API zwraca do północy, więc używamy <

    while current_day < end_day:
        # Pomijaj weekendy (opcjonalnie)
        # if current_day.weekday() >= 5: # 5 = sobota, 6 = niedziela
        #     current_day += datetime.timedelta(days=1)
        #     continue

        # Użyj strftime z lokalizacją, jeśli system to obsługuje poprawnie
        try:
            day_name = current_day.strftime('%A')
        except UnicodeEncodeError: # Na niektórych systemach może być problem z polskimi znakami
            day_name = ["Poniedziałek", "Wtorek", "Środa", "Czwartek", "Piątek", "Sobota", "Niedziela"][current_day.weekday()]

        print(f"\n--- {current_day.strftime('%Y-%m-%d')}, {day_name} ---")

        # Definiowanie początku i końca dnia pracy w lokalnej strefie czasowej
        day_start_time = tz.localize(datetime.datetime.combine(current_day, datetime.time(WORK_START_HOUR, 0)))
        day_end_time = tz.localize(datetime.datetime.combine(current_day, datetime.time(WORK_END_HOUR, 0)))

        # Aktualny początek potencjalnego wolnego slotu
        potential_free_start = day_start_time

        # Filtruj wydarzenia tylko dla bieżącego dnia
        day_events = []
        for event in events:
            start = parse_event_time(event['start'], tz)
            end = parse_event_time(event['end'], tz)

            # Obsługa wydarzeń całodniowych - traktujemy je jako blokujące cały dzień pracy
            if isinstance(start, datetime.date):
                 # Jeśli data startowa wydarzenia całodniowego to bieżący dzień
                 if start == current_day:
                      # Zablokuj cały dzień roboczy
                      day_events.append({'start': day_start_time, 'end': day_end_time})
                      # Można by przerwać, ale bezpieczniej przetworzyć resztę
                 continue # Przejdź do następnego wydarzenia

            # Obsługa normalnych wydarzeń czasowych
            elif isinstance(start, datetime.datetime) and isinstance(end, datetime.datetime):
                event_start_dt = start
                event_end_dt = end

                # Sprawdź, czy wydarzenie (nawet częściowo) przypada na bieżący dzień roboczy
                if event_end_dt > day_start_time and event_start_dt < day_end_time:
                     # Przytnij czas wydarzenia do granic dnia pracy
                     effective_start = max(event_start_dt, day_start_time)
                     effective_end = min(event_end_dt, day_end_time)
                     if effective_start < effective_end:
                         day_events.append({'start': effective_start, 'end': effective_end})
            else:
                # Pomiń jeśli nie udało się sparsować czasu startowego lub końcowego
                event_summary = event.get('summary', 'Brak tytułu')
                print(f"Pomijam wydarzenie '{event_summary}' z nierozpoznanym czasem startu/końca.")
                continue


        # Sortuj wydarzenia dnia wg czasu rozpoczęcia (ważne dla algorytmu)
        day_events.sort(key=lambda x: x['start'])

        # Znajdowanie wolnych slotów
        free_slots_found = False
        for event in day_events:
            event_start = event['start']
            event_end = event['end']

            # Sprawdź, czy jest luka między potential_free_start a początkiem tego wydarzenia
            # Dodajemy mały margines (sekunda), aby uniknąć problemów z precyzją
            if potential_free_start < event_start - datetime.timedelta(seconds=1):
                # Mamy wolny slot
                print(f"  Wolne: {potential_free_start.strftime('%H:%M')} - {event_start.strftime('%H:%M')}")
                free_slots_found = True

            # Przesuń potential_free_start na koniec bieżącego wydarzenia (lub dalej, jeśli wydarzenia się nakładają)
            potential_free_start = max(potential_free_start, event_end)

        # Sprawdź, czy jest wolny slot po ostatnim wydarzeniu do końca dnia pracy
        if potential_free_start < day_end_time - datetime.timedelta(seconds=1):
            print(f"  Wolne: {potential_free_start.strftime('%H:%M')} - {day_end_time.strftime('%H:%M')}")
            free_slots_found = True

        if not free_slots_found and not day_events:
             # Jeśli nie było żadnych wydarzeń w tym dniu, cały dzień pracy jest wolny
             print(f"  Wolne: {day_start_time.strftime('%H:%M')} - {day_end_time.strftime('%H:%M')}")
        elif not free_slots_found and day_events:
             # Jeśli były wydarzenia, ale nie znaleziono slotów (np. dzień cały zajęty)
             print("  Brak wolnych slotów w godzinach pracy.")


        # Przejdź do następnego dnia
        current_day += datetime.timedelta(days=1)

if __name__ == '__main__':
    # Ustawienie strefy czasowej
    try:
        tz = pytz.timezone(TIMEZONE)
    except pytz.exceptions.UnknownTimeZoneError:
        print(f"BŁĄD: Nieznana strefa czasowa '{TIMEZONE}'. Używam UTC.")
        tz = pytz.utc

    # Pobierz dzisiejszą datę w odpowiedniej strefie czasowej
    today = datetime.datetime.now(tz).date()

    # Uzyskaj zakres dat: bieżący i następny tydzień
    start_range, end_range = get_week_range(today, tz)

    # Pobierz usługę kalendarza używając konta usługi
    service = get_calendar_service()

    if service:
        # Znajdź i wypisz wolne sloty
        find_free_slots(service, CALENDAR_ID, start_range, end_range, tz)
    else:
        print("Nie udało się uzyskać dostępu do usługi Google Calendar przy użyciu konta usługi. Zakończono.")
