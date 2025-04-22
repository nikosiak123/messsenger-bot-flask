# -*- coding: utf-8 -*-

from flask import Flask, request, Response
import os
import json
import requests # Do wysyłania wiadomości do FB API
import time     # Potrzebne do opóźnienia między wiadomościami ORAZ do symulacji pisania
# math is imported but not used directly in splitting logic, can be removed if not needed elsewhere
# import math
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

app = Flask(__name__)

# --- Konfiguracja ---
# W produkcji zalecane jest ładowanie tokenów ze zmiennych środowiskowych
VERIFY_TOKEN = os.environ.get("FB_VERIFY_TOKEN", "KOLAGEN") # Twój token weryfikacyjny FB
PAGE_ACCESS_TOKEN = os.environ.get("FB_PAGE_ACCESS_TOKEN", "EACNAHFzEhkUBO7nbFAtYvfPWbEht1B3chQqWLx76Ljg2ekdbJYoOrnpjATqhS0EZC8S0q8a49hEZBaZByZCaj5gr1z62dAaMgcZA1BqFOruHfFo86EWTbI3S9KL59oxFWfZCfCjwbQra9lY5of1JVnj2c9uFJDhIpWlXxLLao9Cv8JKssgs3rEDxIJBRr26HgUewZDZD") # Token dostępu do strony FB
PROJECT_ID = os.environ.get("GCP_PROJECT_ID", "linear-booth-450221-k1")  # Twoje Google Cloud Project ID
LOCATION = os.environ.get("GCP_LOCATION", "us-central1")  # Region GCP dla Vertex AI
# --- ZASADA 1: Użycie modelu wskazanego przez użytkownika ---
MODEL_ID = os.environ.get("VERTEX_MODEL_ID", "gemini-2.0-flash-001") # Model wskazany przez użytkownika

# Adres URL API Facebook Graph do wysyłania wiadomości
FACEBOOK_GRAPH_API_URL = f"https://graph.facebook.com/v19.0/me/messages" # Użyj stabilnej wersji API

# --- Konfiguracja Przechowywania Historii i Wiadomości ---
HISTORY_DIR = "conversation_store" # Nazwa katalogu do przechowywania historii
MAX_HISTORY_TURNS = 5 # Ile ostatnich par (user+model) wiadomości przechowywać (liczone jako wiadomości, nie tury)
MESSAGE_CHAR_LIMIT = 1990 # Maksymalna długość pojedynczej wiadomości (trochę mniej niż 2000 dla bezpieczeństwa)
MESSAGE_DELAY_SECONDS = 1.5 # Opóźnienie między wysyłaniem KOLEJNYCH CZĘŚCI wiadomości

# --- Konfiguracja Symulacji Pisania ---
ENABLE_TYPING_DELAY = True # Ustaw na False, aby wyłączyć symulację pisania
MIN_TYPING_DELAY_SECONDS = 0.8 # Minimalne opóźnienie nawet dla krótkich wiadomości
MAX_TYPING_DELAY_SECONDS = 3.5 # Maksymalne opóźnienie, aby nie czekać za długo
TYPING_CHARS_PER_SECOND = 30   # Szacowana szybkość "pisania" (znaków na sekundę)

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
    history = []
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            history_data = json.load(f)
            if isinstance(history_data, list):
                for i, msg_data in enumerate(history_data):
                    # Walidacja struktury wiadomości
                    if (isinstance(msg_data, dict) and
                            'role' in msg_data and isinstance(msg_data['role'], str) and
                            msg_data['role'] in ('user', 'model') and # Tylko te role są ważne dla Gemini
                            'parts' in msg_data and isinstance(msg_data['parts'], list) and
                            msg_data['parts']):

                        # Walidacja części (parts)
                        text_parts = []
                        valid_parts = True
                        for part_data in msg_data['parts']:
                            if isinstance(part_data, dict) and 'text' in part_data and isinstance(part_data['text'], str):
                                text_parts.append(Part.from_text(part_data['text']))
                            else:
                                print(f"Ostrzeżenie [{user_psid}]: Niepoprawny format części w historii (indeks {i}): {part_data}. Pomijanie wiadomości.")
                                valid_parts = False
                                break # Pomiń całą wiadomość, jeśli choć jedna część jest zła

                        if valid_parts and text_parts: # Upewnij się, że są jakieś poprawne części
                            history.append(Content(role=msg_data['role'], parts=text_parts))
                        elif not valid_parts:
                             pass # Już wypisano ostrzeżenie

                    else:
                        print(f"Ostrzeżenie [{user_psid}]: Pominięto niepoprawny format wiadomości w historii (indeks {i}): {msg_data}")

                print(f"[{user_psid}] Wczytano historię z pliku: {len(history)} poprawnych wiadomości.")
                return history
            else:
                print(f"!!! BŁĄD [{user_psid}]: Plik historii nie zawiera listy JSON. Zaczynam nową historię. !!!")
                return [] # Zwróć pustą listę
    except FileNotFoundError:
        print(f"[{user_psid}] Nie znaleziono pliku historii. Zaczynam nową.")
        return []
    except (json.JSONDecodeError, KeyError, IndexError, TypeError) as e:
        print(f"!!! BŁĄD [{user_psid}] podczas odczytu lub parsowania pliku historii: {e} !!!")
        print(f"    Plik: {filepath}")
        print("    Zaczynam nową historię dla tego użytkownika.")
        # Opcjonalnie: Przenieś uszkodzony plik
        # try:
        #     os.rename(filepath, f"{filepath}.corrupted_{int(time.time())}")
        # except OSError as backup_e:
        #     print(f"    Nie można utworzyć kopii zapasowej uszkodzonego pliku: {backup_e}")
        return []
    except Exception as e:
        print(f"!!! Niespodziewany BŁĄD [{user_psid}] podczas wczytywania historii: {e} !!!")
        return []

