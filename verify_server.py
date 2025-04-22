# -*- coding: utf-8 -*-

from flask import Flask, request, Response
import os
import json
import requests # Do wysyłania wiadomości do FB API
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
PAGE_ACCESS_TOKEN = "EACNAHFzEhkUBO7nbFAtYvfPWbEht1B3chQqWLx76Ljg2ekdbJYoOrnpjATqhS0EZC8S0q8a49hEZBaZByZCaj5gr1z62dAaMgcZA1BqFOruHfFo86EWTbI3S9KL59oxFWfZCfCjwbQra9lY5of1JVnj2c9uFJDhIpWlXxLLao9Cv8JKssgs3rEDxIJBRr26HgUewZDZD" # Token dostępu do strony FB
PROJECT_ID = "linear-booth-450221-k1"  # Twoje Google Cloud Project ID
LOCATION = "us-central1"  # Region GCP dla Vertex AI (zmień, jeśli ten nie działa)
MODEL_ID = "gemini-1.5-flash-preview-0514" # Model Gemini do użycia (zmień, jeśli inny działał)

# Adres URL API Facebook Graph do wysyłania wiadomości
FACEBOOK_GRAPH_API_URL = f"https://graph.facebook.com/v19.0/me/messages" # Użyj stabilnej wersji API

# --- Konfiguracja Przechowywania Historii ---
HISTORY_DIR = "conversation_store" # Nazwa katalogu do przechowywania historii
MAX_HISTORY_TURNS = 5 # Ile ostatnich par (user+model) wiadomości przechowywać

# --- Funkcja do bezpiecznego tworzenia katalogu ---
def ensure_dir(directory):
    """Upewnia się, że katalog istnieje, tworzy go jeśli nie."""
    try:
        os.makedirs(directory)
        print(f"Utworzono katalog historii: {directory}")
    except OSError as e:
        if e.errno != errno.EEXIST: # Ignoruj błąd, jeśli katalog już istnieje
            print(f"!!! Błąd podczas tworzenia katalogu {directory}: {e} !!!")
            raise # Rzuć inny błąd dalej, bo to może być problem z uprawnieniami

# --- Funkcja do odczytu historii z pliku JSON ---
def load_history(user_psid):
    """Wczytuje historię konwersacji dla danego PSID z pliku JSON."""
    filepath = os.path.join(HISTORY_DIR, f"{user_psid}.json")
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            history_data = json.load(f)
            # Sprawdzamy, czy to lista i konwertujemy słowniki na obiekty Content
            if isinstance(history_data, list):
                history = []
                for msg in history_data:
                    # Prosta walidacja struktury przed konwersją
                    if isinstance(msg, dict) and 'role' in msg and 'parts' in msg and isinstance(msg['parts'], list) and msg['parts']:
                         # Zakładamy, że parts zawiera listę słowników z kluczem 'text'
                         text_parts = [Part.from_text(part.get('text', '')) for part in msg['parts'] if isinstance(part, dict)]
                         if text_parts: # Dodaj tylko jeśli są jakieś części tekstowe
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
    except Exception as e: # Łapanie innych potencjalnych błędów
        print(f"!!! Niespodziewany BŁĄD podczas wczytywania historii dla PSID {user_psid}: {e} !!!")
        return []


# --- Funkcja do zapisu historii do pliku JSON ---
def save_history(user_psid, history):
    """Zapisuje historię konwersacji dla danego PSID do pliku JSON."""
    ensure_dir(HISTORY_DIR) # Upewnij się, że katalog istnieje
    filepath = os.path.join(HISTORY_DIR, f"{user_psid}.json")
    try:
        # Konwertujemy obiekty Content na format możliwy do zapisu w JSON
        # Upewniamy się, że obsługujemy części poprawnie
        history_data = []
        for msg in history:
            parts_data = []
            # Iterujemy przez części i zapisujemy tylko tekst (najczęstszy przypadek)
            for part in msg.parts:
                 if hasattr(part, 'text'):
                     parts_data.append({'text': part.text})
                 # Można dodać obsługę innych typów Part (np. obrazy), jeśli są potrzebne
            if parts_data: # Zapisuj tylko jeśli są jakieś części
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

