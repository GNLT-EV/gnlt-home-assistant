"""Notifications for situations that need the person.

A yes/no sensor under "Diagnostics" is the record; the notification is what
the person actually sees - the same idea as the platform's owner alerts. Text
says what happened and what to do, in the language of Home Assistant (a
persistent notification is not translated by Home Assistant itself).

Wording follows the rules for client texts: behaviour and action, no talk of
defects.
"""

from __future__ import annotations

from collections.abc import Callable

from homeassistant.components import persistent_notification
from homeassistant.core import HomeAssistant, callback
from homeassistant.util import dt as dt_util

from .charger import Charger

TEXTS: dict[str, dict[str, tuple[str, str]]] = {
    "ru": {
        "charging_without_session": (
            "{name}: зарядка идёт без управления",
            "Станция продолжила зарядку сама — например, когда вернулось электричество. "
            "Показания идут, но обычной командой такую зарядку не остановить. "
            "Чтобы остановить, нажмите «Прервать зарядку» на странице станции или выньте кабель.",
        ),
        "empty_session": (
            "{name}: машина не начала заряжаться",
            "Станция приняла команду, но машина не взяла ток — так бывает, если после прошлой "
            "зарядки машина уже уснула. Если она проснётся — например, когда откроете дверь, — "
            "зарядка может пойти сама. Если нет — выньте кабель, вставьте снова и запустите зарядку.",
        ),
        "charging_without_session_ended": (
            "{name}: станция сама заряжала",
            "Станция сама заряжала с {start} до {end} — например, после возврата электричества или перезапуска. Сейчас эта зарядка остановлена. Все события — в разделе «Активность».",
        ),
        "out_of_service": (
            "{name}: разъём станции отключён",
            "Станция сама заряжала и не приняла команду остановки, поэтому Home Assistant отключил разъём, чтобы остановить зарядку. Примерно через 10 минут разъём включится сам. Если не хотите, чтобы зарядка продолжилась, выньте кабель из машины.",
        ),
        "faulted": (
            "{name}: станция остановила зарядку",
            "Станция сообщила: {error}. {cause}Чтобы продолжить, выньте кабель из машины и вставьте снова. "
            "Если сообщение не исчезло, нажмите «Перезагрузить» на странице станции.",
        ),
    },
    "pl": {
        "charging_without_session": (
            "{name}: ładowanie trwa bez sterowania",
            "Ładowarka sama wznowiła ładowanie – na przykład po powrocie zasilania. "
            "Odczyty są przesyłane, ale takiego ładowania nie można zatrzymać zwykłym poleceniem. "
            "Aby je zatrzymać, naciśnij „Przerwij ładowanie” na stronie ładowarki w Home Assistant "
            "lub odłącz kabel.",
        ),
        "empty_session": (
            "{name}: samochód nie zaczął się ładować",
            "Ładowarka przyjęła polecenie, ale samochód nie pobrał prądu. Zdarza się to, gdy po "
            "poprzednim ładowaniu auto przeszło już w tryb uśpienia. Jeśli się wybudzi – na przykład "
            "gdy otworzysz drzwi – ładowanie może się rozpocząć samo. Jeśli nie, odłącz kabel, "
            "podłącz go ponownie i uruchom ładowanie.",
        ),
        "charging_without_session_ended": (
            "{name}: ładowarka ładowała samodzielnie",
            "Ładowarka ładowała samodzielnie od {start} do {end} – na przykład po przywróceniu zasilania lub ponownym uruchomieniu ładowarki. To ładowanie zostało już zatrzymane. Wszystkie zdarzenia znajdziesz w sekcji „Aktywność”.",
        ),
        "out_of_service": (
            "{name}: złącze ładowarki wyłączone",
            "Ładowarka ładowała samodzielnie i nie wykonała polecenia zatrzymania, dlatego Home Assistant wyłączył złącze, aby przerwać ładowanie. Za około 10 minut złącze włączy się ponownie samo. Jeśli nie chcesz, żeby ładowanie się wznowiło, odłącz kabel od samochodu.",
        ),
        "faulted": (
            "{name}: ładowarka zatrzymała ładowanie",
            "Ładowarka zgłosiła: {error}. {cause}Aby wznowić ładowanie, odłącz kabel od samochodu i podłącz go ponownie. "
            "Jeśli komunikat nie zniknie, naciśnij „Uruchom ponownie” na stronie ładowarki.",
        ),
    },
    "en": {
        "charging_without_session": (
            "{name}: charging without control",
            "The charger continued charging by itself - for example, when the power came back. "
            "Readings are coming in, but this charge cannot be stopped with the usual command. "
            "To stop it, press \"Interrupt charging\" on the charger page or unplug the cable.",
        ),
        "empty_session": (
            "{name}: the car did not start charging",
            "The charger accepted the command, but the car did not take current - this happens "
            "when the car has gone to sleep after the previous charge. If it wakes up - for example, "
            "when you open the door - charging may start by itself. If not, unplug the cable, plug it "
            "in again and start charging.",
        ),
        "charging_without_session_ended": (
            "{name}: the charger charged by itself",
            "The charger charged by itself from {start} to {end} - for example after the power came back or a restart. This charge has stopped. All events are in \"Activity\".",
        ),
        "out_of_service": (
            "{name}: the charger's connector is out of service",
            "The charger was charging by itself and refused the stop command, so Home Assistant took the connector out of service to stop the charge. It returns to service by itself in about 10 minutes. If you do not want the charge to go on, unplug the cable from the car.",
        ),
        "faulted": (
            "{name}: the charger stopped charging",
            "The charger reported: {error}. {cause}To continue, unplug the cable from the car and plug it in again. "
            'If the message stays, press "Restart" on the charger page.',
        ),
    },
}

