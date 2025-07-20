import google.generativeai as genai
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from flask import Flask, request
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

# --- KONFIGURACJA MESSENGERA ---
FB_PAGE_ACCESS_TOKEN = os.environ.get("FB_PAGE_ACCESS_TOKEN", "EAAKusF6JViEBPNJiRftrqPmOy6CoZAWZBw3ZBEWl8dd7LtinSSF85JeKYXA3ZB7xlvFG6e5txU1i8RUEiskmZCXXyuIH4x4B4j4zBrOXm0AQyskcKBUaMVgS2o3AMZA2FWF0PNTuusd6nbxGPzGZAWyGoPP9rjDl1COwLk1YhTOsG7eaXa6FIxnXQaGFdB9oh7gdADaq7e4aQZDZD")
VERIFY_TOKEN = os.environ.get("FB_VERIFY_TOKEN", "KOLAGEN")

# --- KONFIGURACJA KALENDARZA GOOGLE ---
GOOGLE_CALENDAR_ID = '2d32166ec3d5e2387c4c411e2bbdb85c702f3b5b85955d1ae18c3bee76c7d8b8@group.calendar.google.com' 
CALENDAR_SERVICE_ACCOUNT_FILE = 'KALENDARZ_KLUCZ.json'
CALENDAR_SCOPES = ['https://www.googleapis.com/auth/calendar'] 
CALENDAR_TIMEZONE = 'Europe/Warsaw'

APPOINTMENT_DURATION_MINUTES = 60
SEARCH_DAYS = 14
WORK_START_HOUR = 8
WORK_END_HOUR = 22
MIN_BOOKING_LEAD_HOURS = 24
BREAK_BUFFER_MINUTES = 10

# --- MECHANIZMY BOTA ---
HELD_SLOTS = {} 
HOLD_DURATION_HOURS = 24
HISTORY_DIR = "conversation_history"

# --- Inicjalizacja API ---
try:
    genai.configure(api_key=API_KEY)
except Exception as e:
    print(f"Błąd konfiguracji API Gemini: {e}")
    exit()

# --- Inicjalizacja Aplikacji Webowej ---
app = Flask(__name__)

# --- FUNKCJE POMOCNICZE ---

def send_message(recipient_psid, message_text):
    """Wysyła wiadomość tekstową do użytkownika na Messengerze."""
    print(f"Wysyłanie do {recipient_psid}: '{message_text[:100]}...'")
    params = {"access_token": FB_PAGE_ACCESS_TOKEN}
    headers = {"Content-Type": "application/json"}
    # Dzielimy wiadomość na fragmenty, jeśli jest za długa
    chunks = [message_text[i:i + 2000] for i in range(0, len(message_text), 2000)]
    
    for chunk in chunks:
        data = json.dumps({
            "recipient": {"id": recipient_psid},
            "message": {"text": chunk},
            "messaging_type": "RESPONSE"
        })
        try:
            r = requests.post("https://graph.facebook.com/v19.0/me/messages", params=params, headers=headers, data=data)
            if r.status_code != 200:
                print(f"BŁĄD: Nie udało się wysłać wiadomości. Status: {r.status_code}, Odpowiedź: {r.text}")
        except Exception as e:
            print(f"BŁĄD: Wyjątek podczas wysyłania wiadomości: {e}")
        time.sleep(1) # Mała pauza między fragmentami

def get_calendar_service():
    if not os.path.exists(CALENDAR_SERVICE_ACCOUNT_FILE):
        print(f"!!! KRYTYCZNY BŁĄD: Brak pliku klucza '{CALENDAR_SERVICE_ACCOUNT_FILE}' w bieżącym folderze. !!!")
        return None
    try:
        creds = service_account.Credentials.from_service_account_file(
            CALENDAR_SERVICE_ACCOUNT_FILE, scopes=CALENDAR_SCOPES)
        service = build('calendar', 'v3', credentials=creds)
        print("--- Usługa Google Calendar API połączona (z uprawnieniami do zapisu). ---")
        return service
    except Exception as e:
        print(f"!!! KRYTYCZNY BŁĄD: Nie można połączyć się z Google Calendar API: {e} !!!")
        return None

