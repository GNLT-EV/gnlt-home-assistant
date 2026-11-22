"""Notifications and repair issues of a ModBus entry.

Same mechanism as the OCPP ``notifications.py``: a persistent notification in
the language of Home Assistant (ru/be -> Russian, pl -> Polish, else English;
``notifications.language_of``), because Home Assistant does not translate
persistent notifications itself. The likely causes of faults reuse the OCPP
texts (``notifications.ERRORS``). Repair issues are translated by Home
Assistant (``issues`` in the translation files).

* Sources: the station's events (fault, link lost/restored, identity) and the command layer's notifications; repair issues follow the station's flags on every poll.
* Like OCPP, a flag that is on when Home Assistant starts is shown too
.
* State-type notifications are dismissed when their flag clears.
* Shown from a callback scheduled on the loop, never inside the poll.

Wording follows the rules for client texts: what happened and what to do.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from homeassistant.components import persistent_notification
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util

from ..const import DOMAIN
from ..notifications import ERRORS, language_of
from . import const as C
from .runtime import ModbusRuntime

_LOGGER = logging.getLogger(__name__)

ISSUE_NOT_RESPONDING = "station_not_responding"
ISSUE_IDENTITY = "identity_mismatch"
ISSUE_RELAY = "relay_welded"
ISSUE_PNC = "pnc_reset"
ISSUES = (ISSUE_NOT_RESPONDING, ISSUE_IDENTITY, ISSUE_RELAY, ISSUE_PNC)


# State-type notifications: dismissed once the flag is gone.
STATE_PROBLEMS = {
    "empty_session": "empty_session",
    "identity_mismatch": C.PROBLEM_IDENTITY_MISMATCH,
    "relay_welded": C.PROBLEM_RELAY_WELDED,
    "pnc_reset": C.PROBLEM_PNC_RESET,
}

# Reg 4 code -> OCPP error code whose likely cause applies.
OCPP_CAUSE = {
    19: "OverCurrentFailure",
    13: "UnderVoltage",
    14: "OverVoltage",
    15: "HighTemperature",
    21: "EVCommunicationError",
    22: "EVCommunicationError",
    17: "GroundFailure",
    24: "GroundFailure",
    25: "GroundFailure",
}
SCREEN_CODE = C.SCREEN_CODE  # reg 4 code -> code on the charger screen

STATUS_NAMES: dict[str, dict[str, str]] = {
    "en": {
        "unknown": "unknown",
        "standby": "free",
        "connected": "cable connected",
        "starting": "starting",
        "charging": "charging",
        "start_failed": "start failed",
        "finished": "charge finished",
        "fault": "fault",
        "scheduled": "waiting for the charger's own schedule",
        "firmware_update": "firmware update",
        "booting": "starting up",
    },
    "ru": {
        "unknown": "неизвестно",
        "standby": "свободна",
        "connected": "кабель подключён",
        "starting": "запуск",
        "charging": "зарядка",
        "start_failed": "запуск не удался",
        "finished": "зарядка завершена",
        "fault": "неисправность",
        "scheduled": "ожидание собственного расписания станции",
        "firmware_update": "обновление прошивки",
        "booting": "включается",
    },
    "pl": {
        "unknown": "nieznany",
        "standby": "wolna",
        "connected": "kabel podłączony",
        "starting": "rozpoczynanie ładowania",
        "charging": "ładowanie",
        "start_failed": "nieudany start",
        "finished": "ładowanie zakończone",
        "fault": "usterka",
        "scheduled": "oczekiwanie na własny harmonogram ładowarki",
        "firmware_update": "aktualizacja oprogramowania",
        "booting": "uruchamia się",
    },
}

ERROR_NAMES: dict[str, dict[str, str]] = {
    "en": {
        "no_error": "no error",
        "cp_fault": "control pilot (CP) error",
        "emergency_stop": "emergency stop pressed",
        "under_voltage": "mains voltage below normal",
        "over_voltage": "mains voltage above normal",
        "over_temperature": "overheating",
        "meter_fault": "meter error",
        "leakage_fault": "leakage current",
        "output_short": "short circuit at the output",
        "over_current": "current above the limit",
        "vehicle_fault": "car error",
        "vehicle_timeout": "no answer from the car",
        "relay_welded": "power relay stuck (welded)",
        "rcd_fault": "residual current device (RCD) error",
        "pe_fault": "earthing (PE) error",
        "startup_fault": "start-up error",
        "unknown": "error",
    },
    "ru": {
        "no_error": "нет ошибок",
        "cp_fault": "ошибка сигнала CP",
        "emergency_stop": "нажата аварийная кнопка",
        "under_voltage": "напряжение в сети ниже нормы",
        "over_voltage": "напряжение в сети выше нормы",
        "over_temperature": "перегрев",
        "meter_fault": "ошибка счётчика",
        "leakage_fault": "ток утечки",
        "output_short": "короткое замыкание на выходе",
        "over_current": "ток превысил допустимый",
        "vehicle_fault": "ошибка машины",
        "vehicle_timeout": "машина не отвечает",
        "relay_welded": "силовое реле залипло",
        "rcd_fault": "ошибка УЗО",
        "pe_fault": "ошибка заземления (PE)",
        "startup_fault": "ошибка при включении",
        "unknown": "ошибка",
    },
    "pl": {
        "no_error": "brak błędów",
        "cp_fault": "błąd sygnału CP",
        "emergency_stop": "wciśnięty wyłącznik awaryjny",
        "under_voltage": "napięcie sieci poniżej normy",
        "over_voltage": "napięcie sieci powyżej normy",
        "over_temperature": "przegrzanie",
        "meter_fault": "błąd licznika",
        "leakage_fault": "prąd upływu",
        "output_short": "zwarcie na wyjściu",
        "over_current": "prąd powyżej limitu",
        "vehicle_fault": "błąd samochodu",
        "vehicle_timeout": "samochód nie odpowiada",
        "relay_welded": "sklejony przekaźnik mocy",
        "rcd_fault": "błąd wyłącznika różnicowoprądowego",
        "pe_fault": "błąd uziemienia (PE)",
        "startup_fault": "błąd przy uruchamianiu",
        "unknown": "błąd",
    },
}

# Manual recovery steps ModBus cannot do itself.
# The replug advice only for the car-side faults a re-plug clears
# (C.REPLUG_FAULTS = 21/22); "default" is neutral.
RECOVERY: dict[str, dict[int | str, str]] = {
    "en": {
        "default": "If the error stays, check the charger screen and its manual, or contact the seller.",
        11: 'Unplug the cable and plug it in again. If it repeats with this car, check the item "CP mode" in the charger menu.',
        12: "Release the emergency stop button on the charger.",
        17: "This error is cleared only by switching the charger's mains power off and on again. If it repeats, call an electrician.",
        21: "The car does not answer the charger: it may be fully charged or delay charging. Unplug the cable and plug it in again.",
        22: "The car does not answer the charger: it may be fully charged or delay charging. Unplug the cable and plug it in again.",
        23: "The charger needs service. Home Assistant starts no charge while the charger reports this error.",
        25: 'Check the earthing. For a network without earthing, see the item "Grounding selection" in the charger menu.',
    },
    "ru": {
        "default": "Если ошибка не уходит, проверьте экран станции и инструкцию или обратитесь к продавцу.",
        11: "Выньте кабель и вставьте снова. Если с этой машиной повторяется, проверьте пункт «CP mode» в меню станции.",
        12: "Разблокируйте аварийную кнопку на станции.",
        17: "Эта ошибка сбрасывается только выключением и повторным включением питания станции. Если повторяется — обратитесь к электрику.",
        21: "Машина не отвечает станции: возможно, она уже заряжена или откладывает зарядку. Выньте кабель и вставьте снова.",
        22: "Машина не отвечает станции: возможно, она уже заряжена или откладывает зарядку. Выньте кабель и вставьте снова.",
        23: "Станции нужен сервис. Home Assistant не запускает зарядку, пока станция сообщает об этой ошибке.",
        25: "Проверьте заземление. Для сети без заземления — пункт «Grounding selection» в меню станции.",
    },
    "pl": {
        "default": "Jeśli błąd nie znika, sprawdź ekran ładowarki i jej instrukcję albo skontaktuj się ze sprzedawcą.",
        11: "Odłącz kabel i podłącz go ponownie. Jeśli to się powtarza z tym samochodem, sprawdź pozycję „CP mode” w menu ładowarki.",
        12: "Odblokuj wyłącznik awaryjny na ładowarce.",
        17: "Ten błąd kasuje się tylko przez wyłączenie i ponowne włączenie zasilania ładowarki. Jeśli się powtarza, skontaktuj się z elektrykiem.",
        21: "Samochód nie odpowiada ładowarce: może być już naładowany albo odkładać ładowanie na później. Odłącz kabel i podłącz go ponownie.",
        22: "Samochód nie odpowiada ładowarce: może być już naładowany albo odkładać ładowanie na później. Odłącz kabel i podłącz go ponownie.",
        23: "Ładowarka wymaga serwisu. Home Assistant nie rozpocznie ładowania, dopóki ładowarka zgłasza ten błąd.",
        25: "Sprawdź uziemienie. W sieci bez uziemienia zobacz pozycję „Grounding selection” w menu ładowarki.",
    },
}

LINK_HINTS: dict[str, dict[str, str]] = {
    "en": {
        "refused": " Last error: the connection was refused - ModBus TCP is not selected in the charger menu, the charger is in OCPP mode, or it still holds a previous connection.",
        "unreachable": " Last error: the charger is not reachable on the network - check its Wi-Fi and IP address (Wi-Fi menu of the charger).",
        "busy": " Last error: the charger closes the connection or does not answer - another ModBus program or a recent connection.",
    },
    "ru": {
        "refused": " Последняя ошибка: соединение отклонено — в меню станции не выбран ModBus TCP, станция в режиме OCPP или ещё держит прежнее соединение.",
        "unreachable": " Последняя ошибка: станция недоступна в сети — проверьте её Wi-Fi и IP-адрес (меню Wi-Fi станции).",
        "busy": " Последняя ошибка: станция закрывает соединение или не отвечает — подключена другая программа ModBus или соединение было установлено только что.",
    },
    "pl": {
        "refused": " Ostatni błąd: połączenie odrzucone — w menu ładowarki nie wybrano ModBus TCP, ładowarka jest w trybie OCPP albo wciąż utrzymuje poprzednie połączenie.",
        "unreachable": " Ostatni błąd: ładowarka jest niedostępna w sieci — sprawdź jej Wi-Fi i adres IP (menu Wi-Fi ładowarki).",
        "busy": " Ostatni błąd: ładowarka zamyka połączenie albo nie odpowiada — prawdopodobnie połączony jest inny program ModBus albo połączenie nastąpiło przed chwilą.",
    },
}
_HINT_GROUP = {
    C.FAIL_REFUSED: "refused",
    C.FAIL_CONNECT_TIMEOUT: "unreachable",
    C.FAIL_UNREACHABLE: "unreachable",
    C.FAIL_CLOSED_BY_STATION: "busy",
    C.FAIL_NO_ANSWER: "busy",
}

OBSERVED: dict[str, dict[str, str]] = {
    "en": {"status": "status: {}", "current": "current {} A", "phases": "phases: {}", "setpoint": "limit {} A", "none": "no data"},
    "ru": {"status": "состояние: {}", "current": "ток {} А", "phases": "фаз: {}", "setpoint": "ограничение {} А", "none": "нет данных"},
    "pl": {"status": "stan: {}", "current": "prąd {} A", "phases": "fazy: {}", "setpoint": "limit {} A", "none": "brak danych"},
}

REASONS: dict[str, dict[str, str]] = {
    "en": {
        "plug_and_charge_on": 'Plug & Charge is on - switch it off with the "Plug and charge" switch',
        "restart": "Home Assistant restarted",
        "link": "the connection to the charger was lost",
        "stop": "a stop came in between",
        "start": "start",
        "stop_kind": "stop",
        "phase": "phase change",
    },
    "ru": {
        "plug_and_charge_on": "включён режим «Подключай и заряжай» — выключите его переключателем «Подключай и заряжай»",
        "restart": "Home Assistant перезапустился",
        "link": "пропала связь со станцией",
        "stop": "в процессе пришла команда остановки",
        "start": "запуск",
        "stop_kind": "остановка",
        "phase": "смена фаз",
    },
    "pl": {
        "plug_and_charge_on": "włączony jest tryb „Podłącz i ładuj” — wyłącz go przełącznikiem „Podłącz i ładuj”",
        "restart": "Home Assistant uruchomił się ponownie",
        "link": "utracono połączenie z ładowarką",
        "stop": "w międzyczasie wydano polecenie zatrzymania",
        "start": "start",
        "stop_kind": "zatrzymanie",
        "phase": "zmiana faz",
    },
}

RESUME: dict[str, dict[str, str]] = {
    "en": {
        "yes": "The charge was running before: Home Assistant starts it again.",
        "off": 'The charge was running before; "Continue after a power cut" is off, so it is not started again.',
        "none": "",
    },
    "ru": {
        "yes": "До этого шла зарядка: Home Assistant запустит её снова.",
        "off": "До этого шла зарядка; «Продолжать после отключения питания» выключено, поэтому она не запускается снова.",
        "none": "",
    },
    "pl": {
        "yes": "Wcześniej trwało ładowanie: Home Assistant uruchomi je ponownie.",
        "off": "Wcześniej trwało ładowanie; opcja „Kontynuuj po zaniku zasilania” jest wyłączona, więc ładowanie nie zostanie wznowione.",
        "none": "",
    },
}

TEXTS: dict[str, dict[str, tuple[str, str]]] = {
    "en": {
        "faulted": ("{name}: the charger reports an error", "The charger reports: {error}. {cause}{recovery}"),
        "link_lost": (
            "{name}: no connection to the charger",
            "Home Assistant has had no answer from the charger over ModBus for 3 minutes. Check in this order: "
            "1) wait 3 minutes - after a reconnect the charger may stay silent, and after a Home Assistant crash it "
            "may still hold the previous connection; 2) no other program uses ModBus of this charger (evcc, other "
            "integrations, other devices on port 502); 3) the charger is not switched to OCPP; 4) the IP address of "
            "the charger has not changed (Wi-Fi menu of the charger); 5) in the charger menu switch ModBus TCP or "
            "load management off and on; 6) switch the charger's mains power off for at least 10 seconds, then wait "
            "about 20 seconds.{hint}",
        ),
        "identity_mismatch": (
            "{name}: another charger answers at this address",
            "Another device (serial number {serial_seen}) answers at the address of this charger - the router may "
            "have given the address to another unit. Home Assistant sends no commands until this charger answers "
            "again. Reserve a fixed IP address for the charger in the router and enter it in the charger settings.",
        ),
        "relay_welded": (
            "{name}: power relay stuck - service needed",
            "The charger reports a stuck (welded) power relay. Home Assistant starts no charge while the charger "
            "reports this error. The charger needs service.",
        ),
        "pnc_reset": (
            "{name}: Plug & Charge switched on by the charger",
            "After a power cut the charger switched Plug & Charge back on: it starts charging by itself and does not "
            "take start and stop commands from Home Assistant. Open Settings - Repairs to switch it off with one click.",
        ),
        "empty_session": (
            "{name}: the car did not start charging",
            "The charger was switched on, but the car did not take current for 3 minutes. Check the car: its charge "
            "limit, its own charging schedule or sleep mode.",
        ),
        "station_restarted": ("{name}: the charger restarted", "The charger restarted, for example after a power cut. {resume}"),
        "self_start": (
            "{name}: the charger started charging by itself",
            "Charging started without a command from Home Assistant (Plug & Charge, a button or the charger's own "
            "schedule). Home Assistant does not stop it. The charger now: {observed}.",
        ),
        "self_start_ended": (
            "{name}: the charger charged by itself",
            "The charger charged by itself from {start} to {end} (Plug & Charge, a button or the charger's own "
            "schedule). This charge has stopped. All events are in \"Activity\".",
        ),
        "finished_replug": (
            "{name}: re-plug before the next start",
            "The charger reports the charge as finished. Many cars need the cable unplugged and plugged in again "
            "before the next start.",
        ),
        "current_clamped": (
            "{name}: current limit lowered",
            "The current limit {desired} A is above what the charger allows now. Charging uses {applied} A.",
        ),
        "generic": ("{name}: message from the charger", "{kind}"),
    },
    "ru": {
        "faulted": ("{name}: станция сообщает об ошибке", "Станция сообщила: {error}. {cause}{recovery}"),
        "link_lost": (
            "{name}: нет связи со станцией",
            "Станция 3 минуты не отвечает Home Assistant по ModBus. Проверьте по порядку: "
            "1) подождите 3 минуты — после переподключения станция может молчать, а после сбоя Home Assistant она "
            "может ещё держать прежнее соединение; 2) ModBus этой станции не использует другая программа (evcc, "
            "другие интеграции, другие устройства на порту 502); 3) станция не переключена на OCPP; 4) не сменился "
            "IP-адрес станции (меню Wi-Fi станции); 5) в меню станции выключите и снова включите ModBus TCP или "
            "балансировку нагрузки; 6) выключите питание станции не меньше чем на 10 секунд и подождите около 20 секунд.{hint}",
        ),
        "identity_mismatch": (
            "{name}: по этому адресу отвечает другая станция",
            "По адресу этой станции отвечает другое устройство (серийный номер {serial_seen}) — роутер мог отдать "
            "адрес другому устройству. Home Assistant не отправляет команды, пока снова не ответит эта станция. "
            "Закрепите за станцией постоянный IP-адрес в роутере и укажите его в настройках станции.",
        ),
        "relay_welded": (
            "{name}: залипло силовое реле — нужен сервис",
            "Станция сообщает о залипшем силовом реле. Home Assistant не запускает зарядку, пока станция сообщает "
            "об этой ошибке. Станции нужен сервис.",
        ),
        "pnc_reset": (
            "{name}: станция сама включила «Подключай и заряжай»",
            "После отключения питания станция снова включила режим «Подключай и заряжай»: она сама начинает зарядку и "
            "не принимает команды запуска и остановки от Home Assistant. Откройте «Настройки» → «Ремонт», чтобы "
            "выключить режим одним нажатием.",
        ),
        "empty_session": (
            "{name}: машина не начала заряжаться",
            "Станция включилась, но машина 3 минуты не брала ток. Проверьте машину: её лимит заряда, собственное "
            "расписание зарядки или режим сна.",
        ),
        "station_restarted": ("{name}: станция перезапустилась", "Станция перезапустилась, например после отключения питания. {resume}"),
        "self_start": (
            "{name}: станция сама начала зарядку",
            "Зарядка началась без команды Home Assistant («Подключай и заряжай», кнопка или собственное расписание "
            "станции). Home Assistant её не останавливает. Станция сейчас: {observed}.",
        ),
        "self_start_ended": (
            "{name}: станция сама заряжала",
            "Станция сама заряжала с {start} до {end} («Подключай и заряжай», кнопка или собственное расписание "
            "станции). Сейчас эта зарядка остановлена. Все события — в разделе «Активность».",
        ),
        "finished_replug": (
            "{name}: перед следующим запуском переподключите кабель",
            "Станция сообщает, что зарядка завершена. Многим машинам перед следующим запуском нужно вынуть кабель "
            "и вставить снова.",
        ),
        "current_clamped": (
            "{name}: ограничение тока снижено",
            "Ограничение тока {desired} А выше, чем станция сейчас допускает. Зарядка идёт с током {applied} А.",
        ),
        "generic": ("{name}: сообщение станции", "{kind}"),
    },
    "pl": {
        "faulted": ("{name}: ładowarka zgłasza błąd", "Ładowarka zgłosiła: {error}. {cause}{recovery}"),
        "link_lost": (
            "{name}: brak połączenia z ładowarką",
            "Ładowarka od 3 minut nie odpowiada Home Assistant przez ModBus. Sprawdź po kolei: "
            "1) poczekaj 3 minuty — po ponownym połączeniu ładowarka może milczeć, a po awarii systemu Home Assistant ładowarka może "
            "wciąż utrzymywać poprzednie połączenie; 2) żaden inny program nie korzysta z ModBus tej ładowarki (evcc, "
            "inne integracje, inne urządzenia na porcie 502); 3) ładowarka nie jest przełączona na OCPP; 4) adres IP "
            "ładowarki się nie zmienił (menu Wi-Fi ładowarki); 5) w menu ładowarki wyłącz i ponownie włącz ModBus TCP "
            "lub równoważenie obciążenia; 6) wyłącz zasilanie ładowarki na co najmniej 10 sekund i poczekaj około 20 sekund.{hint}",
        ),
        "identity_mismatch": (
            "{name}: pod tym adresem odpowiada inna ładowarka",
            "Pod adresem tej ładowarki odpowiada inne urządzenie (numer seryjny {serial_seen}) — router mógł przydzielić "
            "ten adres innemu urządzeniu. Home Assistant nie wysyła poleceń, dopóki ta ładowarka znów nie odpowie. "
            "Zarezerwuj w routerze stały adres IP dla ładowarki i wpisz go w ustawieniach ładowarki.",
        ),
        "relay_welded": (
            "{name}: sklejony przekaźnik mocy — potrzebny serwis",
            "Ładowarka zgłasza sklejony przekaźnik mocy. Home Assistant nie rozpocznie ładowania, dopóki ładowarka "
            "zgłasza ten błąd. Ładowarka wymaga serwisu.",
        ),
        "pnc_reset": (
            "{name}: ładowarka sama włączyła tryb „Podłącz i ładuj”",
            "Po zaniku zasilania ładowarka ponownie włączyła tryb „Podłącz i ładuj”: sama rozpoczyna ładowanie i nie "
            "przyjmuje poleceń startu i zatrzymania z Home Assistant. Otwórz „Ustawienia → Naprawy”, aby wyłączyć ten "
            "tryb jednym kliknięciem.",
        ),
        "empty_session": (
            "{name}: samochód nie zaczął się ładować",
            "Ładowarka się włączyła, ale samochód przez 3 minuty nie pobierał prądu. Sprawdź samochód: jego limit "
            "ładowania, własny harmonogram ładowania albo tryb uśpienia.",
        ),
        "station_restarted": (
            "{name}: ładowarka uruchomiła się ponownie",
            "Ładowarka uruchomiła się ponownie, na przykład po zaniku zasilania. {resume}",
        ),
        "self_start": (
            "{name}: ładowarka sama rozpoczęła ładowanie",
            "Ładowanie rozpoczęło się bez polecenia z Home Assistant (tryb „Podłącz i ładuj”, przycisk lub własny "
            "harmonogram ładowarki). Home Assistant go nie zatrzymuje. Stan ładowarki: {observed}.",
        ),
        "self_start_ended": (
            "{name}: ładowarka ładowała samodzielnie",
            "Ładowarka ładowała samodzielnie od {start} do {end} (tryb „Podłącz i ładuj”, przycisk lub własny "
            "harmonogram ładowarki). To ładowanie zostało już zatrzymane. Wszystkie "
            "zdarzenia znajdziesz w sekcji „Aktywność”.",
        ),
        "finished_replug": (
            "{name}: przed kolejnym startem podłącz kabel ponownie",
            "Ładowarka zgłasza zakończenie ładowania. Wiele samochodów wymaga odłączenia i ponownego podłączenia "
            "kabla przed kolejnym startem.",
        ),
        "current_clamped": (
            "{name}: limit prądu obniżony",
            "Limit prądu {desired} A jest wyższy, niż ładowarka teraz dopuszcza. Ładowanie odbywa się prądem {applied} A.",
        ),
        "generic": ("{name}: komunikat ładowarki", "{kind}"),
    },
}


# what an automatic feature would do, or why it does not (attribute
# ``would_do`` of the status sensor). The command layer gives "origin: reason";
# attributes are not translated by Home Assistant, so the text is built here.
WOULD_DO_ORIGINS: dict[str, dict[str, str]] = {
    "en": {
        "schedule": "Schedule",
        "auto_start": "Start when the cable is connected",
        "resume": "Continue after a power cut",
        "empty_session": "Stop of a charge the car did not take",
        "restore": "Restoring the current limit",
        "sequence": "Command",
    },
    "ru": {
        "schedule": "Расписание",
        "auto_start": "Запуск при подключении кабеля",
        "resume": "Продолжение после отключения питания",
        "empty_session": "Остановка зарядки, которую машина не приняла",
        "restore": "Восстановление ограничения тока",
        "sequence": "Команда",
    },
    "pl": {
        "schedule": "Harmonogram",
        "auto_start": "Start po podłączeniu kabla",
        "resume": "Kontynuacja po zaniku zasilania",
        "empty_session": "Zatrzymanie ładowania, w którym samochód nie pobierał prądu",
        "restore": "Przywrócenie limitu prądu",
        "sequence": "Polecenie",
    },
}
WOULD_DO_REASONS: dict[str, dict[str, str]] = {
    "en": {
        "plug_and_charge_on": "not possible - Plug & Charge is on",
        "cooldown": "waits 3 minutes after the last attempt",
        "busy": "another command is running",
        "mb_faulted": "the charger reports an error",
        "mb_faulted_replug": "the charger reports an error",
        "replug": "re-plug the cable first",
        "not_connected": "no connection to the charger",
        "other": "not done",
    },
    "ru": {
        "plug_and_charge_on": "невозможно — включён «Подключай и заряжай»",
        "cooldown": "ждёт 3 минуты после последней попытки",
        "busy": "выполняется другая команда",
        "mb_faulted": "станция сообщает об ошибке",
        "mb_faulted_replug": "станция сообщает об ошибке",
        "replug": "сначала переподключите кабель",
        "not_connected": "нет связи со станцией",
        "other": "не выполнено",
    },
    "pl": {
        "plug_and_charge_on": "niemożliwe — włączony jest tryb „Podłącz i ładuj”",
        "cooldown": "czeka 3 minuty po ostatniej próbie",
        "busy": "trwa wykonywanie innego polecenia",
        "mb_faulted": "ładowarka zgłasza błąd",
        "mb_faulted_replug": "ładowarka zgłasza błąd",
        "replug": "najpierw podłącz kabel ponownie",
        "not_connected": "brak połączenia z ładowarką",
        "other": "nie wykonano",
    },
}


def would_do_text(lang: str, key: str | None) -> str | None:
    """"schedule: busy" -> "Schedule: another command is running"."""
    if not key:
        return None
    origin, _sep, reason = key.partition(":")
    origin, reason = origin.strip(), reason.strip()
    origins, reasons = WOULD_DO_ORIGINS[lang], WOULD_DO_REASONS[lang]
    head = origins.get(origin) or origins["sequence"]
    return f"{head}: {reasons.get(reason, reasons['other'])}"


class _Safe(dict):
    def __missing__(self, key: str) -> str:
        return "?"


def _num(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:.1f}"
    return "?" if value is None else str(value)


def observed_text(lang: str, observed: dict[str, Any] | None) -> str:
    words = OBSERVED[lang]
    obs = observed or {}
    parts = []
    status = obs.get("status")
    if status is not None:
        key = C.STATUS_KEYS.get(status, C.STATUS_UNKNOWN) if isinstance(status, int) else str(status)
        parts.append(words["status"].format(STATUS_NAMES[lang].get(key, key)))
    if obs.get("current_a") is not None:
        parts.append(words["current"].format(_num(float(obs["current_a"]))))
    if obs.get("phases") is not None:
        parts.append(words["phases"].format(obs["phases"]))
    if obs.get("setpoint_a") is not None:
        parts.append(words["setpoint"].format(_num(float(obs["setpoint_a"]))))
    return ", ".join(parts) or words["none"]


def fault_texts(lang: str, code: int | None) -> dict[str, str]:
    """Name, likely cause and manual recovery of a reg-4 code."""
    key = C.ERROR_KEYS.get(code, C.ERROR_UNKNOWN) if code is not None else C.ERROR_UNKNOWN
    name = ERROR_NAMES[lang].get(key, ERROR_NAMES[lang]["unknown"])
    if key == C.ERROR_UNKNOWN:
        name = f"{name} {code}"
    if code in SCREEN_CODE:
        name = f"{name} ({SCREEN_CODE[code]})"
    ocpp = OCPP_CAUSE.get(code or 0)
    cause = ERRORS[lang].get(ocpp, ("", ""))[1] if ocpp else ""
    recovery = RECOVERY[lang].get(code or 0, RECOVERY[lang]["default"])
    return {"error": name, "cause": cause, "recovery": recovery}


def render(lang: str, kind: str, data: dict[str, Any], name: str) -> tuple[str, str]:
    """Title and message of a notification ``kind`` in ``lang``."""
    texts = TEXTS[lang]
    values: dict[str, Any] = {"name": name, "kind": kind}
    for key in ("phases", "station_phases", "serial_seen", "value"):
        values[key] = _num(data.get(key))
    for key, src in (("desired", "desired_a"), ("applied", "applied_a"), ("current", "current_a")):
        values[key] = _num(None if data.get(src) is None else float(data[src]))
    values["observed"] = observed_text(lang, data.get("observed"))
    reasons = REASONS[lang]
    reason = data.get("reason")
    values["reason"] = reasons.get(str(reason), str(reason) if reason else "?")
    values["why"] = reasons.get(str(reason), str(reason) if reason else "?")
    seq_kind = data.get("kind")
    what = reasons.get("stop_kind" if seq_kind == "stop" else str(seq_kind), str(seq_kind) if seq_kind else "?")
    step = data.get("step")
    values["what"] = f"{what}, {step}" if step is not None else what
    if data.get("resume"):
        values["resume"] = RESUME[lang]["yes"]
    elif data.get("record_open_before"):
        values["resume"] = RESUME[lang]["off"]
    else:
        values["resume"] = RESUME[lang]["none"]
    values["hint"] = LINK_HINTS[lang].get(_HINT_GROUP.get(str(data.get("hint")), ""), "")
    values["start"] = str(data.get("start") or "?")
    values["end"] = str(data.get("end") or "?")
    if kind == "faulted":
        values.update(fault_texts(lang, data.get("code")))
    title, message = texts.get(kind) or texts["generic"]
    return title.format_map(_Safe(values)).strip(), message.format_map(_Safe(values)).strip()


# --- repair issues ---------------------------------------------------------------------


def _issue_id(key: str, serial: str) -> str:
    return f"{key}_{serial}"


def async_delete_issues(hass: HomeAssistant, serial: str) -> None:
    for key in ISSUES:
        ir.async_delete_issue(hass, DOMAIN, _issue_id(key, serial))


def _create_issue(hass: HomeAssistant, runtime: ModbusRuntime, key: str, **placeholders: str) -> None:
    fixable = key == ISSUE_PNC
    ir.async_create_issue(
        hass,
        DOMAIN,
        _issue_id(key, runtime.serial),
        is_fixable=fixable,
        is_persistent=False,
        severity=ir.IssueSeverity.ERROR if key in (ISSUE_RELAY, ISSUE_IDENTITY) else ir.IssueSeverity.WARNING,
        translation_key=key,
        translation_placeholders={"name": runtime.title, **placeholders},
        data={"entry_id": runtime.entry.entry_id, "serial": runtime.serial} if fixable else None,
    )


# --- the watcher ---------------------------------------------------------------------------


def watch(hass: HomeAssistant, runtime: ModbusRuntime) -> Callable[[], None]:
    """Raise and clear notifications and repair issues of one station."""
    station = runtime.station
    serial = runtime.serial
    shown: dict[str, float | None] = {}  # kind -> snapshot time when shown
    # Issues raised by an earlier runtime of this entry (before a reload) are
    # ours to clear too.
    registry = ir.async_get(hass)
    issues: set[str] = {key for key in ISSUES if registry.async_get_issue(DOMAIN, _issue_id(key, serial)) is not None}

    def notification_id(kind: str) -> str:
        return f"{DOMAIN}_{serial}_mb_{kind}"

    @callback
    def _show(kind: str, data: dict[str, Any]) -> None:
        lang = language_of(hass.config.language)
        title, message = render(lang, kind, data, runtime.title)
        persistent_notification.async_create(hass, message, title=title, notification_id=notification_id(kind))

    def show(kind: str, data: dict[str, Any]) -> None:
        shown[kind] = station.snapshot.time
        # Own callback on the loop: a notification never runs inside the poll.
        hass.loop.call_soon(_show, kind, dict(data))

    def dismiss(kind: str) -> None:
        if shown.pop(kind, "absent") != "absent":
            hass.loop.call_soon(persistent_notification.async_dismiss, hass, notification_id(kind))

    def issue(key: str, on: bool, **placeholders: str) -> None:
        if on and key not in issues:
            issues.add(key)
            _create_issue(hass, runtime, key, **placeholders)
        elif not on and key in issues:
            issues.discard(key)
            ir.async_delete_issue(hass, DOMAIN, _issue_id(key, serial))
        elif not on and registry.async_get_issue(DOMAIN, _issue_id(key, serial)) is not None:
            ir.async_delete_issue(hass, DOMAIN, _issue_id(key, serial))  # idempotent clear

    def on_event(name: str, data: dict[str, Any]) -> None:
        if name == C.EVENT_FAULT:
            if data.get("code") == C.ERROR_RELAY_WELDED:
                return  # repair issue + its own notification (on_snapshot)
            # also a fault already there when HA starts (OCPP)
            shown.pop("faulted", None)  # a new code is shown again
            show("faulted", {"code": data.get("code")})
        elif name == C.EVENT_FAULT_CLEARED:
            dismiss("faulted")
        elif name == C.EVENT_LINK_LOST:
            show("link_lost", {"hint": data.get("hint")})
            issue(ISSUE_NOT_RESPONDING, True)
        elif name == C.EVENT_LINK_RESTORED:
            dismiss("link_lost")  # only if it was shown
            issue(ISSUE_NOT_RESPONDING, False)
        elif name == C.EVENT_IDENTITY_MISMATCH:
            seen = str(data.get("serial_seen") or "?")
            show("identity_mismatch", {"serial_seen": seen})
            issue(ISSUE_IDENTITY, True, serial_seen=seen)
        elif name == C.EVENT_IDENTITY_RESTORED:
            dismiss("identity_mismatch")
            issue(ISSUE_IDENTITY, False)

    def on_note(kind: str, data: dict[str, Any]) -> None:
        show(kind, data)  # also on start-up, like OCPP

    self_start: list[str | None] = [None]  # HH:MM it began, while shown

    def watch_self_start() -> None:
        """Like OCPP's charging_without_session: shown while it lasts,
        then replaced by a summary with the start and end time."""
        now = runtime.commander.charging_without_session
        clock = dt_util.now().strftime("%H:%M")
        if now and self_start[0] is None:
            self_start[0] = clock
            show("self_start", {"observed": runtime.commander._observed()})
        elif not now and self_start[0] is not None:
            began, self_start[0] = self_start[0], None
            shown.pop("self_start", None)
            hass.loop.call_soon(
                _show_as, "self_start_ended", "self_start", {"start": began, "end": clock}
            )

    @callback
    def _show_as(kind: str, slot: str, data: dict[str, Any]) -> None:
        lang = language_of(hass.config.language)
        title, message = render(lang, kind, data, runtime.title)
        persistent_notification.async_create(hass, message, title=title, notification_id=notification_id(slot))

    def on_snapshot(snap: Any) -> None:
        watch_self_start()
        problems = snap.problems
        relay = snap.error == C.ERROR_RELAY_WELDED
        if relay and ISSUE_RELAY not in issues:
            show("relay_welded", {})
        issue(ISSUE_RELAY, relay)
        pnc = C.PROBLEM_PNC_RESET in problems
        if pnc and ISSUE_PNC not in issues:
            show("pnc_reset", {})
        issue(ISSUE_PNC, pnc)
        if snap.time is None:
            return
        for kind, problem in STATE_PROBLEMS.items():
            if kind not in shown or problem in problems:
                continue
            at = shown[kind]
            # Only a poll after the one the notification came with can clear it.
            if at is None or snap.time > at:
                dismiss(kind)

    unsubscribe = [
        station.subscribe_events(on_event),
        runtime.commander.subscribe_notifications(on_note),
        station.subscribe(on_snapshot),
    ]

    def stop() -> None:
        for unsub in unsubscribe:
            unsub()

    return stop
