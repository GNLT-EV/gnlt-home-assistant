# GNLT EV Charger dla Home Assistant

[English](README.md) · **Polski** · [Русский](README.ru.md)

Integracja ładowarek GNLT (przenośnych i naściennych) z Home Assistantem. Ładowarka współpracuje z Home Assistantem
bezpośrednio w Twojej sieci domowej, bez chmury.

> **Ważne.** Ładowarka działa albo z aplikacją EV-Charger, albo z Home Assistantem, ale nie z obydwoma jednocześnie.
> Po podłączeniu do Home Assistanta ładowarka zniknie z aplikacji EV-Charger. Jak ją przywrócić – w sekcji
> [Powrót do aplikacji EV-Charger](#powrót-do-aplikacji-ev-charger).

## Funkcje

- Uruchamianie i zatrzymywanie ładowania, limit prądu (można go zmieniać także w trakcie ładowania)
- Moc i moc pozorna (U×I), prąd, napięcie, energia ładowania; energia całkowita – do panelu „Energia” w Home Assistant
- Harmonogram ładowania, cena energii (taryfa jedno- lub dwustrefowa), koszt ładowania, podsumowania dzienne i miesięczne
- Ostrzeżenia: „Samochód nie zaczął się ładować”, „Ładowanie rozpoczęte przez samą ładowarkę”, „Złącze nieczynne”,
  „Ładowarka zatrzymała ładowanie” – z prawdopodobną przyczyną
- Liczba faz, którymi w danej chwili ładuje się samochód
- Plik diagnostyczny dla pomocy technicznej
- Połączenie ładowarki przez Bluetooth lub ręczne
- Języki: angielski, polski, rosyjski

## Czego potrzebujesz

- Home Assistant 2025.11 lub nowszy.
- Stały adres IP komputera lub serwera z Home Assistantem w Twojej sieci domowej. Zarezerwuj go w ustawieniach
  routera.
- Tylko jeśli Home Assistant działa w Dockerze: uruchom kontener z `network_mode: host` albo z `-p 9000:9000`.
  Nie udostępniaj tego portu w internecie.
- Sieć Wi-Fi 2,4 GHz z zabezpieczeniem WPA2. Sieci 5 GHz, WPA3 i tryby mieszane (WPA/WPA2, WPA2/WPA3) nie zadziałają.
  Nazwa sieci i hasło – do 32 znaków: tylko litery bez polskich znaków (A–Z, a–z), cyfry i znaki specjalne.
- Do połączenia przez Bluetooth: Bluetooth w Home Assistant w pobliżu ładowarki – wbudowany, adapter USB albo
  ESPHome Bluetooth Proxy.
- Do połączenia ręcznego: aplikacja EV-Charger na telefonie.

## Instalacja

1. Otwórz **HACS**.
2. W menu **⋮** wybierz **Niestandardowe repozytoria**.
3. Wklej `https://github.com/GNLT-EV/gnlt-home-assistant`, w polu **Typ** wybierz **Integracja** i kliknij **Dodaj**.
   Zamknij okno.
4. W wyszukiwarce HACS wpisz `GNLT EV Charger` i otwórz integrację.
5. Kliknij **Pobierz**, a potem jeszcze raz **Pobierz** w oknie, które się otworzy.
6. Uruchom ponownie Home Assistanta: **Ustawienia** → **System** → **⏻** → **Uruchom ponownie Home Assistanta** →
   **Uruchom ponownie**.

Nowe wersje pojawią się w **Ustawienia** → **System** → **Aktualizacje**.

## Połączenie ładowarki przez Bluetooth

1. Zamknij aplikację EV-Charger na telefonie. Na ładowarce naciśnij przycisk **OK**, wybierz **Wi-Fi**, upewnij się,
   że Wi-Fi jest włączone, wyłącz **OCPP** i wyjdź z menu.
2. W Home Assistant otwórz **Ustawienia** → **Urządzenia oraz usługi** → **Dodaj integrację**.
3. W wyszukiwarce wpisz `GNLT` i wybierz **GNLT EV Charger**.
4. Wybierz **Skonfiguruj ładowarkę przez Bluetooth (zalecane)**.
5. W polu **Ładowarka** wybierz swoją ładowarkę – jej numer seryjny jest na obudowie – i kliknij **Zatwierdź**. Jeśli
   zobaczysz „Nie znaleziono ładowarki”, sprawdź, czy OCPP na ładowarce jest wyłączone, a aplikacja EV-Charger
   zamknięta, przysuń adapter Bluetooth (albo komputer z Home Assistantem, jeśli ma wbudowany Bluetooth) bliżej
   ładowarki i ponownie kliknij **Zatwierdź**. Jeśli to nie pomoże, połącz ładowarkę
   [ręcznie](#ręczne-połączenie-ładowarki).
6. Wypełnij pola i kliknij **Zatwierdź**:
   - **Nazwa sieci** i **Hasło sieci** – Twoja sieć Wi-Fi;
   - **Adres Home Assistanta w Twojej sieci** – adres IP komputera lub serwera z Home Assistantem, na przykład
     `192.168.1.10`. Jeśli otwierasz Home Assistanta w przeglądarce pod adresem typu `http://192.168.1.10:8123`,
     Twój adres to `192.168.1.10`. W przeciwnym razie znajdź go na liście urządzeń w routerze. Jeśli w polu jest
     inny adres, zastąp go swoim;
   - **Port dla ładowarek**, jeśli to pole jest widoczne – `9000`. Jeśli zobaczysz „Ten port jest używany przez inny
     program”, wpisz `9001`. Jeśli Home Assistant działa w Dockerze z `-p 9000:9000`, utwórz kontener ponownie
     z `-p 9001:9001` i zacznij od nowa od kroku 2;
   - **Rozpoczynaj ładowanie na polecenie, a nie po podłączeniu kabla** – pozostaw tę opcję włączoną.

   Jeśli formularz pokazuje błąd w nazwie sieci lub haśle, zmień je w ustawieniach routera, wpisz nowe w formularzu
   i kliknij **Zatwierdź**.
7. Poczekaj około pół minuty, aż ładowarka zapisze ustawienia. Jeśli pojawi się błąd, zrób to, co jest w nim napisane.
   Jeśli błąd się powtarza, połącz ładowarkę [ręcznie](#ręczne-połączenie-ładowarki).
8. Sprawdź, czy wybrana jest wersja Twojej ładowarki – liczba faz i moc, jak na tabliczce znamionowej ładowarki.
   Jeśli wybrana jest inna, wybierz swoją. Kliknij **Zatwierdź**.
9. Na ładowarce poczekaj, aż na ekranie pojawi się ikona Wi-Fi. Naciśnij **OK**, wybierz **Wi-Fi**, włącz **OCPP**
   i wyjdź z menu. Jeśli ikona Wi-Fi się nie pojawi, sprawdź, czy Wi-Fi na ładowarce jest włączone: jeśli jest
   wyłączone, włącz je i poczekaj na ikonę. Jeśli jest włączone, zamknij okno w Home Assistant, porównaj nazwę sieci
   i hasło z ustawieniami routera, sprawdź tam, czy sieć działa w paśmie 2,4 GHz z zabezpieczeniem WPA2 bez trybu
   mieszanego, i zacznij od nowa od kroku 1.
10. W Home Assistant kliknij **Zatwierdź** i poczekaj, aż ładowarka się połączy – do 10 minut.
11. W oknie **Nazwij i przypisz** kliknij **Pomiń i zakończ**.

Ładowarka pojawi się w **Ustawienia** → **Urządzenia oraz usługi** → **GNLT EV Charger**.

Jeśli zamiast okna **Nazwij i przypisz** pojawi się okno „Ładowarka jeszcze się nie połączyła”, sprawdź wymienione
w nim punkty. Adres serwera jest widoczny na ładowarce: **OK** → **Wi-Fi**. Jeśli OCPP jest wyłączone, włącz je.
Następnie kliknij **Zatwierdź**, a potem **Pomiń i zakończ**: ładowarka zostanie dodana i połączy się, gdy tylko
połączenie zacznie działać.
Jeśli nie ma ikony Wi-Fi albo adres jest inny, zajrzyj potem do sekcji [Jeśli coś nie działa](#jeśli-coś-nie-działa).

## Karty w sekcji „Wykryte”

Home Assistant może sam pokazać ładowarkę w **Ustawienia** → **Urządzenia oraz usługi**, w sekcji **Wykryte**.
Kliknij **Dodaj** na karcie i sprawdź, które okno się otworzy:

- **„W pobliżu jest ładowarka GNLT”** – ładowarka jest w trybie Bluetooth. Zamknij aplikację EV-Charger na telefonie,
  kliknij **Zatwierdź** i kontynuuj od kroku 6 sekcji
  [Połączenie ładowarki przez Bluetooth](#połączenie-ładowarki-przez-bluetooth).
- **„Ładowarka … połączona”** – ładowarka sama połączyła się z Home Assistantem. Wybierz wersję ładowarki, kliknij
  **Zatwierdź**, a potem **Pomiń i zakończ**.
- **„To nie jest ładowarka GNLT.”** – kliknij **Zamknij**, a na karcie kliknij **Ignoruj**.

## Ręczne połączenie ładowarki

Użyj tego sposobu, jeśli Home Assistant nie ma Bluetootha.

1. Połącz ładowarkę z aplikacją EV-Charger i ze swoją siecią Wi-Fi zgodnie z instrukcją obsługi ładowarki. Jeśli
   aplikacja nie znajduje ładowarki, wyłącz OCPP na ładowarce. Upewnij się, że Wi-Fi na ładowarce jest włączone.
2. W Home Assistant otwórz **Ustawienia** → **Urządzenia oraz usługi** → **Dodaj integrację**.
3. W wyszukiwarce wpisz `GNLT` i wybierz **GNLT EV Charger**.
4. Wybierz **Ładowarka jest już w sieci — wprowadź jej numer**.
5. Wpisz **Numer seryjny** (12 cyfr z obudowy ładowarki) i wybierz **Wersja ładowarki** – liczbę faz i moc, jak
   na tabliczce znamionowej ładowarki. Jeśli jest pole **Port dla ładowarek**, pozostaw `9000` (jeśli port jest
   zajęty – `9001`, jak w kroku 6 sekcji o Bluetooth). Kliknij **Zatwierdź**.
6. Home Assistant pokaże adres serwera i ChargeID. Sprawdź, czy adres serwera zawiera adres IP Twojego Home
   Assistanta (jak w kroku 6 sekcji o Bluetooth). Jeśli widać inny adres, na przykład `ws://172.17.0.2…`, zamknij
   okno, otwórz **Ustawienia** → **System** → **Sieć**, w sekcji **Sieć lokalna** wyłącz **Automatycznie**, wpisz
   `http://adres-IP-komputera:8123`, na przykład `http://192.168.1.10:8123`, kliknij **Zapisz** i zacznij od nowa
   od kroku 2. Jeśli adres jest poprawny, zostaw okno otwarte.
7. W aplikacji, w ustawieniach OCPP, wpisz adres serwera i ChargeID z okna Home Assistanta.
8. Na ładowarce naciśnij **OK** i wybierz **Wi-Fi** – widać tam wpisany przez Ciebie adres. Jeśli adresu nie ma
   albo jest inny, sprawdź, co wpisano w aplikacji (krok 7). Włącz **OCPP** i wyjdź z menu.
9. W Home Assistant kliknij **Zatwierdź**, a potem **Pomiń i zakończ**.

## Zmiana sieci Wi-Fi

**Z Bluetoothem** – nie musisz usuwać ładowarki z Home Assistanta:

1. Zamknij aplikację EV-Charger na telefonie. Na ładowarce naciśnij **OK**, wybierz **Wi-Fi**, wyłącz **OCPP**
   i wyjdź z menu.
2. Wykonaj kroki 2–8 z sekcji [Połączenie ładowarki przez Bluetooth](#połączenie-ładowarki-przez-bluetooth),
   podając nową sieć.
   Jeśli ładowarka nie zostanie znaleziona przez Bluetooth, skorzystaj ze sposobu „Bez Bluetootha”.
3. Po oknie wersji ładowarki zobaczysz „Ładowarka otrzymała nowe ustawienia” – kliknij **Zamknij**.
4. Poczekaj, aż na ekranie ładowarki pojawi się ikona Wi-Fi. Naciśnij **OK**, wybierz **Wi-Fi**, włącz **OCPP**
   i wyjdź z menu.

**Bez Bluetootha:**

1. Na ładowarce naciśnij **OK**, wybierz **Wi-Fi**, wyłącz **OCPP** i wyjdź z menu.
2. W Home Assistant usuń ładowarkę: **Ustawienia** → **Urządzenia oraz usługi** → **GNLT EV Charger** → przy
   ładowarce **⋮** → **Usuń** i jeszcze raz **Usuń**.
3. Jeśli ładowarki nie ma w aplikacji EV-Charger, dodaj ją tak jak w kroku 4 sekcji
   [Powrót do aplikacji EV-Charger](#powrót-do-aplikacji-ev-charger).
4. Połącz ładowarkę zgodnie z sekcją [Ręczne połączenie ładowarki](#ręczne-połączenie-ładowarki), podając nową sieć.

## Powrót do aplikacji EV-Charger

1. Na ładowarce naciśnij **OK**, wybierz **Wi-Fi**, wyłącz **OCPP** i wyjdź z menu.
2. W Home Assistant otwórz **Ustawienia** → **Urządzenia oraz usługi** → **GNLT EV Charger**.
3. Przy ładowarce kliknij **⋮** → **Usuń** i jeszcze raz **Usuń**.
4. W aplikacji EV-Charger na telefonie dodaj ładowarkę ponownie: stuknij ikonę **kodu QR** lub **+**, zeskanuj kod QR
   ładowarki, wpisz kod PUK (na odwrocie instrukcji obsługi ładowarki i pod pokrywą ładowarki) i potwierdź dodanie.
5. Na ładowarce naciśnij **OK**, wybierz **Wi-Fi**, włącz **OCPP** i wyjdź z menu. Ładowarka połączy się z aplikacją
   EV-Charger.

## Warto wiedzieć

- Nazwy czujników i przełączników ładowarki są wyświetlane w języku Home Assistanta wybranym w ustawieniach systemu.
- Do panelu „Energia” dodaj czujnik **Energia całkowita (HA)**: uwzględnia wszystkie ładowania, także te rozpoczęte przez samą ładowarkę.
  Liczy energię od chwili dodania ładowarki do Home Assistanta; „Total” na ekranie ładowarki to energia od początku jej pracy.
- Ładowarki z oprogramowaniem `SW:A3B_2.7-HW:B07_0.4` i `SW:A3B_3.1-HW:B07_0.5` przy ładowaniu trójfazowym podają
  moc tylko jednej fazy. Czujnik **Moc** pokazuje wtedy sumę U×I dla wszystkich faz (wartość obliczona); moc podana
  przez ładowarkę znajduje się w atrybutach czujnika.
- Po utracie połączenia ładowarka jest jeszcze przez około dwie minuty widoczna jako połączona.
- Jeśli samochód nie zaczął się ładować, ładowanie może rozpocząć się samo, gdy samochód się wybudzi. Jeśli tego nie
  chcesz, odłącz kabel.
- Niektóre przełączniki mogą być wyszarzone: te funkcje zależą od wersji oprogramowania ładowarki.

## Jeśli coś nie działa

- **Na ładowarce nie ma ikony Wi-Fi.** Na ładowarce naciśnij **OK**, wybierz **Wi-Fi** i sprawdź, czy Wi-Fi jest
  włączone. Sprawdź w ustawieniach routera: sieć 2,4 GHz, zabezpieczenie WPA2 bez trybu mieszanego. Następnie
  ponownie wpisz nazwę sieci i hasło: jeśli ładowarka nie jest jeszcze dodana – od kroku 1 sekcji
  [Połączenie ładowarki przez Bluetooth](#połączenie-ładowarki-przez-bluetooth) albo od kroku 1 sekcji
  [Ręczne połączenie ładowarki](#ręczne-połączenie-ładowarki); jeśli jest już dodana – zobacz
  [Zmiana sieci Wi-Fi](#zmiana-sieci-wi-fi).
- **Ikona Wi-Fi jest widoczna, ale ładowarka nie łączy się z Home Assistantem.** Na ładowarce naciśnij **OK**
  i wybierz **Wi-Fi**: OCPP musi być włączone, a adres serwera musi zawierać adres IP Twojego Home Assistanta. Jeśli
  adres jest inny, zobacz [Zmiana sieci Wi-Fi](#zmiana-sieci-wi-fi) i podaj tę samą sieć – z Bluetoothem lub bez
  niego. Jeśli Home Assistant działa w Dockerze, sprawdź, czy port jest opublikowany. Jeśli wszystko jest poprawne,
  a ładowarka nadal się nie łączy, skontaktuj się z nami – dane kontaktowe poniżej.
- **Ładowarki nie widać przez Bluetooth.** Wyłącz OCPP na ładowarce, zamknij aplikację EV-Charger na telefonie
  i przysuń adapter Bluetooth (albo komputer z Home Assistantem) bliżej ładowarki. Albo połącz ładowarkę
  [ręcznie](#ręczne-połączenie-ładowarki).
- **„Ta ładowarka jest już dodana.”** Ładowarka jest już w **Ustawienia** → **Urządzenia oraz usługi** →
  **GNLT EV Charger**. Aby zmienić sieć Wi-Fi, zobacz [Zmiana sieci Wi-Fi](#zmiana-sieci-wi-fi).
- **„Ta ładowarka jest już konfigurowana w innym oknie.”** Kliknij **Zamknij**. Jeśli w sekcji **Wykryte** jest
  karta tej ładowarki, kliknij na niej **Dodaj** i kontynuuj. Jeśli karty nie ma, uruchom ponownie Home Assistanta
  i zacznij od nowa.
- **„Ta wersja ładowarki nie może współpracować z Home Assistantem przez OCPP.”** Skontaktuj się ze sprzedawcą. Aby
  korzystać z aplikacji EV-Charger, wykonaj kroki 1, 4 i 5 z sekcji
  [Powrót do aplikacji EV-Charger](#powrót-do-aplikacji-ev-charger).
- **Powiadomienie „… ładowarka zatrzymała ładowanie”.** Postępuj zgodnie z treścią powiadomienia. „Prąd powyżej limitu”
  zwykle oznacza, że samochód nie stosuje się do ustawionego limitu prądu i pobiera więcej. Ustaw inną
  wartość **Limitu prądu** – na przykład 10 A albo maksymalną – a następnie odłącz kabel od samochodu i podłącz go ponownie.
- **Odczyty lub ładowanie wyglądają inaczej, niż oczekujesz.** Od razu po takim zdarzeniu pobierz plik diagnostyczny:
  **Ustawienia** → **Urządzenia oraz usługi** → **GNLT EV Charger** → przy ładowarce **⋮** → **Pobierz diagnostykę**.
  Wyślij nam ten plik – dane kontaktowe poniżej. Plik nie zawiera nazwy ani hasła sieci Wi-Fi.

## Pomoc

GNLT · info@gnlt.pl · +48 505 699 273 · https://gnlt.pl

Zgłaszanie problemów: https://github.com/GNLT-EV/gnlt-home-assistant/issues
