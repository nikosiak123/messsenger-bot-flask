# -*- coding: utf-8 -*-

from flask import Flask, request, Response
import os
import json
import requests # Do wysyłania wiadomości do FB API
import time     # Potrzebne do opóźnienia między wiadomościami ORAZ do symulacji pisania
# math is imported but not used directly in splitting logic, can be removed if not needed elsewhere
# import math # Rzeczywiście nieużywane, można usunąć
import vertexai # Do komunikacji z Vertex AI
# Pełne importy z vertexai potrzebne dla Content, Part i Safety Settings
from vertexai.generative_models import (
    GenerativeModel,
    Part,
    Content,
    GenerationConfig,
    SafetySetting,
    HarmCategory,
    HarmBlockThreshold
)
import errno # Potrzebne do bezpiecznego tworzenia katalogu
import logging # Lepsza alternatywa dla print, ale zostawimy print zgodnie z oryginałem
import datetime # Potrzebne do pracy z datami/czasem
import pytz     # Potrzebne do stref czasowych

# --------- NOWY IMPORT ---------
# Importujemy funkcje kalendarza z naszego modułu
# Zakładamy, że plik calendar_utils.py jest w tym samym katalogu
from calendar_utils import (
    get_free_slots,
    book_appointment,
    _get_timezone, # Prywatna, ale przydatna do uzyskania obiektu tz
    DEFAULT_APPOINTMENT_DURATION # Użyjemy domyślnej długości z modułu
)
# ------------------------------

app = Flask(__name__)

# --- Konfiguracja ---
# W produkcji zalecane jest ładowanie tokenów ze zmiennych środowiskowych
VERIFY_TOKEN = os.environ.get("FB_VERIFY_TOKEN", "KOLAGEN") # Twój token weryfikacyjny FB
PAGE_ACCESS_TOKEN = os.environ.get("FB_PAGE_ACCESS_TOKEN", "EACNAHFzEhkUBO7nbFAtYvfPWbEht1B3chQqWLx76Ljg2ekdbJYoOrnpjATqhS0EZC8S0q8a49hEZBaZByZCaj5gr1z62dAaMgcZA1BqFOruHfFo86EWTbI3S9KL59oxFWfZCfCjwbQra9lY5of1JVnj2c9uFJDhIpWlXxLLao9Cv8JKssgs3rEDxIJBRr26HgUewZDZD") # WAŻNE: Podaj swój prawdziwy token!
PROJECT_ID = os.environ.get("GCP_PROJECT_ID", "linear-booth-450221-k1")  # Twoje Google Cloud Project ID
LOCATION = os.environ.get("GCP_LOCATION", "us-central1")  # Region GCP dla Vertex AI
# --- ZASADA 1: Użycie modelu wskazanego przez użytkownika ---
MODEL_ID = os.environ.get("VERTEX_MODEL_ID", "gemini-1.5-flash-001") # Zmieniono na nowszy model Flash

# Adres URL API Facebook Graph do wysyłania wiadomości
FACEBOOK_GRAPH_API_URL = f"https://graph.facebook.com/v19.0/me/messages" # Użyj stabilnej wersji API

# --- Konfiguracja Przechowywania Historii i Wiadomości ---
HISTORY_DIR = "conversation_store" # Nazwa katalogu do przechowywania historii
MAX_HISTORY_TURNS = 15 # Ile ostatnich par (user+model) wiadomości przechowywać
MESSAGE_CHAR_LIMIT = 1990 # Maksymalna długość pojedynczej wiadomości
MESSAGE_DELAY_SECONDS = 1.5 # Opóźnienie między wysyłaniem KOLEJNYCH CZĘŚCI wiadomości

# --- Konfiguracja Symulacji Pisania ---
ENABLE_TYPING_DELAY = True # Ustaw na False, aby wyłączyć symulację pisania
MIN_TYPING_DELAY_SECONDS = 0.8 # Minimalne opóźnienie nawet dla krótkich wiadomości
MAX_TYPING_DELAY_SECONDS = 3.5 # Maksymalne opóźnienie, aby nie czekać za długo
TYPING_CHARS_PER_SECOND = 30   # Szacowana szybkość "pisania"

# --- NOWA KONFIGURACJA DLA KALENDARZA ---
TARGET_CALENDAR_ID = 'f19e189826b9d6e36950da347ac84d5501ecbd6bed0d76c8641be61a67749c67@group.calendar.google.com' # ID Kalendarza do rezerwacji
APPOINTMENT_DURATION_MINUTES = DEFAULT_APPOINTMENT_DURATION # Użyj wartości z calendar_utils
MAX_SLOTS_TO_SHOW = 5 # Ile wolnych terminów pokazać użytkownikowi naraz
QUICK_REPLY_BOOK_PREFIX = "BOOK_SLOT_" # Prefiks dla payloadu przycisków rezerwacji
# ----------------------------------------

# --- Funkcja do bezpiecznego tworzenia katalogu ---
# (Bez zmian w stosunku do dostarczonego kodu)
def ensure_dir(directory):
    """Upewnia się, że katalog istnieje, tworzy go jeśli nie."""
    try:
        os.makedirs(directory)
        print(f"Utworzono katalog historii: {directory}")
    except OSError as e:
        if e.errno != errno.EEXIST:
            print(f"!!! Błąd podczas tworzenia katalogu {directory}: {e} !!!")
            raise

