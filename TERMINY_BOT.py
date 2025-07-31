import google.generativeai as genai
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from flask import Flask, request
from pyairtable import Api
import json
import os
import random
import datetime
import locale
import re
import pytz
import time
import requests
import threading

# --- Ustawienia Lokalizacji ---
try:
    locale.setlocale(locale.LC_TIME, 'pl_PL.UTF-8')
except locale.Error:
    print("Ostrzeżenie: Nie można ustawić polskiej lokalizacji.")

# --- KONFIGURACJA ---
API_KEY = "AIzaSyCJGoODg04hUZ3PpKf5tb7NoIMtT9G9K9I"
FB_PAGE_ACCESS_TOKEN = "EAAKusF6JViEBPNJiRftrqPmOy6CoZAWZBw3ZBEWl8dd7LtinSSF85JeKYXA3ZB7xlvFG6e5txU1i8RUEiskmZCXXyuIH4x4B4j4zBrOXm0AQyskcKBUaMVgS2o3AMZA2FWF0PNTuusd6nbxGPzGZAWyGoPP9rjDl1COwLk1YhTOsG7eaXa6FIxnXQaGFdB9oh7gdADaq7e4aQZDZD"
VERIFY_TOKEN = "KOLAGEN"
AIRTABLE_API_KEY = "patcSdupvwJebjFDo.7e15a93930d15261989844687bcb15ac5c08c84a29920c7646760bc6f416146d"
AIRTABLE_BASE_ID = "appTjrMTVhYBZDPw9"
AIRTABLE_BOOKINGS_TABLE_NAME = "Rezerwacje"
CALENDAR_SERVICE_ACCOUNT_FILE = 'KALENDARZ_KLUCZ.json'
CALENDAR_TIMEZONE = 'Europe/Warsaw'
APPOINTMENT_DURATION_MINUTES = 60
SEARCH_DAYS = 14
WORK_START_HOUR = 8
WORK_END_HOUR = 22
MIN_BOOKING_LEAD_HOURS = 24
BREAK_BUFFER_MINUTES = 10
HELD_SLOTS = {} 
HOLD_DURATION_HOURS = 24
HISTORY_DIR = "conversation_history"
GRAY_COLOR_ID = "8" 
SERVICE_INFO = {
    "Cennik za 60 minut": {
        "Szkoła Podstawowa: 60 zł",
        "Liceum/Technikum (Podstawa, kl. 1-3): 65 zł; (Podstawa, kl. 4/5): 70 zł",
        "Liceum/Technikum (Rozszerzenie, kl. 1-3): 70 zł; (Rozszerzenie, kl. 4/5): 75 zł"
    },
    "Format Lekcji": "Wszystkie zajęcia odbywają się online za pośrednictwem platformy Teams. Link do spotkania jest taki sam na wszystkich zajęciach",
    "Polityka Odwoływania": "Zajęcia można bezpłatnie odwołać najpóźniej na 24 godziny przed ich planowanym rozpoczęciem, zwrot za lekcje odwołane mniej niż 24h przed rozpoczęciem pokryje jedynie połowę ceny zajęć",
    "Przedmioty z których udzielamy korepetycji oraz kontakt gdzie nalezy umówić pierwszą lekcję, ten profil zajmuję się tylko zarządzaniem kolejnymi terminami, pierwszy termin należy umówić pod którąś z tych stron": "Polski: https://tiny.pl/0xnsgbt2; Matematyka: https://tiny.pl/f7xz5n0g; Angielski: https://tiny.pl/prrr7qf1",
}
CALENDARS_CONFIG = []
CALENDAR_NAME_TO_ID = {}
CALENDAR_SCOPES = ['https://www.googleapis.com/auth/calendar'] 

def load_config():
    global CALENDARS_CONFIG, CALENDAR_NAME_TO_ID
    try:
        with open('config.json', 'r', encoding='utf-8') as f:
            config = json.load(f)
            CALENDARS_CONFIG = config.get("CALENDARS", [])
            CALENDAR_NAME_TO_ID = {cal['name']: cal['id'] for cal in CALENDARS_CONFIG}
            print("--- Konfiguracja kalendarzy załadowana pomyślnie. ---")
            print(f"--- Znaleziono {len(CALENDARS_CONFIG)} kalendarzy. ---")
    except FileNotFoundError:
        print("!!! KRYTYCZNY BŁĄD: Plik 'config.json' nie został znaleziony! !!!")
    except json.JSONDecodeError:
        print("!!! KRYTYCZNY BŁĄD: Błąd parsowania pliku 'config.json'! Sprawdź jego składnię. !!!")
    except Exception as e:
        print(f"!!! KRYTYCZNY BŁĄD podczas ładowania konfiguracji: {e} !!!")