def cleanup_and_get_active_held_slots():
    global HELD_SLOTS
    tz = pytz.timezone(CALENDAR_TIMEZONE)
    now = datetime.datetime.now(tz)
    expiration_delta = datetime.timedelta(hours=HOLD_DURATION_HOURS)
    slots_to_check = list(HELD_SLOTS.keys())
    for slot_iso in slots_to_check:
        hold_time = HELD_SLOTS[slot_iso]
        if now - hold_time > expiration_delta:
            print(f"--- BLOKADA WYGASŁA: Termin {slot_iso} został zwolniony. ---")
            del HELD_SLOTS[slot_iso]
    return list(HELD_SLOTS.keys())

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
    active_held_slots_iso = cleanup_and_get_active_held_slots()
    held_blocks = [(datetime.datetime.fromisoformat(iso), datetime.datetime.fromisoformat(iso) + datetime.timedelta(minutes=duration_minutes)) for iso in active_held_slots_iso]
    all_busy_blocks = busy_from_gcal + held_blocks
    available_slots = []
    duration_delta = datetime.timedelta(minutes=duration_minutes)
    search_start_dt = now + datetime.timedelta(hours=MIN_BOOKING_LEAD_HOURS)
    for day_offset in range(search_days):
        current_date = (now + datetime.timedelta(days=day_offset)).date()
        day_start_work = tz.localize(datetime.datetime.combine(current_date, datetime.time(WORK_START_HOUR)))
        day_end_work = tz.localize(datetime.datetime.combine(current_date, datetime.time(WORK_END_HOUR)))
        current_time = max(day_start_work, search_start_dt)
        today_busy_blocks = sorted([b for b in all_busy_blocks if b[0].date() == current_date])
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

def delete_google_event(service, calendar_id, event_id):
    if not service: return False, "Brak połączenia z API"
    try: service.events().delete(calendarId=calendar_id, eventId=event_id).execute(); return True, f"Wydarzenie {event_id} usunięte."
    except HttpError as e: return False, f"Błąd API podczas usuwania: {e}"

def create_google_event(service, calendar_id, termin_iso, summary, recurrence_rule=None, color_id=None):
    if not service: return False, "Brak połączenia z API"
    start_dt = datetime.datetime.fromisoformat(termin_iso)
    if start_dt.tzinfo is None: start_dt = pytz.timezone(CALENDAR_TIMEZONE).localize(start_dt)
    end_dt = start_dt + datetime.timedelta(minutes=APPOINTMENT_DURATION_MINUTES)
    event = {'summary': summary, 'start': {'dateTime': start_dt.isoformat(), 'timeZone': CALENDAR_TIMEZONE}, 'end': {'dateTime': end_dt.isoformat(), 'timeZone': CALENDAR_TIMEZONE}}
    if recurrence_rule: event['recurrence'] = [recurrence_rule]
    if color_id: event['colorId'] = color_id
    try:
        created_event = service.events().insert(calendarId=calendar_id, body=event).execute()
        return True, created_event
    except HttpError as e: return False, f"Błąd API podczas tworzenia: {e}"

