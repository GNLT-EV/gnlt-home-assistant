# GNLT EV Charger dla Home Assistant

[English](README.md) · **Polski** · [Русский](README.ru.md)

Integracja ładowarek GNLT (przenośnych i naściennych) z Home Assistantem. Ładowarka współpracuje z Home Assistantem
bezpośrednio w Twojej sieci domowej, bez chmury.

> **Ważne.** Ładowarka działa albo z aplikacją GNLT, albo z Home Assistantem, ale nie z obydwoma jednocześnie.
> Po podłączeniu do Home Assistanta ładowarka zniknie z aplikacji GNLT. Jak ją przywrócić – w sekcji
> [Powrót do aplikacji GNLT](#powrót-do-aplikacji-gnlt).

## Funkcje

- Uruchamianie i zatrzymywanie ładowania, limit prądu (można go zmieniać także w trakcie ładowania)
- Moc, prąd, napięcie, energia ładowania i energia całkowita – do panelu „Energia” w Home Assistant
- Harmonogram ładowania, cena energii (taryfa jedno- lub dwustrefowa), koszt ładowania, podsumowania dzienne i miesięczne
- Ostrzeżenia: „Samochód nie zaczął się ładować”, „Ładowanie rozpoczęte przez samą ładowarkę”, „Złącze nieczynne”
- Połączenie ładowarki przez Bluetooth lub ręczne
- Języki: angielski, polski, rosyjski

## Czego potrzebujesz

- Home Assistant 2025.3 lub nowszy.
- Stały adres IP komputera lub serwera z Home Assistantem w sieci domowej. Zarezerwuj go w ustawieniach routera.
- Wolny port 9000 na komputerze lub serwerze z Home Assistantem. Jeśli Home Assistant działa w Dockerze, uruchom
  kontener z `network_mode: host` albo z `-p 9000:9000`. Nie udostępniaj tego portu w internecie.
- Sieć Wi-Fi 2,4 GHz z zabezpieczeniem WPA2. Sieci 5 GHz, WPA3 i tryby mieszane (WPA/WPA2, WPA2/WPA3) nie nadają się.
  Nazwa sieci i hasło mogą zawierać tylko litery alfabetu łacińskiego bez znaków diakrytycznych, cyfry i znaki specjalne.
- Do połączenia przez Bluetooth: Bluetooth w Home Assistant w pobliżu ładowarki – wbudowany, adapter USB albo
  ESPHome Bluetooth Proxy.
- Do połączenia ręcznego: aplikacja GNLT na telefonie.

## Instalacja

1. Otwórz **HACS**.
2. W menu **⋮** wybierz **Niestandardowe repozytoria**.
3. Wklej `https://github.com/GNLT-EV/gnlt-home-assistant`, w polu **Typ** wybierz **Integracja** i kliknij **Dodaj**.
   Zamknij okno.
4. W wyszukiwarce HACS wpisz `GNLT EV Charger` i otwórz integrację.
5. Kliknij **Pobierz**, a potem jeszcze raz **Pobierz** w oknie, które się otworzy.
6. Uruchom ponownie Home Assistanta: **Ustawienia** → **System** → **⏻** → **Uruchom ponownie Home Assistanta** →
   **Uruchom ponownie**.

Nowe wersje pojawią się w **Ustawienia** → **Aktualizacje**, tak jak inne aktualizacje Home Assistanta.

## Połączenie ładowarki przez Bluetooth

1. Na ładowarce naciśnij przycisk **OK**, wybierz **Wi-Fi**, wyłącz **OCPP** i wyjdź z menu.
2. W Home Assistant otwórz **Ustawienia** → **Urządzenia oraz usługi** → **Dodaj integrację**.
3. W wyszukiwarce wpisz `GNLT` i wybierz **GNLT EV Charger**.
4. Wybierz **Skonfiguruj ładowarkę przez Bluetooth (zalecane)**.
5. Zaznacz swoją ładowarkę – jej numer seryjny jest na obudowie – i kliknij **Zatwierdź**.
6. Wypełnij pola i kliknij **Zatwierdź**:
   - **Nazwa sieci** i **Hasło sieci** – dane Twojej sieci Wi-Fi;
   - **Adres Home Assistanta w Twojej sieci** – adres IP komputera lub serwera z Home Assistantem, na przykład
     `192.168.1.10`. Jeśli w polu jest inny adres, usuń go i wpisz swój. Jeśli otwierasz Home Assistanta
     w przeglądarce pod adresem typu `http://192.168.1.10:8123`, Twój adres to `192.168.1.10`. W przeciwnym
     razie znajdź go na liście urządzeń w routerze;
   - **Port dla ładowarek**, jeśli to pole jest widoczne – `9000`;
   - **Rozpoczynaj ładowanie na polecenie, a nie po podłączeniu kabla** – pozostaw tę opcję włączoną.
7. Poczekaj około pół minuty, aż ładowarka zapisze ustawienia. Nie oddalaj się od ładowarki.
8. Sprawdź, czy zaznaczona jest wersja Twojej ładowarki – liczba faz i moc, jak na jej tabliczce znamionowej.
   Jeśli zaznaczona jest inna, zaznacz swoją. Kliknij **Zatwierdź**.
9. Na ładowarce poczekaj, aż na ekranie pojawi się ikona Wi-Fi. Naciśnij **OK**, wybierz **Wi-Fi**, włącz **OCPP**
   i wyjdź z menu.
10. W Home Assistant kliknij **Zatwierdź** i poczekaj, aż ładowarka się połączy.
11. W ostatnim oknie kliknij **Pomiń i zakończ**.

Ładowarka pojawi się w **Ustawienia** → **Urządzenia oraz usługi** → **GNLT EV Charger**.

Jeśli w kroku 5 ładowarki nie ma na liście: sprawdź, czy OCPP na ładowarce jest wyłączone, zamknij aplikację GNLT
na telefonie i kliknij **Zatwierdź**, aby wyszukać ją ponownie.

## Ręczne połączenie ładowarki

Użyj tego sposobu, jeśli Home Assistant nie ma modułu Bluetooth.

1. Połącz ładowarkę ze swoją siecią Wi-Fi w aplikacji GNLT: stuknij ikonę **kodu QR** lub **+**, zeskanuj kod QR
   ładowarki (jest w instrukcji obsługi i pod pokrywą ładowarki), wpisz 6-cyfrowy kod PUK (na odwrocie instrukcji)
   i potwierdź dodanie. Potem stuknij ikonę **Wi-Fi** i wpisz nazwę oraz hasło sieci.
2. W Home Assistant otwórz **Ustawienia** → **Urządzenia oraz usługi** → **Dodaj integrację**.
3. W wyszukiwarce wpisz `GNLT` i wybierz **GNLT EV Charger**.
4. Wybierz **Ładowarka jest już w sieci — wprowadź jej numer**.
5. Wpisz **Numer seryjny** (12 cyfr z obudowy ładowarki), w polu **Wersja ładowarki** wybierz liczbę faz i moc, jak
   na jej tabliczce znamionowej, i kliknij **Zatwierdź**.
6. Home Assistant pokaże adres serwera i ChargeID. Zostaw to okno otwarte. Jeśli adres zaczyna się od `ws://172.`,
   zamknij to okno i otwórz **Ustawienia** → **System** → **Sieć**. W sekcji **Sieć lokalna** wyłącz
   **Automatycznie**, wpisz `http://adres-IP-komputera:8123`, na przykład `http://192.168.1.10:8123`, kliknij
   **Zapisz** i zacznij ponownie od kroku 2.
7. Na ładowarce naciśnij **OK** i wybierz **Wi-Fi**. Wyłącz **OCPP**, wpisz adres serwera i ChargeID z okna
   Home Assistanta, włącz **OCPP** i wyjdź z menu.
8. W Home Assistant kliknij **Zatwierdź**, a potem w ostatnim oknie **Pomiń i zakończ**.

## Powrót do aplikacji GNLT

1. W Home Assistant otwórz **Ustawienia** → **Urządzenia oraz usługi** → **GNLT EV Charger**.
2. Przy ładowarce kliknij **⋮** → **Usuń** i potwierdź.
3. Na ładowarce naciśnij **OK**, wybierz **Wi-Fi**, wyłącz **OCPP** i wyjdź z menu.
4. W aplikacji GNLT na telefonie dodaj ładowarkę ponownie: stuknij ikonę **kodu QR** lub **+**, zeskanuj kod QR
   ładowarki, wpisz kod PUK i potwierdź dodanie.
5. Na ładowarce naciśnij **OK**, wybierz **Wi-Fi**, włącz **OCPP** i wyjdź z menu. Ładowarka połączy się z aplikacją
   GNLT.

## Warto wiedzieć

- Nazwy czujników i przełączników ładowarki są wyświetlane w języku wybranym w ustawieniach systemu Home Assistanta.
- Po utracie połączenia ładowarka jest jeszcze przez około dwie minuty widoczna jako połączona.
- Jeśli samochód nie zaczął się ładować, ładowanie może rozpocząć się samo, gdy samochód się wybudzi. Jeśli tego nie
  chcesz, odłącz kabel.
- Niektóre przełączniki mogą być wyszarzone: te funkcje zależą od wersji oprogramowania ładowarki.

## Jeśli coś nie działa

- **Na ładowarce nie ma ikony Wi-Fi.** Sprawdź nazwę i hasło sieci oraz ustawienia routera: sieć 2,4 GHz,
  zabezpieczenie WPA2 bez trybu mieszanego. Potem ponownie dodaj ładowarkę do Home Assistanta.
- **Ikona Wi-Fi jest widoczna, ale ładowarka nie łączy się z Home Assistantem.** Sprawdź, czy OCPP na ładowarce jest włączone,
  czy adres Home Assistanta jest poprawny i czy port 9000 jest wolny.
- **Ładowarki nie widać przez Bluetooth.** Wyłącz OCPP na ładowarce, zamknij aplikację GNLT na telefonie i przysuń
  adapter Bluetooth bliżej ładowarki.

## Pomoc

GNLT · info@gnlt.pl · +48 505 699 273 · https://gnlt.pl

Zgłaszanie problemów: https://github.com/GNLT-EV/gnlt-home-assistant/issues