# --- Inicjalizacja ---
load_config()
try:
    genai.configure(api_key=API_KEY)
    airtable_api = Api(AIRTABLE_API_KEY)
    print("--- Połączono z Airtable API. ---")
except Exception as e:
    print(f"Błąd konfiguracji API: {e}")
    exit()

app = Flask(__name__)

# --- FUNKCJE POMOCNICZE ---


# --- ZMIENIONA FUNKCJA ---
def stworz_opis_wydarzenia(fields):
    """Tworzy sformatowany opis wydarzenia na podstawie pól z Airtable."""
    opis_czesci = []
    
    typ_szkoly = fields.get('Typ Szkoły', '')
    klasa = fields.get('Klasa', '')
    if typ_szkoly or klasa:
        opis_czesci.append(f"Poziom: {typ_szkoly} {klasa}".strip())

    # Sprawdzamy, czy w typie szkoły jest słowo kluczowe wskazujące na szkołę średnią
    if 'liceum' in typ_szkoly.lower() or 'technikum' in typ_szkoly.lower():
        poziom_materialu = fields.get('Poziom', '')
        if poziom_materialu:
            opis_czesci.append(f"Materiał: {poziom_materialu}")

    link_kontaktowy = fields.get('LINK', '')
    if link_kontaktowy:
        opis_czesci.append(f"Link do kontaktu: {link_kontaktowy}")
        
    # NOWA CZĘŚĆ: Dodawanie linku do zajęć (np. Google Meet / Teams)
    link_zajec = fields.get('TEAMS', '') # Pobieramy wartość z kolumny 'TEAMS'
    if link_zajec:
        opis_czesci.append(f"Link do zajęć: {link_zajec}")
        
    return "\n".join(opis_czesci)
    
def delete_unconfirmed_event(service, calendar_id, student_first_name, student_last_name):
    """Wyszukuje i usuwa wydarzenie z tytułem '(NIEPOTWIERDZONE) Imię Nazwisko Ucznia'."""
    if not service or not student_first_name or not student_last_name:
        return False

    # Konstruujemy nowy, dokładny tytuł, którego szukamy
    target_summary = f"(NIEPOTWIERDZONE) {student_first_name} {student_last_name}"
    print(f"--- WYSZUKIWANIE WYDARZENIA DO USUNIĘCIA o tytule: '{target_summary}' w kalendarzu {calendar_id} ---")

    try:
        events_result = service.events().list(
            calendarId=calendar_id,
            q=target_summary,
            singleEvents=True
        ).execute()
        
        events_to_delete = events_result.get('items', [])

        if not events_to_delete:
            print(f"--- Nie znaleziono wydarzenia '{target_summary}' do usunięcia. ---")
            return True 

        for event in events_to_delete:
            if event.get('summary') == target_summary:
                event_id = event['id']
                print(f"--- ZNALEZIONO i usuwam wydarzenie: ID={event_id}, Tytuł='{event.get('summary')}' ---")
                service.events().delete(calendarId=calendar_id, eventId=event_id).execute()
        
        return True

    except HttpError as e:
        print(f"BŁĄD API podczas usuwania wydarzenia '{target_summary}': {e}")
        return False
    except Exception as e:
        print(f"BŁĄD KRYTYCZNY podczas usuwania wydarzenia '{target_summary}': {e}")
        return False

def send_message(psid, message_text):
    print(f"Wysyłanie do {psid}: '{message_text[:100]}...'")
    params = {"access_token": FB_PAGE_ACCESS_TOKEN}
    headers = {"Content-Type": "application/json"}
    data = json.dumps({"recipient": {"id": psid}, "message": {"text": message_text}, "messaging_type": "RESPONSE"})
    try:
        r = requests.post("https://graph.facebook.com/v19.0/me/messages", params=params, headers=headers, data=data)
        if r.status_code != 200:
            print(f"BŁĄD: Nie udało się wysłać wiadomości. Status: {r.status_code}, Odpowiedź: {r.text}")
    except Exception as e:
        print(f"BŁĄD: Wyjątek podczas wysyłania wiadomości: {e}")