# --- Funkcja do pobierania danych profilu użytkownika ---
# (Pełna wersja, taka jak dostarczona przez Ciebie)
def get_user_profile(psid):
    """Pobiera imię, nazwisko i URL zdjęcia profilowego użytkownika z Facebook Graph API."""
    if not PAGE_ACCESS_TOKEN or PAGE_ACCESS_TOKEN == "EACNAHFzEhkUBO7nbFAtYvfPWbEht1B3chQqWLx76Ljg2ekdbJYoOrnpjATqhS0EZC8S0q8a49hEZBaZByZCaj5gr1z62dAaMgcZA1BqFOruHfFo86EWTbI3S9KL59oxFWfZCfCjwbQra9lY5of1JVnj2c9uFJDhIpWlXxLLao9Cv8JKssgs3rEDxIJBRr26HgUewZDZD" or len(PAGE_ACCESS_TOKEN) < 50:
         print(f"!!! [{psid}] Brak lub nieprawidłowy PAGE_ACCESS_TOKEN. Nie można pobrać profilu. !!!")
         return None
    # Zdefiniuj URL API Graph z użyciem szablonu - BRAKUJĄCY ELEMENT Z POPRZEDNIEJ WERSJI
    USER_PROFILE_API_URL_TEMPLATE = "https://graph.facebook.com/v19.0/{psid}?fields=first_name,last_name,profile_pic&access_token={token}"
    url = USER_PROFILE_API_URL_TEMPLATE.format(psid=psid, token=PAGE_ACCESS_TOKEN)
    print(f"--- [{psid}] Pobieranie profilu użytkownika...")

    profile_data = {}
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        data = r.json()
        if 'error' in data:
            print(f"!!! BŁĄD FB API (profil) {psid}: {data['error']} !!!"); return None
        profile_data['first_name'] = data.get('first_name')
        profile_data['last_name'] = data.get('last_name')
        profile_data['profile_pic'] = data.get('profile_pic')
        profile_data['id'] = data.get('id')
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
# (Bez zmian w stosunku do dostarczonego kodu - zawiera już walidację)
def load_history(user_psid):
    filepath = os.path.join(HISTORY_DIR, f"{user_psid}.json")
    history = []
    if not os.path.exists(filepath):
        # print(f"[{user_psid}] Nie znaleziono pliku historii. Zaczynam nową.") # Wiadomość już w bloku except FileNotFoundError
        return history
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            history_data = json.load(f)
            if isinstance(history_data, list):
                for i, msg_data in enumerate(history_data):
                    if (isinstance(msg_data, dict) and
                            'role' in msg_data and msg_data['role'] in ('user', 'model') and
                            'parts' in msg_data and isinstance(msg_data['parts'], list) and
                            msg_data['parts']):
                        text_parts = []
                        valid_parts = True
                        for part_data in msg_data['parts']:
                            if isinstance(part_data, dict) and 'text' in part_data and isinstance(part_data['text'], str):
                                text_parts.append(Part.from_text(part_data['text']))
                            else:
                                print(f"Ostrzeżenie [{user_psid}]: Niepoprawny format części w historii (indeks {i}): {part_data}. Pomijanie wiadomości.")
                                valid_parts = False
                                break
                        if valid_parts and text_parts:
                            history.append(Content(role=msg_data['role'], parts=text_parts))
                    else:
                        print(f"Ostrzeżenie [{user_psid}]: Pominięto niepoprawny format wiadomości w historii (indeks {i}): {msg_data}")
                print(f"[{user_psid}] Wczytano historię z pliku: {len(history)} poprawnych wiadomości.")
                return history
            else:
                print(f"!!! BŁĄD [{user_psid}]: Plik historii nie zawiera listy JSON. Zaczynam nową historię.")
                return []
    except FileNotFoundError: # Ten except jest technicznie redundantny przez wcześniejszy if not os.path.exists, ale zostawmy dla pewności
        print(f"[{user_psid}] Nie znaleziono pliku historii (ponownie). Zaczynam nową.")
        return []
    except (json.JSONDecodeError, KeyError, IndexError, TypeError) as e:
        print(f"!!! BŁĄD [{user_psid}] podczas odczytu lub parsowania pliku historii: {e} !!!")
        print(f"    Plik: {filepath}")
        print("    Zaczynam nową historię dla tego użytkownika.")
        return []
    except Exception as e:
        print(f"!!! Niespodziewany BŁĄD [{user_psid}] podczas wczytywania historii: {e} !!!")
        return []


