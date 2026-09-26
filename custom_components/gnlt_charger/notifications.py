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
    },
}

WATCHED: dict[str, Callable[[Charger], bool]] = {
    "charging_without_session": lambda c: c.charging_without_session,
    "empty_session": lambda c: c.empty_session,
    "out_of_service": lambda c: c.forced_inoperative_at is not None,
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

    @callback
    def on_update() -> None:
        lang = language_of(hass.config.language)
        for key, flag in WATCHED.items():
            now = flag(charger)
            if now == shown[key]:
                continue
            shown[key] = now
            notification_id = f"gnlt_charger_{charger.identity}_{key}"
            clock = dt_util.now().strftime("%H:%M")
            if now:
                started[key] = clock
                title, message = TEXTS[lang][key]
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