def get_user_profile(psid):
    try:
        url = f"https://graph.facebook.com/{psid}?fields=first_name,last_name&access_token={FB_PAGE_ACCESS_TOKEN}"
        response = requests.get(url)
        response.raise_for_status()
        data = response.json()
        print(f"--- POBRANO PROFIL FB dla {psid}: {data} ---")
        return data.get("first_name"), data.get("last_name")
    except requests.exceptions.RequestException as e:
        print(f"BŁĄD: Nie można pobrać profilu FB dla PSID {psid}: {e}")
        return None, None

def check_user_status_in_airtable(first_name, last_name):
    """Sprawdza status i zwraca krotkę (status, dane_rekordu)."""
    if not airtable_api or not first_name or not last_name:
        return "OK_PROCEED", None
    try:
        table = airtable_api.table(AIRTABLE_BASE_ID, AIRTABLE_BOOKINGS_TABLE_NAME)
        formula = f"AND({{Imię Rodzica}} = '{first_name}', {{Nazwisko Rodzica}} = '{last_name}')"
        record = table.first(formula=formula)
        if not record:
            print(f"--- AIRTABLE CHECK: Użytkownik {first_name} {last_name} nie znaleziony. Status: NOT_FOUND ---")
            return "NOT_FOUND", None
        status = record.get('fields', {}).get('Status')
        print(f"--- AIRTABLE CHECK: Użytkownik {first_name} {last_name} znaleziony. Status w bazie: '{status}' ---")
        if status == "Dane zebrane - oczekiwanie na potwierdzenie":
            return "AWAITING_CONFIRMATION", record
        else:
            return "OK_PROCEED", record
    except Exception as e:
        print(f"BŁĄD: Wystąpił błąd podczas sprawdzania statusu w Airtable dla {first_name} {last_name}: {e}")
        return "OK_PROCEED", None

def update_airtable_status(record_id, new_status):
    """Aktualizuje pole 'Status' dla danego rekordu w Airtable."""
    if not airtable_api: return False, "Brak połączenia z Airtable"
    try:
        table = airtable_api.table(AIRTABLE_BASE_ID, AIRTABLE_BOOKINGS_TABLE_NAME)
        table.update(record_id, {'Status': new_status})
        print(f"--- AIRTABLE UPDATE: Zaktualizowano status rekordu {record_id} na '{new_status}' ---")
        return True, "Status zaktualizowany"
    except Exception as e:
        print(f"BŁĄD: Nie udało się zaktualizować statusu dla rekordu {record_id}: {e}")
        return False, str(e)

def get_calendar_service(service_account_file, scopes):
    if not os.path.exists(service_account_file):
        print(f"!!! KRYTYCZNY BŁĄD: Brak pliku klucza '{service_account_file}' w bieżącym folderze. !!!")
        return None
    try:
        creds = service_account.Credentials.from_service_account_file(service_account_file, scopes=scopes)
        service = build('calendar', 'v3', credentials=creds)
        print("--- Usługa Google Calendar API połączona (z uprawnieniami do zapisu). ---")
        return service
    except Exception as e:
        print(f"!!! KRYTYCZNY BŁĄD: Nie można połączyć się z Google Calendar API: {e} !!!")
        return None

