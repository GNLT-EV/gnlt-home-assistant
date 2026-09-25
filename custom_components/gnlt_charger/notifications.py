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
    },
}

WATCHED: dict[str, Callable[[Charger], bool]] = {
    "charging_without_session": lambda c: c.charging_without_session,
    "empty_session": lambda c: c.empty_session,
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

    @callback
    def on_update() -> None:
        lang = language_of(hass.config.language)
        for key, flag in WATCHED.items():
            now = flag(charger)
            if now == shown[key]:
                continue
            shown[key] = now
            notification_id = f"gnlt_charger_{charger.identity}_{key}"
            if now:
                title, message = TEXTS[lang][key]
                persistent_notification.async_create(
                    hass, message, title=title.format(name=name), notification_id=notification_id
                )
            else:
                persistent_notification.async_dismiss(hass, notification_id)

    return charger.add_listener(on_update)