def stworz_instrukcje_systemowa(dostepne_sloty_str, aktualne_wydarzenia_str):
    return f"""
    Jesteś systemem AI, który zarządza prawdziwym Kalendarzem Google. Twoja odpowiedź MUSI być jednym, kompletnym obiektem JSON.

    --- GŁÓWNE DYREKTYWY ---
    1.  **ŚWIADOMOŚĆ DANYCH:** Zawsze działasz na prawdziwych danych. Twoim źródłem prawdy jest poniższa lista wydarzeń. Odwołując lub przekładając, musisz używać `eventId` z tej listy.
    2.  **PROAKTYWNE DOPYTYWANIE:** Jesteś proaktywnym asystentem. Gdy użytkownik prosi o umówienie terminu, ale nie precyzuje typu, Twoim **pierwszym i jedynym zadaniem** jest zapytać o to. Użyj do tego akcji "ROZMOWA". NIGDY nie proponuj terminu, dopóki nie poznasz typu zajęć.
    3.  **DWUSTOPNIOWE UMAWIANIE:** NIGDY nie podawaj całej listy wolnych terminów, chyba że użytkownik o to wyraźnie poprosi. ZAWSZE najpierw zapytaj o ogólne preferencje (dzień, pora dnia). Dopiero potem, na podstawie odpowiedzi, zaproponuj JEDEN konkretny termin z listy.

    AKTUALNE WYDARZENIA W KALENDARZU:
    {aktualne_wydarzenia_str}
    DOSTĘPNE SLOTY DO REZERWACJI:
    {dostepne_sloty_str}
    --- BIBLIOTEKA PRZYKŁADÓW AKCJI ---
    1. Akcja: ROZMOWA (gdy inicjujesz proces rezerwacji)
       - Przykład JSON: {{ "action": "ROZMOWA", "details": {{}}, "user_response": "Jasne, chętnie pomogę. Czy te zajęcia mają być jednorazowe, czy cykliczne, powtarzające się co tydzień?" }}
    2. Akcja: ROZMOWA (gdy dopytujesz o preferencje terminu)
       - Przykład JSON: {{ "action": "ROZMOWA", "details": {{}}, "user_response": "Rozumiem. W takim razie proszę podać preferowany dzień tygodnia lub porę dnia (np. rano, popołudnie, wieczór), a ja znajdę najlepszy termin." }}
    3. Akcja: ZAPROPONUJ_TERMIN
       - Przykład JSON: {{ "action": "ZAPROPONUJ_TERMIN", "details": {{"proponowany_termin_iso": "2024-07-26T18:20:00+02:00"}}, "user_response": "Znalazłem wolny termin w piątek o 18:20. Czy pasuje?"}}
    4. Akcja: DOPISZ_ZAJECIA
       - Przykład JSON: {{ "action": "DOPISZ_ZAJECIA", "details": {{ "nowy_termin_iso": "2024-07-26T18:20:00+02:00", "summary": "Korepetycje" }}, "user_response": "Świetnie! Zapisałem korepetycje na ten termin." }}
    """

# --- GŁÓWNA LOGIKA BOTA (przeniesiona z `main` do funkcji) ---