def find_available_slots_gcal(service, calendar_id, duration_minutes, search_days):
    if not service: return []
    tz = pytz.timezone(CALENDAR_TIMEZONE)
    now = datetime.datetime.now(tz)
    time_min_gcal = now.isoformat()
    time_max_gcal = (now + datetime.timedelta(days=search_days)).isoformat()
    try:
        events_result = service.events().list(
            calendarId=calendar_id, timeMin=time_min_gcal, timeMax=time_max_gcal,
            singleEvents=True
        ).execute()
        all_events = events_result.get('items', [])
    except HttpError as e:
        print(f"Błąd API podczas pobierania wszystkich wydarzeń: {e}")
        return []
    busy_from_gcal = []
    buffer_delta = datetime.timedelta(minutes=BREAK_BUFFER_MINUTES)
    for event in all_events:
        start_str = event['start'].get('dateTime')
        end_str = event['end'].get('dateTime')
        if not start_str or not end_str: continue
        start_dt = datetime.datetime.fromisoformat(start_str.replace('Z', '+00:00')).astimezone(tz)
        end_dt = datetime.datetime.fromisoformat(end_str.replace('Z', '+00:00')).astimezone(tz)
        if event.get('summary', '').upper() != 'GRAFIK':
            start_dt -= buffer_delta
            end_dt += buffer_delta
        busy_from_gcal.append((start_dt, end_dt))
    available_slots = []
    duration_delta = datetime.timedelta(minutes=duration_minutes)
    search_start_dt = now + datetime.timedelta(hours=MIN_BOOKING_LEAD_HOURS)
    for day_offset in range(search_days):
        current_date = (now + datetime.timedelta(days=day_offset)).date()
        day_start_work = tz.localize(datetime.datetime.combine(current_date, datetime.time(WORK_START_HOUR)))
        day_end_work = tz.localize(datetime.datetime.combine(current_date, datetime.time(WORK_END_HOUR)))
        current_time = max(day_start_work, search_start_dt)
        today_busy_blocks = sorted([b for b in busy_from_gcal if b[0].date() == current_date])
        for busy_start, busy_end in today_busy_blocks:
            if current_time < busy_start:
                potential_slot = current_time
                while potential_slot + duration_delta <= busy_start:
                    available_slots.append(potential_slot)
                    potential_slot += datetime.timedelta(minutes=10)
            current_time = max(current_time, busy_end)
        if current_time < day_end_work:
            potential_slot = current_time
            while potential_slot + duration_delta <= day_end_work:
                available_slots.append(potential_slot)
                potential_slot += datetime.timedelta(minutes=10)
    return sorted(list(set(available_slots)))

def get_google_calendar_events(service, calendar_id):
    if not service: return []
    now = datetime.datetime.utcnow().isoformat() + 'Z'
    try:
        events_result = service.events().list(
            calendarId=calendar_id, timeMin=now, maxResults=25, singleEvents=False,
        ).execute()
        items = events_result.get('items', [])
        filtered_items = [e for e in items if "(NIEPOTWIERDZONE)" not in e.get('summary', '') and 'GRAFIK' not in e.get('summary', '').upper()]
        filtered_items.sort(key=lambda x: x['start'].get('dateTime', x['start'].get('date')))
        return filtered_items
    except HttpError as e: return []
    
def format_events_for_ai(events):
    if not events: return "Kalendarz jest pusty na najbliższe dni (brak potwierdzonych korepetycji)."
    tz = pytz.timezone(CALENDAR_TIMEZONE)
    formatted = []
    for event in events:
        start_str = event['start'].get('dateTime')
        if not start_str: continue 
        start_dt = datetime.datetime.fromisoformat(start_str.replace('Z', '+00:00')).astimezone(tz)
        summary = event.get('summary', 'Brak nazwy')
        recurrence_info = " (Cykliczne)" if 'recurrence' in event else ""
        formatted_start = start_dt.strftime('%Y-%m-%d %H:%M:%S %Z')
        formatted.append(f"- ID: {event['id']}, Nazwa: {summary}{recurrence_info}, Start: {formatted_start}")
    return "\n".join(formatted)

def create_google_event(service, calendar_id, termin_iso, summary, description=None, recurrence_rule=None, color_id=None):
    if not service: return False, "Brak połączenia z API"
    start_dt = datetime.datetime.fromisoformat(termin_iso)
    if start_dt.tzinfo is None: start_dt = pytz.timezone(CALENDAR_TIMEZONE).localize(start_dt)
    end_dt = start_dt + datetime.timedelta(minutes=APPOINTMENT_DURATION_MINUTES)
    
    event = {
        'summary': summary,
        'start': {'dateTime': start_dt.isoformat(), 'timeZone': CALENDAR_TIMEZONE},
        'end': {'dateTime': end_dt.isoformat(), 'timeZone': CALENDAR_TIMEZONE}
    }
    
    if description:
        event['description'] = description
    if recurrence_rule: 
        event['recurrence'] = [recurrence_rule]
    if color_id: 
        event['colorId'] = color_id
        
    try:
        created_event = service.events().insert(calendarId=calendar_id, body=event).execute()
        return True, created_event
    except HttpError as e: 
        return False, f"Błąd API podczas tworzenia: {e}"