# --- Funkcja do zapisu historii do pliku JSON (z zapisem atomowym) ---
# (Bez zmian w stosunku do dostarczonego kodu - zawiera już zapis atomowy i konwersję)
def save_history(user_psid, history):
    ensure_dir(HISTORY_DIR)
    filepath = os.path.join(HISTORY_DIR, f"{user_psid}.json")
    temp_filepath = f"{filepath}.tmp"
    history_data = []
    try:
        # Konwersja i walidacja przed zapisem
        for msg in history:
            if isinstance(msg, Content) and hasattr(msg, 'role') and msg.role in ('user', 'model') and hasattr(msg, 'parts') and isinstance(msg.parts, list):
                parts_data = [{'text': part.text} for part in msg.parts if isinstance(part, Part) and hasattr(part, 'text')]
                if parts_data: history_data.append({'role': msg.role, 'parts': parts_data})
                else: print(f"Ostrzeżenie [{user_psid}]: Pomijanie wiadomości bez części podczas zapisu (Rola: {msg.role})")
            else: print(f"Ostrzeżenie [{user_psid}]: Pomijanie nieprawidłowego obiektu podczas zapisu: {msg}")

        # Przycinanie historii DO ZAPISU (dodane dla pewności)
        max_messages_to_save = MAX_HISTORY_TURNS * 2
        if len(history_data) > max_messages_to_save:
            history_data = history_data[-max_messages_to_save:]
            print(f"[{user_psid}] Historia przycięta (w JSON) DO ZAPISU: {len(history_data)} wiadomości.")

        # Zapis atomowy
        with open(temp_filepath, 'w', encoding='utf-8') as f: json.dump(history_data, f, ensure_ascii=False, indent=2)
        os.replace(temp_filepath, filepath)
        print(f"[{user_psid}] Zapisano historię ({len(history_data)} wiadomości) do: {filepath}")
    except Exception as e:
        print(f"!!! BŁĄD [{user_psid}] podczas zapisu historii: {e} !!! Plik: {filepath}")
        if os.path.exists(temp_filepath):
            try: os.remove(temp_filepath); print(f"    Usunięto plik tymczasowy {temp_filepath}.")
            except OSError as remove_e: print(f"    Nie można usunąć pliku tymczasowego {temp_filepath}: {remove_e}")

# --- Inicjalizacja Vertex AI ---
# (Bez zmian w stosunku do dostarczonego kodu)
gemini_model = None
try:
    print(f"Inicjalizowanie Vertex AI dla projektu: {PROJECT_ID}, lokalizacja: {LOCATION}")
    vertexai.init(project=PROJECT_ID, location=LOCATION)
    print("Inicjalizacja Vertex AI pomyślna.")
    print(f"Ładowanie modelu: {MODEL_ID}")
    gemini_model = GenerativeModel(MODEL_ID)
    print("Model załadowany pomyślnie.")
except Exception as e:
    print(f"!!! KRYTYCZNY BŁĄD podczas inicjalizacji Vertex AI: {e} !!!")


# --- Funkcja POMOCNICZA do wysyłania JEDNEJ wiadomości ---
# (Bez zmian w stosunku do dostarczonego kodu)
def _send_single_message(recipient_id, message_text):
    """Wysyła pojedynczy fragment wiadomości tekstowej przez Messenger API."""
    print(f"--- Wysyłanie fragmentu do {recipient_id} (długość: {len(message_text)}) ---")
    params = {"access_token": PAGE_ACCESS_TOKEN}
    payload = {
        "recipient": {"id": recipient_id},
        "message": {"text": message_text},
        "messaging_type": "RESPONSE"
    }
    try:
        r = requests.post(FACEBOOK_GRAPH_API_URL, params=params, json=payload, timeout=30)
        r.raise_for_status()
        response_json = r.json()
        # print(f"Odpowiedź z Facebook API dla fragmentu: {response_json}") # Opcjonalne
        if response_json.get('error'):
            print(f"!!! BŁĄD zwrócony przez Facebook API: {response_json['error']} !!!")
            return False
        return True
    except requests.exceptions.Timeout:
        print(f"!!! BŁĄD TIMEOUT podczas wysyłania fragmentu do {recipient_id} !!!")
        return False
    except requests.exceptions.RequestException as e:
        print(f"!!! BŁĄD podczas wysyłania fragmentu do {recipient_id}: {e} !!!")
        if hasattr(e, 'response') and e.response is not None:
            try: print(f"Odpowiedź serwera FB (błąd): {e.response.json()}")
            except json.JSONDecodeError: print(f"Odpowiedź serwera FB (błąd, nie JSON): {e.response.text}")
        return False


# --- Funkcja GŁÓWNA do wysyłania wiadomości (z dzieleniem) ---
# (Bez zmian w stosunku do dostarczonego kodu)
def send_message(recipient_id, full_message_text):
    """Wysyła wiadomość tekstową do użytkownika, dzieląc ją w razie potrzeby."""
    if not full_message_text or not isinstance(full_message_text, str) or not full_message_text.strip():
        print(f"[{recipient_id}] Pominięto wysyłanie pustej wiadomości.")
        return
    message_len = len(full_message_text)
    print(f"[{recipient_id}] Przygotowanie wiadomości (dł: {message_len}).")
    if message_len <= MESSAGE_CHAR_LIMIT:
        _send_single_message(recipient_id, full_message_text)
    else:
        chunks = []; remaining_text = full_message_text
        print(f"[{recipient_id}] Dzielenie wiadomości (limit: {MESSAGE_CHAR_LIMIT})...")
        while remaining_text:
            if len(remaining_text) <= MESSAGE_CHAR_LIMIT:
                chunks.append(remaining_text.strip()); break
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
            print(f"[{recipient_id}] Wysyłanie fragmentu {i+1}/{num_chunks} (dł: {len(chunk)})...")
            if not _send_single_message(recipient_id, chunk):
                print(f"!!! [{recipient_id}] Anulowano wysyłanie reszty po błędzie fragm. {i+1} !!!"); break
            send_success_count += 1
            if i < num_chunks - 1:
                print(f"[{recipient_id}] Oczekiwanie {MESSAGE_DELAY_SECONDS}s..."); time.sleep(MESSAGE_DELAY_SECONDS)
        print(f"--- [{recipient_id}] Zakończono wysyłanie {send_success_count}/{num_chunks} fragmentów ---")