# What the charger reports (OCPP errorCode) and its most likely cause, as the
# person can act on it. Codes without a known cause get only the general advice.
ERRORS: dict[str, dict[str, tuple[str, str]]] = {
    "ru": {
        "OverCurrentFailure": (
            "ток превысил допустимый",
            "Обычно это значит, что машина взяла больше тока, чем разрешено. Некоторые машины не выполняют "
            "отдельные значения ограничения тока (например, 11–15 А) и берут больше. Поставьте другое "
            "«Ограничение тока» — например, 10 А или максимальное. ",
        ),
        "GroundFailure": (
            "ошибка заземления или утечка тока",
            "Проверьте заземление розетки или линии, к которой подключена станция; если повторяется — обратитесь к электрику. ",
        ),
        "OverVoltage": ("напряжение в сети выше нормы", "Зарядка возможна, когда напряжение вернётся в норму. "),
        "UnderVoltage": ("напряжение в сети ниже нормы", "Зарядка возможна, когда напряжение вернётся в норму. "),
        "HighTemperature": ("перегрев", "Дайте станции остыть; если повторяется — уменьшите «Ограничение тока». "),
        "EVCommunicationError": ("нет связи с машиной по кабелю", "Проверьте, что разъём плотно вставлен в машину. "),
    },
    "pl": {
        "OverCurrentFailure": (
            "prąd powyżej limitu",
            "Zwykle oznacza to, że samochód pobrał więcej prądu, niż było dozwolone. Niektóre samochody nie "
            "respektują niektórych wartości limitu prądu (na przykład 11–15 A) i pobierają więcej. Ustaw inną "
            "wartość „Limitu prądu” – na przykład 10 A albo maksymalną. ",
        ),
        "GroundFailure": (
            "błąd uziemienia lub upływ prądu",
            "Sprawdź uziemienie gniazda lub obwodu, do którego podłączona jest ładowarka; jeśli błąd się powtarza, skontaktuj się z elektrykiem. ",
        ),
        "OverVoltage": ("napięcie sieci powyżej normy", "Ładowanie będzie możliwe, gdy napięcie wróci do normy. "),
        "UnderVoltage": ("napięcie sieci poniżej normy", "Ładowanie będzie możliwe, gdy napięcie wróci do normy. "),
        "HighTemperature": ("przegrzanie", "Poczekaj, aż ładowarka ostygnie; jeśli to się powtarza, zmniejsz „Limit prądu”. "),
        "EVCommunicationError": ("brak komunikacji z samochodem przez kabel", "Sprawdź, czy wtyczka jest dobrze włożona do gniazda w samochodzie. "),
    },
    "en": {
        "OverCurrentFailure": (
            "current above the limit",
            "Usually this means the car took more current than allowed. Some cars do not follow certain current "
            'limits (for example 11-15 A) and take more. Set a different "Current limit" - for example 10 A or '
            "the maximum. ",
        ),
        "GroundFailure": (
            "earth fault or leakage current",
            "Check the earthing of the socket or the line the charger is connected to; if it repeats, call an electrician. ",
        ),
        "OverVoltage": ("mains voltage above normal", "Charging is possible again when the voltage is back to normal. "),
        "UnderVoltage": ("mains voltage below normal", "Charging is possible again when the voltage is back to normal. "),
        "HighTemperature": ("overheating", 'Let the charger cool down; if it repeats, lower the "Current limit". '),
        "EVCommunicationError": ("no communication with the car over the cable", "Check that the plug is fully inserted in the car. "),
    },
}