def stworz_instrukcje_POTWIERDZENIE(dane_lekcji, info_o_uslugach_str):
    szczegoly_lekcji_str = json.dumps(dane_lekcji.get('fields', {}), indent=2, ensure_ascii=False)
    instrukcja = f"""
    Jesteś asystentem klienta. Twoim **głównym celem** jest doprowadzenie do potwierdzenia przez użytkownika lekcji. Twoja odpowiedź ZAWSZE musi być w formacie JSON.
    --- SZCZEGÓŁY LEKCJI DO POTWIERDZENIA ---
    {szczegoly_lekcji_str}
    --- OGÓLNE INFORMACJE O USŁUGACH ---
    {info_o_uslugach_str}
    TWOJA LOGIKA DZIAŁANIA:
    1.  **PRIORYTET #1: BĄDŹ POMOCNY.** Jeśli pierwsza wiadomość użytkownika to pytanie, odpowiedz na nie wyczerpująco.
    2.  **CEL GŁÓWNY:** ZAWSZE na końcu swojej pomocnej odpowiedzi, przypomnij o lekcji do potwierdzenia.
    3.  Jeśli pierwsza wiadomość użytkownika nie jest pytaniem, od razu przejdź do celu głównego.
    4.  Gdy użytkownik się zgodzi, użyj akcji `POTWIERDZ_I_UTWORZ_WYDARZENIE`.
    --- PRZYKŁADY AKCJI ---
    1. ROZMOWA: {{ "action": "ROZMOWA", "details": {{}}, "user_response": "Cena za liceum to 70 zł. A propos, widzę, że mamy dla Ciebie lekcję na jutro. Czy potwierdzamy?" }}
    2. POTWIERDZ_I_UTWORZ_WYDARZENIE: {{ "action": "POTWIERDZ_I_UTWORZ_WYDARZENIE", "details": {{}}, "user_response": "Świetnie! Potwierdziłem Twoją lekcję. Do zobaczenia!" }}
    """
    return instrukcja

def stworz_instrukcje_STANDARDOWA(dostepne_sloty_str, aktualne_wydarzenia_str, info_o_uslugach_str):
    instrukcja = f"""
    Jesteś systemem AI, który zarządza prawdziwym Kalendarzem Google. Twoja odpowiedź MUSI być jednym, kompletnym obiektem JSON.
    --- GŁÓWNE DYREKTYWY ---
    1.  **ŚWIADOMOŚĆ DANYCH:** Działasz na prawdziwych danych z eventId.
    2.  **PROAKTYWNE DOPYTYWANIE:** Gdy użytkownik chce umówić termin, ZAWSZE najpierw zapytaj, czy ma być jednorazowy czy cykliczny.
    3.  **DWUSTOPNIOWE UMAWIANIE:** ZAWSZE najpierw pytaj o ogólne preferencje, a dopiero potem proponuj JEDEN konkretny termin.
    --- OGÓLNE INFORMACJE O USŁUGACH ---
    {info_o_uslugach_str}
    AKTUALNE WYDARZENIA W KALENDARZU:
    {aktualne_wydarzenia_str}
    DOSTĘPNE SLOTY DO REZERWACJI:
    {dostepne_sloty_str}
    --- BIBLIOTEKA PRZYKŁADÓW AKCJI ---
    1. ROZMOWA (inicjacja): {{ "action": "ROZMOWA", "details": {{}}, "user_response": "Jasne, chętnie pomogę. Czy te zajęcia mają być jednorazowe, czy cykliczne, powtarzające się co tydzień?" }}
    2. ROZMOWA (preferencje): {{ "action": "ROZMOWA", "details": {{}}, "user_response": "Rozumiem. W takim razie proszę podać preferowany dzień tygodnia lub porę dnia, a ja znajdę najlepszy termin." }}
    3. ZAPROPONUJ_TERMIN: {{ "action": "ZAPROPONUJ_TERMIN", "details": {{"proponowany_termin_iso": "2024-07-26T18:20:00+02:00"}}, "user_response": "Znalazłem wolny termin w piątek o 18:20. Czy pasuje?"}}
    4. DOPISZ_ZAJECIA: {{ "action": "DOPISZ_ZAJECIA", "details": {{ "nowy_termin_iso": "2024-07-26T18:20:00+02:00", "summary": "Korepetycje" }}, "user_response": "Świetnie! Zapisałem korepetycje na ten termin." }}
    """
    return instrukcja