# --- Funkcja do zapisu historii do pliku JSON (z zapisem atomowym) ---
def save_history(user_psid, history):
    """Zapisuje historię konwersacji dla danego PSID do pliku JSON."""
    ensure_dir(HISTORY_DIR) # Upewnij się, że katalog istnieje
    filepath = os.path.join(HISTORY_DIR, f"{user_psid}.json")
    temp_filepath = f"{filepath}.tmp" # Plik tymczasowy
    history_data = []
    try:
        # Konwertuj obiekty Content z powrotem na format JSON
        for msg in history:
            # Upewnij się, że to poprawny obiekt Content z rolą i częściami
            if isinstance(msg, Content) and hasattr(msg, 'role') and msg.role in ('user', 'model') and hasattr(msg, 'parts') and isinstance(msg.parts, list):
                parts_data = []
                for part in msg.parts:
                    # Upewnij się, że część ma tekst
                    if isinstance(part, Part) and hasattr(part, 'text') and isinstance(part.text, str):
                        parts_data.append({'text': part.text})
                    else:
                        print(f"Ostrzeżenie [{user_psid}]: Pomijanie nieprawidłowej części podczas zapisu historii: {part}")

                if parts_data: # Zapisz tylko jeśli są jakieś poprawne części
                    history_data.append({'role': msg.role, 'parts': parts_data})
                else:
                    print(f"Ostrzeżenie [{user_psid}]: Pomijanie wiadomości bez poprawnych części podczas zapisu (Rola: {msg.role})")
            else:
                 print(f"Ostrzeżenie [{user_psid}]: Pomijanie nieprawidłowego obiektu wiadomości podczas zapisu: {msg}")

        # Zapisz do pliku tymczasowego
        with open(temp_filepath, 'w', encoding='utf-8') as f:
            json.dump(history_data, f, ensure_ascii=False, indent=2)

        # Atomowo zastąp oryginalny plik tymczasowym
        os.replace(temp_filepath, filepath)

        print(f"[{user_psid}] Zapisano historię ({len(history_data)} wiadomości) do pliku: {filepath}")

    except Exception as e:
        print(f"!!! BŁĄD [{user_psid}] podczas zapisu pliku historii: {e} !!!")
        print(f"    Plik docelowy: {filepath}")
        # Spróbuj usunąć plik tymczasowy, jeśli istnieje po błędzie
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
    print(f"Ładowanie modelu: {MODEL_ID}") # Używa MODEL_ID zdefiniowanego wyżej
    # Można tu dodać system_instruction jeśli model i biblioteka to wspierają
    # system_instruction_content = Content(role="system", parts=[Part.from_text(SYSTEM_INSTRUCTION_TEXT)])
    # gemini_model = GenerativeModel(MODEL_ID, system_instruction=system_instruction_content)
    gemini_model = GenerativeModel(MODEL_ID)
    print("Model załadowany pomyślnie.")
except Exception as e:
    print(f"!!! KRYTYCZNY BŁĄD podczas inicjalizacji Vertex AI lub ładowania modelu: {e} !!!")
    print(f"    Sprawdź, czy model '{MODEL_ID}' istnieje i jest dostępny w regionie '{LOCATION}' dla projektu '{PROJECT_ID}'.")
    print("    Upewnij się, że masz odpowiednie uprawnienia IAM i włączone API Vertex AI.")
    # W środowisku produkcyjnym można rozważyć wyjście z aplikacji lub tryb awaryjny
    # import sys
    # sys.exit(1)


