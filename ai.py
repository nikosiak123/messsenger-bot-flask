import vertexai
from vertexai.generative_models import GenerativeModel, Part

# --- Konfiguracja ---
PROJECT_ID = "twoj-google-cloud-project-id"  # Zastąp ID Twojego projektu
LOCATION = "us-central1"  # Zastąp odpowiednią lokalizacją (np. europe-west4)

# Inicjalizacja Vertex AI SDK
vertexai.init(project=PROJECT_ID, location=LOCATION)

# Wybór modelu Gemini
# Dostępne modele: 'gemini-1.0-pro', 'gemini-1.0-pro-vision', 'gemini-1.5-pro-preview-0409' itd.
# Sprawdź dokumentację dla najnowszych dostępnych modeli
model = GenerativeModel("gemini-1.0-pro") # Przykład użycia Gemini Pro (tylko tekst)

# Przygotowanie promptu (zapytania)
prompt = "Opowiedz krótki żart o programistach."

# Wygenerowanie odpowiedzi
print(f"--- Wysyłanie promptu do Gemini Pro ---")
print(f"Prompt: {prompt}")

try:
    # Wywołanie modelu
    response = model.generate_content(prompt)

    # Wyświetlenie odpowiedzi
    print("\n--- Odpowiedź Gemini ---")
    # Sprawdzamy, czy odpowiedź zawiera tekst
    if hasattr(response, 'text'):
         print(response.text)
    else:
         # Czasem odpowiedź może być bardziej złożona lub zawierać blokady bezpieczeństwa
         print("Otrzymano odpowiedź, ale bez prostego pola 'text'. Cała odpowiedź:")
         print(response)

except Exception as e:
    print(f"!!! Wystąpił błąd podczas komunikacji z Vertex AI: {e} !!!")

print("\n--- Koniec ---")

# --- Przykład dla Gemini Pro Vision (Multimodalny - tekst + obraz) ---
# model_vision = GenerativeModel("gemini-1.0-pro-vision")
# image_path = "sciezka/do/twojego/obrazka.jpg"
# image_part = Part.from_uri(image_path, mime_type="image/jpeg")
# prompt_vision = "Opisz co widzisz na tym obrazku."
# response_vision = model_vision.generate_content([image_part, prompt_vision]) # Przesyłamy listę części
# print(response_vision.text)