# =====================================================================
# === KONIEC PIERWSZEJ POŁOWY KODU ===
# =====================================================================
# =====================================================================
# === POCZĄTEK DRUGIEJ POŁOWY KODU ===
# =====================================================================

def uruchom_logike_potwierdzania(user_psid, message_text, record_data, historia_konwersacji, calendar_id):
    """Uruchamia wyspecjalizowaną logikę AI, której celem jest potwierdzenie rezerwacji."""
    calendar_service = get_calendar_service(CALENDAR_SERVICE_ACCOUNT_FILE, CALENDAR_SCOPES)
    model = genai.GenerativeModel('gemini-1.5-pro-latest')
    
    info_o_uslugach_str = json.dumps(SERVICE_INFO, indent=2, ensure_ascii=False)
    
    prompt_do_wyslania = [
        {'role': 'user', 'parts': [{'text': stworz_instrukcje_POTWIERDZENIE(record_data, info_o_uslugach_str)}]},
        {'role': 'model', 'parts': [{'text': "OK, rozumiem. Moim celem jest potwierdzenie rezerwacji, ale najpierw odpowiem na pytania użytkownika."}]}
    ] + historia_konwersacji
    
    response = model.generate_content(prompt_do_wyslania)
    
    try:
        raw_text = response.text
        cleaned_text = re.sub(r'^```json\s*|\s*```$', '', raw_text, flags=re.MULTILINE).strip()
        decyzja_ai = json.loads(cleaned_text)
        akcja = decyzja_ai.get("action")
        odpowiedz_tekstowa = decyzja_ai.get("user_response")
        if not akcja or not odpowiedz_tekstowa: raise ValueError("Niekompletna odpowiedź AI.")
    except (json.JSONDecodeError, ValueError) as e:
        print(f"Bot (wątek {user_psid}): Błąd parsowania w logice potwierdzania: {e}")
        send_message(user_psid, "Przepraszam, mam chwilowy problem techniczny. Spróbuj ponownie.")
        return historia_konwersacji

    send_message(user_psid, odpowiedz_tekstowa)

    if akcja == "POTWIERDZ_I_UTWORZ_WYDARZENIE":
        record_id = record_data.get('id')
        fields = record_data.get('fields', {})
        
        # Pobieramy wszystkie potrzebne dane ucznia z Airtable
        uczen_imie = fields.get('Imię Ucznia')
        uczen_nazwisko = fields.get('Nazwisko Ucznia')
        termin_iso = fields.get('Date') # Zakładam, że data jest w tym polu

        # Sprawdzamy, czy mamy wszystkie kluczowe dane
        if record_id and termin_iso and uczen_imie and uczen_nazwisko:
            
            # NOWA LOGIKA TWORZENIA WYDARZENIA
            # 1. Stwórz nowy tytuł i opis zgodnie z wymaganiami
            nowy_tytul = f"{uczen_imie} {uczen_nazwisko}"
            nowy_opis = stworz_opis_wydarzenia(fields)

            # 2. Usuń stare, niepotwierdzone wydarzenie z kalendarza (szukając po imieniu i nazwisku UCZNIA)
            delete_unconfirmed_event(calendar_service, calendar_id, uczen_imie, uczen_nazwisko)

            # 3. Zaktualizuj status w Airtable
            update_success, _ = update_airtable_status(record_id, "Potwierdzone")

            # 4. Utwórz nowe, potwierdzone wydarzenie w kalendarzu z nowym tytułem i opisem
            if update_success:
                create_success, result = create_google_event(
                    service=calendar_service, 
                    calendar_id=calendar_id, 
                    termin_iso=termin_iso, 
                    summary=nowy_tytul, 
                    description=nowy_opis
                )
                if not create_success:
                    send_message(user_psid, "UWAGA: Wystąpił błąd przy tworzeniu nowego, potwierdzonego wydarzenia w Kalendarzu Google. Skontaktuj się z administratorem.")
            else:
                 send_message(user_psid, "UWAGA: Nie udało się zaktualizować statusu w bazie danych. Nowe wydarzenie nie zostało utworzone.")

        else:
            send_message(user_psid, "UWAGA: Brak kluczowych danych (ID, termin, dane ucznia) w rekordzie Airtable do potwierdzenia rezerwacji. Nie można kontynuować.")
            
    historia_konwersacji.append({'role': 'model', 'parts': [{'text': json.dumps(decyzja_ai, ensure_ascii=False)}]})
    return historia_konwersacji


