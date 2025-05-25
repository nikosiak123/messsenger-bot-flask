# -*- coding: utf-8 -*-
import sys
import time
import os
import pickle
import traceback
import unicodedata # Nadal potrzebne do kluczy
import re # Do czyszczenia nazw plików
import threading # Do zrzutów ekranu w tle
from datetime import datetime # Do nazw plików zrzutów

# --- IMPORTY dla Google AI Studio (Gemini API) ---
import google.generativeai as genai
# --- Koniec importów ---

# --- IMPORTY dla Google Sheets ---
try:
    import gspread
    from google.oauth2.service_account import Credentials
    GSPREAD_AVAILABLE = True
except ImportError:
    GSPREAD_AVAILABLE = False
    print("OSTRZEŻENIE: Biblioteki gspread lub google-auth nie są zainstalowane. Statystyki Google Sheets nie będą działać.")
    print("Aby zainstalować: pip install gspread google-auth google-auth-oauthlib google-auth-httplib2")
# --- Koniec importów dla Google Sheets ---


# Selenium Imports
from selenium import webdriver
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.common.by import By
from selenium.common.exceptions import WebDriverException, TimeoutException, StaleElementReferenceException, NoSuchElementException
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.keys import Keys
from concurrent.futures import ThreadPoolExecutor, as_completed # Do równoległego przetwarzania

# Globalne zmienne
COOKIES_FILE = "cookies.pkl"
PROCESSED_POSTS_DIR = "processed_posts_db" 
SCREENSHOTS_DIR = "screenshots" 
GOOGLE_SHEET_ID = "1vpsIAEkqtY3ZJ5Mr67Dda45aZ55V1O-Ux9ODjwk13qw" 
GOOGLE_SHEET_KEY_FILE = "ARKUSZ_KLUCZ.json" 
GOOGLE_SHEET_WORKSHEET_NAME = "Arkusz3"


# --- Konfiguracja ścieżek ---
#PATH_DO_GOOGLE_CHROME = "/opt/google/chrome/chrome"  # Standardowa ścieżka do Chrome
#PATH_DO_RECZNEGO_CHROMEDRIVER = "/home/nikodnaj2/chromedriver-linux64/chromedriver" # Twoja 
PATH_DO_GOOGLE_CHROME = "/opt/google/chrome/chrome" 
PATH_DO_RECZNEGO_CHROMEDRIVER = "/home/nikodnaj2/chromedriver-linux64/chromedriver" 
# --- Koniec konfiguracji ścieżek ---

# --- Mapowanie wyboru użytkownika na nazwy profili (DOKŁADNE NAZWY STRON) ---
PROFILE_MAPPING = {
    "1": {
        "name": "Zakręcone Korepetycje - MATEMATYKA",
        "specialization_subject": "MATEMATYKA" 
    },
    "2": {
        "name": "Zakręcone Korepetycje - j. Polski",
        "specialization_subject": "POLSKI"
    },
    "3": {
        "name": "English Zone - Zakręcone Korepetycje",
        "specialization_subject": "ANGIELSKI"
    },
    "4": {
        "name": "Profil Testowy 4 - Fizyka", 
        "specialization_subject": "INNY_PRZEDMIOT"
    },
    "5": {
        "name": "Profil Testowy 5 - Chemia", 
        "specialization_subject": "INNY_PRZEDMIOT"
    },
    "6": {
        "name": "Profil Testowy 6 - Biologia", 
        "specialization_subject": "INNY_PRZEDMIOT"
    }
}
# --- Koniec mapowania ---

# --- Funkcje pomocnicze ---

def sanitize_filename(name):
    name = ''.join(c for c in unicodedata.normalize('NFD', name)
                   if unicodedata.category(c) != 'Mn')
    name = name.replace(' ', '_').replace('-', '_')
    name = re.sub(r'[^\w_]+', '', name)
    name = re.sub(r'_+', '_', name)
    name = name.strip('_')
    return name.lower() or "default_profile"

def get_processed_posts_filepath(profile_name):
    if not os.path.exists(PROCESSED_POSTS_DIR):
        try:
            os.makedirs(PROCESSED_POSTS_DIR)
            print(f"INFO: Utworzono katalog na bazy przetworzonych postów: {PROCESSED_POSTS_DIR}")
        except OSError as e:
            print(f"BŁĄD: Nie można utworzyć katalogu {PROCESSED_POSTS_DIR}: {e}")
            return f"processed_posts_{sanitize_filename(profile_name)}.pkl"
    return os.path.join(PROCESSED_POSTS_DIR, f"processed_{sanitize_filename(profile_name)}.pkl")

def load_processed_keys(profile_name):
    filepath = get_processed_posts_filepath(profile_name)
    if os.path.exists(filepath):
        try:
            with open(filepath, 'rb') as f:
                keys = pickle.load(f)
                if isinstance(keys, set):
                    print(f"INFO [{profile_name}]: Załadowano {len(keys)} przetworzonych kluczy z {filepath}")
                    return keys
                else:
                    print(f"OSTRZEŻENIE [{profile_name}]: Plik {filepath} nie zawierał zbioru (set). Tworzę nowy.")
                    return set()
        except (pickle.UnpicklingError, EOFError, TypeError) as e:
            print(f"BŁĄD [{profile_name}]: Nie można odczytać pliku przetworzonych kluczy {filepath}: {e}. Tworzę nowy.")
            return set()
        except Exception as e:
            print(f"BŁĄD [{profile_name}]: Nieoczekiwany błąd podczas ładowania kluczy z {filepath}: {e}")
            return set()
    else:
        print(f"INFO [{profile_name}]: Plik przetworzonych kluczy {filepath} nie istnieje. Tworzę nowy zbiór.")
        return set()

def save_processed_keys(keys_set, profile_name):
    filepath = get_processed_posts_filepath(profile_name)
    try:
        with open(filepath, 'wb') as f:
            pickle.dump(keys_set, f)
    except Exception as e:
        print(f"BŁĄD [{profile_name}]: Nie można zapisać przetworzonych kluczy do {filepath}: {e}")

def scroll_to_element_and_wait(driver, element, wait_time=1.0, block_position='center'):
    try:
        driver.execute_script(f"arguments[0].scrollIntoView({{block: '{block_position}', inline: 'nearest', behavior: 'auto'}});", element)
        time.sleep(wait_time)
        return True
    except StaleElementReferenceException:
        print("  OSTRZEŻENIE (scroll_to_element_and_wait): Element stał się nieaktualny podczas próby scrollowania.")
        return False
    except Exception as e:
        print(f"  BŁĄD (scroll_to_element_and_wait): Nie udało się zescrollować do elementu: {e}")
        return False

def save_cookies(driver, file_path):
    try:
        with open(file_path, 'wb') as file:
            pickle.dump(driver.get_cookies(), file)
        print(f"INFO: Ciasteczka zapisane do pliku: {file_path}")
    except Exception as e:
        print(f"BŁĄD: Nie udało się zapisać ciasteczek: {e}")

def load_cookies(driver, file_path):
    if os.path.exists(file_path):
        try:
            with open(file_path, 'rb') as file:
                cookies = pickle.load(file)
                if cookies:
                    print(f"INFO: Próba załadowania {len(cookies)} ciasteczek z {file_path}")
                    target_domain = '.facebook.com'
                    try:
                        first_cookie_domain = cookies[0].get('domain')
                        if first_cookie_domain and isinstance(first_cookie_domain, str):
                             target_domain = first_cookie_domain.lstrip('.')
                             if not target_domain.startswith('.'):
                                 target_domain = '.' + target_domain
                    except IndexError:
                        pass
                    driver.get(f"https://www.{target_domain.lstrip('.')}")
                    time.sleep(1)
                    for cookie in cookies:
                        if 'expiry' in cookie and isinstance(cookie['expiry'], float):
                            cookie['expiry'] = int(cookie['expiry'])
                        if 'sameSite' in cookie and cookie['sameSite'] not in ['Strict', 'Lax', 'None']:
                            del cookie['sameSite']
                        try:
                            driver.add_cookie(cookie)
                        except Exception as e_cookie:
                            print(f"INFO: Nie można dodać ciasteczka: {cookie.get('name', 'N/A')}. Błąd: {e_cookie}")
                    print(f"INFO: Zakończono próbę ładowania ciasteczek z pliku: {file_path}")
                    return True
                else:
                    print(f"INFO: Plik z ciasteczkami ({file_path}) jest pusty.")
                    return False
        except (pickle.UnpicklingError, EOFError) as e_pickle:
            print(f"BŁĄD: Nie udało się odczytać pliku ciasteczek (może być uszkodzony): {e_pickle}")
            return False
        except Exception as e:
            print(f"BŁĄD: Nie udało się wczytać ciasteczek: {e}")
            traceback.print_exc()
            return False
    else:
        print(f"INFO: Plik z ciasteczkami ({file_path}) nie istnieje.")
        return False

# --- DODANA FUNKCJA DO ZRZUTÓW EKRANU ---
def take_screenshots_periodically(driver, profile_id_for_filename, stop_event, interval=10):
    """Robi zrzuty ekranu co określony interwał, dopóki stop_event nie zostanie ustawiony."""
    if not os.path.exists(SCREENSHOTS_DIR):
        try:
            os.makedirs(SCREENSHOTS_DIR)
            print(f"INFO [Screenshots]: Utworzono katalog na zrzuty ekranu: {SCREENSHOTS_DIR}")
        except OSError as e:
            print(f"BŁĄD [Screenshots]: Nie można utworzyć katalogu {SCREENSHOTS_DIR}: {e}")
            return 

    print(f"INFO [Screenshots-{profile_id_for_filename}]: Wątek zrzutów ekranu uruchomiony (interwał: {interval}s).")
    count = 0
    while not stop_event.is_set():
        try:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3] 
            filename = os.path.join(SCREENSHOTS_DIR, f"screen_{profile_id_for_filename}_{timestamp}_{count}.png")
            if driver and hasattr(driver, 'save_screenshot'): 
                driver.save_screenshot(filename)
                count += 1
            else:
                print(f"OSTRZEŻENIE [Screenshots-{profile_id_for_filename}]: Driver nie jest dostępny, pomijanie zrzutu.")
        except WebDriverException as e_wd:
            print(f"BŁĄD [Screenshots-{profile_id_for_filename}]: Błąd WebDriver podczas robienia zrzutu: {e_wd}")
            if "browser has already quit" in str(e_wd).lower() or "target window already closed" in str(e_wd).lower():
                print(f"INFO [Screenshots-{profile_id_for_filename}]: Przeglądarka zamknięta, zatrzymywanie wątku zrzutów.")
                break
        except Exception as e:
            print(f"BŁĄD [Screenshots-{profile_id_for_filename}]: Nieoczekiwany błąd podczas robienia zrzutu: {e}")
        
        stop_event.wait(timeout=interval) 

    print(f"INFO [Screenshots-{profile_id_for_filename}]: Wątek zrzutów ekranu zakończony.")
