# -*- coding: utf-8 -*-
import sys
import time
import os
import pickle

# Selenium Imports
from selenium import webdriver
from selenium.webdriver.chrome.service import Service as ChromeService # Zmienione na ChromeService dla spójności z pierwszym skryptem
from selenium.webdriver.common.by import By
from selenium.common.exceptions import WebDriverException # Zachowane dla obsługi błędów

# Opcjonalnie: przekierowanie logów do pliku, jeśli chcesz śledzić działanie
# sys.stdout = open('log_otwarcia_fb.txt', 'w', encoding='utf-8')

# Globalne zmienne
COOKIES_FILE = "cookies.pkl" # Nazwa pliku z ciasteczkami

# --- Konfiguracja ścieżek z pierwszego skryptu ---
PATH_DO_GOOGLE_CHROME = "/opt/google/chrome/chrome"
PATH_DO_RECZNEGO_CHROMEDRIVER = "/home/nikodnaj/PROJEKT_AUTOMATYZACJA/chromedriver-linux64/chromedriver"
# --- Koniec konfiguracji ścieżek ---

def save_cookies(driver, file_path):
    """Zapisuje cookies z przeglądarki do pliku."""
    try:
        with open(file_path, 'wb') as file:
            pickle.dump(driver.get_cookies(), file)
        print(f"INFO: Ciasteczka zapisane do pliku: {file_path}")
    except Exception as e:
        print(f"BŁĄD: Nie udało się zapisać ciasteczek: {e}")

def load_cookies(driver, file_path):
    """Wczytuje cookies z pliku i ustawia je w przeglądarce."""
    if os.path.exists(file_path):
        try:
            with open(file_path, 'rb') as file:
                cookies = pickle.load(file)
                if cookies:
                    print(f"INFO: Próba załadowania {len(cookies)} ciasteczek z {file_path}")
                    for cookie in cookies:
                        if 'expiry' in cookie and isinstance(cookie['expiry'], float):
                            cookie['expiry'] = int(cookie['expiry'])
                        # Usunięcie 'sameSite' jeśli nie jest jednym z akceptowalnych wartości
                        # lub jeśli powoduje problemy. Można dostosować.
                        if 'sameSite' in cookie and cookie['sameSite'] not in ['Strict', 'Lax', 'None', 'Lax ', 'Strict ', 'None ']:
                            print(f"INFO: Usuwanie niepoprawnej wartości 'sameSite': {cookie['sameSite']} dla ciasteczka {cookie.get('name', 'N/A')}")
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
        except Exception as e:
            print(f"BŁĄD: Nie udało się wczytać ciasteczek: {e}")
            return False
    else:
        print(f"INFO: Plik z ciasteczkami ({file_path}) nie istnieje.")
        return False
    return False

