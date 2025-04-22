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
LOCATION = "us-central1"  # Region GCP dla Vertex AI
MODEL_ID = "gemini-2.0-flash-001" # Model wskazany przez użytkownika

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
    print(f"    Sprawdź, czy model '{MODEL_ID}' istnieje i jest dostępny w regionie '{LOCATION}' dla projektu '{PROJECT_ID}'.")
    print("    Upewnij się, że masz odpowiednie uprawnienia IAM i Access Scopes dla VM.")


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
        print("Wiadomość mieści się w limicie, wysyłanie jako całość.")
        _send_single_message(recipient_id, full_message_text)
    else:
        chunks = []
        remaining_text = full_message_text
        print(f"Wiadomość za długa (limit: {MESSAGE_CHAR_LIMIT}). Dzielenie na fragmenty...")

        while remaining_text:
            if len(remaining_text) <= MESSAGE_CHAR_LIMIT:
                chunks.append(remaining_text)
                break # Koniec
            else:
                split_index = -1
                temp_index = remaining_text.rfind('\n\n', 0, MESSAGE_CHAR_LIMIT)
                if temp_index != -1: split_index = temp_index + 2
                else:
                    temp_index = remaining_text.rfind('\n', 0, MESSAGE_CHAR_LIMIT)
                    if temp_index != -1: split_index = temp_index + 1
                    else:
                        temp_index = remaining_text.rfind(' ', 0, MESSAGE_CHAR_LIMIT)
                        if temp_index != -1: split_index = temp_index + 1
                        else: split_index = MESSAGE_CHAR_LIMIT

                chunk = remaining_text[:split_index]
                chunks.append(chunk)
                remaining_text = remaining_text[split_index:]

        num_chunks = len(chunks)
        print(f"Podzielono wiadomość na {num_chunks} fragmentów.")

        for i, chunk in enumerate(chunks):
            chunk_to_send = chunk
            print(f"Wysyłanie fragmentu {i+1}/{num_chunks} (długość: {len(chunk_to_send)})...")
            if not _send_single_message(recipient_id, chunk_to_send):
                print(f"!!! Anulowano wysyłanie pozostałych fragmentów z powodu błędu przy fragmencie {i+1} !!!")
                break
            if i < num_chunks - 1:
                print(f"Oczekiwanie {MESSAGE_DELAY_SECONDS}s przed następnym fragmentem...")
                time.sleep(MESSAGE_DELAY_SECONDS)

        print(f"--- Zakończono wysyłanie {num_chunks} fragmentów wiadomości do {recipient_id} ---")