# --- Funkcja POMOCNICZA do wysyłania JEDNEJ wiadomości ---
def _send_single_message(recipient_id, message_text):
    """Wysyła pojedynczy fragment wiadomości tekstowej przez Messenger API."""
    print(f"--- Wysyłanie fragmentu do {recipient_id} (długość: {len(message_text)}) ---")
    params = {"access_token": PAGE_ACCESS_TOKEN}
    payload = {
        "recipient": {"id": recipient_id},
        "message": {"text": message_text},
        "messaging_type": "RESPONSE" # Standardowy typ odpowiedzi
    }

    try:
        # Dodano timeout na wypadek problemów z siecią
        r = requests.post(FACEBOOK_GRAPH_API_URL, params=params, json=payload, timeout=30)
        r.raise_for_status() # Rzuci wyjątek dla odpowiedzi 4xx/5xx
        response_json = r.json()
        print(f"Odpowiedź z Facebook API dla fragmentu: {response_json}")
        # Sprawdzenie, czy FB API nie zwróciło błędu w odpowiedzi JSON
        if response_json.get('error'):
            print(f"!!! BŁĄD zwrócony przez Facebook API: {response_json['error']} !!!")
            return False
        return True # Sukces
    except requests.exceptions.Timeout:
        print(f"!!! BŁĄD TIMEOUT podczas wysyłania fragmentu wiadomości do Messengera dla PSID {recipient_id} !!!")
        return False # Błąd
    except requests.exceptions.RequestException as e:
        print(f"!!! BŁĄD podczas wysyłania fragmentu wiadomości do Messengera dla PSID {recipient_id}: {e} !!!")
        if hasattr(e, 'response') and e.response is not None:
            try:
                print(f"Odpowiedź serwera FB (błąd): {e.response.json()}")
            except json.JSONDecodeError:
                print(f"Odpowiedź serwera FB (błąd, nie JSON): {e.response.text}")
        return False # Błąd

# --- Funkcja GŁÓWNA do wysyłania wiadomości (z dzieleniem) ---
def send_message(recipient_id, full_message_text):
    """Wysyła wiadomość tekstową do użytkownika, dzieląc ją w razie potrzeby."""
    # Sprawdzenie czy wiadomość nie jest pusta lub niepoprawna
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
                chunks.append(remaining_text.strip()) # Usuń białe znaki na końcu ostatniego fragmentu
                break # Koniec

            # Szukaj najlepszego miejsca podziału wstecz od limitu znaków
            split_index = -1
            # Preferowane separatory (od najlepszego do najgorszego)
            for delimiter in ['\n\n', '\n', '. ', '! ', '? ', ' ']:
                # Szukaj w zakresie dozwolonej długości fragmentu
                # Zostaw miejsce na sam separator, jeśli ma > 1 znak
                search_limit = MESSAGE_CHAR_LIMIT - (len(delimiter) -1) if len(delimiter) > 1 else MESSAGE_CHAR_LIMIT
                temp_index = remaining_text.rfind(delimiter, 0, search_limit)
                if temp_index != -1:
                    # Podziel *po* separatorze
                    split_index = temp_index + len(delimiter)
                    break # Znaleziono dobre miejsce podziału

            # Jeśli nie znaleziono preferowanego separatora, tnij na limicie
            if split_index == -1:
                split_index = MESSAGE_CHAR_LIMIT

            chunk = remaining_text[:split_index].strip() # Pobierz fragment i usuń ew. białe znaki
            if chunk: # Dodaj tylko jeśli fragment nie jest pusty
                chunks.append(chunk)
            remaining_text = remaining_text[split_index:].strip() # Usuń białe znaki na początku następnego fragmentu

        num_chunks = len(chunks)
        print(f"[{recipient_id}] Podzielono wiadomość na {num_chunks} fragmentów.")

        send_success_count = 0
        for i, chunk in enumerate(chunks):
            print(f"[{recipient_id}] Wysyłanie fragmentu {i+1}/{num_chunks} (długość: {len(chunk)})...")
            if not _send_single_message(recipient_id, chunk):
                print(f"!!! [{recipient_id}] Anulowano wysyłanie pozostałych fragmentów z powodu błędu przy fragmencie {i+1} !!!")
                break # Przerwij wysyłanie, jeśli jeden fragment się nie powiedzie
            send_success_count += 1
            # Opóźnienie między wiadomościami, oprócz ostatniej
            # Ten delay jest między *fragmentami* długiej wiadomości
            if i < num_chunks - 1:
                print(f"[{recipient_id}] Oczekiwanie {MESSAGE_DELAY_SECONDS}s przed następnym fragmentem...")
                time.sleep(MESSAGE_DELAY_SECONDS)

        print(f"--- [{recipient_id}] Zakończono wysyłanie {send_success_count}/{num_chunks} fragmentów wiadomości ---")


# --- Funkcja do generowania odpowiedzi przez Gemini z Historią i Instrukcją ---