# --- KONIEC FUNKCJI DO ZRZUTÓW EKRANU ---


def search_and_filter_facebook(driver, search_term):
    try:
        print(f"INFO: Rozpoczynanie wyszukiwania dla frazy: '{search_term}'")
        wait = WebDriverWait(driver, 15)
        search_input_xpath = "//input[@aria-label='Szukaj na Facebooku' or @placeholder='Szukaj na Facebooku']"
        search_input_element = wait.until(EC.visibility_of_element_located((By.XPATH, search_input_xpath)))
        search_input_element.click()
        time.sleep(0.3)
        search_input_element.clear()
        time.sleep(0.2)
        search_input_element.send_keys(search_term)
        print(f"INFO: Wpisano tekst: '{search_term}'.")
        time.sleep(0.5)
        search_input_element.send_keys(Keys.RETURN)
        print("INFO: Naciśnięto RETURN w polu wyszukiwania.")
        print("INFO: Oczekiwanie na załadowanie wyników wyszukiwania...")
        try:
            WebDriverWait(driver, 10).until(EC.any_of(
                EC.url_contains("/search/"),
                EC.presence_of_element_located((By.XPATH, "//span[normalize-space(.)='Posty']"))
            ))
            print(f"INFO: Strona wyników wyszukiwania załadowana. URL: {driver.current_url}")
        except TimeoutException:
            print("OSTRZEŻENIE: Nie wykryto jednoznacznie załadowania strony wyników. Kontynuuję.")
        time.sleep(3)
        print("INFO: Próba znalezienia i kliknięcia filtra 'Posty'...")
        posts_filter_xpath = "//a[@role='link'][.//span[normalize-space(.)='Posty']][not(contains(@href,'/groups/'))]"
        posts_filter_alt_xpath = "//div[@role='list']//div[@role='listitem']//a[@role='link'][.//span[normalize-space(.)='Posty']]"

        clicked_successfully = False
        element_to_click = None
        try:
            print(f"INFO: Próba znalezienia filtra 'Posty' XPath1: {posts_filter_xpath}")
            element_to_click = wait.until(EC.presence_of_element_located((By.XPATH, posts_filter_xpath)))
        except TimeoutException:
            print(f"INFO: Nie znaleziono filtra 'Posty' XPath1. Próba XPath2: {posts_filter_alt_xpath}")
            try:
                element_to_click = wait.until(EC.presence_of_element_located((By.XPATH, posts_filter_alt_xpath)))
            except TimeoutException:
                print(f"BŁĄD: Nie znaleziono filtra 'Posty' za pomocą obu XPath.")

        if element_to_click:
            try:
                driver.execute_script("arguments[0].click();", element_to_click)
                print("INFO: JS click wykonany na filtrze 'Posty'")
                time.sleep(2.5)
                try:
                    WebDriverWait(driver, 5).until(EC.visibility_of_element_located((By.XPATH, "//input[@aria-label='Najnowsze posty'][@type='checkbox']")))
                    print("SUKCES: Filtr 'Posty' zastosowany (checkbox 'Najnowsze posty' widoczny).")
                    clicked_successfully = True
                except TimeoutException:
                    if "filter=" in driver.current_url.lower() and "post" in driver.current_url.lower():
                         print(f"SUKCES: Filtr 'Posty' prawdopodobnie zastosowany (URL: {driver.current_url}).")
                         clicked_successfully = True
                    else:
                        print("OSTRZEŻENIE: Po kliknięciu filtra 'Posty', nie potwierdzono jego zastosowania (ani checkbox, ani URL).")
            except StaleElementReferenceException:
                print("BŁĄD: Element filtra 'Posty' stał się nieaktualny przed kliknięciem.")
            except Exception as e_click:
                print(f"BŁĄD ({type(e_click).__name__}) klikania filtra 'Posty': {str(e_click).splitlines()[0]}")

        return clicked_successfully
    except Exception as e:
        print(f"BŁĄD w search_and_filter_facebook: {e}")
        traceback.print_exc()
        return False

def click_latest_posts_checkbox(driver):
    try:
        print("INFO: Próba kliknięcia w checkbox 'Najnowsze posty'...")
        wait = WebDriverWait(driver, 15)
        checkbox_xpath = "//input[@aria-label='Najnowsze posty'][@type='checkbox']"
        checkbox_element = wait.until(EC.presence_of_element_located((By.XPATH, checkbox_xpath)))

        try:
            if checkbox_element.get_attribute("aria-checked") == "true":
                print("INFO: Checkbox 'Najnowsze posty' jest już zaznaczony.")
                return True
        except StaleElementReferenceException:
             print("OSTRZEŻENIE: Element checkboxa stał się nieaktualny przed sprawdzeniem stanu. Próba kliknięcia.")
             checkbox_element = wait.until(EC.presence_of_element_located((By.XPATH, checkbox_xpath)))

        print("INFO: Próba kliknięcia checkboxa za pomocą JavaScript...")
        driver.execute_script("arguments[0].click();", checkbox_element)
        time.sleep(1.5)

        try:
            checkbox_element = wait.until(EC.presence_of_element_located((By.XPATH, checkbox_xpath)))
            if checkbox_element.get_attribute("aria-checked") == "true":
                print("SUKCES: Kliknięto checkbox 'Najnowsze posty' (JS).")
                time.sleep(2)
                return True
            else:
                print("OSTRZEŻENIE: JS click nie zmienił stanu checkboxa. Próba standardowego .click()...")
                checkbox_element = wait.until(EC.element_to_be_clickable((By.XPATH, checkbox_xpath)))
                checkbox_element.click()
                time.sleep(1.5)
                checkbox_element = wait.until(EC.presence_of_element_located((By.XPATH, checkbox_xpath)))
                if checkbox_element.get_attribute("aria-checked") == "true":
                    print("SUKCES (po .click()): Checkbox zaznaczony.")
                    time.sleep(2)
                    return True
                else:
                    print("OSTRZEŻENIE: Checkbox nadal nie zaznaczony po obu próbach kliknięcia.")
                    return False
        except StaleElementReferenceException:
             print("OSTRZEŻENIE: Element checkboxa stał się nieaktualny podczas weryfikacji po kliknięciu.")
             time.sleep(2)
             return True

    except TimeoutException:
        print(f"BŁĄD: Nie znaleziono elementu checkboxa 'Najnowsze posty' w DOM XPath: {checkbox_xpath}")
        return False
    except Exception as e:
        print(f"BŁĄD klikania checkboxa 'Najnowsze posty': {e}")
        traceback.print_exc()
        return False

def normalize_text(text):
    if not isinstance(text, str):
        return ""
    text = text.lower()
    text = ''.join(c for c in unicodedata.normalize('NFD', text) if unicodedata.category(c) != 'Mn')
    text = text.replace('ł', 'l')
    return text

