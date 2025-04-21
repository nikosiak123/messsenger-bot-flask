# -*- coding: utf-8 -*-

from flask import Flask, request, Response
import os
import json
import requests # Do wysyłania wiadomości do FB API
import vertexai # Do komunikacji z Vertex AI
from vertexai.generative_models import GenerativeModel # Do użycia modeli Gemini

app = Flask(__name__)

# --- Konfiguracja ---
VERIFY_TOKEN = "KOLAGEN" # Twój token weryfikacyjny FB

# !!! WAŻNE: Zastąp poniższe wartości swoimi !!!
PAGE_ACCESS_TOKEN = "EACNAHFzEhkUBO7nbFAtYvfPWbEht1B3chQqWLx76Ljg2ekdbJYoOrnpjATqhS0EZC8S0q8a49hEZBaZByZCaj5gr1z62dAaMgcZA1BqFOruHfFo86EWTbI3S9KL59oxFWfZCfCjwbQra9lY5of1JVnj2c9uFJDhIpWlXxLLao9Cv8JKssgs3rEDxIJBRr26HgUewZDZD" # Token dostępu do strony FB
PROJECT_ID = "linear-booth-450221-k1"  # Twoje Google Cloud Project ID
LOCATION = "us-central1"  # Region GCP dla Vertex AI (np. us-central1, europe-west1)
MODEL_ID = "gemini-2.0-flash-001" # Używamy modelu wskazanego przez użytkownika

# Adres URL API Facebook Graph do wysyłania wiadomości
FACEBOOK_GRAPH_API_URL = f"https://graph.facebook.com/v19.0/me/messages" # Użyj stabilnej wersji API

# --- Inicjalizacja Vertex AI ---
try:
    print(f"Inicjalizowanie Vertex AI dla projektu: {PROJECT_ID}, lokalizacja: {LOCATION}")
    vertexai.init(project=PROJECT_ID, location=LOCATION)
    print("Inicjalizacja Vertex AI pomyślna.")
    # Załadowanie modelu Gemini
    print(f"Ładowanie modelu: {MODEL_ID}")
    gemini_model = GenerativeModel(MODEL_ID)
    print("Model załadowany pomyślnie.")
except Exception as e:
    print(f"!!! KRYTYCZNY BŁĄD podczas inicjalizacji Vertex AI lub ładowania modelu: {e} !!!")
    print("   Sprawdź PROJECT_ID, LOCATION, uprawnienia konta usługi VM i czy API Vertex AI jest włączone.")
    print(f"   Upewnij się również, że model '{MODEL_ID}' jest poprawną i dostępną nazwą.")
    gemini_model = None # Ustawiamy model na None, aby uniknąć błędów później

# --- Funkcja do wysyłania wiadomości do Messengera ---
def send_message(recipient_id, message_text):
    """Wysyła wiadomość tekstową do użytkownika przez Messenger API."""
    if not message_text: # Nie wysyłaj pustych wiadomości
        print("Pominięto wysyłanie pustej wiadomości.")
        return

    print(f"--- Próba wysłania odpowiedzi do {recipient_id} ---")
    params = {"access_token": PAGE_ACCESS_TOKEN}
    payload = {
        "recipient": {"id": recipient_id},
        "message": {"text": message_text},
        "messaging_type": "RESPONSE" # Ważne dla zgodności z polityką FB
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

# --- Funkcja do generowania odpowiedzi przez Gemini ---
def get_gemini_response(prompt_text):
    """Generuje odpowiedź tekstową używając załadowanego modelu Gemini."""
    if not gemini_model:
        print("!!! BŁĄD: Model Gemini nie został załadowany. Nie można wygenerować odpowiedzi. !!!")
        return "Przepraszam, mam chwilowy problem techniczny z moim AI."

    print(f"--- Generowanie odpowiedzi Gemini dla promptu: '{prompt_text}' ---")
    try:
        # Konfiguracja generowania - można dostosować
        generation_config = {
            "max_output_tokens": 2048,
            "temperature": 0.9,
            "top_p": 1,
            "top_k": 32
        }
        safety_settings = {} # Puste dla uproszczenia

        # Wywołanie modelu
        response = gemini_model.generate_content(
            prompt_text,
            generation_config=generation_config,
            safety_settings=safety_settings,
            stream=False,
        )

        print("\n--- Odpowiedź Gemini ---")
        # Sprawdzanie odpowiedzi
        if response.candidates and hasattr(response.candidates[0].content, 'parts') and response.candidates[0].content.parts:
             generated_text = response.candidates[0].content.parts[0].text
             print(f"Wygenerowany tekst: {generated_text}")
             return generated_text
        else:
             print("Odpowiedź Gemini była pusta lub zablokowana.")
             print(f"Cała odpowiedź: {response}")
             return "Hmm, nie wiem co odpowiedzieć."

    except Exception as e:
        print(f"!!! BŁĄD podczas generowania treści przez Gemini: {e} !!!")
        # Sprawdźmy, czy błąd to znany problem z dostępem do modelu
        if "Publisher Model" in str(e):
             print("   >>> Wygląda na to, że nadal występuje problem z dostępem do modelu.")
             print("   >>> Sprawdź, czy model jest dostępny w regionie i czy Twój projekt ma uprawnienia.")
             return "Niestety, nie mogę teraz uzyskać dostępu do mojego modułu AI dla tego zapytania."
        return "Wystąpił błąd podczas myślenia. Spróbuj zadać pytanie inaczej."


# --- Obsługa Weryfikacji Webhooka (metoda GET) ---
# (Bez zmian)
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

# --- Obsługa Odbioru Wiadomości (metoda POST) - Z Gemini ---
# (Bez zmian w logice, użyje nowego MODEL_ID w get_gemini_response)
@app.route('/webhook', methods=['POST'])
def webhook_handle():
    print("\n------------------------------------------")
    print("!!! FUNKCJA webhook_handle WYWOŁANA (POST) !!!")
    data = None
    try:
        data = request.get_json()
        print("Odebrane dane JSON:")
        print(json.dumps(data, indent=2))

        if data and data.get("object") == "page":
            for entry in data.get("entry", []):
                for messaging_event in entry.get("messaging", []):
                    sender_id = messaging_event["sender"]["id"]

                    if messaging_event.get("message"):
                        if "text" in messaging_event["message"]:
                            message_text = messaging_event["message"]["text"]
                            print(f"Odebrano wiadomość tekstową '{message_text}' od użytkownika {sender_id}")
                            response_text = get_gemini_response(message_text)
                            send_message(sender_id, response_text)
                        else:
                            print(f"Odebrano wiadomość bez tekstu od użytkownika {sender_id}")
                            send_message(sender_id, "Przepraszam, na razie potrafię czytać tylko tekst.")
                    elif messaging_event.get("postback"):
                         payload = messaging_event["postback"]["payload"]
                         print(f"Odebrano postback z payload '{payload}' od użytkownika {sender_id}")
                         prompt_for_button = f"Użytkownik kliknął przycisk oznaczony jako: {payload}. Co powinienem odpowiedzieć?"
                         response_text = get_gemini_response(prompt_for_button)
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
    port = 8080
    print(f"Uruchamianie serwera Flask z integracją Gemini na porcie {port}...")
    app.run(host='0.0.0.0', port=port, debug=True)