# --- ZMIENIONA FUNKCJA ---
def uruchom_glowna_logike_planowania(user_psid, message_text, historia_konwersacji, calendar_id, record_data):
    """Uruchamia standardową logikę planowania dla zweryfikowanych klientów."""
    calendar_service = get_calendar_service(CALENDAR_SERVICE_ACCOUNT_FILE, CALENDAR_SCOPES)
    model = genai.GenerativeModel('gemini-1.5-pro-latest')
    
    MAX_RETRIES = 3; decyzja_ai = None; proposal_verified = False
    for attempt in range(MAX_RETRIES):
        events = get_google_calendar_events(calendar_service, calendar_id)
        events_str_for_ai = format_events_for_ai(events)
        available_slots = find_available_slots_gcal(calendar_service, calendar_id, APPOINTMENT_DURATION_MINUTES, SEARCH_DAYS)
        available_slots_text_for_ai = "\n".join([slot.isoformat() for slot in available_slots])
        if not available_slots_text_for_ai:
            available_slots_text_for_ai = "Brak dostępnych terminów w najbliższym czasie."
        
        info_o_uslugach_str = json.dumps(SERVICE_INFO, indent=2, ensure_ascii=False)
        
        prompt_do_wyslania = [
            {'role': 'user', 'parts': [{'text': stworz_instrukcje_STANDARDOWA(available_slots_text_for_ai, events_str_for_ai, info_o_uslugach_str)}]},
            {'role': 'model', 'parts': [{'text': "OK, rozumiem. Działam na prawdziwym Kalendarzu Google."}]}
        ] + historia_konwersacji
        
        response = model.generate_content(prompt_do_wyslania)
        try:
            raw_text = response.text
            cleaned_text = re.sub(r'^```json\s*|\s*```$', '', raw_text, flags=re.MULTILINE).strip()
            decyzja_ai = json.loads(cleaned_text)
            if "action" not in decyzja_ai or "user_response" not in decyzja_ai:
                raise ValueError("Odpowiedź AI jest niekompletna.")
            
            proposal_verified = True; break
        except (json.JSONDecodeError, ValueError) as e:
            print(f"Bot (wątek {user_psid}): Błąd parsowania w głównej logice: {e}")
            proposal_verified = False; break
    
    if not proposal_verified:
        send_message(user_psid, "Przepraszam, mam chwilowy problem z przetworzeniem Twojej prośby.")
        return historia_konwersacji

    akcja = decyzja_ai.get("action")
    szczegoly = decyzja_ai.get("details", {})
    odpowiedz_tekstowa = decyzja_ai.get("user_response")
    
    print(f"--- DEBUG (wątek {user_psid}): AI chce wykonać akcję: '{akcja}' ze szczegółami: {szczegoly} ---")
    send_message(user_psid, odpowiedz_tekstowa)
    
    # --- ZMIENIONA LOGIKA TWORZENIA WYDARZENIA ---
    if akcja == "DOPISZ_ZAJECIA":
        nowy_termin_iso = szczegoly.get("nowy_termin_iso")
        if nowy_termin_iso and record_data:
            fields = record_data.get('fields', {})
            uczen_imie = fields.get('Imię Ucznia')
            uczen_nazwisko = fields.get('Nazwisko Ucznia')
            
            if uczen_imie and uczen_nazwisko:
                # Tytuł i opis są teraz tworzone w ujednolicony sposób
                summary = f"{uczen_imie} {uczen_nazwisko}"
                description = stworz_opis_wydarzenia(fields)
                
                success, result = create_google_event(
                    service=calendar_service, 
                    calendar_id=calendar_id, 
                    termin_iso=nowy_termin_iso, 
                    summary=summary,
                    description=description
                )
                if success:
                    print(f"--- Utworzono szczegółowe wydarzenie: {result.get('htmlLink')} ---")
                else:
                    send_message(user_psid, "Niestety, wystąpił błąd podczas dodawania zajęć do kalendarza.")
            else:
                send_message(user_psid, "Błąd: Nie można utworzyć wydarzenia, brak danych ucznia w bazie.")
                print(f"BŁĄD KRYTYCZNY (wątek {user_psid}): Brak Imienia/Nazwiska ucznia w record_data przy akcji DOPISZ_ZAJECIA.")

    historia_konwersacji.append({'role': 'model', 'parts': [{'text': json.dumps(decyzja_ai, ensure_ascii=False)}]})
    return historia_konwersacji

