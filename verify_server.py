# -*- coding: utf-8 -*-

from flask import Flask, request, Response
import os
import json
import requests # Potrzebna biblioteka do wysyłania żądań HTTP

app = Flask(__name__)

# --- Token Weryfikacyjny ---
# Ten sam token, który wpisujesz w polu "Verify token" na Facebooku
VERIFY_TOKEN = "KOLAGEN"

# --- Page Access Token ---
# !!! UŻYTO PRZYKŁADOWEGO TOKENU PODANEGO PRZEZ UŻYTKOWNIKA !!!
# W prawdziwej aplikacji wstaw tutaj swój rzeczywisty token!
# Pamiętaj o bezpieczeństwie - nie umieszczaj prawdziwych tokenów w kodzie w produkcji.
PAGE_ACCESS_TOKEN = "EACNAHFzEhkUBO77CfzD3OSOHMC5aLZAZA4X6bbBF6lWXENIrci2mwZCj24q8oJb20fDgU4YXsZC9IMGCZBpc78FTLkrZADFr2WfHTZBDnVavxqQi8ZBudc4iqELuDiLBSDfQImtQIrdwvJtfj4NvtSZCq0kpkMFyh0trKjTgl3zMuD45lDmpakqhxXfaPRD0v0grgBAZDZD" # <--- PRZYKŁADOWY TOKEN

# Adres URL API Facebook Graph do wysyłania wiadomości
FACEBOOK_GRAPH_API_URL = "https://graph.facebook.com/v21.0/me/messages" # Używamy najnowszej dostępnej wersji API


# --- Funkcja do wysyłania wiadomości ---
def send_message(recipient_id, message_text):
    """Wysyła wiadomość tekstową do użytkownika przez Messenger API."""
    print(f"--- Próba wysłania odpowiedzi do {recipient_id} ---")
    params = {
        "access_token": PAGE_ACCESS_TOKEN
    }
    # Przygotowujemy słownik Pythona z danymi
    payload = {
        "recipient": {
            "id": recipient_id
        },
        "message": {
            "text": message_text
        }
    }
    print(f"Wysyłane dane (payload): {payload}") # Logujemy, co wysyłamy

    try:
        # Używamy parametru 'json' zamiast 'data' i przekazujemy słownik
        r = requests.post(FACEBOOK_GRAPH_API_URL, params=params, json=payload)
        r.raise_for_status() # Sprawdza, czy wystąpił błąd HTTP (np. 400, 500)
        response_json = r.json()
        print(f"Odpowiedź z Facebook API: {response_json}")
        print(f"--- Wiadomość wysłana pomyślnie do {recipient_id} ---")
    except requests.exceptions.RequestException as e:
        print(f"!!! BŁĄD podczas wysyłania wiadomości: {e} !!!")
        # Próbujemy zalogować odpowiedź błędu, jeśli istnieje
        if hasattr(e, 'response') and e.response is not None:
            try:
                print(f"Odpowiedź serwera (błąd): {e.response.json()}")
            except json.JSONDecodeError:
                print(f"Odpowiedź serwera (błąd, nie JSON): {e.response.text}")


# --- Obsługa Weryfikacji Webhooka (metoda GET) ---
@app.route('/webhook', methods=['GET'])
def webhook_verification():
    # Funkcja bez zmian - obsługuje weryfikację przy konfiguracji webhooka
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


# --- Obsługa Odbioru Wiadomości (metoda POST) ---
@app.route('/webhook', methods=['POST'])
def webhook_handle():
    # Funkcja obsługuje przychodzące wiadomości i zdarzenia
    print("\n------------------------------------------")
    print("!!! FUNKCJA webhook_handle WYWOŁANA (POST) !!!")
    data = None
    try:
        data = request.get_json()
        print("Odebrane dane JSON:")
        print(json.dumps(data, indent=2)) # Loguje całe odebrane dane

        # Przetwarzanie odebranych danych
        if data and data.get("object") == "page":
            for entry in data.get("entry", []):
                for messaging_event in entry.get("messaging", []):

                    # Sprawdza, czy to wiadomość od użytkownika
                    if messaging_event.get("message"):
                        sender_id = messaging_event["sender"]["id"] # ID nadawcy (PSID)

                        # Sprawdza, czy wiadomość zawiera tekst
                        if "text" in messaging_event["message"]:
                            message_text = messaging_event["message"]["text"]
                            print(f"Odebrano wiadomość tekstową '{message_text}' od użytkownika {sender_id}")

                            # *** Logika odpowiedzi - Echo ***
                            response_text = f"Otrzymałem: {message_text}" # Przygotowuje odpowiedź
                            send_message(sender_id, response_text) # Wysyła odpowiedź

                        else:
                            # Obsługa wiadomości bez tekstu (np. załącznik)
                            print(f"Odebrano wiadomość bez tekstu od użytkownika {sender_id}")
                            send_message(sender_id, "Przepraszam, rozumiem tylko wiadomości tekstowe.")

                    # Obsługa kliknięcia przycisku (postback)
                    elif messaging_event.get("postback"):
                         sender_id = messaging_event["sender"]["id"]
                         payload = messaging_event["postback"]["payload"]
                         print(f"Odebrano postback z payload '{payload}' od użytkownika {sender_id}")
                         send_message(sender_id, f"Kliknąłeś przycisk! Payload: {payload}")

                    else:
                        # Loguje inne, nieobsługiwane typy zdarzeń
                        print("Odebrano inne zdarzenie messaging:", messaging_event)

    except Exception as e:
        print(f"!!! BŁĄD podczas przetwarzania webhooka POST: {e} !!!")
        # Odpowiada 200 OK mimo błędu, aby Facebook nie próbował ponownie
        return Response("EVENT_PROCESSING_ERROR", status=200)

    # Zawsze odpowiada 200 OK na końcu, potwierdzając odbiór
    print("Odpowiadam 200 OK (koniec przetwarzania).")
    print("------------------------------------------\n")
    return Response("EVENT_RECEIVED", status=200)


# --- Uruchomienie Serwera ---
if __name__ == '__main__':
    port = 8080
    print(f"Uruchamianie serwera Flask na porcie {port}...")
    # Wyłącz debug=True w środowisku produkcyjnym!
    app.run(host='0.0.0.0', port=port, debug=True)
  