# --------- NOWA FUNKCJA ---------
def send_quick_replies(recipient_id, text, quick_replies_list):
    """Wysyła wiadomość z przyciskami szybkiej odpowiedzi."""
    if not PAGE_ACCESS_TOKEN or PAGE_ACCESS_TOKEN == "EACNAHFzEhkUBO7nbFAtYvfPWbEht1B3chQqWLx76Ljg2ekdbJYoOrnpjATqhS0EZC8S0q8a49hEZBaZByZCaj5gr1z62dAaMgcZA1BqFOruHfFo86EWTbI3S9KL59oxFWfZCfCjwbQra9lY5of1JVnj2c9uFJDhIpWlXxLLao9Cv8JKssgs3rEDxIJBRr26HgUewZDZD" or len(PAGE_ACCESS_TOKEN) < 50:
        print(f"!!! [{recipient_id}] Brak PAGE_ACCESS_TOKEN. Nie można wysłać QR.")
        return False
    if not quick_replies_list:
         print(f"[{recipient_id}] Brak przycisków QR. Wysyłanie zwykłej wiadomości.")
         return _send_single_message(recipient_id, text)

    print(f"--- Wysyłanie szybkich odpowiedzi do {recipient_id} ({len(quick_replies_list)} przycisków) ---")
    params = {"access_token": PAGE_ACCESS_TOKEN}
    headers = {'Content-Type': 'application/json'}

    fb_quick_replies = []
    for qr in quick_replies_list[:13]: # Limit 13 QR
         if isinstance(qr, dict) and "title" in qr and "payload" in qr:
             fb_quick_replies.append({
                 "content_type": "text",
                 "title": qr["title"][:20], # Limit 20 znaków
                 "payload": qr["payload"][:1000] # Limit 1000 znaków
             })
         else: print(f"Ostrzeżenie [{recipient_id}]: Pominięto nieprawidłowy format QR: {qr}")

    if not fb_quick_replies:
         print(f"!!! [{recipient_id}] Żaden przycisk QR nie miał poprawnego formatu. Wysyłanie zwykłej wiadomości.")
         return _send_single_message(recipient_id, text)

    if len(text) > MESSAGE_CHAR_LIMIT:
        print(f"Ostrzeżenie [{recipient_id}]: Tekst QR ({len(text)}) > limit {MESSAGE_CHAR_LIMIT}. Skracanie.")
        text = text[:MESSAGE_CHAR_LIMIT-3] + "..."

    payload = {
        "recipient": {"id": recipient_id},
        "messaging_type": "RESPONSE",
        "message": {"text": text, "quick_replies": fb_quick_replies}
    }
    try:
        r = requests.post(FACEBOOK_GRAPH_API_URL, params=params, headers=headers, json=payload, timeout=30)
        r.raise_for_status()
        response_json = r.json()
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
# ------------------------------

# --- ZMODYFIKOWANA INSTRUKCJA SYSTEMOWA (dodano obsługę akcji kalendarza) ---
# TODO: Zmień [Nazwa Twojej Firmy], [Twój Numer], [Twój Email] na prawdziwe dane
SYSTEM_INSTRUCTION_TEXT = """Jesteś profesjonalnym i uprzejmym asystentem obsługi klienta reprezentującym centrum specjalizujące się w wysokiej jakości korepetycjach online z matematyki, języka angielskiego i języka polskiego ('Zakręcone Korepetycje'). Obsługujemy uczniów od 4 klasy szkoły podstawowej aż do klasy maturalnej, oferując zajęcia zarówno na poziomie podstawowym, jak i rozszerzonym.

Twoim głównym celem jest aktywne zachęcanie klientów (uczniów lub ich rodziców) do skorzystania z naszych usług i **umówienia się na pierwszą lekcję próbną** (zgodną z cennikiem). Prezentuj ofertę rzeczowo, podkreślając korzyści płynące z nauki z naszymi doświadczonymi korepetytorami online.

Przebieg rozmowy (elastyczny przewodnik):
1.  Przywitaj się i zapytaj, w czym możesz pomóc w kwestii korepetycji.
2.  Ustal przedmiot zainteresowania (matematyka, j. polski, j. angielski).
3.  Ustal klasę ucznia.
4.  Dla szkoły średniej zapytaj o poziom (podstawowy/rozszerzony).
5.  Na podstawie zebranych informacji podaj cenę za 60-minutową lekcję (cennik poniżej).
6.  Po podaniu ceny, aktywnie zachęcaj do umówienia pierwszej lekcji.
7.  Informuj o formie zajęć (online przez MS Teams, bez instalacji) jeśli padnie takie pytanie.
8.  Odpowiadaj na inne pytania klienta najlepiej jak potrafisz, używając dostępnych informacji.

Cennik (za 60 min):
*   Klasy 4-8 SP: 60 zł
*   Klasy 1-3 LO/Technikum (podstawa): 65 zł
*   Klasy 1-3 LO/Technikum (rozszerzenie): 70 zł
*   Klasa 4 LO/Technikum (podstawa): 70 zł
*   Klasa 4 LO/Technikum (rozszerzenie): 75 zł

**<<< NOWA SEKCJA: Obsługa Umawiania Terminów >>>**
*   **Rozpoznawanie intencji:** Jeśli użytkownik wyraźnie pyta o możliwość umówienia się, dostępność terminów, rezerwację wizyty lub chce sprawdzić kalendarz (np. "Chcę umówić wizytę", "Jakie macie wolne terminy?", "Czy mogę się zapisać na przyszły tydzień?"), **NIE pytaj go o preferowaną datę**. Zamiast tego, **odpowiedz TYLKO I WYŁĄCZNIE specjalnym znacznikiem:** `[ACTION: FIND_SLOTS]`
*   **Ignorowanie preferencji:** Nawet jeśli użytkownik wspomni o preferowanym dniu lub porze (np. "Chcę się umówić na wtorek po południu"), Twoją odpowiedzią **musi być tylko** znacznik `[ACTION: FIND_SLOTS]`. System sam sprawdzi dostępne opcje.
*   **NIE używaj** znacznika `[ACTION: FIND_SLOTS]` w żadnym innym kontekście.

**Ważne zasady (pozostają w mocy):**
*   **Kontynuacja po przerwie:** **ZAWSZE** analizuj historię i kontynuuj rozmowę od miejsca, w którym została przerwana. **NIE ZACZYNAJ OD NOWA**.
*   Nie używaj formy "Pan/Pani" jeśli nie masz pewności co do płci.
*   Rozdzielaj wywiad na krótsze wiadomości.
*   Bądź perswazyjny, ale nie nachalny. Jeśli klient odmawia, zaproponuj zastanowienie się.
*   Jeśli nie znasz odpowiedzi, powiedz o tym i zaproponuj kontakt: tel. [Twój Numer], email: [Twój Email].
*   Odpowiadaj zawsze po polsku.
"""
# ---------------------------------------------------------------------

