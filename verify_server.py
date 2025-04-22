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
PAGE_ACCESS_TOKEN = "EACNAHFzEhkUBO7nbFAtYvfPWbEht1B3chQqWLx76Ljg2ekdbJYoOrnpjATqhS0EZC8S0q8a49hEZBaZByZCaj5gr1z62dAaMgcZA1BqFOruHfFo86EWTbI3S9KL59oxFWfZCfCjwbQra9lY5of1JVnj2c9uFJDhIpWlXxLLao9Cv8JKssgs3rEDxIJBRr26HgUewZDZD" # Token dostępu do strony FB - UŻYTO PRZYKŁADOWEGO!
PROJECT_ID = "linear-booth-450221-k1"  # Twoje Google Cloud Project ID
LOCATION = "us-central1"  # Region GCP dla Vertex AI (np. us-central1, europe-west1)
# Użyj modelu, który na pewno działał u Ciebie (np. Flash)
MODEL_ID = "gemini-1.5-flash-preview-0514" # Model Gemini do użycia

# Adres URL API Facebook Graph do wysyłania wiadomości
FACEBOOK_GRAPH_API_URL = f"https://graph.facebook.com/v19.0/me/messages" # Użyj stabilnej wersji API

# --- Magazyn Historii (Słownik w Pamięci - TYLKO DO DEMO!) ---
conversation_history = {}
MAX_HISTORY_TURNS = 5 # Ile ostatnich par (user+model) wiadomości przechowywać (zmniejszone dla testów)

# --- Inicjalizacja Vertex AI ---
gemini_model = None # Zmienna globalna na model
try:
    print(f"Inicjalizowanie Vertex AI dla projektu: {PROJECT_ID}, lokalizacja: {LOCATION}")
    vertexai.init(project=PROJECT_ID, location=LOCATION)
    print("Inicjalizacja Vertex AI pomyślna.")
    print(f"Ładowanie modelu: {MODEL_ID}")
    # Dodatkowe ustawienia systemowe dla modelu (opcjonalne)
    # system_instruction_text = "Jesteś pomocnym asystentem o nazwie Bot, rozmawiającym po polsku."
    # model_config = {"system_instruction": system_instruction_text} # Nowsze SDK mogą wspierać to bezpośrednio
    gemini_model = GenerativeModel(
        MODEL_ID,
        # system_instruction=system_instruction_text # Sprawdź dokumentację SDK dla tej opcji
        )
    print("Model załadowany pomyślnie.")
