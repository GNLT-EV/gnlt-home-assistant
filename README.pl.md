# GNLT EV Charger dla Home Assistant

🇬🇧 [English](README.md)

Lokalna integracja ładowarek GNLT (przenośnych i naściennych). Ładowarka łączy się z Home Assistant bezpośrednio
przez OCPP 1.6J w sieci domowej – bez chmury i bez zewnętrznych serwerów.

## Funkcje

- Uruchamianie i zatrzymywanie ładowania, limit prądu ładowania (można go zmieniać także w trakcie sesji)
- Moc, prąd, napięcie, energia bieżącej sesji ładowania i energia całkowita – gotowe do użycia w panelu „Energia”
  w Home Assistant
- Wbudowany harmonogram ładowania, cena energii (taryfa jedno- lub dwustrefowa), koszt ładowania, podsumowania
  dzienne i miesięczne
- Ostrzeżenia: „Samochód nie zaczął się ładować”, „Ładowanie rozpoczęte przez samą ładowarkę”, „Złącze nieczynne”
- Konfiguracja przez Bluetooth (sieć Wi-Fi i adres serwera są zapisywane w ładowarce) albo ręcznie – przez wpisanie
  numeru seryjnego
- Języki: angielski, polski

## Wymagania

- Home Assistant 2025.3 lub nowszy (OS, Supervised albo Container)
- Stały adres IP serwera Home Assistant w sieci lokalnej (rezerwacja DHCP w routerze)
- Wolny port TCP 9000 na urządzeniu z Home Assistant. W Dockerze: `network_mode: host` albo mapowanie portu
  `-p 9000:9000`. Nie udostępniaj tego portu w internecie.
- Sieć Wi-Fi 2,4 GHz dla ładowarki
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

## Konfiguracja

Ustawienia → Urządzenia oraz usługi → **Dodaj integrację** → **GNLT EV Charger**.

- **Przez Bluetooth:** najpierw wyłącz OCPP na ekranie ładowarki (Settings → Wi-Fi), potem przejdź przez kreator
  konfiguracji, a na końcu ponownie włącz OCPP.
- **Ręcznie:** wpisz 12-cyfrowy numer seryjny; kreator pokaże adres serwera (`ws://<ip>:9000/ocpp/`), który trzeba
  wpisać w ustawieniach OCPP ładowarki, a w polu ChargeID – numer seryjny. Jeśli Home Assistant działa w Dockerze
  bez trybu sieci hosta, najpierw przejdź do Ustawienia → System → Sieć → Sieć lokalna i wpisz tam adres swojego
  komputera.

Zmieniając adres w ładowarce, zachowaj kolejność: wyłącz OCPP → wpisz adres → włącz OCPP → wyjdź z menu.

## Warto wiedzieć

- Ładowarka łączy się tylko z jednym serwerem: albo z aplikacją GNLT, albo z Home Assistant.
- Po utracie połączenia z siecią ładowarka jeszcze przez około dwie minuty jest widoczna jako połączona – to normalne.
- Jeśli samochód nie zaczął się ładować (zwykle dlatego, że przeszedł w tryb uśpienia), ładowanie może rozpocząć się
  samo, gdy samochód się wybudzi. Jeśli tego nie chcesz, odłącz kabel.
- Niektóre przełączniki mogą być nieaktywne (wyszarzone) – ich dostępność zależy od wersji oprogramowania ładowarki.

## Pomoc

Zgłoszenia: https://github.com/GNLT-EV/gnlt-home-assistant/issues
