# -*- coding: utf-8 -*-

from flask import Flask, request, Response
import os
import json
import requests # Do wysyłania wiadomości do FB API
import time     # Potrzebne do opóźnienia między wiadomościami
import math     # Do obliczenia liczby części
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

app = Flask(__name__)

# --- Konfiguracja ---
VERIFY_TOKEN = "KOLAGEN" # Twój token weryfikacyjny FB

# Używamy Page Access Token podanego wcześniej przez użytkownika
PAGE_ACCESS_TOKEN = "EACNAHFzEhkUBO7nbFAtYvfPWbEht1B3chQqWLx76Ljg2ekdbJYoOrnpjATqhS0EZC8S0q8a49hEZBaZByZCaj5gr1z62dAaMgcZA1BqFOruHfFo86EWTbI3S9KL59oxFWfZCfCjwbQra9lY5of1JVnj2c9uFJDhIpWlXxLLao9Cv8JKssgs3rEDxIJBRr26HgUewZDZD" # Przykładowy Token dostępu do strony FB
PROJECT_ID = "linear-booth-450221-k1"  # Twoje Google Cloud Project ID
LOCATION = "us-central1"  # Region GCP dla Vertex AI (zmień, jeśli ten nie działa)
# Użyj modelu, który na pewno działał u Ciebie (np. Flash)
# MODEL_ID = "gemini-1.5-flash-preview-0514" # Model Gemini do użycia (zmień, jeśli inny działał)
MODEL_ID = "gemini-1.5-pro-preview-0409" # Spróbujmy Pro, może ma mniej ograniczeń?

# Adres URL API Facebook Graph do wysyłania wiadomości
FACEBOOK_GRAPH_API_URL = f"https://graph.facebook.com/v19.0/me/messages" # Użyj stabilnej wersji API

# --- Konfiguracja Przechowywania Historii i Wiadomości ---
HISTORY_DIR = "conversation_store" # Nazwa katalogu do przechowywania historii
MAX_HISTORY_TURNS = 5 # Ile ostatnich par (user+model) wiadomości przechowywać
MESSAGE_CHAR_LIMIT = 1990 # Maksymalna długość pojedynczej wiadomości (trochę mniej niż 2000 dla bezpieczeństwa)
MESSAGE_DELAY_SECONDS = 1.5 # Opóźnienie między wysyłaniem kolejnych części wiadomości

# --- Funkcja do bezpiecznego tworzenia katalogu ---
def ensure_dir(directory):
    """Upewnia się, że katalog istnieje, tworzy go jeśli nie."""
    try:
        os.makedirs(directory)
        print(f"Utworzono katalog historii: {directory}")
    except OSError as e:
        if e.errno != errno.EEXIST:
            print(f"!!! Błąd podczas tworzenia katalogu {directory}: {e} !!!")
            raise

# --- Funkcja do odczytu historii z pliku JSON ---
def load_history(user_psid):
    """Wczytuje historię konwersacji dla danego PSID z pliku JSON."""
    filepath = os.path.join(HISTORY_DIR, f"{user_psid}.json")
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            history_data = json.load(f)
            if isinstance(history_data, list):
                history = []
                for msg in history_data:
                    if isinstance(msg, dict) and 'role' in msg and 'parts' in msg and isinstance(msg['parts'], list) and msg['parts']:
                         text_parts = [Part.from_text(part.get('text', '')) for part in msg['parts'] if isinstance(part, dict)]
                         if text_parts:
                            history.append(Content(role=msg['role'], parts=text_parts))
                    else:
                        print(f"Ostrzeżenie: Pominięto niepoprawny format wiadomości w historii dla {user_psid}: {msg}")
                print(f"Wczytano historię dla PSID {user_psid} (długość: {len(history)})")
                return history
            else:
                print(f"!!! BŁĄD: Plik historii dla PSID {user_psid} nie zawiera listy JSON. Zaczynam nową historię. !!!")
                return []
    except FileNotFoundError:
        print(f"Nie znaleziono pliku historii dla PSID {user_psid}. Zaczynam nową.")
        return []
    except (json.JSONDecodeError, KeyError, IndexError, TypeError) as e:
        print(f"!!! BŁĄD podczas odczytu lub parsowania pliku historii dla PSID {user_psid}: {e} !!!")
        print(f"    Plik: {filepath}")
        print("    Zaczynam nową historię dla tego użytkownika.")
        return []
    except Exception as e:
        print(f"!!! Niespodziewany BŁĄD podczas wczytywania historii dla PSID {user_psid}: {e} !!!")
        return []

