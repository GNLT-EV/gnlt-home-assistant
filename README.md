# GNLT EV Charger for Home Assistant

🇵🇱 [Instrukcja po polsku](README.pl.md)

Local integration for GNLT EV chargers (portable and wall-mounted). The charger talks to Home Assistant
directly over OCPP 1.6J in your home network — no cloud, no external servers, no internet needed.

## Features

- Start and stop charging, charging current limit (also while charging)
- Power, current, voltage, energy of the charge and total energy — ready for the Home Assistant Energy dashboard
- Built-in charging schedule, electricity price (single or two-zone tariff), cost of the charge, day and month totals
- Warnings: the car did not take the charge, charging started by the charger itself, connector out of service
- Setup over Bluetooth (Wi-Fi and server address are written to the charger) or manually by serial number
- Languages: English, Polski

## Requirements

- Home Assistant 2025.3 or newer (OS, Supervised or Container)
- A fixed IP address of Home Assistant in your network (DHCP reservation in the router)
- Free TCP port 9000 on the Home Assistant host. For Docker use `network_mode: host` or publish `-p 9000:9000`.
  Do not expose this port to the internet.
- Wi-Fi 2.4 GHz for the charger
- Optional: Bluetooth near the charger (built-in, USB adapter or ESPHome Bluetooth proxy)

## Installation

### HACS (recommended)

1. HACS → ⋮ → **Custom repositories** → add `https://github.com/GNLT-EV/gnlt-home-assistant`, type **Integration**.
2. Find **GNLT EV Charger** in HACS and click **Download**.
3. Restart Home Assistant.

Updates then appear in Home Assistant like any other update.

### Manual

Copy `custom_components/gnlt_charger` into the `custom_components` folder of your Home Assistant configuration and
restart Home Assistant.

## Setup

Settings → Devices & services → **Add integration** → **GNLT EV Charger**.

- **Over Bluetooth:** first switch OCPP off on the charger (Settings → Wi-Fi on the charger screen), then follow the
  wizard; at the end switch OCPP back on.
- **Manually:** enter the 12-digit serial number; the wizard shows the server address (`ws://<ip>:9000/ocpp/`) to
  enter in the charger's OCPP settings, with the serial number as ChargeID. If Home Assistant runs in Docker
  without host networking, set Settings → System → Network → Local network to the address of your computer first.

Change the address on the charger in this order: OCPP off → enter the address → OCPP on → leave the menu.

## Good to know

- A charger connects to one server only: either the GNLT app or Home Assistant.
- After a network loss the charger is shown as connected for about two more minutes — this is intended.
- If the car did not take the charge (usually a car that has gone to sleep), the charge may start by itself when the
  car wakes up. If you do not want that, unplug the cable.
- Some switches may be inactive (grey) — these features depend on the charger's firmware version.

## Support

Issues: https://github.com/GNLT-EV/gnlt-home-assistant/issues