# --- ZMODYFIKOWANA FUNKCJA: Generuje odpowiedź LUB akcję ---
# (Nazwa zmieniona z get_gemini_response_with_history na get_gemini_response_or_action)
def get_gemini_response_or_action(user_psid, current_user_message):
    """
    Generuje odpowiedź Gemini lub specjalny znacznik akcji.
    Zwraca tekst odpowiedzi LUB znacznik akcji LUB komunikat błędu.
    """
    if not gemini_model:
        print(f"!!! KRYTYCZNY BŁĄD [{user_psid}]: Model Gemini niezaładowany!")
        # Zapisz historię do momentu błędu, aby nie utracić wiadomości użytkownika
        history = load_history(user_psid)
        user_content = Content(role="user", parts=[Part.from_text(current_user_message)])
        save_history(user_psid, history + [user_content]) # Zapisz stan przed błędem
        return "Przepraszam, wystąpił wewnętrzny problem techniczny (AI niedostępne)."

    history = load_history(user_psid)
    user_content = Content(role="user", parts=[Part.from_text(current_user_message)])
    full_conversation_for_save = history + [user_content] # Pełna historia do ewentualnego zapisu

    # Przycinanie historii DO WYSŁANIA (jak w Twoim kodzie)
    max_messages_to_send = MAX_HISTORY_TURNS * 2
    history_to_send = full_conversation_for_save[-max_messages_to_send:] if len(full_conversation_for_save) > max_messages_to_send else full_conversation_for_save
    if len(full_conversation_for_save) > max_messages_to_send:
        print(f"[{user_psid}] Historia przycięta DO WYSŁANIA do Gemini: {len(history_to_send)} wiadomości.")

    # Przygotowanie promptu z instrukcją (jak w Twoim kodzie)
    prompt_content_with_instruction = [
        Content(role="user", parts=[Part.from_text(SYSTEM_INSTRUCTION_TEXT)]),
        Content(role="model", parts=[Part.from_text("Rozumiem. Jestem gotów pomagać zgodnie z wytycznymi, w tym inicjować sprawdzanie terminów znacznikiem [ACTION: FIND_SLOTS].")])
    ] + history_to_send

    # Logowanie wysyłanej zawartości (jak w Twoim kodzie)
    print(f"\n--- [{user_psid}] Zawartość wysyłana do Gemini ({MODEL_ID}) ---")
    for i, content in enumerate(prompt_content_with_instruction):
        role = content.role; raw_text = content.parts[0].text
        text_fragment = raw_text[:150].replace('\n', '\\n')
        text_to_log = text_fragment + "..." if len(raw_text) > 150 else text_fragment
        print(f"  [{i}] Role: {role}, Text: '{text_to_log}'")
    print(f"--- Koniec zawartości dla {user_psid} ---\n")

    try:
        # Konfiguracja i generowanie (jak w Twoim kodzie)
        generation_config = GenerationConfig(temperature=0.7, top_p=0.95, top_k=40)
        safety_settings = {
            HarmCategory.HARM_CATEGORY_HARASSMENT: HarmBlockThreshold.BLOCK_MEDIUM_AND_ABOVE,
            HarmCategory.HARM_CATEGORY_HATE_SPEECH: HarmBlockThreshold.BLOCK_MEDIUM_AND_ABOVE,
            HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT: HarmBlockThreshold.BLOCK_MEDIUM_AND_ABOVE,
            HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT: HarmBlockThreshold.BLOCK_MEDIUM_AND_ABOVE,
        }
        response = gemini_model.generate_content(
            prompt_content_with_instruction, generation_config=generation_config,
            safety_settings=safety_settings, stream=False
        )
        # print(f"\n--- [{user_psid}] Odpowiedź Gemini (Raw) --- \n{response}\n----------") # Opcjonalne

        generated_text = ""
        # Przetwarzanie odpowiedzi (jak w Twoim kodzie)
        if response.candidates and response.candidates[0].content and response.candidates[0].content.parts:
            generated_text = response.candidates[0].content.parts[0].text.strip()

            # <<< --- NOWA LOGIKA: Sprawdzenie znacznika akcji --- >>>
            if generated_text == "[ACTION: FIND_SLOTS]":
                print(f"[{user_psid}] Gemini zwrócił akcję: FIND_SLOTS")
                save_history(user_psid, full_conversation_for_save) # Zapisz historię do wiad. użytkownika
                return "[ACTION: FIND_SLOTS]"
            # <<< --- KONIEC NOWEJ LOGIKI --- >>>

            # Normalna odpowiedź
            print(f"[{user_psid}] Wygenerowany tekst (dł: {len(generated_text)})")
            text_preview = generated_text[:150].replace('\n', '\\n'); print(f"   Fragment: '{text_preview}...'")
            # Zapisz odpowiedź modelu do historii
            model_content = Content(role="model", parts=[Part.from_text(generated_text)])
            final_history_to_save = full_conversation_for_save + [model_content]
            # Przytnij DO ZAPISU (jak w Twoim kodzie)
            max_messages_to_save = MAX_HISTORY_TURNS * 2
            if len(final_history_to_save) > max_messages_to_save:
                 final_history_to_save = final_history_to_save[-max_messages_to_save:]
                 print(f"[{user_psid}] Historia przycięta DO ZAPISU: {len(final_history_to_save)} wiadomości.")
            save_history(user_psid, final_history_to_save)
            return generated_text

        else:
            # Obsługa błędów / blokad Gemini (jak w Twoim kodzie)
            finish_reason = "UNKNOWN"; safety_ratings = []
            if response.candidates:
                 finish_reason_obj = response.candidates[0].finish_reason; finish_reason = finish_reason_obj.name if hasattr(finish_reason_obj, 'name') else str(finish_reason_obj)
                 safety_ratings = response.candidates[0].safety_ratings if response.candidates[0].safety_ratings else []
            print(f"!!! [{user_psid}] Odpowiedź Gemini pusta/zablokowana. Powód: {finish_reason}, Oceny: {safety_ratings} !!!")
            save_history(user_psid, full_conversation_for_save) # Zapisz do wiad. użytkownika
            if finish_reason == 'SAFETY': return "Przepraszam, treść wiadomości narusza zasady bezpieczeństwa."
            elif finish_reason == 'RECITATION': return "Wygląda na to, że moje źródła są ograniczone. Czy mogę pomóc inaczej?"
            else: return "Hmm, nie udało mi się wygenerować odpowiedzi. Spróbuj inaczej."

    except Exception as e:
        # Obsługa wyjątków (jak w Twoim kodzie)
        import traceback; print(f"!!! KRYTYCZNY BŁĄD Gemini ({MODEL_ID}) dla PSID {user_psid}: {e} !!!"); traceback.print_exc()
        save_history(user_psid, full_conversation_for_save) # Zapisz do wiad. użytkownika
        # Logika komunikatów o błędach (jak w Twoim kodzie)
        error_str = str(e).lower()
        if "permission denied" in error_str: return "Błąd: Brak uprawnień AI."
        elif "model" in error_str and "not found" in error_str: return f"Błąd: Model AI ('{MODEL_ID}') niedostępny."
        elif "deadline exceeded" in error_str: return "Błąd: AI nie odpowiedziało."
        elif "quota" in error_str: return "Błąd: Limit zapytań AI."
        elif "content" in error_str and "invalid" in error_str: return "Błąd: Wewnętrzny błąd AI."
        return "Wystąpił nieoczekiwany błąd techniczny."
