# zarzadzaj_kalendarzami.py
import json
import os

CONFIG_FILE = 'calendars_config.json'

def wczytaj_konfiguracje():
    """Wczytuje konfigurację kalendarzy z pliku JSON."""
    if not os.path.exists(CONFIG_FILE):
        return []
    try:
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        print(f"Błąd: Nie można odczytać pliku konfiguracyjnego '{CONFIG_FILE}'.")
        return []

def zapisz_konfiguracje(data):
    """Zapisuje konfigurację kalendarzy do pliku JSON."""
    try:
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=4, ensure_ascii=False)
        print("\nKonfiguracja została pomyślnie zapisana.")
        return True
    except IOError:
        print(f"\nBłąd: Nie można zapisać do pliku konfiguracyjnego '{CONFIG_FILE}'.")
        return False

def listuj_kalendarze():
    """Wyświetla listę skonfigurowanych kalendarzy."""
    kalendarze = wczytaj_konfiguracje()
    print("\n--- Aktualnie Skonfigurowane Kalendarze ---")
    if not kalendarze:
        print("Brak skonfigurowanych kalendarzy.")
    else:
        for i, kalendarz in enumerate(kalendarze, 1):
            print(f"{i}. Nazwa:    {kalendarz.get('name')}")
            print(f"   ID:       {kalendarz.get('id')}")
            print(f"   Przedmiot: {kalendarz.get('subject')}")
            print("-" * 20)
    print("--------------------------------------------")

def dodaj_kalendarz():
    """Dodaje nowy kalendarz do konfiguracji."""
    kalendarze = wczytaj_konfiguracje()
    print("\n--- Dodawanie Nowego Kalendarza ---")
    
    name = input("Podaj nazwę kalendarza (np. 'Korepetycje Anny - Chemia'): ").strip()
    cal_id = input("Podaj ID kalendarza (np. '...long...@group.calendar.google.com'): ").strip()
    subject = input("Podaj przypisany przedmiot (np. 'Chemia'): ").strip()
    
    if not all([name, cal_id, subject]):
        print("\nBłąd: Wszystkie pola są wymagane. Anulowano.")
        return
        
    nowy_kalendarz = {
        'id': cal_id,
        'name': name,
        'subject': subject
    }
    
    kalendarze.append(nowy_kalendarz)
    
    if zapisz_konfiguracje(kalendarze):
        print(f"Pomyślnie dodano kalendarz: {name}")

def usun_kalendarz():
    """Usuwa kalendarz z konfiguracji."""
    kalendarze = wczytaj_konfiguracje()
    if not kalendarze:
        print("\nBrak kalendarzy do usunięcia.")
        return
        
    listuj_kalendarze()
    try:
        wybor = int(input("\nPodaj numer kalendarza, który chcesz usunąć (0 aby anulować): "))
        if wybor == 0:
            print("Anulowano.")
            return
        if 1 <= wybor <= len(kalendarze):
            usuniety = kalendarze.pop(wybor - 1)
            if zapisz_konfiguracje(kalendarze):
                print(f"Pomyślnie usunięto kalendarz: {usuniety.get('name')}")
        else:
            print("Błędny numer. Proszę spróbować ponownie.")
    except ValueError:
        print("Błędne dane. Proszę podać numer.")

def menu_glowne():
    """Wyświetla główne menu i obsługuje wybór użytkownika."""
    while True:
        print("\n=== Narzędzie do Zarządzania Kalendarzami Bota ===")
        print("1. Wyświetl listę kalendarzy")
        print("2. Dodaj nowy kalendarz")
        print("3. Usuń kalendarz")
        print("4. Wyjdź")
        
        wybor = input("Wybierz opcję [1-4]: ")
        
        if wybor == '1':
            listuj_kalendarze()
        elif wybor == '2':
            dodaj_kalendarz()
        elif wybor == '3':
            usun_kalendarz()
        elif wybor == '4':
            print("Do widzenia.")
            break
        else:
            print("Nieprawidłowa opcja. Proszę wybrać numer od 1 do 4.")

if __name__ == '__main__':
    menu_glowne()
