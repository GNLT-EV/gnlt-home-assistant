# GNLT EV Charger for Home Assistant

**English** · [Polski](README.pl.md) · [Русский](README.ru.md)

Local integration for GNLT EV chargers (portable and wall-mounted). The charger talks to Home Assistant
directly over OCPP 1.6J in your home network — no cloud, no external servers.

> **Before you start.** A charger connects to one server only: either the GNLT app or Home Assistant. After you
> connect it to Home Assistant, the charger disappears from the GNLT app. How to bring it back — see
> [Back to the GNLT app](#back-to-the-gnlt-app).

## Features

- Start and stop charging, charging current limit (also while charging)
- Power, current, voltage, energy of the charge and total energy — ready for the Home Assistant Energy dashboard
- Built-in charging schedule, electricity price (single or two-zone tariff), cost of the charge, day and month totals
- Warnings: the car did not take the charge, charging started by the charger itself, connector out of service
- Setup over Bluetooth (Wi-Fi and server address are written to the charger) or manually by serial number
- Languages: English, Polski, Русский

## Requirements

- Home Assistant 2025.3 or newer (OS, Supervised or Container)
- A fixed IP address of Home Assistant in your network (DHCP reservation in the router). The charger remembers the
  address; if it changes, the charger stops connecting.
- Free TCP port 9000 on the Home Assistant host. For Docker use `network_mode: host` or publish `-p 9000:9000`.
  Do not expose this port to the internet.
- Wi-Fi 2.4 GHz for the charger; network name and password may contain only letters without accents (A–Z, a–z), digits and symbols
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

## Connecting the charger

Settings → Devices & services → **Add integration** → **GNLT EV Charger**.

### Over Bluetooth (recommended)

1. On the charger screen open **Settings → Wi-Fi** and switch **OCPP off**. While OCPP is on, the charger is not
   visible over Bluetooth. Wi-Fi stays on.
2. In the wizard choose **Set up a charger over Bluetooth**. The charger may also appear by itself as a discovered
   device.
3. Choose the charger. Over Bluetooth it is named by its serial number or `BL602-BLE-DEV`.
4. Enter the Wi-Fi network name and password. Check the Home Assistant address — it is filled in automatically.
   Leave **Start charging by command, not by plugging in** checked.
5. Home Assistant writes the settings to the charger — about half a minute, stay near the charger.
6. Check the charger version (single- or three-phase and power). Usually the charger reports it itself.
7. On the charger: wait for the Wi-Fi icon on the screen, then in **Settings → Wi-Fi** switch **OCPP on**. This step
   is done only by hand. In Home Assistant confirm — it waits for the charger.

### Manually (without Bluetooth)

1. In the wizard choose **The charger is already on the network — enter its number**. Enter the 12-digit serial
   number from the housing and the charger version.
2. The wizard shows an address like `ws://192.168.1.10:9000/ocpp/`. Enter it in the charger's OCPP settings, with the
   serial number as ChargeID. The address starts with `ws://`, not `wss://`. If Home Assistant runs in Docker without
   host networking, the wizard may show the container's internal address (172.x.x.x) — then set Settings → System →
   Network → Local network to the address of your computer first.
3. Change the address on the charger in this order: OCPP off → enter the address → OCPP on → leave the menu.
   Otherwise the charger stays on the old address.

## Back to the GNLT app

1. In Home Assistant delete the device: Settings → Devices & services → GNLT EV Charger → ⋮ → Delete.
2. On the charger switch OCPP off (**Settings → Wi-Fi**).
3. In the GNLT app add the charger again over Bluetooth. The app writes the platform address to the charger itself.
4. Switch OCPP on on the charger.

## Good to know

- After a network loss the charger is shown as connected for about two more minutes — this is intended.
- If the car did not take the charge (usually a car that has gone to sleep), the charge may start by itself when the
  car wakes up. If you do not want that, unplug the cable.
- Some switches may be inactive (grey) — these features depend on the charger's firmware version.

## If something goes wrong

- **No Wi-Fi icon on the charger** — wrong network name or password, or the network is 5 GHz only. Repeat the setup.
- **Wi-Fi icon is there, but the charger does not connect** — check that OCPP is on, the Home Assistant address is
  right and port 9000 is reachable. Unplugging the charger for a minute also helps.
- **The charger is not visible over Bluetooth** — OCPP is still on, or the adapter is too far away.

## Support

GNLT · info@gnlt.pl · +48 505 699 273 · https://gnlt.pl

Issues: https://github.com/GNLT-EV/gnlt-home-assistant/issues