# --- Funkcja send_message ---
def send_message(recipient_id, message_text):
    """Wysyła wiadomość tekstową do użytkownika przez Messenger API."""
    if not message_text:
        print("Pominięto wysyłanie pustej wiadomości.")
        return

    print(f"--- Próba wysłania odpowiedzi do {recipient_id} ---")
    params = {"access_token": PAGE_ACCESS_TOKEN}
    payload = {
        "recipient": {"id": recipient_id},
        "message": {"text": message_text},
        "messaging_type": "RESPONSE"
    }
    print(f"Wysyłane dane (payload): {json.dumps(payload, indent=2)}")

    try:
        r = requests.post(FACEBOOK_GRAPH_API_URL, params=params, json=payload)
        r.raise_for_status()
        response_json = r.json()
        print(f"Odpowiedź z Facebook API: {response_json}")
        print(f"--- Wiadomość wysłana pomyślnie do {recipient_id} ---")
    except requests.exceptions.RequestException as e:
        print(f"!!! BŁĄD podczas wysyłania wiadomości do Messengera: {e} !!!")
        if hasattr(e, 'response') and e.response is not None:
            try:
                print(f"Odpowiedź serwera FB (błąd): {e.response.json()}")
            except json.JSONDecodeError:
                print(f"Odpowiedź serwera FB (błąd, nie JSON): {e.response.text}")

# --- Funkcja do generowania odpowiedzi przez Gemini z Historią ---
def get_gemini_response_with_history(user_psid, current_user_message):
    """Generuje odpowiedź Gemini, używając historii zapisanej w pliku JSON, odpowiadając po polsku."""
    if not gemini_model:
        return "Przepraszam, mam problem z połączeniem z AI (model niezaładowany)."

    # 1. Odczytaj historię z pliku
    history = load_history(user_psid)

    # 2. Dodaj nową wiadomość użytkownika do bieżącej tury
    history.append(Content(role="user", parts=[Part.from_text(current_user_message)]))

    # 3. Przycinanie historii
    if len(history) > MAX_HISTORY_TURNS * 2:
        relevant_history = [msg for msg in history if msg.role in ("user", "model")]
        if len(relevant_history) > MAX_HISTORY_TURNS * 2:
            history = relevant_history[-(MAX_HISTORY_TURNS * 2):]
        print(f"Historia przycięta dla PSID {user_psid}")

    # 4. Przygotuj prompt (bez roli 'system', instrukcja dodawana do pierwszej wiadomości)
    prompt_content = history.copy() # Pracujemy na kopii do wysłania
    if len(prompt_content) == 1: # Jeśli to pierwsza wiadomość w historii tej tury
        prompt_content[0].parts[0].text = f"Odpowiedz na poniższe pytanie lub polecenie w języku polskim.\n\nPytanie: {current_user_message}"
        print("Dodano instrukcję językową do pierwszej wiadomości (modyfikacja obiektu w prompt_content).")

    print(f"--- Generowanie odpowiedzi Gemini ({MODEL_ID}) z historią dla PSID {user_psid} ---")
    print(f"Pełny prompt wysyłany do Gemini (content): {prompt_content}")

    try:
        # Konfiguracja generowania
        generation_config = GenerationConfig(
            max_output_tokens=2048,
            temperature=0.8,
            top_p=1.0,
            top_k=32
        )
        # Konfiguracja bezpieczeństwa
        safety_settings = {
            HarmCategory.HARM_CATEGORY_HARASSMENT: HarmBlockThreshold.BLOCK_MEDIUM_AND_ABOVE,
            HarmCategory.HARM_CATEGORY_HATE_SPEECH: HarmBlockThreshold.BLOCK_MEDIUM_AND_ABOVE,
            HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT: HarmBlockThreshold.BLOCK_MEDIUM_AND_ABOVE,
            HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT: HarmBlockThreshold.BLOCK_MEDIUM_AND_ABOVE,
        }

        # Wywołanie modelu
        response = gemini_model.generate_content(
            prompt_content, # Używamy zmodyfikowanego prompt_content
            generation_config=generation_config,
            safety_settings=safety_settings,
            stream=False,
        )

        print("\n--- Odpowiedź Gemini ---")
        if response.candidates and response.candidates[0].content.parts:
            generated_text = response.candidates[0].content.parts[0].text
            print(f"Wygenerowany tekst: {generated_text}")

            # 5. Dodaj odpowiedź bota do ORYGINALNEJ listy 'history' przed zapisem
            history.append(Content(role="model", parts=[Part.from_text(generated_text)]))

            # 6. Zapisz ZAKTUALIZOWANĄ (przyciętą wcześniej) historię do pliku
            save_history(user_psid, history)
            print(f"Zaktualizowano i zapisano historię dla PSID {user_psid}")

            return generated_text
        else:
            finish_reason = response.candidates[0].finish_reason if response.candidates else "UNKNOWN"
            safety_ratings = response.candidates[0].safety_ratings if response.candidates else []
            print(f"Odpowiedź Gemini była pusta lub zablokowana. Powód zakończenia: {finish_reason}, Oceny bezpieczeństwa: {safety_ratings}")
            print(f"Cała odpowiedź: {response}")
            # Zapisz historię *bez* odpowiedzi AI w tym przypadku
            save_history(user_psid, history)
            return "Hmm, nie mogłem wygenerować odpowiedzi lub została zablokowana."

    except Exception as e:
        print(f"!!! BŁĄD podczas generowania treści przez Gemini ({MODEL_ID}): {e} !!!")
        # Zapisz historię nawet przy błędzie AI
        save_history(user_psid, history)
        error_str = str(e).lower()
        if "publisher model" in error_str or "not found" in error_str or "is not available" in error_str or "permission denied" in error_str or "access token scope" in error_str or "content with system role is not supported" in error_str:
             print(f"   >>> Wystąpił błąd związany z modelem lub uprawnieniami: {e}")
             return f"Nie mogę użyć modułu AI '{MODEL_ID}'. Sprawdź konfigurację."
        return "Wystąpił błąd podczas myślenia."