# Przeniesiona instrukcja dla lepszej czytelności
SYSTEM_INSTRUCTION_TEXT = """Jesteś profesjonalnym i uprzejmym asystentem obsługi klienta reprezentującym centrum specjalizujące się w wysokiej jakości korepetycjach online z matematyki, języka angielskiego i języka polskiego ('Zakręcone Korepetycje'). Obsługujemy uczniów od 4 klasy szkoły podstawowej aż do klasy maturalnej, oferując zajęcia zarówno na poziomie podstawowym, jak i rozszerzonym.

Twoim głównym celem jest aktywne zachęcanie klientów (uczniów lub ich rodziców) do skorzystania z naszych usług i umówienia się na pierwszą lekcję próbną (zgodną z cennikiem). Prezentuj ofertę rzeczowo, podkreślając korzyści płynące z nauki z naszymi doświadczonymi korepetytorami online (np. lepsze wyniki, zdana matura, większa pewność siebie, wygoda nauki z domu).

Przebieg rozmowy (elastyczny przewodnik):
1.  Przywitaj się i zapytaj, czy rozmówca szuka korepetycji lub jak możesz pomóc w tej kwestii.
2.  Ustal przedmiot zainteresowania (matematyka, j. polski, j. angielski).
3.  Ustal klasę ucznia (np. "7 klasa SP", "2 klasa LO").
4.  Dla szkoły średniej (LO/Technikum) zapytaj o poziom (podstawowy/rozszerzony).
5.  Na podstawie przedmiotu, klasy i poziomu przedstaw odpowiednią cenę za 60-minutową lekcję:
    *   Klasy 4-8 SP: 60 zł
    *   Klasy 1-3 LO/Technikum (podstawa): 65 zł
    *   Klasy 1-3 LO/Technikum (rozszerzenie): 70 zł
    *   Klasa 4 LO/Technikum (podstawa): 70 zł
    *   Klasa 4 LO/Technikum (rozszerzenie): 75 zł
6.  Po podaniu ceny, aktywnie zachęcaj do umówienia pierwszej lekcji. Podkreśl, że to świetna okazja do poznania korepetytora i sprawdzenia naszej metodyki nauczania online. Wspomnij, że lekcja jest płatna zgodnie z cennikiem.
7.  Informacje o formie zajęć (np. online przez MS Teams - link, bez instalacji) podaj, gdy klient wykaże zainteresowanie lub zapyta.
8.  Jeśli pojawią się obawy co do formy online, wyjaśnij różnicę między lekcjami 1-na-1 a zdalną nauką szkolną, podkreślając indywidualne podejście i przygotowanie naszych korepetytorów.
9.  Jeśli jeszcze raz odmówią powiedz, że zawsze warto spróbować, jeśli wspomnij o tym że już próbowali korepetycje online to powiedz że korepetytor korepetytorowi nie równy.

Ważne zasady:
*   **NAJWAŻNIEJSZE: Kontynuacja po przerwie!** Jeśli użytkownik odpisze po jakimś czasie lub po Twojej wiadomości sugerującej zastanowienie się (np. po "proszę się zastanowić"), **ZAWSZE** dokładnie przeanalizuj dostarczoną historię konwersacji i kontynuuj rozmowę od miejsca, w którym została przerwana. **NIE WOLNO** rozpoczynać wywiadu od nowa, jeśli informacje (np. o przedmiocie, klasie) zostały już wcześniej podane w historii. Odnieś się do ostatniego tematu rozmowy.
*   Staraj się kontynuować konwersację z historii a nie zaczynać od nowa, nawet gdy ktoś napisze "Dzień dobry" kontynuuj rozmowę a nie pytaj o początku o wszystko.
*   Jeśli masz już jakieś informacje od klienta w historii konwersacji nie pytaj o nie jeszcze raz.
*   Staraj się nie używać formy "Pan/Pani", brzmisz wtedy jak bot wybieraj raczej coś typu "Państwo", ni chyba, że w trakcie konwersacji z odpowiedzi osoby można wyczytać jej płeć.
*   Jeśli rodzic zada ci jakieś pytanie odpowiedz na nie, przebieg rozmowy to tylko sugestia.
*   Jeśli dostałeś jakąś informację przedtem i jest ona w historii konwersacji z osobą nie pytaj się jej poraz drugi tylko od razu użyj tamtej informacji.
*   Staraj się rozdzielać wywiad i rozmowę na wiele wiadomości, tak abyś nie musiał wysyłać długich tekstów w wiadomościach. Zadawaj jedno główne pytanie na wiadomość.
*   Bądź zawsze grzeczny i profesjonalny, ale komunikuj się w sposób przystępny i budujący relację.
*   Staraj się być przekonujący i konsekwentnie dąż do umówienia pierwszej lekcji. Bądź lekko asertywny w prezentowaniu korzyści.
*   Jeśli klient zaczyna wyrażać irytację lub zdecydowanie odmawia, odpuść dalsze namawianie w tej konkretnej wiadomości. Zamiast kończyć rozmowę stwierdzeniem o braku współpracy, powiedz np. "Rozumiem, dziękuję za informację. Gdyby zmienili Państwo zdanie lub mieli inne pytania, jestem do dyspozycji. Proszę się jeszcze spokojnie zastanowić." Nigdy nie zamykaj definitywnie drzwi do przyszłej współpracy.
*   Jeśli nie znasz odpowiedzi na konkretne pytanie (np. o dostępność nauczyciela w danym terminie), powiedz: "To szczegółowa informacja, którą muszę sprawdzić w naszym systemie. Proszę o chwilę cierpliwości, zaraz wrócę z odpowiedzią." lub "Najaktualniejsze informacje o dostępności terminów możemy ustalić po wstępnym zapisie, skontaktuje się wtedy z Państwem nasz koordynator." Nie wymyślaj informacji.
*   Odpowiadaj zawsze w języku polskim.
*   Nie udzielaj porad ani informacji niezwiązanych z ofertą korepetycji firmy 'Zakręcone Korepetycje'.
*   Nie używaj słowa "wyłącznie" jeśli mówisz o korepetycjach online (punkt 7 w przebiegu rozmowy został zaktualizowany).

Twoim zadaniem jest efektywne pozyskiwanie klientów poprzez profesjonalną i perswazyjną rozmowę."""