# -----------------------------------------------------------

# --- Obsługa Weryfikacji Webhooka (GET) ---
# (Bez zmian w stosunku do dostarczonego kodu)
@app.route('/webhook', methods=['GET'])
def webhook_verification():
    print("--- Otrzymano żądanie GET weryfikacyjne ---")
    hub_mode = request.args.get('hub.mode'); hub_token = request.args.get('hub.verify_token'); hub_challenge = request.args.get('hub.challenge')
    print(f"Mode: {hub_mode}, Token: {'Obecny' if hub_token else 'Brak'}, Challenge: {'Obecny' if hub_challenge else 'Brak'}")
    if hub_mode == 'subscribe' and hub_token == VERIFY_TOKEN:
        print("Weryfikacja GET udana!"); return Response(hub_challenge, status=200, mimetype='text/plain')
    else: print(f"Weryfikacja GET nieudana."); return Response("Verification failed", status=403, mimetype='text/plain')

# --- Główna Obsługa Webhooka (POST) ---
# <<< --- ZMODYFIKOWANA LOGIKA OBSŁUGI WIADOMOŚCI --- >>>
@app.route('/webhook', methods=['POST'])
def webhook_handle():
    """Obsługuje przychodzące wiadomości, w tym logikę umawiania terminów."""
    print("\n------------------------------------------")
    print(f"--- {datetime.datetime.now()} Otrzymano POST ---")
    raw_data = request.data.decode('utf-8'); data = None
    try:
        data = json.loads(raw_data)
        if data and data.get("object") == "page":
            for entry in data.get("entry", []):
                page_id = entry.get("id"); timestamp = entry.get("time")
                print(f"  Przetwarzanie wpisu dla strony: {page_id} (czas: {timestamp})")
                for messaging_event in entry.get("messaging", []):
                    if "sender" not in messaging_event or "id" not in messaging_event["sender"]: continue
                    sender_id = messaging_event["sender"]["id"]
                    print(f"  -> Zdarzenie od PSID: {sender_id}")

                    # --- OBSŁUGA WIADOMOŚCI (Tekst, Szybkie Odpowiedzi, Załączniki) ---
                    if messaging_event.get("message"):
                        message_data = messaging_event["message"]; message_id = message_data.get("mid")
                        print(f"    Typ zdarzenia: message (ID: {message_id})")
                        if message_data.get("is_echo"): print("      Pominięto echo."); continue

                        user_input_text = None; quick_reply_payload = None
                        if "quick_reply" in message_data:
                            quick_reply_payload = message_data["quick_reply"].get("payload")
                            user_input_text = message_data.get("text", quick_reply_payload)
                            print(f"      Odebrano Quick Reply. Payload: '{quick_reply_payload}', Tekst: '{user_input_text}'")
                        elif "text" in message_data:
                            user_input_text = message_data["text"]
                            print(f"      Odebrano wiadomość tekstową: '{user_input_text}'")

                        # <<< --- NOWA LOGIKA: Sprawdź czy to rezerwacja --- >>>
                        if quick_reply_payload and quick_reply_payload.startswith(QUICK_REPLY_BOOK_PREFIX):
                            slot_iso_string = quick_reply_payload[len(QUICK_REPLY_BOOK_PREFIX):]
                            try:
                                tz = _get_timezone()
                                start_time = datetime.datetime.fromisoformat(slot_iso_string).astimezone(tz)
                                end_time = start_time + datetime.timedelta(minutes=APPOINTMENT_DURATION_MINUTES)
                                print(f"      Użytkownik wybrał slot: {start_time.strftime('%Y-%m-%d %H:%M %Z')}")
                                # Pobierz profil dla user_name - używając funkcji z Twojego kodu
                                user_profile = get_user_profile(sender_id)
                                user_name = user_profile.get('first_name', '') if user_profile else ''

                                if ENABLE_TYPING_DELAY: time.sleep(MIN_TYPING_DELAY_SECONDS)
                                success, message_to_user = book_appointment(
                                    TARGET_CALENDAR_ID, start_time, end_time,
                                    summary=f"Rezerwacja FB Bot",
                                    description=f"Rezerwacja przez bota FB.\nPSID: {sender_id}\nImię: {user_name}",
                                    user_name=user_name
                                )
                                send_message(sender_id, message_to_user)
                            except ValueError as ve: print(f"!!! BŁĄD [{sender_id}] parsowania ISO QR: {slot_iso_string}. {ve} !!!"); send_message(sender_id, "Błąd przetwarzania terminu.")
                            except Exception as book_err: print(f"!!! KRYTYCZNY BŁĄD [{sender_id}] rezerwacji: {book_err} !!!"); import traceback; traceback.print_exc(); send_message(sender_id, "Niespodziewany błąd rezerwacji.")

                        # <<< --- KONIEC LOGIKI REZERWACJI --- >>>

                        # --- Jeśli to nie była rezerwacja, ale jest tekst -> Przetwórz przez Gemini ---
                        elif user_input_text:
                             print(f"      Przekazywanie tekstu do Gemini...")
                             gemini_output = get_gemini_response_or_action(sender_id, user_input_text)

                             # <<< --- NOWA LOGIKA: Obsługa akcji FIND_SLOTS --- >>>
                             if gemini_output == "[ACTION: FIND_SLOTS]":
                                 print(f"      Wykonywanie akcji: FIND_SLOTS")
                                 tz = _get_timezone(); now = datetime.datetime.now(tz)
                                 search_start = now
                                 search_end_date = (now + datetime.timedelta(days=7)).date() # Szukaj na 7 dni wprzód
                                 search_end = tz.localize(datetime.datetime.combine(search_end_date, datetime.time(23, 59, 59)))
                                 if ENABLE_TYPING_DELAY: print(f"      Symulowanie szukania..."); time.sleep(MIN_TYPING_DELAY_SECONDS)
                                 free_slots = get_free_slots(TARGET_CALENDAR_ID, search_start, search_end, APPOINTMENT_DURATION_MINUTES)
                                 if free_slots:
                                     replies = []
                                     print(f"      Znaleziono {len(free_slots)} slotów. Tworzenie QR...")
                                     for slot_start in free_slots[:MAX_SLOTS_TO_SHOW]:
                                         try: slot_text = slot_start.strftime("%A, %d.%m %H:%M")
                                         except: day_names = ["Pon", "Wt", "Śr", "Czw", "Pt", "Sob", "Niedz"]; slot_text = f"{day_names[slot_start.weekday()]}, {slot_start.strftime('%d.%m %H:%M')}"
                                         replies.append({"title": slot_text, "payload": f"{QUICK_REPLY_BOOK_PREFIX}{slot_start.isoformat()}"})
                                     message_text = "Oto kilka najbliższych wolnych terminów:"
                                     send_quick_replies(sender_id, message_text, replies)
                                 else:
                                     print(f"      Nie znaleziono slotów do {search_end_date}.")
                                     send_message(sender_id, "Niestety, brak wolnych terminów w najbliższym tygodniu. Spróbuj później.")
                             # <<< --- KONIEC LOGIKI FIND_SLOTS --- >>>

                             # --- Jeśli to normalna odpowiedź Gemini ---
                             elif isinstance(gemini_output, str):
                                 print(f"      Otrzymano normalną odpowiedź Gemini.")
                                 if ENABLE_TYPING_DELAY: # Symulacja pisania
                                     response_len = len(gemini_output); calculated_delay = response_len / TYPING_CHARS_PER_SECOND
                                     final_delay = max(0, min(MAX_TYPING_DELAY_SECONDS, calculated_delay + MIN_TYPING_DELAY_SECONDS))
                                     if final_delay > 0: print(f"      Symulowanie pisania (Gemini)... {final_delay:.2f}s"); time.sleep(final_delay)
                                 send_message(sender_id, gemini_output)
                             else: # Błąd krytyczny w Gemini
                                  print(f"!!! [{sender_id}] Błąd z get_gemini_response_or_action.")
                                  send_message(sender_id, "Przepraszam, błąd przetwarzania wiadomości.")

                        # --- Obsługa załączników (jak w Twoim kodzie) ---
                        elif "attachments" in message_data:
                             attachment_type = message_data['attachments'][0].get('type', 'nieznany'); print(f"      Odebrano załącznik: {attachment_type}.")
                             send_message(sender_id, "Przepraszam, nie obsługuję załączników.")
                        # --- Inne (jak w Twoim kodzie) ---
                        else: print(f"      Odebrano nieznany typ wiadomości: {message_data}"); send_message(sender_id, "Otrzymałem wiadomość, ale nie potrafię jej zinterpretować.")

                    # --- Obsługa innych zdarzeń (Postback, Read, Delivery) ---
                    # (Logika bez zmian w stosunku do dostarczonego kodu)
                    elif messaging_event.get("postback"):
                         postback_data = messaging_event["postback"]; payload = postback_data.get("payload"); title = postback_data.get("title", payload)
                         print(f"    Typ zdarzenia: postback. Tytuł: '{title}', Payload: '{payload}'")
                         # Stwórz prompt i wywołaj Gemini (jak w Twoim kodzie)
                         prompt_for_button = f"Użytkownik kliknął przycisk: '{title}' (payload: {payload})."
                         # Zmieniono wywołanie na nową funkcję
                         response_text = get_gemini_response_or_action(sender_id, prompt_for_button)
                         # UWAGA: Tutaj nie ma logiki FIND_SLOTS ani rezerwacji dla postback! Dodaj, jeśli potrzebne.
                         if isinstance(response_text, str) and not response_text.startswith("[ACTION"):
                             if ENABLE_TYPING_DELAY: # Symulacja
                                 response_len = len(response_text); calculated_delay = response_len / TYPING_CHARS_PER_SECOND
                                 final_delay = max(0, min(MAX_TYPING_DELAY_SECONDS, calculated_delay + MIN_TYPING_DELAY_SECONDS))
                                 if final_delay > 0: print(f"      Symulowanie pisania (postback)... {final_delay:.2f}s"); time.sleep(final_delay)
                             send_message(sender_id, response_text)
                         elif response_text == "[ACTION: FIND_SLOTS]":
                              print("Ostrzeżenie: Gemini zwrócił akcję FIND_SLOTS dla postback, która nie jest obecnie obsługiwana w tym miejscu.")
                              send_message(sender_id, "Otrzymałem Twoją akcję, ale nie mogę jej teraz wykonać dla tego przycisku.")
                         else: # Błąd Gemini
                              send_message(sender_id, response_text or "Błąd przetwarzania akcji przycisku.") # Wyślij komunikat błędu zwrócony przez Gemini
                    elif messaging_event.get("read"): print(f"    Typ zdarzenia: read.")
                    elif messaging_event.get("delivery"): print(f"    Typ zdarzenia: delivery.")
                    else: print(f"    Odebrano inne zdarzenie messaging: {messaging_event}")

        else: print("Otrzymano POST o nieznanym typie obiektu:", data.get("object"))
    except json.JSONDecodeError as json_err: print(f"!!! KRYTYCZNY BŁĄD: Nie można zdekodować JSON: {json_err} !!!"); print(f"   Surowe dane: {raw_data[:500]}"); return Response("Invalid JSON", status=400)
    except Exception as e: import traceback; print(f"!!! KRYTYCZNY BŁĄD przetwarzania POST: {e} !!!"); traceback.print_exc(); return Response("EVENT_PROCESSING_ERROR", status=200)
    return Response("EVENT_RECEIVED", status=200)

# --- Uruchomienie Serwera ---
# (Logika startowa bez zmian w stosunku do dostarczonego kodu, dodano walidację tokena FB)
if __name__ == '__main__':
    ensure_dir(HISTORY_DIR)
    port = int(os.environ.get("PORT", 8080))
    debug_mode = os.environ.get("FLASK_DEBUG", "False").lower() in ("true", "1", "yes")

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
        try: from waitress import serve; print("Uruchamianie serwera produkcyjnego Waitress..."); serve(app, host='0.0.0.0', port=port)
        except ImportError: print("Waitress nie jest zainstalowany. Uruchamianie wbudowanego serwera Flask (niezalecane na produkcji)."); app.run(host='0.0.0.0', port=port, debug=False)
