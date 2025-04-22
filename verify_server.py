# -*- coding: utf-8 -*-

from flask import Flask, request, Response
import os
import json
import requests
import vertexai
from vertexai.generative_models import GenerativeModel, Part, Content, GenerationConfig, SafetySetting, HarmCategory, HarmBlockThreshold

app = Flask(__name__)

# --- Konfiguracja (jak poprzednio) ---
VERIFY_TOKEN = "KOLAGEN"
PAGE_ACCESS_TOKEN = "TWOJ_PAGE_ACCESS_TOKEN_WKLEJ_TUTAJ"
PROJECT_ID = "linear-booth-450221-k1"
LOCATION = "us-central1" # Lub inny działający region
MODEL_ID = "gemini-1.5-flash-preview-0514" # Użyj działającego modelu
FACEBOOK_GRAPH_API_URL = f"https://graph.facebook.com/v19.0/me/messages"

# --- Magazyn Historii (Słownik w Pamięci - TYLKO DO DEMO!) ---
conversation_history = {}
MAX_HISTORY_TURNS = 10 # Ile ostatnich par (user+model) wiadomości przechowywać

# --- Inicjalizacja Vertex AI (jak poprzednio) ---
# ... (kod inicjalizacji vertexai.init i GenerativeModel) ...
# Upewnij się, że gemini_model jest poprawnie zainicjowany lub None
gemini_model = None # Zmienna globalna na model
try:
    print(f"Inicjalizowanie Vertex AI dla projektu: {PROJECT_ID}, lokalizacja: {LOCATION}")
    vertexai.init(project=PROJECT_ID, location=LOCATION)
    print("Inicjalizacja Vertex AI pomyślna.")
    print(f"Ładowanie modelu: {MODEL_ID}")
    gemini_model = GenerativeModel(MODEL_ID)
    print("Model załadowany pomyślnie.")
except Exception as e:
    print(f"!!! KRYTYCZNY BŁĄD podczas inicjalizacji Vertex AI lub ładowania modelu: {e} !!!")

# --- Funkcja send_message (bez zmian) ---
# ... (kod funkcji send_message) ...
def send_message(recipient_id, message_text):
    # ... (implementacja jak poprzednio) ...
    pass # Placeholder - wklej tu poprzednią implementację

# --- Funkcja do generowania odpowiedzi przez Gemini z Historią ---
from vertexai.generative_models import GenerativeModel, Part, Content, GenerationConfig, SafetySetting, HarmCategory, HarmBlockThreshold

# --- Obsługa Weryfikacji Webhooka (metoda GET) ---
# (Bez zmian)
@app.route('/webhook', methods=['GET'])
def webhook_verification():
    # ... (kod jak poprzednio) ...
    pass # Placeholder

# --- Obsługa Odbioru Wiadomości (metoda POST) - Z Historią ---
@app.route('/webhook', methods=['POST'])
def webhook_handle():
    print("\n------------------------------------------")
    print("!!! FUNKCJA webhook_handle WYWOŁANA (POST) !!!")
    data = None
    try:
        data = request.get_json()
        # print("Odebrane dane JSON:") # Można odkomentować do debugowania
        # print(json.dumps(data, indent=2))

        if data and data.get("object") == "page":
            for entry in data.get("entry", []):
                for messaging_event in entry.get("messaging", []):
                    sender_id = messaging_event["sender"]["id"]

                    if messaging_event.get("message"):
                        if "text" in messaging_event["message"]:
                            message_text = messaging_event["message"]["text"]
                            print(f"Odebrano wiadomość tekstową '{message_text}' od użytkownika {sender_id}")

                            # *** Wywołanie Gemini z uwzględnieniem historii ***
                            response_text = get_gemini_response_with_history(sender_id, message_text)

                            send_message(sender_id, response_text) # Wyślij odpowiedź
                        else:
                            print(f"Odebrano wiadomość bez tekstu od użytkownika {sender_id}")
                            send_message(sender_id, "Przepraszam, rozumiem tylko wiadomości tekstowe.")
                    elif messaging_event.get("postback"):
                         payload = messaging_event["postback"]["payload"]
                         print(f"Odebrano postback z payload '{payload}' od użytkownika {sender_id}")
                         # *** Wywołanie Gemini dla postbacka (też można dodać historię) ***
                         # Dla uproszczenia, na razie użyjemy prostego promptu
                         prompt_for_button = f"Użytkownik kliknął przycisk oznaczony jako: {payload}. Odpowiedz na to krótko po polsku."
                         response_text = get_gemini_response(prompt_for_button) # Używamy starej funkcji bez historii dla postback
                         send_message(sender_id, response_text)
                    else:
                        print("Odebrano inne zdarzenie messaging:", messaging_event)
    except Exception as e:
        print(f"!!! BŁĄD podczas przetwarzania webhooka POST: {e} !!!")
        return Response("EVENT_PROCESSING_ERROR", status=200)

    print("Odpowiadam 200 OK (koniec przetwarzania).")
    print("------------------------------------------\n")
    return Response("EVENT_RECEIVED", status=200)

# --- Uruchomienie Serwera ---
if __name__ == '__main__':
    # Uzupełnij brakujące fragmenty (inicjalizacja AI, send_message, webhook_verification)
    port = 8080
    print(f"Uruchamianie serwera Flask z integracją Gemini (z historią w pamięci) na porcie {port}...")
    app.run(host='0.0.0.0', port=port, debug=True)