def main():
    driver = None  # Inicjalizacja drivera
    try:
        print("INFO: Uruchamianie przeglądarki Chrome...")
        options = webdriver.ChromeOptions()
        options.add_argument("--disable-notifications") # Wyłącza powiadomienia przeglądarki
        options.add_argument('--no-sandbox') # Często potrzebne w środowiskach Linux/Docker
        options.add_argument('--disable-dev-shm-usage') # Często potrzebne w środowiskach Linux/Docker
        options.add_argument('user-agent=Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/100.0.4896.75 Safari/537.36') # User-agent z pierwszego skryptu

        # --- Użycie ścieżek zdefiniowanych globalnie ---
        options.binary_location = PATH_DO_GOOGLE_CHROME
        service = ChromeService(executable_path=PATH_DO_RECZNEGO_CHROMEDRIVER)
        driver = webdriver.Chrome(service=service, options=options)
        # --- Koniec użycia ścieżek ---

        driver.implicitly_wait(5) # Niejawne oczekiwanie na elementy

        print("INFO: Otwieranie strony Facebook...")
        # Najpierw otwórz domenę, dla której chcesz załadować ciasteczka
        # To ważne, bo ciasteczka są powiązane z domeną.
        # Dla Facebooka, często lepiej jest otworzyć główną domenę PRZED załadowaniem ciasteczek
        driver.get("https://www.facebook.com") # Użyj 'https://www.' dla spójności
        time.sleep(2) # Krótka pauza na załadowanie strony

        # Wczytywanie ciasteczek
        cookies_loaded = load_cookies(driver, COOKIES_FILE)

        if cookies_loaded:
            print("INFO: Odświeżanie strony po wczytaniu ciasteczek...")
            driver.get("https://www.facebook.com") # Można też użyć driver.refresh(), ale get jest pewniejsze
            time.sleep(5) # Daj stronie chwilę na załadowanie z ciasteczkami
            print("INFO: Sprawdzanie, czy jesteś zalogowany...")
            try:
                # Przykładowy element, który może istnieć po zalogowaniu (dostosuj, jeśli trzeba)
                # Wyszukiwanie po aria-label jest dość stabilne
                search_box_xpath = "//input[@aria-label='Szukaj na Facebooku'] | //input[@aria-label='Search Facebook'] | //div[@role='search']//input[@type='search']"
                driver.find_element(By.XPATH, search_box_xpath)
                print("SUKCES: Wygląda na to, że jesteś zalogowany przy użyciu ciasteczek!")
            except Exception: # Użyj bardziej ogólnego Exception, bo może to być NoSuchElementException lub TimeoutException
                print("INFO: Nie udało się potwierdzić zalogowania automatycznie przez znalezienie pola wyszukiwania. Sprawdź stronę.")
                print("INFO: Jeśli nie jesteś zalogowany, usuń plik cookies.pkl i uruchom skrypt ponownie, aby się zalogować i zapisać nowe ciasteczka.")
        else:
            print("INFO: Nie wczytano ciasteczek. Prawdopodobnie zostaniesz poproszony o zalogowanie.")

        input_text = "INFO: Sprawdź stan zalogowania. Jeśli trzeba, zaloguj się ręcznie.\n" \
                     "Jeśli jesteś zalogowany, naciśnij Enter, aby zapisać ciasteczka i kontynuować.\n" \
                     "Jeśli chcesz pominąć zapisywanie ciasteczek, wpisz 'pomin' i Enter.\n" \
                     "Wpisz cokolwiek innego (lub zamknij okno), aby zakończyć bez zapisywania: "
        user_action = input(input_text)

        if user_action.lower() == "": # Użytkownik nacisnął Enter
            print("INFO: Zapisywanie ciasteczek...")
            save_cookies(driver, COOKIES_FILE)
        elif user_action.lower() == "pomin":
            print("INFO: Pominięto zapisywanie ciasteczek.")
        else:
            print("INFO: Zakończenie bez zapisywania ciasteczek.")


        print("INFO: Skrypt zakończył działanie. Przeglądarka pozostanie otwarta przez chwilę. Zamknij ją ręcznie lub poczekaj.")
    except WebDriverException as e:
        print(f"KRYTYCZNY BŁĄD WebDriver: {e}")
        print("Upewnij się, że masz zainstalowany ChromeDriver i jest on w systemowym PATH,")
        print(f"lub podana ścieżka do ChromeDrivera jest poprawna: '{PATH_DO_RECZNEGO_CHROMEDRIVER}'.")
        print(f"Upewnij się również, że wersja ChromeDriver pasuje do Twojej wersji przeglądarki Chrome (ścieżka Chrome: '{PATH_DO_GOOGLE_CHROME}').")
    except Exception as e:
        print(f"KRYTYCZNY BŁĄD: Wystąpił nieoczekiwany błąd: {e}")
        import traceback
        traceback.print_exc()
    finally:
        if driver:
            driver.quit()
            print("INFO: Przeglądarka została zamknięta.")

if __name__ == "__main__":
    main()