# A Faulted status without an error code.
NO_CODE = {"ru": "ошибка", "pl": "błąd", "en": "an error"}


def error_text(lang: str, charger: Charger) -> tuple[str, str]:
    """(what the charger reported, its likely cause) in the notification language."""
    code = charger.error_code or ""
    name, cause = ERRORS[lang].get(code, (code or NO_CODE[lang], ""))
    if charger.vendor_error:
        name = f"{name} ({charger.vendor_error})"
    return name, cause


WATCHED: dict[str, Callable[[Charger], bool]] = {
    "charging_without_session": lambda c: c.charging_without_session,
    "empty_session": lambda c: c.empty_session,
    "out_of_service": lambda c: c.forced_inoperative_at is not None,
    "faulted": lambda c: c.status == "Faulted",
}

# Events worth knowing about after they are over: when the flag goes off the
# notification is not dismissed but replaced by a summary with the times; the
# person closes it. All others are states: shown while they last.
SUMMARY_WHEN_OVER: dict[str, str] = {
    "charging_without_session": "charging_without_session_ended",
}


def language_of(ha_language: str | None) -> str:
    """Language of the notification texts: Russian for ru/be, Polish for pl,
    English for everything else (the default, as for the rest of the UI)."""
    code = (ha_language or "").lower()
    if code.startswith(("ru", "be")):
        return "ru"
    if code.startswith("pl"):
        return "pl"
    return "en"


def watch(hass: HomeAssistant, charger: Charger, name: str) -> Callable[[], None]:
    """Raise and clear notifications as the charger's flags change."""
    shown: dict[str, bool] = {key: False for key in WATCHED}
    started: dict[str, str] = {}
    fault_code: list[tuple[str | None, str | None]] = [(None, None)]

    @callback
    def on_update() -> None:
        lang = language_of(hass.config.language)
        for key, flag in WATCHED.items():
            now = flag(charger)
            if key == "faulted" and now:
                code = (charger.error_code, charger.vendor_error)
                if shown[key] and code != fault_code[0]:
                    shown[key] = False  # another error while still Faulted: show the new one
                fault_code[0] = code
            if now == shown[key]:
                continue
            shown[key] = now
            notification_id = f"gnlt_charger_{charger.identity}_{key}"
            clock = dt_util.now().strftime("%H:%M")
            if now:
                started[key] = clock
                title, message = TEXTS[lang][key]
                if key == "faulted":
                    error, cause = error_text(lang, charger)
                    message = message.format(error=error, cause=cause)
                persistent_notification.async_create(
                    hass, message, title=title.format(name=name), notification_id=notification_id
                )
            elif key in SUMMARY_WHEN_OVER:
                title, message = TEXTS[lang][SUMMARY_WHEN_OVER[key]]
                persistent_notification.async_create(
                    hass,
                    message.format(start=started.pop(key, "?"), end=clock),
                    title=title.format(name=name),
                    notification_id=notification_id,
                )
            else:
                persistent_notification.async_dismiss(hass, notification_id)

    return charger.add_listener(on_update)