def get_gemini_response_with_history(user_psid, current_user_message):
    """Generuje odpowiedź Gemini, używając historii zapisanej w pliku JSON, odpowiadając po polsku."""
    if not gemini_model:
        print("!!! KRYTYCZNY BŁĄD: Model Gemini nie jest załadowany !!!")
        return "Przepraszam, mam problem z połączeniem z AI (model niezaładowany)."

    # --- KROK 1: Załaduj historię ---
    history = load_history(user_psid)
    # Logowanie załadowanej historii przeniesione do load_history

    # --- KROK 2: Przygotuj bieżącą wiadomość użytkownika ---
    user_content = Content(role="user", parts=[Part.from_text(current_user_message)])

    # --- KROK 3: Połącz historię z bieżącą wiadomością ---
    current_conversation_with_user_msg = history + [user_content]

    # --- KROK 4: Przytnij historię, jeśli jest za długa (DO WYSŁANIA DO MODELU) ---
    # Liczymy wiadomości (nie tury), user+model = 2 wiadomości na turę
    max_messages_to_send = MAX_HISTORY_TURNS * 2
    history_to_send = current_conversation_with_user_msg
    if len(current_conversation_with_user_msg) > max_messages_to_send:
        # Przycinamy *od początku* listy, zachowując najnowsze wiadomości
        history_to_send = current_conversation_with_user_msg[-max_messages_to_send:]
        print(f"[{user_psid}] Historia przycięta DO WYSŁANIA do: {len(history_to_send)} wiadomości (limit: {max_messages_to_send}).")
    else:
         print(f"[{user_psid}] Użyto całej historii DO WYSŁANIA: {len(history_to_send)} wiadomości.")

    # --- KROK 5: Przygotuj pełny prompt z instrukcją systemową ---
    # Struktura: Instrukcja jako pierwsza wiadomość "user" + inicjująca odpowiedź "model" + przycięta historia
    # Sprawdź, czy ten format działa najlepiej dla Twojego modelu.
    prompt_content_with_instruction = [
        Content(role="user", parts=[Part.from_text(SYSTEM_INSTRUCTION_TEXT)]),
        Content(role="model", parts=[Part.from_text("Rozumiem. Jestem gotów pomagać klientom zgodnie z podanymi wytycznymi.")]) # Odpowiedź inicjująca
    ] + history_to_send # Dodajemy *przyciętą* historię

    # --- KROK 6: LOGOWANIE ZAWARTOŚCI WYSYŁANEJ DO GEMINI ---
    print(f"\n--- [{user_psid}] Zawartość wysyłana do Gemini ({MODEL_ID}) ---")
    for i, content in enumerate(prompt_content_with_instruction):
        role = content.role
        # Poprawka: wykonaj replace i skracanie przed f-stringiem
        raw_text = content.parts[0].text
        text_fragment = raw_text[:150].replace('\n', '\\n')
        text_to_log = text_fragment + "..." if len(raw_text) > 150 else text_fragment
        print(f"  [{i}] Role: {role}, Text: '{text_to_log}'") # Używamy przetworzonej zmiennej
    print(f"--- Koniec zawartości dla {user_psid} ---\n")
    # ---------------------------------------------------------

    try:
        # --- KROK 7: Konfiguracja i generowanie odpowiedzi ---
        generation_config = GenerationConfig(
            max_output_tokens=2048,
            temperature=0.7, # Zmniejszona dla większej spójności
            top_p=0.95,      # Wartości domyślne/zalecane
            top_k=40
        )
        safety_settings = {
            HarmCategory.HARM_CATEGORY_HARASSMENT: HarmBlockThreshold.BLOCK_MEDIUM_AND_ABOVE,
            HarmCategory.HARM_CATEGORY_HATE_SPEECH: HarmBlockThreshold.BLOCK_MEDIUM_AND_ABOVE,
            HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT: HarmBlockThreshold.BLOCK_MEDIUM_AND_ABOVE,
            HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT: HarmBlockThreshold.BLOCK_MEDIUM_AND_ABOVE,
        }

        response = gemini_model.generate_content(
            prompt_content_with_instruction, # Używamy promptu z instrukcją i przyciętą historią
            generation_config=generation_config,
            safety_settings=safety_settings,
            stream=False # Pobierz całą odpowiedź naraz
        )

        print(f"\n--- [{user_psid}] Odpowiedź Gemini (Raw) ---")
        print(response) # Logowanie surowej odpowiedzi dla debugowania

        # --- KROK 8: Przetwarzanie i zapis odpowiedzi ---
        generated_text = ""
        if response.candidates and response.candidates[0].content and response.candidates[0].content.parts:
            generated_text = response.candidates[0].content.parts[0].text
            print(f"[{user_psid}] Wygenerowany tekst (pełna długość): {len(generated_text)}")

            # --- POPRAWKA f-string ---
            # Wykonaj replace i skracanie przed f-stringiem
            text_preview = generated_text[:150].replace('\n', '\\n')
            print(f"[{user_psid}] Fragment wygenerowanego tekstu: '{text_preview}...'")
            # --- KONIEC POPRAWKI f-string ---

            # --- KROK 9: Aktualizacja i zapis PEŁNEJ (ale przyciętej do zapisu) historii ---
            # Dodajemy odpowiedź modelu do *pełnej* historii z bieżącą wiadomością użytkownika
            model_content = Content(role="model", parts=[Part.from_text(generated_text)])
            # current_conversation_with_user_msg zawierała pełną historię + bieżącą wiadomość usera
            final_history_list = current_conversation_with_user_msg + [model_content]

            # Przycinamy historię *przed zapisem* do pliku, zachowując najnowsze
            max_messages_to_save = MAX_HISTORY_TURNS * 2
            history_to_save = final_history_list
            if len(final_history_list) > max_messages_to_save:
                 history_to_save = final_history_list[-max_messages_to_save:]
                 print(f"[{user_psid}] Historia przycięta DO ZAPISU do: {len(history_to_save)} wiadomości (limit: {max_messages_to_save}).")

            save_history(user_psid, history_to_save) # Zapisujemy przyciętą historię
            # print(f"[{user_psid}] Zaktualizowano i zapisano historię.") # Komunikat przeniesiony do save_history
            return generated_text
        else:
            # Obsługa pustej/zablokowanej odpowiedzi
            finish_reason = "UNKNOWN"; safety_ratings = []
            if response.candidates:
                 # Użyj .name jeśli to enum, inaczej konwertuj na string
                 finish_reason_obj = response.candidates[0].finish_reason
                 finish_reason = finish_reason_obj.name if hasattr(finish_reason_obj, 'name') else str(finish_reason_obj)
                 safety_ratings = response.candidates[0].safety_ratings if response.candidates[0].safety_ratings else []
            print(f"!!! [{user_psid}] Odpowiedź Gemini pusta/zablokowana. Powód: {finish_reason}, Oceny: {safety_ratings} !!!")

            # Zapisz historię tylko do wiadomości użytkownika w tym przypadku
            # Używamy historii, którą faktycznie *próbowano* wysłać
            save_history(user_psid, history_to_send)
            print(f"[{user_psid}] Zapisano historię do ostatniej wiadomości użytkownika z powodu błędu generowania.")
            # Zwróć odpowiednią wiadomość zastępczą
            if finish_reason == 'SAFETY':
                 return "Przepraszam, ale nie mogę odpowiedzieć na to zapytanie ze względu na zasady bezpieczeństwa."
            elif finish_reason == 'RECITATION':
                 return "Wygląda na to, że moje źródła na ten temat są ograniczone. Czy mogę pomóc w czymś innym?"
            else: # Inne powody (np. MAX_TOKENS, UNKNOWN)
                 return "Hmm, nie mogłem wygenerować odpowiedzi tym razem. Spróbuj ponownie lub inaczej sformułować pytanie."


    except Exception as e:
        print(f"!!! BŁĄD podczas generowania treści przez Gemini ({MODEL_ID}) dla PSID {user_psid}: {e} !!!")
        # Zapisz historię tylko do wiadomości użytkownika w tym przypadku
        # Używamy historii, którą faktycznie *próbowano* wysłać
        save_history(user_psid, history_to_send)
        print(f"[{user_psid}] Zapisano historię do ostatniej wiadomości użytkownika z powodu wyjątku podczas generowania.")

        # Zwróć odpowiednią wiadomość o błędzie
        error_str = str(e).lower()
        if "permission denied" in error_str or "api key not valid" in error_str:
             print("   >>> Błąd uprawnień lub klucza API Vertex AI.")
             return "Przepraszam, wystąpił problem z autoryzacją dostępu do modułu AI."
        elif "model" in error_str and ("not found" in error_str or "is not available" in error_str):
             print(f"   >>> Model '{MODEL_ID}' nie znaleziony lub niedostępny.")
             return f"Przepraszam, wybrany model AI ('{MODEL_ID}') jest obecnie niedostępny."
        elif "deadline exceeded" in error_str or "timeout" in error_str:
             print("   >>> Przekroczono limit czasu żądania Vertex AI.")
             return "Moduł AI nie odpowiedział na czas. Spróbuj ponownie za chwilę."
        elif "quota" in error_str or "resource exhausted" in error_str:
             print("   >>> Przekroczono limit (quota) Vertex AI.")
             return "Przepraszam, chwilowo osiągnęliśmy limit zapytań do AI. Spróbuj ponownie później."
        elif "content has an invalid" in error_str or "content is invalid" in error_str or "role" in error_str:
             print(f"   >>> Nieprawidłowy format treści wysłanej do Vertex AI: {e}")
             return "Wystąpił wewnętrzny błąd formatowania zapytania do AI."
        # Ogólny błąd
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