def process_message(user_psid, message_text):
    calendar_service = get_calendar_service()
    if not calendar_service: 
        send_message(user_psid, "Przepraszam, mam problem z połączeniem z systemem kalendarza. Spróbuj ponownie później.")
        return
        
    model = genai.GenerativeModel('gemini-1.5-pro-latest')
    os.makedirs(HISTORY_DIR, exist_ok=True)
    history_file = os.path.join(HISTORY_DIR, f"{user_psid}.json")
    try:
        with open(history_file, 'r') as f:
            historia_konwersacji = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        historia_konwersacji = []
    
    historia_konwersacji.append({'role': 'user', 'parts': [{'text': message_text}]})
    
    MAX_RETRIES = 3; decyzja_ai = None; proposal_verified = False
    for attempt in range(MAX_RETRIES):
        events = get_google_calendar_events(calendar_service, GOOGLE_CALENDAR_ID)
        events_str_for_ai = format_events_for_ai(events)
        available_slots = find_available_slots_gcal(calendar_service, GOOGLE_CALENDAR_ID, APPOINTMENT_DURATION_MINUTES, SEARCH_DAYS)
        available_slots_text_for_ai = "\n".join([slot.isoformat() for slot in available_slots])
        if not available_slots_text_for_ai:
            available_slots_text_for_ai = "Brak dostępnych terminów w najbliższym czasie."
        prompt_do_wyslania = [
            {'role': 'user', 'parts': [{'text': stworz_instrukcje_systemowa(available_slots_text_for_ai, events_str_for_ai)}]},
            {'role': 'model', 'parts': [{'text': "OK, rozumiem. Działam na prawdziwym Kalendarzu Google."}]}
        ] + historia_konwersacji
        response = model.generate_content(prompt_do_wyslania)
        try:
            raw_text = response.text
            cleaned_text = re.sub(r'^```json\s*|\s*```$', '', raw_text, flags=re.MULTILINE).strip()
            decyzja_ai = json.loads(cleaned_text)
            if "action" not in decyzja_ai or "user_response" not in decyzja_ai:
                raise ValueError("Odpowiedź AI jest niekompletna.")
            if decyzja_ai.get("action") == "ZAPROPONUJ_TERMIN":
                proponowany_iso = decyzja_ai.get("details", {}).get("proponowany_termin_iso")
                if proponowany_iso:
                    cleanup_and_get_active_held_slots()
                    if proponowany_iso in HELD_SLOTS:
                        historia_konwersacji.append({'role': 'model', 'parts': [{'text': raw_text}]})
                        historia_konwersacji.append({'role': 'user', 'parts': [{'text': "Twoja ostatnia propozycja terminu okazała się już zajęta. Proszę, wybierz inny wolny termin."}]})
                        print(f"Bot (wątek {user_psid}): Chwileczkę, weryfikuję termin... (próba {attempt + 1}/{MAX_RETRIES})")
                        continue
                    else:
                        HELD_SLOTS[proponowany_iso] = datetime.datetime.now(pytz.timezone(CALENDAR_TIMEZONE))
                        print(f"--- BLOKADA ZAŁOŻONA: Termin {proponowany_iso} zablokowany na {HOLD_DURATION_HOURS}h. ---")
                        proposal_verified = True; break
                else: raise ValueError("Akcja ZAPROPONUJ_TERMIN nie zawiera terminu w 'details'.")
            else: 
                proposal_verified = True; break
        except (json.JSONDecodeError, ValueError) as e:
            print(f"Bot (wątek {user_psid}): Przepraszam, mam chwilowy problem. Spróbuj zadać pytanie inaczej.")
            print(f"(DEBUG: Błąd parsowania: {e}, Odpowiedź AI: {raw_text})")
            proposal_verified = False; break
    
    if not proposal_verified:
        send_message(user_psid, "Przepraszam, mam chwilowy problem z przetworzeniem Twojej prośby. Spróbuj zadać pytanie inaczej.")
        return

    akcja = decyzja_ai.get("action")
    szczegoly = decyzja_ai.get("details", {})
    odpowiedz_tekstowa = decyzja_ai.get("user_response")
    
    print(f"--- DEBUG (wątek {user_psid}): AI chce wykonać akcję: '{akcja}' ze szczegółami: {szczegoly} ---")
    send_message(user_psid, odpowiedz_tekstowa)
    
    if akcja == "DOPISZ_ZAJECIA" or akcja == "PRZELOZ_ZAJECIA":
        nowy_termin_iso = szczegoly.get("nowy_termin_iso")
        if nowy_termin_iso and nowy_termin_iso in HELD_SLOTS:
            del HELD_SLOTS[nowy_termin_iso]
            print(f"--- BLOKADA USUNIĘTA: Termin {nowy_termin_iso} został sfinalizowany. ---")
        if akcja == "DOPISZ_ZAJECIA":
            summary = szczegoly.get("summary", "Korepetycje")
            if nowy_termin_iso:
                success, result = create_google_event(calendar_service, GOOGLE_CALENDAR_ID, nowy_termin_iso, summary)
                if success: print(f"--- Utworzono wydarzenie: {result.get('htmlLink')} ---")
        elif akcja == "PRZELOZ_ZAJECIA":
            event_id = szczegoly.get("eventId")
            if event_id and nowy_termin_iso:
                event_details = get_google_event_details(calendar_service, GOOGLE_CALENDAR_ID, event_id)
                if event_details:
                    summary = event_details.get('summary', 'Przełożone zajęcia')
                    recurrence_rule = event_details.get('recurrence')
                    delete_success, _ = delete_google_event(calendar_service, GOOGLE_CALENDAR_ID, event_id)
                    if delete_success:
                        create_google_event(calendar_service, GOOGLE_CALENDAR_ID, nowy_termin_iso, summary, recurrence_rule)
    elif akcja == "ODWOLAJ_ZAJECIA":
        event_id = szczegoly.get("eventId")
        if event_id:
            success, message = delete_google_event(calendar_service, GOOGLE_CALENDAR_ID, event_id)
            if success: print(f"--- {message} ---")
    
    historia_konwersacji.append({'role': 'model', 'parts': [{'text': json.dumps(decyzja_ai, ensure_ascii=False)}]})
    with open(history_file, 'w') as f:
        json.dump(historia_konwersacji[-20:], f, indent=2)

# --- WEBHOOK MESSENGERA ---
@app.route('/webhook', methods=['GET', 'POST'])
def webhook():
    if request.method == 'GET':
        token_sent = request.args.get("hub.verify_token")
        if token_sent == VERIFY_TOKEN:
            return request.args.get("hub.challenge")
        return 'Invalid verification token', 403
    
    elif request.method == 'POST':
        data = request.get_json()
        if data.get("object") == "page":
            for entry in data.get("entry", []):
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
    print("Uruchamianie serwera Flask na porcie 8081...")
    app.run(host='0.0.0.0', port=8081, debug=True)