def classify_post_with_gemini(model, post_text):
    default_response = {'category': "INNE", 'subject': None}
    error_response = {'category': "ERROR", 'subject': None}

    if not post_text or len(post_text.strip()) < 10:
        return default_response

    prompt_for_ai = f"""
Przeanalizuj poniższy tekst posta z Facebooka dotyczący korepetycji.
1. Najpierw skategoryzuj intencję posta jako SZUKAM, OFERUJE lub INNE. Skup się na głównej intencji. Posty typu "Szukam korepetytora" to SZUKAM. Posty typu "Udzielę korepetycji" to OFERUJE. Inne posty (np. pytania ogólne, dyskusje, reklamy niezwiązane bezpośrednio z ofertą/szukaniem) to INNE.
2. Jeśli intencja to SZUKAM, dodatkowo określ główny przedmiot korepetycji jako POLSKI, MATEMATYKA, ANGIELSKI lub INNY_PRZEDMIOT. Jeśli wymieniono wiele przedmiotów, wybierz pierwszy lub najważniejszy. Jeśli przedmiot nie jest jasny, nie pasuje do listy lub jest zbyt ogólny (np. "przedmioty ścisłe"), użyj INNY_PRZEDMIOT.

Odpowiedz TYLKO w następującym formacie JSON, bez żadnych dodatkowych wyjaśnień przed lub po:
{{
  "category": "SZUKAM" | "OFERUJE" | "INNE",
  "subject": "POLSKI" | "MATEMATYKA" | "ANGIELSKI" | "INNY_PRZEDMIOT" | null
}}
Jeśli kategoria to OFERUJE lub INNE, subject zawsze powinien być null.

Tekst posta:
---
{post_text}
---

Twoja odpowiedź JSON:
"""

    try:
        safety_settings = [
            {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_MEDIUM_AND_ABOVE"},
            {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_MEDIUM_AND_ABOVE"},
            {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_MEDIUM_AND_ABOVE"},
            {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_MEDIUM_AND_ABOVE"},
        ]
        generation_config = genai.types.GenerationConfig(
            response_mime_type="application/json"
        )

        response = model.generate_content(
            prompt_for_ai,
            generation_config=generation_config,
            safety_settings=safety_settings
        )

        if not response.parts:
            feedback_info = "Brak szczegółów"
            reason = "Nieznany"
            if hasattr(response, 'prompt_feedback'):
                reason = response.prompt_feedback.block_reason.name if response.prompt_feedback.block_reason else "Nieznany"
                feedback_info = f"Prompt Feedback: {response.prompt_feedback}"
            elif hasattr(response, 'candidates') and response.candidates and hasattr(response.candidates[0], 'finish_reason'):
                 reason = response.candidates[0].finish_reason.name
                 if reason == 'SAFETY':
                    feedback_info = f"Safety Ratings: {response.candidates[0].safety_ratings if hasattr(response.candidates[0], 'safety_ratings') else 'N/A'}"

            print(f"  OSTRZEŻENIE: Odpowiedź Gemini zablokowana lub pusta. Powód: {reason}. Info: {feedback_info}")
            return error_response

        import json
        try:
            result = json.loads(response.text)
            category = result.get('category')
            subject = result.get('subject')

            valid_categories = ["SZUKAM", "OFERUJE", "INNE"]
            valid_subjects = ["POLSKI", "MATEMATYKA", "ANGIELSKI", "INNY_PRZEDMIOT", None]

            if category not in valid_categories:
                print(f"  OSTRZEŻENIE: Nieprawidłowa kategoria w odpowiedzi AI: {category}. Używam 'INNE'.")
                category = "INNE"
                subject = None
            elif category != "SZUKAM":
                subject = None
            elif subject not in valid_subjects:
                print(f"  OSTRZEŻENIE: Nieprawidłowy przedmiot w odpowiedzi AI dla SZUKAM: {subject}. Używam 'INNY_PRZEDMIOT'.")
                subject = "INNY_PRZEDMIOT"
            elif category == "SZUKAM" and subject is None:
                 print(f"  OSTRZEŻENIE: Kategoria SZUKAM, ale brak przedmiotu w odpowiedzi AI. Używam 'INNY_PRZEDMIOT'.")
                 subject = "INNY_PRZEDMIOT"


            return {'category': category, 'subject': subject}

        except json.JSONDecodeError as e_json:
            print(f"  BŁĄD: Nie udało się sparsować odpowiedzi JSON od Gemini: {e_json}")
            print(f"  Surowa odpowiedź AI: {response.text}")
            return error_response
        except KeyError as e_key:
            print(f"  BŁĄD: Brakujący klucz w odpowiedzi JSON od Gemini: {e_key}")
            print(f"  Odpowiedź AI: {response.text}")
            return error_response

    except Exception as e:
        print(f"  BŁĄD: Wyjątek podczas wywołania Gemini API: {e}")
        traceback.print_exc()
        return error_response

# --- ZMODYFIKOWANA FUNKCJA try_block_or_hide_post ---
def try_block_or_hide_post(driver, post_container_element, author_name):
    wait_short = WebDriverWait(driver, 7)
    print(f"  INFO: Próba rozbudowanej akcji (zgłoś/ukryj/zablokuj) dla posta od '{author_name}'...")
    action_successful_overall = False
    try:
        three_dots_xpath_options = [
            ".//div[@aria-label='Działania dla tego posta' or @aria-label='Actions for this post']",
            ".//div[@aria-label='Więcej opcji' or @aria-label='More options']",
            ".//div[@aria-label='Menu czynności dotyczących posta']",
            ".//div[@role='button' and @aria-haspopup='menu' and (contains(@aria-label,'opcj') or contains(@aria-label,'action') or contains(@aria-label,'czynności'))][1]"
        ]
        
        three_dots_button = None
        for xpath_option in three_dots_xpath_options:
            try:
                temp_button = post_container_element.find_element(By.XPATH, xpath_option)
                if temp_button:
                    if scroll_to_element_and_wait(driver, temp_button, wait_time=0.5):
                        three_dots_button = wait_short.until(EC.element_to_be_clickable(temp_button))
                        if three_dots_button:
                            print(f"    DEBUG: Znaleziono przycisk 3 kropek za pomocą XPath: {xpath_option}")
                            break 
            except (NoSuchElementException, TimeoutException):
                continue 
        
        if not three_dots_button:
            print(f"  BŁĄD: Nie znaleziono przycisku 'trzech kropek' dla posta od {author_name} za pomocą żadnej z opcji XPath.")
            return False

        driver.execute_script("arguments[0].click();", three_dots_button)
        print("    INFO: Kliknięto 3 kropki.")
        time.sleep(1.5)

        reported_post_successfully = False
        try:
            report_post_xpath = "//div[@role='menuitem']//span[normalize-space(.)='Zgłoś post' or normalize-space(.)='Report post']"
            print(f"    DEBUG: Próba znalezienia opcji 'Zgłoś post' XPath: {report_post_xpath}")
            report_post_button = wait_short.until(EC.element_to_be_clickable((By.XPATH, report_post_xpath)))
            driver.execute_script("arguments[0].click();", report_post_button)
            print("    INFO: Kliknięto 'Zgłoś post'.")
            reported_post_successfully = True
            time.sleep(1.5) 
        except (NoSuchElementException, TimeoutException, StaleElementReferenceException) as e_report:
            print(f"    OSTRZEŻENIE: Nie udało się kliknąć 'Zgłoś post': {type(e_report).__name__}")

        hide_option_clicked = False
        if reported_post_successfully:
            dont_want_to_see_options = [
                "//div[@role='dialog']//div[@role='listitem']//span[normalize-space(.)='Nie chcę tego widzieć']",
                "//div[@role='dialog']//div[@role='button']//span[normalize-space(.)='Nie chcę tego widzieć']",
                "//div[@role='listitem']//span[normalize-space(.)='Nie chcę tego widzieć']",
                "//div[@role='button']//span[normalize-space(.)='Nie chcę tego widzieć']",
                "//div[@role='dialog']//div[@role='listitem']//span[contains(.,'want to see this') and contains(.,'don')]",
                "//div[@role='dialog']//div[@role='button']//span[contains(.,'want to see this') and contains(.,'don')]"
            ]
            for xpath in dont_want_to_see_options:
                try:
                    print(f"    DEBUG: Próba znalezienia 'Nie chcę tego widzieć' XPath: {xpath}")
                    hide_button = wait_short.until(EC.element_to_be_clickable((By.XPATH, xpath)))
                    driver.execute_script("arguments[0].click();", hide_button)
                    print("    INFO: Kliknięto 'Nie chcę tego widzieć' (po zgłoszeniu).")
                    hide_option_clicked = True
                    action_successful_overall = True
                    time.sleep(1.5)
                    break
                except (NoSuchElementException, TimeoutException, StaleElementReferenceException):
                    continue
            
            if not hide_option_clicked:
                 print("    OSTRZEŻENIE: Nie znaleziono opcji 'Nie chcę tego widzieć' po kliknięciu 'Zgłoś post'.")
        
        if not hide_option_clicked:
            print("    INFO: Próba standardowego ukrycia posta (ponieważ 'Zgłoś post' nie doprowadziło do ukrycia lub nie było kroku 'Nie chcę tego widzieć').")
            try:
                driver.find_element(By.TAG_NAME, "body").send_keys(Keys.ESCAPE) 
                time.sleep(0.5)
            except: pass

            hide_options_xpaths_fallback = [
                "//div[@role='menuitem']//span[normalize-space(.)='Ukryj post']",
                "//div[@role='menuitem']//span[normalize-space(.)='Hide post']",
                "//div[@role='menuitem']//span[contains(.,'want to see this') and contains(.,'don')]"
            ]
            for xpath in hide_options_xpaths_fallback:
                try:
                    print(f"    DEBUG: Próba standardowego ukrycia z XPath: {xpath}")
                    hide_button = wait_short.until(EC.element_to_be_clickable((By.XPATH, xpath)))
                    driver.execute_script("arguments[0].click();", hide_button)
                    print("    INFO: Kliknięto standardową opcję ukrywania.")
                    hide_option_clicked = True
                    action_successful_overall = True
                    time.sleep(1.5)
                    break
                except Exception as e_std_hide:
                    print(f"    DEBUG: Niepowodzenie dla XPath (standardowe ukrywanie): {xpath} - {e_std_hide}")
                    continue

        if not hide_option_clicked:
            print("    OSTRZEŻENIE: Nie udało się ukryć posta żadną z metod.")
            try: driver.find_element(By.TAG_NAME, "body").send_keys(Keys.ESCAPE); time.sleep(0.5)
            except: pass
            return

        if hide_option_clicked:
            blocked_profile = False
            try:
                block_xpath = f"//div[@role='button' or @role='menuitem']//span[starts-with(normalize-space(),'Zablokuj') and contains(normalize-space(),'{author_name}')]"
                block_button = wait_short.until(EC.element_to_be_clickable((By.XPATH, block_xpath)))
                driver.execute_script("arguments[0].click();", block_button)
                print(f"      INFO: Kliknięto 'Zablokuj profil {author_name}'."); time.sleep(1.5)
                confirm_block_xpath = "//div[@aria-label='Zablokuj' or @aria-label='Block'][@role='button']//span[normalize-space()='Zablokuj' or normalize-space()='Block']"
                confirm_block_button = wait_short.until(EC.element_to_be_clickable((By.XPATH, confirm_block_xpath)))
                driver.execute_script("arguments[0].click();", confirm_block_button)
                print("      INFO: Kliknięto pierwsze potwierdzenie 'Zablokuj'."); time.sleep(1.5)
                try:
                    confirm_block_xpath_2 = "//div[@aria-label='Potwierdź' or @aria-label='Confirm'][@role='button']//span[normalize-space()='Potwierdź' or normalize-space()='Confirm']"
                    confirm_block_button_2 = wait_short.until(EC.element_to_be_clickable((By.XPATH, confirm_block_xpath_2)))
                    driver.execute_script("arguments[0].click();", confirm_block_button_2)
                    print("      INFO: Kliknięto drugie potwierdzenie 'Potwierdź' dla blokady."); time.sleep(1.5)
                except (TimeoutException, NoSuchElementException): print("      INFO: Drugie potwierdzenie blokady nie było wymagane lub nie znaleziono.")
                blocked_profile = True; action_successful_overall = True
                print(f"    SUKCES: Zablokowano profil '{author_name}'.")
            except (NoSuchElementException, TimeoutException, StaleElementReferenceException):
                print(f"    INFO: Opcja 'Zablokuj profil {author_name}' nie była dostępna po ukryciu posta.")

            if not blocked_profile:
                hid_all_from_author = False
                try:
                    hide_all_xpath = f"//div[@role='button' or @role='menuitem']//span[starts-with(normalize-space(),'Ukryj wszystko od') or starts-with(normalize-space(),'Hide all from')][contains(normalize-space(),'{author_name}')]"
                    hide_all_button = wait_short.until(EC.element_to_be_clickable((By.XPATH, hide_all_xpath)))
                    driver.execute_script("arguments[0].click();", hide_all_button)
                    print(f"      INFO: Kliknięto 'Ukryj wszystko od {author_name}'."); time.sleep(1.5)
                    confirm_hide_all_xpath = "//div[@aria-label='Potwierdź' or @aria-label='Confirm'][@role='button']//span[normalize-space()='Potwierdź' or normalize-space()='Confirm']"
                    confirm_hide_all_button = wait_short.until(EC.element_to_be_clickable((By.XPATH, confirm_hide_all_xpath)))
                    driver.execute_script("arguments[0].click();", confirm_hide_all_button)
                    print("      INFO: Kliknięto potwierdzenie 'Ukryj wszystko od'."); time.sleep(1.5)
                    hid_all_from_author = True; action_successful_overall = True
                    print(f"    SUKCES: Ukryto wszystko od '{author_name}'.")
                except (NoSuchElementException, TimeoutException, StaleElementReferenceException):
                    print(f"    INFO: Opcja 'Ukryj wszystko od {author_name}' nie była dostępna po ukryciu posta.")

        closed_properly = False
        try:
            done_button_xpath = "//div[@aria-label='Gotowe' or @aria-label='Done'][@role='button']"
            done_button = wait_short.until(EC.element_to_be_clickable((By.XPATH, done_button_xpath)))
            driver.execute_script("arguments[0].click();", done_button)
            print("    INFO: Kliknięto 'Gotowe'."); time.sleep(1)
            closed_properly = True
        except (NoSuchElementException, TimeoutException, StaleElementReferenceException):
            print("    INFO: Przycisk 'Gotowe' nie znaleziony (prawdopodobnie niepotrzebny).")
            closed_properly = True

        if action_successful_overall:
            print(f"    INFO: Sekwencja ukrywania/blokowania dla '{author_name}' zakończona.")
        else:
             print(f"    OSTRZEŻENIE: Nie udało się wykonać żadnej akcji ukrywania/blokowania dla '{author_name}'.")

    except (NoSuchElementException, TimeoutException, StaleElementReferenceException) as e_menu:
        print(f"  OSTRZEŻENIE: Nie można było otworzyć menu posta lub wykonać początkowej akcji: {type(e_menu).__name__} - {e_menu}")
        try:
            driver.find_element(By.TAG_NAME, "body").send_keys(Keys.ESCAPE)
            time.sleep(0.5)
            print("    INFO: Próba zamknięcia menu przez ESCAPE po błędzie.")
        except:
            pass
    except Exception as e:
        print(f"  BŁĄD podczas próby rozbudowanej akcji ukrywania/blokowania: {e}")
        traceback.print_exc()
        try:
            driver.find_element(By.TAG_NAME, "body").send_keys(Keys.ESCAPE)
            time.sleep(0.5)
        except:
            pass
# --- KONIEC FUNKCJI DO BLOKOWANIA ---

# --- Funkcja do aktualizacji Google Sheet ---
def update_google_sheet(date_str, target_row_label, profile_name_for_log, sheet_id, sheet_name="Arkusz3"):
    """
    Aktualizuje (inkrementuje) wartość w komórce na przecięciu kolumny z dzisiejszą datą
    i wiersza z określoną etykietą statusu (np. "Odrzucone", "Oczekujace", "Dodane").

    Args:
        date_str (str): Dzisiejsza data w formacie "YYYY-MM-DD" (lub jakikolwiek format używany jako nagłówek kolumny).
        target_row_label (str): Etykieta wiersza do znalezienia w pierwszej kolumnie (np. "Odrzucone", "Oczekujace", "Dodane").
        profile_name_for_log (str): Nazwa profilu, używana tylko do logowania.
        sheet_id (str): ID Arkusza Google.
        sheet_name (str): Nazwa arkusza (zakładki).
    """
    if not GSPREAD_AVAILABLE:
        print(f"INFO [{profile_name_for_log}]: gspread niedostępny, pomijanie aktualizacji Google Sheet.")
        return

    try:
        scopes = ['https://www.googleapis.com/auth/spreadsheets']
        creds = Credentials.from_service_account_file(GOOGLE_SHEET_KEY_FILE, scopes=scopes)
        gc = gspread.authorize(creds)
        
        spreadsheet = gc.open_by_key(sheet_id)
        worksheet = spreadsheet.worksheet(sheet_name)
        print(f"INFO [GoogleSheet - {profile_name_for_log}]: Pomyślnie połączono z arkuszem '{sheet_name}'")

        # Krok 1: Znajdź lub utwórz kolumnę dla dzisiejszej daty
        headers = worksheet.row_values(1) # Zakładamy, że daty są w pierwszym wierszu
        date_col_index = None
        for i, header_date in enumerate(headers):
            # Dopasuj format daty, jeśli w arkuszu jest inny niż YYYY-MM-DD
            # Np. jeśli w arkuszu jest "D.M.YYYY" (5.5.2025), a date_str to "2025-05-05"
            # trzeba by przekonwertować jedno na format drugiego dla porównania.
            # Na razie zakładamy, że formaty są zgodne lub date_str jest już w formacie z arkusza.
            if header_date == date_str:
                date_col_index = i + 1 
                break
        
        if date_col_index is None:
            # Jeśli nie ma kolumny z dzisiejszą datą, dodaj ją
            # UWAGA: To może prowadzić do błędu "exceeds grid limits", jeśli arkusz ma stały limit kolumn.
            # Jeśli arkusz ma być ograniczony, tę logikę trzeba by zmienić.
            next_col_letter = gspread.utils.rowcol_to_a1(1, len(headers) + 1)[:-1] # Pobierz literę następnej kolumny
            print(f"INFO [GoogleSheet - {profile_name_for_log}]: Kolumna dla daty '{date_str}' nie istnieje. Próba dodania w kolumnie {next_col_letter}...")
            try:
                worksheet.update_cell(1, len(headers) + 1, date_str)
                date_col_index = len(headers) + 1 # Po dodaniu, indeks to nowa liczba kolumn
                print(f"INFO [GoogleSheet - {profile_name_for_log}]: Dodano nową kolumnę z datą: {date_str} w kolumnie {date_col_index}")
            except Exception as e_add_col:
                print(f"BŁĄD [GoogleSheet - {profile_name_for_log}]: Nie udało się dodać kolumny dla daty '{date_str}': {e_add_col}")
                print(f"    Sprawdź limity kolumn w arkuszu. Obecnie {len(headers)} kolumn.")
                return # Nie można kontynuować bez kolumny daty

        # Krok 2: Znajdź wiersz na podstawie `target_row_label`
        first_column_values = worksheet.col_values(1) # Zakładamy, że etykiety wierszy są w kolumnie A
        row_index = None
        try:
            # Szukamy dokładnego dopasowania etykiety
            row_index = first_column_values.index(target_row_label) + 1
        except ValueError: 
            # Jeśli nie ma wiersza z taką etykietą, można go dodać (opcjonalnie)
            # lub zgłosić błąd/pominąć, jeśli zakładamy, że wiersze już istnieją.
            # Na podstawie obrazka, wiersze "Odrzucone", "Oczekujace", "Dodane" już istnieją.
            print(f"BŁĄD [GoogleSheet - {profile_name_for_log}]: Nie znaleziono wiersza z etykietą '{target_row_label}' w pierwszej kolumnie.")
            print(f"    Upewnij się, że wiersze 'Odrzucone', 'Oczekujace', 'Dodane' istnieją w kolumnie A.")
            return # Nie można kontynuować bez wiersza

        # Krok 3: Zaktualizuj komórkę
        if row_index and date_col_index:
            current_value_str = worksheet.cell(row_index, date_col_index).value
            current_value = 0
            if current_value_str and isinstance(current_value_str, str) and current_value_str.isdigit():
                current_value = int(current_value_str)
            elif isinstance(current_value_str, (int, float)): # Jeśli już jest liczbą
                current_value = int(current_value_str)
            
            new_value = current_value + 1
            worksheet.update_cell(row_index, date_col_index, new_value)
            
            print(f"INFO [GoogleSheet - {profile_name_for_log}]: Zaktualizowano: Etykieta wiersza '{target_row_label}', Data {date_str} (kol: {date_col_index}, wiersz: {row_index}). Nowa wartość: {new_value}")
        else:
            if not row_index:
                print(f"BŁĄD [GoogleSheet - {profile_name_for_log}]: Nie udało się ustalić wiersza dla: {target_row_label}")
            if not date_col_index: # Powinno być obsłużone wcześniej, ale dla pewności
                 print(f"BŁĄD [GoogleSheet - {profile_name_for_log}]: Nie udało się ustalić kolumny dla daty: {date_str}")

    except gspread.exceptions.APIError as e_gspread_api:
        print(f"BŁĄD API Google Sheets [{profile_name_for_log}]: {e_gspread_api}")
        if "exceeds grid limits" in str(e_gspread_api).lower():
            print(f"    >>> Błąd limitu siatki! Sprawdź, czy arkusz '{sheet_name}' nie osiągnął maksymalnej liczby kolumn/wierszy. <<<")
        elif "insufficient authentication scopes" in str(e_gspread_api).lower():
            print("    >>> UPEWNIJ SIĘ, ŻE KONTO SERWISOWE MA UPRAWNIENIA DO EDYCJI ARKUSZA I POPRAWNE ZAKRESY (SCOPES) <<<")
    except Exception as e_sheet:
        print(f"BŁĄD [{profile_name_for_log}]: Nie udało się zaktualizować Google Sheet: {e_sheet}")
        traceback.print_exc()


# --- ZMODYFIKOWANA Funkcja scroll_and_extract_post_data ---

def scroll_and_extract_post_data(driver, model, profile_name, config, profile_specialization):
    print(f"\nINFO [{profile_name}]: Rozpoczynanie scrollowania (specjalizacja: {profile_specialization})...")
    processed_post_keys = load_processed_keys(profile_name)
    last_height = driver.execute_script("return document.body.scrollHeight")
    wait = WebDriverWait(driver, 7)
    short_wait_for_popups = WebDriverWait(driver, 4)

    post_container_xpath = "//div[contains(@data-pagelet, 'FeedUnit_') or @role='article']"
    author_name_xpath = ".//strong | .//h2//span | .//h3//span | .//h4//span | .//a[@role='link' and contains(@href,'facebook.com/')][not(contains(@href,'/reactions/'))][not(contains(@href,'/comment/'))]//span[normalize-space()]"
    author_filter_keywords = ['sponsored', 'sponsorowane', 'polubiono', 'liked', 'skomentowano', 'commented', 'udostępniono', 'shared', 'obserwuj', 'follow', 'dołącz', 'join', 'wiadomość', 'message', 'więcej', 'more', 'odpowiedz', 'reply', 'edytowano', 'edited']
    content_xpath_options = [
        ".//div[@data-ad-preview='message']", ".//div[@data-ad-preview='headline']",
        ".//div[@dir='auto' and normalize-space() and string-length(normalize-space(.)) > 15]",
        ".//span[contains(@class,'text_exposed_root')]/parent::div", ".//div[contains(@class,'text_exposed_root')]"
    ]
    see_more_button_xpath = ".//div[@role='button' and (contains(.,'Zobacz więcej') or contains(.,'See more'))]"
    
    generalized_like_button_xpath = "//div[@role='button' and (starts-with(@aria-label, 'Lubię to!') or starts-with(@aria-label, 'Like') or starts-with(@aria-label, 'Poleć') or starts-with(@aria-label, 'Recommend'))][.//i[@data-visualcompletion='css-img' and contains(@style, 'olX2yf1iinG.png') and contains(@style, 'background-position: 0px -798px')]]"
    like_react_button_xpath_options = [generalized_like_button_xpath]

    comment_button_xpath = ".//div[@role='button'][@aria-label='Skomentuj' or @aria-label='Comment']"
    comment_button_alt_xpath = ".//div[@aria-label='Dodaj komentarz'][@role='button']"
    comment_dialog_textbox_xpath = "//div[@role='dialog']//div[@role='textbox']"
    comment_textbox_xpath = "//div[@role='textbox'][not(ancestor::div[@role='dialog'])]"
    send_comment_button_xpath = "//div[@aria-label='Opublikuj' or @aria-label='Post'][@role='button']"
    close_dialog_button_xpath = "//div[@role='dialog']//div[@aria-label='Zamknij' or @aria-label='Close'][@role='button']"
    comment_text_to_write = "Udzielamy korepetycji z tego przedmiotu, zapraszam do kontaktu na pv :)"

    scroll_cycle_count = 0
    short_wait_count = 0
    short_wait_interval = 60
    long_wait_interval = 180

    while True:
        print(f"\n--- [{profile_name}] Cykl scrollowania: {scroll_cycle_count + 1} (Krótkie oczekiwania: {short_wait_count}) ---")
        all_posts_data_this_scroll_session = []
        processed_post_keys_this_cycle = set()
        reached_end_of_scroll_this_session = False
        scroll_attempts_this_session = 0
        max_scroll_attempts_per_session = 15

        try:
            while not reached_end_of_scroll_this_session and scroll_attempts_this_session < max_scroll_attempts_per_session:
                new_posts_found_this_single_scroll = 0
                post_containers_on_page = []
                try:
                    post_containers_on_page = driver.find_elements(By.XPATH, post_container_xpath)
                    if not post_containers_on_page and scroll_attempts_this_session > 0:
                        print(f"INFO [{profile_name}]: Brak kontenerów postów.")
                        reached_end_of_scroll_this_session = True
                        continue
                    
                    current_keys_in_view = set()
                    for i, post_container_loop_var in enumerate(post_containers_on_page):
                        post_container = post_container_loop_var 
                        current_post_data = {}
                        post_key = None
                        author_name = "Nieznany Autor"
                        post_content_text = ""
                        try:
                            if not scroll_to_element_and_wait(driver, post_container, wait_time=0.3, block_position='nearest'):
                                print(f"  OSTRZEŻENIE [{profile_name}]: Scroll do posta {i+1} nieudany. Pomijanie.")
                                continue
                            
                            try:
                                pagelet_id = post_container.get_attribute('data-pagelet')
                                location_y = post_container.location['y']
                                post_key_base = f"{pagelet_id}_{location_y}" if pagelet_id else f"loc_{location_y}"
                            except Exception:
                                post_key_base = f"index_{i}"

                            try:
                                author_elements = post_container.find_elements(By.XPATH, author_name_xpath)
                                potential_authors = []
                                for el in author_elements:
                                    try:
                                        text = el.text.strip()
                                        if text and 2 < len(text) < 50 and not any(keyword in text.lower() for keyword in author_filter_keywords) and not text.isdigit() and el.is_displayed():
                                            potential_authors.append(text)
                                    except StaleElementReferenceException:
                                        continue
                                if potential_authors:
                                    author_name = potential_authors[0]
                            except (NoSuchElementException, StaleElementReferenceException):
                                pass
                            except Exception as e_author:
                                print(f"  OSTRZEŻENIE [{profile_name}]: Ekstrakcja autora: {e_author}")
                            current_post_data['author'] = author_name

                            try:
                                see_more_button = post_container.find_element(By.XPATH, see_more_button_xpath)
                                if see_more_button.is_displayed():
                                    driver.execute_script("arguments[0].click();", see_more_button)
                                    time.sleep(1)
                                    print(f"    INFO [{profile_name}]: Kliknięto 'Zobacz więcej' ({author_name}).")
                                    post_containers_after_see_more = driver.find_elements(By.XPATH, post_container_xpath)
                                    if i < len(post_containers_after_see_more):
                                        post_container = post_containers_after_see_more[i]
                                    else:
                                        print(f"    OSTRZEŻENIE [{profile_name}]: Nie można ponownie zlokalizować posta po 'Zobacz więcej'.")
                                        continue
                            except (NoSuchElementException, TimeoutException, StaleElementReferenceException):
                                pass
                            except Exception as e_sm:
                                print(f"  OSTRZEŻENIE [{profile_name}]: Błąd 'Zobacz więcej': {type(e_sm).__name__}")
                            
                            content_found = False
                            for content_xpath in content_xpath_options:
                                try:
                                    content_elements = post_container.find_elements(By.XPATH, content_xpath)
                                    combined_text = "".join(el.text.strip() + "\n" for el in content_elements if el.is_displayed() and el.text.strip())
                                    post_content_text = combined_text.strip()
                                    if post_content_text:
                                        content_found = True
                                        break
                                except (NoSuchElementException, StaleElementReferenceException):
                                    continue
                            if not content_found:
                                try:
                                    post_content_text = post_container.text.strip()
                                    if post_content_text.startswith(author_name):
                                        post_content_text = post_content_text[len(author_name):].strip()
                                    for btn_txt in ["Lubię to!", "Komentarz", "Udostępnij", "Like", "Comment", "Share"]:
                                        if post_content_text.endswith(btn_txt):
                                            post_content_text = post_content_text[:-len(btn_txt)].strip()
                                except StaleElementReferenceException:
                                    post_content_text = "[Błąd - Stale (Fallback)]"
                                except Exception as e_fb_txt:
                                    post_content_text = f"[Błąd - Fallback Text: {e_fb_txt}]"
                            if not post_content_text:
                                post_content_text = "[Brak treści]"
                            current_post_data['content'] = post_content_text
                            post_key_content_snippet = normalize_text(post_content_text[:60].replace("\n", " "))
                            post_key = f"{normalize_text(author_name)}_{post_key_content_snippet}"
                            current_keys_in_view.add(post_key)
                            if post_key in processed_post_keys:
                                continue

                            is_valid_post = False
                            is_offering_to_block = False
                            classified_subject = None
                            if post_content_text and not post_content_text.startswith(("[Błąd", "[Brak treści")) and len(post_content_text) > 15:
                                classification = classify_post_with_gemini(model, post_content_text)
                                category = classification['category']
                                classified_subject = classification['subject']
                                print(f"    AI: {category}" + (f", Przedmiot: {classified_subject}" if classified_subject else "") + f" ({author_name})")
                                if category == "SZUKAM":
                                    is_valid_post = True
                                    current_post_data['subject'] = classified_subject
                                elif category == "OFERUJE" and not any(kw in author_name.lower() for kw in ["spotted", "ogloszenia", "ogłoszenia", "korepetycje", "nauka", "szkoła", "centrum", "instytut"]):
                                    is_offering_to_block = True
                                elif category == "ERROR":
                                    print(f"  OSTRZEŻENIE [{profile_name}]: Błąd klasyfikacji AI ({author_name}).")
                            
                            if is_offering_to_block:
                                print(f"  AKCJA [{profile_name}]: Blokowanie OFERUJE od '{author_name}'.")
                                try_block_or_hide_post(driver, post_container, author_name)
                                processed_post_keys.add(post_key)
                                processed_post_keys_this_cycle.add(post_key)
                                new_posts_found_this_single_scroll += 1
                                continue
                            
                            if is_valid_post:
                                print(f"  AKCJA [{profile_name}]: Przetwarzanie SZUKAM od '{author_name}' (AI: {classified_subject}).")
                                liked_successfully = False
                                commented_successfully = False
                                proceed = True
                                try:
                                    like_btn = None
                                    for xpath in like_react_button_xpath_options:
                                        try:
                                            like_btn_candidate = post_container.find_element(By.XPATH, xpath)
                                            if like_btn_candidate.is_displayed() and like_btn_candidate.is_enabled():
                                                like_btn = like_btn_candidate
                                                print(f"    DEBUG: Znaleziono przycisk polubienia: {xpath}")
                                                break
                                        except (NoSuchElementException, StaleElementReferenceException):
                                            continue
                                    if like_btn:
                                        is_liked = False
                                        if like_btn.get_attribute("aria-pressed") == "true": is_liked = True
                                        elif "var(--reaction-like" in (like_btn.get_attribute("style") or ""): is_liked = True
                                        elif any(txt in (like_btn.get_attribute("aria-label") or "") for txt in ["Lubisz to", "Super", "Ha ha", "Wow", "Przykro mi", "Wrr"]): is_liked = True
                                        
                                        if is_liked:
                                            print(f"    INFO: Post '{author_name}' już polubiony/zareagowano. Pomijam.")
                                            proceed = False
                                        else:
                                            if scroll_to_element_and_wait(driver, like_btn):
                                                driver.execute_script("arguments[0].click();", like_btn)
                                                print(f"    INFO: Kliknięto polubienie ({author_name}).")
                                                liked_successfully = True
                                                time.sleep(0.7)
                                            else:
                                                print(f"    OSTRZEŻENIE: Nie udało się zescrollować do polubienia.")
                                                proceed = False
                                    else:
                                        print(f"    OSTRZEŻENIE: Nie znaleziono przycisku polubienia za pomocą: {generalized_like_button_xpath}")
                                        proceed = False
                                except Exception as e_like:
                                    print(f"  OSTRZEŻENIE: Błąd polubienia: {e_like}")
                                    proceed = False

                                if proceed and classified_subject != profile_specialization:
                                    print(f"    INFO: Pomijanie komentowania. AI przedmiot ('{classified_subject}') != specjalizacja profilu ('{profile_specialization}').")
                                    proceed = False
                                
                                if proceed:
                                    try:
                                        comment_btn = None
                                        try: comment_btn = post_container.find_element(By.XPATH, comment_button_xpath)
                                        except (NoSuchElementException, StaleElementReferenceException):
                                            try: comment_btn = post_container.find_element(By.XPATH, comment_button_alt_xpath)
                                            except (NoSuchElementException, StaleElementReferenceException): print(f"    OSTRZEŻENIE: Nie znaleziono przycisku 'Skomentuj'.")

                                        if comment_btn and comment_btn.is_displayed() and comment_btn.is_enabled():
                                            if scroll_to_element_and_wait(driver, comment_btn):
                                                driver.execute_script("arguments[0].click();", comment_btn); print(f"    INFO: Kliknięto 'Skomentuj'."); time.sleep(1.5)
                                                comment_field = None; send_btn_xpath = None
                                                try:
                                                    comment_field = wait.until(EC.visibility_of_element_located((By.XPATH, comment_dialog_textbox_xpath)))
                                                    send_btn_xpath = "//div[@role='dialog']" + send_comment_button_xpath
                                                except TimeoutException:
                                                    try:
                                                        comment_field = wait.until(EC.visibility_of_element_located((By.XPATH, comment_textbox_xpath)))
                                                        send_btn_xpath = send_comment_button_xpath
                                                    except TimeoutException: print(f"    OSTRZEŻENIE: Nie znaleziono pola komentarza.")
                                                
                                                if comment_field and send_btn_xpath:
                                                    driver.execute_script("arguments[0].click();", comment_field); time.sleep(0.3)
                                                    comment_field.send_keys(comment_text_to_write); print(f"    INFO: Wpisano komentarz."); time.sleep(0.5)
                                                    
                                                    comment_sent_method = None
                                                    try:
                                                        send_button = wait.until(EC.element_to_be_clickable((By.XPATH, send_btn_xpath)))
                                                        driver.execute_script("arguments[0].click();", send_button); print(f"    INFO: Wysłano komentarz (przycisk)."); commented_successfully = True; comment_sent_method = "button"
                                                    except TimeoutException:
                                                        print(f"    OSTRZEŻENIE: Nie znaleziono przycisku wysłania. Próba ENTER.")
                                                        try:
                                                            comment_field.send_keys(Keys.RETURN); print(f"    INFO: Wysłano komentarz (Enter)."); commented_successfully = True; comment_sent_method = "enter"
                                                        except Exception as e_enter: print(f"    BŁĄD: Wysyłanie przez ENTER: {e_enter}")
                                                    except Exception as e_send: print(f"    BŁĄD: Wysyłanie komentarza: {e_send}")

                                                    if commented_successfully:
                                                        print(f"    INFO [{profile_name}]: Komentarz wysłany ({comment_sent_method}). Oczekiwanie 10 sekund na ustalenie statusu...")
                                                        time.sleep(10)

                                                        if GSPREAD_AVAILABLE:
                                                            status_for_sheet = "ERROR_CHECKING"
                                                            try:
                                                                rejected_xpath = ".//div[starts-with(@aria-label, 'Odrzucono') or starts-with(@aria-label, 'Rejected')]"
                                                                pending_xpath = ".//div[starts-with(@aria-label, 'Oczekujący') or starts-with(@aria-label, 'Pending')]"
                                                                is_rejected = False; is_pending = False
                                                                
                                                                container_to_check_status = post_container
                                                                try: _ = container_to_check_status.is_displayed()
                                                                except StaleElementReferenceException:
                                                                    print(f"    OSTRZEŻENIE: Kontener posta nieaktualny przed sprawdzaniem statusu komentarza.")
                                                                
                                                                try:
                                                                    if any(el.is_displayed() for el in container_to_check_status.find_elements(By.XPATH, rejected_xpath)):
                                                                        status_for_sheet = "REJECTED"; is_rejected = True; print(f"    STATUS: ODRZUCONO.")
                                                                except (NoSuchElementException, StaleElementReferenceException): pass
                                                                except Exception as e_rej: print(f"    BŁĄD szukania 'Odrzucono': {e_rej}")

                                                                if not is_rejected:
                                                                    try:
                                                                        if any(el.is_displayed() for el in container_to_check_status.find_elements(By.XPATH, pending_xpath)):
                                                                            status_for_sheet = "PENDING"; is_pending = True; print(f"    STATUS: OCZEKUJĄCY.")
                                                                    except (NoSuchElementException, StaleElementReferenceException): pass
                                                                    except Exception as e_pend: print(f"    BŁĄD szukania 'Oczekujący': {e_pend}")
                                                                
                                                                if not is_rejected and not is_pending:
                                                                    status_for_sheet = "ACCEPTED"; print(f"    STATUS: ZAAKCEPTOWANY (domyślnie).")
                                                            except Exception as e_stat: print(f"    BŁĄD sprawdzania statusu: {e_stat}")
                                                            
                                                            day_str = str(datetime.now().day)
                                                            month_str = str(datetime.now().month)
                                                            year_str = str(datetime.now().year)
                                                            today_date_for_sheet_column = f"{day_str}.{month_str}.{year_str}"

                                                            target_row_for_sheet = None
                                                            if status_for_sheet == "REJECTED": target_row_for_sheet = "Odrzucone"
                                                            elif status_for_sheet == "PENDING": target_row_for_sheet = "Oczekujace"
                                                            elif status_for_sheet == "ACCEPTED": target_row_for_sheet = "Dodane"
                                                            
                                                            if target_row_for_sheet:
                                                                update_google_sheet(
                                                                    date_str=today_date_for_sheet_column,
                                                                    target_row_label=target_row_for_sheet,
                                                                    profile_name_for_log=profile_name, 
                                                                    sheet_id=GOOGLE_SHEET_ID,
                                                                    sheet_name=GOOGLE_SHEET_WORKSHEET_NAME
                                                                )
                                                            else: print(f"    OSTRZEŻENIE [{profile_name}]: Nieznany status '{status_for_sheet}' do mapowania na wiersz.")
                                                        
                                                        print(f"    INFO [{profile_name}]: Próba zamknięcia okna komentarza...")
                                                        closed_comment_window = False
                                                        try:
                                                            close_btn = short_wait_for_popups.until(EC.element_to_be_clickable((By.XPATH, close_dialog_button_xpath)))
                                                            driver.execute_script("arguments[0].click();", close_btn); print(f"    INFO [{profile_name}]: Kliknięto 'Zamknij'."); closed_comment_window = True; time.sleep(0.5)
                                                        except (TimeoutException, NoSuchElementException, StaleElementReferenceException):
                                                            print(f"    INFO [{profile_name}]: Nie kliknięto 'Zamknij'. Próba ESCAPE...");
                                                            try: driver.find_element(By.TAG_NAME, "body").send_keys(Keys.ESCAPE); print(f"    INFO [{profile_name}]: Naciśnięto ESCAPE."); closed_comment_window = True; time.sleep(1.0)
                                                            except Exception as e_escape: print(f"    OSTRZEŻENIE [{profile_name}]: Błąd ESCAPE: {e_escape}")
                                                        if not closed_comment_window: print(f"    OSTRZEŻENIE [{profile_name}]: Nie potwierdzono zamknięcia okna komentarza.")
                                                elif comment_field: print(f"    OSTRZEŻENIE [{profile_name}]: Znaleziono pole, ale nie przycisk wysłania.")
                                            else: print(f"    OSTRZEŻENIE [{profile_name}]: Nie zescrollowano do 'Skomentuj'.")
                                        elif comment_btn: print(f"    OSTRZEŻENIE [{profile_name}]: 'Komentarz' znaleziony, ale nie widoczny/klikalny.")
                                        if not comment_field and comment_btn : # type: ignore
                                            print(f"    INFO [{profile_name}]: Kliknięto 'Skomentuj', ale pole nie pojawiło się. Próba ESCAPE.");
                                            try: driver.find_element(By.TAG_NAME, "body").send_keys(Keys.ESCAPE); time.sleep(0.5)
                                            except: pass
                                    except Exception as e_comment_outer:
                                        print(f"  OSTRZEŻENIE [{profile_name}]: Ogólny błąd komentarza: {e_comment_outer}")
                                        traceback.print_exc()

                                processed_post_keys.add(post_key)
                                processed_post_keys_this_cycle.add(post_key)
                                if proceed:
                                    all_posts_data_this_scroll_session.append(current_post_data)
                                    new_posts_found_this_single_scroll += 1
                                    try:
                                        _ = post_container.is_displayed()
                                        print(f"  Post [{len(all_posts_data_this_scroll_session)}] dodany ({profile_name}): Autor: {current_post_data['author']}" + (f", Przedmiot: {current_post_data.get('subject')}" if current_post_data.get('subject') else ""))
                                    except StaleElementReferenceException:
                                        print(f"  OSTRZEŻENIE: Post nieaktualny po przetworzeniu.")
                        except StaleElementReferenceException:
                            print(f"  OSTRZEŻENIE: Kontener posta {i+1} nieaktualny. Pomijanie.")
                            if post_key and post_key in current_keys_in_view: current_keys_in_view.remove(post_key)
                            continue
                        except Exception as e_post_outer:
                            print(f"  BŁĄD przetwarzania posta {i+1}: {type(e_post_outer).__name__} - {e_post_outer}")
                            traceback.print_exc()
                except Exception as e_inner_loop_exc:
                    print(f"BŁĄD w pętli postów: {e_inner_loop_exc}")
                    traceback.print_exc()

                if new_posts_found_this_single_scroll == 0:
                    scroll_attempts_this_session += 1
                    print(f"INFO [{profile_name}]: Brak nowych postów (próba {scroll_attempts_this_session}/{max_scroll_attempts_per_session}).")
                else:
                    print(f"INFO [{profile_name}]: Znaleziono {new_posts_found_this_single_scroll} nowych postów.")
                    scroll_attempts_this_session = 0
                
                if scroll_attempts_this_session >= max_scroll_attempts_per_session:
                    print(f"INFO [{profile_name}]: Limit prób scrollowania.")
                    reached_end_of_scroll_this_session = True
                
                if not reached_end_of_scroll_this_session:
                    print(f"INFO [{profile_name}]: Scrollowanie...")
                    driver.execute_script("window.scrollBy(0, window.innerHeight * 1.5);")
                    time.sleep(2)
                    new_height = driver.execute_script("return document.body.scrollHeight")
                    if new_height == last_height and scroll_attempts_this_session > 0:
                        print(f"INFO [{profile_name}]: Wysokość strony bez zmian.")
                    last_height = new_height
            
            scroll_cycle_count += 1
            if all_posts_data_this_scroll_session:
                print(f"--- [{profile_name}] Koniec sesji. Zebrano {len(all_posts_data_this_scroll_session)} postów SZUKAM. ---")
            if processed_post_keys_this_cycle:
                print(f"INFO [{profile_name}]: Dodano {len(processed_post_keys_this_cycle)} kluczy. Zapisywanie {len(processed_post_keys)}...")
                save_processed_keys(processed_post_keys, profile_name)
            else:
                print(f"INFO [{profile_name}]: Brak nowych kluczy (łącznie {len(processed_post_keys)}).")

            if scroll_cycle_count % 9 == 0:
                print(f"--- [{profile_name}] {scroll_cycle_count} cykli. Odświeżanie i długie czekanie. ---")
                short_wait_count = 0
                current_url_before_refresh = driver.current_url
                driver.refresh()
                print(f"INFO [{profile_name}]: Strona odświeżona. Czekanie {long_wait_interval}s...")
                time.sleep(long_wait_interval)
                if "/search/" in current_url_before_refresh:
                    print(f"INFO [{profile_name}]: Przywracanie URL: {current_url_before_refresh}")
                    driver.get(current_url_before_refresh)
                    time.sleep(3)
                print(f"INFO [{profile_name}]: Odświeżono, kontynuacja.")
            elif scroll_cycle_count % 3 == 0:
                short_wait_count += 1
                print(f"--- [{profile_name}] {scroll_cycle_count} cykli. Czekanie {short_wait_interval}s (krótkie {short_wait_count}). ---")
                time.sleep(short_wait_interval)
                try:
                    print(f"    INFO [{profile_name}]: Próba ESCAPE...")
                    driver.find_element(By.TAG_NAME, "body").send_keys(Keys.ESCAPE)
                    time.sleep(0.5)
                    print(f"    INFO: ESCAPE naciśnięty.")
                except Exception as e_esc_short:
                    print(f"    OSTRZEŻENIE: Błąd ESCAPE: {e_esc_short}")
            last_height = driver.execute_script("return document.body.scrollHeight")
        except WebDriverException as e_wd_main_cycle:
            print(f"KRYTYCZNY BŁĄD WebDriver [{profile_name}]: {e_wd_main_cycle}")
            traceback.print_exc()
            print(f"INFO [{profile_name}]: Próba odzyskania za 30s...")
            time.sleep(30)
            try:
                driver.refresh()
                time.sleep(5)
            except Exception as e_refresh_fatal:
                print(f"BŁĄD [{profile_name}]: Odświeżenie po błędzie: {e_refresh_fatal}. Przerywam profil.")
                return []
        except Exception as e_main_cycle:
            print(f"KRYTYCZNY BŁĄD [{profile_name}]: {e_main_cycle}")
            traceback.print_exc()
            print(f"INFO [{profile_name}]: Czekanie {short_wait_interval * 2}s...")
            time.sleep(short_wait_interval * 2)
    return []


# --- ZMODYFIKOWANA Funkcja process_single_profile_task ---
def process_single_profile_task(config, ai_model_instance):
    profile_id = config["id"]
    profile_name_on_fb = config["profile_name_on_fb_details"]["name"]
    profile_specialization = config["profile_name_on_fb_details"]["specialization_subject"]
    search_term_for_profile = config["search_term"]

    print(f"INFO [{profile_id} ({profile_name_on_fb})]: Rozpoczynanie zadania (Specjalizacja: {profile_specialization})...")
    driver = None
    screenshot_thread = None
    stop_screenshot_event = threading.Event()

    try:
        options = webdriver.ChromeOptions()
        options.add_argument("--disable-notifications")
        options.add_argument('--no-sandbox')
        options.add_argument('--disable-dev-shm-usage')
        options.add_argument("--headless=new") 
        options.add_argument("--window-size=1200,800") 
        options.add_argument('user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36')
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        options.add_experimental_option('useAutomationExtension', False)
        options.binary_location = PATH_DO_GOOGLE_CHROME
        service = ChromeService(executable_path=PATH_DO_RECZNEGO_CHROMEDRIVER)
        driver = webdriver.Chrome(service=service, options=options)
        print(f"INFO [{profile_id}]: Przeglądarka uruchomiona (tryb headless).")

        screenshot_thread = threading.Thread(target=take_screenshots_periodically, args=(driver, profile_id, stop_screenshot_event, 10))
        screenshot_thread.daemon = True 
        screenshot_thread.start()

        driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
            "source": """
                Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                Object.defineProperty(navigator, 'languages', { get: () => ['pl-PL', 'pl'] });
                Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
            """
        })

        print(f"INFO [{profile_id}]: Otwieranie strony Facebook...")
        driver.get("https://www.facebook.com")
        time.sleep(2)

        try:
            cookie_consent_button_xpaths = [
                "//div[@aria-label='Odrzuć opcjonalne ciasteczka']//div[@role='button']",
                "//div[@aria-label='Zezwól na korzystanie z niezbędnych i opcjonalnych plików cookie']//div[@role='button']",
                "//button[contains(., 'Allow all cookies')]", "//button[contains(., 'Accept All')]",
                "//button[contains(., 'Zezwól na wszystkie pliki cookie')]", "//div[@aria-label='Allow all cookies']",
                "//button[@data-cookiebanner='accept_button']" ]
            consent_handled = False
            for xpath in cookie_consent_button_xpaths:
                try:
                    button = WebDriverWait(driver, 3).until(EC.element_to_be_clickable((By.XPATH, xpath)))
                    driver.execute_script("arguments[0].click();", button)
                    print(f"INFO [{profile_id}]: Obsłużono zgodę na ciasteczka.")
                    consent_handled = True; time.sleep(1); break
                except TimeoutException: pass
            if not consent_handled: print(f"INFO [{profile_id}]: Okno zgody na ciasteczka nie pojawiło się lub nie zostało obsłużone.")
        except Exception as e_cookie: print(f"OSTRZEŻENIE [{profile_id}]: Błąd obsługi zgody na ciasteczka: {e_cookie}")

        cookies_loaded = load_cookies(driver, COOKIES_FILE)
        is_logged_in = False
        if cookies_loaded:
            print(f"INFO [{profile_id}]: Odświeżanie strony po załadowaniu ciasteczek...")
            driver.get("https://www.facebook.com"); time.sleep(4)
            print(f"INFO [{profile_id}]: Sprawdzanie stanu zalogowania...")
            try:
                wait_login = WebDriverWait(driver, 15)
                wait_login.until(EC.presence_of_element_located((By.XPATH, "//input[@aria-label='Szukaj na Facebooku' or @placeholder='Szukaj na Facebooku'] | //a[@aria-label='Home' or @aria-label='Strona główna']")))
                print(f"SUKCES [{profile_id}]: Wygląda na to, że zalogowano pomyślnie za pomocą ciasteczek!")
                is_logged_in = True
            except TimeoutException:
                print(f"OSTRZEŻENIE [{profile_id}]: Nie potwierdzono automatycznego zalogowania (Timeout).")
                try:
                    if driver: 
                        screenshot_filename = f"screenshot_login_fail_{profile_id}.png"
                        driver.save_screenshot(os.path.join(SCREENSHOTS_DIR, screenshot_filename))
                        print(f"INFO [{profile_id}]: Zapisano zrzut ekranu: {screenshot_filename}")
                except Exception as e_ss: print(f"BŁĄD [{profile_id}]: Nie udało się zapisać zrzutu ekranu: {e_ss}")
            except Exception as e_login: print(f"BŁĄD [{profile_id}]: Sprawdzanie zalogowania: {e_login}")

        if not is_logged_in:
            print(f"KRYTYCZNY BŁĄD [{profile_id}]: Nie udało się zalogować. Przerywam zadanie.")
            return []

        if not switch_profile(driver, profile_name_on_fb, profile_id):
            print(f"KRYTYCZNY BŁĄD [{profile_id}]: Nie udało się przełączyć na profil '{profile_name_on_fb}'. Przerywam.")
            return []

        # Wyszukiwanie i filtrowanie TYLKO RAZ na początku
        if not search_and_filter_facebook(driver, search_term_for_profile):
            print(f"OSTRZEŻENIE [{profile_id}]: Wstępne wyszukiwanie/filtrowanie nie powiodło się. Przerywam.")
            return []
        if not click_latest_posts_checkbox(driver):
            print(f"OSTRZEŻENIE [{profile_id}]: Nie udało się kliknąć 'Najnowsze posty' na początku. Przerywam.")
            return []
        
        print(f"INFO [{profile_id}]: Wstępne wyszukiwanie i filtrowanie zakończone. Rozpoczynam tryb ciągłego scrollowania.")
        scroll_and_extract_post_data(driver, ai_model_instance, profile_name_on_fb, config, profile_specialization)
        print(f"INFO [{profile_id}]: Zakończono (teoretycznie) zadanie dla profilu (tryb ciągły).")
        return [] # W trybie ciągłym, ta funkcja nie zwraca już listy postów

    except WebDriverException as e_wd:
        print(f"KRYTYCZNY BŁĄD WebDriver w wątku dla {profile_id} ({profile_name_on_fb}): {e_wd}")
        traceback.print_exc()
        return []
    except Exception as e:
        print(f"KRYTYCZNY BŁĄD w wątku dla {profile_id} ({profile_name_on_fb}): {e}")
        traceback.print_exc()
        return []
    finally:
        print(f"INFO [{profile_id}]: Rozpoczynanie procedury zamykania dla profilu...")
        if stop_screenshot_event:
            print(f"INFO [{profile_id}]: Sygnalizowanie zatrzymania wątku zrzutów ekranu...")
            stop_screenshot_event.set()
        
        if screenshot_thread and screenshot_thread.is_alive():
            print(f"INFO [{profile_id}]: Oczekiwanie na zakończenie wątku zrzutów ekranu...")
            screenshot_thread.join(timeout=5)
            if screenshot_thread.is_alive():
                print(f"OSTRZEŻENIE [{profile_id}]: Wątek zrzutów ekranu nie zakończył się w ciągu 5 sekund.")

        if driver:
            print(f"INFO [{profile_id}]: Zamykanie przeglądarki...")
            driver.quit()
            print(f"INFO [{profile_id}]: Przeglądarka zamknięta.")
        print(f"INFO [{profile_id}]: Zakończono procedurę zamykania dla profilu.")


# --- ZMODYFIKOWANA FUNKCJA switch_profile (BEZ WERYFIKACJI I ZMNIEJSZONYM SCROLLOWANIEM) ---
def switch_profile(driver, profile_name_on_fb, task_id="Profil"):
    print(f"INFO [{task_id}]: Próba przełączenia na profil strony: '{profile_name_on_fb}'")
    wait = WebDriverWait(driver, 15)

    try:
        profile_icon_xpath = "//div[@role='banner']//div[@aria-label='Konto' or @aria-label='Account' or contains(@aria-label,'Twój profil')][@role='button']"
        profile_icon_alt_xpath = "//div[@role='navigation']//a[@aria-label='Twój profil']"
        profile_icon = None
        try: profile_icon = wait.until(EC.element_to_be_clickable((By.XPATH, profile_icon_xpath)))
        except TimeoutException:
            print(f"  INFO [{task_id}]: Nie znaleziono ikony profilu XPath1. Próba XPath2...")
            try: profile_icon = wait.until(EC.element_to_be_clickable((By.XPATH, profile_icon_alt_xpath)))
            except TimeoutException: print(f"  BŁĄD [{task_id}]: Nie znaleziono ikony profilu/konta."); return False
        
        driver.execute_script("arguments[0].click();", profile_icon)
        print(f"  INFO [{task_id}]: Kliknięto ikonę profilu/konta."); time.sleep(1.5)

        see_all_profiles_xpath = "//div[@role='menuitem' or @role='button']//span[normalize-space(.)='Zobacz wszystkie profile' or normalize-space(.)='See all profiles']"
        try:
            see_all_button = wait.until(EC.element_to_be_clickable((By.XPATH, see_all_profiles_xpath)))
            driver.execute_script("arguments[0].click();", see_all_button)
            print(f"  INFO [{task_id}]: Kliknięto 'Zobacz wszystkie profile'."); time.sleep(3.0)
        except TimeoutException:
            print(f"  BŁĄD [{task_id}]: Nie znaleziono przycisku 'Zobacz wszystkie profile'.")
            try: driver.find_element(By.TAG_NAME, "body").send_keys(Keys.ESCAPE)
            except: pass
            return False

        target_profile_xpath = f"//div[@role='button'][contains(@aria-label, \"{profile_name_on_fb}\")]"
        try:
            print(f"  DEBUG [{task_id}]: Próba znalezienia profilu na CAŁEJ STRONIE XPath: {target_profile_xpath}")
            wait_longer = WebDriverWait(driver, 20)
            profile_to_switch_presence = wait_longer.until(EC.presence_of_element_located((By.XPATH, target_profile_xpath)))
            print(f"  INFO [{task_id}]: Wykryto obecność elementu pasującego do XPath.")
            
            all_matching_elements = driver.find_elements(By.XPATH, target_profile_xpath)
            if not all_matching_elements:
                print(f"  BŁĄD [{task_id}]: Nie znaleziono elementów po wykryciu obecności (powinno być niemożliwe)."); return False
            
            profile_to_switch_clickable = all_matching_elements[0]

            driver.execute_script("arguments[0].click();", profile_to_switch_clickable)
            print(f"  INFO [{task_id}]: Kliknięto element dla '{profile_name_on_fb}' (JS click).")
            print(f"  INFO [{task_id}]: Zakładam, że kliknięcie zainicjowało przełączenie. Czekam chwilę..."); time.sleep(5)
            return True

        except TimeoutException:
            print(f"  BŁĄD [{task_id}]: Nie znaleziono elementu pasującego do XPath '{target_profile_xpath}' w DOM (Timeout 20s).")
            try:
                if driver: 
                    screenshot_filename = f"screenshot_profile_presence_fail_{task_id}.png"
                    driver.save_screenshot(os.path.join(SCREENSHOTS_DIR, screenshot_filename))
                    print(f"  INFO [{task_id}]: Zapisano zrzut ekranu: {screenshot_filename}")
            except Exception as e_ss: print(f"  BŁĄD [{task_id}]: Zapis zrzutu ekranu: {e_ss}")
            try: driver.find_element(By.TAG_NAME, "body").send_keys(Keys.ESCAPE)
            except: pass
            return False
        except Exception as e_click:
             print(f"  BŁĄD [{task_id}]: Znajdowanie/klikanie profilu '{profile_name_on_fb}': {e_click}")
             traceback.print_exc(); return False

    except (NoSuchElementException, TimeoutException, StaleElementReferenceException) as e_switch:
        print(f"  BŁĄD [{task_id}]: Etap przełączania profilu '{profile_name_on_fb}': {type(e_switch).__name__} - {str(e_switch).splitlines()[0]}")
        traceback.print_exc()
        try: driver.find_element(By.TAG_NAME, "body").send_keys(Keys.ESCAPE); time.sleep(0.5)
        except: pass
        return False
    except Exception as e:
        print(f"  BŁĄD [{task_id}]: Nieoczekiwany błąd przełączania profilu: {e}")
        traceback.print_exc()
        return False


def main():
    ai_model = None
    try:
        print("INFO: Inicjalizacja Gemini API (Google AI Studio)...")
        try:
            api_key_to_use = os.environ.get('GEMINI_API_KEY')
            if not api_key_to_use:
                print("KRYTYCZNY BŁĄD: Zmienna środowiskowa GEMINI_API_KEY nie jest ustawiona.")
                sys.exit(1)
            genai.configure(api_key=api_key_to_use)
            ai_model = genai.GenerativeModel('gemini-1.5-flash-latest')
            print(f"INFO: Gemini API zainicjalizowane pomyślnie. Model: {ai_model.model_name}")
        except Exception as e_gemini:
            print(f"KRYTYCZNY BŁĄD: Inicjalizacja Gemini API: {e_gemini}")
            traceback.print_exc(); sys.exit(1)

        print("\nWybierz profile do przetworzenia (oddzielone spacją, np. 1 2):")
        for key, details in PROFILE_MAPPING.items():
            display_name = details["name"].split(' - ')[-1] if ' - ' in details["name"] else details["name"]
            print(f"{key}. {display_name} ({details['name']}) - Specjalizacja: {details['specialization_subject']}")

        user_choices_str = input("Wybór: ")
        user_choices = user_choices_str.split()

        selected_configs = []
        for choice in user_choices:
            choice = choice.strip()
            if choice in PROFILE_MAPPING:
                selected_configs.append({
                    "id": choice,
                    "profile_name_on_fb_details": PROFILE_MAPPING[choice],
                    "search_term": "korepetycji"
                })
            else:
                print(f"OSTRZEŻENIE: Nieznany wybór '{choice}', zostanie zignorowany.")

        if not selected_configs:
            print("INFO: Nie wybrano żadnych profili. Zakończenie.")
            return

        print(f"\nINFO: Wybrano {len(selected_configs)} profili do przetworzenia.")
        for cfg in selected_configs: print(f"  - {cfg['profile_name_on_fb_details']['name']} (ID: {cfg['id']})")

        MAX_ALLOWED_WORKERS = 5 
        MAX_WORKERS = min(len(selected_configs), MAX_ALLOWED_WORKERS) 
        
        print(f"INFO: Uruchamianie przetwarzania w {MAX_WORKERS} wątkach (tryb ciągły)...")

        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = [executor.submit(process_single_profile_task, config, ai_model) for config in selected_configs]
            for future in as_completed(futures):
                try:
                    future.result()
                except Exception as exc:
                    print(f"INFO: Wątek zakończył się lub napotkał błąd na poziomie executor: {exc}")


        print("\n--- PODSUMOWANIE (Tryb Ciągły) ---")
        print("INFO: Skrypt działał w trybie ciągłym. Przetwarzanie w wątkach zostało zakończone (lub przerwane).")
        print("INFO: W trybie ciągłym, dane nie są globalnie agregowane w 'all_collected_posts' w funkcji main.")
        print("      Każdy wątek przetwarza swój profil cyklicznie.")


        input("\nINFO: Zakończono główną część programu (lub przerwano). Naciśnij Enter, aby zamknąć...")

    except KeyboardInterrupt:
        print("\nINFO: Przerwano działanie skryptu przez użytkownika (Ctrl+C).")
    except WebDriverException as e_wd:
        print(f"\nKRYTYCZNY BŁĄD WebDriver (poza wątkiem): {e_wd}")
        traceback.print_exc()
    except Exception as e:
        print(f"\nKRYTYCZNY BŁĄD OGÓLNY (poza wątkiem): {e}")
        traceback.print_exc()
    finally:
        print("INFO: Program zakończył działanie.")

if __name__ == "__main__":
    main()