# --- Obsługa Odbioru Wiadomości (metoda POST) - Z Historią ---
@app.route('/webhook', methods=['POST'])
def webhook_handle():
    """Obsługuje przychodzące wiadomości i zdarzenia z Facebooka."""
    print("\n------------------------------------------")
    print("--- Otrzymano żądanie POST z Facebooka ---")
    data = None
    try:
        data = request.get_json()
        # Odkomentuj poniższe tylko do zaawansowanego debugowania
        # print("Odebrane dane JSON:", json.dumps(data, indent=2))

        if data and data.get("object") == "page":
            for entry in data.get("entry", []):
                # Przetwarzaj zdarzenia 'messaging'
                for messaging_event in entry.get("messaging", []):
                    # Podstawowa walidacja zdarzenia
                    if "sender" not in messaging_event or "id" not in messaging_event["sender"]:
                        print("Pominięto zdarzenie messaging bez sender.id:", messaging_event)
                        continue
                    sender_id = messaging_event["sender"]["id"]
                    recipient_id = messaging_event.get("recipient", {}).get("id") # ID strony
                    print(f"Przetwarzanie zdarzenia dla Sender PSID: {sender_id}, Recipient Page ID: {recipient_id}")

                    # Obsługa wiadomości tekstowych
                    if messaging_event.get("message"):
                        message_data = messaging_event["message"]

                        # Ignoruj echa wiadomości wysłanych przez bota
                        if message_data.get("is_echo"):
                            print(f"[{sender_id}] Pominięto echo wiadomości.")
                            continue

                        if "text" in message_data:
                            message_text = message_data["text"]
                            print(f"[{sender_id}] Odebrano wiadomość tekstową: '{message_text}'")

                            # Generuj odpowiedź Gemini z historią
                            response_text = get_gemini_response_with_history(sender_id, message_text)

                            # --- DODANO: Symulacja opóźnienia pisania ---
                            if ENABLE_TYPING_DELAY and response_text: # Tylko jeśli włączone i jest co wysłać
                                response_len = len(response_text)
                                # Oblicz podstawowe opóźnienie na podstawie długości
                                calculated_delay = response_len / TYPING_CHARS_PER_SECOND
                                # Dodaj minimalny czas i ogranicz do maksimum
                                final_delay = min(MAX_TYPING_DELAY_SECONDS, calculated_delay + MIN_TYPING_DELAY_SECONDS)
                                # Upewnij się, że nie jest ujemne (choć nie powinno być)
                                final_delay = max(0, final_delay)

                                print(f"[{sender_id}] Symulowanie pisania... Opóźnienie: {final_delay:.2f}s (długość: {response_len})")
                                time.sleep(final_delay)
                            # --- KONIEC: Symulacja opóźnienia pisania ---

                            # Wyślij odpowiedź (z dzieleniem w razie potrzeby)
                            send_message(sender_id, response_text)

                        elif "attachments" in message_data:
                             attachment_type = message_data['attachments'][0].get('type', 'nieznany')
                             print(f"[{sender_id}] Odebrano wiadomość z załącznikiem typu: {attachment_type}.")
                             # Można dodać opóźnienie również tutaj, jeśli chcesz
                             # if ENABLE_TYPING_DELAY: time.sleep(MIN_TYPING_DELAY_SECONDS)
                             send_message(sender_id, "Przepraszam, obecnie nie przetwarzam załączników. Proszę opisz, co chciałeś/chciałaś przekazać.")

                        else:
                            print(f"[{sender_id}] Odebrano wiadomość bez tekstu lub załączników.")
                            # Można dodać opóźnienie również tutaj
                            # if ENABLE_TYPING_DELAY: time.sleep(MIN_TYPING_DELAY_SECONDS)
                            send_message(sender_id, "Przepraszam, rozumiem tylko wiadomości tekstowe.")

                    # Obsługa kliknięć przycisków (postback)
                    elif messaging_event.get("postback"):
                         postback_data = messaging_event["postback"]
                         payload = postback_data.get("payload")
                         title = postback_data.get("title", payload) # Użyj tytułu jeśli jest, inaczej payload
                         print(f"[{sender_id}] Odebrano postback. Tytuł: '{title}', Payload: '{payload}'")

                         # Stwórz prompt opisujący kliknięcie
                         prompt_for_button = f"Użytkownik kliknął przycisk: '{title}' (payload: {payload})."

                         # Wywołaj Gemini z historią, traktując kliknięcie jak nową wiadomość
                         response_text = get_gemini_response_with_history(sender_id, prompt_for_button)

                         # --- DODANO: Symulacja opóźnienia pisania ---
                         if ENABLE_TYPING_DELAY and response_text:
                             response_len = len(response_text)
                             calculated_delay = response_len / TYPING_CHARS_PER_SECOND
                             final_delay = min(MAX_TYPING_DELAY_SECONDS, calculated_delay + MIN_TYPING_DELAY_SECONDS)
                             final_delay = max(0, final_delay)
                             print(f"[{sender_id}] Symulowanie pisania (postback)... Opóźnienie: {final_delay:.2f}s (długość: {response_len})")
                             time.sleep(final_delay)
                         # --- KONIEC: Symulacja opóźnienia pisania ---

                         send_message(sender_id, response_text)

                    # Opcjonalnie: Obsługa innych zdarzeń (read, delivery)
                    elif messaging_event.get("read"):
                        print(f"[{sender_id}] Wiadomość przeczytana.")
                    elif messaging_event.get("delivery"):
                        print(f"[{sender_id}] Wiadomość dostarczona.")

                    else:
                        print(f"[{sender_id}] Odebrano inne (nieobsługiwane) zdarzenie messaging:", messaging_event)

                # Opcjonalnie: Przetwarzaj zdarzenia 'standby' jeśli używasz Handover Protocol
                # for standby_event in entry.get("standby", []):
                #     print("Odebrano zdarzenie standby:", standby_event)

        else:
            print("Otrzymano żądanie POST o nieznanym typie obiektu:", data.get("object"))

    except json.JSONDecodeError:
        print("!!! BŁĄD: Nie można zdekodować JSON z ciała żądania POST !!!")
        # Zwróć 400 Bad Request, jeśli JSON jest niepoprawny
        return Response("Invalid JSON format", status=400)
    except Exception as e:
        print(f"!!! KRYTYCZNY BŁĄD podczas przetwarzania webhooka POST: {e} !!!")
        # Zwróć 200 OK, aby Facebook nie próbował wysłać ponownie tego samego zdarzenia
        # Logowanie błędu jest kluczowe do diagnozy
        return Response("EVENT_PROCESSING_ERROR", status=200)

    # Zawsze odpowiada 200 OK na końcu, potwierdzając odbiór zdarzenia
    return Response("EVENT_RECEIVED", status=200)