# --- Obsługa Weryfikacji Webhooka (metoda GET) ---
@app.route('/webhook', methods=['GET'])
def webhook_verification():
    print("!!! FUNKCJA webhook_verification WYWOŁANA !!!")
    print("--- Otrzymano żądanie GET weryfikacyjne ---")
    hub_mode = request.args.get('hub.mode')
    hub_token = request.args.get('hub.verify_token')
    hub_challenge = request.args.get('hub.challenge')
    print(f"Mode: {hub_mode}, Token: {hub_token}, Challenge: {hub_challenge}")
    if hub_mode == 'subscribe' and hub_token == VERIFY_TOKEN:
        print("Weryfikacja GET udana!")
        return Response(hub_challenge, status=200, mimetype='text/plain')
    else:
        print("Weryfikacja GET nieudana.")
        return Response("Verification failed", status=403, mimetype='text/plain')

# --- Obsługa Odbioru Wiadomości (metoda POST) - Z Historią ---
@app.route('/webhook', methods=['POST'])
def webhook_handle():
    print("\n------------------------------------------")
    print("!!! FUNKCJA webhook_handle WYWOŁANA (POST) !!!")
    data = None
    try:
        data = request.get_json()
        # print("Odebrane dane JSON:") # Odkomentuj w razie potrzeby debugowania
        # print(json.dumps(data, indent=2))

        if data and data.get("object") == "page":
            for entry in data.get("entry", []):
                for messaging_event in entry.get("messaging", []):
                    if "sender" not in messaging_event:
                        print("Pominięto zdarzenie bez sender.id:", messaging_event)
                        continue

                    sender_id = messaging_event["sender"]["id"]

                    if messaging_event.get("message"):
                        if messaging_event["message"].get("is_echo"):
                            print(f"Pominięto echo wiadomości dla PSID {sender_id}")
                            continue

                        if "text" in messaging_event["message"]:
                            message_text = messaging_event["message"]["text"]
                            print(f"Odebrano wiadomość tekstową '{message_text}' od użytkownika {sender_id}")
                            response_text = get_gemini_response_with_history(sender_id, message_text)
                            send_message(sender_id, response_text)
                        else:
                            print(f"Odebrano wiadomość bez tekstu od użytkownika {sender_id}")
                            send_message(sender_id, "Przepraszam, rozumiem tylko wiadomości tekstowe.")

                    elif messaging_event.get("postback"):
                         payload = messaging_event["postback"]["payload"]
                         print(f"Odebrano postback z payload '{payload}' od użytkownika {sender_id}")
                         prompt_for_button = f"Użytkownik kliknął przycisk oznaczony jako: {payload}."
                         response_text = get_gemini_response_with_history(sender_id, prompt_for_button)
                         send_message(sender_id, response_text)
                    else:
                        print("Odebrano inne zdarzenie messaging:", messaging_event)
    except Exception as e:
        print(f"!!! BŁĄD podczas przetwarzania webhooka POST: {e} !!!")
        return Response("EVENT_PROCESSING_ERROR", status=200)

    return Response("EVENT_RECEIVED", status=200)

# --- Uruchomienie Serwera ---
if __name__ == '__main__':
    # Upewnij się, że katalog na historię istnieje przy starcie
    ensure_dir(HISTORY_DIR)
    port = 8080
    print(f"Uruchamianie serwera Flask z integracją Gemini (model: {MODEL_ID}, historia w plikach JSON) na porcie {port}...")
    app.run(host='0.0.0.0', port=port, debug=True)