def process_message(user_psid, message_text):
    first_name, last_name = get_user_profile(user_psid)
    if not first_name or not last_name:
        send_message(user_psid, "Przepraszam, mam problem z weryfikacją Twojego konta na Facebooku.")
        return

    user_status, record_data = check_user_status_in_airtable(first_name, last_name)
    
    assigned_calendar_id = None
    if user_status == "OK_PROCEED" or user_status == "AWAITING_CONFIRMATION":
        assigned_calendar_name = record_data.get('fields', {}).get('Nazwa Kalendarza')
        if assigned_calendar_name:
            assigned_calendar_id = CALENDAR_NAME_TO_ID.get(assigned_calendar_name)
        if not assigned_calendar_id:
            print(f"--- BŁĄD KONFIGURACJI KLIENTA: Użytkownik {first_name} {last_name} ma nieprawidłową lub brak nazwy kalendarza: '{assigned_calendar_name}'. Traktuję jak NOT_FOUND. ---")
            user_status = "NOT_FOUND" 

    os.makedirs(HISTORY_DIR, exist_ok=True)
    history_file = os.path.join(HISTORY_DIR, f"{user_psid}.json")
    try:
        with open(history_file, 'r', encoding='utf-8') as f:
            historia_konwersacji = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        historia_konwersacji = []
        
    historia_konwersacji.append({'role': 'user', 'parts': [{'text': message_text}]})

    if user_status == "NOT_FOUND":
        send_message(user_psid, "Witaj! Wygląda na to, że jesteś nowym klientem lub w Twojej rezerwacji brakuje kluczowych informacji. Aby umówić pierwsze zajęcia, skontaktuj się z nami bezpośrednio.")
        return 
    
    if user_status == "AWAITING_CONFIRMATION":
        print(f"--- Uruchamianie logiki POTWIERDZANIA dla {user_psid} ---")
        historia_konwersacji = uruchom_logike_potwierdzania(user_psid, message_text, record_data, historia_konwersacji, assigned_calendar_id)
    else: # OK_PROCEED
        print(f"--- Uruchamianie logiki STANDARDOWEJ dla {user_psid} ---")
        # ZMIANA: Przekazujemy `record_data`, aby funkcja miała dostęp do szczegółów klienta
        historia_konwersacji = uruchom_glowna_logike_planowania(user_psid, message_text, historia_konwersacji, assigned_calendar_id, record_data)

    with open(history_file, 'w', encoding='utf-8') as f:
        json.dump(historia_konwersacji[-20:], f, indent=2)

# --- WEBHOOK MESSENGERA ---
@app.route('/webhook2', methods=['GET', 'POST'])
def webhook():
    if request.method == 'GET':
        token_sent = request.args.get("hub.verify_token")
        if token_sent == VERIFY_TOKEN:
            return request.args.get("hub.challenge")
        return 'Invalid verification token', 403
    
    elif request.method == 'POST':
        data = request.get_json()
        print(json.dumps(data, indent=2))
        if data.get("object") == "page":
            for entry in data.get("entry", []):
                page_id = entry.get("id")
                for messaging_event in entry.get("messaging", []):
                    if messaging_event.get("message"):
                        sender_psid = messaging_event["sender"]["id"]
                        message = messaging_event["message"]
                        if message.get("text") and not message.get("is_echo"):
                            message_text = message["text"]
                            thread = threading.Thread(target=process_message, args=(sender_psid, message_text))
                            thread.start()
        return "ok", 200

# --- URUCHOMIENIE SERWERA ---
if __name__ == '__main__':
    if not CALENDARS_CONFIG:
        print("!!! ZAKOŃCZONO DZIAŁANIE: Konfiguracja kalendarzy nie została załadowana. Sprawdź plik 'config.json'.")
    else:
        print("Uruchamianie serwera Flask na porcie 8081...")
        app.run(host='0.0.0.0', port=8081, debug=True)