# --- Uruchomienie Serwera ---
if __name__ == '__main__':
    # Upewnij się, że katalog na historię istnieje przy starcie
    ensure_dir(HISTORY_DIR)

    # Pobierz port ze zmiennej środowiskowej lub użyj domyślnego 8080
    port = int(os.environ.get("PORT", 8080))
    # Kontroluj tryb debugowania przez zmienną środowiskową (domyślnie wyłączony dla bezpieczeństwa)
    debug_mode = os.environ.get("DEBUG", "False").lower() in ("true", "1", "yes")

    print(f"Uruchamianie serwera Flask...")
    print(f"  Tryb: {'Deweloperski (Debug ON)' if debug_mode else 'Produkcyjny (Debug OFF)'}")
    print(f"  Port: {port}")
    print(f"  Katalog historii: {HISTORY_DIR}")
    print(f"  Projekt Vertex AI: {PROJECT_ID}")
    print(f"  Lokalizacja Vertex AI: {LOCATION}")
    print(f"  Model Vertex AI: {MODEL_ID}") # Wyświetla używany model
    print(f"  Symulacja pisania włączona: {ENABLE_TYPING_DELAY}")
    if ENABLE_TYPING_DELAY:
        print(f"    Parametry symulacji: Min={MIN_TYPING_DELAY_SECONDS}s, Max={MAX_TYPING_DELAY_SECONDS}s, CPS={TYPING_CHARS_PER_SECOND}")
    # Nie loguj tokenów dostępu w produkcji!
    # print(f"  FB Verify Token: {VERIFY_TOKEN}")

    # Uruchom aplikację Flask
    # UWAGA: W środowisku produkcyjnym użyj serwera WSGI jak gunicorn lub uwsgi zamiast app.run()
    # Przykład dla gunicorn: gunicorn --bind 0.0.0.0:{port} verify_server:app  (jeśli plik nazywa się verify_server.py)
    app.run(host='0.0.0.0', port=port, debug=debug_mode)