except Exception as e:
    print(f"!!! KRYTYCZNY BŁĄD podczas inicjalizacji Vertex AI lub ładowania modelu: {e} !!!")
    print("   Sprawdź PROJECT_ID, LOCATION, uprawnienia konta usługi VM i czy API Vertex AI jest włączone.")
    print(f"   Upewnij się również, że model '{MODEL_ID}' jest poprawną i dostępną nazwą.")

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
    """Generuje odpowiedź Gemini, uwzględniając historię rozmowy."""
    if not gemini_model:
        return "Przepraszam, mam problem z połączeniem z AI."

    # 1. Pobierz historię dla użytkownika (lista obiektów Content)
    #    Używamy .copy(), aby nie modyfikować globalnego słownika bezpośrednio w tej turze
    history = conversation_history.get(user_psid, []).copy()

    # 2. Dodaj nową wiadomość użytkownika do lokalnej kopii historii
    history.append(Content(role="user", parts=[Part.from_text(current_user_message)]))

    # 3. Zarządzaj długością historii (przycinanie)
    #    Zachowaj tylko MAX_HISTORY_TURNS * 2 ostatnich wiadomości (para user+model)
    if len(history) > MAX_HISTORY_TURNS * 2:
        # Bierzemy pod uwagę tylko wiadomości użytkownika i modelu
        relevant_history = [msg for msg in history if msg.role in ("user", "model")]
        if len(relevant_history) > MAX_HISTORY_TURNS * 2:
            # Przytnij tylko historię user/model, zachowując najnowsze
            history = relevant_history[-(MAX_HISTORY_TURNS * 2):]
        print(f"Historia przycięta do (maks) {MAX_HISTORY_TURNS * 2} ostatnich tur dla PSID {user_psid}")

    # 4. Przygotuj prompt dla Gemini - użyjemy bezpośrednio listy `history`
    #    Instrukcję o języku polskim dodamy jako osobny komunikat systemowy (jeśli model to wspiera)
    #    lub model powinien sam wykryć język z kontekstu.
    prompt_content = history # Używamy bieżącej (potencjalnie przyciętej) historii

    # Opcjonalna instrukcja systemowa (sprawdź czy działa z Twoim modelem/SDK)
    system_instruction = Content(role="system", parts=[Part.from_text("Jesteś pomocnym asystentem. Odpowiadaj zawsze w języku polskim.")])
    # prompt_content = [system_instruction] + history # Odkomentuj, aby przetestować instrukcję systemową


    print(f"--- Generowanie odpowiedzi Gemini z historią dla PSID {user_psid} ---")
    print(f"Pełny prompt wysyłany do Gemini (content): {prompt_content}")

    try:
        generation_config = GenerationConfig(
            max_output_tokens=2048,
            temperature=0.8,
            top_p=1.0,
            top_k=32
        )
        safety_settings = {
            HarmCategory.HARM_CATEGORY_HARASSMENT: HarmBlockThreshold.BLOCK_MEDIUM_AND_ABOVE,
            HarmCategory.HARM_CATEGORY_HATE_SPEECH: HarmBlockThreshold.BLOCK_MEDIUM_AND_ABOVE,
            HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT: HarmBlockThreshold.BLOCK_MEDIUM_AND_ABOVE,
            HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT: HarmBlockThreshold.BLOCK_MEDIUM_AND_ABOVE,
        }

        # Wywołanie modelu z listą obiektów Content
        # Uwaga: Starsze modele Gemini mogły oczekiwać `history` jako osobnego parametru
        # Sprawdź dokumentację, jeśli poniższe nie działa:
        response = gemini_model.generate_content(
            prompt_content, # Przekazujemy całą przygotowaną listę Content
            generation_config=generation_config,
            safety_settings=safety_settings,
            stream=False,
        )

        print("\n--- Odpowiedź Gemini ---")
        if response.candidates and response.candidates[0].content.parts:
            generated_text = response.candidates[0].content.parts[0].text
            print(f"Wygenerowany tekst: {generated_text}")

            # 5. Dodaj odpowiedź bota do GŁÓWNEJ historii (przechowywanej globalnie)
            #    Najpierw pobierz aktualną globalną historię, na wypadek gdyby zmieniła się w międzyczasie (mało prawdopodobne w Flasku bez async)
            global_history = conversation_history.get(user_psid, []).copy()
            global_history.append(Content(role="user", parts=[Part.from_text(current_user_message)])) # Dodajemy wiadomość usera
            global_history.append(Content(role="model", parts=[Part.from_text(generated_text)]))     # Dodajemy odpowiedź AI

            # Przytnij globalną historię przed zapisaniem
            if len(global_history) > MAX_HISTORY_TURNS * 2:
                 relevant_global_history = [msg for msg in global_history if msg.role in ("user", "model")]
                 if len(relevant_global_history) > MAX_HISTORY_TURNS * 2:
                     global_history = relevant_global_history[-(MAX_HISTORY_TURNS * 2):]

            # Zapisz zaktualizowaną globalną historię
            conversation_history[user_psid] = global_history
            print(f"Zaktualizowano globalną historię dla PSID {user_psid}, długość: {len(conversation_history[user_psid])}")

            return generated_text
        else:
            # Obsługa sytuacji, gdy odpowiedź jest pusta lub zablokowana
            finish_reason = response.candidates[0].finish_reason if response.candidates else "UNKNOWN"
            safety_ratings = response.candidates[0].safety_ratings if response.candidates else []
            print(f"Odpowiedź Gemini była pusta lub zablokowana. Powód zakończenia: {finish_reason}, Oceny bezpieczeństwa: {safety_ratings}")
            print(f"Cała odpowiedź: {response}")
            return "Hmm, nie mogłem wygenerować odpowiedzi lub została zablokowana."

    except Exception as e:
        print(f"!!! BŁĄD podczas generowania treści przez Gemini: {e} !!!")
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
                    # Sprawdzamy czy zdarzenie ma nadawcę - niektóre systemowe mogą nie mieć
                    if "sender" not in messaging_event:
                        print("Pominięto zdarzenie bez sender.id:", messaging_event)
                        continue # Przejdź do następnego zdarzenia

                    sender_id = messaging_event["sender"]["id"] # PSID użytkownika

                    if messaging_event.get("message"): # Czy to wiadomość?
                        # Ignoruj echa wiadomości wysłanych przez bota
                        if messaging_event["message"].get("is_echo"):
                            print(f"Pominięto echo wiadomości dla PSID {sender_id}")
                            continue

                        if "text" in messaging_event["message"]:
                            message_text = messaging_event["message"]["text"]
                            print(f"Odebrano wiadomość tekstową '{message_text}' od użytkownika {sender_id}")
                            # Wywołanie Gemini z uwzględnieniem historii
                            response_text = get_gemini_response_with_history(sender_id, message_text)
                            send_message(sender_id, response_text) # Wyślij odpowiedź
                        else:
                            print(f"Odebrano wiadomość bez tekstu od użytkownika {sender_id}")
                            send_message(sender_id, "Przepraszam, rozumiem tylko wiadomości tekstowe.")

                    elif messaging_event.get("postback"): # Czy to kliknięcie przycisku?
                         payload = messaging_event["postback"]["payload"]
                         print(f"Odebrano postback z payload '{payload}' od użytkownika {sender_id}")
                         # Można też przekazać historię do obsługi postbacków, ale na razie prosty prompt
                         prompt_for_button = f"Użytkownik kliknął przycisk oznaczony jako: {payload}. Odpowiedz na to krótko po polsku."
                         # Użyjemy funkcji z historią, ale prompt nie zawiera historii
                         # Lepszym rozwiązaniem byłoby dodanie informacji o kliknięciu do historii
                         response_text = get_gemini_response_with_history(sender_id, prompt_for_button)
                         send_message(sender_id, response_text)
                    else:
                        # Loguje inne, nieobsługiwane typy zdarzeń
                        print("Odebrano inne zdarzenie messaging:", messaging_event)
    except Exception as e:
        print(f"!!! BŁĄD podczas przetwarzania webhooka POST: {e} !!!")
        # Ważne: Odpowiedz 200 OK, aby Facebook nie próbował wysłać ponownie
        return Response("EVENT_PROCESSING_ERROR", status=200)

    # Zawsze odpowiada 200 OK na końcu, potwierdzając odbiór
    # print("Odpowiadam 200 OK (koniec przetwarzania).") # Można odkomentować
    # print("------------------------------------------\n")
    return Response("EVENT_RECEIVED", status=200)

# --- Uruchomienie Serwera ---
if __name__ == '__main__':
    port = 8080
    print(f"Uruchamianie serwera Flask z integracją Gemini (z historią w pamięci) na porcie {port}...")
    # Wyłącz debug=True w środowisku produkcyjnym!
    app.run(host='0.0.0.0', port=port, debug=True)
