# GNLT EV Charger dla Home Assistant

[English](README.md) · **Polski** · [Русский](README.ru.md)

Lokalna integracja ładowarek GNLT (przenośnych i naściennych). Ładowarka łączy się z Home Assistant bezpośrednio
przez OCPP 1.6J w sieci domowej – bez chmury i bez zewnętrznych serwerów.

> **Zanim zaczniesz.** Ładowarka łączy się tylko z jednym serwerem: albo z aplikacją GNLT, albo z Home Assistant.
> Po podłączeniu do Home Assistant ładowarka zniknie z aplikacji GNLT. Jak przywrócić ją do aplikacji – w sekcji
> [Powrót do aplikacji GNLT](#powrót-do-aplikacji-gnlt).

## Funkcje

- Uruchamianie i zatrzymywanie ładowania, limit prądu ładowania (można go zmieniać także w trakcie sesji)
- Moc, prąd, napięcie, energia bieżącej sesji ładowania i energia całkowita – gotowe do użycia w panelu „Energia”
  w Home Assistant
- Wbudowany harmonogram ładowania, cena energii (taryfa jedno- lub dwustrefowa), koszt ładowania, podsumowania
  dzienne i miesięczne
- Ostrzeżenia: „Samochód nie zaczął się ładować”, „Ładowanie rozpoczęte przez samą ładowarkę”, „Złącze nieczynne”
- Konfiguracja przez Bluetooth (sieć Wi-Fi i adres serwera są zapisywane w ładowarce) albo ręcznie – przez wpisanie
  numeru seryjnego
- Języki: angielski, polski, rosyjski

## Wymagania

- Home Assistant 2025.3 lub nowszy (OS, Supervised albo Container)
- Stały adres IP serwera Home Assistant w sieci lokalnej (rezerwacja DHCP w routerze). Ładowarka zapamiętuje ten
  adres; jeśli się zmieni, ładowarka przestanie się łączyć.
- Wolny port TCP 9000 na urządzeniu z Home Assistant. W Dockerze: `network_mode: host` albo mapowanie portu
  `-p 9000:9000`. Nie udostępniaj tego portu w internecie.
- Sieć Wi-Fi 2,4 GHz dla ładowarki; nazwa sieci i hasło mogą zawierać tylko litery bez polskich znaków (A–Z, a–z),
  cyfry i znaki specjalne
- Opcjonalnie: Bluetooth w pobliżu ładowarki (wbudowany, adapter USB albo ESPHome Bluetooth Proxy)

## Instalacja

### HACS (zalecane)

1. HACS → ⋮ → **Niestandardowe repozytoria** → dodaj `https://github.com/GNLT-EV/gnlt-home-assistant`
   i wybierz typ **Integracja**.
2. Znajdź **GNLT EV Charger** w HACS i kliknij **Pobierz**.
3. Uruchom ponownie Home Assistant.

Kolejne aktualizacje będą się pojawiać w Home Assistant tak samo jak wszystkie inne.

### Ręcznie

Skopiuj folder `custom_components/gnlt_charger` do folderu `custom_components` w katalogu konfiguracyjnym
Home Assistant i uruchom ponownie Home Assistant.

## Podłączenie ładowarki

Ustawienia → Urządzenia oraz usługi → **Dodaj integrację** → **GNLT EV Charger**.

### Przez Bluetooth (zalecane)

1. Na ekranie ładowarki otwórz **Settings → Wi-Fi** i wyłącz **OCPP**. Dopóki OCPP jest włączone, ładowarka nie jest
   wykrywana przez Bluetooth. Wi-Fi zostaw włączone.
2. W kreatorze wybierz **Skonfiguruj ładowarkę przez Bluetooth**. Ładowarka może też pojawić się sama jako wykryte
   urządzenie.
3. Wybierz ładowarkę. Przez Bluetooth jest widoczna pod swoim numerem seryjnym albo jako `BL602-BLE-DEV`.
4. Wpisz nazwę i hasło sieci Wi-Fi. Sprawdź adres Home Assistant – jest uzupełniany automatycznie. Opcję
   **Rozpoczynaj ładowanie na polecenie, a nie po podłączeniu kabla** zostaw zaznaczoną.
5. Home Assistant zapisze ustawienia w ładowarce – trwa to około pół minuty, zostań w pobliżu.
6. Sprawdź wersję ładowarki (jedno- lub trójfazowa, moc). Zwykle ładowarka podaje ją sama.
7. Na ładowarce: poczekaj na ikonę Wi-Fi na ekranie, potem w **Settings → Wi-Fi** włącz **OCPP**. Ten krok trzeba
   wykonać ręcznie. W Home Assistant kliknij **Zatwierdź** – Home Assistant zaczeka, aż ładowarka się połączy.

### Ręcznie (bez Bluetooth)

1. W kreatorze wybierz **Ładowarka jest już w sieci — wprowadź jej numer**. Wpisz 12-cyfrowy numer seryjny podany
   na obudowie i wersję ładowarki.
2. Kreator pokaże adres w postaci `ws://192.168.1.10:9000/ocpp/`. Wpisz go w ustawieniach OCPP ładowarki, a w polu
   ChargeID – numer seryjny. Adres zaczyna się od `ws://`, nie `wss://`. Jeśli Home Assistant działa w Dockerze bez
   trybu sieci hosta, kreator może pokazać wewnętrzny adres kontenera (172.x.x.x) – wtedy najpierw otwórz
   Ustawienia → System → Sieć → Sieć lokalna i wpisz tam adres swojego komputera.
3. Zmieniając adres w ładowarce, zachowaj kolejność: wyłącz OCPP → wpisz adres → włącz OCPP → wyjdź z menu.
   W przeciwnym razie ładowarka nadal będzie łączyć się ze starym adresem.

## Powrót do aplikacji GNLT

1. W Home Assistant usuń urządzenie: Ustawienia → Urządzenia oraz usługi → GNLT EV Charger → ⋮ → Usuń.
2. Na ładowarce wyłącz OCPP (**Settings → Wi-Fi**).
3. W aplikacji GNLT dodaj ładowarkę ponownie przez Bluetooth. Aplikacja sama zapisze w ładowarce adres serwera GNLT.
4. Włącz OCPP na ładowarce.

## Warto wiedzieć

- Po utracie połączenia z siecią ładowarka jeszcze przez około dwie minuty jest widoczna jako połączona – to normalne.
- Jeśli samochód nie zaczął się ładować (zwykle dlatego, że przeszedł w tryb uśpienia), ładowanie może rozpocząć się
  samo, gdy samochód się wybudzi. Jeśli tego nie chcesz, odłącz kabel.
- Niektóre przełączniki mogą być nieaktywne (wyszarzone) – ich dostępność zależy od wersji oprogramowania ładowarki.

## Jeśli coś nie działa

- **Brak ikony Wi-Fi na ładowarce** – błędna nazwa lub hasło sieci albo sieć działa tylko w paśmie 5 GHz.
  Powtórz konfigurację.
- **Ikona Wi-Fi jest, ale ładowarka nie łączy się z Home Assistant** – sprawdź, czy OCPP jest włączone, czy adres
  Home Assistant jest poprawny i czy port 9000 jest dostępny. Pomaga też odłączenie ładowarki od zasilania na minutę.
- **Home Assistant nie wykrywa ładowarki przez Bluetooth** – OCPP jest nadal włączone albo adapter Bluetooth jest
  za daleko od ładowarki.

## Pomoc

GNLT · info@gnlt.pl · +48 505 699 273 · https://gnlt.pl

Zgłaszanie błędów: https://github.com/GNLT-EV/gnlt-home-assistant/issues