# --- Funkcja do generowania odpowiedzi przez Gemini z Historią i Instrukcją ---
def get_gemini_response_with_history(user_psid, current_user_message):
    """Generuje odpowiedź Gemini, używając historii zapisanej w pliku JSON, odpowiadając po polsku."""
    if not gemini_model:
        return "Przepraszam, mam problem z połączeniem z AI (model niezaładowany)."

    history = load_history(user_psid)
    user_content = Content(role="user", parts=[Part.from_text(current_user_message)])
    current_turn_history = history + [user_content]

    history_to_send = current_turn_history
    if len(current_turn_history) > MAX_HISTORY_TURNS * 2:
        relevant_history = [msg for msg in current_turn_history if msg.role in ("user", "model")]
        if len(relevant_history) > MAX_HISTORY_TURNS * 2:
            history_to_send = relevant_history[-(MAX_HISTORY_TURNS * 2):]
            print(f"Historia przycięta dla PSID {user_psid}")

    system_instruction_text = """Jesteś profesjonalnym i uprzejmym asystentem obsługi klienta reprezentującym centrum specjalizujące się w wysokiej jakości korepetycjach online z matematyki, języka angielskiego i języka polskiego. Obsługujemy uczniów od 4 klasy szkoły podstawowej aż do klasy maturalnej, oferując zajęcia zarówno na poziomie podstawowym, jak i rozszerzonym.

Twoim głównym celem jest aktywne zachęcanie klientów (uczniów lub ich rodziców) do skorzystania z naszych usług i umówienia się na pierwszą lekcję. Prezentuj ofertę rzeczowo, podkreślając korzyści płynące z nauki z naszymi doświadczonymi korepetytorami online (np. lepsze wyniki, zdana matura, większa pewność siebie).

Przebieg rozmowy:
1. Najpierw ustal, jakiego przedmiotu, dla której klasy i na jakim poziomie (podstawowy/rozszerzony) klient potrzebuje korepetycji.
2. Następnie przedstaw odpowiednią cenę za 60-minutową lekcję:
    * Klasy 4-8 SP: 60 zł
    * Klasy 1-3 LO/Technikum (podstawa): 65 zł
    * Klasy 1-3 LO/Technikum (rozszerzenie): 70 zł
    * Klasa 4 LO/Technikum (podstawa): 70 zł
    * Klasa 4 LO/Technikum (rozszerzenie): 75 zł
3. Aktywnie zachęcaj do umówienia się na pierwszą lekcję, aby uczeń mógł poznać korepetytora i sprawdzić naszą skuteczną formę zajęć online. Wspomnij przy tym krótko, że jest to lekcja zgodna z cennikiem.
4. Informację o tym, że zajęcia odbywają się wyłącznie online przez platformę Teams (bez konieczności pobierania, przez link), podaj w dalszej części rozmowy, gdy klient wykaże już zainteresowanie.
5. Jeśli klient wyrazi obawy dotyczące formy online, wyjaśnij, że nasze lekcje 1-na-1 znacząco różnią się od nauki zdalnej w szkole podczas pandemii, a nasi korepetytorzy są doskonale przygotowani do efektywnej pracy w tym trybie. Podkreśl wygodę i indywidualne podejście.

Ważne zasady:
* Bądź zawsze grzeczny i profesjonalny, ale komunikuj się w sposób przystępny i budujący relację.
* Staraj się być przekonujący i konsekwentnie dąż do umówienia pierwszej lekcji. Bądź lekko asertywny w prezentowaniu korzyści.
* Jeśli klient zaczyna wyrażać irytację lub zdecydowanie odmawia, odpuść dalsze namawianie w tej konkretnej wiadomości. Zamiast kończyć rozmowę stwierdzeniem o braku współpracy, powiedz np. "Rozumiem, dziękuję za informację. Gdyby zmienili Państwo zdanie lub mieli inne pytania, jestem do dyspozycji. Proszę się jeszcze spokojnie zastanowić." Nigdy nie zamykaj definitywnie drzwi do przyszłej współpracy.
* Jeśli nie znasz odpowiedzi na konkretne pytanie (np. o dostępność nauczyciela w danym terminie), powiedz: "To szczegółowa informacja, którą muszę sprawdzić w naszym systemie. Proszę o chwilę cierpliwości, zaraz wrócę z odpowiedzią." lub "Najaktualniejsze informacje o dostępności terminów możemy ustalić po wstępnym zapisie, skontaktuje się wtedy z Państwem nasz koordynator." Nie wymyślaj informacji.
* Odpowiadaj zawsze w języku polskim.
* Nie udzielaj porad ani informacji niezwiązanych z ofertą korepetycji firmy 'Zakręcone Korepetycje'.

Twoim zadaniem jest efektywne pozyskiwanie klientów poprzez profesjonalną i perswazyjną rozmowę."""

    prompt_content_with_instruction = [Content(role="user", parts=[Part.from_text(system_instruction_text)])] + history_to_send

    print(f"--- Generowanie odpowiedzi Gemini ({MODEL_ID}) z historią i instrukcją dla PSID {user_psid} ---")
    print(f"Ostatnia wiadomość użytkownika w prompcie: {prompt_content_with_instruction[-1]}")

    try:
        generation_config = GenerationConfig(
            max_output_tokens=2048, temperature=0.8, top_p=1.0, top_k=32
        )
        safety_settings = {
            HarmCategory.HARM_CATEGORY_HARASSMENT: HarmBlockThreshold.BLOCK_MEDIUM_AND_ABOVE,
            HarmCategory.HARM_CATEGORY_HATE_SPEECH: HarmBlockThreshold.BLOCK_MEDIUM_AND_ABOVE,
            HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT: HarmBlockThreshold.BLOCK_MEDIUM_AND_ABOVE,
            HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT: HarmBlockThreshold.BLOCK_MEDIUM_AND_ABOVE,
        }

        response = gemini_model.generate_content(
            prompt_content_with_instruction,
            generation_config=generation_config, safety_settings=safety_settings, stream=False
        )

        print("\n--- Odpowiedź Gemini ---")
        if response.candidates and response.candidates[0].content.parts:
            generated_text = response.candidates[0].content.parts[0].text
            print(f"Wygenerowany tekst (pełna długość): {len(generated_text)}")

            final_history_to_save = history_to_send + [Content(role="model", parts=[Part.from_text(generated_text)])]
            if len(final_history_to_save) > MAX_HISTORY_TURNS * 2:
                 relevant_final_history = [msg for msg in final_history_to_save if msg.role in ("user", "model")]
                 if len(relevant_final_history) > MAX_HISTORY_TURNS * 2:
                     final_history_to_save = relevant_final_history[-(MAX_HISTORY_TURNS * 2):]
            save_history(user_psid, final_history_to_save)
            print(f"Zaktualizowano i zapisano historię dla PSID {user_psid}")
            return generated_text
        else:
            finish_reason = response.candidates[0].finish_reason if response.candidates else "UNKNOWN"; safety_ratings = response.candidates[0].safety_ratings if response.candidates else []; print(f"Odpowiedź Gemini pusta/zablokowana. Powód: {finish_reason}, Oceny: {safety_ratings}"); print(f"Cała odpowiedź: {response}")
            save_history(user_psid, history_to_send)
            return "Hmm, nie mogłem wygenerować odpowiedzi."

    except Exception as e:
        print(f"!!! BŁĄD podczas generowania treści przez Gemini ({MODEL_ID}): {e} !!!")
        save_history(user_psid, history_to_send)
        error_str = str(e).lower()
        if "publisher model" in error_str or "not found" in error_str or "is not available" in error_str or "permission denied" in error_str or "access token scope" in error_str or "content with system role is not supported" in error_str:
             print(f"   >>> Błąd związany z modelem/uprawnieniami: {e}")
             return f"Nie mogę użyć modułu AI '{MODEL_ID}'."
        elif "deadline exceeded" in error_str:
             print(f"   >>> Przekroczono limit czasu Gemini ({MODEL_ID}).")
             return "Hmm, myślenie zajęło mi zbyt dużo czasu."
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
        # print("Odebrane dane JSON:", json.dumps(data, indent=2)) # Odkomentuj w razie potrzeby debugowania

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
                            # Wywołanie Gemini z uwzględnieniem historii i instrukcji
                            response_text = get_gemini_response_with_history(sender_id, message_text)
                            send_message(sender_id, response_text) # Wywołanie funkcji z dzieleniem
                        else:
                            print(f"Odebrano wiadomość bez tekstu od użytkownika {sender_id}")
                            send_message(sender_id, "Przepraszam, rozumiem tylko wiadomości tekstowe.")

                    elif messaging_event.get("postback"):
                         payload = messaging_event["postback"]["payload"]
                         print(f"Odebrano postback z payload '{payload}' od użytkownika {sender_id}")
                         # Tworzymy prompt informujący o kliknięciu przycisku
                         prompt_for_button = f"Użytkownik kliknął przycisk oznaczony jako: {payload}."
                         # Wywołujemy Gemini z historią, traktując kliknięcie jak nową wiadomość
                         response_text = get_gemini_response_with_history(sender_id, prompt_for_button)
                         send_message(sender_id, response_text) # Wywołanie funkcji z dzieleniem
                    else:
                        print("Odebrano inne zdarzenie messaging:", messaging_event)
    except Exception as e:
        print(f"!!! BŁĄD podczas przetwarzania webhooka POST: {e} !!!")
        # Ważne: Odpowiedz 200 OK, aby Facebook nie próbował wysłać ponownie
        return Response("EVENT_PROCESSING_ERROR", status=200)

    # Zawsze odpowiada 200 OK na końcu, potwierdzając odbiór
    return Response("EVENT_RECEIVED", status=200)

# --- Uruchomienie Serwera ---
if __name__ == '__main__':
    # Upewnij się, że katalog na historię istnieje przy starcie
    ensure_dir(HISTORY_DIR)
    port = 8080
    print(f"Uruchamianie serwera Flask z integracją Gemini (model: {MODEL_ID}, historia w JSON, dzielenie wiadomości) na porcie {port}...")
    # Wyłącz debug=True w środowisku produkcyjnym!
    app.run(host='0.0.0.0', port=port, debug=True)
