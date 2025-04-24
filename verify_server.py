# -*- coding: utf-8 -*-

from flask import Flask, request, Response
import os
import json
import requests # Do wysyłania wiadomości do FB API ORAZ pobierania profilu
import time     # Potrzebne do opóźnienia między wiadomościami ORAZ do symulacji pisania
import vertexai # Do komunikacji z Vertex AI
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
VERIFY_TOKEN = os.environ.get("FB_VERIFY_TOKEN", "KOLAGEN") # Twój token weryfikacyjny FB
# Użycie przykładowego tokenu jako wartości domyślnej
PAGE_ACCESS_TOKEN = os.environ.get("FB_PAGE_ACCESS_TOKEN", "EACNAHFzEhkUBO0oIHS5GZBKkzpZCbqM9LpaPDRn8wYa4mTByZAvKA7LOkuLCDxBZBi1r1ELAZALloTXWZCr3mgIuaWhD2k2hdTMCoNzQ8K5CbXO7VZBqBZBQHfXZB9NIkWznjfXxNhvmuxGGfY230S5gCGpLaihNWK0FqbSch1jcEeDZCPgZCEQJpPtaMp3AXJTjxRGLQZDZD")
PROJECT_ID = os.environ.get("GCP_PROJECT_ID", "linear-booth-450221-k1")
LOCATION = os.environ.get("GCP_LOCATION", "us-central1")
MODEL_ID = os.environ.get("VERTEX_MODEL_ID", "gemini-2.0-flash-001")

# Adresy URL API Facebook Graph
MESSAGING_API_URL = f"https://graph.facebook.com/v19.0/me/messages"
USER_PROFILE_API_URL_TEMPLATE = "https://graph.facebook.com/v19.0/{psid}?fields=first_name,last_name,profile_pic&access_token={token}"

# --- Konfiguracja Przechowywania Historii i Wiadomości ---
HISTORY_DIR = "conversation_store"
MAX_HISTORY_TURNS = 15
MESSAGE_CHAR_LIMIT = 1990
MESSAGE_DELAY_SECONDS = 1.5

# --- Konfiguracja Symulacji Pisania ---
ENABLE_TYPING_DELAY = True
MIN_TYPING_DELAY_SECONDS = 0.8
MAX_TYPING_DELAY_SECONDS = 3.5
TYPING_CHARS_PER_SECOND = 30

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

# --- Funkcja do pobierania danych profilu użytkownika ---
def get_user_profile(psid):
    """Pobiera imię, nazwisko i URL zdjęcia profilowego użytkownika z Facebook Graph API."""
    # Sprawdzenie, czy token jest ustawiony (inny niż domyślny placeholder, jeśli go używasz)
    # Zmieniono sprawdzenie, aby nie polegało na konkretnym placeholderze
    if not PAGE_ACCESS_TOKEN:
         print(f"!!! [{psid}] Brak skonfigurowanego PAGE_ACCESS_TOKEN. Nie można pobrać profilu. !!!")
         return None

    url = USER_PROFILE_API_URL_TEMPLATE.format(psid=psid, token=PAGE_ACCESS_TOKEN)
    print(f"--- [{psid}] Pobieranie profilu użytkownika z: {url.split('access_token=')[0]}... ---") # Nie loguj tokenu

    profile_data = {}
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        data = r.json()
        print(f"--- [{psid}] Odpowiedź User Profile API: {data} ---")

        profile_data['first_name'] = data.get('first_name')
        profile_data['last_name'] = data.get('last_name')
        profile_data['profile_pic'] = data.get('profile_pic')
        profile_data['id'] = data.get('id')

        return profile_data

    except requests.exceptions.Timeout:
        print(f"!!! BŁĄD TIMEOUT podczas pobierania profilu użytkownika dla PSID {psid} !!!")
        return None
    except requests.exceptions.RequestException as e:
        print(f"!!! BŁĄD podczas pobierania profilu użytkownika dla PSID {psid}: {e} !!!")
        if hasattr(e, 'response') and e.response is not None:
            try:
                error_details = e.response.json()
                print(f"Odpowiedź serwera FB (błąd profilu): {error_details}")
                if "error" in error_details and "type" in error_details["error"]:
                    if error_details["error"]["type"] == "OAuthException":
                         print("   >>> Wygląda na błąd autoryzacji. Sprawdź czy PAGE_ACCESS_TOKEN jest poprawny i nie wygasł.")
            except json.JSONDecodeError:
                print(f"Odpowiedź serwera FB (błąd profilu, nie JSON): {e.response.text}")
        return None
    except Exception as e:
        print(f"!!! Niespodziewany BŁĄD podczas pobierania profilu użytkownika dla PSID {psid}: {e} !!!")
        return None