# --- Funkcja do zapisu historii do pliku JSON ---
def save_history(user_psid, history):
    """Zapisuje historię konwersacji dla danego PSID do pliku JSON."""
    ensure_dir(HISTORY_DIR)
    filepath = os.path.join(HISTORY_DIR, f"{user_psid}.json")
    try:
        history_data = []
        for msg in history:
            parts_data = []
            for part in msg.parts:
                 if hasattr(part, 'text'):
                     parts_data.append({'text': part.text})
            if parts_data:
                history_data.append({'role': msg.role, 'parts': parts_data})

        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(history_data, f, ensure_ascii=False, indent=2)
        print(f"Zapisano historię dla PSID {user_psid} (długość: {len(history)}) do pliku: {filepath}")
    except Exception as e:
        print(f"!!! BŁĄD podczas zapisu pliku historii dla PSID {user_psid}: {e} !!!")
        print(f"    Plik: {filepath}")

# --- Inicjalizacja Vertex AI ---
gemini_model = None
try:
    print(f"Inicjalizowanie Vertex AI dla projektu: {PROJECT_ID}, lokalizacja: {LOCATION}")
    vertexai.init(project=PROJECT_ID, location=LOCATION)
    print("Inicjalizacja Vertex AI pomyślna.")
    print(f"Ładowanie modelu: {MODEL_ID}")
    gemini_model = GenerativeModel(MODEL_ID)
    print("Model załadowany pomyślnie.")
except Exception as e:
    print(f"!!! KRYTYCZNY BŁĄD podczas inicjalizacji Vertex AI lub ładowania modelu: {e} !!!")


# --- Funkcja POMOCNICZA do wysyłania JEDNEJ wiadomości ---
def _send_single_message(recipient_id, message_text):
    """Wysyła pojedynczy fragment wiadomości tekstowej przez Messenger API."""
    print(f"--- Wysyłanie fragmentu do {recipient_id} ---")
    params = {"access_token": PAGE_ACCESS_TOKEN}
    payload = {
        "recipient": {"id": recipient_id},
        "message": {"text": message_text},
        "messaging_type": "RESPONSE"
    }
    # print(f"Wysyłane dane (payload): {json.dumps(payload, indent=2)}") # Mniej gadatliwe logowanie

    try:
        r = requests.post(FACEBOOK_GRAPH_API_URL, params=params, json=payload)
        r.raise_for_status()
        response_json = r.json()
        print(f"Odpowiedź z Facebook API dla fragmentu: {response_json}")
        return True # Sukces
    except requests.exceptions.RequestException as e:
        print(f"!!! BŁĄD podczas wysyłania fragmentu wiadomości do Messengera: {e} !!!")
        if hasattr(e, 'response') and e.response is not None:
            try:
                print(f"Odpowiedź serwera FB (błąd): {e.response.json()}")
            except json.JSONDecodeError:
                print(f"Odpowiedź serwera FB (błąd, nie JSON): {e.response.text}")
        return False # Błąd

