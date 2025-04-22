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

app = Flask(__name__)

# --- Konfiguracja ---
VERIFY_TOKEN = "KOLAGEN" # Twój token weryfikacyjny FB

# !!! WAŻNE: Zastąp poniższe wartości swoimi !!!
PAGE_ACCESS_TOKEN = "EACNAHFzEhkUBO7nbFAtYvfPWbEht1B3chQqWLx76Ljg2ekdbJYoOrnpjATqhS0EZC8S0q8a49hEZBaZByZCaj5gr1z62dAaMgcZA1BqFOruHfFo86EWTbI3S9KL59oxFWfZCfCjwbQra9lY5of1JVnj2c9uFJDhIpWlXxLLao9Cv8JKssgs3rEDxIJBRr26HgUewZDZD"
PROJECT_ID = "linear-booth-450221-k1"  # Twoje Google Cloud Project ID
LOCATION = "us-central1"  # Region GCP dla Vertex AI (sprawdź, czy ten działa)
MODEL_ID = "gemini-2.0-flash-001" # Używamy modelu wskazanego przez użytkownika

# Adres URL API Facebook Graph do wysyłania wiadomości
FACEBOOK_GRAPH_API_URL = f"https://graph.facebook.com/v19.0/me/messages" # Użyj stabilnej wersji API

# --- Magazyn Historii (Słownik w Pamięci - TYLKO DO DEMO!) ---
conversation_history = {}
MAX_HISTORY_TURNS = 5 # Ile ostatnich par (user+model) wiadomości przechowywać

# --- Inicjalizacja Vertex AI ---
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
    print(f"    Sprawdź, czy model '{MODEL_ID}' istnieje i jest dostępny w regionie '{LOCATION}' dla projektu '{PROJECT_ID}'.")
    print("    Upewnij się, że masz odpowiednie uprawnienia IAM i Access Scopes dla VM.")

# --- Funkcja do wysyłania wiadomości do Messengera ---
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
        "messaging_type": "RESPONSE" # Ważne dla zgodności z polityką FB
    }
    print(f"Wysyłane dane (payload): {json.dumps(payload, indent=2)}")

    try:
        r = requests.post(FACEBOOK_GRAPH_API_URL, params=params, json=payload)
        r.raise_for_status() # Sprawdza błędy HTTP
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

# --- Funkcja do generowania odpowiedzi przez Gemini z Historią i po Polsku ---
def get_gemini_response_with_history(user_psid, current_user_message):
    """Generuje odpowiedź Gemini, uwzględniając historię rozmowy, odpowiadając po polsku."""
    if not gemini_model:
        print("!!! BŁĄD: Model Gemini nie został załadowany. Nie można wygenerować odpowiedzi. !!!")
        return "Przepraszam, mam problem z połączeniem z AI (model niezaładowany)."

    # Pobieramy historię, używając .copy(), aby uniknąć modyfikacji oryginału przed zapisem
    history = conversation_history.get(user_psid, []).copy()

    # Dodajemy nową wiadomość użytkownika do bieżącej tury
    history.append(Content(role="user", parts=[Part.from_text(current_user_message)]))

    # Przycinamy historię, jeśli jest zbyt długa
    if len(history) > MAX_HISTORY_TURNS * 2:
        relevant_history = [msg for msg in history if msg.role in ("user", "model")]
        if len(relevant_history) > MAX_HISTORY_TURNS * 2:
            history = relevant_history[-(MAX_HISTORY_TURNS * 2):]
        print(f"Historia przycięta dla PSID {user_psid}")

    # Tworzymy prompt z instrukcją systemową i historią
    # Instrukcja systemowa może nie być wspierana przez wszystkie modele/wersje SDK w ten sposób
    prompt_content = [Content(role="system", parts=[Part.from_text("Jesteś pomocnym asystentem. Odpowiadaj zawsze w języku polskim.")])] + history

    print(f"--- Generowanie odpowiedzi Gemini ({MODEL_ID}) z historią dla PSID {user_psid} ---")
    print(f"Pełny prompt wysyłany do Gemini (content): {prompt_content}")

    try:
        # Konfiguracja generowania
        generation_config = GenerationConfig(
            max_output_tokens=2048,
            temperature=0.8, # Można eksperymentować
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
            prompt_content,
            generation_config=generation_config,
            safety_settings=safety_settings,
            stream=False,
        )

        print("\n--- Odpowiedź Gemini ---")
        if response.candidates and response.candidates[0].content.parts:
            generated_text = response.candidates[0].content.parts[0].text
            print(f"Wygenerowany tekst: {generated_text}")

            # Zapisz zaktualizowaną historię (wiadomość usera + odpowiedź AI) do globalnego słownika
            # Używamy `history`, które już zawiera wiadomość usera z tej tury
            history.append(Content(role="model", parts=[Part.from_text(generated_text)]))
            # Upewniamy się, że zapisywana historia nie przekracza limitu
            if len(history) > MAX_HISTORY_TURNS * 2:
                 relevant_history = [msg for msg in history if msg.role in ("user", "model")]
                 if len(relevant_history) > MAX_HISTORY_TURNS * 2:
                     history = relevant_history[-(MAX_HISTORY_TURNS * 2):]
            conversation_history[user_psid] = history # Zapisujemy ostateczną historię
            print(f"Zaktualizowano globalną historię dla PSID {user_psid}, długość: {len(conversation_history[user_psid])}")

            return generated_text
        else:
            # Logowanie problemu z odpowiedzią
            finish_reason = response.candidates[0].finish_reason if response.candidates else "UNKNOWN"
            safety_ratings = response.candidates[0].safety_ratings if response.candidates else []
            print(f"Odpowiedź Gemini była pusta lub zablokowana. Powód zakończenia: {finish_reason}, Oceny bezpieczeństwa: {safety_ratings}")
            print(f"Cała odpowiedź: {response}")
            return "Hmm, nie mogłem wygenerować odpowiedzi lub została zablokowana."

    except Exception as e:
        print(f"!!! BŁĄD podczas generowania treści przez Gemini ({MODEL_ID}): {e} !!!")
        # Lepsze logowanie błędu dostępu do modelu
        error_str = str(e).lower()
        if "publisher model" in error_str or "not found" in error_str or "is not available" in error_str or "permission denied" in error_str or "access token scope" in error_str:
             print(f"   >>> Wygląda na to, że występuje problem z dostępem do modelu '{MODEL_ID}' w regionie '{LOCATION}'.")
             print("   >>> Sprawdź: 1) Poprawność nazwy modelu. 2) Dostępność w regionie. 3) Uprawnienia IAM ('Vertex AI User') i Access Scopes ('cloud-platform') dla konta usługi VM. 4) Czy API Vertex AI jest włączone.")
             return f"Nie mogę użyć modułu AI '{MODEL_ID}'. Sprawdź konfigurację."
        return "Wystąpił błąd podczas myślenia. Spróbuj zadać pytanie inaczej."


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
        # print("Odebrane dane JSON:") # Można odkomentować do debugowania
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
                         # Wywołujemy z historią, ale przekazujemy informację o kliknięciu jako 'wiadomość'
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
    port = 8080
    print(f"Uruchamianie serwera Flask z integracją Gemini (model: {MODEL_ID}) na porcie {port}...")
    app.run(host='0.0.0.0', port=port, debug=True)