# --- Funkcja do odczytu historii z pliku JSON ---
def load_history(user_psid):
    """Wczytuje historię konwersacji dla danego PSID z pliku JSON."""
    filepath = os.path.join(HISTORY_DIR, f"{user_psid}.json")
    history = []
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            history_data = json.load(f)
            if isinstance(history_data, list):
                for i, msg_data in enumerate(history_data):
                    if (isinstance(msg_data, dict) and
                            'role' in msg_data and isinstance(msg_data['role'], str) and
                            msg_data['role'] in ('user', 'model') and
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
                return history
            else:
                print(f"!!! BŁĄD [{user_psid}]: Plik historii nie zawiera listy JSON. Zaczynam nową historię. !!!")
                return []
    except FileNotFoundError:
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
def save_history(user_psid, history):
    """Zapisuje historię konwersacji dla danego PSID do pliku JSON."""
    ensure_dir(HISTORY_DIR)
    filepath = os.path.join(HISTORY_DIR, f"{user_psid}.json")
    temp_filepath = f"{filepath}.tmp"
    history_data = []
    try:
        for msg in history:
            if isinstance(msg, Content) and hasattr(msg, 'role') and msg.role in ('user', 'model') and hasattr(msg, 'parts') and isinstance(msg.parts, list):
                parts_data = []
                for part in msg.parts:
                    if isinstance(part, Part) and hasattr(part, 'text') and isinstance(part.text, str):
                        parts_data.append({'text': part.text})
                    else:
                        print(f"Ostrzeżenie [{user_psid}]: Pomijanie nieprawidłowej części podczas zapisu historii: {part}")
                if parts_data:
                    history_data.append({'role': msg.role, 'parts': parts_data})
                else:
                    print(f"Ostrzeżenie [{user_psid}]: Pomijanie wiadomości bez poprawnych części podczas zapisu (Rola: {msg.role})")
            else:
                 print(f"Ostrzeżenie [{user_psid}]: Pomijanie nieprawidłowego obiektu wiadomości podczas zapisu: {msg}")

        with open(temp_filepath, 'w', encoding='utf-8') as f:
            json.dump(history_data, f, ensure_ascii=False, indent=2)
        os.replace(temp_filepath, filepath)
        print(f"[{user_psid}] Zapisano historię ({len(history_data)} wiadomości) do pliku: {filepath}")

    except Exception as e:
        print(f"!!! BŁĄD [{user_psid}] podczas zapisu pliku historii: {e} !!!")
        print(f"    Plik docelowy: {filepath}")
        if os.path.exists(temp_filepath):
            try:
                os.remove(temp_filepath)
                print(f"    Usunięto plik tymczasowy {temp_filepath} po błędzie zapisu.")
            except OSError as remove_e:
                print(f"    Nie można usunąć pliku tymczasowego {temp_filepath} po błędzie zapisu: {remove_e}")


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
    print(f"    Sprawdź konfigurację projektu, lokalizacji, modelu i uprawnień.")


# --- Funkcja POMOCNICZA do wysyłania JEDNEJ wiadomości ---
def _send_single_message(recipient_id, message_text):
    """Wysyła pojedynczy fragment wiadomości tekstowej przez Messenger API."""
    if not PAGE_ACCESS_TOKEN: # Dodatkowe sprawdzenie przed próbą wysłania
        print(f"!!! [{recipient_id}] Brak PAGE_ACCESS_TOKEN. Nie można wysłać wiadomości.")
        return False

    print(f"--- Wysyłanie fragmentu do {recipient_id} (długość: {len(message_text)}) ---")
    params = {"access_token": PAGE_ACCESS_TOKEN}
    payload = {
        "recipient": {"id": recipient_id},
        "message": {"text": message_text},
        "messaging_type": "RESPONSE"
    }
    try:
        r = requests.post(MESSAGING_API_URL, params=params, json=payload, timeout=30)
        r.raise_for_status()
        response_json = r.json()
        print(f"Odpowiedź z Facebook API dla fragmentu: {response_json}")
        if response_json.get('error'):
            print(f"!!! BŁĄD zwrócony przez Facebook API: {response_json['error']} !!!")
            if "OAuthException" in response_json['error'].get('type', ''):
                 print("   >>> Wygląda na błąd autoryzacji przy WYSYŁANIU. Sprawdź PAGE_ACCESS_TOKEN.")
            return False
        return True
    except requests.exceptions.Timeout:
        print(f"!!! BŁĄD TIMEOUT podczas wysyłania fragmentu wiadomości do Messengera dla PSID {recipient_id} !!!")
        return False
    except requests.exceptions.RequestException as e:
        print(f"!!! BŁĄD podczas wysyłania fragmentu wiadomości do Messengera dla PSID {recipient_id}: {e} !!!")
        if hasattr(e, 'response') and e.response is not None:
            try:
                print(f"Odpowiedź serwera FB (błąd wysyłania): {e.response.json()}")
            except json.JSONDecodeError:
                print(f"Odpowiedź serwera FB (błąd wysyłania, nie JSON): {e.response.text}")
        return False


# --- Funkcja GŁÓWNA do wysyłania wiadomości (z dzieleniem) ---
def send_message(recipient_id, full_message_text):
    """Wysyła wiadomość tekstową do użytkownika, dzieląc ją w razie potrzeby."""
    if not full_message_text or not isinstance(full_message_text, str) or not full_message_text.strip():
        print(f"[{recipient_id}] Pominięto wysyłanie pustej lub nieprawidłowej wiadomości.")
        return

    message_len = len(full_message_text)
    print(f"[{recipient_id}] Całkowita długość wiadomości do wysłania: {message_len} znaków.")

    if message_len <= MESSAGE_CHAR_LIMIT:
        print(f"[{recipient_id}] Wiadomość mieści się w limicie, wysyłanie jako całość.")
        _send_single_message(recipient_id, full_message_text)
    else:
        chunks = []
        remaining_text = full_message_text
        print(f"[{recipient_id}] Wiadomość za długa (limit: {MESSAGE_CHAR_LIMIT}). Dzielenie na fragmenty...")

        while remaining_text:
            if len(remaining_text) <= MESSAGE_CHAR_LIMIT:
                chunks.append(remaining_text.strip())
                break

            split_index = -1
            for delimiter in ['\n\n', '\n', '. ', '! ', '? ', ' ']:
                search_limit = MESSAGE_CHAR_LIMIT - (len(delimiter) -1) if len(delimiter) > 1 else MESSAGE_CHAR_LIMIT
                temp_index = remaining_text.rfind(delimiter, 0, search_limit)
                if temp_index != -1:
                    split_index = temp_index + len(delimiter)
                    break

            if split_index == -1:
                split_index = MESSAGE_CHAR_LIMIT

            chunk = remaining_text[:split_index].strip()
            if chunk:
                chunks.append(chunk)
            remaining_text = remaining_text[split_index:].strip()

        num_chunks = len(chunks)
        print(f"[{recipient_id}] Podzielono wiadomość na {num_chunks} fragmentów.")

        send_success_count = 0
        for i, chunk in enumerate(chunks):
            print(f"[{recipient_id}] Wysyłanie fragmentu {i+1}/{num_chunks} (długość: {len(chunk)})...")
            if not _send_single_message(recipient_id, chunk):
                print(f"!!! [{recipient_id}] Anulowano wysyłanie pozostałych fragmentów z powodu błędu przy fragmencie {i+1} !!!")
                break
            send_success_count += 1
            if i < num_chunks - 1:
                print(f"[{recipient_id}] Oczekiwanie {MESSAGE_DELAY_SECONDS}s przed następnym fragmentem...")
                time.sleep(MESSAGE_DELAY_SECONDS)

        print(f"--- [{recipient_id}] Zakończono wysyłanie {send_success_count}/{num_chunks} fragmentów wiadomości ---")


# --- Funkcja do generowania odpowiedzi przez Gemini z Historią i Instrukcją ---
SYSTEM_INSTRUCTION_TEXT = """Jesteś profesjonalnym i uprzejmym asystentem obsługi klienta... (jak poprzednio) ..."""

def get_gemini_response_with_history(user_psid, current_user_message):
    """Generuje odpowiedź Gemini, używając historii zapisanej w pliku JSON, odpowiadając po polsku."""
    if not gemini_model:
        print("!!! KRYTYCZNY BŁĄD: Model Gemini nie jest załadowany !!!")
        return "Przepraszam, mam problem z połączeniem z AI (model niezaładowany)."

    history = load_history(user_psid)
    user_content = Content(role="user", parts=[Part.from_text(current_user_message)])
    current_conversation_with_user_msg = history + [user_content]

    max_messages_to_send = MAX_HISTORY_TURNS * 2
    history_to_send = current_conversation_with_user_msg
    if len(current_conversation_with_user_msg) > max_messages_to_send:
        history_to_send = current_conversation_with_user_msg[-max_messages_to_send:]
        print(f"[{user_psid}] Historia przycięta DO WYSŁANIA do: {len(history_to_send)} wiadomości (limit: {max_messages_to_send}).")

    prompt_content_with_instruction = [
        Content(role="user", parts=[Part.from_text(SYSTEM_INSTRUCTION_TEXT)]),
        Content(role="model", parts=[Part.from_text("Rozumiem. Jestem gotów pomagać klientom zgodnie z podanymi wytycznymi.")])
    ] + history_to_send

    try:
        generation_config = GenerationConfig(
            max_output_tokens=2048, temperature=0.7, top_p=0.95, top_k=40
        )
        safety_settings = {
            HarmCategory.HARM_CATEGORY_HARASSMENT: HarmBlockThreshold.BLOCK_MEDIUM_AND_ABOVE,
            HarmCategory.HARM_CATEGORY_HATE_SPEECH: HarmBlockThreshold.BLOCK_MEDIUM_AND_ABOVE,
            HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT: HarmBlockThreshold.BLOCK_MEDIUM_AND_ABOVE,
            HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT: HarmBlockThreshold.BLOCK_MEDIUM_AND_ABOVE,
        }

        response = gemini_model.generate_content(
            prompt_content_with_instruction,
            generation_config=generation_config,
            safety_settings=safety_settings,
            stream=False
        )

        generated_text = ""
        if response.candidates and response.candidates[0].content and response.candidates[0].content.parts:
            generated_text = response.candidates[0].content.parts[0].text
            print(f"[{user_psid}] Wygenerowany tekst (pełna długość): {len(generated_text)}")
            text_preview = generated_text[:150].replace('\n', '\\n')
            print(f"[{user_psid}] Fragment wygenerowanego tekstu: '{text_preview}...'")

            model_content = Content(role="model", parts=[Part.from_text(generated_text)])
            final_history_list = current_conversation_with_user_msg + [model_content]
            max_messages_to_save = MAX_HISTORY_TURNS * 2
            history_to_save = final_history_list
            if len(final_history_list) > max_messages_to_save:
                 history_to_save = final_history_list[-max_messages_to_save:]
                 print(f"[{user_psid}] Historia przycięta DO ZAPISU do: {len(history_to_save)} wiadomości (limit: {max_messages_to_save}).")

            save_history(user_psid, history_to_save)
            return generated_text
        else:
            finish_reason = "UNKNOWN"; safety_ratings = []
            if response.candidates:
                 finish_reason_obj = response.candidates[0].finish_reason
                 finish_reason = finish_reason_obj.name if hasattr(finish_reason_obj, 'name') else str(finish_reason_obj)
                 safety_ratings = response.candidates[0].safety_ratings if response.candidates[0].safety_ratings else []
            print(f"!!! [{user_psid}] Odpowiedź Gemini pusta/zablokowana. Powód: {finish_reason}, Oceny: {safety_ratings} !!!")
            save_history(user_psid, history_to_send)
            print(f"[{user_psid}] Zapisano historię do ostatniej wiadomości użytkownika z powodu błędu generowania.")
            if finish_reason == 'SAFETY': return "Przepraszam, ale nie mogę odpowiedzieć na to zapytanie ze względu na zasady bezpieczeństwa."
            elif finish_reason == 'RECITATION': return "Wygląda na to, że moje źródła na ten temat są ograniczone. Czy mogę pomóc w czymś innym?"
            else: return "Hmm, nie mogłem wygenerować odpowiedzi tym razem. Spróbuj ponownie lub inaczej sformułować pytanie."

    except Exception as e:
        print(f"!!! BŁĄD podczas generowania treści przez Gemini ({MODEL_ID}) dla PSID {user_psid}: {e} !!!")
        save_history(user_psid, history_to_send)
        print(f"[{user_psid}] Zapisano historię do ostatniej wiadomości użytkownika z powodu wyjątku podczas generowania.")
        error_str = str(e).lower()
        if "permission denied" in error_str or "api key not valid" in error_str: return "Przepraszam, wystąpił problem z autoryzacją dostępu do modułu AI."
        elif "model" in error_str and ("not found" in error_str or "is not available" in error_str): return f"Przepraszam, wybrany model AI ('{MODEL_ID}') jest obecnie niedostępny."
        elif "deadline exceeded" in error_str or "timeout" in error_str: return "Moduł AI nie odpowiedział na czas. Spróbuj ponownie za chwilę."
        elif "quota" in error_str or "resource exhausted" in error_str: return "Przepraszam, chwilowo osiągnęliśmy limit zapytań do AI. Spróbuj ponownie później."
        elif "content has an invalid" in error_str or "content is invalid" in error_str or "role" in error_str: return "Wystąpił wewnętrzny błąd formatowania zapytania do AI."
        return "Wystąpił nieoczekiwany błąd podczas przetwarzania Twojej wiadomości. Pracujemy nad rozwiązaniem."

# --- Obsługa Weryfikacji Webhooka (metoda GET) ---
@app.route('/webhook', methods=['GET'])
def webhook_verification():
    """Obsługuje weryfikację webhooka przez Facebook."""
    print("--- Otrzymano żądanie GET weryfikacyjne ---")
    hub_mode = request.args.get('hub.mode')
    hub_token = request.args.get('hub.verify_token')
    hub_challenge = request.args.get('hub.challenge')
    print(f"Mode: {hub_mode}, Token: {'Obecny' if hub_token else 'Brak'}, Challenge: {'Obecny' if hub_challenge else 'Brak'}")

    if hub_mode == 'subscribe' and hub_token == VERIFY_TOKEN:
        print("Weryfikacja GET udana!")
        return Response(hub_challenge, status=200, mimetype='text/plain')
    else:
        print(f"Weryfikacja GET nieudana. Mode: {hub_mode}, Token pasuje: {hub_token == VERIFY_TOKEN}")
        return Response("Verification failed", status=403, mimetype='text/plain')


# --- Obsługa Odbioru Wiadomości (metoda POST) - Z Historią i Pobieraniem Profilu ---
@app.route('/webhook', methods=['POST'])
def webhook_handle():
    """Obsługuje przychodzące wiadomości, pobiera profil użytkownika i generuje odpowiedź."""
    print("\n------------------------------------------")
    print("--- Otrzymano żądanie POST z Facebooka ---")
    data = None
    try:
        data = request.get_json()

        if data and data.get("object") == "page":
            for entry in data.get("entry", []):
                for messaging_event in entry.get("messaging", []):
                    if "sender" not in messaging_event or "id" not in messaging_event["sender"]:
                        print("Pominięto zdarzenie messaging bez sender.id:", messaging_event)
                        continue
                    sender_id = messaging_event["sender"]["id"]
                    recipient_id = messaging_event.get("recipient", {}).get("id")

                    print(f"Przetwarzanie zdarzenia dla Sender PSID: {sender_id}, Recipient Page ID: {recipient_id}")

                    # --- POBIERANIE PROFILU UŻYTKOWNIKA ---
                    user_profile = get_user_profile(sender_id)
                    if user_profile:
                        first_name = user_profile.get('first_name', 'Brak')
                        last_name = user_profile.get('last_name', 'Brak')
                        profile_pic = user_profile.get('profile_pic', 'Brak')
                        print(f"  -> Profil: Imię={first_name}, Nazwisko={last_name}, Zdjecie={profile_pic}")
                    else:
                        print(f"  -> Nie udało się pobrać profilu dla PSID: {sender_id}")
                    # --- KONIEC POBIERANIA PROFILU ---

                    # Reszta logiki obsługi zdarzeń
                    if messaging_event.get("message"):
                        message_data = messaging_event["message"]
                        if message_data.get("is_echo"):
                            print(f"[{sender_id}] Pominięto echo wiadomości.")
                            continue

                        if "text" in message_data:
                            message_text = message_data["text"]
                            print(f"[{sender_id}] Odebrano wiadomość tekstową: '{message_text}'")

                            response_text = get_gemini_response_with_history(sender_id, message_text)

                            if ENABLE_TYPING_DELAY and response_text:
                                response_len = len(response_text)
                                calculated_delay = response_len / TYPING_CHARS_PER_SECOND
                                final_delay = min(MAX_TYPING_DELAY_SECONDS, calculated_delay + MIN_TYPING_DELAY_SECONDS)
                                final_delay = max(0, final_delay)
                                print(f"[{sender_id}] Symulowanie pisania... Opóźnienie: {final_delay:.2f}s (długość: {response_len})")
                                time.sleep(final_delay)

                            send_message(sender_id, response_text)

                        elif "attachments" in message_data:
                             attachment_type = message_data['attachments'][0].get('type', 'nieznany')
                             print(f"[{sender_id}] Odebrano wiadomość z załącznikiem typu: {attachment_type}.")
                             send_message(sender_id, "Przepraszam, obecnie nie przetwarzam załączników. Proszę opisz, co chciałeś/chciałaś przekazać.")
                        else:
                            print(f"[{sender_id}] Odebrano wiadomość bez tekstu lub załączników.")
                            send_message(sender_id, "Przepraszam, rozumiem tylko wiadomości tekstowe.")

                    elif messaging_event.get("postback"):
                         postback_data = messaging_event["postback"]
                         payload = postback_data.get("payload")
                         title = postback_data.get("title", payload)
                         print(f"[{sender_id}] Odebrano postback. Tytuł: '{title}', Payload: '{payload}'")

                         prompt_for_button = f"Użytkownik kliknął przycisk: '{title}' (payload: {payload})."
                         response_text = get_gemini_response_with_history(sender_id, prompt_for_button)

                         if ENABLE_TYPING_DELAY and response_text:
                             response_len = len(response_text)
                             calculated_delay = response_len / TYPING_CHARS_PER_SECOND
                             final_delay = min(MAX_TYPING_DELAY_SECONDS, calculated_delay + MIN_TYPING_DELAY_SECONDS)
                             final_delay = max(0, final_delay)
                             print(f"[{sender_id}] Symulowanie pisania (postback)... Opóźnienie: {final_delay:.2f}s (długość: {response_len})")
                             time.sleep(final_delay)

                         send_message(sender_id, response_text)

                    elif messaging_event.get("read"):
                        print(f"[{sender_id}] Wiadomość przeczytana.")
                    elif messaging_event.get("delivery"):
                        print(f"[{sender_id}] Wiadomość dostarczona.")
                    else:
                        print(f"[{sender_id}] Odebrano inne (nieobsługiwane) zdarzenie messaging:", messaging_event)

        else:
            print("Otrzymano żądanie POST o nieznanym typie obiektu:", data.get("object"))

    except json.JSONDecodeError:
        print("!!! BŁĄD: Nie można zdekodować JSON z ciała żądania POST !!!")
        return Response("Invalid JSON format", status=400)
    except Exception as e:
        print(f"!!! KRYTYCZNY BŁĄD podczas przetwarzania webhooka POST: {e} !!!")
        return Response("EVENT_PROCESSING_ERROR", status=200)

    return Response("EVENT_RECEIVED", status=200)


# --- Uruchomienie Serwera ---
if __name__ == '__main__':
    ensure_dir(HISTORY_DIR)
    port = int(os.environ.get("PORT", 8080))
    debug_mode = os.environ.get("DEBUG", "False").lower() in ("true", "1", "yes")

    # Sprawdzenie, czy token strony jest ustawiony (nie jest pusty)
    if not PAGE_ACCESS_TOKEN:
         print("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
         print("!!! OSTRZEŻENIE: FB_PAGE_ACCESS_TOKEN nie jest ustawiony          !!!")
         print("!!! Pobieranie profili użytkowników i wysyłanie wiadomości       !!!")
         print("!!! NIE BĘDZIE DZIAŁAĆ bez poprawnego tokenu dostępu do strony. !!!")
         print("!!! Ustaw zmienną środowiskową FB_PAGE_ACCESS_TOKEN lub podaj   !!!")
         print("!!! poprawny token bezpośrednio w kodzie.                       !!!")
         print("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")


    print(f"Uruchamianie serwera Flask...")
    print(f"  Tryb: {'Deweloperski (Debug ON)' if debug_mode else 'Produkcyjny (Debug OFF)'}")
    print(f"  Port: {port}")
    print(f"  Katalog historii: {HISTORY_DIR}")
    print(f"  Projekt Vertex AI: {PROJECT_ID}")
    print(f"  Lokalizacja Vertex AI: {LOCATION}")
    print(f"  Model Vertex AI: {MODEL_ID}")
    print(f"  Symulacja pisania włączona: {ENABLE_TYPING_DELAY}")
    if ENABLE_TYPING_DELAY:
        print(f"    Parametry symulacji: Min={MIN_TYPING_DELAY_SECONDS}s, Max={MAX_TYPING_DELAY_SECONDS}s, CPS={TYPING_CHARS_PER_SECOND}")

    app.run(host='0.0.0.0', port=port, debug=debug_mode)