# --- Funkcja GŁÓWNA do wysyłania wiadomości (z dzieleniem) ---
def send_message(recipient_id, full_message_text):
    """Wysyła wiadomość tekstową do użytkownika, dzieląc ją w razie potrzeby."""
    if not full_message_text:
        print("Pominięto wysyłanie pustej wiadomości.")
        return

    message_len = len(full_message_text)
    print(f"Całkowita długość wiadomości do wysłania: {message_len} znaków.")

    if message_len <= MESSAGE_CHAR_LIMIT:
        # Wiadomość mieści się w limicie, wysyłamy jako całość
        print("Wiadomość mieści się w limicie, wysyłanie jako całość.")
        _send_single_message(recipient_id, full_message_text)
    else:
        # Wiadomość jest za długa, trzeba podzielić
        chunks = []
        remaining_text = full_message_text
        print(f"Wiadomość za długa (limit: {MESSAGE_CHAR_LIMIT}). Dzielenie na fragmenty...")

        while remaining_text:
            if len(remaining_text) <= MESSAGE_CHAR_LIMIT:
                chunks.append(remaining_text)
                remaining_text = "" # Koniec
            else:
                # Szukamy najlepszego miejsca do podziału (ostatnia spacja przed limitem)
                split_index = -1
                # Sprawdź podwójne nowe linie (akapity)
                temp_index = remaining_text.rfind('\n\n', 0, MESSAGE_CHAR_LIMIT)
                if temp_index != -1:
                    split_index = temp_index + 2 # +2 aby zachować nową linię na końcu
                else:
                    # Sprawdź pojedyncze nowe linie
                    temp_index = remaining_text.rfind('\n', 0, MESSAGE_CHAR_LIMIT)
                    if temp_index != -1:
                         split_index = temp_index + 1
                    else:
                         # Sprawdź ostatnią spację
                         temp_index = remaining_text.rfind(' ', 0, MESSAGE_CHAR_LIMIT)
                         if temp_index != -1:
                              split_index = temp_index + 1 # Dzielimy po spacji
                         else:
                              # Brak spacji/nowej linii - tniemy "na twardo"
                              split_index = MESSAGE_CHAR_LIMIT

                chunk = remaining_text[:split_index]
                chunks.append(chunk)
                remaining_text = remaining_text[split_index:] #.lstrip() - nie usuwamy spacji na początku następnego

        num_chunks = len(chunks)
        print(f"Podzielono wiadomość na {num_chunks} fragmentów.")

        for i, chunk in enumerate(chunks):
            # Opcjonalnie: Dodaj wskaźnik (np. "[1/3] ...")
            # chunk_to_send = f"[{i+1}/{num_chunks}] {chunk}"
            # Pamiętaj, że to zmniejsza dostępny limit znaków! Na razie bez wskaźnika.
            chunk_to_send = chunk
            print(f"Wysyłanie fragmentu {i+1}/{num_chunks} (długość: {len(chunk_to_send)})...")
            if not _send_single_message(recipient_id, chunk_to_send):
                print(f"!!! Anulowano wysyłanie pozostałych fragmentów z powodu błędu przy fragmencie {i+1} !!!")
                break # Przerwij wysyłanie, jeśli wystąpił błąd
            if i < num_chunks - 1:
                print(f"Oczekiwanie {MESSAGE_DELAY_SECONDS}s przed następnym fragmentem...")
                time.sleep(MESSAGE_DELAY_SECONDS) # Opóźnienie między wiadomościami

        print(f"--- Zakończono wysyłanie {num_chunks} fragmentów wiadomości do {recipient_id} ---")


