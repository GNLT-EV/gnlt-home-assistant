# GNLT EV Charger for Home Assistant

**English** · [Polski](README.pl.md) · [Русский](README.ru.md)

Integration of GNLT EV chargers (portable and wall-mounted) with Home Assistant. The charger works with Home Assistant
directly in your home network, without a cloud.

> **Important.** The charger works either with the EV-Charger app or with Home Assistant, not with both at the same time.
> After you connect it to Home Assistant, the charger disappears from the EV-Charger app. How to bring it back — see
> [Back to the EV-Charger app](#back-to-the-ev-charger-app).

## Features

- Start and stop charging, current limit (can be changed during charging)
- Power and apparent power (U×I), current, voltage, energy of the charge; total energy — for the Home Assistant Energy dashboard
- Charging schedule, electricity price (single or two-rate tariff), cost of charging, totals for the day and the month
- Warnings: "The car did not start charging", "Charging started by the charger itself", "Connector out of service",
  "The charger stopped charging" - with the likely cause
- How many phases are charging the car right now
- A diagnostics file for support
- Connecting the charger over Bluetooth or manually
- ModBus TCP mode as an alternative to OCPP
- Load management and PV charging for several ModBus chargers (beta)
- Languages: English, Polish, Russian

## What you need

- Home Assistant 2025.11 or newer.
- A fixed IP address of the computer or server running Home Assistant in your home network. Reserve it in your
  router settings.
- Only if Home Assistant runs in Docker: start the container with `network_mode: host` or with `-p 9000:9000`.
  Do not open this port to the internet.
- A 2.4 GHz Wi-Fi network with WPA2 security. 5 GHz networks, WPA3 and mixed modes (WPA/WPA2, WPA2/WPA3) will not
  work. Network name and password — up to 32 characters: only Latin letters without accents, digits and symbols.
- To connect over Bluetooth: Bluetooth in Home Assistant near the charger — built-in, a USB adapter or an ESPHome
  Bluetooth Proxy.
- To connect manually: the EV-Charger app on your phone.

## Installation

1. Open **HACS**.
2. In the **⋮** menu choose **Custom repositories**.
3. Paste `https://github.com/GNLT-EV/gnlt-home-assistant`, choose **Integration** in the **Type** field and click
   **Add**. Close the window.
4. Type `GNLT EV Charger` in the HACS search and open the integration.
5. Click **Download**, then **Download** again in the window that opens.
6. Restart Home Assistant: **Settings** → **System** → **⏻** → **Restart Home Assistant** → **Restart**.

New versions will appear in **Settings** → **System** → **Updates**.

## Connecting the charger over Bluetooth

1. Close the EV-Charger app on your phone. On the charger press the **OK** button, choose **Wi-Fi**, make sure Wi-Fi
   is switched on, switch **OCPP** off and leave the menu.
2. In Home Assistant open **Settings** → **Devices & services** → **Add integration**.
3. Type `GNLT` in the search and choose **GNLT EV Charger**.
4. Choose **Set up a charger over Bluetooth (OCPP, recommended)**.
5. In the **Charger** field choose your charger — its serial number is on the housing — and click **Submit**. If you
   see "Charger not found", check that OCPP is off on the charger and the EV-Charger app is closed, move the Bluetooth
   adapter (or the computer running Home Assistant, if Bluetooth is built in) closer to the charger and click
   **Submit** again. If that does not help, connect the charger
   [manually](#connecting-the-charger-manually).
6. Fill in the fields and click **Submit**:
   - **Network name** and **Network password** — your Wi-Fi network;
   - **Home Assistant address in your network** — the IP address of the computer or server running Home Assistant,
     for example `192.168.1.10`. If you open Home Assistant in the browser at an address like
     `http://192.168.1.10:8123`, your address is `192.168.1.10`. Otherwise find it in your router's list of devices.
     If the field shows a different address, replace it with yours;
   - **Port for chargers**, if this field is shown — `9000`. If you see "This port is used by another program", enter
     `9001`. If Home Assistant runs in Docker with `-p 9000:9000`, recreate the container with `-p 9001:9001` and
     start again from step 2;
   - **Start charging by command, not by plugging in** — leave it switched on.

   If the form shows an error in the network name or password, change them in the router settings, enter the new
   ones in the form and click **Submit**.
7. Wait about half a minute while the charger saves the settings. If an error appears, do what it says. If the error
   keeps coming back, connect the charger [manually](#connecting-the-charger-manually).
8. Check that your charger version is selected — the number of phases and the power, as on the charger's rating
   plate. If a different one is selected, select yours. Click **Submit**.
9. On the charger wait for the Wi-Fi icon on the screen. Press **OK**, choose **Wi-Fi**, switch **OCPP** on and leave
   the menu. If the Wi-Fi icon does not appear, check that Wi-Fi is switched on at the charger: if it is off, switch it
   on and wait for the icon. If it is on, close the window in Home Assistant, compare the network name and password
   with your router settings, check there that the network is 2.4 GHz with WPA2 security without a mixed mode and
   start again from step 1.
10. In Home Assistant click **Submit** and wait until the charger connects — up to 10 minutes.
11. In the **Name and assign** window click **Skip and finish**.

The charger appears in **Settings** → **Devices & services** → **GNLT EV Charger**.

If the window "The charger has not connected yet" appears instead of **Name and assign**, check the points in it.
The server address is shown on the charger: **OK** → **Wi-Fi**. If OCPP is off, switch it on. Then click **Submit**:
then **Skip and finish**: the charger is added and connects as soon as the connection works. If there is no Wi-Fi
icon or the address is
different, see [If something goes wrong](#if-something-goes-wrong) afterwards.

## Cards in "Discovered"

Home Assistant may show the charger by itself in **Settings** → **Devices & services**, section **Discovered**. Click
**Add** on the card and see which window opens:

- **"GNLT charger nearby"** — the charger is in Bluetooth mode. Close the EV-Charger app on your phone, click
  **Submit** and continue from step 6 of [Connecting the charger over Bluetooth](#connecting-the-charger-over-bluetooth).
- **"Charger … connected"** — the charger connected to Home Assistant by itself. Select the charger version, click
  **Submit**, then **Skip and finish**.
- **"This is not a GNLT charger."** — click **Close**, and on the card click **Ignore**.

## Connecting the charger manually

Use this way if Home Assistant has no Bluetooth.

1. Connect the charger to the EV-Charger app and to your Wi-Fi network as described in the charger manual. If the app
   does not find the charger, switch OCPP off on the charger. Make sure Wi-Fi is switched on at the charger.
2. In Home Assistant open **Settings** → **Devices & services** → **Add integration**.
3. Type `GNLT` in the search and choose **GNLT EV Charger**.
4. Choose **OCPP: the charger is already on the network — enter its number**.
5. Enter the **Serial number** (12 digits from the charger housing), choose the **Charger version** — the number of
   phases and the power, as on the charger's rating plate. If there is a **Port for chargers** field, leave `9000` (if
   the port is busy — `9001`, as in step 6 of the Bluetooth section). Click **Submit**.
6. Home Assistant shows the server address and the ChargeID. Check that the server address contains the IP address of
   your Home Assistant (as in step 6 of the Bluetooth section). If it shows a different address, for example
   `ws://172.17.0.2…`, close the window, open **Settings** → **System** → **Network**, in **Local network** switch
   **Automatic** off, enter `http://computer-IP-address:8123`, for example `http://192.168.1.10:8123`, click **Save**
   and start again from step 2. If the address is right, keep the window open.
7. In the app, in the OCPP settings, enter the server address and the ChargeID from the Home Assistant window.
8. On the charger press **OK** and choose **Wi-Fi** — the address you entered is shown there. If there is no address
   or it is different, check what you entered in the app (step 7). Switch **OCPP** on and leave the menu.
9. In Home Assistant click **Submit**, then **Skip and finish**.

## Changing the Wi-Fi network

**With Bluetooth** — you do not need to delete the charger from Home Assistant:

1. Close the EV-Charger app on your phone. On the charger press **OK**, choose **Wi-Fi**, switch **OCPP** off and
   leave the menu.
2. Do steps 2–8 of [Connecting the charger over Bluetooth](#connecting-the-charger-over-bluetooth) with the new
   network.
   If the charger is not found over Bluetooth, use the "Without Bluetooth" way.
3. After the charger version window you see "The charger received the new settings" — click **Close**.
4. Wait for the Wi-Fi icon on the charger screen. Press **OK**, choose **Wi-Fi**, switch **OCPP** on and leave the menu.

**Without Bluetooth:**

1. On the charger press **OK**, choose **Wi-Fi**, switch **OCPP** off and leave the menu.
2. In Home Assistant delete the charger: **Settings** → **Devices & services** → **GNLT EV Charger** → next to the
   charger **⋮** → **Delete** and **Delete** again.
3. If the charger is not in the EV-Charger app, add it as in step 4 of
   [Back to the EV-Charger app](#back-to-the-ev-charger-app).
4. Connect the charger as described in [Connecting the charger manually](#connecting-the-charger-manually) with the
   new network.

## Back to the EV-Charger app

1. On the charger press **OK**, choose **Wi-Fi**, switch **OCPP** off and leave the menu.
2. In Home Assistant open **Settings** → **Devices & services** → **GNLT EV Charger**.
3. Next to the charger click **⋮** → **Delete** and **Delete** again.
4. In the EV-Charger app on your phone add the charger again: tap the **QR code** icon or **+**, scan the charger's
   QR code, enter the PUK code (on the back of the charger manual and under the charger cover) and confirm adding.
5. On the charger press **OK**, choose **Wi-Fi**, switch **OCPP** on and leave the menu. The charger connects to the
   EV-Charger app.

## ModBus mode

Instead of OCPP, a charger can be added over **ModBus TCP** in the local network. ModBus sets the current and
switches between 1 and 3 phases.

**Prepare the charger** (also shown in the wizard):

1. Connect the charger to Wi-Fi (2.4 GHz) with the charger app over Bluetooth, before switching ModBus on. Reserve its
   IP address in the router. ModBus TCP works only over Wi-Fi, not with the EM3-Box.
2. Charger menu **Load & communication**: load balancing on, communication mode **ModBus TCP**. OCPP and ModBus
   exclude each other: the charger works in one of the two modes at a time.
3. Menu **Current adjustment** at the maximum: the charger uses the lower of this value and the limit from Home
   Assistant.
4. Menu **Charge mode**: switch Plug & charge off if Home Assistant should decide when to charge (it is on from the
   factory).
5. The charger's own reservations off; date and time set on the charger.
6. The charger's "1-phase mode" off if you want to switch between 1 and 3 phases.
7. Only one ModBus program at a time: the charger accepts one ModBus connection. Switch off evcc, other integrations
   and any other device using port 502 of the charger.

**Adding the charger:**

1. In Home Assistant open **Settings** → **Devices & services** → **Add integration**.
2. Type `GNLT` in the search and choose **GNLT EV Charger**.
3. Choose **ModBus TCP (local network)** and click **Submit**.
4. Enter the charger's IP address (shown in the charger's Wi-Fi menu) and leave port `502`. Click **Submit**.
5. Check the charger version — the number of phases and the power, as on the rating plate. Click **Submit**.

**Firmware.** Stopping a charge and switching phases while charging over ModBus require firmware 3.1 or newer (the
version is shown on the charger screen). On older firmware, for example 2.7, the charger does not stop on a stop
command: it lowers the current to the minimum and accepts no new commands until the cable is unplugged from the car. To
stop a charge on such firmware, unplug the cable from the car. For full control use the OCPP connection.

**Entities:** status, charger message, cable, power, apparent power, power L1–L3, currents and voltages L1–L3, phases in
use, energy of the charge and total energy (Energy dashboard), prices and costs, energy and cost today and this month,
last charge, schedule, start when the cable is connected, continue after a power cut, Plug & Charge, current limit,
charging on/off, phases 1/3, refresh, the button "Turn Plug & Charge off and stop", notifications and diagnostics.
Diagnostic entities: the charger's current setpoint, minimum, maximum and cable current, the charger's meter,
charge duration, connection errors and "Stop does not work". "Stop does not work" turns on when the charger keeps
charging after a stop command (firmware older than 3.1).

**Not available over ModBus:** restart of the charger, "Interrupt charging", temperature, firmware version, the car's
state of charge, and Bluetooth set-up while ModBus is on.

**Poll interval.** Home Assistant reads the charger every 10 seconds. To change it: **Settings** → **Devices &
services** → **GNLT EV Charger** → the ModBus entry → **Configure** → "How often Home Assistant reads the charger,
seconds".

**Switching between OCPP and ModBus.** One charger can have two entries: an OCPP entry and a ModBus entry. The entry
whose mode is active on the charger works; the other one shows the charger as not connected. To switch, change the
mode in the charger menu: for ModBus — **Load & communication** → ModBus TCP; for OCPP — switch ModBus TCP off there and
switch **OCPP** on in the **Wi-Fi** menu. You may disable the entry you do not use. In the Energy dashboard use only one **Energy total (HA)** sensor.

**Good to know (ModBus)**

- **Wi-Fi:** 2.4 GHz, WPA2 only, ideally a separate network name without mesh roaming, and a fixed IP address.
- **Plug & Charge:** when it is on, the charger starts charging by itself as soon as the cable is connected. After a
  power cut the charger may switch it back on; Home Assistant then offers a one-click repair. The button "Turn Plug &
  Charge off and stop" does both at once.
- **Automatic starts** (start when the cable is connected, schedule, continue after a power cut) are at least
  3 minutes apart. A start by hand is not limited.
- **Stopping without ModBus:** in the car app, in the charger app over Bluetooth (with ModBus switched off), or by
  unplugging the cable.

**If the charger does not answer** (in this order): wait 3 minutes → check that no other ModBus program or device is
connected → in the charger menu switch ModBus TCP or load balancing off and on → switch the charger's mains power off
for at least 10 seconds and wait about 20 seconds. A factory reset also deletes the ModBus settings.

## Load management and PV for several chargers (beta)

> **Beta.** This function is new. It does not replace the electrical protection of the installation: the circuit
> breakers stay as they are. Your feedback is welcome — see [Support](#support).

A **GNLT site** controls several chargers added over ModBus together:

- no phase of the grid connection goes above the fuse limit you set — the site measures the grid current of each phase
  and shares the free current between the chargers;
- each charger can charge from PV surplus.

On the device page of each charger choose the **Charge mode**: **Off**, **Fast**, **Min + PV** or **PV**.

**What you need:** chargers added over ModBus and one current sensor per grid phase in Home Assistant (amperes, import
positive, export negative). A grid power sensor is optional: without it the grid power is calculated from the three
currents × 230 V.

**Adding the site:**

1. In Home Assistant open **Settings** → **Devices & services** → **Add integration**.
2. Type `GNLT` in the search and choose **GNLT EV Charger**.
3. Choose **GNLT site: load management and PV for several ModBus chargers (beta)**.
4. Choose the grid current sensors, enter the fuse limit per phase, choose the chargers of the site and click
   **Submit**. Then set the priority and the grid phase of each charger and the PV settings.

If the grid meter stops sending values, the site does what is set in "When the grid meter fails".

If Home Assistant or the network stops, each charger keeps its last current; the circuit breaker of the installation remains the protection.

## Good to know

- The names of the charger's sensors and switches are shown in the Home Assistant language set in the system settings.
- Add the **Energy total (HA)** sensor to the Energy dashboard: it counts every charge, including those the charger started by itself.
  It counts energy since the charger was added to Home Assistant; "Total" on the charger's screen is the energy over its whole life.
- On chargers whose firmware contains `HW:B07` (for example `SW:A3B_2.7-HW:B07_0.4` or `SW:A3B_3.1-HW:B07_0.5`; the firmware is
  shown on the device page in Home Assistant over OCPP), the charger reports the power of one phase when charging on three phases.
  The **Power** sensor then shows the sum of U×I of the phases (a calculated value); the charger's own figure is in the
  sensor's attributes.
- After a connection loss the charger is shown as connected for about two more minutes.
- If the car did not start charging, charging may start by itself when the car wakes up. If you do not want that,
  unplug the cable.
- Some switches may be grey: these features depend on the charger's firmware version.

## If something goes wrong

- **No Wi-Fi icon on the charger.** On the charger press **OK**, choose **Wi-Fi** and check that Wi-Fi is switched on.
  Check in the router settings: 2.4 GHz network, WPA2 security without a mixed mode. Then enter the network name and
  password again: if the charger is not added yet — from step 1 of
  [Connecting the charger over Bluetooth](#connecting-the-charger-over-bluetooth) or from step 1 of
  [Connecting the charger manually](#connecting-the-charger-manually); if it is already added — see
  [Changing the Wi-Fi network](#changing-the-wi-fi-network).
- **The Wi-Fi icon is there, but the charger does not connect to Home Assistant.** On the charger press **OK** and
  choose **Wi-Fi**: OCPP must be switched on and the server address must contain the IP address of your Home
  Assistant. If the address is different, see [Changing the Wi-Fi network](#changing-the-wi-fi-network) with the same
  network, with or without Bluetooth. If Home Assistant runs in Docker, check that the port is published. If
  everything is right and the charger still does not connect, contact us — see below.
- **The charger is not visible over Bluetooth.** Switch OCPP off on the charger, close the EV-Charger app on your phone
  and move the Bluetooth adapter (or the computer running Home Assistant) closer to the charger. Or connect the charger
  [manually](#connecting-the-charger-manually).
- **"This charger is already added."** The charger is already in **Settings** → **Devices & services** →
  **GNLT EV Charger**. To change the Wi-Fi network, see [Changing the Wi-Fi network](#changing-the-wi-fi-network).
- **"This charger is already being set up in another window."** Click **Close**. If there is a card of this charger
  in **Discovered**, click **Add** on it and continue. If there is no card, restart Home Assistant and start again.
- **"This charger version cannot work with Home Assistant over OCPP."** Contact the seller. To use the EV-Charger app,
  do steps 1, 4 and 5 of [Back to the EV-Charger app](#back-to-the-ev-charger-app).
- **Notification "… the charger stopped charging".** Do what the notification says. "Current above the limit" usually means
  the car does not follow the set current limit and takes more. Set a different **Current limit** - for example
  10 A or the maximum - then unplug the cable from the car and plug it in again.
- **Readings or charging do not behave as you expect.** Right after it happens, download the diagnostics file:
  **Settings** → **Devices & services** → **GNLT EV Charger** → next to the charger **⋮** → **Download diagnostics**.
  Send us the file - see below. The file does not contain your Wi-Fi network name or password.

## Support

GNLT · info@gnlt.pl · +48 505 699 273 · https://gnlt.pl

Issues: https://github.com/GNLT-EV/gnlt-home-assistant/issues