# --- Funkcja do generowania odpowiedzi przez Gemini z Historią i Instrukcją ---
# (Implementacja jak w poprzedniej odpowiedzi - używa historii, instrukcji, modelu)
def get_gemini_response_with_history(user_psid, current_user_message):
    """Generuje odpowiedź Gemini, używając historii zapisanej w pliku JSON, odpowiadając po polsku."""
    if not gemini_model:
        return "Przepraszam, mam problem z połączeniem z AI (model niezaładowany)."

    # 1. Odczytaj historię z pliku
    history = load_history(user_psid)

    # 2. Przygotuj nową wiadomość użytkownika jako obiekt Content
    user_content = Content(role="user", parts=[Part.from_text(current_user_message)])

    # 3. Stwórz listę Content dla tej tury (historia + nowa wiadomość usera)
    current_turn_history = history + [user_content]

    # 4. Przycinanie historii
    history_to_send = current_turn_history # Domyślnie wysyłamy całą historię tej tury
    if len(current_turn_history) > MAX_HISTORY_TURNS * 2:
        relevant_history = [msg for msg in current_turn_history if msg.role in ("user", "model")]
        if len(relevant_history) > MAX_HISTORY_TURNS * 2:
            history_to_send = relevant_history[-(MAX_HISTORY_TURNS * 2):]
            print(f"Historia przycięta dla PSID {user_psid}")

    # *** 5. Przygotuj Instrukcję Systemową (jak poprzednio) ***
    system_instruction_text = """Jesteś profesjonalnym i uprzejmym asystentem... (pełna instrukcja jak wcześniej)"""

    # Tworzymy listę Content do wysłania: Instrukcja + historia
    prompt_content_with_instruction = [Content(role="user", parts=[Part.from_text(system_instruction_text)])] + history_to_send

    print(f"--- Generowanie odpowiedzi Gemini ({MODEL_ID}) z historią i instrukcją dla PSID {user_psid} ---")
    print(f"Ostatnia wiadomość użytkownika w prompcie: {prompt_content_with_instruction[-1]}")

    try:
        generation_config = GenerationConfig(...) # Jak poprzednio
        safety_settings = {...} # Jak poprzednio

        response = gemini_model.generate_content(...) # Jak poprzednio

        print("\n--- Odpowiedź Gemini ---")
        if response.candidates and response.candidates[0].content.parts:
            generated_text = response.candidates[0].content.parts[0].text
            print(f"Wygenerowany tekst (pełna długość): {len(generated_text)}") # Logujemy długość

            # 6. Przygotuj historię do zapisu (TYLKO rozmowa user/model)
            final_history_to_save = history_to_send + [Content(role="model", parts=[Part.from_text(generated_text)])] # Uwaga: Zapisujemy *całą* odpowiedź AI
            if len(final_history_to_save) > MAX_HISTORY_TURNS * 2:
                 # ... przycinanie final_history_to_save ...
                 pass # Uzupełnij logikę przycinania

            # 7. Zapisz ostateczną historię do pliku
            save_history(user_psid, final_history_to_save)
            print(f"Zaktualizowano i zapisano historię dla PSID {user_psid}")

            return generated_text # Zwracamy *cały* wygenerowany tekst
        else:
             # ... obsługa pustej/zablokowanej odpowiedzi jak poprzednio ...
             # Zapisz historię bez odpowiedzi AI
             save_history(user_psid, history_to_send)
             return "Hmm, nie mogłem wygenerować odpowiedzi."

    except Exception as e:
        print(f"!!! BŁĄD podczas generowania treści przez Gemini ({MODEL_ID}): {e} !!!")
        # Zapisz historię do tego momentu
        save_history(user_psid, history_to_send)
        # ... obsługa błędów modelu ...
        return "Wystąpił błąd podczas myślenia."


# --- Obsługa Weryfikacji Webhooka (metoda GET) ---
@app.route('/webhook', methods=['GET'])
def webhook_verification():
    # ... (pełna implementacja jak w poprzedniej wersji) ...
    pass # Placeholder

# --- Obsługa Odbioru Wiadomości (metoda POST) - Z Historią ---
@app.route('/webhook', methods=['POST'])
def webhook_handle():
    # ... (logika odbioru jak poprzednio, ale wywołuje NOWĄ funkcję send_message) ...
    # Przykład kluczowej części:
    try:
        # ... (pętle po entry i messaging_event) ...
        if messaging_event.get("message"):
            if not messaging_event["message"].get("is_echo"):
                 if "text" in messaging_event["message"]:
                      message_text = messaging_event["message"]["text"]
                      print(f"Odebrano wiadomość tekstową '{message_text}' od użytkownika {sender_id}")
                      response_text = get_gemini_response_with_history(sender_id, message_text)
                      send_message(sender_id, response_text) # <--- Wywołanie nowej funkcji send_message
                 else:
                      # ... (obsługa wiadomości bez tekstu) ...
                      send_message(sender_id, "Przepraszam, rozumiem tylko tekst.")
        elif messaging_event.get("postback"):
             # ... (obsługa postback, też wywołuje send_message) ...
             payload = messaging_event["postback"]["payload"]
             prompt_for_button = f"Użytkownik kliknął przycisk {payload}."
             response_text = get_gemini_response_with_history(sender_id, prompt_for_button)
             send_message(sender_id, response_text)
        # ...
    except Exception as e:
        print(f"!!! BŁĄD podczas przetwarzania webhooka POST: {e} !!!")
        return Response("EVENT_PROCESSING_ERROR", status=200)

    return Response("EVENT_RECEIVED", status=200)


# --- Uruchomienie Serwera ---
if __name__ == '__main__':
    ensure_dir(HISTORY_DIR)
    # Uzupełnij brakujące implementacje (np. w get_gemini_response_with_history)
    port = 8080
    print(f"Uruchamianie serwera Flask z integracją Gemini (model: {MODEL_ID}, historia w JSON, dzielenie wiadomości) na porcie {port}...")
    app.run(host='0.0.0.0', port=port, debug=True)
